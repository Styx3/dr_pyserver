"""Ground-drop + pickup tests — inventory drop (0x23) and activate-click pickup.

Regression coverage for the two 2026-06-10 live bugs:
  - Dropping an item from the inventory crashed the client with "zone
    communication error code 7" — the old ``_spawn_dropped_item`` wrote the
    item's own GC class as the entity-create type. The client's entity factory
    only accepts the world-object class ``itemobject`` (processEntityCreate
    rejects anything else); the item's class belongs inside the init body.
  - Clicking ground loot did nothing — drops were never tracked, so the
    activate (0x06) dispatch had nothing to route to. Port of the C#
    ``_droppedItems`` registry + ``HandleItemRightClickPickup`` auto-bag.

Byte layouts mirror C# InventoryHandler.HandleDropItem /
UnityGameServer.HandleItemRightClickPickup (live-proven in the C# server).
"""
import random
from types import SimpleNamespace

import pytest

from drserver.data.saved_character import SavedCharacter, SavedInventoryItem
from drserver.managers import loot, loot_roller
from drserver.managers.loot_roller import PoolItem
from drserver.net import inventory as inv
from drserver.net.inventory_model import CursorItem, InventoryModel
from drserver.util.byte_io import LEReader, LEWriter

_POTION = "potionpal.healthpotion_noob"


@pytest.fixture(autouse=True)
def clean_registry():
    loot._dropped_items.clear()
    yield
    loot._dropped_items.clear()


@pytest.fixture(autouse=True)
def _seeded_loot_rng(monkeypatch):
    """Pin the roller's RNG — boss ITEM drops are 0.75 chance (not 1.0), so the
    drop-registration test flaked order-dependently on an unseeded Random.
    These tests cover the drop REGISTRY / pickup wire, not the odds."""
    real_random = random.Random
    monkeypatch.setattr(loot_roller.random, "Random",
                        lambda *a, **k: real_random(7))


@pytest.fixture
def stub_repo(monkeypatch):
    char = SavedCharacter(id=1, name="Tester", level=1)
    char.max_hp = char.current_hp = 200 * 256
    char.max_mana = char.current_mana = 200 * 256
    char.gold = 100
    char.inventory = []
    monkeypatch.setattr(inv.character_repository, "get_character", lambda _id: char)
    monkeypatch.setattr(inv.character_repository, "save_character", lambda c: None)
    return char


def _conn(conn_id=4, zone="town", instance=1):
    conn = SimpleNamespace(
        conn_id=conn_id, char_sql_id=1, player_level=1, login_name="Tester",
        unit_container_id=0x0202, modifiers_id=0x0203,
        hp_wire=200 * 256, client_hp_wire=None,
        player_pos_x=100.0, player_pos_y=200.0, player_pos_z=10.0,
        current_zone_gc_type=zone, instance_id=instance,
        is_spawned=True,
        inv_model=InventoryModel(), sent=[],
    )
    conn.send_to_client = conn.sent.append
    return conn


def _server(*conns):
    return SimpleNamespace(
        connections={c.conn_id: c for c in conns}, quests=None,
    )


def _drop_potion(server, conn, count=1):
    conn.inv_model.cursor = CursorItem(gc_class=_POTION, count=count)
    inv.handle_drop_item(server, conn, LEReader(b""))


# ── handle_drop_item (0x23) ──────────────────────────────────────────────────

def test_drop_creates_itemobject_entity_not_item_class(stub_repo):
    """The entity-create type must be 'itemobject' — the item's own GC class
    there is the client's 'Invalid entity type' → code-7 crash."""
    # Arrange
    conn = _conn()
    server = _server(conn)

    # Act
    _drop_potion(server, conn)

    # Assert
    assert len(conn.sent) == 1
    pkt = conn.sent[0]
    r = LEReader(pkt)
    assert r.read_byte() == 0x07          # BeginStream
    # 0x29 ClearActive ack on the UnitContainer frees the cursor (C# shape).
    assert r.read_byte() == 0x35
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x29
    assert r.read_byte() == 0x02          # synch trailer
    r.read_uint32()                       # synch HP
    # Entity create — type MUST be the world-object class.
    assert r.read_byte() == 0x01
    entity_id = r.read_uint16()
    assert 0xC000 <= entity_id <= 0xFDFF  # loot entity-id block
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "itemobject"
    assert _POTION.encode() not in pkt[:pkt.index(b"itemobject")]
    # SetPosition block carries the player position ×256 (+1 Z settle bias).
    assert r.read_byte() == 0x02
    assert r.read_uint16() == entity_id
    assert r.read_uint32() == 0x00000006  # worldEntityFlags
    assert r.read_int32() == int(100.0 * 256)
    assert r.read_int32() == int(200.0 * 256)
    assert r.read_int32() == int((10.0 + 1.0) * 256)
    # The item's real class lives inside the init body.
    assert _POTION.encode() in pkt
    assert pkt[-1] == 0x06                # EndStream


def test_drop_registers_drop_and_clears_cursor(stub_repo):
    # Arrange
    conn = _conn()
    server = _server(conn)

    # Act
    _drop_potion(server, conn, count=3)

    # Assert
    assert conn.inv_model.cursor is None
    assert len(loot._dropped_items) == 1
    drop = next(iter(loot._dropped_items.values()))
    assert drop.gc_class == _POTION
    assert drop.count == 3
    assert drop.zone_gc_type == "town"
    assert drop.instance_id == 1


def test_drop_broadcasts_bare_create_to_instance_peers(stub_repo):
    # Arrange — one peer in the same instance, one in another instance.
    conn = _conn(conn_id=4)
    peer = _conn(conn_id=5)
    stranger = _conn(conn_id=6, instance=2)
    server = _server(conn, peer, stranger)

    # Act
    _drop_potion(server, conn)

    # Assert — the peer gets the create WITHOUT the ClearActive prefix.
    assert len(peer.sent) == 1
    r = LEReader(peer.sent[0])
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x01          # straight to CreateEntity
    r.read_uint16()
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "itemobject"
    assert stranger.sent == []


def test_drop_with_empty_cursor_is_a_noop(stub_repo):
    conn = _conn()
    inv.handle_drop_item(_server(conn), conn, LEReader(b""))
    assert conn.sent == []
    assert loot._dropped_items == {}


# ── handle_ground_pickup (activate 0x06 on a tracked drop) ──────────────────

def test_pickup_unknown_entity_returns_false(stub_repo):
    conn = _conn()
    assert inv.handle_ground_pickup(_server(conn), conn, 0x0101, 0xC123, 1, 2) is False


def test_pickup_item_auto_bags_into_free_slot(stub_repo):
    # Arrange — drop on the ground, empty bag.
    conn = _conn()
    server = _server(conn)
    loot.register_drop(loot.DroppedItem(
        entity_id=0xC010, gc_class=_POTION, count=2,
        pos_x=100.0, pos_y=200.0, zone_gc_type="town", instance_id=1))

    # Act
    handled = inv.handle_ground_pickup(server, conn, 0x0101, 0xC010, 7, 9)

    # Assert — handled, claimed, item in the model at the first free cell.
    assert handled is True
    assert loot.find_drop(0xC010) is None
    items = conn.inv_model.main_items()
    assert len(items) == 1 and items[0].gc_class == _POTION
    assert items[0].count == 2 and (items[0].x, items[0].y) == (0, 0)

    # Wire: ack + remove entity + defensive 0x29 + 0x1E ItemAdd.
    r = LEReader(conn.sent[0])
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0101      # UnitBehavior component the click hit
    assert r.read_byte() == 0x01          # ActionResponse
    assert r.read_byte() == 7             # responseId echoed
    assert r.read_byte() == 0x06          # BehaviourActionActivate
    assert r.read_byte() == 9             # sessionID echoed
    assert r.read_uint16() == 0xC010
    assert r.read_byte() == 0x02
    r.read_uint32()
    assert r.read_byte() == 0x05          # remove ground entity
    assert r.read_uint16() == 0xC010


def test_pickup_merges_into_existing_stack_with_0x22(stub_repo):
    # Arrange — bag already holds 4 of the same potion.
    conn = _conn()
    server = _server(conn)
    existing = conn.inv_model.add(_POTION, 0, 0, count=4)
    stub_repo.inventory = [SavedInventoryItem(gc_class=_POTION, x=0, y=0, count=4)]
    loot.register_drop(loot.DroppedItem(
        entity_id=0xC011, gc_class=_POTION, count=2,
        pos_x=100.0, pos_y=200.0, zone_gc_type="town", instance_id=1))

    # Act
    inv.handle_ground_pickup(server, conn, 0x0101, 0xC011, 1, 2)

    # Assert — merged, no new slot, 0x22 UpdateQuantity carries the new count.
    assert existing.count == 6
    assert len(conn.inv_model.main_items()) == 1
    pkt = conn.sent[0]
    r = LEReader(pkt)
    assert r.read_byte() == 0x07
    # Skip the ack (0x35 cid 0x01 resp 0x06 sid u16 eid + 0x02 u32).
    r.read_byte(); r.read_uint16(); r.read_byte(); r.read_byte()
    r.read_byte(); r.read_byte(); r.read_uint16(); r.read_byte(); r.read_uint32()
    assert r.read_byte() == 0x05
    assert r.read_uint16() == 0xC011
    assert r.read_byte() == 0x35
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x22          # UpdateQuantity
    assert r.read_uint32() == existing.slot_id
    assert r.read_byte() == 6


def test_pickup_gold_credits_character(stub_repo):
    # Arrange
    conn = _conn()
    server = _server(conn)
    loot.register_drop(loot.DroppedItem(
        entity_id=0xC012, gold_amount=37, pos_x=100.0, pos_y=200.0,
        zone_gc_type="town", instance_id=1))

    # Act
    inv.handle_ground_pickup(server, conn, 0x0101, 0xC012, 1, 2)

    # Assert — gold credited, 0x20 AddCurrency on the UnitContainer.
    assert stub_repo.gold == 137
    assert loot.find_drop(0xC012) is None
    pkt = conn.sent[0]
    r = LEReader(pkt)
    assert r.read_byte() == 0x07
    r.read_byte(); r.read_uint16(); r.read_byte(); r.read_byte()
    r.read_byte(); r.read_byte(); r.read_uint16(); r.read_byte(); r.read_uint32()
    assert r.read_byte() == 0x05
    assert r.read_uint16() == 0xC012
    assert r.read_byte() == 0x35
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x20          # AddCurrency
    assert r.read_uint32() == 37


def test_pickup_full_bag_leaves_item_on_ground_but_acks(stub_repo, monkeypatch):
    # Arrange — no free slot anywhere.
    conn = _conn()
    server = _server(conn)
    monkeypatch.setattr(inv, "_find_free_slot", lambda *_a: None)
    drop = loot.DroppedItem(entity_id=0xC013, gc_class=_POTION, count=1,
                            pos_x=100.0, pos_y=200.0,
                            zone_gc_type="town", instance_id=1)
    loot.register_drop(drop)

    # Act
    handled = inv.handle_ground_pickup(server, conn, 0x0101, 0xC013, 1, 2)

    # Assert — still on the ground, bag unchanged, action still acked
    # (no ack = client action state machine wedges).
    assert handled is True
    assert loot.find_drop(0xC013) is drop
    assert conn.inv_model.main_items() == []
    acks = [p for p in conn.sent if len(p) > 2 and p[1] == 0x35]
    assert len(acks) == 1


def test_pickup_despawns_entity_for_instance_peers(stub_repo):
    # Arrange
    conn = _conn(conn_id=4)
    peer = _conn(conn_id=5)
    server = _server(conn, peer)
    loot.register_drop(loot.DroppedItem(
        entity_id=0xC014, gc_class=_POTION, count=1,
        pos_x=100.0, pos_y=200.0, zone_gc_type="town", instance_id=1))

    # Act
    inv.handle_ground_pickup(server, conn, 0x0101, 0xC014, 1, 2)

    # Assert — peer got the bare despawn stream.
    assert peer.sent == [bytes([0x07, 0x05, 0x14, 0xC0, 0x06])]


# ── mob-loot registration ────────────────────────────────────────────────────

def test_generate_loot_registers_clickable_drops():
    # Arrange — deterministic single-item pool (DB-free) + clean registry.
    loot._dropped_items.clear()
    loot_roller._pool_cache = [PoolItem("1haxe1pal.1haxe1-1", 1)]
    conn = _conn()
    server = _server(conn)

    # Act — a Boss gold + Boss item generator (both drop on every activation,
    # chance 1.0 — default-tier GG/IG are now probabilistic).
    loot.generate_loot_for_monster(server, conn, 50.0, 60.0, 0.0, level=3,
                                   treasure_generators=[("BossGG", 1), ("BossIG", 1)],
                                   difficulty="BOSS")

    # Assert — gold pile + rolled items, each clickable (findable by entity id).
    drops = list(loot._dropped_items.values())
    gold = [d for d in drops if d.gold_amount > 0]
    items = [d for d in drops if d.gold_amount == 0]
    assert len(gold) == 1
    assert gold[0].gold_amount >= 3
    assert items and all(d.gc_class == "1haxe1pal.1haxe1-1" for d in items)
    assert items[0].scale_mod                       # rolled rarity ScaleMod present
    assert items[0].zone_gc_type == "town" and items[0].instance_id == 1
    for d in drops:
        assert loot.find_drop(d.entity_id) is d


# ── pickup range gate ────────────────────────────────────────────────────────

def test_pickup_beyond_range_acks_but_leaves_drop(stub_repo):
    """A click on a far-away drop must NOT bag it (the old behaviour grabbed
    loot from across the zone), but it must still ack the action or the
    client's action state machine wedges."""
    # Arrange — drop 500 units away from the player at (100, 200).
    conn = _conn()
    server = _server(conn)
    drop = loot.DroppedItem(entity_id=0xC020, gc_class=_POTION, count=1,
                            pos_x=600.0, pos_y=200.0,
                            zone_gc_type="town", instance_id=1)
    loot.register_drop(drop)

    # Act
    handled = inv.handle_ground_pickup(server, conn, 0x0101, 0xC020, 1, 2)

    # Assert — handled (acked), drop still on the ground, bag unchanged.
    assert handled is True
    assert loot.find_drop(0xC020) is drop
    assert conn.inv_model.main_items() == []
    acks = [p for p in conn.sent if len(p) > 2 and p[1] == 0x35]
    assert len(acks) >= 1


def test_pickup_just_inside_range_bags(stub_repo):
    # Arrange — drop 100 units away (default groundPickupRange is 150).
    conn = _conn()
    server = _server(conn)
    loot.register_drop(loot.DroppedItem(
        entity_id=0xC021, gc_class=_POTION, count=1,
        pos_x=200.0, pos_y=200.0, zone_gc_type="town", instance_id=1))

    # Act
    inv.handle_ground_pickup(server, conn, 0x0101, 0xC021, 1, 2)

    # Assert — bagged.
    assert loot.find_drop(0xC021) is None
    assert len(conn.inv_model.main_items()) == 1
