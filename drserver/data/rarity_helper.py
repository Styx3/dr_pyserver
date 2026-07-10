"""RarityHelper — item rarity classification and tier/lookup logic.

Ported from C# RarityHelper (MerchantManager.cs:2362-2717) and ItemRarity enum.
All methods are exact ports matching the C# behaviour.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Dict, Optional


class ItemRarity(IntEnum):
    """Item quality tier (wire protocol int value)."""
    Normal = 0
    Superior = 1
    Magical = 2
    Rare = 3
    Unique = 4
    Mythic = 5


# ── Level deltas (from GlobalKnobs.gc) ──
# These are the level adjustment applied to items relative to the player.
_RARITY_LEVEL_DELTA: Dict[ItemRarity, int] = {
    ItemRarity.Normal: -12,
    ItemRarity.Superior: -10,
    ItemRarity.Magical: -7,
    ItemRarity.Rare: -5,
    ItemRarity.Unique: -2,
    ItemRarity.Mythic: 3,
}


# ── Quality price modifiers (Fixed32: value * 256) ──
_RARITY_QUALITY_MODIFIER_FIXED32: Dict[ItemRarity, int] = {
    ItemRarity.Normal: 67,
    ItemRarity.Superior: 144,
    ItemRarity.Magical: 274,
    ItemRarity.Rare: 520,
    ItemRarity.Unique: 1170,
    ItemRarity.Mythic: 9961,
}

# ScaleMod pools by rarity (random string on equip).
_SCALE_MODS: Dict[ItemRarity, tuple[str, ...]] = {
    ItemRarity.Normal: ("ScaleModPAL.Binder.Mod1",),
    ItemRarity.Superior: (
        "ScaleModPAL.Superior.Mod1", "ScaleModPAL.Superior.Mod2", "ScaleModPAL.Superior.Mod3",
    ),
    ItemRarity.Magical: tuple(f"ScaleModPAL.Magic.Mod{i}" for i in range(1, 7)),   # Mod1..Mod6
    ItemRarity.Rare: tuple(f"ScaleModPAL.Rare.Mod{i}" for i in range(1, 6)),       # Mod1..Mod5
    ItemRarity.Unique: tuple(f"ScaleModPAL.Unique.Mod{i}" for i in range(0, 9)),    # Mod0..Mod8
    ItemRarity.Mythic: tuple(f"ScaleModPAL.Rare.Mod{i}" for i in range(1, 6)),
}


# ── Public helper functions (exact ports of C# RarityHelper static methods) ──

def get_tier_from_gc_type(gc_type: Optional[str]) -> int:
    """Extract dash-suffix (-N) from a GC class name, e.g. ``1HAxePAL-3`` → 3."""
    if not gc_type:
        return 1
    dash_idx = gc_type.rfind("-")
    if dash_idx > 0 and dash_idx < len(gc_type) - 1:
        try:
            return int(gc_type[dash_idx + 1:])
        except ValueError:
            pass
    return 1


def get_rarity_from_tier(tier: int) -> ItemRarity:
    """Map a tier suffix (1-5) to the corresponding ItemRarity."""
    mapping = {1: ItemRarity.Normal, 2: ItemRarity.Superior,
               3: ItemRarity.Magical, 4: ItemRarity.Rare, 5: ItemRarity.Unique}
    return mapping.get(tier, ItemRarity.Rare)


def is_mythic_pal_item(gc_type: Optional[str]) -> bool:
    """Return True if the GC type is a MythicPAL item (no dash suffix)."""
    if not gc_type:
        return False
    return "mythicpal" in gc_type.lower()


def get_item_level(gc_type: Optional[str]) -> int:
    """Compute the item level from its PAL tier number in the GC class name.

    Finds the digits immediately before the last ``PAL`` substring.
    e.g. ``1HAxe2PAL`` → palTier=2 → itemLevel = (2-1)*10 + 1 = 11
    """
    if not gc_type:
        return 1
    pal_idx = gc_type.lower().rfind("pal")
    if pal_idx < 1:
        return 1
    # Scan backwards from pal_idx to find the start of the tier digits.
    num_start = pal_idx - 1
    while num_start >= 0 and gc_type[num_start].isdigit():
        num_start -= 1
    num_start += 1
    if num_start >= pal_idx:
        return 1
    try:
        pal_tier = int(gc_type[num_start:pal_idx])
    except ValueError:
        return 1
    if pal_tier <= 1:
        return 1
    return (pal_tier - 1) * 10 + 1


def get_required_level_from_gc_class(gc_type: Optional[str]) -> int:
    """Compute the minimum required level to equip an item.

    itemLevel = get_item_level(gc_type) + level_delta for the item's rarity.
    Clamped to >= 1.
    """
    item_level = get_item_level(gc_type)
    rarity = get_rarity_from_tier(get_tier_from_gc_type(gc_type))
    delta = _RARITY_LEVEL_DELTA.get(rarity, -12)
    return max(1, item_level + delta)


def get_level_delta(rarity: ItemRarity) -> int:
    """Return the level delta for a rarity (from GlobalKnobs.gc)."""
    return _RARITY_LEVEL_DELTA.get(rarity, -12)


def get_quality_modifier_fixed32(rarity: ItemRarity) -> int:
    """Return the Fixed32 quality price modifier for a rarity."""
    return _RARITY_QUALITY_MODIFIER_FIXED32.get(rarity, 67)


def scale_mods_for(item_rarity: ItemRarity) -> tuple[str, ...]:
    """The ScaleMod pool for a rarity (so callers can pick with their own RNG)."""
    return _SCALE_MODS.get(item_rarity) or ("ScaleModPAL.Binder.Mod1",)


def get_random_scale_mod(item_rarity: ItemRarity) -> str:
    """Pick a random ScaleMod string for the given rarity.

    In C# this uses UnityEngine.Random; here we use Python's random.
    """
    import random
    mods = _SCALE_MODS.get(item_rarity)
    if not mods:
        return "ScaleModPAL.Binder.Mod1"
    return random.choice(mods)


def get_deterministic_scale_mod(gc_class: Optional[str], rarity: ItemRarity) -> str:
    """Deterministic per-gcClass ScaleMod pick (djb2 of the lowered name).

    Port of C# ``RarityHelper.GetDeterministicScaleMod``: write paths that fire
    on every zone-load/relog must hand the client the SAME mod each time or the
    item's stats change on relog.
    """
    mods = _SCALE_MODS.get(rarity)
    if not mods:
        return "ScaleModPAL.Rare.Mod1"
    h = 5381
    for ch in (gc_class or "").lower():
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return mods[h % len(mods)]


# ── Price formulas (exact Fixed32 ports of the client binary) ───────────────
#
# All knobs are overridable via the server_settings overlay using the same keys
# the C# server used (itemGoldValuePerLevel, itemPriceModifier<Rarity>,
# itemLevelDelta<Rarity>, itemBuyValueModifier, free_/member_ prefixed variants).
# Defaults are the binary-exact GlobalKnobs.gc values.

_RARITY_KEY = {
    ItemRarity.Normal: "Normal", ItemRarity.Superior: "Superior",
    ItemRarity.Magical: "Magical", ItemRarity.Rare: "Rare",
    ItemRarity.Unique: "Unique", ItemRarity.Mythic: "Mythic",
}

# Merchant::getSellValue multiplier — RPGSettings[0xB4] = 0x34 = 52 (≈0.203).
# TTD-verified: the client stores round_up(0.20 * 256) = 52, NOT round(51.2).
_SELL_MOD_FIXED32 = 52


def _settings_float(key: str, default: float, prefix: str = "") -> float:
    """server_settings lookup with optional free_/member_ prefix fallback."""
    from ..core import settings
    if prefix:
        value = settings.get_float(prefix + key, -1.0)
        if value >= 0:
            return value
    value = settings.get_float(key, -1.0)
    return value if value >= 0 else default


def get_equip_required_level(item_level: int, rarity: ItemRarity) -> int:
    """Equip level requirement = itemLevel + levelDelta[rarity], min 1."""
    delta = int(_settings_float(f"itemLevelDelta{_RARITY_KEY[rarity]}",
                                float(_RARITY_LEVEL_DELTA.get(rarity, -12))))
    return max(1, item_level + delta)


def get_base_gold_value(gc_type: Optional[str]) -> float:
    """Name-based GC GoldValue fallback (port of C# GetBaseGoldValue).

    Used when the content tables carry no ``gc_gold_value`` for an item. The
    staff check must run before the 2H check: 2H staves inherit BasePoleArm
    (GoldValue 1.0), not Base2HMelee (2.0) — TTD-verified.
    """
    if not gc_type:
        return 1.0
    lower = gc_type.lower()
    if "armor" in lower or "robe" in lower:
        return 4.0
    if "shoulder" in lower or "pauldron" in lower:
        return 2.0
    if "shield" in lower or "buckler" in lower:
        return 2.5
    if "staff" in lower:
        return 1.0
    if "2h" in lower or "cannon" in lower or "crossbow" in lower or "rifle" in lower:
        return 2.0
    if "helm" in lower or "hat" in lower or "hood" in lower or "cap" in lower:
        return 1.5
    if "boot" in lower or "shoe" in lower or "greave" in lower:
        return 1.25
    if "potion" in lower:
        return 0.2
    return 1.0


def _quality_mod_fixed32(rarity: ItemRarity, prefix: str = "") -> int:
    value = _settings_float(f"itemPriceModifier{_RARITY_KEY[rarity]}", -1.0, prefix)
    if value >= 0:
        return int(round(value * 256.0))
    return _RARITY_QUALITY_MODIFIER_FIXED32.get(rarity, 256)


def calculate_buy_price(level: int, rarity: ItemRarity, gold_value: float,
                        prefix: str = "") -> int:
    """Merchant BUY price (port of C# CalculatePriceWithGoldValue).

    Formula: goldPerLevel * adjustedLevel * goldValue * qualityMod * buyMod,
    where adjustedLevel = max(1, level + levelDelta[rarity]). All multiplies in
    Fixed32 (Q8.8) like the client.
    """
    adjusted_level = get_equip_required_level(level, rarity)
    gold_per_level = int(_settings_float("itemGoldValuePerLevel", 50.0, prefix))
    quality_fixed32 = _quality_mod_fixed32(rarity, prefix)
    buy_mod_fixed32 = int(round(_settings_float("itemBuyValueModifier", 1.0, prefix) * 256))
    gold_value_fixed32 = int(round(gold_value * 256))

    numerator = gold_per_level * adjusted_level * gold_value_fixed32 * quality_fixed32
    price = numerator // 65536
    price = (price * buy_mod_fixed32) // 256
    return max(1, int(price))


def calculate_sell_price(level: int, gold_value: float,
                         rarity: ItemRarity = ItemRarity.Normal,
                         is_mythic_pal: bool = False,
                         player_level: int = 0) -> int:
    """Merchant SELL price — exact match to the client binary
    ``Merchant::getSellValue`` (0x59B700) + ``Item::getValue`` (0x580BE0).

    Port of C# CalculateSellPrice (TTD-derived):
      1. cappedLevel = min(itemLevel, playerLevel + 5)
      2. getValue: Fixed32 chain with an integer truncation (sar eax,8)
      3. getSellValue: value << 8, * sellMod(52), >> 8, >> 8
    The truncate-then-shift precision loss is deliberate — it is what the
    client computes and displays.
    """
    if level < 1:
        level = 1

    sell_level = level
    if player_level > 0:
        sell_level = min(sell_level, player_level + 5)
    # MythicPAL: the client computes a very high level from GC, always capped.
    if is_mythic_pal and player_level > 0:
        sell_level = player_level + 5

    gold_per_level = int(_settings_float("itemGoldValuePerLevel", 50.0))
    quality_fixed32 = _quality_mod_fixed32(rarity)
    gold_value_fixed32 = int(round(gold_value * 256))

    # Item::getValue — exact Fixed32 chain.
    step1 = (gold_per_level * 256 * sell_level * 256) // 256
    modified_gv = (gold_value_fixed32 * quality_fixed32) // 256
    step2 = (step1 * modified_gv) // 256
    get_value = step2 >> 8                       # sar eax,8 — truncates
    if get_value < 1:
        get_value = 1

    # Merchant::getSellValue — shl 8 back (precision loss), * sellMod, sar 8.
    value_q8 = get_value * 256
    sell_q8 = (value_q8 * _SELL_MOD_FIXED32) // 256
    return max(1, sell_q8 >> 8)
