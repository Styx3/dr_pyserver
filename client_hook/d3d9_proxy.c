/*
 * d3d9.dll — proxy-delivery variant of the DR HP-synch hook (pure drag-and-drop).
 *
 * GOAL: apply the hook by dropping ONE file into the game folder, with no rename
 * and no loader. DungeonRunners.exe statically imports Direct3DCreate9 from
 * d3d9.dll, which it normally loads from System32 (d3d9.dll is NOT shipped in the
 * game folder and is NOT a KnownDLL). Placing our d3d9.dll in the game folder
 * makes the app-folder copy win the loader search, so:
 *
 *   - DllMain (process init, before any combat) installs the FUN_005dd900 bypass
 *     via dr_install_synch_hook() (shared hook.c). It does NOT touch d3d9, so no
 *     LoadLibrary-under-loader-lock.
 *   - Our exported Direct3DCreate9 lazily loads the REAL System32 d3d9.dll on its
 *     first call (graphics init, after the loader lock is released) and forwards
 *     to it unchanged, so rendering is untouched.
 *
 * This is the same technique as RainbowRunnerSpy2 (which proxies d3d9.dll for an
 * imgui overlay); we only pass Direct3DCreate9 through and add the hook.
 *
 * INSTALL (in the game folder): drop this d3d9.dll in — that's it. No rename.
 * Launch via play_dr.bat (passes ran_from_launcher) or DungeonRunners.exe directly.
 * Revert by deleting the file.
 */
#include <windows.h>
#include "hook.h"

typedef void * (WINAPI *PFN_Direct3DCreate9)(UINT);

static HMODULE              g_real_d3d9   = NULL;
static PFN_Direct3DCreate9  g_real_create = NULL;

/* Load the genuine System32 d3d9.dll by absolute path (so we never re-load
 * ourselves) and resolve its Direct3DCreate9. Called lazily from the export. */
static void load_real_d3d9(void)
{
    char path[MAX_PATH];
    UINT n = GetSystemDirectoryA(path, MAX_PATH);   /* e.g. C:\Windows\System32 */
    if (n == 0 || n > MAX_PATH - 12) {
        OutputDebugStringA("[drhook/d3d9] GetSystemDirectoryA failed");
        return;
    }
    lstrcatA(path, "\\d3d9.dll");
    g_real_d3d9 = LoadLibraryA(path);
    if (!g_real_d3d9) {
        OutputDebugStringA("[drhook/d3d9] LoadLibrary(System32\\d3d9.dll) failed");
        return;
    }
    g_real_create = (PFN_Direct3DCreate9)GetProcAddress(g_real_d3d9, "Direct3DCreate9");
    if (!g_real_create)
        OutputDebugStringA("[drhook/d3d9] GetProcAddress(Direct3DCreate9) failed");
}

/* The one export DungeonRunners.exe imports from d3d9.dll. Undecorated name is
 * forced via d3d9.def + --enable-stdcall-fixup. */
void * WINAPI Direct3DCreate9(UINT SDKVersion)
{
    if (!g_real_create)
        load_real_d3d9();
    return g_real_create ? g_real_create(SDKVersion) : NULL;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        dr_install_all();          /* launcher bypass + HP-synch + combat-report; no LoadLibrary under loader lock */
    }
    return TRUE;
}
