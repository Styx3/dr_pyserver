"""DB-backed character DTOs.

Ported from the data classes in C# Data/ClassConfig.cs (SavedCharacter and
friends). These mirror the SQLite schema loaded by CharacterRepository.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .character import Vector3


@dataclass
class StartingInventoryItem:
    gc_class: str = ""
    x: int = 0
    y: int = 0
    quantity: int = 1


@dataclass
class StartingEquipment:
    weapon: Optional[str] = None
    armor: Optional[str] = None
    helmet: Optional[str] = None
    gloves: Optional[str] = None
    boots: Optional[str] = None
    shoulders: Optional[str] = None
    shield: Optional[str] = None
    ring1: Optional[str] = None
    ring2: Optional[str] = None
    amulet: Optional[str] = None
    slot_rarity: Dict[str, int] = field(default_factory=dict)
    slot_level: Dict[str, int] = field(default_factory=dict)
    # Per-slot ScaleMod GC class ("" = unset → deterministic fallback). Keeps
    # the stat roll the player saw when acquiring the item stable across
    # equip/relog (C# PresetScaleMod, which C# itself loses on zone rebuild).
    slot_scale_mod: Dict[str, str] = field(default_factory=dict)
    # Per-slot items.modpal.* attribute mods (Intellect etc.), parallel to
    # slot_scale_mod, so an equipped item keeps its rolled affixes across
    # equip / zone rebuild / relog.
    slot_mod_refs: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class ClassDefinition:
    display_name: str = ""
    description: str = ""
    starting_equipment: StartingEquipment = field(default_factory=StartingEquipment)
    starting_skills: List[str] = field(default_factory=list)
    starting_inventory: List[StartingInventoryItem] = field(default_factory=list)


@dataclass
class SavedInventoryItem:
    gc_class: str = ""
    x: int = 0
    y: int = 0
    count: int = 1
    buy_price: int = 0
    rarity: int = 0          # ItemRarity (0=Normal .. 5=Mythic)
    stored_level: int = -1   # -1 = legacy / compute from gc class
    scale_mod: str = ""      # ScaleMod GC class rolled at acquire ("" = unset)
    mod_refs: List[str] = field(default_factory=list)  # items.modpal.* affixes


@dataclass
class SkillLevelEntry:
    skill: str = ""
    level: int = 1


@dataclass
class HotbarSlotEntry:
    slot: int = 0      # 0x64 (100) .. 0x6D (109) = hotbar slots 1-10
    skill: str = ""


@dataclass
class SavedQuestObjective:
    objective_name: str = ""
    type: str = ""
    target: str = ""
    label: str = ""
    required: int = 0
    current: int = 0


@dataclass
class SavedQuest:
    quest_id: str = ""
    quest_giver_id: str = ""
    accepted_at: str = ""
    objectives: List[SavedQuestObjective] = field(default_factory=list)


@dataclass
class SavedCharacter:
    id: int = 0
    name: str = ""
    account_id: int = 0
    account_name: str = ""
    class_name: str = "Fighter"
    level: int = 1
    experience: int = 0
    gold: int = 100
    equipment: StartingEquipment = field(default_factory=StartingEquipment)
    skills: List[str] = field(default_factory=list)
    inventory: List[SavedInventoryItem] = field(default_factory=list)
    position: Vector3 = field(default_factory=Vector3)
    zone_id: int = 0
    world_id: int = 0
    current_zone_name: str = "tutorial"
    avatar_class: str = ""
    skin: int = 0
    face: int = 0
    face_feature: int = 0
    hair: int = 0
    hair_color: int = 0
    active_quests: List[SavedQuest] = field(default_factory=list)
    completed_quests: List[str] = field(default_factory=list)
    unlocked_checkpoints: List[str] = field(default_factory=list)
    current_hp: int = 0     # wire format (actual * 256); 0 = use max
    current_mana: int = 0
    max_hp: int = 0
    max_mana: int = 0
    stat_strength: int = 0
    stat_agility: int = 0
    stat_intellect: int = 0
    stat_endurance: int = 0
    last_respec_time: int = 0
    respec_count: int = 0
    pvp_wins: int = 0
    pvp_rating: int = 0
    tp_zone: str = ""
    tp_zone_id: int = 0
    tp_target_zone: str = ""
    tp_pos_x: float = 0.0
    tp_pos_y: float = 0.0
    tp_pos_z: float = 0.0
    skill_levels: List[SkillLevelEntry] = field(default_factory=list)
    hotbar_slots: List[HotbarSlotEntry] = field(default_factory=list)

    def get_skill_level(self, skill_gc_class: str) -> int:
        for e in self.skill_levels:
            if e.skill.lower() == skill_gc_class.lower():
                return e.level
        return 1

    def set_skill_level(self, skill_gc_class: str, level: int) -> None:
        for e in self.skill_levels:
            if e.skill.lower() == skill_gc_class.lower():
                e.level = level
                return
        self.skill_levels.append(SkillLevelEntry(skill=skill_gc_class, level=level))
