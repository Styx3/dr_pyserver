"""Foundation-layer smoke tests: byte IO, crypto, compression round-trips."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.util.byte_io import LEReader, LEWriter
from drserver.util.compression import compress, decompress
from drserver.util.crypto import BlowfishEncryption, DESEncryption


def test_byte_io_roundtrip():
    w = LEWriter()
    w.write_byte(0x07)
    w.write_uint16(0x1234)
    w.write_uint24(0x010203)
    w.write_uint32(0xDEADBEEF)
    w.write_int32(-5)
    w.write_uint64(0x1122334455667788)
    w.write_float(3.5)
    w.write_string("hello")
    w.write_cstring("world")
    w.write_bytes(b"\x01\x02\x03")
    data = w.to_array()

    r = LEReader(data)
    assert r.read_byte() == 0x07
    assert r.read_uint16() == 0x1234
    assert r.read_uint24() == 0x010203
    assert r.read_uint32() == 0xDEADBEEF
    assert r.read_int32() == -5
    assert r.read_uint64() == 0x1122334455667788
    assert r.read_float() == 3.5
    assert r.read_string() == "hello"
    assert r.read_cstring() == "world"
    assert r.read_bytes(3) == b"\x01\x02\x03"
    assert r.remaining == 0


def test_uint16_at_backfill():
    w = LEWriter()
    w.write_uint16(0)  # placeholder size
    w.write_bytes(b"abcd")
    w.write_uint16_at(0, w.length)
    out = w.to_array()
    assert out[0] == 6 and out[1] == 0  # 6 bytes total, LE


def test_compression_roundtrip():
    payload = b"the quick brown fox" * 20
    comp = compress(payload)
    assert comp[:2] == b"\x78\x9c"  # zlib header the client expects
    assert decompress(comp) == payload


def test_blowfish_roundtrip():
    bf = BlowfishEncryption("[;'.]94-31==-%&@!^+]")
    pt = b"AuthPacketData!!"  # 16 bytes, multiple of 8
    ct = bf.encrypt(pt)
    assert len(ct) == 16
    assert bf.decrypt(ct) == pt


def test_blowfish_zero_pads():
    bf = BlowfishEncryption("TESTKEY")
    ct = bf.encrypt(b"abc")  # 3 bytes -> padded to 8
    assert len(ct) == 8
    assert bf.decrypt(ct)[:3] == b"abc"


def test_des_roundtrip():
    des = DESEncryption("TEST")
    pt = b"12345678"
    ct = des.encrypt(pt)
    assert des.decrypt(ct)[:8] == pt


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
