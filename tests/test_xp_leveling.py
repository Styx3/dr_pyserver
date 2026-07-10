"""Server-authoritative XP / level-up — port of C# Networking/PlayerState.cs.

The vanilla client self-levels LOCALLY on each kill (combat is client-authoritative):
it awards itself XP, crosses the level threshold, recomputes its avatar HP, and emits
only a bare ``0x1C`` confirmation. The live crash 2026-06-01 proved this — the client
reached ``Avatar Styx3 - Level 2`` while the server still sent the L1 ``hp_wire`` (68096),
so the ``0x02`` avatar synch trailer mismatched (Local 72192 vs Remote 68096) and the
client fatally crashed.

The server therefore must mirror the client's EXACT XP math so its level tracks the
client's in lockstep, then refresh ``conn.hp_wire`` so the per-tick ``0x36`` and every
``0x02`` trailer carry the leveled-up value. No XP packet is sent: the client already
self-levels, and sending one would double-count (client applies ExperienceMod=5) and
out-pace the server.

Curve is reverse-engineered from the client binary (DR-Server PlayerState.cs):
  * GetClientThreshold (HeroDesc::getRequiredExp @0x4FAF60): MULTIPLIER=100 →
    L2=1000, L3=2500, L4=4500, L5=6500.
  * GetXPPerKill (0x4F8409 / 0x42BFF0): ~500 XP/kill within 5 levels, 0 if the mob is
    5+ levels below the player.
  * AddExperience: accumulate, level while Experience >= threshold(level+1), deducting
    the threshold each level and refilling HP/MP.
"""
import pytest

from drserver.data.player_state import (
    apply_xp,
    compute_avatar_max_hp_wire,
    xp_per_kill,
    xp_threshold_for_level,
)


# ── Level thresholds (client-RE'd, MULTIPLIER=100) ─────────────────────────────
@pytest.mark.unit
def test_threshold_matches_native_keyframes():
    # DR-Server comment: "Matches native thresholds: L2=1000, L3=2500, L4=4500, L5=6500"
    assert xp_threshold_for_level(2) == 1000
    assert xp_threshold_for_level(3) == 2500
    assert xp_threshold_for_level(4) == 4500
    assert xp_threshold_for_level(5) == 6500


@pytest.mark.unit
def test_threshold_interpolates_above_level_5():
    # Between keyframes (5,65) and (100,5000): strictly increasing, ×100 scaled.
    t6 = xp_threshold_for_level(6)
    assert t6 > 6500           # past the L5 keyframe
    assert t6 == 11600         # exact integer result of the Fixed8.8/16.16 port


# ── XP per kill ────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_xp_per_kill_same_level_is_500():
    assert xp_per_kill(1, 1) == 500
    assert xp_per_kill(2, 2) == 500
    assert xp_per_kill(50, 50) == 500


@pytest.mark.unit
def test_xp_per_kill_recruit_mob_for_level_1_player():
    # dungeon00_level01 RECRUIT mob = level 2, player L1 → effectiveLevel=min(2,1)=1 → 500.
    assert xp_per_kill(2, 1) == 500


@pytest.mark.unit
def test_xp_per_kill_zero_when_mob_five_levels_below():
    # Boundary is inclusive: monster_level <= player_level - 5 → 0.
    assert xp_per_kill(6, 10) == 298   # within range (6 > 10-5), scaled down
    assert xp_per_kill(5, 10) == 0     # exactly 5 below → no XP
    assert xp_per_kill(4, 10) == 0
    assert xp_per_kill(1, 10) == 0


@pytest.mark.unit
def test_xp_per_kill_never_below_one_when_in_range():
    # The C# clamp: xp < 1 → 1 (never returns 0 for an in-range kill).
    assert xp_per_kill(6, 10) >= 1


# ── AddExperience / level-up loop (deduct model) ───────────────────────────────
@pytest.mark.unit
def test_one_kill_does_not_level():
    # 500 XP < 1000 threshold → still L1, no level.
    assert apply_xp(1, 0, 500) == (1, 500, False)


@pytest.mark.unit
def test_two_kills_reach_level_2():
    # 500 + 500 = 1000 = L2 threshold → L2 with the threshold deducted.
    level, exp, did = apply_xp(1, 500, 500)
    assert (level, exp, did) == (2, 0, True)


@pytest.mark.unit
def test_overshoot_carries_remainder():
    # 1500 XP from L1: cross L2 (−1000), 500 remains, not enough for L3 (2500).
    assert apply_xp(1, 0, 1500) == (2, 500, True)


@pytest.mark.unit
def test_multi_level_in_one_grant():
    # 3500 from L1: −1000 → L2 (2500 left), −2500 → L3 (0 left), L4 needs 4500.
    assert apply_xp(1, 0, 3500) == (3, 0, True)


@pytest.mark.unit
def test_max_level_does_not_overflow():
    level, exp, did = apply_xp(100, 0, 10_000_000)
    assert level == 100
    assert did is False


# ── End-to-end: HP wire tracks the level after enough kills ────────────────────
@pytest.mark.unit
def test_hp_wire_tracks_level_after_kills():
    # Two L2-mob kills take a L1 player to L2; hp_wire must follow (the synch fix).
    level, _exp, did = apply_xp(1, 0, xp_per_kill(2, 1) + xp_per_kill(2, 1))
    assert did is True and level == 2
    assert compute_avatar_max_hp_wire(level) == 72192   # 68096 + 1*4096 (live crash value)
