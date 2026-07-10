"""Server-driven monster combat AI — Follow-action aggro + server-stepped chase.

OPT-IN since 2026-06-13 (``DR_MONSTER_AI=1``; default OFF = deferred-enroll
model). Every packet this module streams asserts replay-tracked monster HP in
its ``0x02`` EntitySynchInfo trailer, and the mob's server-fed Follow action
carries NO input-authority bit — so an UNPATCHED client runs the
zero-tolerance HP compare (FUN_005dd900) on every one of them and fatally
crashes on the first replay divergence (live-captured against the C# server
2026-06-12: client 60.0 vs server 55.0 — bible.md §6-LIVE). Until the server's
combat math is bit-exact (bible.md §6a), this model is only safe on a client
patched in the synch-compare path. The default model instead defers the
``0x64`` enroll burst to the player's first attack (net/movement.py 0x50 →
GameServer.enroll_instance_monsters): the client brain owns the mobs and the
server never asserts mob HP afterward, so the compare never runs.

Architecture (DRS-NET port, wire shapes client-verified 2026-06-11):

With this model the client mob brain is never enrolled (no ``0x64``
client-control handoff — the old enroll model made the client chase the
avatar's CENTER with no range gate, the "run-through" bug). Instead:

* **Aggro** (proximity or the player attacking the mob) installs a **Follow
  action** on the mob's behavior: ``0x35 <bhv:u16> 0x04 0x16 0x00
  <target:u16>``. Client-verified via Ghidra: sub-op ``0x04`` (CreateAction,
  ``Behavior::processUpdate`` FUN_00515620 case 3) reads the action class id
  byte; class id ``0x16`` is registered as **"Follow"** (extends UnitAction,
  class obj DAT_0093095c); ``Follow::readData`` (FUN_005227a0, vtbl+0xbc) reads
  exactly 1 mode byte (→ +0x6d, ctor default 0) + u16 target entity id.
  Actions have NO readState — one stream-consuming call only.

* **Chase is server-stepped**: this module advances the mob toward its target
  at tick rate and STOPS at the effective attack range (weapon range + mob
  collision radius + avatar combat radius) so mobs stand off correctly instead
  of overlapping the player. Position corrections stream as MoverUpdate
  ``0x65`` packets (``0x00 0x01 0x03 <heading:i32> <destX:i32> <destY:i32>``,
  dest = the STOP-RING point ×256 — the client mover walks all the way to
  dest, so the dest itself must never be inside the avatar; raw player-center
  dests lunged mobs through the player in melee, live 2026-07-02) throttled to
  one per :data:`MOVE_SEND_INTERVAL` per mob — byte-identical to the
  live-proven OP8 mover block in our own spawn stream and to DRS-NET
  BuildMonsterMovePacket (which sent the raw target position).

* **Follow is RE-ASSERTED, not fire-once**: the client locally displaces the
  Follow action (the ``0xF0`` swing action replaces it; the player's landed
  hits fire OnDamaged), leaving the no-range-gate attack approach driving the
  mob into the avatar. Follow is re-sent (throttled to
  :data:`FOLLOW_REASSERT_INTERVAL`) on every player 0x50 against the mob
  (:func:`aggro_from_attack`) and between swings while in contact.

* Every behavior component update carries the mandatory EntitySynchInfo
  trailer ``0x02 <hpWire:u32>`` with the mob's replay-tracked HP
  (``TrackedMonster.current_hp`` — the same source the live-proven enroll
  burst used). If live testing hits a monster HP-synch crash mid-fight, the
  fallback lever is a ``0x00`` flags-only trailer (no HP asserted, no
  zero-tolerance compare) — UNVERIFIED, verify before relying on it.

* **Packets are UNFRAMED and ride the per-connection INTERVAL queue**
  (``conn.interval_message_queue``), drained into the per-4th-tick ``0x0D``
  WorldInterval frame so the steady-state entity-channel message rate stays at
  EXACTLY one message per 133 ms. GHIDRA-PROVEN (FUN_005d9e30, the client
  world-clock pump): the client consumes ONE entity-channel message per 4
  world ticks (both the 33 ms timestep and the 4-tick message cadence are
  programmed by our own 0x0D payload's ``0x21``/``0x03`` fields), and when
  MORE than 2 messages are backed up it multiplies its world-clock advance by
  3 to catch up. LIVE 2026-06-12: streaming mob moves as extra messages
  (immediate frames first, then per-tick queue flushes) pushed production to
  ~7-30 msgs/s vs the 7.5/s consumption contract → permanent 3× catch-up =
  2-3× mob speed, boosted player attack cadence, rubber-band movement; towns
  (no mobs, no extra messages) unaffected. Any future sustained packet stream
  must share the interval frame the same way.

Mob attacks (swing visuals + damage) are the next slice — DRS-NET
OnMonsterAttackStarted/Resolved (UseTarget 0x50 / AttackTarget2 0xF0 action
creates). Until then an in-range mob holds position without swinging.

Player→monster damage is unaffected: the client rolls it locally and the
kill replay (combat.py ROUTE 2B) mirrors it server-side.
"""
from __future__ import annotations

import math
import os
import time
from typing import TYPE_CHECKING, Dict, Optional

from ..core import log
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection
    from .combat import TrackedMonster
    from .world_instance import WorldInstanceRegistry, ZoneInstance

# Combat-model switch. OFF (default) = the live-proven deferred-enroll model
# (movement 0x50 → enroll burst, client brain owns mobs — survives the
# UNPATCHED client's synch compare). DR_MONSTER_AI=1 = this server-driven
# Follow+chase model (fixes run-through, but asserts mob HP the unpatched
# compare checks — patched-client debugging only; see module docstring).
MONSTER_AI_ENABLED = os.environ.get("DR_MONSTER_AI") == "1"

# Server-driven mob→player damage injection (docs/MOB_ATTACK_INJECTION.md).
# OFF by default; requires DR_MONSTER_AI=1 too (the swing logic lives inside the
# server-chase tick). When ON, an in-range mob emits MOB_ATTACK over the
# telemetry channel on its weapon cadence and keeps streaming a throttled synch
# while in contact so the client hook has a game-thread drain point to apply the
# damage. Lets a display-only (un-enrolled) mob hurt the player → no run-through.
MOB_ATTACK_INJECT_ENABLED = os.environ.get("DR_MOB_ATTACK_INJECT") == "1"

# Client-side mob-position clamp (run-through fix — bible §14.6). When ON, the
# server streams OP_MOB_CLAMP for each aggroed mob and the client hook pins it to
# the stop-ring around the avatar every frame (rewriting the mob's world position
# unit+0x90/+0x94), so the client-executed Follow action can't drive it through
# the player. Server-driven mob movement (0x65/Follow) is COSMETIC for these mobs
# — the client's local Follow owns it — so this is the only lever that reaches it.
# Needs the rebuilt client hook (client_hook/d3d9.dll — self-validating: a wrong
# offset degrades to a logged no-op). OFF by default; requires DR_MONSTER_AI=1 too.
MOB_CLAMP_ENABLED = os.environ.get("DR_MOB_CLAMP") == "1"

# Seconds between OP_MOB_CLAMP refreshes per aggroed mob. Must stay well under the
# hook's CLAMP_FRESH_MS (500 ms) freshness window so a live mob never ages out
# mid-fight, but sparse enough to stay off the telemetry socket's hot path.
CLAMP_SEND_INTERVAL = 0.2

# ── Enroll + clamp model (native mob behaviour + client-side stop-ring) ──
# The alternative to the server-driven injection model. Here the mob's OWN client
# AI runs it (enrolled via the deferred 0x64 burst — the DEFAULT model, so
# DR_MONSTER_AI stays OFF), giving native attacks, animations and hit-recovery;
# the server only streams OP_MOB_CLAMP so the client brain can't run the mob
# through the player (bible §14.6). No injection, no 0xF0 swing, no Follow/chase —
# the client does all mob behaviour, we only constrain position. This fixes the
# injection model's "swing animation cancelled by the player's hit-reaction, with
# no brain to resume it" (live 2026-07-03): a native mob recovers its own attack.
# Requires the rebuilt client hook (clamp + forget). Single flag; needs the hook's
# +0xE5 synch bypass (client-simulated mob HP) which the shim already installs.
MOB_ENROLL_CLAMP_ENABLED = os.environ.get("DR_MOB_ENROLL_CLAMP") == "1"

# ── Pure-native model = THE DEFAULT (no enroll, no clamp, no injection) ──────
# BREAKTHROUGH 2026-07-08, LIVE-CONFIRMED (bible §14.6 round 6n): the client's OWN
# monster brain already proximity-aggros, chases to melee (~5u) and attacks with
# animation CORRECTLY, with no server help — the "mobs spawn passive, need waking"
# premise behind the whole enroll/inject/clamp program was WRONG (mobs only *look*
# passive out of aggro range). The deferred 0x64 enroll burst REPLACED that correct
# native behaviour with a FollowClient chase that runs to the avatar's centre (the
# run-through the old enroll docstring admitted as a "cost"); the clamp then pinned
# the mob at its 16u ring (too far for melee → foot-slide, no attack anim). Live
# before/after capture + the user's shift-click(no enroll)=correct vs
# click-attack(enroll)=broken A/B test isolated the enroll as the breaker, and
# removing it (this is now the DEFAULT) fixed combat live 2026-07-08.
#
# So the 0x50 attack path does NOTHING to the mobs by default — the client brain
# owns aggro/chase/attack/recovery. Safe: the server never asserts monster HP
# (broadcast_monster_hp is unwired) and kills arrive via the client telemetry hook
# (notify_client_kill), so an un-enrolled-but-client-simulated mob needs no
# simulated_by marking to avoid a synch crash. The client hook is still needed for
# its +0xE5 synch bypass + kill telemetry (NOT for enroll/clamp).
#
# LEGACY_ENROLL_ENABLED (DR_LEGACY_ENROLL=1) restores the old deferred-0x64 enroll
# on first attack — kept only as an opt-in escape hatch for patched-client debugging
# (it reintroduces the run-through). Default OFF. The DR_MONSTER_AI / DR_MOB_* flags
# above are likewise retired-but-retained opt-in debugging models.
LEGACY_ENROLL_ENABLED = os.environ.get("DR_LEGACY_ENROLL") == "1"

# Seconds between mob swings (authored weapon cadence varies; a tunable default
# until per-weapon cadence is threaded — magnitude/timing are live-tuned).
MOB_ATTACK_INTERVAL = 1.5

# Aggro radius (world units) — authored creatures/base/behavior/Melee.gc
# AgroRange. Ranged pulls beyond this radius aggro via aggro_from_attack().
AGGRO_RADIUS = 100.0
_AGGRO_RADIUS_SQ = AGGRO_RADIUS * AGGRO_RADIUS

# Chase speed (units/s) — authored creatures/base/StockUnit.gc Speed.
MOVE_SPEED = 40.0

# Effective-range stop components (DRS-NET ResolveMonsterEffectiveAttackRange =
# AttackRange + monster CollisionRadius + avatar combat radius). Collision
# radius is authored StockUnit CollisionRadius; avatar radius is authored
# avatar.base.avatar CollisionRadius (DRS-NET default 3).
MOB_COLLISION_RADIUS = 5.0
AVATAR_COMBAT_RADIUS = 3.0
CONTACT_EPSILON = 1.0 / 16.0  # DRS-NET CLIENT_CONTACT_RANGE_EPSILON

# Seconds between 0x65 position corrections per mob (DRS-NET
# MONSTER_MOVE_SEND_INTERVAL).
MOVE_SEND_INTERVAL = 0.15

# Seconds between Follow re-asserts per mob. The client locally DISPLACES the
# mob's Follow action — the 0xF0 swing action replaces it (CreateAction swaps
# the active action) and the player's landed hits fire the mob's OnDamaged
# reaction — leaving the no-range-gate attack approach (§14.6) driving the mob
# into the avatar ("runs through me as soon as I attack", live 2026-07-02).
# Follow was only ever sent on target CHANGE, so the displacement was permanent;
# re-asserting it restores the live-proven stand-at-range action state.
FOLLOW_REASSERT_INTERVAL = 1.0

# Don't re-assert Follow inside this window after a swing — an immediate
# CreateAction would replace the 0xF0 before its swing animation plays.
SWING_FOLLOW_RESTORE_DELAY = 0.4

# Largest dt one chase step may integrate (event-loop hiccup guard).
_MAX_STEP_DT = 0.2

# HP wire floor: at/below this the monster is dead/dying — no aggro, no chase.
_HP_WIRE_FLOOR = 256

# Follow action class id — client Action::Registry, registered name "Follow"
# (Ghidra-verified: registration thunk 0x007e9ae0 pushes id 0x16 + class obj
# DAT_0093095c built by FUN_00526b30 with name string "Follow").
FOLLOW_ACTION_CLASS_ID = 0x16

# AttackTarget2 action class — a mob's basic weapon swing. DRS-NET
# BuildMonsterAttackPacket emits CreateAction (0x04) with class 0xF0 for a normal
# weapon attack (0x50 UseTarget is the active-skill path, + a manipulator-id
# useFlags byte). v1 sends the basic 0xF0 swing so the mob plays its attack
# animation; the DAMAGE is the client-hook injection (OP_MOB_ATTACK → Damage::apply).
ATTACK_ACTION_CLASS_ID = 0xF0

# Per-instance chase-step timestamps. tick() is invoked once per spawned
# connection per server tick; the first call each tick steps the instance and
# this guard makes the others no-ops (asyncio is single-threaded).
_instance_step_at: Dict[tuple, float] = {}


def build_monster_follow_packet(behavior_id: int, target_entity_id: int,
                                hp_wire: int) -> bytes:
    """Serialize the Follow action that aggros a mob onto ``target_entity_id``.

      ``0x35`` <behaviorId:u16> ``0x04`` ``0x16`` ``0x00``
      <targetEntityId:u16> ``0x02`` <hpWire:u32>

    UNFRAMED (no 0x07/0x06) — enqueued on the per-connection message queue and
    flushed inside the tick's single frame (see module docstring). The ``0x00``
    is Follow's readData mode byte (FUN_005227a0 → +0x6d; ctor default 0 —
    DRS-NET sends 0). Port of C# BuildMonsterFollowPacket (which is likewise
    frameless: it is always MessageQueue-enqueued).
    """
    w = LEWriter()
    w.write_byte(0x35)                          # ComponentUpdate
    w.write_uint16(behavior_id & 0xFFFF)
    w.write_byte(0x04)                          # CreateAction
    w.write_byte(FOLLOW_ACTION_CLASS_ID)        # "Follow" action class
    w.write_byte(0x00)                          # Follow mode byte (+0x6d)
    w.write_uint16(target_entity_id & 0xFFFF)
    w.write_byte(0x02)                          # EntitySynchInfo: HP present
    w.write_uint32(hp_wire & 0xFFFFFFFF)
    return w.to_array()


def build_monster_attack_packet(behavior_id: int, target_entity_id: int,
                                hp_wire: int) -> bytes:
    """Serialize the attack action that makes a mob play its swing animation.

      ``0x35`` <behaviorId:u16> ``0x04`` ``0xF0`` ``0x00``
      <targetEntityId:u16> ``0x02`` <hpWire:u32>

    UNFRAMED (rides the interval queue, like Follow/Move). Port of DRS-NET
    ``BuildMonsterAttackPacket`` (the non-skill ``0xF0`` AttackTarget2 path;
    ``0x04`` CreateAction, mode byte ``0x00``). This drives the VISUAL swing
    only — a display-only mob does NOT self-apply damage from it (the client is
    packet-blind for mob→player damage, bible §6), so there is no double damage:
    the damage is the client-hook injection (``OP_MOB_ATTACK`` → ``Damage::apply``).
    """
    w = LEWriter()
    w.write_byte(0x35)                          # ComponentUpdate
    w.write_uint16(behavior_id & 0xFFFF)
    w.write_byte(0x04)                          # CreateAction
    w.write_byte(ATTACK_ACTION_CLASS_ID)        # AttackTarget2 (basic weapon swing)
    w.write_byte(0x00)                          # mode byte
    w.write_uint16(target_entity_id & 0xFFFF)
    w.write_byte(0x02)                          # EntitySynchInfo: HP present
    w.write_uint32(hp_wire & 0xFFFFFFFF)
    return w.to_array()


def build_monster_move_packet(behavior_id: int, dest_x: float, dest_y: float,
                              heading_wire: int, hp_wire: int) -> bytes:
    """Serialize one chase position correction (MoverUpdate ``0x65``).

      ``0x35`` <behaviorId:u16> ``0x65`` ``0x00`` ``0x01`` ``0x03``
      <heading:i32> <destX*256:i32> <destY*256:i32> ``0x02`` <hpWire:u32>

    UNFRAMED (no 0x07/0x06) — message-queue ride, see module docstring (C#
    passes ``beginEndStream=false`` for exactly this). The sub-message body is
    byte-identical to the live-proven OP8 mover block in the monster spawn
    stream (monsters.py) and C# BuildMonsterMovePacket. ``dest`` is where the
    client mover walks the mob TO — the chase stop-ring point (or the mob's
    own spot for the in-contact pin), never the raw player position: the
    mover has no range gate, so a player-center dest ends inside the avatar.
    """
    w = LEWriter()
    w.write_byte(0x35)
    w.write_uint16(behavior_id & 0xFFFF)
    w.write_byte(0x65)
    w.write_byte(0x00); w.write_byte(0x01); w.write_byte(0x03)
    w.write_int32(int(heading_wire))
    w.write_int32(int(dest_x * 256.0))
    w.write_int32(int(dest_y * 256.0))
    w.write_byte(0x02)
    w.write_uint32(hp_wire & 0xFFFFFFFF)
    return w.to_array()


def heading_wire_toward(from_x: float, from_y: float,
                        to_x: float, to_y: float) -> int:
    """Wire heading toward a point: ``atan2`` degrees ×256 (DRS-NET formula)."""
    return int(math.degrees(math.atan2(to_y - from_y, to_x - from_x)) * 256.0)


def effective_attack_range(mon: "TrackedMonster") -> float:
    """Where the chase stops: weapon range + mob radius + avatar radius."""
    attack_range = getattr(mon, "attack_range", 8.0) or 8.0
    return attack_range + MOB_COLLISION_RADIUS + AVATAR_COMBAT_RADIUS


def mob_swing_damage_wire(mon: "TrackedMonster") -> int:
    """Per-swing mob→player damage (HP ×256, wire) for the injection.

    v1 uses the grounded ``Tables.gc`` MonsterDamage curve at the mob's level
    (``combat.monster_curves.interp_damage`` returns Fixed32). Deterministic, no
    variance — magnitude is a live-tuning knob (docs/MOB_ATTACK_INJECTION.md).
    """
    from ..combat.monster_curves import MonsterCurves
    level = max(1, int(getattr(mon, "level", 1) or 1))
    return max(256, int(MonsterCurves.interp_damage(level)))   # floor 1.0 HP


def _maybe_swing(server: "GameServer", registry: "WorldInstanceRegistry",
                 inst: "ZoneInstance", mon: "TrackedMonster",
                 target_conn: "RRConnection", now: float) -> None:
    """Fire a mob swing on cadence:

    1. broadcast the **attack action** (``0xF0`` AttackTarget2) so the mob plays
       its swing ANIMATION on every client displaying it, and
    2. push ``MOB_ATTACK`` to the target's hook so the client applies the DAMAGE
       locally (Damage::apply) — the client is packet-blind for a display mob's
       damage, so the action animates only and there is no double damage.

    Both are intent only — the owning client animates and the hook applies."""
    if now - mon.last_attack_time < MOB_ATTACK_INTERVAL:
        return
    telem = getattr(server, "telemetry", None)
    if telem is None or not hasattr(telem, "send_mob_attack"):
        return

    # Damage first (telemetry → client-hook injection). If no hook is connected
    # the swing is a no-op — don't phantom-animate a swing that deals nothing.
    damage_wire = mob_swing_damage_wire(mon)
    if not telem.send_mob_attack(target_conn, mon.entity_id, damage_wire, element=0):
        return
    mon.last_attack_time = now

    # Swing animation (entity-channel action; rides the interval frame) so the
    # mob visibly swings on every client displaying it. The damage is the hook
    # injection above (display mobs don't self-apply → no double damage).
    _enqueue_to_instance(server, registry, inst,
                         build_monster_attack_packet(
                             mon.behavior_id, _player_target_id(target_conn),
                             mon.current_hp))
    log.info(f"[MONSTER-AI] SWING eid={mon.entity_id} '{mon.label}' -> "
             f"'{target_conn.login_name}' dmg_wire={damage_wire}")


def _maybe_send_clamp(server: "GameServer", mon: "TrackedMonster",
                      target_conn: "RRConnection", now: float) -> None:
    """Stream OP_MOB_CLAMP for an aggroed mob (throttled) so the client hook pins
    it to the stop-ring around the avatar — the run-through fix (bible §14.6).

    No-op unless a clamp model is on (:data:`MOB_CLAMP_ENABLED` for the injection
    model, :data:`MOB_ENROLL_CLAMP_ENABLED` for the enroll model); the ring is the
    same ``effective_attack_range`` the server chase stops at, in Fixed32 (×256).
    """
    if not (MOB_CLAMP_ENABLED or MOB_ENROLL_CLAMP_ENABLED):
        return
    if now - mon.last_clamp_sent < CLAMP_SEND_INTERVAL:
        return
    telem = getattr(server, "telemetry", None)
    if telem is None or not hasattr(telem, "send_mob_clamp"):
        return
    ring_wire = int(effective_attack_range(mon) * 256.0)
    first = mon.last_clamp_sent == 0.0
    if telem.send_mob_clamp(target_conn, mon.entity_id, ring_wire):
        mon.last_clamp_sent = now
        if first:
            log.info(f"[CLAMP] first send eid={mon.entity_id} '{mon.label}' "
                     f"ring={ring_wire} -> '{target_conn.login_name}'")
    elif first:
        log.warn(f"[CLAMP] send FAILED eid={mon.entity_id} — no hook connected?")


def _player_target_id(conn: "RRConnection") -> int:
    """The entity id a mob follows/attacks — the player's avatar (the
    damageable unit), falling back to the unit-behavior id."""
    avatar = getattr(conn, "avatar", None)
    avatar_id = getattr(avatar, "id", 0) if avatar is not None else 0
    return avatar_id or conn.unit_behavior_id


def _is_dead(mon: "TrackedMonster") -> bool:
    return mon.pending_kill or mon.current_hp <= _HP_WIRE_FLOOR


def _enqueue_to_instance(server: "GameServer", registry: "WorldInstanceRegistry",
                         inst: "ZoneInstance", packet: bytes) -> None:
    """Queue an unframed behavior update for every spawned member of ``inst``.

    Goes on the INTERVAL queue: each member's tick loop writes it inside that
    player's next per-4th-tick ``0x0D`` WorldInterval frame — NEVER as an
    immediate standalone frame and NEVER via the per-tick ``message_queue``
    flush. Both raise the entity-channel message rate above the client's
    one-message-per-133ms consumption contract and trip its 3× world-clock
    catch-up (FUN_005d9e30; see module docstring).
    """
    for conn in server.connections.values():
        if not conn.is_spawned:
            continue
        if registry.key_for(conn) != inst.key:
            continue
        conn.interval_message_queue.enqueue(packet)


def _conn_for_target(server: "GameServer", registry: "WorldInstanceRegistry",
                     inst: "ZoneInstance", target_id: int) -> Optional["RRConnection"]:
    """The spawned connection in ``inst`` whose avatar/behavior id is
    ``target_id`` — or None (left zone / despawned / disconnected)."""
    for conn in server.connections.values():
        if not conn.is_spawned:
            continue
        if registry.key_for(conn) != inst.key:
            continue
        if _player_target_id(conn) == target_id or conn.unit_behavior_id == target_id:
            return conn
    return None


def _nearest_player(server: "GameServer", inst: "ZoneInstance",
                    mon: "TrackedMonster",
                    registry: "WorldInstanceRegistry") -> Optional["RRConnection"]:
    """Closest spawned player in ``inst`` within aggro radius of ``mon``."""
    best: Optional["RRConnection"] = None
    best_sq = _AGGRO_RADIUS_SQ
    for other in server.connections.values():
        if not other.is_spawned:
            continue
        if registry.key_for(other) != inst.key:
            continue
        dx = other.player_pos_x - mon.pos_x
        dy = other.player_pos_y - mon.pos_y
        dist_sq = dx * dx + dy * dy
        if dist_sq <= best_sq:
            best_sq = dist_sq
            best = other
    return best


def _send_follow(server: "GameServer", registry: "WorldInstanceRegistry",
                 inst: "ZoneInstance", mon: "TrackedMonster",
                 target_id: int, now: float) -> None:
    """Broadcast the mob's Follow action (first aggro OR re-assert) and stamp
    the :data:`FOLLOW_REASSERT_INTERVAL` throttle anchor."""
    mon.last_follow_sent = now
    _enqueue_to_instance(server, registry, inst,
                         build_monster_follow_packet(mon.behavior_id, target_id,
                                                     mon.current_hp))


def _aggro(server: "GameServer", registry: "WorldInstanceRegistry",
           inst: "ZoneInstance", mon: "TrackedMonster",
           target_conn: "RRConnection", reason: str, now: float) -> None:
    """Aggro a mob onto a player: broadcast the Follow action and lock the
    target. Re-aggro onto the SAME player re-asserts Follow (throttled): the
    player's landed hits fire the mob's local OnDamaged reaction, which can
    displace the Follow action client-side and leave the no-range-gate attack
    approach running (run-through, live 2026-07-02) — and this path fires on
    every player 0x50, i.e. exactly when the displacement happens. Switching
    targets re-sends Follow immediately."""
    target_id = _player_target_id(target_conn)
    if mon.target_id == target_id:
        # Re-assert Follow (throttled) ONLY when the clamp is off. With the clamp
        # on, the hook pins the mob at range, so Follow is no longer needed to
        # prevent run-through — and re-sending it here (every player 0x50) replaces
        # the mob's 0xF0 swing action client-side, cancelling the swing ANIMATION
        # while the damage keeps flowing ("mobs stop attacking by animation but
        # damage still appears", live 2026-07-03). Let the swing play uninterrupted.
        if not MOB_CLAMP_ENABLED and now - mon.last_follow_sent >= FOLLOW_REASSERT_INTERVAL:
            _send_follow(server, registry, inst, mon, target_id, now)
        return
    mon.target_id = target_id
    _send_follow(server, registry, inst, mon, target_id, now)
    log.info(f"[MONSTER-AI] FOLLOW eid={mon.entity_id} '{mon.label}' -> "
             f"'{target_conn.login_name}' bid={mon.behavior_id} reason={reason}")


def aggro_from_attack(server: "GameServer", conn: "RRConnection",
                      target_entity_id: int,
                      now: Optional[float] = None) -> None:
    """The player attacked ``target_entity_id`` — aggro that mob onto them
    (or re-assert its Follow action when already aggroed, throttled).

    Covers ranged/spell pulls from beyond :data:`AGGRO_RADIUS` (DRS-NET
    ``MonsterBehavior2::onAttacked``-equivalent entry point; called from the
    ch7 0x50 UseTarget path in net/movement.py — the slot the old 0x64 enroll
    burst occupied).
    """
    if not MONSTER_AI_ENABLED:
        return
    combat = getattr(server, "combat", None)
    registry = getattr(server, "world_instances", None)
    if combat is None or registry is None:
        return
    mon = combat.get_monster(target_entity_id)
    if mon is None or _is_dead(mon) or not mon.behavior_id:
        return
    inst = registry._instances.get(registry.key_for(conn))
    if inst is None or mon.entity_id not in inst.monster_ids:
        return
    if now is None:
        now = time.monotonic()
    _aggro(server, registry, inst, mon, conn, "attacked", now)


def purge_monster(server: "GameServer",
                  registry: "Optional[WorldInstanceRegistry]",
                  entity_id: int, behavior_id: int) -> None:
    """Drop a dead mob from instance tracking and from any client's queued
    interval (chase/follow) packets BEFORE its ``0x05`` destroy is sent.

    The **Code-9 "Invalid ComponentID" race** (``DR_MONSTER_AI``): a ``0x65``
    chase or ``0x35`` Follow update for the mob's behavior component can still
    sit in a connection's ``interval_message_queue`` when the kill fires. If it
    drains AFTER the destroy, the client gets a component update for a component
    it just destroyed → "zone communication error code 9". Removing the mob
    from ``monster_ids`` stops :func:`tick_instance` queuing new ones; purging
    the already-queued ones closes the window.

    Safe in BOTH combat models: ``monster_ids`` stays accurate either way, and
    in the deferred-enroll default (no server chase stream) the queue purge is a
    no-op — nothing in the interval queue references ``behavior_id``.
    """
    if registry is None:
        return
    inst = None
    for candidate in registry._instances.values():
        if entity_id in candidate.monster_ids:
            inst = candidate
            break
    if inst is None:
        return
    inst.monster_ids.remove(entity_id)
    if not behavior_id:
        return

    # Queued chase/follow packets are unframed and begin
    # ``0x35 <behaviorId:u16> …`` (build_monster_follow_packet /
    # build_monster_move_packet). Match on that 3-byte prefix.
    prefix = b"\x35" + (behavior_id & 0xFFFF).to_bytes(2, "little")
    removed = 0
    for conn in server.connections.values():
        if not conn.is_spawned:
            continue
        if registry.key_for(conn) != inst.key:
            continue
        removed += conn.interval_message_queue.remove_where(
            lambda pkt: pkt[:3] == prefix)
    if removed:
        log.info(f"[MONSTER-AI] purged {removed} queued chase packet(s) for "
                 f"dead eid={entity_id} bid={behavior_id}")


def tick(server: "GameServer", registry: "WorldInstanceRegistry",
         conn: "RRConnection", now: float) -> None:
    """Back-compat entry point: resolve ``conn``'s instance and drive its AI.

    The authoritative driver is now the per-instance tick loop
    (:meth:`WorldInstanceRegistry._instance_tick_loop`), which calls
    :func:`tick_instance` once per live instance regardless of how many players
    are present. This shim keeps older per-connection callers / tests working by
    resolving ``conn``'s instance and delegating.
    """
    if not MONSTER_AI_ENABLED:
        return
    inst = registry._instances.get(registry.key_for(conn))
    if inst is None:
        return
    tick_instance(server, registry, inst, now)


def _simulator_conn(server: "GameServer", registry: "WorldInstanceRegistry",
                    inst: "ZoneInstance",
                    sim: "set") -> Optional["RRConnection"]:
    """A spawned connection in ``inst`` that SIMULATES this mob (its conn_id is in
    the mob's ``simulated_by`` — i.e. it enrolled the mob into its client AI). The
    clamp pins the mob to that player's avatar. Returns the first match, or None."""
    for conn in server.connections.values():
        if not conn.is_spawned:
            continue
        if getattr(conn, "conn_id", None) not in sim:
            continue
        if registry.key_for(conn) != inst.key:
            continue
        return conn
    return None


def _tick_enroll_clamp(server: "GameServer", registry: "WorldInstanceRegistry",
                       inst: "ZoneInstance", now: float) -> None:
    """Enroll + clamp model: the client brain runs the mob (native attacks /
    animation / hit-recovery); the server only streams the stop-ring clamp so the
    brain can't drive it through the player (bible §14.6).

    Only ENROLLED mobs (``simulated_by`` non-empty — the client is driving them)
    are clamped, to the simulator's avatar. No Follow / chase / injection / 0xF0:
    the mob's own AI owns everything but position, which the hook constrains. The
    actively-fought mob is kept cached in the hook by the combat detour (every
    player hit re-caches it), so the clamp resolves it without an extra synch
    stream.
    """
    combat = getattr(server, "combat", None)
    if combat is None or inst is None or not inst.monster_ids:
        return

    key = inst.key
    if now - _instance_step_at.get(key, 0.0) < 0.02:      # one step per tick
        return
    _instance_step_at[key] = now

    for mid in list(inst.monster_ids):
        mon = combat.get_monster(mid)
        if mon is None or not mon.behavior_id or _is_dead(mon):
            continue
        sim = getattr(mon, "simulated_by", None)
        if not sim:
            continue                    # not enrolled yet — client isn't driving it
        target_conn = _simulator_conn(server, registry, inst, sim)
        if target_conn is None:
            continue
        _maybe_send_clamp(server, mon, target_conn, now)


def tick_instance(server: "GameServer", registry: "WorldInstanceRegistry",
                  inst: "ZoneInstance", now: float) -> None:
    """Advance monster combat AI for ONE zone instance — player-independent.

    Aggro scan → Follow; then step every aggroed mob toward its target,
    stopping at effective attack range; stream throttled 0x65 corrections
    while moving. Combat damage stays CLIENT-authoritative — this only drives
    intent (the monster's approach/swing); the owning client animates and
    self-applies the damage.

    The ``_instance_step_at`` dt-guard is retained as a safety net so accidental
    double-calls in the same ~33 ms window (e.g. the back-compat :func:`tick`
    shim firing from several connections) collapse to a single step.
    """
    # Enroll + clamp model (DR_MOB_ENROLL_CLAMP, DR_MONSTER_AI off): the client
    # brain owns mob behaviour; the server only streams the stop-ring clamp.
    if MOB_ENROLL_CLAMP_ENABLED and not MONSTER_AI_ENABLED:
        _tick_enroll_clamp(server, registry, inst, now)
        return
    if not MONSTER_AI_ENABLED:
        return
    combat = getattr(server, "combat", None)
    if combat is None:
        return
    if inst is None or not inst.monster_ids:
        return

    key = inst.key
    last = _instance_step_at.get(key, 0.0)
    dt = now - last
    if dt < 0.02:                       # already stepped this tick — collapse
        return
    _instance_step_at[key] = now
    dt = min(dt, _MAX_STEP_DT)

    for mid in inst.monster_ids:
        mon = combat.get_monster(mid)
        if mon is None or not mon.behavior_id:
            continue
        if _is_dead(mon):
            mon.target_id = 0
            continue

        # ── Acquire ──
        if not mon.target_id:
            target = _nearest_player(server, inst, mon, registry)
            if target is None:
                continue
            _aggro(server, registry, inst, mon, target, "proximity", now)

        # ── Validate target ──
        target_conn = _conn_for_target(server, registry, inst, mon.target_id)
        if target_conn is None:
            mon.target_id = 0           # target left — stand down, can re-aggro
            continue

        # ── Client-side stop-ring clamp (run-through fix, bible §14.6) ──
        # Server chase packets are cosmetic (the client Follow owns movement), so
        # stream the clamp intent that lets the hook pin this mob at range.
        _maybe_send_clamp(server, mon, target_conn, now)

        # ── Step the chase, stop at effective range ──
        tx = target_conn.player_pos_x
        ty = target_conn.player_pos_y
        dx = tx - mon.pos_x
        dy = ty - mon.pos_y
        dist = math.hypot(dx, dy)
        stop_at = effective_attack_range(mon) + CONTACT_EPSILON
        if dist <= stop_at:
            # Contact: hold position. With injection ON, fire swings on cadence
            # and keep streaming a throttled synch toward the player (dest = the
            # mob's OWN spot, so it doesn't drift) — that synch is the
            # game-thread drain point the client hook uses to apply the queued
            # mob-attack damage (docs/MOB_ATTACK_INJECTION.md). With injection
            # OFF this branch is unchanged (just holds).
            if MOB_ATTACK_INJECT_ENABLED:
                _maybe_swing(server, registry, inst, mon, target_conn, now)
                # Restore Follow between swings ONLY when the clamp is off. The
                # 0xF0 swing replaces the mob's Follow action client-side, so
                # WITHOUT the clamp the mob was left running AttackTarget2's
                # no-range-gate approach into the avatar (run-through). WITH the
                # clamp the hook pins the mob at range, so re-asserting Follow is
                # both unnecessary AND harmful — the Follow CreateAction cancels
                # the in-flight swing animation (live 2026-07-03). So skip it and
                # let the swing play.
                if (not MOB_CLAMP_ENABLED and mon.target_id
                        and now - mon.last_attack_time >= SWING_FOLLOW_RESTORE_DELAY
                        and now - mon.last_follow_sent >= FOLLOW_REASSERT_INTERVAL):
                    _send_follow(server, registry, inst, mon, mon.target_id, now)
                if now - mon.last_move_sent >= MOVE_SEND_INTERVAL:
                    mon.last_move_sent = now
                    _enqueue_to_instance(
                        server, registry, inst,
                        build_monster_move_packet(
                            mon.behavior_id, mon.pos_x, mon.pos_y,
                            heading_wire_toward(mon.pos_x, mon.pos_y, tx, ty),
                            mon.current_hp))
            continue

        step = min(MOVE_SPEED * dt, dist - effective_attack_range(mon))
        if step > 0.0 and dist > 0.0:
            mon.pos_x += dx / dist * step
            mon.pos_y += dy / dist * step

        # ── Throttled position correction toward the target ──
        if now - mon.last_move_sent < MOVE_SEND_INTERVAL:
            continue
        mon.last_move_sent = now
        # Dest = the STOP-RING point on the mob's approach side, NOT the raw
        # player position. The client mover walks the mob ALL THE WAY to dest
        # at authored speed — the stop-at-range only exists in the server's
        # own stepping — so a dest of the player's center lunges the mob INTO
        # the avatar whenever the player dances across the ring in melee
        # (~11u covered per 0.15s correction vs a ~16u ring = the "runs
        # through me as soon as I attack" bug, live 2026-07-02). Distant
        # approaches never showed it because contact flips to own-spot pins
        # before the mob nears the center.
        ring = effective_attack_range(mon)
        packet = build_monster_move_packet(
            mon.behavior_id, tx - dx / dist * ring, ty - dy / dist * ring,
            heading_wire_toward(mon.pos_x, mon.pos_y, tx, ty),
            mon.current_hp)
        _enqueue_to_instance(server, registry, inst, packet)
