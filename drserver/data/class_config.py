"""Class definitions (starting gear/skills/inventory per class).

Ported from the class-definition half of C# ClassConfig. Loads class_definitions
+ class_starting_skills from SQLite, with a hardcoded Fighter/Mage/Ranger
fallback. (The in-memory character storage half of the C# ClassConfig is
superseded by CharacterRepository and is not ported.)
"""
from __future__ import annotations

from typing import Dict, Optional

from ..core import log
from ..db import game_database as db
from .saved_character import ClassDefinition, StartingEquipment, StartingInventoryItem

_classes: Dict[str, ClassDefinition] = {}
_loaded = False

# Every class starts with these consumables. Quantities + slots match the
# extracted client class definitions ({Fighter,Warlock,Ranger}StartingInventory.gc,
# verified 2026-06-04): 20 health + 20 mana + 1 town portal, identical per class.
_STARTING_INVENTORY = [
    StartingInventoryItem("potionpal.healthpotion_noob", 1, 0, quantity=20),
    StartingInventoryItem("potionpal.manapotion_noob", 2, 0, quantity=20),
    StartingInventoryItem("items.consumables.consumable_townportal", 0, 0, quantity=1),
]

# Each class's profession skill (extracted *StartingSkills.gc, verified
# 2026-06-04). These are NOT in the class_starting_skills table, so we inject
# them at load time regardless of the DB so every new character gets them.
_PROFESSION_SKILLS = {
    "Fighter": "skills.professions.Warrior",
    "Mage": "skills.professions.Warlock",
    "Ranger": "skills.professions.Ranger",
}


def _ensure_profession_skill(class_name: str, skills: list) -> None:
    """Prepend the class profession skill if missing (ground-truth ordering)."""
    prof = _PROFESSION_SKILLS.get(class_name)
    if prof and prof not in skills:
        skills.insert(0, prof)


def _hardcoded_defaults() -> Dict[str, ClassDefinition]:
    return {
        "Fighter": ClassDefinition(
            display_name="Fighter", description="Melee combat specialist",
            starting_equipment=StartingEquipment(
                weapon="items.pal.1hmacepal.normal001", armor="scalearmor1pal.scalearmor1-1",
                helmet="", gloves="scalegloves1pal.scalegloves1-1", boots="scaleboots1pal.scaleboots1-1",
                shoulders="", shield="", ring1="", ring2="", amulet=""),
            starting_skills=["skills.generic.Butcher", "skills.generic.Stomp",
                             "skills.generic.FighterClassPassive", "skills.generic.MeleeAttackSpeedModPassive"],
            starting_inventory=list(_STARTING_INVENTORY)),
        "Mage": ClassDefinition(
            display_name="Mage", description="Ranged magic specialist",
            starting_equipment=StartingEquipment(
                weapon="1hstaff1pal.1hstaff1-1", armor="items.pal.magebodypal.normal001",
                helmet="", gloves="items.pal.mageglovespal.normal002", boots="items.pal.magebootspal.normal002",
                shoulders="", shield="", ring1="", ring2="", amulet=""),
            starting_skills=["skills.generic.FireBolt", "skills.generic.ShadowLightning",
                             "skills.generic.MageClassPassive", "skills.generic.MagicDamageModPassive"],
            starting_inventory=list(_STARTING_INVENTORY)),
        "Ranger": ClassDefinition(
            display_name="Ranger", description="Ranged physical specialist",
            starting_equipment=StartingEquipment(
                weapon="2hcrossbow1pal.2hcrossbow1-1", armor="leatherarmor1pal.leatherarmor1-1",
                helmet="", gloves="leathergloves1pal.leathergloves1-1", boots="leatherboots1pal.leatherboots1-1",
                shoulders="", shield="", ring1="", ring2="", amulet=""),
            starting_skills=["skills.generic.PoisonShot", "skills.generic.PoisonBlastRadius",
                             "skills.generic.RangerClassPassive", "skills.generic.RangeAttackSpeedModPassive"],
            starting_inventory=list(_STARTING_INVENTORY)),
    }


def load() -> None:
    global _loaded
    if _loaded:
        return
    _classes.clear()
    try:
        for r in db.execute_reader("SELECT * FROM class_definitions"):
            cn = db.get_string(r, "class_name")
            _classes[cn] = ClassDefinition(
                display_name=db.get_string(r, "display_name"),
                description=db.get_string(r, "description"),
                starting_equipment=StartingEquipment(
                    weapon=db.get_string(r, "weapon"), armor=db.get_string(r, "armor"),
                    helmet=db.get_string(r, "helmet"), gloves=db.get_string(r, "gloves"),
                    boots=db.get_string(r, "boots"), shoulders=db.get_string(r, "shoulders"),
                    shield=db.get_string(r, "shield"), ring1=db.get_string(r, "ring1"),
                    ring2=db.get_string(r, "ring2"), amulet=db.get_string(r, "amulet")),
                starting_skills=[],
                starting_inventory=list(_STARTING_INVENTORY),
            )
        for r in db.execute_reader("SELECT class_name, skill_gc_type FROM class_starting_skills"):
            cn, skill = r[0], r[1]
            if cn in _classes:
                _classes[cn].starting_skills.append(skill)
        log.info(f"[ClassConfig] loaded {len(_classes)} classes from SQLite")
    except Exception as ex:  # noqa: BLE001
        log.error(f"[ClassConfig] SQLite error: {ex}; using hardcoded defaults")

    if not _classes:
        _classes.update(_hardcoded_defaults())
        log.info("[ClassConfig] loaded 3 hardcoded class definitions")

    # Inject the per-class profession skill (absent from class_starting_skills)
    # so every class matches its extracted *StartingSkills.gc.
    for cn, cd in _classes.items():
        _ensure_profession_skill(cn, cd.starting_skills)
    _loaded = True


def get_class_definition(class_name: str) -> Optional[ClassDefinition]:
    if not _loaded:
        load()
    cd = _classes.get(class_name)
    if cd is None:
        log.error(f"[ClassConfig] class '{class_name}' not found")
    return cd
