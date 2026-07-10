"""Starting-loadout correctness for newly-created characters (all 3 classes).

Ground truth is the EXTRACTED CLIENT CONTENT, not the C# emulator:
extracter/avatar/classes/{Fighter,Warlock,Ranger}Starting{Equipment,Skills,Inventory}.gc
and {...}Base.gc. Validated 2026-06-04. Each class must start with:

  * Stats: Strength/Agility/Toughness/Power = 10 each
    (DB columns stat_strength/agility/intellect/endurance; Toughness->endurance,
     Power->intellect).
  * Inventory: HealthPotion_Noob x20, ManaPotion_Noob x20, Consumable_TownPortal x1
    (identical across all classes) + the server's BlingGnome skillbook (kept by design).
  * Skills: the class PROFESSION skill (Warrior/Warlock/Ranger) + 4 class generics
    + the server's SummonBlingGnome (kept by design).
  * Equipment: class weapon + scale/cloth/leather armor set.

This guards against the pre-fix bug where create only granted 1 health potion,
no mana potion / town portal, no profession skill, and zero stats.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from _paths import copy_shipped_db, has_shipped_db

from drserver.data import class_config
from drserver.db import account_repository as accounts
from drserver.db import character_repository as chars
from drserver.db import game_database

# class_name -> (profession skill, one class-generic skill, weapon substring)
_EXPECTED = {
    "Fighter": ("skills.professions.Warrior", "skills.generic.Butcher", "1hmace"),
    "Mage": ("skills.professions.Warlock", "skills.generic.FireBolt", "1hstaff"),
    "Ranger": ("skills.professions.Ranger", "skills.generic.PoisonShot", "2hcrossbow"),
}


@pytest.fixture(autouse=True, scope="module")
def _db():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    game_database.initialize(copy_shipped_db())
    from drserver.core import settings
    settings.load()
    class_config.load()


def _inventory(cid: int) -> dict[str, int]:
    rows = game_database.execute_reader(
        "SELECT gc_class, count FROM character_inventory WHERE character_id = :id",
        {"id": cid},
    ).fetchall()
    return {r[0].lower(): r[1] for r in rows}


def _skills(cid: int) -> set[str]:
    rows = game_database.execute_reader(
        "SELECT skill_gc_class FROM character_skills WHERE character_id = :id",
        {"id": cid},
    ).fetchall()
    return {r[0] for r in rows}


def _stats(cid: int):
    r = game_database.execute_reader(
        "SELECT stat_strength, stat_agility, stat_intellect, stat_endurance "
        "FROM characters WHERE id = :id", {"id": cid}
    ).fetchone()
    return tuple(r)


@pytest.mark.parametrize("class_name", list(_EXPECTED))
def test_starting_loadout_matches_client_ground_truth(class_name):
    profession, generic, weapon_sub = _EXPECTED[class_name]
    acct = accounts.create_account(f"Load_{class_name}", "pw")
    created = chars.create_character(f"Hero{class_name}", class_name, acct, f"Load_{class_name}")
    assert created is not None
    cid = created.id

    # ── Inventory: 20 health + 20 mana + 1 town portal (+ kept bling gnome book).
    inv = _inventory(cid)
    health = next((c for g, c in inv.items() if "healthpotion" in g), None)
    mana = next((c for g, c in inv.items() if "manapotion" in g), None)
    townportal = any("townportal" in g for g in inv)
    assert health == 20, f"health potion qty {health}, expected 20 ({class_name})"
    assert mana == 20, f"mana potion qty {mana}, expected 20 ({class_name})"
    assert townportal, f"town portal scroll missing ({class_name})"
    assert any("blinggnome" in g for g in inv), "bling gnome skillbook should be kept"

    # ── Skills: profession + a class generic + kept bling gnome.
    sk = _skills(cid)
    assert profession in sk, f"missing profession skill {profession} ({class_name})"
    assert generic in sk, f"missing class generic {generic} ({class_name})"
    assert any("blinggnome" in s.lower() for s in sk), "bling gnome skill should be kept"

    # ── Stats: ALLOCATED points only = 0/0/0/0. The client supplies each class's
    # base (10/10/10/10 from *Base.gc) and adds the wire value on top, so storing
    # 10 here would display as 20 in-game.
    assert _stats(cid) == (0, 0, 0, 0), f"stats {_stats(cid)} != all-0 ({class_name})"

    # ── Equipment: class weapon present.
    assert created.equipment.weapon and weapon_sub in created.equipment.weapon.lower()


def _resolves(gc_type: str, tables: tuple[str, ...]) -> bool:
    """Exact-match the gc_type against the given content tables (post-migration keys)."""
    for table in tables:
        row = game_database.execute_reader(
            f"SELECT 1 FROM {table} WHERE gc_type = :k LIMIT 1", {"k": gc_type}
        ).fetchone()
        if row:
            return True
    return False


@pytest.mark.parametrize("class_name", list(_EXPECTED))
def test_starting_keys_resolve_in_migrated_db(class_name):
    """Every loadout key the config emits must exist verbatim in the re-keyed
    content tables. Guards the DB migration: a lowercased/re-namespaced key in
    the DB must not leave a dangling reference in class_config (caught the
    Consumable_TownPortal mixed-case dangling key, 2026-06-07)."""
    cd = class_config.get_class_definition(class_name)
    assert cd is not None

    for item in cd.starting_inventory:
        assert _resolves(item.gc_class, ("items",)), \
            f"starting inventory key {item.gc_class!r} missing from items table"

    eq = cd.starting_equipment
    for slot in ("weapon", "armor", "helmet", "gloves", "boots",
                 "shoulders", "shield", "ring1", "ring2", "amulet"):
        key = getattr(eq, slot)
        if not key:
            continue
        assert _resolves(key, ("weapons", "armor", "items")), \
            f"{class_name} {slot} key {key!r} missing from content tables"


if __name__ == "__main__":
    import traceback

    if not has_shipped_db():
        print("SKIP: shipped DB not present")
        sys.exit(0)
    game_database.initialize(copy_shipped_db())
    from drserver.core import settings
    settings.load()
    class_config.load()
    failed = 0
    for cn in _EXPECTED:
        try:
            test_starting_loadout_matches_client_ground_truth(cn)
            print(f"PASS {cn}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {cn}")
            traceback.print_exc()
    print(f"\n{len(_EXPECTED) - failed}/{len(_EXPECTED)} passed")
    sys.exit(1 if failed else 0)


def _hotbar(cid: int) -> dict[str, int]:
    rows = game_database.execute_reader(
        "SELECT skill_gc_class, hotbar_slot FROM character_skills"
        " WHERE character_id = :id", {"id": cid},
    ).fetchall()
    return {r[0].lower(): r[1] for r in rows}


@pytest.mark.parametrize("class_name,active100,active105,passive108,passive109", [
    ("Fighter", "skills.generic.stomp", "skills.generic.butcher",
     "skills.generic.fighterclasspassive", "skills.generic.meleeattackspeedmodpassive"),
    ("Mage", "skills.generic.shadowlightning", "skills.generic.firebolt",
     "skills.generic.mageclasspassive", "skills.generic.magicdamagemodpassive"),
    ("Ranger", "skills.generic.poisonblastradius", "skills.generic.poisonshot",
     "skills.generic.rangerclasspassive", "skills.generic.rangeattackspeedmodpassive"),
])
def test_starting_skill_hotbar_slots_persisted(class_name, active100, active105,
                                               passive108, passive109):
    """Creation persists each starting skill's authored hotbar ID (client
    *StartingSkills.gc: actives 100/105, passives 108/109) so OP4 manipulator
    slots match the client loadout — incl. the two class PASSIVES."""
    acct = accounts.create_account(f"Slot_{class_name}", "pw")
    created = chars.create_character(f"Slot{class_name}", class_name, acct,
                                     f"Slot_{class_name}")
    assert created is not None
    hb = _hotbar(created.id)
    assert hb.get(active100) == 100
    assert hb.get(active105) == 105
    assert hb.get(passive108) == 108
    assert hb.get(passive109) == 109


def test_creation_persists_appearance_bytes():
    """The 5 creation appearance bytes (skin, face, faceFeature, hair,
    hairColor — C# HandleCharacterCreate order) survive into the characters row
    and the loaded SavedCharacter (they feed the avatar GC Skin/Face/... props)."""
    acct = accounts.create_account("Appear_T", "pw")
    created = chars.create_character(
        "AppearTester", "Mage", acct, "Appear_T", "avatar.classes.WarlockFemale",
        skin=1, face=2, face_feature=3, hair=4, hair_color=5)
    assert created is not None
    loaded = chars.get_character(created.id)
    assert (loaded.skin, loaded.face, loaded.face_feature,
            loaded.hair, loaded.hair_color) == (1, 2, 3, 4, 5)
