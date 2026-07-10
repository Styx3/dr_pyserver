"""Round-trip tests for the zone-portal (teleport gate) wire format.

Verifies build_portal_stream emits the exact byte layout the client expects
(port of C# SendZonePortals) and that the bytes parse back to the source fields.
"""
import pytest

import _paths
from drserver.db import game_database
from drserver.managers import portals as portals_module
from drserver.managers.portals import (
    PortalData,
    PortalManager,
    build_dungeon_warp_gates,
    build_portal_stream,
    portal_manager,
)
from drserver.util.byte_io import LEReader

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 5000

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


def _tutorial_portal() -> PortalData:
    # Mirrors the shipped DB row: tutorial gate -> dungeon00_level01 @ Start1.
    return PortalData(
        id=432,
        zone="tutorial",
        name="ZonePortal",
        gc_type="misc.ZonePortal_agg",
        pos_x=960.0,
        pos_y=719.0,
        pos_z=60.0,
        heading=0.0,
        width=60,
        height=60,
        target_zone="dungeon00_level01",
        spawn_point="Start1",
        color=0xFFFF0000,
    )


def test_portal_stream_round_trips_to_source_fields():
    # Arrange
    portal = _tutorial_portal()
    entity_id = 0x0205

    # Act
    packet = build_portal_stream(entity_id, portal)
    r = LEReader(packet)

    # Assert — exact field order from C# SendZonePortals.
    assert r.read_byte() == 0x07                       # BeginStream
    assert r.read_byte() == 0x01                       # create entity
    assert r.read_uint16() == entity_id
    assert r.read_byte() == 0xFF                       # GCType tag
    assert r.read_cstring() == "misc.ZonePortal_agg"   # preserve_case
    assert r.read_byte() == 0x02                       # init entity
    assert r.read_uint16() == entity_id
    assert r.read_uint32() == 0x06                     # flags
    assert r.read_int32() == int(960.0 * 256)
    assert r.read_int32() == int(719.0 * 256)
    assert r.read_int32() == int(60.0 * 256)
    assert r.read_int32() == 0                          # heading * 256
    assert r.read_byte() == 0x07                        # initFlags
    assert r.read_uint16() == 0                         # parentID
    assert r.read_byte() == 0                           # unk2
    assert r.read_uint32() == 0                         # unk4
    assert r.read_cstring() == "Start1"                 # spawn point
    assert r.read_cstring() == "dungeon00_level01"      # target zone
    assert r.read_uint16() == 60                        # width
    assert r.read_uint16() == 60                        # height
    assert r.read_uint32() == 0xFFFF0000                # color
    assert r.read_byte() == 0x06                        # EndStream
    assert r.remaining == 0


def test_position_is_fixed_point_256():
    # Arrange
    portal = _tutorial_portal()

    # Act
    packet = build_portal_stream(0x0205, portal)
    r = LEReader(packet)
    # skip to the init position block
    r.read_byte()                  # BeginStream
    r.read_byte(); r.read_uint16()  # create + id
    r.read_byte(); r.read_cstring()  # gctype
    r.read_byte(); r.read_uint16()  # init + id
    r.read_uint32()                 # flags

    # Assert — drfloat: wire value is world units * 256.
    assert r.read_int32() / 256.0 == pytest.approx(960.0)
    assert r.read_int32() / 256.0 == pytest.approx(719.0)


def test_register_and_resolve_entity():
    # Arrange
    mgr = PortalManager()
    portal = _tutorial_portal()

    # Act
    mgr.register_entity(0x0205, portal)

    # Assert
    assert mgr.find_by_entity_id(0x0205) is portal
    assert mgr.find_by_entity_id(0x9999) is None


def test_build_dungeon_warp_gates_from_room_nodes():
    # Arrange — a procedural level whose maze places several linked room nodes.
    server = _FakeServer()

    # Act — gates are data-driven from dungeon_room_nodes (no static portal rows).
    built = build_dungeon_warp_gates(server, "dungeon11_level05")

    # Assert — one create stream per placed link, each a framed portal whose
    # activation resolves to the linked zone (the existing HandlePortalActivation
    # path reads portal_manager.find_by_entity_id).
    from drserver.managers import dungeon_spawner
    gates = dungeon_spawner.warp_gates("dungeon11_level05")
    assert len(built) == len(gates) >= 2
    for entity_id, packet in built:
        assert packet[0] == 0x07 and packet[-1] == 0x06
        portal = portal_manager.find_by_entity_id(entity_id)
        assert portal is not None
        assert portal.target_zone   # links somewhere
        assert portal.gc_type == "misc.ZonePortal_agg"

    # A non-maze zone has no maze-derived gates.
    assert build_dungeon_warp_gates(server, "town") == []
