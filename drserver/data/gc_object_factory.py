"""Entity GCObject construction.

Ported from C# Data/GCObjectFactory.cs. Builds the avatar entity tree (and its
component sub-objects) from a SavedCharacter for the character-select list and
in-world spawn. Equipment items are added to BOTH the Equipment and Manipulators
components (the original server pattern that makes gear render).

The C# create_equipment_item did a DatabaseLoader.FindItem lookup purely to log a
verification message — the GCObject was built either way — so that lookup is
omitted here (DatabaseLoader is a later port and does not affect the result).
"""
from __future__ import annotations

from .gc_object import GCObject, StringProperty, UInt32Property
from .saved_character import SavedCharacter

# Equipment slot -> Manipulator "ID" property value (renderer slot assignment).
_SLOT_ID = {
    "weapon": 10, "armor": 6, "helmet": 5, "gloves": 2, "boots": 7,
    "shoulders": 12, "shield": 11, "ring1": 3, "ring2": 4, "amulet": 1,
}


def _player_extra_data(name_unused: str = "") -> bytes:
    extra = bytearray()
    extra += b"plzwork1\x00"
    extra += b"plzwork2\x00"
    id_bytes = (0x05040302).to_bytes(4, "little")
    extra += id_bytes + id_bytes
    extra += b"\x00\xAA"
    extra += b"Normal\x00"
    extra += b"\x02\x00"
    extra += (0x05040302).to_bytes(4, "little")
    return bytes(extra)


def new_player(name: str) -> GCObject:
    player = GCObject(native_class="Player", gc_class="Player", name=name)
    player.add_property(StringProperty("Name", name))
    player.extra_data = _player_extra_data()
    return player


def _get_weapon_native_class(gc_class: str) -> str:
    lower = gc_class.lower()
    if "shield" in lower:
        return "Armor"
    if any(t in lower for t in ("gun", "bow", "crossbow", "rifle", "pistol", "blaster", "cannon", "launcher")):
        return "RangedWeapon"
    return "MeleeWeapon"


def _get_skill_native_class(skill_id: str) -> str:
    low = skill_id.lower()
    if "passive" in low or "trait" in low:
        return "PassiveSkill"
    return "ActiveSkill"


def get_avatar_gc_class(class_name: str) -> str:
    return {
        "Fighter": "avatar.classes.FighterFemale",
        "Mage": "avatar.classes.WarlockFemale",   # Mage maps to Warlock in game files
        "Ranger": "avatar.classes.RangerFemale",
    }.get(class_name, "avatar.classes.FighterFemale")


def _avatar_properties(c: SavedCharacter) -> list:
    return [
        UInt32Property("Skin", c.skin), UInt32Property("Face", c.face),
        UInt32Property("FaceFeature", c.face_feature), UInt32Property("Hair", c.hair),
        UInt32Property("HairColor", c.hair_color), UInt32Property("TotalWorldTime", 10),
        UInt32Property("LastKnownQueueLevel", 0), UInt32Property("HasBlingGnome", 1),
        UInt32Property("Level", c.level), UInt32Property("HitPoints", 1337),
        UInt32Property("ManaPoints", 1337), UInt32Property("Experience", c.experience),
        UInt32Property("AttributePoints", 100), UInt32Property("ReSpecTimer", 0),
        UInt32Property("StrengthPoints", 100), UInt32Property("AgilityPoints", 100),
        UInt32Property("ToughnessPoints", 100), UInt32Property("PowerPoints", 100),
        UInt32Property("MaxTotalAttributePool", 100), UInt32Property("PVPRating", 1337),
    ]


def load_avatar(character: SavedCharacter) -> GCObject:
    avatar_gc_class = character.avatar_class or get_avatar_gc_class(character.class_name)
    avatar = GCObject(native_class="Avatar", gc_class=avatar_gc_class, name="avatar")
    avatar.properties = _avatar_properties(character)

    avatar.add_child(new_modifiers())
    manipulators = new_manipulators()
    avatar.add_child(manipulators)
    avatar.add_child(new_unit_behavior())
    avatar.add_child(new_skills())
    equipment = new_equipment()
    avatar.add_child(equipment)
    avatar.add_child(new_unit_container_with_seven_children())
    avatar.add_child(new_avatar_metrics())
    avatar.add_child(new_dialog_manager())

    populate_equipment_from_character(equipment, manipulators, character)
    populate_skills_from_character(manipulators, character)
    return avatar


def populate_equipment_from_character(equipment: GCObject, manipulators: GCObject,
                                      character: SavedCharacter) -> int:
    eq = character.equipment
    if eq is None:
        return 0
    rarity_map = eq.slot_rarity or {}
    level_map = eq.slot_level or {}
    scale_map = eq.slot_scale_mod or {}
    mods_map = eq.slot_mod_refs or {}
    count = 0
    # Single-instance slots: same GCObject goes to both Equipment and Manipulators.
    for slot in ("weapon", "armor", "helmet", "gloves", "boots", "shoulders", "shield"):
        gc = getattr(eq, slot, None)
        if not gc:
            continue
        item = create_equipment_item(gc)
        if item is None:
            continue
        # Per-slot stored rarity/level — without these every colored item
        # spawned back as white at relog/warp (C# syncs slotRarity/slotLevel).
        item.stored_rarity = rarity_map.get(slot, -1)
        item.stored_level = level_map.get(slot, -1)
        item.preset_scale_mod = scale_map.get(slot) or None
        item.preset_mod_refs = list(mods_map.get(slot) or [])
        if slot == "shield" and item.native_class in ("MeleeWeapon", "RangedWeapon"):
            item.target_slot = 11  # dual-wield off-hand
        item.add_property(UInt32Property("ID", _SLOT_ID[slot]))
        equipment.add_child(item)
        manipulators.add_child(item)
        count += 1
    # Rings/amulet: separate instances for Equipment vs Manipulators.
    for slot in ("ring1", "ring2", "amulet"):
        gc = getattr(eq, slot, None)
        if not gc:
            continue
        for_equip = create_equipment_item(gc)
        for_manip = create_equipment_item(gc)
        if for_equip is None or for_manip is None:
            continue
        for inst in (for_equip, for_manip):
            inst.stored_rarity = rarity_map.get(slot, -1)
            inst.stored_level = level_map.get(slot, -1)
            inst.preset_scale_mod = scale_map.get(slot) or None
            inst.preset_mod_refs = list(mods_map.get(slot) or [])
        if slot == "ring2":
            for_equip.target_slot = 4
            for_manip.target_slot = 4
        for_equip.add_property(UInt32Property("ID", _SLOT_ID[slot]))
        for_manip.add_property(UInt32Property("ID", _SLOT_ID[slot]))
        equipment.add_child(for_equip)
        manipulators.add_child(for_manip)
        count += 1
    return count


def populate_skills_from_character(manipulators: GCObject, character: SavedCharacter) -> int:
    count = 0
    for skill_id in character.skills:
        # PassiveSkills are not ActiveSkill children: they ship from saved data
        # with the PASSIVE wire body (trailing u32 modifier id — PassiveSkill::
        # readInit @0x53D0E0; see data.class_passives + net.spawn OP4/OP9/OP10).
        # Serializing one with the ActiveSkill body desyncs the manipulator
        # reader ("Invalid type tag" crash, 2026-06-04).
        # Profession skills (skills.professions.*, e.g. Warrior/Warlock/Ranger) are
        # likewise NOT ActiveSkill manipulators: serializing one as an ActiveSkill
        # crashes the client the instant it parses the avatar (confirmed live
        # 2026-06-04 — adding the Fighter's professions.Warrior froze the client on
        # the character list). They stay in the DB / character_skills; we just don't
        # emit them as manipulators.
        if _get_skill_native_class(skill_id) == "PassiveSkill":
            continue
        if ".professions." in skill_id.lower():
            continue
        manipulators.add_child(GCObject(native_class="ActiveSkill", gc_class=skill_id, name=""))
        count += 1
    return count


def create_equipment_item(gc_class: str) -> GCObject:
    if not gc_class:
        return None
    lower = gc_class.lower()
    # Check ring/amulet first — "RingUnique" contains "gun"!
    if "ring" in lower or "amulet" in lower:
        native_class = "Item"
    elif any(t in lower for t in ("sword", "axe", "mace", "staff", "wand", "dagger",
                                  "hammer", "spear", "pick", "club", "katana",
                                  "polearm")):
        native_class = "MeleeWeapon"
    elif any(t in lower for t in ("bow", "gun", "crossbow", "cannon")):
        native_class = "RangedWeapon"
    else:
        native_class = "Armor"
    return GCObject(native_class=native_class, gc_class=gc_class, name="")


# ── component factories ──
def new_modifiers() -> GCObject:
    return GCObject(native_class="Modifiers", gc_class="Modifiers", name="")


def new_manipulators() -> GCObject:
    return GCObject(native_class="Manipulators", gc_class="Manipulators", name="")


def new_unit_behavior() -> GCObject:
    return GCObject(native_class="UnitBehavior", gc_class="avatar.base.UnitBehavior", name="")


def new_skills() -> GCObject:
    return GCObject(native_class="Skills", gc_class="avatar.base.skills", name="")


def new_equipment() -> GCObject:
    return GCObject(native_class="Equipment", gc_class="avatar.base.Equipment", name="")


def new_unit_container_with_seven_children() -> GCObject:
    uc = GCObject(native_class="UnitContainer", gc_class="UnitContainer", name="")
    child_gc = ["avatar.base.Inventory", "avatar.base.TradeInventory", "avatar.base.Bank",
                "avatar.base.Bank2", "avatar.base.Bank3", "avatar.base.Bank4", "avatar.base.Bank5"]
    for gc in child_gc:
        uc.add_child(GCObject(native_class="Inventory", gc_class=gc, name=""))
    # A Manipulator object serialized into ExtraData.
    from ..util.byte_io import LEWriter
    manipulator = GCObject(native_class="Manipulator", gc_class="Manipulator", name="")
    w = LEWriter()
    manipulator.write_full_gc_object(w)
    uc.extra_data = w.to_array()
    return uc


def new_avatar_metrics() -> GCObject:
    am = GCObject(native_class="AvatarMetrics", gc_class="AvatarMetrics", name="")
    # 5*u32 + 2*u32 + 5*u64 + 4*u32 = 84 bytes, all zero.
    am.extra_data = b"\x00" * 84
    return am


def new_dialog_manager() -> GCObject:
    return GCObject(native_class="DialogManager", gc_class="DialogManager", name="")


def new_proc_modifier() -> GCObject:
    return GCObject(native_class="ProcModifier", gc_class="ProcModifier", name="")


def load_minimal_avatar() -> GCObject:
    """Empty avatar (no equipment) used to trigger the creation UI."""
    avatar = GCObject(native_class="Avatar", gc_class="avatar.classes.FighterFemale", name="avatar")
    avatar.properties = [
        UInt32Property("Skin", 0), UInt32Property("Face", 0), UInt32Property("FaceFeature", 0),
        UInt32Property("Hair", 0), UInt32Property("HairColor", 0), UInt32Property("TotalWorldTime", 0),
        UInt32Property("LastKnownQueueLevel", 0), UInt32Property("HasBlingGnome", 0),
        UInt32Property("Level", 1), UInt32Property("HitPoints", 100), UInt32Property("ManaPoints", 100),
        UInt32Property("Experience", 0), UInt32Property("AttributePoints", 0), UInt32Property("ReSpecTimer", 0),
        UInt32Property("StrengthPoints", 0), UInt32Property("AgilityPoints", 0),
        UInt32Property("ToughnessPoints", 0), UInt32Property("PowerPoints", 0),
        UInt32Property("MaxTotalAttributePool", 0), UInt32Property("PVPRating", 0),
    ]
    avatar.add_child(new_modifiers())
    avatar.add_child(new_manipulators())
    avatar.add_child(new_unit_behavior())
    avatar.add_child(new_skills())
    avatar.add_child(new_equipment())
    avatar.add_child(new_unit_container_with_seven_children())
    avatar.add_child(new_avatar_metrics())
    avatar.add_child(new_dialog_manager())
    return avatar
