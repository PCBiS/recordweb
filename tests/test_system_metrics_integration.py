"""Exercise the original collector without starting recording or application IO."""
import ast
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from module.storage_metrics import collectStorageMetrics, resolveMonitorPaths


SOURCE = Path(__file__).resolve().parents[1]


class SystemMetricsIntegrationTests(unittest.TestCase):
    def collector(self, directory):
        tree = ast.parse((SOURCE / 'recordWEB.py').read_text())
        function = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == '_collectSysMetrics')
        psutil = SimpleNamespace(
            cpu_percent=Mock(return_value=12.5),
            virtual_memory=Mock(return_value=SimpleNamespace(total=1000, used=200, available=800, percent=20.0)),
            net_io_counters=Mock(return_value=SimpleNamespace(bytes_sent=100, bytes_recv=300)),
            cpu_count=Mock(return_value=4),
            disk_partitions=Mock(side_effect=AssertionError('must not enumerate unrelated mounts')),
        )
        storage = Mock(wraps=collectStorageMetrics)
        scope = {
            'List': list, 'time': time, 'psutil': psutil, 'cpu_name': 'fixture CPU',
            '_SYS_METRICS_LOCK': threading.Lock(),
            '_SYS_METRICS_CACHE': {'at': 0.0, 'data': None},
            '_SYS_METRICS_CACHE_TTL': 60.0,
            '_SYS_NET_PREV': SimpleNamespace(bytes_sent=0, bytes_recv=0),
            '_SYS_NET_PREV_AT': time.monotonic() - 1,
            'resolveMonitorPaths': resolveMonitorPaths,
            'collectStorageMetrics': storage,
            'BASE_DIR': directory,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'metrics fixture', 'exec'), scope)
        return scope['_collectSysMetrics'], psutil, storage

    def test_selected_path_overrides_channels_and_leaves_other_metrics_intact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'selected').mkdir()
            (root / 'unrelated-channel').mkdir()
            collect, psutil, _ = self.collector(td)
            data = collect({'systemMonitorPaths': ['selected']}, [{'output_dir': 'unrelated-channel'}])
            self.assertEqual([d['mountpoint'] for d in data['disks']], [str(root / 'selected')])
            self.assertTrue(data['disks'][0]['available'])
            self.assertEqual(data['cpu']['percent'], 12.5)
            self.assertEqual(data['memory']['free'], 800)
            self.assertGreater(data['network']['down_bps'], 0)
            psutil.disk_partitions.assert_not_called()

    def test_path_change_invalidates_cache_and_empty_selection_tracks_channels(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ['first', 'second']:
                (root / name).mkdir()
            collect, _, storage = self.collector(td)
            first = collect({}, [{'output_dir': 'first'}])
            cached = collect({}, [{'output_dir': 'first'}])
            self.assertIs(first, cached)
            self.assertEqual(storage.call_count, 1)
            second = collect({}, [{'output_dir': 'second'}])
            self.assertEqual(second['disks'][0]['mountpoint'], str(root / 'second'))
            self.assertEqual(storage.call_count, 2)
            missing = collect({'systemMonitorPaths': ['missing']}, [])
            self.assertFalse(missing['disks'][0]['available'])
            self.assertIsNone(missing['disks'][0]['total'])


if __name__ == '__main__':
    unittest.main()
