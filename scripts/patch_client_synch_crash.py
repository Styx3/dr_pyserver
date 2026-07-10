#!/usr/bin/env python3
"""Patch the Dungeon Runners client to stop the avatar HP-synch crash.

WHY THIS EXISTS
---------------
Combat is CLIENT-AUTHORITATIVE: the client self-simulates its avatar's HP and
the engine's ``ClientEntityManager`` compares any server-sent avatar HP against
the local value with ZERO tolerance (``FUN_005dd900``). On a mismatch it shows
"Entity synch error detected" and the Avatar process exits ``0xc000013a``.

The compare is skipped only in peaceful/town zones (the avatar action's
``+0x95`` bit0 is set there). In combat zones the bit is clear, so the per-frame
``0x36`` HP heartbeat — which the client REQUIRES for movement + world pacing —
always triggers the compare. The client never reports its self-simmed HP, so the
server can never send a matching value. This was proven exhaustively: there is no
server-side packet that both keeps the client alive and avoids the compare.

THE PATCH
---------
At VA ``0x5DD9DC`` (file offset ``0x1DCDDC`` in ``.text``; ImageBase 0x400000) the
3-byte compare ``CMP BL,[EAX+0x20]`` (``3A 58 20``) is replaced with
``JMP 0x5DD95F`` + ``NOP`` (``EB 81 90``). That routes the compare into the
function's own "return success" path (the one peaceful zones already use), so the
avatar synch compare ALWAYS succeeds. It does NOT touch the ``+0x95`` bit, so
skills, PvP, and peaceful-zone behaviour are unaffected (unlike the older
``+0x95 |= 1`` patch, which stalled movement and disabled skills).

Live-verified clean 2026-06-09: with the patch applied and the NORMAL server
(no ``DR_NO_HP_HEARTBEAT``), taking dungeon hits standing and moving no longer
crashes; movement, mob speed, and skills are normal.

USAGE
-----
    python scripts/patch_client_synch_crash.py [--exe PATH] [--revert] [--check]

The client must be CLOSED (Windows locks the EXE while it runs). A pristine
backup is written next to the EXE as ``<name>.orig-presynchpatch`` and is never
overwritten, so ``--revert`` always restores the true original.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

DEFAULT_EXE = r"C:\Games\Dungeon Runners\Client 666\DungeonRunners.exe"
# WSL view of the same file (the repo is developed under WSL).
DEFAULT_EXE_WSL = "/mnt/c/Games/Dungeon Runners/Client 666/DungeonRunners.exe"

FILE_OFFSET = 0x1DCDDC          # VA 0x5DD9DC - .text(VA 0x1000, raw 0x400)
ORIG = bytes.fromhex("3a5820")  # CMP BL, [EAX+0x20]
PATCH = bytes.fromhex("eb8190")  # JMP 0x5DD95F ; NOP
BACKUP_SUFFIX = ".orig-presynchpatch"


def _resolve_exe(exe: str) -> str:
    if os.path.exists(exe):
        return exe
    # Allow passing the Windows path while running under WSL.
    if exe == DEFAULT_EXE and os.path.exists(DEFAULT_EXE_WSL):
        return DEFAULT_EXE_WSL
    return exe


def _read3(path: str, off: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(off)
        return f.read(3)


def check(exe: str) -> int:
    cur = _read3(exe, FILE_OFFSET)
    if cur == PATCH:
        print(f"[check] PATCHED   (offset 0x{FILE_OFFSET:X} = {cur.hex()})")
    elif cur == ORIG:
        print(f"[check] ORIGINAL  (offset 0x{FILE_OFFSET:X} = {cur.hex()})")
    else:
        print(f"[check] UNKNOWN bytes at 0x{FILE_OFFSET:X}: {cur.hex()} — wrong EXE/build?")
        return 2
    return 0


def apply_patch(exe: str) -> int:
    backup = exe + BACKUP_SUFFIX
    cur = _read3(exe, FILE_OFFSET)
    if cur == PATCH:
        print("[patch] already patched — nothing to do")
        return 0
    if cur != ORIG:
        print(f"[patch] ABORT: bytes at 0x{FILE_OFFSET:X} are {cur.hex()}, "
              f"expected {ORIG.hex()}. Wrong EXE or already-modified build.")
        return 2
    if not os.path.exists(backup):
        shutil.copy2(exe, backup)
        print(f"[patch] backup created: {backup}")
    else:
        print(f"[patch] backup already present (kept): {backup}")
    try:
        with open(exe, "r+b") as f:
            f.seek(FILE_OFFSET)
            f.write(PATCH)
            f.flush()
            os.fsync(f.fileno())
    except (PermissionError, OSError) as ex:
        print(f"[patch] FAILED — is the client still running? Close it first. ({ex})")
        return 1
    chk = _read3(exe, FILE_OFFSET)
    ok = chk == PATCH
    print(f"[patch] {ORIG.hex()} -> {PATCH.hex()}  verify={chk.hex()}  {'OK' if ok else 'VERIFY FAILED'}")
    return 0 if ok else 1


def revert(exe: str) -> int:
    backup = exe + BACKUP_SUFFIX
    try:
        if os.path.exists(backup):
            shutil.copy2(backup, exe)
            print(f"[revert] restored from {backup}")
        else:
            with open(exe, "r+b") as f:
                f.seek(FILE_OFFSET)
                f.write(ORIG)
                f.flush()
                os.fsync(f.fileno())
            print(f"[revert] no backup found — wrote original bytes {ORIG.hex()} at 0x{FILE_OFFSET:X}")
    except (PermissionError, OSError) as ex:
        print(f"[revert] FAILED — is the client still running? Close it first. ({ex})")
        return 1
    return check(exe)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Patch/revert the DR client avatar HP-synch crash.")
    ap.add_argument("--exe", default=DEFAULT_EXE, help="path to DungeonRunners.exe")
    ap.add_argument("--revert", action="store_true", help="restore the original (unpatched) bytes")
    ap.add_argument("--check", action="store_true", help="report current patch state and exit")
    args = ap.parse_args(argv)

    exe = _resolve_exe(args.exe)
    if not os.path.exists(exe):
        print(f"[error] EXE not found: {exe}")
        return 2
    if args.check:
        return check(exe)
    if args.revert:
        return revert(exe)
    return apply_patch(exe)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
