"""CombatManager.handle_hp_sync adopts the client's own-avatar HP report.

The vanilla client is authoritative for its OWN avatar HP: it self-levels on
kills and recomputes HP locally (L1 68096 -> L2 72192), then reports that value
to the server via the 0x36/0x03 entity-synch suffix. If the server keeps echoing
a stale level-based value in its 0x02 synch trailers, the client's
processComponentUpdate compare (FUN_005dd900) mismatches and fatally crashes the
Avatar on dungeon zones (exit 0xc000013a). The server must trust the client's
report and feed it back so every outbound trailer matches — mirroring C#
UnityGameServer.ObserveClientPlayerHP / Networking/Sync/HpSyncService.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers.combat import CombatManager
from drserver.util.byte_io import LEReader, LEWriter


def _server():
    return types.SimpleNamespace(connections={}, combat=None, quests=None)


def _conn(avatar_id=510, hp_wire=68096):
    return types.SimpleNamespace(
        conn_id=1, login_name="Styx3", hp_wire=hp_wire,
        avatar=types.SimpleNamespace(id=avatar_id),
    )


def _hp_sync_reader(entity_id, hp_wire):
    """Build a 0x36/0x03 body: [entityId:u16][flags:1=0x02][hp:u32]."""
    w = LEWriter()
    w.write_uint16(entity_id)
    w.write_byte(0x02)            # flags: HP present
    w.write_uint32(hp_wire)
    return LEReader(w.to_array())


def test_adopts_own_avatar_levelup_hp():
    # Arrange
    cm = CombatManager(_server())
    conn = _conn(avatar_id=510, hp_wire=68096)        # L1

    # Act — client reports its locally-leveled L2 HP for its own avatar
    cm.handle_hp_sync(conn, _hp_sync_reader(510, 72192), source="HP-SYNC-0x36")

    # Assert — server adopts it so the next trailer matches
    assert conn.hp_wire == 72192


def test_adopts_own_avatar_damage_hp():
    # Arrange
    cm = CombatManager(_server())
    conn = _conn(avatar_id=510, hp_wire=68096)

    # Act — client reports current (damaged) HP
    cm.handle_hp_sync(conn, _hp_sync_reader(510, 40000), source="HP-SYNC-0x36")

    # Assert
    assert conn.hp_wire == 40000


def test_rejects_sentinel_hp():
    # Arrange
    cm = CombatManager(_server())
    conn = _conn(avatar_id=510, hp_wire=68096)

    # Act — 0xFFFF00 is the MP/no-data sentinel, not a real HP
    cm.handle_hp_sync(conn, _hp_sync_reader(510, 0xFFFF00), source="HP-SYNC-0x36")

    # Assert — rejected, hp_wire unchanged
    assert conn.hp_wire == 68096


def test_rejects_zero_hp():
    # Arrange
    cm = CombatManager(_server())
    conn = _conn(avatar_id=510, hp_wire=68096)

    # Act
    cm.handle_hp_sync(conn, _hp_sync_reader(510, 0), source="HP-SYNC-0x36")

    # Assert — 0 left to respawn/refresh, not echoed
    assert conn.hp_wire == 68096


def test_monster_report_does_not_touch_avatar_hp_wire():
    # Arrange
    cm = CombatManager(_server())
    cm.register_monster(50200, "t", "M", 5000, 5, "GRUNT", "world.town", 0, 0, 0)
    conn = _conn(avatar_id=510, hp_wire=68096)

    # Act — a monster report (equal HP: no broadcast/death) must not alter the avatar wire
    cm.handle_hp_sync(conn, _hp_sync_reader(50200, 5000), source="HP-SYNC-0x36")

    # Assert
    assert conn.hp_wire == 68096
    assert cm.get_monster(50200).current_hp == 5000
