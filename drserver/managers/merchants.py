"""Merchant (vendor) subsystem — the full client shop protocol.

Port of C# ``MerchantManager`` (DR-Server, live-proven wire shapes) with the
unfaithful parts replaced by client ground truth:

* Tab configuration (labels, item generators, level ranges, per-vendor
  Buy/SellValueMod) comes straight from the vendor NPC ``.gc`` data imported
  by ``drserver/data/merchants_importer.py`` — the C# hardcoded override layer
  (invented level cascades, relabels) is gone because the DB now carries the
  authored values.
* The native refresh interval is the client's 0x2328 ticks (300s), not the
  emulator-invented 180s.

Protocol summary (all byte shapes verbatim from the C# implementation, which
ran against the real client):

* NPC spawn carries a ``merchant`` component (0x32 create, gcType "merchant",
  hasInit payload = inventories + reset timer). Static tabs ship
  ``hasItems=0`` — the client loads their contents from its own GC data.
  Quest-item tabs are server-sent with unique ids (500+) to dodge the GC-baked
  id-255 collision. Dynamic tabs are shipped EMPTY in the cached instance
  stream; per-player stock arrives as 0x35/0x1E add-updates after spawn.
* Client buys with an inbound ComponentUpdate sub ``0x1E`` on the merchant
  component (2 unknown bytes + uint32 itemId); sells with sub ``0x1F``
  (uint16 entityRef + uint32 itemId).
* The client runs the restock countdown itself (we send the remaining ticks in
  the init payload) and emits an EMPTY sub ``0x22`` on the player's
  UnitContainer at expiry — that is the cue to push removes + fresh adds.
* Prices are the client's exact Fixed32 math (``rarity_helper.calculate_buy_price``
  / ``calculate_sell_price``); the server must charge exactly what the client
  displays.
"""
from __future__ import annotations

import re
import time
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..db import character_repository
from ..data import item_catalog
from ..data import rarity_helper
from ..data import modpal_pool
from ..data.rarity_helper import ItemRarity
from ..data.gc_object import get_packet_gc_class_for, hash_djb2
from ..util.byte_io import LEWriter, LEReader
from ..net.component_update import write_synch, synch_hp

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection

# ── Native client constants (binary-derived, see C# MerchantManager) ────────
NATIVE_REFRESH_TICKS = 0x2328               # client restock timer payload
REFRESH_TICKS_PER_SECOND = 30.0
DEFAULT_REFRESH_SECONDS = NATIVE_REFRESH_TICKS / REFRESH_TICKS_PER_SECOND  # 300s
REFRESH_ADD_DELAY_SECONDS = 0x000F / REFRESH_TICKS_PER_SECOND              # 0.5s

# Quest-item tabs are re-keyed to 500+ so they never collide with the GC-baked
# static ids (255-262) — the client's getItemByID returns the FIRST match
# across all inventories (disassembly-confirmed in the C# port).
_QUEST_ITEM_ID_BASE = 500

_SUFFIX_RE = re.compile(r"-\d+$")
# Name fragments that mark special content the merchant pool must never sell
# (mythic wire format needs a per-item mod-slot table; the rest are seasonal /
# boss / generated exclusives — same exclusions as the C# base filter).
_EXCLUDED_FRAGMENTS = ("mythic", "prebuilt", "partialbuilt", "seasonal",
                       "wishingwell", "boss", "generated", "test")

_WEAPON_FRAGMENTS = ("axe", "sword", "mace", "pick", "staff", "crossbow",
                     "gun", "cannon")
_ARMOR_FRAGMENTS = ("armor", "boot", "glove", "helm", "shoulder", "pauldron",
                    "shield", "buckler", "body", "chest")

# Native rarity-name (item_wire_mods.rarity) → ItemRarity wire enum.
_RARITY_NAME_TO_ENUM: Dict[str, ItemRarity] = {
    "Normal": ItemRarity.Normal, "Superior": ItemRarity.Superior,
    "Magical": ItemRarity.Magical, "Magic": ItemRarity.Magical,
    "Rare": ItemRarity.Rare, "Unique": ItemRarity.Unique,
    "Mythic": ItemRarity.Mythic,
}

# Reverse: wire enum -> the rarity string item_wire_mods is keyed by.
_RARITY_ENUM_TO_NAME: Dict[ItemRarity, str] = {
    ItemRarity.Normal: "Normal", ItemRarity.Superior: "Superior",
    ItemRarity.Magical: "Magical", ItemRarity.Rare: "Rare",
    ItemRarity.Unique: "Unique", ItemRarity.Mythic: "Mythic",
}

# Mod sub-families that are binding / level-prefix bookkeeping rather than the
# item's primary stat — excluded when picking a representative attribute mod so a
# mage item shows its Intellect, not a "+N required level" binder. (Used only by
# the sparse item_wire_mods fallback; modpal_pool has its own subtree filter.)
_NON_STAT_MOD_FRAGMENTS = ("binder", "required", "levelprefix")

# How many attribute mods an item carries, by rarity — one primary attribute plus
# rarity-scaled thematic bonuses, so higher-rarity gear visibly has a richer (and
# random) mod stack instead of a single repeated stat.
_MODS_BY_RARITY: Dict[ItemRarity, int] = {
    ItemRarity.Normal: 0, ItemRarity.Superior: 1, ItemRarity.Magical: 2,
    ItemRarity.Rare: 3, ItemRarity.Unique: 3, ItemRarity.Mythic: 4,
}

# Upper safety ceiling on generated stock per dynamic tab. The 10x14 grid is the
# real limiter — greedy placement of 2x2+ gear fills ~20-28 items (~80-95% of the
# grid) before it runs out of room — so this is just a bound to keep the stock-add
# packet sane. It must stay ABOVE the grid's natural fill, otherwise it chops tabs
# below it and leaves visibly empty columns (the old value of 24 truncated the
# armor / scrap / junkpile tabs, which the grid would otherwise pack to 27-28).
_MAX_GENERATED_PER_TAB = 40

# After a purchase the freed slot is NOT refilled instantly: an item the exact
# size of the hole (e.g. a 1x2 dagger) would almost always be replaced by the
# same archetype. The refill is DEBOUNCED instead — each buy pushes the due time
# out, so a burst of purchases coalesces into ONE top-up that re-packs the
# combined freed space with varied (and larger) items. Fires ~1s after the last
# buy; the per-instance tick (which flushes merchants ~once a second) drives it.
BUY_REFILL_DELAY_SECONDS = 1.0

# Per-IG ACCEPTED rarities (the vendor shows an item at the rarity the IG
# actually generates it — forcing a rarity the IG never produces for that item,
# e.g. a Superior sword, makes the client reject the ItemAdd → "Code 7" on join).
# Town vendors carry the lower green/blue tiers; special-event vendors the higher.
_IG_RARITIES: Dict[str, frozenset] = {
    "merchantweaponig": frozenset({"Superior", "Magical"}),
    "merchantarmorig": frozenset({"Superior", "Magical"}),
    "merchanttrashig": frozenset({"Superior", "Magical"}),
    "merchantsuperiorig": frozenset({"Superior"}),
    "merchantrandomig": frozenset({"Superior", "Magical"}),
    "merchantspecialevent01ig": frozenset({"Rare", "Unique"}),
}
_IG_KIND: Dict[str, Tuple[str, ...]] = {
    "merchantweaponig": ("weapon",),
    "merchantarmorig": ("armor",),
    "merchanttrashig": ("weapon", "armor"),
    "merchantsuperiorig": ("weapon", "armor"),
    "merchantrandomig": ("weapon", "armor"),
    "merchantspecialevent01ig": ("weapon", "armor"),
}

# Two independent gates:
#
# _NATIVE_POOL_ENABLED — generate from the native named-PAL family the IGs
#   actually reference (faithful item identity + real per-IG rarities + real
#   display names like "Vergrim's Vestments"), instead of the dash-suffix
#   cosmetic-variant family (Cardboard/Tin/Diamond axes whose -N is a SKIN, not
#   rarity, so the old code mislabeled every one Rare/Unique). No protocol risk —
#   these are items.pal.* the client knows; serialized exactly like any gear.
#
# _NATIVE_MODS_ENABLED — emit the items.modpal.* ATTRIBUTE mods (Intellect etc.)
#   as ItemModifier children so mage gear actually shows Intellect, fighter gear
#   its stats, and so on. RE-RESOLVED 2026-06-20 (Ghidra, client ground truth):
#   ItemAttributeModifier::readData @0x00588AE0 reads flags:u8, then u8 if
#   flags&1, then u32 if flags&2 — so the body is the SAME 6 bytes as the
#   already-working ScaleMod (flags 0x03 -> 0x03 0x15 <u32>). The earlier crashes
#   came from a WRONG body (a 1-byte 0x00, then a 14-byte buff-Modifier body),
#   never from the type resolution (a live trace had already parsed "+1 Endurance"
#   fine). Each modref is a real registered client class (sourced from
#   item_wire_mods), so by-hash resolution + the fixed 6-byte body is crash-safe.
# REVERTED pool stays legacy (dash-suffix), proven-working; mods are a TARGETED
# ADDITION on top of it (armor only; weapons keep ScaleMod, matching the live
# client's hybrid generation), NOT the named-PAL rework that regressed (Code-7).
_NATIVE_POOL_ENABLED = False
_NATIVE_MODS_ENABLED = True


def _is_weapon_type(gc_type: str) -> bool:
    lower = gc_type.lower()
    return any(f in lower for f in _WEAPON_FRAGMENTS)


def _is_armor_type(gc_type: str) -> bool:
    lower = gc_type.lower()
    return any(f in lower for f in _ARMOR_FRAGMENTS)


def is_client_droppable_item(gc_type: str) -> bool:
    """True iff a weapon/armor ``gc_type`` is a client-itemized class that is
    safe to serialize onto the wire as a ground-drop / inventory entity.

    The proven-safe baseline is the **dash-suffix PAL family the merchant sells**
    — it renders and equips on the live client. The filter excludes:

    * classes without the ``-N`` dash suffix (the deprecated content namespace,
      e.g. ``items.deprecated.deprecatedchildarmorpal.boots036``) — the client
      has no stable GCObject schema for these, so dropping one desyncs the entity
      stream and crashes with "Zone communication error" (``GCClassRegistry::
      readType Invalid type tag`` — live-confirmed 2026-06-17),
    * the ``mythic/prebuilt/partialbuilt/seasonal/...`` special families (bespoke
      wire bodies), and ``.visual`` cosmetic entries,
    * anything that is not a weapon or armor type.

    Single source of truth shared by the merchant stock pool (:meth:`_ensure_pool`)
    and the loot roller (:func:`loot_roller.load_pool`) so the two never drift.
    """
    lower = (gc_type or "").lower()
    if not _SUFFIX_RE.search(lower):
        return False
    if any(f in lower for f in _EXCLUDED_FRAGMENTS):
        return False
    if lower.endswith(".visual"):
        return False
    return _is_weapon_type(lower) or _is_armor_type(lower)


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class MerchantItem:
    """One item in a merchant tab (static or generated)."""
    gc_type: str
    item_id: int
    x: int = 0
    y: int = 0
    quantity: int = 1
    level: int = 1
    rarity: ItemRarity = ItemRarity.Normal
    price: int = 0              # pre-calculated base buy price
    gold_value: float = 1.0     # raw GC GoldValue (per-player recalc input)
    scale_mod: str = ""
    # Native ItemModifier refs (items.modpal.*) resolved from the IG/MG/ModPAL
    # chain (see item_wire_mods table). Emitted as by-hash 0x04 children.
    mod_refs: List[str] = field(default_factory=list)


@dataclass
class MerchantTab:
    inv_id: int
    name: str
    label: str
    gc_type: str                # component child path, e.g. ...Merchant.Weapons
    width: int = 10
    height: int = 14
    static_contents: bool = False
    auto_generate: bool = False
    item_generator: str = ""
    min_item_level: int = 0
    max_item_level: int = 0
    regen_seconds: float = 0.0
    server_sends_items: bool = False
    generated_for_level: int = 0
    items: List[MerchantItem] = field(default_factory=list)


@dataclass
class MerchantDef:
    merchant_id: int
    npc_gc_type: str
    merchant_gc_type: str
    name: str
    sell_value_mod: float = 1.0
    buy_value_mod: float = 1.0
    regenerate_items: bool = True
    next_item_id: int = 263     # above the GC-baked static ids (255-262)
    inventories: List[MerchantTab] = field(default_factory=list)

    @property
    def dynamic_tabs(self) -> List[MerchantTab]:
        return [t for t in self.inventories if t.auto_generate and not t.static_contents]


# ── Item-generator (IG) tab filters ──────────────────────────────────────────
# Faithful to the authored ItemGeneratorTable .gc files; Chance=N is a 1-in-N
# gate (C#-matching interpretation). Tier = the -N gc suffix (1..5).

def _ig_accepts(ig: str, gc_type: str, tier: int, rng: random.Random) -> bool:
    ig_lower = (ig or "").lower()
    if ig_lower == "merchantweaponig":
        # Authored: Rare Chance=1 (always) + Unique Chance=20 (1-in-20), weapons.
        if not _is_weapon_type(gc_type):
            return False
        if tier == 4:
            return True
        return tier == 5 and rng.randrange(20) == 0
    if ig_lower == "merchantarmorig":
        # Authored: Rare Chance=1 + Unique Chance=20, armor.
        if not _is_armor_type(gc_type):
            return False
        if tier == 4:
            return True
        return tier == 5 and rng.randrange(20) == 0
    if ig_lower == "merchanttrashig":
        # Authored "Scrap Heap": Superior + Magic, weapons and armor.
        return tier in (2, 3)
    if ig_lower == "merchantsuperiorig":
        # Authored: Superior only, weapons and armor.
        return tier == 2
    if ig_lower == "merchantrandomig":
        # Authored (dungeon vendors): Rare Chance=1 + Unique Chance=2, both kinds.
        if tier == 4:
            return True
        return tier == 5 and rng.randrange(2) == 0
    if ig_lower == "merchantspecialevent01ig":
        # Authored (Amazonian): Rare Chance=4 + Unique Chance=1 (+ Mythic, which
        # the pool excludes until the mythic wire path is ported).
        if tier == 4:
            return rng.randrange(4) == 0
        return tier == 5
    # Unknown / unported generator (MythicIG, QuestItemIG.Token, items.ig.*
    # single-family tables) — generate nothing rather than something wrong.
    return False


class MerchantManager:
    """Global merchant registry + runtime stock (stock is shared per vendor
    across zone instances, matching the C# server)."""

    def __init__(self) -> None:
        self._merchants: Dict[str, MerchantDef] = {}     # npc_gc_type.lower() -> def
        self._loaded = False
        self._last_regen: Dict[str, float] = {}          # f"{npc}_{inv}" -> monotonic
        # Sellable pool: (gc_type, tier, gc_gold_value, mod_count) per candidate.
        self._pool: List[Tuple[str, int, float, int]] = []
        # Native pool from item_wire_mods: (gc_type, rarity_name, gc_gold_value).
        self._native_pool: List[Tuple[str, str, float]] = []
        # (item_gc_lower, rarity_name) -> ordered items.modpal.* refs.
        self._wire_mods: Dict[Tuple[str, str], List[str]] = {}
        self._wire_mods_loaded = False
        # (modpal-family, rarity_name) -> registered modref strings (primary
        # stat mods only) — drives the class-appropriate attribute mod a merchant
        # item carries (e.g. mage gear -> Intellect).
        self._mod_family_index: Dict[Tuple[str, str], List[str]] = {}
        self._mod_index_loaded = False
        self._mod_counts: Dict[str, int] = {}
        self._is_member_cache: Dict[str, bool] = {}

    # ── Loading ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        if self._loaded:
            return
        self._merchants.clear()
        try:
            for row in db.execute_reader("SELECT * FROM merchants").fetchall():
                npc_gc = db.get_string(row, "npc_gc_type")
                md = MerchantDef(
                    merchant_id=db.get_int(row, "id"),
                    npc_gc_type=npc_gc,
                    merchant_gc_type=db.get_string(row, "merchant_gc_type"),
                    name=db.get_string(row, "name"),
                    sell_value_mod=db.get_float(row, "sell_value_mod", 1.0),
                    buy_value_mod=db.get_float(row, "buy_value_mod", 1.0),
                    regenerate_items=bool(db.get_int(row, "regenerate_items", 1)),
                )
                self._merchants[npc_gc.lower()] = md

            by_id = {md.merchant_id: md for md in self._merchants.values()}

            for row in db.execute_reader(
                    "SELECT * FROM merchant_inventories").fetchall():
                md = by_id.get(db.get_int(row, "merchant_id"))
                if md is None:
                    continue
                auto = bool(db.get_int(row, "auto_generate", 0))
                static = bool(db.get_int(row, "static_contents", 0 if auto else 1))
                tab = MerchantTab(
                    inv_id=db.get_int(row, "inv_id"),
                    name=db.get_string(row, "name"),
                    label=db.get_string(row, "label"),
                    gc_type=db.get_string(row, "gc_type"),
                    width=db.get_int(row, "width", 10),
                    height=db.get_int(row, "height", 14),
                    static_contents=static,
                    auto_generate=auto,
                    item_generator=db.get_string(row, "item_generator"),
                    min_item_level=db.get_int(row, "min_item_level"),
                    max_item_level=db.get_int(row, "max_item_level"),
                    regen_seconds=db.get_float(row, "regen_seconds",
                                               DEFAULT_REFRESH_SECONDS),
                )
                if tab.auto_generate and tab.regen_seconds <= 0:
                    tab.regen_seconds = DEFAULT_REFRESH_SECONDS
                md.inventories.append(tab)

            for row in db.execute_reader(
                    "SELECT * FROM merchant_inventory_items").fetchall():
                md = by_id.get(db.get_int(row, "merchant_id"))
                if md is None:
                    continue
                inv_id = db.get_int(row, "inv_id")
                for tab in md.inventories:
                    if tab.inv_id != inv_id:
                        continue
                    gc = db.get_string(row, "item_gc_type")
                    tab.items.append(MerchantItem(
                        gc_type=gc,
                        item_id=db.get_int(row, "item_slot_id"),
                        x=db.get_int(row, "inventory_x"),
                        y=db.get_int(row, "inventory_y"),
                        quantity=db.get_int(row, "quantity", 1),
                        level=rarity_helper.get_item_level(gc),
                        rarity=rarity_helper.get_rarity_from_tier(
                            rarity_helper.get_tier_from_gc_type(gc)),
                        gold_value=self._gold_value_for(gc),
                    ))
                    break

            # Post-process: quest tabs become server-sent with unique 500+ ids
            # (client id-255 collision); bump the per-merchant id counter past
            # every static id.
            for md in self._merchants.values():
                md.inventories.sort(key=lambda t: t.inv_id)
                for tab in md.inventories:
                    for item in tab.items:
                        if item.item_id >= md.next_item_id:
                            md.next_item_id = item.item_id + 1
                    if tab.static_contents and any(
                            "questitem" in i.gc_type.lower() for i in tab.items):
                        tab.server_sends_items = True
                        next_qid = max(_QUEST_ITEM_ID_BASE, md.next_item_id)
                        for item in tab.items:
                            item.item_id = next_qid
                            next_qid += 1
                        md.next_item_id = next_qid

            self._loaded = True
            log.info(f"[Merchant] loaded {len(self._merchants)} merchants")
        except Exception as ex:  # noqa: BLE001 — content DB may be absent in tests
            log.error(f"[Merchant] load error: {ex}")

    def _ensure_pool(self) -> None:
        """Build the sellable candidate pool from the weapons/armor content
        tables (C# read the now-dropped ``sellable_items`` table; the rebuilt
        typed tables carry the same keys + the raw ``gc_gold_value``)."""
        if self._pool:
            return
        try:
            for table in ("weapons", "armor"):
                for row in db.execute_reader(
                        f"SELECT gc_type, gc_gold_value, mod_count FROM {table}"
                ).fetchall():
                    gc = db.get_string(row, "gc_type")
                    lower = gc.lower()
                    if not is_client_droppable_item(lower):
                        continue
                    tier = rarity_helper.get_tier_from_gc_type(lower)
                    gv = db.get_float(row, "gc_gold_value", 0.0)
                    if gv <= 0:
                        gv = rarity_helper.get_base_gold_value(lower)
                    mod_count = db.get_int(row, "mod_count", -1)
                    self._pool.append((gc, tier, gv, mod_count))
                    self._mod_counts[lower] = mod_count
            log.info(f"[Merchant] sellable pool: {len(self._pool)} candidates")
        except Exception as ex:  # noqa: BLE001
            log.error(f"[Merchant] pool load error: {ex}")

    def _ensure_wire_mods(self) -> None:
        """Load the baked native item mods (``item_wire_mods`` table) into
        ``_wire_mods`` and build ``_native_pool`` (the named-PAL family the IGs
        actually reference — mage gear included). Falls back to the legacy
        dash-suffix pool if the table is absent (older DBs / unit tests)."""
        if self._wire_mods_loaded:
            return
        self._wire_mods_loaded = True
        try:
            rows = db.execute_reader(
                "SELECT item_gc_type, rarity, slot, mod_ref FROM item_wire_mods"
                " ORDER BY item_gc_type, rarity, slot").fetchall()
        except Exception:  # noqa: BLE001 — table absent on legacy DBs
            log.info("[Merchant] item_wire_mods table absent — legacy pool only")
            return
        # _wire_mods is keyed by the NORMALIZED item key (items.pal. prefix
        # stripped) so lookups via _wire_mods_for(normalize_key(...)) match.
        for row in rows:
            gc_full = db.get_string(row, "item_gc_type")
            rarity = db.get_string(row, "rarity")
            nkey = item_catalog.normalize_key(gc_full)
            self._wire_mods.setdefault((nkey, rarity), []).append(
                db.get_string(row, "mod_ref"))

    def _ensure_native_pool(self) -> None:
        """Build the vendor SELECTION pool from the **IG-generated ``(item,
        rarity)`` combos** in ``item_wire_mods`` — the exact item+rarity pairs the
        client's generators actually produce, so the client always accepts the
        ItemAdd. Sourcing the rarity from the IG (not forcing one) is essential:
        an ``(item, Superior)`` pair the IG never generates (only axes resolve at
        Superior; swords/maces/staves at Magical+) makes the client reject the
        ItemAdd → "Zone communication error. Code 7" on join. Broad: all weapon/
        armor families. Base ``Normal###`` items only (no ``_element`` variants —
        those are non-vendor gc types). Entry = ``(gc, rarity_name, kind, gold)``.
        """
        if self._native_pool:
            return
        try:
            rows = db.execute_reader(
                "SELECT DISTINCT item_gc_type, rarity FROM item_wire_mods"
            ).fetchall()
        except Exception:  # noqa: BLE001 — table absent → legacy pool
            return
        for row in rows:
            rarity_name = db.get_string(row, "rarity")
            if rarity_name not in _RARITY_NAME_TO_ENUM:
                continue                              # Seasonal / WishingWell
            gc = db.get_string(row, "item_gc_type")
            lower = gc.lower()
            leaf = lower.rsplit(".", 1)[-1]
            if not leaf.startswith("normal") or "_" in leaf:
                continue                              # plain base items only
            if any(f in lower for f in _EXCLUDED_FRAGMENTS):
                continue
            kind = ("weapon" if _is_weapon_type(lower)
                    else "armor" if _is_armor_type(lower) else None)
            if kind is None:
                continue
            self._native_pool.append(
                (gc, rarity_name, kind, self._gold_value_for(gc)))
        log.info(f"[Merchant] native pool: {len(self._native_pool)} "
                 f"IG-generated (item,rarity) candidates")

    def _wire_mods_for(self, gc_type: str, rarity_name: str) -> List[str]:
        self._ensure_wire_mods()
        return list(self._wire_mods.get(
            (item_catalog.normalize_key(gc_type).lower(), rarity_name), []))

    def _ensure_mod_family_index(self) -> None:
        """Index the baked client mods by (modpal-family, rarity) so a merchant
        item can be given a class-appropriate attribute mod (e.g. mage gear ->
        Intellect). Every modref is a real registered client class (resolved from
        the client's own IG/modpal content into ``item_wire_mods``), so emitting
        one is crash-safe regardless of which item it lands on."""
        if self._mod_index_loaded:
            return
        self._mod_index_loaded = True
        try:
            rows = db.execute_reader(
                "SELECT mod_ref, rarity FROM item_wire_mods").fetchall()
        except Exception:  # noqa: BLE001 — table absent on legacy DBs
            log.info("[Merchant] item_wire_mods absent — no attribute mods")
            return
        for row in rows:
            ref = db.get_string(row, "mod_ref")
            low = ref.lower()
            if any(f in low for f in _NON_STAT_MOD_FRAGMENTS):
                continue
            match = re.search(r"modpal\.([a-z]+)", low)
            if match is None:
                continue
            bucket = self._mod_family_index.setdefault(
                (match.group(1), db.get_string(row, "rarity")), [])
            if ref not in bucket:
                bucket.append(ref)

    @staticmethod
    def _modpal_family_for(gc_type: str) -> Optional[str]:
        """Map an item to its client modpal family. ARMOR only — weapons are the
        old numbered generation whose stats ride the ScaleMod alone (no separate
        attribute modpal), matching the live client's hybrid item generation."""
        low = gc_type.lower()
        if not _is_armor_type(low):
            return None
        is_shield = "shield" in low or "buckler" in low
        if "crystal" in low or "mage" in low:
            return "mageshieldmodpal" if is_shield else "magemodpal"
        if "plate" in low or "chain" in low:
            return None if is_shield else "fightermodpal"
        if "leather" in low or "scale" in low or "splint" in low:
            return None if is_shield else "rangermodpal"
        return "sharedarmormodpal"

    def _roll_item_mods(self, gc_type: str, rarity: ItemRarity,
                        rng: random.Random) -> List[str]:
        """Roll a random, class-appropriate attribute mod stack for an item — one
        primary attribute (Intellect for mage, etc., from the rarity-tier subtree)
        plus rarity-scaled thematic bonuses (DamageBonus, resist, ...), all
        distinct. Drawn from the rich GCDictionary pool (every ref registered ->
        crash-safe); falls back to the sparse baked ``item_wire_mods`` table when
        the dictionary is unavailable. ``[]`` when no stat mods apply (weapons,
        Normal gear, or unmapped families)."""
        family = self._modpal_family_for(gc_type)
        if family is None:
            return []
        target = _MODS_BY_RARITY.get(rarity, 1)
        if target <= 0:
            return []
        rarity_name = _RARITY_ENUM_TO_NAME.get(rarity, "")
        quality, thematic = modpal_pool.stat_mods(family, rarity_name)
        if not quality and not thematic:
            # Dictionary unavailable: fall back to the baked (sparse) table.
            self._ensure_mod_family_index()
            quality = list(self._mod_family_index.get((family, rarity_name), []))
        if not quality and not thematic:
            return []
        chosen: List[str] = []
        if quality:                                   # one primary attribute mod
            chosen.append(rng.choice(quality))
        extras = [m for m in thematic if m not in chosen]
        rng.shuffle(extras)
        for ref in extras:
            if len(chosen) >= target:
                break
            chosen.append(ref)
        if len(chosen) < target and quality:          # top up if thematic ran out
            spare = [m for m in quality if m not in chosen]
            rng.shuffle(spare)
            chosen.extend(spare[:target - len(chosen)])
        return chosen[:target]

    def _gold_value_for(self, gc_type: str) -> float:
        key = item_catalog.normalize_key(gc_type)
        try:
            for table in ("weapons", "armor", "items"):
                row = db.execute_reader(
                    f"SELECT gc_gold_value FROM {table} WHERE gc_type = :k COLLATE NOCASE",
                    {"k": key}).fetchone()
                if row is not None and row[0] and row[0] > 0:
                    return float(row[0])
        except Exception:  # noqa: BLE001
            pass
        return rarity_helper.get_base_gold_value(gc_type)

    def _mod_count_for(self, gc_type: str) -> int:
        """Mod-slot byte count for the merchant Item serialization.

        C# REGULAR path: DB modCount; chain armor (not shield) is 1 regardless
        (no SpeedM child); unknown items default weapon=1 / armor=2.
        """
        lower = item_catalog.normalize_key(gc_type)
        count = self._mod_counts.get(lower)
        if count is None or count < 0:
            try:
                for table in ("weapons", "armor", "items"):
                    row = db.execute_reader(
                        f"SELECT mod_count FROM {table} WHERE gc_type = :k COLLATE NOCASE",
                        {"k": lower}).fetchone()
                    if row is not None and row[0] is not None:
                        count = int(row[0])
                        break
            except Exception:  # noqa: BLE001
                count = None
            if count is None or count < 0:
                count = 1 if _is_weapon_type(lower) else 2
            self._mod_counts[lower] = count
        if lower.startswith("chain") and "shield" not in lower:
            return 1
        return max(0, min(12, count))

    # ── Public lookups ───────────────────────────────────────────────────────

    def is_merchant(self, npc_gc_type: str) -> bool:
        if not self._loaded:
            self.load()
        return (npc_gc_type or "").lower() in self._merchants

    def get_by_npc(self, npc_gc_type: str) -> Optional[MerchantDef]:
        if not self._loaded:
            self.load()
        return self._merchants.get((npc_gc_type or "").lower())

    # ── Stock generation ─────────────────────────────────────────────────────

    # Higher rarity skews the rolled level toward the top of the tab band so a
    # Unique never shows up at "Requires Level 1" (the client displays
    # levelByte-5, FUN_00496640) — keeps tier and level consistent.
    _RARITY_LEVEL_SKEW = {
        ItemRarity.Normal: 0.0, ItemRarity.Superior: 0.15,
        ItemRarity.Magical: 0.35, ItemRarity.Rare: 0.55,
        ItemRarity.Unique: 0.75, ItemRarity.Mythic: 0.85,
    }

    def _roll_stock_level(self, tab: MerchantTab, rng: random.Random,
                          rarity: Optional[ItemRarity] = None) -> int:
        lo = tab.min_item_level if tab.min_item_level > 0 else 1
        hi = tab.max_item_level if tab.max_item_level > 0 else lo
        if hi < lo:
            hi = lo
        if rarity is not None and hi > lo:
            lo += int((hi - lo) * self._RARITY_LEVEL_SKEW.get(rarity, 0.5))
        return lo if hi <= lo else rng.randint(lo, hi)

    def generate_tab(self, md: MerchantDef, tab: MerchantTab,
                     player_level: int) -> None:
        """Regenerate one dynamic tab's stock.

        Prefers the NATIVE named-PAL pool (``item_wire_mods`` — the family the
        client IGs actually reference, with real attribute mods + mage gear);
        bounded to ``_MAX_GENERATED_PER_TAB`` items so the shop is stocked, not
        overcrowded. Falls back to the legacy dash-suffix pool when the baked
        mods table is absent (older DBs / unit tests)."""
        tab.items.clear()
        tab.generated_for_level = player_level
        rng = random.Random()
        if _NATIVE_POOL_ENABLED:
            self._ensure_native_pool()
            ig = (tab.item_generator or "").lower()
            rarities = _IG_RARITIES.get(ig)
            kinds = _IG_KIND.get(ig)
            if rarities and kinds and self._native_pool:
                native = [(gc, rn, gv) for gc, rn, kind, gv in self._native_pool
                          if kind in kinds and rn in rarities]
                if native:
                    rng.shuffle(native)
                    self._place_native(md, tab, native, rng, player_level)
                    return
        self._generate_tab_legacy(md, tab, rng)

    @staticmethod
    def _grid_placer(tab: MerchantTab):
        grid = [[False] * tab.height for _ in range(tab.width)]

        def find_spot(w: int, h: int) -> Optional[Tuple[int, int]]:
            # X-outer, Y-inner — matches the client's Inventory::findSlot.
            for x in range(tab.width - w + 1):
                for y in range(tab.height - h + 1):
                    if all(not grid[x + dx][y + dy]
                           for dx in range(w) for dy in range(h)):
                        for dx in range(w):
                            for dy in range(h):
                                grid[x + dx][y + dy] = True
                        return x, y
            return None
        return find_spot

    @staticmethod
    def _native_item_level(tab: MerchantTab, gc: str, player_level: int) -> int:
        """Deterministic required level for a native item, **scaled to the
        buyer**. Keyed on item identity (same item → same level every restock,
        its true level, not a random roll), inside the vendor's authored
        ``MinItemLevel..MaxItemLevel`` band, with the upper bound capped to the
        player's level so a low-level character is never shown high-level gear.
        The client displays ``levelByte - 5`` (FUN_00496640), so capping the byte
        at ``player_level + 5`` keeps every shown item's required level <= the
        player's — accessible now, not "high level"."""
        lo = tab.min_item_level if tab.min_item_level > 0 else 1
        hi = tab.max_item_level if tab.max_item_level > 0 else lo
        if player_level > 0:
            hi = min(hi, max(lo, player_level + 5))
        if hi <= lo:
            return max(1, lo)
        return lo + (hash_djb2(item_catalog.normalize_key(gc)) % (hi - lo + 1))

    def _place_native(self, md: MerchantDef, tab: MerchantTab,
                      candidates: List[Tuple[str, str, float]],
                      rng: random.Random, player_level: int) -> None:
        find_spot = self._grid_placer(tab)
        for gc, rarity_name, gv in candidates:
            if len(tab.items) >= _MAX_GENERATED_PER_TAB:
                break
            w, h = self._item_dimensions(gc)
            spot = find_spot(w, h)
            if spot is None:
                continue
            x, y = spot
            rarity = _RARITY_NAME_TO_ENUM.get(rarity_name, ItemRarity.Superior)
            level = self._native_item_level(tab, gc, player_level)
            price = rarity_helper.calculate_buy_price(level, rarity, gv)
            mod_refs = (self._wire_mods_for(gc, rarity_name)
                        if _NATIVE_MODS_ENABLED else [])
            tab.items.append(MerchantItem(
                gc_type=gc, item_id=md.next_item_id, x=x, y=y, quantity=1,
                level=level, rarity=rarity, price=price, gold_value=gv,
                scale_mod=rarity_helper.get_random_scale_mod(rarity),
                mod_refs=mod_refs))
            md.next_item_id += 1

    def _legacy_candidates(self, tab: MerchantTab,
                           rng: random.Random) -> List[Tuple[str, int, float]]:
        """Shuffled ``(gc, tier, gold_value)`` candidates from the legacy
        dash-suffix pool for a tab's item generator. Shared by full-tab
        generation and single-slot refill so the two never diverge."""
        self._ensure_pool()
        authored_max = tab.max_item_level if tab.max_item_level > 0 else max(
            tab.min_item_level if tab.min_item_level > 0 else 1, 1)
        candidates = [
            (gc, tier, gv) for gc, tier, gv, _mc in self._pool
            if rarity_helper.get_item_level(gc) <= authored_max
            and _ig_accepts(tab.item_generator, gc, tier, rng)
        ]
        rng.shuffle(candidates)
        return candidates

    def _make_legacy_item(self, md: MerchantDef, tab: MerchantTab, gc: str,
                          tier: int, gv: float, x: int, y: int,
                          rng: random.Random) -> MerchantItem:
        """Build one placed MerchantItem (rolls level + price + ScaleMod) and
        advance the merchant's item-id counter."""
        rarity = rarity_helper.get_rarity_from_tier(tier)
        level = self._roll_stock_level(tab, rng)
        price = rarity_helper.calculate_buy_price(level, rarity, gv)
        item = MerchantItem(
            gc_type=gc, item_id=md.next_item_id, x=x, y=y, quantity=1,
            level=level, rarity=rarity, price=price, gold_value=gv,
            scale_mod=rarity_helper.get_random_scale_mod(rarity))
        if _NATIVE_MODS_ENABLED:
            item.mod_refs = self._roll_item_mods(gc, rarity, rng)
        md.next_item_id += 1
        return item

    def _generate_tab_legacy(self, md: MerchantDef, tab: MerchantTab,
                             rng: random.Random) -> None:
        """Legacy dash-suffix pool (kept for DBs without item_wire_mods)."""
        candidates = self._legacy_candidates(tab, rng)
        if not candidates:
            return
        find_spot = self._grid_placer(tab)
        for gc, tier, gv in candidates:
            if len(tab.items) >= _MAX_GENERATED_PER_TAB:
                break
            w, h = self._item_dimensions(gc)
            spot = find_spot(w, h)
            if spot is None:
                continue
            tab.items.append(
                self._make_legacy_item(md, tab, gc, tier, gv, *spot, rng))

    @staticmethod
    def _build_occupancy(tab: MerchantTab) -> List[List[bool]]:
        """Occupancy grid reflecting the tab's CURRENT items — for incremental
        placement (refilling the single cell a purchase just freed) without
        disturbing the items already on the wire."""
        grid = [[False] * tab.height for _ in range(tab.width)]
        for it in tab.items:
            w, h = MerchantManager._item_dimensions(it.gc_type)
            for dx in range(w):
                for dy in range(h):
                    gx, gy = it.x + dx, it.y + dy
                    if 0 <= gx < tab.width and 0 <= gy < tab.height:
                        grid[gx][gy] = True
        return grid

    @staticmethod
    def _region_free(grid: List[List[bool]], tab: MerchantTab,
                     x: int, y: int, w: int, h: int) -> bool:
        if x < 0 or y < 0 or x + w > tab.width or y + h > tab.height:
            return False
        return all(not grid[x + dx][y + dy]
                   for dx in range(w) for dy in range(h))

    @staticmethod
    def _first_free_spot(grid: List[List[bool]], tab: MerchantTab,
                         w: int, h: int) -> Optional[Tuple[int, int]]:
        """First open w×h region (X-outer, Y-inner — client findSlot order)."""
        for x in range(tab.width - w + 1):
            for y in range(tab.height - h + 1):
                if MerchantManager._region_free(grid, tab, x, y, w, h):
                    return x, y
        return None

    def schedule_buy_refill(self, conn: "RRConnection", npc_gc_type: str,
                            merchant_cid: int) -> None:
        """Arm/extend the debounced post-buy refill for this connection. Called
        on every dynamic-tab purchase; pushing the due time out on each buy lets
        a burst of purchases coalesce into one top-up (so freed space accumulates
        and a *different* / larger item can land instead of re-spawning the same
        archetype). The per-instance tick fires it once the due time passes."""
        conn.merchant_refill_npc = npc_gc_type
        conn.merchant_refill_cid = merchant_cid
        conn.merchant_refill_due = time.monotonic() + BUY_REFILL_DELAY_SECONDS

    def flush_pending_buy_refill(self, conn: "RRConnection") -> bool:
        """Run the debounced refill if its due time has passed: re-pack the
        vendor's freed grid space with fresh varied items and push them with the
        proven 0x1E add shape. Driven from the per-instance merchant flush."""
        npc_gc_type = getattr(conn, "merchant_refill_npc", None)
        merchant_cid = getattr(conn, "merchant_refill_cid", 0)
        due = getattr(conn, "merchant_refill_due", 0.0)
        if not npc_gc_type or not merchant_cid or due <= 0:
            return False
        if not getattr(conn, "is_spawned", True) or not getattr(conn, "allow_flush", True):
            return False
        if time.monotonic() < due:
            return False
        conn.merchant_refill_due = 0.0           # consume the debounce window
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            return False
        return self._fill_free_space(conn, md, merchant_cid)

    def _fill_free_space(self, conn: "RRConnection", md: MerchantDef,
                         merchant_cid: int) -> bool:
        """Top up every dynamic tab's open grid space with fresh varied items
        and push them in one 0x1E add batch (the freed holes from one or more
        buys are re-packed; larger items can now span the combined space)."""
        rng = random.Random()
        additions: List[Tuple[MerchantTab, MerchantItem]] = []
        for tab in md.dynamic_tabs:
            for item in self._top_up_tab(md, tab, rng):
                additions.append((tab, item))
        if not additions:
            return False
        w = LEWriter()
        w.write_byte(0x07)
        for tab, item in additions:
            w.write_byte(0x35)
            w.write_uint16(merchant_cid)
            w.write_byte(0x1E)
            w.write_byte(tab.inv_id)
            self._write_item(w, item)
            w.write_byte(0x02)
            w.write_uint32(0x00000000)
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())
        self._track_sent_stock(conn, merchant_cid, md)
        log.info(f"[Merchant] refilled {len(additions)} slot(s) on "
                 f"{md.npc_gc_type} for '{conn.login_name}'")
        return True

    def _top_up_tab(self, md: MerchantDef, tab: MerchantTab,
                    rng: random.Random) -> List[MerchantItem]:
        """Fill a dynamic tab's open cells with fresh items, preferring archetypes
        not already on the tab (variety), then allowing repeats — until the grid
        is packed or the safety ceiling is hit. Returns the items added."""
        if tab.static_contents or tab.server_sends_items:
            return []
        if len(tab.items) >= _MAX_GENERATED_PER_TAB:
            return []
        candidates = self._legacy_candidates(tab, rng)
        if not candidates:
            return []
        grid = self._build_occupancy(tab)
        present = {it.gc_type for it in tab.items}
        added: List[MerchantItem] = []
        for require_new in (True, False):
            for gc, tier, gv in candidates:
                if len(tab.items) >= _MAX_GENERATED_PER_TAB:
                    break
                if require_new and gc in present:
                    continue
                w, h = self._item_dimensions(gc)
                spot = self._first_free_spot(grid, tab, w, h)
                if spot is None:
                    continue
                x, y = spot
                item = self._make_legacy_item(md, tab, gc, tier, gv, x, y, rng)
                tab.items.append(item)
                added.append(item)
                present.add(gc)
                for dx in range(w):
                    for dy in range(h):
                        grid[x + dx][y + dy] = True
            if len(tab.items) >= _MAX_GENERATED_PER_TAB:
                break
        return added

    def ensure_inventory_for_level(self, npc_gc_type: str,
                                   player_level: int) -> bool:
        """ItemTimeline: regenerate dynamic tabs when the requesting player's
        level differs from the one the stock was generated for."""
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            return False
        regenerated = False
        for tab in md.dynamic_tabs:
            if tab.generated_for_level != player_level:
                self.generate_tab(md, tab, player_level)
                self._last_regen[f"{md.npc_gc_type}_{tab.inv_id}"] = time.monotonic()
                regenerated = True
        return regenerated

    def _ticks_until_regen(self, md: MerchantDef, tab: MerchantTab) -> int:
        interval = tab.regen_seconds if tab.regen_seconds > 0 else DEFAULT_REFRESH_SECONDS
        last = self._last_regen.get(f"{md.npc_gc_type}_{tab.inv_id}")
        if last is None:
            return int(round(interval * REFRESH_TICKS_PER_SECOND))
        elapsed = time.monotonic() - last
        remaining = (interval - elapsed) * REFRESH_TICKS_PER_SECOND
        return max(0, int(remaining + 0.999))

    @staticmethod
    def _item_dimensions(gc_type: str) -> Tuple[int, int]:
        lower = gc_type.lower()
        if ("potion" in lower or "scroll" in lower or "consumable" in lower
                or "townportal" in lower or "questitem" in lower):
            return (1, 1)
        w, h = item_catalog.get_item_size(gc_type)
        if w <= 0 or h <= 0:
            return (2, 2)
        return (w, h)

    # ── Wire writers ─────────────────────────────────────────────────────────

    def write_merchant_component(self, w: LEWriter, npc_gc_type: str,
                                 npc_entity_id: int, merchant_cid: int,
                                 include_dynamic: bool = False) -> bool:
        """0x32 create of the ``merchant`` component on an NPC entity.

        ``include_dynamic=False`` ships dynamic tabs empty (the per-player
        stock is pushed post-spawn as 0x1E add-updates) — required because the
        instance NPC stream is built once and cached for every joiner.
        """
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            return False
        w.write_byte(0x32)
        w.write_uint16(npc_entity_id)
        w.write_uint16(merchant_cid)
        w.write_byte(0xFF)
        w.write_cstring("merchant")
        w.write_byte(0x01)                      # hasInit
        self._write_init_payload(w, md, include_dynamic)
        return True

    def _write_init_payload(self, w: LEWriter, md: MerchantDef,
                            include_dynamic: bool) -> None:
        w.write_uint32(0x000000FF)
        w.write_uint32(0x00000000)
        w.write_byte(len(md.inventories))
        for tab in sorted(md.inventories, key=lambda t: t.inv_id):
            self._write_tab(w, md, tab, include_dynamic)

        reset_ticks = 0
        for tab in md.inventories:
            if not tab.static_contents:
                reset_ticks = min(0xFFFF, self._ticks_until_regen(md, tab))
                break
        w.write_byte(0x01)
        w.write_uint16(reset_ticks)
        w.write_uint16(0x000F)

    def _write_tab(self, w: LEWriter, md: MerchantDef, tab: MerchantTab,
                   include_dynamic: bool) -> None:
        w.write_byte(0xFF)
        w.write_cstring(tab.gc_type.lower())
        w.write_byte(tab.inv_id)

        if tab.server_sends_items:
            # Phase 1: bare Item::readData per GC archetype child (no type tag),
            # Phase 2: zero additional server-created items.
            w.write_byte(0x01)
            for item in tab.items:
                w.write_uint32(item.item_id)
                w.write_byte(item.x)
                w.write_byte(item.y)
                w.write_byte(min(0xFF, max(1, item.quantity)))
                w.write_byte(0x01)              # level
                w.write_byte(0x00)              # flags
                w.write_byte(0x00)              # ItemModifier child count
            w.write_byte(0x00)
        elif (tab.static_contents or not tab.items
              or (not include_dynamic and tab.auto_generate)):
            w.write_byte(0x00)                  # client loads tab from GC data
        else:
            w.write_byte(0x01)
            count = min(len(tab.items), 255)
            w.write_byte(count)
            for item in tab.items[:count]:
                self._write_item(w, item)

    def _write_item(self, w: LEWriter, item: MerchantItem) -> None:
        """Container child Item serialization (port of C# WriteItem, minus the
        mythic IG-inject path — the pool never emits mythic items)."""
        gc = item_catalog.normalize_key(item.gc_type)
        packet_gc = get_packet_gc_class_for(gc)

        lower = item.gc_type.lower()
        is_consumable = "consumable" in lower
        is_quest = "questitem" in lower

        w.write_byte(0xFF)
        w.write_cstring(packet_gc)
        w.write_uint32(item.item_id)
        w.write_byte(item.x)
        w.write_byte(item.y)
        w.write_byte(min(0xFF, max(1, item.quantity)))
        level = item.level if item.level > 0 else (
            1 if (is_consumable or is_quest)
            else rarity_helper.get_item_level(item.gc_type))
        w.write_byte(min(0xFF, level))

        # NO flags byte here: merchant items go through Container →
        # ReadChildData<Item> → Item::readData (ID/X/Y/Qty/Level + mod bytes),
        # unlike Equipment::readInit. One byte per GC-defined ItemModifier child.
        if is_consumable or is_quest:
            # C# simple-item path: itemData?.modCount ?? 1 — consumables are not
            # in the typed tables, so the unknown default is 1, never the
            # weapon/armor fallback.
            lower_key = item_catalog.normalize_key(item.gc_type)
            mod_slots = self._mod_counts.get(lower_key)
            if mod_slots is None or mod_slots < 0:
                mod_slots = 1
        else:
            mod_slots = self._mod_count_for(item.gc_type)
        for _ in range(mod_slots):
            w.write_byte(0x00)

        if is_consumable:
            w.write_byte(0x01)
            w.write_byte(0xFF)
            w.write_cstring("mods.itemscale.normal")
            w.write_byte(0x03)
            w.write_byte(0x15)
            w.write_uint32(0x11111111)
        elif is_quest:
            w.write_byte(0x00)
        else:
            scale_mod = item.scale_mod or rarity_helper.get_random_scale_mod(item.rarity)
            self._write_item_mod_children(w, item, scale_mod)

    @staticmethod
    def _attr_mod_refs(item: MerchantItem) -> List[str]:
        """Attribute mods to emit as children (Intellect, elemental, etc.). The
        level-banded LevelPrefix mod is dropped — it would name the wrong tier;
        the wire ``level`` byte carries the item level instead."""
        return [m for m in item.mod_refs
                if "levelprefixmodpal" not in m.lower()]

    @staticmethod
    def _write_attr_mod_body(w: LEWriter) -> None:
        """``ItemAttributeModifier::readData`` body (client @0x00588AE0,
        Ghidra-decoded 2026-06-20): ``flags:u8`` then ``u8`` if ``flags&1`` then
        ``u32`` if ``flags&2``. ``flags=0x03`` -> the proven 6-byte ScaleMod body
        ``0x03 0x15 <u32>``. The modifier's actual effect comes from its GC
        definition (the wire value is content-lenient), so EVERY modifier child —
        ScaleMod and every attribute mod — uses this identical body."""
        w.write_byte(0x03)                              # flags: u8 + u32 present
        w.write_byte(0x15)                              # u8 sub-field
        w.write_uint32(0x11111111)                      # u32 sub-field

    def _write_item_mod_children(self, w: LEWriter, item: MerchantItem,
                                 scale_mod: str) -> None:
        """ItemModifier child list: ``[childCount]`` then N attribute mods, then
        the ScaleMod, all under ONE count (each is an ItemAttributeModifier).

        Per child: ``readType`` (attribute mods by-hash ``0x04 <u32 djb2(name)>``,
        which the client resolves the same way it already did before — a live
        trace had parsed "+1 Endurance" fine; ScaleMod stays by-name ``0xFF
        <cstring>``) followed by the fixed 6-byte ``ItemAttributeModifier::
        readData`` body (:meth:`_write_attr_mod_body`). The earlier crashes were a
        WRONG body length (1-byte, then a 14-byte buff-Modifier body), never the
        type resolution.
        """
        attr_refs = self._attr_mod_refs(item)
        w.write_byte(min(0xFF, len(attr_refs) + 1))   # children: attr mods + ScaleMod
        for mod_ref in attr_refs:
            w.write_byte(0x04)                          # by-hash type tag
            w.write_uint32(hash_djb2(mod_ref))          # GCClassRegistry TypeID
            self._write_attr_mod_body(w)
        w.write_byte(0xFF)                              # by-name ScaleMod child
        w.write_cstring(scale_mod)
        self._write_attr_mod_body(w)

    def build_stock_add_packet(self, npc_gc_type: str,
                               merchant_cid: int) -> Optional[bytes]:
        """0x07 [0x35 cid 0x1E invId <item> 0x02 u32(0)]* 0x06 — push the
        current dynamic stock to one client (NPC-component synch trailer is the
        constant 0, matching the C# refresh packets)."""
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            return None
        w = LEWriter()
        w.write_byte(0x07)
        wrote = False
        for tab in sorted(md.dynamic_tabs, key=lambda t: t.inv_id):
            for item in tab.items:
                w.write_byte(0x35)
                w.write_uint16(merchant_cid)
                w.write_byte(0x1E)
                w.write_byte(tab.inv_id)
                self._write_item(w, item)
                w.write_byte(0x02)
                w.write_uint32(0x00000000)
                wrote = True
        w.write_byte(0x06)
        return w.to_array() if wrote else None

    def build_refresh_packet(self, npc_gc_type: str, merchant_cid: int,
                             removed_ids: List[int]) -> Optional[bytes]:
        """Removes for the stale ids, then adds for the fresh stock."""
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            return None
        w = LEWriter()
        w.write_byte(0x07)
        for item_id in dict.fromkeys(removed_ids):
            w.write_byte(0x35)
            w.write_uint16(merchant_cid)
            w.write_byte(0x1F)
            w.write_uint32(item_id)
            w.write_byte(0x02)
            w.write_uint32(0x00000000)
        wrote = bool(removed_ids)
        for tab in sorted(md.dynamic_tabs, key=lambda t: t.inv_id):
            for item in tab.items:
                w.write_byte(0x35)
                w.write_uint16(merchant_cid)
                w.write_byte(0x1E)
                w.write_byte(tab.inv_id)
                self._write_item(w, item)
                w.write_byte(0x02)
                w.write_uint32(0x00000000)
                wrote = True
        w.write_byte(0x06)
        return w.to_array() if wrote else None

    # ── Per-connection refresh scheduling (client 0x22 boundary flow) ────────

    def arm_refresh(self, conn: "RRConnection", npc_gc_type: str,
                    merchant_cid: int) -> None:
        """Arm the per-connection restock push — called when the player
        activates (clicks) a merchant NPC. The client's own countdown emits the
        empty UnitContainer 0x22 at expiry; we flush then."""
        md = self.get_by_npc(npc_gc_type)
        if md is None or not md.dynamic_tabs:
            return
        ticks = self._ticks_until_regen(md, md.dynamic_tabs[0])
        due = time.monotonic() + ticks / REFRESH_TICKS_PER_SECOND + REFRESH_ADD_DELAY_SECONDS
        conn.active_merchant_npc = npc_gc_type
        conn.active_merchant_cid = merchant_cid
        conn.active_merchant_due = due
        log.info(f"[Merchant] armed refresh for '{conn.login_name}' on "
                 f"{npc_gc_type} in {due - time.monotonic():.1f}s")

    def on_container_boundary(self, conn: "RRConnection") -> bool:
        """Empty 0x22 on the player's UnitContainer = the client's restock
        timer expired. Regenerate + push removes/adds, then re-arm.

        The client emits empty 0x22 on OTHER container boundaries too (e.g.
        right after a (re)spawn — live-observed 2026-06-10), so this MUST be
        gated on the armed due time like the C# server
        (``ActiveMerchantRefreshDueUtc <= now``); flushing early restocked a
        288s timer after 22s and crash-looped the respawning client.
        """
        npc_gc_type = getattr(conn, "active_merchant_npc", None)
        merchant_cid = getattr(conn, "active_merchant_cid", 0)
        if not npc_gc_type or not merchant_cid:
            return False
        # Never push a refresh stream to a client that is loading / not flush-
        # ready (e.g. mid-respawn) — that desyncs the entity stream (Code 3).
        if not getattr(conn, "is_spawned", True) or not getattr(conn, "allow_flush", True):
            return False
        due = getattr(conn, "active_merchant_due", 0.0)
        if time.monotonic() < due:
            log.debug(f"[Merchant] premature container boundary for "
                      f"'{conn.login_name}' ignored ({due - time.monotonic():.1f}s early)")
            return False
        return self._do_refresh(conn, npc_gc_type, merchant_cid)

    def flush_due_refresh(self, conn: "RRConnection") -> bool:
        """Tick-driven restock (port of C# FlushClientMerchantRefreshes).

        The client's own countdown (the reset-ticks baked into the cached NPC
        stream) and our armed due time drift apart, so its empty 0x22 can land
        BEFORE the server's due — the boundary handler rightly ignores it, but
        nothing retried afterwards and the shop stayed empty until the zone
        instance was torn down. C# solves this with a server-driven flush once
        the due time passes; call this from the per-connection tick loop.
        """
        npc_gc_type = getattr(conn, "active_merchant_npc", None)
        merchant_cid = getattr(conn, "active_merchant_cid", 0)
        if not npc_gc_type or not merchant_cid:
            return False
        if not getattr(conn, "is_spawned", True) or not getattr(conn, "allow_flush", True):
            return False
        due = getattr(conn, "active_merchant_due", 0.0)
        if time.monotonic() < due:
            return False
        return self._do_refresh(conn, npc_gc_type, merchant_cid)

    def _do_refresh(self, conn: "RRConnection", npc_gc_type: str,
                    merchant_cid: int) -> bool:
        """Regenerate the vendor's dynamic tabs + push removes/adds, then re-arm."""
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            conn.active_merchant_npc = None
            return False

        removed = [item.item_id for tab in md.dynamic_tabs for item in tab.items]
        for tab in md.dynamic_tabs:
            self.generate_tab(md, tab, max(1, conn.player_level))
            self._last_regen[f"{md.npc_gc_type}_{tab.inv_id}"] = time.monotonic()

        packet = self.build_refresh_packet(npc_gc_type, merchant_cid, removed)
        if packet:
            conn.send_to_client(packet)
            log.info(f"[Merchant] restocked {npc_gc_type} for "
                     f"'{conn.login_name}' ({len(packet)} bytes)")
        self._track_sent_stock(conn, merchant_cid, md)
        self.arm_refresh(conn, npc_gc_type, merchant_cid)
        return True

    @staticmethod
    def _track_sent_stock(conn: "RRConnection", merchant_cid: int,
                          md: MerchantDef) -> None:
        sent_map = getattr(conn, "merchant_stock_sent", None)
        if sent_map is None:
            sent_map = {}
            conn.merchant_stock_sent = sent_map
        sent_map[merchant_cid] = [i.item_id for t in md.dynamic_tabs
                                  for i in t.items]

    def send_zone_stock(self, server: "GameServer", conn: "RRConnection",
                        merchant_components: List[Tuple[int, str]]) -> None:
        """Push the dynamic stock of every merchant in the zone to a joiner
        (the cached instance stream ships dynamic tabs empty).

        Re-entering the same instance (or any duplicate enter) must not re-add
        item ids the client already holds — when this connection was already
        sent stock for a component, prefix removes for those ids (the proven
        refresh shape) instead of blindly adding."""
        sent_map = getattr(conn, "merchant_stock_sent", None)
        if sent_map is None:
            sent_map = {}
            conn.merchant_stock_sent = sent_map
        for merchant_cid, npc_gc_type in merchant_components:
            self.ensure_inventory_for_level(npc_gc_type,
                                            max(1, conn.player_level))
            md = self.get_by_npc(npc_gc_type)
            if md is None:
                continue
            prior = sent_map.get(merchant_cid)
            if prior:
                packet = self.build_refresh_packet(npc_gc_type, merchant_cid,
                                                   prior)
            else:
                packet = self.build_stock_add_packet(npc_gc_type, merchant_cid)
            if packet:
                conn.send_to_client(packet)
            self._track_sent_stock(conn, merchant_cid, md)

    # ── Buy ──────────────────────────────────────────────────────────────────

    def handle_buy(self, server: "GameServer", conn: "RRConnection",
                   merchant_cid: int, reader: LEReader) -> bool:
        """Inbound 0x35 <merchant cid> 0x1E — buy request."""
        if reader.remaining < 6:
            return False
        reader.read_byte()                       # unknown (tab index?)
        reader.read_byte()
        item_id = reader.read_uint32()

        npc_gc_type = server.merchant_components.get(merchant_cid)
        md = self.get_by_npc(npc_gc_type or "")
        if md is None:
            log.warn(f"[Merchant] buy on unknown component 0x{merchant_cid:04X}")
            return True

        tab, item = self._find_item(md, item_id)
        if item is None:
            log.warn(f"[Merchant] buy: item id={item_id} not found on "
                     f"{md.npc_gc_type}")
            return True

        is_free = self._is_free_player(conn)
        prefix = "free_" if is_free else "member_"
        lower = item.gc_type.lower()
        is_consumable = ("consumable" in lower or "potion" in lower
                         or "townportal" in lower)
        is_quest = "questitem" in lower

        # ── Membership restriction (GC ForceRequiresMembership + rarity gate) ──
        if is_free:
            is_major = "majorhealthpotion" in lower or "majormanapotion" in lower
            member_equip = (not is_consumable and item.rarity in (
                ItemRarity.Rare, ItemRarity.Unique, ItemRarity.Mythic))
            if is_major or member_equip:
                log.info(f"[Merchant] membership required: '{conn.login_name}' "
                         f"x {item.gc_type}")
                # Tell the player, not just the server log. The system-message
                # path renders a private "Announce>" chat line — the same
                # surface @-command feedback uses.
                conn.send_system_message(
                    "A Dungeon Runners membership is required to buy this item.")
                return True

        price = self._buy_price(conn, item, prefix,
                                is_consumable=is_consumable, is_quest=is_quest)

        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return True
        if saved.gold < price:
            log.info(f"[Merchant] '{conn.login_name}' lacks gold "
                     f"({saved.gold} < {price})")
            return True

        # Space check BEFORE deducting gold.
        from ..net import inventory as inv_module
        inv_module._reconcile(conn)
        width, height = self._item_dimensions(item.gc_type)
        bag_gc = self._inventory_gc_type(item.gc_type)
        stackable = is_consumable and not is_quest
        if not self._has_bag_space(conn, bag_gc, width, height,
                                   stackable=stackable, is_free=is_free):
            log.info(f"[Merchant] '{conn.login_name}' bag full for {item.gc_type}")
            return True

        saved.gold -= price
        character_repository.save_character(saved)

        # Dynamic stock: remove the sold item from the tab + tell the client.
        if tab is not None and not tab.static_contents:
            tab.items.remove(item)
            w = LEWriter()
            w.write_byte(0x07)
            w.write_byte(0x35)
            w.write_uint16(merchant_cid)
            w.write_byte(0x1F)
            w.write_uint32(item.item_id)
            w.write_byte(0x02)
            w.write_uint32(0x00000000)
            w.write_byte(0x06)
            conn.send_to_client(w.to_array())

        # Gold delta on the player's UnitContainer (negative AddCurrency 0x20 —
        # 0x21 RemoveCurrency is gated behind a client merchant context).
        if conn.unit_container_id:
            w = LEWriter()
            w.write_byte(0x07)
            w.write_byte(0x35)
            w.write_uint16(conn.unit_container_id)
            w.write_byte(0x20)
            w.write_int32(-price)
            w.write_byte(0x00)                  # CurrencySource
            w.write_uint32(0x00000000)          # entityHandle
            w.write_byte(0x01)                  # notifyFlag
            write_synch(w, synch_hp(conn))
            w.write_byte(0x06)
            conn.send_to_client(w.to_array())

        self._grant_item(server, conn, item, bag_gc, width, height,
                         is_consumable=is_consumable, is_quest=is_quest,
                         is_free=is_free, price=price)
        # Arm the debounced refill so the freed space is re-packed shortly after
        # the player stops buying (a real component cid only — the @buy admin
        # path uses a sentinel cid and has no client component to receive adds).
        if (tab is not None and not tab.static_contents and merchant_cid > 0):
            self.schedule_buy_refill(conn, md.npc_gc_type, merchant_cid)
        log.info(f"[Merchant] '{conn.login_name}' bought {item.gc_type} "
                 f"x{item.quantity} for {price}g from {md.npc_gc_type}")
        return True

    def _find_item(self, md: MerchantDef, item_id: int
                   ) -> Tuple[Optional[MerchantTab], Optional[MerchantItem]]:
        for tab in md.inventories:
            for item in tab.items:
                if item.item_id == item_id:
                    return tab, item
        return None, None

    def _buy_price(self, conn: "RRConnection", item: MerchantItem, prefix: str,
                   *, is_consumable: bool, is_quest: bool) -> int:
        if is_quest:
            return 1                            # GoldValue 0 → client shows 1
        if is_consumable:
            lower = item.gc_type.lower()
            gc_gold = 0.175
            scale_to_level = True
            if "majorhealthpotion" in lower or "majormanapotion" in lower:
                gc_gold = 0.2
            elif "healthpotion" in lower or "manapotion" in lower:
                gc_gold = 0.175
            elif "townportal" in lower:
                gc_gold = 2.0
                scale_to_level = False
            player_level = max(1, conn.player_level)
            effective = max(player_level, 3) if scale_to_level else 1
            gold_per_level = rarity_helper._settings_float(
                "itemGoldValuePerLevel", 50.0, prefix)
            buy_mod = rarity_helper._settings_float(
                "itemBuyValueModifier", 1.0, prefix)
            unit = max(1, int(gold_per_level * effective * gc_gold * buy_mod))
            return unit * max(1, item.quantity)
        # Equipment: the exact display formula with the membership prefix.
        if item.gold_value > 0:
            return rarity_helper.calculate_buy_price(
                item.level, item.rarity, item.gold_value, prefix)
        return max(1, item.price)

    @staticmethod
    def _inventory_gc_type(gc_type: str) -> str:
        """Map merchant GC definition paths to the gc_types the client's
        inventory system uses (port of C# MapConsumableGcType)."""
        bare = item_catalog.normalize_key(gc_type)
        lower = bare.lower()
        # MAJOR potions round-trip: stored as the PAL item, but
        # get_packet_gc_class_for maps them BACK to consumable_major* on the wire,
        # so the client renders "Major Health/Mana Potion" correctly.
        if "consumable_majorhealthpotion" in lower:
            return "potionpal.healthpotion_itempack"
        if "consumable_majormanapotion" in lower:
            return "potionpal.manapotion_itempack"
        # MINOR potions are valid client items on their OWN — the base
        # StartingInventory grants items.consumables.Consumable_MinorHealthPotion
        # directly. The old remap to `potionpal.*_noob` was a C# MapConsumableGcType
        # port that has NO reverse wire mapping, so buying a vendor "Minor Health
        # Potion" landed "Health Potion of the Daring Noobosaur" in the bag (a
        # DISTINCT starter item) — live 2026-07-01. Return the FULL consumable path
        # (normalize_key strips `items.consumables.`, which get_packet_gc_class_for
        # does NOT re-add) so the wire class stays consumable_minor* and the client
        # renders the right item.
        if ("consumable_minorhealthpotion" in lower
                or "consumable_healthpotion" in lower):
            return "items.consumables.Consumable_MinorHealthPotion"
        if ("consumable_minormanapotion" in lower
                or "consumable_manapotion" in lower):
            return "items.consumables.Consumable_MinorManaPotion"
        if "consumable_townportal" in lower:
            return "items.consumables.Consumable_TownPortal"
        return bare

    @staticmethod
    def _max_stack(gc_type: str, is_free: bool) -> int:
        lower = gc_type.lower()
        if "healthpotion_itempack" in lower or "manapotion_itempack" in lower:
            return 10
        if "townportal" in lower:
            return 5
        return 5 if is_free else 10

    def _has_bag_space(self, conn: "RRConnection", bag_gc: str,
                       width: int, height: int, *, stackable: bool,
                       is_free: bool) -> bool:
        from ..net.inventory import _find_free_slot
        model = conn.inv_model
        if stackable:
            max_stack = self._max_stack(bag_gc, is_free)
            lower = bag_gc.lower()
            packet_gc = get_packet_gc_class_for(lower).lower()
            for it in model.main_items():
                if (it.gc_class.lower() in (lower, packet_gc)
                        and it.count < max_stack):
                    return True                  # a partial stack can absorb it
        return _find_free_slot(model, width, height) is not None

    def _grant_item(self, server: "GameServer", conn: "RRConnection",
                    item: MerchantItem, bag_gc: str, width: int, height: int,
                    *, is_consumable: bool, is_quest: bool, is_free: bool,
                    price: int) -> None:
        from ..net import inventory as inv_module
        model = conn.inv_model
        quantity = max(1, item.quantity)
        packet_gc = get_packet_gc_class_for(bag_gc)

        if is_consumable and not is_quest:
            max_stack = self._max_stack(bag_gc, is_free)
            for it in model.main_items():
                if it.gc_class.lower() not in (bag_gc.lower(), packet_gc.lower()):
                    continue
                if it.count >= max_stack:
                    continue
                new_count = min(it.count + quantity, max_stack)
                it.count = new_count
                inv_module._persist(conn)
                w = LEWriter()
                w.write_byte(0x07)
                # 0x22 UpdateQuantity — same proven shape as the loot stack-merge.
                w.write_byte(0x35)
                w.write_uint16(conn.unit_container_id)
                w.write_byte(0x22)
                w.write_uint32(it.slot_id)
                w.write_byte(min(0xFF, new_count))
                write_synch(w, synch_hp(conn))
                w.write_byte(0x06)
                conn.send_to_client(w.to_array())
                return

        slot_xy = inv_module._find_free_slot(model, width, height)
        if slot_xy is None:
            return                               # checked earlier; race-guard
        x, y = slot_xy
        rarity = int(item.rarity) if not (is_consumable or is_quest) else -1
        stored_level = item.level if not (is_consumable or is_quest) else -1
        # Carry the EXACT ScaleMod + attribute mods the shop displayed onto the
        # bought item (C# PresetScaleMod) — re-deriving them after the buy changed
        # the item's stats, and the attribute mods (Intellect etc.) were dropped
        # entirely, so the bagged item lost what the vendor showed.
        scale_mod = item.scale_mod if not (is_consumable or is_quest) else ""
        mod_refs = list(item.mod_refs) if not (is_consumable or is_quest) else []
        new_item = model.add(bag_gc, x, y, count=quantity, rarity=rarity,
                             stored_level=stored_level, buy_price=price,
                             scale_mod=scale_mod, mod_refs=mod_refs)
        inv_module._persist(conn)

        level = item.level if item.level > 0 else 1
        w = LEWriter()
        w.write_byte(0x07)
        inv_module._write_item_added(w, conn, new_item, level)
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())

        if is_quest:
            inv_module._notify_quest_item(server, conn, bag_gc)

    # ── Sell ─────────────────────────────────────────────────────────────────

    def handle_sell(self, server: "GameServer", conn: "RRConnection",
                    merchant_cid: int, reader: LEReader) -> bool:
        """Inbound 0x35 <merchant cid> 0x1F — sell request.

        Two client gestures: cursor sell (item held on the cursor, ack with
        0x29 ClearActiveItem) and Shift+Click sell (item still in a bag slot,
        ack with 0x1F remove of that slot).
        """
        if reader.remaining < 6:
            return False
        reader.read_uint16()                    # entityRef (unused, C# parity)
        item_id = reader.read_uint32()

        from ..net import inventory as inv_module
        inv_module._reconcile(conn)
        model = conn.inv_model

        cursor = model.cursor
        is_shift_click = cursor is None
        if is_shift_click:
            inv_item = model.resolve(item_id)
            if inv_item is None:
                log.warn(f"[Merchant] sell: no item at slot {item_id}")
                return True
            gc_class = inv_item.gc_class
            count = inv_item.count
        else:
            gc_class = cursor.gc_class
            count = cursor.count

        price = self._sell_price(conn, gc_class)

        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return True
        saved.gold += price
        character_repository.save_character(saved)

        if conn.unit_container_id:
            w = LEWriter()
            w.write_byte(0x07)
            if is_shift_click:
                w.write_byte(0x35)
                w.write_uint16(conn.unit_container_id)
                w.write_byte(0x1F)
                w.write_uint32(item_id)
                write_synch(w, synch_hp(conn))
            else:
                w.write_byte(0x35)
                w.write_uint16(conn.unit_container_id)
                w.write_byte(0x29)              # ClearActiveItem (0 bytes)
                write_synch(w, synch_hp(conn))
            w.write_byte(0x35)
            w.write_uint16(conn.unit_container_id)
            w.write_byte(0x20)                  # AddCurrency (10-byte form)
            w.write_uint32(price)
            w.write_byte(0x00)
            w.write_uint32(0x00000000)
            w.write_byte(0x01)
            write_synch(w, synch_hp(conn))
            w.write_byte(0x06)
            conn.send_to_client(w.to_array())

        if is_shift_click:
            model.remove(item_id)
        else:
            model.cursor = None
        inv_module._persist(conn)
        log.info(f"[Merchant] '{conn.login_name}' sold {gc_class} x{count} "
                 f"for {price}g")
        return True

    def _sell_price(self, conn: "RRConnection", gc_class: str) -> int:
        """Client sell price for one item, derived from its GC class alone —
        tier-suffix rarity + PAL item level (mythic exception), exactly like
        the C# HandleSellItem the client's display was TTD-verified against.
        Stored per-instance rarity/level are deliberately NOT consulted."""
        lower = item_catalog.normalize_key(gc_class)
        item_level = max(1, rarity_helper.get_item_level(gc_class))
        rarity = rarity_helper.get_rarity_from_tier(
            rarity_helper.get_tier_from_gc_type(lower))
        gold_value = self._gold_value_for(gc_class)
        is_mythic = rarity_helper.is_mythic_pal_item(lower)
        player_level = max(0, conn.player_level)
        if is_mythic:
            rarity = ItemRarity.Mythic
            if player_level > 0:
                item_level = player_level + 3

        # The client applies the rarity ItemLevelDelta to sell prices too.
        adjusted = rarity_helper.get_equip_required_level(item_level, rarity)
        sell = rarity_helper.calculate_sell_price(
            adjusted, gold_value, rarity, is_mythic, player_level)
        # Exploit guard: never pay more than the matching buy price.
        buy = rarity_helper.calculate_buy_price(adjusted, rarity, gold_value)
        return min(sell, buy)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _is_free_player(self, conn: "RRConnection") -> bool:
        login = (conn.login_name or "").lower()
        if not login:
            return False
        cached = self._is_member_cache.get(login)
        if cached is None:
            try:
                row = db.execute_reader(
                    "SELECT is_member FROM accounts WHERE LOWER(username)=:u",
                    {"u": login}).fetchone()
                cached = bool(row[0]) if row is not None else True
            except Exception:  # noqa: BLE001
                cached = True
            self._is_member_cache[login] = cached
        return not cached

    def reset(self) -> None:
        """Test hook: clear all cached state so the next call reloads."""
        self._merchants.clear()
        self._loaded = False
        self._last_regen.clear()
        self._pool.clear()
        self._mod_counts.clear()
        self._mod_family_index.clear()
        self._mod_index_loaded = False
        self._is_member_cache.clear()

    # ── Legacy chat-command surface (@buy/@sell/@shop admin tooling) ─────────

    def buy_item(self, server: "GameServer", conn: "RRConnection",
                 npc_gc_type: str, item_slot_id: int) -> bool:
        """@buy compat: purchase by slot id through the real buy path."""
        from ..net.chat_commands import _send_chat
        md = self.get_by_npc(npc_gc_type)
        if md is None:
            _send_chat(conn, f"No merchant found for: {npc_gc_type}")
            return False
        _tab, item = self._find_item(md, item_slot_id)
        if item is None:
            _send_chat(conn, f"Item slot #{item_slot_id} not found.")
            return False
        w = LEWriter()
        w.write_byte(0x00)
        w.write_byte(0x00)
        w.write_uint32(item_slot_id)
        # Reuse the real buy path via a temporary component registration.
        fake_cid = -1
        server.merchant_components[fake_cid] = npc_gc_type
        try:
            self.handle_buy(server, conn, fake_cid, LEReader(w.to_array()))
        finally:
            server.merchant_components.pop(fake_cid, None)
        return True

    def sell_item(self, conn: "RRConnection", inv_index: int) -> bool:
        """@sell compat: sell the Nth bag item at the client sell price."""
        from ..net.chat_commands import _send_chat
        from ..net import inventory as inv_module
        inv_module._reconcile(conn)
        items = conn.inv_model.main_items()
        if inv_index < 0 or inv_index >= len(items):
            _send_chat(conn, f"Invalid inventory index: {inv_index}")
            return False
        it = items[inv_index]
        price = self._sell_price(conn, it.gc_class)
        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return False
        saved.gold += price
        character_repository.save_character(saved)
        conn.inv_model.remove(it.slot_id)
        inv_module._persist(conn)
        _send_chat(conn, f"Sold: {it.gc_class} for {price} gold.")
        return True


merchant_manager = MerchantManager()
