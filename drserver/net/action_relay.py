"""Relay a player's action onto other clients' copies of that player's avatar.

**The bug (live user report 2026-07-09):** P1 shift-attacking in town, casting a
skill, or swinging at a mob was INVISIBLE to P2. The server acked the *actor*
(the ``0x01`` ActionResponse on the actor's own component) and told no one else.

**The model (bible §15.4 — original-dev testimony):** the native server
"replicated the accepted input down to all clients, including the source." A
combat action IS input. The downstream shape that makes a *displayed* unit visibly
act is **CreateAction** (sub-op ``0x04``) — the same mechanism the client accepts
for mob swings (DRS-NET ``BuildMonsterAttackPacket``: ``0x04 0x50 0x00 <useFlags>
<target>``) and for our own self-cast relay. We convert the actor's inbound
ActionResponse into a CreateAction on each viewer's **remapped** behavior id
(``server.remote_behavior_ids[viewer][actor]``, populated by the spawn exchange).

Wire (FRAMED — its own ``0x07 … 0x06`` stream, sent straight to the viewer)::

    0x07 0x35 <viewerBehaviorId:u16> 0x04 <actionClass> <body…> 0x00 0x06

* ``actionClass`` = the inbound action-type byte (``0x50`` UseTarget /
  ``0x51`` UsePosition / ``0x52`` SelfCast) — 1:1 with the ack path.
* ``body`` — the action payload with the **mode byte normalized to 0x00** (the
  actor's rolling session id has no meaning on the viewer; DRS-NET ships ``0x00``
  there on every server-originated action). Callers assemble it.
* trailing ``0x00`` — the **empty synch** (no HP). A viewer *displays* the actor's
  avatar (control mode 1), so the server must NOT assert the actor's HP — an
  HP-bearing trailer is the monster-swing crash class (bible §4/§6). Proven
  remote-avatar trailer, same as the movement / equipment relays.

**Delivery = framed-direct, NOT the interval queue.** The relay must stay *in
order* with the movement relay (``_broadcast_player_movement``, also framed-direct):
a targeted ``0x50`` roots the display avatar into a UseTarget approach, and the
avatar only leaves that pose when the following move stream reaches it. Batching
the action onto the ``0x0D`` interval queue delivered it *late* — after the moves
— so it re-rooted the avatar and froze it on the viewer's screen (live regression
2026-07-09: "movement no longer synced after a skill cast / basic attack"). The
matching un-root is :func:`relay_cancel_action`, fired from ``_handle_client_move``
on the first move after an action. Action cadence (~2–4/s held) is far under the
movement relay's own framed-direct rate, so this does not threaten the §2 budget.

Byte shapes are **[T1]** (CreateAction is the T0 mechanism for a displayed unit to
act — proven for mobs); the per-class body is ``# UNVERIFIED`` against a live P2
capture. Kill-switch: ``DR_ACTION_RELAY=0``.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ..core import log
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .connection import RRConnection
    from .game_server import GameServer


def _enabled() -> bool:
    return os.environ.get("DR_ACTION_RELAY") != "0"


def _viewers(server: "GameServer", actor_conn: "RRConnection"):
    """Yield ``(viewer_conn, remapped_behavior_id)`` for every spawned player who
    shares the actor's ``(zone_gc_type, instance_id)`` and has a behavior mapping
    for the actor (the spawn exchange populated ``remote_behavior_ids``)."""
    connections = getattr(server, "connections", None)
    if not connections:
        return
    remote_behavior_ids = getattr(server, "remote_behavior_ids", None) or {}
    actor_login = getattr(actor_conn, "login_name", None)
    if not actor_login:
        return
    for other in list(connections.values()):
        if other is actor_conn or not getattr(other, "is_spawned", False):
            continue
        if other.current_zone_gc_type != actor_conn.current_zone_gc_type:
            continue
        if other.instance_id != actor_conn.instance_id:
            continue
        viewer_map = remote_behavior_ids.get(other.login_name)
        if not viewer_map or actor_login not in viewer_map:
            continue
        yield other, viewer_map[actor_login]


def relay_player_action(server: "GameServer", actor_conn: "RRConnection",
                        action_class: int, body: bytes) -> int:
    """Fan the actor's CreateAction to same-instance viewers (framed-direct).

    ``body`` is the CreateAction payload AFTER the class byte — for UseTarget it
    is ``[mode=0x00][useFlags][targetEid:u16]``; the caller normalizes the mode
    byte. Marks the actor as having a viewer-visible action pending so the next
    move un-roots it (:func:`relay_cancel_action`). Returns the viewer count.
    """
    if not _enabled():
        return 0
    sent = 0
    for viewer, behavior_id in _viewers(server, actor_conn):
        w = LEWriter()
        w.write_byte(0x07)                       # BeginStream
        w.write_byte(0x35)
        w.write_uint16(behavior_id & 0xFFFF)
        w.write_byte(0x04)                       # CreateAction
        w.write_byte(action_class & 0xFF)
        w.write_bytes(body)
        w.write_byte(0x00)                       # empty synch — no actor HP
        w.write_byte(0x06)                       # EndStream
        viewer.send_to_client(w.to_array())
        sent += 1
    if sent:
        actor_conn.viewer_action_pending = True
        log.debug(f"[ACTION-RELAY] '{actor_conn.login_name}' class=0x{action_class:02X} "
                  f"-> {sent} viewer(s)")
    return sent


def relay_cancel_action(server: "GameServer", actor_conn: "RRConnection") -> int:
    """Relay a CancelAction (sub-op ``0x03``) so viewers' copies leave the actor's
    in-flight action (attack/cast approach) and resume following its movement.

    Fired both on an explicit client CancelAction and on the first move after a
    relayed action (``_handle_client_move``) — a targeted ``0x50`` roots the
    display avatar in an approach that no viewer-side brain ends, so without this
    the avatar freezes mid-swing on the viewer's screen. Clears the pending flag.

    Wire: ``0x07 0x35 <viewerBehaviorId:u16> 0x03 0x00 0x00 0x06`` (sessionId 0,
    empty synch). Returns the number of viewers relayed to.
    """
    actor_conn.viewer_action_pending = False
    if not _enabled():
        return 0
    sent = 0
    for viewer, behavior_id in _viewers(server, actor_conn):
        w = LEWriter()
        w.write_byte(0x07)                       # BeginStream
        w.write_byte(0x35)
        w.write_uint16(behavior_id & 0xFFFF)
        w.write_byte(0x03)                       # CancelAction
        w.write_byte(0x00)                       # sessionId (viewer-agnostic)
        w.write_byte(0x00)                       # empty synch
        w.write_byte(0x06)                       # EndStream
        viewer.send_to_client(w.to_array())
        sent += 1
    return sent
