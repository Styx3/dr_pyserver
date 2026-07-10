"""Auth + game servers.

Ported from Go RainbowRunner message types (verified working). Two asyncio
listeners:

  * auth port (2110): Login (0x00) -> LoginOk (0x03) -> ServerListExt (0x05)
    -> ServerList (0x04) -> AboutToPlay (0x02) -> PlayOk (0x07).
  * game port (2603): initial connection with OneTimeKey from PlayOk response.

Auth message types (from the client PDB):
  0x00 ProtocolVer  0x01 LoginFail  0x02 BlockedAccount  0x03 LoginOk
  0x04 ServerList   0x05 ServerFail  0x06 PlayFail  0x07 PlayOk
  0x08 AccountKicked  0x09 BlockedAccountMsg  0x0A CSCCheck
  0x0B QueueSize    0x0C HandoffToQueue  0x0D PositionInQueue
  0x0E HandoffToGame
"""
from __future__ import annotations

import asyncio
import struct
import time

from ..core import log, settings
from ..core.config import ServerConfig
from ..core.sessions import global_sessions, queue_bridge, session_manager
from ..db import account_repository as accounts
from ..util.byte_io import LEReader, LEWriter
from . import auth_crypto as ac


class AuthServer:
    def __init__(self, config: ServerConfig):
        self.config = config

    async def start(self) -> None:
        auth_ip = settings.get_string("authIP", self.config.auth_server_ip)
        auth_port = settings.get_int("authPort", self.config.auth_server_port)

        auth_srv = await asyncio.start_server(self._handle_auth, auth_ip, auth_port)
        log.info(f"Auth server listening on {auth_ip}:{auth_port}")
        await auth_srv.serve_forever()

    # ─────────────────────────── AUTH PORT ───────────────────────────
    async def _handle_auth(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info(f"Auth client connected: {peer}")
        username: str | None = None
        account_id: int = 0
        try:
            # Go welcome packet: unencrypted 3-byte init [3, 0, 0].
            writer.write(bytes([3, 0, 0]))
            await writer.drain()

            while True:
                header = await reader.readexactly(2)
                total_len = header[0] | (header[1] << 8)
                if total_len < 4:
                    log.warn(f"[AUTH] bad frame length {total_len}")
                    break
                body = await reader.readexactly(total_len - 2)
                plain = ac.decrypt_auth_body(body, self.config.blowfish_key)
                if not plain:
                    continue
                msg_type = plain[0]
                if msg_type == 0x00:
                    username, account_id = self._handle_login(writer, plain)
                elif msg_type == 0x02:
                    self._handle_about_to_play(writer, plain, username)
                elif msg_type == 0x05:
                    self._send_server_list(writer)
                else:
                    log.warn(f"[AUTH] unknown message 0x{msg_type:02X}")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as ex:  # noqa: BLE001
            log.error(f"[AUTH] handler error: {ex}")
        finally:
            log.info(f"Auth client disconnected: {peer}")
            writer.close()

    def _handle_login(self, writer: asyncio.StreamWriter, data: bytes) -> tuple[str | None, int]:
        if len(data) < 31:
            log.warn("[AUTH] login data too short")
            return None, 0
        r = LEReader(data)
        r.read_byte()  # msg type
        login_block = r.read_bytes(24)
        tail6 = r.read_bytes(6)
        username, password = ac.decode_login(login_block, tail6, self.config.des_key)

        if not username:
            log.warn("[AUTH] login decode produced empty username")
            return None, 0

        log.info(f"[AUTH] login attempt: {username}")

        account_id = accounts.get_account_id(username)
        if account_id == 0:
            account_id = accounts.create_account(username, password)
            if account_id == 0:
                log.error(f"[AUTH] failed to create account for '{username}'")
                return username, 0
        else:
            if password and not accounts.verify_password(username, password):
                log.warn(f"[AUTH] invalid password for '{username}' (pw='{password}')")
                self._send_login_fail(writer)
                return username, 0

        if accounts.is_banned(username):
            log.error(f"[AUTH] account '{username}' is banned")
            self._send_blocked_account(writer)
            return username, 0

        self._send_login_ok(writer, account_id)
        self._send_server_list(writer)
        return username, account_id

    def _handle_about_to_play(self, writer, data: bytes, username: str | None) -> None:
        r = LEReader(data)
        r.read_byte()  # msg type
        session_lo = r.read_uint32()
        session_hi = r.read_uint32()
        server_id = r.read_byte()
        log.info(f"[AUTH] AboutToPlay session=0x{session_hi:08X}{session_lo:08X} serverId={server_id}")

        session_token = ((time.time_ns() // 100) ^ 0x12345678) & 0xFFFFFFFF
        user = username or "unknown"
        global_sessions.store(session_token, user)

        # PlayOk (0x07) only — never send HandoffToGame (0x0E) here.
        # The client closes the auth TCP immediately after PlayOk; 0x0E is an
        # internal queue-server-to-game-server relay, not a client-facing message.
        self._send_play_ok(writer, session_token, server_id)

    def _send_login_ok(self, writer, account_id: int) -> None:
        w = LEWriter()
        for v in (0xFFEEFFEE, 0xAABBAABB, 0xDDCCDDCC, 0xBBCCBBCC,
                   0x00000000, 0xFFFFFFFF, 0xFFFFFFFF, 0x00000000, 0x00000000):
            w.write_uint32(v)
        w.write_byte(0x01)
        w.write_byte(0x01)
        w.write_byte(0x01)
        writer.write(ac.build_auth_frame(0x03, w.to_array(), self.config.blowfish_key))

    def _send_login_fail(self, writer) -> None:
        w = LEWriter()
        w.write_byte(0x00)
        writer.write(ac.build_auth_frame(0x01, w.to_array(), self.config.blowfish_key))

    def _send_blocked_account(self, writer) -> None:
        w = LEWriter()
        w.write_byte(0x00)
        writer.write(ac.build_auth_frame(0x02, w.to_array(), self.config.blowfish_key))

    def _send_server_list(self, writer) -> None:
        ip_int = ac.ip_to_uint32_le(self.config.game_server_ip)
        game_port = self.config.game_server_port

        player_count = 0
        try:
            from .game_server import _active_game_server
            if _active_game_server:
                player_count = len(_active_game_server.connections)
        except Exception:
            pass

        w = LEWriter()
        w.write_byte(0x01)        # server count
        w.write_byte(0x00)        # last server ID

        w.write_byte(0x00)        # server ID
        w.write_uint32(ip_int)    # IP
        w.write_uint32(game_port) # port
        w.write_byte(0x00)        # age limit
        w.write_byte(0x00)        # PK flag
        w.write_uint16(player_count)    # online
        w.write_uint16(0x00FF)          # max
        w.write_byte(0x01)        # status: online

        writer.write(ac.build_auth_frame(0x04, w.to_array(), self.config.blowfish_key))
        log.info(f"[AUTH] server list sent (port {game_port}, players {player_count})")

    def _send_play_ok(self, writer, one_time_key: int, server_id: int) -> None:
        w = LEWriter()
        w.write_uint32(one_time_key)
        w.write_uint32(0x5678DEFA)
        w.write_byte(server_id)
        writer.write(ac.build_auth_frame(0x07, w.to_array(), self.config.blowfish_key))
        log.info(f"[AUTH] PlayOk sent (key=0x{one_time_key:08X})")

    def _send_handoff_to_game_raw(self, writer, one_time_key: int, server_id: int) -> None:
        """Send HandoffToGame in raw queue transport format (unencrypted).

        The C# queue server sends this as a length-prefixed unencrypted message,
        not wrapped in a Blowfish auth frame.
        """
        ip_int = ac.ip_to_uint32_le(self.config.game_server_ip)
        game_port = self.config.game_server_port

        w = LEWriter()
        w.write_byte(0x0E)              # HandoffToGame type
        w.write_uint32(ip_int)          # Game server IP
        w.write_uint32(game_port)       # Game server port
        w.write_uint32(one_time_key)    # One-time key
        w.write_uint32(0)               # padding zero
        payload = w.to_array()

        # Raw transport: [uint32 len][payload]
        frame = struct.pack("<I", len(payload)) + payload
        writer.write(frame)
        log.info(f"[AUTH] HandoffToGame raw sent (token=0x{one_time_key:08X}, "
                 f"{self.config.game_server_ip}:{game_port})")
