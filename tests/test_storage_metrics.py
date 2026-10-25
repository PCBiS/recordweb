from pathlib import Path
from types import SimpleNamespace
import ast
import json
import tempfile
import unittest
from unittest.mock import patch

from module.storage_metrics import (
    collectStorageMetrics,
    normalizeMonitorPaths,
    resolveMonitorPaths,
)


class StorageMetricsTests(unittest.TestCase):
    def test_settings_save_and_load_round_trip_normalizes_text(self):
        # Exercise the real config functions without importing recorder startup.
        source = Path(__file__).resolve().parents[1] / "module" / "data_manager.py"
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("saveConfig", "loadConfig")]
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "config.json"
            config_path.write_text(json.dumps({"fileManagerRoots": ["keep"], "other": "preserved"}))
            def read_json(path, fallback):
                return json.loads(Path(path).read_text()) if Path(path).exists() else fallback
            def write_json(path, value):
                Path(path).write_text(json.dumps(value))
                return True
            scope = {"CONFIG_PATH": str(config_path), "readJsonSafe": read_json,
                     "writeJsonSafe": write_json, "normalizeMonitorPaths": normalizeMonitorPaths,
                     "print": lambda *args, **kwargs: None}
            exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
            payload = {"systemMonitorPaths": " chzzk \narchive\nchzzk"}
            returned = scope["saveConfig"](payload)
            loaded = scope["loadConfig"]()
            persisted = read_json(config_path, {})
            for config in (returned, loaded, persisted):
                self.assertEqual(config["systemMonitorPaths"], ["chzzk", "archive"])
                self.assertEqual(config["fileManagerRoots"], ["keep"])
                self.assertEqual(config["other"], "preserved")
            self.assertIsInstance(payload["systemMonitorPaths"], str)

    def test_settings_normalize_text_and_ignore_invalid_values(self):
        self.assertEqual(normalizeMonitorPaths(" /recordings \n\n/archive\n/recordings"),
                         ["/recordings", "/archive"])
        self.assertEqual(normalizeMonitorPaths([None, {}, 3, "\x00", "chzzk"]), ["chzzk"])

    def test_empty_setting_uses_recording_paths_without_changing_permissions(self):
        cfg = {"systemMonitorPaths": [], "fileManagerRoots": ["/unrelated"]}
        channels = [{"output_dir": "chzzk"}, {"output_dir": "chzzk"}, {}]
        self.assertEqual(resolveMonitorPaths(cfg, channels), ["chzzk"])
        self.assertEqual(cfg["fileManagerRoots"], ["/unrelated"])
        self.assertEqual(resolveMonitorPaths({"systemMonitorPaths": ["/selected"]}, channels),
                         ["/selected"])
        self.assertEqual(resolveMonitorPaths({}, []), [])

    def test_relative_symlink_and_same_filesystem_are_counted_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            storage = base / "recordings"
            storage.mkdir()
            (storage / "channel").mkdir()
            (base / "chzzk").symlink_to(storage / "channel", target_is_directory=True)
            rows = collectStorageMetrics(["chzzk", "recordings", "recordings/channel"], td)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["available"])
            self.assertEqual(rows[0]["mountpoint"], str(storage / "channel"))
            self.assertEqual(rows[0]["paths"], [str(storage / "channel"), str(storage)])
            self.assertGreater(rows[0]["total"], 0)

    def test_missing_directory_and_file_are_unavailable_without_parent_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "file").write_text("fixture")
            with patch("module.storage_metrics.shutil.disk_usage") as disk_usage:
                rows = collectStorageMetrics(["missing", "file"], td)
            disk_usage.assert_not_called()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertFalse(row["available"])
                self.assertIsNone(row["total"])
                self.assertEqual(row["error"], "unavailable")
                self.assertNotEqual(row["mountpoint"], td)

    def test_distinct_filesystems_remain_separate_and_permission_errors_are_explicit(self):
        directory_mode = 0o040755
        def fake_stat(path):
            return SimpleNamespace(st_mode=directory_mode, st_dev=1 if path.endswith("a") else 2)
        def fake_usage(path):
            if path.endswith("blocked"):
                raise PermissionError("fixture")
            return SimpleNamespace(total=1000, used=200, free=800)
        with patch("module.storage_metrics.os.path.realpath", side_effect=lambda p: p), \
             patch("module.storage_metrics.os.stat", side_effect=fake_stat), \
             patch("module.storage_metrics.shutil.disk_usage", side_effect=fake_usage):
            rows = collectStorageMetrics(["a", "b", "blocked"], "/base")
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["available"] for row in rows], [True, True, False])
        self.assertEqual(rows[0]["percent"], 20.0)


if __name__ == "__main__":
    unittest.main()
