"""Tests for server-side melee kill detection (Step 6 of ROUTE 2B).

Proves the payoff path: a replayed swing stream drains a tracked monster's HP
via the native damage roll, and the resulting kill is finalized through the
``on_kill`` callback — the same pipeline that raises ``conn.hp_wire`` to the
leveled value (eliminating the Avatar synch crash).
"""

from __future__ import annotations

import pytest

from drserver.combat.damage_computer import NativeWeaponDamageInput
from drserver.combat.native_kill_replay import (
    NativeKillReplay,
    NativeMonsterHost,
    native_weapon_damage_resolver,
)
from drserver.combat.rng import MersenneTwister
from drserver.combat.weapon_cycle import NATIVE_UPDATE_TICK


class FakeMonster:
    def __init__(self, entity_id=0x0547, level=1, hp_wire=29184):
        self.entity_id = entity_id
        self.level = level
        self.current_hp = hp_wire
        self.max_hp = hp_wire
        self.is_alive = True


class FakeConn:
    def __init__(self, conn_id=1, avatar_id=510, hp_wire=68096):
        self.conn_id = conn_id
        self.login_name = "Styx3"
        self.hp_wire = hp_wire

        class _Av:
            id = avatar_id

        self.avatar = _Av()


def _fixed_damage(hit, blocked, damage_wire):
    """A DamageResolver that always returns the same outcome."""
    return lambda cycle, rng: (hit, blocked, damage_wire)


# --------------------------------------------------------------------------- #
# NativeMonsterHost.resolve_hit — HP drain + kill flag
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_host_drains_hp_and_reports_kill():
    monster = FakeMonster(hp_wire=1000)
    host = NativeMonsterHost(_fixed_damage(True, False, 1500))

    class _C:
        pass

    cycle = _C()
    cycle.monster = monster
    killed, applied = host.resolve_hit(cycle, None)

    assert monster.current_hp == 0
    assert killed is True
    assert applied == (1000 + 255) // 256  # clamped to remaining HP


@pytest.mark.unit
def test_host_non_killing_hit_subtracts_only():
    monster = FakeMonster(hp_wire=5000)
    host = NativeMonsterHost(_fixed_damage(True, False, 1280))

    class _C:
        pass

    cycle = _C()
    cycle.monster = monster
    killed, applied = host.resolve_hit(cycle, None)

    assert monster.current_hp == 3720
    assert killed is False
    assert applied == 5  # 1280 wire / 256


@pytest.mark.unit
def test_host_miss_and_block_do_no_damage():
    monster = FakeMonster(hp_wire=5000)

    class _C:
        pass

    cycle = _C()
    cycle.monster = monster

    miss_host = NativeMonsterHost(_fixed_damage(False, False, 9999))
    assert miss_host.resolve_hit(cycle, None) == (False, 0)
    assert monster.current_hp == 5000

    block_host = NativeMonsterHost(_fixed_damage(True, True, 9999))
    assert block_host.resolve_hit(cycle, None) == (False, 0)
    assert monster.current_hp == 5000


@pytest.mark.unit
def test_host_monster_alive_tracks_hp():
    host = NativeMonsterHost(_fixed_damage(True, False, 1))

    class _C:
        pass

    cycle = _C()
    cycle.monster = FakeMonster(hp_wire=256)
    assert host.monster_alive(cycle) is True
    cycle.monster.current_hp = 0
    assert host.monster_alive(cycle) is False
    cycle.monster = None
    assert host.monster_alive(cycle) is False


# --------------------------------------------------------------------------- #
# NativeKillReplay — register swing, tick, finalize
# --------------------------------------------------------------------------- #


def _drive_ticks(replay: NativeKillReplay, conn_key: str, ticks: int, now0: float = 1.0):
    for k in range(1, ticks + 1):
        replay.tick(now0 + k * NATIVE_UPDATE_TICK)


@pytest.mark.unit
def test_single_swing_kill_finalizes_once():
    kills: list[tuple] = []
    monster = FakeMonster(hp_wire=1000)  # dies in one ~4000-wire hit
    host = NativeMonsterHost(_fixed_damage(True, False, 4000))
    replay = NativeKillReplay(host, on_kill=lambda c, m: kills.append((c, m)))

    conn = FakeConn()
    replay.register_swing("conn1", monster.entity_id, monster, now=1.0,
                          conn=conn, player_entity_id=conn.avatar.id)
    # Hit lands at native tick 14; drive past it.
    _drive_ticks(replay, "conn1", ticks=14)

    assert len(kills) == 1
    assert kills[0] == (conn, monster)
    assert monster.current_hp == 0


@pytest.mark.unit
def test_miss_never_finalizes_kill():
    kills: list[tuple] = []
    monster = FakeMonster(hp_wire=1000)
    host = NativeMonsterHost(_fixed_damage(False, False, 9999))
    replay = NativeKillReplay(host, on_kill=lambda c, m: kills.append((c, m)))

    replay.register_swing("conn1", monster.entity_id, monster, now=1.0)
    _drive_ticks(replay, "conn1", ticks=30)

    assert kills == []
    assert monster.current_hp == 1000


@pytest.mark.unit
def test_kill_finalize_raises_hp_wire_to_leveled_value():
    # The finalize callback is the existing death pipeline; here a fake server
    # mirrors award_kill_xp -> _refresh_avatar_hp_wire raising hp_wire to L2.
    awarded: list[int] = []

    def fake_finalize(conn, monster):
        awarded.append(monster.level)
        # award_kill_xp would level the player and refresh the wire to L2 (72192).
        conn.hp_wire = 72192

    monster = FakeMonster(level=1, hp_wire=500)
    host = NativeMonsterHost(_fixed_damage(True, False, 4000))
    replay = NativeKillReplay(host, on_kill=fake_finalize)

    conn = FakeConn(hp_wire=68096)  # L1 wire
    replay.register_swing("conn1", monster.entity_id, monster, now=1.0, conn=conn)
    _drive_ticks(replay, "conn1", ticks=14)

    assert awarded == [1]
    assert conn.hp_wire == 72192  # raised in lockstep with the client self-level


@pytest.mark.unit
def test_integration_real_damage_computer_kill():
    # Wire the real native damage path: a guaranteed-hit input (huge AR, no DR,
    # no block) with a fixed weapon damage drains a small-HP monster to a kill.
    kills: list[tuple] = []

    def build_input(cycle):
        return NativeWeaponDamageInput(
            attacker_level=1, defender_level=1,
            attack_rating=10000, defense_rating=0, block_chance=0,
            damage_level=0, damage_bonus=0, damage_mod=0,
            weapon_damage_f32=4096, weapon_volatility_f32=256,  # ~16hp base
            crit_threshold=0, crit_damage_percent=0,
            source="test",
        )

    host = NativeMonsterHost(native_weapon_damage_resolver(build_input))
    replay = NativeKillReplay(
        host, on_kill=lambda c, m: kills.append((c, m)),
        rng=MersenneTwister(0x8D801C2B),
    )

    # begin_cycle consumes one use-RNG draw before the hit, so the real roll for
    # this seed/input deals 395 wire; HP below that dies in one hit.
    monster = FakeMonster(hp_wire=300)
    conn = FakeConn()
    replay.register_swing("conn1", monster.entity_id, monster, now=1.0, conn=conn)
    _drive_ticks(replay, "conn1", ticks=14)

    assert len(kills) == 1
    assert monster.current_hp == 0


@pytest.mark.unit
def test_clear_connection_and_clear():
    host = NativeMonsterHost(_fixed_damage(True, False, 1))
    replay = NativeKillReplay(host, on_kill=lambda c, m: None)
    replay.register_swing("conn1", 0x0547, FakeMonster(), now=1.0)
    replay.register_swing("conn2", 0x054C, FakeMonster(), now=1.0)

    replay.clear_connection("conn1")
    assert "conn1" not in replay.tracker._active_cycles
    assert "conn2" in replay.tracker._active_cycles

    replay.clear()
    assert not replay.tracker._active_cycles
