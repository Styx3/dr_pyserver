"""Hub + town-catalog NPC importer tests.

Guards the source-of-truth contract for ``world_npc_importer``:

* hub NPC placements are parsed from the client ``*.world`` (ground truth),
  excluding terrain / checkpoints;
* the dungeon-portal sub-hubs author no NPCs (faithful = empty);
* the import is add-only / idempotent and never rewrites curated rows;
* the hub vendors get registered as functional merchants.

The ``*.world`` / ``.gc`` parses need the extracter; the DB parts use a temp
copy of the shipped DB so the live DB is never locked or mutated.
"""
import os
import sqlite3

import pytest

import _paths
from drserver.data.world_npc_importer import (
    DEPRECATED_TOWN_NPCS,
    HUB_WORLDS,
    NPCPlacement,
    _npc_name,
    collect_new_npcs,
    import_world_npcs,
    parse_world_npc_placements,
)

EXTRACTER = os.path.normpath(os.path.join(_paths.REPO_ROOT, "..", "extracter"))
_HAS_EXTRACTER = os.path.isdir(EXTRACTER)
_needs_extracter = pytest.mark.skipif(
    not _HAS_EXTRACTER, reason="extracter content not present")
_needs_db = pytest.mark.skipif(
    not _paths.has_shipped_db(), reason="shipped DB not present")


# ── pure helpers (no I/O) ──────────────────────────────────────────────────

def test_npc_name_strips_NPC_prefix():
    assert _npc_name("world.test.NPC_HubVendor") == "HubVendor"
    assert _npc_name("world.test.npc.QuestGiver_A") == "QuestGiver_A"
    assert _npc_name("world.town.npc.Amazon1") == "Amazon1"


def test_deprecated_town_npcs_are_town_types():
    # The fabricated town extras are flagged for deletion (the client places
    # them nowhere); each is a town NPC gc type.
    assert DEPRECATED_TOWN_NPCS, "expected a deprecated-extras list"
    for gc in DEPRECATED_TOWN_NPCS:
        assert gc.startswith("world.town.npc.")


# ── .world parsing (ground truth) ──────────────────────────────────────────

@_needs_extracter
def test_thehub_placements_are_the_authored_four():
    # Act
    placements = parse_world_npc_placements(
        os.path.join(EXTRACTER, "TheHub.world"), "thehub")

    # Assert — exactly the four NPCs TheHub.world authors.
    types = {p.gc_type for p in placements}
    assert types == {
        "world.test.NPC_HubVendor",
        "world.test.NPC_TestArmorVendor",
        "world.test.npc.QuestGiver_A",
        "world.test.npc.Well",
    }
    hub_vendor = next(p for p in placements
                      if p.gc_type == "world.test.NPC_HubVendor")
    assert (hub_vendor.pos_x, hub_vendor.pos_y, hub_vendor.pos_z) == (22, 251, 5)
    assert hub_vendor.heading == 270
    assert all(p.zone_type == "thehub" for p in placements)


@_needs_extracter
def test_parsing_excludes_terrain_and_checkpoints():
    # Act
    placements = parse_world_npc_placements(
        os.path.join(EXTRACTER, "TheHub.world"), "thehub")

    # Assert — no terrain/misc/checkpoint leaks into the NPC set.
    for p in placements:
        low = p.gc_type.lower()
        assert "terrain." not in low
        assert "checkpoint" not in low
        assert "worldobjectgroup" not in low


@_needs_extracter
def test_pvp_hub_has_single_authored_vendor():
    # Act
    placements = parse_world_npc_placements(
        os.path.join(EXTRACTER, "PVP_hub.world"), "pvp_hub")

    # Assert
    assert [p.gc_type for p in placements] == ["world.test.NPC_HubVendor"]
    assert placements[0].pos_x == 220 and placements[0].pos_y == -110


@_needs_extracter
def test_portal_sub_hubs_author_no_npcs():
    # Faithful = empty: the dungeon-portal hubs ship without NPCs.
    for world_file in ("TheHubPortals_Dungeon01.world",
                       "TheHub_OldLinks.world", "BugHub.world"):
        path = os.path.join(EXTRACTER, world_file)
        if not os.path.isfile(path):
            continue
        assert parse_world_npc_placements(path, "x") == []


@_needs_extracter
def test_collect_new_npcs_is_hubs_only():
    # Act
    collected = collect_new_npcs(EXTRACTER)

    # Assert — only authored hub NPCs; town is left as the client/C# ship it.
    by_zone = {}
    for p in collected:
        by_zone.setdefault(p.zone_type, []).append(p)
    assert set(by_zone) == {"thehub", "pvp_hub"}
    assert len(by_zone["thehub"]) == 4
    assert len(by_zone["pvp_hub"]) == 1


# ── DB import (add-only / idempotent) on a throwaway copy ──────────────────

@_needs_extracter
@_needs_db
def test_import_removes_fabricated_town_and_is_idempotent():
    # Arrange — a temp copy of the live DB (never touch the real one).
    db = _paths.copy_shipped_db()
    conn = sqlite3.connect(db)
    try:
        # Act — import.
        import_world_npcs(conn, EXTRACTER)
        conn.commit()

        # Assert — the fabricated town extras are gone (self-heal to C# set).
        for gc in DEPRECATED_TOWN_NPCS:
            assert conn.execute(
                "SELECT COUNT(*) FROM npcs WHERE gc_type=?", (gc,)).fetchone()[0] == 0
        # Town is the faithful, client-matching 24; hubs are authored.
        assert conn.execute(
            "SELECT COUNT(*) FROM npcs WHERE zone_type='town'").fetchone()[0] == 24
        assert conn.execute(
            "SELECT COUNT(*) FROM npcs WHERE zone_type='thehub'").fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM npcs WHERE zone_type='pvp_hub'").fetchone()[0] == 1

        # Act — re-import is a no-op (idempotent: nothing added, nothing left to remove).
        again = import_world_npcs(conn, EXTRACTER)
        conn.commit()
        assert again == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM npcs WHERE zone_type='town'").fetchone()[0] == 24
    finally:
        conn.close()


@_needs_extracter
@_needs_db
def test_hub_vendor_registered_as_merchant():
    # Arrange
    db = _paths.copy_shipped_db()
    conn = sqlite3.connect(db)
    try:
        # Act
        import_world_npcs(conn, EXTRACTER)
        conn.commit()

        # Assert — HubVendor now sells, with at least one inventory item.
        row = conn.execute(
            "SELECT id FROM merchants WHERE npc_gc_type='world.test.NPC_HubVendor'"
        ).fetchone()
        assert row is not None, "HubVendor should be a registered merchant"
        items = conn.execute(
            "SELECT COUNT(*) FROM merchant_inventory_items WHERE merchant_id=?",
            (row[0],)).fetchone()[0]
        assert items > 0
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
