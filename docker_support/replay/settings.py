"""Bounded replay settings and serialized live configuration transactions."""
import asyncio
import inspect
import math
import os
from pathlib import Path
import tempfile

from .manager import ReplayError
from .store import directory, safe_path

SETTINGS_KEYS = ('replayEnabled', 'replayDownloadRoot', 'replayMinFreeGiB')
GIB = 1024 ** 3
MIN_FREE_GIB = .001
MAX_FREE_GIB = 65536
STARTUP_ERROR = '다시보기 저장소를 준비하지 못했습니다. 관리자에게 문의해 주세요.'
ACTIVE_ERROR = '분석·다운로드 작업이 대기 또는 진행 중입니다. 모든 작업이 끝난 뒤 설정을 변경해 주세요.'
APPLY_ERROR = '다시보기 설정을 적용하지 못했습니다. 이전 설정으로 복원했습니다.'


def _absolute_path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError('invalid_path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path):
        raise ValueError('invalid_path')
    return safe_path(path)


def download_base(environ=None):
    """Only the operator environment can widen the storage boundary."""
    env = os.environ if environ is None else environ
    if env.get('RECORDWEB_REPLAY_DOWNLOAD_BASE'):
        return _absolute_path(env['RECORDWEB_REPLAY_DOWNLOAD_BASE'])
    base = Path('/recordings')
    legacy = env.get('RECORDWEB_REPLAY_DOWNLOAD_ROOT')
    if legacy:
        legacy = _absolute_path(legacy)
        if not legacy.is_relative_to(base):
            return legacy
    return _absolute_path(base)


def validate_download_root(value, base):
    path = _absolute_path(value)
    base = _absolute_path(base)
    if not path.is_relative_to(base):
        raise ValueError('outside_download_base')
    return path


def _boolean(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in ('true', 'false', '1', '0', 'on', 'off'):
        return value.lower() in ('true', '1', 'on')
    raise ValueError('invalid_boolean')


def _threshold(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('invalid_threshold')
    result = float(value)
    if not math.isfinite(result) or not MIN_FREE_GIB <= result <= MAX_FREE_GIB:
        raise ValueError('invalid_threshold')
    return result


def resolve_settings(config, environ=None, download_base=None):
    """Explicit saved values take precedence over image environment defaults."""
    env = os.environ if environ is None else environ
    config = config or {}
    base = globals()['download_base'](env) if download_base is None else _absolute_path(download_base)
    try:
        legacy_min = int(env.get('RECORDWEB_REPLAY_MIN_FREE_BYTES', 5 * GIB)) / GIB
        if not math.isfinite(legacy_min):
            raise ValueError()
        legacy_min = max(MIN_FREE_GIB, legacy_min)
        if legacy_min > MAX_FREE_GIB:
            legacy_min = 5
    except (ValueError, TypeError, OverflowError):
        legacy_min = 5
    try:
        return {
            'replayEnabled': _boolean(config['replayEnabled'] if 'replayEnabled' in config else env.get('RECORDWEB_REPLAY_ENABLED', '1')),
            'replayDownloadRoot': str(validate_download_root(config['replayDownloadRoot'] if 'replayDownloadRoot' in config else env.get('RECORDWEB_REPLAY_DOWNLOAD_ROOT', '/recordings/replay'), base)),
            'replayMinFreeGiB': _threshold(config['replayMinFreeGiB'] if 'replayMinFreeGiB' in config else legacy_min),
        }
    except (ValueError, TypeError, OSError):
        raise ReplayError(400, '다시보기 사용 여부·저장 경로·최소 여유 공간 설정을 확인해 주세요.') from None


async def _call(function, *args, **kwargs):
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class ReplaySettingsController:
    def __init__(self, app, state_dir, app_dir, manager_factory, load_config, save_config, environ=None):
        self.app = app
        self.state_dir = Path(state_dir)
        self.app_dir = Path(app_dir)
        self.manager_factory = manager_factory
        self.load_config = load_config
        self.save_config = save_config
        self.environ = dict(os.environ if environ is None else environ)
        self.download_base = download_base(self.environ)
        self.lock = asyncio.Lock()
        self.current = None
        self.app.state.replay = None
        self.app.state.replay_startup_error = None

    @property
    def enabled(self):
        return bool(self.current and self.current['replayEnabled'])

    async def _open(self, values):
        manager = None
        try:
            manager = self.manager_factory(self.state_dir, values['replayDownloadRoot'], self.app_dir,
                                           min_free_bytes=int(values['replayMinFreeGiB'] * GIB),
                                           download_base=self.download_base)
            await manager.start()
            return manager
        except BaseException:
            if manager is not None:
                try:
                    await manager.close()
                except (Exception, asyncio.CancelledError):
                    pass
            raise

    async def start(self, config):
        async with self.lock:
            try:
                self.current = resolve_settings(config, self.environ, self.download_base)
                if self.enabled:
                    self.app.state.replay = await self._open(self.current)
                self.app.state.replay_startup_error = None
            except Exception:
                self.app.state.replay = None
                self.app.state.replay_startup_error = STARTUP_ERROR

    async def close(self):
        async with self.lock:
            manager = self.app.state.replay
            self.app.state.replay = None
            if manager is not None:
                await manager.close()

    def _preflight(self, values):
        root = directory(validate_download_root(values['replayDownloadRoot'], self.download_base))
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix='.replay-settings-', dir=root)
            os.close(fd)
        finally:
            if name is not None:
                os.unlink(name)

    async def begin(self, request, posted, current_config):
        await self.lock.acquire()
        try:
            old_values = dict(self.current or resolve_settings(current_config, self.environ, self.download_base))
            supplied = {key: posted[key] for key in SETTINGS_KEYS if key in posted}
            values = resolve_settings(old_values | supplied, self.environ, self.download_base)
            changed = values != old_values
            if changed:
                if not request.session.get('logged_in'):
                    raise ReplayError(401, '다시보기 설정을 변경하려면 로그인해 주세요.')
                manager = self.app.state.replay
                if manager is not None and manager.has_active_work():
                    raise ReplayError(409, ACTIVE_ERROR)
                if values['replayEnabled'] and (values['replayEnabled'] != old_values['replayEnabled'] or values['replayDownloadRoot'] != old_values['replayDownloadRoot']):
                    try:
                        self._preflight(values)
                    except (OSError, ValueError):
                        raise ReplayError(400, '다시보기 저장 경로에 쓸 수 없습니다. 경로와 권한을 확인해 주세요.') from None
            return SettingsTransaction(self, values, old_values, dict(current_config or {}), changed)
        except BaseException:
            self.lock.release()
            raise


class SettingsTransaction:
    def __init__(self, controller, values, previous, previous_config, changed):
        self.controller = controller
        self.values = values
        self.previous = previous
        self.previous_config = previous_config
        self.changed = changed
        self.finished = False
        self.applied = False

    async def _restore_manager(self):
        controller = self.controller
        manager = controller.app.state.replay
        controller.app.state.replay = None
        if manager is not None:
            await manager.close()
        controller.current = dict(self.previous)
        if self.previous['replayEnabled']:
            controller.app.state.replay = await controller._open(self.previous)
        controller.app.state.replay_startup_error = None

    async def commit(self, saved_config):
        controller = self.controller
        if self.finished:
            raise RuntimeError('settings_transaction_finished')
        if not self.changed:
            controller.current = dict(self.values)
            return
        try:
            # RecordWEB's legacy saver returns submitted values even when its
            # write failed. Confirm durable replay values before changing service.
            persisted = await _call(controller.load_config)
            if resolve_settings(persisted, controller.environ, controller.download_base) != self.values:
                raise ReplayError(500, APPLY_ERROR)
            manager = controller.app.state.replay
            controller.app.state.replay = None
            if manager is not None:
                await manager.close()
            if self.values['replayEnabled']:
                controller.app.state.replay = await controller._open(self.values)
            controller.current = dict(self.values)
            controller.app.state.replay_startup_error = None
            self.applied = True
        except (Exception, asyncio.CancelledError) as failure:
            # The original config route saved successfully. Restore the whole
            # prior config while the same lock excludes replay/settings races.
            restore = self.previous_config | self.previous
            try:
                await self._restore_manager()
            except (Exception, asyncio.CancelledError):
                controller.app.state.replay_startup_error = STARTUP_ERROR
            try:
                persisted = await _call(controller.save_config, restore)
                verified = await _call(controller.load_config)
                if resolve_settings(verified, controller.environ, controller.download_base) != self.previous:
                    raise RuntimeError('settings_restore_unconfirmed')
                controller.app.state.config = persisted if isinstance(persisted, dict) else restore
            except Exception:
                controller.app.state.config = restore
                raise ReplayError(500, '다시보기 설정 적용과 설정 파일 복원에 실패했습니다. 관리자에게 문의해 주세요.') from None
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            raise ReplayError(500, APPLY_ERROR) from None

    async def rollback(self):
        if self.applied:
            await self._restore_manager()
            self.applied = False

    async def finish(self):
        if not self.finished:
            self.finished = True
            self.controller.lock.release()
