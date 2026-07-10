"""GCObject tests: djb2 hashing, packet-class mapping, DFC byte layout."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.data.character import Character
from drserver.data.gc_object import (
    GCObject,
    StringProperty,
    UInt32Property,
    detect_rarity_from_gc_class,
    get_packet_gc_class_for,
    hash_djb2,
)
from drserver.util.byte_io import LEReader


def _ref_djb2(s: str) -> int:
    """Independent djb2 reference (cross-check, not a copy of the impl)."""
    h = 5381
    for ch in bytes(s.lower(), "utf-8"):
        h = ((h * 33) + ch) & 0xFFFFFFFF
    return h


def test_djb2():
    assert hash_djb2("") == 5381
    assert hash_djb2(None) == 5381
    for s in ("Avatar", "Player", "items.pal.mageshoulderspal.rare001", "SkillSlot"):
        assert hash_djb2(s) == _ref_djb2(s), s
    # Case-insensitive (lowercased before hashing).
    assert hash_djb2("AVATAR") == hash_djb2("avatar")


def test_packet_gc_class():
    # items.pal namespace gets prefixed.
    assert get_packet_gc_class_for("MageShouldersPAL.rare001") == "items.pal.mageshoulderspal.rare001"
    # Already-prefixed passes through (lowercased).
    assert get_packet_gc_class_for("items.pal.x.y") == "items.pal.x.y"
    # Bare namespace stays bare.
    assert get_packet_gc_class_for("1HMeleeWeaponPAL.sword") == "1hmeleeweaponpal.sword"
    # Potion remaps.
    assert get_packet_gc_class_for("potionpal.healthpotion_itempack") == "items.consumables.consumable_majorhealthpotion"


def test_rarity_detection():
    assert detect_rarity_from_gc_class("foo_mythicpal") == 5
    assert detect_rarity_from_gc_class("ring_unique") == 4
    assert detect_rarity_from_gc_class("sword_rare001") == 3
    assert detect_rarity_from_gc_class("plain_item") == 0


def test_equipment_slot():
    assert GCObject(gc_class="FighterHelmPAL.x").get_equipment_slot_from_gc_class() == 5
    assert GCObject(gc_class="1HSwordPAL.x").get_equipment_slot_from_gc_class() == 10
    assert GCObject(gc_class="amulet_x").get_equipment_slot_from_gc_class() == 1
    ring = GCObject(gc_class="ring_x")
    ring.target_slot = 4
    assert ring.get_equipment_slot_from_gc_class() == 4  # target_slot override


def test_write_full_gc_object_layout():
    obj = GCObject(id=0x1234, native_class="Avatar", gc_class="Player", name="Hero")
    obj.add_property(UInt32Property("SkillSlot", 7))
    obj.add_property(StringProperty("Name", "Hero"))
    child = GCObject(id=0x99, native_class="Equipment", gc_class="Equipment", name="eq")
    obj.add_child(child)

    from drserver.util.byte_io import LEWriter
    w = LEWriter()
    obj.write_full_gc_object(w)
    r = LEReader(w.to_array())

    assert r.read_byte() == 0x2D                       # DFC version
    assert r.read_uint32() == hash_djb2("Avatar")      # native class hash
    assert r.read_uint32() == 0x1234                   # id
    assert r.read_cstring() == "Hero"                  # name
    assert r.read_uint32() == 1                         # child count
    # child object (recursive)
    assert r.read_byte() == 0x2D
    assert r.read_uint32() == hash_djb2("Equipment")
    assert r.read_uint32() == 0x99
    assert r.read_cstring() == "eq"
    assert r.read_uint32() == 0                          # child has no children
    assert r.read_uint32() == hash_djb2("Equipment")    # child gc class hash
    assert r.read_uint32() == 0                          # child end marker
    # back to parent: gc class hash + properties
    assert r.read_uint32() == hash_djb2("Player")
    assert r.read_uint32() == hash_djb2("SkillSlot")
    assert r.read_uint32() == 7
    assert r.read_uint32() == hash_djb2("Name")
    assert r.read_cstring() == "Hero"
    assert r.read_uint32() == 0                          # parent end marker
    assert r.remaining == 0


def test_create_player():
    ch = Character(id=42, name="Styx3")
    player = GCObject.create_player(ch)
    assert player.native_class == "Player"
    assert player.id == 42
    assert player.name == "Styx3"
    assert b"plzwork1" in player.extra_data and b"Normal" in player.extra_data


def test_write_init_for_inventory_consumable_matches_csharp():
    # Arrange — a starting consumable. Even when the DB rarity is non-normal,
    # a consumable must never carry a ScaleMod block: the client reads one
    # mod-slot byte then a mod-count byte, so a ScaleMod tag (0x01/0xFF) would
    # be misread as "255 modifiers" and desync the inventory (the spawn-reject
    # join bug). C# emits exactly: FF cstring uint32(slot) px py 01 level 00 00.
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="MeleeWeapon",
                    gc_class="potionpal.healthpotion_noob", name="")
    item.stored_rarity = 2          # non-normal in DB, but it's a consumable
    item.stored_level = 1

    # Act
    w = LEWriter()
    item.write_init_for_inventory(w, pos_x=0, pos_y=0, inventory_slot=1, player_level=1)
    payload = bytes(w.to_array())

    # Assert — header slot is the leading uint32 (C# WriteInitForInventory),
    # tail is `level 00 00`, with no ScaleMod and no trailing slot uint32.
    cstr = b"potionpal.healthpotion_noob\x00"
    assert payload.startswith(b"\xFF" + cstr)
    body = payload[1 + len(cstr):]
    assert body == struct.pack("<I", 1) + bytes([0, 0, 0x01, 0x01, 0x00, 0x00])
    assert b"ScaleMod" not in payload


def test_write_init_for_inventory_writes_stack_count():
    # Arrange — a stack of 20 noob potions (the starting loadout). The stack
    # quantity was previously hardcoded to 0x01, so the client rendered every
    # stack as a single item (live bug 2026-06-07: chars showed 1 potion, not 20).
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="MeleeWeapon",
                    gc_class="potionpal.healthpotion_noob", name="")
    item.stored_level = 1

    # Act
    w = LEWriter()
    item.write_init_for_inventory(w, pos_x=1, pos_y=0, inventory_slot=1,
                                  player_level=1, count=20)
    payload = bytes(w.to_array())

    # Assert — the count byte (after the slot uint32 + px + py) is 20, not 1.
    cstr = b"potionpal.healthpotion_noob\x00"
    body = payload[1 + len(cstr):]
    assert body == struct.pack("<I", 1) + bytes([1, 0, 20, 0x01, 0x00, 0x00])


def test_write_init_for_inventory_normal_equipment_has_no_separate_flag_byte():
    # Arrange — a normal-rarity equippable item (no DB loaded → mod_count 0).
    # The client's inventory item reader (FUN_00581710) reads ONE flags byte
    # after `level`, but for normal gear that byte is the FIRST modCount 0x00 —
    # NOT a separately-written field. Writing a separate flag byte shifts the
    # stream by one and crashes the client ("Invalid ComponentID(0)" /
    # "Unknown message type"). C# WriteInitForInventory writes no separate flag
    # for normal items either.
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Armor",
                    gc_class="scalegloves1pal.scalegloves1-1", name="")
    item.stored_rarity = 0
    item.stored_level = 1

    # Act
    w = LEWriter()
    item.write_init_for_inventory(w, pos_x=0, pos_y=0, inventory_slot=5, player_level=1)
    payload = bytes(w.to_array())

    # Assert — body = slot(4) px py count level + modCount×00 + scaleMod. With no
    # DB the mod_count is 0, so the tail after level is a SINGLE `00` (the
    # ScaleMod/Phase-2 count), with no extra leading flag byte.
    cstr = b"scalegloves1pal.scalegloves1-1\x00"
    assert payload.startswith(b"\xFF" + cstr)
    body = payload[1 + len(cstr):]
    # slot=5, px=0, py=0, count=1, level=1, scalemod-count=0
    assert body == struct.pack("<I", 5) + bytes([0, 0, 0x01, 0x01, 0x00])


def test_write_init_for_inventory_emits_attribute_mods():
    # A colored item carrying preset_mod_refs serializes its attribute mods as
    # by-hash ItemModifier children alongside the by-name ScaleMod, all under one
    # child count — the same shape the merchant emits, so a bought item keeps its
    # Intellect in the bag / on relog.
    from drserver.util.byte_io import LEWriter, LEReader
    from drserver.data.gc_object import hash_djb2

    mods = ["items.modpal.MageModPal.Rare.Mod1",
            "items.modpal.MageModPal.DamageBonus.Mod1"]
    item = GCObject(native_class="Armor",
                    gc_class="crystalarmor1pal.crystalarmor1-4", name="")
    item.stored_rarity = 3                        # Rare -> colored
    item.preset_scale_mod = "ScaleModPAL.Rare.Mod1"
    item.preset_mod_refs = list(mods)

    w = LEWriter()
    item.write_init_for_inventory(w, pos_x=0, pos_y=0, inventory_slot=5, player_level=1)
    r = LEReader(w.to_array())

    assert r.read_byte() == 0xFF
    r.read_cstring()                              # gc class
    r.read_uint32()                               # inventory slot
    for _ in range(4):
        r.read_byte()                             # px, py, count, level
    # mod_count is 0 (no DB) -> straight to the ItemModifier child list
    count = r.read_byte()
    assert count == len(mods) + 1                 # attr mods + ScaleMod
    for ref in mods:
        assert r.read_byte() == 0x04              # by-hash
        assert r.read_uint32() == hash_djb2(ref)
        assert r.read_byte() == 0x03 and r.read_byte() == 0x15
        assert r.read_uint32() == 0x11111111
    assert r.read_byte() == 0xFF                  # ScaleMod by-name
    r.read_cstring()
    assert r.read_byte() == 0x03 and r.read_byte() == 0x15
    assert r.read_uint32() == 0x11111111
    assert r.remaining == 0


def test_write_init_for_inventory_no_trailing_slot():
    # Arrange — a normal-rarity equippable item in inventory.
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Armor",
                    gc_class="magebodypal.normal001", name="")
    item.stored_rarity = 0

    # Act
    w = LEWriter()
    item.write_init_for_inventory(w, pos_x=2, pos_y=3, inventory_slot=7, player_level=1)
    payload = bytes(w.to_array())

    # Assert — leading slot field carries the passed slot; C# writes no trailing
    # slot, so the payload must not end with a second copy of it.
    cstr = b"items.pal.magebodypal.normal001\x00"
    assert payload.startswith(b"\xFF" + cstr)
    assert payload[1 + len(cstr):1 + len(cstr) + 4] == struct.pack("<I", 7)
    assert payload[-4:] != struct.pack("<I", 7)


def test_scale_mod_is_deterministic_and_tier_suffix_aware():
    # Arrange — a Rare (tier -4) sword whose stored rarity was never set
    # (legacy DB row). The ScaleMod pick must (a) come from the tier-suffix
    # rarity pool, and (b) be the SAME on every serialization — this runs on
    # every relog/zone-load and a random pick re-rolled the item's stats.
    item = GCObject(native_class="MeleeWeapon",
                    gc_class="1hsword3pal.1hsword3-4", name="")

    # Act
    first = item.get_modifier_gc_class()
    second = item.get_modifier_gc_class()

    # Assert
    assert first == second
    assert first.startswith("ScaleModPAL.Rare.")


def test_suffix_colored_item_writes_scale_mod_block_without_stored_rarity():
    # Arrange — tier -2 (Superior) gloves, stored_rarity unset. The -N suffix
    # encodes the rarity for all PAL gear; the item must render colored, not
    # white, even when the per-instance rarity was lost.
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Armor",
                    gc_class="scalegloves1pal.scalegloves1-2", name="")

    # Act
    w = LEWriter()
    item.write_init_for_inventory(w, pos_x=0, pos_y=0, inventory_slot=5, player_level=1)
    payload = bytes(w.to_array())

    # Assert — the colored block is `01 FF <scaleMod cstring> 03 15 11111111`.
    assert b"ScaleModPAL.Superior." in payload
    tag = payload.index(b"\x01\xFFScaleModPAL")
    assert payload[tag] == 0x01


def test_op5_colored_item_writes_scale_mod_block():
    """OP5 (zone-in equipment) must carry the ScaleMod block for colored gear —
    the old hardcoded 0x00 tail stripped the rarity off every equipped item on
    warp/relog (items turned white until unequip+re-equip)."""
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="MeleeWeapon",
                    gc_class="1hsword3pal.1hsword3-4", name="")
    item.stored_rarity = 3
    item.stored_level = 21

    # Act
    w = LEWriter()
    item.write_init_for_equip_op5(w, player_level=50)
    payload = bytes(w.to_array())

    # Assert — `01 FF <scaleMod cstring> 03 15 11111111` tail present.
    assert b"ScaleModPAL.Rare." in payload
    tag = payload.index(b"\x01\xFFScaleModPAL")
    assert payload[tag] == 0x01
    assert payload.endswith(b"\x03\x15\x11\x11\x11\x11")


def test_op5_normal_item_keeps_single_zero_tail():
    # Arrange — plain white (tier-1, no stored rarity) gear: tail stays 0x00.
    from drserver.util.byte_io import LEWriter

    item = GCObject(native_class="Armor",
                    gc_class="fighterbodypal.armor1-1", name="")

    # Act
    w = LEWriter()
    item.write_init_for_equip_op5(w, player_level=1)
    payload = bytes(w.to_array())

    # Assert — no ScaleMod block, single 0x00 terminator.
    assert b"ScaleModPAL" not in payload
    assert payload.endswith(b"\x00")


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
