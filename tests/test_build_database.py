"""Tests for the from-zero content-DB builder (``scripts/build_database.py``).

Two layers:

* a fast, always-on unit test that the content schema creates the tables it owns
  on an in-memory DB (no extracter needed);
* an extracter-gated integration test that actually builds a subset of the DB and
  asserts per-table row counts against the known-good live-DB parity numbers.

The integration test is skipped automatically when the extracted client content is
not present, mirroring the other extracter-gated tests in this suite.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from drserver.data import content_schema
from drserver.data import extracter_paths


def _load_build_database():
    """Import ``scripts/build_database.py`` as a module (it is not a package)."""
    path = os.path.join(_REPO_ROOT, "scripts", "build_database.py")
    spec = importlib.util.spec_from_file_location("build_database", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fast unit test — no extracter required ───────────────────────────────────

def test_content_schema_creates_owned_tables():
    conn = sqlite3.connect(":memory:")
    content_schema.create_content_schema(conn)
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = set(content_schema.CONTENT_TABLES) - present
    assert not missing, f"content_schema did not create: {sorted(missing)}"


# ── integration test — builds a subset from the real extracter ───────────────

_EXTRACTER = extracter_paths.resolve_extracter_dir()
_ITEM_SEED = os.path.join(_REPO_ROOT, "scripts", "seed", "items_seed.sql.gz")

# Steps that are quick enough to build in a test, with their parity targets. A
# tuple (lo, hi) is an inclusive range; a bare int is exact. Live-DB counts are
# the reference (see the plan doc); ranges absorb the few hand-curated rows that
# the extracter-only build does not yet reproduce.
_PARITY = {
    "creatures":    ("creatures", 1400),
    "skills":       ("skills", 237),
    "quests":       ("quests", 1289),
    "item_wire_mods": ("item_wire_mods", 1148),
    "zones":        ("zones", 575),
    "classes":      ("class_definitions", 3),
    "merchants":    ("merchants", (150, 157)),
    "npcs":         ("npcs", 41),
}


@pytest.mark.skipif(_EXTRACTER is None, reason="extracter client content not present")
def test_build_subset_hits_parity(tmp_path):
    bd = _load_build_database()
    out = str(tmp_path / "built.db")
    steps = set(_PARITY.keys())
    steps.add("world_markers")  # zone_portals / zone_waypoints / zone_checkpoints
    have_seed = os.path.isfile(_ITEM_SEED)
    if have_seed:
        steps.add("items")  # loads items/weapons/armor from the seed asset
    bd.build(_EXTRACTER, out, only=steps)

    conn = sqlite3.connect(out)
    try:
        for step, (table, target) in _PARITY.items():
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if isinstance(target, tuple):
                lo, hi = target
                assert lo <= n <= hi, f"{table}: {n} not in [{lo},{hi}]"
            else:
                assert n == target, f"{table}: {n} != {target}"
        # classes also seed starting skills (4 per class × 3 classes)
        skills = conn.execute("SELECT COUNT(*) FROM class_starting_skills").fetchone()[0]
        assert skills == 12, f"class_starting_skills: {skills} != 12"
        # every zone carries the client world gc_type
        bad = conn.execute(
            "SELECT COUNT(*) FROM zones WHERE gc_type <> 'world.' || lower(name)"
        ).fetchone()[0]
        assert bad == 0, f"{bad} zones have a non-world gc_type"
        # world markers parsed from *.world (live 439 / 296 / 15; small edge-case gap ok)
        for table, lo, hi in (("zone_portals", 425, 445), ("zone_waypoints", 290, 300),
                              ("zone_checkpoints", 15, 15)):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert lo <= n <= hi, f"{table}: {n} not in [{lo},{hi}]"
        # items/weapons/armor come from the seed asset (not extracter-derivable)
        if have_seed:
            for table, target in (("items", 11761), ("weapons", 1630), ("armor", 1746)):
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert n == target, f"{table} (seed): {n} != {target}"
    finally:
        conn.close()
