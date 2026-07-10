"""Same-zone respawn must reposition in place, NOT reload the zone.

Live 2026-06-01: cross-zone respawn (dungeon -> tutorial) works end-to-end, but
a *same-zone* respawn (die in tutorial -> respawn in tutorial) stalled with the
generic "there was an error talking to the server." popup. Root cause: the live
native client will not reload the zone it is already in — it never sends the
13/6 join reply — so the disconnect(13/02)+connect(13/00) dance in _transfer_zone
strands the player (instance torn down, is_spawned cleared, no re-spawn). Every
self-referential respawn_zone (town/tutorial/thehub/pvp_*) hits this.

Fix: when the respawn target == the current zone, reposition the avatar in place
(restore HP, move to respawn waypoint, re-send the avatar spawn/control/mover
packets) WITHOUT a zone reload, keeping is_spawned and the tick intact.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.net import game_server as game_server_module
from drserver.net.game_server import GameServer


# ── Test doubles ──────────────────────────────────────────────────────────────
class _Zone:
    def __init__(self, zone_id, name, spawn=(750.0, 450.0, 39.0)):
        self.id = zone_id
        self.name = name
        self.spawn_x, self.spawn_y, self.spawn_z = spawn


class _Registry:
    def __init__(self, *zones):
        self._by_name = {z.name: z for z in zones}

    def find_by_name(self, name):
        return self._by_name.get(name)


class _MsgQueue:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1

    def is_empty(self):
        return True


class _Conn:
    def __init__(self, zone_id, zone_name):
        self.login_name = "Styx3"
        self.current_zone_id = zone_id
        self.current_zone_name = zone_name
        self.current_zone_gc_type = "world.tutorial"
        self.is_spawned = True
        self.allow_flush = True
        self.unit_behavior_id = 533
        self.session_id = 0x10
        self.player_heading = 0.0
        self.player_pos_x = 10.0
        self.player_pos_y = 20.0
        self.player_pos_z = 30.0
        self.hp_wire = 68096
        self.message_queue = _MsgQueue()
        self._tick_task = None
        # Per-connection merchant-refresh watchdog state (armed when the player
        # clicks a vendor; flushed once-a-second by the movement tick loop).
        self.active_merchant_npc = None
        self.active_merchant_cid = 0
        self.active_merchant_due = 0.0
        self.sent: list[bytes] = []

    def send_to_client(self, packet: bytes) -> None:
        self.sent.append(packet)

    def send_system_message(self, msg: str) -> None:
        self.sent.append(b"SYS:" + msg.encode())


def _make_server():
    """A GameServer without the heavy async __init__."""
    return object.__new__(GameServer)


def _channel_pairs(sent):
    """Return [(channel, message_type)] for the leading 2 bytes of each packet."""
    return [(p[0], p[1]) for p in sent if len(p) >= 2]


# ── Same-zone respawn: reposition in place ─────────────────────────────────────
def test_same_zone_respawn_does_not_send_zone_reload(monkeypatch):
    # Arrange
    tut = _Zone(7, "tutorial")
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(tut))
    gs = _make_server()
    # Avoid the real HP-recompute (needs the character DB) and the asyncio tick.
    monkeypatch.setattr(gs, "_refresh_avatar_hp_wire", lambda conn: None)
    from drserver.net import movement
    monkeypatch.setattr(movement, "start_tick", lambda server, conn: None)
    conn = _Conn(zone_id=7, zone_name="tutorial")

    # Act — respawn into the same zone the player is already in.
    gs._transfer_zone(conn, "tutorial")

    # Assert — NO zone disconnect (13/02) or connect (13/00) was sent.
    pairs = _channel_pairs(conn.sent)
    assert (13, 0x02) not in pairs, "same-zone respawn must not send zone disconnect"
    assert (13, 0x00) not in pairs, "same-zone respawn must not send zone connect"


def test_same_zone_respawn_keeps_player_spawned(monkeypatch):
    tut = _Zone(7, "tutorial")
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(tut))
    gs = _make_server()
    monkeypatch.setattr(gs, "_refresh_avatar_hp_wire", lambda conn: None)
    from drserver.net import movement
    monkeypatch.setattr(movement, "start_tick", lambda server, conn: None)
    conn = _Conn(zone_id=7, zone_name="tutorial")

    gs._transfer_zone(conn, "tutorial")

    # The player must NOT be torn down / stranded.
    assert conn.is_spawned is True
    assert conn.message_queue.cleared == 0, "in-place respawn must not clear the queue"


def test_same_zone_respawn_moves_avatar_to_respawn_point(monkeypatch):
    tut = _Zone(7, "tutorial", spawn=(750.0, 450.0, 39.0))
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(tut))
    gs = _make_server()
    monkeypatch.setattr(gs, "_refresh_avatar_hp_wire", lambda conn: None)
    from drserver.net import movement
    monkeypatch.setattr(movement, "start_tick", lambda server, conn: None)
    conn = _Conn(zone_id=7, zone_name="tutorial")

    gs._transfer_zone(conn, "tutorial")

    # Avatar repositioned to the zone's respawn/default spawn.
    assert (conn.player_pos_x, conn.player_pos_y, conn.player_pos_z) == (750.0, 450.0, 39.0)
    # And at least the avatar-alive packets (component update 0x07) were sent.
    assert any(p[0] == 0x07 for p in conn.sent), "expected avatar spawn-state packets"


def test_same_zone_respawn_rearms_tick(monkeypatch):
    tut = _Zone(7, "tutorial")
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(tut))
    gs = _make_server()
    monkeypatch.setattr(gs, "_refresh_avatar_hp_wire", lambda conn: None)
    from drserver.net import movement
    started = []
    monkeypatch.setattr(movement, "start_tick", lambda server, conn: started.append(conn))
    conn = _Conn(zone_id=7, zone_name="tutorial")

    gs._transfer_zone(conn, "tutorial")

    assert started == [conn], "the heartbeat tick must be (re-)armed in place"


# ── Cross-zone respawn: the guard must NOT fire ────────────────────────────────
def test_cross_zone_respawn_still_reloads(monkeypatch):
    # target id (9) != current zone id (7) -> the same-zone guard must not fire,
    # so the normal disconnect/connect reload still happens.
    tut = _Zone(7, "tutorial")
    town = _Zone(9, "town")
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(town))
    gs = _make_server()
    # Stub the teardown collaborators the cross-zone path touches.
    from drserver.net import spawn as spawn_module
    monkeypatch.setattr(spawn_module, "broadcast_entity_remove", lambda s, c: None)

    class _Instances:
        def leave(self, server, conn):
            pass

    gs.world_instances = _Instances()
    gs.remote_behavior_ids = {}
    gs.remote_avatar_ids = {}
    gs.remote_player_ids = {}
    gs.remote_manip_ids = {}
    gs.spawned_avatar_ids = {}
    conn = _Conn(zone_id=7, zone_name="tutorial")

    gs._transfer_zone(conn, "town")

    pairs = _channel_pairs(conn.sent)
    assert (13, 0x02) in pairs, "cross-zone respawn must still send zone disconnect"
    assert (13, 0x00) in pairs, "cross-zone respawn must still send zone connect"


# ── Armed-merchant watchdog must not survive a cross-zone transfer ─────────────
def test_cross_zone_clears_armed_merchant(monkeypatch):
    """A vendor armed in the old zone must be disarmed when the player leaves it.

    Live 2026-06-17: player talked to the tutorial HermitVendor, walked into a
    dungeon, and ~300s later the once-a-second restock watchdog flushed a
    `0x35 <hermit_cid> 0x1E/0x1F` ComponentUpdate into the dungeon zone — where
    that component id does not resolve -> "Invalid ComponentID" -> Zone
    communication error Code 9. The merchant cid is per-zone, so the armed
    state must be cleared on cross-zone transfer.
    """
    tut = _Zone(7, "tutorial")
    town = _Zone(9, "town")
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(town))
    gs = _make_server()
    from drserver.net import spawn as spawn_module
    monkeypatch.setattr(spawn_module, "broadcast_entity_remove", lambda s, c: None)

    class _Instances:
        def leave(self, server, conn):
            pass

    gs.world_instances = _Instances()
    gs.remote_behavior_ids = {}
    gs.remote_avatar_ids = {}
    gs.remote_player_ids = {}
    gs.remote_manip_ids = {}
    gs.spawned_avatar_ids = {}
    conn = _Conn(zone_id=7, zone_name="tutorial")
    # Player clicked the tutorial vendor: watchdog armed for its cid.
    conn.active_merchant_npc = "world.tutorial.npc.HermitVendor"
    conn.active_merchant_cid = 0x021C
    conn.active_merchant_due = 0.0   # due elapsed -> watchdog would flush

    gs._transfer_zone(conn, "town")

    assert conn.active_merchant_npc is None, "armed vendor must be disarmed on zone change"
    assert conn.active_merchant_cid == 0, "stale merchant cid must be cleared"


def test_same_zone_respawn_keeps_armed_merchant(monkeypatch):
    """In-place respawn stays in the same zone, so the vendor cid is still valid
    and the armed watchdog must be left intact."""
    tut = _Zone(7, "tutorial")
    monkeypatch.setattr(game_server_module, "zone_registry", _Registry(tut))
    gs = _make_server()
    monkeypatch.setattr(gs, "_refresh_avatar_hp_wire", lambda conn: None)
    from drserver.net import movement
    monkeypatch.setattr(movement, "start_tick", lambda server, conn: None)
    conn = _Conn(zone_id=7, zone_name="tutorial")
    conn.active_merchant_npc = "world.tutorial.npc.HermitVendor"
    conn.active_merchant_cid = 0x021C

    gs._transfer_zone(conn, "tutorial")

    assert conn.active_merchant_npc == "world.tutorial.npc.HermitVendor"
    assert conn.active_merchant_cid == 0x021C


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0

    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value=None):
            if value is None:
                # object-attr form: setattr(obj, "name", value) used as (obj, name, value)
                raise RuntimeError("two-arg setattr unsupported in shim")
            old = getattr(target, name, None)
            self._undo.append((target, name, old))
            setattr(target, name, value)

        def undo(self):
            for t, n, o in reversed(self._undo):
                try:
                    setattr(t, n, o)
                except Exception:
                    pass

    for fn in funcs:
        mp = _MP()
        try:
            fn(mp)
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
