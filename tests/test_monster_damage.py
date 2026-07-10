"""MonsterCurves + MonsterDamageComputer parity tests — ROUTE 2B step 2.

Mirrors the pure (non-.gc-IO) checks in C# Combat/MonsterDamageComputerSelfTest.cs
plus the MonsterCurves anchor values documented in MonsterCurves.cs. The two
self-tests that load a .gc profile (TestProfileToStats, TestSampleMobSwing) need
the MonsterAttackData loader + full MonsterUnitStatsBuilder.Build and belong to a
later sub-step.
"""
from __future__ import annotations

import pytest

from drserver.combat.monster_curves import MonsterCurves
from drserver.combat.monster_damage import (
    MonsterUnitStats,
    PlayerUnitStats,
    compute_base_damage_mod,
    compute_reflected_damage,
    compute_swing,
    event_kind_from_attack_style,
    on_query_apply_damage,
)
from drserver.combat.rng import MersenneTwister


def _make_strong_attacker() -> MonsterUnitStats:
    return MonsterUnitStats(
        level=10, attack_style=1, weapon_damage_type=0, discriminator=0,
        base_attack_rating=9999, base_attack_rating_mod=0,
        base_damage_mod=100, base_critical_chance=6, crit_multiplier=150,
        weapon_damage_per_level=10, weapon_volatility_fixed=128, weapon_damage_fixed=256,
        damage_mod_scale=256,
    )


def _make_weak_target() -> PlayerUnitStats:
    return PlayerUnitStats(base_defense_rating=1, base_defense_rating_mod=0,
                           block_chance=0, discriminator=0)


# ── MonsterCurves anchors (from MonsterCurves.cs comments) ──────────────────

@pytest.mark.unit
def test_curve_base_ar_anchor_disc1():
    # AR=0.25 (Fixed32=64), MonsterAR curve at L1.0 = 25600: (64 x 25600) >> 16 = 25.
    assert MonsterCurves.compute_base_ar(0.25, 1) == 25


@pytest.mark.unit
def test_curve_base_dr_anchor_disc1():
    # DR=0.25 (Fixed32=64), MonsterDR curve at L1.0 = 8960: (64 x 8960) >> 16 = 8.
    assert MonsterCurves.compute_base_dr(0.25, 1) == 8


@pytest.mark.unit
def test_curve_base_ar_pup_rank1_anchor():
    # Section 10c x32dbg capture: pup rank1 AR=0.15 -> baseAR 59 at disc=2.
    # (round(0.15*256)=38) x curve(disc=2) >> 16 == 59.
    assert MonsterCurves.compute_base_ar(0.15, 2) == 59


@pytest.mark.unit
def test_curve_interp_below_and_above_clamps():
    # Below first anchor clamps to first value, above last clamps to last.
    assert MonsterCurves.compute_base_ar(1.0, 0) == (256 * 25600) >> 16  # disc 0 -> L0 < L1 anchor
    assert MonsterCurves.compute_base_dr(1.0, 200) == (256 * (3087 * 256)) >> 16  # disc 200 > L110


# ── ComputeBaseDamageMod (UnitDesc::getDamageMod) ───────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("authored,expected", [
    (1.0, 0),     # baseline
    (2.0, 0),     # client special-case
    (1.10, 10),   # GruntVeteran
    (2.20, 119),  # Champion/Hero
    (0.5, -50),   # pup runtime (post-Tim halving)
    (0.25, -75),  # Abba_Labba_Melee_Hero
    (5.0, 400),   # amazon_gatekeeper / boss2
    (0.10, -90),  # AbaddonCannonProc
    (0.0, -100),  # max debuff
    (1.5, 50),
    (3.0, 200),
])
def test_compute_base_damage_mod(authored, expected):
    assert compute_base_damage_mod(authored) == expected


# ── ComputeSwing roll-count / determinism / discriminator ───────────────────

@pytest.mark.unit
def test_roll_count_on_hit_not_blocked_consumes_three():
    # Strong attacker vs weak target -> hits, not blocked -> 3 RNG calls.
    attacker = _make_strong_attacker()
    target = _make_weak_target()
    rng = MersenneTwister(0xDEADBEEF)

    start = rng.calls_since_reseed
    result = compute_swing(attacker, target, rng)
    consumed = rng.calls_since_reseed - start

    assert result.hit
    assert not result.blocked
    assert consumed == 3
    assert result.damage > 0


@pytest.mark.unit
def test_roll_count_on_miss_heavy_attacker():
    # 0 AR mob vs 9999 DR player -> ~10% floor; mostly miss, 2-3 calls each.
    attacker = MonsterUnitStats(
        level=1, attack_style=1, weapon_damage_type=0, discriminator=0,
        base_attack_rating=0, base_attack_rating_mod=-1000, crit_multiplier=100,
        weapon_damage_per_level=10, weapon_volatility_fixed=128, weapon_damage_fixed=256,
    )
    target = PlayerUnitStats(base_defense_rating=9999, base_defense_rating_mod=0,
                             discriminator=0, block_chance=0)

    total_rolls = 0
    misses = 0
    trials = 20
    for i in range(trials):
        rng = MersenneTwister(0x10000 + i)
        start = rng.calls_since_reseed
        result = compute_swing(attacker, target, rng)
        total_rolls += rng.calls_since_reseed - start
        if not result.hit:
            misses += 1

    assert misses > 0
    assert 40 <= total_rolls <= 60


@pytest.mark.unit
def test_discriminator_parity_raw_delta_x5():
    # mob disc=2, player disc=0, AR=100, DR=100 -> hitChance=50 ->
    # hitChanceScaled = 50*256 - (0-2)*5 = 12810 (old x0x100 bug gave 15360).
    attacker = MonsterUnitStats(
        level=1, attack_style=1, weapon_damage_type=0, discriminator=2,
        base_attack_rating=100, base_attack_rating_mod=0, base_critical_chance=0,
        crit_multiplier=200, weapon_damage_per_level=10, weapon_volatility_fixed=128,
        weapon_damage_fixed=256,
    )
    target = PlayerUnitStats(base_defense_rating=100, base_defense_rating_mod=0,
                             discriminator=0, block_chance=0)

    result = compute_swing(attacker, target, MersenneTwister(0x5A5A5A5A))
    assert result.hit_chance_scaled == 12810


@pytest.mark.unit
def test_determinism_same_seed_same_outcome():
    attacker = _make_strong_attacker()
    target = _make_weak_target()
    r1 = compute_swing(attacker, target, MersenneTwister(0xABCDEF12))
    r2 = compute_swing(attacker, target, MersenneTwister(0xABCDEF12))
    assert r1.r1_hit == r2.r1_hit
    assert r1.damage == r2.damage
    assert r1.hit == r2.hit
    assert r1.crit == r2.crit


# ── OnQueryApplyDamage (C6) ─────────────────────────────────────────────────

_BASE_DMG = 25600  # wire = 100 HP


@pytest.mark.unit
@pytest.mark.parametrize("target_kwargs,dmg_type,expected_dmg,expected_resisted", [
    ({"damage_taken_mod": 100}, 0, 25600, False),
    ({"damage_taken_mod": 50}, 0, 12800, False),
    ({"damage_taken_mod": 200}, 0, 51200, False),
    # physical types ignore elemental resist
    ({"fire_resist": 100, "ice_resist": 100}, 0, 25600, False),
    ({"fire_resist": 100, "ice_resist": 100}, 1, 25600, False),
    ({"fire_resist": 100, "ice_resist": 100}, 2, 25600, False),
    # fire (type 3)
    ({"fire_resist": 0}, 3, 25600, False),
    ({"fire_resist": 50}, 3, 12800, False),
    ({"fire_resist": 100}, 3, 0, True),
    ({"fire_resist": 200}, 3, 0, True),
    ({"fire_resist": 60}, 3, 10240, False),
    # other elemental slots
    ({"ice_resist": 100}, 4, 0, True),
    ({"poison_resist": 100}, 5, 0, True),
    ({"divine_resist": 100}, 6, 0, True),
    ({"shadow_resist": 100}, 7, 0, True),
    # combined multiplicative stack
    ({"damage_taken_mod": 50, "fire_resist": 50}, 3, 6400, False),
])
def test_on_query_apply_damage(target_kwargs, dmg_type, expected_dmg, expected_resisted):
    target = PlayerUnitStats(**target_kwargs)
    got, resisted = on_query_apply_damage(_BASE_DMG, dmg_type, target)
    assert got == expected_dmg
    assert resisted == expected_resisted


@pytest.mark.unit
def test_on_query_apply_damage_zero_and_null():
    got, resisted = on_query_apply_damage(0, 3, PlayerUnitStats(damage_taken_mod=200))
    assert got == 0 and not resisted
    got, resisted = on_query_apply_damage(_BASE_DMG, 3, None)
    assert got == _BASE_DMG and not resisted


# ── ComputeReflectedDamage (C7) ─────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("kind,base,melee,ranged,expected", [
    (0, 0, 0, 0, 0),
    (4, 999, 999, 999, 0),          # no recursion
    (0, 50, 999, 999, 12800),
    (3, 25, 999, 999, 6400),        # spell uses base only
    (1, 10, 20, 999, 7680),         # melee adds melee bonus
    (1, 0, 15, 999, 3840),
    (2, 10, 999, 30, 10240),        # ranged adds ranged bonus
    (2, 0, 999, 5, 1280),
    (1, 0, 0, 999, 0),              # melee ignores ranged bonus
    (2, 0, 999, 0, 0),              # ranged ignores melee bonus
    (0, -50, 0, 0, 0),              # negative clamped
    (1, 10, -30, 0, 0),             # net negative clamped
    (0, 200, 0, 0, 51200),          # >100%
])
def test_compute_reflected_damage(kind, base, melee, ranged, expected):
    assert compute_reflected_damage(_BASE_DMG, kind, base, melee, ranged) == expected


@pytest.mark.unit
def test_compute_reflected_damage_zero_incoming():
    assert compute_reflected_damage(0, 0, 100, 0, 0) == 0


@pytest.mark.unit
@pytest.mark.parametrize("style,expected_kind", [
    (1, 1), (5, 1), (6, 1), (8, 1),
    (3, 2), (9, 2), (13, 2),
    (0, 0), (99, 0),
])
def test_event_kind_from_attack_style(style, expected_kind):
    assert event_kind_from_attack_style(style) == expected_kind
