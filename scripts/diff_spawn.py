"""Diagnostic: byte-diff our generated player spawn against the working C#
`[PLAYER-SPAWN-HEX]` capture, printing the first diverging offset with context.

Usage:
    PYTHONPATH=. python3 scripts/diff_spawn.py <char_id> <cs_hex_file>

The cs_hex_file is a file containing one dash-separated hex line, e.g.
`07-01-DA-07-FF-...-46` (extract with `grep 'Size: NNNN hex:' Player.log`).
Throwaway harness, not a test.
"""
from __future__ import annotations

import sys

from drserver.db import game_database, character_repository
from drserver.core import settings
from drserver.net import spawn


def _build_ours(char_id: int) -> bytes:
    game_database.initialize("Database/dungeon_runners.db")
    settings.load()
    # The real server loads this at startup (game_server.py); get_mod_count
    # does not lazy-load, so without this every item reports 0 mod slots.
    from drserver.data.item_stat_database import item_stat_database
    item_stat_database.load()

    saved = character_repository.get_character(char_id)
    if saved is None:
        raise SystemExit(f"no character id={char_id}")

    captured: list[bytes] = []

    class CharHandle:
        id = saved.id
        name = saved.name

    class FakeServer:
        def __init__(self):
            self.selected_character = {saved.name: CharHandle()}
            self.next_entity_id = 10
            self.player_avatar_entity_id = {}

    class FakeConn:
        def __init__(self):
            self.login_name = saved.name
            self.conn_id = 1
            self.player_heading = 0.0
            self.current_zone_id = 0
            self.char_sql_id = saved.id
            self.avatar = None
            self.player = None
            self.unit_behavior_id = 0
            self.unit_container_id = 0
            self.modifiers_id = 0
            self.quest_manager_id = 0
            self.dialog_manager_id = 0
            self.equipment_component_id = 0

        def send_compressed_a(self, dest, mtype, inner):
            captured.append(inner)

    spawn.send_player_entity_spawn(FakeServer(), FakeConn())
    if not captured:
        raise SystemExit("no spawn payload captured")
    return captured[0]


def _load_cs(path: str) -> bytes:
    with open(path) as f:
        line = f.read().strip()
    parts = [p for p in line.replace("\n", "").split("-") if p]
    return bytes(int(p, 16) for p in parts)


def _ctx(b: bytes, i: int, span: int = 24) -> str:
    lo = max(0, i - span)
    hi = min(len(b), i + span)
    out = []
    for j in range(lo, hi):
        mark = ">" if j == i else " "
        out.append(f"{mark}{b[j]:02X}")
    return "".join(out)


def _hexa(b: bytes) -> str:
    """Hex + inline ascii for a short byte run."""
    if not b:
        return "(empty)"
    h = "-".join(f"{c:02X}" for c in b)
    a = "".join(chr(c) if 32 <= c < 127 else "." for c in b)
    return f"{h}   |{a}|"


def main(char_id: int, cs_path: str) -> None:
    import difflib

    ours = _build_ours(char_id)
    cs = _load_cs(cs_path)
    print(f"ours: {len(ours)} bytes   C#: {len(cs)} bytes   (delta {len(ours) - len(cs):+d})\n")

    sm = difflib.SequenceMatcher(a=ours, b=cs, autojunk=False)
    # 'replace' of equal length = likely entity-id value diff (benign, no shift).
    # 'insert'/'delete', or 'replace' with unequal lengths = STRUCTURAL (the bug).
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        olen, clen = i2 - i1, j2 - j1
        benign = tag == "replace" and olen == clen and olen <= 4
        flag = "   (benign value diff — likely entity id)" if benign else "   <<< STRUCTURAL"
        print(f"[{tag}] ours[{i1}:{i2}] ({olen}B) vs C#[{j1}:{j2}] ({clen}B){flag}")
        print(f"    ours @0x{i1:04X}: {_hexa(ours[i1:i2])}")
        print(f"    C#   @0x{j1:04X}: {_hexa(cs[j1:j2])}")
        if not benign:
            print(f"    ours ctx: ...{_ctx(ours, i1)}...")
            print(f"    C#   ctx: ...{_ctx(cs, j1)}...")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: diff_spawn.py <char_id> <cs_hex_file>")
    main(int(sys.argv[1]), sys.argv[2])
