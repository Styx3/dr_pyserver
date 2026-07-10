"""Per-instance authoritative world state.

The single-player C# server (and the first Python port) spawned NPCs, monsters
and world entities on *every* player's zone-join and broadcast the create packets
to everyone already in the zone — so a second player duplicated the whole world
for existing players, while the joiner never received the entities that were
already there.

This module fixes that the way a real multiplayer server must:

  * Each ``(zone_id, instance_id)`` owns ONE authoritative set of entities.
  * Entities are built **once**, lazily, when the first player enters the
    instance, with **stable, globally-unique** ids from ``server.allocate_entity_id``.
  * On join, the stored snapshot is sent to the **joining connection only**.
  * When the last player leaves, the instance is torn down (combat state
    unregistered) so it repopulates cleanly next time.

"Will it work for 2 players? for 5?" — yes: every joiner gets the same stable
entity ids, nobody re-creates anything, and avatars are exchanged separately by
``net.spawn.exchange_player_spawns``.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core import log

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection

InstanceKey = Tuple[int, int]  # (zone_id, instance_id)

# Per-instance world tick cadence — mirrors ``net.movement.TICK_INTERVAL``
# (33 ms ≈ 30 Hz, the client's world-clock rate). Kept as a local constant so
# the managers layer never imports the net layer (the dependency runs net →
# managers, not the reverse).
INSTANCE_TICK_INTERVAL = 0.033
_MERCHANT_FLUSH_EVERY = 30   # ticks (~1 s) — matches the old per-conn watchdog


@dataclass
class ZoneInstance:
    """One live copy of a zone: its authoritative entities + a populate flag."""

    key: InstanceKey
    zone_id: int
    instance_id: int
    zone_name: str = ""
    zone_gc_type: str = ""
    # Built-once create streams (each a full BeginStream..EndStream packet) and
    # their entity ids, kept in spawn order so the snapshot is deterministic.
    entity_packets: List[bytes] = field(default_factory=list)
    entity_ids: List[int] = field(default_factory=list)
    monster_ids: List[int] = field(default_factory=list)
    # Vendor components in this instance: (merchant component id, npc gc_type).
    # Dynamic shop stock is pushed per joiner from these (the cached NPC stream
    # ships dynamic tabs empty).
    merchant_components: List[Tuple[int, str]] = field(default_factory=list)
    populated: bool = False
    # The per-instance world tick task (mob AI + member merchant flush). Runs
    # independent of any single player's connection loop; started on first
    # populate, cancelled on teardown. None when no event loop is running
    # (unit tests / sync context). Excluded from equality/repr.
    tick_task: Optional["asyncio.Task"] = field(
        default=None, compare=False, repr=False)

    def add(self, built: List[Tuple[int, bytes]], *, monsters: bool = False) -> None:
        for entity_id, packet in built:
            self.entity_ids.append(entity_id)
            self.entity_packets.append(packet)
            if monsters:
                self.monster_ids.append(entity_id)

    @property
    def pathmap_key(self) -> str:
        """Per-instance geometry pathmap key (matches the ``_inst`` suffix the
        :class:`~drserver.managers.pathmap.PathMapManager` keys instance maps on
        so 2+ players in this instance share one generated map)."""
        return f"{self.zone_name}_inst{self.instance_id}"


class WorldInstanceRegistry:
    """Holds every live ``ZoneInstance`` and drives spawn-once / snapshot / teardown."""

    def __init__(self) -> None:
        self._instances: Dict[InstanceKey, ZoneInstance] = {}

    @staticmethod
    def key_for(conn: "RRConnection") -> InstanceKey:
        return (conn.current_zone_id, conn.instance_id)

    def get_or_create(self, conn: "RRConnection") -> ZoneInstance:
        key = self.key_for(conn)
        inst = self._instances.get(key)
        if inst is None:
            inst = ZoneInstance(
                key=key,
                zone_id=conn.current_zone_id,
                instance_id=conn.instance_id,
                zone_name=conn.current_zone_name or "",
                zone_gc_type=conn.current_zone_gc_type or "",
            )
            self._instances[key] = inst
            log.info(f"[INSTANCE] created {key} zone='{inst.zone_name}' gc='{inst.zone_gc_type}'")
        return inst

    def find(self, zone_gc_type: str, instance_id: int) -> Optional[ZoneInstance]:
        """The live instance for a ``(zone_gc_type, instance_id)`` pair, or None.

        Combat resolves the boss's instance this way (it holds the mob's
        ``zone_gc_type`` + ``instance_id``, not the ``zone_id`` the registry keys
        on) to open its exit gates on death."""
        for inst in self._instances.values():
            if inst.zone_gc_type == zone_gc_type and inst.instance_id == instance_id:
                return inst
        return None

    # ── Join ───────────────────────────────────────────────────────────────
    def enter(self, server: "GameServer", conn: "RRConnection") -> None:
        """Populate the instance on first entry, then send the snapshot to ``conn`` only."""
        inst = self.get_or_create(conn)
        if not inst.populated:
            self._populate(server, conn, inst)
            inst.populated = True
        self._send_snapshot(conn, inst)
        # Boss exit-gate: if this instance's boss died before the player joined,
        # its gate was opened but the just-sent snapshot spawned it closed — tell
        # this joiner it is open (DoorsToOpenOnDeath). No-op when nothing opened.
        from . import world_entities as we_module
        we_module.reopen_gates_for_joiner(conn, inst.entity_ids)
        # Vendor stock: regenerate for this player's level and push the dynamic
        # tab contents (0x35/0x1E adds) — the snapshot's merchant init shipped
        # those tabs empty.
        if inst.merchant_components:
            from .merchants import merchant_manager
            merchant_manager.send_zone_stock(server, conn,
                                             inst.merchant_components)
        # Drive the instance's player-independent world tick (mob AI + member
        # merchant flush). Idempotent — a second joiner reuses the running task.
        self.start_instance_tick(server, inst)

    def _populate(self, server: "GameServer", conn: "RRConnection", inst: ZoneInstance) -> None:
        from . import npcs as npc_module
        from . import monsters as monster_module
        from . import world_entities as we_module
        from . import portals as portal_module
        from . import checkpoints as checkpoint_module
        from . import dungeon_spawner

        zone_name = conn.current_zone_name or ""
        zone_lower = zone_name.lower()
        zone_gc = conn.current_zone_gc_type

        # TEMP ISOLATION HARNESS (warp sync-error debug). Set DR_SKIP to a
        # comma list of categories to suppress: monsters,npcs,world,portals,
        # checkpoints. e.g. DR_SKIP=monsters,world,portals leaves the dungeon
        # with only the player (+ checkpoints/npcs if any) so we can tell
        # whether the synch error comes from a spawned entity or from the
        # player spawn/respawn/tick itself. DR_NO_MONSTERS=1 kept as an alias.
        # Remove this harness once the synch root cause is found.
        import os as _os
        _skip = {s.strip() for s in _os.environ.get("DR_SKIP", "").split(",") if s.strip()}
        if _os.environ.get("DR_NO_MONSTERS") == "1":
            _skip.add("monsters")
        if _skip:
            log.warn(f"[INSTANCE] DR_SKIP active {sorted(_skip)} for zone "
                     f"{zone_name!r} (isolation test)")

        # Monsters. Procedural dungeon levels generate from the maze seed so mobs
        # land in the rooms the client renders; fixed zones (boss arenas) load
        # their hand-authored spawns; public zones (town/tutorial) have none.
        if "monsters" in _skip:
            pass
        elif dungeon_spawner.is_procedural_zone(zone_name):
            # Build + register the per-instance geometry pathmap (keyed so 2+
            # players share it) and place mobs on it. seed defaults to the
            # zone-name layout seed, matching the value game_server puts in the
            # 13/0x00 zone-connect packet so the client renders the same maze.
            spawns = dungeon_spawner.generate_spawns(
                zone_name, instance_key=inst.pathmap_key)
            tuples = [(s.gc_type, s.creature_gc_type,
                       s.pos_x, s.pos_y, s.pos_z, s.heading)
                      for s in spawns]
            built = monster_module.build_monsters_from_spawns(
                server, zone_name, zone_gc, tuples,
                instance_id=inst.instance_id)
            inst.add(built, monsters=True)
        elif "town" not in zone_lower and "tutorial" not in zone_lower:
            # Static (non-maze) worlds — boss arenas, lobbies, quest off-shoots —
            # spawn at their authored ``base.Encounter`` markers (data-driven from
            # the client ``*.world`` content); the legacy hand-authored
            # ``dungeon_spawns`` table remains as an override/fallback source.
            static = (dungeon_spawner.generate_static_spawns(zone_name)
                      or dungeon_spawner.load_static_spawns(zone_name))
            if static:
                tuples = [(s.gc_type, s.creature_gc_type,
                           s.pos_x, s.pos_y, s.pos_z, s.heading)
                          for s in static]
                built = monster_module.build_monsters_from_spawns(
                    server, zone_name, zone_gc, tuples,
                    instance_id=inst.instance_id)
            else:
                # Legacy fallback: random placement near the first joiner.
                pos = (conn.player_pos_x, conn.player_pos_y, conn.player_pos_z)
                built = monster_module.build_zone_monsters(
                    server, zone_gc, pos, count=5, zone_name=zone_name,
                    instance_id=inst.instance_id)
            inst.add(built, monsters=True)

        # NPCs (stable per-zone definitions).
        if "npcs" not in _skip:
            inst.add(npc_module.build_zone_npcs(
                server, conn.current_zone_name or "",
                merchant_sink=inst.merchant_components))

        # World entities (chests, teleporters, shrines, gates).
        if "world" not in _skip:
            inst.add(we_module.build_zone_world_entities(server, conn.current_zone_name or ""))

        # Zone portals (teleport gates to the next zone).
        if "portals" not in _skip:
            inst.add(portal_module.build_zone_portals(server, conn.current_zone_name or ""))
            # Procedural dungeons have no static portal rows — their entrance/exit
            # warps are placed at the maze room-node cells (data-driven from
            # dungeon_room_nodes). The instance pathmap was registered above by the
            # monster spawner, so gates snap to floor Z.
            if dungeon_spawner.is_procedural_zone(zone_name):
                inst.add(portal_module.build_dungeon_warp_gates(
                    server, zone_name, inst.pathmap_key))

        # Waystone obelisks (checkpoint recall points).
        if "checkpoints" not in _skip:
            inst.add(checkpoint_module.build_zone_checkpoints(server, conn.current_zone_name or ""))

        log.info(f"[INSTANCE] populated {inst.key} with {len(inst.entity_ids)} entities "
                 f"({len(inst.monster_ids)} monsters)")

    @staticmethod
    def _send_snapshot(conn: "RRConnection", inst: ZoneInstance) -> None:
        for packet in inst.entity_packets:
            conn.send_to_client(packet)
        if inst.entity_packets:
            log.info(f"[INSTANCE] sent snapshot of {len(inst.entity_packets)} entities "
                     f"to '{conn.login_name}' in {inst.key}")

    # ── Per-instance world tick (player-independent) ─────────────────────────
    def start_instance_tick(self, server: "GameServer", inst: ZoneInstance) -> None:
        """Start the instance's world tick task if it isn't already running.

        The tick drives the SHARED, player-independent world logic for the
        instance — monster AI (aggro/chase intent) and the member merchant
        restock flush — once per instance rather than once per connected player.
        This replaces the old per-connection driving (where every player's loop
        re-ran the same shared mob AI, deduped by a timestamp guard) and fixes
        the 2+ player double-simulation.

        No-op when no asyncio event loop is running (unit tests / sync context):
        the tick is a live-server concern and its absence is harmless there.
        """
        if inst.tick_task is not None and not inst.tick_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        inst.tick_task = loop.create_task(self._instance_tick_loop(server, inst))

    async def _instance_tick_loop(self, server: "GameServer",
                                  inst: ZoneInstance) -> None:
        from . import monster_ai
        from .merchants import merchant_manager

        log.info(f"[INSTANCE-TICK] start {inst.key} zone='{inst.zone_name}'")
        ticks = 0
        try:
            # Run while this exact instance is still live (teardown pops the key
            # AND cancels this task; the membership check guards the cancel race).
            while self._instances.get(inst.key) is inst:
                now = time.monotonic()
                try:
                    monster_ai.tick_instance(server, self, inst, now)
                except Exception as ex:  # noqa: BLE001 — never kill the loop
                    log.error(f"[INSTANCE-TICK] AI step failed for {inst.key}: {ex}")

                ticks += 1
                if ticks % _MERCHANT_FLUSH_EVERY == 0:
                    self._flush_member_merchants(server, inst, merchant_manager)

                await asyncio.sleep(INSTANCE_TICK_INTERVAL)
        except asyncio.CancelledError:  # pragma: no cover
            pass
        finally:
            log.debug(f"[INSTANCE-TICK] stop {inst.key}")

    def _flush_member_merchants(self, server: "GameServer", inst: ZoneInstance,
                                merchant_manager) -> None:
        """Run the restock watchdog for each armed player in this instance.

        Merchant stock is per-player (generated at the player's level and pushed
        to that one connection), so this iterates the instance's members rather
        than holding shared state. The armed cid is per-zone, so a player who has
        left this instance is skipped (and was disarmed by ``_transfer_zone``)."""
        for conn in list(server.connections.values()):
            if not getattr(conn, "is_spawned", False):
                continue
            if self.key_for(conn) != inst.key:
                continue
            # Debounced post-buy refill (armed independently of the restock
            # watchdog — a single purchase doesn't arm active_merchant_npc).
            try:
                merchant_manager.flush_pending_buy_refill(conn)
            except Exception as ex:  # noqa: BLE001 — never kill the loop
                log.error(f"[INSTANCE-TICK] merchant refill failed for "
                          f"'{getattr(conn, 'login_name', '?')}': {ex}")
            if not getattr(conn, "active_merchant_npc", None):
                continue
            try:
                merchant_manager.flush_due_refresh(conn)
            except Exception as ex:  # noqa: BLE001 — never kill the loop
                log.error(f"[INSTANCE-TICK] merchant flush failed for "
                          f"'{getattr(conn, 'login_name', '?')}': {ex}")

    # ── Live spawn (admin /spawn debug command) ──────────────────────────────
    def spawn_monsters_live(self, server: "GameServer", conn: "RRConnection",
                            count: int) -> List[int]:
        """Build extra monsters, store them in the instance, and broadcast to
        everyone currently in it (so all players see the same new monsters)."""
        from . import monsters as monster_module

        inst = self.get_or_create(conn)
        pos = (conn.player_pos_x, conn.player_pos_y, conn.player_pos_z)
        built = monster_module.build_zone_monsters(
            server, conn.current_zone_gc_type, pos, count=count,
            zone_name=conn.current_zone_name or "",
            instance_id=inst.instance_id,
        )
        inst.add(built, monsters=True)
        for _eid, packet in built:
            self._broadcast(server, inst, packet)
        return [eid for eid, _ in built]

    # ── Deferred monster client-AI enrollment ───────────────────────────────
    def enroll_monsters(self, server: "GameServer", conn: "RRConnection") -> int:
        """Enroll every live monster in ``conn``'s instance into ``conn``'s
        client AI (send the deferred ``0x64`` burst to ``conn`` only).

        Monsters spawn passive (no ``0x64`` — see
        ``monsters.ENROLL_MONSTERS_AT_SPAWN``) so they can't damage the player
        during the avatar's unprotected zone-entry window. This wakes them once
        the player engages (their first attack blesses the avatar action with the
        local-input-authority bit), so the incoming counter-damage is safe.

        Per-client: the burst goes only to ``conn`` (the engaging player's
        client takes ownership). Returns the number of monsters enrolled.
        """
        from . import monsters as monster_module

        inst = self._instances.get(self.key_for(conn))
        if inst is None or not inst.monster_ids:
            return 0
        combat = getattr(server, "combat", None)
        if combat is None:
            return 0

        pairs: List[Tuple[int, int]] = []
        for eid in inst.monster_ids:
            mon = combat.get_monster(eid)
            if mon is None or getattr(mon, "pending_kill", False):
                continue
            behavior_id = getattr(mon, "behavior_id", 0)
            if not behavior_id:
                continue
            # Enroll each mob into this client's AI ONCE. This runs from the
            # 0x50 attack path, which fires on every swing — hit AND miss — so
            # without a gate every player attack re-sends the mob's 0x64
            # FollowClient block plus a stale HP synch. For a mob this client is
            # already simulating that is a disturbance, not a wake: it re-pokes
            # the client-side AI and re-asserts an HP the server no longer owns
            # (bible §6-LIVE.7 — the server must never originate an enrolled
            # mob's HP), so the mob visibly "changes behaviour / stops swinging
            # whenever the player attacks, even on a miss" (live 2026-07-06). The
            # enroll is a one-shot wake by design (the 0x50-path comment says
            # "the FIRST attack sends the burst"); skip mobs already simulated by
            # this client so the client brain runs uninterrupted afterward.
            sim = getattr(mon, "simulated_by", None)
            if sim is not None:
                if conn.conn_id in sim:
                    continue
                # This client now SIMULATES the mob (client AI owns it). Record it
                # so broadcast_monster_hp never sends it authoritative mob HP.
                sim.add(conn.conn_id)
            pairs.append((behavior_id, getattr(mon, "current_hp", 0)))

        packet = monster_module.build_monster_enroll_stream(pairs)
        if not packet:
            return 0
        conn.send_to_client(packet)
        log.info(f"[INSTANCE] enrolled {len(pairs)} monsters into "
                 f"'{conn.login_name}' AI in {inst.key}")
        return len(pairs)

    def _broadcast(self, server: "GameServer", inst: ZoneInstance, packet: bytes) -> None:
        for other in list(server.connections.values()):
            if not other.is_spawned:
                continue
            if self.key_for(other) != inst.key:
                continue
            other.send_to_client(packet)

    # ── Leave ─────────────────────────────────────────────────────────────
    def leave(self, server: "GameServer", conn: "RRConnection") -> None:
        """Drop the instance once its last player is gone."""
        key = self.key_for(conn)
        inst = self._instances.get(key)
        if inst is None:
            return
        still_present = any(
            other is not conn and other.is_spawned and self.key_for(other) == key
            for other in server.connections.values()
        )
        if still_present:
            return
        self._teardown(server, inst)

    def _teardown(self, server: "GameServer", inst: ZoneInstance) -> None:
        # Stop the instance world tick first so it can't fire against torn-down
        # entities / freed merchant cids.
        if inst.tick_task is not None and not inst.tick_task.done():
            inst.tick_task.cancel()
        inst.tick_task = None
        combat = getattr(server, "combat", None)
        if combat is not None:
            for mid in inst.monster_ids:
                unregister = getattr(combat, "unregister_monster", None)
                if unregister is not None:
                    unregister(mid)
        # Unregister this instance's vendor components.
        freed_cids = {cid for cid, _gc in inst.merchant_components}
        for merchant_cid in freed_cids:
            server.merchant_components.pop(merchant_cid, None)
        for eid in [eid for eid, cid in server.npc_merchant_cids.items()
                    if cid in freed_cids]:
            server.npc_merchant_cids.pop(eid, None)
        # Free the per-instance geometry pathmap (no-op if none was registered).
        from .pathmap import pathmap_manager
        pathmap_manager.unregister_instance(inst.pathmap_key)
        self._instances.pop(inst.key, None)
        log.info(f"[INSTANCE] torn down {inst.key} (empty)")


world_instance_registry = WorldInstanceRegistry()
