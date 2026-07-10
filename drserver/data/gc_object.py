"""GCObject — Game Content Object, DFC wire format.

Ported from C# Data/GCObject.cs. A GCObject is a tree of typed properties +
child objects, serialized to the client in DFC format (version 0x2D) using djb2
hashes of class/property names. Two property types exist: string and uint32.

The five WriteInit* item-serialization methods are the wire-format serializers
for all item types (equipment, consumables, rings/amulets, inventory drops).
Phase 6 — ported from C# verified-by-PDB packet captures.
"""
from __future__ import annotations

import json
from typing import List, Optional

from ..util.byte_io import LEWriter

DFC_VERSION = 0x2D  # version 45 — must match the client

# Namespaces whose classes live in items.pal.* in the original DR hierarchy; the
# client registry knows these by fully-qualified name. Every other namespace is
# referenced bare. (Built by scanning all .gc files; see C# comment.)
_ITEMS_PAL_NAMESPACES = {
    "1haxepal", "1hmacepal", "1hstaffpal", "1hswordpal",
    "fighterbodypal", "fighterbootspal", "fighterglovespal", "fighterhelmpal",
    "fightershieldpal", "fightershoulderspal",
    "itempackpal", "itempackvisuals",
    "magebodypal", "magebootspal", "mageglovespal", "magehelmpal",
    "mageshieldpal", "mageshoulderspal",
    "rangerbodypal", "rangerbootspal", "rangerglovespal", "rangerhelmpal",
    "rangershoulderspal",
    "shieldvisuals", "voucherpal",
}


def hash_djb2(s: Optional[str]) -> int:
    """djb2 hash over the lowercased string (matches GCObject.HashDjb2)."""
    if not s:
        return 5381
    h = 5381
    for c in s.lower():
        h = (((h << 5) + h) + ord(c)) & 0xFFFFFFFF
    return h


def get_packet_gc_class_for(gc_class: Optional[str]) -> str:
    """Return the GCClass form the client expects on the wire.

    Single source of truth for the items.pal.* namespace prefixing + potion remap.
    """
    if not gc_class:
        return gc_class or ""
    lower = gc_class.lower()
    if lower == "potionpal.healthpotion_itempack":
        return "items.consumables.consumable_majorhealthpotion"
    if lower == "potionpal.manapotion_itempack":
        return "items.consumables.consumable_majormanapotion"
    if lower.startswith("items.pal."):
        return lower
    dot = lower.find(".")
    if dot > 0 and lower[:dot] in _ITEMS_PAL_NAMESPACES:
        return "items.pal." + lower
    return lower


def write_gc_type(writer: "LEWriter", type_name: str, preserve_case: bool = False) -> None:
    """Entity-creation GC type tag: 0xFF + cstring(type_name).

    Lowercases ``type_name`` unless ``preserve_case`` is set (WriteGCType in C#).
    """
    safe = type_name if preserve_case else type_name.lower()
    writer.write_byte(0xFF)
    writer.write_cstring(safe)


def detect_rarity_from_gc_class(gc_class: Optional[str]) -> int:
    """Fallback rarity detection from name patterns (ItemRarity enum int)."""
    if not gc_class:
        return 0
    lower = gc_class.lower()
    if "mythicpal" in lower or "mythic" in lower:
        return 5  # Mythic
    if "unique" in lower:
        return 4
    if "rare" in lower:
        return 3
    if "magical" in lower or "magic" in lower:
        return 2
    if "superior" in lower:
        return 1
    return 0  # Normal


class GCObjectProperty:
    def __init__(self, name: str = ""):
        self.name = name

    def write_dfc(self, writer: LEWriter) -> None:
        raise NotImplementedError

    def serialize(self) -> bytes:
        """Legacy non-DFC serialization (name + null + type byte + value)."""
        raise NotImplementedError


class StringProperty(GCObjectProperty):
    def __init__(self, name: str = "", value: str = ""):
        super().__init__(name)
        self.value = value

    def write_dfc(self, writer: LEWriter) -> None:
        writer.write_uint32(hash_djb2(self.name))
        writer.write_cstring(self.value)

    def serialize(self) -> bytes:
        return self.name.encode("utf-8") + b"\x00\x01" + self.value.encode("utf-8") + b"\x00"


class UInt32Property(GCObjectProperty):
    def __init__(self, name: str = "", value: int = 0):
        super().__init__(name)
        self.value = value

    def write_dfc(self, writer: LEWriter) -> None:
        writer.write_uint32(hash_djb2(self.name))
        writer.write_uint32(self.value)

    def serialize(self) -> bytes:
        out = LEWriter()
        out.write_bytes(self.name.encode("utf-8"))
        out.write_byte(0)
        out.write_byte(2)
        out.write_uint32(self.value)
        return out.to_array()


# ── Baked-mod "special" items (Mythic / Prebuilt / Generated / …) ──────────────
#
# These items carry their modifiers BAKED INTO the client's own GC definition
# (the ``.Mod1..N`` children in the client dictionary). Their inventory / OP5-
# equipment wire form is therefore NOT the normal ``modCount*0x00 + ScaleMod``
# block — the client instantiates the mods itself and expects, after the level
# byte, only ``flag + N empty mod-slots + one 0x00`` (an EMPTY ScaleMod list),
# all zero. Sending a by-name ``ScaleModPAL...`` block (or the wrong slot count)
# desyncs the client's GC reader → ``GCClassRegistry::readType Invalid type tag``
# → Avatar access-violation (live-caught 2026-07-10 on an equipped
# 2HStaffMythicPAL). Port of C# ``MerchantRuntime.GetOP5ModCount`` /
# ``ItemStatDatabase.TryGetItemReadDataSlotCount``: the slot count is the item's
# authored ItemModifier child count (``avatar.base.equipment`` OP5 reads exactly
# ``flag + slots + emptyList`` = slots + 2 zero bytes).
_BAKED_SPECIAL_KEYWORDS = (
    "mythic", "prebuilt", "partialbuilt", "generated", "boss",
    "seasonal", "wishingwell",
)
_baked_slot_cache: dict = {}


def authored_baked_mod_slots(gc_class: str) -> Optional[int]:
    """Authored ItemModifier child count for a baked-mod "special" item, or
    ``None`` when ``gc_class`` is a normal item (→ use the ScaleMod wire form).

    Returned N means the wire tail after ``level`` is ``N + 2`` zero bytes
    (flag + N empty mod-slots + empty ScaleMod list), NEVER a ScaleMod block.
    Result is memoised (content is static at runtime). A miss / DB error / any
    non-special item returns ``None`` so the normal path is used unchanged.
    """
    if not gc_class:
        return None
    key = gc_class.lower()
    short = key[len("items.pal."):] if key.startswith("items.pal.") else key
    if not any(k in short for k in _BAKED_SPECIAL_KEYWORDS):
        return None
    if short in _baked_slot_cache:
        return _baked_slot_cache[short]
    result = _compute_baked_slots(gc_class)
    _baked_slot_cache[short] = result
    return result


def clear_baked_slot_cache() -> None:
    """Drop the baked-slot memo — call when the content DB is rebuilt/swapped."""
    _baked_slot_cache.clear()


def _compute_baked_slots(gc_class: str) -> Optional[int]:
    from ..db import game_database as db
    raw: Optional[str] = None
    is_weapon = False
    for table in ("weapons", "armor", "items"):
        try:
            row = db.execute_reader(
                f"SELECT raw_json FROM {table} WHERE LOWER(gc_type)=LOWER(:g)",
                {"g": gc_class}).fetchone()
        except Exception:  # noqa: BLE001 — table/DB may be absent in some contexts
            row = None
        if row is not None and row["raw_json"]:
            raw = row["raw_json"]
            is_weapon = table == "weapons"
            break
    if not raw:
        return None
    try:
        node = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    children = node.get("children") or {}
    entries = (list(children.items()) if isinstance(children, dict)
               else [(str(i), c) for i, c in enumerate(children)])
    count = 0
    has_attr_mod = False
    for name, child in entries:
        if not isinstance(child, dict) or str(name).lower() == "description":
            continue
        ext = str(child.get("extends") or child.get("base_type") or "").lower()
        if "mod" in ext:
            count += 1
            if "itemattributemodifier" in ext:
                has_attr_mod = True
    if count == 0:
        return None
    # A weapon's base ItemAttributeModifier (the "SpeedM" every weapon inherits)
    # is NOT flattened into the weapons table by the importer, but the client
    # instantiates it from the base class — so a weapon needs one extra slot.
    # Armor tables ARE inheritance-flattened (their SpeedM is already counted).
    if is_weapon and not has_attr_mod:
        count += 1
    return count


def jewelry_op5_mod_slots(gc_class: str) -> Optional[int]:
    """Modslot count for a NON-mythic amulet/ring in the OP5 equipment tail, or
    ``None`` when ``gc_class`` is not (non-mythic) jewelry.

    Port of the C# amulet/ring OP5 branches (GameServer.Zone.cs ~4988 / ~5062):
    the wire is ``<N> 0x00 slots + one 0x00 mods-count`` and carries NO ScaleMod.
    Mythic jewelry has baked mods → returns ``None`` so it takes the baked path.
    """
    if not gc_class:
        return None
    lower = gc_class.lower()
    is_amulet = "amulet" in lower
    is_ring = ("ring" in lower) and not is_amulet
    if not (is_amulet or is_ring):
        return None
    if "mythic" in lower:
        return None                          # baked mods — general/baked path
    if is_amulet:
        # C#: "questamuletpal"/"uniqueamuletpal" → 2, else 1 (incl. amuletpal.amulet*).
        if "questamuletpal" in lower or "uniqueamuletpal" in lower:
            return 2
        return 1
    return 1                                  # rings default to 1 (non-mythic)


class GCObject:
    def __init__(self, id: int = 0, name: str = "", native_class: str = "", gc_class: str = ""):
        self.id = id
        self.name = name
        self.native_class = native_class
        self.gc_class = gc_class
        self.properties: List[GCObjectProperty] = []
        self.children: List["GCObject"] = []
        self.extra_data: bytes = b""
        self.target_slot: Optional[int] = None     # rings: slot 3 or 4
        self.preset_scale_mod: Optional[str] = None
        # items.modpal.* attribute mods (Intellect etc.) to emit as by-hash
        # ItemModifier children alongside the ScaleMod (vendor/loot affixes).
        self.preset_mod_refs: List[str] = []
        self.stored_rarity: int = -1               # -1 = unset, 0-5 = ItemRarity
        self.stored_level: int = -1                # -1 = unset, >=0 = fixed level

    # ── class-name helpers ──
    def get_packet_gc_class(self) -> str:
        return get_packet_gc_class_for(self.gc_class)

    def get_effective_rarity(self) -> int:
        if self.stored_rarity >= 0:
            return self.stored_rarity
        return detect_rarity_from_gc_class(self.gc_class)

    # ── item helpers (Phase 6) ──

    def get_modifier_gc_class(self) -> str:
        """Determine ScaleMod GC class — exact port of C# GetModifierGCClass.

        Tier-suffix rarity first; falls back to GetEffectiveRarity (StoredRarity
        + name keywords) when the suffix says Normal. The pick is DETERMINISTIC
        per gc class — this serializer runs on every relog/zone-load and a
        random pick (the old behaviour) re-rolled the item's stats each time.
        """
        if self.preset_scale_mod:
            return self.preset_scale_mod

        from .rarity_helper import (ItemRarity, get_tier_from_gc_type,
                                    get_rarity_from_tier, get_deterministic_scale_mod)
        tier = get_tier_from_gc_type(self.gc_class)
        item_rarity = get_rarity_from_tier(tier)
        if item_rarity == ItemRarity.Normal:
            effective = self.get_effective_rarity()
            if 0 < effective < 5:
                item_rarity = ItemRarity(effective)
        return get_deterministic_scale_mod(self.gc_class, item_rarity)

    def get_item_required_level(self) -> int:
        """Minimum level to equip this item. Port of C# GCObject.GetItemRequiredLevel."""
        from .rarity_helper import get_required_level_from_gc_class
        return get_required_level_from_gc_class(self.gc_class)

    @staticmethod
    def _is_consumable_type(native_class: str, gc_class: str) -> bool:
        """Return True if this native_class/gc_class represents a consumable.

        Mirrors the C# ``isConsumableDrop`` keyword list (WriteInitForDroppedItem)
        — including ``consumable`` itself (golden tickets, java, presents carry
        no other keyword) and the ``ActiveItem`` native class.
        """
        lower = gc_class.lower()
        return any(t in lower for t in (
            "potion", "scroll", "skillbook", "voucher", "townportal",
            "questitem", "itempal", "consumable",
        )) or native_class in ("ActiveItem", "ActiveSkill", "PassiveSkill")

    @staticmethod
    def _is_weapon(native_class: str) -> bool:
        return native_class in ("MeleeWeapon", "RangedWeapon")

    def _get_effective_level(self, player_level: int) -> int:
        """Compute the item's displayed level based on stored_level or rarity."""
        if self.stored_level >= 0:
            return self.stored_level
        from .rarity_helper import get_item_level
        return get_item_level(self.gc_class)

    # ── Item Wire-Format Serialization (Phase 6) ──

    def write_init(self, writer: LEWriter, player_level: int) -> None:
        """WriteInit — full equipment item serialization (includes weapon bytes).

        Wire format (binary-verified from C#):
          0xFF  CString(gc_class)  uint32(equipSlot)  0x00 0x00 0x01
          byte(level)  [optional: 0x00 flag]  [modCount * 0x00]
          [ScaleMod block]  [weapon extra bytes]
        """
        self._write_init_common(writer, player_level, include_weapon_bytes=True)

    def write_init_without_weapon_bytes(self, writer: LEWriter, player_level: int) -> None:
        """WriteInit without weapon trailing bytes — used for inventory pickup,
        equip/unequip handlers, and as the base for WriteInitForDroppedItem
        (non-consumable path).
        """
        self._write_init_common(writer, player_level, include_weapon_bytes=False)

    def write_init_for_dropped_item(self, writer: LEWriter, player_level: int) -> None:
        """Wire format for items on the ground (0x01 create entity).

        For consumables, writes a simplified 9-byte header:
          0xFF CString(gc_class)
          0x00000000 0x00 0x00 0x01 <effectiveLevel> 0x00 0x00
        For equipment/weapons, delegates to WriteInit without weapon bytes.
        """
        if self._is_consumable_type(self.native_class, self.gc_class):
            writer.write_byte(0xFF)
            writer.write_cstring(self.get_packet_gc_class())
            writer.write_uint32(0x00000000)
            writer.write_byte(0x00)
            writer.write_byte(0x00)
            writer.write_byte(0x01)
            writer.write_byte(self._get_effective_level(player_level))
            writer.write_byte(0x00)  # flags
            writer.write_byte(0x00)  # modCount
        else:
            self._write_init_common(writer, player_level, include_weapon_bytes=False)

    def write_init_for_inventory(self, writer: LEWriter, pos_x: int, pos_y: int,
                                  inventory_slot: int, player_level: int,
                                  count: int = 1) -> None:
        """Wire format for items in an inventory slot.

        Port of C# ``GCObject.WriteInitForInventory`` (GCObject.cs:2841). The
        header is ``0xFF cstring(gc) uint32(inventorySlot) posX posY count``.

        ``count`` is the stack quantity (e.g. 20 noob potions). It was previously
        hardcoded to ``0x01`` which made every stack render as a single item in
        the client — the starting potions all showed as 1 instead of 20.

        Consumables / generic non-equippable "Item" types then emit exactly
        ``level 0x00 0x00`` — one mod slot, zero mods — and stop. They must
        NOT carry a ScaleMod block: the client's ReadInit for these reads one
        mod-slot byte then a mod-count byte, so a ScaleMod tag (0x01) followed
        by its 0xFF type tag would be misread as "255 modifiers" and desync
        the whole inventory section (the spawn-reject join bug).

        C# also writes NO trailing inventory slot here — the leading
        ``inventorySlot`` is the only slot field.
        """
        level = self._get_effective_level(player_level)

        writer.write_byte(0xFF)
        writer.write_cstring(self.get_packet_gc_class())
        writer.write_uint32(inventory_slot)
        writer.write_byte(pos_x)
        writer.write_byte(pos_y)
        writer.write_byte(min(0xFF, max(1, count)))   # stack quantity (clamped to a byte)

        if self._is_consumable_type(self.native_class, self.gc_class):
            writer.write_byte(level)
            writer.write_byte(0x00)               # one mod slot
            writer.write_byte(0x00)               # zero mods
            return

        writer.write_byte(level)
        # Baked-mod special item (Mythic/Prebuilt/…): the client owns the mods —
        # emit `flag + N empty slots + empty ScaleMod list` (N+2 zero bytes), NO
        # ScaleMod block (a ScaleMod here desyncs the client → Avatar crash on the
        # quest-reward Prebuilt/Mythic gear). See `authored_baked_mod_slots`.
        baked_slots = authored_baked_mod_slots(self.gc_class)
        if baked_slots is not None:
            for _ in range(baked_slots + 2):
                writer.write_byte(0x00)
            return
        # FLAG BYTE — exact C# WriteInitForInventory `writeFlag` rule
        # (GCObject.cs:3263). The extra leading 0x00 is written ONLY for
        # mythic/prebuilt/partialbuilt SPECIAL items, and even then weapons skip
        # it unless mythic (`writeFlag && (!isWeapon || isMythic)`). Regular
        # dash-suffix colored gear (Superior/Magic/Rare/Unique) — everything a
        # merchant sells — gets NO flag byte; its wire is `level + modCount×00 +
        # scaleMod`. The previous gate (`effective_rarity >= 1`) wrongly wrote a
        # flag byte for every colored item, shifting the stream by one so the
        # ScaleMod 0x01 tag was misread as 255 modifiers → the next
        # ComponentUpdate header desynced → client "Zone communication error.
        # Code 3" on buy, and an access violation re-loading the saved item on
        # relog. Loot never hit it (dropped items left rarity unset → effective 0).
        lower_gc = self.gc_class.lower()
        is_mythic = "mythic" in lower_gc
        is_special = is_mythic or "prebuilt" in lower_gc or "partialbuilt" in lower_gc
        is_weapon = self._is_weapon(self.native_class)
        if is_special and (not is_weapon or is_mythic):
            writer.write_byte(0x00)               # special-item flag byte
        # modCount * 0x00 — the first of these is what the client reads as flags.
        mod_count = self._get_mod_count()
        for _ in range(mod_count):
            writer.write_byte(0x00)
        # ScaleMod block (colored items only; normal = single 0x00).
        self._write_scale_mod_block(writer)
        # NOTE: C# WriteInitForInventory writes no trailing inventory slot.

    def write_init_for_equip(self, writer: LEWriter, player_level: int) -> None:
        """Wire format for equipping an item (uses TargetSlot if set)."""
        self._write_init_common(writer, player_level, include_weapon_bytes=True)

    def write_init_for_equip_op5(self, writer: LEWriter, player_level: int) -> None:
        """OP5 Equipment component inline format (UnityGameServer.cs ~22550+).

        Differs from WriteInitForEquip:
          - No rarity byte(s)
          - No weapon trailing bytes (MeleeWeapon 01 00 02 00 00 / Ranged)
          - No flag byte for normal items
          - modCount status bytes + ScaleMod block follow

        The trailing ScaleMod block matters: OP5 is the ONLY equipment write
        that fires on zone-in/warp, and the old hardcoded 0x00 terminator
        (= "no ScaleMod children") stripped the rarity mod off every equipped
        item — colored gear spawned back WHITE after every warp/relog until
        unequip+re-equip resent it through the full equip stream. C# OP5
        writes the same Normal-0x00 / colored-0x01+block tail as WriteInit.
        """
        equip_slot = self.get_equipment_slot_from_gc_class()
        mod_count = self._get_mod_count()

        writer.write_byte(0xFF)
        writer.write_cstring(self.get_packet_gc_class())
        writer.write_uint32(equip_slot)
        writer.write_byte(0x00)
        writer.write_byte(0x00)
        writer.write_byte(0x01)
        writer.write_byte(self._get_effective_level(player_level))
        # Amulets & rings have their OWN OP5 wire tail (C# GameServer.Zone.cs
        # ~4970/5050): `modCount empty slots + a 0x00 mods-count`, and NO
        # ScaleMod block for non-mythic jewelry. The generic ScaleMod tail
        # corrupts the client's jewelry ReadInit → the equipment stream desyncs
        # and the client drops the item ("processMessage Unknown message type",
        # live-caught 2026-07-10 on an equipped AmuletUnique6). Checked BEFORE
        # the baked/general paths (mythic jewelry falls through to baked).
        jewelry_slots = jewelry_op5_mod_slots(self.gc_class)
        if jewelry_slots is not None:
            for _ in range(jewelry_slots):
                writer.write_byte(0x00)
            writer.write_byte(0x00)          # mods-count = 0 (non-mythic → no ScaleMod)
            return
        # Baked-mod special item (Mythic/Prebuilt/…): the client owns the mods,
        # so emit `flag + N empty slots + empty ScaleMod list` (N+2 zero bytes)
        # and NO ScaleMod block. Sending a ScaleMod here desyncs the client's GC
        # reader → Avatar crash (live-caught on 2HStaffMythicPAL). See
        # `authored_baked_mod_slots`.
        baked_slots = authored_baked_mod_slots(self.gc_class)
        if baked_slots is not None:
            for _ in range(baked_slots + 2):
                writer.write_byte(0x00)
            return
        for _ in range(mod_count):
            writer.write_byte(0x00)
        self._write_scale_mod_block(writer)

    # ── Private write helpers ──

    def _write_init_common(self, writer: LEWriter, player_level: int,
                           include_weapon_bytes: bool = False) -> None:
        """Core WriteInit implementation shared by all variants."""
        level = self._get_effective_level(player_level)
        equip_slot = self.get_equipment_slot_from_gc_class()

        writer.write_byte(0xFF)
        writer.write_cstring(self.get_packet_gc_class())
        writer.write_uint32(equip_slot)
        writer.write_byte(0x00)          # fill
        writer.write_byte(0x00)          # fill
        writer.write_byte(0x01)          # quantity
        writer.write_byte(level)

        # FLAG BYTE — exact C# WriteInit `writeFlag` rule (GCObject.cs:1090).
        # IDENTICAL to WriteInitForInventory: written ONLY for mythic/prebuilt/
        # partialbuilt SPECIAL items, and weapons skip it unless mythic. Regular
        # dash-suffix colored gear (Superior/Magic/Rare/Unique) — what a vendor
        # sells — gets NO flag byte. The previous `effective_rarity >= 1` gate
        # wrote a spurious flag for every colored item, shifting the equip stream
        # by one so the item rendered WHITE and the client crashed with "Zone
        # communication error. Code 3" on equip. Equip was only ever live-tested
        # with white (Normal) starting gear, so this never surfaced.
        lower_gc = self.gc_class.lower()
        is_mythic = "mythic" in lower_gc
        is_special = is_mythic or "prebuilt" in lower_gc or "partialbuilt" in lower_gc
        is_weapon = self._is_weapon(self.native_class)
        if is_special and (not is_weapon or is_mythic):
            writer.write_byte(0x00)

        # Modifier slots — one 0x00 per modifier.
        mod_count = self._get_mod_count()
        for _ in range(mod_count):
            writer.write_byte(0x00)

        # ScaleMod block.
        self._write_scale_mod_block(writer)

        # Weapon extra trailing bytes.
        if include_weapon_bytes and self._is_weapon(self.native_class):
            if self.native_class == "MeleeWeapon":
                writer.write_uint16(0x0001)
                writer.write_byte(0x02)
                writer.write_uint16(0x0000)
            elif self.native_class == "RangedWeapon":
                writer.write_uint16(0x0000)
                writer.write_uint16(0x0000)

    def _get_mod_count(self) -> int:
        """Return the number of modifier slots for this item type.

        Uses the ItemStatDatabase (pre-computed from SQLite) for lookups.
        Falls back to 0.
        """
        from .item_stat_database import item_stat_database
        return item_stat_database.get_mod_count(self.gc_class)

    def _write_scale_mod_block(self, writer: LEWriter) -> None:
        """Write the ScaleMod block after modCount slots.

        Normal-rarity items: single 0x00 byte (no ScaleMod).
        Colored-rarity items: 0x01 tag + 0xFF cstring(scaleMod) + 0x03 + 0x15 + 0x11111111.
        """
        from .rarity_helper import ItemRarity, get_tier_from_gc_type, get_rarity_from_tier
        effective_rarity = self.get_effective_rarity()
        lower = self.gc_class.lower()
        # The -N tier suffix encodes rarity for ALL PAL vendor/loot gear; an
        # item whose stored rarity was lost (legacy DB rows) must still render
        # colored, not white.
        suffix_rarity = get_rarity_from_tier(get_tier_from_gc_type(self.gc_class))
        is_normal = (effective_rarity == 0
                     and suffix_rarity == ItemRarity.Normal
                     and not any(q in lower for q in (
                         "superior", "magical", "magic", "rare", "unique", "mythic")))

        if is_normal:
            writer.write_byte(0x00)
        else:
            # Colored item — ItemModifier child list: N by-hash attribute mods
            # (items.modpal.*, the rolled affixes) then the by-name ScaleMod, all
            # under one count. Each child is an ItemAttributeModifier whose body
            # is the fixed 6 bytes ``0x03 0x15 <u32>`` (readData @0x00588AE0:
            # flags=0x03 -> u8 + u32; the effect comes from the GC def). This is
            # the same child list the merchant emits — carrying preset_mod_refs
            # here is what keeps a bought item's Intellect in the bag / on relog.
            mod_refs = [m for m in self.preset_mod_refs if m]
            writer.write_byte(min(0xFF, 1 + len(mod_refs)))   # attr mods + ScaleMod
            for ref in mod_refs:
                writer.write_byte(0x04)          # by-hash type tag
                writer.write_uint32(hash_djb2(ref))
                writer.write_byte(0x03)
                writer.write_byte(0x15)
                writer.write_uint32(0x11111111)
            scale_mod = self.get_modifier_gc_class()
            writer.write_byte(0xFF)          # by-name ScaleMod child
            writer.write_cstring(scale_mod)
            writer.write_byte(0x03)
            writer.write_byte(0x15)
            writer.write_uint32(0x11111111)

    # ── tree building ──
    def add_property(self, prop: GCObjectProperty) -> None:
        self.properties.append(prop)

    def add_child(self, child: "GCObject") -> None:
        self.children.append(child)

    def get_property_uint32(self, property_name: str) -> int:
        for p in self.properties:
            if p.name == property_name and isinstance(p, UInt32Property):
                return p.value
        return 0

    # ── DFC serialization ──
    def write_full_gc_object(self, writer: LEWriter) -> None:
        writer.write_byte(DFC_VERSION)
        writer.write_uint32(hash_djb2(self.native_class))
        writer.write_uint32(self.id)
        writer.write_cstring(self.name)
        writer.write_uint32(len(self.children))
        for child in self.children:
            child.write_full_gc_object(writer)
        # Hash the canonical client-facing form, not the raw stored GCClass.
        writer.write_uint32(hash_djb2(get_packet_gc_class_for(self.gc_class)))
        for prop in self.properties:
            prop.write_dfc(writer)
        writer.write_uint32(0)  # end-of-object marker
        if self.extra_data:
            writer.write_bytes(self.extra_data)

    def write_init_for_drop(self, writer: LEWriter) -> None:
        writer.write_byte(0xFF)
        writer.write_cstring(self.get_packet_gc_class())

    def write_data(self, writer: LEWriter) -> None:
        """Skill data: [SkillSlot uint32][0x01]. Only valid for ActiveSkill."""
        if self.native_class == "ActiveSkill":
            skill_slot = self.get_property_uint32("SkillSlot") or 100
            writer.write_uint32(skill_slot)
            writer.write_byte(0x01)

    def get_equipment_slot_from_gc_class(self) -> int:
        if self.target_slot is not None:
            return self.target_slot
        lower = self.gc_class.lower()
        if "helm" in lower:
            return 5
        if "shoulder" in lower or "pauldron" in lower:
            return 8
        if "armor" in lower or "body" in lower or "chest" in lower:
            return 6
        if "gloves" in lower:
            return 2
        if "boots" in lower:
            return 7
        if "shield" in lower:
            return 11
        if "ring" in lower:
            return 3
        if "amulet" in lower:
            return 1
        return 10  # weapons + default

    @staticmethod
    def create_player(character) -> "GCObject":
        """Build the Player GCObject for a character (duck-typed: .id, .name)."""
        player = GCObject(id=character.id, native_class="Player", gc_class="Player", name=character.name)
        player.add_property(StringProperty("Name", character.name))

        extra = bytearray()
        extra += b"plzwork1\x00"
        extra += b"plzwork2\x00"
        id_bytes = (0x05040302).to_bytes(4, "little")
        extra += id_bytes
        extra += id_bytes
        extra += b"\x00\xAA"
        extra += b"Normal\x00"
        extra += b"\x02\x00"
        extra += (0x05040302).to_bytes(4, "little")
        player.extra_data = bytes(extra)
        return player
