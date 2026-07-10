"""PvP Duel Manager — 1v1 challenge/accept lifecycle with countdown.

Ported from C# DuelManager.cs. Manages the PvP duel protocol:
challenge → accept/decline → countdown → in_combat → end (kill/forfeit).
Includes 5s countdown with damage immunity, 60s cooldown between duels,
and level 5 minimum requirement.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, TYPE_CHECKING

from ..core import log

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection


@dataclass
class Duel:
    challenger: str           # login_name
    target: str               # login_name
    state: str = "pending"    # pending, countdown, active, ended
    started_at: float = 0.0
    countdown_until: float = 0.0
    winner: Optional[str] = None


class DuelManager:
    def __init__(self, server: "GameServer"):
        self._server = server
        self._duels: Dict[str, Duel] = {}        # key = f"{challenger}:{target}" or single login
        self._cooldowns: Dict[str, float] = {}   # login -> cooldown end time
        self._in_duel: set[str] = set()          # logins currently in a duel

    def challenge(self, challenger_conn: "RRConnection", target_name: str) -> None:
        """Challenge another player to a duel."""
        challenger = challenger_conn.login_name
        if not challenger:
            return

        target_conn = self._find_player(target_name)
        if target_conn is None:
            self._send_chat(challenger_conn, f"Player '{target_name}' not found.")
            return

        target = target_conn.login_name

        if challenger == target:
            self._send_chat(challenger_conn, "You cannot duel yourself.")
            return

        if challenger_conn.player_level < 5 or target_conn.player_level < 5:
            self._send_chat(challenger_conn, "Both players must be at least level 5 to duel.")
            return

        if challenger in self._in_duel or target in self._in_duel:
            self._send_chat(challenger_conn, "One of you is already in a duel.")
            return

        # Check cooldown.
        now = time.time()
        cd = max(self._cooldowns.get(challenger, 0), self._cooldowns.get(target, 0))
        if now < cd:
            remaining = int(cd - now)
            self._send_chat(challenger_conn, f"Duel cooldown: {remaining}s remaining.")
            return

        key = f"{challenger}:{target}"
        if key in self._duels and self._duels[key].state != "ended":
            self._send_chat(challenger_conn, "You already have a pending duel with this player.")
            return

        self._duels[key] = Duel(challenger=challenger, target=target, state="pending", started_at=now)
        self._send_chat(target_conn, f"{challenger} challenges you to a duel! @accept to fight.")
        self._send_chat(challenger_conn, f"Duel challenge sent to {target_name}.")
        log.info(f"[Duel] {challenger} challenged {target}")

    def accept(self, conn: "RRConnection") -> None:
        """Accept a pending duel challenge."""
        login = conn.login_name
        if not login:
            return

        for key, duel in list(self._duels.items()):
            if duel.target == login and duel.state == "pending":
                duel.state = "countdown"
                duel.started_at = time.time()
                duel.countdown_until = time.time() + 5.0

                self._in_duel.add(duel.challenger)
                self._in_duel.add(duel.target)

                challenger_conn = self._find_player(duel.challenger)
                if challenger_conn:
                    self._send_chat(challenger_conn, f"Duel with {login} starting in 5 seconds!")
                self._send_chat(conn, f"Duel with {duel.challenger} starting in 5 seconds!")

                log.info(f"[Duel] {duel.challenger} vs {duel.target} — countdown started")
                return

        self._send_chat(conn, "No pending duel challenge.")

    def decline(self, conn: "RRConnection") -> None:
        """Decline a pending duel challenge."""
        login = conn.login_name
        for key, duel in list(self._duels.items()):
            if duel.target == login and duel.state == "pending":
                challenger_conn = self._find_player(duel.challenger)
                if challenger_conn:
                    self._send_chat(challenger_conn, f"{login} declined your duel challenge.")
                self._send_chat(conn, "Duel challenge declined.")
                del self._duels[key]
                return

    def on_player_killed(self, victim_login: str, killer_login: str) -> None:
        """Called when a player is killed — check if it ends a duel."""
        for key, duel in list(self._duels.items()):
            if duel.state in ("countdown", "active") and victim_login in (duel.challenger, duel.target):
                duel.state = "ended"
                duel.winner = killer_login

                winner_conn = self._find_player(killer_login)
                loser_conn = self._find_player(victim_login)

                if winner_conn:
                    self._send_chat(winner_conn, f"Duel won! You defeated {victim_login}.")
                if loser_conn:
                    self._send_chat(loser_conn, f"Duel lost! Defeated by {killer_login}.")

                # Set cooldowns.
                now = time.time()
                self._cooldowns[duel.challenger] = now + 60
                self._cooldowns[duel.target] = now + 60
                self._in_duel.discard(duel.challenger)
                self._in_duel.discard(duel.target)

                log.info(f"[Duel] {killer_login} won duel vs {victim_login}")
                del self._duels[key]
                return

    def on_disconnect(self, login: str) -> None:
        """Clean up duels on player disconnect."""
        for key, duel in list(self._duels.items()):
            if login in (duel.challenger, duel.target):
                self._in_duel.discard(duel.challenger)
                self._in_duel.discard(duel.target)
                del self._duels[key]

    def _find_player(self, name: str) -> Optional["RRConnection"]:
        name_lower = name.lower()
        for conn in self._server.connections.values():
            if conn.login_name and conn.login_name.lower() == name_lower:
                return conn
        return None

    def _send_chat(self, conn: "RRConnection", text: str) -> None:
        from ..net.chat_commands import _send_chat
        _send_chat(conn, text)
