"""Import connecting-hub + town-catalog NPC placements into the ``npcs`` table.

Populates the hubs the client ships with NPCs (``thehub``, ``pvp_hub``) from
their authored ``*.world`` placements, and adds the town NPC-catalog entries
``town.world`` never places. **Add-only** — existing rows (incl. the curated
town/tutorial/pvp rows) are never rewritten. Also registers the hub vendors as
functional merchants. See ``drserver/data/world_npc_importer.py``.

Usage (from repo root, with the server STOPPED — never write the live DB while
the Windows server holds it open, or the WAL tears):
    python scripts/import_world_npcs.py                 # live DB, makes a .bak
    python scripts/import_world_npcs.py --db /tmp/x.db  # explicit target
    python scripts/import_world_npcs.py --no-backup     # skip the timestamped backup
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.data.world_npc_importer import import_world_npcs  # noqa: E402
from drserver.data.extracter_paths import extracter_dir  # noqa: E402


def _default_extracter() -> str:
    return extracter_dir()


def _default_db() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "Database", "dungeon_runners.db",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=_default_db(), help="SQLite DB to import into")
    ap.add_argument("--extracter", default=_default_extracter(),
                    help="extracted client content root")
    ap.add_argument("--no-backup", action="store_true", help="skip timestamped backup")
    args = ap.parse_args()

    if not os.path.isdir(args.extracter):
        ap.error(f"extracter root not found: {args.extracter}")
    if not os.path.isfile(args.db):
        ap.error(f"db not found: {args.db}")

    if not args.no_backup:
        backup = f"{args.db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copyfile(args.db, backup)
        print(f"[backup] {backup}")

    conn = sqlite3.connect(args.db)
    try:
        added = import_world_npcs(conn, args.extracter)
        conn.commit()
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity_check failed: {ok}")
    finally:
        conn.close()
    print(f"[done] added {added} NPC rows (hub + town-catalog); integrity ok")


if __name__ == "__main__":
    main()
