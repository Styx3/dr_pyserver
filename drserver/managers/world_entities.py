"""World entity spawner — chests, shrines, teleporters, gates.

Ported from C# WorldEntitySpawner.cs. Loads world entities from the
zone_world_entities SQLite table and spawns them as interactive objects
in their assigned zones. Entity types: chest, teleporter, shrine, gate, npc, nci.

Phase 10: Full entity spawning with correct init byte sequences per entity type.
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
    from .connection import RRConnection


@dataclass
class WorldEntityData:
    id: int
    zone_name: str
    name: str
    gc_type: str
    entity_type: str          # chest, teleporter, shrine, gate, npc, nci
    pos_x: float
    pos_y: float
    pos_z: float
    heading: float
    floor_index: int
    item_generator: str        # e.g. "TreasureChestBossIG" for chests
    item_count: int
    target_zone: str           # teleporter target zone
    target_waypoint: str
    display_label: str
    flags: int


class WorldEntityManager:
    """Global registry of world entity definitions."""

    def __init__(self):
        self._entities: Dict[str, List[WorldEntityData]] = {}  # zone_name -> list
        self._by_id: Dict[int, WorldEntityData] = {}           # entity_id -> def
        self._opened_chests: set = set()                       # entity_ids (one-shot)
        self._opened_gates: set = set()                        # entity_ids (boss-door)
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._entities.clear()

        try:
            for row in db.execute_reader("SELECT * FROM zone_world_entities").fetchall():
                zone = db.get_string(row, "zone")
                if not zone:
                    continue

                we = WorldEntityData(
                    id=db.get_int(row, "id"),
                    zone_name=zone,
                    name=db.get_string(row, "name"),
                    gc_type=db.get_string(row, "gc_type"),
                    entity_type=db.get_string(row, "entity_type"),
                    pos_x=db.get_float(row, "pos_x"),
                    pos_y=db.get_float(row, "pos_y"),
                    pos_z=db.get_float(row, "pos_z"),
                    heading=db.get_float(row, "heading"),
                    floor_index=db.get_int(row, "floor_index", 0),
                    item_generator=db.get_string(row, "item_generator"),
                    item_count=db.get_int(row, "item_count"),
                    target_zone=db.get_string(row, "target_zone"),
                    target_waypoint=db.get_string(row, "target_waypoint"),
                    display_label=db.get_string(row, "display_label"),
                    flags=db.get_int(row, "flags"),
                )
                zone_key = zone.lower()
                if zone_key not in self._entities:
                    self._entities[zone_key] = []
                self._entities[zone_key].append(we)

            self._loaded = True
            log.info(f"[WorldEntity] loaded {sum(len(v) for v in self._entities.values())} entities "
                     f"across {len(self._entities)} zones")
        except Exception as ex:
            log.error(f"[WorldEntity] load error: {ex}")

    def get_for_zone(self, zone_name: str) -> List[WorldEntityData]:
        if not self._loaded:
            self.load()
        zone_key = zone_name.lower()
        return self._entities.get(zone_key, [])

    # ── Live spawned-entity registry (entity_id → definition) ──
    # Each instance's entities get globally-unique ids (server.allocate_entity_id),
    # so a single id→data map serves every instance and an opened-chest set keyed
    # by id is naturally per-instance. Mirrors portal/checkpoint find_by_entity_id.
    def register_entity(self, entity_id: int, we: WorldEntityData) -> None:
        self._by_id[entity_id] = we

    def find_by_entity_id(self, entity_id: int) -> Optional[WorldEntityData]:
        return self._by_id.get(entity_id)

    def is_chest_opened(self, entity_id: int) -> bool:
        return entity_id in self._opened_chests

    def mark_chest_opened(self, entity_id: int) -> None:
        self._opened_chests.add(entity_id)

    # Boss exit-gates unlocked by DoorsToOpenOnDeath. Keyed by the gate's
    # globally-unique entity id, so the set is naturally per-instance (each
    # instance allocates its own gate ids) — mirrors _opened_chests.
    def is_gate_opened(self, entity_id: int) -> bool:
        return entity_id in self._opened_gates

    def mark_gate_opened(self, entity_id: int) -> None:
        self._opened_gates.add(entity_id)


world_entity_manager = WorldEntityManager()


def build_world_entity_stream(entity_id: int, behavior_id: int,
                              we: WorldEntityData) -> bytes:
    """Serialize one world entity as a standalone BeginStream…EndStream packet.

    Byte-for-byte port of C# ``WorldEntitySpawner.WriteEntitySpawn``
    (WorldEntitySpawner.cs:124). Gates use Door::readInit (2 trailing bytes,
    no behavior child); everything else (teleporter / chest / shrine / nci)
    uses the intermediate + NCI readInit followed by a ``0x32`` behavior child
    with a UnitMover. The previous hand-rolled layout did NOT match the client
    and desynced the town stream (processMessage error 3). Used by the
    round-trip test.
    """
    is_gate = we.entity_type == "gate"

    w = LEWriter()
    w.write_byte(0x07)                       # BeginStream

    # ── Create entity (0x01) ──
    w.write_byte(0x01)
    w.write_uint16(entity_id)
    write_gc_type(w, we.gc_type, preserve_case=True)

    # ── Init entity (0x02) — WorldEntity::WriteInit ──
    w.write_byte(0x02)
    w.write_uint16(entity_id)
    w.write_uint32(we.flags & 0xFFFFFFFF)    # flags from DB (default 0x07)
    w.write_int32(int(we.pos_x * 256))
    w.write_int32(int(we.pos_y * 256))
    w.write_int32(int(we.pos_z * 256))
    w.write_int32(int(we.heading * 256))
    w.write_byte(0x00)                       # initFlags (no parent / cstrings)

    if is_gate:
        # Door::readInit @ 0x5A6A10 — 2 bytes, no NCI, no behavior child.
        w.write_byte(0x00)                   # door open/closed state
        w.write_byte(0x00)                   # additional door flags
    else:
        # Intermediate parent::readInit @ 0x50A580 — 6 bytes.
        w.write_byte(0x00)                   # intermediate flags (no conditionals)
        w.write_byte(0x00)                   # level/mode
        w.write_uint16(0)                    # +0x316
        w.write_uint16(0)                    # +0x318

        # NCI::readInit @ 0x5A8E20 — 4 bytes.
        w.write_byte(0x00)                   # activation flags (0 = not activated)
        w.write_byte(0x00)                   # state
        w.write_uint16(0)                    # counter

        # 0x32 CreateChild: Behavior (readInit always runs — no hasInit byte).
        w.write_byte(0x32)
        w.write_uint16(entity_id)
        w.write_uint16(behavior_id)
        write_gc_type(w, "base.noncombatinteractive.behavior", preserve_case=True)
        w.write_byte(0x01)                   # flag byte → [child+0x60]

        # Behavior::readInit (4 bytes).
        w.write_byte(0xFF)                   # flags
        w.write_byte(0x00)                   # action class ID (0 = none)
        w.write_byte(0x00)                   # second action class ID
        w.write_byte(0x01)                   # end byte

        # UnitMover::readInit (10 bytes, flags=0x08).
        w.write_byte(0x08)                   # mover flags
        w.write_int32(int(we.heading * 256))  # mover+0x64
        w.write_int32(int(we.heading * 256))  # mover+0x68
        w.write_byte(0x00)                   # waypoint

        # UnitBehavior::readInit own (3 bytes).
        w.write_byte(0xFF)                   # flags
        w.write_byte(0x00)                   # extra
        w.write_byte(0x00)                   # extra2

    w.write_byte(0x06)                       # EndStream
    return w.to_array()


def build_zone_world_entities(server: "GameServer", zone_name: str) -> List[tuple[int, bytes]]:
    """Build world-entity create streams ONCE (chests, teleporters, shrines, gates).

    Returns ``[(entity_id, create_packet), …]`` with stable, globally-unique ids
    and no broadcast; the per-instance registry stores and replays them to each
    joiner. See [[world-instance]].
    """
    entities = world_entity_manager.get_for_zone(zone_name)
    if not entities:
        return []

    built: List[tuple[int, bytes]] = []
    for we in entities:
        entity_id = server.allocate_entity_id()
        behavior_id = server.allocate_entity_id()
        # Register entity→def so a 0x06 click can be routed to the chest/shrine/
        # gate/teleporter handler (see handle_activation).
        world_entity_manager.register_entity(entity_id, we)
        built.append((entity_id, build_world_entity_stream(entity_id, behavior_id, we)))

    if built:
        log.info(f"[WorldEntity] built {len(built)} entity create streams for zone '{zone_name}'")
    return built


# Authored max-HP wire for an interactive's NonCombatInteractive EntitySynchInfo
# trailer (C# WriteNonCombatInteractiveEntitySynchInfo → ResolveAuthoredUnit-
# MaxHealthWire). The dungeon00 chests/shrine are authored HitPoints=14080; the
# value is informational for a server-owned interactive (the client never
# simulates it, so the zero-tolerance HP compare never fires — Regime A), so a
# constant is safe. ×256 fixed-point like all wire HP.
_NCI_MAX_HP_WIRE = 14080 * 256

# The Endurance buff the NewbieEnduranceShrine grants (world content:
# SpellModEffect.Duration=300; Modifier = "<shrine_gc>.Modifier", an
# AttributeModifier ENDURANCE +10). The client applies the named modifier from
# its own content, so the server only names it + duration.
_SHRINE_BUFF_DURATION_S = 300.0


def _build_nci_activate(entity_id: int, activated: bool) -> bytes:
    """The NonCombatInteractive activate/open ComponentUpdate (C#
    HandleChestActivation / world-entity activate): ``0x03 <eid> 0x0A <state:u32>``
    + the NCI EntitySynchInfo ``0x02 <maxHpWire>``. ``state`` 1 = opened (chest),
    0 = activated (shrine/generic). Frameless — rides the message queue."""
    w = LEWriter()
    w.write_byte(0x03)                       # processEntityUpdate
    w.write_uint16(entity_id)
    w.write_byte(0x0A)                       # NonCombatInteractive update
    w.write_uint32(0x00000001 if activated else 0x00000000)
    w.write_byte(0x02)                       # EntitySynchInfo: HP present
    w.write_uint32(_NCI_MAX_HP_WIRE)
    return w.to_array()


def open_chest(server: "GameServer", conn: "RRConnection",
               entity_id: int, we: WorldEntityData) -> None:
    """Open a clicked chest: send the NCI open-state, roll + drop its loot, and
    mark it opened (one-shot). Port of C# HandleChestActivation. The 0x06 ack is
    sent by the caller (net.movement)."""
    if world_entity_manager.is_chest_opened(entity_id):
        log.info(f"[CHEST] '{conn.login_name}' re-clicked opened chest "
                 f"'{we.name}' (0x{entity_id:04X}) — no re-loot")
        return
    world_entity_manager.mark_chest_opened(entity_id)
    conn.send_to_client(_build_nci_activate(entity_id, activated=True))

    from . import loot as loot_manager
    level = max(1, getattr(conn, "player_level", 1) or 1)
    generators = [(we.item_generator, we.item_count or 1)] if we.item_generator else []
    if generators:
        loot_manager.generate_loot_for_monster(
            server, conn, we.pos_x, we.pos_y, we.pos_z, level, generators,
            difficulty="DUNGEON_BOSS")
    log.info(f"[CHEST] '{conn.login_name}' opened '{we.name}' "
             f"(gen='{we.item_generator}') at ({we.pos_x:.0f},{we.pos_y:.0f})")


def activate_shrine(server: "GameServer", conn: "RRConnection",
                    entity_id: int, we: WorldEntityData) -> None:
    """Activate a clicked shrine: play the NCI activate visual and apply the
    shrine's buff modifier to the player (the obelisk's actual effect). The 0x06
    ack is sent by the caller."""
    conn.send_to_client(_build_nci_activate(entity_id, activated=False))
    from . import player_modifiers
    mod_gc = f"{we.gc_type}.Modifier"   # content convention: <shrine>.Modifier
    applied = player_modifiers.apply_buff(conn, mod_gc, _SHRINE_BUFF_DURATION_S)
    log.info(f"[SHRINE] '{conn.login_name}' activated '{we.name}' "
             f"buff='{mod_gc}' applied={applied}")


# ── Boss exit-gate open-on-death (DoorsToOpenOnDeath) ────────────────────────


def _build_gate_open(entity_id: int) -> bytes:
    """"Open" a boss gate by REMOVING its portcullis entity — the proven
    ``0x07 0x05 <eid> 0x06`` entity-destroy stream (BeginStream · Destroy · eid ·
    EndStream), identical to the live-confirmed mob despawn in
    ``combat._broadcast_despawn``.

    Why destroy, not a Door open ComponentUpdate: a bare ``0x03 <eid> 0x0A``
    entity-update (the C#-derived NCI activate shape) was **live-disproven**
    2026-07-10 — with a debugger breakpoint on the client's entity-update
    dispatcher gated to the gate's eid, a boss kill produced NO hit, i.e. the
    unwrapped update never reaches processing (entity ops ride a BeginStream
    frame). Removing the portcullis makes the archway (a separate
    ``BossDoorArch_01`` visual) passable — the functional "open". A native
    swing-open animation needs the door's real update wire pinned via a live
    trace and is deferred.
    """
    w = LEWriter()
    w.write_byte(0x07)                       # BeginStream
    w.write_byte(0x05)                       # Destroy entity
    w.write_uint16(entity_id)                # gate entity id
    w.write_byte(0x06)                       # EndStream
    return w.to_array()


def _instance_conns(server: "GameServer", zone_gc_type: str,
                    instance_id: int) -> List["RRConnection"]:
    """Spawned players in one zone INSTANCE (mirrors combat._broadcast_despawn)."""
    return [c for c in list(server.connections.values())
            if getattr(c, "is_spawned", False)
            and getattr(c, "current_zone_gc_type", None) == zone_gc_type
            and getattr(c, "instance_id", 0) == instance_id]


def open_boss_doors(server: "GameServer", monster) -> int:
    """Open every gate the killed ``monster`` unlocks via ``DoorsToOpenOnDeath``.

    Reads the mob's content for its door name(s), matches them against the gates
    live in the mob's own zone INSTANCE (bridging the DB node-name vs. content
    ``Name`` mismatch via :func:`boss_door_resolver.door_name_of`), records each
    gate opened, and broadcasts the Door open ComponentUpdate to that instance's
    players. Returns the number of gates opened. Only ``world.*`` mobs author the
    attribute, so a grunt kill short-circuits without touching the filesystem.
    """
    # DoorsToOpenOnDeath is authored on the world.* ENTITY node (e.g.
    # world.dungeon00.mob.boss), NOT the shared creatures.* stat row the mob is
    # tracked as — so key off entity_gc_type (falls back to gc_type).
    gc_type = (getattr(monster, "entity_gc_type", "")
               or getattr(monster, "gc_type", "") or "")
    if not gc_type.lower().startswith("world."):
        return 0

    from ..data import boss_door_resolver as bdr
    doornames = bdr.doors_opened_by(gc_type)
    if not doornames:
        return 0

    zone_gc_type = getattr(monster, "zone_gc_type", "") or ""
    instance_id = getattr(monster, "instance_id", 0)
    registry = getattr(server, "world_instances", None)
    inst = registry.find(zone_gc_type, instance_id) if registry is not None else None
    if inst is None:
        return 0

    gate_eids = [
        eid for eid in inst.entity_ids
        if (we := world_entity_manager.find_by_entity_id(eid)) is not None
        and we.entity_type == "gate"
        and bdr.door_name_of(we.gc_type) in doornames
    ]
    if not gate_eids:
        return 0

    conns = _instance_conns(server, zone_gc_type, instance_id)
    for eid in gate_eids:
        world_entity_manager.mark_gate_opened(eid)
        packet = _build_gate_open(eid)
        for conn in conns:
            conn.send_to_client(packet)

    log.info(f"[BossDoor] '{getattr(monster, 'label', gc_type)}' death opened "
             f"{len(gate_eids)} gate(s) {gate_eids} for {len(conns)} player(s) "
             f"in ({zone_gc_type}, {instance_id})")
    return len(gate_eids)


def reopen_gates_for_joiner(conn: "RRConnection", entity_ids: List[int]) -> None:
    """Re-send the Door open for any already-opened gate to a late joiner.

    The instance's create-stream snapshot spawns gates closed (built once, at
    populate — usually before the boss dies). A player entering an instance whose
    boss is already dead must be told the gate is open, or it stays a wall for
    them. Sent right after the snapshot so the create precedes the update.
    """
    for eid in entity_ids:
        if world_entity_manager.is_gate_opened(eid):
            conn.send_to_client(_build_gate_open(eid))
