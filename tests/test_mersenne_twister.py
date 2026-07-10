"""MersenneTwister parity tests — ROUTE 2B step 1.

Oracle strategy: the DR-client MT (C# Combat/MersenneTwister.cs) is bit-identical
to canonical MT19937. Proof of equivalence of its non-standard tempering masks:

    (y & 0xFF3A58AD) << 7  == (y << 7) & 0x9D2C5680   (standard mask b)
    (y & 0xFFFFDF8C) << 15 == (y << 15) & 0xEFC60000  (standard mask c)

because (y & A) << s == (y << s) & ((A << s) & 0xFFFFFFFF). So an independent
canonical MT19937 reference is a valid C# oracle. The expected vectors below were
produced by a throwaway textbook mt19937ar implementation (NOT the production
port), and the seed-5489 first value (3499211612) is the widely-published
MT19937 init_genrand(5489) reference output.
"""
from __future__ import annotations

import pytest

from drserver.combat.rng import MersenneTwister


# Canonical MT19937 ground truth (first five outputs per seed).
_KNOWN_VECTORS = {
    5489: [3499211612, 581869302, 3890346734, 3586334585, 545404204],
    0x1105: [4293858116, 699692587, 1213834231, 4068197670, 994957275],
    0x1A2B3C4D: [3014973049, 4164822375, 1290338347, 701091106, 2408075860],
    0xDEADBEEF: [956529277, 3842322136, 3319553134, 1843186657, 2704993644],
}


@pytest.mark.unit
@pytest.mark.parametrize("seed,expected", _KNOWN_VECTORS.items())
def test_generate_matches_canonical_vector(seed, expected):
    # Arrange
    mt = MersenneTwister(seed)

    # Act
    produced = [mt.generate() for _ in range(len(expected))]

    # Assert
    assert produced == expected


@pytest.mark.unit
def test_default_seed_is_0x1105_when_uninitialized():
    # C# Generate() lazily seeds 0x1105 when Seed() was never called.
    # Arrange
    mt = MersenneTwister()

    # Act
    produced = [mt.generate() for _ in range(5)]

    # Assert
    assert produced == _KNOWN_VECTORS[0x1105]
    assert mt.last_seed == 0x1105


@pytest.mark.unit
def test_seed_resets_stream_and_diagnostics():
    # Arrange
    mt = MersenneTwister(5489)
    mt.generate()
    mt.generate()

    # Act
    mt.seed(0xDEADBEEF)

    # Assert — fresh stream, counters reset.
    assert mt.last_seed == 0xDEADBEEF
    assert mt.calls_since_reseed == 0
    assert [mt.generate() for _ in range(5)] == _KNOWN_VECTORS[0xDEADBEEF]


@pytest.mark.unit
def test_calls_since_reseed_tracks_position():
    # C# ClientEventReplaySelfTest T1 — fast-forward lands at the target position.
    # Arrange
    mt = MersenneTwister(0x1A2B3C4D)

    # Act
    for _ in range(137):
        mt.generate()

    # Assert
    assert mt.calls_since_reseed == 137


@pytest.mark.unit
def test_last_generated_value_records_last_output():
    # Arrange
    mt = MersenneTwister(5489)

    # Act
    value = mt.generate()

    # Assert
    assert mt.last_generated_value == value == _KNOWN_VECTORS[5489][0]


@pytest.mark.unit
def test_position_determinism_replay_guarantee():
    # C# ClientEventReplaySelfTest T2 — same seed + same position => identical next value.
    # This is THE guarantee the native damage replay relies on.
    # Arrange
    a = MersenneTwister(0x1A2B3C4D)
    b = MersenneTwister(0x1A2B3C4D)
    for _ in range(250):
        a.generate()
        b.generate()

    # Act / Assert
    assert a.generate() == b.generate() == 1638286145


@pytest.mark.unit
def test_mirrors_are_independent():
    # C# ClientEventReplaySelfTest T3 — two mirrors at different positions don't share state.
    # Arrange
    p1 = MersenneTwister(0x1A2B3C4D)
    p2 = MersenneTwister(0x1A2B3C4D)
    for _ in range(10):
        p1.generate()
    for _ in range(20):
        p2.generate()

    # Assert
    assert p1.calls_since_reseed == 10
    assert p2.calls_since_reseed == 20


@pytest.mark.unit
def test_incremental_and_fastforward_streams_match():
    # C# ClientEventReplaySelfTest T4 — replay (fast-forward) == live (incremental).
    # Arrange
    inc = MersenneTwister(0x1A2B3C4D)
    ff = MersenneTwister(0x1A2B3C4D)
    for _ in range(99):
        inc.generate()
        ff.generate()

    # Act / Assert
    for _ in range(3):
        assert inc.generate() == ff.generate()


@pytest.mark.unit
def test_generate_range_is_inclusive_and_in_bounds():
    # C# Generate(uint min, uint max): (Generate() % (max-min+1)) + min, inclusive.
    # Arrange
    mt = MersenneTwister(5489)

    # Act
    values = [mt.generate_range(10, 20) for _ in range(500)]

    # Assert
    assert all(10 <= v <= 20 for v in values)
    assert min(values) == 10
    assert max(values) == 20


@pytest.mark.unit
def test_generate_range_matches_modulo_formula():
    # Arrange — exact C# formula against the known first output for seed 5489.
    mt = MersenneTwister(5489)
    expected_raw = _KNOWN_VECTORS[5489][0]

    # Act
    result = mt.generate_range(100, 199)

    # Assert
    assert result == (expected_raw % 100) + 100


@pytest.mark.unit
def test_generate_range_zero_width_returns_min():
    # C#: range==0 (min==max) returns min without consuming the modulo branch result.
    # Arrange
    mt = MersenneTwister(5489)

    # Act / Assert
    assert mt.generate_range(42, 42) == 42


@pytest.mark.unit
def test_generate_int_inverted_bounds_returns_min():
    # C# GenerateInt: max < min => return min.
    # Arrange
    mt = MersenneTwister(5489)

    # Act / Assert
    assert mt.generate_int(50, 10) == 50


@pytest.mark.unit
def test_generate_int_handles_negative_range():
    # Arrange
    mt = MersenneTwister(5489)

    # Act
    values = [mt.generate_int(-5, 5) for _ in range(500)]

    # Assert
    assert all(-5 <= v <= 5 for v in values)
    assert min(values) == -5
    assert max(values) == 5


@pytest.mark.unit
def test_genrand_int32_alias_shares_stream():
    # Back-compat: WanderSimulator calls genrand_int32(); it must be the same stream as generate().
    # Arrange
    mt = MersenneTwister(5489)

    # Act / Assert
    assert mt.genrand_int32() == _KNOWN_VECTORS[5489][0]
    assert mt.generate() == _KNOWN_VECTORS[5489][1]
    assert mt.calls_since_reseed == 2
