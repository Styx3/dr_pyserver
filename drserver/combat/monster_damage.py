"""Monster -> player damage computer — ROUTE 2B step 2.

Port of C# Combat/MonsterDamageComputer.cs (server-side mirror of the client's
Weapon::applyDamage @ 0x00597e50). Reproduces the client's hit/block/crit/damage
rolls in the same order from the shared MT19937, so the server can replay a
client swing and arrive at byte-identical damage.

Also ports the pure stat helper MonsterUnitStatsBuilder.ComputeBaseDamageMod
(UnitDesc::getDamageMod @ 0x0050FBF0). The full MonsterUnitStatsBuilder.Build()
needs the .gc profile loader (MonsterAttackData) and is a later sub-step.

All multi-byte arithmetic follows the C# integer/ushort semantics. C#
Mathf.RoundToInt is round-half-to-even; Python round() matches it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .rng import MersenneTwister


@dataclass
class MonsterUnitStats:
    """Cached mob combat stats consumed by the damage formula (+0x100..+0x300)."""

    level: int = 0
    attack_style: int = 0          # weaponDesc[+0xD5]: 1=melee basic, 3=ranged, ...
    weapon_damage_type: int = 0    # 0=SLASHING, 1=PIERCING, ...
    discriminator: int = 0         # unit[+0x314]: per-mob RNG/curve offset

    base_damage_mod: int = 0       # +0x100
    base_critical_chance: int = 0  # +0x104
    crit_multiplier: int = 0       # +0x118 (x100 — 200 = 2.0x)

    base_defense_rating: int = 0   # +0x12C
    base_defense_rating_mod: int = 0  # +0x130
    block_chance: int = 0          # +0x138 (mob = 0)

    base_attack_rating: int = 0    # +0xF0
    base_attack_rating_mod: int = 0  # +0xF4
    base_damage_bonus: int = 0     # +0xFC

    melee_ar: int = 0
    melee_ar_mod: int = 0
    melee_damage_bonus: int = 0
    melee_damage_mod: int = 0
    melee_crit_chance: int = 0
    melee_defense_rating: int = 0
    melee_defense_rating_mod: int = 0

    ranged_ar: int = 0
    ranged_ar_mod: int = 0
    ranged_damage_bonus: int = 0
    ranged_damage_mod: int = 0
    ranged_crit_chance: int = 0
    ranged_defense_rating: int = 0
    ranged_defense_rating_mod: int = 0

    style5_ar: int = 0
    style5_ar_mod: int = 0
    style5_damage_bonus: int = 0
    style5_damage_mod: int = 0
    style5_crit_chance: int = 0

    style6_ar: int = 0
    style6_ar_mod: int = 0
    style6_damage_bonus: int = 0
    style6_damage_mod: int = 0
    style6_crit_chance: int = 0

    damage_type_bonus: list[int] = field(default_factory=lambda: [0] * 8)
    damage_type_mod: list[int] = field(default_factory=lambda: [0] * 8)

    base_reflect_pct: int = 0
    melee_reflect_bonus_pct: int = 0
    ranged_reflect_bonus_pct: int = 0

    damage_mod_scale: int = 256    # +0x300, 256 = 1.0x

    weapon_damage_fixed: int = 256       # weaponDesc[+0xEC] Fixed32 (256 = 1.0)
    weapon_damage_per_level: int = 0     # GlobalKnobs.WeaponDamagePerLevel (=10)
    weapon_volatility_fixed: int = 0     # weaponDesc[+0xF0] Fixed32


@dataclass
class PlayerUnitStats:
    """Player target stats consumed by the damage formula."""

    base_defense_rating: int = 0      # +0x12C
    base_defense_rating_mod: int = 0  # +0x130
    block_chance: int = 0             # +0x138
    discriminator: int = 0            # +0x314

    melee_defense_rating: int = 0
    melee_defense_rating_mod: int = 0
    ranged_defense_rating: int = 0
    ranged_defense_rating_mod: int = 0

    damage_taken_mod: int = 100       # +0x2C4, 100 = full damage
    fire_resist: int = 0              # type 3 -> +0x2DC (percent-resisted)
    ice_resist: int = 0               # type 4 -> +0x2E0
    poison_resist: int = 0            # type 5 -> +0x2E4
    divine_resist: int = 0            # type 6 -> +0x2E8
    shadow_resist: int = 0            # type 7 -> +0x2EC

    base_reflect_pct: int = 0         # +0x134
    melee_reflect_bonus_pct: int = 0  # +0x194
    ranged_reflect_bonus_pct: int = 0  # +0x1B8


@dataclass
class SwingResult:
    """One swing's outcome (and diagnostics for roll-by-roll comparison)."""

    hit: bool = False
    blocked: bool = False
    crit: bool = False
    damage: int = 0           # final damage in wire units (256 = 1 hp)

    r1_hit: int = 0
    r2_block: int = 0
    r3_damage: int = 0
    hit_roll: int = 0
    hit_chance_scaled: int = 0
    block_roll: int = 0
    block_chance: int = 0
    crit_chance: int = 0
    attacker_ar: int = 0
    target_dr: int = 0
    damage_min: int = 0
    damage_max: int = 0
    attack_style: int = 0
    damage_type: int = 0


_U16 = 0xFFFF
_HIT_ROLL_MOD = 0x6464   # 25700
_HIT_FLOOR = 0x0A00      # PvE 10% hit floor
_CRIT_CAP = 0x5A00


def compute_swing(attacker: MonsterUnitStats, target: PlayerUnitStats,
                  rng: MersenneTwister) -> SwingResult:
    """Compute one swing. Consumes 2 RNG (miss/block) or 3 (hit, not blocked)."""
    result = SwingResult()
    attack_style = attacker.attack_style
    damage_type = attacker.weapon_damage_type
    result.attack_style = attack_style
    result.damage_type = damage_type

    # AR / DR / hitChance
    weapon_ar = get_weapon_specific_ar(attack_style, attacker)
    weapon_ar_mod = get_weapon_specific_armod(attack_style, attacker)
    base_ar = attacker.base_attack_rating
    base_ar_mod = attacker.base_attack_rating_mod
    context_field0 = 0  # mob basic attacks
    ar = ((weapon_ar + base_ar) * (weapon_ar_mod + base_ar_mod + context_field0 + 100)) // 100
    if ar < 0:
        ar = 0

    dr = compute_defense_rating(attack_style, target)
    hit_chance = (ar * 100) // (ar + dr) if (ar + dr) != 0 else 0

    # Discriminator adjustment (+0x314): raw delta x5 (Ghidra-confirmed).
    discrim_attacker = attacker.discriminator
    discrim_target = target.discriminator
    hit_chance_scaled = hit_chance * 256 - ((discrim_target - discrim_attacker) * 5)
    if hit_chance_scaled < _HIT_FLOOR:
        hit_chance_scaled = _HIT_FLOOR

    result.attacker_ar = ar
    result.target_dr = dr
    result.hit_chance_scaled = hit_chance_scaled

    # Roll 1: HIT
    result.r1_hit = rng.generate()
    hit_roll = result.r1_hit % _HIT_ROLL_MOD
    result.hit_roll = hit_roll

    # Roll 2: BLOCK (always consumed)
    result.r2_block = rng.generate()
    block_chance = target.block_chance
    block_roll = ((result.r2_block >> 8) & 0xFF) % 100 + 1
    result.block_chance = block_chance
    result.block_roll = block_roll

    hit = hit_roll < hit_chance_scaled
    blocked = hit and block_roll <= block_chance
    result.hit = hit
    result.blocked = blocked

    if not hit or blocked:
        return result

    # Crit (reuses r1)
    crit_chance = compute_critical_chance(attack_style, attacker)
    discrim_delta = discrim_attacker - discrim_target
    crit_chance += (discrim_delta * 0x500) >> 8
    if crit_chance > _CRIT_CAP:
        crit_chance = _CRIT_CAP
    if crit_chance < 0:
        crit_chance = 0
    result.crit_chance = crit_chance
    crit = hit_roll < crit_chance
    result.crit = crit

    # Damage range
    dmg_mod = compute_damage_mod(attack_style, damage_type, attacker)
    dmg_bonus = compute_damage_bonus(attack_style, damage_type, attacker)
    min_dmg, max_dmg = compute_damage_range(attacker, dmg_mod, dmg_bonus)
    result.damage_min = min_dmg
    result.damage_max = max_dmg

    # Roll 3: DAMAGE
    result.r3_damage = rng.generate()
    rng_range = ((max_dmg >> 8) - (min_dmg >> 8)) + 1
    if rng_range <= 0:
        rng_range = 1
    damage = (result.r3_damage % rng_range) * 0x100 + (min_dmg & ~0xFF)

    if crit:
        damage = (attacker.crit_multiplier * damage) // 100
    if damage < 0x100:
        damage = 0x100

    result.damage = damage
    return result


def compute_defense_rating(attack_style: int, target: PlayerUnitStats) -> int:
    dr = target.base_defense_rating
    dr_mod = target.base_defense_rating_mod
    if attack_style in (1, 5, 6, 8):
        dr += target.melee_defense_rating
        dr_mod += target.melee_defense_rating_mod
    elif attack_style in (3, 9, 13):
        dr += target.ranged_defense_rating
        dr_mod += target.ranged_defense_rating_mod
    result = ((dr_mod + 100) * dr) // 100
    return 0 if result < 0 else result


def get_weapon_specific_ar(attack_style: int, attacker: MonsterUnitStats) -> int:
    if attack_style == 1:
        return attacker.melee_ar
    if attack_style in (3, 9, 13):
        return attacker.ranged_ar
    if attack_style == 5:
        return attacker.style5_ar + attacker.melee_ar
    if attack_style in (6, 8):
        return attacker.style6_ar + attacker.melee_ar
    return 0


def get_weapon_specific_armod(attack_style: int, attacker: MonsterUnitStats) -> int:
    if attack_style == 1:
        return attacker.melee_ar_mod
    if attack_style in (3, 9, 13):
        return attacker.ranged_ar_mod
    if attack_style == 5:
        return attacker.style5_ar_mod + attacker.melee_ar_mod
    if attack_style in (6, 8):
        return attacker.style6_ar_mod + attacker.melee_ar_mod
    return 0


def compute_critical_chance(attack_style: int, attacker: MonsterUnitStats) -> int:
    result = attacker.base_critical_chance << 8
    addend = 0
    if attack_style == 1:
        addend = attacker.melee_crit_chance << 8
    elif attack_style in (3, 9, 13):
        addend = attacker.ranged_crit_chance << 8
    elif attack_style == 5:
        addend = (attacker.melee_crit_chance + attacker.style5_crit_chance) << 8
    elif attack_style in (6, 8):
        addend = (attacker.melee_crit_chance + attacker.style6_crit_chance) << 8
    term = (addend << 8) // 0x6400
    result += (result * term) >> 8
    if result < 0:
        result = 0
    if result > 0x6400:
        result = 0x6400
    return result


def compute_damage_bonus(attack_style: int, damage_type: int,
                         attacker: MonsterUnitStats) -> int:
    bonus = attacker.base_damage_bonus & _U16
    if attack_style == 1:
        bonus += attacker.melee_damage_bonus
    elif attack_style in (3, 9, 13):
        bonus += attacker.ranged_damage_bonus
    elif attack_style == 5:
        bonus += attacker.style5_damage_bonus + attacker.melee_damage_bonus
    elif attack_style in (6, 8):
        bonus += attacker.style6_damage_bonus + attacker.melee_damage_bonus
    if damage_type < 8:
        bonus += attacker.damage_type_bonus[damage_type]
    return bonus & _U16


def compute_damage_mod(attack_style: int, damage_type: int,
                       attacker: MonsterUnitStats) -> int:
    result = (attacker.base_damage_mod + 0) * 256  # dmgBonus_param=0 for basic attack
    if attack_style == 1:
        result += attacker.melee_damage_mod * 256
    elif attack_style in (3, 9, 13):
        result += attacker.ranged_damage_mod * 256
    elif attack_style == 5:
        result += (attacker.style5_damage_mod + attacker.melee_damage_mod) * 256
    elif attack_style in (6, 8):
        result += (attacker.style6_damage_mod + attacker.melee_damage_mod) * 256
    if damage_type < 8:
        result += attacker.damage_type_mod[damage_type] * 256
    result += 0x6400  # +100 percent base
    if result < 0:
        result = 0
    return ((result * attacker.damage_mod_scale) >> 16) & _U16


def compute_damage_range(attacker: MonsterUnitStats, dmg_mod: int,
                         dmg_bonus: int) -> tuple[int, int]:
    """Port of computeDamageRange @ 0x00598ED0. Returns (min, max) wire units."""
    level = attacker.level
    wpn_dmg = attacker.weapon_damage_fixed
    wpn_dmg_per_lvl = attacker.weapon_damage_per_level
    volatility = attacker.weapon_volatility_fixed

    prod1 = ((dmg_mod & _U16) * 256 + level * 256) * wpn_dmg
    prod1_shifted = prod1 >> 8

    prod2 = prod1_shifted * (wpn_dmg_per_lvl << 8)
    prod2_shifted = prod2 >> 8

    base_value = prod2_shifted // 100

    spread = (base_value * volatility) >> 8

    min_dmg = base_value - spread
    max_dmg = base_value + spread

    if (min_dmg & 0xFF) > 0x7E:
        min_dmg += 0x100
    min_dmg &= ~0xFF
    if (max_dmg & 0xFF) > 0x7E:
        max_dmg += 0x100
    max_dmg &= ~0xFF

    if min_dmg < 0x100:
        min_dmg = 0x100
    if max_dmg < 0x100:
        max_dmg = 0x100
    return min_dmg, max_dmg


def on_query_apply_damage(damage_wire: int, damage_type: int,
                          target: PlayerUnitStats | None) -> tuple[int, bool]:
    """C6 — Unit::onQueryApplyDamage @ 0x0050b9c0. Returns (adjusted_damage, resisted)."""
    resisted = False
    if damage_wire == 0 or target is None:
        return damage_wire, resisted

    damage = damage_wire

    # Step 1 — DamageTakenMod
    if target.damage_taken_mod != 100:
        damage = (damage * target.damage_taken_mod) // 100

    # Step 2 — elemental resistance (types 3..7 only)
    elemental = {
        3: target.fire_resist,
        4: target.ice_resist,
        5: target.poison_resist,
        6: target.divine_resist,
        7: target.shadow_resist,
    }
    if damage_type not in elemental:
        return (0 if damage < 0 else damage), resisted

    res_mult = 100 - elemental[damage_type]
    if res_mult != 100:
        if res_mult < 1:
            return 0, True
        damage = (damage * res_mult) // 100

    return (0 if damage < 0 else damage), resisted


def compute_reflected_damage(incoming_damage_wire: int, event_kind: int,
                             base_reflect_pct: int, melee_reflect_bonus_pct: int,
                             ranged_reflect_bonus_pct: int) -> int:
    """C7 — reflect/thorns block inside Unit::onApplyDamage @ 0x0050BE50."""
    if event_kind == 4 or incoming_damage_wire == 0:
        return 0
    reflect_pct = base_reflect_pct
    if event_kind == 1:
        reflect_pct += melee_reflect_bonus_pct
    elif event_kind == 2:
        reflect_pct += ranged_reflect_bonus_pct
    if reflect_pct <= 0:
        return 0
    reflected = (incoming_damage_wire * reflect_pct) // 100
    return reflected if reflected > 0 else 0


def event_kind_from_attack_style(attack_style: int) -> int:
    if attack_style in (1, 5, 6, 8):
        return 1  # melee
    if attack_style in (3, 9, 13):
        return 2  # ranged
    return 0


def compute_base_damage_mod(authored_damage_mod: float) -> int:
    """MonsterUnitStatsBuilder.ComputeBaseDamageMod — UnitDesc::getDamageMod @ 0x0050FBF0.

    cached = ((round(authored x 256) - 256) x 25600) >> 16  ~= (authored - 1.0) x 100,
    with the client's auth=2.0 special-case returning 0.
    """
    authored_fixed = round(authored_damage_mod * 256.0)
    if authored_fixed - 256 == 256:  # auth=2.0 client special-case
        return 0
    delta = authored_fixed - 256
    return (delta * 25600) >> 16
