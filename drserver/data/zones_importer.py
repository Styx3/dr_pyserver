"""Faithful augmentation of the ``zones`` table from the client ``*.zone`` files.

The legacy ``zones`` table (inherited from the C# emulator) is FAITHFUL on the
columns it carries — ``name`` and ``respawn_zone`` match the client ``*.zone``
``Name``/``RespawnZone`` fields 575/575 — but it is **lossy**: every client
``*.zone`` (``* extends ZoneDef``, plain ASCII) also defines ``Label``,
``MinLevel``/``MaxLevel`` (zone level-gating), ``Private``, ``RespawnSpawnPoint``,
``IsTown`` and a handful of PVP/elite flags that the table dropped entirely.

This module parses those ``.zone`` files and **adds** the missing fields as new
columns, matched to existing rows by ``Name`` (lowercased). It is purely
additive: existing rows and the emulator's hand-authored ``spawn_x/y/z`` (which
the client does NOT express numerically — it names a ``RespawnSpawnPoint`` that
the engine resolves to geometry at runtime) are preserved untouched.

See ``docs``/memory ``project_db_content_table_validation`` for the provenance
analysis.
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

# db_column, .zone field name, kind ("text" | "int" | "bool")
ZONE_FIELDS: List[Tuple[str, str, str]] = [
    ("label", "Label", "text"),
    ("private", "Private", "bool"),
    ("min_level", "MinLevel", "int"),
    ("max_level", "MaxLevel", "int"),
    ("respawn_spawn_point", "RespawnSpawnPoint", "text"),
    ("is_town", "IsTown", "bool"),
    ("is_legendary", "IsLegendary", "bool"),
    ("use_elite_generators", "UseEliteGenerators", "bool"),
    ("death_penalty", "DeathPenalty", "bool"),
    ("entry_modifier", "EntryModifier", "text"),
    ("pvp_type", "PVPType", "int"),
    ("pvp_match_type", "PVPMatchType", "text"),
    ("max_occupancy", "MaxOccupancy", "int"),
    ("update_frequency", "UpdateFrequency", "int"),
    ("allow_pvp_announcements", "AllowPvPAnnouncements", "bool"),
    ("send_bank_contents", "SendBankContents", "bool"),
    ("allow_duel_request", "AllowDuelRequest", "bool"),
]

_SQL_TYPE = {"text": "TEXT", "int": "INTEGER", "bool": "INTEGER DEFAULT 0"}

# Matches  Key = value;   or   Key = "quoted value";
_FIELD_RE = re.compile(r'^\s*([A-Za-z_]+)\s*=\s*"?(.*?)"?\s*;', re.MULTILINE)


def parse_zone_file(text: str) -> Dict[str, str]:
    """Return the raw ``Key -> value`` pairs from one ``.zone`` file body."""
    return {m.group(1): m.group(2).strip() for m in _FIELD_RE.finditer(text)}


def _coerce(value: Optional[str], kind: str):
    """Convert a raw ``.zone`` string value to the column's storage type."""
    if value is None or value == "":
        return None
    if kind == "text":
        return value
    if kind == "int":
        try:
            return int(value)
        except ValueError:
            return None
    if kind == "bool":
        return 1 if value.strip().lower() == "true" else 0
    return None


def zone_row(text: str) -> Tuple[Optional[str], Dict[str, object]]:
    """Parse a ``.zone`` body into ``(name_lower, {db_column: coerced_value})``.

    ``name_lower`` is the lowercased ``Name`` field — the join key to the
    existing ``zones`` rows. Absent ``bool`` fields coerce to 0 (false), which
    matches the client semantics (a flag is only written when true).
    """
    raw = parse_zone_file(text)
    name = raw.get("Name")
    out: Dict[str, object] = {}
    for col, field, kind in ZONE_FIELDS:
        v = _coerce(raw.get(field), kind)
        if v is None and kind == "bool":
            v = 0
        out[col] = v
    return (name.lower() if name else None, out)


def _existing_columns(conn: sqlite3.Connection) -> set:
    return {r[1] for r in conn.execute('PRAGMA table_info("zones")')}


def augment_zones_table(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Add the missing client ``.zone`` columns to ``zones`` and populate them.

    Returns the number of zone rows updated. Idempotent: only adds columns that
    are not already present, so a re-run just refreshes values.
    """
    have = _existing_columns(conn)
    for col, _field, kind in ZONE_FIELDS:
        if col not in have:
            conn.execute(f'ALTER TABLE zones ADD COLUMN {col} {_SQL_TYPE[kind]}')

    # Build name(lower) -> parsed values from every .zone file.
    parsed: Dict[str, Dict[str, object]] = {}
    for fp in glob.glob(os.path.join(extracter_root, "*.zone")):
        with open(fp, encoding="latin-1") as fh:
            name, vals = zone_row(fh.read())
        if name:
            parsed[name] = vals

    set_clause = ", ".join(f"{col} = :{col}" for col, _f, _k in ZONE_FIELDS)
    updated = 0
    for row in conn.execute("SELECT id, name FROM zones").fetchall():
        zid, name = row[0], (row[1] or "").lower()
        vals = parsed.get(name)
        if vals is None:
            continue
        params = dict(vals)
        params["id"] = zid
        conn.execute(f"UPDATE zones SET {set_clause} WHERE id = :id", params)
        updated += 1
    return updated


def rebuild_zones_table(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Seed the base ``zones`` rows from every client ``*.zone`` file, then fill
    the augmented columns via :func:`augment_zones_table`. For a from-zero build
    (``augment_zones_table`` only UPDATEs rows that already exist).

    Base columns are the ones the client ``*.zone`` expresses directly: ``name``
    (``Name``), ``gc_type`` (the zone's world GCObject type, ``world.<lower name>``
    — verified against every live row) and ``respawn_zone`` (``RespawnZone``).
    ``spawn_x/y/z`` stay 0: the client names a ``RespawnSpawnPoint`` the engine
    resolves to geometry at runtime rather than shipping numeric coords. Returns
    the number of base rows inserted.
    """
    conn.execute("DELETE FROM zones")
    zid = 1
    for fp in sorted(glob.glob(os.path.join(extracter_root, "*.zone"))):
        with open(fp, encoding="latin-1") as fh:
            raw = parse_zone_file(fh.read())
        name = raw.get("Name")
        if not name:
            continue
        conn.execute(
            "INSERT INTO zones (id, name, gc_type, respawn_zone) VALUES (?,?,?,?)",
            (zid, name, "world." + name.lower(), (raw.get("RespawnZone") or "").lower()),
        )
        zid += 1
    augment_zones_table(conn, extracter_root)
    return zid - 1
