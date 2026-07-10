"""Tests for the faithful ``zones`` table augmentation from ``*.zone`` files."""
from __future__ import annotations

import sqlite3

import pytest

from drserver.data.zones_importer import (
    ZONE_FIELDS,
    augment_zones_table,
    parse_zone_file,
    zone_row,
)

# A real-shaped private dungeon .zone (level-gated, returns to town).
DUNGEON_ZONE = """* extends ZoneDef
{
\tName = dungeon01_level05;
\tPrivate = true;
\tLabel = "Algernon - Level 5";
\tRespawnZone = town;
\tMinLevel = 3;
\tMaxLevel = 20;
}
"""

# A real-shaped public town .zone with bank + named respawn point.
TOWN_ZONE = """* extends ZoneDef
{
\tName = Town;
\tPrivate = false;
\tLabel = "Townston";
\tRespawnSpawnPoint = Start;
\tRespawnZone = Town;
\tMaxOccupancy = 25;
\tUpdateFrequency = 6;
\tIsTown = true;
\tSendBankContents = true;
}
"""


@pytest.mark.unit
def test_parse_zone_file_extracts_quoted_and_bare_values():
    # Arrange / Act
    fields = parse_zone_file(DUNGEON_ZONE)

    # Assert — quoted label keeps inner spaces; bare values stripped
    assert fields["Name"] == "dungeon01_level05"
    assert fields["Label"] == "Algernon - Level 5"
    assert fields["RespawnZone"] == "town"
    assert fields["MinLevel"] == "3"


@pytest.mark.unit
def test_parse_zone_file_handles_apostrophe_in_label():
    # Arrange
    body = '* extends ZoneDef\n{\n\tName = shop;\n\tLabel = "Choppe\'s Shoppe";\n}\n'

    # Act
    fields = parse_zone_file(body)

    # Assert
    assert fields["Label"] == "Choppe's Shoppe"


@pytest.mark.unit
def test_zone_row_coerces_types_and_keys_on_lowercased_name():
    # Act
    name, vals = zone_row(DUNGEON_ZONE)

    # Assert
    assert name == "dungeon01_level05"
    assert vals["label"] == "Algernon - Level 5"
    assert vals["private"] == 1  # bool true -> 1
    assert vals["min_level"] == 3  # int
    assert vals["max_level"] == 20
    assert vals["is_town"] == 0  # absent bool -> 0 (false)


@pytest.mark.unit
def test_zone_row_town_flags():
    # Act
    name, vals = zone_row(TOWN_ZONE)

    # Assert
    assert name == "town"
    assert vals["is_town"] == 1
    assert vals["send_bank_contents"] == 1
    assert vals["private"] == 0
    assert vals["respawn_spawn_point"] == "Start"
    assert vals["max_occupancy"] == 25
    assert vals["update_frequency"] == 6


def _seed_zones_db() -> sqlite3.Connection:
    """A minimal legacy ``zones`` table: name + respawn + hand-authored spawn."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE zones (id INTEGER PRIMARY KEY, name TEXT, gc_type TEXT, "
        "respawn_zone TEXT DEFAULT '', spawn_x REAL DEFAULT 0, spawn_y REAL DEFAULT 0, "
        "spawn_z REAL DEFAULT 0, spawn_heading REAL DEFAULT 0, explored_bit_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO zones (id, name, gc_type, respawn_zone, spawn_x) "
        "VALUES (1, 'dungeon01_level05', 'world.dungeon01_level05', 'town', -342.0)"
    )
    conn.execute(
        "INSERT INTO zones (id, name, gc_type, respawn_zone) VALUES (2, 'Town', 'world.town', 'Town')"
    )
    conn.commit()
    return conn


@pytest.mark.integration
def test_augment_adds_columns_and_preserves_existing(tmp_path):
    # Arrange — two .zone files + a legacy DB
    (tmp_path / "dungeon01_level05.zone").write_text(DUNGEON_ZONE, encoding="latin-1")
    (tmp_path / "town.zone").write_text(TOWN_ZONE, encoding="latin-1")
    conn = _seed_zones_db()

    # Act
    updated = augment_zones_table(conn, str(tmp_path))

    # Assert — both rows updated, new columns exist, spawn_x preserved
    assert updated == 2
    cols = {r[1] for r in conn.execute('PRAGMA table_info("zones")')}
    for col, _f, _k in ZONE_FIELDS:
        assert col in cols
    d = conn.execute(
        "SELECT label, min_level, max_level, private, spawn_x FROM zones WHERE name='dungeon01_level05'"
    ).fetchone()
    assert d == ("Algernon - Level 5", 3, 20, 1, -342.0)
    t = conn.execute(
        "SELECT label, is_town, send_bank_contents, respawn_spawn_point FROM zones WHERE name='Town'"
    ).fetchone()
    assert t == ("Townston", 1, 1, "Start")


@pytest.mark.integration
def test_augment_is_idempotent(tmp_path):
    # Arrange
    (tmp_path / "town.zone").write_text(TOWN_ZONE, encoding="latin-1")
    conn = _seed_zones_db()

    # Act — run twice (only Town has a .zone file here, so 1 row matches)
    augment_zones_table(conn, str(tmp_path))
    second = augment_zones_table(conn, str(tmp_path))

    # Assert — no ALTER error, value stable
    assert second == 1
    assert conn.execute("SELECT is_town FROM zones WHERE name='Town'").fetchone()[0] == 1
