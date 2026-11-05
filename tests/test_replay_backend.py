"""Offline behavioral checks. All tools/core/provider responses are fixtures."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from docker_support.replay.engine import analyze_urls, clean_text, guarded_core, metadata, parse_url, provider_url, run_download, validate_options
from docker_support.replay.manager import ReplayError, ReplayManager

FAKE_WORKER = r'''
import argparse,json,os,subprocess,sys,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--operation');p.add_argument('--app-dir');p.add_argument('--work-dir')
a=p.parse_args(); task=json.load(sys.stdin)
def emit(x): print(json.dumps(x),flush=True)
if a.operation=='analyze':
    if task['urls'][0].endswith('/8'): time.sleep(30)
    items=[]
    for url in task['urls']:
        items.append(dict(url=url,platform='chzzk',video_id=url.rsplit('/',1)[1],title='fixture',duration_seconds=100,
           qualities=[dict(id='1080p',label='1080p')],supports=dict(section=True,split=True,speed=False),error=None))
    emit(dict(event='result',data=dict(items=items)))
else:
    print('RAW_SECRET_COOKIE_SIGNED_URL',file=sys.stderr,flush=True)
    url=task['url']; w=Path(a.work_dir)
    if url.endswith('/2') or url.endswith('/7'):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)'])
        Path(os.environ['REPLAY_TEST_CHILD']).write_text(str(child.pid))
        emit(dict(event='stage',stage='downloading'))
        time.sleep(300)
    if url.endswith('/6'):
        print('RAW_SECRET_COOKIE_SIGNED_URL',flush=True)
        sys.exit(1)
    if url.endswith('/5'):
        emit(dict(event='result',data=dict(name='../outside')))
        sys.exit(0)
    target=w/'video'
    if url.endswith('/4'):
        target.symlink_to(os.environ['REPLAY_TEST_PROTECTED'])
    else:
        target.write_bytes(b'INVALID' if url.endswith('/3') else b'VALID-FAKE-MEDIA' * 100)
    emit(dict(event='progress',stage='downloading',progress=25,downloaded_bytes=99999999,total_bytes=99999999))
    emit(dict(event='result',data=dict(name='video',extension='.mp4',duration_seconds=10)))
'''

FAKE_PROBE = '#!' + sys.executable + '\n' + r'''import json,sys
from pathlib import Path
p=Path(sys.argv[-1])
valid=p.read_bytes().startswith(b'VALID-FAKE-MEDIA')
print(json.dumps(dict(format=dict(duration='10' if valid else '0',format_name='matroska,webm'),streams=[dict(codec_type='video' if valid else 'audio')])) )
'''


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.base = Path(self.temp.name)
        self.worker = self.base / 'fake_worker.py'
        self.worker.write_text(FAKE_WORKER)
        bin_dir = self.base / 'bin'
        bin_dir.mkdir()
        probe = bin_dir / 'ffprobe'
        probe.write_text(FAKE_PROBE)
        probe.chmod(0o700)
        self.protected = self.base / 'protected'
        self.protected.write_text('KEEP')
        self.env = patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                                          'REPLAY_TEST_CHILD': str(self.base / 'child.pid'),
                                          'REPLAY_TEST_PROTECTED': str(self.protected)})
        self.env.start()
        self.managers = []
        self.manager = self.make_manager()
        await self.manager.start()

    def make_manager(self, suffix='', **kwargs):
        manager = ReplayManager(self.base / ('state' + suffix), self.base / ('downloads' + suffix), self.base,
                                min_free_bytes=0, worker_command=[sys.executable, str(self.worker)], **kwargs)
        self.managers.append(manager)
        return manager

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()
        self.env.stop()
        self.temp.cleanup()

    async def wait_state(self, manager, kind, ident, states=('completed', 'failed', 'cancelled', 'interrupted')):
        for _ in range(300):
            item = manager.get_analysis('alice', ident) if kind == 'analysis' else manager.get_job('alice', ident)
            if item['status'] in states:
                return item
            await asyncio.sleep(.02)
        self.fail('queue did not reach expected state')

    async def enqueue(self, video=1, manager=None, key='request-key-123'):
        manager = manager or self.manager
        analysis = await manager.analyze('alice', ['https://chzzk.naver.com/video/' + str(video)])
        self.assertEqual(analysis['status'], 'queued')
        analysis = await self.wait_state(manager, 'analysis', analysis['id'])
        self.assertEqual(analysis['status'], 'completed')
        items = [{'url': analysis['items'][0]['url'], 'quality': '1080p'}]
        job = (await manager.create_jobs('alice', analysis['id'], items, key))['jobs'][0]
        return job, analysis, items

    async def test_lifecycle_owner_idempotency_cursor_and_no_secret(self):
        job, analysis, items = await self.enqueue()
        duplicate = await self.manager.create_jobs('alice', analysis['id'], items, 'request-key-123')
        self.assertEqual(duplicate['jobs'][0]['id'], job['id'])
        with self.assertRaises(ReplayError) as conflict:
            await self.manager.create_jobs('alice', analysis['id'], items + items, 'request-key-123')
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaises(ReplayError) as isolated:
            self.manager.get_job('bob', job['id'])
        self.assertEqual(isolated.exception.status, 404)
        with self.assertRaises(ReplayError):
            self.manager.get_analysis('bob', analysis['id'])
        self.assertEqual(self.manager.list_jobs('bob'), {'jobs': []})
        completed = await self.wait_state(self.manager, 'job', job['id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertTrue(completed['can_download'])
        self.assertEqual(completed['total_bytes'], None)  # fabricated event totals ignored
        self.assertTrue(self.manager.resolve_file('alice', job['id']).read_bytes().startswith(b'VALID'))
        logs = self.manager.logs('alice', job['id'], 0)
        self.assertTrue(logs['logs'])
        self.assertEqual(self.manager.logs('alice', job['id'], logs['next_cursor'])['logs'], [])
        db_dump = '\n'.join(self.manager.db.iterdump())
        self.assertNotIn('RAW_SECRET', db_dump)
        self.assertEqual(list((self.manager.work_root).iterdir()), [])
        # Completed output survives a retry attempt and shutdown.
        with self.assertRaises(ReplayError):
            await self.manager.retry('alice', job['id'])
        path = self.manager.resolve_file('alice', job['id'])
        await self.manager.close()
        self.assertTrue(path.is_file())

    async def test_invalid_media_symlink_and_path_results_never_publish(self):
        for video in (3, 4, 5, 6):
            job, _, _ = await self.enqueue(video, key='request-key-' + str(video))
            failed = await self.wait_state(self.manager, 'job', job['id'])
            self.assertEqual(failed['status'], 'failed', video)
            self.assertFalse(failed['can_download'])
            with self.assertRaises(ReplayError):
                self.manager.resolve_file('alice', job['id'])
        self.assertEqual(self.protected.read_text(), 'KEEP')
        self.assertEqual(list(self.manager.completed_root.iterdir()), [])
        self.assertNotIn('RAW_SECRET', '\n'.join(self.manager.db.iterdump()))

    async def test_cancel_active_kills_descendant_and_retry_reuses_job(self):
        job, _, _ = await self.enqueue(2)
        child_path = self.base / 'child.pid'
        for _ in range(200):
            if child_path.exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue(child_path.exists())
        child = int(child_path.read_text())
        cancelled = await self.manager.cancel('alice', job['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        for _ in range(100):
            proc_status = Path('/proc') / str(child) / 'stat'
            if not proc_status.exists() or proc_status.read_text().split()[2] == 'Z':
                break
            await asyncio.sleep(.01)
        else:
            self.fail('descendant remained alive after group cancellation')
        retry = (await self.manager.retry('alice', job['id']))['jobs'][0]
        self.assertEqual(retry['id'], job['id'])
        self.assertEqual(retry['created_at'], job['created_at'])
        self.assertEqual(retry['attempt'], 2)
        self.assertEqual(len(self.manager.list_jobs('alice')['jobs']), 1)
        with self.assertRaises(ReplayError) as duplicate:
            await self.manager.retry('alice', job['id'])
        self.assertEqual(duplicate.exception.status, 409)
        self.assertEqual(retry['status'], 'queued')
        self.assertEqual((await self.manager.cancel('alice', retry['id']))['status'], 'cancelled')

    async def test_failed_legacy_retry_preserves_log_then_completes(self):
        job, _, _ = await self.enqueue(3)
        failed = await self.wait_state(self.manager, 'job', job['id'])
        self.assertEqual(failed['status'], 'failed')
        logs = self.manager.logs('alice', job['id'])
        self.assertIn(failed['error'], [entry['message'] for entry in logs['logs']])
        legacy = self.manager._get('job', 'alice', job['id'])
        orphan = self.manager.completed_root / (job['id'] + '.mkv')
        orphan.write_bytes(b'previous publication')
        del legacy['attempt']
        self.manager._save('job', 'alice', legacy)
        self.worker.write_text(FAKE_WORKER.replace("url.endswith('/3')", 'False'))
        retry = (await self.manager.retry('alice', job['id']))['jobs'][0]
        self.assertEqual((retry['attempt'], retry['error_code'], retry['downloaded_bytes']), (2, None, 0))
        complete = await self.wait_state(self.manager, 'job', job['id'])
        self.assertEqual(complete['status'], 'completed')
        self.assertTrue(self.manager.resolve_file('alice', job['id']).is_file())
        self.assertEqual(orphan.read_bytes(), b'previous publication')
        self.assertNotEqual(self.manager.resolve_file('alice', job['id']), orphan)
        self.assertEqual(self.manager.logs('alice', job['id'])['logs'][:len(logs['logs'])], logs['logs'])

    async def test_completed_retention_boundary_and_files(self):
        from datetime import datetime, timezone, timedelta
        job, _, _ = await self.enqueue()
        completed = await self.wait_state(self.manager, 'job', job['id'])
        path = self.manager.resolve_file('alice', job['id'])
        moment = datetime.fromisoformat(completed['completed_at'])
        self.manager.prune_completed(moment + timedelta(hours=1, microseconds=-1))
        self.assertEqual(self.manager.get_job('alice', job['id'])['status'], 'completed')
        self.manager.prune_completed(moment + timedelta(hours=1))
        self.assertTrue(path.is_file())
        self.assertEqual(self.manager.list_jobs('alice')['jobs'], [])
        self.assertEqual(self.manager.db.execute('SELECT count(*) FROM logs WHERE job=?', (job['id'],)).fetchone()[0], 0)
        self.assertEqual(self.manager.db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    async def test_retention_keeps_mixed_request_siblings_and_legacy_clock(self):
        from datetime import datetime, timezone, timedelta
        moment = datetime.now(timezone.utc)
        old = self.manager._new_job({'url': 'fixture'})
        old.update(status='completed', updated_at=(moment-timedelta(hours=2)).isoformat())
        self.manager._save('job', 'alice', old)
        retained = []
        for status in ('queued', 'running', 'finalizing', 'cancelling', 'failed', 'cancelled', 'interrupted'):
            job = self.manager._new_job({'url': 'fixture'})
            job.update(status=status, updated_at=old['updated_at'])
            self.manager._save('job', 'alice', job)
            retained.append(job['id'])
        with self.manager.db:
            self.manager.db.execute('INSERT INTO requests VALUES(?,?,?,?)', ('alice', 'mixed-key', '{}', json.dumps([old['id']]+retained)))
        self.manager.prune_completed(moment)
        self.assertEqual({j['id'] for j in self.manager.list_jobs('alice')['jobs']}, set(retained))
        ids = self.manager.db.execute('SELECT ids FROM requests WHERE key=?', ('mixed-key',)).fetchone()[0]
        self.assertEqual(json.loads(ids), retained)
        # Avoid handing these synthetic non-downloadable records to a worker.
        with self.manager.db:
            self.manager.db.execute("DELETE FROM records WHERE kind='job'")

    async def test_idle_retention_runs_and_shutdown_wakes_sweeper(self):
        from datetime import datetime, timezone, timedelta
        old = self.manager._new_job({'url': 'fixture'})
        old.update(status='completed', completed_at=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
        self.manager._save('job', 'alice', old)
        self.manager.wake['retention'].set()
        for _ in range(100):
            if not self.manager.list_jobs('alice')['jobs']:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.manager.list_jobs('alice')['jobs'], [])
        await asyncio.wait_for(self.manager.close(), 1)
        self.assertTrue(all(task.done() for task in self.manager.tasks))

    async def test_cancel_while_spawn_is_suspended(self):
        original = asyncio.create_subprocess_exec
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            if '--operation' in args and args[args.index('--operation') + 1] == 'download':
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        with patch('asyncio.create_subprocess_exec', delayed):
            job, _, _ = await self.enqueue()
            await asyncio.wait_for(entered.wait(), 2)
            cancelling = asyncio.create_task(self.manager.cancel('alice', job['id']))
            await asyncio.sleep(.02)
            self.assertEqual(self.manager.get_job('alice', job['id'])['status'], 'cancelling')
            release.set()
            result = await cancelling
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(list(self.manager.completed_root.iterdir()), [])

    async def test_cancel_while_validation_probe_spawn_is_suspended(self):
        original = asyncio.create_subprocess_exec
        entered, release = asyncio.Event(), asyncio.Event()
        probes = []

        async def delayed(*args, **kwargs):
            if Path(args[0]).name == 'ffprobe':
                entered.set()
                await release.wait()
                process = await original(*args, **kwargs)
                probes.append(process)
                return process
            return await original(*args, **kwargs)

        with patch('asyncio.create_subprocess_exec', delayed):
            job, _, _ = await self.enqueue()
            await asyncio.wait_for(entered.wait(), 2)
            cancelling = asyncio.create_task(self.manager.cancel('alice', job['id']))
            await asyncio.sleep(.02)
            self.assertEqual(self.manager.get_job('alice', job['id'])['status'], 'cancelling')
            release.set()
            result = await cancelling
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(probes)
        self.assertIsNotNone(probes[0].returncode)
        self.assertEqual(list(self.manager.completed_root.iterdir()), [])

    async def test_concurrent_close_shares_cleanup_and_releases_lock(self):
        job, _, _ = await self.enqueue(7)
        await asyncio.gather(self.manager.close(), self.manager.close())
        replacement = self.make_manager()
        self.assertEqual(replacement.get_job('alice', job['id'])['status'], 'interrupted')

    async def test_crash_recovery_cleans_only_known_interrupted_partial(self):
        crashed = self.make_manager('crashed')
        analysis = await crashed.analyze('alice', ['https://chzzk.naver.com/video/1'])
        partial = crashed.work_root / analysis['id']
        partial.mkdir()
        (partial / 'video.part').write_bytes(b'partial')
        unrelated = crashed.work_root / 'operator-notes'
        unrelated.mkdir()
        (unrelated / 'keep').write_text('KEEP')
        crashed.store.close()  # Simulate process loss before graceful close.
        self.managers.remove(crashed)
        replacement = self.make_manager('crashed')
        self.assertEqual(replacement.get_analysis('alice', analysis['id'])['status'], 'interrupted')
        self.assertFalse(partial.exists())
        self.assertEqual((unrelated / 'keep').read_text(), 'KEEP')

    async def test_slots_analysis_timeout_shutdown_and_restart_recovery(self):
        slow = self.make_manager('slow', analysis_timeout=.1, job_timeout=.2)
        await slow.start()
        analysis = await slow.analyze('alice', ['https://chzzk.naver.com/video/8'])
        self.assertEqual((await self.wait_state(slow, 'analysis', analysis['id']))['status'], 'failed')
        job, _, _ = await self.enqueue(7)
        # An active download must not block the separate metadata slot.
        second = await self.manager.analyze('alice', ['https://chzzk.naver.com/video/1'])
        self.assertEqual((await self.wait_state(self.manager, 'analysis', second['id']))['status'], 'completed')
        queued, _, _ = await self.enqueue(1, key='request-key-second')
        self.assertEqual(self.manager.get_job('alice', queued['id'])['status'], 'queued')
        await asyncio.wait_for(self.manager.close(), 5)
        replacement = self.make_manager()
        self.assertEqual(replacement.get_job('alice', job['id'])['status'], 'interrupted')
        self.assertEqual(replacement.get_job('alice', queued['id'])['status'], 'interrupted')
        await replacement.start()
        await asyncio.sleep(.05)
        self.assertEqual(replacement.get_job('alice', queued['id'])['status'], 'interrupted')

    async def test_lock_disk_pressure_and_symlink_root(self):
        with self.assertRaises(BlockingIOError):
            self.make_manager()
        self.manager.min_free_bytes = 10 ** 30
        job, _, _ = await self.enqueue()
        self.assertEqual((await self.wait_state(self.manager, 'job', job['id']))['status'], 'failed')
        link = self.base / 'alias'
        link.symlink_to(self.base / 'downloads', target_is_directory=True)
        with self.assertRaises(ValueError):
            ReplayManager(self.base / 'other-state', link / 'child', self.base)


class EngineTests(unittest.TestCase):
    def test_metadata_redacts_signed_urls_case_insensitively(self):
        self.assertEqual(clean_text('title HTTPS://ci.me/a?signature=secret end'), 'title [주소] end')

    def test_url_boundary(self):
        for url in ('http://chzzk.naver.com/video/1', 'https://user@chzzk.naver.com/video/1',
                    'https://chzzk.naver.com.evil.test/video/1', 'https://chzzk.naver.com:444/video/1',
                    'https://chzzk.naver.com/video/1#fragment', 'https://ci.me/@a/vods/1?token=secret',
                    'https://youtube.com/watch?v=12345678901&v=ABCDEFGHIJK', 'https://youtu.be/12345678901\\@evil.test'):
            with self.assertRaises(ValueError, msg=url):
                parse_url(url)
        self.assertEqual(parse_url('https://youtu.be/12345678901?t=1')['url'], 'https://www.youtube.com/watch?v=12345678901')
        self.assertFalse(parse_url('https://youtube.com/watch?v=12345678901&list=foo')['playlist'])
        self.assertTrue(parse_url('https://youtube.com/playlist?list=PL_123')['playlist'])
        for url in ('https://127.0.0.1/a', 'https://evil.test/a', 'file:///etc/passwd', 'https://ci.me.evil.test/a', 'https://user:pass@ci.me/a'):
            with self.assertRaises(ValueError):
                provider_url(url, 'cime')
        provider_url('https://streaming.cf.ci.me/a?signature=sensitive', 'cime')

    def core(self, in_key=None):
        qualities = [{'id': '1080p', 'quality': '1080p', 'height': 1080, 'downloadType': 'streamlink_hls'}]
        info = {'inKey': in_key, 'title': 'fixture', 'duration': 100, 'master_url': 'https://signed.test/token=secret'}
        return types.SimpleNamespace(getVODQualities=lambda url: (qualities, info), getCimeVODQualities=lambda url: (qualities, info),
                                     getYoutubeQualities=lambda url: ([('1080p', '1080p')], info), checkDuplicateFile=lambda path: (True, ''))

    def test_playlist_expansion_per_video_quality_and_partial_errors(self):
        core = self.core()
        core.getPlaylistItems = lambda url: [f'{i:011d}' for i in range(80)]

        def qualities(url):
            if url.endswith('00000000002'):
                raise RuntimeError('RAW_SECRET')
            return [('quality ' + url[-1], 'fmt' + url[-1])], {'title': url[-1]}

        core.getYoutubeQualities = qualities
        items = analyze_urls(core, ['https://youtube.com/playlist?list=PL123'])
        self.assertEqual(len(items), 50)
        self.assertEqual(items[1]['qualities'][0]['id'], 'fmt1')
        self.assertTrue(items[2]['error'])
        self.assertNotIn('RAW_SECRET', json.dumps(items))
        self.assertIsNone(items[1]['duration_seconds'])

    def test_exact_hls_branch_and_options(self):
        hls = metadata(self.core(None), 'https://chzzk.naver.com/video/1')
        empty_key_dash = metadata(self.core(''), 'https://chzzk.naver.com/video/1')
        self.assertFalse(hls['supports']['split'])
        self.assertTrue(empty_key_dash['supports']['split'])
        self.assertNotIn('master_url', hls)
        self.assertEqual(validate_options({'quality': '1080p'}, hls)['split'], 1)
        for item in ({'quality': 'missing'}, {'quality': '1080p', 'split': 4}, {'quality': '1080p', 'section': '00:00:01-00:00:02'},
                     {'quality': '1080p', 'speed_limit': '5M'}, {'quality': '1080p', 'section': 123}):
            with self.assertRaises((TypeError, ValueError)):
                validate_options(item, hls)
        validate_options({'quality': '1080p', 'split': 4, 'section': '00:00:01-00:00:02'}, empty_key_dash)
        with self.assertRaises(ValueError):
            validate_options({'quality': '1080p', 'section': '00:00:01-00:01:41'}, empty_key_dash)

    def test_original_platform_calls_and_extensionless_media_gate(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            work = Path(temp)
            for platform, url in [('youtube', 'https://youtu.be/12345678901'), ('cime', 'https://ci.me/@channel/vods/123'), ('chzzk', 'https://chzzk.naver.com/video/123')]:
                core = self.core('DASH_KEY')
                calls = []

                def download(*args):
                    calls.append(args)
                    self.assertEqual(args[1], '1080p')
                    Path(args[2], 'video').write_bytes(b'VALID')
                    return True

                core.downloadYoutube = core.downloadCimeVOD = core.downloadVOD = download
                events = []
                result = run_download(core, {'url': url, 'quality': '1080p'}, work, events.append,
                                      probe=lambda path: {'extension': '.mp4', 'duration_seconds': 10})
                self.assertEqual(result['name'], 'video')
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][3], 'video' if platform == 'youtube' else 'video.mp4')
                if platform == 'chzzk':
                    self.assertEqual(calls[0][4], '분할 없음')
                (work / 'video').unlink()
            with self.assertRaises(ValueError):
                run_download(core, {'url': url, 'quality': 'missing'}, work, events.append)

    def test_refresh_disallows_core_quality_fallback(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            core = self.core('DASH')
            count = 0

            def changing(url):
                nonlocal count
                count += 1
                return ([{'id': '1080p' if count == 1 else '720p'}], {'inKey': 'x'})

            core.getVODQualities = changing

            def original_download(*args):
                core.getVODQualities(args[0])  # original second lookup
                self.fail('would have silently fallen back')

            core.downloadVOD = original_download
            with self.assertRaises(ValueError):
                run_download(core, {'url': 'https://chzzk.naver.com/video/1', 'quality': '1080p'}, temp, lambda event: None)

    def test_worker_suppresses_import_python_native_and_child_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            app = Path(temp)
            (app / 'module').mkdir()
            (app / 'module' / '__init__.py').write_text('')
            (app / 'module' / 'replay_download.py').write_text(textwrap.dedent('''
                import os,subprocess,sys,types
                print('RAW_SECRET_IMPORT')
                os.write(1,b'RAW_SECRET_NATIVE\\n')
                os.write(2,b'RAW_SECRET_STDERR\\n')
                subprocess.run([sys.executable,'-c',"print('RAW_SECRET_CHILD')"])
                data_manager=types.SimpleNamespace()
                def getVODQualities(url):
                    print('RAW_SECRET_CORE')
                    return [{'id':'1080p','label':'1080p'}],{'title':'safe title','inKey':None}
            '''))
            result = subprocess.run([sys.executable, str(ROOT / 'docker_support/replay/worker.py'), '--operation', 'analyze',
                                     '--app-dir', str(app), '--work-dir', str(app)], input=json.dumps({'urls': ['https://chzzk.naver.com/video/1']}),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn('RAW_SECRET', result.stdout + result.stderr)
            self.assertEqual(result.stderr, '')
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[-1]['data']['items'][0]['title'], 'safe title')


if __name__ == '__main__':
    unittest.main()
