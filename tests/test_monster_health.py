"""Monster HP/level derivation tests — guards the dungeon "sync error" fix.

The client compares the server-sent entity-synch HP against a value it computes
locally and raises *"Entity synch error"* on any mismatch. The server must
reproduce C# ``MonsterHealthTable.CalculateHP`` + ``GetLevelForTier`` +
``GetZoneBaseLevel`` exactly (CombatManager.cs:310 sends ``CalculateHP`` — raw,
not ×256). ``CalculateHP`` is Fixed32 integer math over the ``MonsterHealth``
CurveTable (DatabaseLoader.cs:885), not float — see ``monster_health.py``.
"""
import _paths  # noqa: F401  (sets sys.path)
from drserver.managers import monster_health as mh


def test_base_hp_endpoints_and_clamp():
    # Arrange / Act / Assert — linear endpoints and out-of-range clamp.
    assert mh.base_hp(1) == 60.5
    assert round(mh.base_hp(100)) == 5452
    assert mh.base_hp(0) == mh.base_hp(1)        # clamp low
    assert mh.base_hp(250) == mh.base_hp(100)    # clamp high


def test_difficulty_modifier_known_and_unknown():
    assert mh.difficulty_modifier("RECRUIT") == 1.0
    assert mh.difficulty_modifier("veteran") == 2.0   # case-insensitive
    assert mh.difficulty_modifier("BOSS") == 8.0
    assert mh.difficulty_modifier("") == 1.0          # empty → 1
    assert mh.difficulty_modifier("GRUNT") == 1.0     # unknown → 1


def test_zone_base_level():
    # ★2026-06-21 (bible §14.1): base recalibrated +2 (was C# +1) so dropping the
    # bogus tier→level offset preserves the live-verified baseline mob levels.
    assert mh.zone_base_level("tutorial") == 2
    assert mh.zone_base_level("dungeon00_level01") == 2   # 00*4+2
    assert mh.zone_base_level("dungeon01_level02") == 6   # 01*4+2
    assert mh.zone_base_level("dungeon02_level03") == 10  # 02*4+2
    assert mh.zone_base_level("") == 2


def test_warg_pup_recruit_in_dungeon00():
    # Arrange — RECRUIT pup in dungeon00_level01.
    # Act
    level = mh.monster_level("RECRUIT", "dungeon00_level01")
    hp = mh.monster_hp_wire("RECRUIT", "dungeon00_level01")
    # Assert — level 2 (base 2 + LevelOffset 0); raw int(GetBaseHP(2)*1.0)=114.
    # PRESERVED across the §14 level-model fix (base +2 absorbs the dropped tier
    # offset). Live ground truth: client synch HP for "Dew Valley Pup" == 29184.
    assert level == 2
    assert mh.calculate_hp(2, "RECRUIT") == 114
    assert hp == 114 * 256
    assert hp == 29184


def test_warg_grunt_veteran_in_dungeon00():
    # ★2026-06-21 (bible §14.3): tier no longer adds to level — VETERAN now sits at
    # the zone base (level 2), its 2.0× HP coming from the difficulty MULTIPLIER
    # alone (not a compounded level). raw int(GetBaseHP(2)*2.0)=229.
    assert mh.monster_level("VETERAN", "dungeon00_level01") == 2
    assert mh.monster_hp_wire("VETERAN", "dungeon00_level01") == 229 * 256


def test_health_mod_applied_dungeon01_whisker_flinger():
    """LIVE GROUND TRUTH (x32dbg, 2026-06-08): warping into dungeon01_level03 the
    client's local synch HP for a RECRUIT "Whisker Flinger" (HealthMod 0.75) was
    63744 (= 249 × 256). We previously sent the mod-1.0 value (332) and crashed
    the zero-tolerance synch compare. The mod must be threaded through."""
    # level 6 (tier 1 + zone 5); curve(6)=332, ×0.75 = 249.
    assert mh.monster_level("RECRUIT", "dungeon01_level03") == 6
    assert mh.calculate_hp(6, "RECRUIT", 1.0) == 332
    assert mh.calculate_hp(6, "RECRUIT", 0.75) == 249
    assert mh.monster_hp_wire("RECRUIT", "dungeon01_level03", 0.75) == 63744
    # mod defaults to 1.0 (dungeon00 mobs all use 1.0 — no regression).
    assert mh.monster_hp_wire("RECRUIT", "dungeon00_level01") == \
        mh.monster_hp_wire("RECRUIT", "dungeon00_level01", 1.0)


def test_boss_champion_in_dungeon00():
    # ★2026-06-21 (bible §14.3): tier no longer adds to level — CHAMPION now sits at
    # the zone base (level 2), its 4.0× HP from the difficulty MULTIPLIER alone (the
    # old level 5 double-counted the tier). raw int(GetBaseHP(2)*4.0)=459.
    assert mh.monster_level("CHAMPION", "dungeon00_level01") == 2
    assert mh.monster_hp_wire("CHAMPION", "dungeon00_level01") == 459 * 256


def test_calculate_hp_is_exact_fixed32_not_float():
    # The client uses Fixed32 integer math (CurveTable + >>8 per step), not float.
    # At these (level, difficulty) combos the exact result differs from the naive
    # float int(base_hp*mod) by 1 — and 1 raw == 256 wire == a fatal synch crash.
    # Pin the EXACT values so a regression back to float math fails here.
    assert mh.calculate_hp(13, "RECRUIT") == 713   # float int() would give 714
    assert mh.calculate_hp(13, "FODDER") == 356    # float would give 357
    assert mh.calculate_hp(12, "VETERAN") == 1318  # float would give 1319
    assert mh.calculate_hp(6, "CHAMPION") == 1330  # float would give 1331


def test_calculate_hp_mod_parameter():
    # C# CalculateHP(level, difficulty, mod): mod is a second Fixed32 multiplier
    # applied before difficulty. mod=1.0 is the default spawn path.
    assert mh.calculate_hp(2, "RECRUIT", 1.0) == mh.calculate_hp(2, "RECRUIT")
    # mod=0 clamps to max(1,...) per C# (never returns 0 HP).
    assert mh.calculate_hp(2, "RECRUIT", 0.0) == 1


def test_level_clamp_is_1_to_110():
    # C# MonsterHealthTable clamps to [1,110]; the MonsterHealth curve saturates
    # at its L100 endpoint, so L100..L110 share the L100 value.
    assert mh.calculate_hp(110, "RECRUIT") == mh.calculate_hp(100, "RECRUIT")
    assert mh.calculate_hp(0, "RECRUIT") == mh.calculate_hp(1, "RECRUIT")


def test_hp_wire_is_raw_times_256():
    # The synch field is the raw level-scaled HP × 256 (fixed point), matching the
    # client's local value. The raw magnitude is small; the wire value is ×256.
    raw = mh.calculate_hp(mh.monster_level("RECRUIT", "dungeon00_level01"), "RECRUIT")
    assert raw < 1000
    assert mh.monster_hp_wire("RECRUIT", "dungeon00_level01") == raw * 256


if __name__ == "__main__":
    import sys
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
