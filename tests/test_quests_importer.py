"""Tests for the ``.gc`` -> ``quests`` table importer.

Builds a small quest tree under ``tmp_path`` mirroring the real layout
(``quests/base/`` class library + ``world/**/quest/`` instances) and imports
into an in-memory SQLite DB. Proves: real ``Description`` fields are mapped with
``extends`` inheritance; the objective sub-tree (named + anonymous) is captured;
selection keeps only Quest-rooted nodes with a Description (excludes
item/token defs and item-generator tables); stem collisions in the nested
``token/`` tree resolve by full path; and the fabricated legacy columns are
dropped. No ``/mnt/c`` or real-DB access.
"""
import json
import sqlite3

import pytest

from drserver.data.quests_importer import (
    collect_quest_rows,
    gc_type_for,
    rebuild_quests_table,
)

pytestmark = pytest.mark.unit


def _write(root, rel_path, text):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def extracter(tmp_path):
    """A minimal extracter tree: quest class library + world quest instances."""
    # ── class library (quests/base) ──
    _write(
        tmp_path,
        "quests/base/Quest.gc",
        """Quest extends Quest
{
    static Description extends QuestDesc
    {
        TokenReward = 0;
        CashReward = 0.5;
        GrantXPBuff = false;
    }
}
""",
    )
    _write(
        tmp_path,
        "quests/base/QuestObsolete.gc",
        """QuestObsolete extends quests.base.Quest
{
    static Description extends QuestDesc
    {
        MinLevel = 1;
        NPC = world.town.npc.TownCommander;
        Label = "Obsolete Quest";
        Summary = "A recall has been issued.";
        TokenReward = 1;
        CashReward = 0;
    }
}
""",
    )
    _write(tmp_path, "quests/base/KillObjective.gc", "KillObjective extends KillObjective { }")
    _write(tmp_path, "quests/base/ItemObjective.gc", "ItemObjective extends ItemObjective { }")
    # tutorial quest that lives in the class-library tree (not world/)
    _write(
        tmp_path,
        "quests/base/HelperNoobosaur/Q101_a1.gc",
        """Q101_a1 extends quests.base.Quest
{
    Description
    {
        Label = "Getting Help";
        Repeatable = true;
    }
}
""",
    )

    # ── colliding token bases (same stem `MythicBody` at two depths) ──
    _write(
        tmp_path,
        "quests/base/token/MythicBody.gc",
        """MythicBody extends quests.base.Quest
{
    Description
    {
        Label = "Default Body";
        NumRewardItems = 1;
    }
}
""",
    )
    _write(
        tmp_path,
        "quests/base/token/fi/MythicBody.gc",
        """MythicBody extends quests.base.token.MythicBody
{
    Description
    {
        Label = "Fighter Body";
        RewardItemGenerator = TokenRewardFighterIG;
    }
}
""",
    )

    # ── world quest instances ──
    _write(
        tmp_path,
        "world/dungeon02/quest/Q01_a1.gc",
        """Q01_a1 extends quests.base.QuestObsolete
{
    Description
    {
        UIZoneInfo = quests.UIDesc.Algors_TerrorDome;
        Label = "Agrock's Gauntlet";
        FollowupQuest = world.dungeon02.quest.Q01_a1_rev;
    }

    * extends quests.base.KillObjective
    {
        Name = "MainObjective1";
        Label = "Slay the rats";
        MonsterType = world.dungeon02.mob.Rat;
        RequiredKills = 5;
    }
}
""",
    )
    # token-trade quest, extends the *fighter* MythicBody (collision case)
    _write(
        tmp_path,
        "world/dungeon02/quest/token/fi/MythBody.gc",
        """MythBody extends quests.base.token.fi.MythicBody
{
    Description
    {
        NPC = world.dungeon02.npc.TokenMaster;
        MinLevel = 55;
        MaxLevel = 100;
    }
}
""",
    )
    # an *item* definition under a quest/ dir — roots at a non-Quest class → excluded
    _write(
        tmp_path,
        "world/dungeon02/quest/token/Relic.gc",
        """Relic extends SomeItemClass
{
    Description { Label = "A shiny relic"; }
}
""",
    )
    # an item-generator table — no Description → excluded
    _write(
        tmp_path,
        "world/dungeon02/quest/generators/Loot_IG.gc",
        """Loot_IG extends ItemGeneratorTable
{
    Entry { Chance = 50; Item = foo; }
}
""",
    )
    return str(tmp_path)


# ── gc_type derivation ────────────────────────────────────────────────────────


def test_gc_type_for_world_quest():
    assert (
        gc_type_for("/x", "/x/world/dungeon02/quest/Q01_a1.gc")
        == "world.dungeon02.quest.Q01_a1"
    )


def test_gc_type_for_helper_quest():
    assert (
        gc_type_for("/x", "/x/quests/base/HelperNoobosaur/Q101_a1.gc")
        == "quests.base.HelperNoobosaur.Q101_a1"
    )


# ── selection ─────────────────────────────────────────────────────────────────


def test_selection_keeps_real_quests_only(extracter):
    rows = {r["gc_type"]: r for r in collect_quest_rows(extracter)}
    # real quests kept
    assert "world.dungeon02.quest.Q01_a1" in rows
    assert "world.dungeon02.quest.token.fi.MythBody" in rows
    assert "quests.base.HelperNoobosaur.Q101_a1" in rows
    # item def (root != Quest) and generator (no Description) excluded
    assert "world.dungeon02.quest.token.Relic" not in rows
    assert "world.dungeon02.quest.generators.Loot_IG" not in rows


def test_helper_noobosaur_quests_are_included(extracter):
    rows = {r["gc_type"]: r for r in collect_quest_rows(extracter)}
    assert rows["quests.base.HelperNoobosaur.Q101_a1"]["label"] == "Getting Help"
    assert rows["quests.base.HelperNoobosaur.Q101_a1"]["repeatable"] == 1


# ── inheritance ───────────────────────────────────────────────────────────────


def test_quest_inherits_base_fields_and_overrides(extracter):
    rows = {r["gc_type"]: r for r in collect_quest_rows(extracter)}
    q = rows["world.dungeon02.quest.Q01_a1"]
    # own override wins
    assert q["label"] == "Agrock's Gauntlet"
    assert q["followup_quest"] == "world.dungeon02.quest.Q01_a1_rev"
    assert q["ui_zone_info"] == "quests.UIDesc.Algors_TerrorDome"
    # inherited from QuestObsolete
    assert q["min_level"] == 1
    assert q["npc"] == "world.town.npc.TownCommander"
    assert q["token_reward"] == 1
    # QuestObsolete overrides Quest's 0.5 -> 0
    assert q["cash_reward"] == 0
    # inherited all the way from the Quest base
    assert q["grant_xp_buff"] == 0
    # base_class is the immediate parent (last segment)
    assert q["base_class"] == "QuestObsolete"


def test_collision_resolves_by_full_path(extracter):
    # MythBody extends quests.base.token.fi.MythicBody, whose stem `MythicBody`
    # collides with quests.base.token.MythicBody. Full-path resolution must pick
    # the *fighter* base (Label "Fighter Body", a RewardItemGenerator) and still
    # root at Quest two levels up.
    rows = {r["gc_type"]: r for r in collect_quest_rows(extracter)}
    m = rows["world.dungeon02.quest.token.fi.MythBody"]
    assert m["label"] == "Fighter Body"
    assert m["reward_item_generator"] == "TokenRewardFighterIG"
    assert m["num_reward_items"] == 1  # inherited from the class-agnostic base
    assert m["min_level"] == 55
    assert m["npc"] == "world.dungeon02.npc.TokenMaster"


# ── objectives ────────────────────────────────────────────────────────────────


def test_objectives_are_counted_and_classified(extracter):
    rows = {r["gc_type"]: r for r in collect_quest_rows(extracter)}
    q = rows["world.dungeon02.quest.Q01_a1"]
    assert q["objective_count"] == 1
    assert q["objective_kinds"] == "KillObjective"


def test_raw_json_captures_objective_subtree(extracter):
    rows = {r["gc_type"]: r for r in collect_quest_rows(extracter)}
    q = rows["world.dungeon02.quest.Q01_a1"]
    raw = json.loads(q["raw_json"])
    # Description child present with merged props
    assert raw["children"]["Description"]["properties"]["Label"] == "Agrock's Gauntlet"
    # anonymous objective preserved losslessly
    objs = raw["anonymous_children"]
    assert len(objs) == 1
    assert objs[0]["properties"]["MonsterType"] == "world.dungeon02.mob.Rat"
    assert objs[0]["properties"]["RequiredKills"] == "5"


# ── full rebuild into SQLite ──────────────────────────────────────────────────


def test_rebuild_drops_fabricated_schema_and_writes_real_columns(extracter):
    # Arrange — start from the legacy fabricated table to prove it's replaced.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE quests (id TEXT PRIMARY KEY, name TEXT, zone TEXT, "
        "faction TEXT, status TEXT, quest_type TEXT, reward_experience INTEGER)"
    )
    conn.execute(
        "INSERT INTO quests (id, name, zone, faction, status, quest_type, "
        "reward_experience) VALUES ('fake', 'Fake', '', '', 'available', 'quest', 99)"
    )

    # Act
    n = rebuild_quests_table(conn, extracter)
    conn.commit()

    # Assert — fabricated columns gone, real ones present.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(quests)")}
    assert "zone" not in cols
    assert "faction" not in cols
    assert "status" not in cols
    assert "quest_type" not in cols
    assert "reward_experience" not in cols
    assert {"gc_type", "label", "min_level", "objective_kinds", "raw_json"} <= cols
    assert n == 3  # Q01_a1 + MythBody + HelperNoobosaur Q101_a1


def test_rebuild_is_idempotent(extracter):
    conn = sqlite3.connect(":memory:")
    first = rebuild_quests_table(conn, extracter)
    conn.commit()
    second = rebuild_quests_table(conn, extracter)
    conn.commit()
    assert first == second
    count = conn.execute("SELECT COUNT(*) FROM quests").fetchone()[0]
    assert count == second
