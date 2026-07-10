"""Server-side melee kill detection by replaying the client swing stream
(ROUTE 2B, Step 6 — the payoff that wires Steps 1–5 together).

The vanilla client is *packet-proven blind*: it self-levels locally on kills and
never reports monster HP (or its own level-up) over TCP.  So the server cannot
learn of a kill from the client; it must **replay** the swings the client sends
(ch7/0x34 ``0904 0100 50 <sid> <target>``) through the native damage path and
detect the kill itself.

This module ties together:
  * :class:`WeaponCycleTracker` (Step 4) — the 30Hz swing cadence,
  * :func:`resolve_native_weapon_damage` (Step 3) — the per-swing damage roll,
  * the tracked-monster HP model (``CombatManager``),

and, on a replayed kill, invokes an ``on_kill`` finalize callback — the same
death pipeline (``CombatManager._process_monster_kill`` →
``GameServer.award_kill_xp`` → ``_refresh_avatar_hp_wire``) the dead client-HP
trigger used to feed.  That raises ``conn.hp_wire`` to the leveled value the same
tick the client self-levels, eliminating the fatal Avatar type-2 synch crash.

Mirrors ``UnityGameServer.DrainWeaponCycleKills`` (cs:9970) and the
``RegisterAttack`` / ``TickPlayerEntity`` call sites; projectile/spell/control
machinery is out of scope (melee path only).

The per-swing damage *input* (equipment/stat-derived AR/DR/weapon knobs) is the
remaining Step-3 deferral: it is supplied by an injected resolver so this glue
stays testable and faithful before the ``PlayerState``/``Monster`` stat
resolvers land.
"""

from __future__ import annotations

from typing import Callable, Optional

from drserver.combat.damage_computer import (
    NativeWeaponDamageInput,
    resolve_native_weapon_damage,
)
from drserver.combat.rng import MersenneTwister
from drserver.combat.weapon_cycle import (
    WeaponCycle,
    WeaponCycleHost,
    WeaponCycleTracker,
    resolve_native_basic_attack_cooldown_ticks,
)

#: ``resolve_damage(cycle, rng) -> (hit, blocked, damage_wire)``.
DamageResolver = Callable[[WeaponCycle, Optional[MersenneTwister]], "tuple[bool, bool, int]"]

#: ``(connection, monster) -> None`` — finalize a replayed kill.
KillFinalizer = Callable[[object, object], None]


def native_weapon_damage_resolver(
    input_builder: Callable[[WeaponCycle], NativeWeaponDamageInput],
) -> DamageResolver:
    """Build a :data:`DamageResolver` over :func:`resolve_native_weapon_damage`.

    ``input_builder(cycle)`` produces the per-swing :class:`NativeWeaponDamageInput`
    (the deferred equipment/stat resolution); the room ``rng`` is attached here so
    the damage rolls advance the shared stream in lockstep with the client.
    """

    def _resolve(cycle: WeaponCycle, rng: MersenneTwister | None):
        inp = input_builder(cycle)
        inp.rng = rng
        result = resolve_native_weapon_damage(inp)
        return (result.is_hit, result.is_blocked, result.damage_wire)

    return _resolve


class NativeMonsterHost(WeaponCycleHost):
    """:class:`WeaponCycleHost` backed by tracked-monster HP + a damage resolver.

    On each landed swing it rolls native damage, subtracts the wire damage from
    the monster's HP and reports a kill when HP reaches zero — the server-side
    equivalent of ``ApplyNativePlayerDamageToMonsterWire`` (cs:1327).
    """

    def __init__(self, resolve_damage: DamageResolver, *, contact: bool = True):
        self._resolve_damage = resolve_damage
        self._contact = contact

    def monster_alive(self, cycle: WeaponCycle) -> bool:
        monster = cycle.monster
        return monster is not None and getattr(monster, "current_hp", 0) > 0

    def has_contact(self, cycle: WeaponCycle) -> bool:
        # The client already decided to swing; positional contact verification is
        # deferred (needs avatar/monster positions). Assume in-range.
        return self._contact

    def resolve_cooldown_ticks(self, cycle: WeaponCycle) -> int:
        """Swing cadence from the player's attack-speed mods (COMBAT_FORMULA §8a).

        The base host always returns the 30-tick (1.0s) default — it never sees the
        equipped weapon or the ``*_ATTACK_SPEED_MOD`` deltas. That made the server
        swing ~1/s while a Fighter (``MELEE_ATTACK_SPEED_MOD +25``) swings faster, so
        mobs the client killed-and-switched were left half-alive (the "killed 2,
        server killed 1" divergence, 2026-06-15 log). Feed the aggregated attack-speed
        percent so the cadence tracks the client. Falls back to the base default on any
        resolution failure. (Weapon-speed field is deferred — direction needs a live
        cadence measurement; attack-speed mods are unambiguous.)
        """
        conn = getattr(cycle, "connection", None) or getattr(cycle, "player_state", None)
        if conn is None:
            return super().resolve_cooldown_ticks(cycle)
        try:
            from . import modifier_aggregator, swing_stats
            profile = swing_stats.resolve_swing_profile(conn)
            mods = modifier_aggregator.aggregate_combat_modifiers(
                swing_stats._saved_character(conn))
            pct = modifier_aggregator.attack_speed_pct(
                mods, is_ranged=profile.is_ranged,
                weapon_class_id=profile.weapon_class_id)
            return resolve_native_basic_attack_cooldown_ticks(None, attack_speed_pct=pct)
        except Exception:  # pragma: no cover — defensive: never break the cadence
            return super().resolve_cooldown_ticks(cycle)

    def resolve_hit(self, cycle: WeaponCycle, rng: MersenneTwister | None):
        monster = cycle.monster
        if monster is None:
            return (False, 0)
        hit, blocked, damage_wire = self._resolve_damage(cycle, rng)
        if not hit or blocked or damage_wire <= 0:
            return (False, 0)
        current = getattr(monster, "current_hp", 0)
        applied = min(current, damage_wire)
        monster.current_hp = max(0, current - damage_wire)
        killed = monster.current_hp <= 0
        # applied damage in HP units (round up, matching C# appliedDamage).
        return (killed, (applied + 255) // 256)


class NativeKillReplay:
    """Replays swings into kills and finalizes them.

    Mirrors ``DrainWeaponCycleKills`` (cs:9970): on each tick, advance the weapon
    cycles, then drain completed kills and finalize each through ``on_kill``.
    """

    def __init__(
        self,
        host: WeaponCycleHost,
        on_kill: KillFinalizer,
        *,
        rng: MersenneTwister | None = None,
    ):
        self._tracker = WeaponCycleTracker(host=host)
        self._on_kill = on_kill
        self._rng = rng or MersenneTwister()

    @property
    def tracker(self) -> WeaponCycleTracker:
        return self._tracker

    @property
    def rng(self) -> MersenneTwister:
        return self._rng

    def register_swing(
        self,
        conn_key: str,
        target_id: int,
        monster: object,
        *,
        now: float,
        conn: object | None = None,
        player_entity_id: int = 0,
        can_start_now: bool = True,
    ) -> None:
        """Client sent a UseTarget swing — register/repeat the weapon cycle.

        Mirrors the ``RegisterAttack`` call site (cs:13668).
        """
        self._tracker.register_attack(
            conn_key, target_id,
            monster=monster, conn=conn, player_entity_id=player_entity_id,
            can_start_now=can_start_now, now=now, rng=self._rng,
        )

    def tick(self, now: float) -> None:
        """Advance every active cycle one native tick, then finalize kills."""
        self._tracker.tick_all(now, self._rng)
        self._drain_kills()

    def tick_player_entity(self, player_entity_id: int, now: float) -> None:
        """Advance one avatar's cycle (cs:10060), then finalize kills."""
        self._tracker.tick_player_entity(player_entity_id, now, self._rng)
        self._drain_kills()

    def _drain_kills(self) -> None:
        while self._tracker.has_pending_kills:
            kill = self._tracker.dequeue_kill()
            if kill is None or not kill.killed:
                continue
            self._on_kill(kill.connection, kill.monster)

    def clear_connection(self, conn_key: str) -> None:
        self._tracker.clear_connection(conn_key)

    def clear(self) -> None:
        self._tracker.clear()
