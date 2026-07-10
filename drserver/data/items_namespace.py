"""Re-key the unnumbered-generation item rows to their faithful client namespace.

The ``items`` rebuild (see ``items_importer``) drew from the *flat* DR_Server
``gc/`` dir, where every ``.gc`` file sits in one directory. That flattening lost
the content sub-path: the unnumbered generation, which the client addresses under
``items.pal.``, ``items.consumables.``, ``items.modpal.`` … (e.g.
``items.pal.MageBodyPAL.Normal001``), was keyed by bare stem
(``magebodypal.normal001``). The numbered generation (``1HAxe1PAL.1HAxe1-1``)
genuinely lives at the root namespace and is already correct.

The decisive symptom: class starting gear, merchant consumable stock and live
``character_equipment`` rows all reference the **full** client paths
(``items.pal.*`` / ``items.consumables.*``) and therefore 0-joined the items
tables. The fix is purely a key migration — prepend the correct
``items.<reldir>.`` prefix to exactly the rows whose top PAL stem corresponds to
a ``.gc`` file under ``extracter/items/<reldir>/``. The numbered generation is
untouched (its stems do not appear under ``extracter/items/``).

Stems that appear under *multiple* sub-dirs (the per-weapon ``*IG`` item
generators) are ambiguous and left as-is (reported, not re-keyed). The operation
is idempotent: an already-prefixed key's first segment is ``items`` (not a stem
in the map), so a re-run skips it.
"""
from __future__ import annotations

import glob
import os
import sqlite3
from typing import Dict, List, Tuple

_GEAR_TABLES = ("items", "weapons", "armor")


def build_stem_prefix_map(items_root: str) -> Dict[str, str]:
    """``stem(lower) -> "items.<reldir>."`` for every ``.gc`` under ``items_root``.

    Only stems that resolve to a *single* sub-dir are returned; ambiguous stems
    (same filename in multiple dirs) are omitted so they are never mis-keyed.
    """
    from collections import defaultdict

    found: Dict[str, set] = defaultdict(set)
    for fp in glob.glob(os.path.join(items_root, "**", "*.gc"), recursive=True):
        rel = os.path.relpath(fp, items_root)
        reldir = os.path.dirname(rel).replace(os.sep, ".").replace("/", ".").lower()
        stem = os.path.splitext(os.path.basename(fp))[0].lower()
        found[stem].add(f"items.{reldir}." if reldir else "items.")
    return {s: next(iter(v)) for s, v in found.items() if len(v) == 1}


def ambiguous_stems(items_root: str) -> List[str]:
    """Stems present under more than one sub-dir (skipped by the re-key)."""
    from collections import defaultdict

    found: Dict[str, set] = defaultdict(set)
    for fp in glob.glob(os.path.join(items_root, "**", "*.gc"), recursive=True):
        rel = os.path.relpath(fp, items_root)
        reldir = os.path.dirname(rel).replace(os.sep, ".").replace("/", ".").lower()
        stem = os.path.splitext(os.path.basename(fp))[0].lower()
        found[stem].add(f"items.{reldir}." if reldir else "items.")
    return sorted(s for s, v in found.items() if len(v) > 1)


def new_key(gc_type: str, stem_prefix: Dict[str, str]) -> str | None:
    """Faithful client key for a bare gc_type, or ``None`` if it should not move.

    A row moves iff its first path segment is a known single-dir stem. Already
    prefixed keys (first segment ``items``) and numbered-gen keys are left alone.
    """
    if not gc_type:
        return None
    stem = gc_type.split(".", 1)[0].lower()
    prefix = stem_prefix.get(stem)
    if prefix is None:
        return None
    return prefix + gc_type


def _rekey_table(conn: sqlite3.Connection, table: str,
                 stem_prefix: Dict[str, str]) -> Tuple[int, int]:
    """Re-key one table in place. Returns ``(moved, skipped_collision)``."""
    existing = {str(r[0]).lower() for r in conn.execute(f"SELECT gc_type FROM {table}")}
    moves: List[Tuple[str, str]] = []
    for (key,) in conn.execute(f"SELECT gc_type FROM {table}"):
        if key is None:
            continue
        nk = new_key(key, stem_prefix)
        if nk is None:
            continue
        moves.append((key, nk))

    moved = 0
    skipped = 0
    for old, nk in moves:
        if nk.lower() in existing:  # never clobber an existing row
            skipped += 1
            continue
        conn.execute(f"UPDATE {table} SET gc_type = :nk WHERE gc_type = :old",
                     {"nk": nk, "old": old})
        existing.discard(old.lower())
        existing.add(nk.lower())
        moved += 1
    return moved, skipped


def rekey_item_tables(conn: sqlite3.Connection, items_root: str) -> int:
    """Re-key ``items``/``weapons``/``armor`` to the client namespace in place.

    Returns the total rows moved across the three tables. Caller owns the
    transaction. Idempotent.
    """
    stem_prefix = build_stem_prefix_map(items_root)
    total = 0
    for table in _GEAR_TABLES:
        moved, _skipped = _rekey_table(conn, table, stem_prefix)
        total += moved
    return total
