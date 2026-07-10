"""Auth crypto + framing tests: Go-Blowfish endian round-trip, frame structure, login decode."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.net import auth_crypto as ac
from drserver.util.crypto import DESEncryption

KEY = "[;'.]94-31==-%&@!^+]"
DES_KEY = "TEST"


def test_go_blowfish_roundtrip():
    pt = b"\x00\x01\x02\x03\x04\x05\x06\x07" * 4  # 32 bytes, multiple of 8
    ct = ac._blowfish_encrypt(pt, KEY)
    assert ct != pt
    assert ac._blowfish_decrypt(ct, KEY) == pt


def test_frame_structure_and_decrypt():
    # LoginOk-style payload (35 bytes -> +1 type = 36, already /8 = 36? 36%8=4 -> pad to 40)
    payload = bytes(range(35))
    frame = ac.build_auth_frame(0x03, payload, KEY)

    # Length prefix is the total frame length, LE.
    declared = struct.unpack_from("<H", frame, 0)[0]
    assert declared == len(frame)

    body = frame[2:]
    assert len(body) % 8 == 0  # encrypted body is block-aligned

    # Decrypting the body recovers: type + payload + zero-pad + checksum(4) + 4 zero
    plain = ac._blowfish_decrypt(body, KEY)
    assert plain[0] == 0x03
    assert plain[1 : 1 + len(payload)] == payload

    # Verify the embedded XOR checksum over the padded message.
    msg_len = len(plain) - 8  # strip 8-byte trailer
    checksum = 0
    for i in range(0, msg_len, 4):
        checksum ^= struct.unpack_from("<I", plain, i)[0]
    embedded = struct.unpack_from("<I", plain, msg_len)[0]
    assert checksum == embedded
    assert plain[msg_len + 4 : msg_len + 8] == b"\x00\x00\x00\x00"


def test_decode_login():
    user = "Styx3"
    password = "secret"
    blob = bytearray(30)
    blob[0 : len(user)] = user.encode("ascii")
    blob[14 : 14 + len(password)] = password.encode("ascii")

    des = DESEncryption(DES_KEY)
    block24 = des.encrypt(bytes(blob[0:24]))  # client DES-encrypts first 24
    tail6 = bytes(blob[24:30])                 # last 6 plaintext

    du, dp = ac.decode_login(block24, tail6, DES_KEY)
    assert du == user
    assert dp == password


def test_ip_pack():
    assert ac.ip_to_uint32_le("127.0.0.1") == (127 | (0 << 8) | (0 << 16) | (1 << 24))


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
