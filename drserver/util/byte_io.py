"""Little-endian binary read/write for the DR network protocol.

Ported from C# DungeonRunners.Utilities.LEReader / LEWriter. All protocol
integers are little-endian (the client is x86). Method names are snake_case
Python equivalents of the C# API.
"""
from __future__ import annotations

import struct


class LEReader:
    """Little-endian binary reader over an immutable bytes buffer."""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes):
        if data is None:
            raise ValueError("data is None")
        self._data = bytes(data)
        self._pos = 0

    @property
    def position(self) -> int:
        return self._pos

    @property
    def length(self) -> int:
        return len(self._data)

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    @property
    def has_data(self) -> bool:
        return self._pos < len(self._data)

    def read_byte(self) -> int:
        if self._pos >= len(self._data):
            raise EOFError("Attempted to read beyond end of data")
        b = self._data[self._pos]
        self._pos += 1
        return b

    def read_bytes(self, count: int) -> bytes:
        if self._pos + count > len(self._data):
            raise EOFError(
                f"Attempted to read {count} bytes but only {self.remaining} remaining"
            )
        result = self._data[self._pos : self._pos + count]
        self._pos += count
        return result

    def read_uint16(self) -> int:
        if self._pos + 2 > len(self._data):
            raise EOFError("Not enough data to read UInt16")
        value = self._data[self._pos] | (self._data[self._pos + 1] << 8)
        self._pos += 2
        return value

    def read_uint24(self) -> int:
        if self._pos + 3 > len(self._data):
            raise EOFError("Not enough data to read UInt24")
        d = self._data
        p = self._pos
        value = d[p] | (d[p + 1] << 8) | (d[p + 2] << 16)
        self._pos += 3
        return value

    def read_uint32(self) -> int:
        if self._pos + 4 > len(self._data):
            raise EOFError("Not enough data to read UInt32")
        d = self._data
        p = self._pos
        value = d[p] | (d[p + 1] << 8) | (d[p + 2] << 16) | (d[p + 3] << 24)
        self._pos += 4
        return value

    def read_int32(self) -> int:
        value = self.read_uint32()
        return value - 0x100000000 if value >= 0x80000000 else value

    def read_uint64(self) -> int:
        if self._pos + 8 > len(self._data):
            raise EOFError("Not enough data to read UInt64")
        value = int.from_bytes(self._data[self._pos : self._pos + 8], "little", signed=False)
        self._pos += 8
        return value

    def read_float(self) -> float:
        if self._pos + 4 > len(self._data):
            raise EOFError("Not enough data to read Float")
        value = struct.unpack_from("<f", self._data, self._pos)[0]
        self._pos += 4
        return value

    def read_string(self) -> str:
        """Length-prefixed (uint16) UTF-8 string."""
        length = self.read_uint16()
        if length == 0:
            return ""
        if self._pos + length > len(self._data):
            raise EOFError(f"String length {length} exceeds remaining data")
        result = self._data[self._pos : self._pos + length].decode("utf-8", "replace")
        self._pos += length
        return result

    def read_cstring(self) -> str:
        """Null-terminated UTF-8 string."""
        start = self._pos
        data = self._data
        while self._pos < len(data) and data[self._pos] != 0:
            self._pos += 1
        if self._pos >= len(data):
            raise EOFError("C-string not null-terminated")
        result = data[start : self._pos].decode("utf-8", "replace") if self._pos > start else ""
        self._pos += 1  # consume terminator
        return result

    def peek_remaining(self) -> bytes:
        return self._data[self._pos :]

    def get_raw_bytes(self, start: int, length: int) -> bytes:
        return self._data[start : start + length]

    def skip(self, count: int) -> None:
        if self._pos + count > len(self._data):
            raise EOFError(f"Cannot skip {count} bytes, only {self.remaining} remaining")
        self._pos += count

    def seek(self, position: int) -> None:
        if position < 0 or position > len(self._data):
            raise IndexError(position)
        self._pos = position


class LEWriter:
    """Little-endian binary writer backed by a mutable bytearray."""

    __slots__ = ("_buf",)

    def __init__(self):
        self._buf = bytearray()

    def write_byte(self, value: int) -> None:
        self._buf.append(value & 0xFF)

    def write_bytes(self, data: bytes) -> None:
        if data:
            self._buf.extend(data)

    def write_uint16(self, value: int) -> None:
        self._buf.append(value & 0xFF)
        self._buf.append((value >> 8) & 0xFF)

    def write_uint16_at(self, position: int, value: int) -> None:
        """Backfill a uint16 at an earlier position (e.g. a size field)."""
        if position < 0 or position + 1 >= len(self._buf):
            raise IndexError(f"Position {position} out of range for buffer size {len(self._buf)}")
        self._buf[position] = value & 0xFF
        self._buf[position + 1] = (value >> 8) & 0xFF

    def write_uint24(self, value: int) -> None:
        self._buf.append(value & 0xFF)
        self._buf.append((value >> 8) & 0xFF)
        self._buf.append((value >> 16) & 0xFF)

    def write_uint32(self, value: int) -> None:
        value &= 0xFFFFFFFF
        self._buf.append(value & 0xFF)
        self._buf.append((value >> 8) & 0xFF)
        self._buf.append((value >> 16) & 0xFF)
        self._buf.append((value >> 24) & 0xFF)

    def write_int32(self, value: int) -> None:
        self.write_uint32(value & 0xFFFFFFFF)

    def write_uint64(self, value: int) -> None:
        self._buf.extend((value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little"))

    def write_float(self, value: float) -> None:
        self._buf.extend(struct.pack("<f", value))

    def write_string(self, value: str) -> None:
        """Length-prefixed (uint16) UTF-8 string."""
        if not value:
            self.write_uint16(0)
            return
        data = value.encode("utf-8")
        self.write_uint16(len(data))
        self._buf.extend(data)

    def write_cstring(self, value: str) -> None:
        """Null-terminated UTF-8 string."""
        if value:
            self._buf.extend(value.encode("utf-8"))
        self._buf.append(0)

    def to_array(self) -> bytes:
        return bytes(self._buf)

    # alias matching C# GetBuffer()
    def get_buffer(self) -> bytes:
        return bytes(self._buf)

    @property
    def length(self) -> int:
        return len(self._buf)

    @property
    def position(self) -> int:
        return len(self._buf)

    def clear(self) -> None:
        self._buf.clear()
