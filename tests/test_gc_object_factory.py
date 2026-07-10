"""GCObjectFactory tests: avatar tree structure + DFC serialization round-trip."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.data import gc_object_factory as factory
from drserver.data.gc_object import DFC_VERSION
from drserver.data.saved_character import SavedCharacter, StartingEquipment
from drserver.util.byte_io import LEReader, LEWriter

_AVATAR_COMPONENTS = ["Modifiers", "Manipulators", "UnitBehavior", "Skills",
                      "Equipment", "UnitContainer", "AvatarMetrics", "DialogManager"]


def _fighter() -> SavedCharacter:
    c = SavedCharacter(id=1, name="Hero", class_name="Fighter", level=5)
    c.equipment = StartingEquipment(
        weapon="items.pal.1hmacepal.normal001",
        armor="scalearmor1pal.scalearmor1-1",
        gloves="scalegloves1pal.scalegloves1-1",
        boots="scaleboots1pal.scaleboots1-1",
        ring1="ring_normal001", ring2="ring_normal002", amulet="amulet_normal001")
    c.skills = ["skills.generic.Butcher", "skills.generic.FighterClassPassive"]
    return c


def test_avatar_tree_structure():
    avatar = factory.load_avatar(_fighter())
    assert avatar.native_class == "Avatar"
    assert avatar.gc_class == "avatar.classes.FighterFemale"
    assert [c.native_class for c in avatar.children] == _AVATAR_COMPONENTS


def test_equipment_goes_to_both():
    c = _fighter()
    avatar = factory.load_avatar(c)
    components = {ch.native_class: ch for ch in avatar.children}
    equipment = components["Equipment"]
    manipulators = components["Manipulators"]
    # 7 single-instance slots present here: weapon, armor, gloves, boots, ring1, ring2, amulet.
    # Equipment holds all 7; Manipulators holds the 7 items + 1 active skill (passive skipped).
    assert len(equipment.children) == 7
    active_skills = [ch for ch in manipulators.children if ch.native_class == "ActiveSkill"]
    assert len(active_skills) == 1  # Butcher (passive skipped)
    assert len(manipulators.children) == 7 + 1


def test_weapon_native_class_detection():
    assert factory.create_equipment_item("items.pal.1hmacepal.normal001").native_class == "MeleeWeapon"
    assert factory.create_equipment_item("2hcrossbow1pal.x").native_class == "RangedWeapon"
    assert factory.create_equipment_item("ring_unique").native_class == "Item"  # 'ring' before 'gun'
    assert factory.create_equipment_item("scalearmor1pal.x").native_class == "Armor"


def test_avatar_dfc_serializes():
    avatar = factory.load_avatar(_fighter())
    w = LEWriter()
    avatar.write_full_gc_object(w)
    data = w.to_array()
    assert len(data) > 0
    r = LEReader(data)
    assert r.read_byte() == DFC_VERSION  # serialization produced a valid DFC stream


def test_minimal_avatar_has_no_equipment():
    avatar = factory.load_minimal_avatar()
    components = {ch.native_class: ch for ch in avatar.children}
    assert len(components["Equipment"].children) == 0
    assert [c.native_class for c in avatar.children] == _AVATAR_COMPONENTS


def test_avatar_metrics_extra_data():
    am = factory.new_avatar_metrics()
    assert am.extra_data == b"\x00" * 84


def test_profession_skill_excluded_from_manipulators():
    """Profession skills (skills.professions.*) must NOT be emitted as ActiveSkill
    manipulators — doing so crashes the client the instant it parses the avatar
    (live 2026-06-04: the Fighter's professions.Warrior froze the client on the
    character list). Active skills like Butcher still go in; passives stay out.
    """
    c = _fighter()
    c.skills = ["skills.professions.Warrior", "skills.generic.Butcher",
                "skills.generic.FighterClassPassive"]
    avatar = factory.load_avatar(c)
    manipulators = {ch.native_class: ch for ch in avatar.children}["Manipulators"]
    gc_classes = [ch.gc_class for ch in manipulators.children]
    assert not any(".professions." in g.lower() for g in gc_classes), gc_classes
    assert "skills.generic.Butcher" in gc_classes          # active skill still emitted
    assert "skills.generic.FighterClassPassive" not in gc_classes  # passive still excluded


if __name__ == "__main__":
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
