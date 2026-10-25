"""Filesystem capacity for selected recording directories, without recursive scans."""

from __future__ import annotations

import os
import shutil
import stat
from typing import Any


def normalizeMonitorPaths(value: Any) -> list[str]:
    """Accept a saved list or the settings form's newline-separated text."""
    if isinstance(value, str):
        candidates = value.splitlines()
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = []

    paths = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        path = candidate.strip()
        if not path or "\x00" in path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def resolveMonitorPaths(config: dict, channels: list[dict]) -> list[str]:
    """Use explicit paths, or the configured recording destinations by default."""
    paths = normalizeMonitorPaths((config or {}).get("systemMonitorPaths", []))
    if paths:
        return paths
    return normalizeMonitorPaths([
        channel.get("output_dir")
        for channel in channels or []
        if isinstance(channel, dict)
    ])


def collectStorageMetrics(paths: list[str], base_directory: str) -> list[dict]:
    """Return one capacity row per filesystem and retain unavailable directories.

    Relative paths are resolved against the application directory, matching the
    recording configuration. A missing directory is never replaced by a parent.
    This measures the filesystem's capacity, not the sizes of files in a folder.
    """
    rows = []
    by_filesystem = {}
    seen_paths = set()
    for configured_path in normalizeMonitorPaths(paths):
        expanded = os.path.expanduser(configured_path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(base_directory, expanded)
        path = os.path.normcase(os.path.realpath(expanded))
        if path in seen_paths:
            continue
        seen_paths.add(path)

        row = {
            "device": "",
            "mountpoint": path,
            "label": path,
            "fstype": "",
            "paths": [path],
            "available": False,
            "total": None,
            "used": None,
            "free": None,
            "percent": None,
        }
        try:
            info = os.stat(path)
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(path)
            usage = shutil.disk_usage(path)
            if info.st_dev in by_filesystem:
                by_filesystem[info.st_dev]["paths"].append(path)
                continue
        except (OSError, ValueError):
            row["error"] = "unavailable"
            rows.append(row)
            continue

        accessible = int(usage.used) + int(usage.free)
        row.update({
            "available": True,
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
            "percent": round(100.0 * usage.used / accessible, 1) if accessible else 0.0,
        })
        by_filesystem[info.st_dev] = row
        rows.append(row)
    return rows
