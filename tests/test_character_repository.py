"""Character persistence tests against a copy of the real shipped DB.

Copies the shipped dungeon_runners.db so the test never mutates the original,
then exercises: class-definition load, reading existing characters, and a
create -> save -> reload round-trip on the real schema.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from _paths import copy_shipped_db, has_shipped_db

from drserver.core import settings
from drserver.data import class_config
from drserver.db import account_repository as accounts
from drserver.db import character_repository as chars
from drserver.db import game_database


def _setup():
    game_database.initialize(copy_shipped_db())
    settings.load()
    class_config.load()


@pytest.fixture(autouse=True, scope="module")
def _db():
    """Initialize the DB layer against a throwaway copy of the shipped DB."""
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    _setup()


def test_class_definitions_load():
    fighter = class_config.get_class_definition("Fighter")
    assert fighter is not None
    assert fighter.starting_equipment.weapon  # has a starting weapon
    assert len(fighter.starting_skills) > 0


def test_read_existing_characters():
    ids = [r[0] for r in game_database.execute_reader("SELECT id FROM characters")]
    assert len(ids) > 0, "shipped DB should have characters"
    ch = chars.get_character(int(ids[0]))
    assert ch is not None
    assert ch.name
    # equipment + skills sub-tables should be populated objects (not None)
    assert ch.equipment is not None
    assert isinstance(ch.skills, list)
    print(f"  read char id={ch.id} name='{ch.name}' class={ch.class_name} "
          f"lv={ch.level} skills={len(ch.skills)} inv={len(ch.inventory)}")


def test_create_and_roundtrip():
    acct = accounts.create_account("PortTester", "pw")
    assert acct != 0
    created = chars.create_character("PortHero", "Fighter", acct, "PortTester")
    assert created is not None
    assert created.name == "PortHero"
    assert created.equipment.weapon  # starting weapon assigned
    assert len(created.skills) > 0   # starting skills + bling gnome
    assert any("blinggnome" in s.lower() for s in created.skills)

    # Modify + save + reload.
    created.level = 7
    created.gold = 555
    created.position.x = 123.5
    created.current_zone_name = "world.town"
    chars.save_character(created)

    reloaded = chars.get_character(created.id)
    assert reloaded.level == 7
    assert reloaded.gold == 555
    assert abs(reloaded.position.x - 123.5) < 0.01
    assert reloaded.current_zone_name == "world.town"


def test_item_mod_refs_persist_for_inventory_and_equipment():
    """A bought item's attribute mods (Intellect etc.) survive save/reload in both
    the bag and an equipment slot, and the spawn rebuild re-attaches them to the
    equipped GCObject (so they don't vanish on relog / zone-in)."""
    from drserver.data.saved_character import SavedInventoryItem
    from drserver.data import gc_object_factory
    from drserver.data.gc_object import GCObject

    mods = ["items.modpal.MageModPal.Rare.Mod1",
            "items.modpal.MageModPal.DamageBonus.Mod1"]
    acct = accounts.create_account("ModTester", "pw")
    ch = chars.create_character("ModHero", "Mage", acct, "ModTester")
    assert ch is not None
    ch.inventory = [SavedInventoryItem(gc_class="crystalarmor1pal.crystalarmor1-4",
                                       x=0, y=0, rarity=3, stored_level=14,
                                       scale_mod="ScaleModPAL.Rare.Mod1",
                                       mod_refs=list(mods))]
    ch.equipment.helmet = "crystalhelm1pal.crystalhelm1-4"
    ch.equipment.slot_rarity["helmet"] = 3
    ch.equipment.slot_mod_refs["helmet"] = list(mods)
    chars.save_character(ch)

    reloaded = chars.get_character(ch.id)
    assert reloaded.inventory[0].mod_refs == mods            # bag survives
    assert reloaded.equipment.slot_mod_refs.get("helmet") == mods  # equip survives

    # spawn rebuild re-attaches the affixes to the equipped item
    equipment = GCObject(native_class="Equipment", gc_class="avatar.base.Equipment", name="")
    manips = GCObject(native_class="Manipulators", gc_class="m", name="")
    gc_object_factory.populate_equipment_from_character(equipment, manips, reloaded)
    helm = next(c for c in equipment.children if "crystalhelm" in c.gc_class.lower())
    assert helm.preset_mod_refs == mods


def test_delete_character_cascades():
    """delete_character removes the character AND its child rows (no orphans).

    The client's delete (character channel type 0x04, payload
    [cstring name][uint32 id], confirmed live 2026-06-04) ultimately calls this.
    A bare DELETE FROM characters would leave equipment/skills/inventory rows
    orphaned under the reused id, corrupting the next character created with the
    same rowid — so the delete must cascade to every character_id child table.
    """
    acct = accounts.create_account("DelTester", "pw")
    created = chars.create_character("DelHero", "Fighter", acct, "DelTester")
    assert created is not None
    cid = created.id

    # Starting equipment + skills mean child rows exist for this id.
    def _child_counts() -> dict[str, int]:
        tables = (
            "character_equipment", "character_inventory", "character_skills",
            "character_quests", "quest_objectives", "completed_quests",
            "character_checkpoints", "character_modifiers",
        )
        return {
            t: game_database.execute_reader(
                f"SELECT COUNT(*) FROM {t} WHERE character_id = :id", {"id": cid}
            ).fetchone()[0]
            for t in tables
        }

    before = _child_counts()
    assert before["character_equipment"] > 0 or before["character_skills"] > 0, \
        "fixture should have produced child rows to test the cascade"

    assert chars.delete_character(cid) is True

    # Character row gone.
    rows = game_database.execute_reader(
        "SELECT id FROM characters WHERE id = :id", {"id": cid}
    ).fetchall()
    assert rows == []

    # Every child table cleared for this id.
    after = _child_counts()
    assert all(n == 0 for n in after.values()), f"orphaned child rows remain: {after}"


if __name__ == "__main__":
    import traceback

    _setup()
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
