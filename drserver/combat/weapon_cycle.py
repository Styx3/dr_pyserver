"""Native melee weapon-cycle cadence (ROUTE 2B, Step 4).

Port of the *melee* swing-cadence core of DR-Server's
``Combat/WeaponCycleTracker.cs`` (1690 lines).  The C# class tracks weapon
cycle timing so combat RNG fires at the correct 30Hz tick positions, mirroring
the client's ``MeleeWeapon::update`` at binary 0x591980.

Scope (deliberately melee-only — see ROUTE 2B plan):
  * ``native_tick_index_from_time`` and the per-swing tick model
    (cycle length / hit tick / sound tick) derived from the weapon-speed field,
  * ``WeaponCycleTracker.register_attack`` / ``tick`` / ``tick_player_entity``
    and the active-cycle state machine (begin → tick → hit → repeat/stop),
  * kill surfacing via ``dequeue_kill`` and :class:`CompletedAttack`.

Deliberately **out of scope** (ranged-only / later steps):
  * projectile flight, impact-delay scheduling, ``DrainDueProjectileImpacts``
    and the ``NativeDueEventScheduler`` queue,
  * spell / weapon-skill damage paths.

The damage resolution, monster-HP model, contact test and equipment-derived
weapon knobs are abstracted behind :class:`WeaponCycleHost` so this layer stays
pure and testable.  Step 6 (kill detection in ``CombatManager``) supplies a
real host that runs ``resolve_native_weapon_damage`` and applies monster HP.

Default melee knobs mirror the starter Small Club documented in the C# source:
WeaponSpeed field 105, default animation frames total=30 / hit=15 / sound=10,
which normalize to a 28-tick cycle with the hit at tick 14 and sound at tick 9.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from drserver.combat.rng import MersenneTwister

# --------------------------------------------------------------------------- #
# Constants (WeaponCycleTracker.cs:44-50)
# --------------------------------------------------------------------------- #

DEFAULT_TOTAL_TICKS = 30
DEFAULT_SOUND_POSITION = 10
DEFAULT_HIT_POSITION = 15
NATIVE_UPDATE_TICK = 1.0 / 30.0
MAX_PENDING_REPEAT_USES = 16

#: Starter weapon-speed field ([edi+0x90]); Small Club = 105.
DEFAULT_WEAPON_SPEED_FIELD = 105


# --------------------------------------------------------------------------- #
# Pure tick math (static methods of WeaponCycleTracker / DamageComputer)
# --------------------------------------------------------------------------- #


def native_tick_index_from_time(time: float) -> int:
    """``WeaponCycleTracker.NativeTickIndexFromTime`` (cs:61).

    ``Mathf.Max(0, Mathf.FloorToInt(time / (1/30) + 0.0001))`` with a 0 floor.
    """
    if time <= 0.0:
        return 0
    return max(0, math.floor((time / NATIVE_UPDATE_TICK) + 0.0001))


def native_tick_position(default_position: int, speed_field: int) -> int:
    """``GetNativeTickPosition`` / ``GetNativeCycleTickCount`` (cs:388-396).

    ``Math.Max(1, (defaultPosition * 100) / speedField)`` — C# integer division,
    which equals Python floor-division for the non-negative operands here.
    """
    speed_field = max(1, speed_field)
    return max(1, (default_position * 100) // speed_field)


def native_cycle_ticks(total_frames: int, speed_field: int) -> int:
    """Total ticks in a cycle for the given animation frame count + speed."""
    total_frames = total_frames if total_frames > 0 else DEFAULT_TOTAL_TICKS
    return native_tick_position(total_frames, speed_field)


def _round_positive_to_int(value: float) -> int:
    """``DamageComputer.RoundPositiveToInt`` (cs:230) — floor(value + 0.5)."""
    if value <= 0.0:
        return 0
    return math.floor(value + 0.5)


def apply_native_attack_speed_pct_to_ticks(ticks: int, pct: float) -> int:
    """``DamageComputer.ApplyNativeAttackSpeedPctToTicks`` (cs:202)."""
    if ticks <= 0 or abs(pct) < 0.0001:
        return max(0, ticks)
    scale = 1.0 + (pct / 100.0)
    if scale < 0.05:
        scale = 0.05
    adjusted = math.floor((ticks / scale) + 0.5)
    return max(1, adjusted)


def resolve_native_basic_attack_cooldown_ticks(
    state: object | None,
    *,
    weapon_cooldown: float = 0.0,
    weapon_speed: float = 0.0,
    attack_speed_pct: float = 0.0,
) -> int:
    """``DamageComputer.ResolveNativeBasicAttackCooldownTicks`` (cs:211).

    The default (no equipment knobs) yields 30 ticks (= 1.0s).  Real equipment
    values are supplied by the host once :class:`PlayerState` wiring lands
    (Step 6); this keeps the pure default path here.
    """
    ticks = 0
    if weapon_cooldown > 0.0:
        ticks = _round_positive_to_int(weapon_cooldown * 30.0)

    if ticks <= 0:
        ticks = 30
        ws = weapon_speed if weapon_speed > 0.0 else 100.0
        adjusted = _round_positive_to_int(ticks * (ws / 100.0))
        if adjusted > 0:
            ticks = adjusted

    ticks = apply_native_attack_speed_pct_to_ticks(ticks, attack_speed_pct)
    return max(1, ticks)


# --------------------------------------------------------------------------- #
# Data carriers (WeaponCycle / CompletedAttack)
# --------------------------------------------------------------------------- #


@dataclass
class WeaponCycle:
    """Per-connection active weapon cycle (WeaponCycleTracker.cs:1461).

    Melee-relevant subset; projectile-only fields are omitted.
    """

    target_id: int = 0
    monster: object | None = None
    player_state: object | None = None
    connection: object | None = None
    player_entity_id: int = 0

    is_active: bool = False
    awaiting_contact: bool = False
    server_approach_only: bool = False

    tick_counter: int = 0
    cycle_start_time: float = 0.0
    last_tick_time: float = 0.0
    next_use_time: float = 0.0

    proc_fired: bool = False
    hit_fired: bool = False
    attack_sound_fired: bool = False

    use_rng_consumed: bool = False
    use_raw: int = 0
    attack_animation_index: int = 0

    swing_count: int = 0
    pending_repeat_uses: int = 0

    attack_total_frames: int = 0
    attack_hit_frame: int = 0
    attack_sound_frame: int = 0
    attack_animation_id: int = 0


@dataclass
class CompletedAttack:
    """A resolved swing that killed its target (WeaponCycleTracker.cs:1495)."""

    conn_key: str
    monster: object | None
    killed: bool = False
    damage_dealt: int = 0
    connection: object | None = None


# --------------------------------------------------------------------------- #
# Host seam — damage / monster / contact / weapon-knob resolution
# --------------------------------------------------------------------------- #


class WeaponCycleHost:
    """Default melee host.

    Overridden by the combat manager (Step 6) to plug in real damage
    resolution, monster HP and equipment knobs.  The defaults model an
    in-range melee swing with the starter Small Club and no damage applied
    (pure-cadence behaviour).
    """

    def monster_alive(self, cycle: WeaponCycle) -> bool:
        monster = cycle.monster
        if monster is None:
            return False
        return bool(getattr(monster, "is_alive", True))

    def has_contact(self, cycle: WeaponCycle) -> bool:
        return True

    def is_ranged(self, cycle: WeaponCycle) -> bool:
        return False

    def resolve_speed_field(self, cycle: WeaponCycle) -> int:
        return DEFAULT_WEAPON_SPEED_FIELD

    def resolve_frames(self, cycle: WeaponCycle) -> tuple[int, int, int]:
        """Return (total_frames, hit_frame, sound_frame)."""
        return (DEFAULT_TOTAL_TICKS, DEFAULT_HIT_POSITION, DEFAULT_SOUND_POSITION)

    def resolve_cooldown_ticks(self, cycle: WeaponCycle) -> int:
        return resolve_native_basic_attack_cooldown_ticks(cycle.player_state)

    def resolve_hit(self, cycle: WeaponCycle, rng: Optional[MersenneTwister]):
        """Resolve a landed swing.

        Return ``(killed: bool, applied_damage: int)``.  The default applies no
        damage (cadence only); Step 6 runs ``resolve_native_weapon_damage`` and
        the monster-HP model here.
        """
        return (False, 0)


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #


class WeaponCycleTracker:
    """Ports the melee subset of ``WeaponCycleTracker`` (cs:31)."""

    def __init__(self, host: WeaponCycleHost | None = None):
        self._host = host or WeaponCycleHost()
        self._active_cycles: dict[str, WeaponCycle] = {}
        self._completed_attacks: list[CompletedAttack] = []

    # -- tick-model helpers (instance variants of the static math) ---------- #

    def _speed_field(self, cycle: WeaponCycle) -> int:
        return max(1, self._host.resolve_speed_field(cycle))

    def _cycle_ticks(self, cycle: WeaponCycle) -> int:
        total = cycle.attack_total_frames if cycle.attack_total_frames > 0 else DEFAULT_TOTAL_TICKS
        return native_cycle_ticks(total, self._speed_field(cycle))

    def _hit_tick(self, cycle: WeaponCycle) -> int:
        hit_frame = cycle.attack_hit_frame if cycle.attack_hit_frame > 0 else DEFAULT_HIT_POSITION
        return native_tick_position(hit_frame, self._speed_field(cycle))

    def _sound_tick(self, cycle: WeaponCycle) -> int:
        sound_frame = cycle.attack_sound_frame if cycle.attack_sound_frame > 0 else DEFAULT_SOUND_POSITION
        return native_tick_position(sound_frame, self._speed_field(cycle))

    def _hit_event_tick(self, cycle: WeaponCycle) -> int:
        tick = self._hit_tick(cycle)
        if self._host.is_ranged(cycle):
            return min(self._cycle_ticks(cycle), tick + 1)
        return tick

    def _sound_event_tick(self, cycle: WeaponCycle) -> int:
        tick = self._sound_tick(cycle)
        if self._host.is_ranged(cycle):
            return min(self._cycle_ticks(cycle), tick + 1)
        return tick

    def _is_use_ready(self, cycle: WeaponCycle, now: float) -> bool:
        return cycle.next_use_time <= 0.0 or now + 0.0001 >= cycle.next_use_time

    # -- registration (UseTarget) ------------------------------------------- #

    def register_attack(
        self,
        conn_key: str,
        target_id: int,
        *,
        monster: object | None = None,
        player_state: object | None = None,
        conn: object | None = None,
        player_entity_id: int = 0,
        can_start_now: bool = True,
        now: float = 0.0,
        rng: MersenneTwister | None = None,
    ) -> None:
        """``WeaponCycleTracker.RegisterAttack`` (cs:152) — melee subset."""
        cycle = self._active_cycles.get(conn_key)
        if cycle is None:
            cycle = WeaponCycle()
            self._active_cycles[conn_key] = cycle

        same_monster = (
            cycle.monster is not None
            and monster is not None
            and getattr(cycle.monster, "entity_id", None) == getattr(monster, "entity_id", None)
        )
        same_target = cycle.target_id == target_id and same_monster

        if (cycle.is_active or cycle.awaiting_contact) and same_target:
            cycle.monster = monster
            cycle.player_state = player_state
            cycle.connection = conn
            if player_entity_id:
                cycle.player_entity_id = player_entity_id
            if cycle.is_active and can_start_now:
                self._queue_repeat_use(cycle, monster)
            if can_start_now and cycle.awaiting_contact:
                cycle.server_approach_only = False
                if not self._is_use_ready(cycle, now):
                    self._queue_repeat_use(cycle, monster)
                    cycle.is_active = False
                    cycle.awaiting_contact = True
                    cycle.last_tick_time = now
                    return
                self._begin_cycle(cycle, target_id, monster, now, rng)
            return

        # Fresh cycle / new target.
        cycle.target_id = target_id
        cycle.monster = monster
        cycle.player_state = player_state
        cycle.connection = conn
        if player_entity_id:
            cycle.player_entity_id = player_entity_id
        cycle.tick_counter = 0
        self._reset_swing_rng_state(cycle)
        cycle.swing_count = 0
        cycle.pending_repeat_uses = 0
        cycle.server_approach_only = not can_start_now
        cycle.last_tick_time = now
        cycle.cycle_start_time = 0.0

        if can_start_now:
            if not self._is_use_ready(cycle, now):
                cycle.is_active = False
                cycle.awaiting_contact = True
                cycle.server_approach_only = False
                cycle.last_tick_time = now
                return
            self._begin_cycle(cycle, target_id, monster, now, rng)
        else:
            cycle.is_active = False
            cycle.awaiting_contact = True

    def _queue_repeat_use(self, cycle: WeaponCycle, monster: object | None) -> None:
        """``QueueNativeRepeatUse`` (cs:248) — melee subset."""
        if cycle is None or monster is None or not getattr(monster, "is_alive", True):
            return
        cycle.pending_repeat_uses = min(MAX_PENDING_REPEAT_USES, cycle.pending_repeat_uses + 1)

    def _reset_swing_rng_state(self, cycle: WeaponCycle) -> None:
        """``ResetSwingRngState`` (cs:470)."""
        cycle.proc_fired = False
        cycle.hit_fired = False
        cycle.attack_sound_fired = False
        cycle.use_rng_consumed = False
        cycle.use_raw = 0

    def _begin_cycle(
        self,
        cycle: WeaponCycle,
        target_id: int,
        monster: object | None,
        now: float,
        rng: MersenneTwister | None,
    ) -> None:
        """``BeginCycle`` (cs:451)."""
        cycle.is_active = True
        cycle.awaiting_contact = False
        cycle.server_approach_only = False
        cycle.target_id = target_id
        cycle.monster = monster
        cycle.tick_counter = 0
        self._reset_swing_rng_state(cycle)
        cycle.last_tick_time = now
        cycle.cycle_start_time = now
        self._consume_use_rng(cycle, rng)
        self._resolve_attack_frames(cycle)
        cooldown_ticks = self._host.resolve_cooldown_ticks(cycle)
        cycle.next_use_time = now + cooldown_ticks * NATIVE_UPDATE_TICK

    def _consume_use_rng(self, cycle: WeaponCycle, rng: MersenneTwister | None) -> None:
        """``ConsumeNativeUseRng`` (cs:577) — melee path only.

        Melee consumes exactly one draw and advances the animation index:
        ``anim = ((useRaw & 1) + prev + 1) % 3``.  Ranged consumes no room RNG.
        """
        if cycle.use_rng_consumed:
            return
        if rng is None or self._host.is_ranged(cycle):
            cycle.use_raw = 0
            cycle.use_rng_consumed = True
            return
        cycle.use_raw = rng.generate()
        cycle.use_rng_consumed = True
        prev = cycle.attack_animation_index
        cycle.attack_animation_index = ((cycle.use_raw & 1) + prev + 1) % 3

    def _resolve_attack_frames(self, cycle: WeaponCycle) -> None:
        """``ResolveNativeAttackFrames`` (cs:484) — host-supplied frames.

        Animation-list lookup against the GC database is deferred; the host
        returns the frame triple (defaults to 30/15/10).
        """
        total, hit, sound = self._host.resolve_frames(cycle)
        cycle.attack_total_frames = max(1, total)
        cycle.attack_hit_frame = max(1, hit)
        cycle.attack_sound_frame = max(1, sound)

    # -- ticking ------------------------------------------------------------ #

    def tick(self, conn_key: str, now: float, rng: MersenneTwister | None = None) -> None:
        cycle = self._active_cycles.get(conn_key)
        if cycle is not None:
            self._advance_cycle_to_now(cycle, now, rng)

    def tick_all(self, now: float, rng: MersenneTwister | None = None) -> None:
        for cycle in list(self._active_cycles.values()):
            self._advance_cycle_to_now(cycle, now, rng)

    def tick_player_entity(
        self, player_entity_id: int, now: float, rng: MersenneTwister | None = None
    ) -> None:
        """``TickPlayerEntity`` (cs:280) — advance the cycle for one avatar."""
        for cycle in self._active_cycles.values():
            if cycle.player_entity_id == player_entity_id and player_entity_id != 0:
                self._advance_cycle_to_now(cycle, now, rng)
                return

    def _advance_cycle_to_now(
        self, cycle: WeaponCycle, now: float, rng: MersenneTwister | None
    ) -> None:
        """``AdvanceCycleToNow`` (cs:292) — one native tick per call."""
        if not cycle.is_active and not cycle.awaiting_contact:
            return
        interval = NATIVE_UPDATE_TICK
        if cycle.last_tick_time > 0.0 and now - cycle.last_tick_time + 0.0001 < interval:
            return
        self._tick_cycle(cycle, now, rng)

    def _tick_cycle(self, cycle: WeaponCycle, now: float, rng: MersenneTwister | None) -> None:
        """``TickCycle`` (cs:595) — melee path."""
        if cycle.monster is None or not self._host.monster_alive(cycle):
            cycle.is_active = False
            cycle.awaiting_contact = False
            return

        if cycle.awaiting_contact:
            if not self._host.has_contact(cycle):
                cycle.last_tick_time = now
                return
            if not self._is_use_ready(cycle, now):
                cycle.last_tick_time = now
                return
            self._begin_cycle(cycle, cycle.target_id, cycle.monster, now, rng)

        if not cycle.is_active:
            return

        interval = NATIVE_UPDATE_TICK
        if cycle.last_tick_time > 0.0 and now - cycle.last_tick_time + 0.0001 < interval:
            return
        if cycle.last_tick_time > 0.0:
            cycle.last_tick_time += interval
            tick_now = cycle.last_tick_time
        else:
            tick_now = now
            cycle.last_tick_time = tick_now
        cycle.tick_counter += 1

        if cycle.tick_counter == self._sound_event_tick(cycle) and not cycle.proc_fired:
            cycle.proc_fired = True
            cycle.attack_sound_fired = True

        if cycle.tick_counter == self._hit_event_tick(cycle) and not cycle.hit_fired:
            cycle.hit_fired = True
            cycle.swing_count += 1
            self._consume_use_rng(cycle, rng)
            killed, applied_damage = self._host.resolve_hit(cycle, rng)
            if killed and cycle.monster is not None:
                self._completed_attacks.append(
                    CompletedAttack(
                        conn_key=self._conn_key_for(cycle),
                        connection=cycle.connection,
                        monster=cycle.monster,
                        killed=True,
                        damage_dealt=applied_damage,
                    )
                )
                cycle.is_active = False

        if cycle.tick_counter >= self._cycle_ticks(cycle):
            self._on_cycle_complete(cycle, tick_now, rng)

    def _on_cycle_complete(
        self, cycle: WeaponCycle, tick_now: float, rng: MersenneTwister | None
    ) -> None:
        """Cycle-end handling (cs:808) — repeat or stop. Projectiles omitted."""
        repeat_queued = (
            cycle.pending_repeat_uses > 0
            and cycle.monster is not None
            and getattr(cycle.monster, "is_alive", True)
        )
        cycle.tick_counter = 0
        self._reset_swing_rng_state(cycle)
        cycle.cycle_start_time = 0.0
        cycle.last_tick_time = tick_now
        cycle.is_active = False
        cycle.server_approach_only = False

        if repeat_queued:
            cycle.pending_repeat_uses -= 1
            if self._host.has_contact(cycle):
                if self._is_use_ready(cycle, tick_now):
                    self._begin_cycle(cycle, cycle.target_id, cycle.monster, tick_now, rng)
                else:
                    cycle.awaiting_contact = True
            else:
                cycle.awaiting_contact = True
            return

        cycle.awaiting_contact = False

    def _conn_key_for(self, cycle: WeaponCycle) -> str:
        for key, value in self._active_cycles.items():
            if value is cycle:
                return key
        return ""

    # -- kill queue / lifecycle --------------------------------------------- #

    def dequeue_kill(self) -> CompletedAttack | None:
        """``DequeueKill`` (cs:1435)."""
        if self._completed_attacks:
            return self._completed_attacks.pop(0)
        return None

    @property
    def has_pending_kills(self) -> bool:
        return bool(self._completed_attacks)

    def get_active_target(self, conn_key: str) -> object | None:
        cycle = self._active_cycles.get(conn_key)
        if cycle is not None and (cycle.is_active or cycle.awaiting_contact):
            return cycle.monster
        return None

    def clear_connection(self, conn_key: str) -> None:
        """``ClearConnection`` (cs:1442)."""
        self._active_cycles.pop(conn_key, None)

    def clear(self) -> None:
        """``Clear`` (cs:1452)."""
        self._active_cycles.clear()
        self._completed_attacks.clear()
