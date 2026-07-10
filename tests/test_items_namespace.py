"""Tests for re-keying unnumbered-gen items to the client ``items.*`` namespace."""
from __future__ import annotations

import sqlite3

import pytest

from drserver.data.items_namespace import (
    ambiguous_stems,
    build_stem_prefix_map,
    new_key,
    rekey_item_tables,
)


def _make_items_tree(root):
    """Minimal extracter/items/ layout: pal/, consumables/, and an ambiguous
    stem (``magicig``) under two ig/ sub-dirs."""
    (root / "pal").mkdir(parents=True)
    (root / "consumables").mkdir()
    (root / "ig" / "1haxe").mkdir(parents=True)
    (root / "ig" / "1hmace").mkdir()
    (root / "pal" / "MageBodyPAL.gc").write_text("MageBodyPAL\n{\n}\n")
    (root / "pal" / "1HMacePAL.gc").write_text("1HMacePAL\n{\n}\n")
    (root / "consumables" / "Consumable_MinorHealthPotion.gc").write_text("x\n{\n}\n")
    (root / "ig" / "1haxe" / "MagicIG.gc").write_text("x\n{\n}\n")
    (root / "ig" / "1hmace" / "MagicIG.gc").write_text("x\n{\n}\n")


@pytest.mark.unit
def test_build_stem_prefix_map_maps_single_dir_stems(tmp_path):
    # Arrange
    _make_items_tree(tmp_path)

    # Act
    m = build_stem_prefix_map(str(tmp_path))

    # Assert — single-dir stems mapped, ambiguous stem omitted
    assert m["magebodypal"] == "items.pal."
    assert m["1hmacepal"] == "items.pal."
    assert m["consumable_minorhealthpotion"] == "items.consumables."
    assert "magicig" not in m


@pytest.mark.unit
def test_ambiguous_stems_lists_multi_dir_collisions(tmp_path):
    # Arrange
    _make_items_tree(tmp_path)

    # Act / Assert
    assert ambiguous_stems(str(tmp_path)) == ["magicig"]


@pytest.mark.unit
def test_new_key_prefixes_only_known_bare_stems(tmp_path):
    # Arrange
    _make_items_tree(tmp_path)
    m = build_stem_prefix_map(str(tmp_path))

    # Act / Assert
    assert new_key("magebodypal.normal001", m) == "items.pal.magebodypal.normal001"
    # numbered-gen stem not under extracter/items -> unchanged
    assert new_key("1haxe1pal.1haxe1-1", m) is None
    # already prefixed (first segment 'items') -> unchanged (idempotent)
    assert new_key("items.pal.magebodypal.normal001", m) is None


def _seed_items_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (gc_type TEXT PRIMARY KEY, label TEXT)")
    conn.execute("CREATE TABLE weapons (gc_type TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE armor (gc_type TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO items VALUES (?,?)", [
        ("magebodypal.normal001", "Rags"),         # unnumbered -> re-key
        ("1haxe1pal.1haxe1-1", "Axe"),             # numbered   -> keep
        ("consumable_minorhealthpotion", "Potion"),  # -> re-key
        ("magicig.something", "Gen"),              # ambiguous  -> keep
    ])
    conn.execute("INSERT INTO armor VALUES ('magebodypal.normal001')")
    conn.commit()
    return conn


@pytest.mark.integration
def test_rekey_item_tables_moves_unnumbered_keeps_numbered(tmp_path):
    # Arrange
    _make_items_tree(tmp_path)
    conn = _seed_items_db()

    # Act
    moved = rekey_item_tables(conn, str(tmp_path))

    # Assert — items: magebody + consumable moved (2); armor: 1 -> total 3
    assert moved == 3
    keys = {r[0] for r in conn.execute("SELECT gc_type FROM items")}
    assert "items.pal.magebodypal.normal001" in keys
    assert "items.consumables.consumable_minorhealthpotion" in keys
    assert "1haxe1pal.1haxe1-1" in keys           # numbered untouched
    assert "magicig.something" in keys             # ambiguous untouched
    armor = {r[0] for r in conn.execute("SELECT gc_type FROM armor")}
    assert armor == {"items.pal.magebodypal.normal001"}


@pytest.mark.integration
def test_rekey_is_idempotent_and_preserves_data(tmp_path):
    # Arrange
    _make_items_tree(tmp_path)
    conn = _seed_items_db()

    # Act
    rekey_item_tables(conn, str(tmp_path))
    second = rekey_item_tables(conn, str(tmp_path))

    # Assert — nothing left to move; label preserved
    assert second == 0
    label = conn.execute(
        "SELECT label FROM items WHERE gc_type='items.pal.magebodypal.normal001'"
    ).fetchone()[0]
    assert label == "Rags"
