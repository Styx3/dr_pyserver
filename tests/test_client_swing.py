"""Bit-exact regression tests for the client combat damage chain (client_swing.py).

Pinned to a SINGLE live x64dbg capture (2026-06-14, PID 3788, ``docs/COMBAT_FORMULA.md``):
a player (Styx3) element-5 melee swing on 'Whisker Broodling'. Every value below was
read off the running client at a verified instruction boundary, then this port was
fixed until it reproduced them (the "test after proven" rule — these are not guesses):

  resolver inputs   element=5, armor_class=0, weapon[+0xec]=154, weapon[+0xf0]=64,
                    hi_flag(event[6])=10, accuracy_target_term=0, extra_c10=0
  attacker stats    +0x180=11 (b30 element term), +0x300=256 (=1.0 scalar),
                    +0x104=3 (a30 base), +0xf0=70 (acc) — every other offset 0
  intermediates     mit (c10)=100, in_EAX (b30)=11, lo=0x900 (9.0), hi=0x1000 (16.0)
  variance draw     raw genrand=0x0A0499A0, range=0x701 → applied damage=0x0E76 (3702, 14.46),
                    crit flag=0 (no crit)

Still synthetic here (NOT yet live-validated): the hit/miss threshold and block gate.
The integration test forces a clean hit so it asserts only the proven damage magnitude.
"""
from __future__ import annotations

from drserver.combat.client_swing import (
    StatBlock,
    _mitigation_00598c10,
    _sum_00598b30,
    _variance_range_00598ed0,
    compute_swing,
)

# --- live-captured stat blocks ---------------------------------------------------

# Attacker CombatStats (piVar8 @0x26898510), only the non-zero offsets that matter.
_ATTACKER = StatBlock({0x0F0: 70, 0x104: 3, 0x180: 11, 0x300: 256})
# Weapon (event[1] @0x0D3898F0): variance scalar + spread factor.
_WEAPON = StatBlock({0x0EC: 154, 0x0F0: 64})

_ELEMENT = 5
_ARMOR_CLASS = 0
_HI_FLAG = 10                  # event[6] low u16
_RAW_DRAW3 = 0x0A0499A0        # genrand return at 0x599016
_APPLIED_DAMAGE = 0x0E76       # Damage::apply +0x38 (3702 = 14.46 drfloat)


class _QueueMT:
    """Minimal MersenneTwister stand-in: returns queued raw genrand values in order."""

    def __init__(self, *draws: int) -> None:
        self._draws = list(draws)

    def generate(self) -> int:
        return self._draws.pop(0)


def test_mitigation_c10_double_shift():
    # ((0x6400 * 256) >> 8) >> 8 == 100. A single >>8 would give 25600.
    mit = _mitigation_00598c10(_ATTACKER, _ELEMENT, _ARMOR_CLASS, 0) & 0xFFFF
    assert mit == 100


def test_b30_returns_variance_power_term():
    # element 5, armor 0: attacker[+0xfc] + attacker[+0x1c8] + attacker[+0x180] + attacker[+0x234]
    #                   = 0 + 0 + 11 + 0 = 11  (== the client's in_EAX into ed0)
    assert _sum_00598b30(_ATTACKER, _ELEMENT, _ARMOR_CLASS) == 11


def test_variance_range_reproduces_client_lo_hi():
    lo, hi = _variance_range_00598ed0(100, 11, _WEAPON, _HI_FLAG)
    assert (lo, hi) == (0x900, 0x1000)


def test_variance_draw_reproduces_applied_damage():
    # range = ((hi>>8)<<8) - lo + 1 = 0x701; (raw % range) + lo = 3702 = 0x0E76.
    lo, hi = 0x900, 0x1000
    rng = ((hi >> 8) << 8) - lo + 1
    assert rng == 0x701
    assert (_RAW_DRAW3 % rng) + lo == _APPLIED_DAMAGE


# --- hit/miss threshold: a second live capture (2026-06-14, a MISS) -------------
# Same player/weapon, vs a mob with avoidance. Client computed threshold=0x3900 (57.0),
# rolled roll1=0x39A2 (14754) >= threshold -> MISS. block_chance(+0x138)=0.
#   attacker: +0xf0=70 (acc), +0x314=2 (block input)   ; element-5 acc terms all 0
#   defender: +0x12c=52 (avoidance base), +0x314=2     ; +0x18c/+0x190/+0x138 = 0
# Python: acc=70, defence=52 -> hit_pct=7000/122=57; range_adj=(2-2)*...=0 -> 57*256=0x3900.
_ATK_HM = StatBlock({0x0F0: 70, 0x104: 3, 0x180: 11, 0x300: 256, 0x314: 2})
_DEF_HM = StatBlock({0x12C: 52, 0x314: 2})


def _hm_swing(*draws: int):
    return compute_swing(
        _ATK_HM, _DEF_HM, _WEAPON, _QueueMT(*draws),
        element=_ELEMENT, armor_class=_ARMOR_CLASS, melee_in_range=False,
    )


def test_hit_threshold_matches_client_boundary():
    # threshold = 0x3900 = 14592. Pin it exactly via the hit/miss boundary.
    assert _hm_swing(14591, 0, _RAW_DRAW3).hit is True    # 14591 < 14592 -> hit
    assert _hm_swing(14592, 0).hit is False               # 14592 >= 14592 -> miss


def test_captured_miss_reproduces():
    # The actual live roll1=0x39A2 (14754) >= 0x3900 -> miss, 2 MT draws consumed.
    result = _hm_swing(0x39A2, 0)
    assert result.hit is False
    assert result.draws == 2


def test_compute_swing_reproduces_live_damage():
    # Force a clean hit (draw#1=1000 → roll1 in [a30 base, threshold) so it hits without
    # critting; draw#2=0 → not blocked) and assert the LIVE-CAPTURED applied damage.
    mt = _QueueMT(1000, 0, _RAW_DRAW3)
    result = compute_swing(
        _ATTACKER,
        StatBlock(),          # defender: zero avoidance/block → guaranteed hit
        _WEAPON,
        mt,
        element=_ELEMENT,
        armor_class=_ARMOR_CLASS,
        melee_in_range=False,
        crit_extra=_HI_FLAG,
    )
    assert result.hit is True
    assert result.blocked is False
    assert result.crit is False
    assert result.damage_wire == _APPLIED_DAMAGE
    assert result.draws == 3


# --- element-1 (mob->player physical) live capture (2026-06-15, PID 15184) ------
# The avatar-desync path (bible §6-LIVE.5 / COMBAT_FORMULA §6i). A Warg mob (eid 558)
# hit the avatar (eid 0x1FE) for element-1 physical damage. Captured at Damage::apply
# (0x4F6580) + stepped through c10/b30/ed0/draw. Proves element-1 uses the SAME
# magnitude formula as element-5 — only the (zero-here) defender resist offset switches.
#   attacker (mob 558): +0xf0=60 (AR), +0x100=-50 (DMG_MOD, signed), +0x300=256
#   weapon:             +0xec=256 (Wv=1.0), +0xf0=128 (Ws=0.5)
#   resolver inputs:    element=1, armor_class=0, hi_flag(event[6])=15
#   intermediates:      mit (c10)=50 (100 + DMG_MOD(-50)), b30=0, lo=0x400, hi=0xB00
#   variance draw:      raw genrand=0xF4507E00, range=0x701 -> applied=0x90F (2319, 9.06)
_ATK_E1 = StatBlock({0x0F0: 60, 0x100: -50, 0x300: 256})
_WEAPON_E1 = StatBlock({0x0EC: 256, 0x0F0: 128})
_ELEMENT_1 = 1
_HI_FLAG_E1 = 15
_RAW_DRAW_E1 = 0xF4507E00
_APPLIED_E1 = 0x90F


def test_element1_mitigation_applies_negative_damage_mod():
    # mob DMG_MOD -50 -> mit = clamp(100 + (-50), >=0) = 50 (halves damage).
    mit = _mitigation_00598c10(_ATK_E1, _ELEMENT_1, _ARMOR_CLASS, 0) & 0xFFFF
    assert mit == 50


def test_element1_b30_is_zero():
    assert _sum_00598b30(_ATK_E1, _ELEMENT_1, _ARMOR_CLASS) == 0


def test_element1_variance_range_reproduces_client_lo_hi():
    lo, hi = _variance_range_00598ed0(50, 0, _WEAPON_E1, _HI_FLAG_E1)
    assert (lo, hi) == (0x400, 0xB00)


def test_element1_compute_swing_reproduces_live_damage():
    # Force a clean hit (draw#1=1000 < threshold, no crit; draw#2=0 not blocked) vs a
    # zero-avoidance defender, and assert the LIVE-CAPTURED applied damage.
    mt = _QueueMT(1000, 0, _RAW_DRAW_E1)
    result = compute_swing(
        _ATK_E1,
        StatBlock(),
        _WEAPON_E1,
        mt,
        element=_ELEMENT_1,
        armor_class=_ARMOR_CLASS,
        melee_in_range=False,
        crit_extra=_HI_FLAG_E1,
    )
    assert result.hit is True
    assert result.blocked is False
    assert result.crit is False
    assert result.damage_wire == _APPLIED_E1
    assert result.draws == 3
