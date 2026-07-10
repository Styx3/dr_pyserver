"""Boss exit-gate open-on-death (DoorsToOpenOnDeath).

Guards the 2026-07-10 fix where killing the dungeon00 boss (RattleTooth) left
its exit gate sealed forever — clicking it kept nagging "Defeat the boss" even
though the boss was already dead (the open-on-death wire was deferred/unwired).

Wire (live-tested 2026-07-10): "opening" the gate REMOVES its portcullis via the
proven ``0x07 0x05 <eid> 0x06`` entity-destroy frame (same as the live mob
despawn), leaving the archway passable. A bare ``0x03 <eid> 0x0A`` entity-update
(the C#-derived NCI shape) was live-disproven — a debugger breakpoint on the
client's entity-update dispatcher, gated to the gate eid, took NO hit on a boss
kill, so the unwrapped update is never processed.

The boss's ``DoorsToOpenOnDeath="Boss00ExitGate"`` (content ``world/dungeon00/
mob/boss.gc``) is bridged to the gate ``world.dungeon00.data.BossGate`` via the
gate's ``Name`` **property** — the DB stores the node name (``BossGate``), so the
link is read from content, not the DB. The boss is tracked by its ``creatures.*``
stat row, so the resolver keys off the ``world.*`` entity gc_type.
"""
import _paths
from drserver.data import boss_door_resolver as bdr
from drserver.data.extracter_paths import resolve_extracter_dir
from drserver.managers import world_entities as we_module
from drserver.managers.world_entities import WorldEntityData, world_entity_manager
from drserver.util.byte_io import LEReader

import pytest


# ── Fakes ────────────────────────────────────────────────────────────────────

class _InstConn:
    def __init__(self, zone_gc_type: str, instance_id: int, spawned: bool = True):
        self.is_spawned = spawned
        self.current_zone_gc_type = zone_gc_type
        self.instance_id = instance_id
        self.sent = []

    def send_to_client(self, packet: bytes) -> None:
        self.sent.append(packet)


class _FakeInstance:
    def __init__(self, entity_ids):
        self.entity_ids = list(entity_ids)


class _FakeRegistry:
    def __init__(self, inst, zone_gc_type, instance_id):
        self._inst, self._zone, self._iid = inst, zone_gc_type, instance_id

    def find(self, zone_gc_type, instance_id):
        if zone_gc_type == self._zone and instance_id == self._iid:
            return self._inst
        return None


class _FakeServer:
    def __init__(self):
        self.connections = {}
        self.world_instances = None


class _Monster:
    def __init__(self, gc_type, zone_gc_type="world.dungeon00_level03_boss",
                 instance_id=0, label="Rattle Tooth", entity_gc_type=None):
        self.gc_type = gc_type
        # Mirrors TrackedMonster: gc_type is the creatures.* stat row; the world.*
        # entity node (which carries DoorsToOpenOnDeath) is entity_gc_type.
        self.entity_gc_type = gc_type if entity_gc_type is None else entity_gc_type
        self.zone_gc_type = zone_gc_type
        self.instance_id = instance_id
        self.label = label


def _gate(eid_name="BossGate", gc="world.dungeon00.data.BossGate") -> WorldEntityData:
    return WorldEntityData(
        id=51, zone_name="dungeon00_level03_boss", name=eid_name, gc_type=gc,
        entity_type="gate", pos_x=0.0, pos_y=0.0, pos_z=0.0, heading=0.0,
        floor_index=0, item_generator="", item_count=0, target_zone="",
        target_waypoint="", display_label="", flags=7,
    )


def _prime_dungeon00_caches() -> None:
    """Prime the content-resolver caches so the wiring tests are hermetic (no
    extracter dependency). Keys are lowercased gc_types (as the resolver keys)."""
    bdr._doors_cache["world.dungeon00.mob.boss"] = frozenset({"boss00exitgate"})
    bdr._name_cache["world.dungeon00.data.bossgate"] = "boss00exitgate"


# ── Wire ─────────────────────────────────────────────────────────────────────

def test_build_gate_open_is_wrapped_entity_destroy():
    # "Open" = remove the portcullis via the proven BeginStream·Destroy·EndStream
    # frame (0x07 0x05 <eid> 0x06), the same wire as the live mob despawn. A bare
    # 0x03 entity-update was live-disproven (never dispatched on the client).
    pkt = we_module._build_gate_open(0x1234)
    assert pkt == bytes([0x07, 0x05, 0x34, 0x12, 0x06])
    r = LEReader(pkt)
    assert r.read_byte() == 0x07            # BeginStream
    assert r.read_byte() == 0x05            # Destroy entity
    assert r.read_uint16() == 0x1234        # gate eid
    assert r.read_byte() == 0x06            # EndStream
    assert r.remaining == 0


# ── open_boss_doors ──────────────────────────────────────────────────────────

def test_open_boss_doors_broadcasts_to_instance_and_marks_opened():
    _prime_dungeon00_caches()
    gate_eid, other_eid = 0x5051, 0x5098
    world_entity_manager.register_entity(gate_eid, _gate())
    world_entity_manager._opened_gates.discard(gate_eid)

    zone, iid = "world.dungeon00_level03_boss", 0
    inst = _FakeInstance([gate_eid, other_eid])   # other_eid unregistered (skipped)
    server = _FakeServer()
    server.world_instances = _FakeRegistry(inst, zone, iid)
    here1 = _InstConn(zone, iid)
    here2 = _InstConn(zone, iid)
    elsewhere = _InstConn("world.town", 0)         # other zone — must NOT receive
    server.connections = {1: here1, 2: here2, 3: elsewhere}

    opened = we_module.open_boss_doors(server, _Monster("world.dungeon00.mob.boss"))

    assert opened == 1
    assert world_entity_manager.is_gate_opened(gate_eid)
    want = we_module._build_gate_open(gate_eid)
    assert here1.sent == [want]
    assert here2.sent == [want]
    assert elsewhere.sent == []


def test_open_boss_doors_uses_entity_gc_type_not_creature():
    # Regression (2026-07-10 live): the boss is TRACKED as its creatures.* stat
    # row (WhiskerBroodlingChampion) but DoorsToOpenOnDeath lives on the world.*
    # entity node — keying off gc_type alone left the gate sealed after the kill.
    _prime_dungeon00_caches()
    gate_eid = 0x5052
    world_entity_manager.register_entity(gate_eid, _gate())
    world_entity_manager._opened_gates.discard(gate_eid)
    zone, iid = "world.dungeon00_level03_boss", 0
    server = _FakeServer()
    server.world_instances = _FakeRegistry(_FakeInstance([gate_eid]), zone, iid)
    here = _InstConn(zone, iid)
    server.connections = {1: here}

    boss = _Monster("creatures.whiskers.broodling.basic.champion",
                    label="WhiskerBroodlingChampion",
                    entity_gc_type="world.dungeon00.mob.boss")
    opened = we_module.open_boss_doors(server, boss)

    assert opened == 1
    assert world_entity_manager.is_gate_opened(gate_eid)
    assert here.sent == [we_module._build_gate_open(gate_eid)]


def test_open_boss_doors_ignores_non_world_mobs():
    # Grunts are creatures.* — they never author DoorsToOpenOnDeath, so the call
    # short-circuits before any content read or instance lookup.
    server = _FakeServer()
    server.world_instances = _FakeRegistry(_FakeInstance([]), "z", 0)
    conn = _InstConn("world.dungeon00_level03_boss", 0)
    server.connections = {1: conn}

    opened = we_module.open_boss_doors(
        server, _Monster("creatures.whiskers.blademaster.basic.grunt"))

    assert opened == 0
    assert conn.sent == []


def test_open_boss_doors_no_matching_gate_in_instance():
    _prime_dungeon00_caches()
    # A gate whose door Name does not match the boss's DoorsToOpenOnDeath.
    stray_eid = 0x5061
    bdr._name_cache["world.town.data.pvpgate"] = "pvpgate1"
    world_entity_manager.register_entity(
        stray_eid, _gate(eid_name="PvPGate1", gc="world.town.data.PvPGate"))
    zone, iid = "world.dungeon00_level03_boss", 0
    server = _FakeServer()
    server.world_instances = _FakeRegistry(_FakeInstance([stray_eid]), zone, iid)
    conn = _InstConn(zone, iid)
    server.connections = {1: conn}

    opened = we_module.open_boss_doors(server, _Monster("world.dungeon00.mob.boss"))

    assert opened == 0
    assert conn.sent == []
    assert not world_entity_manager.is_gate_opened(stray_eid)


def test_open_boss_doors_missing_instance_is_safe():
    _prime_dungeon00_caches()
    server = _FakeServer()
    server.world_instances = _FakeRegistry(_FakeInstance([]), "other.zone", 9)
    assert we_module.open_boss_doors(
        server, _Monster("world.dungeon00.mob.boss")) == 0


# ── late joiner ──────────────────────────────────────────────────────────────

def test_reopen_gates_for_joiner_only_sends_opened():
    opened_eid, closed_eid = 0x6001, 0x6002
    world_entity_manager._opened_gates.discard(closed_eid)
    world_entity_manager.mark_gate_opened(opened_eid)
    conn = _InstConn("world.dungeon00_level03_boss", 0)

    we_module.reopen_gates_for_joiner(conn, [opened_eid, closed_eid])

    assert conn.sent == [we_module._build_gate_open(opened_eid)]


# ── content resolver (real extracter, skipped when absent) ───────────────────

@pytest.mark.skipif(resolve_extracter_dir() is None,
                    reason="extracter content not present")
def test_resolver_bridges_dungeon00_boss_to_its_gate_from_content():
    bdr.clear_cache()
    doors = bdr.doors_opened_by("world.dungeon00.mob.boss")
    assert "boss00exitgate" in doors
    # The gate's DB node-name is "BossGate"; the link lives in its Name property.
    assert bdr.door_name_of("world.dungeon00.data.BossGate") == "boss00exitgate"
    assert bdr.door_name_of("world.dungeon00.data.BossGate") in doors


@pytest.mark.skipif(resolve_extracter_dir() is None,
                    reason="extracter content not present")
def test_resolver_returns_empty_for_mob_without_doors():
    bdr.clear_cache()
    assert bdr.doors_opened_by(
        "creatures.whiskers.blademaster.basic.grunt") == frozenset()
