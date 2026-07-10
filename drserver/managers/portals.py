"""Zone-portal (teleport gate) manager — port of C# InitializeZonePortals /
SendZonePortals / HandlePortalActivation (UnityGameServer.cs:11442+).

A zone portal is an interactive WorldEntity (``misc.ZonePortal_agg``) that, when
the player walks into / activates it, transfers them to ``target_zone`` at the
named ``spawn_point`` waypoint. Definitions live in the ``zone_portals`` SQLite
table; one tutorial portal (→ ``dungeon00_level01`` @ ``Start1``) ships in the DB.

The create/init byte layout below is copied verbatim from the C# writer so the
client accepts it — wrong layouts fail silently (the client drops the packet).
Each portal is wrapped in its own BeginStream(0x07)…EndStream(0x06) so it slots
into the per-entity instance snapshot replay (see [[world-instance]]).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..util.byte_io import LEWriter
from ..data.gc_object import write_gc_type

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer


@dataclass(frozen=True)
class PortalData:
    id: int
    zone: str
    name: str
    gc_type: str
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    width: int
    height: int
    target_zone: str
    spawn_point: str
    color: int


@dataclass(frozen=True)
class WaypointData:
    name: str
    pos_x: float
    pos_y: float
    pos_z: float


class PortalManager:
    """Global registry of zone-portal definitions + spawn-point waypoints."""

    def __init__(self) -> None:
        self._by_zone: Dict[str, List[PortalData]] = {}
        self._waypoints: Dict[str, List[WaypointData]] = {}   # zone -> waypoints
        self._entity_to_portal: Dict[int, PortalData] = {}    # spawned entity id -> portal
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._by_zone.clear()
        self._waypoints.clear()
        try:
            for row in db.execute_reader("SELECT * FROM zone_portals").fetchall():
                zone = db.get_string(row, "zone")
                if not zone:
                    continue
                p = PortalData(
                    id=db.get_int(row, "id"),
                    zone=zone,
                    name=db.get_string(row, "name"),
                    gc_type=db.get_string(row, "gc_type"),
                    pos_x=db.get_float(row, "pos_x"),
                    pos_y=db.get_float(row, "pos_y"),
                    pos_z=db.get_float(row, "pos_z"),
                    heading=db.get_float(row, "heading"),
                    width=db.get_int(row, "width"),
                    height=db.get_int(row, "height"),
                    target_zone=db.get_string(row, "target_zone"),
                    spawn_point=db.get_string(row, "spawn_point"),
                    color=db.get_int(row, "color"),
                )
                self._by_zone.setdefault(zone.lower(), []).append(p)

            for row in db.execute_reader("SELECT * FROM zone_waypoints").fetchall():
                zone = db.get_string(row, "zone")
                if not zone:
                    continue
                self._waypoints.setdefault(zone.lower(), []).append(
                    WaypointData(
                        name=db.get_string(row, "name"),
                        pos_x=db.get_float(row, "pos_x"),
                        pos_y=db.get_float(row, "pos_y"),
                        pos_z=db.get_float(row, "pos_z"),
                    )
                )

            self._loaded = True
            total = sum(len(v) for v in self._by_zone.values())
            log.info(f"[PortalManager] loaded {total} portals across "
                     f"{len(self._by_zone)} zones, "
                     f"{sum(len(v) for v in self._waypoints.values())} waypoints")
        except Exception as ex:  # noqa: BLE001
            log.error(f"[PortalManager] load error: {ex}")

    def get_for_zone(self, zone_name: str) -> List[PortalData]:
        if not self._loaded:
            self.load()
        return self._by_zone.get(zone_name.lower(), [])

    def find_waypoint(self, zone_name: str, name: str) -> Optional[WaypointData]:
        if not self._loaded:
            self.load()
        for wp in self._waypoints.get(zone_name.lower(), []):
            if wp.name.lower() == name.lower():
                return wp
        return None

    def find_by_entity_id(self, entity_id: int) -> Optional[PortalData]:
        return self._entity_to_portal.get(entity_id)

    def register_entity(self, entity_id: int, portal: PortalData) -> None:
        self._entity_to_portal[entity_id] = portal


portal_manager = PortalManager()


def build_portal_stream(entity_id: int, portal: PortalData) -> bytes:
    """Serialize one portal as a standalone BeginStream…EndStream packet.

    Byte-for-byte port of the per-portal body in C# SendZonePortals
    (UnityGameServer.cs:11556+). Used directly by the round-trip test.
    """
    w = LEWriter()
    w.write_byte(0x07)                         # BeginStream

    # ── Create portal entity (0x01) ──
    w.write_byte(0x01)
    w.write_uint16(entity_id)
    write_gc_type(w, portal.gc_type, preserve_case=True)

    # ── Init portal entity (0x02) — WorldEntity::WriteInit ──
    w.write_byte(0x02)
    w.write_uint16(entity_id)
    w.write_uint32(0x06)                       # flags: visible | activatable
    w.write_int32(int(portal.pos_x * 256))
    w.write_int32(int(portal.pos_y * 256))
    w.write_int32(int(portal.pos_z * 256))
    w.write_int32(int(portal.heading * 256))
    w.write_byte(0x07)                         # initFlags: hasParent | unk2 | unk4
    w.write_uint16(0)                          # parentID  (0x01 flag)
    w.write_byte(0)                            # Unk2Case  (0x02 flag)
    w.write_uint32(0)                          # Unk4Case  (0x04 flag)
    w.write_cstring(portal.spawn_point or "")
    w.write_cstring(portal.target_zone or "")
    w.write_uint16(portal.width & 0xFFFF)
    w.write_uint16(portal.height & 0xFFFF)
    w.write_uint32(portal.color & 0xFFFFFFFF)

    w.write_byte(0x06)                         # EndStream
    return w.to_array()


# Default gate appearance for procedural-dungeon warps (matches the C# /
# zone_portals dungeon rows: red ZonePortal_agg, 60×30). The position comes from
# the maze; only the visual box is constant.
_WARP_GATE_GC_TYPE = "misc.ZonePortal_agg"
_WARP_GATE_WIDTH = 60
_WARP_GATE_HEIGHT = 60
_WARP_GATE_COLOR = 0xFFFF0000  # opaque red (same as the dungeon zone_portals rows)


def build_dungeon_warp_gates(server: "GameServer", zone_name: str,
                             pathmap_key: str = "") -> List[tuple[int, bytes]]:
    """Build inter-level warp-gate portals for a procedural dungeon level.

    The maze levels (``dungeonNN_level01..07``) have NO ``zone_portals`` rows —
    their entrance/exit warps live in ``dungeon_room_nodes`` and must be placed
    at the cell the maze actually dropped each room node in. This resolves those
    (:func:`dungeon_spawner.warp_gates`), snaps each to floor Z via the instance
    pathmap (already registered by the monster spawner when this runs), wraps it
    in a :class:`PortalData`, registers entity→portal so activation transfers the
    player (the existing ``HandlePortalActivation`` path), and returns the create
    streams for the instance snapshot. Empty for non-maze zones.
    """
    from . import dungeon_spawner

    gates = dungeon_spawner.warp_gates(zone_name)
    if not gates:
        return []

    built: List[tuple[int, bytes]] = []
    for gate in gates:
        # The gate position/rotation is fully data-driven from the tile's authored
        # ZonePortal model anchor (world X/Y/Z + heading resolved in
        # dungeon_spawner.warp_gates) — exactly where the client renders the portal.
        # No floor-snap or hardcoded Z offset.
        portal = PortalData(
            id=0,
            zone=zone_name,
            name=gate.spawn_name or gate.node_kind,
            gc_type=_WARP_GATE_GC_TYPE,
            pos_x=gate.world_x,
            pos_y=gate.world_y,
            pos_z=gate.world_z,
            heading=gate.heading,
            width=_WARP_GATE_WIDTH,
            height=_WARP_GATE_HEIGHT,
            target_zone=gate.link_to_zone,
            spawn_point=gate.link_to_spawn,
            color=_WARP_GATE_COLOR,
        )
        entity_id = server.allocate_entity_id()
        portal_manager.register_entity(entity_id, portal)
        built.append((entity_id, build_portal_stream(entity_id, portal)))

    log.info(f"[PORTAL] built {len(built)} dungeon warp gates for zone "
             f"'{zone_name}' (authored anchors)")
    return built


def build_zone_portals(server: "GameServer", zone_name: str) -> List[tuple[int, bytes]]:
    """Build the per-zone portal create streams ONCE and register each spawned
    entity id → portal so activation can resolve the target zone.

    Returns ``[(entity_id, create_packet), …]`` for the instance snapshot.
    """
    portals = portal_manager.get_for_zone(zone_name)
    if not portals:
        return []

    built: List[tuple[int, bytes]] = []
    for portal in portals:
        entity_id = server.allocate_entity_id()
        portal_manager.register_entity(entity_id, portal)
        built.append((entity_id, build_portal_stream(entity_id, portal)))

    log.info(f"[PORTAL] built {len(built)} portal create streams for zone '{zone_name}'")
    return built
