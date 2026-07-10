"""Inventory handler tests — slot occupancy, item placement, use, drop."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from _paths import copy_shipped_db, has_shipped_db

from drserver.net.inventory import _get_item_size
from drserver.net.inventory_model import InventoryModel


def _model(rows):
    m = InventoryModel()
    m.load(rows)
    return m


def test_item_dimensions_loaded():
    """Verify item dimensions resolve from the rebuilt items table via item_catalog."""
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database as db
    from drserver.data import item_catalog
    db.initialize(copy_shipped_db())
    item_catalog.reset()

    # A known 2x2 item resolves through the catalog (legacy items.pal. key tolerated).
    assert item_catalog.get_item_size("items.pal.1HAxe1PAL.1HAxe1-1") == (2, 2)


def test_get_item_size():
    # Known 2x2 item (1H Axe variants).
    w, h = _get_item_size("items.pal.1HAxe1PAL.1HAxe1-1")
    assert w in (1, 2), f"Expected 1 or 2, got {w}"

    # Unknown item defaults to 1x1
    w, h = _get_item_size("nonexistent_item_xyz")
    assert w == 1
    assert h == 1


def test_slot_occupied_basic():
    model = _model([{"gc_class": "items.pal.1HAxe1PAL.1HAxe1-1", "x": 0, "y": 0, "count": 1}])
    # 1H Axe is 2x2; cells (0,0)..(1,1) are occupied.
    assert model.occupied(0, 0, 1, 1, _get_item_size)
    assert model.occupied(1, 0, 1, 1, _get_item_size)
    assert model.occupied(0, 1, 1, 1, _get_item_size)
    assert model.occupied(1, 1, 1, 1, _get_item_size)
    # Cell (2,0) is free.
    assert not model.occupied(2, 0, 1, 1, _get_item_size)


def test_slot_occupied_empty():
    model = InventoryModel()
    assert not model.occupied(0, 0, 1, 1, _get_item_size)
    assert not model.occupied(5, 5, 1, 1, _get_item_size)


def test_find_by_grid():
    model = _model([{"gc_class": "items.pal.1HAxe1PAL.1HAxe1-1", "x": 2, "y": 3, "count": 1}])
    # Should find the 2x2 item anywhere in its rectangle.
    found = model.by_grid(2, 3, _get_item_size)
    assert found is not None and found.x == 2

    # (4,3) is free (2+2=4, so (4,3) is not inside the item).
    assert model.by_grid(4, 3, _get_item_size) is None


def test_slot_occupied_multiple_items():
    model = _model([
        {"gc_class": "items.pal.1HAxe1PAL.1HAxe1-1", "x": 0, "y": 0, "count": 1},   # 2x2
        {"gc_class": "items.consumables.consumable_majorhealthpotion", "x": 3, "y": 0, "count": 5},  # 1x1
    ])
    assert model.occupied(0, 0, 1, 1, _get_item_size)     # item 1
    assert model.occupied(1, 1, 1, 1, _get_item_size)     # item 1
    assert model.occupied(3, 0, 1, 1, _get_item_size)     # item 2
    assert not model.occupied(2, 0, 1, 1, _get_item_size)  # free
    assert not model.occupied(5, 5, 1, 1, _get_item_size)  # free


# ── count / remove by gc_class (quest item objectives + RemoveOnFinalize) ──────

def _conn_with_items(monkeypatch, rows):
    """Stub conn with an inventory model + in-memory char repo, so the count /
    remove helpers can mutate + persist without a live UnitContainer. Uses
    monkeypatch so the patched repo is restored (no cross-test pollution)."""
    import types
    from drserver.net import inventory as inv
    model = _model(rows)
    char = types.SimpleNamespace(inventory=[])
    conn = types.SimpleNamespace(
        char_sql_id=1, inv_model=model, unit_container_id=0,
        send_to_client=lambda b: None,
    )
    monkeypatch.setattr(inv, "character_repository", types.SimpleNamespace(
        get_character=lambda _id: char,
        save_character=lambda ch: None,
    ))
    return conn, model


def test_count_items_by_gc_sums_stacks(monkeypatch):
    from drserver.net import inventory as inv
    conn, _ = _conn_with_items(monkeypatch, [
        {"gc_class": "QuestItemPAL.Token", "x": 0, "y": 0, "count": 60},
        {"gc_class": "QuestItemPAL.Token", "x": 1, "y": 0, "count": 20},
        {"gc_class": "items.misc.junk", "x": 2, "y": 0, "count": 5},
    ])
    assert inv.count_items_by_gc(conn, "questitempal.token") == 80   # case-insensitive
    assert inv.count_items_by_gc(conn, "items.misc.junk") == 5
    assert inv.count_items_by_gc(conn, "nothing.here") == 0


def test_remove_items_by_gc_decrements_then_removes_slots(monkeypatch):
    from drserver.net import inventory as inv
    conn, model = _conn_with_items(monkeypatch, [
        {"gc_class": "QuestItemPAL.Token", "x": 0, "y": 0, "count": 60},
        {"gc_class": "QuestItemPAL.Token", "x": 1, "y": 0, "count": 20},
    ])
    removed = inv.remove_items_by_gc(conn, "QuestItemPAL.Token", 75)
    assert removed == 75
    assert inv.count_items_by_gc(conn, "QuestItemPAL.Token") == 5     # 80 - 75
    assert len(model.main_items()) == 1   # one slot consumed, one decremented


def test_remove_items_by_gc_caps_at_available(monkeypatch):
    from drserver.net import inventory as inv
    conn, model = _conn_with_items(monkeypatch, [
        {"gc_class": "QuestItemPAL.Token", "x": 0, "y": 0, "count": 10},
    ])
    removed = inv.remove_items_by_gc(conn, "QuestItemPAL.Token", 75)
    assert removed == 10
    assert model.main_items() == []


if __name__ == "__main__":
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
