"""InventoryModel — slot-map + cursor unit tests.

The client identifies items by the slot id the server assigned, not by list
position. These pin the off-by-one fix (slots are 1-based) and the cursor
pickup/place round trip.
"""
from drserver.net.inventory_model import CursorItem, InventoryModel


def _rows():
    return [
        {"gc_class": "potionpal.healthpotion_noob", "x": 1, "y": 0, "count": 20},
        {"gc_class": "potionpal.manapotion_noob", "x": 2, "y": 0, "count": 18},
        {"gc_class": "SkillBookPAL.SummonBlingGnome", "x": 0, "y": 2, "count": 1},
    ]


def test_load_assigns_one_based_slot_ids():
    # Arrange / Act
    model = InventoryModel()
    seeded = model.load(_rows())

    # Assert — first item is slot 1, not 0.
    assert [it.slot_id for it in seeded] == [1, 2, 3]
    assert model.by_slot(1).gc_class == "potionpal.healthpotion_noob"
    assert model.by_slot(2).gc_class == "potionpal.manapotion_noob"
    assert model.by_slot(3).gc_class == "SkillBookPAL.SummonBlingGnome"


def test_resolve_matches_client_slot_not_list_index():
    # Arrange — the bug: clicking mana potion (slot 2) returned items[2]=bling gnome.
    model = InventoryModel()
    model.load(_rows())

    # Act / Assert — slot 2 is the mana potion, exactly.
    assert model.resolve(2).gc_class == "potionpal.manapotion_noob"
    assert model.resolve(3).gc_class == "SkillBookPAL.SummonBlingGnome"


def test_pickup_place_round_trip_assigns_fresh_slot():
    # Arrange
    model = InventoryModel()
    model.load(_rows())
    picked = model.resolve(2)

    # Act — pickup: remove + put on cursor.
    model.remove(picked.slot_id)
    model.cursor = CursorItem(gc_class=picked.gc_class, count=picked.count)
    assert model.by_slot(2) is None

    # Place at a new cell — gets a fresh slot id, cursor cleared.
    placed = model.add(model.cursor.gc_class, 5, 2, count=model.cursor.count)
    model.cursor = None

    # Assert
    assert placed.slot_id == 4               # next after 1,2,3
    assert model.by_slot(4).x == 5 and model.by_slot(4).y == 2
    assert model.cursor is None


def test_occupied_detects_overlap():
    # Arrange
    model = InventoryModel()
    model.load(_rows())
    size = lambda gc: (1, 1)  # noqa: E731

    # Act / Assert
    assert model.occupied(2, 0, 1, 1, size) is True     # mana potion cell
    assert model.occupied(9, 7, 1, 1, size) is False    # empty corner


def test_mod_refs_round_trip_through_add_save_load():
    # A bought/looted item's attribute mods (Intellect etc.) must survive the
    # model -> to_saved (DB row) -> load cycle, else they vanish on relog.
    model = InventoryModel()
    mods = ["items.modpal.MageModPal.Rare.Mod1",
            "items.modpal.MageModPal.DamageBonus.Mod1"]
    added = model.add("crystalarmor1pal.crystalarmor1-4", 0, 0, rarity=3,
                      stored_level=14, scale_mod="ScaleModPAL.Rare.Mod1",
                      mod_refs=mods)
    assert added.mod_refs == mods

    saved = model.to_saved()
    assert saved[0]["mod_refs"] == mods

    reloaded = InventoryModel()
    reloaded.load(saved)
    assert reloaded.main_items()[0].mod_refs == mods
    # an item with no mods round-trips to an empty list, not None
    plain = InventoryModel()
    plain.add("scalegloves1pal.scalegloves1-1", 1, 1)
    assert plain.to_saved()[0]["mod_refs"] == []


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
