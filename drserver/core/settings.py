"""Dynamic runtime settings (gameplay tunables).

Ported from C# ServerSettings. Priority: DB override > server.cfg > hardcoded
default. server.cfg is plain ``key = value`` text (``#`` comments). The DB layer
(SQLite ``server_settings`` table, live ``@set`` command) is optional and wired up
once the database module registers a connection provider via ``bind_database``.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from . import log

_cfg_values: dict[str, str] = {}
_db_values: dict[str, str] = {}
_loaded = False
_cfg_path: Optional[str] = None

# Database hook — set by db layer in Phase 3. Returns dict[str,str] of overrides.
_db_load: Optional[Callable[[], dict]] = None
_db_save: Optional[Callable[[str, str], None]] = None
_db_remove: Optional[Callable[[str], None]] = None


def bind_database(load_fn, save_fn, remove_fn) -> None:
    """Register SQLite-backed override hooks (called by the db layer)."""
    global _db_load, _db_save, _db_remove
    _db_load, _db_save, _db_remove = load_fn, save_fn, remove_fn


def _find_cfg_path() -> Optional[str]:
    for candidate in (
        os.path.join(os.getcwd(), "server.cfg"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "server.cfg"),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _parse_cfg_line(line: str) -> Optional[tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    eq = line.find("=")
    if eq <= 0:
        return None
    key = line[:eq].strip()
    value = line[eq + 1 :].strip()
    # Strip inline comments, but only when '#' follows whitespace so hex colors
    # like #FFCC66 survive.
    for ci in range(1, len(value)):
        if value[ci] == "#" and value[ci - 1].isspace():
            value = value[:ci].strip()
            break
    return (key, value) if key else None


def _load_cfg_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                parsed = _parse_cfg_line(raw)
                if parsed:
                    _cfg_values[parsed[0]] = parsed[1]
    except OSError as ex:
        log.error(f"[CONFIG] Error reading {path}: {ex}")


def load() -> None:
    global _loaded, _cfg_path
    _loaded = True  # set first to prevent recursion via getters
    _cfg_values.clear()
    _db_values.clear()
    _cfg_path = _find_cfg_path()
    if _cfg_path:
        _load_cfg_file(_cfg_path)
        log.info(f"[CONFIG] Loaded {len(_cfg_values)} settings from {_cfg_path}")
    else:
        log.info("[CONFIG] server.cfg not found — using defaults")
    if _db_load:
        try:
            _db_values.update(_db_load() or {})
        except Exception as ex:  # noqa: BLE001 - DB load is non-fatal
            log.error(f"[CONFIG] DB load error (non-fatal): {ex}")


def reload() -> None:
    _cfg_values.clear()
    if _cfg_path:
        _load_cfg_file(_cfg_path)
    if _db_load:
        try:
            _db_values.clear()
            _db_values.update(_db_load() or {})
        except Exception as ex:  # noqa: BLE001
            log.error(f"[CONFIG] DB reload error: {ex}")


def _ensure_loaded() -> None:
    if not _loaded:
        load()


def get_string(key: str, default: str = "") -> str:
    _ensure_loaded()
    if key in _db_values:
        return _db_values[key]
    if key in _cfg_values:
        return _cfg_values[key]
    return default


def get_int(key: str, default: int) -> int:
    val = get_string(key, None)  # type: ignore[arg-type]
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return default


def get_float(key: str, default: float) -> float:
    val = get_string(key, None)  # type: ignore[arg-type]
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return default


def get_bool(key: str, default: bool) -> bool:
    val = get_string(key, None)  # type: ignore[arg-type]
    if val is None:
        return default
    val = val.lower().strip()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def set_value(key: str, value: str) -> None:
    _db_values[key] = value
    if _db_save:
        _db_save(key, value)


def remove(key: str) -> None:
    _db_values.pop(key, None)
    if _db_remove:
        _db_remove(key)


def get_all() -> dict[str, tuple[str, str]]:
    _ensure_loaded()
    result: dict[str, tuple[str, str]] = {}
    for k, v in _cfg_values.items():
        result[k] = (v, "cfg")
    for k, v in _db_values.items():
        result[k] = (v, "db")
    return result
