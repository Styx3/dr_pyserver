"""Importer: authored NPC placements for the connecting-hub zones → ``npcs`` table.

Source-of-truth (Tier 2, extracted client content): every public ``*.world``
places its NPCs in the ``Entities`` block as anonymous nodes that ``extend`` an
NPC gc type (``world.*.npc.*`` / ``world.*.NPC_*``) and carry ``Position`` /
``Heading`` / ``HitPoints`` / ``ManaPoints``. The server populates a zone from
the ``npcs`` table keyed by ``zone_type`` (= the zone name); ``town`` /
``tutorial`` / ``pvp_start`` already have their (manually curated) rows, but the
connecting hubs were never imported, so ``thehub`` and ``pvp_hub`` arrive empty.

This module imports the hubs the client actually ships with NPCs:

* ``thehub``   (``TheHub.world``)   — HubVendor, TestArmorVendor, QuestGiver, Well
* ``pvp_hub``  (``PVP_hub.world``)  — HubVendor

The 13 dungeon-portal sub-hubs (``thehubportals_dungeon*``, ``thehub_oldlinks``,
``bughub``) author **zero** NPCs in client content and are deliberately left
empty — faithful to what the client ships (user decision 2026-06-21).

**Town is left exactly as the client/C# ship it (24 placed NPCs).** An earlier
revision also emitted the town NPC *catalog* entries that have
``world/town/npc/*.gc`` defs but that ``town.world`` never **places** (Amazon1,
Gnome1/2, Patrice, TokenJewelry, VendorTURD). The client positions those NPCs
**nowhere** (no ``.world`` places them), so any coordinate was fabricated and
"misplaced" — they diverged from C# (which loads the same 24 town rows). They are
removed here and actively deleted from the table on import (see
``DEPRECATED_TOWN_NPCS``) so a stale DB self-heals back to the faithful set.

The import is **add-only** for placements: existing rows (incl. the curated
town/tutorial/pvp ones, whose display names differ from the ``.gc`` ``Name``)
are never rewritten.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..core import log
from .gc_parser import GCNode, parse_file


@dataclass(frozen=True)
class NPCPlacement:
    """One authored (or fabricated) NPC placement destined for the ``npcs`` table."""

    gc_type: str
    zone_type: str
    name: str
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    hit_points: int   # verbatim ×256 wire units, matching the existing rows
    mana_points: int


# ── .world → zone_type for the connecting hubs the client ships with NPCs ──
HUB_WORLDS: dict[str, str] = {
    "TheHub.world": "thehub",
    "PVP_hub.world": "pvp_hub",
}

# The public non-hub worlds whose NPCs the C# DB shipped pre-placed. For a
# from-zero build these must come from the same authored ``.world`` placements
# (24 + 8 + 4 = 36 rows, matching the live table). Import stays add-only, so a DB
# that already has the curated town/tutorial/pvp rows keeps them untouched.
PUBLIC_WORLDS: dict[str, str] = {
    "town.world": "town",
    "Tutorial.world": "tutorial",
    "pvp_start.world": "pvp_start",
}

# Hub vendor ``.gc`` files (relative to the extracter root) that carry a
# ``Merchant`` block but live OUTSIDE the ``world/<zone>/npc/`` tree the standard
# merchant discovery scans (their gc type also lacks the ``.npc.`` segment), so
# they must be registered explicitly to actually open a shop on click.
HUB_VENDOR_FILES: Tuple[Tuple[str, str, str], ...] = (
    ("world/test/NPC_HubVendor.gc", "world.test.NPC_HubVendor", "HubVendor"),
    ("world/test/NPC_TestArmorVendor.gc",
     "world.test.NPC_TestArmorVendor", "TestArmorVendor"),
)

# Town NPC catalog entries that have ``world/town/npc/*.gc`` defs but that the
# client PLACES nowhere (no ``.world`` positions them). A prior revision spawned
# them at fabricated coordinates → "misplaced" + diverged from C# (24 town rows).
# They are deleted from ``npcs`` on import so a stale DB self-heals to the
# faithful, client-matching town set. (Reinstate by authoring real placements,
# never invented coordinates.)
DEPRECATED_TOWN_NPCS: Tuple[str, ...] = (
    "world.town.npc.TokenJewelry",
    "world.town.npc.Amazon1",
    "world.town.npc.VendorTURD",
    "world.town.npc.Patrice",
    "world.town.npc.Gnome1",
    "world.town.npc.Gnome2",
)


def _npc_name(gc_type: str) -> str:
    """Display label = the gc type's leaf, minus a leading ``NPC_`` prefix
    (``world.test.NPC_HubVendor`` → ``HubVendor``)."""
    leaf = gc_type.rsplit(".", 1)[-1]
    return leaf[4:] if leaf.startswith("NPC_") else leaf


def _parse_position(raw: str) -> Tuple[float, float, float]:
    parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
    while len(parts) < 3:
        parts.append("0")
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return 0.0, 0.0, 0.0


def _is_npc_placement(node: GCNode) -> bool:
    """True for an ``Entities`` node that places an NPC: it ``extend``s an NPC gc
    type (``.npc`` segment, covering both ``.npc.`` and ``.NPC_``) and carries a
    ``Position``. Excludes ``world.checkpoints.*`` (handled by ``checkpoints``)."""
    ext = (node.extends or "").lower()
    if ".npc" not in ext or "checkpoint" in ext:
        return False
    return node.has_property("Position")


def parse_world_npc_placements(world_path: str, zone_type: str) -> List[NPCPlacement]:
    """Collect every authored NPC placement from a ``*.world`` file."""
    node = parse_file(world_path)
    if node is None:
        return []
    out: List[NPCPlacement] = []

    def walk(n: GCNode) -> None:
        if _is_npc_placement(n):
            px, py, pz = _parse_position(n.get_string("Position"))
            out.append(NPCPlacement(
                gc_type=n.extends or "",
                zone_type=zone_type,
                name=_npc_name(n.extends or ""),
                pos_x=px, pos_y=py, pos_z=pz,
                heading=n.get_float("Heading"),
                hit_points=n.get_int("HitPoints"),
                mana_points=n.get_int("ManaPoints"),
            ))
        for child in list(n.children.values()) + n.anonymous_children:
            walk(child)

    walk(node)
    return out


def collect_new_npcs(extracter_root: str) -> List[NPCPlacement]:
    """Authored NPC placements for the hubs (``thehub``/``pvp_hub``) and the public
    ``town``/``tutorial``/``pvp_start`` worlds — every placement the client ships,
    from real ``.world`` coordinates (never fabricated). Add-only downstream, so
    curated existing rows are preserved."""
    out: List[NPCPlacement] = []
    for world_file, zone_type in {**HUB_WORLDS, **PUBLIC_WORLDS}.items():
        path = os.path.join(extracter_root, world_file)
        if os.path.isfile(path):
            out.extend(parse_world_npc_placements(path, zone_type))
        else:
            log.warn(f"[world_npc] missing world: {path}")
    return out


def register_hub_vendor_merchants(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Add-only registration of the hub vendors as functional merchants.

    Reuses the merchant ``.gc`` parser + writer; skips any vendor already in the
    ``merchants`` table so the existing client-merchant rows are untouched.
    Returns the number of merchants added.
    """
    from .merchants_importer import (
        _ensure_columns, _write_merchant, parse_merchant_block,
    )

    _ensure_columns(conn)
    existing = {
        (row[0] or "").lower()
        for row in conn.execute("SELECT npc_gc_type FROM merchants")
    }
    next_id = (conn.execute("SELECT COALESCE(MAX(id),0) FROM merchants")
               .fetchone()[0] or 0) + 1

    added = 0
    for rel_path, gc_type, name in HUB_VENDOR_FILES:
        if gc_type.lower() in existing:
            continue
        path = os.path.join(extracter_root, rel_path.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="latin-1") as fh:
                text = fh.read()
        except OSError:
            continue
        if "extends Merchant" not in text:
            continue
        md = parse_merchant_block(text, gc_type, name)
        if md is None:
            continue
        _write_merchant(conn, md, next_id)
        existing.add(gc_type.lower())
        next_id += 1
        added += 1
        log.info(f"[world_npc] registered hub merchant {gc_type} id={next_id - 1}")
    return added


# ── NPC teleporters (on-NPC ``Teleporter extends NPCTeleporter`` components) ──
#
# An NPC can carry a single ``Teleporter`` component (client class ``NPCTeleporter``)
# that adds a "Teleport to X" option to its dialog; clicking it sends QM inbound op
# 0x08 ``u32 npcEntityId`` (bible §13.5 #8). The Zone/SpawnPoint authored in that
# block is NOT in the curated ``npcs`` rows, so it lives in this additive companion
# table keyed by the NPC's path-derived gc type. Only ``world/**/npc/*.gc`` defs are
# scanned (the lone shipped user is ``world.town.npc.SnowMan1`` → Snowman Sanctuary;
# the ``world_nci`` TestTeleporter is a world NCI, handled in ``movement.py``).

@dataclass(frozen=True)
class NPCTeleporterRow:
    gc_type: str       # path-derived dotted gc type, e.g. world.town.npc.SnowMan1
    zone: str          # authored Zone (e.g. dungeon_snowman)
    spawn_point: str   # authored SpawnPoint (e.g. start)
    label: str         # dialog label (e.g. "Teleport to Snowman Sanctuary")


def _path_to_gc_type(extracter_root: str, path: str) -> str:
    """``<root>/world/town/npc/SnowMan1.gc`` → ``world.town.npc.SnowMan1`` (same
    path→dotted convention as ``creatures_importer`` / ``gc_database``)."""
    rel = os.path.relpath(path, extracter_root)
    if rel.lower().endswith(".gc"):
        rel = rel[:-3]
    return rel.replace(os.sep, ".").replace("/", ".")


def _find_npc_teleporter_node(node: GCNode) -> Optional[GCNode]:
    """DFS for the first descendant that ``extends`` ``NPCTeleporter``."""
    stack = [node]
    while stack:
        n = stack.pop()
        if "npcteleporter" in (n.extends or "").lower():
            return n
        stack.extend(n.children.values())
        stack.extend(n.anonymous_children)
    return None


def parse_npc_teleporters(extracter_root: str) -> List[NPCTeleporterRow]:
    """Scan ``world/**/npc/*.gc`` for NPCs with a ``Teleporter extends
    NPCTeleporter`` block and collect their authored destinations."""
    out: List[NPCTeleporterRow] = []
    world_root = os.path.join(extracter_root, "world")
    for dirpath, _dirs, files in os.walk(world_root):
        if "npc" not in (p.lower() for p in dirpath.split(os.sep)):
            continue
        for fn in files:
            if not fn.lower().endswith(".gc"):
                continue
            path = os.path.join(dirpath, fn)
            try:                                  # cheap prefilter before parsing
                with open(path, encoding="latin-1") as fh:
                    if "NPCTeleporter" not in fh.read():
                        continue
            except OSError:
                continue
            node = parse_file(path)
            if node is None:
                continue
            tnode = _find_npc_teleporter_node(node)
            if tnode is None:
                continue
            zone = tnode.get_string("Zone").strip()
            if not zone:
                continue
            out.append(NPCTeleporterRow(
                gc_type=_path_to_gc_type(extracter_root, path),
                zone=zone,
                spawn_point=tnode.get_string("SpawnPoint").strip(),
                label=tnode.get_string("Label").strip(),
            ))
    return out


def import_npc_teleporters(conn: sqlite3.Connection, extracter_root: str) -> int:
    """(Re)build the additive ``npc_teleporters`` companion table. Idempotent
    (``INSERT OR REPLACE``); leaves the curated ``npcs`` table untouched. Returns
    the number of teleporter rows written."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS npc_teleporters ("
        " gc_type TEXT PRIMARY KEY, zone TEXT NOT NULL,"
        " spawn_point TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '')")
    rows = parse_npc_teleporters(extracter_root)
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO npc_teleporters"
            " (gc_type, zone, spawn_point, label) VALUES (?,?,?,?)",
            (r.gc_type, r.zone, r.spawn_point, r.label))
        log.info(f"[npc_teleport] {r.gc_type} -> {r.zone}/{r.spawn_point}")
    return len(rows)


def import_world_npcs(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Add-only import of hub + town-catalog NPCs into ``npcs``.

    Existing ``(gc_type, zone_type)`` rows are skipped, so re-running is
    idempotent and the curated town/tutorial/pvp rows are never rewritten.
    Returns the number of NPC rows added. Also deletes the deprecated fabricated
    town NPCs (self-heal to the client-matching set) and registers the hub
    vendors as merchants. The caller owns the commit.
    """
    removed = conn.executemany(
        "DELETE FROM npcs WHERE zone_type='town' AND gc_type=?",
        [(gc,) for gc in DEPRECATED_TOWN_NPCS]).rowcount
    if removed:
        log.info(f"[world_npc] removed {removed} deprecated fabricated town NPC(s)")

    placements = collect_new_npcs(extracter_root)
    existing = {
        ((row[0] or "").lower(), (row[1] or "").lower())
        for row in conn.execute("SELECT gc_type, zone_type FROM npcs")
    }
    next_id = (conn.execute("SELECT COALESCE(MAX(id),0) FROM npcs")
               .fetchone()[0] or 0) + 1

    added = 0
    for p in placements:
        key = (p.gc_type.lower(), p.zone_type.lower())
        if key in existing:
            continue
        conn.execute(
            "INSERT INTO npcs (id, zone_type, gc_type, name, pos_x, pos_y,"
            " pos_z, heading, hit_points, mana_points)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (next_id, p.zone_type, p.gc_type, p.name, p.pos_x, p.pos_y,
             p.pos_z, p.heading, p.hit_points, p.mana_points))
        existing.add(key)
        next_id += 1
        added += 1
        log.info(f"[world_npc] +{p.zone_type} {p.gc_type} @"
                 f"({p.pos_x:.0f},{p.pos_y:.0f},{p.pos_z:.0f})")

    merchants_added = register_hub_vendor_merchants(conn, extracter_root)
    teleporters = import_npc_teleporters(conn, extracter_root)
    log.info(f"[world_npc] added {added} NPC rows, {merchants_added} hub merchants, "
             f"{teleporters} NPC teleporter(s)")
    return added
