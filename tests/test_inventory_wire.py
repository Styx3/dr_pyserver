"""Inventory UnitContainer wire-format tests — slot-map + cursor updates.

Regression guards:
  - 2026-05-30 crash: a UnitContainer ComponentUpdate without the WriteSynch
    trailer (0x02 + uint32 SynchHP) desyncs the stream and crashes the client.
  - 2026-06-07: the client echoes the assigned slot id on use/pickup; treating it
    as a 0-based list index picks the wrong item (off-by-one).

Pins the sub-message byte layout to the C# InventoryHandler.
"""
from types import SimpleNamespace

from drserver.data.gc_object_factory import create_equipment_item
from drserver.net import inventory as inv
from drserver.net.inventory_model import InventoryModel
from drserver.util.byte_io import LEReader, LEWriter

_GC = "items.pal.1HAxe1PAL.1HAxe1-1"
_LEVEL = 5
_HP_WIRE = 200 * 256


def _conn(uc_id: int = 0x0202) -> SimpleNamespace:
    return SimpleNamespace(unit_container_id=uc_id, hp_wire=_HP_WIRE,
                           player_level=_LEVEL, inv_model=InventoryModel())


def test_clear_active_is_synch_terminated():
    # Arrange
    conn = _conn()
    w = LEWriter()

    # Act
    inv._write_clear_active(w, conn)
    r = LEReader(w.to_array())

    # Assert
    assert r.read_byte() == 0x35            # ComponentUpdate
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x29            # clear active item (no body)
    assert r.read_byte() == 0x02            # WriteSynch flag
    assert r.read_uint32() == _HP_WIRE
    assert r.remaining == 0


def test_set_active_carries_equipment_item_and_synch():
    # Arrange
    conn = _conn()
    body = LEWriter()
    create_equipment_item(_GC).write_init_without_weapon_bytes(body, _LEVEL)
    item_body = body.to_array()
    w = LEWriter()

    # Act
    inv._write_set_active(w, conn, _GC, count=1, level=_LEVEL)
    r = LEReader(w.to_array())

    # Assert — C# pickup-handler set-active block (equipment path).
    assert r.read_byte() == 0x35
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x28            # set active item
    assert r.read_bytes(len(item_body)) == item_body
    assert r.read_byte() == 0x02
    assert r.read_uint32() == _HP_WIRE
    assert r.remaining == 0


def test_set_active_simple_item_uses_bare_cursor_format():
    # Arrange — consumables serialize with the bare (no ScaleMod) cursor format.
    conn = _conn()
    w = LEWriter()

    # Act
    inv._write_set_active(w, conn, "potionpal.healthpotion_noob", count=20, level=1)
    r = LEReader(w.to_array())

    # Assert
    assert r.read_byte() == 0x35
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x28
    assert r.read_byte() == 0xFF            # type tag
    r.read_cstring()
    assert r.read_uint32() == 0x00          # cursor has no grid slot
    assert r.read_byte() == 0x00            # x
    assert r.read_byte() == 0x00            # y
    assert r.read_byte() == 20              # stack count carried on cursor
    assert r.read_byte() == 0x01            # level
    assert r.read_byte() == 0x00            # flags
    assert r.read_byte() == 0x00            # mod count
    assert r.read_byte() == 0x02            # synch
    assert r.read_uint32() == _HP_WIRE


def test_item_removed_uses_client_slot_id():
    # Arrange
    conn = _conn()
    w = LEWriter()

    # Act
    inv._write_item_removed(w, conn, 7)
    r = LEReader(w.to_array())

    # Assert
    assert r.read_byte() == 0x35
    assert r.read_uint16() == conn.unit_container_id
    assert r.read_byte() == 0x1F            # ItemRemoved
    assert r.read_uint32() == 7
    assert r.read_byte() == 0x02
    assert r.read_uint32() == _HP_WIRE


if __name__ == "__main__":
    import sys
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
