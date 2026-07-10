"""Equipment wire-format tests — the unequip/equip ComponentUpdate streams.

Regression guard for the live-test crash (2026-05-30): unequipping an item
hard-crashed the client with "zone communication error code 2" because the
equipment ComponentUpdates were sent WITHOUT the per-update WriteSynch trailer
(0x02 + uint32 SynchHP), desyncing the client stream.

These tests pin the byte layout to the C# EquipmentHandler (HandleRemoveEquippedItem
+ WriteSynch): one BeginStream, three synch-terminated ComponentUpdates
(Equipment remove / UnitContainer set-active / Manipulators remove), one EndStream.
"""
import pytest

from drserver.data.gc_object_factory import create_equipment_item
from drserver.net.equipment import build_unequip_stream
from drserver.util.byte_io import LEReader, LEWriter

_GC = "items.pal.1HAxe1PAL.1HAxe1-1"
_LEVEL = 7
_HP_WIRE = 200 * 256


def _expected_item_body(gc_class: str, level: int) -> bytes:
    item = create_equipment_item(gc_class)
    w = LEWriter()
    item.write_init_without_weapon_bytes(w, level)
    return w.to_array()


def test_unequip_stream_is_three_synch_terminated_updates():
    # Arrange
    equip_id, uc_id, manip_id, slot = 0x0101, 0x0102, 0x0103, 10
    item = create_equipment_item(_GC)
    item_body = _expected_item_body(_GC, _LEVEL)

    # Act
    packet = build_unequip_stream(equip_id, uc_id, manip_id, slot, item, _LEVEL, _HP_WIRE)
    r = LEReader(packet)

    # Assert — exact field order from C# HandleRemoveEquippedItem.
    assert r.read_byte() == 0x07                       # BeginStream

    # Part 1: Equipment remove + synch
    assert r.read_byte() == 0x35
    assert r.read_uint16() == equip_id
    assert r.read_byte() == 0x29                       # RemoveEquippedItem
    assert r.read_uint32() == slot
    assert r.read_byte() == 0x02                       # WriteSynch flag
    assert r.read_uint32() == _HP_WIRE

    # Part 2: UnitContainer set active item + synch
    assert r.read_byte() == 0x35
    assert r.read_uint16() == uc_id
    assert r.read_byte() == 0x28                       # set active item
    assert r.read_bytes(len(item_body)) == item_body   # item.WriteInitWithoutWeaponBytes
    assert r.read_byte() == 0x02
    assert r.read_uint32() == _HP_WIRE

    # Part 3: Manipulators remove (0x01, NOT 0x1F) + synch
    assert r.read_byte() == 0x35
    assert r.read_uint16() == manip_id
    assert r.read_byte() == 0x01                       # Go/C# remove opcode
    assert r.read_uint32() == slot
    assert r.read_byte() == 0x02
    assert r.read_uint32() == _HP_WIRE

    assert r.read_byte() == 0x06                       # EndStream
    assert r.remaining == 0


def test_unequip_stream_carries_supplied_hp_in_every_synch():
    # Arrange — a distinct HP so we can spot a hard-coded default.
    hp = 1234 * 256
    item = create_equipment_item(_GC)

    # Act
    packet = build_unequip_stream(0x10, 0x11, 0x12, 6, item, _LEVEL, hp)

    # Assert — the synch HP appears exactly three times (one per update).
    needle = hp.to_bytes(4, "little")
    assert packet.count(needle) == 3


def test_unequip_stream_has_single_begin_and_end():
    # Arrange / Act
    item = create_equipment_item(_GC)
    packet = build_unequip_stream(0x10, 0x11, 0x12, 6, item, _LEVEL, _HP_WIRE)

    # Assert — exactly one BeginStream/EndStream wraps the whole packet.
    assert packet[0] == 0x07
    assert packet[-1] == 0x06


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
