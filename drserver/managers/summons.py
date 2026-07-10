"""Player summon units — Monster Bait, Build Snowman (SpellSpawnEffect skills).

The client skills carry a ``SpellSpawnEffect`` whose ``SpawnUnit`` names a
``base.Summonable`` creature; the SERVER must create the entity (the entity
stream is server-owned — same model as the Bling Gnome):

* **Monster Bait** (``skills.generic.SummonMonsterBait``): TargetType=POSITION
  → arrives on the 0x51 position-cast. Spawns ``skills.generic.MonsterBait``
  ("Yum yums", Speed 0, Lifespan 30 s) at the cast position.
* **Build Snowman** (``skills.generic.SummonSnowMan``): TargetType=SELF →
  arrives on the 0x52 self-cast. Spawns
  ``creatures.summon.snowman.base.Snowman_Summon`` ("Chill Bill") next to the
  caster.

Wire: both creatures' ``Behavior`` children extend **MonsterBehavior2** — the
exact component set our live-proven monster spawn stream emits (monsters.py
OP1–OP8). The create stream below reuses those byte shapes verbatim, with the
Bling Gnome's owner-bearing Unit init (unitFlags ``0x16|0x01`` + owner ref +
HP/mana — the DRS-NET-proven henchman variant) so the unit binds to the caster.

The OWNER's client brain runs owned henchmen natively (live-proven 2026-06-12:
the snowman followed its caster with no enrollment) — what it FIGHTS with is
the manipulators block (same rule as mobs), and whether it engages is the
unit's own Behavior desc (snowman AGGRESSIVE/AgroRange 90, bait NEUTRAL).
Unit deaths are client-simulated: the snowman MELTS (HealthDecline modifier),
so he gets NO server lifespan; the bait keeps its authored 30 s.

My Flaming Buddy (``FireMeleeSummon``) is NOT here: its spawn rides a
client-rolled ON_HIT proc the server cannot observe — needs a live trace first.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core import log
from ..util.byte_io import LEWriter
from ..data.gc_object import write_gc_type
from .bling_gnome import henchman_hp_wire  # shared henchman health curve

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection


@dataclass(frozen=True)
class SummonDef:
    """One summonable unit type (client .gc data)."""
    skill_match: str          # lowercase substring of the casting skill gc
    unit_gc_type: str         # SpawnUnit entity class
    lifespan: Optional[float]  # server despawn; None = client-simulated death
    max_health: float         # Description.MaxHealth (henchman-curve scale)
    label: str


MONSTER_BAIT = SummonDef(
    skill_match="summonmonsterbait",
    unit_gc_type="skills.generic.MonsterBait",
    lifespan=30.0,            # MonsterBait.gc Lifespan (authored 30 s)
    max_health=0.9,           # MonsterBait.gc MaxHealth
    label="Monster Bait",
)

# The snowman has NO server lifespan: his death is the client-simulated MELT —
# Snowman_Summon.gc Modifiers.HealthDecline (Modifier_Healthregen −1.55/s,
# level-table-scaled) drains him; friendly ICE damage heals him back
# (DamageToHealingRatio 0.4 — the skill text's "use other Mage Ice skills to
# keep him around longer"). A server 0x05 remove here yanked him mid-melt
# (2026-06-12 live: "disappears after 10-15 s").
SNOWMAN = SummonDef(
    skill_match="summonsnowman",
    unit_gc_type="creatures.summon.snowman.base.Snowman_Summon",
    lifespan=None,
    max_health=0.3,           # Snowman_Summon.gc MaxHealth
    label="Snowman",
)

_DEFS = (MONSTER_BAIT, SNOWMAN)


def def_for_skill(skill_gc_class: Optional[str]) -> Optional[SummonDef]:
    """The SummonDef a skill cast should spawn, or None."""
    s = (skill_gc_class or "").lower()
    for d in _DEFS:
        if d.skill_match in s:
            return d
    return None


@dataclass
class SummonState:
    entity_id: int
    behavior_id: int
    owner_login: str
    summon_def: SummonDef
    zone_gc_type: str
    instance_id: int
    spawned_at: float
    despawn_task: Optional[asyncio.Task] = None


class SummonManager:
    """Live player summons (bait / snowman), keyed by owner login. One of
    each TYPE per player; re-casting replaces the previous one."""

    def __init__(self, server: "GameServer"):
        self._server = server
        self._summons: Dict[str, List[SummonState]] = {}

    # ── Owner resolution ─────────────────────────────────────────────────

    def owner_conn_for_entity(self, entity_id: int) -> "Optional[RRConnection]":
        """The owner connection of a live summon with ``entity_id``, else None.

        The combat-telemetry hook reports the *attacker* entity of a killing
        blow (``combat_hook.c`` reads ``attacker[+0x80]``); a mob finished off by
        a player's snowman/bait reports the SUMMON's eid, not the avatar's. This
        maps that eid back to the owner so the kill is still credited (loot/XP)."""
        for login, states in self._summons.items():
            if any(st.entity_id == entity_id for st in states):
                for conn in self._server.connections.values():
                    if conn.login_name == login:
                        return conn
        return None

    # ── Casting triggers ─────────────────────────────────────────────────

    def try_cast(self, conn: "RRConnection", skill_gc_class: Optional[str],
                 pos_x: Optional[float] = None,
                 pos_y: Optional[float] = None,
                 pos_z: Optional[float] = None) -> bool:
        """Spawn the summon for a cast skill. Returns True when the skill was
        a summon (caller skips its normal handling). Position defaults to the
        caster (snowman SELF-cast); the bait passes its 0x51 cast position."""
        d = def_for_skill(skill_gc_class)
        if d is None:
            return False
        if not conn.is_spawned or not conn.login_name:
            return True
        self._despawn_existing(conn, d)
        self._spawn(conn, d,
                    pos_x if pos_x is not None else conn.player_pos_x + 10.0,
                    pos_y if pos_y is not None else conn.player_pos_y + 10.0,
                    pos_z if pos_z is not None else conn.player_pos_z)
        return True

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _spawn(self, conn: "RRConnection", d: SummonDef,
               pos_x: float, pos_y: float, pos_z: float) -> None:
        st = SummonState(
            entity_id=self._server.allocate_entity_id(),
            behavior_id=self._server.allocate_entity_id(),
            owner_login=conn.login_name,
            summon_def=d,
            zone_gc_type=conn.current_zone_gc_type or "",
            instance_id=conn.instance_id,
            spawned_at=time.monotonic(),
        )
        level = max(1, min(255, getattr(conn, "player_level", 1) or 1))
        packet = self.build_summon_spawn_packet(
            st, d, level, henchman_hp_wire(level, d.max_health),
            self._owner_ref(conn), pos_x, pos_y, pos_z,
            skills_id=self._server.allocate_entity_id(),
            manipulators_id=self._server.allocate_entity_id(),
            modifiers_id=self._server.allocate_entity_id(),
        )
        self._broadcast(conn, packet)
        self._summons.setdefault(conn.login_name, []).append(st)
        if d.lifespan is not None:
            st.despawn_task = asyncio.create_task(
                self._despawn_after(conn, st, d.lifespan))
        log.info(f"[SUMMON] '{conn.login_name}' spawned {d.label} "
                 f"0x{st.entity_id:04X} at ({pos_x:.0f},{pos_y:.0f}) "
                 f"lifespan={d.lifespan}")

    async def _despawn_after(self, conn: "RRConnection", st: SummonState,
                             delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self._remove(conn, st, send_packets=True)
        except asyncio.CancelledError:
            pass

    def _despawn_existing(self, conn: "RRConnection", d: SummonDef) -> None:
        for st in list(self._summons.get(conn.login_name or "", [])):
            if st.summon_def is d:
                self._remove(conn, st, send_packets=True)

    def cleanup(self, conn: "RRConnection") -> None:
        """Zone transition / disconnect: drop state silently for the owner,
        despawn for peers (summons are DeathOnZone / left behind)."""
        for st in self._summons.pop(conn.login_name or "", []):
            if st.despawn_task is not None:
                st.despawn_task.cancel()
            remove = _entity_remove_packet(st.entity_id)
            for peer in self._instance_peers(conn):
                peer.send_to_client(remove)

    def _remove(self, conn: "RRConnection", st: SummonState,
                send_packets: bool) -> None:
        owned = self._summons.get(st.owner_login, [])
        if st in owned:
            owned.remove(st)
        if st.despawn_task is not None:
            st.despawn_task.cancel()
            st.despawn_task = None
        if send_packets:
            self._broadcast(conn, _entity_remove_packet(st.entity_id))
        log.info(f"[SUMMON] '{st.owner_login}' {st.summon_def.label} "
                 f"0x{st.entity_id:04X} despawned")

    # ── Packet builder ───────────────────────────────────────────────────

    @staticmethod
    def build_summon_spawn_packet(st: SummonState, d: SummonDef, level: int,
                                  hp_wire: int, owner_entity_id: int,
                                  pos_x: float, pos_y: float, pos_z: float,
                                  skills_id: int, manipulators_id: int,
                                  modifiers_id: int) -> bytes:
        """One framed summon create stream.

        OP order and byte shapes are the live-proven monster spawn stream
        (monsters.py BuildMonsterSpawnPacket port) — both units' Behavior is a
        MonsterBehavior2 — except OP2's Unit init, which uses the Bling
        Gnome's owner-bearing variant (DRS-NET BuildEntitySnapshotPacket:
        unitFlags 0x16|0x01 gate owner u16 + HP u32 + mana u32 + byte).
        """
        px, py, pz = int(pos_x * 256), int(pos_y * 256), int(pos_z * 256)
        unit_flags = 0x16 | (0x01 if owner_entity_id else 0x00)

        w = LEWriter()
        w.write_byte(0x07)                          # BeginStream

        # ── OP1: create ──
        w.write_byte(0x01)
        w.write_uint16(st.entity_id)
        write_gc_type(w, d.unit_gc_type, preserve_case=True)

        # ── OP2: init (Entity + owner-bearing Unit + StockUnit zero tail) ──
        w.write_byte(0x02)
        w.write_uint16(st.entity_id)
        w.write_uint32(0x06)                        # visible | activatable
        w.write_int32(px); w.write_int32(py); w.write_int32(pz)
        w.write_int32(0)                            # heading
        w.write_byte(0x00)
        w.write_byte(unit_flags)
        w.write_byte(level & 0xFF)
        w.write_uint16(0); w.write_uint16(0)
        if unit_flags & 0x01:
            w.write_uint16(owner_entity_id)
        w.write_uint32(hp_wire)
        w.write_uint32(0)                           # mana wire
        w.write_byte(0x00)
        w.write_byte(0x00)
        w.write_uint16(0); w.write_uint16(0)
        w.write_byte(0x00)
        w.write_uint16(0); w.write_uint32(0)
        w.write_byte(0x00)
        w.write_uint32(0); w.write_uint32(0); w.write_uint32(0)

        # ── OP3: Behavior (MonsterBehavior2 — mob-proven init body) ──
        w.write_byte(0x32)
        w.write_uint16(st.entity_id)
        w.write_uint16(st.behavior_id)
        write_gc_type(w, f"{d.unit_gc_type}.Behavior", preserve_case=True)
        w.write_byte(0x01)
        w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00); w.write_byte(0x00)
        w.write_byte(0x85); w.write_byte(0x00)
        for _ in range(5):
            w.write_uint32(0)
        w.write_byte(0x00)
        w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)
        w.write_byte(0x0F)
        w.write_uint16(0xFFFF); w.write_uint16(0xFFFF); w.write_uint16(0xFFFF)
        w.write_byte(0x10)
        w.write_uint32(0); w.write_uint32(0)
        w.write_uint16(0x0001)

        # ── OP4: skills (empty, mob shape) ──
        w.write_byte(0x32)
        w.write_uint16(st.entity_id)
        w.write_uint16(skills_id)
        write_gc_type(w, "skills")
        w.write_byte(0x01)
        w.write_byte(0xFF); w.write_byte(0xFF); w.write_byte(0xFF)
        w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)

        # ── OP5: manipulators — the unit's REAL weapon/skill loadout, the
        # mob-proven body shapes (monsters.py OP5). The owner's client brain
        # runs owned henchmen natively (the snowman follows on its own) and
        # ATTACKS WITH WHATEVER THIS BLOCK DECLARES — empty manipulators were
        # the 2026-06-12 "snowman only follows, never fights" bug. Both bait
        # and snowman author PrimaryWeapon extends base melee, ID 10 — exactly
        # the creature_manipulators fallback entry (whether the brain engages
        # comes from the unit's own Behavior desc: snowman AGGRESSIVE,
        # bait NEUTRAL). ──
        from . import creature_manipulators
        entries = creature_manipulators.manipulators_for(d.unit_gc_type)
        w.write_byte(0x32)
        w.write_uint16(st.entity_id)
        w.write_uint16(manipulators_id)
        write_gc_type(w, "manipulators")
        w.write_byte(0x01)
        w.write_byte(len(entries) & 0xFF)
        for entry in entries:
            write_gc_type(w, entry.gc_type, preserve_case=True)
            w.write_uint32(entry.manip_id)
            if entry.kind == creature_manipulators.KIND_SKILL:
                w.write_byte(0x00)                  # ActiveSkill readData tail
                w.write_byte(0x00)                  # readState flags (none)
            else:
                w.write_byte(0x00); w.write_byte(0x00); w.write_byte(0x00)
                w.write_byte(0x00)                  # +0x7f (zeros live-proven)
                w.write_byte(0x00)                  # +0x83 flags
                w.write_byte(0x00)                  # contained-mods count
                w.write_uint16(0x0000)              # readState +0x86
                if entry.kind != creature_manipulators.KIND_RANGED:
                    w.write_byte(0x00)              # melee-only +0x8d
                w.write_uint16(0x0000)              # readState target

        # ── OP6: modifiers (mob shape) ──
        w.write_byte(0x32)
        w.write_uint16(st.entity_id)
        w.write_uint16(modifiers_id)
        write_gc_type(w, "modifiers")
        w.write_byte(0x01)
        w.write_uint32(0)
        w.write_byte(0x00)
        w.write_uint32(0)

        # ── OP7: SpawnAction ──
        w.write_byte(0x35)
        w.write_uint16(st.behavior_id)
        w.write_byte(0x04); w.write_byte(0x04); w.write_byte(0xFF)
        w.write_int32(px); w.write_int32(py); w.write_int32(pz)
        w.write_uint16(st.entity_id)
        w.write_byte(0x02)
        w.write_uint32(hp_wire)

        # ── OP8: MoverUpdate (anchor in place) ──
        w.write_byte(0x35)
        w.write_uint16(st.behavior_id)
        w.write_byte(0x65); w.write_byte(0x00); w.write_byte(0x01); w.write_byte(0x03)
        w.write_int32(0); w.write_int32(px); w.write_int32(py)
        w.write_byte(0x02)
        w.write_uint32(hp_wire)

        w.write_byte(0x06)                          # EndStream
        return w.to_array()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _owner_ref(self, conn: "RRConnection") -> int:
        player = getattr(conn, "player", None)
        player_id = getattr(player, "id", 0) if player is not None else 0
        return player_id or self._server.get_player_avatar_id(conn.login_name)

    def _broadcast(self, conn: "RRConnection", packet: bytes) -> None:
        if conn.is_connected:
            conn.send_to_client(packet)
        for peer in self._instance_peers(conn):
            peer.send_to_client(packet)

    def _instance_peers(self, conn: "RRConnection") -> List["RRConnection"]:
        return [other for other in self._server.connections.values()
                if other is not conn and other.is_spawned
                and other.current_zone_gc_type == conn.current_zone_gc_type
                and other.instance_id == conn.instance_id]


def _entity_remove_packet(entity_id: int) -> bytes:
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x05)
    w.write_uint16(entity_id)
    w.write_byte(0x06)
    return w.to_array()
