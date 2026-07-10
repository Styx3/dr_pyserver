"""On-NPC ``NPCTeleporter`` → QM inbound op 0x08 ("Teleport to X" dialog option).

Covers the four pieces that wire the Snowman Sanctuary teleporter (bible §13.5 #8):
the ``.gc`` ``Teleporter`` parser, the additive ``npc_teleporters`` companion table,
the ``NPCManager`` load + case-insensitive lookup, and the quest handler that reads
``u32 npcEntityId`` and changes zone. AAA style, matching ``tests/``.
"""
import os
import sqlite3
import types

import pytest

import _paths
from drserver.util.byte_io import LEReader
from drserver.data.world_npc_importer import (
    import_npc_teleporters,
    parse_npc_teleporters,
)

EXTRACTER = os.path.normpath(os.path.join(_paths.REPO_ROOT, "..", "extracter"))
_needs_extracter = pytest.mark.skipif(
    not os.path.isdir(EXTRACTER), reason="extracter content not present")
_needs_db = pytest.mark.skipif(
    not _paths.has_shipped_db(), reason="shipped DB not present")


SNOWMAN_GC = """SnowMan1 extends npc.misc.SnowMan.Basic.Default
{
    Name = Snowman_QuestGiver;
    Description
    {
        Teleporter extends NPCTeleporter
        {
            Label = "Teleport to Snowman Sanctuary";
            Zone = dungeon_snowman;
            SpawnPoint = start;
        }
    }
}
"""

PLAIN_NPC_GC = """TownGuy extends npc.misc.Default
{
    Description { Label = "Just a guy"; }
}
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ── parsing ─────────────────────────────────────────────────────────────────

def test_parse_npc_teleporters_reads_block(tmp_path):
    # Arrange — an NPC with a teleporter, a plain NPC, and a world-NCI teleporter
    # (NOT under an /npc/ dir → must be excluded; it is the movement.py path).
    _write(tmp_path / "world/town/npc/SnowMan1.gc", SNOWMAN_GC)
    _write(tmp_path / "world/town/npc/TownGuy.gc", PLAIN_NPC_GC)
    _write(tmp_path / "world/test/world_nci/TestTeleporter.gc",
           SNOWMAN_GC.replace("SnowMan1", "TestTeleporter"))

    # Act
    rows = parse_npc_teleporters(str(tmp_path))

    # Assert — only the on-NPC teleporter, keyed by its path-derived gc type.
    assert len(rows) == 1
    r = rows[0]
    assert r.gc_type == "world.town.npc.SnowMan1"
    assert r.zone == "dungeon_snowman"
    assert r.spawn_point == "start"
    assert "Snowman Sanctuary" in r.label


@_needs_extracter
def test_parse_npc_teleporters_finds_shipped_snowman():
    # Act — scan the real extracted content.
    rows = {r.gc_type.lower(): r for r in parse_npc_teleporters(EXTRACTER)}

    # Assert — the shipped Snowman is the one real on-NPC teleporter.
    snow = rows.get("world.town.npc.snowman1")
    assert snow is not None, "SnowMan1's NPCTeleporter should be discovered"
    assert snow.zone == "dungeon_snowman"
    assert snow.spawn_point == "start"


# ── companion table (idempotent) ────────────────────────────────────────────

def test_import_npc_teleporters_builds_table(tmp_path):
    # Arrange
    _write(tmp_path / "world/town/npc/SnowMan1.gc", SNOWMAN_GC)
    conn = sqlite3.connect(":memory:")
    try:
        # Act
        n = import_npc_teleporters(conn, str(tmp_path))
        conn.commit()

        # Assert — row written, keyed by gc type.
        assert n == 1
        row = conn.execute(
            "SELECT zone, spawn_point FROM npc_teleporters WHERE gc_type=?",
            ("world.town.npc.SnowMan1",)).fetchone()
        assert row == ("dungeon_snowman", "start")

        # Act — re-import is idempotent (INSERT OR REPLACE, table reused).
        assert import_npc_teleporters(conn, str(tmp_path)) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM npc_teleporters").fetchone()[0] == 1
    finally:
        conn.close()


# ── NPCManager load + lookup ────────────────────────────────────────────────

@_needs_extracter
@_needs_db
def test_npc_manager_resolves_shipped_snowman_teleporter():
    # Arrange — a throwaway copy of the shipped DB, populated from the extracter.
    from drserver.db import game_database
    db_path = _paths.copy_shipped_db()
    conn = sqlite3.connect(db_path)
    try:
        import_npc_teleporters(conn, EXTRACTER)
        conn.commit()
    finally:
        conn.close()
    game_database.initialize(db_path)

    # Act
    from drserver.managers.npcs import NPCManager
    mgr = NPCManager()
    mgr.load()

    # Assert — resolved case-insensitively (the spawned NPC's gc_type case may
    # differ from the authored path); unknown NPCs resolve to None.
    assert mgr.teleporter_for("WORLD.TOWN.NPC.SnowMan1") == ("dungeon_snowman", "start")
    assert mgr.teleporter_for("world.town.npc.unknown") is None


def test_teleporter_for_missing_table_is_safe(tmp_path):
    # An un-migrated DB (no npc_teleporters table) must not raise; teleporters
    # are simply absent (op 0x08 then no-ops).
    from drserver.db import game_database
    db_path = str(tmp_path / "bare.db")
    game_database.initialize(db_path)             # base schema, no companion table

    from drserver.managers.npcs import NPCManager
    mgr = NPCManager()
    mgr.load()

    assert mgr.teleporter_for("world.town.npc.SnowMan1") is None


# ── quest handler dispatch (op 0x08) ────────────────────────────────────────

def test_op08_reads_entity_id_and_changes_zone(monkeypatch):
    # Arrange — capture change_zone; stub the NPC + teleporter resolution.
    from drserver.managers.quests import QuestManager
    from drserver.managers import npcs as npcs_mod

    captured = {}
    server = types.SimpleNamespace(
        change_zone=lambda conn, zone, spawn: captured.update(
            zone=zone, spawn=spawn))
    qm = QuestManager(server)

    npc = types.SimpleNamespace(gc_type="world.town.NPC.SnowMan1")
    monkeypatch.setattr(npcs_mod.npc_manager, "find_by_entity_id",
                        lambda eid: npc if eid == 1573 else None)
    monkeypatch.setattr(npcs_mod.npc_manager, "teleporter_for",
                        lambda gc: ("dungeon_snowman", "start"))
    qm._states[7] = object()                      # skip DB-backed init
    conn = types.SimpleNamespace(conn_id=7, login_name="Styx3")

    # Act — op 0x08 body = u32 npcEntityId.
    handled = qm.handle_component_update(
        conn, 0x08, LEReader((1573).to_bytes(4, "little")))

    # Assert
    assert handled is True
    assert captured == {"zone": "dungeon_snowman", "spawn": "start"}


def test_op08_unknown_entity_is_safe_noop(monkeypatch):
    from drserver.managers.quests import QuestManager
    from drserver.managers import npcs as npcs_mod

    calls = []
    qm = QuestManager(types.SimpleNamespace(
        change_zone=lambda *a, **k: calls.append(a)))
    monkeypatch.setattr(npcs_mod.npc_manager, "find_by_entity_id", lambda eid: None)
    conn = types.SimpleNamespace(conn_id=7, login_name="Styx3")

    qm.handle_npc_teleport(conn, 999)

    assert calls == []
