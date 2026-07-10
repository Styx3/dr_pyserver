"""Round-trip tests for the waystone / checkpoint (obelisk) wire format + lookup.

Verifies build_checkpoint_stream emits the exact byte layout the client expects
(port of C# SendZoneCheckpoints) and that destination resolution matches the C#
rules (id = "world.checkpoints.<Name w/o spaces>"; the physical entity's
"…Entity" suffix is stripped before matching).
"""
import pytest

from drserver.managers.checkpoints import (
    CheckpointDestination,
    CheckpointEntity,
    CheckpointManager,
    build_checkpoint_stream,
)
from drserver.util.byte_io import LEReader


def _town_obelisk() -> CheckpointEntity:
    # Mirrors the shipped zone_checkpoints row for town.
    return CheckpointEntity(
        zone="town",
        name="CheckpointEntity",
        gc_type="world.checkpoints.TownCheckpointEntity",
        pos_x=420.0,
        pos_y=-170.0,
        pos_z=50.0,
        heading=0.0,
    )


def _town_destination() -> CheckpointDestination:
    return CheckpointDestination(
        id="world.checkpoints.TownCheckpoint",
        name="Town Checkpoint",
        zone="Town",
        spawn_point="Waypoint",
        pos_x=420.0,
        pos_y=-170.0,
        pos_z=50.0,
        order=1,
        is_active=True,
        level_requirement=1,
        unlock_quest="",
    )


def test_checkpoint_stream_round_trips_to_source_fields():
    # Arrange
    entity = _town_obelisk()
    entity_id = 0x0301

    # Act
    packet = build_checkpoint_stream(entity_id, entity)
    r = LEReader(packet)

    # Assert — exact field order from C# SendZoneCheckpoints.
    assert r.read_byte() == 0x07                                   # BeginStream
    assert r.read_byte() == 0x01                                   # create entity
    assert r.read_uint16() == entity_id
    assert r.read_byte() == 0xFF                                   # GCType tag
    assert r.read_cstring() == "world.checkpoints.TownCheckpointEntity"
    assert r.read_byte() == 0x02                                   # init entity
    assert r.read_uint16() == entity_id
    assert r.read_uint32() == 0x06                                 # flags (visible|activatable)
    assert r.read_int32() == int(420.0 * 256)
    assert r.read_int32() == int(-170.0 * 256)
    assert r.read_int32() == int(50.0 * 256)
    assert r.read_int32() == 0                                     # heading * 256
    assert r.read_byte() == 0x00                                   # initFlags (no parent/cstrings)
    assert r.read_byte() == 0x06                                   # EndStream
    assert r.remaining == 0


def test_position_is_fixed_point_256():
    # Arrange
    entity = _town_obelisk()

    # Act
    packet = build_checkpoint_stream(0x0301, entity)
    r = LEReader(packet)
    r.read_byte()                       # BeginStream
    r.read_byte(); r.read_uint16()       # create + id
    r.read_byte(); r.read_cstring()      # gctype
    r.read_byte(); r.read_uint16()       # init + id
    r.read_uint32()                      # flags

    # Assert — drfloat: wire value is world units * 256.
    assert r.read_int32() / 256.0 == pytest.approx(420.0)
    assert r.read_int32() / 256.0 == pytest.approx(-170.0)


def test_register_and_resolve_entity():
    # Arrange
    mgr = CheckpointManager()
    mgr._loaded = True   # skip DB load for the registry-only test
    entity = _town_obelisk()

    # Act
    mgr.register_entity(0x0301, entity)

    # Assert
    assert mgr.find_by_entity_id(0x0301) is entity
    assert mgr.find_by_entity_id(0x9999) is None


def test_find_destination_strips_entity_suffix():
    # Arrange — a manager primed with one destination, no DB.
    mgr = CheckpointManager()
    dest = _town_destination()
    mgr._destinations = [dest]
    mgr._dest_by_id = {dest.id.lower(): dest}
    mgr._loaded = True

    # Act / Assert — both the bare id and the physical "…Entity" gc_type resolve.
    assert mgr.find_destination("world.checkpoints.TownCheckpoint") is dest
    assert mgr.find_destination("world.checkpoints.TownCheckpointEntity") is dest
    assert mgr.find_destination("WORLD.CHECKPOINTS.TOWNCHECKPOINT") is dest   # case-insensitive
    assert mgr.find_destination("world.checkpoints.Nope") is None
    assert mgr.find_destination("") is None


def _fake_conn(unlocked):
    """Minimal stand-in for RRConnection covering the QM-component fields."""
    from types import SimpleNamespace
    return SimpleNamespace(
        unlocked_checkpoints=set(unlocked),
        has_saved_town_portal=False,
        town_portal_zone_name="",
        town_portal_zone_id=0,
        zone_portal_source="",
    )


def test_quest_manager_component_serializes_actual_unlocked_checkpoints():
    """The obelisk recall menu is built from the checkpoint list in the
    QuestManager component, so it must reflect the character's real unlocked
    set — not a fixed default. Regression for the hardcoded-defaults bug."""
    from drserver.managers.checkpoints import checkpoint_manager
    from drserver.net import spawn
    from drserver.util.byte_io import LEWriter

    checkpoint_manager._loaded = True   # skip DB; unresolved ids fall back to tail order
    unlocked = {
        "world.checkpoints.TownCheckpoint",
        "world.checkpoints.Dungeon05Checkpoint",   # unlocked later, must appear
    }
    conn = _fake_conn(unlocked)

    w = LEWriter()
    spawn._write_quest_manager(w, conn, player_id=0x0101, qm_id=0x0202)
    body = w.to_array()

    # Both unlocked destinations are present; a non-unlocked one is not.
    assert b"world.checkpoints.TownCheckpoint" in body
    assert b"world.checkpoints.Dungeon05Checkpoint" in body
    assert b"world.checkpoints.Dungeon16Checkpoint" not in body


def test_quest_manager_component_falls_back_to_defaults_when_none_unlocked():
    from drserver.managers.checkpoints import checkpoint_manager
    from drserver.net import spawn
    from drserver.util.byte_io import LEWriter

    checkpoint_manager._loaded = True
    conn = _fake_conn(set())

    w = LEWriter()
    spawn._write_quest_manager(w, conn, player_id=0x0101, qm_id=0x0202)
    body = w.to_array()

    assert b"world.checkpoints.TownCheckpoint" in body
    assert b"world.checkpoints.TutorialCheckpoint" in body


def _recall_conn(unlocked):
    from types import SimpleNamespace
    sysmsgs = []
    return SimpleNamespace(
        login_name="tester",
        quest_manager_id=0x0218,
        equipment_component_id=0x9001,
        unit_container_id=0x9002,
        unlocked_checkpoints=set(unlocked),
        send_system_message=lambda m: sysmsgs.append(m),
        _sysmsgs=sysmsgs,
    )


def test_obelisk_menu_recall_resolves_djb2_hash_and_changes_zone():
    """The live obelisk dialog sends recall as a QM-component sub-message 0x07
    with a DJB2 hash of the checkpoint id (ch7/0x34). The server must resolve
    the hash against the unlocked set and transfer. Regression for the dead
    'click Town -> nothing' menu select."""
    from types import SimpleNamespace
    from drserver.data.gc_object import hash_djb2
    from drserver.managers.checkpoints import checkpoint_manager
    from drserver.net import movement
    from drserver.util.byte_io import LEReader

    # Arrange — prime the destination registry without a DB hit.
    dest = _town_destination()
    checkpoint_manager._dest_by_id = {dest.id.lower(): dest}
    checkpoint_manager._loaded = True

    conn = _recall_conn({"world.checkpoints.TownCheckpoint"})
    captured = {}
    server = SimpleNamespace(
        change_zone=lambda c, zone: captured.setdefault("zone", zone),
    )

    # Body after the ch7/0x34 message_type: [componentId u16][0x07][0x04][u32 hash]
    cp_hash = hash_djb2("world.checkpoints.TownCheckpoint")   # 0x921756F4
    body = (conn.quest_manager_id.to_bytes(2, "little")
            + bytes([0x07, 0x04]) + cp_hash.to_bytes(4, "little"))

    # Act
    handled = movement._component_update(server, conn, LEReader(body))

    # Assert
    assert handled is True
    assert captured.get("zone") == "Town"
    assert conn._sysmsgs == []        # no error surfaced


def test_obelisk_menu_recall_rejects_unknown_hash():
    from types import SimpleNamespace
    from drserver.managers.checkpoints import checkpoint_manager
    from drserver.net import movement
    from drserver.util.byte_io import LEReader

    checkpoint_manager._dest_by_id = {}
    checkpoint_manager._loaded = True
    conn = _recall_conn(set())
    captured = {}
    server = SimpleNamespace(change_zone=lambda c, zone: captured.setdefault("zone", zone))

    body = conn.quest_manager_id.to_bytes(2, "little") + bytes([0x07, 0x04, 0xEF, 0xBE, 0xAD, 0xDE])
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert "zone" not in captured              # no teleport
    assert conn._sysmsgs                        # player told it isn't unlocked
