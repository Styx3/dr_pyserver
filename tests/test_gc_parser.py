"""Tests for the ``.gc`` content parser (``drserver.data.gc_parser``).

The ``.gc`` format is tier-2 ground truth (extracted client content). These
tests pin the parser against both minimal hand-written snippets and the *real*
``skills/generic/Stomp.gc`` shipped by the client, so the importer it feeds can
be trusted. Inline text only — no ``/mnt/c`` reads (those hang).
"""
import pytest

from drserver.data.gc_parser import GCNode, parse, parse_file

pytestmark = pytest.mark.unit


# Verbatim copy of extracter/skills/generic/Stomp.gc (tier-2 ground truth),
# trimmed of trailing whitespace only. Used to prove the parser handles the
# real grammar: named child blocks, static children, anonymous ``*`` children,
# quoted strings containing ``[...]`` placeholders, bare enum/float values, and
# both ``//`` and column-aligned comments.
STOMP_GC = '''Stomp extends skills.generic.base.ActiveSkillBase
{
\tDescription
\t{
\t\tLabel = "Righteous Stomp";
\t\tDescription = "A wave of holiness that may knock down up to [SpellAOEEffect.NumTargets] enemies within [SpellAOEEffect.Radius] meters and deal [SpellDamageEffect.MinDamage] to [SpellDamageEffect.MaxDamage] Divine Damage to each one.";
\t\tCategory = "Offensive";

\t\tProfessionType = FIGHTER;
\t\tElementType = DIVINE;

\t\tIcon = RighteousStomp;
\t\tActiveIcon = RighteousStomp_on;

\t\tTargetType = SELF;

\t\tManaCostMod = 1.5;

\t\tCoolDown = 20;

\t\tAnimationID = 67;

//\t\tThis modifies the gold value
\t\tGoldValueMod = 1.0;

//\t\tThis is the intended starting character level for the skill
\t\tRequiredLevel = 3;

\t\tRequiredLevelInc = 5;

\t\tMaxSkillLevel = 20;

\t\tEffect = skills.generic.Stomp.Effect;
\t}

\tstatic StompEffect extends base.Effect
\t{
\t\tLifetime = 30;
\t\tLingerTime = 30;

\t\tDescription
\t\t{
\t\t\tVisual = D3D:Stomp_Cast;
\t\t\tMinX = -10;
\t\t}
\t}

\tstatic Effect extends SpellEffect
\t{
\t\t* extends SpellSnapToGroundEffect
\t\t{
\t\t\t* extends SpellEffectEffect
\t\t\t{
\t\t\t\tEffect = skills.generic.Stomp.StompEffect;
\t\t\t\tEffectLocation = SOURCE;
\t\t\t}
\t\t}

\t\t* extends SpellAOEEffect
\t\t{
\t\t\tName = "SpellAOEEffect";
\t\t\tTargetType = ENEMY;
\t\t\tNumTargetsMin = 6;
\t\t}
\t}

\tstatic VisualModifier extends EffectMod
\t{
\t\tstatic Description extends EffectModDesc
\t\t{
\t\t\tRemoveOnDeath = true;
\t\t\tVisual = VISUAL:skills.generic.Visuals.StompVisual;
\t\t}
\t}
}
'''


# ── basic structure ────────────────────────────────────────────────────────


def test_parses_node_name_and_extends_path():
    # Arrange
    text = "Stomp extends skills.generic.base.ActiveSkillBase { }"

    # Act
    node = parse(text)

    # Assert
    assert node is not None
    assert node.name == "Stomp"
    assert node.extends == "skills.generic.base.ActiveSkillBase"


def test_node_without_extends_has_none_extends():
    node = parse("Foo { Bar = 1; }")
    assert node is not None
    assert node.name == "Foo"
    assert node.extends is None


def test_returns_none_for_empty_or_whitespace_source():
    assert parse("") is None
    assert parse("   \n\t  ") is None


def test_static_keyword_sets_is_static_flag():
    node = parse("static Thing { }")
    assert node is not None
    assert node.name == "Thing"
    assert node.is_static is True


# ── property typing + case-insensitive lookup ───────────────────────────────


def test_reads_string_int_float_and_bool_properties():
    # Arrange
    text = 'N { Label = "Hi there"; Cost = 20; Mod = 1.5; Flag = true; }'

    # Act
    node = parse(text)

    # Assert
    assert node is not None
    assert node.get_string("Label") == "Hi there"
    assert node.get_int("Cost") == 20
    assert node.get_float("Mod") == 1.5
    assert node.get_bool("Flag") is True


def test_property_lookup_is_case_insensitive():
    node = parse("N { CoolDown = 20; }")
    assert node is not None
    assert node.get_int("cooldown") == 20
    assert node.get_int("COOLDOWN") == 20
    assert node.has_property("CoolDown") is True
    assert node.has_property("nope") is False


def test_get_int_coerces_float_valued_property():
    node = parse("N { Level = 3.0; }")
    assert node is not None
    assert node.get_int("Level") == 3


def test_accessors_return_fallback_for_missing_keys():
    node = parse("N { }")
    assert node is not None
    assert node.get_string("x", "def") == "def"
    assert node.get_int("x", 7) == 7
    assert node.get_float("x", 2.5) == 2.5
    assert node.get_bool("x", True) is True


def test_bool_accepts_one_as_true_and_false_otherwise():
    node = parse('N { A = 1; B = false; C = "no"; }')
    assert node is not None
    assert node.get_bool("A") is True
    assert node.get_bool("B") is False
    assert node.get_bool("C") is False


# ── value lexing edge cases ──────────────────────────────────────────────────


def test_quoted_string_preserves_braces_brackets_and_semicolons():
    # A quoted value must not be terminated early by ``}``/``;`` inside it.
    text = 'N { D = "deal [Min] to [Max]; ok {x}"; After = 5; }'
    node = parse(text)
    assert node is not None
    assert node.get_string("D") == "deal [Min] to [Max]; ok {x}"
    assert node.get_int("After") == 5


def test_bare_value_terminates_at_semicolon():
    node = parse("N { Type = skills.generic.Stomp.Effect; }")
    assert node is not None
    assert node.get_string("Type") == "skills.generic.Stomp.Effect"


def test_bare_value_with_colon_prefix_is_kept_whole():
    node = parse("N { Visual = D3D:Stomp_Cast; }")
    assert node is not None
    assert node.get_string("Visual") == "D3D:Stomp_Cast"


def test_negative_numeric_value_is_parsed():
    node = parse("N { MinX = -10; }")
    assert node is not None
    assert node.get_int("MinX") == -10


# ── comments ─────────────────────────────────────────────────────────────────


def test_line_and_block_comments_are_stripped():
    text = "N { // ignore me\n A = 1; /* and me */ B = 2; }"
    node = parse(text)
    assert node is not None
    assert node.get_int("A") == 1
    assert node.get_int("B") == 2


def test_comment_markers_inside_quoted_string_are_preserved():
    node = parse('N { Url = "http://example.com/x"; }')
    assert node is not None
    assert node.get_string("Url") == "http://example.com/x"


# ── named children ───────────────────────────────────────────────────────────


def test_named_child_block_is_captured_with_get_child():
    text = "Skill { Description { Label = \"X\"; } }"
    node = parse(text)
    assert node is not None
    child = node.get_child("Description")
    assert child is not None
    assert child.get_string("Label") == "X"


def test_get_child_is_case_insensitive():
    node = parse("Skill { Description { } }")
    assert node is not None
    assert node.get_child("description") is not None
    assert node.get_child("MISSING") is None


def test_child_block_with_extends_records_parent_path():
    node = parse("Skill { static Effect extends SpellEffect { } }")
    assert node is not None
    eff = node.get_child("Effect")
    assert eff is not None
    assert eff.extends == "SpellEffect"
    assert eff.is_static is True


# ── anonymous children ───────────────────────────────────────────────────────


def test_anonymous_child_goes_to_anonymous_children_list():
    text = "Effect { * extends SpellAOEEffect { Name = \"x\"; } }"
    node = parse(text)
    assert node is not None
    assert node.children == {}
    assert len(node.anonymous_children) == 1
    anon = node.anonymous_children[0]
    assert anon.is_anonymous is True
    assert anon.extends == "SpellAOEEffect"
    assert anon.get_string("Name") == "x"


def test_nested_anonymous_children_are_parsed_recursively():
    text = "Effect { * extends Outer { * extends Inner { K = 1; } } }"
    node = parse(text)
    assert node is not None
    outer = node.anonymous_children[0]
    assert outer.extends == "Outer"
    inner = outer.anonymous_children[0]
    assert inner.extends == "Inner"
    assert inner.get_int("K") == 1


# ── real client content (ground truth) ──────────────────────────────────────


def test_parses_real_stomp_gc_top_level():
    node = parse(STOMP_GC, "Stomp")
    assert node is not None
    assert node.name == "Stomp"
    assert node.extends == "skills.generic.base.ActiveSkillBase"
    assert node.source_file == "Stomp"


def test_stomp_description_child_holds_real_fields():
    node = parse(STOMP_GC, "Stomp")
    assert node is not None
    desc = node.get_child("Description")
    assert desc is not None
    assert desc.get_string("Label") == "Righteous Stomp"
    assert desc.get_string("ProfessionType") == "FIGHTER"
    assert desc.get_string("ElementType") == "DIVINE"
    assert desc.get_int("CoolDown") == 20
    assert desc.get_int("RequiredLevel") == 3
    assert desc.get_int("MaxSkillLevel") == 20
    assert desc.get_float("ManaCostMod") == 1.5
    assert desc.get_string("Effect") == "skills.generic.Stomp.Effect"
    # the long templated description keeps its [..] placeholders intact
    assert "[SpellAOEEffect.NumTargets]" in desc.get_string("Description")


def test_stomp_static_children_are_named():
    node = parse(STOMP_GC, "Stomp")
    assert node is not None
    assert node.get_child("StompEffect") is not None
    assert node.get_child("StompEffect").is_static is True
    assert node.get_child("Effect") is not None
    assert node.get_child("VisualModifier") is not None


def test_stomp_effect_has_two_anonymous_children():
    node = parse(STOMP_GC, "Stomp")
    assert node is not None
    eff = node.get_child("Effect")
    assert eff is not None
    assert eff.extends == "SpellEffect"
    assert len(eff.anonymous_children) == 2
    extends_paths = {c.extends for c in eff.anonymous_children}
    assert extends_paths == {"SpellSnapToGroundEffect", "SpellAOEEffect"}


def test_parse_file_uses_filename_stem_as_source(tmp_path):
    # Arrange — write to tmp (fast), not /mnt/c.
    p = tmp_path / "MySkill.gc"
    p.write_text("MySkill { Cost = 1; }", encoding="utf-8")

    # Act
    node = parse_file(str(p))

    # Assert
    assert node is not None
    assert node.name == "MySkill"
    assert node.source_file == "MySkill"
    assert node.get_int("Cost") == 1


def test_multiline_quoted_value_with_separated_semicolon_keeps_following_props():
    # A multi-line quoted value whose terminating ';' sits on its own line must
    # not abort the block: every property after it must still parse. Regression
    # for the QuestObsolete bug (TokenReward/CashReward were silently dropped,
    # so quests inheriting it got the wrong rewards).
    text = (
        'Q { Description { MinLevel = 1; '
        'Summary = "\n  A long\n  recall notice."\n   ; '
        'TokenReward = 1; CashReward = 0; } }'
    )
    node = parse(text)
    desc = node.get_child("Description")
    assert desc.get_int("MinLevel") == 1
    assert "A long" in desc.get_string("Summary")
    assert desc.get_int("TokenReward") == 1      # would be dropped before the fix
    assert desc.get_float("CashReward") == 0
