"""Client-faithful dungeon maze generator — port of C# ``MazeGenerator`` (latest DR-Server).

Replaces the old growing-tree / ``DotNetRandom`` port. The client builds dungeon
terrain with a **MersenneTwister**-driven room-node + corridor carver anchored at
world origin ``(0,0)`` with a half-grid offset (``CenterOverride`` is *ignored*),
and an **E/W mirror** baked into the tile-name suffix (``1e``↔WEST, ``1w``↔EAST,
per native ``TileLibrary::char2Direction``). The previous algorithm produced the
wrong maze shape in the wrong world location, so every cell-derived mob position
was off. See [[project_dungeon_map_alignment]].

Pipeline order is load-bearing — the RNG draw sequence must match the client per
step or layouts diverge::

    PlaceRoomNodes → GenerateCorridors → ApplyForcedRoomExits → Sparsify
    → RemoveDeadEnds → SyncOpeningsFromNativeBits → BuildResult

RNG is :class:`drserver.combat.rng.MersenneTwister` (MT19937) seeded with the
``uint`` zone-connect seed (``0xBEEFBEEF``) — **not** ``DotNetRandom``.

Per CLAUDE.md the C# is reference-only, but this operates on authoritative client
content and was reverse-engineered against client maze captures; the port is
**live-verified** before it is called done.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..combat.rng import MersenneTwister

TILE_SIZE = 400


def _resolve_tile_size(tile_set_prefix: str) -> int:
    """Per-tileset maze cell stride from client content, default 400.

    Reads the tileset's ``TileSize`` (``base/World_<theme>.gc``) via the tile
    resolver. Lazy import keeps :mod:`maze` free of a hard dependency on content
    resolution (and avoids an import cycle). Any failure → 400 (elmforest)."""
    try:
        from . import tile_cobj_resolver
        return tile_cobj_resolver.tile_size_for(tile_set_prefix)
    except Exception:  # noqa: BLE001 — content unavailable ⇒ safe default
        return TILE_SIZE

# Public opening-direction indices (stored in the ``_openings`` sets).
NORTH = 0
EAST = 1
SOUTH = 2
WEST = 3

# Native cell-bitfield direction bits (match C# DIR_*).
DIR_NORTH = 0x01
DIR_SOUTH = 0x02
DIR_EAST = 0x04
DIR_WEST = 0x08
CELL_ROOM = 0x10

_ORDERED_DIRS = (DIR_NORTH, DIR_EAST, DIR_SOUTH, DIR_WEST)
_EMPTY_NEIGHBOR_DIRS = (DIR_NORTH, DIR_SOUTH, DIR_WEST, DIR_EAST)

# (x, y) grid coordinate.
_Cell = Tuple[int, int]


@dataclass
class RoomNodeSpec:
    """A requested room placement (C# ``RoomNodeSpec``)."""
    tile_set: str
    grid_x: Optional[int] = None
    grid_y: Optional[int] = None
    chance: int = 100
    source_index: int = -1


@dataclass
class PlacedRoomNode:
    """A room actually placed into the grid (C# ``PlacedRoomNode``)."""
    source_index: int
    tile_set: str
    tile_type: str
    grid_x: int
    grid_y: int


@dataclass
class MazeCell:
    grid_x: int
    grid_y: int
    world_grid_y: int
    connections: str
    tile_type: str
    world_origin_x: float
    world_origin_y: float
    world_center_x: float
    world_center_y: float
    # World-unit side length of this cell (the tileset's ``TileSize``: elmforest
    # 400, cave_small 360, ruins 280, …). REQUIRED (no default): every cell must
    # state its stride so the pathmap builder and spawner agree on the cell's
    # world footprint — a silent 400 default is what caused the cross-theme drift
    # this field fixes.
    tile_size: float

    @property
    def has_north(self) -> bool:
        return "1n" in self.connections

    @property
    def has_east(self) -> bool:
        return "1e" in self.connections

    @property
    def has_south(self) -> bool:
        return "1s" in self.connections

    @property
    def has_west(self) -> bool:
        return "1w" in self.connections


class MazeGenerator:
    """Deterministic room-node + corridor carver, byte-faithful to the C# reference."""

    def __init__(self, width: int, height: int, seed: int,
                 randomness: int = 90, sparseness: int = 5,
                 dead_end_removal_chance: int = 100,
                 rng: Optional[MersenneTwister] = None) -> None:
        self.width = width
        self.height = height
        self.seed = seed & 0xFFFFFFFF
        self.randomness = randomness
        self.sparseness = sparseness
        self.dead_end_removal_chance = dead_end_removal_chance
        # C#: new MersenneTwister(seed)
        self._rng = rng if rng is not None else MersenneTwister(self.seed)

        self._cells: List[List[int]] = [[0] * width for _ in range(height)]
        self._occupied: List[List[bool]] = [[False] * width for _ in range(height)]
        self._forced_exits: List[List[int]] = [[0] * width for _ in range(height)]
        self._room_tile_types: List[List[Optional[str]]] = [
            [None] * width for _ in range(height)
        ]
        self._openings: List[List[set]] = [
            [set() for _ in range(width)] for _ in range(height)
        ]
        self._room_nodes: List[RoomNodeSpec] = []
        self._placed_room_nodes: List[PlacedRoomNode] = []

        # World-unit cell stride, resolved per-tileset from the prefix passed to
        # :meth:`generate` (elmforest 400, cave_small 360, ruins 280, …). Defaults
        # to the elmforest size until generate() resolves it.
        self.tile_size: int = TILE_SIZE

        # CenterOverride is IGNORED by BuildResult (the maze anchors at world 0,0).
        # Kept as settable attributes for backward-compat with callers that still
        # assign them; they have no effect on the generated geometry.
        self.center_override_x: Optional[float] = None
        self.center_override_y: Optional[float] = None

    @property
    def placed_room_nodes(self) -> List[PlacedRoomNode]:
        return self._placed_room_nodes

    # ── public API ──────────────────────────────────────────────────────────

    def add_room_node(self, tile_set: str, grid_x: Optional[int] = None,
                      grid_y: Optional[int] = None, chance: int = 100,
                      source_index: int = -1) -> None:
        """Queue a room node for placement during :meth:`generate` (C# ``AddRoomNode``)."""
        if not tile_set:
            return
        self._room_nodes.append(
            RoomNodeSpec(tile_set, grid_x, grid_y, chance, source_index)
        )

    def generate(self, tile_set_prefix: str = "elmforest_tileset_") -> List[MazeCell]:
        # Resolve the per-tileset world cell stride from client content (the
        # ``TileSize`` of ``base/World_<theme>.gc``). A wrong/fixed stride drifts
        # every cell origin for non-elmforest themes — see :meth:`_build_result`.
        self.tile_size = _resolve_tile_size(tile_set_prefix)
        self._place_room_nodes()
        self._generate_corridors()
        self._apply_forced_room_exits()
        self._sparsify()
        self._remove_dead_ends()
        self._sync_openings_from_native_bits()
        return self._build_result(tile_set_prefix)

    def get_connections(self, gx: int, gy: int) -> Optional[str]:
        if not self._in_bounds(gx, gy):
            return None
        opens = self._openings[gy][gx]
        conns = ""
        if NORTH in opens:
            conns += "1n"
        # E/W mirror (see module docstring).
        if WEST in opens:
            conns += "1e"
        if SOUTH in opens:
            conns += "1s"
        if EAST in opens:
            conns += "1w"
        return conns

    # ── RNG helpers (C# RandomDirection / Roll100 / NextInt) ─────────────────

    def _random_direction(self) -> int:
        r = self._rng.generate() & 3
        if r == 0:
            return DIR_NORTH
        if r == 1:
            return DIR_SOUTH
        if r == 2:
            return DIR_WEST
        return DIR_EAST

    def _roll100(self) -> int:
        return (self._rng.generate() % 100) + 1

    def next_int(self, min_inclusive: int, max_exclusive: int) -> int:
        """C# ``NextInt`` — ``Generate(min, max-1)`` (inclusive both ends)."""
        if max_exclusive <= min_inclusive:
            return min_inclusive
        return self._rng.generate_range(min_inclusive, max_exclusive - 1)

    # ── grid primitives ──────────────────────────────────────────────────────

    def _in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def _cell_byte(self, x: int, y: int) -> int:
        bits = self._cells[y][x]
        if self._occupied[y][x]:
            bits = bits | self._forced_exits[y][x] | CELL_ROOM
        return bits & 0xFF

    def _set_dir(self, x: int, y: int, direction: int) -> None:
        if self._in_bounds(x, y):
            self._cells[y][x] |= direction

    def _unset_dir(self, x: int, y: int, direction: int) -> None:
        if self._in_bounds(x, y):
            self._cells[y][x] &= ~direction & 0xFF

    def _neighbor(self, x: int, y: int, direction: int) -> _Cell:
        if direction == DIR_NORTH:
            return (x, y - 1)
        if direction == DIR_SOUTH:
            return (x, y + 1)
        if direction == DIR_EAST:
            return (x + 1, y)
        if direction == DIR_WEST:
            return (x - 1, y)
        return (x, y)

    @staticmethod
    def _opposite_dir(direction: int) -> int:
        if direction == DIR_NORTH:
            return DIR_SOUTH
        if direction == DIR_SOUTH:
            return DIR_NORTH
        if direction == DIR_EAST:
            return DIR_WEST
        if direction == DIR_WEST:
            return DIR_EAST
        return 0

    @staticmethod
    def _is_dead_end(bits: int) -> bool:
        return bits in (DIR_NORTH, DIR_SOUTH, DIR_EAST, DIR_WEST)

    def _cell_key(self, x: int, y: int) -> int:
        return y * self.width + x

    # ── room-node placement ──────────────────────────────────────────────────

    def _place_room_nodes(self) -> None:
        self._placed_room_nodes.clear()

        for spec in self._room_nodes:
            if spec.chance <= 0 or self._roll100() > spec.chance:
                continue

            variants = self._get_tile_variants(spec.tile_set)
            while variants:
                variant_index = self.next_int(0, len(variants))
                tile_type = variants[variant_index]
                variants.pop(variant_index)

                exits = self._parse_exit_bits(spec.tile_set, tile_type)
                candidates = self._get_room_candidates(spec, exits)
                if not candidates:
                    continue

                cx, cy = candidates[self.next_int(0, len(candidates))]
                self._occupied[cy][cx] = True
                self._forced_exits[cy][cx] = exits
                self._room_tile_types[cy][cx] = tile_type
                self._placed_room_nodes.append(PlacedRoomNode(
                    source_index=spec.source_index, tile_set=spec.tile_set,
                    tile_type=tile_type, grid_x=cx, grid_y=cy,
                ))
                break

    def _get_tile_variants(self, tile_set: str) -> List[str]:
        """Concrete variant tiles for a room-node ``tile_set``, from client content.

        The client's per-tileset variant vector is the tileset's exit-suffixed
        ``.tile`` leaves sorted alphabetically (proven via x64dbg, 2026-06-09 —
        see [[project_dungeon_map_alignment]]). Derived live from the extracted
        content so it covers every theme with no hardcoded table: the C#
        ``RoomTileVariants`` only listed dungeon00's six tilesets, and the content
        resolver was verified to reproduce those six exactly (variant + order).
        Falls back to the bare ``tile_set`` when content has no exit-suffixed
        tiles (or the extracter is unavailable) — that room then carries 0 exits
        and cannot place, which is the correct "missing content" outcome.
        """
        from . import tile_cobj_resolver
        derived = tile_cobj_resolver.variants_for(tile_set or "")
        if derived:
            return derived
        return [tile_set]

    def _parse_exit_bits(self, tile_set: str, tile_type: str) -> int:
        suffix = tile_type or ""
        if tile_set and suffix.lower().startswith(tile_set.lower()):
            suffix = suffix[len(tile_set):]

        bits = 0
        if "1n" in suffix:
            bits |= DIR_NORTH
        if "1s" in suffix:
            bits |= DIR_SOUTH
        # Native TileLibrary::char2Direction mirrors tile-name e/w against maze bits.
        if "1e" in suffix:
            bits |= DIR_WEST
        if "1w" in suffix:
            bits |= DIR_EAST
        return bits

    def _get_room_candidates(self, spec: RoomNodeSpec, exits: int) -> List[_Cell]:
        min_x, max_x = 0, self.width - 1
        min_y, max_y = 0, self.height - 1

        if spec.grid_x is not None:
            x = spec.grid_x
            if x < 0:
                return []
            if x >= self.width:
                x = self.width - 1
            min_x = max_x = x

        if spec.grid_y is not None:
            y = spec.grid_y
            if y < 0:
                return []
            if y >= self.height:
                y = self.height - 1
            min_y = max_y = y

        candidates: List[_Cell] = []
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                if self._occupied[y][x]:
                    continue
                if not self._forced_exits_fit(x, y, exits):
                    continue
                if not self._map_fully_connected_with_candidate(x, y, exits):
                    continue
                candidates.append((x, y))
        return candidates

    def _forced_exits_fit(self, x: int, y: int, exits: int) -> bool:
        if exits == 0:
            return True
        for direction in _ORDERED_DIRS:
            if (exits & direction) == 0:
                continue
            nx, ny = self._neighbor(x, y, direction)
            if not self._in_bounds(nx, ny):
                return False
        return True

    def _map_fully_connected_with_candidate(self, candidate_x: int, candidate_y: int,
                                            candidate_exits: int) -> bool:
        total = self.width * self.height
        visited = {self._cell_key(candidate_x, candidate_y)}
        queue: deque = deque([(candidate_x, candidate_y)])

        while queue:
            cx, cy = queue.popleft()
            bits = self._candidate_cell_byte(cx, cy, candidate_x, candidate_y, candidate_exits)
            dirs = bits & 0x0F
            current_solid = bits != 0
            if bits == 0:
                dirs = 0x0F

            for direction in _EMPTY_NEIGHBOR_DIRS:
                if (dirs & direction) == 0:
                    continue

                nx, ny = self._neighbor(cx, cy, direction)
                if not self._in_bounds(nx, ny):
                    if current_solid:
                        return False
                    continue

                neighbor_bits = self._candidate_cell_byte(
                    nx, ny, candidate_x, candidate_y, candidate_exits)
                if neighbor_bits != 0 and (neighbor_bits & self._opposite_dir(direction)) == 0:
                    if current_solid:
                        return False
                    continue

                key = self._cell_key(nx, ny)
                if key not in visited:
                    visited.add(key)
                    queue.append((nx, ny))

        return len(visited) == total

    def _candidate_cell_byte(self, x: int, y: int, candidate_x: int,
                             candidate_y: int, candidate_exits: int) -> int:
        if x == candidate_x and y == candidate_y:
            return (CELL_ROOM | (candidate_exits & 0x0F)) & 0xFF
        if self._occupied[y][x]:
            return (CELL_ROOM | (self._forced_exits[y][x] & 0x0F)
                    | (self._cells[y][x] & 0x0F)) & 0xFF
        return self._cells[y][x]

    # ── corridor generation ──────────────────────────────────────────────────

    def _generate_corridors(self) -> None:
        empty_remaining = self.width * self.height - 1
        for y in range(self.height):
            for x in range(self.width):
                if self._occupied[y][x]:
                    empty_remaining -= 1
        if empty_remaining < 0:
            empty_remaining = 0

        current = self._get_random_empty_cell()
        has_current = current is not None
        use_corridor_fallback = False
        last_dir = 0
        straight_streak = 0

        while empty_remaining > 0:
            if not has_current:
                current = (self._get_random_corridor_cell()
                           if use_corridor_fallback else self._get_random_empty_cell())
                use_corridor_fallback = False
                if current is None:
                    break
                has_current = True

            x, y = current
            current_bits = self._cell_byte(x, y)
            blocked = self._blocked_directions(x, y, current_bits)
            chosen_dir = 0

            if (current_bits & CELL_ROOM) == 0:
                if (self._roll100() > self.randomness
                        and self._can_continue_dir(x, y, last_dir, straight_streak)):
                    chosen_dir = last_dir
                    straight_streak += 1
                else:
                    straight_streak = 0

            accepted = False
            while True:
                if chosen_dir != 0 and (blocked & chosen_dir) == 0:
                    nx, ny = self._neighbor(x, y, chosen_dir)
                    self._set_dir(x, y, chosen_dir)
                    self._set_dir(nx, ny, self._opposite_dir(chosen_dir))
                    current = (nx, ny)
                    has_current = True
                    last_dir = chosen_dir
                    empty_remaining -= 1
                    accepted = True
                    break

                chosen_dir = self._random_direction()
                nnx, nny = self._neighbor(x, y, chosen_dir)
                if not self._in_bounds(nnx, nny):
                    blocked |= chosen_dir
                elif self._cell_byte(nnx, nny) == 0:
                    continue
                else:
                    blocked |= chosen_dir

                if (blocked & 0x0F) == 0x0F:
                    break

            if not accepted:
                has_current = False
                use_corridor_fallback = True

    def _blocked_directions(self, x: int, y: int, bits: int) -> int:
        blocked = bits & 0x0F
        if (bits & CELL_ROOM) != 0:
            blocked = (~bits) & 0x0F

        if y <= 0:
            blocked |= DIR_NORTH
        if y + 1 >= self.height:
            blocked |= DIR_SOUTH
        if x <= 0:
            blocked |= DIR_WEST
        if x + 1 >= self.width:
            blocked |= DIR_EAST
        return blocked

    def _can_continue_dir(self, x: int, y: int, direction: int, streak: int) -> bool:
        if direction == 0 or streak >= self._straight_limit(direction):
            return False
        nx, ny = self._neighbor(x, y, direction)
        return self._in_bounds(nx, ny) and self._cell_byte(nx, ny) == 0

    def _straight_limit(self, direction: int) -> int:
        if direction in (DIR_EAST, DIR_WEST):
            return 0 if self.width <= 1 else self.width // 2
        if direction in (DIR_NORTH, DIR_SOUTH):
            return 0 if self.height <= 1 else self.height // 2
        return 0

    def _get_random_empty_cell(self) -> Optional[_Cell]:
        candidates: List[_Cell] = []
        for y in range(self.height):
            for x in range(self.width):
                if self._cell_byte(x, y) == 0:
                    candidates.append((x, y))
        if not candidates:
            return None
        return candidates[self.next_int(0, len(candidates))]

    def _get_random_corridor_cell(self) -> Optional[_Cell]:
        normal: List[_Cell] = []
        forced: List[_Cell] = []

        for y in range(self.height):
            for x in range(self.width):
                bits = self._cell_byte(x, y)
                if (bits & 0x0F) == 0:
                    continue

                empty_mask = self._empty_neighbor_mask(x, y)
                if empty_mask == 0:
                    continue

                if self._occupied[y][x]:
                    if (empty_mask & bits & 0x0F) != 0:
                        forced.append((x, y))
                    continue

                normal.append((x, y))

        if normal:
            return normal[self.next_int(0, len(normal))]
        if forced:
            return forced[self.next_int(0, len(forced))]
        return None

    def _get_random_dead_end_cell(self) -> Optional[_Cell]:
        candidates: List[_Cell] = []
        for y in range(self.height):
            for x in range(self.width):
                if not self._occupied[y][x] and self._is_dead_end(self._cells[y][x]):
                    candidates.append((x, y))
        if not candidates:
            return None
        return candidates[self.next_int(0, len(candidates))]

    def _empty_neighbor_mask(self, x: int, y: int) -> int:
        mask = 0
        for direction in _EMPTY_NEIGHBOR_DIRS:
            nx, ny = self._neighbor(x, y, direction)
            if self._in_bounds(nx, ny) and self._cell_byte(nx, ny) == 0:
                mask |= direction
        return mask

    # ── post-passes ──────────────────────────────────────────────────────────

    def _apply_forced_room_exits(self) -> None:
        for y in range(self.height):
            for x in range(self.width):
                exits = self._forced_exits[y][x]
                if exits == 0:
                    continue

                for direction in _ORDERED_DIRS:
                    nx, ny = self._neighbor(x, y, direction)
                    if not self._in_bounds(nx, ny):
                        continue

                    if (exits & direction) != 0:
                        self._set_dir(x, y, direction)
                        self._set_dir(nx, ny, self._opposite_dir(direction))
                    elif (self._cells[y][x] & direction) != 0:
                        self._unset_dir(x, y, direction)
                        self._unset_dir(nx, ny, self._opposite_dir(direction))

    def _sparsify(self) -> None:
        if self.sparseness <= 0:
            return

        for _ in range(self.sparseness):
            cell = self._get_random_dead_end_cell()
            if cell is None:
                return

            x, y = cell
            direction = self._cells[y][x]
            nx, ny = self._neighbor(x, y, direction)
            if not self._in_bounds(nx, ny) or self._occupied[ny][nx]:
                continue

            self._cells[y][x] = 0
            self._unset_dir(nx, ny, self._opposite_dir(direction))

    def _remove_dead_ends(self) -> None:
        for y in range(self.height):
            for x in range(self.width):
                if self._occupied[y][x] or not self._is_dead_end(self._cells[y][x]):
                    continue

                if self._roll100() > self.dead_end_removal_chance:
                    continue

                cx, cy = x, y
                while True:
                    current_bits = self._cells[cy][cx]
                    blocked = 0
                    chosen_dir = 0
                    nxt: _Cell = (0, 0)

                    while chosen_dir == 0:
                        direction = self._random_direction()
                        cand_x, cand_y = self._neighbor(cx, cy, direction)
                        if (not self._in_bounds(cand_x, cand_y)
                                or current_bits == direction
                                or self._occupied[cand_y][cand_x]):
                            blocked |= direction
                        else:
                            chosen_dir = direction
                            nxt = (cand_x, cand_y)

                        if (blocked & 0x0F) == 0x0F:
                            break

                    if chosen_dir == 0:
                        break

                    self._set_dir(cx, cy, chosen_dir)
                    self._set_dir(nxt[0], nxt[1], self._opposite_dir(chosen_dir))
                    cx, cy = nxt
                    if self._cells[cy][cx] != self._opposite_dir(chosen_dir):
                        break

    def _sync_openings_from_native_bits(self) -> None:
        for y in range(self.height):
            for x in range(self.width):
                opens = self._openings[y][x]
                opens.clear()
                bits = self._cell_byte(x, y)
                if bits & DIR_NORTH:
                    opens.add(NORTH)
                if bits & DIR_EAST:
                    opens.add(EAST)
                if bits & DIR_SOUTH:
                    opens.add(SOUTH)
                if bits & DIR_WEST:
                    opens.add(WEST)

    def _build_result(self, tile_set_prefix: str) -> List[MazeCell]:
        half_grid_x = self.width // 2
        half_grid_y = self.height // 2
        native_root_x = 0.0
        native_root_y = 0.0
        tile_size = self.tile_size  # per-tileset cell stride (resolved in generate)

        cells: List[MazeCell] = []
        for y in range(self.height):
            for x in range(self.width):
                opens = self._openings[y][x]
                conns = ""
                if NORTH in opens:
                    conns += "1n"
                # E/W mirror (see module docstring).
                if WEST in opens:
                    conns += "1e"
                if SOUTH in opens:
                    conns += "1s"
                if EAST in opens:
                    conns += "1w"
                if not conns:
                    conns = "0n"

                world_grid_y = y
                cell_ox = native_root_x + (x - half_grid_x) * tile_size
                cell_oy = native_root_y + (world_grid_y - half_grid_y) * tile_size

                tile_type = self._room_tile_types[y][x]
                if not tile_type:
                    tile_type = f"{tile_set_prefix}{conns}_a"

                cells.append(MazeCell(
                    grid_x=x,
                    grid_y=y,
                    world_grid_y=world_grid_y,
                    connections=conns,
                    tile_type=tile_type,
                    world_origin_x=float(cell_ox),
                    world_origin_y=float(cell_oy),
                    world_center_x=cell_ox + tile_size / 2.0,
                    world_center_y=cell_oy + tile_size / 2.0,
                    tile_size=float(tile_size),
                ))
        return cells

    # ── diagnostics (Phase D parity diffing — C# DumpGrid) ───────────────────

    def dump_grid(self) -> str:
        """Deterministic text dump for diffing against client-captured grids.

        Mirrors C# ``MazeGenerator.DumpGrid`` byte-for-byte so two dumps from the
        same seed compare equal and a server dump can be diffed against a client
        x32dbg capture (Phase D geometry-parity verification).
        """
        lines = [
            "# MazeGenerator grid dump v1",
            f"seed=0x{self.seed:08X}",
            f"width={self.width}",
            f"height={self.height}",
            f"randomness={self.randomness}",
            f"sparseness={self.sparseness}",
            f"deadEndRemoval={self.dead_end_removal_chance}",
            f"tileSize={self.tile_size}",
            f"placedRoomNodes={len(self._placed_room_nodes)}",
        ]
        for i, p in enumerate(self._placed_room_nodes):
            lines.append(
                f"  ROOMNODE idx={i} src={p.source_index} tileSet='{p.tile_set}' "
                f"tileType='{p.tile_type}' grid=({p.grid_x},{p.grid_y})"
            )
        lines.append("# cells: x y room dirs tileType")
        for y in range(self.height):
            for x in range(self.width):
                bits = self._cells[y][x]
                room = self._occupied[y][x]
                dirs = ""
                if bits & DIR_NORTH:
                    dirs += "N"
                if bits & DIR_EAST:
                    dirs += "E"
                if bits & DIR_SOUTH:
                    dirs += "S"
                if bits & DIR_WEST:
                    dirs += "W"
                if not dirs:
                    dirs = "-"
                tile_type = self._room_tile_types[y][x] or ""
                lines.append(
                    f"CELL x={x} y={y} room={1 if room else 0} dirs={dirs} "
                    f"raw=0x{bits:02X} tile='{tile_type}'"
                )
        lines.append("# end")
        return "\n".join(lines) + "\n"
