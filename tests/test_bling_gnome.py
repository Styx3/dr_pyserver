"""Bling Gnome wire/behaviour tests — guards the DRS-NET BlingGnomeRuntime port.

Every packet shape here mirrors the DRS-NET (C#) builder byte-for-byte; those
builders are live-proven against the native client (BlingGnomeRuntime.cs).
Round-trips the entity snapshot, component create, mover update, and the
ConvertItemsToGold action, plus the henchman HP curve and conversion gating.
"""
import _paths  # noqa: F401 — sys.path bootstrap

from drserver.managers import bling_gnome as bg
from drserver.managers.bling_gnome import BlingGnomeManager, GnomeState
from drserver.managers.loot import DroppedItem, drops_near, register_drop, remove_drop
from drserver.util.byte_io import LEReader


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 100
        self.connections = {}
        self.remote_avatar_ids = {}

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid

    def get_player_avatar_id(self, login):
        return 0


def _gnome_state(**overrides) -> GnomeState:
    base = dict(entity_id=0x6001, behavior_id=0x6002, modifiers_id=0x6003,
                manipulators_id=0x6004, owner_login="tester", owner_conn_id=1,
                pos_x=100.0, pos_y=200.0, pos_z=8.0, heading=90.0,
                snapshot_level=10, hp_wire=bg.gnome_hp_wire(10))
    base.update(overrides)
    g = GnomeState(**base)
    g.spawn_pos_x, g.spawn_pos_y = g.pos_x, g.pos_y
    g.spawn_heading = g.heading
    return g


def _manager() -> BlingGnomeManager:
    return BlingGnomeManager(_FakeServer())


# ── HP curve (DRS-NET ResolveBlingGnomeHitPointsWire) ────────────────────────

def test_gnome_hp_curve_level1_matches_drsnet_fixed_point():
    # Arrange / Act: 245 × 0.3 in 16.16 fixed point = 73 HP
    hp = bg.gnome_hp_wire(1)
    # Assert: 73 × 256 on the wire
    assert hp == 73 * 256


def test_gnome_hp_curve_interpolates_between_anchor_levels():
    # level 3 sits halfway between (1, 245) and (5, 1550)
    hp3 = bg.gnome_hp_wire(3)
    assert bg.gnome_hp_wire(1) < hp3 < bg.gnome_hp_wire(5)


def test_gnome_hp_curve_clamps_past_table_end():
    assert bg.gnome_hp_wire(200) == bg.gnome_hp_wire(110)


# ── Entity snapshot (DRS-NET BuildEntitySnapshotPacket) ──────────────────────

def test_entity_snapshot_layout_with_owner():
    # Arrange
    mgr, g = _manager(), _gnome_state()

    # Act
    pkt = mgr.build_entity_snapshot_packet(g, owner_entity_id=0x1234)
    r = LEReader(pkt)

    # Assert — framed create + StockUnit init, owner bit set
    assert r.read_byte() == 0x07                       # BeginStream
    assert r.read_byte() == 0x01                       # EntityCreate
    assert r.read_uint16() == g.entity_id
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == bg.ENTITY_GC_TYPE
    assert r.read_byte() == 0x02                       # EntityInit
    assert r.read_uint16() == g.entity_id
    assert r.read_uint32() == 0x06                     # world-entity flags
    assert r.read_int32() == int(g.pos_x * 256)
    assert r.read_int32() == int(g.pos_y * 256)
    assert r.read_int32() == int(g.pos_z * 256)
    assert r.read_int32() == int(g.heading * 256)
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x17                       # unitFlags 0x16 | owner
    assert r.read_byte() == g.snapshot_level
    r.read_uint16(); r.read_uint16()
    assert r.read_uint16() == 0x1234                   # owner entity ref
    assert r.read_uint32() == g.hp_wire
    assert r.read_uint32() == 0                        # mana
    assert pkt[-1] == 0x06                             # EndStream


def test_entity_snapshot_without_owner_omits_owner_ref():
    mgr, g = _manager(), _gnome_state()

    with_owner = mgr.build_entity_snapshot_packet(g, owner_entity_id=0x1234)
    without = mgr.build_entity_snapshot_packet(g, owner_entity_id=0)

    # owner ref is a u16: packet shrinks by exactly 2 and flags lose bit 0
    assert len(with_owner) - len(without) == 2
    r = LEReader(without)
    r.read_byte(); r.read_byte(); r.read_uint16(); r.read_byte()
    r.read_cstring(); r.read_byte(); r.read_uint16(); r.read_uint32()
    r.read_int32(); r.read_int32(); r.read_int32(); r.read_int32(); r.read_byte()
    assert r.read_byte() == 0x16                       # no owner bit


# ── Component create packet (DRS-NET DelayedBehaviorCreate) ──────────────────

def test_component_packet_creates_modifiers_manipulators_behavior():
    mgr, g = _manager(), _gnome_state()

    pkt = mgr._build_component_packet(g)
    r = LEReader(pkt)

    assert r.read_byte() == 0x07
    # Modifiers
    assert r.read_byte() == 0x32
    assert r.read_uint16() == g.entity_id
    assert r.read_uint16() == g.modifiers_id
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "Modifiers"
    assert r.read_byte() == 0x01
    r.read_uint32(); r.read_uint32(); r.read_byte()
    # Manipulators
    assert r.read_byte() == 0x32
    assert r.read_uint16() == g.entity_id
    assert r.read_uint16() == g.manipulators_id
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "Manipulators"
    assert r.read_byte() == 0x01
    assert r.read_byte() == 0x00
    # Behavior (typed)
    assert r.read_byte() == 0x32
    assert r.read_uint16() == g.entity_id
    assert r.read_uint16() == g.behavior_id
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == bg.BEHAVIOR_GC_TYPE
    assert pkt[-1] == 0x06


# ── Mover update (DRS-NET SendMoverUpdate) ───────────────────────────────────

def test_mover_update_single_record_when_walking():
    mgr, g = _manager(), _gnome_state()
    g.mover_valid = True
    g.mover_x, g.mover_y, g.mover_heading = 90.0, 200.0, 90.0

    pkt = mgr.build_mover_update(g, terminal=False)
    r = LEReader(pkt)

    assert r.read_byte() == 0x35
    assert r.read_uint16() == g.behavior_id
    assert r.read_byte() == 0x65                       # MoverUpdate
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x01                       # one record
    flags = r.read_byte()
    assert flags & 0x01 == 0                           # not terminal
    assert r.read_int32() == int(g.heading * 256)
    assert r.read_int32() == int(g.pos_x * 256)
    assert r.read_int32() == int(g.pos_y * 256)
    assert r.read_byte() == 0x02                       # synch: HP present
    assert r.read_uint32() == g.hp_wire
    assert r.remaining == 0


def test_mover_update_terminal_writes_two_record_stop():
    mgr, g = _manager(), _gnome_state()

    pkt = mgr.build_mover_update(g, terminal=True)
    r = LEReader(pkt)

    r.read_byte(); r.read_uint16(); r.read_byte(); r.read_byte()
    assert r.read_byte() == 0x02                       # two records
    first_flags = r.read_byte()
    assert first_flags & 0x01 == 0                     # lead record not terminal
    r.read_int32(); r.read_int32(); r.read_int32()
    second_flags = r.read_byte()
    assert second_flags & 0x01 == 0x01                 # stop record terminal
    r.read_int32()
    assert r.read_int32() == int(g.pos_x * 256)        # stops at gnome pos
    assert r.read_int32() == int(g.pos_y * 256)


# ── ConvertItemsToGold action (DRS-NET SendConvertItemsToGoldAction) ─────────

def test_convert_action_carries_radius_owner_pos_and_gold_mod():
    mgr, g = _manager(), _gnome_state()

    pkt = mgr.build_convert_action(g, owner_x=300.0, owner_y=400.0)
    r = LEReader(pkt)

    assert r.read_byte() == 0x35
    assert r.read_uint16() == g.behavior_id
    assert r.read_byte() == 0x04                       # CreateAction
    assert r.read_byte() == 0xA1
    assert r.read_byte() == 0x00
    assert r.read_uint32() == 5                        # ConvertItemsToGold id
    assert r.read_uint16() == bg.CONVERT_SEARCH_RADIUS
    assert r.read_int32() == 300 * 256
    assert r.read_int32() == 400 * 256
    assert r.read_int32() == int(bg.GOLD_VALUE_MOD * 256)
    assert r.read_byte() == 0x02
    assert r.read_uint32() == g.hp_wire
    assert r.remaining == 0


# ── Skill / target / conversion gating ───────────────────────────────────────

def test_is_gnome_skill_matches_summon_class_only():
    assert BlingGnomeManager.is_gnome_skill("skills.generic.SummonBlingGnome")
    assert BlingGnomeManager.is_gnome_skill("SKILLS.GENERIC.SUMMONBLINGGNOME")
    assert not BlingGnomeManager.is_gnome_skill("skills.generic.FireBolt")
    assert not BlingGnomeManager.is_gnome_skill(None)


def test_can_convert_rejects_gold_high_rarity_and_protected_classes():
    ok = DroppedItem(entity_id=1, gc_class="items.pal.swordpal.normal001", rarity=2)
    assert BlingGnomeManager._can_convert(ok)
    assert not BlingGnomeManager._can_convert(
        DroppedItem(entity_id=2, gold_amount=50))                  # gold pile
    assert not BlingGnomeManager._can_convert(
        DroppedItem(entity_id=3, gc_class="items.pal.x", rarity=4))  # > Rare
    assert not BlingGnomeManager._can_convert(
        DroppedItem(entity_id=4, gc_class="items.pal.mythicpal.m1", rarity=2))
    assert not BlingGnomeManager._can_convert(
        DroppedItem(entity_id=5, gc_class="items.questitempal.q1", rarity=0))


def test_drops_near_filters_zone_instance_and_radius():
    # Arrange
    inside = DroppedItem(entity_id=0xCF01, gold_amount=10, pos_x=10, pos_y=10,
                         zone_gc_type="world.town", instance_id=0)
    far = DroppedItem(entity_id=0xCF02, gold_amount=10, pos_x=900, pos_y=900,
                      zone_gc_type="world.town", instance_id=0)
    other_inst = DroppedItem(entity_id=0xCF03, gold_amount=10, pos_x=10, pos_y=10,
                             zone_gc_type="world.town", instance_id=7)
    for d in (inside, far, other_inst):
        register_drop(d)
    try:
        # Act
        found = drops_near("world.town", 0, 0.0, 0.0, 100.0)
        # Assert
        assert [eid for eid, _ in found] == [0xCF01]
    finally:
        for d in (inside, far, other_inst):
            remove_drop(d.entity_id)


# ── Follow mirror math ───────────────────────────────────────────────────────

def test_project_step_clamps_to_target():
    x, y = BlingGnomeManager._project_step(0.0, 0.0, 3.0, 4.0, 10.0)
    assert (x, y) == (3.0, 4.0)                        # step ≥ dist → arrive


def test_project_step_advances_along_ray():
    x, y = BlingGnomeManager._project_step(0.0, 0.0, 30.0, 40.0, 5.0)
    assert abs(x - 3.0) < 1e-6 and abs(y - 4.0) < 1e-6


def test_should_follow_path_settles_inside_radius():
    assert not BlingGnomeManager._should_follow_path(
        False, owner_dist_sq=20.0 ** 2, follow_dist_sq=20.0 ** 2)
    assert BlingGnomeManager._should_follow_path(
        False, owner_dist_sq=50.0 ** 2, follow_dist_sq=50.0 ** 2)
