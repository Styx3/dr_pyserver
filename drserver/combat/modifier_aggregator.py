"""modifier_aggregator.py — sum ``AttributeModifier`` deltas into a ``{enum: value}``
map, the input to the client's CombatStats recompute.

bible §6 / ``docs/COMBAT_FORMULA.md`` §8: the client builds a unit's CombatStats from
**base attributes → per-stat derivations → + Σ(all active AttributeModifiers)**, and each
modifier's ``Attribute`` enum indexes the stats array directly at
``offset = 0xC8 + enum × 4`` (RESOLVED 2026-06-14, Ghidra; proven ~30×). The recompute
(``FUN_005093e0``, unit vtable+0xA4) flat-adds every active modifier's ``Value`` into its
slot. AttributeModifiers come from **passives in tray + equipped items + active buffs** —
the server has the references and sends them but never folded the numeric deltas into
combat (the user-flagged equipment/passive gap).

This module aggregates those deltas. **Passives are wired** (deltas hardcoded from
``extracter/skills/generic/*Passive.gc`` — the importer dropped the nested ``Modifier``
block, so they are not in the DB). **Equipment** (item base + the per-item ScaleMod the
server already picks, which ``extends EnhancementsPAL.*``) and **buffs** (already tracked
as ``ActiveModifier``) are the next slices — see :func:`aggregate_combat_modifiers`.

Apply math is **flat integer add**; ``_MOD`` percent fields store the raw percent (the
formula adds the slot to a literal ``100``). Fractional authored values (attack-speed
``-6.25``) belong to a separate non-damage subsystem and are rounded here (they are not
read by the swing-damage formula). # math T1 from formula structure — confirm scale live.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Tuple

from drserver.data import class_passives

# ── Attribute enum (combat-relevant subset; full 0-137 in COMBAT_FORMULA §8) ──
STRENGTH = 1
AGILITY = 2
ENDURANCE = 3
INTELLECT = 4
ATTACK_RATING = 10
ATTACK_RATING_MOD = 11
ATTACK_SPEED_MOD = 12
DAMAGE_BONUS = 13
DAMAGE_MOD = 14
CRITICAL_CHANCE = 15
CRITICAL_DAMAGE_MOD = 20
DEFENSE_RATING = 25
DEFENSE_RATING_MOD = 26
BLOCK = 28
MAX_HIT_POINTS = 33
MELEE_ATTACK_RATING = 43
MELEE_ATTACK_RATING_MOD = 44
MELEE_ATTACK_SPEED_MOD = 45
MELEE_DAMAGE_BONUS = 46
MELEE_DAMAGE_MOD = 47
MELEE_CRITICAL_CHANCE_MOD = 48
MELEE_DEFENSE_RATING = 49
MELEE_DEFENSE_RATING_MOD = 50
RANGE_ATTACK_RATING = 52
RANGE_ATTACK_RATING_MOD = 53
RANGE_ATTACK_SPEED_MOD = 54
RANGE_DAMAGE_BONUS = 55
RANGE_DAMAGE_MOD = 56
MELEE1H_ATTACK_RATING = 61
MELEE1H_ATTACK_RATING_MOD = 62
MELEE1H_ATTACK_SPEED_MOD = 63
MELEE1H_DAMAGE_BONUS = 64
MELEE1H_DAMAGE_MOD = 65
MELEE2H_ATTACK_SPEED_MOD = 69
INTELLECT_MOD = 73
STRENGTH_MOD = 74
AGILITY_MOD = 75
ENDURANCE_MOD = 76
HEALTH_MOD = 78
MANA_PER_INTELLECT_MOD = 83
HEALTH_PER_ENDURANCE_MOD = 84
ATTACK_RATING_PER_AGILITY_MOD = 86
MAGIC_DAMAGE_MOD = 119
MAGIC_DAMAGE_BONUS = 120

#: The primary attributes — folded into the per-stat derivations (AR=AGI×14 etc.) by
#: :mod:`drserver.combat.swing_stats`, NOT written as raw CombatStats slots here (writing
#: them would not change the swing-damage formula, which reads derived stats). Equipment
#: STR/AGI mods must reach the derivation via ``swing_stats`` — that is the equipment slice.
PRIMARY_ATTRIBUTES = frozenset({
    STRENGTH, AGILITY, ENDURANCE, INTELLECT,
    STRENGTH_MOD, AGILITY_MOD, ENDURANCE_MOD, INTELLECT_MOD,
})


def offset_for(enum: int) -> int:
    """CombatStats byte offset for an ``Attribute`` enum (``0xC8 + enum×4``)."""
    return 0xC8 + (enum & 0xFFFF) * 4


# Full ``Attribute`` enum name→int (0-137), transcribed from the client binary table
# at ``0x86c7d0`` (COMBAT_FORMULA §8). Used to resolve the ``Attribute = <NAME>`` string
# in modifier ``.gc`` content (passives parse to ints directly above; equipment /
# ``AttributesPAL`` resolution uses this).
_ATTRIBUTE_ENUM_ORDER = (
    "PVP_DAMAGE_MOD STRENGTH AGILITY ENDURANCE INTELLECT SPEED SPEEDMOD MIN_SPEED_MOD "
    "IMMOBILE SIZEMOD ATTACK_RATING ATTACK_RATING_MOD ATTACK_SPEED_MOD DAMAGE_BONUS "
    "DAMAGE_MOD CRITICAL_CHANCE STUN_MOD MAGIC_CRITICAL_CHANCE STUN_RESIST SILENCE "
    "CRITICAL_DAMAGE_MOD HIT_POINT_STEAL MANA_POINT_STEAL FACTIONOVERRIDE FEAR_RESIST "
    "DEFENSE_RATING DEFENSE_RATING_MOD DAMAGE_REFLECT BLOCK DODGE DAMAGE_IMMUNITY "
    "DAMAGE_RESIST INVISIBLE MAX_HIT_POINTS MAX_MANA_POINTS HIT_POINT_REGEN MANA_POINT_REGEN "
    "HIT_POINT_REGEN_MOD MANA_POINT_REGEN_MOD HIT_POINT_REGEN_BONUS MANA_POINT_REGEN_BONUS "
    "MANA_COST_BONUS MAGIC_CRITICAL_CHANCE_MOD MELEE_ATTACK_RATING MELEE_ATTACK_RATING_MOD "
    "MELEE_ATTACK_SPEED_MOD MELEE_DAMAGE_BONUS MELEE_DAMAGE_MOD MELEE_CRITICAL_CHANCE_MOD "
    "MELEE_DEFENSE_RATING MELEE_DEFENSE_RATING_MOD MELEE_DAMAGE_REFLECT RANGE_ATTACK_RATING "
    "RANGE_ATTACK_RATING_MOD RANGE_ATTACK_SPEED_MOD RANGE_DAMAGE_BONUS RANGE_DAMAGE_MOD "
    "RANGE_CRITICAL_CHANCE_MOD RANGE_DEFENSE_RATING RANGE_DEFENSE_RATING_MOD RANGE_DAMAGE_REFLECT "
    "MELEE1H_ATTACK_RATING MELEE1H_ATTACK_RATING_MOD MELEE1H_ATTACK_SPEED_MOD MELEE1H_DAMAGE_BONUS "
    "MELEE1H_DAMAGE_MOD MELEE1H_CRITICAL_CHANCE_MOD MELEE2H_ATTACK_RATING MELEE2H_ATTACK_RATING_MOD "
    "MELEE2H_ATTACK_SPEED_MOD MELEE2H_DAMAGE_BONUS MELEE2H_DAMAGE_MOD MELEE2H_CRITICAL_CHANCE_MOD "
    "INTELLECT_MOD STRENGTH_MOD AGILITY_MOD ENDURANCE_MOD PRIMARY_ATTRIBUTE_MOD HEALTH_MOD "
    "MANA_MOD DAMAGE_PER_AGILITY_MOD DAMAGE_PER_INTELLECT_MOD DAMAGE_PER_STRENGTH_MOD "
    "MANA_PER_INTELLECT_MOD HEALTH_PER_ENDURANCE_MOD DEFENSE_RATING_PER_STRENGTH_MOD "
    "ATTACK_RATING_PER_AGILITY_MOD AGGRO_MOD MOLASSES_MOD AGGRO_BONUS CRUSHING_DAMAGE_MOD "
    "CRUSHING_DAMAGE_BONUS CRUSHING_DAMAGE_RESIST PIERCING_DAMAGE_MOD PIERCING_DAMAGE_BONUS "
    "PIERCING_DAMAGE_RESIST SLASHING_DAMAGE_MOD SLASHING_DAMAGE_BONUS SLASHING_DAMAGE_RESIST "
    "FIRE_DAMAGE_MOD FIRE_DAMAGE_BONUS FIRE_DAMAGE_RESIST FIRE_DAMAGE_WEAPON_ADD ICE_DAMAGE_MOD "
    "ICE_DAMAGE_BONUS ICE_DAMAGE_RESIST ICE_DAMAGE_WEAPON_ADD POISON_DAMAGE_MOD POISON_DAMAGE_BONUS "
    "POISON_DAMAGE_RESIST POISON_DAMAGE_WEAPON_ADD SHADOW_DAMAGE_MOD SHADOW_DAMAGE_BONUS "
    "SHADOW_DAMAGE_RESIST SHADOW_DAMAGE_WEAPON_ADD DIVINE_DAMAGE_MOD DIVINE_DAMAGE_BONUS "
    "DIVINE_DAMAGE_RESIST DIVINE_DAMAGE_WEAPON_ADD MAGIC_DAMAGE_MOD MAGIC_DAMAGE_BONUS "
    "MAGIC_DAMAGE_RESIST EXPERIENCE_IMMUNITY ATTACK_SILENCE EXPMOD CAST_SPEED_MOD IMMUNITY_MOD "
    "DAMAGE_TAKEN_MOD FIRE_DAMAGE_RESIST_MOD ICE_DAMAGE_RESIST_MOD POISON_DAMAGE_RESIST_MOD "
    "SHADOW_DAMAGE_RESIST_MOD DIVINE_DAMAGE_RESIST_MOD FIRE_DAMAGE_TAKEN_MOD ICE_DAMAGE_TAKEN_MOD "
    "POISON_DAMAGE_TAKEN_MOD SHADOW_DAMAGE_TAKEN_MOD DIVINE_DAMAGE_TAKEN_MOD"
).split()

#: ``Attribute`` name → enum int (e.g. ``"MELEE_ATTACK_RATING_MOD" → 44``).
ATTRIBUTE_NAME_TO_ENUM = {name: i for i, name in enumerate(_ATTRIBUTE_ENUM_ORDER)}


# Passive gc-type (lowercased) → ((enum, value, value_inc), …). Sourced from
# extracter/skills/generic/*Passive.gc ``static Modifier extends AttributeModifier``.
# value_inc is per-skill-level above 1 (most starters are 0; e.g. MeleeAttackRatingMod
# scales +20/level). bible/[[reference_modifier_catalogue]].
_PASSIVE_MODIFIERS: Dict[str, Tuple[Tuple[int, float, float], ...]] = {
    "skills.generic.fighterclasspassive": (
        (HEALTH_PER_ENDURANCE_MOD, 50, 0), (MANA_PER_INTELLECT_MOD, -25, 0),
        (STRENGTH, 5, 0), (AGILITY, 5, 0), (ENDURANCE, -5, 0), (INTELLECT, -5, 0),
        (RANGE_ATTACK_SPEED_MOD, -10, 0),
    ),
    "skills.generic.mageclasspassive": (
        (HEALTH_PER_ENDURANCE_MOD, -25, 0), (MANA_PER_INTELLECT_MOD, 100, 0),
        (STRENGTH, -5, 0), (AGILITY, -5, 0), (ENDURANCE, 5, 0), (INTELLECT, 5, 0),
    ),
    "skills.generic.rangerclasspassive": (
        (HEALTH_PER_ENDURANCE_MOD, 10, 0), (MANA_PER_INTELLECT_MOD, -5, 0),
        (STRENGTH, -5, 0), (AGILITY, 5, 0), (ENDURANCE, 5, 0), (INTELLECT, -5, 0),
    ),
    # #109 stat passives (the half class_passives.py never modelled).
    "skills.generic.meleeattackspeedmodpassive": (
        (MELEE_ATTACK_SPEED_MOD, 25, 0), (MAGIC_DAMAGE_MOD, -5, 0),
        (RANGE_ATTACK_SPEED_MOD, -6.25, 0), (MELEE_ATTACK_RATING_MOD, 100, 0),
    ),
    "skills.generic.magicdamagemodpassive": (
        (MAGIC_DAMAGE_MOD, 20, 0), (RANGE_ATTACK_SPEED_MOD, -6.25, 0),
        (MELEE_ATTACK_SPEED_MOD, -6.25, 0),
    ),
    "skills.generic.rangeattackspeedmodpassive": (
        (RANGE_ATTACK_SPEED_MOD, 25, 0), (MAGIC_DAMAGE_MOD, -5, 0),
        (MELEE_ATTACK_SPEED_MOD, -6.25, 0),
    ),
    # Higher-level (req 30) Fighter passive — scales +20% MAR per level.
    "skills.generic.meleeattackratingmodpassive": (
        (MELEE_ATTACK_RATING_MOD, 80, 20), (HEALTH_MOD, -20, 0),
    ),
}


def aggregate_passive_modifiers(saved) -> Dict[int, float]:
    """Sum the ``Attribute`` deltas of every passive in the character's tray.

    Uses :func:`class_passives.collect_passive_manipulators` to find which passives
    are active (hotbar-driven, with the legacy starting-slot fallback) and their
    levels, then sums ``value + value_inc × (level − 1)`` per enum.
    """
    agg: Dict[int, float] = defaultdict(float)
    if saved is None:
        return {}
    for pm in class_passives.collect_passive_manipulators(saved):
        entries = _PASSIVE_MODIFIERS.get(pm.skill.lower())
        if not entries:
            continue
        level = max(1, int(getattr(pm, "level", 1) or 1))
        for enum, value, value_inc in entries:
            agg[enum] += value + value_inc * (level - 1)
    return dict(agg)


def aggregate_equipment_modifiers(saved) -> Dict[int, float]:
    """Sum the ``AttributeModifier`` deltas of the character's equipped items.

    ★ The chain is fully RE'd and resolvable from server content (COMBAT_FORMULA §8):
    equipped item + its picked mod → ``AttributesPAL.<name>`` (``Attribute`` enum +
    component weight) → ``PoolTables.<pool>`` (a ``{L1→min … L110→max}`` linear lerp by
    item level). The ``Attribute`` name resolves via :data:`ATTRIBUTE_NAME_TO_ENUM`.

    **Returns ``{}`` for now — wiring is BLOCKED on two real items, by design (do not
    fabricate magnitudes; a wrong fold is worse than none):**

    1. **Which mod an instance picked.** ``saved.equipment.slot_scale_mod`` holds a
       ScaleMod gc-type, but ``rarity_helper`` mints C#-fabricated names (``ScaleModPAL.*``)
       that don't match client content (``AxeModPAL.*`` / ``FighterModPAL.*`` …). The
       ScaleMod selection must be re-grounded in content before a slot maps to its real mod.
    2. **The magnitude scale.** The PoolTable lerp gives a raw value (e.g. ``strengthb``
       ``[20@L1 … 1110@L110]``); the Fixed32/÷-scale to the final stat is UNVERIFIED —
       needs one live capture (equip a modded item, read the CombatStats offsets, diff).

    Normal-rarity items carry only label-only ``Binder`` mods (no ``AttributesPAL``
    component → zero combat effect), so the common case is genuinely empty regardless.
    """
    return {}


def attack_speed_pct(modifiers: Dict[int, float], *, is_ranged: bool,
                     weapon_class_id: int = 0) -> float:
    """Total attack-speed-mod percent for the weapon class (drives swing cadence).

    Sums the generic ``ATTACK_SPEED_MOD`` with the weapon-class-specific term the
    client's swing-cooldown reads: ``RANGE_ATTACK_SPEED_MOD`` for ranged, else
    ``MELEE_ATTACK_SPEED_MOD`` plus the ``MELEE1H``/``MELEE2H`` variant. Positive =
    faster (``weapon_cycle.apply_native_attack_speed_pct_to_ticks`` divides ticks by
    ``1 + pct/100``). e.g. a Fighter's #109 passive (``MELEE_ATTACK_SPEED_MOD +25``)
    → +25 → ``30 → 24`` ticks (1.0s → 0.8s).
    """
    pct = modifiers.get(ATTACK_SPEED_MOD, 0.0)
    if is_ranged:
        pct += modifiers.get(RANGE_ATTACK_SPEED_MOD, 0.0)
    else:
        pct += modifiers.get(MELEE_ATTACK_SPEED_MOD, 0.0)
        if weapon_class_id in (1, 5):          # HTH / 1H-melee
            pct += modifiers.get(MELEE1H_ATTACK_SPEED_MOD, 0.0)
        elif weapon_class_id in (6, 8):        # 2H-melee / polearm
            pct += modifiers.get(MELEE2H_ATTACK_SPEED_MOD, 0.0)
    return pct


def aggregate_combat_modifiers(saved) -> Dict[int, float]:
    """All active ``AttributeModifier`` deltas for a character, summed by enum.

    Wired: **passives** (:func:`aggregate_passive_modifiers`). Inert pending blockers:
    **equipment** (:func:`aggregate_equipment_modifiers`). TODO: **buffs** (the
    ``ActiveModifier`` set tracked in ``net/inventory.py`` — fold identically once the
    apply scale is live-confirmed).
    """
    agg: Dict[int, float] = defaultdict(float)
    for source in (aggregate_passive_modifiers(saved), aggregate_equipment_modifiers(saved)):
        for enum, value in source.items():
            agg[enum] += value
    return dict(agg)
