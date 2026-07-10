"""Player spawn packet — port of SendPlayerEntitySpawn (first-login path).

Builds the single BeginStream(0x07) .. EndStreamConnected(0x46) packet that
creates the player's own avatar + player entities and all their components, then
starts the movement tick.

Operation order (matches UnityGameServer.cs exactly — note OP12 is emitted
BEFORE OP11):
  OP1  create avatar          (0x01)
  OP2  create player          (0x01)
  OP3  init player            (0x02)
  OP4  Manipulators component  (0x32) — skills pass then equipment pass
  OP5  Equipment component     (0x32) — WriteInitForEquip per item
  OP6  QuestManager component  (0x32)
  OP7  DialogManager component (0x32)
  OP8  UnitContainer component (0x32) — gold + 3 inventories with starter items
  OP9  Modifiers component     (0x32)
  OP10 Skills component        (0x32) — active skills + profession
  OP12 init avatar             (0x02) — WorldEntity/Unit/Hero/Avatar WriteInit
  OP11 UnitBehavior component  (0x32) — heading + mover flags
  0x46 EndStreamConnected
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import log
from ..data import class_passives
from ..data.gc_object import GCObject, write_gc_type
from ..data import gc_object_factory
from ..db import character_repository
from ..managers.checkpoints import DEFAULT_CHECKPOINTS as _DEFAULT_CHECKPOINTS
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection


# Starting-skill canonical hotbar slots by class (binary-verified from the
# StartingSkills.gc files). Other skills get ids 200+ (skill book, no hotbar).
_STARTING_SLOTS = {
    "fighter": {"skills.generic.Stomp": 100, "skills.generic.Butcher": 105},
    "warrior": {"skills.generic.Stomp": 100, "skills.generic.Butcher": 105},
    "ranger": {"skills.generic.PoisonBlastRadius": 100, "skills.generic.PoisonShot": 105},
    "mage": {"skills.generic.ShadowLightning": 100, "skills.generic.FireBolt": 105},
    "warlock": {"skills.generic.ShadowLightning": 100, "skills.generic.FireBolt": 105},
}

_PROFESSION = {
    "mage": "skills.professions.Warlock",
    "warlock": "skills.professions.Warlock",
    "ranger": "skills.professions.Ranger",
}

def _starting_slot_map(class_name: str) -> dict:
    key = (class_name or "fighter").lower()
    for token, mapping in _STARTING_SLOTS.items():
        if token in key:
            return dict(mapping)
    return dict(_STARTING_SLOTS["fighter"])


def _find_child(parent: GCObject, gc_class: str):
    for c in parent.children:
        if c.gc_class == gc_class:
            return c
    return None


def write_avatar_entity_init(
    w: LEWriter,
    *,
    avatar_id: int,
    hp_wire: int,
    mana_wire: int,
    exp: int,
    level: int,
    owner_id: int,
    heading_wire: int,
    stat_strength: int,
    stat_agility: int,
    stat_endurance: int,
    stat_intellect: int,
    stat_pts_remaining: int,
    respec_remaining: int,
    pvp_wins: int,
    pvp_rating: int,
    face: int,
    hair: int,
    hair_color: int,
    pos_x: int = 0,
    pos_y: int = 0,
    pos_z: int = 0,
    world_entity_flags: int = 0x04,
) -> None:
    """EntityInit (opcode 0x02) — WorldEntity / Unit / Hero / Avatar WriteInit.

    Field order is identical for the player's own avatar (spawn OP12) and a
    remote player's avatar (other-player OP6); this is the single source for both.
    Only ``world_entity_flags`` differs: the self-spawn keeps the default ``0x04``
    (visible), while a remote avatar passes ``0x06`` (visible|activatable) so a
    viewer can mouse-pick / target it — see the other-player OP6 call site.

    The BLOCKING bit (0x01) must stay OFF for avatars. 0x05 was live-tested
    2026-06-11 to stop enrolled mobs at body contact (it worked — mobs paused
    at the player's edge) but the engine does NOT exclude self-collision: the
    avatar's own mover fights its own collider, teleporting the player to
    random spots and wedging them in place. Mobs must stay 0x06 too — a mob
    collider blocks the player's attack approach outside the swing gate
    (the same day's unhittable-anchored-mobs regression).

    Per RR ``Unit.WriteInit`` with ``UnitFlags = 0x07`` the Unit block emits
    ownerID (bit ``0x01``), then **HP** (bit ``0x02``), then **MP** (bit ``0x04``).
    The FIRST uint32 after ownerID is therefore the avatar's HP — the value the
    client adopts as its local synch field ``entity[+0xbc]`` and compares against
    every ``0x02`` synch trailer (per-tick ``0x36``, SpawnAction, MoverUpdate).
    Writing the ``0xFFFF00`` mana sentinel there made the client default its HP to
    65535 and fatally mismatch the trailers on ``dungeon00_level01`` (Avatar synch
    crash, exit 0xc000013a). ``hp_wire`` must be a real ×256 wire HP that equals
    what the trailers carry (``conn.hp_wire``).
    """
    w.write_byte(0x02)
    w.write_uint16(avatar_id)
    # WorldEntity.WriteInit
    w.write_uint32(world_entity_flags)        # worldEntityFlags
    w.write_int32(pos_x)
    w.write_int32(pos_y)
    w.write_int32(pos_z)
    w.write_int32(heading_wire)
    w.write_byte(0x01)                        # worldEntityInitFlags
    w.write_uint16(0)                         # Unk1Case
    # Unit.WriteInit
    w.write_byte(0x07)                        # unitFlags (0x01 owner | 0x02 HP | 0x04 MP)
    w.write_byte(level)
    w.write_uint16(0)                         # UnitUnkUint16_0
    w.write_uint16(0)                         # UnitUnkUint16_1
    w.write_uint16(owner_id)                  # ownerID            (UnitFlags 0x01)
    w.write_uint32(hp_wire)                   # HP — synch field   (UnitFlags 0x02)
    w.write_uint32(mana_wire)                 # MP                 (UnitFlags 0x04)
    # Hero.WriteInit
    w.write_uint32(exp)
    w.write_uint16(stat_strength)
    w.write_uint16(stat_agility)
    w.write_uint16(stat_endurance)
    w.write_uint16(stat_intellect)
    w.write_uint16(stat_pts_remaining)
    w.write_uint16(respec_remaining)
    w.write_uint32(pvp_wins)
    w.write_uint32(pvp_rating)
    # Avatar.WriteInit
    w.write_byte(face)
    w.write_byte(hair)
    w.write_byte(hair_color)


def send_player_entity_spawn(server: "GameServer", conn: "RRConnection") -> None:
    character = server.selected_character.get(conn.login_name)
    if character is None:
        log.error(f"[SPAWN] no selected character for {conn.login_name}")
        return

    saved = character_repository.get_character(character.id)
    if saved is None:
        log.error(f"[SPAWN] no saved data for character {character.id}")
        return

    # ── Build the entity tree and assign ids (first-login layout) ──
    server.next_entity_id = conn.conn_id * 500 + 10
    avatar = gc_object_factory.load_avatar(saved)
    player = gc_object_factory.new_player(character.name)
    conn.avatar = avatar
    conn.player = player

    def next_id() -> int:
        nid = server.next_entity_id
        server.next_entity_id += 1
        return nid

    avatar.id = next_id()
    player.id = next_id()

    manipulators = _find_child(avatar, "Manipulators")
    equipment = _find_child(avatar, "avatar.base.Equipment")
    unit_container = _find_child(avatar, "UnitContainer")
    modifiers = _find_child(avatar, "Modifiers")
    skills = _find_child(avatar, "avatar.base.skills")
    unit_behavior = _find_child(avatar, "avatar.base.UnitBehavior")

    # QuestManager + DialogManager live on the player (created fresh, as in C#).
    quest_manager = GCObject(native_class="QuestManager", gc_class="QuestManager", name="QuestManager")
    dialog_manager = GCObject(native_class="DialogManager", gc_class="DialogManager", name="DialogManager")
    player.add_child(quest_manager)
    player.add_child(dialog_manager)

    for comp in (manipulators, equipment, unit_container, modifiers, skills, unit_behavior):
        if comp is not None:
            comp.id = next_id()
            for child in comp.children:
                child.id = next_id()
    quest_manager.id = next_id()
    dialog_manager.id = next_id()

    conn.unit_behavior_id = unit_behavior.id if unit_behavior else 0
    conn.unit_container_id = unit_container.id if unit_container else 0
    conn.modifiers_id = modifiers.id if modifiers else 0
    conn.quest_manager_id = quest_manager.id
    conn.dialog_manager_id = dialog_manager.id
    conn.equipment_component_id = equipment.id if equipment else 0
    # Equip/unequip stream Manipulators sub-updates (item render add/remove) address
    # this id; if it stays 0 the client rejects the whole stream with
    # "processComponentUpdate ERROR: Invalid ComponentID(0)" (zone error Code 9) —
    # the item still reaches the cursor (the prior UnitContainer update succeeds)
    # but the Manipulators update aborts it. Was never assigned here.
    conn.manipulators_component_id = manipulators.id if manipulators else 0
    conn.skills_component_id = skills.id if skills else 0
    server.player_avatar_entity_id[str(conn.conn_id)] = avatar.id
    # Tell the client hook to drop units cached in the PREVIOUS zone — the avatar
    # and mobs are freed/re-created across a transfer, so a Unit* learned there
    # would dangle. The mob-attack injection (docs/MOB_ATTACK_INJECTION.md) uses
    # an un-fresh avatar pointer (the avatar isn't synched while idle), so the
    # reset is what keeps that pointer from surviving a zone change. The hook
    # re-learns this zone's units from the synch stream that follows.
    telem = getattr(server, "telemetry", None)
    if telem is not None and hasattr(telem, "send_zone_reset"):
        telem.send_zone_reset(conn)
    # Login-keyed lookup used by build_other_player_spawn_packet / movement relay.
    if conn.login_name:
        server.spawned_avatar_ids[conn.login_name] = avatar.id

    heading_wire = int(conn.player_heading * 256)
    level = saved.level or 1
    # Keep the connection's cached level in synch with the DB — the equip
    # level gate (net/equipment.py) and item serializers read it. It was never
    # set here, so it stayed at its __init__ default of 1 and a level-69
    # player was rejected from equipping anything with a requirement.
    conn.player_level = level

    w = LEWriter()
    w.write_byte(0x07)  # BeginStream

    # ── OP1: create avatar ──
    w.write_byte(0x01)
    w.write_uint16(avatar.id)
    write_gc_type(w, avatar.gc_class, preserve_case=True)

    # ── OP2: create player ──
    w.write_byte(0x01)
    w.write_uint16(player.id)
    write_gc_type(w, player.gc_class.lower())

    # ── OP3: init player ──
    w.write_byte(0x02)
    w.write_uint16(player.id)
    w.write_cstring(player.name)
    w.write_uint32(0)
    # u32 groupId + u32 charSqlId — the ids the client's group UI keys self
    # identity on (C# OP3: GroupDirectory groupId, then [OP3-USERID]
    # charSqlId). Re-applied 2026-07-09: the 07-09 bisect revert to zone-id/0
    # was variable-elimination only — the fields are same-width, value-only
    # (cannot desync the reader) and required for right-click invite.
    w.write_uint32(server.groups.group_id_for(conn)
                   if getattr(server, "groups", None) else 0)
    w.write_byte(0x00)                       # membership: admin
    w.write_uint32(getattr(conn, "char_sql_id", 0) or (conn.conn_id + 1))
    w.write_uint32(saved.pvp_wins)
    w.write_uint32(saved.pvp_rating)
    w.write_byte(0x00)                        # PvP team null string
    w.write_cstring("Reborn")                 # posse name
    w.write_uint32(0)
    # ── OP4: Manipulators (skills + equipment, two passes matching C#) ──
    active_skills = [c for c in (manipulators.children if manipulators else [])
                     if c.native_class == "ActiveSkill"]
    equip_children = [c for c in (manipulators.children if manipulators else [])
                      if c.native_class not in ("ActiveSkill", "PassiveSkill")]
    # Passives ride OP4 too, but with the PASSIVE wire body (trailing u32
    # modifier id instead of the active's flags byte) — see data.class_passives.
    passive_manips = class_passives.collect_passive_manipulators(saved)
    slot_map = _starting_slot_map(saved.class_name)
    for hbs in saved.hotbar_slots:
        slot_map[hbs.skill] = hbs.slot

    w.write_byte(0x32)
    w.write_uint16(avatar.id)
    w.write_uint16(manipulators.id)
    write_gc_type(w, "Manipulators")
    w.write_byte(0x01)
    # C# writes a SINGLE child count = total manipulator children (skills +
    # passives + equipment), then all skills, then all equipment with NO second
    # count (UnityGameServer.cs:16535 + the two passes). Writing a separate
    # equip-children count here desyncs the client (the stray byte before the
    # first equipment item's 0xFF).
    w.write_byte(len(active_skills) + len(passive_manips) + len(equip_children))
    # Pass 1: skills (hotbar slot assignment). Also record the session
    # slot-id → skill map (the C# _playerManipMap, built during this same Op4
    # write) — net.skills uses it for hotbar place/remove displacement and
    # 0x52 self-cast buff resolution.
    manip_map = {}
    non_slot_id = 200
    for skill in active_skills:
        skill_id = slot_map.get(skill.gc_class)
        if skill_id is None:
            skill_id = non_slot_id
            non_slot_id += 1
        manip_map[skill_id] = skill.gc_class
        w.write_byte(0xFF)
        w.write_cstring(skill.gc_class.lower())
        w.write_uint32(skill_id)
        w.write_byte(max(1, saved.get_skill_level(skill.gc_class)))
        w.write_byte(0x00)
    # Pass 1b: passive skills. Body = 0xFF · cstring · u32 slot · u8 level ·
    # u32 modifierId — PassiveSkill::readInit @0x53D0E0 reads exactly one u32
    # after the shared id+level (Ghidra-verified 2026-06-12); the active-style
    # 1-byte tail desyncs the reader (the old "Invalid type tag" crash).
    for pm in passive_manips:
        manip_map[pm.slot] = pm.skill
        w.write_byte(0xFF)
        w.write_cstring(pm.skill.lower())
        w.write_uint32(pm.slot)
        w.write_byte(pm.level)
        w.write_uint32(pm.modifier_id)
    conn.skill_manip_map = manip_map
    # Pass 2: equipment manipulators — no count byte (C# WriteInit per item).
    for item in equip_children:
        item.write_init_for_equip(w, level)
    # ── OP5: Equipment (inline format — NOT WriteInitForEquip) ──
    eq_children = equipment.children if equipment else []
    w.write_byte(0x32)
    w.write_uint16(avatar.id)
    w.write_uint16(equipment.id)
    write_gc_type(w, "avatar.base.Equipment")
    w.write_byte(0x01)
    w.write_byte(len(eq_children))
    for item in eq_children:
        item.write_init_for_equip_op5(w, level)
    # ── OP6: QuestManager ──
    # Build/refresh this session's runtime quest state so the journal block
    # reflects the character's actual active quests (assigns instance ids).
    active_quests = []
    try:
        active_quests = server.quests.get_active_runtime(conn)
    except Exception as ex:  # noqa: BLE001 — never break the spawn stream on quests
        log.warn(f"[SPAWN] quest state init failed: {ex}")
    _write_quest_manager(w, conn, player.id, quest_manager.id, active_quests)
    # ── OP7: DialogManager ──
    w.write_byte(0x32)
    w.write_uint16(player.id)
    w.write_uint16(dialog_manager.id)
    write_gc_type(w, "DialogManager")
    w.write_byte(0x01)
    # ── OP8: UnitContainer (gold + 3 inventories with items) ──
    w.write_byte(0x32)
    w.write_uint16(avatar.id)
    w.write_uint16(unit_container.id)
    write_gc_type(w, "UnitContainer")
    w.write_byte(0x01)
    w.write_uint32(0)
    w.write_uint32(saved.gold)
    w.write_byte(0x03)
    # Seed the session slot-map from DB so the slot ids written below are the
    # exact ids the client will echo back on use/pickup/drop. The cursor and any
    # mid-session adds reuse this allocator (see net.inventory / net.equipment).
    conn.inv_model.reset()
    seeded = conn.inv_model.load(
        [{"gc_class": it.gc_class, "x": it.x, "y": it.y, "count": it.count,
          "rarity": it.rarity, "stored_level": it.stored_level,
          "buy_price": getattr(it, "buy_price", 0)}
         for it in (saved.inventory or [])][:20])
    for gc_class, inv_id in (("avatar.base.Inventory", 0x0B),
                             ("avatar.base.Bank", 0x0C),
                             ("avatar.base.TradeInventory", 0x0D)):
        write_gc_type(w, gc_class)
        w.write_byte(inv_id)
        w.write_byte(0x01)                    # has items section
        # Only send inventory items for the main inventory (not bank/trade).
        inv_items = seeded if gc_class == "avatar.base.Inventory" else []
        if inv_items:
            w.write_byte(len(inv_items))
            # Each item carries the slot id the model assigned; the client echoes
            # this id verbatim on UseItem/Pickup/Drop.
            for inv_item in inv_items:
                item = GCObject(native_class="MeleeWeapon", gc_class=inv_item.gc_class, name="")
                item.stored_rarity = inv_item.rarity
                item.stored_level = inv_item.stored_level
                item.write_init_for_inventory(w, inv_item.x, inv_item.y, inv_item.slot_id, level,
                                              count=inv_item.count)
        else:
            w.write_byte(0x00)                # 0 items
    w.write_byte(0x00)                        # UnitContainer terminator
    # ── OP9: Modifiers (carries the passive-skill modifiers) ──
    # C# WritePassiveModifiersComponent: header u32 (0xF000+count) · u32 0 ·
    # u8 count, then per passive a "<skill>.modifier" entry shaped like the
    # standard Modifier body (u32 id · u8 level · u32 powerLevel · u32 duration ·
    # u8 sourceIsSelf), then one 0x01 byte per passive. With zero passives this
    # is the empty block we always shipped (only the first u32 differs).
    w.write_byte(0x32)
    w.write_uint16(avatar.id)
    w.write_uint16(modifiers.id)
    write_gc_type(w, "Modifiers")
    w.write_byte(0x01)
    w.write_uint32(class_passives.PASSIVE_MODIFIER_ID_BASE + len(passive_manips))
    w.write_uint32(0x00000000)
    w.write_byte(len(passive_manips))
    for pm in passive_manips:
        w.write_byte(0xFF)
        w.write_cstring(pm.skill.lower() + ".modifier")
        w.write_uint32(pm.modifier_id)
        w.write_byte(pm.level)
        w.write_uint32(0x00000000)
        w.write_uint32(0x00000000)
        w.write_byte(0x00)
    for _ in passive_manips:
        w.write_byte(0x01)
    # ── OP10: Skills (active skills + passives + profession) ──
    w.write_byte(0x32)
    w.write_uint16(avatar.id)
    w.write_uint16(skills.id)
    write_gc_type(w, "avatar.base.skills")
    w.write_byte(0x01)
    w.write_uint32(0xFFFFFFFF)
    # Passives list with the actives here (same entry shape, C# OP10).
    op10_passives = [s for s in (saved.skills or [])
                     if class_passives.is_passive_skill(s)]
    w.write_byte(len(active_skills) + len(op10_passives))
    for skill in active_skills:
        write_gc_type(w, skill.gc_class)
        w.write_uint32(0)
        w.write_byte(max(1, saved.get_skill_level(skill.gc_class)))
    for skill_gc in op10_passives:
        write_gc_type(w, skill_gc)
        w.write_uint32(0)
        w.write_byte(max(1, saved.get_skill_level(skill_gc)))
    w.write_byte(0x01)
    profession = _PROFESSION.get((saved.class_name or "").lower(), "skills.professions.Warrior")
    write_gc_type(w, profession)
    # ── OP12: init avatar (WorldEntity / Unit / Hero / Avatar WriteInit) ──
    # The first uint32 after ownerID is HP (UnitFlags 0x02), NOT mana. It must be
    # the real ×256 wire HP the synch trailers carry (conn.hp_wire, set by
    # _refresh_avatar_hp_wire BEFORE this packet is built); the second uint32 is
    # MP (UnitFlags 0x04) where the 0xFFFF00 sentinel is fine (MP is not synched).
    points_per_level = 5
    allocated = (saved.stat_strength + saved.stat_agility
                 + saved.stat_endurance + saved.stat_intellect)
    available = (level - 1) * points_per_level
    stat_pts_remaining = max(0, available - allocated)
    write_avatar_entity_init(
        w,
        avatar_id=avatar.id,
        hp_wire=conn.hp_wire,
        mana_wire=0xFFFF00,                    # MP sentinel (client clamps; not synched)
        exp=saved.experience,
        level=level,
        owner_id=player.id,                   # ownerID = player entity
        heading_wire=heading_wire,
        stat_strength=saved.stat_strength,
        stat_agility=saved.stat_agility,
        stat_endurance=saved.stat_endurance,
        stat_intellect=saved.stat_intellect,
        stat_pts_remaining=stat_pts_remaining,
        respec_remaining=0,
        pvp_wins=saved.pvp_wins,
        pvp_rating=saved.pvp_rating,
        face=saved.face,
        hair=saved.hair,
        hair_color=saved.hair_color,
    )
    # ── OP11: UnitBehavior ──
    w.write_byte(0x32)
    w.write_uint16(avatar.id)
    w.write_uint16(unit_behavior.id)
    write_gc_type(w, "avatar.base.UnitBehavior")
    w.write_byte(0x01)
    w.write_byte(0xFF)                        # Behavior marker
    w.write_byte(0x00)                        # Action1 null
    w.write_byte(0x00)                        # Action2 null
    w.write_byte(0xFF)                        # generation counter
    w.write_byte(0x08)                        # UnitMoverFlags (bit3 = apply heading)
    w.write_int32(heading_wire)
    w.write_int32(heading_wire)
    w.write_byte(0x00)                        # WaypointFlags
    w.write_byte(0xFF)                        # SessionID
    w.write_byte(0x00)
    w.write_byte(0x00)
    w.write_byte(0x46)                        # EndStreamConnected

    conn.session_id = 0xFF

    spawn = w.to_array()
    conn.send_compressed_a(0x01, 0x0F, spawn)
    log.info(f"[SPAWN] sent player spawn ({len(spawn)} bytes) avatar={avatar.id} "
             f"player={player.id} ub={conn.unit_behavior_id} for '{conn.login_name}'")


def build_other_player_spawn_packet(server: "GameServer", player_conn: "RRConnection",
                                    avatar_entity_id: int, viewer_conn: "RRConnection") -> bytes:
    """Build the spawn packet that lets ``viewer_conn`` see ``player_conn``'s avatar.

    Port of C# BuildOtherPlayerSpawnPacket (UnityGameServer.cs:15224). Creates a
    remote avatar with Skills / Manipulators / Modifiers / UnitBehavior components
    (UnitBehavior LAST, matching self-spawn order) followed by EntityInit + WarpTo.
    Registers per-viewer remap ids so movement relay can target this avatar.
    """
    pos_x = int(player_conn.player_pos_x * 256)
    pos_y = int(player_conn.player_pos_y * 256)
    pos_z = int(player_conn.player_pos_z * 256)
    heading = int(player_conn.player_heading * 256)
    # Fallback only; the authoritative level is saved.level (DB), applied once the
    # character row is loaded below. player_conn.player_level is not kept in sync for
    # a viewed player, so relying on it showed every remote player as level 1.
    level = max(1, player_conn.player_level)

    # Offset slightly so players don't stack (C#: +1280 = 5 units on X).
    pos_x += 1280

    def next_id() -> int:
        nid = server.next_entity_id & 0xFFFF
        server.next_entity_id += 1
        return nid

    remote_player_id = next_id()
    remote_behavior_id = next_id()
    remote_skills_id = next_id()
    remote_manip_id = next_id()
    remote_mod_id = next_id()

    server.remote_behavior_ids.setdefault(viewer_conn.login_name, {})[player_conn.login_name] = remote_behavior_id
    server.remote_avatar_ids.setdefault(viewer_conn.login_name, {})[player_conn.login_name] = avatar_entity_id & 0xFFFF
    server.remote_player_ids.setdefault(viewer_conn.login_name, {})[player_conn.login_name] = remote_player_id
    server.remote_manip_ids.setdefault(viewer_conn.login_name, {})[player_conn.login_name] = remote_manip_id

    avatar = player_conn.avatar
    avatar_gc_class = avatar.gc_class if avatar else "avatar.classes.Fighter"

    saved = character_repository.get_character(player_conn.char_sql_id)
    if saved and saved.level:
        level = saved.level                              # authoritative DB level

    w = LEWriter()
    w.write_byte(0x07)                                   # BeginStream

    # ── OP1: create entity ──
    w.write_byte(0x01)
    w.write_uint16(avatar_entity_id)
    write_gc_type(w, avatar_gc_class, preserve_case=True)

    # ── OP1b: create + init the remote Player object ──
    # The self-spawn ships a Player object (OP2 create + OP3 init with the name)
    # and points the avatar's ownerID at it; the client reads that owner to bind
    # the avatar to a NAMED unit (the floating nameplate). The other-player packet
    # historically omitted it (owner_id=0), so the viewer saw an unbound placeholder
    # nameplate and right-clicking it crashed the client (unguarded null target
    # deref in FUN_0046e6e0 — the right-click target-resolution can't validate an
    # owner-less unit). We send ONLY the public Player create+init (name / pvp /
    # posse) — NOT the owner-only QuestManager / DialogManager children.
    player_name = (saved.name if saved else None) or player_conn.login_name or "Player"
    w.write_byte(0x01)                                   # create Player
    w.write_uint16(remote_player_id)
    write_gc_type(w, "player")
    w.write_byte(0x02)                                   # init Player
    w.write_uint16(remote_player_id)
    w.write_cstring(player_name)
    w.write_uint32(0)
    # groupId + charSqlId identity, like the self OP3 above (C# writes the
    # same tail for remote players — the viewer's client keys right-click
    # invite/goto targeting on these). Re-applied 2026-07-09.
    w.write_uint32(server.groups.group_id_for(player_conn)
                   if getattr(server, "groups", None) else 0)
    w.write_byte(0x00)                                   # membership
    w.write_uint32(getattr(player_conn, "char_sql_id", 0)
                   or (player_conn.conn_id + 1))
    w.write_uint32(saved.pvp_wins if saved else 0)
    w.write_uint32(saved.pvp_rating if saved else 0)
    w.write_byte(0x00)                                   # PvP team null string
    w.write_cstring("Reborn")                            # posse name
    w.write_uint32(0)

    # ── OP2: Skills component ──
    w.write_byte(0x32)
    w.write_uint16(avatar_entity_id)
    w.write_uint16(remote_skills_id)
    write_gc_type(w, "skills")
    w.write_byte(0x01)
    w.write_byte(0xFF); w.write_byte(0xFF)
    w.write_byte(0xFF); w.write_byte(0xFF)               # gold
    w.write_byte(0x00)                                   # zero skills
    w.write_byte(0x01)                                   # one profession
    profession = _PROFESSION.get((player_conn.class_name or "").lower(), "skills.professions.Warrior")
    write_gc_type(w, profession, preserve_case=True)

    # ── OP3: Manipulators component (skills + equipment) ──
    w.write_byte(0x32)
    w.write_uint16(avatar_entity_id)
    w.write_uint16(remote_manip_id)
    write_gc_type(w, "manipulators")
    w.write_byte(0x01)

    manip = _find_child(avatar, "Manipulators") if avatar else None
    skill_items = [c for c in (manip.children if manip else []) if c.native_class == "ActiveSkill"]
    equip_items = [c for c in (manip.children if manip else [])
                   if c.native_class not in ("ActiveSkill", "PassiveSkill") and c.gc_class]
    remote_passives = class_passives.collect_passive_manipulators(saved)

    slot_map = _starting_slot_map(saved.class_name if saved else player_conn.class_name)
    if saved:
        for hbs in saved.hotbar_slots:
            slot_map[hbs.skill] = hbs.slot

    w.write_byte(len(skill_items) + len(remote_passives) + len(equip_items))
    non_slot_id = 200
    for skill in skill_items:                            # PASS 1: skills
        slot_id = slot_map.get(skill.gc_class)
        if slot_id is None:
            slot_id = non_slot_id
            non_slot_id += 1
        w.write_byte(0xFF)
        w.write_cstring(skill.gc_class.lower())
        w.write_uint32(slot_id)
        w.write_byte(0x01)
        w.write_byte(0x00)
    for pm in remote_passives:                           # PASS 1b: passives
        w.write_byte(0xFF)
        w.write_cstring(pm.skill.lower())
        w.write_uint32(pm.slot)
        w.write_byte(pm.level)
        w.write_uint32(pm.modifier_id)
    for item in equip_items:                             # PASS 2: equipment
        item.write_init(w, level)

    # ── OP4: Modifiers component (same passive-aware shape as self OP9) ──
    w.write_byte(0x32)
    w.write_uint16(avatar_entity_id)
    w.write_uint16(remote_mod_id)
    write_gc_type(w, "modifiers")
    w.write_byte(0x01)
    w.write_uint32(class_passives.PASSIVE_MODIFIER_ID_BASE + len(remote_passives))
    w.write_uint32(0x00000000)
    w.write_byte(len(remote_passives))
    for pm in remote_passives:
        w.write_byte(0xFF)
        w.write_cstring(pm.skill.lower() + ".modifier")
        w.write_uint32(pm.modifier_id)
        w.write_byte(pm.level)
        w.write_uint32(0x00000000)
        w.write_uint32(0x00000000)
        w.write_byte(0x00)
    for _ in remote_passives:
        w.write_byte(0x01)

    # ── OP5: UnitBehavior component — LAST (matches self-spawn OP11) ──
    w.write_byte(0x32)
    w.write_uint16(avatar_entity_id)
    w.write_uint16(remote_behavior_id)
    write_gc_type(w, "avatar.base.UnitBehavior")
    w.write_byte(0x01)
    w.write_byte(0xFF); w.write_byte(0x00); w.write_byte(0x00)   # Behavior::readInit
    w.write_byte(0xFF); w.write_byte(0x00)                       # UnitMover gen + flags
    w.write_uint32(0x00000000)
    w.write_uint32(0x00000000)
    w.write_byte(0x00)                                           # WaypointFlags
    w.write_byte(0xFF)                                           # SessionID
    w.write_byte(0x01)                                           # Unk1 bit0 = activate
    w.write_byte(0x00)

    # ── OP6: EntityInit (avatar format, matches self-spawn OP12) ──
    # ownerID = the remote Player object created in OP1b (binds the avatar to a
    # named unit for the nameplate). HP is the first uint32 (UnitFlags 0x02): send
    # the remote player's real ×256 wire HP so a viewer's synch compare matches.
    #
    # worldEntityFlags = 0x06 (visible|activatable), NOT the self-spawn default 0x04
    # (visible only). The "activatable" bit 0x02 is what lets a VIEWER mouse-pick /
    # target this entity: empirically mobs (0x06) and NPCs (0x07) set it and are
    # clickable, while a remote avatar shipped at 0x04 is invisible to clicks (live
    # bug: "clicking another player does nothing"). We match mobs (0x06) rather than
    # NPCs (0x07) so players stay non-blocking — bit 0x01 = blocking gates the client
    # WorldEntity::processInited @0x4d3560 collider add, and players must not collide.
    from ..data.player_state import (
        compute_avatar_max_hp_wire, compute_avatar_max_mana_wire,
        compute_saved_avatar_hp_wire, compute_saved_avatar_mana_wire)
    # Passive-aware: the owning client computes its HP with the class-passive
    # bonus, and its relayed synch trailers carry that value — the viewer's
    # create must agree.
    other_hp_wire = (compute_saved_avatar_hp_wire(saved) if saved
                     else compute_avatar_max_hp_wire(level))
    # A real, bounded MP for the viewer's nameplate bar. The 0xFFFF00 sentinel is
    # fine for the SELF avatar (the owning client manages its own mana locally), but
    # a viewer renders the remote nameplate's mana bar straight from this MP field —
    # the 16M sentinel made the bar overflow the screen with no boundary. Send the
    # level-derived max so the bar reads full and stays bounded.
    other_mana_wire = (compute_saved_avatar_mana_wire(saved) if saved
                       else compute_avatar_max_mana_wire(level))
    write_avatar_entity_init(
        w,
        avatar_id=avatar_entity_id,
        hp_wire=other_hp_wire,
        mana_wire=other_mana_wire,                       # real bounded MP (nameplate bar)
        exp=0,                                           # experience hidden for others
        level=level,
        owner_id=remote_player_id,                       # bind avatar → remote Player (nameplate)
        heading_wire=heading,
        world_entity_flags=0x06,                         # visible|activatable (clickable)
        stat_strength=saved.stat_strength if saved else 0,
        stat_agility=saved.stat_agility if saved else 0,
        stat_endurance=saved.stat_endurance if saved else 0,
        stat_intellect=saved.stat_intellect if saved else 0,
        stat_pts_remaining=0,
        respec_remaining=0,
        pvp_wins=saved.pvp_wins if saved else 0,
        pvp_rating=saved.pvp_rating if saved else 0,
        face=saved.face if saved else 0,
        hair=saved.hair if saved else 0,
        hair_color=saved.hair_color if saved else 0,
        pos_x=pos_x,
        pos_y=pos_y,
        pos_z=pos_z,
    )

    # ── OP7: WarpTo — place at initial position ──
    w.write_byte(0x35)
    w.write_uint16(remote_behavior_id)
    w.write_byte(0x04)
    w.write_byte(0x11)                                   # WarpTo
    w.write_byte(0x00)                                   # SessionID
    w.write_int32(pos_x)
    w.write_int32(pos_y)
    w.write_int32(pos_z)
    w.write_byte(0x02)
    w.write_uint32(other_hp_wire)                        # synch HP — match the create

    w.write_byte(0x06)                                   # EndStream
    return w.to_array()


def exchange_player_spawns(server: "GameServer", conn: "RRConnection") -> None:
    """Spawn ``conn`` into every other spawned player in the same zone/instance and
    vice-versa (C# UnityGameServer.cs:19178 multiplayer exchange block)."""
    my_avatar_id = server.get_player_avatar_id(conn.login_name)
    for other in list(server.connections.values()):
        if other is conn or not other.is_spawned:
            continue
        if other.current_zone_gc_type != conn.current_zone_gc_type:
            continue
        if other.instance_id != conn.instance_id:
            continue

        other_avatar_id = server.get_player_avatar_id(other.login_name)
        if other_avatar_id:
            conn.send_to_client(build_other_player_spawn_packet(server, other, other_avatar_id, conn))
            log.info(f"[MULTIPLAYER] sent {other.login_name}'s avatar to {conn.login_name}")
        if my_avatar_id:
            other.send_to_client(build_other_player_spawn_packet(server, conn, my_avatar_id, other))
            log.info(f"[MULTIPLAYER] sent {conn.login_name}'s avatar to {other.login_name}")

    # Replicate other players' live Bling Gnomes to the joiner (DRS-NET
    # SendGnomesToConnection late-join block).
    gnome = getattr(server, "gnome", None)
    if gnome is not None:
        gnome.send_gnomes_to_connection(conn)


def broadcast_entity_remove(server: "GameServer", leaving_conn: "RRConnection") -> None:
    """Despawn a leaving player's avatar from every other viewer (C# BroadcastEntityRemove)."""
    zone_gc = leaving_conn.current_zone_gc_type
    for other in list(server.connections.values()):
        if other is leaving_conn or not other.is_spawned:
            continue
        if other.current_zone_gc_type != zone_gc:
            continue
        if other.instance_id != leaving_conn.instance_id:
            continue
        avatar_map = server.remote_avatar_ids.get(other.login_name)
        if avatar_map and leaving_conn.login_name in avatar_map:
            avatar_entity_id = avatar_map.pop(leaving_conn.login_name)
            w = LEWriter()
            w.write_byte(0x07)                          # BeginStream
            w.write_byte(0x05)                          # EntityDespawn
            w.write_uint16(avatar_entity_id)
            # Also despawn the bound remote Player object (OP1b) so it isn't leaked.
            player_map = server.remote_player_ids.get(other.login_name)
            if player_map and leaving_conn.login_name in player_map:
                w.write_byte(0x05)                      # EntityDespawn (Player)
                w.write_uint16(player_map.pop(leaving_conn.login_name))
            w.write_byte(0x06)                          # EndStream
            other.send_to_client(w.to_array())
            log.info(f"[MULTIPLAYER] despawned {leaving_conn.login_name} for {other.login_name}")
        behavior_map = server.remote_behavior_ids.get(other.login_name)
        if behavior_map:
            behavior_map.pop(leaving_conn.login_name, None)
        manip_map = server.remote_manip_ids.get(other.login_name)
        if manip_map:
            manip_map.pop(leaving_conn.login_name, None)

    server.remote_behavior_ids.pop(leaving_conn.login_name, None)
    server.remote_avatar_ids.pop(leaving_conn.login_name, None)
    server.remote_player_ids.pop(leaving_conn.login_name, None)
    server.remote_manip_ids.pop(leaving_conn.login_name, None)
    if leaving_conn.login_name:
        server.spawned_avatar_ids.pop(leaving_conn.login_name, None)


def _write_quest_manager(w: LEWriter, conn: "RRConnection",
                         player_id: int, qm_id: int,
                         active_quests=None) -> None:
    """WriteQuestManagerComponent.

    Emits the player's active quests (so the journal repopulates after a zone
    transfer / relog) followed by the unlocked-checkpoint list. The checkpoint
    list is what populates the client's obelisk recall menu, so it MUST reflect
    this character's *actual* unlocked waystones (``conn.unlocked_checkpoints``).
    Re-sent on every zone load. Mirrors C# ``QuestManager.WriteQuestManagerComponent``.
    """
    w.write_byte(0x32)
    w.write_uint16(player_id)
    w.write_uint16(qm_id)
    write_gc_type(w, "QuestManager")
    w.write_byte(0x01)
    w.write_uint32(0x01)
    # HasTownPortal block — the obelisk "Saved Places" entry reads this.
    if conn.has_saved_town_portal and conn.town_portal_zone_name:
        w.write_byte(0x01)                    # HasTownPortal = TRUE
        w.write_cstring(conn.town_portal_zone_name)
        w.write_cstring("")
        w.write_uint32(conn.town_portal_zone_id)
    else:
        w.write_byte(0x00)                    # HasTownPortal = FALSE
        w.write_cstring("Hello")
        w.write_cstring("HelloAgain")
        w.write_uint32(0x00)
    w.write_byte(0x00)                        # no alternate zone
    w.write_cstring("")
    w.write_cstring("")
    w.write_uint32(0x00)
    w.write_cstring(conn.zone_portal_source or "")   # zone portal source
    w.write_cstring("")
    w.write_cstring("")
    w.write_byte(0x00)
    # Active quests — count + per-quest gc_type/instance/objectives.
    from ..managers import quest_wire
    quest_wire.write_quest_entries(w, list(active_quests or []))
    # Unlocked checkpoints — sorted by display order so the menu is stable and
    # matches the obelisk-cycle ordering. Falls back to the Town/Tutorial
    # defaults only if the character somehow has none unlocked.
    from ..managers.checkpoints import checkpoint_manager
    unlocked = conn.unlocked_checkpoints or set(_DEFAULT_CHECKPOINTS)

    def _order(cp_id: str) -> int:
        dest = checkpoint_manager.find_destination(cp_id)
        return dest.order if dest is not None else 1_000_000

    ordered = sorted(unlocked, key=_order)
    w.write_uint16(len(ordered))
    for cp in ordered:
        write_gc_type(w, cp, preserve_case=True)
