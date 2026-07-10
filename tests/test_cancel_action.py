"""CancelAction (ch7 ComponentUpdate sub-message 0x03 on a non-QuestManager
component) — port of C# ``HandleCancelAction`` (UGS:15481).

The client sends it when the player moves to break off an in-progress
auto-approach (attack on a far target / NPC walk-up). The server must echo
``[0x35][cid][0x03][sessionId]`` + the owner synch trailer or the client's
action state machine stays locked in the auto-run. Regression for the live
"can't cancel attack/NPC approach with movement" bug (2026-06-10).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.net import movement
from drserver.util.byte_io import LEReader


class _MsgQueue:
    def __init__(self):
        self.items = []

    def enqueue(self, msg):
        self.items.append(msg)


def _conn(hp_wire=68096):
    return SimpleNamespace(
        login_name="Styx3",
        quest_manager_id=0x0218,
        equipment_component_id=9001,
        unit_container_id=9002,
        hp_wire=hp_wire,
        message_queue=_MsgQueue(),
        # The cancel ack rides the interval queue (0x0D-frame carrier): the
        # client spams CancelAction at frame rate when an action is stuck, and
        # per-tick-flushed acks blow the §2 one-message-per-133ms budget.
        interval_message_queue=_MsgQueue(),
    )


def _server(combat=None):
    return SimpleNamespace(quests=None, combat=combat)


def test_cancel_action_echoes_ack_with_owner_synch_trailer():
    # Arrange — [cid=0x0215][sub=0x03][sessionId=0x10]
    conn = _conn(hp_wire=68096)
    body = bytes.fromhex("150203" + "10")

    # Act
    handled = movement._component_update(_server(), conn, LEReader(body))

    # Assert — consumed, and the ack is [0x35][cid][0x03][sid][0x02][hp:u32],
    # delivered on the interval queue (never the per-tick flush).
    assert handled is True
    assert len(conn.interval_message_queue.items) == 1
    expected = (bytes([0x35, 0x15, 0x02, 0x03, 0x10, 0x02])
                + (68096).to_bytes(4, "little"))
    assert conn.interval_message_queue.items[0] == expected
    assert conn.message_queue.items == []


def test_cancel_action_clears_pending_swing_replay():
    # Arrange — a fake combat manager records the use-target clear.
    cleared = []
    combat = SimpleNamespace(clear_combat=lambda key: cleared.append(key))
    conn = _conn()

    # Act
    handled = movement._component_update(
        _server(combat), conn, LEReader(bytes.fromhex("15020310")))

    # Assert — the cancelled attack's replay cycle is dropped for this conn.
    assert handled is True
    assert cleared == ["Styx3"]


def test_cancel_action_on_quest_manager_component_stays_quest_abandon():
    # Arrange — sub-message 0x03 on the QM component is quest ABANDON, not
    # CancelAction; it must still route to the quest handler.
    quest_calls = []
    conn = _conn()
    server = SimpleNamespace(
        combat=None,
        quests=SimpleNamespace(
            handle_component_update=lambda c, sub, r: quest_calls.append(sub)),
    )
    body = conn.quest_manager_id.to_bytes(2, "little") + bytes([0x03, 0x01, 0x00, 0x00, 0x00])

    # Act
    handled = movement._component_update(server, conn, LEReader(body))

    # Assert — quest path taken, no cancel ack enqueued.
    assert handled is True
    assert quest_calls == [0x03]
    assert conn.message_queue.items == []
    assert conn.interval_message_queue.items == []
