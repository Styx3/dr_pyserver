"""ItemStatDatabase — equipment stat bonus resolution.

Ported from C# Data/ItemStatDatabase.cs. Uses the ``stat_pools`` and
``item_resolved_mods`` tables (pre-computed and shipped in the SQLite DB) to
resolve item-level stat bonuses for any equipped item.

Stat pool formula:  base + (itemLevel - 1) * scale / divisor
Mod bonus:           poolValue * valueMult  (truncated to int)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from ..core import log
from ..db import game_database as db


@dataclass
class StatPool:
    pool_name: str
    base: float
    scale: float
    divisor: float

    def compute(self, item_level: int) -> float:
        """Pool value at a given item level."""
        return self.base + (item_level - 1) * self.scale / max(self.divisor, 1.0)


@dataclass
class ItemMod:
    mod_slot: int       # 0-based mod slot index on the item
    attribute: str      # attribute name (e.g. "ENDURANCE", "FIRE_DAMAGE_BONUS")
    pool_name: str      # stat pool key
    value_mult: float   # multiplier against pool value

    def resolve(self, pools: Dict[str, StatPool], item_level: int) -> int:
        """Compute the stat bonus for this mod at a given item level."""
        pool = pools.get(self.pool_name)
        if pool is None:
            return 0
        pool_value = pool.compute(item_level)
        return int(pool_value * self.value_mult)


class ItemStatDatabase:
    """Resolves item stat bonuses from pre-computed SQLite data."""

    def __init__(self):
        self._pools: Dict[str, StatPool] = {}
        self._mods_by_item: Dict[str, List[ItemMod]] = {}   # lower(gc_class) -> mods
        self._mod_count_by_item: Dict[str, int] = {}        # lower(gc_type) -> mod_count
        self._loaded = False

    def load(self) -> None:
        """Load stat pools and resolved mods from SQLite."""
        if self._loaded:
            return
        self._pools.clear()
        self._mods_by_item.clear()

        try:
            # Load stat pools.
            for row in db.execute_reader("SELECT * FROM stat_pools").fetchall():
                self._pools[row["pool_name"]] = StatPool(
                    pool_name=row["pool_name"],
                    base=float(row["base_value"] or 0),
                    scale=float(row["scale"] or 0),
                    divisor=float(row["divisor"] or 1),
                )

            # Load item mods, indexed by lowercased GC class.
            for row in db.execute_reader("SELECT * FROM item_resolved_mods").fetchall():
                gc_key = (row["full_gc_key"] or "").lower()
                if not gc_key:
                    continue
                mod = ItemMod(
                    mod_slot=row["mod_slot"] or 0,
                    attribute=row["attribute"] or "",
                    pool_name=row["pool_name"] or "",
                    value_mult=float(row["value_mult"] or 0),
                )
                if gc_key not in self._mods_by_item:
                    self._mods_by_item[gc_key] = []
                self._mods_by_item[gc_key].append(mod)

            # Per-item mod_count (C# ItemData.modCount) from the item tables.
            # This is the authoritative slot count for normal (non-mythic)
            # gear — item_resolved_mods only covers mythic items.
            for table in ("weapons", "armor", "items"):
                for row in db.execute_reader(
                        f"SELECT gc_type, mod_count FROM {table}").fetchall():
                    gc_key = (row["gc_type"] or "").lower()
                    if not gc_key or row["mod_count"] is None:
                        continue
                    # First table to define a gc_type wins; don't clobber.
                    self._mod_count_by_item.setdefault(gc_key, int(row["mod_count"]))

            self._loaded = True
            log.info(f"[ItemStatDB] loaded {len(self._pools)} pools, "
                     f"{len(self._mods_by_item)} item types with mods, "
                     f"{len(self._mod_count_by_item)} item mod-counts")
        except Exception as ex:
            log.error(f"[ItemStatDB] load error: {ex}")

    def get_mod_count(self, gc_class: str) -> int:
        """Return the number of modifier slots for an item by GC class name.

        Used by GCObject._get_mod_count() for WriteInit serialization.
        """
        key = gc_class.lower()
        # Also try without the items.pal. prefix.
        if key.startswith("items.pal."):
            short = key[len("items.pal."):]
        else:
            short = key
        mods = self._mods_by_item.get(key) or self._mods_by_item.get(short)
        if mods:
            # Dedupe by mod_slot.
            slots = {m.mod_slot for m in mods}
            return len(slots)
        # Fall back to per-item mod_count from the item tables (C# REGULAR
        # path: itemData.modCount, with the chain-armor exception → 1).
        count = self._mod_count_by_item.get(key)
        if count is None:
            count = self._mod_count_by_item.get(short)
        if count is not None:
            if "chain" in short and "shield" not in short:
                return 1
            return count
        return 0

    def get_item_stats(self, gc_class: str, item_level: int,
                        slot_divisor: int = 8) -> Dict[str, int]:
        """Compute stat bonuses for an item at a given level.

        Args:
            gc_class: The item's GC class name (wire format, e.g. "items.pal.1haxepal.normal001")
            item_level: The effective item level
            slot_divisor: Divisor applied to each bonus (default 8 = no split;
                          equipment pieces use their slot count)

        Returns:
            Dict of attribute_name -> bonus_value (integer)
        """
        if not self._loaded:
            self.load()

        key = gc_class.lower()
        if key.startswith("items.pal."):
            short = key[len("items.pal."):]
        else:
            short = key

        mods = self._mods_by_item.get(key) or self._mods_by_item.get(short)
        if not mods:
            return {}

        result: Dict[str, int] = {}
        for mod in mods:
            bonus = mod.resolve(self._pools, item_level) / max(slot_divisor, 1)
            attr = mod.attribute
            result[attr] = result.get(attr, 0) + int(bonus)
        return result

    def get_cumulative_stats(self, equipped: Dict[int, str], player_level: int) -> Dict[str, int]:
        """Compute cumulative stat bonuses from all equipped items.

        Args:
            equipped: Dict of slot_number -> gc_class
            player_level: Player's level (used to derive item effective level)

        Returns:
            Dict of attribute_name -> total_bonus
        """
        result: Dict[str, int] = {}
        for slot, gc_class in equipped.items():
            if not gc_class:
                continue
            from .rarity_helper import get_item_level, get_required_level_from_gc_class
            item_level = get_item_level(gc_class)
            stats = self.get_item_stats(gc_class, item_level)
            for attr, bonus in stats.items():
                result[attr] = result.get(attr, 0) + bonus
        return result


# Module-level singleton.
item_stat_database = ItemStatDatabase()
