"""Client-faithful item attribute catalog — buy/sell price + inventory size.

Single source for merchant pricing and inventory-grid sizing, sourced from the
rebuilt ``items`` / ``weapons`` / ``armor`` content tables (which use bare,
client-valid PAL keys, e.g. ``1haxe1pal.1haxe1-1``).

This replaces the legacy ``sellable_items`` and ``item_dimensions`` tables.
Those carried the older emulator's fabricated ``items.pal.<numberedPAL>``
namespace — a hybrid key that existed in *neither* the client ``GCDictionary``
nor the rebuilt content tables — so they no longer joined to ``items`` after the
content rebuild. The price/size data they held is fully covered by the rebuilt
tables: ``weapons``/``armor`` carry the verbatim tier-scaled ``gold_value`` and
``items`` carries ``inventory_width``/``inventory_height``.

Lookups are namespace-tolerant, mirroring C# ``MerchantManager``: the gc_type is
lowercased, the legacy ``items.pal.`` / ``items.consumables.`` prefixes are
stripped, and an ``endswith`` suffix fallback bridges any residual gap.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from ..core import log
from ..db import game_database as db

# Buy price for items we have no recorded value for (matches the legacy default).
DEFAULT_PRICE = 10
# Sell-back fraction of buy price (C# / prior Python behaviour).
SELL_FRACTION = 0.25
# Legacy namespace prefixes the client never used on bare PAL keys.
_LEGACY_PREFIXES = ("items.pal.", "items.consumables.")

# norm gc_type -> tier-scaled gold_value (weapons/armor only carry this column)
_prices: Dict[str, float] = {}
# norm gc_type -> (width, height)
_dims: Dict[str, Tuple[int, int]] = {}
_loaded = False


def normalize_key(gc_type: Optional[str]) -> str:
    """Lowercase a gc_type and strip the legacy ``items.pal.`` / ``items.consumables.`` prefix."""
    key = (gc_type or "").lower()
    for prefix in _LEGACY_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def _load() -> None:
    global _loaded
    if _loaded:
        return
    try:
        # Inventory dimensions live on the items superset.
        for row in db.execute_reader(
            "SELECT gc_type, inventory_width, inventory_height FROM items"
        ).fetchall():
            key = normalize_key(db.get_string(row, "gc_type"))
            if key:
                _dims[key] = (
                    db.get_int(row, "inventory_width", 1),
                    db.get_int(row, "inventory_height", 1),
                )
        # Tier-scaled buy prices live on the typed subsets (verbatim from the emulator).
        for table in ("weapons", "armor"):
            for row in db.execute_reader(
                f"SELECT gc_type, gold_value FROM {table}"
            ).fetchall():
                key = normalize_key(db.get_string(row, "gc_type"))
                value = db.get_float(row, "gold_value", 0.0)
                if key and value > 0:
                    _prices[key] = value
        _loaded = True
        log.info(f"[ItemCatalog] loaded {len(_dims)} dimensions, {len(_prices)} prices")
    except Exception as ex:  # noqa: BLE001 — content DB may be absent in some contexts
        log.error(f"[ItemCatalog] load error: {ex}")


def _tolerant_lookup(table: Dict, key: str):
    """Exact match first, then a C#-style suffix (endswith) fallback either direction."""
    if key in table:
        return table[key]
    if not key:
        return None
    for stored_key, value in table.items():
        if stored_key.endswith(key) or key.endswith(stored_key):
            return value
    return None


def get_buy_price(gc_type: Optional[str]) -> int:
    """Merchant buy price for a gc_type, or ``DEFAULT_PRICE`` when unknown."""
    _load()
    value = _tolerant_lookup(_prices, normalize_key(gc_type))
    if value and value > 0:
        return int(value)
    return DEFAULT_PRICE


def get_sell_price(gc_type: Optional[str]) -> int:
    """Sell-back price = ``SELL_FRACTION`` of buy price, at least 1 gold."""
    return max(1, int(get_buy_price(gc_type) * SELL_FRACTION))


def get_item_size(gc_type: Optional[str]) -> Tuple[int, int]:
    """Inventory grid (width, height) for a gc_type, defaulting to ``(1, 1)``."""
    _load()
    return _tolerant_lookup(_dims, normalize_key(gc_type)) or (1, 1)


def reset() -> None:
    """Clear caches (test hook; forces a reload on next access)."""
    global _loaded
    _prices.clear()
    _dims.clear()
    _loaded = False
