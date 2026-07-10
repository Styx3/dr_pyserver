"""zlib compression for game packets.

Ported from C# PacketCompression / ZlibUtil. The C# code hand-rolled the zlib
header (0x78 0x9C) + raw DEFLATE + big-endian Adler-32 trailer; Python's stdlib
``zlib`` produces exactly that standard zlib stream, so it is a drop-in.
"""
from __future__ import annotations

import zlib


def compress(data: bytes) -> bytes:
    if not data:
        return data
    # level 6 yields the 0x78 0x9C header the client expects.
    return zlib.compress(data, 6)


def decompress(data: bytes) -> bytes:
    if not data or len(data) < 6:
        return data
    return zlib.decompress(data)
