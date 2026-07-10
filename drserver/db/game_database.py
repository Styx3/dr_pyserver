"""SQLite access layer.

Ported from C# GameDatabase. Opens the shipped dungeon_runners.db (50+ tables:
accounts, characters, equipment, inventory, skills, quests, items, zones,
pathmap_nodes, merchants, ...). The C# opened a connection per operation; here we
keep one shared connection on the single asyncio loop. Query helpers accept
``:name`` placeholders to mirror the C# ``@name`` parameter style.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Optional

from ..core import log, settings

_db_path: Optional[str] = None
_conn: Optional[sqlite3.Connection] = None

# Server-owned persistence schema, ported from C# GameDatabase.CreatePlayerTables().
# These are bootstrapped on every startup (CREATE IF NOT EXISTS is a no-op when the
# table already exists), so character creation/persistence works against a fresh DB
# rather than depending on a pre-shipped one. Read-only *content* tables (creatures,
# zones, items, ...) are NOT created here — they come from the shipped content DB and
# the server degrades gracefully when they are absent.
#
# Columns match what db/character_repository.py reads and writes. Note `buy_price`:
# the C# CharacterRepository read/wrote character_inventory.buy_price but C#'s DDL
# never created it — C# only worked because its shipped DB already had the column.
# A fresh DB must declare it explicitly or save/load of inventory fails.
_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL, salt TEXT NOT NULL, email TEXT DEFAULT '',
        is_member INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')), last_login TEXT DEFAULT (datetime('now')),
        current_character_id INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS server_settings (
        key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS characters (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE, class_name TEXT NOT NULL DEFAULT 'Fighter',
        avatar_class TEXT DEFAULT '', level INTEGER DEFAULT 1, experience INTEGER DEFAULT 0,
        gold INTEGER DEFAULT 100, skin INTEGER DEFAULT 0, face INTEGER DEFAULT 0,
        face_feature INTEGER DEFAULT 0, hair INTEGER DEFAULT 0, hair_color INTEGER DEFAULT 0,
        current_zone TEXT DEFAULT 'tutorial', zone_id INTEGER DEFAULT 0,
        position_x REAL DEFAULT 0, position_y REAL DEFAULT 0, position_z REAL DEFAULT 0,
        current_hp INTEGER DEFAULT 0, current_mana INTEGER DEFAULT 0,
        max_hp INTEGER DEFAULT 0, max_mana INTEGER DEFAULT 0,
        stat_strength INTEGER DEFAULT 0, stat_agility INTEGER DEFAULT 0,
        stat_intellect INTEGER DEFAULT 0, stat_endurance INTEGER DEFAULT 0,
        last_respec_time INTEGER DEFAULT 0, respec_count INTEGER DEFAULT 0,
        pvp_wins INTEGER DEFAULT 0, pvp_rating INTEGER DEFAULT 0,
        tp_zone TEXT DEFAULT '', tp_zone_id INTEGER DEFAULT 0, tp_target_zone TEXT DEFAULT '',
        tp_pos_x REAL DEFAULT 0, tp_pos_y REAL DEFAULT 0, tp_pos_z REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (account_id) REFERENCES accounts(id))""",
    """CREATE TABLE IF NOT EXISTS character_equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        slot TEXT NOT NULL, gc_class TEXT NOT NULL DEFAULT '',
        rarity INTEGER DEFAULT -1, stored_level INTEGER DEFAULT -1,
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
        UNIQUE(character_id, slot))""",
    """CREATE TABLE IF NOT EXISTS character_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        gc_class TEXT NOT NULL, slot_x INTEGER DEFAULT 0, slot_y INTEGER DEFAULT 0,
        count INTEGER DEFAULT 1, buy_price INTEGER DEFAULT 0,
        rarity INTEGER DEFAULT -1, stored_level INTEGER DEFAULT -1,
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS character_skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        skill_gc_class TEXT NOT NULL, level INTEGER DEFAULT 1, hotbar_slot INTEGER DEFAULT -1,
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
        UNIQUE(character_id, skill_gc_class))""",
    """CREATE TABLE IF NOT EXISTS character_quests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        quest_id TEXT NOT NULL, quest_giver_id TEXT DEFAULT '',
        accepted_at TEXT DEFAULT (datetime('now')), status TEXT DEFAULT 'active',
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
        UNIQUE(character_id, quest_id))""",
    """CREATE TABLE IF NOT EXISTS quest_objectives (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        quest_id TEXT NOT NULL, objective_name TEXT NOT NULL,
        type TEXT DEFAULT '', target TEXT DEFAULT '', label TEXT DEFAULT '',
        required INTEGER DEFAULT 0, current INTEGER DEFAULT 0,
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS completed_quests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        quest_id TEXT NOT NULL, completed_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
        UNIQUE(character_id, quest_id))""",
    """CREATE TABLE IF NOT EXISTS character_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        checkpoint_id TEXT NOT NULL,
        FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
        UNIQUE(character_id, checkpoint_id))""",
)

# Best-effort column upgrades for an older/real C# DB that predates a column.
# No-ops on a freshly created full-schema DB (the column already exists → caught).
_SCHEMA_MIGRATIONS = (
    "ALTER TABLE character_inventory ADD COLUMN buy_price INTEGER DEFAULT 0",
    "ALTER TABLE character_inventory ADD COLUMN rarity INTEGER DEFAULT -1",
    "ALTER TABLE character_inventory ADD COLUMN stored_level INTEGER DEFAULT -1",
    "ALTER TABLE character_equipment ADD COLUMN rarity INTEGER DEFAULT -1",
    "ALTER TABLE character_equipment ADD COLUMN stored_level INTEGER DEFAULT -1",
    "ALTER TABLE character_inventory ADD COLUMN scale_mod TEXT DEFAULT ''",
    "ALTER TABLE character_equipment ADD COLUMN scale_mod TEXT DEFAULT ''",
    # Comma-joined items.modpal.* attribute mods (Intellect etc.) rolled at
    # acquire, so a bought/looted item keeps its mods through bag + relog.
    "ALTER TABLE character_inventory ADD COLUMN mod_refs TEXT DEFAULT ''",
    "ALTER TABLE character_equipment ADD COLUMN mod_refs TEXT DEFAULT ''",
    "ALTER TABLE characters ADD COLUMN max_hp INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN max_mana INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN stat_strength INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN stat_agility INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN stat_intellect INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN stat_endurance INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN last_respec_time INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN respec_count INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN pvp_wins INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN pvp_rating INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN tp_zone TEXT DEFAULT ''",
    "ALTER TABLE characters ADD COLUMN tp_zone_id INTEGER DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN tp_target_zone TEXT DEFAULT ''",
    "ALTER TABLE characters ADD COLUMN tp_pos_x REAL DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN tp_pos_y REAL DEFAULT 0",
    "ALTER TABLE characters ADD COLUMN tp_pos_z REAL DEFAULT 0",
    "ALTER TABLE accounts ADD COLUMN current_character_id INTEGER DEFAULT 0",
)


def _create_schema(conn: sqlite3.Connection) -> None:
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl)
    for alter in _SCHEMA_MIGRATIONS:
        try:
            conn.execute(alter)
        except sqlite3.OperationalError:
            pass  # column already exists — expected on a full-schema DB
    # Nobody is online at startup; clear stale session pointers (matches C#).
    try:
        conn.execute("UPDATE accounts SET current_character_id = 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def initialize(path: str) -> None:
    global _db_path, _conn
    _db_path = path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _create_schema(_conn)
    log.info(f"[DB] opened {path}")
    # Wire the dynamic-settings overlay now that the DB is available.
    settings.bind_database(_settings_load, _settings_save, _settings_remove)


def get_db_path() -> Optional[str]:
    return _db_path


def get_connection() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("GameDatabase not initialized")
    return _conn


def execute_non_query(sql: str, params: Optional[dict] = None) -> int:
    cur = get_connection().execute(sql, params or {})
    get_connection().commit()
    return cur.rowcount


def execute_scalar(sql: str, params: Optional[dict] = None) -> Any:
    row = get_connection().execute(sql, params or {}).fetchone()
    return row[0] if row is not None else None


def execute_reader(sql: str, params: Optional[dict] = None) -> sqlite3.Cursor:
    return get_connection().execute(sql, params or {})


# ── typed row accessors (mirror C# GameDatabase.GetInt/GetString/GetFloat) ──
def _row_value(row: sqlite3.Row, col: str):
    try:
        if col in row.keys():
            return row[col]
    except (IndexError, KeyError):
        pass
    return None


def get_int(row: sqlite3.Row, col: str, default: int = 0) -> int:
    v = _row_value(row, col)
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def get_string(row: sqlite3.Row, col: str, default: str = "") -> str:
    v = _row_value(row, col)
    return str(v) if v is not None else default


def get_float(row: sqlite3.Row, col: str, default: float = 0.0) -> float:
    v = _row_value(row, col)
    return float(v) if v is not None else default


def get_bool(row: sqlite3.Row, col: str, default: bool = False) -> bool:
    v = _row_value(row, col)
    return bool(v) if v is not None else default


# ── server_settings hooks for core.settings ──
def _settings_load() -> dict:
    cur = execute_reader("SELECT key, value FROM server_settings")
    return {row["key"]: row["value"] for row in cur}


def _settings_save(key: str, value: str) -> None:
    execute_non_query(
        "INSERT OR REPLACE INTO server_settings (key, value, updated_at) "
        "VALUES (:k, :v, datetime('now'))",
        {"k": key, "v": value},
    )


def _settings_remove(key: str) -> None:
    execute_non_query("DELETE FROM server_settings WHERE key = :k", {"k": key})
