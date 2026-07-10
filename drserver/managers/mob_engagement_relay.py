"""Make P1's mob fight visible on P2's screen (bible §7 "mob-copy limit").

**The bug (live user report 2026-07-09):** "when mobs aggro P1, P2 doesn't see
them" move. In the native-mob model each client's own brain simulates its private
copy of every mob, and that brain aggros only on **local** proximity — it ignores
avatars it merely *displays* (other players). So when a mob chases P1, P2's copy
keeps idling at its spawn point: the fight is invisible to the rest of the party.

**What the server CAN do.** It cannot stream the owner-copy's exact mob position
(the client sends no monster state upstream — bible §6-LIVE.12), and it must never
assert a mob HP a client is *simulating* (the zero-tolerance compare — §4). But it
DOES observe the engagement: P1's ``0x50`` attack names the mob. So on each swing
it tells the **non-engaged** instance members' copies to Follow the engaging
player's avatar — the T0 Follow CreateAction (``0x04 0x16``,
``monster_ai.build_monster_follow_packet``). Their idle copy then visibly chases /
fights P1 instead of standing at spawn.

**HP-safety (the load-bearing constraint).** The Follow trailer carries the mob's
**spawn max HP**. A viewer who has NOT attacked this mob only *displays* it
(control mode 1) — its local HP is whatever the server last set (= max, untouched),
so the send matches by construction and the compare passes. The instant a viewer
attacks the mob themselves they begin *simulating* its HP locally; from then on
ANY server HP for that mob can mismatch and crash them, so an engaged viewer is
**excluded** from all further relays (each attacker owns their own copy). The
engaging player is likewise never sent anything — server packets break the native
chase for the client actually fighting the mob (bible §14.6 round 6n).

**Known cosmetic limit.** The client Follow action drives to the target's centre
with no range gate (the run-through, §14.6), so on the *viewer's* screen the mob
may overlap P1. That is strictly better than a mob frozen at spawn, and it is only
the non-fighting viewer's view. True one-copy mobs need owner-scoped enrollment
(§7 tail, [T2]).

Byte shape reuses the live-proven Follow packet; the relay *policy* is **[T1]**,
``# UNVERIFIED`` against a live 2-client capture. Kill-switch:
``DR_MOB_ENGAGEMENT_RELAY=0``.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Dict, Set

from ..core import log
from ..util.byte_io import LEWriter
from .monster_ai import build_monster_follow_packet

if TYPE_CHECKING:  # pragma: no cover
    from ..net.connection import RRConnection
    from ..net.game_server import GameServer

# Re-assert cadence: a held attack button fires ~2 swings/s, but the viewer only
# needs an occasional Follow refresh (its local mover keeps chasing between
# packets). One relay per second per mob keeps the viewer's channel-7 budget
# clear (bible §2) while surviving the mob's action swaps.
_RELAY_THROTTLE = 1.0

# mob_eid -> set of logins who have ATTACKED it (they now simulate its HP; never
# assert mob HP to them again).
_engaged: Dict[int, Set[str]] = {}
# mob_eid -> monotonic time of the last relay pass (throttle anchor).
_last_relay: Dict[int, float] = {}


def reset() -> None:
    """Drop all engagement state (test isolation / full-server reset)."""
    _engaged.clear()
    _last_relay.clear()


def purge_mob(mob_eid: int) -> None:
    """Forget a mob (called on its death / despawn) so a recycled eid starts
    clean — no stale throttle window or engaged-set survives a respawn."""
    _engaged.pop(mob_eid, None)
    _last_relay.pop(mob_eid, None)


def on_player_attack(server: "GameServer", attacker_conn: "RRConnection",
                     mob_eid: int, now: float) -> int:
    """Record ``attacker``'s engagement with ``mob_eid`` and relay a Follow to the
    instance's non-engaged viewers. Returns the number of viewers relayed to.

    Instance-scoped: the mob must belong to the attacker's exact
    ``(zone_gc_type, instance_id)`` copy, or a foreign eid would leak onto
    another copy's wire (Code 6 — bible §7).
    """
    if os.environ.get("DR_MOB_ENGAGEMENT_RELAY") == "0":
        return 0
    combat = getattr(server, "combat", None)
    if combat is None:
        return 0
    mob = combat.get_monster(mob_eid)
    if mob is None:
        return 0
    if mob.instance_id != attacker_conn.instance_id:
        return 0
    if mob.zone_gc_type != attacker_conn.current_zone_gc_type:
        return 0

    attacker_login = attacker_conn.login_name
    engaged = _engaged.setdefault(mob_eid, set())
    engaged.add(attacker_login)

    # Throttle the whole pass (per mob) — the viewer's local mover keeps chasing
    # between refreshes, so we only re-assert on the second-scale.
    last = _last_relay.get(mob_eid)
    if last is not None and now - last < _RELAY_THROTTLE:
        return 0

    avatar_eid = _avatar_eid(server, attacker_login)
    if not avatar_eid:
        return 0

    connections = getattr(server, "connections", None) or {}
    sent = 0
    for other in list(connections.values()):
        if other is attacker_conn or not getattr(other, "is_spawned", False):
            continue
        if other.login_name in engaged:
            continue                                    # they simulate their own copy
        if other.current_zone_gc_type != attacker_conn.current_zone_gc_type:
            continue
        if other.instance_id != attacker_conn.instance_id:
            continue
        # Framed-direct (own 0x07…0x06 stream), like the movement relay — the
        # Follow builder is UNFRAMED (interval-queue shaped), so wrap it. The
        # avatar_eid is the mob's chase target: the avatar ENTITY id is NOT
        # remapped per viewer (only component ids are — spawn.py:527), so P1's
        # global avatar id resolves on every instance member's client.
        w = LEWriter()
        w.write_byte(0x07)                              # BeginStream
        w.write_bytes(build_monster_follow_packet(mob.behavior_id, avatar_eid,
                                                  mob.max_hp))
        w.write_byte(0x06)                              # EndStream
        other.send_to_client(w.to_array())
        sent += 1

    if sent:
        _last_relay[mob_eid] = now
        log.debug(f"[MOB-ENGAGE] '{attacker_login}' mob={mob_eid} "
                  f"-> {sent} viewer(s) follow avatar={avatar_eid}")
    return sent


def _avatar_eid(server: "GameServer", login: str) -> int:
    getter = getattr(server, "get_player_avatar_id", None)
    if getter is not None:
        return getter(login) or 0
    return (getattr(server, "spawned_avatar_ids", None) or {}).get(login, 0)
