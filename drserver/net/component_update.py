"""Shared ComponentUpdate stream helpers.

A UnitBehavior update stream is ``0x07 <component-updates…> 0x06``. Each
ComponentUpdate (``0x35 <componentId:u16> <subOpcode> <body…>``) MUST be
followed by a **synch trailer** before the next ``0x35`` or the closing
``0x06``. The client's ``UnitBehavior::ReadUpdate`` reads exactly one
sub-message then the synch; omitting it makes the reader consume the next
control byte as the synch flag, desyncing the entire stream — the client
throws "zone communication error" and hard-crashes (see CLAUDE.md golden
gotchas + the live-test unequip/inventory crashes).

Port of C# ``EquipmentHandler.WriteSynch`` (EquipmentHandler.cs:372): a
``0x02`` dungeon-mode flag followed by the uint32 SynchHP. The tick loop's
0x36 HP-sync and 0x65 self-echo use the same ``0x02 + hp_wire`` trailer.
"""
from __future__ import annotations

from ..util.byte_io import LEWriter

# Fallback HP (200 HP in 256× fixed-point) when a connection has no hp_wire yet.
_DEFAULT_HP_WIRE = 200 * 256


def write_synch(writer: LEWriter, hp_wire: int) -> None:
    """Append the per-ComponentUpdate synch trailer for an **HP-bearing** entity
    (the avatar): ``0x02`` (HP-present flag) + uint32 SynchHP.

    Use ONLY for components owned by the avatar (Equipment, Manipulators,
    Modifiers, UnitContainer, Skills…). For components owned by the **player
    object** (QuestManager, DialogManager) the player object is not an HP unit,
    so its synch must be flags-only :func:`write_synch_none` — see its docstring
    for the zero-tolerance-compare reason.
    """
    writer.write_byte(0x02)
    writer.write_uint32(hp_wire if hp_wire else _DEFAULT_HP_WIRE)


def write_synch_none(writer: LEWriter) -> None:
    """Append a flags-only ``0x00`` synch trailer — NO HP — for a ComponentUpdate
    on an entity the client does **not** treat as an HP unit (the Player object:
    QuestManager / DialogManager).

    The synch byte is still mandatory (the client's ``ReadComponentUpdate`` always
    consumes one flags byte after each sub-message; omitting it desyncs the
    stream). But asserting HP here is fatal on an unpatched client: the
    zero-tolerance compare ``FUN_005dd900`` derives the entity's *local* flags
    from its "has-HP-component" check (client ``FUN_004FA200``) — the Player
    object fails it, so local flags = ``0x00``. A server ``0x02`` trailer then
    mismatches (``cmp bl,[server flags]`` → "Entity synch error" → crash
    ``0xc000013a``). x64dbg-proven 2026-06-13: the fresh-character tutorial
    desync was exactly this — a QuestManager (player-object cid 536) available-
    quest update carrying ``0x02`` + HP 300 against the player object's local
    flags 0. Flags ``0x00`` makes both sides agree (``0==0``) and the compare
    passes. (bible.md §4; the patched client used to mask this.)
    """
    writer.write_byte(0x00)


def synch_hp(conn) -> int:  # noqa: ANN001 — RRConnection, avoid import cycle
    """Resolve the SynchHP to ship for this connection's owner-component updates.

    Mirrors the tick heartbeat (``movement._heartbeat_hp``): never carry an HP
    above the value the client last self-reported, or the zero-tolerance avatar
    synch compare (``FUN_005dd900``) crashes on dungeon zones.
    """
    hp = getattr(conn, "hp_wire", 0) or _DEFAULT_HP_WIRE
    client_hp = getattr(conn, "client_hp_wire", None)
    if client_hp is not None and 0 < client_hp < hp:
        return client_hp
    return hp
