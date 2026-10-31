import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docker_support.common import RotatingJSON, read_json
from docker_support.compose_source import compose
from docker_support.diagnostics import publish_packet, safe_events, snapshot
from docker_support.supervisor import classify, TriggerPolicy


class SourceTests(unittest.TestCase):
    def test_composition_matches_existing_fixes_and_preserves_original(self):
        raw = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "upstream").rglob("*") if p.is_file()}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "runtime"
            manifest = compose(ROOT / "upstream", out, ROOT / "patches/upstream.json", ROOT / "patches/public.json")
            for p in json.loads((ROOT / "patches/public.json").read_text())["files"]:
                self.assertEqual((out / p["path"]).read_text(), (ROOT / p["path"]).read_text())
            self.assertEqual((out / "module/live_recorder.py").read_bytes(), (ROOT / "upstream/module/live_recorder.py").read_bytes())
            self.assertEqual(manifest["upstream_version"], "1.2.9")
            with self.assertRaises(ValueError):
                compose(ROOT / "upstream", out, ROOT / "patches/upstream.json", ROOT / "patches/public.json")
        self.assertEqual(raw, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "upstream").rglob("*") if p.is_file()})

    def test_unknown_baseline_refused_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "wrong.json"
            wrong.write_text('{"files": {}}')
            with self.assertRaises(ValueError):
                compose(ROOT / "upstream", Path(tmp) / "output", wrong, ROOT / "patches/public.json")
            self.assertFalse((Path(tmp) / "output").exists())


class CaptureTests(unittest.TestCase):
    def test_rotation_is_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            writer = RotatingJSON(path, 1024, 2)
            for i in range(100):
                writer.write({"n": i, "x": "x" * 180})
            writer.close()
            files = list(Path(tmp).glob("log.jsonl*"))
            self.assertEqual(len(files), 3)
            for p in files:
                self.assertLessEqual(p.stat().st_size, 1024)
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text().splitlines()[-1])["n"], 99)

    def test_log_symlink_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.write_text("preserve")
            link = Path(tmp) / "log"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                RotatingJSON(link).write({"x": 1})
            self.assertEqual(real.read_text(), "preserve")

    def test_severity_and_rate_policy(self):
        self.assertIsNone(classify("INFO: GET /status HTTP/1.1 200"))
        self.assertIsNone(classify("normal stderr output"))
        self.assertEqual(classify("[FSM][ERROR] OSError"), ("ERROR", "application"))
        self.assertEqual(classify("[CIME][ERROR] 녹화 프로세스 오류: connection timeout"), ("ERROR", "process"))
        self.assertEqual(classify("[cli][error] ffmpeg failed"), ("ERROR", "process"))
        p = TriggerPolicy(120, 3, 60)
        self.assertFalse(p.observe("WARNING", "storage", 0))
        self.assertFalse(p.observe("WARNING", "storage", 1))
        self.assertTrue(p.observe("WARNING", "storage", 2))
        self.assertFalse(p.observe("ERROR", "storage", 3))
        self.assertTrue(p.observe("ERROR", "storage", 123))
        q = TriggerPolicy()
        self.assertFalse(q.observe("ERROR", "network", 0))
        self.assertTrue(q.observe("ERROR", "network", 1))


class DiagnosticTests(unittest.TestCase):
    def test_packet_excludes_free_text_and_secret_config_and_has_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app/json").mkdir(parents=True)
            sentinel = "PRIVATE_COOKIE_CHANNEL_TITLE_DO_NOT_EXPORT"
            (root / "app/json/config.json").write_text(json.dumps({"autoPostProcessing": True, "recheckInterval": 60, "filenamePattern": sentinel, "cookie": sentinel, "extra_ffmpeg_options": sentinel, "video_codec": sentinel}))
            events = [{"time": "2026-10-31T00:00:00+00:00", "code": "storage", "level": "ERROR", "message": sentinel, "exception_type": "OSError", "errno": "ENOSPC"}]
            docs = snapshot(root / "app", os.getpid(), "manual", events, {"file_dropped": 1, "secret": sentinel})
            for _ in range(5):
                path = publish_packet(root / "logs", docs)
            self.assertEqual(len(list((root / "logs/diagnostics").glob("*.zip"))), 3)
            with zipfile.ZipFile(path) as z:
                data = b"".join(z.read(n) for n in z.namelist())
                self.assertNotIn(sentinel.encode(), data)
                self.assertIn(b"ENOSPC", data)
                self.assertEqual(len(z.namelist()), 7)
            self.assertLess(path.stat().st_size, 4 * 1024 * 1024)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_packet_budget_and_json_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                publish_packet(tmp, {"large.json": "x" * 5000}, maximum=1024)
            link = Path(tmp) / "link"
            link.symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                read_json(link)


class SupervisorIntegrationTests(unittest.TestCase):
    def launch(self, tmp, program, **extra):
        env = dict(os.environ, RECORDWEB_LOG_DIR=str(Path(tmp) / "logs"), RECORDWEB_APP_ROOT=str(Path(tmp) / "app"), RECORDWEB_DIAG_POST_SECONDS="0")
        env.update(extra)
        return subprocess.Popen([sys.executable, str(ROOT / "docker_support/supervisor.py"), "--", sys.executable, "-u", "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)

    def test_raw_output_unicode_children_partial_lines_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            program = "import os,sys,subprocess;print('한글');subprocess.run([sys.executable,'-c',\"import os;os.write(2,b'child-stderr\\\\n')\"]);os.write(1,('가'*20000+'\\rfinal').encode());sys.exit(7)"
            proc = self.launch(tmp, program)
            out, err = proc.communicate(timeout=12)
            self.assertEqual(proc.returncode, 7)
            self.assertIn("한글".encode(), out)
            self.assertIn(b"child-stderr", err)
            self.assertTrue(out.endswith(b"\rfinal"))
            records = [json.loads(x) for x in (Path(tmp) / "logs/console.jsonl").read_text().splitlines()]
            text = "".join(r["message"] for r in records if r["stream"] == "stdout")
            self.assertIn("가" * 20000, text)
            self.assertTrue(text.endswith("\rfinal"))
            packs = list((Path(tmp) / "logs/diagnostics").glob("*.zip"))
            self.assertTrue(packs)
            with zipfile.ZipFile(packs[-1]) as z:
                self.assertEqual(json.loads(z.read("manifest.json"))["trigger"], "child_exit")

    def test_repeated_warning_captures_but_single_warning_does_not(self):
        for count in (1, 3):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                proc = self.launch(tmp, "print('[WARN] disk issue\\n'*" + str(count) + ",end='')")
                proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(bool(list((Path(tmp) / "logs/diagnostics").glob("*.zip"))), count == 3)

    def test_sink_failure_does_not_kill_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "logs").write_text("not a directory")
            proc = self.launch(tmp, "print('application-keeps-running')")
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertIn(b"application-keeps-running", out)

    def test_sigterm_reaches_app_and_final_output_is_drained(self):
        with tempfile.TemporaryDirectory() as tmp:
            program = "import signal,time,sys;signal.signal(signal.SIGTERM,lambda *a:(print('cleanup-complete'),sys.exit(0)));print('ready',flush=True);time.sleep(60)"
            proc = self.launch(tmp, program)
            self.assertEqual(proc.stdout.readline().strip(), b"ready")
            os.killpg(proc.pid, signal.SIGTERM)  # same forwarding contract as container init
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertIn(b"cleanup-complete", out)
            self.assertIn("cleanup-complete", (Path(tmp) / "logs/console.jsonl").read_text())

    def test_manual_cli_after_app_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.launch(tmp, "print('ok')")
            proc.communicate(timeout=10)
            r = subprocess.run([sys.executable, str(ROOT / "docker_support/diagnostics.py"), "--log-dir", str(Path(tmp) / "logs"), "--app-root", str(Path(tmp) / "app"), "--symptom", "recording"], capture_output=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            path = Path(json.loads(r.stdout)["packet"])
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
