"""observe_monster_hp + the live 0x50 swing routing.

History: an earlier session hypothesized the client appends the monster HP as a
``0x02 <hpWire>`` suffix on its ``0x50`` swing, and wired that into the kill
pipeline. A live capture DISPROVED it — the inbound swing is exactly 9 bytes
(``1502 01 00 50 <sid> <useFlags> <target:2>``), no suffix, and the client
reports no monster HP over any channel. So the live ``0x50`` branch now feeds
the ROUTE 2B replay tracker (``CombatManager.register_swing``) instead.

These tests cover the two pieces that survived:
  * ``observe_monster_hp`` — the shared HP/death seam (still used by the
    0x36/0x03 ``handle_hp_sync`` path), and
  * the live ``0x50`` branch routing into ``register_swing`` (NOT a synchronous
    HP change — the kill resolves later on the tick loop).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers.combat import CombatManager
from drserver.net import movement
from drserver.util.byte_io import LEReader


class _MsgQueue:
    def __init__(self):
        self.items = []

    def enqueue(self, msg):
        self.items.append(msg)


class _Conn:
    conn_id = 1
    is_spawned = True
    current_zone_gc_type = "world.town"
    instance_id = 0
    equipment_component_id = 9001
    unit_container_id = 9002

    def __init__(self, hp_wire=68096):
        self.login_name = "Styx3"
        self.unit_behavior_id = 533
        self.hp_wire = hp_wire
        self.session_id = 0x10
        self.player_level = 1
        self.message_queue = _MsgQueue()
        # Combat action acks ride the interval queue (0x0D-frame carrier —
        # bible.md §2; held-button streams must not add per-tick messages).
        self.interval_message_queue = _MsgQueue()

    def send_to_client(self, data):
        return len(data)


class _Server:
    def __init__(self):
        self.connections = {}
        self.quests = None
        self.combat = None
        self.xp_awards = []
        self.reassert_calls = []

    def award_kill_xp(self, conn, monster_level):
        self.xp_awards.append((conn, monster_level))

    def reassert_control_on_action(self, conn, now):
        self.reassert_calls.append((conn, now))

    def enroll_instance_monsters(self, conn):
        return False


def _manager():
    server = _Server()
    cm = CombatManager(server)
    server.combat = cm
    return server, cm


def _register(cm, entity_id, hp=10000, level=2):
    cm.register_monster(entity_id, "test.type", "Dew Valley Pup", hp, level,
                        "GRUNT", "world.town", 0, 0, 0)


_TARGET_EID = 0x12  # the 0x50 swing below targets 0x0012


def _swing_bytes():
    """A 9-byte 0x50 BehaviourActionUseTarget body (no HP suffix — as captured).

    cid=533(0x0215) sub=0x01 resp=0x00 action=0x50 sid=0x0a target=0x1234
    high=0x00 -> actual_target_id = 0x12.
    """
    return bytes.fromhex("1502010050" + "0a" + "3412" + "00")


# ── observe_monster_hp: the shared seam (0x36/0x03 path) ─────────────────────
def test_observe_monster_hp_lowers_hp_without_death():
    server, cm = _manager()
    _register(cm, _TARGET_EID, hp=10000)
    conn = _Conn()

    handled = cm.observe_monster_hp(conn, _TARGET_EID, 5000, "TEST")

    assert handled is True
    assert cm.get_monster(_TARGET_EID).current_hp == 5000
    assert not cm.get_monster(_TARGET_EID).pending_kill
    assert server.xp_awards == []


def test_observe_monster_hp_triggers_kill_at_wire_floor():
    server, cm = _manager()
    _register(cm, _TARGET_EID, hp=10000, level=7)
    conn = _Conn()

    handled = cm.observe_monster_hp(conn, _TARGET_EID, 256, "TEST")

    assert handled is True
    assert not cm.is_monster(_TARGET_EID)
    assert server.xp_awards == [(conn, 7)]


def test_observe_monster_hp_unknown_entity_returns_false():
    server, cm = _manager()
    conn = _Conn()

    handled = cm.observe_monster_hp(conn, 99999, 0, "TEST")

    assert handled is False
    assert server.xp_awards == []


# ── live 0x50 branch routes into the replay tracker ──────────────────────────
def test_0x50_swing_registers_into_replay_without_synchronous_hp_change():
    server, cm = _manager()
    _register(cm, _TARGET_EID, hp=10000)
    conn = _Conn()

    handled = movement._component_update(server, conn, LEReader(_swing_bytes()))

    assert handled is True
    # The swing is registered for replay but resolves on the tick loop, so the
    # monster HP is NOT changed synchronously and no kill/XP fires yet.
    assert cm.get_monster(_TARGET_EID).current_hp == 10000
    assert server.xp_awards == []
    # The action ack is still echoed to the client — on the interval queue.
    assert len(conn.interval_message_queue.items) == 1


def test_0x50_swing_against_non_monster_is_a_noop_for_combat():
    server, cm = _manager()  # no monster registered at 0x12
    conn = _Conn()

    handled = movement._component_update(server, conn, LEReader(_swing_bytes()))

    assert handled is True
    assert server.xp_awards == []
    assert len(conn.interval_message_queue.items) == 1  # ack still sent


def test_0x50_swing_ack_sent_in_combat_zone_not_dropped():
    """Regression (live 2026-06-16): the Regime-B avatar-HP posture must NOT drop
    the 0x50 action ack in a combat zone. Dropping it (``if not _suppress_hp``)
    left the attack action unresolved → the avatar locked up, the client spammed
    CancelAction and could neither move nor attack. Action acks are load-bearing
    responses to the client's OWN packet and ship the clamped self-report, so they
    are sent regardless of zone. The other tests run in 'world.town' where
    suppression was already off, so this pins the dungeon case that was actually
    broken."""
    server, cm = _manager()
    _register(cm, _TARGET_EID, hp=10000)
    conn = _Conn()
    conn.current_zone_gc_type = "world.dungeon00"  # combat zone → suppress would be True

    handled = movement._component_update(server, conn, LEReader(_swing_bytes()))

    assert handled is True
    # The 0x50 ack MUST still be echoed in a combat zone (the bug dropped it).
    assert len(conn.interval_message_queue.items) == 1
    # ...and it carries flags=0x02 + HP (the avatar is an HP unit; a flags=0x00
    # trailer would crash it the other way).
    r = LEReader(conn.interval_message_queue.items[0])
    assert r.read_byte() == 0x35
    r.read_uint16(); r.read_byte(); r.read_byte()      # cid, 0x01, resp
    assert r.read_byte() == 0x50                        # action
    r.read_byte(); r.read_byte(); r.read_uint16()       # sid, use_flags, target
    assert r.read_byte() == 0x02                         # trailer: HP present


def test_0x50_swing_ack_rides_interval_queue_not_per_tick_flush():
    """Regression (live 2026-07-02): a HELD attack button is a sustained 0x50
    stream. Each ack flushed via the per-tick message_queue is an EXTRA
    channel-7 message on top of the exactly-saturated 7.5/s 0x0D cadence —
    the client's >2-backlog catch-up then runs the whole world at 3× ("game
    speeds up while holding attack", even in town; and the sped-up client-side
    mob approach outruns the chase pins = run-through on aggro). Combat acks
    must ride conn.interval_message_queue (drained INSIDE the 0x0D frame,
    bible.md §2), never conn.message_queue."""
    server, cm = _manager()
    _register(cm, _TARGET_EID, hp=10000)
    conn = _Conn()

    handled = movement._component_update(server, conn, LEReader(_swing_bytes()))

    assert handled is True
    assert conn.message_queue.items == [], \
        "0x50 ack must NOT ride the per-tick flush (breaks the §2 rate contract)"
    assert len(conn.interval_message_queue.items) == 1


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
