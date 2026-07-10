"""Interactive world-entity activation tests — chest open + shrine buff.

Guards the 2026-06-17 fix where clicking a chest or the boss-room obelisk
(shrine) fell through the 0x06 BehaviourActionActivate handler to a generic ack
and did nothing. Ports of C# HandleChestActivation / world-entity activate:

* chest  → NCI open-state ``0x03 <eid> 0x0A 0x00000001`` + rolled loot, one-shot.
* shrine → NCI activate ``0x03 <eid> 0x0A 0x00000000`` + the shrine's buff
  modifier on the player (the obelisk's actual effect).
"""
import _paths
from drserver.db import game_database
from drserver.managers import world_entities as we_module
from drserver.managers import player_modifiers
from drserver.managers.world_entities import WorldEntityData, world_entity_manager
from drserver.util.byte_io import LEReader

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.connections = {}


class _FakeConn:
    def __init__(self) -> None:
        self.login_name = "Tester"
        self.player_level = 5
        self.hp_wire = 200 * 256
        self.modifiers_id = 0x4321
        self.current_zone_gc_type = "world.dungeon00"
        self.instance_id = 0
        self.sent = []
        self.tracked_modifiers = {}

    def send_to_client(self, packet: bytes) -> None:
        self.sent.append(packet)


def _chest(entity_type="chest", generator="") -> WorldEntityData:
    return WorldEntityData(
        id=1, zone_name="dungeon00_level03_boss", name="BossChest",
        gc_type="terrain.interactives.loot.BossChest", entity_type=entity_type,
        pos_x=-440.0, pos_y=-700.0, pos_z=40.0, heading=180.0, floor_index=0,
        item_generator=generator, item_count=1, target_zone="",
        target_waypoint="", display_label="", flags=7,
    )


def test_registry_register_find_and_opened_tracking():
    we = _chest()
    world_entity_manager.register_entity(0xABCD, we)
    assert world_entity_manager.find_by_entity_id(0xABCD) is we
    assert world_entity_manager.find_by_entity_id(0x9999) is None
    assert not world_entity_manager.is_chest_opened(0xABCD)
    world_entity_manager.mark_chest_opened(0xABCD)
    assert world_entity_manager.is_chest_opened(0xABCD)


def test_nci_activate_open_state_bytes():
    # Chest open = state 1; shrine/generic activate = state 0. Both carry the
    # NCI EntitySynchInfo (0x02 + maxHpWire).
    for activated, want_state in ((True, 1), (False, 0)):
        pkt = we_module._build_nci_activate(0x1234, activated=activated)
        r = LEReader(pkt)
        assert r.read_byte() == 0x03            # processEntityUpdate
        assert r.read_uint16() == 0x1234
        assert r.read_byte() == 0x0A            # NonCombatInteractive update
        assert r.read_uint32() == want_state
        assert r.read_byte() == 0x02            # EntitySynchInfo: HP present
        r.read_uint32()                         # maxHpWire
        assert r.remaining == 0


def test_open_chest_sends_open_state_and_is_one_shot():
    # Empty generator isolates the open-state + idempotency from the loot roll.
    we = _chest(generator="")
    world_entity_manager.register_entity(0x2002, we)
    world_entity_manager._opened_chests.discard(0x2002)
    conn = _FakeConn()
    server = _FakeServer()

    we_module.open_chest(server, conn, 0x2002, we)
    assert world_entity_manager.is_chest_opened(0x2002)
    assert len(conn.sent) == 1                   # one NCI open-state message
    assert conn.sent[0][0] == 0x03

    # Re-clicking an opened chest must NOT send again / re-loot.
    we_module.open_chest(server, conn, 0x2002, we)
    assert len(conn.sent) == 1


def test_activate_shrine_applies_buff_and_sends_visual():
    we = _chest(entity_type="shrine")
    we = WorldEntityData(
        id=2, zone_name="dungeon00_level03_boss", name="NewbieEnduranceShrine",
        gc_type="world.dungeon00.data.NewbieEnduranceShrine", entity_type="shrine",
        pos_x=-510.0, pos_y=-600.0, pos_z=40.0, heading=90.0, floor_index=0,
        item_generator="", item_count=0, target_zone="", target_waypoint="",
        display_label="", flags=7,
    )
    conn = _FakeConn()
    server = _FakeServer()

    we_module.activate_shrine(server, conn, 0x3003, we)

    # Visual (NCI activate, state 0) + the buff modifier add were both sent.
    assert any(p[0] == 0x03 for p in conn.sent)   # NCI activate visual
    # The shrine's <gc>.Modifier is tracked on the player for zone-change resend.
    mods = player_modifiers.active_modifiers(conn)
    assert any(m.gc_type.endswith(".Modifier") for m in mods)
