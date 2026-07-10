"""Per-connection inventory slot-map + cursor (active item) model.

The client identifies every inventory item by the ``inventorySlot`` uint32 the
server assigned when it serialized that item (``GCObject.WriteInitForInventory``).
On UseItem (0x25) / Pickup (0x28) / Drop (0x23) the client echoes that exact
slot id back — it is **not** a 0-based list index. The original Python port
treated it as ``items[index]`` which is off-by-one (slots are 1-based) and breaks
entirely once items are added/removed mid-session.

This model mirrors the C# server's tracking dictionary (``GetNextInventorySlot``
/ ``GetInventoryItemBySlot``): a monotonic slot allocator, a ``slot_id -> item``
map seeded at spawn, and a single cursor item (C# ``PlayerState.ActiveItem``)
that Pickup fills, Place/Equip/Drop consume. It is the session source of truth;
every mutation is also persisted to SQLite so a reconnect rebuilds the same map.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

INVENTORY_COLS = 10
INVENTORY_ROWS = 8
MAIN_INVENTORY = 0x0B


@dataclass
class CursorItem:
    """The item currently held on the cursor (C# PlayerState.ActiveItem)."""
    gc_class: str
    count: int = 1
    rarity: int = -1
    stored_level: int = -1
    buy_price: int = 0
    scale_mod: str = ""
    mod_refs: List[str] = field(default_factory=list)


@dataclass
class InvItem:
    """One inventory item tracked by its client-facing slot id."""
    slot_id: int
    gc_class: str
    x: int
    y: int
    count: int = 1
    rarity: int = -1
    stored_level: int = -1
    buy_price: int = 0
    scale_mod: str = ""
    mod_refs: List[str] = field(default_factory=list)
    container: int = MAIN_INVENTORY


SizeFn = Callable[[str], "tuple[int, int]"]


class InventoryModel:
    """Session slot-map + cursor. Attached to each connection as ``inv_model``."""

    def __init__(self) -> None:
        self.items: dict[int, InvItem] = {}
        self.cursor: Optional[CursorItem] = None
        self._next_slot: int = 1

    # ── lifecycle ──
    def reset(self) -> None:
        self.items.clear()
        self.cursor = None
        self._next_slot = 1

    def alloc_slot(self) -> int:
        slot = self._next_slot
        self._next_slot += 1
        return slot

    def load(self, rows: list[dict], container: int = MAIN_INVENTORY) -> list[InvItem]:
        """Seed the map from DB rows, assigning fresh slot ids in order.

        Returns the ordered list so the spawn packet can serialize each item with
        the very slot id the client will later echo back.
        """
        ordered: list[InvItem] = []
        for r in rows:
            item = InvItem(
                slot_id=self.alloc_slot(),
                gc_class=r["gc_class"], x=r["x"], y=r["y"],
                count=r.get("count", 1), rarity=r.get("rarity", -1),
                stored_level=r.get("stored_level", -1),
                buy_price=r.get("buy_price", 0),
                scale_mod=r.get("scale_mod", ""),
                mod_refs=list(r.get("mod_refs") or []), container=container,
            )
            self.items[item.slot_id] = item
            ordered.append(item)
        return ordered

    # ── lookup ──
    def by_slot(self, slot_id: int) -> Optional[InvItem]:
        return self.items.get(slot_id)

    def resolve(self, client_id: int) -> Optional[InvItem]:
        """Map a client-sent item id to a tracked item.

        Direct slot lookup is the common (bag-click) case. The hotbar can send a
        value offset from the server slots; fall back to a 0-based index into the
        sorted slot list, matching the C# heuristic.
        """
        item = self.items.get(client_id)
        if item is not None:
            return item
        if not self.items:
            return None
        ordered = [self.items[k] for k in sorted(self.items)]
        if 0 <= client_id < len(ordered):
            return ordered[client_id]
        return None

    def by_grid(self, x: int, y: int, size_fn: SizeFn,
                container: int = MAIN_INVENTORY) -> Optional[InvItem]:
        for item in self.items.values():
            if item.container != container:
                continue
            w, h = size_fn(item.gc_class)
            if item.x <= x < item.x + w and item.y <= y < item.y + h:
                return item
        return None

    # ── mutation ──
    def remove(self, slot_id: int) -> Optional[InvItem]:
        return self.items.pop(slot_id, None)

    def add(self, gc_class: str, x: int, y: int, *, count: int = 1, rarity: int = -1,
            stored_level: int = -1, buy_price: int = 0, scale_mod: str = "",
            mod_refs: Optional[List[str]] = None,
            container: int = MAIN_INVENTORY) -> InvItem:
        item = InvItem(
            slot_id=self.alloc_slot(), gc_class=gc_class, x=x, y=y, count=count,
            rarity=rarity, stored_level=stored_level, buy_price=buy_price,
            scale_mod=scale_mod, mod_refs=list(mod_refs or []), container=container,
        )
        self.items[item.slot_id] = item
        return item

    def occupied(self, x: int, y: int, w: int, h: int, size_fn: SizeFn,
                 container: int = MAIN_INVENTORY,
                 ignore_slot: Optional[int] = None) -> bool:
        for item in self.items.values():
            if item.container != container or item.slot_id == ignore_slot:
                continue
            iw, ih = size_fn(item.gc_class)
            if (item.x < x + w and item.x + iw > x and
                    item.y < y + h and item.y + ih > y):
                return True
        return False

    # ── persistence ──
    def main_items(self) -> list[InvItem]:
        return [it for it in self.items.values() if it.container == MAIN_INVENTORY]

    def to_saved(self) -> list[dict]:
        """Serialize the main inventory back to the DB row shape."""
        return [
            {
                "gc_class": it.gc_class, "x": it.x, "y": it.y, "count": it.count,
                "rarity": it.rarity, "stored_level": it.stored_level,
                "buy_price": it.buy_price, "scale_mod": it.scale_mod,
                "mod_refs": list(it.mod_refs),
            }
            for it in self.main_items()
        ]
