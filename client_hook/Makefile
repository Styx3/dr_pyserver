# Build the DR client hook — d3d9.dll drop-in proxy (32-bit Windows PE).
#
# Toolchain: i686-w64-mingw32 cross-compiler.
#   WSL/Debian/Ubuntu:  sudo apt-get install -y gcc-mingw-w64-i686 binutils-mingw-w64-i686
#   MSYS2 (MINGW32):     pacman -S mingw-w64-i686-gcc   (then build inside the MINGW32 shell as `make`)
#
# The client is 32-bit, so the artifact MUST be 32-bit.
#
# NOTE (WSL): invoking the MSYS2 gcc via a WSL /mnt path breaks cc1 lookup; build
# through cmd.exe with PATH=C:\msys64\mingw32\bin instead (see README.md).
#
# EXACT REBUILD COMMAND (verified on this WSL + msys64 setup — run from WSL):
#   cmd.exe /c "cd /d C:\path\to\dr_pyserver\client_hook && set PATH=C:\msys64\mingw32\bin;C:\msys64\usr\bin&& make clean && make"
#   (mingw32\bin supplies gcc/cc1; usr\bin supplies make. No space before && after the PATH set.)
#
#   make            # build d3d9.dll
#   make clean
#
# Delivery: d3d9.dll is a pure drag-and-drop proxy (NO rename). It wraps
# Direct3DCreate9 (forwarding to System32 d3d9 at runtime) and installs the hook
# from DllMain. Drop it in the game folder next to DungeonRunners.exe. See README.md.

CC      ?= i686-w64-mingw32-gcc
CFLAGS  := -O2 -Wall -Wextra
# NOTE: do NOT pass -masm=intel — hook.c/combat_hook.c toggle intel syntax locally
# inside their file-scope asm blocks and reset to att; a global -masm=intel breaks it.

# Shared hook core: HP-synch bypass + launcher token/bypass (hook.c), combat-report
# detour (combat_hook.c), telemetry sender/reader (telemetry_client.c, needs
# ws2_32), server-driven mob-attack injection + mob-clamp (inject_hook.c).
HOOK_SRC := hook.c combat_hook.c telemetry_client.c inject_hook.c
HOOK_LIB := -static-libgcc -lkernel32 -lws2_32

all: d3d9.dll

d3d9.dll: d3d9_proxy.c $(HOOK_SRC) hook.h d3d9.def
	$(CC) $(CFLAGS) -shared -o $@ d3d9_proxy.c $(HOOK_SRC) d3d9.def -Wl,--enable-stdcall-fixup $(HOOK_LIB)
	@echo "built $@ (proxy -> System32 d3d9.dll; no rename needed)"

clean:
	rm -f d3d9.dll

.PHONY: all clean
