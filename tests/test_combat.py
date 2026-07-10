"""CombatManager tests — monster registration, HP tracking, death detection."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers.combat import CombatManager, TrackedMonster


class MockConnection:
    conn_id = 1
    is_spawned = True
    current_zone_gc_type = "world.town"
    instance_id = 0
    equipment_component_id = 100

    def send_to_client(self, data):
        self._last_sent = data
        return len(data)

    def send_raw(self, data):
        pass


class MockServer:
    def __init__(self):
        self.connections = {}
        self.combat = None
        self.quests = None
        self.xp_awards = []
        self.xp_accruals = []

    def award_kill_xp(self, conn, monster_level):
        # Full server XP grant + level-up (legacy / no-level kill path).
        self.xp_awards.append((conn, monster_level))

    def accrue_kill_xp(self, conn, monster_level):
        # XP accrual only — the client's KILL_AT snap owns the level.
        self.xp_accruals.append((conn, monster_level))


def _capture_loot_pos(monkeypatch):
    """Patch loot.generate_loot_for_monster to record the (x,y,z) it's called
    with. Returns the recording list."""
    from drserver.managers import loot
    recorded = []

    def fake(server, conn, pos_x, pos_y, pos_z, level, treasure_generators,
             difficulty=""):
        recorded.append((pos_x, pos_y, pos_z))

    monkeypatch.setattr(loot, "generate_loot_for_monster", fake)
    return recorded


def test_loot_uses_client_reported_death_pos(monkeypatch):
    """A KILL_AT death_pos drops loot at the mob's real spot — not the spawn
    anchor or the killer's position."""
    recorded = _capture_loot_pos(monkeypatch)
    server = MockServer()
    cm = CombatManager(server)
    cm.register_monster(50123, "t", "Dummy", 5000, 5, "GRUNT",
                        "world.town", 100.0, 200.0, 0.0)  # spawn anchor, never moved
    killer = MockConnection()
    killer.player_pos_x, killer.player_pos_y, killer.player_pos_z = 500.0, 500.0, 0.0

    cm.notify_client_kill(50123, killer, death_pos=(777.0, 888.0, 10.0))

    assert recorded == [(777.0, 888.0, 10.0)]


def test_loot_falls_back_to_killer_pos_without_death_pos(monkeypatch):
    """No death_pos + un-moved mob (client-simulated) → drop at the killer."""
    recorded = _capture_loot_pos(monkeypatch)
    server = MockServer()
    cm = CombatManager(server)
    cm.register_monster(50124, "t", "Dummy", 5000, 5, "GRUNT",
                        "world.town", 100.0, 200.0, 0.0)  # spawn anchor == pos
    killer = MockConnection()
    killer.player_pos_x, killer.player_pos_y, killer.player_pos_z = 500.0, 500.0, 0.0

    cm.notify_client_kill(50124, killer)   # no death_pos

    assert recorded == [(500.0, 500.0, 0.0)]


def test_level_synced_kill_accrues_xp_without_leveling(monkeypatch):
    """When the level was snapped from the client (KILL_AT), the server must NOT
    grant a full XP+level (that could make the server LEAD the client, which the
    synch compare crashes on) — but it must still ACCRUE experience so the stored
    value tracks the client and the zone-transfer re-send stops clobbering it.
    Legacy kills still take the full award path."""
    _capture_loot_pos(monkeypatch)
    server = MockServer()
    cm = CombatManager(server)
    cm.register_monster(50125, "t", "Dummy", 5000, 5, "GRUNT",
                        "world.town", 100.0, 200.0, 0.0)
    cm.register_monster(50126, "t", "Dummy", 5000, 5, "GRUNT",
                        "world.town", 100.0, 200.0, 0.0)
    killer = MockConnection()
    killer.player_pos_x = killer.player_pos_y = killer.player_pos_z = 0.0

    cm.notify_client_kill(50125, killer, level_synced=True)
    assert server.xp_awards == []                      # no full grant / level-up
    assert server.xp_accruals == [(killer, 5)]         # but XP is tracked

    cm.notify_client_kill(50126, killer)               # legacy path
    assert server.xp_awards == [(killer, 5)]           # full award
    assert server.xp_accruals == [(killer, 5)]         # unchanged


def test_kill_purges_mob_from_instance_and_queue(monkeypatch):
    """A kill removes the mob from its instance's monster_ids and drops any
    queued chase/follow packets BEFORE the despawn — the Code-9 stale-packet
    race (DR_MONSTER_AI)."""
    from dataclasses import dataclass, field
    from typing import List

    _capture_loot_pos(monkeypatch)
    from drserver.managers import monster_ai
    from drserver.net.connection import MessageQueue

    @dataclass
    class _Inst:
        key: tuple
        monster_ids: List[int] = field(default_factory=list)

    class _Reg:
        def __init__(self, inst):
            self._instances = {inst.key: inst}
            self._inst = inst

        def key_for(self, conn):
            return self._inst.key

    inst = _Inst(key=(7, 0), monster_ids=[50200])
    server = MockServer()
    server.world_instances = _Reg(inst)
    killer = MockConnection()
    killer.player_pos_x = killer.player_pos_y = killer.player_pos_z = 0.0
    killer.is_spawned = True
    killer.current_zone_gc_type = "world.town"
    killer.interval_message_queue = MessageQueue()
    server.connections[1] = killer

    cm = CombatManager(server)
    cm.register_monster(50200, "t", "Warg", 5000, 5, "GRUNT",
                        "world.town", 1.0, 2.0, 0.0, behavior_id=0x0301)
    killer.interval_message_queue.enqueue(
        monster_ai.build_monster_move_packet(0x0301, 1.0, 2.0, 0, 5000))

    cm.notify_client_kill(50200, killer)

    assert 50200 not in inst.monster_ids
    assert killer.interval_message_queue.is_empty()
    assert cm.get_monster(50200) is None        # tracking cleaned up too


def test_killer_despawn_deferred_others_immediate(monkeypatch):
    """The killer's entity-destroy is DEFERRED (it plays the death anim/corpse/
    fade locally); other displaying players get it immediately."""
    import asyncio
    from drserver.managers import combat as combat_mod

    monkeypatch.setattr(combat_mod, "_DEATH_DESPAWN_DELAY", 0.02)
    _capture_loot_pos(monkeypatch)
    server = MockServer()
    killer = MockConnection()
    killer.conn_id = 1
    other = MockConnection()
    other.conn_id = 2
    server.connections = {1: killer, 2: other}
    cm = CombatManager(server)
    cm.register_monster(50300, "t", "M", 5000, 5, "GRUNT",
                        "world.town", 1.0, 2.0, 0.0)

    async def run():
        cm.notify_client_kill(50300, killer)
        # Immediately: the non-killer displayer got the destroy; the killer didn't.
        assert getattr(other, "_last_sent", None) is not None
        assert getattr(killer, "_last_sent", None) is None
        await asyncio.sleep(0.05)                       # past the deferral
        assert getattr(killer, "_last_sent", None) is not None

    asyncio.run(run())


def test_deferred_despawn_skips_player_who_left_zone(monkeypatch):
    """A deferred destroy must NOT fire if the killer has since left the zone
    (re-validated at fire time) — no stale destroy into the new zone."""
    import asyncio
    from drserver.managers import combat as combat_mod

    monkeypatch.setattr(combat_mod, "_DEATH_DESPAWN_DELAY", 0.02)
    _capture_loot_pos(monkeypatch)
    server = MockServer()
    killer = MockConnection()
    killer.conn_id = 1
    server.connections = {1: killer}
    cm = CombatManager(server)
    cm.register_monster(50301, "t", "M", 5000, 5, "GRUNT",
                        "world.town", 1.0, 2.0, 0.0)

    async def run():
        cm.notify_client_kill(50301, killer)
        killer.current_zone_gc_type = "dungeon01_level01"   # zoned out
        await asyncio.sleep(0.05)
        assert getattr(killer, "_last_sent", None) is None  # destroy suppressed

    asyncio.run(run())


def test_tracked_monster_creation():
    m = TrackedMonster(
        entity_id=50000, gc_type="creatures.fade.general.boss",
        label="Test Boss", current_hp=10000, max_hp=10000,
        level=50, difficulty="BOSS", zone_gc_type="world.town",
        pos_x=100, pos_y=200, pos_z=0, spawn_time=0,
    )
    assert m.entity_id == 50000
    assert m.current_hp == 10000
    assert m.difficulty == "BOSS"
    assert not m.pending_kill


def test_combat_manager_register_and_get():
    server = MockServer()
    cm = CombatManager(server)
    cm.register_monster(50123, "test.type", "Test Dummy", 5000, 10, "GRUNT",
                        "world.town", 0, 0, 0)
    m = cm.get_monster(50123)
    assert m is not None
    assert m.label == "Test Dummy"
    assert m.current_hp == 5000
    assert m.max_hp == 5000
    assert cm.is_monster(50123)
    assert not cm.is_monster(99999)


def test_hp_sync_no_death():
    server = MockServer()
    cm = CombatManager(server)
    cm.register_monster(50200, "test.type", "Healthy", 10000, 10, "GRUNT",
                        "world.town", 0, 0, 0)

    from drserver.util.byte_io import LEReader
    # Build HP sync: entityId(2) flags(1) hp(4)
    data = bytearray()
    data.extend((50200 & 0xFF, (50200 >> 8) & 0xFF))
    data.append(0x02)  # flags with HP present
    data.extend((5000).to_bytes(4, 'little'))

    conn = MockConnection()
    reader = LEReader(bytes(data))
    result = CombatManager.handle_hp_sync(cm, conn, reader)
    assert result is True

    m = cm.get_monster(50200)
    assert m.current_hp == 5000  # updated
    assert not m.pending_kill    # HP > 0, not dead


def test_hp_sync_triggers_death():
    server = MockServer()
    cm = CombatManager(server)
    cm.register_monster(50300, "test.type", "Dying", 10000, 10, "GRUNT",
                        "world.town", 0, 0, 0)

    from drserver.util.byte_io import LEReader
    # Client reports HP = 0 (dead in wire format).
    data = bytearray()
    data.extend((50300 & 0xFF, (50300 >> 8) & 0xFF))
    data.append(0x02)
    data.extend((0).to_bytes(4, 'little'))

    conn = MockConnection()
    reader = LEReader(bytes(data))
    result = CombatManager.handle_hp_sync(cm, conn, reader)
    assert result is True

    # Monster should have been removed from tracking (killed).
    assert not cm.is_monster(50300)


def test_action_dispatch_echo():
    server = MockServer()
    cm = CombatManager(server)

    from drserver.util.byte_io import LEReader
    # Build action dispatch: responseId(1) actionType(0x50) sessionId(1) targetId(2) highByte(1)
    data = bytearray([0x01, 0x50, 0xFF, 0x05, 0x00, 0x00])

    conn = MockConnection()
    reader = LEReader(bytes(data))
    result = CombatManager.handle_action_dispatch(cm, conn, reader, component_id=100)
    assert result is True
    # Should have sent ActionResponse to client.
    assert hasattr(conn, '_last_sent')


def test_rng_seed():
    server = MockServer()
    cm = CombatManager(server)
    assert cm._rng_seed == 0

    from drserver.util.byte_io import LEReader
    reader = LEReader((0x12345678).to_bytes(4, 'little'))
    cm.set_rng_seed(reader)
    assert cm._rng_seed == 0x12345678


def _make_conn(conn_id, zone, instance_id):
    conn = MockConnection()
    conn.conn_id = conn_id
    conn.current_zone_gc_type = zone
    conn.instance_id = instance_id
    conn.login_name = f"p{conn_id}"
    conn.player_pos_x = conn.player_pos_y = conn.player_pos_z = 0.0
    conn.sent = []
    conn.send_to_client = conn.sent.append
    return conn


def test_despawn_is_instance_scoped(monkeypatch):
    """The Code-6 regression (live 2026-07-08): P1 kills a mob in HIS private
    copy of a dungeon; P2 solos a DIFFERENT copy of the same zone. The 0x05
    destroy names an eid P2's client never saw → "Invalid EntityID" → Zone
    communication error Code 6. The despawn must reach only the mob's own
    (zone, instance) — group members included, other copies excluded."""
    _capture_loot_pos(monkeypatch)
    server = MockServer()
    cm = CombatManager(server)
    server.combat = cm
    cm.register_monster(60001, "t", "Dummy", 5000, 5, "GRUNT",
                        "world.dungeon00", 100.0, 200.0, 0.0,
                        instance_id=7)

    killer = _make_conn(1, "world.dungeon00", 7)
    group_peer = _make_conn(2, "world.dungeon00", 7)      # same copy: sees it
    other_solo = _make_conn(3, "world.dungeon00", 2)      # own copy: must NOT
    town_player = _make_conn(4, "world.town", 0)
    server.connections = {c.conn_id: c for c in
                          (killer, group_peer, other_solo, town_player)}

    assert cm.notify_client_kill(60001, killer)

    assert len(group_peer.sent) == 1          # instance peer gets the destroy
    assert group_peer.sent[0][1] == 0x05
    assert other_solo.sent == []              # other copy: no unknown-eid packet
    assert town_player.sent == []
    # The killer's destroy is deferred cleanup; with no event loop it fires
    # immediately, still instance-validated.
    assert len(killer.sent) == 1


def test_late_kill_after_zone_change_drops_no_loot(monkeypatch):
    """A telemetry kill landing after the killer left the instance must not
    drop loot into the killer's NEW zone at stale coordinates."""
    recorded = _capture_loot_pos(monkeypatch)
    server = MockServer()
    cm = CombatManager(server)
    server.combat = cm
    cm.register_monster(60002, "t", "Dummy", 5000, 5, "GRUNT",
                        "world.dungeon00", 100.0, 200.0, 0.0,
                        instance_id=7)
    killer = _make_conn(1, "world.town", 0)   # already back in town
    server.connections = {1: killer}

    assert cm.notify_client_kill(60002, killer)

    assert recorded == []                     # no loot in the wrong zone
    assert server.xp_awards == [(killer, 5)]  # progression still credited


if __name__ == "__main__":
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
