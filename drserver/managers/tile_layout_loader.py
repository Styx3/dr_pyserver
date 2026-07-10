"""Loader for Dungeon Runners ``.tile`` files.

Port of C# ``DungeonRunners.Utilities.TileLayoutLoader``. A ``.tile`` is a
GC-script template describing how concrete sub-assets (floor segments, walls,
props) are placed to build one maze cell. This flattens the placement tree into
:class:`TilePlacement` rows that drive per-instance PathMap construction: each
placement's ``extends_path`` resolves to a ``.cobj`` (via
:mod:`drserver.managers.tile_cobj_resolver`), and its ``x``/``y``/``heading``
position that cobj's local-space collision grid into the tile's frame.

``.tile`` structure (excerpted)::

    * extends base.world
    {
        Entities { ... }       // NPC spawn points (usually no .cobj)
        Map
        {
            * extends worldobjectgroup       // container — no Position
            {
                Name = terrain;
                * extends terrain.elmforest.floor.elmforest_floor_40_6
                {
                    Heading = 270;
                    Position = 20,260,50;
                }
            }
        }
    }

Flattening rule (matching the C#): any anonymous block carrying a ``Position``
property is a placement. Containers (no ``Position``) just contribute their
children to the flat list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..data import gc_parser
from ..data.gc_parser import GCNode

# A tile's encounter spawn points are placements extending ``base.Encounter<N>``
# (``N`` = the encounter budget/difficulty hint). They carry a Position like any
# other placement, so the flattener already captures them; this pattern picks
# them back out for the dungeon spawner (the client reads the same markers).
_ENCOUNTER_RE = re.compile(r"(?i)(?:^|\.)encounter(\d*)$")


@dataclass(frozen=True)
class TilePlacement:
    """One placed sub-asset within a tile: its source path + local transform."""

    extends_path: str
    x: float
    y: float
    z: float
    heading: float
    name: str = ""  # the block's ``Name`` property (e.g. a Waypoint's "SpawnPoint")

    @property
    def leaf_name(self) -> str:
        if not self.extends_path:
            return ""
        dot = self.extends_path.rfind(".")
        return self.extends_path if dot < 0 else self.extends_path[dot + 1:]


@dataclass(frozen=True)
class AuthoredAnchor:
    """A designer-placed anchor inside a tile (local tile-frame offset + facing).

    The entrance/portal room tiles bake two such anchors into their ``Entities``
    block: a ``misc.Waypoint`` named ``SpawnPoint`` (where the player materialises
    on arrival) and a ``misc.ZonePortal*`` model (where the inter-level warp gate
    renders). The world position is ``cell.world_origin + (x, y, z)`` — the SAME
    transform the live-verified encounter markers use. Port of the values C#
    ``DungeonMazeSpawner`` hardcoded for elmforest only; here they are read from
    each tile's own content so EVERY theme places correctly."""

    x: float
    y: float
    z: float
    heading: float


@dataclass(frozen=True)
class EncounterMarker:
    """One encounter spawn point within a tile (local tile-frame offset)."""

    x: float
    y: float
    z: float
    heading: float
    budget: int  # the ``N`` in ``base.Encounter<N>`` (0 when unspecified)


@dataclass(frozen=True)
class TileLayout:
    source_path: str
    root_extends: Optional[str]
    placements: Tuple[TilePlacement, ...]

    @property
    def encounter_markers(self) -> Tuple[EncounterMarker, ...]:
        """Encounter spawn points carried by this tile, in file (placement) order."""
        out: List[EncounterMarker] = []
        for p in self.placements:
            m = _ENCOUNTER_RE.search(p.extends_path or "")
            if m is None:
                continue
            out.append(EncounterMarker(
                p.x, p.y, p.z, p.heading, int(m.group(1)) if m.group(1) else 0))
        return tuple(out)

    @property
    def player_spawn_anchor(self) -> Optional[AuthoredAnchor]:
        """The ``misc.Waypoint`` named ``SpawnPoint`` (player arrival), if present.

        Only the entrance tiles (e.g. ``elmforest_undergroundentrance_*``) carry
        one; the bare portal rooms (hub/up/down) do not — there the caller derives
        the player position from the portal anchor. Prefers an exact ``SpawnPoint``
        name, falling back to any ``misc.Waypoint``."""
        fallback: Optional[AuthoredAnchor] = None
        for p in self.placements:
            if p.leaf_name.lower() != "waypoint":
                continue
            anchor = AuthoredAnchor(p.x, p.y, p.z, p.heading)
            if p.name.lower() == "spawnpoint":
                return anchor
            if fallback is None:
                fallback = anchor
        return fallback

    @property
    def zone_portal_anchor(self) -> Optional[AuthoredAnchor]:
        """The ``misc.ZonePortal*`` model placement (warp-gate position), if present.

        Matches every theme's portal variant (``ZonePortal_agg`` /
        ``zoneportal_elite`` / ``ZonePortal_hub`` …) by the case-insensitive
        ``zoneportal`` substring. Returns the first one in file order."""
        for p in self.placements:
            if "zoneportal" in p.leaf_name.lower():
                return AuthoredAnchor(p.x, p.y, p.z, p.heading)
        return None


def _try_parse_vector3(raw: str) -> Optional[Tuple[float, float, float]]:
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 3:
        return None
    try:
        return float(parts[0].strip()), float(parts[1].strip()), float(parts[2].strip())
    except ValueError:
        return None


def _collect(node: Optional[GCNode], output: List[TilePlacement]) -> None:
    if node is None:
        return

    if node.is_anonymous and node.extends and node.has_property("Position"):
        vec = _try_parse_vector3(node.get_string("Position"))
        if vec is not None:
            x, y, z = vec
            heading = node.get_float("Heading", 0.0)
            name = node.get_string("Name")
            output.append(TilePlacement(node.extends, x, y, z, heading, name))

    for child in node.anonymous_children:
        _collect(child, output)
    for child in node.children.values():
        _collect(child, output)


# Cache of a base tile's flattened placements, keyed by its dotted ``extends``
# path — base shells (cave_small_exit_1n, …) are shared by many derived tiles.
_base_placement_cache: dict = {}
_MAX_EXTENDS_DEPTH = 6  # guard against cycles / deep chains


def _load_base_placements(extends: Optional[str], depth: int) -> List[TilePlacement]:
    """Flattened placements inherited from a tile's ``extends`` base, recursively.

    A maze tile (e.g. ``cave_small_hub_1n``) carries only its portal + stairs and
    ``extends`` a base shell (``tiles.cave_small.portals.base.cave_small_exit_1n``)
    that holds the room WALLS/FLOOR and the authored ``SpawnPoint`` waypoint.
    Without resolving this the pathmap had no walls (floor-snap useless) and the
    player spawn fell back to a guessed offset. Terminal bases (``base.World``)
    resolve to no file and end the chain. Elmforest tiles ``extends base.World``
    directly (geometry inline) so they're unaffected."""
    if not extends or depth >= _MAX_EXTENDS_DEPTH:
        return []
    if extends in _base_placement_cache:
        return _base_placement_cache[extends]
    from . import tile_cobj_resolver
    path = tile_cobj_resolver.resolve_extends_path(extends)
    if path is None:
        _base_placement_cache[extends] = []
        return []
    base_root = gc_parser.parse_file(path)
    out: List[TilePlacement] = []
    if base_root is not None:
        out.extend(_load_base_placements(base_root.extends, depth + 1))
        _collect(base_root, out)
    _base_placement_cache[extends] = out
    return out


def load(file_path: str) -> TileLayout:
    root = gc_parser.parse_file(file_path)
    if root is None:
        raise ValueError(f"GCParser returned None for {file_path}")
    # Inherited base shell first (walls/floor/SpawnPoint), then this tile's own
    # additions (portal, stairs) — both share the tile-local coordinate frame.
    placements: List[TilePlacement] = list(_load_base_placements(root.extends, 0))
    _collect(root, placements)
    return TileLayout(file_path, root.extends, tuple(placements))


def load_from_text(text: str, source_name: str = "") -> TileLayout:
    root = gc_parser.parse(text, source_name)
    if root is None:
        raise ValueError("GCParser returned None")
    placements: List[TilePlacement] = []
    _collect(root, placements)
    return TileLayout(source_name, root.extends, tuple(placements))
