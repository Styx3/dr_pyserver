"""Mob engagement relay — mirror P1's fight onto same-instance viewers.

Native-mob model (bible §14.7): every client's own brain simulates its copy of
each mob, and the brain ignores DISPLAYED avatars — so when a mob aggros P1,
P2's copy keeps idling at spawn (live user report 2026-07-09: "when mobs aggro
p1, p2 doesn't see them" move). The server cannot know the owner-copy's exact
position (the client streams no mob state upstream, §6-LIVE.12), but it DOES
know the engagement: P1's 0x50 attack names the mob. This relay sends the
NON-engaged instance members a Follow CreateAction (the T0-proven
``0x04 0x16 <mode> <target:u16>`` shape, managers/monster_ai.py) aimed at the
engaging player's avatar, so their idle copy visibly chases/fights P1.

HP-safety: the trailer carries the mob's spawn max HP, and a viewer stops
being eligible the moment they attack the mob themselves — their local copy
then holds a self-computed (lower) HP and ANY server-asserted value would trip
the zero-tolerance compare (§4). The engaging OWNER never receives anything
(server packets break the native chase for the engaged client — §14.6 6n).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers import mob_engagement_relay as relay
from drserver.util.byte_io import LEReader


def _conn(login, zone="world.dungeon01.level01", instance=7, spawned=True):
    sent = []
    return SimpleNamespace(
        login_name=login,
        current_zone_gc_type=zone,
        instance_id=instance,
        is_spawned=spawned,
        send_to_client=lambda b, _s=sent: _s.append(bytes(b)),
        sent=sent,
    )


def _monster(eid=0x0123, behavior=0x0124, zone="world.dungeon01.level01",
             instance=7, max_hp=29184):
    return SimpleNamespace(
        entity_id=eid, behavior_id=behavior, zone_gc_type=zone,
        instance_id=instance, max_hp=max_hp, current_hp=max_hp)


def _server(*conns, monster=None, avatar_ids=None):
    monsters = {monster.entity_id: monster} if monster is not None else {}
    return SimpleNamespace(
        connections={c.login_name: c for c in conns},
        combat=SimpleNamespace(get_monster=lambda eid: monsters.get(eid)),
        spawned_avatar_ids=avatar_ids or {},
        get_player_avatar_id=lambda login: (avatar_ids or {}).get(login, 0),
    )


def setup_function(_fn):
    relay.reset()


def test_attack_relays_follow_to_non_engaged_viewer():
    # Arrange — P1 and P2 share the instance; P1's avatar eid is 0x01FE.
    p1, p2 = _conn("P1"), _conn("P2")
    mob = _monster()
    server = _server(p1, p2, monster=mob,
                     avatar_ids={"P1": 0x01FE, "P2": 0x02FE})

    # Act — P1 attacks the mob.
    relay.on_player_attack(server, p1, mob.entity_id, now=100.0)

    # Assert — P2's copy is told (FRAMED) to Follow P1's avatar; trailer = spawn
    # max HP (P2 never engaged: their local copy still holds exactly max).
    assert len(p2.sent) == 1
    r = LEReader(p2.sent[0])
    assert r.read_byte() == 0x07                            # BeginStream
    assert r.read_byte() == 0x35
    assert r.read_uint16() == mob.behavior_id
    assert (r.read_byte(), r.read_byte()) == (0x04, 0x16)   # CreateAction Follow
    assert r.read_byte() == 0x00                            # Follow mode
    assert r.read_uint16() == 0x01FE                        # chase P1's avatar
    assert r.read_byte() == 0x02
    assert r.read_uint32() == mob.max_hp
    assert r.read_byte() == 0x06                            # EndStream
    assert r.remaining == 0
    # The engaging owner gets NOTHING (native brain owns the fight on P1).
    assert p1.sent == []


def test_relay_throttled_per_mob():
    # Arrange
    p1, p2 = _conn("P1"), _conn("P2")
    mob = _monster()
    server = _server(p1, p2, monster=mob, avatar_ids={"P1": 0x01FE})

    # Act — a held attack button (~2 swings/s) must not spam Follow packets.
    relay.on_player_attack(server, p1, mob.entity_id, now=100.0)
    relay.on_player_attack(server, p1, mob.entity_id, now=100.4)
    relay.on_player_attack(server, p1, mob.entity_id, now=100.9)
    relay.on_player_attack(server, p1, mob.entity_id, now=101.1)

    # Assert — one initial + one re-assert after the 1 s throttle window.
    assert len(p2.sent) == 2


def test_engaged_viewer_is_excluded_from_relays():
    # Arrange — P2 attacked the same mob: their copy's HP is self-computed now,
    # so ANY server HP assertion (even max) can mismatch and crash them.
    p1, p2 = _conn("P1"), _conn("P2")
    mob = _monster()
    server = _server(p1, p2, monster=mob,
                     avatar_ids={"P1": 0x01FE, "P2": 0x02FE})

    # Act
    relay.on_player_attack(server, p2, mob.entity_id, now=99.0)
    p1.sent.clear()
    p2.sent.clear()
    relay.on_player_attack(server, p1, mob.entity_id, now=101.0)

    # Assert — both engaged: no relay in either direction for this mob.
    assert p2.sent == []
    assert p1.sent == []


def test_relay_scoped_to_the_mobs_instance():
    # Arrange — same zone, different private copy (disjoint entity ids).
    p1 = _conn("P1", instance=7)
    other_copy = _conn("Copy", instance=8)
    mob = _monster(instance=7)
    server = _server(p1, other_copy, monster=mob, avatar_ids={"P1": 0x01FE})

    # Act
    relay.on_player_attack(server, p1, mob.entity_id, now=100.0)

    # Assert — never leak an eid into another copy (Code 6 class, bible §7).
    assert other_copy.sent == []


def test_attack_on_unknown_or_foreign_mob_is_ignored():
    # Arrange — target isn't a registered mob of P1's instance.
    p1, p2 = _conn("P1"), _conn("P2")
    foreign = _monster(instance=9)
    server = _server(p1, p2, monster=foreign, avatar_ids={"P1": 0x01FE})

    # Act / Assert — unknown eid: no-op; foreign-instance mob: no-op.
    relay.on_player_attack(server, p1, 0xDEAD, now=100.0)
    relay.on_player_attack(server, p1, foreign.entity_id, now=100.0)
    assert p2.sent == []


def test_purge_mob_clears_state():
    # Arrange — engagement recorded, then the mob dies (telemetry kill).
    p1, p2 = _conn("P1"), _conn("P2")
    mob = _monster()
    server = _server(p1, p2, monster=mob, avatar_ids={"P1": 0x01FE})
    relay.on_player_attack(server, p1, mob.entity_id, now=100.0)

    # Act
    relay.purge_mob(mob.entity_id)
    p2.sent.clear()
    relay.on_player_attack(server, p1, mob.entity_id, now=100.1)

    # Assert — fresh engagement state: the relay fires again immediately
    # (no stale throttle window survives a respawned eid).
    assert len(p2.sent) == 1


def test_kill_switch_env(monkeypatch):
    # Arrange
    p1, p2 = _conn("P1"), _conn("P2")
    mob = _monster()
    server = _server(p1, p2, monster=mob, avatar_ids={"P1": 0x01FE})
    monkeypatch.setenv("DR_MOB_ENGAGEMENT_RELAY", "0")

    # Act / Assert
    relay.on_player_attack(server, p1, mob.entity_id, now=100.0)
    assert p2.sent == []
