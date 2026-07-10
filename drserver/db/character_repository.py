"""Character CRUD backed by SQLite.

Ported from C# Database/CharacterRepository.cs. Loads/saves SavedCharacter and
its sub-tables (equipment, inventory, skills, hotbar, quests, checkpoints). The
equipment/inventory row validators are preserved verbatim — a corrupt row can
produce avatar DFC bytes that trip a fatal client assert during login, so bad
rows are clamped or skipped before they reach the serializer (the DB row is not
modified).

Multi-write operations (create/save) run inside a transaction (``with conn:``)
so they commit atomically; reads use the shared connection directly.
"""
from __future__ import annotations

from typing import List, Optional

from ..core import log
from ..data import class_config
from ..data.character import Vector3
from ..data import class_passives
from ..data.saved_character import (
    HotbarSlotEntry,
    SavedCharacter,
    SavedInventoryItem,
    SavedQuest,
    SavedQuestObjective,
    SkillLevelEntry,
    StartingEquipment,
)
from . import game_database as db

_EQUIP_SLOTS = ("weapon", "armor", "helmet", "gloves", "boots",
                "shoulders", "shield", "ring1", "ring2", "amulet")


# ─────────────────────────────── CREATE ───────────────────────────────
def create_character(name: str, class_name: str, account_id: int,
                     account_name: str, avatar_class: str = "", *,
                     skin: int = 0, face: int = 0, face_feature: int = 0,
                     hair: int = 0, hair_color: int = 0) -> Optional[SavedCharacter]:
    if character_name_exists(name):
        log.error(f"[DB-CHAR] name '{name}' already taken")
        return None
    class_def = class_config.get_class_definition(class_name)
    if class_def is None:
        log.error(f"[DB-CHAR] invalid class: {class_name}")
        return None
    try:
        conn = db.get_connection()
        with conn:
            conn.execute(
                # Stats stored here are ALLOCATED points only (0 at creation). The
                # client supplies each class's base (10/10/10/10 from *Base.gc) and
                # adds the wire value on top, so sending 10 would display as 20.
                "INSERT INTO characters (account_id, name, class_name, avatar_class, level, experience, gold,"
                " current_zone, stat_strength, stat_agility, stat_intellect, stat_endurance,"
                " skin, face, face_feature, hair, hair_color)"
                " VALUES (:aid, :name, :cls, :av, 1, 0, 100, 'tutorial', 0, 0, 0, 0,"
                " :sk, :fc, :ff, :hr, :hc)",
                {"aid": account_id, "name": name, "cls": class_name, "av": avatar_class,
                 "sk": skin, "fc": face, "ff": face_feature, "hr": hair, "hc": hair_color})
            char_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

            eq = class_def.starting_equipment
            for slot, gc in (("weapon", eq.weapon), ("armor", eq.armor), ("helmet", eq.helmet),
                             ("gloves", eq.gloves), ("boots", eq.boots), ("shoulders", eq.shoulders or ""),
                             ("shield", eq.shield or ""), ("ring1", eq.ring1 or ""),
                             ("ring2", eq.ring2 or ""), ("amulet", eq.amulet or "")):
                _insert_equipment(conn, char_id, slot, gc)

            # Persist each starting skill with its authored hotbar ID (client
            # *StartingSkills.gc: actives 100/105, passives 108/109) so spawn's
            # OP4 manipulator slots match the client's loadout.
            for skill in class_def.starting_skills or []:
                conn.execute(
                    "INSERT INTO character_skills (character_id, skill_gc_class, level, hotbar_slot)"
                    " VALUES (:cid, :s, 1, :h)",
                    {"cid": char_id, "s": skill,
                     "h": class_passives.starting_hotbar_slot(skill)})
            for item in class_def.starting_inventory or []:
                conn.execute(
                    "INSERT INTO character_inventory (character_id, gc_class, slot_x, slot_y, count)"
                    " VALUES (:cid, :gc, :x, :y, :qty)",
                    {"cid": char_id, "gc": item.gc_class, "x": item.x, "y": item.y,
                     "qty": getattr(item, "quantity", 1)})

            # Bling Gnome skill book + learned skill for every new character.
            conn.execute(
                "INSERT INTO character_inventory (character_id, gc_class, slot_x, slot_y, count, rarity, stored_level)"
                " VALUES (:cid, :gc, 0, 2, 1, 5, 1)",
                {"cid": char_id, "gc": "SkillBookPAL.SummonBlingGnome"})
            conn.execute("INSERT INTO character_skills (character_id, skill_gc_class, level) VALUES (:cid, :s, 1)",
                         {"cid": char_id, "s": "skills.generic.SummonBlingGnome"})
        log.info(f"[DB-CHAR] created '{name}' (id {char_id}) class={class_name} account={account_id}")
        return get_character(char_id)
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-CHAR] create error: {ex}")
        return None


# ──────────────────────────────── READ ────────────────────────────────
def get_character(character_id: int) -> Optional[SavedCharacter]:
    try:
        row = db.execute_reader("SELECT * FROM characters WHERE id = :id", {"id": character_id}).fetchone()
        if row is None:
            return None
        ch = _read_character_row(row)
        ch.equipment = _load_equipment(character_id)
        ch.skills = _load_skill_list(character_id)
        ch.skill_levels = _load_skill_levels(character_id)
        ch.hotbar_slots = _load_hotbar_slots(character_id)
        ch.inventory = _load_inventory(character_id)
        ch.active_quests = _load_active_quests(character_id)
        ch.completed_quests = _load_completed_quests(character_id)
        ch.unlocked_checkpoints = _load_checkpoints(character_id)
        return ch
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-CHAR] get_character error: {ex}")
        return None


def get_character_by_name(account_name: str, character_name: str) -> Optional[SavedCharacter]:
    try:
        cid = db.execute_scalar("SELECT id FROM characters WHERE name = :n", {"n": character_name})
        return get_character(int(cid)) if cid is not None else None
    except Exception:  # noqa: BLE001
        return None


def get_characters_for_account(account_name: str) -> List[SavedCharacter]:
    from . import account_repository as accounts
    result: List[SavedCharacter] = []
    try:
        account_id = accounts.get_account_id(account_name)
        if account_id == 0:
            return result
        ids = [r[0] for r in db.execute_reader(
            "SELECT id FROM characters WHERE account_id = :aid", {"aid": account_id})]
        for cid in ids:
            ch = get_character(int(cid))
            if ch is not None:
                result.append(ch)
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-CHAR] get_for_account error: {ex}")
    return result


def character_name_exists(name: str) -> bool:
    try:
        count = db.execute_scalar("SELECT COUNT(*) FROM characters WHERE name = :n", {"n": name})
        return int(count or 0) > 0
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────── UPDATE ───────────────────────────────
def save_character(ch: SavedCharacter) -> None:
    try:
        conn = db.get_connection()
        with conn:
            conn.execute(
                "UPDATE characters SET level=:lv, experience=:xp, gold=:g, avatar_class=:ac,"
                " skin=:sk, face=:fc, face_feature=:ff, hair=:hr, hair_color=:hc,"
                " current_zone=:zone, zone_id=:zid, position_x=:px, position_y=:py, position_z=:pz,"
                " current_hp=:hp, current_mana=:mp, max_hp=:mxhp, max_mana=:mxmp,"
                " stat_strength=:str, stat_agility=:agi, stat_intellect=:int, stat_endurance=:end,"
                " last_respec_time=:lrt, respec_count=:rsc, pvp_wins=:pvpw, pvp_rating=:pvpr,"
                " tp_zone=:tpz, tp_zone_id=:tpzid, tp_target_zone=:tptz,"
                " tp_pos_x=:tppx, tp_pos_y=:tppy, tp_pos_z=:tppz WHERE id=:id",
                {"id": ch.id, "lv": ch.level, "xp": ch.experience, "g": ch.gold, "ac": ch.avatar_class or "",
                 "sk": ch.skin, "fc": ch.face, "ff": ch.face_feature, "hr": ch.hair, "hc": ch.hair_color,
                 "zone": ch.current_zone_name or "tutorial", "zid": ch.zone_id,
                 "px": ch.position.x, "py": ch.position.y, "pz": ch.position.z,
                 "hp": ch.current_hp, "mp": ch.current_mana, "mxhp": ch.max_hp, "mxmp": ch.max_mana,
                 "str": ch.stat_strength, "agi": ch.stat_agility, "int": ch.stat_intellect, "end": ch.stat_endurance,
                 "lrt": ch.last_respec_time, "rsc": ch.respec_count, "pvpw": ch.pvp_wins, "pvpr": ch.pvp_rating,
                 "tpz": ch.tp_zone or "", "tpzid": ch.tp_zone_id, "tptz": ch.tp_target_zone or "",
                 "tppx": ch.tp_pos_x, "tppy": ch.tp_pos_y, "tppz": ch.tp_pos_z})

            # Equipment.
            conn.execute("DELETE FROM character_equipment WHERE character_id = :cid", {"cid": ch.id})
            if ch.equipment is not None:
                eq = ch.equipment
                sr, sl = eq.slot_rarity or {}, eq.slot_level or {}
                sm = eq.slot_scale_mod or {}
                mr = eq.slot_mod_refs or {}
                for slot, gc in (("weapon", eq.weapon), ("armor", eq.armor), ("helmet", eq.helmet),
                                 ("gloves", eq.gloves), ("boots", eq.boots), ("shoulders", eq.shoulders or ""),
                                 ("shield", eq.shield or ""), ("ring1", eq.ring1 or ""),
                                 ("ring2", eq.ring2 or ""), ("amulet", eq.amulet or "")):
                    _insert_equipment(conn, ch.id, slot, gc, sr.get(slot, -1), sl.get(slot, -1),
                                      sm.get(slot, ""), mr.get(slot) or [])

            # Inventory.
            conn.execute("DELETE FROM character_inventory WHERE character_id = :cid", {"cid": ch.id})
            for item in ch.inventory or []:
                conn.execute(
                    "INSERT INTO character_inventory (character_id, gc_class, slot_x, slot_y, count, buy_price, rarity, stored_level, scale_mod, mod_refs)"
                    " VALUES (:cid, :gc, :x, :y, :c, :bp, :r, :sl, :sm, :mr)",
                    {"cid": ch.id, "gc": item.gc_class, "x": item.x, "y": item.y, "c": item.count,
                     "bp": item.buy_price, "r": item.rarity, "sl": item.stored_level,
                     "sm": item.scale_mod or "",
                     "mr": ",".join(item.mod_refs or [])})

            # Skills (+ hotbar slot).
            conn.execute("DELETE FROM character_skills WHERE character_id = :cid", {"cid": ch.id})
            for skill in ch.skills or []:
                level = ch.get_skill_level(skill)
                hotbar = -1
                for hs in ch.hotbar_slots or []:
                    if hs.skill == skill:
                        hotbar = hs.slot
                        break
                conn.execute(
                    "INSERT INTO character_skills (character_id, skill_gc_class, level, hotbar_slot) VALUES (:cid, :s, :l, :h)",
                    {"cid": ch.id, "s": skill, "l": level, "h": hotbar})

            # Active quests + objectives.
            conn.execute("DELETE FROM character_quests WHERE character_id = :cid AND status = 'active'", {"cid": ch.id})
            conn.execute("DELETE FROM quest_objectives WHERE character_id = :cid", {"cid": ch.id})
            for q in ch.active_quests or []:
                conn.execute(
                    "INSERT OR REPLACE INTO character_quests (character_id, quest_id, quest_giver_id, accepted_at, status)"
                    " VALUES (:cid, :qid, :gid, :at, 'active')",
                    {"cid": ch.id, "qid": q.quest_id, "gid": q.quest_giver_id or "", "at": q.accepted_at or ""})
                for obj in q.objectives or []:
                    conn.execute(
                        "INSERT INTO quest_objectives (character_id, quest_id, objective_name, type, target, label, required, current)"
                        " VALUES (:cid, :qid, :on, :t, :tgt, :lb, :req, :cur)",
                        {"cid": ch.id, "qid": q.quest_id, "on": obj.objective_name or "", "t": obj.type or "",
                         "tgt": obj.target or "", "lb": obj.label or "", "req": obj.required, "cur": obj.current})

            # Completed quests.
            conn.execute("DELETE FROM completed_quests WHERE character_id = :cid", {"cid": ch.id})
            for qid in ch.completed_quests or []:
                conn.execute("INSERT OR IGNORE INTO completed_quests (character_id, quest_id) VALUES (:cid, :qid)",
                             {"cid": ch.id, "qid": qid})

            # Checkpoints.
            conn.execute("DELETE FROM character_checkpoints WHERE character_id = :cid", {"cid": ch.id})
            for cp in ch.unlocked_checkpoints or []:
                conn.execute("INSERT OR IGNORE INTO character_checkpoints (character_id, checkpoint_id) VALUES (:cid, :cp)",
                             {"cid": ch.id, "cp": cp})
        log.info(f"[DB-CHAR] saved '{ch.name}' lv={ch.level} xp={ch.experience}")
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-CHAR] save_character error: {ex}")


# ─────────────────────────────── DELETE ───────────────────────────────
def delete_character(character_id: int) -> bool:
    try:
        db.execute_non_query("DELETE FROM characters WHERE id = :id", {"id": character_id})
        log.info(f"[DB-CHAR] deleted character id {character_id}")
        return True
    except Exception as ex:  # noqa: BLE001
        log.error(f"[DB-CHAR] delete error: {ex}")
        return False


# ─────────────────────── private row/sub-table loaders ───────────────────────
def _read_character_row(r) -> SavedCharacter:
    return SavedCharacter(
        id=db.get_int(r, "id"),
        account_id=db.get_int(r, "account_id"),
        name=db.get_string(r, "name"),
        class_name=db.get_string(r, "class_name", "Fighter"),
        avatar_class=db.get_string(r, "avatar_class"),
        level=db.get_int(r, "level", 1),
        experience=db.get_int(r, "experience"),
        gold=db.get_int(r, "gold", 100),
        skin=db.get_int(r, "skin"),
        face=db.get_int(r, "face"),
        face_feature=db.get_int(r, "face_feature"),
        hair=db.get_int(r, "hair"),
        hair_color=db.get_int(r, "hair_color"),
        zone_id=db.get_int(r, "zone_id"),
        current_zone_name=db.get_string(r, "current_zone", "tutorial"),
        position=Vector3(db.get_float(r, "position_x"), db.get_float(r, "position_y"), db.get_float(r, "position_z")),
        current_hp=db.get_int(r, "current_hp"),
        current_mana=db.get_int(r, "current_mana"),
        tp_zone=db.get_string(r, "tp_zone", ""),
        tp_zone_id=db.get_int(r, "tp_zone_id"),
        tp_target_zone=db.get_string(r, "tp_target_zone", ""),
        tp_pos_x=db.get_float(r, "tp_pos_x"),
        tp_pos_y=db.get_float(r, "tp_pos_y"),
        tp_pos_z=db.get_float(r, "tp_pos_z"),
        stat_strength=db.get_int(r, "stat_strength"),
        stat_agility=db.get_int(r, "stat_agility"),
        stat_intellect=db.get_int(r, "stat_intellect"),
        stat_endurance=db.get_int(r, "stat_endurance"),
        last_respec_time=db.get_int(r, "last_respec_time"),
        respec_count=db.get_int(r, "respec_count"),
        pvp_wins=db.get_int(r, "pvp_wins"),
        pvp_rating=db.get_int(r, "pvp_rating"),
    )


def _insert_equipment(conn, char_id: int, slot: str, gc_class: Optional[str],
                      rarity: int = -1, stored_level: int = -1,
                      scale_mod: str = "", mod_refs: Optional[List[str]] = None) -> None:
    conn.execute(
        "INSERT INTO character_equipment (character_id, slot, gc_class, rarity, stored_level, scale_mod, mod_refs)"
        " VALUES (:cid, :s, :gc, :r, :sl, :sm, :mr)",
        {"cid": char_id, "s": slot, "gc": gc_class or "", "r": rarity, "sl": stored_level,
         "sm": scale_mod or "", "mr": ",".join(mod_refs or [])})


def _load_equipment(char_id: int) -> StartingEquipment:
    equip = StartingEquipment()
    rows = db.execute_reader(
        "SELECT slot, gc_class, COALESCE(rarity, -1), COALESCE(stored_level, -1),"
        " COALESCE(scale_mod, ''), COALESCE(mod_refs, '')"
        " FROM character_equipment WHERE character_id = :cid", {"cid": char_id})
    for r in rows:
        slot, gc, rarity, stored_level, scale_mod = r[0], r[1], r[2], r[3], r[4]
        mod_refs = [m for m in (r[5] or "").split(",") if m]
        # Validate/clamp (see C# comment): an empty gc_class is a normal unequipped slot.
        if slot not in _EQUIP_SLOTS:
            log.error(f"[EQUIP-VALIDATOR] skipping unknown slot '{slot}' (char {char_id})")
            continue
        if rarity != -1 and not (0 <= rarity <= 5):
            log.error(f"[EQUIP-VALIDATOR] clamping rarity {rarity} (char {char_id} slot {slot})")
            rarity = -1
        if stored_level != -1 and not (1 <= stored_level <= 120):
            log.error(f"[EQUIP-VALIDATOR] clamping stored_level {stored_level} (char {char_id} slot {slot})")
            stored_level = -1
        equip.slot_rarity[slot] = rarity
        equip.slot_level[slot] = stored_level
        equip.slot_scale_mod[slot] = scale_mod or ""
        equip.slot_mod_refs[slot] = mod_refs
        setattr(equip, slot, gc)
    return equip


def _load_skill_list(char_id: int) -> List[str]:
    return [r[0] for r in db.execute_reader(
        "SELECT skill_gc_class FROM character_skills WHERE character_id = :cid", {"cid": char_id})]


def _load_skill_levels(char_id: int) -> List[SkillLevelEntry]:
    return [SkillLevelEntry(skill=r[0], level=r[1]) for r in db.execute_reader(
        "SELECT skill_gc_class, level FROM character_skills WHERE character_id = :cid", {"cid": char_id})]


def _load_hotbar_slots(char_id: int) -> List[HotbarSlotEntry]:
    return [HotbarSlotEntry(skill=r[0], slot=r[1]) for r in db.execute_reader(
        "SELECT skill_gc_class, hotbar_slot FROM character_skills WHERE character_id = :cid AND hotbar_slot >= 0",
        {"cid": char_id})]


def _load_inventory(char_id: int) -> List[SavedInventoryItem]:
    items: List[SavedInventoryItem] = []
    rows = db.execute_reader(
        "SELECT gc_class, slot_x, slot_y, count, COALESCE(buy_price, 0), COALESCE(rarity, -1), COALESCE(stored_level, -1),"
        " COALESCE(scale_mod, ''), COALESCE(mod_refs, '')"
        " FROM character_inventory WHERE character_id = :cid", {"cid": char_id})
    for r in rows:
        gc, sx, sy, count, buy_price, rarity, stored_level = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        scale_mod = r[7]
        mod_refs = [m for m in (r[8] or "").split(",") if m]
        # Validate/clamp (see C# comment): unrepairable rows are skipped.
        if not gc or not (0 <= sx <= 9) or not (0 <= sy <= 7):
            log.error(f"[INV-VALIDATOR] skipping unrepairable row char {char_id} slot ({sx},{sy}) gc '{gc}'")
            continue
        if count < 1:
            count = 1
        if rarity != -1 and not (0 <= rarity <= 5):
            rarity = -1
        if stored_level != -1 and not (1 <= stored_level <= 120):
            stored_level = -1
        items.append(SavedInventoryItem(gc_class=gc, x=sx, y=sy, count=count,
                                        buy_price=buy_price, rarity=rarity, stored_level=stored_level,
                                        scale_mod=scale_mod or "", mod_refs=mod_refs))
    return items


def _load_active_quests(char_id: int) -> List[SavedQuest]:
    quests: List[SavedQuest] = []
    for r in db.execute_reader(
            "SELECT quest_id, quest_giver_id, accepted_at FROM character_quests"
            " WHERE character_id = :cid AND status = 'active'", {"cid": char_id}):
        quests.append(SavedQuest(quest_id=r[0], quest_giver_id=r[1] or "", accepted_at=r[2] or ""))
    for q in quests:
        for r in db.execute_reader(
                "SELECT objective_name, type, target, label, required, current FROM quest_objectives"
                " WHERE character_id = :cid AND quest_id = :qid", {"cid": char_id, "qid": q.quest_id}):
            q.objectives.append(SavedQuestObjective(
                objective_name=r[0] or "", type=r[1] or "", target=r[2] or "",
                label=r[3] or "", required=r[4], current=r[5]))
    return quests


def _load_completed_quests(char_id: int) -> List[str]:
    return [r[0] for r in db.execute_reader(
        "SELECT quest_id FROM completed_quests WHERE character_id = :cid", {"cid": char_id})]


def load_checkpoints(char_id: int) -> List[str]:
    """Public: the GC ids (world.checkpoints.<Name>) this character has unlocked."""
    return _load_checkpoints(char_id)


def add_checkpoint(char_id: int, checkpoint_id: str) -> None:
    """Persist a single unlocked checkpoint without rewriting the whole save.

    Mirrors the C# unlock path (INSERT OR IGNORE into character_checkpoints).
    """
    if not char_id or not checkpoint_id:
        return
    db.execute_non_query(
        "INSERT OR IGNORE INTO character_checkpoints (character_id, checkpoint_id) "
        "VALUES (:cid, :cp)",
        {"cid": char_id, "cp": checkpoint_id},
    )


def _load_checkpoints(char_id: int) -> List[str]:
    out: List[str] = []
    for r in db.execute_reader(
            "SELECT checkpoint_id FROM character_checkpoints WHERE character_id = :cid", {"cid": char_id}):
        cp = r[0]
        if not cp.lower().startswith("world.checkpoints."):
            cp = "world.checkpoints." + cp
        out.append(cp)
    return out
