"""Viewer relay for player actions — CreateAction fan-out (net/action_relay.py).

Live user report 2026-07-09: P1 shift-attacking in town, casting skills or
fighting mobs in a dungeon was INVISIBLE to P2 — the server acked the actor
and told no one else. The native model (bible §15.4, original-dev testimony)
replicates every client's input to the other clients; the downstream shape is
CreateAction on the viewer's remapped behavior id:

  ``0x07 0x35 <viewerBehaviorId:u16> 0x04 <actionClassId> <mode=0x00> <body> 0x00 0x06``

Delivery is FRAMED-DIRECT (own 0x07…0x06 stream, in order with the movement
relay) — the interval-queue variant delivered the action LATE and re-rooted the
display avatar, freezing its movement on the viewer (2026-07-09 regression).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.net import action_relay
from drserver.util.byte_io import LEReader


def _conn(login, zone="world.town", instance=0, spawned=True):
    sent = []
    return SimpleNamespace(
        login_name=login,
        current_zone_gc_type=zone,
        instance_id=instance,
        is_spawned=spawned,
        viewer_action_pending=False,
        send_to_client=lambda b, _s=sent: _s.append(bytes(b)),
        sent=sent,
    )


def _server(*conns, behavior_ids=None):
    return SimpleNamespace(
        connections={c.login_name: c for c in conns},
        remote_behavior_ids=behavior_ids or {},
    )


def test_relay_use_target_reaches_same_instance_viewer():
    # Arrange — P2 views P1 through remapped behavior id 0x0500.
    p1, p2 = _conn("P1"), _conn("P2")
    server = _server(p1, p2, behavior_ids={"P2": {"P1": 0x0500}})

    # Act — P1's basic attack (0x50) on target 0x0123, useFlags 0x01.
    sent = action_relay.relay_player_action(
        server, p1, 0x50, bytes([0x00, 0x01]) + (0x0123).to_bytes(2, "little"))

    # Assert — one FRAMED CreateAction to P2, empty synch, and the actor is
    # marked action-pending so the next move un-roots the display copy.
    assert sent == 1
    assert p1.viewer_action_pending is True
    r = LEReader(p2.sent[0])
    assert r.read_byte() == 0x07                  # BeginStream
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0500              # viewer-remapped behavior id
    assert r.read_byte() == 0x04                  # CreateAction
    assert r.read_byte() == 0x50                  # UseTarget class
    assert (r.read_byte(), r.read_byte()) == (0x00, 0x01)   # mode 0, useFlags
    assert r.read_uint16() == 0x0123              # target eid (instance-shared)
    assert r.read_byte() == 0x00                  # empty synch — no owner HP
    assert r.read_byte() == 0x06                  # EndStream
    assert r.remaining == 0
    assert p1.sent == []                          # actor gets the ack, not this


def test_relay_skips_other_zone_other_instance_and_unspawned():
    # Arrange — three ineligible viewers.
    p1 = _conn("P1", zone="world.dungeon01.level01", instance=7)
    far = _conn("Far", zone="world.town", instance=7)
    other_copy = _conn("Copy", zone="world.dungeon01.level01", instance=8)
    ghost = _conn("Ghost", zone="world.dungeon01.level01", instance=7,
                  spawned=False)
    server = _server(p1, far, other_copy, ghost, behavior_ids={
        "Far": {"P1": 1}, "Copy": {"P1": 2}, "Ghost": {"P1": 3}})

    # Act
    sent = action_relay.relay_player_action(server, p1, 0x52, b"\x00\x66")

    # Assert — nobody eligible, nothing sent, no pending flag.
    assert sent == 0
    assert p1.viewer_action_pending is False
    for viewer in (far, other_copy, ghost):
        assert viewer.sent == []


def test_relay_requires_viewer_behavior_mapping():
    # Arrange — same instance but the spawn exchange hasn't mapped P1 yet.
    p1, p2 = _conn("P1"), _conn("P2")
    server = _server(p1, p2, behavior_ids={})

    # Act / Assert — no mapping, no send (never guess a component id).
    assert action_relay.relay_player_action(server, p1, 0x50, b"\x00\x00\x00\x00") == 0
    assert p2.sent == []


def test_relay_kill_switch_env(monkeypatch):
    # Arrange
    p1, p2 = _conn("P1"), _conn("P2")
    server = _server(p1, p2, behavior_ids={"P2": {"P1": 0x0500}})
    monkeypatch.setenv("DR_ACTION_RELAY", "0")

    # Act / Assert — the env kill-switch silences the relay entirely.
    assert action_relay.relay_player_action(server, p1, 0x52, b"\x00\x66") == 0
    assert p2.sent == []


def test_relay_tolerates_minimal_server_objects():
    # Arrange — handlers run against stub servers in tests / early boot.
    p1 = _conn("P1")
    bare = SimpleNamespace()                      # no connections attribute

    # Act / Assert — a bare server is a no-op, never a crash.
    assert action_relay.relay_player_action(bare, p1, 0x50, b"\x00\x00\x00\x00") == 0


def test_cancel_relay_stops_viewer_copy_and_clears_pending():
    # Arrange — P1 has an action pending on viewers.
    p1, p2 = _conn("P1"), _conn("P2")
    p1.viewer_action_pending = True
    server = _server(p1, p2, behavior_ids={"P2": {"P1": 0x0600}})

    # Act — un-root P1's display copy on P2.
    sent = action_relay.relay_cancel_action(server, p1)

    # Assert — framed CancelAction (0x03), pending cleared.
    assert sent == 1
    assert p1.viewer_action_pending is False
    assert p2.sent == [bytes([0x07, 0x35, 0x00, 0x06, 0x03, 0x00, 0x00, 0x06])]


def test_cancel_relay_clears_pending_even_when_disabled(monkeypatch):
    # Arrange
    p1, p2 = _conn("P1"), _conn("P2")
    p1.viewer_action_pending = True
    server = _server(p1, p2, behavior_ids={"P2": {"P1": 0x0600}})
    monkeypatch.setenv("DR_ACTION_RELAY", "0")

    # Act / Assert — the flag is always cleared so it can't leak a stuck state.
    assert action_relay.relay_cancel_action(server, p1) == 0
    assert p1.viewer_action_pending is False
    assert p2.sent == []
