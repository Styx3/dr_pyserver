"""Per-instance world-state tests.

Pins the multiplayer fix: a zone instance is populated ONCE (stable ids, no
re-spawn), every joiner receives the same snapshot, and the instance is torn
down only when its last player leaves. These are the behaviours that, when
broken, made a second player duplicate the whole world.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers.world_instance import WorldInstanceRegistry


# ── Test doubles ─────────────────────────────────────────────────────────────
class FakeConn:
    def __init__(self, login, zone_id=42, instance_id=0, zone_name="dungeon01",
                 conn_id=1):
        self.login_name = login
        self.conn_id = conn_id
        self.current_zone_id = zone_id
        self.instance_id = instance_id
        self.current_zone_name = zone_name
        self.current_zone_gc_type = "world.dungeon01"
        self.player_pos_x = 100.0
        self.player_pos_y = 0.0
        self.player_pos_z = 50.0
        self.is_spawned = True
        self.received: list[bytes] = []

    def send_to_client(self, packet: bytes) -> None:
        self.received.append(packet)


class FakeCombat:
    def __init__(self):
        self.unregistered: list[int] = []

    def unregister_monster(self, entity_id: int) -> None:
        self.unregistered.append(entity_id)


class FakeServer:
    def __init__(self):
        self._next = 1000
        self.connections: dict[int, FakeConn] = {}
        self.combat = FakeCombat()
        self.merchant_components = {}
        self.npc_merchant_cids = {}

    def allocate_entity_id(self) -> int:
        eid = self._next
        self._next += 1
        return eid

    def add(self, conn: FakeConn) -> None:
        self.connections[id(conn)] = conn


def _stub_builders(monkeypatch):
    """Make the three build_* functions return canned, allocator-backed entities
    and count how many times each runs."""
    calls = {"npcs": 0, "monsters": 0, "world": 0}

    def fake_npcs(server, zone_name, merchant_sink=None):
        calls["npcs"] += 1
        return [(server.allocate_entity_id(), b"npc")]

    def fake_monsters(server, zone_gc_type, pos, count=5, zone_name="",
                      instance_id=0):
        calls["monsters"] += 1
        return [(server.allocate_entity_id(), b"mob") for _ in range(2)]

    def fake_world(server, zone_name):
        calls["world"] += 1
        return [(server.allocate_entity_id(), b"chest")]

    import drserver.managers.npcs as npcs
    import drserver.managers.monsters as monsters
    import drserver.managers.world_entities as we
    monkeypatch.setattr(npcs, "build_zone_npcs", fake_npcs)
    monkeypatch.setattr(monsters, "build_zone_monsters", fake_monsters)
    monkeypatch.setattr(we, "build_zone_world_entities", fake_world)
    return calls


# ── Tests ────────────────────────────────────────────────────────────────────
def test_instance_populated_once_for_two_players(monkeypatch):
    """Second joiner must NOT trigger a re-spawn — the root duplication bug."""
    calls = _stub_builders(monkeypatch)
    reg = WorldInstanceRegistry()
    server = FakeServer()

    p1 = FakeConn("P1")
    server.add(p1)
    reg.enter(server, p1)

    # 1 npc + 2 monsters + 1 world entity were built exactly once.
    assert (calls["npcs"], calls["monsters"], calls["world"]) == (1, 1, 1)
    snapshot_ids = list(reg._instances[reg.key_for(p1)].entity_ids)
    assert len(snapshot_ids) == 4

    p2 = FakeConn("P2")
    server.add(p2)
    reg.enter(server, p2)

    # No rebuild on the second join.
    assert (calls["npcs"], calls["monsters"], calls["world"]) == (1, 1, 1)
    # Same stable ids are still the instance's entities.
    assert reg._instances[reg.key_for(p2)].entity_ids == snapshot_ids


def test_each_joiner_receives_full_snapshot(monkeypatch):
    _stub_builders(monkeypatch)
    reg = WorldInstanceRegistry()
    server = FakeServer()

    p1 = FakeConn("P1")
    server.add(p1)
    reg.enter(server, p1)
    p2 = FakeConn("P2")
    server.add(p2)
    reg.enter(server, p2)

    # Both players got all 4 entity create packets — nobody is missing entities.
    assert len(p1.received) == 4
    assert len(p2.received) == 4
    assert p1.received == p2.received


def test_instance_torn_down_only_when_empty(monkeypatch):
    _stub_builders(monkeypatch)
    reg = WorldInstanceRegistry()
    server = FakeServer()

    p1, p2 = FakeConn("P1"), FakeConn("P2")
    server.add(p1)
    server.add(p2)
    reg.enter(server, p1)
    reg.enter(server, p2)
    key = reg.key_for(p1)
    monster_ids = list(reg._instances[key].monster_ids)

    # P1 leaves — P2 still present, instance survives.
    server.connections.pop(id(p1))
    reg.leave(server, p1)
    assert key in reg._instances
    assert server.combat.unregistered == []

    # P2 leaves — now empty, instance torn down and monster combat state released.
    server.connections.pop(id(p2))
    reg.leave(server, p2)
    assert key not in reg._instances
    assert sorted(server.combat.unregistered) == sorted(monster_ids)


def test_town_zone_has_no_monsters(monkeypatch):
    calls = _stub_builders(monkeypatch)
    reg = WorldInstanceRegistry()
    server = FakeServer()

    p1 = FakeConn("P1", zone_name="newbie_town")
    server.add(p1)
    reg.enter(server, p1)

    # Public zones skip monster population.
    assert calls["monsters"] == 0
    assert calls["npcs"] == 1


# ── Per-instance world tick lifecycle ─────────────────────────────────────────
def test_enter_does_not_start_tick_without_event_loop(monkeypatch):
    """In a sync context (no running loop) enter() must not try to create a task
    — the per-instance world tick is a live-server concern and absent here. This
    is what keeps every sync test in this suite safe."""
    _stub_builders(monkeypatch)
    reg = WorldInstanceRegistry()
    server = FakeServer()
    p1 = FakeConn("P1")
    server.add(p1)

    reg.enter(server, p1)

    inst = reg._instances[reg.key_for(p1)]
    assert inst.tick_task is None


def test_instance_tick_runs_then_cancels_on_teardown(monkeypatch):
    """enter() under a running loop starts ONE per-instance world tick that
    drives monster_ai.tick_instance; teardown (last player leaves) cancels it."""
    _stub_builders(monkeypatch)

    calls = {"n": 0}
    import drserver.managers.monster_ai as mai
    import drserver.managers.world_instance as wi
    monkeypatch.setattr(
        mai, "tick_instance",
        lambda server, reg, inst, now: calls.__setitem__("n", calls["n"] + 1))
    # Tighten the cadence so the test doesn't wait on the 33 ms production tick.
    monkeypatch.setattr(wi, "INSTANCE_TICK_INTERVAL", 0.001)

    async def _run():
        reg = WorldInstanceRegistry()
        server = FakeServer()
        p1 = FakeConn("P1")
        server.add(p1)

        reg.enter(server, p1)
        inst = reg._instances[reg.key_for(p1)]
        assert inst.tick_task is not None and not inst.tick_task.done()

        await asyncio.sleep(0.02)                 # let it tick several times
        assert calls["n"] >= 1                    # the shared AI driver ran

        task = inst.tick_task
        server.connections.pop(id(p1))
        reg.leave(server, p1)                      # last player -> teardown
        assert reg.key_for(p1) not in reg._instances

        for _ in range(50):                        # let the cancellation settle
            if task.done():
                break
            await asyncio.sleep(0.002)
        assert task.done()

    asyncio.run(_run())


def test_second_joiner_reuses_running_tick(monkeypatch):
    """A second player entering a populated instance must not spawn a second
    tick task — start_instance_tick is idempotent."""
    _stub_builders(monkeypatch)
    import drserver.managers.monster_ai as mai
    import drserver.managers.world_instance as wi
    monkeypatch.setattr(mai, "tick_instance",
                        lambda server, reg, inst, now: None)
    monkeypatch.setattr(wi, "INSTANCE_TICK_INTERVAL", 0.001)

    async def _run():
        reg = WorldInstanceRegistry()
        server = FakeServer()
        p1, p2 = FakeConn("P1"), FakeConn("P2")
        server.add(p1)
        server.add(p2)

        reg.enter(server, p1)
        inst = reg._instances[reg.key_for(p1)]
        first_task = inst.tick_task

        reg.enter(server, p2)
        assert inst.tick_task is first_task        # same task, not a second one

        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


# ── Deferred monster client-AI enrollment ────────────────────────────────────
class _EnrollMonster:
    def __init__(self, behavior_id, current_hp, pending_kill=False,
                 simulated_by=None):
        self.behavior_id = behavior_id
        self.current_hp = current_hp
        self.pending_kill = pending_kill
        # Mirror the real TrackedMonster field (default_factory=set): the set of
        # conn_ids already simulating this mob via a 0x64 enroll.
        self.simulated_by = set() if simulated_by is None else simulated_by


class _EnrollCombat:
    def __init__(self, monsters):
        self._m = monsters  # entity_id -> _EnrollMonster

    def get_monster(self, eid):
        return self._m.get(eid)


def test_enroll_monsters_builds_per_client_0x64_burst():
    """enroll_monsters sends one 0x64 burst (only to the engaging client) for
    every live monster in its instance, skipping dead / behaviour-less mobs."""
    reg = WorldInstanceRegistry()
    server = FakeServer()
    server.combat = _EnrollCombat({
        10: _EnrollMonster(0xAAAA, 29184),
        11: _EnrollMonster(0xBBBB, 25600),
        12: _EnrollMonster(0xCCCC, 100, pending_kill=True),  # dead → skipped
        13: _EnrollMonster(0, 100),                          # no bid → skipped
    })
    conn = FakeConn("P1")
    server.add(conn)
    inst = reg.get_or_create(conn)
    inst.monster_ids = [10, 11, 12, 13]

    n = reg.enroll_monsters(server, conn)

    assert n == 2                                    # only the two live, controllable mobs
    assert len(conn.received) == 1                   # a single burst stream
    burst = conn.received[0]
    assert burst[0] == 0x07 and burst[-1] == 0x06    # framed
    # Parse structurally (a raw 0x64 byte-count is unsafe — it collides with HP
    # data bytes, e.g. 25600 = 00 64 00 00). Expect two 0x35/0x64 blocks.
    from drserver.util.byte_io import LEReader
    r = LEReader(burst)
    assert r.read_byte() == 0x07
    enrolled_bids = []
    while True:
        op = r.read_byte()
        if op == 0x06:
            break
        assert op == 0x35
        bid = r.read_uint16()
        assert r.read_byte() == 0x64                 # FollowClient
        assert r.read_byte() == 0x01                 # control ON
        assert r.read_byte() == 0x02                 # synch flag
        r.read_uint32()                               # hp wire
        enrolled_bids.append(bid)
    assert enrolled_bids == [0xAAAA, 0xBBBB]


def test_enroll_monsters_noop_without_monsters():
    reg = WorldInstanceRegistry()
    server = FakeServer()
    server.combat = _EnrollCombat({})
    conn = FakeConn("P1")
    server.add(conn)
    reg.get_or_create(conn)  # empty instance

    assert reg.enroll_monsters(server, conn) == 0
    assert conn.received == []


def test_enroll_monsters_is_one_shot_per_client():
    """A second enroll_monsters for the same client is a NO-OP: mobs already in
    the client's ``simulated_by`` are skipped, so no re-enroll 0x64 burst / stale
    HP synch is re-sent. The 0x50 attack path calls this on every swing (hit AND
    miss); re-poking the client AI made mobs "change behaviour / stop swinging
    whenever the player attacks" (live 2026-07-06, bible §6-LIVE.7)."""
    reg = WorldInstanceRegistry()
    server = FakeServer()
    server.combat = _EnrollCombat({
        10: _EnrollMonster(0xAAAA, 29184),
        11: _EnrollMonster(0xBBBB, 25600),
    })
    conn = FakeConn("P1", conn_id=7)
    server.add(conn)
    inst = reg.get_or_create(conn)
    inst.monster_ids = [10, 11]

    # First engage: both mobs wake, one burst goes out, this client is recorded.
    assert reg.enroll_monsters(server, conn) == 2
    assert len(conn.received) == 1
    assert server.combat.get_monster(10).simulated_by == {7}
    assert server.combat.get_monster(11).simulated_by == {7}

    # Every subsequent swing re-calls enroll: it must send NOTHING (mobs already
    # simulated by conn 7) so the client brain keeps running the swing.
    assert reg.enroll_monsters(server, conn) == 0
    assert len(conn.received) == 1                    # no second burst

    # A different client still enrolls its own copy (per-client ownership).
    other = FakeConn("P2", conn_id=9)
    server.add(other)
    assert reg.enroll_monsters(server, other) == 2
    assert len(other.received) == 1
    assert server.combat.get_monster(10).simulated_by == {7, 9}
