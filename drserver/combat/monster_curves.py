"""Monster stat curves — ROUTE 2B step 2.

Port of C# Combat/MonsterCurves.cs. Retail's CurveTable lookups for cached mob
stats (attack rating, defense rating, max health). Each curve is a sorted array
of (level, value) pairs in Fixed32 (x256); linear interpolation between adjacent
entries uses the client's exact double-truncation formula from
CurveTableEntry::getValue (Ghidra @ 0x005d4050).

C# Mathf.RoundToInt uses round-half-to-even; Python's built-in round() matches it.
"""
from __future__ import annotations

# Each curve: sorted (level_fixed, value_fixed) pairs, both Fixed32 (x256).
# Tables.gc MonsterAttackRating: L1->100, L110->32800.
_MONSTER_ATTACK_RATING_CURVE = (
    (1 * 256, 100 * 256),     # L1 -> 25600
    (110 * 256, 32800 * 256),  # L110 -> 8396800
)

# Tables.gc MonsterDefenseRating: L1->35, L15->287, L110->3087.
_MONSTER_DEFENSE_RATING_CURVE = (
    (1 * 256, 35 * 256),
    (15 * 256, 287 * 256),
    (110 * 256, 3087 * 256),
)

# Tables.gc MonsterDamage: L1->12.7, L25->86.625, L110->372.12 (Fixed32).
_MONSTER_DAMAGE_CURVE = (
    (1 * 256, 3251),
    (25 * 256, 22176),
    (110 * 256, 95263),
)

# Tables.gc MonsterHealth: L1->60.5, L100->5452.
_MONSTER_HEALTH_CURVE = (
    (1 * 256, 60 * 256 + 128),  # 60.5 Fixed32
    (100 * 256, 5452 * 256),
)


def monster_health_base_fixed32(level: int) -> int:
    """Fixed32 base HP from the ``MonsterHealth`` curve — C# ``GCDatabase.
    GetCurveValueFixed32("MonsterHealth", clamp(level, 1, 110))``.

    The level clamp is ``[1, 110]`` (C# ``MonsterHealthTable``), wider than the
    XP/level clamp. Returns the Fixed32 (×256) base HP for the level; callers
    apply difficulty/mod in Fixed32 (see ``monster_health.calculate_hp``).
    """
    lvl = max(1, min(110, level))
    return _interp(lvl << 8, _MONSTER_HEALTH_CURVE)


def monster_defense_rating(authored_dr: float, level: int) -> int:
    """DRS-NET ``DamageResolver.ResolveMonsterDefenseRating``: the per-swing
    defender rating fed into the hit-threshold roll.

    ``ceil(authored x 256)`` (Fixed32FromAuthoredDecimal — CEIL, unlike the
    cached-stat builders which round) x ``MonsterDefenseRating`` curve at
    ``clamp(level, 1, 110)``, ``>> 16``, clamped to ushort.
    """
    import math
    auth_fixed = math.ceil(max(0.0, authored_dr) * 256.0)
    if auth_fixed <= 0:
        return 0
    lvl = max(1, min(110, level))
    curve_val = _interp(lvl << 8, _MONSTER_DEFENSE_RATING_CURVE)
    return ((auth_fixed * curve_val) >> 16) & 0xFFFF


def _interp(key_fixed: int, curve: tuple[tuple[int, int], ...]) -> int:
    """Linear-interpolate a sorted (key, value) curve at key_fixed.

    Client's exact double-truncation formula (CurveTableEntry::getValue):
        frac65536 = ((key - lo.level) * 65536) // (hi.level - lo.level)   # trunc #1
        delta     = (hi.value - lo.value) * frac65536
        result    = lo.value + delta // 65536                             # trunc #2
    Both key and value are Fixed32. Returns Fixed32.
    """
    if not curve:
        return 0
    if key_fixed <= curve[0][0]:
        return curve[0][1]
    if key_fixed >= curve[-1][0]:
        return curve[-1][1]
    for i in range(1, len(curve)):
        if key_fixed <= curve[i][0]:
            k0, v0 = curve[i - 1]
            k1, v1 = curve[i]
            frac65536 = ((key_fixed - k0) * 65536) // (k1 - k0)
            delta = (v1 - v0) * frac65536
            return v0 + delta // 65536
    return curve[-1][1]


class MonsterCurves:
    """Cached mob stat lookups. Static (class-method) mirror of the C# class."""

    @staticmethod
    def compute_base_ar(authored_attack_rating: float, discriminator: int) -> int:
        """Cached baseAR: (auth x MonsterAttackRating(disc<<8)) >> 16."""
        auth_fixed = round(authored_attack_rating * 256.0)
        curve_val = _interp(discriminator << 8, _MONSTER_ATTACK_RATING_CURVE)
        return (auth_fixed * curve_val) >> 16

    @staticmethod
    def compute_base_dr(authored_defense_rating: float, discriminator: int) -> int:
        auth_fixed = round(authored_defense_rating * 256.0)
        curve_val = _interp(discriminator << 8, _MONSTER_DEFENSE_RATING_CURVE)
        return (auth_fixed * curve_val) >> 16

    @staticmethod
    def compute_cached_attack_rating(authored_attack_rating: float, level: int) -> int:
        """Cached mob accuracy ``unit+0xF0`` — ★LIVE-TRACED 2026-06-14 (x64dbg).

        The client fills it on activation (``0x509A19``):
        ``+0xF0 = (desc[+0xD0] × MonsterAttackRating(unit[+0x314] << 8)) >> 16``,
        where ``unit[+0x314]`` is the discriminator == the **mob level** (the curve
        key — NOT a separate stat) and ``desc[+0xD0]`` is the AR base = the authored
        ratio at scale **×64** (distinct from DR/HP which use ×256).

        Live: Whisker grunt (``attack_rating``=1.0) @ L3 → base ``1.0×64=64`` ×
        ``MonsterAttackRating(3)``=179136 ``>> 16`` = **174** (== the byte written).
        ``compute_base_ar`` (×256) overshoots this by exactly 4×.
        """
        base = round(authored_attack_rating * 64.0)
        curve_val = _interp(level << 8, _MONSTER_ATTACK_RATING_CURVE)
        return (base * curve_val) >> 16

    @staticmethod
    def compute_base_max_hp(authored_max_health: float, level: int) -> int:
        """MaxHealth uses level (not disc) as the curve key."""
        auth_fixed = round(authored_max_health * 256.0)
        curve_val = _interp(level << 8, _MONSTER_HEALTH_CURVE)
        return (auth_fixed * curve_val) >> 16

    @staticmethod
    def interp_damage(level: int) -> int:
        """MonsterDamage curve value at the given level (Fixed32)."""
        return _interp(level << 8, _MONSTER_DAMAGE_CURVE)
