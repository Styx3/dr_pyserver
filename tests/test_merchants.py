"""Merchant (vendor) subsystem tests — importer, pricing, wire shapes, buy/sell.

Byte layouts mirror C# MerchantManager (live-proven against the real client);
tab/stock configuration is pinned to the client ``.gc`` ground truth imported
by ``drserver/data/merchants_importer.py`` (NOT the C# override layer — see the
module docstring of ``drserver/managers/merchants.py``).
"""
from types import SimpleNamespace

import pytest

from _paths import copy_shipped_db, has_shipped_db
from drserver.data import rarity_helper
from drserver.data.merchants_importer import parse_merchant_block
from drserver.data.rarity_helper import ItemRarity
from drserver.data.saved_character import SavedCharacter
from drserver.net import inventory as inv
from drserver.net.inventory_model import CursorItem, InventoryModel
from drserver.util.byte_io import LEReader, LEWriter

pytestmark = pytest.mark.skipif(not has_shipped_db(), reason="shipped DB missing")

_V1 = "world.town.npc.VendorWeapon1"
_POTION_VENDOR = "world.town.npc.VendorPotion1"
_HERMIT = "world.tutorial.npc.HermitVendor"


@pytest.fixture(autouse=True, scope="module")
def _db():
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    yield


@pytest.fixture()
def manager():
    from drserver.managers.merchants import merchant_manager
    merchant_manager.reset()
    merchant_manager.load()
    yield merchant_manager
    merchant_manager.reset()


@pytest.fixture
def stub_repo(monkeypatch):
    from drserver.managers import merchants as merch_mod
    char = SavedCharacter(id=1, name="Tester", level=10)
    char.max_hp = char.current_hp = 200 * 256
    char.gold = 100_000
    char.inventory = []
    monkeypatch.setattr(merch_mod.character_repository, "get_character",
                        lambda _id: char)
    monkeypatch.setattr(merch_mod.character_repository, "save_character",
                        lambda c: None)
    return char


def _conn(conn_id=4):
    conn = SimpleNamespace(
        conn_id=conn_id, char_sql_id=1, player_level=10, login_name="Tester",
        unit_container_id=0x0202, hp_wire=200 * 256, client_hp_wire=None,
        inv_model=InventoryModel(), sent=[],
        active_merchant_npc=None, active_merchant_cid=0, active_merchant_due=0.0,
    )
    conn.send_to_client = conn.sent.append
    return conn


def _server(*conns):
    return SimpleNamespace(
        connections={c.conn_id: c for c in conns}, quests=None,
        merchant_components={}, npc_merchant_cids={},
    )


# ── merchants_importer: .gc parse ────────────────────────────────────────────

_VENDOR_GC = """
VendorTest extends npc.BigGuy.Basic.Default
{
\tName = Vendor1_Test;
\tDescription
\t{
\t\tLabel = "Test the Merchant";
\t}

\tMerchant extends Merchant
\t{
\t\tSellValueMod = 1.0;
\t\tBuyValueMod = 6.22;

\t\tWeapons extends MerchantInventory
\t\t{
\t\t\tID = 1;
\t\t\tStaticContents = false;
\t\t\tAutoGenerateItems = true;
\t\t\tItemGenerator = MerchantWeaponIG;
\t\t\tMinItemLevel = 6;
\t\t\tMaxItemLevel = 20;
\t\t\tstatic Description extends InventoryDesc
\t\t\t{
\t\t\t\tLabel = "Scrap Heap";
\t\t\t\tWidth = 10;
\t\t\t\tHeight = 14;
\t\t\t}
\t\t}

\t\tMisc extends MerchantInventory
\t\t{
\t\t\tID = 2;
\t\t\tStaticContents = true;
\t\t\tAutoGenerateItems = false;
\t\t\tItemGenerator = "";
\t\t\tstatic Description extends InventoryDesc
\t\t\t{
\t\t\t\tLabel = "Consumables";
\t\t\t\tWidth = 10;
\t\t\t\tHeight = 10;
\t\t\t}
\t\t\t* extends items.consumables.Consumable_MinorHealthPotion
\t\t\t{
\t\t\t\tInventoryX = 0;
\t\t\t\tInventoryY = 2;
\t\t\t\tID = 255;
\t\t\t}
\t\t\t* extends items.consumables.Consumable_TownPortal
\t\t\t{
\t\t\t\tInventoryX = 1;
\t\t\t\tInventoryY = 4;
\t\t\t\tQuantity = 5;
\t\t\t\tID = 260;
\t\t\t}
\t\t}
\t}
}
"""


def test_importer_parses_vendor_gc_block():
    # Act
    md = parse_merchant_block(_VENDOR_GC, "world.town.npc.VendorTest", "VendorTest")

    # Assert — per-vendor mods + authored tab config survive verbatim
    assert md is not None
    assert md.buy_value_mod == pytest.approx(6.22)
    assert md.sell_value_mod == pytest.approx(1.0)
    assert len(md.inventories) == 2
    weapons, misc = md.inventories
    assert weapons.inv_id == 1
    assert weapons.auto_generate and not weapons.static_contents
    assert weapons.item_generator == "MerchantWeaponIG"
    assert (weapons.min_item_level, weapons.max_item_level) == (6, 20)
    assert weapons.label == "Scrap Heap"          # 'static' prefix must parse
    assert (weapons.width, weapons.height) == (10, 14)
    assert misc.static_contents and not misc.auto_generate
    assert misc.label == "Consumables"


def test_importer_parses_static_items_with_authored_ids():
    # Act
    md = parse_merchant_block(_VENDOR_GC, "world.town.npc.VendorTest", "VendorTest")

    # Assert — authored coordinates, GC-baked ids (255+) and quantities
    items = md.inventories[1].items
    assert [(i.gc_type, i.inventory_x, i.inventory_y, i.item_id, i.quantity)
            for i in items] == [
        ("items.consumables.Consumable_MinorHealthPotion", 0, 2, 255, 1),
        ("items.consumables.Consumable_TownPortal", 1, 4, 260, 5),
    ]


def test_importer_returns_none_for_non_vendor():
    assert parse_merchant_block("Guard extends npc.Base {\n}\n", "x", "Guard") is None


# ── Pricing (exact Fixed32 client math) ─────────────────────────────────────

def test_sell_price_matches_client_ttd_trace():
    """C# TTD-verified case: Mythic shield GV=2.5 at playerLevel=41 → 45,444."""
    assert rarity_helper.calculate_sell_price(
        100, 2.5, ItemRarity.Mythic, True, 41) == 45444


def test_buy_price_fixed32_chain():
    # level 20 Rare (delta -5 → 15), GV 1.0: 50*15*256*520/65536 = 1523
    assert rarity_helper.calculate_buy_price(20, ItemRarity.Rare, 1.0) == 1523


def test_equip_required_level_uses_rarity_delta():
    assert rarity_helper.get_equip_required_level(41, ItemRarity.Unique) == 39
    assert rarity_helper.get_equip_required_level(5, ItemRarity.Normal) == 1


def test_deterministic_scale_mod_is_stable():
    a = rarity_helper.get_deterministic_scale_mod("1haxe1pal.1haxe1-4", ItemRarity.Rare)
    b = rarity_helper.get_deterministic_scale_mod("1haxe1pal.1haxe1-4", ItemRarity.Rare)
    assert a == b and a.startswith("ScaleModPAL.Rare.")


# ── DB faithfulness (client .gc ground truth, not the C# overrides) ─────────

def test_vendor_weapon1_tabs_match_client_gc(manager):
    md = manager.get_by_npc(_V1)
    assert md is not None
    assert md.buy_value_mod == pytest.approx(6.22)
    tabs = [(t.inv_id, t.label, t.item_generator, t.min_item_level,
             t.max_item_level) for t in md.inventories]
    assert tabs == [
        (1, "Weapons", "MerchantWeaponIG", 6, 20),
        (2, "Armor", "MerchantArmorIG", 6, 20),
        (3, "Scrap Heap", "MerchantTrashIG", 3, 22),
    ]


def test_hermit_vendor_matches_client_gc(manager):
    md = manager.get_by_npc(_HERMIT)
    tab2 = md.inventories[1]
    # The legacy DB had MerchantTrashIG 1-100 here; the client authors
    # MerchantSuperiorIG 3-10.
    assert tab2.item_generator == "MerchantSuperiorIG"
    assert (tab2.min_item_level, tab2.max_item_level) == (3, 10)
    quest_tab = md.inventories[2]
    assert quest_tab.server_sends_items
    assert all(i.item_id >= 500 for i in quest_tab.items)


def test_dungeon_vendors_imported(manager):
    """The client ships in-dungeon vendors the C# port never knew about."""
    md = manager.get_by_npc("world.dungeon02.npc.Vendor1")
    assert md is not None
    assert md.inventories[0].item_generator == "MerchantRandomIG"
    assert (md.inventories[0].min_item_level,
            md.inventories[0].max_item_level) == (10, 25)


# ── Merchant component wire shape ────────────────────────────────────────────

def test_potion_vendor_component_bytes(manager):
    # Act
    w = LEWriter()
    assert manager.write_merchant_component(w, _POTION_VENDOR, 0x1111, 0x2222)
    data = w.to_array()

    # Assert — header
    r = LEReader(data)
    assert r.read_byte() == 0x32
    assert r.read_uint16() == 0x1111            # NPC entity
    assert r.read_uint16() == 0x2222            # merchant component
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "merchant"
    assert r.read_byte() == 0x01                # hasInit
    # Init payload
    assert r.read_uint32() == 0x000000FF
    assert r.read_uint32() == 0x00000000
    assert r.read_byte() == 1                   # one tab
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "world.town.npc.vendorpotion1.merchant.misc"
    assert r.read_byte() == 1                   # inv id
    assert r.read_byte() == 0x00                # static → client loads from GC
    # Reset-timer trailer: all-static vendor has no countdown
    assert r.read_byte() == 0x01
    assert r.read_uint16() == 0x0000
    assert r.read_uint16() == 0x000F
    assert r.remaining == 0


def test_hermit_component_quest_tab_server_sent(manager):
    w = LEWriter()
    assert manager.write_merchant_component(w, _HERMIT, 0x1111, 0x2223)
    r = LEReader(w.to_array())
    r.read_byte(); r.read_uint16(); r.read_uint16()
    r.read_byte(); r.read_cstring(); r.read_byte()
    r.read_uint32(); r.read_uint32()
    assert r.read_byte() == 3
    # Tab 1 (static consumables): client-loaded
    assert r.read_byte() == 0xFF
    r.read_cstring()
    assert r.read_byte() == 1
    assert r.read_byte() == 0x00
    # Tab 2 (dynamic, shipped EMPTY in the cached stream)
    assert r.read_byte() == 0xFF
    r.read_cstring()
    assert r.read_byte() == 2
    assert r.read_byte() == 0x00
    # Tab 3 (quest items): server-sent, bare Item::readData, unique id 500
    assert r.read_byte() == 0xFF
    r.read_cstring()
    assert r.read_byte() == 3
    assert r.read_byte() == 0x01
    assert r.read_uint32() == 500
    r.read_byte(); r.read_byte()                # x, y
    assert r.read_byte() == 1                   # quantity
    assert r.read_byte() == 1                   # level
    assert r.read_byte() == 0x00                # flags
    assert r.read_byte() == 0x00                # ItemModifier child count
    assert r.read_byte() == 0x00                # Phase 2: no extra items
    # Reset trailer: full native interval (0x2328 ticks = 300s)
    assert r.read_byte() == 0x01
    assert r.read_uint16() == 0x2328
    assert r.read_uint16() == 0x000F


# ── Dynamic stock generation ─────────────────────────────────────────────────

def test_generate_respects_ig_and_level_filters(manager):
    md = manager.get_by_npc(_V1)
    manager.ensure_inventory_for_level(_V1, 10)

    weapons, armor, scrap = md.inventories
    assert weapons.items, "weapons tab generated empty"
    # Legacy dash pool (the proven-working baseline): MerchantWeaponIG stocks
    # Rare always + Unique gated (tier 4/5 dash suffix).
    for item in weapons.items:
        tier = rarity_helper.get_tier_from_gc_type(item.gc_type)
        assert tier in (4, 5)
        assert 6 <= item.level <= 20            # authored level window
    # MerchantTrashIG ("Scrap Heap"): Superior + Magic (tier 2/3).
    for item in scrap.items:
        tier = rarity_helper.get_tier_from_gc_type(item.gc_type)
        assert tier in (2, 3)
        assert 3 <= item.level <= 22

    # ids unique across the whole merchant and clear of the static range
    ids = [i.item_id for t in md.inventories for i in t.items]
    assert len(ids) == len(set(ids))
    assert all(i >= 263 for i in ids)


def test_stock_add_packet_shape(manager):
    manager.ensure_inventory_for_level(_V1, 10)
    pkt = manager.build_stock_add_packet(_V1, 0x5678)
    assert pkt is not None
    assert pkt[0] == 0x07 and pkt[-1] == 0x06
    r = LEReader(pkt)
    r.read_byte()
    # First add-update block
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x5678
    assert r.read_byte() == 0x1E
    inv_id = r.read_byte()
    assert inv_id in (1, 2, 3)
    assert r.read_byte() == 0xFF                # item gc type tag


def test_ensure_inventory_regenerates_only_on_level_change(manager):
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    before = [i.item_id for i in md.inventories[0].items]
    assert manager.ensure_inventory_for_level(_V1, 10) is False
    assert [i.item_id for i in md.inventories[0].items] == before
    assert manager.ensure_inventory_for_level(_V1, 55) is True
    assert [i.item_id for i in md.inventories[0].items] != before


# ── Buy ──────────────────────────────────────────────────────────────────────

def _buy_reader(item_id: int) -> LEReader:
    w = LEWriter()
    w.write_byte(0x00)
    w.write_byte(0x00)
    w.write_uint32(item_id)
    return LEReader(w.to_array())


def test_buy_dynamic_item_deducts_gold_and_grants(manager, stub_repo):
    # Arrange
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    item = md.inventories[0].items[0]
    expected_price = rarity_helper.calculate_buy_price(
        item.level, item.rarity, item.gold_value, "member_")

    # Act
    assert manager.handle_buy(server, conn, 0x5678, _buy_reader(item.item_id))

    # Assert — gold deducted by the exact client display price
    assert stub_repo.gold == 100_000 - expected_price
    # item removed from merchant stock
    assert item not in md.inventories[0].items
    # bag gained the item with the merchant's rarity/level
    bagged = conn.inv_model.main_items()
    assert len(bagged) == 1
    # bag stores the normalized inventory key (items.pal. prefix stripped).
    from drserver.data import item_catalog
    assert bagged[0].gc_class == item_catalog.normalize_key(item.gc_type)
    assert bagged[0].rarity == int(item.rarity)
    assert bagged[0].stored_level == item.level
    assert bagged[0].buy_price == expected_price
    # the EXACT ScaleMod previewed in the shop rides into the bag (C#
    # PresetScaleMod) — re-deriving it changed the item's stats after the buy
    assert item.scale_mod and bagged[0].scale_mod == item.scale_mod
    # wire: merchant remove (0x1F on merchant cid), gold 0x20, bag add 0x1E
    blob = b"".join(conn.sent)
    assert bytes([0x35, 0x78, 0x56, 0x1F]) in blob
    assert bytes([0x35, 0x02, 0x02, 0x20]) in blob
    assert bytes([0x35, 0x02, 0x02, 0x1E]) in blob


def test_buy_without_gold_is_refused(manager, stub_repo):
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    item = md.inventories[0].items[0]
    stub_repo.gold = 1

    assert manager.handle_buy(server, conn, 0x5678, _buy_reader(item.item_id))

    assert stub_repo.gold == 1
    assert item in md.inventories[0].items
    assert conn.sent == []
    assert conn.inv_model.main_items() == []


def test_inventory_gc_type_potion_mapping():
    """Minor potions keep their real consumable class (client renders "Minor
    Health/Mana Potion"); major potions round-trip through the potionpal itempack
    (get_packet_gc_class_for maps them back to consumable_major* on the wire)."""
    from drserver.managers.merchants import MerchantManager
    from drserver.data.gc_object import get_packet_gc_class_for
    m = MerchantManager._inventory_gc_type

    # MINOR: stored AND wired as the real consumable — NOT the noob PAL potion.
    assert m("items.consumables.Consumable_MinorHealthPotion") == \
        "items.consumables.Consumable_MinorHealthPotion"
    assert m("items.consumables.Consumable_MinorManaPotion") == \
        "items.consumables.Consumable_MinorManaPotion"
    assert get_packet_gc_class_for(m("items.consumables.Consumable_MinorHealthPotion")) == \
        "items.consumables.consumable_minorhealthpotion"
    assert "noob" not in m("items.consumables.Consumable_MinorHealthPotion").lower()

    # MAJOR: potionpal itempack that wires back to the major consumable.
    assert m("items.consumables.Consumable_MajorHealthPotion") == \
        "potionpal.healthpotion_itempack"
    assert get_packet_gc_class_for(m("items.consumables.Consumable_MajorHealthPotion")) == \
        "items.consumables.consumable_majorhealthpotion"


def test_buy_static_potion_stays_in_stock_and_maps_gc(manager, stub_repo):
    """Static stock never depletes; a MINOR Health Potion lands in the bag as the
    real ``Consumable_MinorHealthPotion`` (a valid client item), NOT remapped to
    ``potionpal.healthpotion_noob`` — that swapped the vendor's "Minor Health
    Potion" for the DISTINCT "Health Potion of the Daring Noobosaur" (live
    2026-07-01). The MAJOR potion keeps its potionpal round-trip (tested below)."""
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x2222] = _POTION_VENDOR
    md = manager.get_by_npc(_POTION_VENDOR)
    misc = md.inventories[0]
    minor_hp = next(i for i in misc.items if i.item_id == 255)

    assert manager.handle_buy(server, conn, 0x2222, _buy_reader(255))

    assert minor_hp in misc.items               # static stock untouched
    bagged = conn.inv_model.main_items()
    assert len(bagged) == 1
    assert bagged[0].gc_class == "items.consumables.Consumable_MinorHealthPotion"
    assert "noob" not in bagged[0].gc_class.lower()   # regression: not the noob potion
    # scale-to-level price: 50 * max(10,3) * 0.175 = 87 gold (member defaults)
    assert stub_repo.gold == 100_000 - 87


def test_buy_static_potion_stacks_into_partial_stack(manager, stub_repo):
    from drserver.data.saved_character import SavedInventoryItem
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x2222] = _POTION_VENDOR
    conn.inv_model.add("items.consumables.Consumable_MinorHealthPotion", 0, 0, count=3)
    # The DB carries the same stack (handle_buy reconciles the model with it).
    stub_repo.inventory = [SavedInventoryItem(
        gc_class="items.consumables.Consumable_MinorHealthPotion", x=0, y=0, count=3)]

    assert manager.handle_buy(server, conn, 0x2222, _buy_reader(255))

    bagged = conn.inv_model.main_items()
    assert len(bagged) == 1
    assert bagged[0].count == 4                 # merged, no new slot


# ── Sell ─────────────────────────────────────────────────────────────────────

def _sell_reader(item_id: int) -> LEReader:
    w = LEWriter()
    w.write_uint16(0x0000)                      # entityRef
    w.write_uint32(item_id)
    return LEReader(w.to_array())


def test_sell_shift_click_credits_exact_client_price(manager, stub_repo):
    from drserver.data.saved_character import SavedInventoryItem
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    it = conn.inv_model.add("1haxe1pal.1haxe1-4", 0, 0, count=1)
    # The DB carries the same item (handle_sell reconciles the model with it).
    stub_repo.inventory = [SavedInventoryItem(
        gc_class="1haxe1pal.1haxe1-4", x=0, y=0, count=1)]

    assert manager.handle_sell(server, conn, 0x5678, _sell_reader(it.slot_id))

    # 1haxe1-4: itemLevel 1, Rare delta -5 → adjusted 1; GV from DB (1.0)
    # sell = Fixed32(getValue(50*1*1.0*2.03..)=101) * 0.203 = 20
    expected = rarity_helper.calculate_sell_price(
        rarity_helper.get_equip_required_level(1, ItemRarity.Rare),
        1.0, ItemRarity.Rare, False, 10)
    assert stub_repo.gold == 100_000 + expected
    assert conn.inv_model.main_items() == []    # removed from the bag
    blob = b"".join(conn.sent)
    assert bytes([0x35, 0x02, 0x02, 0x1F]) in blob    # slot remove
    assert bytes([0x35, 0x02, 0x02, 0x20]) in blob    # AddCurrency


def test_sell_from_cursor_clears_active_item(manager, stub_repo):
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    conn.inv_model.cursor = CursorItem(gc_class="1haxe1pal.1haxe1-4", count=1)

    assert manager.handle_sell(server, conn, 0x5678, _sell_reader(0))

    assert conn.inv_model.cursor is None
    blob = b"".join(conn.sent)
    assert bytes([0x35, 0x02, 0x02, 0x29]) in blob    # ClearActiveItem
    assert bytes([0x35, 0x02, 0x02, 0x20]) in blob


def test_sell_never_pays_more_than_buy_price(manager, stub_repo):
    from drserver.data.saved_character import SavedInventoryItem
    conn = _conn()
    conn.player_level = 100                     # high level → no sell cap bite
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    it = conn.inv_model.add("1haxe5pal.1haxe5-5", 0, 0, count=1)
    stub_repo.inventory = [SavedInventoryItem(
        gc_class="1haxe5pal.1haxe5-5", x=0, y=0, count=1)]

    manager.handle_sell(server, conn, 0x5678, _sell_reader(it.slot_id))

    credited = stub_repo.gold - 100_000
    assert credited > 0
    adjusted = rarity_helper.get_equip_required_level(
        rarity_helper.get_item_level("1haxe5pal.1haxe5-5"), ItemRarity.Unique)
    gv = manager._gold_value_for("1haxe5pal.1haxe5-5")
    assert credited <= rarity_helper.calculate_buy_price(
        adjusted, ItemRarity.Unique, gv)


# ── Restock boundary (client 0x22 flow) ──────────────────────────────────────

def test_boundary_restocks_armed_merchant(manager, stub_repo):
    # Arrange — player clicked the vendor (armed) with stock generated
    conn = _conn()
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    old_ids = [i.item_id for t in md.dynamic_tabs for i in t.items]
    manager.arm_refresh(conn, _V1, 0x5678)
    assert conn.active_merchant_npc == _V1
    conn.active_merchant_due = 0.0              # countdown has elapsed

    # Act — the client's restock countdown expired (empty 0x22)
    assert manager.on_container_boundary(conn) is True

    # Assert — fresh stock with new ids; removes for every stale id
    new_ids = [i.item_id for t in md.dynamic_tabs for i in t.items]
    assert new_ids and not set(new_ids) & set(old_ids)
    assert len(conn.sent) == 1
    r = LEReader(conn.sent[0])
    assert r.read_byte() == 0x07
    removes = set()
    while True:
        op = r.read_byte()
        if op != 0x35:
            break
        assert r.read_uint16() == 0x5678
        sub = r.read_byte()
        if sub == 0x1F:
            removes.add(r.read_uint32())
            assert r.read_byte() == 0x02
            assert r.read_uint32() == 0
        else:
            assert sub == 0x1E
            break
    assert removes == set(old_ids)
    # still armed for the next cycle
    assert conn.active_merchant_npc == _V1


def test_boundary_without_armed_merchant_is_noop(manager):
    conn = _conn()
    assert manager.on_container_boundary(conn) is False
    assert conn.sent == []


def test_premature_boundary_is_swallowed(manager, stub_repo):
    """The client emits empty 0x22 on other container boundaries too (e.g.
    right after a respawn — live 2026-06-10). Before the armed due time the
    boundary must do nothing, or a 300s restock fires after seconds and the
    refresh packet crash-loops the respawning client."""
    conn = _conn()
    manager.ensure_inventory_for_level(_V1, 10)
    manager.arm_refresh(conn, _V1, 0x5678)      # due ~300s from now

    assert manager.on_container_boundary(conn) is False

    assert conn.sent == []
    assert conn.active_merchant_npc == _V1      # stays armed


def test_flush_due_refresh_restocks_once_due(manager, stub_repo):
    """Tick-driven restock (port of C# FlushClientMerchantRefreshes): when the
    armed due time passes WITHOUT a client 0x22 boundary (countdown skew), the
    server must push the restock itself — before this fix the shop stayed
    empty until the zone instance was torn down."""
    conn = _conn()
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    old_ids = [i.item_id for t in md.dynamic_tabs for i in t.items]
    manager.arm_refresh(conn, _V1, 0x5678)
    conn.active_merchant_due = 0.0              # due time elapsed

    assert manager.flush_due_refresh(conn) is True

    new_ids = [i.item_id for t in md.dynamic_tabs for i in t.items]
    assert new_ids and not set(new_ids) & set(old_ids)
    assert len(conn.sent) == 1
    assert conn.active_merchant_due > 0.0       # re-armed for the next cycle


def test_flush_due_refresh_noop_before_due(manager, stub_repo):
    conn = _conn()
    manager.ensure_inventory_for_level(_V1, 10)
    manager.arm_refresh(conn, _V1, 0x5678)      # due ~300s from now

    assert manager.flush_due_refresh(conn) is False
    assert conn.sent == []
    assert conn.active_merchant_npc == _V1      # stays armed


def test_flush_due_refresh_noop_when_unarmed(manager):
    conn = _conn()
    assert manager.flush_due_refresh(conn) is False
    assert conn.sent == []


def test_buy_membership_gated_item_notifies_player(manager, stub_repo, monkeypatch):
    """A free player buying member-only gear must be TOLD in-game, not just
    logged server-side."""
    conn = _conn()
    conn.system_messages = []
    conn.send_system_message = conn.system_messages.append
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    monkeypatch.setattr(manager, "_is_free_player", lambda c: True)
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    # Town vendors now stock Superior (green) gear, which is not member-gated;
    # inject a member-only Rare item to exercise the gate.
    from drserver.managers.merchants import MerchantItem
    item = MerchantItem(gc_type="items.pal.1haxepal.normal001",
                        item_id=md.next_item_id, rarity=ItemRarity.Rare,
                        gold_value=1.0, level=10)
    md.dynamic_tabs[0].items.append(item)

    w = LEWriter()
    w.write_byte(0x00)
    w.write_byte(0x00)
    w.write_uint32(item.item_id)
    assert manager.handle_buy(server, conn, 0x5678, LEReader(w.to_array())) is True

    assert conn.system_messages, "free player must see the membership notice"
    assert conn.sent == []                       # nothing granted / charged


def test_reenter_stock_push_removes_before_adding(manager, stub_repo):
    """Re-entering an instance (or any duplicate enter) must not re-add item
    ids the client already holds — the second push prefixes removes for the
    previously sent ids (the proven refresh shape)."""
    conn = _conn()
    server = _server(conn)

    manager.send_zone_stock(server, conn, [(0x5678, _V1)])
    assert len(conn.sent) == 1
    first_ids = conn.merchant_stock_sent[0x5678]
    assert first_ids

    manager.send_zone_stock(server, conn, [(0x5678, _V1)])
    assert len(conn.sent) == 2
    r = LEReader(conn.sent[1])
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x5678
    assert r.read_byte() == 0x1F                # remove comes first
    assert r.read_uint32() in set(first_ids)


# ── Grid fill + refill-on-buy ─────────────────────────────────────────────────

def _no_overlap(manager, tab) -> None:
    occupied = set()
    for it in tab.items:
        w, h = manager._item_dimensions(it.gc_type)
        for dx in range(w):
            for dy in range(h):
                cell = (it.x + dx, it.y + dy)
                assert cell not in occupied, "merchant items overlap in the grid"
                occupied.add(cell)


def _grid_occupancy(manager, tab) -> float:
    cells = sum(manager._item_dimensions(it.gc_type)[0]
                * manager._item_dimensions(it.gc_type)[1] for it in tab.items)
    return cells / (tab.width * tab.height)


def test_dynamic_tabs_fill_grid_densely(manager):
    """The 10x14 grid — not an artificial item cap — is the real limiter, so a
    vendor's stock packs nearly the whole grid (the retired 24-item cap chopped
    the armor tab to ~85% full; lifting it lets the grid reach ~95-100%). Assert
    the best dynamic tab is densely packed, the safety ceiling is respected, and
    nothing overlaps."""
    from drserver.managers import merchants as merch_mod
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    dynamic = [t for t in md.inventories
               if t.auto_generate and not t.static_contents]
    assert max(_grid_occupancy(manager, t) for t in dynamic) >= 0.90
    assert all(len(t.items) <= merch_mod._MAX_GENERATED_PER_TAB for t in dynamic)
    for tab in dynamic:
        _no_overlap(manager, tab)


def test_buy_arms_debounced_refill_without_instant_restock(manager, stub_repo):
    """A purchase does NOT refill instantly (a hole the exact size of the bought
    item would just respawn the same archetype). It leaves the freed slot open
    and arms a debounced refill for the per-instance tick to flush later."""
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    tab = md.inventories[0]
    sold = tab.items[0]
    before_count = len(tab.items)

    assert manager.handle_buy(server, conn, 0x5678, _buy_reader(sold.item_id))

    # sold item gone, a hole remains (no instant refill), debounce armed
    assert sold.item_id not in {i.item_id for i in tab.items}
    assert len(tab.items) == before_count - 1
    assert conn.merchant_refill_npc == md.npc_gc_type
    assert conn.merchant_refill_cid == 0x5678
    assert conn.merchant_refill_due > 0
    # no merchant-component add was pushed yet (only the 0x1F remove + bag add)
    assert bytes([0x35, 0x78, 0x56, 0x1E]) not in b"".join(conn.sent)


def test_pending_refill_flush_repacks_freed_space(manager, stub_repo):
    """Once the debounce window elapses the flush re-packs the freed grid space
    with fresh items (0x1E add on the merchant cid) and disarms itself; before
    the due time it is a no-op."""
    import time
    conn = _conn()
    server = _server(conn)
    server.merchant_components[0x5678] = _V1
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    tab = md.inventories[0]
    # buy a couple of items to open up space
    for _ in range(2):
        manager.handle_buy(server, conn, 0x5678, _buy_reader(tab.items[0].item_id))
    after_buys = len(tab.items)
    conn.sent.clear()

    # not yet due -> no-op
    assert manager.flush_pending_buy_refill(conn) is False
    assert not conn.sent

    # force the debounce window to have elapsed
    conn.merchant_refill_due = time.monotonic() - 0.01
    assert manager.flush_pending_buy_refill(conn) is True
    # space was re-packed and the debounce disarmed
    assert len(tab.items) > after_buys
    assert conn.merchant_refill_due == 0.0
    assert bytes([0x35, 0x78, 0x56, 0x1E]) in b"".join(conn.sent)
    _no_overlap(manager, tab)


def test_buy_skips_admin_sentinel_cid_refill(manager, stub_repo):
    """The @buy admin path uses a sentinel cid (-1) and must NOT arm a refill
    (no real client component to receive adds)."""
    conn = _conn()
    server = _server(conn)
    manager.ensure_inventory_for_level(_V1, 10)
    md = manager.get_by_npc(_V1)
    before = len(md.inventories[0].items)
    assert manager.buy_item(server, conn, _V1, md.inventories[0].items[0].item_id)
    # one sold, nothing armed through the sentinel path
    assert len(md.inventories[0].items) == before - 1
    assert getattr(conn, "merchant_refill_due", 0.0) == 0.0
