"""Restore the full content database and merge in live player data.

Background
----------
The server's configured DB (``DR_Server/.../Build/Database/dungeon_runners.db``)
was overwritten with a fresh, content-less file: only the ~10 runtime player
tables that ``drserver/db/game_database.py`` auto-creates on boot remained, while
all static content (items, creatures, zones, quests, skills, merchants, ...) was
lost. With no content tables, every world-load query returns empty and the client
gets "error talking to the server" on join.

The intact 56-table DB survives inside ``DR_Server.zip`` at
``DR_Server/Assets/DungeonRunners/Database/dungeon_runners.db`` (the *Assets* copy
was not clobbered, only the *Build* copy). The companion JSON exports under
``Client666/docs/database/json_exports`` are NOT usable — the export script wrote
every value as null.

What this does
--------------
1. Extract the intact content DB from the zip (or use ``--from-db``).
2. Copy the current live *player* rows (accounts/characters/character_*) into it,
   so characters created since the snapshot are preserved.
3. Back up the current (broken) target DB, then atomically swap the merged DB in.

Run with ``--dry-run`` to build ``<target>.rebuilt.db`` and print a report without
touching the live file.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

# Player/runtime tables: their rows live in the (broken) target DB and must be
# carried forward. Everything else is static content that comes from the snapshot.
PLAYER_TABLES = (
    "accounts",
    "characters",
    "character_equipment",
    "character_inventory",
    "character_skills",
    "character_quests",
    "character_checkpoints",
    "character_modifiers",
    "completed_quests",
    "quest_objectives",
    "server_settings",
)

ZIP_ENTRY = "DR_Server/Assets/DungeonRunners/Database/dungeon_runners.db"


def _desktop() -> str:
    """The user's Desktop dir, resolved so it works under both WSL and native
    Windows. This script lives at ``…/Desktop/dr_pyserver/scripts``, so the
    Desktop is two levels up; probe absolute fallbacks only if that is absent.

    (Do NOT use ``"/mnt/c" if os.path.isdir("/mnt/c") else "C:"``: on native
    Windows ``/mnt/c`` resolves to ``C:\\mnt\\c`` and, if that folder exists,
    yields a mangled mixed-separator path.)"""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    desktop = os.path.dirname(repo_root)
    if os.path.isdir(desktop):
        return desktop
    home_desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(home_desktop):
        return home_desktop
    return desktop


def _default_zip() -> str:
    return os.path.join(_desktop(), "DR_Server.zip")


def _default_target() -> str:
    return os.path.join(
        _desktop(), "DR_Server", "DR_Server", "Build", "Database",
        "dungeon_runners.db")


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n", {"n": table}
    ).fetchone()
    return row is not None


def extract_snapshot(zip_path: str, entry: str, dest: str) -> None:
    if not os.path.isfile(zip_path):
        sys.exit(f"[restore] zip not found: {zip_path}")
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        if entry not in names:
            # Be forgiving about a leading folder or slash differences.
            cands = [n for n in names if n.endswith("Database/dungeon_runners.db")]
            if not cands:
                sys.exit(f"[restore] entry not found in zip: {entry}")
            entry = cands[0]
        print(f"[restore] extracting {entry}")
        with z.open(entry) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def merge_player_rows(content_db: str, live_db: str) -> dict[str, int]:
    """Copy player rows from the live (broken) DB into the rebuilt content DB."""
    counts: dict[str, int] = {}
    conn = sqlite3.connect(content_db)
    conn.execute("PRAGMA foreign_keys = OFF")  # bulk load; FKs re-checked by server
    try:
        conn.execute("ATTACH DATABASE :p AS live", {"p": live_db})
        for table in PLAYER_TABLES:
            if not _table_exists(conn, table):
                continue
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n",
                {"n": f"live.{table}"},
            ).fetchone() and not _live_has(conn, table):
                continue
            dst_cols = _table_columns(conn, table)
            src_cols = [r[1] for r in conn.execute(f'PRAGMA live.table_info("{table}")')]
            shared = [c for c in dst_cols if c in src_cols]
            if not shared:
                continue
            n_live = conn.execute(f'SELECT COUNT(*) FROM live."{table}"').fetchone()[0]
            if n_live == 0:
                counts[table] = 0
                continue
            collist = ", ".join(f'"{c}"' for c in shared)
            conn.execute(f'DELETE FROM "{table}"')  # snapshot player tables are empty/stale
            conn.execute(
                f'INSERT INTO "{table}" ({collist}) SELECT {collist} FROM live."{table}"'
            )
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE live")
    finally:
        conn.close()
    return counts


def _live_has(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute(f'SELECT 1 FROM live."{table}" LIMIT 1')
        return True
    except sqlite3.OperationalError:
        return False


def report(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        tabs = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        print(f"[restore] result DB has {len(tabs)} tables")
        for t in ("items", "creatures", "zones", "npcs", "merchants", "quests",
                  "skills", "accounts", "characters"):
            if t in tabs:
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                print(f"           {t:14s} {n}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", default=_default_zip(), help="DR_Server.zip path")
    ap.add_argument("--from-db", help="use an already-extracted snapshot DB instead of the zip")
    ap.add_argument("--target", default=_default_target(), help="server DB path to restore")
    ap.add_argument("--dry-run", action="store_true",
                    help="build <target>.rebuilt.db and report; do not swap")
    args = ap.parse_args()

    target = args.target
    rebuilt = target + ".rebuilt.db"

    # 1. Obtain the intact content snapshot.
    if args.from_db:
        if not os.path.isfile(args.from_db):
            sys.exit(f"[restore] --from-db not found: {args.from_db}")
        shutil.copyfile(args.from_db, rebuilt)
        print(f"[restore] copied snapshot from {args.from_db}")
    else:
        extract_snapshot(args.zip, ZIP_ENTRY, rebuilt)

    # 2. Merge live player rows (if a live DB exists at the target).
    if os.path.isfile(target):
        counts = merge_player_rows(rebuilt, target)
        merged = {k: v for k, v in counts.items() if v}
        print(f"[restore] merged player rows: {merged or 'none'}")
    else:
        print(f"[restore] no existing target at {target}; nothing to merge")

    report(rebuilt)

    if args.dry_run:
        print(f"[restore] DRY RUN — rebuilt DB left at:\n           {rebuilt}")
        print("[restore] inspect it, then re-run without --dry-run to swap it in")
        return

    # 3. Back up the broken DB and swap the rebuilt one in.
    if os.path.isfile(target):
        backup = f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(target, backup)
        print(f"[restore] backed up old DB -> {backup}")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    shutil.move(rebuilt, target)
    print(f"[restore] restored content DB -> {target}")
    print("[restore] done. Start the server: python -m drserver")


if __name__ == "__main__":
    main()
