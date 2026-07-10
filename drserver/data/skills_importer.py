"""Rebuild the ``skills`` content table from ``.gc`` ground truth.

This is the pilot of the ``.gc`` -> SQLite importer (see ``gc_parser`` /
``gc_database``). The legacy ``skills`` table inherited from the older C# build
was **fabricated**: its columns (``experience``, ``attr_strength`` …) map onto
C#'s invented ``SkillData``/``SkillAttributes`` and have *no* counterpart in the
client's ``.gc`` files. Nothing in the Python server reads that table, so we are
free to replace it with a faithful, regenerable mirror of the real
``skills/**/*.gc`` content (tier-2 ground truth).

A skill's player-facing metadata lives in its ``Description`` child after
``extends`` inheritance is resolved (e.g. ``Stomp`` inherits ``Range`` from
``ActiveSkillBase``). We import every skill that has a resolved ``Description``;
``profession_type`` is a column so the player-trainable subset (FIGHTER / MAGE /
RANGER / SUMMONER) is a simple ``WHERE`` away rather than a baked-in filter.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .gc_database import GCDatabase
from .gc_parser import GCNode

# Columns mirror the real ``Description`` fields (snake_case). ``gc_type`` is the
# dotted content path (unique per file → primary key); ``source_file`` and
# ``raw_json`` preserve full provenance / lossless capture of every property.
_CREATE_SKILLS = """
CREATE TABLE skills (
    gc_type            TEXT PRIMARY KEY,
    name               TEXT,
    label              TEXT,
    description        TEXT,
    category           TEXT,
    profession_type    TEXT,
    element_type       TEXT,
    target_type        TEXT,
    spell_use          TEXT,
    range              REAL,
    mana_cost_mod      REAL,
    cool_down          REAL,
    cool_down_inc      REAL,
    animation_id       INTEGER,
    gold_value_mod     REAL,
    required_level     INTEGER,
    required_level_inc INTEGER,
    max_skill_level    INTEGER,
    icon               TEXT,
    active_icon        TEXT,
    effect             TEXT,
    source_file        TEXT,
    raw_json           TEXT
)
"""

_INSERT_SKILL = """
INSERT OR REPLACE INTO skills (
    gc_type, name, label, description, category, profession_type, element_type,
    target_type, spell_use, range, mana_cost_mod, cool_down, cool_down_inc,
    animation_id, gold_value_mod, required_level, required_level_inc,
    max_skill_level, icon, active_icon, effect, source_file, raw_json
) VALUES (
    :gc_type, :name, :label, :description, :category, :profession_type,
    :element_type, :target_type, :spell_use, :range, :mana_cost_mod, :cool_down,
    :cool_down_inc, :animation_id, :gold_value_mod, :required_level,
    :required_level_inc, :max_skill_level, :icon, :active_icon, :effect,
    :source_file, :raw_json
)
"""


def gc_type_for(skills_root: str, file_path: str, prefix: str = "skills") -> str:
    """Derive the dotted GC path for a ``.gc`` file, e.g. ``skills.generic.Stomp``."""
    rel = os.path.relpath(file_path, skills_root)
    rel = os.path.splitext(rel)[0]
    dotted = rel.replace(os.sep, ".").replace("/", ".")
    return f"{prefix}.{dotted}" if prefix else dotted


def _gc_files(skills_root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirs, names in os.walk(skills_root):
        for nm in names:
            if nm.lower().endswith(".gc"):
                out.append(os.path.join(dirpath, nm))
    out.sort()
    return out


def skill_row(gc_type: str, desc: GCNode, source_file: str) -> dict:
    """Map a resolved ``Description`` node to a ``skills`` table row dict."""
    name = gc_type.rsplit(".", 1)[-1]
    return {
        "gc_type": gc_type,
        "name": name,
        "label": desc.get_string("Label") or None,
        "description": desc.get_string("Description") or None,
        "category": desc.get_string("Category") or None,
        "profession_type": desc.get_string("ProfessionType") or None,
        "element_type": desc.get_string("ElementType") or None,
        "target_type": desc.get_string("TargetType") or None,
        "spell_use": desc.get_string("SpellUse") or None,
        "range": desc.get_float("Range") if desc.has_property("Range") else None,
        "mana_cost_mod": desc.get_float("ManaCostMod") if desc.has_property("ManaCostMod") else None,
        "cool_down": desc.get_float("CoolDown") if desc.has_property("CoolDown") else None,
        "cool_down_inc": desc.get_float("CoolDownInc") if desc.has_property("CoolDownInc") else None,
        "animation_id": desc.get_int("AnimationID") if desc.has_property("AnimationID") else None,
        "gold_value_mod": desc.get_float("GoldValueMod") if desc.has_property("GoldValueMod") else None,
        "required_level": desc.get_int("RequiredLevel") if desc.has_property("RequiredLevel") else None,
        "required_level_inc": desc.get_int("RequiredLevelInc") if desc.has_property("RequiredLevelInc") else None,
        "max_skill_level": desc.get_int("MaxSkillLevel") if desc.has_property("MaxSkillLevel") else None,
        "icon": desc.get_string("Icon") or None,
        "active_icon": desc.get_string("ActiveIcon") or None,
        "effect": desc.get_string("Effect") or None,
        "source_file": source_file,
        "raw_json": json.dumps(desc.properties, sort_keys=True),
    }


def collect_skill_rows(skills_root: str, prefix: str = "skills") -> list[dict]:
    """Parse + inheritance-resolve every skill ``.gc``; return rows for those
    that have a ``Description``. Pure (no DB writes) so it is easy to dry-run."""
    db = GCDatabase().load_tree(skills_root)
    rows: list[dict] = []
    for fp in _gc_files(skills_root):
        from .gc_parser import parse_file

        node = parse_file(fp)
        if node is None or not node.name:
            continue
        merged = db.flatten(node)
        desc = merged.get_child("Description")
        if desc is None:
            continue  # structural base, not a player/skill object
        gc_type = gc_type_for(skills_root, fp, prefix)
        source_file = os.path.relpath(fp, skills_root).replace(os.sep, "/")
        rows.append(skill_row(gc_type, desc, source_file))
    return rows


def rebuild_skills_table(conn: sqlite3.Connection, skills_root: str, prefix: str = "skills") -> int:
    """Drop and rebuild the ``skills`` table from ``.gc`` content. Returns the
    number of rows written. Caller owns commit/transaction boundaries."""
    rows = collect_skill_rows(skills_root, prefix)
    conn.execute("DROP TABLE IF EXISTS skills")
    conn.execute(_CREATE_SKILLS)
    conn.executemany(_INSERT_SKILL, rows)
    return len(rows)
