"""Group manager — party lifecycle + the C# channel-9 group wire protocol.

Port of DR-Server ``GroupDirectory.cs`` (state) + ``GroupPackets.cs`` (bytes) +
``HandleGroupClientChannel`` (choreography). Inbound requests arrive on channel
0x0B (``game_server._handle_group_channel``); every server→client packet here
rides **channel 9** — the leading payload byte handed to ``conn.send_to_client``
(CompressedA dest=0x01 type=0x0F, the C# ``SendToClient`` wrapper).

Members are keyed by **login name** (stable across reconnects); the wire keys
them by **charSqlId** (u32). A group survives a member disconnect (offline but
still rostered, C# ``DisconnectMember``); a 1-member group dissolves to solo.

Instancing: each group owns one ``instance_token`` (minted lazily, re-minted by
resetInstances 0x26); ``GameServer._assign_instance_id`` keys every grouped
member entering a dungeon to ``(zone_id, token)`` — ONE shared world copy —
while solo players mint fresh private tokens (bible §7).

Client grounding (Ghidra 2026-07-09, T0): the ch-9 receiver is the GroupClient
GC service — queue-drain pump ``FUN_005f7dd0`` → opcode dispatcher
``FUN_005f7e20`` with cases 0x30–0x56 exactly matching this opcode space.
Verified bodies: 0x30 reads u32→GC+0xB0 (self key) + u8→+0xB5 (difficulty) +
u8→+0xB6 (invite mode) and RESETS the roster; 0x35 reads u32 groupId→+0xD0,
u32 leader→+0xD4, u8 flag→+0xB7, u8 open→+0xD8.0, self-block, u8 count,
count×member; 0x50 reads u32 userId ×2, u8 memberFlag, u32 talkGroupId,
4-byte IPv4, u32 port and hands them to the Talkback voice-client connect.
Messages queue until the GroupClient service pumps — they cannot corrupt
other channels. Remaining [T3] bytes: 0x43/0x44/0x45/0x49/0x4A/0x4B/0x4C/0x55
(C#-shaped, dispatcher-confirmed reachable, bodies not yet read).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core import log
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection

MAX_GROUP_SIZE = 5
_HEALTH_PUSH_INTERVAL = 1.0     # seconds between 0x4B fan-outs per member
_DEFAULT_INVITE_MODE = 3        # C# Group.InviteMode initial value
TALKBACK_PORT = 2604            # C# TalkbackServer.Port — see build_join_talkback_group

# Send group state (0x30/0x35 prime + roster/zone/health refresh) from the
# zone-join tail, like C#'s zone-transition block. The 2026-07-08 loading-screen
# stall was observed WITHOUT the talkback 0x50 handoff C# always appends; the
# 2026-07-09 recheck of the C# flow found the prime placement and bytes were
# already identical, leaving the missing 0x50 as the only wire delta — it is
# now sent (see _send_talkback_join). DR_GROUP_PRIME=0 re-disables the
# spawn-tail priming for A/B isolation; event-driven group sends
# (invite/accept/leave/kick, in-game) are NOT gated.
GROUP_PRIME_ENABLED = os.environ.get("DR_GROUP_PRIME", "1") == "1"


# ── Wire builders (GroupPackets.cs, byte-for-byte) ──────────────────────────

def build_process_connected(self_char_id: int, difficulty: int,
                            invite_mode: int) -> bytes:
    """0x30 processConnected — primes the client's group subsystem."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x30)
    w.write_uint32(self_char_id)
    w.write_byte(difficulty & 0xFF)
    w.write_byte(invite_mode & 0xFF)
    return w.to_array()


def write_member(w: LEWriter, char_sql_id: int, name: str,
                 avatar_entity_id: int, is_online: bool) -> None:
    w.write_uint32(char_sql_id)
    w.write_cstring(name)
    w.write_uint32(avatar_entity_id)
    w.write_byte(0x01 if is_online else 0x00)


def build_user_changed_group(group_id: int, leader_char_id: int, flag: int,
                             is_open: int,
                             members: List[tuple[int, str, int, bool]]) -> bytes:
    """0x35 processUserChangedGroup — the full roster snapshot.

    ``members`` is ``[(char_sql_id, name, avatar_entity_id, is_online), …]``,
    leader first (C# member-list build order). A 0-member list with
    ``group_id=1`` is the SOLO state the client uses to clear the party frame.
    """
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x35)
    w.write_uint32(group_id)
    w.write_uint32(leader_char_id)
    w.write_byte(flag & 0xFF)
    w.write_byte(is_open & 0xFF)
    w.write_uint32(0)             # selfEntityId1 (C# always sends 0)
    w.write_uint32(0)             # selfEntityId2 (C# always sends 0)
    w.write_byte(0x00)
    w.write_byte(0x00)
    w.write_byte(len(members) & 0xFF)
    for char_id, name, avatar_id, online in members:
        write_member(w, char_id, name, avatar_id, online)
    return w.to_array()


def build_solo_state(self_char_id: int) -> bytes:
    """The empty-roster 0x35 that returns a client to the solo party frame.

    C# sends ``groupId=1`` with 0 members here (its two call sites disagree on
    the leader field — zone id vs charSqlId; the roster is empty either way, so
    we use the charSqlId variant from the zone-transition path).
    """
    return build_user_changed_group(1, self_char_id, 0xFF, 0, [])


def build_process_invitation(invite_id: int, group_id: int, inviter_name: str,
                             flags: int = 0x00) -> bytes:
    """0x32 processInvitation — pops the join-group dialog on the invitee."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x32)
    w.write_uint32(invite_id)
    w.write_uint32(group_id)
    w.write_cstring(inviter_name)
    w.write_byte(flags & 0xFF)
    return w.to_array()


def build_remove_user(group_id: int, char_id: int) -> bytes:
    """0x43 processRemoveUser — drops one roster entry on the receiving UI."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x43)
    w.write_uint32(group_id)
    w.write_uint32(char_id)
    return w.to_array()


def build_set_leader(group_id: int, new_leader_char_id: int) -> bytes:
    """0x44 processSetLeader."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x44)
    w.write_uint32(group_id)
    w.write_uint32(new_leader_char_id)
    w.write_byte(0xFF)
    return w.to_array()


def build_changed_invite_mode(mode: int) -> bytes:
    """0x45 inviteModeChanged (ack to the requester)."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x45)
    w.write_byte(mode & 0xFF)
    return w.to_array()


def build_member_health_mana(char_id: int, hp15: int, mp15: int) -> bytes:
    """0x4B memberHealthMana — party-frame HP/MP as packed 0..15 fractions."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x4B)
    w.write_uint32(char_id)
    w.write_byte(((hp15 & 0x0F) << 4) | (mp15 & 0x0F))
    return w.to_array()


def build_user_changed_zone(char_id: int, zone_name: str) -> bytes:
    """0x4C userChangedZone — the roster's per-member zone label."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x4C)
    w.write_uint32(char_id)
    w.write_cstring(zone_name)
    return w.to_array()


def build_member_disconnected(group_id: int, char_id: int) -> bytes:
    """0x49 memberDisconnected (greys the roster entry)."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x49)
    w.write_uint32(group_id)
    w.write_uint32(char_id)
    return w.to_array()


def build_member_reconnected(group_id: int, char_id: int) -> bytes:
    """0x4A memberReconnected."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x4A)
    w.write_uint32(group_id)
    w.write_uint32(char_id)
    return w.to_array()


def build_monster_difficulty(difficulty: int, personal_only: bool) -> bytes:
    """0x55 monsterDifficulty."""
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x55)
    w.write_byte(difficulty & 0xFF)
    w.write_byte(0x01 if personal_only else 0x00)
    return w.to_array()


def build_join_talkback_group(char_sql_id: int, is_member: bool,
                              talk_group_id: int, ip4: bytes,
                              port: int = TALKBACK_PORT) -> bytes:
    """0x50 joinTalkbackGroup — the voice-chat handoff (C# SendJoinTalkbackGroup).

    Client reader FUN_005fa5b0 (T0): u32 userId, u32 userId again, u8
    memberFlag (0x00 = free account, 0x01 = paying member), u32 talkGroupId,
    4 raw IPv4 bytes, u32 port. C# appends this after EVERY group prime /
    roster send and after leave — and never starts its TalkbackServer
    listener, so the client's voice connect to ``ip4:port`` is refused and
    tolerated. The packet itself is the group-subsystem handoff; keep sending
    it even though nothing serves voice.
    """
    w = LEWriter()
    w.write_byte(0x09)
    w.write_byte(0x50)
    w.write_uint32(char_sql_id)
    w.write_uint32(char_sql_id)
    w.write_byte(0x01 if is_member else 0x00)
    w.write_uint32(talk_group_id)
    w.write_bytes(ip4[:4])
    w.write_uint32(port)
    return w.to_array()


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class GroupMember:
    login: str
    char_name: str = ""
    char_sql_id: int = 0
    is_online: bool = True
    zone_name: str = ""
    personal_difficulty: int = 0


@dataclass
class Group:
    """A party of up to :data:`MAX_GROUP_SIZE` players."""
    group_id: int
    leader_login: str
    members: List[GroupMember] = field(default_factory=list)
    monster_difficulty: int = 0
    invite_mode: int = _DEFAULT_INVITE_MODE
    is_open: bool = False
    # The group's live dungeon-copy key: every member entering a private zone
    # adopts ``(zone_id, instance_token)`` so the party fights ONE set of mobs.
    # 0 = not minted yet; resetInstances re-mints so the NEXT entry is fresh
    # (members already inside keep their current conn.instance_id — the token
    # must stay stable while a level is occupied, bible §7).
    instance_token: int = 0

    def member(self, login: str) -> Optional[GroupMember]:
        for m in self.members:
            if m.login == login:
                return m
        return None

    def member_by_char_id(self, char_id: int) -> Optional[GroupMember]:
        for m in self.members:
            if m.char_sql_id == char_id:
                return m
        return None


class GroupManager:
    """Global group state + the ch-9 send choreography (C# GroupDirectory)."""

    def __init__(self, server: "GameServer"):
        self._server = server
        self._groups: Dict[int, Group] = {}
        self._player_groups: Dict[str, int] = {}      # login -> group_id
        self._pending_invites: Dict[str, int] = {}    # invitee login -> group_id
        self._last_health_push: Dict[str, float] = {}  # login -> monotonic
        self._next_id = 1

    # ── Lookups ──────────────────────────────────────────────────────────────

    def get_group(self, conn: "RRConnection") -> Optional[Group]:
        return self._group_of(conn.login_name) if conn.login_name else None

    def is_in_group(self, login: str) -> bool:
        return login in self._player_groups

    def member_logins(self, conn: "RRConnection") -> List[str]:
        """Roster logins of ``conn``'s group ([] when solo) — chat fan-out."""
        group = self.get_group(conn)
        return [m.login for m in group.members] if group else []

    def group_id_for(self, conn: "RRConnection") -> int:
        """The wire group id (0 when solo) — the Player-object init carries it."""
        group = self.get_group(conn)
        return group.group_id if group else 0

    def _group_of(self, login: str) -> Optional[Group]:
        gid = self._player_groups.get(login)
        return self._groups.get(gid) if gid else None

    def _conn_for_login(self, login: str) -> Optional["RRConnection"]:
        for conn in self._server.connections.values():
            if conn.login_name == login:
                return conn
        return None

    def _find_player(self, name: str) -> Optional["RRConnection"]:
        """Resolve an invite target by character name or login (C#
        FindConnectionByName). Exact match first, then prefix."""
        name_lower = name.lower()
        prefix: Optional["RRConnection"] = None
        for conn in self._server.connections.values():
            if not conn.login_name:
                continue
            char = self._server.selected_character.get(conn.login_name)
            char_name = (getattr(char, "name", "") or "").lower()
            login = conn.login_name.lower()
            if name_lower in (login, char_name):
                return conn
            if prefix is None and (login.startswith(name_lower)
                                   or char_name.startswith(name_lower)):
                prefix = conn
        return prefix

    def _find_player_by_char_id(self, char_id: int) -> Optional["RRConnection"]:
        for conn in self._server.connections.values():
            # login gate: a pre-auth conn's conn_id+1 fallback id must never
            # shadow a real character id.
            if conn.login_name and self._char_sql_id(conn) == char_id:
                return conn
        return None

    # ── Wire identity helpers ────────────────────────────────────────────────

    def _char_sql_id(self, conn: "RRConnection") -> int:
        """The u32 the wire keys members by (C# GetCharSqlId fallback chain)."""
        char = self._server.selected_character.get(conn.login_name or "")
        char_id = int(getattr(char, "id", 0) or 0)
        if char_id:
            return char_id
        if getattr(conn, "char_sql_id", 0):
            return conn.char_sql_id
        return (getattr(conn, "conn_id", 0) or 0) + 1

    def _char_name(self, conn: "RRConnection") -> str:
        char = self._server.selected_character.get(conn.login_name or "")
        return getattr(char, "name", None) or conn.login_name or "Unknown"

    def _member_wire_entry(self, member: GroupMember) -> tuple[int, str, int, bool]:
        """(charSqlId, name, avatarEntityId, online) — live values for online
        members (and refresh the cached roster entry), cache for offline."""
        conn = self._conn_for_login(member.login) if member.is_online else None
        if conn is not None and getattr(conn, "is_spawned", False):
            member.char_sql_id = self._char_sql_id(conn)
            member.char_name = self._char_name(conn)
            avatar_id = self._server.get_player_avatar_id(member.login) or 0
            return member.char_sql_id, member.char_name, avatar_id, True
        return member.char_sql_id, member.char_name or "Unknown", 0, member.is_online

    # ── Send choreography (GameServer.Zone.cs) ───────────────────────────────

    def _send(self, conn: Optional["RRConnection"], packet: bytes) -> None:
        if conn is not None and getattr(conn, "is_spawned", False):
            conn.send_to_client(packet)

    def _send_talkback_join(self, conn: Optional["RRConnection"],
                            talk_group_id: int) -> None:
        """0x50 voice handoff — C# appends it to every prime/roster send.

        ``talk_group_id`` is the group id for grouped sends and the player's
        OWN charSqlId for solo sends (C# solo/leave paths). The IPv4 is the
        server address this client is connected to (C# LocalEndPoint); C#
        skips the send without an IPv4, we fall back to loopback instead —
        the handoff packet matters, the voice endpoint doesn't (nothing
        listens on 2604 even in C#; the client tolerates the refused connect).
        """
        if conn is None or not talk_group_id:
            return
        ip4 = b"\x7f\x00\x00\x01"
        try:
            sockname = conn.writer.get_extra_info("sockname")
            if sockname:
                parts = [int(p) for p in str(sockname[0]).split(".")]
                if len(parts) == 4 and all(0 <= p <= 255 for p in parts):
                    ip4 = bytes(parts)
        except Exception:  # noqa: BLE001 — loopback fallback covers tests/IPv6
            pass
        try:
            from . import player_modifiers
            is_member = player_modifiers._account_is_member(
                getattr(conn, "login_name", "") or "")
        except Exception:  # noqa: BLE001 — never mislabel a paying account
            is_member = True
        self._send(conn, build_join_talkback_group(
            self._char_sql_id(conn), is_member, talk_group_id, ip4))

    def _send_group_connected_to_all(self, group: Group) -> None:
        """0x30 (once per client) + full 0x35 roster to every online member."""
        leader = group.member(group.leader_login)
        ordered = ([leader] if leader else []) + [
            m for m in group.members if m is not leader]
        entries = [self._member_wire_entry(m) for m in ordered]

        leader_char_id = leader.char_sql_id if leader else 0
        for member in group.members:
            if not member.is_online:
                continue
            conn = self._conn_for_login(member.login)
            if conn is None or not getattr(conn, "is_spawned", False):
                continue
            if not getattr(conn, "group_connected_sent", False):
                self._send(conn, build_process_connected(
                    self._char_sql_id(conn), group.monster_difficulty,
                    group.invite_mode))
                conn.group_connected_sent = True
            self._send(conn, build_user_changed_group(
                group.group_id, leader_char_id, 0xFF,
                1 if group.is_open else 0, entries))
            # C# follows every roster 0x35 with the 0x50 talkback handoff.
            self._send_talkback_join(conn, group.group_id)
        log.info(f"[GROUP] roster sent: group={group.group_id} "
                 f"leader='{group.leader_login}' members="
                 f"{[m.login for m in group.members]}")

    def _send_group_health_to_all(self, group: Group) -> None:
        for member in group.members:
            if not member.is_online:
                continue
            conn = self._conn_for_login(member.login)
            if conn is None or not getattr(conn, "is_spawned", False):
                continue
            self._fan_out(group, build_member_health_mana(
                self._char_sql_id(conn), *self._health_fifteenths(conn)))

    def _fan_out(self, group: Group, packet: bytes,
                 skip_login: Optional[str] = None) -> None:
        for member in group.members:
            if not member.is_online or member.login == skip_login:
                continue
            self._send(self._conn_for_login(member.login), packet)

    def _send_solo_state(self, conn: "RRConnection") -> None:
        conn.group_connected_sent = False
        self._send(conn, build_solo_state(self._char_sql_id(conn)))

    @staticmethod
    def _health_fifteenths(conn: "RRConnection") -> tuple[int, int]:
        """Party-frame HP/MP as 0..15 fractions (C# ResolveGroupMemberHealthMana).

        Current = the freshest client self-report (the avatar's HP is
        client-owned — bible §6; ``conn.hp_wire`` tracks the same report after
        adoption, so the MAX must be re-derived from the character record, not
        read off the conn). Mana has no client report the server adopts, so the
        MP bar is held full rather than shown wrongly drained."""
        current = getattr(conn, "client_hp_wire", None)
        if current is None:
            current = getattr(conn, "hp_wire", 0) or 0
        max_wire = 0
        try:
            from ..db import character_repository
            from ..data.player_state import compute_saved_avatar_hp_wire
            saved = character_repository.get_character(
                getattr(conn, "char_sql_id", 0))
            max_wire = compute_saved_avatar_hp_wire(saved) & 0xFFFFFF00
        except Exception as ex:  # noqa: BLE001 — party bar is best-effort UI
            log.debug(f"[GROUP] max-HP lookup failed for "
                      f"'{getattr(conn, 'login_name', '?')}': {ex}")
        if max_wire <= 0:
            max_wire = max(current, getattr(conn, "hp_wire", 0) or 0, 1)
        return max(0, min(15, current * 15 // max_wire)), 15

    # ── Group lifecycle ──────────────────────────────────────────────────────

    def create(self, leader_conn: "RRConnection") -> Optional[Group]:
        login = leader_conn.login_name
        if not login or login in self._player_groups:
            return self._group_of(login) if login else None
        group = Group(
            group_id=self._next_id, leader_login=login,
            members=[GroupMember(
                login=login, char_name=self._char_name(leader_conn),
                char_sql_id=self._char_sql_id(leader_conn),
                zone_name=leader_conn.current_zone_name or "")],
        )
        self._next_id += 1
        self._groups[group.group_id] = group
        self._player_groups[login] = group.group_id
        log.info(f"[GROUP] created group {group.group_id} leader='{login}'")
        return group

    def invite(self, inviter_conn: "RRConnection", target_name: str) -> None:
        """Invite by character/login name (wire 0x16 + the @invite command)."""
        self._invite(inviter_conn, self._find_player(target_name), target_name)

    def invite_by_char_id(self, inviter_conn: "RRConnection",
                          char_id: int) -> None:
        """Invite by charSqlId (wire 0x12 — right-click portrait invite)."""
        self._invite(inviter_conn, self._find_player_by_char_id(char_id),
                     f"#{char_id}")

    def _invite(self, inviter_conn: "RRConnection",
                target: Optional["RRConnection"], target_label: str) -> None:
        login = inviter_conn.login_name
        if not login:
            return
        if target is None or not target.login_name:
            inviter_conn.send_system_message(
                f"Player '{target_label}' not found.")
            return
        if target.login_name == login:
            return

        group = self._group_of(login)
        if group is None:
            group = self.create(inviter_conn)
            if group is None:
                return
            self._send_group_connected_to_all(group)
        if group.leader_login != login:
            inviter_conn.send_system_message("Only the group leader can invite.")
            return
        if len(group.members) >= MAX_GROUP_SIZE:
            inviter_conn.send_system_message(
                f"Group is full (max {MAX_GROUP_SIZE} players).")
            return
        if target.login_name in self._player_groups:
            inviter_conn.send_system_message(
                f"{self._char_name(target)} is already in a group.")
            return

        self._pending_invites[target.login_name] = group.group_id
        inviter_char_id = self._char_sql_id(inviter_conn)
        self._send(target, build_process_invitation(
            inviter_char_id, group.group_id, self._char_name(inviter_conn)))
        inviter_conn.send_system_message(
            f"Invite sent to {self._char_name(target)}.")
        log.info(f"[GROUP] '{login}' invited '{target.login_name}' to "
                 f"group {group.group_id}")

    def accept(self, conn: "RRConnection") -> None:
        login = conn.login_name
        if not login:
            return
        gid = self._pending_invites.pop(login, None)
        group = self._groups.get(gid) if gid else None
        if group is None:
            conn.send_system_message("No pending group invitation.")
            return
        if len(group.members) >= MAX_GROUP_SIZE:
            conn.send_system_message("Group is full.")
            return
        # Joining supersedes any group the invitee was somehow still in.
        if login in self._player_groups:
            self.leave(conn)

        group.members.append(GroupMember(
            login=login, char_name=self._char_name(conn),
            char_sql_id=self._char_sql_id(conn),
            zone_name=conn.current_zone_name or "",
            personal_difficulty=group.monster_difficulty))
        self._player_groups[login] = group.group_id
        self._send_group_connected_to_all(group)
        self._send_group_health_to_all(group)
        # Cross-fan the members' 0x4C zone labels NOW, not only on the next zone
        # transition. C# (and our prior port) sent zone labels only from the
        # zone-join tail, so a group formed IN PLACE — both players standing in
        # town — left every member with no zone label → the client greys the
        # roster entry "as if in a different zone" even though they're
        # co-located (live user report 2026-07-09). The label carries each
        # member's real current_zone_name, so a same-zone member matches the
        # viewer's own zone and un-greys; a member actually elsewhere still
        # greys correctly.
        self._send_zone_states(group)
        log.info(f"[GROUP] '{login}' joined group {group.group_id} "
                 f"({len(group.members)} members)")

    def decline(self, conn: "RRConnection") -> None:
        if conn.login_name and self._pending_invites.pop(conn.login_name, None):
            log.info(f"[GROUP] invite declined by '{conn.login_name}'")

    def leave(self, conn: "RRConnection") -> None:
        """Voluntary leave (wire 0x22): 0x43 to the others, solo state to the
        leaver, dissolve at 1 remaining, leader hand-off (C# choreography)."""
        login = conn.login_name
        group = self._group_of(login) if login else None
        if group is None:
            return
        was_leader = group.leader_login == login

        self._fan_out(group, build_remove_user(
            group.group_id, self._char_sql_id(conn)), skip_login=login)
        self._remove_member(group, login)
        self._send_solo_state(conn)
        # C# 0x22: the leaver's talkback session re-keys to their OWN id.
        self._send_talkback_join(conn, self._char_sql_id(conn))
        self._after_roster_shrink(group, leader_left=was_leader,
                                  talkback_last=True)
        log.info(f"[GROUP] '{login}' left group {group.group_id}")

    def kick(self, leader_conn: "RRConnection", target_name: str) -> None:
        """Kick by name (the @kick command)."""
        target = self._find_player(target_name)
        if target is not None:
            self._kick_conn(leader_conn, target)

    def kick_by_char_id(self, leader_conn: "RRConnection", char_id: int) -> None:
        """Kick by charSqlId (wire 0x14)."""
        group = self.get_group(leader_conn)
        if group is None or group.leader_login != leader_conn.login_name:
            return
        member = group.member_by_char_id(char_id)
        if member is None or member.login == leader_conn.login_name:
            return
        target = self._conn_for_login(member.login)
        if target is not None:
            self._kick_conn(leader_conn, target)
        else:
            # Offline member: no client to notify, just drop the roster entry.
            self._fan_out(group, build_remove_user(group.group_id, char_id))
            self._remove_member(group, member.login)
            self._after_roster_shrink(group, leader_left=False)

    def _kick_conn(self, leader_conn: "RRConnection",
                   target: "RRConnection") -> None:
        group = self.get_group(leader_conn)
        if group is None or group.leader_login != leader_conn.login_name:
            leader_conn.send_system_message("Only the group leader can kick.")
            return
        t_login = target.login_name
        if not t_login or group.member(t_login) is None \
                or t_login == leader_conn.login_name:
            return
        # C# sends the removeUser to ALL members, the target included.
        self._fan_out(group, build_remove_user(
            group.group_id, self._char_sql_id(target)))
        self._remove_member(group, t_login)
        self._send_solo_state(target)
        target.send_system_message("You were removed from the group.")
        self._after_roster_shrink(group, leader_left=False)
        log.info(f"[GROUP] '{t_login}' kicked from group {group.group_id}")

    def _remove_member(self, group: Group, login: str) -> None:
        group.members = [m for m in group.members if m.login != login]
        self._player_groups.pop(login, None)
        self._pending_invites.pop(login, None)

    def _after_roster_shrink(self, group: Group, *, leader_left: bool,
                             talkback_last: bool = False) -> None:
        """Shared leave/kick tail: dissolve a 1-member group, else re-send the
        roster (with a fresh 0x30 for everyone when the leader changed).
        ``talkback_last``: C#'s leave path (0x22) re-keys the last member's
        talkback session to their own id; its kick path (0x14) does not."""
        if not group.members:
            self._groups.pop(group.group_id, None)
            log.info(f"[GROUP] group {group.group_id} disbanded (empty)")
            return
        if len(group.members) == 1:
            last = group.members[0]
            last_conn = self._conn_for_login(last.login)
            if last_conn is not None:
                self._send_solo_state(last_conn)
                if talkback_last:
                    self._send_talkback_join(
                        last_conn, self._char_sql_id(last_conn))
                last_conn.send_system_message("Group disbanded.")
            self._remove_member(group, last.login)
            self._groups.pop(group.group_id, None)
            log.info(f"[GROUP] group {group.group_id} dissolved to solo")
            return
        if leader_left:
            online = [m for m in group.members if m.is_online]
            group.leader_login = (online[0] if online else group.members[0]).login
            for member in group.members:
                m_conn = self._conn_for_login(member.login)
                if m_conn is not None:
                    m_conn.group_connected_sent = False
            log.info(f"[GROUP] leadership of group {group.group_id} -> "
                     f"'{group.leader_login}'")
        self._send_group_connected_to_all(group)

    def set_leader(self, conn: "RRConnection", target_name: str) -> None:
        """Transfer leadership by name (the @leader command)."""
        target = self._find_player(target_name)
        if target is not None:
            self._set_leader_conn(conn, target)

    def set_leader_by_char_id(self, conn: "RRConnection", char_id: int) -> None:
        """Transfer leadership by charSqlId (wire 0x15)."""
        group = self.get_group(conn)
        member = group.member_by_char_id(char_id) if group else None
        target = self._conn_for_login(member.login) if member else None
        if target is not None:
            self._set_leader_conn(conn, target)

    def _set_leader_conn(self, conn: "RRConnection",
                         target: "RRConnection") -> None:
        group = self.get_group(conn)
        if group is None or group.leader_login != conn.login_name:
            conn.send_system_message("Only the group leader can transfer leadership.")
            return
        member = group.member(target.login_name or "")
        if member is None:
            conn.send_system_message("That player is not in your group.")
            return
        group.leader_login = member.login
        # The new leader's personal difficulty becomes the group's (C# SetLeader).
        group.monster_difficulty = member.personal_difficulty
        self._fan_out(group, build_set_leader(
            group.group_id, member.char_sql_id or self._char_sql_id(target)))
        log.info(f"[GROUP] leader of group {group.group_id} set to "
                 f"'{member.login}'")

    # ── Settings (difficulty / invite mode / open group) ─────────────────────

    def set_monster_difficulty(self, conn: "RRConnection", difficulty: int) -> None:
        group = self.get_group(conn)
        if group is None or difficulty >= 4:
            return
        member = group.member(conn.login_name or "")
        if member is None:
            return
        member.personal_difficulty = difficulty
        personal_only = group.leader_login != conn.login_name
        if not personal_only:
            group.monster_difficulty = difficulty
        packet = build_monster_difficulty(difficulty, personal_only)
        if personal_only:
            self._send(conn, packet)
        else:
            self._fan_out(group, packet)

    def set_invite_mode(self, conn: "RRConnection", mode: int) -> None:
        group = self.get_group(conn)
        if group is not None:
            group.invite_mode = mode
        self._send(conn, build_changed_invite_mode(mode))

    def set_open_group(self, conn: "RRConnection", is_open: bool) -> None:
        group = self.get_group(conn)
        if group is None:
            return
        group.is_open = is_open
        self._send_group_connected_to_all(group)

    # ── Instancing (bible §7 — the seam game_server._group_instance_token uses) ─

    def instance_token_for(self, conn: "RRConnection") -> Optional[int]:
        """The group's dungeon-copy token, minted on first use; None when solo.

        Every grouped member entering a private zone lands on
        ``(zone_id, token)`` — one shared world copy per group. The token is
        per-GROUP (not per-zone): distinct zones already split on ``zone_id``.
        """
        group = self.get_group(conn)
        if group is None:
            return None
        if not group.instance_token:
            group.instance_token = self._server.allocate_instance_id()
            log.info(f"[GROUP] group {group.group_id} minted instance token "
                     f"{group.instance_token}")
        return group.instance_token

    def reset_instances(self, conn: "RRConnection") -> None:
        """Wire 0x26 — leader-only: the NEXT dungeon entry gets a fresh copy.

        Members currently inside keep their live conn.instance_id (the key must
        stay stable while a level is occupied); their old copy tears down when
        the last of them walks out (world_instance.leave). Solo players need no
        reset — every solo entry already mints a fresh private token.
        """
        group = self.get_group(conn)
        if group is None:
            conn.send_system_message("Dungeon instances reset.")
            return
        if group.leader_login != conn.login_name:
            conn.send_system_message(
                "Only the party leader can reset dungeon instances.")
            return
        group.instance_token = self._server.allocate_instance_id()
        conn.send_system_message(
            "Dungeon instances reset. Re-enter to get a new layout.")
        log.info(f"[GROUP] group {group.group_id} instances reset -> token "
                 f"{group.instance_token}")

    def goto_member(self, conn: "RRConnection", char_id: int) -> None:
        """Wire 0x27 — right-click roster "Go To": warp to the member's side.

        A grouped requester adopts the group instance token on the transfer, so
        landing in the member's dungeon means landing in the SAME copy.
        """
        group = self.get_group(conn)
        member = group.member_by_char_id(char_id) if group else None
        target = self._conn_for_login(member.login) if member else None
        if target is None or not getattr(target, "is_spawned", False) \
                or target is conn:
            conn.send_system_message("That group member cannot be reached.")
            return
        zone = target.current_zone_name or "town"
        log.info(f"[GROUP] goto: '{conn.login_name}' -> '{target.login_name}' "
                 f"in '{zone}'")
        self._server.change_zone_to_position(
            conn, zone, target.player_pos_x, target.player_pos_y,
            target.player_pos_z)

    # ── Presence hooks (game_server lifecycle) ───────────────────────────────

    def on_disconnect(self, conn: "RRConnection") -> None:
        """Member dropped: keep the roster seat, grey it out (C#
        DisconnectMember + 0x49)."""
        login = conn.login_name
        group = self._group_of(login) if login else None
        if login:
            self._pending_invites.pop(login, None)
        if group is None:
            return
        member = group.member(login)
        if member is None:
            return
        member.is_online = False
        conn.group_connected_sent = False
        self._fan_out(group, build_member_disconnected(
            group.group_id, member.char_sql_id), skip_login=login)
        log.info(f"[GROUP] '{login}' went offline in group {group.group_id}")

    def on_player_spawned(self, conn: "RRConnection") -> None:
        """Zone-join tail hook (fresh spawn, zone transfer AND reconnect).

        Solo: prime the client's group subsystem with the C# zone-transition
        else-branch — 0x30 processConnected (self charSqlId, difficulty 0,
        invite mode 0) + the empty solo 0x35. The client stores the 0x30 id as
        its own group identity (C# log: "GC+0xB0"); without this priming the
        right-click group menu and later roster packets have no self to key on
        (live 2026-07-08: invite option missing / roster ignored). Deliberately
        does NOT touch ``group_connected_sent`` — C#'s solo send is ungated, so
        joining a group later still delivers the group 0x30.

        Grouped: refresh the member's zone, re-send the full roster (the C#
        zone-transition block; 0x30 stays gated on ``group_connected_sent``,
        which is sticky across zone changes exactly like C#) and cross-fan the
        members' 0x4C zone labels. A returning offline member is re-onlined
        first (0x4A to the others).

        2026-07-08 the spawn-tail sends stalled the 666 client's loading
        screen; the 2026-07-09 C# re-audit found placement and bytes already
        identical to the working reference — the missing talkback 0x50 was the
        only wire delta and is now appended exactly where C# appends it. If
        the stall recurs, A/B with DR_GROUP_PRIME=0 and trace the load-screen
        wait live (x64dbg) instead of guessing at the send point.
        """
        if not GROUP_PRIME_ENABLED:
            return
        login = conn.login_name
        group = self._group_of(login) if login else None
        if group is None:
            if login and getattr(conn, "is_spawned", False):
                self_key = self._char_sql_id(conn)
                self._send(conn, build_process_connected(self_key, 0, 0))
                self._send(conn, build_solo_state(self_key))
                # C# solo prime: talkback keyed to the player's OWN id.
                self._send_talkback_join(conn, self_key)
                # The right-click "Invite to Group" SEND-gate (client
                # FUN_005f6f20) requires this prime: it only emits the 0x12
                # invite when +0xd0(groupId)!=0 AND +0xd4(leader)==+0xb0(self).
                # 0x30 sets +0xb0=self_key, the solo 0x35 sets +0xd0=1 +0xd4=
                # self_key → gate passes. Logged so a silent prime is visible.
                log.info(f"[GROUP] solo prime sent to '{login}' "
                         f"(self_key={self_key}, spawned=True)")
            else:
                log.info(f"[GROUP] solo prime SKIPPED for "
                         f"'{login}' (spawned={getattr(conn, 'is_spawned', False)})")
            return
        member = group.member(login)
        if member is None:
            return
        if not member.is_online:
            member.is_online = True
            self._fan_out(group, build_member_reconnected(
                group.group_id, member.char_sql_id), skip_login=login)
            log.info(f"[GROUP] '{login}' reconnected to group {group.group_id}")
        member.zone_name = conn.current_zone_name or ""
        self._send_group_connected_to_all(group)
        self._send_zone_states(group)
        self._send_group_health_to_all(group)

    def _send_zone_states(self, group: Group) -> None:
        """Every member receives every OTHER member's 0x4C zone label."""
        states: List[tuple[str, bytes]] = []
        for member in group.members:
            if not member.is_online:
                continue
            m_conn = self._conn_for_login(member.login)
            if m_conn is None or not getattr(m_conn, "is_spawned", False):
                continue
            member.zone_name = m_conn.current_zone_name or member.zone_name
            states.append((member.login, build_user_changed_zone(
                member.char_sql_id or self._char_sql_id(m_conn),
                member.zone_name)))
        for member in group.members:
            conn = self._conn_for_login(member.login) if member.is_online else None
            if conn is None:
                continue
            for owner_login, packet in states:
                if owner_login != member.login:
                    self._send(conn, packet)

    def on_member_hp(self, conn: "RRConnection") -> None:
        """Adopted-HP hook: fan the member's party-frame HP (0x4B), throttled.

        Driven by combat.adopt_client_avatar_hp — the client's own self-report
        is the only truthful avatar HP (bible §6), so the party bars follow it.
        This is UI-level (ch 9), never an entity synch trailer: it cannot trip
        the zero-tolerance compare.
        """
        login = conn.login_name
        if not login or login not in self._player_groups:
            return
        now = time.monotonic()
        if now - self._last_health_push.get(login, 0.0) < _HEALTH_PUSH_INTERVAL:
            return
        self._last_health_push[login] = now
        group = self._group_of(login)
        if group is None:
            return
        self._fan_out(group, build_member_health_mana(
            self._char_sql_id(conn), *self._health_fifteenths(conn)))
