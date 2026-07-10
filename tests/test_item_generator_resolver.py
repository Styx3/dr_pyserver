"""Item-generator resolver — direct authored items, generator tables, links.

Exercises the resolver against the real shipped ``items`` content so the
generator graph (SingleItemGenerator / ItemGeneratorLink / RandomItemGenerator,
dotted sub-entries, level filtering) is validated on true data, the way the C#
``GenerateAuthoredGeneratorLoot`` / ``IsDirectAuthoredRewardItem`` path is.
"""
import random
import sys

import pytest

sys.path.insert(0, "tests")
from _paths import copy_shipped_db, has_shipped_db  # noqa: E402


@pytest.fixture(scope="module")
def resolver():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    from drserver.data import item_generator_resolver
    return item_generator_resolver


# ── direct authored items (gc_type IS a real item) ──────────────────────────────

def test_direct_authored_item_returns_itself(resolver):
    assert resolver.is_real_item("QuestItemPAL.Token") is True
    assert resolver.resolve_generator_items("QuestItemPAL.Token", 1, 1) == \
        ["QuestItemPAL.Token"]


def test_direct_item_respects_count(resolver):
    assert resolver.resolve_generator_items("QuestItemPAL.Token", 1, 3) == \
        ["QuestItemPAL.Token"] * 3


def test_generator_table_is_not_a_real_item(resolver):
    assert resolver.is_real_item("NormalPotionIG") is False


# ── generator tables ────────────────────────────────────────────────────────────

def test_generator_table_resolves_to_member_items(resolver):
    out = resolver.resolve_generator_items("NormalPotionIG", level=4, count=4,
                                           rng=random.Random(1))
    assert len(out) == 4
    assert all(o.lower().startswith("items.consumables.consumable_") for o in out)


def test_single_entry_generator(resolver):
    assert resolver.resolve_generator_items("Dungeon01_ItemIG.Entry4", level=7,
                                            count=1) == ["Dungeon01ItemPAL.D01_Item_02"]


def test_maxlevel_filters_out_of_band_entries(resolver):
    # Entry4's only member carries MaxLevel 20 → nothing rolls above it.
    assert resolver.resolve_generator_items("Dungeon01_ItemIG.Entry4", level=99,
                                            count=1) == []


# ── dotted sub-entries + nested links ──────────────────────────────────────────

def test_sub_entry_navigates_to_single_item_child(resolver):
    # keyig.<child> is a SingleItemGenerator child of the keyig table.
    assert resolver.resolve_generator_items("keyig.D08_Q05_1_Key", 1, 1) == \
        ["KeyPAL.D08_Q05_1_Key"]


def test_linked_generator_is_followed(resolver):
    # TokenRewardMythicJewelryIG → ItemGeneratorLink → MythicRingIG/MythicAmuletIG.
    out = resolver.resolve_generator_items("TokenRewardMythicJewelryIG", level=15,
                                           count=1, rng=random.Random(2))
    assert len(out) == 1
    assert "mythic" in out[0].lower()


def test_random_item_generator_link_is_followed(resolver):
    # SuperiorJewelryIG.Ring → RandomItemGenerator (ItemGenerator → SuperiorRingIG).
    out = resolver.resolve_generator_items("SuperiorJewelryIG.Ring", level=1,
                                           count=1, rng=random.Random(3))
    assert len(out) == 1 and out[0].lower().startswith("ringpal.")


# ── unresolvable / empty ────────────────────────────────────────────────────────

def test_non_table_generator_base_is_not_a_direct_item(resolver):
    """A generator sitting in the items table under a NON-ItemGeneratorTable base
    (LegendIG, RandomItemGenerator, *LightGenerator, …) must be detected by
    structure, not handed to the client as a bogus item. Regression for the
    wishing-well "The First Time is Free" turn-in: OneTimeUseOnlyWishingWellIG
    (base LegendIG) was wrongly given as an item and broke the turn-in."""
    if resolver._items_row("OneTimeUseOnlyWishingWellIG") is None:
        pytest.skip("wishing-well generator not in shipped DB")
    assert resolver.is_real_item("OneTimeUseOnlyWishingWellIG") is False
    # Never resolves to its own generator name (the bogus-item bug).
    assert "OneTimeUseOnlyWishingWellIG" not in resolver.resolve_generator_items(
        "OneTimeUseOnlyWishingWellIG", 20, 3)


def test_unknown_generator_returns_empty(resolver):
    assert resolver.resolve_generator_items("WishingWellIG", 1, 1) == []


def test_empty_input_returns_empty(resolver):
    assert resolver.resolve_generator_items("", 1, 1) == []
    assert resolver.is_real_item("") is False


def test_deep_generator_graph_resolves_without_hanging(resolver):
    """Regression: ``wishingwell1hweaponig`` (an 842-byte IG) hung the resolver for
    6 s+ — the un-indexed ``LOWER(gc_type)`` full-table scan in ``_items_row`` was
    re-run for every node across the recursion + re-roll budget. With the row memo
    it must terminate fast. (It terminates either way — depth/attempts are bounded —
    so a slow regression fails the time bound instead of hanging the suite.)"""
    import time
    resolver.clear_cache()
    start = time.perf_counter()
    out = resolver.resolve_generator_items(
        "wishingwell1hweaponig", level=30, count=1, rng=random.Random(7))
    elapsed = time.perf_counter() - start
    assert isinstance(out, list)                     # bounded → always terminates
    assert elapsed < 2.0, f"resolve took {elapsed:.2f}s — the hang regressed"


def test_items_row_is_memoized(resolver):
    """The row memo serves repeat lookups without re-querying (the hang fix)."""
    calls = {"n": 0}
    from drserver.db import game_database
    orig = game_database.execute_reader

    def counting_reader(sql, params=None):
        if "FROM items WHERE LOWER(gc_type)" in sql:
            calls["n"] += 1
        return orig(sql, params)

    resolver.clear_cache()
    game_database.execute_reader = counting_reader
    try:
        resolver.resolve_generator_items(
            "wishingwellarmorig", level=10, count=1, rng=random.Random(1))
        after_first = calls["n"]
        resolver.resolve_generator_items(
            "wishingwellarmorig", level=10, count=1, rng=random.Random(1))
        after_second = calls["n"]
    finally:
        game_database.execute_reader = orig
    # Second identical resolve adds no new items-row queries (all memoized).
    assert after_first > 0
    assert after_second == after_first
