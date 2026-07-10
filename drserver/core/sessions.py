"""Session + queue handoff state.

Ports GlobalSessions, SessionManager, and QueueConnectionBridge (capacity + IP
tracking) from the C# server. The C# code used locks because socket reads ran on
background threads; the Python server runs everything on one asyncio loop, so no
locking is needed. The social-stream parts of QueueConnectionBridge are deferred
to Phase 6 (social channel).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

from . import log


# ─────────────────────────── GlobalSessions ───────────────────────────
class _GlobalSessions:
    def __init__(self):
        self._sessions: dict[int, str] = {}

    def store(self, token: int, username: str) -> None:
        self._sessions[token] = username
        log.debug(f"[GlobalSessions] stored token 0x{token:08X} for '{username}'")

    def try_consume(self, token: int) -> Optional[str]:
        username = self._sessions.pop(token, None)
        if username is None:
            log.warn(f"[GlobalSessions] invalid/expired token 0x{token:08X}")
        return username

    def exists(self, token: int) -> bool:
        return token in self._sessions

    def clear(self) -> None:
        self._sessions.clear()

    @property
    def count(self) -> int:
        return len(self._sessions)


global_sessions = _GlobalSessions()


# ─────────────────────────── SessionManager ───────────────────────────
@dataclass
class UserSession:
    account_id: int
    username: str
    password: str
    is_authenticated: bool = True
    selected_character_id: int = 0
    created_time: datetime.datetime = field(default_factory=datetime.datetime.now)
    last_login_time: datetime.datetime = field(default_factory=datetime.datetime.now)


class _SessionManager:
    def __init__(self):
        self._sessions: dict[int, UserSession] = {}
        self._username_to_account: dict[str, int] = {}
        self._play_tokens: dict[int, str] = {}
        self._next_account_id = 1

    def create_session(self, username: str, password: str) -> Optional[UserSession]:
        from ..db import account_repository as accounts

        account_id = accounts.get_account_id(username)
        if account_id != 0:
            # Existing account — verify password against DB.
            if not accounts.verify_password(username, password):
                log.warn(f"[SessionManager] invalid password for '{username}'")
                return None
        else:
            # New account — create in DB.
            account_id = accounts.create_account(username, password)
            if account_id == 0:
                log.error(f"[SessionManager] failed to create account for '{username}'")
                return None

        session = UserSession(account_id=account_id, username=username, password=password)
        self._sessions[account_id] = session
        self._username_to_account[username] = account_id
        log.debug(f"[SessionManager] session created for '{username}' (id {account_id})")
        return session

    def get_session(self, account_id: int) -> Optional[UserSession]:
        return self._sessions.get(account_id)

    def get_session_by_username(self, username: str) -> Optional[UserSession]:
        aid = self._username_to_account.get(username)
        return self._sessions.get(aid) if aid is not None else None

    def set_selected_character(self, account_id: int, character_id: int) -> None:
        s = self._sessions.get(account_id)
        if s is not None:
            s.selected_character_id = character_id

    def set_play_token(self, play_token: int, username: str) -> None:
        self._play_tokens[play_token] = username
        log.debug(f"[SessionManager] set play token 0x{play_token:08X} for '{username}'")

    def validate_play_token(self, play_token: int) -> Optional[str]:
        return self._play_tokens.get(play_token)

    def remove_play_token(self, play_token: int) -> None:
        self._play_tokens.pop(play_token, None)

    def get_account_id(self, username: str) -> int:
        return self._username_to_account.get(username, 0)


session_manager = _SessionManager()


# ─────────────────────── QueueConnectionBridge ───────────────────────
class _QueueBridge:
    def __init__(self):
        self.max_players = 1
        self._current_players = 0
        self._pending_ips: dict[str, str] = {}

    @property
    def current_players(self) -> int:
        return self._current_players

    @property
    def has_capacity(self) -> bool:
        return self._current_players < self.max_players

    def player_connected(self) -> None:
        self._current_players += 1
        log.debug(f"[QUEUE-BRIDGE] connected; now {self._current_players}/{self.max_players}")

    def player_disconnected(self) -> None:
        if self._current_players > 0:
            self._current_players -= 1
        log.debug(f"[QUEUE-BRIDGE] disconnected; now {self._current_players}/{self.max_players}")

    def expect_queue_from_ip(self, ip: str, username: str) -> None:
        self._pending_ips[ip] = username
        log.debug(f"[QUEUE-BRIDGE] expecting queue from {ip} for {username}")

    def check_and_consume_queue_ip(self, ip: str) -> Optional[str]:
        return self._pending_ips.pop(ip, None)


queue_bridge = _QueueBridge()
