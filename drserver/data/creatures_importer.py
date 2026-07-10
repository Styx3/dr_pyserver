"""Rebuild the ``creatures`` content table from ``.gc`` ground truth.

Third domain of the ``.gc`` -> SQLite importer (after ``skills`` and ``quests``;
see ``gc_parser`` / ``gc_database`` / ``skills_importer`` / ``quests_importer``).

The legacy ``creatures`` table inherited from the older C# build has **fabricated
absolute** columns: ``hit_points``, ``mana_points``, ``base_damage`` and the two
``*_packet`` mirrors. There is **no counterpart for any absolute combat number in
the client ``.gc``** — empirically, across all 1037 creature files there is not a
single ``HitPoints``/``BaseDamage``/``ManaPoints`` field, only the *multipliers*
(``MaxHealth``/``DamageMod``/``AttackRating``/``CriticalChance``) layered over a
difficulty tier (``CreatureDifficulty`` = GRUNT/CHAMPION/HERO/…). The absolute HP
is computed *client-side* from the spawn level × these multipliers; the server
mirrors it via :mod:`drserver.managers.monster_health` (the client-validated
Fixed32 ``MonsterHealth`` curve), **not** from this table. The emulator also
fabricated ``*.boss`` element variants that do not exist in the ``.gc`` (e.g.
``skull_dog.divine.boss`` — ``Divine.gc`` only defines Grunt/Champion/Hero).

So we replace the table with a faithful, regenerable mirror of the real creature
content: the difficulty tier, the multipliers, resists, movement/collision,
treasure generators, generated-name pools, visual + bounding box, and a lossless
``raw_json`` of the flattened node. The only consumer
(:class:`drserver.managers.monsters.MonsterManager`) reads ``gc_type``,
``creature_difficulty``, ``label``, ``behaviour_type`` and ``treasure_gen*`` /
``treasure_count*`` — all real fields that survive here.

Where the creatures live (empirically derived 2026-06-05):
    * ``extracter/creatures/`` holds both the **abstract base library**
      (``creatures.base.UnitMelee_Champion`` …) and the **concrete instances**,
      the latter as **nested child nodes** inside element files, e.g.
      ``creatures/amphibs/skull_dog/Divine.gc`` defines ``Divine.Champion`` →
      gc_type ``creatures.amphibs.skull_dog.divine.champion``.

Selection criterion: a node is a spawnable creature iff its ``extends`` chain
roots at the native ``StockUnit`` base **and** it is not one of the
``creatures.base.*`` abstract library classes. This cleanly separates the ~1473
concrete creatures from the bases, visuals, sounds, animations and weapon/skill
manipulator definitions that share the tree. ``gc_type`` is the lowercased dotted
content path (matching the existing table + the case-insensitive lookups in
``MonsterManager``).
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3

from .gc_database import GCDatabase
from .gc_parser import GCNode, parse_file

# The native base every concrete creature's ``extends`` chain terminates at.
_CREATURE_ROOT = "StockUnit"

# Schema mirrors the real flattened fields (snake_case). The fabricated legacy
# columns (hit_points, mana_points, base_damage, hit_points_packet,
# mana_points_packet) are dropped — absolute HP/damage do not exist in the ``.gc``
# and are derived at runtime by ``monster_health`` from creature_difficulty.
# ``creature_difficulty`` / ``behaviour_type`` / ``treasure_gen*`` keep their
# legacy names so ``MonsterManager.load`` is a drop-in. ``raw_json`` is the
# lossless capture (full flattened node incl. Behavior/Object/Manipulators).
_CREATE_CREATURES = """
CREATE TABLE creatures (
    gc_type              TEXT PRIMARY KEY,
    name                 TEXT,
    base_class           TEXT,
    label                TEXT,
    creature_difficulty  TEXT,
    creature_family      TEXT,
    creature_element     TEXT,
    behaviour_type       TEXT,
    faction_id           INTEGER,
    difficulty           REAL,
    max_health           REAL,
    max_mana             REAL,
    attack_rating        REAL,
    defense_rating       REAL,
    critical_chance      REAL,
    damage_mod           REAL,
    attack_range         REAL,
    speed_mod            INTEGER,
    size_mod             INTEGER,
    speed                INTEGER,
    walk_speed           INTEGER,
    turn_rate            INTEGER,
    collision_radius     INTEGER,
    collision_priority   INTEGER,
    corpse_linger_time   INTEGER,
    fear_resist          INTEGER,
    stun_resist          INTEGER,
    divine_resist        INTEGER,
    fire_resist          INTEGER,
    ice_resist           INTEGER,
    poison_resist        INTEGER,
    shadow_resist        INTEGER,
    use_generated_name   INTEGER,
    first_names          TEXT,
    last_names           TEXT,
    visual               TEXT,
    bbox_min_x           REAL,
    bbox_min_y           REAL,
    bbox_min_z           REAL,
    bbox_max_x           REAL,
    bbox_max_y           REAL,
    bbox_max_z           REAL,
    treasure_gen1        TEXT,
    treasure_count1      INTEGER,
    treasure_gen2        TEXT,
    treasure_count2      INTEGER,
    treasure_gen3        TEXT,
    treasure_count3      INTEGER,
    treasure_gen4        TEXT,
    treasure_count4      INTEGER,
    source_file          TEXT,
    raw_json             TEXT
)
"""

_CREATURE_COLUMNS = [
    "gc_type", "name", "base_class", "label", "creature_difficulty",
    "creature_family", "creature_element", "behaviour_type", "faction_id",
    "difficulty", "max_health", "max_mana", "attack_rating", "defense_rating",
    "critical_chance", "damage_mod", "attack_range", "speed_mod", "size_mod",
    "speed", "walk_speed", "turn_rate", "collision_radius", "collision_priority",
    "corpse_linger_time", "fear_resist", "stun_resist", "divine_resist",
    "fire_resist", "ice_resist", "poison_resist", "shadow_resist",
    "use_generated_name", "first_names", "last_names", "visual",
    "bbox_min_x", "bbox_min_y", "bbox_min_z", "bbox_max_x", "bbox_max_y",
    "bbox_max_z", "treasure_gen1", "treasure_count1", "treasure_gen2",
    "treasure_count2", "treasure_gen3", "treasure_count3", "treasure_gen4",
    "treasure_count4", "source_file", "raw_json",
]
_INSERT_CREATURE = (
    f"INSERT OR REPLACE INTO creatures ({', '.join(_CREATURE_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _CREATURE_COLUMNS)})"
)


# ── path helpers ──

def creatures_root(extracter_root: str) -> str:
    return os.path.join(extracter_root, "creatures")


def build_creature_db(extracter_root: str) -> GCDatabase:
    """Load the whole ``creatures/`` tree, registered by full dotted path so
    ``extends`` resolves collision-free (stems like ``Champion`` repeat per
    element/family)."""
    db = GCDatabase()
    root = creatures_root(extracter_root)
    if os.path.isdir(root):
        db.load_tree(root, dotted_prefix="creatures")
    return db


def _file_dotted(extracter_root: str, file_path: str) -> str:
    rel = os.path.splitext(os.path.relpath(file_path, extracter_root))[0]
    return rel.replace(os.sep, ".").replace("/", ".")


def _root_extends(db: GCDatabase, node: GCNode) -> str:
    """Name of the class a node's ``extends`` chain terminates at."""
    seen: set[str] = set()
    cur: GCNode | None = node
    while cur is not None and cur.extends and cur.extends.lower() not in seen:
        seen.add(cur.extends.lower())
        nxt = db.resolve(cur.extends)
        if nxt is None:
            return cur.extends.rsplit(".", 1)[-1]
        cur = nxt
    return cur.name if cur is not None else "?"


def _is_creature(db: GCDatabase, gc_type: str, node: GCNode) -> bool:
    """A concrete spawnable creature: its ``extends`` chain roots at the native
    ``StockUnit`` base, and it is not an abstract library/species base.

    Abstract bases live under a ``base`` directory segment — both the top-level
    library (``creatures.base.UnitMelee_Champion``) and per-species bases
    (``creatures.amphibs.skull_dog.base.SkullDogBase_Champion``). Concrete
    instances (``creatures.amphibs.skull_dog.divine.champion``) never sit under a
    ``base`` segment. Excluding any ``base`` segment cleanly drops both."""
    if "base" in gc_type.lower().split("."):
        return False
    return _root_extends(db, node) == _CREATURE_ROOT


# ── value accessors (None when absent) ──

def _s(desc: GCNode | None, key: str) -> str | None:
    if desc is None or not desc.has_property(key):
        return None
    return desc.get_string(key) or None


def _i(desc: GCNode | None, key: str) -> int | None:
    return desc.get_int(key) if desc is not None and desc.has_property(key) else None


def _f(desc: GCNode | None, key: str) -> float | None:
    return desc.get_float(key) if desc is not None and desc.has_property(key) else None


def _b(desc: GCNode | None, key: str) -> int | None:
    return ((1 if desc.get_bool(key) else 0)
            if desc is not None and desc.has_property(key) else None)


def _child_desc(merged: GCNode, child_name: str) -> GCNode | None:
    child = merged.get_child(child_name)
    return child.get_child("Description") if child is not None else None


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


def creature_row(db: GCDatabase, gc_type: str, node: GCNode, merged: GCNode,
                 source_file: str) -> dict:
    """Map a resolved creature node to a ``creatures`` table row dict."""
    desc = merged.get_child("Description")
    beh = _child_desc(merged, "Behavior")
    obj = _child_desc(merged, "Object")
    base_class = (node.extends or "").rsplit(".", 1)[-1] or None
    # Behaviour reference = the (inherited) Behavior block's extends path.
    beh_node = merged.get_child("Behavior")
    behaviour_type = beh_node.extends if beh_node is not None else None

    return {
        "gc_type": gc_type,
        "name": (merged.get_string("Name") or None),
        "base_class": base_class,
        "label": _s(desc, "Label"),
        "creature_difficulty": _s(desc, "CreatureDifficulty"),
        "creature_family": _s(desc, "CreatureFamily"),
        "creature_element": _s(desc, "CreatureElement"),
        "behaviour_type": behaviour_type,
        "faction_id": _i(desc, "FactionID"),
        "difficulty": _f(desc, "Difficulty"),
        "max_health": _f(desc, "MaxHealth"),
        "max_mana": _f(desc, "MaxMana"),
        "attack_rating": _f(desc, "AttackRating"),
        "defense_rating": _f(desc, "DefenseRating"),
        "critical_chance": _f(desc, "CriticalChance"),
        "damage_mod": _f(desc, "DamageMod"),
        "attack_range": _f(desc, "AttackRange"),
        "speed_mod": _i(desc, "SpeedMod"),
        "size_mod": _i(desc, "SizeMod"),
        "speed": _i(desc, "Speed"),
        "walk_speed": _i(desc, "WalkSpeed"),
        "turn_rate": _i(desc, "TurnRate"),
        "collision_radius": _i(desc, "CollisionRadius"),
        "collision_priority": _i(beh, "CollisionPriority"),
        "corpse_linger_time": _i(desc, "CorpseLingerTime"),
        "fear_resist": _i(desc, "FearResist"),
        "stun_resist": _i(desc, "StunResist"),
        "divine_resist": _i(desc, "DivineResist"),
        "fire_resist": _i(desc, "FireResist"),
        "ice_resist": _i(desc, "IceResist"),
        "poison_resist": _i(desc, "PoisonResist"),
        "shadow_resist": _i(desc, "ShadowResist"),
        "use_generated_name": _b(desc, "UseGeneratedName"),
        "first_names": _s(desc, "FirstNames"),
        "last_names": _s(desc, "LastNames"),
        "visual": _s(obj, "Visual"),
        "bbox_min_x": _f(obj, "MinX"),
        "bbox_min_y": _f(obj, "MinY"),
        "bbox_min_z": _f(obj, "MinZ"),
        "bbox_max_x": _f(obj, "MaxX"),
        "bbox_max_y": _f(obj, "MaxY"),
        "bbox_max_z": _f(obj, "MaxZ"),
        "treasure_gen1": _s(desc, "TreasureGenerator"),
        "treasure_count1": _i(desc, "TreasureCount"),
        "treasure_gen2": _s(desc, "TreasureGenerator2"),
        "treasure_count2": _i(desc, "TreasureCount2"),
        "treasure_gen3": _s(desc, "TreasureGenerator3"),
        "treasure_count3": _i(desc, "TreasureCount3"),
        "treasure_gen4": _s(desc, "TreasureGenerator4"),
        "treasure_count4": _i(desc, "TreasureCount4"),
        "source_file": source_file,
        "raw_json": json.dumps(_node_to_dict(merged), sort_keys=True),
    }


def collect_creature_rows(extracter_root: str) -> list[dict]:
    """Parse + inheritance-resolve every creature node; return rows for the
    concrete creatures (root at ``StockUnit``, not a ``creatures.base.*`` base).
    Pure (no DB writes) so it is easy to dry-run. gc_type is lowercased; on a
    lowercase collision the first occurrence (sorted file order) wins."""
    db = build_creature_db(extracter_root)
    root = creatures_root(extracter_root)
    rows: list[dict] = []
    seen: set[str] = set()

    files: list[str] = []
    for dirpath, _dirs, names in os.walk(root):
        for nm in names:
            if nm.lower().endswith(".gc"):
                files.append(os.path.join(dirpath, nm))
    files.sort()

    for fp in files:
        top = parse_file(fp)
        if top is None or not top.name:
            continue
        source_file = os.path.relpath(fp, extracter_root).replace(os.sep, "/")
        file_dotted = _file_dotted(extracter_root, fp)

        stack: list[tuple[str, GCNode]] = [(file_dotted, top)]
        while stack:
            prefix, n = stack.pop()
            if _is_creature(db, prefix, n):
                key = prefix.lower()
                if key not in seen:
                    seen.add(key)
                    merged = db.flatten(n)
                    rows.append(creature_row(db, key, n, merged, source_file))
            for k, child in n.children.items():
                stack.append((f"{prefix}.{k}", child))
    rows.sort(key=lambda r: r["gc_type"])
    return rows


def _build_chassis_db(extracter_root: str) -> GCDatabase:
    """A creature DB that ALSO resolves the top-level ``base/`` chassis and the
    ``npc/`` tree, so a unique boss whose chain leaves ``creatures/`` flattens
    fully (``world.dungeon06.mob.boss`` → ``…wheelerboss.base.Mutant_WheelerBoss_Base``
    → ``base.RangedUnit`` → ``base.StockUnit``; Frump → ``npc.OldMan…``).

    Kept SEPARATE from :func:`build_creature_db` so the canonical creature import
    stays byte-identical. ``creatures/`` is loaded LAST so its stems win any
    bare-stem collision with ``base/``/``npc/`` (full dotted paths resolve
    regardless of load order)."""
    db = GCDatabase()
    for sub, pref in (("base", "base"), ("npc", "npc")):
        p = os.path.join(extracter_root, sub)
        if os.path.isdir(p):
            db.load_tree(p, dotted_prefix=pref)
    root = creatures_root(extracter_root)
    if os.path.isdir(root):
        db.load_tree(root, dotted_prefix="creatures")
    return db


def collect_world_boss_creature_rows(
        extracter_root: str, seen: set[str]) -> list[dict]:
    """Creature rows for dungeon boss MOB ENTITIES that carry their own stats but
    are absent from the normal ``creatures/`` import (bible §14.4).

    A ``world.*.mob.boss`` entity that overrides ``CreatureDifficulty`` IS the
    real creature, but its ``extends`` target is a non-importable chassis
    (``…base.*`` / an ``npc.*`` / unresolvable), so the boss has no ``MonsterData``
    and never spawns (d06 Rotgut, d08, d09, d05 Frump — the 4 still-broken bosses;
    the other 22 self-defining mobs already resolve to a concrete creature). We
    flatten the entity against the chassis DB and key the row by its world dotted
    path; ``dungeon_world_importer`` self-maps the encounter to the same key.

    ``seen`` = the concrete-creature keys already collected; an entity whose
    resolved creature is in ``seen`` is already spawnable and skipped."""
    from .dungeon_world_importer import build_mob_creature_map  # local: avoid cycle

    db = _build_chassis_db(extracter_root)
    mob_map = build_mob_creature_map(extracter_root)
    rows: list[dict] = []
    out_seen: set[str] = set()
    pattern = os.path.join(extracter_root, "world", "*", "mob", "**", "*.gc")
    for fp in sorted(glob.glob(pattern, recursive=True)):
        top = parse_file(fp)
        if top is None or not top.name:
            continue
        source_file = os.path.relpath(fp, extracter_root).replace(os.sep, "/")
        file_dotted = _file_dotted(extracter_root, fp)
        cands = [(file_dotted, top)]
        cands += [(f"{file_dotted}.{c.name}", c) for c in top.children.values()]
        for key, n in cands:
            lk = key.lower()
            if lk in seen or lk in out_seen:
                continue
            own = n.get_child("Description")
            is_boss = lk.endswith(".mob.boss") or (
                own is not None
                and (own.get_string("CreatureDifficulty") or "").upper()
                == "DUNGEON_BOSS")
            resolved = mob_map.get(lk)
            # A non-boss alias that resolves to a concrete creature is already
            # spawnable — use the concrete (no row needed). But a BOSS must be
            # imported even when it resolves to a concrete, so it spawns AS itself
            # with its overridden stats (the wire carries this entity → the client
            # validates against them; bible §14.4). Self-mapped bosses + the
            # base-class / abstract cases all flow through here.
            if (not is_boss and resolved is not None
                    and resolved.lower() in seen):
                continue
            if _root_extends(db, n) != _CREATURE_ROOT:
                continue                       # not a creature chain
            merged = db.flatten(n)
            mdesc = merged.get_child("Description")
            # A self-contained creature needs an HP basis: either a MERGED tier
            # (``CreatureDifficulty`` — Rotgut's per-species base, or a master that
            # inherits HERO from an abstract ``creatures.base.*``) OR a raw numeric
            # ``Difficulty`` override (Z'lash & co. extend bare ``UnitMelee`` with
            # ``Difficulty=5`` and no tier — bible §14.4). The numeric ``Difficulty``
            # is the real multiplier the client validates against, threaded via
            # ``MonsterData.difficulty_value``.
            if mdesc is None or not (mdesc.has_property("CreatureDifficulty")
                                     or mdesc.has_property("Difficulty")):
                continue
            out_seen.add(lk)
            rows.append(creature_row(db, lk, n, merged, source_file))
    return rows


def collect_referenced_base_creature_rows(
        extracter_root: str, seen: set[str]) -> list[dict]:
    """Creature rows for PER-SPECIES base creatures that dungeon content spawns
    directly but the blanket ``base``-segment exclusion drops (bible §14.4).

    Some species have no element variants and are spawned via their per-species
    base (``creatures.humanoid.raythemale.base.melee``, ``…orokchieftain.base.hero``
    — the d08 boss guards + ~86 regular d07/d08 mobs, plus widower/ratsputin/relic
    leaders). These DO root at ``StockUnit`` and carry an inherited tier, so they
    are real creatures — only their ``base`` path segment excluded them. We import
    ONLY the ones actually referenced by the mob→creature map (12 today), keyed by
    their real path, so no abstract library (``creatures.base.*``) or unreferenced
    chassis leaks in. The genuinely-abstract ``creatures.base.unitmelee`` refs that
    some mobs still resolve to are a separate resolution gap (left as-is)."""
    from .dungeon_world_importer import build_mob_creature_map  # local: avoid cycle

    db = _build_chassis_db(extracter_root)
    mob_map = build_mob_creature_map(extracter_root)
    rows: list[dict] = []
    out_seen: set[str] = set()
    for target in sorted(set(mob_map.values())):
        key = target.lower()
        segs = key.split(".")
        if not segs or segs[0] != "creatures" or "base" not in segs[2:]:
            continue                       # only PER-SPECIES base (skip base lib)
        if key in seen or key in out_seen:
            continue
        node = db.resolve(target)
        if node is None or _root_extends(db, node) != _CREATURE_ROOT:
            continue
        out_seen.add(key)
        merged = db.flatten(node)
        rows.append(creature_row(db, key, node, merged, "(referenced base)"))
    return rows


def rebuild_creatures_table(conn: sqlite3.Connection, extracter_root: str) -> int:
    """Drop and rebuild the ``creatures`` table from ``.gc`` content. Returns the
    number of rows written. Caller owns commit/transaction boundaries."""
    rows = collect_creature_rows(extracter_root)
    seen = {r["gc_type"] for r in rows}
    # ★ bible §14.4: also import (a) the unique boss mob ENTITIES (world.*.mob.boss
    # with their own CreatureDifficulty) whose chassis is non-importable, so the
    # d05/06/08/09 dungeon bosses gain MonsterData and finally spawn; and (b) the
    # per-species base creatures dungeon content spawns directly (d08 guards +
    # d07/d08 raythemale/orokchieftain mobs) that the base-segment rule dropped.
    rows += collect_world_boss_creature_rows(extracter_root, seen)
    seen |= {r["gc_type"] for r in rows}
    rows += collect_referenced_base_creature_rows(extracter_root, seen)
    conn.execute("DROP TABLE IF EXISTS creatures")
    conn.execute(_CREATE_CREATURES)
    conn.executemany(_INSERT_CREATURE, rows)
    # ★ bible §14.4: fill creature_manipulators (weapons + skills) for the newly
    # imported bosses/leaders/bases — else they spawn weaponless / skill-less
    # (generic-melee fallback). Additive: the legacy ~1189 rows are untouched.
    from .creature_manipulators_importer import (  # local: avoid import cycle
        import_missing_creature_manipulators)
    added = import_missing_creature_manipulators(conn, extracter_root)
    if added:
        from ..core import log
        log.info(f"[CREATURES] filled {added} manipulator rows for new creatures")
    return len(rows)
