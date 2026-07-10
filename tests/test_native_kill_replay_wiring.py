"""ROUTE 2B wiring — CombatManager.register_swing / tick_combat / clear_combat.

The replay ENGINE (rng/damage/weapon_cycle/native_kill_replay) is already tested
in isolation. These tests cover the INTEGRATION seams that make it live:

  * ``register_swing`` looks the target up as a tracked monster and feeds the
    replay (no-op for non-monsters),
  * ``tick_combat`` advances the per-connection cycle,
  * ``clear_combat`` drops the cycle,
  * a replayed kill finalizes through the existing death pipeline
    (``_process_monster_kill`` -> ``award_kill_xp``).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers.combat import CombatManager


class _Conn:
    def __init__(self, login_name="Styx3", ub_id=533, player_level=1):
        self.login_name = login_name
        self.unit_behavior_id = ub_id
        self.player_level = player_level
        self.conn_id = 1
        self.is_spawned = True
        self.current_zone_gc_type = "world.tutorial"
        self.instance_id = 0

    def send_to_client(self, data):
        return len(data)


class _Server:
    def __init__(self, telemetry_authoritative_kills=False):
        self.connections = {}
        self.quests = None
        self.combat = None
        self.xp_awards = []
        # Default False here so the replay-path tests exercise the replay-finalize
        # fallback; production defaults to True (telemetry is the kill authority).
        self.config = SimpleNamespace(
            telemetry_authoritative_kills=telemetry_authoritative_kills)
        # Act as the telemetry channel too: a "connected hook" is simulated by the
        # same flag, so _telemetry_authoritative (config AND has_active_hook) tracks it.
        self.telemetry = self

    def has_active_hook(self):
        return self.config.telemetry_authoritative_kills

    def award_kill_xp(self, conn, monster_level):
        self.xp_awards.append((conn, monster_level))


def _manager_with_monster(eid=0x12, hp_wire=10000, level=2):
    server = _Server()
    cm = CombatManager(server)
    server.combat = cm
    cm.register_monster(eid, "test.type", "Dew Valley Pup", hp_wire, level,
                        "GRUNT", "world.tutorial", 0, 0, 0)
    return server, cm


class _RecordingReplay:
    """Stands in for NativeKillReplay to assert the manager's delegation."""

    def __init__(self):
        self.registered = []
        self.ticked = []
        self.cleared = []

    def register_swing(self, conn_key, target_id, monster, **kw):
        self.registered.append((conn_key, target_id, monster, kw))

    def tick_player_entity(self, player_entity_id, now):
        self.ticked.append((player_entity_id, now))

    def clear_connection(self, conn_key):
        self.cleared.append(conn_key)


# ── delegation seams ─────────────────────────────────────────────────────────
def test_register_swing_feeds_replay_for_tracked_monster():
    server, cm = _manager_with_monster(eid=0x12)
    cm._kill_replay = _RecordingReplay()
    conn = _Conn()

    cm.register_swing(conn, 0x12, now=5.0)

    assert len(cm._kill_replay.registered) == 1
    conn_key, target_id, monster, kw = cm._kill_replay.registered[0]
    assert conn_key == "Styx3"
    assert target_id == 0x12
    assert monster is cm.get_monster(0x12)
    assert kw["conn"] is conn
    assert kw["player_entity_id"] == 533


def test_register_swing_ignores_non_monster_target():
    server, cm = _manager_with_monster(eid=0x12)
    cm._kill_replay = _RecordingReplay()

    cm.register_swing(_Conn(), 0x9999, now=1.0)  # not a tracked monster

    assert cm._kill_replay.registered == []


def test_tick_combat_advances_this_connection():
    server, cm = _manager_with_monster()
    cm._kill_replay = _RecordingReplay()
    conn = _Conn(ub_id=533)

    cm.tick_combat(conn, now=12.5)

    assert cm._kill_replay.ticked == [(533, 12.5)]


def test_clear_combat_drops_the_cycle():
    server, cm = _manager_with_monster()
    cm._kill_replay = _RecordingReplay()

    cm.clear_combat("Styx3")

    assert cm._kill_replay.cleared == ["Styx3"]


def test_on_replay_kill_finalizes_through_death_pipeline():
    server, cm = _manager_with_monster(eid=0x12, level=7)
    conn = _Conn()
    monster = cm.get_monster(0x12)

    cm._on_replay_kill(conn, monster)

    assert not cm.is_monster(0x12)            # despawned + untracked
    assert server.xp_awards == [(conn, 7)]    # XP credited at the monster level


def test_on_replay_kill_suppressed_when_telemetry_authoritative():
    """With telemetry as the kill authority (production default), the replay must
    NOT finalize the kill — the client hook reports the real kill instead, so the
    server no longer despawns the mob early or drops loot prematurely."""
    server, cm = _manager_with_monster(eid=0x12, level=7)
    server.config.telemetry_authoritative_kills = True
    monster = cm.get_monster(0x12)

    cm._on_replay_kill(_Conn(), monster)

    assert cm.is_monster(0x12)            # still tracked — replay did NOT despawn it
    assert server.xp_awards == []         # no replay-originated XP


def test_on_replay_kill_ignores_already_removed_monster():
    server, cm = _manager_with_monster(eid=0x12)
    monster = cm.get_monster(0x12)
    cm.unregister_monster(0x12)

    cm._on_replay_kill(_Conn(), monster)      # must not double-process / raise

    assert server.xp_awards == []


# ── end-to-end through the REAL engine ───────────────────────────────────────
def test_swing_replay_kills_one_hp_monster_and_awards_xp():
    """A 1-HP monster dies on the first landed swing, driving the real
    damage/weapon-cycle engine end-to-end -> award_kill_xp."""
    server, cm = _manager_with_monster(eid=0x12, hp_wire=256, level=3)
    cm._combat_rng.seed(0x8D801C2B)  # known-good pup stream (deterministic)
    conn = _Conn(player_level=3)

    now = 0.0
    cm.register_swing(conn, 0x12, now=now)
    # Advance well past the starter hit tick (~14) across a few cycles.
    for _ in range(120):
        now += 1.0 / 30.0
        cm.tick_combat(conn, now)
        if not cm.is_monster(0x12):
            break

    assert not cm.is_monster(0x12), "the replayed swings should have killed the 1-HP mob"
    assert server.xp_awards == [(conn, 3)]


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
