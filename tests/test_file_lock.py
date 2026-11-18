"""Stale file lock recovery when owning process died."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.storage.file_lock import FileLock, clear_stale_lock


class TestStaleFileLock(unittest.TestCase):
    def test_clear_stale_lock_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.lock"
            path.write_text("999999999", encoding="ascii")
            self.assertTrue(clear_stale_lock(path))
            self.assertFalse(path.exists())

    def test_clear_stale_lock_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.lock"
            path.write_text(str(os.getpid()), encoding="ascii")
            self.assertFalse(clear_stale_lock(path))
            self.assertTrue(path.exists())

    def test_acquire_after_stale_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            path.write_text("999999999", encoding="ascii")
            lock = FileLock(path, timeout=2)
            lock.acquire()
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="ascii"), str(os.getpid()))
            lock.release()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
