"""Faithful port of .NET Framework / Mono ``System.Random``.

The C# server (``DungeonRunners.Managers.MazeGenerator``) seeds a
``new System.Random((int)seed)`` and drives the Growing-Tree maze algorithm from
it. The original client generates the *same* maze terrain from the *same* seed,
so for server-placed mobs to land in real rooms the server must reproduce the
client's maze **exactly** — which means reproducing .NET's RNG bit-for-bit, not
substituting Python's Mersenne-Twister ``random.Random``.

This is the documented subtractive (Knuth) generator from the .NET Framework
reference source (``Random.cs``); Mono ships the same algorithm, which is what
the C# reference server runs under Unity.

Only the methods the maze needs are implemented: ``next``, ``next_max`` and
``next_range``. ``GetSampleForLargeRange`` (ranges wider than int.MaxValue) is
not needed by the maze and is omitted.
"""
from __future__ import annotations

_MBIG = 2147483647   # int.MaxValue
_MSEED = 161803398
_INT_MIN = -2147483648


def to_int32(value: int) -> int:
    """Reinterpret an arbitrary integer as a signed 32-bit int, mirroring the
    C# ``(int)seed`` cast used on the ``uint`` maze seed (e.g. 0xBEEFBEEF)."""
    return ((value & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


class DotNetRandom:
    """Drop-in reproduction of ``System.Random`` (legacy/Framework algorithm)."""

    def __init__(self, seed: int) -> None:
        # Match C#: subtraction = (Seed == int.MinValue) ? int.MaxValue : Abs(Seed)
        subtraction = _MBIG if seed == _INT_MIN else abs(seed)

        seed_array = [0] * 56
        mj = _MSEED - subtraction
        seed_array[55] = mj
        mk = 1
        for i in range(1, 55):
            ii = (21 * i) % 55
            seed_array[ii] = mk
            mk = mj - mk
            if mk < 0:
                mk += _MBIG
            mj = seed_array[ii]
        for _k in range(1, 5):
            for i in range(1, 56):
                seed_array[i] -= seed_array[1 + (i + 30) % 55]
                if seed_array[i] < 0:
                    seed_array[i] += _MBIG
        self._seed_array = seed_array
        self._inext = 0
        self._inextp = 21

    def _internal_sample(self) -> int:
        loc_inext = self._inext + 1
        if loc_inext >= 56:
            loc_inext = 1
        loc_inextp = self._inextp + 1
        if loc_inextp >= 56:
            loc_inextp = 1

        ret_val = self._seed_array[loc_inext] - self._seed_array[loc_inextp]
        if ret_val == _MBIG:
            ret_val -= 1
        if ret_val < 0:
            ret_val += _MBIG

        self._seed_array[loc_inext] = ret_val
        self._inext = loc_inext
        self._inextp = loc_inextp
        return ret_val

    def _sample(self) -> float:
        return self._internal_sample() * (1.0 / _MBIG)

    def next(self) -> int:
        """``Random.Next()`` — non-negative int in [0, int.MaxValue)."""
        return self._internal_sample()

    def next_max(self, max_value: int) -> int:
        """``Random.Next(int maxValue)`` — int in [0, maxValue)."""
        if max_value < 0:
            raise ValueError("maxValue must be non-negative")
        return int(self._sample() * max_value)

    def next_range(self, min_value: int, max_value: int) -> int:
        """``Random.Next(int minValue, int maxValue)`` — int in [minValue, maxValue)."""
        if min_value > max_value:
            raise ValueError("minValue must not be greater than maxValue")
        rng = max_value - min_value
        # Maze ranges are always <= int.MaxValue, so the simple path suffices.
        return int(self._sample() * rng) + min_value

    def next_double(self) -> float:
        """``Random.NextDouble()`` — float in [0.0, 1.0)."""
        return self._sample()
