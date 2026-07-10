"""Loot ground-drop creates use the client-valid `itemobject` entity type.

Regression for the 0xC000 `Invalid entity type` crash: the entity-create type
must be the world-object class `itemobject` (the item's own gc class goes inside
the init body), matching C# SendGoldPileSpawnPacket / SendDroppedItemSpawnPacket.
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers import loot, loot_roller
from drserver.managers.loot_roller import PoolItem


@pytest.fixture(autouse=True)
def _seeded_loot_rng(monkeypatch):
    """Pin the roller's RNG. Drop chances are probabilistic (boss items 0.75,
    not 1.0), so asserting `len(sent) >= 1` on an unseeded Random flaked the
    suite intermittently. The pinned seed rolls a drop for every generator these
    tests use — the assertions test the WIRE SHAPE, not the drop odds."""
    real_random = random.Random
    monkeypatch.setattr(loot_roller.random, "Random",
                        lambda *a, **k: real_random(7))

# A deterministic single-item pool so item generators drop a known gc (the real
# roller loads this from the content DB, absent in the unit-test environment).
_POOL_ITEM = "1haxe1pal.1haxe1-1"


def _inject_pool():
    loot_roller._pool_cache = [PoolItem(_POOL_ITEM, 1)]


class _Conn:
    def __init__(self):
        self.is_spawned = True
        self.current_zone_gc_type = "world.tutorial"
        self.instance_id = 0
        self.sent = []

    def send_to_client(self, data):
        self.sent.append(data)


class _Server:
    def __init__(self, conn):
        self.connections = {1: conn}


def _spawn(generators):
    conn = _Conn()
    loot.generate_loot_for_monster(
        _Server(conn), conn, pos_x=10.0, pos_y=20.0, pos_z=5.0, level=3,
        treasure_generators=generators)
    return conn.sent


def _assert_well_formed_create(packet: bytes):
    assert packet[0] == 0x07, "must open with BeginStream"
    assert packet[-1] == 0x06, "must close with EndStream"
    # 0x07 0x01 <u16 eid> 0xFF "itemobject\0" ...
    assert packet[1] == 0x01, "first sub-message is CreateEntity"
    assert packet[4] == 0xFF, "gc-type marker precedes the entity class"
    assert b"itemobject\x00" in packet, "create type MUST be the world-object class"
    # The entity-create class must NOT be the item's own gc (the 0xC000 crash):
    # "itemobject\0" must appear immediately after the 0xFF marker at offset 4.
    assert packet[5:5 + len(b"itemobject\x00")] == b"itemobject\x00"


def test_gold_generator_drops_a_gold_pile():
    # BossGG drops on every activation (chance 1.0) — deterministic without a seed
    # (DefaultGG is now probabilistic: gold is no longer every kill).
    sent = _spawn([("BossGG", 1)])

    assert len(sent) == 1
    _assert_well_formed_create(sent[0])
    assert b"Currency\x00" in sent[0], "gold pile uses the native Currency (coin-pile) gc"


def test_item_generator_drops_pool_items():
    _inject_pool()
    sent = _spawn([("BossIG", 1)])            # boss tier reliably drops 2-4 items

    assert len(sent) >= 1
    for pkt in sent:
        _assert_well_formed_create(pkt)
    assert (_POOL_ITEM + "\x00").encode() in b"".join(sent)


def test_mixed_generators_drop_gold_and_items():
    _inject_pool()
    sent = _spawn([("BossIG", 1), ("BossGG", 1)])     # both tiers always drop (chance 1.0)

    blob = b"".join(sent)
    for pkt in sent:
        _assert_well_formed_create(pkt)
    assert b"Currency\x00" in blob                         # gold pile (coin-pile) present
    assert (_POOL_ITEM + "\x00").encode() in blob          # at least one rolled item


def test_no_generators_sends_nothing():
    assert _spawn([]) == []


def test_drops_only_broadcast_within_the_same_instance():
    killer = _Conn()
    other_instance = _Conn()
    other_instance.instance_id = 99
    other_zone = _Conn()
    other_zone.current_zone_gc_type = "world.town"
    server = _Server(killer)
    server.connections = {1: killer, 2: other_instance, 3: other_zone}

    loot.generate_loot_for_monster(server, killer, 0.0, 0.0, 0.0, level=1,
                                   treasure_generators=[("BossGG", 1)])

    assert len(killer.sent) == 1
    assert other_instance.sent == []
    assert other_zone.sent == []


def test_drop_item_near_player_spawns_and_registers_a_clickable_drop():
    """The bag-independent reward drop (wishing-well prize / full-bag reward)
    spawns a client-valid itemobject at the player's feet, broadcasts it to the
    instance (the dropping player included), and registers it for pickup."""
    conn = _Conn()
    conn.login_name = "Styx3"
    conn.player_pos_x, conn.player_pos_y, conn.player_pos_z = 100.0, 200.0, 5.0
    conn.player_level = 7
    server = _Server(conn)

    eid = loot.drop_item_near_player(server, conn, _POOL_ITEM, level=7)

    assert eid is not None
    assert len(conn.sent) == 1                       # the dropping player sees it
    _assert_well_formed_create(conn.sent[0])
    assert (_POOL_ITEM + "\x00").encode() in conn.sent[0]
    drop = loot.find_drop(eid)                        # registered -> clickable
    assert drop is not None and drop.gc_class == _POOL_ITEM
    # Dropped near the player's feet, not at world origin.
    assert abs(drop.pos_x - 100.0) <= 3.0 and abs(drop.pos_y - 200.0) <= 3.0


def test_drop_item_near_player_ignores_empty_gc():
    conn = _Conn()
    conn.player_pos_x = conn.player_pos_y = conn.player_pos_z = 0.0
    assert loot.drop_item_near_player(_Server(conn), conn, "") is None
    assert conn.sent == []


def test_ground_z_at_falls_back_without_a_pathmap():
    """No zone / no pathmap coverage → keep the caller's (stale) Z verbatim, so
    the fix can never regress a zone the pathmap doesn't cover."""
    import types
    conn = types.SimpleNamespace(current_zone_name="", instance_id=0)
    assert loot.ground_z_at(conn, 10.0, 20.0, 49.4) == 49.4


def test_ground_z_at_resolves_real_floor_from_town_pathmap():
    """The stale conn.player_pos_z (town spawn 49.4) must be replaced by the real
    floor at the drop point — the well platform is ~142, not 49 (live 2026-07-02:
    wishing-well items dropped ~92u underground because the drop used the stale Z)."""
    import os
    import sys
    import types
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _paths
    if not _paths.has_shipped_db():
        return  # shipped content DB absent — skip (fallback path covered above)
    from drserver.db import game_database
    if game_database.get_db_path() is None:
        game_database.initialize(_paths.copy_shipped_db())

    conn = types.SimpleNamespace(current_zone_name="town", instance_id=0)
    z = loot.ground_z_at(conn, 419.0, 336.0, 49.4)   # the well platform
    assert z > 100.0, f"expected the raised well floor (~142), got {z}"
    assert z != 49.4                                  # not the stale spawn Z


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
