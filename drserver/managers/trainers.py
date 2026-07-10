"""Skill trainer NPCs — SkillTrainer component + learn/upgrade purchases.

Port of the DRS-NET trainer wire (GameServer.ClientEntityManager.cs SkillTrainer
component write + GameServer.Combat.cs HandleSkillTrainRequest).

The trainer NPC's *skill list* is client-authored content (the
``SkillTrainer extends SkillTrainer`` block with ``AvailableSkill`` entries in
``extracter/world/town/npc/Base/Trainer*Base.gc``) — the server only ships a
component REFERENCE in the cached NPC spawn stream:

    0x32 · u16 npcEntityId · u16 trainerCid ·
    GCType "<prefix>.base.<Name>Base.SkillTrainer" (preserve case) · 0x00

When the player buys a skill the client sends a ComponentUpdate on that
trainer cid: ``u32 playerEntityId · u8 refType · u32 djb2(skill gc type)``
(+ trailing EntitySynchInfo). The server resolves the hash against the authored
``skills`` table, prices the rank from the authored fields, deducts gold,
persists, and answers with a skills-component gold (0x33) + skill-level (0x32)
update plus the UnitContainer 0x20 gold delta.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional

from ..core import log
from ..data.gc_object import hash_djb2, write_gc_type
from ..db import character_repository, game_database as db
from ..net.component_update import synch_hp, write_synch
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from ..net.connection import RRConnection
    from ..net.game_server import GameServer

# C# SKILL_VALUE_PER_LEVEL — GCDatabase knob "SkillValuePerLevel" (client
# Tables.gc), default 1113.621.
SKILL_VALUE_PER_LEVEL = 1113.621

_TRAINER_TOKENS = ("trainerfighter", "trainermage", "trainerranger")

# Trainer NPC → the class whose skill list it carries (informational — any
# class may train at any trainer; only class passives are class-locked).
_TRAINER_CLASS = {
    "trainerfighter": "Fighter",
    "trainermage": "Mage",
    "trainerranger": "Ranger",
}

# djb2(lower gc_type) → gc_type for every authored skill (lazy; the C#
# _skillHashToGcClass table — our hash_djb2 reproduces its values exactly,
# e.g. 0x997C6A0A = skills.generic.FighterClassPassive).
_skill_hash_to_gc: Dict[int, str] = {}


def is_trainer(npc_gc_type: str) -> bool:
    low = (npc_gc_type or "").lower()
    return any(token in low for token in _TRAINER_TOKENS)


def trainer_class(npc_gc_type: str) -> Optional[str]:
    """The class a trainer NPC teaches ("Fighter"/"Mage"/"Ranger"), or None."""
    low = (npc_gc_type or "").lower()
    for token, cls in _TRAINER_CLASS.items():
        if token in low:
            return cls
    return None


# Authored ProfessionType → player class for the passive gate. NONE (the five
# resist passives) stays unrestricted; SUMMONER spans Mage (snowman line) and
# Ranger (monster bait) trainers, so it is not class-locked either.
_PROFESSION_CLASS = {"FIGHTER": "Fighter", "MAGE": "Mage", "RANGER": "Ranger"}

_passive_owner_cache: Dict[str, Optional[str]] = {}


def passive_owner_class(skill_gc: str) -> Optional[str]:
    """The class a passive is exclusive to, or None (shared/unknown).

    Resolution: the three ``<Class>ClassPassive`` ids map directly; every other
    passive uses its authored ``ProfessionType`` from the ``skills`` table
    (client ground truth — e.g. MeleeAttackSpeedModPassive=FIGHTER,
    MagicDamageModPassive=MAGE, DivineResistPassive=NONE→shared)."""
    from ..data import class_passives
    key = (skill_gc or "").lower()
    if key in _passive_owner_cache:
        return _passive_owner_cache[key]
    owner: Optional[str] = None
    for cls, passive in class_passives.CLASS_PASSIVES.items():
        if passive.passive_skill_id.lower() == key:
            owner = cls
            break
    if owner is None:
        try:
            row = db.execute_reader(
                "SELECT profession_type FROM skills WHERE gc_type = :k COLLATE NOCASE",
                {"k": skill_gc}).fetchone()
            if row is not None:
                owner = _PROFESSION_CLASS.get((row[0] or "").strip().upper())
        except Exception as ex:  # noqa: BLE001 — unresolvable = unrestricted
            log.warn(f"[TRAINER] profession lookup failed for {skill_gc}: {ex}")
    _passive_owner_cache[key] = owner
    return owner


def can_train(skill_gc: str, player_class: str, trainer_npc_gc: str) -> tuple:
    """Class gate for a train purchase — (allowed, denial message).

    Any class may learn from any trainer; the ONLY restriction is that
    PASSIVE skills with an authored class profession are exclusive to that
    class (the skill id arrives as a client-supplied hash, so any trainer
    could carry any passive). Shared passives (ProfessionType NONE, e.g. the
    resist passives) stay open to everyone."""
    from ..data import class_passives
    if class_passives.is_passive_skill(skill_gc):
        owner = passive_owner_class(skill_gc)
        if owner is not None and (player_class or "") != owner:
            return False, f"That passive is exclusive to the {owner} class."
    return True, ""


def skill_trainer_gc_type(npc_gc_type: str) -> str:
    """``world.town.npc.TrainerFighter`` →
    ``world.town.npc.base.TrainerFighterBase.SkillTrainer`` (C# verbatim)."""
    prefix, _, name = npc_gc_type.rpartition(".")
    return f"{prefix}.base.{name}Base.SkillTrainer"


def write_skill_trainer_component(w: LEWriter, npc_gc_type: str,
                                  npc_entity_id: int, trainer_cid: int) -> None:
    """The SkillTrainer component block inside the cached NPC create stream."""
    w.write_byte(0x32)
    w.write_uint16(npc_entity_id)
    w.write_uint16(trainer_cid)
    write_gc_type(w, skill_trainer_gc_type(npc_gc_type), preserve_case=True)
    w.write_byte(0x00)


def _skill_hash_table() -> Dict[int, str]:
    if not _skill_hash_to_gc:
        try:
            for row in db.execute_reader("SELECT gc_type FROM skills"):
                gc = row[0]
                if gc:
                    _skill_hash_to_gc[hash_djb2(gc)] = gc
        except Exception as ex:  # noqa: BLE001
            log.error(f"[TRAINER] skill hash table load failed: {ex}")
    return _skill_hash_to_gc


def _authored_train_data(skill_gc: str) -> Optional[tuple]:
    """(gold_value_mod, required_level, max_skill_level) from the authored
    skills table, or None when the skill has no usable train pricing."""
    row = db.execute_reader(
        "SELECT gold_value_mod, required_level, max_skill_level FROM skills"
        " WHERE gc_type = :k COLLATE NOCASE", {"k": skill_gc}).fetchone()
    if row is None:
        return None
    gold_value_mod = float(row[0] or 0.0)
    required_level = int(row[1] or 0)
    max_skill_level = int(row[2] or 0)
    if gold_value_mod <= 0.0 or required_level <= 0 or max_skill_level <= 0:
        return None
    return gold_value_mod, required_level, max_skill_level


def train_gold_cost(required_level: int, next_level: int, gold_value_mod: float) -> int:
    """C# goldCost = (requiredLevel + (nextLevel-1)·gvm) · SkillValuePerLevel · gvm."""
    cost = int((required_level + (next_level - 1) * gold_value_mod)
               * SKILL_VALUE_PER_LEVEL * gold_value_mod)
    return max(1, cost)


def handle_train_request(server: "GameServer", conn: "RRConnection",
                         component_id: int, reader) -> bool:
    """A ComponentUpdate on a registered trainer cid — learn / rank up a skill.
    Port of C# HandleSkillTrainRequest. Always returns True (consumes)."""
    if reader.remaining < 9:
        log.warn(f"[TRAINER] short request ({reader.remaining} bytes, need 9)")
        _drain(reader)
        return True
    player_entity_id = reader.read_uint32()
    ref_type = reader.read_byte()
    skill_hash = reader.read_uint32()
    _drain(reader)  # trailing EntitySynchInfo

    skill_gc = _skill_hash_table().get(skill_hash)
    if skill_gc is None:
        log.warn(f"[TRAINER] unknown skill hash 0x{skill_hash:08X} "
                 f"(eid=0x{player_entity_id:X} ref=0x{ref_type:02X})")
        return True

    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        log.error(f"[TRAINER] no character for '{conn.login_name}'")
        return True

    # Class gate: own-class trainer only + class passives are class-exclusive.
    trainer_npc_gc = getattr(server, "trainer_components", {}).get(component_id, "")
    allowed, denial = can_train(skill_gc, saved.class_name, trainer_npc_gc)
    if not allowed:
        conn.send_system_message(denial)
        log.info(f"[TRAINER] denied '{conn.login_name}' ({saved.class_name}) "
                 f"{skill_gc} at {trainer_npc_gc}: {denial}")
        return True

    has_skill = any(s.lower() == skill_gc.lower() for s in (saved.skills or []))
    current_level = saved.get_skill_level(skill_gc) if has_skill else 0
    next_level = current_level + 1 if has_skill else 1

    authored = _authored_train_data(skill_gc)
    if authored is None:
        log.warn(f"[TRAINER] no authored train data for {skill_gc}")
        return True
    gold_value_mod, required_level, max_skill_level = authored

    if next_level > max_skill_level:
        conn.send_system_message("That skill is already at its maximum rank.")
        return True

    cost = train_gold_cost(required_level, next_level, gold_value_mod)
    if saved.gold < cost:
        conn.send_system_message(f"Not enough gold — that costs {cost} gold.")
        return True

    saved.gold -= cost
    saved.set_skill_level(skill_gc, next_level)
    if not has_skill:
        saved.skills.append(skill_gc)
    character_repository.save_character(saved)

    _send_train_response(conn, skill_gc, next_level, saved.gold, cost)

    # A newly learned ACTIVE skill becomes a Manipulators child server-side so
    # the next spawn/zone-change ships it (the client adds its own local copy
    # now); passives ship from saved data (data.class_passives).
    from ..data import class_passives
    if not has_skill and not class_passives.is_passive_skill(skill_gc):
        _add_active_to_manipulators(server, conn, skill_gc)

    short = skill_gc.rsplit(".", 1)[-1]
    conn.send_system_message(
        f"{short} -> Rank {next_level}! ({saved.gold} gold)" if has_skill
        else f"Learned {short}! ({saved.gold} gold)")
    log.info(f"[TRAINER] '{conn.login_name}' trained {skill_gc} -> Lv{next_level} "
             f"for {cost}g (gold left {saved.gold})")
    return True


def _send_train_response(conn: "RRConnection", skill_gc: str, next_level: int,
                         gold_after: int, cost: int) -> None:
    """Skills-component gold (0x33) + skill level (0x32) updates in one stream,
    then the UnitContainer 0x20 gold delta (C# TRAINER-COMBINED + TRAINER-GOLD)."""
    skills_cid = getattr(conn, "skills_component_id", 0)
    if skills_cid:
        w = LEWriter()
        w.write_byte(0x07)
        w.write_byte(0x35)
        w.write_uint16(skills_cid)
        w.write_byte(0x33)                       # gold snapshot
        w.write_uint32(gold_after & 0xFFFFFFFF)
        write_synch(w, synch_hp(conn))
        w.write_byte(0x35)
        w.write_uint16(skills_cid)
        w.write_byte(0x32)                       # skill level set
        w.write_byte(0xFF)
        w.write_cstring(skill_gc)
        w.write_byte(next_level & 0xFF)
        write_synch(w, synch_hp(conn))
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())
    else:
        log.warn(f"[TRAINER] skills_component_id=0 for '{conn.login_name}'")
    if conn.unit_container_id:
        w = LEWriter()
        w.write_byte(0x07)
        w.write_byte(0x35)
        w.write_uint16(conn.unit_container_id)
        w.write_byte(0x20)                       # AddCurrency (negative)
        w.write_int32(-cost)
        w.write_byte(0x00)                       # CurrencySource
        w.write_uint32(0x00000000)               # entityHandle
        w.write_byte(0x01)                       # notifyFlag
        write_synch(w, synch_hp(conn))
        w.write_byte(0x06)
        conn.send_to_client(w.to_array())


def _add_active_to_manipulators(server: "GameServer", conn: "RRConnection",
                                skill_gc: str) -> None:
    from ..data.gc_object import GCObject
    avatar = getattr(conn, "avatar", None)
    manip = None
    for child in (avatar.children if avatar else []):
        if child.gc_class == "Manipulators":
            manip = child
            break
    if manip is None:
        return
    manip.add_child(GCObject(native_class="ActiveSkill", gc_class=skill_gc, name=""))
    # Register a manipulator id past the hotbar range so 0x52 self-casts and
    # hotbar placement can resolve the new skill this session (C# manipMap).
    manip_map = getattr(conn, "skill_manip_map", None)
    if isinstance(manip_map, dict):
        next_id = 200
        for existing in manip_map:
            if existing >= next_id:
                next_id = existing + 1
        manip_map[next_id] = skill_gc


def _drain(reader) -> None:
    while reader.remaining > 0:
        reader.read_byte()
