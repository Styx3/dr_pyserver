"""Account persistence.

Ported from C# AccountRepository. Passwords are SHA-256(salt + password), base64,
with a per-account 16-byte base64 salt. On first login, accounts are auto-created.
On subsequent logins, the password is always verified against the stored hash.
"""
from __future__ import annotations

import base64
import hashlib
import os

from ..core import log
from . import game_database as db


def _generate_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.sha256((salt + password).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def get_account_id(username: str) -> int:
    try:
        result = db.execute_scalar(
            "SELECT id FROM accounts WHERE username = :u", {"u": username}
        )
        return int(result) if result is not None else 0
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-AUTH] get_account_id error: {ex}")
        return 0


def verify_password(username: str, password: str) -> bool:
    """Check the password against the stored hash. Returns True if valid or account doesn't exist."""
    try:
        row = db.execute_reader(
            "SELECT password_hash, salt FROM accounts WHERE username = :u",
            {"u": username},
        ).fetchone()
        if row is None:
            return False  # no account — caller should create
        stored_hash = row["password_hash"]
        salt = row["salt"]
        expected_hash = _hash_password(password, salt)
        return stored_hash == expected_hash
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-AUTH] verify_password error: {ex}")
        return False


def create_account(username: str, password: str) -> int:
    if not username or not password:
        log.error("[DB-AUTH] rejected: empty username or password")
        return 0
    salt = _generate_salt()
    pw_hash = _hash_password(password, salt)
    try:
        db.execute_non_query(
            "INSERT INTO accounts (username, password_hash, salt) VALUES (:u, :h, :s)",
            {"u": username, "h": pw_hash, "s": salt},
        )
        account_id = int(db.execute_scalar("SELECT last_insert_rowid()") or 0)
        log.info(f"[DB-AUTH] created account '{username}' (id {account_id})")
        return account_id
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-AUTH] create_account failed for '{username}': {ex}")
        return 0


def is_banned(username: str) -> bool:
    try:
        result = db.execute_scalar(
            "SELECT is_banned FROM accounts WHERE username = :u", {"u": username}
        )
        return bool(result) if result is not None else False
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-AUTH] ban check error: {ex}")
        return False
