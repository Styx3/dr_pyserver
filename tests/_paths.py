"""Single source of truth for filesystem paths used by the test suite.

Tests always run from the server directory (the repo root). Every path is
resolved relative to it, so no absolute or platform-specific path (``C:\\...``
or ``/mnt/c/...``) ever leaks into a test. The client lives outside the repo and
is referenced relative to the server directory too.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# tests/_paths.py -> tests/ -> repo root (the server directory).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Shipped content DB (zones, creatures, items, characters, ...). This is a FILE,
# not the ``Database/`` directory — a copy of the C# build's dungeon_runners.db.
SHIPPED_DB = os.path.join(REPO_ROOT, "Database", "dungeon_runners.db")

# Original DR client, referenced relative to the server directory.
CLIENT_DIR = os.path.normpath(os.path.join(REPO_ROOT, "..", "DungeonRunners", "Client666"))


def has_shipped_db() -> bool:
    """True when the shipped content DB is present."""
    return os.path.isfile(SHIPPED_DB)


def copy_shipped_db() -> str:
    """Copy the shipped DB to a fresh temp file and return its path.

    Tests open the copy so they never lock or mutate the live DB — the running
    server may hold it open in WAL mode, and connecting to it directly can block.

    The copy is deleted at interpreter exit — each copy is ~79 MB, and leaked
    copies have filled the 16 GB tmpfs ``/tmp`` (every test then fails with
    sqlite "disk I/O error" / ENOSPC).
    """
    tmp = tempfile.mkdtemp(prefix="drtest_")
    dest = os.path.join(tmp, "dr.db")
    shutil.copy(SHIPPED_DB, dest)
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    return dest
