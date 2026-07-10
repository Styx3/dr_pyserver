"""Validate the .NET ``System.Random`` port against known reference output.

The maze generator's correctness hinges entirely on this RNG matching .NET
bit-for-bit (the client builds the same maze from the same seed). The most
widely-cited reference vector is ``new Random(0)``: its first ``NextDouble()``
is ``0.7262432699679598`` and thus its first ``Next()`` (the raw internal
sample) is ``1559595546``. If the seed-array initialisation or the subtractive
step were wrong, this exact value would not reproduce.
"""
import pytest

from drserver.util.dotnet_random import DotNetRandom, to_int32


def test_seed0_first_next_matches_dotnet_reference():
    # Arrange
    rng = DotNetRandom(0)
    # Act
    first = rng.next()
    # Assert — exact .NET Framework value
    assert first == 1559595546


def test_seed0_first_nextdouble_matches_dotnet_reference():
    # Arrange
    rng = DotNetRandom(0)
    # Act
    value = rng.next_double()
    # Assert
    assert value == pytest.approx(0.7262432699679598, abs=1e-12)


def test_same_seed_is_deterministic():
    # Arrange
    a = DotNetRandom(0xBEEFBEEF)
    b = DotNetRandom(0xBEEFBEEF)
    # Act
    seq_a = [a.next() for _ in range(50)]
    seq_b = [b.next() for _ in range(50)]
    # Assert
    assert seq_a == seq_b


def test_different_seeds_diverge():
    # Arrange / Act
    seq1 = [DotNetRandom(1).next() for _ in range(10)]
    seq2 = [DotNetRandom(2).next() for _ in range(10)]
    # Assert
    assert seq1 != seq2


def test_next_range_stays_in_bounds():
    # Arrange
    rng = DotNetRandom(12345)
    # Act / Assert — the maze relies on Next(1, 101) and Next(0, n)
    for _ in range(10000):
        v = rng.next_range(1, 101)
        assert 1 <= v < 101


def test_next_max_stays_in_bounds():
    # Arrange
    rng = DotNetRandom(999)
    # Act / Assert
    for _ in range(10000):
        v = rng.next_max(4)
        assert 0 <= v < 4


def test_to_int32_converts_uint_seed_like_csharp_cast():
    # 0xBEEFBEEF as a C# (int) cast is negative; abs() is taken in the ctor.
    assert to_int32(0xBEEFBEEF) == -1091584273
    assert to_int32(0) == 0
    assert to_int32(0x7FFFFFFF) == 2147483647
    assert to_int32(0x80000000) == -2147483648


def test_uint_seed_matches_int32_cast_seed():
    # DotNetRandom must be fed the (int)-cast seed to match C# MazeGenerator.
    a = [DotNetRandom(to_int32(0xBEEFBEEF)).next() for _ in range(5)]
    b = [DotNetRandom(-1091584273).next() for _ in range(5)]
    assert a == b
