"""Synthetic YouTube regression: filesystem, actual offline selector, source CLI."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
from typing import Optional
import unittest
from unittest.mock import patch

import yt_dlp

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('YOUTUBE_TEST_SOURCE', str(ROOT)))
spec = importlib.util.spec_from_file_location('youtube_execution_patch', ROOT/'docker_support/replay/youtube_patch.py')
youtube_patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(youtube_patch)
DATA = (SOURCE/'upstream/module/data_manager.py').read_text()
REPLAY = (SOURCE/'upstream/module/replay_download.py').read_text()


def functions(text, names, namespace):
    tree = ast.parse(text)
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in tree.body} != names:
        raise AssertionError('missing fixture source function')
    exec(compile(tree, 'synthetic_youtube_source', 'exec'), namespace)
    return namespace


class YoutubeCookieTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.jar = Path(self.temporary.name)/'ycookie.txt'
        self.namespace = functions(youtube_patch.transform_data_manager(DATA), {'yloadCookies'},
            {'os':os, 'Optional':Optional, 'yCOOKIE_PATH':str(self.jar)})

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_existing_file_is_anonymous_and_unchanged(self):
        self.jar.touch()
        before=self.jar.stat()
        self.assertIsNone(self.namespace['yloadCookies']())
        after=self.jar.stat()
        self.assertEqual((after.st_ino,after.st_mtime_ns,after.st_size),
                         (before.st_ino,before.st_mtime_ns,0))
        self.assertEqual(self.jar.read_bytes(),b'')

    def test_missing_file_initialization_is_preserved(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(self.namespace['yloadCookies']())
        self.assertEqual(self.jar.read_text(),'# Netscape HTTP Cookie File\n')
        self.assertEqual(self.namespace['yloadCookies'](),str(self.jar))

    def test_nonempty_valid_and_invalid_content_remain_untouched_paths(self):
        for content in ('# Netscape HTTP Cookie File\n','synthetic invalid cookie bytes\n'):
            with self.subTest(content=content):
                self.jar.write_text(content)
                self.assertEqual(self.namespace['yloadCookies'](),str(self.jar))
                self.assertEqual(self.jar.read_text(),content)

    def test_stat_errors_are_not_hidden_as_anonymous(self):
        self.jar.touch()
        with patch.object(os.path,'getsize',side_effect=PermissionError('synthetic')):
            with self.assertRaises(PermissionError):self.namespace['yloadCookies']()

    def test_actual_common_arguments_omit_empty_cookie_and_keep_nonempty_jar(self):
        self.jar.touch()
        manager=types.SimpleNamespace(yloadCookies=self.namespace['yloadCookies'],loadConfig=lambda:{},
                                     toBool=bool,getDeno=lambda:None)
        ns=functions(REPLAY,{'loadYoutubeCookies','getCommonYtdlpAargs'},
                     {'os':os,'data_manager':manager,'base_dir':self.temporary.name})
        empty_args=ns['getCommonYtdlpAargs']()
        self.assertNotIn('--cookies',empty_args)
        self.assertIn('--extractor-args',empty_args)
        self.jar.write_text('synthetic invalid nonempty content\n')
        nonempty_args=ns['getCommonYtdlpAargs']()
        self.assertEqual(nonempty_args[nonempty_args.index('--cookies')+1],str(self.jar))


class YoutubeFormatTests(unittest.TestCase):
    def setUp(self):
        self.commands=[]
        self.registered=[]
        def popen(command):
            self.commands.append(command)
            return types.SimpleNamespace(wait=lambda:0)
        self.namespace=functions(youtube_patch.transform_replay(REPLAY),
            {'downloadYoutube','downloadYoutubePlaylist'}, {
                'os':os,'getFFmpeg':lambda:'/usr/bin/ffmpeg','getYtDlp':lambda:'yt-dlp',
                'sanitize_filename':lambda text:text,'checkDuplicateFile':lambda path:(True,'--continue'),
                'getCommonYtdlpAargs':lambda:['--no-colors','--extractor-args','youtube:player-client=default,mweb'],
                'subprocess':types.SimpleNamespace(Popen=popen),'_set_current_processes':self.registered.extend,
                '_clear_current_processes':self.registered.clear})

    def command(self, quality):
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.namespace['downloadYoutube']('https://www.youtube.com/watch?v=synthetic',quality,
                            directory,'Synthetic title.mp4','2M','00:00:01~00:00:03'))
        self.assertFalse(self.registered)
        return self.commands[-1]

    def select(self, quality, formats):
        command=self.command(quality)
        expression=command[command.index('-f')+1]
        with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True,'skip_download':True}) as ydl:
            return ydl._select_formats(formats,ydl.build_format_selector(expression))

    def video(self, ident, *, audio='none', height=1080):
        return {'format_id':ident,'url':'https://synthetic.invalid/'+ident,'ext':'mp4','protocol':'https',
                'vcodec':'avc1.640028','acodec':audio,'height':height,'width':1920,'tbr':1000}

    def audio(self):
        return {'format_id':'140','url':'https://synthetic.invalid/audio','ext':'m4a','protocol':'https',
                'vcodec':'none','acodec':'mp4a.40.2','abr':128,'tbr':128}

    def test_muxed_selected_format_works_without_audio_only_format(self):
        selected=self.select('95',[self.video('95',audio='mp4a.40.2',height=720)])
        self.assertEqual([f['format_id'] for f in selected],['95'])
        self.assertNotEqual(selected[0]['acodec'],'none')

    def test_video_only_selected_format_merges_bestaudio(self):
        selected=self.select('137',[self.audio(),self.video('137')])
        self.assertEqual([f['format_id'] for f in selected[0]['requested_formats']],['137','140'])

    def test_video_only_without_audio_has_no_silent_fallback(self):
        self.assertEqual(self.select('137',[self.video('137')]),[])

    def test_selected_quality_does_not_switch_to_higher_muxed_format(self):
        selected=self.select('95',[self.video('95',audio='mp4a.40.2',height=720),
                                   self.video('96',audio='mp4a.40.2',height=1080)])
        self.assertEqual([f['format_id'] for f in selected],['95'])

    def test_cli_keeps_sections_rate_resume_merge_location_and_existing_args(self):
        cmd=self.command('95')
        for option,value in [('-f','95[acodec!=none]/95+bestaudio'),('--merge-output-format','mkv'),
                             ('--ffmpeg-location','/usr/bin'),('--postprocessor-args','ffmpeg:-loglevel error'),
                             ('--download-sections','*00:00:01-00:00:03'),('--limit-rate','2M'),
                             ('--extractor-args','youtube:player-client=default,mweb')]:
            self.assertEqual(cmd[cmd.index(option)+1],value)
        self.assertEqual(cmd.count('--no-hls-use-mpegts'),1)
        self.assertIn('--continue',cmd)
        self.assertIn('--no-colors',cmd)
        self.assertTrue(cmd[cmd.index('-o')+1].endswith('Synthetic title'))

    def test_completed_live_hls_uses_mp4_output_without_altering_selected_format(self):
        cmd=self.command('95')
        self.assertEqual(cmd[cmd.index('-f')+1],'95[acodec!=none]/95+bestaudio')
        self.assertEqual(cmd.count('--no-hls-use-mpegts'),1)
        self.assertNotIn('--hls-use-mpegts',cmd)
        self.assertEqual(cmd[cmd.index('--merge-output-format')+1],'mkv')

    def test_playlist_retains_selected_format_for_each_entry(self):
        ns=self.namespace
        ns['getYoutubePlaylistInfo']=lambda url:('Synthetic playlist',2)
        ns['getPlaylistItems']=lambda url:['https://www.youtube.com/watch?v=one','https://www.youtube.com/watch?v=two']
        ns['getYoutubeQualities']=lambda *a,**kw:([{'id':'95'}],{'title':'Synthetic title','uploader':'Synthetic uploader'})
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            ns['downloadYoutubePlaylist']('https://www.youtube.com/playlist?list=synthetic',directory,'2M',selected_quality='95')
        self.assertEqual(len(self.commands),2)
        self.assertTrue(all(cmd[cmd.index('-f')+1]=='95[acodec!=none]/95+bestaudio' for cmd in self.commands))
        self.assertTrue(all(cmd[cmd.index('--limit-rate')+1]=='2M' for cmd in self.commands))


class YoutubeTransformTests(unittest.TestCase):
    def test_transforms_reject_unknown_or_already_patched_source(self):
        for transform,text in [(youtube_patch.transform_data_manager,DATA),(youtube_patch.transform_replay,REPLAY)]:
            with self.assertRaises(ValueError):transform('pass\n')
            with self.assertRaises(ValueError):transform(transform(text))

    def test_execution_manifest_contains_both_fixes_and_preserves_upstream(self):
        spec=importlib.util.spec_from_file_location('youtube_fixture_compose',SOURCE/'docker_support/compose_source.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (SOURCE/'upstream').rglob('*') if p.is_file()}
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'execution'
            receipt=module.compose(SOURCE/'upstream',out,SOURCE/'patches/upstream.json',SOURCE/'patches/public.json')
            self.assertIn('os.path.getsize(yCOOKIE_PATH) == 0',(out/'module/data_manager.py').read_text())
            self.assertIn("f'{format_id}[acodec!=none]/{format_id}+bestaudio'",(out/'module/replay_download.py').read_text())
            for name in ('module/data_manager.py','module/replay_download.py'):
                self.assertEqual(receipt['files'][name],hashlib.sha256((out/name).read_bytes()).hexdigest())
        self.assertEqual(before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (SOURCE/'upstream').rglob('*') if p.is_file()})


if __name__=='__main__':unittest.main()
