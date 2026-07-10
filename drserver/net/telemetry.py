"""Combat telemetry channel — ground-truth combat events from the client hook.

Combat is client-authoritative and packet-blind (bible §6-LIVE.4): the client
kills mobs locally but the native protocol never tells the server, so loot/XP
never fire, and the server's replay-based kill mis-times the despawn (§6-LIVE.6
— it either kills the mob before the client does, or never, so no rewards). The
``client_hook/`` DLL detours the client's damage dispatch (``FUN_004f6580``) and
reports the exact local kill here over a dedicated TCP socket; the server then
runs its existing authoritative reward path (``CombatManager.notify_client_kill``
→ despawn + loot + quest + XP) at the moment the kill actually happens.

This is an out-of-band control channel (NOT the §2 hot relay path) between our
own client agent and our own server — not third-party infrastructure.

Wire protocol (little-endian, one record per event), matching
``client_hook/telemetry_client.c``:

==========  =========================================  =======
opcode      payload                                    bytes
==========  =========================================  =======
HELLO 0x01  ``[avatar_eid u32]``                       5
KILL  0x02  ``[victim_eid u32][killer_eid u32]``       9
DAMAGE 0x03 ``[target u32][attacker u32][hp_after u32]`` 13   (phase 2)
==========  =========================================  =======
"""
from __future__ import annotations

import asyncio
import struct
from typing import TYPE_CHECKING, Optional

from ..core import log

if TYPE_CHECKING:  # pragma: no cover
    from .connection import RRConnection
    from .game_server import GameServer

OP_HELLO = 0x01
OP_KILL = 0x02
OP_DAMAGE = 0x03
OP_KILL_AT = 0x04
OP_KILL_AT_XP = 0x05      # KILL_AT + the killer's exact Experience (u32) appended

# ── Server→client opcodes (high bit set, namespaced away from C→S 0x01–0x05) ──
# MOB_ATTACK tells the hook a server-driven mob hit the player; the hook applies
# the damage locally via Damage::apply (FUN_004f6580) at the per-frame pump —
# see docs/MOB_ATTACK_INJECTION.md. Server-authoritative mob→player damage with
# the mob kept display-only (no client-brain enroll → no run-through). The
# avatar (target) eid is sent explicitly so the hook resolves the target from
# its eid→Unit* cache without guessing.
OP_MOB_ATTACK = 0x80   # [mob_eid u32][avatar_eid u32][damage_wire u32][element u8] (14 bytes)
# ZONE_RESET tells the hook to drop its eid→Unit* cache + pending attacks on a
# zone change, so no Unit* learned in the previous zone (where the avatar/mobs
# are freed and re-created) is ever dereferenced after the transfer.
OP_ZONE_RESET = 0x81   # (no payload, 1 byte)
# MOB_CLAMP marks an aggroed mob for the run-through fix (bible §14.6): the hook
# pins it to the stop-ring around the avatar every frame (rewrites the mob's
# world position unit+0x90/+0x94), so the client-side Follow action can't drive
# it through the player. Server packets alone can't fix this — the client's local
# Follow owns mob movement. Streamed on the mob's chase cadence; the hook ages a
# mob out CLAMP_FRESH_MS after the last refresh (de-aggro / death).
OP_MOB_CLAMP = 0x82    # [mob_eid u32][avatar_eid u32][ring_wire u32] (13 bytes)

# payload length (excluding the 1-byte opcode) per opcode. KILL_AT carries the
# victim's death position (3×i32, Fixed32 ×256) and the killer's level (u16);
# KILL_AT_XP appends the killer's exact progress-into-level Experience (u32) so
# the server adopts the client's true XP and the zone-transfer re-send stops
# clobbering it. KILL_AT_XP is emitted only by a rebuilt client hook; the older
# DLL keeps sending KILL_AT, so both coexist.
_PAYLOAD_LEN = {OP_HELLO: 4, OP_KILL: 8, OP_DAMAGE: 12,
                OP_KILL_AT: 22, OP_KILL_AT_XP: 26}


class _Hook:
    """A live client-hook connection: its outbound writer + the avatar eid it
    reported in HELLO (0 until then). Lets the server push MOB_ATTACK to the
    right player's hook."""

    __slots__ = ("writer", "avatar_eid")

    def __init__(self, writer: "asyncio.StreamWriter") -> None:
        self.writer = writer
        self.avatar_eid = 0


class TelemetryServer:
    """TCP listener that turns client-reported combat events into authoritative
    server actions, and pushes server-driven mob attacks back to the hook. The
    dispatch helpers (:meth:`on_kill`, :meth:`conn_for_avatar_eid`) are
    socket-free so they can be unit-tested directly."""

    def __init__(self, game: "GameServer", host: str, port: int) -> None:
        self._game = game
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._active = 0          # live hook connections
        # Live hooks for the server→client direction (MOB_ATTACK). Each entry is
        # a _Hook(writer, avatar_eid); avatar_eid is learned from the HELLO and
        # used to route a mob-attack to the right player's client in MP.
        self._hooks: "list[_Hook]" = []

    def has_active_hook(self) -> bool:
        """True while at least one client hook is connected. The combat manager
        only cedes kill authority to telemetry while a hook is actually present,
        so the swing-replay keeps driving kills (loot/XP) when no hook is — e.g.
        an un-deployed/old client DLL — instead of nothing killing mobs."""
        return self._active > 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port)
        log.info(f"[Telemetry] combat channel listening on {self._host}:{self._port}")
        async with self._server:
            await self._server.serve_forever()

    # ── dispatch (socket-free, testable) ─────────────────────────────────────

    def conn_for_avatar_eid(self, eid: int) -> "Optional[RRConnection]":
        """Reverse-map a client-side avatar entity id to its connection.

        ``spawn.py`` stores ``player_avatar_entity_id[str(conn_id)] = avatar.id``,
        and the synch protocol forces that id to equal the value the client uses,
        so the killer eid the hook reports matches exactly one entry."""
        game = self._game
        for cid_str, avatar_id in game.player_avatar_entity_id.items():
            if avatar_id == eid:
                try:
                    return game.connections.get(int(cid_str))
                except (TypeError, ValueError):
                    return None
        return None

    def _conn_for_owned_unit(self, eid: int) -> "Optional[RRConnection]":
        """Resolve a kill whose attacker is a player's OWNED unit (summon / bling
        gnome) back to the owner connection.

        The hook reports the killing blow's *attacker* entity id
        (``combat_hook.c`` reads ``attacker[+0x80]``); a mob finished off by the
        player's snowman/bait/gnome reports that unit's eid — which is never an
        avatar, so :meth:`conn_for_avatar_eid` misses it and the kill goes
        uncredited (the live "no loot drops from bosses" report)."""
        game = self._game
        for mgr_name in ("summons", "gnome"):
            mgr = getattr(game, mgr_name, None)
            finder = getattr(mgr, "owner_conn_for_entity", None)
            if finder is not None:
                conn = finder(eid)
                if conn is not None:
                    return conn
        return None

    def _sole_player_for_monster(self, victim_eid: int) -> "Optional[RRConnection]":
        """Fallback: credit the kill to the only spawned player sharing the dying
        mob's zone INSTANCE.

        Covers AoE/DoT/projectile kills whose damage-source entity is neither the
        avatar nor a tracked owned unit. Unambiguous for solo dungeons (the common
        case); returns None when 0 or >1 players could claim it, so shared (group)
        instances keep the existing 'unresolved' behaviour (no mis-credit). The
        instance filter keeps two solo players in separate private copies of the
        same dungeon from claiming each other's kills — and makes the common
        one-player-per-copy case resolve instead of bailing on candidate count."""
        game = self._game
        combat = getattr(game, "combat", None)
        mon = combat.get_monster(victim_eid) if (
            combat is not None and hasattr(combat, "get_monster")) else None
        zone = getattr(mon, "zone_gc_type", None)
        inst = getattr(mon, "instance_id", None) if zone is not None else None
        candidates = [
            c for c in game.connections.values()
            if getattr(c, "is_spawned", False)
            and (zone is None or getattr(c, "current_zone_gc_type", None) == zone)
            and (inst is None or getattr(c, "instance_id", 0) == inst)
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_killer(self, killer_eid: int,
                        victim_eid: int) -> "tuple[Optional[RRConnection], bool]":
        """Resolve a reported killer eid to ``(conn, is_avatar)``.

        ``is_avatar`` gates the level snap: the hook reads the killer's level off
        the avatar's embedded PlayerState (``attacker[+0x314]``), valid ONLY when
        the attacker IS the avatar. For an owned-unit / fallback credit that field
        is garbage, so the snap must be skipped — a bogus high level would make the
        server LEAD the client and crash the zero-tolerance HP synch compare."""
        conn = self.conn_for_avatar_eid(killer_eid)
        if conn is not None:
            return conn, True
        conn = self._conn_for_owned_unit(killer_eid)
        if conn is not None:
            return conn, False
        conn = self._sole_player_for_monster(victim_eid)
        if conn is not None:
            return conn, False
        return None, False

    def on_kill(self, victim_eid: int, killer_eid: int,
                death_pos: "Optional[tuple[float, float, float]]" = None,
                killer_level: int = 0,
                killer_experience: "Optional[int]" = None) -> bool:
        """Process a client-reported kill. Returns True if a tracked mob was
        credited (the server filters: non-mob victims / unresolved killers are
        ignored, so the hook can report every death blindly).

        ``death_pos`` (world floats) is the mob's real death spot, used to drop
        loot ON the mob rather than at the killer / spawn anchor (KILL_AT only).
        ``killer_level`` is the client's current avatar level — snapped onto the
        character **before** the mob filter, so the level stays in lockstep even
        for kills the server can't credit (untracked mob / unresolved killer).
        ``killer_experience`` (KILL_AT_XP only) is the client's exact
        progress-into-level XP — adopted verbatim as the SOLE XP authority so the
        zone-transfer Avatar re-send matches the client and never clobbers it."""
        game = self._game
        combat = getattr(game, "combat", None)
        if combat is None:
            return False
        killer_conn, killer_is_avatar = self._resolve_killer(killer_eid, victim_eid)

        # Level snap runs regardless of whether the mob is server-tracked: the
        # client self-levels on EVERY kill, so gating the snap on a tracked
        # victim would let the level drift on any kill the server missed. It is
        # gated on a *direct avatar* killer, though: the hook reads the level off
        # attacker[+0x314], which is only the avatar's PlayerState — for an
        # owned-unit / fallback credit that value is garbage and snapping it would
        # make the server lead the client (synch crash).
        level_synced = False
        if killer_conn is not None and killer_is_avatar and killer_level > 0:
            sync = getattr(game, "sync_client_level", None)
            if sync is not None:
                if killer_experience is not None:
                    sync(killer_conn, killer_level,
                         client_experience=killer_experience)
                else:
                    sync(killer_conn, killer_level)
                level_synced = True

        if not combat.is_monster(victim_eid):
            return False                       # not a server-tracked mob — ignore
        if killer_conn is None:
            log.warn(f"[Telemetry] KILL victim={victim_eid} killer_eid={killer_eid} "
                     "unresolved (no player / owned unit) — ignored")
            return False
        log.info(f"[Telemetry] KILL victim={victim_eid} by conn={killer_conn.conn_id}"
                 + ("" if killer_is_avatar else f" (via owned unit {killer_eid})")
                 + (f" at ({death_pos[0]:.0f},{death_pos[1]:.0f})" if death_pos else ""))
        # Suppress the server's own XP grant whenever the snap is authoritative
        # (avatar kill) OR the kill was credited to a non-avatar attacker. In the
        # latter case the level field is untrusted (no snap happened), so a
        # server XP grant could lead the client's level — the synch compare
        # crashes on the server LEADING just as hard as lagging. award_kill_xp
        # stays live only for the legacy / no-level direct-avatar path.
        if not killer_is_avatar:
            level_synced = True
        return combat.notify_client_kill(victim_eid, killer_conn,
                                         death_pos=death_pos,
                                         level_synced=level_synced)

    def on_damage(self, target_eid: int, attacker_eid: int, hp_after_wire: int) -> None:
        """Phase 2: relay mid-fight mob HP to displaying clients (HP bars). Stub."""
        return None

    # ── server→client push (MOB_ATTACK) ──────────────────────────────────────

    def send_mob_attack(self, conn: "RRConnection", mob_eid: int,
                        damage_wire: int, element: int = 0) -> bool:
        """Push a server-driven mob attack to ``conn``'s client hook.

        The hook applies ``damage_wire`` (HP ×256) to the avatar locally via
        Damage::apply on the mob's next synch (docs/MOB_ATTACK_INJECTION.md), so
        the mob can hurt the player while staying display-only (no run-through).
        Returns True if at least one hook was written to. Routed by avatar eid
        when known; broadcast to all hooks otherwise (solo). Fire-and-forget —
        a tiny 10-byte record, no drain await.
        """
        if not self._hooks:
            return False
        avatar_eid = self._avatar_eid_for_conn(conn)
        rec = struct.pack("<BIIIB", OP_MOB_ATTACK, mob_eid & 0xFFFFFFFF,
                          avatar_eid & 0xFFFFFFFF, damage_wire & 0xFFFFFFFF,
                          element & 0xFF)
        sent = False
        for hook in list(self._hooks):
            # Route to the matching avatar when both ends know the eid; with an
            # unknown eid (no HELLO yet / server can't resolve) fall back to all.
            if avatar_eid and hook.avatar_eid and hook.avatar_eid != avatar_eid:
                continue
            try:
                hook.writer.write(rec)
                sent = True
            except Exception:  # noqa: BLE001 — a dead socket is reaped by its reader
                pass
        return sent

    def send_mob_clamp(self, conn: "RRConnection", mob_eid: int,
                       ring_wire: int) -> bool:
        """Push a mob-clamp intent to ``conn``'s client hook (run-through fix).

        Tells the hook to pin mob ``mob_eid`` to a ``ring_wire`` (Fixed32, world
        units ×256) stop-ring around ``conn``'s avatar each frame, so the client
        Follow action can't drive it through the player (bible §14.6). The hook
        self-validates the position offset before writing and ages the mark out
        once refreshes stop (de-aggro / death). Same routing + fire-and-forget
        shape as :meth:`send_mob_attack`. Returns True if a hook was written to.
        """
        if not self._hooks:
            return False
        avatar_eid = self._avatar_eid_for_conn(conn)
        rec = struct.pack("<BIII", OP_MOB_CLAMP, mob_eid & 0xFFFFFFFF,
                          avatar_eid & 0xFFFFFFFF, ring_wire & 0xFFFFFFFF)
        sent = False
        for hook in list(self._hooks):
            if avatar_eid and hook.avatar_eid and hook.avatar_eid != avatar_eid:
                continue
            try:
                hook.writer.write(rec)
                sent = True
            except Exception:  # noqa: BLE001 — a dead socket is reaped by its reader
                pass
        return sent

    def send_zone_reset(self, conn: "RRConnection") -> None:
        """Tell ``conn``'s hook to drop its cached units + pending attacks.

        Sent on every zone change: the avatar and mobs are freed/re-created
        across a transfer, so a Unit* learned in the old zone would dangle. The
        hook re-learns the new zone's units from the synch stream. Routed by
        avatar eid when known; broadcast otherwise. Fire-and-forget."""
        if not self._hooks:
            return
        avatar_eid = self._avatar_eid_for_conn(conn)
        rec = bytes((OP_ZONE_RESET,))
        for hook in list(self._hooks):
            if avatar_eid and hook.avatar_eid and hook.avatar_eid != avatar_eid:
                continue
            try:
                hook.writer.write(rec)
            except Exception:  # noqa: BLE001
                pass

    def _avatar_eid_for_conn(self, conn: "RRConnection") -> int:
        """The client-side avatar eid for ``conn`` (0 if unspawned/unknown).
        Mirrors :meth:`conn_for_avatar_eid` in reverse via the same map."""
        game = self._game
        cid = getattr(conn, "conn_id", None)
        if cid is None:
            return 0
        try:
            return int(game.player_avatar_entity_id.get(str(cid), 0))
        except (TypeError, ValueError):
            return 0

    # ── socket plumbing ───────────────────────────────────────────────────────

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        hook = _Hook(writer)
        self._hooks.append(hook)
        self._active += 1
        log.info(f"[Telemetry] hook connected from {peer} (active={self._active})")
        try:
            while True:
                op_byte = await reader.readexactly(1)
                op = op_byte[0]
                n = _PAYLOAD_LEN.get(op)
                if n is None:
                    log.warn(f"[Telemetry] unknown opcode 0x{op:02x} from {peer} — dropping")
                    break
                payload = await reader.readexactly(n) if n else b""
                self._dispatch(op, payload, hook)
        except asyncio.IncompleteReadError:
            log.info(f"[Telemetry] hook disconnected {peer}")
        except Exception as ex:  # noqa: BLE001 — never let a hook bug kill the channel
            log.warn(f"[Telemetry] handler error {peer}: {ex}")
        finally:
            self._active = max(0, self._active - 1)
            try:
                self._hooks.remove(hook)
            except ValueError:
                pass
            log.info(f"[Telemetry] hook gone {peer} (active={self._active})")
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    def _dispatch(self, op: int, payload: bytes,
                  hook: "Optional[_Hook]" = None) -> None:
        if op == OP_HELLO:
            (avatar_eid,) = struct.unpack("<I", payload)
            if hook is not None:
                hook.avatar_eid = avatar_eid
            log.info(f"[Telemetry] hello avatar_eid={avatar_eid}")
        elif op == OP_KILL:
            victim, killer = struct.unpack("<II", payload)
            self.on_kill(victim, killer)
        elif op == OP_KILL_AT:
            victim, killer, px, py, pz, level = struct.unpack("<IIiiiH", payload)
            # Positions arrive as Fixed32 (×256 "drfloat"); the loot path takes
            # world floats and re-applies ×256, so divide here.
            self.on_kill(victim, killer,
                         death_pos=(px / 256.0, py / 256.0, pz / 256.0),
                         killer_level=level)
        elif op == OP_KILL_AT_XP:
            victim, killer, px, py, pz, level, exp = struct.unpack(
                "<IIiiiHI", payload)
            # As KILL_AT, plus the killer's exact progress-into-level Experience.
            self.on_kill(victim, killer,
                         death_pos=(px / 256.0, py / 256.0, pz / 256.0),
                         killer_level=level, killer_experience=exp)
        elif op == OP_DAMAGE:
            target, attacker, hp_after = struct.unpack("<III", payload)
            self.on_damage(target, attacker, hp_after)
