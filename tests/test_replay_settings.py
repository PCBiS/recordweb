"""Offline policy, transaction and completed-file lifecycle checks."""
import asyncio
from pathlib import Path
import tempfile
import types
import unittest
import sys
from unittest.mock import patch

from docker_support.replay.manager import ReplayError, ReplayManager
from docker_support.replay.settings import GIB, ReplaySettingsController, resolve_settings


class SettingsPolicyTests(unittest.TestCase):
    def test_defaults_legacy_environment_and_json_precedence(self):
        self.assertEqual(resolve_settings({}, {}), {
            'replayEnabled': True, 'replayDownloadRoot': '/recordings/replay', 'replayMinFreeGiB': 5.0})
        env = {'RECORDWEB_REPLAY_ENABLED': '1', 'RECORDWEB_REPLAY_DOWNLOAD_ROOT': '/recordings/from-env',
               'RECORDWEB_REPLAY_MIN_FREE_BYTES': str(9 * GIB)}
        explicit = {'replayEnabled': False, 'replayDownloadRoot': '/recordings/saved', 'replayMinFreeGiB': .001}
        self.assertEqual(resolve_settings(explicit, env), explicit)
        env['RECORDWEB_REPLAY_MIN_FREE_BYTES'] = '0'
        self.assertEqual(resolve_settings({}, env)['replayMinFreeGiB'], .001)
        env['RECORDWEB_REPLAY_DOWNLOAD_ROOT'] = '/tmp/operator-root'
        self.assertEqual(resolve_settings({}, env)['replayDownloadRoot'], '/tmp/operator-root')
        with self.assertRaises(ReplayError):
            resolve_settings({'replayDownloadRoot': '/tmp/other'}, env)

    def test_invalid_threshold_boolean_and_protected_paths(self):
        for value in (float('nan'), float('inf'), 0, -.001, .0009, 65537, True, {}, ''):
            with self.subTest(value=value), self.assertRaises(ReplayError):
                resolve_settings({'replayMinFreeGiB': value}, {})
        for path in ('relative', '/recordings/../etc', '/app', '/etc', '/proc', '/recordings\x00x'):
            with self.subTest(path=path), self.assertRaises(ReplayError):
                resolve_settings({'replayDownloadRoot': path}, {})
        with self.assertRaises(ReplayError):
            resolve_settings({'replayEnabled': 'perhaps'}, {})
        self.assertEqual(resolve_settings({'replayMinFreeGiB': 65536}, {})['replayMinFreeGiB'], 65536)

    def test_symlink_components_and_normalization(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'alias').symlink_to(root, target_is_directory=True)
            env = {'RECORDWEB_REPLAY_DOWNLOAD_BASE': temp}
            with self.assertRaises(ReplayError):
                resolve_settings({'replayDownloadRoot': str(root / 'alias' / 'child')}, env)
            result = resolve_settings({'replayDownloadRoot': str(root) + '//a/./'}, env)
            self.assertEqual(result['replayDownloadRoot'], str(root / 'a'))


class FakeManager:
    instances = []
    fail_root = None

    def __init__(self, state_dir, root, app_dir, **kwargs):
        self.root = Path(root)
        self.closed = False
        self.active = False
        self.kwargs = kwargs
        self.instances.append(self)

    async def start(self):
        if str(self.root) == self.fail_root:
            raise RuntimeError('SECRET_MUST_NOT_BE_EXPOSED')

    async def close(self):
        self.closed = True

    def has_active_work(self):
        return self.active


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.initial = {'loginMode': True, 'otherSetting': 'before', 'replayEnabled': True,
                        'replayDownloadRoot': str(self.root / 'first'), 'replayMinFreeGiB': 5.0}
        self.disk = dict(self.initial)
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(config=dict(self.initial)))
        self.request = types.SimpleNamespace(session={'logged_in': True})
        FakeManager.instances = []
        FakeManager.fail_root = None
        self.controller = ReplaySettingsController(self.app, self.root / 'state', self.root, FakeManager,
                                                   lambda: dict(self.disk), self.save,
                                                   {'RECORDWEB_REPLAY_DOWNLOAD_BASE': str(self.root)})
        await self.controller.start(self.initial)

    def save(self, value):
        self.disk = dict(value)
        return dict(value)

    async def asyncTearDown(self):
        await self.controller.close()
        self.temp.cleanup()

    async def apply(self, posted):
        tx = await self.controller.begin(self.request, posted, self.disk)
        try:
            self.save(self.disk | tx.values)
            await tx.commit(self.disk)
        finally:
            await tx.finish()
        return tx

    async def test_omission_and_unrelated_save_allowed_during_active_work(self):
        manager = self.app.state.replay
        manager.active = True
        tx = await self.controller.begin(types.SimpleNamespace(session={}), {'otherSetting': 'after'}, self.disk)
        self.assertFalse(tx.changed)
        self.assertEqual(tx.values, {key: self.initial[key] for key in tx.values})
        await tx.commit(self.disk)
        await tx.finish()
        await tx.finish()
        self.assertIs(self.app.state.replay, manager)
        self.assertFalse(self.controller.lock.locked())
        for key, value in [('replayMinFreeGiB', 6), ('replayEnabled', False),
                           ('replayDownloadRoot', str(self.root / 'second'))]:
            with self.assertRaises(ReplayError) as failure:
                await self.controller.begin(self.request, {key: value}, self.disk)
            self.assertEqual(failure.exception.status, 409)
            self.assertFalse(self.controller.lock.locked())

    async def test_auth_validation_and_preflight_happen_before_persistence(self):
        with self.assertRaises(ReplayError) as failure:
            await self.controller.begin(types.SimpleNamespace(session={}), {'replayEnabled': False}, self.disk)
        self.assertEqual(failure.exception.status, 401)
        with self.assertRaises(ReplayError):
            await self.controller.begin(self.request, {'replayDownloadRoot': '/etc'}, self.disk)
        self.assertEqual(self.disk, self.initial)
        candidate = self.root / 'second'
        tx = await self.controller.begin(self.request, {'replayDownloadRoot': str(candidate)}, self.disk)
        self.assertTrue(candidate.is_dir())
        self.assertEqual(list(candidate.iterdir()), [])
        self.assertEqual(self.app.state.replay.root, self.root / 'first')
        await tx.rollback()
        await tx.finish()
        self.assertEqual(self.disk, self.initial)

    async def test_disable_enable_and_hot_threshold_apply(self):
        first = self.app.state.replay
        await self.apply({'replayEnabled': False})
        self.assertTrue(first.closed)
        self.assertFalse(self.controller.enabled)
        self.assertIsNone(self.app.state.replay)
        await self.apply({'replayEnabled': True, 'replayMinFreeGiB': '0.125'})
        self.assertTrue(self.controller.enabled)
        self.assertEqual(self.app.state.replay.kwargs['min_free_bytes'], GIB // 8)
        self.assertEqual(self.app.state.replay.kwargs['download_base'], self.root)

    async def test_failed_reconfigure_restores_saved_config_and_working_manager(self):
        FakeManager.fail_root = str(self.root / 'unavailable')
        with self.assertRaises(ReplayError) as failure:
            await self.apply({'replayDownloadRoot': FakeManager.fail_root})
        self.assertEqual(failure.exception.status, 500)
        self.assertNotIn('SECRET', failure.exception.detail)
        self.assertEqual(self.disk, self.initial)
        self.assertEqual(self.app.state.config, self.initial)
        self.assertEqual(str(self.app.state.replay.root), self.initial['replayDownloadRoot'])
        self.assertFalse(self.app.state.replay.closed)
        self.assertFalse(self.controller.lock.locked())
        self.assertIsNone(self.app.state.replay_startup_error)

    async def test_startup_failure_keeps_application_available(self):
        await self.controller.close()
        FakeManager.fail_root = self.initial['replayDownloadRoot']
        await self.controller.start(self.initial)
        self.assertIsNone(self.app.state.replay)
        self.assertTrue(self.app.state.replay_startup_error)
        self.assertTrue(self.controller.enabled)

    async def test_silent_config_write_failure_does_not_apply_new_settings(self):
        tx = await self.controller.begin(self.request, {'replayMinFreeGiB': 10}, self.disk)
        try:
            # Legacy saveConfig can return the submitted object without writing.
            with self.assertRaises(ReplayError) as failure:
                await tx.commit(self.disk | tx.values)
            self.assertEqual(failure.exception.status, 500)
            self.assertEqual(self.controller.current['replayMinFreeGiB'], 5)
            self.assertEqual(self.app.state.replay.kwargs['min_free_bytes'], 5 * GIB)
            self.assertEqual(self.disk, self.initial)
        finally:
            await tx.finish()

    async def test_preflight_write_failure_prevents_save(self):
        with patch('docker_support.replay.settings.tempfile.mkstemp', side_effect=PermissionError('private details')):
            with self.assertRaises(ReplayError) as failure:
                await self.controller.begin(self.request, {'replayDownloadRoot': str(self.root / 'denied')}, self.disk)
        self.assertEqual(failure.exception.status, 400)
        self.assertNotIn('private', failure.exception.detail)
        self.assertEqual(self.disk, self.initial)
        self.assertFalse(self.controller.lock.locked())

    async def test_disable_works_when_storage_is_unwritable(self):
        with patch('docker_support.replay.settings.tempfile.mkstemp', side_effect=PermissionError()):
            await self.apply({'replayEnabled': False})
        self.assertFalse(self.controller.enabled)
        self.assertIsNone(self.app.state.replay)


class FileHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_safe_code_is_persisted_and_untrusted_code_is_replaced(self):
        for code, expected in [('http_403', 'http_403'), ('RAW_SECRET_cookie_signed_url', 'engine_operation_failed')]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                script = "import json; print(json.dumps({'event': 'error', 'code': " + repr(code) + "}))"
                manager = ReplayManager(base / 'state', base / 'root', base,
                                        worker_command=[sys.executable, '-c', script])
                analysis = await manager.analyze('alice', ['https://chzzk.naver.com/video/1'])
                await manager.start()
                try:
                    for _ in range(100):
                        result = manager.get_analysis('alice', analysis['id'])
                        if result['status'] == 'failed':
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['error_code'], expected)
                    self.assertNotIn('RAW_SECRET', '\n'.join(manager.db.iterdump()))
                finally:
                    await manager.close()

    async def test_root_change_and_disable_enable_preserve_completed_history(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            state, old_root, new_root = base / 'state', base / 'old', base / 'new'
            old = ReplayManager(state, old_root, base, download_base=base)
            ident = 'a' * 32
            name = ident + '.mkv'
            output = old.completed_root / name
            output.write_bytes(b'completed-media')
            old._save('job', 'alice', {'id': ident, 'status': 'completed', 'filename': name,
                                      'can_download': True})  # pre-settings legacy payload
            await old.close()
            # First startup migrates only completed jobs to the original root.
            old = ReplayManager(state, old_root, base, download_base=base)
            self.assertEqual(old.resolve_file('alice', ident), output)
            self.assertNotIn('_download_root', old.get_job('alice', ident))
            await old.close()
            new = ReplayManager(state, new_root, base, download_base=base)
            self.assertEqual(new.resolve_file('alice', ident), output)
            self.assertFalse(new.has_active_work())
            retry = new._new_job({'url': 'test'})
            self.assertEqual(retry['_download_root'], str(new_root))
            with self.assertRaises(ReplayError):
                new.resolve_file('bob', ident)
            poisoned = new._get('job', 'alice', ident)
            poisoned['_download_root'] = '/etc'
            new._save('job', 'alice', poisoned)
            with self.assertRaises(ReplayError):
                new.resolve_file('alice', ident)
            poisoned['_download_root'] = str(old_root)
            new._save('job', 'alice', poisoned)
            await new.close()  # disabled: DB closes, history remains
            enabled = ReplayManager(state, new_root, base, download_base=base)
            self.assertEqual(enabled.resolve_file('alice', ident).read_bytes(), b'completed-media')
            await enabled.close()

    async def test_active_guard_includes_analysis_and_every_nonterminal_status(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            manager = ReplayManager(base / 'state', base / 'root', base)
            try:
                for kind in ('analysis', 'job'):
                    for index, status in enumerate(('queued', 'running', 'finalizing', 'cancelling')):
                        ident = ('a' if kind == 'analysis' else 'b') * 31 + str(index)
                        manager._save(kind, 'alice', {'id': ident, 'status': status})
                        self.assertTrue(manager.has_active_work())
                        manager._save(kind, 'alice', {'id': ident, 'status': 'failed'})
                        self.assertFalse(manager.has_active_work())
            finally:
                await manager.close()


if __name__ == '__main__':
    unittest.main()
