"""Class passive skills — wire collection + client HP/mana parity math.

Port of C# Skills/ClassPassiveData.cs + GameServer.Combat.cs passive helpers.

Every class's authored loadout (client ``avatar/classes/*StartingSkills.gc``)
grants two PASSIVE skills alongside the two actives: the class passive
(``skills.generic.<Class>ClassPassive``, hotbar ID 108) and a stat passive
(ID 109). Passives ride three spawn components:

* OP4 Manipulators — entry body ``0xFF · cstring · u32 slot · u8 level ·
  u32 modifierId``. The trailing u32 is the PASSIVE-specific tail
  (``PassiveSkill::readInit`` @0x53D0E0 reads exactly one u32 — Ghidra-verified
  2026-06-12); actives end in a single flags byte instead. Sending a passive
  with the active 1-byte tail desyncs the manipulator reader (the 2026-06-04
  "Invalid type tag" crash that got passives gated off).
* OP9 Modifiers — one ``<skill>.modifier`` entry per passive (the client
  applies the stat deltas from it).
* OP10 Skills — listed with the actives (same entry format).

The class passive changes the client's max-HP/mana computation
(HEALTH_PER_ENDURANCE_MOD etc.), and the avatar HP synch compare is
zero-tolerance — the server's ``hp_wire`` must add the same bonus
(:func:`class_passive_hp_bonus_wire`). Math is byte-exact from C#
ClassPassiveData (itself the client formula: L1 no-passive = 10 end × 25 +
16 = 266 HP / 10 int × 17 + 5 = 175 MP — the live-proven Styx3 baseline).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:  # pragma: no cover
    from .saved_character import SavedCharacter

# Client formula constants (C# ClassPassiveData; verified vs live L1 readings).
BASE_ENDURANCE = 10
BASE_INTELLECT = 10
HERO_HEALTH_PER_LEVEL = 16
HEALTH_PER_ENDURANCE = 25
POWER_PER_INTELLECT = 17
POWER_PER_LEVEL = 5

# First passive manipulator/modifier id; counts up per passive (C# 0xF000++).
PASSIVE_MODIFIER_ID_BASE = 0xF000

# Authored hotbar IDs from the client *StartingSkills.gc files
# (actives 100/105, passives 108/109).
STARTING_HOTBAR_SLOTS = {
    "skills.generic.stomp": 100, "skills.generic.butcher": 105,
    "skills.generic.fighterclasspassive": 108, "skills.generic.meleeattackspeedmodpassive": 109,
    "skills.generic.poisonblastradius": 100, "skills.generic.poisonshot": 105,
    "skills.generic.rangerclasspassive": 108, "skills.generic.rangeattackspeedmodpassive": 109,
    "skills.generic.shadowlightning": 100, "skills.generic.firebolt": 105,
    "skills.generic.mageclasspassive": 108, "skills.generic.magicdamagemodpassive": 109,
}


@dataclass(frozen=True)
class ClassPassive:
    """Stat deltas of a ``<Class>ClassPassive`` (C# ClassPassiveData.Passives)."""
    passive_skill_id: str
    health_per_endurance_mod: int
    endurance_mod: int
    strength_mod: int
    agility_mod: int
    intellect_mod: int
    mana_per_intellect_mod: int
    range_attack_speed_mod: int


CLASS_PASSIVES = {
    "Fighter": ClassPassive("skills.generic.FighterClassPassive",
                            health_per_endurance_mod=50, endurance_mod=-5,
                            strength_mod=5, agility_mod=5, intellect_mod=-5,
                            mana_per_intellect_mod=-25, range_attack_speed_mod=-10),
    "Mage": ClassPassive("skills.generic.MageClassPassive",
                         health_per_endurance_mod=-25, endurance_mod=5,
                         strength_mod=-5, agility_mod=-5, intellect_mod=5,
                         mana_per_intellect_mod=100, range_attack_speed_mod=0),
    "Ranger": ClassPassive("skills.generic.RangerClassPassive",
                           health_per_endurance_mod=10, endurance_mod=5,
                           strength_mod=-5, agility_mod=5, intellect_mod=-5,
                           mana_per_intellect_mod=-5, range_attack_speed_mod=0),
}

_PASSIVE_SKILL_TO_CLASS = {
    p.passive_skill_id.lower(): cls for cls, p in CLASS_PASSIVES.items()
}


@dataclass(frozen=True)
class PassiveManipulator:
    """One passive ready for the wire (C# PassiveManipulator struct)."""
    slot: int
    skill: str
    level: int
    modifier_id: int


def is_passive_skill(skill_gc_class: str) -> bool:
    """C# IsPassiveSkill — passives/traits are identified by gc-type name."""
    low = (skill_gc_class or "").lower()
    return "passive" in low or "trait" in low


def starting_hotbar_slot(skill_gc_class: str) -> int:
    """Authored hotbar ID for a starting skill, or -1 (no hotbar slot)."""
    return STARTING_HOTBAR_SLOTS.get((skill_gc_class or "").lower(), -1)


def collect_passive_manipulators(saved: "SavedCharacter | None") -> List[PassiveManipulator]:
    """Passives to ship in OP4/OP9, with slots and 0xF000+ modifier ids.

    Port of C# CollectPassiveManipulators (hotbar-slot driven), extended with a
    legacy fallback: characters created before hotbar persistence have passive
    skill rows but ``hotbar_slot = -1``, so any known starting passive missing a
    hotbar entry falls back to its authored slot (108/109).
    """
    result: List[PassiveManipulator] = []
    if saved is None:
        return result

    def _skill_level(skill: str) -> int:
        getter = getattr(saved, "get_skill_level", None)
        return max(1, getter(skill)) if callable(getter) else 1

    modifier_id = PASSIVE_MODIFIER_ID_BASE
    seen = set()
    for hbs in getattr(saved, "hotbar_slots", None) or []:
        if not is_passive_skill(hbs.skill) or hbs.skill.lower() in seen:
            continue
        seen.add(hbs.skill.lower())
        result.append(PassiveManipulator(slot=hbs.slot, skill=hbs.skill,
                                         level=_skill_level(hbs.skill),
                                         modifier_id=modifier_id))
        modifier_id += 1
    for skill in getattr(saved, "skills", None) or []:
        if not is_passive_skill(skill) or skill.lower() in seen:
            continue
        slot = starting_hotbar_slot(skill)
        if slot < 0:
            continue  # learned non-starting passive without a hotbar row
        seen.add(skill.lower())
        result.append(PassiveManipulator(slot=slot, skill=skill,
                                         level=_skill_level(skill),
                                         modifier_id=modifier_id))
        modifier_id += 1
    return result


# ── client HP/mana formula (byte-exact port of C# ClassPassiveData) ───────────
def _clamp_wire(wire: int) -> int:
    if wire <= 0:
        return 0
    return min(wire, 0xFFFFFFFF)


def calculate_hp_wire(level: int, endurance: int, health_per_endurance_mod_percent: int) -> int:
    """Avatar max HP in ×256 wire — C# ClassPassiveData.CalculateHPWire."""
    lvl = max(1, level)
    end = max(1, endurance)
    percent = max(0, 100 + health_per_endurance_mod_percent)
    percent_fixed = (percent * 0x10000) // 0x6400
    hp_per_endurance_fixed = (HEALTH_PER_ENDURANCE * 256 * percent_fixed) >> 8
    endurance_hp = ((end << 8) * hp_per_endurance_fixed) >> 16
    level_hp = lvl * HERO_HEALTH_PER_LEVEL
    return _clamp_wire((endurance_hp + level_hp) * 256)


def calculate_mana_wire(level: int, intellect: int, mana_per_intellect_mod_percent: int) -> int:
    """Avatar max mana in ×256 wire — C# ClassPassiveData.CalculateManaWire."""
    lvl = max(1, level)
    intel = max(1, intellect)
    percent = max(0, 100 + mana_per_intellect_mod_percent)
    percent_fixed = (percent * 0x10000) // 0x6400
    mana_per_intellect_fixed = (POWER_PER_INTELLECT * 256 * percent_fixed) >> 8
    intellect_mana = ((intel << 8) * mana_per_intellect_fixed) >> 16
    level_mana = lvl * POWER_PER_LEVEL
    return _clamp_wire((intellect_mana + level_mana) * 256)


def class_passive_hp_bonus_wire(class_name: str, level: int = 1,
                                allocated_endurance: int = 0) -> int:
    """×256 HP delta the class passive adds — C# CalculateHPBonusWire."""
    passive = CLASS_PASSIVES.get(class_name)
    if passive is None:
        return 0
    base_endurance = BASE_ENDURANCE + max(0, allocated_endurance)
    no_passive = calculate_hp_wire(level, base_endurance, 0)
    passive_endurance = max(1, base_endurance + passive.endurance_mod)
    with_passive = calculate_hp_wire(level, passive_endurance,
                                     passive.health_per_endurance_mod)
    return with_passive - no_passive


def class_passive_mana_bonus_wire(class_name: str, level: int = 1,
                                  allocated_intellect: int = 0) -> int:
    """×256 mana delta the class passive adds — C# CalculateManaBonusWire."""
    passive = CLASS_PASSIVES.get(class_name)
    if passive is None:
        return 0
    base_intellect = BASE_INTELLECT + max(0, allocated_intellect)
    no_passive = calculate_mana_wire(level, base_intellect, 0)
    passive_intellect = max(1, base_intellect + passive.intellect_mod)
    with_passive = calculate_mana_wire(level, passive_intellect,
                                       passive.mana_per_intellect_mod)
    return with_passive - no_passive


def saved_character_passive_hp_bonus_wire(saved: "SavedCharacter | None") -> int:
    """HP bonus for the passives a character actually carries on the wire.

    Mirrors C# RecalculateHotbarPassiveBonuses for the class-passive part: the
    bonus applies only when the ``<Class>ClassPassive`` is among the shipped
    passives (so a character with no passive rows keeps the flat baseline).
    """
    if saved is None:
        return 0
    bonus = 0
    for pm in collect_passive_manipulators(saved):
        cls = _PASSIVE_SKILL_TO_CLASS.get(pm.skill.lower())
        if cls is not None:
            bonus += class_passive_hp_bonus_wire(
                cls, getattr(saved, "level", 1) or 1,
                max(0, getattr(saved, "stat_endurance", 0)))
    return bonus


def saved_character_passive_mana_bonus_wire(saved: "SavedCharacter | None") -> int:
    """Mana bonus for the shipped class passive (see HP counterpart)."""
    if saved is None:
        return 0
    bonus = 0
    for pm in collect_passive_manipulators(saved):
        cls = _PASSIVE_SKILL_TO_CLASS.get(pm.skill.lower())
        if cls is not None:
            bonus += class_passive_mana_bonus_wire(
                cls, getattr(saved, "level", 1) or 1,
                max(0, getattr(saved, "stat_intellect", 0)))
    return bonus
