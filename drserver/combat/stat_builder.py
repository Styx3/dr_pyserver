"""stat_builder.py — assemble ``client_swing.StatBlock`` from server content data.

Bridges the server's content tables (``creatures`` authored ratios, ``weapons``,
the per-player :class:`~drserver.combat.swing_stats.SwingProfile`) to the
bit-exact client combat formula in :mod:`drserver.combat.client_swing`
(bible §6a/§6c, ``docs/COMBAT_FORMULA.md`` §6). This is the **stat BUILDER** the
docs call out as "a later sub-step" — ``authored_ratio × MonsterCurves(level/disc)``
assembled into a per-offset ``StatBlock`` the resolver reads.

Confidence tiers (anchored to the 2026-06-14 live capture, ``COMBAT_FORMULA.md`` §6b):

  PROVEN bit-exact
    * monster avoidance / defense rating ``+0x12C`` via ``compute_base_dr``
      (live Whisker-Broodling L2: 52 == ``compute_base_dr(1.0, disc=2)``).
    * the constants ``+0x118`` crit-damage % = 200 and ``+0x300`` damage scale = 256
      (both the player and the mob read these live).

  UNVERIFIED (shipped tiered — live diff via [REPLAY-DIAG], not asserted as fact)
    * monster attack rating ``+0xF0`` via ``compute_base_ar`` — live 60 ≠ formula 399;
      §6d concludes the mob's combat stats are *server-provided on activation*, so the
      exact send-value mapping is still open.
    * monster damage-mod ``+0x100`` via ``compute_base_damage_mod`` — live −50 needs
      authored 0.5; the extra level/difficulty scaling is not yet reconciled.
    * player attack rating ``+0xF0`` from the :class:`SwingProfile` — live 70 ≠ AGI×14.

The DEFENDER side (what the player→mob swing the combat manager replays actually
reads) is fully in the PROVEN tier; only the ATTACKER magnitude inputs are tiered.
Per bible §7 step 1, wiring this advances the MT in the exact client draw order even
before the attacker magnitude is bit-exact.
"""
from __future__ import annotations

from dataclasses import dataclass

from .client_swing import StatBlock
from .monster_curves import MonsterCurves
from .monster_damage import compute_base_damage_mod

# ── Combat-stats byte offsets (client CombatStats struct; COMBAT_FORMULA §6) ──
OFF_ACCURACY = 0x0F0        # +0xF0  attack rating / accuracy base
OFF_ACCURACY_MOD = 0x0F4    # +0xF4  accuracy mod
OFF_B30_BASE = 0x0FC        # +0xFC  b30 base (penetration / variance power) — live player = 0
OFF_B30_MELEE = 0x180       # +0x180 b30 melee term (elements 1/5/6/8) — live player = 11
OFF_B30_RANGED = 0x1A4      # +0x1A4 b30 ranged term (elements 3/9/0xD)
OFF_DAMAGE_MOD = 0x100      # +0x100 c10 base (damage mod)
OFF_WEAPON_BASE = 0x104     # +0x104 a30 base weapon damage (crit gate only)
OFF_CRIT_REDUCTION = 0x108  # +0x108 crit-reduction factor (live player 100)
OFF_CRIT_DAMAGE = 0x118     # +0x118 crit-damage % (200 = 2.0×)
OFF_AVOIDANCE = 0x12C       # +0x12C avoidance / defense rating base
OFF_AVOIDANCE_MOD = 0x130   # +0x130 avoidance mod
OFF_BLOCK_CHANCE = 0x138    # +0x138 block chance
OFF_DAMAGE_SCALE = 0x300    # +0x300 global damage scalar (256 = 1.0)
OFF_DISCRIMINATOR = 0x314   # +0x314 discriminator / weapon-range / block input

# Weapon-object offsets (event[1]; COMBAT_FORMULA §3).
OFF_WPN_VARIANCE = 0x0EC    # +0xEC  variance scalar (Fixed32)
OFF_WPN_SPREAD = 0x0F0      # +0xF0  spread factor (Fixed32)
OFF_WPN_ARMOR_CLASS = 0x0E8  # +0xE8 armor class (0..7) — drives mitigation offsets

# Proven constants (player + mob both carry these live, COMBAT_FORMULA §6).
CRIT_DAMAGE_PERCENT = 200
DAMAGE_SCALE = 256


@dataclass(frozen=True)
class MonsterStatInput:
    """Authored ratios for one creature (a ``creatures`` DB row), plus the
    level/discriminator the curves key on.

    ``discriminator`` is the client's ``+0x314`` curve key; for the live-captured
    L2 Whisker Broodling it equalled the level (2), which is the default the
    callers use. ``authored_*`` are the raw multiplier ratios from the row
    (e.g. ``defense_rating = 1.0``), NOT the cached combat values.
    """

    level: int
    discriminator: int
    authored_attack_rating: float = 1.0
    authored_defense_rating: float = 1.0
    authored_damage_mod: float = 1.0
    authored_critical_chance: float = 1.0
    block_chance: int = 0           # mobs = 0 (live-confirmed +0x138)
    attack_style: int = 1           # weapon class id (1=HTH/basic melee) — see note


def monster_defender_statblock(m: MonsterStatInput) -> StatBlock:
    """The DEFENDER ``StatBlock`` for a monster (player→mob swing).

    PROVEN tier: the only offsets the defender role reads are avoidance
    (``+0x12C``/``+0x130``), block (``+0x138``) and the discriminator (``+0x314``).
    ``compute_base_dr`` is bit-exact vs the live ``+0x12C`` (52 for L2 auth-1.0).
    The unresolved attacker AR/damage-mod are deliberately NOT set here — they are
    never read on the defender path, so omitting them keeps this block fully PROVEN.
    """
    sb = StatBlock()
    sb.set(OFF_AVOIDANCE, MonsterCurves.compute_base_dr(m.authored_defense_rating, m.discriminator))
    sb.set(OFF_AVOIDANCE_MOD, 0)
    sb.set(OFF_BLOCK_CHANCE, m.block_chance & 0xFFFF)
    sb.set(OFF_DISCRIMINATOR, m.discriminator)
    return sb


def monster_attacker_statblock(m: MonsterStatInput) -> StatBlock:
    """The ATTACKER ``StatBlock`` for a monster (mob→player swing).

    Extends the proven defender block with the attacker magnitude inputs. The
    AR (``+0xF0``) and damage-mod (``+0x100``) mappings are **UNVERIFIED** (live
    60/−50 vs formula 399/0); they are wired via the existing converters so the
    builder is complete and the MT draw order is correct, but the magnitude must
    be diffed against a live mob→player swing before it is trusted (bible §6d).
    """
    sb = monster_defender_statblock(m)
    # ★LIVE-TRACED 2026-06-14: +0xF0 = (AR_ratio×64 × MonsterAttackRating(level<<8))>>16.
    # Scale is ×64 (NOT ×256 like DR), curve key is the level/discriminator. Whisker
    # grunt L3 → 174, reproduced exactly. (compute_base_ar's ×256 was 4× too high.)
    sb.set(OFF_ACCURACY, MonsterCurves.compute_cached_attack_rating(m.authored_attack_rating, m.level))
    # UNVERIFIED — compute_base_damage_mod(1.0) = 0, live +0x100 = −50. See COMBAT_FORMULA §6b.
    sb.set(OFF_DAMAGE_MOD, compute_base_damage_mod(m.authored_damage_mod) & 0xFFFF)
    sb.set(OFF_CRIT_DAMAGE, CRIT_DAMAGE_PERCENT)   # proven constant
    sb.set(OFF_DAMAGE_SCALE, DAMAGE_SCALE)         # proven constant
    return sb


_RANGED_ELEMENTS = frozenset({3, 9, 0xD})


def player_attacker_statblock(profile, *, element: int = 5, discriminator: int = 0,
                              modifiers=None) -> StatBlock:
    """The ATTACKER ``StatBlock`` for a player (player→mob swing), mapped from a
    resolved :class:`~drserver.combat.swing_stats.SwingProfile`.

    Live-anchored to the Styx3 L2 capture (``COMBAT_FORMULA.md`` §6b): accuracy
    ``+0xF0`` = 70 and the melee b30 term ``+0x180`` = 11 — both reproduced from the
    *allocated+passive* stats (``combat_attack_rating`` / ``combat_damage_bonus``),
    NOT the base-inclusive display stats (which gave 210/35). # UNVERIFIED (one capture).

    Offsets:
      * ``+0xF0`` = ``profile.combat_attack_rating`` (live 70).
      * b30 term: ``+0x180`` (melee elements 1/5/6/8) or ``+0x1A4`` (ranged 3/9/0xD)
        = ``profile.combat_damage_bonus`` (live 11). ``+0xFC`` stays 0 (live player = 0).
      * ``+0x118`` = 200, ``+0x300`` = 256, ``+0x108`` = 100 — PROVEN constants.
      * ``+0x314`` = ``discriminator`` (resolver passes player level; live = 2).

    ``modifiers`` (optional ``{Attribute_enum: value}`` from
    :func:`drserver.combat.modifier_aggregator.aggregate_combat_modifiers`) folds the
    direct AttributeModifier deltas from passives / equipment / buffs into their
    CombatStats slots (``0xC8 + enum×4``; ``COMBAT_FORMULA.md`` §8). Each is **added**
    on top of the base derivations (so e.g. a Fighter's ``MELEE_ATTACK_RATING_MOD +100``
    lands in ``+0x178`` and the accBonus term doubles accuracy). Primary-attribute enums
    are skipped — they reach combat through the ``swing_stats`` derivation, not a raw slot.
    """
    sb = StatBlock()
    sb.set(OFF_ACCURACY, max(0, int(getattr(profile, "combat_attack_rating", 0))))
    b30_slot = OFF_B30_RANGED if element in _RANGED_ELEMENTS else OFF_B30_MELEE
    sb.set(b30_slot, max(0, int(getattr(profile, "combat_damage_bonus", 0))))
    sb.set(OFF_CRIT_REDUCTION, 100)                                                 # live player = 100
    sb.set(OFF_CRIT_DAMAGE, CRIT_DAMAGE_PERCENT)                                    # proven
    sb.set(OFF_DAMAGE_SCALE, DAMAGE_SCALE)                                          # proven
    sb.set(OFF_DISCRIMINATOR, discriminator & 0xFFFF)
    _apply_modifiers(sb, modifiers)
    return sb


def _apply_modifiers(sb: StatBlock, modifiers) -> None:
    """Flat-add direct AttributeModifier deltas into their ``0xC8+enum×4`` slots.

    Skips primary attributes (folded by ``swing_stats``). Rounds toward nearest for the
    rare fractional authored values (attack-speed mods, not read by the damage formula).
    """
    if not modifiers:
        return
    from .modifier_aggregator import PRIMARY_ATTRIBUTES, offset_for
    for enum, value in modifiers.items():
        if enum in PRIMARY_ATTRIBUTES:
            continue
        off = offset_for(enum)
        sb.set(off, sb.i32(off) + int(round(value)))


def player_weapon_statblock(profile, *, armor_class: int = 0) -> StatBlock:
    """The weapon ``StatBlock`` (``event[1]``) for a player swing.

    ``+0xEC`` (variance scalar) and ``+0xF0`` (spread factor) come from the equipped
    weapon's Fixed32 Damage / DamageVolatility (``profile.weapon_damage_f32`` /
    ``profile.weapon_volatility_f32``). ``+0xE8`` armor class drives the per-armor-class
    mitigation offsets (live 0). The live capture read ``+0xEC``=154 / ``+0xF0``=64.
    """
    sb = StatBlock()
    sb.set(OFF_WPN_VARIANCE, max(0, int(getattr(profile, "weapon_damage_f32", 0))))
    sb.set(OFF_WPN_SPREAD, max(0, int(getattr(profile, "weapon_volatility_f32", 0))))
    sb.set(OFF_WPN_ARMOR_CLASS, armor_class & 0xFF)
    return sb
