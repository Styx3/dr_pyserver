"""Tests for the native item-modifier resolver (IG → MG → ModPAL chain).

The hermetic tests use inline ``.gc`` fixtures so they run without the
extracter; an extracter-gated integration test asserts the real mage-gear →
Intellect resolution when the client content is present.
"""
from __future__ import annotations

import os
import textwrap

import pytest

from drserver.data import item_mod_resolver as R
from drserver.data import gc_parser
from drserver.data import extracter_paths


def _write(tmp_path, rel, text):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return str(path)


# ── MG resolution ────────────────────────────────────────────────────────────

def test_anonymous_single_mod_generators_are_indexed(tmp_path):
    """``* extends SingleItemModGenerator`` (anonymous) children must resolve."""
    mg = _write(tmp_path, "items/mg/WeaponBinderMG.gc", """
        WeaponBinderMG extends items.mg.BaseStaticMG.Base
        {
            * extends SingleItemModGenerator { Chance = 1; ItemModifier = items.modpal.WeaponBinderModPAL.Mod1; }
            * extends SingleItemModGenerator { Chance = 1; ItemModifier = items.modpal.WeaponBinderModPAL.Mod2; }
        }
    """)
    gens = R.load_mod_generators(os.path.dirname(mg))
    assert "weaponbindermg" in gens
    choices = R.resolve_gen("items.mg.WeaponBinderMG", gens)
    assert [c.mod_ref for c in choices] == [
        "items.modpal.WeaponBinderModPAL.Mod1",
        "items.modpal.WeaponBinderModPAL.Mod2",
    ]


def test_generator_links_are_followed(tmp_path):
    """``ItemModifierGeneratorLink`` targets are flattened transitively."""
    mg_dir = tmp_path / "items/mg"
    _write(tmp_path, "items/mg/AxeMG.gc", """
        AxeMG
        {
            Magic
            {
                * extends ItemModifierGeneratorLink { Chance = 1; LinkedGenerator = items.mg.WeaponMagicMG.Slash; }
            }
        }
    """)
    _write(tmp_path, "items/mg/WeaponMagicMG.gc", """
        WeaponMagicMG
        {
            Slash { * extends SingleItemModGenerator { Chance = 1; ItemModifier = items.modpal.WeaponMagicModPAL.SlashingDamageB_01; } }
        }
    """)
    gens = R.load_mod_generators(str(mg_dir))
    choices = R.resolve_gen("items.mg.AxeMG.Magic", gens)
    assert any("SlashingDamageB" in c.mod_ref for c in choices)


def test_pick_mod_respects_level_bands():
    choices = [
        R.ModChoice(1, 1, 10, "modA"),
        R.ModChoice(1, 11, 20, "modB"),
        R.ModChoice(1, 21, 30, "modC"),
    ]
    assert R.pick_mod(choices, 5) == "modA"
    assert R.pick_mod(choices, 15) == "modB"
    assert R.pick_mod(choices, 25) == "modC"
    # Above all bands → first eligible by MinLevel (highest band start).
    assert R.pick_mod(choices, 99) == "modC"


# ── direct + wrapper IG entries ──────────────────────────────────────────────

def test_direct_item_entry_resolves_mods(tmp_path):
    _write(tmp_path, "items/mg/MageMG.gc", """
        MageMG
        {
            SupPostMG { Mod1 extends SingleItemModGenerator { Chance = 1; MinLevel = 1; ItemModifier = items.modpal.MageModPal.Superior.Mod1; } }
        }
    """)
    _write(tmp_path, "items/ig/mage/RareMageBodyIG.gc", """
        RareMageBodyIG extends ItemGeneratorTable
        {
            Body001 extends ItemTimeline.XXXLightGenerator
            {
                Chance = 1;
                Item = items.pal.MageBodyPAL.Rare001;
                ItemModGenerator1 = items.mg.MageMG.SupPostMG;
            }
        }
    """)
    resolved = R.build_resolved_items(str(tmp_path))
    by_key = {(r.item_ref.lower(), r.rarity): r for r in resolved}
    key = ("items.pal.magebodypal.rare001", "Rare")
    assert key in by_key
    assert "items.modpal.MageModPal.Superior.Mod1" in by_key[key].mod_refs


def test_wrapper_entry_applies_mods_to_base_items(tmp_path):
    _write(tmp_path, "items/mg/AxeMG.gc", """
        AxeMG { Magic { * extends SingleItemModGenerator { Chance = 1; ItemModifier = items.modpal.WeaponMagicModPAL.SlashingDamageB_01; } } }
    """)
    _write(tmp_path, "items/ig/1haxe/NormalIG.gc", """
        NormalIG
        {
            Standard extends ItemGeneratorTable
            {
                1HAxe1 extends ItemTimeLine.1HAxePhase1 { Chance = 1; Item = items.pal.1HAxePAL.Normal001; }
            }
        }
    """)
    _write(tmp_path, "items/ig/1haxe/MagicIG.gc", """
        MagicIG
        {
            Standard extends ItemGeneratorTable
            {
                * extends RandomItemGenerator
                {
                    Chance = 1;
                    ItemGenerator = items.ig.1hAxe.NormalIG.Standard;
                    ItemModGenerator1 = items.mg.AxeMG.Magic;
                }
            }
        }
    """)
    resolved = R.build_resolved_items(str(tmp_path))
    by_key = {(r.item_ref.lower(), r.rarity): r for r in resolved}
    key = ("items.pal.1haxepal.normal001", "Magical")
    assert key in by_key
    assert any("SlashingDamageB" in m for m in by_key[key].mod_refs)


# ── integration against the real extracter (skipped when absent) ─────────────

_EXTRACTER = extracter_paths.resolve_extracter_dir() or ""


@pytest.mark.skipif(not os.path.isdir(os.path.join(_EXTRACTER, "items", "ig")),
                    reason="extracter client content not present")
def test_real_mage_gear_carries_intellect_mod():
    resolved = R.build_resolved_items(_EXTRACTER)
    mage_rare = [r for r in resolved
                 if "magebodypal.rare" in r.item_ref.lower() and r.rarity == "Rare"]
    assert mage_rare, "expected resolved Rare mage body items"
    # MageModPal.Superior.Mod1 == EnhancementsPAL.IntellectB (the Intellect mod).
    assert all(any("MageModPal.Superior" in m for m in r.mod_refs)
               for r in mage_rare)
