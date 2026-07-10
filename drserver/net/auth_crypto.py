"""Auth-channel crypto + framing.

The auth protocol Blowfish-encrypts each 8-byte block, but the client stores
32-bit words little-endian while the Blowfish engine is big-endian-oriented,
so every 4-byte word is byte-swapped before and after the cipher.

Wire frame: [uint16 totalLen LE][blowfish body], where body =
  msgType(1) + payload, padded to a multiple of 8, then an 8-byte trailer:
  4-byte XOR checksum (over 32-bit LE words of the padded msg) + 4 zero bytes,
  the whole thing Blowfish-encrypted with the word swap.
"""
from __future__ import annotations

import struct

from ..util.crypto import BlowfishEncryption, DESEncryption


def _le_words_to_be(block8: bytes) -> bytes:
    v1, v2 = struct.unpack("<II", block8)
    return struct.pack(">II", v1, v2)


def _be_words_to_le(block8: bytes) -> bytes:
    v1, v2 = struct.unpack(">II", block8)
    return struct.pack("<II", v1, v2)


def _blowfish_decrypt(data: bytes, key: str) -> bytes:
    bf = BlowfishEncryption(key)
    out = bytearray(len(data))
    for i in range(0, (len(data) // 8) * 8, 8):
        block = data[i : i + 8]
        dec = bf.decrypt(_le_words_to_be(block))
        out[i : i + 8] = _be_words_to_le(dec)
    return bytes(out)


def _blowfish_encrypt(data: bytes, key: str) -> bytes:
    bf = BlowfishEncryption(key)
    out = bytearray(len(data))
    for i in range(0, (len(data) // 8) * 8, 8):
        block = data[i : i + 8]
        enc = bf.encrypt(_le_words_to_be(block))
        out[i : i + 8] = _be_words_to_le(enc)
    return bytes(out)


def build_auth_frame(server_msg_type: int, payload: bytes, blowfish_key: str) -> bytes:
    msg = bytes([server_msg_type & 0xFF]) + payload
    rem = len(msg) % 8
    if rem != 0:
        msg = msg + b"\x00" * (8 - rem)
    checksum = 0
    for i in range(0, len(msg), 4):
        word = struct.unpack_from("<I", msg, i)[0]
        checksum ^= word
    final = msg + struct.pack("<I", checksum) + b"\x00\x00\x00\x00"
    encrypted = _blowfish_encrypt(final, blowfish_key)
    frame_len = len(encrypted) + 2
    return struct.pack("<H", frame_len) + encrypted


def decrypt_auth_body(data: bytes, blowfish_key: str) -> bytes:
    return _blowfish_decrypt(data, blowfish_key)


def decode_login(block24: bytes, tail6: bytes, des_key: str) -> tuple[str, str]:
    des = DESEncryption(des_key)
    decrypted = des.decrypt(block24)
    all_bytes = decrypted[:24] + tail6[:6]
    user = all_bytes[0:14].split(b"\x00", 1)[0].decode("ascii", "replace")
    password = all_bytes[14:30].split(b"\x00", 1)[0].decode("ascii", "replace")
    return user, password


def ip_to_uint32_le(ip: str) -> int:
    p = [int(x) for x in ip.split(".")]
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24)
