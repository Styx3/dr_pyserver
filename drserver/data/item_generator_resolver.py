"""Resolve an item-generator gc_type into concrete item gc_types.

Port of the authored-generator resolution the C# server runs for quest rewards
and on-accept items (``GCObjectGeneratorTable.GenerateAuthoredGeneratorLoot`` /
``TryGiveDirectAuthoredQuestReward`` / ``IsDirectAuthoredRewardItem``,
GameServer.Types.cs). A quest's ``RewardItemGenerator`` / ``OnAcceptItemGenerator``
is one of two shapes, both handled here:

* **A direct authored item** — the gc_type is itself a real item (present in
  ``items`` with a non-generator base, or in ``weapons`` / ``armor``). Give it
  directly. ~92 % of the shipped reward generators are this shape.
* **An ``ItemGeneratorTable``** (``items.base_type == 'ItemGeneratorTable'``)
  whose children are ``SingleItemGenerator`` nodes (``Chance`` + ``Item`` +
  optional ``MinLevel`` / ``MaxLevel``) and ``ItemGeneratorLink`` nodes
  (``Chance`` + ``LinkedGenerator``). Pick one child weighted by ``Chance``,
  filtered by the player's level, following links into nested generators.

A generator can be referenced by a dotted sub-entry (``SuperiorJewelryIG.Ring``,
``keyig.D08_Q05_1_Key``): the longest dotted prefix that is itself a generator
table is loaded and the remaining segments navigate to the child node.

The generator definitions live in the shipped ``items`` table (``raw_json`` holds
the inheritance-flattened node tree produced by ``items_importer``). This module
is read-only and resolution happens only on the rare turn-in / accept path, so it
queries the DB directly without caching.
"""
from __future__ import annotations

import json
import random
from typing import List, Optional

from ..core import log
from ..db import game_database as db

# Props that mark a node as a generator entry: a direct item, a link to another
# generator (ItemGeneratorLink), or a RandomItemGenerator's ItemGenerator link.
_GENERATOR_PROPS = ("Item", "LinkedGenerator", "ItemGenerator")
_MAX_DEPTH = 8
# Re-roll budget per reward item to skip dead-end links (generators absent from
# our content, e.g. the items.ig.* weapon-IG tree) so a resolvable link still pays out.
_ROLL_ATTEMPTS = 12


# ── DB lookups ───────────────────────────────────────────────────────────────

# Per-(db, gc_type) memo of the items-row lookup. ``_items_row``'s
# ``WHERE LOWER(gc_type)=LOWER(:g)`` is a full-table scan (no functional index on
# the 11k+ rows); the deep generator graphs re-probe the same gc_types (and many
# missing dotted prefixes) across ``_MAX_DEPTH`` × ``_ROLL_ATTEMPTS``, which
# multiplied a single resolve into seconds — the wishing-well 1H-weapon IG took
# 6 s+. Content is static at runtime, so memoizing collapses every repeat lookup
# (hits AND misses) to O(1). Keyed by db path so a different content DB (tests,
# re-import) never reads a stale cache.
_MISSING = object()
_ROW_CACHE: dict = {}


def clear_cache() -> None:
    """Drop the items-row memo — call when the content DB is rebuilt/swapped."""
    _ROW_CACHE.clear()


def _items_row(gc_type: str) -> Optional[tuple]:
    """``(base_type, raw_json, width, height)`` for an ``items`` row, or None.

    ``width``/``height`` are the inventory grid dims: real placeable items carry
    them; generator rows (``*IG`` item tables, ``*GG`` gold generators) leave them
    NULL — the discriminator :func:`is_real_item` uses to reject a generator
    gc_type masquerading as an item. Memoized per (db, gc_type) — see ``_ROW_CACHE``."""
    key = (db.get_db_path(), gc_type.lower())
    cached = _ROW_CACHE.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    try:
        row = db.execute_reader(
            "SELECT base_type, raw_json, inventory_width, inventory_height "
            "FROM items WHERE LOWER(gc_type)=LOWER(:g)",
            {"g": gc_type}).fetchone()
    except Exception as ex:  # noqa: BLE001 — content DB may be absent in some contexts
        log.debug(f"[ItemGen] items lookup error for {gc_type}: {ex}")
        return None                                # transient error — do not cache
    result = None if row is None else (
        db.get_string(row, "base_type"), db.get_string(row, "raw_json"),
        db.get_int(row, "inventory_width", 0), db.get_int(row, "inventory_height", 0))
    _ROW_CACHE[key] = result
    return result


def _concrete_item_exists(gc_type: str) -> bool:
    """``gc_type`` is a real, placeable item — present in ``items`` with inventory
    dims (NOT a dimensionless generator row), or in ``weapons`` / ``armor``."""
    row = _items_row(gc_type)
    if row is not None:
        return row[2] > 0 and row[3] > 0          # real item iff it has grid dims
    for table in ("weapons", "armor"):
        try:
            hit = db.execute_reader(
                f"SELECT 1 FROM {table} WHERE LOWER(gc_type)=LOWER(:g) LIMIT 1",
                {"g": gc_type}).fetchone()
        except Exception:  # noqa: BLE001
            hit = None
        if hit is not None:
            return True
    return False


def is_real_item(gc_type: str) -> bool:
    """True iff ``gc_type`` is a concrete, deserializable item — NOT a generator.
    Mirrors C# ``IsDirectAuthoredRewardItem`` (``FindItem != null`` plus the
    not-a-generator check). A generator can sit in the ``items`` table under many
    bases (``ItemGeneratorTable``/``LegendIG``/``RandomItemGenerator``/
    ``*LightGenerator``/``BaseBossLootIG`` …), so it is detected by STRUCTURE
    (:func:`_load_generator_node`), never by base_type alone — otherwise the
    generator gc_type is handed to the client as a bogus item (e.g. the wishing
    well's ``OneTimeUseOnlyWishingWellIG``, which broke that quest's turn-in)."""
    if not gc_type:
        return False
    if _load_generator_node(gc_type) is not None:
        return False
    return _concrete_item_exists(gc_type)


# ── node helpers ─────────────────────────────────────────────────────────────

def _prop(props: dict, key: str) -> Optional[str]:
    """Case-insensitive property fetch (raw_json keeps the authored casing)."""
    if not isinstance(props, dict):
        return None
    if key in props:
        return props[key]
    lk = key.lower()
    for k, v in props.items():
        if k.lower() == lk:
            return v
    return None


def _children(node: dict) -> List[dict]:
    """Named children (dict values) followed by anonymous children."""
    out: List[dict] = []
    children = node.get("children") or {}
    if isinstance(children, dict):
        out.extend(children.values())
    elif isinstance(children, list):
        out.extend(children)
    out.extend(node.get("anonymous_children") or [])
    return [c for c in out if isinstance(c, dict)]


def _navigate(node: dict, path: List[str]) -> Optional[dict]:
    """Descend ``node`` along named child segments (case-insensitive)."""
    cur = node
    for seg in path:
        children = cur.get("children") or {}
        nxt = None
        if isinstance(children, dict):
            nxt = children.get(seg)
            if nxt is None:
                sl = seg.lower()
                nxt = next((v for k, v in children.items() if k.lower() == sl), None)
        if nxt is None:
            return None
        cur = nxt
    return cur


def _node_is_generator(node: Optional[dict]) -> bool:
    """A node is a generator if it — or any direct child — carries a generator
    prop (``Item`` / ``LinkedGenerator`` / ``ItemGenerator``). Detects generators
    by STRUCTURE rather than base_type, so every generator base resolves
    (ItemGeneratorTable, RandomItemGenerator, LegendIG, *LightGenerator,
    BaseBossLootIG, …) — not just ItemGeneratorTable."""
    if not isinstance(node, dict):
        return False
    props = node.get("properties") or {}
    if any(_prop(props, k) for k in _GENERATOR_PROPS):
        return True
    for child in _children(node):
        cp = child.get("properties") or {}
        if any(_prop(cp, k) for k in _GENERATOR_PROPS):
            return True
    return False


def _generator_node_from_row(row: Optional[tuple]) -> Optional[dict]:
    """Parse an ``items`` row into its generator node, or None when the row is
    absent / not a generator."""
    if row is None or not row[1]:
        return None
    try:
        node = json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        return None
    return node if _node_is_generator(node) else None


def _load_generator_node(gc_type: str) -> Optional[dict]:
    """The generator node for ``gc_type`` (top-level generator or dotted
    sub-entry navigated from the longest generator prefix), or None when
    ``gc_type`` is not a generator."""
    node = _generator_node_from_row(_items_row(gc_type))
    if node is not None:
        return node
    parts = gc_type.split(".")
    for i in range(len(parts) - 1, 0, -1):
        root = _generator_node_from_row(_items_row(".".join(parts[:i])))
        if root is not None:
            child = _navigate(root, parts[i:])
            if child is not None:
                return child
    return None


def _level_ok(props: dict, level: int) -> bool:
    for key, lo in (("MaxLevel", False), ("MinLevel", True)):
        raw = _prop(props, key)
        if raw in (None, ""):
            continue
        try:
            bound = int(float(raw))
        except (ValueError, TypeError):
            continue
        if bound <= 0:
            continue
        if lo and level < bound:
            return False
        if not lo and level > bound:
            return False
    return True


def _chance(props: dict) -> float:
    raw = _prop(props, "Chance")
    try:
        c = float(raw) if raw not in (None, "") else 1.0
    except (ValueError, TypeError):
        c = 1.0
    return c if c > 0 else 1.0


def _weighted_choice(items: List[dict], weights: List[float],
                     rng: random.Random) -> dict:
    total = sum(weights)
    if total <= 0:
        return rng.choice(items)
    pick = rng.random() * total
    upto = 0.0
    for item, w in zip(items, weights):
        upto += w
        if pick < upto:
            return item
    return items[-1]


def _roll_one(node: Optional[dict], level: int, rng: random.Random,
              depth: int) -> Optional[str]:
    """Resolve a single concrete item gc_type from a generator node."""
    if node is None or depth > _MAX_DEPTH:
        return None
    props = node.get("properties") or {}

    item = _prop(props, "Item")
    if item:
        return item if _level_ok(props, level) else None

    # A nested generator: ItemGeneratorLink uses ``LinkedGenerator``;
    # RandomItemGenerator uses ``ItemGenerator`` (its ``ItemModGenerator*`` mod
    # rolls are not needed to name the base reward item).
    linked = _prop(props, "LinkedGenerator") or _prop(props, "ItemGenerator")
    if linked:
        sub = _load_generator_node(linked)
        if sub is not None:
            return _roll_one(sub, level, rng, depth + 1)
        return linked if is_real_item(linked) else None

    candidates: List[dict] = []
    weights: List[float] = []
    for child in _children(node):
        cp = child.get("properties") or {}
        if not _level_ok(cp, level):
            continue
        if not (_prop(cp, "Item") or _prop(cp, "LinkedGenerator")
                or _prop(cp, "ItemGenerator") or _children(child)):
            continue
        candidates.append(child)
        weights.append(_chance(cp))
    if not candidates:
        return None
    chosen = _weighted_choice(candidates, weights, rng)
    return _roll_one(chosen, level, rng, depth + 1)


# ── public API ───────────────────────────────────────────────────────────────

def resolve_generator_items(gc_type: str, level: int = 1, count: int = 1,
                            rng: Optional[random.Random] = None) -> List[str]:
    """Resolve a reward/on-accept generator gc_type to up to ``count`` concrete
    item gc_types.

    Returns ``[gc_type] * count`` when it is a direct authored item, a list of
    rolled items when it is a generator table, or ``[]`` when it cannot be
    resolved (the caller should log a missing-generator notice, matching C#).
    """
    if not gc_type:
        return []
    rng = rng or random.Random()
    level = max(1, int(level or 1))
    count = max(1, int(count or 1))

    node = _load_generator_node(gc_type)
    if node is None:
        return [gc_type] * count if is_real_item(gc_type) else []

    out: List[str] = []
    for _ in range(count):
        # Re-roll past dead-end links: many generators (e.g. the wishing well's
        # dominant Mythic→MythicIG) route into the items.ig.* weapon-IG tree that
        # is absent / crash-prone in our content ([[project_loot_generation_split]]),
        # which resolves to nothing. Retrying lets the resolvable links
        # (jewelry/armor/WishingWellSpecificIG) still produce a reward. Quest-only
        # path, so this never affects mob-loot distribution.
        picked = None
        for _ in range(_ROLL_ATTEMPTS):
            picked = _roll_one(node, level, rng, 0)
            if picked:
                break
        if picked:
            out.append(picked)
    return out
