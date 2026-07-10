"""Quest manager — wire-level quest lifecycle ported from C# QuestManager.cs.

Replaces the earlier chat-only stub (which read columns that do not exist in the
shipped schema and so loaded zero quests). This version:

* loads the quest catalogue from the real ``quests`` + ``quest_objective_templates``
  tables, enriching kill/item objective targets from each quest's ``raw_json``
  (the DB ``target`` column is empty for ~98% of kill objectives — the monster
  types live in the GC ``MonsterType``/``MonsterType2``… properties);
* identifies quests on the wire by the DJB2 hash of their gc_type (matches the
  client + C# ``ComputeDJB2Hash``);
* tracks per-session quest state with client-facing **instance ids** and drives
  the QuestManager ComponentUpdate packets (add / progress / finalize / remove /
  available-markers / accept+turn-in dialogs — see ``quest_wire``);
* tracks kill/item objective progress and pushes progress packets;
* persists active + completed quests through the character repository.

The NPC-dialog accept/turn-in handshake (component-update submessages
0x01-empty/0x02/0x03/0x04/0x05/0x06 on the QuestManager component) is dispatched
here from ``movement._component_update``. The live client's Accept / Complete
button is the **empty-payload 0x01** (C# UGS:12012); 0x05 and the double-0x06
forms are kept as alternate confirms. ``@quest`` chat commands remain as a
fallback.

Reward grant on turn-in is a full port of C# ``ApplyQuestRewards``: gold =
round(level × QuestGoldPerLevel(250) × CashReward) credited **and** shipped live
(``0x20`` AddCurrency); King's-Coin token rewards as the stacked
``QuestItemPAL.Token`` item; the ``QuestXPBonus`` EXPMOD buff when GrantXPBuff is
set (never direct XP — the client self-levels and a direct grant double-counts);
reward items rolled from the quest's RewardItemGenerator
(``data.item_generator_resolver``); and a permanent ModToAddOnComplete modifier.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..db import character_repository
from . import quest_wire

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection
    from ..util.byte_io import LEReader

# C# ApplyQuestRewards knobs (GlobalKnobs.gc defaults).
_QUEST_GOLD_PER_LEVEL = 250.0
_DEFAULT_CASH_REWARD = 0.5

# King's Coins are the stacked "QuestItemPAL.Token" item the Token Masters
# collect; GrantXPBuff sends the QuestXPBonus EXPMOD modifier (C# GameServer.Types).
_KINGS_COIN_ITEM = "QuestItemPAL.Token"
_KINGS_COIN_MAX_STACK = 100
_QUEST_XP_BONUS_MODIFIER = "quests.base.QuestXPBonus"

# ── Wishing-well reward ──
# The well quests name `WishingWellIG` (repeatable) / `OneTimeUseOnlyWishingWellIG`
# (the dangling "First Time is Free" LegendIG). The authentic WishingWell IGs
# resolve ONLY to Mythic/prebuilt items whose bespoke wire body the server can't
# serialise — they CRASH the client on the inventory add ("GCClassRegistry::
# readType Invalid type tag", the loot-drop crash class; every one is in
# `merchants.is_client_droppable_item`'s blacklist, live-confirmed 2026-07-01).
# So the reward instead draws from the loot roller's CLIENT-SAFE droppable pool
# (dash-suffix PAL weapons/armor), class-filtered + near the player's level, and
# every item is hard-guarded by that predicate. (Serialising the authentic Mythic
# prebuilts is a separate future task.)
_WISHING_WELL_ALIASES = ("wishingwellig", "onetimeuseonlywishingwellig")
_WW_CASTER_CLASSES = ("mage", "warlock")
_WW_LEVEL_BAND = 15                             # reward within ±band of player level
_WW_ARMOR_FRAGMENTS = ("body", "armor", "boot", "glove", "helm", "shoulder",
                       "shield", "chest", "pauldron", "buckler", "pants", "legs",
                       "bracer", "robe", "cap", "mantle")
_WW_RANGED_FRAGMENTS = ("gun", "cannon", "crossbow", "bow", "rifle", "pistol")
_WW_CASTER_WEP_FRAGMENTS = ("staff", "wand", "scepter", "orb")
_WW_MELEE_FRAGMENTS = ("axe", "sword", "mace", "pick", "hammer", "dagger", "spear",
                       "club", "katana", "polearm")


def _ww_item_ok(gc_type: str, class_name: str) -> bool:
    """Class-appropriateness for a client-safe pool item. Armor: casters take
    light/cloth (reject plate/leather), martial take plate/leather (reject
    light/cloth/mage). Weapons: casters→staff/wand, rangers→ranged, fighters→melee.
    Non-weapon/non-armor (jewelry, etc.) passes for everyone (the gamble)."""
    low = gc_type.lower()
    caster = (class_name or "").lower() in _WW_CASTER_CLASSES
    if any(a in low for a in _WW_ARMOR_FRAGMENTS):
        if caster:
            # Casters wear only light/cloth. The client-safe pool is old-gen
            # plate/chain/scale/leather (no cloth), so casters generally fall
            # through to a staff/wand weapon instead of heavy armor they can't use.
            return any(lgt in low for lgt in ("cloth", "light", "silk", "robe",
                                              "mantle", "sandal"))
        return not ("light" in low or "cloth" in low or "mage" in low)
    is_ranged = any(w in low for w in _WW_RANGED_FRAGMENTS)
    is_caster_wep = any(w in low for w in _WW_CASTER_WEP_FRAGMENTS)
    is_melee = any(w in low for w in _WW_MELEE_FRAGMENTS)
    if not (is_ranged or is_caster_wep or is_melee):
        return True                             # not a weapon → allow (jewelry/etc.)
    if caster:
        return is_caster_wep
    if (class_name or "").lower() == "ranger":
        return is_ranged
    return is_melee                             # fighter / default → melee


# ── Catalogue (static quest definitions) ─────────────────────────────────────────

@dataclass
class QuestObjectiveTemplate:
    objective_name: str
    type: str                      # "kill" | "item" | "goto" | "activate"
    targets: List[str]             # monster/item gc types (may be several)
    label: str
    required: int
    remove_on_finalize: bool = False  # ItemObjective: consume the items on turn-in


@dataclass
class QuestTemplate:
    quest_id: str
    name: str
    level: int                     # min_level
    max_level: int
    npcs: List[str]                # quest-giver gc types (npc, npc2)
    required_quest: str
    followup_quest: str
    repeatable: bool
    token_reward: int              # King's Coins (QuestItemPAL.Token)
    cash_reward: float             # gold multiplier
    on_accept_item: str
    grant_xp_buff: bool = False    # send the QuestXPBonus EXPMOD modifier
    reward_item_generator: str = ""
    num_reward_items: int = 0
    mod_to_add_on_complete: str = ""
    auto_accept_on_query: bool = False  # 0-objective → turn-in-ready on query (C# CanQueryComplete)
    temporary: bool = False
    objectives: List[QuestObjectiveTemplate] = field(default_factory=list)


# ── Runtime (per-session) quest state ────────────────────────────────────────────

@dataclass
class RuntimeObjective:
    objective_name: str = ""
    type: str = "kill"
    targets: List[str] = field(default_factory=list)
    label: str = ""
    required: int = 1
    current: int = 0
    remove_on_finalize: bool = False

    @property
    def is_complete(self) -> bool:
        return self.current >= self.required


@dataclass
class RuntimeQuest:
    quest_id: str = ""
    quest_giver_id: str = ""
    instance_id: int = 0
    objectives: List[RuntimeObjective] = field(default_factory=list)


@dataclass
class PlayerQuestState:
    level: int = 1
    active_quests: List[RuntimeQuest] = field(default_factory=list)
    completed_quests: List[str] = field(default_factory=list)


class QuestManager:
    """Global quest catalogue + per-session runtime state."""

    def __init__(self, server: "GameServer"):
        self._server = server
        self._templates: Dict[str, QuestTemplate] = {}    # quest_id -> template
        self._by_hash: Dict[int, QuestTemplate] = {}       # DJB2(quest_id) -> template
        self._states: Dict[int, PlayerQuestState] = {}     # conn_id -> state
        self._loaded = False

    # ── catalogue loading ────────────────────────────────────────────────────────

    def load(self) -> None:
        if self._loaded:
            return
        try:
            self._load_templates()
            self._loaded = True
            log.info(f"[QuestManager] loaded {len(self._templates)} quests "
                     f"({len(self._by_hash)} hashed)")
        except Exception as ex:  # noqa: BLE001
            log.error(f"[QuestManager] load error: {ex}")

    def _load_templates(self) -> None:
        self._templates.clear()
        self._by_hash.clear()

        rows = db.execute_reader("SELECT * FROM quests").fetchall()
        for row in rows:
            qid = db.get_string(row, "gc_type")
            if not qid:
                continue
            npcs = [n for n in (db.get_string(row, "npc"), db.get_string(row, "npc2"),
                                db.get_string(row, "npc3")) if n]
            self._templates[qid] = QuestTemplate(
                quest_id=qid,
                name=db.get_string(row, "name"),
                level=db.get_int(row, "min_level", 1),
                max_level=db.get_int(row, "max_level", 100) or 100,
                npcs=npcs,
                required_quest=db.get_string(row, "required_quest"),
                followup_quest=db.get_string(row, "followup_quest"),
                repeatable=bool(db.get_int(row, "repeatable")),
                token_reward=db.get_int(row, "token_reward"),
                cash_reward=db.get_float(row, "cash_reward", _DEFAULT_CASH_REWARD),
                on_accept_item=db.get_string(row, "on_accept_item_generator"),
                grant_xp_buff=bool(db.get_int(row, "grant_xp_buff")),
                reward_item_generator=db.get_string(row, "reward_item_generator"),
                num_reward_items=db.get_int(row, "num_reward_items"),
                mod_to_add_on_complete=db.get_string(row, "mod_to_add_on_complete"),
                auto_accept_on_query=bool(db.get_int(row, "auto_accept_on_query")),
                temporary=bool(db.get_int(row, "temporary")),
                # Objectives are parsed from the quest's authoritative raw_json
                # (ItemObjective / KillObjective / GoToObjective / ActivateObjective).
                # The legacy `quest_objective_templates` table is stale — it omits
                # ~520 quests (every token-trade + many item quests) and is not
                # written by the current importer; raw_json is the importer's own
                # ground truth (objective_count matches it 1:1).
                objectives=_extract_objectives(db.get_string(row, "raw_json")),
            )

        for qid, tmpl in self._templates.items():
            self._by_hash[quest_wire.quest_hash(qid)] = tmpl

    # ── per-session state ────────────────────────────────────────────────────────

    def initialize_player(self, conn: "RRConnection") -> PlayerQuestState:
        """Build runtime quest state from the saved character (active + completed),
        assigning fresh session instance ids. Idempotent per connection."""
        saved = character_repository.get_character(conn.char_sql_id)
        active: List[RuntimeQuest] = []
        completed: List[str] = []
        level = 1
        if saved is not None:
            level = saved.level or 1
            completed = list(saved.completed_quests or [])
            for sq in (saved.active_quests or []):
                rq = self._runtime_from_saved(sq)
                rq.instance_id = conn.next_quest_instance_id
                conn.next_quest_instance_id += 1
                active.append(rq)
        state = PlayerQuestState(level=level, active_quests=active, completed_quests=completed)
        self._states[conn.conn_id] = state
        log.info(f"[QuestManager] init '{conn.login_name}': {len(active)} active, "
                 f"{len(completed)} completed")
        return state

    def ensure_player_state(self, conn: "RRConnection") -> PlayerQuestState:
        state = self._states.get(conn.conn_id)
        if state is None:
            state = self.initialize_player(conn)
        return state

    def remove_player(self, conn: "RRConnection") -> None:
        self._states.pop(conn.conn_id, None)

    def get_player_state(self, conn: "RRConnection") -> Optional[PlayerQuestState]:
        return self._states.get(conn.conn_id)

    def get_active_runtime(self, conn: "RRConnection") -> List[RuntimeQuest]:
        """Active quests for the spawn QM-component block."""
        return self.ensure_player_state(conn).active_quests

    def get_quest_by_instance(self, conn: "RRConnection",
                              instance_id: int) -> Optional[RuntimeQuest]:
        state = self.get_player_state(conn)
        if state is None:
            return None
        return next((q for q in state.active_quests if q.instance_id == instance_id), None)

    def _runtime_from_saved(self, sq) -> RuntimeQuest:  # noqa: ANN001 — SavedQuest
        """Rebuild a runtime quest from a saved one, restoring full objective
        targets from the template (the DB stores only the first target)."""
        tmpl = self._templates.get(sq.quest_id)
        objectives: List[RuntimeObjective] = []
        saved_current = {o.objective_name: o.current for o in (sq.objectives or [])}
        if tmpl is not None and tmpl.objectives:
            for ot in tmpl.objectives:
                objectives.append(RuntimeObjective(
                    objective_name=ot.objective_name, type=ot.type,
                    targets=list(ot.targets), label=ot.label, required=ot.required,
                    current=saved_current.get(ot.objective_name, 0),
                    remove_on_finalize=ot.remove_on_finalize))
        else:
            for o in (sq.objectives or []):
                objectives.append(RuntimeObjective(
                    objective_name=o.objective_name, type=o.type,
                    targets=[o.target] if o.target else [], label=o.label,
                    required=o.required, current=o.current))
        return RuntimeQuest(quest_id=sq.quest_id, quest_giver_id=sq.quest_giver_id or "",
                            objectives=objectives)

    # ── accept / query / abandon / turn-in ────────────────────────────────────────

    def accept_quest(self, conn: "RRConnection", quest_id: str,
                     npc_id: str) -> Optional[RuntimeQuest]:
        """Validate and add a quest to the active list — C# ``AcceptQuest``."""
        state = self.ensure_player_state(conn)
        tmpl = self._templates.get(quest_id)
        if tmpl is None:
            return None
        if any(q.quest_id.lower() == quest_id.lower() for q in state.active_quests):
            return None
        already_completed = any(c.lower() == quest_id.lower() for c in state.completed_quests)
        if already_completed and not tmpl.repeatable:
            return None
        if already_completed and tmpl.repeatable:
            state.completed_quests = [c for c in state.completed_quests
                                      if c.lower() != quest_id.lower()]

        rq = RuntimeQuest(quest_id=quest_id, quest_giver_id=npc_id, objectives=[
            RuntimeObjective(objective_name=o.objective_name, type=o.type,
                             targets=list(o.targets), label=o.label,
                             required=o.required, current=0,
                             remove_on_finalize=o.remove_on_finalize)
            for o in tmpl.objectives])
        state.active_quests.append(rq)
        return rq

    # ── completion checks (inventory-aware) ──────────────────────────────────────

    def _sync_item_objectives(self, conn: "RRConnection",
                              quest: RuntimeQuest) -> bool:
        """Set each item objective's ``current`` to the live inventory count of its
        target item (capped at ``required``). Item objectives ("have N King's
        Coins") are satisfied by *holding* the items, so the bag is authoritative —
        not a pickup counter (which misses items already owned, e.g. coins earned
        from other quests). Returns True if any objective changed."""
        try:
            from ..net import inventory as inv
        except Exception:  # noqa: BLE001
            return False
        changed = False
        for obj in quest.objectives:
            if obj.type != "item" or not obj.targets:
                continue
            have = sum(inv.count_items_by_gc(conn, t) for t in obj.targets)
            new_current = min(obj.required, have)
            if new_current != obj.current:
                obj.current = new_current
                changed = True
        return changed

    def can_query_complete(self, conn: "RRConnection",
                           quest: RuntimeQuest) -> bool:
        """Whether querying this active quest should open the turn-in dialog.

        With objectives: all complete (item objectives first refreshed from the
        inventory). Without (modelled) objectives: there is nothing left to do,
        so the quest is turn-in-ready as soon as it is active — e.g. the
        "Speak with each Skill Trainer" chain quests (0 objectives, no
        ``AutoAcceptOnQuery``).

        ``AutoAcceptOnQuery`` governs the *accept* step, NOT completion. Gating
        0-objective completion on it (the old behaviour) silently dropped the
        client's ``0x04`` turn-in query for every plain 0-objective quest — the
        server sent no turn-in dialog, so the NPC dialog just closed. Live-
        confirmed 2026-06-21 (x64dbg: "Skill Trainers" re-fired ``op4`` with no
        ``recv06`` response). See bible.md §13."""
        self._sync_item_objectives(conn, quest)
        if quest.objectives:
            return all(o.is_complete for o in quest.objectives)
        return True

    def handle_accept_confirmed(self, conn: "RRConnection", npc_entity_id: int,
                                quest_hash: int) -> None:
        """Resolve the hash, accept, assign an instance id, and add to journal —
        C# ``HandleAcceptConfirmed``."""
        tmpl = self._by_hash.get(quest_hash)
        if tmpl is None:
            log.debug(f"[QUEST-ACCEPT] unknown hash 0x{quest_hash:08X}")
            return
        npc_id = conn.current_dialog_npc_id or f"npc_{npc_entity_id}"
        rq = self.accept_quest(conn, tmpl.quest_id, npc_id)
        if rq is None:
            return
        rq.instance_id = conn.next_quest_instance_id
        conn.next_quest_instance_id += 1

        # onAcceptItem auto-completes any matching item objective.
        if tmpl.on_accept_item:
            for obj in rq.objectives:
                if obj.type == "item" and any(
                        tmpl.on_accept_item.lower() == t.lower() for t in obj.targets):
                    obj.current = obj.required

        # Item objectives are "have N in inventory" — seed them from the bag so an
        # AutoAcceptOnQuery token-trade quest accepted while already holding the
        # coins shows turn-in-ready immediately (e.g. King's Coin x75).
        self._sync_item_objectives(conn, rq)

        quest_wire.send_add_packet(conn, rq)
        log.info(f"[QUEST-ACCEPT] '{conn.login_name}' accepted {tmpl.quest_id} "
                 f"inst={rq.instance_id}")
        self.send_available_quest_update(conn)
        self.save_player_quests(conn)

    def handle_query(self, conn: "RRConnection", npc_entity_id: int,
                     quest_hash: int) -> None:
        """Show the accept- or turn-in dialog for a quest — C# ``SendQueryResponse``."""
        tmpl = self._by_hash.get(quest_hash)
        if tmpl is None:
            return
        active = next((q for q in self.ensure_player_state(conn).active_quests
                       if q.quest_id.lower() == tmpl.quest_id.lower()), None)
        if active is not None:
            if self.can_query_complete(conn, active):
                quest_wire.send_turn_in_dialog(conn, active.instance_id)
            # else: already active, objectives incomplete → suppress the dialog.
            return
        quest_wire.send_accept_dialog(conn, quest_hash)

    def handle_abandon(self, conn: "RRConnection", instance_id: int) -> None:
        """Drop an active quest — C# ``Handle0x03`` / the abandon dispatch."""
        state = self.get_player_state(conn)
        if state is not None:
            before = len(state.active_quests)
            state.active_quests = [q for q in state.active_quests
                                   if q.instance_id != instance_id]
            if len(state.active_quests) != before:
                log.info(f"[QUEST-ABANDON] '{conn.login_name}' dropped inst={instance_id}")
        quest_wire.send_remove_packet(conn, instance_id)
        self.save_player_quests(conn)

    def handle_turn_in_confirmed(self, conn: "RRConnection", instance_id: int) -> None:
        """Complete a quest: grant rewards, move to completed, finalize — C#
        ``HandleTurnInConfirmed``."""
        state = self.get_player_state(conn)
        quest = self.get_quest_by_instance(conn, instance_id)
        if state is None or quest is None:
            return
        tmpl = self._templates.get(quest.quest_id)

        # Consume RemoveOnFinalize item objectives (King's Coins, collect items)
        # BEFORE removing the quest — C# RemoveQuestItemsFromInventory. Wrapped so
        # an inventory hiccup can never abort the turn-in (the quest is finalized
        # below regardless).
        try:
            self._consume_finalize_items(conn, quest)
        except Exception as ex:  # noqa: BLE001
            log.error(f"[QUEST-ITEM-REMOVE] failed for {quest.quest_id}: {ex}")

        state.active_quests.remove(quest)
        if quest.quest_id not in state.completed_quests:
            state.completed_quests.append(quest.quest_id)

        quest_wire.send_finalize_packet(conn, instance_id)
        quest_wire.send_remove_packet(conn, instance_id)
        # Reward granting must never break the turn-in itself (the quest is already
        # finalized above): a content gap / resolver miss is logged, not fatal.
        try:
            self._apply_rewards(conn, tmpl)
        except Exception as ex:  # noqa: BLE001
            log.error(f"[QUEST-REWARDS] grant failed for {quest.quest_id}: {ex}")
        self.send_available_quest_update(conn)
        self.save_player_quests(conn)
        log.info(f"[QUEST-TURNIN] '{conn.login_name}' completed {quest.quest_id}")

    def handle_npc_teleport(self, conn: "RRConnection", npc_entity_id: int) -> None:
        """Resolve an NPC's on-NPC ``NPCTeleporter`` destination and change zone —
        the client's "Teleport to X" dialog option (QM inbound op 0x08). No-ops
        when the entity is unknown or carries no teleporter (bible §13.5 #8)."""
        from .npcs import npc_manager
        npc = npc_manager.find_by_entity_id(npc_entity_id)
        if npc is None:
            log.warn(f"[QUEST-TELEPORT] eid {npc_entity_id} not a known NPC "
                        f"(no entity->NPC mapping); ignoring op 0x08")
            return
        dest = npc_manager.teleporter_for(npc.gc_type)
        if dest is None:
            log.warn(f"[QUEST-TELEPORT] NPC {npc.gc_type} (eid {npc_entity_id}) "
                        f"has no teleporter — is npc_teleporters populated? "
                        f"(run scripts/import_world_npcs.py)")
            return
        zone, spawn_point = dest
        log.info(f"[QUEST-TELEPORT] '{conn.login_name}' via {npc.gc_type} "
                 f"(eid {npc_entity_id}) -> {zone}/{spawn_point}")
        self._server.change_zone(conn, zone, spawn_point)

    def _consume_finalize_items(self, conn: "RRConnection",
                                quest: RuntimeQuest) -> None:
        """Remove the items backing each RemoveOnFinalize item objective from the
        player's inventory on turn-in — port of C#
        ``RemoveQuestItemsFromInventory`` (consumes King's Coins, collected items)."""
        from ..net import inventory as inv
        for obj in quest.objectives:
            if obj.type != "item" or not obj.remove_on_finalize:
                continue
            need = max(1, obj.required)
            for target in obj.targets:
                if need <= 0:
                    break
                need -= inv.remove_items_by_gc(conn, target, need)

    def _apply_rewards(self, conn: "RRConnection", tmpl: Optional[QuestTemplate]) -> None:
        """Grant a completed quest's rewards — port of C# ``ApplyQuestRewards``:
        gold (credited + live AddCurrency), King's Coins, the QuestXPBonus EXPMOD
        buff, generator-rolled reward items, and a permanent on-complete modifier.
        Each grant degrades gracefully when its component id / inventory model is
        not yet known (returns early inside the helper), so the chat summary and
        gold credit always land."""
        if tmpl is None:
            return
        from ..net import inventory as inv
        from . import player_modifiers

        cash = tmpl.cash_reward if tmpl.cash_reward > 0 else _DEFAULT_CASH_REWARD
        level = tmpl.level if tmpl.level > 0 else 1
        gold = max(1, round(level * _QUEST_GOLD_PER_LEVEL * cash))
        inv.give_gold(conn, gold)

        if tmpl.token_reward > 0:
            inv.give_stacked_item(conn, _KINGS_COIN_ITEM, tmpl.token_reward,
                                  _KINGS_COIN_MAX_STACK)

        if tmpl.grant_xp_buff:
            # The client applies the EXPMOD locally; this never grants XP directly
            # (the client self-levels — a direct grant double-counts, bible §6a).
            player_modifiers.apply_buff(conn, _QUEST_XP_BONUS_MODIFIER, 0)

        reward_items = self._roll_reward_items(conn, tmpl, level)
        is_ww = self._is_wishing_well(tmpl)
        bagged = 0
        dropped = 0
        for item_gc in reward_items:
            # The wishing well spits its prize onto the floor at the player's
            # feet — ALWAYS on the ground, regardless of bag space (live
            # 2026-07-01). Other quest rewards go into the bag, falling back to
            # the ground only when it's full (default DR behavior — the reward is
            # never silently lost).
            if is_ww:
                dropped += 1 if self._drop_reward(conn, item_gc, level) else 0
            elif inv.give_stacked_item(conn, item_gc, 1, 1) > 0:
                bagged += 1
            else:
                dropped += 1 if self._drop_reward(conn, item_gc, level) else 0

        if tmpl.mod_to_add_on_complete:
            player_modifiers.apply_buff(conn, tmpl.mod_to_add_on_complete, 0)

        total_items = bagged + dropped
        msg = f"Quest complete: {tmpl.name or tmpl.quest_id}! +{gold} gold"
        if tmpl.token_reward:
            msg += f", +{tmpl.token_reward} King's Coin(s)"
        if total_items:
            if dropped and not bagged:
                msg += f", +{total_items} item(s) on the ground"
            elif dropped:
                msg += f", +{total_items} item(s) ({dropped} on the ground)"
            else:
                msg += f", +{total_items} item(s)"
        if tmpl.grant_xp_buff:
            msg += ", +XP bonus"
        try:
            conn.send_system_message(msg)
        except Exception:  # noqa: BLE001
            pass
        log.info(f"[QUEST-REWARDS] '{conn.login_name}' {tmpl.quest_id} (L{level}) -> "
                 f"{gold} gold, {tmpl.token_reward} coin(s), {bagged} bagged + "
                 f"{dropped} dropped item(s){', xpBuff' if tmpl.grant_xp_buff else ''}"
                 f"{' [wishing-well]' if is_ww else ''}")

    @staticmethod
    def _is_wishing_well(tmpl: QuestTemplate) -> bool:
        """True when the quest's reward generator is a wishing-well alias — those
        rewards drop on the ground rather than going into the bag."""
        return bool(tmpl.reward_item_generator
                    and tmpl.reward_item_generator.lower() in _WISHING_WELL_ALIASES)

    def _drop_reward(self, conn: "RRConnection", item_gc: str, level: int) -> bool:
        """Drop one reward item on the ground at the player's feet. Returns True
        when a ground entity was spawned. Never raises — a drop hiccup must not
        abort the turn-in (the quest is already finalized)."""
        from . import loot
        try:
            return loot.drop_item_near_player(
                self._server, conn, item_gc, level=level) is not None
        except Exception as ex:  # noqa: BLE001
            log.error(f"[QUEST-REWARDS] ground drop failed for {item_gc}: {ex}")
            return False

    def _roll_reward_items(self, conn: "RRConnection", tmpl: QuestTemplate,
                           level: int) -> List[str]:
        """Resolve the quest's reward item generator to concrete item gc_types
        (C# rolls ``numRewardItems`` from ``rewardItemGenerator``). Returns ``[]``
        when the quest has no item reward or the generator can't be resolved.

        The wishing-well generators (``_WISHING_WELL_ALIASES``) don't resolve as a
        bare name, so they redirect to a class-appropriate set of real WishingWell
        IGs (each rolls a random, level-gated tier)."""
        if not tmpl.reward_item_generator or tmpl.num_reward_items <= 0:
            return []
        if tmpl.reward_item_generator.lower() in _WISHING_WELL_ALIASES:
            return self._roll_wishing_well_items(conn, level, tmpl.num_reward_items)
        from ..data import item_generator_resolver
        items = item_generator_resolver.resolve_generator_items(
            tmpl.reward_item_generator, level=level, count=tmpl.num_reward_items)
        if not items:
            log.warn(f"[QUEST-REWARDS] unresolved reward generator "
                        f"'{tmpl.reward_item_generator}' for {tmpl.quest_id}")
        return items

    def _roll_wishing_well_items(self, conn: "RRConnection", level: int,
                                 count: int,
                                 rng: Optional[random.Random] = None) -> List[str]:
        """Roll ``count`` CLIENT-SAFE, class-appropriate wishing-well rewards.

        Draws from the loot roller's droppable pool (``is_client_droppable_item``
        dash-suffix PAL weapons/armor — the only items the client can deserialise
        on the inventory add), class-filtered (:func:`_ww_item_ok`) and near the
        player's level band, then HARD-GUARDS every pick with the same predicate.
        The authentic WishingWell Mythic/prebuilt items are deliberately NOT used —
        they crash the client (see the ``_WISHING_WELL_ALIASES`` note)."""
        from ..managers import loot_roller
        from ..managers.merchants import is_client_droppable_item
        rng = rng or random.Random()
        class_name = getattr(conn, "class_name", "") or ""
        safe = [p for p in loot_roller.load_pool()
                if is_client_droppable_item(p.gc_type)
                and _ww_item_ok(p.gc_type, class_name)]
        banded = [p for p in safe
                  if abs(p.level - max(level, 1)) <= _WW_LEVEL_BAND] or safe
        out: List[str] = []
        for _ in range(max(1, count)):
            if not banded:
                break
            pick = rng.choice(banded).gc_type
            if is_client_droppable_item(pick):   # belt-and-suspenders vs the crash
                out.append(pick)
        if not out:
            log.warn(f"[QUEST-REWARDS] wishing well produced no CLIENT-SAFE item "
                     f"(class={class_name}, level={level})")
        else:
            log.info(f"[QUEST-REWARDS] wishing well -> {out} "
                     f"(class={class_name}, level={level})")
        return out

    # ── inbound component-update dispatch (channel-7 QM component) ────────────────

    def handle_component_update(self, conn: "RRConnection", sub_message: int,
                                reader: "LEReader") -> bool:
        """Dispatch a QuestManager ComponentUpdate submessage (C# UnityGameServer
        quest branch). Returns True when handled."""
        self.ensure_player_state(conn)
        if sub_message == 0x01:                       # empty-payload dialog confirm
            # The live client's Accept / Complete button sends submsg 0x01 with
            # an EMPTY payload on the QM component (C# UGS:12012 "0x01 empty =
            # ACCEPT"); which pending field is set decides accept vs turn-in.
            if conn.pending_quest_hash != 0:
                quest_hash = conn.pending_quest_hash
                npc_entity_id = conn.pending_quest_npc_entity_id
                conn.pending_quest_hash = 0
                conn.pending_quest_npc_entity_id = 0
                self.handle_accept_confirmed(conn, npc_entity_id, quest_hash)
            elif conn.pending_turn_in_instance_id != 0:
                instance_id = conn.pending_turn_in_instance_id
                conn.pending_turn_in_instance_id = 0
                self.handle_turn_in_confirmed(conn, instance_id)
            else:                                     # quest-log view, no dialog up
                conn.viewing_quest_instance_id = 1
            return True
        if sub_message == 0x02:                       # close / cancel dialog
            conn.pending_quest_hash = 0
            conn.pending_quest_npc_entity_id = 0
            conn.viewing_quest_instance_id = 0
            # Closing a turn-in dialog WITHOUT confirming (Back/Close). Remember
            # which quest's dialog this was so the client's follow-up 0x03
            # dialog-teardown can be told apart from a genuine Abandon — the
            # client sends 0x02→0x03 on EVERY turn-in-dialog close (live x64dbg
            # 2026-07-02: Complete = 0x04→0x05→0x02→0x03, Back = 0x04→0x02→0x03).
            # A confirmed turn-in (0x05) already cleared turn_in_dialog_instance_id,
            # so only a NON-confirmed close arms the teardown guard.
            conn.dialog_teardown_instance_id = conn.turn_in_dialog_instance_id
            conn.turn_in_dialog_instance_id = 0
            return True
        if sub_message == 0x03:                       # abandon (or dialog teardown)
            teardown = conn.dialog_teardown_instance_id
            conn.dialog_teardown_instance_id = 0
            if reader.remaining >= 4:
                instance_id = reader.read_uint32()
                # Suppress the dialog-teardown 0x03 the client fires right after
                # backing out of a turn-in dialog — it targets the quest whose
                # dialog we just closed unconfirmed, and abandoning it dropped the
                # STILL-ACTIVE quest (bug #9). A genuine Abandon (quest-log button)
                # never matches: an incomplete quest shows no turn-in dialog, so
                # turn_in_dialog_instance_id was never set for it.
                if instance_id != 0 and instance_id == teardown:
                    log.info(f"[QUEST-ABANDON] ignored dialog-teardown 0x03 for "
                             f"inst={instance_id} (turn-in dialog dismissed, quest "
                             f"kept active)")
                    return True
                self.handle_abandon(conn, instance_id)
            return True
        if sub_message == 0x04:                       # view from quest log / yellow "?"
            # Clicking a quest in the log (or the turn-in NPC's "?") sends the
            # instance id; when the quest is turn-in-ready show the turn-in dialog
            # (C# [QUEST-0x04] → CanQueryComplete). Otherwise it is just a view —
            # no packet. CanQueryComplete (not a bare `objectives and all(...)`) is
            # required so 0-objective AutoAcceptOnQuery quests (the wishing well)
            # and item-objective token-trade quests resolve correctly.
            if reader.remaining >= 4:
                instance_id = reader.read_uint32()
                quest = self.get_quest_by_instance(conn, instance_id)
                if quest is not None and self.can_query_complete(conn, quest):
                    quest_wire.send_turn_in_dialog(conn, instance_id)
                    # A turn-in dialog is now up for this quest — arm the
                    # teardown guard so a Back/Close (0x02→0x03) doesn't abandon
                    # it (bug #9). Only turn-in-ready quests get a dialog, so an
                    # incomplete quest's genuine Abandon is never suppressed.
                    conn.turn_in_dialog_instance_id = instance_id
            return True
        if sub_message == 0x05:                       # turn-in confirm — FUN_005c0840
            # The client's turn-in confirm is op 0x05 carrying `u32 instanceId · u8`
            # (bible §13.5 #1, live op4->recv06->op5). It is NEVER an accept — accept
            # is the empty 0x01. Read the instanceId from the payload so turn-ins that
            # never set a server-side pending_turn_in (the wishing-well
            # AutoAcceptOnQuery path) finalize too; fall back to the pending instance
            # for an empty body. The trailing u8 (confirm / reward-choice index — open
            # Q2) is consumed and ignored. handle_turn_in_confirmed no-ops on an
            # unknown instance, so a stray id is harmless.
            instance_id = conn.pending_turn_in_instance_id
            if reader.remaining >= 4:
                instance_id = reader.read_uint32()
                if reader.remaining >= 1:
                    reader.read_byte()                # confirm / reward-choice index
            conn.pending_turn_in_instance_id = 0
            # Confirmed turn-in: disarm the teardown guard so the trailing
            # 0x02→0x03 the client still sends is a harmless no-op (the quest is
            # finalized below and no longer active) rather than a suppressed
            # "abandon" (bug #9).
            conn.turn_in_dialog_instance_id = 0
            if instance_id:
                self.handle_turn_in_confirmed(conn, instance_id)
            return True
        if sub_message == 0x06:                       # query (dialog) / accept
            if reader.remaining < 9:
                return True
            npc_entity_id = reader.read_uint32()
            reader.read_byte()                        # gcType indicator
            quest_hash = reader.read_uint32()
            if conn.pending_quest_hash == quest_hash and quest_hash != 0:
                conn.pending_quest_hash = 0
                self.handle_accept_confirmed(conn, npc_entity_id, quest_hash)
            else:
                conn.pending_quest_hash = quest_hash
                conn.pending_quest_npc_entity_id = npc_entity_id
                self.handle_query(conn, npc_entity_id, quest_hash)
            return True
        if sub_message == 0x08:                       # NPCTeleporter — FUN_005c5b30
            # An NPC carrying an ``NPCTeleporter`` component shows a "Teleport to X"
            # dialog option; clicking it sends `u32 npcEntityId`. Resolve the NPC's
            # authored Teleporter{Zone,SpawnPoint} and change zone (bible §13.5 #8 —
            # e.g. world.town.npc.SnowMan1 -> dungeon_snowman/start, "Snowman
            # Sanctuary"). Distinct from the placed world-entity teleporter in
            # movement.py (a standalone NCI, not an on-NPC component).
            if reader.remaining >= 4:
                self.handle_npc_teleport(conn, reader.read_uint32())
            return True
        return False

    # ── progress tracking ─────────────────────────────────────────────────────────

    def on_creature_killed(self, conn: "RRConnection", monster_gc_type) -> None:
        """Advance kill objectives — C# ``OnCreatureKilled``."""
        candidates = ([monster_gc_type] if isinstance(monster_gc_type, str)
                      else list(monster_gc_type))
        self._advance_and_push(conn, "kill", candidates)

    def on_item_picked_up(self, conn: "RRConnection", item_gc_type: str) -> None:
        """Re-sync item objectives from the inventory and push any that changed —
        C# ``OnItemPickedUp``. Item objectives track *held* quantity (the bag),
        not a pickup counter, so a fresh pickup is reflected by recomputing the
        count rather than blindly incrementing (which would over-count merges or
        miss items already owned)."""
        state = self.get_player_state(conn)
        if state is None:
            return
        changed: List[RuntimeQuest] = []
        for quest in state.active_quests:
            if any(o.type == "item" for o in quest.objectives) and \
                    self._sync_item_objectives(conn, quest):
                changed.append(quest)
        for quest in changed:
            quest_wire.send_progress_packet(conn, quest)
        if changed:
            self.save_player_quests(conn)

    def _advance_and_push(self, conn: "RRConnection", event_type: str,
                          candidates: List[str]) -> None:
        state = self.get_player_state(conn)
        if state is None:
            return
        changed_quests: List[RuntimeQuest] = []
        lowered = [c.lower() for c in candidates if c]
        for quest in state.active_quests:
            advanced = False
            for obj in quest.objectives:
                if obj.is_complete or obj.type.lower() != event_type.lower():
                    continue
                if not obj.targets:
                    continue           # empty-target objective → no wildcard match
                if _matches(obj.targets, lowered):
                    obj.current += 1
                    advanced = True
                    log.info(f"[Quest] '{conn.login_name}' {obj.label}: "
                             f"{obj.current}/{obj.required}")
            if advanced:
                changed_quests.append(quest)
        for quest in changed_quests:
            quest_wire.send_progress_packet(conn, quest)
        if changed_quests:
            self.save_player_quests(conn)

    # ── available-quest markers ───────────────────────────────────────────────────

    def available_quests_by_npc(self, conn: "RRConnection") -> Dict[str, List[int]]:
        """Map quest-giver NPC gc_type -> acceptable quest hashes for the current
        zone — feeds the ``!`` markers (C# ``SendAvailableQuestUpdateForZone``).

        The marker key MUST equal the spawned NPC's gc_type **exactly**: the client
        matches the marker NPC string case-sensitively against the entities it
        holds, but the ``quests.npc`` field's case is inconsistent in the shipped
        content (e.g. ``world.town.NPC.TownCommander`` / ``world.Town.npc.OldMan1``
        vs the spawned ``world.town.npc.TownCommander``). So every giver is
        canonicalised to the spawned NPC's gc_type via the NPC manager. This is the
        root cause of "quests work in tutorial but not town": tutorial's quest npc
        fields happen to match the spawned case, town's mostly do not. Givers with
        no NPC-manager entry (e.g. dungeon quest-givers spawned by other paths)
        fall back to the zone-prefix gate with their raw string.
        """
        state = self.ensure_player_state(conn)
        active = {q.quest_id.lower() for q in state.active_quests}
        completed = {c.lower() for c in state.completed_quests}
        zone_prefix = _zone_prefix(getattr(conn, "current_zone_gc_type", "") or "")
        spawned = self._spawned_npc_gctypes(conn)
        by_npc: Dict[str, List[int]] = {}
        for tmpl in self._templates.values():
            if tmpl.quest_id.lower() in active or tmpl.quest_id.lower() in completed:
                continue
            if tmpl.required_quest and tmpl.required_quest.lower() not in completed:
                continue
            if not (tmpl.level <= state.level <= tmpl.max_level):
                continue
            for npc in tmpl.npcs:
                canonical = spawned.get(npc.lower())
                if canonical is not None:
                    key = canonical                      # exact spawned case
                elif zone_prefix and npc.lower().startswith(zone_prefix):
                    key = npc                            # not NPC-manager-served (dungeon)
                else:
                    continue
                by_npc.setdefault(key, []).append(quest_wire.quest_hash(tmpl.quest_id))
        return by_npc

    @staticmethod
    def _spawned_npc_gctypes(conn: "RRConnection") -> Dict[str, str]:
        """``{lowercased gc_type -> exact-case gc_type}`` of the NPCs actually
        spawned in the player's current zone, so the quest marker can be keyed by
        the exact string the client holds (see :meth:`available_quests_by_npc`).
        Keyed off ``current_zone_name`` — the same value ``world_instance`` passes
        to ``build_zone_npcs`` — so the map is the spawned set verbatim."""
        try:
            from .npcs import npc_manager
            zone_name = getattr(conn, "current_zone_name", "") or ""
            return {nd.gc_type.lower(): nd.gc_type
                    for nd in npc_manager.get_for_zone(zone_name)}
        except Exception:  # noqa: BLE001 — never break the marker on NPC lookup
            return {}

    def send_available_quest_update(self, conn: "RRConnection") -> None:
        if conn.quest_manager_id:
            quest_wire.send_available_quest_update(conn, self.available_quests_by_npc(conn))

    # ── persistence ───────────────────────────────────────────────────────────────

    def save_player_quests(self, conn: "RRConnection") -> None:
        """Write runtime active + completed quests back to the character row."""
        from ..data.saved_character import SavedQuest, SavedQuestObjective
        state = self.get_player_state(conn)
        if state is None:
            return
        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return
        saved.active_quests = [
            SavedQuest(
                quest_id=q.quest_id, quest_giver_id=q.quest_giver_id, accepted_at="",
                objectives=[
                    SavedQuestObjective(
                        objective_name=o.objective_name, type=o.type,
                        target=(o.targets[0] if o.targets else ""), label=o.label,
                        required=o.required, current=o.current)
                    for o in q.objectives])
            for q in state.active_quests]
        saved.completed_quests = list(state.completed_quests)
        character_repository.save_character(saved)


# ── module helpers ───────────────────────────────────────────────────────────────

def _matches(targets: List[str], candidates_lower: List[str]) -> bool:
    """A candidate matches an objective when it equals a target or contains it
    (case-insensitive) — C# UpdateProgress rule 1
    (``target.Equals(candidate) || candidate.Contains(target)``)."""
    for target in targets:
        tl = target.lower()
        if not tl:
            continue
        for cand in candidates_lower:
            if tl == cand or tl in cand:
                return True
    return False


def _zone_prefix(zone_gc_type: str) -> str:
    """First two dotted segments, e.g. ``world.town`` — used to scope quest givers
    to the current zone."""
    if not zone_gc_type:
        return ""
    parts = zone_gc_type.lower().split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


# Objective class (root ``extends``) → objective type used on the wire / for
# progress matching. The quest's raw_json carries the resolved objective sub-blocks.
_OBJECTIVE_KIND_TYPE = (
    ("itemobjective", "item"),
    ("killobjective", "kill"),
    ("gotoobjective", "goto"),
    ("activateobjective", "activate"),
)


def _to_int(value, default: int = 1) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _objective_type(extends: str) -> Optional[str]:
    ext = (extends or "").lower()
    for needle, kind in _OBJECTIVE_KIND_TYPE:
        if needle in ext:
            return kind
    return None


def _extract_objectives(raw_json: str) -> List[QuestObjectiveTemplate]:
    """Parse a quest's authoritative ``raw_json`` into objective templates.

    Walks every node whose ``extends`` roots at an ``*Objective`` class and reads
    its type-specific fields:

    * **ItemObjective** → ``ItemType`` target, ``RequiredQuantity``, ``RemoveOnFinalize``
    * **KillObjective** → ``MonsterType``/``MonsterType2…`` targets, ``RequiredKills``
    * **ActivateObjective** → ``EntityType`` target
    * **GoToObjective** → ``TargetEntityName`` target

    This replaces the stale ``quest_objective_templates`` table (which omits ~520
    quests, including every token-trade quest). ``raw_json`` is the importer's own
    ground truth — its ``objective_count`` matches this walk 1:1.
    """
    try:
        root = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return []
    out: List[QuestObjectiveTemplate] = []

    def visit(node) -> None:
        if not isinstance(node, dict):
            return
        kind = _objective_type(str(node.get("extends", "")))
        props = node.get("properties") or {}
        if kind and isinstance(props, dict):
            name = props.get("Name") or props.get("name") or "MainObjective"
            label = props.get("Label") or props.get("label") or ""
            targets: List[str] = []
            if kind == "item":
                if props.get("ItemType"):
                    targets.append(str(props["ItemType"]))
                required = _to_int(props.get("RequiredQuantity"), 1)
            elif kind == "kill":
                for key, value in props.items():
                    if key.lower().startswith("monstertype") and value:
                        targets.append(str(value))
                required = _to_int(props.get("RequiredKills"), 1)
            elif kind == "activate":
                if props.get("EntityType"):
                    targets.append(str(props["EntityType"]))
                required = _to_int(props.get("RequiredQuantity"), 1)
            else:  # goto
                if props.get("TargetEntityName"):
                    targets.append(str(props["TargetEntityName"]))
                required = 1
            remove_on_finalize = str(
                props.get("RemoveOnFinalize", "")).strip().lower() == "true"
            # de-dupe targets, preserving order
            seen: set[str] = set()
            uniq = [t for t in targets if not (t.lower() in seen or seen.add(t.lower()))]
            out.append(QuestObjectiveTemplate(
                objective_name=name, type=kind, targets=uniq, label=label,
                required=max(1, required), remove_on_finalize=remove_on_finalize))
        for child in (node.get("anonymous_children") or []):
            visit(child)
        children = node.get("children") or {}
        if isinstance(children, dict):
            for child in children.values():
                visit(child)
        elif isinstance(children, list):
            for child in children:
                visit(child)

    visit(root)
    return out
