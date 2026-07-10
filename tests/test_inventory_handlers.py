"""Inventory handler integration tests — the live-bug paths, repo stubbed.

Covers the 2026-06-07 fixes end-to-end without a real DB:
  - UseItem resolves the client's slot id to the right item (off-by-one fix).
  - Pickup → Place moves an item via the cursor (place carries no item data).
  - Using a stack decrements it; using the last one removes it.
"""
from types import SimpleNamespace

import pytest

from drserver.data.saved_character import SavedCharacter, SavedInventoryItem
from drserver.net import inventory as inv
from drserver.net.inventory_model import InventoryModel
from drserver.util.byte_io import LEReader, LEWriter


@pytest.fixture
def stub_repo(monkeypatch):
    """In-memory single-character repository."""
    char = SavedCharacter(id=1, name="Tester", level=1)
    char.max_hp = char.current_hp = 200 * 256
    char.max_mana = char.current_mana = 200 * 256
    char.inventory = [
        SavedInventoryItem(gc_class="potionpal.healthpotion_noob", x=1, y=0, count=20),
        SavedInventoryItem(gc_class="potionpal.manapotion_noob", x=2, y=0, count=18),
        SavedInventoryItem(gc_class="SkillBookPAL.SummonBlingGnome", x=0, y=2, count=1),
    ]
    monkeypatch.setattr(inv.character_repository, "get_character", lambda _id: char)
    monkeypatch.setattr(inv.character_repository, "save_character", lambda c: None)
    return char


def _conn(char):
    conn = SimpleNamespace(
        conn_id=4, char_sql_id=1, player_level=1, login_name="Tester",
        unit_container_id=0x0202, modifiers_id=0x0203,
        hp_wire=200 * 256, client_hp_wire=None,
        inv_model=InventoryModel(), sent=[],
    )
    conn.send_to_client = conn.sent.append
    # Seed the model the way spawn does.
    conn.inv_model.load([
        {"gc_class": it.gc_class, "x": it.x, "y": it.y, "count": it.count}
        for it in char.inventory
    ])
    return conn


def _use_packet(slot_id: int) -> LEReader:
    w = LEWriter()
    w.write_uint32(slot_id)
    return LEReader(w.to_array())


def test_use_resolves_client_slot_to_correct_item(stub_repo):
    # Arrange — slot 2 is the mana potion (the old code returned items[2]=bling gnome).
    conn = _conn(stub_repo)

    # Act
    inv.handle_use_item(None, conn, _use_packet(2))

    # Assert — mana potion consumed (count 18 -> 17), still present.
    mana = conn.inv_model.by_slot(2)
    assert mana is not None and mana.gc_class == "potionpal.manapotion_noob"
    assert mana.count == 17


def test_use_last_in_stack_removes_item(stub_repo):
    # Arrange — bling gnome book has count 1 (slot 3).
    conn = _conn(stub_repo)

    # Act
    inv.handle_use_item(None, conn, _use_packet(3))

    # Assert — book gone.
    assert conn.inv_model.by_slot(3) is None


def test_pickup_then_place_moves_item_via_cursor(stub_repo):
    # Arrange
    conn = _conn(stub_repo)

    # Act — pick up the mana potion (slot 2)…
    pick = LEWriter(); pick.write_uint32(2)
    inv.handle_pickup_item(None, conn, LEReader(pick.to_array()))
    assert conn.inv_model.cursor is not None
    assert conn.inv_model.by_slot(2) is None

    # …then place it at a free cell (4,4). Place carries only [inv][x][y].
    place = LEWriter()
    place.write_byte(0x0B); place.write_byte(4); place.write_byte(4)
    inv.handle_place_item(None, conn, LEReader(place.to_array()))

    # Assert — cursor cleared, item now at (4,4) with a fresh slot id.
    assert conn.inv_model.cursor is None
    moved = conn.inv_model.by_grid(4, 4, inv._get_item_size)
    assert moved is not None and moved.gc_class == "potionpal.manapotion_noob"
    assert moved.count == 18


def test_use_health_potion_sends_buff_modifier(stub_repo):
    # Arrange — slot 1 is the noob health potion.
    conn = _conn(stub_repo)

    # Act
    inv.handle_use_item(None, conn, _use_packet(1))

    # Assert — among the emitted packets is a Modifiers Add (0x35 <modId> 0x00)
    # carrying the potion's "<item>.Modifier" buff (the heal-over-time + the use
    # animation). Without it the potion only decremented and never animated/healed.
    mod_cstr = b"potionpal.healthpotion_noob.Modifier\x00"
    add = b"\x35" + (0x0203).to_bytes(2, "little") + b"\x00\xFF" + mod_cstr
    assert any(add in pkt for pkt in conn.sent), \
        f"no potion modifier Add in {[p.hex() for p in conn.sent]}"
    # The active-modifier id is tracked so a re-quaff refreshes rather than dupes.
    assert getattr(conn, "_active_health_mod_id", 0) != 0


def test_place_with_empty_cursor_is_noop(stub_repo):
    # Arrange
    conn = _conn(stub_repo)
    place = LEWriter()
    place.write_byte(0x0B); place.write_byte(4); place.write_byte(4)

    # Act
    inv.handle_place_item(None, conn, LEReader(place.to_array()))

    # Assert — nothing placed, no packet sent.
    assert conn.inv_model.by_grid(4, 4, inv._get_item_size) is None
    assert conn.sent == []


if __name__ == "__main__":
    import sys
    import traceback

    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    failed = 0
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        try:
            char = None
            # minimal manual fixture for direct run
            import drserver.net.inventory as _inv
            sc = SavedCharacter(id=1, name="Tester", level=1)
            sc.max_hp = sc.current_hp = sc.max_mana = sc.current_mana = 200 * 256
            sc.inventory = [
                SavedInventoryItem(gc_class="potionpal.healthpotion_noob", x=1, y=0, count=20),
                SavedInventoryItem(gc_class="potionpal.manapotion_noob", x=2, y=0, count=18),
                SavedInventoryItem(gc_class="SkillBookPAL.SummonBlingGnome", x=0, y=2, count=1),
            ]
            _inv.character_repository.get_character = lambda _id, _sc=sc: _sc
            _inv.character_repository.save_character = lambda c: None
            fn(sc)
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
