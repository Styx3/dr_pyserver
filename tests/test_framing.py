"""Tests for net/framing — THCSockets frame parse/build round-trips.

Run: ./.venv/bin/python tests/test_framing.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.net import framing
from drserver.util.byte_io import LEWriter

_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}")


def test_connect_length_is_four():
    frame = bytes([0x03, 0x11, 0x22, 0x33, 0x0A, 0xAA])  # connect + trailing junk
    check("connect_len_4", framing.calculate_message_length(frame, 0, 0x03) == 4)


def test_compressed_a_length():
    # [0x0A][uint24][uint32 bodyLen=5][5 body bytes]
    w = LEWriter()
    w.write_byte(0x0A)
    w.write_uint24(0x010203)
    w.write_uint32(5)
    w.write_bytes(b"\x01\x02\x03\x04\x05")
    data = w.to_array()
    check("compressed_a_len", framing.calculate_message_length(data, 0, 0x0A) == 8 + 5)


def test_split_frames_connect_then_compressed():
    connect = bytes([0x03, 0x01, 0x00, 0x00])
    comp = framing.build_compressed_a(0x123456, 0x01, 0x0F, b"hello world")
    frames = framing.split_frames(connect + comp)
    check("split_count", len(frames) == 2)
    check("split_first_connect", frames[0] == connect)
    check("split_second_compressed", frames[1] == comp)


def test_compressed_a_roundtrip():
    inner = bytes([0x04, 0x05]) + b"payload-bytes-here-12345"
    frame = framing.build_compressed_a(0xABCDEF, 0x01, 0x0F, inner)
    check("compressed_a_leading_byte", frame[0] == 0x0A)
    decoded = framing.parse_compressed_a(frame)
    # build uses dest=0x01 as the "channel" slot, 0x0F as messageType.
    check("compressed_a_channel", decoded.channel == 0x01)
    check("compressed_a_type", decoded.message_type == 0x0F)
    check("compressed_a_payload", decoded.payload == inner)


def test_connect_response():
    cid = framing.read_connect_client_id(bytes([0x03, 0xEF, 0xCD, 0xAB]))
    check("connect_client_id", cid == 0xABCDEF)
    resp = framing.build_connect_response(cid)
    check("connect_resp_op", resp[0] == 0x04)
    check("connect_resp_cid", framing.read_connect_client_id(b"\x04" + resp[1:4]) == cid)
    check("connect_resp_len", len(resp) == 8)


def test_ping_response_echoes():
    ping = bytes([0x02, 0xDE, 0xAD, 0xBE, 0xEF])
    resp = framing.build_ping_response(ping)
    check("ping_op", resp[0] == 0x02)
    check("ping_echo", resp == ping)


def test_message_0x10():
    payload = bytes([0x03])
    frame = framing.build_message_0x10(0x112233, 0x0A, payload)
    check("m10_op", frame[0] == 0x10)
    decoded = framing.parse_direct(frame)
    check("m10_channel", decoded.channel == 0x0A)
    check("m10_payload", decoded.payload == payload)


def test_real_client_first_frame_header():
    """Ground-truth: the first 32 bytes a REAL client sends on the game channel.

    Captured live 2026-05-31 (server.log: "[GAME] conn 2 first bytes: ...").
    This validates VALIDATION_PLAN.md item 1.4 against the client, not the C#
    emulator: the inbound game channel is plaintext THCSockets framing + zlib,
    with NO per-packet cipher. If a cipher were applied, neither the 0x0A frame
    byte nor the 78 9c zlib magic would appear in cleartext at these offsets.
    The header fields land exactly where parse_compressed_a reads them, and the
    server inflated this body and produced "initial login for 'Styx3'".
    """
    header = bytes.fromhex(
        "0add7b00750100000000006e020000789ca551514e023110c5df39c51c40b1bb"
    )
    # CalculateMessageLength must see a 0x0A frame of 8 + bodyLen(373) = 381.
    assert header[0] == 0x0A
    assert framing.calculate_message_length(header, 0, 0x0A) == 381
    # Header fields decode at the parse_compressed_a offsets.
    peer = header[1] | header[2] << 8 | header[3] << 16
    assert peer == 0x7BDD
    body_len = int.from_bytes(header[4:8], "little")
    assert body_len == 373
    assert header[8] == 0x00            # channel  (initial login)
    assert header[9] == 0x00            # msg type (initial login)
    assert header[10] == 0x00           # pad
    uncompressed_len = int.from_bytes(header[11:15], "little")
    assert uncompressed_len == 622
    # The compressed body is zlib (default compression) — plaintext channel.
    assert header[15:17] == b"\x78\x9c"
    check("real_client_first_frame_header", True)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_passed}/{_passed + _failed} passed")
    sys.exit(1 if _failed else 0)
