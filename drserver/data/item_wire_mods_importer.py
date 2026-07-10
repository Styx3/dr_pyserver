"""Bake the native item → ItemModifier mods into the ``item_wire_mods`` table.

Resolves the client's ItemGenerator → ItemModGenerator → ModPAL chain (see
``item_mod_resolver``) for every ``(item, rarity)`` the IGs define, and stores
the ordered ``items.modpal.*`` refs so the server can emit them on the wire as
by-hash ``ItemModifier`` children (``0x04 <djb2> 0x00`` — verified against the
client, ``docs/CLIENT_GROUND_TRUTH.md``).

Additive: creates/replaces only ``item_wire_mods``; no other table is touched.
Run via ``scripts/rebuild_content_tables.py --table item_wire_mods`` (which
operates on a tmp copy of the DB and swaps it in — never edits the live DB while
the server holds it).
"""
from __future__ import annotations

import sqlite3

from ..core import log
from . import item_mod_resolver


_CREATE = """
CREATE TABLE IF NOT EXISTS item_wire_mods (
    item_gc_type TEXT NOT NULL,
    rarity       TEXT NOT NULL,
    slot         INTEGER NOT NULL,
    mod_ref      TEXT NOT NULL
)
"""

_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_item_wire_mods_key "
    "ON item_wire_mods (item_gc_type, rarity)"
)


def rebuild_item_wire_mods_table(conn: sqlite3.Connection,
                                 extracter_root: str) -> int:
    """(Re)build ``item_wire_mods`` from the extracter IG/MG/ModPAL chain.

    Returns the number of (item, rarity, slot) rows written.
    """
    resolved = item_mod_resolver.build_resolved_items(extracter_root)

    conn.execute("DROP TABLE IF EXISTS item_wire_mods")
    conn.execute(_CREATE)
    conn.execute(_INDEX)

    rows = 0
    for item in resolved:
        # Store the gc ref lowered to match the server's normalize_key lookups.
        item_key = item.item_ref.lower()
        for slot, mod_ref in enumerate(item.mod_refs):
            conn.execute(
                "INSERT INTO item_wire_mods (item_gc_type, rarity, slot, mod_ref)"
                " VALUES (?,?,?,?)",
                (item_key, item.rarity, slot, mod_ref))
            rows += 1
    conn.commit()

    distinct_items = len({(r.item_ref.lower(), r.rarity) for r in resolved})
    log.info(f"[ItemWireMods] baked {rows} mod rows for {distinct_items} "
             f"(item,rarity) pairs")
    return rows
