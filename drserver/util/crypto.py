"""Blowfish and DES for auth packets.

Ported from C# BlowfishEncryption (BouncyCastle BlowfishEngine) and DESEncryption
(DESCryptoServiceProvider). Both ciphers use ECB with manual zero-padding to the
8-byte block size. BouncyCastle/pycryptodome/OpenSSL all use big-endian Blowfish
word order, so the cipher is compatible.

Requires pycryptodome (Crypto.Cipher.Blowfish, Crypto.Cipher.DES).
"""
from __future__ import annotations

from Crypto.Cipher import DES as _DES
from Crypto.Cipher import Blowfish as _Blowfish

_BLOCK = 8


def _zero_pad(data: bytes) -> bytes:
    rem = len(data) % _BLOCK
    if rem == 0:
        return data
    return data + b"\x00" * (_BLOCK - rem)


class BlowfishEncryption:
    """Blowfish-ECB. Key is ASCII text with a trailing NUL appended (matches C#)."""

    def __init__(self, key: str):
        if not key:
            raise ValueError("key is empty")
        if not key.endswith("\0"):
            key += "\0"
        self._key = key.encode("ascii")

    def encrypt(self, data: bytes) -> bytes:
        if not data:
            return data
        cipher = _Blowfish.new(self._key, _Blowfish.MODE_ECB)
        return cipher.encrypt(_zero_pad(data))

    def decrypt(self, data: bytes) -> bytes:
        if not data or len(data) % _BLOCK != 0:
            return data
        cipher = _Blowfish.new(self._key, _Blowfish.MODE_ECB)
        return cipher.decrypt(data)


class DESEncryption:
    """DES-ECB, zero padding, all-zero IV (ECB ignores IV). Key truncated/padded to 8 bytes."""

    def __init__(self, key: str):
        if not key:
            raise ValueError("key is empty")
        kb = key.encode("ascii")[:_BLOCK]
        self._key = kb + b"\x00" * (_BLOCK - len(kb))

    def encrypt(self, data: bytes) -> bytes:
        if not data:
            return data
        cipher = _DES.new(self._key, _DES.MODE_ECB)
        return cipher.encrypt(_zero_pad(data))

    def decrypt(self, data: bytes) -> bytes:
        if not data:
            return data
        cipher = _DES.new(self._key, _DES.MODE_ECB)
        return cipher.decrypt(_zero_pad(data))
