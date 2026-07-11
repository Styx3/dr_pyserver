#!/usr/bin/env python3
"""Build a complete content database from zero, using ONLY the extracter + Python.

This replaces the dead ``scripts/restore_database.py`` (which needed the
now-missing ``DR_Server.zip``). It creates an empty SQLite file, applies the
player schema (``drserver/db/game_database.py``) and the content schema
(``drserver/data/content_schema.py``), then runs every ``.gc`` / ``.world``
importer against the extracted client content, in dependency order.

To dodge the WSL 9P/sqlite file-lock hang, the DB is built on local tmpfs and
atomically moved to ``--out`` after a ``PRAGMA integrity_check`` (same safe-swap
discipline as ``scripts/rebuild_content_tables.py``).

Usage
-----
    # full build to the default Database/dungeon_runners.db
    python scripts/build_database.py

    # explicit extracter + output; build only some domains; list the steps
    python scripts/build_database.py --extracter /path/to/extracter --out /tmp/dr.db
    python scripts/build_database.py --only creatures,skills,quests
    python scripts/build_database.py --list
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Callable, List, Optional, Tuple

# Make ``drserver`` importable when run as a bare script from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from drserver.db import game_database  # noqa: E402
from drserver.data import content_schema  # noqa: E402
from drserver.data.extracter_paths import extracter_dir  # noqa: E402
from drserver.data.creatures_importer import rebuild_creatures_table  # noqa: E402
from drserver.data.creature_manipulators_importer import (  # noqa: E402
    import_missing_creature_manipulators,
)
from drserver.data.skills_importer import rebuild_skills_table  # noqa: E402
from drserver.data.quests_importer import rebuild_quests_table  # noqa: E402
from drserver.data.merchants_importer import rebuild_merchant_tables  # noqa: E402
from drserver.data.item_wire_mods_importer import (  # noqa: E402
    rebuild_item_wire_mods_table,
)
from drserver.data.dungeon_world_importer import (  # noqa: E402
    build_schema as _dungeon_build_schema,
    import_dungeon_worlds,
)
from drserver.data.world_npc_importer import (  # noqa: E402
    import_world_npcs,
    import_npc_teleporters,
)
from drserver.data.zones_importer import rebuild_zones_table  # noqa: E402
from drserver.data.world_markers_importer import import_world_markers  # noqa: E402
from drserver.data.class_equipment_importer import rebuild_class_tables  # noqa: E402

# A build step: name -> fn(conn, extracter_root) -> row count. Steps run in list
# order; dependencies (creatures before manipulators, etc.) are encoded by order.
Step = Tuple[str, Callable[[sqlite3.Connection, str], int]]


def _skills(conn: sqlite3.Connection, ex: str) -> int:
    return rebuild_skills_table(conn, os.path.join(ex, "skills"))


def _dungeon_worlds(conn: sqlite3.Connection, ex: str) -> int:
    _dungeon_build_schema(conn)
    levels, _units, _nodes, statics = import_dungeon_worlds(conn, ex)
    return levels + statics


def _classes(conn: sqlite3.Connection, ex: str) -> int:
    return rebuild_class_tables(conn, os.path.join(ex, "avatar", "classes"))


# items/weapons/armor + item_resolved_mods use the client-validated numbered-PAL
# generation whose source (DR_Server) is gone; the extracter cannot reproduce them
# without fabricating base classes (armor's BaseArmorClasses exists only as a
# GCDictionary name). They load from a seed exported by scripts/export_item_seed.py
# from a known-good DB. Overridable via --item-seed.
DEFAULT_ITEM_SEED = os.path.join(_REPO_ROOT, "scripts", "seed", "items_seed.sql.gz")
_ITEM_SEED_TABLES = ("items", "weapons", "armor", "item_resolved_mods", "stat_pools")
_item_seed_path = DEFAULT_ITEM_SEED


def _items(conn: sqlite3.Connection, ex: str) -> int:
    """Load items/weapons/armor + item_resolved_mods from the seed (see above).
    The extracter arg is unused — these tables are not extracter-derivable."""
    if not os.path.isfile(_item_seed_path):
        raise SystemExit(
            f"item seed not found: {_item_seed_path}\n"
            "  Items are not reproducible from the extracter (see docs/DATABASE.md).\n"
            "  Create it once from a known-good DB:\n"
            "    python scripts/export_item_seed.py --db Database/dungeon_runners.db"
        )
    with gzip.open(_item_seed_path, "rt", encoding="utf-8") as fh:
        sql = fh.read()
    for t in _ITEM_SEED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(sql)
    return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


# NB: items/weapons/armor (Phase 3), the orphan-table importers (Phase 2, e.g.
# zone_portals/summons/stat_pools) and base zones/class seeding (Phase 1) are added
# to this list as those importers land. Every step here is self-sufficient from the
# extracter today.
STEPS: List[Step] = [
    ("creatures", rebuild_creatures_table),
    ("creature_manipulators", import_missing_creature_manipulators),
    ("skills", _skills),
    ("classes", _classes),
    ("items", _items),
    ("quests", rebuild_quests_table),
    ("item_wire_mods", rebuild_item_wire_mods_table),
    ("merchants", rebuild_merchant_tables),
    ("dungeon_worlds", _dungeon_worlds),
    ("zones", rebuild_zones_table),
    ("world_markers", import_world_markers),
    ("npcs", import_world_npcs),
    ("npc_teleporters", import_npc_teleporters),
]


def _new_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # importers insert in dependency order
    return conn


def build(extracter: str, out: str, only: Optional[set] = None,
          item_seed: Optional[str] = None) -> None:
    global _item_seed_path
    if item_seed:
        _item_seed_path = item_seed
    if not os.path.isdir(extracter):
        raise SystemExit(f"extracter not found: {extracter}")

    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="dr_build_")
    os.close(fd)
    os.remove(tmp)  # let sqlite create it fresh
    try:
        conn = _new_db(tmp)
        try:
            game_database._create_schema(conn)          # player/runtime tables
            content_schema.create_content_schema(conn)  # content tables
            conn.commit()
            for name, fn in STEPS:
                if only and name not in only:
                    continue
                t0 = time.time()
                n = fn(conn, extracter)
                conn.commit()
                print(f"[build] {name:22} {n:>7} rows  ({time.time() - t0:.1f}s)")
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise SystemExit(f"integrity_check failed: {result}")
        finally:
            conn.close()

        # Atomic swap onto the destination filesystem (fsync + re-verify, per the
        # 9P torn-file lesson in rebuild_content_tables.py).
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        staged = f"{out}.staged"
        with open(tmp, "rb") as src, open(staged, "wb") as dst:
            shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        check = sqlite3.connect(staged)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                os.remove(staged)
                raise SystemExit("staged DB failed integrity_check; output untouched")
        finally:
            check.close()
        os.replace(staged, out)
        print(f"[build] wrote {out}")
    finally:
        for junk in (tmp, f"{tmp}-wal", f"{tmp}-shm"):
            if os.path.exists(junk):
                os.remove(junk)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--extracter", default=extracter_dir(),
                    help="extracted client content root (default: auto-resolved)")
    ap.add_argument("--out", default=os.path.join(_REPO_ROOT, "Database", "dungeon_runners.db"),
                    help="output DB path")
    ap.add_argument("--only", default="",
                    help="comma-separated subset of step names to run")
    ap.add_argument("--item-seed", default=DEFAULT_ITEM_SEED,
                    help="items/weapons/armor SQL seed (see scripts/export_item_seed.py)")
    ap.add_argument("--list", action="store_true", help="list step names and exit")
    args = ap.parse_args()

    if args.list:
        for name, _ in STEPS:
            print(name)
        return
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    build(args.extracter, args.out, only, item_seed=args.item_seed)


if __name__ == "__main__":
    main()
