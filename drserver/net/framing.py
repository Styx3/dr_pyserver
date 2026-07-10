"""THCSockets framing for the game server.

Ported from UnityGameServer.cs ProcessMessage / CalculateMessageLength /
HandleCompressedA / HandleCompressedE / SendCompressedA / SendMessage0x10.

The game-server wire protocol (after the queue handoff) is *not* additionally
encrypted — it is Go/Blowfish-style framing only. A TCP read may contain several
concatenated frames; ``split_frames`` chops the buffer into individual frames,
each of which is dispatched by ``ProcessSingleMessage`` in the server.

Frame layout by leading message type byte:
  0x02 Ping        - echo; variable length (consume rest of buffer)
  0x03 Connect     - 4 bytes total: [0x03][uint24 clientId]
  0x0A CompressedA - [0x0A][uint24 peer][uint32 bodyLen][body...]; total = 8 + bodyLen
  0x0E CompressedE - same length rule as 0x0A
  0x10 Direct      - [0x10][uint24 peer][uint24 bodyLen][channel][payload]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..util.byte_io import LEReader, LEWriter
from ..util import compression


# ─────────────────────────────────────────────────────────────────────────────
# Incoming: split a TCP buffer into individual frames
# ─────────────────────────────────────────────────────────────────────────────

def calculate_message_length(data: bytes, offset: int, message_type: int) -> int:
    """Length of the single frame starting at ``offset`` (CalculateMessageLength)."""
    remaining = len(data) - offset
    if message_type == 0x03:        # Connect — fixed 4 bytes
        return 4
    if message_type in (0x0A, 0x0E):  # CompressedA / CompressedE
        if remaining < 8:
            return -1
        body_len = int.from_bytes(data[offset + 4:offset + 8], "little")
        return 8 + body_len
    # 0x02 ping, 0x10 direct, and unknown types consume the rest of the buffer.
    return remaining


def split_frames(data: bytes) -> list[bytes]:
    """Split a raw TCP read into individual frames (ProcessMessage loop)."""
    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        message_type = data[offset]
        msg_len = calculate_message_length(data, offset, message_type)
        if msg_len <= 0 or offset + msg_len > len(data):
            break
        frames.append(data[offset:offset + msg_len])
        offset += msg_len
    return frames


@dataclass(frozen=True)
class ChannelMessage:
    """A decoded channel message (channel + type + inner payload)."""
    channel: int
    message_type: int
    payload: bytes


def parse_compressed_a(data: bytes) -> ChannelMessage:
    """Decode a 0x0A CompressedA frame -> (channel, messageType, inflated payload)."""
    reader = LEReader(data)
    reader.read_byte()              # 0x0A
    reader.read_uint24()            # peer id
    body_len = reader.read_uint32()
    channel = reader.read_byte()
    message_type = reader.read_byte()
    reader.read_byte()              # 0x00
    reader.read_uint32()            # uncompressed length (advisory)
    compressed = reader.read_bytes(body_len - 7)
    payload = compression.decompress(compressed)
    return ChannelMessage(channel, message_type, payload)


def parse_compressed_e(data: bytes) -> Optional[ChannelMessage]:
    """Decode a 0x0E CompressedE frame. channel/type are the first 2 payload bytes."""
    reader = LEReader(data)
    reader.read_byte()              # 0x0E
    reader.read_uint24()            # dest
    body_len = reader.read_uint24()
    reader.read_byte()              # 0x00
    reader.read_uint24()            # source
    reader.read_bytes(5)            # skip 5
    reader.read_uint32()            # uncompressed length (advisory)
    compressed = reader.read_bytes(body_len - 12)
    payload = compression.decompress(compressed)
    if len(payload) < 2:
        return None
    return ChannelMessage(payload[0], payload[1], payload[2:])


def parse_direct(data: bytes) -> ChannelMessage:
    """Decode a 0x10 Direct frame -> channel message (messageType is 0)."""
    reader = LEReader(data)
    reader.read_byte()              # 0x10
    reader.read_uint24()            # peer id
    body_len = reader.read_uint24()
    channel = reader.read_byte()
    payload = reader.read_bytes(body_len)
    return ChannelMessage(channel, 0, payload)


# ─────────────────────────────────────────────────────────────────────────────
# Outgoing: build frames
# ─────────────────────────────────────────────────────────────────────────────

def build_compressed_a(peer_id: int, dest: int, message_type: int, inner: bytes) -> bytes:
    """SendCompressedA: deflate ``inner`` and wrap it in a 0x0A frame."""
    compressed = compression.compress(inner)
    writer = LEWriter()
    writer.write_byte(0x0A)
    writer.write_uint24(peer_id & 0xFFFFFF)
    writer.write_uint32(len(compressed) + 7)
    writer.write_byte(dest)
    writer.write_byte(message_type)
    writer.write_byte(0x00)
    writer.write_uint32(len(inner))
    writer.write_bytes(compressed)
    return writer.to_array()


def build_message_0x10(client_id: int, channel: int, payload: bytes) -> bytes:
    """SendMessage0x10: uncompressed direct frame on ``channel``."""
    writer = LEWriter()
    writer.write_byte(0x10)
    writer.write_uint24(client_id & 0xFFFFFF)
    writer.write_uint24(len(payload))
    writer.write_byte(channel)
    if payload:
        writer.write_bytes(payload)
    return writer.to_array()


def build_connect_response(client_id: int) -> bytes:
    """HandleConnect reply: [0x04][uint24 clientId][uint32 0]."""
    writer = LEWriter()
    writer.write_byte(0x04)
    writer.write_uint24(client_id & 0xFFFFFF)
    writer.write_uint32(0)
    return writer.to_array()


def build_ping_response(ping_frame: bytes) -> bytes:
    """HandlePing reply: [0x02] + echo of the original payload tail."""
    writer = LEWriter()
    writer.write_byte(0x02)
    if len(ping_frame) > 1:
        writer.write_bytes(ping_frame[1:])
    return writer.to_array()


def read_connect_client_id(connect_frame: bytes) -> int:
    """Read the uint24 client/peer id out of a 0x03 connect frame."""
    reader = LEReader(connect_frame)
    reader.read_byte()              # 0x03
    return reader.read_uint24()
