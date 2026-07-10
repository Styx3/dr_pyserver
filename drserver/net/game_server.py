"""Game server — asyncio port of UnityGameServer.cs (the main body).

Listens on the game port after the auth/queue handoff. The protocol is THCSockets
framing only (no extra crypto past the queue). Lifecycle:

  connect (0x03/0x04)
    -> initial connection channel 0 (OneTimeKey) -> ack + character flow
    -> character channel 4 (list / play)
    -> zone channel 13 progression -> player spawn (0x07..0x46)
    -> movement (entity channel 7, opcode 0x65) + tick self-echo

Shared mutable state (connections, entity-id counter, selected characters) lives
on the GameServer instance and is passed to the spawn / movement modules.
"""
from __future__ import annotations

import asyncio
import re
import socket
from typing import Dict, List, Optional, Tuple

from ..core import log
from ..core.config import ServerConfig
from ..core.sessions import global_sessions, queue_bridge
from ..data.gc_object import GCObject
from ..data import gc_object_factory
from ..db import character_repository
from ..managers.zones import zone_registry, TOWN_ZONE_ID, Zone
from . import framing
from .connection import RRConnection

# ── Per-(zone, instance) world-instance keying (bible §7/§9 item 6) ──────────
# Instance 0 is the SHARED public instance: every player in a peaceful zone keys
# to (zone_id, 0) and sees each other. Private dungeon runs draw a fresh token
# from GameServer.allocate_instance_id() so two players who solo the same dungeon
# get separate copies.
#
# The authoritative "peaceful/shared" signal is the data-driven Zone.is_town flag
# from the client .zone content — it covers town, tutorial, pvp_start AND every
# connecting hub between dungeons (thehub, thehub_oldlinks, thehubportals_dungeon*
# are all is_town=1). The name tokens below are only a fallback for the handful of
# shared social zones the content leaves is_town=0 (pvp_hub, bughub). Real dungeon
# names (dungeonNN_levelMM, elite01, amazon, epic01, boss arenas) are is_town=0 and
# contain none of these fragments → private.
PUBLIC_INSTANCE_ID = 0
_PUBLIC_ZONE_TOKENS = ("town", "tutorial", "hub", "pvp", "lobby")
# Dungeon content is ALWAYS private, even the entrance levels the content flags
# is_town=1 (dungeonNN_level00) and the in-dungeon quest rooms (dNN_lMM_q##) — the
# is_town flag means "peaceful (no random encounters)", which is a SEPARATE axis
# from shared-vs-private. The bible requirement is "a dungeon is private to the
# player", and sharing a still-monster-bearing dungeon zone would reopen the
# Regime-B enrolled-mob crash surface. Matches dungeonNN_*, eliteNN_*, amazon*,
# epicNN*, squeakeasy_*, and the dNN_lMM_q## quest sub-zones.
_DUNGEON_ZONE_RE = re.compile(
    r"^(dungeon\d|elite\d|amazon|epic\d|squeakeasy|d\d\d?_l\d)", re.IGNORECASE)

# Minimum gap (seconds) between avatar control-authority re-asserts. The client's
# attack actions fire faster than this; re-asserting on every swing risks
# disrupting the weapon cycle, so the toggle is throttled. Kept well under the
# spacing between kills/level-ups so authority is always fresh at a level-up.
_CONTROL_REASSERT_INTERVAL = 0.5

# How many control re-asserts to fire over the post-zone-entry window. On a warp
# the avatar arrives with a fresh STEADY action whose input-authority bit
# (action +0x95) is CLEAR, and the spawn-stream toggle only set the transient
# SPAWN action's bit (discarded on the action swap). So the OFF->ON re-assert is
# deferred to the first inbound client packets (steady action live) and repeated
# a few times — throttled to _CONTROL_REASSERT_INTERVAL apart, this spans roughly
# the first BURST*INTERVAL seconds of play, covering the window before a mob can
# path+hit and before the player's first attack would otherwise grant authority.
_CONTROL_REASSERT_BURST = 6

# DISABLED BY DEFAULT (DR_CONTROL_REASSERT=1 to restore). This OFF->ON control
# burst on the first inbound MOVEMENT packets after a warp is a Python-port-only
# workaround for the avatar HP-synch crash — C# DR-Server never bursts control on
# movement (it resets control only on use-target release / as a pending retry).
# Each OFF->ON toggle makes the client relinquish prediction and snap the avatar
# back to the server's authoritative spawn position, so a 6× burst fires right as
# the player starts walking post-warp → the "teleported back 3-5 times on arrival"
# bug. The crash it guarded against is now handled by the live-verified client
# patch (scripts/patch_client_synch_crash.py), so default it off to match C#.
import os as _reassert_os
_CONTROL_REASSERT_ENABLED = _reassert_os.environ.get("DR_CONTROL_REASSERT") == "1"

# Module-level reference for auth server to query player count.
_active_game_server: Optional["GameServer"] = None


class GameServer:
    def __init__(self, config: ServerConfig):
        global _active_game_server
        self.config = config
        self._server: Optional[asyncio.AbstractServer] = None
        self._next_conn_id = 1

        # ── Shared game state ──
        self.connections: Dict[int, RRConnection] = {}
        self.next_entity_id = 10
        # Per-run instance token allocator (bible §7/§9 item 6). 0 is reserved
        # for the shared public instance; private dungeon runs start at 1.
        self.next_instance_id = 1
        self.persistent_characters: Dict[str, List[GCObject]] = {}
        self.selected_character: Dict[str, GCObject] = {}
        self.users: Dict[int, str] = {}
        self.char_list_sent: set[int] = set()

        # Per-viewer remapped behavior ids for multiplayer movement relay:
        #   _remote_behavior_ids[viewer_login][mover_login] = behavior component id
        self.remote_behavior_ids: Dict[str, Dict[str, int]] = {}
        self.remote_avatar_ids: Dict[str, Dict[str, int]] = {}
        # _remote_player_ids[viewer_login][mover_login] = remote Player-object entity
        # id (OP1b), bound as the avatar's ownerID so the viewer's nameplate resolves.
        self.remote_player_ids: Dict[str, Dict[str, int]] = {}
        # _remote_manip_ids[viewer_login][mover_login] = the viewer-side copy's
        # Manipulators component id — the target of live equip/unequip visual
        # relays (net.equipment._relay_equipment_to_viewers).
        self.remote_manip_ids: Dict[str, Dict[str, int]] = {}
        self.player_avatar_entity_id: Dict[str, int] = {}
        # login_name -> own avatar entity id (C# _spawnedAvatarIds). Read back via
        # get_player_avatar_id() when building OTHER players' spawn packets.
        self.spawned_avatar_ids: Dict[str, int] = {}

        # Combat manager (lazy init — needs self reference).
        self.combat = None
        # Combat telemetry channel (set in __main__ when enabled); combat checks
        # its has_active_hook() to decide kill authority.
        self.telemetry = None
        # Quest manager.
        self.quests = None

        # Merchant (vendor) component registry — populated when NPC spawn
        # streams are built. merchant component id -> npc gc_type, and
        # npc entity id -> merchant component id (for click-time arming).
        self.merchant_components: Dict[int, str] = {}
        self.npc_merchant_cids: Dict[int, int] = {}

        # Skill-trainer component registry — trainer component id -> npc
        # gc_type; train purchases route on these cids (managers.trainers).
        self.trainer_components: Dict[int, str] = {}

        # Per-(zone, instance) authoritative world state. Built once, snapshotted
        # to each joiner — never re-spawned per join. See managers/world_instance.
        from ..managers.world_instance import world_instance_registry
        self.world_instances = world_instance_registry

    def get_player_avatar_id(self, login_name: Optional[str]) -> int:
        """Return a player's own avatar entity id, or 0 (C# GetPlayerAvatarId)."""
        if not login_name:
            return 0
        return self.spawned_avatar_ids.get(login_name, 0)

    def allocate_entity_id(self) -> int:
        """Single server-global entity-id allocator (C# ``_nextEntityId++``).

        Every entity the client sees — avatars, components, NPCs, monsters, world
        entities — draws from this one monotonic counter so ids are unique across
        players and zones. (The old per-manager id bases collided with 2+ players;
        one even recycled ``mod 256``.)
        """
        eid = self.next_entity_id
        self.next_entity_id += 1
        return eid

    def allocate_instance_id(self) -> int:
        """Monotonic per-run instance token (mirrors ``allocate_entity_id``).

        ``0`` is reserved for the shared public instance, so private tokens start
        at ``1``. One token == one live dungeon copy; it is stable for the whole
        time a player is inside the level (snapshot, pathmap key, monster ids and
        merchant cids all hang off ``(zone_id, instance_id)``).
        """
        iid = self.next_instance_id
        self.next_instance_id += 1
        return iid

    @staticmethod
    def _is_public_zone(zone_name: str, *, is_town: bool = False) -> bool:
        """True for shared social spaces (everyone keys to instance 0).

        ``is_town`` (data-driven, from the .zone content) is the primary signal —
        it covers town/tutorial/pvp_start and every connecting hub between dungeons
        (thehub*, thehubportals_dungeon*). The name tokens are a fallback for the
        few shared zones the content leaves is_town=0 (pvp_hub, bughub). Dungeon
        content is forced private regardless of is_town (see ``_DUNGEON_ZONE_RE``).
        """
        name = (zone_name or "").lower()
        if _DUNGEON_ZONE_RE.match(name):
            return False
        return is_town or any(tok in name for tok in _PUBLIC_ZONE_TOKENS)

    def _group_instance_token(self, conn: RRConnection) -> Optional[int]:
        """The group's instance token if ``conn`` is grouped, else None.

        Grouped members entering a private zone all land on
        ``(zone_id, group.instance_token)`` — ONE shared dungeon copy (same
        entities, same mobs, same maze) instead of each member minting a
        private copy. Solo players fall through to a fresh private token.
        """
        groups = getattr(self, "groups", None)
        if groups is None:
            return None
        return groups.instance_token_for(conn)

    def _assign_instance_id(self, conn: RRConnection,
                            zone: Optional[Zone] = None) -> None:
        """Set ``conn.instance_id`` for the zone the connection is ENTERING.

        Policy (bible §7 / §9 item 6):
          * public zone (town / tutorial / social hub) → ``PUBLIC_INSTANCE_ID``
          * grouped player in a private zone           → group leader's token
          * solo player in a private zone              → a fresh private token

        Called from the zone-transfer paths ONLY (``_start_zone_join`` /
        ``_transfer_zone``), so the token changes only on zone transfer and stays
        stable across the 13/6 re-join that triggers the snapshot. Before this
        existed ``conn.instance_id`` was always 0, so every player in a dungeon
        shared one instance — the second-player duplication / shared-solo-dungeon
        bug. The per-instance machinery (``world_instance``) was already correct;
        only the key was missing.

        ``zone`` is the destination :class:`Zone` (for its ``is_town`` flag); it
        falls back to a registry lookup so the data-driven peaceful-zone signal is
        always available even if a caller omits it.
        """
        if zone is None:
            zone = zone_registry.get_by_id(conn.current_zone_id)
        zone_name = conn.current_zone_name or (zone.name if zone else "")
        is_town = bool(getattr(zone, "is_town", False))
        if self._is_public_zone(zone_name, is_town=is_town):
            conn.instance_id = PUBLIC_INSTANCE_ID
            return
        token = self._group_instance_token(conn)
        conn.instance_id = (token if token is not None
                            else self.allocate_instance_id())
        log.info(f"[INSTANCE] assigned instance_id={conn.instance_id} to "
                 f"'{conn.login_name}' for zone '{zone_name}'")

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        global _active_game_server
        _active_game_server = self
        zone_registry.load()
        from ..managers import monsters, combat
        monsters.monster_manager.load()
        from ..data.item_stat_database import item_stat_database
        item_stat_database.load()
        self.combat = combat.CombatManager(self)
        from ..managers import quests
        self.quests = quests.QuestManager(self)
        self.quests.load()
        from ..managers import npcs as npc_module
        npc_module.npc_manager.load()
        from ..managers import groups
        self.groups = groups.GroupManager(self)
        # Build marker (guards against a stale python -m drserver process —
        # its absence in the server log means an OLD build is still running).
        log.info(f"[BUILD] 2026-07-09 group wire C#-faithful: spawn-tail "
                 f"priming {'ACTIVE' if groups.GROUP_PRIME_ENABLED else 'off (DR_GROUP_PRIME=0)'} "
                 f"+ talkback 0x50; 13/1+OP3/OP1b carry charSqlId")
        from ..managers import world_entities as we_module
        we_module.world_entity_manager.load()
        from ..managers import social
        self.social = social.SocialManager(self)
        self.social.load()
        from ..managers.merchants import merchant_manager
        merchant_manager.load()
        from ..managers.portals import portal_manager
        portal_manager.load()
        from ..managers import duels
        self.duels = duels.DuelManager(self)
        from ..managers.bling_gnome import BlingGnomeManager
        self.gnome = BlingGnomeManager(self)
        from ..managers.summons import SummonManager
        self.summons = SummonManager(self)
        self._server = await asyncio.start_server(
            self._on_client, self.config.game_server_ip, self.config.game_server_port
        )
        log.info(f"[GAME] listening on {self.config.game_server_ip}:{self.config.game_server_port}")

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn_id = self._next_conn_id
        self._next_conn_id += 1

        # Disable Nagle's algorithm. Movement is a stream of tiny packets (~30-40 B)
        # sent at 30 Hz; with Nagle on (the default) the OS coalesces them and the
        # client's delayed-ACK stalls each segment up to ~200 ms, which shows up as
        # movement that smooths out at first then turns choppy after ~10-15 s.
        # DR runs on the L2 TCP engine (no UDP), so TCP_NODELAY is the correct fix.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as ex:  # pragma: no cover - platform dependent
                log.warn(f"[GAME] conn {conn_id}: could not set TCP_NODELAY: {ex}")

        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "0.0.0.0"
        if client_ip.startswith("::ffff:"):
            client_ip = client_ip[len("::ffff:"):]
        log.info(f"[GAME] new TCP connection #{conn_id} from {client_ip}:{peer[1] if peer else '?'}")
        conn = RRConnection(conn_id, reader, writer)
        self.connections[conn_id] = conn

        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "0.0.0.0"
        if client_ip.startswith("::ffff:"):
            client_ip = client_ip[len("::ffff:"):]

        # Queue handoff: the auth server registered this IP -> login name.
        queued_user = queue_bridge.check_and_consume_queue_ip(client_ip)
        if queued_user:
            conn.login_name = queued_user
            log.info(f"[GAME] conn {conn_id} from {client_ip} -> queued user '{queued_user}'")
        else:
            # Direct connection — username will be resolved via OneTimeKey on channel 0.
            log.info(f"[GAME] conn {conn_id} from {client_ip} (no queue entry, awaiting OneTimeKey)")

        try:
            await self._read_loop(conn)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as ex:  # noqa: BLE001
            log.error(f"[GAME] conn {conn_id} handler error: {ex}")
        finally:
            self._on_disconnect(conn)

    async def _read_loop(self, conn: RRConnection) -> None:
        buf = bytearray()
        first_packet = True
        while conn.is_connected:
            chunk = await conn.reader.read(8192)
            if not chunk:
                break
            if first_packet:
                log.info(f"[GAME] conn {conn.conn_id} first bytes: {chunk[:32].hex()}")
                first_packet = False
            buf.extend(chunk)
            frames = framing.split_frames(bytes(buf))
            consumed = sum(len(f) for f in frames)
            del buf[:consumed]
            for frame in frames:
                self._process_single_message(conn, frame)
            await conn.writer.drain()

    def _on_disconnect(self, conn: RRConnection) -> None:
        log.info(f"[GAME] conn {conn.conn_id} disconnected")
        queue_bridge.player_disconnected()
        tick_task = getattr(conn, "_tick_task", None)
        if tick_task is not None:
            tick_task.cancel()
        # ROUTE 2B: drop this player's active weapon-cycle replay so a stale
        # cycle never references a torn-down monster after the conn is gone.
        if getattr(self, "combat", None) is not None and conn.login_name:
            self.combat.clear_combat(conn.login_name)
        # Tear down this player's Bling Gnome + summons (peers get the entity
        # removes — DRS-NET OnPlayerDisconnect).
        if conn.login_name:
            for manager_attr in ("gnome", "summons"):
                manager = getattr(self, manager_attr, None)
                if manager is None:
                    continue
                try:
                    manager.cleanup(conn)
                except Exception as ex:  # noqa: BLE001
                    log.debug(f"[{manager_attr.upper()}] disconnect cleanup failed: {ex}")
        # MULTIPLAYER: despawn this player's avatar from everyone still in the zone
        # (and clear the remap maps) BEFORE removing the connection. C#
        # BroadcastEntityRemove — without it other clients keep a frozen ghost.
        if conn.is_spawned and conn.login_name:
            try:
                from . import spawn
                spawn.broadcast_entity_remove(self, conn)
            except Exception as ex:  # noqa: BLE001
                log.error(f"[MULTIPLAYER] entity remove failed: {ex}")
            # Tear the world instance down once its last player leaves so it
            # repopulates cleanly next time (and combat state is released).
            try:
                self.world_instances.leave(self, conn)
            except Exception as ex:  # noqa: BLE001
                log.error(f"[INSTANCE] leave failed for '{conn.login_name}': {ex}")
        # Release this session's runtime quest state (instance ids etc.).
        if getattr(self, "quests", None) is not None:
            try:
                self.quests.remove_player(conn)
            except Exception as ex:  # noqa: BLE001
                log.debug(f"[QUEST] remove_player failed: {ex}")
        # Group: keep the roster seat, grey it out for the others (0x49). The
        # member can reconnect into the same group (and its dungeon copy).
        if getattr(self, "groups", None) is not None and conn.login_name:
            try:
                self.groups.on_disconnect(conn)
            except Exception as ex:  # noqa: BLE001
                log.debug(f"[GROUP] disconnect cleanup failed: {ex}")
        self.connections.pop(conn.conn_id, None)
        self.char_list_sent.discard(conn.conn_id)
        if conn.login_name:
            self.remote_behavior_ids.pop(conn.login_name, None)
            self.remote_avatar_ids.pop(conn.login_name, None)
            self.remote_player_ids.pop(conn.login_name, None)
            self.remote_manip_ids.pop(conn.login_name, None)
            self.spawned_avatar_ids.pop(conn.login_name, None)
        conn.disconnect()

    # ── Frame dispatch ───────────────────────────────────────────────────────
    def _process_single_message(self, conn: RRConnection, data: bytes) -> None:
        if not data:
            return
        message_type = data[0]

        # Extract the client's peer_id from the first frame we process.
        # The client does NOT send a 0x03 Connect frame after the queue
        # handoff — it goes straight to 0x0A CompressedA.  We steal the
        # 24-bit peer field from bytes 1-3 so outbound CompressedA frames
        # carry the correct peer_id the client expects.
        if conn.peer_id24 == 0 and len(data) >= 4:
            if message_type in (0x0A, 0x0E):
                conn.peer_id24 = int.from_bytes(data[1:4], "little")
            elif message_type == 0x10:
                conn.peer_id24 = int.from_bytes(data[1:4], "little")

        if message_type == 0x02:
            if conn.is_spawned:
                log.debug(f"[PING] conn {conn.conn_id} ping after spawn ({len(data)} bytes) — client alive")
            conn.send_raw(framing.build_ping_response(data))
        elif message_type == 0x03:
            self._handle_connect(conn, data)
        elif message_type == 0x0A:
            self._dispatch_channel(conn, framing.parse_compressed_a(data))
        elif message_type == 0x0E:
            msg = framing.parse_compressed_e(data)
            if msg is not None:
                self._dispatch_channel(conn, msg)
        elif message_type == 0x10:
            self._dispatch_channel(conn, framing.parse_direct(data))
        else:
            log.warn(f"[GAME] unknown frame type 0x{message_type:02X} len={len(data)}")

    def _handle_connect(self, conn: RRConnection, data: bytes) -> None:
        client_id = framing.read_connect_client_id(data)
        conn.peer_id24 = client_id
        conn.send_raw(framing.build_connect_response(client_id))
        log.debug(f"[GAME] conn {conn.conn_id} connect, peer=0x{client_id:06X}")

    def _handle_chat_channel(self, conn: RRConnection, message_type: int, data: bytes) -> None:
        """Handle channel 3 (chat) — dispatch commands or broadcast messages."""
        from . import chat_commands

        reader = framing.LEReader(data)
        sub_channel = message_type      # 0x01=World, 0x02=Zone, 0x03=Group, 0x04=Tell, 0x06=Noob

        if not reader.has_data:
            return

        msg_text = reader.read_cstring()
        if not msg_text:
            return

        log.debug(f"[CHAT] conn {conn.conn_id} ch=0x{sub_channel:02X} msg='{msg_text[:50]}'")

        # ── Item-link capture (shift-click an item into chat) ──────────────
        # The link wire format is UNVERIFIED (no reference in C#/Go). If the
        # client appends binary after the message cstring, or embeds control
        # bytes in the text, log it verbatim so a single live shift-click
        # gives us the format to port. Until then the text round-trips as-is.
        if reader.remaining > 0:
            log.info(f"[CHAT-LINK?] conn {conn.conn_id} ch=0x{sub_channel:02X} "
                     f"trailing {reader.remaining}B after text: "
                     f"{data[len(data) - reader.remaining:].hex()}")
        if any(ord(c) < 0x20 for c in msg_text):
            log.info(f"[CHAT-LINK?] conn {conn.conn_id} control bytes in text: "
                     f"{msg_text.encode('utf-8', 'replace').hex()}")

        # Try command dispatch first.
        if msg_text.startswith("@"):
            if chat_commands.dispatch(self, conn, msg_text):
                return

        # Regular chat — broadcast to zone.
        chat_commands._send_chat_zone(self, conn, msg_text, sub_channel)

        # Chat log hook for admin panel.
        try:
            from ..admin.admin_server import hook_chat_message
            ch_name = {0x01: "world", 0x02: "zone", 0x03: "group", 0x04: "tell", 0x06: "noob"}.get(sub_channel, "say")
            hook_chat_message(conn.login_name or "?", msg_text, ch_name, conn.current_zone_name)
        except Exception:
            pass

    def _dispatch_channel(self, conn: RRConnection, msg: framing.ChannelMessage) -> None:
        ch = msg.channel
        # Inbound channel/type trace (excludes ch7 movement + the ch0 heartbeat).
        # DEBUG, not INFO: this is on the inbound packet path and log.* writes to
        # stdout synchronously, blocking the asyncio event loop — at INFO it taxes
        # every frame the client sends during play. Enable with DR_LOG_LEVEL=DEBUG.
        if not (ch == 7 or (ch == 0 and msg.message_type == 0x02 and not msg.payload)):
            log.debug(f"[ch-trace] ch={ch} type=0x{msg.message_type:02X} "
                      f"len={len(msg.payload)} head={msg.payload[:8].hex()}")
        if ch == 0:
            self._handle_initial_connection(conn, msg.message_type, msg.payload)
        elif ch == 3:
            # C# routes channel 3 to SocialManager (friends/rosters/tells) —
            # NOT chat. Chat is channel 6 (see below). Routing social binary
            # through the chat handler would parse it as garbage chat text.
            self._handle_social_channel(conn, msg.message_type, msg.payload)
        elif ch == 4:
            self._handle_character_channel(conn, msg.message_type, msg.payload)
        elif ch == 6:
            # Channel 6 = chat (+ @admin commands, which ride the chat channel).
            # The client sends chat here; the old admin stub silently dropped it.
            self._handle_chat_channel(conn, msg.message_type, msg.payload)
        elif ch == 7:
            from . import movement
            movement.handle(self, conn, msg.message_type, msg.payload)
        elif ch == 9 or ch == 0x0B:
            # Group / party. ★ The client sends group requests (invite 0x12/
            # 0x16, accept 0x20, leave 0x22, kick/leader/difficulty, PvP
            # 0x29-0x2F) on **channel 9** — matching the server's own outbound
            # group packets (inner byte 0x09) and the client's shared group
            # channel object. C# routes BOTH `case 9` (HandleGroupChannel) and
            # `case 11` (HandleGroupClientChannel) here. We previously listened
            # only on 0x0B, so every client→server group message landed in the
            # silent "unhandled channel" branch below and was dropped — the
            # root cause of "invite/accept do nothing" (server→client 0x32 via
            # @invite still worked, since that path needs no inbound).
            self._handle_group_channel(conn, msg.message_type, msg.payload)
        elif ch == 0x0C:
            self._handle_social_channel(conn, msg.message_type, msg.payload)
        elif ch == 13:
            # QuestManager/Zone channel. C# routes by message type: recall
            # requests (0x07 direct, 0x0C obelisk, 0x0B saved place) vs the
            # zone-load progression (0x06 join + 0x00/0x01/0x08 zone msgs).
            if msg.message_type == 0x07:
                self._handle_checkpoint_teleport(conn, msg.payload)
            elif msg.message_type == 0x0C:
                self._handle_obelisk_teleport(conn)
            elif msg.message_type == 0x0B:
                self._handle_saved_place_teleport(conn)
            elif msg.message_type == 0x08 and conn.is_spawned:
                # The death / sync-error "Respawn" button. Live wire capture
                # (2026-06-01) proved the client sends it here as ch13/0x08
                # body=01 — NOT as the ch7/0x04 entity-request the C# server
                # assumes (movement._handle_entity_request stays as a harmless
                # C#-faithful fallback). Both servers previously dumped 0x08 into
                # the zone-load catch-all, so the button did nothing. Guarded on
                # is_spawned so a stray pre-spawn 0x08 still reaches zone-load.
                self.request_respawn(conn)
            else:
                self._handle_zone_channel(conn, msg.payload)
        else:
            log.debug(f"[GAME] unhandled channel {ch} type=0x{msg.message_type:02X}")

    # ── Channel 0: initial connection ────────────────────────────────────────
    def _handle_initial_connection(self, conn: RRConnection, message_type: int, data: bytes) -> None:
        if message_type == 0x02 and len(data) == 0:
            return  # heartbeat
        if message_type != 0x00 or len(data) < 5:
            log.warn(f"[GAME] initial conn unhandled type=0x{message_type:02X} len={len(data)}")
            return

        reader = framing.LEReader(data)
        reader.read_byte()                  # subtype
        one_time_key = reader.read_uint32()

        user = global_sessions.try_consume(one_time_key)
        if not user:
            if conn.login_name:
                user = conn.login_name       # queue connection — no OneTimeKey needed
            else:
                log.error(f"[GAME] invalid OneTimeKey 0x{one_time_key:08X}")
                return

        conn.login_name = user
        self.users[conn.conn_id] = user
        log.info(f"[GAME] initial login for '{user}'")

        conn.send_message_0x10(0x0A, bytes([0x03]))               # advance ack

        advance = framing.LEWriter()
        advance.write_uint24(0x00B2B3B4)
        advance.write_byte(0x00)
        conn.send_compressed_a(0x00, 0x03, advance.to_array())     # A-lane advance

        self._start_character_flow(conn)

    def _start_character_flow(self, conn: RRConnection) -> None:
        saved = character_repository.get_characters_for_account(conn.login_name) or []
        chars: List[GCObject] = []
        for sc in saved:
            chars.append(GCObject(id=sc.id, native_class="Player", gc_class="Player", name=sc.name))
        self.persistent_characters[conn.login_name] = chars
        log.info(f"[GAME] character flow: {len(chars)} characters for '{conn.login_name}'")

    # ── Channel 4: character ─────────────────────────────────────────────────
    def _handle_character_channel(self, conn: RRConnection, message_type: int, data: bytes) -> None:
        log.info(f"[CHAR] conn {conn.conn_id} msg_type=0x{message_type:02X} len={len(data)} data={data[:32].hex()}")
        if message_type == 0:                       # CharacterConnected request
            conn.send_to_client(bytes([4, 0]))
        elif message_type == 1:                     # UI nudge
            w = framing.LEWriter()
            w.write_byte(4)
            w.write_byte(1)
            w.write_uint32(0)
            conn.send_to_client(w.to_array())
        elif message_type == 2:                     # CreateCharacter
            self._handle_character_create(conn, data)
        elif message_type == 3:                     # Get character list
            self._send_character_list(conn)
        elif message_type == 4:                     # DeleteCharacter
            self._handle_character_delete(conn, data)
        elif message_type == 5:                     # Play character
            self._handle_character_play(conn, data)
        else:
            log.debug(f"[GAME] unhandled character msg 0x{message_type:02X}")

    def _send_character_list(self, conn: RRConnection) -> None:
        chars = self.persistent_characters.get(conn.login_name)
        if not chars:
            self._start_character_flow(conn)
            chars = self.persistent_characters.get(conn.login_name, [])

        if not chars:
            conn.send_to_client(bytes([4, 3, 0]))
            self.char_list_sent.add(conn.conn_id)
            return

        saved_next = self.next_entity_id
        self.next_entity_id = 10000

        for character in sorted(chars, key=lambda c: c.id):
            saved_char = character_repository.get_character(character.id)
            if saved_char is None:
                continue

            temp_char = gc_object_factory.new_player(character.name)
            temp_char.id = character.id

            self._build_send_player_objects(temp_char, saved_char)

            w = framing.LEWriter()
            w.write_byte(4)
            w.write_byte(2)
            w.write_uint32(character.id)
            w.write_cstring(character.name)
            temp_char.write_full_gc_object(w)
            conn.send_to_client(w.to_array())

        self.char_list_sent.add(conn.conn_id)
        self.next_entity_id = max(saved_next, self.next_entity_id)
        log.info(f"[GAME] sent character list ({len(chars)}) to '{conn.login_name}'")

    def _build_send_player_objects(self, gc_char: GCObject, saved_char) -> tuple:
        """Build the avatar and proc_mod with IDs, and attach as children to gc_char. Returns (avatar, proc_mod)."""
        avatar = gc_object_factory.load_avatar(saved_char)
        avatar.id = self.next_entity_id
        self.next_entity_id += 1
        for child in avatar.children:
            child.id = self.next_entity_id
            self.next_entity_id += 1
            for grandchild in child.children:
                grandchild.id = self.next_entity_id
                self.next_entity_id += 1

        proc_mod = gc_object_factory.new_proc_modifier()
        proc_mod.id = self.next_entity_id
        self.next_entity_id += 1

        gc_char.add_child(avatar)
        gc_char.add_child(proc_mod)

        return avatar, proc_mod

    def _write_send_player(self, w: framing.LEWriter, gc_char: GCObject, avatar: GCObject) -> None:
        """Write sendPlayer data: character DFC, avatar DFC (siblings), trailing bytes."""
        gc_char.write_full_gc_object(w)
        avatar.write_full_gc_object(w)
        w.write_byte(0x01)
        w.write_byte(0x01)
        w.write_cstring("Normal")
        w.write_byte(0x01)
        w.write_byte(0x01)
        w.write_uint32(0x01)    # unknown, 1 by default

    def _handle_character_play(self, conn: RRConnection, data: bytes) -> None:
        chars = self.persistent_characters.get(conn.login_name)
        if not chars:
            log.error(f"[GAME] no characters for '{conn.login_name}'")
            return

        character = None
        if data and len(data) >= 4:
            selected_id = framing.LEReader(data).read_uint32()
            character = next((c for c in chars if c.id == selected_id), None)
        if character is None:
            character = chars[0]

        self.selected_character[conn.login_name] = character
        conn.char_sql_id = character.id
        from ..db import character_repository
        conn.unlocked_checkpoints = set(character_repository.load_checkpoints(character.id))
        # The starter waystones (Town + Tutorial) are ALWAYS unlocked. The old
        # fallback only fired when the set was EMPTY, so a character that
        # discovered a dungeon obelisk before ever activating the town one never
        # had TownCheckpoint in its QuestManager list — the client then has no
        # menu entry for the town obelisk and falls back to the bare teleport
        # request, which the rotator services by cycling to the next waystone
        # (insta-teleport to tutorial instead of opening the menu).
        from ..managers.checkpoints import DEFAULT_CHECKPOINTS
        missing = DEFAULT_CHECKPOINTS - conn.unlocked_checkpoints
        if missing:
            conn.unlocked_checkpoints |= missing
            for cp in sorted(missing):
                character_repository.add_checkpoint(character.id, cp)
        # Restore a persisted town-portal return point (waypoint scroll).
        from ..managers import town_portal
        town_portal.load_tp_state(conn, character)
        log.info(f"[GAME] selected character '{character.name}' (id={character.id}); "
                 f"{len(conn.unlocked_checkpoints)} checkpoint(s) unlocked")

        conn.send_to_client(bytes([4, 5]))          # play ack
        conn.send_to_client(bytes([9, 0x30]))       # group connected (processConnected)
        self._start_zone_join(conn)

    def _handle_character_create(self, conn: RRConnection, data: bytes) -> None:
        """Handle CreateCharacter (msg type 0x02).

        Client sends: name cstr, class cstr, then 5 appearance bytes:
        skin, face, faceFeature, hair, hairColor (C# GameServer.Mover.cs
        HandleCharacterCreate reads exactly these five, in this order).
        """
        reader = framing.LEReader(data)
        char_name = reader.read_cstring()
        avatar_class = reader.read_cstring() if reader.remaining > 0 else ""
        skin = face = face_feature = hair = hair_color = 0
        if reader.remaining >= 5:
            skin = reader.read_byte()
            face = reader.read_byte()
            face_feature = reader.read_byte()
            hair = reader.read_byte()
            hair_color = reader.read_byte()

        log.info(f"[GAME] create character: '{char_name}' class='{avatar_class}' "
                 f"skin={skin} face={face} ff={face_feature} hair={hair} hc={hair_color}")

        from ..data.gc_object_factory import get_avatar_gc_class
        class_name = "Fighter"
        if "Fighter" in avatar_class:
            class_name = "Fighter"
        elif "Warlock" in avatar_class:
            class_name = "Mage"
        elif "Ranger" in avatar_class:
            class_name = "Ranger"

        from ..db import account_repository, character_repository
        account_id = account_repository.get_account_id(conn.login_name)
        if account_id == 0:
            account_id = account_repository.create_account(conn.login_name, "")

        saved = character_repository.create_character(
            char_name, class_name, account_id, conn.login_name, avatar_class,
            skin=skin, face=face, face_feature=face_feature,
            hair=hair, hair_color=hair_color)
        if saved is None:
            log.error(f"[GAME] failed to create character '{char_name}'")
            return

        gc_obj = GCObject(id=saved.id, native_class="Player", gc_class="Player", name=saved.name)
        if conn.login_name not in self.persistent_characters:
            self.persistent_characters[conn.login_name] = []
        self.persistent_characters[conn.login_name].append(gc_obj)

        # Send CharacterCreate response: [4, 2, id, name_cstr, Player_full_DFC]  (C# format)
        saved_next = self.next_entity_id
        self.next_entity_id = 10000

        temp_char = gc_object_factory.new_player(char_name)
        temp_char.id = saved.id

        self._build_send_player_objects(temp_char, saved)

        w = framing.LEWriter()
        w.write_byte(4)
        w.write_byte(2)
        w.write_uint32(saved.id)
        w.write_cstring(char_name)
        temp_char.write_full_gc_object(w)
        conn.send_to_client(w.to_array())
        self.next_entity_id = max(saved_next, self.next_entity_id)

        # Re-send full character list.
        self._send_character_list(conn)

    def _handle_character_delete(self, conn: RRConnection, data: bytes) -> None:
        """Handle DeleteCharacter (character channel type 0x04).

        The client sends ``[cstring name][uint32 id]`` (confirmed live against
        the client 2026-06-04: e.g. ``53 74 79 78 33 00 | 01 00 00 00`` =
        "Styx3", id 1). We only delete a character the login actually OWNS — the
        in-memory ``persistent_characters[login]`` list is loaded per-account, so
        it is the authority on ownership. (The C# emulator deletes by raw id with
        no ownership check; we don't copy that hole.) ``delete_character`` cascades
        to every ``character_id`` child table via ``ON DELETE CASCADE``.

        Response mirrors the create path: an ack ``[0x04,0x04][uint32 id]`` on the
        default SendCompressedA, then a fresh character list so the client UI
        rebuilds from authoritative data.
        """
        reader = framing.LEReader(data)
        char_name = reader.read_cstring() if reader.remaining > 0 else ""
        char_id = reader.read_uint32() if reader.remaining >= 4 else 0

        chars = self.persistent_characters.get(conn.login_name, [])
        owned = next((c for c in chars if c.id == char_id), None)
        if owned is None:
            log.warn(f"[GAME] delete refused: '{conn.login_name}' does not own "
                     f"char id={char_id} (name='{char_name}')")
            self._send_character_list(conn)
            return

        from ..db import character_repository
        if not character_repository.delete_character(char_id):
            log.error(f"[GAME] delete failed for char id={char_id}")
            self._send_character_list(conn)
            return

        chars.remove(owned)
        sel = self.selected_character.get(conn.login_name)
        if sel is not None and getattr(sel, "id", None) == char_id:
            self.selected_character.pop(conn.login_name, None)
        log.info(f"[GAME] deleted character '{char_name}' (id={char_id}) "
                 f"for '{conn.login_name}'")

        # Delete ack [4, 4, uint32 id].
        w = framing.LEWriter()
        w.write_byte(4)
        w.write_byte(4)
        w.write_uint32(char_id)
        conn.send_to_client(w.to_array())

        self._send_character_list(conn)

    def _start_zone_join(self, conn: RRConnection) -> None:
        """Begin zone join flow after character selection."""
        start_zone = zone_registry.find_by_name("tutorial")
        zone_id = start_zone.id if start_zone else TOWN_ZONE_ID
        zone_name = start_zone.name if start_zone else "tutorial"
        conn.current_zone_id = zone_id
        conn.current_zone_name = zone_name
        conn.current_zone_gc_type = self._zone_gc_type(zone_name)
        self._assign_instance_id(conn, start_zone)

        from ..managers import dungeon_spawner
        w = framing.LEWriter()
        w.write_byte(13)
        w.write_byte(0)
        w.write_cstring("tutorial")
        # Per-level maze layout seed (the client builds its maze from this); the
        # spawner derives the same value from the zone name so mobs match.
        w.write_uint32(dungeon_spawner.layout_seed(zone_name))
        w.write_byte(0x01)
        w.write_byte(0xFF)
        w.write_cstring("")
        w.write_uint32(0x00)
        conn.send_to_client(w.to_array())
        log.info(f"[GAME] sent zone info -> {zone_name} (id=0x{zone_id:08X})")

    def change_zone(self, conn: RRConnection, target_zone: str,
                    spawn_point: str = "") -> None:
        """Transfer a player to another zone at a named spawn-point waypoint —
        port of C# ChangeZone. Falls back to the target zone's default spawn when
        the waypoint is missing (matches the C# PendingSpawn fallback)."""
        self._transfer_zone(conn, target_zone, spawn_point=spawn_point)

    def change_zone_to_position(self, conn: RRConnection, target_zone: str,
                                pos_x: float, pos_y: float, pos_z: float) -> None:
        """Transfer a player to an explicit position — port of C#
        ChangeZoneToPosition (used by the checkpoint-menu recall)."""
        self._transfer_zone(conn, target_zone, position=(pos_x, pos_y, pos_z))

    def request_respawn(self, conn: RRConnection) -> None:
        """Respawn the player to the hub — port of C# HandleClientRequestRespawn.

        Sent by the client as an entity-request (channel-7 opcode 0x04) with no
        payload, e.g. the death / sync-error "Respawn" button. C# restores full HP
        then ChangeZoneToPosition(respawnZone). The respawn zone is the current
        zone's ``respawn_zone`` column; dungeons point at the shared town hub, so
        we default to ``town`` when unset (previously unhandled — the request was
        dropped, which is why "Respawn" did nothing and the player was stranded in
        the dungeon)."""
        current = zone_registry.get_by_id(conn.current_zone_id)
        target = (current.respawn_zone if current and current.respawn_zone else "town")
        log.info(f"[RESPAWN] '{conn.login_name}' {conn.current_zone_gc_type} -> {target}")
        self.change_zone(conn, target)

    def _transfer_zone(self, conn: RRConnection, target_zone: str, *,
                       spawn_point: str = "",
                       position: Optional[Tuple[float, float, float]] = None) -> None:
        """Shared zone-transfer core for portals and checkpoint recall.

        Resolves the target zone FIRST so a bad destination warns the player
        (system message) and leaves them in place instead of silently stranding
        them. Then despawns the avatar, resets spawn state, repositions, and
        re-sends the zone disconnect+connect so the client reloads and the spawn
        progression re-runs. Any unexpected error is caught and surfaced to the
        player rather than killing the connection.
        """
        from . import spawn
        from ..managers import dungeon_spawner

        try:
            target = zone_registry.find_by_name(target_zone)
            if target is None:
                log.warn(f"[CHANGE-ZONE] unknown destination '{target_zone}' for "
                         f"'{conn.login_name}'")
                conn.send_system_message(f"Cannot travel: unknown destination '{target_zone}'.")
                conn.allow_flush = True
                return

            # Same-zone respawn: the live native client will NOT reload the zone
            # it is already in — it never replies with the 13/6 join, so the
            # disconnect(13/02)+connect(13/00) reload below would strand the
            # player ("there was an error talking to the server."). Every
            # self-referential respawn_zone (town/tutorial/thehub/pvp_*) hits
            # this. Reposition in place instead — no zone reload. (C# always
            # reloads, but its live-tested clients differ; the drserver live
            # capture 2026-06-01 proved the same-zone reload stalls.)
            if conn.is_spawned and target.id == conn.current_zone_id:
                self._respawn_in_place(conn, target, spawn_point=spawn_point,
                                       position=position)
                return

            old_zone = conn.current_zone_name

            # Despawn from the old instance so other players don't keep a ghost.
            if conn.is_spawned and conn.login_name:
                # Bling Gnome + summons die on zone change (DeathOnZone) —
                # silent state drop + peer despawn (DRS-NET
                # CleanupForZoneTransition).
                for manager_attr in ("gnome", "summons"):
                    manager = getattr(self, manager_attr, None)
                    if manager is None:
                        continue
                    try:
                        manager.cleanup(conn)
                    except Exception as ex:  # noqa: BLE001
                        log.debug(f"[{manager_attr.upper()}] zone cleanup failed: {ex}")
                try:
                    spawn.broadcast_entity_remove(self, conn)
                except Exception as ex:  # noqa: BLE001
                    log.error(f"[CHANGE-ZONE] entity remove failed: {ex}")
                try:
                    self.world_instances.leave(self, conn)
                except Exception as ex:  # noqa: BLE001
                    log.error(f"[CHANGE-ZONE] instance leave failed: {ex}")

            # Stop the movement tick; the new progression restarts it.
            tick_task = getattr(conn, "_tick_task", None)
            if tick_task is not None:
                tick_task.cancel()
                conn._tick_task = None

            # ROUTE 2B: drop the weapon-cycle replay for the old zone's monsters.
            if getattr(self, "combat", None) is not None and conn.login_name:
                self.combat.clear_combat(conn.login_name)

            # Reset spawn state and clear per-zone multiplayer maps for this player.
            conn.is_spawned = False
            conn.allow_flush = False
            conn.message_queue.clear()
            # Disarm the merchant-restock watchdog: the armed merchant cid is
            # per-zone, so once the player leaves the vendor's zone the
            # once-a-second flush would push a 0x35 <cid> 0x1E/0x1F refresh into
            # the new zone where that component does not resolve -> "Invalid
            # ComponentID" -> Zone communication error Code 9 (live 2026-06-17:
            # tutorial HermitVendor refresh fired while fighting in a dungeon).
            conn.active_merchant_npc = None
            conn.active_merchant_cid = 0
            conn.active_merchant_due = 0.0
            # Same per-zone-cid hazard for the debounced post-buy refill: cancel
            # it so a pending top-up never fires into the new zone (Code 9).
            conn.merchant_refill_npc = None
            conn.merchant_refill_cid = 0
            conn.merchant_refill_due = 0.0
            if conn.login_name:
                self.remote_behavior_ids.pop(conn.login_name, None)
                self.remote_avatar_ids.pop(conn.login_name, None)
                self.remote_player_ids.pop(conn.login_name, None)
                self.remote_manip_ids.pop(conn.login_name, None)
                self.spawned_avatar_ids.pop(conn.login_name, None)

            # Point at the new zone.
            conn.current_zone_id = target.id
            conn.current_zone_name = target.name
            conn.current_zone_gc_type = self._zone_gc_type(target.name)
            # Key the per-instance world: public zones share instance 0; a fresh
            # private dungeon run mints its own token (the old shared-(zone,0)
            # bug). The same-zone respawn above returns BEFORE this, so an
            # in-place respawn keeps its current instance.
            self._assign_instance_id(conn, target)
            # NB: conn.zone_portal_source is deliberately NOT set here. It feeds
            # the obelisk menu's "Recent Zone Portal" saved place and C# only
            # updates it when WALKING THROUGH a portal (HandlePortalActivation),
            # not on obelisk recalls/admin warps — setting it on every transfer
            # made "Town" show up under Saved Places after any teleport.

            # Resolve the spawn position: explicit > named waypoint > zone default.
            if position is not None:
                conn.player_pos_x, conn.player_pos_y, conn.player_pos_z = position
            elif spawn_point:
                from ..managers.portals import portal_manager
                wp = portal_manager.find_waypoint(target.name, spawn_point)
                if wp is not None:
                    conn.player_pos_x = wp.pos_x
                    conn.player_pos_y = wp.pos_y
                    conn.player_pos_z = wp.pos_z
                else:
                    log.warn(f"[CHANGE-ZONE] waypoint '{spawn_point}' not found in "
                             f"'{target.name}' — using zone default spawn")
                    conn.player_pos_x, conn.player_pos_y, conn.player_pos_z = (
                        target.spawn_x, target.spawn_y, target.spawn_z)
            else:
                conn.player_pos_x, conn.player_pos_y, conn.player_pos_z = (
                    target.spawn_x, target.spawn_y, target.spawn_z)

            # Procedural dungeon: the maze is anchored at world origin and
            # regenerated per layout seed, so the static .zone/waypoint XY never
            # lands inside it. Place the player at the entrance the maze actually
            # built (the room node whose SpawnName matches the portal's spawn
            # point, else the start room / main entrance). Keep the resolved Z —
            # the floor pathmap isn't built until the instance populates, and the
            # client settles height on arrival.
            if dungeon_spawner.is_procedural_zone(target.name):
                entry = dungeon_spawner.entry_position(target.name, spawn_point)
                if entry is not None:
                    conn.player_pos_x, conn.player_pos_y = entry[0], entry[1]
                    if entry[2] is not None:
                        conn.player_pos_z = entry[2]
                    conn.player_heading = entry[3]   # authored facing (into the room)
                    log.info(f"[CHANGE-ZONE] '{conn.login_name}' maze entry "
                             f"'{target.name}'@{spawn_point or '(entrance)'} "
                             f"-> ({entry[0]:.0f},{entry[1]:.0f},{conn.player_pos_z:.0f}) "
                             f"h={entry[3]:.0f}")

            # Hand the resolved position + facing to the avatar spawn (consumed by
            # _send_post_spawn_packets); without this it would fall back to the
            # zone default and ignore the waypoint / maze entry resolved above.
            conn.pending_spawn_x = conn.player_pos_x
            conn.pending_spawn_y = conn.player_pos_y
            conn.pending_spawn_z = conn.player_pos_z
            conn.pending_spawn_heading = conn.player_heading
            conn.has_pending_spawn = True

            # PACKET 1 — ZONE DISCONNECT (0x0D 0x02): tells the client to tear down
            # the current zone before loading the next. C# sends this before the
            # connect; omitting it leaves the old zone half-loaded on a re-entry.
            # (Safe to the *transferring* player — the gotcha is only about sending
            # ZoneMessageDisconnected to *remaining* players.)
            d = framing.LEWriter()
            d.write_byte(13)
            d.write_byte(0x02)
            d.write_cstring("zoneleave")
            conn.send_to_client(d.to_array())

            # PACKET 2 — ZONE CONNECT (0x0D 0x00): load the new zone. The client
            # reloads and replies 13/6, re-entering _send_zone_progression.
            w = framing.LEWriter()
            w.write_byte(13)
            w.write_byte(0x00)
            w.write_cstring(target.name)
            # Per-level maze layout seed (see _start_zone_join). Derived from the
            # target zone name so the client maze == the server spawn maze.
            w.write_uint32(dungeon_spawner.layout_seed(target.name))
            w.write_byte(0x01)
            w.write_byte(0xFF)
            w.write_cstring("")
            w.write_uint32(0x00)
            conn.send_to_client(w.to_array())
            log.info(f"[CHANGE-ZONE] '{conn.login_name}' {old_zone} -> {target.name} "
                     f"(spawn='{spawn_point}' pos={position})")
        except Exception as ex:  # noqa: BLE001
            log.error(f"[CHANGE-ZONE] transfer to '{target_zone}' failed: {ex}")
            try:
                conn.send_system_message("Travel failed — please try again.")
                conn.allow_flush = True
            except Exception:  # noqa: BLE001
                pass

    # ── Waystone / checkpoint recall (channel 13) ─────────────────────────────
    def _handle_checkpoint_teleport(self, conn: RRConnection, payload: bytes) -> None:
        """goToCheckpoint (13/0x07) — recall to a checkpoint named by GC id.

        Tag byte 0xFF = a NUL-terminated GC class follows; 0x00 = null ref, which
        the client uses to mean "cycle the obelisk". Port of C#
        HandleCheckpointTeleportRequest.
        """
        from ..managers.checkpoints import checkpoint_manager
        if not payload:
            return
        tag = payload[0]
        if tag == 0x00:
            self._handle_obelisk_teleport(conn)
            return
        if tag != 0xFF:
            log.debug(f"[CP-TELEPORT] unknown tag 0x{tag:02X}")
            return
        end = payload.find(b"\x00", 1)
        name = payload[1:end if end > 0 else len(payload)].decode("ascii", "ignore")
        dest = checkpoint_manager.find_destination(name)
        if dest is None:
            log.warn(f"[CP-TELEPORT] checkpoint '{name}' not in database")
            conn.send_system_message("That waystone is unavailable.")
            return
        log.info(f"[CP-TELEPORT] '{conn.login_name}' recall -> {dest.zone} via '{name}'")
        self.change_zone(conn, dest.zone)

    def _handle_obelisk_teleport(self, conn: RRConnection) -> None:
        """Obelisk (13/0x0C) — cycle through the character's unlocked waystones,
        skipping the current zone. Port of C# HandleObeliskTeleport."""
        from ..managers.checkpoints import checkpoint_manager
        current = (conn.current_zone_name or "").lower()
        available = []
        for cp_id in conn.unlocked_checkpoints:
            dest = checkpoint_manager.find_destination(cp_id)
            if dest is None or not dest.is_active:
                continue
            if dest.zone.lower() == current:
                continue
            available.append(dest)
        available.sort(key=lambda d: d.order)
        if not available:
            conn.send_system_message("No other waystones unlocked yet.")
            return
        idx = conn.obelisk_click_index % len(available)
        conn.obelisk_click_index = idx + 1
        dest = available[idx]
        log.info(f"[OBELISK] '{conn.login_name}' -> {dest.zone} "
                 f"({dest.id}, {idx + 1}/{len(available)})")
        self.change_zone(conn, dest.zone)

    def _handle_saved_place_teleport(self, conn: RRConnection) -> None:
        """SavedPlace (13/0x0B) — return to the saved town-portal point, or town.
        Port of C# HandleSavedPlaceTeleport."""
        if conn.has_saved_town_portal and conn.town_portal_zone_name:
            self.change_zone(conn, conn.town_portal_zone_name)
        else:
            self.change_zone(conn, "town")

    def _refresh_avatar_hp_wire(self, conn: RRConnection,
                                reset_client_hp: bool = True) -> None:
        """Set ``conn.hp_wire`` to the avatar's ×256 wire synch HP.

        The client computes its own avatar max HP and compares it to this value on
        dungeon zones (e.g. dungeon00_level01); a mismatch is a *fatal* Avatar
        synch crash (exit 0xc000013a), so it must equal what the client computes
        from level (base 68096 + 4096/level; Styx3 L1 Mage -> 68096 = 266 HP).
        Sent as a clean ×256 wire value (no updateNumber in the low byte): the
        active C# ``GetSynchValue`` returns ``CurrentHPWire`` raw and RR sends
        ``HP.ToWire()`` — the client compares the full uint32, and the OP12 create
        and the 0x02 trailers must be byte-identical. (The updateNumber-in-low-byte
        variant was a commented-out, rejected C# path; the client does not validate
        updateNumber, only displays it in the crash log.) Feeds the avatar entity
        spawn, the per-tick 0x36 heartbeat and every 0x02 action trailer; must run
        BEFORE the avatar entity is built so the create agrees too.

        ``reset_client_hp`` (default True): spawn/respawn/zone entry start the
        avatar at full HP, so the stale client-reported value is dropped and the
        wire is set to the level max. Pass False on a *level-up* refresh: the
        client owns its own HP and may be damaged, so keep echoing the adopted
        ``conn.client_hp_wire`` (C# ``PlayerState.SynchHP``) rather than clobbering
        the wire back up to the new level max and crashing the zero-tolerance
        avatar synch compare. (Even so, the post-level heartbeat-vs-report race is
        only fully closed by the input-authority bypass — see CLIENT_SERVER_MODEL.)
        """
        from ..data.player_state import (
            compute_saved_avatar_hp_wire, resolve_synch_hp_wire)
        saved = character_repository.get_character(conn.char_sql_id)
        # Level + allocated endurance + class-passive bonus: the client computes
        # max HP from all three once the passives ship on the wire (OP4/OP9/OP10),
        # so the server must mirror the same math (data.class_passives).
        # NB (2026-06-15): live-proven NOT bit-exact — server ships 329 vs client
        # 350 for the level-3 Mage Styx3 (avatar synch desync on dungeon entry).
        # The 21 HP gap is NOT gear (equipped gear carries no HP affix in our item
        # data); it is a formula/attribute mismatch (client END=16 vs server-derived
        # 15, plus a residual). Needs the client's exact HP-derivation (Ghidra) —
        # bible §6a "same formula" T3→T0. See _refresh_avatar_hp_wire desync note.
        new_max = compute_saved_avatar_hp_wire(saved) & 0xFFFFFF00
        if reset_client_hp:
            conn.client_hp_wire = None
            conn.hp_wire = new_max
        else:
            conn.hp_wire = resolve_synch_hp_wire(
                new_max, getattr(conn, "client_hp_wire", None))

    def award_kill_xp(self, conn: RRConnection, monster_level: int) -> None:
        """Award server-side XP for a kill, keeping the avatar level in lockstep
        with the client's local self-leveling.

        Combat is client-authoritative: the vanilla client awards itself XP per
        kill, levels up LOCALLY, and recomputes its avatar HP. If the server's
        level lags, the avatar ``0x02`` synch trailer carries a stale ``hp_wire``
        and the client fatally crashes (live 2026-06-01: client at L2 vs server
        still sending the L1 wire 68096). We mirror the client's exact XP math
        (``player_state.apply_xp``), persist the new level, and refresh
        ``conn.hp_wire`` so the per-tick ``0x36`` + every ``0x02`` trailer carry
        the leveled-up value. No XP packet is sent — the client already
        self-levels and a packet would double-count (ExperienceMod=5) and
        out-pace the server.
        """
        self._grant_kill_xp(conn, monster_level, accrue_only=False)

    def _grant_kill_xp(self, conn: RRConnection, monster_level: int,
                       accrue_only: bool) -> None:
        """Shared XP math for :meth:`award_kill_xp`.

        ``accrue_only`` accumulates experience WITHOUT leveling past the current
        (already client-snapped) level. Used when the telemetry layer owns the
        level (``sync_client_level``): the client self-levels authoritatively, so
        the server must keep ``saved.experience`` tracking the client's gains but
        never bump the level itself — the zero-tolerance HP synch compare crashes
        if the server LEADS the client. Without this the experience field froze
        and the zone-transfer Avatar re-send clobbered the client's locally-earned
        XP (the live "XP lost going to town" report).

        NB the server XP curve is not yet confirmed byte-exact against the client
        (the live "523 vs ~22000-to-level" mismatch), so this keeps experience
        only APPROXIMATELY current; the exact value arrives from the client hook's
        experience report (``sync_client_level(..., client_experience=...)``)."""
        from ..data.player_state import apply_xp, xp_per_kill

        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return
        level = saved.level or 1
        gained = xp_per_kill(monster_level, level)
        if gained <= 0:
            return

        # Cap leveling at the current level when accruing so the server never
        # leads the client; the client's own KILL_AT snap drives the level-up.
        # Otherwise use apply_xp's default cap (full leveling, legacy path).
        if accrue_only:
            new_level, new_exp, did_level = apply_xp(
                level, saved.experience or 0, gained, max_level=level)
        else:
            new_level, new_exp, did_level = apply_xp(
                level, saved.experience or 0, gained)
        saved.experience = new_exp
        if did_level:
            saved.level = new_level
            # Client refills HP/MP to max on level-up; clear so the next
            # refresh_player_state recomputes from the new level.
            saved.max_hp = None
            saved.current_hp = None
            saved.max_mana = None
            saved.current_mana = None
        character_repository.save_character(saved)

        if did_level:
            # Keep the cached level current — the equip level gate reads it.
            conn.player_level = new_level
            # Preserve a damaged client-reported HP — do NOT clobber up to the
            # new level max (resolve_synch_hp_wire / C# PlayerState.SynchHP).
            self._refresh_avatar_hp_wire(conn, reset_client_hp=False)
            log.info(f"[XP] '{conn.login_name}' LEVEL UP -> {new_level} "
                     f"hp_wire={conn.hp_wire} (+{gained} xp)")
        else:
            log.debug(f"[XP] '{conn.login_name}' +{gained} xp "
                      f"(exp={new_exp}, L{saved.level}, accrue_only={accrue_only})")

    def accrue_kill_xp(self, conn: RRConnection, monster_level: int) -> None:
        """Accumulate XP for a kill whose level the client already owns (the
        telemetry snap path). Tracks ``saved.experience`` without ever leading the
        client's level. See :meth:`_grant_kill_xp`."""
        self._grant_kill_xp(conn, monster_level, accrue_only=True)

    def sync_client_level(self, conn: RRConnection, client_level: int,
                          client_experience: Optional[int] = None) -> None:
        """Snap the character UP to a client-reported level (telemetry KILL_AT),
        and optionally adopt the client's exact experience.

        The client awards itself XP and levels up LOCALLY and authoritatively
        (combat is client-authoritative). The server's own XP mirror
        (``award_kill_xp``) can fall behind whenever a kill is dropped (untracked
        mob / unresolved killer) or its XP math drifts from the client's — and
        when it does, the avatar ``0x02`` synch trailer carries a stale, too-low
        ``hp_wire`` and the client fatally crashes (the "client leveled, server
        still behind" report). Snapping to the level the client itself reports on
        every kill keeps them in lockstep regardless of how XP was credited.

        Level is upward-only: never demote (the client is the authority and only
        climbs). ``client_experience`` — when the client hook reports its exact
        progress-into-level (``OP_KILL_AT_XP``, present after the next DLL
        rebuild) — is adopted verbatim as the SOLE XP authority, so the
        zone-transfer Avatar re-send (``gc_object_factory`` ``Experience``) matches
        the client exactly and never clobbers locally-earned XP. Until then it is
        ``None`` and the approximate server-side accumulation (carried across the
        crossed thresholds here) keeps experience close.
        """
        if client_level <= 0:
            return
        saved = character_repository.get_character(conn.char_sql_id)
        if saved is None:
            return
        old_level = saved.level or 1
        leveled = client_level > old_level
        if not leveled and client_experience is None:
            return                       # already in lockstep, nothing to update

        if leveled:
            # Carry the accumulated experience across the crossed level thresholds
            # so it stays "progress into the current level" (matches apply_xp's
            # carry; approximate until the curve is confirmed — the exact value
            # below overrides it when present).
            from ..data.player_state import xp_threshold_for_level
            exp = saved.experience or 0
            for lvl in range(old_level + 1, client_level + 1):
                exp = max(0, exp - xp_threshold_for_level(lvl))
            saved.experience = exp
            saved.level = client_level
            # Client refills HP/MP to max on level-up; clear so the next refresh
            # recomputes from the new level (preserving any damaged client HP).
            saved.max_hp = None
            saved.current_hp = None
            saved.max_mana = None
            saved.current_mana = None

        if client_experience is not None:
            saved.experience = max(0, client_experience)

        character_repository.save_character(saved)
        if leveled:
            conn.player_level = client_level
            self._refresh_avatar_hp_wire(conn, reset_client_hp=False)
            log.info(f"[XP] '{conn.login_name}' level SNAP -> {client_level} "
                     f"(client-reported) hp_wire={conn.hp_wire} exp={saved.experience}")
        else:
            log.debug(f"[XP] '{conn.login_name}' exp ADOPT {saved.experience} "
                      f"(client-reported, L{client_level})")

    def _send_avatar_spawn_state(self, conn: RRConnection,
                                 sx: float, sy: float, sz: float) -> None:
        """Send the avatar-alive sequence at a given position — SpawnAction
        (0x35/0x04 BehaviourActionSpawn) + FollowClient (0x35/0x64 Control ON) +
        UnitMoverUpdate (0x35/0x65) — each carrying the current avatar synch HP.

        Shared by the initial/zone-entry post-spawn (loading-screen dismissal +
        camera follow) and the same-zone respawn-in-place path (revive + snap to
        the respawn point). The caller must set ``conn.hp_wire`` (via
        ``_refresh_avatar_hp_wire``) first; this also updates ``conn.player_pos``.
        """
        ub_id = conn.unit_behavior_id
        hp_wire = conn.hp_wire
        heading = conn.player_heading

        # SomeUnitID = the avatar's ENTITY id (C# SendPlayerEntitySpawn writes
        # (ushort)avatar.Id — UnityGameServer.cs:23980). This binds the spawn
        # action to the avatar entity so the client treats it as its own
        # local-input action; sending 0 (the prior bug) left it unbound.
        avatar = getattr(conn, "avatar", None)
        some_unit_id = (avatar.id & 0xFFFF) if avatar is not None and getattr(
            avatar, "id", None) else 0

        conn.player_pos_x = sx
        conn.player_pos_y = sy
        conn.player_pos_z = sz

        # ── SpawnAction ──
        w = framing.LEWriter()
        w.write_byte(0x07)                       # BeginStream
        w.write_byte(0x35)
        w.write_uint16(ub_id)
        w.write_byte(0x04)                       # CreateAction1 sub-opcode
        w.write_byte(0x04)                       # BehaviourActionSpawn
        w.write_byte(0xFF)                       # SessionID
        w.write_int32(int(sx * 256))
        w.write_int32(int(sy * 256))
        w.write_int32(int(sz * 256))
        w.write_uint16(some_unit_id)             # SomeUnitID = avatar entity id
        w.write_byte(0x02)                       # Synch flag
        w.write_uint32(hp_wire)
        w.write_byte(0x06)                       # EndStream
        conn.send_to_client(w.to_array())

        # ── FollowClient (separate stream — one ComponentUpdate per stream) ──
        w = framing.LEWriter()
        w.write_byte(0x07)                       # BeginStream
        w.write_byte(0x35)
        w.write_uint16(ub_id)
        w.write_byte(0x64)                       # StateMachine / FollowClient
        w.write_byte(0x01)                       # Control ON
        w.write_byte(0x02)                       # Synch flag
        w.write_uint32(hp_wire)
        w.write_byte(0x06)                       # EndStream
        conn.send_to_client(w.to_array())

        # ── UnitMoverUpdate — dismisses loading screen / snaps the camera ──
        w = framing.LEWriter()
        w.write_byte(0x07)                       # BeginStream
        w.write_byte(0x35)
        w.write_uint16(ub_id)
        w.write_byte(0x65)                       # UnitMoverUpdate
        w.write_byte(conn.session_id)           # C#: conn.SessionID++ (writes current)
        conn.session_id = (conn.session_id + 1) & 0xFF
        w.write_byte(0x01)                       # move count
        w.write_byte(0x03)                       # move type (0x01 | 0x02)
        w.write_int32(int(heading * 256))
        w.write_int32(int(sx * 256))
        w.write_int32(int(sy * 256))
        w.write_byte(0x02)                       # Synch flag
        w.write_uint32(hp_wire)
        w.write_byte(0x06)                       # EndStream
        conn.send_to_client(w.to_array())
        log.debug(f"[AVATAR-STATE] ub={ub_id} pos=({sx},{sy},{sz}) hp_wire={hp_wire}")

        # Arm the post-zone-entry control re-assert burst.
        #
        # On a zone-warp the avatar arrives ALREADY enrolled (behavior +0xe5==4)
        # with a brand-new active action whose authority byte (action +0x95)
        # defaults bit-CLEAR — so a FollowClient ON here is a no-op (the client's
        # 0x64 handler FUN_00520020 only acts when the control bit CHANGES, and it
        # is already set from the previous zone). The synch-compare bypass
        # (ctrl[0x47][0x95]&1) therefore never gets set, and the first mob hit
        # BEFORE the player attacks fatally crashes the avatar (zero-tolerance
        # FUN_005dd900).
        #
        # An OFF->ON toggle DOES set the bit (it re-runs FUN_005202f0's
        # controller-activate). But sending it INLINE here — during the spawn
        # stream — only sets the TRANSIENT spawn action's +0x95; the client then
        # swaps to a fresh idle/steady action (+0x95 CLEAR) which is the one live
        # when a mob hits, so the live re-test still crashed (2026-06-02 13:16).
        # The bit is PER-ACTION and does not carry across the spawn->idle swap.
        #
        # So defer the toggle to the first inbound client packets after entry
        # (movement 0x65 / hp-sync 0x36 — see net.movement.handle ->
        # reassert_control_after_zone_entry), when the STEADY action is live, and
        # repeat it a few times for safety. LIVE-CONFIRMED 2026-06-02 (x64dbg):
        # with the steady action's bit set the avatar took damage and
        # died/respawned normally — no synch crash. See docs/CLIENT_SERVER_MODEL §7.
        self._arm_control_reassert_window(conn)

    def send_client_control_reset(self, conn: RRConnection) -> None:
        """Re-assert the client's input authority over its OWN avatar.

        Emits a release-then-regrant control toggle — Control OFF (``0x64``/
        ``0x00``) then ON (``0x64``/``0x01``) — for the avatar's behavior
        component in a single ``0x07``/``0x06`` stream, each block carrying the
        current ``conn.hp_wire`` synch trailer. Port of C#
        ``UnityGameServer.SendClientControlReset`` (which writes
        ``WriteClientControlUpdate(false)`` then ``WriteClientControlUpdate(true)``).

        Why this fixes the level-up Avatar synch crash: the vanilla client
        self-levels LOCALLY on kills and recomputes its avatar max HP (e.g. L1
        wire 68096 -> L2 72192) without telling the server, so ``conn.hp_wire``
        goes stale and every per-tick ``0x36`` heartbeat / ``0x02`` trailer
        mismatches the client's local value. The client's type-2 synch compare
        on its own avatar (Ghidra ``FUN_005dd900``) is SKIPPED while the client
        holds input authority (``ctrl[0x47][0x95]&1`` set); the OFF->ON
        transition forces that flag to re-init, so the stale-HP compare never
        runs and the fatal Avatar crash (exit 0xc000013a) is avoided. A single
        Control-ON at spawn does not establish it — the toggle does.
        """
        ub_id = conn.unit_behavior_id
        if not ub_id:
            return
        # The OFF->ON control toggle was built to dodge the avatar synch compare by
        # "re-asserting input authority". That compare is gated by the avatar
        # action's +0x95 PEACEFUL-zone bit (proven 2026-06-09 via the skill-use
        # gate FUN_004a4810) — which this toggle does NOT set. So in a combat zone
        # it does nothing useful, and its 0x02+hp trailer ships conn.hp_wire
        # (=level MAX right after zone entry, before the client has reported) in a
        # fresh Upd=0 ComponentUpdate → the exact [Remote]=MAX/Upd=0 mismatch this
        # method's own docstring documents. This is a server-ORIGINATED avatar-HP
        # send, so the Regime-B posture suppresses it in combat zones by default
        # (bible.md §6 / §6-LIVE.8); peaceful zones keep it (compare is skipped
        # there) for the legacy level-up path.
        from . import movement
        if movement.suppress_originated_avatar_hp(conn):
            return
        # Clamp the trailer to the client's last self-report — NEVER ship an HP
        # above it. After zone entry conn.hp_wire was reset to the level MAX
        # (_refresh_avatar_hp_wire), so a raw trailer here re-sends [Remote]=MAX
        # in a fresh ComponentUpdate (Upd=0) while the client has self-simmed
        # lower, fatally mismatching the zero-tolerance avatar synch compare
        # (live crash 2026-06-03: [Local]67282 vs [Remote]72192=MAX/Upd=0). Use
        # the same clamp as the 0x36 heartbeat (C# PlayerState.SynchHP). The
        # inbound-ch7 adopt runs BEFORE this reassert (movement.handle), so
        # client_hp_wire is fresh from the same packet's trailing EntitySynchInfo.
        from ..data.player_state import resolve_synch_hp_wire
        hp_wire = resolve_synch_hp_wire(conn.hp_wire,
                                        getattr(conn, "client_hp_wire", None))
        w = framing.LEWriter()
        w.write_byte(0x07)                       # BeginStream
        for control_on in (0x00, 0x01):          # release (OFF) then regrant (ON)
            w.write_byte(0x35)                   # ComponentUpdate
            w.write_uint16(ub_id)
            w.write_byte(0x64)                   # StateMachine / FollowClient
            w.write_byte(control_on)
            w.write_byte(0x02)                   # Synch flag
            w.write_uint32(hp_wire)
        w.write_byte(0x06)                       # EndStream
        conn.send_to_client(w.to_array())

    def enroll_instance_monsters(self, conn: RRConnection) -> bool:
        """Wake this instance's monsters into ``conn``'s client AI via the
        deferred ``0x64`` burst — the LEGACY combat model (opt-in
        ``DR_LEGACY_ENROLL=1``; net/movement.py 0x50 fork).

        SUPERSEDED by the native default (bible §14.6 round 6n, LIVE-CONFIRMED
        2026-07-08): the client's own monster brain already aggros/chases/attacks
        correctly with no server help, and this ``0x64`` burst is what BROKE it
        into a run-to-center chase (the "Cost" below). Retained only as an escape
        hatch for patched-client debugging.

        Once per zone entry, on the player's first attack: the first attack
        blesses the avatar action with the input-authority bit, so the
        counter-damage from the newly woken mobs bypasses the zero-tolerance
        synch compare, and the server never asserts monster HP after the burst
        — on an UNPATCHED client the compare simply never runs (bible.md §4).
        Cost: the client brain chases the avatar's center (run-through).
        Latched via ``conn._monsters_enrolled``.

        Alternative model (``DR_MONSTER_AI=1``): server-driven Follow + stepped
        chase (managers/monster_ai.py, DRS-NET port — fixes run-through, but
        asserts replay-tracked mob HP that only a FUN_005dd900-patched client
        survives until the server's combat math is bit-exact; bible.md §6a).
        """
        if getattr(conn, "_monsters_enrolled", False):
            return False
        enrolled = self.world_instances.enroll_monsters(self, conn)
        conn._monsters_enrolled = True
        return enrolled > 0

    def reassert_control_on_action(self, conn: RRConnection, now: float) -> bool:
        """Throttled avatar control re-assert, fired from the client's attack
        actions (channel-7 ``0x50`` BehaviourActionUseTarget).

        C# re-asserts control when a combat use-target resolves; drserver lacks
        reliable kill detection, so it re-asserts on the attack actions
        themselves (which it DOES see on the wire), throttled to one toggle per
        :data:`_CONTROL_REASSERT_INTERVAL` seconds to avoid disrupting the
        weapon cycle. Kills (and thus the local level-up) happen mid-attack, so
        this keeps the client's avatar authority fresh across the desync window.
        Returns True iff a reset was actually sent.
        """
        if not _CONTROL_REASSERT_ENABLED:
            return False
        last = getattr(conn, "_last_control_reset_time", 0.0)
        if now - last < _CONTROL_REASSERT_INTERVAL:
            return False
        conn._last_control_reset_time = now
        self.send_client_control_reset(conn)
        return True

    def _arm_control_reassert_window(self, conn: RRConnection) -> None:
        """Arm a throttled burst of avatar control re-asserts for the first few
        inbound client packets after zone entry.

        Called at the end of the avatar spawn-state send (zone entry / respawn).
        It does NOT toggle control inline — that would only set the transient
        spawn action's authority bit, which the client discards when it swaps to
        the steady/idle action (the one live when a mob hits). Instead it primes
        a pending counter and resets the throttle so the very first inbound
        post-warp packet (~33 ms, before a mob can path+hit) fires the OFF->ON
        toggle on the STEADY action, repeated up to :data:`_CONTROL_REASSERT_BURST`
        times. See ``reassert_control_after_zone_entry`` and
        ``_send_avatar_spawn_state``.
        """
        # Disabled by default — the burst snaps the avatar back to spawn on the
        # first post-warp movement packets (teleport-back on arrival). Superseded
        # by the client synch-crash patch; C# does not burst control on movement.
        conn._control_reassert_pending = (
            _CONTROL_REASSERT_BURST if _CONTROL_REASSERT_ENABLED else 0)
        conn._last_control_reset_time = 0.0   # so the first inbound packet fires
        # New zone → its mobs are passive again until the player engages here.
        conn._monsters_enrolled = False

    def reassert_control_after_zone_entry(self, conn: RRConnection, now: float) -> bool:
        """Re-assert avatar input authority on an inbound client packet during the
        post-zone-entry burst window (movement ``0x65`` / hp-sync ``0x36`` / acks).

        Fired from ``net.movement.handle`` on every inbound channel-7 packet, but
        a no-op once the burst is exhausted (``_control_reassert_pending`` reaches
        0). While pending, it sends the OFF->ON control toggle (throttled by
        :data:`_CONTROL_REASSERT_INTERVAL` via ``reassert_control_on_action``) and
        decrements the counter only when a toggle actually goes out — so the burst
        spans ~``BURST*INTERVAL`` seconds of real play, setting the bit on the live
        STEADY action before the player's first attack would. Returns True iff a
        reset was sent.
        """
        pending = getattr(conn, "_control_reassert_pending", 0)
        if pending <= 0:
            return False
        if self.reassert_control_on_action(conn, now):
            conn._control_reassert_pending = pending - 1
            return True
        return False

    def _respawn_in_place(self, conn: RRConnection, target,
                          *, spawn_point: str = "",
                          position: Optional[Tuple[float, float, float]] = None) -> None:
        """Same-zone respawn — reposition the avatar WITHOUT a zone reload.

        The live native client will not reload the zone it is already in (it
        never sends the 13/6 join reply), so the disconnect/connect dance used
        for a true zone change strands the player. Instead we restore the avatar
        to full HP (``_refresh_avatar_hp_wire`` — the ×256 level max), move it to
        the respawn waypoint, and re-send the avatar spawn/control/mover packets
        so the client revives and snaps the camera. ``is_spawned`` and the
        running tick stay intact (the tick is re-armed defensively).
        """
        # ROUTE 2B: a respawn ends any in-progress swing cycle.
        if getattr(self, "combat", None) is not None and conn.login_name:
            self.combat.clear_combat(conn.login_name)

        # Resolve the respawn position: explicit > named waypoint > zone default.
        if position is not None:
            x, y, z = position
        elif spawn_point:
            from ..managers.portals import portal_manager
            wp = portal_manager.find_waypoint(target.name, spawn_point)
            if wp is not None:
                x, y, z = wp.pos_x, wp.pos_y, wp.pos_z
            else:
                log.warn(f"[RESPAWN] waypoint '{spawn_point}' not found in "
                         f"'{target.name}' — using zone default spawn")
                x, y, z = target.spawn_x, target.spawn_y, target.spawn_z
        else:
            x, y, z = target.spawn_x, target.spawn_y, target.spawn_z

        # Procedural dungeon: snap to the maze entrance (see _transfer_zone).
        from ..managers import dungeon_spawner
        if dungeon_spawner.is_procedural_zone(target.name):
            entry = dungeon_spawner.entry_position(target.name, spawn_point)
            if entry is not None:
                x, y = entry[0], entry[1]
                if entry[2] is not None:
                    z = entry[2]
                conn.player_heading = entry[3]   # authored facing (into the room)

        # RestoreToFull — refresh the avatar synch HP to the level max.
        self._refresh_avatar_hp_wire(conn)

        # Revive + reposition the avatar in place; keep the player spawned.
        conn.is_spawned = True
        conn.allow_flush = True
        self._send_avatar_spawn_state(conn, x, y, z)

        # Re-arm the heartbeat (idempotent — start_tick cancels any existing).
        from . import movement
        movement.start_tick(self, conn)
        log.info(f"[RESPAWN] '{conn.login_name}' in-place -> {target.name} "
                 f"at ({x},{y},{z})")

    def _send_post_spawn_packets(self, conn: RRConnection) -> None:
        """Packets 2 & 3 from C# SendPlayerEntitySpawn — critical for the client to
        dismiss the loading screen and begin camera-follow / controlling the avatar.

        Packet 2: SpawnAction (0x35/0x04) + FollowClient (0x35/0x64)
        Packet 3: UnitMoverUpdate (0x35/0x65) — loading screen dismissal
        """
        ub_id = conn.unit_behavior_id
        if not ub_id:
            log.error("[POST-SPAWN] no unit_behavior_id — cannot send post-spawn packets")
            return

        # Ensure the avatar synch HP is current (idempotent — also set before the
        # avatar entity is built; see _refresh_avatar_hp_wire / _spawn_player).
        self._refresh_avatar_hp_wire(conn)

        # A zone transfer (portal / recall / maze entry) resolves the arrival
        # position and stashes it in pending_spawn; honor it so the player lands
        # where the transfer decided (the waypoint or the procedural maze
        # entrance) instead of the static zone default. The initial tutorial join
        # sets no pending spawn, so it still uses the zone default below.
        if conn.has_pending_spawn:
            sx, sy, sz = (conn.pending_spawn_x, conn.pending_spawn_y,
                          conn.pending_spawn_z)
            conn.player_heading = conn.pending_spawn_heading  # authored maze facing
            conn.has_pending_spawn = False
        else:
            zone = zone_registry.get_by_id(conn.current_zone_id)
            if zone and (zone.spawn_x != 0 or zone.spawn_y != 0):
                sx, sy, sz = zone.spawn_x, zone.spawn_y, zone.spawn_z
            else:
                default = self.config.default_spawn_position
                sx, sy, sz = default[0], default[1], default[2]

        # Packets 2 & 3 — the avatar-alive sequence (shared with same-zone
        # respawn-in-place; sets conn.player_pos and sends SpawnAction +
        # FollowClient + UnitMoverUpdate at the given position).
        self._send_avatar_spawn_state(conn, sx, sy, sz)

        # ── Welcome message ──
        welcome = framing.LEWriter()
        welcome.write_byte(0x06)                  # chat/system message type
        welcome.write_byte(0x00)                  # channel = system
        welcome.write_byte(0x0D)                  # subtype = GlobalAnnouncement
        for ch in "Welcome to Dungeon Runners!\n":
            welcome.write_byte(ord(ch) if isinstance(ch, str) else ch)
        welcome.write_byte(0x00)                    # C#: null terminator
        conn.send_to_client(welcome.to_array())

        # NB: the movement tick is intentionally NOT started here. C#
        # SendPlayerEntitySpawn streams the zone entities (NPCs, portals,
        # checkpoints, world entities) FIRST and only starts SendTickUpdates at
        # the very end. Starting the per-tick HP/echo heartbeat while the client
        # is still in its ClientEntityManager streaming state interleaves tick
        # packets into the entity stream and trips the sync check
        # ("Zone communication error. Code 3"). The tick is started by
        # _send_zone_progression once every entity has been sent.
        log.debug(f"[POST-SPAWN] '{conn.login_name}' post-spawn packets sent")

    @staticmethod
    def _zone_gc_type(zone_name: str) -> str:
        if "tutorial" in zone_name:
            return "world.tutorial"
        if "town" in zone_name:
            return "world.town"
        # Every dungeon (incl. dungeon00) maps to its world group: world.<dungeonNN>.
        # dungeon00 was formerly special-cased to world.tutorial — removed; it is
        # now treated like every other dungeon (fully data-driven).
        return "world." + zone_name.split("_")[0]

    # ── Channel 13: zone progression ──────────────────────────────────────────
    def _handle_zone_channel(self, conn: RRConnection, body: bytes) -> None:
        # The client sends an empty 13/6 (and/or a non-empty 0x06) once the zone
        # assets are loaded; both trigger the spawn progression.
        join_requested = (not body) or (len(body) >= 1 and body[0] == 0x06)
        if not join_requested:
            if body and body[0] in (0x00, 0x01, 0x08):
                return
            log.debug(f"[GAME] zone channel type 0x{body[0]:02X}")
            return

        if conn.is_spawned:
            log.debug("[GAME] zone join ignored — already spawned")
            return

        self._send_zone_progression(conn)

    def _send_zone_progression(self, conn: RRConnection) -> None:
        zone = zone_registry.get_by_id(conn.current_zone_id)
        explored = 0x12
        if conn.current_zone_id == TOWN_ZONE_ID:
            explored = 31
        if zone and zone.explored_bit_count > 0:
            explored = zone.explored_bit_count

        # Step 1: ZoneMessageReady (13/1) — playerUserId + explored bitmap.
        # The u32 is the player's charSqlId (C# GetCharSqlId), NOT the zone id:
        # the client stores it as its own user id — ZoneClient::processReady
        # @0x5FC250 reads it into ZoneClient+0xF4 (T0-decompiled 2026-07-09).
        # We sent current_zone_id here until 2026-07-09, diverging from the
        # C# reference the group/party self-identification was tested against.
        char = self.selected_character.get(conn.login_name or "")
        player_user_id = (int(getattr(char, "id", 0) or 0)
                          or getattr(conn, "char_sql_id", 0)
                          or (conn.conn_id + 1))
        w = framing.LEWriter()
        w.write_byte(13)
        w.write_byte(1)
        w.write_uint32(player_user_id & 0xFFFFFFFF)
        w.write_uint16(explored)
        for _ in range(explored):
            w.write_uint32(0x00000000)
        conn.send_to_client(w.to_array())

        # Step 2: ZoneMessageInstanceCount (13/5).
        w = framing.LEWriter()
        w.write_byte(13)
        w.write_byte(5)
        w.write_uint32(0x00)
        w.write_uint32(0x00)
        conn.send_to_client(w.to_array())

        # Step 3: ClientEntity interval (7/0x0D).
        w = framing.LEWriter()
        w.write_byte(7)
        w.write_byte(0x0D)
        w.write_int32(0)        # tick (Time.time-based; 0 is fine at start)
        w.write_int32(33)       # tick interval ms
        w.write_int32(0)        # movement prediction buffer
        w.write_int32(0)        # path manager budget
        w.write_uint16(100)     # budget per update
        w.write_uint16(20)      # budget per path
        w.write_byte(0x06)      # stream end
        conn.send_to_client(w.to_array())

        # Step 3b: room RNG seed (7/0x0C) — faithful port of C# SendRandomSeed
        # (UGS:1745), sent ONCE at zone connect with a STABLE seed. The client
        # seeds its combat/room RNG from this; the server seeds its own replay
        # RNG identically (seed_room_rng) so the two share the stream.
        # This is the COMBAT room-RNG seed and is independent of the maze
        # *layout* seed (13/0x00, dungeon_spawner.layout_seed): the two are
        # separate wire fields, so we keep this one a stable constant to avoid
        # perturbing the zero-tolerance HP-synch stream.
        # NB: this REPLACES the old per-tick time-based 0x0C reseed, which made
        # damage/hit/miss/crit parity impossible (it perturbed the client's RNG
        # every 33ms).
        try:
            from ..managers import dungeon_spawner
            room_seed = dungeon_spawner.MAZE_SEED & 0xFFFFFFFF
            sw = framing.LEWriter()
            sw.write_byte(0x07)             # BeginStream
            sw.write_byte(0x0C)             # processRandomSeed opcode
            sw.write_uint32(room_seed)
            sw.write_byte(0x06)             # EndStream
            conn.send_to_client(sw.to_array())
            if getattr(self, "combat", None) is not None:
                self.combat.seed_room_rng(room_seed)
        except Exception as ex:  # noqa: BLE001
            log.error(f"[ROOM-RNG] seed send failed for '{conn.login_name}': {ex}")

        # Step 4: spawn the player's own avatar/player entities.
        # Set the avatar synch HP FIRST so the OP12 create carries the same ×256
        # wire HP as the per-tick 0x36 / 0x02 trailers (the avatar synch field is
        # the OP12 HP uint32, UnitFlags 0x02 — a mismatch is the fatal
        # dungeon00_level01 Avatar crash).
        self._refresh_avatar_hp_wire(conn)
        from . import spawn
        spawn.send_player_entity_spawn(self, conn)

        # ── Mark spawned BEFORE post-spawn packets (matches C# order) ──
        conn.is_spawned = True
        conn.session_id = 0xFF

        # ── Post-spawn initialization (Packets 2 & 3 from C# reference) ──
        self._send_post_spawn_packets(conn)

        # Steps 5–7: enter the per-(zone, instance) world. The instance is
        # populated ONCE (monsters/NPCs/world-entities with stable ids) by the
        # first player to enter; every joiner — including this one — then receives
        # the same stored snapshot. No per-join re-spawn, no duplication.
        try:
            self.world_instances.enter(self, conn)
        except Exception as ex:  # noqa: BLE001
            log.error(f"[INSTANCE] enter failed for '{conn.login_name}': {ex}")

        # Returning to the zone a waypoint scroll was cast in re-spawns the
        # (visual-only) return portal and clears the saved place — one return
        # trip per scroll (C# SpawnReturnTownPortal after SendZonePortals).
        try:
            from ..managers import town_portal
            town_portal.spawn_return_portal_if_home(self, conn)
        except Exception as ex:  # noqa: BLE001
            log.error(f"[TOWN-PORTAL] return-portal spawn failed: {ex}")

        # Step 8: MULTIPLAYER — exchange avatar spawns with other players already
        # in this zone/instance so everyone can see each other (C# 19178).
        try:
            spawn.exchange_player_spawns(self, conn)
        except Exception as ex:  # noqa: BLE001
            log.error(f"[MULTIPLAYER] spawn exchange failed: {ex}")

        conn.allow_flush = True

        # Start the per-tick HP/echo heartbeat LAST — only now that the player
        # avatar AND every zone entity (NPCs, portals, checkpoints, world
        # entities) have been streamed. This mirrors C# SendPlayerEntitySpawn,
        # which starts SendTickUpdates at the very end (UnityGameServer.cs:19167)
        # rather than mid-progression. Starting it earlier interleaves tick
        # packets into the entity stream and trips the client's
        # "Zone communication error. Code 3" sync check (most visible on @z town,
        # which streams many NPCs/entities after spawn).
        from . import movement
        movement.start_tick(self, conn)
        log.info(f"[ZONE-PROGRESSION] '{conn.login_name}' fully initialized, tick started")

        # Group state for the zone-join tail (C# zone-transition block): a
        # grouped player re-receives the roster (0x30 once/0x35) + talkback
        # 0x50, the members' zone labels (0x4C) and health (0x4B); a
        # reconnecting member is re-onlined (0x4A to the others). A solo
        # player gets the self-prime (0x30 + empty 0x35 + 0x50) that keys the
        # client's group identity (GC+0xB0) for the right-click invite menu.
        if getattr(self, "groups", None) is not None:
            try:
                self.groups.on_player_spawned(conn)
            except Exception as ex:  # noqa: BLE001
                log.error(f"[GROUP] spawn-tail group state failed: {ex}")

        # Re-send still-active tracked buff modifiers — the client drops every
        # modifier on a zone transition. Port of C# ResendAllModifiers after
        # SendPlayerEntitySpawn (UnityGameServer.cs:18804). Done after the tick
        # start so the Add streams never interleave with the entity stream.
        try:
            from ..managers import player_modifiers
            player_modifiers.resend_all(conn)
            # Spawn protection on every combat-zone entry (join + warp), and
            # the free-account XP modifier re-sent on EVERY zone entry (the
            # client drops modifiers on rezone; members are skipped inside).
            player_modifiers.send_zone_spawn_invulnerability(conn)
            player_modifiers.send_free_player_modifier(conn)
        except Exception as ex:  # noqa: BLE001 — never break spawn on buffs
            log.error(f"[MOD-RESEND] failed for '{conn.login_name}': {ex}")

        # Quest "!" markers for this zone's quest-givers — C# sends
        # SendAvailableQuestUpdateForZone at the end of the spawn sequence. Done
        # after the entity stream + tick so it never interleaves with the stream.
        if getattr(self, "quests", None) is not None:
            try:
                self.quests.send_available_quest_update(conn)
            except Exception as ex:  # noqa: BLE001
                log.debug(f"[QUEST] available-quest update failed: {ex}")

        # NB: NO zone-entry gnome auto-summon. The 2026-06-12 live test proved
        # spawning the gnome during zone settle kills him instantly client-side
        # (ZoneAction=DeathOnZone → death anim plays at every spawn/warp) and
        # leaves the server believing a gnome exists, so casts mis-routed to
        # activate() instead of spawn(). DRS-NET never auto-summons either —
        # the gnome returns when the player re-casts / re-places the skill.

    # ── Channel 6: admin / bling-gnome UI ─────────────────────────────────
    def _handle_admin_channel(self, conn: RRConnection, message_type: int,
                               data: bytes) -> None:
        """Handle admin channel (ch 6) — bling gnome, admin commands.
        Currently a logging stub; full admin panel/BlingGnome UI is deferred.
        """
        log.debug(f"[ADMIN] ch=6 type=0x{message_type:02X} len={len(data)}")

    # ── Channel 0x0B: group client + PvP duel ─────────────────────────────
    def _handle_group_channel(self, conn: RRConnection, message_type: int,
                               data: bytes) -> None:
        """Handle group/PvP channel (ch 0x0B) — dispatches to GroupManager or DuelManager.

        Incoming opcodes (client→server, from C# GroupPackets.cs/PVPPackets.cs):
        Groups: 0x12 inviteUser, 0x14 removeUser, 0x15 setLeader, 0x16 inviteByName,
                0x17 setMonsterDifficulty, 0x20 acceptInvite, 0x21 declineInvite,
                0x22 leaveGroup, 0x24 setOpenGroup, 0x26 resetInstances,
                0x27 gotoMember, 0x28 setInviteMode
        PvP:    0x29 enterPVPZone, 0x2A requestPVPMatch, 0x2B cancelPVPMatch,
                0x2C leavePVP, 0x2D requestPVPDuel, 0x2E acceptPVPDuel,
                0x2F declinePVPDuel
        """
        # Group ops are rare — log every inbound at INFO (opcode + hex) so a
        # non-working invite/accept is diagnosable from the server log without
        # DR_LOG_LEVEL=DEBUG. Covers unknown opcodes the per-branch logs miss.
        log.info(f"[GROUP-IN] conn={conn.conn_id} type=0x{message_type:02X} "
                 f"len={len(data)} hex={data[:16].hex()}")
        reader = framing.LEReader(data)
        if message_type == 0x12:    # inviteUser (by charSqlId)
            target_id = reader.read_uint32() if reader.remaining >= 4 else 0
            log.info(f"[GROUP] inviteUser conn={conn.conn_id} target_id={target_id}")
            if self.groups and target_id:
                self.groups.invite_by_char_id(conn, target_id)
        elif message_type == 0x16:  # inviteByName
            if reader.has_data:
                target_name = reader.read_cstring()
                log.info(f"[GROUP] inviteByName conn={conn.conn_id} target='{target_name}'")
                if self.groups and target_name:
                    self.groups.invite(conn, target_name)
        elif message_type == 0x14:  # removeUser/kick (by charSqlId)
            kick_id = reader.read_uint32() if reader.remaining >= 4 else 0
            log.info(f"[GROUP] removeUser conn={conn.conn_id} target_id={kick_id}")
            if self.groups and kick_id:
                self.groups.kick_by_char_id(conn, kick_id)
        elif message_type == 0x15:  # setLeader (by charSqlId)
            leader_id = reader.read_uint32() if reader.remaining >= 4 else 0
            log.info(f"[GROUP] setLeader conn={conn.conn_id} target_id={leader_id}")
            if self.groups and leader_id:
                self.groups.set_leader_by_char_id(conn, leader_id)
        elif message_type == 0x17:  # setMonsterDifficulty
            diff = reader.read_byte() if reader.has_data else 0
            log.info(f"[GROUP] setMonsterDifficulty conn={conn.conn_id} diff={diff}")
            if self.groups:
                self.groups.set_monster_difficulty(conn, diff)
        elif message_type == 0x20:  # acceptInvite (u32 inviteId, unused — C# too)
            log.info(f"[GROUP] acceptInvite conn={conn.conn_id}")
            if self.groups:
                self.groups.accept(conn)
        elif message_type == 0x21:  # declineInvite
            log.info(f"[GROUP] declineInvite conn={conn.conn_id}")
            if self.groups:
                self.groups.decline(conn)
        elif message_type == 0x22:  # leaveGroup
            log.info(f"[GROUP] leaveGroup conn={conn.conn_id}")
            if self.groups:
                self.groups.leave(conn)
        elif message_type == 0x24:  # setOpenGroup
            flag = reader.read_byte() if reader.has_data else 0
            log.info(f"[GROUP] setOpenGroup conn={conn.conn_id} flag={flag}")
            if self.groups:
                self.groups.set_open_group(conn, flag != 0)
        elif message_type == 0x26:  # resetInstances (leader-only)
            log.info(f"[GROUP] resetInstances conn={conn.conn_id}")
            if self.groups:
                self.groups.reset_instances(conn)
        elif message_type == 0x27:  # gotoMember (right-click roster -> "Go To")
            goto_id = reader.read_uint32() if reader.remaining >= 4 else 0
            log.info(f"[GROUP] gotoMember conn={conn.conn_id} target_id={goto_id}")
            if self.groups and goto_id:
                self.groups.goto_member(conn, goto_id)
        elif message_type == 0x28:  # setInviteMode
            mode = reader.read_byte() if reader.has_data else 0
            log.info(f"[GROUP] setInviteMode conn={conn.conn_id} mode={mode}")
            if self.groups:
                self.groups.set_invite_mode(conn, mode)
        # ── PvP duel opcodes ──
        elif message_type == 0x29:  # enterPVPZone
            log.info(f"[PVP] enterPVPZone conn={conn.conn_id}")
        elif message_type == 0x2A:  # requestPVPMatch
            log.info(f"[PVP] requestPVPMatch conn={conn.conn_id}")
        elif message_type == 0x2B:  # cancelPVPMatch
            log.info(f"[PVP] cancelPVPMatch conn={conn.conn_id}")
        elif message_type == 0x2C:  # leavePVP
            log.info(f"[PVP] leavePVP conn={conn.conn_id}")
        elif message_type == 0x2D:  # requestPVPDuel
            target_name = reader.read_cstring() if reader.has_data else ""
            log.info(f"[DUEL] requestPVPDuel conn={conn.conn_id} target='{target_name}'")
            if self.duels and target_name:
                self.duels.challenge(conn, target_name)
        elif message_type == 0x2E:  # acceptPVPDuel
            log.info(f"[DUEL] acceptPVPDuel conn={conn.conn_id}")
            if self.duels:
                self.duels.accept(conn)
        elif message_type == 0x2F:  # declinePVPDuel
            log.info(f"[DUEL] declinePVPDuel conn={conn.conn_id}")
            if self.duels:
                self.duels.decline(conn)
        else:
            log.debug(f"[GROUP/PVP] unknown type=0x{message_type:02X} len={len(data)}")

    # ── Channel 0x0C: social ──────────────────────────────────────────────
    def _handle_social_channel(self, conn: RRConnection, message_type: int,
                                data: bytes) -> None:
        """Handle social channel (ch 0x0C) — friends, ignores, tells, /who.
        Wires the already-implemented SocialManager.
        """
        if not self.social:
            log.debug(f"[SOCIAL] no social manager, ignoring ch=0x0C")
            return
        reader = framing.LEReader(data)
        if message_type == 0x00:  # add friend
            name = reader.read_cstring() if reader.has_data else ""
            log.info(f"[SOCIAL] add_friend conn={conn.conn_id} name='{name}'")
            if name:
                self.social.add_friend(conn, name)
        elif message_type == 0x01:  # remove friend
            name = reader.read_cstring() if reader.has_data else ""
            log.info(f"[SOCIAL] remove_friend conn={conn.conn_id} name='{name}'")
            if name:
                self.social.remove_friend(conn, name)
        elif message_type == 0x02:  # add ignore
            name = reader.read_cstring() if reader.has_data else ""
            log.info(f"[SOCIAL] add_ignore conn={conn.conn_id} name='{name}'")
            if name:
                self.social.add_ignore(conn, name)
        elif message_type == 0x03:  # remove ignore
            name = reader.read_cstring() if reader.has_data else ""
            log.info(f"[SOCIAL] remove_ignore conn={conn.conn_id} name='{name}'")
            if name:
                self.social.remove_ignore(conn, name)
        elif message_type == 0x04:  # /tell (send_tell)
            target = reader.read_cstring() if reader.has_data else ""
            message = reader.read_cstring() if reader.has_data else ""
            log.info(f"[SOCIAL] tell conn={conn.conn_id} to='{target}'")
            if target and message:
                self.social.send_tell(conn, target, message)
        elif message_type == 0x05:  # /who
            log.info(f"[SOCIAL] who conn={conn.conn_id}")
            self.social.send_who(conn)
        else:
            log.debug(f"[SOCIAL] unknown type=0x{message_type:02X} len={len(data)}")
