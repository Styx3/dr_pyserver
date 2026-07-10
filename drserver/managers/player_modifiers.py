"""Tracked player buff modifiers — self-cast buffs that must survive zone changes.

Port of the C# UnityGameServer ``_buffModifierMap`` + ``ModifierTracker`` +
``SendTrackedModifier``/``ResendAllModifiers`` (UnityGameServer.cs:294, 11164,
25386). When a player self-casts a buff skill (action 0x52) the CLIENT applies
the buff modifier locally — status-effect simulation is client-authoritative —
so the server sends NO Add at cast time. But the client drops every modifier on
a zone transition, so the server tracks each buff it recognises and re-sends the
still-active ones (a Modifiers ``0x00`` Add carrying the REMAINING duration)
right after the zone-entry spawn completes.

Durations go on the wire in engine ticks (1000/24 ≈ 41.667 ticks/sec); a wire
duration of 0 means permanent/client-managed. The re-send adds a 3-second
zone-loading buffer before declaring a buff expired (C# GetModifiers).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

from ..core import log
from ..util.byte_io import LEWriter
from ..net.component_update import write_synch, synch_hp

if TYPE_CHECKING:  # pragma: no cover
    from ..net.connection import RRConnection

_TICKS_PER_SEC = 1000.0 / 24.0
_ZONE_BUFFER_TICKS = 3.0 * _TICKS_PER_SEC

# ── Buff modifier map: skill short name → (modifier GC type, base duration s,
# duration increase per skill level s). Verbatim port of C# _buffModifierMap
# (UnityGameServer.cs:294) including the "diviineintervention" typo alias.
BUFF_MODIFIER_MAP: Dict[str, tuple] = {
    "sprint":                    ("skills.generic.Sprint.Modifier",                    30.0, 10.0),
    "manashield":                ("skills.generic.ManaShield.Modifier",                180.0,  0.0),
    "healsself":                 ("skills.generic.HealSelf.Modifier",                    0.0,  0.0),
    "manaself":                  ("skills.generic.ManaSelf.Modifier",                    0.0,  0.0),
    "blight":                    ("skills.generic.Blight.Modifier",                     30.0,  1.0),
    "charge":                    ("skills.generic.Charge.CastModifier",                150.0,  0.5),
    "divineresistbuff":          ("skills.generic.DivineResistBuff.Modifier",           40.0,  0.0),
    "fireresistbuff":            ("skills.generic.FireResistBuff.Modifier",             40.0,  0.0),
    "iceresistbuff":             ("skills.generic.IceResistBuff.Modifier",              40.0,  0.0),
    "poisonresistbuff":          ("skills.generic.PoisonResistBuff.Modifier",           40.0,  0.0),
    "shadowresistbuff":          ("skills.generic.ShadowResistBuff.Modifier",           40.0,  0.0),
    "divinedamagebuff":          ("skills.generic.DivineDamageBuff.Modifier",           30.0,  0.0),
    "firedamagebuff":            ("skills.generic.FireDamageBuff.Modifier",             30.0,  0.0),
    "icedamagebuff":             ("skills.generic.IceDamageBuff.Modifier",              30.0,  0.0),
    "poisondamagebuff":          ("skills.generic.PoisonDamageBuff.Modifier",           30.0,  0.0),
    "shadowdamagebuff":          ("skills.generic.ShadowDamageBuff.Modifier",           30.0,  0.0),
    "1hmeleespeedbuff":          ("skills.generic.1HMeleeSpeedBuff.Modifier",           30.0,  0.0),
    "2hmeleespeedbuff":          ("skills.generic.2HMeleeSpeedBuff.Modifier",           30.0,  0.0),
    "rangedspeedbuff":           ("skills.generic.RangedSpeedBuff.Modifier",            30.0,  0.0),
    "stunresistbuff":            ("skills.generic.StunResistBuff.Modifier",             30.0,  0.0),
    "minmovespeedbuff":          ("skills.generic.MinMoveSpeedBuff.Modifier",           15.0,  0.0),
    "aggroincreasemodbuff":      ("skills.generic.AggroIncreaseModBuff.Modifier",       25.0,  5.0),
    "meleedamagereflectionbuff": ("skills.generic.MeleeDamageReflectionBuff.Modifier",  30.0,  0.0),
    "stomp":                     ("skills.generic.Stomp.VisualModifier",                 0.0,  0.0),
    "poisonblastradius":         ("skills.generic.PoisonBlastRadius.Modifier",           4.0,  0.25),
    "shadowrage":                ("skills.generic.ShadowRage.CastModifier",             25.0,  1.0),
    "firecone":                  ("skills.generic.FireCone.CastModifier",                0.0,  0.0),
    "diviineintervention":       ("skills.generic.DivineIntervention.Modifier",         15.0,  1.0),
    "divineintervention":        ("skills.generic.DivineIntervention.Modifier",         15.0,  1.0),
    "strengthbuff":              ("skills.generic.StrengthBuff.Modifier",               30.0, 15.0),
    "shadowtendrils":            ("skills.generic.ShadowTendrils.Modifier",             30.0,  0.0),
    "firetrail":                 ("skills.generic.FireTrail.Modifier",                  25.0,  0.0),
    "poisontrail":               ("skills.generic.PoisonTrail.Modifier",                60.0,  0.0),
}

_next_modifier_id = 1


@dataclass(frozen=True)
class TrackedModifier:
    gc_type: str
    mod_id: int
    level: int = 0
    power_level: int = 0
    duration_ticks: int = 0      # 0 = permanent
    source_is_self: int = 0
    added_at: float = 0.0        # time.monotonic()


def _next_id() -> int:
    global _next_modifier_id
    nid = _next_modifier_id
    _next_modifier_id += 1
    return nid


def _store(conn: "RRConnection") -> Dict[str, TrackedModifier]:
    store = getattr(conn, "tracked_modifiers", None)
    if store is None:
        store = {}
        conn.tracked_modifiers = store
    return store


def buff_for_skill(skill_gc_class: str) -> Optional[tuple]:
    """The (modifier gc type, base s, inc s) entry for a skill gc class, by its
    short name ("skills.generic.Sprint" → "sprint") — or None."""
    short = (skill_gc_class or "").lower().rsplit(".", 1)[-1]
    return BUFF_MODIFIER_MAP.get(short)


def track_skill_buff(conn: "RRConnection", skill_gc_class: str,
                     skill_level: int) -> bool:
    """Track the buff modifier a self-cast skill applies client-side, so the
    zone-change re-send can restore it. Returns True iff the skill maps to a
    known buff. Port of the C# HandleSelfCastSpell BUFF TRACKING block."""
    buff = buff_for_skill(skill_gc_class)
    if buff is None:
        return False
    mod_gc, dur_base, dur_inc = buff
    duration_ticks = (0 if dur_base == 0 else
                      int((dur_base + skill_level * dur_inc) * _TICKS_PER_SEC))
    store = _store(conn)
    # One instance per modifier type — re-cast replaces (C# TrackModifier).
    store[mod_gc.lower()] = TrackedModifier(
        gc_type=mod_gc, mod_id=_next_id(), level=skill_level & 0xFF,
        duration_ticks=duration_ticks, source_is_self=0x01,
        added_at=time.monotonic())
    log.info(f"[BUFF-TRACK] '{conn.login_name}' {skill_gc_class} -> '{mod_gc}' "
             f"dur={duration_ticks} ticks lv={skill_level}")
    return True


def apply_buff(conn: "RRConnection", mod_gc_type: str,
               duration_seconds: float, level: int = 0) -> bool:
    """Apply (and track) an arbitrary buff modifier on the player — e.g. the
    ``AttributeModifier`` an interactive shrine grants. Rides the SAME proven
    Modifiers ``0x00`` Add wire as the spawn-invuln / skill buffs
    (:func:`build_modifier_add_packet`), and tracks it so a zone change
    re-sends it. ``duration_seconds <= 0`` ⇒ permanent. Returns False when the
    player's Modifiers component id isn't known yet."""
    if not getattr(conn, "modifiers_id", 0):
        return False
    mod = TrackedModifier(
        gc_type=mod_gc_type, mod_id=_next_id(), level=level & 0xFF,
        duration_ticks=(0 if duration_seconds <= 0
                        else int(duration_seconds * _TICKS_PER_SEC)),
        source_is_self=0x01, added_at=time.monotonic())
    _store(conn)[mod_gc_type.lower()] = mod
    conn.send_to_client(build_modifier_add_packet(conn, mod))
    log.info(f"[BUFF] '{conn.login_name}' applied '{mod_gc_type}' "
             f"dur={mod.duration_ticks} ticks")
    return True


def untrack(conn: "RRConnection", mod_gc_type: str) -> bool:
    """Drop a tracked modifier by GC type (C# UntrackModifier)."""
    return _store(conn).pop(mod_gc_type.lower(), None) is not None


def active_modifiers(conn: "RRConnection") -> List[TrackedModifier]:
    """Still-active tracked modifiers as remaining-duration COPIES, with the 3 s
    zone-loading buffer applied; expired ones are dropped from the store
    (C# ModifierTracker.GetModifiers)."""
    store = _store(conn)
    now = time.monotonic()
    result: List[TrackedModifier] = []
    for key in list(store.keys()):
        mod = store[key]
        if mod.duration_ticks == 0:
            result.append(mod)
            continue
        elapsed_ticks = (now - mod.added_at) * _TICKS_PER_SEC + _ZONE_BUFFER_TICKS
        if elapsed_ticks >= mod.duration_ticks:
            del store[key]
            continue
        remaining = int(mod.duration_ticks - elapsed_ticks)
        result.append(TrackedModifier(
            gc_type=mod.gc_type, mod_id=mod.mod_id, level=mod.level,
            power_level=mod.power_level, duration_ticks=remaining,
            source_is_self=mod.source_is_self, added_at=mod.added_at))
    return result


def build_modifier_add_packet(conn: "RRConnection", mod: TrackedModifier) -> bytes:
    """A Modifiers ``0x00`` Add stream for one tracked modifier — the 14-byte
    ``Modifier::readData`` body (TTD-proven @ 0x4FF390, same wire as the potion
    modifier in net.inventory). Port of C# SendTrackedModifier."""
    w = LEWriter()
    w.write_byte(0x07)                       # BeginStream
    w.write_byte(0x35)                       # ComponentUpdate
    w.write_uint16(conn.modifiers_id)
    w.write_byte(0x00)                       # Add modifier
    w.write_byte(0xFF)
    w.write_cstring(mod.gc_type)             # client hashes case-insensitively
    w.write_uint32(mod.mod_id)
    w.write_byte(mod.level & 0xFF)
    w.write_uint32(mod.power_level)
    w.write_uint32(mod.duration_ticks)       # 0 = permanent
    w.write_byte(mod.source_is_self & 0xFF)
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)                       # EndStream
    return w.to_array()


def resend_all(conn: "RRConnection") -> int:
    """Re-send every still-active tracked modifier after a zone-entry spawn —
    port of C# ResendAllModifiers (called after SendPlayerEntitySpawn,
    UnityGameServer.cs:18804). Returns the number of modifiers sent."""
    if not getattr(conn, "modifiers_id", 0):
        return 0
    mods = active_modifiers(conn)
    for mod in mods:
        conn.send_to_client(build_modifier_add_packet(conn, mod))
        log.info(f"[MOD-RESEND] '{conn.login_name}' '{mod.gc_type}' "
                 f"id={mod.mod_id} dur={mod.duration_ticks} ticks")
    return len(mods)


# ── Permanent avatar.base modifiers (spawn invulnerability + free-player XP) ──
#
# Both ride the same Modifiers 0x00 Add wire as the tracked buffs above; the
# client simulates the effect locally (DAMAGE_IMMUNITY / EXPMOD). Port of C#
# SendZoneSpawnInvulnerability (GameServer.Types.cs:846, fixed id 3, duration
# 1800 ticks ≈ 43 s) and SendFreePlayerModifier (GameServer.Combat.cs:2724,
# fixed id 1, duration 0 = permanent, once per login).

ZONE_SPAWN_INVULN_GC = "avatar.base.ZoneSpawnInvulnerabilityModifier"
ZONE_SPAWN_INVULN_ID = 3
ZONE_SPAWN_INVULN_DURATION_TICKS = 1800
FREE_PLAYER_XP_GC = "avatar.base.FreePlayerExperienceModifier"
FREE_PLAYER_XP_ID = 1


def zone_allows_spawn_invulnerability(zone_name: str) -> bool:
    """Combat zones get the spawn-protection window; towns/hubs/tutorial/pvp
    staging do not. Verbatim port of C# ZoneAllowsSpawnInvulnerability."""
    zone = (zone_name or "").strip().lower()
    if not zone:
        return False
    if zone in ("tutorial", "world.tutorial", "town", "world.town",
                "thehub", "world.thehub", "pvp_start", "pvp_hub"):
        return False
    if zone.startswith("town") or zone.startswith("world.town") or "hub" in zone:
        return False
    if zone == "amazon_dungeon":
        return True
    return zone.startswith(("dungeon", "world.dungeon", "d0", "d1", "elite",
                            "epic", "squeakeasy", "deathmatch", "pvpgroup", "pvpduel"))


def send_zone_spawn_invulnerability(conn: "RRConnection") -> bool:
    """Brief DAMAGE_IMMUNITY right after a zone-entry spawn (join + warp).
    Returns True iff sent (combat zone + modifiers component known)."""
    if not getattr(conn, "modifiers_id", 0):
        return False
    if not zone_allows_spawn_invulnerability(getattr(conn, "current_zone_name", "")):
        return False
    mod = TrackedModifier(gc_type=ZONE_SPAWN_INVULN_GC, mod_id=ZONE_SPAWN_INVULN_ID,
                          level=0, power_level=0,
                          duration_ticks=ZONE_SPAWN_INVULN_DURATION_TICKS,
                          source_is_self=0x01)
    conn.send_to_client(build_modifier_add_packet(conn, mod))
    log.info(f"[ZONE-INVULN] '{conn.login_name}' spawn protection sent "
             f"(zone={conn.current_zone_name})")
    return True


def _account_is_member(login_name: str) -> bool:
    """``accounts.is_member`` lookup. Unknown account or DB error counts as
    MEMBER so a paying account is never mislabeled free (same default as the
    merchants membership gate)."""
    if not login_name:
        return True
    try:
        from ..db import game_database as db
        row = db.execute_reader(
            "SELECT is_member FROM accounts WHERE LOWER(username)=:u",
            {"u": login_name.lower()}).fetchone()
        return bool(row[0]) if row is not None else True
    except Exception:  # noqa: BLE001
        return True


def send_free_player_modifier(conn: "RRConnection") -> bool:
    """Free-account XP modifier — PERSISTENT for free players: re-sent after
    EVERY zone-entry spawn (join + warp), because the client drops all
    modifiers on a zone transition. Member accounts (``accounts.is_member``)
    never receive it. (The C# emulator only sent it once per login, losing the
    XP cap after the first warp — deliberately not copied.)"""
    if not getattr(conn, "modifiers_id", 0):
        return False
    if _account_is_member(getattr(conn, "login_name", "")):
        return False
    mod = TrackedModifier(gc_type=FREE_PLAYER_XP_GC, mod_id=FREE_PLAYER_XP_ID,
                          level=0, power_level=0, duration_ticks=0,
                          source_is_self=0x01)
    conn.send_to_client(build_modifier_add_packet(conn, mod))
    log.info(f"[XP-MOD] '{conn.login_name}' FreePlayerExperienceModifier sent "
             f"(zone={getattr(conn, 'current_zone_name', '')})")
    return True
