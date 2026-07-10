"""Rebuild the ``items`` / ``weapons`` / ``armor`` content tables from ``.gc``
ground truth.

Fourth (and largest) domain of the ``.gc`` -> SQLite importer, after ``skills``,
``quests`` and ``creatures`` (see ``gc_parser`` / ``gc_database`` and the sibling
``*_importer`` modules).

Unlike the prior three, the item content does **not** live in the extracted
client tree (``extracter/``); its leaf naming there (``1HAxePAL.Normal001``) is a
*newer* generation that does not match the live DB. The generation the live DB
was actually built from is the **numbered-PAL** tree shipped flat in the C#
``DR_Server`` build (``DR_Server/Build/Database/gc/`` — ``1HAxe1PAL.gc`` →
``1HAxe1-1``). The *client's own* ``GCDictionary.dict`` enumerates exactly these
numbered-PAL paths, so they are the client-validated form.

The legacy emulator ``items`` table is a heterogeneous, partly-fabricated bag:
~26% of its keys resolve to **no node anywhere in the gc tree** (mis-keyed
modifiers such as ``amuletmodpal.mod1`` — the real node is
``AmuletModPAL.Superior.Mod1`` — and pure inventions under ``world.*`` /
``skills.*``), its descriptive columns (label / damage / base_type) are empty or
wrong, and its weapon/armor classification is unreliable.

Selection is therefore **principled, by extends-root** — the same shape as
creatures' ``StockUnit`` test. A node is an item iff its ``extends`` chain
terminates at one of the native item base classes:

    weapons : MeleeWeapon, RangedWeapon
    armor   : Armor
    item    : Item, ActiveItem, ItemAttributeModifier, Attribute,
              RandomItemGenerator, ItemGeneratorTable, AttributeModifier

``items`` is the **superset** (every item-rooted node, carrying a ``category``);
``weapons`` and ``armor`` are the weapon-/armor-rooted subsets. ``gc_type`` is the
lowercased dotted content path (``{filestem}.{child path}``), matching the live
tables and the case-insensitive lookups in the consumers.

Three numeric columns have **no counterpart in the ``.gc``** and must never be
fabricated: ``mod_count`` (drives ``GCObject`` wire serialization via
``item_stat_database``), ``gc_gold_value`` and ``gold_value``. ``collect_item_rows``
leaves ``mod_count`` ``None`` and seeds the gold columns from the ``.gc``
``GoldValue`` only as a fallback; :func:`rebuild_items_table` then overlays the
**verbatim** live values by ``gc_type`` before writing, and carries forward any
live row still referenced by a character or merchant that the selector missed —
so the rebuild never orphans live player/merchant state.

Existing schema columns are preserved (the consumers — ``item_stat_database``,
``admin_server``, ``merchants`` — read named columns), with ``source_file`` and a
lossless ``raw_json`` added.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .gc_database import GCDatabase
from .gc_parser import GCNode, parse_file

# ── native item base classes (extends-chain roots) ──
_WEAPON_ROOTS = {"meleeweapon", "rangedweapon"}
_ARMOR_ROOTS = {"armor"}
_ITEM_ROOTS = {
    "item", "activeitem", "itemattributemodifier", "attribute",
    "randomitemgenerator", "itemgeneratortable", "attributemodifier",
}
_ALL_ROOTS = _WEAPON_ROOTS | _ARMOR_ROOTS | _ITEM_ROOTS

# ── schemas (existing live columns kept for consumer drop-in + source_file/raw_json) ──
_CREATE_ITEMS = """
CREATE TABLE items (
    gc_type          TEXT PRIMARY KEY,
    category         TEXT,
    label            TEXT,
    base_type        TEXT,
    inventory_icon   TEXT DEFAULT '',
    ground_object    TEXT DEFAULT '',
    stackable        INTEGER DEFAULT 0,
    inventory_width  INTEGER DEFAULT 1,
    inventory_height INTEGER DEFAULT 1,
    max_stack_size   INTEGER DEFAULT 1,
    mod_count        INTEGER,
    drop_level       INTEGER DEFAULT 0,
    level_req        INTEGER DEFAULT 0,
    gc_gold_value    REAL DEFAULT 0,
    source_file      TEXT,
    raw_json         TEXT
)
"""

# weapons and armor share a schema (the legacy DB defined them identically).
_GEAR_COLUMNS_SQL = """
    gc_type          TEXT PRIMARY KEY,
    label            TEXT,
    base_type        TEXT,
    slot             TEXT DEFAULT '',
    level            INTEGER DEFAULT 0,
    class_req        TEXT DEFAULT '',
    description      TEXT DEFAULT '',
    gold_value       REAL DEFAULT 0,
    defense_rating   REAL DEFAULT 0,
    damage           REAL DEFAULT 0,
    weapon_range     INTEGER DEFAULT 0,
    cooldown         REAL DEFAULT 1.0,
    slot_type        TEXT DEFAULT '',
    weapon_class     TEXT DEFAULT '',
    inventory_width  INTEGER DEFAULT 1,
    inventory_height INTEGER DEFAULT 1,
    inventory_icon   TEXT DEFAULT '',
    ground_object    TEXT DEFAULT '',
    equipable        INTEGER DEFAULT 1,
    mod_count        INTEGER,
    gc_gold_value    REAL DEFAULT 0,
    source_file      TEXT,
    raw_json         TEXT
"""
_CREATE_WEAPONS = f"CREATE TABLE weapons ({_GEAR_COLUMNS_SQL})"
_CREATE_ARMOR = f"CREATE TABLE armor ({_GEAR_COLUMNS_SQL})"
_CREATE_WEAPONS_IDX = "CREATE INDEX idx_weapons_gc ON weapons(gc_type COLLATE NOCASE)"
_CREATE_ARMOR_IDX = "CREATE INDEX idx_armor_gc ON armor(gc_type COLLATE NOCASE)"

_ITEM_COLUMNS = [
    "gc_type", "category", "label", "base_type", "inventory_icon",
    "ground_object", "stackable", "inventory_width", "inventory_height",
    "max_stack_size", "mod_count", "drop_level", "level_req", "gc_gold_value",
    "source_file", "raw_json",
]
_WEAPON_COLUMNS = [
    "gc_type", "label", "base_type", "slot", "level", "class_req",
    "description", "gold_value", "defense_rating", "damage", "weapon_range",
    "cooldown", "slot_type", "weapon_class", "inventory_width",
    "inventory_height", "inventory_icon", "ground_object", "equipable",
    "mod_count", "gc_gold_value", "source_file", "raw_json",
]
_ARMOR_COLUMNS = list(_WEAPON_COLUMNS)

# Gold columns to preserve verbatim from the live tables, per table.
_PRESERVE_BY_TABLE = {
    "items": ("mod_count", "gc_gold_value"),
    "weapons": ("mod_count", "gc_gold_value", "gold_value"),
    "armor": ("mod_count", "gc_gold_value", "gold_value"),
}


def _insert_sql(table: str, columns: list[str]) -> str:
    return (f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + c for c in columns)})")


# ── value accessors (None when absent) ──

def _s(desc: GCNode | None, key: str) -> str | None:
    if desc is None or not desc.has_property(key):
        return None
    return desc.get_string(key) or None


def _i(desc: GCNode | None, key: str) -> int | None:
    return desc.get_int(key) if desc is not None and desc.has_property(key) else None


def _f(desc: GCNode | None, key: str) -> float | None:
    return desc.get_float(key) if desc is not None and desc.has_property(key) else None


def _b(desc: GCNode | None, key: str) -> int | None:
    return ((1 if desc.get_bool(key) else 0)
            if desc is not None and desc.has_property(key) else None)


# ── tree helpers ──

def build_item_db(gc_dir: str) -> GCDatabase:
    """Load the whole flat ``gc/`` directory. No ``dotted_prefix``: top node name
    == filename stem, and ``extends`` paths (``1HMeleeWeaponPAL.1HAxe1``) resolve
    against the recursively-registered child paths."""
    db = GCDatabase()
    if os.path.isdir(gc_dir):
        db.load_tree(gc_dir)
    return db


def _root_extends(db: GCDatabase, node: GCNode) -> str:
    """Name of the class a node's ``extends`` chain terminates at."""
    seen: set[str] = set()
    cur: GCNode | None = node
    while cur is not None and cur.extends and cur.extends.lower() not in seen:
        seen.add(cur.extends.lower())
        nxt = db.resolve(cur.extends)
        if nxt is None:
            return cur.extends.rsplit(".", 1)[-1]
        cur = nxt
    return cur.name if cur is not None else "?"


def _classify(root: str) -> str | None:
    r = root.lower()
    if r in _WEAPON_ROOTS:
        return "weapons"
    if r in _ARMOR_ROOTS:
        return "armor"
    if r in _ITEM_ROOTS:
        return "item"
    return None


def _node_to_dict(node: GCNode) -> dict:
    """Lossless recursive serialization of a (flattened) node for ``raw_json``."""
    return {
        "name": node.name,
        "extends": node.extends,
        "is_static": node.is_static,
        "is_anonymous": node.is_anonymous,
        "properties": dict(node.properties),
        "children": {k: _node_to_dict(v) for k, v in node.children.items()},
        "anonymous_children": [_node_to_dict(c) for c in node.anonymous_children],
    }


# ── row builders ──

def _base_type(node: GCNode) -> str | None:
    """The immediate ``extends`` target's last path segment (the PAL family),
    e.g. ``1HMeleeWeaponPAL.1HAxe1`` -> ``1HAxe1``."""
    if not node.extends:
        return None
    return node.extends.rsplit(".", 1)[-1] or None


def _gc_gold(desc: GCNode | None) -> float | None:
    return _f(desc, "GoldValue")


def _item_row(gc_type: str, category: str, node: GCNode, merged: GCNode,
              source_file: str) -> dict:
    desc = merged.get_child("Description")
    gold = _gc_gold(desc)
    return {
        "gc_type": gc_type,
        "category": category,
        "label": _s(desc, "Label"),
        "base_type": _base_type(node),
        "inventory_icon": _s(desc, "InventoryIcon"),
        "ground_object": _s(desc, "GroundObject"),
        "stackable": _b(desc, "Stackable"),
        "inventory_width": _i(desc, "InventoryWidth"),
        "inventory_height": _i(desc, "InventoryHeight"),
        "max_stack_size": _i(desc, "MaxStackSize"),
        "mod_count": None,                       # never fabricated; preserved later
        "drop_level": _i(desc, "MinLevel"),
        "level_req": _i(desc, "LevelReq"),
        "gc_gold_value": gold,                   # .gc fallback; overlaid verbatim later
        "source_file": source_file,
        "raw_json": json.dumps(_node_to_dict(merged), sort_keys=True),
    }


def _gear_row(gc_type: str, node: GCNode, merged: GCNode, source_file: str,
              *, is_armor: bool) -> dict:
    desc = merged.get_child("Description")
    gold = _gc_gold(desc)
    return {
        "gc_type": gc_type,
        "label": _s(desc, "Label"),
        "base_type": _base_type(node),
        "slot": None,
        "level": _i(desc, "LevelReq"),
        "class_req": None,
        "description": _s(desc, "Description"),
        "gold_value": gold,                      # .gc fallback; overlaid verbatim later
        "defense_rating": _f(desc, "DefenseRating") if is_armor else None,
        "damage": None if is_armor else _f(desc, "Damage"),
        "weapon_range": None if is_armor else _i(desc, "Range"),
        "cooldown": None if is_armor else _f(desc, "CoolDown"),
        "slot_type": _s(desc, "SlotType"),
        "weapon_class": None if is_armor else _s(desc, "WeaponClass"),
        "inventory_width": _i(desc, "InventoryWidth"),
        "inventory_height": _i(desc, "InventoryHeight"),
        "inventory_icon": _s(desc, "InventoryIcon"),
        "ground_object": _s(desc, "GroundObject"),
        "equipable": _b(desc, "Equipable"),
        "mod_count": None,                       # never fabricated; preserved later
        "gc_gold_value": gold,
        "source_file": source_file,
        "raw_json": json.dumps(_node_to_dict(merged), sort_keys=True),
    }


def collect_item_rows(gc_dir: str) -> dict[str, list[dict]]:
    """Parse + inheritance-resolve every node under the flat ``gc/`` dir; return
    ``{"items": [...], "weapons": [...], "armor": [...]}``. ``items`` is the
    superset; the subset tables hold the weapon-/armor-rooted nodes. Pure (no DB
    writes), so it is easy to dry-run. ``gc_type`` is lowercased; on a lowercase
    collision the first occurrence (sorted file order) wins."""
    db = build_item_db(gc_dir)
    items: list[dict] = []
    weapons: list[dict] = []
    armor: list[dict] = []
    seen: set[str] = set()

    files = sorted(
        os.path.join(gc_dir, n) for n in os.listdir(gc_dir)
        if n.lower().endswith(".gc")
    ) if os.path.isdir(gc_dir) else []

    for fp in files:
        top = parse_file(fp)
        if top is None or not top.name:
            continue
        source_file = os.path.basename(fp)
        stem = os.path.splitext(source_file)[0]

        stack: list[tuple[str, GCNode]] = [(stem, top)]
        while stack:
            prefix, n = stack.pop()
            if n.extends:
                category = _classify(_root_extends(db, n))
                if category is not None:
                    key = prefix.lower()
                    if key not in seen:
                        seen.add(key)
                        merged = db.flatten(n)
                        items.append(_item_row(key, category, n, merged, source_file))
                        if category == "weapons":
                            weapons.append(_gear_row(key, n, merged, source_file, is_armor=False))
                        elif category == "armor":
                            armor.append(_gear_row(key, n, merged, source_file, is_armor=True))
            for k, child in n.children.items():
                stack.append((f"{prefix}.{k}", child))

    items.sort(key=lambda r: r["gc_type"])
    weapons.sort(key=lambda r: r["gc_type"])
    armor.sort(key=lambda r: r["gc_type"])
    return {"items": items, "weapons": weapons, "armor": armor}


# ── live-value preservation + referential safety net ──

def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _preserve_map(conn: sqlite3.Connection, table: str,
                  cols: tuple[str, ...]) -> dict[str, dict]:
    """``lower(gc_type) -> {col: value}`` for the gold/mod columns that exist on
    the pre-existing live ``table`` (the columns the ``.gc`` cannot supply)."""
    have = _table_columns(conn, table)
    keep = [c for c in cols if c in have]
    if not keep or "gc_type" not in have:
        return {}
    out: dict[str, dict] = {}
    sql = f"SELECT gc_type, {', '.join(keep)} FROM {table} WHERE gc_type IS NOT NULL"
    for row in conn.execute(sql):
        out[str(row[0]).lower()] = {keep[i]: row[i + 1] for i in range(len(keep))}
    return out


def _referenced_keys(conn: sqlite3.Connection) -> set[str]:
    """Item gc_types referenced by live player/merchant state — never orphan
    these even if the selector misses them."""
    refs: set[str] = set()
    for table, col in (("character_equipment", "gc_class"),
                       ("character_inventory", "gc_class"),
                       ("merchant_inventory_items", "item_gc_type")):
        if col not in _table_columns(conn, table):
            continue
        try:
            for row in conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"):
                if row[0]:
                    refs.add(str(row[0]).lower())
        except sqlite3.Error:
            continue
    return refs


def _carry_forward(conn: sqlite3.Connection, table: str, columns: list[str],
                   refs: set[str], built_keys: set[str]) -> list[dict]:
    """Old rows whose gc_type is referenced by live state but absent from the
    rebuilt set, remapped onto the new schema (missing columns -> None)."""
    have = _table_columns(conn, table)
    if "gc_type" not in have:
        return []
    shared = [c for c in columns if c in have]
    rows: list[dict] = []
    sql = f"SELECT {', '.join(shared)} FROM {table} WHERE gc_type IS NOT NULL"
    for row in conn.execute(sql):
        rec = {shared[i]: row[i] for i in range(len(shared))}
        key = str(rec.get("gc_type") or "").lower()
        if not key or key in built_keys or key not in refs:
            continue
        full = {c: rec.get(c) for c in columns}
        full["gc_type"] = key
        rows.append(full)
    return rows


def _apply_preserved(rows: list[dict], preserve: dict[str, dict]) -> None:
    """Overlay verbatim live ``mod_count`` / gold values onto rebuilt rows by
    ``gc_type``. Rows with no live match keep their ``.gc`` fallback (and
    ``mod_count`` stays ``None`` — never fabricated)."""
    for r in rows:
        live = preserve.get(r["gc_type"])
        if not live:
            continue
        for col, val in live.items():
            if col in r:
                r[col] = val


def rebuild_items_table(conn: sqlite3.Connection, gc_dir: str) -> int:
    """Drop and rebuild ``items`` / ``weapons`` / ``armor`` from ``.gc`` content.

    Preserves the live ``mod_count`` / ``gc_gold_value`` / ``gold_value`` columns
    verbatim by ``gc_type`` (they have no ``.gc`` source), and carries forward any
    live row still referenced by a character or merchant that the principled
    selector missed. Returns the ``items`` row count. Caller owns the
    commit/transaction boundary."""
    # 1. snapshot live values to preserve, and live references, BEFORE dropping.
    preserve = {t: _preserve_map(conn, t, cols) for t, cols in _PRESERVE_BY_TABLE.items()}
    refs = _referenced_keys(conn)

    # 2. build faithful rows from the gc tree.
    built = collect_item_rows(gc_dir)
    built_keys = {t: {r["gc_type"] for r in built[t]} for t in built}

    # 3. carry forward referenced-but-unselected live rows (never orphan).
    carried = {
        "items": _carry_forward(conn, "items", _ITEM_COLUMNS, refs, built_keys["items"]),
        "weapons": _carry_forward(conn, "weapons", _WEAPON_COLUMNS, refs, built_keys["weapons"]),
        "armor": _carry_forward(conn, "armor", _ARMOR_COLUMNS, refs, built_keys["armor"]),
    }
    for t in built:
        built[t].extend(carried[t])

    # 4. overlay preserved live values.
    _apply_preserved(built["items"], preserve["items"])
    _apply_preserved(built["weapons"], preserve["weapons"])
    _apply_preserved(built["armor"], preserve["armor"])

    # 5. drop + recreate + insert.
    for t in ("items", "weapons", "armor"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute(_CREATE_ITEMS)
    conn.execute(_CREATE_WEAPONS)
    conn.execute(_CREATE_WEAPONS_IDX)
    conn.execute(_CREATE_ARMOR)
    conn.execute(_CREATE_ARMOR_IDX)
    conn.executemany(_insert_sql("items", _ITEM_COLUMNS), built["items"])
    conn.executemany(_insert_sql("weapons", _WEAPON_COLUMNS), built["weapons"])
    conn.executemany(_insert_sql("armor", _ARMOR_COLUMNS), built["armor"])
    return len(built["items"])
