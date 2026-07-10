"""Tests for the client-ground-truth class starting-equipment importer.

Ground truth = the extracted client ``extracter/avatar/classes/*StartingEquipment.gc``
files. These embed each equipped item as a top-level ``* extends <key>`` block
tagged with an ``ID`` (the equipment slot) and may nest a weapon-modifier
``* extends`` that must NOT be treated as an equipped item.
"""
from __future__ import annotations

import sqlite3

import pytest

from drserver.data import class_equipment_importer as cei
from drserver.net.equipment import _SLOT_TO_DB

# Verbatim copies of the client gc bodies (extracter/avatar/classes/).
_FIGHTER_GC = """\
FighterStartingEquipment
{
\t* extends items.pal.1HMacePAL.Normal001
\t{
\t\tID = 10;
\t\tLevel = 1;

\t\t* extends items.modpal.LevelPrefixModPAL.Weapon01.Mod1  //"Cardboard"
\t\t{
\t\t}
\t}

\t* extends ScaleArmor1Pal.ScaleArmor1-1
\t{
\t\tID = 6;
\t\tLevel = 1;
\t}

\t* extends ScaleGloves1Pal.ScaleGloves1-1
\t{
\t\tID = 2;
\t\tLevel = 1;
\t}

\t* extends ScaleBoots1Pal.ScaleBoots1-1
\t{
\t\tID = 7;
\t\tLevel = 1;
\t}
}
"""

_WARLOCK_GC = """\
WarlockStartingEquipment
{
\t* extends 1HStaff1Pal.1HStaff1-1
\t{
\t\tID = 10;
\t\tLevel = 1;
\t}

\t* extends items.pal.MageBodyPAL.Normal001 //was ClothArmor0PAL.ClothArmor0-1
\t{
\t\tID = 6;
\t\tLevel = 1;
\t}

\t* extends items.pal.MageGlovesPAL.Normal002 //was ClothGloves1Pal.ClothGloves1-1
\t{
\t\tID = 2;
\t\tLevel = 1;
\t}

\t* extends items.pal.MageBootsPAL.Normal002 //was ClothBoots1Pal.ClothBoots1-1
\t{
\t\tID = 7;
\t\tLevel = 1;
\t}
}
"""

_RANGER_GC = """\
RangerStartingEquipment
{
\t* extends 2HCrossbow1PAL.2HCrossbow1-1
\t{
\t\tID = 10;
\t\tLevel = 1;
\t}

\t* extends LeatherArmor1Pal.LeatherArmor1-1
\t{
\t\tID = 6;
\t\tLevel = 1;
\t}

\t* extends LeatherGloves1Pal.LeatherGloves1-1
\t{
\t\tID = 2;
\t\tLevel = 1;
\t}

\t* extends LeatherBoots1Pal.LeatherBoots1-1
\t{
\t\tID = 7;
\t\tLevel = 1;
\t}
}
"""


@pytest.mark.unit
def test_parse_returns_slot_keyed_equipment():
    # Arrange / Act
    slots = cei.parse_starting_equipment(_FIGHTER_GC)

    # Assert — only the four top-level equipped items, keyed by slot ID.
    assert slots == {
        10: "items.pal.1hmacepal.normal001",
        6: "scalearmor1pal.scalearmor1-1",
        2: "scalegloves1pal.scalegloves1-1",
        7: "scaleboots1pal.scaleboots1-1",
    }


@pytest.mark.unit
def test_parse_excludes_nested_weapon_mod():
    # The Fighter mace nests a modpal "* extends"; it must not become a slot.
    slots = cei.parse_starting_equipment(_FIGHTER_GC)

    assert not any("modpal" in v for v in slots.values())
    assert len(slots) == 4


@pytest.mark.unit
def test_slot_to_column_agrees_with_wire_slot_map():
    # The importer's slot->column map must match the live equip wire mapping,
    # otherwise gear lands in the wrong column.
    for slot_id, column in cei.SLOT_TO_COLUMN.items():
        assert _SLOT_TO_DB[slot_id] == column


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE class_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_name TEXT NOT NULL UNIQUE,
            display_name TEXT DEFAULT '', description TEXT DEFAULT '',
            weapon TEXT DEFAULT '', armor TEXT DEFAULT '', helmet TEXT DEFAULT '',
            gloves TEXT DEFAULT '', boots TEXT DEFAULT '', shoulders TEXT DEFAULT '',
            shield TEXT DEFAULT '', ring1 TEXT DEFAULT '', ring2 TEXT DEFAULT '',
            amulet TEXT DEFAULT '')"""
    )
    for cn in ("Fighter", "Mage", "Ranger"):
        conn.execute(
            "INSERT INTO class_definitions (class_name) VALUES (?)", (cn,)
        )
    conn.commit()
    return conn


def _write_classes(tmp_path):
    (tmp_path / "FighterStartingEquipment.gc").write_text(_FIGHTER_GC)
    (tmp_path / "WarlockStartingEquipment.gc").write_text(_WARLOCK_GC)
    (tmp_path / "RangerStartingEquipment.gc").write_text(_RANGER_GC)
    return str(tmp_path)


@pytest.mark.integration
def test_import_updates_all_three_classes(tmp_path):
    # Arrange
    conn = _make_db()
    classes_dir = _write_classes(tmp_path)

    # Act
    updated = cei.import_class_equipment(conn, classes_dir)

    # Assert
    assert updated == 3
    row = conn.execute(
        "SELECT weapon, armor, gloves, boots FROM class_definitions "
        "WHERE class_name = 'Mage'"
    ).fetchone()
    assert row == (
        "1hstaff1pal.1hstaff1-1",
        "items.pal.magebodypal.normal001",
        "items.pal.mageglovespal.normal002",
        "items.pal.magebootspal.normal002",
    )


@pytest.mark.integration
def test_warlock_file_maps_to_mage_row(tmp_path):
    conn = _make_db()
    cei.import_class_equipment(conn, _write_classes(tmp_path))

    # No "Warlock" row should be created; the Warlock file feeds the Mage row.
    assert conn.execute(
        "SELECT COUNT(*) FROM class_definitions WHERE class_name = 'Warlock'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT weapon FROM class_definitions WHERE class_name = 'Mage'"
    ).fetchone()[0] == "1hstaff1pal.1hstaff1-1"


@pytest.mark.integration
def test_import_is_idempotent(tmp_path):
    conn = _make_db()
    classes_dir = _write_classes(tmp_path)

    cei.import_class_equipment(conn, classes_dir)
    before = conn.execute(
        "SELECT class_name, weapon, armor, gloves, boots FROM class_definitions "
        "ORDER BY class_name"
    ).fetchall()
    cei.import_class_equipment(conn, classes_dir)
    after = conn.execute(
        "SELECT class_name, weapon, armor, gloves, boots FROM class_definitions "
        "ORDER BY class_name"
    ).fetchall()

    assert before == after


@pytest.mark.integration
def test_missing_dir_updates_nothing(tmp_path):
    conn = _make_db()
    updated = cei.import_class_equipment(conn, str(tmp_path / "nope"))
    assert updated == 0
