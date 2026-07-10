"""Unit tests for the geometry→PathMap builder and per-instance registry.

Covers (Phase B):
  * ``PathMap.create_empty`` / ``world_to_grid`` round-trip,
  * world-bounds + sub-shape-2 band-overlap helpers,
  * a synthetic end-to-end build (resolver/loader monkeypatched so the test is
    deterministic and does not need the extracter),
  * ``PathMapManager`` instance registration + the procedural-instance refusal.
"""
import _paths  # noqa: F401  (sets up sys.path)
from drserver.data.cobj_parser import CobjBBox, CobjData
from drserver.managers import pathmap_builder as pb
from drserver.managers.maze import MazeCell
from drserver.managers.pathmap import PathMap, PathMapManager
from drserver.managers.tile_layout_loader import TileLayout, TilePlacement


def _cell(gx, gy, ox, oy, tile_type="t", tile_size=400.0):
    return MazeCell(
        grid_x=gx, grid_y=gy, world_grid_y=gy, connections="1n1s1e1w",
        tile_type=tile_type, world_origin_x=float(ox), world_origin_y=float(oy),
        world_center_x=ox + tile_size / 2.0, world_center_y=oy + tile_size / 2.0,
        tile_size=tile_size)


# ── PathMap.create_empty ────────────────────────────────────────────────────

def test_create_empty_world_to_grid_roundtrip():
    # Arrange / Act
    pm = PathMap.create_empty("z", min_world_x=-800.0, max_world_x=800.0,
                              min_world_y=-400.0, max_world_y=400.0)

    # Assert: offset is the lower-left bound so gx/gy reproduce builder indices.
    assert pm.world_offset_x == -800.0 and pm.world_offset_y == -400.0
    assert pm.world_to_grid(-800.0, -400.0) == (0, 0)
    assert pm.world_to_grid(-800.0 + 50.0, -400.0 + 30.0) == (5, 3)
    assert pm.node_count == 0


# ── builder helpers ─────────────────────────────────────────────────────────

def test_compute_world_bounds_spans_cell_footprints():
    cells = [_cell(0, 0, -400, -400), _cell(1, 0, 0, -400), _cell(0, 1, -400, 0)]
    min_x, max_x, min_y, max_y = pb._compute_world_bounds(cells)
    assert (min_x, min_y) == (-400.0, -400.0)
    # right/top edge = max origin + one tile
    assert (max_x, max_y) == (0.0 + pb.MAZE_TILE_SIZE, 0.0 + pb.MAZE_TILE_SIZE)


def test_stack_blocks_walking_band_overlap_only():
    # A ground-level pillar overlaps [0,30] -> blocks.
    assert pb._stack_blocks_walking_band([CobjBBox(0, 25)], base_z=0, walking_z_max=30)
    # A high bridge (zLow 80) does not overlap the walking band -> no block.
    assert not pb._stack_blocks_walking_band([CobjBBox(80, 120)], base_z=0, walking_z_max=30)
    # base_z (cobj originZ2 + placement z) lifts a bbox out of the band.
    assert not pb._stack_blocks_walking_band([CobjBBox(0, 25)], base_z=100, walking_z_max=30)


# ── synthetic end-to-end build ──────────────────────────────────────────────

def _patch_synthetic(monkeypatch, layout, cobj):
    monkeypatch.setattr(pb.tile_cobj_resolver, "resolve_tile_path",
                        lambda name: "fake.tile")
    monkeypatch.setattr(pb.tile_cobj_resolver, "resolve_cobj_path",
                        lambda path: "fake.cobj")
    monkeypatch.setattr(pb.tile_layout_loader, "load", lambda path: layout)
    import drserver.data.cobj_parser as real_cp
    monkeypatch.setattr(real_cp, "parse_file", lambda path: cobj)


def _wall_and_floor_cobj(size=4):
    # Tall wall strip along the first column (h 99 > threshold 30), flat floor
    # (h 0) elsewhere.
    heightmap = []
    for cy in range(size):
        for cx in range(size):
            heightmap.append(99 if cx == 0 else 0)
    return CobjData(
        dfc_hash=0, cell_size1=10, origin_x1=0, origin_y1=0,
        width1=size, height1=size, heightmap=tuple(heightmap),
        cell_size2=0, origin_x2=0, origin_y2=0, origin_z2=0,
        width2=0, height2=0, depth2=0, cells=(), bytes_consumed=0)


def test_build_marks_walls_blocked_floor_walkable_and_holes_blocked(monkeypatch):
    # One placement covering only the cell's first 40×40 units: walls block,
    # its floor cells are walkable, and the UNCOVERED remainder of the cell is
    # a hole (no geometry) — NOT walkable (mobs used to spawn in such voids).
    layout = TileLayout("t.tile", "base.World",
                        (TilePlacement("terrain.x.wallpiece", 0.0, 0.0, 0.0, 0.0),))
    _patch_synthetic(monkeypatch, layout, _wall_and_floor_cobj())

    pm = pb.build("synthetic_inst1", [_cell(0, 0, 0, 0)])

    assert pm is not None
    assert pm.node_count > 0
    # The wall column sits near the cell origin -> blocked.
    assert not pm.is_walkable(5.0, 5.0)
    # A covered floor cell is walkable.
    assert pm.is_walkable(20.0, 20.0)
    # Far corner of the cell footprint has NO geometry -> hole -> blocked.
    assert not pm.is_walkable(380.0, 380.0)


def test_build_floor_height_comes_from_placement_z(monkeypatch):
    # Floor pieces are all-zero heightmaps placed at an authored Z (raised
    # rooms, e.g. cave_small at z≈10); the node must carry placement.z + h so
    # mobs spawn ON the floor instead of half-sunk at z=0.
    layout = TileLayout("t.tile", "base.World",
                        (TilePlacement("terrain.x.floorpiece", 0.0, 0.0, 12.0, 0.0),))
    _patch_synthetic(monkeypatch, layout, _wall_and_floor_cobj())

    pm = pb.build("synthetic_inst_z", [_cell(0, 0, 0, 0)])

    assert pm is not None
    assert pm.is_walkable(20.0, 20.0)
    assert pm.get_height_at(20.0, 20.0, default_height=-1.0) == 12.0


def test_build_nowalk_placement_blocks_its_floor(monkeypatch):
    # Designer-marked no-walk floor (leaf contains "nowalk") blocks even though
    # its heightmap is below the wall threshold.
    layout = TileLayout("t.tile", "base.World",
                        (TilePlacement("terrain.x.FloorNoWalk_40", 0.0, 0.0, 0.0, 0.0),))
    _patch_synthetic(monkeypatch, layout, _wall_and_floor_cobj())

    pm = pb.build("synthetic_inst_nw", [_cell(0, 0, 0, 0)])

    assert pm is not None
    assert not pm.is_walkable(20.0, 20.0)


def test_build_cell_without_geometry_falls_back_open(monkeypatch):
    # A cell whose tile can't be resolved keeps the legacy open footprint
    # (missing content must degrade gracefully, not empty the level).
    monkeypatch.setattr(pb.tile_cobj_resolver, "resolve_tile_path",
                        lambda name: None)

    pm = pb.build("synthetic_inst_fb", [_cell(0, 0, 0, 0)])

    assert pm is not None
    assert pm.is_walkable(200.0, 200.0)
    assert pm.get_height_at(200.0, 200.0, default_height=-1.0) == 0.0


def test_build_returns_none_for_empty_cells():
    assert pb.build("z", []) is None


# ── PathMapManager registry ─────────────────────────────────────────────────

def test_register_and_get_instance_pathmap():
    mgr = PathMapManager()
    pm = PathMap.create_empty("dungeon01_level01_inst7", -10, 10, -10, 10)
    mgr.register_instance("dungeon01_level01_inst7", pm)
    assert mgr.get("dungeon01_level01_inst7") is pm
    assert mgr.get("DUNGEON01_LEVEL01_INST7") is pm  # case-insensitive


def test_unregister_instance_frees_map():
    mgr = PathMapManager()
    pm = PathMap.create_empty("dungeon01_level01_inst7", -10, 10, -10, 10)
    mgr.register_instance("dungeon01_level01_inst7", pm)
    mgr.unregister_instance("dungeon01_level01_inst7")
    # No registered instance + procedural base -> refuse (None), never the static base.
    assert mgr.get("dungeon01_level01_inst7") is None


def test_procedural_instance_without_map_refuses_static_base(monkeypatch):
    mgr = PathMapManager()
    monkeypatch.setattr("drserver.managers.pathmap._is_procedural_zone",
                        lambda base: True)
    assert mgr.get("dungeon01_level01_inst99") is None
