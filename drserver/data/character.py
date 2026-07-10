"""Character data model.

Ported from C# Data/Character.cs — the lightweight in-memory character model
(used by GCObject.create_player and as a transient gameplay struct). The rich
DB-backed representation (SavedCharacter) loaded from SQLite lives with
CharacterRepository.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass
class Item:
    id: int = 0
    template_id: int = 0
    name: str = ""
    quantity: int = 1
    slot: int = 0


@dataclass
class Skill:
    id: int = 0
    name: str = ""
    level: int = 1
    experience: int = 0


@dataclass
class Equipment:
    weapon: Optional[Item] = None
    armor: Optional[Item] = None
    helmet: Optional[Item] = None
    gloves: Optional[Item] = None
    boots: Optional[Item] = None
    shoulders: Optional[Item] = None
    shield: Optional[Item] = None
    ring1: Optional[Item] = None
    ring2: Optional[Item] = None
    amulet: Optional[Item] = None


class Inventory:
    def __init__(self, max_slots: int):
        self.max_slots = max_slots
        self.items: List[Item] = []

    def add_item(self, item: Item) -> bool:
        if len(self.items) >= self.max_slots:
            return False
        self.items.append(item)
        return True

    def remove_item(self, item_id: int) -> bool:
        for i, it in enumerate(self.items):
            if it.id == item_id:
                del self.items[i]
                return True
        return False


_DEFAULT_SKILLS = [
    (1, "Basic Attack"), (2, "Heal"), (3, "Fireball"), (4, "Ice Bolt"),
    (5, "Lightning"), (6, "Shield"), (7, "Buff"), (8, "Debuff"), (9, "Ultimate"),
]


@dataclass
class Character:
    id: int = 0
    name: str = ""
    account_id: int = 0
    level: int = 1
    experience: int = 0
    gold: int = 0
    position: Vector3 = field(default_factory=Vector3)
    zone_id: int = 0
    world_id: int = 0
    current_hp: int = 0
    max_hp: int = 0
    current_mp: int = 0
    max_mp: int = 0
    equipment: Equipment = field(default_factory=Equipment)
    skills: List[Skill] = field(default_factory=list)
    base_inventory: Inventory = field(default_factory=lambda: Inventory(30))
    bank_inventory: Inventory = field(default_factory=lambda: Inventory(50))
    trade_inventory: Inventory = field(default_factory=lambda: Inventory(10))
    gender: int = 0
    hair_style: int = 0
    hair_color: int = 0
    face_style: int = 0
    skin_color: int = 0

    def __post_init__(self):
        if not self.skills:
            self.skills = [Skill(id=i, name=n, level=1) for i, n in _DEFAULT_SKILLS]
