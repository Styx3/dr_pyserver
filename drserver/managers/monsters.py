"""Monster manager — creature spawning and lifecycle.

Ported from C# CombatManager.cs / CombatPackets.cs. Loads creature data from
the SQLite creatures table, builds monster GCObject entities, and spawns them
into the relevant zones. Client-side monster AI is fully authoritative (matches
the C# server's design — the client handles combat, the server spawns and relays).

Phase 7: MVP with entity spawning and zone integration. Movement/AI and combat
validation are deferred to Phase 8+.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, TYPE_CHECKING

from ..core import log
from ..db import game_database as db
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection


@dataclass
class MonsterData:
    """Loaded creature row from SQLite."""
    id: int
    gc_type: str              # e.g. creatures.amphibs.skull_dog.divine.champion
    label: str                # display label e.g. "Divine Skull Dog"
    behaviour_type: str
    difficulty: str           # GRUNT, VETERAN, CHAMPION, HERO, BOSS, ...
    hit_points: int
    mana_points: int
    hp_packet: int            # hit_points * 256 (wire format)
    mp_packet: int            # mana_points * 256
    health_mod: float         # creatures.max_health — HealthMod multiplier (×curve)
    level_range: tuple[int, int]
    speed: float
    visual: str               # visual asset tag
    pvp_rating: int
    treasure_generators: List[tuple[str, int]]   # (gen_name, count)
    # Combat stats
    base_damage: int
    attack_rating: int
    defense_rating: float     # authored multiplier (0.75/1.0) — feeds the
                              # MonsterDefenseRating curve, NOT a flat rating
    crit_chance: int
    attack_range: float
    collision_radius: int
    # Numeric ``Difficulty`` (the real HP multiplier; the tier sets its default,
    # bosses/leaders override it — bible §14.4). None ⇒ fall back to the tier.
    difficulty_value: "float | None" = None


class MonsterManager:
    """Global registry of loaded creature definitions."""

    def __init__(self):
        self._creatures: Dict[str, MonsterData] = {}      # gc_type -> data
        self._loaded = False

    def load(self) -> None:
        """Load all creature definitions from SQLite."""
        if self._loaded:
            return
        self._creatures.clear()

        try:
            for row in db.execute_reader("SELECT * FROM creatures").fetchall():
                gc_type = db.get_string(row, "gc_type")
                if not gc_type:
                    continue

                difficulty = db.get_string(row, "creature_difficulty").upper()
                hp = db.get_int(row, "hit_points", 100)
                mp = db.get_int(row, "mana_points", 0)

                # Level range from difficulty tier.
                level_min, level_max = _difficulty_levels(difficulty)

                # Treasure generators.
                treasures = []
                for i in range(1, 5):
                    gen_name = db.get_string(row, f"treasure_gen{i}")
                    gen_count = db.get_int(row, f"treasure_count{i}")
                    if gen_name and gen_count > 0:
                        treasures.append((gen_name, gen_count))

                def _safe_int(key: str, default: int = 0) -> int:
                    try:
                        v = db.get_string(row, key)
                        return int(v) if v else default
                    except (ValueError, TypeError):
                        return default

                def _safe_float(key: str, default: float = 0.0) -> float:
                    try:
                        v = db.get_string(row, key)
                        return float(v) if v else default
                    except (ValueError, TypeError):
                        return default

                md = MonsterData(
                    id=db.get_int(row, "id"),
                    gc_type=gc_type,
                    label=db.get_string(row, "label") or gc_type,
                    behaviour_type=db.get_string(row, "behaviour_type"),
                    difficulty=difficulty,
                    hit_points=db.get_int(row, "hit_points", 100),
                    mana_points=db.get_int(row, "mana_points", 0),
                    hp_packet=_safe_int("hit_points_packet", hp * 256),
                    mp_packet=_safe_int("mana_points_packet", mp * 256),
                    health_mod=_safe_float("max_health", 1.0) or 1.0,
                    level_range=(level_min, level_max),
                    speed=_safe_float("speed", 3.0),
                    visual=db.get_string(row, "visual"),
                    pvp_rating=_safe_int("attack_rating", 0),
                    treasure_generators=treasures,
                    base_damage=_safe_int("base_damage", 5),
                    attack_rating=_safe_int("attack_rating", 10),
                    defense_rating=_safe_float("defense_rating", 5.0),
                    crit_chance=_safe_int("critical_chance", 5),
                    attack_range=_safe_float("attack_range", 5.0),
                    collision_radius=_safe_int("collision_radius", 2),
                    difficulty_value=(_safe_float("difficulty", 0.0) or None),
                )
                self._creatures[gc_type] = md

            log.info(f"[MonsterManager] loaded {len(self._creatures)} creatures from SQLite")
            self._loaded = True
        except Exception as ex:
            log.error(f"[MonsterManager] load error: {ex}")

monster_manager = MonsterManager()


def resolve_monster_entity_gc_type(zone_name: str, gc_type: str) -> "str | None":
    """The entity GC type the client must receive.

    Fully data-driven: the spawners hand us the unit ``Type`` straight from the
    ``.enc`` content, which the client loads verbatim — so both namespaces the
    content actually uses pass through unchanged (case preserved for the wire):

    * ``world.<dungeon>.mob.*`` — the per-dungeon mob assets, and
    * raw ``creatures.*`` — elite01/boss encounter tables name concrete
      creatures directly (e.g. ``creatures.fade.lichLord.Fire.Hero``); these
      are real client content, and the C# reference sends them verbatim too
      (its dungeon00 spawn units are ``creatures.forestCreatures.Warg.Basic.
      Pup`` etc. — a live-proven path).

    Anything else (e.g. a fabricated family name like the removed dungeon00
    ``melee0N.rank<N>`` map emitted) resolves to None and the caller skips it:
    a missing mob is fine, a desynced stream is a crash.
    """
    low = (gc_type or "").lower()
    if low.startswith("world.") or low.startswith("creatures."):
        return gc_type
    return None


# Defer client-AI enrollment until the player engages.
#
# The ``0x64 FollowClient`` control block hands a monster's movement+attack AI to
# the client (CombatPackets.cs:464 "client starts sending 0x65 position updates").
# Once enrolled, the client self-sims the mob's aggro/chase/attack from the brain
# content (Perception=500, no Leashed flag → chases with no give-up) and applies
# the player damage LOCALLY. That damage is what trips the avatar HP-synch crash
# (FUN_005dd900): the client lowers its own avatar HP, and any server avatar-HP
# trailer that doesn't match crashes — UNLESS the avatar's active action carries
# the local-input-authority bit (+0x95 bit0), which is set ONLY by the action
# going Start→Active. Live-observed: a freshly-warped avatar does NOT self-advance
# to Active during a 4-5 s idle window (so a timed spawn delay can't help — the
# calm already exists and still crashes). The ONLY thing that blesses the action
# is the player's OWN attack action.
#
# So we spawn mobs WITHOUT 0x64 — passive + anchored at their spawn (no client AI,
# no aggro, no chase, no damage) — and enroll them (send the 0x64 burst) the moment
# the player issues their first attack (net.movement 0x50 path), by which point the
# avatar's action is Active/0x11 and protected. Set True to restore the legacy
# inline-enroll (instant-aggro → crash) behavior. See
# project_l2_divergence_combat_authority + docs/CLIENT_SERVER_MODEL §7.
ENROLL_MONSTERS_AT_SPAWN = False


def build_monster_enroll_stream(
    monsters: "List[tuple[int, int]]") -> bytes:
    """Build ONE deferred client-AI enrollment stream for ``[(behavior_id,
    hp_wire), …]``.

    Sent to a single client (per-client ownership) once it engages, this is the
    ``0x64 FollowClient`` block we omit from the create stream when
    :data:`ENROLL_MONSTERS_AT_SPAWN` is False — it flips each monster from a
    passive server-owned entity to a client-simulated one (the client begins
    running its aggro/attack AI and emitting ``0x65`` position updates). One
    block per monster in a single ``0x07``/``0x06`` stream (mirrors the
    multi-block control-reset stream in ``GameServer.send_client_control_reset``):

      ``0x07`` | (per mob) ``0x35`` <behaviorId:u16> ``0x64`` ``0x01`` ``0x02``
      <hpWire:u32> | ``0x06``

    Returns ``b""`` for an empty list (nothing to enroll).
    """
    if not monsters:
        return b""
    w = LEWriter()
    w.write_byte(0x07)                              # BeginStream
    for behavior_id, hp_wire in monsters:
        if not behavior_id:
            continue
        w.write_byte(0x35)                          # ComponentUpdate
        w.write_uint16(behavior_id & 0xFFFF)
        w.write_byte(0x64)                          # FollowClient — client owns AI
        w.write_byte(0x01)                          # control ON
        w.write_byte(0x02)                          # Synch flag
        w.write_uint32(hp_wire & 0xFFFFFFFF)
    w.write_byte(0x06)                              # EndStream
    return w.to_array()


def build_zone_monsters(server: "GameServer", zone_gc_type: str,
                        pos: tuple[float, float, float], count: int = 5,
                        zone_name: str = "",
                        instance_id: int = 0) -> List[tuple[int, bytes]]:
    """Build ``count`` monster create streams near ``pos`` (admin ``/spawn`` live
    debug + the generic non-maze fallback).

    Data-driven: the mobs are sampled from the level's own ``.enc`` encounter
    table (:func:`dungeon_spawner.sample_mob_units`) so they are real
    ``world.<dungeon>.mob.*`` assets the client can load — no hardcoded creature
    map. Returns ``[]`` for a zone with no encounter content (nothing loadable to
    spawn). Placement is jittered around ``pos`` and fixed for the instance.
    Delegates packet framing to :func:`build_monsters_from_spawns` (shared C#
    op-order). See [[world-instance]].
    """
    from . import dungeon_spawner

    units = dungeon_spawner.sample_mob_units(zone_name, count)
    if not units:
        log.info(f"[MonsterManager] no encounter content for zone "
                 f"'{zone_name}' (gc='{zone_gc_type}') — spawning none")
        return []

    base_x, base_y, base_z = pos
    spawns = [
        (entity_gc, creature_gc,
         base_x + random.uniform(-100, 100),
         base_y + random.uniform(-100, 100),
         base_z, 0.0)
        for entity_gc, creature_gc in units
    ]
    return build_monsters_from_spawns(server, zone_name, zone_gc_type, spawns,
                                      instance_id=instance_id)


def build_monsters_from_spawns(
    server: "GameServer", zone_name: str, zone_gc_type: str,
    spawns: "List[tuple[str, str, float, float, float, float]]",
    instance_id: int = 0,
) -> List[tuple[int, bytes]]:
    """Build monster create streams at explicit, pre-computed positions.

    ``spawns`` is ``[(entity_gc_type, creature_gc_type, x, y, z, heading), …]``:

    * ``entity_gc_type`` — what the client must load. The data-driven maze
      spawner supplies the real ``world.<dungeon>.mob.*`` asset from the ``.enc``;
      the legacy static/random paths supply a raw ``creatures.*`` type that is
      mapped through :func:`resolve_monster_entity_gc_type`.
    * ``creature_gc_type`` — the concrete ``creatures.*`` row used for HP/level
      stats. Empty means "same as ``entity_gc_type``" (the legacy boss-arena
      ``dungeon_spawns`` path, where the spawn IS a creature).

    Entries that resolve to no loadable asset, or whose creature is unknown (no
    stats → unsafe HP synch), are skipped so the stream never desyncs — a missing
    mob is fine, a desynced stream is a crash. Shares the packet layout with
    :func:`build_zone_monsters`.
    """
    if not monster_manager._loaded:
        monster_manager.load()

    built: List[tuple[int, bytes]] = []
    skipped = 0
    for entity_in, creature_in, x, y, z, heading in spawns:
        creature_gc = (creature_in or entity_in)
        entity_gc_type = resolve_monster_entity_gc_type(zone_name, entity_in)
        if entity_gc_type is None:
            skipped += 1
            continue
        md = monster_manager._creatures.get(creature_gc.lower())
        if md is None:
            skipped += 1
            continue
        built.append(_build_monster_stream(
            server, md, entity_gc_type,
            pos_x=x, pos_y=y, pos_z=z, heading=heading,
            zone_gc_type=zone_gc_type, zone_name=zone_name,
            instance_id=instance_id,
        ))

    log.info(f"[MonsterManager] built {len(built)} monster streams from "
             f"{len(spawns)} spawns for '{zone_name}' (skipped {skipped})")
    return built


def _build_monster_stream(
    server: "GameServer", md: "MonsterData", entity_gc_type: str,
    pos_x: float, pos_y: float, pos_z: float, heading: float,
    zone_gc_type: str, zone_name: str = "", instance_id: int = 0,
) -> tuple[int, bytes]:
    """Serialize one monster create stream (faithful port of C#
    ``CombatPackets.BuildMonsterSpawnPacket``) and register it with combat.

    Per-monster op order MUST match C# exactly:
      0x07 | 0x01 create | 0x02 init | 0x32 behavior | 0x32 skills |
      0x32 manipulators | 0x32 modifiers | 0x35 SpawnAction |
      0x35 MoverUpdate(0x65) | 0x35 FollowClient(0x64) | 0x06
    """
    from ..data.gc_object import write_gc_type
    from . import creature_manipulators, monster_health

    entity_id = server.allocate_entity_id()
    behavior_id = server.allocate_entity_id()
    skills_id = server.allocate_entity_id()
    manipulators_id = server.allocate_entity_id()
    modifiers_id = server.allocate_entity_id()
    # Level + HP MUST match what the client derives locally — it compares the
    # entity-synch HP exactly and pops a fatal "sync error" otherwise. The client
    # stores the monster HP as raw level-scaled HP × 256 (live crash log: "Dew
    # Valley Pup" local HP 29184 == 114 * 256). monster_hp_wire returns that ×256
    # value; all 0x02 synch trailers below carry it. See monster_health.py.
    level = max(1, min(255, monster_health.monster_level(md.difficulty, zone_name)))
    # HP MUST equal the client's locally-computed value or the zero-tolerance
    # entity-synch compare (FUN_005dd900) crashes. The client derives it from the
    # creature content: curve(level) × HealthMod × difficultyMod. HealthMod is the
    # creature's max_health field (md.health_mod) — dropping it sent e.g. 332
    # instead of 332×0.75=249 for a dungeon01 "Whisker Flinger" → crash.
    hp_wire = monster_health.monster_hp_wire(
        md.difficulty, zone_name, md.health_mod,
        difficulty_value=md.difficulty_value)
    px = int(pos_x * 256)
    py = int(pos_y * 256)
    pz = int(pos_z * 256)
    hd = int(heading * 256)
    behaviour_type = md.behaviour_type or "creatures.base.behavior.melee"
    # ★2026-06-21 (bible §14.4): a few unique bosses (wheelerboss/Rotgut, …)
    # extend the top-level ``base.RangedUnit`` whose Behavior block roots at the
    # RAW ``MonsterBehavior2`` (not a ``creatures.base.behavior.*`` class). Sent
    # verbatim, the client instantiates the bare behavior which never wires up a
    # weapon → its combat-stat setup derefs a NULL weapon (FUN_00535950
    # ``[ebp+0x82]``) → ACCESS_VIOLATION on warp into the boss room. Sanitise any
    # non-concrete behaviour to a real ``creatures.base.behavior.*`` matched to the
    # primary weapon's kind (ranged cannon → Ranged like the working pukers; melee
    # → Melee; skill-only → Caster) so the unit both has a weapon hookup AND
    # attacks the right way.
    if not behaviour_type.lower().startswith("creatures.base.behavior."):
        _ents = creature_manipulators.manipulators_for(md.gc_type)  # cached
        _wpns = [e for e in _ents
                 if e.kind != creature_manipulators.KIND_SKILL]
        if _wpns and _wpns[0].kind == creature_manipulators.KIND_RANGED:
            behaviour_type = "creatures.base.behavior.Ranged"
        elif not _wpns:
            behaviour_type = "creatures.base.behavior.Caster"
        else:
            behaviour_type = "creatures.base.behavior.Melee"

    w = LEWriter()
    w.write_byte(0x07)                              # BeginStream

    # ── OP1: create monster entity (mapped base / dungeon-rank type) ──
    w.write_byte(0x01)
    w.write_uint16(entity_id)
    write_gc_type(w, entity_gc_type, preserve_case=True)

    # ── OP2: init entity (Entity 21 + Unit 6 + StockUnit 25 bytes) ──
    w.write_byte(0x02)
    w.write_uint16(entity_id)
    # worldEntityFlags: 0x01 blocking (collider, client gates it on flags&1 @
    # 0x4d3560) | 0x02 activatable (mouse-pick) | 0x04 visible. 0x07 (adding
    # the blocking bit, like NPCs) was tried 2026-06-11 to stop mobs pathing
    # THROUGH the player while attacking, but REGRESSED basic attacks on
    # anchored/far mobs (boss arenas, other-room mobs): the collider stops the
    # avatar's attack approach OUTSIDE swing range, so the swing never fires.
    # Same-room mobs still worked only because they aggro and close the
    # distance themselves. Mobs therefore stay 0x06 (the C#/live-proven
    # value); the "runs through you" pattern needs a different fix.
    w.write_uint32(0x06)                            # Entity::readInit worldEntityFlags
    w.write_int32(px); w.write_int32(py); w.write_int32(pz)
    w.write_int32(hd)
    w.write_byte(0x00)
    # Unit::readInit (6)
    w.write_byte(0x00); w.write_byte(level)
    w.write_uint16(0); w.write_uint16(0)
    # StockUnit::setEntityId (25)
    w.write_byte(0x00); w.write_uint16(0); w.write_uint16(0)
    w.write_byte(0x00); w.write_uint16(0); w.write_uint32(0)
    w.write_byte(0x00); w.write_uint32(0); w.write_uint32(0); w.write_uint32(0)

    # ── OP3: behavior component ──
    w.write_byte(0x32)
    w.write_uint16(entity_id)
    w.write_uint16(behavior_id)
    write_gc_type(w, behaviour_type)
    w.write_byte(0x01)
    # Behavior::readInit (4)
    w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00); w.write_byte(0x00)
    # DFCStateMachine::readInit (23)
    w.write_byte(0x85); w.write_byte(0x00)
    for _ in range(5):
        w.write_uint32(0)
    w.write_byte(0x00)
    # UnitBehavior::readInit (3)
    w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)
    # StateMachine::ReadMessage
    w.write_byte(0x0F)
    w.write_uint16(0xFFFF); w.write_uint16(0xFFFF); w.write_uint16(0xFFFF)
    w.write_byte(0x10)
    w.write_uint32(0); w.write_uint32(0)
    w.write_uint16(0x0001)

    # ── OP4: skills component ──
    w.write_byte(0x32)
    w.write_uint16(entity_id)
    w.write_uint16(skills_id)
    write_gc_type(w, "skills")
    w.write_byte(0x01)
    w.write_byte(0xFF); w.write_byte(0xFF); w.write_byte(0xFF)
    w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)

    # ── OP5: manipulators component (the creature's OWN weapon/skills) ──
    # The client brain attacks with whatever this block declares: a Melee brain
    # swings the PrimaryWeapon, a Ranged brain fires it, a Caster brain casts
    # its CreatureBolt. Resolved per creature from the imported content
    # (creature_manipulators table) — see managers/creature_manipulators.py.
    # Per-kind body layouts follow C# CombatPackets (MeleeWeapon live-proven;
    # RangedWeapon/ActiveSkill UNVERIFIED against a live trace).
    entries = creature_manipulators.manipulators_for(md.gc_type)
    w.write_byte(0x32)
    w.write_uint16(entity_id)
    w.write_uint16(manipulators_id)
    write_gc_type(w, "manipulators")
    w.write_byte(0x01)
    w.write_byte(len(entries) & 0xFF)               # manipulator count
    for entry in entries:
        write_gc_type(w, entry.gc_type, preserve_case=True)
        w.write_uint32(entry.manip_id)              # authored manip "ID" (0 if none)
        if entry.kind == creature_manipulators.KIND_SKILL:
            # ActiveSkill body = readData (vtable+0xf0, FUN_0053dfb0: u32 id +
            # 1 byte) PLUS readState (vtable+0x100, FUN_00539ba0: 1 FLAGS byte,
            # each set bit pulling 2-4 more bytes). The C#-derived 5-byte shape
            # omitted the flags byte, so the client stole the next manip's 0xFF
            # name tag as flags=0xFF -> 12-byte over-read -> readType lands
            # mid-gc-string -> fatal "Invalid type tag 46" (the dungeon01-05
            # load freeze). Flags 0x00 = no optional fields.
            w.write_byte(0x00)
            w.write_byte(0x00)
        else:
            # Weapon::readData (FUN_00581710) field order after the u32 id:
            # +0x80, +0x81, +0x82, +0x7f, +0x83(flags), contained-mod count.
            # ALL ZERO is the live-proven shape. The 2026-06-11 run-through
            # probe put the authored Range in the 4th byte (+0x7f, the
            # FUN_00580be0 effective-distance fallback) — LIVE-DISPROVEN:
            # melee approach was unchanged at 8, and rifle mobs (pukers, 90)
            # started CHARGING the player instead of standing to shoot, so
            # +0x7f is not (just) range. Keep zeros; the post-enroll
            # run-through lives in the client brain's approach goal, not in
            # this body (entry.weapon_range stays available for server AI).
            w.write_byte(0x00)                      # +0x80
            w.write_byte(0x00)                      # +0x81
            w.write_byte(0x00)                      # +0x82
            w.write_byte(0x00)                      # +0x7f
            w.write_byte(0x00)                      # +0x83 flags (no u16 target)
            w.write_byte(0x00)                      # contained-mods count
            w.write_uint16(0x0000)                  # readState +0x86
            if entry.kind != creature_manipulators.KIND_RANGED:
                w.write_byte(0x00)                  # melee-only +0x8d
            w.write_uint16(0x0000)                  # readState target (0 = none)

    # ── OP6: modifiers component ──
    w.write_byte(0x32)
    w.write_uint16(entity_id)
    w.write_uint16(modifiers_id)
    write_gc_type(w, "modifiers")
    w.write_byte(0x01)
    w.write_uint32(0)
    w.write_byte(0x00)
    w.write_uint32(0)

    # ── OP7: SpawnAction (0x35/0x04) ──
    w.write_byte(0x35)
    w.write_uint16(behavior_id)
    w.write_byte(0x04); w.write_byte(0x04); w.write_byte(0xFF)
    w.write_int32(px); w.write_int32(py); w.write_int32(pz)
    w.write_uint16(entity_id)
    w.write_byte(0x02)
    w.write_uint32(hp_wire)                         # MaxHPWire

    # ── OP8: MoverUpdate (0x35/0x65) ──
    w.write_byte(0x35)
    w.write_uint16(behavior_id)
    w.write_byte(0x65); w.write_byte(0x00); w.write_byte(0x01); w.write_byte(0x03)
    w.write_int32(hd); w.write_int32(px); w.write_int32(py)
    w.write_byte(0x02)
    w.write_uint32(hp_wire)                         # CurrentHPWire

    # ── FollowClient (0x35/0x64) — client owns this entity's AI ──
    # Deferred by default: spawning a mob already enrolled makes the client
    # self-sim its aggro/attack and hit the player during the avatar's
    # unprotected Start window → HP-synch crash. We instead spawn it passive
    # (server-owned, anchored) and enroll it on the player's first attack, once
    # the avatar action is Active/0x11 and protected. See ENROLL_MONSTERS_AT_SPAWN
    # + build_monster_enroll_stream.
    if ENROLL_MONSTERS_AT_SPAWN:
        w.write_byte(0x35)
        w.write_uint16(behavior_id)
        w.write_byte(0x64); w.write_byte(0x01); w.write_byte(0x02)
        w.write_uint32(hp_wire)

    w.write_byte(0x06)                              # EndStream

    if hasattr(server, "combat") and server.combat is not None:
        # Authored weapon Range drives the server-AI chase stop (effective
        # attack range) — melee 8, rifles/bows 90+, so ranged mobs stand off.
        weapon_ranges = [e.weapon_range for e in entries
                         if e.kind != creature_manipulators.KIND_SKILL
                         and e.weapon_range > 0.0]
        server.combat.register_monster(
            entity_id=entity_id, gc_type=md.gc_type,
            label=md.label or md.gc_type, hp_wire=hp_wire,
            level=level, difficulty=md.difficulty, zone_gc_type=zone_gc_type,
            pos_x=pos_x, pos_y=pos_y, pos_z=pos_z,
            treasure_generators=md.treasure_generators,
            behavior_id=behavior_id,
            attack_range=max(weapon_ranges, default=8.0),
            defense_rating=md.defense_rating,
            instance_id=instance_id,
            entity_gc_type=entity_gc_type,
        )

    return entity_id, w.to_array()


def _difficulty_levels(difficulty: str) -> tuple[int, int]:
    """Return (min_level, max_level) for a difficulty tier."""
    levels = {
        "GRUNT": (1, 5), "FODDER": (1, 3), "RECRUIT": (3, 8),
        "VETERAN": (5, 15), "WARMONGER": (10, 25),
        "CHAMPION": (15, 35), "HERO": (25, 50), "BOSS": (30, 70),
        "DUNGEON_BOSS": (40, 100),
    }
    return levels.get(difficulty.upper(), (1, 5))
