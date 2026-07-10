"""Tests for the ``.gc`` -> ``skills`` table importer (pilot of the rebuild).

Builds a small skill tree under ``tmp_path`` and imports into an in-memory
SQLite DB — proves the importer maps real ``Description`` fields (with ``extends``
inheritance) onto the new schema, and drops the fabricated legacy columns. No
``/mnt/c`` or real-DB access.
"""
import json
import sqlite3

import pytest

from drserver.data.skills_importer import (
    collect_skill_rows,
    gc_type_for,
    rebuild_skills_table,
    skill_row,
)

pytestmark = pytest.mark.unit


def _write(root, rel_path, text):
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def skills_root(tmp_path):
    """Two real-shaped player skills + a structural base with no Description.

    ``Stomp`` extends ``ActiveSkillBase`` (inherits ``Range``); ``Sprint`` is a
    RANGER skill; ``ActiveSkillBase`` itself is a base whose Description should
    still surface for skills that extend it but which, as a node, is skipped
    only if it lacks a Description (here it has one — see assertions).
    """
    root = tmp_path / "skills"
    _write(
        root,
        "generic/Base/ActiveSkillBase.gc",
        """ActiveSkillBase extends ActiveSkill
{
    static Description extends ActiveSkillDesc
    {
        Label = "Default";
        ProfessionType = NONE;
        Range = 90;
        CoolDown = 0;
        MaxSkillLevel = 1;
    }
}
""",
    )
    _write(
        root,
        "generic/Stomp.gc",
        """Stomp extends skills.generic.base.ActiveSkillBase
{
    static Description extends ActiveSkillDesc
    {
        Label = "Righteous Stomp";
        Description = "A wave of holiness.";
        Category = "Offensive";
        ProfessionType = FIGHTER;
        ElementType = DIVINE;
        TargetType = SELF;
        ManaCostMod = 1.5;
        CoolDown = 20;
        AnimationID = 67;
        RequiredLevel = 3;
        MaxSkillLevel = 20;
        Icon = RighteousStomp;
        Effect = skills.generic.Stomp.Effect;
    }
}
""",
    )
    _write(
        root,
        "professions/Sprint.gc",
        """Sprint extends skills.generic.base.ActiveSkillBase
{
    static Description extends ActiveSkillDesc
    {
        Label = "Sprint";
        ProfessionType = RANGER;
        CoolDown = 8;
        MaxSkillLevel = 12;
    }
}
""",
    )
    # A node with NO Description — must be skipped by the importer.
    _write(root, "generic/Base/SpellEffect.gc", "SpellEffect { Foo = 1; }")
    return str(root)


# ── gc_type derivation ───────────────────────────────────────────────────────


def test_gc_type_for_builds_dotted_path():
    assert (
        gc_type_for("/x/skills", "/x/skills/generic/Stomp.gc")
        == "skills.generic.Stomp"
    )


def test_gc_type_for_honours_custom_prefix():
    assert gc_type_for("/x/skills", "/x/skills/A.gc", prefix="items") == "items.A"


# ── row mapping ──────────────────────────────────────────────────────────────


def test_skill_row_maps_real_description_fields():
    # Build a Description node directly via the parser.
    from drserver.data.gc_parser import parse

    node = parse(
        'S { Description { Label="Righteous Stomp"; ProfessionType=FIGHTER;'
        ' CoolDown=20; MaxSkillLevel=20; ManaCostMod=1.5; } }'
    )
    desc = node.get_child("Description")
    row = skill_row("skills.generic.Stomp", desc, "generic/Stomp.gc")

    assert row["gc_type"] == "skills.generic.Stomp"
    assert row["name"] == "Stomp"
    assert row["label"] == "Righteous Stomp"
    assert row["profession_type"] == "FIGHTER"
    assert row["cool_down"] == 20
    assert row["max_skill_level"] == 20
    assert row["mana_cost_mod"] == 1.5
    assert row["source_file"] == "generic/Stomp.gc"


def test_skill_row_uses_none_for_absent_fields():
    from drserver.data.gc_parser import parse

    node = parse('S { Description { Label="X"; } }')
    desc = node.get_child("Description")
    row = skill_row("skills.X", desc, "X.gc")
    assert row["range"] is None
    assert row["animation_id"] is None
    assert row["element_type"] is None


def test_skill_row_raw_json_captures_all_properties():
    from drserver.data.gc_parser import parse

    node = parse('S { Description { Label="X"; Weird=42; } }')
    desc = node.get_child("Description")
    row = skill_row("skills.X", desc, "X.gc")
    raw = json.loads(row["raw_json"])
    assert raw["Label"] == "X"
    assert raw["Weird"] == "42"  # raw values are strings


# ── collection + inheritance ─────────────────────────────────────────────────


def test_collect_skips_nodes_without_description(skills_root):
    rows = collect_skill_rows(skills_root)
    gc_types = {r["gc_type"] for r in rows}
    assert "skills.generic.Base.SpellEffect" not in gc_types  # no Description


def test_collect_resolves_inherited_range_from_base(skills_root):
    rows = {r["gc_type"]: r for r in collect_skill_rows(skills_root)}
    stomp = rows["skills.generic.Stomp"]
    # Stomp never declares Range; it must inherit 90 from ActiveSkillBase.
    assert stomp["range"] == 90
    # …while its own overrides win.
    assert stomp["label"] == "Righteous Stomp"
    assert stomp["cool_down"] == 20


def test_collect_finds_all_player_skills(skills_root):
    rows = {r["gc_type"]: r for r in collect_skill_rows(skills_root)}
    assert rows["skills.professions.Sprint"]["profession_type"] == "RANGER"
    assert rows["skills.generic.Stomp"]["profession_type"] == "FIGHTER"


# ── full rebuild into SQLite ─────────────────────────────────────────────────


def test_rebuild_drops_fabricated_schema_and_writes_real_columns(skills_root):
    # Arrange — start from the *legacy fabricated* table to prove it's replaced.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE skills (id INTEGER PRIMARY KEY, name TEXT, experience INTEGER, attr_strength INTEGER)"
    )
    conn.execute("INSERT INTO skills (name, experience, attr_strength) VALUES ('Fake', 100, 5)")

    # Act
    n = rebuild_skills_table(conn, skills_root)
    conn.commit()

    # Assert — fabricated columns gone, real ones present.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(skills)")}
    assert "experience" not in cols
    assert "attr_strength" not in cols
    assert {"gc_type", "label", "profession_type", "cool_down", "raw_json"} <= cols
    assert n >= 3


def test_rebuilt_rows_are_queryable_by_profession(skills_root):
    conn = sqlite3.connect(":memory:")
    rebuild_skills_table(conn, skills_root)
    conn.commit()
    conn.row_factory = sqlite3.Row

    players = conn.execute(
        "SELECT gc_type FROM skills WHERE profession_type IN ('FIGHTER','MAGE','RANGER','SUMMONER') ORDER BY gc_type"
    ).fetchall()
    names = [r["gc_type"] for r in players]
    assert "skills.generic.Stomp" in names
    assert "skills.professions.Sprint" in names


def test_rebuild_is_idempotent(skills_root):
    conn = sqlite3.connect(":memory:")
    first = rebuild_skills_table(conn, skills_root)
    conn.commit()
    second = rebuild_skills_table(conn, skills_root)
    conn.commit()
    assert first == second
    count = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    assert count == second
