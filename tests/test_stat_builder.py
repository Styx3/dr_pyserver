"""Tests for the combat StatBlock builder + the client_swing replay bridge.

Anchored to the 2026-06-14 live capture (``docs/COMBAT_FORMULA.md`` §6b): a L2
Whisker Broodling (authored defense_rating 1.0) whose cached avoidance ``+0x12C``
read **52** on the running client. The builder must reproduce that bit-exact, and
the live-captured MISS (player acc 70 vs mob avoidance 52 → threshold 0x3900,
roll1 0x39A2 → miss) must reproduce end-to-end when the defender block comes from
the builder rather than a hand-written dict.

The player ATTACKER magnitude (``+0xF0`` AR, ``hi_flag``) is UNVERIFIED and is NOT
asserted as fact here — only the PROVEN defender, the proven constants, and the
draw-order/count are pinned.
"""
from __future__ import annotations

import types

from drserver.combat import stat_builder
from drserver.combat.client_swing import StatBlock, compute_swing
from drserver.combat.client_swing_resolver import resolve_client_swing
from drserver.combat.swing_stats import SwingProfile


# --- live anchors (COMBAT_FORMULA §6b / test_client_swing.py) ---------------------
_LIVE_DR = 52              # mob +0x12C, authored 1.0 @ disc/level 2
_LIVE_DISC = 2
_WEAPON = StatBlock({0x0EC: 154, 0x0F0: 64})   # the captured weapon
# captured player (not-melee path): acc 70, scale 256, +0x314=2 (matches the mob's
# disc so the range-adjust term cancels — threshold = hitPct*256 exactly).
_PLAYER_ACC = StatBlock({0x0F0: 70, 0x300: 256, 0x314: 2})


class _QueueMT:
    """MersenneTwister stand-in returning queued raw genrand values in order."""

    def __init__(self, *draws: int) -> None:
        self._draws = list(draws)

    def generate(self) -> int:
        return self._draws.pop(0)

    @property
    def remaining(self) -> int:
        return len(self._draws)


def _grunt_input() -> stat_builder.MonsterStatInput:
    # creatures.whiskers.broodling.basic.grunt: defense_rating ratio = 1.0, L2.
    return stat_builder.MonsterStatInput(
        level=2, discriminator=_LIVE_DISC, authored_defense_rating=1.0)


# --- monster defender block: the PROVEN tier --------------------------------------

def test_monster_defender_reproduces_live_dr():
    sb = stat_builder.monster_defender_statblock(_grunt_input())
    assert sb.i32(stat_builder.OFF_AVOIDANCE) == _LIVE_DR       # +0x12C == 52 (live)
    assert sb.i32(stat_builder.OFF_DISCRIMINATOR) == _LIVE_DISC  # +0x314 == 2
    assert sb.i32(stat_builder.OFF_BLOCK_CHANCE) == 0           # mobs = 0 (live)


def test_monster_defender_omits_unverified_attacker_offsets():
    # The defender role never reads AR/damage-mod; keep them unset so the block is
    # 100% PROVEN (no UNVERIFIED value can leak into a player→mob swing).
    sb = stat_builder.monster_defender_statblock(_grunt_input())
    assert sb.i32(stat_builder.OFF_ACCURACY) == 0
    assert sb.i32(stat_builder.OFF_DAMAGE_MOD) == 0


def test_builder_defender_reproduces_captured_miss():
    # End-to-end: the live MISS (player +0xF0=70 vs builder-built mob avoidance 52)
    # → threshold 0x3900 (57.0); the captured roll1=0x39A2 (14754) >= threshold → miss.
    defender = stat_builder.monster_defender_statblock(_grunt_input())
    result = compute_swing(
        _PLAYER_ACC, defender, _WEAPON, _QueueMT(0x39A2, 0),
        element=5, armor_class=0, melee_in_range=False,
    )
    assert result.hit is False
    assert result.draws == 2          # miss consumes exactly 2 MT draws


def test_builder_defender_hit_boundary():
    # threshold = 0x3900 = 14592 (acc 70 / def 52 → hitPct 57 → 57*256).
    defender = stat_builder.monster_defender_statblock(_grunt_input())

    def swing(roll1: int):
        return compute_swing(
            _PLAYER_ACC, defender, _WEAPON, _QueueMT(roll1, 0, 0x0A0499A0),
            element=5, armor_class=0, melee_in_range=False,
        )

    assert swing(14591).hit is True     # 14591 < 14592
    assert swing(14592).hit is False    # 14592 >= 14592


# --- proven constants on the attacker blocks --------------------------------------

def test_monster_attacker_carries_proven_constants():
    sb = stat_builder.monster_attacker_statblock(_grunt_input())
    assert sb.i32(stat_builder.OFF_CRIT_DAMAGE) == 200   # +0x118 (live)
    assert sb.i32(stat_builder.OFF_DAMAGE_SCALE) == 256  # +0x300 (live)
    # defender offsets still present (attacker block extends the defender block).
    assert sb.i32(stat_builder.OFF_AVOIDANCE) == _LIVE_DR


def test_monster_attacker_accuracy_reproduces_live_trace():
    # ★LIVE-TRACED 2026-06-14 (x64dbg): Whisker grunt (attack_rating 1.0) at L3 wrote
    # +0xF0 = 174 = (round(1.0×64) × MonsterAttackRating(3<<8)=179136) >> 16.
    sb = stat_builder.monster_attacker_statblock(
        stat_builder.MonsterStatInput(level=3, discriminator=3,
                                      authored_attack_rating=1.0, authored_defense_rating=1.0))
    assert sb.i32(stat_builder.OFF_ACCURACY) == 174       # +0xF0 (live byte written)


def test_player_weapon_block_maps_profile_fields():
    profile = _fake_profile(weapon_damage_f32=154, weapon_volatility_f32=64)
    sb = stat_builder.player_weapon_statblock(profile)
    assert sb.i32(stat_builder.OFF_WPN_VARIANCE) == 154   # +0xEC
    assert sb.i32(stat_builder.OFF_WPN_SPREAD) == 64       # +0xF0


def test_player_attacker_block_carries_proven_constants():
    sb = stat_builder.player_attacker_statblock(_fake_profile())
    assert sb.i32(stat_builder.OFF_CRIT_DAMAGE) == 200
    assert sb.i32(stat_builder.OFF_DAMAGE_SCALE) == 256
    assert sb.i32(stat_builder.OFF_CRIT_REDUCTION) == 100


def test_player_attacker_reproduces_live_capture():
    # Live Styx3 L2 (COMBAT_FORMULA §6b): element-5 melee, +0xF0=70, +0x180=11, +0xFC=0
    # — both from the allocated+passive (5/5) combat stats, NOT the 15/15 display stats.
    profile = _fake_profile(combat_attack_rating=70, combat_damage_bonus=11)
    sb = stat_builder.player_attacker_statblock(profile, element=5)
    assert sb.i32(stat_builder.OFF_ACCURACY) == 70        # +0xF0 (live)
    assert sb.i32(stat_builder.OFF_B30_MELEE) == 11        # +0x180 (live)
    assert sb.i32(stat_builder.OFF_B30_BASE) == 0          # +0xFC stays 0 (live)


def test_player_attacker_ranged_uses_ranged_b30_slot():
    profile = _fake_profile(combat_damage_bonus=11)
    sb = stat_builder.player_attacker_statblock(profile, element=3)  # ranged
    assert sb.i32(stat_builder.OFF_B30_RANGED) == 11
    assert sb.i32(stat_builder.OFF_B30_MELEE) == 0


# --- the replay bridge: draw-order / count is the sync-critical part --------------

def _fake_profile(**over) -> SwingProfile:
    base = dict(
        player_level=2, strength=15, agility=15, weapon_class_id=5, is_ranged=False,
        attack_rating=210, damage_bonus=31, damage_mod=100, damage_level=1,
        weapon_damage_f32=154, weapon_volatility_f32=64,
        weapon_gc="", source="test",
        combat_attack_rating=70, combat_damage_bonus=11,
    )
    base.update(over)
    return SwingProfile(**base)


def _cycle():
    # char_sql_id 0 → resolve_swing_profile returns the known _FALLBACK starter
    # anchor (attack_rating 210, weapon_class 5) → no DB, deterministic threshold.
    # NB: register_swing populates cycle.connection (NOT cycle.player_state) — mirror
    # that exactly so the test exercises the real field the resolver reads.
    conn = types.SimpleNamespace(login_name="Tester", char_sql_id=0)
    monster = types.SimpleNamespace(level=2, defense_rating=1.0, entity_id=2563)
    return types.SimpleNamespace(connection=conn, player_state=None, monster=monster)


def test_bridge_hit_consumes_three_draws():
    # _FALLBACK acc 210 / builder def 52 → hitPct 80 → threshold 0x5000 (20480);
    # roll1=0 → hit, block_chance 0 → not blocked.
    mt = _QueueMT(0, 0, 0x0A0499A0)
    hit, blocked, dmg = resolve_client_swing(_cycle(), mt)
    assert hit is True
    assert blocked is False
    assert mt.remaining == 0           # exactly 3 draws consumed (hit)
    assert dmg > 0


def test_bridge_miss_consumes_two_draws():
    mt = _QueueMT(25000, 0, 0x0A0499A0)   # 25000 >= 20480 → miss
    hit, blocked, dmg = resolve_client_swing(_cycle(), mt)
    assert hit is False
    assert dmg == 0
    assert mt.remaining == 1           # only 2 draws consumed (variance not reached)


def test_bridge_inert_without_profile_or_rng():
    assert resolve_client_swing(_cycle(), None) == (False, False, 0)
    bad = types.SimpleNamespace(player_state=None, monster=None)
    assert resolve_client_swing(bad, _QueueMT()) == (False, False, 0)
