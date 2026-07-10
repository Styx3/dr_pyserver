"""Bling Gnome summon — port of DRS-NET ``BlingGnomeRuntime`` (Bling/BlingGnomeRuntime.cs).

The Bling Gnome ("Pope Sweet Geebus") is the henchman summoned by the
``skills.generic.SummonBlingGnome`` skill. Client-faithful behaviour
(SummonBlingGnome.gc + BlingGnome_Summon.gc):

* **Placing the skill on the hotbar summons the gnome** (the skill's own
  description: "Place this skilzizzil in your skill tray to 'auto-magically'
  summon up a mad Bling Gnome"). Removing/displacing it despawns him.
* The gnome **follows the owner** and **auto-picks up gold drops** for them.
* **Casting the skill** opens a 10 s ``SpellConvertItemsToGoldEffect`` window:
  ground items of Rare quality and lower within 200 units are converted to
  gold at ``ConversionRatio 0.5 × GoldValueMod 2.3``. 45 s cooldown.

Wire model (every shape below is the DRS-NET live-proven packet):

* spawn      = entity create ``0x01`` + gc type + StockUnit init ``0x02`` with
  the owner bit (unitFlags ``0x16|0x01``) + per-viewer owner entity ref.
* components = one framed packet of three ``0x32`` component creates
  (Modifiers / Manipulators / Behavior-with-anchor) + spawn animation.
* movement   = MoverUpdate ``0x65`` on the Behavior cid; animations are action
  ``0x20`` PlayAnimation; the convert window is action ``0xA1`` id 5
  (ConvertItemsToGold). All carry the gnome's EntitySynchInfo (``0x02`` + HP).

Delivery: one-shot packets (spawn / components / remove) are framed direct
sends — same as ground-loot creates. Recurring behavior traffic (movers,
fidgets, actions) rides UNFRAMED on ``conn.interval_message_queue`` so it is
drained inside the per-4th-tick ``0x0D`` WorldInterval frame — the mob-proven
path that keeps the client's one-message-per-133 ms world-clock contract
(see managers/monster_ai.py docstring).

HP: the gnome uses the client henchman health curve × ``MaxHealth 0.3``
(BlingGnome_Summon.gc); the owning client simulates it, so any client HP
report observed for the gnome's cids is adopted, never contradicted.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core import log
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection

ENTITY_GC_TYPE = "creatures.summon.blinggnome.base.BlingGnome_Summon"
BEHAVIOR_GC_TYPE = "creatures.summon.blinggnome.base.BlingGnome_Summon.Behavior"
SKILL_GC_TYPE = "skills.generic.SummonBlingGnome"

# ── SummonBlingGnome.gc / BlingGnome_Summon.gc constants (client data) ──
CONVERSION_RATIO = 0.5          # Behavior.ConversionRatio
GOLD_VALUE_MOD = 2.3            # skill GoldValueMod
CONVERT_SEARCH_RADIUS = 200     # SpellConvertItemsToGoldEffect.SearchRadius
BEHAVIOR_SEARCH_RANGE = 250     # Behavior.SearchRange (gold sniffing)
GNOME_SPEED = 50.0              # Description.Speed
EFFECT_DURATION_SECONDS = 10.0  # SpellModEffect.Duration
CONVERT_BOUNCE_SECONDS = 2.05   # DRS-NET conversion bounce delay
IDLE_BASE_SECONDS = 2           # Behavior.BaseTime
IDLE_VARIABLE_SECONDS = 7       # Behavior.VariableTime
MAX_CONVERT_RARITY = 3          # "Rare quality and lower"

# ── DRS-NET follow-mirror tuning ──
_FOLLOW_START_RADIUS = 30.0
_FOLLOW_SETTLE_RADIUS = 30.0
_FOLLOW_TOO_CLOSE = 10.0
_FOLLOW_RETARGET_DEADBAND = 20.0
_FOLLOW_MAX_STEP_CADENCE = 0.132
_FOLLOW_DEFAULT_STEP_CADENCE = 0.099
_FOLLOW_STEP_MIN_INTERVAL = 0.033
_MOVER_ACTIVE_MIN_INTERVAL = 0.300
_MOVER_SETTLED_MIN_INTERVAL = 0.500
_MOVER_ACTIVE_MAX_DRIFT = 14.0
_MOVER_SETTLED_MAX_DRIFT = 18.0

_BEHAVIOR_TICK_INTERVAL = 0.15
_SEARCH_PULSE_SECONDS = 1.0
_BEHAVIOR_CREATE_DELAY = 0.066

# ── Animation ids (DRS-NET, matched to BlingGnome_Animations.gc) ──
_ANIM_FIDGET_FIRST = 101
_ANIM_FIDGET_LAST = 104
_ANIM_PICKUP = 110
_ANIM_ACTIVE_SKILL = 140
_ANIM_DEATH = 150
_ANIM_SPAWN = 180
_ANIM_STATE_DIRECT = 0x00
_ANIM_STATE_ACTIVE = 0x06
_ANIM_STATE_FIDGET = 0x08

# Henchman health curve (UseHenchmanCurveTables) × MaxHealth 0.3, fixed-point.
_BLING_HEALTH_SCALE = 0.3
_HENCHMAN_HEALTH_LEVELS = (1, 5, 10, 50, 75, 100, 110)
_HENCHMAN_HEALTH_VALUES = (245.0, 1550.0, 2810.0, 10989.0, 19646.0, 26009.0, 28020.0)


def _to_fixed(v: float) -> int:
    return int(v * 256.0)


def _to_fixed_rounded(v: float) -> int:
    return int(v * 256.0 + 0.5) if v >= 0 else int(v * 256.0 - 0.5)


def _dist_sq(ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = ax - bx, ay - by
    return dx * dx + dy * dy


def _nearly_equal(a: float, b: float) -> bool:
    return abs(a - b) < 0.001


def resolve_curve_value(level: int, levels: Tuple[int, ...],
                        values: Tuple[float, ...], fallback: float) -> float:
    """Piecewise-linear curve lookup (DRS-NET ResolveCurveValue)."""
    if not levels or len(levels) != len(values):
        return fallback
    level = max(1, level)
    if level <= levels[0]:
        return values[0]
    for i in range(1, len(levels)):
        if level <= levels[i]:
            prev_lv, next_lv = levels[i - 1], levels[i]
            prev_v, next_v = values[i - 1], values[i]
            if next_lv <= prev_lv:
                return next_v
            t = (level - prev_lv) / (next_lv - prev_lv)
            return prev_v + (next_v - prev_v) * t
    return values[-1]


def henchman_hp_wire(level: int, max_health_scale: float) -> int:
    """Summoned-unit max HP on the wire: henchman curve × the creature's
    MaxHealth scale, fixed-point multiply then >>16, ×256 (DRS-NET
    ResolveBlingGnomeHitPointsWire generalized — shared with summons.py)."""
    curve_raw = _to_fixed_rounded(resolve_curve_value(
        level, _HENCHMAN_HEALTH_LEVELS, _HENCHMAN_HEALTH_VALUES, 245.0))
    scale_raw = _to_fixed_rounded(max_health_scale)
    health = (curve_raw * scale_raw) >> 16
    return max(1, health) * 256


def gnome_hp_wire(level: int) -> int:
    """Gnome max HP: henchman curve × MaxHealth 0.3 (BlingGnome_Summon.gc)."""
    return henchman_hp_wire(level, _BLING_HEALTH_SCALE)


@dataclass
class GnomeState:
    """One live gnome (DRS-NET GnomeState)."""
    entity_id: int
    behavior_id: int
    modifiers_id: int
    manipulators_id: int
    owner_login: str
    owner_conn_id: int
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0
    heading: float = 0.0
    spawn_pos_x: float = 0.0
    spawn_pos_y: float = 0.0
    spawn_heading: float = 0.0
    snapshot_level: int = 1
    hp_wire: int = 256

    behavior_created: bool = False
    is_active: bool = False
    activate_until: float = 0.0
    items_converted: int = 0
    gold_generated: int = 0
    next_fidget_at: float = 0.0
    last_fidget_anim: int = 0
    next_search_at: float = 0.0

    has_move_target: bool = False
    move_target_x: float = 0.0
    move_target_y: float = 0.0
    last_follow_at: float = 0.0
    mover_valid: bool = False
    mover_at: float = 0.0
    mover_x: float = 0.0
    mover_y: float = 0.0
    mover_heading: float = 0.0
    mover_terminal: bool = False

    component_packet: bytes = b""
    behavior_task: Optional[asyncio.Task] = None
    conversion_task: Optional[asyncio.Task] = None
    busy_until: float = 0.0          # pickup bounce gate (replaces C# coroutine waits)


class BlingGnomeManager:
    """All live gnomes, keyed by owner login (DRS-NET keys conn-id and
    re-keys on reconnect; login is the stable equivalent here)."""

    def __init__(self, server: "GameServer"):
        self._server = server
        self._gnomes: Dict[str, GnomeState] = {}

    # ── Queries ──────────────────────────────────────────────────────────

    def has_gnome(self, conn: "RRConnection") -> bool:
        return bool(conn.login_name) and conn.login_name in self._gnomes

    def gnome_for(self, conn: "RRConnection") -> Optional[GnomeState]:
        return self._gnomes.get(conn.login_name or "")

    def is_gnome_target(self, conn: "RRConnection", target_entity_id: int) -> bool:
        """True when ``target_entity_id`` is this player's own gnome (the 0x50
        use-target route — DRS-NET TryResolveGnomeTarget owner gate)."""
        g = self.gnome_for(conn)
        return g is not None and (target_entity_id == 0
                                  or target_entity_id == g.entity_id)

    def owner_conn_for_entity(self, entity_id: int) -> Optional["RRConnection"]:
        """The owner connection of a live gnome with ``entity_id``, else None.

        Lets the combat-telemetry kill-credit map a mob finished off by a
        player's gnome (the hook reports the gnome's eid, not the avatar's) back
        to the owner. See :meth:`SummonManager.owner_conn_for_entity`."""
        for st in self._gnomes.values():
            if st.entity_id == entity_id:
                return self._server.connections.get(st.owner_conn_id)
        return None

    @staticmethod
    def is_gnome_skill(skill_gc_class: Optional[str]) -> bool:
        s = (skill_gc_class or "").lower()
        return "blinggnome" in s or "summonbling" in s

    # ── Skill triggers ───────────────────────────────────────────────────

    def toggle(self, conn: "RRConnection") -> None:
        """Cast / chat toggle: summon when absent, open the convert window
        when present (DRS-NET ToggleGnome)."""
        if self.has_gnome(conn):
            self.activate(conn)
        else:
            self.spawn(conn)

    def spawn(self, conn: "RRConnection") -> None:
        """Summon the gnome next to the owner (DRS-NET SpawnGnome)."""
        login = conn.login_name
        if not login or login in self._gnomes or not conn.is_spawned:
            return

        g = GnomeState(
            entity_id=self._server.allocate_entity_id(),
            behavior_id=self._server.allocate_entity_id(),
            modifiers_id=self._server.allocate_entity_id(),
            manipulators_id=self._server.allocate_entity_id(),
            owner_login=login,
            owner_conn_id=conn.conn_id,
        )
        g.pos_x = conn.player_pos_x + 5.0
        g.pos_y = conn.player_pos_y + 5.0
        g.pos_z = conn.player_pos_z
        g.heading = getattr(conn, "player_heading", 0.0) or 0.0
        g.spawn_pos_x, g.spawn_pos_y = g.pos_x, g.pos_y
        g.spawn_heading = g.heading

        level = max(1, min(255, getattr(conn, "player_level", 1) or 1))
        g.snapshot_level = level
        g.hp_wire = gnome_hp_wire(level)

        now = time.monotonic()
        g.next_fidget_at = now + self._fidget_delay()
        g.next_search_at = now + _SEARCH_PULSE_SECONDS
        self._gnomes[login] = g

        # Entity snapshot to the owner + every instance peer (per-viewer owner ref).
        conn.send_to_client(self.build_entity_snapshot_packet(
            g, self._own_owner_ref(conn)))
        for peer in self._instance_peers(conn):
            peer.send_to_client(self.build_entity_snapshot_packet(
                g, self._remote_owner_ref(peer, login)))

        g.behavior_task = asyncio.create_task(self._behavior_loop(conn, g))
        log.info(f"[GNOME] '{login}' summoned entity=0x{g.entity_id:04X} "
                 f"beh=0x{g.behavior_id:04X} level={level} hp={g.hp_wire}")

    def activate(self, conn: "RRConnection", target_entity_id: int = 0) -> bool:
        """Open the 10 s convert-items-to-gold window (DRS-NET ActivateGnome)."""
        g = self.gnome_for(conn)
        if g is None or not g.behavior_created:
            log.info(f"[GNOME-ACTIVATE] '{conn.login_name}' skipped "
                     f"target={target_entity_id} "
                     f"created={g.behavior_created if g else None}")
            return False
        if target_entity_id and target_entity_id != g.entity_id:
            return False

        now = time.monotonic()
        g.is_active = True
        g.activate_until = now + EFFECT_DURATION_SECONDS
        g.items_converted = 0
        g.gold_generated = 0
        g.next_search_at = now

        self._send_play_animation(conn, g, _ANIM_ACTIVE_SKILL, _ANIM_STATE_ACTIVE, 40)
        self._send_convert_action(conn, g)
        if g.conversion_task is not None:
            g.conversion_task.cancel()
        g.conversion_task = asyncio.create_task(self._apply_active_conversion(conn, g))
        log.info(f"[GNOME-ACTIVATE] '{conn.login_name}' window open "
                 f"{EFFECT_DURATION_SECONDS}s radius={CONVERT_SEARCH_RADIUS}")
        return True

    def despawn(self, conn: "RRConnection", play_death_anim: bool = True) -> None:
        """Remove the gnome (hotbar removal / chat toggle off — DRS-NET
        DespawnGnome)."""
        login = conn.login_name
        g = self._gnomes.pop(login or "", None)
        if g is None:
            return
        self._cancel_tasks(g)
        if play_death_anim and g.behavior_created:
            self._send_play_animation(conn, g, _ANIM_DEATH, _ANIM_STATE_DIRECT, 24)
            asyncio.create_task(self._delayed_entity_remove(conn, g, 0.8))
        else:
            self._broadcast_framed(conn, self._build_entity_remove(g),
                                   include_owner=conn.is_connected)
        log.info(f"[GNOME] '{login}' despawned 0x{g.entity_id:04X} — "
                 f"{g.items_converted} items -> {g.gold_generated}g")

    def cleanup(self, conn: "RRConnection") -> None:
        """Silent state drop on zone transition / disconnect (DRS-NET
        CleanupForZoneTransition / OnPlayerDisconnect). The gnome's
        ``ZoneAction = DeathOnZone`` — it never crosses zones. No packets to
        the (possibly torn-down) owner; peers get the entity remove so they
        don't keep a ghost."""
        login = conn.login_name
        g = self._gnomes.pop(login or "", None)
        if g is None:
            return
        self._cancel_tasks(g)
        remove = self._build_entity_remove(g)
        for peer in self._instance_peers(conn):
            peer.send_to_client(remove)
        log.info(f"[GNOME] '{login}' cleaned up 0x{g.entity_id:04X} (zone/disconnect)")

    def send_gnomes_to_connection(self, viewer: "RRConnection") -> None:
        """Late-join replication: show every live same-instance gnome to a
        newly spawned viewer (DRS-NET SendGnomesToConnection)."""
        for login, g in self._gnomes.items():
            if login == viewer.login_name:
                continue
            owner = self._conn_by_login(login)
            if owner is None or not owner.is_spawned:
                continue
            if not self._same_instance(owner, viewer):
                continue
            viewer.send_to_client(self.build_entity_snapshot_packet(
                g, self._remote_owner_ref(viewer, login)))
            if g.component_packet:
                viewer.send_to_client(g.component_packet)
            log.info(f"[GNOME] late-join replication 0x{g.entity_id:04X} "
                     f"owner='{login}' viewer='{viewer.login_name}'")

    # ── Packet builders (DRS-NET byte-for-byte) ─────────────────────────

    def build_entity_snapshot_packet(self, g: GnomeState,
                                     owner_entity_id: int) -> bytes:
        """Entity create + StockUnit init (DRS-NET BuildEntitySnapshotPacket).

        unitFlags ``0x16`` | ``0x01`` owner bit; owner ref is per-viewer (the
        viewer's id for the owner's avatar)."""
        unit_flags = 0x16 | (0x01 if owner_entity_id else 0x00)
        w = LEWriter()
        w.write_byte(0x07)                       # BeginStream

        w.write_byte(0x01)                       # EntityCreate
        w.write_uint16(g.entity_id)
        w.write_byte(0xFF)
        w.write_cstring(ENTITY_GC_TYPE)

        w.write_byte(0x02)                       # EntityInit (StockUnit)
        w.write_uint16(g.entity_id)
        w.write_uint32(0x06)                     # world-entity flags (blocking|activatable... DRS-NET 0x06)
        w.write_int32(_to_fixed(g.pos_x))
        w.write_int32(_to_fixed(g.pos_y))
        w.write_int32(_to_fixed(g.pos_z))
        w.write_int32(_to_fixed(g.heading))
        w.write_byte(0x00)

        w.write_byte(unit_flags)
        w.write_byte(g.snapshot_level & 0xFF)
        w.write_uint16(0)
        w.write_uint16(0)
        if unit_flags & 0x01:
            w.write_uint16(owner_entity_id)
        w.write_uint32(g.hp_wire)
        w.write_uint32(0)                        # mana wire
        w.write_byte(0x00)

        w.write_byte(0x00)
        w.write_uint16(0); w.write_uint16(0)
        w.write_byte(0x00)
        w.write_uint16(0); w.write_uint32(0)
        w.write_byte(0x00)
        w.write_uint32(0); w.write_uint32(0); w.write_uint32(0)

        w.write_byte(0x06)                       # EndStream
        return w.to_array()

    def _build_component_packet(self, g: GnomeState) -> bytes:
        """Modifiers + Manipulators + Behavior component creates and the spawn
        animation, in one frame (DRS-NET DelayedBehaviorCreate)."""
        anchor_x, anchor_y = _to_fixed(g.pos_x), _to_fixed(g.pos_y)
        w = LEWriter()
        w.write_byte(0x07)

        w.write_byte(0x32)                       # ComponentCreate
        w.write_uint16(g.entity_id)
        w.write_uint16(g.modifiers_id)
        w.write_byte(0xFF)
        w.write_cstring("Modifiers")
        w.write_byte(0x01)
        w.write_uint32(0)
        w.write_uint32(0)
        w.write_byte(0x00)

        w.write_byte(0x32)
        w.write_uint16(g.entity_id)
        w.write_uint16(g.manipulators_id)
        w.write_byte(0xFF)
        w.write_cstring("Manipulators")
        w.write_byte(0x01)
        w.write_byte(0x00)

        w.write_byte(0x32)
        w.write_uint16(g.entity_id)
        w.write_uint16(g.behavior_id)
        w.write_byte(0xFF)
        w.write_cstring(BEHAVIOR_GC_TYPE)
        w.write_byte(0x01)

        w.write_byte(0x00)
        w.write_byte(0x00)
        w.write_byte(0x00)
        w.write_byte(0x01)
        w.write_byte(0x42)                       # behavior init: wander anchor
        w.write_int32(anchor_x)
        w.write_int32(anchor_y)
        w.write_byte(0x01)
        w.write_byte(0x00)
        w.write_byte(0x00)
        w.write_byte(0x00)

        w.write_byte(0x09)
        w.write_uint16(0x0000)

        w.write_int32(anchor_x)
        w.write_int32(anchor_y)
        self._write_play_animation_submsg(w, g, _ANIM_SPAWN, _ANIM_STATE_DIRECT, 24)

        w.write_byte(0x06)
        return w.to_array()

    def _build_entity_remove(self, g: GnomeState) -> bytes:
        w = LEWriter()
        w.write_byte(0x07)
        w.write_byte(0x05)                       # EntityDespawn
        w.write_uint16(g.entity_id)
        w.write_byte(0x06)
        return w.to_array()

    def _write_synch(self, w: LEWriter, g: GnomeState) -> None:
        """Gnome EntitySynchInfo trailer — its own HP, never the avatar's."""
        w.write_byte(0x02)
        w.write_uint32(g.hp_wire & 0xFFFFFFFF)

    def _write_play_animation_submsg(self, w: LEWriter, g: GnomeState,
                                     logical_id: int, anim_state: int,
                                     duration_ticks: int) -> None:
        """Action 0x20 PlayAnimation on the Behavior cid (DRS-NET
        WritePlayAnimationSubmsg, incl. its active/fidget wire quirks)."""
        wire_state, wire_arg = anim_state, logical_id
        if logical_id == _ANIM_ACTIVE_SKILL:
            wire_state, wire_arg = _ANIM_STATE_ACTIVE, 40
        elif _ANIM_FIDGET_FIRST <= logical_id <= _ANIM_FIDGET_LAST:
            wire_state, wire_arg = _ANIM_STATE_DIRECT, logical_id
        w.write_byte(0x35)
        w.write_uint16(g.behavior_id)
        w.write_byte(0x04)                       # CreateAction
        w.write_byte(0x20)                       # PlayAnimation
        w.write_byte(0x00)
        w.write_uint32(wire_state)
        w.write_uint32(wire_arg)
        w.write_uint32(duration_ticks)
        w.write_uint32(0x3F800000)               # 1.0f speed
        self._write_synch(w, g)

    def build_mover_update(self, g: GnomeState, terminal: bool) -> bytes:
        """MoverUpdate 0x65 on the Behavior cid, UNFRAMED (interval-queue
        ride). Terminal updates write the DRS-NET two-record stop shape."""
        prev_heading = g.mover_heading if g.mover_valid else g.spawn_heading
        prev_x = g.mover_x if g.mover_valid else g.spawn_pos_x
        prev_y = g.mover_y if g.mover_valid else g.spawn_pos_y
        flags = self._mover_flags(prev_heading, prev_x, prev_y,
                                  g.heading, g.pos_x, g.pos_y, terminal)
        w = LEWriter()
        w.write_byte(0x35)
        w.write_uint16(g.behavior_id)
        w.write_byte(0x65)
        w.write_byte(0x00)
        if terminal:
            w.write_byte(0x02)
            lead = self._mover_flags(g.heading, g.pos_x, g.pos_y,
                                     g.heading, g.pos_x + 1.0, g.pos_y, False)
            self._write_mover_record(w, (lead | 0x06) & 0x06,
                                     g.heading, g.pos_x + 1.0, g.pos_y)
            self._write_mover_record(w, (flags | 0x03) & 0x07,
                                     g.heading, g.pos_x, g.pos_y)
        else:
            w.write_byte(0x01)
            self._write_mover_record(w, flags, g.heading, g.pos_x, g.pos_y)
        self._write_synch(w, g)
        return w.to_array()

    @staticmethod
    def _mover_flags(prev_heading: float, prev_x: float, prev_y: float,
                     heading: float, pos_x: float, pos_y: float,
                     terminal: bool) -> int:
        flags = 0x01 if terminal else 0x00
        if not _nearly_equal(prev_heading, heading):
            flags |= 0x02
        if not terminal and (not _nearly_equal(prev_x, pos_x)
                             or not _nearly_equal(prev_y, pos_y)):
            flags |= 0x06
        return flags & 0x07

    @staticmethod
    def _write_mover_record(w: LEWriter, flags: int, heading: float,
                            pos_x: float, pos_y: float) -> None:
        w.write_byte(flags & 0x07)
        w.write_int32(_to_fixed(heading))
        w.write_int32(_to_fixed(pos_x))
        w.write_int32(_to_fixed(pos_y))

    def build_convert_action(self, g: GnomeState, owner_x: float,
                             owner_y: float) -> bytes:
        """Action 0xA1 id 5 = ConvertItemsToGold (DRS-NET
        SendConvertItemsToGoldAction), UNFRAMED."""
        w = LEWriter()
        w.write_byte(0x35)
        w.write_uint16(g.behavior_id)
        w.write_byte(0x04)
        w.write_byte(0xA1)
        w.write_byte(0x00)
        w.write_uint32(5)
        w.write_uint16(CONVERT_SEARCH_RADIUS)
        w.write_int32(_to_fixed(owner_x))
        w.write_int32(_to_fixed(owner_y))
        w.write_int32(int(GOLD_VALUE_MOD * 256))
        self._write_synch(w, g)
        return w.to_array()

    # ── Behavior loop ────────────────────────────────────────────────────

    async def _behavior_loop(self, conn: "RRConnection", g: GnomeState) -> None:
        """Server-stepped gnome brain: delayed component create, then the
        0.15 s follow/search/fidget tick (DRS-NET DelayedBehaviorCreate +
        GnomeBehaviorLoop)."""
        try:
            await asyncio.sleep(_BEHAVIOR_CREATE_DELAY)
            if self._gnomes.get(g.owner_login) is not g or not conn.is_connected:
                return
            g.component_packet = self._build_component_packet(g)
            self._broadcast_framed(conn, g.component_packet)
            g.behavior_created = True

            await asyncio.sleep(0.2)
            while (conn.is_connected
                   and self._gnomes.get(g.owner_login) is g):
                now = time.monotonic()

                if g.is_active and now >= g.activate_until:
                    g.is_active = False
                    log.info(f"[GNOME] '{g.owner_login}' conversion window expired")

                self._update_follow_mirror(conn, g, now)

                if now >= g.next_search_at and now >= g.busy_until:
                    g.next_search_at = now + _SEARCH_PULSE_SECONDS
                    picked = self._try_pickup_nearest_gold(conn, g)
                    if picked:
                        g.busy_until = now + 0.3

                if (g.behavior_created and now >= g.next_fidget_at
                        and not g.is_active and not g.has_move_target):
                    anim = self._pick_fidget_animation(g)
                    self._send_play_animation(
                        conn, g, anim, _ANIM_STATE_FIDGET,
                        50 if anim == 103 else 40)
                    g.next_fidget_at = now + self._fidget_delay()

                await asyncio.sleep(_BEHAVIOR_TICK_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception as ex:  # noqa: BLE001 — never kill the server loop
            log.error(f"[GNOME] behavior loop failed for '{g.owner_login}': {ex}")

    # ── Follow mirror (DRS-NET UpdateFollowMirror) ───────────────────────

    def _update_follow_mirror(self, conn: "RRConnection", g: GnomeState,
                              now: float) -> None:
        if not g.behavior_created:
            return
        owner_x = conn.player_pos_x
        owner_y = conn.player_pos_y
        owner_z = conn.player_pos_z
        owner_heading = getattr(conn, "player_heading", 0.0) or 0.0

        follow_x, follow_y = owner_x, owner_y
        offset_index = g.entity_id % 4
        offset_x = -18.0 if offset_index in (1, 3) else 18.0
        offset_y = -10.0 if offset_index >= 2 else 10.0
        if _dist_sq(g.pos_x, g.pos_y, owner_x, owner_y) < _FOLLOW_TOO_CLOSE ** 2:
            follow_x += offset_x
            follow_y += offset_y

        owner_dist_sq = _dist_sq(g.pos_x, g.pos_y, owner_x, owner_y)
        follow_dist_sq = _dist_sq(g.pos_x, g.pos_y, follow_x, follow_y)
        follow_move_active = g.has_move_target
        should_path = self._should_follow_path(follow_move_active,
                                               owner_dist_sq, follow_dist_sq)
        changed = False
        state_changed = False

        if should_path:
            if g.has_move_target and _dist_sq(
                    g.move_target_x, g.move_target_y,
                    follow_x, follow_y) < _FOLLOW_RETARGET_DEADBAND ** 2:
                follow_x, follow_y = g.move_target_x, g.move_target_y

            if (not g.has_move_target
                    or not _nearly_equal(g.move_target_x, follow_x)
                    or not _nearly_equal(g.move_target_y, follow_y)):
                g.move_target_x, g.move_target_y = follow_x, follow_y
                g.has_move_target = True
                state_changed = True

            move_due = (g.last_follow_at <= 0
                        or now - g.last_follow_at >= _FOLLOW_STEP_MIN_INTERVAL)
            if move_due:
                dx, dy = follow_x - g.pos_x, follow_y - g.pos_y
                if not _nearly_equal(dx, 0.0) or not _nearly_equal(dy, 0.0):
                    step = self._follow_step(g, now)
                    new_x, new_y = self._project_step(
                        g.pos_x, g.pos_y, follow_x, follow_y, step)
                    heading = self._heading_from_delta(dx, dy, g.heading)
                    if (not _nearly_equal(g.pos_x, new_x)
                            or not _nearly_equal(g.pos_y, new_y)):
                        g.pos_x, g.pos_y = new_x, new_y
                        changed = True
                    if not _nearly_equal(g.heading, heading):
                        g.heading = heading
                        changed = True
                g.last_follow_at = now

            if not _nearly_equal(g.pos_z, owner_z):
                g.pos_z = owner_z
                changed = True

            if (_nearly_equal(g.pos_x, follow_x)
                    and _nearly_equal(g.pos_y, follow_y)):
                if g.has_move_target:
                    g.has_move_target = False
                    g.move_target_x = g.move_target_y = 0.0
                    state_changed = True
                if not _nearly_equal(g.heading, owner_heading):
                    g.heading = owner_heading
                    changed = True
        elif follow_move_active:
            g.has_move_target = False
            g.move_target_x = g.move_target_y = 0.0
            state_changed = True
            if not _nearly_equal(g.pos_z, owner_z):
                g.pos_z = owner_z
                changed = True
            if not _nearly_equal(g.heading, owner_heading):
                g.heading = owner_heading
                changed = True

        terminal_needed = (not should_path and not g.has_move_target
                           and g.mover_valid and not g.mover_terminal)
        if (changed or state_changed or terminal_needed) \
                and self._mover_update_due(g, now):
            self._send_mover_update(conn, g, terminal=not g.has_move_target,
                                    now=now)

    @staticmethod
    def _should_follow_path(follow_move_active: bool, owner_dist_sq: float,
                            follow_dist_sq: float) -> bool:
        if (owner_dist_sq < _FOLLOW_TOO_CLOSE ** 2
                and follow_dist_sq > _FOLLOW_RETARGET_DEADBAND ** 2):
            return True
        if follow_dist_sq <= _FOLLOW_SETTLE_RADIUS ** 2:
            return False
        if follow_move_active:
            return True
        return owner_dist_sq > _FOLLOW_START_RADIUS ** 2

    def _mover_update_due(self, g: GnomeState, now: float) -> bool:
        prev_x = g.mover_x if g.mover_valid else g.spawn_pos_x
        prev_y = g.mover_y if g.mover_valid else g.spawn_pos_y
        prev_heading = g.mover_heading if g.mover_valid else g.spawn_heading
        terminal = not g.has_move_target
        moved_sq = _dist_sq(prev_x, prev_y, g.pos_x, g.pos_y)
        heading_changed = not _nearly_equal(prev_heading, g.heading)
        terminal_changed = g.mover_valid and g.mover_terminal != terminal

        if moved_sq <= 0 and not heading_changed and not terminal_changed:
            return False
        if not g.mover_valid or g.mover_at <= 0:
            return True
        if terminal_changed:
            return True
        if g.has_move_target:
            if moved_sq >= _MOVER_ACTIVE_MAX_DRIFT ** 2:
                return True
            return now - g.mover_at >= _MOVER_ACTIVE_MIN_INTERVAL
        if moved_sq >= _MOVER_SETTLED_MAX_DRIFT ** 2:
            return True
        return now - g.mover_at >= _MOVER_SETTLED_MIN_INTERVAL

    def _follow_step(self, g: GnomeState, now: float) -> float:
        cadence = _FOLLOW_DEFAULT_STEP_CADENCE
        if g.last_follow_at > 0:
            cadence = max(0.0, now - g.last_follow_at)
        cadence = min(cadence, _FOLLOW_MAX_STEP_CADENCE)
        return max(1.0, round(GNOME_SPEED * cadence))

    @staticmethod
    def _project_step(pos_x: float, pos_y: float, target_x: float,
                      target_y: float, step: float) -> Tuple[float, float]:
        dx, dy = target_x - pos_x, target_y - pos_y
        dist = math.sqrt(dx * dx + dy * dy)
        if dist <= 0 or dist <= step:
            return target_x, target_y
        scale = step / dist
        return pos_x + dx * scale, pos_y + dy * scale

    @staticmethod
    def _heading_from_delta(dx: float, dy: float, fallback: float) -> float:
        if _nearly_equal(dx, 0.0) and _nearly_equal(dy, 0.0):
            return fallback
        heading = round(math.degrees(math.atan2(-dx, dy)))
        return heading % 360.0

    # ── Gold pickup / item conversion ────────────────────────────────────

    def _try_pickup_nearest_gold(self, conn: "RRConnection",
                                 g: GnomeState) -> bool:
        """Sniff out the nearest gold pile near the gnome or owner and bag it
        (DRS-NET TryFindNearestGoldDrop + PickupGroundItem gold path)."""
        from . import loot

        best_eid, best_dist = 0, float("inf")
        for eid, drop in self._drops_near(conn, g.pos_x, g.pos_y,
                                          BEHAVIOR_SEARCH_RANGE) \
                + self._drops_near(conn, conn.player_pos_x, conn.player_pos_y,
                                   BEHAVIOR_SEARCH_RANGE):
            if drop.gold_amount <= 0:
                continue
            dist = min(_dist_sq(drop.pos_x, drop.pos_y, g.pos_x, g.pos_y),
                       _dist_sq(drop.pos_x, drop.pos_y,
                                conn.player_pos_x, conn.player_pos_y))
            if dist < best_dist:
                best_dist, best_eid = dist, eid

        if not best_eid:
            return False
        info = loot.remove_drop(best_eid)
        if info is None:
            return False
        self._broadcast_framed(conn, self._build_drop_remove(best_eid))
        self._credit_gold(conn, max(1, info.gold_amount))
        g.items_converted += 1
        g.gold_generated += max(1, info.gold_amount)
        log.info(f"[GNOME-PICKUP] '{g.owner_login}' gold 0x{best_eid:04X} "
                 f"+{info.gold_amount}g (total {g.gold_generated}g)")
        return True

    async def _apply_active_conversion(self, conn: "RRConnection",
                                       g: GnomeState) -> None:
        """The activation window's rare-and-below item→gold sweep (DRS-NET
        ApplyActiveConversion: collect candidates, bounce, then convert)."""
        from . import loot
        try:
            await asyncio.sleep(0.15)
            if self._gnomes.get(g.owner_login) is not g or not conn.is_connected:
                return

            seen: set[int] = set()
            candidates: List[Tuple[int, "loot.DroppedItem"]] = []
            for eid, drop in self._drops_near(conn, g.pos_x, g.pos_y,
                                              CONVERT_SEARCH_RADIUS) \
                    + self._drops_near(conn, conn.player_pos_x,
                                       conn.player_pos_y,
                                       CONVERT_SEARCH_RADIUS):
                if eid in seen or not self._can_convert(drop):
                    continue
                seen.add(eid)
                candidates.append((eid, drop))
            candidates.sort(key=lambda c: c[0])
            if not candidates:
                return
            log.info(f"[GNOME-CONVERT] '{g.owner_login}' "
                     f"candidates={len(candidates)}")

            await asyncio.sleep(CONVERT_BOUNCE_SECONDS)

            for eid, drop in candidates:
                if (self._gnomes.get(g.owner_login) is not g
                        or not conn.is_connected):
                    return
                if g.behavior_created:
                    self._send_play_animation(conn, g, _ANIM_PICKUP,
                                              _ANIM_STATE_DIRECT, 16)
                await asyncio.sleep(0.3)
                info = loot.remove_drop(eid)
                if info is None:
                    continue
                gold = self._conversion_gold(conn, info)
                self._broadcast_framed(conn, self._build_drop_remove(eid))
                self._credit_gold(conn, gold)
                g.items_converted += 1
                g.gold_generated += gold
                log.info(f"[GNOME-CONVERT] '{g.owner_login}' "
                         f"item 0x{eid:04X} '{info.gc_class}' -> {gold}g "
                         f"(total {g.gold_generated}g)")
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        except Exception as ex:  # noqa: BLE001
            log.error(f"[GNOME-CONVERT] failed for '{g.owner_login}': {ex}")

    def _drops_near(self, conn: "RRConnection", x: float, y: float,
                    radius: float) -> List[Tuple[int, object]]:
        from . import loot
        return loot.drops_near(conn.current_zone_gc_type or "",
                               conn.instance_id, x, y, radius)

    @staticmethod
    def _can_convert(drop) -> bool:
        """Rare-and-below, never gold/quest/unique/collection/mythic (DRS-NET
        CanConvertItem; the skill text: 'Rare quality and lower')."""
        if drop.gold_amount > 0 or not drop.gc_class:
            return False
        if drop.rarity > MAX_CONVERT_RARITY:
            return False
        gc = drop.gc_class.lower()
        return not any(tag in gc for tag in
                       ("quest", "unique", "collection", "mythic"))

    def _conversion_gold(self, conn: "RRConnection", drop) -> int:
        """base gold value × ConversionRatio 0.5 × GoldValueMod 2.3 (DRS-NET
        CalculateConversionGold; base from the item catalog, fallback
        level×2)."""
        from ..data import item_catalog
        base = item_catalog.get_buy_price(drop.gc_class)
        if base <= 0:
            base = max(1, (getattr(conn, "player_level", 1) or 1) * 2)
        return max(1, int(base * CONVERSION_RATIO * GOLD_VALUE_MOD))

    def _credit_gold(self, conn: "RRConnection", amount: int) -> None:
        """Persist + ship the 0x20 AddCurrency (same shape as the pickup
        handler / DRS-NET BlingGnomeCreditGold)."""
        if amount <= 0:
            return
        from ..db import character_repository
        from ..net.component_update import write_synch, synch_hp
        try:
            saved = character_repository.get_character(conn.char_sql_id)
            if saved is not None:
                saved.gold += amount
                character_repository.save_character(saved)
        except Exception as ex:  # noqa: BLE001
            log.warn(f"[GNOME-GOLD] persist failed for '{conn.login_name}': {ex}")
        if not getattr(conn, "unit_container_id", 0):
            return
        w = LEWriter()
        w.write_byte(0x07)
        w.write_byte(0x35)
        w.write_uint16(conn.unit_container_id)
        w.write_byte(0x20)                       # AddCurrency
        w.write_uint32(amount)
        w.write_byte(0x00)
        w.write_uint32(0)
        w.write_byte(0x01)
        write_synch(w, synch_hp(conn))
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())

    @staticmethod
    def _build_drop_remove(entity_id: int) -> bytes:
        w = LEWriter()
        w.write_byte(0x07)
        w.write_byte(0x05)
        w.write_uint16(entity_id)
        w.write_byte(0x06)
        return w.to_array()

    # ── Send helpers ─────────────────────────────────────────────────────

    def _send_play_animation(self, conn: "RRConnection", g: GnomeState,
                             logical_id: int, anim_state: int,
                             duration_ticks: int) -> None:
        if not g.behavior_created:
            return
        w = LEWriter()
        self._write_play_animation_submsg(w, g, logical_id, anim_state,
                                          duration_ticks)
        self._enqueue_interval(conn, w.to_array())

    def _send_convert_action(self, conn: "RRConnection", g: GnomeState) -> None:
        if not g.behavior_created:
            return
        self._enqueue_interval(conn, self.build_convert_action(
            g, conn.player_pos_x, conn.player_pos_y))

    def _send_mover_update(self, conn: "RRConnection", g: GnomeState,
                           terminal: bool, now: float) -> None:
        if not g.behavior_created:
            return
        self._enqueue_interval(conn, self.build_mover_update(g, terminal))
        g.mover_valid = True
        g.mover_at = now
        g.mover_x, g.mover_y = g.pos_x, g.pos_y
        g.mover_heading = g.heading
        g.mover_terminal = terminal

    def _enqueue_interval(self, conn: "RRConnection", packet: bytes) -> None:
        """Queue an UNFRAMED behavior update for the owner + instance peers on
        the interval queue (drained inside each player's 0x0D frame)."""
        conn.interval_message_queue.enqueue(packet)
        for peer in self._instance_peers(conn):
            peer.interval_message_queue.enqueue(packet)

    def _broadcast_framed(self, conn: "RRConnection", packet: bytes,
                          include_owner: bool = True) -> None:
        if include_owner:
            conn.send_to_client(packet)
        for peer in self._instance_peers(conn):
            peer.send_to_client(packet)

    def _instance_peers(self, conn: "RRConnection") -> List["RRConnection"]:
        return [other for other in self._server.connections.values()
                if other is not conn and other.is_spawned
                and self._same_instance(conn, other)]

    @staticmethod
    def _same_instance(a: "RRConnection", b: "RRConnection") -> bool:
        return (a.current_zone_gc_type == b.current_zone_gc_type
                and a.instance_id == b.instance_id)

    def _conn_by_login(self, login: str) -> Optional["RRConnection"]:
        for conn in self._server.connections.values():
            if conn.login_name == login:
                return conn
        return None

    def _own_owner_ref(self, conn: "RRConnection") -> int:
        """Owner entity ref for the OWNER's client: the Player OBJECT id (the
        client binds ownerID to a Player for henchman/nameplate resolution),
        falling back to the avatar id (DRS-NET: conn.Player.Id then
        GetPlayerAvatarId)."""
        player = getattr(conn, "player", None)
        player_id = getattr(player, "id", 0) if player is not None else 0
        if player_id:
            return player_id
        return self._server.get_player_avatar_id(conn.login_name)

    def _remote_owner_ref(self, viewer: "RRConnection", owner_login: str) -> int:
        """The owner entity id AS THE VIEWER KNOWS IT — the viewer's remapped
        remote Player-object id for the owner, falling back to the remote
        avatar id (DRS-NET ResolveRemotePlayerEntityId order)."""
        player_map = self._server.remote_player_ids.get(viewer.login_name or "")
        if player_map and player_map.get(owner_login):
            return player_map[owner_login]
        avatar_map = self._server.remote_avatar_ids.get(viewer.login_name or "")
        if avatar_map and owner_login in avatar_map:
            return avatar_map[owner_login]
        return 0

    async def _delayed_entity_remove(self, conn: "RRConnection",
                                     g: GnomeState, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self._broadcast_framed(conn, self._build_entity_remove(g),
                                   include_owner=conn.is_connected)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _cancel_tasks(g: GnomeState) -> None:
        for task in (g.behavior_task, g.conversion_task):
            if task is not None:
                task.cancel()
        g.behavior_task = g.conversion_task = None
        g.behavior_created = False

    def _pick_fidget_animation(self, g: GnomeState) -> int:
        candidates = (101, 102, 103, 104)
        idx = random.randrange(len(candidates))
        anim = candidates[idx]
        if anim == g.last_fidget_anim:
            anim = candidates[(idx + 1) % len(candidates)]
        g.last_fidget_anim = anim
        return anim

    @staticmethod
    def _fidget_delay() -> float:
        return IDLE_BASE_SECONDS + random.random() * IDLE_VARIABLE_SECONDS
