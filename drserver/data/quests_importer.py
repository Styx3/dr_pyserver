"""Rebuild the ``quests`` content table from ``.gc`` ground truth.

Second domain of the ``.gc`` -> SQLite importer (after ``skills``; see
``gc_parser`` / ``gc_database`` / ``skills_importer``). The legacy ``quests``
table inherited from the older C# build has **fabricated** columns: ``zone``,
``faction``, ``status`` and ``quest_type`` are constant across all 1293 rows
(``''/''/'available'/'quest'``), and ``level``/``reward_experience``/
``reward_gold`` have no counterpart in the client's ``.gc``. Nothing in the
Python server reads this table (only the runtime ``completed_quests`` /
``quest_objectives`` tables), so we replace it with a faithful, regenerable
mirror of the real quest content.

Where the quests actually live (empirically derived 2026-06-04):
    * ``extracter/quests/`` holds only the **base classes** (``Quest``,
      ``QuestToken``, ``QuestObsolete`` …), reward-token bases and ``UIDesc``.
      It is the class library, *not* the quest instances.
    * The 1289 real **quest instances** live under ``extracter/world/**/quest/``
      (+ 5 tutorial quests under ``quests/base/HelperNoobosaur/``). Each
      ``extends`` a ``quests.base.*`` class, e.g.
      ``Q01_a1 extends quests.base.QuestObsolete``.

Selection criterion: a node is a quest iff, after ``extends`` inheritance is
resolved, it has a ``Description`` child **and** its extends chain roots at the
base ``Quest`` class. This cleanly separates real quests from the item/token
*definitions* that also live under ``quest/`` dirs (those root at item classes
like ``MythicBody`` / ``Unique2HWeapon``) and from item-generator tables (no
``Description``). NB: the ``quest/token/`` "exchange King's Coin for loot"
entries *are* genuine token-trade quests (they root at ``Quest``) and are kept.

Collision note: quest stems collide heavily across nested dirs (``MythicBody``
appears in ``token/``, ``token/fi/``, ``token/ma/``, ``token/rg/`` and per
dungeon). We load the tree with ``dotted_prefix`` so ``extends`` resolves by
full path (``quests.base.token.fi.MythicBody``) instead of a colliding stem.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .gc_database import GCDatabase
from .gc_parser import GCNode, parse_file

# Columns mirror the real flattened ``Description`` fields (snake_case). The
# fabricated legacy columns (zone, faction, status, quest_type, hash, level,
# reward_experience, reward_gold) are dropped. ``gc_type`` is the dotted content
# path (== the id other quests reference via FollowupQuest/RequiredQuest1);
# ``source_file`` + ``raw_json`` preserve full provenance / lossless capture
# (including the objective + drop-trigger sub-tree, which lives only in JSON).
_CREATE_QUESTS = """
CREATE TABLE quests (
    gc_type                   TEXT PRIMARY KEY,
    name                      TEXT,
    base_class                TEXT,
    label                     TEXT,
    summary                   TEXT,
    description               TEXT,
    reward_text               TEXT,
    npc                       TEXT,
    npc2                      TEXT,
    npc3                      TEXT,
    min_level                 INTEGER,
    max_level                 INTEGER,
    required_quest            TEXT,
    followup_quest            TEXT,
    ui_zone_info              TEXT,
    required_class            TEXT,
    token_reward              INTEGER,
    cash_reward               REAL,
    grant_xp_buff             INTEGER,
    repeatable                INTEGER,
    temporary                 INTEGER,
    auto_accept_on_query      INTEGER,
    permanent_abandon         INTEGER,
    min_repeat_seconds        INTEGER,
    reward_item_generator     TEXT,
    reward_icon_generator     TEXT,
    reward_item_description   TEXT,
    num_reward_items          INTEGER,
    reward_items_soulbound    INTEGER,
    reward_items_dropped      INTEGER,
    on_accept_item_generator  TEXT,
    on_accept_items_soulbound INTEGER,
    on_accept_items_nosell    INTEGER,
    mod_to_add_on_complete    TEXT,
    objective_count           INTEGER,
    objective_kinds           TEXT,
    source_file               TEXT,
    raw_json                  TEXT
)
"""

_INSERT_QUEST = """
INSERT OR REPLACE INTO quests (
    gc_type, name, base_class, label, summary, description, reward_text, npc,
    npc2, npc3, min_level, max_level, required_quest, followup_quest,
    ui_zone_info, required_class, token_reward, cash_reward, grant_xp_buff,
    repeatable, temporary, auto_accept_on_query, permanent_abandon,
    min_repeat_seconds, reward_item_generator, reward_icon_generator,
    reward_item_description, num_reward_items, reward_items_soulbound,
    reward_items_dropped, on_accept_item_generator, on_accept_items_soulbound,
    on_accept_items_nosell, mod_to_add_on_complete, objective_count,
    objective_kinds, source_file, raw_json
) VALUES (
    :gc_type, :name, :base_class, :label, :summary, :description, :reward_text,
    :npc, :npc2, :npc3, :min_level, :max_level, :required_quest,
    :followup_quest, :ui_zone_info, :required_class, :token_reward,
    :cash_reward, :grant_xp_buff, :repeatable, :temporary,
    :auto_accept_on_query, :permanent_abandon, :min_repeat_seconds,
    :reward_item_generator, :reward_icon_generator, :reward_item_description,
    :num_reward_items, :reward_items_soulbound, :reward_items_dropped,
    :on_accept_item_generator, :on_accept_items_soulbound,
    :on_accept_items_nosell, :mod_to_add_on_complete, :objective_count,
    :objective_kinds, :source_file, :raw_json
)
"""

# The base class every real quest's extends chain terminates at.
_QUEST_ROOT = "Quest"


def gc_type_for(extracter_root: str, file_path: str) -> str:
    """Dotted content path for a quest ``.gc`` relative to the extracter root,
    e.g. ``world/dungeon02/quest/Q01_a1.gc`` -> ``world.dungeon02.quest.Q01_a1``.
    This matches the ids quests use to reference each other."""
    rel = os.path.splitext(os.path.relpath(file_path, extracter_root))[0]
    return rel.replace(os.sep, ".").replace("/", ".")


def _gc_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirs, names in os.walk(root):
        for nm in names:
            if nm.lower().endswith(".gc"):
                out.append(os.path.join(dirpath, nm))
    out.sort()
    return out


def world_quest_dirs(extracter_root: str) -> list[str]:
    """Every ``world/**/quest`` directory (where quest instances live)."""
    world = os.path.join(extracter_root, "world")
    out: list[str] = []
    if os.path.isdir(world):
        for dirpath, _dirs, _names in os.walk(world):
            if os.path.basename(dirpath) == "quest":
                out.append(dirpath)
    out.sort()
    return out


def build_quest_db(extracter_root: str) -> GCDatabase:
    """Load the quest class library + all world quest dirs into one registry,
    each registered by full dotted path so ``extends`` resolves collision-free."""
    db = GCDatabase()
    quests_dir = os.path.join(extracter_root, "quests")
    if os.path.isdir(quests_dir):
        db.load_tree(quests_dir, dotted_prefix="quests")
    for qdir in world_quest_dirs(extracter_root):
        prefix = os.path.relpath(qdir, extracter_root).replace(os.sep, ".")
        db.load_tree(qdir, dotted_prefix=prefix)
    return db


def _candidate_files(extracter_root: str) -> list[str]:
    """Quest-instance candidates: every ``.gc`` under ``world/**/quest/`` plus
    the named tutorial quests under ``quests/base/HelperNoobosaur/``."""
    files: list[str] = []
    for qdir in world_quest_dirs(extracter_root):
        files.extend(_gc_files(qdir))
    helper = os.path.join(extracter_root, "quests", "base", "HelperNoobosaur")
    if os.path.isdir(helper):
        files.extend(_gc_files(helper))
    files.sort()
    return files


def _root_extends(db: GCDatabase, node: GCNode) -> str:
    """Name of the class a node's ``extends`` chain terminates at (collision-free
    via the full-path registry)."""
    seen: set[str] = set()
    cur: GCNode | None = node
    while cur is not None and cur.extends and cur.extends.lower() not in seen:
        seen.add(cur.extends.lower())
        nxt = db.resolve(cur.extends)
        if nxt is None:
            return cur.extends.rsplit(".", 1)[-1]
        cur = nxt
    return cur.name if cur is not None else "?"


def _node_to_dict(node: GCNode) -> dict:
    """Lossless recursive serialization of a (flattened) node for ``raw_json``."""
    return {
        "name": node.name,
        "extends": node.extends,
        "is_static": node.is_static,
        "is_anonymous": node.is_anonymous,
        "properties": dict(node.properties),
        "children": {k: _node_to_dict(v) for k, v in node.children.items()},
        "anonymous_children": [_node_to_dict(c) for c in node.anonymous_children],
    }


def _objectives(db: GCDatabase, merged: GCNode) -> list[GCNode]:
    """Objective sub-blocks of a quest: any named child (besides Description) or
    anonymous child whose extends chain roots at a ``*Objective`` class."""
    out: list[GCNode] = []
    for name, child in merged.children.items():
        if name.lower() == "description":
            continue
        if _root_extends(db, child).endswith("Objective"):
            out.append(child)
    for child in merged.anonymous_children:
        if _root_extends(db, child).endswith("Objective"):
            out.append(child)
    return out


def _i(desc: GCNode, key: str) -> int | None:
    return desc.get_int(key) if desc.has_property(key) else None


def _f(desc: GCNode, key: str) -> float | None:
    return desc.get_float(key) if desc.has_property(key) else None


def _b(desc: GCNode, key: str) -> int | None:
    return (1 if desc.get_bool(key) else 0) if desc.has_property(key) else None


def quest_row(db: GCDatabase, gc_type: str, node: GCNode, merged: GCNode,
              desc: GCNode, source_file: str) -> dict:
    """Map a resolved quest node to a ``quests`` table row dict."""
    objs = _objectives(db, merged)
    kinds = sorted({_root_extends(db, o) for o in objs})
    base_class = (node.extends or "").rsplit(".", 1)[-1] or None
    return {
        "gc_type": gc_type,
        "name": gc_type.rsplit(".", 1)[-1],
        "base_class": base_class,
        "label": desc.get_string("Label") or None,
        "summary": desc.get_string("Summary") or None,
        "description": desc.get_string("Description") or None,
        "reward_text": desc.get_string("RewardText") or None,
        "npc": desc.get_string("NPC") or None,
        "npc2": desc.get_string("NPC2") or None,
        "npc3": desc.get_string("NPC3") or None,
        "min_level": _i(desc, "MinLevel"),
        "max_level": _i(desc, "MaxLevel"),
        "required_quest": desc.get_string("RequiredQuest1") or None,
        "followup_quest": desc.get_string("FollowupQuest") or None,
        "ui_zone_info": desc.get_string("UIZoneInfo") or None,
        "required_class": desc.get_string("RequiredClass") or None,
        "token_reward": _i(desc, "TokenReward"),
        "cash_reward": _f(desc, "CashReward"),
        "grant_xp_buff": _b(desc, "GrantXPBuff"),
        "repeatable": _b(desc, "Repeatable"),
        "temporary": _b(desc, "Temporary"),
        "auto_accept_on_query": _b(desc, "AutoAcceptOnQuery"),
        "permanent_abandon": _b(desc, "PermanentAbandon"),
        "min_repeat_seconds": _i(desc, "MinRepeatTimeSeconds"),
        "reward_item_generator": desc.get_string("RewardItemGenerator") or None,
        "reward_icon_generator": desc.get_string("RewardIconGenerator") or None,
        "reward_item_description": desc.get_string("RewardItemDescription") or None,
        "num_reward_items": _i(desc, "NumRewardItems"),
        "reward_items_soulbound": _b(desc, "RewardItemsAreSoulBound"),
        "reward_items_dropped": _b(desc, "RewardItemsAreDropped"),
        "on_accept_item_generator": desc.get_string("OnAcceptItemGenerator") or None,
        "on_accept_items_soulbound": _b(desc, "OnAcceptItemsAreSoulBound"),
        "on_accept_items_nosell": _b(desc, "OnAcceptItemsAreNoSell"),
        "mod_to_add_on_complete": desc.get_string("ModToAddOnComplete") or None,
        "objective_count": len(objs),
        "objective_kinds": ",".join(kinds) if kinds else None,
        "source_file": source_file,
        "raw_json": json.dumps(_node_to_dict(merged), sort_keys=True),
    }


def collect_quest_rows(extracter_root: str) -> list[dict]:
    """Parse + inheritance-resolve every quest candidate; return rows for those
    that are real quests (have a Description and root at ``Quest``). Pure (no DB
    writes) so it is easy to dry-run."""
    db = build_quest_db(extracter_root)
    rows: list[dict] = []
    for fp in _candidate_files(extracter_root):
        node = parse_file(fp)
        if node is None or not node.name:
            continue
        merged = db.flatten(node)
        desc = merged.get_child("Description")
        if desc is None:
            continue  # item-generator table, not a quest
        if _root_extends(db, node) != _QUEST_ROOT:
            continue  # item/token *definition* under a quest/ dir
        gc_type = gc_type_for(extracter_root, fp)
        source_file = os.path.relpath(fp, extracter_root).replace(os.sep, "/")
        rows.append(quest_row(db, gc_type, node, merged, desc, source_file))
    return rows


def rebuild_quests_table(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Drop and rebuild the ``quests`` table from ``.gc`` content. Returns the
    number of rows written. Caller owns commit/transaction boundaries."""
    rows = collect_quest_rows(extracter_root)
    conn.execute("DROP TABLE IF EXISTS quests")
    conn.execute(_CREATE_QUESTS)
    conn.executemany(_INSERT_QUEST, rows)
    return len(rows)
