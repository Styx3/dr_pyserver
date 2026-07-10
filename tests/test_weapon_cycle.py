"""Tests for the native melee weapon-cycle cadence (Step 4 of ROUTE 2B).

Pins the 30Hz swing-cadence core ported from DR-Server's
``Combat/WeaponCycleTracker.cs`` against:
  * ``WeaponCycleTracker.NativeTickIndexFromTime`` (binary 0x591980 tick model),
  * the embedded ``NativeDamageReplaySelfTest.RunStarterCrossbowCycleReplay``
    oracle (numFrames 17 / trigger 2 / sound 2 / speed 95 -> cycle 17, event 3),
  * the documented melee defaults (Small Club speed 105 -> cycle 28, hit 14,
    sound 9), and
  * the per-swing state machine (hit fires once at the hit tick, the cycle
    resets at cycle-end, kills surface via DequeueKill).

Projectile/ranged flight, scheduler queues and spell paths are deliberately
out of scope here (Step 4 is melee-only).
"""

from __future__ import annotations

import pytest

from drserver.combat.rng import MersenneTwister
from drserver.combat.weapon_cycle import (
    DEFAULT_HIT_POSITION,
    DEFAULT_SOUND_POSITION,
    DEFAULT_TOTAL_TICKS,
    NATIVE_UPDATE_TICK,
    CompletedAttack,
    WeaponCycleHost,
    WeaponCycleTracker,
    native_cycle_ticks,
    native_tick_index_from_time,
    native_tick_position,
    resolve_native_basic_attack_cooldown_ticks,
)


class FakeMonster:
    """Minimal monster stand-in for cadence tests."""

    def __init__(self, entity_id: int = 0x0547, name: str = "warg_pup", alive: bool = True):
        self.entity_id = entity_id
        self.name = name
        self.is_alive = alive


# --------------------------------------------------------------------------- #
# NativeTickIndexFromTime (binary 0x591980 / FloorToInt(time/tick + 0.0001))
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_native_tick_index_zero_and_negative_time_is_zero():
    # Arrange / Act / Assert
    assert native_tick_index_from_time(0.0) == 0
    assert native_tick_index_from_time(-1.0) == 0


@pytest.mark.unit
def test_native_tick_index_one_tick():
    assert native_tick_index_from_time(NATIVE_UPDATE_TICK) == 1


@pytest.mark.unit
def test_native_tick_index_hit_frame_and_half_second():
    # 14 ticks in, then 15 ticks (0.5s) in
    assert native_tick_index_from_time(14.0 / 30.0) == 14
    assert native_tick_index_from_time(0.5) == 15


# --------------------------------------------------------------------------- #
# Tick-position math — pinned to RunStarterCrossbowCycleReplay self-test
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_starter_crossbow_cycle_replay_oracle():
    # NativeDamageReplaySelfTest.RunStarterCrossbowCycleReplay constants:
    #   numFrames=17, triggerTime=2, soundTriggerTime=2, weaponSpeed=95, ranged
    num_frames, trigger_time, sound_time, speed = 17, 2, 2, 95

    cycle_ticks = native_cycle_ticks(num_frames, speed)
    base_trigger = native_tick_position(trigger_time, speed)
    base_sound = native_tick_position(sound_time, speed)
    # ranged adds +1, clamped to the cycle length
    trigger_tick = min(cycle_ticks, base_trigger + 1)
    sound_tick = min(cycle_ticks, base_sound + 1)

    assert cycle_ticks == 17
    assert trigger_tick == 3
    assert sound_tick == 3


@pytest.mark.unit
def test_melee_default_small_club_cadence():
    # Small Club: WeaponSpeed field 105, default frames 30/15/10.
    speed = 105
    assert native_cycle_ticks(DEFAULT_TOTAL_TICKS, speed) == 28
    assert native_tick_position(DEFAULT_HIT_POSITION, speed) == 14
    assert native_tick_position(DEFAULT_SOUND_POSITION, speed) == 9


@pytest.mark.unit
def test_default_basic_attack_cooldown_ticks():
    # No PlayerState -> cooldown 0 -> fallback 30 ticks * (100/100) -> 30.
    assert resolve_native_basic_attack_cooldown_ticks(None) == 30


# --------------------------------------------------------------------------- #
# Cadence state machine
# --------------------------------------------------------------------------- #


def _begin_and_collect_hits(tracker: WeaponCycleTracker, conn_key: str, ticks: int,
                            now0: float = 1.0):
    """Drive `ticks` native ticks; return the tick counters at which hits fired."""
    hit_ticks: list[int] = []
    cycle = tracker._active_cycles[conn_key]  # noqa: SLF001 — test introspection
    for k in range(1, ticks + 1):
        before = cycle.hit_fired
        tracker.tick(conn_key, now0 + k * NATIVE_UPDATE_TICK)
        if cycle.hit_fired and not before:
            hit_ticks.append(cycle.tick_counter)
    return hit_ticks


@pytest.mark.unit
def test_register_attack_starts_active_cycle():
    tracker = WeaponCycleTracker()
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0)

    cycle = tracker._active_cycles["conn1"]
    assert cycle.is_active is True
    assert cycle.awaiting_contact is False
    assert cycle.target_id == 0x0547


@pytest.mark.unit
def test_hit_fires_once_at_hit_tick_for_default_melee():
    tracker = WeaponCycleTracker()
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0)

    hits = _begin_and_collect_hits(tracker, "conn1", ticks=28)

    # Default melee: hit event tick == 14, fires exactly once per cycle.
    assert hits == [14]


@pytest.mark.unit
def test_cycle_resets_at_cycle_end():
    tracker = WeaponCycleTracker()
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0)
    cycle = tracker._active_cycles["conn1"]

    # Drive a full 28-tick cycle; with no repeat queued the cycle stops.
    for k in range(1, 29):
        tracker.tick("conn1", 1.0 + k * NATIVE_UPDATE_TICK)

    assert cycle.tick_counter == 0
    assert cycle.is_active is False


@pytest.mark.unit
def test_kill_at_hit_tick_is_enqueued_and_dequeued():
    class KillingHost(WeaponCycleHost):
        def resolve_hit(self, cycle, rng):
            return True, 114  # killed, applied damage

    tracker = WeaponCycleTracker(host=KillingHost())
    monster = FakeMonster(entity_id=0x0547)
    tracker.register_attack("conn1", 0x0547, monster=monster, now=1.0)

    for k in range(1, 15):  # reach the hit tick (14)
        tracker.tick("conn1", 1.0 + k * NATIVE_UPDATE_TICK)

    assert tracker.has_pending_kills is True
    kill = tracker.dequeue_kill()
    assert isinstance(kill, CompletedAttack)
    assert kill.killed is True
    assert kill.monster is monster
    assert kill.damage_dealt == 114
    assert tracker.dequeue_kill() is None
    # cycle deactivated on kill
    assert tracker._active_cycles["conn1"].is_active is False


@pytest.mark.unit
def test_use_rng_consumed_once_per_cycle_and_updates_anim_index():
    tracker = WeaponCycleTracker()
    rng = MersenneTwister(0x1A2B3C4D)
    before_pos = rng.calls_since_reseed
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0, rng=rng)

    cycle = tracker._active_cycles["conn1"]
    # BeginCycle consumes exactly one use-RNG draw for melee.
    assert rng.calls_since_reseed == before_pos + 1
    # anim index = ((useRaw & 1) + prev(0) + 1) % 3
    assert cycle.attack_animation_index == ((cycle.use_raw & 1) + 0 + 1) % 3


@pytest.mark.unit
def test_approach_intent_when_cannot_start_now():
    tracker = WeaponCycleTracker()
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0,
                            can_start_now=False)

    cycle = tracker._active_cycles["conn1"]
    assert cycle.is_active is False
    assert cycle.awaiting_contact is True


@pytest.mark.unit
def test_repeat_use_queued_on_redundant_use_target():
    tracker = WeaponCycleTracker()
    monster = FakeMonster(entity_id=0x0547)
    tracker.register_attack("conn1", 0x0547, monster=monster, now=1.0)
    # Same target while the cycle is active -> queues a repeat use.
    tracker.register_attack("conn1", 0x0547, monster=monster, now=1.0)

    cycle = tracker._active_cycles["conn1"]
    assert cycle.pending_repeat_uses == 1


@pytest.mark.unit
def test_out_of_contact_holds_in_awaiting_state():
    class NoContactHost(WeaponCycleHost):
        def has_contact(self, cycle):
            return False

    tracker = WeaponCycleTracker(host=NoContactHost())
    # Approach intent (cannot start) -> stays awaiting while out of contact.
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0,
                            can_start_now=False)
    cycle = tracker._active_cycles["conn1"]

    for k in range(1, 10):
        tracker.tick("conn1", 1.0 + k * NATIVE_UPDATE_TICK)

    assert cycle.is_active is False
    assert cycle.awaiting_contact is True
    assert cycle.hit_fired is False


@pytest.mark.unit
def test_dead_monster_deactivates_cycle():
    tracker = WeaponCycleTracker()
    monster = FakeMonster()
    tracker.register_attack("conn1", 0x0547, monster=monster, now=1.0)

    monster.is_alive = False
    tracker.tick("conn1", 1.0 + NATIVE_UPDATE_TICK)

    cycle = tracker._active_cycles["conn1"]
    assert cycle.is_active is False
    assert cycle.awaiting_contact is False


@pytest.mark.unit
def test_clear_connection_and_clear():
    tracker = WeaponCycleTracker()
    tracker.register_attack("conn1", 0x0547, monster=FakeMonster(), now=1.0)
    tracker.register_attack("conn2", 0x054C, monster=FakeMonster(), now=1.0)

    tracker.clear_connection("conn1")
    assert "conn1" not in tracker._active_cycles
    assert "conn2" in tracker._active_cycles

    tracker.clear()
    assert not tracker._active_cycles
    assert tracker.has_pending_kills is False
