"""GroupManager — the C# ch-9 wire port, lifecycle choreography and the
group→dungeon-instance token (bible §7).

Packet layouts are pinned byte-for-byte against DR-Server GroupPackets.cs;
the ch-9 dispatcher and the 0x30/0x35/0x50 bodies are additionally verified
against the client binary (GroupClient dispatcher FUN_005f7e20, Ghidra
2026-07-09 — see managers/groups.py header).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.managers import groups as groups_mod
from drserver.managers.groups import (
    TALKBACK_PORT,
    GroupManager,
    build_join_talkback_group,
    build_member_health_mana,
    build_process_connected,
    build_process_invitation,
    build_remove_user,
    build_set_leader,
    build_solo_state,
    build_user_changed_group,
    build_user_changed_zone,
)
from drserver.net.game_server import GameServer, PUBLIC_INSTANCE_ID


# ── Test doubles ─────────────────────────────────────────────────────────────
class FakeChar:
    def __init__(self, char_id, name):
        self.id = char_id
        self.name = name


class FakeConn:
    _next_id = 1

    def __init__(self, login, char_id, zone="town"):
        self.conn_id = FakeConn._next_id
        FakeConn._next_id += 1
        self.login_name = login
        self.char_sql_id = char_id
        self.is_spawned = True
        self.current_zone_id = 1
        self.current_zone_name = zone
        self.current_zone_gc_type = f"world.{zone}"
        self.instance_id = 0
        self.hp_wire = 68096
        self.client_hp_wire = None
        self.player_pos_x = self.player_pos_y = self.player_pos_z = 0.0
        self.sent = []
        self.messages = []

    def send_to_client(self, data):
        self.sent.append(bytes(data))

    def send_system_message(self, message):
        self.messages.append(message)


def _server(*conns):
    srv = GameServer.__new__(GameServer)
    srv.next_instance_id = 1
    srv.connections = {c.conn_id: c for c in conns}
    srv.selected_character = {
        c.login_name: FakeChar(c.char_sql_id, c.login_name.capitalize())
        for c in conns}
    srv.spawned_avatar_ids = {c.login_name: 0x500 + c.conn_id for c in conns}
    srv.warps = []
    srv.change_zone_to_position = (
        lambda conn, zone, x, y, z: srv.warps.append((conn.login_name, zone, x, y, z)))
    srv.groups = GroupManager(srv)
    return srv


def _packets(conn, opcode):
    """The ch-9 packets of one opcode sent to ``conn``."""
    return [p for p in conn.sent if len(p) >= 2 and p[0] == 0x09 and p[1] == opcode]


def _form_pair(srv, leader, member):
    srv.groups.invite(leader, member.login_name)
    srv.groups.accept(member)
    for c in (leader, member):
        c.sent.clear()
    return srv.groups.get_group(leader)


# ── Packet bytes (GroupPackets.cs parity) ────────────────────────────────────
def test_process_connected_bytes():
    assert build_process_connected(0x11223344, 2, 3) == bytes(
        [0x09, 0x30, 0x44, 0x33, 0x22, 0x11, 0x02, 0x03])


def test_user_changed_group_bytes_one_member():
    pkt = build_user_changed_group(
        7, 0xAABBCCDD, 0xFF, 1, [(0x01020304, "Ann", 0x0506, True)])
    expected = bytes([0x09, 0x35,
                      7, 0, 0, 0,                    # groupId
                      0xDD, 0xCC, 0xBB, 0xAA,        # leaderCharSqlId
                      0xFF, 0x01,                    # flag, isOpen
                      0, 0, 0, 0, 0, 0, 0, 0,        # selfEntityId1/2
                      0x00, 0x00,                    # two pad bytes (C#)
                      0x01,                          # member count
                      0x04, 0x03, 0x02, 0x01])       # member charSqlId
    expected += b"Ann\x00" + bytes([0x06, 0x05, 0, 0, 0x01])
    assert pkt == expected


def test_solo_state_is_group_one_empty_roster():
    pkt = build_solo_state(0x42)
    assert pkt[:2] == bytes([0x09, 0x35])
    assert pkt[2:6] == (1).to_bytes(4, "little")     # groupId 1 = solo
    assert pkt[-1] == 0x00                            # member count 0


def test_invitation_bytes():
    pkt = build_process_invitation(0x0A, 3, "Bob")
    assert pkt == bytes([0x09, 0x32, 0x0A, 0, 0, 0, 3, 0, 0, 0]) + b"Bob\x00\x00"


def test_member_health_mana_packs_nibbles():
    pkt = build_member_health_mana(0x99, 7, 15)
    assert pkt == bytes([0x09, 0x4B, 0x99, 0, 0, 0, (7 << 4) | 15])


def test_set_leader_and_remove_user_bytes():
    assert build_set_leader(5, 0x30)[-1] == 0xFF
    assert build_remove_user(5, 0x30) == bytes(
        [0x09, 0x43, 5, 0, 0, 0, 0x30, 0, 0, 0])


def test_join_talkback_group_bytes():
    """0x50 body vs the client reader FUN_005fa5b0: u32 userId ×2, u8
    memberFlag, u32 talkGroupId, raw IPv4, u32 port (C# SendJoinTalkbackGroup)."""
    pkt = build_join_talkback_group(0x65, True, 7, bytes([192, 168, 1, 9]))
    assert pkt == (bytes([0x09, 0x50])
                   + (0x65).to_bytes(4, "little") * 2
                   + b"\x01"
                   + (7).to_bytes(4, "little")
                   + bytes([192, 168, 1, 9])
                   + TALKBACK_PORT.to_bytes(4, "little"))
    assert build_join_talkback_group(0x65, False, 7, b"\x7f\x00\x00\x01")[10] == 0x00


# ── Lifecycle choreography ───────────────────────────────────────────────────
def test_invite_sends_invitation_and_accept_forms_group():
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)

    srv.groups.invite(alice, "bob")
    invites = _packets(bob, 0x32)
    assert len(invites) == 1
    assert invites[0][2:6] == (101).to_bytes(4, "little")   # inviter charSqlId

    srv.groups.accept(bob)
    group = srv.groups.get_group(alice)
    assert group is not None and group.leader_login == "alice"
    assert [m.login for m in group.members] == ["alice", "bob"]
    # Both get 0x30 (once) + a 0x35 roster naming both members, leader first.
    for conn in (alice, bob):
        assert len(_packets(conn, 0x30)) == 1
        roster = _packets(conn, 0x35)[-1]
        assert roster[6:10] == (101).to_bytes(4, "little")  # leader char id
        assert b"Alice\x00" in roster and b"Bob\x00" in roster
        assert roster.index(b"Alice\x00") < roster.index(b"Bob\x00")


def test_only_leader_can_invite():
    alice, bob, eve = FakeConn("alice", 101), FakeConn("bob", 102), FakeConn("eve", 103)
    srv = _server(alice, bob, eve)
    _form_pair(srv, alice, bob)

    srv.groups.invite(bob, "eve")
    assert not _packets(eve, 0x32)
    assert any("leader" in m for m in bob.messages)


def test_leave_hands_leadership_and_resends_roster():
    alice, bob, carol = (FakeConn("alice", 101), FakeConn("bob", 102),
                         FakeConn("carol", 103))
    srv = _server(alice, bob, carol)
    srv.groups.invite(alice, "bob")
    srv.groups.accept(bob)
    srv.groups.invite(alice, "carol")
    srv.groups.accept(carol)
    for c in (alice, bob, carol):
        c.sent.clear()

    srv.groups.leave(alice)

    assert not srv.groups.is_in_group("alice")
    group = srv.groups.get_group(bob)
    assert group.leader_login == "bob"                 # first online member
    assert _packets(bob, 0x43) and _packets(carol, 0x43)   # remove fanned out
    solo = _packets(alice, 0x35)
    assert solo and solo[-1][-1] == 0x00               # leaver got solo state
    # Remaining members got a fresh 0x30 (leader change re-primes) + roster.
    assert _packets(bob, 0x30) and _packets(carol, 0x30)
    assert _packets(bob, 0x35) and _packets(carol, 0x35)


def test_second_to_last_leave_dissolves_group():
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)

    srv.groups.leave(bob)

    assert not srv.groups.is_in_group("alice")
    assert not srv.groups.is_in_group("bob")
    assert srv.groups._groups == {}
    assert _packets(alice, 0x35)[-1][-1] == 0x00       # last member went solo


def test_kick_by_char_id_removes_target():
    alice, bob, carol = (FakeConn("alice", 101), FakeConn("bob", 102),
                         FakeConn("carol", 103))
    srv = _server(alice, bob, carol)
    srv.groups.invite(alice, "bob")
    srv.groups.accept(bob)
    srv.groups.invite(alice, "carol")
    srv.groups.accept(carol)
    for c in (alice, bob, carol):
        c.sent.clear()

    srv.groups.kick_by_char_id(alice, 102)             # kick bob

    assert not srv.groups.is_in_group("bob")
    assert _packets(bob, 0x43)                         # target sees the remove too
    assert _packets(bob, 0x35)[-1][-1] == 0x00         # and goes solo
    group = srv.groups.get_group(alice)
    assert [m.login for m in group.members] == ["alice", "carol"]


def test_disconnect_keeps_seat_and_respawn_reonlines(monkeypatch):
    monkeypatch.setattr(groups_mod, "GROUP_PRIME_ENABLED", True)
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    group = _form_pair(srv, alice, bob)

    srv.groups.on_disconnect(bob)
    assert group.member("bob").is_online is False
    assert srv.groups.is_in_group("bob")               # seat kept
    assert _packets(alice, 0x49)                       # greyed out for alice

    alice.sent.clear()
    srv.groups.on_player_spawned(bob)
    assert group.member("bob").is_online is True
    assert _packets(alice, 0x4A)                       # re-onlined
    assert _packets(bob, 0x35)                         # roster re-sent to bob


def test_spawn_tail_fans_other_members_zone_labels(monkeypatch):
    monkeypatch.setattr(groups_mod, "GROUP_PRIME_ENABLED", True)
    alice, bob = FakeConn("alice", 101, zone="town"), FakeConn("bob", 102, zone="town")
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)

    bob.current_zone_name = "dungeon01_level01"
    srv.groups.on_player_spawned(bob)

    labels = _packets(alice, 0x4C)
    assert labels and b"dungeon01_level01\x00" in labels[-1]
    # bob receives alice's label, never his own
    bob_labels = _packets(bob, 0x4C)
    assert bob_labels and all(
        p[2:6] == (101).to_bytes(4, "little") for p in bob_labels)


def test_health_push_is_throttled_and_fanned():
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)

    alice.client_hp_wire = 30000
    srv.groups.on_member_hp(alice)
    srv.groups.on_member_hp(alice)                     # inside the 1 s window
    assert len(_packets(bob, 0x4B)) == 1
    pkt = _packets(bob, 0x4B)[0]
    assert pkt[2:6] == (101).to_bytes(4, "little")
    assert (pkt[6] >> 4) < 15                          # visibly damaged bar


def test_goto_member_warps_to_targets_zone_and_position():
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)
    bob.current_zone_name = "dungeon01_level01"
    bob.player_pos_x, bob.player_pos_y, bob.player_pos_z = 10.0, 20.0, 3.0

    srv.groups.goto_member(alice, 102)

    assert srv.warps == [("alice", "dungeon01_level01", 10.0, 20.0, 3.0)]


def test_solo_spawn_primes_group_subsystem(monkeypatch):
    """Every ungrouped zone join sends 0x30 (self charSqlId, diff 0, mode 0) +
    the empty solo 0x35 + the 0x50 talkback handoff keyed to the player's own
    id — the C# zone-transition else-branch. Without the prime the client has
    no group self-identity: no right-click invite option, roster packets
    ignored (live 2026-07-08)."""
    monkeypatch.setattr(groups_mod, "GROUP_PRIME_ENABLED", True)
    alice = FakeConn("alice", 101)
    srv = _server(alice)

    srv.groups.on_player_spawned(alice)

    primes = _packets(alice, 0x30)
    assert len(primes) == 1
    assert primes[0] == bytes([0x09, 0x30]) + (101).to_bytes(4, "little") + b"\x00\x00"
    solo = _packets(alice, 0x35)
    assert len(solo) == 1 and solo[0][-1] == 0x00      # empty roster
    talkback = _packets(alice, 0x50)
    assert len(talkback) == 1
    assert talkback[0][2:6] == (101).to_bytes(4, "little")    # userId = self
    assert talkback[0][11:15] == (101).to_bytes(4, "little")  # talkGroup = self
    assert talkback[0][-4:] == TALKBACK_PORT.to_bytes(4, "little")
    # Ungated (C#): the flag stays clear so a later group join still gets its 0x30.
    assert not getattr(alice, "group_connected_sent", False)


def test_priming_defaults_on():
    """DR_GROUP_PRIME defaults ON since 2026-07-09 (the 0x50 delta vs the
    working C# reference is closed; the env flag remains for A/B isolation)."""
    assert groups_mod.GROUP_PRIME_ENABLED is True


def test_grouped_zone_change_resends_roster_but_not_0x30(monkeypatch):
    """group_connected_sent is sticky across zone changes (C#): the roster 0x35
    and zone labels re-send on every join, the 0x30 only once per membership."""
    monkeypatch.setattr(groups_mod, "GROUP_PRIME_ENABLED", True)
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)          # 0x30 already delivered here

    srv.groups.on_player_spawned(alice)  # zone change while grouped

    assert _packets(alice, 0x30) == []   # sticky — no duplicate prime
    assert _packets(alice, 0x35)         # roster refresh still arrives
    assert _packets(alice, 0x50)         # talkback handoff rides every roster send


def test_roster_sends_carry_talkback_handoff():
    """C# SendGroupConnectedToAll appends 0x50 (talkGroup = group id) to every
    member after their 0x35; accept must deliver it to joiner AND veterans."""
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)

    srv.groups.invite(alice, "bob")
    srv.groups.accept(bob)

    gid = srv.groups.get_group(alice).group_id
    for conn in (alice, bob):
        handoffs = _packets(conn, 0x50)
        assert handoffs, f"no 0x50 for {conn.login_name}"
        assert handoffs[-1][11:15] == gid.to_bytes(4, "little")
        assert handoffs[-1][2:6] == conn.char_sql_id.to_bytes(4, "little")


def test_accept_cross_fans_zone_labels_for_colocated_members():
    """A group formed IN PLACE (both in town) must cross-fan 0x4C zone labels on
    accept — otherwise the client greys same-zone members as "different zone"
    until a zone transition (live user report 2026-07-09). This DIVERGES from C#
    (which sent labels only on the zone-join tail and has the same grey bug)."""
    alice, bob = FakeConn("alice", 101, zone="town"), FakeConn("bob", 102, zone="town")
    srv = _server(alice, bob)

    srv.groups.invite(alice, "bob")
    srv.groups.accept(bob)

    # alice receives bob's label, bob receives alice's — each carries the real
    # current zone ("town"), so the client un-greys the co-located member.
    alice_labels = _packets(alice, 0x4C)
    bob_labels = _packets(bob, 0x4C)
    assert alice_labels and b"town\x00" in alice_labels[-1]
    assert bob_labels and b"town\x00" in bob_labels[-1]
    assert alice_labels[-1][2:6] == (102).to_bytes(4, "little")   # bob's char id
    assert bob_labels[-1][2:6] == (101).to_bytes(4, "little")     # alice's char id
    assert _packets(alice, 0x4B) and _packets(bob, 0x4B)   # health still fans


def test_leave_rekeys_talkback_but_kick_does_not():
    """C# 0x22 leave re-keys the leaver's (and the dissolved last member's)
    talkback session to their own charSqlId; the 0x14 kick path sends no 0x50."""
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)

    srv.groups.leave(bob)
    bob_talkback = _packets(bob, 0x50)
    assert bob_talkback and bob_talkback[-1][11:15] == (102).to_bytes(4, "little")
    alice_talkback = _packets(alice, 0x50)          # last member, dissolved
    assert alice_talkback and alice_talkback[-1][11:15] == (101).to_bytes(4, "little")

    carol, dave = FakeConn("carol", 103), FakeConn("dave", 104)
    srv2 = _server(carol, dave)
    _form_pair(srv2, carol, dave)
    srv2.groups.kick_by_char_id(carol, 104)
    assert not _packets(dave, 0x50)                 # kicked: solo state only


# ── Group → dungeon-instance token (bible §7) ────────────────────────────────
def test_group_members_share_one_instance_token():
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)

    token_a = srv.groups.instance_token_for(alice)
    token_b = srv.groups.instance_token_for(bob)
    assert token_a == token_b and token_a >= 1
    # stable across repeated asks (no re-mint)
    assert srv.groups.instance_token_for(alice) == token_a


def test_solo_player_has_no_group_token():
    alice = FakeConn("alice", 101)
    srv = _server(alice)
    assert srv.groups.instance_token_for(alice) is None


def test_reset_instances_is_leader_only_and_mints_fresh_token():
    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    group = _form_pair(srv, alice, bob)
    first = srv.groups.instance_token_for(alice)

    srv.groups.reset_instances(bob)                    # not the leader
    assert group.instance_token == first
    srv.groups.reset_instances(alice)                  # leader
    assert group.instance_token != first
    assert srv.groups.instance_token_for(bob) == group.instance_token


def test_assign_instance_id_uses_group_token_in_dungeons():
    """End-to-end: grouped members entering a dungeon key to ONE copy; the
    same members in town still share the public instance 0."""
    class FakeZone:
        def __init__(self, name, is_town=False):
            self.name = name
            self.is_town = is_town

    alice, bob = FakeConn("alice", 101), FakeConn("bob", 102)
    srv = _server(alice, bob)
    _form_pair(srv, alice, bob)

    dungeon = FakeZone("dungeon01_level01")
    for conn in (alice, bob):
        conn.current_zone_id = 42
        conn.current_zone_name = "dungeon01_level01"
        srv._assign_instance_id(conn, dungeon)
    assert alice.instance_id == bob.instance_id != PUBLIC_INSTANCE_ID
    assert (alice.current_zone_id, alice.instance_id) == \
           (bob.current_zone_id, bob.instance_id)

    town = FakeZone("town", is_town=True)
    alice.current_zone_name = "town"
    srv._assign_instance_id(alice, town)
    assert alice.instance_id == PUBLIC_INSTANCE_ID

    # Ungrouped players in the same dungeon still split into private copies.
    solo = FakeConn("carol", 103)
    srv.connections[solo.conn_id] = solo
    solo.current_zone_id = 42
    solo.current_zone_name = "dungeon01_level01"
    srv._assign_instance_id(solo, dungeon)
    assert solo.instance_id != bob.instance_id


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
