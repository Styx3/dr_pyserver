"""Movement + tick — faithful port of the C# entity-channel movement path.

Incoming player movement arrives on the ClientEntity channel (7) as a
BeginStream(0x07) wrapping a component update (0x35) whose sub-message 0x65 is
the UnitMover update. We parse it, update the connection's authoritative
position, and relay it to other players in the same zone/instance.

The tick loop is a faithful port of C# ``SendTickUpdates`` (the proven-smooth
reference). The cadence is the whole point — it is NOT a per-tick flood:

  * **No per-tick send.** Most ticks the loop only advances server-side state
    (combat replay, monster AI). C# sends the owner NOTHING on those ticks.
  * **Every 4th tick (~132 ms): one ``0x0D`` WorldInterval packet.** This is the
    client's movement / world-pacing watchdog feed — the thing the old "needs an
    HP heartbeat" theory actually needed. Stripping the tick to just ``0x0C``
    once froze movement because it dropped the watchdog, not the HP.
  * **Piggybacked on that 0x0D: the pending local move-ack.** The owner's OWN
    movement is echoed back as ``0x35 ub 0x65 <session> <count> <VERBATIM raw
    records>`` + the owner EntitySynchInfo trailer. Echoing the client's exact
    records with the SAME sessionId lets the client dedupe against its local
    prediction — no rubber-band. (The old code synthesized a single record from
    the latest ``conn.player_pos_*`` at 30 Hz, which lagged prediction by
    RTT+≤33 ms and snapped the avatar back. That was the jitter source.)
  * **Multiplayer relay**: on each client move we relay to other players in the
    zone (rate-limited to one per tick interval), plus a fallback every 15th tick
    that replays the last-known position so viewers see pathfind arrival / stops.

There is **no standalone per-tick ``0x36`` HP heartbeat** — C# never sends one.
Avatar HP rides the move-ack and event acks. **Regime-B posture (default since
2026-06-15, bible.md §6 / §6-LIVE.8):** the avatar's own client simulates its HP,
so the server never *originates* an avatar-HP trailer in combat zones — the
combat acks, control toggle, skill ack, and move-ack are dropped there
(:func:`suppress_originated_avatar_hp`); only the client's own self-report is
adopted-then-echoed. The escape hatch ``DR_AVATAR_HP_ORIGINATE=1`` restores the
legacy "ship originated HP everywhere" behavior (safe only on a client patched by
``scripts/patch_client_synch_crash.py``, for A/B testing).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core import log
from ..util.byte_io import LEReader, LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection

TICK_INTERVAL = 0.033        # seconds between ticks (C# uses 33 ms = ~30 Hz)
# Only apply TCP backpressure once asyncio's transport buffer is genuinely
# backed up. C# fires tick packets and never blocks; awaiting drain() *every*
# tick adds a variable suspension before each sleep, so the real tick period
# becomes (drain_time + 33 ms) and drifts under load — which the client renders
# as movement that grows choppy after a while and snaps back smooth when you
# stop (buffer drains, echo pauses). Draining only when the buffer exceeds this
# high-water mark keeps a steady 30 Hz cadence while still bounding memory.
_DRAIN_HIGH_WATER = 256 * 1024

# A single UnitMover record on the wire: [moveType:u8][heading:i32][x:i32][y:i32].
_MOVE_RECORD_SIZE = 13

# Local move-ack hold-off (C# QueueLocalPlayerMovementAck: Time.time + 0.008f).
# Lets a burst of moves coalesce before the next 0x0D tick flushes the ack.
_MOVE_ACK_HOLDOFF = 0.008

# Owner move-ack flush cadence and multiplayer fallback-relay cadence, in ticks
# (C# SendTickUpdates: ``tickCount % 4`` for the WorldInterval + ack, ``% 15``
# for the fallback position broadcast).
_WORLD_INTERVAL_EVERY = 4
_FALLBACK_RELAY_EVERY = 15

# Component-update sub-opcodes that carry a [u16 cid][byte sub] component header.
_COMPONENT_OPCODES = (0x32, 0x34, 0x35)

# ── Owner avatar-HP suppression — TRIED & DISPROVEN LIVE (2026-06-03) ─────────────
# Hypothesis (track 1): the owner is authoritative for its own avatar HP, so the
# server could stop echoing it — write the EntitySynchInfo trailer as bare
# ``flags=0x00`` (no HP) on everything sent to the owner about its OWN avatar (the
# 0x36 heartbeat, the 0x35/0x65 position echo, the action acks) and the synch
# compare would have no [Remote] HP to mismatch [Local].
#
# LIVE RESULT: crashed at WARP, BEFORE any damage. The client's compare
# (FUN_005dd900) is NOT field-gated by the trailer flags — it compares the whole
# EntitySynchInfo including Flags. ``flags=0x00`` produced ``[Remote] Flags=0,
# HP=0`` against a healthy ``[Local] Flags=2, HP=72192`` → fatal mismatch. (Same
# wall C# hit: "send HP=0 → mismatches a healthy entity at max HP → popup.")
#
# ⇒ There is NO no-op trailer: a 0x35 component update to the owner MUST carry
# ``flags=0x02`` + an HP that equals the client's current local HP. At warp that's
# full==full (matches); the ONLY failure is the post-damage race (heartbeat ships
# the pre-report value in the ~33ms gap). That race is unwinnable by clamping, so
# the authority bit (track 2) is the real fix. Keep this False; the constant +
# helper stay only to centralize the trailer and record why suppression fails.
_SUPPRESS_OWNER_AVATAR_HP = False


def _write_owner_synch_trailer(w: LEWriter, hp_wire: int) -> None:
    """Append the EntitySynchInfo trailer the server sends to a client about ITS
    OWN avatar (the 0x36 heartbeat, the position echo, and action acks).

    Writes the legacy ``[flags=0x02][hp:u32]``. ``_SUPPRESS_OWNER_AVATAR_HP`` would
    write a bare ``flags=0x00`` instead, but that is DISPROVEN (see the constant) —
    the client compares the Flags field, so a zero-flag trailer crashes a healthy
    avatar at warp. The trailer must carry flags=0x02 + HP.
    """
    if _SUPPRESS_OWNER_AVATAR_HP:
        w.write_byte(0x00)
    else:
        w.write_byte(0x02)
        w.write_uint32(hp_wire)


def _read_trailing_avatar_hp(data: bytes) -> "int | None":
    """Extract the client's self-reported avatar HP from a ch7 packet's trailing
    EntitySynchInfo. Port of C# ``UnityGameServer.TryReadTrailingEntitySynchHP``.

    The client appends an EntitySynchInfo trailer — ``[flags]`` then, when
    ``flags & 0x02``, a 4-byte little-endian HP — just before the ``0x06``
    EndStream on its ROUTINE packets (movement ``0x65``, action acks), far more
    often than it emits a standalone ``0x36`` sub-message. Scan from the end:
    skip trailing NUL padding, require the ``0x06`` terminator, back up 5 bytes
    to the flags, and read the HP when the synch bit is set. Returns ``None`` when
    no HP trailer is present.
    """
    if not data or len(data) < 6:
        return None
    i = len(data) - 1
    while i > 0 and data[i] == 0x00:
        i -= 1
    if data[i] != 0x06:
        return None
    flags_off = i - 5
    if flags_off < 0 or (data[flags_off] & 0x02) == 0:
        return None
    return (data[flags_off + 1]
            | (data[flags_off + 2] << 8)
            | (data[flags_off + 3] << 16)
            | (data[flags_off + 4] << 24))


def _heartbeat_hp(conn: "RRConnection") -> int:
    """The HP to ship in the per-tick ``0x36`` heartbeat — never above the last
    value the client self-reported.

    Belt-and-suspenders on the HP race behind the avatar synch crash. On zone
    entry ``_refresh_avatar_hp_wire(reset_client_hp=True)`` sets ``conn.hp_wire``
    to the level MAX and clears ``client_hp_wire``; the 30 Hz heartbeat can then
    outrun the client's first damage report and ship ``[Remote]=MAX`` while the
    client has self-simmed lower (live crash 2026-06-02: ``[Local]69978`` vs
    ``[Remote]72192``). Once the client has reported a value, never carry an HP
    above it. Before the first report (``client_hp_wire is None``) the level-max
    wire is correct (the avatar IS at full HP on spawn).
    """
    hp = getattr(conn, "hp_wire", 200 * 256)
    client_hp = getattr(conn, "client_hp_wire", None)
    if client_hp is not None and client_hp < hp:
        return client_hp
    return hp


def suppress_originated_avatar_hp(conn: "RRConnection") -> bool:
    """True ⇒ the server must NOT *originate* an avatar-HP trailer for ``conn``.

    Regime-B posture (bible.md §6 / §6-LIVE.8): the avatar's own client SIMULATES
    its HP, so a *spontaneous server-originated* HP send — one the client did not
    trigger with its own packet — ships a value the client did not itself compute
    and fails the zero-tolerance synch compare (``FUN_005dd900``) the instant a
    mob has damaged the avatar before its next self-report lands (the stale-MAX
    race, caught live 2026-06-15). Today the only such sender left is the ``0x64``
    control-reset toggle (``GameServer.send_client_control_reset``); the per-tick
    ``0x36`` heartbeat was removed from the tick loop outright.

    ALL acks of the client's OWN packets are load-bearing and are NOT gated on
    this: the local move-ack (the player's ``0x65`` echo), the combat action acks
    ``0x50``/``0x51``/``0x52``, CancelAction, and the portal/checkpoint/
    NPC-activate acks. Dropping any of them stalls the client's action state
    machine (live 2026-06-16: frozen movement + CancelAction spam; live
    2026-07-02: self-casts never resolved — no animation, retry spam). They ship
    the clamped :func:`_heartbeat_hp` value; the client's own packets carry a
    fresh self-report that :func:`handle` adopts (``adopt_client_avatar_hp``)
    BEFORE anything echoes it, so adopt-then-echo never races on those.

    Default ON outside town/tutorial (promoted from the old opt-in
    ``DR_NO_HP_HEARTBEAT=1`` on 2026-06-15, the native Regime-B posture). Escape
    hatch: ``DR_AVATAR_HP_ORIGINATE=1`` restores the legacy "ship originated HP
    everywhere" behavior for A/B comparison against a patched client.
    """
    import os
    if os.environ.get("DR_AVATAR_HP_ORIGINATE") == "1":
        return False
    zgc = getattr(conn, "current_zone_gc_type", "") or ""
    return "town" not in zgc and "tutorial" not in zgc


def handle(server: "GameServer", conn: "RRConnection", message_type: int, data: bytes) -> None:
    """HandleClientEntityChannel — dispatch a channel-7 message.

    NB: do NOT add per-packet logging here. This runs on every inbound movement
    packet (~30 Hz); ``log.info``/``log.debug`` write to stdout SYNCHRONOUSLY and
    block the asyncio event loop, which renders as choppy movement that snaps back
    when the backlog drains (same trap as ``connection._TRACE_SEND``). An earlier
    ``[LVLUP-DIAG ch7]`` INFO trace here was the live jitter source — removed.
    """
    # Adopt the client's self-reported avatar HP from the trailing
    # EntitySynchInfo BEFORE dispatch. The client reports its (self-simmed) HP
    # on its routine move/action packets, not just via the standalone 0x36
    # sub-message; without scanning the trailer the server keeps shipping MaxHP
    # in every 0x02 suffix and the avatar synch compare crashes on any damage
    # taken (Local damaged vs Remote MaxHP). C# TryReadTrailingEntitySynchHP.
    combat = getattr(server, "combat", None)
    trailing_hp = None
    if combat is not None:
        trailing_hp = _read_trailing_avatar_hp(data)
        if trailing_hp is not None:
            combat.adopt_client_avatar_hp(conn, trailing_hp,
                                          f"trailing-ch7-0x{message_type:02X}")

    # Re-assert avatar input authority on the first inbound client packets after
    # zone entry (movement 0x65 / hp-sync 0x36 / action acks), when the client's
    # STEADY action is live — NOT during the spawn stream, where the bit lands on
    # the transient spawn action and is discarded on the action swap. No-op once
    # the post-warp burst is exhausted. See GameServer.reassert_control_after_zone_entry.
    reassert = getattr(server, "reassert_control_after_zone_entry", None)
    if reassert is not None:
        reassert(conn, _now())

    reader = LEReader(data)
    if message_type == 0x07:
        _parse_entity_stream(server, conn, reader)
    elif message_type in _COMPONENT_OPCODES:
        _component_update(server, conn, reader)
    elif message_type == 0x04:
        _handle_entity_request(server, conn, reader)
    else:
        log.debug(f"[ENTITY] type=0x{message_type:02X} len={len(data)}")


def _handle_entity_request(server: "GameServer", conn: "RRConnection", reader: LEReader) -> None:
    """HandleEntityRequest (C# UnityGameServer.cs HandleEntityRequest, channel-7 opcode 0x04).

    Wire: ``uint16 entityId`` + ``byte requestType`` + body. A request with <3
    bytes — or an unrecognised requestType — is a respawn (the death / sync-error
    "Respawn" button). The stat requests are ported in ``net.attributes``:
    ``0x11`` SpendAttribPoint, ``0x12`` ReturnAttribPoint, ``0x13`` ReSpec.
    """
    from . import attributes

    if reader.remaining < 3:
        server.request_respawn(conn)
        return
    reader.read_uint16()                      # entityId (unused here)
    request_type = reader.read_byte()
    if request_type == attributes.REQ_SPEND_ATTRIB:
        attributes.handle_stat_spend(server, conn, reader)
    elif request_type == attributes.REQ_RETURN_ATTRIB:
        attributes.handle_stat_return(server, conn, reader)
    elif request_type == attributes.REQ_RESPEC:
        attributes.handle_respec(server, conn)
    else:
        server.request_respawn(conn)


def _parse_entity_stream(server: "GameServer", conn: "RRConnection", reader: LEReader) -> None:
    from ..managers import combat as combat_module

    while reader.remaining > 0:
        sub_type = reader.read_byte()
        if sub_type == 0x06:        # EndStream
            break
        # NB: no per-sub-message logging here — this is on the 30 Hz movement hot
        # path and synchronous stdout blocks the event loop (see handle()).
        if sub_type in _COMPONENT_OPCODES:
            if not _component_update(server, conn, reader):
                break
        elif sub_type == 0x03:      # SendUpdate (HP sync)
            if server.combat:
                combat_module.CombatManager.handle_hp_sync(
                    server.combat, conn, reader, source="SendUpdate-0x03")
        elif sub_type == 0x36:      # EntitySyncHP
            if server.combat:
                combat_module.CombatManager.handle_hp_sync(
                    server.combat, conn, reader, source="HP-SYNC-0x36")
        elif sub_type == 0x08:      # CombatTick
            if server.combat:
                combat_module.CombatManager.handle_combat_tick(server.combat, conn, reader)
        elif sub_type == 0x09:      # Aggro
            if server.combat:
                combat_module.CombatManager.handle_aggro(server.combat, conn, reader)
        elif sub_type == 0x0C:      # RNG seed
            if server.combat:
                combat_module.CombatManager.set_rng_seed(server.combat, reader)
        elif sub_type == 0x64:      # StateMachine (AI state transition)
            break
        else:
            break


def _component_update(server: "GameServer", conn: "RRConnection", reader: LEReader) -> bool:
    """Returns True if the message body was fully consumed.

    Routes to movement, equipment, or inventory handlers based on sub-opcode
    and target component ID.
    """
    if reader.remaining < 3:
        return False
    component_id = reader.read_uint16()
    sub_message = reader.read_byte()

    # ── UnitBehavior: movement ──
    if sub_message == 0x65 and component_id < 50000:
        _handle_client_move(server, conn, reader, component_id)
        return True

    # ── UnitBehavior: action dispatch (combat / NPC click) ──
    if sub_message == 0x01 and component_id < 50000:
        # QuestManager dialog confirm: the live client's Accept / Complete
        # button is submsg 0x01 with an EMPTY payload on the QM component
        # (C# UGS:12012 "0x01 empty = ACCEPT!") — it must be intercepted
        # before the action read below, which needs >= 4 bytes and would
        # otherwise swallow the confirm silently.
        qm_id = getattr(conn, "quest_manager_id", 0)
        if qm_id and component_id == qm_id and reader.remaining == 0:
            if server.quests is not None:
                server.quests.handle_component_update(conn, 0x01, reader)
            return True
        if reader.remaining >= 4:
            response_id = reader.read_byte()
            action_type = reader.read_byte()
            # Actions are user-gesture-rate (not the 30 Hz hot path); one line
            # each so live tests show exactly what the client sent (the
            # Shift-cast / targeted-skill wire shapes are still UNVERIFIED).
            log.info(f"[ACTION] '{conn.login_name}' cid={component_id} "
                     f"type=0x{action_type:02X} resp={response_id} "
                     f"remaining={reader.remaining} "
                     f"body={reader._data[reader._pos:reader._pos + 24].hex()}")

            # ── 0x52 SELF-CAST (buffs / AoE) — C# UGS:12067: a self-cast
            # carries only [sessionID][slotID] (≤ 2 bytes left), so it must be
            # intercepted BEFORE the u16 target read below misparses it. The
            # checkpoint-recall 0x52 carries a u16 target + cstring and falls
            # through to the normal read.
            if action_type == 0x52 and reader.remaining <= 2:
                from . import skills as skills_module
                skills_module.handle_self_cast(server, conn, reader,
                                               component_id, response_id)
                return True

            if reader.remaining < 3:
                return False
            sid = reader.read_byte()
            target_eid = reader.read_uint16()

            # Clamp action-ack trailers to the client's last self-report (same
            # rule as the 0x36 heartbeat / control-reset). After zone entry
            # conn.hp_wire is the level MAX; echoing raw MAX in an action ack
            # while the client has self-simmed lower fails the zero-tolerance
            # avatar synch compare. NB: this is hygiene — it does NOT close the
            # first-hit heartbeat race (the authority bit is the real fix).
            hp_wire = _heartbeat_hp(conn)
            # The action ack trailer ships _heartbeat_hp = the client's last
            # self-reported HP, clamped (never above what the client reported).
            #
            # These action acks (0x50/0x51/0x52) are RESPONSES to the client's own
            # action packet and are LOAD-BEARING: without the ack the client's
            # attack/cast action never resolves and the avatar locks up — it then
            # spams CancelAction and can neither move nor attack (live 2026-06-16).
            # They are NOT the "spontaneous server-originated avatar HP" the
            # Regime-B posture (bible.md §6-LIVE.8) suppresses — that is the
            # periodic 0x36 heartbeat, the monster 0x65 corrections, and the 0x64
            # control toggle, which the client never triggers. The bible's own rule
            # is to "echo the client's freshest value ON THE CLIENT'S OWN PACKETS
            # (move/action), never send a spontaneous trailer". So these acks are
            # sent unconditionally. (Earlier they were dropped in combat zones,
            # over-applying the posture and freezing every click-to-attack — the
            # same bug class as the dropped move-ack.)
            #
            # DELIVERY (live 2026-07-02): combat acks ride the INTERVAL queue —
            # drained inside the per-4th-tick 0x0D frame — NOT the per-tick
            # message_queue flush. The 0x0D cadence alone saturates the client's
            # one-message-per-133ms consumption budget (bible.md §2), so each
            # per-tick-flushed ack is an EXTRA channel-7 message; a held attack /
            # skill button turns that into a sustained stream and the client's
            # >2-backlog catch-up runs the whole world at 3× ("game speeds up
            # while holding attack, even in town", live 2026-07-02).
            # One-shot acks that precede a zone change (portal / checkpoint /
            # teleporter 0x06, checkpoint-recall 0x52) STAY on message_queue:
            # the zone change clears the interval queue (start_tick), and their
            # click-rate is genuinely bursty.

            if action_type == 0x51 and reader.remaining >= 11:
                # BehaviourActionUsePosition (spell / Shift-cast at position).
                # Reads actionID (low byte of target_eid), then 3x int32 pos.
                # C# reads exactly 11 remaining bytes (UGS:12551) — the prior
                # >= 12 guard silently dropped suffix-less casts (no ack).
                action_id = target_eid & 0xFF
                pos_remain = reader.read_bytes(11)
                pos_x = (target_eid >> 8) & 0xFF
                pos_x |= pos_remain[0] << 8 | pos_remain[1] << 16 | pos_remain[2] << 24
                pos_y = pos_remain[3] | (pos_remain[4] << 8) | (pos_remain[5] << 16) | (pos_remain[6] << 24)
                pos_z = pos_remain[7] | (pos_remain[8] << 8) | (pos_remain[9] << 16) | (pos_remain[10] << 24)

                w = LEWriter()
                w.write_byte(0x35)
                w.write_uint16(component_id)
                w.write_byte(0x01); w.write_byte(response_id)
                w.write_byte(0x51); w.write_byte(sid)
                w.write_byte(action_id)
                w.write_uint32(pos_x); w.write_uint32(pos_y); w.write_uint32(pos_z)
                _write_owner_synch_trailer(w, hp_wire)
                conn.interval_message_queue.enqueue(w.to_array())

                # ── MULTIPLAYER: relay the position-cast animation to viewers
                # (mode byte normalized to 0x00; body = actionId + the 3 int32
                # cast coords). See net.action_relay.
                from . import action_relay
                _pos_body = LEWriter()
                _pos_body.write_byte(0x00)               # mode
                _pos_body.write_byte(action_id)
                _pos_body.write_uint32(pos_x)
                _pos_body.write_uint32(pos_y)
                _pos_body.write_uint32(pos_z)
                action_relay.relay_player_action(server, conn, 0x51,
                                                 _pos_body.to_array())

                # Summon position-casts (Monster Bait, TargetType=POSITION):
                # spawn the SpellSpawnEffect unit at the cast position —
                # entity creation is server-owned (managers.summons). The
                # action_id is the casting manipulator/hotbar slot id (same
                # id space as the 0x52 slotID).
                _summons = getattr(server, "summons", None)
                if _summons is not None:
                    _skill = getattr(conn, "skill_manip_map", {}).get(action_id)
                    def _signed(v: int) -> int:
                        return v - 0x100000000 if v >= 0x80000000 else v
                    if _summons.try_cast(conn, _skill,
                                         pos_x=_signed(pos_x) / 256.0,
                                         pos_y=_signed(pos_y) / 256.0,
                                         pos_z=_signed(pos_z) / 256.0):
                        log.info(f"[SPELL-0x51] '{conn.login_name}' summon "
                                 f"position-cast slot={action_id} "
                                 f"skill='{_skill}'")

            elif action_type == 0x50 and reader.remaining >= 1:
                # BehaviourActionUseTarget (targeted attack)
                # Format: after sessionID comes [useFlags][targetId bytes...]
                manipulator_id = sid
                use_flags = target_eid & 0xFF
                target_id_low = (target_eid >> 8) & 0xFF
                target_id_high = reader.read_byte()
                actual_target_id = target_id_low | (target_id_high << 8)

                # ── Bling Gnome use-target: the skill auto-targets the owner's
                # gnome (TargetType=HENCHMAN), so this 0x50 opens the convert
                # window, NOT an attack swing (DRS-NET isBlingGnomeTarget
                # branch — same ack echo, then ActivateGnome). ──
                _gnome = getattr(server, "gnome", None)
                if _gnome is not None and _gnome.is_gnome_target(conn, actual_target_id):
                    w = LEWriter()
                    w.write_byte(0x35)
                    w.write_uint16(component_id)
                    w.write_byte(0x01); w.write_byte(response_id)
                    w.write_byte(0x50); w.write_byte(manipulator_id)
                    w.write_byte(use_flags); w.write_uint16(actual_target_id)
                    _write_owner_synch_trailer(w, hp_wire)
                    conn.interval_message_queue.enqueue(w.to_array())
                    activated = _gnome.activate(conn, actual_target_id)
                    log.info(f"[GNOME-ACTIVATE] '{conn.login_name}' use-target "
                             f"0x{actual_target_id:04X} activated={activated}")
                    return True

                # ROUTE 2B: the client is packet-proven blind (no monster-HP /
                # kill report), so feed this swing into the server-side replay
                # tracker. It advances on the per-tick loop (combat.tick_combat)
                # and, on a replayed kill, raises conn.hp_wire via the existing
                # death pipeline — the per-tick 0x36 heartbeat then carries the
                # leveled value. Resolution is asynchronous to this ack (the hit
                # lands a cadence-tick later), so the ack itself just echoes.
                _combat = getattr(server, "combat", None)
                if _combat is not None:
                    _combat.register_swing(conn, actual_target_id, _now())

                w = LEWriter()
                w.write_byte(0x35)
                w.write_uint16(component_id)
                w.write_byte(0x01); w.write_byte(response_id)
                w.write_byte(0x50); w.write_byte(manipulator_id)
                w.write_byte(use_flags); w.write_uint16(actual_target_id)
                _write_owner_synch_trailer(w, hp_wire)
                conn.interval_message_queue.enqueue(w.to_array())

                # ── MULTIPLAYER: make the swing visible on other players'
                # screens. The ack above only reaches the actor; without this a
                # shift-attack in town or a swing at a mob was invisible to the
                # rest of the party (live 2026-07-09). Relay a CreateAction
                # (0x50) on each viewer's remapped avatar behavior — the mode
                # byte is normalized to 0x00 (the actor's session id is
                # meaningless to a viewer). See net.action_relay.
                from . import action_relay
                action_relay.relay_player_action(
                    server, conn, 0x50,
                    bytes([0x00, use_flags]) + actual_target_id.to_bytes(2, "little"))

                # Re-assert the client's avatar control authority (throttled) so
                # the type-2 HP synch compare on its own avatar stays bypassed
                # through a local level-up — otherwise the stale conn.hp_wire
                # fatally crashes the client (see GameServer.send_client_control_reset).
                server.reassert_control_on_action(conn, _now())

                # ── MULTIPLAYER: make P1's mob fight visible on P2's screen.
                # In the native-mob model every client's brain simulates its own
                # copy and aggros only on LOCAL proximity, so a mob chasing P1
                # stays frozen at spawn on P2's screen (live 2026-07-09). Tell
                # the non-engaged instance members' copies to Follow the
                # attacker's avatar. HP-safe (max HP to a displaying copy) and
                # instance-scoped inside the relay. See mob_engagement_relay.
                from ..managers import mob_engagement_relay
                mob_engagement_relay.on_player_attack(
                    server, conn, actual_target_id, _now())

                # Combat-model fork (monster_ai flags):
                #
                # * native (DEFAULT): do NOTHING to the mobs — the client's own
                #   monster brain already proximity-aggros, chases to melee and
                #   attacks with animation correctly (bible §14.6 round 6n,
                #   LIVE-CONFIRMED 2026-07-08: the enroll 0x64 was what BROKE that
                #   native behaviour into a run-to-center chase). No 0x64, no
                #   clamp, no injection — the client does all mob behaviour, the
                #   server never asserts mob HP, and kills arrive via the client
                #   telemetry hook.
                #
                # * legacy enroll (DR_LEGACY_ENROLL=1): the first attack sends the
                #   deferred 0x64 burst (superseded — reintroduces the run-through;
                #   opt-in escape hatch for patched-client debugging only).
                #
                # * follow (DR_MONSTER_AI=1): server-driven Follow + stepped
                #   chase (managers/monster_ai.py, DRS-NET port). Fixes
                #   run-through, but its packets assert replay-tracked mob HP,
                #   which only a client patched in FUN_005dd900 survives today
                #   (bible.md §6-LIVE).
                from ..managers import monster_ai
                if monster_ai.MONSTER_AI_ENABLED:
                    monster_ai.aggro_from_attack(server, conn, actual_target_id,
                                                 _now())
                elif monster_ai.LEGACY_ENROLL_ENABLED:
                    enroll = getattr(server, "enroll_instance_monsters", None)
                    if enroll is not None:
                        enroll(conn)
                # else (default): native — client brain owns the mobs, nothing to do

            elif action_type == 0x52:
                # BehaviourActionUse — checkpoint-menu recall. After the u16 target
                # comes a NUL-terminated checkpoint GC type. Ack then teleport to
                # the destination's exact position (port of C# HandleCheckpointUse).
                from ..managers.checkpoints import checkpoint_manager
                gc_type = reader.read_cstring() if reader.remaining > 0 else ""
                conn.session_id = sid
                w = LEWriter()
                w.write_byte(0x35)
                w.write_uint16(component_id)
                w.write_byte(0x01); w.write_byte(response_id)
                w.write_byte(0x52); w.write_byte(sid)
                _write_owner_synch_trailer(w, hp_wire)
                conn.message_queue.enqueue(w.to_array())
                dest = checkpoint_manager.find_destination(gc_type)
                if dest is not None:
                    log.info(f"[CHECKPOINT-USE] '{conn.login_name}' recall -> {dest.zone}")
                    server.change_zone_to_position(conn, dest.zone,
                                                   dest.pos_x, dest.pos_y, dest.pos_z)
                else:
                    log.warn(f"[CHECKPOINT-USE] unknown checkpoint '{gc_type}'")
                    conn.send_system_message("That waystone destination is unavailable.")

            elif action_type != 0x06:
                # Unhandled action type — log only, send NOTHING (C# UGS:12650).
                # The previous code fell into the 0x06 branch below and acked an
                # Activate the client never sent, which can derail its action
                # state machine.
                log.warn(f"[ACTION] '{conn.login_name}' UNHANDLED "
                         f"type=0x{action_type:02X} sid={sid} "
                         f"target={target_eid} remaining={reader.remaining}")

            else:
                # action_type 0x06 (BehaviourActionActivate — NPC click / general activate)
                # C# format: [responseId][0x06][sessionID][u16 targetID][0x02][hp_wire]
                if action_type == 0x06:
                    # Dropped ground item FIRST (C# checks IsDroppedItem before
                    # portals/chests/checkpoints) — every click on a tracked
                    # drop auto-bags it via the right-click pickup port.
                    from ..managers import loot as loot_manager
                    if loot_manager.find_drop(target_eid) is not None:
                        from . import inventory as inv_module
                        if inv_module.handle_ground_pickup(
                                server, conn, component_id, target_eid,
                                response_id, sid):
                            return True

                    # Portal (teleport gate) takes priority — activating it both
                    # acks the action and transfers the player. Port of C#
                    # HandlePortalActivation.
                    from ..managers.portals import portal_manager
                    portal = portal_manager.find_by_entity_id(target_eid)
                    if portal is not None:
                        conn.session_id = sid
                        w = LEWriter()
                        w.write_byte(0x35)
                        w.write_uint16(component_id)
                        w.write_byte(0x01); w.write_byte(response_id)
                        w.write_byte(0x06); w.write_byte(sid)
                        w.write_uint16(target_eid)
                        _write_owner_synch_trailer(w, hp_wire)
                        conn.message_queue.enqueue(w.to_array())
                        log.info(f"[PORTAL] '{conn.login_name}' activated gate "
                                 f"-> {portal.target_zone}@{portal.spawn_point}")
                        # Walking through a portal sets the "Recent Zone Portal"
                        # saved place (C# HandlePortalActivation:16309) — the
                        # only transfer kind that may touch it.
                        conn.zone_portal_source = getattr(
                            conn, "current_zone_name", "") or ""
                        server.change_zone(conn, portal.target_zone, portal.spawn_point)
                        return True

                    # Waystone obelisk — activating it acks the action and
                    # *unlocks* the matching recall destination for this character
                    # (port of C# HandleCheckpointActivation). It does NOT teleport;
                    # recall happens later via the obelisk menu (channel 13).
                    from ..managers.checkpoints import checkpoint_manager
                    cp_entity = checkpoint_manager.find_by_entity_id(target_eid)
                    if cp_entity is not None:
                        conn.session_id = sid
                        w = LEWriter()
                        w.write_byte(0x35)
                        w.write_uint16(component_id)
                        w.write_byte(0x01); w.write_byte(response_id)
                        w.write_byte(0x06); w.write_byte(sid)
                        w.write_uint16(target_eid)
                        _write_owner_synch_trailer(w, hp_wire)
                        conn.message_queue.enqueue(w.to_array())
                        dest = checkpoint_manager.find_destination(cp_entity.gc_type)
                        if dest is not None and dest.id not in conn.unlocked_checkpoints:
                            conn.unlocked_checkpoints.add(dest.id)
                            from ..db import character_repository
                            character_repository.add_checkpoint(conn.char_sql_id, dest.id)
                            log.info(f"[CHECKPOINT] '{conn.login_name}' unlocked {dest.id}")
                        return True

                    # Interactive world entity (chest / shrine / gate /
                    # teleporter) — spawned from zone_world_entities and
                    # registered by id at instance populate. The generic 0x06
                    # ack used to swallow these clicks (chest never opened,
                    # shrine did nothing — user report 2026-06-17). Port of C#
                    # GameServer.Combat.cs 0x06 → WorldEntitySpawner.TryGetEntity.
                    from ..managers import world_entities as we_module
                    we = we_module.world_entity_manager.find_by_entity_id(target_eid)
                    if we is not None:
                        conn.session_id = sid

                        def _ack_world_entity() -> None:
                            w = LEWriter()
                            w.write_byte(0x35)
                            w.write_uint16(component_id)
                            w.write_byte(0x01); w.write_byte(response_id)
                            w.write_byte(0x06); w.write_byte(sid)
                            w.write_uint16(target_eid)
                            _write_owner_synch_trailer(w, hp_wire)
                            conn.message_queue.enqueue(w.to_array())

                        if we.entity_type == "chest":
                            _ack_world_entity()
                            we_module.open_chest(server, conn, target_eid, we)
                            return True
                        if we.entity_type == "shrine":
                            _ack_world_entity()
                            we_module.activate_shrine(server, conn, target_eid, we)
                            return True
                        if we.entity_type == "teleporter" and we.target_zone:
                            _ack_world_entity()
                            conn.zone_portal_source = getattr(
                                conn, "current_zone_name", "") or ""
                            server.change_zone(conn, we.target_zone,
                                               we.target_waypoint)
                            log.info(f"[TELEPORTER] '{conn.login_name}' -> "
                                     f"{we.target_zone}@{we.target_waypoint}")
                            return True
                        if we.entity_type == "gate":
                            # Boss exit-gate: opened by the boss's DoorsToOpenOnDeath
                            # on death (world_entities.open_boss_doors). Once open it
                            # is a passable arch — clicking it is a no-op (walk
                            # through), so suppress the "sealed" nag; while the boss
                            # still lives, keep the locked hint.
                            _ack_world_entity()
                            if not we_module.world_entity_manager.is_gate_opened(
                                    target_eid):
                                conn.send_system_message(
                                    "The gate is sealed. Defeat the boss to open it.")
                            return True
                        # Other NCI (portrait etc.): ack + activate visual.
                        _ack_world_entity()
                        conn.send_to_client(
                            we_module._build_nci_activate(target_eid, activated=False))
                        return True

                    from ..managers.npcs import npc_manager
                    npc = npc_manager.find_by_entity_id(target_eid)
                    if npc is not None:
                        conn.current_dialog_npc_id = npc.gc_type
                        log.debug(f"[NPC] clicked '{npc.name}' gc={npc.gc_type}")
                        # Vendor click: arm the per-connection restock push so
                        # the client's empty UnitContainer 0x22 (its restock
                        # countdown expiring) refreshes this shop (C#
                        # TrackPendingMerchantActivation).
                        merchant_cid = server.npc_merchant_cids.get(target_eid)
                        if merchant_cid:
                            from ..managers.merchants import merchant_manager
                            merchant_manager.arm_refresh(conn, npc.gc_type,
                                                         merchant_cid)

                w = LEWriter()
                w.write_byte(0x35)
                w.write_uint16(component_id)
                w.write_byte(0x01); w.write_byte(response_id)
                w.write_byte(0x06); w.write_byte(sid)
                w.write_uint16(target_eid)
                _write_owner_synch_trailer(w, hp_wire)
                conn.message_queue.enqueue(w.to_array())
        return True

    # ── Merchant (vendor) shop: buy (0x1E) / sell (0x1F) on a merchant cid ──
    # The merchant component ids are registered when the zone's NPC stream is
    # built; routing is keyed strictly on that registry so the UnitContainer
    # 0x1E/0x1F (item add/remove) traffic is unaffected.
    if sub_message in (0x1E, 0x1F) and component_id in server.merchant_components:
        from ..managers.merchants import merchant_manager
        if sub_message == 0x1E:
            return merchant_manager.handle_buy(server, conn, component_id, reader)
        return merchant_manager.handle_sell(server, conn, component_id, reader)

    # ── Skill trainer: learn / rank-up purchase on a trainer cid ──
    # Like merchants, trainer component ids are registered at NPC-stream build;
    # any ComponentUpdate on one is a train request (C# routes by TrainerId
    # alone — GameServer.Combat.cs:4380).
    if component_id in getattr(server, "trainer_components", {}):
        from ..managers import trainers
        return trainers.handle_train_request(server, conn, component_id, reader)

    # ── Skills / hotbar: place (0x35), remove (0x36), slot equip (0x39) ──
    # C# routes the hotbar 0x35/0x36 by sub-message alone (UGS:12664); 0x39 is
    # gated on the player's Skills component inside the handler.
    if sub_message in (0x35, 0x36, 0x39) and component_id < 50000:
        from . import skills as skills_module
        if skills_module.handle_skills_component_update(
                server, conn, reader, component_id, sub_message):
            return True

    # ── Equipment component: equip/unequip ──
    if component_id == conn.equipment_component_id:
        from . import equipment
        if sub_message == 0x28:       # AddEquippedItem
            equipment.handle_add_equipped_item(server, conn, reader, component_id)
            return True
        elif sub_message == 0x29:     # RemoveEquippedItem
            equipment.handle_remove_equipped_item(server, conn, reader, component_id)
            return True

    # ── QuestManager: quest dialog accept / turn-in / abandon / cancel ──
    # The client drives the quest handshake via ComponentUpdates on the
    # QuestManager component: 0x02 cancel, 0x03 abandon, 0x04 log-view|turn-in
    # dialog, 0x05 turn-in confirm, 0x06 query|accept, 0x08 NPCTeleporter request
    # (plus the empty-payload 0x01 confirm intercepted in the action branch
    # above). Port of the C# UnityGameServer quest dispatch.
    if component_id == conn.quest_manager_id and sub_message in (0x02, 0x03, 0x04,
                                                                 0x05, 0x06, 0x08):
        if server.quests is not None:
            server.quests.handle_component_update(conn, sub_message, reader)
        return True

    # ── UnitBehavior: cancel current action ──
    # Sub-message 0x03 on a NON-QuestManager component = CancelAction (port of
    # C# HandleCancelAction, UGS:15481). The client sends it when the player
    # moves to break off an in-progress approach (attack on a far target / NPC
    # walk-up). It must be acked — [0x35][cid][0x03][sessionId] + owner synch
    # trailer — or the client's action state machine stays locked in the
    # auto-run and movement input can't cancel it.
    if sub_message == 0x03 and component_id < 50000:
        _handle_cancel_action(server, conn, reader, component_id)
        return True

    # ── QuestManager: town-portal return (0x0A from the obelisk dialog) ──
    # Port of C# UGS:11993 [QUEST-0x0A]: teleport back to the saved town-portal
    # point if one exists; otherwise ignore.
    if sub_message == 0x0A and component_id == conn.quest_manager_id:
        if conn.has_saved_town_portal and conn.town_portal_zone_name:
            log.info(f"[QM-0x0A] '{conn.login_name}' town-portal return -> "
                     f"{conn.town_portal_zone_name}")
            server.change_zone_to_position(
                conn, conn.town_portal_zone_name, conn.town_portal_pos_x,
                conn.town_portal_pos_y, conn.town_portal_pos_z)
        else:
            log.warn(f"[QM-0x0A] '{conn.login_name}' no saved town portal")
        return True

    # ── QuestManager: "Recent Zone Portal" saved place (0x0C) ──
    # Port of C# UGS:13068 — the obelisk dialog's saved-place entry for the
    # zone the player last walked into a portal from.
    if sub_message == 0x0C and component_id == conn.quest_manager_id:
        while reader.remaining > 0:
            reader.read_byte()
        if conn.zone_portal_source:
            log.info(f"[QM-0x0C] '{conn.login_name}' recent-zone-portal return -> "
                     f"{conn.zone_portal_source}")
            server.change_zone(conn, conn.zone_portal_source)
        else:
            log.warn(f"[QM-0x0C] '{conn.login_name}' no zone portal source set")
        return True

    # ── QuestManager: obelisk recall (menu-select goToCheckpoint) ──
    # When the player picks a destination from the obelisk dialog, the client
    # sends a ComponentUpdate on the QuestManager component, sub-message 0x07,
    # carrying a DJB2 hash of the chosen checkpoint GC id (tag 0x04=u32 / 0x02=u16
    # / 0x01=byte). Port of C# UnityGameServer.cs:13014. The cstring ch13/0x07
    # path is a separate/legacy trigger; this is what the live client emits.
    if sub_message == 0x07 and component_id == conn.quest_manager_id:
        _handle_qm_checkpoint_recall(server, conn, reader)
        return True

    # ── UnitContainer: inventory operations ──
    if component_id == conn.unit_container_id:
        from . import inventory as inv_module
        if sub_message in (0x25, 0x26):   # UseItem / UseItemPosition
            inv_module.handle_use_item(server, conn, reader)
            return True
        elif sub_message == 0x28:     # PickupItemFromInventory
            inv_module.handle_pickup_item(server, conn, reader)
            return True
        elif sub_message == 0x29:     # PlaceItemInInventory
            inv_module.handle_place_item(server, conn, reader)
            return True
        elif sub_message == 0x23:     # DropItem
            inv_module.handle_drop_item(server, conn, reader)
            return True
        elif sub_message == 0x22 and reader.remaining == 0:
            # Empty 0x22 = the client's merchant restock countdown expired
            # (C# UGS:12851 → FlushClientMerchantRefreshOnClientBoundary).
            # Regenerate + push removes/adds for the armed vendor, if any.
            from ..managers.merchants import merchant_manager
            merchant_manager.on_container_boundary(conn)
            return True

    return False


def _handle_cancel_action(server: "GameServer", conn: "RRConnection",
                          reader: LEReader, component_id: int) -> None:
    """CancelAction — the client wants to stop its current action (port of C#
    ``HandleCancelAction``, UGS:15481).

    Wire in: ``[0x35][cid:u16][0x03][sessionId:u8]``. Echo
    ``[0x35][cid][0x03][sessionId]`` + the owner synch trailer so the client
    releases the action (stops the auto-approach), then drop any server-side
    pending swing replay for this connection (C# ClearUseTargetAndReleaseControl
    / WeaponCycleTracker target clear) so a cancelled out-of-range attack can't
    keep replaying to a kill.

    The ack rides the INTERVAL queue (same rule as the 0x50/0x51/0x52 combat
    acks — see the action-dispatch comment): the client spams CancelAction at
    frame rate when an action is stuck, and per-tick-flushed acks for that
    stream blow the one-message-per-133ms budget → 3× world catch-up
    (bible.md §2).
    """
    session_id = reader.read_byte() if reader.remaining >= 1 else 0
    log.info(f"[CANCEL-ACTION] '{conn.login_name}' cid={component_id} "
             f"sid=0x{session_id:02X}")

    w = LEWriter()
    w.write_byte(0x35)
    w.write_uint16(component_id)
    w.write_byte(0x03)
    w.write_byte(session_id)
    _write_owner_synch_trailer(w, _heartbeat_hp(conn))
    conn.interval_message_queue.enqueue(w.to_array())

    # ── MULTIPLAYER: stop the actor's animation on viewers' copies too, so a
    # cancelled attack/cast doesn't leave P1 frozen mid-swing on P2's screen. ──
    from . import action_relay
    action_relay.relay_cancel_action(server, conn)

    combat = getattr(server, "combat", None)
    if combat is not None:
        combat.clear_combat(conn.login_name)


def _handle_qm_checkpoint_recall(server: "GameServer", conn: "RRConnection",
                                 reader: LEReader) -> None:
    """Recall from the obelisk menu — resolve the DJB2-hashed checkpoint id the
    client sent and transfer the player. Port of C# (UnityGameServer.cs:13014).

    Body after the sub-message byte: a tag (0x04=u32 / 0x02=u16 / 0x01=byte)
    followed by the hash, then any trailing bytes. The hash is matched against
    DJB2 of each of this character's unlocked checkpoint GC ids.
    """
    from ..data.gc_object import hash_djb2
    from ..managers.checkpoints import checkpoint_manager

    cp_hash = 0
    if reader.remaining >= 1:
        tag = reader.read_byte()
        if tag == 0x04 and reader.remaining >= 4:
            cp_hash = reader.read_uint32()
        elif tag == 0x02 and reader.remaining >= 2:
            cp_hash = reader.read_uint16()
        elif tag == 0x01 and reader.remaining >= 1:
            cp_hash = reader.read_byte()
    # Drain any trailer so the stream stays aligned.
    while reader.remaining > 0:
        reader.read_byte()

    if cp_hash == 0:
        log.warn(f"[CP-RECALL] '{conn.login_name}' empty checkpoint hash")
        return

    matched = next((cp for cp in conn.unlocked_checkpoints
                    if hash_djb2(cp) == cp_hash), None)
    if matched is None:
        log.warn(f"[CP-RECALL] '{conn.login_name}' no unlocked checkpoint for "
                 f"hash 0x{cp_hash:08X}")
        conn.send_system_message("That waystone is not unlocked.")
        return

    dest = checkpoint_manager.find_destination(matched)
    if dest is None:
        log.warn(f"[CP-RECALL] '{conn.login_name}' '{matched}' has no destination row")
        conn.send_system_message("That waystone destination is unavailable.")
        return

    log.info(f"[CP-RECALL] '{conn.login_name}' recall -> {dest.zone} "
             f"(hash 0x{cp_hash:08X} = {matched})")
    server.change_zone(conn, dest.zone)


def _handle_client_move(server: "GameServer", conn: "RRConnection",
                        reader: LEReader, component_id: int) -> None:
    if reader.remaining < 2:
        return
    session_id = reader.read_byte()
    move_count = reader.read_byte()

    raw_start = reader.position
    last_x = last_y = last_heading = 0.0
    for _ in range(move_count):
        if reader.remaining < 13:
            break
        reader.read_byte()                  # move type
        heading = reader.read_int32()
        pos_x = reader.read_int32()
        pos_y = reader.read_int32()
        last_x = pos_x / 256.0
        last_y = pos_y / 256.0
        last_heading = heading / 256.0
    raw_end = reader.position

    if move_count > 0:
        conn.player_pos_x = last_x
        conn.player_pos_y = last_y
        conn.player_heading = last_heading
    conn.session_id = session_id

    raw_move = reader.get_raw_bytes(raw_start, raw_end - raw_start)

    # ── MULTIPLAYER un-root: the first move after a relayed action cancels that
    # action on viewers' copies. A targeted 0x50 roots the display avatar in a
    # UseTarget approach that no viewer-side brain ends, so without this the
    # avatar freezes mid-swing on the viewer and stops following P1's movement
    # (live regression 2026-07-09). Fire BEFORE the move relay so the cancel and
    # the move stay in order on the viewer's stream. ──
    if move_count > 0 and getattr(conn, "viewer_action_pending", False):
        from . import action_relay
        action_relay.relay_cancel_action(server, conn)

    # Echo the owner's OWN movement back on the next 0x0D tick (C#
    # QueueLocalPlayerMovementAck), and relay it to other players now.
    _queue_local_move_ack(conn, session_id, move_count, raw_move)
    _broadcast_player_movement(server, conn, session_id, move_count, raw_move)


def _queue_local_move_ack(conn: "RRConnection", session_id: int,
                          move_count: int, raw_move: bytes) -> None:
    """Stage the owner's own move records for the next 0x0D-tick echo.

    Faithful port of C# ``QueueLocalPlayerMovementAck``: normalize to whole
    13-byte records, coalesce with any already-pending ack for the same session
    (keep the last 255 records), and arm a short hold-off so a burst of moves
    flushes together. The actual echo is written by ``_build_pending_local_move_ack``.
    """
    if move_count == 0 or not raw_move:
        return
    raw_count = min(move_count, len(raw_move) // _MOVE_RECORD_SIZE)
    if raw_count <= 0:
        return
    normalized = raw_move[: raw_count * _MOVE_RECORD_SIZE]

    if (conn.pending_local_move_session == session_id
            and conn.pending_local_move_count > 0
            and conn.pending_local_move_data):
        combined = conn.pending_local_move_data + normalized
        combined_count = len(combined) // _MOVE_RECORD_SIZE
        keep_count = min(255, combined_count)
        keep_bytes = keep_count * _MOVE_RECORD_SIZE
        conn.pending_local_move_data = combined[len(combined) - keep_bytes:]
        conn.pending_local_move_count = keep_count
    else:
        conn.pending_local_move_session = session_id
        conn.pending_local_move_count = min(255, raw_count)
        conn.pending_local_move_data = normalized

    due = _now() + _MOVE_ACK_HOLDOFF
    if conn.pending_local_move_flush_at <= 0.0 or conn.pending_local_move_flush_at > due:
        conn.pending_local_move_flush_at = due


def _build_pending_local_move_ack(conn: "RRConnection") -> "bytes | None":
    """Build the owner's local move-ack sub-message, or None if none is due.

    Port of C# ``TryWritePendingLocalPlayerMovementAck``. The echo is
    ``0x35 <ub:u16> 0x65 <session> <count> <verbatim records>`` followed by the
    owner EntitySynchInfo trailer (flags=0x02 + clamped HP). The verbatim records
    + same sessionId let the client dedupe against its own prediction; the trailer
    rides the avatar HP (safe with the synch-crash patch). Clears the pending ack.
    """
    if not conn.unit_behavior_id:
        return None
    if conn.pending_local_move_count == 0 or not conn.pending_local_move_data:
        return None
    if _now() < conn.pending_local_move_flush_at:
        return None

    session_id = conn.pending_local_move_session
    move_count = conn.pending_local_move_count
    data = conn.pending_local_move_data

    w = LEWriter()
    w.write_byte(0x35)
    w.write_uint16(conn.unit_behavior_id)
    w.write_byte(0x65)
    w.write_byte(session_id & 0xFF)
    w.write_byte(move_count & 0xFF)
    w.write_bytes(data)
    _write_owner_synch_trailer(w, _heartbeat_hp(conn))

    conn.pending_local_move_count = 0
    conn.pending_local_move_data = b""
    conn.pending_local_move_flush_at = 0.0
    return w.to_array()


def _broadcast_player_movement(server: "GameServer", conn: "RRConnection",
                               session_id: int, move_count: int, raw_move: bytes) -> None:
    """Relay a move to other spawned players in the same zone/instance.

    Faithful port of C# ``BroadcastPlayerMovement``: rate-limited to one relay per
    tick interval; while moving it relays the live records and remembers them; on
    a stop (``move_count == 0``, from the fallback tick) it replays the last-known
    records ONCE so viewers see the player halt at the right spot. Uses the
    per-viewer remapped behavior id (remote_behavior_ids[viewer][mover]); no-op
    until foreign-player spawn populates that map.
    """
    now = _now()
    # Rate-limit (C# LastPositionUpdateTime gate) — at most one relay per tick.
    if now - conn.last_position_update_time < TICK_INTERVAL:
        return
    conn.last_position_update_time = now

    if move_count > 0 and raw_move:
        conn.last_raw_move_data = raw_move
        conn.last_raw_move_count = move_count
        conn.stop_signal_sent = False
        relay_count = move_count
        relay_data = raw_move
    else:
        # Player stopped — replay the last-known position exactly once.
        if conn.stop_signal_sent or not conn.last_raw_move_data:
            return
        relay_count = conn.last_raw_move_count or 1
        relay_data = conn.last_raw_move_data
        conn.stop_signal_sent = True

    # Normalize to whole 13-byte records (C# TryNormalizeUnitMoverUpdateData).
    safe_count = min(relay_count, len(relay_data) // _MOVE_RECORD_SIZE)
    if safe_count <= 0:
        return
    relay_data = relay_data[: safe_count * _MOVE_RECORD_SIZE]

    for other in list(server.connections.values()):
        if other is conn or not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue
        viewer_map = server.remote_behavior_ids.get(other.login_name)
        if not viewer_map or conn.login_name not in viewer_map:
            continue
        remote_behavior_id = viewer_map[conn.login_name]

        # Wire format MUST match C# BroadcastPlayerMovement exactly: the relay
        # carries the raw move entries then a single 0x00 (no synch) + EndStream.
        # Appending a 0x02/HP synch block here desyncs the viewer's stream and the
        # client drops the avatar update ("zone communication error").
        w = LEWriter()
        w.write_byte(0x07)
        w.write_byte(0x35)
        w.write_uint16(remote_behavior_id)
        w.write_byte(0x65)
        w.write_byte(0xFF)
        w.write_byte(safe_count)
        w.write_bytes(relay_data)
        w.write_byte(0x00)
        w.write_byte(0x06)
        other.send_to_client(w.to_array())


def build_world_interval_packet(tick_count: int, move_ack: bytes = b"",
                                interval_messages: bytes = b"") -> bytes:
    """Build the every-4th-tick owner packet — faithful port of the C#
    ``SendTickUpdates`` ``tickCount % 4`` block (UGS:24312).

    Layout (one BeginStream/EndStream): ``0x07`` | ``0x0D`` <tickCount:u32>
    <0x21:u32> <0x03:u32> <0x01:u32> <100:u16> <20:u16> | [move_ack] |
    [interval_messages] | ``0x06``.

    The ``0x0D`` WorldInterval is the client's movement / world-pacing feed —
    and it PROGRAMS the pacing (client FUN_005da7d0 → FUN_005d9e30): ``0x21``
    (33) = the world-tick timestep in ms; ``0x03`` = consume one entity-channel
    message every 4 world ticks; the ``0x01 100 20`` tail = PathManager budgets
    (FUN_004c3cc0 "PathManager::ReadBudget"). The client therefore drains
    server messages at EXACTLY 7.5/s and runs its world clock at 3× whenever
    more than 2 messages back up — so every sustained stream must ride INSIDE
    this frame:

    * ``move_ack`` — the owner's pending local move-ack sub-message
      (``_build_pending_local_move_ack``; C# writes it into the same packet).
    * ``interval_messages`` — drained ``conn.interval_message_queue`` bytes
      (monster Follow/Move corrections — managers/monster_ai.py).
    """
    w = LEWriter()
    w.write_byte(0x07)                  # BeginStream
    w.write_byte(0x0D)                  # ClientEntity world interval
    w.write_uint32(tick_count & 0xFFFFFFFF)
    w.write_uint32(0x21)
    w.write_uint32(0x03)
    w.write_uint32(0x01)
    w.write_uint16(100)
    w.write_uint16(20)
    if move_ack:
        w.write_bytes(move_ack)         # owner local move-ack (0x35 … 0x65 …)
    if interval_messages:
        w.write_bytes(interval_messages)
    w.write_byte(0x06)                  # EndStream
    return w.to_array()


def _reset_movement_relay_state(conn: "RRConnection") -> None:
    """Clear per-zone movement-relay state so a new tick loop starts clean.

    Called from start_tick on every spawn / zone entry. Without it, a stale
    pending local move-ack (the PREVIOUS zone's coalesced move records) flushes on
    the first 0x0D tick in the NEW zone and snaps the client back to its pre-warp
    position — and because the pending batch can hold several coalesced records it
    replays as several jumps (the "teleported back 3-5 times on arrival" bug). The
    fallback-relay state (last_raw_move_*/stop_signal) and the broadcast rate-limit
    gate (last_position_update_time) are reset for the same reason.
    """
    conn.pending_local_move_session = 0
    conn.pending_local_move_count = 0
    conn.pending_local_move_data = b""
    conn.pending_local_move_flush_at = 0.0
    conn.last_raw_move_data = b""
    conn.last_raw_move_count = 0
    conn.stop_signal_sent = False
    conn.last_position_update_time = 0.0
    # Stale OLD-zone monster Follow/Move updates must not ride the first
    # interval frame of the NEW zone (their behavior ids no longer exist).
    conn.interval_message_queue.clear()


def start_tick(server: "GameServer", conn: "RRConnection") -> None:
    # Cancel any existing heartbeat first so a re-entry (zone warp) never leaves
    # two concurrent tick loops doubling the stream into the client.
    existing = getattr(conn, "_tick_task", None)
    if existing is not None and not existing.done():
        existing.cancel()
    # Drop any movement-relay state carried over from the previous zone so the
    # first tick here does not echo a stale (pre-warp) position back to the client.
    _reset_movement_relay_state(conn)
    conn._tick_task = asyncio.create_task(_tick_loop(server, conn))


async def _tick_loop(server: "GameServer", conn: "RRConnection") -> None:
    unit_behavior_id = conn.unit_behavior_id
    log.info(f"[TICK] start for '{conn.login_name}' ub={unit_behavior_id}")
    # Regime-B posture (bible.md §6 / §6-LIVE.8, default since 2026-06-15): outside
    # town/tutorial the owner move-ack rides conn.hp_wire (=MAX before the client's
    # first self-report) → its 0x02 trailer would lose the zero-tolerance avatar
    # synch compare on an UNPATCHED client. So the ack is dropped in combat zones
    # and only the non-HP 0x0D WorldInterval is sent. The escape hatch
    # DR_AVATAR_HP_ORIGINATE=1 (suppress_originated_avatar_hp) restores the legacy
    # "ship HP on the move-ack everywhere" behavior for patched-client A/B testing.
    import os as _os
    _tick_count = 0

    # ── Tick-health diagnostic (DR_TICK_HEALTH=1) ──────────────────────────────
    # The client paces its world clock off the 0x0D WorldInterval cadence; if the
    # asyncio loop delivers ticks irregularly (WSL2 timer granularity, per-tick
    # work, GC), the cadence jitters → choppy movement / "fast mobs" even when the
    # wire bytes match C#. This logs the ACTUAL tick timing once per window
    # (~2 s) — never per tick — so we can confirm or rule out loop drift. Expected
    # healthy: ~30 ticks/s, max_gap ≈ 33 ms. A large max_gap = event-loop stall.
    _health = _os.environ.get("DR_TICK_HEALTH") == "1"
    _h_last = _now()
    _h_win_start = _h_last
    _h_ticks = 0
    _h_gap_max = 0.0
    _h_work_max = 0.0
    try:
        while conn.is_connected and conn.is_spawned:
            _iter_start = _now()
            if _health:
                _gap = _iter_start - _h_last
                _h_last = _iter_start
                if _gap > _h_gap_max:
                    _h_gap_max = _gap
                _h_ticks += 1

            # Flush queued messages first (matches C# FlushAllQueues in Update())
            if conn.allow_flush and not conn.message_queue.is_empty():
                _flush_message_queue(conn)

            # ROUTE 2B: advance this connection's weapon-cycle replay one native
            # tick (33ms cadence == NATIVE_UPDATE_TICK) and finalize any kill —
            # raising conn.hp_wire BEFORE the move-ack below carries it out.
            if getattr(server, "combat", None) is not None:
                server.combat.tick_combat(conn, _now())

            # NB: shared, player-independent world logic — monster AI (aggro +
            # chase intent) and the merchant restock watchdog — used to run here,
            # once per connected player and deduped by a timestamp guard. It now
            # runs once per zone instance on its own tick task
            # (managers.world_instance.WorldInstanceRegistry._instance_tick_loop),
            # which fixes the 2+ player double-simulation. This loop keeps only
            # the player-OWNED work below: the combat replay above, the local
            # move-ack, and the viewer relay.
            _tick_count += 1

            # ── Owner packet: every 4th tick (~132 ms), exactly like C#. ──
            # One 0x0D WorldInterval (the movement/world-pacing watchdog feed) with
            # the pending local move-ack piggybacked. NO per-tick 0x36 heartbeat
            # and NO synthesized position echo — those fought the client's local
            # prediction at 30 Hz and produced the jitter / rubber-band.
            if conn.unit_behavior_id and _tick_count % _WORLD_INTERVAL_EVERY == 0:
                # The local move-ack is the player's OWN 0x65 movement echo
                # (verbatim records + sessionId) the client needs to confirm its
                # predicted movement — dropping it freezes the avatar after its
                # prediction window ("runs, then stuck after a couple seconds").
                # It is NOT a server-originated HP send: per bible §6-LIVE.8 the
                # move echo is the SAFE HP path — its trailer ships _heartbeat_hp,
                # = the client's own adopted self-report (handle() adopts the
                # inbound trailer BEFORE this echo, and the echo only fires while
                # the client is actively moving and thus reporting HP ~30 Hz). The
                # one genuinely-originated avatar-HP send left (the 0x64 control
                # toggle) is suppressed at its OWN site — not here; the combat
                # acks 0x50/0x51/0x52 are load-bearing and ride this same 0x0D
                # frame via the interval queue. (Earlier the posture dropped
                # whole acks in combat zones, over-applying Regime-B and breaking
                # dungeon movement / attacks / self-casts.)
                move_ack = _build_pending_local_move_ack(conn)
                # Streaming entity updates (monster Follow/Move) ride INSIDE
                # this frame — the client consumes one entity-channel message
                # per 133 ms and triples its world clock on backlog >2, so the
                # interval frame is the only sustainable carrier.
                _interval_msgs = b"".join(conn.interval_message_queue.dequeue_all())
                conn.send_to_client(build_world_interval_packet(
                    _tick_count, move_ack or b"", _interval_msgs))

            # ── Multiplayer fallback relay: every 15th tick (~500 ms), like C#. ──
            # Replays the last-known position so viewers catch movement the direct
            # relay missed (pathfind arrival, knockback) and see the player halt.
            if _tick_count % _FALLBACK_RELAY_EVERY == 0:
                _broadcast_player_movement(server, conn, conn.session_id, 0, b"")

            # Fire-and-forget like C#; only yield to drain if the socket is
            # actually congested (keeps the tick cadence steady — see note above).
            _buf = 0
            try:
                transport = getattr(conn.writer, "transport", None)
                if transport is not None:
                    _buf = transport.get_write_buffer_size()
                    if _buf > _DRAIN_HIGH_WATER:
                        await conn.writer.drain()
            except Exception:  # noqa: BLE001
                break

            if _health:
                _work = _now() - _iter_start
                if _work > _h_work_max:
                    _h_work_max = _work
                _elapsed = _iter_start - _h_win_start
                if _elapsed >= 2.0:
                    log.info(
                        f"[TICK-HEALTH] '{conn.login_name}' ticks/s="
                        f"{_h_ticks / _elapsed:.1f} max_gap={_h_gap_max * 1000:.0f}ms "
                        f"max_work={_h_work_max * 1000:.1f}ms buf={_buf}B "
                        f"(target 30/s, 33ms)")
                    _h_win_start = _iter_start
                    _h_ticks = 0
                    _h_gap_max = 0.0
                    _h_work_max = 0.0

            await asyncio.sleep(TICK_INTERVAL)
    except asyncio.CancelledError:  # pragma: no cover
        pass
    finally:
        log.debug(f"[TICK] stop for '{conn.login_name}'")


def _flush_message_queue(conn: "RRConnection") -> None:
    """Send all queued messages wrapped in BeginStream/EndStream (matches C# FlushAllQueues)."""
    messages = conn.message_queue.dequeue_all()
    if not messages:
        return
    w = LEWriter()
    w.write_byte(0x07)  # BeginStream
    for msg in messages:
        w.write_bytes(msg)
    w.write_byte(0x06)  # EndStream
    conn.send_to_client(w.to_array())


def _now() -> float:
    import time
    return time.monotonic()
