"""Town portal / waypoint scroll — port of the C# town-portal system
(``SpawnTownPortalWithRemoval`` / ``SpawnReturnTownPortal``, UnityGameServer.cs:16685+).

Using a waypoint scroll (gc class containing ``townportal`` / ``waypointscroll``)
spawns a clickable ``items.townportal.TownPortalBlue`` entity in front of the
player that teleports them to town, and saves the cast point as the character's
"Saved Place" (the obelisk menu's return entry + the QuestManager 0x0A return).
Re-entering the source zone re-spawns a visual-only return portal and clears the
saved state — one return trip per scroll.

The TownPortalBlue init layout is NOT the zone-portal one (no spawn-point /
width/height/color trailer): it parents to the owning avatar (initFlags 0x01)
and trails ``[state][u32 0][zone GUID]``. Copied verbatim from the C# reference
— # UNVERIFIED against the live client; confirm on the next live test.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Tuple

from ..core import log
from ..db import character_repository
from ..util.byte_io import LEWriter
from ..data.gc_object import write_gc_type
from .portals import PortalData, portal_manager

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection

TOWN_PORTAL_GC_TYPE = "items.townportal.TownPortalBlue"
TOWN_PORTAL_NAME = "TownPortal"
TOWN_PORTAL_TARGET_ZONE = "town"

_PORTAL_FORWARD_OFFSET = 10.0       # spawn distance in front of the player (C#)
_PORTAL_WIDTH = 3
_PORTAL_HEIGHT = 3
_PORTAL_COLOR = 0x0000FFFF

_FLAGS_CLICKABLE = 0x06             # visible | activatable (owner's portal)
_FLAGS_VISUAL_ONLY = 0x04           # visible only (other players / return portal)
_STATE_MATERIALIZING = 0x01         # plays the spawn animation
_STATE_ACTIVE = 0x02


def is_waypoint_scroll(gc_class_lower: str) -> bool:
    """True for any town-portal consumable or the member waypoint compass."""
    return "townportal" in gc_class_lower or "waypointscroll" in gc_class_lower


def is_consumed_on_use(gc_class_lower: str) -> bool:
    """The member ``scrollpal.permawaypointscroll`` compass is permanent."""
    return "perma" not in gc_class_lower


def build_town_portal_stream(entity_id: int, owner_avatar_id: int,
                             pos: Tuple[float, float, float], target_zone: str,
                             zone_guid: int, *, flags: int, state: int) -> bytes:
    """Serialize one TownPortalBlue entity as a BeginStream…EndStream packet.

    Byte-for-byte port of the spawn body in C# SpawnTownPortalWithRemoval
    (UnityGameServer.cs:16786+). # UNVERIFIED — C#-reference layout.
    """
    w = LEWriter()
    w.write_byte(0x07)                         # BeginStream

    # ── Create portal entity (0x01) ──
    w.write_byte(0x01)
    w.write_uint16(entity_id)
    write_gc_type(w, TOWN_PORTAL_GC_TYPE, preserve_case=True)

    # ── Init portal entity (0x02) — WorldEntity::WriteInit, parented variant ──
    w.write_byte(0x02)
    w.write_uint16(entity_id)
    w.write_uint32(flags)
    w.write_int32(int(pos[0] * 256))
    w.write_int32(int(pos[1] * 256))
    w.write_int32(int(pos[2] * 256))
    w.write_int32(0)                           # heading
    w.write_byte(0x01)                         # initFlags: hasParent
    w.write_uint16(owner_avatar_id & 0xFFFF)   # parent = owning avatar
    w.write_cstring(target_zone)
    w.write_cstring("")
    w.write_byte(state)
    w.write_uint32(0x00)
    w.write_uint32(zone_guid & 0xFFFFFFFF)

    w.write_byte(0x06)                         # EndStream
    return w.to_array()


def use_waypoint_scroll(server: "GameServer", conn: "RRConnection") -> None:
    """Spawn the clickable blue portal in front of the player, save the return
    point (conn + DB + the QM 0x0A client state), and show a visual-only copy to
    everyone else in this zone instance.

    The caller consumes the scroll via the normal inventory 0x1F/0x1E path
    first — this only handles the portal side (C# SpawnTownPortalWithRemoval
    minus the removal packet).
    """
    heading_rad = math.radians(conn.player_heading)
    spawn_x = conn.player_pos_x + math.sin(heading_rad) * _PORTAL_FORWARD_OFFSET
    spawn_y = conn.player_pos_y + math.cos(heading_rad) * _PORTAL_FORWARD_OFFSET
    spawn_z = conn.player_pos_z

    entity_id = server.allocate_entity_id()
    portal = PortalData(
        id=entity_id,
        zone=conn.current_zone_name or "",
        name=TOWN_PORTAL_NAME,
        gc_type=TOWN_PORTAL_GC_TYPE,
        pos_x=spawn_x,
        pos_y=spawn_y,
        pos_z=spawn_z,
        heading=0.0,
        width=_PORTAL_WIDTH,
        height=_PORTAL_HEIGHT,
        target_zone=TOWN_PORTAL_TARGET_ZONE,
        spawn_point="",
        color=_PORTAL_COLOR,
    )
    portal_manager.register_entity(entity_id, portal)

    conn.has_saved_town_portal = True
    conn.town_portal_zone_name = conn.current_zone_name or ""
    conn.town_portal_zone_id = conn.current_zone_id
    conn.town_portal_target_zone = TOWN_PORTAL_TARGET_ZONE
    conn.town_portal_pos_x = spawn_x
    conn.town_portal_pos_y = spawn_y
    conn.town_portal_pos_z = spawn_z
    persist_tp_state(conn)

    _send_qm_town_portal_state(conn)

    avatar_id = server.get_player_avatar_id(conn.login_name)
    pos = (spawn_x, spawn_y, spawn_z)
    conn.send_to_client(build_town_portal_stream(
        entity_id, avatar_id, pos, TOWN_PORTAL_TARGET_ZONE, conn.current_zone_id,
        flags=_FLAGS_CLICKABLE, state=_STATE_ACTIVE))
    _broadcast_to_instance(server, conn, build_town_portal_stream(
        entity_id, avatar_id, pos, TOWN_PORTAL_TARGET_ZONE, conn.current_zone_id,
        flags=_FLAGS_VISUAL_ONLY, state=_STATE_ACTIVE))

    log.info(f"[TOWN-PORTAL] '{conn.login_name}' opened portal 0x{entity_id:04X} "
             f"at ({spawn_x:.1f}, {spawn_y:.1f}) in '{conn.town_portal_zone_name}' "
             f"-> {TOWN_PORTAL_TARGET_ZONE}")


def spawn_return_portal_if_home(server: "GameServer", conn: "RRConnection") -> None:
    """On zone entry: if this is the zone the scroll was cast in, re-spawn the
    (visual-only) return portal at the cast point and clear the saved state —
    one return trip per scroll. Port of C# SpawnReturnTownPortal."""
    if not conn.has_saved_town_portal:
        return
    if ((conn.current_zone_name or "").lower()
            != (conn.town_portal_zone_name or "").lower()):
        return

    entity_id = server.allocate_entity_id()
    avatar_id = server.get_player_avatar_id(conn.login_name)
    packet = build_town_portal_stream(
        entity_id, avatar_id,
        (conn.town_portal_pos_x, conn.town_portal_pos_y, conn.town_portal_pos_z),
        conn.town_portal_target_zone or TOWN_PORTAL_TARGET_ZONE,
        conn.town_portal_zone_id,
        flags=_FLAGS_VISUAL_ONLY, state=_STATE_MATERIALIZING)
    conn.send_to_client(packet)
    _broadcast_to_instance(server, conn, packet)

    log.info(f"[TOWN-PORTAL] '{conn.login_name}' returned to "
             f"'{conn.town_portal_zone_name}' — return portal spawned, state cleared")
    clear_tp_state(conn)


def load_tp_state(conn: "RRConnection", saved) -> None:
    """Restore a persisted town-portal return point at character select."""
    if not getattr(saved, "tp_zone", ""):
        return
    conn.has_saved_town_portal = True
    conn.town_portal_zone_name = saved.tp_zone
    conn.town_portal_zone_id = saved.tp_zone_id
    conn.town_portal_target_zone = saved.tp_target_zone or TOWN_PORTAL_TARGET_ZONE
    conn.town_portal_pos_x = saved.tp_pos_x
    conn.town_portal_pos_y = saved.tp_pos_y
    conn.town_portal_pos_z = saved.tp_pos_z
    log.info(f"[TOWN-PORTAL] restored saved portal for '{conn.login_name}': "
             f"'{saved.tp_zone}' ({saved.tp_pos_x:.1f}, {saved.tp_pos_y:.1f})")


def clear_tp_state(conn: "RRConnection") -> None:
    """Drop the saved return point (conn + DB)."""
    conn.has_saved_town_portal = False
    conn.town_portal_zone_name = ""
    conn.town_portal_target_zone = ""
    conn.town_portal_zone_id = 0
    conn.town_portal_pos_x = 0.0
    conn.town_portal_pos_y = 0.0
    conn.town_portal_pos_z = 0.0
    persist_tp_state(conn)


def persist_tp_state(conn: "RRConnection") -> None:
    """Mirror the connection's town-portal fields into the characters row."""
    try:
        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return
        saved.tp_zone = conn.town_portal_zone_name if conn.has_saved_town_portal else ""
        saved.tp_zone_id = conn.town_portal_zone_id
        saved.tp_target_zone = conn.town_portal_target_zone
        saved.tp_pos_x = conn.town_portal_pos_x
        saved.tp_pos_y = conn.town_portal_pos_y
        saved.tp_pos_z = conn.town_portal_pos_z
        character_repository.save_character(saved)
    except Exception as ex:  # noqa: BLE001 — never break the scroll on a DB error
        log.error(f"[TOWN-PORTAL] persist failed for '{conn.login_name}': {ex}")


def _send_qm_town_portal_state(conn: "RRConnection") -> None:
    """QuestManager 0x0A — tells the client a town portal is saved so the
    obelisk menu's "Saved Places" entry lights up (C# packet 2 of
    SpawnTownPortalWithRemoval)."""
    from ..net.component_update import write_synch_none

    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x35)
    w.write_uint16(conn.quest_manager_id)
    w.write_byte(0x0A)
    w.write_byte(0x01)
    w.write_uint32(conn.town_portal_zone_id & 0xFFFFFFFF)
    w.write_cstring(conn.town_portal_zone_name)
    w.write_cstring("")
    # Flags-only trailer: the QuestManager is a player-OBJECT component with no HP
    # unit, so an HP-bearing trailer makes the client validate HP against an entity
    # that has none → the generic "Oops! You've encountered a sync error" (code 1)
    # the waypoint scroll threw in a dungeon (live x64dbg 2026-07-01). Same rule as
    # quest_wire (bible §4 / the owner-only-components golden gotcha).
    write_synch_none(w)                        # player-object: no HP trailer
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())


def _broadcast_to_instance(server: "GameServer", conn: "RRConnection",
                           packet: bytes) -> None:
    """Send a packet to every OTHER spawned player in the same zone instance."""
    for other in list(server.connections.values()):
        if other is conn or not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue
        other.send_to_client(packet)
