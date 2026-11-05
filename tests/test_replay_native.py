"""Offline native-process checks; optional real tools use synthetic media only."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from docker_support.replay.engine import guarded_core, probe_file, run_download

ROOT = Path(__file__).resolve().parents[1]


class NativePipeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('REPLAY_TEST_REAL_FFMPEG'), 'optional real FFmpeg toolchain')
    def test_extensionless_matroska_is_not_mislabeled_webm(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary:
            root = Path(temporary)
            source = root / 'fixture.mkv'
            subprocess.run([os.environ['REPLAY_TEST_REAL_FFMPEG'], '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=blue:s=64x64:r=10', '-t', '0.2', '-c:v', 'libx264', '-threads', '1', str(source)],
                           capture_output=True, check=True, timeout=15)
            extensionless = root / 'video'
            extensionless.write_bytes(source.read_bytes())
            self.assertEqual(probe_file(extensionless)['extension'], '.mkv')
            self.assertEqual(probe_file(source)['extension'], '.mkv')

    def pipeline(self, real=False):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temporary:
            base = Path(temporary)
            work = base / 'work'
            work.mkdir()
            fixture = base / 'fixture.ts'
            if real:
                ffmpeg = Path(os.environ['REPLAY_TEST_REAL_FFMPEG'])
                subprocess.run([str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=10',
                                '-t', '2', '-c:v', 'mpeg2video', '-threads', '1', '-f', 'mpegts', str(fixture)],
                               check=True, capture_output=True, timeout=15)
                upstream = Path(os.environ['REPLAY_TEST_UPSTREAM'])
                spec = importlib.util.spec_from_file_location('replay_native_fixture_core', upstream)
                core = importlib.util.module_from_spec(spec)
                package = types.ModuleType('module')
                package.data_manager = types.SimpleNamespace()
                with patch.dict(sys.modules, {'module': package}), patch.object(sys, 'dont_write_bytecode', True):
                    spec.loader.exec_module(core)
            else:
                fixture.write_bytes(b'BINARY-MEDIA\x00\xff\xfe' * 200)
                ffmpeg = base / 'ffmpeg'
                ffmpeg.write_text('#!' + sys.executable + '\n' + '''
import os,sys
from pathlib import Path
args=sys.argv[1:]
progress=args[args.index('-progress')+1]
fd=int(progress.split(':')[1])
assert fd not in (0,1,2)
assert args[args.index('-i')+1]=='pipe:0'
assert 'pipe' in args[args.index('-protocol_whitelist')+1].split(',')
assert sys.stdin.buffer.read()==Path(os.environ['REPLAY_TEST_MEDIA']).read_bytes()
Path(args[-1]).write_bytes(b'VALID-MEDIA'*200)
os.write(fd,b'out_time_us=1000000\\nprogress=end\\n')
''')
                ffmpeg.chmod(0o700)
                core = types.SimpleNamespace(subprocess=subprocess)

                def original_pipeline(url, quality, folder, name, split, section):
                    # Same stdio plumbing as upstream's HLS downloadVOD branch.
                    stream = core.subprocess.Popen([str(streamlink), url, quality, '--stdout'],
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                    mux = core.subprocess.Popen([str(ffmpeg), '-i', 'pipe:0', '-c', 'copy', str(Path(folder) / name)],
                                                stdin=stream.stdout, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                    stream.stdout.close()
                    self.assertEqual(mux.wait(timeout=10), 0)
                    self.assertEqual(stream.wait(timeout=10), 0)
                    stream.stderr.close()
                    mux.stderr.close()
                    return True

                core.downloadVOD = original_pipeline
            streamlink = base / 'streamlink'
            streamlink.write_text('#!' + sys.executable + '\n' +
                                  "import os,sys\nfrom pathlib import Path\nsys.stdout.buffer.write(Path(os.environ['REPLAY_TEST_MEDIA']).read_bytes())\n")
            streamlink.chmod(0o700)
            core.getVODQualities = lambda url: ([{'id': '1080p', 'label': '1080p'}], {'inKey': None, 'duration': 2})
            core.checkDuplicateFile = lambda path: (True, '')
            core.getVODUrl = lambda url: 'https://chzzk.naver.com/video/1'
            core.getFFmpeg = lambda: str(ffmpeg)
            core.getStreamlink = lambda: str(streamlink)
            core.getCookieHeaders = lambda: {}
            response = types.SimpleNamespace(raise_for_status=lambda: None,
                                             json=lambda: {'code': 200, 'content': {'inKey': None}})
            core.requests = types.SimpleNamespace(get=lambda *args, **kwargs: response)
            events = []
            with patch.dict(os.environ, {'REPLAY_TEST_MEDIA': str(fixture)}), contextlib.redirect_stdout(io.StringIO()):
                with guarded_core(core, 'chzzk', events.append):
                    result = run_download(core, {'url': 'https://chzzk.naver.com/video/1', 'quality': '1080p'},
                                          work, events.append,
                                          probe=probe_file if real else lambda path: {'extension': '.mp4', 'duration_seconds': 2})
            progress = [event['progress'] for event in events if event['event'] == 'progress']
            self.assertTrue(progress, events)
            self.assertTrue(any(value is not None and 0 < value < 100 for value in progress), progress)
            self.assertGreater((work / result['name']).stat().st_size, 1024)

    def test_dedicated_progress_fd_does_not_decode_or_replace_streamlink_media_pipe(self):
        self.pipeline()

    @unittest.skipUnless(os.environ.get('REPLAY_TEST_REAL_FFMPEG') and os.environ.get('REPLAY_TEST_UPSTREAM'),
                         'optional synthetic-media check needs isolated FFmpeg and upstream source paths')
    def test_actual_upstream_streamlink_pipeline_with_real_ffmpeg_and_probe(self):
        self.pipeline(real=True)


if __name__ == '__main__':
    unittest.main()
