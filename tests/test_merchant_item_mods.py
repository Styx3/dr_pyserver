"""Merchant ItemModifier emission — wire shape + generation.

Asserts the ItemModifier child list matches the client-decoded format
(``ItemAttributeModifier::readData`` @0x00588AE0, Ghidra 2026-06-20): every child
is ``readType`` (attribute mods by-hash ``0x04 <u32 djb2>``, ScaleMod by-name
``0xFF cstring``) followed by the SAME 6-byte body ``0x03 0x15 u32`` (flags=0x03
=> u8 + u32). All N attribute mods + the ScaleMod sit under one child count.
"""
from __future__ import annotations

import random

import pytest

from _paths import copy_shipped_db, has_shipped_db
from drserver.managers.merchants import (
    MerchantManager, MerchantItem, MerchantDef, MerchantTab,
    _IG_RARITIES, _IG_KIND, _MAX_GENERATED_PER_TAB,
)
from drserver.data.rarity_helper import ItemRarity
from drserver.data.gc_object import hash_djb2
from drserver.util.byte_io import LEWriter, LEReader


@pytest.fixture(autouse=True, scope="module")
def _db():
    if not has_shipped_db():
        yield
        return
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    yield


def _read_cstring(r: LEReader) -> str:
    out = bytearray()
    while True:
        b = r.read_byte()
        if b == 0:
            break
        out.append(b)
    return out.decode("latin-1")


def test_write_item_mod_children_matches_verified_format():
    mgr = MerchantManager()
    mods = ["items.modpal.MageModPal.Superior.Mod1",   # Intellect
            "items.modpal.MageModPal.Magic.Mod1"]       # elemental
    item = MerchantItem(gc_type="items.pal.magebodypal.rare001", item_id=600,
                        rarity=ItemRarity.Rare, mod_refs=list(mods))
    w = LEWriter()
    mgr._write_item_mod_children(w, item, "ScaleModPAL.Rare.Mod1")

    r = LEReader(w.to_array())
    count = r.read_byte()
    assert count == len(mods) + 1                      # attr mods + 1 ScaleMod
    for mod_ref in mods:                                # by-hash children first
        assert r.read_byte() == 0x04
        assert r.read_uint32() == hash_djb2(mod_ref)
        # 6-byte ItemAttributeModifier::readData body (flags 0x03 => u8 + u32);
        # identical to the proven ScaleMod body. The effect comes from the GC def.
        assert r.read_byte() == 0x03
        assert r.read_byte() == 0x15
        assert r.read_uint32() == 0x11111111
    assert r.read_byte() == 0xFF                        # by-name ScaleMod child
    assert _read_cstring(r) == "ScaleModPAL.Rare.Mod1"
    assert r.read_byte() == 0x03                        # same 6-byte body
    assert r.read_byte() == 0x15
    assert r.read_uint32() == 0x11111111
    assert r.remaining == 0


def test_roll_item_mods_class_mapping_and_count():
    """Mage (crystal) armor rolls a varied MageModPal stack; weapons get none.
    The stack is rarity-scaled and every ref is a MageModPal class."""
    import random
    if not has_shipped_db():
        pytest.skip("shipped DB missing")
    from drserver.managers.merchants import merchant_manager as mm
    mm.reset()
    mm.load()
    rng = random.Random(1)
    mods = mm._roll_item_mods("crystalarmor1pal.crystalarmor1-4",
                              ItemRarity.Rare, rng)
    assert 1 <= len(mods) <= 3                          # rarity-scaled stack
    assert all("magemodpal" in m.lower() for m in mods)
    assert len(mods) == len(set(mods))                  # distinct mods
    # weapons carry no separate attribute modpal (old-gen ScaleMod only)
    assert mm._roll_item_mods("1haxe1pal.1haxe1-4", ItemRarity.Rare, rng) == []
    # Normal gear gets no stat mod
    assert mm._roll_item_mods("crystalarmor1pal.crystalarmor1-4",
                              ItemRarity.Normal, rng) == []
    mm.reset()


def test_modpal_pool_is_rich_and_varied():
    """The GCDictionary pool gives many distinct mods per (family, rarity) — the
    fix for 'every item has the same mod'. Skips gracefully if the dictionary is
    not reachable in this environment (falls back to the baked table)."""
    from drserver.data import modpal_pool
    modpal_pool.reset()
    quality, thematic = modpal_pool.stat_mods("magemodpal", "Rare")
    if not quality and not thematic:
        pytest.skip("GCDictionary not reachable in this environment")
    assert len(quality) >= 5                            # many primary attributes
    assert len(thematic) >= 5                            # plus thematic bonuses
    modpal_pool.reset()


def test_generated_stock_frames_cleanly_with_mods():
    """With attribute mods live, a vendor's whole generated stock still frames
    byte-exactly — every Item plus its modifier children parse with nothing left
    over. This is the safety property an entity-stream desync would violate."""
    if not has_shipped_db():
        pytest.skip("shipped DB missing")
    import drserver.managers.merchants as merch_mod
    assert merch_mod._NATIVE_MODS_ENABLED               # mods are live
    mm = merch_mod.merchant_manager
    mm.reset()
    mm.load()
    mm.ensure_inventory_for_level("world.town.npc.VendorWeapon1", 30)
    pkt = mm.build_stock_add_packet("world.town.npc.VendorWeapon1", 0x5678)
    assert pkt and pkt[0] == 0x07 and pkt[-1] == 0x06

    saw_attr_mod = False
    r = LEReader(pkt)
    assert r.read_byte() == 0x07
    while True:
        op = r.read_byte()
        if op == 0x06:                                  # end-of-packet
            break
        assert op == 0x35                               # add-update block
        r.read_uint16()                                 # merchant cid
        assert r.read_byte() == 0x1E                    # sub: ItemAdd
        r.read_byte()                                   # inv id
        # ── Item (mirror _write_item exactly) ──
        assert r.read_byte() == 0xFF                    # gc type tag
        gc = _read_cstring(r)
        r.read_uint32()                                 # id
        r.read_byte(); r.read_byte(); r.read_byte(); r.read_byte()  # x,y,qty,level
        for _ in range(mm._mod_count_for(gc)):          # GC mod-slot 0x00 prefix
            assert r.read_byte() == 0x00
        count = r.read_byte()                           # modifier child count
        for _ in range(count):
            tag = r.read_byte()
            if tag == 0x04:                             # attribute mod (by-hash)
                r.read_uint32()
                saw_attr_mod = True
            elif tag == 0xFF:                           # ScaleMod (by-name)
                _read_cstring(r)
            else:
                raise AssertionError(f"bad modifier tag {tag:#x} on {gc}")
            assert r.read_byte() == 0x03                # the fixed 6-byte body
            assert r.read_byte() == 0x15
            assert r.read_uint32() == 0x11111111
        assert r.read_byte() == 0x02                    # add-update trailer
        assert r.read_uint32() == 0x00000000
    assert r.remaining == 0                             # framed byte-exactly
    assert saw_attr_mod, "no attribute mod emitted across the whole stock"
    mm.reset()
    """The level-banded LevelPrefix mod (resolved at a representative level) is
    not emitted — only level-stable attribute mods are."""
    item = MerchantItem(
        gc_type="items.pal.1haxepal.normal001", item_id=1, rarity=ItemRarity.Rare,
        mod_refs=["items.modpal.LevelPrefixModPAL.Weapon01.Mod2",
                  "items.modpal.WeaponMagicModPAL.SlashingDamageB_01"])
    refs = MerchantManager._attr_mod_refs(item)
    assert refs == ["items.modpal.WeaponMagicModPAL.SlashingDamageB_01"]


def test_ig_rarity_and_kind_mapping():
    # Town weapon/armor vendors stock the lower green/blue tiers of their kind.
    assert _IG_RARITIES["merchantweaponig"] == frozenset({"Superior", "Magical"})
    assert _IG_KIND["merchantweaponig"] == ("weapon",)
    assert _IG_KIND["merchantarmorig"] == ("armor",)
    # Special-event vendors carry the higher tiers.
    assert _IG_RARITIES["merchantspecialevent01ig"] == frozenset({"Rare", "Unique"})
    # Scrap heap / random carry both kinds.
    assert set(_IG_KIND["merchanttrashig"]) == {"weapon", "armor"}


def test_native_generation_is_bounded_and_carries_mods(monkeypatch):
    # The native pool + mod emission are both gated off by default (pool reverted
    # to the original dash family; mods pending the wire-body trace). Enable both
    # to exercise the native pool path.
    import drserver.managers.merchants as merch_mod
    monkeypatch.setattr(merch_mod, "_NATIVE_POOL_ENABLED", True)
    monkeypatch.setattr(merch_mod, "_NATIVE_MODS_ENABLED", True)
    mgr = MerchantManager()
    # Inject the selection pool (gc, gold, kind) + baked mods directly (bypass
    # the DB load). _wire_mods is keyed by the normalized key (items.pal.
    # stripped) + the IG-assigned rarity (Superior for MerchantArmorIG).
    mgr._wire_mods_loaded = True
    mgr._wire_mods = {
        (f"magebodypal.normal{n:03d}", "Superior"):
            ["items.modpal.MageModPal.Superior.Mod1"]
        for n in range(1, 60)
    }
    mgr._native_pool = [
        (f"items.pal.magebodypal.normal{n:03d}", "Superior", "armor", 4.0)
        for n in range(1, 60)
    ]

    md = MerchantDef(merchant_id=1, npc_gc_type="x", merchant_gc_type="x.M",
                     name="X")
    tab = MerchantTab(inv_id=1, name="Armor", label="Armor",
                      gc_type="x.M.Armor", item_generator="MerchantArmorIG",
                      min_item_level=6, max_item_level=20, auto_generate=True)
    mgr.generate_tab(md, tab, player_level=20)

    assert tab.items, "native armor tab generated empty"
    assert len(tab.items) <= _MAX_GENERATED_PER_TAB    # bounded, not grid-packed
    assert all("magebodypal" in it.gc_type for it in tab.items)  # mage gear present
    assert all(it.mod_refs for it in tab.items)        # real mods attached
    assert any("MageModPal.Superior" in m
               for it in tab.items for m in it.mod_refs)  # Intellect
