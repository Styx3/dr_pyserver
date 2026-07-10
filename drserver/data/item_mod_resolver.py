"""Native item-modifier resolver — walks the client's ItemGenerator → ItemMod
Generator → ModPAL chain to recover the real stat mods each item carries.

This is the T0-faithful replacement for the old "one generic ScaleMod per item"
stub. The client generates items via ``ItemGeneratorTable`` entries that pair an
``Item = items.pal.<Family>PAL.<Quality>NNN`` with up to five
``ItemModGeneratorN = items.mg.<...>`` references; each generator is an
``ItemModifierGeneratorTable`` whose ``SingleItemModGenerator`` children name an
``ItemModifier = items.modpal.<...>`` (e.g. ``MageModPal.Superior.Mod1`` =
*IntellectB*). The resolved mod refs are emitted on the wire as by-hash
``ItemModifier`` children (``0x04 <djb2> 0x00`` — see
``docs/CLIENT_GROUND_TRUTH.md``).

Ground-truth structure (extracter, T0):

* ``items/ig/{class}/{Rarity}{Class}{Slot}IG.gc`` — DIRECT entries: a node with
  ``Item =`` + ``ItemModGenerator1..5``. Rarity is in the IG root name.
* ``items/ig/{weaponfamily}/{Rarity}IG.gc`` — WRAPPER entries: a
  ``RandomItemGenerator`` with ``ItemGenerator = items.ig.<...>.NormalIG.<Sub>``
  (the base item list) + ``ItemModGenerator1..5``; the mods apply to every item
  the referenced base IG enumerates, at the wrapper's rarity.
* ``items/mg/*.gc`` — ``ItemModifierGeneratorTable``s; each ``SingleItemMod
  Generator`` child carries ``Chance``/``MinLevel``/``MaxLevel``/``ItemModifier``.
  Level-prefix generators are **level-banded** (pick the band that contains the
  item level), the rest pick the first applicable mod (DR-Server parity, but
  level-aware).

The resolver is pure over the parsed ``.gc`` tree so it can be unit-tested
against the extracter and baked into the DB by ``item_wire_mods_importer``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .gc_parser import GCNode, parse_file

# Rarity keywords that can appear in an IG root name (longest first so "Magic"
# is not shadowed by a substring match).
_RARITIES = ("Mythic", "WishingWell", "Seasonal", "Superior", "Magic",
             "Unique", "Rare", "Normal")

# Quality token → ItemRarity int (wire value), for callers that want it.
_RARITY_INT = {"Normal": 0, "Superior": 1, "Magic": 2, "Magical": 2,
               "Rare": 3, "Unique": 4, "Mythic": 5}


@dataclass
class ModChoice:
    """One ``SingleItemModGenerator`` option inside a generator table."""
    chance: int
    min_level: int
    max_level: int
    mod_ref: str               # items.modpal.<...>


@dataclass
class RawGenerator:
    """A generator table before link resolution: its own ``SingleItemMod
    Generator`` choices plus the ``ItemModifierGeneratorLink`` targets it
    aggregates."""
    choices: List[ModChoice] = field(default_factory=list)
    links: List[str] = field(default_factory=list)   # items.mg.<...> paths


@dataclass
class IGEntry:
    """A resolved item-generator entry (direct or wrapper)."""
    rarity: str                          # Normal/Superior/Magic/Rare/Unique/...
    item_ref: Optional[str]              # direct: items.pal.<...>; None if wrapper
    target_ig: Optional[str]             # wrapper: items.ig.<...> base list ref
    generators: List[str] = field(default_factory=list)  # items.mg.<...> paths
    source: str = ""


@dataclass
class ResolvedItem:
    """Final (item, rarity) → ordered mod refs the wire should carry."""
    item_ref: str
    rarity: str
    mod_refs: List[str]


# ── MG parsing ──────────────────────────────────────────────────────────────

def _walk(node: GCNode):
    """Yield (dotted_path, node) for every named descendant (depth-first)."""
    stack: List[Tuple[str, GCNode]] = [(node.name, node)]
    while stack:
        path, n = stack.pop()
        yield path, n
        for child_name, child in n.children.items():
            stack.append((f"{path}.{child_name}", child))


def _children_of(node: GCNode) -> List[GCNode]:
    """All children, named + anonymous (generators use ``* extends ...``)."""
    return list(node.children.values()) + node.anonymous_children


def _strip_mg(path: str) -> str:
    return path[len("items.mg."):] if path.lower().startswith("items.mg.") else path


def load_mod_generators(mg_dir: str) -> Dict[str, RawGenerator]:
    """Build ``{generator_path_lower: RawGenerator}`` from items/mg/*.gc.

    A generator path is the dotted location of an ItemModifierGeneratorTable,
    e.g. ``magemg.suppostmg`` for ``items.mg.MageMG.SupPostMG`` (the leading
    ``items.mg.`` is stripped). A generator may carry direct ``SingleItemMod
    Generator`` choices (named or anonymous ``* extends``) AND/OR
    ``ItemModifierGeneratorLink`` targets (``LinkedGenerator =``) which it
    aggregates — both forms are captured here and flattened by ``resolve_gen``.
    """
    gens: Dict[str, RawGenerator] = {}
    if not os.path.isdir(mg_dir):
        return gens
    for fname in sorted(os.listdir(mg_dir)):
        if not fname.endswith(".gc"):
            continue
        root = parse_file(os.path.join(mg_dir, fname))
        if root is None:
            continue
        for path, n in _walk(root):
            choices: List[ModChoice] = []
            links: List[str] = []
            for c in _children_of(n):
                mod_ref = c.get_string("ItemModifier")
                if mod_ref:
                    choices.append(ModChoice(
                        chance=c.get_int("Chance", 1),
                        min_level=c.get_int("MinLevel", 1),
                        max_level=c.get_int("MaxLevel", 9999),
                        mod_ref=mod_ref.strip()))
                link = c.get_string("LinkedGenerator")
                if link:
                    links.append(_strip_mg(link.strip()))
            if choices or links:
                gens[path.lower()] = RawGenerator(choices, links)
    return gens


def resolve_gen(gen_path: str, gens: Dict[str, RawGenerator],
                _seen: Optional[set] = None) -> List[ModChoice]:
    """Flatten a generator to its full choice list, following LinkedGenerators."""
    norm = _strip_mg(gen_path).lower()
    if _seen is None:
        _seen = set()
    if norm in _seen:
        return []
    _seen.add(norm)
    raw = gens.get(norm)
    if raw is None:
        return []
    out = list(raw.choices)
    for link in raw.links:
        out.extend(resolve_gen(link, gens, _seen))
    return out


def pick_mod(choices: List[ModChoice], item_level: int) -> Optional[str]:
    """Select one mod ref from a generator for an item at ``item_level``.

    Prefer the level band that contains the level (level-prefix generators are
    banded); otherwise the first choice whose MinLevel <= level; else the first.
    """
    if not choices:
        return None
    banded = [c for c in choices if c.min_level <= item_level <= c.max_level]
    if banded:
        return banded[0].mod_ref
    # No band contains the level: above all bands → closest (highest MinLevel)
    # eligible band; below all bands → the first (lowest) choice.
    eligible = [c for c in choices if c.min_level <= item_level]
    if eligible:
        return max(eligible, key=lambda c: c.min_level).mod_ref
    return choices[0].mod_ref


# ── IG parsing ──────────────────────────────────────────────────────────────

def _rarity_of(name: str) -> str:
    for r in _RARITIES:
        if r.lower() in name.lower():
            return "Magical" if r == "Magic" else r
    return "Normal"


def _generators_of(node: GCNode) -> List[str]:
    out: List[str] = []
    i = 1
    while True:
        v = node.get_string(f"ItemModGenerator{i}")
        if not v:
            break
        out.append(v.strip())
        i += 1
    return out


def _parse_ig_file(path: str) -> List[IGEntry]:
    root = parse_file(path)
    if root is None:
        return []
    rarity = _rarity_of(root.name)
    entries: List[IGEntry] = []
    for _p, n in _walk(root):
        # Anonymous children (``* extends RandomItemGenerator``) carry the data
        # for wrapper IGs; named children carry direct items.
        candidates = list(n.children.values()) + n.anonymous_children
        for c in candidates:
            gens = _generators_of(c)
            if not gens:
                continue
            item_ref = c.get_string("Item")
            target_ig = c.get_string("ItemGenerator")
            if item_ref:
                entries.append(IGEntry(rarity, item_ref.strip(), None, gens,
                                       os.path.basename(path)))
            elif target_ig:
                entries.append(IGEntry(rarity, None, target_ig.strip(), gens,
                                       os.path.basename(path)))
    return entries


def load_ig_entries(ig_dir: str) -> List[IGEntry]:
    """Parse every items/ig/**/*.gc into direct + wrapper entries."""
    entries: List[IGEntry] = []
    for dirpath, _dirs, files in os.walk(ig_dir):
        for fname in sorted(files):
            if fname.endswith(".gc"):
                entries.extend(_parse_ig_file(os.path.join(dirpath, fname)))
    return entries


def _base_items_of(target_ig: str, ig_dir: str,
                   _cache: Dict[str, List[str]]) -> List[str]:
    """Resolve a wrapper's ``ItemGenerator`` ref to the concrete item refs it
    enumerates (e.g. ``items.ig.1hAxe.NormalIG.Standard`` → the 1HAxePAL items).
    """
    key = target_ig.lower()
    if key in _cache:
        return _cache[key]
    # The file is the segment after items.ig.<dir>. e.g.
    # items.ig.1hAxe.NormalIG.Standard → dir=1haxe file=NormalIG node path .Standard
    parts = target_ig.split(".")
    items: List[str] = []
    # Find the .gc file whose stem matches one of the path segments.
    for dirpath, _dirs, files in os.walk(ig_dir):
        for fname in files:
            stem = fname[:-3] if fname.endswith(".gc") else fname
            if stem.lower() in (p.lower() for p in parts):
                root = parse_file(os.path.join(dirpath, fname))
                if root is None:
                    continue
                for _p, n in _walk(root):
                    iv = n.get_string("Item")
                    if iv:
                        items.append(iv.strip())
                if items:
                    break
        if items:
            break
    _cache[key] = items
    return items


def build_resolved_items(extracter_dir: str) -> List[ResolvedItem]:
    """Full pipeline: (item, rarity) → ordered mod refs, from the extracter.

    Uses a representative item level for the level-banded mod pick so the level-
    prefix mod is sensible; per-stock level refinement happens at generation
    time, but the attribute mods (Intellect etc.) are level-stable.
    """
    ig_dir = os.path.join(extracter_dir, "items", "ig")
    mg_dir = os.path.join(extracter_dir, "items", "mg")
    gens = load_mod_generators(mg_dir)
    ig_entries = load_ig_entries(ig_dir)
    base_cache: Dict[str, List[str]] = {}

    def resolve_gens(gen_paths: List[str], level: int) -> List[str]:
        refs: List[str] = []
        for gp in gen_paths:
            choices = resolve_gen(gp, gens)
            mref = pick_mod(choices, level)
            if mref and mref not in refs:
                refs.append(mref)
        return refs

    # Representative level per rarity for the banded pick (mid-band; attribute
    # mods are level-stable, only the level-prefix differs by band).
    rep_level = 20
    out: Dict[Tuple[str, str], ResolvedItem] = {}
    for e in ig_entries:
        item_refs = ([e.item_ref] if e.item_ref
                     else _base_items_of(e.target_ig, ig_dir, base_cache))
        mod_refs = resolve_gens(e.generators, rep_level)
        if not mod_refs:
            continue
        for item_ref in item_refs:
            if not item_ref:
                continue
            key = (item_ref.lower(), e.rarity)
            # First writer wins (stable); later duplicate IGs don't clobber.
            if key not in out:
                out[key] = ResolvedItem(item_ref, e.rarity, list(mod_refs))
    return list(out.values())
