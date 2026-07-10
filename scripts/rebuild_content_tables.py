"""Regenerate static content tables in the server DB from ``.gc`` ground truth.

Pilot domain: ``skills``. The legacy ``skills`` table inherited from the older
C# build was **fabricated** — its columns (``experience``, ``attr_strength`` …)
map onto C#'s invented ``SkillData`` and have no counterpart in the client's
``.gc`` files. This rebuilds it faithfully from ``extracter/skills/**/*.gc`` with
``extends`` inheritance resolved (see ``drserver/data/skills_importer.py``).

Only the named table is replaced; every other table (player runtime + other
content) is carried over untouched, because we operate on a *copy* of the live
DB and swap it back.

WSL note: SQLite over ``/mnt/c`` (9P/drvfs) hangs on file locking, so we build
on the local tmpfs (``/tmp``) and ``shutil.move`` the result back — a bulk copy,
which 9P tolerates. Reading the ``.gc`` files directly off ``/mnt/c`` is fine
(plain sequential reads, no locking).

Examples
--------
    # dry run: build + report, touch nothing
    python scripts/rebuild_content_tables.py --dry-run

    # apply: back up the live DB, rebuild the skills table, swap in
    python scripts/rebuild_content_tables.py
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# Make ``drserver`` importable when run as a bare script from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from drserver.data.creatures_importer import rebuild_creatures_table  # noqa: E402
from drserver.data.items_importer import rebuild_items_table  # noqa: E402
from drserver.data.quests_importer import rebuild_quests_table  # noqa: E402
from drserver.data.skills_importer import rebuild_skills_table  # noqa: E402
from drserver.data.items_namespace import rekey_item_tables  # noqa: E402
from drserver.data.zones_importer import augment_zones_table  # noqa: E402
from drserver.data.class_equipment_importer import import_class_equipment  # noqa: E402
from drserver.data.merchants_importer import rebuild_merchant_tables  # noqa: E402
from drserver.data.item_wire_mods_importer import rebuild_item_wire_mods_table  # noqa: E402
from drserver.data.extracter_paths import desktop_dir, extracter_dir  # noqa: E402


def _default_db() -> str:
    return os.path.join(_REPO_ROOT, "Database", "dungeon_runners.db")


def _default_extracter() -> str:
    return extracter_dir()


def _default_items_gc() -> str:
    """The numbered-PAL item content tree (flat dir) the live DB was built from.

    Items are NOT a clean projection of ``extracter/`` (that holds a newer,
    differently-keyed generation). The generation matching the live DB + the
    client's ``GCDictionary.dict`` ships flat in the C# ``DR_Server`` build.
    See ``drserver/data/items_importer.py``."""
    return os.path.join(desktop_dir(), "DR_Server", "Build", "Database", "gc")


# table name -> (extracter subdir, rebuild function). Extend as new domains land.
# An empty subdir means the rebuilder takes the extracter *root* (quests are
# scattered across ``world/**/quest/`` + ``quests/base/`` — see quests_importer).
REBUILDERS = {
    "skills": ("skills", rebuild_skills_table),
    "quests": ("", rebuild_quests_table),
    "creatures": ("", rebuild_creatures_table),
    # items rebuilds items + weapons + armor from the flat DR_Server gc/ tree;
    # its content root is the numbered-PAL dir, not an extracter subdir
    # (resolved specially in main() via --items-gc-dir).
    "items": (None, rebuild_items_table),
    # zones is AUGMENTED (not replaced): adds the missing client .zone columns
    # (Label/MinLevel/MaxLevel/IsTown/...) keyed by Name; .zone files live at the
    # extracter root.
    "zones": ("", augment_zones_table),
    # items_keys re-keys the unnumbered-gen items/weapons/armor rows to the client
    # namespace (items.pal./items.consumables./...) using the extracter/items/
    # sub-dir layout; no rows added/removed.
    "items_keys": ("items", rekey_item_tables),
    # class_equipment regenerates the class_definitions starting gear columns
    # from the client avatar/classes/*StartingEquipment.gc files (ground truth),
    # replacing the hand-authored values. Updates rows in place; adds none.
    "class_equipment": ("avatar/classes", import_class_equipment),
    # merchants rebuilds merchants + merchant_inventories + merchant_inventory_items
    # from the vendor NPC .gc files (world/*/npc/*.gc with a Merchant block).
    # Admin* emulator vendors are preserved verbatim.
    "merchants": ("", rebuild_merchant_tables),
    # item_wire_mods bakes the native (item,rarity) -> items.modpal.* mod refs
    # resolved from the IG/MG/ModPAL chain (extracter root). Additive table.
    "item_wire_mods": ("", rebuild_item_wire_mods_table),
}


def _report(db_path: str, table: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        if table == "class_equipment":
            for cn, w, a, g, b in conn.execute(
                "SELECT class_name, weapon, armor, gloves, boots "
                "FROM class_definitions ORDER BY class_name"
            ).fetchall():
                print(f"[rebuild] {cn}: weapon={w} armor={a} gloves={g} boots={b}")
            return
        if table == "items_keys":
            for t in ("items", "weapons", "armor"):
                pref = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE gc_type LIKE 'items.%'"
                ).fetchone()[0]
                tot = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"[rebuild] {t}: {pref}/{tot} rows on client items.* namespace")
            return
        if table == "merchants":
            for mid, gc, smod, bmod in conn.execute(
                "SELECT id, npc_gc_type, sell_value_mod, buy_value_mod "
                "FROM merchants ORDER BY id"
            ).fetchall():
                tabs = conn.execute(
                    "SELECT inv_id, label, item_generator, min_item_level,"
                    " max_item_level, static_contents FROM merchant_inventories"
                    " WHERE merchant_id=? ORDER BY inv_id", (mid,)).fetchall()
                items = conn.execute(
                    "SELECT COUNT(*) FROM merchant_inventory_items WHERE merchant_id=?",
                    (mid,)).fetchone()[0]
                print(f"[rebuild] {gc} sell={smod} buy={bmod} statics={items}")
                for inv_id, label, ig, lo, hi, static in tabs:
                    kind = "STATIC" if static else f"{ig} {lo}-{hi}"
                    print(f"[rebuild]   tab {inv_id} '{label}': {kind}")
            return
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        print(f"[rebuild] {table}: {n} rows, {len(cols)} columns")
        print(f"[rebuild] columns: {', '.join(cols)}")
        if table == "skills":
            players = conn.execute(
                "SELECT COUNT(*) FROM skills "
                "WHERE profession_type IN ('FIGHTER','MAGE','RANGER','SUMMONER')"
            ).fetchone()[0]
            print(f"[rebuild] player-trainable skills (real ProfessionType): {players}")
        if table == "quests":
            with_obj = conn.execute(
                "SELECT COUNT(*) FROM quests WHERE objective_count > 0"
            ).fetchone()[0]
            base_classes = conn.execute(
                "SELECT COUNT(DISTINCT base_class) FROM quests"
            ).fetchone()[0]
            followups = conn.execute(
                "SELECT COUNT(*) FROM quests WHERE followup_quest IS NOT NULL"
            ).fetchone()[0]
            print(f"[rebuild] quests with >=1 objective: {with_obj}")
            print(f"[rebuild] distinct base_class values: {base_classes}")
            print(f"[rebuild] quests with a followup_quest: {followups}")
        if table == "creatures":
            tiers = conn.execute(
                "SELECT creature_difficulty, COUNT(*) FROM creatures "
                "GROUP BY creature_difficulty ORDER BY 2 DESC"
            ).fetchall()
            print("[rebuild] by difficulty: "
                  + ", ".join(f"{(t or '∅')}={n}" for t, n in tiers))
        if table == "items":
            w = conn.execute("SELECT COUNT(*) FROM weapons").fetchone()[0]
            a = conn.execute("SELECT COUNT(*) FROM armor").fetchone()[0]
            cats = conn.execute(
                "SELECT category, COUNT(*) FROM items GROUP BY category ORDER BY 2 DESC"
            ).fetchall()
            preserved = conn.execute(
                "SELECT COUNT(*) FROM items WHERE mod_count IS NOT NULL"
            ).fetchone()[0]
            print(f"[rebuild] weapons table: {w} rows; armor table: {a} rows")
            print("[rebuild] items by category: "
                  + ", ".join(f"{(c or '∅')}={n}" for c, n in cats))
            print(f"[rebuild] items with preserved (live) mod_count: {preserved}")
        if table == "zones":
            labelled = conn.execute(
                "SELECT COUNT(*) FROM zones WHERE label IS NOT NULL AND label != ''"
            ).fetchone()[0]
            levelled = conn.execute(
                "SELECT COUNT(*) FROM zones WHERE min_level IS NOT NULL"
            ).fetchone()[0]
            towns = conn.execute(
                "SELECT COUNT(*) FROM zones WHERE is_town = 1"
            ).fetchone()[0]
            print(f"[rebuild] zones with Label: {labelled}; with Min/MaxLevel: "
                  f"{levelled}; towns: {towns}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=_default_db(), help="server DB to update")
    ap.add_argument("--extracter", default=_default_extracter(), help="extracted client content root")
    ap.add_argument("--items-gc-dir", default=_default_items_gc(),
                    help="flat numbered-PAL gc/ dir (items source; not extracter)")
    ap.add_argument("--table", default="skills", choices=sorted(REBUILDERS), help="content table to rebuild")
    ap.add_argument("--dry-run", action="store_true", help="build + report, do not touch the live DB")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        ap.error(f"DB not found: {args.db}")
    subdir, rebuild_fn = REBUILDERS[args.table]
    # items draws from the flat DR_Server gc/ tree; all others from extracter.
    if args.table == "items":
        content_root = args.items_gc_dir
    else:
        content_root = os.path.join(args.extracter, subdir)
    if not os.path.isdir(content_root):
        ap.error(f"content dir not found: {content_root}")

    # Build on local tmpfs (avoids 9P/sqlite lock hang), starting from a copy of
    # the live DB so all other tables survive.
    fd, tmp_db = tempfile.mkstemp(suffix=".db", prefix=f"rebuild_{args.table}_")
    os.close(fd)
    shutil.copyfile(args.db, tmp_db)
    try:
        conn = sqlite3.connect(tmp_db)
        try:
            n = rebuild_fn(conn, content_root)
            conn.commit()
        finally:
            conn.close()
        print(f"[rebuild] {args.table}: wrote {n} rows from {content_root}")
        _report(tmp_db, args.table)

        if args.dry_run:
            keep = f"{args.db}.rebuilt.db"
            shutil.copyfile(tmp_db, keep)
            print(f"[rebuild] DRY RUN — built {keep}; live DB untouched")
            return

        backup = f"{args.db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copyfile(args.db, backup)
        print(f"[rebuild] backed up live DB -> {backup}")
        # Cross-device move onto /mnt/c (9P) has produced torn files that read
        # fine from cache but are malformed on disk (live corruption 2026-06-10).
        # Stage a sibling copy on the destination filesystem, fsync it, VERIFY
        # its integrity by re-reading, then atomically replace.
        staged = f"{args.db}.staged"
        with open(tmp_db, "rb") as src, open(staged, "wb") as dst:
            shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        check = sqlite3.connect(staged)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            os.remove(staged)
            raise RuntimeError(
                f"staged DB failed integrity_check ({result}); live DB untouched")
        os.replace(staged, args.db)
        print(f"[rebuild] swapped rebuilt DB into {args.db} (integrity ok)")
    finally:
        if tmp_db and os.path.exists(tmp_db):
            os.remove(tmp_db)


if __name__ == "__main__":
    main()
