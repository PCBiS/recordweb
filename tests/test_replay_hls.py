"""Offline regression for unsigned segment BaseURL versus signed HLS manifest."""
import ast
import os
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from docker_support.replay.hls_patch import transform

ROOT = Path(__file__).resolve().parents[1]


def collector():
    upstream = Path(os.environ.get('REPLAY_TEST_UPSTREAM', str(ROOT / 'upstream/module/replay_download.py')))
    source = upstream.read_text()
    wanted = {'_to_int', '_nvodAttr', '_findNvodLabel', 'collectChzzkMpdQualities'}
    tree = ast.parse(transform(source))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {'ET': ET, 'NVOD_NS': 'urn:naver:vod:2020', 'MPD_NS': 'urn:mpeg:dash:schema:mpd:2011'}
    exec(compile(tree, str(upstream), 'exec'), namespace)
    return namespace['collectChzzkMpdQualities']


class HlsManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collect = staticmethod(collector())

    def quality(self, mime='video/mp2t', base='https://vod.pstatic.net/segments/', rep='https://vod.pstatic.net/rep.m3u8?token=SYNTHETIC_REP', adaptation='https://vod.pstatic.net/adapt.m3u8?token=SYNTHETIC_ADAPT'):
        rep_attr = ' nvod:m3u="' + rep.replace('&', '&amp;') + '"' if rep else ''
        adapt_attr = ' nvod:m3u="' + adaptation.replace('&', '&amp;') + '"' if adaptation else ''
        xml = f'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020"><Period><AdaptationSet mimeType="{mime}"{adapt_attr}><Representation id="exact-id" height="1080"{rep_attr}><BaseURL>{base.replace('&', '&amp;')}</BaseURL></Representation></AdaptationSet></Period></MPD>'''
        return self.collect(xml)

    def test_representation_signed_manifest_wins_over_unsigned_directory(self):
        q = self.quality()[0]
        self.assertEqual(q['baseurl'], 'https://vod.pstatic.net/rep.m3u8?token=SYNTHETIC_REP')
        self.assertEqual(q['id'], 'exact-id')
        self.assertEqual(q['downloadType'], 'hls_mpd')

    def test_adaptation_manifest_is_used_when_representation_has_none(self):
        self.assertEqual(self.quality(rep='')[0]['baseurl'], 'https://vod.pstatic.net/adapt.m3u8?token=SYNTHETIC_ADAPT')

    def test_progressive_dash_keeps_direct_baseurl(self):
        q = self.quality(mime='video/mp4', base='https://vod.pstatic.net/PD1080.mp4')[0]
        self.assertEqual(q['baseurl'], 'https://vod.pstatic.net/PD1080.mp4')
        self.assertEqual(q['downloadType'], 'dash_file')

    def test_relative_manifest_resolves_against_absolute_base(self):
        self.assertEqual(self.quality(rep='../signed.m3u8?token=SYNTHETIC')[0]['baseurl'], 'https://vod.pstatic.net/signed.m3u8?token=SYNTHETIC')
        self.assertEqual(self.quality(rep='/signed.m3u8?token=SYNTHETIC')[0]['baseurl'], 'https://vod.pstatic.net/signed.m3u8?token=SYNTHETIC')

    def test_both_relative_paths_do_not_invent_origin(self):
        self.assertEqual(self.quality(base='segments/', rep='signed.m3u8?token=SYNTHETIC')[0]['baseurl'], 'segments/signed.m3u8?token=SYNTHETIC')

    def test_missing_manifest_does_not_select_segment_directory(self):
        self.assertEqual(self.quality(rep='', adaptation=''), [])
        self.assertEqual(self.quality(base='https://vod.pstatic.net/direct.m3u8', rep='', adaptation='')[0]['baseurl'], 'https://vod.pstatic.net/direct.m3u8')
        self.assertEqual(self.quality(mime='video/mp4', base='https://vod.pstatic.net/video.mp4', rep='', adaptation='')[0]['downloadType'], 'dash_file')

    def test_transform_rejects_unknown_or_already_patched_source(self):
        with self.assertRaises(ValueError):
            transform('unknown')


if __name__ == '__main__':
    unittest.main()
