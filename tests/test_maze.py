"""Structural + determinism tests for the client-faithful maze generator port.

We cannot run the C# reference here (no .NET runtime), so exact-layout parity is
validated live against the native client (Phase D: ``dump_grid`` vs an x32dbg
capture). These tests lock the invariants the algorithm must hold and guard
against RNG-order regressions: determinism per seed, symmetric openings, the
E/W tile-name mirror, the world-origin transform, and forced room-node placement.
"""
from drserver.managers.maze import (
    MazeGenerator, TILE_SIZE, NORTH, EAST, SOUTH, WEST,
)

SEED = 0xBEEFBEEF


def test_same_seed_produces_identical_maze():
    a = MazeGenerator(4, 5, SEED).generate()
    b = MazeGenerator(4, 5, SEED).generate()
    assert [(c.grid_x, c.grid_y, c.connections) for c in a] == \
           [(c.grid_x, c.grid_y, c.connections) for c in b]


def test_dump_grid_is_deterministic_per_seed():
    a = MazeGenerator(6, 6, SEED)
    a.generate()
    b = MazeGenerator(6, 6, SEED)
    b.generate()
    assert a.dump_grid() == b.dump_grid()


def test_different_seeds_produce_different_mazes():
    a = MazeGenerator(4, 5, 0xBEEFBEEF).generate()
    b = MazeGenerator(4, 5, 0x12345678).generate()
    assert [c.connections for c in a] != [c.connections for c in b]


def test_cell_count_matches_dimensions():
    cells = MazeGenerator(4, 5, SEED).generate()
    assert len(cells) == 4 * 5


def test_openings_are_symmetric_with_ew_mirror():
    """A passage is mutual. Tile-name e/w is mirrored against the grid: a cell
    that opens toward its EAST neighbour is named ``1w``, and that neighbour —
    opening back WEST — is named ``1e`` (native TileLibrary::char2Direction).

    Grid coords: NORTH is ``y-1`` (Neighbor(DIR_NORTH)), so ``has_north`` (``1n``)
    on (x,y) must pair with ``has_south`` (``1s``) on (x, y-1).
    """
    w, h = 6, 6
    cells = {(c.grid_x, c.grid_y): c for c in MazeGenerator(w, h, SEED).generate()}
    for (x, y), cell in cells.items():
        if cell.has_north and (x, y - 1) in cells:
            assert cells[(x, y - 1)].has_south
        if cell.has_west and (x + 1, y) in cells:   # "1w" == opens EAST in grid
            assert cells[(x + 1, y)].has_east       # neighbour opens WEST == "1e"


def test_world_origin_anchored_at_grid_centre():
    """CenterOverride is ignored: cell (halfX, halfY) sits at world ~(0,0), and
    cell origins are ``(x-halfX)*TILE`` / ``(y-halfY)*TILE`` from native root 0,0.
    """
    w, h = 4, 5
    cells = {(c.grid_x, c.grid_y): c for c in MazeGenerator(w, h, SEED).generate()}
    half_x, half_y = w // 2, h // 2

    centre = cells[(half_x, half_y)]
    assert centre.world_origin_x == 0.0
    assert centre.world_origin_y == 0.0

    corner = cells[(0, 0)]
    assert corner.world_origin_x == (0 - half_x) * TILE_SIZE
    assert corner.world_origin_y == (0 - half_y) * TILE_SIZE
    assert corner.world_center_x == corner.world_origin_x + TILE_SIZE / 2.0
    assert corner.world_center_y == corner.world_origin_y + TILE_SIZE / 2.0


def test_per_tileset_stride_scales_cell_origins(monkeypatch):
    """The cell stride is the tileset's ``TileSize`` (resolved from content), not a
    fixed 400: a cave maze (360) spaces cells by 360, ruins (280) by 280. This is
    the dungeon01 spawn-in-walls fix — every cell origin scales with the size."""
    import drserver.managers.maze as maze

    def _gen_with_size(size):
        monkeypatch.setattr(maze, "_resolve_tile_size", lambda prefix: size)
        cells = {(c.grid_x, c.grid_y): c
                 for c in maze.MazeGenerator(5, 5, SEED).generate("x_")}
        return cells

    for size in (400, 360, 280, 520):
        cells = _gen_with_size(size)
        half = 5 // 2
        corner = cells[(0, 0)]
        assert corner.world_origin_x == (0 - half) * size
        assert corner.world_origin_y == (0 - half) * size
        assert corner.world_center_x == corner.world_origin_x + size / 2.0
        assert corner.tile_size == float(size)
        # Grid placement (which cell) is independent of stride.
    assert set(_gen_with_size(360)) == set(_gen_with_size(400))


def test_centre_override_is_ignored():
    """Setting center_override has no effect on the generated geometry."""
    plain = {(c.grid_x, c.grid_y): c for c in MazeGenerator(4, 5, SEED).generate()}
    gen = MazeGenerator(4, 5, SEED)
    gen.center_override_x = 9999.0
    gen.center_override_y = 9999.0
    overridden = {(c.grid_x, c.grid_y): c for c in gen.generate()}
    assert plain[(0, 0)].world_origin_x == overridden[(0, 0)].world_origin_x
    assert plain[(0, 0)].world_origin_y == overridden[(0, 0)].world_origin_y


def test_corridor_tile_type_uses_prefix_and_connections():
    """With no room nodes, every cell is a corridor named from its connections."""
    cells = MazeGenerator(4, 5, SEED).generate()
    for c in cells:
        assert c.tile_type == f"elmforest_tileset_{c.connections}_a"


def test_room_node_forced_to_requested_cell(monkeypatch):
    """A room node pinned to a grid cell lands there with a room-set tile.

    Variants come from the content resolver (no hardcoded table), so stub it for
    a hermetic test that does not depend on the extracted client content.
    """
    import drserver.managers.tile_cobj_resolver as r
    monkeypatch.setattr(
        r, "variants_for",
        lambda ts: ["elmforest_hub_1e", "elmforest_hub_1n",
                    "elmforest_hub_1s", "elmforest_hub_1w"]
        if ts == "elmforest_hub_" else [],
    )
    gen = MazeGenerator(5, 5, SEED)
    gen.add_room_node("elmforest_hub_", grid_x=2, grid_y=2, chance=100, source_index=7)
    cells = {(c.grid_x, c.grid_y): c for c in gen.generate()}

    placed = gen.placed_room_nodes
    assert len(placed) == 1
    node = placed[0]
    assert (node.grid_x, node.grid_y) == (2, 2)
    assert node.source_index == 7
    assert node.tile_set == "elmforest_hub_"
    assert node.tile_type.startswith("elmforest_hub_")

    # The generated cell carries the placed room tile, not the corridor name.
    assert cells[(2, 2)].tile_type == node.tile_type
    assert not cells[(2, 2)].tile_type.startswith("elmforest_tileset_")


def test_get_tile_variants_derives_from_content(monkeypatch):
    """Variants come from the content resolver — no hardcoded table. The bare
    tile_set is the fallback when content yields nothing (extracter absent)."""
    import drserver.managers.tile_cobj_resolver as r
    monkeypatch.setattr(
        r, "variants_for",
        lambda ts: ["elmforest_hub_1e", "elmforest_hub_1n",
                    "elmforest_hub_1s", "elmforest_hub_1w"]
        if ts == "elmforest_hub_" else [],
    )
    gen = MazeGenerator(4, 4, SEED)
    assert gen._get_tile_variants("elmforest_hub_") == [
        "elmforest_hub_1e", "elmforest_hub_1n",
        "elmforest_hub_1s", "elmforest_hub_1w",
    ]
    # No content match → bare tile_set fallback (0 exits, cannot place).
    assert gen._get_tile_variants("unknown_set_") == ["unknown_set_"]


def test_non_dungeon00_room_node_places_via_resolver(monkeypatch):
    """A room node with an exit-less tile_set (no hardcoded entry) places by
    deriving variants from client content — without this it never placed and the
    maze diverged from the client. The resolver is stubbed for hermeticity."""
    import drserver.managers.tile_cobj_resolver as r
    monkeypatch.setattr(
        r, "variants_for",
        lambda ts: ["cat_up_1e", "cat_up_1n", "cat_up_1s", "cat_up_1w"]
        if ts == "cat_up_" else [],
    )

    gen = MazeGenerator(5, 5, SEED)
    gen.add_room_node("cat_up_", grid_x=2, grid_y=2, chance=100, source_index=3)
    gen.generate("cat_tileset_")

    placed = gen.placed_room_nodes
    assert len(placed) == 1
    assert (placed[0].grid_x, placed[0].grid_y) == (2, 2)
    assert placed[0].tile_type in {
        "cat_up_1e", "cat_up_1n", "cat_up_1s", "cat_up_1w",
    }


def test_exitless_tile_set_without_variants_does_not_place(monkeypatch):
    """Guards the bug's signature: an exit-less tile_set with NO resolvable
    variants falls back to the bare set (0 exits) and cannot place."""
    import drserver.managers.tile_cobj_resolver as r
    monkeypatch.setattr(r, "variants_for", lambda ts: [])

    gen = MazeGenerator(5, 5, SEED)
    gen.add_room_node("cat_up_", grid_x=2, grid_y=2, chance=100, source_index=3)
    gen.generate("cat_tileset_")

    assert gen.placed_room_nodes == []


def test_parse_exit_bits_mirrors_east_west():
    """ParseExitBits maps tile-name 1e→WEST bit and 1w→EAST bit (the mirror)."""
    from drserver.managers.maze import DIR_NORTH, DIR_SOUTH, DIR_EAST, DIR_WEST
    gen = MazeGenerator(4, 4, SEED)
    assert gen._parse_exit_bits("elmforest_hub_", "elmforest_hub_1e") == DIR_WEST
    assert gen._parse_exit_bits("elmforest_hub_", "elmforest_hub_1w") == DIR_EAST
    assert gen._parse_exit_bits("elmforest_hub_", "elmforest_hub_1n") == DIR_NORTH
    assert gen._parse_exit_bits("elmforest_hub_", "elmforest_hub_1s") == DIR_SOUTH
