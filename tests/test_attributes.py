"""Attribute allocation — spend / return / respec primary-stat points.

Ports of the C# entity-request handlers (HandleStatSpendRequest /
HandleStatReturnRequest / HandleRespecRequest). Binds the real handlers to stub
conn/server objects with an in-memory character repository so the glue (validate
→ persist → confirm packet) is exercised without a DB or live client. The respec
cost curve is checked against the client's binary-verified gold costs.
"""
import types

import pytest

from drserver.net import attributes
from drserver.util.byte_io import LEReader, LEWriter


# ── fixtures ────────────────────────────────────────────────────────────────────

def _make_char(level=5, gold=100000, **stats):
    return types.SimpleNamespace(
        level=level, gold=gold,
        stat_strength=stats.get("str", 0), stat_agility=stats.get("agi", 0),
        stat_endurance=stats.get("end", 0), stat_intellect=stats.get("int", 0),
        last_respec_time=stats.get("last_respec_time", 0),
        respec_count=stats.get("respec_count", 0),
    )


def _bind(monkeypatch, char, avatar_id=0x0210):
    """Stub conn + server bound to the in-memory char; capture sends."""
    saves, packets, messages = [], [], []
    fake_repo = types.SimpleNamespace(
        get_character=lambda _id: char,
        save_character=lambda ch: saves.append(ch),
    )
    monkeypatch.setattr(attributes, "character_repository", fake_repo)
    # Keep the unit test off the equipment/DB HP-refresh path.
    monkeypatch.setattr("drserver.data.player_state.refresh_player_state",
                        lambda conn: None)

    conn = types.SimpleNamespace(
        char_sql_id=1, login_name="Styx3", hp_wire=68096, client_hp_wire=None,
        send_to_client=lambda b: packets.append(bytes(b)),
        send_system_message=lambda m: messages.append(m),
    )
    server = types.SimpleNamespace(
        get_player_avatar_id=lambda _login: avatar_id,
    )
    return conn, server, saves, packets, messages


def _decode_hero_update(packet):
    """Decode a SendHeroStatUpdate packet into a dict of its fields."""
    r = LEReader(packet)
    fields = {
        "begin": r.read_byte(), "op": r.read_byte(), "avatar": r.read_uint16(),
        "sub_type": r.read_byte(), "stat_type": r.read_byte(),
        "num_points": r.read_byte(), "synch_flag": r.read_byte(),
        "synch_hp": r.read_uint32(), "end": r.read_byte(),
    }
    return fields


# ── points / curve math ─────────────────────────────────────────────────────────

def test_points_available_is_five_per_level_minus_allocated():
    char = _make_char(level=5, str=3, agi=2)        # earned 20, allocated 5
    assert attributes.points_available(char) == 15


def test_points_available_never_negative_at_level_one():
    assert attributes.points_available(_make_char(level=1)) == 0


@pytest.mark.parametrize("level,expected", [
    (1, 203), (10, 203), (14, 13285), (20, 32906), (30, 65617), (40, 98324),
])
def test_respec_cost_matches_client_binary(level, expected):
    assert attributes.respec_cost_gold(level) == expected


# ── spend (0x11) ────────────────────────────────────────────────────────────────

def test_spend_within_pool_persists_and_confirms(monkeypatch):
    char = _make_char(level=5)                      # pool = 20
    conn, server, saves, packets, _ = _bind(monkeypatch, char)

    attributes.handle_stat_spend(server, conn, LEReader(bytes([0, 3])))  # 3 into STR

    assert char.stat_strength == 3
    assert saves == [char]
    f = _decode_hero_update(packets[0])
    assert (f["begin"], f["op"], f["avatar"]) == (0x07, 0x03, 0x0210)
    assert (f["sub_type"], f["stat_type"], f["num_points"]) == (0x11, 0, 3)
    assert (f["synch_flag"], f["synch_hp"], f["end"]) == (0x02, 68096, 0x06)


def test_spend_over_pool_is_rejected(monkeypatch):
    char = _make_char(level=1)                       # pool = 0
    conn, server, saves, packets, _ = _bind(monkeypatch, char)

    attributes.handle_stat_spend(server, conn, LEReader(bytes([0, 1])))

    assert char.stat_strength == 0
    assert saves == [] and packets == []


def test_spend_into_endurance_routes_to_correct_stat(monkeypatch):
    char = _make_char(level=10)                      # pool = 45
    conn, server, _, packets, _ = _bind(monkeypatch, char)

    attributes.handle_stat_spend(server, conn, LEReader(bytes([2, 5])))  # END

    assert char.stat_endurance == 5
    assert _decode_hero_update(packets[0])["stat_type"] == 2


def test_spend_zero_points_is_ignored(monkeypatch):
    char = _make_char(level=5)
    conn, server, saves, packets, _ = _bind(monkeypatch, char)

    attributes.handle_stat_spend(server, conn, LEReader(bytes([0, 0])))

    assert saves == [] and packets == []


# ── return (0x12) ────────────────────────────────────────────────────────────────

def test_return_refunds_allocated_and_confirms(monkeypatch):
    char = _make_char(level=10, end=4)
    conn, server, saves, packets, _ = _bind(monkeypatch, char)

    attributes.handle_stat_return(server, conn, LEReader(bytes([2, 2])))  # refund 2 END

    assert char.stat_endurance == 2
    f = _decode_hero_update(packets[0])
    assert (f["sub_type"], f["stat_type"], f["num_points"]) == (0x12, 2, 2)


def test_return_more_than_allocated_is_rejected(monkeypatch):
    char = _make_char(level=10, end=1)
    conn, server, saves, packets, _ = _bind(monkeypatch, char)

    attributes.handle_stat_return(server, conn, LEReader(bytes([2, 2])))

    assert char.stat_endurance == 1
    assert saves == [] and packets == []


# ── respec (0x13) ────────────────────────────────────────────────────────────────

def test_respec_resets_stats_and_charges_curve_gold(monkeypatch):
    char = _make_char(level=20, gold=50000, str=5, agi=4, end=3, int=2)
    conn, server, saves, packets, messages = _bind(monkeypatch, char)

    attributes.handle_respec(server, conn)

    assert (char.stat_strength, char.stat_agility,
            char.stat_endurance, char.stat_intellect) == (0, 0, 0, 0)
    assert char.gold == 50000 - 32906               # L20 curve cost
    assert char.respec_count == 1
    assert char.last_respec_time > 0
    # reset packet: 0x07 0x03 <avatar> 0x13 0x02 <u32 0xFFFF00> 0x06
    r = LEReader(packets[0])
    assert [r.read_byte(), r.read_byte()] == [0x07, 0x03]
    assert r.read_uint16() == 0x0210
    assert [r.read_byte(), r.read_byte()] == [0x13, 0x02]
    assert r.read_uint32() == 0xFFFF00
    assert messages                                  # gold receipt sent


def test_respec_insufficient_gold_is_rejected(monkeypatch):
    char = _make_char(level=20, gold=100, str=5)
    conn, server, saves, packets, messages = _bind(monkeypatch, char)

    attributes.handle_respec(server, conn)

    assert char.stat_strength == 5 and char.gold == 100
    assert saves == [] and packets == []
    assert messages                                  # "not enough gold" warning


def test_respec_on_cooldown_is_rejected(monkeypatch):
    import time
    char = _make_char(level=20, gold=50000, str=5,
                      last_respec_time=int(time.time()))   # just respecced
    conn, server, saves, packets, messages = _bind(monkeypatch, char)

    attributes.handle_respec(server, conn)

    assert char.stat_strength == 5                   # untouched
    assert saves == [] and packets == []
    assert messages                                  # cooldown warning
