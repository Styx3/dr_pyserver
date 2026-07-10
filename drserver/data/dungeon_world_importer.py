"""Data-driven dungeon spawn importer — parses the client ``*.world`` / ``*.enc``
/ ``mob/*.gc`` content into SQLite so every dungeon spawns *its own* mobs.

WHY THIS EXISTS
---------------
The first port hand-coded **dungeon00 (Dew Valley)** as Python literals: its maze
params, its encounter lists and a fake ``melee0N.rankN`` creature→asset family
map (since removed — see [[project_dungeon_map_alignment]]; dungeon00 is now fully
data-driven like every other dungeon). That convention existed ONLY in dungeon00;
forcing it onto another dungeon emitted an unloadable type. Every other dungeon
names its mobs
differently — e.g. ``world.dungeon02.mob.upper.RaceA.base1``,
``world.dungeon02.mob.boss`` — so forcing the dungeon00 map onto, say,
``dungeon02_level03`` emitted ``world.dungeon02.mob.melee01.rank3``, which the
client cannot load → ``processEntityCreate ERROR: Invalid entity type`` → crash.

The client itself is fully data-driven. Each playable maze level is a
``dungeonNN_levelMM.world`` (``* extends base.world_<tileset>``) carrying the maze
parameters and an ``EncounterTable`` reference. The ``.enc`` (``EncounterTable``)
lists ``EncounterUnit`` blocks whose ``Type`` is the real
``world.<dungeon>.mob.<path>`` asset the client renders. Those mob nodes
(``world/<dungeon>/mob/**/*.gc``) ``extend`` a concrete ``creatures.*`` creature,
which is where the server reads HP/level/difficulty for the synch trailer.

This importer reproduces that chain into tables (see :func:`build_schema`):

* ``dungeon_levels``          — one row per ``*.world`` maze level.
* ``dungeon_encounters``      — flattened ``EncounterUnit`` rows per ``.enc``.
* ``dungeon_room_nodes``      — the ``*.world`` room nodes (``base.StartRoom`` /
  ``linkroomnode`` / ``roomnode``) that pin rooms into the maze and carry the
  warp links between levels. The C# *hardcodes* these (only dungeon00 levels
  01–03 exist as literals); the ``.world`` files carry them for every dungeon, so
  this is strictly more faithful. Consumed by the maze spawner (room placement)
  and the warp-gate/portal builder (Phase C).
* ``static_worlds``           — one row per NON-maze ``*.world`` (the boss
  arenas, ``*_level00`` lobbies, squeakeasy/quest off-shoots, …): the world base
  and its ``EncounterTable``.
* ``static_world_encounters`` — the authored ``base.Encounter<N>`` markers inside
  a static world's ``Entities`` block (position / size / optional per-marker
  ``EncounterType``). These are where the client's designers placed the mobs —
  the static counterpart of the maze tiles' encounter markers.
* (mob→creature resolution is folded into ``dungeon_encounters`` at import time.)

COVERAGE: every ``*.world`` at the extracter root is imported (maze worlds into
``dungeon_levels``+``dungeon_room_nodes``, static worlds into ``static_worlds``+
``static_world_encounters``) except the ``!``-prefixed tileset test scratch
files. The old ``dungeon*_level*`` filename filter silently dropped the whole
``elite01_*`` campaign, ``amazon_dungeon``, the ``epic01_*`` roads and every
``*_boss`` room (those are static, not mazes) — all real warpable zones.

The runtime spawner (:mod:`drserver.managers.dungeon_spawner`) reads these tables,
runs the existing maze generator with the per-dungeon params, and emits the
verbatim ``world.*`` entity type (client-loadable) plus the resolved
``creatures.*`` type (stats). No per-dungeon code, no reinvented names.
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .gc_parser import GCNode, parse_file

# A mob node's ``extends`` chain terminates at a real creature here.
_CREATURE_PREFIX = "creatures."
# Mob asset namespaces live under this prefix.
_WORLD_PREFIX = "world."
_MAX_RESOLVE_HOPS = 8


# ──────────────────────────────────────────────────────────────────────────
# Path / namespace helpers
# ──────────────────────────────────────────────────────────────────────────
def _file_dotted(extracter_root: str, file_path: str) -> str:
    """``…/extracter/world/dungeon02/mob/upper/raceA.gc`` →
    ``world.dungeon02.mob.upper.raceA`` (drops extension, '/'→'.')."""
    rel = os.path.splitext(os.path.relpath(file_path, extracter_root))[0]
    return rel.replace(os.sep, ".").replace("/", ".")


def _tile_prefix_for_base(world_base: str) -> str:
    """Derive the corridor-tile prefix the maze generator appends direction
    codes to, from the world base type. ``base.world_cave_large`` →
    ``cave_large_tileset_``; ``base.world_elmforest`` → ``elmforest_tileset_``.

    The maze cell tile names are ``<prefix><conns>_a`` (e.g.
    ``elmforest_tileset_1n1e_a``), matching the client tile assets.
    """
    base = (world_base or "").strip()
    for marker in ("base.world_", "base.World_", "base.WORLD_"):
        if base.lower().startswith(marker.lower()):
            family = base[len(marker):]
            return f"{family.lower()}_tileset_"
    return "elmforest_tileset_"  # safe default (dungeon00 family)


# ──────────────────────────────────────────────────────────────────────────
# Mob → creature resolution map
# ──────────────────────────────────────────────────────────────────────────
# Stat/component blocks inside a mob node — NOT mob entities, so the recursive
# walk does not descend into them (they'd bloat the map with ModifierDesc /
# MonsterBehavior `extends` that never appear as an encounter ``Type``).
_MOB_BLOCK_NAMES = frozenset({
    "description", "behavior", "object", "modifiers", "manipulators",
    "visuals", "sounds", "animations", "map", "soundenvironment", "entities",
})


def _walk_mob_extends(prefix: str, node: "GCNode", raw: Dict[str, str]) -> None:
    """Register ``prefix → node.extends`` and recurse into nested mob nodes.

    Recursion is needed because some mob files nest entities >1 level deep in a
    single file (``boss_guard.gc``: ``boss_guard { Poison { 01 extends … } }`` →
    ``…mob.boss_guard.poison.01``). The old top+1-child scan dropped those, so
    e.g. dungeon11's 4 poison boss-guards never spawned (bible §14.4)."""
    if node.extends:
        raw[prefix.lower()] = node.extends
    children = list(node.children.values())
    children += [c for c in node.anonymous_children if c.name]
    for child in children:
        if child.name.lower() in _MOB_BLOCK_NAMES:
            continue
        _walk_mob_extends(f"{prefix}.{child.name}", child, raw)


def build_mob_extends_map(extracter_root: str) -> Dict[str, str]:
    """Map every ``world.<dungeon>.mob.<path>`` asset (lowercased) to the single
    type it directly ``extends`` (original case) — the raw, un-chased edge.

    The first hop of the mob→creature chain; also used to pick the *asset* a
    boss-posse variant renders as (e.g. ``…mob.boss_posse.RattleTooth`` extends
    ``world.dungeon00.mob.boss``, the visual-bearing boss asset). Walks the full
    node tree so deeply-nested guard entities resolve."""
    raw: Dict[str, str] = {}
    pattern = os.path.join(extracter_root, "world", "*", "mob", "**", "*.gc")
    for fp in glob.glob(pattern, recursive=True):
        node = parse_file(fp)
        if node is None:
            continue
        _walk_mob_extends(_file_dotted(extracter_root, fp), node, raw)
    return raw


def build_mob_creature_map(extracter_root: str) -> Dict[str, str]:
    """Map every ``world.<dungeon>.mob.<path>`` asset type to the concrete
    ``creatures.*`` it ultimately ``extends``.

    Two shapes occur:
      * container file (``raceA.gc``): top node ``raceA`` with named children
        ``base1 extends creatures.…`` → ``world.….mob.upper.raceA.base1``.
      * direct file (``boss.gc``): top node ``boss extends creatures.…`` →
        ``world.….mob.boss``.

    Intermediate ``extends`` that point at another ``world.*`` mob are chased
    (mob templates inherit from siblings) up to a small hop cap. Keys are stored
    lowercased for case-insensitive lookup; values are the original creature
    case.
    """
    raw = build_mob_extends_map(extracter_root)

    resolved: Dict[str, str] = {}
    for mob_type in raw:
        target = raw[mob_type]
        for _ in range(_MAX_RESOLVE_HOPS):
            if target.lower().startswith(_CREATURE_PREFIX):
                resolved[mob_type] = target
                break
            nxt = raw.get(target.lower())
            if nxt is None:
                break
            target = nxt

    # ★ bible §14.4: a mob entity whose ``extends`` target is NOT a spawnable
    # concrete creature IS (often) its own creature — map it to ITSELF so the
    # encounter points at the entity (imported by
    # ``creatures_importer.collect_world_boss_creature_rows``). Three cases:
    #   • resolution fails entirely (None) — e.g. Frump → ``npc.OldMan`` ;
    #   • resolution lands on the ABSTRACT library (``creatures.base.*``) — the
    #     ~85 regular master/quest mobs that inherit a tier from it (it has no
    #     identity, so the entity is the spawnable form) ;
    #   • resolution lands on a per-species base AND the entity OWNS its stats
    #     (a boss override, e.g. Rotgut) — prefer the entity over the imported
    #     base so its ``DUNGEON_BOSS``/``Difficulty`` win.
    # A per-species base WITHOUT an own override is left pointing at the base
    # (imported by ``collect_referenced_base_creature_rows``). Remapping to an
    # un-imported entity is safe: the spawner skips a creature it can't load.
    boss_types = _boss_mob_types(extracter_root)
    for key in list(raw):
        # A BOSS must spawn AS its named entity — the wire sends this entity, so
        # the client loads its overridden Difficulty (Sissirat 11, Abaddon 25,
        # Rotgut 25, …) and the server's HP has to come from the SAME entity, not
        # the generic concrete it extends (else the entity-synch HP mismatches).
        # Imported by ``collect_world_boss_creature_rows``. Scoped to bosses so
        # ordinary leaders/masters keep using their (working) concrete creature.
        if key in boss_types:
            resolved[key] = key
            continue
        cur = resolved.get(key)
        if cur is None:
            resolved[key] = key
            continue
        segs = cur.lower().split(".")
        # A thin alias whose chain only reaches the ABSTRACT library
        # (``creatures.base.*`` has no identity) — spawn the entity instead.
        if len(segs) >= 2 and segs[0] == "creatures" and segs[1] == "base":
            resolved[key] = key
    return resolved


def _boss_mob_types(extracter_root: str) -> set:
    """Lowercased ``world.*.mob.*`` keys that are dungeon BOSSES — the top
    ``boss`` node of a ``…/mob/boss.gc`` (path ends ``.mob.boss``) or any node
    whose own ``Description`` sets ``CreatureDifficulty = DUNGEON_BOSS``. These
    are force-spawned as their named entity (with their override ``Difficulty``);
    see :func:`build_mob_creature_map`."""
    out: set = set()
    pattern = os.path.join(extracter_root, "world", "*", "mob", "**", "*.gc")
    for fp in glob.glob(pattern, recursive=True):
        node = parse_file(fp)
        if node is None:
            continue
        top = _file_dotted(extracter_root, fp)
        cands = [(top, node)]
        cands += [(f"{top}.{c.name}", c) for c in node.children.values()]
        for key, n in cands:
            lk = key.lower()
            d = n.get_child("Description")
            is_boss_diff = (d is not None
                            and (d.get_string("CreatureDifficulty") or "").upper()
                            == "DUNGEON_BOSS")
            if lk.endswith(".mob.boss") or is_boss_diff:
                out.add(lk)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Named entity placements (boss-fight posses) — RoomPlaceholderEntity resolution
# ──────────────────────────────────────────────────────────────────────────
# A boss ``*.world`` places its fight as direct named entities (NOT
# ``base.Encounter`` markers), e.g.::
#
#     * extends world.dungeon00.data.BossFightNCI01.RattleTooth { Position=…; }
#
# Those types are ``RoomPlaceholderEntity`` blocks carrying a ``WorldEntityTable``
# that draws a ``WorldEntity`` (a ``WorldEntityGeneratorTable`` →
# ``SingleWorldEntityGenerator``). The chain is::
#
#   world.dungeon00.data.BossFightNCI01.RattleTooth   (placeholder)
#     → WorldEntityTable world.dungeon00.mob.boss_posse_table.RattleTooth
#     → WorldEntity      world.dungeon00.mob.boss_posse.RattleTooth
#     → extends          world.dungeon00.mob.boss   (the rendered asset)
#     → extends          creatures.whiskers.broodling.Basic.Champion  (stats)
#
# These functions resolve that chain so the spawner can place the actual boss +
# posse (was dropped entirely — only ``base.Encounter`` markers were imported).
def _first_world_entity(node: GCNode) -> Optional[str]:
    """The ``WorldEntity`` a generator-table entry draws (its first
    ``SingleWorldEntityGenerator`` child, or an inline ``WorldEntity``)."""
    for sub in node.anonymous_children:
        we = sub.get_string("WorldEntity")
        if we:
            return we
    return node.get_string("WorldEntity") or None


def build_world_entity_generator_map(extracter_root: str) -> Dict[str, str]:
    """Map each ``WorldEntityGeneratorTable`` entry (lowercased dotted) to the
    ``WorldEntity`` it generates. Generator tables live under ``world/*/mob/``."""
    out: Dict[str, str] = {}
    pattern = os.path.join(extracter_root, "world", "*", "mob", "**", "*.gc")
    for fp in glob.glob(pattern, recursive=True):
        node = parse_file(fp)
        if node is None:
            continue
        top = _file_dotted(extracter_root, fp)
        we = _first_world_entity(node)
        if we:
            out[top.lower()] = we
        for child in node.children.values():
            cwe = _first_world_entity(child)
            if cwe:
                out[f"{top}.{child.name}".lower()] = cwe
    return out


def _collect_world_entity_tables(node: GCNode, dotted: str,
                                 out: Dict[str, str]) -> None:
    wet = node.get_string("WorldEntityTable")
    if wet:
        out[dotted.lower()] = wet
    for child in node.children.values():
        _collect_world_entity_tables(child, f"{dotted}.{child.name}", out)


def build_placeholder_table_map(extracter_root: str) -> Dict[str, str]:
    """Map each ``RoomPlaceholderEntity`` placement type (lowercased dotted, e.g.
    ``world.dungeon00.data.bossfightnci01.rattletooth``) to its
    ``WorldEntityTable``. Placeholders live under ``world/*/data/``."""
    out: Dict[str, str] = {}
    pattern = os.path.join(extracter_root, "world", "*", "data", "**", "*.gc")
    for fp in glob.glob(pattern, recursive=True):
        node = parse_file(fp)
        if node is None:
            continue
        top = _file_dotted(extracter_root, fp)
        _collect_world_entity_tables(node, top, out)
    return out


@dataclass
class PlacementResolver:
    """Resolves a ``*.world`` entity placement type to a spawnable
    ``(entity_gc_type, creature_gc_type)`` pair, or ``None`` when the type is not
    a creature (interactives/markers are handled elsewhere)."""
    mob_map: Dict[str, str]          # world.*.mob.* (lower) -> creatures.*
    mob_extends: Dict[str, str]      # world.*.mob.* (lower) -> direct extends
    placeholder_map: Dict[str, str]  # world.*.data.* (lower) -> WorldEntityTable
    wegen_map: Dict[str, str]        # generator entry (lower) -> WorldEntity

    @classmethod
    def build(cls, extracter_root: str,
              mob_map: Optional[Dict[str, str]] = None) -> "PlacementResolver":
        return cls(
            mob_map=mob_map if mob_map is not None
            else build_mob_creature_map(extracter_root),
            mob_extends=build_mob_extends_map(extracter_root),
            placeholder_map=build_placeholder_table_map(extracter_root),
            wegen_map=build_world_entity_generator_map(extracter_root),
        )

    def _resolve_world_entity(self, we_type: str) -> Optional[Tuple[str, str]]:
        we = we_type.lower()
        creature = self.mob_map.get(we)
        if creature is None:
            return (we_type, we_type) if we.startswith(_CREATURE_PREFIX) else None
        # The rendered asset is what the WorldEntity directly extends (the
        # visual/size-bearing mob asset, e.g. world.dungeon00.mob.boss), as long
        # as it's a client-loadable namespace; otherwise fall back to the
        # WorldEntity itself.
        entity = self.mob_extends.get(we, we_type)
        el = entity.lower()
        if not (el.startswith(_WORLD_PREFIX) or el.startswith(_CREATURE_PREFIX)):
            entity = we_type
        return (entity, creature)

    def resolve(self, type_path: str) -> Optional[Tuple[str, str]]:
        if not type_path:
            return None
        p = type_path.lower()
        table = self.placeholder_map.get(p)
        if table:
            we = self.wegen_map.get(table.lower())
            if we:
                return self._resolve_world_entity(we)
        direct = self._resolve_world_entity(type_path)
        if direct is not None:
            return direct
        if p.startswith(_CREATURE_PREFIX):
            return (type_path, type_path)
        return None


# ──────────────────────────────────────────────────────────────────────────
# Encounter (.enc) parsing
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class EncounterUnitRow:
    enc_table: str          # lowercased .enc dotted name
    group_idx: int
    unit_idx: int
    entity_gc_type: str     # world.* asset, original case (wire)
    creature_gc_type: str   # creatures.* lowercased (stats lookup)
    chance: float
    difficulty: float


def _enc_file_for(extracter_root: str, enc_dotted: str) -> Optional[str]:
    """``world.dungeon02.enc.level03_encounter`` →
    ``…/extracter/world/dungeon02/enc/level03_encounter.gc`` (case-tolerant)."""
    parts = enc_dotted.split(".")
    rel = os.path.join(*parts) + ".gc"
    direct = os.path.join(extracter_root, rel)
    if os.path.isfile(direct):
        return direct
    # Case-insensitive fallback (the .enc refs use Dungeon02 / dungeon02 mixed).
    # Recursive: room-node tables live in subdirs (world/*/enc/base/…).
    lower_target = rel.lower()
    for fp in glob.glob(os.path.join(extracter_root, "world", "*", "enc",
                                     "**", "*.gc"), recursive=True):
        if os.path.relpath(fp, extracter_root).lower() == lower_target:
            return fp
    return None


def parse_encounter_table(
    extracter_root: str, enc_dotted: str, mob_map: Dict[str, str]
) -> List[EncounterUnitRow]:
    """Parse one ``.enc`` ``EncounterTable`` into flat unit rows.

    Each anonymous ``Encounter`` block becomes a ``group_idx``; each
    ``EncounterUnit`` inside it a ``unit_idx`` carrying its ``Type`` (resolved to
    a creature via ``mob_map``) and ``Difficulty`` plus the group ``Chance``.
    Units whose ``Type`` resolves to no creature are dropped (no stats → unsafe
    to spawn), but a valid ``world.*`` asset with a known creature is kept.
    """
    fp = _enc_file_for(extracter_root, enc_dotted)
    if fp is None:
        return []
    node = parse_file(fp)
    if node is None:
        return []

    rows: List[EncounterUnitRow] = []
    enc_key = enc_dotted.lower()
    for group_idx, enc in enumerate(node.anonymous_children):
        chance = enc.get_float("Chance", 1.0)
        unit_idx = 0
        for unit in enc.anonymous_children:
            entity = unit.get_string("Type")
            if not entity:
                continue
            creature = mob_map.get(entity.lower())
            if creature is None:
                # The Type may itself be a creature (rare) — accept that.
                if entity.lower().startswith(_CREATURE_PREFIX):
                    creature = entity
                else:
                    continue
            rows.append(EncounterUnitRow(
                enc_table=enc_key, group_idx=group_idx, unit_idx=unit_idx,
                entity_gc_type=entity, creature_gc_type=creature.lower(),
                chance=chance, difficulty=unit.get_float("Difficulty", 0.0),
            ))
            unit_idx += 1
    return rows


# ──────────────────────────────────────────────────────────────────────────
# World (.world) parsing
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class LevelRow:
    zone_name: str
    world_base: str
    tile_prefix: str
    encounter_table: str            # lower
    leader_encounter: str           # lower, or ""
    maze_width: int
    maze_height: int
    maze_randomness: int
    maze_sparseness: int
    maze_dead_end_removal_chance: int


@dataclass
class RoomNodeRow:
    """One ``*.world`` room node (entry / warp-link / encounter room).

    ``source_index`` is the node's position among the level's TileSet-bearing
    children in file order — it matches the order the maze generator's
    ``add_room_node`` is called, so a placed room can be traced back to its
    ``encounter_type`` / warp link.
    """
    zone_name: str
    source_index: int
    node_kind: str          # extends discriminator: startroom / linkroomnode / roomnode
    tile_set: str
    grid_x: Optional[int]   # None ⇒ "any column" (maze picks)
    grid_y: Optional[int]   # None ⇒ "any row"
    chance: int
    encounter_type: str     # lower, or ""
    link_to_zone: str       # warp target zone, or ""
    link_to_spawn: str      # warp target spawn-point, or ""
    spawn_name: str         # this node's own spawn-point name, or ""
    # ``EncounterWorldEntityTable`` — a ``WorldEntityGeneratorTable`` the room
    # spawns alongside its encounter (elite01 uses these for the stage
    # teleporters). Imported for data-completeness; placement not yet wired.
    encounter_we_table: str = ""


# Cache of the base room-node grid pins, keyed by the dotted path BELOW
# ``world.base.room.`` (e.g. ``tier1.core.mainentrance`` → (2, 4)). Built once
# per extracter root from ``world/base/room/Tier{1,2,3}.gc``.
_base_room_grids: Dict[str, Dict[str, Tuple[Optional[int], Optional[int]]]] = {}


def _load_base_room_grids(extracter_root: str
                          ) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    """Grid pins inherited from the base room classes (``world.base.room.*``).

    A level's entrance/exit/oneoff room nodes ``extends world.base.room.TierN.
    <Section>.<Name>`` and inherit that class's ``GridX``/``GridY`` rather than
    declaring them inline (e.g. ``Tier1.Core.MainEntrance`` pins (2,4), ``Exit``
    pins GridY=0). Reading only the ``.world`` instance missed these, so the maze
    free-placed the entrance/exit and diverged from the client (live-confirmed via
    x64dbg, 2026-06-09: the client's placed cells exactly match these pins). Maps
    the lowercased ``TierN.Section.Name`` path → (GridX, GridY); either may be None
    (e.g. Exit pins only the row)."""
    if extracter_root in _base_room_grids:
        return _base_room_grids[extracter_root]
    out: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    for tier in ("Tier1", "Tier2", "Tier3"):
        fp = os.path.join(extracter_root, "world", "base", "room", f"{tier}.gc")
        root = parse_file(fp) if os.path.isfile(fp) else None
        if root is None:
            continue
        for section in root.children.values():
            for room in section.children.values():
                gx = room.get_int("GridX") if room.has_property("GridX") else None
                gy = room.get_int("GridY") if room.has_property("GridY") else None
                if gx is None and gy is None:
                    continue
                key = f"{tier}.{section.name}.{room.name}".lower()
                out[key] = (gx, gy)
    _base_room_grids[extracter_root] = out
    return out


def parse_room_nodes(extracter_root: str, fp: str, zone_name: str
                     ) -> List[RoomNodeRow]:
    """Extract the room nodes from a ``dungeonNN_levelMM.world`` maze level.

    Walks the world node's anonymous children (``* extends base.StartRoom`` /
    ``linkroomnode`` / ``roomnode``); every block carrying a ``TileSet`` becomes a
    row, numbered in file order to match the maze generator's ``add_room_node``
    call sequence. ``GridX``/``GridY`` are taken from the node, falling back to the
    base room class it ``extends`` (:func:`_load_base_room_grids`) — the
    entrance/exit/oneoff pins live on those base classes, not the instance.
    Returns ``[]`` for non-maze worlds.
    """
    node = parse_file(fp)
    if node is None or not node.has_property("MazeWidth"):
        return []

    base_grids = _load_base_room_grids(extracter_root)
    rows: List[RoomNodeRow] = []
    source_index = 0
    for child in node.anonymous_children:
        tile_set = child.get_string("TileSet")
        if not tile_set:
            continue
        kind = (child.extends or "").split(".")[-1].lower()
        grid_x = child.get_int("GridX") if child.has_property("GridX") else None
        grid_y = child.get_int("GridY") if child.has_property("GridY") else None
        # Inherit unset coords from the base room class (world.base.room.TierN.…).
        extends = (child.extends or "")
        if extends.lower().startswith("world.base.room."):
            base = base_grids.get(extends[len("world.base.room."):].lower())
            if base is not None:
                if grid_x is None:
                    grid_x = base[0]
                if grid_y is None:
                    grid_y = base[1]
        chance = child.get_int("Chance", 100) if child.has_property("Chance") else 100
        rows.append(RoomNodeRow(
            zone_name=zone_name,
            source_index=source_index,
            node_kind=kind,
            tile_set=tile_set,
            grid_x=grid_x,
            grid_y=grid_y,
            chance=chance,
            encounter_type=child.get_string("EncounterType").lower(),
            link_to_zone=child.get_string("LinkToZone"),
            link_to_spawn=child.get_string("LinkToSpawn"),
            spawn_name=child.get_string("SpawnName"),
            encounter_we_table=child.get_string(
                "EncounterWorldEntityTable").lower(),
        ))
        source_index += 1
    return rows


def parse_world_file(extracter_root: str, fp: str) -> Optional[LevelRow]:
    """Parse a ``dungeonNN_levelMM.world`` maze level. Returns None when the file
    is not a maze level (no ``MazeWidth``)."""
    node = parse_file(fp)
    if node is None or not node.has_property("MazeWidth"):
        return None

    world_base = node.extends or ""
    zone_name = os.path.splitext(os.path.basename(fp))[0]
    enc = node.get_string("EncounterTable").lower()

    # Leader encounter: the first tile marker whose EncounterType names a
    # "leader" table (matches the old per-row leader placement). Tiles are the
    # node's anonymous children (``* extends world.base.room.…``).
    leader = ""
    for tile in node.anonymous_children:
        etype = tile.get_string("EncounterType").lower()
        if "leader" in etype:
            leader = etype
            break

    return LevelRow(
        zone_name=zone_name,
        world_base=world_base,
        tile_prefix=_tile_prefix_for_base(world_base),
        encounter_table=enc,
        leader_encounter=leader,
        maze_width=node.get_int("MazeWidth"),
        maze_height=node.get_int("MazeHeight"),
        maze_randomness=node.get_int("MazeRandomness"),
        maze_sparseness=node.get_int("MazeSparseness"),
        maze_dead_end_removal_chance=node.get_int("MazeDeadEndRemovalChance"),
    )


# ──────────────────────────────────────────────────────────────────────────
# Static (non-maze) world parsing — boss arenas, lobbies, quest off-shoots
# ──────────────────────────────────────────────────────────────────────────
# An authored encounter marker extends ``base.Encounter`` / ``base.Encounter<N>``
# (same convention as the maze .tile markers; ``Encounter_Empty`` deliberately
# fails the ``\d*$`` so empty markers spawn nothing).
_ENCOUNTER_EXTENDS_RE = re.compile(r"(?i)(?:^|\.)encounter(\d*)$")


@dataclass
class StaticWorldRow:
    """One non-maze ``*.world`` (no ``MazeWidth``) — a hand-authored layout."""
    zone_name: str
    world_base: str
    encounter_table: str    # lower, or ""


@dataclass
class StaticMarkerRow:
    """One authored ``base.Encounter<N>`` marker inside a static world.

    Positions are world coordinates (static worlds are not tiled — the designer
    placed everything in the world frame, Z included). ``size_x``/``size_y`` are
    the encounter AREA extents (0 when unspecified); ``encounter_type`` is an
    optional per-marker table overriding the world's ``EncounterTable``.
    """
    zone_name: str
    marker_idx: int
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    size_x: float
    size_y: float
    encounter_type: str     # lower, or ""


@dataclass
class StaticPlacementRow:
    """One named creature placement in a static ``*.world`` (e.g. a boss-fight
    posse member) — resolved to a spawnable asset + creature at its authored
    world position. The non-maze analogue of an encounter unit, but placed
    verbatim (no budget expansion — the designer placed each one)."""
    zone_name: str
    placement_idx: int
    entity_gc_type: str     # the world.* / creatures.* asset the client renders
    creature_gc_type: str   # lower, the creatures.* row for stats
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    name: str


def _collect_creature_placements(node: GCNode, zone_name: str,
                                 resolver: "PlacementResolver",
                                 out: List[StaticPlacementRow]) -> None:
    """Recursively collect direct creature placements (boss/posse) under ``node``.

    A placement is any positioned child whose ``extends`` resolves to a creature
    (via :class:`PlacementResolver`) and is NOT a ``base.Encounter`` marker
    (those feed :func:`_collect_encounter_markers`) — interactives/terrain
    resolve to ``None`` and are skipped."""
    for child in list(node.anonymous_children) + list(node.children.values()):
        ext = child.extends or ""
        if (ext and child.has_property("Position")
                and not _ENCOUNTER_EXTENDS_RE.search(ext)):
            resolved = resolver.resolve(ext)
            if resolved is not None:
                vec = _try_parse_vector3(child.get_string("Position"))
                if vec is not None:
                    entity, creature = resolved
                    out.append(StaticPlacementRow(
                        zone_name=zone_name,
                        placement_idx=len(out),
                        entity_gc_type=entity,
                        creature_gc_type=creature.lower(),
                        pos_x=vec[0], pos_y=vec[1], pos_z=vec[2],
                        heading=child.get_float("Heading", 0.0),
                        name=child.get_string("Name"),
                    ))
        _collect_creature_placements(child, zone_name, resolver, out)


def _try_parse_vector3(raw: str) -> Optional[Tuple[float, float, float]]:
    parts = (raw or "").split(",")
    if len(parts) != 3:
        return None
    try:
        return (float(parts[0].strip()), float(parts[1].strip()),
                float(parts[2].strip()))
    except ValueError:
        return None


def _collect_encounter_markers(node: GCNode, zone_name: str,
                               out: List[StaticMarkerRow]) -> None:
    """Recursively collect ``base.Encounter*`` placements under ``node``."""
    for child in list(node.anonymous_children) + list(node.children.values()):
        ext = child.extends or ""
        if _ENCOUNTER_EXTENDS_RE.search(ext) and child.has_property("Position"):
            vec = _try_parse_vector3(child.get_string("Position"))
            if vec is not None:
                # ★2026-06-21 (bible §14.4): a boss-arena marker overrides the
                # world's EncounterTable with its OWN ``EncounterTable`` (e.g.
                # ``world.dungeon06.enc.level08_master_encounter`` = Rotgut +
                # guards), NOT an ``EncounterType``. Reading only ``EncounterType``
                # left this empty for dungeon01–15 bosses → the spawner had a
                # marker region but no creature table to roll → the boss never
                # spawned (only dungeon00/16 worked, via named placements). Prefer
                # the per-marker ``EncounterTable`` (it gets collected + parsed by
                # the import driver), fall back to ``EncounterType``.
                enc_override = (child.get_string("EncounterTable")
                                or child.get_string("EncounterType")).lower()
                out.append(StaticMarkerRow(
                    zone_name=zone_name,
                    marker_idx=len(out),
                    pos_x=vec[0], pos_y=vec[1], pos_z=vec[2],
                    heading=child.get_float("Heading", 0.0),
                    size_x=child.get_float("SizeX", 0.0),
                    size_y=child.get_float("SizeY", 0.0),
                    encounter_type=enc_override,
                ))
        _collect_encounter_markers(child, zone_name, out)


def parse_static_world(
    fp: str, resolver: Optional["PlacementResolver"] = None
) -> Optional[Tuple[StaticWorldRow, List[StaticMarkerRow],
                    List[StaticPlacementRow]]]:
    """Parse a non-maze ``*.world`` (boss arena / lobby / quest off-shoot).

    The boss rooms carry an ``Entities`` block with ``base.Encounter`` markers
    (``Position`` + ``SizeX``/``SizeY``), a world-level ``EncounterTable`` AND
    direct named creature placements (the boss + its posse) — the static
    analogue of the maze chain. Returns ``None`` for maze worlds (``MazeWidth``
    present) and for worlds with no encounter table, marker, or placement
    (menus/scratch worlds). ``resolver`` (required to resolve placements) is
    optional so old callers/tests still parse markers; without it the placement
    list is empty.
    """
    node = parse_file(fp)
    if node is None or node.has_property("MazeWidth"):
        return None
    zone_name = os.path.splitext(os.path.basename(fp))[0]
    enc = node.get_string("EncounterTable").lower()

    markers: List[StaticMarkerRow] = []
    placements: List[StaticPlacementRow] = []
    entities = node.get_child("Entities")
    if entities is not None:
        _collect_encounter_markers(entities, zone_name, markers)
        if resolver is not None:
            _collect_creature_placements(entities, zone_name, resolver, placements)

    if not enc and not markers and not placements:
        return None
    row = StaticWorldRow(zone_name=zone_name, world_base=node.extends or "",
                         encounter_table=enc)
    return row, markers, placements


# ──────────────────────────────────────────────────────────────────────────
# Schema + import driver
# ──────────────────────────────────────────────────────────────────────────
def build_schema(conn: sqlite3.Connection) -> None:
    """(Re)create the dungeon spawn tables. Dropped first so re-runs are clean."""
    conn.execute("DROP TABLE IF EXISTS dungeon_levels")
    conn.execute("DROP TABLE IF EXISTS dungeon_encounters")
    conn.execute("DROP TABLE IF EXISTS dungeon_room_nodes")
    conn.execute("DROP TABLE IF EXISTS static_worlds")
    conn.execute("DROP TABLE IF EXISTS static_world_encounters")
    conn.execute("DROP TABLE IF EXISTS static_world_placements")
    conn.execute("""
        CREATE TABLE dungeon_levels (
            zone_name TEXT PRIMARY KEY,
            world_base TEXT,
            tile_prefix TEXT,
            encounter_table TEXT,
            leader_encounter TEXT,
            maze_width INTEGER,
            maze_height INTEGER,
            maze_randomness INTEGER,
            maze_sparseness INTEGER,
            maze_dead_end_removal_chance INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE dungeon_encounters (
            enc_table TEXT,
            group_idx INTEGER,
            unit_idx INTEGER,
            entity_gc_type TEXT,
            creature_gc_type TEXT,
            chance REAL,
            difficulty REAL,
            PRIMARY KEY (enc_table, group_idx, unit_idx)
        )
    """)
    conn.execute("CREATE INDEX idx_enc_table ON dungeon_encounters(enc_table)")
    conn.execute("""
        CREATE TABLE dungeon_room_nodes (
            zone_name TEXT,
            source_index INTEGER,
            node_kind TEXT,
            tile_set TEXT,
            grid_x INTEGER,
            grid_y INTEGER,
            chance INTEGER,
            encounter_type TEXT,
            link_to_zone TEXT,
            link_to_spawn TEXT,
            spawn_name TEXT,
            encounter_we_table TEXT DEFAULT '',
            PRIMARY KEY (zone_name, source_index)
        )
    """)
    conn.execute("CREATE INDEX idx_room_nodes_zone ON dungeon_room_nodes(zone_name)")
    conn.execute("""
        CREATE TABLE static_worlds (
            zone_name TEXT PRIMARY KEY,
            world_base TEXT,
            encounter_table TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE static_world_encounters (
            zone_name TEXT,
            marker_idx INTEGER,
            pos_x REAL,
            pos_y REAL,
            pos_z REAL,
            heading REAL,
            size_x REAL,
            size_y REAL,
            encounter_type TEXT,
            PRIMARY KEY (zone_name, marker_idx)
        )
    """)
    conn.execute(
        "CREATE INDEX idx_static_enc_zone ON static_world_encounters(zone_name)")
    conn.execute("""
        CREATE TABLE static_world_placements (
            zone_name TEXT,
            placement_idx INTEGER,
            entity_gc_type TEXT,
            creature_gc_type TEXT,
            pos_x REAL,
            pos_y REAL,
            pos_z REAL,
            heading REAL,
            name TEXT,
            PRIMARY KEY (zone_name, placement_idx)
        )
    """)
    conn.execute(
        "CREATE INDEX idx_static_placement_zone "
        "ON static_world_placements(zone_name)")


def import_dungeon_worlds(conn: sqlite3.Connection, extracter_root: str
                          ) -> Tuple[int, int, int, int]:
    """Parse every root ``*.world`` (maze levels AND static worlds, + their
    encounter tables and room nodes) and populate the dungeon spawn tables.
    Returns ``(levels, encounter_units, room_nodes, static_worlds)``.

    Every ``*.world`` at the extracter root is a warpable zone candidate
    (``elite01_*``, ``amazon_dungeon``, ``epic01_*``, PvP arenas, the ``*_boss``
    rooms, lobbies, quest off-shoots, …); only the ``!``-prefixed tileset
    scratch files are skipped. Mazes land in ``dungeon_levels``/
    ``dungeon_room_nodes``; statics in ``static_worlds``/
    ``static_world_encounters``. A row for a non-warpable test world is inert —
    the spawner only reads rows for the zone actually being entered.
    """
    build_schema(conn)
    mob_map = build_mob_creature_map(extracter_root)
    # Resolver for direct named creature placements (boss + posse) in static
    # worlds — shares the mob_map so the chain's final hop is free.
    resolver = PlacementResolver.build(extracter_root, mob_map=mob_map)

    levels: List[LevelRow] = []
    room_node_rows: List[RoomNodeRow] = []
    static_rows: List[StaticWorldRow] = []
    static_marker_rows: List[StaticMarkerRow] = []
    static_placement_rows: List[StaticPlacementRow] = []
    for fp in sorted(glob.glob(os.path.join(extracter_root, "*.world"))):
        if os.path.basename(fp).startswith("!"):
            continue
        row = parse_world_file(extracter_root, fp)
        if row is not None:
            levels.append(row)
            room_node_rows.extend(
                parse_room_nodes(extracter_root, fp, row.zone_name))
            continue
        static = parse_static_world(fp, resolver)
        if static is not None:
            static_rows.append(static[0])
            static_marker_rows.extend(static[1])
            static_placement_rows.extend(static[2])

    # Collect the distinct .enc tables referenced (level main + leader, room-node
    # per-room tables, static-world main + per-marker tables) and parse once.
    enc_names = set()
    for lvl in levels:
        if lvl.encounter_table:
            enc_names.add(lvl.encounter_table)
        if lvl.leader_encounter:
            enc_names.add(lvl.leader_encounter)
    for rn in room_node_rows:
        if rn.encounter_type:
            enc_names.add(rn.encounter_type)
    for sw in static_rows:
        if sw.encounter_table:
            enc_names.add(sw.encounter_table)
    for sm in static_marker_rows:
        if sm.encounter_type:
            enc_names.add(sm.encounter_type)

    unit_rows: List[EncounterUnitRow] = []
    for enc in sorted(enc_names):
        unit_rows.extend(parse_encounter_table(extracter_root, enc, mob_map))

    conn.executemany(
        "INSERT OR REPLACE INTO dungeon_levels VALUES "
        "(:zone_name, :world_base, :tile_prefix, :encounter_table, "
        ":leader_encounter, :maze_width, :maze_height, :maze_randomness, "
        ":maze_sparseness, :maze_dead_end_removal_chance)",
        [vars(l) for l in levels],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO dungeon_encounters VALUES "
        "(:enc_table, :group_idx, :unit_idx, :entity_gc_type, "
        ":creature_gc_type, :chance, :difficulty)",
        [vars(u) for u in unit_rows],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO dungeon_room_nodes VALUES "
        "(:zone_name, :source_index, :node_kind, :tile_set, :grid_x, :grid_y, "
        ":chance, :encounter_type, :link_to_zone, :link_to_spawn, :spawn_name, "
        ":encounter_we_table)",
        [vars(r) for r in room_node_rows],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO static_worlds VALUES "
        "(:zone_name, :world_base, :encounter_table)",
        [vars(s) for s in static_rows],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO static_world_encounters VALUES "
        "(:zone_name, :marker_idx, :pos_x, :pos_y, :pos_z, :heading, "
        ":size_x, :size_y, :encounter_type)",
        [vars(m) for m in static_marker_rows],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO static_world_placements VALUES "
        "(:zone_name, :placement_idx, :entity_gc_type, :creature_gc_type, "
        ":pos_x, :pos_y, :pos_z, :heading, :name)",
        [vars(p) for p in static_placement_rows],
    )
    conn.commit()
    return len(levels), len(unit_rows), len(room_node_rows), len(static_rows)
