"""Fill ``creature_manipulators`` for creatures the legacy table never covered.

The legacy ``creature_manipulators`` table (built by a now-absent tool) covers
~1189 creatures. The bible §14.4 boss/leader/base imports added ~211 new
spawnable creatures (``world.*.mob.boss`` entities, per-species bases, master/
quest mobs) that have NO manipulator rows — so ``manipulators_for`` falls back to
the generic invisible melee weapon: the dungeon06 boss + its gun/axe guards spawn
WEAPONLESS and the bosses have none of their skills.

This importer resolves each MISSING creature's merged ``Manipulators`` block from
the ``.gc`` content (the inherited ``PrimaryWeapon`` + its ``CreatureSkill*`` /
``AttackSkill*`` blocks) and inserts rows in the same shape the reader expects
(``slot, gc_type, slot_type=ID, weapon_class, weapon_range``). Existing rows are
left untouched (additive), so no working creature changes.

A weapon's ``WeaponClass`` (``1HMELEE`` / ``2HRANGED`` / …) drives the wire range
hint (ranged → 90, melee → 8) the reader uses to classify a primary weapon whose
native root it can't resolve locally.
"""
from __future__ import annotations

import glob
import os
import sqlite3
from typing import Dict, Optional

from .creatures_importer import _build_chassis_db, _file_dotted
from .gc_database import GCDatabase
from .gc_parser import GCNode, parse_file

_RANGED_RANGE = "90"
_MELEE_RANGE = "8"


def _build_node_map(extracter_root: str, db: GCDatabase) -> Dict[str, GCNode]:
    """Map every spawnable creature dotted path (lowercased) → its raw node:
    the ``creatures/`` tree (already in ``db``) plus the ``world/*/mob/`` entity
    files (boss/leader/quest mobs, which live outside ``creatures/``)."""
    nodes: Dict[str, GCNode] = {}
    pattern = os.path.join(extracter_root, "world", "*", "mob", "**", "*.gc")
    for fp in glob.glob(pattern, recursive=True):
        top = parse_file(fp)
        if top is None or not top.name:
            continue
        dotted = _file_dotted(extracter_root, fp)
        nodes[dotted.lower()] = top
        for child in top.children.values():
            nodes[f"{dotted}.{child.name}".lower()] = child
    return nodes


def _weapon_class(db: GCDatabase, weapon_gc: str) -> str:
    """The flattened ``WeaponClass`` of a weapon gc (``1HMELEE`` / ``2HRANGED`` /
    …), or ``""`` if unresolvable."""
    node = db.resolve(weapon_gc)
    if node is None:
        return ""
    desc = db.flatten(node).get_child("Description")
    return (desc.get_string("WeaponClass") if desc is not None else "") or ""


def _manip_rows_for(db: GCDatabase, creature_gc: str, node: GCNode) -> list[dict]:
    """Resolve one creature's merged ``Manipulators`` block into table rows."""
    manips = db.flatten(node).get_child("Manipulators")
    if manips is None:
        return []
    rows: list[dict] = []
    children = list(manips.children.items()) + [
        (c.name, c) for c in manips.anonymous_children if c.name]
    for slot_name, child in children:
        gc = child.extends or ""
        if not gc:
            continue
        slot = (slot_name or "").lower()
        manip_id = child.get_int("ID") if child.has_property("ID") else 0
        wclass = _weapon_class(db, gc) if "weapon" in slot else ""
        wrange = (_RANGED_RANGE if "RANGED" in wclass.upper()
                  else _MELEE_RANGE) if "weapon" in slot else "0"
        rows.append({
            "creature_gc_type": creature_gc, "slot": slot,
            "gc_type": gc, "slot_type": str(manip_id),
            "weapon_class": wclass, "weapon_range": wrange,
        })
    return rows


def import_missing_creature_manipulators(
        conn: sqlite3.Connection, extracter_root: str) -> int:
    """Insert manipulator rows for every creature in ``creatures`` that has none.
    Returns the number of rows inserted. Additive — existing rows are kept."""
    try:
        have = {r[0].lower() for r in conn.execute(
            "SELECT DISTINCT creature_gc_type FROM creature_manipulators")}
        creatures = [r[0] for r in conn.execute("SELECT gc_type FROM creatures")]
    except sqlite3.Error:
        return 0

    db = _build_chassis_db(extracter_root)
    node_map = _build_node_map(extracter_root, db)
    rows: list[dict] = []
    for gc in creatures:
        key = (gc or "").lower()
        if not key or key in have:
            continue
        node: Optional[GCNode] = node_map.get(key) or db.resolve(gc)
        if node is None:
            continue
        rows.extend(_manip_rows_for(db, gc, node))

    if rows:
        conn.executemany(
            "INSERT INTO creature_manipulators "
            "(creature_gc_type, slot, gc_type, slot_type, weapon_class, "
            "weapon_range) VALUES "
            "(:creature_gc_type, :slot, :gc_type, :slot_type, :weapon_class, "
            ":weapon_range)", rows)
    return len(rows)
