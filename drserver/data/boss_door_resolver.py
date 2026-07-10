"""Resolve which world Doors a mob opens on death — the ``DoorsToOpenOnDeath``
mechanic — straight from the client content packs (the extracter), the
authoritative source. No DB column, no rebuild.

The client content authors the boss→gate link on the mob's *own* node::

    world/dungeon00/mob/boss.gc
        boss extends creatures.whiskers.broodling.Basic.Champion {
            Description { DoorsToOpenOnDeath = "Boss00ExitGate"; ... } }

and the gate placement carries the matching ``Name`` **property**::

    world/dungeon00/data/BossGate.gc
        BossGate extends terrain.interactives.doors.BossGate {
            Name = "Boss00ExitGate"; ... }

Note the gate's ``zone_world_entities`` row stores the *node* name (``BossGate``),
**not** this ``Name`` property (``Boss00ExitGate``) — so the two are bridged here
by reading the gate's own content file for its ``Name``. Both lookups resolve a
single file from a ``gc_type`` and are cached, so a boss kill reads at most a
handful of files. Case-insensitive path resolution tolerates the DB↔filesystem
case drift (``world.dungeon00.data.BossGate`` → ``world/dungeon00/data/BossGate.gc``).
"""
from __future__ import annotations

import os
import re
from typing import Dict, FrozenSet, Optional

from ..core import log
from .extracter_paths import resolve_extracter_dir

# ``DoorsToOpenOnDeath = "A,B";`` — one or more comma-separated door names.
_DOORS_RE = re.compile(r'DoorsToOpenOnDeath\s*=\s*"([^"]*)"', re.IGNORECASE)
# ``Name = "Boss00ExitGate";`` (quoted) or ``Name = BossGate;`` (bare ident).
_NAME_QUOTED_RE = re.compile(r'\bName\s*=\s*"([^"]+)"')
_NAME_BARE_RE = re.compile(r'\bName\s*=\s*([A-Za-z0-9_]+)\s*;')

# Caches (gc_type-lower → result). ``None``/empty are cached too so a miss is not
# re-probed on every kill.
_file_cache: Dict[str, Optional[str]] = {}
_doors_cache: Dict[str, FrozenSet[str]] = {}
_name_cache: Dict[str, Optional[str]] = {}


def _ci_child(parent: str, name: str) -> Optional[str]:
    """Return the real child of ``parent`` matching ``name`` case-insensitively,
    or ``None``. Exact match is preferred (fast path) before the dir scan."""
    exact = os.path.join(parent, name)
    if os.path.exists(exact):
        return exact
    try:
        low = name.lower()
        for entry in os.listdir(parent):
            if entry.lower() == low:
                return os.path.join(parent, entry)
    except OSError:
        return None
    return None


def _resolve_gc_file(gc_type: str) -> Optional[str]:
    """Map a ``gc_type`` to its ``.gc`` content file, or ``None``.

    Walks the dotted segments case-insensitively from the extracter root. A
    nested node (``…mob.quest.Q05Gate``) has no file of its own, so progressively
    shorter segment prefixes are tried (``…/quest/Q05Gate.gc`` → ``…/quest.gc``)
    until an existing file is found; the caller then scans that file's body.
    """
    key = gc_type.lower()
    if key in _file_cache:
        return _file_cache[key]

    root = resolve_extracter_dir()
    segs = [s for s in gc_type.split(".") if s]
    result: Optional[str] = None
    if root and segs:
        for cut in range(len(segs), 0, -1):
            cur: Optional[str] = root
            for seg in segs[:cut - 1]:
                cur = _ci_child(cur, seg)
                if cur is None:
                    break
            if cur is None:
                continue
            leaf = _ci_child(cur, segs[cut - 1] + ".gc")
            if leaf is not None and os.path.isfile(leaf):
                result = leaf
                break

    _file_cache[key] = result
    return result


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError as ex:  # pragma: no cover - defensive
        log.warn(f"[BossDoor] could not read '{path}': {ex}")
        return ""


def doors_opened_by(mob_gc_type: str) -> FrozenSet[str]:
    """Lowercased door names the mob's ``DoorsToOpenOnDeath`` lists (may be empty).

    Reads the mob's own content file. Values are the door ``Name`` *properties*
    (bridged to a gate via :func:`door_name_of`), not gc_types.
    """
    key = (mob_gc_type or "").lower()
    if key in _doors_cache:
        return _doors_cache[key]

    names: set[str] = set()
    path = _resolve_gc_file(mob_gc_type)
    if path:
        for group in _DOORS_RE.findall(_read(path)):
            for name in group.split(","):
                name = name.strip().lower()
                if name:
                    names.add(name)

    result = frozenset(names)
    _doors_cache[key] = result
    return result


def door_name_of(gate_gc_type: str) -> Optional[str]:
    """The gate's ``Name`` property (lowercased), or ``None``.

    Prefers the first quoted ``Name = "…"`` (the placement's exit-gate name, e.g.
    ``Boss00ExitGate``), falling back to a bare ``Name = Ident;``.
    """
    key = (gate_gc_type or "").lower()
    if key in _name_cache:
        return _name_cache[key]

    result: Optional[str] = None
    path = _resolve_gc_file(gate_gc_type)
    if path:
        text = _read(path)
        m = _NAME_QUOTED_RE.search(text) or _NAME_BARE_RE.search(text)
        if m:
            result = m.group(1).strip().lower() or None

    _name_cache[key] = result
    return result


def clear_cache() -> None:
    """Drop the resolution caches (tests / content reload)."""
    _file_cache.clear()
    _doors_cache.clear()
    _name_cache.clear()
