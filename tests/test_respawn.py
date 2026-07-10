"""Respawn routing.

Two inbound triggers exist for the "Respawn" button:

1. Channel-7 opcode 0x04 entity-request (no payload) — the C#-faithful path
   (HandleClientRequestRespawn), ported in movement._handle_entity_request. The
   live native client never actually uses this for the Respawn button, but it is a
   correct C# port and harmless, so it stays.
2. Channel-13 message_type 0x08 (body 0x01) — the REAL trigger the live client
   sends. Confirmed by wire capture 2026-06-01: five Respawn clicks produced five
   identical `ch=13 type=0x08 len=1 head=01` messages, and nothing else. Both
   drserver and the C# server previously routed ch13/0x08 to the zone-load handler
   (its catch-all `else`), so the button did nothing. Guarded on is_spawned so a
   stray pre-spawn 0x08 still falls through to the zone-load handler.

Stat requests (0x11/0x12/0x13) on the 0x04 path must NOT be treated as respawns.
"""
from drserver.net.framing import ChannelMessage
from drserver.net.game_server import GameServer
from drserver.net.movement import _handle_entity_request
from drserver.util.byte_io import LEReader


class _FakeConn:
    login_name = "Styx3"
    current_zone_gc_type = "world.tutorial"


class _FakeServer:
    def __init__(self):
        self.respawned = 0

    def request_respawn(self, conn):
        self.respawned += 1


def _req(payload: bytes):
    server, conn = _FakeServer(), _FakeConn()
    _handle_entity_request(server, conn, LEReader(payload))
    return server.respawned


def test_empty_request_triggers_respawn():
    # No payload (the Respawn button) -> respawn.
    assert _req(b"") == 1


def test_short_request_triggers_respawn():
    # <3 bytes -> respawn.
    assert _req(b"\x01\x02") == 1


def test_stat_spend_request_does_not_respawn():
    # entityId(u16) + 0x11 (SpendAttribPoint) -> not a respawn.
    assert _req(b"\x10\x00\x11") == 0


def test_unknown_request_type_triggers_respawn():
    # entityId(u16) + unknown requestType -> respawn (C# default branch).
    assert _req(b"\x10\x00\x7E") == 1


# ── Channel-13 message_type 0x08 — the live Respawn trigger ────────────────────

class _DispatchConn:
    is_spawned = True
    login_name = "Styx3"
    current_zone_id = 0
    current_zone_gc_type = "world.tutorial"


class _DispatchServer:
    def __init__(self):
        self.respawned = 0
        self.zone_channel_calls = 0

    def request_respawn(self, conn):
        self.respawned += 1

    def _handle_zone_channel(self, conn, body):
        self.zone_channel_calls += 1


def _dispatch_ch13(message_type: int, payload: bytes, *, is_spawned: bool = True):
    server, conn = _DispatchServer(), _DispatchConn()
    conn.is_spawned = is_spawned
    GameServer._dispatch_channel(server, conn, ChannelMessage(13, message_type, payload))
    return server


def test_ch13_type8_spawned_triggers_respawn():
    # The live capture: ch=13 type=0x08 len=1 head=01 while spawned -> respawn.
    server = _dispatch_ch13(0x08, b"\x01", is_spawned=True)
    assert server.respawned == 1
    assert server.zone_channel_calls == 0


def test_ch13_type8_prespawn_falls_through_to_zone():
    # Defensive guard: a 0x08 before the player has spawned must NOT respawn;
    # it falls through to the existing zone-load handler instead.
    server = _dispatch_ch13(0x08, b"\x01", is_spawned=False)
    assert server.respawned == 0
    assert server.zone_channel_calls == 1


def test_ch13_other_type_does_not_respawn():
    # An ordinary zone message (e.g. type 0x06) must still go to the zone handler.
    server = _dispatch_ch13(0x06, b"", is_spawned=True)
    assert server.respawned == 0
    assert server.zone_channel_calls == 1


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
