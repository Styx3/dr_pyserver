"""Summon-unit tests (Monster Bait / Build Snowman) — managers/summons.py.

The create stream reuses the LIVE-PROVEN monster spawn shapes (monsters.py
OP1–OP8, MonsterBehavior2 behavior body) with the DRS-NET-proven owner-bearing
Unit init. These tests pin that layout and the skill→summon routing.
"""
import _paths  # noqa: F401

from drserver.managers import summons as sm
from drserver.managers.bling_gnome import henchman_hp_wire
from drserver.managers.summons import (MONSTER_BAIT, SNOWMAN, SummonManager,
                                       SummonState, def_for_skill)
from drserver.util.byte_io import LEReader


def _state(d) -> SummonState:
    return SummonState(entity_id=0x7001, behavior_id=0x7002,
                       owner_login="tester", summon_def=d,
                       zone_gc_type="world.town", instance_id=0,
                       spawned_at=0.0)


def test_def_for_skill_routes_bait_and_snowman_only():
    assert def_for_skill("skills.generic.SummonMonsterBait") is MONSTER_BAIT
    assert def_for_skill("skills.generic.SummonSnowMan") is SNOWMAN
    assert def_for_skill("SKILLS.GENERIC.SUMMONSNOWMAN") is SNOWMAN
    assert def_for_skill("skills.generic.FireBolt") is None
    assert def_for_skill(None) is None


def test_henchman_hp_scales_with_max_health():
    # bait MaxHealth 0.9 = 3× the gnome's 0.3 scale at the same curve point
    assert henchman_hp_wire(1, 0.9) > henchman_hp_wire(1, 0.3)
    # 245 × 0.9 fixed-point = 220 HP on the wire
    assert henchman_hp_wire(1, 0.9) == 220 * 256


def test_summon_spawn_packet_layout():
    # Arrange
    st = _state(MONSTER_BAIT)
    hp = henchman_hp_wire(10, MONSTER_BAIT.max_health)

    # Act
    pkt = SummonManager.build_summon_spawn_packet(
        st, MONSTER_BAIT, level=10, hp_wire=hp, owner_entity_id=0x0042,
        pos_x=100.0, pos_y=200.0, pos_z=8.0,
        skills_id=0x7003, manipulators_id=0x7004, modifiers_id=0x7005)
    r = LEReader(pkt)

    # Assert — OP1 create
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x01
    assert r.read_uint16() == st.entity_id
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == MONSTER_BAIT.unit_gc_type

    # OP2 init: entity block + owner-bearing unit block
    assert r.read_byte() == 0x02
    assert r.read_uint16() == st.entity_id
    assert r.read_uint32() == 0x06
    assert r.read_int32() == 100 * 256
    assert r.read_int32() == 200 * 256
    assert r.read_int32() == 8 * 256
    r.read_int32()                                 # heading
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x17                   # unitFlags 0x16 | owner
    assert r.read_byte() == 10                     # level
    r.read_uint16(); r.read_uint16()
    assert r.read_uint16() == 0x0042               # owner ref
    assert r.read_uint32() == hp
    assert r.read_uint32() == 0                    # mana

    # skip StockUnit zero tail (1+1+2+2+1+2+4+1+4+4+4 = 26 bytes)
    r.read_bytes(26)

    # OP3 behavior: typed <unit>.Behavior
    assert r.read_byte() == 0x32
    assert r.read_uint16() == st.entity_id
    assert r.read_uint16() == st.behavior_id
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == f"{MONSTER_BAIT.unit_gc_type}.Behavior"

    # tail: SpawnAction + MoverUpdate on the behavior cid, then EndStream
    assert pkt[-1] == 0x06
    assert pkt.count(b"\x35" + st.behavior_id.to_bytes(2, "little")) >= 2


def test_summon_spawn_packet_carries_melee_weapon_manipulator():
    # The snowman MUST declare its melee PrimaryWeapon (base melee, ID 10) or
    # the owner's client brain follows but never fights (2026-06-12 live bug).
    st = _state(SNOWMAN)
    pkt = SummonManager.build_summon_spawn_packet(
        st, SNOWMAN, level=5, hp_wire=256, owner_entity_id=0,
        pos_x=0.0, pos_y=0.0, pos_z=0.0,
        skills_id=3, manipulators_id=4, modifiers_id=5)

    idx = pkt.find(b"manipulators\x00")
    assert idx > 0
    body = pkt[idx + len("manipulators") + 1:]
    assert body[0] == 0x01                          # init flag
    assert body[1] >= 1                             # at least one manipulator
    assert b"creatures.base.weapons.melee\x00" in body


def test_summon_lifespans_bait_timed_snowman_client_melt():
    # Bait despawns server-side at its authored 30 s; the snowman's death is
    # the client-simulated melt — NO server lifespan.
    assert MONSTER_BAIT.lifespan == 30.0
    assert SNOWMAN.lifespan is None


def test_summon_spawn_packet_no_owner_omits_ref():
    st = _state(SNOWMAN)
    with_owner = SummonManager.build_summon_spawn_packet(
        st, SNOWMAN, 5, 256, 0x0042, 0.0, 0.0, 0.0, 3, 4, 5)
    without = SummonManager.build_summon_spawn_packet(
        st, SNOWMAN, 5, 256, 0, 0.0, 0.0, 0.0, 3, 4, 5)
    assert len(with_owner) - len(without) == 2
