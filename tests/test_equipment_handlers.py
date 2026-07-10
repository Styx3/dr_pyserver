"""Equipment handler integration tests — the 2026-06-10 live-bug fixes.

Covers (repo stubbed, no real DB):
  - Equip → unequip round-trips the item's stored rarity/level (the old code
    wiped slot_rarity/slot_level on every save → colored items reverted white).
  - Dual-wield: a 1H weapon equipped to off-hand slot 11 serializes
    equipSlot=11 inside the item payload (TargetSlot) — without it the client
    dropped the equip and the item vanished.
  - Level requirement enforced on equip (C# enableEquipLevelCheck).
  - Wrong-slot and 2H/off-hand conflicts are rejected (item stays in hand).
  - Spawn path: populate_equipment_from_character applies the persisted
    per-slot rarity/level.
"""
from types import SimpleNamespace

import pytest

from drserver.data.saved_character import SavedCharacter, StartingEquipment
from drserver.net import equipment as eq
from drserver.net.inventory_model import CursorItem, InventoryModel
from drserver.util.byte_io import LEReader, LEWriter


@pytest.fixture
def stub_char(monkeypatch):
    """In-memory single-character repository shared by the handlers."""
    char = SavedCharacter(id=1, name="Tester", level=50)
    char.max_hp = char.current_hp = 200 * 256
    char.max_mana = char.current_mana = 200 * 256
    char.equipment = StartingEquipment()
    monkeypatch.setattr(eq.character_repository, "get_character", lambda _id: char)
    monkeypatch.setattr(eq.character_repository, "save_character", lambda c: None)
    return char


def _conn(player_level: int = 50):
    conn = SimpleNamespace(
        conn_id=7, char_sql_id=1, player_level=player_level, login_name="Tester",
        equipment_component_id=0x0301, unit_container_id=0x0302,
        manipulators_component_id=0x0303,
        hp_wire=200 * 256, client_hp_wire=None,
        inv_model=InventoryModel(), sent=[], system_messages=[],
    )
    conn.send_to_client = conn.sent.append
    conn.send_system_message = conn.system_messages.append
    return conn


def _slot_packet(slot: int) -> LEReader:
    w = LEWriter()
    w.write_uint32(slot)
    return LEReader(w.to_array())


def _first_item_equip_slot(packet: bytes) -> int:
    """Parse the first ComponentUpdate item payload's uint32 equipSlot.

    Stream: 0x07 | 0x35 u16(cid) opcode | 0xFF cstring(gc) u32(equipSlot) ...
    """
    assert packet[0] == 0x07 and packet[1] == 0x35
    assert packet[5] == 0xFF
    end = packet.index(b"\x00", 6)
    return int.from_bytes(packet[end + 1:end + 5], "little")


def test_equip_persists_rarity_and_level_per_slot(stub_char):
    # Arrange — a Rare (tier -4) sword on the cursor with a stored level.
    conn = _conn()
    conn.inv_model.cursor = CursorItem(
        gc_class="1hsword3pal.1hsword3-4", rarity=3, stored_level=21)

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — slot maps carry the rarity/level; cursor is consumed.
    assert stub_char.equipment.weapon == "1hsword3pal.1hsword3-4"
    assert stub_char.equipment.slot_rarity["weapon"] == 3
    assert stub_char.equipment.slot_level["weapon"] == 21
    assert conn.inv_model.cursor is None
    assert len(conn.sent) == 1


def test_unequip_restores_rarity_and_level_to_cursor(stub_char):
    # Arrange — Rare sword already equipped with stored rarity/level.
    conn = _conn()
    stub_char.equipment.weapon = "1hsword3pal.1hsword3-4"
    stub_char.equipment.slot_rarity = {"weapon": 3}
    stub_char.equipment.slot_level = {"weapon": 21}

    # Act
    eq.handle_remove_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — the cursor item keeps what the slot stored.
    cursor = conn.inv_model.cursor
    assert cursor is not None and cursor.gc_class == "1hsword3pal.1hsword3-4"
    assert cursor.rarity == 3
    assert cursor.stored_level == 21
    assert stub_char.equipment.weapon is None
    assert "weapon" not in stub_char.equipment.slot_rarity


def test_equip_swap_puts_old_rarity_on_cursor(stub_char):
    # Arrange — Superior sword equipped, Rare sword on the cursor.
    conn = _conn()
    stub_char.equipment.weapon = "1hsword3pal.1hsword3-2"
    stub_char.equipment.slot_rarity = {"weapon": 1}
    stub_char.equipment.slot_level = {"weapon": 21}
    conn.inv_model.cursor = CursorItem(
        gc_class="1hsword3pal.1hsword3-4", rarity=3, stored_level=23)

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — new item persisted, old item's rarity/level ride the cursor.
    assert stub_char.equipment.slot_rarity["weapon"] == 3
    cursor = conn.inv_model.cursor
    assert cursor.gc_class == "1hsword3pal.1hsword3-2"
    assert cursor.rarity == 1
    assert cursor.stored_level == 21


def test_equip_1h_weapon_to_offhand_serializes_slot_11(stub_char):
    # Arrange — dual-wield: 1H sword into off-hand slot 11.
    conn = _conn()
    conn.inv_model.cursor = CursorItem(
        gc_class="1hsword3pal.1hsword3-2", rarity=1, stored_level=21)

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(11), 0)

    # Assert — packet item payload carries equipSlot 11, DB uses the shield slot.
    assert len(conn.sent) == 1
    assert _first_item_equip_slot(conn.sent[0]) == 11
    assert stub_char.equipment.shield == "1hsword3pal.1hsword3-2"


def test_unequip_offhand_weapon_serializes_slot_11(stub_char):
    # Arrange — 1H sword sitting in the off-hand (shield) slot.
    conn = _conn()
    stub_char.equipment.shield = "1hsword3pal.1hsword3-2"
    stub_char.equipment.slot_rarity = {"shield": 1}
    stub_char.equipment.slot_level = {"shield": 21}

    # Act
    eq.handle_remove_equipped_item(None, conn, _slot_packet(11), 0)

    # Assert — the UnitContainer set-active payload (2nd update) says slot 11.
    packet = conn.sent[0]
    # Skip update 1 (Equipment remove: 0x35 u16 0x29 u32(slot) 0x02 u32(hp)
    # = 13 bytes); update 2 (UnitContainer set-active) starts at offset 14.
    assert packet[14] == 0x35
    body = b"\x07" + packet[14:]
    assert _first_item_equip_slot(body) == 11


def test_equip_rejects_underleveled_player(stub_char):
    # Arrange — level-1 player, level-21 Rare sword.
    conn = _conn(player_level=1)
    cursor = CursorItem(gc_class="1hsword3pal.1hsword3-4", rarity=3, stored_level=21)
    conn.inv_model.cursor = cursor

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — nothing sent, item stays in hand, player told why.
    assert conn.sent == []
    assert conn.inv_model.cursor is cursor
    assert stub_char.equipment.weapon is None
    assert conn.system_messages, "player must be told the level requirement"


def test_equip_rejects_wrong_slot(stub_char):
    # Arrange — body armor into the weapon slot.
    conn = _conn()
    cursor = CursorItem(gc_class="fighterbodypal.armor1-1", rarity=0, stored_level=1)
    conn.inv_model.cursor = cursor

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — rejected, item stays in hand.
    assert conn.sent == []
    assert conn.inv_model.cursor is cursor


def test_equip_2h_blocked_while_offhand_occupied(stub_char):
    # Arrange — shield equipped, 2H sword on the cursor.
    conn = _conn()
    stub_char.equipment.shield = "fightershieldpal.shield1-1"
    cursor = CursorItem(gc_class="2hsword2pal.2hsword2-1", rarity=0, stored_level=11)
    conn.inv_model.cursor = cursor

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — blocked (C# behaviour); the old auto-unequip destroyed the shield.
    assert conn.sent == []
    assert conn.inv_model.cursor is cursor
    assert stub_char.equipment.shield == "fightershieldpal.shield1-1"


def test_equip_offhand_blocked_while_2h_equipped(stub_char):
    # Arrange — 2H staff in main hand, shield on the cursor.
    conn = _conn()
    stub_char.equipment.weapon = "2hstaffpal.2hstaff1-1"
    cursor = CursorItem(gc_class="fightershieldpal.shield1-1", rarity=0, stored_level=1)
    conn.inv_model.cursor = cursor

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(11), 0)

    # Assert
    assert conn.sent == []
    assert conn.inv_model.cursor is cursor


def test_spawn_equipment_applies_slot_rarity_and_level():
    # Arrange — saved character with a Rare weapon recorded in the slot maps.
    from drserver.data import gc_object_factory as factory
    char = SavedCharacter(id=1, name="Tester", level=50)
    char.equipment = StartingEquipment(
        weapon="1hsword3pal.1hsword3-4",
        slot_rarity={"weapon": 3}, slot_level={"weapon": 21})
    equipment = factory.new_equipment()
    manipulators = factory.new_manipulators()

    # Act
    count = factory.populate_equipment_from_character(equipment, manipulators, char)

    # Assert — the spawned item carries the stored rarity/level.
    assert count == 1
    item = equipment.children[0]
    assert item.stored_rarity == 3
    assert item.stored_level == 21


def test_equip_required_level_is_flat_minus_5(stub_char):
    """Client tooltip rule (FUN_00496640): required = level byte − 5, flat —
    NO rarity delta. The old rarity-aware formula said 9 for a Normal level-21
    item (−12) while the client displayed 16; the gate must agree with the
    tooltip."""
    # Arrange — level-10 player, Normal (rarity 0) level-21 sword: required 16.
    conn = _conn(player_level=10)
    cursor = CursorItem(gc_class="1hsword3pal.1hsword3-4", rarity=0, stored_level=21)
    conn.inv_model.cursor = cursor

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — rejected with the level the client shows (16, not 9).
    assert conn.sent == []
    assert conn.inv_model.cursor is cursor
    assert any("16" in m for m in conn.system_messages)


def test_equip_allows_at_exact_required_level(stub_char):
    # Arrange — level-16 player, level-21 item: required 21 − 5 = 16.
    conn = _conn(player_level=16)
    conn.inv_model.cursor = CursorItem(
        gc_class="1hsword3pal.1hsword3-4", rarity=3, stored_level=21)

    # Act
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — equips.
    assert len(conn.sent) == 1
    assert stub_char.equipment.weapon == "1hsword3pal.1hsword3-4"


def test_equip_unequip_round_trips_scale_mod(stub_char):
    """The ScaleMod rolled at acquire must survive equip → DB → unequip; losing
    it re-rolled the item's stats to the deterministic pick on relog."""
    # Arrange — Rare sword whose cursor carries a shop-rolled ScaleMod.
    conn = _conn()
    conn.inv_model.cursor = CursorItem(
        gc_class="1hsword3pal.1hsword3-4", rarity=3, stored_level=21,
        scale_mod="ScaleModPAL.Rare.Mod5")

    # Act — equip, then unequip the same slot.
    eq.handle_add_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — persisted per-slot.
    assert stub_char.equipment.slot_scale_mod["weapon"] == "ScaleModPAL.Rare.Mod5"

    eq.handle_remove_equipped_item(None, conn, _slot_packet(10), 0)

    # Assert — back on the cursor, slot map cleared.
    assert conn.inv_model.cursor.scale_mod == "ScaleModPAL.Rare.Mod5"
    assert "weapon" not in (stub_char.equipment.slot_scale_mod or {})
    # The unequip stream serialized the preset mod, not the deterministic one.
    assert b"ScaleModPAL.Rare.Mod5" in conn.sent[-1]


# ── Viewer relay: peers see live equip/unequip (2026-07-08 user report) ──────
def _viewer(login, zone="world.town", instance_id=0):
    v = SimpleNamespace(
        conn_id=hash(login) & 0xFF, login_name=login, is_spawned=True,
        current_zone_gc_type=zone, instance_id=instance_id, sent=[])
    v.send_to_client = v.sent.append
    return v


def _relay_server(owner, *viewers, manip_id=0x0777):
    srv = SimpleNamespace(
        connections={c.conn_id: c for c in (owner, *viewers)},
        remote_manip_ids={v.login_name: {owner.login_name: manip_id}
                          for v in viewers},
    )
    return srv


def test_equip_is_relayed_to_instance_peers_only(stub_char):
    """An equip mirrors the Manipulators visual add onto same-instance peers'
    copies (remapped manip id, empty synch); other instances get nothing."""
    conn = _conn()
    conn.is_spawned = True
    conn.current_zone_gc_type = "world.town"
    conn.instance_id = 0
    peer = _viewer("peer")
    outsider = _viewer("outsider", instance_id=5)     # other copy of the zone
    server = _relay_server(conn, peer, outsider)
    conn.inv_model.cursor = CursorItem(gc_class="1hsword3pal.1hsword3-4",
                                       rarity=0, stored_level=1)

    eq.handle_add_equipped_item(server, conn, _slot_packet(10), 0)

    assert len(peer.sent) == 1
    pkt = peer.sent[0]
    # 0x07 | 0x35 <remote manip id> 0x00 <item …> 0x00(empty synch) | 0x06
    assert pkt[0] == 0x07 and pkt[-1] == 0x06
    assert pkt[1] == 0x35
    assert int.from_bytes(pkt[2:4], "little") == 0x0777
    assert pkt[4] == 0x00                             # Manipulators visual add
    assert b"1hsword3" in pkt
    assert pkt[-2] == 0x00                            # flags-only empty synch
    assert outsider.sent == []                        # instance-scoped


def test_unequip_is_relayed_as_manipulator_remove(stub_char):
    conn = _conn()
    conn.is_spawned = True
    conn.current_zone_gc_type = "world.town"
    conn.instance_id = 0
    peer = _viewer("peer")
    server = _relay_server(conn, peer)
    conn.inv_model.cursor = CursorItem(gc_class="1hsword3pal.1hsword3-4",
                                       rarity=0, stored_level=1)
    eq.handle_add_equipped_item(server, conn, _slot_packet(10), 0)
    peer.sent.clear()

    eq.handle_remove_equipped_item(server, conn, _slot_packet(10), 0)

    assert len(peer.sent) == 1
    pkt = peer.sent[0]
    # 0x07 | 0x35 <manip> 0x01 <u32 slot> 0x00 | 0x06
    assert pkt[:2] == bytes([0x07, 0x35])
    assert int.from_bytes(pkt[2:4], "little") == 0x0777
    assert pkt[4] == 0x01                             # Manipulators remove
    assert int.from_bytes(pkt[5:9], "little") == 10
    assert pkt[9] == 0x00 and pkt[10] == 0x06


def test_swap_relays_remove_then_add(stub_char):
    conn = _conn()
    conn.is_spawned = True
    conn.current_zone_gc_type = "world.town"
    conn.instance_id = 0
    peer = _viewer("peer")
    server = _relay_server(conn, peer)
    conn.inv_model.cursor = CursorItem(gc_class="1hsword3pal.1hsword3-4",
                                       rarity=0, stored_level=1)
    eq.handle_add_equipped_item(server, conn, _slot_packet(10), 0)
    peer.sent.clear()
    conn.inv_model.cursor = CursorItem(gc_class="1hsword3pal.1hsword3-5",
                                       rarity=0, stored_level=1)

    eq.handle_add_equipped_item(server, conn, _slot_packet(10), 0)

    assert len(peer.sent) == 1
    pkt = peer.sent[0]
    assert pkt[4] == 0x01                             # remove of the old item…
    assert int.from_bytes(pkt[5:9], "little") == 10
    add_at = pkt.index(bytes([0x35, 0x77, 0x07, 0x00]), 5)  # …then the add
    assert b"1hsword3-5" in pkt[add_at:]
