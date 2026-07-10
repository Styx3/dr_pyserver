"""Wire-level quest manager — catalogue loading, instance ids, packets, progress.

Loads the real shipped catalogue (the old stub read columns that don't exist and
loaded zero quests), then exercises the accept → progress → turn-in lifecycle
against stub conn/server objects with an in-memory character repository. Quest
packets are decoded back from their bytes to assert the wire layout matches the
C# QuestManager senders.
"""
import sys
import types

import pytest

sys.path.insert(0, "tests")
from _paths import copy_shipped_db, has_shipped_db  # noqa: E402

from drserver.managers import quest_wire  # noqa: E402
from drserver.util.byte_io import LEReader  # noqa: E402


@pytest.fixture(scope="module")
def catalog():
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    from drserver.managers.quests import QuestManager
    qm = QuestManager(types.SimpleNamespace())
    qm.load()
    return qm


def _make_char(level=10):
    return types.SimpleNamespace(
        level=level, gold=1000, experience=0,
        active_quests=[], completed_quests=[],
    )


def _bind(monkeypatch, qm, char):
    """Stub conn + in-memory repo; capture sent packets and system messages."""
    from drserver.managers import quests as quests_mod
    from drserver.net import inventory as inventory_mod
    qm._states.clear()                            # module-scoped catalog: isolate tests
    saves, packets, messages = [], [], []
    fake_repo = types.SimpleNamespace(
        get_character=lambda _id: char,
        save_character=lambda ch: saves.append(ch),
    )
    monkeypatch.setattr(quests_mod, "character_repository", fake_repo)
    # Gold reward now flows through inventory.give_gold (port of C# GiveGold), so
    # its character_repository must hit the same in-memory char as the manager's.
    monkeypatch.setattr(inventory_mod, "character_repository", fake_repo)

    conn = types.SimpleNamespace(
        conn_id=7, char_sql_id=1, login_name="Styx3",
        quest_manager_id=0x0210, hp_wire=68096, client_hp_wire=None,
        next_quest_instance_id=1, current_dialog_npc_id=None,
        current_zone_gc_type="world.town", pending_quest_hash=0,
        pending_quest_npc_entity_id=0, viewing_quest_instance_id=0,
        pending_turn_in_instance_id=0,
        turn_in_dialog_instance_id=0, dialog_teardown_instance_id=0,
        send_to_client=lambda b: packets.append(bytes(b)),
        send_system_message=lambda m: messages.append(m),
    )
    return conn, saves, packets, messages


def _pick_quest(qm, level=10):
    """A catalogue quest with objectives and a level window around ``level``."""
    for t in qm._templates.values():
        if t.objectives and t.level <= level <= t.max_level:
            return t
    raise AssertionError("no suitable quest in catalogue")


def _pick_quest_no_item(qm, level=10):
    """A quest whose objectives are all non-item, so manually marking them
    complete is not overridden by the inventory-driven item-objective sync
    (item objectives reflect *held* quantity — see ``_sync_item_objectives``)."""
    for t in qm._templates.values():
        if (t.objectives and t.level <= level <= t.max_level
                and not any(o.type == "item" for o in t.objectives)):
            return t
    raise AssertionError("no suitable non-item quest in catalogue")


def _decode_add(packet):
    r = LEReader(packet)
    out = {"begin": r.read_byte(), "cu": r.read_byte(), "qm": r.read_uint16(),
           "submsg": r.read_byte(), "gc_ind": r.read_byte(), "hash": r.read_uint32(),
           "instance": r.read_uint32(), "all_complete": r.read_byte()}
    count = r.read_byte()
    out["objectives"] = []
    for _ in range(count):
        flags = r.read_byte()
        label = r.read_cstring()
        req = r.read_uint16()
        out["objectives"].append((flags, label, req))
    # QuestManager is a PLAYER-OBJECT component (no HP) → flags-only 0x00 synch
    # trailer, NO HP uint32 (write_synch_none). Asserting HP here crashes an
    # unpatched client's zero-tolerance compare (2026-06-13 tutorial desync).
    out["synch_flag"] = r.read_byte()
    out["end"] = r.read_byte()
    return out


# ── catalogue ─────────────────────────────────────────────────────────────────

def test_catalog_loads_from_real_schema(catalog):
    assert len(catalog._templates) > 1000
    assert len(catalog._by_hash) == len(catalog._templates)


def test_kill_objective_targets_enriched_from_raw_json(catalog):
    q = catalog._templates.get("world.dungeon03.quest.Q04_a1")
    assert q is not None
    kill = next(o for o in q.objectives if o.type == "kill")
    assert "world.dungeon03.mob.quest_level2a" in kill.targets


def test_quest_hash_matches_djb2_lowercase():
    # h*33 over the lowercased string (C# ComputeDJB2Hash == gc_object.hash_djb2).
    expected = 5381
    for c in "world.town.quest.x":
        expected = (expected * 33 + ord(c)) & 0xFFFFFFFF
    assert quest_wire.quest_hash("World.Town.Quest.X") == expected


# ── accept ────────────────────────────────────────────────────────────────────

def test_accept_assigns_instance_id_and_sends_add_packet(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, saves, packets, _ = _bind(monkeypatch, catalog, char)

    catalog.handle_accept_confirmed(conn, npc_entity_id=42,
                                    quest_hash=quest_wire.quest_hash(tmpl.quest_id))

    state = catalog.get_player_state(conn)
    assert len(state.active_quests) == 1
    rq = state.active_quests[0]
    assert rq.quest_id == tmpl.quest_id
    assert rq.instance_id == 1
    add = _decode_add(packets[0])
    assert (add["begin"], add["cu"], add["qm"], add["submsg"]) == (0x07, 0x35, 0x0210, 0x01)
    assert add["hash"] == quest_wire.quest_hash(tmpl.quest_id)
    assert add["instance"] == 1
    assert (add["synch_flag"], add["end"]) == (0x00, 0x06)   # player-object: no HP
    assert saves                                  # persisted


def test_accept_twice_is_rejected(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    h = quest_wire.quest_hash(tmpl.quest_id)

    catalog.handle_accept_confirmed(conn, 42, h)
    n_after_first = len(catalog.get_player_state(conn).active_quests)
    catalog.handle_accept_confirmed(conn, 42, h)

    assert len(catalog.get_player_state(conn).active_quests) == n_after_first == 1


# ── progress ──────────────────────────────────────────────────────────────────

def test_kill_advances_matching_objective_and_sends_progress(monkeypatch, catalog):
    tmpl = catalog._templates.get("world.dungeon03.quest.Q04_a1")
    char = _make_char(level=tmpl.level)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    packets.clear()

    catalog.on_creature_killed(conn, "world.dungeon03.mob.quest_level2a")

    rq = catalog.get_player_state(conn).active_quests[0]
    obj = next(o for o in rq.objectives if o.type == "kill")
    assert obj.current == 1
    # send_progress_packet emits two packets (objective list + complete flag).
    assert len(packets) == 2
    assert packets[0][4] == 0x03                  # QM submessage processUpdateQuest (after qm_id u16)


def test_unrelated_kill_does_not_advance(monkeypatch, catalog):
    tmpl = catalog._templates.get("world.dungeon03.quest.Q04_a1")
    char = _make_char(level=tmpl.level)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    packets.clear()

    catalog.on_creature_killed(conn, "world.dungeon99.mob.unrelated")

    rq = catalog.get_player_state(conn).active_quests[0]
    assert all(o.current == 0 for o in rq.objectives)
    assert packets == []


# ── turn-in / abandon ──────────────────────────────────────────────────────────

def test_turn_in_grants_gold_and_finalizes(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, saves, packets, messages = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:                       # force-complete objectives
        o.current = o.required
    packets.clear()
    gold_before = char.gold

    catalog.handle_turn_in_confirmed(conn, rq.instance_id)

    state = catalog.get_player_state(conn)
    assert state.active_quests == []
    assert tmpl.quest_id in state.completed_quests
    assert char.gold > gold_before                # reward applied
    # finalize (0x08) + remove (0x02) packets sent (submsg at index 4, after qm_id).
    submsgs = [p[4] for p in packets if len(p) > 4 and p[1] == 0x35]
    assert 0x08 in submsgs and 0x02 in submsgs


def test_abandon_removes_quest_and_sends_remove(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    packets.clear()

    catalog.handle_abandon(conn, rq.instance_id)

    assert catalog.get_player_state(conn).active_quests == []
    r = LEReader(packets[0])
    assert [r.read_byte(), r.read_byte()] == [0x07, 0x35]
    assert r.read_uint16() == 0x0210
    assert r.read_byte() == 0x02                   # RemoveQuest submessage
    assert r.read_uint32() == rq.instance_id


# ── inbound component-update dispatch ──────────────────────────────────────────

def test_component_update_0x06_query_shows_dialog(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    h = quest_wire.quest_hash(tmpl.quest_id)

    # 0x06 body: u32 npcEntityId, byte gcInd, u32 hash.
    body = LEReader(bytes([5, 0, 0, 0, 0x04]) + h.to_bytes(4, "little"))
    catalog.handle_component_update(conn, 0x06, body)

    # First 0x06 sets pending hash and sends the accept dialog (submsg 0x04 at index 4).
    assert conn.pending_quest_hash == h
    assert packets and packets[0][4] == 0x04


def test_component_update_0x05_turns_in_from_payload_instance_id(monkeypatch, catalog):
    """Grounded turn-in (bible §13.5 #1 / live op4->recv06->op5): op 0x05 carries
    `u32 instanceId · u8` and finalizes that quest — even with NO server-set
    pending_turn_in (the wishing-well AutoAcceptOnQuery path that used to silently
    fail because the old handler only matched a 9-byte accept body)."""
    tmpl = _pick_quest_no_item(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:
        o.current = o.required
    assert conn.pending_turn_in_instance_id == 0          # no server-set pending

    # op 0x05 body = u32 instanceId + trailing confirm byte.
    body = LEReader(rq.instance_id.to_bytes(4, "little") + bytes([1]))
    catalog.handle_component_update(conn, 0x05, body)

    state = catalog.get_player_state(conn)
    assert state.active_quests == []
    assert tmpl.quest_id in state.completed_quests


def test_component_update_0x05_falls_back_to_pending_turn_in(monkeypatch, catalog):
    """An empty-payload 0x05 finalizes the server-set pending turn-in instance."""
    tmpl = _pick_quest_no_item(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:
        o.current = o.required
    quest_wire.send_turn_in_dialog(conn, rq.instance_id)   # sets pending turn-in
    assert conn.pending_turn_in_instance_id == rq.instance_id

    catalog.handle_component_update(conn, 0x05, LEReader(b""))

    state = catalog.get_player_state(conn)
    assert state.active_quests == []
    assert tmpl.quest_id in state.completed_quests
    assert conn.pending_turn_in_instance_id == 0


def test_component_update_0x01_empty_confirms_pending_accept(monkeypatch, catalog):
    """The live client's Accept button is submsg 0x01 with an EMPTY payload
    (C# UGS:12012) — regression for the dead 'press Accept -> nothing'."""
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    h = quest_wire.quest_hash(tmpl.quest_id)
    # First 0x06 = query: sets the pending hash + shows the dialog.
    catalog.handle_component_update(
        conn, 0x06, LEReader(bytes([5, 0, 0, 0, 0x04]) + h.to_bytes(4, "little")))
    packets.clear()

    catalog.handle_component_update(conn, 0x01, LEReader(b""))

    state = catalog.get_player_state(conn)
    assert len(state.active_quests) == 1
    assert state.active_quests[0].quest_id == tmpl.quest_id
    assert conn.pending_quest_hash == 0
    assert packets and packets[0][4] == 0x01      # AddQuest sent to the journal


def test_component_update_0x01_empty_confirms_pending_turn_in(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:
        o.current = o.required
    quest_wire.send_turn_in_dialog(conn, rq.instance_id)   # sets pending turn-in
    packets.clear()

    catalog.handle_component_update(conn, 0x01, LEReader(b""))

    state = catalog.get_player_state(conn)
    assert state.active_quests == []
    assert tmpl.quest_id in state.completed_quests
    assert conn.pending_turn_in_instance_id == 0


def test_component_update_0x01_empty_without_pending_is_view(monkeypatch, catalog):
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)

    catalog.handle_component_update(conn, 0x01, LEReader(b""))

    assert conn.viewing_quest_instance_id == 1
    assert packets == []                          # no packet, nothing accepted
    assert catalog.get_player_state(conn).active_quests == []


def test_component_update_0x04_complete_quest_sends_turn_in_dialog(monkeypatch, catalog):
    """Clicking a fully-complete quest (log / yellow '?') sends 0x04 + instance
    id; the server must answer with the turn-in dialog (C# [QUEST-0x04])."""
    tmpl = _pick_quest_no_item(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:
        o.current = o.required
    packets.clear()

    catalog.handle_component_update(
        conn, 0x04, LEReader(rq.instance_id.to_bytes(4, "little")))

    assert conn.pending_turn_in_instance_id == rq.instance_id
    r = LEReader(packets[0])
    assert [r.read_byte(), r.read_byte()] == [0x07, 0x35]
    assert r.read_uint16() == 0x0210
    assert r.read_byte() == 0x06                  # turn-in dialog submessage
    assert r.read_uint32() == rq.instance_id


def test_component_update_0x04_incomplete_quest_no_dialog(monkeypatch, catalog):
    tmpl = _pick_quest(catalog, level=10)
    char = _make_char(level=10)
    conn, _, packets, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    packets.clear()

    catalog.handle_component_update(
        conn, 0x04, LEReader(rq.instance_id.to_bytes(4, "little")))

    assert conn.pending_turn_in_instance_id == 0
    assert packets == []


def test_turn_in_dialog_back_does_not_abandon_quest(monkeypatch, catalog):
    """Bug #9 (live x64dbg 2026-07-02): backing out of the turn-in dialog sends
    0x04 -> 0x02 -> 0x03; the trailing 0x03 is the client's dialog teardown, NOT a
    user Abandon — it must leave the still-active quest intact."""
    tmpl = _pick_quest_no_item(catalog, level=10)
    char = _make_char(level=10)
    conn, _, _, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:                        # turn-in-ready (so 0x04 shows dialog)
        o.current = o.required
    inst = rq.instance_id.to_bytes(4, "little")

    catalog.handle_component_update(conn, 0x04, LEReader(inst))   # open turn-in dialog
    catalog.handle_component_update(conn, 0x02, LEReader(b""))    # Back / close
    catalog.handle_component_update(conn, 0x03, LEReader(inst))   # dialog teardown

    state = catalog.get_player_state(conn)
    assert rq in state.active_quests               # kept active — NOT abandoned
    assert tmpl.quest_id not in state.completed_quests


def test_turn_in_complete_sequence_finalizes_and_teardown_is_harmless(monkeypatch, catalog):
    """Complete sends 0x04 -> 0x05 -> 0x02 -> 0x03. The 0x05 finalizes the quest;
    the trailing teardown 0x03 must not resurrect/mangle anything (quest stays
    completed, not re-added to active)."""
    tmpl = _pick_quest_no_item(catalog, level=10)
    char = _make_char(level=10)
    conn, _, _, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]
    for o in rq.objectives:
        o.current = o.required
    inst = rq.instance_id.to_bytes(4, "little")

    catalog.handle_component_update(conn, 0x04, LEReader(inst))
    catalog.handle_component_update(conn, 0x05, LEReader(inst + bytes([1])))
    catalog.handle_component_update(conn, 0x02, LEReader(b""))
    catalog.handle_component_update(conn, 0x03, LEReader(inst))

    state = catalog.get_player_state(conn)
    assert state.active_quests == []
    assert tmpl.quest_id in state.completed_quests


def test_genuine_abandon_without_turn_in_dialog_still_removes(monkeypatch, catalog):
    """A real Abandon (quest-log button) is a standalone 0x03 with no preceding
    turn-in dialog. An incomplete quest never shows a turn-in dialog, so the
    teardown guard never arms — the abandon must go through."""
    tmpl = _pick_quest(catalog, level=10)          # has objectives -> incomplete
    char = _make_char(level=10)
    conn, _, _, _ = _bind(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, quest_wire.quest_hash(tmpl.quest_id))
    rq = catalog.get_player_state(conn).active_quests[0]

    catalog.handle_component_update(
        conn, 0x03, LEReader(rq.instance_id.to_bytes(4, "little")))

    assert catalog.get_player_state(conn).active_quests == []   # abandoned


# ── movement dispatch routing (channel-7 ComponentUpdate -> QuestManager) ──────

def _dispatch_conn():
    return types.SimpleNamespace(
        login_name="tester", quest_manager_id=0x0210,
        equipment_component_id=0x9001, unit_container_id=0x9002,
    )


def test_movement_routes_empty_0x01_on_qm_component_to_quests():
    """An empty-payload 0x01 on the QM component is the Accept/Complete confirm;
    it must reach the quest manager, not be swallowed by the action branch."""
    from drserver.net import movement

    conn = _dispatch_conn()
    calls = []
    server = types.SimpleNamespace(quests=types.SimpleNamespace(
        handle_component_update=lambda c, sub, r: calls.append(sub)))

    body = conn.quest_manager_id.to_bytes(2, "little") + bytes([0x01])
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert calls == [0x01]


def test_movement_routes_0x04_on_qm_component_to_quests():
    from drserver.net import movement

    conn = _dispatch_conn()
    calls = []
    server = types.SimpleNamespace(quests=types.SimpleNamespace(
        handle_component_update=lambda c, sub, r: calls.append(sub)))

    body = (conn.quest_manager_id.to_bytes(2, "little") + bytes([0x04])
            + (7).to_bytes(4, "little"))
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert calls == [0x04]


def test_movement_routes_0x08_on_qm_component_to_quests():
    """op 0x08 (NPCTeleporter request) on the QM component must reach the quest
    manager — regression for the dispatcher dropping it (handler existed but
    0x08 was missing from the QM sub-message routing tuple)."""
    from drserver.net import movement

    conn = _dispatch_conn()
    calls = []
    server = types.SimpleNamespace(quests=types.SimpleNamespace(
        handle_component_update=lambda c, sub, r: calls.append(sub)))

    body = (conn.quest_manager_id.to_bytes(2, "little") + bytes([0x08])
            + (1573).to_bytes(4, "little"))
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert calls == [0x08]


def test_movement_routes_0x0a_on_qm_component_to_town_portal():
    from drserver.net import movement

    conn = _dispatch_conn()
    conn.has_saved_town_portal = True
    conn.town_portal_zone_name = "town"
    conn.town_portal_pos_x, conn.town_portal_pos_y, conn.town_portal_pos_z = 1.0, 2.0, 3.0
    captured = {}
    server = types.SimpleNamespace(
        quests=None,
        change_zone_to_position=lambda c, z, x, y, zz: captured.update(
            zone=z, pos=(x, y, zz)))

    body = conn.quest_manager_id.to_bytes(2, "little") + bytes([0x0A])
    handled = movement._component_update(server, conn, LEReader(body))

    assert handled is True
    assert captured == {"zone": "town", "pos": (1.0, 2.0, 3.0)}


# ── available-quest markers: spawned-NPC case canonicalisation ──────────────────

def test_marker_npc_keys_match_spawned_case_in_town(monkeypatch, catalog):
    """The town quest-giver markers must be keyed by the SPAWNED NPC's exact-case
    gc_type (the client matches case-sensitively), not the quests table's
    inconsistent raw npc (``world.town.NPC.TownCommander``). Regression for
    "quests work in tutorial but not town" — root cause was the case mismatch
    leaving town markers unattached."""
    from drserver.managers.npcs import npc_manager
    npc_manager.load()
    spawned = {nd.gc_type for nd in npc_manager.get_for_zone("town")}
    if "world.town.npc.TownCommander" not in spawned:
        pytest.skip("town NPC set not present in shipped DB")

    char = _make_char(level=20)
    conn, _, _, _ = _bind(monkeypatch, catalog, char)
    conn.current_zone_gc_type = "world.town"
    conn.current_zone_name = "town"

    by_npc = catalog.available_quests_by_npc(conn)

    # TownCommander is offered under the exact spawned case, with quests.
    assert "world.town.npc.TownCommander" in by_npc
    assert by_npc["world.town.npc.TownCommander"]
    # No marker key carries the wrong-case raw form the quests table stores.
    assert "world.town.NPC.TownCommander" not in by_npc
    # Every key that names a town NPC matches a spawned entity exactly.
    spawned_lower = {s.lower() for s in spawned}
    for key in by_npc:
        if key.lower() in spawned_lower:
            assert key in spawned, f"marker key {key!r} not in spawned exact case"


# ── spawn QM-component block ───────────────────────────────────────────────────

def test_write_quest_entries_emits_active_quests(monkeypatch, catalog):
    from drserver.managers.quests import RuntimeObjective, RuntimeQuest
    from drserver.util.byte_io import LEWriter
    rq = RuntimeQuest(quest_id="world.town.quest.x", instance_id=3,
                      objectives=[RuntimeObjective(label="Slay", required=5, current=2)])
    w = LEWriter()
    quest_wire.write_quest_entries(w, [rq])

    r = LEReader(w.to_array())
    assert r.read_uint16() == 1                    # one active quest
    # write_gc_type tag (0xFF) + gc_type cstring, then instance + flags + objectives.
    assert r.read_byte() == 0xFF
    assert r.read_cstring() == "world.town.quest.x"
    assert r.read_uint32() == 3
    assert r.read_byte() == 0x00                    # not all-done
    assert r.read_byte() == 1                       # one objective
    flags = r.read_byte()
    assert flags == 0x02                            # incomplete (no 0x01)
    assert r.read_cstring() == "Slay: 2 / 5"
    assert r.read_uint16() == 5


# ── player-object synch trailer (the unpatched-client crash regression) ─────────

def test_available_quest_update_trailer_is_flags_only_no_hp():
    """The QuestManager (player-object component) update MUST end with a flags-
    only 0x00 synch trailer — NO HP uint32 — even when the connection HAS an
    hp_wire. x64dbg-proven 2026-06-13: a 0x02+HP trailer here crashes an
    UNPATCHED client (player object isn't an HP unit → local flags 0 ≠ server
    flags 2 → fatal Entity synch error). See write_synch_none / bible.md §4."""
    conn = types.SimpleNamespace(
        quest_manager_id=0x0218,                    # = the live crash cid (536)
        hp_wire=300 * 256, client_hp_wire=None,     # HP present but must NOT be emitted
        sent=[],
    )
    conn.send_to_client = lambda b: conn.sent.append(bytes(b))

    quest_wire.send_available_quest_update(conn, {"world.town.npc.Foo": [123]})

    packet = conn.sent[0]
    assert packet[0] == 0x07 and packet[1] == 0x35   # BeginStream + ComponentUpdate
    # Trailer is the last 2 bytes: flags 0x00, EndStream 0x06 — NOT 0x02 + 4B HP.
    assert packet[-2:] == bytes([0x00, 0x06]), packet.hex()
    assert (300 * 256).to_bytes(4, "little") not in packet, "HP must not ride the QM trailer"


# ── raw_json objective parsing (replaces the stale quest_objective_templates) ────

def test_objectives_parsed_from_raw_json_well_and_token(catalog):
    """The well + token-trade quests were dropped by the stale
    quest_objective_templates table; their objectives now come from raw_json."""
    well = catalog._templates.get("world.town.quest.well.base")
    assert well is not None and well.auto_accept_on_query is True
    assert well.objectives == []                       # 0-objective AutoAcceptOnQuery

    token = catalog._templates.get("world.dungeon02.quest.token.MythJewelry")
    assert token is not None and token.auto_accept_on_query is True
    assert len(token.objectives) == 1
    obj = token.objectives[0]
    assert obj.type == "item"
    assert obj.targets == ["QuestItemPAL.Token"]       # King's Coin
    assert obj.required == 75
    assert obj.remove_on_finalize is True

    sq = catalog._templates.get("world.dungeon01.quest.Squeakeasy_Token_Trade")
    assert sq is not None and sq.token_reward == 1     # pays a King's Coin
    assert sq.objectives[0].type == "item" and sq.objectives[0].required == 20


# ── can_query_complete (C# CanQueryComplete: AutoAcceptOnQuery + inventory) ──────

def _bind_with_inventory(monkeypatch, qm, char, item_rows=()):
    """``_bind`` conn augmented with a real inventory model (unit_container_id 0 =
    no live wire send; the model still mutates + persists)."""
    from drserver.net import inventory as inventory_mod
    from drserver.net.inventory_model import InventoryModel
    conn, saves, packets, messages = _bind(monkeypatch, qm, char)
    model = InventoryModel()
    model.load(list(item_rows))
    conn.inv_model = model
    conn.unit_container_id = 0
    char.inventory = []
    return conn, model, packets


def test_can_query_complete_zero_objective(monkeypatch, catalog):
    """A 0-objective quest is turn-in-ready as soon as it is active — there is
    nothing left to do. This holds whether or not AutoAcceptOnQuery is set:
    AutoAcceptOnQuery governs the *accept* step, not completion. Live-confirmed
    2026-06-21 that gating completion on it dropped the client's 0x04 turn-in
    query for plain 0-objective quests like "Skill Trainers" (bible §13)."""
    from drserver.managers.quests import RuntimeQuest
    char = _make_char(level=10)
    conn, _model, _ = _bind_with_inventory(monkeypatch, catalog, char)

    # AutoAcceptOnQuery 0-objective quest (the wishing well).
    well = RuntimeQuest(quest_id="world.town.quest.well.base", instance_id=1)
    assert catalog.can_query_complete(conn, well) is True

    # Plain 0-objective, NON-AutoAccept quest (e.g. the Skill Trainers chain) is
    # ALSO turn-in-ready — the regression this fix addresses.
    non_auto = next(t for t in catalog._templates.values()
                    if not t.objectives and not t.auto_accept_on_query)
    rq = RuntimeQuest(quest_id=non_auto.quest_id, instance_id=2)
    assert catalog.can_query_complete(conn, rq) is True


def test_can_query_complete_item_objective_needs_inventory(monkeypatch, catalog):
    """A token-trade item objective (King's Coin x75) is complete only when the
    bag actually holds the coins — the truth is inventory, not a pickup counter."""
    token_id = "world.dungeon02.quest.token.MythJewelry"
    h = quest_wire.quest_hash(token_id)

    # No coins → accept leaves the objective incomplete → not turn-in-ready.
    char = _make_char(level=20)
    conn, model, _ = _bind_with_inventory(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, h)
    rq = catalog.get_player_state(conn).active_quests[0]
    assert rq.objectives[0].current == 0
    assert catalog.can_query_complete(conn, rq) is False

    # 75 coins in the bag → objective complete → turn-in-ready.
    model.add("QuestItemPAL.Token", 0, 0, count=75)
    assert catalog.can_query_complete(conn, rq) is True
    assert rq.objectives[0].current == 75


def test_well_quest_0x04_from_log_sends_turn_in_dialog(monkeypatch, catalog):
    """Opening the 0-objective wishing-well quest from the quest log (0x04) must
    open the turn-in dialog — the reported 'opens then disappears' bug was the
    0x04 handler requiring non-empty objectives instead of CanQueryComplete."""
    h = quest_wire.quest_hash("world.town.quest.well.base")
    char = _make_char(level=10)
    conn, _model, packets = _bind_with_inventory(monkeypatch, catalog, char)
    catalog.handle_accept_confirmed(conn, 1, h)
    rq = catalog.get_player_state(conn).active_quests[0]
    packets.clear()

    catalog.handle_component_update(
        conn, 0x04, LEReader(rq.instance_id.to_bytes(4, "little")))

    assert conn.pending_turn_in_instance_id == rq.instance_id
    r = LEReader(packets[0])
    assert [r.read_byte(), r.read_byte()] == [0x07, 0x35]
    assert r.read_uint16() == conn.quest_manager_id
    assert r.read_byte() == 0x06                        # turn-in dialog submessage
    assert r.read_uint32() == rq.instance_id


def test_token_turn_in_consumes_kings_coins(monkeypatch, catalog):
    """Turning in a RemoveOnFinalize token quest consumes the coins it required."""
    token_id = "world.dungeon02.quest.token.MythJewelry"
    h = quest_wire.quest_hash(token_id)
    char = _make_char(level=20)
    conn, model, _ = _bind_with_inventory(
        monkeypatch, catalog, char,
        item_rows=[{"gc_class": "QuestItemPAL.Token", "x": 0, "y": 0, "count": 90}])
    catalog.handle_accept_confirmed(conn, 1, h)
    rq = catalog.get_player_state(conn).active_quests[0]
    assert catalog.can_query_complete(conn, rq) is True

    from drserver.net import inventory as inv
    catalog.handle_turn_in_confirmed(conn, rq.instance_id)

    # 75 of the 90 coins consumed; quest finalized.
    assert inv.count_items_by_gc(conn, "QuestItemPAL.Token") == 15
    assert token_id in catalog.get_player_state(conn).completed_quests
    assert all(q.instance_id != rq.instance_id
               for q in catalog.get_player_state(conn).active_quests)


# ── wishing-well reward redirect (the dangling IG → real class-appropriate IGs) ─

def _ww_tmpl(generator, count=1):
    """Minimal QuestTemplate stand-in for the reward roll (only these fields are
    read by _roll_reward_items)."""
    return types.SimpleNamespace(
        reward_item_generator=generator, num_reward_items=count,
        quest_id="world.town.quest.well.base_r")


# Fragments of the Mythic/prebuilt items the loot system blacklists (they crash
# the client on the inventory add — readType Invalid type tag, live 2026-07-01).
_WW_CRASH_FRAGMENTS = ("mythic", "prebuilt", "partialbuilt", "wishingwell",
                       "boss", "generated", "seasonal")


def test_wishing_well_rewards_are_client_safe_and_class_appropriate(catalog):
    """Every well reward MUST be client-serialisable (``is_client_droppable_item``)
    — the authentic Mythic prebuilts crash the client on the inventory add — and
    class-appropriate. Drawn from the loot roller's client-safe pool."""
    import random as _r
    from drserver.managers.merchants import is_client_droppable_item
    from drserver.managers.quests import _ww_item_ok
    from drserver.managers import loot_roller
    loot_roller.reset_pool()                              # fresh pool from this DB
    for cls in ("Fighter", "Ranger", "Mage", "Warlock"):
        conn = types.SimpleNamespace(conn_id=9, class_name=cls)
        for seed in range(10):
            out = catalog._roll_wishing_well_items(conn, level=25, count=1,
                                                   rng=_r.Random(seed))
            assert out, f"{cls} well produced no item"
            for it in out:
                assert is_client_droppable_item(it), f"{cls} got UNSAFE {it}"
                assert _ww_item_ok(it, cls), f"{cls} got wrong-class {it}"


def test_wishing_well_never_grants_a_crashing_mythic_prebuilt(catalog):
    """Regression for the live crash 2026-07-01: the reward must NEVER be a
    Mythic/prebuilt/wishingwell item (the loot blacklist's crash class)."""
    import random as _r
    conn = types.SimpleNamespace(conn_id=9, class_name="Fighter")
    for seed in range(24):
        for it in catalog._roll_wishing_well_items(conn, level=30, count=1,
                                                   rng=_r.Random(seed)):
            low = it.lower()
            assert not any(b in low for b in _WW_CRASH_FRAGMENTS), \
                f"granted a crashing item: {it}"


def test_one_time_wishing_well_alias_redirects_and_is_client_safe(catalog):
    """The 'First Time is Free' dangling 'OneTimeUseOnlyWishingWellIG' redirects
    through the same client-safe path (not just the bare 'WishingWellIG')."""
    from drserver.managers.merchants import is_client_droppable_item
    conn = types.SimpleNamespace(conn_id=9, class_name="Ranger")

    out = catalog._roll_reward_items(
        conn, _ww_tmpl("OneTimeUseOnlyWishingWellIG", count=1), level=15)
    assert len(out) == 1
    assert is_client_droppable_item(out[0])
