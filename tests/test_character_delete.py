"""Character-delete handler tests (character channel type 0x04).

Drives GameServer._handle_character_delete the way the real client does. The
request layout — ``[cstring name][uint32 id]`` — and the type-0x04 opcode were
captured live against the client 2026-06-04 (server.log, e.g.
``53 74 79 78 33 00 | 01 00 00 00`` = "Styx3", id 1). Asserts the delete ack
``[0x04,0x04][uint32 id]`` is sent, the character is removed from both the
in-memory list and the DB, and that a character the login does NOT own cannot be
deleted.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from _paths import copy_shipped_db, has_shipped_db

from drserver.core import settings
from drserver.core.config import ServerConfig
from drserver.data import class_config
from drserver.db import account_repository as accounts
from drserver.db import character_repository as chars
from drserver.db import game_database
from drserver.net import framing
from drserver.net.connection import RRConnection
from drserver.net.game_server import GameServer
from drserver.util.byte_io import LEWriter


class _FakeWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, _name):
        return ("127.0.0.1", 5000)

    def all_bytes(self) -> bytes:
        return b"".join(self.chunks)


def _decode(raw: bytes) -> list[framing.ChannelMessage]:
    out = []
    for frame in framing.split_frames(raw):
        if frame and frame[0] == 0x0A:
            out.append(framing.parse_compressed_a(frame))
    return out


def _build_delete_payload(name: str, char_id: int) -> bytes:
    w = LEWriter()
    w.write_cstring(name)
    w.write_uint32(char_id)
    return w.to_array()


@pytest.fixture()
def server_conn():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    db_path = copy_shipped_db()
    game_database.initialize(db_path)
    settings.load()
    class_config.load()
    from drserver.managers.zones import zone_registry
    zone_registry.load()

    server = GameServer(ServerConfig(database_path=db_path))
    writer = _FakeWriter()
    conn = RRConnection(1, reader=None, writer=writer)
    conn.login_name = accounts.create_account("DelHandlerAcct", "pw") and "DelHandlerAcct"
    # Two characters so the list is non-trivial after one delete.
    chars.create_character("Doomed", "Fighter", accounts.get_account_id("DelHandlerAcct"), "DelHandlerAcct")
    chars.create_character("Keeper", "Fighter", accounts.get_account_id("DelHandlerAcct"), "DelHandlerAcct")
    server._start_character_flow(conn)
    return server, conn, writer


def test_delete_owned_character(server_conn):
    server, conn, writer = server_conn
    roster = server.persistent_characters[conn.login_name]
    doomed = next(c for c in roster if c.name == "Doomed")
    doomed_id = doomed.id

    writer.chunks.clear()
    data = _build_delete_payload("Doomed", doomed_id)
    server._handle_character_channel(conn, 4, data)

    # Delete ack [0x04, 0x04, uint32 id] present.
    acks = [m for m in _decode(writer.all_bytes())
            if len(m.payload) >= 6 and m.payload[0] == 4 and m.payload[1] == 4]
    assert len(acks) == 1
    assert int.from_bytes(acks[0].payload[2:6], "little") == doomed_id

    # Removed from the in-memory roster and from the DB.
    assert all(c.id != doomed_id for c in server.persistent_characters[conn.login_name])
    assert chars.get_character(doomed_id) is None
    # The other character is untouched.
    assert any(c.name == "Keeper" for c in server.persistent_characters[conn.login_name])


def test_delete_unowned_character_refused(server_conn):
    server, conn, writer = server_conn
    before = {c.id for c in server.persistent_characters[conn.login_name]}
    unowned_id = max(before) + 9999  # an id this login does not own

    writer.chunks.clear()
    server._handle_character_channel(conn, 4, _build_delete_payload("Hacker", unowned_id))

    # No delete ack; roster unchanged.
    acks = [m for m in _decode(writer.all_bytes())
            if len(m.payload) >= 2 and m.payload[0] == 4 and m.payload[1] == 4]
    assert acks == []
    assert {c.id for c in server.persistent_characters[conn.login_name]} == before


if __name__ == "__main__":
    import traceback

    if not has_shipped_db():
        print("SKIP: shipped DB not present")
        sys.exit(0)
    settings.load()
    class_config.load()
    from drserver.managers.zones import zone_registry
    failed = 0
    for name in ("test_delete_owned_character", "test_delete_unowned_character_refused"):
        # Fresh DB copy per test (mirrors pytest function-scoped fixture).
        db_path = copy_shipped_db()
        game_database.initialize(db_path)
        zone_registry.load()
        server = GameServer(ServerConfig(database_path=db_path))
        writer = _FakeWriter()
        conn = RRConnection(1, reader=None, writer=writer)
        accounts.create_account("DelHandlerAcct", "pw")
        conn.login_name = "DelHandlerAcct"
        chars.create_character("Doomed", "Fighter", accounts.get_account_id("DelHandlerAcct"), "DelHandlerAcct")
        chars.create_character("Keeper", "Fighter", accounts.get_account_id("DelHandlerAcct"), "DelHandlerAcct")
        server._start_character_flow(conn)
        try:
            globals()[name]((server, conn, writer))
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{2 - failed}/2 passed")
    sys.exit(1 if failed else 0)
