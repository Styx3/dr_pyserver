"""Tick-packet + movement-relay regression tests.

Guards the faithful port of C# ``SendTickUpdates`` / ``BroadcastPlayerMovement`` /
``QueueLocalPlayerMovementAck``. The owner receives ONE packet every 4th tick
(~132 ms): a ``0x0D`` WorldInterval (the movement/world-pacing watchdog feed)
with the pending local move-ack piggybacked. The move-ack echoes the client's
VERBATIM raw move records + the same sessionId so the client dedupes against its
local prediction (no rubber-band) — the old code synthesized a single record from
the latest position at 30 Hz, which fought prediction and produced jitter.

There is NO standalone per-tick ``0x36`` HP heartbeat (C# sends none): avatar HP
rides the move-ack trailer (flags=0x02 + HP) and event acks. A bare flags=0x00
owner trailer was tried and crashed at warp (the client compares the Flags
field; see ``_SUPPRESS_OWNER_AVATAR_HP``).
"""
import types

from drserver.net import movement
from drserver.net.movement import (
    build_world_interval_packet, _heartbeat_hp,
    _queue_local_move_ack, _build_pending_local_move_ack)
from drserver.util.byte_io import LEReader, LEWriter


# ── helpers ──────────────────────────────────────────────────────────────────

def _move_record(move_type: int, heading: float, x: float, y: float) -> bytes:
    """One 13-byte UnitMover record: [moveType:u8][heading:i32][x:i32][y:i32]."""
    w = LEWriter()
    w.write_byte(move_type)
    w.write_int32(int(heading * 256))
    w.write_int32(int(x * 256))
    w.write_int32(int(y * 256))
    return w.to_array()


def _move_conn(**overrides):
    from drserver.net.connection import MessageQueue
    conn = types.SimpleNamespace(
        login_name="Styx3", unit_behavior_id=0x0123,
        hp_wire=200 * 256, client_hp_wire=None,
        pending_local_move_session=0, pending_local_move_count=0,
        pending_local_move_data=b"", pending_local_move_flush_at=0.0,
        interval_message_queue=MessageQueue(),
    )
    for k, v in overrides.items():
        setattr(conn, k, v)
    return conn


# ── WorldInterval packet (every-4th-tick owner packet) ────────────────────────

def test_world_interval_packet_layout():
    # Act — the every-4th-tick 0x0D WorldInterval (C# SendTickUpdates UGS:24312).
    packet = build_world_interval_packet(tick_count=8)
    r = LEReader(packet)

    # Assert — [0x07][0x0D][u32 tick][u32 0x21][u32 0x03][u32 0x01][u16 100][u16 20][0x06].
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x0D
    assert r.read_uint32() == 8
    assert r.read_uint32() == 0x21
    assert r.read_uint32() == 0x03
    assert r.read_uint32() == 0x01
    assert r.read_uint16() == 100
    assert r.read_uint16() == 20
    assert r.read_byte() == 0x06
    assert r.remaining == 0
    # No per-tick HP heartbeat and no RNG reseed ride the watchdog packet.
    assert 0x36 not in packet
    assert 0x0C not in packet


def test_world_interval_packet_appends_move_ack_before_endstream():
    # Arrange — a synthetic move-ack sub-message.
    move_ack = bytes([0x35, 0x23, 0x01, 0x65, 0x07, 0x01]) + _move_record(0x03, 90.0, 480.0, -191.0)

    # Act
    packet = build_world_interval_packet(tick_count=4, move_ack=move_ack)

    # Assert — WorldInterval body, then the move-ack, then EndStream.
    assert packet[:2] == bytes([0x07, 0x0D])
    assert move_ack in packet
    assert packet[-1] == 0x06                     # EndStream is last
    assert packet.index(move_ack) + len(move_ack) == len(packet) - 1


# ── Local move-ack (owner's own movement echoed back) ─────────────────────────

def test_local_move_ack_echoes_verbatim_records_with_hp_trailer(monkeypatch):
    # Arrange — fixed clock so we can advance past the 8 ms hold-off.
    clock = {"t": 1000.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    conn = _move_conn(unit_behavior_id=0x0123, hp_wire=72192, client_hp_wire=None)
    records = _move_record(0x03, 90.0, 480.0, -191.0) + _move_record(0x01, 45.0, 481.0, -190.0)

    # Act — queue two records for session 0x07, advance past the hold-off, build.
    _queue_local_move_ack(conn, session_id=0x07, move_count=2, raw_move=records)
    clock["t"] += 0.05
    ack = _build_pending_local_move_ack(conn)

    # Assert — 0x35 <ub:u16> 0x65 <session> <count> <verbatim records> 0x02 <hp:u32>.
    assert ack is not None
    r = LEReader(ack)
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0123
    assert r.read_byte() == 0x65
    assert r.read_byte() == 0x07                  # sessionId echoed verbatim
    assert r.read_byte() == 2                      # move count
    assert r.read_bytes(len(records)) == records   # VERBATIM client records
    assert r.read_byte() == 0x02                   # trailer flags: HP present
    assert r.read_uint32() == 72192                # _heartbeat_hp(conn)
    assert r.remaining == 0
    # Pending ack is consumed — a second build returns None.
    assert conn.pending_local_move_count == 0
    assert _build_pending_local_move_ack(conn) is None


def test_local_move_ack_none_when_nothing_pending():
    conn = _move_conn()
    assert _build_pending_local_move_ack(conn) is None


def test_reset_movement_relay_state_clears_stale_warp_data():
    # On a zone warp, stale pending move records from the OLD zone must be dropped
    # so the first tick in the NEW zone doesn't echo the client back (teleport-back
    # on arrival). start_tick calls _reset_movement_relay_state for this.
    conn = _move_conn(
        pending_local_move_session=5, pending_local_move_count=3,
        pending_local_move_data=b"x" * 39, pending_local_move_flush_at=123.0,
        last_raw_move_data=b"y" * 13, last_raw_move_count=1,
        stop_signal_sent=True, last_position_update_time=99.0,
    )

    movement._reset_movement_relay_state(conn)

    assert conn.pending_local_move_session == 0
    assert conn.pending_local_move_count == 0
    assert conn.pending_local_move_data == b""
    assert conn.pending_local_move_flush_at == 0.0
    assert conn.last_raw_move_data == b""
    assert conn.last_raw_move_count == 0
    assert conn.stop_signal_sent is False
    assert conn.last_position_update_time == 0.0
    # And with nothing pending, no move-ack is produced.
    assert _build_pending_local_move_ack(conn) is None


def test_local_move_ack_holds_off_until_flush_time(monkeypatch):
    # The 8 ms hold-off lets a burst of moves coalesce before the next tick flush.
    clock = {"t": 500.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    conn = _move_conn()
    _queue_local_move_ack(conn, session_id=0x02, move_count=1,
                          raw_move=_move_record(0x03, 0.0, 1.0, 2.0))
    # Same instant — still within the hold-off window → not yet due.
    assert _build_pending_local_move_ack(conn) is None
    clock["t"] += 0.05
    assert _build_pending_local_move_ack(conn) is not None


def test_local_move_ack_coalesces_same_session(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    conn = _move_conn()
    rec = _move_record(0x03, 0.0, 1.0, 2.0)

    _queue_local_move_ack(conn, session_id=0x05, move_count=1, raw_move=rec)
    _queue_local_move_ack(conn, session_id=0x05, move_count=1, raw_move=rec)

    # Two single-record moves on the same session coalesce into one 2-record ack.
    assert conn.pending_local_move_count == 2
    assert conn.pending_local_move_data == rec + rec


def test_local_move_ack_new_session_replaces_pending(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    conn = _move_conn()
    rec_a = _move_record(0x03, 0.0, 1.0, 2.0)
    rec_b = _move_record(0x01, 9.0, 9.0, 9.0)

    _queue_local_move_ack(conn, session_id=0x05, move_count=1, raw_move=rec_a)
    _queue_local_move_ack(conn, session_id=0x06, move_count=1, raw_move=rec_b)

    # A different session discards the old pending data (no cross-session mixing).
    assert conn.pending_local_move_session == 0x06
    assert conn.pending_local_move_count == 1
    assert conn.pending_local_move_data == rec_b


# ── Multiplayer relay (BroadcastPlayerMovement) ───────────────────────────────

class _RelayConn:
    def __init__(self, login_name, zone="world.town", instance_id=0):
        self.login_name = login_name
        self.current_zone_gc_type = zone
        self.instance_id = instance_id
        self.is_spawned = True
        self.sent = []

    def send_to_client(self, data):
        self.sent.append(data)


def _relay_server(mover, *viewers):
    conns = {mover.login_name: mover}
    remote_behavior_ids = {}
    for i, v in enumerate(viewers):
        conns[v.login_name] = v
        remote_behavior_ids[v.login_name] = {mover.login_name: 0x4000 + i}
    return types.SimpleNamespace(connections=conns, remote_behavior_ids=remote_behavior_ids)


def test_broadcast_relays_verbatim_records_to_viewer(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    mover = _move_conn()
    mover.login_name = "Mover"
    mover.current_zone_gc_type = "world.town"
    mover.instance_id = 0
    mover.last_position_update_time = 0.0
    mover.last_raw_move_data = b""
    mover.last_raw_move_count = 0
    mover.stop_signal_sent = False
    viewer = _RelayConn("Viewer")
    server = _relay_server(mover, viewer)
    rec = _move_record(0x03, 90.0, 480.0, -191.0)

    movement._broadcast_player_movement(server, mover, session_id=0x07, move_count=1, raw_move=rec)

    assert len(viewer.sent) == 1
    r = LEReader(viewer.sent[0])
    assert r.read_byte() == 0x07                   # BeginStream
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x4000               # viewer's remapped behavior id
    assert r.read_byte() == 0x65
    assert r.read_byte() == 0xFF                    # sessionId sentinel for relays
    assert r.read_byte() == 1                       # count
    assert r.read_bytes(len(rec)) == rec            # verbatim records
    assert r.read_byte() == 0x00                    # remote avatar: NO HP synch
    assert r.read_byte() == 0x06                    # EndStream
    assert r.remaining == 0


def test_broadcast_rate_limited_to_one_per_tick(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    mover = _move_conn()
    mover.login_name = "Mover"
    mover.current_zone_gc_type = "world.town"
    mover.instance_id = 0
    mover.last_position_update_time = 0.0
    mover.last_raw_move_data = b""
    mover.last_raw_move_count = 0
    mover.stop_signal_sent = False
    viewer = _RelayConn("Viewer")
    server = _relay_server(mover, viewer)
    rec = _move_record(0x03, 1.0, 2.0, 3.0)

    movement._broadcast_player_movement(server, mover, 0x07, 1, rec)
    # Second relay in the same tick window is dropped (C# LastPositionUpdateTime).
    movement._broadcast_player_movement(server, mover, 0x07, 1, rec)
    assert len(viewer.sent) == 1
    # After a tick interval, it relays again.
    clock["t"] += movement.TICK_INTERVAL + 0.001
    movement._broadcast_player_movement(server, mover, 0x07, 1, rec)
    assert len(viewer.sent) == 2


def test_broadcast_stop_signal_sent_once(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    mover = _move_conn()
    mover.login_name = "Mover"
    mover.current_zone_gc_type = "world.town"
    mover.instance_id = 0
    mover.last_position_update_time = 0.0
    mover.last_raw_move_data = b""
    mover.last_raw_move_count = 0
    mover.stop_signal_sent = False
    viewer = _RelayConn("Viewer")
    server = _relay_server(mover, viewer)
    rec = _move_record(0x03, 1.0, 2.0, 3.0)

    # Move once (remembers last records), then two stop ticks (moveCount=0).
    movement._broadcast_player_movement(server, mover, 0x07, 1, rec)
    clock["t"] += movement.TICK_INTERVAL + 0.001
    movement._broadcast_player_movement(server, mover, mover.session_id if hasattr(mover, "session_id") else 0, 0, b"")
    clock["t"] += movement.TICK_INTERVAL + 0.001
    movement._broadcast_player_movement(server, mover, 0, 0, b"")

    # One move relay + exactly one stop-signal replay (the second stop is suppressed).
    assert len(viewer.sent) == 2


def test_first_move_after_action_unroots_viewer_copy(monkeypatch):
    # After a relayed action roots P1's display copy on the viewer, the FIRST
    # move must relay a CancelAction (0x03) so the copy leaves the attack pose
    # and resumes following — else it freezes mid-swing (regression 2026-07-09).
    clock = {"t": 100.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    mover = _move_conn()
    mover.login_name = "Mover"
    mover.current_zone_gc_type = "world.town"
    mover.instance_id = 0
    mover.is_spawned = True
    mover.viewer_action_pending = True                 # an action is pending
    mover.player_pos_x = mover.player_pos_y = mover.player_heading = 0.0
    mover.session_id = 0
    mover.last_position_update_time = 0.0
    mover.last_raw_move_data = b""
    mover.last_raw_move_count = 0
    mover.stop_signal_sent = False
    viewer = _RelayConn("Viewer")
    server = _relay_server(mover, viewer)

    # One move record (moveCount=1): [session][count][record].
    body = bytes([0x07, 0x01]) + _move_record(0x03, 90.0, 5.0, 6.0)
    movement._handle_client_move(server, mover, LEReader(body), component_id=0x0123)

    # The viewer receives BOTH a CancelAction (un-root) and the move relay; the
    # pending flag is cleared so a held-still avatar isn't spammed with cancels.
    assert mover.viewer_action_pending is False
    # cancel packet: 0x07 0x35 <bhv:u16 LE=0x4000> 0x03 <sid> <synch> 0x06
    cancels = [p for p in viewer.sent
               if len(p) >= 5 and p[0] == 0x07 and p[1] == 0x35 and p[4] == 0x03]
    assert cancels == [bytes([0x07, 0x35, 0x00, 0x40, 0x03, 0x00, 0x00, 0x06])]


def test_move_without_pending_action_sends_no_cancel(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(movement, "_now", lambda: clock["t"])
    mover = _move_conn()
    mover.login_name = "Mover"
    mover.current_zone_gc_type = "world.town"
    mover.instance_id = 0
    mover.is_spawned = True
    mover.viewer_action_pending = False                # no action pending
    mover.player_pos_x = mover.player_pos_y = mover.player_heading = 0.0
    mover.session_id = 0
    mover.last_position_update_time = 0.0
    mover.last_raw_move_data = b""
    mover.last_raw_move_count = 0
    mover.stop_signal_sent = False
    viewer = _RelayConn("Viewer")
    server = _relay_server(mover, viewer)

    body = bytes([0x07, 0x01]) + _move_record(0x03, 90.0, 5.0, 6.0)
    movement._handle_client_move(server, mover, LEReader(body), component_id=0x0123)

    # No CancelAction — only the move relay.
    assert not [p for p in viewer.sent
                if len(p) >= 5 and p[0] == 0x07 and p[1] == 0x35 and p[4] == 0x03]


# ── Action ack (unchanged: still carries clamped HP trailer) ───────────────────

def test_action_ack_carries_clamped_hp_trailer():
    # A generic 0x06 activate ack is echoed back to the owning client. It MUST
    # carry the EntitySynchInfo trailer (flags=0x02 + HP) — the client compares
    # the Flags field, so a bare flags=0x00 trailer crashes a healthy avatar
    # (DISPROVEN track 1; see _SUPPRESS_OWNER_AVATAR_HP). The HP is clamped via
    # _heartbeat_hp (never above the client's last self-report).
    class _MsgQueue:
        def __init__(self):
            self.items = []

        def enqueue(self, msg):
            self.items.append(msg)

    class _Server:
        combat = None

    conn = types.SimpleNamespace(
        login_name="Styx3", unit_behavior_id=533, hp_wire=72192,
        client_hp_wire=None, session_id=0x10,
        equipment_component_id=9001, unit_container_id=9002,
        current_dialog_npc_id=None, message_queue=_MsgQueue(),
    )
    # cid=533(0x0215) sub=0x01 resp=0x00 action=0x06 sid=0x0a target=0x4321
    data = bytes.fromhex("1502010006" + "0a" + "2143")
    handled = movement._component_update(_Server(), conn, LEReader(data))

    assert handled is True
    ack = conn.message_queue.items[0]
    # ack = 0x35 <cid:u16> 0x01 <resp> 0x06 <sid> <target:u16> 0x02 <hp:u32>
    r = LEReader(ack)
    assert r.read_byte() == 0x35
    r.read_uint16(); r.read_byte(); r.read_byte()   # 0x01, resp
    assert r.read_byte() == 0x06                     # activate
    r.read_byte(); r.read_uint16()                   # sid, target
    assert r.read_byte() == 0x02                     # trailer flags: HP present
    assert r.read_uint32() == 72192                  # clamped _heartbeat_hp
    assert r.remaining == 0


# ── _heartbeat_hp: never ship HP above the last client report ─────────────────

def _conn(hp_wire, client_hp_wire):
    return types.SimpleNamespace(hp_wire=hp_wire, client_hp_wire=client_hp_wire)


def test_heartbeat_hp_clamps_to_last_client_report():
    # Server about to ship L2 max, client last self-reported damaged -> clamp down.
    assert _heartbeat_hp(_conn(hp_wire=72192, client_hp_wire=69978)) == 69978


def test_heartbeat_hp_uses_hp_wire_when_no_client_report():
    # No client report yet (fresh spawn): full level-max wire is correct.
    assert _heartbeat_hp(_conn(hp_wire=68096, client_hp_wire=None)) == 68096


def test_heartbeat_hp_does_not_inflate_above_hp_wire():
    # Client reported HIGHER than the server wire (post-heal) -> keep the server wire.
    assert _heartbeat_hp(_conn(hp_wire=68096, client_hp_wire=72192)) == 68096


# ── Warp-gate activation (no arrival-immunity workaround) ──

class _MsgQueue:
    def __init__(self):
        self.items = []

    def enqueue(self, msg):
        self.items.append(msg)


def _activate_conn():
    return types.SimpleNamespace(
        login_name="Styx3", unit_behavior_id=533, hp_wire=72192,
        client_hp_wire=None, session_id=0x10,
        equipment_component_id=9001, unit_container_id=9002,
        current_dialog_npc_id=None, message_queue=_MsgQueue(),
    )


def test_portal_activate_always_transfers():
    # The player now spawns offset from the warp (toward the corridor opening),
    # so it never overlaps the gate trigger and the client doesn't auto-fire on
    # arrival. A gate activate therefore always transfers — no immunity gating.
    from drserver.managers.portals import portal_manager, PortalData

    portal = PortalData(
        id=1, zone="dungeon11_level05", name="ZonePortal",
        gc_type="misc.ZonePortal_agg", pos_x=0.0, pos_y=0.0, pos_z=30.0,
        heading=0.0, width=60, height=30,
        target_zone="dungeon11_level04", spawn_point="start5", color=0xFFFF0000,
    )
    portal_manager.register_entity(0x4321, portal)

    class _Server:
        combat = None

        def __init__(self):
            self.transfers = []

        def change_zone(self, conn, target, spawn_point=""):
            self.transfers.append((target, spawn_point))

    # cid=533 sub=0x01 resp=0x00 action=0x06 sid=0x0a target=0x4321
    data = bytes.fromhex("1502010006" + "0a" + "2143")

    srv = _Server()
    conn = _activate_conn()
    assert movement._component_update(srv, conn, LEReader(data)) is True
    assert len(conn.message_queue.items) == 1            # acked
    assert srv.transfers == [("dungeon11_level04", "start5")]  # and transferred
