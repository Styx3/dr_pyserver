#ifndef DR_HOOK_H
#define DR_HOOK_H

/*
 * The DR client hook, delivered by the d3d9.dll drop-in proxy (d3d9_proxy.c).
 * Its DllMain calls dr_install_all(); the individual installers are exposed for
 * testing/selective use. See hook.c / combat_hook.c / inject_hook.c /
 * telemetry_client.c and ../bible.md §4/§6.
 */

/* Install the FUN_005dd900 HP-synch-compare bypass for client-simulated units.
 * Returns 1 on success, 0 on prologue mismatch / Win32 failure. */
int dr_install_synch_hook(void);

/* Neutralize FUN_006ff8f0 (relaunch-via-launcher). Superseded by
 * dr_install_launcher_token (insufficient alone — see hook.c). Kept for reference. */
int dr_install_launcher_bypass(void);

/* Inject `ran_from_launcher` into the command line (hook GetCommandLineA) so a
 * bare DungeonRunners.exe launch takes the normal token path. Call from DllMain. */
int dr_install_launcher_token(void);

/* Detour FUN_004f6580 (damage dispatch) to report kills over the telemetry
 * channel. Returns 1 on success, 0 on prologue mismatch / Win32 failure. */
int dr_install_combat_report_hook(void);

/* Install the per-frame pump hook (FUN_005d9e30) that drains server-driven
 * mob-attack injections on the game thread. Returns 1 on success, 0 on prologue
 * mismatch / Win32 failure. See inject_hook.c / docs/MOB_ATTACK_INJECTION.md. */
int dr_install_inject_hook(void);

/* Run all installers (launcher bypass, synch bypass, combat-report, injection). */
void dr_install_all(void);

/* Record an eid→Unit* observation into the injection cache. Called from the
 * synch detour (game thread) for every validated synched unit. */
void dr_inject_note_unit(void *unit);

/* Enqueue a server-driven mob→player attack (telemetry OP_MOB_ATTACK). Called
 * from the telemetry reader thread; applied on the game thread by the pump
 * drain. Eids are client-side u16 entity ids; damage_wire is HP ×256. */
void dr_inject_enqueue(unsigned int mob_eid, unsigned int avatar_eid,
                       unsigned int damage_wire, unsigned char element);

/* Drop all cached units + pending attacks (telemetry OP_ZONE_RESET). Called on
 * a zone change so no Unit* from the previous zone is dereferenced after the
 * avatar/mobs are freed and re-created. */
void dr_inject_reset(void);

/* The client killed a mob locally — drop the injection layer's pending attacks,
 * clamp, and cache for it and block-list its eid, so late server MOB_ATTACK /
 * MOB_CLAMP for the same mob applies nothing ("no damage after the mob is dead").
 * Called from the damage detour (combat_hook.c) on every lethal blow; a non-mob
 * victim (avatar/summon) is a safe no-op. */
void dr_inject_forget_mob(unsigned int eid);

/* Mark a mob as an aggroed follower to CLAMP to the stop-ring around the avatar
 * (telemetry OP_MOB_CLAMP, run-through fix — bible §14.6). Called from the
 * telemetry reader thread; applied on the game thread by the per-frame pump
 * drain, which rewrites the mob's world position (unit+0x90/+0x94) so the client
 * Follow action can't drive it through the player. Self-validated + freshness-
 * gated. ``ring_wire`` is the stop distance in Fixed32 (world units ×256). */
void dr_inject_set_clamp(unsigned int mob_eid, unsigned int avatar_eid,
                         unsigned int ring_wire);

/* Start the telemetry socket thread now (from DllMain), so the channel connects
 * at client launch — BEFORE the first fight — instead of lazily on the first
 * KILL. Required for the enroll+clamp model: the server clamps mobs the instant
 * they enroll (on the player's first attack), which is before any kill. */
void dr_telemetry_start(void);

/* Queue a KILL{victim,killer} record for the telemetry sender (combat_hook.c →
 * telemetry_client.c). Eids are the client-side u16 entity ids. */
void dr_telemetry_report_kill(unsigned int victim_eid, unsigned int killer_eid);

/* Queue a KILL_AT record: KILL plus the victim's death position (Fixed32 ×256,
 * entity +0x130/+0x134/+0x138) and the killer's level (entity +0x314). Lets the
 * server drop loot at the mob's real position and keep the character's level in
 * lockstep with the client's local self-leveling. */
void dr_telemetry_report_kill_at(unsigned int victim_eid, unsigned int killer_eid,
                                 int px, int py, int pz, unsigned short level);

/* Queue a KILL_AT_XP record: KILL_AT plus the killer's exact Experience —
 * progress-into-current-level, the PlayerState accumulator at entity +0x320
 * (AddExperience FUN_004f82f0 param_1[200]; deducted by the level threshold on
 * level-up). Lets the server adopt the client's true XP so the zone-transfer
 * Avatar re-send never clobbers locally-earned experience. Only meaningful when
 * the killer is the avatar; the server ignores it for owned-unit credits. */
void dr_telemetry_report_kill_at_xp(unsigned int victim_eid, unsigned int killer_eid,
                                    int px, int py, int pz, unsigned short level,
                                    unsigned int experience);

#endif /* DR_HOOK_H */
