"""Faithful (re)generation of ``class_definitions`` starting gear from the client.

Ground truth = the extracted client ``extracter/avatar/classes/*StartingEquipment.gc``
files (tier-2 client content — authoritative; see the source-of-truth hierarchy in
``CLAUDE.md``). Each file describes the items a fresh character of that class spawns
wearing, as a tree of top-level ``* extends <gc_key>`` blocks, each tagged with an
``ID`` (the equipment slot) and possibly nesting a weapon-modifier ``* extends`` that
is NOT itself an equipped item::

    FighterStartingEquipment
    {
        * extends items.pal.1HMacePAL.Normal001
        {
            ID = 10;                                  // slot 10 = weapon
            Level = 1;
            * extends items.modpal.LevelPrefixModPAL.Weapon01.Mod1   // a MOD, skip
            { }
        }
        * extends ScaleArmor1Pal.ScaleArmor1-1 { ID = 6; ... }       // slot 6 = armor
        ...
    }

The legacy ``class_definitions`` rows (inherited from the C# emulator) happened to
match the client after the items-namespace re-key (see
[[project_db_content_table_validation]]), but were hand-authored. This module derives
them straight from the client files so their provenance is reproducible.

The client equips only weapon/armor/gloves/boots at level 1; the remaining slots
(helmet/shoulders/shield/rings/amulet) are left untouched. The Fighter's "Cardboard"
weapon mod is intentionally dropped — ``class_definitions`` has no modifier column.
The client's ``Warlock`` class is the DB's ``Mage``.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Dict

# Equipment slot ID -> class_definitions column. Mirrors the live wire slot map
# (``drserver.net.equipment._SLOT_TO_DB``); a test pins them together.
SLOT_TO_COLUMN: Dict[int, str] = {
    1: "amulet", 2: "gloves", 3: "ring1", 4: "ring2", 5: "helmet",
    6: "armor", 7: "boots", 8: "shoulders", 10: "weapon", 11: "shield",
}

# Client gc file stem -> DB class_name (client "Warlock" == DB "Mage").
CLASS_FILE_TO_DB: Dict[str, str] = {
    "Fighter": "Fighter",
    "Warlock": "Mage",
    "Ranger": "Ranger",
}

_EXTENDS_RE = re.compile(r"^\*\s+extends\s+(\S+)")
_ID_RE = re.compile(r"^ID\s*=\s*(\d+)\s*;")


def parse_starting_equipment(text: str) -> Dict[int, str]:
    """Parse one ``*StartingEquipment.gc`` body into ``{slot_id: gc_key_lower}``.

    Only the top-level equipped items (blocks directly inside the class body) are
    returned; nested ``* extends`` blocks (e.g. weapon modifiers) are excluded.
    Keys are lowercased to match the ``items``/``weapons``/``armor`` table keys.
    """
    out: Dict[int, str] = {}
    depth = 0
    pending_key: str | None = None
    # Stack of per-block frames: each {"key", "id", "depth"}.
    stack: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _EXTENDS_RE.match(line)
        if m:
            pending_key = m.group(1)
            # An "* extends X { ... }" may open its brace on the same line.
            if "{" not in line:
                continue

        if "{" in line:
            depth += 1
            stack.append({"key": pending_key, "id": None, "depth": depth})
            pending_key = None
            # fall through in case "}" also on this line (single-line block)

        idm = _ID_RE.match(line)
        if idm and stack:
            stack[-1]["id"] = int(idm.group(1))

        if "}" in line:
            frame = stack.pop()
            # Top-level equipped items are the blocks at depth 2 (the class body
            # is depth 1). Nested blocks (mods) sit deeper and are skipped.
            if frame["depth"] == 2 and frame["key"] and frame["id"] is not None:
                out[frame["id"]] = frame["key"].lower()
            depth -= 1

    return out


def import_class_equipment(conn: sqlite3.Connection, classes_dir: str) -> int:
    """Regenerate ``class_definitions`` gear columns from the client class files.

    Reads ``<classes_dir>/<Class>StartingEquipment.gc`` for each known class and
    UPDATEs the matching ``class_definitions`` row's gear columns. Returns the
    number of class rows updated. Idempotent and additive: only the slots the
    client equips are written; unrelated columns are left untouched. A missing
    directory or file is skipped (returns the count of classes actually updated).
    """
    updated = 0
    for stem, class_name in CLASS_FILE_TO_DB.items():
        path = os.path.join(classes_dir, f"{stem}StartingEquipment.gc")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="latin-1") as fh:
            slots = parse_starting_equipment(fh.read())

        cols = {
            SLOT_TO_COLUMN[sid]: key
            for sid, key in slots.items()
            if sid in SLOT_TO_COLUMN
        }
        if not cols:
            continue

        set_clause = ", ".join(f"{col} = :{col}" for col in cols)
        params = dict(cols)
        params["class_name"] = class_name
        cur = conn.execute(
            f"UPDATE class_definitions SET {set_clause} WHERE class_name = :class_name",
            params,
        )
        updated += cur.rowcount
    return updated
