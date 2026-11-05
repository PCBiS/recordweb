"""Offline integration tests for the public replay router hook."""
from contextlib import asynccontextmanager
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
integration = importlib.import_module("docker_support.replay.integration")


class FakeManager:
    instances = []
    events = []
    fail_start = False
    file_path = None

    def __init__(self, *args, **kwargs):
        self.root = Path(args[1])
        self.__class__.instances.append(self)

    async def start(self):
        self.__class__.events.append("manager-enter")
        if self.__class__.fail_start:
            raise RuntimeError("offline startup failure")

    async def close(self):
        self.__class__.events.append("manager-exit")

    def list_jobs(self, owner):
        return {"owner": owner, "jobs": []}

    async def analyze(self, owner, urls):
        return {"owner": owner, "urls": urls}

    def resolve_file(self, owner, ident):
        assert owner == "recordweb-account"
        assert ident == "done-job"
        return Path(self.__class__.file_path)


class ReplayRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_env = {
            name: os.environ.get(name)
            for name in ("RECORDWEB_REPLAY_DOWNLOAD_ROOT", "RECORDWEB_REPLAY_STATE_DIR")
        }
        self.addCleanup(self.restore_env)
        os.environ["RECORDWEB_REPLAY_DOWNLOAD_ROOT"] = str(self.root / "downloads")
        os.environ["RECORDWEB_REPLAY_STATE_DIR"] = str(self.root / "state")
        (self.root / "downloads").mkdir()
        (self.root / "templates").mkdir()
        (self.root / "templates" / "replay.html").write_text("replay", encoding="utf-8")
        FakeManager.instances = []
        FakeManager.events = []
        FakeManager.fail_start = False
        FakeManager.file_path = None
        integration.ReplayManager = FakeManager

    def restore_env(self):
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def make_app(self, login_mode=True):
        templates = Jinja2Templates(directory=str(self.root / "templates"))
        templates.env.globals.update(program_version="test", web_ui_title="test")

        @asynccontextmanager
        async def original_lifespan(app):
            app.state.config = {"loginMode": login_mode}
            FakeManager.events.append("original-enter")
            try:
                yield
            finally:
                FakeManager.events.append("original-exit")

        app = FastAPI(lifespan=original_lifespan)
        app.add_middleware(SessionMiddleware, secret_key="test-only-session-secret")

        async def require_login(request: Request):
            if request.session.get("logged_in"):
                return True
            raise HTTPException(401, "Login required")

        @app.get("/login-fixture")
        async def login_fixture(request: Request):
            # Match RecordWEB: authentication stores logged_in without username.
            request.session["logged_in"] = True
            return JSONResponse({"ok": True})

        @app.get("/original")
        async def original_route():
            return {"ok": True}

        integration.install_replay(app, templates, require_login, str(self.root))
        return app

    async def client(self, app, *, authenticated=False, follow_redirects=True):
        cookies = None
        if authenticated:
            bootstrap = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )
            await bootstrap.get("/login-fixture")
            cookies = bootstrap.cookies
            await bootstrap.aclose()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=follow_redirects,
            cookies=cookies,
        )

    async def test_unauthenticated_replay_api_requires_login(self):
        app = self.make_app()
        async with app.router.lifespan_context(app):
            async with await self.client(app) as client:
                response = await client.get("/api/replay/capabilities")
        self.assertEqual(response.status_code, 401)

    async def test_capabilities_accepts_logged_in_only_and_sets_csrf(self):
        app = self.make_app()
        async with app.router.lifespan_context(app):
            async with await self.client(app, authenticated=True) as client:
                response = await client.get("/api/replay/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "live")
        self.assertTrue(response.json()["csrf_token"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_mutations_require_csrf_and_same_origin(self):
        app = self.make_app()
        async with app.router.lifespan_context(app):
            async with await self.client(app, authenticated=True) as client:
                caps = await client.get("/api/replay/capabilities")
                token = caps.json()["csrf_token"]
                missing = await client.post("/api/replay/analyses", json={"urls": []})
                invalid = await client.post("/api/replay/analyses", json={"urls": []}, headers={"x-csrf-token": "wrong"})
                cross_origin = await client.post("/api/replay/analyses", json={"urls": []}, headers={"x-csrf-token": token, "origin": "https://evil.example"})
                accepted = await client.post("/api/replay/analyses", json={"urls": ["https://example.test/vod"]}, headers={"x-csrf-token": token, "origin": "http://test"})
        self.assertEqual((missing.status_code, invalid.status_code, cross_origin.status_code), (403, 403, 403))
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["owner"], "recordweb-account")

    async def test_login_mode_off_blocks_replay_but_preserves_original_route(self):
        app = self.make_app(login_mode=False)
        async with app.router.lifespan_context(app):
            async with await self.client(app) as client:
                replay = await client.get("/api/replay/jobs")
                original = await client.get("/original")
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(original.status_code, 200)

    async def test_replay_manager_lifespan_is_inside_original_lifespan(self):
        app = self.make_app()
        async with app.router.lifespan_context(app):
            self.assertEqual(FakeManager.events, ["original-enter", "manager-enter"])
        self.assertEqual(FakeManager.events, ["original-enter", "manager-enter", "manager-exit", "original-exit"])

    async def test_startup_failure_returns_503_without_breaking_original_route(self):
        app = self.make_app()
        FakeManager.fail_start = True
        async with app.router.lifespan_context(app):
            async with await self.client(app, authenticated=True) as client:
                replay = await client.get("/api/replay/jobs")
                original = await client.get("/original")
        self.assertEqual(replay.status_code, 503)
        self.assertEqual(original.status_code, 200)
        self.assertEqual(FakeManager.events, ["original-enter", "manager-enter", "manager-exit", "original-exit"])

    async def test_completed_download_supports_ranges_and_safe_headers(self):
        app = self.make_app()
        media = self.root / "finished.mp4"
        media.write_bytes(b"0123456789")
        FakeManager.file_path = media
        async with app.router.lifespan_context(app):
            async with await self.client(app, authenticated=True) as client:
                response = await client.get("/api/replay/jobs/done-job/file", headers={"range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    async def test_malformed_json_and_oversized_payload_are_rejected(self):
        app = self.make_app()
        async with app.router.lifespan_context(app):
            async with await self.client(app, authenticated=True) as client:
                token = (await client.get("/api/replay/capabilities")).json()["csrf_token"]
                headers = {"x-csrf-token": token}
                malformed = await client.post("/api/replay/analyses", content=b"not-json", headers=headers)
                oversized = await client.post("/api/replay/analyses", content=b"{" + b" " * 32768 + b"}", headers=headers)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(oversized.status_code, 413)

    async def test_replay_page_redirects_anonymous_login_mode_session(self):
        app = self.make_app()
        async with app.router.lifespan_context(app):
            async with await self.client(app, follow_redirects=False) as client:
                response = await client.get("/replay")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")


if __name__ == "__main__":
    unittest.main()
