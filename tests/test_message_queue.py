"""MessageQueue.remove_where — predicate-based purge used to drop stale
per-entity packets (e.g. a dead mob's queued chase/follow updates) before the
entity's destroy is sent (the Code-9 'Invalid ComponentID' race)."""
from drserver.net.connection import MessageQueue


def test_remove_where_drops_matching_and_keeps_order():
    q = MessageQueue()
    for p in (b"\x35\x01\x00a", b"\x35\x02\x00b", b"\x35\x01\x00c", b"\x65d"):
        q.enqueue(p)

    removed = q.remove_where(lambda pkt: pkt[:3] == b"\x35\x01\x00")

    assert removed == 2
    assert q.dequeue_all() == [b"\x35\x02\x00b", b"\x65d"]


def test_remove_where_no_match_is_noop():
    q = MessageQueue()
    q.enqueue(b"\x35\x02\x00x")

    removed = q.remove_where(lambda pkt: pkt[:3] == b"\x35\x09\x09")

    assert removed == 0
    assert q.count == 1


def test_remove_where_on_empty_queue():
    q = MessageQueue()
    assert q.remove_where(lambda pkt: True) == 0
    assert q.is_empty()
