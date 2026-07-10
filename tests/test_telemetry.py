"""Combat telemetry channel dispatch tests (net/telemetry.py).

Socket-free: exercises the parsing + kill-credit logic with lightweight fakes.
"""
import struct

from drserver.net.telemetry import (
    TelemetryServer, OP_KILL, OP_KILL_AT, OP_DAMAGE, OP_MOB_ATTACK, OP_HELLO,
    OP_MOB_CLAMP, _Hook,
)


class FakeMonster:
    def __init__(self, zone):
        self.zone_gc_type = zone


class FakeCombat:
    def __init__(self, monster_eids, zone="dungeon00_level03_boss"):
        self._monsters = {eid: FakeMonster(zone) for eid in monster_eids}
        self.killed = []

    def is_monster(self, eid):
        return eid in self._monsters

    def get_monster(self, eid):
        return self._monsters.get(eid)

    def notify_client_kill(self, eid, conn, death_pos=None, level_synced=False):
        if eid not in self._monsters:
            return False
        self.killed.append((eid, conn, death_pos))
        self.last_level_synced = level_synced
        return True


class FakeConn:
    def __init__(self, conn_id, login_name="hero", is_spawned=True,
                 zone="dungeon00_level03_boss"):
        self.conn_id = conn_id
        self.login_name = login_name
        self.is_spawned = is_spawned
        self.current_zone_gc_type = zone


class FakeOwnedUnitMgr:
    """Stand-in for SummonManager / BlingGnomeManager owner resolution."""
    def __init__(self, eid_to_conn):
        self._map = eid_to_conn

    def owner_conn_for_entity(self, eid):
        return self._map.get(eid)


class FakeGame:
    def __init__(self):
        self.combat = FakeCombat({5000})
        self._conn = FakeConn(1)
        self.connections = {1: self._conn}
        # spawn.py stores str(conn_id) -> avatar entity id
        self.player_avatar_entity_id = {"1": 510}
        self.level_snaps = []          # (conn, client_level, client_experience)
        # owned-unit managers (summon eid 591 -> conn 1)
        self.summons = FakeOwnedUnitMgr({591: self._conn})
        self.gnome = FakeOwnedUnitMgr({})

    def sync_client_level(self, conn, client_level, client_experience=None):
        self.level_snaps.append((conn, client_level, client_experience))


def _server():
    return TelemetryServer(FakeGame(), "127.0.0.1", 2700)


def test_conn_for_avatar_eid_resolves_known_avatar():
    srv = _server()

    conn = srv.conn_for_avatar_eid(510)

    assert conn is not None
    assert conn.conn_id == 1


def test_conn_for_avatar_eid_unknown_returns_none():
    srv = _server()

    assert srv.conn_for_avatar_eid(99999) is None


def test_on_kill_credits_tracked_mob_to_resolved_killer():
    srv = _server()

    ok = srv.on_kill(victim_eid=5000, killer_eid=510)

    assert ok is True
    assert srv._game.combat.killed == [(5000, srv._game.connections[1], None)]


def test_on_kill_ignores_non_mob_victim():
    srv = _server()

    ok = srv.on_kill(victim_eid=510, killer_eid=510)  # avatar died, not a mob

    assert ok is False
    assert srv._game.combat.killed == []


def test_on_kill_credits_owned_unit_killer_to_owner():
    """A boss finished off by the player's summon reports the summon's eid (591),
    not the avatar's — the kill must still be credited to the owner (live 'no loot
    drops from bosses'). The level snap is SKIPPED (the summon has no PlayerState
    level), so the kill is flagged level_synced to suppress the server XP grant."""
    srv = _server()

    ok = srv.on_kill(victim_eid=5000, killer_eid=591, killer_level=99)

    assert ok is True
    assert srv._game.combat.killed == [(5000, srv._game.connections[1], None)]
    assert srv._game.level_snaps == []                  # garbage level not snapped
    assert srv._game.combat.last_level_synced is True   # server XP suppressed


def test_on_kill_falls_back_to_sole_player_in_zone():
    """A kill whose attacker is neither the avatar nor a tracked owned unit
    (AoE/DoT/projectile source) is credited to the only spawned player in the
    mob's zone — solo dungeons are unambiguous."""
    srv = _server()

    ok = srv.on_kill(victim_eid=5000, killer_eid=88888)  # unknown attacker entity

    assert ok is True
    assert srv._game.combat.killed == [(5000, srv._game.connections[1], None)]
    assert srv._game.level_snaps == []


def test_on_kill_abstains_when_multiple_players_could_claim():
    """In a shared zone with >1 spawned player, an unresolved killer must NOT be
    mis-credited — fall back to the existing 'unresolved' behaviour."""
    srv = _server()
    other = FakeConn(2, login_name="ally")
    srv._game.connections[2] = other

    ok = srv.on_kill(victim_eid=5000, killer_eid=88888)

    assert ok is False
    assert srv._game.combat.killed == []


def test_dispatch_parses_kill_record_and_credits():
    srv = _server()

    srv._dispatch(OP_KILL, struct.pack("<II", 5000, 510))

    assert srv._game.combat.killed == [(5000, srv._game.connections[1], None)]


def test_dispatch_damage_is_noop_for_now():
    srv = _server()

    srv._dispatch(OP_DAMAGE, struct.pack("<III", 5000, 510, 12345))

    assert srv._game.combat.killed == []


# ── KILL_AT: death position + level snap ────────────────────────────────────

def test_kill_at_passes_death_position_to_loot():
    srv = _server()

    # Fixed32 ×256 on the wire: (600.5, -88.25, 10.0).
    srv._dispatch(OP_KILL_AT, struct.pack(
        "<IIiiiH", 5000, 510, int(600.5 * 256), int(-88.25 * 256), int(10.0 * 256), 7))

    assert len(srv._game.combat.killed) == 1
    eid, conn, death_pos = srv._game.combat.killed[0]
    assert eid == 5000 and conn is srv._game.connections[1]
    assert death_pos == (600.5, -88.25, 10.0)
    # level was reported (7) → the kill is flagged level_synced so the server's
    # own XP grant is suppressed (snap is the sole level authority).
    assert srv._game.combat.last_level_synced is True


def test_kill_at_snaps_killer_level():
    srv = _server()

    srv._dispatch(OP_KILL_AT, struct.pack(
        "<IIiiiH", 5000, 510, 0, 0, 0, 7))

    assert srv._game.level_snaps == [(srv._game.connections[1], 7, None)]


def test_kill_at_snaps_level_even_for_untracked_mob():
    """The client self-levels on EVERY kill, so the level must snap even when the
    victim isn't a server-tracked mob (otherwise the level drifts on missed
    kills — the exact 'client leveled, server behind' bug)."""
    srv = _server()

    srv._dispatch(OP_KILL_AT, struct.pack(
        "<IIiiiH", 4444, 510, 0, 0, 0, 8))  # 4444 is not a tracked mob

    assert srv._game.combat.killed == []          # no loot credit
    assert srv._game.level_snaps == [(srv._game.connections[1], 8, None)]


def test_kill_at_no_level_snap_for_unresolved_killer():
    """A KILL_AT whose killer is neither an avatar nor an owned unit must NOT snap
    the reported level: in a shared zone the fallback abstains (no credit), and
    the level field — read off a non-avatar entity — is never trusted."""
    srv = _server()
    srv._game.connections[2] = FakeConn(2, login_name="ally")  # ambiguous -> abstain

    srv._dispatch(OP_KILL_AT, struct.pack(
        "<IIiiiH", 5000, 99999, 0, 0, 0, 7))  # killer eid unknown

    assert srv._game.level_snaps == []
    assert srv._game.combat.killed == []


def test_kill_at_xp_adopts_exact_experience():
    """OP_KILL_AT_XP carries the killer's exact progress-into-level Experience
    (u32) appended to the KILL_AT record; the server adopts it via
    sync_client_level so the zone-transfer re-send matches the client exactly."""
    from drserver.net.telemetry import OP_KILL_AT_XP

    srv = _server()

    srv._dispatch(OP_KILL_AT_XP, struct.pack(
        "<IIiiiHI", 5000, 510, 0, 0, 0, 7, 21477))

    assert srv._game.level_snaps == [(srv._game.connections[1], 7, 21477)]
    assert len(srv._game.combat.killed) == 1


def test_kill_at_owned_unit_never_snaps_bogus_level():
    """KILL_AT credited via an owned unit must not snap the level field (garbage
    read off the summon, not a PlayerState) — snapping it would lead the client
    and crash the synch compare. Loot is still credited to the owner."""
    srv = _server()

    srv._dispatch(OP_KILL_AT, struct.pack(
        "<IIiiiH", 5000, 591, 0, 0, 0, 250))  # 591 = owner's summon; level=garbage

    assert srv._game.level_snaps == []
    assert len(srv._game.combat.killed) == 1
    assert srv._game.combat.last_level_synced is True


# ── Server→client MOB_ATTACK push (docs/MOB_ATTACK_INJECTION.md) ──────────────

class FakeWriter:
    def __init__(self):
        self.buf = b""

    def write(self, data):
        self.buf += data

    def close(self):
        pass


def test_send_mob_attack_writes_record_to_hook():
    srv = _server()
    w = FakeWriter()
    srv._hooks.append(_Hook(w))                       # avatar_eid 0 = broadcast
    conn = srv._game.connections[1]

    ok = srv.send_mob_attack(conn, mob_eid=5000, damage_wire=2560, element=0)

    assert ok is True
    # [op][mob_eid][avatar_eid (conn 1 -> 510)][damage_wire][element]
    assert w.buf == struct.pack("<BIIIB", OP_MOB_ATTACK, 5000, 510, 2560, 0)


def test_send_mob_attack_routes_by_avatar_eid():
    srv = _server()
    mine, other = FakeWriter(), FakeWriter()
    h_mine = _Hook(mine); h_mine.avatar_eid = 510     # conn 1's avatar (FakeGame map)
    h_other = _Hook(other); h_other.avatar_eid = 777
    srv._hooks += [h_mine, h_other]
    conn = srv._game.connections[1]

    srv.send_mob_attack(conn, mob_eid=5000, damage_wire=2560)

    assert mine.buf != b""                            # routed to the matching hook
    assert other.buf == b""                           # other player's hook untouched


def test_send_mob_attack_broadcasts_to_unidentified_hook():
    srv = _server()
    w = FakeWriter()
    srv._hooks.append(_Hook(w))                       # avatar_eid 0 (no HELLO yet)
    conn = srv._game.connections[1]

    srv.send_mob_attack(conn, mob_eid=5000, damage_wire=2560)

    assert w.buf != b""                               # unidentified hook still gets it


def test_send_mob_attack_no_hooks_returns_false():
    srv = _server()
    assert srv.send_mob_attack(srv._game.connections[1], 5000, 2560) is False


def test_send_mob_clamp_writes_record_to_hook():
    srv = _server()
    w = FakeWriter()
    srv._hooks.append(_Hook(w))                       # avatar_eid 0 = broadcast
    conn = srv._game.connections[1]

    ok = srv.send_mob_clamp(conn, mob_eid=5000, ring_wire=4096)

    assert ok is True
    # [op][mob_eid][avatar_eid (conn 1 -> 510)][ring_wire] = 13 bytes
    assert w.buf == struct.pack("<BIII", OP_MOB_CLAMP, 5000, 510, 4096)
    assert len(w.buf) == 13


def test_send_mob_clamp_routes_by_avatar_eid():
    srv = _server()
    mine, other = FakeWriter(), FakeWriter()
    h_mine = _Hook(mine); h_mine.avatar_eid = 510     # conn 1's avatar
    h_other = _Hook(other); h_other.avatar_eid = 777
    srv._hooks += [h_mine, h_other]

    srv.send_mob_clamp(srv._game.connections[1], mob_eid=5000, ring_wire=4096)

    assert mine.buf != b"" and other.buf == b""       # only the matching hook


def test_send_mob_clamp_no_hooks_returns_false():
    srv = _server()
    assert srv.send_mob_clamp(srv._game.connections[1], 5000, 4096) is False


def test_dispatch_hello_sets_hook_avatar_eid():
    srv = _server()
    hook = _Hook(FakeWriter())

    srv._dispatch(OP_HELLO, struct.pack("<I", 510), hook)

    assert hook.avatar_eid == 510


# ── Server→client ZONE_RESET push (clears the hook's stale-zone cache) ─────────

def test_send_zone_reset_writes_op_byte():
    from drserver.net.telemetry import OP_ZONE_RESET
    srv = _server()
    w = FakeWriter()
    srv._hooks.append(_Hook(w))

    srv.send_zone_reset(srv._game.connections[1])

    assert w.buf == bytes((OP_ZONE_RESET,))


def test_send_zone_reset_no_hooks_is_safe():
    srv = _server()
    srv.send_zone_reset(srv._game.connections[1])   # must not raise
