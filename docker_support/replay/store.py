"""Small transactional persistence layer; only normalized public data is stored."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3


def safe_path(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('symlink_path')
    return path


def directory(path):
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError('directory_required')
    return path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Store:
    def __init__(self, root):
        self.root = directory(root)
        self.lock = os.open(self.root / 'manager.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(safe_path(self.root / 'replay.sqlite3'))
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''
              PRAGMA journal_mode=WAL;
              PRAGMA synchronous=FULL;
              CREATE TABLE IF NOT EXISTS records(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS records_queue ON records(kind,status);
              CREATE TABLE IF NOT EXISTS requests(
                owner TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, ids TEXT NOT NULL,
                PRIMARY KEY(owner,key));
              CREATE TABLE IF NOT EXISTS logs(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
            ''')
        except BaseException:
            os.close(self.lock)
            raise

    def save(self, kind, owner, data):
        self.db.execute('INSERT INTO records VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                        (data['id'], kind, owner, data['status'], canonical(data)))

    def get(self, kind, owner, ident):
        row = self.db.execute('SELECT payload FROM records WHERE id=? AND kind=? AND owner=?',
                              (ident, kind, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
        os.close(self.lock)
