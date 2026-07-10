"""Waystone / checkpoint (obelisk) manager — port of the C# checkpoint system
(InitializeZoneCheckpoints / SendZoneCheckpoints / HandleCheckpointActivation /
HandleCheckpointTeleportRequest / HandleObeliskTeleport, UnityGameServer.cs).

Two distinct concepts back the in-game "waystone":

* **Destinations** (``checkpoints`` table) — the recall targets shown in the
  obelisk menu. Each has a stable GC id ``world.checkpoints.<Name>`` (the C#
  ``DatabaseLoader.LoadCheckpoints`` builds it as ``"world.checkpoints." +
  name.Replace(" ", "")``), a target ``zone`` + ``spawn_point`` and a
  ``display_order`` used to cycle the obelisk.
* **Entities** (``zone_checkpoints`` table) — the physical obelisks placed in a
  zone (gc_type ``world.checkpoints.<Name>Entity``). Walking up and activating
  one *unlocks* the matching destination for that character.

The create/init byte layout is copied verbatim from C# ``SendZoneCheckpoints``:
a WorldEntity with flags ``0x06`` (visible | activatable, NOT blocking — the
blocking flag makes the client pathfinder scan cells and crash near map edges)
and a single ``0x00`` initFlags byte (no parent / cstrings, unlike a portal).
Each obelisk is wrapped in its own BeginStream(0x07)…EndStream(0x06) so it slots
into the per-instance snapshot replay alongside NPCs/portals (see [[world-instance]]).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..util.byte_io import LEWriter
from ..data.gc_object import write_gc_type

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer

# Starter waystones — always unlocked for every character (the town and
# tutorial obelisks are available from the first login).
DEFAULT_CHECKPOINTS = frozenset({
    "world.checkpoints.TownCheckpoint",
    "world.checkpoints.TutorialCheckpoint",
})


@dataclass(frozen=True)
class CheckpointDestination:
    """A recall target (``checkpoints`` row). ``id`` is the GC class the client
    sends when it wants to recall here (``world.checkpoints.<Name>``)."""
    id: str
    name: str
    zone: str
    spawn_point: str
    pos_x: float
    pos_y: float
    pos_z: float
    order: int
    is_active: bool
    level_requirement: int
    unlock_quest: str


@dataclass(frozen=True)
class CheckpointEntity:
    """A physical obelisk placed in a zone (``zone_checkpoints`` row)."""
    zone: str
    name: str
    gc_type: str
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float


def _destination_id(name: str) -> str:
    """Build the GC id the way C# does: ``world.checkpoints.`` + name w/o spaces."""
    return "world.checkpoints." + (name or "").replace(" ", "")


class CheckpointManager:
    """Global registry of recall destinations + physical obelisk entities."""

    def __init__(self) -> None:
        self._destinations: List[CheckpointDestination] = []
        self._dest_by_id: Dict[str, CheckpointDestination] = {}     # lower id -> dest
        self._entities_by_zone: Dict[str, List[CheckpointEntity]] = {}
        self._entity_to_checkpoint: Dict[int, CheckpointEntity] = {}  # spawned id -> entity
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._destinations.clear()
        self._dest_by_id.clear()
        self._entities_by_zone.clear()
        try:
            for row in db.execute_reader("SELECT * FROM checkpoints").fetchall():
                name = db.get_string(row, "name")
                dest = CheckpointDestination(
                    id=_destination_id(name),
                    name=name,
                    zone=db.get_string(row, "zone"),
                    spawn_point=db.get_string(row, "spawn_point"),
                    pos_x=db.get_float(row, "pos_x"),
                    pos_y=db.get_float(row, "pos_y"),
                    pos_z=db.get_float(row, "pos_z"),
                    order=db.get_int(row, "display_order"),
                    is_active=db.get_bool(row, "is_active", True),
                    level_requirement=db.get_int(row, "level_requirement", 1),
                    unlock_quest=db.get_string(row, "unlock_quest"),
                )
                self._destinations.append(dest)
                self._dest_by_id[dest.id.lower()] = dest

            for row in db.execute_reader("SELECT * FROM zone_checkpoints").fetchall():
                zone = db.get_string(row, "zone")
                if not zone:
                    continue
                self._entities_by_zone.setdefault(zone.lower(), []).append(
                    CheckpointEntity(
                        zone=zone,
                        name=db.get_string(row, "name"),
                        gc_type=db.get_string(row, "gc_type"),
                        pos_x=db.get_float(row, "pos_x"),
                        pos_y=db.get_float(row, "pos_y"),
                        pos_z=db.get_float(row, "pos_z"),
                        heading=db.get_float(row, "heading"),
                    )
                )

            self._loaded = True
            log.info(f"[CheckpointManager] loaded {len(self._destinations)} destinations, "
                     f"{sum(len(v) for v in self._entities_by_zone.values())} obelisks across "
                     f"{len(self._entities_by_zone)} zones")
        except Exception as ex:  # noqa: BLE001
            log.error(f"[CheckpointManager] load error: {ex}")

    # ── Destinations ─────────────────────────────────────────────────────────
    def destinations(self) -> List[CheckpointDestination]:
        if not self._loaded:
            self.load()
        return self._destinations

    def find_destination(self, gc_id: str) -> Optional[CheckpointDestination]:
        """Resolve a recall destination by its GC id, case-insensitively.

        Accepts both the bare destination id (``world.checkpoints.TownCheckpoint``)
        and the physical-entity gc_type (``…TownCheckpointEntity``) — the C#
        activation path strips the ``Entity`` suffix before matching.
        """
        if not self._loaded:
            self.load()
        if not gc_id:
            return None
        key = gc_id.lower()
        dest = self._dest_by_id.get(key)
        if dest is None and key.endswith("entity"):
            dest = self._dest_by_id.get(key[: -len("entity")])
        return dest

    # ── Entities ───────────────────────────────────────────────────────────
    def get_for_zone(self, zone_name: str) -> List[CheckpointEntity]:
        if not self._loaded:
            self.load()
        return self._entities_by_zone.get((zone_name or "").lower(), [])

    def find_by_entity_id(self, entity_id: int) -> Optional[CheckpointEntity]:
        return self._entity_to_checkpoint.get(entity_id)

    def register_entity(self, entity_id: int, entity: CheckpointEntity) -> None:
        self._entity_to_checkpoint[entity_id] = entity


checkpoint_manager = CheckpointManager()


def build_checkpoint_stream(entity_id: int, entity: CheckpointEntity) -> bytes:
    """Serialize one obelisk as a standalone BeginStream…EndStream packet.

    Byte-for-byte port of the per-checkpoint body in C# SendZoneCheckpoints
    (UnityGameServer.cs:11623+). Used directly by the round-trip test.
    """
    w = LEWriter()
    w.write_byte(0x07)                         # BeginStream

    # ── Create checkpoint entity (0x01) ──
    w.write_byte(0x01)
    w.write_uint16(entity_id)
    write_gc_type(w, entity.gc_type, preserve_case=True)

    # ── Init checkpoint entity (0x02) — WorldEntity::WriteInit ──
    w.write_byte(0x02)
    w.write_uint16(entity_id)
    w.write_uint32(0x06)                       # flags: visible | activatable (NOT blocking)
    w.write_int32(int(entity.pos_x * 256))
    w.write_int32(int(entity.pos_y * 256))
    w.write_int32(int(entity.pos_z * 256))
    w.write_int32(int(entity.heading * 256))
    w.write_byte(0x00)                         # initFlags (no parent / no cstrings)

    w.write_byte(0x06)                         # EndStream
    return w.to_array()


def build_zone_checkpoints(server: "GameServer", zone_name: str) -> List[tuple[int, bytes]]:
    """Build the per-zone obelisk create streams ONCE and register each spawned
    entity id → entity so activation can resolve the destination.

    Returns ``[(entity_id, create_packet), …]`` for the instance snapshot.
    """
    entities = checkpoint_manager.get_for_zone(zone_name)
    if not entities:
        return []

    built: List[tuple[int, bytes]] = []
    for entity in entities:
        entity_id = server.allocate_entity_id()
        checkpoint_manager.register_entity(entity_id, entity)
        built.append((entity_id, build_checkpoint_stream(entity_id, entity)))

    log.info(f"[CHECKPOINT] built {len(built)} obelisk create streams for zone '{zone_name}'")
    return built
