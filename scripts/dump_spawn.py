"""Diagnostic: build the player spawn packet for an existing character and dump
the raw (pre-compression) inner bytes as hex, so we can structurally diff against
the working C# `[PLAYER-SPAWN-HEX]` reference. Not a test — throwaway harness.
"""
from __future__ import annotations

import sys

from drserver.db import game_database, character_repository
from drserver.core import settings
from drserver.net import spawn


class _Cap:
    """Minimal stand-ins capturing the inner spawn payload."""
    def __init__(self):
        self.captured = []


def main(char_id: int) -> None:
    game_database.initialize("Database/dungeon_runners.db")
    settings.load()

    saved = character_repository.get_character(char_id)
    if saved is None:
        print(f"no character id={char_id}")
        return
    print(f"char: name={saved.name} class={saved.class_name} level={saved.level}")

    captured: list[bytes] = []

    # Fake character handle (spawn uses .id and .name)
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
        print("no spawn payload captured")
        return
    payload = captured[0]
    print(f"spawn inner payload: {len(payload)} bytes, first=0x{payload[0]:02X} last=0x{payload[-1]:02X}")
    for i in range(0, len(payload), 16):
        chunk = payload[i:i + 16]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{i:08X}  {hexs:<48}  |{ascii_}|")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
