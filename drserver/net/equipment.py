"""Equipment handler — equip/unequip with swap logic and DB persistence.

Ported from C# Networking/EquipmentHandler.cs. Routes client equipment requests
(0x28 AddEquippedItem, 0x29 RemoveEquippedItem) on the Equipment component
through validation and sends state-sync packets back to the client.

Phase 6: Full implementation with slot validation, 2H/shield conflict checks,
swap logic, and SQLite persistence via character_repository.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..core import log, settings
from ..db import character_repository
from ..data.gc_object import GCObject
from ..data import gc_object_factory
from ..data import rarity_helper
from ..util.byte_io import LEReader, LEWriter
from .component_update import write_synch, write_synch_none, synch_hp
from .inventory_model import CursorItem

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection

# Wire slot → DB slot name mapping.
_SLOT_TO_DB: dict[int, str] = {
    1: "amulet", 2: "gloves", 3: "ring1", 4: "ring2",
    5: "helmet", 6: "armor", 7: "boots", 8: "shoulders",
    10: "weapon", 11: "shield",
}

# Two-handed weapon GC class patterns.
_TWO_HANDED_KEYWORDS = ("bow", "crossbow", "gun", "staff", "polearm", "cannon", "2h")

_VALID_SLOTS = set(_SLOT_TO_DB.keys())


def _load_current_equipment(conn: "RRConnection") -> tuple[
        dict[str, Optional[str]], dict[str, int], dict[str, int], dict[str, str],
        dict[str, list[str]]]:
    """Load the current equipment state from DB: (gc, rarity, level, scaleMod,
    modRefs) per slot.

    Rarity/level/scaleMod/modRefs travel WITH the slot — the C# server tracks the
    live GCObject per slot and syncs ``GetEffectiveRarity()``/``StoredLevel`` into
    ``equipment.slotRarity/slotLevel`` on every save. Dropping them (the old
    behaviour) reverted every colored item to white on unequip/relog; dropping
    the ScaleMod re-rolled the item's stats; dropping modRefs lost its affixes.
    """
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None or saved.equipment is None:
        return ({name: None for name in _SLOT_TO_DB.values()}, {}, {}, {}, {})
    eq = saved.equipment
    gc = {name: getattr(eq, name, None) for name in _SLOT_TO_DB.values()}
    return (gc, dict(eq.slot_rarity or {}), dict(eq.slot_level or {}),
            dict(eq.slot_scale_mod or {}),
            {k: list(v) for k, v in (eq.slot_mod_refs or {}).items()})


def _save_equipment_to_db(conn: "RRConnection", equipment: dict[str, Optional[str]],
                          rarity: dict[str, int], level: dict[str, int],
                          scale: dict[str, str],
                          mods: dict[str, list[str]]) -> None:
    """Persist equipment state + per-slot rarity/level/scaleMod/modRefs to SQLite."""
    from ..data.saved_character import SavedCharacter, StartingEquipment

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return
    eq = saved.equipment
    if eq is None:
        eq = StartingEquipment()
        saved.equipment = eq
    for db_name, gc_class in equipment.items():
        setattr(eq, db_name, gc_class)
    eq.slot_rarity = {k: v for k, v in rarity.items() if equipment.get(k)}
    eq.slot_level = {k: v for k, v in level.items() if equipment.get(k)}
    eq.slot_scale_mod = {k: v for k, v in scale.items() if equipment.get(k) and v}
    eq.slot_mod_refs = {k: list(v) for k, v in mods.items() if equipment.get(k) and v}
    character_repository.save_character(saved)


def handle_add_equipped_item(server: "GameServer", conn: "RRConnection",
                              reader: LEReader, _component_id: int) -> None:
    """Handle 0x28 on Equipment component — equip the cursor item, with swap.

    Client wire format: ``uint32 equipSlot`` only. The item is whatever is on the
    cursor (C# ``PlayerState.ActiveItem``) — the packet carries no item data.
    """
    if reader.remaining < 4:
        log.warn(f"[EQUIP] conn {conn.conn_id}: AddEquippedItem too short ({reader.remaining} bytes)")
        return

    slot = reader.read_uint32()

    cursor = conn.inv_model.cursor
    if cursor is None:
        log.info(f"[EQUIP] conn {conn.conn_id}: equip slot {slot} with empty cursor — no-op")
        return

    gc_class = cursor.gc_class
    log.info(f"[EQUIP] conn {conn.conn_id} equip slot={slot} gc='{gc_class}'")

    if slot not in _VALID_SLOTS:
        log.warn(f"[EQUIP] conn {conn.conn_id}: invalid equip slot {slot}")
        return

    gc_lower = gc_class.lower()

    # Quest items are inventory-only — never equippable (C# HandleAddEquippedItem).
    if gc_lower.startswith("questitempal"):
        log.warn(f"[EQUIP] conn {conn.conn_id}: rejected quest item '{gc_class}'")
        return

    # Build the item object; rarity/level travel with it from the cursor.
    item = gc_object_factory.create_equipment_item(gc_class)
    item.stored_level = cursor.stored_level
    item.stored_rarity = cursor.rarity
    item.preset_scale_mod = cursor.scale_mod or None
    item.preset_mod_refs = list(cursor.mod_refs)

    # ── Slot validation (port of C#): the item's base slot must match the
    # request, with two exceptions — 1H weapons may go to off-hand 11
    # (dual-wield) and rings fit either ring slot.
    item.target_slot = None
    correct_slot = item.get_equipment_slot_from_gc_class()
    if slot != correct_slot:
        is_1h_weapon = correct_slot == 10 and not any(
            kw in gc_lower for kw in _TWO_HANDED_KEYWORDS)
        if is_1h_weapon and slot == 11:
            pass                                # dual-wield off-hand
        elif correct_slot in (3, 4) and slot in (3, 4):
            pass                                # either ring slot
        else:
            log.warn(f"[EQUIP] conn {conn.conn_id}: '{gc_class}' belongs in slot "
                     f"{correct_slot}, not {slot} — item stays in hand")
            return

    # ── Level requirement (C# enableEquipLevelCheck, default on). The client
    # tooltip (FUN_00496640) shows "Requires Level %d" = GC LevelReq, or when
    # that is 0: the item's wire level byte − 5, clamped to [0, 100] — flat,
    # NO rarity delta (binary-verified 2026-06-10). The old rarity-aware
    # formula said "level 9" while the tooltip said 16 (−12 vs −5).
    if settings.get_bool("enableEquipLevelCheck", True):
        item_level = (cursor.stored_level if cursor.stored_level >= 0
                      else rarity_helper.get_item_level(gc_class))
        required = max(0, min(100, item_level - 5))
        if conn.player_level < required:
            log.info(f"[EQUIP] conn {conn.conn_id}: level too low for '{gc_class}' "
                     f"(requires {required}, player {conn.player_level})")
            conn.send_system_message(
                f"You must be level {required} to equip that item.")
            return

    current, rarity_map, level_map, scale_map, mods_map = _load_current_equipment(conn)

    # ── 2H / off-hand conflict: BLOCK like C# (the old auto-unequip silently
    # destroyed the conflicting item — it never went back to a bag).
    if slot == 10 and any(kw in gc_lower for kw in _TWO_HANDED_KEYWORDS):
        if current.get("shield"):
            log.info(f"[EQUIP] conn {conn.conn_id}: 2H '{gc_class}' blocked — "
                     f"off-hand '{current['shield']}' equipped")
            conn.send_system_message("Unequip your off-hand item first.")
            return
    elif slot == 11:
        main_gc = current.get("weapon") or ""
        if any(kw in main_gc.lower() for kw in _TWO_HANDED_KEYWORDS):
            log.info(f"[EQUIP] conn {conn.conn_id}: off-hand '{gc_class}' blocked — "
                     f"2H '{main_gc}' equipped")
            conn.send_system_message("Unequip your two-handed weapon first.")
            return

    # ── Dual-wield: a 1H weapon going to off-hand must serialize equipSlot=11
    # (the slot rides INSIDE the item payload — without TargetSlot the packet
    # said 10 and the client dropped the equip, eating the item).
    if correct_slot == 10:
        item.target_slot = 11 if slot == 11 else None

    db_name = _SLOT_TO_DB[slot]
    existing_gc = current.get(db_name)
    existing_item = None
    if existing_gc:
        existing_item = gc_object_factory.create_equipment_item(existing_gc)
        existing_item.stored_rarity = rarity_map.get(db_name, -1)
        existing_item.stored_level = level_map.get(db_name, -1)
        existing_item.preset_scale_mod = scale_map.get(db_name) or None
        existing_item.preset_mod_refs = list(mods_map.get(db_name) or [])
        if db_name == "shield" and existing_item.native_class in (
                "MeleeWeapon", "RangedWeapon"):
            existing_item.target_slot = 11

    packet = build_equip_stream(
        conn.equipment_component_id, conn.unit_container_id,
        conn.manipulators_component_id, slot, item, existing_item,
        conn.player_level, synch_hp(conn),
    )
    conn.send_to_client(packet)

    # Show the change to everyone else in the instance (their copy of this
    # avatar renders gear via its own Manipulators component).
    relay_equipment_to_viewers(
        server, conn, item, slot,
        removed_slot=slot if existing_item is not None else None)

    # Persist new equipment + its rarity/level/scaleMod.
    current[db_name] = gc_class
    rarity_map[db_name] = item.get_effective_rarity()
    level_map[db_name] = cursor.stored_level
    scale_map[db_name] = cursor.scale_mod or ""
    mods_map[db_name] = list(cursor.mod_refs)
    _save_equipment_to_db(conn, current, rarity_map, level_map, scale_map, mods_map)

    # Swap: the previously-equipped item moves onto the cursor (keeping its
    # stored rarity/level/affixes); otherwise the cursor is now empty.
    if existing_item is not None:
        conn.inv_model.cursor = CursorItem(
            gc_class=existing_gc,
            rarity=existing_item.stored_rarity,
            stored_level=existing_item.stored_level,
            scale_mod=existing_item.preset_scale_mod or "",
            mod_refs=list(existing_item.preset_mod_refs),
        )
        log.info(f"[EQUIP] conn {conn.conn_id} swapped '{gc_class}' into slot {slot}, "
                 f"'{existing_gc}' now on cursor")
    else:
        conn.inv_model.cursor = None
        log.info(f"[EQUIP] conn {conn.conn_id} equipped '{gc_class}' to slot {slot} ({db_name})")


def handle_remove_equipped_item(server: "GameServer", conn: "RRConnection",
                                 reader: LEReader, _component_id: int) -> None:
    """Handle 0x29 on Equipment component — unequip an item.

    Client wire format:
      uint32 equipSlot
    """
    if reader.remaining < 4:
        log.warn(f"[EQUIP] conn {conn.conn_id}: RemoveEquippedItem too short ({reader.remaining} bytes)")
        return

    slot = reader.read_uint32()
    log.info(f"[EQUIP] conn {conn.conn_id} unequip slot={slot}")

    if slot not in _VALID_SLOTS:
        log.warn(f"[EQUIP] conn {conn.conn_id}: invalid unequip slot {slot}")
        return

    db_name = _SLOT_TO_DB[slot]
    current, rarity_map, level_map, scale_map, mods_map = _load_current_equipment(conn)
    gc_class = current.get(db_name)

    # C# HandleRemoveEquippedItem returns without sending when the slot is empty.
    # Sending a stray ComponentUpdate for a no-op desyncs the client.
    if not gc_class:
        log.info(f"[EQUIP] conn {conn.conn_id}: unequip slot {slot} already empty — no-op")
        return

    # Build the unequipped item so the UnitContainer can hold it as the active
    # (in-hand / cursor) item — matches C# putting the item "in hand" on unequip.
    # Rarity/level come back out of the slot they were stored with on equip;
    # rebuilding the item bare (the old behaviour) reverted it to white.
    item = gc_object_factory.create_equipment_item(gc_class)
    item.stored_rarity = rarity_map.get(db_name, -1)
    item.stored_level = level_map.get(db_name, -1)
    item.preset_scale_mod = scale_map.get(db_name) or None
    item.preset_mod_refs = list(mods_map.get(db_name) or [])
    if db_name == "shield" and item.native_class in ("MeleeWeapon", "RangedWeapon"):
        item.target_slot = 11                   # dual-wield off-hand
    hp_wire = synch_hp(conn)
    packet = build_unequip_stream(
        conn.equipment_component_id,
        conn.unit_container_id,
        conn.manipulators_component_id,
        slot,
        item,
        conn.player_level,
        hp_wire,
    )
    conn.send_to_client(packet)

    # Strip the item from every instance peer's copy of the avatar.
    relay_equipment_to_viewers(server, conn, None, slot, removed_slot=slot)

    # Remove from DB and put the unequipped item onto the cursor so the player
    # can drop it into the inventory grid (the unequip stream already set it as
    # the UnitContainer active item client-side). The cursor carries the stored
    # rarity/level so a later place/equip keeps them.
    current[db_name] = None
    rarity_map.pop(db_name, None)
    level_map.pop(db_name, None)
    scale_map.pop(db_name, None)
    mods_map.pop(db_name, None)
    _save_equipment_to_db(conn, current, rarity_map, level_map, scale_map, mods_map)
    conn.inv_model.cursor = CursorItem(
        gc_class=gc_class, rarity=item.stored_rarity,
        stored_level=item.stored_level,
        scale_mod=item.preset_scale_mod or "",
        mod_refs=list(item.preset_mod_refs))

    log.info(f"[EQUIP] conn {conn.conn_id} unequipped '{gc_class}' from slot {slot}"
             f" (rarity={item.stored_rarity}, level={item.stored_level})")


def relay_equipment_to_viewers(server: "GameServer", conn: "RRConnection",
                               item: Optional[GCObject], slot: int,
                               removed_slot: Optional[int] = None) -> None:
    """Mirror an equip/unequip onto every instance peer's copy of the avatar.

    The viewer-side avatar renders gear through its **Manipulators** component
    (spawn PASS 2) — the owner-only Equipment/UnitContainer components do not
    exist on that copy, so the relay carries ONLY the visual manipulator ops,
    retargeted at the viewer's remapped manipulators id
    (``server.remote_manip_ids[viewer][owner]``):

      remove: ``0x35 <manip> 0x01 <u32 slot>``  (same op the owner stream uses)
      add:    ``0x35 <manip> 0x00 <item init>`` (same serializer/rarity bytes)

    Trailers are the flags-only empty synch — the proven remote-avatar shape
    (movement relay / self-cast relay); an HP-bearing trailer here would assert
    the owner's HP to a viewer mid-combat (the monster-swing crash class).
    Without this relay a peer only saw gear as of the moment the avatar spawned
    for them (live user report 2026-07-08).
    """
    if server is None or getattr(server, "remote_manip_ids", None) is None:
        return          # solo-context callers/tests run without a full server
    for other in list(server.connections.values()):
        if other is conn or not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue
        manip_map = server.remote_manip_ids.get(other.login_name)
        if not manip_map or conn.login_name not in manip_map:
            continue
        remote_manip_id = manip_map[conn.login_name]

        w = LEWriter()
        w.write_byte(0x07)                      # BeginStream
        if removed_slot is not None:
            w.write_byte(0x35)
            w.write_uint16(remote_manip_id)
            w.write_byte(0x01)                  # Manipulators remove (visual)
            w.write_uint32(removed_slot)
            write_synch_none(w)
        if item is not None:
            w.write_byte(0x35)
            w.write_uint16(remote_manip_id)
            w.write_byte(0x00)                  # Manipulators add (visual)
            item.write_init_without_weapon_bytes(w, conn.player_level)
            write_synch_none(w)
        w.write_byte(0x06)                      # EndStream
        other.send_to_client(w.to_array())
        log.info(f"[EQUIP-RELAY] '{conn.login_name}' slot={slot} -> viewer "
                 f"'{other.login_name}' (manip={remote_manip_id}, "
                 f"{'swap' if removed_slot is not None and item is not None else 'add' if item is not None else 'remove'})")


def build_unequip_stream(equipment_id: int, unit_container_id: int, manipulators_id: int,
                         slot: int, item: GCObject, player_level: int, hp_wire: int) -> bytes:
    """Build the unequip ComponentUpdate stream (port of C# HandleRemoveEquippedItem).

    One BeginStream wraps three synch-terminated ComponentUpdates:
      1. Equipment 0x29 (remove)            — uint32 slot
      2. UnitContainer 0x28 (set active)    — item.WriteInitWithoutWeaponBytes
      3. Manipulators 0x01 (remove visual)  — uint32 slot

    Every update is followed by WriteSynch (0x02 + uint32 SynchHP); omitting it
    is what crashed the client ("zone communication error").
    """
    w = LEWriter()
    w.write_byte(0x07)                          # BeginStream

    # Part 1 — Equipment remove.
    w.write_byte(0x35)
    w.write_uint16(equipment_id)
    w.write_byte(0x29)
    w.write_uint32(slot)
    write_synch(w, hp_wire)

    # Part 2 — UnitContainer set active item (item now "in hand").
    w.write_byte(0x35)
    w.write_uint16(unit_container_id)
    w.write_byte(0x28)
    item.write_init_without_weapon_bytes(w, player_level)
    write_synch(w, hp_wire)

    # Part 3 — Manipulators remove (0x01, not 0x1F).
    w.write_byte(0x35)
    w.write_uint16(manipulators_id)
    w.write_byte(0x01)
    w.write_uint32(slot)
    write_synch(w, hp_wire)

    w.write_byte(0x06)                          # EndStream
    return w.to_array()


def build_equip_stream(equipment_id: int, unit_container_id: int, manipulators_id: int,
                       slot: int, item: GCObject, existing: Optional[GCObject],
                       player_level: int, hp_wire: int) -> bytes:
    """Build the equip ComponentUpdate stream (port of C# HandleAddEquippedItem).

    One BeginStream wraps synch-terminated ComponentUpdates:
      [swap only] Equipment 0x29 remove + Manipulators 0x01 remove
      Equipment 0x28 add (item, no weapon bytes)
      UnitContainer 0x28 set-active(old)  OR  0x29 clear-active
      Manipulators 0x00 add (item, no weapon bytes)
    """
    w = LEWriter()
    w.write_byte(0x07)                          # BeginStream

    if existing is not None:
        w.write_byte(0x35)
        w.write_uint16(equipment_id)
        w.write_byte(0x29)
        w.write_uint32(slot)
        write_synch(w, hp_wire)

        w.write_byte(0x35)
        w.write_uint16(manipulators_id)
        w.write_byte(0x01)
        w.write_uint32(slot)
        write_synch(w, hp_wire)

    # Add the new item to Equipment.
    w.write_byte(0x35)
    w.write_uint16(equipment_id)
    w.write_byte(0x28)
    item.write_init_without_weapon_bytes(w, player_level)
    write_synch(w, hp_wire)

    # UnitContainer cursor: swap puts the old item in hand, else clear it.
    w.write_byte(0x35)
    w.write_uint16(unit_container_id)
    if existing is not None:
        w.write_byte(0x28)
        existing.write_init_without_weapon_bytes(w, player_level)
    else:
        w.write_byte(0x29)
    write_synch(w, hp_wire)

    # Render the new item on the avatar.
    w.write_byte(0x35)
    w.write_uint16(manipulators_id)
    w.write_byte(0x00)
    item.write_init_without_weapon_bytes(w, player_level)
    write_synch(w, hp_wire)

    w.write_byte(0x06)                          # EndStream
    return w.to_array()


