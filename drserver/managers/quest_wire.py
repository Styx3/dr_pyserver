"""Quest wire-packet builders — QuestManager ComponentUpdate stream.

Port of the C# ``QuestManager`` packet senders (Managers/QuestManager.cs). Every
quest packet is a ComponentUpdate addressed to the player's per-session
QuestManager component id (``conn.quest_manager_id``):

    0x07 BeginStream · 0x35 ComponentUpdate · uint16 questManagerId · <submsg>
      ... · <player entity synch trailer> · 0x06 EndStream

QM submessages (the byte after the component id):
    0x01 AddQuest       0x02 RemoveQuest     0x03 UpdateQuest progress
    0x04 QueryQuest     0x06 QueryComplete   0x07 AvailableQuestUpdate
    0x08 FinalizeQuest

The objective wire format used by Add / Progress / the spawn QM-component block is
identical: ``[count:u8]`` then per objective ``[flags:u8][label:cstr][required:u16]``,
where ``flags = 0x02 | (complete ? 0x01 : 0)`` and ``label`` is the HUD string
``"<Label>: <current> / <required>"``. The client crashes on a zero-objective Add,
so a single pre-completed "Read" placeholder is synthesised for info quests.

These functions take a lightweight runtime quest (duck-typed: ``.quest_id``,
``.instance_id``, ``.objectives`` with ``.label/.current/.required/.is_complete``)
so the manager and the spawn writer share one set of builders.
"""
from __future__ import annotations

from typing import Dict, List, TYPE_CHECKING

from ..data.gc_object import hash_djb2, write_gc_type
from ..net.component_update import write_synch_none
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from ..net.connection import RRConnection
    from .quests import RuntimeObjective, RuntimeQuest


def quest_hash(quest_id: str) -> int:
    """DJB2 hash of the quest gc_type — the client's on-wire quest identity.

    Matches C# ``DatabaseLoader.ComputeDJB2Hash`` (``h*33`` over the lowercased
    string), which is exactly ``data.gc_object.hash_djb2``.
    """
    return hash_djb2(quest_id)


def _objective_label(obj: "RuntimeObjective") -> str:
    required = obj.required if obj.required > 0 else 1
    return f"{obj.label or 'Objective'}: {obj.current} / {required}"


def _write_objectives(w: LEWriter, objectives: List["RuntimeObjective"]) -> None:
    """``[count:u8]`` then per objective ``[flags][label:cstr][required:u16]``."""
    w.write_byte(len(objectives) & 0xFF)
    for obj in objectives:
        flags = 0x02 | (0x01 if obj.is_complete else 0x00)
        w.write_byte(flags)
        w.write_cstring(_objective_label(obj))
        w.write_uint16((obj.required if obj.required > 0 else 1) & 0xFFFF)


def _begin(conn: "RRConnection", submsg: int) -> LEWriter:
    w = LEWriter()
    w.write_byte(0x07)                       # BeginStream
    w.write_byte(0x35)                       # ComponentUpdate
    w.write_uint16(conn.quest_manager_id)
    w.write_byte(submsg)
    return w


def _finish(conn: "RRConnection", w: LEWriter) -> None:
    # The QuestManager lives on the PLAYER OBJECT (spawn.py: player.add_child),
    # which is NOT an HP unit — its synch trailer must be flags-only 0x00, never
    # 0x02+HP. Asserting HP here crashes an unpatched client's zero-tolerance
    # compare (x64dbg-proven 2026-06-13 fresh-char tutorial desync; see
    # write_synch_none / bible.md §4).
    write_synch_none(w)                        # player-object: no HP
    w.write_byte(0x06)                        # EndStream
    conn.send_to_client(w.to_array())


# ── 0x01 AddQuest ────────────────────────────────────────────────────────────────

def send_add_packet(conn: "RRConnection", quest: "RuntimeQuest") -> None:
    """Add a quest to the client's journal — C# ``SendAddPacket``."""
    w = _begin(conn, 0x01)
    w.write_byte(0x04)                       # GCType indicator: hash follows
    w.write_uint32(quest_hash(quest.quest_id))
    w.write_uint32(quest.instance_id)

    objectives = list(quest.objectives)
    if not objectives:
        # Zero-objective info quest: synthesise a pre-completed placeholder so the
        # client (which crashes on 0 objectives) shows it turn-in-ready.
        from .quests import RuntimeObjective
        objectives = [RuntimeObjective(label="Read", required=1, current=1)]

    all_complete = all(o.is_complete for o in objectives)
    w.write_byte(0x01 if all_complete else 0x00)
    _write_objectives(w, objectives)
    _finish(conn, w)


# ── 0x02 RemoveQuest ──────────────────────────────────────────────────────────────

def send_remove_packet(conn: "RRConnection", instance_id: int) -> None:
    """Remove a quest from the client's journal — C# ``SendRemovePacket``."""
    w = _begin(conn, 0x02)
    w.write_uint32(instance_id)
    _finish(conn, w)


# ── 0x03 UpdateQuest (objective progress + complete flag) ─────────────────────────

def send_progress_packet(conn: "RRConnection", quest: "RuntimeQuest") -> None:
    """Two packets — objective list then the all-complete flag (C# ``SendProgressPacket``)."""
    objectives = list(quest.objectives)
    all_complete = all(o.is_complete for o in objectives)

    # Packet 1: questSubmsg 0x01 = readObjectives.
    w1 = _begin(conn, 0x03)
    w1.write_uint32(quest.instance_id)
    w1.write_byte(0x01)
    _write_objectives(w1, objectives)
    _finish(conn, w1)

    # Packet 2: questSubmsg 0x00 = set complete flag (NPC icon → yellow ?).
    w2 = _begin(conn, 0x03)
    w2.write_uint32(quest.instance_id)
    w2.write_byte(0x00)
    w2.write_byte(0x01 if all_complete else 0x00)
    _finish(conn, w2)


# ── 0x06 QueryComplete / 0x04 QueryAccept ─────────────────────────────────────────

def send_complete_packet(conn: "RRConnection", instance_id: int) -> None:
    """0x06 quest-complete notification — C# ``SendCompletePacket``."""
    w = _begin(conn, 0x06)
    w.write_uint32(instance_id)
    _finish(conn, w)


def send_turn_in_dialog(conn: "RRConnection", instance_id: int) -> None:
    """Show the turn-in dialog (submsg 0x06, reads the quest instanceId) —
    C# ``SendQueryResponse`` turn-in branch / ``SendTurnInDialog``."""
    conn.pending_turn_in_instance_id = instance_id
    conn.pending_quest_hash = 0
    w = _begin(conn, 0x06)
    w.write_uint32(instance_id)
    _finish(conn, w)


def send_accept_dialog(conn: "RRConnection", quest_hash_value: int) -> None:
    """Show the accept dialog (submsg 0x04 + 0x04 + hash) — C# ``SendQueryResponse``
    new-quest branch."""
    w = _begin(conn, 0x04)
    w.write_byte(0x04)                       # GCType indicator: hash follows
    w.write_uint32(quest_hash_value)
    _finish(conn, w)


# ── 0x08 FinalizeQuest ────────────────────────────────────────────────────────────

def send_finalize_packet(conn: "RRConnection", instance_id: int) -> None:
    """0x08 finalize — C# ``SendFinalizePacket``."""
    w = _begin(conn, 0x08)
    w.write_uint32(instance_id)
    _finish(conn, w)


# ── 0x07 AvailableQuestUpdate (NPC "!" markers) ───────────────────────────────────

def send_available_quest_update(conn: "RRConnection",
                                npc_to_hashes: Dict[str, List[int]]) -> None:
    """Tell the client which NPCs offer which quests (the ``!`` markers) —
    C# ``SendAvailableQuestUpdateForZone``. ``npc_to_hashes`` maps an NPC gc_type
    to the list of acceptable quest hashes for it."""
    w = _begin(conn, 0x07)
    w.write_byte(len(npc_to_hashes) & 0xFF)
    for npc_gc_type, hashes in npc_to_hashes.items():
        for ch in npc_gc_type:
            w.write_byte(ord(ch) & 0xFF)
        w.write_byte(0x00)                   # null terminator
        w.write_byte(len(hashes) & 0xFF)
        for h in hashes:
            w.write_byte(0x04)               # GCType indicator
            w.write_uint32(h)
    _finish(conn, w)


# ── Spawn QM-component active-quest block ─────────────────────────────────────────

def write_quest_entries(w: LEWriter, active_quests: List["RuntimeQuest"]) -> None:
    """The active-quest block inside ``WriteQuestManagerComponent`` — ``[count:u16]``
    then per quest ``writeGcType(quest_id)`` + ``instanceId:u32`` + ``allDone:u8`` +
    objectives. Written into the spawn stream, not standalone (no framing/synch)."""
    w.write_uint16(len(active_quests) & 0xFFFF)
    for quest in active_quests:
        write_gc_type(w, quest.quest_id, preserve_case=True)
        w.write_uint32(quest.instance_id)
        all_done = all(o.is_complete for o in quest.objectives)
        w.write_byte(0x01 if all_done else 0x00)
        _write_objectives(w, list(quest.objectives))
