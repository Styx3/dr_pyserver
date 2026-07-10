"""Client-informed random loot roller — level / rarity / difficulty driven.

Replaces the static placeholder drop. Treasure generators are tiered item (IG)
and gold (GG) generators — ``Default``/``Champion``/``Hero``/``Boss`` (read off the
generator name). The tier sets the rarity distribution + drop count, the mob
level picks the item-level band, and the mob difficulty nudges rarity up. Item
level + rarity come from the rebuilt content tables via ``rarity_helper``
(``get_item_level`` decodes the PAL tier; a rolled rarity → a random ScaleMod).

This is a data-driven approximation of the client ``TreasureGenerator``: the exact
per-IG drop weights would need RE of the client loot routine, but this rolls REAL
items from the content pools at level-appropriate rarities instead of one fixed
placeholder. The rolled rarity/level/ScaleMod ride on the ``DroppedItem`` so the
pickup path materializes the right item.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..core import log
from ..data.rarity_helper import (ItemRarity, get_deterministic_scale_mod,
                                  get_item_level, get_rarity_from_tier,
                                  get_tier_from_gc_type, scale_mods_for)
from ..db import game_database as db

# ── New-generation armor drops (the running client's REAL armor loot) ─────────
#
# The 666 client is a HYBRID item generation ([[project_loot_generation_split]],
# T0 from GCDictionary.dict): WEAPONS are old-gen quality-variant items with NO
# loot generators in the client, but ARMOR is the NEW generation
# (``items.pal.<class><slot>pal.<quality>NNN``) WITH the ``items.ig.<class>.*``
# generators actually loaded. So the extracter IG tree — already resolved to
# (item, rarity) -> mods in the ``item_wire_mods`` table — IS the client's real
# armor loot system, and we can drop rarity-appropriate REAL armor from it.
#
# Their drop WIRE body is UNVALIDATED, though: merchants only ever sell the
# dash-suffix old-gen gear, so no new-gen armor has crossed the wire yet. Keep
# this OFF until a live UNPATCHED test confirms no "Zone communication error";
# flipping it on is a one-line change.
EMIT_NEWGEN_ARMOR_DROPS = False

# When armor is available at the rolled rarity, the chance a given item slot
# drops armor (vs the old-gen weapon/flat pool). An approximation until the
# top-level generator weights are RE'd from the binary (Phase 2).
_ARMOR_DROP_SHARE = 0.5

# New-gen armor class shape: items.pal.<letters>pal.<quality>NNN. The numbered
# weapon families (items.pal.1haxepal.*) start with a digit so they never match
# — exactly right, since new-gen weapons are NOT in the client dict.
_NEWGEN_ARMOR_RE = re.compile(r"^items\.pal\.[a-z]+pal\.")
# Excluded armor families: bespoke wire bodies and/or absent from the client
# dict (verified 2026-06-17 — every dict-missing new-gen armor was one of these).
_NEWGEN_ARMOR_EXCLUDE = ("partialbuilt", "prebuilt", "seasonal")

# item_wire_mods rarity strings -> ItemRarity int. Seasonal / WishingWell are not
# base rarities and are skipped (event/well loot, not mob drops).
_RARITY_STR_TO_INT = {
    "normal": 0, "superior": 1, "magic": 2, "magical": 2,
    "rare": 3, "unique": 4, "mythic": 5,
}


def is_droppable_newgen_armor(gc_type: str) -> bool:
    """True iff a new-gen armor class is in the running client's GCDictionary and
    safe to drop.

    The dict-confirmed droppable subset (T0) is every new-gen armor class EXCEPT
    the ``partialbuilt`` / ``prebuilt`` / ``seasonal`` families. This is a
    SEPARATE predicate from :func:`merchants.is_client_droppable_item` (which
    governs the dash-suffix old-gen weapon/armor path) so the two generations
    never cross-contaminate. Apply it only to rows of the ``armor`` table.
    """
    lower = (gc_type or "").lower()
    if not _NEWGEN_ARMOR_RE.match(lower):
        return False
    return not any(f in lower for f in _NEWGEN_ARMOR_EXCLUDE)

# Rarity weights per generator tier — (Normal, Superior, Magical, Rare, Unique, Mythic).
_TIER_WEIGHTS = {
    "default":  (62, 24, 9, 4, 1, 0),
    "champion": (40, 28, 18, 9, 4, 1),
    "hero":     (18, 26, 28, 18, 8, 2),
    "boss":     (6, 16, 26, 28, 17, 7),
}
# Probability a single IG generator activation yields ANY item, by tier. Most
# trash kills drop NOTHING — an item is the exception, not the rule. (The old
# code dropped 0-N items per activation AND multiplied by the DB treasure_count,
# so common count-2 mobs almost always dropped an item — far too generous. These
# chances are an approximation until the real TreasureGenerator weights are RE'd
# from the binary, Phase 2.)
#
# Boss item drops are NOT guaranteed (user-confirmed against the live client
# 2026-06-21). The C# server appears to drop boss loot 100% of the time, but the
# C# server is REFERENCE ONLY — never ground truth — so we don't inherit its
# guarantee. Boss stays the most generous tier (a multi-IG boss almost always
# drops something) but any single activation can come up empty.
_TIER_ITEM_CHANCE = {"default": 0.06, "champion": 0.13, "hero": 0.25, "boss": 0.75}
# When an activation DOES drop, how many items (inclusive range).
_TIER_ITEM_COUNT = {"default": (1, 1), "champion": (1, 1), "hero": (1, 2), "boss": (1, 3)}
# Probability a single GG generator activation yields a gold pile. Gold is common
# but NOT every kill (the old code dropped gold on every activation).
_TIER_GOLD_CHANCE = {"default": 0.20, "champion": 0.30, "hero": 0.45, "boss": 1.0}
# Gold multiplier per GG generator tier.
_TIER_GOLD_MULT = {"default": 1, "champion": 2, "hero": 3, "boss": 5}
# Mob difficulty nudges the rolled rarity up by N steps (GRUNT is the baseline).
_DIFFICULTY_BONUS = {
    "GRUNT": 0, "GRUNTRECRUIT": 0, "GRUNTFODDER": 0, "GRUNTVETERAN": 1,
    "VETERAN": 1, "CHAMPION": 2, "HERO": 3, "MINIBOSS": 3, "BOSS": 4,
}


@dataclass(frozen=True)
class PoolItem:
    gc_type: str
    level: int


@dataclass(frozen=True)
class ArmorPoolItem:
    """One new-gen armor item available at a given rarity (mods resolve downstream
    from ``item_wire_mods`` by ``(gc_type, rarity)`` — the merchant mechanism)."""
    gc_type: str
    level: int


@dataclass
class LootRoll:
    """One rolled drop — gold pile or an item with its rolled rarity/level/mod."""
    is_gold: bool
    gc_type: str = ""
    level: int = 1
    rarity: int = 0
    scale_mod: str = ""
    gold_amount: int = 0
    is_armor: bool = False   # new-gen armor (IG-driven), vs old-gen weapon/flat pick


def tier_of(generator_name: str) -> str:
    n = (generator_name or "").lower()
    if "boss" in n:
        return "boss"
    if "hero" in n:
        return "hero"
    if "champion" in n:
        return "champion"
    return "default"


def is_gold_generator(generator_name: str) -> bool:
    return (generator_name or "").upper().endswith("GG")


def _weighted_index(weights: Sequence[int], rng: random.Random) -> int:
    total = sum(weights)
    if total <= 0:
        return 0
    r = rng.randint(1, total)
    acc = 0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return i
    return len(weights) - 1


def roll_rarity(tier: str, difficulty_bonus: int, rng: random.Random) -> ItemRarity:
    idx = _weighted_index(_TIER_WEIGHTS.get(tier, _TIER_WEIGHTS["default"]), rng)
    idx = min(idx + max(0, difficulty_bonus), len(ItemRarity) - 1)
    return ItemRarity(idx)


def pick_item(pool: Sequence[PoolItem], mob_level: int,
              rng: random.Random) -> Optional[PoolItem]:
    """Pick a random item from the highest level band at or under the mob level."""
    if not pool:
        return None
    lvl = max(1, mob_level)
    eligible = [p for p in pool if p.level <= lvl]
    if not eligible:
        eligible = sorted(pool, key=lambda p: p.level)[:50]   # all above the mob — take lowest
    top = max(p.level for p in eligible)
    band = [p for p in eligible if p.level >= top - 10] or eligible
    return rng.choice(band)


def _pick_armor(armor_pool, rarity: ItemRarity, mob_level: int,
                rng: random.Random) -> Optional[ArmorPoolItem]:
    """Pick a level-eligible new-gen armor item at ``rarity``, or None.

    Gated by :data:`EMIT_NEWGEN_ARMOR_DROPS`; returns None (→ fall back to the
    old-gen weapon/flat pick) when disabled, no pool, no rarity match, or the
    per-slot armor share isn't hit.
    """
    if not (EMIT_NEWGEN_ARMOR_DROPS and armor_pool):
        return None
    choices = armor_pool.get(int(rarity))
    if not choices:
        return None
    eligible = [a for a in choices if a.level <= max(1, mob_level)] or list(choices)
    if rng.random() >= _ARMOR_DROP_SHARE:
        return None
    return rng.choice(eligible)


def roll_loot(generators, mob_level: int, difficulty: str,
              pool: Sequence[PoolItem],
              rng: Optional[random.Random] = None,
              armor_pool: Optional[Dict[int, List[ArmorPoolItem]]] = None) -> List[LootRoll]:
    """Roll the full drop list for one kill from its treasure generators.

    When :data:`EMIT_NEWGEN_ARMOR_DROPS` is on and ``armor_pool`` has items at the
    rolled rarity, a slot may drop rarity-appropriate REAL new-gen armor (the
    client's IG-driven armor loot); otherwise it draws from the old-gen
    weapon/flat ``pool`` exactly as before.
    """
    rng = rng or random.Random()
    diff_bonus = _DIFFICULTY_BONUS.get((difficulty or "").upper(), 0)
    rolls: List[LootRoll] = []
    for name, count in (generators or []):
        cnt = max(1, int(count or 1))
        tier = tier_of(name)
        if is_gold_generator(name):
            gold_chance = _TIER_GOLD_CHANCE.get(tier, 0.40)
            for _ in range(cnt):
                if rng.random() >= gold_chance:
                    continue                       # this activation drops no gold
                amt = rng.randint(1, 8) * max(1, mob_level) * _TIER_GOLD_MULT.get(tier, 1)
                rolls.append(LootRoll(is_gold=True, gold_amount=amt))
            continue
        item_chance = _TIER_ITEM_CHANCE.get(tier, 0.12)
        cmin, cmax = _TIER_ITEM_COUNT.get(tier, (1, 1))
        for _ in range(cnt):
            if rng.random() >= item_chance:
                continue                           # this activation drops no item
            for _ in range(rng.randint(cmin, cmax)):
                rarity = roll_rarity(tier, diff_bonus, rng)
                armor = _pick_armor(armor_pool, rarity, mob_level, rng)
                if armor is not None:
                    rolls.append(LootRoll(
                        is_gold=False, gc_type=armor.gc_type, level=max(1, mob_level),
                        rarity=int(rarity), scale_mod=rng.choice(scale_mods_for(rarity)),
                        is_armor=True))
                    continue
                item = pick_item(pool, mob_level, rng)
                if item is None:
                    continue
                # Old-gen dash-suffix gear encodes its OWN rarity in the -N suffix
                # (C# model: every write path paints color via
                # GetRarityFromTier(GetTierFromGcType) → GetModifierGCClass). The
                # independent `rarity` roll above only selects new-gen armor; for
                # an old-gen pick it must NOT override the suffix, or the stamped
                # rarity/ScaleMod contradicts the item name and the color flips
                # white↔yellow between ground/bag/equip/reload (live 2026-07-02).
                # Derive rarity + a DETERMINISTIC ScaleMod from the item so every
                # context agrees.
                item_rarity = get_rarity_from_tier(
                    get_tier_from_gc_type(item.gc_type))
                rolls.append(LootRoll(
                    is_gold=False, gc_type=item.gc_type, level=max(1, mob_level),
                    rarity=int(item_rarity),
                    scale_mod=get_deterministic_scale_mod(item.gc_type, item_rarity)))
    return rolls


_pool_cache: Optional[List[PoolItem]] = None


def load_pool() -> List[PoolItem]:
    """Droppable item pool = the itemized PAL weapons/armor, level via PAL tier."""
    global _pool_cache
    if _pool_cache:                       # only cache a non-empty pool (retry if DB wasn't ready)
        return _pool_cache
    from .merchants import is_client_droppable_item

    pool: List[PoolItem] = []
    try:
        for table in ("weapons", "armor"):
            for row in db.execute_reader(f"SELECT gc_type FROM {table}").fetchall():
                gc = db.get_string(row, "gc_type")
                # Only the dash-suffix PAL family the client can actually
                # deserialize as a ground entity. The looser "'pal' in gc" filter
                # also matched the deprecated content classes (no -N suffix), and
                # dropping one crashed the client with a "Zone communication
                # error" entity-stream desync (Invalid type tag 100, live 2026-06-17).
                # Share the merchant's proven-safe predicate so the two never drift.
                if gc and "pal" in gc.lower() and is_client_droppable_item(gc):
                    pool.append(PoolItem(gc, get_item_level(gc)))
    except Exception as ex:  # noqa: BLE001 — content DB may be absent in some contexts
        log.error(f"[LootRoller] pool load error: {ex}")
    if pool:
        _pool_cache = pool
        log.info(f"[LootRoller] loaded item pool: {len(pool)} PAL items")
    return pool


_armor_pool_cache: Optional[Dict[int, List[ArmorPoolItem]]] = None


def load_armor_rarity_pool() -> Dict[int, List[ArmorPoolItem]]:
    """Rarity-indexed pool of new-gen armor the client's IGs actually drop.

    Built from ``item_wire_mods`` (the baked IG -> (item, rarity) -> mods chain)
    joined to the ``armor`` table, keeping only the dict-safe new-gen armor
    classes (:func:`is_droppable_newgen_armor`). Empty (and uncached, so it
    retries) when the table is absent — e.g. an older DB — so the roller simply
    falls back to the old-gen pool.
    """
    global _armor_pool_cache
    if _armor_pool_cache is not None:
        return _armor_pool_cache
    pool: Dict[int, List[ArmorPoolItem]] = {}
    try:
        rows = db.execute_reader(
            "SELECT DISTINCT m.item_gc_type AS gc, m.rarity AS rarity "
            "FROM item_wire_mods m JOIN armor a ON a.gc_type = m.item_gc_type"
        ).fetchall()
        for row in rows:
            gc = db.get_string(row, "gc")
            rarity_int = _RARITY_STR_TO_INT.get(db.get_string(row, "rarity").lower())
            if rarity_int is None or not is_droppable_newgen_armor(gc):
                continue
            pool.setdefault(rarity_int, []).append(ArmorPoolItem(gc, get_item_level(gc)))
    except Exception as ex:  # noqa: BLE001 — item_wire_mods may be absent in some DBs
        log.error(f"[LootRoller] armor pool load error: {ex}")
    if pool:
        _armor_pool_cache = pool
        total = sum(len(v) for v in pool.values())
        log.info(f"[LootRoller] loaded new-gen armor pool: {total} items across "
                 f"{len(pool)} rarities")
    return pool


def reset_pool() -> None:
    """Test hook — force a reload on next access."""
    global _pool_cache, _armor_pool_cache
    _pool_cache = None
    _armor_pool_cache = None
