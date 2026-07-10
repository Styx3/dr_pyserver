"""Chat commands — admin/debug tool system.

Ported from C# ChatCommandHandler.cs + Go chatcommander. Parses @commands
from chat messages and executes server-side actions. Commands are prefixed
with @ in zone/world/noob chat channels.

Admin commands available (growing list):
  @z <zone>          — Change zone (e.g. @z town)
  @warp <x> <y> <z>  — Teleport to position  
  @gold <amount>     — Give gold
  @level <n>         — Set player level
  @xp <amount>       — Give XP
  @heal              — Full heal HP/MP
  @spawn <creature>  — Spawn a creature by GC type
  @stats             — Show current stats
  @quest <id>        — Accept a quest
  @item <gc_type> [rarity] — Give an item
  @clear             — Clear inventory
  @pos               — Show current position
"""
from __future__ import annotations

import re
import random
from typing import TYPE_CHECKING, Callable, Dict, List

from ..core import log
from ..db import character_repository
from ..data.gc_object import get_packet_gc_class_for, detect_rarity_from_gc_class
from ..data.rarity_helper import ItemRarity, get_item_level
from ..util.byte_io import LEWriter

if TYPE_CHECKING:  # pragma: no cover
    from .game_server import GameServer
    from .connection import RRConnection

_CMD_SPLIT = re.compile(r'(?:^@|)(?:"([^"]*)"|(\S+))(?: |$)')


def _parse_args(msg: str) -> tuple[str, List[str]]:
    """Parse @command arg1 arg2... from a chat message. Returns (command, args)."""
    if not msg.startswith("@"):
        return "", []
    parts = msg[1:].strip().split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def build_say_packet(sender_name: str, message: str) -> bytes:
    """Build the outbound chat "Say" packet — port of C# BroadcastChatToZone.

    Layout: ``[0x06][0x00][0x02][0x00][sender\\0][message\\0]`` (chat channel,
    type 0, subtype 0x02 = Say/white, padding 0x00). The channel byte MUST be
    0x06; the client ignores chat sent on channel 3 (that's the social channel).
    """
    w = LEWriter()
    w.write_byte(0x06)           # chat channel
    w.write_byte(0x00)           # message type 0
    w.write_byte(0x02)           # subtype 0x02 = Say (white)
    w.write_byte(0x00)           # padding
    w.write_cstring(sender_name)
    w.write_cstring(message)
    return w.to_array()


def _display_name(server: "GameServer", conn: "RRConnection") -> str:
    """Resolve the in-game character name for chat — port of C#
    BroadcastChatToZone: prefer the selected character's name, fall back to the
    account/login name. The client shows whatever the server puts in the Say
    packet, so without this it displayed the account name instead of the hero.
    """
    char = server.selected_character.get(conn.login_name)
    name = getattr(char, "name", None)
    return name or conn.login_name or "Unknown"


def _send_chat(conn: "RRConnection", text: str, channel: int = 0x06) -> None:
    """Send a system/command-response line to one player.

    @-command output goes through SendSystemMessage in C#
    (``[0x06][0x00][0x0D]<text>[0x00]``) — reuse the verified path on the
    connection rather than re-rolling the chat bytes here.
    """
    conn.send_system_message(text)


# ── Multi-channel chat routing ────────────────────────────────────────────────
#
# Inbound chat arrives on framing channel 6 with the message_type byte carrying
# the *sub-channel* the player typed into (confirmed live 2026-06-08):
#   0x01=World  0x02=Zone  0x03=Group  0x04=Tell  0x05=Market  0x06=Noob
#
# Display (verified in the client 2026-06-08 — ChatControl ctor FUN_0041d610 +
# the chat-receive parser FUN_005ff450):
#   * The *subtype* byte indexes the client's prefix table. Index 0x0D =
#     "Announce> " — that is why every line we sent via the system-message path
#     was tagged "Announce>". Keep 0x0D ONLY for server / @-command output.
#   * Subtype 0x03 = the local "Say" channel: EMPTY prefix and always visible.
#     Unlike World(0x02)/Market(0x0B)/Noob(0x0C) — which are *joinable* channels
#     gated by the avatar's +0x9c bitmask and silently dropped until joined —
#     0x03 has no join gate, so it is the one structured subtype that reliably
#     surfaces. (That join gate is why the old subtype-0x02 packet "parsed but
#     never surfaced".)
#   * Every subtype except 0x0D reads a sender-name cstring before the message,
#     and the client renders "<sender>: <message>". So we route ALL player chat
#     through 0x03 and fold the channel tag into the sender field — the window
#     shows e.g. "[Noob] Maje: hi" with no "Announce>" and no markup surprises.
#
# Native display subtypes — index the client's prefix + COLOUR table built in
# the ChatControl ctor (FUN_0041d610). Sending on the real subtype lets the
# client render the proper "<Channel>> " prefix and channel colour itself, and
# routes the line to the right tab. We must NOT bake the channel name into the
# sender field (that produced "[Noob Styx3]: …" with no colour).
_CHAT_SUBTYPE_SAY = 0x03  # local "Say": empty prefix, no join gate

# inbound framing message_type → (display_subtype, scope, min_level)
_CHAT_CHANNELS: Dict[int, tuple] = {
    0x01: (0x02, "global", 15),  # World>
    0x02: (0x03, "zone",   0),   # local / Zone say (no prefix)
    0x03: (0x04, "group",  0),   # Group>
    0x04: (0x05, "tell",   0),   # Tell>  (handled in _send_tell)
    0x05: (0x0B, "global", 15),  # Market>
    0x06: (0x0C, "global", 0),   # Noob>
}

# Subtypes that read the grouped extra flag byte in the parser (FUN_005ff450).
_GROUPED_SUBTYPES = frozenset({0x02, 0x03, 0x04, 0x05, 0x0B, 0x0C, 0x10})


def build_chat_line(sender_field: str, message: str, subtype: int = _CHAT_SUBTYPE_SAY) -> bytes:
    """Build an outbound structured chat line on its native ``subtype``.

    Layout ``[0x06][0x00][subtype]([flag])[sender\\0][message\\0]`` — channel
    0x06, message-type 0 (say), the display ``subtype``, the grouped-subtype
    flag byte (only for subtypes the client expects it on), then the sender name
    and the message. The client prepends the channel prefix + colour for the
    subtype, so ``sender_field`` should be the bare character name.
    """
    w = LEWriter()
    w.write_byte(0x06)            # chat channel
    w.write_byte(0x00)            # message type 0 = say
    w.write_byte(subtype)        # native display subtype (prefix + colour)
    if subtype in _GROUPED_SUBTYPES:
        w.write_byte(0x00)        # grouped-subtype flag byte
    w.write_cstring(sender_field)
    w.write_cstring(message)
    return w.to_array()


def _player_level(server: "GameServer", conn: "RRConnection") -> int:
    """Current level — the saved character (DB) is authoritative.

    ``conn.player_level`` only tracks the in-session ``@level`` override and
    stays at its default of 1 otherwise, so a genuinely high-level character
    was being mis-gated. Read the persisted level first, then fall back.
    """
    char_id = getattr(conn, "char_sql_id", None)
    if char_id is not None:
        saved = character_repository.get_character(char_id)
        if saved and saved.level:
            return int(saved.level)
    char = server.selected_character.get(conn.login_name)
    lvl = getattr(char, "level", None)
    if lvl:
        return int(lvl)
    return int(getattr(conn, "player_level", 1) or 1)


def _find_conn_by_name(server: "GameServer", name: str) -> "RRConnection | None":
    """Resolve a spawned player by character name (preferred) or login name."""
    nl = name.lower()
    for c in server.connections.values():
        if not c.is_spawned:
            continue
        char = server.selected_character.get(c.login_name)
        cname = (getattr(char, "name", None) or c.login_name or "")
        if cname.lower() == nl or (c.login_name or "").lower() == nl:
            return c
    return None


def _chat_audience(server: "GameServer", conn: "RRConnection", scope: str) -> List["RRConnection"]:
    spawned = [c for c in server.connections.values() if c.is_spawned]
    if scope == "global":
        return spawned
    if scope == "zone":
        return [c for c in spawned
                if c.current_zone_gc_type == conn.current_zone_gc_type
                and c.instance_id == conn.instance_id]
    if scope == "group":
        gm = getattr(server, "groups", None)
        members = set(gm.member_logins(conn)) if gm else set()
        if not members:
            return [conn]  # solo — echo to self only
        return [c for c in spawned if c.login_name in members]
    return [conn]


def _send_tell(server: "GameServer", conn: "RRConnection", text: str) -> None:
    """Handle a /tell — inbound payload is ``<target> <message...>``."""
    target_name, _, message = text.partition(" ")
    if not target_name or not message:
        _send_chat(conn, "Usage: /tell <name> <message>")
        return
    target = _find_conn_by_name(server, target_name)
    if target is None:
        _send_chat(conn, f"Player '{target_name}' not found or offline.")
        return
    sender = _display_name(server, conn)
    # Native Tell> subtype (0x05) carries the colour + prefix; the client
    # brackets the bare name. Recipient sees "Tell> Maje", sender's echo names
    # the target ("Tell> to Styx3").
    tell_subtype = _CHAT_CHANNELS[0x04][0]
    target.send_to_client(build_chat_line(sender, message, tell_subtype))
    conn.send_to_client(build_chat_line(f"to {_display_name(server, target)}", message, tell_subtype))


# Human-readable channel names for the level-gate notice (subtype → name).
_CHANNEL_NAMES: Dict[int, str] = {0x01: "World", 0x05: "Market"}


def _send_chat_zone(server: "GameServer", conn: "RRConnection", text: str, channel: int = 0x02) -> None:
    """Route a chat message to the right audience on its native subtype.

    ``channel`` is the inbound framing message_type (the sub-channel typed into).
    World/Market are level-15 gated; Zone is the current zone instance; Group
    goes to group members; Tell is point-to-point. Each line is sent on the
    channel's native display subtype so the client applies the right prefix and
    colour. Echoes to the sender too (the client does not echo locally).
    """
    subtype, scope, min_level = _CHAT_CHANNELS.get(channel, (_CHAT_SUBTYPE_SAY, "zone", 0))
    if scope == "tell":
        _send_tell(server, conn, text)
        return
    if min_level and _player_level(server, conn) < min_level:
        chan = _CHANNEL_NAMES.get(channel, "this")
        _send_chat(conn, f"You must be level {min_level} to talk in the {chan} channel.")
        return
    sender = _display_name(server, conn)
    line = build_chat_line(sender, text, subtype)
    for other in _chat_audience(server, conn, scope):
        other.send_to_client(line)


# ── Command registry ──────────────────────────────────────────────────────────
CommandFn = Callable[["GameServer", "RRConnection", List[str]], None]
_commands: Dict[str, CommandFn] = {}


def register(name: str, fn: CommandFn) -> None:
    _commands[name.lower()] = fn


def dispatch(server: "GameServer", conn: "RRConnection", msg: str) -> bool:
    """Parse and execute a chat command. Returns True if a command was executed."""
    cmd_name, args = _parse_args(msg)
    if not cmd_name:
        return False

    handler = _commands.get(cmd_name)
    if handler is None:
        _send_chat(conn, f"[ERROR] Unknown command: @{cmd_name}")
        return True

    try:
        handler(server, conn, args)
    except Exception as ex:
        log.error(f"[CMD] @{cmd_name} failed: {ex}")
        _send_chat(conn, f"[ERROR] Command failed: {ex}")

    return True


# ── Built-in commands ─────────────────────────────────────────────────────────

def _cmd_help(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    cmds = sorted(_commands.keys())
    _send_chat(conn, f"Commands: {', '.join(cmds)}")


def _cmd_zone(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, f"Current zone: {conn.current_zone_name} (id=0x{conn.current_zone_id:08X})")
        return
    zone_name = args[0].lower()
    from ..managers.zones import zone_registry
    zone = zone_registry.find_by_name(zone_name)
    if zone:
        # Apply instantly via the real zone-transfer path (despawn, reposition,
        # disconnect+connect so the client reloads) — matches C# @z and
        # RainbowRunner's general.changeZone. The old stub only mutated conn
        # fields and told the player to rejoin, so the teleport never took.
        _send_chat(conn, f"Teleporting to {zone.name}...")
        server.change_zone(conn, zone.name)
    else:
        _send_chat(conn, f"Zone '{zone_name}' not found. Try: town, tutorial")


def _cmd_warp(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    try:
        x = float(args[0]) if args else 0
        y = float(args[1]) if len(args) > 1 else 0
        z = float(args[2]) if len(args) > 2 else 0
    except ValueError:
        _send_chat(conn, "Usage: @warp <x> <y> <z>")
        return
    conn.player_pos_x = x
    conn.player_pos_y = y
    conn.player_pos_z = z
    _send_chat(conn, f"Warped to ({x:.1f}, {y:.1f}, {z:.1f})")


def _cmd_gold(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    try:
        amount = int(args[0]) if args else 1000
    except ValueError:
        _send_chat(conn, "Usage: @gold <amount>")
        return
    saved = character_repository.get_character(conn.char_sql_id)
    if saved:
        saved.gold += amount
        character_repository.save_character(saved)
    _send_chat(conn, f"Added {amount} gold. Total: {saved.gold if saved else '?'}")


def _cmd_level(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    try:
        new_level = max(1, min(100, int(args[0]))) if args else 1
    except ValueError:
        _send_chat(conn, "Usage: @level <1-100>")
        return
    saved = character_repository.get_character(conn.char_sql_id)
    if saved:
        saved.level = new_level
        character_repository.save_character(saved)
        conn.player_level = new_level
    _send_chat(conn, f"Level set to {new_level}")


def _cmd_xp(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    try:
        amount = int(args[0]) if args else 1000
    except ValueError:
        _send_chat(conn, "Usage: @xp <amount>")
        return
    saved = character_repository.get_character(conn.char_sql_id)
    if saved:
        saved.experience += amount
        character_repository.save_character(saved)
    _send_chat(conn, f"Added {amount} XP. Total: {saved.experience if saved else '?'}")


def _cmd_heal(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    saved = character_repository.get_character(conn.char_sql_id)
    if saved:
        saved.current_hp = saved.max_hp or (200 * 256)
        saved.current_mana = saved.max_mana or (200 * 256)
        character_repository.save_character(saved)
    _send_chat(conn, "Fully healed!")


def _cmd_spawn(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    from ..managers import monsters
    if not args:
        _send_chat(conn, "Usage: @spawn <creature_gc_type>")
        return
    gc_type = args[0]
    _send_chat(conn, f"Spawning: {gc_type}")
    # Find the creature in the monster manager.
    monsters.monster_manager.load()
    # Live-spawn into the current instance and broadcast to everyone in it.
    server.world_instances.spawn_monsters_live(server, conn, count=3)


def _cmd_stats(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    saved = character_repository.get_character(conn.char_sql_id)
    if saved:
        hp = (saved.current_hp or saved.max_hp or 0) // 256
        max_hp = (saved.max_hp or 1) // 256
        mp = (saved.current_mana or saved.max_mana or 0) // 256
        max_mp = (saved.max_mana or 1) // 256
        _send_chat(conn,
            f"Lv{saved.level} {saved.class_name} | "
            f"HP:{hp}/{max_hp} MP:{mp}/{max_mp} | "
            f"XP:{saved.experience} Gold:{saved.gold} | "
            f"STR:{saved.stat_strength} AGI:{saved.stat_agility} "
            f"INT:{saved.stat_intellect} END:{saved.stat_endurance}")
    else:
        _send_chat(conn, "No character data loaded.")


def _cmd_item(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @item <gc_type> [rarity 0-5]")
        return
    gc_class = args[0]
    rarity = 0
    if len(args) > 1:
        try:
            rarity = max(0, min(5, int(args[1])))
        except ValueError:
            pass

    from ..data.saved_character import SavedInventoryItem
    saved = character_repository.get_character(conn.char_sql_id)
    if saved is None:
        return

    items = [SavedInventoryItem(
        gc_class=gc_class, x=0, y=0, count=1,
        rarity=rarity, stored_level=get_item_level(gc_class),
    )]
    # Find an empty slot.
    occupied: set[tuple[int, int]] = set()
    for it in (saved.inventory or []):
        occupied.add((it.x, it.y))
    for y in range(8):
        for x in range(10):
            if (x, y) not in occupied:
                items[0].x = x
                items[0].y = y
                occupied.add((x, y))
                break
        else:
            continue
        break

    saved.inventory = (saved.inventory or []) + items
    character_repository.save_character(saved)
    _send_chat(conn, f"Gave item: {gc_class} (rarity={rarity})")


def _cmd_clear(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    saved = character_repository.get_character(conn.char_sql_id)
    if saved:
        saved.inventory = []
        character_repository.save_character(saved)
    _send_chat(conn, "Inventory cleared.")


def _cmd_pos(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    _send_chat(conn,
        f"Position: ({conn.player_pos_x:.1f}, {conn.player_pos_y:.1f}, {conn.player_pos_z:.1f}) "
        f"Zone: {conn.current_zone_name} Heading: {conn.player_heading:.1f}")


# ── Group commands ───────────────────────────────────────────────────────────

def _cmd_invite(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @invite <player_name>")
        return
    if server.groups:
        server.groups.invite(conn, args[0])


def _cmd_accept(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if server.groups:
        server.groups.accept(conn)


def _cmd_decline(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if server.groups:
        server.groups.decline(conn)


def _cmd_leave(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if server.groups:
        server.groups.leave(conn)


def _cmd_kick(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @kick <player_name>")
        return
    if server.groups:
        server.groups.kick(conn, args[0])


def _cmd_leader(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @leader <player_name>")
        return
    if server.groups:
        server.groups.set_leader(conn, args[0])


# ── Register all commands ──
register("help", _cmd_help)
register("z", _cmd_zone)
register("zone", _cmd_zone)
register("warp", _cmd_warp)
register("gold", _cmd_gold)
register("level", _cmd_level)
register("xp", _cmd_xp)
register("heal", _cmd_heal)
register("spawn", _cmd_spawn)
register("stats", _cmd_stats)
register("item", _cmd_item)
register("clear", _cmd_clear)
register("pos", _cmd_pos)
register("invite", _cmd_invite)
register("accept", _cmd_accept)
register("decline", _cmd_decline)
register("leave", _cmd_leave)
register("kick", _cmd_kick)
register("leader", _cmd_leader)


# ── Social commands ──────────────────────────────────────────────────────────

def _cmd_friend(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @friend <player_name>")
        return
    if server.social:
        server.social.add_friend(conn, args[0])


def _cmd_unfriend(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @unfriend <player_name>")
        return
    if server.social:
        server.social.remove_friend(conn, args[0])


def _cmd_ignore(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @ignore <player_name>")
        return
    if server.social:
        server.social.add_ignore(conn, args[0])


def _cmd_unignore(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @unignore <player_name>")
        return
    if server.social:
        server.social.remove_ignore(conn, args[0])


def _cmd_who(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if server.social:
        server.social.send_who(conn)


def _cmd_tell(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if len(args) < 2:
        _send_chat(conn, "Usage: @tell <player_name> <message>")
        return
    if server.social:
        server.social.send_tell(conn, args[0], " ".join(args[1:]))


register("friend", _cmd_friend)
register("unfriend", _cmd_unfriend)
register("ignore", _cmd_ignore)
register("unignore", _cmd_unignore)
register("who", _cmd_who)
register("tell", _cmd_tell)
register("msg", _cmd_tell)


# ── Quest commands ──────────────────────────────────────────────────────────

def _cmd_quest(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    """Quest debug/fallback commands. The normal flow is via NPC dialog; these
    drive the same QuestManager methods + wire packets directly by quest id."""
    usage = "Usage: @quest [list|available|accept <id>|turnin <id>|abandon <id>]"
    if not args:
        _send_chat(conn, usage)
        return
    sub = args[0].lower()
    qm = server.quests
    if qm is None:
        _send_chat(conn, "Quest system not loaded.")
        return
    state = qm.ensure_player_state(conn)

    if sub == "list":
        if state.active_quests:
            for q in state.active_quests:
                tmpl = qm._templates.get(q.quest_id)
                name = tmpl.name if tmpl else q.quest_id
                objs = ", ".join(f"{o.label}: {o.current}/{o.required}"
                                 for o in q.objectives) or "no objectives"
                _send_chat(conn, f"  [{q.instance_id}] {name} — {objs}")
        else:
            _send_chat(conn, "No active quests.")

    elif sub == "available":
        by_npc = qm.available_quests_by_npc(conn)
        total = sum(len(v) for v in by_npc.values())
        if total:
            _send_chat(conn, f"Available quests ({total}) from {len(by_npc)} NPC(s):")
            for npc, hashes in list(by_npc.items())[:10]:
                _send_chat(conn, f"  {npc}: {len(hashes)} quest(s)")
        else:
            _send_chat(conn, "No quests available for your level in this zone.")

    elif sub == "accept" and len(args) > 1:
        from ..managers import quest_wire
        qm.handle_accept_confirmed(conn, 0, quest_wire.quest_hash(args[1]))

    elif sub == "turnin" and len(args) > 1:
        q = next((x for x in state.active_quests
                  if x.quest_id.lower() == args[1].lower()), None)
        if q is None:
            _send_chat(conn, f"You don't have quest: {args[1]}")
        elif not all(o.is_complete for o in q.objectives):
            _send_chat(conn, "Objectives not complete.")
        else:
            qm.handle_turn_in_confirmed(conn, q.instance_id)

    elif sub == "abandon" and len(args) > 1:
        q = next((x for x in state.active_quests
                  if x.quest_id.lower() == args[1].lower()), None)
        if q is None:
            _send_chat(conn, f"Quest not active: {args[1]}")
        else:
            qm.handle_abandon(conn, q.instance_id)

    else:
        _send_chat(conn, usage)


register("quest", _cmd_quest)


# ── Merchant commands ────────────────────────────────────────────────────────

def _cmd_buy(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if len(args) < 2:
        _send_chat(conn, "Usage: @buy <npc_name> <item_slot_id>")
        return

    from ..managers.merchants import merchant_manager
    # Find the NPC's full gc_type from partial name.
    from ..managers.npcs import npc_manager
    npc_manager.load()
    found_gc = None
    search = args[0].lower()
    for gc in npc_manager._npcs:
        if search in gc.lower():
            found_gc = gc
            break
    if found_gc is None:
        _send_chat(conn, f"NPC '{args[0]}' not found. Try: VendorPotion1, VendorWeapon1")
        return

    try:
        slot = int(args[1])
    except ValueError:
        _send_chat(conn, "Usage: @buy <npc_name> <item_slot_id>")
        return

    merchant_manager.buy_item(server, conn, found_gc, slot)


def _cmd_sell(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @sell <inventory_index>")
        return
    try:
        idx = int(args[0])
    except ValueError:
        _send_chat(conn, "Usage: @sell <inventory_index>")
        return

    from ..managers.merchants import merchant_manager
    merchant_manager.sell_item(conn, idx)


def _cmd_shop(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @shop <npc_name> — show merchant's items")
        return

    from ..managers.merchants import merchant_manager
    from ..managers.npcs import npc_manager

    npc_manager.load()
    search = args[0].lower()
    found_gc = None
    for gc in npc_manager._npcs:
        if search in gc.lower():
            found_gc = gc
            break

    if found_gc is None:
        _send_chat(conn, f"NPC '{args[0]}' not found.")
        return

    md = merchant_manager.get_by_npc(found_gc)
    if md is None:
        _send_chat(conn, f"No merchant data for '{args[0]}'.")
        return

    # Make sure dynamic tabs have stock to show.
    merchant_manager.ensure_inventory_for_level(found_gc, max(1, conn.player_level))
    _send_chat(conn, f"--- {md.name}'s Shop ---")
    for inv in md.inventories:
        _send_chat(conn, f"  [{inv.label or inv.name}]")
        for it in inv.items[:10]:
            price = it.price
            if price == 0:
                from ..data import item_catalog
                price = item_catalog.get_buy_price(it.gc_type)
            _send_chat(conn, f"    #{it.item_id}: {it.gc_type.split('.')[-1][:30]} — {price or '?'}g")


register("buy", _cmd_buy)
register("sell", _cmd_sell)
register("shop", _cmd_shop)


# ── Duel commands ───────────────────────────────────────────────────────────

def _cmd_duel(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    if not args:
        _send_chat(conn, "Usage: @duel <player_name>")
        return
    if hasattr(server, 'duels') and server.duels:
        server.duels.challenge(conn, args[0])


register("duel", _cmd_duel)


# ── Bling Gnome command ─────────────────────────────────────────────────────

def _cmd_gnome(server: "GameServer", conn: "RRConnection", args: List[str]) -> None:
    """@gnome — summon / despawn the Bling Gnome (DRS-NET ToggleGnomeFromChat:
    chat toggles existence, unlike the skill cast which opens the convert
    window when he is already out)."""
    gnome = getattr(server, "gnome", None)
    if gnome is None:
        _send_chat(conn, "Bling Gnome system not loaded.")
        return
    if gnome.has_gnome(conn):
        g = gnome.gnome_for(conn)
        if g is not None and g.gold_generated > 0:
            _send_chat(conn, f"[Bling Gnome] Converted {g.items_converted} "
                             f"items into {g.gold_generated} gold!")
        gnome.despawn(conn)
        _send_chat(conn, "[Bling Gnome] Despawned.")
    else:
        gnome.spawn(conn)
        _send_chat(conn, "[Bling Gnome] Summoned!")


def _cmd_gnome_status(server: "GameServer", conn: "RRConnection",
                      args: List[str]) -> None:
    """@gnomestatus / @gs — live gnome counters (DRS-NET ShowGnomeStatus)."""
    gnome = getattr(server, "gnome", None)
    g = gnome.gnome_for(conn) if gnome is not None else None
    if g is None:
        _send_chat(conn, "[Bling Gnome] No gnome active. Use the skill or @gnome.")
        return
    active = " | ACTIVE" if g.is_active else ""
    _send_chat(conn, f"[Bling Gnome] Items: {g.items_converted} | "
                     f"Gold: {g.gold_generated}{active}")


register("gnome", _cmd_gnome)
register("bling", _cmd_gnome)
register("gnomestatus", _cmd_gnome_status)
register("gs", _cmd_gnome_status)
