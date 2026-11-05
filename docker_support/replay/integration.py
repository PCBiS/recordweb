"""Attach replay to the existing RecordWEB app, session and lifespan."""
from contextlib import asynccontextmanager, suppress
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from .manager import ReplayManager, ReplayError
from .settings import ReplaySettingsController, resolve_settings


def install_replay(app, templates, require_login, app_dir, config_namespace=None):
    if getattr(app.state, 'replay_installed', False):
        return
    app.state.replay_installed = True
    directory = Path(app_dir).resolve()
    state_dir = Path(os.environ.get('RECORDWEB_REPLAY_STATE_DIR', directory / 'json' / 'replay'))
    app.state.replay = None
    app.state.replay_startup_error = None
    namespace = config_namespace or {}
    load_config = namespace.get('loadConfig', lambda: dict(getattr(app.state, 'config', {}) or {}))
    def fallback_save(config):
        app.state.config = dict(config)
        return config
    save_config = namespace.get('saveConfig', fallback_save)
    controller = ReplaySettingsController(app, state_dir, directory, ReplayManager, load_config, save_config)
    app.state.replay_settings = controller

    def template_context(request):
        cfg = getattr(request.app.state, 'config', {}) or {}
        return {'loginMode': bool(cfg.get('loginMode', False)), 'replay_enabled': controller.enabled, 'replay_settings_installed': True}
    templates.context_processors.append(template_context)

    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            try:
                try:
                    await controller.start(application.state.config)
                except Exception as exc:
                    application.state.replay_startup_error = '다시보기 저장소를 준비하지 못했습니다. 관리자에게 문의해 주세요.'
                    print('[WARN] Replay startup unavailable: ' + type(exc).__name__)
                yield
            finally:
                with suppress(Exception, asyncio.CancelledError):
                    await controller.close()
    app.router.lifespan_context = lifespan

    async def access(request: Request):
        # requireLogin intentionally permits the rest of the app when loginMode is off.
        # Downloads always require authenticated access and do not inherit that bypass.
        if not controller.enabled:
            raise HTTPException(403, '다시보기 기능이 비활성화되어 있습니다.')
        cfg = getattr(request.app.state, 'config', {}) or {}
        if not cfg.get('loginMode', False):
            raise HTTPException(403, '설정관리에서 로그인 모드를 켜고 로그인해 주세요.')
        await require_login(request)
        if not request.session.get('logged_in'):
            raise HTTPException(401, '로그인이 필요합니다.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.session.get('replay_csrf', '')
            supplied = request.headers.get('x-csrf-token', '')
            if not token or not supplied.isascii() or not hmac.compare_digest(token, supplied):
                raise HTTPException(403, '화면을 새로고침한 뒤 다시 시도해 주세요.')
            origin = request.headers.get('origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != request.headers.get('host', '').lower():
                    raise HTTPException(403, '다른 사이트에서 보낸 요청은 허용하지 않습니다.')
            if request.headers.get('sec-fetch-site') == 'cross-site':
                raise HTTPException(403, '다른 사이트에서 보낸 요청은 허용하지 않습니다.')
        # Current RecordWEB has one account and stores only logged_in at login.
        return str(request.session.get('username') or 'recordweb-account')

    def manager(request):
        service = request.app.state.replay
        if service is None:
            raise HTTPException(503, request.app.state.replay_startup_error or '다시보기 기능을 준비하고 있습니다.')
        return service

    def invoke(function, *args):
        try:
            return function(*args)
        except ReplayError as exc:
            raise HTTPException(exc.status, exc.detail)

    async def invoke_async(function, *args):
        try:
            return await function(*args)
        except ReplayError as exc:
            raise HTTPException(exc.status, exc.detail)

    async def payload(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise HTTPException(413, '요청이 너무 큽니다.')
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise HTTPException(400, 'JSON 객체가 필요합니다.')

    @app.get('/replay')
    async def replay_page(request: Request):
        cfg = getattr(request.app.state, 'config', {}) or {}
        if cfg.get('loginMode') and not request.session.get('logged_in'):
            return RedirectResponse('/login', status_code=303)
        return templates.TemplateResponse(request=request, name='replay.html', context={
            'program_version': templates.env.globals.get('program_version', ''),
            'web_ui_title': templates.env.globals.get('web_ui_title', '내맘대로 자동녹화 웹UI'),
        })

    router = APIRouter(prefix='/api/replay')
    @router.get('/capabilities')
    async def capabilities(request: Request, response: Response, owner=Depends(access)):
        service = manager(request)
        request.session.setdefault('replay_csrf', secrets.token_urlsafe(32))
        usage = shutil.disk_usage(service.root)
        response.headers['Cache-Control'] = 'no-store'
        return {'enabled': True, 'mode': 'live', 'csrf_token': request.session['replay_csrf'],
                'platforms': [{'id': k, 'name': n} for k, n in [('chzzk', '치지직'), ('cime', '씨미'), ('youtube', 'YouTube')]],
                'storage': {'free_bytes': usage.free, 'total_bytes': usage.total}, 'max_concurrent': 1,
                'default_root': str(service.root), 'min_free_gib': controller.current['replayMinFreeGiB']}

    @router.post('/analyses', status_code=202)
    async def analyze(request: Request, owner=Depends(access)):
        data = await payload(request)
        async with controller.lock:
            await access(request)
            return await invoke_async(manager(request).analyze, owner, data.get('urls'))

    @router.get('/analyses/{ident}')
    async def analysis(request: Request, ident: str, owner=Depends(access)):
        return invoke(manager(request).get_analysis, owner, ident)

    @router.post('/jobs')
    async def create_jobs(request: Request, owner=Depends(access)):
        data = await payload(request)
        async with controller.lock:
            await access(request)
            return await invoke_async(manager(request).create_jobs, owner, data.get('analysis_id'), data.get('items'), data.get('idempotency_key'))

    @router.get('/jobs')
    async def jobs(request: Request, owner=Depends(access)):
        return invoke(manager(request).list_jobs, owner)

    @router.get('/jobs/{ident}')
    async def job(request: Request, ident: str, owner=Depends(access)):
        return invoke(manager(request).get_job, owner, ident)

    @router.get('/jobs/{ident}/logs')
    async def logs(request: Request, ident: str, after: int=0, owner=Depends(access)):
        return invoke(manager(request).logs, owner, ident, max(0, after))

    @router.post('/jobs/{ident}/cancel')
    async def cancel(request: Request, ident: str, owner=Depends(access)):
        return await invoke_async(manager(request).cancel, owner, ident)

    @router.post('/jobs/{ident}/retry')
    async def retry(request: Request, ident: str, owner=Depends(access)):
        async with controller.lock:
            await access(request)
            return await invoke_async(manager(request).retry, owner, ident)

    @router.get('/jobs/{ident}/file')
    async def download(request: Request, ident: str, owner=Depends(access)):
        path = invoke(manager(request).resolve_file, owner, ident)
        response = FileResponse(path, filename=path.name, media_type='application/octet-stream')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app.include_router(router)
