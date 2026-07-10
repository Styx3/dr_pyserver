"""Adopt the client's avatar HP from the trailing EntitySynchInfo on routine
channel-7 packets (movement 0x65 / action acks), not just the standalone 0x36.

Live crash (2026-06-02, dungeon00_level01): the client self-sims damage and
reports its current HP as a trailing ``[flags=0x02][hp:u32]`` before the 0x06
EndStream on EVERY move packet (160 updates), but the server only parsed the
standalone 0x36 sub-message and kept shipping MaxHP in its 0x02 suffixes ->
``processUpdateComponent`` compared [Local 74701] vs [Remote 76288 MaxHP] ->
"Entity synch error" zone-disconnect. The fix scans the trailing synch info on
every inbound ch7 packet and adopts it (C# UnityGameServer.TryReadTrailing-
EntitySynchInfo / ObserveClientPlayerHP).
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers.combat import CombatManager
from drserver.net import movement
from drserver.util.byte_io import LEWriter


def _server():
    srv = types.SimpleNamespace(connections={}, combat=None, quests=None)
    srv.combat = CombatManager(srv)
    return srv


def _conn(avatar_id=510, hp_wire=76288):
    return types.SimpleNamespace(
        conn_id=1, login_name="Styx3", hp_wire=hp_wire,
        unit_behavior_id=533, session_id=0x10,
        avatar=types.SimpleNamespace(id=avatar_id),
        client_hp_wire=None,
        # Movement state exercised by the 0x65 move path (matches RRConnection).
        player_pos_x=0.0, player_pos_y=0.0, player_heading=0.0,
        current_zone_gc_type="world.town", instance_id=0, is_spawned=True,
        pending_local_move_session=0, pending_local_move_count=0,
        pending_local_move_data=b"", pending_local_move_flush_at=0.0,
        last_position_update_time=0.0, last_raw_move_data=b"",
        last_raw_move_count=0, stop_signal_sent=False,
    )


def _move_packet(ub_id=533, hp_wire=74701, *, with_hp=True, move_count=1):
    """Inner ch7-0x07 stream: 0x35 <ub> 0x65 <sid> <count> [moves] [synch] 0x06."""
    w = LEWriter()
    w.write_byte(0x35)
    w.write_uint16(ub_id)
    w.write_byte(0x65)              # UnitMoverUpdate
    w.write_byte(0x10)             # session id
    w.write_byte(move_count)
    for _ in range(move_count):
        w.write_byte(0x03)         # move type
        w.write_int32(0)           # heading
        w.write_int32(1000 * 256)  # x
        w.write_int32(2000 * 256)  # y
    if with_hp:
        w.write_byte(0x02)         # synch flags: HP present
        w.write_uint32(hp_wire)
    else:
        w.write_byte(0x00)         # no synch
    w.write_byte(0x06)             # EndStream
    return w.to_array()


# ── _read_trailing_avatar_hp (pure parse) ────────────────────────────────────

def test_trailing_scan_reads_hp_from_move_packet():
    data = _move_packet(hp_wire=74701)
    assert movement._read_trailing_avatar_hp(data) == 74701


def test_trailing_scan_tolerates_nul_padding():
    data = _move_packet(hp_wire=74701) + b"\x00\x00\x00"
    assert movement._read_trailing_avatar_hp(data) == 74701


def test_trailing_scan_returns_none_without_synch_bit():
    data = _move_packet(with_hp=False)
    assert movement._read_trailing_avatar_hp(data) is None


def test_trailing_scan_returns_none_when_too_short():
    assert movement._read_trailing_avatar_hp(b"\x06") is None
    assert movement._read_trailing_avatar_hp(b"") is None


def test_trailing_scan_returns_none_without_terminator():
    # No 0x06 terminator -> not a well-formed ch7 stream.
    assert movement._read_trailing_avatar_hp(b"\x35\x02\x01\x02\x03\x04") is None


# ── handle() end-to-end adoption ─────────────────────────────────────────────

def test_handle_adopts_trailing_hp_into_conn_hp_wire():
    # Arrange — server shipping MaxHP, client moves while damaged
    srv = _server()
    conn = _conn(hp_wire=76288)

    # Act — a routine move packet carrying the damaged HP trailer
    movement.handle(srv, conn, 0x07, _move_packet(ub_id=533, hp_wire=74701))

    # Assert — adopted, so the next 0x36 heartbeat carries the matching value
    assert conn.hp_wire == 74701


def test_handle_does_not_adopt_when_no_hp_trailer():
    srv = _server()
    conn = _conn(hp_wire=76288)

    movement.handle(srv, conn, 0x07, _move_packet(with_hp=False))

    assert conn.hp_wire == 76288


def test_handle_rejects_sentinel_trailer():
    srv = _server()
    conn = _conn(hp_wire=76288)

    movement.handle(srv, conn, 0x07, _move_packet(hp_wire=0xFFFF00))

    assert conn.hp_wire == 76288
