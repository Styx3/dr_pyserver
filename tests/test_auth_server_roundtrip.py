"""End-to-end auth round-trip: in-process client drives the real AuthServer.

Connects to the running auth listener, sends a Login (0x00) frame encrypted the
way the client does, and asserts the server replies with LoginOk (0x03) then the
server list (0x04). Exercises the full chain: frame crypto, login decode, account
auto-create (temp DB), and response framing.
"""
import asyncio
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.core import settings
from drserver.core.config import ServerConfig
from drserver.db import game_database
from drserver.net import auth_crypto as ac
from drserver.net.auth_server import AuthServer
from drserver.util.crypto import DESEncryption

KEY = "[;'.]94-31==-%&@!^+]"
DES_KEY = "TEST"


def _build_login_frame(user: str, password: str) -> bytes:
    blob = bytearray(30)
    blob[0 : len(user)] = user.encode("ascii")
    blob[14 : 14 + len(password)] = password.encode("ascii")
    block24 = DESEncryption(DES_KEY).encrypt(bytes(blob[0:24]))
    tail6 = bytes(blob[24:30])

    body = bytes([0x00]) + block24 + tail6  # 31 bytes
    if len(body) % 8 != 0:
        body += b"\x00" * (8 - len(body) % 8)  # pad to 32
    enc = ac._blowfish_encrypt(body, KEY)
    return struct.pack("<H", len(enc) + 2) + enc


async def _read_auth_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(2)
    total_len = header[0] | (header[1] << 8)
    body = await reader.readexactly(total_len - 2)
    return ac._blowfish_decrypt(body, KEY)


async def _run() -> None:
    tmp = tempfile.mkdtemp()
    game_database.initialize(os.path.join(tmp, "test.db"))
    settings.load()

    config = ServerConfig()
    config.auth_server_port = 0  # ephemeral

    server = AuthServer(config)
    # ``start_server`` already begins accepting connections. Do NOT spawn a
    # ``serve_forever()`` task: with one running, the server's ``wait_closed()``
    # blocks forever on teardown (Python 3.12), which used to hang the suite.
    srv = await asyncio.start_server(server._handle_auth, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        welcome = await asyncio.wait_for(reader.readexactly(3), timeout=3)
        assert welcome == bytes([3, 0, 0]), f"bad welcome {welcome!r}"

        writer.write(_build_login_frame("TestUser", "pw123"))
        await writer.drain()

        first = await asyncio.wait_for(_read_auth_frame(reader), timeout=3)
        assert first[0] == 0x03, f"expected LoginOk 0x03, got 0x{first[0]:02X}"
        second = await asyncio.wait_for(_read_auth_frame(reader), timeout=3)
        assert second[0] == 0x04, f"expected ServerList 0x04, got 0x{second[0]:02X}"

        # Account should now exist in the DB.
        from drserver.db import account_repository as accounts
        assert accounts.get_account_id("TestUser") != 0
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                pass
        srv.close()
        try:
            await asyncio.wait_for(srv.wait_closed(), timeout=2)
        except asyncio.TimeoutError:
            pass
    print("PASS test_auth_login_roundtrip")


def test_auth_login_roundtrip():
    # Overall guard so a protocol regression can never hang the whole suite.
    asyncio.run(asyncio.wait_for(_run(), timeout=15))


if __name__ == "__main__":
    import traceback
    try:
        test_auth_login_roundtrip()
        print("\n1/1 passed")
    except Exception:
        traceback.print_exc()
        print("\n0/1 passed")
        sys.exit(1)
