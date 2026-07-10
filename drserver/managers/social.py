"""Social manager — friends, ignore, /who, /tell.

Ported from C# SocialManager.cs. Handles the social roster channel (0x0C)
with friends add/remove, ignore add/remove, online/offline notifications,
busy flags, and "/who" list queries. Persists all data to SQLite.

Phase 10: MVP with friends/ignores + /who + /tell. Busy flags and
friends publicity deferred.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection


class SocialManager:
    """Per-character social state."""

    def __init__(self, server: "GameServer"):
        self._server = server
        self._friends: Dict[str, Set[str]] = {}     # login_name -> set of friend login_names
        self._ignores: Dict[str, Set[str]] = {}     # login_name -> set of ignored login_names
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._friends.clear()
        self._ignores.clear()

        try:
            for row in db.execute_reader("SELECT * FROM social_friends_v2").fetchall():
                player = db.get_string(row, "player_login") or db.get_string(row, "player")
                friend = db.get_string(row, "friend_login") or db.get_string(row, "friend")
                if player and friend:
                    if player not in self._friends:
                        self._friends[player] = set()
                    self._friends[player].add(friend)

            for row in db.execute_reader("SELECT * FROM social_ignores_v2").fetchall():
                player = db.get_string(row, "player_login") or db.get_string(row, "player")
                ignored = db.get_string(row, "ignored_login") or db.get_string(row, "ignored")
                if player and ignored:
                    if player not in self._ignores:
                        self._ignores[player] = set()
                    self._ignores[player].add(ignored)

            self._loaded = True
            log.info(f"[Social] loaded {sum(len(v) for v in self._friends.values())} friends, "
                     f"{sum(len(v) for v in self._ignores.values())} ignores")
        except Exception as ex:
            log.error(f"[Social] load error: {ex}")

    def add_friend(self, conn: "RRConnection", friend_name: str) -> None:
        login = conn.login_name
        if not login:
            return
        if login not in self._friends:
            self._friends[login] = set()
        self._friends[login].add(friend_name.lower())

        try:
            db.execute_non_query(
                "INSERT OR IGNORE INTO social_friends_v2 (player_login, friend_login) VALUES (:p, :f)",
                {"p": login, "f": friend_name.lower()})
        except Exception:
            pass

        self._send_chat(conn, f"Added friend: {friend_name}")

    def remove_friend(self, conn: "RRConnection", friend_name: str) -> None:
        login = conn.login_name
        if login and login in self._friends:
            self._friends[login].discard(friend_name.lower())
        try:
            db.execute_non_query(
                "DELETE FROM social_friends_v2 WHERE player_login=:p AND friend_login=:f",
                {"p": login, "f": friend_name.lower()})
        except Exception:
            pass

        self._send_chat(conn, f"Removed friend: {friend_name}")

    def add_ignore(self, conn: "RRConnection", target_name: str) -> None:
        login = conn.login_name
        if not login:
            return
        if login not in self._ignores:
            self._ignores[login] = set()
        self._ignores[login].add(target_name.lower())

        try:
            db.execute_non_query(
                "INSERT OR IGNORE INTO social_ignores_v2 (player_login, ignored_login) VALUES (:p, :i)",
                {"p": login, "i": target_name.lower()})
        except Exception:
            pass

        self._send_chat(conn, f"Ignoring: {target_name}")

    def remove_ignore(self, conn: "RRConnection", target_name: str) -> None:
        login = conn.login_name
        if login and login in self._ignores:
            self._ignores[login].discard(target_name.lower())
        try:
            db.execute_non_query(
                "DELETE FROM social_ignores_v2 WHERE player_login=:p AND ignored_login=:i",
                {"p": login, "i": target_name.lower()})
        except Exception:
            pass

        self._send_chat(conn, f"Stopped ignoring: {target_name}")

    def send_tell(self, conn: "RRConnection", target_name: str, message: str) -> None:
        """Send a direct tell message to a target player."""
        sender_login = conn.login_name
        target_conn = self._find_player(target_name)
        if target_conn is None:
            self._send_chat(conn, f"Player '{target_name}' not found or offline.")
            return

        # Check if target ignores sender.
        target_ignores = self._ignores.get(target_conn.login_name, set())
        if sender_login and sender_login.lower() in target_ignores:
            self._send_chat(conn, f"Player '{target_name}' is ignoring you.")
            return

        # Send tell to target. Channel byte MUST be 0x06 (chat); byte4 is the
        # tell direction flag (0x02 = incoming).
        w = LEWriter()
        w.write_byte(0x06)           # Chat channel
        w.write_byte(0x00)           # Chat message
        w.write_byte(0x04)           # Tell source
        w.write_byte(0x02)           # incoming
        w.write_cstring(sender_login or "Unknown")
        w.write_cstring(message)
        target_conn.send_to_client(w.to_array())

        # Echo to sender (direction flag 0x01 = outgoing).
        w2 = LEWriter()
        w2.write_byte(0x06)
        w2.write_byte(0x00)
        w2.write_byte(0x04)          # Tell2 (echo)
        w2.write_byte(0x01)
        w2.write_cstring(target_conn.login_name or target_name)
        w2.write_cstring(message)
        conn.send_to_client(w2.to_array())

    def send_who(self, conn: "RRConnection") -> None:
        """Send /who list — all online players."""
        online = []
        for c in self._server.connections.values():
            if c.is_spawned and c.login_name:
                online.append(f"{c.login_name} (Lv{c.player_level}, {c.current_zone_name})")
        if online:
            self._send_chat(conn, f"Online ({len(online)}): " + ", ".join(online))
        else:
            self._send_chat(conn, "No players online.")

    # ── Internal helpers ──

    def _find_player(self, name: str) -> Optional["RRConnection"]:
        name_lower = name.lower()
        for conn in self._server.connections.values():
            if conn.login_name and conn.login_name.lower() == name_lower:
                return conn
            if conn.login_name and conn.login_name.lower().startswith(name_lower):
                return conn
        return None

    def _send_chat(self, conn: "RRConnection", text: str) -> None:
        from ..net.chat_commands import _send_chat
        _send_chat(conn, text)
