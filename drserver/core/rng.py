"""RNG shim replacing UnityEngine.Random.

Provides the Unity Random surface used by ported code. Combat crit/loot rolls in
the C# server also used a dedicated MersenneTwister (ported separately in combat/);
this shim covers the general UnityEngine.Random.* call sites.
"""
from __future__ import annotations

import random as _random

_rng = _random.Random()


def seed(value: int) -> None:
    _rng.seed(value)


def value() -> float:
    """Unity Random.value: float in [0, 1]."""
    return _rng.random()


def range_float(min_inclusive: float, max_inclusive: float) -> float:
    """Unity Random.Range(float, float): inclusive on both ends."""
    return _rng.uniform(min_inclusive, max_inclusive)


def range_int(min_inclusive: int, max_exclusive: int) -> int:
    """Unity Random.Range(int, int): max is EXCLUSIVE."""
    if max_exclusive <= min_inclusive:
        return min_inclusive
    return _rng.randrange(min_inclusive, max_exclusive)
