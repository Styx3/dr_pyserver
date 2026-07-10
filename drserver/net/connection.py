"""RRConnection — per-connection state for the game server.

Ported from RRConnection.cs. Carries auth identity, zone state, player position,
component ids, quest/dialog/portal state, and the outbound MessageQueue. The C#
TcpClient/NetworkStream are replaced by an asyncio StreamWriter; the read side is
driven by the server's connection handler.
"""
from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import Callable, Optional

from ..core import log
from . import framing

# Per-send wire tracing is extremely chatty (every tick of every connection emits
# one line at 30 Hz). Synchronous stdout writes block the asyncio event loop, so
# leaving this on under DEBUG visibly degrades movement smoothness over time.
# Opt in explicitly with DR_TRACE_SEND=1 when debugging the wire.
_TRACE_SEND = os.environ.get("DR_TRACE_SEND", "0").lower() in ("1", "true", "yes")


class MessageQueue:
    """FIFO of raw byte payloads flushed to the client in AfterTick."""

    def __init__(self):
        self._q: deque[bytes] = deque()

    def enqueue(self, data: bytes) -> None:
        self._q.append(data)

    def is_empty(self) -> bool:
        return len(self._q) == 0

    def dequeue_all(self) -> list[bytes]:
        out = list(self._q)
        self._q.clear()
        return out

    def remove_where(self, predicate: Callable[[bytes], bool]) -> int:
        """Drop every queued payload for which ``predicate(payload)`` is True.

        Returns the number removed. Used to purge stale per-entity updates
        (e.g. a dead mob's queued chase/follow packets) before its destroy is
        sent, closing the Code-9 'Invalid ComponentID' race.
        """
        kept = [p for p in self._q if not predicate(p)]
        removed = len(self._q) - len(kept)
        if removed:
            self._q.clear()
            self._q.extend(kept)
        return removed

    def clear(self) -> None:
        self._q.clear()

    @property
    def count(self) -> int:
        return len(self._q)


class RRConnection:
    def __init__(self, conn_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.conn_id = conn_id
        self.reader = reader
        self.writer = writer
        self.is_connected = True

        # ── Identity ──
        self.login_name: Optional[str] = None
        self.peer_id24: int = 0
        self.dll_session_token: int = 0

        # ── Zone ──
        self.current_zone_id: int = 0
        self.current_zone_gc_type: str = "world.town"
        self.current_zone_name: str = ""
        self.instance_id: int = 0
        self.zone_portal_source: str = ""

        # ── Player position / movement ──
        self.player_pos_x: float = 480.0
        self.player_pos_y: float = -191.0
        self.player_pos_z: float = 0.0
        self.player_heading: float = 0.0
        self.session_id: int = 0
        self.movement_generation: int = 0
        # Broadcast rate-limit gate (C# RRConnection.LastPositionUpdateTime): the
        # movement relay to OTHER players fires at most once per tick interval.
        self.last_position_update_time: float = 0.0
        self.update_number: int = 0

        # ── Pending local-player movement ack (C# QueueLocalPlayerMovementAck) ──
        # The owner's OWN movement is echoed back only on the 0x0D WorldInterval
        # tick (every 4th tick, ~132 ms), carrying the client's VERBATIM raw move
        # records + the same sessionId so the client dedupes against its local
        # prediction. Synthesizing a per-tick echo from the latest position (the
        # old 30 Hz behaviour) fought prediction → jitter + rubber-band.
        self.pending_local_move_session: int = 0
        self.pending_local_move_count: int = 0
        self.pending_local_move_data: bytes = b""
        self.pending_local_move_flush_at: float = 0.0

        # ── Fallback multiplayer relay state (C# BroadcastPlayerMovement) ──
        # Last raw move records seen, replayed once when the player stops so other
        # players see them halt at the right spot (catches pathfind arrival /
        # knockback the direct relay misses).
        self.last_raw_move_data: bytes = b""
        self.last_raw_move_count: int = 0
        self.stop_signal_sent: bool = False

        # ── Component ids ──
        self.unit_behavior_id: int = 0
        self.unit_container_id: int = 0
        self.modifiers_id: int = 0
        self.dialog_manager_id: int = 0
        self.quest_manager_id: int = 0
        self.skills_component_id: int = 0
        self.manipulators_component_id: int = 0
        self.modifiers_component_id: int = 0
        self.behavior_component_id: int = 0
        self.equipment_component_id: int = 0      # Phase 6 — equipment component

        # ── Skills usage (net.skills / managers.player_modifiers) ──
        # Session manipulator-slot → skill GC class map, built at spawn Op4
        # (the C# _playerManipMap); hotbar place/remove and 0x52 self-cast
        # buff resolution read it.
        self.skill_manip_map: dict[int, str] = {}
        # Tracked self-cast buff modifiers (gc_type.lower() → TrackedModifier)
        # re-sent after every zone-entry spawn (C# ModifierTracker).
        self.tracked_modifiers: dict = {}

        # ── Character ──
        self.avatar = None       # GCObject (Phase 3)
        self.player = None       # GCObject
        self.avatar_gc_type: str = ""
        self.class_name: str = "Fighter"
        self.player_level: int = 1
        self.char_sql_id: int = 0
        self.is_admin: bool = True

        # ── Merchant (vendor) restock state ──
        # Armed when the player clicks a vendor; the client's own restock
        # countdown emits an empty UnitContainer 0x22 at expiry, which flushes
        # fresh stock for this vendor (managers/merchants.py).
        self.active_merchant_npc: Optional[str] = None
        self.active_merchant_cid: int = 0
        self.active_merchant_due: float = 0.0
        # Debounced post-buy refill: armed/extended on each dynamic-tab purchase,
        # flushed ~1s after buying stops so freed space accumulates (one batched
        # top-up with varied items, instead of re-spawning the same archetype).
        self.merchant_refill_npc: Optional[str] = None
        self.merchant_refill_cid: int = 0
        self.merchant_refill_due: float = 0.0

        # ── Quest/dialog state ──
        self.current_dialog_npc_id: Optional[str] = None
        self.next_quest_instance_id: int = 1
        self.pending_quest_hash: int = 0
        self.pending_quest_npc_entity_id: int = 0
        self.viewing_quest_instance_id: int = 0
        self.pending_turn_in_instance_id: int = 0
        self.is_abandon_confirmed: bool = False
        self.abandon_click_count: int = 0
        # The instance whose turn-in dialog is currently DISPLAYED (0 = none), and
        # the instance whose turn-in dialog was just CLOSED without confirming.
        # Together they distinguish a genuine Abandon (0x03) from the client's
        # dialog-teardown 0x03 that follows every turn-in-dialog close — see the
        # 0x02/0x03 handlers (live x64dbg capture 2026-07-02, bug #9).
        self.turn_in_dialog_instance_id: int = 0
        self.dialog_teardown_instance_id: int = 0

        # ── Spawn ──
        # Position the next zone-entry avatar spawn should use (resolved at
        # zone-transfer: explicit recall pos > named waypoint > procedural maze
        # entry > zone default). ``has_pending_spawn`` gates it so the initial
        # tutorial join (which sets none) still falls back to the zone default.
        self.pending_spawn_x: float = 0.0
        self.pending_spawn_y: float = 0.0
        self.pending_spawn_z: float = 0.0
        self.pending_spawn_heading: float = 0.0
        self.has_pending_spawn: bool = False
        self.is_spawned: bool = False
        self.allow_flush: bool = False
        self.group_connected_sent: bool = False

        # ── Combat ──
        self.hp_wire: int = 0  # set by _send_post_spawn_packets, used by tick loop
        # Last avatar HP the client self-reported (trailing EntitySynchInfo). The
        # client owns its own HP, so the level-up refresh echoes this damaged
        # value instead of clobbering hp_wire back up to the level max (see
        # data.player_state.resolve_synch_hp_wire). None until the first report;
        # cleared on spawn/respawn/zone (fresh full HP).
        self.client_hp_wire: "int | None" = None

        # ── Inventory slot-map + cursor (active item) ──
        # Session source of truth for inventory item slot ids and the held cursor
        # item. Seeded from DB at spawn (see net.spawn) and mutated by the
        # inventory/equipment handlers. The client echoes the assigned slot id, so
        # this MUST agree with the ids written into the spawn packet.
        from .inventory_model import InventoryModel
        self.inv_model = InventoryModel()

        # ── Avatar control-authority re-assert (synch-crash bypass) ──
        # Last monotonic time an OFF->ON control toggle was sent (throttle), and a
        # countdown of re-asserts armed at zone entry to fire on the first inbound
        # client packets — when the STEADY action is live — so the input-authority
        # bit (action +0x95) is set before a mob can hit. See
        # GameServer._arm_control_reassert_window / reassert_control_after_zone_entry.
        self._last_control_reset_time: float = 0.0
        self._control_reassert_pending: int = 0

        # ── Waystone / checkpoint (obelisk) recall ──
        # GC ids (world.checkpoints.<Name>) this character has unlocked, loaded
        # from character_checkpoints at character-select. obelisk_click_index
        # cycles the destination list on each empty obelisk click (C# 0x0C).
        self.unlocked_checkpoints: set[str] = set()
        self.obelisk_click_index: int = 0

        # ── Town portal ──
        self.has_saved_town_portal: bool = False
        self.town_portal_zone_name: str = ""
        self.town_portal_target_zone: str = ""
        self.town_portal_zone_id: int = 0
        self.town_portal_pos_x: float = 0.0
        self.town_portal_pos_y: float = 0.0
        self.town_portal_pos_z: float = 0.0

        self.message_queue = MessageQueue()
        # Streaming entity updates (monster Follow/Move) drained INTO the
        # per-4th-tick 0x0D WorldInterval frame. The client consumes exactly
        # ONE entity-channel message per 4 world ticks (133 ms) and runs its
        # world clock at 3x while more than 2 messages are backed up
        # (FUN_005d9e30) — sustained producers must share the interval frame,
        # never add frames of their own. See movement.build_world_interval_packet.
        self.interval_message_queue = MessageQueue()
        self._tick_task = None   # asyncio.Task running the movement tick (set at spawn)
        # True after this player's action was relayed to viewers (net.action_relay)
        # and before their next move — the move un-roots the display copy on
        # viewers' screens so it resumes following. See _handle_client_move.
        self.viewer_action_pending = False

    def send_raw(self, data: bytes) -> None:
        """Write bytes to the client (fire-and-forget on the event loop)."""
        if not self.is_connected:
            return
        try:
            if _TRACE_SEND:
                log.debug(f"[SEND] conn={self.conn_id} len={len(data)} "
                          f"first_bytes={data[:24].hex()}")
            self.writer.write(data)
        except Exception as ex:  # noqa: BLE001
            log.warn(f"[RRConnection {self.conn_id}] write error: {ex}")
            self.is_connected = False

    def send_compressed_a(self, dest: int, message_type: int, inner: bytes) -> None:
        """SendCompressedA — the primary game-server send path (0x0A frame)."""
        self.send_raw(framing.build_compressed_a(self.peer_id24, dest, message_type, inner))

    def send_to_client(self, inner: bytes) -> None:
        """SendToClient — SendCompressedA with the default dest=0x01 type=0x0F."""
        self.send_compressed_a(0x01, 0x0F, inner)

    def send_system_message(self, message: str) -> None:
        """SendSystemMessage — an on-screen system/error toast.

        Port of C# SendSystemMessage (UnityGameServer.cs:5921): ClientEntity
        channel 6, then ``0x00 0x0D`` + ASCII text + NUL terminator, wrapped in
        the default SendCompressedA (dest 0x01, type 0x0F). Used to *warn* the
        player on a recoverable error instead of dropping the connection.
        """
        from . import framing
        w = framing.LEWriter()
        w.write_byte(0x06)
        w.write_byte(0x00)
        w.write_byte(0x0D)
        for ch in message:
            w.write_byte(ord(ch) & 0xFF)
        w.write_byte(0x00)
        self.send_to_client(w.to_array())

    def send_message_0x10(self, channel: int, payload: bytes) -> None:
        """SendMessage0x10 — uncompressed direct frame on ``channel``."""
        self.send_raw(framing.build_message_0x10(self.peer_id24, channel, payload))

    def disconnect(self) -> None:
        self.is_connected = False
        try:
            self.writer.close()
        except Exception as ex:  # noqa: BLE001
            log.warn(f"[RRConnection {self.conn_id}] error during disconnect: {ex}")
