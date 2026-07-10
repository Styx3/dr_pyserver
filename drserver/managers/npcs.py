"""NPC manager — non-player character spawning and lifecycle.

Ported from C# UnityGameServer NPC spawning. Loads NPC definitions from the
SQLite npcs table, spawns them into the relevant zones as interactive entities
with idle UnitBehavior components.

Phase 9: MVP with entity spawning per-zone. NPC dialog/interaction and
merchant inventory loading deferred to Phase 10.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..util.byte_io import LEWriter
from ..data.gc_object import write_gc_type

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection

@dataclass
class NPCData:
    id: int
    gc_type: str
    name: str
    zone_type: str
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    hit_points: int
    mana_points: int
    hp_wire: int          # HP * 256
    mp_wire: int          # MP * 256


class NPCManager:
    """Global registry of NPC definitions."""

    def __init__(self):
        self._npcs: Dict[str, NPCData] = {}              # gc_type -> data
        self._npcs_by_zone: Dict[str, List[NPCData]] = {}  # zone_type -> list
        self._entity_to_npc: Dict[int, NPCData] = {}      # entity_id -> NPCData
        # lowercased gc_type -> (zone, spawn_point) for on-NPC NPCTeleporter blocks
        self._teleporters: Dict[str, Tuple[str, str]] = {}
        self._loaded = False

    def load(self) -> None:
        """Load all NPC definitions from SQLite."""
        if self._loaded:
            return
        self._npcs.clear()
        self._npcs_by_zone.clear()
        self._teleporters.clear()

        try:
            for row in db.execute_reader("SELECT * FROM npcs").fetchall():
                gc_type = db.get_string(row, "gc_type")
                if not gc_type:
                    continue

                hp = db.get_int(row, "hit_points", 100)
                mp = db.get_int(row, "mana_points", 0)

                nd = NPCData(
                    id=db.get_int(row, "id"),
                    gc_type=gc_type,
                    name=db.get_string(row, "name") or gc_type,
                    zone_type=db.get_string(row, "zone_type"),
                    pos_x=db.get_float(row, "pos_x"),
                    pos_y=db.get_float(row, "pos_y"),
                    pos_z=db.get_float(row, "pos_z"),
                    heading=db.get_float(row, "heading"),
                    hit_points=hp,
                    mana_points=mp,
                    hp_wire=hp * 256,
                    mp_wire=mp * 256,
                )
                self._npcs[gc_type] = nd
                if nd.zone_type:
                    zone_key = nd.zone_type.lower()
                    if zone_key not in self._npcs_by_zone:
                        self._npcs_by_zone[zone_key] = []
                    self._npcs_by_zone[zone_key].append(nd)

            self._load_teleporters()
            self._loaded = True
            log.info(f"[NPCManager] loaded {len(self._npcs)} NPCs across "
                     f"{len(self._npcs_by_zone)} zones, "
                     f"{len(self._teleporters)} teleporter(s)")
        except Exception as ex:
            log.error(f"[NPCManager] load error: {ex}")

    def _load_teleporters(self) -> None:
        """Load the additive ``npc_teleporters`` companion table (on-NPC
        ``NPCTeleporter`` destinations). Missing on an un-migrated DB → no
        teleporters (op 0x08 then no-ops), never an error."""
        try:
            rows = db.execute_reader(
                "SELECT gc_type, zone, spawn_point FROM npc_teleporters").fetchall()
        except Exception:  # noqa: BLE001 — table absent until the importer runs
            return
        for row in rows:
            gc_type = db.get_string(row, "gc_type")
            zone = db.get_string(row, "zone")
            if not gc_type or not zone:
                continue
            self._teleporters[gc_type.lower()] = (
                zone, db.get_string(row, "spawn_point"))

    def get_for_zone(self, zone_name: str) -> List[NPCData]:
        """Return NPCs that should spawn in a given zone.

        Matched **exactly** on the lowercased zone name (= ``zone_type``). The
        old substring fallback leaked the ``thehub`` NPC set into every
        ``thehubportals_dungeon*`` / ``thehub_oldlinks`` room (``"thehub" in
        "thehubportals_dungeon01"``), which the client authors empty — exact
        match keeps those portal sub-hubs NPC-free as shipped. Every zone the
        ``npcs`` table actually serves (town, tutorial, pvp_start, thehub,
        pvp_hub) is a whole zone name, so no partial match is ever needed.
        """
        if not self._loaded:
            self.load()
        return list(self._npcs_by_zone.get(zone_name.lower(), []))

    def find_by_entity_id(self, entity_id: int) -> Optional[NPCData]:
        """Look up an NPC by its spawned entity ID."""
        return self._entity_to_npc.get(entity_id)

    def teleporter_for(self, gc_type: str) -> Optional[Tuple[str, str]]:
        """``(zone, spawn_point)`` for an NPC's on-NPC ``NPCTeleporter`` block, or
        ``None``. Looked up case-insensitively (the spawned NPC's gc_type case can
        differ from the authored path — e.g. ``world.town.NPC.x`` vs ``...npc...``)."""
        if not self._loaded:
            self.load()
        return self._teleporters.get((gc_type or "").lower())


npc_manager = NPCManager()


def build_zone_npcs(server: "GameServer", zone_name: str,
                    merchant_sink: Optional[List[tuple[int, str]]] = None,
                    ) -> List[tuple[int, bytes]]:
    """Build the per-zone NPC create streams ONCE — port of C# SendZoneNPCs
    (UnityGameServer.cs:13012+), but with stable, globally-unique ids and no
    broadcast. Returns ``[(entity_id, create_packet), …]``; the per-instance
    registry stores these and sends them to each joiner. See [[world-instance]].

    Format per NPC inside shared BeginStream…EndStream:
      0x01 create | 0x32 behavior (npc.base.behavior) | 0x32 skills |
      0x32 manipulators | 0x32 modifiers | [0x32 merchant — IF VENDOR] |
      0x02 StockUnit init | 0x35 WarpTo(0x11) + Synch(0x02, 0x47E00)

    Vendor (merchant) NPCs additionally get a ``merchant`` component whose init
    carries the tab layout (dynamic tabs ship EMPTY here — the stream is cached
    per instance; per-player stock is pushed post-spawn). Each merchant
    component id is registered on the server and appended to ``merchant_sink``
    as ``(component_id, npc_gc_type)`` for the zone instance.
    """
    npcs = npc_manager.get_for_zone(zone_name)
    if not npcs:
        log.debug(f"[NPC] no NPCs found for zone '{zone_name}'")
        return []

    from .merchants import merchant_manager

    built: List[tuple[int, bytes]] = []

    for nd in npcs:
        entity_id = server.allocate_entity_id()
        ub_id = server.allocate_entity_id()
        npc_manager._entity_to_npc[entity_id] = nd
        level = 100

        w = LEWriter()
        w.write_byte(0x07)

        # ── OP1: create NPC entity ──
        w.write_byte(0x01)
        w.write_uint16(entity_id)
        write_gc_type(w, nd.gc_type, preserve_case=True)

        # ── OP2: Behavior component (npc.base.behavior) ──
        w.write_byte(0x32)
        w.write_uint16(entity_id)
        w.write_uint16(ub_id)
        write_gc_type(w, "npc.base.behavior")
        w.write_byte(0x01)
        w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)
        w.write_byte(0x01)
        w.write_byte(0x85); w.write_byte(0x00)
        for _ in range(5):
            w.write_uint32(0)
        w.write_byte(0x00)
        w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)
        w.write_byte(0x00); w.write_byte(0x00)
        w.write_uint32(0); w.write_uint32(0)

        # ── OP3: Skills component ──
        w.write_byte(0x32)
        w.write_uint16(entity_id)
        w.write_uint16(server.allocate_entity_id())
        write_gc_type(w, "skills")
        w.write_byte(0x01)
        w.write_byte(0xFF); w.write_byte(0xFF); w.write_byte(0xFF)
        w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x01)
        write_gc_type(w, "skills.professions.Warrior", preserve_case=True)

        # ── OP4: Manipulators component (empty) ──
        w.write_byte(0x32)
        w.write_uint16(entity_id)
        w.write_uint16(server.allocate_entity_id())
        write_gc_type(w, "manipulators")
        w.write_byte(0x01)
        w.write_byte(0x00)

        # ── OP5: Modifiers component ──
        w.write_byte(0x32)
        w.write_uint16(entity_id)
        w.write_uint16(server.allocate_entity_id())
        write_gc_type(w, "modifiers")
        w.write_byte(0x01)
        w.write_uint32(0x00000000)
        w.write_byte(0x00)
        w.write_uint32(0x00000000)

        # ── OP5b: Merchant component — IF VENDOR (C# UGS:17524) ──
        # Dynamic tabs are shipped empty in this cached stream; the per-player
        # stock follows post-spawn as 0x35/0x1E add-updates.
        if merchant_manager.is_merchant(nd.gc_type):
            merchant_cid = server.allocate_entity_id()
            if merchant_manager.write_merchant_component(
                    w, nd.gc_type, entity_id, merchant_cid,
                    include_dynamic=False):
                server.merchant_components[merchant_cid] = nd.gc_type
                server.npc_merchant_cids[entity_id] = merchant_cid
                if merchant_sink is not None:
                    merchant_sink.append((merchant_cid, nd.gc_type))
                log.info(f"[NPC] merchant component cid=0x{merchant_cid:04X} "
                         f"for {nd.gc_type}")

        # ── OP5c: SkillTrainer component — IF TRAINER (C# CEM:2458) ──
        # The trainer's skill list is client-authored (Trainer*Base.gc
        # SkillTrainer block); the stream only carries the component reference.
        # Train purchases arrive as ComponentUpdates on this cid
        # (managers.trainers.handle_train_request).
        from .trainers import is_trainer, write_skill_trainer_component
        if is_trainer(nd.gc_type):
            trainer_cid = server.allocate_entity_id()
            write_skill_trainer_component(w, nd.gc_type, entity_id, trainer_cid)
            server.trainer_components[trainer_cid] = nd.gc_type
            log.info(f"[NPC] skill-trainer component cid=0x{trainer_cid:04X} "
                     f"for {nd.gc_type}")

        # ── OP6: StockUnit entity init ──
        w.write_byte(0x02)
        w.write_uint16(entity_id)
        # worldEntityFlags: visible|activatable|blocking. Bit 0x01 = blocking is
        # what makes the NPC a solid collider — client WorldEntity::processInited
        # @0x4d3560 gates WorldCollisionManager::add + PathMap::Invalidate on
        # (flags & 1). C# SendZoneNPCs shipped 0x06 (no collision); town has a
        # real pathmap so the Invalidate null-guard is satisfied and NPCs collide.
        w.write_uint32(0x07)                   # worldEntityFlags (collision on)
        w.write_int32(int(nd.pos_x * 256))
        w.write_int32(int(nd.pos_y * 256))
        w.write_int32(int(nd.pos_z * 256))
        w.write_int32(int(nd.heading * 256))
        w.write_byte(0x00)                     # initFlags
        w.write_byte(0x01)                     # unitFlags
        for _ in range(8):
            w.write_uint32(0)                  # StockUnit padding

        # ── OP7: WarpTo (0x35/0x04 sub=0x11) + Synch(0x02, 0x47E00) ──
        w.write_byte(0x35)
        w.write_uint16(ub_id)
        w.write_byte(0x04)
        w.write_byte(0x11)                     # ActionWarp
        w.write_byte(0x00)                     # SessionID
        w.write_int32(int(nd.pos_x * 256))
        w.write_int32(int(nd.pos_y * 256))
        w.write_int32(int(nd.pos_z * 256))
        w.write_byte(0x02)
        w.write_uint32(0x47E00)                # magic synch value

        w.write_byte(0x06)
        spawn_packet = w.to_array()
        built.append((entity_id, spawn_packet))

    log.info(f"[NPC] built {len(built)} NPC create streams for zone '{zone_name}'")
    return built
