"""Class passives, spawn/free-player modifiers, and the skill-trainer port.

Wire-shape sources: PassiveSkill::readInit @0x53D0E0 (Ghidra, reads one u32
after the shared id+level) for the OP4 passive tail; DRS-NET ClassPassiveData
for the HP/mana math (its L1 no-passive case equals the live-proven Styx3
baseline 266 HP / 175 MP); DRS-NET SendZoneSpawnInvulnerability /
SendFreePlayerModifier / HandleSkillTrainRequest for the modifier + trainer
wire. Expected bonus integers below were hand-derived from the C# fixed-point
math (NOT computed via the code under test).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from drserver.data import class_passives
from drserver.data.saved_character import HotbarSlotEntry, SavedCharacter, SkillLevelEntry
from drserver.managers import player_modifiers
from drserver.util.byte_io import LEReader


def _saved(class_name: str = "Mage", skills=None, hotbar=None,
           endurance: int = 0, intellect: int = 0, level: int = 1) -> SavedCharacter:
    ch = SavedCharacter()
    ch.class_name = class_name
    ch.level = level
    ch.skills = skills or []
    ch.hotbar_slots = hotbar or []
    ch.stat_endurance = endurance
    ch.stat_intellect = intellect
    return ch


# ── client HP/mana formula ─────────────────────────────────────────────────────
class TestPassiveHPMath:
    @pytest.mark.unit
    def test_level1_no_passive_matches_live_baseline(self):
        # Arrange/Act/Assert: Styx3 live reading — 266 HP / 175 MP wire.
        assert class_passives.calculate_hp_wire(1, 10, 0) == 68096
        assert class_passives.calculate_mana_wire(1, 10, 0) == 44800

    @pytest.mark.unit
    def test_flat_per_level_increments_preserved(self):
        # The pre-passive flat baseline: 68096 + 4096/level, 44800 + 1280/level.
        assert class_passives.calculate_hp_wire(5, 10, 0) == 68096 + 4 * 4096
        assert class_passives.calculate_mana_wire(5, 10, 0) == 44800 + 4 * 1280

    @pytest.mark.unit
    @pytest.mark.parametrize("cls,hp_bonus,mana_bonus", [
        # Hand-derived from C# ClassPassiveData fixed-point math at L1, alloc 0:
        # Mage: end 15 @75% -> 281+16=297 HP; int 15 @200% -> 510+5=515 MP.
        ("Mage", (297 - 266) * 256, (515 - 175) * 256),
        # Fighter: end 5 @150% -> 187+16=203 HP; int 5 @75% -> 63+5=68 MP.
        ("Fighter", (203 - 266) * 256, (68 - 175) * 256),
        # Ranger: end 15 @110% -> 411+16=427 HP; int 5 @95% -> 80+5=85 MP.
        ("Ranger", (427 - 266) * 256, (85 - 175) * 256),
    ])
    def test_class_passive_bonuses_match_csharp(self, cls, hp_bonus, mana_bonus):
        assert class_passives.class_passive_hp_bonus_wire(cls, 1, 0) == hp_bonus
        assert class_passives.class_passive_mana_bonus_wire(cls, 1, 0) == mana_bonus

    @pytest.mark.unit
    def test_unknown_class_has_zero_bonus(self):
        assert class_passives.class_passive_hp_bonus_wire("Paladin") == 0
        assert class_passives.class_passive_mana_bonus_wire("Paladin") == 0

    @pytest.mark.unit
    def test_saved_bonus_requires_shipped_passive(self):
        # No passive rows -> baseline preserved (legacy chars keep working HP).
        bare = _saved("Mage")
        assert class_passives.saved_character_passive_hp_bonus_wire(bare) == 0
        with_passive = _saved("Mage", skills=["skills.generic.MageClassPassive"])
        assert (class_passives.saved_character_passive_hp_bonus_wire(with_passive)
                == (297 - 266) * 256)


# ── passive manipulator collection ─────────────────────────────────────────────
class TestCollectPassives:
    @pytest.mark.unit
    def test_hotbar_passives_collected_with_authored_slots(self):
        saved = _saved("Mage", hotbar=[
            HotbarSlotEntry(skill="skills.generic.MageClassPassive", slot=108),
            HotbarSlotEntry(skill="skills.generic.MagicDamageModPassive", slot=109),
            HotbarSlotEntry(skill="skills.generic.FireBolt", slot=105),  # active: skipped
        ])
        result = class_passives.collect_passive_manipulators(saved)
        assert [(p.skill, p.slot, p.modifier_id) for p in result] == [
            ("skills.generic.MageClassPassive", 108, 0xF000),
            ("skills.generic.MagicDamageModPassive", 109, 0xF001),
        ]

    @pytest.mark.unit
    def test_legacy_character_without_hotbar_rows_falls_back(self):
        # Pre-fix characters have the skill rows but hotbar_slot = -1.
        saved = _saved("Fighter", skills=[
            "skills.generic.Butcher",
            "skills.generic.FighterClassPassive",
            "skills.generic.MeleeAttackSpeedModPassive",
        ])
        result = class_passives.collect_passive_manipulators(saved)
        assert [(p.skill, p.slot) for p in result] == [
            ("skills.generic.FighterClassPassive", 108),
            ("skills.generic.MeleeAttackSpeedModPassive", 109),
        ]

    @pytest.mark.unit
    def test_skill_levels_carried(self):
        saved = _saved("Mage", hotbar=[
            HotbarSlotEntry(skill="skills.generic.MageClassPassive", slot=108)])
        saved.skill_levels = [SkillLevelEntry(skill="skills.generic.MageClassPassive", level=4)]
        assert class_passives.collect_passive_manipulators(saved)[0].level == 4

    @pytest.mark.unit
    def test_is_passive_skill(self):
        assert class_passives.is_passive_skill("skills.generic.FighterClassPassive")
        assert class_passives.is_passive_skill("skills.generic.SomeTrait")
        assert not class_passives.is_passive_skill("skills.generic.FireBolt")
        assert not class_passives.is_passive_skill("")


# ── spawn-invulnerability + free-player modifiers ──────────────────────────────
class _FakeConn:
    def __init__(self, zone: str = "dungeon01_level01"):
        self.modifiers_id = 0x0123
        self.current_zone_name = zone
        self.login_name = "tester"
        self.hp_wire = 68096
        self.client_hp_wire = None
        self.sent: list[bytes] = []

    def send_to_client(self, data: bytes) -> None:
        self.sent.append(data)


class TestSpawnModifiers:
    @pytest.mark.unit
    @pytest.mark.parametrize("zone,allowed", [
        ("dungeon01_level01", True), ("world.dungeon00_level01", True),
        ("amazon_dungeon", True), ("elite01_intro", True), ("epic01", True),
        ("town", False), ("world.town", False), ("tutorial", False),
        ("thehub", False), ("pvp_hub", False), ("", False),
    ])
    def test_zone_gate(self, zone, allowed):
        assert player_modifiers.zone_allows_spawn_invulnerability(zone) is allowed

    @pytest.mark.unit
    def test_invulnerability_packet_bytes(self):
        conn = _FakeConn()
        assert player_modifiers.send_zone_spawn_invulnerability(conn) is True
        r = LEReader(conn.sent[0])
        assert r.read_byte() == 0x07
        assert r.read_byte() == 0x35
        assert r.read_uint16() == 0x0123          # modifiers component id
        assert r.read_byte() == 0x00              # Add modifier
        assert r.read_byte() == 0xFF
        assert r.read_cstring() == "avatar.base.ZoneSpawnInvulnerabilityModifier"
        assert r.read_uint32() == 3               # fixed instance id (C#)
        assert r.read_byte() == 0                 # level
        assert r.read_uint32() == 0               # power level
        assert r.read_uint32() == 1800            # duration ticks
        assert r.read_byte() == 0x01              # sourceIsSelf
        assert r.read_byte() == 0x02              # synch trailer
        assert r.read_uint32() == 68096
        assert r.read_byte() == 0x06

    @pytest.mark.unit
    def test_invulnerability_skipped_in_town(self):
        conn = _FakeConn(zone="town")
        assert player_modifiers.send_zone_spawn_invulnerability(conn) is False
        assert conn.sent == []

    @pytest.mark.unit
    def test_free_player_modifier_resent_every_spawn(self, monkeypatch):
        # Persistent for free accounts: the client drops modifiers on every
        # zone change, so the server re-sends on EVERY zone-entry spawn.
        monkeypatch.setattr(player_modifiers, "_account_is_member", lambda _: False)
        conn = _FakeConn()
        assert player_modifiers.send_free_player_modifier(conn) is True
        assert player_modifiers.send_free_player_modifier(conn) is True
        assert len(conn.sent) == 2
        r = LEReader(conn.sent[0])
        r.read_byte(); r.read_byte(); r.read_uint16(); r.read_byte()
        assert r.read_byte() == 0xFF
        assert r.read_cstring() == "avatar.base.FreePlayerExperienceModifier"
        assert r.read_uint32() == 1               # fixed instance id (C#)
        r.read_byte()
        r.read_uint32()
        assert r.read_uint32() == 0               # permanent

    @pytest.mark.unit
    def test_free_player_modifier_skipped_for_members(self, monkeypatch):
        monkeypatch.setattr(player_modifiers, "_account_is_member", lambda _: True)
        conn = _FakeConn()
        assert player_modifiers.send_free_player_modifier(conn) is False
        assert conn.sent == []


# ── skill trainer ──────────────────────────────────────────────────────────────
class TestTrainerHelpers:
    @pytest.mark.unit
    def test_is_trainer(self):
        from drserver.managers import trainers
        assert trainers.is_trainer("world.town.npc.TrainerFighter")
        assert trainers.is_trainer("world.town.npc.TrainerMage")
        assert not trainers.is_trainer("world.town.npc.HermitVendor")

    @pytest.mark.unit
    def test_skill_trainer_gc_type(self):
        from drserver.managers import trainers
        assert (trainers.skill_trainer_gc_type("world.town.npc.TrainerFighter")
                == "world.town.npc.base.TrainerFighterBase.SkillTrainer")

    @pytest.mark.unit
    def test_component_block_bytes(self):
        from drserver.managers import trainers
        from drserver.util.byte_io import LEWriter
        w = LEWriter()
        trainers.write_skill_trainer_component(
            w, "world.town.npc.TrainerMage", 0x1234, 0x0457)
        r = LEReader(w.to_array())
        assert r.read_byte() == 0x32
        assert r.read_uint16() == 0x1234
        assert r.read_uint16() == 0x0457
        assert r.read_byte() == 0xFF              # preserve-case GC type marker
        assert r.read_cstring() == "world.town.npc.base.TrainerMageBase.SkillTrainer"
        assert r.read_byte() == 0x00

    @pytest.mark.unit
    def test_train_gold_cost_formula(self):
        from drserver.managers import trainers
        # C#: (requiredLevel + (next-1)*gvm) * 1113.621 * gvm; FireBolt rank 1
        # (required 3, gvm 1.0) -> int(3 * 1113.621) = 3340.
        assert trainers.train_gold_cost(3, 1, 1.0) == 3340
        assert trainers.train_gold_cost(3, 2, 1.0) == int(4 * 1113.621)
        assert trainers.train_gold_cost(1, 1, 0.0001) == 1   # floor at 1 gold


class TestTrainerClassGate:
    @pytest.mark.unit
    def test_trainer_class_mapping(self):
        from drserver.managers import trainers
        assert trainers.trainer_class("world.town.npc.TrainerFighter") == "Fighter"
        assert trainers.trainer_class("world.town.npc.TrainerMage") == "Mage"
        assert trainers.trainer_class("world.town.npc.TrainerRanger") == "Ranger"
        assert trainers.trainer_class("world.town.npc.HermitVendor") is None

    @pytest.mark.unit
    def test_own_class_trainer_allows(self):
        from drserver.managers import trainers
        ok, _ = trainers.can_train("skills.generic.FireBolt", "Mage",
                                   "world.town.npc.TrainerMage")
        assert ok

    @pytest.mark.unit
    def test_cross_class_trainer_allowed_for_non_passives(self):
        # Any class may learn regular skills from any trainer — only the
        # class passives are gated.
        from drserver.managers import trainers
        ok, _ = trainers.can_train("skills.generic.Butcher", "Mage",
                                   "world.town.npc.TrainerFighter")
        assert ok

    @pytest.mark.unit
    def test_foreign_class_passive_denied_at_any_trainer(self):
        from drserver.managers import trainers
        ok, msg = trainers.can_train("skills.generic.FighterClassPassive", "Mage",
                                     "world.town.npc.TrainerFighter")
        assert not ok and "Fighter" in msg

    @pytest.mark.unit
    def test_own_class_passive_allowed(self):
        from drserver.managers import trainers
        ok, _ = trainers.can_train("skills.generic.MageClassPassive", "Mage",
                                   "world.town.npc.TrainerMage")
        assert ok

    @pytest.mark.unit
    def test_foreign_class_passive_denied_even_at_own_trainer(self):
        # A spoofed FighterClassPassive hash sent to the MAGE trainer by a Mage:
        # the trainer match passes, the passive-exclusivity rule must refuse.
        from drserver.managers import trainers
        ok, msg = trainers.can_train("skills.generic.FighterClassPassive", "Mage",
                                     "world.town.npc.TrainerMage")
        assert not ok and "Fighter" in msg

    @pytest.mark.unit
    def test_passive_rule_holds_without_trainer_context(self):
        from drserver.managers import trainers
        ok, _ = trainers.can_train("skills.generic.FireBolt", "Mage", "")
        assert ok
        ok, _ = trainers.can_train("skills.generic.RangerClassPassive", "Mage", "")
        assert not ok


@pytest.fixture(scope="module")
def content_db():
    from _paths import copy_shipped_db, has_shipped_db
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())


class TestPassiveProfessionGate:
    """All passives with an authored class ProfessionType are class-locked —
    not just the three <Class>ClassPassives (live bug: a Mage could buy
    MeleeAttackSpeedModPassive from the Fighter trainer)."""

    @pytest.mark.integration
    def test_profession_tagged_passive_denied_cross_class(self, content_db):
        from drserver.managers import trainers
        trainers._passive_owner_cache.clear()
        ok, msg = trainers.can_train(
            "skills.generic.MeleeAttackSpeedModPassive", "Mage",
            "world.town.npc.TrainerFighter")
        assert not ok and "Fighter" in msg

    @pytest.mark.integration
    def test_profession_tagged_passive_allowed_for_own_class(self, content_db):
        from drserver.managers import trainers
        trainers._passive_owner_cache.clear()
        ok, _ = trainers.can_train(
            "skills.generic.MeleeAttackSpeedModPassive", "Fighter",
            "world.town.npc.TrainerFighter")
        assert ok

    @pytest.mark.integration
    @pytest.mark.parametrize("skill", [
        "skills.generic.DivineResistPassive",   # ProfessionType NONE — shared
        "skills.generic.PoisonResistPassive",
    ])
    def test_shared_resist_passives_open_to_all(self, content_db, skill):
        from drserver.managers import trainers
        trainers._passive_owner_cache.clear()
        ok, _ = trainers.can_train(skill, "Mage", "world.town.npc.TrainerRanger")
        assert ok

    @pytest.mark.integration
    @pytest.mark.parametrize("skill,owner", [
        ("skills.generic.MagicDamageModPassive", "Mage"),
        ("skills.generic.RangeAttackSpeedModPassive", "Ranger"),
        ("skills.generic.BlockKnockdownProcPassive", "Fighter"),
        ("skills.generic.ShadowLightningUpgradeProcPassive", "Mage"),
        ("skills.generic.InfectiousPoisonUpgradeProcPassive", "Ranger"),
    ])
    def test_owner_class_resolution_from_authored_profession(self, content_db,
                                                             skill, owner):
        from drserver.managers import trainers
        trainers._passive_owner_cache.clear()
        assert trainers.passive_owner_class(skill) == owner
