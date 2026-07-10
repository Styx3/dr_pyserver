"""Tests for the AttributeModifier aggregator + its CombatStats wiring.

Anchored to the PROVEN map (``docs/COMBAT_FORMULA.md`` §8): a unit's CombatStats is
a flat int[] at ``+0xC8`` indexed by the ``Attribute`` enum, ``offset = 0xC8 + enum×4``.
These tests pin the offset map (the five certain anchors), the passive delta sums
(from ``extracter/skills/generic/*Passive.gc``), and that a Fighter's #109
``MELEE_ATTACK_RATING_MOD +100`` folds into ``+0x178`` and doubles swing accuracy.
"""
from __future__ import annotations

import types

from drserver.combat import modifier_aggregator as ma
from drserver.combat import stat_builder
from drserver.combat.client_swing import StatBlock
from drserver.combat.client_swing import _acc_bonus_005989c0
from drserver.combat.swing_stats import SwingProfile


def _saved(*skill_gc: str):
    """Minimal SavedCharacter stand-in: a skills list + level-1 getter."""
    return types.SimpleNamespace(
        skills=list(skill_gc),
        hotbar_slots=[],
        get_skill_level=lambda s: 1,
    )


# --- the offset map (offset = 0xC8 + enum×4): the five certain anchors -------------

def test_offset_for_certain_anchors():
    assert ma.offset_for(ma.ATTACK_RATING_MOD) == 0xF4
    assert ma.offset_for(ma.DAMAGE_MOD) == 0x100
    assert ma.offset_for(ma.CRITICAL_DAMAGE_MOD) == 0x118
    assert ma.offset_for(ma.DEFENSE_RATING) == 0x12C
    assert ma.offset_for(ma.BLOCK) == 0x138
    # element block — these are what the §5 formula reads
    assert ma.offset_for(ma.MELEE_ATTACK_RATING_MOD) == 0x178
    assert ma.offset_for(ma.MELEE_DAMAGE_BONUS) == 0x180
    assert ma.offset_for(ma.MELEE1H_ATTACK_RATING_MOD) == 0x1C0


# --- passive delta sums -----------------------------------------------------------

def test_fighter_passives_aggregate():
    """Fighter carries #108 (Gym Freak) + #109 (Way of the Roo)."""
    agg = ma.aggregate_combat_modifiers(
        _saved("skills.generic.FighterClassPassive",
               "skills.generic.MeleeAttackSpeedModPassive"))
    # #109 — the headline missing mod
    assert agg[ma.MELEE_ATTACK_RATING_MOD] == 100
    assert agg[ma.MELEE_ATTACK_SPEED_MOD] == 25
    # #108 primary-stat + derived-formula mods
    assert agg[ma.STRENGTH] == 5
    assert agg[ma.AGILITY] == 5
    assert agg[ma.HEALTH_PER_ENDURANCE_MOD] == 50
    # both passives reduce magic damage (-5 each) and range attack speed
    assert agg[ma.MAGIC_DAMAGE_MOD] == -5
    assert agg[ma.RANGE_ATTACK_SPEED_MOD] == -10 + -6.25


def test_mage_passives_aggregate():
    agg = ma.aggregate_combat_modifiers(
        _saved("skills.generic.MageClassPassive",
               "skills.generic.MagicDamageModPassive"))
    assert agg[ma.MAGIC_DAMAGE_MOD] == 20
    assert agg[ma.INTELLECT] == 5
    assert agg[ma.MANA_PER_INTELLECT_MOD] == 100


def test_no_passives_is_empty():
    assert ma.aggregate_combat_modifiers(None) == {}
    assert ma.aggregate_combat_modifiers(_saved()) == {}


# --- wiring into the attacker StatBlock --------------------------------------------

def _profile(**kw):
    base = dict(player_level=2, strength=15, agility=15, weapon_class_id=5,
                is_ranged=False, attack_rating=70, damage_bonus=11, damage_mod=100,
                damage_level=10, weapon_damage_f32=154, weapon_volatility_f32=64,
                weapon_gc="", source="test", combat_attack_rating=70,
                combat_damage_bonus=11)
    base.update(kw)
    return SwingProfile(**base)


def test_modifiers_fold_into_statblock():
    agg = {ma.MELEE_ATTACK_RATING_MOD: 100, ma.STRENGTH: 5}
    sb = stat_builder.player_attacker_statblock(_profile(), element=5, discriminator=2,
                                                modifiers=agg)
    # MELEE_ATTACK_RATING_MOD lands in +0x178; STRENGTH (primary) is NOT written raw
    assert sb.i32(0x178) == 100
    assert sb.i32(ma.offset_for(ma.STRENGTH)) == 0   # skipped — folded via derivation
    # base accuracy unchanged
    assert sb.i32(0x0F0) == 70


def test_modifiers_double_melee_accuracy():
    """The end-to-end effect: +100% MAR doubles the element-5 accBonus term."""
    plain = stat_builder.player_attacker_statblock(_profile(), element=5, discriminator=2)
    buffed = stat_builder.player_attacker_statblock(
        _profile(), element=5, discriminator=2,
        modifiers={ma.MELEE_ATTACK_RATING_MOD: 100})
    # accBonus(element 5) = +0x1C0 + +0x178 (COMBAT_FORMULA §5)
    assert _acc_bonus_005989c0(plain, 5) == 0
    assert _acc_bonus_005989c0(buffed, 5) == 100


def test_none_modifiers_is_backward_compatible():
    a = stat_builder.player_attacker_statblock(_profile(), element=5, discriminator=2)
    b = stat_builder.player_attacker_statblock(_profile(), element=5, discriminator=2,
                                               modifiers=None)
    assert a._f == b._f


# --- the full Attribute name→enum table (binary 0x86c7d0) -------------------------

def test_attribute_name_to_enum_anchors():
    m = ma.ATTRIBUTE_NAME_TO_ENUM
    assert m["STRENGTH"] == 1
    assert m["ATTACK_RATING_MOD"] == 11
    assert m["DAMAGE_MOD"] == 14
    assert m["MELEE_ATTACK_RATING_MOD"] == 44
    assert m["MAGIC_DAMAGE_MOD"] == 119
    assert m["DIVINE_DAMAGE_TAKEN_MOD"] == 137   # last entry — pins the full length
    assert len(m) == 138
    # the named constants in this module agree with the table
    assert m["MELEE_ATTACK_RATING_MOD"] == ma.MELEE_ATTACK_RATING_MOD
    assert m["DEFENSE_RATING"] == ma.DEFENSE_RATING


def test_attack_speed_pct_melee_vs_ranged():
    mods = {ma.MELEE_ATTACK_SPEED_MOD: 25, ma.RANGE_ATTACK_SPEED_MOD: -6.25,
            ma.ATTACK_SPEED_MOD: 5}
    # 1H melee picks up generic + melee (not ranged)
    assert ma.attack_speed_pct(mods, is_ranged=False, weapon_class_id=5) == 30
    # ranged picks up generic + range
    assert ma.attack_speed_pct(mods, is_ranged=True, weapon_class_id=9) == -1.25


def test_attack_speed_mod_speeds_up_swing_cadence():
    """The Fighter +25% should drop the 30-tick (1.0s) default to 24 (0.8s)."""
    from drserver.combat.weapon_cycle import resolve_native_basic_attack_cooldown_ticks
    assert resolve_native_basic_attack_cooldown_ticks(None) == 30          # default 1.0s
    assert resolve_native_basic_attack_cooldown_ticks(None, attack_speed_pct=25) == 24  # 0.8s


def test_equipment_modifiers_inert_pending_blockers():
    """Equipment fold is deliberately inert (see COMBAT_FORMULA §8 blockers)."""
    assert ma.aggregate_equipment_modifiers(None) == {}
    assert ma.aggregate_equipment_modifiers(_saved()) == {}
    # aggregate_combat_modifiers == passives only while equipment is inert
    saved = _saved("skills.generic.FighterClassPassive",
                   "skills.generic.MeleeAttackSpeedModPassive")
    assert ma.aggregate_combat_modifiers(saved) == ma.aggregate_passive_modifiers(saved)
