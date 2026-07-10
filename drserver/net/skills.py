"""Skills usage handlers — hotbar place/remove, skill-slot equip, self-cast.

Port of the C# UnityGameServer skills-usage paths:

* **Hotbar PLACE / REMOVE** (subMessage ``0x35`` / ``0x36``,
  UnityGameServer.cs:12664+): the client drags a skill onto / off the hotbar
  and sends ``[slot:u32][typeFlag][gcHash:u32]`` / ``[slot:u32]``. The skill is
  resolved from the DJB2 hash of its lowercase GC class (the C# static
  ``_skillHashToGcClass`` table is exactly ``djb2(lower(gc_type))`` — verified
  against FireBolt/Sprint/HealSelf), persisted to ``character_skills``, and the
  player's **Manipulators** component gets a matching Add (``0x00``) / Remove
  (``0x01``) sub-update so the skill is actually usable.
* **Skill-slot equip** (``0x39`` on the Skills component — binary-verified
  ``Skills::equipSkill`` @ 0x5419C0): the client already assigned the slot
  locally; the server must consume the request bytes (entityRef + slot + synch
  suffix) and acknowledge SILENTLY — replying with 0x38 would undo the client's
  assignment, and not consuming corrupts the stream.
* **Self-cast** (action ``0x52`` with only ``[sessionID][slotID]`` left,
  UnityGameServer.cs:12067): ack-echo, relay the cast animation to other
  players in the zone/instance, and track the buff modifier the client applies
  locally so it survives zone changes (managers.player_modifiers).

Server-side spell damage from the C# handler is intentionally NOT ported:
combat is client-authoritative (binary-proven — see CLAUDE.md), the kill
replay tracker handles outcomes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional

from ..core import log
from ..db import character_repository
from ..data.gc_object import hash_djb2
from ..data.saved_character import HotbarSlotEntry, SavedCharacter
from ..util.byte_io import LEReader, LEWriter
from .component_update import write_synch, synch_hp

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection

# Lazy {djb2(lower(gc_type)): gc_type} over the client `skills` content table —
# replaces the C# hardcoded _skillHashToGcClass (same hashes, full coverage).
_skill_hash_catalog: Optional[Dict[int, str]] = None


def handle_skills_component_update(server: "GameServer", conn: "RRConnection",
                                   reader: LEReader, component_id: int,
                                   sub_message: int) -> bool:
    """Route a Skills-related ComponentUpdate. Returns True if consumed.

    ALL handling is gated on the player's Skills component id. C# claims
    sub-messages 0x35/0x36 on ANY component (UGS:12664) — but treating a
    stray 0x36 on another component as a hotbar remove would emit a garbage
    Manipulators Remove and delete a live skill/weapon manipulator
    client-side. We track the Skills id reliably (spawn), so gate on it and
    log anything else for diagnosis instead of consuming it.
    """
    if component_id != getattr(conn, "skills_component_id", 0):
        log.warn(f"[SKILLS] '{conn.login_name}' sub=0x{sub_message:02X} on "
                 f"non-Skills component {component_id} "
                 f"(skills={getattr(conn, 'skills_component_id', 0)}) — ignored, "
                 f"remaining={reader.remaining}")
        return False
    if sub_message == 0x35:
        return _handle_hotbar_place(server, conn, reader)
    if sub_message == 0x36:
        return _handle_hotbar_remove(server, conn, reader)
    if sub_message == 0x39:
        return _handle_skill_equip(server, conn, reader)
    return False


# ── Hotbar place / remove ────────────────────────────────────────────────────

def _handle_hotbar_place(server: "GameServer", conn: "RRConnection",
                         reader: LEReader) -> bool:
    """Hotbar PLACE: ``[slot:u32][typeFlag:1][gcHash:u32]`` (C# subMessage 0x35).

    Resolves the DJB2 hash to a skill GC class, applies the C# displacement
    rules to the session manipulator map + saved hotbar, persists, and sends
    the Manipulators Add so the client binds the skill to the slot.
    """
    if reader.remaining < 9:
        return False
    slot = reader.read_uint32()
    reader.read_byte()                       # typeFlag (unused, matches C#)
    gc_hash = reader.read_uint32()
    _consume_sync_suffix(server, conn, reader, "HOTBAR-PLACE")

    skill = _resolve_skill_hash(conn, gc_hash)
    if skill is None:
        log.warn(f"[HOTBAR] '{conn.login_name}' place slot={slot}: cannot "
                 f"resolve skill hash 0x{gc_hash:08X}")
        return True

    manip_map = _manip_map(conn)
    displaced = None
    existing = manip_map.get(slot)
    if existing is not None and existing.lower() != skill.lower():
        displaced = existing
    old_slot = next((s for s, gc in manip_map.items()
                     if gc.lower() == skill.lower() and s != slot), None)
    if old_slot is not None:
        del manip_map[old_slot]
    manip_map[slot] = skill

    saved = _load_saved(conn)
    if saved is not None:
        saved.hotbar_slots = [
            h for h in saved.hotbar_slots
            if h.slot != slot and h.skill.lower() != skill.lower()
            and (displaced is None or h.skill.lower() != displaced.lower())]
        saved.hotbar_slots.append(HotbarSlotEntry(slot=slot, skill=skill))
        character_repository.save_character(saved)

    skill_lv = saved.get_skill_level(skill) if saved is not None else 1
    _send_manipulator_add(conn, skill, slot, skill_lv)
    log.info(f"[HOTBAR] '{conn.login_name}' slot {slot} = '{skill}' "
             f"(displaced='{displaced}' old_slot={old_slot})")

    # Bling Gnome: PLACING the skill on the tray summons him; displacing the
    # last tray copy despawns him (client skill text + DRS-NET [HOTBAR] hooks).
    _sync_gnome_with_hotbar(server, conn, placed=skill, displaced=displaced)
    return True


def _sync_gnome_with_hotbar(server: "GameServer", conn: "RRConnection",
                            placed: Optional[str] = None,
                            displaced: Optional[str] = None) -> None:
    """Keep the Bling Gnome's existence in lockstep with the hotbar (DRS-NET
    GameServer.Combat hotbar PLACE/REMOVE gnome blocks)."""
    gnome = getattr(server, "gnome", None)
    if gnome is None:
        return
    if placed is not None and gnome.is_gnome_skill(placed):
        if not gnome.has_gnome(conn):
            gnome.spawn(conn)
        return
    if displaced is not None and gnome.is_gnome_skill(displaced):
        still_on_bar = any(gnome.is_gnome_skill(gc)
                           for gc in _manip_map(conn).values())
        if not still_on_bar and gnome.has_gnome(conn):
            gnome.despawn(conn)


def _handle_hotbar_remove(server: "GameServer", conn: "RRConnection",
                          reader: LEReader) -> bool:
    """Hotbar REMOVE: ``[slot:u32]`` (C# subMessage 0x36)."""
    if reader.remaining < 4:
        return False
    slot = reader.read_uint32()
    _consume_sync_suffix(server, conn, reader, "HOTBAR-REMOVE")

    removed = _manip_map(conn).pop(slot, None)
    saved = _load_saved(conn)
    if saved is not None:
        if removed is None:
            entry = next((h for h in saved.hotbar_slots if h.slot == slot), None)
            removed = entry.skill if entry is not None else None
        saved.hotbar_slots = [
            h for h in saved.hotbar_slots
            if h.slot != slot
            and (removed is None or h.skill.lower() != removed.lower())]
        character_repository.save_character(saved)

    _send_manipulator_remove(conn, slot)
    log.info(f"[HOTBAR] '{conn.login_name}' removed slot {slot} "
             f"(was '{removed}')")
    _sync_gnome_with_hotbar(server, conn, displaced=removed)
    return True


def _handle_skill_equip(server: "GameServer", conn: "RRConnection",
                        reader: LEReader) -> bool:
    """Skill-slot equip (0x39 on the Skills component, ``Skills::equipSkill``
    @ 0x5419C0): ``[0xFF + cstring skill | u16 entityRef][slot:1][synch]``.

    The client already assigned the slot locally — consume the bytes (or the
    stream desyncs) and accept silently; a 0x38 reply would UNDO the
    assignment (C# UnityGameServer.cs:12966).
    """
    skill = ""
    slot = 0
    if reader.remaining >= 1:
        ref_type = reader.read_byte()
        if ref_type == 0xFF and reader.remaining >= 1:
            skill = reader.read_cstring()
        elif reader.remaining >= 2:
            reader.read_uint16()             # entity-id ref (less common)
    if reader.remaining >= 1:
        slot = reader.read_byte()
    _consume_sync_suffix(server, conn, reader, "SKILL-EQUIP")
    log.info(f"[SKILL-EQUIP] '{conn.login_name}' accepted '{skill}' -> "
             f"slot {slot} (silent ack)")
    return True


# ── Self-cast (action 0x52, short form) ─────────────────────────────────────

def handle_self_cast(server: "GameServer", conn: "RRConnection",
                     reader: LEReader, component_id: int,
                     response_id: int) -> None:
    """A 0x52 self-cast (buff / AoE): ``[sessionID][slotID]`` only — the
    checkpoint-recall 0x52 carries a u16 target + cstring and never lands here
    (C# UnityGameServer.cs:12067 disambiguates on ``remaining <= 2``).

    Echo the ActionResponse, relay the cast animation to same-zone/instance
    viewers, and track the buff modifier for zone-change re-send. The client
    applies the buff and any damage itself (client-authoritative combat).

    The ack is LOAD-BEARING and sent unconditionally — the same rule as the
    0x50/0x51 combat acks (net/movement.py): without it the client's cast
    action never resolves (no animation, no effect) and it re-sends the 0x52
    on retry cadence (live 2026-07-02: dungeon Stomp spam, 3 casts/s logged,
    nothing played). The old ``suppress_originated_avatar_hp`` gate dropped
    this ack in every combat zone, over-applying the Regime-B posture exactly
    like the dropped 0x50 ack it was fixed for. The trailer ships the clamped
    last self-report (``_heartbeat_hp``), identical to the 0x50 ack.

    Delivery rides the INTERVAL queue, not the per-tick flush: a held skill
    button is a sustained action stream, and each per-tick-flushed ack is an
    extra channel-7 message on top of the exactly-saturated 7.5/s 0x0D
    cadence → the client's >2-backlog 3× world-clock catch-up engages
    (bible.md §2; the live 2026-07-02 "game speeds up while holding
    attack/skill" bug).
    """
    from . import movement
    from ..managers import player_modifiers

    session_id = reader.read_byte() if reader.remaining >= 1 else 0
    slot_id = reader.read_byte() if reader.remaining >= 1 else 0

    # ── ActionResponse echo (same delivery rules as the 0x50/0x51 acks) ──
    w = LEWriter()
    w.write_byte(0x35)
    w.write_uint16(component_id)
    w.write_byte(0x01); w.write_byte(response_id)
    w.write_byte(0x52); w.write_byte(session_id)
    w.write_byte(slot_id)
    movement._write_owner_synch_trailer(w, movement._heartbeat_hp(conn))
    conn.interval_message_queue.enqueue(w.to_array())

    _broadcast_self_cast(server, conn, session_id, slot_id)

    # ── Bling Gnome cast: summon when absent, open the 10 s convert window
    # when present (DRS-NET HandleSelfCastSpell [SPELL-BLING] branch). The
    # gnome is server-run, not a client-side buff — skip buff tracking. ──
    skill = _manip_map(conn).get(slot_id)
    gnome = getattr(server, "gnome", None)
    if gnome is not None and gnome.is_gnome_skill(skill):
        gnome.toggle(conn)
        log.info(f"[SPELL-0x52] '{conn.login_name}' Bling Gnome cast "
                 f"slot={slot_id} -> toggle")
        return

    # ── Summon self-casts (Build Snowman): spawn the unit server-side —
    # the SpellSpawnEffect entity create is server-owned (managers.summons). ──
    summons = getattr(server, "summons", None)
    if summons is not None and summons.try_cast(conn, skill):
        log.info(f"[SPELL-0x52] '{conn.login_name}' summon cast "
                 f"slot={slot_id} skill='{skill}'")
        return

    # ── Buff tracking for zone-change re-send (C# BUFF TRACKING block) ──
    if skill is not None:
        saved = _load_saved(conn)
        skill_lv = saved.get_skill_level(skill) if saved is not None else 1
        player_modifiers.track_skill_buff(conn, skill, skill_lv)
    log.info(f"[SPELL-0x52] '{conn.login_name}' self-cast slot={slot_id} "
             f"skill='{skill}'")


def _broadcast_self_cast(server: "GameServer", conn: "RRConnection",
                         session_id: int, slot_id: int) -> None:
    """Relay the self-cast animation to other spawned players in the same
    zone/instance. Delegates to the unified :mod:`net.action_relay` (CreateAction
    ``0x52`` on each viewer's remapped behavior id, empty synch, interval-queue
    delivery). The mode byte is normalized to 0x00 — the actor's rolling session
    id is meaningless on the viewer's copy (the same rule the 0x50/0x51 relays
    follow); ``slot_id`` selects the spell so the right animation plays.

    Superseded the old framed-direct ``send_to_client`` path (2026-07-09): a held
    skill button is a sustained stream, so the relay must ride the viewer's 0x0D
    interval frame like every other action relay (bible §2)."""
    from . import action_relay
    action_relay.relay_player_action(server, conn, 0x52,
                                     bytes([0x00, slot_id & 0xFF]))


# ── Manipulators sub-updates (server → client) ───────────────────────────────

def _send_manipulator_add(conn: "RRConnection", skill_gc_class: str,
                          slot: int, skill_level: int) -> None:
    """Manipulators Add (``0x00``) binding a skill to a hotbar slot id —
    C# [HOTBAR-MANIP] Sent Add (UnityGameServer.cs:12812)."""
    manip_id = getattr(conn, "manipulators_component_id", 0)
    if not manip_id:
        log.warn(f"[HOTBAR] '{conn.login_name}' no Manipulators component id")
        return
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x35)
    w.write_uint16(manip_id)
    w.write_byte(0x00)                       # Add
    w.write_byte(0xFF)
    w.write_cstring(skill_gc_class.lower())
    w.write_uint32(slot)
    w.write_byte(max(1, skill_level) & 0xFF)
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())


def _send_manipulator_remove(conn: "RRConnection", slot: int) -> None:
    """Manipulators Remove (``0x01``) for a hotbar slot id —
    C# [HOTBAR-MANIP] Sent Remove (UnityGameServer.cs:12721)."""
    manip_id = getattr(conn, "manipulators_component_id", 0)
    if not manip_id:
        return
    w = LEWriter()
    w.write_byte(0x07)
    w.write_byte(0x35)
    w.write_uint16(manip_id)
    w.write_byte(0x01)                       # Remove
    w.write_uint32(slot)
    write_synch(w, synch_hp(conn))
    w.write_byte(0x06)
    conn.send_to_client(w.to_array())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _manip_map(conn: "RRConnection") -> Dict[int, str]:
    """The session slot-id → skill GC class map (built at spawn Op4 — the C#
    ``_playerManipMap``)."""
    manip_map = getattr(conn, "skill_manip_map", None)
    if manip_map is None:
        manip_map = {}
        conn.skill_manip_map = manip_map
    return manip_map


def _load_saved(conn: "RRConnection") -> Optional[SavedCharacter]:
    try:
        return character_repository.get_character(conn.char_sql_id)
    except Exception as ex:  # noqa: BLE001 — never desync the stream over DB
        log.warn(f"[HOTBAR] '{conn.login_name}' character load failed: {ex}")
        return None


def _resolve_skill_hash(conn: "RRConnection", gc_hash: int) -> Optional[str]:
    """DJB2 hash → skill GC class: the player's session manipulators first
    (C# fallback loop), then the full client skills catalogue."""
    for gc in _manip_map(conn).values():
        if hash_djb2(gc) == gc_hash:
            return gc
    return _hash_catalog().get(gc_hash)


def _hash_catalog() -> Dict[int, str]:
    global _skill_hash_catalog
    if _skill_hash_catalog is None:
        catalog: Dict[int, str] = {}
        try:
            from ..db import game_database as db
            for row in db.execute_reader("SELECT gc_type FROM skills").fetchall():
                gc = row["gc_type"]
                if gc:
                    catalog[hash_djb2(gc)] = gc
        except Exception as ex:  # noqa: BLE001 — DB optional in unit tests
            log.warn(f"[HOTBAR] skills catalogue unavailable: {ex}")
            return {}
        _skill_hash_catalog = catalog
    return _skill_hash_catalog


def _consume_sync_suffix(server: "GameServer", conn: "RRConnection",
                         reader: LEReader, source: str) -> None:
    """Consume the trailing EntitySynchInfo (``[flags][HP:u32 if flags&0x02]``)
    so the stream stays aligned; adopt a self-reported HP like every other
    inbound suffix (C# TryConsumeClientSyncSuffix)."""
    if reader.remaining < 1:
        return
    flags = reader.read_byte()
    if flags & 0x02 and reader.remaining >= 4:
        hp = reader.read_uint32()
        combat = getattr(server, "combat", None)
        if combat is not None:
            combat.adopt_client_avatar_hp(conn, hp, source)
