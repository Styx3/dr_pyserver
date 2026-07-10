"""client_swing.py — bit-exact port of the client's combat damage pipeline.

Direct transcription of ``DungeonRunners.exe`` ``FUN_00597e50`` (combat resolver)
and its sub-functions, reverse-engineered 2026-06-13 (see ``docs/COMBAT_FORMULA.md``).
This is **Path A** (bible §6a): the server must compute combat damage bit-identically
to the client so the zero-tolerance HP-synch compare (bible §4) passes.

STATUS: the **damage-magnitude chain is LIVE-VALIDATED bit-exact** (2026-06-14, element-5
player swing, x64dbg PID 3788): mitigation (``c10``) → ``b30`` → variance range (``ed0``) →
variance draw reproduced the client's applied damage of ``0x0E76`` (14.46) exactly. Three
bugs were fixed in the process (``c10`` double-shift; ``ed0`` first term = ``b30`` not ``mit``;
``ed0`` middle step ``*mit/100``). Still NOT validated: hit/miss threshold (draw#1 vs acc/def),
block (CurveTable ``FUN_00598810`` — no-curve fallback), crit, and element-1; plus mapping the
client ``CombatStats`` offsets to the server's stats (``docs/COMBAT_FORMULA.md`` §6).

Fixed-point: all values are ×256 ("drfloat") signed int32 unless noted. Never use
floats here (bible §6b — a 1-ULP float difference fails the compare).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .rng import MersenneTwister

_U32 = 0xFFFFFFFF


def to_int32(x: int) -> int:
    """Truncate to signed 32-bit (two's complement), matching x86 register width."""
    x &= _U32
    return x - 0x100000000 if x & 0x80000000 else x


def fx_shr8_i32(v: int) -> int:
    """The client's ``(uint)v>>8 | (int)(v>>32)<<24`` idiom = a 64-bit value arithmetic-
    shifted right 8 and truncated to int32. (Python ``>>`` is the arithmetic floor shift,
    matching x86 ``SAR`` on two's-complement values.)"""
    return to_int32((v >> 8) & _U32)


def fx_mul_shr8(a: int, b: int) -> int:
    """``(a * b) >> 8`` as the client does it: 64-bit product, then SHR-8 to int32."""
    return fx_shr8_i32(to_int32(a) * to_int32(b))


def _idiv(num: int, den: int) -> int:
    """x86 ``IDIV``: signed integer division truncating toward zero (Python ``//`` floors)."""
    if den == 0:
        return 0
    q = abs(num) // abs(den)
    if (num < 0) != (den < 0):
        q = -q
    return to_int32(q)


class StatBlock:
    """A resolved combat-stats struct, addressed by client byte offset.

    The client's combat math reads the attacker/defender ``CombatStats`` struct by
    offset (``+0x100``, ``+0x174``, …). We keep the same offset addressing so the port
    is a 1:1 transcription; mapping these offsets to the server's character/creature
    stats is a separate step (``docs/COMBAT_FORMULA.md`` §6). Missing offsets read 0.
    """

    __slots__ = ("_f",)

    def __init__(self, fields: Mapping[int, int] | None = None):
        self._f: dict[int, int] = dict(fields or {})

    def i32(self, offset: int) -> int:
        return to_int32(self._f.get(offset, 0))

    def u16(self, offset: int) -> int:
        return self._f.get(offset, 0) & 0xFFFF

    def set(self, offset: int, value: int) -> "StatBlock":
        self._f[offset] = value & _U32
        return self


@dataclass(frozen=True)
class SwingResult:
    hit: bool
    blocked: bool
    damage_wire: int          # ×256 fixed-point damage actually applied (0 if miss/blocked)
    crit: bool
    draws: int                # MT draws consumed this swing (2 = miss/block, 3 = hit)
    roll_hit: int             # draw #1 raw genrand
    roll_block: int           # draw #2 raw genrand
    roll_variance: int        # draw #3 raw genrand (0 if not reached)


# --- element-switched stat selectors (FUN_005988d0 / 5989c0 / 98950 / 98a30 / 98c10 / 98b30) ---
# Element enum values seen in the decompile: 1, 3, 5, 6, 8, 9, 0xD.

def _avoid_005988d0(defender: StatBlock, element: int) -> int:
    """Defender avoidance/defence for the element (FUN_005988d0)."""
    a = defender.i32(0x12C)
    b = defender.i32(0x130)
    if element in (1, 5, 6, 8):
        a += defender.i32(0x18C)
        b += defender.i32(0x190)
    elif element in (3, 9, 0xD):
        a += defender.i32(0x1B0)
        b += defender.i32(0x1B4)
    v = to_int32((b + 100) * a // 100)
    return max(0, v)


def _acc_bonus_005989c0(attacker: StatBlock, element: int) -> int:
    """FUN_005989c0."""
    if element == 1:
        return attacker.i32(0x178)
    if element == 5:
        return attacker.i32(0x1C0) + attacker.i32(0x178)
    if element in (6, 8):
        return attacker.i32(0x1D8) + attacker.i32(0x178)
    if element in (3, 9, 0xD):
        return attacker.i32(0x19C)
    return 0


def _acc_skill_00598950(attacker: StatBlock, element: int) -> int:
    """FUN_00598950."""
    if element == 1:
        return attacker.i32(0x174)
    if element == 5:
        return attacker.i32(0x1BC) + attacker.i32(0x174)
    if element in (6, 8):
        return attacker.i32(0x1D4) + attacker.i32(0x174)
    if element in (3, 9, 0xD):
        return attacker.i32(0x198)
    return 0


def _base_dmg_00598a30(attacker: StatBlock, element: int) -> int:
    """Base weapon damage, ×256, clamped [0, 0x6400] (FUN_00598a30)."""
    out = to_int32(attacker.i32(0x104) << 8)
    e = 0
    if element == 1:
        e = attacker.i32(0x188) << 8
    elif element in (3, 9, 0xD):
        e = attacker.i32(0x1AC) << 8
    elif element == 5:
        e = attacker.i32(0x188) * 0x100 + attacker.i32(0x1D0) * 0x100
    elif element in (6, 8):
        e = attacker.i32(0x188) * 0x100 + attacker.i32(0x1E8) * 0x100
    # out += out * (e/0x6400) ; the e term is (e<<8)/0x6400 then >>8
    term = to_int32((to_int32(e) << 8) // 0x6400) if e >= 0 else to_int32(-((-(to_int32(e) << 8)) // 0x6400))
    out = to_int32(out + fx_shr8_i32(to_int32(out) * term))
    if out < 0:
        return 0
    if out > 0x6400:
        out = 0x6400
    return out


_ARMOR_C10 = {0: 0x230, 1: 0x23C, 2: 0x248, 3: 0x254, 4: 0x264, 5: 0x274, 6: 0x284, 7: 0x294}
_ARMOR_B30 = {0: 0x234, 1: 0x240, 2: 0x24C, 3: 0x258, 4: 0x268, 5: 0x278, 6: 0x288, 7: 0x298}


def _mitigation_00598c10(attacker: StatBlock, element: int, armor_class: int, extra: int) -> int:
    """FUN_00598c10 — scaled damage term (&0xffff by caller)."""
    v = to_int32((attacker.i32(0x100) + extra) * 0x100)
    if element == 1:
        v += attacker.i32(0x184) * 0x100
    elif element in (3, 9, 0xD):
        v += attacker.i32(0x1A8) * 0x100
    elif element == 5:
        v += attacker.i32(0x1CC) * 0x100 + attacker.i32(0x184) * 0x100
    elif element in (6, 8):
        v += attacker.i32(0x1E4) * 0x100 + attacker.i32(0x184) * 0x100
    off = _ARMOR_C10.get(armor_class)
    if off is not None:
        v += attacker.i32(off) * 0x100
    v = to_int32(v + 0x6400)
    if v < 0:
        v = 0
    # client returns ((v * stat[+0x300]) >> 8) >> 8 — TWO arithmetic shifts (int32 between).
    # LIVE-VERIFIED 2026-06-14: c10 returned 0x64=100 (a single >>8 gives 25600 — wrong).
    inner = fx_shr8_i32(to_int32(v) * attacker.i32(0x300))
    return to_int32(inner >> 8)


def _sum_00598b30(attacker: StatBlock, element: int, armor_class: int) -> int:
    """FUN_00598b30."""
    r = attacker.i32(0xFC)
    if element == 1:
        r += attacker.i32(0x180)
    elif element in (3, 9, 0xD):
        r += attacker.i32(0x1A4)
    elif element == 5:
        r += attacker.i32(0x1C8) + attacker.i32(0x180)
    elif element in (6, 8):
        r += attacker.i32(0x1E0) + attacker.i32(0x180)
    off = _ARMOR_B30.get(armor_class)
    if off is not None:
        r += attacker.i32(off)
    return to_int32(r)


def _round_drfloat(v: int) -> int:
    """Round a ×256 value to the nearest whole (client: ``if (v&0xff)>0x7e: v+=0x100; v&=~0xff``)."""
    if (v & 0xFF) > 0x7E:
        v += 0x100
    return v & ~0xFF


def _variance_range_00598ed0(mit: int, b30: int, weapon: StatBlock, hi_flag: int) -> tuple[int, int]:
    """Compute [lo, hi] damage bounds (FUN_00598ed0). Returns (lo, hi), both ×256, floor 0x100.

    LIVE-VALIDATED 2026-06-14 (element-5 swing): in_EAX=b30=11, mit=100, hi_flag=10,
    weapon[+0xec]=154, weapon[+0xf0]=64 → lo=0x900 (9.0), hi=0x1000 (16.0), matching the
    client's stack exactly. Two corrections vs the first port:
      * the first term uses ``b30`` (FUN_00598b30's return, passed to ed0 via ``eax``), NOT ``mit``;
      * the middle step is ``((t*(mit<<8))>>8 << 8) / 0x6400`` (== t*mit/100), not ``(t*mit)>>8//0x6400``.
    """
    # t0 = ((hi_flag + b30) << 8) * weapon[+0xec], then >>8   (0x598EE5..0x598EFD)
    t = fx_shr8_i32((hi_flag * 0x100 + b30 * 0x100) * weapon.i32(0xEC))
    # a = (t * (mit<<8)) >> 8  (== t*mit)   (0x598F18 imul, 0x598F1C shrd 8)
    a = fx_shr8_i32(to_int32(t) * to_int32(mit << 8))
    # t = (a << 8) / 0x6400  — signed idiv, trunc toward zero   (0x598F3E shrd/sar, 0x598F45 idiv)
    t = _idiv(to_int32(a) << 8, 0x6400)
    spread = fx_shr8_i32(to_int32(t) * weapon.i32(0xF0))                  # weapon+0xf0 spread factor
    lo = _round_drfloat(to_int32(t - spread))
    hi = _round_drfloat(to_int32(t + spread))
    if lo < 0x100:
        lo = 0x100
    if hi < 0x100:
        hi = 0x100
    return lo, hi


def _block_curve_00598810(attacker_range: int, defender_range: int, base: int) -> int:
    """FUN_00598810 — block/parry via CurveTable interpolation.

    DATA-DRIVEN: the client resolves an ``ArchetypeRef<CurveTable>`` and interpolates.
    Bit-exact reproduction needs the client's curve data (extracted content). Until that
    is ported, return the no-curve fallback (the client returns ``in_EAX`` = ``base`` when
    the curve refs don't resolve). TODO(path-a): port the block curve. # UNVERIFIED
    """
    return base


def compute_swing(
    attacker: StatBlock,
    defender: StatBlock,
    weapon: StatBlock,
    mt: MersenneTwister,
    *,
    element: int,
    armor_class: int,
    melee_in_range: bool,
    accuracy_target_term: int = 0,   # resolver: *piVar4 (target0[0])
    level_delta: int = 0,            # resolver: puStack_64 = iVar19 - iVar18 (level/skill delta)
    block_disabled: bool = False,    # resolver: *(damage+0x18) != 0  → blockChance forced 0
    extra_c10: int = 0,              # resolver: piVar4[1]
    crit_extra: int = 0,             # FUN_00598fd0 param_2 (param_1[6])
) -> SwingResult:
    """Port of ``FUN_00597e50`` for one swing. Consumes ``mt`` in the exact client order
    (see ``docs/COMBAT_FORMULA.md`` §1): draw#1 hit/miss, draw#2 block, draw#3 variance.
    """
    # ---- accuracy / hit threshold (COMBAT_FORMULA §2) ----
    acc_skill = _acc_skill_00598950(attacker, element)
    acc_bonus = _acc_bonus_005989c0(attacker, element)
    acc = to_int32(((acc_skill + attacker.i32(0xF0)) *
                    (acc_bonus + attacker.i32(0xF4) + accuracy_target_term + 100)) // 100)
    if acc < 0:
        acc = 0
    defence = _avoid_005988d0(defender, element)
    if melee_in_range:
        acc = to_int32(acc - 0x8C)
        defence = _block_curve_00598810(attacker.u16(0x314), defender.u16(0x314), defence)
    hit_pct = 0 if (acc + defence) == 0 else to_int32(acc * 100 // (acc + defence))

    weapon_range_delta = to_int32(attacker.u16(0x314) * 0x100)  # iVar19 in resolver
    if not (melee_in_range or block_disabled):
        weapon_range_delta = to_int32(defender.u16(0x314) << 8)
    range_adj = fx_shr8_i32(to_int32(weapon_range_delta + attacker.u16(0x314) * -0x100) * 0x500)
    hit_threshold = to_int32(hit_pct * 0x100 - range_adj)
    if (not melee_in_range) and hit_threshold < 0xA00:
        hit_threshold = 0xA00

    block_chance = 0 if block_disabled else defender.u16(0x138)

    # ---- draw #1: hit/miss ----
    roll_hit = mt.generate()
    roll1 = roll_hit % 0x6464
    draws = 1

    # ---- draw #2: block/dodge gate (UNCONDITIONAL) ----
    roll_block = mt.generate()
    roll2 = (roll_block >> 8 & 0xFF) % 100 + 1
    draws = 2

    hit = roll1 < hit_threshold
    blocked = not (block_chance <= roll2)
    if not hit or blocked:
        return SwingResult(False, blocked and hit, 0, False, draws, roll_hit, roll_block, 0)

    # ---- damage (COMBAT_FORMULA §3), only reached on a landed hit ----
    base = _base_dmg_00598a30(attacker, element)
    dmg = to_int32(base + fx_shr8_i32(to_int32(level_delta) * 0x500))   # + level_delta*5
    clamped_base = dmg
    if melee_in_range:
        dmg = fx_mul_shr8(dmg, 0xC0)            # ×0.75
        clamped_base = dmg
    if dmg > 0x5A00:
        dmg = 0x5A00
        clamped_base = 0x5A00

    mit = _mitigation_00598c10(attacker, element, armor_class, extra_c10) & 0xFFFF
    # FUN_00598b30's return is passed to ed0 via eax (the variance "power" term) — live-verified.
    b30 = _sum_00598b30(attacker, element, armor_class)
    lo, hi = _variance_range_00598ed0(mit, b30, weapon, crit_extra)

    # ---- draw #3: damage variance ----
    # LIVE-VERIFIED at 0x599016 (2026-06-14): the client does, with edi=lo, [esp+0x10]=hi:
    #   range  = ((hi >> 8) << 8) - lo + 1        (0x59901A sar 8; 0x59901D shl 8; 0x599020 sub edi; inc)
    #   result = (draw % range) + lo              (0x599025 div; 0x599027 mov eax,edx; 0x599029 add edi)
    # (Earlier port used `draw % ((hi>>8)*0x100+1)` with no lo term — wrong.)
    roll_var = mt.generate()
    draws = 3
    rng = to_int32(((hi >> 8) << 8) - lo + 1)
    dmg_final = (roll_var % rng) + lo if rng > 0 else lo

    # ---- crit: reuses draw#1 (no new draw) ----
    crit = roll1 < clamped_base
    if crit:
        dmg_final = to_int32(attacker.i32(0x118) * dmg_final // 100)
    if dmg_final < 0x100:
        dmg_final = 0x100

    return SwingResult(True, False, dmg_final, crit, draws, roll_hit, roll_block, roll_var)
