"""Monster level + HP derivation — exact port of C# ``MonsterHealthTable``
(DatabaseLoader.cs:869) plus ``GetLevelForTier`` / ``GetZoneBaseLevel``
(CombatManager.cs:750/767).

WHY THIS EXISTS (the dungeon "sync error" root cause): the client computes a
monster's HP **locally** from its own asset/level data and then compares it
*exactly* against the value the server puts in the entity-synch field of the
spawn packet (the ``0x02 <HP>`` tail of the SpawnAction / MoverUpdate). The
client's ``FUN_005dd900`` synch check (entity field ``+0xbc`` vs the remote
value) raises *"Entity synch error detected … Oops! You've encountered a sync
error."* on any mismatch — confirmed in the client binary via Ghidra.

Two facts, both proven against the LIVE client (the source-of-truth top of the
hierarchy, above the C# server):

1. The **raw HP magnitude** is level-scaled, NOT ``hit_points * 256`` from the DB.
   C# derives it with ``CalculateHP(calculatedLevel, difficulty)`` (CombatManager.cs
   :310) — a level-2 RECRUIT warg pup is raw 114, not ``60``/``60*256``.

2. The wire/synch field is the raw HP **× 256** (fixed point), like every other
   HP on the wire. The live crash log for ``dungeon00_level01`` shows the client's
   local value ``HP = 29184`` for "Dew Valley Pup" — exactly ``114 * 256`` — while
   the server sent raw ``114`` → fatal synch mismatch. C# itself sets
   ``CurrentHPWire = (uint)CalculateHP(...)`` (raw) yet reads it back as
   ``HP => CurrentHPWire / 256`` (Monster.cs:90): a latent C# scaling bug that
   would break this zone in C# too. We follow the client: send raw HP × 256.

``calculate_hp`` returns the raw C# magnitude; ``monster_hp_wire`` returns the
×256 value that goes on the wire / in the entity-synch trailers.

EXACT MATH (do not approximate): C# ``MonsterHealthTable.CalculateHP``
(DatabaseLoader.cs:885) is **Fixed32 integer** math, NOT float —
``baseF32 = GetCurveValueFixed32("MonsterHealth", clamp(level,1,110))`` then
``hp = ((baseF32 * modF32) >> 8); hp = (hp * diffF32) >> 8; return max(1, hp >> 8)``
with ``diffF32 = int(difficulty*256)`` and ``modF32 = int(mod*256)``. A naive
float ``int(base_hp * difficulty)`` matches at *some* (level, difficulty) combos
(incl. every mob in dungeon00_level01) but diverges by ±1 at others (e.g. L13
RECRUIT: float 714 vs exact 713). ±1 raw == ±256 wire == a **fatal** zero-tolerance
synch crash, so the Fixed32 path is mandatory for correctness in higher dungeons.
The base curve + the exact double-truncation interpolation live in
``drserver.combat.monster_curves`` (the client's ``CurveTableEntry::getValue``).
"""
from __future__ import annotations

from ..combat.monster_curves import monster_health_base_fixed32

# Case-insensitive difficulty → HP multiplier (C# DifficultyModifiers).
_DIFFICULTY_MODIFIERS = {
    "FODDER": 0.5,
    "RECRUIT": 1.0,
    "VETERAN": 2.0,
    "WARMONGER": 2.5,
    "CHAMPION": 4.0,
    "HERO": 7.0,
    "DUNGEON_BOSS": 8.0,
    "BOSS": 8.0,
}

# Case-insensitive tier → base level offset (C# GetLevelForTier). Tier is the
# same column as creature_difficulty (C# ``CreatureData.tier => creatureDifficulty``).
_TIER_LEVELS = {
    "FODDER": 0,
    "RECRUIT": 1,
    "VETERAN": 2,
    "CHAMPION": 4,
    "HERO": 6,
    "WARMONGER": 8,
}


def base_hp(level: int) -> float:
    """C# ``GetBaseHP``: ``GetCurveValue("MonsterHealth", clamp(level,1,110))``
    = the Fixed32 curve value / 256 (float view). Use ``calculate_hp`` for the
    exact integer result — this float view is for display/diagnostics only."""
    return monster_health_base_fixed32(level) / 256.0


def difficulty_modifier(difficulty: str) -> float:
    """C# ``GetDifficultyModifier`` — unknown/empty difficulty → 1.0."""
    if not difficulty:
        return 1.0
    return _DIFFICULTY_MODIFIERS.get(difficulty.upper(), 1.0)


def calculate_hp(level: int, difficulty, mod: float = 1.0) -> int:
    """Exact C# ``MonsterHealthTable.CalculateHP`` (Fixed32 integer math).

    ``baseF32 = GetCurveValueFixed32("MonsterHealth", clamp(level,1,110))``;
    ``hp = (baseF32 * modF32) >> 8``; ``hp = (hp * diffF32) >> 8``;
    ``return max(1, hp >> 8)`` with ``diffF32/modF32 = int(x * 256)``. This is
    bit-exact to the value the client computes locally and compares against the
    spawn entity-synch field — a float approximation can be ±1 off and crash.

    ``difficulty`` is the tier STRING (mapped via ``difficulty_modifier``) OR the
    raw numeric multiplier (the creature's ``Difficulty`` field) when a float is
    passed — ★2026-06-21 (bible §14.4): the numeric ``Difficulty`` is the real
    multiplier (the tier class only SETS its default; bosses/leaders OVERRIDE it,
    e.g. Rotgut 25, Menacing Manglefeet 30). For 1255/1261 regular creatures the
    two are equal (a no-op), so passing the numeric corrects the override mobs
    without touching the live-verified baseline.
    """
    base_f32 = monster_health_base_fixed32(level)
    dmult = (difficulty if isinstance(difficulty, (int, float))
             else difficulty_modifier(difficulty))
    diff_f32 = int(max(0.0, dmult) * 256.0)
    mod_f32 = int(max(0.0, mod) * 256.0)
    hp_f32 = (base_f32 * mod_f32) >> 8
    hp_f32 = (hp_f32 * diff_f32) >> 8
    return max(1, hp_f32 >> 8)


def level_for_tier(tier: str) -> int:
    """C# ``GetLevelForTier`` — unknown/empty tier → 1."""
    if not tier:
        return 1
    return _TIER_LEVELS.get(tier.upper(), 1)


def zone_base_level(zone_name: str) -> int:
    """The level of a baseline (``LevelOffset 0``) mob in a zone — tutorial → 2,
    ``dungeonNN…`` → ``NN*4 + 2``.

    ★2026-06-21 (bible §14.1/§14.3): calibrated ``+2`` (was the C# ``+1``) so that
    removing the bogus tier→level offset (see :func:`monster_level`) PRESERVES the
    live-verified baseline mob levels instead of dropping them a level. Under the
    old model a baseline RECRUIT mob landed at ``(NN*4+1) + GetLevelForTier(RECRUIT)
    = NN*4+2``; folding that ``+1`` into the base keeps every RECRUIT/baseline mob
    at its previously-working level (the live "Dew Valley Pup" L2 / curve(2)=114 in
    dungeon00 and the "Whisker Flinger" L6 / curve(6)=332 in dungeon01 both still
    hold) while the higher tiers stop double-counting their multiplier as levels.
    The absolute slope (``NN*4``) remains UNVERIFIED against the client — see §14.1.
    """
    if not zone_name:
        return 2
    lower = zone_name.lower()
    if "tutorial" in lower:
        return 2
    if lower.startswith("dungeon") and len(lower) >= 9:
        num_str = lower[7:9]
        if num_str.isdigit():
            return int(num_str) * 4 + 2
    return 2


def monster_level(difficulty: str, zone_name: str, level_offset: int = 0) -> int:
    """Grounded monster level — ``min(100, max(1, ZoneBaseLevel + LevelOffset))``.

    ★2026-06-21 (bible §14.1): the C# ``GetLevelForTier`` tier→level offset that
    used to be added here is an **emulator invention with no grounding** in the
    client or extracted content — the creature *tier* (``creature_difficulty``)
    defines an HP/damage **multiplier only** (``UnitMelee_*.gc`` ``Difficulty``),
    NOT a level bump. Adding it on TOP of the multiplier double-counted the tier
    (a CHAMPION got +4 levels AND ×4.0 HP), the proven cause of "dungeon01 mobs
    are unbeatable". The real, authored per-unit bump is the encounter
    ``EncounterUnit.LevelOffset`` (boss +2, guards +1, trash +0), threaded in via
    ``level_offset``. The ``difficulty`` argument is retained for signature
    compatibility but no longer affects the level.
    """
    return min(100, max(1, zone_base_level(zone_name) + level_offset))


def monster_hp_wire(difficulty: str, zone_name: str, mod: float = 1.0,
                    level_offset: int = 0,
                    difficulty_value: "float | None" = None) -> int:
    """The exact ×256 wire HP the client expects in the entity-synch field.

    Raw level-scaled HP (``calculate_hp``) × 256, where ``mod`` is the creature's
    **HealthMod** (the ``max_health`` field in the ``creatures`` content — a float
    multiplier, NOT raw HP). The client multiplies the level curve by this mod and
    by the difficulty modifier, then compares the result *exactly* against this
    wire field; dropping the mod is a fatal zero-tolerance synch crash.

    ``level_offset`` is the encounter ``LevelOffset`` (see :func:`monster_level`);
    it MUST match whatever ``level`` the spawn packet sends or the synch crashes.

    Live ground truth:
      * dungeon00_level01 RECRUIT warg pup: mod 1.0 → raw 114 → wire 29184.
      * dungeon01_level03 RECRUIT "Whisker Flinger": curve(6)=332, mod 0.75 →
        raw 249 → wire 63744 (the client's local ``HP`` in the synch-error crash).
    The mod is required for every dungeon past 00 (most creatures use 1.25/0.75)."""
    lvl = monster_level(difficulty, zone_name, level_offset)
    dmult = difficulty if difficulty_value is None else difficulty_value
    return calculate_hp(lvl, dmult, mod) * 256
