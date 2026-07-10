"""Parser for Dungeon Runners ``.cobj`` collision files.

Port of C# ``DungeonRunners.Utilities.CobjParser``. Each ``.cobj`` stores one
``HybridCollisionObject``: a two-layer grid encoding for static world geometry
(walls, props, floor segments). The format was reversed from
``dfc::HybridCollisionObject::readObject`` at ``0x004e4560`` in
``DungeonRunners.exe``.

File layout (little-endian)::

    uint8  tag                              # DFC stream marker (0x01/0x05) — ignored
    char   className[21] = "HybridCollisionObject"
    uint8  terminator = 0x00
    uint32 dfcHash                          # ignored by us
    # Sub-shape 1: heightmap (one uint16 per cell)
    int32  cellSize1
    int32  originX1, originY1
    int32  width1, height1
    uint16 heightmap[width1 * height1]
    # Sub-shape 2: per-cell vertical bbox stacks (bridges, stairs, archways)
    int32  cellSize2
    int32  originX2, originY2, originZ2
    int32  width2, height2, depth2
    # For each of (width2 * height2) cells:
    #   uint16 bboxCount
    #   struct { int16 zLow; int16 zHigh; } bboxes[bboxCount]

The two sub-shapes feed :mod:`drserver.managers.pathmap_builder`: sub-shape 1
flags wall cells (height above a threshold), sub-shape 2 flags vertical
structures (pillars, doorframes) whose Z extent overlaps the walking band.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from ..util.byte_io import LEReader

HEADER_SIZE = 27  # 1 tag + 21 className + 1 terminator + 4 hash
EXPECTED_CLASS_NAME = "HybridCollisionObject"
_MAX_DIM = 1024


@dataclass(frozen=True)
class CobjBBox:
    """A vertical extent within a sub-shape-2 cell (placement-local Z)."""

    z_low: int
    z_high: int


@dataclass(frozen=True)
class CobjBBoxCell:
    """The stack of vertical bboxes occupying one sub-shape-2 cell."""

    bboxes: Tuple[CobjBBox, ...] = ()


_EMPTY_CELL = CobjBBoxCell(())


@dataclass(frozen=True)
class CobjData:
    """Parsed ``HybridCollisionObject`` — both collision sub-shapes."""

    dfc_hash: int

    cell_size1: int
    origin_x1: int
    origin_y1: int
    width1: int
    height1: int
    heightmap: Tuple[int, ...]

    cell_size2: int
    origin_x2: int
    origin_y2: int
    origin_z2: int
    width2: int
    height2: int
    depth2: int
    cells: Tuple[CobjBBoxCell, ...]

    bytes_consumed: int

    def get_height(self, cx: int, cy: int) -> int:
        if cx < 0 or cx >= self.width1 or cy < 0 or cy >= self.height1:
            return 0
        return self.heightmap[cy * self.width1 + cx]

    def get_cell(self, cx: int, cy: int) -> CobjBBoxCell:
        if cx < 0 or cx >= self.width2 or cy < 0 or cy >= self.height2:
            return _EMPTY_CELL
        return self.cells[cy * self.width2 + cx]


def _to_int16(value: int) -> int:
    """Reinterpret an unsigned 16-bit value as signed (C# ``(short)``)."""
    return value - 0x10000 if value >= 0x8000 else value


def _validate_grid_dimensions(width: int, height: int, label: str) -> None:
    if width < 0 or height < 0 or width > _MAX_DIM or height > _MAX_DIM:
        raise ValueError(
            f"cobj {label} grid {width}x{height} out of range [0,{_MAX_DIM}]"
        )


def parse(data: bytes) -> CobjData:
    """Parse raw ``.cobj`` bytes into a :class:`CobjData`."""
    if data is None:
        raise ValueError("data is None")
    if len(data) < HEADER_SIZE:
        raise ValueError(
            f"cobj too short: {len(data)} bytes, need at least {HEADER_SIZE}"
        )

    reader = LEReader(data)

    # Tag byte varies per-file (0x01 or 0x05 observed). It's a DFC stream marker,
    # not a class identifier — read but don't validate.
    tag = reader.read_byte()

    name = reader.read_bytes(len(EXPECTED_CLASS_NAME)).decode("ascii", "replace")
    if name != EXPECTED_CLASS_NAME:
        raise ValueError(
            f"cobj className = '{name}', expected '{EXPECTED_CLASS_NAME}' "
            f"(tag was 0x{tag:02X})"
        )

    terminator = reader.read_byte()
    if terminator != 0x00:
        raise ValueError(f"cobj terminator = 0x{terminator:02X}, expected 0x00")

    dfc_hash = reader.read_uint32()

    cell_size1 = reader.read_int32()
    origin_x1 = reader.read_int32()
    origin_y1 = reader.read_int32()
    width1 = reader.read_int32()
    height1 = reader.read_int32()
    _validate_grid_dimensions(width1, height1, "sub-shape 1")

    cell_count1 = width1 * height1
    heightmap = tuple(reader.read_uint16() for _ in range(cell_count1))

    cell_size2 = reader.read_int32()
    origin_x2 = reader.read_int32()
    origin_y2 = reader.read_int32()
    origin_z2 = reader.read_int32()
    width2 = reader.read_int32()
    height2 = reader.read_int32()
    depth2 = reader.read_int32()
    _validate_grid_dimensions(width2, height2, "sub-shape 2")

    cell_count2 = width2 * height2
    cells: List[CobjBBoxCell] = []
    for _ in range(cell_count2):
        count = reader.read_uint16()
        bboxes = tuple(
            CobjBBox(_to_int16(reader.read_uint16()), _to_int16(reader.read_uint16()))
            for _ in range(count)
        )
        cells.append(CobjBBoxCell(bboxes) if bboxes else _EMPTY_CELL)

    return CobjData(
        dfc_hash=dfc_hash,
        cell_size1=cell_size1,
        origin_x1=origin_x1,
        origin_y1=origin_y1,
        width1=width1,
        height1=height1,
        heightmap=heightmap,
        cell_size2=cell_size2,
        origin_x2=origin_x2,
        origin_y2=origin_y2,
        origin_z2=origin_z2,
        width2=width2,
        height2=height2,
        depth2=depth2,
        cells=tuple(cells),
        bytes_consumed=reader.position,
    )


def parse_file(path: str) -> CobjData:
    with open(path, "rb") as fh:
        return parse(fh.read())
