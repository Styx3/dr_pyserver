"""Integration test for the game server connection lifecycle.

Drives the GameServer handlers with a fake stream writer that captures the
outbound frames, then decodes them back into channel messages and asserts the
expected sequence: connect -> initial -> character list -> play -> zone
progression -> player spawn (0x46-terminated). Requires the shipped DB.

Run: ./.venv/bin/python tests/test_game_server_lifecycle.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _paths import copy_shipped_db, has_shipped_db

from drserver.core.config import ServerConfig
from drserver.core.sessions import global_sessions
from drserver.db import game_database
from drserver.net import framing
from drserver.net.connection import RRConnection
from drserver.net.game_server import GameServer
from drserver.util.byte_io import LEWriter

# A throwaway copy of the shipped DB (a FILE, not the Database/ directory).
# Resolved when first needed so collection stays cheap and skips cleanly.
DB_PATH = copy_shipped_db() if has_shipped_db() else None
ACCOUNT = "Styx3"

_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


class FakeWriter:
    """Captures bytes written; satisfies the StreamWriter surface RRConnection uses."""

    def __init__(self):
        self.chunks: list[bytes] = []
        self._closing = False

    def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing

    def get_extra_info(self, _name):
        return ("127.0.0.1", 5000)

    def all_bytes(self) -> bytes:
        return b"".join(self.chunks)


def decode_channel_messages(raw: bytes) -> list[framing.ChannelMessage]:
    msgs: list[framing.ChannelMessage] = []
    for frame in framing.split_frames(raw):
        if not frame:
            continue
        if frame[0] == 0x0A:
            msgs.append(framing.parse_compressed_a(frame))
        elif frame[0] == 0x10:
            msgs.append(framing.parse_direct(frame))
    return msgs


def run_lifecycle():
    game_database.initialize(DB_PATH)
    config = ServerConfig(database_path=DB_PATH)
    server = GameServer(config)
    from drserver.managers.zones import zone_registry
    zone_registry.load()

    writer = FakeWriter()
    conn = RRConnection(1, reader=None, writer=writer)  # reader unused in handler-level test

    # 1. Connect handshake.
    server._handle_connect(conn, bytes([0x03, 0x11, 0x22, 0x33]))
    check("peer_id_set", conn.peer_id24 == 0x332211)
    connect_frames = framing.split_frames(writer.all_bytes())
    check("connect_response_0x04", connect_frames and connect_frames[0][0] == 0x04)
    writer.chunks.clear()

    # 2. Initial connection via OneTimeKey.
    token = 0xDEADBEEF
    global_sessions.store(token, ACCOUNT)
    init = LEWriter()
    init.write_byte(0x00)            # subtype
    init.write_uint32(token)
    server._handle_initial_connection(conn, 0x00, init.to_array())
    check("login_name_set", conn.login_name == ACCOUNT)
    check("chars_loaded", len(server.persistent_characters.get(ACCOUNT, [])) >= 1)
    writer.chunks.clear()

    # 3. Character list request (4/3).
    server._handle_character_channel(conn, 3, b"")
    list_msgs = decode_channel_messages(writer.all_bytes())
    char_entries = [m for m in list_msgs if len(m.payload) >= 2 and m.payload[0] == 4 and m.payload[1] == 2]
    check("char_list_has_entries", len(char_entries) >= 1)
    writer.chunks.clear()

    # 4. Play character (4/5). Select the account's OWN first character rather
    # than a hardcoded id: the shipped DB has accumulated characters across
    # multiple accounts from live testing, and _handle_character_play correctly
    # refuses to select a character the login doesn't own (falling back to its
    # own chars[0]). Picking dynamically keeps this test drift-proof.
    owned_id = server.persistent_characters[ACCOUNT][0].id
    play = LEWriter()
    play.write_uint32(owned_id)
    server._handle_character_channel(conn, 5, play.to_array())
    check("selected_character", server.selected_character[ACCOUNT].id == owned_id)
    play_msgs = decode_channel_messages(writer.all_bytes())
    zone_info = [m for m in play_msgs if m.payload and m.payload[0] == 13 and m.payload[1] == 0]
    check("zone_info_sent", len(zone_info) == 1)
    check("zone_id_assigned", conn.current_zone_id != 0)
    writer.chunks.clear()

    # 5. Zone join (empty 13/6) -> progression + spawn.
    server._handle_zone_channel(conn, b"")
    join_msgs = decode_channel_messages(writer.all_bytes())

    ready = [m for m in join_msgs if m.payload and m.payload[0] == 13 and m.payload[1] == 1]
    instance = [m for m in join_msgs if m.payload and m.payload[0] == 13 and m.payload[1] == 5]
    interval = [m for m in join_msgs if m.payload and m.payload[0] == 7 and m.payload[1] == 0x0D]
    spawn_pkts = [m for m in join_msgs if m.payload and m.payload[0] == 0x07 and m.payload[-1] == 0x46]
    check("zone_ready_sent", len(ready) == 1)
    check("instance_count_sent", len(instance) == 1)
    check("interval_sent", len(interval) == 1)
    check("spawn_packet_0x46", len(spawn_pkts) == 1)
    check("is_spawned", conn.is_spawned is True)
    check("unit_behavior_id_set", conn.unit_behavior_id != 0)

    if spawn_pkts:
        sp = spawn_pkts[0]
        check("spawn_begins_with_create_avatar", sp.payload[1] == 0x01)
        # Avatar id is the first uint16 after BeginStream + create opcode.
        avatar_id = sp.payload[2] | (sp.payload[3] << 8)
        check("spawn_avatar_id_matches_conn", avatar_id == conn.avatar.id)

    # Cancel the tick task started by spawn so the loop can exit cleanly.
    if conn._tick_task is not None:
        conn._tick_task.cancel()


def test_movement_parse():
    """A 0x65 move updates the connection's authoritative position."""
    if DB_PATH is None:
        import pytest
        pytest.skip("shipped content DB not present")
    from drserver.net import movement
    game_database.initialize(DB_PATH)
    config = ServerConfig(database_path=DB_PATH)
    server = GameServer(config)
    conn = RRConnection(2, reader=None, writer=FakeWriter())
    conn.is_spawned = True

    # Build entity-stream: 0x07, 0x35, u16 cid=1234, 0x65, sessionId, moveCount=1,
    # [moveType, heading, posX, posY], 0x00, 0x06
    inner = LEWriter()
    inner.write_byte(0x35)
    inner.write_uint16(1234)
    inner.write_byte(0x65)
    inner.write_byte(0x07)              # session id
    inner.write_byte(0x01)              # move count
    inner.write_byte(0x03)              # move type
    inner.write_int32(int(1.5 * 256))   # heading
    inner.write_int32(int(123.0 * 256)) # posX
    inner.write_int32(int(-45.0 * 256)) # posY
    inner.write_byte(0x00)
    inner.write_byte(0x06)

    movement.handle(server, conn, 0x07, inner.to_array())
    check("move_pos_x", abs(conn.player_pos_x - 123.0) < 0.01)
    check("move_pos_y", abs(conn.player_pos_y - (-45.0)) < 0.01)
    check("move_session_id", conn.session_id == 0x07)


async def amain():
    run_lifecycle()
    test_movement_parse()


if __name__ == "__main__":
    asyncio.run(amain())
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)
