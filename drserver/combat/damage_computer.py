"""Native weapon damage computer — ROUTE 2B step 3.

Port of the pure (no-GCDatabase) subset of C# Combat/DamageComputer.cs — the
binary-verified Weapon::applyDamage path used by NativeDamageReplaySelfTest to
replay a client swing from the shared MT19937 and arrive at byte-identical
damage. Ghidra anchors: applyDamage 0x00597E50, computeDamageRange 0x00598ED0,
RNG #1 hit 0x59804B, #2 block 0x598133, #3 damage 0x599011.

Deferred (need GCDatabase/PlayerState/Monster): ProcessAttack, the spell/ranged
damage paths, and the equipment/knob stat resolvers.

Fixed32 = 8.8 fixed point (256 = 1.0). C# Mathf.CeilToInt -> math.ceil;
C# int cast truncates toward zero; arithmetic >> matches Python >> on negatives.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

from .rng import MersenneTwister

_HIT_ROLL_MOD = 25700
_HIT_FLOOR = 0x0A00


class AttackResultType(IntEnum):
    MISS = 0
    BLOCK = 1
    HIT = 2
    CRITICAL = 3


# ── Fixed32 math ────────────────────────────────────────────────────────────

def from_int(n: int) -> int:
    return n << 8


def from_float(f: float) -> int:
    return int(f * 256.0)  # C# (int) cast truncates toward zero


def to_int(f: int) -> int:
    return f >> 8


def to_float(f: int) -> float:
    return f / 256.0


def fixed_mul(a: int, b: int) -> int:
    return (a * b) >> 8


def round_fixed32(f: int) -> int:
    """Rounds 0.5+ up, then clears the fractional byte (binary-verified)."""
    if (f & 0xFF) > 0x7E:
        f += 0x100
    return f & ~0xFF


def native_fixed32_from_authored_decimal(value: float) -> int:
    return math.ceil(value * 256.0)


def roll_damage_range(min_dmg: int, max_dmg: int, raw: int) -> int:
    rng_range = max(0, max_dmg - min_dmg)
    if rng_range > 0:
        return (raw % (rng_range + 1)) + min_dmg
    return min_dmg


def roll_spell_damage_range(min_dmg: int, max_dmg: int, raw: int) -> int:
    min_hp = min_dmg >> 8
    max_hp = max_dmg >> 8
    if min_hp < 1:
        min_hp = 1
    if max_hp < min_hp:
        max_hp = min_hp
    damage_hp = min_hp
    range_hp = max_hp - min_hp
    if range_hp > 0:
        damage_hp = (raw % range_hp) + min_hp
    return max(0x100, damage_hp << 8)


def resolve_native_hit_threshold(attack_rating: int, defense_rating: int,
                                 attacker_level: int, defender_level: int) -> int:
    attack = max(0, attack_rating)
    defense = max(0, defense_rating)
    chance_percent = 0 if attack + defense == 0 else (attack * 100) // (attack + defense)
    threshold = chance_percent << 8
    level_delta = max(0, min(110, defender_level)) - max(0, min(110, attacker_level))
    threshold -= level_delta * 0x500
    if threshold < _HIT_FLOOR:
        threshold = _HIT_FLOOR
    return threshold


def compute_native_weapon_damage_range(damage_level: int, damage_bonus: int,
                                       damage_mod: int, weapon_damage_f32: int,
                                       volatility_f32: int) -> tuple[int, int]:
    """computeDamageRange @ 0x00598ED0. Returns (min, max) wire units."""
    if damage_level < 0:
        damage_level = 0
    if damage_bonus < 0:
        damage_bonus = 0
    if damage_mod < 0:
        damage_mod = 0
    if weapon_damage_f32 <= 0:
        weapon_damage_f32 = 0x100
    if volatility_f32 < 0:
        volatility_f32 = 0

    normalized = fixed_mul((damage_level + damage_bonus) << 8, weapon_damage_f32)
    normalized = (normalized * (damage_mod << 8)) // 0x6400
    if normalized < 0x100:
        normalized = 0x100

    spread = fixed_mul(normalized, volatility_f32)
    min_damage = round_fixed32(normalized - spread)
    max_damage = round_fixed32(normalized + spread)
    if min_damage < 0x100:
        min_damage = 0x100
    if max_damage < 0x100:
        max_damage = 0x100
    min_damage = (min_damage >> 8) << 8
    max_damage = (max_damage >> 8) << 8
    if max_damage < min_damage:
        max_damage = min_damage
    return min_damage, max_damage


@dataclass
class NativeWeaponDamageInput:
    rng: MersenneTwister | None = None
    attacker_level: int = 0
    defender_level: int = 0
    attack_rating: int = 0
    defense_rating: int = 0
    block_chance: int = 0
    damage_level: int = 0
    damage_bonus: int = 0
    damage_mod: int = 0
    weapon_damage_f32: int = 0
    weapon_volatility_f32: int = 0
    crit_threshold: int = 0
    crit_damage_percent: int = 0
    source: str = ""


@dataclass
class NativeWeaponDamageResult:
    type: AttackResultType = AttackResultType.MISS
    result_name: str = "MISS"
    hit_raw: int = 0
    block_raw: int = 0
    damage_raw: int = 0
    hit_roll: int = 0
    block_roll: int = 0
    hit_threshold: int = 0
    min_damage_f32: int = 0
    max_damage_f32: int = 0
    damage_f32: int = 0
    damage_wire: int = 0
    is_hit: bool = False
    is_blocked: bool = False
    is_critical: bool = False
    room_rng_after: int = 0


def resolve_native_weapon_damage(inp: NativeWeaponDamageInput) -> NativeWeaponDamageResult:
    """Shared native Weapon::applyDamage path. Room RNG order: hit, block, then
    damage only for a landed non-blocked hit. 2 RNG on miss/block, 3 on hit."""
    result = NativeWeaponDamageResult()
    if inp is None:
        return result

    result.hit_threshold = resolve_native_hit_threshold(
        inp.attack_rating, inp.defense_rating, inp.attacker_level, inp.defender_level)

    if inp.rng is None:
        result.type = AttackResultType.MISS
        result.result_name = "NO-RNG"
        return result

    result.hit_raw = inp.rng.generate()
    result.hit_roll = result.hit_raw % _HIT_ROLL_MOD

    result.block_raw = inp.rng.generate()
    result.block_roll = ((result.block_raw >> 8) & 0xFF) % 100 + 1

    result.is_hit = result.hit_roll < result.hit_threshold
    # Client blocks strictly when blockChance > blockRoll (equality is NOT a block).
    result.is_blocked = result.is_hit and result.block_roll < inp.block_chance

    if not result.is_hit:
        result.type = AttackResultType.MISS
        result.result_name = "MISS"
        result.room_rng_after = inp.rng.calls_since_reseed
        return result

    if result.is_blocked:
        result.type = AttackResultType.BLOCK
        result.result_name = "BLOCK"
        result.room_rng_after = inp.rng.calls_since_reseed
        return result

    min_damage, max_damage = compute_native_weapon_damage_range(
        inp.damage_level, inp.damage_bonus, inp.damage_mod,
        inp.weapon_damage_f32, inp.weapon_volatility_f32)
    result.min_damage_f32 = min_damage
    result.max_damage_f32 = max_damage

    result.damage_raw = inp.rng.generate()
    damage = roll_damage_range(min_damage, max_damage, result.damage_raw)

    if inp.crit_threshold > 0 and result.hit_roll < inp.crit_threshold:
        result.is_critical = True
        crit_percent = inp.crit_damage_percent if inp.crit_damage_percent > 0 else 200
        damage = max(0x100, (damage * crit_percent) // 100)

    result.damage_f32 = damage
    result.damage_wire = max(1, damage)
    result.type = AttackResultType.CRITICAL if result.is_critical else AttackResultType.HIT
    result.result_name = "CRIT" if result.is_critical else "HIT"
    result.room_rng_after = inp.rng.calls_since_reseed
    return result
