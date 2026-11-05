"""Safe error protocol, including actual native stderr; no provider requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

from docker_support.replay.engine import guarded_core, run_download
from docker_support.replay.errors import ERROR_MESSAGES, ReplayFailure, classify_diagnostic, classify_exception, safe_code

ROOT = Path(__file__).resolve().parents[1]


class ErrorTests(unittest.TestCase):
    def test_exception_codes_are_fixed(self):
        self.assertEqual(classify_exception(ReplayFailure('quality_changed')), 'quality_changed')
        self.assertEqual(classify_exception(TimeoutError()), 'network_timeout')
        self.assertEqual(classify_exception(FileNotFoundError()), 'tool_missing')
        self.assertEqual(classify_exception(ValueError('invalid_media')), 'invalid_media')
        self.assertEqual(safe_code('RAW_SECRET_URL'), 'engine_operation_failed')
        self.assertEqual(classify_exception(Exception('unclassified synthetic detail')), 'engine_operation_failed')
        self.assertNotIn('설정', ERROR_MESSAGES['engine_operation_failed'])

    def test_native_diagnostic_classification(self):
        for message, code in [('Server returned 400 Bad Request', 'http_400'), ('HTTP error 401 Unauthorized', 'http_401'),
                              ('HTTP/1.1 403 Forbidden', 'http_403'), ('Connection timed out', 'network_timeout'),
                              ('certificate verify failed', 'tls_error'), ('Protocol https not on whitelist', 'protocol_denied'),
                              ('No space left on device', 'storage_unavailable')]:
            self.assertEqual(classify_diagnostic(message), code)

    def test_quality_disappearing_before_download_has_specific_code(self):
        core = types.SimpleNamespace(getVODQualities=lambda url: ([{'id':'new'}], {'inKey':'SYNTHETIC'}))
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as tmp:
            with self.assertRaises(ReplayFailure) as caught:
                run_download(core, {'url':'https://chzzk.naver.com/video/1','quality':'gone'}, tmp, lambda event: None)
        self.assertEqual(caught.exception.code, 'quality_changed')

    def test_direct_native_stderr_is_classified_and_progress_survives(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as tmp:
            tool = Path(tmp) / 'ffmpeg'
            tool.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''
                import os,sys
                fd=int(sys.argv[sys.argv.index('-progress')+1].split(':')[1])
                os.write(fd,b'out_time_us=1000000\\n')
                sys.stderr.write('HTTP error 403 Forbidden https://vod.pstatic.net/a?token=SYNTHETIC_SECRET Cookie: SYNTHETIC_COOKIE\\n')
                sys.exit(1)
            '''))
            tool.chmod(0o700)
            core = types.SimpleNamespace(subprocess=subprocess)
            diagnostics, events = {}, []
            with guarded_core(core, 'chzzk', events.append, duration=2, diagnostics=diagnostics):
                process = core.subprocess.Popen([str(tool), '-i', 'https://vod.pstatic.net/a?token=SYNTHETIC_SECRET', 'out.mp4'])
                self.assertEqual(process.wait(timeout=5), 1)
            self.assertEqual(diagnostics, {'code': 'http_403'})
            self.assertTrue(any(event.get('progress') == 50 for event in events))
            self.assertNotIn('SYNTHETIC', json.dumps(diagnostics) + json.dumps(events))

    def test_worker_emits_only_safe_http_error_code(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as tmp:
            app = Path(tmp)
            (app / 'module').mkdir()
            (app / 'module/__init__.py').write_text('')
            work = app / 'work'
            work.mkdir()
            tool = app / 'ffmpeg'
            tool.write_text('#!' + sys.executable + '\nimport sys\nsys.stderr.write("HTTP error 403 Forbidden https://vod.pstatic.net/a?token=SYNTHETIC_SECRET Cookie: SYNTHETIC_COOKIE\\n")\nsys.exit(1)\n')
            tool.chmod(0o700)
            (app / 'module/replay_download.py').write_text(textwrap.dedent('''
                import subprocess,types
                data_manager=types.SimpleNamespace()
                def getVODQualities(url):
                    return [{'id':'exact'}], {'inKey':'SYNTHETIC_KEY','title':'fixture'}
                def checkDuplicateFile(path): return True,''
                def downloadVOD(url, quality, folder, name, split, section):
                    from pathlib import Path
                    p=subprocess.Popen([str(Path(__file__).parents[1]/'ffmpeg'),'-i','https://vod.pstatic.net/a?token=SYNTHETIC_SECRET',str(Path(folder)/name)])
                    if p.wait(): raise Exception('native command failed')
            '''))
            result = subprocess.run([sys.executable, str(ROOT / 'docker_support/replay/worker.py'), '--operation', 'download', '--app-dir', str(app), '--work-dir', str(work)],
                                    input=json.dumps({'url':'https://chzzk.naver.com/video/1','quality':'exact'}), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, '')
            self.assertNotIn('SYNTHETIC', result.stdout)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[-1], {'event':'error','code':'http_403'})
            self.assertTrue(all(event.get('code', 'engine_operation_failed') in ERROR_MESSAGES for event in events))


if __name__ == '__main__':
    unittest.main()
