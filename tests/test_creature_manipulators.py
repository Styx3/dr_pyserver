"""Per-creature manipulator resolution + the monster spawn stream's OP5 block.

The client-simulated brain attacks with the manipulators the spawn stream
declares (Melee swings the PrimaryWeapon, Ranged fires it, Caster casts its
CreatureBolt). These tests guard the data-driven resolution against the
shipped content DB and the per-kind wire layouts. All three layouts are
client-verified (2026-06-11): the manipulator reader calls readData
(vtable+0xf0) AND readState (vtable+0x100) per manipulator — ActiveSkill =
6 bytes (the C#-derived 5-byte shape missed the readState flags byte and
froze zone loads), MeleeWeapon = 15, RangedWeapon = 14.
"""
import _paths
from drserver.db import game_database
from drserver.managers import creature_manipulators as cm
from drserver.managers import monsters as m
from drserver.util.byte_io import LEReader

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 100
        self.combat = None

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


def setup_function(_fn):
    cm.clear_cache()


def test_melee_creature_resolves_its_base_weapon():
    # Arrange / Act — the warg pup authors one melee PrimaryWeapon (ID 10).
    entries = cm.manipulators_for("creatures.forestcreatures.warg.basic.pup")

    # Assert — weapon_range carries the authored content Range (melee = 8).
    assert entries == [cm.ManipulatorEntry(
        "creatures.base.weapons.melee", 10, cm.KIND_MELEE, weapon_range=8.0)]


def test_nested_ranged_weapon_is_emitted_as_ranged():
    # The orok hunter's bow is a per-creature NESTED content object
    # (creatures.oroks.OrokHunter.base.OrokHunterBow → … → RangedWeapon).
    # The client registry lazily loads any content gc by name, so it is sent
    # as the authored ranged weapon (live-proven 2026-06-11 via the puker
    # rifles in dungeon05).
    entries = cm.manipulators_for("creatures.oroks.orokhunter.divine.grunt")

    ranged = [e for e in entries if e.kind == cm.KIND_RANGED]
    assert len(ranged) == 1
    assert "orokhunter" in ranged[0].gc_type.lower()


def test_nested_bolt_skill_is_emitted():
    # The whisker shaman's only manipulator is a per-creature nested
    # CreatureBolt (creatures.whiskers.shaman.skills.bolt.* → ActiveSkill).
    # Casters ship a skill-only list — legal since the ActiveSkill body
    # carries its trailing readState flags byte.
    entries = cm.manipulators_for("creatures.whiskers.shaman.fire.grunt")

    assert any(e.kind == cm.KIND_SKILL and ".skills.bolt." in e.gc_type
               for e in entries)


def test_top_level_skill_is_kept():
    # Top-level skill-library archetypes (skills.*) ride next to the weapon
    # (the blademaster casts skills.creature.CreatureChargeKnockback).
    entries = cm.manipulators_for("creatures.whiskers.blademaster.basic.grunt")

    assert cm.ManipulatorEntry(
        "skills.creature.CreatureChargeKnockback", 0, cm.KIND_SKILL) in entries
    assert cm.ManipulatorEntry(
        "creatures.base.weapons.melee", 10, cm.KIND_MELEE,
        weapon_range=8.0) in entries


def test_non_manipulator_rows_are_skipped():
    # The warg pinata authors attribmod1 = AttributeModifier — NOT a
    # Manipulator: the client bless check (FUN_004fd050 vs the Manipulator
    # class bitmask) would reject it and leave its body unread, desyncing
    # the stream. Only the melee weapon may go out.
    entries = cm.manipulators_for("creatures.forestcreatures.warg.basic.pinata")

    assert entries == [cm.ManipulatorEntry(
        "creatures.base.weapons.melee", 10, cm.KIND_MELEE, weapon_range=8.0)]


def test_special_slot_skills_root_in_activeskill_and_emit():
    # Slots beyond skill1/skill2 (specialattack, charge, aura, primaryskill,
    # summon traps…) all root in ActiveSkill and ship as skill blocks; the
    # unresolvable proc row (onhitmelee) is dropped.
    entries = cm.manipulators_for("creatures.mutants.spitter_melee.shadow.boss")

    assert any(e.kind == cm.KIND_SKILL and "CombatOne" in e.gc_type
               for e in entries)
    assert all("proc" not in e.gc_type.lower() for e in entries)


def test_unknown_creature_falls_back_to_generic_melee():
    # Arrange / Act
    entries = cm.manipulators_for("creatures.nonexistent.thing")

    # Assert — the pre-refactor default loadout (correct for plain melee units).
    assert entries == [cm.ManipulatorEntry(
        cm.DEFAULT_MELEE_GC_TYPE, cm.DEFAULT_MELEE_ID, cm.KIND_MELEE,
        weapon_range=cm.DEFAULT_MELEE_RANGE)]


def _manipulators_block(packet: bytes) -> LEReader:
    marker = b"\xffmanipulators\x00"
    start = packet.find(marker)
    assert start != -1
    r = LEReader(packet[start + len(marker):])
    assert r.read_byte() == 0x01  # component "1"
    return r


def test_spawn_stream_writes_ranged_weapon_body(monkeypatch):
    # Arrange — force a single ranged entry so the wire layout is deterministic.
    server = _FakeServer()
    monkeypatch.setattr(
        cm, "manipulators_for",
        lambda gc: [cm.ManipulatorEntry("creatures.x.Bow", 10, cm.KIND_RANGED,
                                        weapon_range=90.0)])
    m.monster_manager.load()

    # Act
    built = m.build_monsters_from_spawns(
        server, "dungeon00_level01", "world.dungeon00",
        [("world.dungeon00.mob.melee01.rank1",
          "creatures.forestcreatures.warg.basic.pup", 0.0, 0.0, 0.0, 0.0)])
    _eid, packet = built[0]

    # Assert — RangedWeapon body: <id:u32> 6×0x00 <u16> <u16> (no melee byte).
    # The readData bytes stay ALL ZERO — putting the authored Range in the
    # 4th (+0x7f) was live-disproven 2026-06-11 (rifle mobs started charging).
    r = _manipulators_block(packet)
    assert r.read_byte() == 0x01                 # count
    assert r.read_byte() == 0xFF                 # GCType tag
    assert r.read_cstring() == "creatures.x.Bow"
    assert r.read_uint32() == 10
    for _ in range(6):
        assert r.read_byte() == 0x00             # +0x80/81/82/7f, flags, mods
    assert r.read_uint16() == 0x0000
    assert r.read_uint16() == 0x0000
    # Next opcode is the modifiers component (0x32) — body length is exact.
    assert r.read_byte() == 0x32


def test_spawn_stream_writes_skill_then_weapon(monkeypatch):
    # Arrange — caster loadout: bolt skill + melee fallback weapon.
    server = _FakeServer()
    monkeypatch.setattr(
        cm, "manipulators_for",
        lambda gc: [cm.ManipulatorEntry("creatures.x.skills.bolt.Fire", 0,
                                        cm.KIND_SKILL),
                    cm.ManipulatorEntry("creatures.base.weapons.melee", 10,
                                        cm.KIND_MELEE, weapon_range=8.0)])
    m.monster_manager.load()

    # Act
    built = m.build_monsters_from_spawns(
        server, "dungeon00_level01", "world.dungeon00",
        [("world.dungeon00.mob.melee01.rank1",
          "creatures.forestcreatures.warg.basic.pup", 0.0, 0.0, 0.0, 0.0)])
    _eid, packet = built[0]

    # Assert — ActiveSkill body: <id:u32> 0x00 <flags:0x00> (the readState
    # flags byte is what the 2026-06-11 freeze fix added); then the
    # live-proven melee body.
    r = _manipulators_block(packet)
    assert r.read_byte() == 0x02                 # count
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "creatures.x.skills.bolt.Fire"
    assert r.read_uint32() == 0
    assert r.read_byte() == 0x00                 # readData trailer
    assert r.read_byte() == 0x00                 # readState flags
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "creatures.base.weapons.melee"
    assert r.read_uint32() == 10
    for _ in range(6):
        assert r.read_byte() == 0x00             # +0x80/81/82/7f, flags, mods
    assert r.read_uint16() == 0x0000
    assert r.read_byte() == 0x00                 # melee-only +0x8d
    assert r.read_uint16() == 0x0000
    assert r.read_byte() == 0x32                 # modifiers component follows
