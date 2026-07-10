"""Data-driven dungeon spawn — regression guard for the dungeon02 crash.

Before this work the spawner forced dungeon00's ``melee0N.rankN`` family map onto
every dungeon, so warping into ``dungeon02_level03`` emitted
``world.dungeon02.mob.melee01.rank3`` — an asset the client cannot load →
``processEntityCreate ERROR: Invalid entity type`` → crash. The fix imports each
dungeon's real ``.world`` / ``.enc`` content into ``dungeon_levels`` /
``dungeon_encounters`` and emits the verbatim ``world.<dungeon>.mob.*`` asset.

These tests run against the shipped DB (which now carries the imported tables) so
they validate the end-to-end runtime path, plus a lightweight importer unit test
gated on the extracted client content being present.
"""
import os

import _paths
from drserver.db import game_database
from drserver.managers import dungeon_spawner as ds
from drserver.managers import monsters as m

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 500
        self.combat = None

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


def test_dungeon02_level03_is_maze_level():
    level = ds._load_level("dungeon02_level03")
    assert level is not None
    assert level.tile_prefix == "cave_large_tileset_"
    assert level.maze_width == 5 and level.maze_height == 5


def test_dungeon02_spawns_use_real_client_mob_assets():
    """The crash fix: every spawned entity type must be a ``world.dungeon02.mob.*``
    asset (never the old ``melee0N.rankN`` invention), with a resolved creature."""
    spawns = ds.generate_spawns("dungeon02_level03")
    assert len(spawns) > 0
    for s in spawns:
        assert s.gc_type.lower().startswith("world.dungeon02.mob."), s.gc_type
        assert "melee0" not in s.gc_type.lower()
        assert s.creature_gc_type.startswith("creatures."), s.creature_gc_type


def test_dungeon02_create_streams_carry_world_type():
    """Build the create packets and confirm the OP1 create gc_type is the
    client-loadable world asset (the exact byte the client rejected before)."""
    spawns = ds.generate_spawns("dungeon02_level03")
    tuples = [(s.gc_type, s.creature_gc_type, s.pos_x, s.pos_y, s.pos_z, s.heading)
              for s in spawns]
    built = m.build_monsters_from_spawns(
        _FakeServer(), "dungeon02_level03", "world.dungeon02", tuples)
    assert len(built) == len(spawns) > 0
    for _eid, packet in built:
        assert packet[0] == 0x07 and packet[-1] == 0x06
        # OP1 create carries the gc type as a NUL-terminated string after the
        # 0x01 op + u16 id + 0xFF tag; it must name a world.dungeon02 mob asset.
        body = packet.decode("latin-1")
        assert "world.dungeon02.mob." in body.lower()


def test_multiple_dungeons_resolve_to_their_own_mobs():
    """A dungeon's mobs must come from its OWN namespace — proves the map is
    per-dungeon, not a single hardcoded family set."""
    for zone, ns in (("dungeon00_level01", "world.dungeon00.mob."),
                     ("dungeon02_level03", "world.dungeon02.mob.")):
        spawns = ds.generate_spawns(zone)
        assert spawns, zone
        assert all(s.gc_type.lower().startswith(ns) for s in spawns), zone


def test_importer_resolves_mob_to_creature_chain():
    """Unit-test the importer's mob→creature resolution on real client content
    (gated on the extracter being present)."""
    extracter = os.path.normpath(
        os.path.join(_paths.REPO_ROOT, "..", "extracter"))
    if not os.path.isdir(extracter):
        import pytest
        pytest.skip("extracter content not present")
    from drserver.data.dungeon_world_importer import build_mob_creature_map
    mob_map = build_mob_creature_map(extracter)
    # dungeon02 upper RaceA base1 -> a concrete creature (per the .enc comment,
    # creatures.mutants.pincer.*). We only assert it resolves to a creature.
    key = "world.dungeon02.mob.upper.racea.base1"
    assert key in mob_map
    assert mob_map[key].lower().startswith("creatures.")


def test_importer_parses_world_room_nodes():
    """The room-node parser reproduces the ``.world`` entry/link/encounter nodes
    in file order (gated on the extracter being present)."""
    extracter = os.path.normpath(
        os.path.join(_paths.REPO_ROOT, "..", "extracter"))
    fp = os.path.join(extracter, "dungeon00_level01.world")
    if not os.path.isfile(fp):
        import pytest
        pytest.skip("extracter content not present")
    from drserver.data.dungeon_world_importer import parse_room_nodes
    nodes = parse_room_nodes(extracter, fp, "dungeon00_level01")

    assert [n.source_index for n in nodes] == [0, 1, 2, 3]

    entry = nodes[0]
    assert entry.node_kind == "startroom"
    assert entry.tile_set == "elmforest_hub_"
    assert entry.grid_y == 4 and entry.grid_x is None
    assert entry.link_to_zone == "tutorial" and entry.spawn_name == "start1"

    warp = nodes[1]
    assert warp.node_kind == "linkroomnode"
    assert warp.link_to_zone == "dungeon00_level02"
    assert warp.grid_y == 0

    leader = nodes[2]
    assert leader.node_kind == "roomnode"
    assert leader.encounter_type == "world.dungeon00.enc.level01_leader_encounter"
    assert leader.link_to_zone == ""


def test_room_nodes_table_populated_for_dungeon_levels():
    """The shipped DB carries room nodes for every maze level (end-to-end)."""
    rows = game_database.execute_reader(
        "SELECT zone_name, source_index, tile_set, link_to_zone "
        "FROM dungeon_room_nodes WHERE zone_name = :z ORDER BY source_index",
        {"z": "dungeon00_level01"},
    ).fetchall()
    assert len(rows) == 4
    assert game_database.get_string(rows[0], "tile_set") == "elmforest_hub_"
    assert game_database.get_string(rows[1], "link_to_zone") == "dungeon00_level02"


# ── Static (non-maze) worlds: boss arenas + elite01 coverage (2026-06-10) ──

def test_elite01_intro_is_imported_maze_level():
    """elite01_intro is a real 7x7 cat-tileset maze the old ``dungeon*_level*``
    filename filter dropped — it must be a procedural zone now."""
    level = ds._load_level("elite01_intro")
    assert level is not None
    assert level.tile_prefix == "cat_tileset_"
    assert level.maze_width == 7 and level.maze_height == 7
    assert ds.is_procedural_zone("elite01_intro")


def test_elite01_intro_spawns_resolve_creatures():
    """elite01 encounter units name raw ``creatures.*`` directly (client
    content); every spawn must carry a resolvable creature for the HP synch."""
    spawns = ds.generate_spawns("elite01_intro")
    assert spawns
    for s in spawns:
        low = s.gc_type.lower()
        assert low.startswith(("world.", "creatures.")), s.gc_type
        assert s.creature_gc_type.startswith("creatures."), s.gc_type


def test_elite01_intro_has_exit_warp_gates():
    """The intro's linkroomnodes (amazon_dungeon + town) become warp gates."""
    gates = ds.warp_gates("elite01_intro")
    targets = {g.link_to_zone for g in gates}
    assert "town" in targets and "amazon_dungeon" in targets


def test_resolve_monster_entity_passes_creatures_namespace():
    """Raw ``creatures.*`` unit types are real, client-loadable content (the
    C# reference spawns them verbatim) — they must not be dropped."""
    assert (m.resolve_monster_entity_gc_type(
        "elite01_intro", "creatures.fade.lichLord.Fire.Hero")
        == "creatures.fade.lichLord.Fire.Hero")
    assert m.resolve_monster_entity_gc_type(
        "dungeon00_level01", "world.dungeon00.mob.melee01.rank1") is not None
    assert m.resolve_monster_entity_gc_type(
        "dungeon00_level01", "items.pal.whatever") is None


def test_boss_room_is_static_world_with_authored_marker():
    """dungeon01_level08_boss is a hand-authored (non-maze) world whose
    ``base.Encounter`` marker + master encounter table were imported."""
    assert not ds.is_procedural_zone("dungeon01_level08_boss")
    assert ds.is_static_world_zone("dungeon01_level08_boss")
    markers = ds._load_static_markers("dungeon01_level08_boss")
    assert len(markers) == 1
    mk = markers[0]
    assert (mk.pos_x, mk.pos_y) == (-170.0, 100.0)
    assert mk.size_x == 150.0 and mk.size_y == 200.0


def test_boss_room_static_spawns_include_boss():
    """The boss room spawns its master encounter at the authored marker — the
    boss mob itself plus its guards, all with resolved creatures, inside the
    authored encounter area at the authored floor Z."""
    spawns = ds.generate_static_spawns("dungeon01_level08_boss")
    assert spawns
    types = {s.gc_type.lower() for s in spawns}
    assert "world.dungeon01.mob.boss" in types
    assert "world.dungeon01.mob.boss_guard" in types
    # ★2026-06-21 (bible §14.4): a boss now spawns AS its own named entity
    # (self-mapped + imported into ``creatures`` so its override Difficulty wins),
    # so its creature == its entity; guards/trash still resolve to a concrete
    # ``creatures.*``. Either is a valid spawnable creature.
    for s in spawns:
        assert (s.creature_gc_type.startswith("creatures.")
                or s.creature_gc_type == s.gc_type), s.gc_type
        # Within the authored area around the marker (-170,100), Z authored.
        assert abs(s.pos_x - (-170.0)) <= 75.0
        assert abs(s.pos_y - 100.0) <= 100.0
        assert abs(s.pos_z - 39.9961) < 0.01


def test_static_spawns_deterministic_per_zone():
    spawns_a = ds.generate_static_spawns("dungeon01_level08_boss")
    spawns_b = ds.generate_static_spawns("dungeon01_level08_boss")
    assert spawns_a == spawns_b


def test_dungeon00_boss_arena_repopulated():
    """The dungeon00 boss arena (static rows were dropped 2026-06-09 leaving it
    empty) repopulates from its 12 authored markers — plus the named boss posse
    placements when the rebuilt DB has them. Every spawn is a client-loadable
    asset (a world.* mob, or a raw creatures.* for posse members)."""
    spawns = ds.generate_static_spawns("dungeon00_level03_boss")
    assert len(spawns) >= 12
    for s in spawns:
        low = s.gc_type.lower()
        assert low.startswith("world.dungeon00.mob.") or low.startswith("creatures."), \
            s.gc_type


def test_dungeon00_boss_posse_spawns_when_imported():
    """RattleTooth + posse spawn at their authored positions — gated on the DB
    having the static_world_placements table (rebuild applied). The boss is
    world.dungeon00.mob.boss (the visual/size-bearing asset) at (405,-1195)."""
    placements = ds._load_static_placements("dungeon00_level03_boss")
    if not placements:
        import pytest
        pytest.skip("static_world_placements not in DB (importer not yet rerun)")
    spawns = ds.generate_static_spawns("dungeon00_level03_boss")
    boss = [s for s in spawns if s.gc_type == "world.dungeon00.mob.boss"]
    assert len(boss) == 1
    assert (round(boss[0].pos_x), round(boss[0].pos_y)) == (405, -1195)
    assert boss[0].creature_gc_type == "creatures.whiskers.broodling.basic.champion"
    # The full 7-member posse (boss + 2 blademaster + 2 broodling + 2 warg).
    posse = [s for s in spawns if abs(s.pos_y) > 1100]
    assert len(posse) == 7


def test_static_world_parser_reads_boss_world():
    """Unit-test the static-world parser on the real client content (gated on
    the extracter being present)."""
    extracter = os.path.normpath(
        os.path.join(_paths.REPO_ROOT, "..", "extracter"))
    fp = os.path.join(extracter, "dungeon01_level08_boss.world")
    if not os.path.isfile(fp):
        import pytest
        pytest.skip("extracter content not present")
    from drserver.data.dungeon_world_importer import parse_static_world
    parsed = parse_static_world(fp)
    assert parsed is not None
    world, markers, _placements = parsed
    assert world.zone_name == "dungeon01_level08_boss"
    assert world.encounter_table == "world.dungeon01.enc.level08_master_encounter"
    assert len(markers) == 1
    assert (markers[0].pos_x, markers[0].pos_y) == (-170.0, 100.0)
    # A maze world must NOT parse as static.
    maze_fp = os.path.join(extracter, "elite01_intro.world")
    assert parse_static_world(maze_fp) is None
