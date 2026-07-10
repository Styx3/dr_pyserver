"""Admin primitives — in-process server actions called by the admin panel.

Ported from C# AdminCommandBridge.cs + ChatCommandHandler.cs. These functions
are called directly (not via SQLite bridge) since the admin panel runs in the
same process as the game server.

All functions take the game_server instance as a parameter rather than holding
a module-level reference, so they work from any thread.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from ..core import log
from ..db import game_database as db
from ..managers.zones import zone_registry
from ..util.byte_io import LEWriter

if TYPE_CHECKING:
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection


def _find_conn(server: "GameServer", login_name: str) -> Optional["RRConnection"]:
    name_lower = login_name.lower()
    for conn in server.connections.values():
        if conn.login_name and conn.login_name.lower() == name_lower:
            return conn
    return None


def get_online_players(server: "GameServer") -> List[dict]:
    players = []
    for conn in list(server.connections.values()):
        if not conn.is_spawned or not conn.login_name:
            continue
        char = server.selected_character.get(conn.login_name)
        players.append({
            "login_name": conn.login_name,
            "conn_id": conn.conn_id,
            "char_name": char.name if char else conn.login_name,
            "class_name": conn.class_name,
            "level": conn.player_level,
            "zone": conn.current_zone_name or conn.current_zone_gc_type,
            "char_sql_id": conn.char_sql_id,
        })
    return players


def broadcast_all(server: "GameServer", message: str, color: str = "#FF4444",
                  effect: str = "glow") -> int:
    """Broadcast a system message to all online players. Returns count of recipients."""
    w = LEWriter()
    w.write_byte(0x06)    # Chat channel (was 3 — client ignores chat on ch3)
    w.write_byte(0x00)    # Chat message type
    w.write_byte(0x0E)    # source channel 0x0E = announcement
    w.write_byte(0x02)
    w.write_cstring("SERVER")
    w.write_cstring(message)
    packet = w.to_array()
    count = 0
    for conn in list(server.connections.values()):
        if conn.is_spawned:
            conn.send_to_client(packet)
            count += 1
    return count


def kick_player(server: "GameServer", login_name: str, reason: str = "",
                admin_name: str = "") -> bool:
    """Kick a player by login name. Returns True if player was found and kicked."""
    conn = _find_conn(server, login_name)
    if conn is None:
        return False

    if reason:
        _send_system_msg(conn, f"You have been kicked: {reason}")

    log_activity("kick", login_name, reason, admin_name)
    conn.is_connected = False
    try:
        conn.writer.close()
    except Exception:
        pass
    return True


def ban_player(server: "GameServer", login_name: str, reason: str = "",
               admin_name: str = "") -> bool:
    """Ban an account. Sets is_banned=1 and kicks if online. Returns True."""
    try:
        db.execute_non_query(
            "UPDATE accounts SET is_banned = 1 WHERE username = :u",
            {"u": login_name})
    except Exception as ex:
        log.error(f"[ADMIN] ban DB error: {ex}")
        return False

    conn = _find_conn(server, login_name)
    if conn is not None:
        _send_system_msg(conn, f"You have been banned: {reason}")
        conn.is_connected = False
        try:
            conn.writer.close()
        except Exception:
            pass

    log_ban(login_name, "ban", reason, admin_name)
    log_activity("ban", login_name, reason, admin_name)
    log.info(f"[ADMIN] {admin_name} banned {login_name}: {reason}")
    return True


def unban_player(server: "GameServer", login_name: str, admin_name: str = "") -> bool:
    """Unban an account. Returns True."""
    try:
        db.execute_non_query(
            "UPDATE accounts SET is_banned = 0 WHERE username = :u",
            {"u": login_name})
    except Exception as ex:
        log.error(f"[ADMIN] unban DB error: {ex}")
        return False

    log_ban(login_name, "unban", "", admin_name)
    log_activity("unban", login_name, "", admin_name)
    log.info(f"[ADMIN] {admin_name} unbanned {login_name}")
    return True


def teleport_player(server: "GameServer", login_name: str, zone_name: str,
                    admin_name: str = "") -> bool:
    """Change a player's zone. The client will rejoin on next zone transition."""
    conn = _find_conn(server, login_name)
    if conn is None:
        return False

    zone = zone_registry.find_by_name(zone_name)
    if zone is None:
        return False

    conn.current_zone_id = zone.id
    conn.current_zone_name = zone.name
    from ..net.game_server import GameServer
    conn.current_zone_gc_type = GameServer._zone_gc_type(zone.name)
    _send_system_msg(conn, f"Admin teleported you to {zone.name}. Rejoin or warp to apply.")

    log_activity("teleport", login_name, f"to {zone_name}", admin_name)
    log.info(f"[ADMIN] {admin_name} teleported {login_name} to {zone_name}")
    return True


def send_admin_tell(server: "GameServer", login_name: str, message: str) -> bool:
    """Send a private system message to a player (appears in chat)."""
    conn = _find_conn(server, login_name)
    if conn is None:
        return False
    _send_system_msg(conn, message)
    return True


def _send_system_msg(conn: "RRConnection", text: str) -> None:
    w = LEWriter()
    w.write_byte(0x06)     # Chat channel (was 3 — client ignores chat on ch3)
    w.write_byte(0x00)
    w.write_byte(0x06)     # Noob channel
    w.write_byte(0x02)
    w.write_cstring("ADMIN")
    w.write_cstring(text)
    conn.send_to_client(w.to_array())


# ── Audit logging ──


def _ensure_audit_tables() -> None:
    try:
        for sql in [
            """CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                event_type TEXT NOT NULL,
                player TEXT,
                details TEXT,
                admin TEXT)""",
            """CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                sender TEXT NOT NULL,
                channel TEXT DEFAULT 'say',
                message TEXT NOT NULL,
                zone TEXT)""",
            """CREATE TABLE IF NOT EXISTS ban_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                admin TEXT)""",
            """CREATE TABLE IF NOT EXISTS pending_item_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                gc_class TEXT NOT NULL,
                count INTEGER DEFAULT 1,
                width INTEGER DEFAULT 1,
                height INTEGER DEFAULT 1,
                rarity INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')))""",
        ]:
            db.execute_non_query(sql)
    except Exception as ex:
        log.error(f"[ADMIN] audit table creation: {ex}")


def log_chat(sender: str, message: str, channel: str = "say", zone: str = "") -> None:
    try:
        db.execute_non_query(
            "INSERT INTO chat_log (sender, channel, message, zone) VALUES (:s, :c, :m, :z)",
            {"s": sender, "c": channel, "m": message, "z": zone})
    except Exception:
        pass


def log_activity(event_type: str, player: str = "", details: str = "",
                 admin: str = "") -> None:
    if not admin:
        admin = "admin"
    try:
        db.execute_non_query(
            "INSERT INTO activity_log (event_type, player, details, admin) "
            "VALUES (:e, :p, :d, :a)",
            {"e": event_type, "p": player, "d": details, "a": admin})
    except Exception:
        pass


def log_ban(username: str, action: str, reason: str = "", admin: str = "") -> None:
    try:
        db.execute_non_query(
            "INSERT INTO ban_log (username, action, reason, admin) "
            "VALUES (:u, :a, :r, :ad)",
            {"u": username, "a": action, "r": reason, "ad": admin})
    except Exception:
        pass
