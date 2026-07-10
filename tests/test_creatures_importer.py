"""Tests for the ``.gc`` -> ``creatures`` table importer.

Builds a small creature tree under ``tmp_path`` mirroring the real layout (the
``creatures/base`` abstract library + concrete element instances nested as child
nodes) and imports into an in-memory SQLite DB. Proves: concrete creatures are
selected (root at ``StockUnit``) while the ``creatures.base.*`` bases, visuals
and other manipulators are excluded; multipliers/resists/treasure/names/bbox are
mapped with ``extends`` inheritance (incl. nested-child overrides); the
behaviour reference is captured; the fabricated absolute columns (hit_points,
base_damage, *_packet) are dropped; and ``raw_json`` is lossless. No ``/mnt/c``
or real-DB access.
"""
import json
import sqlite3

import pytest

from drserver.data.creatures_importer import (
    collect_creature_rows,
    rebuild_creatures_table,
    _CREATURE_COLUMNS,
)

pytestmark = pytest.mark.unit


def _write(root, rel_path, text):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def extracter(tmp_path):
    """A minimal extracter tree: creature base library + two concrete creatures."""
    # ── abstract base library (creatures/base) ──
    _write(
        tmp_path,
        "creatures/base/UnitStock.gc",
        """UnitStock extends StockUnit
{
    Name = BaseStockUnit;
    static Description extends StockUnitDesc
    {
        AttackRating = 1.0;
        DefenseRating = 1.0;
        CriticalChance = 1.0;
        MaxHealth = 1.0;
        MaxMana = 1.0;
        Speed = 40;
        WalkSpeed = 25;
        TurnRate = 360;
        FactionID = 1;
        CollisionRadius = 5;
        CorpseLingerTime = 900;
    }
    Object
    {
        Description { MinX = -5; MinY = -5; MinZ = 0; MaxX = 5; MaxY = 5; MaxZ = 15; }
    }
}
""",
    )
    _write(
        tmp_path,
        "creatures/base/UnitMelee.gc",
        """UnitMelee extends creatures.base.UnitStock
{
    Description { AttackRating = 1.00; DefenseRating = 1.00; CriticalChance = 1.25; MaxHealth = 1.00; }
    Behavior extends creatures.base.behavior.Melee { }
}
""",
    )
    _write(
        tmp_path,
        "creatures/base/UnitMelee_Champion.gc",
        """UnitMelee_Champion extends creatures.base.UnitMelee
{
    Name = BaseMeleeChampion;
    Description
    {
        CreatureDifficulty = CHAMPION;
        Difficulty = 4.0;
        AttackRating = 1.50;
        DamageMod = 2.20;
        CriticalChance = 4.00;
        MaxHealth = 1.25;
        DivineResist = 10;
        FireResist = 10;
        SpeedMod = 125;
        SizeMod = 125;
        TreasureGenerator = ChampionGG;
        TreasureCount = 2;
        TreasureGenerator2 = ChampionIG;
        TreasureCount2 = 1;
        TreasureGenerator3 = DefaultIG;
        TreasureCount3 = 1;
    }
    Behavior { Description { CollisionPriority = 55; } }
}
""",
    )
    # ── species base (still abstract, but NOT under creatures.base) ──
    _write(
        tmp_path,
        "creatures/amphibs/skull_dog/base/SkullDogBase_Champion.gc",
        """SkullDogBase_Champion extends creatures.base.UnitMelee_Champion
{
    Name = SkullDogChampion;
    Description { Label = "SkullDogChampion"; Speed = 45; CollisionRadius = 7; }
    Object
    {
        Description { MinX = -7; MinY = -13; MinZ = 2; MaxX = 7; MaxY = 13; MaxZ = 15; }
    }
}
""",
    )
    # a non-creature asset that must be excluded (roots at Visual, not StockUnit)
    _write(
        tmp_path,
        "creatures/amphibs/skull_dog/base/SkullDog_Visuals.gc",
        """SkullDog_Visuals extends Visual { Description { Foo = bar; } }
""",
    )
    # ── concrete element instances (nested child nodes) ──
    _write(
        tmp_path,
        "creatures/amphibs/skull_dog/Divine.gc",
        """Divine
{
    Champion extends creatures.amphibs.skull_dog.base.SkullDogBase_Champion
    {
        Description
        {
            DivineResist = 60;
            UseGeneratedName = true;
            FirstNames = "Cujo, Sparky";
            LastNames = "the Destroyer";
        }
        Object
        {
            Description { Visual = VISUAL:creatures.amphibs.skull_dog.base.SkullDog_Visuals.DivineChampion; }
        }
    }
}
""",
    )
    _write(
        tmp_path,
        "creatures/amphibs/skull_dog/Fire.gc",
        """Fire
{
    Champion extends creatures.amphibs.skull_dog.base.SkullDogBase_Champion
    {
        Description { FireResist = 60; Label = "Fire Skull Dog"; }
    }
}
""",
    )
    return str(tmp_path)


def _by_id(rows):
    return {r["gc_type"]: r for r in rows}


def test_selects_only_concrete_creatures(extracter):
    # Act
    rows = collect_creature_rows(extracter)
    ids = {r["gc_type"] for r in rows}

    # Assert — the two concrete element instances, nothing else.
    assert ids == {
        "creatures.amphibs.skull_dog.divine.champion",
        "creatures.amphibs.skull_dog.fire.champion",
    }
    # bases (root at StockUnit but under creatures.base.*) are excluded…
    assert not any(i.startswith("creatures.base.") for i in ids)
    # …and so are non-creature assets (visuals root at Visual, not StockUnit).
    assert not any("visuals" in i for i in ids)


def test_fields_mapped_with_inheritance(extracter):
    # Act
    row = _by_id(collect_creature_rows(extracter))[
        "creatures.amphibs.skull_dog.divine.champion"
    ]

    # Assert — multipliers from the tier base, overrides from the leaf.
    assert row["creature_difficulty"] == "CHAMPION"
    assert row["difficulty"] == 4.0
    assert row["max_health"] == 1.25
    assert row["damage_mod"] == 2.20
    assert row["attack_rating"] == 1.50
    assert row["critical_chance"] == 4.00
    # Name + label inherited from the species base.
    assert row["name"] == "SkullDogChampion"
    assert row["label"] == "SkullDogChampion"
    # Resists: leaf override wins (60), un-overridden tier value persists (10).
    assert row["divine_resist"] == 60
    assert row["fire_resist"] == 10
    # Movement/collision inherited down the chain.
    assert row["speed"] == 45
    assert row["collision_radius"] == 7
    assert row["faction_id"] == 1
    assert row["corpse_linger_time"] == 900


def test_behaviour_treasure_names_bbox(extracter):
    row = _by_id(collect_creature_rows(extracter))[
        "creatures.amphibs.skull_dog.divine.champion"
    ]
    # Behaviour reference comes from the inherited Behavior block's extends.
    assert row["behaviour_type"] == "creatures.base.behavior.Melee"
    assert row["collision_priority"] == 55
    # Treasure generators (1..3 set on the tier base).
    assert (row["treasure_gen1"], row["treasure_count1"]) == ("ChampionGG", 2)
    assert (row["treasure_gen2"], row["treasure_count2"]) == ("ChampionIG", 1)
    assert (row["treasure_gen3"], row["treasure_count3"]) == ("DefaultIG", 1)
    assert row["treasure_gen4"] is None
    # Generated-name pools (leaf).
    assert row["use_generated_name"] == 1
    assert "Cujo" in row["first_names"]
    assert "Destroyer" in row["last_names"]
    # Bounding box from the species base; visual override from the leaf.
    assert row["bbox_min_x"] == -7.0
    assert row["bbox_max_z"] == 15.0
    assert row["visual"].endswith("DivineChampion")


def test_no_fabricated_absolute_columns(extracter):
    # The faithful schema must NOT carry the emulator's invented absolute stats.
    for fabricated in ("hit_points", "mana_points", "base_damage",
                       "hit_points_packet", "mana_points_packet"):
        assert fabricated not in _CREATURE_COLUMNS


def test_raw_json_is_lossless(extracter):
    row = _by_id(collect_creature_rows(extracter))[
        "creatures.amphibs.skull_dog.divine.champion"
    ]
    blob = json.loads(row["raw_json"])
    assert blob["children"]["Description"]["properties"]["CreatureDifficulty"] == "CHAMPION"
    assert "Object" in blob["children"]


def test_rebuild_creatures_table_roundtrip(extracter):
    # Arrange
    conn = sqlite3.connect(":memory:")

    # Act
    n = rebuild_creatures_table(conn, extracter)
    conn.commit()

    # Assert — rows written, table queryable, gc_type is the primary key.
    assert n == 2
    cur = conn.execute(
        "SELECT gc_type, creature_difficulty, max_health FROM creatures "
        "ORDER BY gc_type"
    )
    got = cur.fetchall()
    assert got[0][0] == "creatures.amphibs.skull_dog.divine.champion"
    assert got[0][1] == "CHAMPION"
    assert got[0][2] == 1.25
    # No fabricated columns survive in the live schema.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(creatures)")}
    assert "hit_points" not in cols and "base_damage" not in cols
    conn.close()
