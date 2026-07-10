"""Server-driven monster AI tests — Follow-action aggro + stepped chase.

Wire shapes are client-verified (Follow class id 0x16 via Ghidra registration
thunk 0x007e9ae0 + Follow::readData FUN_005227a0; the 0x65 mover block is
byte-identical to the live-proven spawn-stream OP8). These tests guard the
builders and the aggro/chase/throttle mechanics.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pytest

from drserver.managers import monster_ai
from drserver.managers.combat import TrackedMonster
from drserver.util.byte_io import LEReader


@pytest.fixture(autouse=True)
def _fresh_step_state(monkeypatch):
    """Reset the per-instance chase-step throttle between tests and force the
    follow model ON (these tests guard the DR_MONSTER_AI=1 machinery; the live
    default is the deferred-enroll model, under which tick/aggro are no-ops)."""
    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", True)
    monster_ai._instance_step_at.clear()
    yield
    monster_ai._instance_step_at.clear()


# ── Wire format ──────────────────────────────────────────────────────────────

def test_follow_packet_wire_shape():
    # Act
    packet = monster_ai.build_monster_follow_packet(
        behavior_id=0x0301, target_entity_id=0x0DB6, hp_wire=29184)
    r = LEReader(packet)

    # Assert — UNFRAMED message-queue ride (the tick flush adds the single
    # per-tick 0x07/0x06 frame; immediate per-packet frames accelerate the
    # client world clock — live-proven 2026-06-12).
    assert r.read_byte() == 0x35            # ComponentUpdate
    assert r.read_uint16() == 0x0301        # monster behavior id
    assert r.read_byte() == 0x04            # CreateAction
    assert r.read_byte() == 0x16            # "Follow" action class id
    assert r.read_byte() == 0x00            # Follow mode byte (+0x6d)
    assert r.read_uint16() == 0x0DB6        # target entity id
    assert r.read_byte() == 0x02            # EntitySynchInfo: HP present
    assert r.read_uint32() == 29184         # monster HP wire
    assert r.remaining == 0                 # no framing bytes


def test_move_packet_wire_shape():
    # Arrange — dest (100.5, -20.0), heading 90° → 23040 wire
    heading = monster_ai.heading_wire_toward(0.0, 0.0, 0.0, 50.0)

    # Act
    packet = monster_ai.build_monster_move_packet(
        behavior_id=0x0301, dest_x=100.5, dest_y=-20.0,
        heading_wire=heading, hp_wire=29184)
    r = LEReader(packet)

    # Assert — 0x35 <bid> 0x65 0x00 0x01 0x03 <hd> <fx> <fy> 0x02 <hp>, unframed
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0301
    assert r.read_byte() == 0x65            # MoverUpdate
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x01            # one mover entry
    assert r.read_byte() == 0x03
    assert r.read_int32() == 23040          # 90° × 256
    assert r.read_int32() == int(100.5 * 256)
    assert r.read_int32() == int(-20.0 * 256)
    assert r.read_byte() == 0x02
    assert r.read_uint32() == 29184
    assert r.remaining == 0                 # no framing bytes


def test_heading_wire_matches_drsnet_formula():
    # atan2(dy, dx) in degrees × 256, signed
    assert monster_ai.heading_wire_toward(0, 0, 10, 0) == 0
    assert monster_ai.heading_wire_toward(0, 0, 0, 10) == 90 * 256
    assert monster_ai.heading_wire_toward(0, 0, -10, 0) == 180 * 256
    assert monster_ai.heading_wire_toward(0, 0, 0, -10) == -90 * 256


# ── Test doubles ─────────────────────────────────────────────────────────────

class _FakeQueue:
    """Mirror of RRConnection.interval_message_queue — AI packets must ride
    the per-4th-tick 0x0D interval frame (one entity-channel message per
    133 ms), never immediate frames or the per-tick ack flush."""

    def __init__(self):
        self.items: List[bytes] = []

    def enqueue(self, packet):
        self.items.append(packet)


class _FakeConn:
    def __init__(self, name, x, y, *, spawned=True, avatar_id=0x0DB6, ub=0x0DCF,
                 conn_id=1):
        self.login_name = name
        self.conn_id = conn_id
        self.player_pos_x = x
        self.player_pos_y = y
        self.is_spawned = spawned
        self.unit_behavior_id = ub
        self.avatar = type("A", (), {"id": avatar_id})()
        self.message_queue = _FakeQueue()
        self.interval_message_queue = _FakeQueue()
        self.direct_sends: List[bytes] = []

    def send_to_client(self, packet):
        self.direct_sends.append(packet)

    @property
    def sent(self) -> List[bytes]:
        return self.interval_message_queue.items


@dataclass
class _FakeInstance:
    key: tuple
    monster_ids: List[int] = field(default_factory=list)


class _FakeCombat:
    def __init__(self):
        self._m: Dict[int, TrackedMonster] = {}

    def add(self, mon):
        self._m[mon.entity_id] = mon

    def get_monster(self, eid) -> Optional[TrackedMonster]:
        return self._m.get(eid)


class _FakeRegistry:
    def __init__(self, inst):
        self._instances = {inst.key: inst}
        self._inst = inst

    def key_for(self, conn):
        return self._inst.key

    def _broadcast(self, server, inst, packet):
        for c in server.connections.values():
            if c.is_spawned:
                c.send_to_client(packet)


class _FakeServer:
    def __init__(self, combat, registry=None):
        self.combat = combat
        self.world_instances = registry
        self.connections: Dict[int, _FakeConn] = {}


def _mob(eid=900, bid=901, x=0.0, y=0.0, hp=29184, attack_range=8.0):
    return TrackedMonster(
        entity_id=eid, gc_type="g", label="Warg Pup", current_hp=hp, max_hp=hp,
        level=2, difficulty="GRUNT", zone_gc_type="z",
        pos_x=x, pos_y=y, pos_z=0.0, spawn_time=0.0, behavior_id=bid,
        spawn_x=x, spawn_y=y, attack_range=attack_range)


def _setup(player_x=0.0, player_y=0.0, mob=None):
    mob = mob or _mob()
    inst = _FakeInstance(key=(1, 0), monster_ids=[mob.entity_id])
    combat = _FakeCombat(); combat.add(mob)
    reg = _FakeRegistry(inst)
    server = _FakeServer(combat, reg)
    conn = _FakeConn("Styx3", player_x, player_y)
    server.connections[1] = conn
    return server, reg, conn, mob


def _opcode(packet: bytes) -> int:
    """The behavior sub-op byte of an unframed update (after 0x35 <cid:u16>)."""
    return packet[3]


# ── Instance-driven entry point (per-instance tick loop) ──────────────────────

def test_tick_instance_drives_ai_directly():
    """The per-instance world tick calls tick_instance(inst) — no conn needed.

    This is the authoritative driver after the per-instance tick refactor; the
    old tick(conn) is now a thin shim over it.
    """
    server, reg, conn, mob = _setup(player_x=50.0)
    inst = reg._instances[(1, 0)]

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    follows = [p for p in conn.sent if _opcode(p) == 0x04]
    assert len(follows) == 1
    assert mob.target_id == 0x0DB6


def test_tick_shim_noop_when_instance_missing():
    """tick(conn) is a safe no-op when the conn's instance isn't registered
    (e.g. raced against a teardown) — it must not raise or aggro anything."""
    server, reg, conn, mob = _setup(player_x=50.0)
    reg._instances.clear()                               # conn maps to no instance

    monster_ai.tick(server, reg, conn, now=10.0)

    assert conn.sent == []
    assert mob.target_id == 0


# ── Aggro → Follow ───────────────────────────────────────────────────────────

def test_proximity_aggro_sends_follow_once():
    server, reg, conn, mob = _setup(player_x=50.0)       # 50 < 100 aggro radius

    monster_ai.tick(server, reg, conn, now=10.0)

    follows = [p for p in conn.sent if _opcode(p) == 0x04]
    assert len(follows) == 1
    assert mob.target_id == 0x0DB6                       # locked on the avatar

    # Second tick: target already locked — no second Follow.
    monster_ai.tick(server, reg, conn, now=10.05)
    follows = [p for p in conn.sent if _opcode(p) == 0x04]
    assert len(follows) == 1


def test_no_aggro_when_player_out_of_range():
    server, reg, conn, mob = _setup(player_x=500.0)      # > 100 aggro radius

    monster_ai.tick(server, reg, conn, now=10.0)

    assert conn.sent == []
    assert mob.target_id == 0


def test_aggro_from_attack_pulls_beyond_aggro_radius():
    server, reg, conn, mob = _setup(player_x=400.0)      # ranged pull distance

    monster_ai.aggro_from_attack(server, conn, mob.entity_id)

    assert mob.target_id == 0x0DB6
    follows = [p for p in conn.sent if _opcode(p) == 0x04]
    assert len(follows) == 1


def test_aggro_from_attack_ignores_non_monster_targets():
    server, reg, conn, mob = _setup(player_x=400.0)

    monster_ai.aggro_from_attack(server, conn, 0x4242)   # NPC/portal click

    assert conn.sent == []
    assert mob.target_id == 0


def test_player_hits_reassert_follow_throttled():
    """Regression (live 2026-07-02): the player's landed hits fire the mob's
    local OnDamaged, which can displace its Follow action client-side and
    leave the no-range-gate attack approach running the mob into the avatar.
    aggro_from_attack fires on every player 0x50 — when the mob already
    targets this player it must RE-ASSERT Follow, throttled to
    FOLLOW_REASSERT_INTERVAL (never one packet per held-button swing)."""
    server, reg, conn, mob = _setup(player_x=10.0)

    monster_ai.aggro_from_attack(server, conn, mob.entity_id, now=10.0)
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # first aggro Follow

    monster_ai.aggro_from_attack(server, conn, mob.entity_id, now=10.5)
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # inside throttle — no dup

    monster_ai.aggro_from_attack(server, conn, mob.entity_id, now=11.1)
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 2                             # re-asserted past 1.0s
    assert mob.target_id == 0x0DB6                       # target unchanged


def test_player_hits_do_not_reassert_follow_when_clamp_on(monkeypatch):
    """Regression (live 2026-07-03): with the clamp pinning the mob at range,
    re-asserting Follow on every player hit is unnecessary AND cancels the mob's
    in-flight 0xF0 swing animation ("mobs stop attacking by animation but damage
    still appears"). The initial aggro Follow (for approach) still fires; later
    hits must NOT re-send it."""
    monkeypatch.setattr(monster_ai, "MOB_CLAMP_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)

    monster_ai.aggro_from_attack(server, conn, mob.entity_id, now=10.0)
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # initial aggro Follow only

    monster_ai.aggro_from_attack(server, conn, mob.entity_id, now=12.0)  # past throttle
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # NO re-assert with clamp on


# ── Chase stepping ───────────────────────────────────────────────────────────

def test_chase_steps_toward_player_and_sends_move():
    server, reg, conn, mob = _setup(player_x=80.0)       # in aggro, out of reach

    monster_ai.tick(server, reg, conn, now=10.0)         # aggro (dt capped)
    start_x = mob.pos_x
    monster_ai.tick(server, reg, conn, now=10.1)         # step 0.1s

    assert mob.pos_x > start_x                           # walked toward player
    moves = [p for p in conn.sent if _opcode(p) == 0x65]
    assert len(moves) >= 1
    r = LEReader(moves[-1])
    r.read_bytes(7)                                      # 35 cid 65 00 01 03
    r.read_int32()                                       # heading
    # Dest = the STOP-RING point (player 80.0 minus the 16.0 effective range),
    # never the player's center: the client mover walks the mob all the way to
    # dest with no range gate, so a center dest lunges it through the avatar
    # in melee (the "runs through me as soon as I attack" bug, live 2026-07-02).
    ring = monster_ai.effective_attack_range(mob)
    assert r.read_int32() == int((80.0 - ring) * 256)    # dest = ring point
    assert r.read_int32() == 0


def test_chase_stops_at_effective_attack_range():
    mob = _mob(attack_range=8.0)                         # stop at 8+5+3 = 16
    server, reg, conn, mob = _setup(player_x=40.0, mob=mob)

    now = 10.0
    monster_ai.tick(server, reg, conn, now=now)
    for _ in range(200):                                 # plenty to arrive
        now += 0.1
        monster_ai.tick(server, reg, conn, now=now)

    dist = abs(40.0 - mob.pos_x)
    assert dist >= monster_ai.effective_attack_range(mob) - 0.01  # never overlaps
    assert dist <= monster_ai.effective_attack_range(mob) + 1.0   # arrived

    # In contact: no further move packets stream.
    sent_before = len(conn.sent)
    monster_ai.tick(server, reg, conn, now=now + 1.0)
    assert len(conn.sent) == sent_before


def test_ranged_mob_stands_off_at_weapon_range():
    mob = _mob(attack_range=90.0)                        # puker rifle
    server, reg, conn, mob = _setup(player_x=80.0, mob=mob)

    monster_ai.aggro_from_attack(server, conn, mob.entity_id)
    start_x = mob.pos_x
    monster_ai.tick(server, reg, conn, now=10.0)
    monster_ai.tick(server, reg, conn, now=10.1)

    assert mob.pos_x == start_x                          # 80 < 90+5+3 — no approach


def test_move_sends_throttled_to_interval():
    server, reg, conn, mob = _setup(player_x=80.0)
    monster_ai.aggro_from_attack(server, conn, mob.entity_id)
    conn.sent.clear()

    now = 10.0
    for _ in range(9):                                   # 9 ticks ~0.03s = 0.27s
        monster_ai.tick(server, reg, conn, now=now)
        now += 0.03

    moves = [p for p in conn.sent if _opcode(p) == 0x65]
    assert 1 <= len(moves) <= 2                          # ~0.27s / 0.15s throttle


def test_target_dropped_when_player_leaves():
    server, reg, conn, mob = _setup(player_x=80.0)
    monster_ai.aggro_from_attack(server, conn, mob.entity_id)

    conn.is_spawned = False                              # player left
    monster_ai.tick(server, reg, conn, now=10.0)

    assert mob.target_id == 0


def test_dead_or_dying_monster_never_aggros_or_chases():
    dying = _mob(hp=128)                                 # <= HP wire floor
    server, reg, conn, mob = _setup(player_x=10.0, mob=dying)

    monster_ai.tick(server, reg, conn, now=10.0)
    monster_ai.aggro_from_attack(server, conn, mob.entity_id)

    assert conn.sent == []
    assert mob.target_id == 0


def test_second_connection_same_tick_is_noop():
    server, reg, conn, mob = _setup(player_x=80.0)
    conn2 = _FakeConn("P2", 80.0, 0.0, avatar_id=0x0EEE, ub=0x0EEF)
    server.connections[2] = conn2

    monster_ai.tick(server, reg, conn, now=10.0)
    sent_after_first = len(conn.sent) + len(conn2.sent)
    monster_ai.tick(server, reg, conn2, now=10.0)        # same tick — guarded

    assert len(conn.sent) + len(conn2.sent) == sent_after_first


def test_kill_switch_disables_ai(monkeypatch):
    server, reg, conn, mob = _setup(player_x=10.0)
    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", False)

    monster_ai.tick(server, reg, conn, now=10.0)
    monster_ai.aggro_from_attack(server, conn, mob.entity_id)

    assert conn.sent == []
    assert mob.target_id == 0


# ── Code-9 purge on kill ──────────────────────────────────────────────────────

def _setup_real_queue(player_x=0.0, mob=None):
    """Like :func:`_setup` but the conn's interval queue is a REAL MessageQueue
    (it owns ``remove_where``, which the purge needs)."""
    from drserver.net.connection import MessageQueue

    mob = mob or _mob()
    inst = _FakeInstance(key=(1, 0), monster_ids=[mob.entity_id])
    combat = _FakeCombat(); combat.add(mob)
    reg = _FakeRegistry(inst)
    server = _FakeServer(combat, reg)
    conn = _FakeConn("Styx3", player_x, 0.0)
    conn.interval_message_queue = MessageQueue()
    server.connections[1] = conn
    return server, reg, conn, mob, inst


def test_purge_monster_removes_from_monster_ids():
    server, reg, conn, mob, inst = _setup_real_queue()

    monster_ai.purge_monster(server, reg, mob.entity_id, mob.behavior_id)

    assert mob.entity_id not in inst.monster_ids


def test_purge_monster_drops_queued_chase_packets():
    server, reg, conn, mob, inst = _setup_real_queue()
    # Two chase packets for the dying mob + one for a different behavior.
    conn.interval_message_queue.enqueue(
        monster_ai.build_monster_move_packet(mob.behavior_id, 1.0, 2.0, 0,
                                             mob.current_hp))
    conn.interval_message_queue.enqueue(
        monster_ai.build_monster_follow_packet(mob.behavior_id, 0x0DB6,
                                               mob.current_hp))
    other = monster_ai.build_monster_move_packet(0x0999, 3.0, 4.0, 0, 1000)
    conn.interval_message_queue.enqueue(other)

    monster_ai.purge_monster(server, reg, mob.entity_id, mob.behavior_id)

    # The dead mob's packets are gone; the other mob's packet survives.
    remaining = conn.interval_message_queue.dequeue_all()
    assert remaining == [other]


def test_purge_monster_noop_without_registry():
    server, reg, conn, mob, inst = _setup_real_queue()
    conn.interval_message_queue.enqueue(
        monster_ai.build_monster_move_packet(mob.behavior_id, 1.0, 2.0, 0, 1))

    monster_ai.purge_monster(server, None, mob.entity_id, mob.behavior_id)

    # Nothing touched — registry-less callers (e.g. unit tests) are safe.
    assert mob.entity_id in inst.monster_ids
    assert conn.interval_message_queue.count == 1


def test_purge_monster_unknown_eid_is_safe():
    server, reg, conn, mob, inst = _setup_real_queue()

    monster_ai.purge_monster(server, reg, 0xDEAD, 0xBEEF)   # not tracked anywhere

    assert mob.entity_id in inst.monster_ids


def test_purge_monster_without_behavior_id_only_untracks():
    """A mob with no behavior id (never streamed) just leaves monster_ids; the
    queue scan is skipped (nothing could reference a 0 behavior)."""
    server, reg, conn, mob, inst = _setup_real_queue(mob=_mob(bid=0))
    conn.interval_message_queue.enqueue(b"\x35\x00\x00rest")  # unrelated payload

    monster_ai.purge_monster(server, reg, mob.entity_id, 0)

    assert mob.entity_id not in inst.monster_ids
    assert conn.interval_message_queue.count == 1


# ── Server-driven mob→player damage injection (MOB_ATTACK) ────────────────────

class _FakeTelemetry:
    def __init__(self):
        self.attacks: List[tuple] = []
        self.clamps: List[tuple] = []

    def send_mob_attack(self, conn, mob_eid, damage_wire, element=0):
        self.attacks.append((conn, mob_eid, damage_wire, element))
        return True

    def send_mob_clamp(self, conn, mob_eid, ring_wire):
        self.clamps.append((conn, mob_eid, ring_wire))
        return True


def test_mob_swing_damage_wire_matches_monster_damage_curve():
    from drserver.combat.monster_curves import MonsterCurves
    mob = _mob()
    mob.level = 5

    dmg = monster_ai.mob_swing_damage_wire(mob)

    assert dmg == max(256, int(MonsterCurves.interp_damage(5)))  # grounded curve, floor 1.0 HP
    assert dmg >= 256


def test_attack_packet_wire_shape():
    packet = monster_ai.build_monster_attack_packet(
        behavior_id=0x0301, target_entity_id=0x0DB6, hp_wire=29184)
    r = LEReader(packet)

    assert r.read_byte() == 0x35            # ComponentUpdate
    assert r.read_uint16() == 0x0301        # monster behavior id
    assert r.read_byte() == 0x04            # CreateAction
    assert r.read_byte() == 0xF0            # AttackTarget2 (basic weapon swing)
    assert r.read_byte() == 0x00            # mode byte
    assert r.read_uint16() == 0x0DB6        # target entity id
    assert r.read_byte() == 0x02            # EntitySynchInfo: HP present
    assert r.read_uint32() == 29184
    assert r.remaining == 0                 # unframed


def test_in_contact_swing_sends_attack_animation(monkeypatch):
    """A swing emits the 0xF0 AttackTarget2 action so the mob visibly swings,
    in addition to the MOB_ATTACK damage telemetry."""
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    attacks = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0xF0]
    assert len(attacks) == 1
    assert len(server.telemetry.attacks) == 1   # damage telemetry alongside


def test_in_contact_mob_swings_on_cadence_when_inject_on(monkeypatch):
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)   # 10 < 16 effective range = contact
    inst = reg._instances[(1, 0)]
    telem = _FakeTelemetry()
    server.telemetry = telem

    monster_ai.tick_instance(server, reg, inst, now=10.0)
    assert len(telem.attacks) == 1                    # first swing fired
    assert telem.attacks[0][1] == mob.entity_id

    monster_ai.tick_instance(server, reg, inst, now=10.05)   # within cadence
    assert len(telem.attacks) == 1                    # no second swing yet

    monster_ai.tick_instance(server, reg, inst, now=12.0)    # past 1.5s cadence
    assert len(telem.attacks) == 2


def test_in_contact_streams_heartbeat_synch_when_inject_on(monkeypatch):
    """In contact, the mob keeps streaming a throttled 0x65 toward its own spot
    so the client hook has a game-thread drain point — but does NOT drift."""
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    moves = [p for p in conn.sent if _opcode(p) == 0x65]
    assert len(moves) == 1
    assert mob.pos_x == 0.0                           # held position (no drift)
    r = LEReader(moves[-1]); r.read_bytes(7); r.read_int32()  # skip to dest
    assert r.read_int32() == 0                        # dest = mob's own x (0), not player


def test_in_contact_follow_restored_between_swings_when_inject_on(monkeypatch):
    """Regression (live 2026-07-02): the 0xF0 swing action REPLACES the mob's
    Follow action client-side (CreateAction swaps the active action) and Follow
    was only ever sent on target change — after the first swing the client was
    left running AttackTarget2's no-range-gate approach into the avatar.
    While in contact the AI must restore Follow between swings: not inside
    SWING_FOLLOW_RESTORE_DELAY (would cancel the swing animation), throttled
    to FOLLOW_REASSERT_INTERVAL."""
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)       # in contact (10 < 16)
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)    # aggro + swing #1
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # the aggro Follow only

    monster_ai.tick_instance(server, reg, inst, now=10.2)    # swing-anim window
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # 0.2 < 0.4 delay — hold

    monster_ai.tick_instance(server, reg, inst, now=11.2)    # past delay+throttle
    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 2                             # Follow restored
    swings = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0xF0]
    assert len(swings) == 1                              # no extra swing fired


def test_clamp_streamed_for_aggroed_mob_when_enabled(monkeypatch):
    """Run-through fix (bible §14.6): with DR_MOB_CLAMP on, an aggroed mob gets an
    OP_MOB_CLAMP intent (ring = effective_attack_range ×256) so the client hook
    pins it at range. Server chase packets alone are cosmetic (client Follow owns
    movement), so this is the lever that actually reaches the mob."""
    monkeypatch.setattr(monster_ai, "MOB_CLAMP_ENABLED", True)
    mob = _mob(attack_range=8.0)                          # ring = 8+5+3 = 16
    server, reg, conn, mob = _setup(player_x=80.0, mob=mob)
    inst = reg._instances[(1, 0)]
    telem = _FakeTelemetry()
    server.telemetry = telem

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    assert len(telem.clamps) == 1
    sent_conn, mob_eid, ring_wire = telem.clamps[0]
    assert sent_conn is conn and mob_eid == mob.entity_id
    assert ring_wire == int(monster_ai.effective_attack_range(mob) * 256.0)


def test_enroll_clamp_streams_clamp_for_enrolled_mob(monkeypatch):
    """Enroll + clamp model (DR_MOB_ENROLL_CLAMP, DR_MONSTER_AI off): the client
    brain runs the mob natively; the server only streams the stop-ring clamp for
    ENROLLED mobs (simulated_by set) to the simulator's avatar. No Follow / chase /
    injection / 0xF0 — native attacks + animation, clamp only for position."""
    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", False)
    monkeypatch.setattr(monster_ai, "MOB_ENROLL_CLAMP_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=40.0)
    mob.simulated_by.add(conn.conn_id)                   # the player enrolled it
    inst = reg._instances[(1, 0)]
    telem = _FakeTelemetry()
    server.telemetry = telem

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    assert len(telem.clamps) == 1
    sent_conn, mob_eid, ring_wire = telem.clamps[0]
    assert sent_conn is conn and mob_eid == mob.entity_id
    assert ring_wire == int(monster_ai.effective_attack_range(mob) * 256.0)
    # No server-driven behaviour packets in this model.
    assert [p for p in conn.sent if p[3] == 0x04] == []  # no Follow/0xF0 CreateActions


def test_enroll_clamp_skips_unenrolled_mob(monkeypatch):
    """A mob the client isn't simulating yet (empty simulated_by) is NOT clamped —
    the clamp only constrains mobs the client brain is actually driving."""
    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", False)
    monkeypatch.setattr(monster_ai, "MOB_ENROLL_CLAMP_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=40.0)       # simulated_by empty
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    assert server.telemetry.clamps == []


def test_enroll_clamp_inactive_when_flag_off(monkeypatch):
    """With DR_MOB_ENROLL_CLAMP off and DR_MONSTER_AI off, tick_instance is a
    full no-op (the deferred-enroll default, unchanged)."""
    monkeypatch.setattr(monster_ai, "MONSTER_AI_ENABLED", False)
    monkeypatch.setattr(monster_ai, "MOB_ENROLL_CLAMP_ENABLED", False)
    server, reg, conn, mob = _setup(player_x=40.0)
    mob.simulated_by.add(conn.conn_id)
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    assert server.telemetry.clamps == []
    assert conn.sent == []


def test_clamp_not_streamed_when_disabled(monkeypatch):
    monkeypatch.setattr(monster_ai, "MOB_CLAMP_ENABLED", False)
    server, reg, conn, mob = _setup(player_x=80.0)
    inst = reg._instances[(1, 0)]
    telem = _FakeTelemetry()
    server.telemetry = telem

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    assert telem.clamps == []


def test_clamp_throttled_to_send_interval(monkeypatch):
    monkeypatch.setattr(monster_ai, "MOB_CLAMP_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=80.0)
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)
    monster_ai.tick_instance(server, reg, inst, now=10.1)   # < 0.2 s — throttled
    assert len(server.telemetry.clamps) == 1
    monster_ai.tick_instance(server, reg, inst, now=10.3)   # past 0.2 s
    assert len(server.telemetry.clamps) == 2


def test_in_contact_follow_not_restored_when_clamp_on(monkeypatch):
    """Regression (live 2026-07-03): with the clamp on, the in-contact Follow
    restore must be suppressed — it cancels the swing animation and the clamp
    already holds the mob at range. Only the swing (0xF0) should stream."""
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", True)
    monkeypatch.setattr(monster_ai, "MOB_CLAMP_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)       # in contact
    inst = reg._instances[(1, 0)]
    server.telemetry = _FakeTelemetry()

    monster_ai.tick_instance(server, reg, inst, now=10.0)    # aggro + swing #1
    monster_ai.tick_instance(server, reg, inst, now=11.5)    # past delay+throttle

    follows = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0x16]
    assert len(follows) == 1                             # aggro Follow only, no restore
    swings = [p for p in conn.sent if p[3] == 0x04 and p[4] == 0xF0]
    assert len(swings) >= 1                              # swings still stream


def test_no_swing_or_heartbeat_when_inject_off(monkeypatch):
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", False)
    server, reg, conn, mob = _setup(player_x=10.0)
    inst = reg._instances[(1, 0)]
    telem = _FakeTelemetry()
    server.telemetry = telem

    monster_ai.tick_instance(server, reg, inst, now=10.0)

    assert telem.attacks == []                        # no swing
    assert [p for p in conn.sent if _opcode(p) == 0x65] == []  # no heartbeat (just Follow)


def test_swing_noop_without_telemetry(monkeypatch):
    """No telemetry server wired (e.g. telemetry disabled) — the swing path is a
    safe no-op and never raises."""
    monkeypatch.setattr(monster_ai, "MOB_ATTACK_INJECT_ENABLED", True)
    server, reg, conn, mob = _setup(player_x=10.0)
    inst = reg._instances[(1, 0)]
    server.telemetry = None

    monster_ai.tick_instance(server, reg, inst, now=10.0)   # must not raise
    assert mob.last_attack_time == 0.0                # no swing recorded
