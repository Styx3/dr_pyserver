"""Per-creature manipulators for the monster spawn stream.

The client-simulated monster brain attacks with whatever manipulators the spawn
stream declares: a Melee brain swings its ``PrimaryWeapon``, a Ranged brain
fires it (projectile), a Caster brain casts its ``CreatureBolt`` skill. Ground
truth is the creature content (``extracter/creatures/**``): every creature's
``Manipulators`` block names its concrete weapon/skill objects, mirrored into
the ``creature_manipulators`` table at import.

Previously every monster was sent ONE hardcoded generic melee weapon
(``creatures.base.weapons.melee``), so ranged mobs had no bow to fire and
casters had no bolt to cast — they could only run into the player. This module
resolves each creature's own manipulators:

Selection is by NATIVE ROOT CLASS, not by slot name: every authored row whose
gc walks (via the ``extends`` chain in the client content) to ``ActiveSkill``
is sent as a skill block (covers creaturebolt / primaryskill / skill1 / skill2
/ attackskill1 / specialattack / charge / aura / procs / summons — a 2026-06-11
audit of all 16 authored slots found every skill slot roots in ActiveSkill);
``MeleeWeapon`` / ``RangedWeapon`` roots are sent as weapon blocks (matching
the C# ``IsRangedManipulator`` projectile check but from authoritative
content; an unresolvable weapon chain falls back to the imported ``Range``
stat — melee authors 8, ranged 90). Rows rooting anywhere else are SKIPPED:
``AttributeModifier`` (attribmod1) and ``base.Effect`` (impacteffect) are not
Manipulators — the client's bless check (FUN_004fd050 vs the Manipulator class
bitmask) would reject them and leave their body bytes unread, desyncing the
stream. Skills/weapons live-verified 2026-06-11 (bolts, rifles, strike skills
all fire in dungeon05).

Wire layouts per kind, all client-verified 2026-06-11 against the classes'
readData (vtable+0xf0) + readState (vtable+0x100) pairs — the manipulator
reader (FUN_004fd050) invokes BOTH per manipulator. ActiveSkill =
FUN_0053dfb0 + FUN_00539ba0 (the C# 5-byte shape MISSED the readState flags
byte and froze zone loads); weapons share Weapon::readData FUN_00581710
(u32 id, 5 bytes, flags, mod-count) and differ in readState (melee
FUN_005923c0 has an extra byte at +0x8d vs ranged FUN_00596280); melee is
also live-proven. Trailing u16 0 = no target → skips the conditional
target/position block:

  ActiveSkill:  <gcType> <id:u32> 0x00 <flags:0x00>
  MeleeWeapon:  <gcType> <id:u32> 6×0x00 <u16:0> 0x00 <u16:0>
  RangedWeapon: <gcType> <id:u32> 6×0x00 <u16:0> <u16:0>
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core import log
from ..db import game_database as db

# Manipulator kinds (wire-body selectors).
KIND_SKILL = "skill"
KIND_MELEE = "melee"
KIND_RANGED = "ranged"

# Fallback when a creature has no imported manipulators: the base melee weapon
# every basic melee unit inherits (creatures/base/UnitMelee.gc: PrimaryWeapon
# extends creatures.base.weapons.melee { ID = 10 }).
DEFAULT_MELEE_GC_TYPE = "creatures.base.weapons.melee"
DEFAULT_MELEE_ID = 10
DEFAULT_MELEE_RANGE = 8.0   # authored creatures/base/weapons/melee.gc Range

# Imported Range above this ⇒ ranged, when the extends chain can't be walked
# (content authors melee Range 8 / ranged Range 90).
_RANGED_RANGE_THRESHOLD = 20.0

# NB the registry lazily loads any content gc by name (FUN_005e2df0 →
# FUN_005e40c0), so nested per-creature objects (bolts, bows, melee_haste)
# resolve exactly like top-level ones. A genuine resolve/bless failure logs
# "Failed to find TypeID" / "Archetype %s is not a Manipulator" to the client's
# logs/DungeonRunners.log — check there first if a zone ever freezes on load.
# (The 2026-06-11 dungeon01-05 freeze was neither: the C#-derived ActiveSkill
# body omitted the readState flags byte, silently over-reading the stream —
# see the module docstring. The bisect gates that hunt left behind were
# removed once the full wiring was live-confirmed across zones.)

_EXTENDS_HOP_LIMIT = 8
_ROOT_EXTENDS_RE = re.compile(r"^\s*([\w]+)\s+extends\s+([\w.]+)", re.MULTILINE)


@dataclass(frozen=True)
class ManipulatorEntry:
    """One manipulator block of the monster spawn stream."""

    gc_type: str
    manip_id: int
    kind: str  # KIND_SKILL | KIND_MELEE | KIND_RANGED
    # Authored weapon Range (content stat, melee 8 / ranged 90), carried on the
    # wire in the weapon body's 4th readData byte (instance field +0x7f) — the
    # zero-range run-through probe; see monsters.py OP5. Skills keep 0.
    weapon_range: float = 0.0


_entry_cache: Dict[str, List[ManipulatorEntry]] = {}
_root_cache: Dict[str, Optional[str]] = {}


def clear_cache() -> None:
    """Drop caches (tests after a DB/content swap)."""
    _entry_cache.clear()
    _root_cache.clear()


def manipulators_for(creature_gc_type: str) -> List[ManipulatorEntry]:
    """The manipulator blocks to send for ``creature_gc_type`` (cached).

    Every imported ``creature_manipulators`` row whose gc roots in a
    Manipulator class with a verified wire shape (ActiveSkill / MeleeWeapon /
    RangedWeapon) is emitted — skills first, weapons last (the live-proven
    order). Non-Manipulator rows (AttributeModifier, Effect, unresolvable
    procs) are skipped. A creature with nothing emittable gets the generic
    base melee weapon (correct for plain melee units)."""
    key = (creature_gc_type or "").lower()
    if key in _entry_cache:
        return _entry_cache[key]

    skills: List[ManipulatorEntry] = []
    weapons: List[ManipulatorEntry] = []
    for row in _load_rows(key):
        gc = row["gc_type"]
        kind = _kind_of(gc, row["slot"], row["range"])
        if kind is None:
            log.debug(f"[CREATURE-MANIP] skip non-manipulator "
                      f"{key}.{row['slot']} = {gc}")
            continue
        entry = ManipulatorEntry(
            gc_type=gc, manip_id=row["id"], kind=kind,
            weapon_range=(0.0 if kind == KIND_SKILL else row["range"]))
        (skills if kind == KIND_SKILL else weapons).append(entry)

    entries = skills + weapons
    if not entries:
        entries = [ManipulatorEntry(
            gc_type=DEFAULT_MELEE_GC_TYPE, manip_id=DEFAULT_MELEE_ID,
            kind=KIND_MELEE, weapon_range=DEFAULT_MELEE_RANGE)]

    _entry_cache[key] = entries
    return entries


def _kind_of(gc_type: str, slot: str, imported_range: float) -> Optional[str]:
    """Wire kind for one authored row, or None to skip it.

    Driven by the gc's native root class; only roots with client-verified
    wire shapes are emittable. A weapon whose extends chain can't be read
    locally falls back to the imported Range stat (melee authors 8,
    ranged 90) — the gc name itself is still authored content the client can
    resolve."""
    root = _native_root_of(gc_type)
    if root == "activeskill":
        return KIND_SKILL
    if root == "meleeweapon":
        return KIND_MELEE
    if root == "rangedweapon":
        return KIND_RANGED
    if root is None and slot == "primaryweapon":
        return (KIND_RANGED if imported_range >= _RANGED_RANGE_THRESHOLD
                else KIND_MELEE)
    return None


def _load_rows(creature_gc_type: str) -> List[Dict]:
    """Authored manipulator rows for one creature, in authored order.

    First row wins per slot (a few creatures author duplicate slots; the
    first is the base loadout) and per gc_type (the same skill can sit in
    two slots, e.g. skill1 + attackskill1 — one manipulator suffices)."""
    out: List[Dict] = []
    seen_slots: set = set()
    seen_gcs: set = set()
    try:
        rows = db.execute_reader(
            "SELECT slot, gc_type, slot_type, weapon_range FROM "
            "creature_manipulators WHERE creature_gc_type = :c COLLATE NOCASE "
            "ORDER BY id", {"c": creature_gc_type}
        ).fetchall()
    except Exception as ex:  # noqa: BLE001 — no table/row ⇒ default loadout
        log.warn(f"[CREATURE-MANIP] load failed for '{creature_gc_type}': {ex}")
        return out
    for r in rows:
        slot = (db.get_string(r, "slot") or "").lower()
        gc_type = db.get_string(r, "gc_type")
        if not slot or not gc_type:
            continue
        if slot in seen_slots or gc_type.lower() in seen_gcs:
            continue
        seen_slots.add(slot)
        seen_gcs.add(gc_type.lower())
        out.append({
            "slot": slot,
            "gc_type": gc_type,
            "id": _parse_int(db.get_string(r, "slot_type")),
            "range": _parse_float(db.get_string(r, "weapon_range")),
        })
    return out


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def _parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value) if value else default
    except (TypeError, ValueError):
        return default


def _native_root_of(gc_type: str) -> Optional[str]:
    """The lowercase native engine class a dotted gc walks to (cached).

    A dotless name is its own class (e.g. the authored ``AttributeModifier``
    row). None = the chain can't be resolved from local content."""
    key = (gc_type or "").lower()
    if key not in _root_cache:
        _root_cache[key] = _resolve_native_root(gc_type)
    return _root_cache[key]


def _resolve_native_root(gc_type: str) -> Optional[str]:
    current = gc_type or ""
    if not current:
        return None
    if "." not in current:
        return current.lower()
    seen = set()
    for _ in range(_EXTENDS_HOP_LIMIT):
        low = current.lower()
        if low in seen:
            return None
        seen.add(low)
        target = _root_extends_of(current)
        if target is None:
            return None
        if "." not in target:
            return target.lower()
        current = target
    return None


def _root_extends_of(dotted: str) -> Optional[str]:
    """The ``extends`` target of a dotted gc object.

    Probes the object's own ``.gc`` file first; weapons declared as NESTED
    blocks (e.g. ``…base.Whisker_Broodling_Weapons.UnarmedWeapon`` inside
    ``Whisker_Broodling_Weapons.gc``) fall back to the parent file, scanned for
    ``<Leaf> extends <target>``."""
    from . import tile_cobj_resolver

    path = tile_cobj_resolver.resolve_extends_path(dotted)
    if path is not None:
        text = _read_text(path)
        if text:
            m = _ROOT_EXTENDS_RE.search(text)
            if m:
                return m.group(2)
        return None

    dot = dotted.rfind(".")
    if dot <= 0:
        return None
    parent, leaf = dotted[:dot], dotted[dot + 1:]
    parent_path = tile_cobj_resolver.resolve_extends_path(parent)
    if parent_path is None:
        return None
    text = _read_text(parent_path)
    if not text:
        return None
    m = re.search(rf"\b{re.escape(leaf)}\s+extends\s+([\w.]+)", text,
                  re.IGNORECASE)
    return m.group(1) if m else None


def _read_text(path: str) -> Optional[str]:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as ex:
        log.warn(f"[CREATURE-MANIP] content read failed '{path}': {ex}")
        return None
