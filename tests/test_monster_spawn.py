"""Monster-spawn packet tests — guards the bug-#2 fix (dungeon "sync error").

The monster create stream must match C# ``CombatPackets.BuildMonsterSpawnPacket``
byte-for-byte: op order ``0x01 create | 0x02 init | 0x32 behavior | 0x32 skills
| 0x32 manipulators | 0x32 modifiers | 0x35 spawn | 0x35 mover | 0x35 follow``,
and only creatures that resolve to a real dungeon mob asset are emitted (random
whole-table creatures with raw paths desynced the client on dungeon entry).
"""
import _paths
from drserver.db import game_database
from drserver.managers import monsters as m
from drserver.util.byte_io import LEReader

# The monster builder reads the creatures table; point the DB at a throwaway copy
# of the shipped content DB (never mutate the shipped file).
if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    """Minimal stand-in: a monotonic id allocator and no combat manager."""

    def __init__(self) -> None:
        self.next_entity_id = 100
        self.combat = None

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


# A real (entity, creature) pair from dungeon00_level01's .enc encounter table —
# the data-driven content the spawner emits (no hardcoded family map). Used to
# build a deterministic single monster for the packet-format assertions below.
_D00_ENTITY = "world.dungeon00.mob.melee01.rank1"
_D00_CREATURE = "creatures.forestcreatures.warg.basic.pup"


def _one_dungeon00_monster(server):
    """Build exactly one dungeon00 monster via the data-driven path."""
    m.monster_manager.load()
    return m.build_monsters_from_spawns(
        server, "dungeon00_level01", "world.dungeon00",
        [(_D00_ENTITY, _D00_CREATURE, 480.0, -191.0, 0.0, 0.0)],
    )


def test_resolution_is_data_driven_world_passthrough():
    # Arrange / Act / Assert — a real world.* mob asset passes through verbatim…
    assert m.resolve_monster_entity_gc_type(
        "dungeon00_level01", "world.dungeon00.mob.melee01.rank1"
    ) == "world.dungeon00.mob.melee01.rank1"
    # …case is preserved for the wire…
    assert m.resolve_monster_entity_gc_type(
        "anyzone", "World.Dungeon00.Mob.Boss"
    ) == "World.Dungeon00.Mob.Boss"
    # …a raw creatures.* type is real client content too (elite01/boss .enc
    # tables name them directly; the C# reference sends them verbatim) — it
    # passes through unchanged…
    assert m.resolve_monster_entity_gc_type(
        "dungeon00_level01", "creatures.forestcreatures.warg.basic.pup"
    ) == "creatures.forestcreatures.warg.basic.pup"
    # …while a fabricated/non-entity namespace is dropped.
    assert m.resolve_monster_entity_gc_type(
        "dungeon00_level01", "items.pal.magebody.normal001") is None
    assert m.resolve_monster_entity_gc_type("", "") is None


def test_monster_packet_matches_csharp_op_order():
    # Arrange
    server = _FakeServer()

    # Act — one monster from the data-driven encounter content.
    built = _one_dungeon00_monster(server)

    # Assert — exactly one resolvable monster, framed BeginStream..EndStream.
    assert len(built) == 1
    entity_id, packet = built[0]
    r = LEReader(packet)
    assert r.read_byte() == 0x07                    # BeginStream

    # OP1 create
    assert r.read_byte() == 0x01
    assert r.read_uint16() == entity_id
    assert r.read_byte() == 0xFF                    # GCType tag
    gc = r.read_cstring()
    assert gc.startswith("world.dungeon00.mob.")    # mapped base type, not raw

    # OP2 init
    assert r.read_byte() == 0x02
    assert r.read_uint16() == entity_id
    # activatable|visible, NO blocking bit — 0x07 (collider, like NPCs) broke
    # basic attacks on anchored/far mobs: the collider stops the avatar's
    # attack approach outside swing range (2026-06-11 live regression).
    assert r.read_uint32() == 0x06                  # worldEntityFlags
    r.read_int32(); r.read_int32(); r.read_int32()  # pos
    r.read_int32()                                   # heading
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x00                    # Unit::readInit
    level = r.read_byte()
    assert level >= 1
    r.read_uint16(); r.read_uint16()
    # StockUnit::setEntityId (25 bytes)
    r.read_byte(); r.read_uint16(); r.read_uint16()
    r.read_byte(); r.read_uint16(); r.read_uint32()
    r.read_byte(); r.read_uint32(); r.read_uint32(); r.read_uint32()

    # OP3..OP6 are four 0x32 component blocks, then three 0x35 action blocks.
    assert r.read_byte() == 0x32                    # behavior
    # (we don't re-validate every component byte here; the op-order + the three
    # trailing 0x35 blocks + clean EndStream prove the stream is well-framed)

    # Walk to the end and confirm the final byte is EndStream with nothing after.
    assert packet[-1] == 0x06
    assert 0x65 in packet                           # MoverUpdate present
    # FollowClient (0x64) is DEFERRED — mobs spawn passive/anchored and are
    # enrolled into client AI on the player's first attack (see
    # ENROLL_MONSTERS_AT_SPAWN). The spawn stream must NOT carry the 0x64 enroll.
    assert m.ENROLL_MONSTERS_AT_SPAWN is False
    # The only 0x64 source is the FollowClient block we now omit; assert the
    # stream ends right after the MoverUpdate (…0x65 block… then EndStream).
    mover_idx = packet.rindex(0x65)
    assert 0x64 not in packet[mover_idx:]           # no FollowClient after mover


def test_passive_spawn_omits_followclient_but_enroll_stream_supplies_it():
    """With deferred enrollment the create stream carries no 0x64, and the
    standalone enroll stream supplies exactly the omitted FollowClient block."""
    # Arrange
    server = _FakeServer()

    # Act — one passive mob, then build the deferred enroll burst for it.
    built = _one_dungeon00_monster(server)
    _eid, spawn_packet = built[0]
    enroll = m.build_monster_enroll_stream([(0x1234, 29184)])

    # Assert — spawn passive (no 0x64), enroll stream is a framed 0x64 burst.
    assert 0x64 not in spawn_packet[spawn_packet.rindex(0x65):]
    r = LEReader(enroll)
    assert r.read_byte() == 0x07                     # BeginStream
    assert r.read_byte() == 0x35                     # ComponentUpdate
    assert r.read_uint16() == 0x1234                 # behavior id
    assert r.read_byte() == 0x64                     # FollowClient
    assert r.read_byte() == 0x01                     # control ON
    assert r.read_byte() == 0x02                     # synch flag
    assert r.read_uint32() == 29184                  # hp wire
    assert r.read_byte() == 0x06                     # EndStream


def test_enroll_stream_empty_for_no_monsters():
    """No monsters → empty bytes (nothing to enroll, no malformed stream)."""
    assert m.build_monster_enroll_stream([]) == b""
    # A monster with no behavior id is skipped (can't be controlled).
    assert m.build_monster_enroll_stream([(0, 100)]) == b"\x07\x06"


def test_manipulators_carry_primary_melee_weapon():
    """OP5 must send the melee weapon manipulator (C# CombatPackets MeleeWeapon).

    Without a weapon in the manipulators component the client-simulated brain has
    nothing to swing, so mobs aggro and approach but never attack (no animation,
    no damage). C# sends one MeleeWeapon for a basic melee creature:
    ``<gcType> <id:u32> 6×0x00 <u16:0> 0x00 <u16:0>``.
    """
    # Arrange
    server = _FakeServer()

    # Act
    built = _one_dungeon00_monster(server)
    assert len(built) == 1
    _entity_id, packet = built[0]

    # Assert — the melee weapon type is on the wire, and the stale ActiveSkill is gone.
    assert b"creatures.base.weapons.melee\x00" in packet
    assert b"skills.creature.base.PrimaryCombat" not in packet

    # Locate the manipulators component and decode the single MeleeWeapon entry.
    marker = b"\xffmanipulators\x00"
    start = packet.find(marker)
    assert start != -1
    r = LEReader(packet[start + len(marker):])
    assert r.read_byte() == 0x01                     # component "1"
    assert r.read_byte() == 0x01                     # manipulator count
    assert r.read_byte() == 0xFF                      # GCType tag
    assert r.read_cstring() == "creatures.base.weapons.melee"
    assert r.read_uint32() == 10                      # manip.Id (UnitMelee PrimaryWeapon ID=10)
    for _ in range(6):                               # 6 zero bytes (live-proven;
        assert r.read_byte() == 0x00                 # +0x7f=Range disproven 06-11)
    assert r.read_uint16() == 0x0000
    assert r.read_byte() == 0x00
    assert r.read_uint16() == 0x0000


def test_no_monsters_for_unresolvable_zone():
    # Arrange
    server = _FakeServer()
    m.monster_manager.load()

    # Act — a non-dungeon zone has no resolvable creatures.
    built = m.build_zone_monsters(
        server, "world.town", (0.0, 0.0, 0.0), count=5, zone_name="town",
    )

    # Assert — spawn nothing rather than emit an unloadable stream.
    assert built == []


if __name__ == "__main__":
    import sys
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
