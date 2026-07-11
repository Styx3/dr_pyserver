#!/usr/bin/env python3
"""Export the item content tables into a compressed SQL seed for the builder.

``items`` / ``weapons`` / ``armor`` use the client-validated **numbered-PAL**
generation whose source (the C# ``DR_Server`` build) is gone, and which the
extracter **cannot** reproduce without fabricating data — the armor base classes
(``BaseArmorClasses.*``) exist only as names in ``GCDictionary.dict``, defined in
no ``.gc`` file. So these tables (plus ``item_resolved_mods``, which is keyed by
item) are exported from a known-good DB into a compressed SQL seed that
``scripts/build_database.py`` loads, while every other domain is rebuilt from the
extracter.

Run this once against a good DB to (re)create the seed asset::

    python scripts/export_item_seed.py --db Database/dungeon_runners.db
"""
from __future__ import annotations

import argparse
import gzip
import os
import sqlite3

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# item_resolved_mods is keyed by item; stat_pools (16 fixed rows ported from C#
# ItemStatDatabase, read at runtime for mod values) is likewise not extracter-derivable.
SEED_TABLES = ("items", "weapons", "armor", "item_resolved_mods", "stat_pools")


def _lit(v: object) -> str:
    """Render a value as a SQL literal (matching sqlite's ``.dump``)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return "X'" + bytes(v).hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def dump_tables(conn: sqlite3.Connection, tables: tuple[str, ...]) -> str:
    """Return a self-contained SQL script (schema + data) for ``tables``."""
    out = ["PRAGMA foreign_keys=OFF;", "BEGIN TRANSACTION;"]
    for t in tables:
        # table DDL first, then its indexes (tables sort before indexes).
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
            "ORDER BY (type='table') DESC",
            (t,),
        ).fetchall()
        if not schema:
            raise SystemExit(f"table not found in source DB: {t}")
        for (sql,) in schema:
            out.append(sql.strip() + ";")
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        collist = ", ".join(f'"{c}"' for c in cols)
        for row in conn.execute(f'SELECT * FROM "{t}"'):
            values = ", ".join(_lit(v) for v in row)
            out.append(f'INSERT INTO "{t}" ({collist}) VALUES ({values});')
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default=os.path.join(_REPO_ROOT, "Database", "dungeon_runners.db"),
                    help="source DB to export the item tables from")
    ap.add_argument("--out", default=os.path.join(_REPO_ROOT, "scripts", "seed", "items_seed.sql.gz"),
                    help="output .sql.gz seed path")
    args = ap.parse_args()
    if not os.path.isfile(args.db):
        ap.error(f"source DB not found: {args.db}")

    # Read-only + immutable: avoids the WSL 9P sqlite file-lock hang on /mnt/c.
    uri = f"file:{os.path.abspath(args.db)}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in SEED_TABLES}
        sql = dump_tables(conn, SEED_TABLES)
    finally:
        conn.close()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as fh:
        fh.write(sql)
    rows = ", ".join(f"{t}={n}" for t, n in counts.items())
    print(f"[export] {rows}")
    print(f"[export] wrote {args.out} ({os.path.getsize(args.out):,} bytes)")


if __name__ == "__main__":
    main()
