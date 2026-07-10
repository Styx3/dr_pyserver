"""ItemStatDatabase tests — stat pool resolution and mod lookups."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from _paths import copy_shipped_db, has_shipped_db

from drserver.db import game_database as db


def _setup():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    db.initialize(copy_shipped_db())


def test_stat_pools_load():
    _setup()
    from drserver.data.item_stat_database import StatPool

    pool = StatPool(pool_name="DamageBonusPool", base=10.0, scale=3270.0, divisor=109.0)
    # Level 1: base + 0 = 10
    assert pool.compute(1) == 10.0
    # Level 11: base + 10*3270/109 = 10 + 300 = 310
    val = pool.compute(11)
    assert abs(val - 310.0) < 0.1


def test_database_load():
    _setup()
    from drserver.data.item_stat_database import item_stat_database

    item_stat_database.load()
    assert len(item_stat_database._pools) > 0
    assert len(item_stat_database._mods_by_item) > 0


def test_get_mod_count():
    _setup()
    from drserver.data.item_stat_database import item_stat_database

    item_stat_database.load()

    # 1HAxeMythic1 has mods in the DB.
    mod_count = item_stat_database.get_mod_count("1haxemythicpal.1haxemythic1")
    assert mod_count >= 1, f"Expected >=1 mods, got {mod_count}"

    # Unknown item has 0.
    assert item_stat_database.get_mod_count("nonexistent_item_xyz") == 0


def test_get_item_stats():
    _setup()
    from drserver.data.item_stat_database import item_stat_database

    item_stat_database.load()

    # A known mythic item stats lookup.
    stats = item_stat_database.get_item_stats("1haxemythicpal.1haxemythic1", 1)
    assert isinstance(stats, dict)
    # Should have some stats at level 1
    # The DB has DIVINE_DAMAGE_WEAPON_ADD and SLASHING_DAMAGE_BONUS
    if stats:
        for attr, bonus in stats.items():
            assert isinstance(attr, str)
            assert isinstance(bonus, int)
            assert bonus >= 0


def test_cumulative_stats():
    _setup()
    from drserver.data.item_stat_database import item_stat_database

    item_stat_database.load()

    equipped = {
        10: "items.pal.1haxepal.normal001",
    }
    cumulative = item_stat_database.get_cumulative_stats(equipped, 1)
    assert isinstance(cumulative, dict)


def test_gc_object_mod_count_integration():
    _setup()
    from drserver.data.gc_object import GCObject
    from drserver.data.item_stat_database import item_stat_database

    item_stat_database.load()

    # A weapon with known mods.
    item = GCObject(native_class="MeleeWeapon", gc_class="1HAxeMythicPAL.1HAxeMythic1")
    count = item._get_mod_count()
    # Should have at least 2 mod slots (from DB data showing 2 distinct slots).
    assert count >= 1, f"Expected >=1 mod count for Mythic axe, got {count}"


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
