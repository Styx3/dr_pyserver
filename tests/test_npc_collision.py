"""NPC-collision regression — guards the worldEntityFlags blocking bit.

NPCs must be solid (the player cannot walk through a vendor/trainer). The client
gates collision in WorldEntity::processInited @0x4d3560 on ``(flags & 1)`` —
when set it runs WorldCollisionManager::add + PathMap::Invalidate. C#
SendZoneNPCs shipped ``0x06`` (bit 0 clear) so NPCs were non-solid; the fix
sends ``0x07``. This test parses a real town NPC stream and asserts the
worldEntityFlags uint32 in the 0x02 init carries the blocking bit.
"""
import _paths
from drserver.db import game_database
from drserver.managers import npcs as npc_module
from drserver.util.byte_io import LEReader

if game_database.get_db_path() is None and _paths.has_shipped_db():
    game_database.initialize(_paths.copy_shipped_db())


class _FakeServer:
    def __init__(self) -> None:
        self.next_entity_id = 100
        self.merchant_components = {}
        self.npc_merchant_cids = {}
        self.trainer_components = {}

    def allocate_entity_id(self) -> int:
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid


def _read_worldentity_flags(packet: bytes, entity_id: int) -> int:
    """Walk a single-NPC stream to its 0x02 entity-init and return the flags
    uint32. Asserts the stream frames cleanly up to that point."""
    r = LEReader(packet)
    assert r.read_byte() == 0x07                       # BeginStream
    # Scan opcodes until the 0x02 init for this entity. Components (0x32) and the
    # create (0x01) precede it; we only need to reach the init reliably, so find
    # the 0x02 byte immediately followed by this entity id.
    data = packet
    needle_lo = entity_id & 0xFF
    needle_hi = (entity_id >> 8) & 0xFF
    for i in range(1, len(data) - 6):
        if data[i] == 0x02 and data[i + 1] == needle_lo and data[i + 2] == needle_hi:
            flags = (data[i + 3] | (data[i + 4] << 8)
                     | (data[i + 5] << 16) | (data[i + 6] << 24))
            return flags
    raise AssertionError("0x02 init for entity not found")


def test_town_npcs_are_solid_colliders():
    # Arrange
    server = _FakeServer()
    npc_module.npc_manager.load()

    # Act
    built = npc_module.build_zone_npcs(server, "town")

    # Assert — town has NPCs and each carries the blocking bit (flags & 1).
    assert built, "expected town NPCs"
    for entity_id, packet in built:
        flags = _read_worldentity_flags(packet, entity_id)
        assert flags & 0x01, f"NPC 0x{entity_id:X} not a collider (flags=0x{flags:X})"
        assert flags == 0x07, f"expected 0x07, got 0x{flags:X}"


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
