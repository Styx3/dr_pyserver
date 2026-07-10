"""Group-channel inbound routing.

★ The live client sends its group requests (invite 0x12/0x16, accept 0x20,
leave 0x22, kick/leader/difficulty, PvP 0x29-0x2F) on **channel 9**, matching
the server's own outbound group packets (inner byte 0x09) and the client's
shared group channel object. C# routes BOTH ``case 9`` (HandleGroupChannel)
and ``case 11`` (HandleGroupClientChannel) to the group dispatcher.

The Python server historically listened only on channel 0x0B (11), so every
client→server group message landed in the silent "unhandled channel" branch
and was dropped — the root cause of "right-click invite / accept do nothing"
(server→client 0x32 via @invite still worked, since that needs no inbound).
These tests pin channel 9 to the group handler so the regression can't return.
"""
from drserver.net.framing import ChannelMessage
from drserver.net.game_server import GameServer


class _Conn:
    is_spawned = True
    login_name = "Styx3"
    conn_id = 1


class _Server:
    def __init__(self):
        self.group_calls = []          # (message_type, payload)
        self.unhandled = []            # (channel, message_type)

    def _handle_group_channel(self, conn, message_type, payload):
        self.group_calls.append((message_type, payload))


def _dispatch(channel: int, message_type: int, payload: bytes = b""):
    server, conn = _Server(), _Conn()
    GameServer._dispatch_channel(
        server, conn, ChannelMessage(channel, message_type, payload))
    return server


def test_channel_9_routes_to_group_handler():
    # 0x20 acceptInvite on channel 9 — the packet the client sends when the
    # user clicks "Accept" on the invite dialog.
    server = _dispatch(9, 0x20, b"\x01\x02\x03\x04")
    assert server.group_calls == [(0x20, b"\x01\x02\x03\x04")]


def test_channel_9_invite_and_leave_route_to_group_handler():
    for op in (0x12, 0x16, 0x22, 0x2D):        # invite-id/name, leave, duel
        server = _dispatch(9, op, b"")
        assert server.group_calls == [(op, b"")], f"op 0x{op:02X} not routed"


def test_channel_0x0B_still_routes_to_group_handler():
    # C# case 11 also reaches the group dispatcher — keep the legacy route.
    server = _dispatch(0x0B, 0x20, b"")
    assert server.group_calls == [(0x20, b"")]
