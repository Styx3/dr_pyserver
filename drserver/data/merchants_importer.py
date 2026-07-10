"""Faithful (re)generation of the merchant tables from the client NPC ``.gc`` files.

Ground truth = the extracted client vendor NPC definitions (tier-2 content;
see the source-of-truth hierarchy in ``CLAUDE.md``)::

    extracter/world/<zone>/npc/<Name>.gc

Any NPC file containing a ``Merchant extends Merchant`` block is a vendor.
The block carries everything the shop needs — per-vendor price modifiers,
tabs (``* extends MerchantInventory``) with authored level ranges, item
generators, labels, grid sizes, and the static stock items with their
client-baked IDs (255+)::

    Merchant extends Merchant
    {
        SellValueMod = 1.0;
        BuyValueMod = 6.22;

        Weapons extends MerchantInventory
        {
            ID = 1;
            StaticContents = false;
            AutoGenerateItems = true;
            ItemGenerator = MerchantWeaponIG;
            MinItemLevel = 6;
            MaxItemLevel = 20;
            static Description extends InventoryDesc
            { Label = "Weapons"; Width = 10; Height = 14; }
        }
        ...
    }

The legacy ``merchants`` / ``merchant_inventories`` / ``merchant_inventory_items``
rows (inherited from the C# emulator) drifted from the client data: invented
180s regen (native = 0x2328 ticks / 30 ticks-per-sec = 300s), the HermitVendor
"Assorted Goods" tab pointing at MerchantTrashIG 1-100 (client: MerchantSuperiorIG
3-10), VendorWeapon* tab-3 labelled "Superior" (client: "Scrap Heap"), and
linearised static-item coordinates for VendorPotion1. The C# server then papered
over parts of this with hardcoded overrides — this importer makes the DB carry
the authored values so no override layer is needed.

Emulator-only admin vendors (``world.town.npc.Admin*``) are server tooling, not
client content — their rows are preserved verbatim across a rebuild.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

# Native client merchant refresh timer: 0x2328 ticks at 30 ticks/second = 300s.
NATIVE_REFRESH_TICKS = 0x2328
REFRESH_TICKS_PER_SECOND = 30
NATIVE_REFRESH_SECONDS = NATIVE_REFRESH_TICKS // REFRESH_TICKS_PER_SECOND

_EXTENDS_RE = re.compile(r"^(?:static\s+)?(\S+)\s+extends\s+(\S+)")
_STAR_EXTENDS_RE = re.compile(r"^\*\s+extends\s+(\S+)")
_KV_RE = re.compile(r'^(\w+)\s*=\s*"?([^";]*)"?\s*;')


@dataclass
class MerchantItemDef:
    gc_type: str
    inventory_x: int = 0
    inventory_y: int = 0
    item_id: int = 0
    quantity: int = 1


@dataclass
class MerchantInventoryDef:
    name: str
    inv_id: int = 0
    static_contents: bool = False
    auto_generate: bool = False
    item_generator: str = ""
    min_item_level: int = 0
    max_item_level: int = 0
    label: str = ""
    width: int = 10
    height: int = 14
    items: List[MerchantItemDef] = field(default_factory=list)


@dataclass
class MerchantDef:
    npc_gc_type: str
    name: str
    sell_value_mod: float = 1.0
    buy_value_mod: float = 1.0
    regenerate_items: bool = True
    inventories: List[MerchantInventoryDef] = field(default_factory=list)


def _to_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def parse_merchant_block(text: str, npc_gc_type: str, name: str) -> Optional[MerchantDef]:
    """Parse the ``Merchant extends Merchant`` block out of one NPC ``.gc`` body.

    Returns ``None`` when the NPC has no merchant block. The parser is
    depth-aware (same style as ``class_equipment_importer``): tab blocks are
    ``<Name> extends MerchantInventory`` directly inside the merchant block,
    static stock items are ``* extends <gc_key>`` directly inside a tab, and
    the ``InventoryDesc`` child holds the tab label/grid.
    """
    merchant: Optional[MerchantDef] = None
    depth = 0
    merchant_depth = 0          # depth of the merchant block body, 0 = not inside
    cur_inv: Optional[MerchantInventoryDef] = None
    inv_depth = 0
    cur_item: Optional[MerchantItemDef] = None
    item_depth = 0
    in_desc_depth = 0
    pending: Optional[tuple] = None      # ("merchant"|"inv"|"item"|"desc", payload)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        m = _EXTENDS_RE.match(line)
        star = _STAR_EXTENDS_RE.match(line)
        if star and merchant is not None and cur_inv is not None and pending is None:
            pending = ("item", star.group(1))
        elif m:
            block_name, parent = m.group(1), m.group(2)
            if parent == "Merchant" and block_name == "Merchant" and merchant is None:
                pending = ("merchant", None)
            elif parent == "MerchantInventory" and merchant is not None:
                pending = ("inv", block_name)
            elif parent == "InventoryDesc" and cur_inv is not None:
                pending = ("desc", None)

        if "{" in line:
            depth += 1
            if pending is not None:
                kind, payload = pending
                pending = None
                if kind == "merchant":
                    merchant = MerchantDef(npc_gc_type=npc_gc_type, name=name)
                    merchant_depth = depth
                elif kind == "inv":
                    cur_inv = MerchantInventoryDef(name=payload)
                    inv_depth = depth
                elif kind == "item":
                    cur_item = MerchantItemDef(gc_type=payload)
                    item_depth = depth
                elif kind == "desc":
                    in_desc_depth = depth

        kv = _KV_RE.match(line)
        if kv and merchant is not None:
            key, value = kv.group(1), kv.group(2).strip()
            if cur_item is not None:
                if key == "InventoryX":
                    cur_item.inventory_x = int(float(value))
                elif key == "InventoryY":
                    cur_item.inventory_y = int(float(value))
                elif key == "ID":
                    cur_item.item_id = int(float(value))
                elif key == "Quantity":
                    cur_item.quantity = int(float(value))
            elif in_desc_depth and cur_inv is not None:
                if key == "Label":
                    cur_inv.label = value
                elif key == "Width":
                    cur_inv.width = int(float(value))
                elif key == "Height":
                    cur_inv.height = int(float(value))
            elif cur_inv is not None:
                if key == "ID":
                    cur_inv.inv_id = int(float(value))
                elif key == "StaticContents":
                    cur_inv.static_contents = _to_bool(value)
                elif key == "AutoGenerateItems":
                    cur_inv.auto_generate = _to_bool(value)
                elif key == "ItemGenerator":
                    cur_inv.item_generator = value
                elif key == "MinItemLevel":
                    cur_inv.min_item_level = int(float(value))
                elif key == "MaxItemLevel":
                    cur_inv.max_item_level = int(float(value))
            else:
                if key == "SellValueMod":
                    merchant.sell_value_mod = float(value)
                elif key == "BuyValueMod":
                    merchant.buy_value_mod = float(value)
                elif key == "RegenerateItems":
                    merchant.regenerate_items = _to_bool(value)

        if "}" in line:
            if cur_item is not None and depth == item_depth:
                cur_inv.items.append(cur_item)
                cur_item = None
                item_depth = 0
            elif in_desc_depth and depth == in_desc_depth:
                in_desc_depth = 0
            elif cur_inv is not None and depth == inv_depth:
                merchant.inventories.append(cur_inv)
                cur_inv = None
                inv_depth = 0
            elif merchant is not None and merchant_depth and depth == merchant_depth:
                merchant_depth = 0          # merchant block closed; keep parsing nothing
            depth -= 1

    if merchant is not None and not merchant.inventories:
        return None
    return merchant


def discover_merchants(extracter_root: str) -> List[MerchantDef]:
    """Scan ``world/*/npc/*.gc`` for vendor (merchant) NPC definitions."""
    found: List[MerchantDef] = []
    world_dir = os.path.join(extracter_root, "world")
    if not os.path.isdir(world_dir):
        return found
    for zone in sorted(os.listdir(world_dir)):
        npc_dir = os.path.join(world_dir, zone, "npc")
        if not os.path.isdir(npc_dir):
            continue
        for fname in sorted(os.listdir(npc_dir)):
            if not fname.lower().endswith(".gc"):
                continue
            stem = os.path.splitext(fname)[0]
            path = os.path.join(npc_dir, fname)
            try:
                with open(path, encoding="latin-1") as fh:
                    text = fh.read()
            except OSError:
                continue
            if "extends Merchant" not in text:
                continue
            npc_gc_type = f"world.{zone}.npc.{stem}"
            md = parse_merchant_block(text, npc_gc_type, stem)
            if md is not None:
                found.append(md)
    return found


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add the client columns the legacy schema lacked."""
    def cols(table: str) -> set:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    add = {
        "merchants": (
            ("sell_value_mod", "REAL", "1.0"),
            ("buy_value_mod", "REAL", "1.0"),
            ("regenerate_items", "INTEGER", "1"),
        ),
        "merchant_inventories": (
            ("static_contents", "INTEGER", "0"),
        ),
    }
    for table, columns in add.items():
        have = cols(table)
        for cname, ctype, default in columns:
            if cname not in have:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {cname} {ctype} DEFAULT {default}")


def rebuild_merchant_tables(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Replace the client-content merchant rows with values parsed from the
    extracter NPC ``.gc`` files. Admin vendor rows (``...npc.Admin*``) and their
    inventories/items are preserved verbatim. Returns the number of client
    merchants written.
    """
    merchants = discover_merchants(extracter_root)
    if not merchants:
        return 0

    _ensure_columns(conn)

    # Preserve existing ids for known vendors so character-side references (none
    # today, but cheap to keep) stay stable; allocate new ids after the max.
    existing = {
        (row[1] or "").lower(): row[0]
        for row in conn.execute("SELECT id, npc_gc_type FROM merchants")
    }
    next_id = (conn.execute("SELECT COALESCE(MAX(id),0) FROM merchants").fetchone()[0]
               or 0) + 1

    # Wipe client merchants (keep Admin*-prefixed emulator tooling).
    client_ids = [
        row[0] for row in conn.execute(
            "SELECT id FROM merchants WHERE npc_gc_type NOT LIKE '%npc.Admin%'")
    ]
    for mid in client_ids:
        conn.execute("DELETE FROM merchant_inventory_items WHERE merchant_id=?", (mid,))
        conn.execute("DELETE FROM merchant_inventories WHERE merchant_id=?", (mid,))
        conn.execute("DELETE FROM merchants WHERE id=?", (mid,))

    for md in merchants:
        mid = existing.get(md.npc_gc_type.lower())
        if mid is None:
            mid = next_id
            next_id += 1
        _write_merchant(conn, md, mid)

    conn.commit()
    return len(merchants)


def _write_merchant(conn: sqlite3.Connection, md: "MerchantDef", mid: int) -> None:
    """Write one merchant (+ its inventories + items) under id ``mid``.

    Shared by :func:`rebuild_merchant_tables` and the add-only hub-vendor
    registration in ``world_npc_importer``. The caller owns id allocation,
    duplicate-skipping, and the surrounding transaction/commit.
    """
    conn.execute(
        "INSERT INTO merchants (id, npc_gc_type, merchant_gc_type, name,"
        " sell_value_mod, buy_value_mod, regenerate_items)"
        " VALUES (?,?,?,?,?,?,?)",
        (mid, md.npc_gc_type, f"{md.npc_gc_type}.Merchant", md.name,
         md.sell_value_mod, md.buy_value_mod, int(md.regenerate_items)))
    for inv in md.inventories:
        conn.execute(
            "INSERT INTO merchant_inventories (merchant_id, inv_id, name,"
            " label, gc_type, auto_generate, item_generator, min_item_level,"
            " max_item_level, regen_seconds, width, height, static_contents)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, inv.inv_id, inv.name, inv.label,
             f"{md.npc_gc_type}.Merchant.{inv.name}",
             int(inv.auto_generate), inv.item_generator,
             inv.min_item_level, inv.max_item_level,
             NATIVE_REFRESH_SECONDS if inv.auto_generate else 0,
             inv.width, inv.height, int(inv.static_contents)))
        for item in inv.items:
            conn.execute(
                "INSERT INTO merchant_inventory_items (merchant_id, inv_id,"
                " item_gc_type, inventory_x, inventory_y, item_slot_id,"
                " quantity) VALUES (?,?,?,?,?,?,?)",
                (mid, inv.inv_id, item.gc_type, item.inventory_x,
                 item.inventory_y, item.item_id, item.quantity))
