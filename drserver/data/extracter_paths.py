"""Robust resolution of the extracter root (and its Desktop parent), shared by
the server runtime and the build scripts.

The extracter (the 19,824 unpacked client files) lives OUTSIDE the repo, as a
sibling of the repo root (``…/Desktop/{dr_pyserver, extracter}``). The server
runs under two filesystem views — native Windows (``C:\\…``) and WSL
(``/mnt/c/…``) — so the location must be *discovered*, never hard-coded to one
drive or one path separator.

The old heuristic — ``"/mnt/c" if os.path.isdir("/mnt/c") else "C:"`` — is WRONG
on native Windows: Windows resolves the POSIX-looking ``/mnt/c`` against the
current drive (``C:\\mnt\\c``), so if such a folder happens to exist the
heuristic takes the WSL branch and then ``os.path.join`` (ntpath) appends with a
backslash, yielding a mangled ``/mnt/c/Users/…/extracter\\GCDictionary.dict``
that no Windows ``open()`` can find. Probe real directories instead.

Resolution order (first hit wins):
1. ``DR_EXTRACTER_DIR`` / ``DR_EXTRACTER_ROOT`` env override (both names honored).
2. The repo-sibling ``…/<repo parent>/extracter`` — drive/view agnostic.
3. A ``~/Desktop/extracter`` fallback.
"""
from __future__ import annotations

import os
from typing import List, Optional

# Both env-var spellings have been used historically (``modpal_pool`` used
# ``DR_EXTRACTER_DIR``; ``tile_cobj_resolver`` used ``DR_EXTRACTER_ROOT``).
# Honor both so no existing configuration breaks.
_ENV_VARS = ("DR_EXTRACTER_DIR", "DR_EXTRACTER_ROOT")


def _repo_root() -> str:
    # extracter_paths.py -> drserver/data -> drserver -> repo root.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def _env_override() -> Optional[str]:
    for var in _ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val
    return None


def candidate_roots() -> List[str]:
    """Plausible extracter locations, most-reliable first.

    The first candidate is derived from this module's own path (the extracter is
    a repo sibling), so it resolves under whatever filesystem view the server
    runs in without a hard-coded drive. The absolute fallbacks cover an extracter
    that is not a repo sibling.
    """
    sibling = os.path.join(os.path.dirname(_repo_root()), "extracter")
    home_desktop = os.path.join(os.path.expanduser("~"), "Desktop", "extracter")
    return [sibling, home_desktop]


def resolve_extracter_dir() -> Optional[str]:
    """The first extracter root that actually exists, or ``None`` if none do.

    Honors the env override first (only when it points at a real directory).
    """
    env = _env_override()
    if env and os.path.isdir(env):
        return env
    for cand in candidate_roots():
        if os.path.isdir(cand):
            return cand
    return None


def extracter_dir() -> str:
    """A usable extracter path for ``open()``/``os.path.join``.

    Returns the first existing root; when none exist, returns a best-guess
    default (the env override if set, else the repo sibling) so callers still
    log an informative, well-formed path instead of a mangled mixed-separator
    one.
    """
    resolved = resolve_extracter_dir()
    if resolved:
        return resolved
    return _env_override() or candidate_roots()[0]


def desktop_dir() -> str:
    """The user's Desktop directory (the repo's parent).

    Build scripts also reference other Desktop siblings (the C# reference
    servers). Derive it from the repo path first — the repo lives at
    ``…/Desktop/dr_pyserver`` — then fall back to probing the absolute WSL and
    Windows locations.
    """
    parent = os.path.dirname(_repo_root())
    if os.path.isdir(parent):
        return parent
    home_desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(home_desktop):
        return home_desktop
    return parent
