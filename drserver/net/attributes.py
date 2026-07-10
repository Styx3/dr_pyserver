"""Attribute allocation — spend / return / respec primary-stat points.

Port of the C# ``UnityGameServer`` entity-request handlers
(``HandleStatSpendRequest`` / ``HandleStatReturnRequest`` / ``HandleRespecRequest``,
UnityGameServer.cs:19406-19737). The client sends these as channel-7 entity
requests (opcode ``0x04``) with a ``requestType`` byte:

* ``0x11`` SpendAttribPoint  — body ``[statType:u8][numPoints:u8]``
* ``0x12`` ReturnAttribPoint  — body ``[statType:u8][numPoints:u8]``
* ``0x13`` ReSpec            — no body

``statType`` is ``0=STR 1=AGI 2=END 3=INT``. The avatar earns
``NativeStatPointsPerLevel`` (5) allocatable points per level; the pool a player
may still spend is ``(level - 1) * 5 - allocated``. Allocated points live on the
``characters`` table (``stat_strength`` / ``stat_agility`` / ``stat_endurance`` /
``stat_intellect``) and are echoed to the client at spawn via the Hero WriteInit
``stat_pts_remaining`` field (see ``spawn.write_avatar_entity_init``). This module
adds the missing inbound path: validate the request, persist the new allocation,
and confirm it to the client so its Attributes panel updates live.

The avatar's synched wire HP is level-derived (``compute_avatar_max_hp_wire``) and
class/stat-independent by design (see ``data/player_state``), so an endurance
allocation does NOT move ``conn.hp_wire`` here — that keeps the zero-tolerance
avatar synch compare safe. The DB-side HP/MP (``refresh_player_state``) is still
recomputed so non-wire displays and saved state track the new stats.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import log
from ..db import character_repository
from ..util.byte_io import LEReader, LEWriter
from .component_update import synch_hp, write_synch

if TYPE_CHECKING:  # pragma: no cover
    from .connection import RRConnection
    from .game_server import GameServer

# Native const (C# UnityGameServer.NativeStatPointsPerLevel) — points per level.
STAT_POINTS_PER_LEVEL = 5

# requestType bytes carried after the 0x04 entity-request opcode.
REQ_SPEND_ATTRIB = 0x11
REQ_RETURN_ATTRIB = 0x12
REQ_RESPEC = 0x13

_STAT_COLUMNS = ("stat_strength", "stat_agility", "stat_endurance", "stat_intellect")

# Respec cooldown — C# HandleRespecRequest uses 15 minutes between respecs.
_RESPEC_COOLDOWN_SECONDS = 900

# ReSpecCost CurveTable (Tables.gc): keyframes L1=0.2, L10=0.2, L120=360 with the
# endpoint values stored as Fixed32 via CEIL — ceil(0.2*256)=52, 360*256=92160.
# Binary-verified gold costs: L1=203, L14=13285, L20=32906, L30=65617, L40=98324.
_RESPEC_CURVE_FX32: tuple[tuple[int, int], ...] = ((1, 52), (10, 52), (120, 92160))


def allocated_total(saved) -> int:  # noqa: ANN001 — SavedCharacter
    """Sum of the four allocated primary stats."""
    return (saved.stat_strength + saved.stat_agility
            + saved.stat_endurance + saved.stat_intellect)


def points_available(saved) -> int:  # noqa: ANN001 — SavedCharacter
    """Unspent attribute points: ``(level - 1) * 5 - allocated`` (never negative)."""
    earned = max(0, (saved.level - 1)) * STAT_POINTS_PER_LEVEL
    return max(0, earned - allocated_total(saved))


def respec_cost_gold(level: int) -> int:
    """Gold cost to respec at ``level`` — port of C# ``EvaluateReSpecCostCurveFx32``.

    Integer-only interpolation across the ReSpecCost curve, then ``(fx32 * 1000)
    >> 8`` — byte-exact with the client binary (curve evaluator 0x5d4050). Matches
    the binary-verified samples L1=203, L14=13285, L20=32906, L30=65617, L40=98324.
    """
    points = _RESPEC_CURVE_FX32
    if level <= points[0][0]:
        fx32 = points[0][1]
    elif level >= points[-1][0]:
        fx32 = points[-1][1]
    else:
        fx32 = points[-1][1]
        for i in range(1, len(points)):
            if level <= points[i][0]:
                l_lo, v_lo = points[i - 1]
                l_hi, v_hi = points[i]
                t_fp = ((level - l_lo) * 65536) // (l_hi - l_lo)
                fx32 = v_lo + (((v_hi - v_lo) * t_fp) >> 16)
                break
    return (fx32 * 1000) >> 8


def _set_stat(saved, stat_type: int, delta: int) -> None:  # noqa: ANN001
    col = _STAT_COLUMNS[stat_type]
    setattr(saved, col, getattr(saved, col) + delta)


# ── Inbound handlers (called from movement._handle_entity_request) ──────────────

def handle_stat_spend(server: "GameServer", conn: "RRConnection",
                      reader: LEReader) -> None:
    """0x11 SpendAttribPoint — allocate ``numPoints`` into ``statType``.

    Port of C# ``HandleStatSpendRequest``. Validates against the unspent pool,
    persists the new allocation, refreshes DB-side HP/MP, and confirms to the
    client with a Hero stat update (so the Attributes panel decrements live).
    """
    if reader.remaining < 2:
        log.debug("[STAT-SPEND] not enough data")
        return
    stat_type = reader.read_byte()      # 0=STR 1=AGI 2=END 3=INT
    num_points = reader.read_byte()
    if stat_type > 3 or num_points == 0:
        log.debug(f"[STAT-SPEND] invalid statType={stat_type} numPoints={num_points}")
        return

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return

    remaining = points_available(saved)
    if num_points > remaining:
        log.debug(f"[STAT-SPEND] not enough: want={num_points} have={remaining} "
                  f"'{conn.login_name}'")
        return

    _set_stat(saved, stat_type, num_points)
    character_repository.save_character(saved)
    _refresh_after_alloc(server, conn)
    log.info(f"[STAT-SPEND] '{conn.login_name}' +{num_points} stat={stat_type} "
             f"(STR={saved.stat_strength} AGI={saved.stat_agility} "
             f"END={saved.stat_endurance} INT={saved.stat_intellect} "
             f"pts={remaining - num_points})")
    send_hero_stat_update(server, conn, REQ_SPEND_ATTRIB, num_points, stat_type)


def handle_stat_return(server: "GameServer", conn: "RRConnection",
                       reader: LEReader) -> None:
    """0x12 ReturnAttribPoint — refund ``numPoints`` from ``statType``.

    Port of C# ``HandleStatReturnRequest`` — only refunds what is already
    allocated, persists, and confirms to the client.
    """
    if reader.remaining < 2:
        log.debug("[STAT-RETURN] not enough data")
        return
    stat_type = reader.read_byte()
    num_points = reader.read_byte()
    if stat_type > 3 or num_points == 0:
        return

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return

    current = getattr(saved, _STAT_COLUMNS[stat_type])
    if num_points > current:
        log.debug(f"[STAT-RETURN] over-refund: want={num_points} have={current}")
        return

    _set_stat(saved, stat_type, -num_points)
    character_repository.save_character(saved)
    _refresh_after_alloc(server, conn)
    log.info(f"[STAT-RETURN] '{conn.login_name}' -{num_points} stat={stat_type} "
             f"(STR={saved.stat_strength} AGI={saved.stat_agility} "
             f"END={saved.stat_endurance} INT={saved.stat_intellect})")
    send_hero_stat_update(server, conn, REQ_RETURN_ATTRIB, num_points, stat_type)


def handle_respec(server: "GameServer", conn: "RRConnection") -> None:
    """0x13 ReSpec — reset all allocated points for a gold cost.

    Port of C# ``HandleRespecRequest``: 15-minute cooldown, gold cost from the
    ReSpecCost curve, zero the four stats, then tell the client to reset its
    Attributes panel. Gold is deducted DB-side (the Python server, unlike C#,
    does not push a live currency packet) with a system-message receipt.
    """
    import time

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return

    now_unix = int(time.time())
    if saved.last_respec_time > 0:
        elapsed = now_unix - saved.last_respec_time
        if elapsed < _RESPEC_COOLDOWN_SECONDS:
            remaining = _RESPEC_COOLDOWN_SECONDS - elapsed
            conn.send_system_message(
                f"Respec on cooldown. {remaining // 60}m {remaining % 60}s remaining.")
            return

    cost = respec_cost_gold(saved.level)
    if saved.gold < cost:
        conn.send_system_message(
            f"Not enough gold to respec. Need {cost}, have {saved.gold}.")
        return

    saved.gold -= cost
    saved.stat_strength = 0
    saved.stat_agility = 0
    saved.stat_endurance = 0
    saved.stat_intellect = 0
    saved.last_respec_time = now_unix
    saved.respec_count += 1
    character_repository.save_character(saved)
    _refresh_after_alloc(server, conn)

    avatar_id = server.get_player_avatar_id(conn.login_name)
    if avatar_id:
        _send_respec_reset_packet(conn, avatar_id)
    conn.send_system_message(f"Respec complete — {cost} gold spent. "
                             "All attribute points refunded.")
    log.info(f"[RESPEC] '{conn.login_name}' reset stats, -{cost} gold "
             f"(respec #{saved.respec_count})")


# ── Outbound packets ────────────────────────────────────────────────────────────

def send_hero_stat_update(server: "GameServer", conn: "RRConnection",
                          sub_type: int, num_points: int, stat_type: int) -> None:
    """SendHeroStatUpdate — confirm a spend/return to the client's Hero.

    Port of C# ``SendHeroStatUpdate``. Wire: ``0x07`` BeginStream, ``0x03``
    processEntityUpdate, ``uint16 avatarId``, ``subType`` (0x11/0x12), ``statType``,
    ``numPoints``, the avatar entity-synch trailer, ``0x06`` EndStream. The client's
    ``processUpdateSpendAttribPoint`` reads ``statType`` then ``numPoints``.
    """
    avatar_id = server.get_player_avatar_id(conn.login_name)
    if not avatar_id:
        return
    w = LEWriter()
    w.write_byte(0x07)               # BeginStream
    w.write_byte(0x03)               # processEntityUpdate
    w.write_uint16(avatar_id)
    w.write_byte(sub_type)           # 0x11 spend / 0x12 return
    w.write_byte(stat_type)          # read first by processUpdateSpendAttribPoint
    w.write_byte(num_points)         # read second
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)               # EndStream
    conn.send_to_client(w.to_array())


def _send_respec_reset_packet(conn: "RRConnection", avatar_id: int) -> None:
    """Respec stat-reset packet — C# HandleRespecRequest tail.

    Wire: ``0x07 0x03 <uint16 avatarId> 0x13 0x02 <uint32 0xFFFF00> 0x06`` (no
    synch trailer, verbatim with C#). Tells the client's Hero to clear its
    allocated points so the Attributes panel shows a full unspent pool.
    """
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x03)
    w.write_uint16(avatar_id)
    w.write_byte(REQ_RESPEC)
    w.write_byte(0x02)
    w.write_uint32(0xFFFF00)
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())


def _refresh_after_alloc(server: "GameServer", conn: "RRConnection") -> None:
    """Recompute DB-side HP/MP from the new allocation (does not touch hp_wire)."""
    try:
        from ..data.player_state import refresh_player_state
        refresh_player_state(conn)
    except Exception as ex:  # noqa: BLE001 — best-effort, never break the request
        log.debug(f"[STAT-ALLOC] refresh_player_state failed: {ex}")
