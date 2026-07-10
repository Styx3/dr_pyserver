"""Per-swing native weapon-damage input (ROUTE 2B — the Step-3 resolver).

:func:`resolve_native_weapon_damage` needs a :class:`NativeWeaponDamageInput`
per swing: the attacker (player) attack-rating / weapon-damage knobs and the
defender (monster) level / defense-rating.

The values are resolved per swing via :mod:`drserver.combat.swing_stats` — the
DRS-NET ``DamageResolver`` port (real agility/strength from the saved
character + class passives, equipped-weapon Damage/DamageVolatility from the
``weapons`` content table, monster authored DefenseRating x the
``MonsterDefenseRating`` curve, the out-level crit threshold). When no ``conn``
is supplied (legacy callers/tests) or the profile cannot be resolved, the
old known-good starter anchor constants are used instead
(``NativeDamageReplaySelfTest`` "LatestPup50024": seed ``0x8D801C2B`` advanced
156 -> ``damage_wire`` 3180 — kept bit-exact by ``swing_stats._FALLBACK``).

# UNVERIFIED vs live: equipment +damage/+stat mod aggregation is not resolved
# yet (damage_mod stays 100, damage_bonus is stat-derived only). Compare the
# [SWING-STATS]/[REPLAY-DIAG] log lines against live client damage and refine
# in swing_stats, not here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from drserver.combat import swing_stats
from drserver.combat.damage_computer import NativeWeaponDamageInput
from drserver.combat.monster_curves import monster_defense_rating

if TYPE_CHECKING:
    from drserver.net.connection import RRConnection


def build_swing_input(player_level: int, monster, *, rng=None,
                      conn: Optional["RRConnection"] = None) -> NativeWeaponDamageInput:
    """Build the per-swing damage input for ``conn`` (or a starter fallback)
    vs ``monster``.

    ``rng`` is normally left ``None`` here and attached by
    :func:`native_weapon_damage_resolver`, which owns the shared room stream.
    """
    p_level = max(1, int(player_level or 1))
    m_level = max(1, int(getattr(monster, "level", 1) or 1))

    profile = (swing_stats.resolve_swing_profile(conn) if conn is not None
               else swing_stats._FALLBACK)

    authored_dr = float(getattr(monster, "defense_rating", 0.0) or 0.0)
    defense = (monster_defense_rating(authored_dr, m_level) if authored_dr > 0.0
               else swing_stats._FALLBACK_MONSTER_DEFENSE_RATING)

    return NativeWeaponDamageInput(
        rng=rng,
        attacker_level=p_level,
        defender_level=m_level,
        attack_rating=profile.attack_rating,
        defense_rating=defense,
        block_chance=0,
        damage_level=profile.damage_level,
        damage_bonus=profile.damage_bonus,
        damage_mod=profile.damage_mod,
        weapon_damage_f32=profile.weapon_damage_f32,
        weapon_volatility_f32=profile.weapon_volatility_f32,
        crit_threshold=swing_stats.critical_threshold(p_level, m_level),
        crit_damage_percent=swing_stats.CRIT_DAMAGE_PERCENT,
        source="native-swing-replay",
    )
