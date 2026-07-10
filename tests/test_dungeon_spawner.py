"""Dungeon maze spawner tests — port of C# DungeonMazeSpawner.

Validates that procedural dungeon levels generate maze-placed spawns (not the
old random-near-player cluster), that placement is deterministic per seed, that
positions sit inside the maze footprint, and that the static boss-arena path
still loads from the ``dungeon_spawns`` table. Exact room parity is confirmed
live against the native client.
"""
import _paths
from drserver.db import game_database
from drserver.managers import dungeon_spawner as ds
from drserver.managers import monsters as m

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 100
        self.combat = None

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


def test_is_procedural_zone_only_for_maze_levels():
    assert ds.is_procedural_zone("dungeon00_level01")
    assert ds.is_procedural_zone("dungeon00_level02")
    assert ds.is_procedural_zone("dungeon00_level03")
    assert not ds.is_procedural_zone("dungeon00_level03_boss")
    assert not ds.is_procedural_zone("tutorial")
    assert not ds.is_procedural_zone("town")
    assert not ds.is_procedural_zone("")


def test_generate_spawns_produces_mobs():
    spawns = ds.generate_spawns("dungeon00_level01")
    assert len(spawns) > 0
    # Entity types are the real client-loadable world assets read from the .enc;
    # each also carries the resolved creatures.* row used for HP/level stats.
    assert all(s.gc_type.lower().startswith("world.") for s in spawns)
    assert all(s.creature_gc_type.startswith("creatures.") for s in spawns)


def test_generate_spawns_is_deterministic_per_seed():
    a = ds.generate_spawns("dungeon00_level01")
    b = ds.generate_spawns("dungeon00_level01")
    assert [(s.gc_type, s.pos_x, s.pos_y, s.pos_z, s.heading) for s in a] == \
           [(s.gc_type, s.pos_x, s.pos_y, s.pos_z, s.heading) for s in b]


def test_spawns_are_spread_not_clustered():
    """The old bug placed all mobs within ±100 of one point. Maze placement
    must spread them across the dungeon footprint (>>200u span)."""
    spawns = ds.generate_spawns("dungeon00_level01")
    xs = [s.pos_x for s in spawns]
    ys = [s.pos_y for s in spawns]
    assert (max(xs) - min(xs)) > 300
    assert (max(ys) - min(ys)) > 300


def test_spawns_lie_within_maze_footprint():
    # dungeon00_level01: 4x5 tiles of 400u centered on the pathmap center.
    spawns = ds.generate_spawns("dungeon00_level01")
    for s in spawns:
        assert -1100 <= s.pos_x <= 1100
        assert -1100 <= s.pos_y <= 1500


def test_non_procedural_zone_returns_empty():
    assert ds.generate_spawns("dungeon00_level03_boss") == []
    assert ds.generate_spawns("tutorial") == []


def test_no_static_spawns_remain():
    # The dungeon00 boss arena was the only hand-authored dungeon_spawns zone;
    # those static rows were dropped (dungeon00 is fully data-driven now), so the
    # static loader returns nothing for it.
    assert ds.load_static_spawns("dungeon00_level03_boss") == []


def test_layout_seed_is_deterministic_per_zone_and_uint32():
    # Same zone name → same seed (so every joiner agrees, and the spawner seed
    # matches the value game_server sends in the 13/0x00 zone-connect packet).
    assert ds.layout_seed("dungeon01_level02") == ds.layout_seed("dungeon01_level02")
    # Case-insensitive (zone names are matched COLLATE NOCASE everywhere).
    assert ds.layout_seed("Dungeon01_Level02") == ds.layout_seed("dungeon01_level02")
    # Different levels get different layouts (the old 0xBEEFBEEF constant made
    # levels with identical maze params render byte-identical).
    assert ds.layout_seed("dungeon01_level01") != ds.layout_seed("dungeon01_level02")
    # Always a uint32.
    for z in ("", "town", "dungeon00_level03"):
        s = ds.layout_seed(z)
        assert 0 <= s <= 0xFFFFFFFF


def test_generate_spawns_registers_per_instance_pathmap():
    from drserver.managers.pathmap import pathmap_manager

    key = "dungeon00_level01_inst777"
    pathmap_manager.unregister_instance(key)  # ensure clean slate
    try:
        spawns = ds.generate_spawns("dungeon00_level01", instance_key=key)
        assert len(spawns) > 0
        pm = pathmap_manager.get(key)
        assert pm is not None, "instance pathmap should be registered under its key"
        assert pm.node_count > 0
    finally:
        pathmap_manager.unregister_instance(key)
    # After teardown the instance map is gone; a procedural instance key with no
    # registered map must NOT fall back to a static map (would mis-snap mobs).
    assert pathmap_manager.get(key) is None


def test_build_monsters_from_maze_spawns_yields_packets():
    spawns = ds.generate_spawns("dungeon00_level01")
    tuples = [(s.gc_type, s.creature_gc_type, s.pos_x, s.pos_y, s.pos_z, s.heading)
              for s in spawns]
    built = m.build_monsters_from_spawns(
        _FakeServer(), "dungeon00_level01", "world.dungeon00", tuples)
    # The .enc resolved every unit to a known creature at import time, so all
    # should build (world.* entity passes through, creatures.* gives stats).
    assert len(built) == len(spawns)
    for entity_id, packet in built:
        assert packet[0] == 0x07          # BeginStream
        assert packet[-1] == 0x06         # EndStream


def test_entry_position_is_the_placed_maze_entrance():
    # A procedural level resolves a maze entry as (x, y, z, heading); non-maze None.
    entry = ds.entry_position("dungeon11_level05")
    assert entry is not None
    assert len(entry) == 4
    x, y, z, heading = entry
    assert isinstance(x, float) and isinstance(y, float)
    # Z is the floor-snapped ground height (float) when the geometry pathmap
    # builds, else None (caller keeps its resolved Z).
    assert z is None or isinstance(z, float)
    assert isinstance(heading, float)
    assert ds.entry_position("tutorial") is None
    assert ds.entry_position("town") is None

    # Addressing the entrance by its SpawnName (how a portal targets a specific
    # arrival tile) resolves the same cell as the default entrance fallback.
    by_name = ds.entry_position("dungeon11_level05", "start5")
    assert by_name == entry

    # Deterministic per layout seed (every joiner agrees on the arrival tile).
    assert ds.entry_position("dungeon11_level05") == entry


def test_entry_position_resolves_for_startroom_less_solo_dungeon():
    """dungeon_snowman (the Snowman Sanctuary teleport target) authors EVERY room
    as a plain ``roomnode`` — no StartRoom node kind, no SpawnName — but names the
    entrance room's tileset ``icecave_snowman_start_``. Without the tileset /
    any-cell fallback, entry_position returned None and the caller placed the
    player at the zone's (0,0,0) default → outside the map (live 2026-07-01:
    "teleport to sanctuary works but I'm spawning outside map bounds")."""
    entry = ds.entry_position("dungeon_snowman", "start")
    assert entry is not None, "must resolve an in-maze entry, not fall back to (0,0,0)"
    x, y, z, heading = entry
    # The maze is anchored at world origin; the entry must land INSIDE it, not at
    # the origin the zone default would place the player.
    assert (x, y) != (0.0, 0.0)
    assert isinstance(x, float) and isinstance(y, float)
    # No spawn_point (respawn / recall) still resolves the same start room.
    assert ds.entry_position("dungeon_snowman") == entry


def test_warp_gates_are_data_driven_from_room_nodes():
    gates = ds.warp_gates("dungeon11_level05")
    # Every placed room node carrying a link becomes a gate; this level wires its
    # back-link (mainentrance), forward-link (exit) and side areas.
    assert len(gates) >= 2
    assert all(g.link_to_zone for g in gates)
    kinds = {g.node_kind for g in gates}
    assert "mainentrance" in kinds and "exit" in kinds
    # The forward exit points at the next level at its named spawn.
    exit_gate = next(g for g in gates if g.node_kind == "exit")
    assert exit_gate.link_to_zone == "dungeon11_level06"
    assert exit_gate.link_to_spawn == "start6"
    # Each gate carries the authored ZonePortal heading (model rotation), not 0.
    assert all(isinstance(g.heading, float) for g in gates)
    # Non-maze zones have no maze gates.
    assert ds.warp_gates("town") == []


def test_entry_resolves_to_the_mainentrance_gate_cell():
    # The player arrives in the same entrance cell the mainentrance warp sits in,
    # but offset from the gate toward the corridor opening (then floor-snapped) so
    # it never overlaps the gate trigger — that co-location was the teleport-back
    # bug. It stays within the same cell: at most one floor-snap spiral away.
    gates = ds.warp_gates("dungeon11_level05")
    entrance = next(g for g in gates if g.node_kind == "mainentrance")
    entry = ds.entry_position("dungeon11_level05")
    dist = ((entry[0] - entrance.world_x) ** 2
            + (entry[1] - entrance.world_y) ** 2) ** 0.5
    assert dist <= 300.0  # within the FindWalkableSpot radius (250) + margin


def test_authored_spawn_uses_the_spawnpoint_waypoint_verbatim():
    from drserver.managers.tile_layout_loader import AuthoredAnchor
    # Fully data-driven: the player spawn is the authored SpawnPoint waypoint
    # (x, y, z, heading) verbatim — no guessed offset, no hardcoded lift.
    ds._tile_anchor_cache["synthetic_entrance"] = (
        AuthoredAnchor(234.0, 73.0, 10.0, -180.0),  # player SpawnPoint
        AuthoredAnchor(236.0, 128.0, 14.0, 0.0),    # ZonePortal
    )
    try:
        assert ds._authored_spawn("synthetic_entrance") == (234.0, 73.0, 10.0, -180.0)
    finally:
        ds._tile_anchor_cache.pop("synthetic_entrance", None)


def test_authored_spawn_falls_back_to_portal_anchor_when_no_waypoint():
    from drserver.managers.tile_layout_loader import AuthoredAnchor
    # A tile with no SpawnPoint falls back to the authored ZonePortal anchor —
    # still designer-placed data, not a guessed opening-offset.
    ds._tile_anchor_cache["synthetic_portal"] = (
        None, AuthoredAnchor(160.0, 305.0, 30.0, 90.0))
    try:
        assert ds._authored_spawn("synthetic_portal") == (160.0, 305.0, 30.0, 90.0)
    finally:
        ds._tile_anchor_cache.pop("synthetic_portal", None)


def test_authored_spawn_is_none_for_anchorless_tile():
    ds._tile_anchor_cache["synthetic_corridor"] = (None, None)
    try:
        assert ds._authored_spawn("synthetic_corridor") is None
    finally:
        ds._tile_anchor_cache.pop("synthetic_corridor", None)


def test_expand_encounter_group_fills_to_a_pack():
    # A 1-2 unit encounter must expand into a PACK (the original game spawned
    # 3-5 mobs per spot, not the literal authored units). Port of C#
    # DungeonMazeSpawner.ExpandEncounterGroup.
    group = [("world.x.warg", "creatures.warg", 0.5),
             ("world.x.brood", "creatures.brood", 0.5)]
    seed = ds._stable_spot_seed("dungeon00_level03:enc:0")
    expanded = ds.expand_encounter_group(group, seed)
    assert len(expanded) > len(group)          # filled beyond the literal units
    # Total spent difficulty never exceeds the spot budget.
    spent = sum(u[2] for u in expanded if u[2] > 0)
    assert spent <= ds._resolve_spot_budget(seed) + 1e-6
    # Only the authored unit types appear (no new creatures invented).
    assert {u[0] for u in expanded} <= {u[0] for u in group}


def test_expand_keeps_zero_difficulty_units_single():
    # A difficulty-0 unit (anchor / breakable prop) spawns exactly once and is
    # never used to fill the budget.
    group = [("world.x.anchor", "creatures.anchor", 0.0),
             ("world.x.brood", "creatures.brood", 0.5)]
    seed = ds._stable_spot_seed("dungeon00_level03:enc:1")
    expanded = ds.expand_encounter_group(group, seed)
    anchors = [u for u in expanded if u[0] == "world.x.anchor"]
    assert len(anchors) == 1
    assert len(expanded) >= 2                   # the weighted brood still fills


def test_expand_is_deterministic_per_spot():
    group = [("world.x.warg", "creatures.warg", 0.35)]
    seed = ds._stable_spot_seed("dungeon00_level03:enc:2")
    assert ds.expand_encounter_group(group, seed) == \
           ds.expand_encounter_group(group, seed)


def test_static_boss_zone_spawns_packs_not_singletons():
    # The boss arena's 12 authored markers must now each spawn a pack, so the
    # zone is populated like the original game (was 1-2 mobs/marker).
    spawns = ds.generate_static_spawns("dungeon00_level03_boss")
    assert len(spawns) > 12                     # more than one mob per marker
    assert all(s.gc_type for s in spawns)


def test_sample_mob_units_is_data_driven():
    units = ds.sample_mob_units("dungeon00_level01", count=5)
    assert len(units) == 5
    # Real client-loadable assets + a creature row for stats — no hardcoded map.
    assert all(e.lower().startswith("world.") for e, _c in units)
    assert all(c.startswith("creatures.") for _e, c in units)
    # Non-maze zones / non-positive counts yield nothing.
    assert ds.sample_mob_units("town", 5) == []
    assert ds.sample_mob_units("dungeon00_level01", 0) == []
