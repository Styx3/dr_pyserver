"""client_swing_resolver.py — wire :func:`client_swing.compute_swing` into the
combat-manager swing replay.

This is the "reproduce, don't relay" path (bible §6/§7): instead of the C#-ported
``monster_damage`` / ``damage_computer`` formulas, the server replays each client
swing through the **live-validated** :mod:`drserver.combat.client_swing` resolver,
advancing the shared room MT in the exact client draw order (2 draws on a
miss/block, 3 on a hit — ``COMBAT_FORMULA.md`` §1).

Per bible §7 step 1 the *primary* value of this wiring is **MT stream alignment**:
the draw order/count matches the client even before the attacker magnitude is
bit-exact. The DEFENDER (mob) avoidance is PROVEN (``compute_base_dr``); the
player ATTACKER magnitude inputs (``+0xF0`` AR, the ``hi_flag``) are still
UNVERIFIED (``stat_builder`` tiering) — diff them against a live swing via the
[REPLAY-DIAG] / [CLIENT-SWING] logs before trusting damage numbers.

Drop-in for the :data:`drserver.combat.native_kill_replay.DamageResolver` seam:
``resolve(cycle, rng) -> (hit, blocked, damage_wire)``.
"""
from __future__ import annotations

from typing import Optional

from drserver.core import log

from . import modifier_aggregator, stat_builder, swing_stats
from .client_swing import compute_swing
from .rng import MersenneTwister

# The captured player swings (both HIT and MISS) resolved on the NOT-melee-in-range
# path (COMBAT_FORMULA §2/§3); the melee-in-range path (acc−140 + block CurveTable)
# is still unvalidated, so we stay on the validated path until it is captured.
_MELEE_IN_RANGE = False

# event[6] "hi_flag" variance power input. LEAD (2026-06-14): for the validated
# Styx3 capture hi_flag = 10 == that player's weapon damage level (dmgLvl, an L1
# staff → 1×WeaponDamagePerLevel(10)). So hi_flag = profile.damage_level — grounded
# on one data point; confirm with a capture at a different weapon level. # UNVERIFIED


def _monster_stat_input(monster) -> stat_builder.MonsterStatInput:
    """Build the authored-ratio input for a tracked monster (defender role)."""
    level = max(1, int(getattr(monster, "level", 1) or 1))
    authored_dr = float(getattr(monster, "defense_rating", 0.0) or 0.0)
    if authored_dr <= 0.0:
        authored_dr = 1.0  # pre-refactor registrations carry no authored DR
    return stat_builder.MonsterStatInput(
        level=level,
        discriminator=level,            # disc == level for the live-proven L2 case
        authored_defense_rating=authored_dr,
        block_chance=0,                 # mobs = 0 (+0x138, live-confirmed)
    )


def resolve_client_swing(cycle, rng: Optional[MersenneTwister]) -> "tuple[bool, bool, int]":
    """Resolve one player→mob swing through ``client_swing.compute_swing``.

    Returns ``(hit, blocked, damage_wire)``. Consumes ``rng`` in the client draw
    order. Falls back to ``(False, False, 0)`` (no kill credit, MT untouched) if
    the player profile cannot be resolved, so a resolver failure is inert rather
    than awarding phantom damage.
    """
    # register_swing stores the connection on cycle.connection (cycle.player_state
    # is left None by the NativeKillReplay register path) — read connection first.
    conn = getattr(cycle, "connection", None) or getattr(cycle, "player_state", None)
    monster = getattr(cycle, "monster", None)
    if conn is None or monster is None or rng is None:
        # Log the inert case so a missing input is visible (distinguishes "resolver
        # ran but bailed" from "resolve_hit never fired").
        log.info(
            f"[CLIENT-SWING] inert: conn={conn is not None} "
            f"monster={monster is not None} rng={rng is not None}"
        )
        return (False, False, 0)

    try:
        profile = swing_stats.resolve_swing_profile(conn)
    except Exception as exc:  # pragma: no cover — DB/profile guard
        log.debug(f"[CLIENT-SWING] profile resolve failed: {exc}")
        return (False, False, 0)

    element = profile.weapon_class_id or 5      # 5 = 1H-melee (bare hands ride it)
    # Direct AttributeModifier deltas (passives now; equipment/buffs next slices) folded
    # into the attacker CombatStats slots — COMBAT_FORMULA §8. e.g. a Fighter's #109
    # MELEE_ATTACK_RATING_MOD +100 doubles accuracy via the +0x178 accBonus term.
    saved = swing_stats._saved_character(conn)
    modifiers = modifier_aggregator.aggregate_combat_modifiers(saved)
    # discriminator (+0x314) = player level for both sides → the range-adjust term
    # cancels against the same-level mob, matching the live-captured threshold (§6b).
    attacker = stat_builder.player_attacker_statblock(
        profile, element=element, discriminator=max(0, int(getattr(profile, "player_level", 0))),
        modifiers=modifiers)
    weapon = stat_builder.player_weapon_statblock(profile, armor_class=0)
    defender = stat_builder.monster_defender_statblock(_monster_stat_input(monster))

    result = compute_swing(
        attacker, defender, weapon, rng,
        element=element,
        armor_class=0,
        melee_in_range=_MELEE_IN_RANGE,
        crit_extra=max(0, int(getattr(profile, "damage_level", 0))),  # hi_flag lead
    )

    log.info(
        f"[CLIENT-SWING] '{getattr(conn, 'login_name', '?')}' el={element} "
        f"hit={result.hit} blocked={result.blocked} crit={result.crit} "
        f"dmg_wire={result.damage_wire} draws={result.draws} "
        f"r1={result.roll_hit & 0xFFFFFFFF:#010x} "
        f"mods={ {k: round(v, 2) for k, v in modifiers.items()} if modifiers else '{}'}"
    )
    return (result.hit, result.blocked, result.damage_wire)


def client_swing_damage_resolver():
    """Return a :data:`DamageResolver` over :func:`resolve_client_swing`.

    Same shape as ``native_kill_replay.native_weapon_damage_resolver`` so it drops
    into ``NativeMonsterHost`` unchanged.
    """
    def _resolve(cycle, rng: MersenneTwister | None):
        return resolve_client_swing(cycle, rng)

    return _resolve
