from __future__ import annotations

import asyncio
import signal
import sys
from typing import Callable


class ShutdownRequested(Exception):
    """Raised when the user requests shutdown (Ctrl+C)."""


_shutdown_requested = False
_shutdown_callbacks: list[Callable[[], None]] = []
_event_loop: asyncio.AbstractEventLoop | None = None
_handlers_installed = False


def is_shutdown_requested() -> bool:
    return _shutdown_requested


def register_asyncio_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _event_loop
    _event_loop = loop


def register_shutdown_handler(on_shutdown: Callable[[], None]) -> None:
    if on_shutdown not in _shutdown_callbacks:
        _shutdown_callbacks.append(on_shutdown)
    _install_signal_handlers()


def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    current = asyncio.current_task(loop)
    for task in asyncio.all_tasks(loop):
        if task is not current and not task.done():
            task.cancel()


def request_shutdown(*, force: bool = False) -> None:
    global _shutdown_requested
    if force:
        print("\nForce quit.", flush=True)
        raise SystemExit(130)
    if _shutdown_requested:
        print("\nForce quit.", flush=True)
        raise SystemExit(130)
    _shutdown_requested = True
    print("\nStopping... (Ctrl+C again to force quit)", flush=True)
    for cb in list(_shutdown_callbacks):
        try:
            cb()
        except Exception:
            pass
    loop = _event_loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_cancel_all_tasks, loop)


def _install_signal_handlers() -> None:
    global _handlers_installed
    if _handlers_installed:
        return
    _handlers_installed = True

    def handler(signum: int, frame: object) -> None:
        request_shutdown()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)


async def interruptible_sleep(seconds: float, *, poll: float = 0.25) -> None:
    """Sleep in short chunks so Ctrl+C is picked up quickly."""
    if seconds <= 0:
        return
    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        if is_shutdown_requested():
            raise ShutdownRequested()
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(poll, remaining))


def keep_awake_enable() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def keep_awake_disable() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS = 0x80000000
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
