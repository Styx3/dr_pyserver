"""Quest reward grant — gold, King's Coins, XP buff, generator items.

Covers the two reusable inventory grant helpers (``give_gold`` / ``give_stacked_item``,
ports of C# GiveGold / GiveStackedItem) at the wire level, then the
``QuestManager._apply_rewards`` orchestration (port of C# ApplyQuestRewards) with
the grant helpers stubbed so the test stays DB-free and deterministic.
"""
import struct
import sys
import types

import pytest

sys.path.insert(0, "tests")
from _paths import copy_shipped_db, has_shipped_db  # noqa: E402


# ── inventory.give_gold ─────────────────────────────────────────────────────────

def _gold_conn(monkeypatch, container, gold=100):
    from drserver.net import inventory
    char = types.SimpleNamespace(gold=gold)
    saves = []
    monkeypatch.setattr(inventory, "character_repository", types.SimpleNamespace(
        get_character=lambda _id: char, save_character=lambda c: saves.append(c)))
    sent = []
    conn = types.SimpleNamespace(
        char_sql_id=1, login_name="Styx3", unit_container_id=container,
        hp_wire=68096, client_hp_wire=None,
        send_to_client=lambda b: sent.append(bytes(b)))
    return inventory, conn, char, saves, sent


def test_give_gold_credits_and_ships_currency(monkeypatch):
    inventory, conn, char, saves, sent = _gold_conn(monkeypatch, container=0x9002)

    credited = inventory.give_gold(conn, 250)

    assert credited == 250
    assert char.gold == 350 and saves               # credited + persisted
    p = sent[0]
    assert (p[0], p[1]) == (0x07, 0x35)             # BeginStream + ComponentUpdate
    assert struct.unpack_from("<H", p, 2)[0] == 0x9002
    assert p[4] == 0x20                             # AddCurrency
    assert struct.unpack_from("<I", p, 5)[0] == 250
    assert p[-1] == 0x06


def test_give_gold_credits_without_container_sends_nothing(monkeypatch):
    inventory, conn, char, saves, sent = _gold_conn(monkeypatch, container=0)

    assert inventory.give_gold(conn, 50) == 50
    assert char.gold == 150                          # DB credit still happens
    assert sent == []                                # but no live packet


def test_give_gold_ignores_nonpositive(monkeypatch):
    inventory, conn, char, saves, sent = _gold_conn(monkeypatch, container=0x1)

    assert inventory.give_gold(conn, 0) == 0
    assert inventory.give_gold(conn, -5) == 0
    assert char.gold == 100 and sent == []


# ── inventory.give_stacked_item ─────────────────────────────────────────────────

def _inv_conn(monkeypatch):
    from drserver.net import inventory, inventory_model
    char = types.SimpleNamespace(gold=0, inventory=[])
    saves = []
    monkeypatch.setattr(inventory, "character_repository", types.SimpleNamespace(
        get_character=lambda _id: char, save_character=lambda c: saves.append(c)))
    monkeypatch.setattr(inventory, "_get_item_size", lambda gc: (1, 1))
    sent = []
    conn = types.SimpleNamespace(
        char_sql_id=1, login_name="Styx3", unit_container_id=0x9002,
        hp_wire=68096, client_hp_wire=None,
        inv_model=inventory_model.InventoryModel(),
        send_to_client=lambda b: sent.append(bytes(b)))
    return inventory, conn, saves, sent


def test_give_stacked_item_adds_new_slot(monkeypatch):
    inventory, conn, saves, sent = _inv_conn(monkeypatch)

    given = inventory.give_stacked_item(conn, "QuestItemPAL.Token", 5, 100)

    assert given == 5
    items = list(conn.inv_model.items.values())
    assert len(items) == 1
    assert items[0].count == 5 and items[0].gc_class == "QuestItemPAL.Token"
    p = sent[0]
    assert p[0] == 0x07 and p[-1] == 0x06 and 0x1E in p   # ItemAdd
    assert saves                                          # persisted


def test_give_stacked_item_merges_existing_stack(monkeypatch):
    inventory, conn, saves, sent = _inv_conn(monkeypatch)
    inventory.give_stacked_item(conn, "QuestItemPAL.Token", 3, 100)
    sent.clear()

    given = inventory.give_stacked_item(conn, "QuestItemPAL.Token", 4, 100)

    assert given == 4
    items = list(conn.inv_model.items.values())
    assert len(items) == 1 and items[0].count == 7        # merged into one stack
    assert 0x22 in sent[0]                                # UpdateQuantity


def test_give_stacked_item_splits_across_stacks(monkeypatch):
    inventory, conn, saves, sent = _inv_conn(monkeypatch)

    given = inventory.give_stacked_item(conn, "QuestItemPAL.Token", 250, 100)

    assert given == 250
    assert sorted(it.count for it in conn.inv_model.items.values()) == [50, 100, 100]


def test_give_stacked_item_noop_without_inventory_model(monkeypatch):
    from drserver.net import inventory
    conn = types.SimpleNamespace(login_name="x", unit_container_id=0x1, inv_model=None)
    assert inventory.give_stacked_item(conn, "QuestItemPAL.Token", 1) == 0


# ── QuestManager._apply_rewards orchestration ──────────────────────────────────

def _stub_template(**over):
    from drserver.managers import quests as quests_mod
    base = dict(
        quest_id="world.dungeon00.quest.Q02_a1", name="A Quest", level=4,
        max_level=100, npcs=[], required_quest="", followup_quest="",
        repeatable=False, token_reward=2, cash_reward=1.0, on_accept_item="",
        grant_xp_buff=True, reward_item_generator="NormalPotionIG",
        num_reward_items=2, mod_to_add_on_complete="")
    base.update(over)
    return quests_mod.QuestTemplate(**base)


def _patch_grants(monkeypatch):
    from drserver.net import inventory as inv
    from drserver.managers import player_modifiers as pm
    from drserver.data import item_generator_resolver as igr
    calls = {"gold": [], "stacked": [], "buff": [], "resolved": []}
    monkeypatch.setattr(inv, "give_gold",
                        lambda conn, amt: calls["gold"].append(amt) or amt)
    monkeypatch.setattr(inv, "give_stacked_item",
                        lambda conn, gc, n=1, ms=100: calls["stacked"].append((gc, n, ms)) or n)
    monkeypatch.setattr(pm, "apply_buff",
                        lambda conn, gc, dur, level=0: calls["buff"].append((gc, dur)) or True)

    def fake_resolve(gc, level=1, count=1, rng=None):
        calls["resolved"].append((gc, level, count))
        return ["items.consumables.Consumable_MinorHealthPotion"] * count
    monkeypatch.setattr(igr, "resolve_generator_items", fake_resolve)
    return calls


def _reward_conn():
    msgs = []
    conn = types.SimpleNamespace(login_name="Styx3", char_sql_id=1,
                                 send_system_message=lambda m: msgs.append(m))
    return conn, msgs


def test_apply_rewards_grants_gold_coins_xp_and_items(monkeypatch):
    from drserver.managers import quests as quests_mod
    calls = _patch_grants(monkeypatch)
    qm = quests_mod.QuestManager(types.SimpleNamespace())
    conn, msgs = _reward_conn()

    qm._apply_rewards(conn, _stub_template())

    assert calls["gold"] == [max(1, round(4 * 250 * 1.0))]          # 1000 gold
    assert ("QuestItemPAL.Token", 2, 100) in calls["stacked"]       # King's Coins
    assert ("quests.base.QuestXPBonus", 0) in calls["buff"]         # EXPMOD buff
    # 2 reward items, each given individually (count 1, max_stack 1) as in C#.
    item_grants = [c for c in calls["stacked"]
                   if c[0] == "items.consumables.Consumable_MinorHealthPotion"]
    assert item_grants == [("items.consumables.Consumable_MinorHealthPotion", 1, 1)] * 2
    assert calls["resolved"] == [("NormalPotionIG", 4, 2)]
    assert msgs and "King's Coin" in msgs[0] and "XP bonus" in msgs[0]


def test_apply_rewards_minimal_quest_only_gold(monkeypatch):
    from drserver.managers import quests as quests_mod
    calls = _patch_grants(monkeypatch)
    qm = quests_mod.QuestManager(types.SimpleNamespace())
    conn, msgs = _reward_conn()

    qm._apply_rewards(conn, _stub_template(
        token_reward=0, grant_xp_buff=False, reward_item_generator="",
        num_reward_items=0, cash_reward=0.5))

    assert calls["gold"] == [max(1, round(4 * 250 * 0.5))]          # 500 gold
    assert calls["stacked"] == [] and calls["buff"] == []
    assert calls["resolved"] == []
    assert msgs and "King's Coin" not in msgs[0]


def test_apply_rewards_mod_on_complete(monkeypatch):
    from drserver.managers import quests as quests_mod
    calls = _patch_grants(monkeypatch)
    qm = quests_mod.QuestManager(types.SimpleNamespace())
    conn, _ = _reward_conn()

    qm._apply_rewards(conn, _stub_template(
        grant_xp_buff=False, token_reward=0, reward_item_generator="",
        num_reward_items=0,
        mod_to_add_on_complete="world.dungeon16.data.base.Mods.PoisonResistance"))

    assert ("world.dungeon16.data.base.Mods.PoisonResistance", 0) in calls["buff"]


def test_apply_rewards_wishing_well_drops_on_ground_not_bag(monkeypatch):
    """A wishing-well reward spits its prize onto the floor at the player's feet —
    ALWAYS on the ground, NEVER into the bag, regardless of inventory space (live
    2026-07-01: "wishing well should drop items on ground regardless")."""
    from drserver.managers import quests as quests_mod
    from drserver.managers import loot
    calls = _patch_grants(monkeypatch)
    drops = []
    monkeypatch.setattr(loot, "drop_item_near_player",
                        lambda server, conn, gc, **kw: drops.append(gc) or 0xC001)
    # The well rolls come from _roll_wishing_well_items — stub it to avoid DB.
    monkeypatch.setattr(quests_mod.QuestManager, "_roll_wishing_well_items",
                        lambda self, conn, level, count, rng=None:
                            ["1haxe1pal.1haxe1-4"] * count)
    qm = quests_mod.QuestManager(types.SimpleNamespace())
    conn, msgs = _reward_conn()

    qm._apply_rewards(conn, _stub_template(
        reward_item_generator="WishingWellIG", num_reward_items=2,
        token_reward=0, grant_xp_buff=False))

    assert drops == ["1haxe1pal.1haxe1-4"] * 2                  # both on the ground
    # NEVER routed through the bag grant.
    assert [c for c in calls["stacked"] if c[0] == "1haxe1pal.1haxe1-4"] == []
    assert msgs and "on the ground" in msgs[0]


def test_apply_rewards_full_bag_drops_reward_on_ground(monkeypatch):
    """A non-well quest reward goes into the bag, but a FULL bag falls back to a
    ground drop so the reward is never silently lost (default DR behavior)."""
    from drserver.managers import quests as quests_mod
    from drserver.managers import loot
    from drserver.net import inventory as inv
    calls = _patch_grants(monkeypatch)
    # Reward grants (max_stack 1) report a full bag (0 given); coins (ms 100) still fit.
    monkeypatch.setattr(inv, "give_stacked_item",
                        lambda conn, gc, n=1, ms=100:
                            calls["stacked"].append((gc, n, ms)) or (0 if ms == 1 else n))
    drops = []
    monkeypatch.setattr(loot, "drop_item_near_player",
                        lambda server, conn, gc, **kw: drops.append(gc) or 0xC001)
    qm = quests_mod.QuestManager(types.SimpleNamespace())
    conn, msgs = _reward_conn()

    qm._apply_rewards(conn, _stub_template(token_reward=0, grant_xp_buff=False))

    assert drops == ["items.consumables.Consumable_MinorHealthPotion"] * 2
    assert msgs and "on the ground" in msgs[0]


def test_apply_rewards_none_template_is_noop(monkeypatch):
    from drserver.managers import quests as quests_mod
    calls = _patch_grants(monkeypatch)
    qm = quests_mod.QuestManager(types.SimpleNamespace())
    conn, msgs = _reward_conn()

    qm._apply_rewards(conn, None)

    assert calls == {"gold": [], "stacked": [], "buff": [], "resolved": []}
    assert msgs == []


# ── end-to-end: real catalog + resolver + grant path (no grant mocks) ───────────

def test_turn_in_end_to_end_grants_coin_buff_and_item(monkeypatch):
    """Turning in a complete reward quest actually credits gold, drops the King's
    Coin + a generator-rolled reward item into the inventory model, and ships the
    QuestXPBonus buff packet — exercising the real resolver and grant wire."""
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    from drserver.managers import quests as quests_mod
    from drserver.net import inventory as inventory_mod
    from drserver.net import inventory_model

    qm = quests_mod.QuestManager(types.SimpleNamespace())
    qm.load()
    qm._states.clear()
    # A dungeon-00 starter quest that grants gold + a King's Coin + the XP buff +
    # a resolvable reward generator (FreePotionIG).
    tmpl = qm._templates["world.dungeon00.quest.Q02_a1"]
    assert tmpl.token_reward > 0 and tmpl.grant_xp_buff and tmpl.reward_item_generator

    char = types.SimpleNamespace(
        level=tmpl.level, gold=100, experience=0,
        active_quests=[], completed_quests=[], inventory=[])
    fake_repo = types.SimpleNamespace(
        get_character=lambda _id: char, save_character=lambda c: None)
    monkeypatch.setattr(quests_mod, "character_repository", fake_repo)
    monkeypatch.setattr(inventory_mod, "character_repository", fake_repo)

    packets = []
    conn = types.SimpleNamespace(
        conn_id=11, char_sql_id=1, login_name="Styx3",
        quest_manager_id=0x0210, modifiers_id=0x0220, unit_container_id=0x0230,
        hp_wire=68096, client_hp_wire=None, next_quest_instance_id=1,
        current_dialog_npc_id=None, current_zone_gc_type="world.dungeon00",
        pending_quest_hash=0, pending_quest_npc_entity_id=0,
        pending_turn_in_instance_id=0, viewing_quest_instance_id=0,
        inv_model=inventory_model.InventoryModel(),
        send_to_client=lambda b: packets.append(bytes(b)),
        send_system_message=lambda m: None)

    qm.handle_accept_confirmed(conn, 1, quests_mod.quest_wire.quest_hash(tmpl.quest_id))
    rq = qm.get_player_state(conn).active_quests[0]
    for o in rq.objectives:
        o.current = o.required

    qm.handle_turn_in_confirmed(conn, rq.instance_id)

    # Gold credited.
    assert char.gold > 100
    # King's Coin landed in the inventory model.
    coins = [it for it in conn.inv_model.items.values()
             if it.gc_class.lower() == "questitempal.token"]
    assert coins and coins[0].count == tmpl.token_reward
    # A reward item (potion) was rolled in and added beyond the coin.
    non_coin = [it for it in conn.inv_model.items.values()
                if it.gc_class.lower() != "questitempal.token"]
    assert non_coin, "reward generator produced no inventory item"
    # The XP-bonus modifier packet was shipped.
    assert any(b"quests.base.QuestXPBonus" in p for p in packets)


def test_wishing_well_turn_in_completes_without_bogus_item(monkeypatch):
    """The wishing-well "The First Time is Free" (0-objective, AutoAcceptOnQuery,
    reward generator OneTimeUseOnlyWishingWellIG with base LegendIG) must turn in
    cleanly: the quest moves to completed and ANY bagged reward is a REAL item —
    never the generator gc_type itself (the bogus-item add that broke this turn-in).
    """
    if not has_shipped_db():
        pytest.skip("shipped content DB not present")
    from drserver.db import game_database
    game_database.initialize(copy_shipped_db())
    from drserver.managers import quests as quests_mod
    from drserver.net import inventory as inventory_mod
    from drserver.net import inventory_model
    from drserver.data import item_generator_resolver

    qm = quests_mod.QuestManager(types.SimpleNamespace())
    qm.load()
    qm._states.clear()
    tmpl = qm._templates["world.town.quest.well.base"]
    assert tmpl.num_reward_items > 0 and tmpl.reward_item_generator
    # The reward generator is NOT a real item — it must never be given directly.
    assert item_generator_resolver.is_real_item(tmpl.reward_item_generator) is False

    char = types.SimpleNamespace(
        level=5, gold=0, experience=0,
        active_quests=[], completed_quests=[], inventory=[])
    fake_repo = types.SimpleNamespace(
        get_character=lambda _id: char, save_character=lambda c: None)
    monkeypatch.setattr(quests_mod, "character_repository", fake_repo)
    monkeypatch.setattr(inventory_mod, "character_repository", fake_repo)

    conn = types.SimpleNamespace(
        conn_id=12, char_sql_id=1, login_name="Styx3",
        quest_manager_id=0x0210, modifiers_id=0x0220, unit_container_id=0x0230,
        hp_wire=68096, client_hp_wire=None, next_quest_instance_id=1,
        current_dialog_npc_id="world.town.npc.Well", current_zone_gc_type="world.town",
        current_zone_name="town", pending_quest_hash=0, pending_quest_npc_entity_id=0,
        pending_turn_in_instance_id=0, viewing_quest_instance_id=0,
        inv_model=inventory_model.InventoryModel(),
        send_to_client=lambda b: None, send_system_message=lambda m: None)

    qm.handle_accept_confirmed(conn, 1, quests_mod.quest_wire.quest_hash(tmpl.quest_id))
    rq = qm.get_player_state(conn).active_quests[0]
    # 0 objectives → query-complete → turn-in confirm.
    qm.handle_turn_in_confirmed(conn, rq.instance_id)

    state = qm.get_player_state(conn)
    assert rq not in state.active_quests
    assert tmpl.quest_id in state.completed_quests
    # Whatever (if anything) was bagged must be a real item, never the generator.
    for it in conn.inv_model.items.values():
        assert it.gc_class.lower() != tmpl.reward_item_generator.lower()
        assert item_generator_resolver.is_real_item(it.gc_class), \
            f"bagged a non-item reward: {it.gc_class}"
