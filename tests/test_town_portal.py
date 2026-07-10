"""Tests for the waypoint-scroll / town-portal system (managers/town_portal.py).

Covers the TownPortalBlue wire layout (port of C# SpawnTownPortalWithRemoval),
the scroll-use side effects (portal registration + saved return point), the
one-trip return portal, and the obelisk saved-place handlers in movement.py.
"""
from types import SimpleNamespace

import pytest

from drserver.managers import town_portal
from drserver.managers.portals import portal_manager
from drserver.util.byte_io import LEReader


def _fake_server(connections=None):
    ids = iter(range(0x0500, 0x0600))
    return SimpleNamespace(
        allocate_entity_id=lambda: next(ids),
        get_player_avatar_id=lambda login: 0x0042,
        connections=connections if connections is not None else {},
    )


def _fake_conn(**overrides):
    sent = []
    conn = SimpleNamespace(
        conn_id=1,
        login_name="tester",
        char_sql_id=-1,
        quest_manager_id=0x0218,
        current_zone_name="dungeon01_level01",
        current_zone_gc_type="world.dungeon01.level01",
        current_zone_id=77,
        instance_id=0,
        is_spawned=True,
        player_pos_x=100.0,
        player_pos_y=200.0,
        player_pos_z=50.0,
        player_heading=0.0,
        hp_wire=68096,
        client_hp_wire=None,
        has_saved_town_portal=False,
        town_portal_zone_name="",
        town_portal_target_zone="",
        town_portal_zone_id=0,
        town_portal_pos_x=0.0,
        town_portal_pos_y=0.0,
        town_portal_pos_z=0.0,
        zone_portal_source="",
        send_to_client=lambda data: sent.append(bytes(data)),
        _sent=sent,
    )
    for key, value in overrides.items():
        setattr(conn, key, value)
    return conn


# ── Scroll classification ────────────────────────────────────────────────────

def test_waypoint_scroll_gc_classes_are_recognised():
    assert town_portal.is_waypoint_scroll("items.consumables.consumable_townportal")
    assert town_portal.is_waypoint_scroll("scrollpal.permawaypointscroll")
    assert not town_portal.is_waypoint_scroll("items.consumables.minorhealthpotion")


def test_perma_waypoint_scroll_is_not_consumed():
    assert town_portal.is_consumed_on_use("items.consumables.consumable_townportal")
    assert not town_portal.is_consumed_on_use("scrollpal.permawaypointscroll")


# ── Wire layout (C# SpawnTownPortalWithRemoval packet 3) ─────────────────────

def test_town_portal_stream_round_trips_to_source_fields():
    # Arrange
    entity_id = 0x0501
    avatar_id = 0x0042

    # Act
    packet = town_portal.build_town_portal_stream(
        entity_id, avatar_id, (110.0, 200.0, 50.0), "town", 77,
        flags=0x06, state=0x02)
    r = LEReader(packet)

    # Assert — exact field order from the C# reference.
    assert r.read_byte() == 0x07                                   # BeginStream
    assert r.read_byte() == 0x01                                   # create entity
    assert r.read_uint16() == entity_id
    assert r.read_byte() == 0xFF                                   # GCType tag
    assert r.read_cstring() == "items.townportal.TownPortalBlue"
    assert r.read_byte() == 0x02                                   # init entity
    assert r.read_uint16() == entity_id
    assert r.read_uint32() == 0x06                                 # clickable flags
    assert r.read_int32() == int(110.0 * 256)
    assert r.read_int32() == int(200.0 * 256)
    assert r.read_int32() == int(50.0 * 256)
    assert r.read_int32() == 0                                     # heading
    assert r.read_byte() == 0x01                                   # initFlags: hasParent
    assert r.read_uint16() == avatar_id                            # parent avatar
    assert r.read_cstring() == "town"                              # target zone
    assert r.read_cstring() == ""
    assert r.read_byte() == 0x02                                   # state: active
    assert r.read_uint32() == 0x00
    assert r.read_uint32() == 77                                   # zone GUID
    assert r.read_byte() == 0x06                                   # EndStream
    assert r.remaining == 0


def test_viewer_stream_is_visual_only():
    packet = town_portal.build_town_portal_stream(
        0x0501, 0x0042, (0.0, 0.0, 0.0), "town", 1, flags=0x04, state=0x01)
    r = LEReader(packet)
    r.read_byte(); r.read_byte(); r.read_uint16()
    r.read_byte(); r.read_cstring()
    r.read_byte(); r.read_uint16()
    assert r.read_uint32() == 0x04                # visible, NOT activatable


# ── Scroll use ───────────────────────────────────────────────────────────────

def test_use_waypoint_scroll_registers_portal_and_saves_return_point(monkeypatch):
    # Arrange
    monkeypatch.setattr(town_portal, "persist_tp_state", lambda conn: None)
    server = _fake_server()
    conn = _fake_conn()

    # Act
    town_portal.use_waypoint_scroll(server, conn)

    # Assert — saved place points at the cast zone.
    assert conn.has_saved_town_portal is True
    assert conn.town_portal_zone_name == "dungeon01_level01"
    assert conn.town_portal_target_zone == "town"
    assert conn.town_portal_zone_id == 77
    # heading 0 -> portal spawns +10 on Y in front of the player.
    assert conn.town_portal_pos_x == pytest.approx(100.0)
    assert conn.town_portal_pos_y == pytest.approx(210.0)

    # The portal entity is registered so activating it transfers the player.
    registered = [p for p in portal_manager._entity_to_portal.values()
                  if p.name == town_portal.TOWN_PORTAL_NAME
                  and p.zone == "dungeon01_level01"]
    assert registered and registered[-1].target_zone == "town"

    # Two packets to the owner: the QM 0x0A state update + the portal spawn.
    assert len(conn._sent) == 2
    qm_packet, spawn_packet = conn._sent
    assert qm_packet[1] == 0x35 and qm_packet[4] == 0x0A
    assert b"items.townportal.TownPortalBlue" in spawn_packet


def test_qm_town_portal_state_trailer_is_flags_only(monkeypatch):
    """The QM 0x0A "saved place" update is a player-OBJECT ComponentUpdate, so its
    synch trailer MUST be flags-only (0x00) — an HP-bearing trailer threw the live
    "Oops! You've encountered a sync error" (code 1) on waypoint-scroll use in a
    dungeon (x64dbg 2026-07-01). Same rule as quest_wire (bible §4)."""
    # Arrange
    monkeypatch.setattr(town_portal, "persist_tp_state", lambda conn: None)
    conn = _fake_conn()
    server = _fake_server()

    # Act
    town_portal.use_waypoint_scroll(server, conn)

    # Assert — decode the QM 0x0A packet down to its synch trailer.
    qm_packet = conn._sent[0]
    r = LEReader(qm_packet)
    assert r.read_byte() == 0x07                       # BeginStream
    assert r.read_byte() == 0x35                       # ComponentUpdate
    assert r.read_uint16() == conn.quest_manager_id    # QM (player-object) component
    assert r.read_byte() == 0x0A                        # town-portal-state sub-msg
    assert r.read_byte() == 0x01                        # has-saved flag
    r.read_uint32()                                     # zone id
    r.read_cstring()                                    # zone name
    r.read_cstring()                                    # ""
    assert r.read_byte() == 0x00                        # flags-only synch — NO HP
    assert r.read_byte() == 0x06                        # EndStream
    # The avatar HP must never ride this player-object component update.
    assert conn.hp_wire.to_bytes(4, "little") not in qm_packet


def test_use_waypoint_scroll_broadcasts_visual_only_to_instance_peers(monkeypatch):
    # Arrange
    monkeypatch.setattr(town_portal, "persist_tp_state", lambda conn: None)
    peer = _fake_conn(login_name="peer", conn_id=2)
    stranger = _fake_conn(login_name="stranger", conn_id=3,
                          current_zone_gc_type="world.town")
    conn = _fake_conn()
    server = _fake_server(connections={1: conn, 2: peer, 3: stranger})

    # Act
    town_portal.use_waypoint_scroll(server, conn)

    # Assert — same-instance peer sees the portal, other-zone player does not.
    assert len(peer._sent) == 1
    assert b"items.townportal.TownPortalBlue" in peer._sent[0]
    assert stranger._sent == []


# ── Return portal (one trip per scroll) ──────────────────────────────────────

def test_return_portal_spawns_at_home_zone_and_clears_state(monkeypatch):
    # Arrange
    monkeypatch.setattr(town_portal, "persist_tp_state", lambda conn: None)
    server = _fake_server()
    conn = _fake_conn(
        has_saved_town_portal=True,
        town_portal_zone_name="dungeon01_level01",
        town_portal_target_zone="town",
        town_portal_zone_id=77,
        town_portal_pos_x=110.0,
        town_portal_pos_y=210.0,
        town_portal_pos_z=50.0,
    )

    # Act
    town_portal.spawn_return_portal_if_home(server, conn)

    # Assert
    assert len(conn._sent) == 1
    assert b"items.townportal.TownPortalBlue" in conn._sent[0]
    assert conn.has_saved_town_portal is False        # one return per scroll
    assert conn.town_portal_zone_name == ""


def test_return_portal_does_not_spawn_in_other_zones(monkeypatch):
    monkeypatch.setattr(town_portal, "persist_tp_state", lambda conn: None)
    server = _fake_server()
    conn = _fake_conn(
        has_saved_town_portal=True,
        town_portal_zone_name="dungeon02_level00",    # saved elsewhere
    )

    town_portal.spawn_return_portal_if_home(server, conn)

    assert conn._sent == []
    assert conn.has_saved_town_portal is True


def test_return_portal_noop_without_saved_state():
    server = _fake_server()
    conn = _fake_conn()

    town_portal.spawn_return_portal_if_home(server, conn)

    assert conn._sent == []


# ── Saved-place handlers (movement.py QM component updates) ──────────────────

def _movement_conn(**overrides):
    conn = _fake_conn(equipment_component_id=0x9001, unit_container_id=0x9002)
    sysmsgs = []
    conn.send_system_message = lambda m: sysmsgs.append(m)
    conn._sysmsgs = sysmsgs
    for key, value in overrides.items():
        setattr(conn, key, value)
    return conn


def test_qm_0x0c_returns_to_recent_zone_portal_source():
    """The obelisk menu's "Recent Zone Portal" entry sends QM sub-message 0x0C
    (C# UGS:13068) — teleport back to the zone the player last walked into a
    portal from."""
    from drserver.net import movement

    conn = _movement_conn(zone_portal_source="town")
    captured = {}
    server = SimpleNamespace(
        change_zone=lambda c, zone: captured.setdefault("zone", zone))

    body = conn.quest_manager_id.to_bytes(2, "little") + bytes([0x0C])
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert captured.get("zone") == "town"


def test_qm_0x0c_without_portal_source_does_not_teleport():
    from drserver.net import movement

    conn = _movement_conn(zone_portal_source="")
    captured = {}
    server = SimpleNamespace(
        change_zone=lambda c, zone: captured.setdefault("zone", zone))

    body = conn.quest_manager_id.to_bytes(2, "little") + bytes([0x0C])
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert "zone" not in captured
