"""item_catalog tests — namespace-tolerant price + dimension lookup.

Validates the client-faithful catalog that replaced the fabricated
``sellable_items`` / ``item_dimensions`` tables. Prices/sizes are sourced from
the rebuilt ``items`` / ``weapons`` / ``armor`` content tables.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from _paths import copy_shipped_db, has_shipped_db

from drserver.data import item_catalog


def test_normalize_key_strips_legacy_prefixes():
    # Arrange / Act / Assert
    assert item_catalog.normalize_key("items.pal.1HAxe1PAL.1HAxe1-1") == "1haxe1pal.1haxe1-1"
    assert item_catalog.normalize_key("items.consumables.Consumable_TownPortal") == "consumable_townportal"
    assert item_catalog.normalize_key("1HAxe1PAL.1HAxe1-1") == "1haxe1pal.1haxe1-1"
    assert item_catalog.normalize_key(None) == ""


def test_normalize_key_strips_only_leading_prefix():
    # A non-prefixed bare key is just lowercased, untouched otherwise.
    assert item_catalog.normalize_key("ScaleArmor1PAL.ScaleArmor1-1") == "scalearmor1pal.scalearmor1-1"


def _require_db():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database as db

    db.initialize(copy_shipped_db())
    item_catalog.reset()


def test_buy_price_from_weapon_table_is_verbatim():
    _require_db()
    # 1HAxe1-1 has gold_value 50 in the rebuilt weapons table (verbatim from emulator).
    assert item_catalog.get_buy_price("1haxe1pal.1haxe1-1") == 50
    # Legacy-namespaced key resolves to the same bare row.
    assert item_catalog.get_buy_price("items.pal.1HAxe1PAL.1HAxe1-1") == 50


def test_buy_price_unknown_defaults():
    _require_db()
    assert item_catalog.get_buy_price("nonexistent_item_xyz") == item_catalog.DEFAULT_PRICE
    assert item_catalog.get_buy_price("") == item_catalog.DEFAULT_PRICE


def test_sell_price_is_fraction_of_buy_min_one():
    _require_db()
    buy = item_catalog.get_buy_price("1haxe1pal.1haxe1-1")
    assert item_catalog.get_sell_price("1haxe1pal.1haxe1-1") == max(1, int(buy * item_catalog.SELL_FRACTION))
    # Unknown item still sells for at least 1 gold.
    assert item_catalog.get_sell_price("nonexistent_item_xyz") >= 1


def test_item_size_known_and_default():
    _require_db()
    # 1H Axe variants occupy a 2x2 grid.
    w, h = item_catalog.get_item_size("items.pal.1HAxe1PAL.1HAxe1-1")
    assert (w, h) == (2, 2)
    # Unknown item defaults to 1x1.
    assert item_catalog.get_item_size("nonexistent_item_xyz") == (1, 1)


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
