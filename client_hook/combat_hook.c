/*
 * combat_hook.c — detours the client's damage dispatcher to report kills.
 *
 * FUN_004f6580 (VA 0x4F6580) is the damage-application dispatch. On entry EDI =
 * the Damage object, with (bible RE 2026-06-16):
 *   [EDI+0x2c] = attacker entity    [EDI+0x34] = target entity
 *   [EDI+0x38] = damage (wire / x256)
 * Entity fields: [+0x80] = entity id (u16 on the wire), [+0x2f0] = current HP
 * (wire / x256).
 *
 * KILL DETECTION IS POST-APPLY (v2, 2026-07-03). The v1 detour predicted the
 * kill BEFORE running the original, as `hp <= [EDI+0x38]` — but for a native
 * (mode != 4) hit the engine RECOMPUTES the damage inside Damage::apply
 * (FUN_004f67f0 resist/crit path), so the entry value of +0x38 is not what
 * actually lands. A lethal blow whose entry damage under-read the recompute was
 * missed FOREVER: the client killed the mob locally, but no KILL telemetry was
 * sent, and — since the client reports no monster HP over any channel (live
 * capture, tests/test_kill_swing_suffix.py) — the server had no other way to
 * learn of the death. Its ghost mob kept swinging (MOB_ATTACK injections =
 * "damage after all mobs are dead", live 2026-07-03) and its loot/XP never
 * fired. v2 wraps the original call and reads the victim's HP AFTER it returns:
 *   kill  :=  hp_before > 0  &&  hp_after <= 0        (exact, no heuristic)
 *
 * Wrapping is safe because FUN_004f6580 takes no stack args and returns with a
 * plain RET — live-proven by inject_hook.c's dr_call_damage_apply, which calls
 * it exactly that way. Damage::apply can nest (reflected/triggered damage), so
 * the pre/post handoff rides a small depth stack, not a single static.
 *
 * On a detected kill we report KILL{victim, killer, pos, level, xp} to the
 * server via the telemetry channel (telemetry_client.c). The server filters: it
 * only acts when the victim is a tracked monster and the killer resolves to a
 * player, so we report every death and let the server decide.
 *
 * Hook shape: same JMP-trampoline as the synch hook. FUN_004f6580's first
 * instruction is MOV EAX,FS:[0] (64 A1 00 00 00 00, 6 bytes — a clean boundary
 * with no relative operand), so we relocate those 6 bytes into the trampoline.
 */
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include "hook.h"

#define RVA_DAMAGE   0x000F6580u   /* FUN_004f6580 — damage dispatch (VA 0x4F6580) */
#define PROLO_LEN    6             /* MOV EAX, FS:[0]  ==  64 A1 00 00 00 00 */

static const unsigned char EXPECTED_PROLO[PROLO_LEN] =
    { 0x64, 0xA1, 0x00, 0x00, 0x00, 0x00 };

extern void detour_damage(void);          /* asm entry, decorated _detour_damage */
uintptr_t g_trampoline_damage = 0;        /* asm reads _g_trampoline_damage */

/* Engine "is unit valid?" (FUN_005cb650, thiscall ECX=this) — resolved by
 * dr_install_synch_hook (hook.c), which dr_install_all runs before this hook.
 * Best-effort guard for the post-apply reads: apply itself never frees a unit
 * (corpses persist for the death anim), but the engine's own check is cheap. */
extern uintptr_t g_validate;
typedef unsigned char (__fastcall *dr_validate_fn)(void *self, void *edx_unused);

static int unit_valid(void *unit)
{
    if (!unit) return 0;
    if (!g_validate) return 1;            /* synch hook absent — direct read */
    return ((dr_validate_fn)g_validate)(unit, 0) != 0;
}

/* Pre/post handoff: one record per in-flight Damage::apply (nesting-safe).
 * GAME THREAD ONLY — apply only ever runs there, so no locking. */
#define DMG_STACK_MAX 8
typedef struct {
    unsigned char *attacker;
    unsigned char *target;
    int            hp_before;             /* target HP (wire) at entry */
} dmg_rec_t;
static dmg_rec_t g_dmg_stack[DMG_STACK_MAX];
static int       g_dmg_depth = 0;

/* Called from the asm detour with the Damage object (EDI), BEFORE the original
 * runs. Feeds the eid->Unit* cache and records the pre-hit HP for the post pass. */
void __cdecl dr_on_damage_pre(unsigned char *dmg)
{
    dmg_rec_t rec;
    rec.attacker = 0; rec.target = 0; rec.hp_before = 0;

    if (dmg) {
        rec.attacker = (unsigned char *)*(uintptr_t *)(dmg + 0x2c);
        rec.target   = (unsigned char *)*(uintptr_t *)(dmg + 0x34);
        if (rec.attacker && rec.target) {
            /* Cache both units for the mob-attack injection (inject_hook.c). A
             * player->mob hit has attacker == the avatar, so this caches the
             * avatar WITHOUT depending on movement (the synch detour only sees
             * it on move-acks, which stop when the player stands still) — and
             * re-populates it after a zone-reset. The mob side is refreshed by
             * both the player's swings and the mob's own attacks. */
            dr_inject_note_unit((void *)rec.attacker);
            dr_inject_note_unit((void *)rec.target);
            rec.hp_before = *(int *)(rec.target + 0x2f0);
        }
    }
    if (g_dmg_depth < DMG_STACK_MAX)
        g_dmg_stack[g_dmg_depth] = rec;
    ++g_dmg_depth;
}

/* Called from the asm detour AFTER the original Damage::apply returned. Reads
 * the victim's post-apply HP for the exact kill decision. */
void __cdecl dr_on_damage_post(void)
{
    if (g_dmg_depth <= 0) return;                 /* unbalanced — be safe */
    --g_dmg_depth;
    if (g_dmg_depth >= DMG_STACK_MAX) return;     /* overflow slot: no record */
    dmg_rec_t rec = g_dmg_stack[g_dmg_depth];

    if (!rec.attacker || !rec.target) return;
    if (rec.hp_before <= 0) return;               /* corpse hit — died earlier */
    if (!unit_valid(rec.target) || !unit_valid(rec.attacker)) return;

    int hp_after = *(int *)(rec.target + 0x2f0);
    if (hp_after > 0) return;                     /* survived the hit */

    unsigned victim = *(unsigned short *)(rec.target   + 0x80);
    unsigned killer = *(unsigned short *)(rec.attacker + 0x80);

    /* The client just killed this unit locally. If it's a mob the injection
     * layer is driving, forget it NOW (drop its pending attacks / clamp / cache
     * + block its eid) so no server-streamed MOB_ATTACK lands "after the mob is
     * dead" (live 2026-07-03). No-op when the victim isn't a tracked mob. */
    dr_inject_forget_mob(victim);

    /* Victim death position. +0x130/+0x134/+0x138 is the AVATAR's world-coord
     * cache (Fixed32 ×256); MOBS read 0 here (live 2026-06-17 — a mob's world
     * position is stored elsewhere). ★ Ghidra 2026-07-02: a UNIT's world
     * horizontal position is +0x90 (X) / +0x94 (Y) — see inject_hook.c clamp.
     * So px/py/pz are 0 for mob kills, and the server rejects an origin
     * death_pos, falling back to its tracked monster / killer position
     * (combat._generate_loot). Kept for the KILL_AT wire shape; the
     * load-bearing field is the killer LEVEL below. Revisit to land loot
     * exactly on moving mobs. */
    int px = *(int *)(rec.target + 0x130);
    int py = *(int *)(rec.target + 0x134);
    int pz = *(int *)(rec.target + 0x138);

    /* Killer's current level and Experience — PlayerState is embedded in the
     * avatar Unit. AddExperience (FUN_004f82f0) reads/increments level as a u16
     * at param_1[0xc5] (+0x314) and accumulates XP into param_1[200] (+0x320),
     * deducting the level threshold on level-up — i.e. +0x320 is "progress into
     * the current level", matching the server's saved.experience. The client
     * self-levels locally and authoritatively; the server snaps the character to
     * this level (never lags) and adopts the exact experience so the
     * zone-transfer Avatar re-send never clobbers the client's locally-earned
     * XP. NB both fields are only valid when `attacker` is the AVATAR; for an
     * owned-unit (summon/gnome) finishing blow they are garbage, which the
     * server ignores (it adopts level/exp only for an avatar killer). */
    unsigned short level     = *(unsigned short *)(rec.attacker + 0x314);
    unsigned int  experience = *(unsigned int   *)(rec.attacker + 0x320);

    dr_telemetry_report_kill_at_xp(victim, killer, px, py, pz, level, experience);
}

__asm__(
    ".intel_syntax noprefix\n"
    ".text\n"
    ".globl _detour_damage\n"
"_detour_damage:\n"
    "    pushad\n"
    "    push edi\n"                          /* arg = Damage* (EDI) */
    "    call _dr_on_damage_pre\n"
    "    add  esp, 4\n"
    "    popad\n"
    "    call dword ptr [_g_trampoline_damage]\n"   /* run the original FUN_004f6580 */
    "    pushad\n"                            /* preserve its return value (EAX) */
    "    call _dr_on_damage_post\n"           /* exact kill decision on post-apply HP */
    "    popad\n"
    "    ret\n"                               /* original is plain-RET, no stack args */
    ".att_syntax prefix\n"
);

int dr_install_combat_report_hook(void)
{
    uintptr_t base   = (uintptr_t)GetModuleHandleA(NULL);
    uintptr_t target = base + RVA_DAMAGE;
    unsigned char *p = (unsigned char *)target;

    if (memcmp(p, EXPECTED_PROLO, PROLO_LEN) != 0) {
        OutputDebugStringA("[drhook] FUN_004f6580 prologue mismatch — combat-report hook aborted");
        return 0;
    }

    unsigned char *tramp = (unsigned char *)VirtualAlloc(
        NULL, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tramp) {
        OutputDebugStringA("[drhook] VirtualAlloc(combat trampoline) failed");
        return 0;
    }
    memcpy(tramp, p, PROLO_LEN);
    tramp[PROLO_LEN] = 0xE9;                  /* JMP rel32 back to original+PROLO_LEN */
    int32_t back = (int32_t)((target + PROLO_LEN) - ((uintptr_t)tramp + PROLO_LEN + 5));
    memcpy(tramp + PROLO_LEN + 1, &back, 4);
    g_trampoline_damage = (uintptr_t)tramp;

    DWORD oldProt;
    if (!VirtualProtect(p, PROLO_LEN, PAGE_EXECUTE_READWRITE, &oldProt)) {
        OutputDebugStringA("[drhook] VirtualProtect(FUN_004f6580) failed");
        return 0;
    }
    unsigned char patch[PROLO_LEN];
    patch[0] = 0xE9;
    int32_t rel = (int32_t)((uintptr_t)&detour_damage - (target + 5));
    memcpy(patch + 1, &rel, 4);
    for (int i = 5; i < PROLO_LEN; ++i) patch[i] = 0x90;   /* NOP pad */
    memcpy(p, patch, PROLO_LEN);
    VirtualProtect(p, PROLO_LEN, oldProt, &oldProt);
    FlushInstructionCache(GetCurrentProcess(), p, PROLO_LEN);

    OutputDebugStringA("[drhook] FUN_004f6580 combat-report hook installed (v2 post-apply kill detect)");
    return 1;
}
