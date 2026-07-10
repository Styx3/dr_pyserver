"""Tests for the ``.gc`` -> ``items`` / ``weapons`` / ``armor`` table importer.

Builds a small flat ``.gc`` directory mirroring the real DR_Server ``gc/`` layout
(numbered-PAL item palettes whose leaves ``extends`` base palettes, which in turn
root at the native item base classes ``MeleeWeapon`` / ``RangedWeapon`` / ``Armor``
/ ``Item`` / ``ActiveItem`` / ``ItemAttributeModifier``). Proves:

* selection is by *extends-root* (the principled selector, like creatures'
  ``StockUnit``) — item-rooted leaves are kept; non-item content (a creature)
  is excluded;
* weapon / armor / generic-item classification by root maps onto the three
  tables (``items`` is the superset, ``weapons`` / ``armor`` the subsets);
* ``extends`` inheritance is flattened (leaf ``Label`` overrides base stats);
* the fabricated, ``.gc``-absent ``mod_count`` is **never** invented by
  ``collect_item_rows`` (left ``None``) — it is preserved verbatim from the
  pre-existing live rows by ``rebuild_items_table``;
* ``gc_gold_value`` / ``gold_value`` are likewise preserved verbatim;
* a live row referenced by a player/merchant but missed by the selector is
  **carried forward** (never orphaned);
* ``raw_json`` is lossless. No ``/mnt/c`` or real-DB access.
"""
import json
import sqlite3

import pytest

from drserver.data.items_importer import (
    collect_item_rows,
    rebuild_items_table,
    _ITEM_COLUMNS,
    _WEAPON_COLUMNS,
)

pytestmark = pytest.mark.unit


def _write(root, name, text):
    p = root / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def gc_dir(tmp_path):
    """Minimal flat gc/ tree: weapon + armor + potion + a modifier + a creature
    (the creature must be excluded)."""
    # ── native-rooted base palettes (extends chain terminates at native classes
    #    that have no .gc, mirroring the real tree) ──
    _write(tmp_path, "1HMeleeWeaponPAL.gc", """1HMeleeWeaponPAL
{
    1HAxe1 extends MeleeWeapon
    {
        Description
        {
            Label = "Default Hand Axe";
            Damage = 0.66;
            WeaponClass = "1HMELEE";
            SlotType = "RightHand";
            Range = 10;
            Equipable = true;
            InventoryWidth = 2;
            InventoryHeight = 2;
            InventoryIcon = "Weapon_Icon_HandAxe";
            GroundObject = Weapon_HandAxe;
            GoldValue = 5.0;
        }
    }
}
""")
    # concrete numbered weapon palette: leaf overrides only the Label
    _write(tmp_path, "1HAxe1PAL.gc", """1HAxe1PAL
{
    1HAxe1-1 extends 1HMeleeWeaponPAL.1HAxe1
    {
        Description { Label = "Cardboard Hand Axe"; }
    }
    1HAxe1-2 extends 1HMeleeWeaponPAL.1HAxe1
    {
        Description { Label = "Wooden Hand Axe"; }
    }
}
""")
    # armor
    _write(tmp_path, "ChainPAL.gc", """ChainPAL
{
    ChainArmor1 extends Armor
    {
        Description
        {
            Label = "Default Chain";
            DefenseRating = 50;
            SlotType = "Chest";
            InventoryWidth = 2;
            InventoryHeight = 3;
            InventoryIcon = "Armor_Icon_Chain";
            GoldValue = 8.0;
        }
    }
}
""")
    _write(tmp_path, "ChainArmor1PAL.gc", """ChainArmor1PAL
{
    ChainArmor1-1 extends ChainPAL.ChainArmor1
    {
        Description { Label = "Rusty Chain Vest"; }
    }
}
""")
    # generic holdable item (potion) rooted at ActiveItem
    _write(tmp_path, "PotionPAL.gc", """PotionPAL
{
    HealthPotion1 extends ActiveItem
    {
        Description
        {
            Label = "Minor Health Potion";
            Stackable = true;
            MaxStackSize = 20;
            LevelReq = 1;
            InventoryIcon = "Potion_Icon_Red";
            GoldValue = 2.0;
        }
    }
}
""")
    # an item-attribute-modifier (affix) — generic item, included in superset
    _write(tmp_path, "AmuletModPAL.gc", """AmuletModPAL
{
    Superior
    {
        Mod1 extends ItemAttributeModifier
        {
            Description
            {
                Label = "Jackalope";
                Quality = SUPERIOR;
                GoldValue = 0;
            }
        }
    }
}
""")
    # a creature — NOT an item, must be excluded even though it has a Description
    _write(tmp_path, "SomeCreature.gc", """SomeCreature
{
    Grunt extends StockUnit
    {
        Description { Label = "Goblin Grunt"; MaxHealth = 1.5; }
    }
}
""")
    return tmp_path


def _by_key(rows):
    return {r["gc_type"]: r for r in rows}


# ── selection + classification ──

def test_collect_selects_item_rooted_nodes_excludes_creature(gc_dir):
    out = collect_item_rows(str(gc_dir))
    items = _by_key(out["items"])
    # weapon leaves, armor leaf, potion, affix are present
    assert "1haxe1pal.1haxe1-1" in items
    assert "1haxe1pal.1haxe1-2" in items
    assert "chainarmor1pal.chainarmor1-1" in items
    assert "potionpal.healthpotion1" in items
    assert "amuletmodpal.superior.mod1" in items
    # the creature is excluded from every table
    assert not any("somecreature" in k for k in items)


def test_classification_into_subset_tables(gc_dir):
    out = collect_item_rows(str(gc_dir))
    items = _by_key(out["items"])
    weapons = _by_key(out["weapons"])
    armor = _by_key(out["armor"])
    # weapon -> items(category=weapons) + weapons table only
    assert items["1haxe1pal.1haxe1-1"]["category"] == "weapons"
    assert "1haxe1pal.1haxe1-1" in weapons
    assert "1haxe1pal.1haxe1-1" not in armor
    # armor -> items(category=armor) + armor table only
    assert items["chainarmor1pal.chainarmor1-1"]["category"] == "armor"
    assert "chainarmor1pal.chainarmor1-1" in armor
    assert "chainarmor1pal.chainarmor1-1" not in weapons
    # potion + affix -> items(category=item) only
    assert items["potionpal.healthpotion1"]["category"] == "item"
    assert "potionpal.healthpotion1" not in weapons
    assert "potionpal.healthpotion1" not in armor
    assert items["amuletmodpal.superior.mod1"]["category"] == "item"


# ── inheritance flattening ──

def test_weapon_inherits_base_stats_leaf_overrides_label(gc_dir):
    weapons = _by_key(collect_item_rows(str(gc_dir))["weapons"])
    w = weapons["1haxe1pal.1haxe1-1"]
    assert w["label"] == "Cardboard Hand Axe"      # leaf override
    assert abs(w["damage"] - 0.66) < 1e-9          # inherited from base
    assert w["weapon_class"] == "1HMELEE"
    assert w["slot_type"] == "RightHand"
    assert w["weapon_range"] == 10
    assert w["equipable"] == 1
    assert w["inventory_icon"] == "Weapon_Icon_HandAxe"


def test_armor_maps_defense_rating(gc_dir):
    armor = _by_key(collect_item_rows(str(gc_dir))["armor"])
    a = armor["chainarmor1pal.chainarmor1-1"]
    assert a["label"] == "Rusty Chain Vest"
    assert abs(a["defense_rating"] - 50.0) < 1e-9
    assert a["slot_type"] == "Chest"
    assert a["damage"] in (None, 0)                 # not a weapon field


def test_generic_item_maps_stack_and_level(gc_dir):
    items = _by_key(collect_item_rows(str(gc_dir))["items"])
    p = items["potionpal.healthpotion1"]
    assert p["label"] == "Minor Health Potion"
    assert p["stackable"] == 1
    assert p["max_stack_size"] == 20
    assert p["level_req"] == 1


# ── never-fabricate mod_count; lossless raw_json ──

def test_mod_count_not_fabricated_by_collect(gc_dir):
    out = collect_item_rows(str(gc_dir))
    for table in ("items", "weapons", "armor"):
        for r in out[table]:
            assert r["mod_count"] is None


def test_raw_json_lossless(gc_dir):
    weapons = _by_key(collect_item_rows(str(gc_dir))["weapons"])
    raw = json.loads(weapons["1haxe1pal.1haxe1-1"]["raw_json"])
    # flattened node carries the inherited Description with the leaf label
    assert raw["children"]["Description"]["properties"]["Label"] == "Cardboard Hand Axe"


# ── rebuild: preserve verbatim + referential safety net ──

def _seed_live(conn):
    """Pre-existing (emulator) item tables + a player/merchant reference."""
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, gc_type TEXT, mod_count INTEGER, gc_gold_value REAL)")
    conn.execute("CREATE TABLE weapons (id INTEGER PRIMARY KEY, gc_type TEXT, mod_count INTEGER, gc_gold_value REAL, gold_value REAL)")
    conn.execute("CREATE TABLE armor (id INTEGER PRIMARY KEY, gc_type TEXT, mod_count INTEGER, gc_gold_value REAL, gold_value REAL)")
    # a weapon with a real mod_count + gold the .gc cannot supply
    conn.execute("INSERT INTO weapons (gc_type, mod_count, gc_gold_value, gold_value) "
                 "VALUES ('1HAxe1PAL.1HAxe1-1', 3, 123.0, 50.0)")
    conn.execute("INSERT INTO items (gc_type, mod_count, gc_gold_value) "
                 "VALUES ('1HAxe1PAL.1HAxe1-1', 3, 123.0)")
    # a referenced-but-unselected emulator item (must be carried forward)
    conn.execute("INSERT INTO items (gc_type, mod_count, gc_gold_value) "
                 "VALUES ('legacy.fabricated.thing', 2, 7.0)")
    conn.execute("CREATE TABLE character_equipment (id INTEGER PRIMARY KEY, gc_class TEXT)")
    conn.execute("CREATE TABLE character_inventory (id INTEGER PRIMARY KEY, gc_class TEXT)")
    conn.execute("CREATE TABLE merchant_inventory_items (id INTEGER PRIMARY KEY, item_gc_type TEXT)")
    conn.execute("INSERT INTO character_inventory (gc_class) VALUES ('legacy.fabricated.thing')")
    conn.commit()


def test_rebuild_preserves_mod_count_and_gold_verbatim(gc_dir):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_live(conn)
    rebuild_items_table(conn, str(gc_dir))
    row = conn.execute("SELECT mod_count, gc_gold_value, gold_value FROM weapons "
                       "WHERE LOWER(gc_type)='1haxe1pal.1haxe1-1'").fetchone()
    assert row["mod_count"] == 3            # preserved from live, not fabricated
    assert row["gc_gold_value"] == 123.0
    assert row["gold_value"] == 50.0
    # a freshly-selected weapon with no live row keeps mod_count NULL
    fresh = conn.execute("SELECT mod_count FROM weapons "
                         "WHERE LOWER(gc_type)='1haxe1pal.1haxe1-2'").fetchone()
    assert fresh["mod_count"] is None


def test_rebuild_carries_forward_referenced_unselected_row(gc_dir):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_live(conn)
    rebuild_items_table(conn, str(gc_dir))
    # the referenced fabricated item is NOT in the gc tree, but is referenced by
    # a character's inventory -> must survive the rebuild
    kept = conn.execute("SELECT COUNT(*) FROM items "
                        "WHERE LOWER(gc_type)='legacy.fabricated.thing'").fetchone()[0]
    assert kept == 1


def test_rebuild_returns_item_count_and_populates_subsets(gc_dir):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_live(conn)
    n = rebuild_items_table(conn, str(gc_dir))
    items_n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    weapons_n = conn.execute("SELECT COUNT(*) FROM weapons").fetchone()[0]
    armor_n = conn.execute("SELECT COUNT(*) FROM armor").fetchone()[0]
    assert n == items_n
    # Full-content-universe scope: the intermediate base palettes
    # (1HMeleeWeaponPAL.1HAxe1, ChainPAL.ChainArmor1) are themselves item-rooted
    # and are included alongside the numbered leaves — this is the +52/+545
    # "intermediate base" inclusion measured in recon.
    assert weapons_n == 3          # base 1HAxe1 + two numbered leaves
    assert armor_n == 2            # base ChainArmor1 + one numbered leaf
    # superset >= subsets + potion + affix + carried-forward legacy row
    assert items_n >= weapons_n + armor_n + 2
