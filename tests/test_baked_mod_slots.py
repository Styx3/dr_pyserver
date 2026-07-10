"""Baked-mod ("special") item serialization — Mythic / Prebuilt / … items.

These items carry their modifiers inside the client's OWN GC definition (the
``.Mod1..N`` children). Their inventory / OP5-equipment wire form must be
``level + flag + N empty mod-slots + one empty-list 0x00`` (N+2 zero bytes) and
NEVER a by-name ``ScaleModPAL...`` block — a ScaleMod desyncs the client's GC
reader (``GCClassRegistry::readType Invalid type tag``) into an Avatar
access-violation. Live-caught 2026-07-10 on an equipped ``2HStaffMythicPAL``
(x64dbg: fault at 0x6F993E, buffer cursor on a mid-string ``0x63``). Port of the
C# ``GetOP5ModCount`` branch; the count is the item's authored ItemModifier
child count (weapons +1 for the un-flattened base SpeedM).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "tests")
from _paths import copy_shipped_db, has_shipped_db  # noqa: E402
from drserver.util.byte_io import LEWriter  # noqa: E402

# The live-crashing items from the Styx3 character (2026-07-10).
STAFF = "2HStaffMythicPAL.2HStaffMythic1"          # equipped weapon (OP5)
BODY = "items.pal.MageBodyPAL.PrebuiltMythic002"   # Mage Body Armor quest reward (bag)


@pytest.fixture(scope="module")
def gc_object():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    from drserver.data import gc_object as mod
    mod.clear_baked_slot_cache()
    return mod


# ── slot-count derivation (port of C# GetOP5ModCount) ───────────────────────────

def test_staff_slot_count_adds_base_speedm(gc_object):
    # 5 authored Mods (Mod1..Mod5) + the inherited base SpeedM the weapons table
    # does not flatten = 6 (client reads flag + 6 slots + empty = 8 zeros).
    assert gc_object.authored_baked_mod_slots(STAFF) == 6


def test_prebuilt_armor_slot_count_uses_flattened_mods(gc_object):
    # 4 authored Mods + the flattened base SpeedM = 5.
    assert gc_object.authored_baked_mod_slots(BODY) == 5


def test_normal_and_non_special_items_are_not_baked(gc_object):
    # "normal" / "unique" are not baked-mod special tiers → normal ScaleMod path.
    assert gc_object.authored_baked_mod_slots("items.pal.magebodypal.normal001") is None
    assert gc_object.authored_baked_mod_slots("AmuletPAL.AmuletUnique6") is None
    assert gc_object.authored_baked_mod_slots("crystalarmor1pal.crystalarmor1-2") is None
    assert gc_object.authored_baked_mod_slots("") is None


# ── OP5 equipment wire (the zone-load crash path) ───────────────────────────────

def test_op5_equip_emits_empty_baked_tail_no_scalemod(gc_object):
    o = gc_object.GCObject(native_class="MeleeWeapon", gc_class=STAFF)
    o.stored_level = 1
    w = LEWriter()
    o.write_init_for_equip_op5(w, 1)
    b = w.to_array()

    assert b"scalemod" not in b.lower(), "baked item must not carry a ScaleMod"
    slot = o.get_equipment_slot_from_gc_class()
    lvl = o._get_effective_level(1)
    # 0x00(str null) slot(u32) 0x00 0x00 0x01 level  then baked(6)+2 = 8 zero bytes
    expected_tail = (b"\x00" + slot.to_bytes(4, "little")
                     + b"\x00\x00\x01" + bytes([lvl]) + b"\x00" * 8)
    assert b.endswith(expected_tail)


# ── inventory wire (the quest turn-in / bag crash path) ─────────────────────────

def test_inventory_emits_empty_baked_tail_no_scalemod(gc_object):
    o = gc_object.GCObject(native_class="Armor", gc_class=BODY)
    o.stored_level = 1
    w = LEWriter()
    o.write_init_for_inventory(w, 0, 0, 5, 1, count=1)
    b = w.to_array()

    assert b"scalemod" not in b.lower()
    lvl = o._get_effective_level(1)
    # 0x00(null) slot(u32=5) posX posY count level  then baked(5)+2 = 7 zero bytes
    expected_tail = (b"\x00" + (5).to_bytes(4, "little")
                     + b"\x00\x00\x01" + bytes([lvl]) + b"\x00" * 7)
    assert b.endswith(expected_tail)


# ── regression: normal colored gear keeps its ScaleMod block ────────────────────

def test_colored_normal_item_still_emits_scalemod(gc_object):
    o = gc_object.GCObject(native_class="Armor", gc_class="crystalarmor1pal.crystalarmor1-2")
    o.stored_rarity = 1
    o.stored_level = 1
    w = LEWriter()
    o.write_init_for_inventory(w, 0, 0, 6, 1, count=1)
    assert b"scalemod" in w.to_array().lower(), "non-baked colored item must keep ScaleMod"


# ── OP5 jewelry (amulet/ring) special format — the second live crash ────────────

AMULET = "AmuletPAL.AmuletUnique6"   # equipped on Styx3; dropped by the OP5 loop


def test_jewelry_slot_counts(gc_object):
    f = gc_object.jewelry_op5_mod_slots
    assert f(AMULET) == 1                                   # non-mythic amulet → 1 slot
    assert f("uniqueamuletpal.something") == 2              # unique-PAL amulet → 2
    assert f("crystalarmor1pal.crystalarmor1-2") is None    # armor → not jewelry
    assert f("2HStaffMythicPAL.2HStaffMythic1") is None     # weapon → not jewelry
    assert f("MythicRingPAL.something") is None             # mythic jewelry → baked path


def test_op5_amulet_emits_no_scalemod(gc_object):
    o = gc_object.GCObject(native_class="Item", gc_class=AMULET)
    o.stored_rarity = 4
    o.stored_level = 1
    w = LEWriter()
    o.write_init_for_equip_op5(w, 1)
    b = w.to_array()
    assert b"scalemod" not in b.lower(), "non-mythic amulet must not carry a ScaleMod (C# jewelry format)"
    slot = o.get_equipment_slot_from_gc_class()
    lvl = o._get_effective_level(1)
    # 0x00(null) slot(u32) 0x00 0x00 0x01 level  then 1 mod-slot + 1 mods-count = 2 zeros
    expected_tail = (b"\x00" + slot.to_bytes(4, "little")
                     + b"\x00\x00\x01" + bytes([lvl]) + b"\x00\x00")
    assert b.endswith(expected_tail)
