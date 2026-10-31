"""Small, bounded and secret-free support primitives."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def number(name, default, low, high):
    try:
        value = int(os.environ.get(name, default))
    except (ValueError, TypeError):
        value = default
    return min(high, max(low, value))


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = path.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise ValueError("unsafe_support_directory")
    os.chmod(path, 0o700)
    return path


def read_json(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise ValueError("bounded_regular_json_required")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("json_size_limit")
        return json.loads(raw)


def atomic_json(path, value):
    path = Path(path)
    parent = private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RotatingJSON:
    """Single writer; no symlink following and a strict per-record limit."""

    def __init__(self, path, maximum=10 * 1024 * 1024, backups=5):
        self.path = Path(path)
        self.maximum = maximum
        self.backups = backups
        self.stream = None
        self.size = 0

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None

    def _open(self):
        private_dir(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
            os.close(fd)
            raise ValueError("unsafe_log_file")
        os.fchmod(fd, 0o600)
        self.stream = os.fdopen(fd, "ab", buffering=0)
        self.size = meta.st_size

    def write(self, value):
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(data) > min(self.maximum, 65536):
            raise ValueError("log_record_too_large")
        if self.stream is None:
            self._open()
        if self.size + len(data) > self.maximum:
            self.close()
            for index in range(self.backups, 0, -1):
                target = self.path.with_name(self.path.name + "." + str(index))
                source = self.path if index == 1 else self.path.with_name(self.path.name + "." + str(index - 1))
                if source.exists() or source.is_symlink():
                    if source.is_symlink() or target.is_symlink():
                        raise ValueError("unsafe_rotation_link")
                    os.replace(source, target)
            self._open()
        self.stream.write(data)
        self.size += len(data)
