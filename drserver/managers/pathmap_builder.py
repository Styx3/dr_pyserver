"""Builds a per-instance :class:`~drserver.managers.pathmap.PathMap` from maze
geometry.

Port of C# ``DungeonRunners.Utilities.PathMapBuilder``. Given the maze
generator's cells, it composes each placed tile's collision geometry into a
walkability grid so the dungeon spawner can snap mobs onto floor at correct Z
(instead of into walls or mid-air).

Pipeline per cell:

1. Resolve the cell's ``tile_type`` to a ``.tile`` file
   (:func:`tile_cobj_resolver.resolve_tile_path`).
2. Parse the ``.tile`` into placements (sub-asset path + Position + Heading)
   (:func:`tile_layout_loader.load`).
3. For each placement that resolves to a ``.cobj``, parse it, transform each
   blocked cell into world space (placement Position + Heading + cell origin),
   and mark the matching PathMap nodes blocked.

Remaining nodes inside the maze footprint are filled walkable.

Two collision sub-shapes are applied:
  * **Sub-shape 1** (heightmap): a cell taller than ``WALL_HEIGHT_THRESHOLD``
    blocks (walls, solid floor steps). Cells at/below the threshold are FLOOR
    COVERAGE: the node records the real floor height ``placement.z + h`` (the
    floor pieces themselves are all-zero heightmaps placed at an authored Z —
    e.g. cave_small rooms sit at z≈10 — so ignoring the placement Z put every
    mob at z=0, half-sunk into raised floors).
  * **Sub-shape 2** (vertical bbox stacks): a cell blocks iff a bbox's world-Z
    extent (``placement.z + originZ2 + bbox``) overlaps the walking band
    ``[WALKING_Z_MIN, WALKING_Z_MAX]`` — catches pillars / doorframes /
    railings; high overhead structures (bridges, archways) correctly do NOT
    block the ground beneath them.

Walkability requires floor coverage: a node inside the maze footprint that NO
placement's geometry covers is a hole/void (gaps in the tile, the area outside
an irregular tile's real shape) and is NOT walkable — previously such nodes
were filled walkable and mobs spawned in pits and outside the map. Placements
whose leaf name contains ``nowalk`` (e.g. ``ElmForest_FloorNoWalk_40``) are
designer-marked unwalkable floor and stamp blocked instead of coverage. A cell
that yields no coverage at all (unresolvable tile/cobjs — missing content)
falls back to the old open-footprint behaviour so partial content degrades
gracefully instead of emptying the level.

NB: the C# PathMapBuilder writes ``Height = 0`` for every node and fills all
uncovered footprint walkable — both proven emulator bugs (reference only);
the heightmap + authored placement Z in the client content are ground truth.

Heading uses Python ``math.cos``/``sin``; the C# uses a Fixed32 SIN/COS LUT.
The difference is sub-degree and well under one node (10 units) at tile scale —
flagged as a Phase-D parity check.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..core import log
from ..data.cobj_parser import CobjBBox, CobjData
from . import tile_cobj_resolver, tile_layout_loader
from .maze import MazeCell
from .pathmap import PathMap, PathNode
from .tile_layout_loader import TilePlacement

NODE_RESOLUTION = 10.0  # world units per PathMap node (matches PathMap.TILE_SIZE)
# Default maze cell side length. The real per-cell footprint is the cell's own
# ``tile_size`` (per-tileset: elmforest 400, cave_small 360, ruins 280, …); kept
# only as a documented default for any cell that predates the per-cell field.
MAZE_TILE_SIZE = 400.0
WALL_HEIGHT_THRESHOLD = 30  # heightmap entries above this block
WALKING_Z_MIN = 0  # bottom of the walking-Z band for sub-shape-2 overlap
WALKING_Z_MAX = WALL_HEIGHT_THRESHOLD  # top of the band (tracks the wall threshold)

_Coord = Tuple[int, int]


def build(zone_name: str, cells: Sequence[MazeCell],
          wall_height_threshold: Optional[int] = None) -> Optional[PathMap]:
    """Build a per-instance PathMap from maze cells, or ``None`` if no cells."""
    if not cells:
        log.warn(f"[PATHMAP-BUILD] zone='{zone_name}' SKIP — empty cell list")
        return None

    threshold = WALL_HEIGHT_THRESHOLD if wall_height_threshold is None else wall_height_threshold

    min_x, max_x, min_y, max_y = _compute_world_bounds(cells)
    pathmap = PathMap.create_empty(zone_name, min_x, max_x, min_y, max_y)

    grid_w = math.ceil((max_x - min_x) / NODE_RESOLUTION) + 1
    grid_h = math.ceil((max_y - min_y) / NODE_RESOLUTION) + 1

    blocked: Set[_Coord] = set()
    in_footprint: Set[_Coord] = set()
    floor: Dict[_Coord, float] = {}      # node → real floor height (placement.z + h)
    fallback_open: Set[_Coord] = set()   # footprint of cells with no usable geometry

    tiles_processed = 0
    tiles_skipped_no_file = 0
    placements_processed = 0
    placements_skipped_no_cobj = 0
    blocked_cells_total = 0
    blocked_cells_subshape2 = 0
    missing_tile_types: List[str] = []
    no_cobj_leaf_counts: Dict[str, int] = {}

    for cell in cells:
        cell_coords = _mark_maze_footprint(cell, in_footprint, grid_w, grid_h,
                                           min_x, min_y)

        tile_path = tile_cobj_resolver.resolve_tile_path(cell.tile_type)
        if tile_path is None:
            tiles_skipped_no_file += 1
            missing_tile_types.append(cell.tile_type)
            fallback_open.update(cell_coords)
            continue

        try:
            layout = tile_layout_loader.load(tile_path)
        except Exception as e:  # noqa: BLE001
            log.warn(f"[PATHMAP-BUILD] tile parse error: {cell.tile_type} — {e}")
            fallback_open.update(cell_coords)
            continue

        tiles_processed += 1
        floor_before = len(floor)
        for placement in layout.placements:
            cobj_path = tile_cobj_resolver.resolve_cobj_path(placement.extends_path)
            if cobj_path is None:
                placements_skipped_no_cobj += 1
                leaf = placement.leaf_name
                if leaf:
                    no_cobj_leaf_counts[leaf] = no_cobj_leaf_counts.get(leaf, 0) + 1
                continue

            try:
                from ..data import cobj_parser
                cobj = cobj_parser.parse_file(cobj_path)
            except Exception:  # noqa: BLE001
                placements_skipped_no_cobj += 1
                continue

            placements_processed += 1
            blocked_cells_total += _apply_subshape1(
                cobj, placement, cell, blocked, floor, grid_w, grid_h,
                min_x, min_y, threshold)
            blocked_cells_subshape2 += _apply_subshape2(
                cobj, placement, cell, blocked, grid_w, grid_h, min_x, min_y, threshold)

        # A cell whose placements yielded zero floor coverage (content didn't
        # resolve) keeps the legacy open-footprint behaviour — degrade to
        # "walkable at z=0" rather than blocking the whole cell.
        if len(floor) == floor_before:
            fallback_open.update(cell_coords)

    walkable = 0
    blocked_count = 0
    holes = 0
    for coord in in_footprint:
        gx, gy = coord
        covered = coord in floor or coord in fallback_open
        is_blocked = coord in blocked or not covered
        if not covered and coord not in blocked:
            holes += 1
        pathmap.set_node(PathNode(
            grid_x=gx,
            grid_y=gy,
            world_x=min_x + gx * NODE_RESOLUTION,
            world_y=min_y + gy * NODE_RESOLUTION,
            height=floor.get(coord, 0.0),
            # Walkable nodes allow all 8 directions; the spawner relies on
            # per-neighbor walkability, not baked connection flags.
            connection_flags=0x00 if is_blocked else 0xFF,
            solid_flag=0xFE if is_blocked else 0x00,
        ))
        if is_blocked:
            blocked_count += 1
        else:
            walkable += 1

    log.info(
        f"[PATHMAP-BUILD] zone='{zone_name}' cells={len(cells)} "
        f"tiles={tiles_processed}/{len(cells)} ({tiles_skipped_no_file} no-file) "
        f"placements={placements_processed} ({placements_skipped_no_cobj} no-cobj) "
        f"nodes={pathmap.node_count} (walkable={walkable} blocked={blocked_count} "
        f"holes={holes}) "
        f"bounds=({min_x:.0f},{min_y:.0f})->({max_x:.0f},{max_y:.0f}) "
        f"blockedCobjCells={blocked_cells_total} blockedSubShape2={blocked_cells_subshape2} "
        f"threshold={threshold}")

    if missing_tile_types:
        uniq = sorted(set(missing_tile_types))
        log.warn(f"[PATHMAP-BUILD] missing tile files for zone='{zone_name}': "
                 f"{', '.join(uniq)}")

    return pathmap


def _compute_world_bounds(cells: Sequence[MazeCell]
                          ) -> Tuple[float, float, float, float]:
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for c in cells:
        if c.world_origin_x < min_x:
            min_x = c.world_origin_x
        if c.world_origin_y < min_y:
            min_y = c.world_origin_y
        x_right = c.world_origin_x + c.tile_size
        y_top = c.world_origin_y + c.tile_size
        if x_right > max_x:
            max_x = x_right
        if y_top > max_y:
            max_y = y_top
    return min_x, max_x, min_y, max_y


def _mark_maze_footprint(cell: MazeCell, in_footprint: Set[_Coord],
                         grid_w: int, grid_h: int,
                         min_x: float, min_y: float) -> List[_Coord]:
    """Mark the cell's square footprint and return its coords (the caller needs
    them again for the per-cell no-geometry fallback)."""
    coords: List[_Coord] = []
    gx0 = math.floor((cell.world_origin_x - min_x) / NODE_RESOLUTION)
    gy0 = math.floor((cell.world_origin_y - min_y) / NODE_RESOLUTION)
    gx1 = math.ceil((cell.world_origin_x + cell.tile_size - min_x) / NODE_RESOLUTION)
    gy1 = math.ceil((cell.world_origin_y + cell.tile_size - min_y) / NODE_RESOLUTION)
    for gx in range(max(0, gx0), min(grid_w, gx1)):
        for gy in range(max(0, gy0), min(grid_h, gy1)):
            in_footprint.add((gx, gy))
            coords.append((gx, gy))
    return coords


def _apply_subshape1(cobj: CobjData, placement: TilePlacement, cell: MazeCell,
                     blocked: Set[_Coord], floor: Dict[_Coord, float],
                     grid_w: int, grid_h: int,
                     min_x: float, min_y: float, wall_height_threshold: int) -> int:
    if cobj.width1 <= 0 or cobj.height1 <= 0:
        return 0

    cs = cobj.cell_size1
    rad = math.radians(placement.heading)
    cos_t = math.cos(rad)
    sin_t = math.sin(rad)
    # Designer-marked unwalkable floor (e.g. ElmForest_FloorNoWalk_40): geometry
    # exists but the player/mobs must not stand on it — stamp blocked, not floor.
    is_nowalk = "nowalk" in (placement.leaf_name or "").lower()
    # Half the stamp extent: a heightmap cell covers a cs-sided square; stamping
    # only its centre node leaves coverage gaps when cs exceeds the node grid.
    reach = max(1, math.ceil(cs / (2.0 * NODE_RESOLUTION))) if cs > NODE_RESOLUTION else 0

    blocked_here = 0
    for cy in range(cobj.height1):
        for cx in range(cobj.width1):
            h = cobj.heightmap[cy * cobj.width1 + cx]

            lx = cobj.origin_x1 + (cx + 0.5) * cs
            ly = cobj.origin_y1 + (cy + 0.5) * cs
            rx = lx * cos_t - ly * sin_t
            ry = lx * sin_t + ly * cos_t
            wx = cell.world_origin_x + placement.x + rx
            wy = cell.world_origin_y + placement.y + ry

            gx_node = round((wx - min_x) / NODE_RESOLUTION)
            gy_node = round((wy - min_y) / NODE_RESOLUTION)
            if gx_node < 0 or gx_node >= grid_w or gy_node < 0 or gy_node >= grid_h:
                continue
            coord = (gx_node, gy_node)

            if h > wall_height_threshold or is_nowalk:
                if coord not in blocked:
                    blocked.add(coord)
                    blocked_here += 1
                continue

            # Floor coverage: world floor height = authored placement Z + cell
            # height. Floor pieces are all-zero heightmaps placed at the room's
            # authored Z, so the placement Z carries the real elevation.
            world_h = placement.z + h
            for ngx in range(gx_node - reach, gx_node + reach + 1):
                for ngy in range(gy_node - reach, gy_node + reach + 1):
                    if ngx < 0 or ngx >= grid_w or ngy < 0 or ngy >= grid_h:
                        continue
                    ncoord = (ngx, ngy)
                    if world_h > floor.get(ncoord, -math.inf):
                        floor[ncoord] = world_h
    return blocked_here


def _apply_subshape2(cobj: CobjData, placement: TilePlacement, cell: MazeCell,
                     blocked: Set[_Coord], grid_w: int, grid_h: int,
                     min_x: float, min_y: float, walking_z_max: int) -> int:
    if cobj.width2 <= 0 or cobj.height2 <= 0 or not cobj.cells:
        return 0

    cs2 = cobj.cell_size2
    rad = math.radians(placement.heading)
    cos_t = math.cos(rad)
    sin_t = math.sin(rad)

    blocked_here = 0
    for cy in range(cobj.height2):
        for cx in range(cobj.width2):
            stack = cobj.cells[cy * cobj.width2 + cx]
            if not stack.bboxes:
                continue
            if not _stack_blocks_walking_band(stack.bboxes,
                                              cobj.origin_z2 + placement.z,
                                              walking_z_max):
                continue

            lx = cobj.origin_x2 + (cx + 0.5) * cs2
            ly = cobj.origin_y2 + (cy + 0.5) * cs2
            rx = lx * cos_t - ly * sin_t
            ry = lx * sin_t + ly * cos_t
            wx = cell.world_origin_x + placement.x + rx
            wy = cell.world_origin_y + placement.y + ry

            gx_node = round((wx - min_x) / NODE_RESOLUTION)
            gy_node = round((wy - min_y) / NODE_RESOLUTION)
            if gx_node < 0 or gx_node >= grid_w or gy_node < 0 or gy_node >= grid_h:
                continue
            coord = (gx_node, gy_node)
            if coord not in blocked:
                blocked.add(coord)
                blocked_here += 1
    return blocked_here


def _stack_blocks_walking_band(bboxes: Sequence[CobjBBox], base_z: float,
                               walking_z_max: int) -> bool:
    """``base_z`` is the bbox frame's world offset: cobj ``originZ2`` plus the
    placement's authored Z (a raised room's doorframe sits at the room's Z)."""
    for b in bboxes:
        world_z_low = b.z_low + base_z
        world_z_high = b.z_high + base_z
        if world_z_low < walking_z_max and world_z_high > WALKING_Z_MIN:
            return True
    return False
