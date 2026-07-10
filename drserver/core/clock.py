"""Time shim replacing UnityEngine.Time.

Unity's ``Time.time`` is float seconds since startup, advanced once per frame;
``Time.deltaTime`` is the frame interval. On the server the tick loop calls
``advance(dt)`` once per tick so ``now()`` is stable within a tick (matching
Unity's per-frame snapshot semantics), and ``delta_time`` returns the last dt.
"""
from __future__ import annotations

import time as _time

_start = _time.monotonic()
_now = 0.0
_delta = 0.0


def advance(dt: float) -> None:
    """Called once per server tick with the elapsed seconds since the last tick."""
    global _now, _delta
    _delta = dt
    _now += dt


def sync_to_wall() -> None:
    """Resync the snapshot clock to the real monotonic clock (e.g. at tick start)."""
    global _now
    _now = _time.monotonic() - _start


def now() -> float:
    """Seconds since server start (Unity Time.time equivalent)."""
    return _now


def delta_time() -> float:
    return _delta


def real_now() -> float:
    """Unsnapshotted monotonic seconds since start (for precise rate-limiting)."""
    return _time.monotonic() - _start
