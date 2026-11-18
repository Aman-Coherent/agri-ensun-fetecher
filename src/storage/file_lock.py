"""Cross-process file lock using atomic create (stdlib only)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


class FileLockTimeout(TimeoutError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
        return int(raw) if raw.isdigit() else None
    except OSError:
        return None


def clear_stale_lock(path: Path) -> bool:
    """Remove lock file if the owning PID is no longer running."""
    if not path.exists():
        return False
    pid = _read_lock_pid(path)
    if pid is not None and _pid_alive(pid):
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


class FileLock:
    def __init__(self, path: Path, timeout: float = 120, poll_seconds: float = 0.25) -> None:
        self.path = path
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self._held = False
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._held:
            return
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while time.monotonic() < deadline:
            if self.path.exists() and clear_stale_lock(self.path):
                pass
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                self._fd = fd
                self._held = True
                return
            except FileExistsError:
                time.sleep(self.poll_seconds)
            except OSError:
                time.sleep(self.poll_seconds)
        raise FileLockTimeout(f"Could not acquire lock: {self.path}")

    def release(self) -> None:
        if not self._held:
            return
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                self.path.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(self.poll_seconds)
        self._held = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
