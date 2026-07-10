"""Native weapon DamageComputer parity tests — ROUTE 2B step 3.

Pins the concrete numeric anchors embedded in C# Combat/NativeDamageReplaySelfTest.cs:
  - LatestPup50024: seed 0x8D801C2B, 156 pre-hit consumes -> ServerDamageWire 3180
  - LatestRatling50000: damageRaw 0x30FF6AD4 -> FirstSuffixHPWire 24002 (29184 - 5182)
Both are end-to-end RNG -> damage oracles, so they validate the MT19937 stream,
the hit/block/damage roll order, ComputeNativeWeaponDamageRange, and RollDamageRange
together against values the C# server actually logged.
"""
from __future__ import annotations

import pytest

from drserver.combat.damage_computer import (
    AttackResultType,
    NativeWeaponDamageInput,
    compute_native_weapon_damage_range,
    fixed_mul,
    from_int,
    native_fixed32_from_authored_decimal,
    resolve_native_hit_threshold,
    resolve_native_weapon_damage,
    roll_damage_range,
    roll_spell_damage_range,
    round_fixed32,
    to_int,
)
from drserver.combat.rng import MersenneTwister

# NativeDamageReplaySelfTest constants.
PUP_SEED = 0x8D801C2B
PUP_PRE_HIT_CONSUMES = 156
PUP_SERVER_DAMAGE_WIRE = 3180
PUP_START_HP_WIRE = 29184
RATLING_DAMAGE_RAW = 0x30FF6AD4
RATLING_START_HP_WIRE = 29184
RATLING_FIRST_SUFFIX_HP_WIRE = 24002


def _pup_input(rng: MersenneTwister) -> NativeWeaponDamageInput:
    return NativeWeaponDamageInput(
        rng=rng, attacker_level=3, defender_level=2,
        attack_rating=210, defense_rating=52, block_chance=0,
        damage_level=3, damage_bonus=31, damage_mod=100,
        weapon_damage_f32=139, weapon_volatility_f32=85,
        crit_threshold=2048, crit_damage_percent=200,
    )


# ── Fixed32 helpers ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_fixed32_helpers():
    assert from_int(5) == 0x500
    assert to_int(0x500) == 5
    assert fixed_mul(0x100, 0x100) == 0x100   # 1.0 * 1.0
    assert fixed_mul(0x200, 0x80) == 0x100    # 2.0 * 0.5


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [
    (0x300, 0x300),       # already integral
    (0x37F, 0x400),       # frac 0x7F > 0x7E -> round up
    (0x37E, 0x300),       # frac 0x7E -> round down
])
def test_round_fixed32(value, expected):
    assert round_fixed32(value) == expected


@pytest.mark.unit
def test_native_fixed32_from_authored_decimal_ceils():
    # NativeFixed32FromAuthoredDecimal uses CeilToInt.
    assert native_fixed32_from_authored_decimal(0.54) == 139  # ceil(138.24)
    assert native_fixed32_from_authored_decimal(0.33) == 85   # ceil(84.48)
    assert native_fixed32_from_authored_decimal(1.0) == 256


# ── Hit threshold ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_hit_threshold_pup_anchor():
    # AR=210, DR=52 -> 80% ; (L2 defender - L3 attacker) = -1 -> +0x500.
    assert resolve_native_hit_threshold(210, 52, 3, 2) == 21760


@pytest.mark.unit
def test_hit_threshold_floor_and_level_penalty():
    # Low attacker AR vs strong defender, attacker far below defender -> 10% floor.
    assert resolve_native_hit_threshold(1, 9999, 1, 50) == 0x0A00
    # Zero AR and DR -> 0% -> floored.
    assert resolve_native_hit_threshold(0, 0, 1, 1) == 0x0A00


# ── Damage range ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_compute_native_weapon_damage_range_pup_anchor():
    # DamageLevel=3, DamageBonus=31, DamageMod=100, weapon 139, volatility 85.
    assert compute_native_weapon_damage_range(3, 31, 100, 139, 85) == (3072, 6400)


@pytest.mark.unit
def test_roll_damage_range_basic_and_zero_width():
    assert roll_damage_range(3072, 6400, 0) == 3072
    assert 3072 <= roll_damage_range(3072, 6400, 12345) <= 6400
    assert roll_damage_range(512, 512, 9999) == 512  # zero width -> min


@pytest.mark.unit
def test_roll_spell_damage_range_hp_units():
    # min 3 hp .. max 6 hp wire; raw picks an hp in [3,6), result << 8, floored at 0x100.
    result = roll_spell_damage_range(3 << 8, 6 << 8, 0)
    assert result == (3 << 8)
    assert roll_spell_damage_range(0, 0, 5) == 0x100  # clamps min hp to 1 -> 0x100


# ── End-to-end replay oracles ───────────────────────────────────────────────

@pytest.mark.unit
def test_pup_replay_reproduces_server_damage_wire():
    rng = MersenneTwister(PUP_SEED)
    for _ in range(PUP_PRE_HIT_CONSUMES):
        rng.generate()

    result = resolve_native_weapon_damage(_pup_input(rng))

    assert result.is_hit
    assert not result.is_blocked
    assert not result.is_critical
    assert result.min_damage_f32 == 3072
    assert result.max_damage_f32 == 6400
    assert result.damage_wire == PUP_SERVER_DAMAGE_WIRE
    assert result.room_rng_after == PUP_PRE_HIT_CONSUMES + 3  # 3 room RNG on a hit


@pytest.mark.unit
def test_ratling_range_roll_reproduces_first_suffix_hp():
    dmg_f32 = native_fixed32_from_authored_decimal(0.54)
    vol_f32 = native_fixed32_from_authored_decimal(0.33)
    min_d, max_d = compute_native_weapon_damage_range(3, 31, 100, dmg_f32, vol_f32)
    damage_wire = roll_damage_range(min_d, max_d, RATLING_DAMAGE_RAW)

    assert damage_wire == 5182
    assert RATLING_START_HP_WIRE - damage_wire == RATLING_FIRST_SUFFIX_HP_WIRE


@pytest.mark.unit
def test_replay_is_deterministic():
    a = resolve_native_weapon_damage(_pup_input(_advanced(PUP_SEED, PUP_PRE_HIT_CONSUMES)))
    b = resolve_native_weapon_damage(_pup_input(_advanced(PUP_SEED, PUP_PRE_HIT_CONSUMES)))
    assert a.damage_wire == b.damage_wire == PUP_SERVER_DAMAGE_WIRE
    assert a.hit_roll == b.hit_roll


# ── Roll-count + crit semantics ─────────────────────────────────────────────

@pytest.mark.unit
def test_miss_consumes_two_rng():
    # AR 0 vs huge DR, attacker far below defender -> 10% floor, likely miss.
    misses = 0
    for i in range(20):
        rng = MersenneTwister(0x20000 + i)
        inp = NativeWeaponDamageInput(rng=rng, attacker_level=1, defender_level=40,
                                      attack_rating=0, defense_rating=9999, block_chance=0,
                                      damage_level=1, damage_bonus=0, damage_mod=100,
                                      weapon_damage_f32=256, weapon_volatility_f32=128)
        result = resolve_native_weapon_damage(inp)
        if not result.is_hit:
            misses += 1
            assert result.room_rng_after == 2
            assert result.type == AttackResultType.MISS
    assert misses > 0


@pytest.mark.unit
def test_crit_doubles_base_damage():
    # Same RNG stream both runs (crit consumes no RNG); crit_threshold=25700 forces crit.
    base = resolve_native_weapon_damage(_crit_input(crit_threshold=0))
    crit = resolve_native_weapon_damage(_crit_input(crit_threshold=25700))

    assert base.is_hit and not base.is_critical
    assert crit.is_hit and crit.is_critical
    assert crit.damage_wire == max(0x100, (base.damage_wire * 200) // 100)


def _advanced(seed: int, n: int) -> MersenneTwister:
    rng = MersenneTwister(seed)
    for _ in range(n):
        rng.generate()
    return rng


def _crit_input(crit_threshold: int) -> NativeWeaponDamageInput:
    return NativeWeaponDamageInput(
        rng=MersenneTwister(0xC0FFEE11), attacker_level=10, defender_level=1,
        attack_rating=9999, defense_rating=1, block_chance=0,
        damage_level=5, damage_bonus=20, damage_mod=100,
        weapon_damage_f32=256, weapon_volatility_f32=128,
        crit_threshold=crit_threshold, crit_damage_percent=200,
    )
