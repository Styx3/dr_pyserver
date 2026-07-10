"""Avatar control-authority re-assert — the level-up Avatar synch crash fix.

The vanilla client self-levels LOCALLY on kills (combat is client-authoritative)
and recomputes its avatar max HP (e.g. L1 wire 68096 -> L2 72192), telling the
server NOTHING over TCP (triple-confirmed live 2026-06-01). The server's
``conn.hp_wire`` stays at the stale L1 value, so every per-tick ``0x36`` heartbeat
and ``0x02`` synch trailer carries 68096 while the client computes 72192. The
client's type-2 synch compare on its OWN avatar (Ghidra FUN_005dd900) then
fatally mismatches (exit 0xc000013a) on EVERY tick after the level-up.

That compare is SKIPPED whenever the client holds input authority over its own
avatar (``ctrl[0x47][0x95]&1`` set). C# keeps it held by re-asserting authority
with a release-then-regrant control toggle (Control OFF ``0x64/0x00`` then ON
``0x64/0x01`` in one stream — ``SendClientControlReset``), fired when a combat
use-target resolves. A single Control-ON at spawn does NOT establish it; the
OFF->ON transition does. Porting that toggle (re-asserted on the client's attack
actions, throttled) keeps the bypass active through the local level-up so the HP
desync is harmless.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import drserver.net.game_server as gsmod
from drserver.net.game_server import (
    GameServer, _CONTROL_REASSERT_BURST, _CONTROL_REASSERT_INTERVAL)
from drserver.util.byte_io import LEReader, LEWriter


@pytest.fixture(autouse=True)
def _enable_reassert(monkeypatch):
    """The control-reassert burst is a legacy unpatched-client workaround, OFF by
    default (it snaps the avatar back to spawn on warp — the teleport-back bug).
    These tests exercise the mechanism itself, so enable it (DR_CONTROL_REASSERT=1).
    The default-off behavior is covered by test_*_disabled_by_default below.

    They also opt OUT of the Regime-B avatar-HP suppression (default-on since
    2026-06-15, bible.md §6 / §6-LIVE.8) via ``DR_AVATAR_HP_ORIGINATE=1``: this file
    verifies that the control toggle / combat-ack MECHANISM emits the right bytes
    and throttles correctly, which is orthogonal to the originate-vs-suppress
    policy. Without the opt-out the test double's empty ``current_zone_gc_type``
    reads as a combat zone and the toggle/ack is suppressed (the intended default —
    that policy is validated live against the unpatched client, per
    feedback_test_after_proven)."""
    monkeypatch.setattr(gsmod, "_CONTROL_REASSERT_ENABLED", True)
    monkeypatch.setenv("DR_AVATAR_HP_ORIGINATE", "1")


# ── Test doubles ────────────────────────────────────────────────────────────────
class _MsgQueue:
    def __init__(self):
        self.items = []

    def enqueue(self, msg):
        self.items.append(msg)


class _Conn:
    def __init__(self, ub_id=533, hp_wire=68096):
        self.login_name = "Styx3"
        self.unit_behavior_id = ub_id
        self.hp_wire = hp_wire
        self.session_id = 0x10
        self.equipment_component_id = 9001
        self.unit_container_id = 9002
        self.message_queue = _MsgQueue()
        # Combat action acks (0x50/0x51/0x52/cancel) ride the interval queue —
        # drained inside the per-4th-tick 0x0D frame (bible.md §2).
        self.interval_message_queue = _MsgQueue()
        self.sent: list[bytes] = []

    def send_to_client(self, packet: bytes) -> None:
        self.sent.append(packet)


def _make_server():
    """A GameServer without the heavy async __init__."""
    return object.__new__(GameServer)


def _parse_control_blocks(packet: bytes):
    """Decode the OFF->ON control-reset stream into a list of
    (component_id, control_byte, hp_wire) tuples."""
    assert packet[0] == 0x07, "must open with BeginStream"
    assert packet[-1] == 0x06, "must close with EndStream"
    r = LEReader(packet[1:-1])
    blocks = []
    while r.remaining > 0:
        assert r.read_byte() == 0x35, "each block is a ComponentUpdate"
        cid = r.read_uint16()
        assert r.read_byte() == 0x64, "FollowClient/StateMachine sub-message"
        control = r.read_byte()
        assert r.read_byte() == 0x02, "synch flag"
        hp = r.read_uint32()
        blocks.append((cid, control, hp))
    return blocks


# ── send_client_control_reset: the OFF->ON toggle ───────────────────────────────
def test_control_reset_emits_off_then_on_in_one_stream():
    # Arrange
    gs = _make_server()
    conn = _Conn(ub_id=533, hp_wire=72192)

    # Act
    gs.send_client_control_reset(conn)

    # Assert — exactly one stream, two control blocks: OFF (0x00) then ON (0x01).
    assert len(conn.sent) == 1
    blocks = _parse_control_blocks(conn.sent[0])
    assert blocks == [(533, 0x00, 72192), (533, 0x01, 72192)]


def test_control_reset_carries_current_hp_wire():
    gs = _make_server()
    conn = _Conn(ub_id=533, hp_wire=68096)

    gs.send_client_control_reset(conn)

    blocks = _parse_control_blocks(conn.sent[0])
    assert all(hp == 68096 for (_cid, _ctl, hp) in blocks)


def test_control_reset_clamps_trailer_to_client_reported_hp():
    # After zone entry conn.hp_wire is the level MAX, but the client has already
    # self-simmed damage and reported a lower value (client_hp_wire). The reset
    # trailer must ship the clamped (client) value, never [Remote]=MAX — that
    # fresh-ComponentUpdate MAX trailer is the live avatar synch crash
    # (2026-06-03: [Local]67282 vs [Remote]72192=MAX/Upd=0).
    gs = _make_server()
    conn = _Conn(ub_id=533, hp_wire=72192)   # level max after zone reset
    conn.client_hp_wire = 67282              # client's last self-report (damaged)

    gs.send_client_control_reset(conn)

    blocks = _parse_control_blocks(conn.sent[0])
    assert all(hp == 67282 for (_cid, _ctl, hp) in blocks), \
        "reset trailer must clamp to the client report, not max HP"


def test_control_reset_noop_without_unit_behavior_id():
    gs = _make_server()
    conn = _Conn(ub_id=0)

    gs.send_client_control_reset(conn)

    assert conn.sent == [], "no avatar behavior component yet -> nothing to assert"


# ── Zone-entry burst re-assert: fire on the STEADY action, not the spawn stream ──
# The spawn-stream control reset only set the TRANSIENT spawn action's authority
# bit (+0x95); the client swaps to a fresh idle/steady action (+0x95 bit CLEAR)
# before a mob can hit, and the bit does NOT carry across the swap — so the live
# re-test still crashed. The fix DEFERS the OFF->ON re-assert to the first inbound
# client packets after zone entry (movement 0x65 / hp-sync 0x36), when the steady
# action is live (live-validated 2026-06-02 by manually setting +0x95 on the idle
# action). Spawn state ARMS a throttled burst rather than toggling inline.
def _parse_spawn_action(packet: bytes):
    """Decode the SpawnAction stream (0x04 BehaviourActionSpawn) into a dict.

    Layout: 0x07 0x35 [ub:u16] 0x04 0x04 0xFF [x:i32][y:i32][z:i32]
            [some_unit_id:u16] 0x02 [hp:u32] 0x06
    """
    assert packet[0] == 0x07 and packet[-1] == 0x06
    r = LEReader(packet[1:-1])
    assert r.read_byte() == 0x35
    ub = r.read_uint16()
    assert r.read_byte() == 0x04, "CreateAction1"
    assert r.read_byte() == 0x04, "BehaviourActionSpawn"
    assert r.read_byte() == 0xFF, "SessionID"
    x, y, z = r.read_int32(), r.read_int32(), r.read_int32()
    some_unit_id = r.read_uint16()
    assert r.read_byte() == 0x02, "synch flag"
    hp = r.read_uint32()
    return {"ub": ub, "pos": (x, y, z), "some_unit_id": some_unit_id, "hp": hp}


def test_spawn_action_some_unit_id_is_avatar_entity_id():
    """The 0x04 BehaviourActionSpawn's SomeUnitID field must be the avatar's
    ENTITY id (C# SendPlayerEntitySpawn writes ``(ushort)avatar.Id``,
    UnityGameServer.cs:23980), NOT 0. This field binds the spawn action to the
    avatar entity so the client owns it as a local-input action; sending 0 leaves
    it unbound, a candidate cause for the action never reaching Active/+0x95=0x11
    (the fresh-warp avatar HP-synch crash)."""
    gs = _make_server()
    conn = _Conn(ub_id=533, hp_wire=72192)
    conn.player_heading = 0.0
    conn.avatar = type("_A", (), {"id": 0x1FE})()  # avatar entity id 510

    gs._send_avatar_spawn_state(conn, 100.0, 200.0, 300.0)

    action = _parse_spawn_action(conn.sent[0])
    assert action["ub"] == 533
    assert action["pos"] == (100 * 256, 200 * 256, 300 * 256)
    assert action["some_unit_id"] == 0x1FE, (
        "SomeUnitID must equal the avatar entity id (C# avatar.Id), not 0")


def test_spawn_state_arms_control_reassert_window():
    gs = _make_server()
    conn = _Conn(ub_id=533, hp_wire=72192)
    conn.player_heading = 0.0

    gs._send_avatar_spawn_state(conn, 100.0, 200.0, 300.0)

    # The burst window is armed and the throttle reset so the very first inbound
    # post-warp packet fires the re-assert...
    assert conn._control_reassert_pending == _CONTROL_REASSERT_BURST
    assert conn._last_control_reset_time == 0.0
    # ...and the spawn stream does NOT end with an inline OFF->ON control toggle:
    # its final packet is the UnitMoverUpdate (0x65), not a 0x64 control block.
    r = LEReader(conn.sent[-1][1:-1])
    assert r.read_byte() == 0x35
    r.read_uint16()
    assert r.read_byte() == 0x65, "spawn state must end with MoverUpdate, not a control toggle"


def test_arm_window_resets_throttle_and_sets_pending():
    gs = _make_server()
    conn = _Conn()
    conn._last_control_reset_time = 999.0   # stale throttle carried from a prior zone

    gs._arm_control_reassert_window(conn)

    assert conn._control_reassert_pending == _CONTROL_REASSERT_BURST
    assert conn._last_control_reset_time == 0.0


def test_reassert_after_zone_entry_fires_on_first_packet():
    gs = _make_server()
    conn = _Conn()
    gs._arm_control_reassert_window(conn)

    sent = gs.reassert_control_after_zone_entry(conn, now=100.0)

    assert sent is True
    assert len(conn.sent) == 1
    assert conn._control_reassert_pending == _CONTROL_REASSERT_BURST - 1


def test_reassert_after_zone_entry_noop_when_not_armed():
    gs = _make_server()
    conn = _Conn()

    sent = gs.reassert_control_after_zone_entry(conn, now=100.0)

    assert sent is False
    assert conn.sent == []


# ── Default-off behavior (the warp teleport-back fix) ───────────────────────────
def test_arm_window_disabled_by_default(monkeypatch):
    # With the reassert mechanism OFF (the default — superseded by the client
    # synch-crash patch), the spawn arm sets NO pending burst, so the first
    # post-warp movement packets do NOT toggle control and snap the avatar back.
    monkeypatch.setattr(gsmod, "_CONTROL_REASSERT_ENABLED", False)
    gs = _make_server()
    conn = _Conn()

    gs._arm_control_reassert_window(conn)

    assert conn._control_reassert_pending == 0
    assert gs.reassert_control_after_zone_entry(conn, now=100.0) is False
    assert conn.sent == []


def test_reassert_on_action_disabled_by_default(monkeypatch):
    monkeypatch.setattr(gsmod, "_CONTROL_REASSERT_ENABLED", False)
    gs = _make_server()
    conn = _Conn()

    assert gs.reassert_control_on_action(conn, now=100.0) is False
    assert conn.sent == []


def test_reassert_after_zone_entry_throttled_within_interval():
    gs = _make_server()
    conn = _Conn()
    gs._arm_control_reassert_window(conn)

    gs.reassert_control_after_zone_entry(conn, now=100.0)
    again = gs.reassert_control_after_zone_entry(conn, now=100.1)  # inside throttle

    assert again is False
    assert len(conn.sent) == 1
    assert conn._control_reassert_pending == _CONTROL_REASSERT_BURST - 1  # not decremented


def test_reassert_after_zone_entry_spans_burst_then_stops():
    gs = _make_server()
    conn = _Conn()
    gs._arm_control_reassert_window(conn)

    fired = 0
    t = 100.0
    for _ in range(_CONTROL_REASSERT_BURST + 4):     # over-poll past the burst
        if gs.reassert_control_after_zone_entry(conn, now=t):
            fired += 1
        t += _CONTROL_REASSERT_INTERVAL + 0.01       # one fire per throttle window

    assert fired == _CONTROL_REASSERT_BURST
    assert conn._control_reassert_pending == 0
    assert len(conn.sent) == _CONTROL_REASSERT_BURST


# ── Integration: an inbound ch7 packet re-asserts control after zone entry ──────
def test_inbound_ch7_packet_reasserts_control_after_zone_entry():
    from drserver.net import movement

    class _ZoneServer:
        combat = None

        def __init__(self):
            self.calls = []

        def reassert_control_after_zone_entry(self, conn, now):
            self.calls.append(now)
            return True

    server = _ZoneServer()
    conn = _Conn(ub_id=533)
    # An empty entity stream: BeginStream payload is just the EndStream byte.
    movement.handle(server, conn, 0x07, b"\x06")

    assert len(server.calls) == 1, "first inbound ch7 packet must re-assert avatar control"


# ── reassert_control_on_action: throttled re-assert ─────────────────────────────
def test_reassert_sends_on_first_action():
    gs = _make_server()
    conn = _Conn()

    sent = gs.reassert_control_on_action(conn, now=100.0)

    assert sent is True
    assert len(conn.sent) == 1


def test_reassert_throttles_rapid_actions():
    gs = _make_server()
    conn = _Conn()

    gs.reassert_control_on_action(conn, now=100.0)
    again = gs.reassert_control_on_action(conn, now=100.1)   # within the interval

    assert again is False
    assert len(conn.sent) == 1, "a second swing inside the throttle window must not re-send"


def test_reassert_resends_after_interval():
    gs = _make_server()
    conn = _Conn()

    gs.reassert_control_on_action(conn, now=100.0)
    later = gs.reassert_control_on_action(conn, now=101.0)   # past the interval

    assert later is True
    assert len(conn.sent) == 2


# ── Integration: a 0x50 attack action triggers a throttled re-assert ────────────
def test_0x50_attack_action_reasserts_control(monkeypatch):
    """Follow model (DR_MONSTER_AI=1): the 0x50 attack routes the target to
    monster_ai.aggro_from_attack (server-driven Follow + chase)."""
    from drserver.net import movement
    from drserver.managers import monster_ai

    class _FakeServer:
        def __init__(self):
            self.reassert_calls = []

        def reassert_control_on_action(self, conn, now):
            self.reassert_calls.append((conn, now))

    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", True)
    aggro_calls = []
    monkeypatch.setattr(monster_ai, "aggro_from_attack",
                        lambda server, conn, eid, now=None: aggro_calls.append((conn, eid)))

    server = _FakeServer()
    conn = _Conn(ub_id=533, hp_wire=68096)

    # cid=533(0x0215) sub=0x01 resp=0x00 action=0x50 sid=0x0a target=0x1234 high=0x00
    data = bytes.fromhex("15 02 01 00 50 0a 34 12 00".replace(" ", ""))
    reader = LEReader(data)

    handled = movement._component_update(server, conn, reader)

    assert handled is True
    assert len(server.reassert_calls) == 1, "the 0x50 attack path must re-assert avatar control"
    assert server.reassert_calls[0][0] is conn
    # the same attack aggros the targeted mob onto the player (server-driven
    # Follow + chase)
    # actual_target_id = (target_eid >> 8) | (high << 8) = 0x12 | 0x0000
    assert aggro_calls == [(conn, 0x0012)], \
        "0x50 attack must route the target to monster_ai.aggro_from_attack"
    # the action response itself is still echoed to the client — on the
    # interval queue (rides the 0x0D frame; held-button ack streams must not
    # exceed the one-message-per-133ms budget)
    assert len(conn.interval_message_queue.items) == 1
    assert conn.message_queue.items == []


def test_0x50_attack_is_native_by_default(monkeypatch):
    """Default combat model = NATIVE (bible §14.6 round 6n, LIVE-CONFIRMED
    2026-07-08): the 0x50 attack must NOT send the 0x64 enroll burst. The
    client's own monster brain already chases to melee and attacks correctly;
    the enroll is what broke it into a run-to-center chase. The action ack still
    rides the interval queue (mobs still take the player's damage); only the
    enroll is skipped."""
    from drserver.net import movement
    from drserver.managers import monster_ai

    class _FakeServer:
        def __init__(self):
            self.reassert_calls = []
            self.enroll_calls = []

        def reassert_control_on_action(self, conn, now):
            self.reassert_calls.append((conn, now))

        def enroll_instance_monsters(self, conn):
            self.enroll_calls.append(conn)

    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", False)
    monkeypatch.setattr(monster_ai, "LEGACY_ENROLL_ENABLED", False)
    aggro_calls = []
    monkeypatch.setattr(monster_ai, "aggro_from_attack",
                        lambda server, conn, eid, now=None: aggro_calls.append((conn, eid)))

    server = _FakeServer()
    conn = _Conn(ub_id=533, hp_wire=68096)

    data = bytes.fromhex("15 02 01 00 50 0a 34 12 00".replace(" ", ""))
    handled = movement._component_update(server, conn, LEReader(data))

    assert handled is True
    assert server.enroll_calls == [], \
        "default is native: the 0x50 attack must NOT enroll (client brain owns mobs)"
    assert aggro_calls == [], "follow-model aggro must stay off by default"
    # the action ack is still echoed (mobs still take the player's damage)
    assert len(server.reassert_calls) == 1
    assert len(conn.interval_message_queue.items) == 1
    assert conn.message_queue.items == []


def test_0x50_attack_enrolls_with_legacy_flag(monkeypatch):
    """Legacy escape hatch (DR_LEGACY_ENROLL=1): the first 0x50 attack sends the
    deferred 0x64 enroll burst (GameServer.enroll_instance_monsters). Retained
    for patched-client debugging only — it reintroduces the run-through
    (superseded by the native default, bible §14.6 round 6n)."""
    from drserver.net import movement
    from drserver.managers import monster_ai

    class _FakeServer:
        def __init__(self):
            self.reassert_calls = []
            self.enroll_calls = []

        def reassert_control_on_action(self, conn, now):
            self.reassert_calls.append((conn, now))

        def enroll_instance_monsters(self, conn):
            self.enroll_calls.append(conn)

    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", False)
    monkeypatch.setattr(monster_ai, "LEGACY_ENROLL_ENABLED", True)
    aggro_calls = []
    monkeypatch.setattr(monster_ai, "aggro_from_attack",
                        lambda server, conn, eid, now=None: aggro_calls.append((conn, eid)))

    server = _FakeServer()
    conn = _Conn(ub_id=533, hp_wire=68096)

    data = bytes.fromhex("15 02 01 00 50 0a 34 12 00".replace(" ", ""))
    handled = movement._component_update(server, conn, LEReader(data))

    assert handled is True
    assert server.enroll_calls == [conn], \
        "legacy flag: the 0x50 attack must send the deferred 0x64 enroll burst"
    assert aggro_calls == [], "follow-model aggro must stay off"
    assert len(server.reassert_calls) == 1
    assert len(conn.interval_message_queue.items) == 1
    assert conn.message_queue.items == []


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0

    class _MP:
        def setattr(self, *a, **k):
            pass

        def undo(self):
            pass

    for fn in funcs:
        try:
            fn(_MP()) if fn.__code__.co_argcount else fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
