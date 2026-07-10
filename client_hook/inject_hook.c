/*
 * inject_hook.c — server-driven mob→player damage injection.
 *
 * THE PROBLEM (bible §14.6 / §6; docs/MOB_ATTACK_INJECTION.md)
 * -----------------------------------------------------------
 * In server-driven chase mode (DR_MONSTER_AI=1) mobs are kept *display-only*
 * (never client-brain enrolled) so they stop at range instead of running through
 * the player. But a display-only mob can't hurt the player: the client only
 * applies mob→player damage for mobs it simulates, and the server can't send
 * authoritative avatar HP (the synch compare crashes). So server-chased mobs are
 * harmless — combat is broken.
 *
 * THE FIX
 * -------
 * The server tells this hook "mob E hit avatar A for N" over the telemetry
 * channel (OP_MOB_ATTACK); the hook applies the damage LOCALLY through the
 * engine's own damage sink, Damage::apply (FUN_004f6580). The avatar's HP drops,
 * floating combat text shows, death/threat fire — exactly like a native hit —
 * while the mob stays display-only. Player→mob combat is unchanged
 * (client-authoritative; kills reported via the existing KILL_AT_XP telemetry).
 *
 * Damage::apply takes EDI = a 0x44-byte Damage object. We build one exactly like
 * the engine's fixed/scripted-damage caller (build+apply ref site 0x50c087..
 * 0x50c0f9, Ghidra 2026-06-22): vtable + refcount + attacker + target + damage +
 * mode 4 (skip the resist recompute → EXACT damage) + element + flags 0x18.
 *
 * THREAD + DRAIN MODEL
 * --------------------
 * Damage::apply must run on the GAME thread; the telemetry socket runs on a
 * background thread. So:
 *   - the background reader (telemetry_client.c) only ENQUEUES pending attacks;
 *   - the per-frame world-clock pump FUN_005d9e30 (hooked here) DRAINS them at a
 *     clean frame boundary — NOT inside the synch-compare detour, which would
 *     re-enter combat in the middle of the entity-stream decoder;
 *   - the synch detour (hook.c) only RECORDS eid→Unit* into a frame-stamped
 *     cache. The drain resolves attacker (mob) + target (avatar) from that cache
 *     by eid and only uses entries refreshed within a freshness window, so a dead
 *     mob (no more heartbeats) is never dereferenced (stale-pointer safe without
 *     touching freed memory).
 */
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include "hook.h"

/* Diagnostics → OutputDebugStringA (DebugView / x64dbg) AND a file `drhook.log`
 * next to DungeonRunners.exe, so a live session can be read back post-hoc
 * without DebugView (added 2026-07-04 to ground the enroll-clamp run-through
 * hunt). RAW WIN32 ONLY — CreateFile/WriteFile, no CRT stdio: an earlier
 * fopen/fprintf version crashed zone-load (multithreaded CRT FILE init from the
 * socket + game threads → access violation, 2026-07-04). The lock + handle are
 * opened ONCE from dr_log_init (called in DllMain, single-threaded, before any
 * game/socket thread runs), so every later inj_dbg from any thread is safe.
 * _vsnprintf/_snprintf are string-only (no FILE) and were already in the light
 * build. */
#define DR_INJECT_DEBUG 1
static HANDLE           g_log_fh = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_log_lock;
static int              g_log_ready = 0;

static void dr_log_init(void)
{
#if DR_INJECT_DEBUG
    char path[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, path, MAX_PATH);       /* exe dir, not CWD */
    while (n && path[n - 1] != '\\') --n;
    lstrcpyA(path + n, "drhook.log");
    InitializeCriticalSection(&g_log_lock);
    g_log_fh = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    g_log_ready = 1;                                          /* set last (x86: visible) */
#endif
}

static void inj_dbg(const char *fmt, ...)
{
#if DR_INJECT_DEBUG
    char buf[256];
    va_list ap;
    int len;
    va_start(ap, fmt);
    len = _vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
    va_end(ap);
    if (len < 0 || len >= (int)sizeof(buf) - 1) { buf[sizeof(buf) - 1] = '\0'; len = (int)strlen(buf); }
    OutputDebugStringA(buf);

    if (g_log_ready && g_log_fh != INVALID_HANDLE_VALUE) {
        char line[320];
        int m = _snprintf(line, sizeof(line) - 1, "[%10lu] %.*s\r\n",
                          (unsigned long)GetTickCount(), len, buf);
        if (m < 0 || m >= (int)sizeof(line)) m = (int)sizeof(line) - 1;
        DWORD wr;
        EnterCriticalSection(&g_log_lock);
        WriteFile(g_log_fh, line, (DWORD)m, &wr, NULL);       /* FILE_APPEND_DATA = atomic tail */
        LeaveCriticalSection(&g_log_lock);
    }
#else
    (void)fmt;
#endif
}

/* RVAs (VA - 0x00400000, the Ghidra image base; this build has no ASLR). */
#define RVA_DAMAGE_APPLY 0x000F6580u   /* FUN_004f6580 Damage::apply (EDI = Damage*) */
#define RVA_DAMAGE_VTBL  0x0046F834u   /* PTR_FUN_0086f834 — the Damage vtable */
#define RVA_PUMP         0x001D9E30u   /* FUN_005d9e30 — per-frame world-clock pump */

/* FUN_005d9e30 prologue: PUSH EBP; MOV EBP,ESP; AND ESP,-8 (all relocatable). */
#define PUMP_PROLO_LEN   6
static const unsigned char PUMP_PROLO[PUMP_PROLO_LEN] =
    { 0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8 };

/* ── the run-through fix: detour the Unit position SETTER ──
 * FUN_0050bd40 commits a unit's new world position (writes +0x90/+0x94/+0x98
 * from EAX = a 3-int vector; unit = the first STACK arg). The mob's mover calls
 * it every frame with the mob's INTENDED next position — which, for a chasing
 * mob, drives straight to the avatar's centre (live-measured 15.4→2.36u, no ring
 * — bible §14.6 round 6i). We detour it and, for a server-marked aggroed mob,
 * clamp that intended position OUT to the melee stop-ring BEFORE the setter
 * commits it. Because the setter itself writes the clamped value, there is
 * NOTHING to overwrite it — categorically different from the round-6e post-mover
 * +0x90 write that the mover reverted 109× (bible §14.6). Prologue identical to
 * the pump (PUSH EBP; MOV EBP,ESP; AND ESP,-8 — all relocatable). Live-verified
 * 2026-07-04 (x64dbg PID 15468). */
#define RVA_SETPOS       0x0010BD40u   /* FUN_0050bd40 Unit::setWorldPosition */
#define SETPOS_PROLO_LEN 6
static const unsigned char SETPOS_PROLO[SETPOS_PROLO_LEN] =
    { 0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8 };
#define RVA_STOCKUNIT_VTBL 0x00472940u /* 0x00872940 — StockUnit (mob) vtable */

/* Entity field offset (live-verified — combat_hook.c / bible §6-LIVE.4). */
#define UNIT_EID_OFF     0x80          /* u16 entity id */

/* Unit WORLD horizontal position — Fixed32 (world units ×256). Ghidra 2026-07-02
 * (Follow update FUN_00526f70 reads exactly these for both mob and avatar in its
 * distance math → shared base-class field, NOT the avatar-only +0x130 cache). Used
 * by the run-through clamp (bible §14.6). The +0x130/+0x134 avatar cache is the
 * self-validation reference (known-good). STATIC-derived — the clamp cross-checks
 * +0x90/+0x94 against +0x130/+0x134 on the avatar before ANY write, so a wrong
 * offset degrades to a logged no-op instead of teleporting/crashing. */
#define UNIT_POS_X_OFF   0x90          /* i32 world X ×256 */
#define UNIT_POS_Y_OFF   0x94          /* i32 world Y ×256 */
#define UNIT_POSCACHE_X  0x130         /* avatar-only world-coord cache X (validation) */
#define UNIT_POSCACHE_Y  0x134         /* avatar-only world-coord cache Y (validation) */
#define POS_VALIDATE_TOL (8 * 256)     /* |+0x90 − +0x130| must be ≤ 8 units to trust */
#define UNIT_HP_OFF      0x2f0         /* i32 current HP (wire) — corpse gate */

/* Damage object (0x44 bytes) — field offsets from the build ref site (Ghidra). */
#define DMG_SIZE         0x44
#define DMG_VTABLE       0x00
#define DMG_REFCOUNT     0x04
#define DMG_ATTACKER     0x2c
#define DMG_TARGET       0x34
#define DMG_DAMAGE       0x38          /* wire (HP ×256) */
#define DMG_MODE         0x3e          /* 4 = exact (skip FUN_004f67f0 resist recompute) */
#define DMG_ELEMENT      0x3f          /* 0 = physical */
#define DMG_FLAGS        0x41          /* 0x18 = apply-HP + display paths */

/* ── eid → Unit* cache (GAME-THREAD ONLY: written by the synch detour via
 * dr_inject_note_unit, read by the pump drain). Frame-stamped for staleness. */
#define CACHE_CAP    64
#define FRESH_FRAMES 30                /* ~0.5 s @60fps; mob heartbeat is ~0.15 s */
typedef struct { unsigned eid; void *unit; unsigned long seen; } cache_ent_t;
static cache_ent_t g_cache[CACHE_CAP];
static unsigned long g_frame = 0;

/* ── pending mob-attacks (socket-thread producer / pump-drain consumer) ── */
#define PEND_CAP 64
typedef struct {
    unsigned mob_eid, avatar_eid, damage_wire;
    unsigned char element;
} pend_t;
static pend_t           g_pend[PEND_CAP];
static volatile LONG    g_pend_n = 0;
static CRITICAL_SECTION g_pend_lock;
static volatile LONG    g_pend_init = 0;

/* ── mob-clamp intents (run-through fix, telemetry OP_MOB_CLAMP) ──
 * Persistent (unlike attacks) — refreshed by the server on the mob's chase
 * cadence and freshness-gated so a de-aggroed/dead mob ages out. Written under
 * g_pend_lock from the socket thread; snapshot-read on the game thread. */
#define CLAMP_CAP        64            /* dense rooms enroll+clamp ~46 mobs (bible density) */
#define CLAMP_FRESH_MS   500           /* tolerate a few dropped refreshes */
typedef struct {
    unsigned mob_eid, avatar_eid, ring_wire;
    DWORD    seen_ms;                  /* GetTickCount at last refresh */
} clamp_t;
static clamp_t          g_clamp[CLAMP_CAP];
static unsigned         g_avatar_eid = 0;      /* last avatar eid the server named */
/* Position-offset self-validation: 0 = untested, 1 = +0x90/+0x94 confirmed as
 * world position (matches the avatar's +0x130 cache), -1 = mismatch → NEVER
 * write (offset wrong; logged for correction). */
static int              g_pos_validated = 0;

/* ── game-thread clamp snapshot (mover-input clamp, round 6i) ──
 * Rebuilt each pump frame by prep_clamp() BEFORE the world update runs the
 * movers, then read LOCK-FREE by the setter detour dr_clamp_setpos on the SAME
 * (game) thread. Holds the aggroed mobs the server marked (from g_clamp) plus the
 * frame-resolved avatar Unit* so the hot detour never takes a lock or calls the
 * engine's entity lookup. */
typedef struct { unsigned mob_eid, ring_wire; } clamp_active_t;
static clamp_active_t   g_clamp_active[CLAMP_CAP];
static int              g_clamp_active_n = 0;
static void            *g_avatar_unit_frame = 0;  /* avatar Unit* resolved this frame */
static unsigned         g_clamp_hits = 0;         /* setter clamps applied this summary */
static uintptr_t        g_stockunit_vtbl = 0;     /* base + RVA_STOCKUNIT_VTBL */

/* ── recently-killed mob blocklist ──
 * The client kills mobs LOCALLY (player→mob is client-authoritative); the moment
 * the combat detour sees that lethal blow (dr_on_damage → dr_inject_forget_mob)
 * we drop the mob's pending attacks / clamp / cache AND block-list its eid, so no
 * further server-streamed MOB_ATTACK/MOB_CLAMP is applied for it. Without this the
 * server keeps swinging until it processes the kill telemetry (a round-trip
 * later), and the injected damage keeps landing "after the mob is dead" (live
 * 2026-07-03). Short TTL so a re-used eid in the same zone eventually frees. */
#define KILLED_CAP       32
#define KILLED_BLOCK_MS  3000
typedef struct { unsigned eid; DWORD killed_ms; } killed_t;
static killed_t         g_killed[KILLED_CAP];

/* Is eid on the fresh kill blocklist? Call under g_pend_lock. */
static int is_recently_killed(unsigned eid)
{
    DWORD now = GetTickCount();
    for (int i = 0; i < KILLED_CAP; ++i)
        if (g_killed[i].eid == eid && (now - g_killed[i].killed_ms) <= KILLED_BLOCK_MS)
            return 1;
    return 0;
}

/* Shared with the asm below. apply = the call target; tramp_pump = original. */
uintptr_t g_damage_apply   = 0;        /* asm reads _g_damage_apply */
uintptr_t g_trampoline_pump = 0;       /* asm reads _g_trampoline_pump */
uintptr_t g_trampoline_setpos = 0;     /* asm reads _g_trampoline_setpos */
static uintptr_t g_damage_vtbl = 0;

/* ── ACTIVE eid→Unit* resolution (v2, 2026-07-03) ──
 * The pump FUN_005d9e30 is __fastcall(ClientEntityManager*) — ECX at our detour
 * entry IS the live entity manager; the detour stows it here every frame. The
 * manager's vtbl+0xC4 is the engine's own find-entity-by-id (thiscall, one u16
 * arg, callee-clean) — the exact call the 0x05 entity-remove handler makes
 * (FUN_005daf60 @ 0x5dafa5, "Invalid EntityID(%u) from server" on NULL). This
 * REPLACES the passive synch-fed cache for the drain/clamp lookups: a destroyed
 * entity resolves NULL (never a stale pointer), and a mob that hasn't exchanged
 * blows yet (enroll model approach phase — where the passive cache was empty and
 * the clamp couldn't engage → run-through, live 2026-07-03) resolves immediately.
 * The returned pointer is a BORROWED same-frame reference (the manager holds it;
 * we never store it past the drain). Game thread only. */
uintptr_t g_entity_mgr = 0;            /* asm writes _g_entity_mgr (pump ECX) */
extern uintptr_t g_validate;           /* hook.c: FUN_005cb650 "is unit valid?" */
typedef unsigned char (__fastcall *validate_fn)(void *self, void *edx_unused);
typedef void *(__fastcall *find_entity_fn)(void *mgr, void *edx_unused,
                                           unsigned eid);

static void *resolve_unit(unsigned eid)
{
    if (!eid || !g_entity_mgr) return 0;
    find_entity_fn fn =
        *(find_entity_fn *)(*(uintptr_t *)g_entity_mgr + 0xC4);
    void *unit = fn((void *)g_entity_mgr, 0, eid);
    if (!unit) return 0;
    if (g_validate && !((validate_fn)g_validate)(unit, 0)) return 0;
    return unit;
}

/* Unit lookup for the drain/clamp. Prefer the live engine map; fall back to the
 * passive synch cache only while the manager hasn't been captured yet (i.e.
 * never inside a drain — the drain runs from the pump that captures it). */
static void *cache_lookup(unsigned eid, int require_fresh);   /* defined below */
static void *lookup_unit(unsigned eid, int require_fresh)
{
    if (g_entity_mgr) return resolve_unit(eid);
    return cache_lookup(eid, require_fresh);
}

extern void detour_pump(void);                 /* asm, decorated _detour_pump */
extern void detour_setpos(void);               /* asm, decorated _detour_setpos */
extern void dr_call_damage_apply(void *dmg);   /* asm thunk: EDI=dmg; call apply */
void __cdecl dr_clamp_setpos(void *unit, int *newpos);  /* called from _detour_setpos */

static void ensure_pend_lock(void)
{
    if (InterlockedCompareExchange(&g_pend_init, 1, 0) == 0)
        InitializeCriticalSection(&g_pend_lock);
}

/* Called from the synch detour (game thread) for every validated synched unit. */
void __cdecl dr_inject_note_unit(void *unit)
{
    if (!unit) return;
    unsigned eid = *(unsigned short *)((unsigned char *)unit + UNIT_EID_OFF);
    if (!eid) return;

    int free_slot = -1;
    for (int i = 0; i < CACHE_CAP; ++i) {
        if (g_cache[i].eid == eid && g_cache[i].unit) {   /* refresh existing */
            g_cache[i].unit = unit;
            g_cache[i].seen = g_frame;
            return;
        }
        if (free_slot < 0 && g_cache[i].unit == 0)
            free_slot = i;
    }
    if (free_slot < 0) {                                   /* evict the stalest */
        unsigned long oldest = ~0UL;
        free_slot = 0;
        for (int i = 0; i < CACHE_CAP; ++i)
            if (g_cache[i].seen < oldest) { oldest = g_cache[i].seen; free_slot = i; }
    }
    g_cache[free_slot].eid  = eid;
    g_cache[free_slot].unit = unit;
    g_cache[free_slot].seen = g_frame;
    inj_dbg("[inj] cache NEW eid=%u unit=%p", eid, unit);
}

/* Look up a unit by eid. require_fresh gates on the freshness window:
 *  - attacker (mob): MUST be fresh — a dead mob stops heartbeating, so a stale
 *    entry means it's gone; never deref it (freed-pointer safe).
 *  - target (avatar): NOT gated — the avatar isn't synched while the player is
 *    idle (no move-ack), so its entry legitimately ages, but its pointer is
 *    STABLE within a zone. Cross-zone staleness is prevented by dr_inject_reset
 *    (server OP_ZONE_RESET on zone change), so an un-fresh avatar entry is still
 *    a live pointer. */
static void *cache_lookup(unsigned eid, int require_fresh)
{
    if (!eid) return 0;
    for (int i = 0; i < CACHE_CAP; ++i) {
        if (g_cache[i].eid != eid || !g_cache[i].unit)
            continue;
        if (require_fresh && (g_frame - g_cache[i].seen) > FRESH_FRAMES)
            return 0;
        return g_cache[i].unit;
    }
    return 0;
}

/* Drop all cached units + pending attacks. Called on a server OP_ZONE_RESET so
 * no Unit* learned in the previous zone (where the avatar/mobs are freed and
 * re-created) can be dereferenced after the transfer. */
void dr_inject_reset(void)
{
    memset(g_cache, 0, sizeof(g_cache));
    if (g_pend_init) {
        EnterCriticalSection(&g_pend_lock);
        g_pend_n = 0;
        memset(g_clamp, 0, sizeof(g_clamp));   /* stale-zone clamp targets */
        memset(g_killed, 0, sizeof(g_killed)); /* stale-zone kill blocklist */
        LeaveCriticalSection(&g_pend_lock);
    }
    /* keep g_pos_validated — the position offset is build-constant, not per-zone */
}

/* Called from the telemetry reader (background thread) per OP_MOB_CLAMP. Marks
 * a mob as an aggroed follower to pin at the stop-ring around the avatar. */
void dr_inject_set_clamp(unsigned mob_eid, unsigned avatar_eid, unsigned ring_wire)
{
    if (!mob_eid) return;
    ensure_pend_lock();
    EnterCriticalSection(&g_pend_lock);
    if (is_recently_killed(mob_eid)) {          /* client already killed it — don't re-pin */
        LeaveCriticalSection(&g_pend_lock);
        return;
    }
    g_avatar_eid = avatar_eid;
    DWORD now = GetTickCount();
    int slot = -1, free_slot = -1;
    for (int i = 0; i < CLAMP_CAP; ++i) {
        if (g_clamp[i].mob_eid == mob_eid) { slot = i; break; }   /* refresh */
        if (free_slot < 0 &&
            (g_clamp[i].mob_eid == 0 || now - g_clamp[i].seen_ms > CLAMP_FRESH_MS))
            free_slot = i;                                        /* free or stale */
    }
    if (slot < 0) slot = free_slot;
    if (slot >= 0) {
        int is_new = (g_clamp[slot].mob_eid != mob_eid);
        g_clamp[slot].mob_eid    = mob_eid;
        g_clamp[slot].avatar_eid = avatar_eid;
        g_clamp[slot].ring_wire  = ring_wire;
        g_clamp[slot].seen_ms    = now;
        LeaveCriticalSection(&g_pend_lock);
        if (is_new)
            inj_dbg("[clamp] RX NEW mob=%u av=%u ring=%u", mob_eid, avatar_eid,
                    ring_wire);
        return;
    }
    LeaveCriticalSection(&g_pend_lock);
}

/* Sane world-coordinate bound (Fixed32 wire): |coord| < 0x800000 = 32768 world
 * units — larger than any zone, but far below a live heap pointer (the client's
 * Unit* run ~0x0C000000–0x27000000, i.e. 200M+). So a real coordinate passes and
 * a garbage/pointer read fails. */
#define POS_SANE_MAX 0x800000

/* Confirm +0x90/+0x94 is the avatar's world position, then enable the clamp.
 *
 * ★ 2026-07-04: the ORIGINAL check cross-referenced the avatar's +0x130/+0x134
 * cache — but that cache reads (0,0) for a LIVE avatar (it is only populated at
 * death time, combat_hook.c), so the check was a false negative that DISABLED
 * the clamp every session (live: +0x90=-162894 +0x94=197350 = plausible coords,
 * but +0x130/+0x134=0 → "offset wrong"). +0x90/+0x94 is Ghidra-proven the shared
 * world-position field (FUN_00526f70 Follow-distance math reads exactly these for
 * both mob and avatar — bible §14.6). So validate it DIRECTLY: non-zero and in a
 * sane range distinguishes a real coordinate from a zero/garbage/pointer read,
 * without depending on the dead +0x130 cache. Fail-safe preserved: an implausible
 * read still DISABLES the clamp. Runs once, game thread, once the avatar caches. */
static void validate_pos_offset(void)
{
    if (g_pos_validated != 0 || !g_avatar_eid) return;
    unsigned char *av = (unsigned char *)lookup_unit(g_avatar_eid, 0);
    if (!av) return;
    int lx = *(int *)(av + UNIT_POS_X_OFF);
    int ly = *(int *)(av + UNIT_POS_Y_OFF);
    int cx = *(int *)(av + UNIT_POSCACHE_X);      /* logged for reference only now */
    int cy = *(int *)(av + UNIT_POSCACHE_Y);
    inj_dbg("[clamp] validate avatar +0x90=%d +0x94=%d (+0x130=%d +0x134=%d ref)",
            lx, ly, cx, cy);
    if ((lx != 0 || ly != 0) && abs(lx) < POS_SANE_MAX && abs(ly) < POS_SANE_MAX) {
        g_pos_validated = 1;
        inj_dbg("[clamp] +0x90/+0x94 ACCEPTED as world position (%d,%d) — clamp ACTIVE",
                lx, ly);
    } else {
        g_pos_validated = -1;
        inj_dbg("[clamp] +0x90/+0x94 implausible (%d,%d) — clamp DISABLED", lx, ly);
    }
}

/* ★ THE MOVER-INPUT CLAMP (round 6i, 2026-07-04) — called from the setter
 * detour (_detour_setpos → dr_clamp_setpos) on the GAME thread, once per unit
 * position-commit. For a server-marked aggroed mob whose INTENDED new position
 * (newpos = the setter's EAX vector) would land inside the melee stop-ring, we
 * rewrite newpos OUT to the ring — so the setter commits the ring position and
 * the mob physically stops at range instead of burying itself in the avatar.
 * Only pushes OUTWARD (dist<ring), so the natural approach from far is untouched
 * and only the final penetration is corrected. Reads the game-thread snapshot
 * (g_clamp_active / g_avatar_unit_frame) built by prep_clamp at frame start — no
 * lock, no engine call in this hot path. Inert unless: the offset is validated,
 * the unit is a StockUnit, it's a fresh server clamp target, and the avatar
 * resolved this frame — so it never touches players/NPCs or unmarked mobs. */
void __cdecl dr_clamp_setpos(void *unit_v, int *newpos)
{
    if (!unit_v || !newpos || g_pos_validated != 1) return;
    unsigned char *unit = (unsigned char *)unit_v;
    if (*(uintptr_t *)unit != g_stockunit_vtbl) return;      /* mobs only */
    unsigned char *av = (unsigned char *)g_avatar_unit_frame;
    if (!av) return;
    unsigned eid = *(unsigned short *)(unit + UNIT_EID_OFF);
    if (!eid) return;

    unsigned ring_wire = 0;
    for (int i = 0; i < g_clamp_active_n; ++i)
        if (g_clamp_active[i].mob_eid == eid) { ring_wire = g_clamp_active[i].ring_wire; break; }
    if (!ring_wire) return;                                  /* not an aggroed target */
    if (*(int *)(unit + UNIT_HP_OFF) <= 0) return;           /* corpse can't be pinned */

    int ax = *(int *)(av + UNIT_POS_X_OFF);
    int ay = *(int *)(av + UNIT_POS_Y_OFF);
    double dx = (double)(newpos[0] - ax), dy = (double)(newpos[1] - ay);
    double dist = sqrt(dx * dx + dy * dy);
    double ring = (double)ring_wire;
    if (dist >= ring) return;                                /* outside ring → natural approach */
    if (dist < 1.0) {                                        /* intended pos ~ on the avatar */
        int mx = *(int *)(unit + UNIT_POS_X_OFF);            /* fall back to current-pos direction */
        int my = *(int *)(unit + UNIT_POS_Y_OFF);
        dx = (double)(mx - ax); dy = (double)(my - ay);
        dist = sqrt(dx * dx + dy * dy);
        if (dist < 1.0) { dx = 1.0; dy = 0.0; dist = 1.0; }  /* degenerate → arbitrary axis */
    }
    newpos[0] = (int)(ax + dx / dist * ring);                /* clamp intended pos OUT to the ring */
    newpos[1] = (int)(ay + dy / dist * ring);                /* (Z newpos[2] left as authored) */
    ++g_clamp_hits;
}

/* Rebuild the game-thread clamp snapshot BEFORE this frame's movers run, and
 * resolve the avatar once. Game thread (pump drain, ahead of the world update).
 * Replaces the old post-mover clamp_mobs write (round-6e dead-end: the mover
 * overwrote it every frame — bible §14.6); the actual clamp now happens at the
 * setter's INPUT (dr_clamp_setpos), which the mover cannot revert. */
static void prep_clamp(void)
{
    DWORD now = GetTickCount();
    int n = 0;
    if (g_pend_init) {
        EnterCriticalSection(&g_pend_lock);
        for (int i = 0; i < CLAMP_CAP; ++i)
            if (g_clamp[i].mob_eid && (now - g_clamp[i].seen_ms) <= CLAMP_FRESH_MS) {
                g_clamp_active[n].mob_eid   = g_clamp[i].mob_eid;
                g_clamp_active[n].ring_wire = g_clamp[i].ring_wire;
                ++n;
            }
        LeaveCriticalSection(&g_pend_lock);
    }
    g_clamp_active_n = n;                                     /* publish for the setter detour */
    g_avatar_unit_frame =
        (g_pos_validated == 1 && g_avatar_eid) ? lookup_unit(g_avatar_eid, 0) : 0;

    /* ── summary every ~2 s: is the clamp firing + holding mobs at the ring? ── */
    static unsigned long s_sum_frame;
    static int s_rec, s_smp_dist, s_smp_ring;
    static unsigned s_smp_eid;
    s_rec += n;
    if (n && g_avatar_unit_frame) {                          /* sample the first fresh mob */
        unsigned char *mob = (unsigned char *)lookup_unit(g_clamp_active[0].mob_eid, 1);
        if (mob) {
            unsigned char *av = (unsigned char *)g_avatar_unit_frame;
            double dx = (double)(*(int *)(mob + UNIT_POS_X_OFF) - *(int *)(av + UNIT_POS_X_OFF));
            double dy = (double)(*(int *)(mob + UNIT_POS_Y_OFF) - *(int *)(av + UNIT_POS_Y_OFF));
            s_smp_eid = g_clamp_active[0].mob_eid;
            s_smp_dist = (int)sqrt(dx * dx + dy * dy);
            s_smp_ring = (int)g_clamp_active[0].ring_wire;
        }
    }
    if (g_frame - s_sum_frame >= 120) {
        if (s_rec)
            inj_dbg("[clamp] sum active=%d hits=%u val=%d | smp eid=%u dist=%d ring=%d",
                    s_rec, g_clamp_hits, g_pos_validated, s_smp_eid, s_smp_dist, s_smp_ring);
        s_rec = 0;
        g_clamp_hits = 0;
        s_sum_frame = g_frame;
    }
}

/* Called from the telemetry reader (background thread) per OP_MOB_ATTACK. */
void dr_inject_enqueue(unsigned mob_eid, unsigned avatar_eid,
                       unsigned damage_wire, unsigned char element)
{
    ensure_pend_lock();
    EnterCriticalSection(&g_pend_lock);
    if (is_recently_killed(mob_eid)) {          /* client already killed it — ignore */
        LeaveCriticalSection(&g_pend_lock);
        return;
    }
    if (g_pend_n < PEND_CAP) {
        pend_t *p = &g_pend[g_pend_n++];
        p->mob_eid = mob_eid; p->avatar_eid = avatar_eid;
        p->damage_wire = damage_wire; p->element = element;
    }
    LeaveCriticalSection(&g_pend_lock);
    inj_dbg("[inj] RX mob=%u avatar=%u dmg=%u", mob_eid, avatar_eid, damage_wire);
}

/* The client killed a mob locally (combat_hook.c dr_on_damage saw the lethal
 * blow). Drop everything the injection layer holds for it and block-list its eid
 * so late server MOB_ATTACK/MOB_CLAMP for the same mob is ignored — no damage
 * "after the mob is dead". Safe no-op for a non-mob victim (avatar/summon eid is
 * never a tracked mob_eid). Runs on the game thread (from the damage detour). */
void dr_inject_forget_mob(unsigned eid)
{
    if (!eid) return;
    ensure_pend_lock();
    EnterCriticalSection(&g_pend_lock);
    /* record on the blocklist (reuse this eid's slot, else the oldest) */
    DWORD now = GetTickCount();
    int slot = -1, oldest = 0;
    DWORD oldest_ms = ~0u;
    for (int i = 0; i < KILLED_CAP; ++i) {
        if (g_killed[i].eid == eid) { slot = i; break; }
        if (g_killed[i].killed_ms < oldest_ms) { oldest_ms = g_killed[i].killed_ms; oldest = i; }
    }
    if (slot < 0) slot = oldest;
    g_killed[slot].eid = eid;
    g_killed[slot].killed_ms = now;
    /* drop pending attacks for this mob (compact in place) */
    int w = 0;
    for (int i = 0; i < g_pend_n; ++i)
        if (g_pend[i].mob_eid != eid) g_pend[w++] = g_pend[i];
    int dropped = (int)g_pend_n - w;
    g_pend_n = w;
    /* drop clamp intents for this mob */
    for (int i = 0; i < CLAMP_CAP; ++i)
        if (g_clamp[i].mob_eid == eid) memset(&g_clamp[i], 0, sizeof(g_clamp[i]));
    LeaveCriticalSection(&g_pend_lock);
    /* drop the cache entry (game-thread-only; this runs on the game thread) */
    for (int i = 0; i < CACHE_CAP; ++i)
        if (g_cache[i].eid == eid) { g_cache[i].unit = 0; g_cache[i].eid = 0; }
    if (dropped)
        inj_dbg("[inj] forget mob eid=%u (killed) — dropped %d pending + clamp + cache",
                eid, dropped);
}

/* Build a Damage object identical to the engine's fixed-damage caller and apply
 * it. attacker = mob, target = avatar, both live (cache-fresh). */
static void apply_one(void *attacker, void *target,
                      unsigned damage_wire, unsigned char element)
{
    unsigned char dmg[DMG_SIZE];
    memset(dmg, 0, DMG_SIZE);
    *(uintptr_t *)(dmg + DMG_VTABLE)   = g_damage_vtbl;
    *(int *)(dmg + DMG_REFCOUNT)       = 1;
    *(void **)(dmg + DMG_ATTACKER)     = attacker;
    *(void **)(dmg + DMG_TARGET)       = target;
    *(int *)(dmg + DMG_DAMAGE)         = (int)damage_wire;
    dmg[DMG_MODE]    = 0x04;            /* exact damage (skip resist recompute) */
    dmg[DMG_ELEMENT] = element;
    dmg[DMG_FLAGS]   = 0x18;            /* apply-HP + display */
    dr_call_damage_apply(dmg);
}

/* Drains pending mob-attacks at the per-frame pump boundary (game thread). */
void __cdecl dr_inject_drain(void)
{
    ++g_frame;
    static int banner_done = 0;
    if (!banner_done && g_entity_mgr) {     /* first pump: safe to touch the CRT */
        banner_done = 1;
        inj_dbg("[drhook] build " __DATE__ " " __TIME__
                " — v2 live entity lookup; mgr=%p vtbl+0xC4=%p",
                (void *)g_entity_mgr,
                (void *)*(uintptr_t *)(*(uintptr_t *)g_entity_mgr + 0xC4));
    }
    if (!g_damage_apply || !g_damage_vtbl) return;

    pend_t local[PEND_CAP];
    int n = 0;
    if (g_pend_init) {
        EnterCriticalSection(&g_pend_lock);
        n = g_pend_n;
        if (n > 0) { memcpy(local, g_pend, (size_t)n * sizeof(pend_t)); g_pend_n = 0; }
        LeaveCriticalSection(&g_pend_lock);
    }
    for (int i = 0; i < n; ++i) {
        void *attacker = lookup_unit(local[i].mob_eid, 1);     /* NULL once destroyed */
        void *target   = lookup_unit(local[i].avatar_eid, 0);
        if (!attacker || !target) {
            inj_dbg("[inj] SKIP mob=%u avatar=%u atk=%p tgt=%p",
                    local[i].mob_eid, local[i].avatar_eid, attacker, target);
            continue;                   /* mob gone or avatar unresolvable — skip */
        }
        if (*(int *)((unsigned char *)attacker + 0x2f0) <= 0) {
            inj_dbg("[inj] SKIP mob=%u dead (hp<=0)", local[i].mob_eid);
            continue;                   /* corpse can't swing — kill beat the queue */
        }
        inj_dbg("[inj] APPLY mob=%u avatar=%u dmg=%u atk=%p tgt=%p",
                local[i].mob_eid, local[i].avatar_eid, local[i].damage_wire,
                attacker, target);
        apply_one(attacker, target, local[i].damage_wire, local[i].element);
    }

    /* Run-through fix (round 6i): validate the position offset once, then rebuild
     * the game-thread clamp snapshot for THIS frame's movers. The actual clamp
     * happens at the setter's input (dr_clamp_setpos via _detour_setpos) — which
     * the mover can't revert — not here (the old post-mover write was reverted
     * every frame, bible §14.6). This runs at the pump, BEFORE the world update
     * drives the movers, so the snapshot is fresh when they call the setter. */
    validate_pos_offset();
    prep_clamp();
}

/* ── asm: the pump detour + the EDI-input apply thunk ── */
__asm__(
    ".intel_syntax noprefix\n"
    ".text\n"
    ".globl _detour_pump\n"
"_detour_pump:\n"
    "    mov  dword ptr [_g_entity_mgr], ecx\n"  /* FUN_005d9e30 is __fastcall(mgr) — capture it */
    "    pushad\n"
    "    call _dr_inject_drain\n"               /* drain pending attacks (cdecl, no args) */
    "    popad\n"
    "    jmp  dword ptr [_g_trampoline_pump]\n"  /* run the original FUN_005d9e30 */
    "\n"
    ".globl _dr_call_damage_apply\n"
"_dr_call_damage_apply:\n"                       /* __cdecl(void *dmg) */
    "    push ebp\n"
    "    mov  ebp, esp\n"
    "    push edi\n"
    "    push esi\n"
    "    push ebx\n"
    "    mov  edi, dword ptr [ebp+8]\n"          /* dmg */
    "    mov  eax, dword ptr [_g_damage_apply]\n"
    "    call eax\n"                             /* Damage::apply(EDI = dmg); plain RET */
    "    pop  ebx\n"
    "    pop  esi\n"
    "    pop  edi\n"
    "    mov  esp, ebp\n"
    "    pop  ebp\n"
    "    ret\n"
    "\n"
    /* Position-setter detour (run-through clamp). On entry (jmp from FUN_0050bd40+0):
     *   EAX = newpos vector ptr (3 i32), [esp+4] = unit, [esp] = return addr.
     * We save flags + scratch regs, call dr_clamp_setpos(unit, newpos) which may
     * rewrite the vector in place, restore (EAX still = newpos ptr), then run the
     * stolen prologue via the trampoline. EBX/ESI/EDI/EBP are untouched, so the
     * original function resumes exactly as if uncalled. */
    ".globl _detour_setpos\n"
"_detour_setpos:\n"
    "    pushfd\n"
    "    push eax\n"                          /* [esp+0x08] after the 3 pushes below */
    "    push ecx\n"
    "    push edx\n"
    "    mov  eax, dword ptr [esp+0x14]\n"     /* unit  = original [esp+4] */
    "    mov  ecx, dword ptr [esp+0x08]\n"     /* newpos = saved EAX (setter's vector arg) */
    "    push ecx\n"                           /* arg1 = newpos */
    "    push eax\n"                           /* arg0 = unit */
    "    call _dr_clamp_setpos\n"              /* __cdecl(unit, newpos); may clamp [newpos] */
    "    add  esp, 8\n"
    "    pop  edx\n"
    "    pop  ecx\n"
    "    pop  eax\n"                           /* EAX = newpos ptr again (contents maybe clamped) */
    "    popfd\n"
    "    jmp  dword ptr [_g_trampoline_setpos]\n"  /* stolen prologue → back to FUN_0050bd40+6 */
    ".att_syntax prefix\n"
);

int dr_install_inject_hook(void)
{
    dr_log_init();                     /* open drhook.log ONCE, single-threaded (DllMain) */
    ensure_pend_lock();                /* ★ init g_pend_lock NOW (DllMain, single-threaded).
                                        * clamp_mobs runs EnterCriticalSection(&g_pend_lock)
                                        * every pump frame — including during zone load,
                                        * BEFORE any MOB_CLAMP arrives to lazily init it via
                                        * dr_inject_set_clamp. Entering a zero-filled (never
                                        * InitializeCriticalSection'd) CS writes through its
                                        * NULL DebugInfo at +0x14 → the "WRITE 0x14" access
                                        * violation / world-clock deadlock that blocked zone
                                        * load (2026-07-04). The old clamp_mobs dodged it with
                                        * a top-of-function g_pos_validated!=1 early-return
                                        * (never reached the lock pre-validation); the
                                        * instrumented version moved that guard down. */
    uintptr_t base   = (uintptr_t)GetModuleHandleA(NULL);
    g_damage_apply   = base + RVA_DAMAGE_APPLY;
    g_damage_vtbl    = base + RVA_DAMAGE_VTBL;
    uintptr_t target = base + RVA_PUMP;
    unsigned char *p = (unsigned char *)target;

    if (memcmp(p, PUMP_PROLO, PUMP_PROLO_LEN) != 0) {
        OutputDebugStringA("[drhook] FUN_005d9e30 prologue mismatch — inject hook aborted");
        return 0;
    }

    unsigned char *tramp = (unsigned char *)VirtualAlloc(
        NULL, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tramp) {
        OutputDebugStringA("[drhook] VirtualAlloc(pump trampoline) failed");
        return 0;
    }
    memcpy(tramp, p, PUMP_PROLO_LEN);
    tramp[PUMP_PROLO_LEN] = 0xE9;                /* JMP rel32 back to original+PROLO */
    int32_t back = (int32_t)((target + PUMP_PROLO_LEN) - ((uintptr_t)tramp + PUMP_PROLO_LEN + 5));
    memcpy(tramp + PUMP_PROLO_LEN + 1, &back, 4);
    g_trampoline_pump = (uintptr_t)tramp;

    DWORD oldProt;
    if (!VirtualProtect(p, PUMP_PROLO_LEN, PAGE_EXECUTE_READWRITE, &oldProt)) {
        OutputDebugStringA("[drhook] VirtualProtect(FUN_005d9e30) failed");
        return 0;
    }
    unsigned char patch[PUMP_PROLO_LEN];
    patch[0] = 0xE9;
    int32_t rel = (int32_t)((uintptr_t)&detour_pump - (target + 5));
    memcpy(patch + 1, &rel, 4);
    for (int i = 5; i < PUMP_PROLO_LEN; ++i) patch[i] = 0x90;   /* NOP pad */
    memcpy(p, patch, PUMP_PROLO_LEN);
    VirtualProtect(p, PUMP_PROLO_LEN, oldProt, &oldProt);
    FlushInstructionCache(GetCurrentProcess(), p, PUMP_PROLO_LEN);

    OutputDebugStringA("[drhook] FUN_005d9e30 pump hook installed — drain active "
                       "(v2 live entity lookup via mgr vtbl+0xC4)");

    /* ── install the position-setter clamp detour (run-through fix, round 6i) ──
     * Inert unless the server streams OP_MOB_CLAMP for aggroed mobs AND the pos
     * offset validates — so installing it is safe in every model. */
    g_stockunit_vtbl = base + RVA_STOCKUNIT_VTBL;
    {
        uintptr_t sp_t = base + RVA_SETPOS;
        unsigned char *sp = (unsigned char *)sp_t;
        if (memcmp(sp, SETPOS_PROLO, SETPOS_PROLO_LEN) != 0) {
            OutputDebugStringA("[drhook] FUN_0050bd40 prologue mismatch — setpos clamp NOT installed");
        } else {
            unsigned char *tr = (unsigned char *)VirtualAlloc(
                NULL, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
            if (!tr) {
                OutputDebugStringA("[drhook] VirtualAlloc(setpos trampoline) failed");
            } else {
                memcpy(tr, sp, SETPOS_PROLO_LEN);
                tr[SETPOS_PROLO_LEN] = 0xE9;
                int32_t sback = (int32_t)((sp_t + SETPOS_PROLO_LEN) -
                                          ((uintptr_t)tr + SETPOS_PROLO_LEN + 5));
                memcpy(tr + SETPOS_PROLO_LEN + 1, &sback, 4);
                g_trampoline_setpos = (uintptr_t)tr;

                DWORD op;
                if (VirtualProtect(sp, SETPOS_PROLO_LEN, PAGE_EXECUTE_READWRITE, &op)) {
                    unsigned char patch[SETPOS_PROLO_LEN];
                    patch[0] = 0xE9;
                    int32_t rel = (int32_t)((uintptr_t)&detour_setpos - (sp_t + 5));
                    memcpy(patch + 1, &rel, 4);
                    for (int i = 5; i < SETPOS_PROLO_LEN; ++i) patch[i] = 0x90;
                    memcpy(sp, patch, SETPOS_PROLO_LEN);
                    VirtualProtect(sp, SETPOS_PROLO_LEN, op, &op);
                    FlushInstructionCache(GetCurrentProcess(), sp, SETPOS_PROLO_LEN);
                    OutputDebugStringA("[drhook] FUN_0050bd40 setpos clamp installed "
                                       "(mover-input, round 6i)");
                } else {
                    OutputDebugStringA("[drhook] VirtualProtect(FUN_0050bd40) failed — "
                                       "setpos clamp NOT installed");
                }
            }
        }
    }
    return 1;
}
