"""RarityHelper + WriteInit serialization tests — Phase 6 item system."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.data.gc_object import GCObject, UInt32Property
from drserver.data.rarity_helper import (
    ItemRarity,
    get_tier_from_gc_type,
    get_rarity_from_tier,
    is_mythic_pal_item,
    get_item_level,
    get_required_level_from_gc_class,
    get_level_delta,
    get_quality_modifier_fixed32,
    get_random_scale_mod,
)
from drserver.util.byte_io import LEReader


def test_item_rarity_enum():
    assert int(ItemRarity.Normal) == 0
    assert int(ItemRarity.Superior) == 1
    assert int(ItemRarity.Magical) == 2
    assert int(ItemRarity.Rare) == 3
    assert int(ItemRarity.Unique) == 4
    assert int(ItemRarity.Mythic) == 5


def test_get_tier_from_gc_type():
    assert get_tier_from_gc_type("1HAxePAL-3") == 3
    assert get_tier_from_gc_type("MageBodyPAL-1") == 1
    assert get_tier_from_gc_type("1HSwordPAL") == 1
    assert get_tier_from_gc_type("") == 1
    assert get_tier_from_gc_type(None) == 1
    assert get_tier_from_gc_type("RangerHelmPAL-5") == 5
    # - at end with no digits
    assert get_tier_from_gc_type("foo-") == 1


def test_get_rarity_from_tier():
    assert get_rarity_from_tier(1) == ItemRarity.Normal
    assert get_rarity_from_tier(2) == ItemRarity.Superior
    assert get_rarity_from_tier(3) == ItemRarity.Magical
    assert get_rarity_from_tier(4) == ItemRarity.Rare
    assert get_rarity_from_tier(5) == ItemRarity.Unique
    assert get_rarity_from_tier(99) == ItemRarity.Rare  # default fallback


def test_is_mythic_pal_item():
    assert is_mythic_pal_item("FighterBodyMythicPAL") is True
    assert is_mythic_pal_item("1HSwordPAL-3") is False
    assert is_mythic_pal_item("") is False
    assert is_mythic_pal_item(None) is False


def test_get_item_level():
    assert get_item_level("1HAxe2PAL") == 11     # (2-1)*10 + 1
    assert get_item_level("MageBodyPAL-1") == 1  # pal_idx < 1 falls back
    assert get_item_level("RangerBoots5PAL") == 41  # (5-1)*10 + 1
    assert get_item_level("1HSwordPAL") == 1     # no tier = 1
    assert get_item_level(None) == 1


def test_get_required_level():
    # Normal item: itemLevel=11, delta=-12 → max(1, -1) = 1
    assert get_required_level_from_gc_class("1HAxe2PAL") >= 1
    # Unique item (tier 5): itemLevel=41, delta=-2 → 39
    req = get_required_level_from_gc_class("RangerBoots5PAL")
    assert req >= 1


def test_level_delta():
    assert get_level_delta(ItemRarity.Normal) == -12
    assert get_level_delta(ItemRarity.Superior) == -10
    assert get_level_delta(ItemRarity.Magical) == -7
    assert get_level_delta(ItemRarity.Rare) == -5
    assert get_level_delta(ItemRarity.Unique) == -2
    assert get_level_delta(ItemRarity.Mythic) == 3


def test_quality_modifier_fixed32():
    assert get_quality_modifier_fixed32(ItemRarity.Normal) == 67
    assert get_quality_modifier_fixed32(ItemRarity.Superior) == 144
    assert get_quality_modifier_fixed32(ItemRarity.Magical) == 274
    assert get_quality_modifier_fixed32(ItemRarity.Rare) == 520
    assert get_quality_modifier_fixed32(ItemRarity.Unique) == 1170
    assert get_quality_modifier_fixed32(ItemRarity.Mythic) == 9961


def test_get_random_scale_mod():
    for rarity in ItemRarity:
        mod = get_random_scale_mod(rarity)
        assert "ScaleModPAL" in mod
        assert "Mod" in mod


def test_write_init_equipment_layout():
    """Verify WriteInit produces the expected wire format structure."""
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="MeleeWeapon", gc_class="1HAxePAL-3", name="")
    item.stored_rarity = int(ItemRarity.Magical)
    item.stored_level = 21

    w = LEWriter()
    item.write_init(w, 20)

    data = w.to_array()
    r = LEReader(data)

    assert r.read_byte() == 0xFF                  # type tag
    gc_class = r.read_cstring()
    assert "1HaxePAL" in gc_class.upper() or "1haxepal" in gc_class
    assert r.read_uint32() == 10                   # equipment slot (weapon)
    assert r.read_byte() == 0x00                   # fill
    assert r.read_byte() == 0x00                   # fill
    assert r.read_byte() == 0x01                   # quantity
    level = r.read_byte()
    assert level > 0                               # item level
    # Should have 0x00 flag byte (colored item, rarity >= 1)
    # modCount * 0x00
    # scaleMod block
    # weapon extra bytes: uint16(1) uint8(2) uint16(0)


def test_write_init_normal_item():
    """Normal rarity items should not have a flag byte."""
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Armor", gc_class="FighterBodyPAL-1", name="")
    item.stored_rarity = int(ItemRarity.Normal)
    item.stored_level = 1

    w = LEWriter()
    item.write_init(w, 1)

    data = w.to_array()
    r = LEReader(data)

    assert r.read_byte() == 0xFF
    r.read_cstring()                               # gc class
    r.read_uint32()                                # slot
    r.read_byte()                                  # fill
    r.read_byte()                                  # fill
    r.read_byte()                                  # quantity
    r.read_byte()                                  # level
    # Normal item: should have modCount * 0x00, then 0x00 for scaleMod
    # No weapon trailing bytes (Armor)


def test_write_init_for_dropped_item_consumable():
    """Consumable items use the simplified 9-byte format."""
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Item", gc_class="items.consumables.consumable_majorhealthpotion", name="")
    w = LEWriter()
    item.write_init_for_dropped_item(w, 10)

    data = w.to_array()
    r = LEReader(data)

    assert r.read_byte() == 0xFF
    r.read_cstring()                               # gc class
    r.read_uint32()                                # 0
    r.read_byte()                                  # 0
    r.read_byte()                                  # 0
    r.read_byte()                                  # 0x01 (quantity)
    r.read_byte()                                  # level
    r.read_byte()                                  # flags
    r.read_byte()                                  # modCount = 0


def test_write_init_for_inventory():
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Armor", gc_class="FighterHelmPAL-2", name="")
    item.stored_rarity = int(ItemRarity.Superior)
    item.stored_level = 11

    w = LEWriter()
    item.write_init_for_inventory(w, 0, 0, 0x0B, 10)

    data = w.to_array()
    r = LEReader(data)

    assert r.read_byte() == 0xFF
    r.read_cstring()                               # gc class
    slot = r.read_uint32()
    # C# WriteInitForInventory (GCObject.cs:2857) writes the passed inventory
    # slot as the leading uint32 — NOT the equipment slot — and writes no
    # trailing slot field.
    assert slot == 0x0B                            # the inventory slot we passed
    assert r.read_byte() == 0                      # posX
    assert r.read_byte() == 0                      # posY
    assert r.read_byte() == 1                      # quantity
    assert r.read_byte() >= 1                      # level


def test_get_modifier_gc_class():
    item = GCObject(native_class="Armor", gc_class="FighterBodyPAL-4", name="")
    scale_mod = item.get_modifier_gc_class()
    assert "ScaleModPAL" in scale_mod

    # Preset overrides detection
    item2 = GCObject(native_class="Armor", gc_class="FighterBodyPAL", name="")
    item2.preset_scale_mod = "CustomScaleMod.MyMod"
    assert item2.get_modifier_gc_class() == "CustomScaleMod.MyMod"


def test_equipment_slot_from_gc_class_full():
    assert GCObject(gc_class="FighterHelmPAL").get_equipment_slot_from_gc_class() == 5
    assert GCObject(gc_class="FighterShouldersPAL").get_equipment_slot_from_gc_class() == 8
    assert GCObject(gc_class="FighterBodyPAL").get_equipment_slot_from_gc_class() == 6
    assert GCObject(gc_class="FighterGlovesPAL").get_equipment_slot_from_gc_class() == 2
    assert GCObject(gc_class="FighterBootsPAL").get_equipment_slot_from_gc_class() == 7
    assert GCObject(gc_class="ShieldPAL").get_equipment_slot_from_gc_class() == 11
    assert GCObject(gc_class="ring_unique").get_equipment_slot_from_gc_class() == 3
    assert GCObject(gc_class="amulet_rare").get_equipment_slot_from_gc_class() == 1
    assert GCObject(gc_class="1HSwordPAL").get_equipment_slot_from_gc_class() == 10


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
