"""Main-thread dispatcher replacing Unity's MainThreadDispatcher.

In Unity, background socket threads marshalled work back to the main thread via a
queue drained in Update(). The Python server runs networking and game logic on a
single asyncio event loop, so "main thread" == the event loop. ``post`` schedules
a callable to run on the loop; ``run_coroutine`` schedules a coroutine. Both are
safe to call from any thread.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

_loop: asyncio.AbstractEventLoop | None = None


def bind(loop: asyncio.AbstractEventLoop) -> None:
    """Bind the dispatcher to the running event loop (called once at startup)."""
    global _loop
    _loop = loop


def post(fn: Callable[[], None]) -> None:
    """Run ``fn`` on the event loop thread."""
    if _loop is None:
        raise RuntimeError("dispatcher not bound to a loop")
    _loop.call_soon_threadsafe(fn)


def run_coroutine(coro: Awaitable) -> None:
    """Schedule a coroutine on the event loop from any thread."""
    if _loop is None:
        raise RuntimeError("dispatcher not bound to a loop")
    asyncio.run_coroutine_threadsafe(coro, _loop)
