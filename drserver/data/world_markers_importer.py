"""Import the world-marker tables from the client ``*.world`` entity placements.

Three tables the runtime reads (``managers/portals.py``, ``managers/checkpoints.py``)
but that no importer produced — they were inherited from the C# DB dump:

* ``zone_portals``     — ``* extends misc.ZonePortal*`` entities (zone transitions).
* ``zone_waypoints``   — ``* extends misc.Waypoint*`` entities (named world points).
* ``zone_checkpoints`` — ``* extends world.checkpoints.*`` entities (per-zone obelisk).

Each marker is a positioned block inside a ``*.world`` file::

    * extends misc.ZonePortal_agg
    {
        Width = 70; Height = 60;
        Zone = dungeon01_level01;  SpawnPoint = dungeon_spawn;
        Heading = -10;  Position = -30,-692,70;  Name = toDungeon;
    }

The owning ``zone`` is the ``*.world`` file stem (lowercased) — matches the live
rows (``bughub``, ``dungeon02_level00`` …). A portal's minimap ``color`` is not in
the ``.world`` block; it lives on the ``misc/ZonePortal*.gc`` class (``Color = 0x…``),
resolved here through ``extends`` (``ZonePortal_agg extends ZonePortal``).

NB: the separate ``checkpoints`` *recall-menu* table (the obelisk destination list)
is a hand-curated set, not expressed in ``.world``, so it is not produced here.
"""
from __future__ import annotations

import glob
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

from .gc_parser import GCNode, parse_file

_PORTAL_PREFIX = "misc.zoneportal"
_WAYPOINT_PREFIX = "misc.waypoint"
_CHECKPOINT_PREFIX = "world.checkpoints"


def _vec3(raw: str) -> Optional[Tuple[float, float, float]]:
    parts = (raw or "").split(",")
    if len(parts) != 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def portal_colors(extracter_root: str) -> Dict[str, int]:
    """``"misc.<class>"(lower) -> ARGB int`` for every ``misc/ZonePortal*.gc``.

    ``Color`` (``0xAARRGGBB``) is resolved through the ``extends`` chain so a
    subclass that omits it inherits the base ``ZonePortal`` color.
    """
    raw: Dict[str, Tuple[Optional[str], Optional[int]]] = {}
    for fp in glob.glob(os.path.join(extracter_root, "misc", "ZonePortal*.gc")):
        node = parse_file(fp)
        if node is None or not node.name:
            continue
        color: Optional[int] = None
        if node.has_property("Color"):
            try:
                color = int(node.get_string("Color"), 0) & 0xFFFFFFFF
            except ValueError:
                color = None
        raw[node.name.lower()] = (node.extends, color)

    def resolve(name: str, seen: Optional[set] = None) -> Optional[int]:
        seen = seen or set()
        if name in seen or name not in raw:
            return None
        seen.add(name)
        ext, color = raw[name]
        if color is not None:
            return color
        return resolve(ext.split(".")[-1].lower(), seen) if ext else None

    return {f"misc.{name}": (resolve(name) or 0) for name in raw}


def _walk(node: GCNode, zone: str, colors: Dict[str, int],
          portals: List[tuple], waypoints: List[tuple], checkpoints: List[tuple]) -> None:
    for child in list(node.anonymous_children) + list(node.children.values()):
        ext = child.extends or ""
        el = ext.lower()
        if child.has_property("Position"):
            vec = _vec3(child.get_string("Position"))
            if vec is not None:
                heading = child.get_float("Heading", 0.0)
                name = child.get_string("Name")
                if el.startswith(_PORTAL_PREFIX):
                    portals.append((
                        zone, name, ext, vec[0], vec[1], vec[2], heading,
                        child.get_int("Width", 0), child.get_int("Height", 0),
                        child.get_string("Zone"), child.get_string("SpawnPoint"),
                        colors.get(el, 0),
                    ))
                elif el.startswith(_WAYPOINT_PREFIX):
                    waypoints.append((zone, name, vec[0], vec[1], vec[2], heading))
                elif el.startswith(_CHECKPOINT_PREFIX):
                    checkpoints.append((zone, name, ext, vec[0], vec[1], vec[2], heading))
        _walk(child, zone, colors, portals, waypoints, checkpoints)


def import_world_markers(conn: sqlite3.Connection, extracter_root: str) -> int:
    """(Re)populate zone_portals / zone_waypoints / zone_checkpoints from every root
    ``*.world`` file. Returns the total number of marker rows written."""
    for table in ("zone_portals", "zone_waypoints", "zone_checkpoints"):
        conn.execute(f"DELETE FROM {table}")

    colors = portal_colors(extracter_root)
    portals: List[tuple] = []
    waypoints: List[tuple] = []
    checkpoints: List[tuple] = []
    for fp in sorted(glob.glob(os.path.join(extracter_root, "*.world"))):
        node = parse_file(fp)
        if node is None:
            continue
        zone = os.path.splitext(os.path.basename(fp))[0].lower()
        _walk(node, zone, colors, portals, waypoints, checkpoints)

    conn.executemany(
        "INSERT INTO zone_portals (zone, name, gc_type, pos_x, pos_y, pos_z, heading,"
        " width, height, target_zone, spawn_point, color)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", portals)
    conn.executemany(
        "INSERT INTO zone_waypoints (zone, name, pos_x, pos_y, pos_z, heading)"
        " VALUES (?,?,?,?,?,?)", waypoints)
    conn.executemany(
        "INSERT INTO zone_checkpoints (zone, name, gc_type, pos_x, pos_y, pos_z, heading)"
        " VALUES (?,?,?,?,?,?,?)", checkpoints)
    return len(portals) + len(waypoints) + len(checkpoints)
