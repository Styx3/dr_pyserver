"""Item-modifier (modpal) pool — the full set of registered attribute mods per
class family, read from the client's ``GCDictionary.dict`` at runtime.

Why the dictionary and not the baked ``item_wire_mods`` table: the table is the
sparse output of the IG/MG resolver (e.g. MageModPal *Superior* resolved to a
single mod), so generating from it makes every item carry the identical stat —
the "mods aren't random" report. The dictionary is the client's flat registry of
EVERY registered class name, so it holds the rich pools (MageModPal.Rare alone
has ~29 mods, plus thematic DamageBonus/DamageResistBonus/Stun/... subtrees) that
make generated items feel varied. Every name here is a real registered class, so
emitting one by-hash is crash-safe; the modifier's effect comes from its GC
definition (the wire body is content-lenient).

Located via the shared :mod:`drserver.data.extracter_paths` resolver (env
override, else the repo-sibling extracter, probed so it works under both native
Windows and WSL). If the dictionary is unavailable the pool is empty and callers
fall back to the baked table.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from ..core import log
from .extracter_paths import extracter_dir

# items.modpal.<Family>.<Subtree>.Mod<N> — the registered mod leaves. Case is
# irrelevant downstream (by-hash resolution lowercases), but the registry mixes
# ``...ModPAL`` and ``...ModPal``, so match case-insensitively.
_LEAF_RE = re.compile(
    r"items\.modpal\.([A-Za-z0-9]+)\.([A-Za-z0-9]+)\.Mod\d+", re.IGNORECASE)

# Rarity -> the single quality-tier subtree to draw the item's PRIMARY attribute
# mod from (Intellect/Endurance/... live here). Normal gear gets no stat mod.
_QUALITY_SUBTREE: Dict[str, Optional[str]] = {
    "Normal": None, "Superior": "superior", "Magical": "magic",
    "Rare": "rare", "Unique": "unique", "Mythic": "mythic",
}
_ALL_QUALITY = frozenset(s for s in _QUALITY_SUBTREE.values() if s)
# Binding / required-level / cosmetic bookkeeping subtrees — never a visible stat.
_EXCLUDED_SUBTREES = frozenset({
    "binder", "craftedbinder", "mythicbinder", "uniquebinder", "questbinder",
    "required", "mythicrequired", "exceptions", "visual",
})

# family_lower -> { subtree_lower -> [modref, ...] }
_pool: Dict[str, Dict[str, List[str]]] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = os.path.join(extracter_dir(), "GCDictionary.dict")
    try:
        with open(path, "r", encoding="latin-1") as fh:
            text = fh.read()
    except OSError:
        log.info(f"[ModpalPool] GCDictionary not found at {path} — "
                 "vendor attribute mods fall back to the baked item_wire_mods")
        return
    seen: set = set()
    for match in _LEAF_RE.finditer(text):
        ref = match.group(0)
        key = ref.lower()
        if key in seen:
            continue
        seen.add(key)
        _pool.setdefault(match.group(1).lower(), {}).setdefault(
            match.group(2).lower(), []).append(ref)
    leaves = sum(len(v) for fam in _pool.values() for v in fam.values())
    log.info(f"[ModpalPool] {leaves} modpal mods across {len(_pool)} families")


def stat_mods(family: str, rarity_name: str) -> Tuple[List[str], List[str]]:
    """Return ``(quality_mods, thematic_mods)`` for ``family`` at ``rarity_name``:

    * ``quality_mods`` — the rarity-tier subtree (the item's primary attribute:
      Intellect for mage, etc.); empty for Normal.
    * ``thematic_mods`` — every non-quality, non-bookkeeping stat subtree
      (DamageBonus, DamageResistBonus, StunBonus, ...).

    Both are registered modref strings; empty when the dictionary is unavailable
    or the family/subtree defines none.
    """
    _load()
    fam = _pool.get(family.lower())
    if not fam:
        return [], []
    quality_sub = _QUALITY_SUBTREE.get(rarity_name)
    quality = list(fam.get(quality_sub, [])) if quality_sub else []
    thematic = [ref for sub, refs in fam.items()
                if sub not in _EXCLUDED_SUBTREES and sub not in _ALL_QUALITY
                for ref in refs]
    return quality, thematic


def reset() -> None:
    """Test hook: drop the cache so the next call reloads (or re-reads env)."""
    global _loaded
    _pool.clear()
    _loaded = False
