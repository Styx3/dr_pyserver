"""Town-entity packet tests — guards the @z town "Zone communication error.
Code 3" fix.

Code 3 = client ``processMessage`` read an unrecognized opcode = a one-byte
stream desync inside one town-only entity packet. NPCs were already byte-exact;
the three *town-only* builders that tutorial never exercises are the suspects:

* ``build_world_entity_stream``  vs C# ``WorldEntitySpawner.WriteEntitySpawn``
* ``build_portal_stream``        vs C# ``SendZonePortals``
* ``build_checkpoint_stream``    vs C# ``SendZoneCheckpoints``

The world-entity builder was the bug: its old hand-rolled init blob did not
match the client's WorldEntity/NCI/Behaviour readInit, so the stream desynced.
Each test fully parses the stream and asserts it ends exactly on EndStream with
no trailing bytes — i.e. the client's reader would consume it cleanly.
"""
import _paths
from drserver.db import game_database
from drserver.managers.world_entities import (
    WorldEntityData,
    build_world_entity_stream,
    build_zone_world_entities,
    world_entity_manager,
)
from drserver.managers.portals import PortalData, build_portal_stream
from drserver.managers.checkpoints import CheckpointEntity, build_checkpoint_stream
from drserver.util.byte_io import LEReader

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 100

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


def _read_world_entity_init(r: LEReader, is_gate: bool) -> None:
    """Parse one WriteEntitySpawn body after BeginStream, asserting the exact
    C# layout. Raises (via assert / struct under-read) on any desync."""
    assert r.read_byte() == 0x01                 # create
    entity_id = r.read_uint16()
    assert r.read_byte() == 0xFF                 # GCType tag
    r.read_cstring()

    assert r.read_byte() == 0x02                 # init
    assert r.read_uint16() == entity_id
    r.read_uint32()                              # flags
    r.read_int32(); r.read_int32(); r.read_int32()  # pos
    r.read_int32()                               # heading
    assert r.read_byte() == 0x00                 # initFlags

    if is_gate:
        assert r.read_byte() == 0x00             # door state
        assert r.read_byte() == 0x00             # door flags
        return

    # intermediate parent::readInit (6 bytes)
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x00
    assert r.read_uint16() == 0
    assert r.read_uint16() == 0
    # NCI::readInit (4 bytes)
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x00
    assert r.read_uint16() == 0
    # 0x32 behavior child
    assert r.read_byte() == 0x32
    assert r.read_uint16() == entity_id
    r.read_uint16()                              # behavior id
    assert r.read_byte() == 0xFF                 # behavior GCType tag
    assert r.read_cstring() == "base.noncombatinteractive.behavior"
    assert r.read_byte() == 0x01                 # flag byte
    # Behavior::readInit (4 bytes)
    assert r.read_byte() == 0xFF
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x01
    # UnitMover::readInit (10 bytes)
    assert r.read_byte() == 0x08
    r.read_int32(); r.read_int32()
    assert r.read_byte() == 0x00
    # UnitBehavior::readInit own (3 bytes)
    assert r.read_byte() == 0xFF
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x00


def test_world_entity_teleporter_stream_is_clean():
    # Arrange — the actual town teleporter row (entity_type "teleporter", flags 6).
    we = WorldEntityData(
        id=62, zone_name="town", name="ToPwnston",
        gc_type="world.town.data.teleport.to_pvp_land", entity_type="teleporter",
        pos_x=724.0, pos_y=-400.0, pos_z=50.0, heading=65.0, floor_index=0,
        item_generator="", item_count=0, target_zone="pvp_start",
        target_waypoint="Waypoint", display_label="Go to Pwnston", flags=6,
    )

    # Act
    packet = build_world_entity_stream(0x0101, 0x0102, we)

    # Assert — frames cleanly with nothing left over (no desync).
    r = LEReader(packet)
    assert r.read_byte() == 0x07                 # BeginStream
    _read_world_entity_init(r, is_gate=False)
    assert r.read_byte() == 0x06                 # EndStream
    assert r.remaining == 0


def test_world_entity_gate_uses_door_init():
    # Arrange
    we = WorldEntityData(
        id=1, zone_name="town", name="Door", gc_type="misc.somegate",
        entity_type="gate", pos_x=10.0, pos_y=20.0, pos_z=0.0, heading=0.0,
        floor_index=0, item_generator="", item_count=0, target_zone="",
        target_waypoint="", display_label="", flags=7,
    )

    # Act
    packet = build_world_entity_stream(5, 6, we)

    # Assert — gate path is the short 2-byte Door init, no behavior child.
    r = LEReader(packet)
    assert r.read_byte() == 0x07
    _read_world_entity_init(r, is_gate=True)
    assert r.read_byte() == 0x06
    assert r.remaining == 0
    assert 0x32 not in packet                    # no behavior child for doors


def test_portal_stream_is_clean():
    # Arrange — the town DungeonPortal row.
    p = PortalData(
        id=430, zone="town", name="DungeonPortal", gc_type="misc.ZonePortal_agg",
        pos_x=-30.0, pos_y=-692.0, pos_z=70.0, heading=-10.0, width=70, height=60,
        target_zone="dungeon01_level01", spawn_point="dungeon_spawn",
        color=4294901760,
    )

    # Act
    packet = build_portal_stream(0x0200, p)

    # Assert
    r = LEReader(packet)
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x01
    assert r.read_uint16() == 0x0200
    assert r.read_byte() == 0xFF
    r.read_cstring()
    assert r.read_byte() == 0x02
    assert r.read_uint16() == 0x0200
    assert r.read_uint32() == 0x06               # flags
    r.read_int32(); r.read_int32(); r.read_int32(); r.read_int32()
    assert r.read_byte() == 0x07                 # initFlags hasParent|unk2|unk4
    assert r.read_uint16() == 0                  # parentID
    assert r.read_byte() == 0                    # Unk2
    assert r.read_uint32() == 0                  # Unk4
    assert r.read_cstring() == "dungeon_spawn"
    assert r.read_cstring() == "dungeon01_level01"
    assert r.read_uint16() == 70
    assert r.read_uint16() == 60
    assert r.read_uint32() == 4294901760
    assert r.read_byte() == 0x06                 # EndStream
    assert r.remaining == 0


def test_checkpoint_stream_is_clean():
    # Arrange — the town obelisk row.
    e = CheckpointEntity(
        zone="town", name="CheckpointEntity",
        gc_type="world.checkpoints.TownCheckpointEntity",
        pos_x=420.0, pos_y=-170.0, pos_z=50.0, heading=0.0,
    )

    # Act
    packet = build_checkpoint_stream(0x0300, e)

    # Assert
    r = LEReader(packet)
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x01
    assert r.read_uint16() == 0x0300
    assert r.read_byte() == 0xFF
    r.read_cstring()
    assert r.read_byte() == 0x02
    assert r.read_uint16() == 0x0300
    assert r.read_uint32() == 0x06               # flags visible|activatable
    r.read_int32(); r.read_int32(); r.read_int32(); r.read_int32()
    assert r.read_byte() == 0x00                 # initFlags
    assert r.read_byte() == 0x06                 # EndStream
    assert r.remaining == 0


def test_town_world_entities_from_db_frame_cleanly():
    # Arrange
    server = _FakeServer()
    world_entity_manager.load()

    # Act — build the real town world entities straight from the shipped DB.
    built = build_zone_world_entities(server, "town")

    # Assert — every town entity stream parses to a clean EndStream.
    assert built, "expected at least the town teleporter"
    for _eid, packet in built:
        r = LEReader(packet)
        assert r.read_byte() == 0x07
        # entity_type unknown here; the teleporter is non-gate. Parse generically:
        # peek is_gate by trying non-gate first is fragile, so just confirm the
        # stream ends on EndStream with no leftover by walking known town data.
        _read_world_entity_init(r, is_gate=False)
        assert r.read_byte() == 0x06
        assert r.remaining == 0


if __name__ == "__main__":
    import sys
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
