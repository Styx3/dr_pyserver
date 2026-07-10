# client_hook — native-behaviour client shim for Dungeon Runners

A small **hook DLL** that lets the *unmodified* client EXE run against our server with
native multiplayer behaviour. The hook (`hook.c`) inline-detours `FUN_005dd900` (the
zero-tolerance HP-synch compare) so the client trusts its own value for entities **it**
simulates, plus the combat-report / mob-attack-injection / mob-clamp hooks
(`combat_hook.c`, `inject_hook.c`, `telemetry_client.c`).

**Delivery: `d3d9.dll` drop-in proxy — pure drag-and-drop, NO rename.** Drop `d3d9.dll`
into the game folder; the client auto-loads it at startup, our `DllMain` installs the
hooks, and our exported `Direct3DCreate9` forwards to the real System32 `d3d9.dll`. The
EXE on disk is never modified — revert by deleting the DLL.

> Historical note: earlier builds also shipped a `dbghelp.dll` proxy and a
> `drloader.exe` + `drhook.dll` injection path. Both were removed 2026-07-03 — `d3d9.dll`
> is self-sufficient (it also injects the `ran_from_launcher` token, below), so the other
> two delivery variants were redundant maintenance surface.

> **Same technique as [RainbowRunnerSpy2](https://github.com/EllieBelly4/RainbowRunnerSpy2)**
> (EllieBelly4's in-game inspector), a drag-and-drop `d3d9.dll` proxy. If you ever run an
> inspector that *also* proxies `d3d9.dll`, only one can win the loader search — load the
> hook a different way then (a fresh proxy of another statically-imported DLL).

## Why this exists (the two client requirements we satisfy)

1. **`ran_from_launcher`** — the client's arg parser (`FUN_006ffa60`) requires this
   command-line token (the native `DungeonNCLauncher.exe` appends it; template @ `0x8CA5D0`).
   `dr_install_launcher_token` (hook.c) hooks `GetCommandLineA` to inject it, so a bare
   `DungeonRunners.exe` launch takes the normal token path. Not a patch — exactly the native
   launcher's behaviour. (`play_dr.bat` also passes it explicitly, as a convenience.)

2. **The avatar HP-synch crash** — combat is client-authoritative (see `../bible.md` §4/§6).
   The engine compares any server-sent HP against the client's locally-simulated value with
   **zero tolerance** (`FUN_005dd900` @ `0x5DD900`); a mismatch crashes the Avatar process
   (`0xc000013a`). The server can neither know (client is silent, §6-LIVE.12) nor reproduce
   (§6-LIVE.6) a simulated entity's HP, so the native-correct fix is client-side: **this
   client trusts its own value for what it simulates.**

## The rule (and why it's multiplayer-safe)

The detour reads the synced unit's control mode at `+0xE5` (bible §10; `1` = display,
`4` = client-simulated — confirmed via the enroll setter `FUN_005202f0`) and:

- `+0xE5 != 1` → **bypass** the compare, return success.
  Covers the player's own avatar **and** mobs it enrolled. Fail-safe toward bypass, so the
  avatar survives the realloc / action-transition windows where the mode byte reads `0`.
- `+0xE5 == 1` → **run the original compare unchanged.**
  Display units (other players' avatars, server-authoritative mobs) keep genuine desync
  detection — and they pass by construction anyway (the client doesn't independently compute
  their HP; bible §6-LIVE.7).

This is per-client and per-entity. From each client's view it only ever ignores server HP for
the units **that client** owns; everyone else's avatars and the shared mobs are still validated
against the server-relayed value. So in a group instance (P1+P2, shared mobs) it composes:
each client trusts itself, displays + validates the others. **No global state, no hardcoded
eid** (our server gives the first connection eid `0x1FE`, later ones different — hardcoding it
would break multiplayer; the control-mode read does not).

It is the generalisation of the engine's own town bypass (`+0x95` bit0 = peaceful-zone flag,
bible §6-LIVE.11) into the correct client-server rule: *skip HP validation for units I am
authoritative over.*

## What else `dr_install_all` installs

Beyond the HP-synch bypass, `DllMain` → `dr_install_all()` (hook.c) also installs:

- **combat-report** (`combat_hook.c`, detours `Damage::apply` `FUN_004f6580`) — reports the
  client's ground-truth kills / XP / level over the telemetry socket, since the native protocol
  is packet-blind for them (bible §6-LIVE.4).
- **mob-attack injection** (`inject_hook.c`, per-frame pump `FUN_005d9e30`) — applies
  server-commanded mob→player damage locally so display-only mobs can hurt the player without
  the server asserting avatar HP (`docs/MOB_ATTACK_INJECTION.md`).
- **mob-clamp** (`inject_hook.c`) — pins aggroed mobs to the melee stop-ring (rewriting
  `unit+0x90/+0x94`) so the client-side Follow action can't drive them through the player, the
  run-through fix (bible §14.6). Self-validating + server-gated (`DR_MOB_CLAMP`).

## Build (32-bit Windows PE — the client is x86)

Install a 32-bit mingw cross-compiler, then `make`:

```bash
# WSL / Debian / Ubuntu:
sudo apt-get install -y gcc-mingw-w64-i686 binutils-mingw-w64-i686
make                       # produces d3d9.dll
```

Or inside an MSYS2 **MINGW32** shell: `pacman -S mingw-w64-i686-gcc && make`.

On this WSL + msys64 setup the cross-compiler is invoked through cmd.exe (a WSL `/mnt` path
breaks cc1 lookup) — the exact one-liner is at the top of the `Makefile`.

## Install & run

`DungeonRunners.exe` statically imports `Direct3DCreate9` from `d3d9.dll`, which it normally
loads from `System32` (it is **not** shipped in the game folder, and not a KnownDLL). Dropping
our `d3d9.dll` in the game folder makes the app-folder copy win the loader search. At process
init our `DllMain` installs the hooks (it does **not** touch `d3d9`, so no LoadLibrary under
loader-lock); our exported `Direct3DCreate9` lazily loads the real `System32\d3d9.dll` on first
call and forwards to it unchanged.

In the game folder (e.g. `…\Dungeon Runners\Client 666\`):

1. copy our built `d3d9.dll` into the folder   *(that's it — no rename)*
2. (optional) copy `play_dr.bat` into the folder
3. launch by double-clicking `DungeonRunners.exe`, or run **`play_dr.bat`** (which cd's to the
   folder and passes `ran_from_launcher` explicitly).

No loader, no `CreateRemoteThread`. The hooks are installed before any combat. **Revert** by
deleting `d3d9.dll`.

Confirm the hooks installed with **DebugView** (Sysinternals) — look for:

```
[drhook] FUN_005dd900 hooked: HP-compare bypass for client-simulated units installed
```

## Test (against the UNPATCHED EXE)

1. `python scripts/patch_client_synch_crash.py --revert` (ensure the static patch is OFF — this
   shim replaces it; the static patch's blanket bypass and this surgical one must not stack).
2. drop in `d3d9.dll`, launch via `play_dr.bat`.
3. Enter a dungeon, let a mob hit you, move + fight.
   - **Pass:** no "Entity synch error" / `0xc000013a`; movement, mob speed, skills normal.
   - DebugView shows the install line once at startup.

## Relationship to `scripts/patch_client_synch_crash.py`

That script is the static-EXE equivalent (blanket bypass of the same compare, live-verified
clean 2026-06-09). This shim supersedes it: pristine EXE, surgical per-entity rule, and it also
handles `ran_from_launcher`. Use one or the other, not both.

## How it works (implementation)

`FUN_005dd900` is `__stdcall` with 3 stack args **+ EDI = the synced unit** (register input),
returns via `RET 0xC`. Its first instruction `MOV EAX,FS:[0]` (`64 A1 00 00 00 00`, 6 bytes) is a
clean boundary with no relative operand, so the detour:

- copies that instruction into a `VirtualAlloc` trampoline + `JMP 0x5DD906` (call-through to the original),
- overwrites the start with `JMP detour` (5 bytes) + `NOP`,
- in the detour: validates EDI via the engine's own `FUN_005cb650`, reads `+0xE5`, then either
  `MOV AL,1 / RET 0xC` (bypass) or `JMP trampoline` (run original).

Addresses are RVA + live module base, so a relocated image still works. The DLL refuses to patch
if the prologue bytes don't match (wrong build / already hooked).
