import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from docker_support.replay.engine import parse_url, metadata, run_download

URL = 'https://chzzk.naver.com/clips/2fkqV67jY2'
INFO = {'title': 'clip', 'duration': 10.0, 'formats': [
    {'id': 'MP4_720', 'width': 720, 'height': 1280, 'fps': '30',
     'url': 'https://vod.pstatic.net/clip.mp4?secret=signed'}]}


class ClipEngineTests(unittest.TestCase):
    def test_clip_url_and_public_metadata(self):
        self.assertTrue(parse_url(URL)['clip'])
        for url in (URL+'?other=1', URL+'#fragment', URL+'/../video/1', URL.replace('https:', 'http:')):
            with self.assertRaises(ValueError):
                parse_url(url)
        with patch('docker_support.replay.clips.resolve_clip', return_value=INFO):
            data = metadata(object(), URL)
        self.assertNotIn('signed', str(data))
        self.assertEqual(data['qualities'][0]['id'], 'MP4_720')
        self.assertEqual(data['supports'], {'section': False, 'split': False, 'speed': False})

    def test_clip_download_uses_exact_fresh_quality_and_probe(self):
        from docker_support.replay.manager import ReplayFailure
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'video.mp4'
            class Process:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def wait(self):
                    target.write_bytes(b'media')
                    return 0
            calls = []
            def spawn(args, **kwargs):
                calls.append(args)
                return Process()
            core = types.SimpleNamespace(subprocess=types.SimpleNamespace(Popen=spawn, DEVNULL=-3), getFFmpeg=lambda: 'ffmpeg')
            task = {'url': URL, 'quality': 'MP4_720', 'section': None, 'split': 1, 'speed_limit': ''}
            with patch('docker_support.replay.clips.resolve_clip', return_value=INFO) as resolve:
                result = run_download(core, task, tmp, lambda event: None,
                                      probe=lambda path: {'duration_seconds': 10, 'extension': '.mp4'})
                self.assertEqual(resolve.call_count, 2)
            self.assertEqual(result['size'], 5)
            self.assertIn(INFO['formats'][0]['url'], calls[0])
            self.assertNotIn('-headers', calls[0])
            target.unlink()
            with patch('docker_support.replay.clips.resolve_clip', side_effect=[INFO, dict(INFO, formats=[])]):
                with self.assertRaises(ReplayFailure):
                    run_download(core, task, tmp, lambda event: None)
