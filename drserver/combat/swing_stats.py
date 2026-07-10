"""Per-swing combat stat resolvers — port of DRS-NET ``Combat/DamageResolver``.

Replaces the hardcoded "starter-vs-pup" constants in
:mod:`drserver.combat.native_swing_input` with the real per-player /
per-monster input resolution DRS-NET uses for its working client-parity
combat replay:

* attacker attack rating  = ``round(Agility x AttackRatingPerAgility(14))``
  (``ResolveAvatarAttackRating``; melee AR-mod% applies, ranged ignores it)
* defender defense rating = authored creature ``DefenseRating`` x the
  ``MonsterDefenseRating`` curve at the monster level
  (``ResolveMonsterDefenseRating`` — see :func:`monster_curves.monster_defense_rating`)
* crit threshold          = ``HeroCriticalChance(3) << 8`` plus
  ``levelDelta x 0x500`` when the attacker out-levels the defender, capped
  ``0x5A00`` (``ResolveCriticalThreshold``); crit damage percent is a flat 200
* damage level            = weapon item level x ``WeaponDamagePerLevel(10)``
  (``ScaleWeaponDamageLevel``; stored/materialized level first, authored
  weapon level next, item-default 1 last)
* damage bonus            = ``floor(MeleeDamagePerStrength(2.3364) x STR)`` for
  melee classes / ``floor(RangedDamagePerAgility(2.124) x AGI)`` for ranged
  (``ResolveUnitWeaponClassDamageBonus``; the per-class GC mods are all 1.0 —
  verified against ``extracter/avatar/classes/*Base.gc``); +equipment stat
  bonuses NOT yet resolved (no +damage mod aggregation — logged as fallback)
* weapon damage / volatility = authored ``Damage`` / ``DamageVolatility`` from
  the equipped weapon's GC Description, ``ceil(x256)`` Fixed32
  (``GetWeaponBaseDamageF32`` / ``GetWeaponVolatilityF32``; volatility clamped
  ``[0, 0.95]``)

Primary stats: ``BASE_STAT_VALUE(10) + allocated + class-passive mod``
(DRS-NET ``PlayerState.Agility/Strength``; passives from
:mod:`drserver.data.class_passives` — Fighter +5 STR/+5 AGI etc.).

# UNVERIFIED vs the live client: the knob defaults below are DRS-NET's
# (Ghidra-derived); equipment +damage/+stat mods are not aggregated yet. The
# [SWING-STATS] log lines exist to diff server swings against live client
# damage — refine here when the live numbers disagree.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from drserver.combat import monster_curves  # noqa: F401 — re-exported for callers
from drserver.core import log

if TYPE_CHECKING:
    from drserver.net.connection import RRConnection

# ── GC knob defaults (DRS-NET GCDatabase.GetKnob fallbacks) ─────────────────
ATTACK_RATING_PER_AGILITY = 14.0
MELEE_DAMAGE_PER_STRENGTH = 2.3364
RANGED_DAMAGE_PER_AGILITY = 2.124
HERO_CRITICAL_CHANCE = 3
CRIT_DAMAGE_PERCENT = 200          # ResolveCriticalDamagePercent — flat
WEAPON_DAMAGE_PER_LEVEL = 10
BASE_STAT_VALUE = 10               # PlayerState.BASE_STAT_VALUE

_CRIT_LEVEL_DELTA_STEP = 0x500
_CRIT_THRESHOLD_CAP = 0x5A00

# DRS-NET TryResolveWeaponClassId. Ranged ids: 3 / 9 / 13.
_WEAPON_CLASS_IDS = {
    "HTH": 1,
    "2HRANGED": 3, "2HCROSSBOW": 3, "2HBOW": 3, "2HGUN": 3,
    "1HMELEE": 5, "1HSTAFF": 5, "1HMACE": 5, "1HSWORD": 5, "1HAXE": 5,
    "2HMELEE": 6, "2HMACE": 6, "2HSWORD": 6, "2HAXE": 6,
    "POLEARM": 8,
    "1HRANGED": 9, "1HCROSSBOW": 9, "1HBOW": 9, "1HGUN": 9,
    "2HCANNON": 13,
}
_RANGED_CLASS_IDS = frozenset({3, 9, 13})


def fixed32_from_authored(value: float) -> int:
    """DRS-NET ``Fixed32FromAuthoredDecimal`` — CEIL(value x 256)."""
    return math.ceil(value * 256.0)


def avatar_attack_rating(agility: int, melee_ar_mod_percent: int = 0,
                         is_ranged: bool = False) -> int:
    """``ResolveAvatarAttackRating``: agility x 14, melee AR-mod% applied."""
    base = max(0, round(max(0, agility) * ATTACK_RATING_PER_AGILITY))
    mod = 0 if is_ranged else max(-100, melee_ar_mod_percent)
    return max(0, (base * (100 + mod)) // 100)


def critical_threshold(attacker_level: int, defender_level: int) -> int:
    """``ResolveCriticalThreshold``: 3<<8 + out-level bonus, capped 0x5A00."""
    threshold = HERO_CRITICAL_CHANCE << 8
    delta = max(0, attacker_level) - max(0, defender_level)
    if delta > 0:
        threshold += delta * _CRIT_LEVEL_DELTA_STEP
    return max(0, min(_CRIT_THRESHOLD_CAP, threshold))


def scale_weapon_damage_level(item_level: int) -> int:
    """``ScaleWeaponDamageLevel``: itemLevel x WeaponDamagePerLevel(10),
    DPSModifier(1.0), Fixed32 math — net effect ``max(1, lvl x 10)`` ushort."""
    lvl = max(1, item_level)
    wdpl_f32 = fixed32_from_authored(float(WEAPON_DAMAGE_PER_LEVEL))
    dps_f32 = fixed32_from_authored(1.0)
    raw = ((lvl << 8) * wdpl_f32) >> 8
    scaled = (raw * dps_f32) >> 16
    return max(1, min(0xFFFF, scaled))


def weapon_class_id(weapon_class: Optional[str]) -> int:
    """``TryResolveWeaponClassId`` — 0 when unknown/empty."""
    return _WEAPON_CLASS_IDS.get((weapon_class or "").strip().upper(), 0)


def damage_bonus(class_id: int, strength: int, agility: int) -> int:
    """``ResolveUnitWeaponClassDamageBonus`` stat-derived part (per-class GC
    mods are all 1.0; equipment +damage stats not aggregated yet)."""
    if class_id in _RANGED_CLASS_IDS:
        return max(0, math.floor(RANGED_DAMAGE_PER_AGILITY * max(0, agility)))
    if class_id in (1, 5, 6, 8):
        return max(0, min(0xFFFF,
                          math.floor(MELEE_DAMAGE_PER_STRENGTH * max(0, strength))))
    return 0


# ── Per-connection swing profile ─────────────────────────────────────────────

@dataclass(frozen=True)
class SwingProfile:
    """Everything player-side a swing-damage replay needs (DRS-NET
    ``CreatePlayerWeaponDamageInput`` minus the monster half)."""
    player_level: int
    strength: int
    agility: int
    weapon_class_id: int
    is_ranged: bool
    attack_rating: int
    damage_bonus: int
    damage_mod: int                  # 100 = no +damage% equipment mods
    damage_level: int
    weapon_damage_f32: int
    weapon_volatility_f32: int
    weapon_gc: str
    source: str
    # ── client_swing combat stats: derived from the FINAL primary attributes ──
    # COMBAT_FORMULA §6g/§6h (live-proven, Styx3 L2 Mage): the client derives AR/b30
    # from the POST-modifier attribute — AGI_final = base 10 + allocated + Σ(primary-
    # attribute AttributeModifiers). For Styx3's Mage redistribution {STR−5, AGI−5,…}
    # that is 10 − 5 = 5 → AR +0xF0 = 70 (= 5×14), b30 +0x180 = 11 (= ⌊2.3364×5⌋).
    # The modifier deltas come from modifier_aggregator (reads the actual tray) so they
    # land even when conn.class_name is wrong; the +10 base IS included (≠ the earlier
    # "exclude base" model, which collapsed to 0 on a negative redistribution). These
    # feed stat_builder.player_attacker_statblock (the client_swing path); the legacy
    # attack_rating/damage_bonus above (class_passives, base-inclusive) stay for the old
    # monster_damage path + its pinned tests. # UNVERIFIED (one capture — confirm 2nd char).
    combat_attack_rating: int = 0
    combat_damage_bonus: int = 0


def _passive_stat_mods(class_name: str) -> tuple[int, int]:
    """(strength_mod, agility_mod) from the class passive skill."""
    try:
        from drserver.data.class_passives import CLASS_PASSIVES
        passive = CLASS_PASSIVES.get(class_name)
        if passive is None:
            return (0, 0)
        return (passive.strength_mod, passive.agility_mod)
    except Exception:  # pragma: no cover — defensive import guard
        return (0, 0)


def _final_combat_attributes(saved, alloc_str: int, alloc_agi: int) -> tuple[int, int]:
    """(strength, agility) the client's CombatStats AR/b30 derivation reads — §6g/§6h.

    ``= BASE_STAT_VALUE(10) + allocated + Σ(primary-attribute AttributeModifiers)``.
    The deltas are summed by
    :func:`drserver.combat.modifier_aggregator.aggregate_combat_modifiers`
    (STRENGTH/AGILITY enums + their ``_MOD`` variants) — which reads the character's
    **actual tray passives**, so a redistribution passive (Mage ``{STR−5, AGI−5}``)
    lands even when ``conn.class_name`` is mismatched (live: server saw "Fighter"→+5
    while the char's real Mage passive is −5). AR/b30 are then derived from these
    **post-modifier** attributes — folding the modifiers BEFORE the derivation fixes
    the order-of-operations bug (the server derived AR from the pre-modifier AGI).
    Live target: Styx3 L2 Mage AGI 5 → AR 70, b30 11.
    """
    str_delta = agi_delta = 0.0
    try:
        from drserver.combat import modifier_aggregator as ma
        agg = ma.aggregate_combat_modifiers(saved)
        str_delta = agg.get(ma.STRENGTH, 0.0) + agg.get(ma.STRENGTH_MOD, 0.0)
        agi_delta = agg.get(ma.AGILITY, 0.0) + agg.get(ma.AGILITY_MOD, 0.0)
    except Exception:  # pragma: no cover — defensive import/DB guard
        pass
    final_str = max(0, BASE_STAT_VALUE + alloc_str + int(round(str_delta)))
    final_agi = max(0, BASE_STAT_VALUE + alloc_agi + int(round(agi_delta)))
    return final_str, final_agi


def _weapon_row(weapon_gc: Optional[str]) -> Optional[dict]:
    """Authored weapon stats (Damage / DamageVolatility / WeaponClass / level)
    from the ``weapons`` content table; None when not found."""
    if not weapon_gc:
        return None
    try:
        from drserver.db import game_database
        cur = game_database.execute_reader(
            "SELECT damage, level, weapon_class, raw_json FROM weapons"
            " WHERE gc_type = :gc OR gc_type LIKE :suffix LIMIT 1",
            {"gc": weapon_gc, "suffix": f"%.{weapon_gc}"})
        row = cur.fetchone()
        if row is None:
            return None
        volatility = 0.5
        try:
            raw = json.loads(row["raw_json"] or "{}")
            desc = _find_description(raw)
            if desc:
                volatility = float(desc.get("DamageVolatility", volatility))
        except (ValueError, TypeError):
            pass
        return {
            "damage": float(row["damage"] or 0.0),
            "level": int(row["level"] or 0),
            "weapon_class": row["weapon_class"] or "",
            "volatility": volatility,
        }
    except Exception as exc:  # DB unavailable in some unit tests
        log.debug(f"[SWING-STATS] weapon lookup failed for '{weapon_gc}': {exc}")
        return None


def _find_description(node: dict) -> Optional[dict]:
    """First GC child block named 'Description' carrying weapon properties."""
    if not isinstance(node, dict):
        return None
    if node.get("name") == "Description":
        return node.get("properties") or {}
    children = node.get("children")
    child_iter = children.values() if isinstance(children, dict) else (children or [])
    for child in child_iter:
        found = _find_description(child)
        if found is not None:
            return found
    return None


#: Pup-anchor monster defense rating — used only when a tracked monster has no
#: authored DefenseRating (pre-refactor registrations / unknown creatures).
_FALLBACK_MONSTER_DEFENSE_RATING = 52

# Legacy starter anchor — used only when the profile cannot be resolved
# (no saved character / DB row). Matches the old hardcoded constants so a
# resolver failure degrades to the previous behaviour instead of zeros.
_FALLBACK = SwingProfile(
    player_level=1, strength=15, agility=15, weapon_class_id=5, is_ranged=False,
    attack_rating=210, damage_bonus=31, damage_mod=100, damage_level=1,
    weapon_damage_f32=139, weapon_volatility_f32=85,
    weapon_gc="", source="fallback-starter-anchor",
    # allocated+passive equivalent of the 15/15 display anchor (=base10+5): AGI/STR 5.
    combat_attack_rating=70, combat_damage_bonus=11,
)


def resolve_swing_profile(conn: "RRConnection") -> SwingProfile:
    """Build (and cache on ``conn``) the player's swing profile.

    Cache key = (level, allocated STR/AGI, equipped weapon gc) so equip swaps,
    level-ups and stat spends re-resolve automatically.
    """
    weapon_gc = _equipped_weapon_gc(conn)
    saved = _saved_character(conn)
    alloc_str = getattr(saved, "stat_strength", 0) if saved else 0
    alloc_agi = getattr(saved, "stat_agility", 0) if saved else 0
    level = max(1, getattr(conn, "player_level", 1) or 1)
    key = (level, alloc_str, alloc_agi, weapon_gc or "")

    cached = getattr(conn, "_swing_profile_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    profile = _build_profile(conn, saved, level, alloc_str, alloc_agi, weapon_gc)
    conn._swing_profile_cache = (key, profile)
    log.info(f"[SWING-STATS] '{getattr(conn, 'login_name', '?')}' profile "
             f"src={profile.source} L{profile.player_level} STR={profile.strength} "
             f"AGI={profile.agility} AR={profile.attack_rating} "
             f"combatAR={profile.combat_attack_rating} "
             f"combatB30={profile.combat_damage_bonus} "
             f"wcls={profile.weapon_class_id} dmgLvl={profile.damage_level} "
             f"bonus={profile.damage_bonus} wpnF32={profile.weapon_damage_f32} "
             f"volF32={profile.weapon_volatility_f32} weapon='{profile.weapon_gc}'")
    return profile


def _saved_character(conn: "RRConnection"):
    try:
        from drserver.db import character_repository
        char_id = getattr(conn, "char_sql_id", 0)
        if not char_id:
            return None
        return character_repository.get_character(char_id)
    except Exception:
        return None


def _equipped_weapon_gc(conn: "RRConnection") -> Optional[str]:
    saved = _saved_character(conn)
    eq = getattr(saved, "equipment", None) if saved else None
    return getattr(eq, "weapon", None) if eq else None


def _build_profile(conn: "RRConnection", saved, level: int, alloc_str: int,
                   alloc_agi: int, weapon_gc: Optional[str]) -> SwingProfile:
    if saved is None:
        log.warn(f"[SWING-STATS] '{getattr(conn, 'login_name', '?')}' no saved "
                 f"character — using fallback starter anchor")
        return _FALLBACK

    str_mod, agi_mod = _passive_stat_mods(getattr(conn, "class_name", "") or
                                          getattr(saved, "class_name", ""))
    strength = max(1, BASE_STAT_VALUE + alloc_str + str_mod)
    agility = max(1, BASE_STAT_VALUE + alloc_agi + agi_mod)
    # Combat AR/b30 derive from the FINAL (post-modifier) attributes, base 10 included
    # — base + allocated + Σ(primary-attr AttributeModifiers from the tray). See §6g.
    combat_str, combat_agi = _final_combat_attributes(saved, alloc_str, alloc_agi)

    weapon = _weapon_row(weapon_gc)
    if weapon is None:
        if weapon_gc:
            log.warn(f"[SWING-STATS] weapon '{weapon_gc}' not in weapons table — "
                     f"fallback weapon stats")
        class_id = 5                      # bare hands ride the 1H-melee path
        dmg_f32 = _FALLBACK.weapon_damage_f32
        vol_f32 = _FALLBACK.weapon_volatility_f32
        dmg_level = scale_weapon_damage_level(1)
        source = "profile-no-weapon"
    else:
        class_id = weapon_class_id(weapon["weapon_class"]) or 5
        dmg_f32 = (fixed32_from_authored(weapon["damage"])
                   if weapon["damage"] > 0 else _FALLBACK.weapon_damage_f32)
        vol_f32 = fixed32_from_authored(max(0.0, min(0.95, weapon["volatility"])))
        stored_level = _stored_weapon_level(saved)
        item_level = stored_level if stored_level > 0 else max(1, weapon["level"])
        dmg_level = scale_weapon_damage_level(item_level)
        source = "profile-db"

    is_ranged = class_id in _RANGED_CLASS_IDS
    return SwingProfile(
        player_level=level,
        strength=strength,
        agility=agility,
        weapon_class_id=class_id,
        is_ranged=is_ranged,
        attack_rating=avatar_attack_rating(agility, 0, is_ranged),
        damage_bonus=damage_bonus(class_id, strength, agility),
        combat_attack_rating=avatar_attack_rating(combat_agi, 0, is_ranged),
        combat_damage_bonus=damage_bonus(class_id, combat_str, combat_agi),
        damage_mod=100,
        damage_level=dmg_level,
        weapon_damage_f32=dmg_f32,
        weapon_volatility_f32=vol_f32,
        weapon_gc=weapon_gc or "",
        source=source,
    )


def _stored_weapon_level(saved) -> int:
    """Materialized (scaled-drop) weapon level from the equipment slot map."""
    eq = getattr(saved, "equipment", None)
    levels = getattr(eq, "slot_level", None) if eq else None
    if not levels:
        return 0
    try:
        return max(0, int(levels.get("weapon", 0) or 0))
    except (TypeError, ValueError):
        return 0
