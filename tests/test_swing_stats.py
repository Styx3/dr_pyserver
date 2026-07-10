"""swing_stats — DRS-NET DamageResolver port (per-swing combat stat inputs).

The known-good cross-checks come from the proven "LatestPup50024" replay
anchor (see native_swing_input): a starter character with agility 15 had
attack_rating 210 (= 15 x 14), damage_bonus 31 (= floor(2.124 x 15), ranged),
crit_threshold 2048 (= 768 + 1 level x 0x500) and the pup defense rating 52
(authored 1.0 x MonsterDefenseRating curve at L2 -> 53, +-1 interp rounding).
"""
import _paths  # noqa: F401

from drserver.combat import swing_stats
from drserver.combat.monster_curves import monster_defense_rating
from drserver.combat.native_swing_input import build_swing_input


class _Monster:
    def __init__(self, level=1, defense_rating=0.0):
        self.level = level
        self.defense_rating = defense_rating


class TestAvatarAttackRating:
    def test_starter_agility_matches_anchor(self):
        # Arrange / Act
        rating = swing_stats.avatar_attack_rating(15)
        # Assert — the proven LatestPup50024 anchor value
        assert rating == 210

    def test_melee_mod_percent_applies(self):
        assert swing_stats.avatar_attack_rating(10, melee_ar_mod_percent=50) == 210

    def test_ranged_ignores_melee_mod(self):
        assert swing_stats.avatar_attack_rating(10, melee_ar_mod_percent=50,
                                                is_ranged=True) == 140

    def test_never_negative(self):
        assert swing_stats.avatar_attack_rating(0) == 0
        assert swing_stats.avatar_attack_rating(10, melee_ar_mod_percent=-500) == 0


class TestCriticalThreshold:
    def test_equal_levels_is_base_three_percent(self):
        assert swing_stats.critical_threshold(5, 5) == 3 << 8

    def test_one_level_above_matches_anchor(self):
        # The LatestPup50024 trace carried crit_threshold 2048.
        assert swing_stats.critical_threshold(2, 1) == 2048

    def test_out_levelling_is_capped(self):
        assert swing_stats.critical_threshold(110, 1) == 0x5A00

    def test_under_levelling_gets_no_bonus(self):
        assert swing_stats.critical_threshold(1, 50) == 3 << 8


class TestWeaponScaling:
    def test_damage_level_is_item_level_times_ten(self):
        assert swing_stats.scale_weapon_damage_level(1) == 10
        assert swing_stats.scale_weapon_damage_level(5) == 50

    def test_damage_level_floor_is_one(self):
        assert swing_stats.scale_weapon_damage_level(0) == 10  # clamped to lvl 1

    def test_fixed32_from_authored_is_ceil(self):
        assert swing_stats.fixed32_from_authored(0.66) == 169   # ceil(168.96)
        assert swing_stats.fixed32_from_authored(0.5) == 128


class TestWeaponClassId:
    def test_known_classes(self):
        assert swing_stats.weapon_class_id("1HMELEE") == 5
        assert swing_stats.weapon_class_id("2hsword") == 6
        assert swing_stats.weapon_class_id("2HRANGED") == 3
        assert swing_stats.weapon_class_id("1HBOW") == 9
        assert swing_stats.weapon_class_id("HTH") == 1

    def test_unknown_is_zero(self):
        assert swing_stats.weapon_class_id("") == 0
        assert swing_stats.weapon_class_id(None) == 0
        assert swing_stats.weapon_class_id("BANANA") == 0


class TestDamageBonus:
    def test_melee_uses_strength(self):
        # floor(2.3364 x 15) = 35
        assert swing_stats.damage_bonus(5, strength=15, agility=99) == 35

    def test_ranged_uses_agility_and_matches_anchor(self):
        # floor(2.124 x 15) = 31 — the LatestPup50024 damage_bonus
        assert swing_stats.damage_bonus(3, strength=99, agility=15) == 31

    def test_unknown_class_gets_none(self):
        assert swing_stats.damage_bonus(0, strength=15, agility=15) == 0


class TestMonsterDefenseRating:
    def test_authored_one_at_level_two_near_anchor(self):
        # The pup anchor used 52; curve interp at L2 gives 53 (+-1 rounding).
        assert monster_defense_rating(1.0, 2) in (52, 53)

    def test_authored_multiplier_scales(self):
        full = monster_defense_rating(1.0, 1)
        reduced = monster_defense_rating(0.75, 1)
        assert full == 35           # L1 curve value x 1.0
        assert 0 < reduced < full

    def test_zero_authored_is_zero(self):
        assert monster_defense_rating(0.0, 10) == 0

    def test_level_clamped_to_curve_range(self):
        assert monster_defense_rating(1.0, 500) == monster_defense_rating(1.0, 110)


class TestBuildSwingInput:
    def test_no_conn_uses_fallback_anchor(self):
        # Arrange
        monster = _Monster(level=1)
        # Act
        inp = build_swing_input(2, monster)
        # Assert — legacy starter anchor values survive resolver failure
        assert inp.attack_rating == 210
        assert inp.damage_bonus == 31
        assert inp.defense_rating == 52        # no authored DR -> pup fallback
        assert inp.crit_threshold == 2048      # L2 vs L1 — the anchor trace
        assert inp.crit_damage_percent == 200

    def test_authored_monster_dr_uses_curve(self):
        monster = _Monster(level=1, defense_rating=1.0)
        inp = build_swing_input(1, monster)
        assert inp.defense_rating == 35

    def test_levels_pass_through(self):
        monster = _Monster(level=7)
        inp = build_swing_input(9, monster)
        assert inp.attacker_level == 9
        assert inp.defender_level == 7
