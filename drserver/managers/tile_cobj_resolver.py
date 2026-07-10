"""Resolves GC-script package paths to ``.tile`` / ``.cobj`` files.

Port of C# ``DungeonRunners.Utilities.TileCobjResolver``. The extracted client
content (``extracter/``) holds 1477 ``.tile`` + 1452 ``.cobj`` files **flat** at
its root, so a single directory scan builds a ``{leaf-lowercase: path}`` index
and every lookup is O(1).

Resolution rule: take the leaf component of a dotted path
(``terrain.elmforest.walls.elmforest_4_straight_2`` → ``elmforest_4_straight_2``)
and match case-insensitively against the flat filenames. Many GC paths resolve
to **no** ``.cobj`` (visual-only props, encounter spawn points, abstract base
classes); those return ``None`` and callers treat them as non-blocking.

No cross-theme aliasing: the C# proved (2026-05-28) that substituting a
different theme's geometry for a missing asset *hurts* parity — it drops walls
into open corridors. The correct answer for a missing asset is ``None`` (cell
stays walkable). See ``TileCobjResolver.ResolveCobjAlias`` in the C# for the
full lesson log.

Indexed lazily on first use; the index is process-wide and cached.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from ..core import log
from ..data import cobj_parser
from ..data.cobj_parser import CobjData
from ..data.extracter_paths import candidate_roots, resolve_extracter_dir

_ENV_VAR = "DR_EXTRACTER_ROOT"

# A room-node tile_set (e.g. ``cat_up_``) expands to its concrete variant tiles —
# the leaves that follow it with a direction-exit suffix and an optional ``_a``/
# ``_b`` shape letter (``cat_up_1n``, ``cat_corner_1e1s_a``, ``…cart_1e1s1w_b``).
# Verified against the live client (x64dbg, 2026-06-09): the client's per-tileset
# variant vector is exactly these leaves sorted alphabetically — see
# ``maze._get_tile_variants`` and [[project_dungeon_map_alignment]].
_VARIANT_SUFFIX = re.compile(r"(?:1[nsew])+(?:_[a-z])?")

# Default maze cell side length when a tileset's ``TileSize`` can't be resolved.
# elmforest is 400; most others differ (cave_small 360, ruins 280, cave_large
# 520, …) — see :func:`tile_size_for`.
DEFAULT_TILE_SIZE = 400

# ``TileSet = <prefix>;`` / ``TileSize = <int>;`` lines inside a per-theme world
# base class (``base/World_<theme>.gc``). The client spaces maze cells by this
# per-tileset size; using a fixed 400 drifts every non-elmforest dungeon (proven
# 2026-06-09 — dungeon01 cave/ruins spawned inside walls). See [[maze]].
_TILESET_LINE = re.compile(r"^\s*TileSet\s*=\s*([A-Za-z0-9_]+)\s*;", re.MULTILINE)
_TILESIZE_LINE = re.compile(r"^\s*TileSize\s*=\s*(\d+)\s*;", re.MULTILINE)


def _candidate_roots() -> list[str]:
    """Plausible extracter locations, most-reliable first — see
    :func:`drserver.data.extracter_paths.candidate_roots`."""
    return candidate_roots()


class _ResolverState:
    """Holds the lazily-built flat index so it can be reset in tests."""

    def __init__(self) -> None:
        self._cobj: Optional[Dict[str, str]] = None
        self._tile: Optional[Dict[str, str]] = None
        self._tile_sizes: Optional[Dict[str, int]] = None

    def reset(self) -> None:
        self._cobj = None
        self._tile = None
        self._tile_sizes = None

    def _resolve_root(self) -> Optional[str]:
        return resolve_extracter_dir()

    def _ensure_indexed(self) -> None:
        if self._cobj is not None:
            return
        cobj: Dict[str, str] = {}
        tile: Dict[str, str] = {}
        root = self._resolve_root()
        if root is not None:
            # Single os.scandir of the flat root — no per-call globbing (WSL /mnt/c
            # is slow; index once). The extracter holds all .tile/.cobj at top level.
            with os.scandir(root) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    leaf, ext = os.path.splitext(entry.name)
                    ext = ext.lower()
                    if ext == ".cobj":
                        cobj[leaf.lower()] = entry.path
                    elif ext == ".tile":
                        tile[leaf.lower()] = entry.path
            log.info(
                f"[TILECOBJ] indexed {len(cobj)} .cobj + {len(tile)} .tile from {root}"
            )
        else:
            log.warn(
                f"[TILECOBJ] extracter root not found (set ${_ENV_VAR}); "
                f"probed: {', '.join(_candidate_roots())}"
            )

        # Never cache an EMPTY index: an empty result almost always means the
        # root was transiently unreachable (WSL /mnt/c can drop out at startup),
        # and the index is process-wide — caching empty would leave every later
        # pathmap build with a 100%-walkable map for the life of the process.
        # Leaving ``_tile``/``_cobj`` as None means the next call re-scans.
        if not tile and not cobj:
            log.warn("[TILECOBJ] empty index — not caching; will retry on next use")
            return
        self._cobj = cobj
        self._tile = tile

    def _ensure_tile_sizes(self) -> None:
        """Build ``{tileset_prefix: TileSize}`` from ``base/World_*.gc``.

        Each per-theme world base (``World_cave_small.gc`` etc.) declares the
        world-unit side length of one maze cell (``TileSize = 360;``). The client
        uses this per-tileset size to space cells; the server must match it
        (otherwise every cell origin drifts by ``gridOffset*(400-TileSize)``).

        Keying rule (correctness-critical):
        * The AUTHORITATIVE key is the **filename family** — ``World_<fam>.gc`` →
          ``<fam>_tileset_`` — because that is exactly what the importer derives
          from a level's ``extends base.world_<fam>`` (the ``tile_prefix`` stored
          in ``dungeon_levels``). The file's *declared* ``TileSet`` can differ
          (``World_lavacaves.gc`` declares ``lavapool_tileset_``), so keying only
          by the declared prefix would miss ``lavacaves_tileset_`` and wrongly
          fall back to 400.
        * ``World_boss_*.gc`` are single-room hand-authored arenas, NOT procedural
          maze worlds, and are the ONLY files that re-declare a prefix with a
          different size (``crypt_tileset_`` 200 vs 280; ``shadow_tileset_`` 480
          vs 400). Skipping them removes the collision entirely.
        * Entries are processed in sorted order so resolution is deterministic
          regardless of ``os.scandir`` order (which is filesystem-dependent).
        * The declared ``TileSet`` prefix is also registered as a non-overriding
          bonus (``setdefault``) so a lookup by the real tileset name still works.

        Cached process-wide; never caches an empty map (transient unreachable
        root → retry next call)."""
        if self._tile_sizes is not None:
            return
        sizes: Dict[str, int] = {}
        root = self._resolve_root()
        base_dir = os.path.join(root, "base") if root else None
        if base_dir and os.path.isdir(base_dir):
            with os.scandir(base_dir) as it:
                entries = sorted((e for e in it if e.is_file()),
                                 key=lambda e: e.name.lower())
            for entry in entries:
                name = entry.name
                low = name.lower()
                if not low.endswith(".gc"):
                    continue
                # Boss arenas are not procedural maze worlds; they alone re-declare
                # a tileset prefix with a conflicting size.
                if low.startswith("world_boss"):
                    continue
                try:
                    text = open(entry.path, encoding="utf-8",
                                errors="ignore").read()
                except OSError:
                    continue
                sz = _TILESIZE_LINE.search(text)
                if not sz:
                    continue
                size = int(sz.group(1))
                # Authoritative key: filename family → ``<fam>_tileset_`` (matches
                # the importer's ``tile_prefix``).
                fam = name[:-3]  # strip ".gc"
                if fam.lower().startswith("world_"):
                    fam = fam[6:]
                sizes[f"{fam.lower()}_tileset_"] = size
                # Bonus: the declared TileSet prefix (e.g. ``lavapool_tileset_``),
                # without overriding a family key.
                ts = _TILESET_LINE.search(text)
                if ts:
                    sizes.setdefault(ts.group(1).lower(), size)
            log.info(f"[TILECOBJ] indexed {len(sizes)} tileset sizes from {base_dir}")
        if not sizes:
            # Don't cache empty (see _ensure_indexed) — root may be transiently
            # unreachable; callers fall back to DEFAULT_TILE_SIZE meanwhile.
            return
        self._tile_sizes = sizes

    def tile_size_for(self, tile_set_prefix: str) -> int:
        """World-unit side length of one maze cell for a tileset prefix.

        ``tile_set_prefix`` is the level's ``TileSet`` (e.g.
        ``cave_small_tileset_``). Returns :data:`DEFAULT_TILE_SIZE` (400) when the
        prefix is empty, unknown, or content is unavailable — 400 is elmforest's
        size and the safe default."""
        if not tile_set_prefix:
            return DEFAULT_TILE_SIZE
        self._ensure_tile_sizes()
        return (self._tile_sizes or {}).get(tile_set_prefix.lower(),
                                            DEFAULT_TILE_SIZE)

    @property
    def cobj_file_count(self) -> int:
        self._ensure_indexed()
        return len(self._cobj or {})

    @property
    def tile_file_count(self) -> int:
        self._ensure_indexed()
        return len(self._tile or {})

    def variants_for(self, tile_set: str) -> List[str]:
        """Return a room-node ``tile_set``'s concrete variant tile leaves, sorted
        alphabetically (the client's per-tileset variant order — see
        :data:`_VARIANT_SUFFIX`). Empty when the set has no exit-suffixed tiles,
        in which case the maze falls back to the bare ``tile_set``."""
        if not tile_set:
            return []
        self._ensure_indexed()
        base = tile_set.lower()
        out: List[str] = []
        for leaf in (self._tile or {}):
            if leaf.startswith(base) and _VARIANT_SUFFIX.fullmatch(leaf[len(base):]):
                out.append(leaf)
        out.sort()
        return out

    def resolve_tile_path(self, tile_type_name: str) -> Optional[str]:
        if not tile_type_name:
            return None
        self._ensure_indexed()
        return (self._tile or {}).get(tile_type_name.lower())

    def resolve_cobj_path(self, extends_path: str) -> Optional[str]:
        if not extends_path:
            return None
        self._ensure_indexed()
        leaf = _leaf_of(extends_path).lower()
        return (self._cobj or {}).get(leaf)

    def resolve_extends_path(self, dotted_path: str) -> Optional[str]:
        """Resolve a dotted ``extends`` target to a base ``.gc``/``.tile`` file.

        A maze tile inherits its room shell (walls/floor + the SpawnPoint
        waypoint) from a base tile referenced as e.g.
        ``tiles.cave_small.portals.base.cave_small_exit_1n`` — a ``.gc`` living in
        a SUBDIRECTORY (``tiles/cave_small/portals/base/…``), not flat at the
        root, so the flat ``.tile`` index misses it. Maps dots → path separators
        and probes ``.gc`` then ``.tile`` under the extracter root. Returns
        ``None`` for terminal bases (``base.World``) or anything with no file."""
        if not dotted_path:
            return None
        root = self._resolve_root()
        if root is None:
            return None
        rel = dotted_path.replace(".", os.sep)
        for ext in (".gc", ".tile"):
            cand = os.path.join(root, rel + ext)
            if os.path.isfile(cand):
                return cand
            cand = os.path.join(root, rel.lower() + ext)
            if os.path.isfile(cand):
                return cand
        return None

    def load_cobj(self, extends_path: str) -> Optional[CobjData]:
        path = self.resolve_cobj_path(extends_path)
        return None if path is None else cobj_parser.parse_file(path)


def _leaf_of(dotted_path: str) -> str:
    dot = dotted_path.rfind(".")
    return dotted_path if dot < 0 else dotted_path[dot + 1:]


_state = _ResolverState()

# Module-level API (mirrors the C# static class).
resolve_tile_path = _state.resolve_tile_path
resolve_cobj_path = _state.resolve_cobj_path
resolve_extends_path = _state.resolve_extends_path
load_cobj = _state.load_cobj
variants_for = _state.variants_for
tile_size_for = _state.tile_size_for


def cobj_file_count() -> int:
    return _state.cobj_file_count


def tile_file_count() -> int:
    return _state.tile_file_count


def reset_index() -> None:
    """Drop the cached index (used by tests that point at a temp root)."""
    _state.reset()
