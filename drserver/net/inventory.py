"""Inventory handler — use/place/pickup/drop items with a slot-map + cursor.

Ported from C# Networking/InventoryHandler.cs. Routes client inventory requests
on the UnitContainer component (0x25 UseItem, 0x28 PickupFromInventory,
0x29 PlaceInInventory, 0x23 DropItem) through the per-connection slot-map model
(``conn.inv_model``) and persists to SQLite.

Item identity is the uint32 slot id the client echoes back — looked up via
``InventoryModel.resolve``, never used as a 0-based list index. Pickup fills the
cursor (``inv_model.cursor``); Place/Drop consume it. Each wire response wraps a
single BeginStream/EndStream around synch-terminated ComponentUpdates
(0x1E ItemAdd, 0x1F ItemRemoved, 0x28 SetActive, 0x29 ClearActive).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core import log, settings
from ..db import character_repository
from ..data.gc_object import GCObject, get_packet_gc_class_for
from ..data import gc_object_factory
from ..managers import loot, town_portal
from ..util.byte_io import LEReader, LEWriter
from .component_update import write_synch, synch_hp
from .inventory_model import CursorItem, InvItem, MAIN_INVENTORY

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection

# Inventory grid dimensions.
_INVENTORY_COLS = 10
_INVENTORY_ROWS = 8

# Max stack for simple-item merge on ground pickup. C# caps free players at 5
# and members at 10 (server-enforced, mirrors original DR membership); the
# Python port has no membership concept on the connection yet, so use the
# member cap.
_MAX_STACK = 10

# Item gc_class keywords that serialize with the bare "simple item" cursor/add
# format (no ScaleMod) — matches the C# branch in Place/Pickup.
_SIMPLE_ITEM_KEYWORDS = (
    "questitem", "consumable", "potion", "townportal", "scroll",
    "skillbook", "voucher",
)


def _get_item_size(gc_class: str) -> tuple[int, int]:
    """Inventory grid size from the client-faithful item catalog."""
    from ..data import item_catalog
    return item_catalog.get_item_size(gc_class)


def _is_simple_item(gc_class: str) -> bool:
    lower = gc_class.lower()
    return any(kw in lower for kw in _SIMPLE_ITEM_KEYWORDS)


def _write_transient_mod_byte(w: LEWriter, gc_class: str) -> None:
    """Transient Mod1 flags byte for the two potions that declare one.

    Binary RE 0x583920: the client walks the GC object's transient children;
    ``ItemAttributeModifier::readData``@0x588AE0 reads 1 byte. DragonJuice and
    IntBuff declare a transient Mod1 in their .gc — every other simple item
    has none and must NOT carry the byte (matches the C# branches).
    """
    lower = gc_class.lower()
    if "dragonjuice" in lower or "intbuff" in lower:
        w.write_byte(0x00)


def _reconcile(conn: "RRConnection") -> None:
    """Sync the slot-map with the DB inventory, preserving known slot ids.

    Items can be added to the DB out-of-band between handler calls (merchant buy,
    loot/quest rewards) or removed (quest turn-in). Reconcile keeps the model in
    step with the DB the way the old reload-every-op code did, but holds each
    surviving item's slot id stable so the client's references stay valid. New DB
    items get fresh slots (the client learns them on the next spawn).
    """
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return
    model = conn.inv_model
    by_key: dict[tuple, list[InvItem]] = {}
    for it in model.main_items():
        by_key.setdefault((it.gc_class, it.x, it.y), []).append(it)

    surviving: set[int] = set()
    for dbit in (saved.inventory or []):
        bucket = by_key.get((dbit.gc_class, dbit.x, dbit.y))
        if bucket:
            existing = bucket.pop(0)
            existing.count = dbit.count
            existing.rarity = dbit.rarity
            existing.stored_level = dbit.stored_level
            existing.buy_price = getattr(dbit, "buy_price", 0)
            existing.scale_mod = getattr(dbit, "scale_mod", "")
            existing.mod_refs = list(getattr(dbit, "mod_refs", None) or [])
            surviving.add(existing.slot_id)
        else:
            added = model.add(
                dbit.gc_class, dbit.x, dbit.y, count=dbit.count, rarity=dbit.rarity,
                stored_level=dbit.stored_level, buy_price=getattr(dbit, "buy_price", 0),
                scale_mod=getattr(dbit, "scale_mod", ""),
                mod_refs=list(getattr(dbit, "mod_refs", None) or []),
            )
            surviving.add(added.slot_id)

    # Drop main-inventory entries no longer in the DB (removed out-of-band).
    for slot_id in [it.slot_id for it in model.main_items() if it.slot_id not in surviving]:
        model.remove(slot_id)


def _persist(conn: "RRConnection") -> None:
    """Write the model's main inventory back to SQLite."""
    from ..data.saved_character import SavedInventoryItem

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return
    saved.inventory = [
        SavedInventoryItem(
            gc_class=it["gc_class"], x=it["x"], y=it["y"], count=it["count"],
            buy_price=it.get("buy_price", 0), rarity=it.get("rarity", -1),
            stored_level=it.get("stored_level", -1),
            scale_mod=it.get("scale_mod", ""),
            mod_refs=list(it.get("mod_refs") or []),
        )
        for it in conn.inv_model.to_saved()
    ]
    character_repository.save_character(saved)


# ── Item / currency grants (reusable by quests, loot, admin) ─────────────────

def give_gold(conn: "RRConnection", amount: int) -> int:
    """Credit gold to the character and ship a live ``0x20`` AddCurrency — port of
    C# ``GiveGold``.

    The DB credit always happens (so a relog is consistent); the live update is
    sent only once the UnitContainer component id is known. Returns the amount
    credited.
    """
    amount = int(amount)
    if amount <= 0:
        return 0
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is not None:
        saved.gold = (saved.gold or 0) + amount
        character_repository.save_character(saved)
    if not getattr(conn, "unit_container_id", 0):
        return amount
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x35)
    w.write_uint16(conn.unit_container_id)
    w.write_byte(0x20)                        # AddCurrency
    w.write_uint32(amount & 0xFFFFFFFF)
    w.write_byte(0x00)
    w.write_uint32(0x00000000)
    w.write_byte(0x01)
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())
    return amount


def give_stacked_item(conn: "RRConnection", gc_class: str, total_count: int = 1,
                      max_stack: int = 100) -> int:
    """Give ``total_count`` of ``gc_class``, topping up existing stacks first
    (``0x22`` UpdateQuantity) then filling free slots (``0x1E`` ItemAdd) — port of
    C# ``GiveStackedItem``.

    All updates ride one BeginStream/EndStream (each its own synch-terminated
    ComponentUpdate); the model is persisted afterwards. Returns the number
    actually given (short of ``total_count`` when the bag fills up). A no-op
    without an inventory model / UnitContainer id (returns 0).
    """
    total = int(total_count)
    if total <= 0 or not gc_class:
        return 0
    model = getattr(conn, "inv_model", None)
    if model is None or not getattr(conn, "unit_container_id", 0):
        return 0
    max_stack = max(1, int(max_stack))
    remaining = total
    given = 0
    target = gc_class.lower()

    w = LEWriter()
    w.write_byte(0x07)                        # BeginStream
    any_update = False

    if max_stack > 1:
        for it in model.main_items():
            if remaining <= 0:
                break
            if it.gc_class.lower() != target or it.count >= max_stack:
                continue
            can_add = min(max_stack - it.count, remaining)
            it.count += can_add
            remaining -= can_add
            given += can_add
            any_update = True
            w.write_byte(0x35)
            w.write_uint16(conn.unit_container_id)
            w.write_byte(0x22)                # UpdateQuantity
            w.write_uint32(it.slot_id)
            w.write_byte(min(0xFF, it.count))
            write_synch(w, synch_hp(conn))

    width, height = _get_item_size(gc_class)
    while remaining > 0:
        slot_xy = _find_free_slot(model, width, height)
        if slot_xy is None:
            log.warn(f"[GIVE-STACKED] '{getattr(conn, 'login_name', '?')}' "
                        f"inventory full — {remaining}x {gc_class} not given")
            break
        x, y = slot_xy
        stack = min(remaining, max_stack)
        remaining -= stack
        given += stack
        new_item = model.add(gc_class, x, y, count=stack)
        _write_item_added(w, conn, new_item, 1)
        any_update = True

    w.write_byte(0x06)                        # EndStream
    if any_update:
        conn.send_to_client(w.to_array())
        _persist(conn)
    return given


def count_items_by_gc(conn: "RRConnection", gc_class: str) -> int:
    """Total quantity of ``gc_class`` held in the main inventory (summing stacks).

    Quest item objectives ("have N King's Coins") are satisfied by *holding* the
    item, so the truth is the bag — not a pickup counter. Case-insensitive.
    Returns 0 without an inventory model.
    """
    model = getattr(conn, "inv_model", None)
    if model is None or not gc_class:
        return 0
    target = gc_class.lower()
    return sum(max(0, it.count) for it in model.main_items()
               if it.gc_class.lower() == target)


def remove_items_by_gc(conn: "RRConnection", gc_class: str, count: int) -> int:
    """Remove up to ``count`` of ``gc_class`` from the main inventory, decrementing
    partial stacks (``0x22`` UpdateQuantity) and removing emptied slots (``0x1F``
    ItemRemoved) — port of C# ``RemoveQuestItemsFromInventory`` (RemoveOnFinalize).

    Returns the number actually removed. All wire updates ride one
    BeginStream/EndStream; the model is persisted afterwards. Without a
    UnitContainer id the model is still mutated + persisted (no live send).
    """
    model = getattr(conn, "inv_model", None)
    count = int(count)
    if model is None or count <= 0 or not gc_class:
        return 0
    target = gc_class.lower()
    has_uc = bool(getattr(conn, "unit_container_id", 0))
    remaining = count
    removed = 0

    w = LEWriter()
    w.write_byte(0x07)                        # BeginStream
    any_update = False
    for it in list(model.main_items()):
        if remaining <= 0:
            break
        if it.gc_class.lower() != target:
            continue
        if it.count > remaining:
            it.count -= remaining
            removed += remaining
            remaining = 0
            if has_uc:
                w.write_byte(0x35)
                w.write_uint16(conn.unit_container_id)
                w.write_byte(0x22)            # UpdateQuantity
                w.write_uint32(it.slot_id)
                w.write_byte(min(0xFF, it.count))
                write_synch(w, synch_hp(conn))
                any_update = True
        else:
            removed += it.count
            remaining -= it.count
            slot = it.slot_id
            model.remove(slot)
            if has_uc:
                _write_item_removed(w, conn, slot)
                any_update = True
    w.write_byte(0x06)                        # EndStream
    if any_update:
        conn.send_to_client(w.to_array())
    if removed:
        _persist(conn)
    return removed


# ── Public handler functions ────────────────────────────────────────────────

def handle_use_item(server: "GameServer", conn: "RRConnection",
                     reader: LEReader) -> bool:
    """Handle 0x25 on UnitContainer — use an item (potion, scroll, skillbook).

    Client format: ``[clientItemId:uint32]`` — the slot id the server assigned.
    """
    if reader.remaining < 4:
        return False

    client_id = reader.read_uint32()
    _reconcile(conn)
    item = conn.inv_model.resolve(client_id)
    if item is None:
        log.warn(f"[INV] conn {conn.conn_id}: use unknown item id {client_id}")
        return True

    gc_class = item.gc_class.lower()
    log.info(f"[INV] conn {conn.conn_id} use '{gc_class}' slot={item.slot_id}")

    if "healthpotion" in gc_class:
        _apply_health_potion(conn, gc_class)
        _consume_one(conn, item, client_id)
        _send_potion_modifier(conn, item.gc_class, is_health=True)
    elif "manapotion" in gc_class:
        _apply_mana_potion(conn, gc_class)
        _consume_one(conn, item, client_id)
        _send_potion_modifier(conn, item.gc_class, is_health=False)
    elif town_portal.is_waypoint_scroll(gc_class):
        # Waypoint / town-portal scroll: consume (the member compass is
        # permanent), then open the blue portal + save the return point.
        log.info(f"[INV] conn {conn.conn_id} used waypoint scroll '{gc_class}'")
        if town_portal.is_consumed_on_use(gc_class):
            _consume_one(conn, item, client_id)
        town_portal.use_waypoint_scroll(server, conn)
    elif "skillbook" in gc_class:
        # The taught skill is granted at character creation; using the book just
        # consumes it gracefully (the C# fall-through to the potion path here
        # wrote a malformed packet that crashed the client with "message type 0").
        _consume_one(conn, item, client_id)
        short = item.gc_class.rsplit(".", 1)[-1]
        _send_system_message(conn, f"You already know {short}; the book vanishes.")
    else:
        log.info(f"[INV] conn {conn.conn_id} used unhandled item: {gc_class}")

    return True


def handle_pickup_item(server: "GameServer", conn: "RRConnection",
                        reader: LEReader) -> bool:
    """Handle 0x28 on UnitContainer — pick an item up onto the cursor.

    Client format: ``[clientItemId:uint32]``. No item data — the server already
    knows the item from its slot map.
    """
    if reader.remaining < 4:
        return False

    client_id = reader.read_uint32()
    _reconcile(conn)
    model = conn.inv_model
    if model.cursor is not None:
        log.info(f"[INV] conn {conn.conn_id}: already holding '{model.cursor.gc_class}'")
        return True

    item = model.resolve(client_id)
    if item is None:
        log.warn(f"[INV] conn {conn.conn_id}: pickup unknown item id {client_id}")
        return True

    model.remove(item.slot_id)
    model.cursor = CursorItem(
        gc_class=item.gc_class, count=item.count,
        rarity=item.rarity, stored_level=item.stored_level, buy_price=item.buy_price,
        scale_mod=item.scale_mod, mod_refs=list(item.mod_refs),
    )
    _persist(conn)
    log.info(f"[INV] conn {conn.conn_id} pickup '{item.gc_class}' x{item.count} "
             f"from ({item.x},{item.y})")

    level = item.stored_level if item.stored_level >= 0 else conn.player_level
    w = LEWriter()
    w.write_byte(0x07)
    # Remove the item from its old slot (use the id the client sent).
    _write_item_removed(w, conn, client_id)
    # Put it on the cursor.
    _write_set_active(w, conn, item.gc_class, item.count, level)
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())
    return True


def handle_place_item(server: "GameServer", conn: "RRConnection",
                       reader: LEReader) -> bool:
    """Handle 0x29 on UnitContainer — place the cursor item into the grid.

    Client format: ``[inventoryID:byte] [x:byte] [y:byte]`` — no item data; the
    item is whatever is on the cursor.
    """
    if reader.remaining < 3:
        return False

    inv_id = reader.read_byte()
    x = reader.read_byte()
    y = reader.read_byte()

    _reconcile(conn)
    model = conn.inv_model
    cursor = model.cursor
    if cursor is None:
        log.info(f"[INV] conn {conn.conn_id}: place with empty cursor at ({x},{y})")
        return True

    log.info(f"[INV] conn {conn.conn_id} place '{cursor.gc_class}' at ({x},{y}) inv={inv_id}")

    if inv_id != MAIN_INVENTORY:
        log.debug(f"[INV] non-main inventory {inv_id} ignored")
        return True

    w_size, h_size = _get_item_size(cursor.gc_class)
    if x < 0 or y < 0 or x + w_size > _INVENTORY_COLS or y + h_size > _INVENTORY_ROWS:
        log.warn(f"[INV] '{cursor.gc_class}' ({w_size}x{h_size}) out of bounds at ({x},{y})")
        return True

    if model.occupied(x, y, w_size, h_size, _get_item_size, container=inv_id):
        log.warn(f"[INV] slot ({x},{y}) occupied for '{cursor.gc_class}'")
        return True

    new_item = model.add(
        cursor.gc_class, x, y, count=cursor.count, rarity=cursor.rarity,
        stored_level=cursor.stored_level, buy_price=cursor.buy_price,
        scale_mod=cursor.scale_mod, mod_refs=list(cursor.mod_refs), container=inv_id,
    )
    model.cursor = None
    _persist(conn)

    level = cursor.stored_level if cursor.stored_level >= 0 else conn.player_level
    # Place response = 0x29 ClearActive (free the cursor) then 0x1E ItemAdd (put
    # the item in the bag slot), matching C# HandlePlaceItemInInventory. The
    # 0x29 ClearActive is the same primitive equip uses to free the cursor on a
    # no-swap equip (live-confirmed working 2026-06-08), so it's safe here; an
    # earlier theory that it "over-consumed" was wrong — the real crashes were an
    # unset manipulators_component_id and a spurious extra flag byte, both fixed.
    # Without the 0x29 the item lands in the bag but the cursor stays stuck.
    w = LEWriter()
    w.write_byte(0x07)
    _write_clear_active(w, conn)
    _write_item_added(w, conn, new_item, level)
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())
    return True


def handle_drop_item(server: "GameServer", conn: "RRConnection",
                      reader: LEReader) -> bool:
    """Handle 0x23 on UnitContainer — drop the cursor item onto the ground.

    Port of C# ``InventoryHandler.HandleDropItem``: one stream carrying the
    0x29 ClearActive ack (frees the cursor) plus an ``itemobject`` entity
    create at the player's position, then the bare create broadcast to every
    other player in the zone instance. The drop is registered in the loot
    tracker so clicking it picks it back up.

    The entity-create type MUST be the world-object class ``itemobject`` —
    writing the item's own GC class there is the client's ``processEntityCreate
    ERROR: Invalid entity type`` → "zone communication error code 7" crash.
    The item's real GC class goes inside the init body
    (``write_init_for_dropped_item``).
    """
    model = conn.inv_model
    cursor = model.cursor
    if cursor is None:
        log.info(f"[INV] conn {conn.conn_id}: drop with empty cursor")
        return True

    entity_id = loot.next_drop_entity_id()
    fx = int(conn.player_pos_x * 256)
    fy = int(conn.player_pos_y * 256)
    # C# Z bias: player Z is foot-level, which on slopes sits AT the surface —
    # an exact-Z drop clips under the geometry. Spawn slightly above and let the
    # client's FlipController animation settle it onto the floor. conn.player_pos_z
    # is stale (no Z in movement records) so resolve the REAL floor from the zone
    # pathmap, falling back to the stale Z when uncovered (live 2026-07-02).
    drop_z = loot.ground_z_at(conn, conn.player_pos_x, conn.player_pos_y,
                              conn.player_pos_z) + 1.0
    fz = int(drop_z * 256)

    gc_obj = _make_ground_gc_object(cursor)
    level = conn.player_level

    w = LEWriter()
    w.write_byte(0x07)
    _write_clear_active(w, conn)
    loot.write_create_and_position(w, entity_id, fx, fy, fz)
    gc_obj.write_init_for_dropped_item(w, level)
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())

    loot.register_drop(loot.DroppedItem(
        entity_id=entity_id, gc_class=cursor.gc_class,
        count=max(1, cursor.count), rarity=cursor.rarity,
        stored_level=cursor.stored_level, scale_mod=cursor.scale_mod,
        pos_x=conn.player_pos_x, pos_y=conn.player_pos_y, pos_z=drop_z,
        zone_gc_type=getattr(conn, "current_zone_gc_type", "") or "",
        instance_id=getattr(conn, "instance_id", 0),
    ))
    log.info(f"[INV] conn {conn.conn_id} dropped '{cursor.gc_class}' x{cursor.count} "
             f"as entity 0x{entity_id:04X}")
    model.cursor = None

    # Broadcast the same create (without the ClearActive) to instance peers.
    wb = LEWriter()
    wb.write_byte(0x07)
    loot.write_create_and_position(wb, entity_id, fx, fy, fz)
    gc_obj.write_init_for_dropped_item(wb, level)
    wb.write_byte(0x06)
    _broadcast_to_instance(server, conn, wb.to_array())
    return True


def handle_ground_pickup(server: "GameServer", conn: "RRConnection",
                          component_id: int, target_eid: int,
                          response_id: int, session_id: int) -> bool:
    """Pick up a tracked ground drop the player clicked (activate 0x06).

    Port of C# ``HandleItemRightClickPickup`` — the DR client sends the same
    actionType 0x06 for every click on a dropped item, so all pickups auto-bag:
    gold piles credit currency, stackable simple items top up an existing
    stack, everything else lands in the first free bag slot. A full bag leaves
    the item on the ground (ack only + "inventory is full"), so the player
    never loses it.
    """
    peek = loot.find_drop(target_eid)
    if peek is None:
        return False

    # ── Range gate. Neither the C# emulator nor the client enforces one, so a
    # click bagged loot from across the whole zone. Reject out-of-range clicks
    # but ALWAYS ack the action or the client's action state machine wedges
    # (same rule as the bag-full path). Radius is a server knob; the default is
    # an UNVERIFIED gameplay choice, not a client-derived constant.
    pickup_range = settings.get_float("groundPickupRange", 150.0)
    dx = peek.pos_x - conn.player_pos_x
    dy = peek.pos_y - conn.player_pos_y
    if pickup_range > 0 and (dx * dx + dy * dy) > pickup_range * pickup_range:
        w = LEWriter()
        w.write_byte(0x07)
        _write_activate_ack(w, conn, component_id, target_eid,
                            response_id, session_id)
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())
        _send_system_message(conn, "You are too far away to pick that up.")
        log.info(f"[PICKUP] conn {conn.conn_id} too far from drop {target_eid} "
                 f"(dist^2={dx * dx + dy * dy:.0f}, range={pickup_range:.0f})")
        return True

    info = loot.remove_drop(target_eid)
    if info is None:
        return False

    _reconcile(conn)

    if info.gold_amount > 0:
        return _pickup_gold(server, conn, component_id, target_eid,
                            response_id, session_id, info)

    model = conn.inv_model
    lower_gc = info.gc_class.lower()

    # ── Stack merge: top up an existing stack of the same simple item ──
    # Quest items are excluded (each pickup needs its own slot so the quest
    # objective counter fires); equipment never merges (per-instance stats).
    if _is_simple_item(info.gc_class) and "questitem" not in lower_gc:
        for it in model.main_items():
            if it.gc_class.lower() != lower_gc:
                continue
            if it.count >= _MAX_STACK:
                continue                      # this stack is full, try the next
            new_count = it.count + info.count
            it.count = new_count
            _persist(conn)

            w = LEWriter()
            w.write_byte(0x07)
            _write_activate_ack(w, conn, component_id, target_eid,
                                response_id, session_id)
            w.write_byte(0x05)                # remove the ground entity
            w.write_uint16(target_eid)
            # 0x22 UpdateQuantity (client processUpdateQuantity@0x57dc50:
            # u32 itemSlotId + u8 newQuantity -> item+0x82).
            w.write_byte(0x35)
            w.write_uint16(conn.unit_container_id)
            w.write_byte(0x22)
            w.write_uint32(it.slot_id)
            w.write_byte(min(0xFF, new_count))
            write_synch(w, synch_hp(conn))
            w.write_byte(0x06)
            conn.send_to_client(w.to_array())

            _despawn_for_others(server, conn, target_eid)
            _notify_quest_item(server, conn, info.gc_class)
            log.info(f"[PICKUP] conn {conn.conn_id} merged '{info.gc_class}' "
                     f"into slot {it.slot_id} -> x{new_count}")
            return True

    # ── New stack / equipment: place into the first free bag slot ──
    w_size, h_size = _get_item_size(info.gc_class)
    slot_xy = _find_free_slot(model, w_size, h_size)
    if slot_xy is None:
        # Bag full — leave the drop on the ground but ALWAYS ack the action,
        # or the client's action state machine wedges (can't move).
        loot.register_drop(info)
        _send_system_message(conn, "Your inventory is full!")
        w = LEWriter()
        w.write_byte(0x07)
        _write_activate_ack(w, conn, component_id, target_eid,
                            response_id, session_id)
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())
        log.info(f"[PICKUP] conn {conn.conn_id} bag full for '{info.gc_class}'")
        return True

    x, y = slot_xy
    new_item = model.add(
        info.gc_class, x, y, count=max(1, info.count), rarity=info.rarity,
        stored_level=info.stored_level, scale_mod=getattr(info, "scale_mod", ""),
    )
    _persist(conn)

    level = info.stored_level if info.stored_level >= 0 else conn.player_level
    w = LEWriter()
    w.write_byte(0x07)
    _write_activate_ack(w, conn, component_id, target_eid,
                        response_id, session_id)
    w.write_byte(0x05)                        # remove the ground entity
    w.write_uint16(target_eid)
    _write_clear_active(w, conn)              # defensive cursor clear (C# Part 3)
    _write_item_added(w, conn, new_item, level)
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())

    _despawn_for_others(server, conn, target_eid)
    _notify_quest_item(server, conn, info.gc_class)
    log.info(f"[PICKUP] conn {conn.conn_id} bagged '{info.gc_class}' "
             f"x{new_item.count} at ({x},{y}) slot={new_item.slot_id}")
    return True


def _pickup_gold(server: "GameServer", conn: "RRConnection",
                 component_id: int, target_eid: int,
                 response_id: int, session_id: int,
                 info: "loot.DroppedItem") -> bool:
    """Gold-pile pickup — credit the character and ship 0x20 AddCurrency."""
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is not None:
        saved.gold += info.gold_amount
        character_repository.save_character(saved)

    w = LEWriter()
    w.write_byte(0x07)
    _write_activate_ack(w, conn, component_id, target_eid,
                        response_id, session_id)
    w.write_byte(0x05)                        # remove the ground entity
    w.write_uint16(target_eid)
    if conn.unit_container_id:
        w.write_byte(0x35)
        w.write_uint16(conn.unit_container_id)
        w.write_byte(0x20)                    # AddCurrency
        w.write_uint32(info.gold_amount)
        w.write_byte(0x00)
        w.write_uint32(0x00000000)
        w.write_byte(0x01)
        write_synch(w, synch_hp(conn))
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())

    _despawn_for_others(server, conn, target_eid)
    log.info(f"[PICKUP] conn {conn.conn_id} +{info.gold_amount} gold "
             f"(entity 0x{target_eid:04X})")
    return True


# ── Wire builders (sub-messages within an outer BeginStream/EndStream) ──

def _write_item_removed(w: LEWriter, conn: "RRConnection", slot_id: int) -> None:
    """0x1F ItemRemoved — uint32 slot."""
    w.write_byte(0x35)
    w.write_uint16(conn.unit_container_id)
    w.write_byte(0x1F)
    w.write_uint32(slot_id)
    write_synch(w, synch_hp(conn))


def _write_item_added(w: LEWriter, conn: "RRConnection", item: InvItem, level: int) -> None:
    """0x1E ItemAdd — inventoryID + item serialization (with its slot id)."""
    w.write_byte(0x35)
    w.write_uint16(conn.unit_container_id)
    w.write_byte(0x1E)
    w.write_byte(item.container)
    if _is_simple_item(item.gc_class):
        w.write_byte(0xFF)
        w.write_cstring(get_packet_gc_class_for(item.gc_class))
        w.write_uint32(item.slot_id)
        w.write_byte(item.x)
        w.write_byte(item.y)
        w.write_byte(min(0xFF, max(1, item.count)))
        w.write_byte(0x01)               # level
        w.write_byte(0x00)               # flags (simple item)
        _write_transient_mod_byte(w, item.gc_class)
        w.write_byte(0x00)               # ReadChildData<ItemModifier> count = 0
    else:
        # Native class drives the weapon/armor flag-byte rule in
        # write_init_for_inventory; pick it from the gc class so armor isn't
        # serialized as a weapon (ranged weapons → RangedWeapon, else armor).
        gc_obj = GCObject(native_class=_native_class_for(item.gc_class),
                          gc_class=item.gc_class, name="")
        gc_obj.stored_rarity = item.rarity
        gc_obj.stored_level = item.stored_level
        gc_obj.preset_scale_mod = item.scale_mod or None
        gc_obj.preset_mod_refs = list(item.mod_refs)
        gc_obj.write_init_for_inventory(w, item.x, item.y, item.slot_id, level,
                                        count=item.count)
    write_synch(w, synch_hp(conn))


def _native_class_for(gc_class: str) -> str:
    """Best-effort NativeClass for a bag equipment item from its gc class."""
    lower = gc_class.lower()
    if any(t in lower for t in ("bow", "gun", "crossbow", "cannon", "rifle", "pistol")):
        return "RangedWeapon"
    if any(t in lower for t in ("sword", "axe", "mace", "pick", "staff", "dagger",
                                "hammer", "spear", "club", "katana", "polearm",
                                "wand", "scepter")):
        return "MeleeWeapon"
    return "Armor"


def _write_set_active(w: LEWriter, conn: "RRConnection", gc_class: str,
                      count: int, level: int) -> None:
    """0x28 SetActiveItem — put the item on the cursor."""
    w.write_byte(0x35)
    w.write_uint16(conn.unit_container_id)
    w.write_byte(0x28)
    if _is_simple_item(gc_class):
        w.write_byte(0xFF)
        w.write_cstring(get_packet_gc_class_for(gc_class))
        w.write_uint32(0x00)             # cursor has no grid slot
        w.write_byte(0x00)
        w.write_byte(0x00)
        w.write_byte(min(0xFF, max(1, count)))
        w.write_byte(0x01)               # level
        w.write_byte(0x00)               # flags
        _write_transient_mod_byte(w, gc_class)
        w.write_byte(0x00)               # mod count
    else:
        item = gc_object_factory.create_equipment_item(gc_class)
        item.write_init_without_weapon_bytes(w, level)
    write_synch(w, synch_hp(conn))


def _write_clear_active(w: LEWriter, conn: "RRConnection") -> None:
    """0x29 ClearActiveItem — empty the cursor."""
    w.write_byte(0x35)
    w.write_uint16(conn.unit_container_id)
    w.write_byte(0x29)
    write_synch(w, synch_hp(conn))


def _consume_one(conn: "RRConnection", item: InvItem, client_id: int) -> None:
    """Decrement an item's stack by one and sync the client (0x1F, then 0x1E if
    any remain). Persists the model to SQLite."""
    item.count -= 1
    w = LEWriter()
    w.write_byte(0x07)
    _write_item_removed(w, conn, client_id)
    if item.count > 0:
        _write_item_added(w, conn, item, conn.player_level)
    else:
        conn.inv_model.remove(item.slot_id)
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())
    _persist(conn)


# ── Potion buff modifier (heal/mana-over-time + the use animation) ──
#
# Port of C# InventoryHandler.SendPotionModifier. The visible potion effect —
# the "regenerate 45% HP/MP over 5s" buff AND its animation — is a Modifier the
# server applies to the player's Modifiers component (a 0x35/0x00 Add). HP/MP are
# client-authoritative, so the client runs the regen itself once the modifier is
# on; we just add it and pull it back off after its 5s GC duration (a wire
# duration of 0 means INFINITE on the client, so the timed remove is required).
_POTION_MOD_DURATION_S = 5.0
_next_mod_instance_id = 1


def _send_potion_modifier(conn: "RRConnection", item_gc_class: str,
                          *, is_health: bool) -> None:
    """Add the potion's `<item>.Modifier` buff, then schedule its removal."""
    modifiers_id = getattr(conn, "modifiers_id", 0)
    if not modifiers_id:
        return

    global _next_mod_instance_id
    attr = "_active_health_mod_id" if is_health else "_active_mana_mod_id"

    # Replace any still-active modifier of the same kind (re-quaff refreshes it).
    old_id = getattr(conn, attr, 0)
    if old_id:
        _send_modifier_remove(conn, modifiers_id, old_id)

    instance_id = _next_mod_instance_id
    _next_mod_instance_id += 1
    mod_gc = item_gc_class + ".Modifier"   # client hashes case-insensitively

    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x35)
    w.write_uint16(modifiers_id)
    w.write_byte(0x00)                     # Add modifier
    w.write_byte(0xFF)
    w.write_cstring(mod_gc)
    w.write_uint32(instance_id)
    w.write_byte(0x00)                     # level
    w.write_uint32(0x00)                   # power level
    w.write_uint32(0x00)                   # duration (0 = client-infinite; we time it)
    w.write_byte(0x00)                     # source-is-self
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())
    setattr(conn, attr, instance_id)
    log.info(f"[INV] conn {conn.conn_id} potion modifier '{mod_gc}' id={instance_id}")

    # Schedule the timed removal (best-effort; no-op if no loop is running).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(_POTION_MOD_DURATION_S,
                    _expire_potion_modifier, conn, modifiers_id, instance_id, attr)


def _send_modifier_remove(conn: "RRConnection", modifiers_id: int, instance_id: int) -> None:
    """Send a Modifiers 0x01 Remove for the given instance id."""
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x35)
    w.write_uint16(modifiers_id)
    w.write_byte(0x01)                     # Remove modifier
    w.write_uint32(instance_id)
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())


def _expire_potion_modifier(conn: "RRConnection", modifiers_id: int,
                            instance_id: int, attr: str) -> None:
    """Remove a potion modifier once its duration elapses (if not already replaced)."""
    if getattr(conn, attr, 0) != instance_id:
        return                              # superseded by a newer quaff
    _send_modifier_remove(conn, modifiers_id, instance_id)
    setattr(conn, attr, 0)


# ── Consumable effects ──

def _apply_health_potion(conn: "RRConnection", gc_class: str) -> None:
    """Heal the player's persisted HP (client owns live HP; this is for save)."""
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return
    heal = 200 * 256
    if "major" in gc_class or "noob" in gc_class:
        heal = 500 * 256
    max_hp = saved.max_hp or 200 * 256
    saved.current_hp = min(max_hp, (saved.current_hp or max_hp) + heal)
    character_repository.save_character(saved)
    log.info(f"[INV] '{conn.login_name}' used health potion, hp={saved.current_hp // 256}")


def _apply_mana_potion(conn: "RRConnection", gc_class: str) -> None:
    """Restore the player's persisted MP."""
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return
    heal = 200 * 256
    if "major" in gc_class:
        heal = 500 * 256
    max_mp = saved.max_mana or 200 * 256
    saved.current_mana = min(max_mp, (saved.current_mana or max_mp) + heal)
    character_repository.save_character(saved)
    log.info(f"[INV] '{conn.login_name}' used mana potion, mp={saved.current_mana // 256}")


def _send_system_message(conn: "RRConnection", text: str) -> None:
    """Best-effort system chat line; never raises if chat isn't wired."""
    try:
        from . import chat_commands
        chat_commands._send_chat(conn, text)
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def _make_ground_gc_object(cursor: CursorItem) -> GCObject:
    """Build the GCObject that serializes a cursor item into a ground drop."""
    if _is_simple_item(cursor.gc_class):
        gc_obj = GCObject(native_class="ActiveItem", gc_class=cursor.gc_class,
                          name="")
    else:
        gc_obj = gc_object_factory.create_equipment_item(cursor.gc_class)
    gc_obj.stored_rarity = cursor.rarity
    gc_obj.stored_level = cursor.stored_level
    gc_obj.preset_scale_mod = cursor.scale_mod or None
    gc_obj.preset_mod_refs = list(cursor.mod_refs)
    return gc_obj


def _find_free_slot(model, w_size: int, h_size: int) -> "tuple[int, int] | None":
    """First free main-bag grid position for a w×h item (C# FindNextFreeInventorySlot)."""
    for y in range(_INVENTORY_ROWS - h_size + 1):
        for x in range(_INVENTORY_COLS - w_size + 1):
            if not model.occupied(x, y, w_size, h_size, _get_item_size,
                                  container=MAIN_INVENTORY):
                return x, y
    return None


def _write_activate_ack(w: LEWriter, conn: "RRConnection", component_id: int,
                        target_eid: int, response_id: int,
                        session_id: int) -> None:
    """ActionResponse acknowledging a 0x06 BehaviourActionActivate click."""
    w.write_byte(0x35)
    w.write_uint16(component_id)
    w.write_byte(0x01)                        # ActionResponse
    w.write_byte(response_id)
    w.write_byte(0x06)                        # BehaviourActionActivate
    w.write_byte(session_id)
    w.write_uint16(target_eid)
    write_synch(w, synch_hp(conn))


def _broadcast_to_instance(server: "GameServer", conn: "RRConnection",
                           packet: bytes) -> None:
    """Send to every OTHER spawned player sharing conn's zone instance."""
    for other in list(server.connections.values()):
        if other is conn or not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue
        other.send_to_client(packet)


def _despawn_for_others(server: "GameServer", conn: "RRConnection",
                        entity_id: int) -> None:
    """Remove a picked-up ground entity for instance peers (C# SendDespawnEntity)."""
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x05)
    w.write_uint16(entity_id)
    w.write_byte(0x06)
    _broadcast_to_instance(server, conn, w.to_array())


def _notify_quest_item(server: "GameServer", conn: "RRConnection",
                       gc_class: str) -> None:
    """Advance item-type quest objectives on pickup (C# NotifyQuestItemAcquired)."""
    quests = getattr(server, "quests", None)
    if quests is not None and gc_class:
        quests.on_item_picked_up(conn, gc_class)
