"""GameServer.award_kill_xp — server-authoritative XP keeps the avatar level (and
thus conn.hp_wire) in lockstep with the client's local self-leveling.

This is the fix for the level-up Avatar synch crash (live 2026-06-01): on a kill the
client self-levels and recomputes HP, so the server must too, then refresh hp_wire so
the per-tick 0x36 + 0x02 trailers carry the new value. Binds the REAL award_kill_xp /
_refresh_avatar_hp_wire to a stub server and monkeypatches the character repository so
the glue (XP grant → persist → hp_wire refresh) is exercised end-to-end without a DB.
"""
import types

import pytest

from drserver.net import game_server as gs
from drserver.data.player_state import compute_avatar_max_hp_wire


def _make_server(monkeypatch, char):
    """A stub bound to the real award_kill_xp / _refresh_avatar_hp_wire, with the
    character repository replaced by an in-memory fake."""
    saves = []

    fake_repo = types.SimpleNamespace(
        get_character=lambda _id: char,
        save_character=lambda ch: saves.append(ch),
    )
    monkeypatch.setattr(gs, "character_repository", fake_repo)

    server = types.SimpleNamespace()
    server.award_kill_xp = gs.GameServer.award_kill_xp.__get__(server)
    server.accrue_kill_xp = gs.GameServer.accrue_kill_xp.__get__(server)
    server._grant_kill_xp = gs.GameServer._grant_kill_xp.__get__(server)
    server.sync_client_level = gs.GameServer.sync_client_level.__get__(server)
    server._refresh_avatar_hp_wire = gs.GameServer._refresh_avatar_hp_wire.__get__(server)
    return server, saves


def _make_char(level=1, experience=0):
    return types.SimpleNamespace(
        level=level, experience=experience,
        max_hp=1, current_hp=1, max_mana=1, current_mana=1,
    )


def _make_conn(level=1):
    return types.SimpleNamespace(
        char_sql_id=1, login_name="Styx3",
        hp_wire=compute_avatar_max_hp_wire(level) & 0xFFFFFF00,
    )


def test_single_kill_grants_xp_without_leveling(monkeypatch):
    char = _make_char(level=1, experience=0)
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, char)

    server.award_kill_xp(conn, monster_level=2)   # 500 xp, threshold L2=1000

    assert char.level == 1
    assert char.experience == 500
    assert conn.hp_wire == 68096                   # unchanged (no level-up refresh)


def test_two_kills_level_up_and_refresh_hp_wire(monkeypatch):
    char = _make_char(level=1, experience=0)
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, char)

    server.award_kill_xp(conn, monster_level=2)   # exp 500
    server.award_kill_xp(conn, monster_level=2)   # exp 1000 → L2, carry 0

    assert char.level == 2
    assert char.experience == 0
    assert conn.hp_wire == 72192                   # 68096 + 1*4096 (the live crash value)


def test_level_up_clears_cached_hp_for_recompute(monkeypatch):
    char = _make_char(level=1, experience=500)
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, char)

    server.award_kill_xp(conn, monster_level=2)   # 500 → L2

    # Client refills to max on level-up; server clears caches so refresh recomputes.
    assert char.level == 2
    assert char.max_hp is None and char.current_hp is None
    assert char.max_mana is None and char.current_mana is None


def test_far_below_monster_grants_nothing(monkeypatch):
    char = _make_char(level=10, experience=0)
    conn = _make_conn(level=10)
    server, saves = _make_server(monkeypatch, char)

    server.award_kill_xp(conn, monster_level=5)   # 5 <= 10-5 → 0 xp

    assert char.experience == 0
    assert char.level == 10
    assert saves == []                             # nothing persisted


def test_missing_character_is_safe(monkeypatch):
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, None)
    server.award_kill_xp(conn, monster_level=2)    # must not raise
    assert conn.hp_wire == 68096


# ── sync_client_level (telemetry KILL_AT) ───────────────────────────────────

def test_sync_client_level_snaps_up_and_refreshes_hp_wire(monkeypatch):
    char = _make_char(level=1, experience=0)
    conn = _make_conn(level=1)
    server, saves = _make_server(monkeypatch, char)

    server.sync_client_level(conn, client_level=3)

    assert char.level == 3
    assert conn.player_level == 3
    assert conn.hp_wire == compute_avatar_max_hp_wire(3) & 0xFFFFFF00
    assert char.max_hp is None and char.current_hp is None   # cleared for recompute
    assert len(saves) == 1


def test_sync_client_level_never_demotes(monkeypatch):
    char = _make_char(level=5, experience=200)
    conn = _make_conn(level=5)
    server, saves = _make_server(monkeypatch, char)

    server.sync_client_level(conn, client_level=3)   # client behind us — ignore

    assert char.level == 5
    assert saves == []                               # nothing persisted


def test_sync_client_level_equal_is_noop(monkeypatch):
    char = _make_char(level=4)
    conn = _make_conn(level=4)
    server, saves = _make_server(monkeypatch, char)

    server.sync_client_level(conn, client_level=4)

    assert char.level == 4
    assert saves == []


def test_sync_client_level_missing_char_is_safe(monkeypatch):
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, None)
    server.sync_client_level(conn, client_level=5)   # must not raise


# ── accrue_kill_xp (telemetry-snap path: track XP, never lead the level) ────

def test_accrue_kill_xp_accumulates_without_leveling(monkeypatch):
    # 900 + 500 = 1400 > L2 threshold (1000), but accrue must NOT level the
    # server (the client's KILL_AT snap owns the level) — just track experience
    # so the zone-transfer Avatar re-send stops clobbering the client's XP.
    char = _make_char(level=1, experience=900)
    conn = _make_conn(level=1)
    server, saves = _make_server(monkeypatch, char)

    server.accrue_kill_xp(conn, monster_level=2)

    assert char.level == 1                 # never leads the client
    assert char.experience == 1400         # XP still tracked across the threshold
    assert len(saves) == 1


def test_accrue_kill_xp_far_below_monster_grants_nothing(monkeypatch):
    char = _make_char(level=10, experience=50)
    conn = _make_conn(level=10)
    server, saves = _make_server(monkeypatch, char)

    server.accrue_kill_xp(conn, monster_level=5)   # 5 <= 10-5 → 0 xp

    assert char.experience == 50 and char.level == 10
    assert saves == []


# ── sync_client_level: experience carry + exact adopt ───────────────────────

def test_sync_client_level_carries_experience_across_levelup(monkeypatch):
    # Accrued 1400 at L1; client snaps to L2 → deduct the L2 threshold (1000) so
    # experience stays "progress into the current level".
    char = _make_char(level=1, experience=1400)
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, char)

    server.sync_client_level(conn, client_level=2)

    assert char.level == 2
    assert char.experience == 400          # 1400 - 1000 carry


def test_sync_client_level_adopts_exact_experience_same_level(monkeypatch):
    # The exact-XP hook reports the same level but a new experience: adopt it
    # even though the level didn't change (was previously a no-op).
    char = _make_char(level=5, experience=200)
    conn = _make_conn(level=5)
    server, saves = _make_server(monkeypatch, char)

    server.sync_client_level(conn, client_level=5, client_experience=999)

    assert char.level == 5
    assert char.experience == 999
    assert len(saves) == 1


def test_sync_client_level_exact_experience_overrides_carry(monkeypatch):
    # On a level-up, the exact reported experience wins over the approximate
    # server-side carry math.
    char = _make_char(level=1, experience=1400)
    conn = _make_conn(level=1)
    server, _ = _make_server(monkeypatch, char)

    server.sync_client_level(conn, client_level=2, client_experience=750)

    assert char.level == 2
    assert char.experience == 750          # exact, not the 400 carry


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
