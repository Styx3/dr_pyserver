"""Combat manager — monster HP tracking, combat message handling, death pipeline.

Ported from C# Combat/CombatManager.cs + CombatPackets.cs. The C# server is
CLIENT-AUTHORITATIVE for combat — the client computes all damage locally. The
server's role is:

1. Track spawned monster entity IDs and their HP (wire format: HP * 256)
2. Echo ActionResponse packets (0x35/0x01) for attacks/spells
3. Process client HP sync messages (0x36 EntitySyncHP, 0x03 SendUpdate)
4. Detect monster death (HP <= 0) → broadcast despawn → generate loot
5. Relay RNG seeds for deterministic combat

Phase 8: MVP with HP tracking, death detection, entity despawn, and basic loot.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..util.byte_io import LEReader, LEWriter
from ..data.gc_object import write_gc_type

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection


# The 0xFFFF00 ×256 value is the engine's MP/no-data sentinel (16776960 = 65535
# HP), never a real avatar HP — a base-only L100 avatar is 473600. Client reports
# at or above it are garbage and must not be adopted into conn.hp_wire.
_AVATAR_HP_SENTINEL_WIRE = 0xFFFF00

# Seconds to defer the entity-destroy (0x05) sent to the KILLER after a kill.
# The killer's client landed the final blow locally (its own Damage::apply zeroed
# the mob), so it plays the death anim → corpse → fade itself; an immediate 0x05
# pops the mob out of existence and skips all of that. Deferring lets the local
# death sequence finish, then the 0x05 is invisible cleanup. Non-killer displayers
# (MP) still get an immediate destroy (they can't show a local death without a
# server-driven death action — a later refinement).
_DEATH_DESPAWN_DELAY = 8.0


@dataclass
class TrackedMonster:
    """Runtime state for a spawned monster entity."""
    entity_id: int
    gc_type: str
    label: str
    current_hp: int              # wire format (HP * 256)
    max_hp: int                  # wire format
    level: int
    difficulty: str
    zone_gc_type: str
    pos_x: float
    pos_y: float
    pos_z: float
    spawn_time: float
    # The zone COPY this mob lives in. Every mob-referencing send (despawn, HP,
    # loot) must stay inside (zone_gc_type, instance_id): the same zone hosts
    # multiple private copies with disjoint entity ids, so a zone-wide send puts
    # an unknown eid on another copy's wire → client "Invalid EntityID" →
    # Zone communication error Code 6 (bible §10 error table).
    instance_id: int = 0
    # The world.* entity gc_type the client renders (e.g. world.dungeon00.mob.boss),
    # distinct from gc_type which is the creatures.* stat row (e.g.
    # creatures.whiskers.broodling.basic.champion). DoorsToOpenOnDeath and other
    # placement-authored attributes live on the ENTITY node, so the boss exit-gate
    # resolver keys off this, not the shared creature. Empty = same as gc_type.
    entity_gc_type: str = ""
    pending_kill: bool = False   # True when HP <= 0, waiting for client confirmation
    kill_confirmed_at: float = 0.0
    treasure_generators: List[tuple[str, int]] = field(default_factory=list)
    behavior_id: int = 0         # the 0x35 component-update target (≠ entity_id)
    last_attack_time: float = 0.0  # monster-AI swing cooldown anchor (seconds)
    # ── Server-driven chase state (monster_ai.py) ──
    target_id: int = 0           # aggroed player entity id (0 = idle)
    spawn_x: float = 0.0         # leash anchor (= spawn position)
    spawn_y: float = 0.0
    attack_range: float = 8.0    # authored weapon Range (melee 8 / ranged 90+)
    last_move_sent: float = 0.0  # 0x65 correction throttle anchor (seconds)
    last_follow_sent: float = 0.0  # Follow (re-)assert throttle anchor (seconds)
    last_clamp_sent: float = 0.0  # OP_MOB_CLAMP throttle anchor (seconds)
    defense_rating: float = 0.0  # authored creature DefenseRating (pre-curve)
    # conn_ids that ENROLLED (0x64) this mob into their client AI = the clients
    # that SIMULATE it locally. The server must NOT send them authoritative mob
    # HP: their local value is client-computed (packet-blind, unreproducible —
    # bible §6-LIVE.6/.7) so a server HP send risks the FUN_005dd900 compare +
    # the Code-9 corpse-purge. Only DISPLAYING clients (not in this set) get HP.
    simulated_by: Set[int] = field(default_factory=set)


class CombatManager:
    """Server-side combat state manager.

    Singletons — one instance lives on the GameServer.
    """

    def __init__(self, server: "GameServer"):
        self._server = server
        self._monsters: Dict[int, TrackedMonster] = {}  # entity_id -> monster
        self._last_diag_hp: Dict[int, int] = {}  # entity_id -> last-logged HP (REPLAY-DIAG dedup)
        self._rng_seed: int = 0

        # ── ROUTE 2B: server-side kill detection by replaying the swing stream.
        # The vanilla client is packet-proven blind (no monster-HP / kill / XP /
        # level-up report over TCP), so the server replays each ch7/0x50 swing
        # through the native damage path, applies it to the tracked monster HP,
        # and on a replayed kill feeds the SAME death pipeline the dead client-HP
        # trigger used to (_process_monster_kill -> award_kill_xp ->
        # _refresh_avatar_hp_wire), raising conn.hp_wire to the leveled value the
        # tick the client self-levels. The per-swing damage INPUT (equipment /
        # monster stat resolvers) and exact RNG-stream alignment are still
        # best-effort — see native_swing_input and the [REPLAY-DIAG] log.
        from ..combat.rng import MersenneTwister
        from ..combat.native_kill_replay import (
            NativeKillReplay, NativeMonsterHost, native_weapon_damage_resolver)
        self._combat_rng = MersenneTwister()
        # bible §6/§7: the live-validated client_swing resolver ("reproduce, don't
        # relay") is the canonical path, but its player-attacker magnitude is still
        # UNVERIFIED (stat_builder tiering). Gate it behind DR_CLIENT_SWING (default
        # OFF) — the DR_MONSTER_AI precedent — so live behaviour is unchanged until
        # opted in, then diff [CLIENT-SWING] vs a live swing to close the magnitude.
        _flag = os.environ.get("DR_CLIENT_SWING", "").strip().lower()
        if _flag in ("1", "true", "yes", "on"):
            from ..combat.client_swing_resolver import client_swing_damage_resolver
            resolver = client_swing_damage_resolver()
            log.info("[Combat] swing resolver = client_swing (DR_CLIENT_SWING on)")
        else:
            resolver = native_weapon_damage_resolver(self._make_swing_input)
            log.info("[Combat] swing resolver = monster_damage (DR_CLIENT_SWING off)")
        self._kill_replay = NativeKillReplay(
            NativeMonsterHost(resolver), self._on_replay_kill, rng=self._combat_rng)

    # ── ROUTE 2B: swing-replay kill detection ────────────────────────────────

    @property
    def _telemetry_authoritative(self) -> bool:
        """True only when telemetry is enabled AND a client hook is actually
        connected — then the server runs NO swing-replay emulation (the hook
        reports the kill). With no hook connected (old/un-deployed DLL), the
        replay keeps driving kills so loot/XP never silently stop."""
        if not getattr(getattr(self._server, "config", None),
                       "telemetry_authoritative_kills", True):
            return False
        telem = getattr(self._server, "telemetry", None)
        return bool(telem is not None and telem.has_active_hook())

    def _make_swing_input(self, cycle):
        """Build the per-swing :class:`NativeWeaponDamageInput` for a cycle.

        ``cycle.player_state`` is the killer connection (stashed at register
        time); ``cycle.monster`` is the tracked target.
        """
        from ..combat.native_swing_input import build_swing_input
        conn = cycle.player_state
        player_level = getattr(conn, "player_level", 1) if conn is not None else 1
        return build_swing_input(player_level, cycle.monster, conn=conn)

    def register_swing(self, conn: "RRConnection", target_id: int, now: float) -> None:
        """Client sent a ch7/0x50 UseTarget swing — feed the replay tracker.

        No-op when ``target_id`` is not a tracked monster (NPC/portal clicks
        route elsewhere). Mirrors the C# ``RegisterAttack`` call site (cs:13668).
        Skipped entirely under telemetry authority — the client hook reports the
        kill, so the server does no swing emulation / HP tracking.
        """
        if self._telemetry_authoritative:
            return
        monster = self._monsters.get(target_id)
        if monster is None:
            return
        self._kill_replay.register_swing(
            conn.login_name, target_id, monster,
            now=now, conn=conn, player_entity_id=conn.unit_behavior_id,
        )
        # Hold-attack sends ~4 ch7/0x50 use-targets per second, but the replay
        # tracker throttles actual damage to the weapon cadence (resolve_hit only
        # lands on a cooldown tick). Logging per-0x50 makes the server look like
        # it's swinging 4×/s ("multiple attacks on server" report 2026-06-15) when
        # it is not. Log only when the tracked HP actually moved — one line per
        # LANDED swing — so the diag reflects the real attack cadence.
        last = self._last_diag_hp.get(target_id)
        if last != monster.current_hp:
            self._last_diag_hp[target_id] = monster.current_hp
            log.info(f"[REPLAY-DIAG] swing '{conn.login_name}' -> eid={target_id} "
                     f"'{monster.label}' hp={monster.current_hp}/{monster.max_hp} "
                     f"L{monster.level} pLvl={getattr(conn, 'player_level', 1)}")

    def tick_combat(self, conn: "RRConnection", now: float) -> None:
        """Advance this connection's weapon cycle one native tick, finalize kills."""
        if self._telemetry_authoritative:
            return
        self._kill_replay.tick_player_entity(conn.unit_behavior_id, now)

    def clear_combat(self, conn_key: str) -> None:
        """Drop a connection's active weapon cycle (disconnect / zone change)."""
        self._kill_replay.clear_connection(conn_key)

    def _on_replay_kill(self, conn: "RRConnection", monster) -> None:
        """Finalize a replayed kill through the existing death pipeline.

        Suppressed when telemetry is the kill authority: the replay can't match
        the client's kill timing (§6-LIVE.6) so it killed mobs early and dropped
        loot prematurely. With telemetry on, the client hook reports the real
        kill (notify_client_kill) and this replay path only logs.
        """
        eid = getattr(monster, "entity_id", None)
        if self._telemetry_authoritative:
            log.debug(f"[REPLAY-DIAG] replay kill eid={eid} "
                      f"'{getattr(monster, 'label', '?')}' SUPPRESSED — telemetry authoritative")
            return
        log.info(f"[REPLAY-DIAG] KILL '{getattr(conn, 'login_name', '?')}' "
                 f"eid={eid} '{getattr(monster, 'label', '?')}'")
        if eid is not None and eid in self._monsters:
            self._process_monster_kill(eid, conn)

    # ── Monster registration ────────────────────────────────────────────────

    def register_monster(self, entity_id: int, gc_type: str, label: str,
                          hp_wire: int, level: int, difficulty: str,
                          zone_gc_type: str, pos_x: float, pos_y: float, pos_z: float,
                          treasure_generators: List[tuple[str, int]] = None,
                          behavior_id: int = 0, attack_range: float = 8.0,
                          defense_rating: float = 0.0,
                          instance_id: int = 0, entity_gc_type: str = "") -> None:
        """Register a newly spawned monster for HP tracking."""
        self._monsters[entity_id] = TrackedMonster(
            entity_id=entity_id, gc_type=gc_type, label=label,
            current_hp=hp_wire, max_hp=hp_wire,
            level=level, difficulty=difficulty,
            zone_gc_type=zone_gc_type,
            instance_id=instance_id,
            entity_gc_type=entity_gc_type or gc_type,
            pos_x=pos_x, pos_y=pos_y, pos_z=pos_z,
            spawn_time=time.time(),
            treasure_generators=treasure_generators or [],
            behavior_id=behavior_id,
            spawn_x=pos_x, spawn_y=pos_y,
            attack_range=attack_range,
            defense_rating=defense_rating,
        )
        log.debug(f"[Combat] registered monster eid={entity_id} '{label}' hp={hp_wire}")

    def unregister_monster(self, entity_id: int) -> None:
        """Drop a monster's tracked state (called on instance teardown)."""
        if self._monsters.pop(entity_id, None) is not None:
            from . import mob_engagement_relay
            mob_engagement_relay.purge_mob(entity_id)
            log.debug(f"[Combat] unregistered monster eid={entity_id}")

    def get_monster(self, entity_id: int) -> Optional[TrackedMonster]:
        return self._monsters.get(entity_id)

    def is_monster(self, entity_id: int) -> bool:
        return entity_id in self._monsters

    def notify_client_kill(self, entity_id: int, killer_conn: "RRConnection",
                           death_pos: Optional[tuple] = None,
                           level_synced: bool = False) -> bool:
        """Public entry for a client-reported kill (telemetry channel).

        The client hook (client_hook/) reports the exact local death — the
        ground truth the native protocol omits (bible §6-LIVE.4, packet-blind)
        and the server cannot reproduce (§6-LIVE.6). Reporting it here lets the
        existing authoritative reward path fire at the moment the client kills
        the mob, so loot/XP land and the despawn timing finally matches (no more
        server-replay killing it early). Returns True if a tracked mob was
        processed.

        ``death_pos`` (world-float ``(x, y, z)``) is the mob's real death spot,
        read from the client entity by the hook (KILL_AT). When present, loot
        drops there instead of at the server's stale spawn-anchor / killer pos.

        ``level_synced`` — the telemetry layer already snapped the character to
        the client's reported level, so the server-side XP grant is suppressed
        (it would otherwise risk the server level LEADING the client, which the
        zero-tolerance HP synch compare crashes on).
        """
        if entity_id not in self._monsters:
            return False
        self._process_monster_kill(entity_id, killer_conn, death_pos=death_pos,
                                   level_synced=level_synced)
        return True

    # ── Combat message handling ──────────────────────────────────────────────

    def handle_action_dispatch(self, conn: "RRConnection", reader: LEReader,
                                component_id: int) -> bool:
        """Handle 0x35/0x01 Action dispatch from client.

        Format: [responseId:1] [actionType:1] [sessionID:1] [targetEntityID:2]

        Returns True if the message was consumed.
        """
        if reader.remaining < 5:
            return False

        response_id = reader.read_byte()
        action_type = reader.read_byte()
        session_id = reader.read_byte()
        target_entity_id = reader.read_uint16()

        use_flags = 0
        actual_target_id = target_entity_id

        if action_type == 0x50:   # BehaviourActionUseTarget (melee attack / targeted spell)
            use_flags = target_entity_id & 0xFF
            if reader.remaining >= 1:
                high_byte = reader.read_byte()
                actual_target_id = (target_entity_id >> 8) | (high_byte << 8)

            # Track weapon cycles for melee (useFlags < 100).
            if use_flags < 100:
                log.debug(f"[Combat] melee attack conn={conn.conn_id} useFlags={use_flags} target={actual_target_id}")

        elif action_type == 0x51:   # BehaviourActionUsePosition (position-targeted spell)
            # Read 12 more bytes of position data (3 int32 = posX, posY, posZ).
            if reader.remaining >= 12:
                pos_x = reader.read_int32()
                pos_y = reader.read_int32()
                pos_z = reader.read_int32()
                log.debug(f"[Combat] spell cast conn={conn.conn_id} pos=({pos_x},{pos_y},{pos_z})")

        elif action_type == 0x06:   # BehaviourActionActivate (NPC/portal/chest click)
            log.debug(f"[Combat] activate conn={conn.conn_id} target={target_entity_id}")

        # Echo ActionResponse back to client.
        self._send_action_response(conn, component_id, response_id, action_type,
                                    session_id, use_flags, actual_target_id)
        return True

    def handle_hp_sync(self, conn: "RRConnection", reader: LEReader,
                        source: str = "HP-SYNC") -> bool:
        """Handle 0x36 EntitySyncHP or 0x03 SendUpdate HP sync.

        Format: [entityId:2] [flags:1] [if flags&0x02: clientHP:4]

        Returns True if processed.
        """
        if reader.remaining < 3:
            return False

        entity_id = reader.read_uint16()
        flags = reader.read_byte()

        if flags & 0x02 and reader.remaining >= 4:
            client_hp = reader.read_uint32()
        else:
            return True  # no HP data, just flags

        # ── Own-avatar HP: adopt the client's self-reported value ────────────
        # The vanilla client is authoritative for its OWN avatar HP — it
        # self-levels on kills and recomputes HP locally (e.g. L1 68096 -> L2
        # 72192), then reports that value here via the 0x36/0x03 entity-synch
        # suffix. The server must trust it and feed it back, so every outbound
        # 0x02 trailer (per-tick 0x36, SpawnAction, FollowClient, MoverUpdate)
        # matches the client's local synch field; otherwise the client's
        # processComponentUpdate compare (FUN_005dd900) mismatches and fatally
        # crashes the Avatar on dungeon zones (exit 0xc000013a). Mirrors C#
        # UnityGameServer.ObserveClientPlayerHP / Networking/Sync/HpSyncService
        # (which adopts the client report into the outbound SynchHP).
        avatar = getattr(conn, "avatar", None)
        if avatar is not None and entity_id == getattr(avatar, "id", -1):
            self.adopt_client_avatar_hp(conn, client_hp, source)
            return True

        self.observe_monster_hp(conn, entity_id, client_hp, source)
        return True

    def adopt_client_avatar_hp(self, conn: "RRConnection", client_hp: int,
                               source: str) -> bool:
        """Adopt the client's self-reported avatar HP into ``conn.hp_wire``.

        The vanilla client is authoritative for its OWN avatar HP — it self-sims
        damage and self-levels on kills, recomputing HP locally (e.g. L1 68096 ->
        L2 72192), and reports the result as an EntitySynchInfo (``flags & 0x02``
        + HP). The server must trust it and echo it back, so every outbound
        ``0x02`` trailer (per-tick ``0x36``, SpawnAction, FollowClient,
        MoverUpdate, action acks) matches the client's local synch field;
        otherwise the client's ``processUpdateComponent`` compare
        (``FUN_005dd900``) mismatches [Local damaged] vs [Remote MaxHP] and fatally
        crashes the Avatar on dungeon zones (exit 0xc000013a). Mirrors C#
        ``UnityGameServer.ObserveClientPlayerHP`` / ``HpSyncService`` (which adopt
        the client report into the outbound SynchHP).

        Values of 0 (death — handled via respawn) and >= the engine MP/no-data
        sentinel are garbage and rejected. Shared by the standalone 0x36/0x03
        sub-message path and the trailing-EntitySynchInfo scan on routine
        movement/action packets (``net.movement._read_trailing_avatar_hp``).

        Returns True iff the value was adopted.
        """
        adopt = 0 < client_hp < _AVATAR_HP_SENTINEL_WIRE
        # [CLIENT-REPORT-FREQ] — fires on EVERY own-avatar report (adopted or
        # not) so a live trace confirms the load-bearing assumption: that the
        # client autonomously reports its own avatar HP often enough for the
        # adopt-and-echo fix to keep the synch trailers matched.
        log.info(f"[CLIENT-REPORT-FREQ] {source} own-avatar "
                 f"reportedHP={client_hp} {'ADOPT' if adopt else 'REJECT'} "
                 f"prev_hp_wire={conn.hp_wire} for '{getattr(conn, 'login_name', '?')}'")
        if adopt:
            conn.hp_wire = client_hp
            # Remember it so a subsequent level-up refresh keeps this damaged
            # value instead of clobbering hp_wire up to the new level max
            # (game_server._refresh_avatar_hp_wire / resolve_synch_hp_wire).
            conn.client_hp_wire = client_hp
            # Party frame: fan the member's HP bar (ch-9 0x4B, throttled inside)
            # to the group. UI-channel only — mobs hitting P1 become VISIBLE to
            # P2 here; the entity-level trailer stays untouched (bible §6).
            groups = getattr(self._server, "groups", None)
            if groups is not None:
                try:
                    groups.on_member_hp(conn)
                except Exception as ex:  # noqa: BLE001 — UI must never break adoption
                    log.debug(f"[GROUP] health push failed: {ex}")
        return adopt

    def observe_monster_hp(self, conn: "RRConnection", entity_id: int,
                           client_hp: int, source: str) -> bool:
        """Feed a client-reported monster HP value into the kill pipeline.

        Shared by the 0x36/0x03 EntitySyncHP path (``handle_hp_sync``) and the
        0x50 swing-suffix path (``net.movement`` action dispatch). Lowers the
        tracked HP, broadcasts the change, and fires the kill -> XP -> level-up
        chain when the monster drops to <= 1 HP (256 in wire fixed-point).

        Returns True if ``entity_id`` is a tracked monster (whether or not it
        died this call); False if it is unknown to this manager.
        """
        monster = self._monsters.get(entity_id)
        if monster is None:
            return False  # not a tracked monster

        # Update server-tracked HP from client report (monotonic-down).
        if client_hp < monster.current_hp:
            monster.current_hp = client_hp
            log.debug(f"[Combat] {source} eid={entity_id} hp={client_hp}/{monster.max_hp}")

            # Broadcast HP change to all zone players.
            from . import hp_broadcast
            hp_broadcast.broadcast_hp_sync(
                self._server, conn, entity_id, monster.current_hp, monster.max_hp)

        # Check for death.
        if client_hp <= 256 and not monster.pending_kill:  # <= 1 HP in wire format
            monster.pending_kill = True
            monster.kill_confirmed_at = time.time()
            log.info(f"[Combat] monster death detected: eid={entity_id} '{monster.label}' "
                     f"(hp={client_hp}, source={source})")
            self._process_monster_kill(entity_id, conn)

        return True

    def handle_combat_tick(self, conn: "RRConnection", reader: LEReader) -> bool:
        """Handle 0x08 CombatTick — client reports damage per tick.
        Format: [sub:1] [size:2] [damage:int32:4]
        Logged only — client is authoritative.
        """
        if reader.remaining < 7:
            return False
        sub = reader.read_byte()
        size = reader.read_uint16()
        damage = reader.read_int32()
        log.debug(f"[Combat] combat_tick conn={conn.conn_id} sub={sub} damage={damage}")
        return True

    def handle_aggro(self, conn: "RRConnection", reader: LEReader) -> bool:
        """Handle 0x09 Aggro — client reports aggro state.
        Format: [entityId:2] [aggroLevel:1]
        Logged only.
        """
        if reader.remaining < 3:
            return False
        entity_id = reader.read_uint16()
        aggro_level = reader.read_byte()
        log.debug(f"[Combat] aggro conn={conn.conn_id} eid={entity_id} level={aggro_level}")
        return True

    def seed_room_rng(self, seed: int) -> None:
        """Seed the server's combat replay RNG with the stable room seed.

        Faithful port of C# ``CombatManager.InitializeRoomRng`` (cs:1791): the
        SERVER picks a per-zone-instance seed and sends it to the client ONCE at
        zone-connect via opcode ``0x0C`` (``UnityGameServer.SendRandomSeed``).
        The client seeds its room/combat RNG from that same value, so the server
        must seed its own replay RNG identically to share the stream.

        NB: the prior Python port had NO stable seed and instead reseeded the
        client every 33ms tick with a time-based value — that perturbed the
        client's local hit/miss/crit rolls every tick and made replay parity
        impossible. The seed is now stable for the room (== the maze/layout seed,
        the same value that drives the procedural spawn placement on both sides).
        """
        self._rng_seed = seed & 0xFFFFFFFF
        self._combat_rng.seed(self._rng_seed)
        log.info(f"[ROOM-RNG] seeded combat replay RNG wire=0x{self._rng_seed:08X}")

    def set_rng_seed(self, reader: LEReader) -> None:
        """Handle an inbound 0x0C RNG seed (legacy/unused — the vanilla client
        is packet-proven to never send 0x0C; the server is authoritative)."""
        if reader.remaining >= 4:
            self.seed_room_rng(reader.read_uint32())

    # ── Action response echo ─────────────────────────────────────────────────

    def _send_action_response(self, conn: "RRConnection", component_id: int,
                               response_id: int, action_type: int, session_id: int,
                               use_flags: int, target_id: int) -> None:
        """Echo ActionResponse packet back to the originating client.

        This is REQUIRED — without it the client's combat UI/animations stall.
        Wire format: 0x07 0x35 compId(2) 0x01 respId(1) actionType(1)
                     sessionId(1) useFlags(1) targetId(2) syncFlags(1) syncHP(4) 0x06
        """
        w = LEWriter()
        w.write_byte(0x07)                   # BeginStream
        w.write_byte(0x35)                   # ComponentUpdate
        w.write_uint16(component_id)
        w.write_byte(0x01)                   # ActionResponse sub-message
        w.write_byte(response_id)
        w.write_byte(action_type)
        w.write_byte(session_id)
        w.write_byte(use_flags)
        w.write_uint16(target_id)
        w.write_byte(0x00)                   # syncFlags
        w.write_uint32(0xFFFFFFFF)           # syncHP (max = full HP)
        w.write_byte(0x06)                   # EndStream
        conn.send_to_client(w.to_array())

    # ── Death pipeline ───────────────────────────────────────────────────────

    def _process_monster_kill(self, entity_id: int, killer_conn: "RRConnection",
                              death_pos: Optional[tuple] = None,
                              level_synced: bool = False) -> None:
        """Handle monster death: broadcast despawn, generate loot, credit quest."""
        monster = self._monsters.get(entity_id)
        if monster is None:
            return

        log.info(f"[Combat] processing kill: eid={entity_id} '{monster.label}'")

        # 0. Purge the mob from instance tracking and drop any chase/follow
        #    packets still queued for it BEFORE the destroy goes out, so no
        #    stale 0x35 update for its (about-to-be-destroyed) behavior
        #    component reaches the client after the 0x05 — the Code-9
        #    "Invalid ComponentID" race (DR_MONSTER_AI). No-op in the
        #    deferred-enroll default (nothing queued for it).
        from . import monster_ai
        monster_ai.purge_monster(
            self._server, getattr(self._server, "world_instances", None),
            entity_id, monster.behavior_id)

        # Drop the mob's multiplayer engagement state (who was fighting it, the
        # relay throttle window) so a recycled entity id never inherits a stale
        # "already engaged" set or throttle anchor (mob_engagement_relay).
        from . import mob_engagement_relay
        mob_engagement_relay.purge_mob(entity_id)

        # 1. Broadcast despawn to the players in the mob's zone INSTANCE only.
        self._broadcast_despawn(entity_id, monster.zone_gc_type,
                                monster.instance_id, killer_conn)

        # 2. Generate loot (at the client-reported death spot when available).
        #    Only while the killer is still inside the mob's instance: the loot
        #    fan-out is keyed off the killer's CURRENT (zone, instance), so a
        #    late kill (deferred telemetry) landing after a zone change would
        #    drop the bag in the killer's new zone at stale coordinates.
        if (killer_conn.current_zone_gc_type == monster.zone_gc_type
                and getattr(killer_conn, "instance_id", 0) == monster.instance_id):
            self._generate_loot(monster, killer_conn, death_pos=death_pos)
        else:
            log.info(f"[Combat] killer '{getattr(killer_conn, 'login_name', '?')}' "
                     f"left instance ({monster.zone_gc_type}, {monster.instance_id}) "
                     f"before the kill landed — skipping loot for eid={entity_id}")

        # 3. Quest credit.
        if self._server.quests is not None:
            self._server.quests.on_creature_killed(killer_conn, monster.gc_type)

        # 3b. Server-authoritative XP. Two modes:
        #   • legacy / no-level kill (level_synced=False): full server-side XP +
        #     level-up (award_kill_xp) keeps the level in lockstep with the
        #     client's local self-leveling so the avatar HP synch trailer never
        #     goes stale (the level-up Avatar synch crash).
        #   • telemetry-snap kill (level_synced=True): the client's KILL_AT snap
        #     already owns the level, so the server must NOT level itself (leading
        #     the client crashes the synch compare just as hard as lagging). It
        #     still ACCRUES experience (capped at the snapped level) so the stored
        #     value tracks the client and the zone-transfer Avatar re-send stops
        #     clobbering the client's locally-earned XP (the "XP lost going to
        #     town" report).
        if level_synced:
            self._server.accrue_kill_xp(killer_conn, monster.level)
        else:
            self._server.award_kill_xp(killer_conn, monster.level)

        # 3c. Boss exit-gate: open any Door the mob's content lists in
        #     DoorsToOpenOnDeath (RattleTooth → "Boss00ExitGate"). Instance-scoped
        #     broadcast; late joiners get it on entry. Wrapped so a content-read
        #     hiccup never breaks the kill pipeline (loot/XP already fired).
        try:
            from . import world_entities as we_module
            we_module.open_boss_doors(self._server, monster)
        except Exception as ex:   # pragma: no cover - defensive
            log.warn(f"[Combat] boss-door open failed for eid={entity_id}: {ex}")

        # 4. Clean up tracking.
        del self._monsters[entity_id]
        self._last_diag_hp.pop(entity_id, None)

    def _broadcast_despawn(self, entity_id: int, zone_gc_type: str,
                            instance_id: int,
                            killer_conn: "RRConnection") -> None:
        """Send entity destroy (0x05) to instance players that DISPLAY the mob.

        Instance-scoped, never zone-scoped: another player soloing his OWN copy
        of the same zone has a disjoint entity set, and a destroy for an eid his
        client never saw is "Invalid EntityID" → Zone communication error Code 6
        (live user report 2026-07-08 — P1's kill crashed P2 in a separate
        private instance of the same dungeon).

        bible §6-LIVE.7/.8: a client that SIMULATES the mob (enrolled it into its
        client AI) kills and despawns it on its OWN cadence. The server's
        replay-based kill timing CANNOT match the client (§6-LIVE.6), so sending
        it a 0x05 destroy makes the mob vanish *before the player kills it* (the
        2026-06-15 "mobs die from server + despawn before I kill them" report).
        Skip the simulators; they purge the corpse themselves. Loot/XP still fire
        server-side (best-effort timing).

        Death animation: the KILLER landed the final blow locally (its own
        Damage::apply zeroed the mob), so it plays the death anim → corpse → fade
        itself — an immediate 0x05 pops the mob out of existence instead. So the
        killer's destroy is DEFERRED (:data:`_DEATH_DESPAWN_DELAY`) as invisible
        cleanup after the local death sequence; other displayers get it now.
        """
        monster = self._monsters.get(entity_id)
        simulated_by = set(getattr(monster, "simulated_by", None) or ())

        w = LEWriter()
        w.write_byte(0x07)                   # BeginStream
        w.write_byte(0x05)                   # Destroy entity
        w.write_uint16(entity_id)
        w.write_byte(0x06)                   # EndStream
        despawn_packet = w.to_array()

        killer_id = getattr(killer_conn, "conn_id", None)
        immediate: List["RRConnection"] = []
        deferred: List["RRConnection"] = []
        for other in list(self._server.connections.values()):
            if not other.is_spawned:
                continue
            if other.current_zone_gc_type != zone_gc_type:
                continue
            if getattr(other, "instance_id", 0) != instance_id:
                continue
            if other.conn_id in simulated_by:
                continue          # this client simulates the mob — it despawns it itself
            (deferred if other.conn_id == killer_id else immediate).append(other)

        for other in immediate:
            other.send_to_client(despawn_packet)
        for other in deferred:
            self._defer_despawn(other, despawn_packet, zone_gc_type, instance_id)

        log.info(f"[Combat] despawn eid={entity_id} now={len(immediate)} "
                 f"deferred={len(deferred)} (skipped {len(simulated_by)} simulator(s))")

    def _defer_despawn(self, conn: "RRConnection", despawn_packet: bytes,
                       zone_gc_type: str, instance_id: int) -> None:
        """Send ``conn`` the entity-destroy after :data:`_DEATH_DESPAWN_DELAY`, so
        its client-local death anim/corpse/fade finishes first. Re-validates the
        conn at fire time (still spawned + same zone INSTANCE) so a stale destroy
        can't hit a player who has since left — or re-entered a different copy of
        the same zone, where the eid is unknown (Code 6). Falls back to an
        immediate send when no event loop is running (unit tests)."""
        import asyncio

        def _fire() -> None:
            if getattr(conn, "is_spawned", False) and \
                    getattr(conn, "current_zone_gc_type", None) == zone_gc_type and \
                    getattr(conn, "instance_id", 0) == instance_id:
                conn.send_to_client(despawn_packet)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and _DEATH_DESPAWN_DELAY > 0:
            loop.call_later(_DEATH_DESPAWN_DELAY, _fire)
        else:
            _fire()

    def _generate_loot(self, monster: TrackedMonster,
                        killer_conn: "RRConnection",
                        death_pos: Optional[tuple] = None) -> None:
        """Generate loot from a killed monster and spawn it on the ground.

        Combat is client-authoritative: a mob usually aggros and dies entirely on
        the killing client, so the server's tracked ``monster.pos`` is still the
        SPAWN anchor — dropping loot there put it across the room from the kill.

        Best source, in order:
          1. ``death_pos`` — the mob's real death spot, reported by the client
             hook (KILL_AT). Exact; use it verbatim — but ONLY when it is a real
             position. The hook reads the mob transform at the killing blow and
             sometimes returns the world origin (0,0,0) (the mob's transform is
             not readable there — the +0x130 offset was RE'd on the avatar). No DR
             zone has loot at the origin, so an origin report is rejected as
             invalid and we fall through; otherwise loot drops miles from the
             player and looks like "nothing dropped".
          2. ``monster.pos`` — only trustworthy when the SERVER drove the chase
             (``monster_ai`` moved pos off the spawn anchor).
          3. the killer's current position — the mob died next to them.
        """
        from . import loot

        if death_pos is not None and (abs(death_pos[0]) > 1.0
                                      or abs(death_pos[1]) > 1.0):
            px, py, pz = death_pos
        else:
            px, py, pz = monster.pos_x, monster.pos_y, monster.pos_z
            moved = (abs(px - monster.spawn_x) > 1.0
                     or abs(py - monster.spawn_y) > 1.0)
            if not moved:
                px = getattr(killer_conn, "player_pos_x", px)
                py = getattr(killer_conn, "player_pos_y", py)
                pz = getattr(killer_conn, "player_pos_z", pz)

        loot.generate_loot_for_monster(
            self._server, killer_conn,
            pos_x=px, pos_y=py, pos_z=pz,
            level=monster.level,
            treasure_generators=monster.treasure_generators,
            difficulty=getattr(monster, "difficulty", ""),
        )
