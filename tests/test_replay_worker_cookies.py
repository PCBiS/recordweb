"""Exercise the worker's actual read-only accessor without starting the worker."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docker_support.replay.worker import readonly_cookie_path


class WorkerCookieTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.path=self.root/'ycookie.txt'

    def tearDown(self):self.temporary.cleanup()

    def call_without_writes(self,path):
        with patch('builtins.open',side_effect=AssertionError('accessor_must_not_open_files')), \
             patch.object(Path,'write_text',side_effect=AssertionError('accessor_must_not_write')), \
             patch.object(Path,'write_bytes',side_effect=AssertionError('accessor_must_not_write')):
            return readonly_cookie_path(str(path))

    def test_missing_file_is_anonymous_without_initialization(self):
        self.assertIsNone(self.call_without_writes(self.path));self.assertFalse(self.path.exists())

    def test_empty_file_is_anonymous_and_unchanged(self):
        self.path.touch();before=self.path.stat()
        self.assertIsNone(self.call_without_writes(self.path))
        after=self.path.stat()
        self.assertEqual((before.st_ino,before.st_mtime_ns,before.st_size),(after.st_ino,after.st_mtime_ns,after.st_size))

    def test_symlink_to_nonempty_file_and_dangling_symlink_are_anonymous(self):
        target=self.root/'nonempty';target.write_text('synthetic nonempty cookie')
        for name,destination in [('linked',target),('dangling',self.root/'absent')]:
            link=self.root/name;link.symlink_to(destination)
            with self.subTest(name=name):self.assertIsNone(self.call_without_writes(link))
        self.assertEqual(target.read_text(),'synthetic nonempty cookie')

    def test_nonempty_valid_or_malformed_jar_retains_exact_path_and_bytes(self):
        for content in ('# Netscape HTTP Cookie File\n','synthetic invalid nonempty cookie\n'):
            self.path.write_text(content)
            with self.subTest(content=content):
                self.assertEqual(self.call_without_writes(self.path),str(self.path))
                self.assertEqual(self.path.read_text(),content)

    def test_directory_is_anonymous(self):
        self.path.mkdir();self.assertIsNone(self.call_without_writes(self.path))


if __name__=='__main__':unittest.main()
