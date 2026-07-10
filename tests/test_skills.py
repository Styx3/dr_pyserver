"""Skills usage — hotbar place/remove, 0x39 slot equip, 0x52 self-cast, buffs.

Ports of the C# UnityGameServer hotbar block (subMessage 0x35/0x36), the
Skills::equipSkill 0x39 request, the 0x52 self-cast dispatch, and the
_buffModifierMap/ModifierTracker zone re-send. Binds the real handlers to stub
conn/server objects with an in-memory character record; asserts the wire bytes
of the Manipulators Add/Remove and Modifiers Add packets round-trip exactly.
"""
import time
import types

import pytest

from drserver.data.gc_object import hash_djb2
from drserver.data.saved_character import HotbarSlotEntry
from drserver.managers import player_modifiers
from drserver.net import skills
from drserver.util.byte_io import LEReader, LEWriter


SPRINT = "skills.generic.Sprint"
FIREBOLT = "skills.generic.FireBolt"
HP_WIRE = 68096


# ── fixtures ────────────────────────────────────────────────────────────────────

class _Char:
    def __init__(self, hotbar=None, skill_levels=None):
        self.hotbar_slots = list(hotbar or [])
        self._levels = dict(skill_levels or {})

    def get_skill_level(self, gc):
        return self._levels.get(gc.lower(), 1)


def _conn(manip_map=None, **kw):
    packets, queued, flushed = [], [], []
    conn = types.SimpleNamespace(
        conn_id=1, char_sql_id=7, login_name="Styx3",
        hp_wire=HP_WIRE, client_hp_wire=None,
        skills_component_id=0x0301, manipulators_component_id=0x0302,
        modifiers_id=0x0303,
        skill_manip_map=dict(manip_map or {}),
        tracked_modifiers={},
        current_zone_gc_type="world.town", instance_id=0,
        is_spawned=True,
        send_to_client=lambda b: packets.append(bytes(b)),
        # Per-tick flush queue — combat action acks must NOT land here (each
        # flushed ack is an extra ch7 message over the §2 budget).
        message_queue=types.SimpleNamespace(
            enqueue=lambda b: flushed.append(bytes(b))),
        # Interval queue — drained inside the per-4th-tick 0x0D frame; the
        # 0x50/0x51/0x52 action acks ride here. ``queued`` captures it.
        interval_message_queue=types.SimpleNamespace(
            enqueue=lambda b: queued.append(bytes(b))),
        **kw,
    )
    conn._flushed = flushed
    return conn, packets, queued


def _bind_repo(monkeypatch, char):
    saves = []
    fake_repo = types.SimpleNamespace(
        get_character=lambda _id: char,
        save_character=lambda ch: saves.append(ch),
    )
    monkeypatch.setattr(skills, "character_repository", fake_repo)
    return saves


def _server(*conns):
    return types.SimpleNamespace(
        connections={c.login_name: c for c in conns},
        remote_behavior_ids={},
    )


def _place_body(slot, gc, type_flag=0x00, sync=b"\x00"):
    w = LEWriter()
    w.write_uint32(slot)
    w.write_byte(type_flag)
    w.write_uint32(hash_djb2(gc))
    w.write_bytes(sync)
    return LEReader(w.to_array())


# ── DJB2 ↔ C# _skillHashToGcClass equivalence ──────────────────────────────────

@pytest.mark.parametrize("gc,expected", [
    ("skills.generic.FireBolt", 0x40243947),
    ("skills.generic.Sprint",   0x86501370),
    ("skills.generic.HealSelf", 0xBD9F10B4),
])
def test_djb2_matches_csharp_skill_hash_table(gc, expected):
    assert hash_djb2(gc) == expected


# ── hotbar PLACE (0x35) ─────────────────────────────────────────────────────────

def test_hotbar_place_persists_and_sends_manipulator_add(monkeypatch):
    conn, packets, _ = _conn(manip_map={200: SPRINT})
    char = _Char(skill_levels={SPRINT.lower(): 3})
    saves = _bind_repo(monkeypatch, char)

    assert skills.handle_skills_component_update(
        _server(conn), conn, _place_body(101, SPRINT), 0x0301, 0x35)

    assert conn.skill_manip_map == {101: SPRINT}      # moved off old slot 200
    assert saves == [char]
    assert [(h.slot, h.skill) for h in char.hotbar_slots] == [(101, SPRINT)]

    r = LEReader(packets[0])
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0302                  # Manipulators component
    assert r.read_byte() == 0x00                      # Add
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == SPRINT.lower()
    assert r.read_uint32() == 101
    assert r.read_byte() == 3                         # trained skill level
    assert r.read_byte() == 0x02 and r.read_uint32() == HP_WIRE
    assert r.read_byte() == 0x06
    assert r.remaining == 0


def test_hotbar_place_displaces_existing_slot_holder(monkeypatch):
    conn, packets, _ = _conn(manip_map={101: FIREBOLT})
    char = _Char(hotbar=[HotbarSlotEntry(slot=101, skill=FIREBOLT)])
    _bind_repo(monkeypatch, char)
    monkeypatch.setattr(skills, "_hash_catalog",
                        lambda: {hash_djb2(SPRINT): SPRINT})

    skills.handle_skills_component_update(
        _server(conn), conn, _place_body(101, SPRINT), 0x0301, 0x35)

    # FireBolt is displaced off the bar entirely (C# RemoveAll on displaced).
    assert conn.skill_manip_map == {101: SPRINT}
    assert [(h.slot, h.skill) for h in char.hotbar_slots] == [(101, SPRINT)]


def test_hotbar_place_unresolvable_hash_consumes_silently(monkeypatch):
    conn, packets, _ = _conn()
    _bind_repo(monkeypatch, _Char())
    monkeypatch.setattr(skills, "_hash_catalog", lambda: {})

    reader = _place_body(101, "skills.generic.NotARealSkill")
    assert skills.handle_skills_component_update(
        _server(conn), conn, reader, 0x0301, 0x35)
    assert packets == [] and reader.remaining == 0


def test_hotbar_place_resolves_from_skills_catalog(monkeypatch):
    """A skill NOT yet on the player's manipulators resolves via the content
    catalogue (the C# static hash table equivalent)."""
    conn, packets, _ = _conn()
    char = _Char()
    _bind_repo(monkeypatch, char)
    monkeypatch.setattr(skills, "_hash_catalog",
                        lambda: {hash_djb2(FIREBOLT): FIREBOLT})

    skills.handle_skills_component_update(
        _server(conn), conn, _place_body(105, FIREBOLT), 0x0301, 0x35)
    assert conn.skill_manip_map == {105: FIREBOLT}
    assert len(packets) == 1


# ── hotbar REMOVE (0x36) ────────────────────────────────────────────────────────

def test_hotbar_remove_clears_slot_and_sends_manipulator_remove(monkeypatch):
    conn, packets, _ = _conn(manip_map={101: SPRINT})
    char = _Char(hotbar=[HotbarSlotEntry(slot=101, skill=SPRINT)])
    saves = _bind_repo(monkeypatch, char)

    w = LEWriter(); w.write_uint32(101); w.write_byte(0x00)
    assert skills.handle_skills_component_update(
        _server(conn), conn, LEReader(w.to_array()), 0x0301, 0x36)

    assert conn.skill_manip_map == {}
    assert char.hotbar_slots == [] and saves == [char]

    r = LEReader(packets[0])
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0302
    assert r.read_byte() == 0x01                      # Remove
    assert r.read_uint32() == 101
    assert r.read_byte() == 0x02 and r.read_uint32() == HP_WIRE
    assert r.read_byte() == 0x06
    assert r.remaining == 0


# ── skill-slot equip (0x39) ─────────────────────────────────────────────────────

def test_skill_equip_0x39_consumes_exactly_and_stays_silent(monkeypatch):
    """Skills::equipSkill @ 0x5419C0 — entityRef + slot + synch suffix must be
    fully consumed (or the stream desyncs) with NO reply (a 0x38 would undo the
    client's local assignment)."""
    conn, packets, _ = _conn()
    w = LEWriter()
    w.write_byte(0xFF); w.write_cstring(SPRINT.lower())
    w.write_byte(0x03)                                 # slot index
    w.write_byte(0x02); w.write_uint32(HP_WIRE)        # synch suffix with HP
    reader = LEReader(w.to_array())

    assert skills.handle_skills_component_update(
        _server(conn), conn, reader, 0x0301, 0x39)
    assert reader.remaining == 0 and packets == []


def test_skill_equip_0x39_only_on_skills_component():
    conn, _, _ = _conn()
    reader = LEReader(b"\xff" + b"x\x00" + b"\x01\x00")
    assert not skills.handle_skills_component_update(
        _server(conn), conn, reader, 0x9999, 0x39)


# ── self-cast (0x52) ────────────────────────────────────────────────────────────

def test_self_cast_acks_relays_and_tracks_buff(monkeypatch):
    monkeypatch.delenv("DR_NO_HP_HEARTBEAT", raising=False)
    conn, _, queued = _conn(manip_map={102: SPRINT})
    char = _Char(skill_levels={SPRINT.lower(): 2})
    _bind_repo(monkeypatch, char)

    viewer, viewer_packets, _ = _conn()
    viewer.login_name = "Other"
    viewer.viewer_action_pending = False
    server = _server(conn, viewer)
    server.remote_behavior_ids = {"Other": {"Styx3": 0x0500}}

    skills.handle_self_cast(server, conn,
                            LEReader(bytes([0x21, 102])), 0x0207, 0x05)

    # ── ActionResponse echo to the caster (interval queue, never the
    # per-tick flush — held-button casts are a sustained stream, §2) ──
    assert conn._flushed == []
    r = LEReader(queued[0])
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0207
    assert (r.read_byte(), r.read_byte()) == (0x01, 0x05)
    assert (r.read_byte(), r.read_byte(), r.read_byte()) == (0x52, 0x21, 102)
    assert r.read_byte() == 0x02 and r.read_uint32() == HP_WIRE
    assert r.remaining == 0

    # ── CreateAction relay to the same-zone viewer (unified action_relay:
    # FRAMED-direct so it stays ordered with the movement relay; mode byte
    # normalized to 0x00, empty synch — no actor HP asserted to a viewer) ──
    r = LEReader(viewer_packets[0])
    assert r.read_byte() == 0x07                      # BeginStream
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0500                  # remapped behavior id
    assert (r.read_byte(), r.read_byte()) == (0x04, 0x52)
    assert (r.read_byte(), r.read_byte()) == (0x00, 102)   # mode 0, slot
    assert r.read_byte() == 0x00                      # empty synch
    assert r.read_byte() == 0x06                      # EndStream
    assert r.remaining == 0

    # ── Sprint buff tracked for zone re-send: (30 + lv*10) s in 24ms ticks ──
    mod = conn.tracked_modifiers["skills.generic.sprint.modifier"]
    assert mod.gc_type == "skills.generic.Sprint.Modifier"
    assert mod.duration_ticks == int((30 + 2 * 10) * 1000 / 24)
    assert mod.source_is_self == 0x01 and mod.level == 2


def test_self_cast_ack_sent_in_combat_zone_not_dropped(monkeypatch):
    """Regression (live 2026-07-02): the Regime-B posture must NOT drop the 0x52
    self-cast ack in a combat zone. ``handle_self_cast`` gated the whole ack on
    ``suppress_originated_avatar_hp`` (True outside town/tutorial), so in every
    dungeon a self-cast never resolved client-side — no animation, no effect,
    and the client re-sent the cast on retry cadence (the Stomp 3-casts/s log).
    Action acks are load-bearing responses to the client's OWN packet — the
    exact bug class the 0x50/0x51 acks were already fixed for."""
    monkeypatch.delenv("DR_AVATAR_HP_ORIGINATE", raising=False)
    conn, _, queued = _conn(manip_map={100: "skills.generic.Stomp"})
    conn.current_zone_gc_type = "world.dungeon01.level01"   # suppress → True
    _bind_repo(monkeypatch, _Char())

    skills.handle_self_cast(_server(conn), conn,
                            LEReader(bytes([0x00, 100])), 0x0219, 0)

    assert conn._flushed == []
    assert len(queued) == 1
    r = LEReader(queued[0])
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0219
    assert (r.read_byte(), r.read_byte()) == (0x01, 0x00)
    assert (r.read_byte(), r.read_byte(), r.read_byte()) == (0x52, 0x00, 100)
    # flags=0x02 + clamped HP — a flags=0x00 trailer is DISPROVEN (crashes a
    # healthy avatar; movement._SUPPRESS_OWNER_AVATAR_HP).
    assert r.read_byte() == 0x02 and r.read_uint32() == HP_WIRE
    assert r.remaining == 0


def test_self_cast_relay_skips_other_zones_and_instances(monkeypatch):
    monkeypatch.delenv("DR_NO_HP_HEARTBEAT", raising=False)
    conn, _, _ = _conn()
    _bind_repo(monkeypatch, _Char())
    far, far_packets, _ = _conn()
    far.login_name = "Far"; far.current_zone_gc_type = "world.dungeon01"
    far.viewer_action_pending = False
    inst, inst_packets, _ = _conn()
    inst.login_name = "Inst"; inst.instance_id = 9
    inst.viewer_action_pending = False
    server = _server(conn, far, inst)
    server.remote_behavior_ids = {"Far": {"Styx3": 1}, "Inst": {"Styx3": 2}}

    skills.handle_self_cast(server, conn, LEReader(bytes([1, 50])), 0x0207, 1)
    # Different zone / different instance → the relay (framed-direct) skips both.
    assert far_packets == [] and inst_packets == []


def test_self_cast_unknown_slot_tracks_nothing(monkeypatch):
    monkeypatch.delenv("DR_NO_HP_HEARTBEAT", raising=False)
    conn, _, queued = _conn()
    _bind_repo(monkeypatch, _Char())
    skills.handle_self_cast(_server(conn), conn,
                            LEReader(bytes([1, 99])), 0x0207, 1)
    assert conn.tracked_modifiers == {} and len(queued) == 1


# ── movement dispatch: 0x52 short-form routes to self-cast ─────────────────────

def test_component_update_routes_short_0x52_to_self_cast(monkeypatch):
    from drserver.net import movement
    monkeypatch.delenv("DR_NO_HP_HEARTBEAT", raising=False)
    conn, _, queued = _conn(manip_map={})
    _bind_repo(monkeypatch, _Char())
    server = _server(conn)
    server.combat = None

    # [cid u16][sub 0x01][respId][0x52][sessionID][slotID] — nothing else.
    w = LEWriter()
    w.write_uint16(0x0207); w.write_byte(0x01)
    w.write_byte(0x09); w.write_byte(0x52); w.write_byte(0x11); w.write_byte(103)
    assert movement._component_update(server, conn, LEReader(w.to_array()))

    r = LEReader(queued[0])
    r.read_byte(); r.read_uint16()
    assert (r.read_byte(), r.read_byte(), r.read_byte()) == (0x01, 0x09, 0x52)
    assert (r.read_byte(), r.read_byte()) == (0x11, 103)


def test_component_update_acks_suffixless_0x51_position_cast(monkeypatch):
    """A 0x51 UsePosition with NO trailing synch suffix (11 position bytes
    remaining — the C# read shape, UGS:12551) must still be acked; the old
    >= 12 guard silently dropped it (Shift-cast / right-click skill)."""
    from drserver.net import movement
    monkeypatch.delenv("DR_NO_HP_HEARTBEAT", raising=False)
    conn, _, queued = _conn()
    server = _server(conn)
    server.combat = None

    w = LEWriter()
    w.write_uint16(0x0207); w.write_byte(0x01)        # cid + action dispatch
    w.write_byte(0x07); w.write_byte(0x51)            # respId + UsePosition
    w.write_byte(0x33)                                # sessionID
    w.write_byte(105)                                 # actionID (slot)
    w.write_int32(480 * 256); w.write_int32(-191 * 256); w.write_int32(0)
    assert movement._component_update(server, conn, LEReader(w.to_array()))

    r = LEReader(queued[0])
    assert r.read_byte() == 0x35 and r.read_uint16() == 0x0207
    assert (r.read_byte(), r.read_byte()) == (0x01, 0x07)
    assert (r.read_byte(), r.read_byte(), r.read_byte()) == (0x51, 0x33, 105)
    assert r.read_uint32() == (480 * 256) & 0xFFFFFFFF
    assert r.read_uint32() == (-191 * 256) & 0xFFFFFFFF
    assert r.read_uint32() == 0
    assert r.read_byte() == 0x02 and r.read_uint32() == HP_WIRE


def test_sub_0x36_on_non_skills_component_is_not_hotbar_remove(monkeypatch):
    """A stray 0x36 sub-update on a component other than Skills must NOT be
    consumed as a hotbar remove (it would emit a garbage Manipulators Remove
    and delete a live skill/weapon manipulator client-side)."""
    conn, packets, _ = _conn(manip_map={100: SPRINT})
    w = LEWriter(); w.write_uint32(100); w.write_byte(0x00)

    assert not skills.handle_skills_component_update(
        _server(conn), conn, LEReader(w.to_array()), 0x0999, 0x36)
    assert packets == []
    assert conn.skill_manip_map == {100: SPRINT}


# ── tracked modifiers (managers.player_modifiers) ───────────────────────────────

def test_buff_for_skill_short_name_lookup():
    assert player_modifiers.buff_for_skill("skills.generic.Sprint")[0] == \
        "skills.generic.Sprint.Modifier"
    assert player_modifiers.buff_for_skill("SKILLS.GENERIC.MANASHIELD")[0] == \
        "skills.generic.ManaShield.Modifier"
    assert player_modifiers.buff_for_skill("skills.generic.FireBolt") is None


def test_track_skill_buff_recast_replaces_instance():
    conn, _, _ = _conn()
    assert player_modifiers.track_skill_buff(conn, SPRINT, 1)
    first = conn.tracked_modifiers["skills.generic.sprint.modifier"].mod_id
    assert player_modifiers.track_skill_buff(conn, SPRINT, 1)
    again = conn.tracked_modifiers["skills.generic.sprint.modifier"].mod_id
    assert again != first and len(conn.tracked_modifiers) == 1


def test_active_modifiers_decrements_duration_and_drops_expired():
    conn, _, _ = _conn()
    player_modifiers.track_skill_buff(conn, SPRINT, 1)        # 40 s
    player_modifiers.track_skill_buff(conn, "skills.generic.Stomp", 1)  # permanent
    # Age Sprint past expiry (40 s duration, 3 s zone buffer).
    key = "skills.generic.sprint.modifier"
    aged = conn.tracked_modifiers[key]
    conn.tracked_modifiers[key] = player_modifiers.TrackedModifier(
        gc_type=aged.gc_type, mod_id=aged.mod_id, level=aged.level,
        duration_ticks=aged.duration_ticks, source_is_self=aged.source_is_self,
        added_at=time.monotonic() - 60.0)

    mods = player_modifiers.active_modifiers(conn)
    assert [m.gc_type for m in mods] == ["skills.generic.Stomp.VisualModifier"]
    assert key not in conn.tracked_modifiers                  # expired dropped


def test_active_modifiers_remaining_duration_shrinks():
    conn, _, _ = _conn()
    player_modifiers.track_skill_buff(conn, "skills.generic.ManaShield", 1)
    full = int(180 * 1000 / 24)
    (mod,) = player_modifiers.active_modifiers(conn)
    # 3 s zone buffer is pre-deducted from the re-sent duration.
    assert mod.duration_ticks < full
    assert mod.duration_ticks >= full - int(4.0 * 1000 / 24)


def test_resend_all_emits_modifier_add_packets():
    conn, packets, _ = _conn()
    player_modifiers.track_skill_buff(conn, SPRINT, 2)
    assert player_modifiers.resend_all(conn) == 1

    r = LEReader(packets[0])
    assert r.read_byte() == 0x07
    assert r.read_byte() == 0x35
    assert r.read_uint16() == 0x0303                  # Modifiers component
    assert r.read_byte() == 0x00                      # Add
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "skills.generic.Sprint.Modifier"
    r.read_uint32()                                   # instance id
    assert r.read_byte() == 2                         # level
    assert r.read_uint32() == 0                       # power level
    assert 0 < r.read_uint32() <= int(50 * 1000 / 24)  # remaining duration
    assert r.read_byte() == 0x01                      # source-is-self
    assert r.read_byte() == 0x02 and r.read_uint32() == HP_WIRE
    assert r.read_byte() == 0x06
    assert r.remaining == 0


def test_resend_all_without_modifiers_component_is_noop():
    conn, packets, _ = _conn()
    conn.modifiers_id = 0
    player_modifiers.track_skill_buff(conn, SPRINT, 1)
    assert player_modifiers.resend_all(conn) == 0 and packets == []
