"""Import client ``*.world`` / ``*.enc`` dungeon spawn content into SQLite.

Replaces the hand-coded dungeon00-only spawn literals with data-driven tables
(`dungeon_levels`, `dungeon_encounters`) covering every PvE dungeon. See
``drserver/data/dungeon_world_importer.py`` for the chain (.world → .enc → mob → creature).

Usage (from repo root):
    python scripts/import_dungeon_worlds.py                 # live DB, makes a .bak
    python scripts/import_dungeon_worlds.py --db /tmp/x.db  # explicit target
    python scripts/import_dungeon_worlds.py --no-backup     # skip the timestamped backup
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.data.dungeon_world_importer import import_dungeon_worlds  # noqa: E402
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
        levels, units, room_nodes, statics = import_dungeon_worlds(
            conn, args.extracter)
    finally:
        conn.close()
    print(f"[done] imported {levels} dungeon levels, {units} encounter units, "
          f"{room_nodes} room nodes, {statics} static worlds")


if __name__ == "__main__":
    main()
