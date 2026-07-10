"""Player state — HP/mana computation and level-up logic.

Ported from C# Networking/PlayerState.cs. Computes HP and mana from base
values, level, primary attributes, and equipment stat bonuses from the
ItemStatDatabase. Handles XP curve and level-up sequencing.

Phase 10: Core HP/mana computation with equipment bonuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, TYPE_CHECKING

from ..core import log
from ..db import character_repository
from ..data.rarity_helper import ItemRarity

if TYPE_CHECKING:  # pragma: no cover
    from .connection import RRConnection


# Base HP per level, scaled up to wire format (×256).
def compute_max_hp(level: int, endurance: int, equipment_hp_bonus: int = 0) -> int:
    """Compute maximum HP in wire format (HP × 256).

    C# formula: baseHP + (level - 1) * hpPerLevel + enduranceHpBonus + equipBonus
    Uses GlobalKnobs.gc values: baseHP=200, hpPerLevel=10, hpPerEndurance=5
    """
    base_hp = 200
    hp_per_level = 10
    hp_per_endurance = 5
    return (base_hp + (level - 1) * hp_per_level + endurance * hp_per_endurance + equipment_hp_bonus) * 256


# ── Avatar max HP / mana in ×256 wire format ───────────────────────────────────
#
# The client computes its own avatar max HP locally and, on certain zones (notably
# dungeon00_level01), compares it against the value the server sends in the avatar
# create + ``0x02`` synch trailers. A mismatch is a *fatal* Avatar synch crash
# (exit 0xc000013a). The value is ×256 WIRE (not raw): ground truth is the live
# Styx3 = Level 1 Mage avatar reading HP 266 / MP 175 — i.e. wire 68096 / 44800.
#
# Ported from C# Entity/PlayerState.cs + Skills/ClassPassiveData.cs (the client
# formula): base = endurance(10+allocated)×25 + level×16, with the class-passive
# HP/mana bonus on top once the passive ships on the wire (see
# ``data.class_passives``). At allocated=0 / no passive this reduces to the
# live-proven flat baseline 68096 + 4096·(level-1).


def compute_avatar_max_hp_wire(level: int, equipment_hp_bonus_wire: int = 0,
                               *, allocated_endurance: int = 0,
                               passive_hp_bonus_wire: int = 0) -> int:
    """Avatar max HP in ×256 wire format — C# PlayerState.CalculateBaseHP (+ gear).

    Feeds ``conn.hp_wire`` so the avatar create packet, the per-tick ``0x36``
    heartbeat and every ``0x02`` action trailer all carry the same value the
    client computes locally. Pass ``equipment_hp_bonus_wire`` for equipped-item
    HP and ``passive_hp_bonus_wire`` for the class-passive delta, both ×256 wire.
    """
    from .class_passives import BASE_ENDURANCE, calculate_hp_wire
    base = calculate_hp_wire(level, BASE_ENDURANCE + max(0, allocated_endurance), 0)
    return base + max(0, equipment_hp_bonus_wire) + passive_hp_bonus_wire


def compute_avatar_max_mana_wire(level: int, equipment_mana_bonus_wire: int = 0,
                                 *, allocated_intellect: int = 0,
                                 passive_mana_bonus_wire: int = 0) -> int:
    """Avatar max mana in ×256 wire format — C# PlayerState.CalculateMaxMana (+ gear)."""
    from .class_passives import BASE_INTELLECT, calculate_mana_wire
    base = calculate_mana_wire(level, BASE_INTELLECT + max(0, allocated_intellect), 0)
    return base + max(0, equipment_mana_bonus_wire) + passive_mana_bonus_wire


def compute_saved_avatar_hp_wire(saved) -> int:
    """Max HP wire for a loaded SavedCharacter (level + allocated endurance +
    class-passive bonus when the passive ships on the wire)."""
    from .class_passives import saved_character_passive_hp_bonus_wire
    if saved is None:
        return compute_avatar_max_hp_wire(1)
    return compute_avatar_max_hp_wire(
        getattr(saved, "level", 1) or 1,
        allocated_endurance=max(0, getattr(saved, "stat_endurance", 0)),
        passive_hp_bonus_wire=saved_character_passive_hp_bonus_wire(saved))


def compute_saved_avatar_mana_wire(saved) -> int:
    """Max mana wire for a loaded SavedCharacter (see HP counterpart)."""
    from .class_passives import saved_character_passive_mana_bonus_wire
    if saved is None:
        return compute_avatar_max_mana_wire(1)
    return compute_avatar_max_mana_wire(
        getattr(saved, "level", 1) or 1,
        allocated_intellect=max(0, getattr(saved, "stat_intellect", 0)),
        passive_mana_bonus_wire=saved_character_passive_mana_bonus_wire(saved))


def resolve_synch_hp_wire(max_wire: int, client_hp_wire: "int | None") -> int:
    """Resolve the outbound avatar synch HP — port of C# ``PlayerState.SynchHP``
    (``HasClientHP && current < sync ? current : sync``).

    The vanilla client is authoritative for its OWN avatar HP: it self-sims
    damage and self-levels, then reports its current HP in a trailing
    EntitySynchInfo. When the server has such a report and it is below the
    level-derived max, echo the client's value so the per-tick ``0x36`` heartbeat
    and every ``0x02`` trailer match the client's local HP. Echoing the max while
    the client is damaged fails the zero-tolerance compare (``FUN_005dd900``) and
    fatally crashes the Avatar on dungeon zones. A missing report (fresh spawn),
    a non-positive value (death, handled via respawn), or a value at/above the
    max (client refilled) all fall back to the level-derived max.
    """
    if client_hp_wire is not None and 0 < client_hp_wire < max_wire:
        return client_hp_wire
    return max_wire


def compute_max_mana(level: int, intellect: int, equipment_mana_bonus: int = 0) -> int:
    """Compute maximum mana in wire format (MP × 256).

    C# formula: baseMP + (level - 1) * mpPerLevel + intellectMpBonus + equipBonus
    """
    base_mp = 200
    mp_per_level = 10
    mp_per_intellect = 5
    return (base_mp + (level - 1) * mp_per_level + intellect * mp_per_intellect + equipment_mana_bonus) * 256


# ── XP curve / level-up (port of C# Networking/PlayerState.cs) ─────────────────
#
# The vanilla client self-levels LOCALLY on each kill (combat is client-authoritative)
# and recomputes its avatar HP. The server must mirror this math EXACTLY so its level
# tracks the client's in lockstep — otherwise the avatar ``0x02`` synch trailer carries
# a stale HP and the client fatally crashes (live 2026-06-01: client at L2, server still
# sending the L1 hp_wire 68096 vs the client's recomputed 72192).
#
# All three functions are reverse-engineered from the client binary via DR-Server:
#   GetClientThreshold (HeroDesc::getRequiredExp @0x4FAF60), GetXPPerKill (@0x4F8409 /
#   @0x42BFF0), AddExperience. Keep the integer/Fixed-point arithmetic byte-exact.

# Tables.gc Experience CurveTable keyframes (level, "# of 1.0 monsters at your level").
_XP_CURVE: tuple[tuple[int, int], ...] = ((2, 10), (3, 25), (4, 45), (5, 65), (100, 5000))
_XP_MULTIPLIER = 100   # GetClientThreshold MULTIPLIER (binary: imul ..., 0x64)
_MAX_LEVEL = 100


def xp_threshold_for_level(next_level: int) -> int:
    """XP required to advance FROM ``next_level - 1`` TO ``next_level``.

    Port of C# ``PlayerState.GetClientThreshold`` — the client's exact Fixed-point
    threshold (Fixed8.8 level/value, Fixed16.16 interpolation t), then ``× 100``.
    Matches native thresholds L2=1000, L3=2500, L4=4500, L5=6500.
    """
    target = next_level << 8  # Fixed8.8
    for i, (lvl, val) in enumerate(_XP_CURVE):
        lv_fixed = lvl << 8
        val_fixed = val << 8
        if target <= lv_fixed:
            if i == 0:
                return val * _XP_MULTIPLIER
            prev_lv = _XP_CURVE[i - 1][0] << 8
            prev_val = _XP_CURVE[i - 1][1] << 8
            delta = val_fixed - prev_val
            # Fixed16.16 interpolation parameter (C# truncating int division).
            t = ((target - prev_lv) << 16) // (lv_fixed - prev_lv)
            interp = prev_val + ((delta * t) >> 16)
            return (interp >> 8) * _XP_MULTIPLIER
    return _XP_CURVE[-1][1] * _XP_MULTIPLIER


def xp_per_kill(monster_level: int, player_level: int) -> int:
    """XP awarded for killing a monster — port of C# ``PlayerState.GetXPPerKill``.

    ~500 XP per kill within 5 levels; 0 if the monster is 5+ levels below the player.
    """
    if monster_level <= player_level - 5:
        return 0
    effective_level = min(monster_level, player_level)
    num = (effective_level << 8) << 8
    den = player_level << 8
    ratio_f32 = num // den            # Fixed32 divide (positive → truncates like C#)
    xp = (ratio_f32 * 500) >> 8       # apply ratio to base 500, drop the 8.8 fraction
    return xp if xp >= 1 else 1


def apply_xp(level: int, experience: int, gained_xp: int,
             max_level: int = _MAX_LEVEL) -> tuple[int, int, bool]:
    """Add ``gained_xp`` and level up — port of C# ``PlayerState.AddExperience``.

    Returns ``(new_level, remaining_experience, did_level)``. The threshold is
    DEDUCTED from experience on each level (the carry rolls into the next level),
    matching the client; ``did_level`` signals the caller to refresh ``hp_wire``.
    """
    experience += gained_xp
    did_level = False
    needed = xp_threshold_for_level(level + 1)
    while experience >= needed and level < max_level:
        level += 1
        experience -= needed
        did_level = True
        needed = xp_threshold_for_level(level + 1)
    return level, experience, did_level


def compute_xp_for_next_level(level: int) -> int:
    """XP to advance from ``level`` to ``level + 1`` (per-level increment).

    Thin alias over :func:`xp_threshold_for_level` kept for existing callers; use
    :func:`apply_xp` for the full deduct-and-carry level-up loop.
    """
    return xp_threshold_for_level(level + 1)


def apply_equipment_bonuses(conn: "RRConnection") -> Dict[str, int]:
    """Compute stat bonuses from equipped items.

    Returns dict with keys: "hp", "mana", "strength", "agility", "intellect", "endurance", etc.
    """
    from ..data.item_stat_database import item_stat_database

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None or saved.equipment is None:
        return {}

    equipped = {}
    slot_map = {
        "weapon": 10, "armor": 6, "helmet": 5, "gloves": 2, "boots": 7,
        "shoulders": 8, "shield": 11, "ring1": 3, "ring2": 4, "amulet": 1,
    }
    eq = saved.equipment
    for db_name, slot_id in slot_map.items():
        gc = getattr(eq, db_name, None)
        if gc:
            equipped[slot_id] = gc

    return item_stat_database.get_cumulative_stats(equipped, saved.level or 1)


def refresh_player_state(conn: "RRConnection") -> None:
    """Recompute and persist the player's max HP/MP based on current level and stats."""
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return

    bonuses = apply_equipment_bonuses(conn)
    hp_bonus = bonuses.get("HITPOINTS", 0) or bonuses.get("MAX_HITPOINTS", 0)
    mana_bonus = bonuses.get("MANA", 0) or bonuses.get("MAX_MANA", 0)

    new_max_hp = compute_max_hp(saved.level, saved.stat_endurance, hp_bonus)
    new_max_mp = compute_max_mana(saved.level, saved.stat_intellect, mana_bonus)

    if new_max_hp != saved.max_hp or new_max_mp != saved.max_mana:
        saved.max_hp = new_max_hp
        saved.max_mana = new_max_mp
        # If current HP/MP exceed new max, cap them.
        if saved.current_hp > new_max_hp or saved.current_hp == 0:
            saved.current_hp = new_max_hp
        if saved.current_mana > new_max_mp or saved.current_mana == 0:
            saved.current_mana = new_max_mp
        character_repository.save_character(saved)
        log.info(f"[PlayerState] '{saved.name}' HP={new_max_hp // 256} MP={new_max_mp // 256}")
