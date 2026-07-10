"""HP broadcast helpers — sends monster HP changes to zone players.

When a monster takes damage, the server should broadcast the HP change to all
players in the zone so they see updated HP bars.
"""
from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from ..util.byte_io import LEWriter

if TYPE_CHECKING:
    from ..net.game_server import GameServer
    from ..net.connection import RRConnection


def broadcast_monster_hp(server: "GameServer", conn: "RRConnection",
                          entity_id: int, current_hp: int, max_hp: int) -> None:
    """Broadcast a monster's HP update to all zone players.

    Sends the HP update in wire format within a BeginStream/EndStream envelope.
    Uses opcode 0x36 (EntitySyncHP) within a compression frame.
    """
    w = LEWriter()
    w.write_byte(0x07)                       # BeginStream
    w.write_byte(0x36)                       # EntitySyncHP
    w.write_uint16(entity_id)
    w.write_byte(0x02)                       # flags (has HP data)
    w.write_uint32(current_hp)               # current HP (wire format)
    w.write_byte(0x02)                       # WriteSynch
    w.write_uint32(0x00047E00)               # synch constant
    w.write_byte(0x06)                       # EndStream

    hp_packet = w.to_array()

    # bible §6-LIVE.7: a client that SIMULATES this mob (enrolled it into its
    # client AI via 0x64) computes the mob's HP locally and is packet-blind for
    # it — the server's value WILL diverge (unreproducible, §6-LIVE.6), so
    # sending it risks the zero-tolerance FUN_005dd900 compare (mob action in its
    # +0x95 bit-clear window) AND the Code-9 corpse-purge. Send mob HP ONLY to
    # clients DISPLAYING the mob (not in simulated_by). In solo the sole owner is
    # excluded, suppressing the send entirely. If the mob is unknown, fall back
    # to the old all-recipients behaviour.
    combat = getattr(server, "combat", None)
    mon = combat.get_monster(entity_id) if combat is not None else None
    simulated_by = getattr(mon, "simulated_by", None) or set()

    for other in list(server.connections.values()):
        if not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue
        if other.conn_id in simulated_by:
            continue          # this client simulates the mob — never assert its HP
        other.send_to_client(hp_packet)


def broadcast_hp_sync(server: "GameServer", conn: "RRConnection",
                       entity_id: int, current_hp: int, max_hp: int) -> None:
    """Broadcast an HP sync update. Used when combat damage occurs."""
    broadcast_monster_hp(server, conn, entity_id, current_hp, max_hp)
