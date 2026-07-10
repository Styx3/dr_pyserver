"""Dungeon maze spawner — data-driven port of C# ``DungeonMazeSpawner``.

Turns a procedurally generated maze (see [[maze]]) into a list of mob spawns
placed across the dungeon's rooms, walking the same cell order as the C#
reference so placement matches the client-rendered rooms.

DATA-DRIVEN (2026-06-08 revalidation)
-------------------------------------
The maze parameters and encounter tables are NO LONGER hand-coded for dungeon00
only. They are imported from the client ``*.world`` / ``*.enc`` content into the
``dungeon_levels`` / ``dungeon_encounters`` tables (see
``drserver.data.dungeon_world_importer``) so EVERY dungeon spawns *its own* mobs
with the real ``world.<dungeon>.mob.*`` asset the client can load. The previous
hand-coded ``melee0N.rankN`` family map only ever matched dungeon00; forcing it
onto e.g. ``dungeon02_level03`` emitted an unloadable type and crashed the client
("Invalid entity type"). See [[project_dungeon_spawn_datadriven]].

Each :class:`DungeonSpawn` carries both the ``entity_gc_type`` (the ``world.*``
asset the client renders) and the ``creature_gc_type`` (the ``creatures.*`` row
the server reads HP/level from for the synch trailer). The monster builder
(:func:`drserver.managers.monsters.build_monsters_from_spawns`) turns these into
create packets.

Determinism note: the C# code jitters group offsets/headings with Unity's global
``Random.Range`` (non-reproducible). We instead derive jitter from a
``DotNetRandom`` seeded off the maze seed so an instance is reproducible across
joiners; this jitter only nudges mobs within their room and never needs to match
the client (the client doesn't predict mob positions — it's told them).
"""
from __future__ import annotations

import os
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core import log
from ..util.dotnet_random import DotNetRandom, to_int32
from .maze import TILE_SIZE, MazeGenerator
from .pathmap import pathmap_manager

# Legacy default maze seed. Still used as the combat room-RNG seed
# (``game_server`` 7/0x0C); the maze *layout* seed is now per-level — see
# :func:`layout_seed`.
MAZE_SEED = 0xBEEFBEEF


def layout_seed(zone_name: str) -> int:
    """Deterministic per-level maze *layout* seed (uint32).

    The client builds its maze from the uint32 the server sends in the
    zone-connect packet (``13/0x00``); the spawner MUST use the same value so
    mobs land on the maze the client actually renders. A stable CRC32 of the
    lowercased zone name means every joiner to one level agrees and different
    levels get different layouts — the old hardcoded ``0xBEEFBEEF`` made every
    level with identical maze params render byte-identical (see
    [[project_dungeon_map_alignment]]). ``zlib.crc32`` is process-stable;
    Python's built-in ``hash`` is salted per-process and must not be used here.

    This is SEPARATE from the combat room-RNG seed (``7/0x0C`` /
    :data:`MAZE_SEED`), which drives the zero-tolerance HP-synch stream and is
    left untouched.
    """
    return zlib.crc32((zone_name or "").lower().encode("utf-8")) & 0xFFFFFFFF

# Hard cap on mobs per generated level (avoids huge encounter tables — e.g.
# dungeon15's 378-group table — flooding a level). Plenty for a populated dungeon.
_MAX_MOBS_PER_LEVEL = 150


@dataclass(frozen=True)
class DungeonSpawn:
    # The client-loadable entity asset (``world.<dungeon>.mob.*`` for maze mobs,
    # or a raw ``creatures.*`` for the legacy static boss arenas).
    gc_type: str
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    # The concrete ``creatures.*`` row for stats. Empty ⇒ "same as gc_type"
    # (the static boss-arena path, where the spawn IS a creature).
    creature_gc_type: str = ""


@dataclass(frozen=True)
class WarpGate:
    """A placed inter-level warp derived from a maze room node.

    A room node carrying a ``link_to_zone`` (``mainentrance`` back-link,
    ``exit`` forward-link, ``shared``/``oneoff`` side-areas) becomes a teleport
    gate at the world centre of the cell the maze actually placed it in. The
    client never predicts these positions (objects are server-told), so the
    gate sits wherever this level's maze dropped the room node — exactly where
    the entrance/exit tile renders.
    """
    source_index: int
    node_kind: str
    tile_set: str
    tile_type: str
    grid_x: int
    grid_y: int
    world_x: float
    world_y: float
    world_z: float
    heading: float
    link_to_zone: str
    link_to_spawn: str
    spawn_name: str


@dataclass(frozen=True)
class _LevelDef:
    """One maze level loaded from ``dungeon_levels``."""
    zone_name: str
    tile_prefix: str
    encounter_table: str
    leader_encounter: str
    maze_width: int
    maze_height: int
    maze_randomness: int
    maze_sparseness: int
    maze_dead_end_removal_chance: int


@dataclass(frozen=True)
class _RoomNode:
    """One ``*.world`` room node (from ``dungeon_room_nodes``).

    Pins a forced room (entrance/exit/quest/leader/boss) into the maze and may
    carry a per-node ``encounter_type`` (a ``world.*.enc.*`` table) and/or a
    warp ``link_to_zone``/``spawn_name`` (the latter consumed by Phase C gates).
    ``grid_x``/``grid_y`` are ``None`` when the maze is free to pick the cell.
    """
    source_index: int
    node_kind: str
    tile_set: str
    grid_x: Optional[int]
    grid_y: Optional[int]
    chance: int
    encounter_type: str
    link_to_zone: str
    link_to_spawn: str
    spawn_name: str


# (entity_gc_type, creature_gc_type, difficulty) for one mob slot in an
# encounter group. ``difficulty`` is the authored ``EncounterUnit.Difficulty``
# (the spawn-budget weight — see :func:`expand_encounter_group`).
_EncUnit = Tuple[str, str, float]
# One encounter = an ordered list of mob slots.
_EncGroup = List[_EncUnit]


@dataclass
class _DungeonLayout:
    """The deterministic maze for one level — built once per ``(zone, seed)``.

    Cheap to hold (cell list + placed-node list); the heavy geometry pathmap is
    built/registered separately per instance. Caching it lets the player-entry
    resolver (run at zone-transfer, before the instance exists), the warp-gate
    builder, and the mob spawner all read the SAME maze without regenerating it.
    """
    zone_name: str
    seed: int
    cells: list                  # List[MazeCell]
    placed_room_nodes: list      # List[PlacedRoomNode]
    room_nodes: List[_RoomNode]  # the .world room-node defs (carry link/spawn)
    cell_by_grid: Dict[Tuple[int, int], object]


# ── DB loaders (cached per process; content is static) ──
_level_cache: Dict[str, Optional[_LevelDef]] = {}
_enc_cache: Dict[str, List[_EncGroup]] = {}
_room_node_cache: Dict[str, List[_RoomNode]] = {}
_layout_cache: Dict[Tuple[str, int], _DungeonLayout] = {}
# Geometry pathmap per (zone, seed). The maze is identical for a given (zone,
# seed), so its floor/wall pathmap is too — built once and shared by the mob
# spawner, the warp-gate floor-snap, AND the player-entry floor-snap (the entry
# is resolved at zone-transfer, before any instance pathmap is registered, so it
# needs a pathmap it can build standalone). ``None`` caches a failed build.
_pathmap_cache: Dict[Tuple[str, int], object] = {}


def _load_level(zone_name: str) -> Optional[_LevelDef]:
    key = (zone_name or "").lower()
    if key in _level_cache:
        return _level_cache[key]
    from ..db import game_database as db
    row = None
    try:
        row = db.execute_reader(
            "SELECT zone_name, tile_prefix, encounter_table, leader_encounter, "
            "maze_width, maze_height, maze_randomness, maze_sparseness, "
            "maze_dead_end_removal_chance FROM dungeon_levels "
            "WHERE zone_name = :z COLLATE NOCASE", {"z": zone_name}
        ).fetchone()
    except Exception as ex:  # noqa: BLE001 — table missing ⇒ no maze levels
        log.warn(f"[MAZE-SPAWN] dungeon_levels query failed for '{zone_name}': {ex}")
        row = None
    level = None
    if row is not None and db.get_int(row, "maze_width") > 0:
        level = _LevelDef(
            zone_name=db.get_string(row, "zone_name"),
            tile_prefix=db.get_string(row, "tile_prefix") or "elmforest_tileset_",
            encounter_table=db.get_string(row, "encounter_table"),
            leader_encounter=db.get_string(row, "leader_encounter"),
            maze_width=db.get_int(row, "maze_width"),
            maze_height=db.get_int(row, "maze_height"),
            maze_randomness=db.get_int(row, "maze_randomness"),
            maze_sparseness=db.get_int(row, "maze_sparseness"),
            maze_dead_end_removal_chance=db.get_int(row, "maze_dead_end_removal_chance"),
        )
    _level_cache[key] = level
    return level


def _load_groups(enc_table: str) -> List[_EncGroup]:
    """Load an encounter table's groups (ordered) from ``dungeon_encounters``."""
    if not enc_table:
        return []
    key = enc_table.lower()
    if key in _enc_cache:
        return _enc_cache[key]
    from ..db import game_database as db
    groups: List[_EncGroup] = []
    try:
        rows = db.execute_reader(
            "SELECT group_idx, unit_idx, entity_gc_type, creature_gc_type, "
            "difficulty FROM dungeon_encounters WHERE enc_table = :e "
            "COLLATE NOCASE ORDER BY group_idx, unit_idx", {"e": key}
        ).fetchall()
    except Exception as ex:  # noqa: BLE001
        log.warn(f"[MAZE-SPAWN] dungeon_encounters query failed for '{enc_table}': {ex}")
        rows = []
    cur_idx: Optional[int] = None
    cur: _EncGroup = []
    for r in rows:
        gidx = db.get_int(r, "group_idx")
        if gidx != cur_idx:
            if cur:
                groups.append(cur)
            cur, cur_idx = [], gidx
        cur.append((db.get_string(r, "entity_gc_type"),
                    db.get_string(r, "creature_gc_type"),
                    db.get_float(r, "difficulty")))
    if cur:
        groups.append(cur)
    _enc_cache[key] = groups
    return groups


def _load_room_nodes(zone_name: str) -> List[_RoomNode]:
    """Load a level's room nodes (ordered by source index) from
    ``dungeon_room_nodes``. Returns ``[]`` when the table or level is absent."""
    key = (zone_name or "").lower()
    if key in _room_node_cache:
        return _room_node_cache[key]
    from ..db import game_database as db
    nodes: List[_RoomNode] = []
    try:
        rows = db.execute_reader(
            "SELECT source_index, node_kind, tile_set, grid_x, grid_y, chance, "
            "encounter_type, link_to_zone, link_to_spawn, spawn_name "
            "FROM dungeon_room_nodes WHERE zone_name = :z COLLATE NOCASE "
            "ORDER BY source_index", {"z": zone_name}
        ).fetchall()
    except Exception as ex:  # noqa: BLE001 — table missing ⇒ no room nodes
        log.warn(f"[MAZE-SPAWN] dungeon_room_nodes query failed for '{zone_name}': {ex}")
        rows = []
    for r in rows:
        gx = r["grid_x"]   # NULL ⇒ None ⇒ "maze picks the column"
        gy = r["grid_y"]
        nodes.append(_RoomNode(
            source_index=db.get_int(r, "source_index"),
            node_kind=db.get_string(r, "node_kind"),
            tile_set=db.get_string(r, "tile_set"),
            grid_x=int(gx) if gx is not None else None,
            grid_y=int(gy) if gy is not None else None,
            chance=db.get_int(r, "chance", 100),
            encounter_type=db.get_string(r, "encounter_type"),
            link_to_zone=db.get_string(r, "link_to_zone"),
            link_to_spawn=db.get_string(r, "link_to_spawn"),
            spawn_name=db.get_string(r, "spawn_name"),
        ))
    _room_node_cache[key] = nodes
    return nodes


def _build_layout(zone_name: str, seed: int) -> Optional[_DungeonLayout]:
    """Generate (or return the cached) deterministic maze for a level.

    Feeds the level's ``*.world`` room nodes into the generator before running
    it (the client does the same, so without them the layout diverges even at
    an identical seed). Returns ``None`` for non-maze zones. The result is keyed
    by ``(zone, seed)`` and reused by the spawner, the warp-gate builder, and the
    player-entry resolver so they all agree on one maze.
    """
    level = _load_level(zone_name)
    if level is None:
        return None
    cache_key = ((zone_name or "").lower(), seed)
    cached = _layout_cache.get(cache_key)
    if cached is not None:
        return cached

    room_nodes = _load_room_nodes(zone_name)
    maze = MazeGenerator(
        level.maze_width, level.maze_height, seed,
        level.maze_randomness, level.maze_sparseness,
        level.maze_dead_end_removal_chance,
    )
    for rn in room_nodes:
        maze.add_room_node(rn.tile_set, rn.grid_x, rn.grid_y, rn.chance,
                           rn.source_index)
    cells = maze.generate(level.tile_prefix)
    layout = _DungeonLayout(
        zone_name=zone_name,
        seed=seed,
        cells=cells,
        placed_room_nodes=list(maze.placed_room_nodes),
        room_nodes=room_nodes,
        cell_by_grid={(c.grid_x, c.grid_y): c for c in cells},
    )
    _layout_cache[cache_key] = layout
    return layout


def _build_pathmap(zone_name: str, seed: int):
    """Build (or return the cached) geometry pathmap for a level's maze.

    Keyed by ``(zone, seed)`` like the layout, because the maze — and therefore
    its walkable floor / wall geometry and per-node ground height — is identical
    for a given seed regardless of which instance renders it. Reused by the mob
    spawner, the warp-gate builder, and the player-entry resolver so all three
    floor-snap against the SAME geometry. Returns ``None`` for non-maze zones or
    when the build fails (missing content)."""
    layout = _build_layout(zone_name, seed)
    if layout is None:
        return None
    key = ((zone_name or "").lower(), seed)
    if key in _pathmap_cache:
        return _pathmap_cache[key]
    from . import pathmap_builder
    pathmap = pathmap_builder.build(zone_name, layout.cells)
    _pathmap_cache[key] = pathmap
    return pathmap


def clear_caches() -> None:
    """Drop the level/encounter/room-node/layout caches (used by tests after a DB swap)."""
    _level_cache.clear()
    _enc_cache.clear()
    _room_node_cache.clear()
    _layout_cache.clear()
    _pathmap_cache.clear()
    _tile_marker_cache.clear()
    _tile_anchor_cache.clear()
    _static_world_cache.clear()
    _static_marker_cache.clear()
    _static_placement_cache.clear()


# Encounter spawn-budget (DungeonMazeSpawner.EncounterDifficultyBudget). One
# encounter marker is filled to a per-spot budget by repeating the chosen
# group's weighted units, so a spot spawns a PACK (3-6 mobs) instead of the
# 1-2 literal units — the original game's behaviour (old-footage groups). Ported
# 1:1 from the C# reference DungeonMazeSpawner.ExpandEncounterGroup.
#
# The C# reference value is 2.25; with the authored 0.25-0.75 unit difficulties
# that yields ~2-4 mobs/spot. ``DR_ENCOUNTER_BUDGET`` overrides it for live
# feel-tuning (raise for denser packs) without a code change.
def _encounter_budget() -> float:
    try:
        return float(os.environ.get("DR_ENCOUNTER_BUDGET", "") or 2.25)
    except ValueError:
        return 2.25


_ENCOUNTER_DIFFICULTY_BUDGET = _encounter_budget()


def _stable_spot_seed(key: str) -> int:
    """FNV-1a (32-bit) hash of a spawn-spot key (C# StableSpotSeed). Gives each
    marker a stable, distinct budget without per-process salt."""
    if not key:
        return 0
    h = 2166136261
    for ch in key:
        h ^= ord(ch) & 0xFFFFFFFF
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _resolve_spot_budget(spot_seed: int) -> float:
    """Per-spot difficulty budget in ``[1.125, 2.25]`` (C# ResolveSpotBudget):
    a hashed fraction of :data:`_ENCOUNTER_DIFFICULTY_BUDGET` so different
    markers get different pack sizes deterministically."""
    h = (spot_seed * 2654435761) & 0xFFFFFFFF
    h ^= h >> 15
    frac = (h & 0xFFFF) / 65535.0
    return _ENCOUNTER_DIFFICULTY_BUDGET * (0.5 + 0.5 * frac)


def expand_encounter_group(group: _EncGroup, spot_seed: int) -> _EncGroup:
    """Expand one encounter group into a full pack for a spawn spot.

    Port of C# ``DungeonMazeSpawner.ExpandEncounterGroup``: every authored unit
    spawns at least once; then weighted units (``difficulty > 0``) are repeated
    round-robin until the spot's hashed difficulty budget is spent. A group of
    1-2 literal units therefore yields a 3-6 mob pack — matching the original
    game instead of our previous one-unit-per-marker output. Units with
    ``difficulty == 0`` (anchors/props) spawn exactly once and never fill.
    """
    result: _EncGroup = []
    weighted: _EncGroup = []
    spent = 0.0
    for unit in group:
        result.append(unit)
        if unit[2] > 0.0:
            weighted.append(unit)
            spent += unit[2]
    if not weighted:
        return result

    spot_budget = max(spent, _resolve_spot_budget(spot_seed))
    min_weight = min(u[2] for u in weighted)
    fill_index = 0
    # Guard against pathological data (all-tiny weights + huge budget): the
    # _MAX_MOBS_PER_LEVEL cap also bounds this, but keep the pack itself sane.
    while spot_budget - spent >= min_weight and len(result) < 16:
        unit = weighted[fill_index % len(weighted)]
        if spent + unit[2] > spot_budget:
            break
        result.append(unit)
        spent += unit[2]
        fill_index += 1
    return result


# Group offsets so mobs at one marker don't stack (DungeonMazeSpawner.GroupOffsets).
_GROUP_OFFSETS = [
    (0.0, 0.0), (60.0, 0.0), (-60.0, 0.0), (0.0, 60.0), (0.0, -60.0),
    (45.0, 45.0), (-45.0, 45.0), (45.0, -45.0), (-45.0, -45.0),
    (90.0, 30.0), (-90.0, -30.0), (30.0, 90.0), (-30.0, -90.0),
]

# Default encounter-area side when a marker carries no ``<N>`` budget (the
# authored sizes run 100–280; 180 sits mid-range and keeps the stock
# ``_GROUP_OFFSETS`` spread usable).
_DEFAULT_ENCOUNTER_SIZE = 180.0
# Never clamp a group tighter than this half-extent (mobs need elbow room).
_MIN_GROUP_HALF = 30.0
# Walkable-search radius for individual group MEMBERS. The group base does the
# big 250-unit spiral once; members only micro-adjust so the pack stays a pack.
_MEMBER_SNAP_RADIUS = 60.0

def _tile_center(tile_size: float = TILE_SIZE) -> Tuple[float, float, float, int]:
    """Geometric centre of a tile (local frame). Last-resort anchor for a forced
    room whose room tile carries no ``base.Encounter`` marker — derived from the
    cell's per-tileset ``tile_size`` (elmforest 400, cave_small 360, …), not a
    hardcoded position. Z is 0 (the pathmap resolves the real ground height at
    emit time); area budget 0 = the default group spread."""
    half = tile_size / 2.0
    return (half, half, 0.0, 0)


# Cache of per-tile-type encounter markers parsed from the ``.tile`` content
# (local-frame (x, y, z, budget) offsets — budget is the ``N`` of
# ``base.Encounter<N>``, the authored encounter-area size). ``[]`` = the tile
# carries no encounter spot (most corridors/dead-ends) ⇒ no generic mob there,
# matching the client.
_tile_marker_cache: Dict[str, List[Tuple[float, float, float, int]]] = {}


def _tile_encounter_markers(tile_type: str) -> List[Tuple[float, float, float, int]]:
    """Real encounter spawn points for a tile, read from its ``.tile`` content.

    The client places encounters at the ``base.Encounter<N>`` markers baked into
    each tile (verified 2026-06-09: ``cat_tileset_1n1s_A`` → (170,170),
    ``cat_tileset_1e1s_A`` → (130,190); stair/room tiles carry none). This makes
    the generic fill faithful to content instead of dropping a mob at every cell
    centre. Returns ``[]`` when the tile has no marker or can't be resolved."""
    if tile_type in _tile_marker_cache:
        return _tile_marker_cache[tile_type]
    from . import tile_cobj_resolver, tile_layout_loader
    markers: List[Tuple[float, float, float, int]] = []
    path = tile_cobj_resolver.resolve_tile_path(tile_type)
    if path is not None:
        try:
            layout = tile_layout_loader.load(path)
            markers = [(m.x, m.y, m.z, m.budget) for m in layout.encounter_markers]
        except Exception as ex:  # noqa: BLE001 — bad/partial tile ⇒ no markers
            log.warn(f"[MAZE-SPAWN] encounter-marker parse failed for "
                     f"'{tile_type}': {ex}")
    _tile_marker_cache[tile_type] = markers
    return markers


# Cache of per-tile-type authored anchors (player SpawnPoint + ZonePortal model),
# parsed from the ``.tile`` content. Value is ``(player, portal)``; either may be
# ``None`` (corridors carry neither; bare portal rooms carry only the portal).
_tile_anchor_cache: Dict[str, Tuple[Optional[object], Optional[object]]] = {}

def _tile_authored_anchors(tile_type: str):
    """``(player_spawn, zone_portal)`` authored anchors for a tile, or ``(None, None)``.

    Read from the tile's ``.tile`` content INCLUDING its inherited base shell
    (see :func:`tile_layout_loader.load` — maze tiles ``extends`` a base tile that
    carries the room walls/floor + the authored ``misc.Waypoint`` SpawnPoint). The
    player anchor is that SpawnPoint waypoint; the portal anchor is the
    ``misc.ZonePortal*`` model. Both are designer-placed (data, not guessed)."""
    if tile_type in _tile_anchor_cache:
        return _tile_anchor_cache[tile_type]
    from . import tile_cobj_resolver, tile_layout_loader
    player = portal = None
    path = tile_cobj_resolver.resolve_tile_path(tile_type)
    if path is not None:
        try:
            layout = tile_layout_loader.load(path)
            player = layout.player_spawn_anchor
            portal = layout.zone_portal_anchor
        except Exception as ex:  # noqa: BLE001 — bad/partial tile ⇒ no anchors
            log.warn(f"[MAZE-SPAWN] authored-anchor parse failed for "
                     f"'{tile_type}': {ex}")
    result = (player, portal)
    _tile_anchor_cache[tile_type] = result
    return result


def _authored_spawn(tile_type: str) -> Optional[Tuple[float, float, float, float]]:
    """Authored local ``(x, y, z, heading)`` for the player's arrival, or ``None``.

    Fully DATA-DRIVEN — no guessed offsets, no hardcoded lift. The player
    materialises exactly at the tile's authored ``misc.Waypoint`` SpawnPoint
    (every one of the 66 entrance tiles carries one in its base shell, verified
    2026-06-09). If a tile somehow has no SpawnPoint, falls back to the authored
    ZonePortal anchor (the warp's own designer-placed position — still data).
    ``None`` only when the tile resolves no anchor at all (caller keeps centre)."""
    player, portal = _tile_authored_anchors(tile_type)
    anchor = player or portal
    if anchor is None:
        return None
    return (anchor.x, anchor.y, anchor.z, anchor.heading)


def is_procedural_zone(zone_name: str) -> bool:
    return _load_level(zone_name) is not None


# ── Static (non-maze) worlds — boss arenas, lobbies, quest off-shoots ──

@dataclass(frozen=True)
class _StaticWorld:
    """One row from ``static_worlds`` (a hand-authored, non-maze level)."""
    zone_name: str
    encounter_table: str


@dataclass(frozen=True)
class _StaticMarker:
    """One authored ``base.Encounter`` marker from ``static_world_encounters``.

    World-frame position (Z included — static worlds aren't tiled, the designer
    placed the marker on the floor). ``size_x``/``size_y`` bound the encounter
    area; ``encounter_type`` optionally overrides the world's main table."""
    marker_idx: int
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    size_x: float
    size_y: float
    encounter_type: str


_static_world_cache: Dict[str, Optional[_StaticWorld]] = {}
_static_marker_cache: Dict[str, List[_StaticMarker]] = {}


def _load_static_world(zone_name: str) -> Optional[_StaticWorld]:
    key = (zone_name or "").lower()
    if key in _static_world_cache:
        return _static_world_cache[key]
    from ..db import game_database as db
    row = None
    try:
        row = db.execute_reader(
            "SELECT zone_name, encounter_table FROM static_worlds "
            "WHERE zone_name = :z COLLATE NOCASE", {"z": zone_name}
        ).fetchone()
    except Exception as ex:  # noqa: BLE001 — table missing ⇒ no static worlds
        log.warn(f"[STATIC-SPAWN] static_worlds query failed for "
                 f"'{zone_name}': {ex}")
    world = None
    if row is not None:
        world = _StaticWorld(
            zone_name=db.get_string(row, "zone_name"),
            encounter_table=db.get_string(row, "encounter_table"),
        )
    _static_world_cache[key] = world
    return world


def _load_static_markers(zone_name: str) -> List[_StaticMarker]:
    key = (zone_name or "").lower()
    if key in _static_marker_cache:
        return _static_marker_cache[key]
    from ..db import game_database as db
    markers: List[_StaticMarker] = []
    try:
        rows = db.execute_reader(
            "SELECT marker_idx, pos_x, pos_y, pos_z, heading, size_x, size_y, "
            "encounter_type FROM static_world_encounters "
            "WHERE zone_name = :z COLLATE NOCASE ORDER BY marker_idx",
            {"z": zone_name}
        ).fetchall()
    except Exception as ex:  # noqa: BLE001
        log.warn(f"[STATIC-SPAWN] static_world_encounters query failed for "
                 f"'{zone_name}': {ex}")
        rows = []
    for r in rows:
        markers.append(_StaticMarker(
            marker_idx=db.get_int(r, "marker_idx"),
            pos_x=db.get_float(r, "pos_x"),
            pos_y=db.get_float(r, "pos_y"),
            pos_z=db.get_float(r, "pos_z"),
            heading=db.get_float(r, "heading"),
            size_x=db.get_float(r, "size_x"),
            size_y=db.get_float(r, "size_y"),
            encounter_type=db.get_string(r, "encounter_type"),
        ))
    _static_marker_cache[key] = markers
    return markers


_static_placement_cache: Dict[str, List[DungeonSpawn]] = {}


def _load_static_placements(zone_name: str) -> List[DungeonSpawn]:
    """Load a static world's direct named creature placements (boss + posse)
    from ``static_world_placements`` — the resolved ``BossFightNCI01.*`` entities
    the importer captured. Each is a ``DungeonSpawn`` at its authored world
    position (z included; designer-placed, no pathmap snap). ``[]`` when the
    table or zone is absent (e.g. pre-rebuild DB)."""
    key = (zone_name or "").lower()
    if key in _static_placement_cache:
        return _static_placement_cache[key]
    from ..db import game_database as db
    spawns: List[DungeonSpawn] = []
    try:
        rows = db.execute_reader(
            "SELECT entity_gc_type, creature_gc_type, pos_x, pos_y, pos_z, "
            "heading FROM static_world_placements WHERE zone_name = :z "
            "COLLATE NOCASE ORDER BY placement_idx", {"z": zone_name}
        ).fetchall()
    except Exception as ex:  # noqa: BLE001 — table missing (pre-rebuild) ⇒ none
        log.warn(f"[STATIC-SPAWN] static_world_placements query failed for "
                 f"'{zone_name}': {ex}")
        rows = []
    for r in rows:
        entity = db.get_string(r, "entity_gc_type")
        if not entity:
            continue
        spawns.append(DungeonSpawn(
            gc_type=entity,
            pos_x=db.get_float(r, "pos_x"),
            pos_y=db.get_float(r, "pos_y"),
            pos_z=db.get_float(r, "pos_z"),
            heading=db.get_float(r, "heading"),
            creature_gc_type=db.get_string(r, "creature_gc_type"),
        ))
    _static_placement_cache[key] = spawns
    return spawns


def is_static_world_zone(zone_name: str) -> bool:
    return _load_static_world(zone_name) is not None


def generate_static_spawns(zone_name: str) -> List[DungeonSpawn]:
    """Mob spawns for a hand-authored (non-maze) world — boss arenas etc.

    Fully data-driven from the client ``*.world`` content: each authored
    ``base.Encounter`` marker spawns one encounter group from its own
    ``EncounterType`` table (or the world's main ``EncounterTable``), spread
    within the marker's authored ``SizeX``/``SizeY`` area at the marker's
    authored Z (the designer placed it on the floor — no pathmap needed).
    Deterministic per zone (seeded jitter) so every joiner to an instance sees
    the same layout. Direct named creature placements (the boss + its posse,
    e.g. ``BossFightNCI01.*``) are appended verbatim from
    ``static_world_placements`` — those are designer-placed single units, not
    encounter packs. Returns ``[]`` for unknown zones with no markers/placements.
    """
    world = _load_static_world(zone_name)
    if world is None:
        return []
    placements = _load_static_placements(zone_name)
    markers = _load_static_markers(zone_name)
    if not markers:
        # No encounter markers, but a boss arena still has its posse placements.
        if placements:
            log.info(f"[STATIC-SPAWN] '{zone_name}' generated {len(placements)} "
                     f"mobs from named placements only (no markers)")
        return list(placements)
    main_groups = _load_groups(world.encounter_table)

    jitter = DotNetRandom(to_int32(layout_seed(zone_name)) ^ 0x5F5F5F5F)

    def _jrange(lo: float, hi: float) -> float:
        return lo + jitter.next_double() * (hi - lo)

    spawns: List[DungeonSpawn] = []
    for marker in markers:
        groups = (_load_groups(marker.encounter_type)
                  if marker.encounter_type else main_groups)
        if not groups:
            continue
        grp = groups[marker.marker_idx % len(groups)]
        # Expand to a full pack (3-6 mobs) via the per-spot difficulty budget —
        # same as the maze fill, so a boss-arena marker spawns a pack, not the
        # 1-2 literal units.
        grp = expand_encounter_group(
            grp, _stable_spot_seed(f"{zone_name}:enc:{marker.marker_idx}"))
        # Spread the group inside the authored area; an unsized marker uses the
        # same small offsets the maze fill does.
        half_x = max(marker.size_x / 2.0 - 10.0, 0.0)
        half_y = max(marker.size_y / 2.0 - 10.0, 0.0)
        for slot, (entity, creature, _difficulty) in enumerate(grp):
            if len(spawns) >= _MAX_MOBS_PER_LEVEL:
                break
            off = _GROUP_OFFSETS[slot % len(_GROUP_OFFSETS)]
            ox = off[0] + _jrange(-12.0, 12.0)
            oy = off[1] + _jrange(-12.0, 12.0)
            if half_x > 0:
                ox = max(-half_x, min(half_x, ox))
            if half_y > 0:
                oy = max(-half_y, min(half_y, oy))
            spawns.append(DungeonSpawn(
                gc_type=entity,
                pos_x=marker.pos_x + ox,
                pos_y=marker.pos_y + oy,
                pos_z=marker.pos_z,
                heading=_jrange(0.0, 360.0),
                creature_gc_type=creature,
            ))

    # Append the boss + posse (direct named placements) verbatim.
    spawns.extend(placements)

    log.info(f"[STATIC-SPAWN] '{zone_name}' generated {len(spawns)} mobs from "
             f"{len(markers)} authored markers + {len(placements)} placements "
             f"(enc='{world.encounter_table}')")
    return spawns


def load_static_spawns(zone_name: str) -> List[DungeonSpawn]:
    """Load hand-authored spawns from the ``dungeon_spawns`` table (e.g. the
    fixed boss arenas) — the non-procedural counterpart to the maze generator.
    These rows store a creature ``gc_type`` directly (no separate creature col)."""
    from ..db import game_database as db
    if not zone_name:
        return []
    try:
        rows = db.execute_reader(
            "SELECT gc_type, pos_x, pos_y, pos_z, heading FROM dungeon_spawns "
            "WHERE zone_name = :zone COLLATE NOCASE", {"zone": zone_name}
        ).fetchall()
    except Exception as ex:  # noqa: BLE001
        log.warn(f"[MAZE-SPAWN] static spawn load failed for '{zone_name}': {ex}")
        return []
    return [
        DungeonSpawn(
            gc_type=db.get_string(r, "gc_type"),
            pos_x=db.get_float(r, "pos_x"),
            pos_y=db.get_float(r, "pos_y"),
            pos_z=db.get_float(r, "pos_z"),
            heading=db.get_float(r, "heading"),
            creature_gc_type="",   # gc_type IS the creature for static spawns
        )
        for r in rows
        if db.get_string(r, "gc_type")
    ]


def _find_walkable_spot(pathmap, zone_name: str, x: float, y: float,
                        radius: float = 250.0) -> Tuple[float, float, bool]:
    """Port of FindWalkableSpot. Without a pathmap we accept the marker as-is
    (markers are hand-placed on open floor); with one we spiral out to a spot
    whose 4 cardinal neighbours are also walkable."""
    if pathmap is None:
        return x, y, True

    import math

    def _open(px: float, py: float) -> bool:
        return (pathmap.is_walkable(px, py)
                and pathmap.is_walkable(px + 20.0, py)
                and pathmap.is_walkable(px - 20.0, py)
                and pathmap.is_walkable(px, py + 20.0)
                and pathmap.is_walkable(px, py - 20.0))

    if _open(x, y):
        return x, y, True
    r = 5.0
    while r <= radius:
        angle = 0
        while angle < 360:
            rad = math.radians(angle)
            tx = x + math.cos(rad) * r
            ty = y + math.sin(rad) * r
            if _open(tx, ty):
                return tx, ty, True
            angle += 20
        r += 5.0
    return x, y, False


def generate_spawns(zone_name: str, seed: Optional[int] = None,
                    instance_key: Optional[str] = None) -> List[DungeonSpawn]:
    """Generate maze-placed mob spawns for a procedural dungeon level.

    Builds the maze with the level's own parameters (from ``dungeon_levels``),
    feeding the level's ``*.world`` room nodes (from ``dungeon_room_nodes``) so
    the layout pins the same forced rooms the client does. Then builds a
    per-instance geometry pathmap from the generated cells (registering it under
    ``instance_key``) so mobs snap onto walkable floor at the correct Z, places
    each room node's own encounter (leader/quest/boss) at its cell, and fills the
    remaining rooms with the level's main encounter table. Mob types are the real
    ``world.<dungeon>.mob.*`` assets the client can load. Returns an empty list
    for non-maze zones.

    ``seed`` defaults to :func:`layout_seed` (zone-name derived) so it matches
    the value the server puts in the zone-connect packet; pass an explicit seed
    only for tests or to reproduce a specific layout.
    """
    level = _load_level(zone_name)
    if level is None:
        return []

    if seed is None:
        seed = layout_seed(zone_name)

    groups = _load_groups(level.encounter_table)
    if not groups:
        log.info(f"[MAZE-SPAWN] '{zone_name}' has no resolvable encounters "
                 f"(enc='{level.encounter_table}') — spawning none")
        return []

    layout = _build_layout(zone_name, seed)
    if layout is None:
        return []
    cells = layout.cells
    room_nodes = layout.room_nodes
    maze_placed = layout.placed_room_nodes

    # Geometry pathmap built from the exact cells the client renders, so mob
    # placement snaps onto floor. Cached per (zone, seed) and shared with the
    # warp-gate + player-entry floor-snap; register it under the instance key for
    # the instance lifetime (world_instance frees it on teardown).
    pathmap = _build_pathmap(zone_name, seed)
    if pathmap is not None and instance_key:
        pathmap_manager.register_instance(instance_key, pathmap)

    # Deterministic jitter RNG (does not need to match the client; see module doc).
    jitter = DotNetRandom(to_int32(seed) ^ 0x5F5F5F5F)

    def _jrange(lo: float, hi: float) -> float:
        return lo + jitter.next_double() * (hi - lo)

    def _height(x: float, y: float, fallback: float) -> float:
        # Real floor height from the geometry pathmap (placement Z + heightmap;
        # see pathmap_builder). The authored marker Z is the fallback. No lift:
        # the client clips the mob to its own floor the moment it moves, so any
        # offset just renders it half-sunk/floating until then.
        if pathmap is None:
            return fallback
        return pathmap.get_height_at(x, y, fallback)

    spawns: List[DungeonSpawn] = []

    def _emit_group(group: _EncGroup, base_x: float, base_y: float,
                    base_z: float, budget: int = 0, spot_key: str = "") -> None:
        # Snap the GROUP base onto walkable floor once, then cluster the
        # members tightly around it, clamped to the marker's authored
        # encounter-area size — encounters spawn as packs (the original game's
        # behaviour). Members only micro-adjust (small search radius) instead
        # of each spiralling up to 250 units, which used to scatter one group
        # across the room/corridor and through wall gaps.
        #
        # Expand the literal group into a full pack first (3-6 mobs) via the
        # per-spot difficulty budget — the previous code placed only the 1-2
        # authored units, which is why dungeons looked under-populated vs. the
        # original game (user report 2026-06-17).
        group = expand_encounter_group(group, _stable_spot_seed(spot_key))
        bx, by, found = _find_walkable_spot(pathmap, zone_name, base_x, base_y)
        if not found:
            return
        half = max((budget or _DEFAULT_ENCOUNTER_SIZE) / 2.0 - 10.0,
                   _MIN_GROUP_HALF)
        for slot, (entity, creature, _difficulty) in enumerate(group):
            if len(spawns) >= _MAX_MOBS_PER_LEVEL:
                return
            off = _GROUP_OFFSETS[slot % len(_GROUP_OFFSETS)]
            ox = max(-half, min(half, off[0] + _jrange(-12.0, 12.0)))
            oy = max(-half, min(half, off[1] + _jrange(-12.0, 12.0)))
            sx, sy, found = _find_walkable_spot(pathmap, zone_name,
                                                bx + ox, by + oy,
                                                radius=_MEMBER_SNAP_RADIUS)
            if not found:
                continue
            spawns.append(DungeonSpawn(
                gc_type=entity, pos_x=sx, pos_y=sy,
                pos_z=_height(sx, sy, base_z), heading=_jrange(0.0, 360.0),
                creature_gc_type=creature,
            ))

    cell_by_grid = layout.cell_by_grid

    # Skip the player's arrival tile so mobs don't spawn on top of them — the
    # real placed entrance (mainentrance/startroom), matching where the player
    # now spawns (see entry_position / game_server._transfer_zone). Falls back to
    # the bottom-right-most cell when no entrance node placed. Cosmetic — mobs
    # spawn passive anyway (see monsters.py).
    entry_cell = _entry_cell(layout) or (
        max(cells, key=lambda c: (c.grid_x, -c.grid_y)) if cells else None)

    # Step 1: each placed room node spawns its OWN encounter (leaders, quests,
    # bosses) at its cell. Room nodes whose encounter_type table wasn't imported
    # resolve to no groups and are skipped (the cell is still reserved so the
    # generic fill doesn't double-stack it).
    src_encounter = {rn.source_index: rn.encounter_type for rn in room_nodes}
    room_node_cells: set = set()
    for placed in maze_placed:
        cell = cell_by_grid.get((placed.grid_x, placed.grid_y))
        if cell is None:
            continue
        room_node_cells.add((placed.grid_x, placed.grid_y))
        if cell is entry_cell:
            continue
        enc = src_encounter.get(placed.source_index, "")
        if not enc:
            continue
        node_groups = _load_groups(enc)
        if not node_groups:
            continue
        grp = node_groups[abs(placed.source_index) % len(node_groups)]
        # Spawn the forced room's encounter at the room tile's OWN content marker
        # (``base.Encounter`` in the .tile). A forced room (leader/quest/boss)
        # must populate, so when its tile carries no marker fall back to the tile
        # centre rather than skipping it.
        markers = (_tile_encounter_markers(cell.tile_type)
                   or [_tile_center(cell.tile_size)])
        mx, my, mz, mbudget = markers[0]
        _emit_group(grp, cell.world_origin_x + mx, cell.world_origin_y + my,
                    mz, mbudget,
                    spot_key=f"{zone_name}:leader:{placed.source_index}")

    # Step 2: fill the remaining (non-room-node) cells with the main encounter
    # table. Client iteration order: top-to-bottom (gridY desc), left-to-right.
    cells_sorted = sorted(cells, key=lambda c: (-c.grid_y, c.grid_x))

    # Marker source per cell: each tile's REAL ``base.Encounter`` markers parsed
    # from client content (no hardcoded table — the dungeon00 hand-tuned values
    # were verified to be exact transcriptions of these content markers). A cell
    # whose tile carries no marker spawns nothing — most corridors/dead-ends,
    # matching the client (a centre-per-cell fallback over-populates corridors).
    enc_idx = 0
    for cell in cells_sorted:
        if cell is entry_cell:
            continue
        if (cell.grid_x, cell.grid_y) in room_node_cells:
            continue

        markers = _tile_encounter_markers(cell.tile_type)
        for marker in markers:
            if len(spawns) >= _MAX_MOBS_PER_LEVEL:
                break
            group = groups[enc_idx % len(groups)]
            _emit_group(group, cell.world_origin_x + marker[0],
                        cell.world_origin_y + marker[1], marker[2], marker[3],
                        spot_key=f"{zone_name}:enc:{enc_idx}")
            enc_idx += 1

    log.info(f"[MAZE-SPAWN] '{zone_name}' generated {len(spawns)} mobs "
             f"(seed=0x{seed:08X}, {level.maze_width}x{level.maze_height}, "
             f"rooms={len(maze_placed)}/{len(room_nodes)}, "
             f"prefix='{level.tile_prefix}')")
    return spawns


# ── Placed room-node helpers (entry position + warp gates) ──

# Kinds whose room node marks the player's arrival point (carries a SpawnName a
# portal targets). The C# names these the same way across every dungeon.
_ENTRANCE_KINDS = ("startroom", "mainentrance")


def _entry_cell(layout: _DungeonLayout, spawn_point: str = ""):
    """The MazeCell the player should arrive in.

    Prefers the placed room node whose ``spawn_name`` matches the requested
    ``spawn_point`` (how a portal addresses a specific arrival tile); otherwise
    the placed start room / main entrance. Returns ``None`` when none placed."""
    placed = {p.source_index: p for p in layout.placed_room_nodes}

    def _cell_for(rn: _RoomNode):
        p = placed.get(rn.source_index)
        if p is None:
            return None
        return layout.cell_by_grid.get((p.grid_x, p.grid_y))

    if spawn_point:
        want = spawn_point.lower()
        for rn in layout.room_nodes:
            if rn.spawn_name and rn.spawn_name.lower() == want:
                cell = _cell_for(rn)
                if cell is not None:
                    return cell

    for kind in _ENTRANCE_KINDS:
        for rn in layout.room_nodes:
            if rn.node_kind == kind:
                cell = _cell_for(rn)
                if cell is not None:
                    return cell

    # Some mazes (e.g. elite01_intro) declare NO start room — every way in is a
    # linkroomnode with a spawn_name. Arriving without a matching spawn_point
    # (respawn, recall) still needs a sane entry: the first placed node carrying
    # a spawn_name.
    for rn in layout.room_nodes:
        if rn.spawn_name:
            cell = _cell_for(rn)
            if cell is not None:
                return cell

    # A handful of solo dungeons (dungeon_snowman, the nci/epic off-shoots) author
    # EVERY room as a plain ``roomnode`` — no StartRoom node kind, no SpawnName — but
    # name the entrance room's tileset ``<...>_start_`` (e.g. ``icecave_snowman_start_``).
    # Without this the entry can't be resolved and the caller falls back to the
    # zone's (0,0,0) default spawn → the player lands OUTSIDE the map (live
    # 2026-07-01: "teleport to sanctuary works but I'm spawning outside map bounds").
    for rn in layout.room_nodes:
        if "start" in (rn.tile_set or "").lower():
            cell = _cell_for(rn)
            if cell is not None:
                return cell

    # Last resort: ANY placed room cell. A wrong-but-in-bounds arrival (inside the
    # maze) is always better than world origin — the player can walk from there.
    for rn in layout.room_nodes:
        cell = _cell_for(rn)
        if cell is not None:
            return cell
    return None


def entry_position(zone_name: str, spawn_point: str = "",
                   seed: Optional[int] = None
                   ) -> Optional[Tuple[float, float, Optional[float], float]]:
    """World ``(x, y, z, heading)`` of a procedural level's maze entry, or ``None``.

    The maze is anchored at world origin and regenerated per layout seed, so the
    static ``.zone`` default spawn never lands inside it — the player must arrive
    at the entrance room the maze actually placed. Resolves the placed room node
    whose ``spawn_name`` matches ``spawn_point`` (the portal target), falling back
    to the placed start room / main entrance.

    Fully DATA-DRIVEN: the player lands at the entrance tile's authored
    ``SpawnPoint`` waypoint — ``world_origin + authored_local`` for X/Y/Z and the
    authored facing — exactly where the client's designer placed it (no guessed
    offset, no hardcoded floor lift; the maze is flat at world Z=0 so the
    waypoint's local Z is its world Z). Only when a tile resolves no anchor at all
    (never, for the 66 entrance tiles) does it fall back to the floor-snapped
    centre with ``z=None`` (caller keeps its resolved Z).
    """
    if seed is None:
        seed = layout_seed(zone_name)
    layout = _build_layout(zone_name, seed)
    if layout is None:
        return None
    cell = _entry_cell(layout, spawn_point)
    if cell is None:
        return None

    spawn = _authored_spawn(cell.tile_type)
    if spawn is not None:
        lx, ly, lz, heading = spawn
        return (cell.world_origin_x + lx, cell.world_origin_y + ly, lz, heading)

    # Fallback only for an anchorless tile: floor-snap the geometric centre.
    x, y = cell.world_center_x, cell.world_center_y
    pathmap = _build_pathmap(zone_name, seed)
    if pathmap is None:
        return (x, y, None, 0.0)
    sx, sy, _found = _find_walkable_spot(pathmap, zone_name, x, y)
    return (sx, sy, pathmap.get_height_at(sx, sy, 0.0), 0.0)


def sample_mob_units(zone_name: str, count: int = 5) -> List[Tuple[str, str]]:
    """A flat random sample of ``(entity_world_gc, creature_gc)`` from a level's
    main encounter table.

    The data-driven replacement for the old hardcoded dungeon00 creature→asset
    family map: the generic fallback / admin live-spawn now draws loadable mobs
    straight from the level's own ``.enc`` content (the entity types are real
    ``world.<dungeon>.mob.*`` assets the client can render). Returns ``[]`` for
    non-maze zones or empty tables.
    """
    if count <= 0:
        return []
    level = _load_level(zone_name)
    if level is None:
        return []
    units = [u for group in _load_groups(level.encounter_table) for u in group]
    if not units:
        return []
    import random
    # Strip the difficulty weight — this flat sample feeds the legacy admin /
    # fallback spawn path, which only needs (entity_world_gc, creature_gc).
    return [(u[0], u[1]) for u in (random.choice(units) for _ in range(count))]


def warp_gates(zone_name: str, seed: Optional[int] = None) -> List[WarpGate]:
    """Inter-level warp gates for a procedural level, placed at the maze cells.

    Every placed room node carrying a ``link_to_zone`` (``mainentrance`` →
    previous level, ``exit`` → next level, ``shared``/``oneoff`` → side areas)
    becomes a gate at its cell's world centre. Data-driven from
    ``dungeon_room_nodes`` (the client's ``.world`` content), so EVERY dungeon
    gets its entrance + exit warps, not just the hand-authored ones. Returns
    ``[]`` for non-maze zones.
    """
    if seed is None:
        seed = layout_seed(zone_name)
    layout = _build_layout(zone_name, seed)
    if layout is None:
        return []
    placed = {p.source_index: p for p in layout.placed_room_nodes}
    gates: List[WarpGate] = []
    for rn in layout.room_nodes:
        if not rn.link_to_zone:
            continue
        p = placed.get(rn.source_index)
        if p is None:
            continue
        cell = layout.cell_by_grid.get((p.grid_x, p.grid_y))
        if cell is None:
            continue
        # Place the gate at the tile's authored ZonePortal model anchor — exactly
        # where the client renders the portal (and the minimap shows it) — in full:
        # X/Y/Z + the model's authored Heading (rotation). ``world_origin + local``
        # for X/Y, and the local Z is the world Z (maze flat at Z=0), so the gate
        # needs no floor-snap or hardcoded lift. Falls back to centre/0 only for a
        # tile with no portal model (never, for the entrance/exit tiles).
        _player, portal_anchor = _tile_authored_anchors(p.tile_type)
        if portal_anchor is not None:
            world_x = cell.world_origin_x + portal_anchor.x
            world_y = cell.world_origin_y + portal_anchor.y
            world_z = portal_anchor.z
            heading = portal_anchor.heading
        else:
            world_x, world_y = cell.world_center_x, cell.world_center_y
            world_z = 0.0
            heading = 0.0
        gates.append(WarpGate(
            source_index=rn.source_index,
            node_kind=rn.node_kind,
            tile_set=rn.tile_set,
            tile_type=p.tile_type,
            grid_x=p.grid_x,
            grid_y=p.grid_y,
            world_x=world_x,
            world_y=world_y,
            world_z=world_z,
            heading=heading,
            link_to_zone=rn.link_to_zone,
            link_to_spawn=rn.link_to_spawn,
            spawn_name=rn.spawn_name,
        ))
    return gates
