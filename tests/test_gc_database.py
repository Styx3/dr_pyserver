"""Tests for the ``.gc`` registry + inheritance resolver (``gc_database``).

Covers recursive loading, the three-tier path resolution (exact / last-segment
/ last-two), and ``extends`` flattening — including the real Stomp -> ActiveSkillBase
chain, where a child inherits ``Range`` from its base while overriding ``Label``,
``ProfessionType`` and ``CoolDown``. All trees are built under pytest's
``tmp_path`` (fast tmpfs), never ``/mnt/c`` (which hangs).
"""
import pytest

from drserver.data.gc_database import GCDatabase

pytestmark = pytest.mark.unit


def _write(root, rel_path, text):
    """Write ``text`` to ``root/rel_path``, creating parent dirs."""
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def skills_tree(tmp_path):
    """A minimal nested skill tree mirroring extracter/skills/generic/...

    ActiveSkillBase defines Range=90 (+ defaults); Stomp extends it and
    overrides a few fields. The nesting (``Base/`` vs ``generic/``) exercises
    the recursive walk and last-segment path resolution.
    """
    _write(
        tmp_path,
        "skills/generic/Base/ActiveSkillBase.gc",
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
        tmp_path,
        "skills/generic/Stomp.gc",
        """Stomp extends skills.generic.base.ActiveSkillBase
{
    static Description extends ActiveSkillDesc
    {
        Label = "Righteous Stomp";
        ProfessionType = FIGHTER;
        CoolDown = 20;
        MaxSkillLevel = 20;
    }
}
""",
    )
    return tmp_path


# ── loading ──────────────────────────────────────────────────────────────────


def test_load_tree_counts_every_gc_file(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.file_count == 2


def test_load_tree_registers_nodes_by_stem(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.get_node("Stomp") is not None
    assert db.get_node("ActiveSkillBase") is not None


def test_load_tree_raises_on_missing_directory(tmp_path):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(NotADirectoryError):
        GCDatabase().load_tree(str(missing))


def test_duplicate_stems_are_recorded_as_collisions(tmp_path):
    # Arrange — two files named Foo.gc in different subdirs.
    _write(tmp_path, "a/Foo.gc", "Foo { X = 1; }")
    _write(tmp_path, "b/Foo.gc", "Foo { X = 2; }")

    # Act
    db = GCDatabase().load_tree(str(tmp_path))

    # Assert
    assert db.file_count == 2
    assert "Foo" in db.collisions


# ── child path registration ─────────────────────────────────────────────────


def test_named_child_is_registered_under_dotted_path(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    desc = db.get_node("Stomp.Description")
    assert desc is not None
    assert desc.get_string("Label") == "Righteous Stomp"


def test_get_node_is_case_insensitive(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.get_node("stomp") is not None
    assert db.get_node("STOMP.description") is not None


# ── resolve (3-tier fallback) ────────────────────────────────────────────────


def test_resolve_exact_path(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.resolve("Stomp") is db.get_node("Stomp")


def test_resolve_falls_back_to_last_segment(skills_tree):
    # Full dotted path isn't registered as-is; last segment "ActiveSkillBase" is.
    db = GCDatabase().load_tree(str(skills_tree))
    node = db.resolve("skills.generic.base.ActiveSkillBase")
    assert node is not None
    assert node.name == "ActiveSkillBase"


def test_resolve_falls_back_to_last_two_segments(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    # "Stomp.Description" is registered; a longer prefix should still resolve
    # via the last-two-segment fallback.
    node = db.resolve("skills.generic.Stomp.Description")
    assert node is not None
    assert node.get_string("Label") == "Righteous Stomp"


def test_resolve_returns_none_for_unknown_and_empty(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.resolve("NoSuchThing") is None
    assert db.resolve("") is None


# ── full-path registration (dotted_prefix) ──────────────────────────────────


def test_dotted_prefix_resolves_colliding_stems_by_full_path(tmp_path):
    # Two files named MythicBody at different depths (stem collision). Without
    # dotted_prefix, resolve() falls back to the last-segment and returns the
    # collision winner; with it, the full path resolves the exact file.
    _write(tmp_path, "token/MythicBody.gc", 'MythicBody { Label = "Default"; }')
    _write(tmp_path, "token/fi/MythicBody.gc", 'MythicBody { Label = "Fighter"; }')

    db = GCDatabase().load_tree(str(tmp_path), dotted_prefix="quests.base")

    base = db.resolve("quests.base.token.MythicBody")
    fighter = db.resolve("quests.base.token.fi.MythicBody")
    assert base is not None and fighter is not None
    assert base.get_string("Label") == "Default"
    assert fighter.get_string("Label") == "Fighter"


def test_dotted_prefix_is_optional_and_backward_compatible(skills_tree):
    # Without dotted_prefix, resolution still works via stem + last-segment
    # fallback (the original behaviour); the full file path is not an exact key.
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.get_node("Stomp") is not None
    # last-segment fallback still resolves a full extends path
    assert db.resolve("skills.generic.base.ActiveSkillBase") is db.get_node("ActiveSkillBase")


# ── inheritance flattening ───────────────────────────────────────────────────


def test_resolve_with_inheritance_merges_child_blocks_from_parent(skills_tree):
    # Arrange
    db = GCDatabase().load_tree(str(skills_tree))

    # Act — flatten Stomp's extends chain.
    merged = db.resolve_with_inheritance("Stomp")

    # Assert — the Description child exists and merges both layers.
    assert merged is not None
    desc = merged.get_child("Description")
    assert desc is not None
    # overridden by Stomp
    assert desc.get_string("Label") == "Righteous Stomp"
    assert desc.get_string("ProfessionType") == "FIGHTER"
    assert desc.get_int("CoolDown") == 20
    # inherited from ActiveSkillBase (Stomp never defines Range)
    assert desc.get_int("Range") == 90


def test_resolve_with_inheritance_caches_result(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    first = db.resolve_with_inheritance("Stomp")
    second = db.resolve_with_inheritance("Stomp")
    assert first is second


def test_resolve_with_inheritance_returns_none_for_unknown(skills_tree):
    db = GCDatabase().load_tree(str(skills_tree))
    assert db.resolve_with_inheritance("Ghost") is None


def test_node_without_extends_resolves_to_itself(tmp_path):
    _write(tmp_path, "Plain.gc", "Plain { A = 1; }")
    db = GCDatabase().load_tree(str(tmp_path))
    merged = db.resolve_with_inheritance("Plain")
    assert merged is not None
    assert merged.get_int("A") == 1


def test_inheritance_is_cycle_safe(tmp_path):
    # Arrange — A extends B, B extends A (pathological; must not recurse forever).
    _write(tmp_path, "A.gc", "A extends B { X = 1; }")
    _write(tmp_path, "B.gc", "B extends A { Y = 2; }")
    db = GCDatabase().load_tree(str(tmp_path))

    # Act
    merged = db.resolve_with_inheritance("A")

    # Assert — terminates and keeps A's own property.
    assert merged is not None
    assert merged.get_int("X") == 1


def test_child_only_in_parent_is_inherited(tmp_path):
    # Parent has a child block the subclass lacks entirely.
    _write(tmp_path, "Base.gc", "Base { Meta { Tag = \"base\"; } }")
    _write(tmp_path, "Child.gc", "Child extends Base { Other = 1; }")
    db = GCDatabase().load_tree(str(tmp_path))

    merged = db.resolve_with_inheritance("Child")
    assert merged is not None
    meta = merged.get_child("Meta")
    assert meta is not None
    assert meta.get_string("Tag") == "base"
