"""Cancelled metadata tasks must not abort the original lifespan cleanup."""
import ast
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_background_tasks_still_flush_and_close(self):
        source = Path(__file__).resolve().parents[1] / 'recordWEB.py'
        node = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'lifespan')
        calls = []
        async def initial(app):
            pass
        async def waiting(*args):
            await asyncio.sleep(60)
        async def flush():
            calls.append('flush')
        async def close():
            calls.append('close')
        recorder = SimpleNamespace(**{name: lambda *args: None for name in ('setChannels', 'setChannelsRef', 'setChannelsLockRef')})
        fake_live = ModuleType('module.live_recorder')
        fake_live.closeHttpxClient = close
        scope = {'asynccontextmanager': asynccontextmanager, 'FastAPI': object, 'asyncio': asyncio, 'suppress': suppress,
                 'loadChannels': lambda: [], 'ChannelFsm': lambda: object(), 'loadConfig': lambda: {}, 'loadCookies': lambda: {},
                 'coerceChannelsInplace': lambda channels: False, 'RecorderManager': recorder, 'initChannelStates': initial,
                 '_makeMetaFetcher': lambda app: object(), 'DebouncedSaver': lambda *a, **k: SimpleNamespace(flush=flush),
                 'mc_ensure': lambda app: None, '_seedMetadataWEB': waiting, 'mc_refreshLoop': waiting}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<lifespan-fixture>', 'exec'), scope)
        app = SimpleNamespace(state=SimpleNamespace())
        with patch.dict(sys.modules, {'module.live_recorder': fake_live}):
            async with scope['lifespan'](app):
                await asyncio.sleep(0)
        self.assertEqual(calls, ['flush', 'close'])


if __name__ == '__main__':
    unittest.main()
