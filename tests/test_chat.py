"""Chat wire-format tests — outbound Say broadcast layout.

Regression guard for the live-test "chat fully dead" bug (2026-05-30). Two
root causes: (1) inbound chat arrives on channel 6 but was routed to the admin
stub (and ch3 social was mis-routed to chat); (2) the outbound broadcast wrote
channel byte 3 with a 0x02 padding byte.

Pins the Say packet to C# UnityGameServer.BroadcastChatToZone:
  [0x06][0x00][0x02][0x00][sender\0][message\0]
"""
from types import SimpleNamespace

from drserver.net import chat_commands
from drserver.net.chat_commands import (
    build_say_packet,
    build_chat_line,
    _display_name,
    _cmd_zone,
    _chat_audience,
    _send_chat_zone,
)
from drserver.util.byte_io import LEReader


def _make_conn(login, sink, zone="world.town", inst=0, spawned=True,
               char_name=None, level=1):
    return SimpleNamespace(
        login_name=login,
        is_spawned=spawned,
        current_zone_gc_type=zone,
        instance_id=inst,
        player_level=level,
        send_to_client=lambda b, _s=sink, _l=login: _s.append((_l, b)),
        send_system_message=lambda t, _s=sink, _l=login: _s.append((_l, ("SYS", t))),
    )


def _server(conns, chars=None, groups=None):
    return SimpleNamespace(
        connections={i: c for i, c in enumerate(conns)},
        selected_character=chars or {},
        groups=groups,
    )


def test_say_packet_matches_csharp_layout():
    # Act
    packet = build_say_packet("Hero", "hello world")
    r = LEReader(packet)

    # Assert — exact byte order from C# BroadcastChatToZone.
    assert r.read_byte() == 0x06            # chat channel (NOT 3)
    assert r.read_byte() == 0x00            # message type 0
    assert r.read_byte() == 0x02            # subtype 0x02 = Say (white)
    assert r.read_byte() == 0x00            # padding (NOT 0x02)
    assert r.read_cstring() == "Hero"       # sender name
    assert r.read_cstring() == "hello world"
    assert r.remaining == 0


def test_say_packet_round_trips_unicode_safe_ascii():
    # Arrange / Act
    packet = build_say_packet("Player_1", "GG @everyone")
    r = LEReader(packet)
    r.read_byte(); r.read_byte(); r.read_byte(); r.read_byte()   # header

    # Assert — message body preserved verbatim (including the @).
    assert r.read_cstring() == "Player_1"
    assert r.read_cstring() == "GG @everyone"


def test_display_name_prefers_character_name_over_login():
    # Arrange — selected character carries the in-game hero name; login is the account.
    conn = SimpleNamespace(login_name="acct42")
    server = SimpleNamespace(selected_character={"acct42": SimpleNamespace(name="Sir Galahad")})

    # Act
    name = _display_name(server, conn)

    # Assert — chat shows the character, matching C# BroadcastChatToZone.
    assert name == "Sir Galahad"


def test_display_name_falls_back_to_login_when_no_character():
    # Arrange — no selected character (e.g. pre-spawn or missing name).
    conn = SimpleNamespace(login_name="acct42")
    server = SimpleNamespace(selected_character={})

    # Act / Assert
    assert _display_name(server, conn) == "acct42"


def test_cmd_zone_applies_instantly_via_change_zone():
    # Arrange — @z must teleport now, not tell the player to rejoin.
    calls = []
    zone = SimpleNamespace(id=0x10, name="Town")
    conn = SimpleNamespace(login_name="acct42", current_zone_name="tutorial",
                           current_zone_id=1, send_system_message=lambda *_: None)
    server = SimpleNamespace(
        selected_character={},
        change_zone=lambda c, z: calls.append((c, z)),
        _zone_gc_type=lambda n: f"world.{n.lower()}",
    )

    # Patch the zone registry lookup the handler imports.
    from drserver.managers import zones as zones_mod
    orig = zones_mod.zone_registry.find_by_name
    zones_mod.zone_registry.find_by_name = lambda n: zone
    try:
        # Act
        _cmd_zone(server, conn, ["town"])
    finally:
        zones_mod.zone_registry.find_by_name = orig

    # Assert — the real transfer path ran with the resolved zone name.
    assert calls == [(conn, "Town")]


# ── Multi-channel routing (2026-06-08) ──────────────────────────────────────
# NB: the byte-level layout of build_chat_line (subtype 0x03 = local "Say")
# is a protocol claim still pending a live client confirmation — only the
# deterministic server-side routing/gating is regression-tested here, plus a
# guard that we are NOT riding the "Announce>" subtype (0x0D) for player chat.

def test_chat_line_default_is_local_say_not_announce():
    # Arrange / Act — default builder rides local-say 0x03, never Announce 0x0D.
    r = LEReader(build_chat_line("Maje", "hi"))

    # Assert — channel 0x06, say type 0, subtype 0x03, grouped flag, bare name.
    assert r.read_byte() == 0x06
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x03
    assert r.read_byte() == 0x00
    sender = r.read_cstring()
    assert sender == "Maje" and "[" not in sender   # client brackets it itself
    assert r.read_cstring() == "hi"


def test_chat_line_rides_native_channel_subtype():
    # Arrange / Act — Noob must go out on the native Noob> subtype 0x0C so the
    # client applies the channel prefix + colour (not a baked-in text label).
    r = LEReader(build_chat_line("Maje", "yo", 0x0C))

    # Assert
    assert r.read_byte() == 0x06
    assert r.read_byte() == 0x00
    assert r.read_byte() == 0x0C            # Noob> native subtype
    assert r.read_byte() == 0x00            # grouped flag byte
    assert r.read_cstring() == "Maje"       # bare name, no "[Noob ...]" label
    assert r.read_cstring() == "yo"


def test_world_channel_blocked_below_level_15():
    # Arrange — level-3 player tries World (channel 0x01, min level 15).
    sink = []
    conn = _make_conn("a", sink, level=3)
    server = _server([conn])

    # Act
    _send_chat_zone(server, conn, "anyone there?", channel=0x01)

    # Assert — gated: a single system notice back to the sender, no broadcast.
    assert len(sink) == 1
    login, payload = sink[0]
    assert login == "a" and payload[0] == "SYS"
    assert "level 15" in payload[1] and "World" in payload[1]


def test_world_channel_allowed_at_level_15():
    # Arrange — at the threshold the message goes out.
    sink = []
    a = _make_conn("a", sink, level=15)
    b = _make_conn("b", sink, level=1)
    server = _server([a, b])

    # Act
    _send_chat_zone(server, a, "lfg", channel=0x01)

    # Assert — both spawned players receive a real chat line (no SYS notice).
    assert {login for login, _ in sink} == {"a", "b"}
    assert all(not isinstance(p, tuple) for _, p in sink)


def test_noob_channel_is_not_level_gated():
    # Arrange — Noob (0x06) has no level requirement.
    sink = []
    a = _make_conn("a", sink, level=1)
    server = _server([a])

    # Act
    _send_chat_zone(server, a, "hello", channel=0x06)

    # Assert — delivered, not gated.
    assert len(sink) == 1 and not isinstance(sink[0][1], tuple)


def test_zone_chat_stays_within_instance():
    # Arrange — two players, different instances of the same zone.
    sink = []
    a = _make_conn("a", sink, zone="world.town", inst=0)
    b = _make_conn("b", sink, zone="world.town", inst=1)
    server = _server([a, b])

    # Act — zone channel (0x02).
    _send_chat_zone(server, a, "local only", channel=0x02)

    # Assert — only the same-instance sender hears it.
    assert [login for login, _ in sink] == ["a"]


def test_tell_delivers_to_target_and_echoes_without_html_entity():
    # Arrange — sender "Maje" tells "Styx3".
    sink = []
    a = _make_conn("acct_a", sink)
    b = _make_conn("acct_b", sink)
    chars = {"acct_a": SimpleNamespace(name="Maje"),
             "acct_b": SimpleNamespace(name="Styx3")}
    server = _server([a, b], chars=chars)

    # Act
    _send_chat_zone(server, a, "Styx3 hi there", channel=0x04)

    # Assert — both lines ride the native Tell> subtype 0x05; the recipient sees
    # the sender's bare name, the sender's echo names the target. No brackets or
    # the literal HTML entity "&gt;" of our own.
    by_login = {login: payload for login, payload in sink}
    rt = LEReader(by_login["acct_b"])
    assert rt.read_byte() == 0x06 and rt.read_byte() == 0x00 and rt.read_byte() == 0x05
    rt.read_byte()  # grouped flag
    assert rt.read_cstring() == "Maje"
    re = LEReader(by_login["acct_a"]); [re.read_byte() for _ in range(4)]
    sender_field = re.read_cstring()
    assert "Styx3" in sender_field and "&gt;" not in sender_field and "[" not in sender_field


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
