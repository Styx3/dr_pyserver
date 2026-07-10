"""Loot manager — monster death drops (gold piles + dropped items).

Ported from C# DR-Server: ``UnityGameServer.SendGoldPileSpawnPacket`` /
``SendDroppedItemSpawnPacket`` + ``GCObject.WriteInitForDroppedItem``.

GROUND-TRUTH FIX (2026-06-02): the earlier MVP wrote the *item's* GC class
(``items.base.Box`` / the potion class) as the **entity-create type**, which the
live client rejects — ``processEntityCreate ERROR: Invalid entity type for
EntityID(0xC000)`` ("zone communication error 7"), a hard crash. The client's
entity factory needs the create type to be the world-object class **`itemobject`**;
the item's real GC class belongs *inside* the init body (``WriteInitForDroppedItem``).
This was dormant until ROUTE 2B made kills fire live.

Scope: gold piles + a placeholder consumable item drop, both using the exact C#
create+init layout the client accepts. Real item selection from treasure
generators (rarity/modifier rolling, equipment-format items) is still Phase 9 —
this just stops the crash and drops client-valid ground objects.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core import log
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection


# Dropped-item entity ids live in their own ushort block (C# 0xC000–0xFDFF)
# so they never collide with avatars/monsters/world entities.
_DROPPED_ITEM_ID_BASE = 0xC000
_DROPPED_ITEM_ID_MAX = 0xFDFF
_next_dropped_id = _DROPPED_ITEM_ID_BASE


@dataclass
class DroppedItem:
    """One tracked ground drop (C# ``DroppedItemInfo``).

    The registry below is what makes a drop *clickable*: the activate-action
    handler looks the clicked entity id up here and routes it to the pickup
    handler. ``gold_amount > 0`` marks a gold pile (``gc_class`` unused).
    """
    entity_id: int
    gc_class: str = ""
    count: int = 1
    rarity: int = -1
    stored_level: int = -1
    scale_mod: str = ""      # ScaleMod rolled at acquire ("" = deterministic)
    gold_amount: int = 0
    pos_x: float = 0.0
    pos_y: float = 0.0
    pos_z: float = 0.0
    zone_gc_type: str = ""
    instance_id: int = 0


# entity_id -> DroppedItem (C# UnityGameServer._droppedItems).
_dropped_items: Dict[int, DroppedItem] = {}


def register_drop(drop: DroppedItem) -> None:
    """Track a ground drop so a click on its entity id can pick it up."""
    _dropped_items[drop.entity_id] = drop


def find_drop(entity_id: int) -> Optional[DroppedItem]:
    return _dropped_items.get(entity_id)


def remove_drop(entity_id: int) -> Optional[DroppedItem]:
    """Claim a drop for pickup — returns it and removes it from tracking."""
    return _dropped_items.pop(entity_id, None)


def drops_near(zone_gc_type: str, instance_id: int, x: float, y: float,
               radius: float) -> List[tuple]:
    """All tracked drops within ``radius`` of (x, y) in one zone instance, as
    ``(entity_id, DroppedItem)`` pairs (DRS-NET ``GetDroppedItemsNear`` — the
    Bling Gnome's gold-sniff / convert search pulse)."""
    radius_sq = radius * radius
    found: List[tuple] = []
    for eid, drop in _dropped_items.items():
        if drop.zone_gc_type != zone_gc_type or drop.instance_id != instance_id:
            continue
        dx, dy = drop.pos_x - x, drop.pos_y - y
        if dx * dx + dy * dy <= radius_sq:
            found.append((eid, drop))
    return found

# The world-object entity class the client's factory creates for ground drops.
# The item's OWN gc class goes inside the init body, NOT here (see module note).
_GROUND_ENTITY_CLASS = "itemobject"

# Gold-pile item gc — the native coin pile is the ``Currency`` item (renders the
# GroundObject_Money coins), per the C# SendGoldPileSpawnPacket. The gold amount
# is encoded at the packet tail. (``ww00_gb01`` = "Gold Ingot" and ``items.base.Box``
# = EntityObject were both wrong — see _build_gold_pile_packet.)
_GOLD_PILE_ITEM_CLASS = "Currency"

# Placeholder dropped item (consumable simple format; real generator-driven item
# selection is Phase 9). Lowercased == its packet gc class (GetPacketGCClassFor).
_DEFAULT_ITEM_CLASS = "items.consumables.consumable_minorhealthpotion"

# Faithfully ported from C# but NOT yet live-verified — flip False to disable the
# ground-entity create if a live run still rejects it.
_SPAWN_GROUND_ENTITIES = True


def next_drop_entity_id() -> int:
    """Allocate the next ground-drop entity id (wraps inside the loot block)."""
    global _next_dropped_id
    eid = _next_dropped_id
    _next_dropped_id += 1
    if _next_dropped_id > _DROPPED_ITEM_ID_MAX:
        _next_dropped_id = _DROPPED_ITEM_ID_BASE
    return eid


def write_create_and_position(w: LEWriter, entity_id: int,
                              fx: int, fy: int, fz: int) -> None:
    """Common ``itemobject`` create + position/init prefix shared by gold piles
    and item drops (C# SendGoldPileSpawnPacket / SendDroppedItemSpawnPacket /
    InventoryHandler.HandleDropItem)."""
    w.write_byte(0x01)                       # CreateEntity
    w.write_uint16(entity_id)
    w.write_byte(0xFF)
    w.write_cstring(_GROUND_ENTITY_CLASS)

    w.write_byte(0x02)                       # SetPosition / init
    w.write_uint16(entity_id)
    w.write_uint32(0x00000006)               # worldEntityFlags
    w.write_int32(fx)
    w.write_int32(fy)
    w.write_int32(fz)
    w.write_int32(0)                         # heading
    w.write_byte(0xF7)
    w.write_uint16(0x0000)
    w.write_byte(0x00)
    w.write_uint32(0x00000000)
    w.write_byte(0x00)                       # +0x101 state=0 -> FlipController anim
    w.write_uint16(0x2233)
    w.write_uint32(0x00000000)
    w.write_int32(fx)
    w.write_int32(fy)
    w.write_byte(0xBA)


def _build_gold_pile_packet(entity_id: int, pos_x: float, pos_y: float, pos_z: float,
                            gold_amount: int) -> bytes:
    """Gold pile drop — a ``Currency`` item on an ``itemobject`` entity (the
    GroundObject_Money coin pile), with the gold amount at the tail.

    Uses the SAME create+position+drop-controller header as the (working) item
    drop (``write_create_and_position`` — the ``00 2233 .. fx fy BA`` block whose
    repeated ``fx,fy`` is the toss source→dest), so the pile flips out of the mob
    and lands instead of just popping into existence. The C# port
    (``SendGoldPileSpawnPacket``) left gold on a static header (``06 .. 27000 01``,
    no controller) — that and the item path are identical 16-byte slots followed
    by the ``0xFF`` GCObject, so gold can ride the item header verbatim and only
    the trailing GCObject (Currency vs equipment) differs. UNVERIFIED-LIVE: the
    drop-controller header is reverse-engineered, not client-confirmed; if a live
    drop rejects it, revert this to the static ``06`` header.
    """
    fx, fy, fz = int(pos_x * 256), int(pos_y * 256), int(pos_z * 256)
    w = LEWriter()
    w.write_byte(0x07)
    write_create_and_position(w, entity_id, fx, fy, fz)
    # ── Currency GCObject (coin pile + amount) — the gold counterpart of the item
    #    GCObject that follows write_create_and_position in _build_item_packet ──
    w.write_byte(0xFF)
    w.write_cstring(_GOLD_PILE_ITEM_CLASS)        # "Currency"
    w.write_uint32(0)                            # id
    w.write_byte(0x00)                           # invX
    w.write_byte(0x00)                           # invY
    w.write_byte(0x01)                           # qty
    w.write_byte(0x01)                           # level
    w.write_byte(0x00)                           # flags
    w.write_byte(0x00)                           # modCount
    w.write_uint32(gold_amount)                  # gold amount
    w.write_byte(0x06)
    return w.to_array()


def _build_item_packet(entity_id: int, pos_x: float, pos_y: float, pos_z: float,
                       item_gc: str, level: int, rarity: int = -1,
                       scale_mod: str = "") -> bytes:
    """Ground create for a rolled loot item.

    Uses the SAME GCObject serializer the player-drop path uses
    (``GCObject.write_init_for_dropped_item``), which writes the simple form for
    consumables and the full equipment form — INCLUDING the required ScaleMod
    block — for colored/equipment items. The previous hand-rolled "consumable
    simple form" omitted that block for colored PAL gear, so the client desynced
    on the dropped weapon ("zone communication error code 2" + nothing rendered).
    """
    from ..data import gc_object_factory
    fx, fy, fz = int(pos_x * 256), int(pos_y * 256), int(pos_z * 256)
    gc_obj = gc_object_factory.create_equipment_item(item_gc)
    if rarity >= 0:
        gc_obj.stored_rarity = rarity
    gc_obj.stored_level = max(1, level)
    gc_obj.preset_scale_mod = scale_mod or None
    w = LEWriter()
    w.write_byte(0x07)
    write_create_and_position(w, entity_id, fx, fy, fz)
    gc_obj.write_init_for_dropped_item(w, max(1, level))
    w.write_byte(0x06)
    return w.to_array()


def _broadcast(server: "GameServer", conn: "RRConnection", packet: bytes) -> None:
    """Send a ground-drop create to every player sharing the killer's instance
    (the dropping player included — they must see their own drop)."""
    for other in list(server.connections.values()):
        if not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue
        other.send_to_client(packet)


def ground_z_at(conn: "RRConnection", x: float, y: float, fallback: float) -> float:
    """The real floor height at ``(x, y)`` in the player's current zone.

    The client sends NO Z in its movement records (13 bytes: type + heading + x +
    y), so the server's ``conn.player_pos_z`` is STALE — frozen at the zone's
    spawn/entry Z no matter where the player walks (live 2026-07-02: town spawn Z
    49.4 held while the player stood on the 142-high well platform, so ground drops
    spawned ~92u underground; ``@pos`` reports the same stale Z everywhere). The
    per-zone pathmap DOES carry per-cell floor heights (mob spawns already snap to
    it), so resolve the true floor here. Falls back to the caller's stale Z only
    when no pathmap covers the point — identical to the old behavior."""
    from .pathmap import pathmap_manager
    zone = getattr(conn, "current_zone_name", "") or ""
    if not zone:
        return fallback
    inst_id = getattr(conn, "instance_id", 0)
    # One lookup handles both: an "<zone>_inst<id>" key resolves the registered
    # per-instance geometry for a procedural dungeon, and folds back to the static
    # base map for a plain zone (town/tutorial); the plain-zone call is the safety net.
    pm = (pathmap_manager.get(f"{zone}_inst{inst_id}")
          or pathmap_manager.get(zone))
    if pm is None:
        return fallback
    return pm.get_height_at(x, y, default_height=fallback)


def drop_item_near_player(server: "GameServer", conn: "RRConnection",
                          gc_class: str, *, level: Optional[int] = None,
                          count: int = 1, rarity: int = -1, scale_mod: str = "",
                          radius: float = 2.5) -> Optional[int]:
    """Spawn ONE item on the ground at the player's feet and broadcast it.

    The bag-independent reward-drop path: a wishing-well prize (which spits its
    reward onto the floor regardless of bag space — live 2026-07-01) or a quest
    reward that won't fit a full bag reuses the exact client-valid ``itemobject``
    create + FlipController toss the mob-loot / player-drop paths use, then
    registers the drop so the player can click it up. Returns the drop entity id,
    or ``None`` when ground entities are disabled or no ``gc_class`` was given.
    """
    if not _SPAWN_GROUND_ENTITIES or not gc_class:
        return None
    lvl = level if level is not None else max(1, getattr(conn, "player_level", 1))
    px = getattr(conn, "player_pos_x", 0.0) + random.uniform(-radius, radius)
    py = getattr(conn, "player_pos_y", 0.0) + random.uniform(-radius, radius)
    # Resolve the REAL floor Z at the drop point (conn.player_pos_z is stale — no Z
    # in movement), then a slight lift so the FlipController settles the item onto
    # the floor (an exact-Z drop clips under the geometry).
    pz = ground_z_at(conn, px, py, getattr(conn, "player_pos_z", 0.0)) + 1.0
    eid = next_drop_entity_id()
    packet = _build_item_packet(eid, px, py, pz, gc_class, lvl, rarity, scale_mod)
    register_drop(DroppedItem(
        entity_id=eid, gc_class=gc_class, count=max(1, count),
        rarity=rarity, stored_level=lvl, scale_mod=scale_mod,
        pos_x=px, pos_y=py, pos_z=pz,
        zone_gc_type=getattr(conn, "current_zone_gc_type", "") or "",
        instance_id=getattr(conn, "instance_id", 0),
    ))
    _broadcast(server, conn, packet)
    log.info(f"[Loot] dropped reward '{gc_class}' (L{lvl}) at ({px:.0f},{py:.0f}) "
             f"for '{getattr(conn, 'login_name', '?')}' as entity 0x{eid:04X}")
    return eid


def generate_loot_for_monster(server: "GameServer", conn: "RRConnection",
                               pos_x: float, pos_y: float, pos_z: float,
                               level: int,
                               treasure_generators: List[tuple[str, int]],
                               difficulty: str = "") -> None:
    """Roll + spawn level/rarity/difficulty-driven ground drops, then broadcast.

    The drop list is rolled by :mod:`loot_roller` from the mob's treasure
    generators (tier → rarity + count), level (item band) and difficulty (rarity
    nudge). Each item carries its rolled rarity/level/ScaleMod on the
    :class:`DroppedItem` so the pickup materializes the right item. Drops are
    scattered slightly so they don't stack on one point.
    """
    from . import loot_roller

    pool = loot_roller.load_pool()
    armor_pool = loot_roller.load_armor_rarity_pool()
    rolls = loot_roller.roll_loot(treasure_generators, level, difficulty, pool,
                                  armor_pool=armor_pool)
    zone_gc = getattr(conn, "current_zone_gc_type", "") or ""
    instance_id = getattr(conn, "instance_id", 0)

    gold_count = 0
    item_count = 0
    for roll in rolls:
        dx = pos_x + random.uniform(-3, 3)
        dy = pos_y + random.uniform(-3, 3)
        eid = next_drop_entity_id()
        if roll.is_gold:
            gold_count += 1
            packet = _build_gold_pile_packet(eid, dx, dy, pos_z, roll.gold_amount)
            drop = DroppedItem(
                entity_id=eid, gold_amount=roll.gold_amount,
                pos_x=dx, pos_y=dy, pos_z=pos_z,
                zone_gc_type=zone_gc, instance_id=instance_id,
            )
        else:
            item_count += 1
            packet = _build_item_packet(eid, dx, dy, pos_z, roll.gc_type, roll.level,
                                        roll.rarity, roll.scale_mod)
            drop = DroppedItem(
                entity_id=eid, gc_class=roll.gc_type,
                stored_level=roll.level, rarity=roll.rarity, scale_mod=roll.scale_mod,
                pos_x=dx, pos_y=dy, pos_z=pos_z,
                zone_gc_type=zone_gc, instance_id=instance_id,
            )
        if _SPAWN_GROUND_ENTITIES:
            register_drop(drop)
            _broadcast(server, conn, packet)

    suppressed = "" if _SPAWN_GROUND_ENTITIES else " (SUPPRESSED)"
    log.info(f"[Loot] L{level} {difficulty or '?'} dropped {gold_count} gold + "
             f"{item_count} item(s){suppressed} at ({pos_x:.0f},{pos_y:.0f}) "
             f"from {','.join(f'{n}(x{c})' for n, c in treasure_generators) or 'none'}")
