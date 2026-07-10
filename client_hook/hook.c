/*
 * hook.c — the FUN_005dd900 HP-synch-compare bypass, installed from the
 * d3d9.dll drop-in proxy's DllMain (dr_install_all) when DungeonRunners.exe
 * starts. (Earlier drloader-injected drhook.dll and dbghelp.dll proxy delivery
 * variants were removed 2026-07-03 — the d3d9 proxy is self-sufficient.)
 *
 * WHY
 * ---
 * Combat is CLIENT-AUTHORITATIVE (see ../bible.md §4/§6). The engine's
 * ClientEntityManager compares any server-sent HP against the client's locally
 * simulated value with ZERO tolerance in FUN_005dd900 (VA 0x5DD900). On a
 * mismatch it raises "Entity synch error detected" and the Avatar process exits
 * 0xc000013a.
 *
 * The compare only ever *diverges* for entities THIS client SIMULATES (its own
 * avatar + mobs it enrolled via 0x64). For entities it merely DISPLAYS (other
 * players' avatars, server-authoritative mobs) the client never independently
 * computes HP, so the server value matches by construction and the compare
 * passes (bible §6-LIVE.7). The server can neither know (client is silent,
 * §6-LIVE.12 — re-confirmed live 2026-06-16 against both emit sites) nor
 * reproduce (§6-LIVE.6) a simulated entity's HP, so the only correct,
 * multiplayer-safe behaviour — and the native one — is: this client trusts its
 * own value for what it simulates.
 *
 * THE HOOK
 * --------
 * Inline-detour FUN_005dd900. In the detour, read the synced unit (EDI) and:
 *   - unit->control_mode (+0xE5) != 1  -> BYPASS the compare, return success.
 *     (covers the avatar AND enrolled mobs; fail-safe toward bypass so the avatar
 *      survives realloc / action-transition windows where the mode read is 0 —
 *      live-verified 2026-06-16: a real mob->avatar mismatch had +0xE5 == 0.)
 *   - unit->control_mode (+0xE5) == 1  -> run the ORIGINAL compare unchanged.
 *     (display units: genuine desync detection preserved; they pass anyway)
 *
 * This generalises the engine's own town bypass (+0x95 bit0, the "peaceful zone"
 * flag, NOT input authority — bible §6-LIVE.11) into the correct client-server
 * rule: "skip HP validation for units I am authoritative over."
 *
 * The detour is reached via a 5-byte JMP that overwrites the function's first
 * instruction (MOV EAX,FS:[0], 6 bytes — a clean boundary with no relative
 * operand, so it relocates verbatim into the trampoline). FUN_005dd900 is
 * __stdcall with 3 stack args + EDI register input and returns via RET 0xC, so
 * the bypass path simply does MOV AL,1 / RET 0xC.
 *
 * Addresses are RVA + the live module base, so a relocated (ASLR) image still
 * works even though this build has none.
 */
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include "hook.h"

/* RVAs (VA - 0x00400000, the Ghidra image base). */
#define RVA_SYNCH       0x001DD900u   /* FUN_005dd900 — the zero-tolerance HP compare */
#define RVA_VALIDATE    0x001CB650u   /* FUN_005cb650 — engine "is unit valid?" (thiscall, ECX=this) */

/* The first instruction of FUN_005dd900: MOV EAX, FS:[0]  ==  64 A1 00 00 00 00 */
#define PROLOGUE_LEN     6
static const unsigned char EXPECTED_PROLOGUE[PROLOGUE_LEN] =
    { 0x64, 0xA1, 0x00, 0x00, 0x00, 0x00 };

/* Shared with the asm detour below. */
extern void detour_synch(void);       /* asm entry, decorated _detour_synch */
uintptr_t g_trampoline = 0;           /* asm reads _g_trampoline : runs the original */
uintptr_t g_validate   = 0;           /* asm reads _g_validate   : FUN_005cb650 address */

/*
 * The detour. Entered via JMP from FUN_005dd900's start, so on entry:
 *   EDI = synced unit ptr,  EBX = entity-manager context,
 *   [esp] = return addr into FUN_005db520,  [esp+4..+0xC] = the 3 stdcall args.
 * EBX/ESI/EDI/EBP are callee-saved, so the thiscall validity check preserves them.
 */
__asm__(
    ".intel_syntax noprefix\n"
    ".text\n"
    ".globl _detour_synch\n"
"_detour_synch:\n"
    "    test edi, edi\n"
    "    je   2f\n"                              /* null unit -> let original handle (null-safe) */
    "    mov  ecx, edi\n"
    "    mov  eax, dword ptr [_g_validate]\n"
    "    call eax\n"                             /* FUN_005cb650(edi); edi preserved (callee-saved) */
    "    test al, al\n"
    "    je   2f\n"                              /* invalid unit -> let original handle */
    "    pushad\n"                               /* record eid->Unit* for mob-attack injection */
    "    push edi\n"
    "    call _dr_inject_note_unit\n"            /* (inject_hook.c) cache this synched unit */
    "    add  esp, 4\n"
    "    popad\n"
    "    movzx eax, byte ptr [edi + 0xE5]\n"     /* control mode */
    "    cmp  eax, 1\n"
    "    je   2f\n"                              /* display unit -> run the real compare */
    "    mov  al, 1\n"                           /* simulated/unknown -> BYPASS: return success */
    "    ret  0xC\n"
"2:\n"
    "    jmp  dword ptr [_g_trampoline]\n"       /* run the original FUN_005dd900 */
    ".att_syntax prefix\n"
);

int dr_install_synch_hook(void)
{
    uintptr_t base   = (uintptr_t)GetModuleHandleA(NULL);
    uintptr_t target = base + RVA_SYNCH;
    g_validate       = base + RVA_VALIDATE;

    if (memcmp((void *)target, EXPECTED_PROLOGUE, PROLOGUE_LEN) != 0) {
        OutputDebugStringA("[drhook] prologue mismatch at FUN_005dd900 — wrong build or already hooked; aborting");
        return 0;
    }

    /* Trampoline: copied prologue + JMP back to (target + PROLOGUE_LEN). */
    unsigned char *tramp = (unsigned char *)VirtualAlloc(
        NULL, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tramp) {
        OutputDebugStringA("[drhook] VirtualAlloc(trampoline) failed");
        return 0;
    }
    memcpy(tramp, (void *)target, PROLOGUE_LEN);
    tramp[PROLOGUE_LEN] = 0xE9;                  /* JMP rel32 */
    int32_t back = (int32_t)((target + PROLOGUE_LEN) - ((uintptr_t)tramp + PROLOGUE_LEN + 5));
    memcpy(tramp + PROLOGUE_LEN + 1, &back, 4);
    g_trampoline = (uintptr_t)tramp;

    /* Patch the function start: JMP detour (5 bytes) + NOP pad to PROLOGUE_LEN. */
    DWORD oldProt;
    if (!VirtualProtect((void *)target, PROLOGUE_LEN, PAGE_EXECUTE_READWRITE, &oldProt)) {
        OutputDebugStringA("[drhook] VirtualProtect(target) failed");
        return 0;
    }
    unsigned char patch[PROLOGUE_LEN];
    patch[0] = 0xE9;
    int32_t rel = (int32_t)((uintptr_t)&detour_synch - (target + 5));
    memcpy(patch + 1, &rel, 4);
    for (int i = 5; i < PROLOGUE_LEN; ++i) patch[i] = 0x90;   /* NOP */
    memcpy((void *)target, patch, PROLOGUE_LEN);
    VirtualProtect((void *)target, PROLOGUE_LEN, oldProt, &oldProt);
    FlushInstructionCache(GetCurrentProcess(), (void *)target, PROLOGUE_LEN);

    OutputDebugStringA("[drhook] FUN_005dd900 hooked: HP-compare bypass for client-simulated units installed");
    return 1;
}

/* RVA of FUN_006ff8f0 — the "relaunch through DungeonNCLauncher.exe" routine the
 * client runs when launched WITHOUT the ran_from_launcher token. It's a clean
 * void(void), so a single RET at its entry skips the relaunch and lets a direct
 * DungeonRunners.exe launch continue to login (the in-memory equivalent of the
 * old binary patch). Must run from DllMain — before the EXE's arg-parser
 * (FUN_006ffa60) calls it. */
#define RVA_RELAUNCH    0x002FF8F0u

int dr_install_launcher_bypass(void)
{
    uintptr_t base = (uintptr_t)GetModuleHandleA(NULL);
    unsigned char *p = (unsigned char *)(base + RVA_RELAUNCH);

    /* Prologue: PUSH -1 (6A FF) ; PUSH imm32 (68 ..). */
    if (!(p[0] == 0x6A && p[1] == 0xFF && p[2] == 0x68)) {
        OutputDebugStringA("[drhook] FUN_006ff8f0 prologue mismatch — launcher bypass aborted");
        return 0;
    }
    DWORD oldProt;
    if (!VirtualProtect(p, 1, PAGE_EXECUTE_READWRITE, &oldProt)) {
        OutputDebugStringA("[drhook] VirtualProtect(FUN_006ff8f0) failed");
        return 0;
    }
    p[0] = 0xC3;                              /* RET — neutralize the relaunch */
    VirtualProtect(p, 1, oldProt, &oldProt);
    FlushInstructionCache(GetCurrentProcess(), p, 1);

    OutputDebugStringA("[drhook] launcher relaunch (FUN_006ff8f0) neutralized — direct launch OK");
    return 1;
}

/* Make a bare DungeonRunners.exe launch behave exactly like play_dr.bat — i.e.
 * as if the launcher started it — by injecting the `ran_from_launcher` token
 * into the command line the client reads. We inline-hook GetCommandLineA to
 * return a token-appended copy; the client's arg parser (FUN_006ffa60) then
 * takes the normal token path (no relaunch, full setup). This is the correct
 * bare-launch fix: RET-patching the relaunch alone left the client on the
 * no-token path that skips setup and still exits. Must run from DllMain, before
 * FUN_006fa720 calls GetCommandLineA. */
static char g_cmdline[2048];

int dr_install_launcher_token(void)
{
    const char *orig = GetCommandLineA();
    if (!orig) return 0;
    if (strstr(orig, "ran_from_launcher")) return 1;     /* already tokenized (play_dr.bat) */

    static const char TOKEN[] = " ran_from_launcher";
    if (strlen(orig) + sizeof(TOKEN) >= sizeof(g_cmdline)) return 0;
    strcpy(g_cmdline, orig);
    strcat(g_cmdline, TOKEN);

    FARPROC target = GetProcAddress(GetModuleHandleA("kernel32.dll"), "GetCommandLineA");
    if (!target) {
        OutputDebugStringA("[drhook] GetProcAddress(GetCommandLineA) failed — token not injected");
        return 0;
    }
    unsigned char *p = (unsigned char *)target;
    DWORD oldProt;
    if (!VirtualProtect(p, 6, PAGE_EXECUTE_READWRITE, &oldProt)) return 0;
    /* MOV EAX, &g_cmdline ; RET  ==  B8 <ptr32> C3 */
    p[0] = 0xB8;
    uintptr_t ptr = (uintptr_t)g_cmdline;
    memcpy(p + 1, &ptr, 4);
    p[5] = 0xC3;
    VirtualProtect(p, 6, oldProt, &oldProt);
    FlushInstructionCache(GetCurrentProcess(), p, 6);

    OutputDebugStringA("[drhook] GetCommandLineA hooked — ran_from_launcher injected (bare launch OK)");
    return 1;
}

/* One call for every DllMain: inject the launcher token first (before the EXE
 * runs its arg parser), then the HP-synch bypass and the combat-report hook.
 * Each is independently guarded and fail-safe, so a stale build degrades
 * gracefully rather than crashing. */
void dr_install_all(void)
{
    dr_install_launcher_token();
    dr_install_synch_hook();
    dr_install_combat_report_hook();
    dr_install_inject_hook();
    dr_telemetry_start();       /* connect the telemetry channel NOW, before any fight */
}
