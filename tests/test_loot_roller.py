"""Loot roller — level/rarity/difficulty-driven drop rolls (loot_roller.py).

Pool-injected + seeded so the rolls are deterministic and DB-free.
"""
import random
import sqlite3

from drserver.data.rarity_helper import ItemRarity
from drserver.managers import loot_roller
from drserver.managers.loot_roller import (
    ArmorPoolItem, PoolItem, is_droppable_newgen_armor, is_gold_generator,
    load_armor_rarity_pool, pick_item, roll_loot, roll_rarity, tier_of)
from drserver.managers.merchants import is_client_droppable_item

# A synthetic pool spanning the real PAL level bands (1, 11, 21, 31, 41).
_POOL = [PoolItem(f"itempal{lvl}.x-{i}", lvl)
         for lvl in (1, 11, 21, 31, 41) for i in range(1, 4)]


def test_tier_of_reads_the_generator_name():
    assert tier_of("DefaultIG") == "default"
    assert tier_of("ChampionIG") == "champion"
    assert tier_of("HeroGG") == "hero"
    assert tier_of("BossIG") == "boss"
    assert tier_of("") == "default"


def test_is_gold_generator():
    assert is_gold_generator("DefaultGG")
    assert is_gold_generator("HeroGG")
    assert not is_gold_generator("DefaultIG")


def test_pick_item_respects_level_band():
    rng = random.Random(1)
    for _ in range(50):
        p = pick_item(_POOL, mob_level=25, rng=rng)
        assert p.level <= 25            # never above the mob
        assert p.level >= 11            # top band at/under 25 is 21 → band >= 11


def test_pick_item_empty_pool_returns_none():
    assert pick_item([], 10, random.Random(0)) is None


def test_roll_rarity_default_favors_common():
    rng = random.Random(7)
    counts = {}
    for _ in range(2000):
        r = roll_rarity("default", 0, rng)
        counts[r] = counts.get(r, 0) + 1
    assert counts[ItemRarity.Normal] > counts.get(ItemRarity.Rare, 0)


def test_roll_rarity_boss_tier_skews_higher_than_default():
    rng = random.Random(7)
    default_mean = sum(int(roll_rarity("default", 0, rng)) for _ in range(2000))
    rng = random.Random(7)
    boss_mean = sum(int(roll_rarity("boss", 0, rng)) for _ in range(2000))
    assert boss_mean > default_mean


def test_difficulty_bonus_raises_rarity():
    rng = random.Random(7)
    base = sum(int(roll_rarity("default", 0, rng)) for _ in range(2000))
    rng = random.Random(7)
    boosted = sum(int(roll_rarity("default", 3, rng)) for _ in range(2000))
    assert boosted > base


def test_roll_loot_gold_generator_yields_scaled_gold():
    # Boss GG always drops (chance 1.0) — deterministic for the amount assertion.
    rolls = roll_loot([("BossGG", 1)], mob_level=5, difficulty="GRUNT",
                      pool=_POOL, rng=random.Random(3))
    assert rolls and all(r.is_gold for r in rolls)
    assert rolls[0].gold_amount > 0


def test_roll_loot_gold_is_probabilistic_not_every_kill():
    # A single DefaultGG activation must NOT drop gold every time (the reported
    # "always money" bug). Over many kills, some drop nothing.
    empties = sum(
        not roll_loot([("DefaultGG", 1)], mob_level=5, difficulty="GRUNT",
                      pool=_POOL, rng=random.Random(s))
        for s in range(200))
    assert empties > 0


def test_roll_loot_default_mob_usually_drops_no_item():
    # The reported "always at least 1 item" bug: a default-tier IG should leave
    # most kills itemless. ~12% chance/activation → the clear majority are empty.
    drops = sum(
        bool([r for r in roll_loot([("DefaultIG", 2)], mob_level=10,
                                   difficulty="GRUNT", pool=_POOL,
                                   rng=random.Random(s)) if not r.is_gold])
        for s in range(400))
    assert drops < 200                         # fewer than half of kills drop an item


def test_roll_loot_item_generator_yields_items_with_rarity_level_and_mod():
    # Boss item drops are NOT guaranteed (chance 0.75), so sample across seeds to
    # collect the drops, then assert every dropped item is well-formed.
    items = [r
             for s in range(50)
             for r in roll_loot([("BossIG", 1)], mob_level=21, difficulty="BOSS",
                                pool=_POOL, rng=random.Random(s))
             if not r.is_gold]
    assert items                              # boss IG is the most generous item tier
    for r in items:
        assert r.gc_type
        assert r.level == 21
        assert r.scale_mod
        assert 0 <= r.rarity <= 5


def test_roll_loot_boss_item_is_not_guaranteed():
    # The 2026-06-21 correction: boss item drops are NOT 100% (the C# server is
    # reference only, not ground truth). A single BossIG activation must leave
    # some kills itemless.
    empties = sum(
        not [r for r in roll_loot([("BossIG", 1)], mob_level=21, difficulty="BOSS",
                                  pool=_POOL, rng=random.Random(s)) if not r.is_gold]
        for s in range(200))
    assert empties > 0


def test_old_gen_drop_rarity_is_derived_from_the_dash_suffix():
    """Old-gen dash-suffix gear encodes its rarity in the -N suffix. The drop must
    stamp the SUFFIX rarity + its deterministic ScaleMod (NOT an independent roll),
    so the color is identical in every context — ground/bag/equip/reload. The old
    independent roll_rarity stamped a contradicting rarity (e.g. Normal/Binder) that
    only some paths honored → the white↔yellow flip (live 2026-07-02)."""
    from drserver.data.rarity_helper import (get_deterministic_scale_mod,
                                             get_rarity_from_tier, get_tier_from_gc_type)
    # tier 6 and tier 10 → Rare (get_rarity_from_tier defaults tier>5 to Rare).
    pool = [PoolItem("platearmor1pal.platearmor1-6", 50),
            PoolItem("splintboots1pal.splintboots1-10", 50)]
    items = [r
             for s in range(60)
             for r in roll_loot([("BossIG", 1)], mob_level=50, difficulty="BOSS",
                                pool=pool, rng=random.Random(s))
             if not r.is_gold]
    assert items
    for r in items:
        expected_rarity = get_rarity_from_tier(get_tier_from_gc_type(r.gc_type))
        assert r.rarity == int(expected_rarity)        # suffix, not the roll
        # ScaleMod is the deterministic per-item pick — same value the serializer's
        # suffix fallback produces, so a lost/persisted ScaleMod can never disagree.
        assert r.scale_mod == get_deterministic_scale_mod(r.gc_type, expected_rarity)


def test_roll_loot_is_deterministic_for_a_seed():
    gens = [("ChampionIG", 1), ("DefaultGG", 1)]
    a = roll_loot(gens, 15, "VETERAN", _POOL, random.Random(99))
    b = roll_loot(gens, 15, "VETERAN", _POOL, random.Random(99))
    assert a == b


def test_roll_loot_empty_pool_drops_no_items_no_crash():
    rolls = roll_loot([("DefaultIG", 1)], 10, "GRUNT", pool=[], rng=random.Random(1))
    assert all(r.is_gold for r in rolls)      # pool empty → no items, no error


# ── Droppable-item filter (the proven client-itemized set) ────────────────────
# A dropped GCObject is deserialized by the client through GCClassRegistry; a
# class the client has no stable schema for desyncs the entity stream and crashes
# with "Zone communication error" (Invalid type tag). The proven-safe set is the
# dash-suffix PAL family the merchant already sells (renders + equips live).

def test_droppable_excludes_deprecated_nonsuffix_classes():
    # The deprecated content classes lack the "-N" dash suffix the client
    # itemizes; dropping one crashed the client (Invalid type tag 100).
    assert not is_client_droppable_item(
        "items.deprecated.deprecatedchildarmorpal.boots036")
    assert not is_client_droppable_item(
        "items.deprecated.deprecatedchildarmorpal.body001")


def test_droppable_accepts_dash_suffix_pal_gear():
    assert is_client_droppable_item("1haxe1pal.1haxe1-1")
    assert is_client_droppable_item("chainarmor1pal.chainarmor1-10")


def test_droppable_excludes_special_families():
    # mythic / prebuilt / partialbuilt / generated need bespoke wire bodies.
    assert not is_client_droppable_item("items.pal.fighterbodypal.partialbuiltmythic001")
    assert not is_client_droppable_item("items.pal.magebootspal.generatedmythic001")


def test_droppable_requires_weapon_or_armor():
    assert not is_client_droppable_item("somethingelsepal.thing-1")


# ── New-gen armor drops (the running client's real IG-driven armor loot) ──────
# The 666 client carries armor in the NEW generation
# (items.pal.<class><slot>pal.<quality>NNN) WITH the items.ig.<class>.* loot
# generators loaded (T0 from GCDictionary). Weapons stay old-gen. See
# [[project_loot_generation_split]].

def test_newgen_armor_predicate_accepts_dict_safe_armor():
    assert is_droppable_newgen_armor("items.pal.magebodypal.normal001")
    assert is_droppable_newgen_armor("items.pal.fighterhelmpal.unique003")
    assert is_droppable_newgen_armor("items.pal.magehelmpal.generatedmythic001")


def test_newgen_armor_predicate_rejects_special_and_other_gens():
    # partialbuilt / prebuilt / seasonal: bespoke wire or absent from client dict.
    assert not is_droppable_newgen_armor("items.pal.fighterbodypal.partialbuiltmythic001")
    assert not is_droppable_newgen_armor("items.pal.magebodypal.prebuiltboss001")
    assert not is_droppable_newgen_armor("items.pal.mageshieldpal.generateduniqueseasonal001")
    # New-gen WEAPONS are NOT in the client dict (old-gen only) → never droppable.
    assert not is_droppable_newgen_armor("items.pal.1haxepal.normal001")
    # Old-gen dash-suffix gear is the OTHER path's job, not this predicate's.
    assert not is_droppable_newgen_armor("1haxe1pal.1haxe1-1")


def test_roll_loot_ignores_armor_pool_when_flag_off():
    # Default OFF: armor_pool present but no roll should be armor. Boss item drops
    # aren't guaranteed, so sample across seeds — no drop may ever be armor, and at
    # least one old-gen item must drop.
    armor = {r: [ArmorPoolItem(f"items.pal.magebodypal.q{r}", 1)] for r in range(6)}
    rolls = [r
             for s in range(50)
             for r in roll_loot([("BossIG", 1)], mob_level=21, difficulty="BOSS",
                                pool=_POOL, rng=random.Random(s), armor_pool=armor)]
    assert not any(r.is_armor for r in rolls)
    assert any(not r.is_gold for r in rolls)        # still drops old-gen items


def test_roll_loot_drops_rarity_appropriate_armor_when_enabled(monkeypatch):
    monkeypatch.setattr(loot_roller, "EMIT_NEWGEN_ARMOR_DROPS", True)
    monkeypatch.setattr(loot_roller, "_ARMOR_DROP_SHARE", 1.0)   # force armor when available
    # Armor available at every rarity → every item slot that drops must be
    # rarity-matched armor (boss item drops aren't guaranteed, so sample seeds).
    armor = {r: [ArmorPoolItem(f"items.pal.magebodypal.q{r}", 1)] for r in range(6)}
    items = [r
             for s in range(50)
             for r in roll_loot([("BossIG", 1)], mob_level=21, difficulty="BOSS",
                                pool=_POOL, rng=random.Random(s), armor_pool=armor)
             if not r.is_gold]
    assert items
    for r in items:
        assert r.is_armor
        assert r.gc_type == f"items.pal.magebodypal.q{r.rarity}"   # rarity-appropriate
        assert r.scale_mod


def _memory_db_with(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE armor (gc_type TEXT)")
    conn.execute("CREATE TABLE item_wire_mods (item_gc_type TEXT, rarity TEXT, "
                 "slot INTEGER, mod_ref TEXT)")
    for gc, rarity, in_armor in rows:
        if in_armor:
            conn.execute("INSERT INTO armor VALUES (?)", (gc,))
        conn.execute("INSERT INTO item_wire_mods VALUES (?,?,0,'m')", (gc, rarity))
    conn.commit()
    return conn


def test_load_armor_rarity_pool_builds_from_db_and_filters(monkeypatch):
    conn = _memory_db_with([
        ("items.pal.magebodypal.normal001", "Normal", True),       # kept → rarity 0
        ("items.pal.magehelmpal.unique002", "Unique", True),       # kept → rarity 4
        ("items.pal.fighterbodypal.partialbuiltmythic001", "Mythic", True),  # excluded (special)
        ("items.pal.1haxepal.normal001", "Normal", False),         # weapon, not in armor table
        ("items.pal.magebodypal.wishingwell01", "WishingWell", True),        # excluded (rarity)
    ])
    monkeypatch.setattr(loot_roller.db, "get_connection", lambda: conn)
    loot_roller.reset_pool()
    pool = load_armor_rarity_pool()
    assert {a.gc_type for a in pool.get(0, [])} == {"items.pal.magebodypal.normal001"}
    assert {a.gc_type for a in pool.get(4, [])} == {"items.pal.magehelmpal.unique002"}
    flat = [a.gc_type for items in pool.values() for a in items]
    assert "items.pal.fighterbodypal.partialbuiltmythic001" not in flat  # special excluded
    assert "items.pal.1haxepal.normal001" not in flat                    # not armor-joined
    loot_roller.reset_pool()
