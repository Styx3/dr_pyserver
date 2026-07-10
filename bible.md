# bible.md — The Dungeon Runners Sync Bible

**The single canonical answer to "how do the client and server stay in sync?"** —
catalogued by evidence tier, with the per-player / per-group zone-instancing design
that follows from it.

This file exists because the answer is the foundation for a desync-free server.
Everything else (combat, loot, grouping) is a corollary of the rules here. When a
later doc and this file disagree, **re-derive from the client and update both.**

> Companion: `docs/CLIENT_SERVER_MODEL.md` is the protocol-level reference (channels,
> framing, ComponentUpdate). This file is the **sync-and-determinism** reference and
> the **instancing design**. Read CLAUDE.md's source-of-truth hierarchy first.

---

## 0. How to read this — evidence tiers (NON-NEGOTIABLE)

The user directive, restated: **only client-derived data is truth.** Specifically:

| Tier | Source | Status |
|------|--------|--------|
| **T0 — GROUND TRUTH** | the client binary (Ghidra @ base `0x400000`, x64dbg live), WireMCP captures of the real client, extracted client content (`extracter/`) | **Truth.** Build load-bearing logic on this. |
| **DEV — ORIGINAL-DEV TESTIMONY** | first-hand recollection from an original DR server developer (2026-07-02 conversation — §15) | Authoritative for **design intent** (outranks all T3). ~18-year-old memory — verify specifics against T0 before shipping. |
| **T1 — DERIVED** | a logical conclusion that follows necessarily from T0 facts | Usable, but flag the inference. |
| **T2 — HYPOTHESIS** | plausible, consistent with T0, **not yet** confirmed against the client | Mark `# UNVERIFIED`. Do not ship load-bearing logic. |
| **T3 — FABRICATED / EMULATOR LORE** | C# (DR-Server / old UGS), Go (RainbowRunner), L2 notes, prior assumptions | **Reference only. Treat as wrong until a T0 source confirms it.** |

Every claim below carries a tag: **[T0]**, **[T1]**, **[T2]**, **[T3]**.
A claim "matches the C# server" is **[T3]** — that is *not* evidence of correctness
(proven 2026-06-04: the avatar HP-synch crash existed in the C# server itself).

Each **[T0]** claim names the binary address or capture so it can be re-checked.

---

## 1. The core answer, in one screen

The client and server **do not** keep a shared simulation clock and they **do not**
exchange ping/latency. The model is **deterministic dual-simulation from
server-provided data** (user model, 2026-06-12; consistent with all [T0] evidence):

1. The client **authorizes on the server**; the server **sends it all the state** —
   user level, exp, equipment, the mobs it spawned in the dungeon + their stats, the
   RNG seed. The server is **authoritative for this data**.
2. Once client and server data match, **the client computes ALL gameplay locally** —
   damage, hits, HP, mana — *driving itself from that server-provided data* with its
   built-in formulas and wall-clock-seeded RNG.
3. The server is **expected to emulate every action 1:1 on its own end** — same data,
   same formulas, same RNG — so that at check/validation time it produces the
   **identical** numbers the client arrived at, and can send them back unchanged.

The compare (mechanism 3 below) is the **gate that enforces the two simulations
stayed identical**. It is kept reproducible by three mechanisms, all paced by the
**client's own wall clock** (`timeGetTime()`):

1. **Time / pacing — the message-rate contract.** The client's world clock advances
   by *consuming entity-channel messages*. The server programs the timestep and
   cadence with a `0x0D` WorldInterval packet, and **must not exceed ~1 entity
   message per 133 ms sustained**, or the client fast-forwards the whole world at 3×.
   (§2) **[T0]**

2. **RNG — the MT19937 seed.** Combat rolls come from a **per-entity-manager** Mersenne
   Twister at `(entitymgr)+0x44`, which the client **seeds from its local `time(NULL)`**
   (wall-clock *seconds*) on every zone load — **not** `timeGetTime`, and **not**
   transmitted upstream (★corrected 2026-06-13 from live x64dbg; the old "timeGetTime sent
   every frame" claim was wrong — see §3). The server can **override** that seed by sending
   a `0x0C` **downstream** (client handler `0x5DA870`), which wins as the **last writer**
   when it lands after the client's `time(0)` reset; the server then seeds its mirror MT
   with the same value. **Live-proven viable** (our `0x0C` delivered `0xBEEFBEEF` into a
   client MT; the maze-layout seed is *already* synced this exact way). (§3) **[T0]**

3. **The consistency gate — the zero-tolerance HP synch compare.** When the server
   sends an entity's HP, the client compares it **exactly** against its locally
   computed value; mismatch = **fatal crash**. This is not something the server
   *games* — it is the gate the **server's 1:1 emulation must satisfy** by having
   independently computed the same number. The only skip is the **input-authority
   bit** for the avatar, whose human input the server can't predict before it
   arrives. (§4) **[T0]**

**There is no fourth mechanism.** No latency compensation, no reconciliation, no
rewind — divergence is not corrected, it crashes. (§5)

The practical consequence is **regime-dependent** (proven 2026-06-15, §6-LIVE.6/8):
for **shared / seed-generated state** (maze, spawns, mob stats — "Regime A") the server
reproduces the client bit-for-bit (same data, same `0x0C` seed, fixed-point) and the
compare passes by construction. For the **runtime combat sim of an entity the receiving
client simulates** (its own avatar; enrolled mobs — "Regime B") bit-exact reproduction is
**infeasible** — the combat MT is the shared entity-manager RNG advanced by frame-paced
mob-AI/proc draws the headless server cannot replay (§6-LIVE.6) — and is **not needed**:
the engine exempts those entities from the compare via the `+0x95` bit, so the server must
**not originate** their HP, only relay the owning client's self-report. Adopt-then-echo on
the client's *own* packet does not race; only a *server-originated* send on a report-less
packet does (the stale-MAX avatar crash, §6-LIVE.8). The full rule is §6.

---

## 2. Time & the message-rate contract  **[T0]**

### What sets the clock

The client's world simulation is pumped by `FUN_005d9e30` (per render frame). The
server's `0x0D` WorldInterval packet (`FUN_005da7d0` = processInterval) writes the
parameters into the client's clock engine:

```
0x0D <tickCount:u32> <0x21:u32> <0x03:u32> | <0x01:u32> <100:u16> <20:u16>
      └ overwrites client    └ timestep   └ consume ONE   └ PathManager::ReadBudget
        world tick (+0xa94)    = 33 ms       ch7 msg per      (FUN_004c3cc0)
                                             4 world ticks
```

The pump, in pseudo-C **[T0]** (`FUN_005d9e30`):

```c
elapsed = timeGetTime() - last;
if (pendingCh7Messages > 2)  elapsed *= 3;   // catch-up: world runs at 3×
accumulator += elapsed;
runWorldTicks(accumulator / 33ms);           // every 4th tick: consume ONE queued msg
```

### The contract

- **Sustained budget: ≤ 1 channel-7 message per 133 ms** (= 1 per 4 ticks @ 33 ms).
  This is the `0x0D` cadence itself. **[T0/T1]**
- Exceed it → inbound queue passes depth 2 → **entire world simulates at 3×** (mobs
  move/attack 2–3× speed, movement rubber-bands). Live-proven 2026-06-12. **[T0]**
- **Bursts are fine — but a HELD BUTTON is not a burst.** The depth-2 threshold
  absorbs short spikes (single clicks). A held attack/skill button is a *sustained*
  action stream, and since the `0x0D` cadence alone already saturates the budget,
  per-tick-flushed acks for it tip the backlog permanently: live 2026-07-02 —
  holding attack (even shift-attacking in place in TOWN) or a skill button ran the
  whole world at 3× (mobs faster, attack cadence boosted). **[T0 symptom / T1
  mechanism]** (NB: the same-day hypothesis that this 3× also explained the
  "mobs run through me when I attack" bug was DISPROVEN live — the run-through
  persisted after the ack fix; its real causes are the §14.6 send-model gaps.)
- Streaming producers (monster move/follow corrections, **and the combat action
  acks `0x50`/`0x51`/`0x52`/CancelAction** — anything the client can emit at
  held-button or retry rate) **must ride inside the per-4th-tick `0x0D` frame**
  (`conn.interval_message_queue`, drained by `build_world_interval_packet`) —
  never as standalone frames, never via the per-tick ack flush. One-shot acks that
  precede a zone change (portal/checkpoint/teleporter `0x06`, checkpoint-recall
  `0x52`) stay on the per-tick flush: the zone change clears the interval queue,
  and their click-rate is genuinely bursty. **[T1]** (this is our implementation
  of the [T0] contract; ack carrier moved 2026-07-02)

### Why "sync is tied to time" is true but subtle

Message **arrival timing IS world-time delivery.** A jittery `0x0D` cadence =
choppy movement (2026-06-09). On Windows the ~15.6 ms default timer resolution made
`asyncio.sleep(0.033)` jitter between 21–32 Hz; the fix is `timeBeginPeriod(1)` at
boot (Unity does this; plain asyncio does not). **[T0-adjacent / live-proven]**

---

## 3. RNG determinism & the seed model  **[T0]** ← ★rewritten 2026-06-13 from live x64dbg (prior "timeGetTime via upstream 0x0C" was WRONG)

### The generator is binary-exact

The client's combat RNG is **MT19937**, confirmed in the binary:

- `FUN_0044b1b0` = `init_genrand(seed)`: `mt[0]=seed; mt[i]=(mt[i-1]^(mt[i-1]>>30))*0x6c078965+i`. **[T0]**
- `FUN_0044b1f0` = generate/twist: matrix `0x9908b0df`, reload at index `0x270` (624),
  and **auto-seeds with `0x1105` (4357) if never seeded** (index hits `0x271`). **[T0]**
- Temper masks in the binary `(y<<7)&(0xff3a58ad<<7)` / `(y<<15)&(0xffffdf8c<<15)`
  reduce to canonical `0x9D2C5680` / `0xEFC60000`. **[T0]**

Our `drserver/combat/rng.py` matches all of these constants **byte-for-byte**,
including the `0x1105` lazy default. **[T0 — parity verified this session]**

### The seed is the client's local `time(NULL)`, set per entity-manager on zone load  **[T0 — live 2026-06-13]**

The combat/entity MT lives at **`(entitymgr)+0x44`**, one per `ClientEntityManager`. On
every zone load the entity-manager reset path seeds it from the client's **local
`time(NULL)`** (wall-clock *seconds*):

```
0x5DDF6E  lea edx,[edi+0x44]        ; &MT   (entitymgr = edi)
0x5DDF71  mov ecx,eax               ; seed  = time(NULL)
0x5DDF78  call FUN_0044b1b0         ; init_genrand(&MT, time())
          └ seed source = CRT time() @0x7220E5 (GetSystemTimeAsFileTime [0x82B234] → Unix seconds)
```

Live seeds captured were Unix timestamps for "now" (`0x6A2D9408`, `0x6A2D94C4`, …),
incrementing with real wall-clock between captures. A dungeon load builds **several**
managers (≈ one per room), each seeded from a fresh `time(0)` (caller `0x5DDF7D` every time).

**This corrects the prior claim.** The RNG seed is `time(0)` (seconds), **not** `timeGetTime`,
and the client does **not** transmit it upstream. `timeGetTime` (`[0x82B354]`, winmm) is read
in the *same* reset into `(entitymgr)+0xB18` — that is the **world-clock pacing base** (§2), a
different clock for a different purpose. The bible previously **fused** the two; they are separate.

### The server overrides the seed downstream — `0x0C` → `0x5DA870`  **[T0 — live-proven viable]**

The inbound `0x0C` handler `FUN_005da870` reads a u32 and calls `init_genrand((obj)+0x44, seed)`
— reseeding the **same** `+0x44` MT. Dispatch verified: `FUN_005da460` reads the opcode, indexes
table `0x5DA730` then jumps via `0x5DA6E0`; opcode `0x0C` → entry `0x07` → `0x5DA58D` → `call
0x5DA870` (and `0x0D` → `0x5DA7D0`, the working world-interval, identical mechanism). Because the
client's `time(0)` reset runs during load and our `0x0C` is sent only **after** the client signals
`13/6` ("zone loaded"), the server's seed is the **last writer**. **Proven 2026-06-13:** our
server's `0x0C` (`game_server.py:_send_zone_progression`, `MAZE_SEED = 0xBEEFBEEF`) was caught
writing `0xBEEFBEEF` into a client MT (`0x2F37D9DC = ebx+0x44`). **So the server CAN control the
client's combat seed** — Path A's foundation. (Earlier "it never fires" was a measurement error:
the watch was removed before the post-`13/6` packet arrived.)

### Proof-of-concept already live: the maze-layout seed  **[T0 — live 2026-06-13]**

A **separate** MT (seeded via caller `0x4CFD40`) drives maze / mob-placement generation. The
server sends its layout seed (`13/0x00`) and the client adopts it: the captured client reseed
`0x1D77A96E` **equals** the server log's `dungeon01_level01` maze seed `0x1D77A96E` (and
`dungeon00_level01` is `0x0A0CBD2D` on *every* warp). Result: the layout is **identical on every
re-entry** — server→client seed sharing + deterministic generation **already produces
byte-identical results on both sides.** This is the live proof that Path A's model works; combat
just needs the same treatment (right MT + matched formulas + draw order).

### RESOLVED 2026-06-13 (Ghidra): combat draws from the per-instance `+0x44` MT — the one `0x0C` seeds

Traced entirely in the decompiler (no client). The combat resolver **`FUN_00597e50`** reads its MT
pointer from `event+0x14` (each `genrand` call does `MOV ESI,[ESP+0x24]` = `event+0x14`). The melee
handler **`FUN_00594430`** builds that event with **`event+0x14 = *(X+0x88) + 0x44`** — a `+0x44` MT
member of a per-instance entity-manager object, i.e. the **same `+0x44`** the inbound `0x0C` handler
seeds (`init_genrand((obj)+0x44)`, above). Neither `FUN_00597e50` nor its 3 callers
(`FUN_005921c0` / `FUN_00594430` / `FUN_005960f0`) reference the global `0x932FF8`. **So combat is
NOT the global RNG — it is the seedable per-instance MT. Path A's seed-targeting is correct. [T0]**

> **★ LIVE-CONFIRMED 2026-06-14** (x64dbg, PID 3788): the server's `0x0C`→`0x5DA894` seeded MT
> `0x1CE490E4` (= instance `0x1CE490A0` + `0x44`) with `0xBEEFBEEF`; on the next swing (resolver
> `0x597E50`, caller `FUN_005921c0`), the event's `param_1[5]` **and** `esi` at *both* `genrand`
> sites all equalled `0x1CE490E4`. **The server controls the exact combat RNG stream — Path A's
> foundation is proven end-to-end.** **[T0 — live]**

- The **global `0x932FF8`** (~50 xrefs) is a separate general/effects RNG, *not* the combat stream. **[T0]**
- **RNG draw order per swing** (from `(instance)+0x44`): ① `genrand() % 0x6464` = hit/miss; ②
  `(genrand()>>8 & 0xff) % 100 + 1` = block/dodge gate; ③ a draw inside **`FUN_00598fd0`** = damage
  variance. Miss = 2 draws, hit = 3 (`FUN_00598a30`/`c10`/`b30` draw nothing). **[T0]**
- **Damage formula** (`FUN_00597e50`): weapon base (`FUN_00598a30`) + attacker stats, ×0.75, clamp
  `0x5a00` (90), mitigation (`FUN_00598c10`/`b30`), variance (`FUN_00598fd0`), crit ×`piVar8[0x46]/100`,
  floor `0x100` (1.0); hit-chance = `acc/(acc+def)` math. Port these bit-exact for Path A. **[T0]**
- **Last live-confirm before "done":** that the instance combat reads (`*(X+0x88)`) is the *same*
  object our `0x0C` targets — very likely (identical `+0x44`, same per-instance class) but only a live
  `esi` read at the verified boundary `0x59804b` proves it 100%. **[T1→verify]**
- Even with the right seed, the server must draw in the **same order** + **bit-exact formulas**.
  ★ **2026-06-14: the damage-MAGNITUDE chain is now LIVE-VALIDATED bit-exact** — a captured element-5
  player swing reproduced the client's applied damage `0x0E76` (14.46) exactly through
  `c10`→`b30`→`ed0`→variance draw (3 port bugs fixed; `drserver/combat/client_swing.py` +
  `tests/test_client_swing.py`; details in `docs/COMBAT_FORMULA.md` §3/§7). Still open before Path A is
  "done": hit/miss **threshold** (draw#1 vs acc/def), **block** curve, **crit**, **element-1**, and the
  server-side `CombatStats` mapping (§6a). The old "it killed a mob the client kept alive" divergence was
  the combined seed gap + these formula bugs; the magnitude bug class is now closed. **[T0]**

### Tooling caveat (x64dbg-automate)  **[T0 — this session]**

`SetBreakpointCondition` did **not** reliably filter (a `genrand` BP halted on a non-matching
`esi` despite the condition). Use **unconditional** breakpoints + manual register reads, or a
**bounded run-trace with `log_file`**, to filter by MT instance — not break-conditions.

**Only set software BPs at *verified* instruction boundaries.** A disassembly that *starts* at an
arbitrary mid-function address can be mis-aligned; an `INT3` placed there corrupts a real
instruction and crashes the client — 2026-06-13 a BP at `0x598400` (a mis-aligned address) trashed
`esp` → `EXCEPTION_ACCESS_VIOLATION`, unrecoverable. Verify boundaries by disassembling **forward
from a known function entry**, or anchor only at **CALL targets** (e.g. `0x598A30`, `0x4F6580`),
which are guaranteed-clean entries.

### What is NOT a modifier / NOT RNG-synced

Combat **HP is not a modifier** and is not server-rolled — it rides the synch trailer
(§4) and is owned by the simulating client. The `0x0C` is RNG **seed** sync only; do
not confuse it with HP. The old per-tick `0x0C` spam (reseeding 30×/s) was a bug — it
reseeded the client's combat RNG to garbage every frame. **[T0/historical]**

---

## 4. State agreement — the zero-tolerance HP synch compare  **[T0]**

### The trailer (EntitySynchInfo)

After **every** `0x35` ComponentUpdate, and on standalone `0x36` HP syncs, a trailer
rides the wire (`FUN_005dd840` reads it): **[T0]**

```
[flags:1]              always
[hp:u32]              present iff (flags & 0x02)    // stored at entity+0x18, +0x1c=4
```

flags bit `0x02` = HP present (confirmed: `FUN_005dd840` masks bit 25 of the
flag-byte-in-high-position = bit `0x02`). HP/MP are **×256 fixed-point** on the wire
("drfloat"). **[T0]**

### The compare and the crash

`0x35` (via `FUN_005db520`) and `0x36` (`FUN_005db6a0`) both:
1. resolve the component by id; **null → "Invalid ComponentID(%d)" → error code 9**
   at `+0xac0` (this is the infamous "Code 9"). **[T0]**
2. apply the trailer, then call **`FUN_005dd900`** — the **zero-tolerance compare**:

```c
if (flags != serverFlags || localHP != serverHP)
    → "Entity synch error detected" → fatal Avatar crash (exit 0xc000013a)
```

**No tolerance band.** The server is either exactly right or it must not compare. **[T0]**

### The bypass — local-input-authority bit (`activeAction +0x95 & 1`)

`FUN_005dd900` resolves the entity's controller (`FUN_004d3bf0`, cached at
`behavior+0xa4`) → active UnitAction (`controller[0x47]`) → if `*(action+0x95) & 1`
is set, **returns OK without comparing.** The input gate `FUN_0051cb50` early-outs on
the same bit. So a **locally-owned** entity ignores server HP entirely. **[T0]**

- The avatar in steady state has this bit set (live: `+0x95 = 0x11`) → a standing
  player never crashes on its own HP. **[T0]**
- Each new action's ctor sets `+0x95 = 0x10` (bit CLEAR) (`FUN_005fe000`); the bit is
  re-set by the controller-activate path (`behavior+0x84` vtbl `+0x58`) on an OFF→ON
  control toggle. **[T0]** This is why a fresh dungeon-warp avatar is briefly
  unprotected (the crash window) and why we re-assert control on the first inbound
  ch7 packets after zone entry. (Full fix history in `CLIENT_SERVER_MODEL.md §7.)

### ★ The crash is LIVE-CONFIRMED on an UNPATCHED client (2026-06-13)  **[T0]**

During a real fight the compare fired on the **avatar** (`[edi+0x80] = 0x1FE`) when a
server-authoritative mob hit the player: local/client HP `[edi+0x2f0] = 0x11AEE` (72,430)
vs server HP `[eax+0x18] = 0x12900` (75,520); flags both `0x02` (match) → HP differs →
mismatch path. The decision bytes were **unpatched** (`005dd9e4` still `JZ`; `005ddaf8`
still `XOR AL,AL`), and the server log corroborated from the other side (it killed the
mob; the client kept it alive). So the model in this section is **not** hypothetical — it
crashes real clients today. To skip the crash in a debugger, force `eip = 0x5dd95f` (the OK
return). This was the avatar-HP manifestation of the §3 RNG/formula split — a mob→player hit
computed differently on each side, landing in the avatar's `+0x95` bit-clear window.

### ⚠️ Patched-client caveat — prior "no crash" tests are INVALID HP-correctness evidence  **[T0]**

The user's previously-running client was **patched in the HP-sync-with-server path**
(confirmed 2026-06-12). The patch neutralizes the crash decision inside
`FUN_005dd900` so the compare always returns OK. Unpatched disassembly of the
decision (re-verified against the unpatched exe this session):

```
005dd956  TEST byte ptr [ESI+0x95],1   ; local-input-authority bit
005dd95d  JZ   005dd966                 ; bit clear → run compare ; bit set → bypass
005dd95f  MOV  AL,1 ; JMP epilogue       ; ← OK return (match or bypass)
005dd9ba  MOV  EBP,[EDI+0x2f0]           ; local HP = CurrentHPWire  (confirms +0x2F0)
005dd9dc  CMP  BL,[EAX+0x20]             ; local flags vs server flags
005dd9df  JNZ  005dd9ea                  ; flags differ → ERROR
005dd9e1  CMP  EBP,[EAX+0x18]            ; local HP vs server HP
005dd9e4  JZ   005dd95f                  ; HP equal → OK return
005dd9ea  …build "Entity synch error"…   ; mismatch path
005ddaf0  CALL 006f9960                  ; error logger
005ddaf8  XOR  AL,AL                      ; ← 0 return → caller crashes (0xc000013a)
```

The single crash-suppression patch sites are `005dd9e4` (force the HP-equal branch
unconditional → "HP always matches") or `005ddaf8` (`XOR AL,AL`→`MOV AL,1` → force OK
return). Diffing the exact patched byte needs the patched exe alongside this one.

**Consequence — this re-tiers our entire live-test history.** Every "no HP crash"
result recorded in memory/docs (the many `RE-TEST LIVE` / "no crash (client patched)"
notes) was obtained on a client with this compare **disabled**. Those results are
**[T3] for HP correctness** — they prove gameplay flowed, **not** that the server
sent correct HP. Against an **unpatched** client (i.e. real players) **any** HP
mismatch — including the heartbeat-vs-report race (§6 of CLIENT_SERVER_MODEL) — is a
hard crash. So:

- The **1:1 reproduction model (§6)** is load-bearing for real clients — adopt-and-echo
  is the workaround that races and crashes them. The `+0x95` authority bit is real but
  **proven not a server-controllable fix** (user's extended tests, 2026-06-12).
- All HP-path features must be re-validated against the **unpatched** client before
  being called done. "It didn't crash on my patched client" is not a pass.
- The `0x0C` seed bridge (§3) → exact server-side HP becomes more valuable: it is the
  path to HP the unpatched compare will actually accept.

### Authority table (who owns what, from one client's view)  **[T0]**

| Entity (from one client's view) | Control mode `+0xe5` | How its HP must reach this client | Compare runs? |
|--------|--------------------|---------|---------------|
| Its own avatar | 4 (local sim) | client computes; server replays its input | **skipped** via `+0x95` bit |
| Another player's avatar | 1 (display) | server must hold P2's **exact computed** value (reproduce, not adopt-and-race) | yes — must equal P2's value |
| Monster (enrolled) | 4 (local sim) | enrolling client computes; server reproduces it for everyone else | skipped on owner; reproduced for others |
| Monster (not enrolled) | 1 (display) | server computes (authoritative) | yes |

---

## 5. Latency / ping — the deliberate absence  **[T0 (negative result)]**

The user asked to "take latency/ping into account." **The protocol does not.**

- A binary string scan for `ping / latency / lag / roundtrip / servertime / timesync`
  returns **nothing** relevant (only TLS "Time Stamping", "Grouping", PvP-flag, and
  shadow-"Mapping" strings). There is **no RTT handshake, no clock-offset exchange,
  no NTP-style sync.** **[T0]**
- The only "time" in the loop is the **client's local `timeGetTime()`**, which drives
  (a) the world-clock pump cadence (§2) and (b) the per-frame MT reseed (§3).
- THCSockets `0x02` Ping frames exist at the transport layer, but they are **echo
  keepalives**, not latency measurement (`framing.py`: reply echoes the tail). **[T0]**

**Implication for us:** you cannot "compensate for ping." Sync survives latency
*because* gameplay is **client-computed** — the client never waits for the server to
resolve its own actions; the server reproduces them in parallel (§6). The server's only
timing obligation is the **message-rate contract (§2)**; honor that and latency is
irrelevant to correctness (only to how stale P2's view of P1 is). **Do not** add lockstep, input-delay, or rollback — none
exist in the client and adding server-side timing authority will fight the compare
(§4) and crash.

---

## 6. Authority model — reproduce shared state, relay simulated state  (corrected 2026-06-15 by §6-LIVE.6/8)

**The model is two regimes, split per-entity by the `+0x95` input-authority bit (§4).**
The thesis previously stated here — "the server runs the client's gameplay emulation 1:1
and holds data exactly equal to the client's simulation *for everything*" (user model,
2026-06-12) — was a **[T2] hypothesis**, and the live captures below **overturned it for
runtime combat.** Keep the half that is proven; drop the half that is not.

### Regime A — shared / seed-generated state: the server IS a deterministic lockstep peer  **[T0]**

State generated from a server-provided seed with no per-frame-timing dependency — maze
layout, what spawns where, mob stats, initial/max HP. The server reproduces it bit-for-bit
(same `0x0C` seed, fixed-point, same generation) so its value equals the client's **by
construction**; proven by the maze-layout seed round-tripping byte-identical on every
re-entry (§3). And for any entity a receiving client merely **displays** (control mode
`+0xe5`=1 — other players' avatars, non-enrolled mobs), the client does **not**
independently compute HP, so a server HP send matches by construction → the compare (§4)
**passes**. Here "reproduce, don't relay" is correct, and the reproduction modules
(`client_swing`, `monster_damage`, `monster_curves`…) belong.

### Regime B — runtime combat of an entity the receiving client SIMULATES: the server must NOT originate its HP

For an entity a client simulates (mode 4 — its own avatar; mobs it enrolled via `0x64`):

- **Bit-exact reproduction is infeasible.** **[T0 — §6-LIVE.6]** The combat MT (`entitymgr
  +0x44`, the one `0x0C` seeds) is the *shared* entity-manager RNG; between swings the client
  advances it by frame-paced mob-AI/wander/timer draws (`FUN_0052d680`), per-element resist
  rolls (`FUN_0050b660`), and procs. A headless server cannot reproduce the draw *position*
  (magnitude formulas are bit-exact both directions, §6-LIVE.5; the position is not). The
  original native server, also headless, could not have done this either — which is exactly
  why the engine exempts these entities from the compare via `+0x95`.
- **The avatar crash is a STALE-SEND race, not a repro gap.** **[T0 — §6-LIVE.8]** Caught
  unpatched: the server shipped avatar MAX (313.0) while the client had decremented to 303.7
  — it had simply not yet adopted the client's reported HP. The cure is to **never originate**
  an avatar HP; echo only the client's freshest self-report, which rides its own move/action
  packets (adopted at `movement.py:176` *before* the echo — that adopt-then-echo does **not**
  race; it ships exactly what the client just reported). Suppress every server-*originated*
  avatar HP send (periodic `0x36`, combat acks `0x50/0x51`, control-toggle `0x64`).

### The rule  **[T1 — derived from §4 + §6-LIVE.6/8]**

The side that *simulates* an entity owns its HP; the other side only mirrors. **The server
originates a compared HP only for entities it itself simulates and the receiving client
displays (Regime A).** For Regime-B entities it is the **authority broker**: it adopts the
owning client's report and relays it to clients that *display* the entity (P1 enrolls a mob →
reports HP → server mirrors to P2, for whom the mob is Regime A → P2's compare passes). It
never sends a Regime-B owner a value that owner did not compute.

This refines, not contradicts, the lockstep classification (§6b): DR is **lockstep for
Regime-A state, authoritative-ownership for Regime-B state**, with `+0x95` marking the seam.
The old blanket warning "adopt-and-echo always races and crashes" was over-broad — it races
only for a **server-originated** send on a packet carrying no fresh report (the stale-MAX
bug); adopt-then-echo on the client's own packet is safe and is what native did.

### 6a. The reconstruction roadmap (how to actually crack sync)

> **Scope (corrected 2026-06-15):** this roadmap's reproduction goal applies to **Regime A**
> (§6) — server-authoritative / displayed entities + server-side kill detection — **not** to
> the avatar or enrolled-mob compare, which §6-LIVE.6 proved unreachable by replay. The seed
> bridge and bit-exact formulas below remain necessary for Regime A; they are **not** the
> avatar fix (that is §6-LIVE.8: never originate Regime-B HP).

What must be true — and where each piece stands:

- **Same data** — ✅ the server has it: the rebuilt **client-faithful** stat tables
  (items, weapons, armor, creatures) + spawn tables. *This is why the DB-rebuild work
  is load-bearing for sync, not just display.*
- **Same RNG** — MT19937 is byte-exact (§3); the `0x0C` `timeGetTime` seed must be read
  off the wire and adopted into the server's mirror MT. **[T2 — needs WireMCP capture]**
- **Same formulas** — damage / hit / HP- & mana-derivation must be **bit-identical to
  the client**. Our `compute_swing` / `monster_damage` / `damage_computer` /
  `swing_stats` are ported from **C# (T3)** → they must be re-derived/verified against
  the client's own combat functions before they can satisfy the compare.
- **Same input order/timing** — the server replays the client's `0x50` swings / movement
  in arrival order, advancing the same MT positions (`rng_position_inferrer` scaffold).

Steps, in order:

1. **Seed bridge — PROVEN (2026-06-13), not "wire-unconfirmed".** The server's `0x0C`
   reaches the client (`0x5DA870`) and reseeds the per-manager `+0x44` MT as the last writer
   (after the client's `time(0)` reset); the maze-layout seed already round-trips identically
   (§3). Remaining sub-step: **confirm which MT the synch-compared combat damage actually draws
   from** — the per-manager `+0x44` we can seed, vs the global `0x932FF8` — and seed *that* one,
   mirroring the same value server-side.
2. **RE the client's combat math to bit-exact** — decompile the damage path (the client
   functions C# annotated as `Damage::apply@0x4F6580` etc.) + the max-HP/mana derivation;
   replace the C#-ported formulas with client-true ones. Re-tier T3 → T0 per formula.
3. **Enumerate the client's MT draw order** per frame (wander, hit, damage, crit…) so
   the server consumes the stream identically.
4. **Replay client inputs deterministically** into the mirror.
5. **Validate against the UNPATCHED client** (§4 caveat) — the only honest pass is "no
   synch crash with the compare live."

Build **Regime-A** state (displayed-entity HP, spawns, layout, kill detection) as
**"reproduce, don't relay"** — it composes. Build **Regime-B** state (an entity's HP for the
client that simulates it) as **"relay the owner, never originate"** — originating it crashes
real clients (§6-LIVE.6/8). Do not apply Regime-A reproduction logic to the avatar's own
combat HP; that is the mistake behind every HP workaround in the tree.

### 6-LIVE. Captured desync (x64dbg, 2026-06-12)  **[T0 — live]**

First live capture of the combat desync — caught at the `FUN_005dd900` mismatch path
(`bp 0x5dd9ea`) when a **skill was cast on a mob**, against the **C# server + unpatched
client**:

| field | value |
|---|---|
| entity | **mob** eid `0xC356` (not avatar `0x1FE`) |
| action `+0x95` | `0x10` → input-authority bit **CLEAR** → compare **ran** (not bypassed) |
| flags | local `0x02` == server `0x02` (match) |
| **client local HP** | `[edi+0x2f0]` = `0x3C00` = **60.0** |
| **server HP** | `[eax+0x18]` = `0x3700` = **55.0** |
| **Δ** | **exactly 5.0** (1280 = 5×256) |
| max HP (tentative) | `+0x2e0` cluster = `0x64` ≈ 100 |

Proves on real hardware: (1) combat is genuinely **dual-simulated** — both sides held a
computed mob HP; (2) they **diverged** — the server applied 5.0 *more* damage than the
client → the two sims are **not** bit-identical; (3) the compare is zero-tolerance and
fired because the mob's action wasn't input-authoritative; (4) **C# does not reproduce
the client** (55 vs the client's 60) → C# is not a sync reference, confirming §0. Open
diagnostic: is the 5.0 an **RNG-roll** gap or a **data/formula** gap? → next capture is
the client's *local* skill-damage computation.

### 6-LIVE.2. Player→mob HP divergence over a full fight (server log, 2026-06-14)  **[T0 — live]**

After the player-stat fix removed the *over*-damage, a real fight shows the Python server now
**under-damages** — the inverse desync, same root cause. The player killed mob `563` on the **client**,
but the server's swing-replay never registered the kill:

| field | value |
|---|---|
| mob | eid `563` 'Whisker Broodling', maxHP `29184` (= 114.0) |
| client | **killed it** (dead client-side; player switched to eid `558` 'Warg Pup') |
| server replay HP | only ground down to `15927/29184` (≈ 62, still "alive") before target switch |
| server per-swing (`[CLIENT-SWING] dmg_wire`) | `4216, 3583, 5458, 4551` (≈ 14–21 HP, avg ≈ 17) |
| implied client damage | ≈ 25 HP/swing (114 HP killed in ~4–5 swings) |
| weapon | 1HMace `items.pal.1hmacepal.normal001` (`wpnF32=203 volF32=85`) — **NOT** the staff that was the only bit-exact validation |

**Ground truths established (all [T0] unless noted):**

1. **The server's swing-replay mob HP diverges from the client's real mob HP over a fight.** With the
   corrected player stats the server now *under*-damages (≈17 vs client ≈25 HP/swing) and **never
   registers the kill the client already made.** (Pre-fix it *over*-damaged and killed mobs the client
   kept alive — §3 memory.) Both directions are the **same** failure: *the server's reproduction ≠ the
   client's*. "Player→mob is essentially working" is therefore **FALSE in practice** — it diverges every
   fight.

2. **`client_swing` is NOT bit-exact for arbitrary weapons/elements.** The *only* bit-exact validation
   was the staff (`wpnF32=154/64`, element-5, applied `0x0E76`). This fight used a 1HMace (`203/85`)
   whose damage was never validated. **Per-weapon / per-element validation is required, not assumed.**

3. **The server MT is deterministic but NOT proven in lockstep with the client.** The server's raw `r1`
   draws **recur identically across sessions** (`0x4a09187d`, `0x0fe3daa3` appear in *both* the 14:46 and
   22:41 runs ⇒ seeded `0xBEEFBEEF` from the same start). Whether the **client's** combat MT is actually
   drawing from `0xBEEFBEEF` at the same per-swing offset is **UNPROVEN** — the faster client kill means
   either the client rolls a different stream (MT position not aligned) **or** the per-swing magnitude
   range itself is wrong for this weapon. The log can't separate the two; **both must be closed.** [T0/T1]

4. **The client is packet-blind for mob HP** (never reports it upstream), so the server's mob HP is
   **always its own replay and is never corrected.** When the replay diverges, the server has **no signal
   to learn the truth** — replay-based kill detection inherently mis-times every kill whose damage repro
   isn't bit-exact.

5. **Under-damage is a known CRASH vector, not cosmetic.** Server thinks the mob is alive → no
   kill/loot/despawn → the client (which killed it) **purges the corpse** → the server's continued
   `0x65`/`0x35` updates then reference a **client-destroyed** entity → "Invalid ComponentID / Code 9"
   crash (see `project_combat_desync_crash`). So this divergence is dangerous, not just wrong-looking.

**Consequence for the model.** §6 "reproduce, don't relay" requires **both** (a) bit-exact per-weapon /
per-element formulas **and** (b) genuine MT lockstep (the server consuming the client's *exact* draw
sequence, including mob swings). **Neither is fully achieved.** Until both hold, the server's mob HP
drifts from the client's every fight — and the same gap, on the avatar, is the unpatched-client crash.

### 6-LIVE.3. PLAYER CombatStats captured + the AR order-of-operations bug (x64dbg, 2026-06-15)  **[T0 — live]**

First **full** dump of the live player CombatStats array (the bit-exact target the server must reproduce).
Caught a clean avatar swing at resolver `0x597E50`, `attacker=event[4]`=`0x26C65728` (eid `0x1FE`), dumped
`unit+0xC8..+0x320`. Full per-slot table + addresses → `docs/COMBAT_FORMULA.md` **§6g**. The decisive find:

| stat | **client (live)** | server log (`[SWING-STATS]`, same swing) |
|---|---|---|
| AGILITY (enum2) | **5** | 15 |
| ATTACK_RATING `+0xF0` | **70** (= 14 × **5**) | 210 (= 14 × **15**) |
| MELEE_DAMAGE_BONUS `+0x180` | **11** (= ⌊2.3364 × **5**⌋) | 35 (= ⌊2.3364 × **15**⌋) |

The player's primary attributes are **base 10 each + a redistribution passive** `{STR−5, AGI−5, END+5,
INT+5}` → live STR/AGI/END/INT = **5/5/15/15**. The client derives AR/b30/HP from the **post-modifier**
attributes (AGI=5); the server derives them from the **pre-modifier** AGI (15) → **3× over-accuracy**.
**This is an order-of-operations bug, not missing data:** the server's `modifier_aggregator` *already*
emits `{1:-5,2:-5,3:+5,4:+5}` (live-confirmed by the 5/5/15/15 slots), but the AGI×14 derivation runs
**before** those primary-attribute deltas are folded. The `_PER_AGILITY` mod slots (enum80/86) are **0**
live, so the ×14 / ×2.3364 rates are **avatar-base constants**, not per-character mod data. ✅ **FIXED
2026-06-15 (code):** `swing_stats._final_combat_attributes` folds the primary-attr modifiers (sourced from
`modifier_aggregator`, which reads the **actual tray** — so the Mage −5 lands despite the `conn.class_name`
"Fighter" mismatch) into `base 10 + allocated` **before** deriving AR/b30; reproduces the live §6g target
**AR=70, b30=11** (existing suite 868 green; no new test pending live validation). The earlier §6e "exclude
the +10 base" model was wrong (it collapses to AR 0 on a negative redistribution). # UNVERIFIED LIVE —
re-test against the unpatched client, THEN pin a regression test.

Capture 3 (same swing): live `Damage::apply +0x38` = **3092 (12.08 HP)** — *inside* the server's dmg_wire
spread (2599/2715/3415/3660) but equal to **none** → the residual gap is **MT draw-position**, not
magnitude (the §6e staff variance range is already bit-exact). Genuine MT lockstep (§6-LIVE.2 pt 3) remains
the open item for per-swing equality.

### 6-LIVE.4. Mob-death model: packet-blind CONFIRMED + the avatar-HP desync caught live (x64dbg, 2026-06-15)  **[T0 — live]**

**(a) The client is packet-blind for mob HP/death — re-confirmed.** `Damage::apply` (`FUN_004f6580`,
the applied-damage entry) makes **no network send**: it only dispatches local damage events
(`FUN_004f68f0` with ids `0x1f4..0x1f8`) to the attacker (`[edi+0x2C]`) and defender (`[edi+0x34]`) event
handlers; the HP subtraction happens **inside the defender's handler**, purely local. No upstream report
of mob HP or mob death exists on this path. Corroborated behaviorally: the **server's own `REPLAY-DIAG`
exists and runs** — the server grinds mob HP by replaying the client's `0x50` swings precisely *because*
it is never told the mob's HP. **Consequence: replay-based kill detection is the ONLY option for mob
death; it is viable ONLY with bit-exact per-element damage + MT lockstep (neither yet holds → mis-timed
kills, §6-LIVE.2).**

**(b) Live mob-HP trajectory (client, player→mob)** — mob 563 'Whisker Broodling', maxHP 114, read at
`defender+0x2F0` on each `Damage::apply`:

| hit | mob HP before (`+0x2F0`) | applied (`+0x38`) | element |
|---|---|---|---|
| 1 | **114.0** (29184) | 11.95 (3058) | 5 (player staff) |
| 2 | **102.05** (26126) | 14.39 (3684) | 5 |

The client decrements per-swing with variance (11.95, 14.39 — same el-5 staff distribution as Capture 3).
The server's `REPLAY-DIAG` for the *same* mob lands different per-swing numbers (MT-position) → cumulative
drift → the server's mob never reaches 0 when the client's does.

**(c) ★ The avatar-HP desync caught live (mob→player) — the crux the user reported ("on mob damage player
gets desync").** A `Damage::apply` with **attacker = mob 563** (`0x28238AA0`), **defender = avatar**
(eid `0x1FE`): the mob dealt **8.91 HP (2281, element 1 / physical)** to the avatar, whose local HP was
**313.0** → client-local result **304.09**. The server simulates this *same* mob hit with its **own** mob
stats (AR 60, dmg_mod −50) + avatar defense (DR 87) + its own RNG draw + the element-1 formula, arrives at
a **different** avatar HP, and sends it downstream → the zero-tolerance compare (`FUN_005dd900`, §4) fires
→ **desync/crash**. So the avatar crash and the "mob survives on server" bug are the **same root cause**
seen from the two damage directions: the server does not reproduce the client's combat bit-exactly. **The
server must reproduce mob→player element-1 damage (8.91 here) exactly to stop the avatar desync.**

**(d) The avatar unit REALLOCATES across a desync/reload.** Between Capture 1 and this kill the avatar's
unit pointer changed (`0x26C65728` → `0x262DD818`) while **eid `0x1FE` stayed stable** (and maxHP shifted
297→~313). **The server must key the avatar by eid, never by a cached unit pointer**, and must not assume
CombatStats are stable across a reload.

### 6-LIVE.5. ★ Element-1 mob→player damage RESOLVED — formula == element-5, gap is INPUTS (x64dbg, 2026-06-15)  **[T0 — live, PID 15184]**

The avatar-desync path of §6-LIVE.4c, fully traced. Two element-1 physical hits (Warg mobs eid 558/563 →
avatar `0x1FE`) captured at `Damage::apply` + stepped through the magnitude chain (`c10`→`b30`→`ed0`→draw).
Live: DMG_MOD `+0x100`=−50 → **mit 50%** (halves dmg); b30 0; weapon Wv `+0xEC`=256 / Ws `+0xF0`=128;
**hi_flag `event[6]`=15**; variance range **lo `0x400` (4.0) / hi `0xB00` (11.0)**; applied 4.81 & 9.06 HP.

**★ THE RESULT: element-1 is NOT a formula gap.** Replaying the live inputs through the *existing*
`drserver/combat/client_swing.py` `compute_swing` reproduces every step **bit-exact**. Element-1 uses the
**same** magnitude formula as the el-5-validated path (§3); the element only switches which **defender
per-element resist** the mitigation reads (`+0x18C/+0x190` for el-1) — 0 on this avatar. So the server
already has the math for the avatar-desync fix. The remaining gaps are the **input-sourcing** ones (the
stat BUILDER, §6a item "same data"), NOT the formula:
- **`hi_flag`** = `(u16)[manipulator+0x88]` (caller `FUN_005921c0`@`0x5921FD`; manipulator vtbl `0x893490`,
  weapon at `+0x5C`). Live 15 (mob) vs 10 (player staff = weapon damage level). Computed at manipulator
  construction → needs a manipulator-init write trace to pin its source.
- **Mob `+0x100` DMG_MOD** (−50 here, −40 in the §6-LIVE-prior capture) and the **mob weapon Wv/Ws** —
  the same loader-input gap flagged in `docs/COMBAT_FORMULA.md` §6f.

Full capture table + event-struct build in `docs/COMBAT_FORMULA.md` §6i. The cross-cutting blocker is still
**MT draw-position lockstep** (§6-LIVE.3 Capture 3): magnitude is now proven bit-exact in BOTH directions
(player→mob el-5, mob→player el-1); the open item is the per-swing MT draw offset — **now resolved, §6-LIVE.6.**

### 6-LIVE.6. ★★ The per-instance "combat" MT is the SHARED entity-manager RNG — draw-position lockstep by swing-replay is INFEASIBLE (x64dbg, 2026-06-15)  **[T0 — live, PID 15184]**

Direct measurement of *every* consumer of the per-instance combat MT (`entitymgr+0x44`), the stream the
server seeds via `0x0C` and that the swing variance draws from. Method: from `genrand` (`FUN_0044b1f0`),
the **index counter `mti` is at `MT+0x10`** (incremented every draw); a **hardware write-watch on `mti`**
(`0x22914DB4` for the live instance `0x22914D60`) fires on **every draw from that one MT instance and no
other** (the global `0x932FF8` and other instances are filtered out). The return address at `[esp+8]`
(genrand's frame; `[esp+4]`=`0x9908b0df` confirms it) gives each draw's caller. Six consecutive draws:

| # | caller | function | what it draws |
|---|--------|----------|---------------|
| 1 | `0x598050` | swing resolver | hit/miss (`% 0x6464`) |
| 2 | `0x598138` | swing resolver | block gate (`% 100`) |
| 3 | `0x599016` | swing resolver | damage variance |
| 4 | `0x50B8E7` | **`FUN_0050b660`** | per-element **resist/avoidance** roll (`% 0x6400`), switches on damage-type 0–7 reading defender `*_DAMAGE_RESIST` slots |
| 5 | `0x50C204` | creature-combat | a **proc** roll (`% 100`) |
| 6 | `0x52D6D4` | **`FUN_0052d680`** | generic **random-in-range** `(genrand % ((hi-lo)>>8))*0x100 + lo` (bounds `+0xb4/+0xb8`) — mob **AI/behaviour/wander/timer** |

All six wrote `mti` of the **same** instance (`esi`=`0x22914DA4` every time) → all draw from the one shared
MT. The player and all mobs in the room share it (one `ClientEntityManager` per room, §3). **Crucially,
fires #4–6 occurred with NO swing-resolver entry between them** (the resolver BP `0x597E50` did not fire) —
i.e. these draws happen **between** swings.

**★ Conclusion (this resolves §6-LIVE.3 Capture 3 "the gap is draw-position, not magnitude"): the
`entitymgr+0x44` MT is NOT a swing-private stream — it is the shared entity-manager RNG consumed by combat
resolution (hit/block/variance), per-hit combat sub-rolls (resist `FUN_0050b660`, procs), AND creature
AI/behaviour (`FUN_0052d680` random-in-range: wander, timers, decisions). Between any two swings the client
advances this MT by an unpredictable number of AI/proc/resist draws.** **[T0 for the consumers; T1 for the
infeasibility inference below.]**

Therefore **the server cannot achieve MT draw-position lockstep by replaying swings** (even all swings, both
directions). To keep the variance draw at the client's position it would have to reproduce *every* consumer
in exact order — including mob AI/wander/timer rolls (`FUN_0052d680`) that depend on client-local per-frame
state and timing the headless server does not simulate. This is why the seed bridge + bit-exact per-swing
formulas (both now proven) still do **not** yield matching HP over a fight: the **variance draw lands at a
drifting MT position**. The magnitude formulas are correct; the position is unrecoverable by replay.

**Consequence for the model (§6).** "Reproduce, don't relay" for HP requires reproducing the client's
*exact* damage, which needs the variance draw at the client's MT position — unrecoverable for any entity
whose HP the server must send to a client that is *displaying* (not simulating) it. This is a **fundamental
obstacle**, not a porting gap. Re-examine the authority model: for a solo player the only `FUN_005dd900`
compare that can fire is on the **avatar during its `+0x95` authority-bit-clear window** (mob→player hits,
§6-LIVE.4c). The productive paths are therefore (a) **keep the avatar's input-authority bit set** so its HP
is never compared (bible §4 — previously "not server-controllable", but re-verify given this), or (b) make
the server's avatar-HP **conservative/biased so it never *exceeds* the client's** (a mismatch still crashes,
so this only helps if the compare can be avoided) — neither is "reproduce the roll". The seed/`0x0C` bridge
remains necessary but is **not sufficient**. Open: enumerate which entities are client-simulated vs
server-displayed per scenario (solo/group/PvP) to scope where the compare actually fires. # T1 — re-test.

### 6-LIVE.7. Authority scoping — the compare only crashes SIMULATED entities; the server's lever is "don't assert HP for what the receiver simulates" (2026-06-15)  **[T1 — derived from §4 + §6-LIVE.6 + code review]**

Scoping where `FUN_005dd900` actually fires (task from §6-LIVE.6). The compare reads the entity's **local**
HP (`+0x2F0`) and compares to the server-sent HP. The decisive split:

- **DISPLAYED entities** (control mode `+0xe5`=1: other players' avatars, non-enrolled mobs): the client
  does **not** independently compute their HP — its local value is just whatever the server last set. So a
  server HP send **matches by construction** → these **never crash**, no authority bit needed. The server
  is correctly authoritative here.
- **SIMULATED entities** (mode 4: the client's own avatar; mobs it enrolled via `0x64`): the client
  computes their HP **independently** → it can diverge from the server's → the compare would crash, and is
  avoided **only** by the `+0x95` authority bit (set in steady state, CLEAR for the action-transition
  window). The crash is a server HP send landing in that window with a value ≠ the client's computed HP.

**For SOLO play the simulated set = the avatar + every mob the player is fighting (all enrolled).** Both
crash-classes are real: (a) **avatar** — the server echoes the client's last-reported HP (`synch_hp`/
`adopt_client_avatar_hp`), residual post-damage race; the trailer **cannot** be suppressed (avatar is an
HP unit → `flags=0x00` mismatches local `0x02` and crashes the *other* way — `net/component_update.py`
`write_synch_none` + `movement.py:75-91` already document this). (b) **enrolled mobs** — the server is
packet-blind for mob HP (§6-LIVE.4a) AND can't reproduce it (§6-LIVE.6), so its mob HP diverges; sending it
(`hp_broadcast.broadcast_hp_sync`, `0x65` corrections) during the mob's bit-clear window crashes the owner
(the §6-LIVE.2 capture: mob action `+0x95`=0x10, compare ran) and also drives the Code-9 corpse-purge
(`project_combat_desync_crash`).

**★ The server-side lever: never assert authoritative HP for an entity the RECEIVING client simulates.**
- **Enrolled mobs** → in solo, **suppress mob-HP updates to the client that owns/simulates the mob** (it
  computes its own; the server's divergent value can only crash it). Send mob HP only to clients
  **displaying** the mob (group members who didn't enroll it). This removes the enrolled-mob crash + the
  corpse-purge class without any RNG reproduction. **[actionable]**
- **Avatar** → unavoidably must carry `0x02`+HP; keep echoing the client's freshest reported value and
  never send a *spontaneous* avatar trailer (only ride the client's own packets, which carry fresh HP —
  `movement.py` already adopts-before-echo). The residual first-hit race is **authority-bit-bound**, proven
  not server-controllable (2026-06-12) → for unpatched clients it needs the client patch
  (`scripts/patch_client_synch_crash.py`). **No server-only fix exists for the avatar combat-damage race.**

**Net:** "make HP sync work with the server" is achievable for everything the server is *authoritative* over
(displayed entities, and not asserting HP for simulated ones), but the **avatar's own combat-damage HP is
fundamentally client-owned** — the honest server posture is to not contradict it, not to reproduce it.

### 6-LIVE.8. ★★ The avatar desync is a STALE-MAX RACE, not a damage-repro problem — the fix is tractable (x64dbg, 2026-06-15)  **[T0 — live, PID 15184]**

Caught the avatar crash live at the mismatch path (`bp 0x5DD9EA`, UNPATCHED client — `0x5DD9E4 JZ` +
`0x5DDAF8 XOR AL,AL` intact) the instant a mob hit the avatar:

| field | value |
|---|---|
| entity (`[edi+0x80]`) | **`0x1FE` avatar** |
| **client/local HP** (`ebp`=`[edi+0x2F0]`) | `0x12FBA` = **303.7** (took ~9.3 from the mob) |
| **server HP** (`[eax+0x18]`) | `0x13900` = **313.0 = the avatar MAX** |
| flags | local `0x02` == server `0x02` |
| authority bit (`[esi+0x95]`) | `0x10` → **bit clear → compare ran** |

**The server shipped the avatar's MAX HP while the client had decremented to 303.7.** It is **NOT** a
miscomputed-damage divergence (§6-LIVE.4c's framing as a server re-sim was wrong for the default config) —
the server simply **hadn't adopted the client's lower HP yet** and shipped the un-decremented level-max
(`_heartbeat_hp` returns `conn.hp_wire`=MAX when `client_hp_wire is None`).

**★ This REFRAMES the avatar problem as tractable and DECOUPLES it from §6-LIVE.6.** The server does **not**
need to reproduce the mob's damage (the MT-lockstep-infeasible path) — it only needs to **never ship an
avatar HP the client didn't compute**. The client *reports* its HP in the trailing EntitySynchInfo on its
move/action packets (the handler already adopts it, `movement.py:176`), so the server *can* track it; the
crash is purely the **gap**: a server send shipping stale MAX *before* the client's post-hit report is
adopted. The captured value being exactly MAX (not some other wrong number) confirms the shipper used
un-adopted `hp_wire` — i.e. a **combat-action ack** (`0x50`/`0x51`, `_heartbeat_hp` with `client_hp_wire`
None), **not** the move echo (which adopts the packet's fresh trailer first → ships the client's value).

**Fix (server-only, no client patch, no RNG repro):** suppress the avatar HP trailer on the **combat-action
acks** in combat zones — exactly what the existing **`DR_NO_HP_HEARTBEAT=1`** flag already does
(`movement.py:322-334`, gates the `0x50`/`0x51` ack HP). The ack is not load-bearing (the client is
authoritative for its own attack). **To validate: revert the client patch, run the server with
`DR_NO_HP_HEARTBEAT=1`, fight a mob — the compare should stop firing on the avatar.** If a residual crash
remains, the move echo / another owner send is also shipping stale HP and needs the same gate. Candidate to
promote the flag to default-on (or always-on in non-town zones) once live-validated. # T1 — validate live.

> Live aid applied this session: `0x5DDAF8` patched `XOR AL,AL`→`MOV AL,1` in the running client (debugger
> memory, reverts on restart) so the user can fight for research while the server fix is validated.

### 6-LIVE.9. ★ Residual mob→player crash CONFIRMED — it is a stale-PREVIOUS-report (round-trip echo) race, NOT stale-MAX (x64dbg, 2026-06-16)  **[T0 — live, PID 9832]**

§6-LIVE.8 predicted "if a residual crash remains, the move echo / another owner send is also shipping stale
HP." It does. Caught live at `bp 0x5DD9EA` (UNPATCHED — `0x5DD9E4 JZ` + `0x5DDAF8 XOR AL,AL` intact) the
instant a mob hit the avatar, with `suppress_originated_avatar_hp` **already default-ON** in a dungeon (so the
`0x50`/`0x51`/`0x64`/self-cast originated sends were NOT the culprit):

| field | value |
|---|---|
| entity (`[edi+0x80]`) | **`0x1FE` avatar** |
| client/local HP (`ebp`=`[edi+0x2F0]`) | `0xC565` = **197.39** |
| server HP (`[eax+0x18]`, `eax`=`[esp+0x84C]`) | `0xCB00` = **203.0** |
| flags | local `0x02` == server `0x02` (**match** — HP-only mismatch) |
| authority bit (`[esi+0x95]`) | `0x10` → bit clear → compare ran |
| Δ | server **+5.6 HP HIGHER** than client |

**The decisive difference from §6-LIVE.8:** the server value `203.0` is an **intermediate, clean round number**,
NOT the avatar MAX (this char's max ≈ 313 per §6-LIVE.8; local is 197.39, well below). `203.0` is a value the
server **already adopted from an earlier client self-report** and then **echoed back stale**. So
`suppress_originated_avatar_hp` (which targets the *un-adopted* stale-MAX `_heartbeat_hp`) cannot fix this — the
offending send ships `_heartbeat_hp` = the *last-adopted* client value, almost certainly the **move echo
(`0x65`)**, which is deliberately **not** suppressed (load-bearing for movement; the regime-B ack fix proved
dropping it freezes the player).

**The mechanism is round-trip staleness, intrinsic to echoing avatar HP at all:**
1. Client trailer reports `203.0` → server adopts `client_hp_wire=203.0` (`movement.py:176`).
2. Server echoes `203.0` on the move ack (`_heartbeat_hp` → clamped to 203.0).
3. In flight, a mob hits the avatar for ~5.6 (client local sim) → local HP now `197.39`.
4. Echo lands while the avatar's active action is the hit-reaction (`+0x95` clear) → compare runs →
   `203.0 ≠ 197.39` → crash.

**Consequence:** "never *originate* Regime-B HP" must strengthen to "**never *assert* avatar HP on owner sends at
all**." Any HP the server puts on the wire for the avatar is stale by the round-trip + ongoing client sim,
however fresh the adopt was. Two candidate fixes (server-only, no client patch):
- **(a) NONE trailer on owner sends** — ship a flags-only `0x00` EntitySynchInfo (`write_synch_none()`) for the
  avatar on the move echo + all owner sends, so the client never reads an HP to compare. ⚠️ RISK: the compare
  **forces** local flags to `0x02` (`0x5DD9C0 MOV BL,0x02`), so a server record with flags `0x00` trips the
  **flags** branch (`0x5DD9DF JNZ 0x5DD9EA`) and crashes too — UNLESS omitting the avatar's synch record
  *entirely* (no trailer) skips `FUN_005dd900` for it. **Verify live before coding:** does the move echo's
  EntitySynchInfo presence gate the compare, or does `FUN_004FA200` @`0x5DD9B1` (the `test al,al` guarding the
  local HP+flags load at `0x5DD9BA`)?
- **(b) keep `+0x95` set through combat** — if the avatar's input-authority bit stays set during the hit window
  the compare is bypassed (`0x5DD956 TEST [ESI+0x95],1`). §4 marked this "not server-controllable"; re-verify,
  since it is the clean exit.

Next: trace which send shipped `203.0` (correlate server-log timestamp with move-ack vs another owner send),
then pick (a)/(b). [T0 capture; T1 fix]

**★ RE FOLLOW-UP (2026-06-16, Ghidra static — the trailer-presence gate is RESOLVED; candidate (a) DISPROVEN).**
Decompiled the compare (`FUN_005dd900`) and all three callers. The synch trailer is **mandatory** in BOTH
synch-bearing entity-channel sub-messages — dispatcher `FUN_005da460` (`ClientEntityManager::processMessage`,
switches a sub-type byte): **case 3** = `processEntityUpdate` (`FUN_005dae30`) and **case 0x35** =
`processComponentUpdate` (`FUN_005db520`, which is what carries the `0x65` move / `0x50` attack *component*
opcodes via `vtbl+0xD4`). Both call `FUN_005dd840` (reads ≥1 **flags byte** → record `+0x20`, then HP u32 iff
`flags & 0x02`) and then `FUN_005dd900` **unconditionally**. So:
- **There is no conditional "omit the trailer" inside a 0x35/0x03 message** — the flags byte is always read and
  the compare always runs for whatever entity the message names.
- **`FUN_005dd900` forces the avatar's LOCAL flags `cVar7 = 2`** because `FUN_004FA200` (= "has the HP/synch
  component", bitmask test on global `DAT_009308ec`) returns true for the avatar; local HP = `unaff_EDI[0xBC]`
  (=`+0x2F0`). The compare then needs `server_flags == 2` **and** `server_hp == local_hp` exactly.
- ⇒ **Candidate (a) "NONE trailer (flags 0x00)" CRASHES** (`cVar7=2 != server_flags=0` → flags branch). Disproven.
  No flags value is benign for the avatar; only the exact current HP passes.
- **The bypass** (`FUN_004d3bf0()[0x47][0x95] & 1`) is the LOCAL avatar's **active-action** authority bit —
  *global*, one bit on whatever action the local avatar is currently performing, **not** per-synced-entity and
  **not** server-settable (it is set by local input).

**⇒ The ONLY two non-crash exits for the avatar are: (1) the avatar is ABSENT from the server's entity-channel
message** (the message never names the avatar → `processEntityUpdate`/`processComponentUpdate` never invoke the
compare for it), **or (2) the avatar's own-action bit is set** (whole compare block skipped). The "ship a benign
trailer" idea is dead.

**Native rule (refines §6 / §6-LIVE.9):** the server must **never send the avatar — to its own client — any
ComponentUpdate/EntityUpdate that names the avatar, outside its own authoritative-action window.** The move-echo
(`0x35`+`0x65`, `_heartbeat_hp`) is safe *during continuous movement* (the move action holds the bit set → the
compare block is skipped regardless of the HP byte), but it **races at the stop boundary**: an echo sent while
moving but *processed* after the move action ends lands with the bit clear → the stale HP (the captured `203.0`)
is compared → crash. So the fix is not a trailer tweak — it is to **gate avatar ComponentUpdates to the bit-set
window**: echo movement only while the move stream is live, stop echoing the instant it pauses, and never emit a
*spontaneous* avatar ComponentUpdate (the `suppress_originated_avatar_hp` posture, extended to the move-echo's
trailing/boundary sends). The residual question is purely *timing* — keep the echo from outliving the action —
not *what bytes* to put in the trailer. [T0 RE; T1 fix]

### 6-LIVE.10. ★★ The avatar authority bit is CLEAR even WHILE RUNNING — the `+0x95` bypass NEVER engages for the avatar; "moving is safe" is DISPROVEN (x64dbg, 2026-06-16)  **[T0 — live, PID 9832]**

§6-LIVE.9 assumed the move-echo is "safe during continuous movement" because the move action holds the `+0x95`
bit set. **Live capture disproves that.** Caught the same crash (`bp 0x5DD9EA`) the instant a mob hit the avatar
**while the player was running**:

| field | value |
|---|---|
| entity (`[edi+0x80]`) | **`0x1FE` avatar** |
| client/local HP (`ebp`) | `0xC673` = **198.45** |
| server HP (`[eax+0x18]`) | `0xCB00` = **203.0** (same stale last-report as the stationary crash) |
| flags | local `0x02` == server `0x02` |
| bypass action (`FUN_004d3bf0()[0x47]`, `esi`) | `0x20A7CD80` — **the same persistent default action** as the stationary crash |
| authority bit (`[esi+0x95]`) | **`0x10` → bit CLEAR** — *while running* |

`FUN_004d3bf0()` returns the **local controlled unit (the avatar)** (caches at `this+0xA4`; finds the
locally-controlled player unit); `[0x47]` (=`+0x11C`) is its **active action**, and `[0x95]&1` is that action's
authority bit. That action object is **`0x20A7CD80` in both the stationary and the running crash**, and its
`+0x95` is `0x10` (clear) in both. Its constructor (`FUN_005FE000`) **initializes `+0x95 = 0x10`** (bit clear)
by default; nothing in our movement/combat ever raises it to `0x11`.

**⇒ The `+0x95` bypass does NOT engage for the avatar in our setup — moving, stationary, or swinging.** Movement
appears "safe" only because, absent combat, **server HP == client HP so the compare PASSES by equality**, not
because it is bypassed. The instant mob damage opens an unreported delta (client decrements locally; the server
is packet-blind and still echoing the last-reported `203.0`), the compare runs and crashes **regardless of
motion**. This kills §6-LIVE.9 candidate (b) ("keep `+0x95` set through combat") as well — the server cannot set
that bit (confirmed: it is raised only by genuine local-input authority the engine grants, not by our `0x64`
toggle; matches §4's "not server-settable").

**⇒ Only ONE non-crash exit remains: the avatar must be ABSENT from every HP-bearing synch message the server
sends to its own client.** Since the `0x35`/`0x03` trailer is mandatory and `flags=0x00` also crashes
(§6-LIVE.9), and the bit never bypasses, **there is no safe avatar-HP trailer at all.** The move-ack and the
`0x50`/`0x51` action acks (both `0x35` ComponentUpdates naming the avatar's UnitBehavior, carrying
`_heartbeat_hp`) are therefore fundamentally crash-prone during combat — they only survive pure movement by HP
equality. This is the **native model arrived at structurally**: the server must **not echo the avatar's own HP
to the owner at all** (it owns it; the server only *relays* avatar HP to OTHER clients). The open engineering
problem is the **ack mechanism** — the move/action acks are load-bearing (dropping them froze the client,
[[project_regime_b_ack_fix]]), yet they cannot carry an avatar-HP trailer. Resolve by finding a non-synch-bearing
ack sub-message (dispatcher `FUN_005da460` cases **other than** 3/0x35/0x36 do **not** call the compare — e.g.
1/2/5/8/0x32/0x33), or by confirming the client tolerates the move records without the owner trailer. [T0 capture;
T1 fix direction]

### 6-LIVE.11. ★★★ `+0x95` bit0 = "TOWN / peaceful zone", NOT "local-input authority" — and the two non-comparing-ack routes are both dead (Ghidra, 2026-06-16)  **[T0]**

Two follow-ups closed the loop on §6-LIVE.10.

**(1) The ack-path search is exhausted — route (A) is dead.** All six non-comparing dispatcher cases decompiled:
case 1 `processEntityCreate`, 2 `processEntityInit`, 5 `processEntityRemove`, 8 `processEntityCreateInit`, 0x32
`processComponentCreate`, 0x33 `processComponentInit`. **Every one is entity/component lifecycle — none is a
movement or action ack.** Movement (`0x65`) and actions (`0x50`) exist *only* as component opcodes inside case
0x35 `processComponentUpdate`, which calls `FUN_005dd840`+`FUN_005dd900` unconditionally. **There is no
non-synch ack path; any avatar movement/action echo is compared.**

**(2) ★ `+0x95` bit0 is the TOWN/peaceful-zone flag.** `FUN_004a4810` (skill/item-use handler) gates:
`piVar5 = FUN_004d3bf0(); if (piVar5[0x47][0x95] & 1) → display "Can't use in town."` So **bit0 set ⟺ the
avatar's active action is in a town/peaceful zone** (combat skills blocked). The synch compare bypass
(`FUN_005dd900`: skip when `[0x47][0x95]&1`) is therefore **"skip HP validation in town,"** and the ctor default
`+0x95 = 0x10` (bit0 clear) is **"combat zone"**. This **corrects the long-standing "local-input-authority bit"
label** in §4 / §6-LIVE.8/9/10 and `CLAUDE.md` — it was wrong. The bit is per-ZONE (set by zone type), not per
input/action, and the live data fits exactly: dungeon avatar action `+0x95 = 0x10` (bit0 clear → compare runs →
crash), while town would be `0x11` (bit0 set → bypass). It also explains the whole observed pattern:
`suppress_originated_avatar_hp` OFF-in-town works (HP bypassed), ON-in-dungeon, and **every live avatar crash is
in a dungeon.**

**⇒ The definitive consequence.** In a **combat zone there is NO bypass** — the avatar's HP is zero-tolerance
validated against any value the server sends. The server cannot send the right value (packet-blind for mob
damage) and cannot send a benign one (`flags=0x00` crashes; trailer mandatory in 0x35). "Set the bit" (route C)
would mean making the dungeon a peaceful zone — i.e. disabling combat — so it is not an option. **The only
correct behavior is the native one: in a combat zone the server NEVER sends the avatar its own HP.** The avatar
owns its HP; the server relays it only to OTHER clients (who display it, never validate it). The remaining
engineering task is therefore concrete: **stop carrying the avatar-HP trailer on owner-directed move/action acks
in combat zones**, and resolve the load-bearing-ack freeze (regime-B) another way — confirm what the client
actually requires from the move ack (position reconciliation) vs. the HP trailer (which it must not receive). The
HP trailer and the movement records are separable; the movement echo does not *need* the avatar's HP — that
coupling is our porting artifact. [T0]

**★ Caller + entity-gate pinned (x64dbg, 2026-06-16).** The crash's compare is invoked from
`processComponentUpdate` (return addr `0x5DB637` in `FUN_005db520`; caller string
`"ClientEntityManager::processComponentUpdate"`) — i.e. the avatar **move-echo** (`0x35`+`0x65`), caught while
running. The compared entity is resolved at `0x5DB60A-0x5DB617`: `entity = [component+0x14]`, and the compare
runs against it **only if `[entity+0x61] & 0x02` is set** (else the compared entity is NULL → local flags 0). On
the live avatar `[entity+0x61] = 0x0A` (bit 0x02 set) → validated. (A momentary `0x00` read was a **freed,
reallocated** avatar object — the avatar pointer changes on every desync; key by eid `0x1FE`, never by pointer.)
So `+0x61` bit 0x02 is the generic "this unit has synched HP" flag — **set for every real unit, including the
avatar; it is not a clean per-owner lever** (clearing it would also stop the avatar's HP relaying to other
players). Net: **no client-side gate spares the avatar's own HP in combat** — not `+0x95` (town only), not
`+0x61` (unit-has-HP, must stay set). The server-side rework (never emit avatar HP on owner sends; solve the
move-echo freeze empirically) is the only remaining lever. Live also reconfirmed the packet-blind staleness: the
server's adopted HP was frozen at `203.0` across three successive crashes while the client free-fell
`197.4 → 198.5 → 192.5` — the client takes mob damage the server never hears about, so any echoed value is stale
by construction. [T0]

### 6-LIVE.12. ★★★ The client sends NO avatar-HP report in combat — the server is structurally blind (server log + DR_HP_TRAILER_DIAG, 2026-06-16)  **[T0 — live]**

The decisive test. Instrumented every inbound ch7 packet (`DR_HP_TRAILER_DIAG=1`, `net/movement.py`) to dump the
tail + the result of `_read_trailing_avatar_hp`. Live dungeon fight, 22 consecutive packets (moves + `0x50`
actions): **`parsed_hp=None` on every one.** The tails are pure move records (position i32s) and action bytes;
**none carries a `0x06`-terminated EntitySynchInfo with an HP** (the `0x02` bytes present are high bytes of
position fixed-point, not synch flags). The whole-session grep also shows **zero** `0x36`/`HP-SYNC`/adopt events.

**⇒ The client does not report its avatar HP to the server during combat.** This is **not** a parser bug — there
is genuinely no trailer. So `conn.hp_wire` is frozen at the zone-entry MAX (e.g. 203) for the entire fight, the
client free-falls locally, and every load-bearing move/action ack ships that stale MAX → crash. **"Reflect the
client's per-packet HP" is therefore impossible — there is nothing to reflect.** Combined with §6-LIVE.6 (the
server cannot *reproduce* the HP either), the server has **no way to know the avatar's combat HP** — not by
report, not by simulation.

**This is a hard contradiction in the current approach, and it localizes the missing piece.** The move/action
acks are load-bearing (dropping froze the client, [[project_regime_b_ack_fix]]) yet they carry a mandatory,
zero-tolerance-validated HP trailer the server can never get right. No client-side gate spares the avatar in
combat (§6-LIVE.11: `+0x95`=town, `+0x61`=unit-has-HP). So the native protocol must differ from ours in one of
exactly two ways, and the next investigation must decide which:
- **(a) The native CLIENT reports its avatar HP frequently** (so the native server tracks and echoes a matching
  value), and ours is silent due to a setup/handshake difference. → Trace the client's *outbound* packet
  construction: when/why does it append its own EntitySynchInfo HP trailer? What component/flag state gates that?
  This is the most likely missing piece — in deterministic lockstep the peer must publish its state for the
  cross-check, so a totally-silent client implies we never put it in the reporting state.
- **(b) The native server's move/action ack is NOT a `0x35` naming the avatar's UnitBehavior** (so no avatar HP
  validation), and the client's movement-prediction drain comes from a message we're mis-modeling.

Recommended: pursue (a) first — find what makes the client emit its own HP trailer. If we can get the client
reporting, the server tracks it and the acks echo the client's own freshly-stated number (the native
"reflect, don't originate" shape), shrinking the residual to a sub-tick round-trip. [T0 diagnosis; T1/T2 next]

**★ Client-side BP confirms the client is silent (x64dbg, 2026-06-16).** Found the engine's EntitySynchInfo
emitter `FUN_005e00b0`: it writes a flags byte always, then HP (`[entity+0x2F0]`) only when the gate passes —
`[entity+0x61]&0x02` AND two capabilities (`DAT_009308ec` the has-HP type, `DAT_00930854`). The exact emit is
`0x5E04C1 mov edx,[edi+0x2F0]; mov bl,0x02`. BP set there; the client played **zone-load → dungeon → combat with
the BP NEVER firing**, while the avatar compare crashed as usual. ⇒ `FUN_005e00b0` is **server-side** engine code
(its vtable string is "ServerEntityManager") that the *client* never runs for its own outbound — so it was the
wrong place to look, and the client emits its avatar HP via **no path we've found**. The client is now confirmed
silent **three independent ways**: server-side adopt-log (zero events), this client-side BP (never fires), and
the frozen-at-203 server HP. **The avatar's combat HP is, in our setup, never on the wire from the client.**

**⇒ The contradiction is now airtight and localizes to the move-ack model.** Move-ack is load-bearing (drop →
freeze, [[project_regime_b_ack_fix]]); it must be `0x35`-on-avatar (no non-comparing move path exists, §6-LIVE.11);
`0x35` mandates a trailer the client validates zero-tolerance; the server can neither know the HP (client silent,
this section) nor reproduce it (§6-LIVE.6); no combat-zone bypass exists (§6-LIVE.11). All five are [T0]. Since
the original server demonstrably did *not* crash (and C# emulators *do* — `CLAUDE.md` source-of-truth note), one
of our **models** is wrong, and the only unverified one left is **what actually frees the client's movement-
prediction window** — we assume it's the HP-bearing `0x65` echo, but if the client reconciles on the move
records / sessionId alone (HP-independent), there may be a send that frees the window without a validated avatar
HP. NEXT = RE the client movement-prediction path (`ClientUnitBehavior::AddMovementUpdate`, the standing open
issue) to find the real reconciliation trigger. This is genuine unsolved RE — no emulator has cracked it. [T0]

### 6-LIVE.13. ★★★ Path A resolved — the move-apply and the HP-compare are SEQUENTIAL and SEPARABLE, but the protocol gives the server no way to split them (Ghidra, 2026-06-16)  **[T0]**

Traced the order of operations inside `processComponentUpdate` (`FUN_005db520`) — the `0x35` handler that
carries the `0x65` move-echo:

```
1. read componentId (u16) + opcode (1 byte, e.g. 0x65)
2. resolve component  → piVar5 = FUN_0062bf30()
3. (*(piVar5+0xD4))(reader)   ← the move-applier: consumes [session][count][13-byte records],
                                 applies them, and reconciles the client's movement-PREDICTION window
4. FUN_005dd840(reader)        ← read the trailing EntitySynchInfo (flags +, if flags&2, HP)
5. FUN_005dd900(record)        ← the zero-tolerance HP compare  → CRASH
```

**Decisive structural fact: the move records are fully applied at step 3, BEFORE the HP compare at step 5.** The
crash is a *terminal validation* that runs after the load-bearing work (the prediction-window reconcile, keyed on
the `sessionId` the client also uses to dedupe its own prediction) has already completed. So the move-ack's two
jobs are **separable and sequential** — freeing the prediction window does **not** depend on the HP. (Disambiguation
caught while tracing: `[behavior+0x61]` is a move-STATE enum, e.g. set to 2/3 in `FUN_00535fb0`/`0x536080` — a
*different field* from the `[entity+0x61]` synch-flags byte that gates the compare. Don't conflate them.)

**But the protocol provides no server-side split.** Steps 3-4-5 are hard-wired in sequence inside the one `0x35`
handler; the move records cannot reach the applier (`vtbl+0xD4`) via any sub-message that omits steps 4-5 (the
non-comparing dispatcher cases call `vtbl+0xC0`/`+0xC8` = full create/init, not the incremental move applier,
§6-LIVE.11). So the server **cannot** deliver movement without the bundled compare.

**⇒ Path A's verdict.** The prediction window genuinely does not *need* the avatar HP, yet there is no
server-only way to feed the window without it. Avoiding the crash therefore requires one of exactly three things,
and the first two are out of reach in our setup:
- **(a) the server knows the exact avatar HP** — needs the client to report it; the client is silent
  (§6-LIVE.12). **★ Thread (a) explored + closed for the move path (2026-06-16):** the client→server component
  update is message type **`0x34`** (server→client is `0x35`); inspected byte-for-byte, the client's `0x34` move
  packets **end on the last 13-byte move record — NO flags byte, NO EntitySynchInfo, no trailer at all** (and our
  `net/framing.py` keeps the full decompressed payload, so nothing is stripped server-side). So the client's move
  serializer does **not** append HP *by construction* — there is no `flags=0`-vs-`flags=2` gate to flip on the
  move path; the avatar HP is simply never on the client's outbound moves. `DAT_00930854`/`DAT_009308ec` (the
  serializer gate = "WorldEntity" + has-HP) both *pass* for the avatar, confirming the gate isn't the blocker —
  the client just uses a serializer that omits the trailer. Whether a *separate* periodic client→server entity
  update carries HP is unconfirmed, but the server's adopt path (runs on every inbound packet) logged **zero** HP
  all session, so if such a packet exists we never receive it. Net: the client is confirmed silent **five ways**,
  and making it report would require a client-behavior change we cannot drive from the server. Thread (a) is a
  dead end in our setup.
- **(b) the server doesn't echo the owner's own movement** — but dropping it freezes the client (regime-B [T0]),
  so the native client's prediction model must differ from what we can drive from the server.
- **(c) neutralize the client's terminal compare** (`FUN_005dd900`) for the avatar — the only **server-
  independent** option. The engine already ships exactly this bypass for **town** (`+0x95` bit0, §6-LIVE.11); a
  minimal client patch that extends that tolerance to combat zones is the honest, playable path **if** the native
  reporting mechanism (a) cannot be found. It is a client modification, not a server "native" reproduction — name
  it as such.

**Honest status:** every server-side avenue is exhausted; the avatar-HP-in-combat crash is unsolved in *all*
emulators (incl. C#). The remaining native hope is thread (a) — the client's own HP-report path. If that comes up
empty, (c) is the realistic resurrection answer. [T0 structure; the choice between (a) and (c) is a project call]

### 6-LIVE.14. ★★ The launcher does NOT inject HP reporting — thread (a)-via-launcher DISPROVEN; the shim implements (c) surgically (2026-06-16)  **[T0]**

The standing hypothesis behind thread (a) — "the native client reports its avatar HP, and our setup never puts it
in the reporting state" (§6-LIVE.12) — invited the question: *did the required `DungeonNCLauncher.exe` inject or
unlock that reporting?* **Investigated and disproven.** `…/Client 666/DungeonNCLauncher.exe` is a **49 KB .NET
stub** (`NC.FindLauncherStub`, PDB `…\NCLauncher\NC.FindLauncherStub\…\FindLauncher.pdb`); its **only** PE import is
`mscoree.dll` (the CLR shim) and **none** of `CreateRemoteThread / WriteProcessMemory / VirtualAllocEx /
OpenProcess / SetWindowsHookEx` are imported. Its entire managed logic (from its strings) is: read
`Registry`/`SearchPaths`/`Environment` to **locate the PlayNC Launcher**, then `ProcessStartInfo.Start(arguments)`
to run it; if not found → *"Cannot locate the PlayNC Launcher."* **No memory I/O, no sockets, no HP/synch code, no
DLL injection.** It chains to NCSoft's platform PlayNC Launcher (account auth + patching), which spawns
`DungeonRunners.exe … ran_from_launcher` (template `0x8CA5D0`). **[T0 — objdump imports + full string dump]**

**⇒ The launcher adds no code to the running game**, so it cannot be the source of any client→server HP report. This
**reinforces §6-LIVE.12**: the client's avatar-HP silence is *unconditional*, not launcher-gated. The only residual
form of thread (a) is a *client-internal* emit path gated on some arg/flag — but that lives in `DungeonRunners.exe`
(RE-able by us, launcher-independent) and §6-LIVE.12 already searched it three ways and found only the server-side
emitter `FUN_005e00b0`. So thread (a) is, for practical purposes, closed.

**The resurrection answer is therefore (c), built this session as a runtime shim** (not an EXE patch):
`client_hook/` — `drloader.exe` (launches the client with `ran_from_launcher` + injects the DLL; *is* the launcher,
satisfying the token natively, no patch) and `drhook.dll` (inline-detours `FUN_005dd900` @ `0x5DD900`). The detour
reads the synced unit's control mode `[EDI+0xE5]` (bible §10; `1`=display, `4`=client-sim, confirmed via enroll
setter `FUN_005202f0`) and **bypasses the compare when `+0xE5 != 1`** (the unit is one *this* client simulates — its
avatar + enrolled mobs), running the original compare unchanged for display units (which pass by construction,
§6-LIVE.7). This is the surgical, per-entity, **multiplayer-safe** generalisation of the engine's own town bypass
(`+0x95` bit0, §6-LIVE.11): *skip HP validation for units I am authoritative over*. No hardcoded eid (our server's
`conn_id*500+10` gives each client a different avatar eid → hardcoding would break MP; the control-mode read does
not). Built 32-bit via the MSYS2 `mingw-w64-i686` toolchain; detour asm verified in the linked DLL. # T1 fix —
validate against the UNPATCHED client (revert `scripts/patch_client_synch_crash.py` first; the two must not stack).

### 6-LIVE.15. ★★ The client's outbound serializer FOUND — native HP-trailer code EXISTS; the avatar is "silent" because its HP/synch component never reaches the trailer path (Ghidra, 2026-06-16)  **[T0 static]**

Pursuing the user directive "let the client report its HP to the server" (in-protocol, native trailer — the realisation of thread (a), §6-LIVE.12/13). Traced the client's **outbound** entity-message path:

- **`FUN_005df010` = the per-frame outbound serializer** (corrects bible §10's "role unconfirmed"). It allocates a message-builder (`piVar6`; **`piVar6[4]` = the output stream**, write primitive `(*(stream)+0xC)(buf, len)`), and:
  - when its send-queue is non-empty (`param_1[0x293]-[0x292]`), writes **`0x0C` + `timeGetTime()`** to the stream **and** seeds a local MT via `FUN_0044b1b0(timeGetTime)`. ⚠️ This is an *upstream* `0x0C`+`timeGetTime` — the exact thing §3 marked DISPROVEN. It IS emitted (in this fn), but only the maze/combat-seed *consumer* mapping matters for §3; flag for a §3 revisit, **not** today's goal. **[T0]**
  - calls three sub-serializers: **`FUN_005e00b0`** (component updates **with** the synch trailer), **`FUN_005df6f0`** (per-controlled-entity component updates), **`FUN_005e0a30`**.
- **The native HP-trailer-writing code EXISTS** in both `FUN_005e00b0` and `FUN_005df6f0`: when a component's type-tag method (`comp vtbl+0xA8`) returns the right tag and the entity passes `[entity+0x61] & 2` (has-HP — the avatar passes, §6-LIVE.11) **plus** capability-bitmask gates on `DAT_009308ec`/`DAT_00930854`, they write `[flags:1=0x02]` then `[hp:u32 = entity[0xBC] = +0x2F0]` to the stream — **exactly the EntitySynchInfo trailer `FUN_005dd840` reads.** So the avatar HP report is something the client engine is **built to emit**.

**⇒ §6-LIVE.12 reframed: the client is silent NOT because the emit code is absent, but because the avatar's HP/synch component never reaches the trailer-writing branch in our setup** (its `vtbl+0xA8` dirty/type tag and/or outbound-list membership and/or the capability gate). Movement (`0x65`) is a *separate, trailer-less* serializer path — which is why move packets carry no HP (§6-LIVE.12's observation), yet a *component* update would. **This is the missing piece localized to a gate, not a void.**

**Two in-protocol implementations now in view** (both reuse the server's existing adopt path):
- **(X) enable native emission** — flip the gate so the avatar's HP/synch component reaches the trailer branch (most native; the client emits a genuine EntitySynchInfo); or
- **(Y) append at the move-packet tail** — hook the move serializer and write `[0x02][hp]` to `piVar6[4]` right after the avatar's records (server's `_read_trailing_avatar_hp` reads trailer-from-end).

Both need the **exact per-avatar injection point / failing gate pinned**, which is far more efficient **live** (BP in `FUN_005df010`/`FUN_005df6f0`/`FUN_005e00b0` on the playing client, observe which path the avatar's component takes and why its trailer tag stays 0) than by continued static decompilation of these large reference-counted serializers. **Residual still applies:** even with reporting, echoing the OWNER its own HP races by the round-trip (§6-LIVE.9), so the surgical owner-self bypass (`client_hook/drhook.dll`, §6-LIVE.14) remains the owner-authority piece; reporting makes the SERVER + other clients hold the true value (real relay sync). NEXT = live RE to pin the gate. [T0 static; live pin pending]

### 6b. Is this a real pattern? — yes: **deterministic lockstep**  **[T1 / external refs]**

The model is not exotic. "Deterministic dual-simulation from server-provided data" is a
client-server adaptation of **deterministic lockstep** — the netcode family introduced
publicly by Age of Empires (Bettner & Terrano, *"1500 Archers on a 28.8"*, GDC 2001)
and used by most RTS engines (StarCraft, Warcraft III, Supreme Commander) and modern
deterministic games (Factorio). Tier note: the lineage facts below are **external
sources** (reliable history, not the client binary); the *classification* of DR as
lockstep is **[T1]** — inferred from fingerprints matching the documented pattern.

**Lineage (external):** DR descends from **Exarch** (NCSoft), originally *Trade Wars:
Dark Millennium*, built by **Realm Interactive** (Phoenix AZ, ~2002–04), cancelled
2004; the team was relocated to Austin and the work became Dungeon Runners (2007). The
transport middleware in the binary (`THCSockets`, `NcGrouping`, `ncGroupingMessagesLib`)
is NCSoft's shared network stack (also under Lineage 2). So: NCSoft-era middleware for
transport, a deterministic-simulation game layer for combat.

**Fingerprint match** — every defining trait of lockstep is present in the client:

| Lockstep trait (documented) | DR client evidence |
|---|---|
| All machines run the **same deterministic sim** | client + server both compute combat (§6) |
| Exchange **inputs, not state** | client sends `0x50` swings / movement; HP is *computed*, not handed over as authority |
| RNG **seeded identically** (but advances with the *frame*, not a shared turn — see deviation below) | MT19937, combat seed = local `time(0)` per zone load, **server-overridable downstream** via `0x0C` (§3) **[T0]** |
| **Fixed-point** math (floats are non-deterministic across CPUs/compilers) | the **×256 "drfloat"** — positions, rotation, HP, MP all ×256 **[T0]** |
| **Checksum/state compare** detects desync | the EntitySynchInfo HP compare `FUN_005dd900` (a per-entity state check) (§4) **[T0]** |
| Desync = **out-of-sync error halts the game** | "Entity synch error detected" → fatal crash `0xc000013a` (§4) **[T0]** |

**The ×256 fixed-point is the determinism substrate, not just a wire quirk.** This is
the reframe: lockstep *requires* fixed-point because IEEE floats diverge across
CPUs/compilers/instruction-sets, which would desync the sim. DR's ×256 everywhere is
exactly that requirement. **Engineering consequence [T1]:** our Python server must do
combat/position math in **fixed-point integer arithmetic**, never floats — a float path
that rounds even 1 ULP differently from the client will fail the zero-tolerance compare
and crash real clients. (This elevates an item we'd treated as cosmetic encoding to a
correctness constraint.)

**How DR deviates from classic (P2P) lockstep:**
- It is **client-server hybrid**: the server both *provides* the authoritative initial
  state/seed (what spawns, mob stats, the world) and *acts as a deterministic peer* that
  re-simulates. Classic lockstep is symmetric P2P with no authority.
- The desync check is **per-entity state** (HP in the synch trailer), not one global
  frame checksum — finer-grained, and it **crashes the client** rather than showing a
  graceful "out of sync" dialog.
- The avatar's **input-authority bit** (`+0x95`, §4) exempts the one entity whose human
  input the server can't predict before it arrives — the lockstep "you own your own
  commands" rule made per-entity.
- **It is only *partial*, per-entity lockstep.** Classic lockstep keeps RNG deterministic via
  a **command-delay turn barrier** (no peer executes input until all peers have it) and
  advances randomness at fixed in-turn points. DR has **no barrier** (the client executes its
  own input immediately; §4) and its combat RNG advances with the **client's frame clock**,
  not a shared turn (§6-LIVE.6 **[T0]**). So a headless server is a valid deterministic peer
  for **seed-generated state** (no frame-timing dependency — maze/spawns round-trip
  identically, §3) but **not** for **frame-paced runtime combat** of entities a client
  simulates. The `+0x95` bit is the engine's own declaration of that seam, and the Regime-A /
  Regime-B split (§6) is the direct consequence. **[T1 — derived from the T0 facts cited]**

**Why an MMOARPG used RTS netcode:** almost certainly bandwidth + the 2003-era origin —
sending *inputs* instead of full entity state is the literal "1500 archers on a 28.8"
problem, and a deterministic sim also gives free replays/repro. It also explains why DR
sync is so brittle (zero tolerance, hard crash): that is lockstep's nature, not a bug.
The bar to clear is therefore **lockstep-grade bit-exact determinism**, which is exactly
the §6a roadmap.

---

## 7. Per-player / per-group zone instancing  — design + the live bug

### The requirement (from the user)

- A dungeon is **private to the player** who entered it. Two players who both walk
  into `dungeon00_level01` get **separate** copies and **do not see each other**.
- **In a group**, the dungeon is tied to the **group leader**; the other members get
  the **leader's** instance and **do** see each other, share mob HP state, and fight
  the same mobs.
- **Loot is per-player even in a group** (each player gets their own drops). (§8)
- Town / tutorial / social hubs are **shared** (public instance).

### The infrastructure already exists — and the exact bug

`drserver/managers/world_instance.py` already implements per-instance authoritative
world state, keyed on:

```python
InstanceKey = Tuple[int, int]   # (zone_id, instance_id)
```

It builds entities once per instance, snapshots to each joiner, and tears down when
empty. **It is correct.** The bug is the key:

> **`conn.instance_id` is declared `= 0` at `net/connection.py:66` and is NEVER
> reassigned anywhere in the codebase.** **[T0 — verified by grep: zero writes]**

So every player in a given zone shares `(zone_id, 0)` → one shared instance → the
exact symptom the user reports. The fix is **assigning `instance_id`**, not rebuilding
the system.

### The fix design (assign the instance key on zone entry)

Compute and set `conn.instance_id` in the zone-transfer paths (`_start_zone_join`
and `_transfer_zone` in `game_server.py`, where `current_zone_id` is set at lines 720
and 849) using a small policy:

```
instance_id_for(conn, zone):
    if zone.is_public (town / tutorial / social hub):
        return 0                      # everyone shares instance 0
    if conn is in a group:
        return group.instance_token   # leader-owned; stable for the group's life
    return conn.private_instance_token  # per-character unique, stable per dungeon run
```

Design notes (all **[T1]** — derived from the existing [T0] instance system):

- **Token allocation.** A monotonic counter on `GameServer` (e.g.
  `allocate_instance_id()`), the same pattern as `allocate_entity_id`. Private tokens
  are per-(character, dungeon-run); a new run (re-enter from town) may reuse or mint a
  fresh token depending on whether dungeons should persist across exits (DR dungeons
  are ephemeral → mint fresh on each fresh entry from a public zone).
- **Group binding — ✅ SHIPPED 2026-07-08.** The group object owns one `instance_token`
  (minted lazily on first private-zone entry, `groups.instance_token_for`); every
  grouped member entering a private zone adopts it via `_group_instance_token`, so the
  party shares ONE copy. `resetInstances` (ch-0x0B `0x26`, leader-only) mints a fresh
  token for the NEXT entry; members inside keep their live key (stability rule below).
  Leaving the group affects only future zone transfers. The full C# group wire is
  ported (`managers/groups.py`: ch-9 out `0x30/0x32/0x35/0x43/0x44/0x45/0x49/0x4A/
  0x4B/0x4C/0x50/0x55`, ch-0x0B in `0x12/0x14/0x15/0x16/0x17/0x20/0x21/0x22/0x24/0x26/
  0x27/0x28`); party-frame HP (0x4B) rides the adopted client self-report (UI
  channel — no synch-compare exposure). Ch-9 grounding upgraded **[T0]** for the
  dispatcher + 0x30/0x35/0x50 — see the 2026-07-09 subsection below.
- **Stability across the level.** The token must not change while the player is inside
  (the snapshot, pathmap key `f"{zone}_inst{instance_id}"`, monster ids, and merchant
  cids all hang off it). It changes only on zone transfer.
- **Multiplayer correctness already handled.** `world_instance.py` sends the snapshot
  to the **joining connection only** and exchanges avatars separately
  (`net.spawn.exchange_player_spawns`), so once the key is right, "P1 and P2 in the
  same group see each other; P1 and P2 in solo dungeons do not" falls out for free —
  the loot `_broadcast` and monster enroll are **already** instance-scoped
  (`loot.py:202`, `world_instance.enroll_monsters`). **[T0 — code-confirmed]**

### ★ The Code-6 cross-instance leak — FIXED 2026-07-08  **[T0 — live user report]**

With per-player tokens live, P1 killing a mob in HIS copy crashed P2 soloing a
DIFFERENT copy of the same zone: "Zone communication error. Code 6." Root cause:
`combat._broadcast_despawn` filtered recipients by **zone only** — the `0x05` destroy
named an eid P2's client never received → `processEntityInit` "Invalid EntityID" →
code 6 (§10 error table). Fix set (all shipped + pytest-pinned):

- `TrackedMonster.instance_id` recorded at registration (threaded
  `world_instance._populate` → `monsters.build_*` → `register_monster`).
- `_broadcast_despawn` + `_defer_despawn` re-validate `(zone_gc_type, instance_id)`
  (the deferred killer-despawn also re-checks at fire time).
- Loot on a late kill is **skipped** when the killer already left the mob's instance
  (the fan-out keys off the killer's CURRENT instance → wrong-zone ground drops).
- `telemetry._sole_player_for_monster` filters by the mob's instance — two solo
  players in separate copies can no longer claim each other's kills, and the common
  one-player-per-copy case now resolves.
- **Rule (extends the §10 Code-9 rule): any send that NAMES an entity id must be
  scoped to that entity's `(zone_gc_type, instance_id)`, never zone-wide.** Audited
  2026-07-08: hp_broadcast / loot / spawn-exchange / movement relay / skills relay /
  inventory / town_portal / summons / bling_gnome / monster_ai were already
  instance-scoped; combat despawn + telemetry fallback were the leaks.

### ★ The ch-9 GroupClient dispatcher FOUND + the loading-screen stall re-diagnosed (Ghidra, 2026-07-09)  **[T0 static + C# audit]**

The 2026-07-08 live stall (spawn-tail 0x30/0x35 prime froze the 666 client's
loading screen; priming was then hard-disabled, killing the invite menu) was
re-worked from the client binary + a line-level C# re-audit:

- **Client receiver (T0):** ch-9 lands in the `GroupClient` GC service —
  queue-drain pump `FUN_005f7dd0` (poll vtbl+0x34 / pop vtbl+0x38) → opcode
  dispatcher **`FUN_005f7e20`**, `switch` cases **0x30–0x56** matching the C#
  opcode space 1:1 (0x30→`FUN_005f80e0`, 0x32→`FUN_005f8210`, 0x35→`FUN_005f8960`,
  0x42–0x45, 0x47–0x56 incl. 0x50→`FUN_005fa5b0`, 0x55→`FUN_005fa320`
  `processMonsterDifficultySet` — name from client log strings). Messages **queue**
  until the service pumps; they cannot corrupt other channels' streams.
- **Bodies verified (T0):** 0x30 reads `u32→GC+0xB0` (self key — confirms the C#
  "GC+0xB0" log), `u8→+0xB5` difficulty, `u8→+0xB6` invite mode, resets the
  roster. 0x35 reads `u32 groupId→+0xD0, u32 leader→+0xD4, u8→+0xB7, u8 open,
  self-block, u8 count, count×(u32 charId, cstr name, u32 avatarEid, u8 online)`
  and fires UI events (0x121108/0x121111). 0x50 reads `u32 userId ×2, u8
  memberFlag, u32 talkGroupId, raw IPv4, u32 port` → Talkback voice connect.
  Our builders were already byte-identical to all three.
- **The C# "different placement" theory is DEAD:** DRS-NET sends the prime inside
  the same 13/0x06 zone-join burst we do (GameServer.Zone.cs ~2281/~2436), after
  spawn + monsters, before the modifier resend. Same trigger, same wrapper
  (`SendToClient` = `SendCompressedA(0x01,0x0F)`).
- **★ The only wire deltas vs the working C# were:**
  1. **Talkback 0x50 was never sent.** C# appends it after EVERY roster/prime
     send (per-member in `SendGroupConnectedToAll`, solo primes, leave paths) —
     and **`TalkbackServer.Start()` is never called anywhere in DRS-NET**: the
     client's voice connect to port 2604 is refused and silently tolerated. So
     the packet, not a voice server, is the handoff. Now sent at the exact C#
     points (`groups._send_talkback_join`; kick deliberately sends none, like C#).
  2. **13/1 ZoneMessageReady carried the ZONE id.** C# sends the player's
     charSqlId; client-verified: `ZoneClient::processReady` **`FUN_005fc250`**
     stores the u32 at **ZoneClient+0xF4** as the player user id. Fixed in
     `_send_zone_progression`.
- Priming is back ON by default (`DR_GROUP_PRIME=0` for A/B). `accept()` also
  aligned to C# 0x20 (roster + health, no 0x4C zone labels). The round-5 bisect
  revert of the OP3/OP1b identity fields (self + remote Player init: `u32
  groupId` + `u32 charSqlId`, C#-verified in `BuildOtherPlayerSpawnPacket` and
  the OP3 writer) is UNDONE — the fields are back, they're what the client's
  right-click invite/goto keys on. `[BUILD]` startup line updated to fingerprint
  this build. Suite 1124.
- **[T2 open]** the round-4/5 stall was never cleanly attributed (round 5 stalled
  with priming OFF and a clean server log — stale-process suspicion unresolved).
  If the load stalls on a `[BUILD]`-confirmed 2026-07-09 server, A/B in this
  order: `DR_GROUP_PRIME=0` (priming+0x50) → if still stuck, x64dbg the
  load-screen wait; do NOT re-revert the OP3/13-1 ids blind (both are C#-exact
  and width-identical).

### The mob-copy limit in a SHARED instance — what is and isn't fixable  **[T1 — from §6/§14.7]**

Two grouped players in one instance receive the same mob entities (same eids), but
each client's native brain simulates its own copy (§14.7 native-mob model) — so mob
positions/HP diverge between P1's and P2's screens, and "the mob is hitting P2" is
not visible on P1's copy. **Entity-level mob HP sync between two natively-simulating
clients is structurally impossible today**: the client sends no monster HP upstream
(§6-LIVE.12) and a server mob-HP send to a client that simulates the mob trips the
zero-tolerance compare (§6-LIVE.2/.7). What IS synced across the party: mob **kills**
(telemetry → instance-wide `0x05` despawn, killer deferred for the death anim), loot
drops, XP, member **HP bars** (ch-9 `0x4B` party frame — UI channel, no compare), and
live **equipment changes** (`net.equipment.relay_equipment_to_viewers` mirrors the
Manipulators visual ops onto each viewer's remapped component id with the proven
empty-synch trailer). Open investigation for true one-copy mobs: make the NON-owner
clients *display* the mob (owner-scoped enrollment — the EntitySynchInfo `0x01`
ownerID / `behavior+0x70` enroll gate, §7 of CLIENT_SERVER_MODEL); needs a live
trace of whether an ownerID'd spawn suppresses the native brain. **[T2 — unverified]**

### ★ Player-action + mob-engagement relays — SHIPPED 2026-07-09  **[T1 — live-test pending]**

Three multiplayer-visibility gaps the user reported (P1's actions/fights invisible to
P2; grouped members greyed while co-located) closed without touching the HP-compare:

- **Player CreateAction relay (`net/action_relay.py`).** P1's combat action was acked
  only to P1 — a shift-attack (0x50), position/Shift cast (0x51) or self-cast (0x52)
  was **invisible** to every other client. The server now converts the inbound `0x01`
  ActionResponse into a **CreateAction (`0x04 <class>`)** on each same-`(zone,instance)`
  viewer's *remapped* avatar behavior (`remote_behavior_ids[viewer][actor]`), mode byte
  **normalized to 0x00** (the actor's session id is meaningless on the viewer),
  **empty-synch trailer** (never assert the actor's HP to a displaying viewer — the
  proven remote-avatar shape, like the movement/equipment relays). ✅ **P2 sees P1's
  actions live (2026-07-09).** The old `skills._broadcast_self_cast` delegates here.
  - **★ Delivery MUST be framed-direct, NOT the interval queue** (fixed 2026-07-09 same
    day): the interval-queue variant delivered the action **late/batched**, out of order
    with the framed-direct movement relay — a targeted `0x50` roots the display avatar in
    a UseTarget approach that no viewer-side brain ends, and the late action re-rooted it
    → **"movement no longer synced after a cast / basic attack"** (live regression). Fix:
    (a) relay framed-direct so action + movement stay ordered, AND (b) an **un-root** —
    the first move after a relayed action relays a **CancelAction (`0x03`)** to viewers
    (`movement._handle_client_move`, gated on `conn.viewer_action_pending`) so the copy
    leaves the attack pose and resumes following. Action cadence (~2–4/s) is far under the
    movement relay's own framed-direct rate, so no §2 budget risk.
  - Shape is **[T1]** (CreateAction is the T0 mechanism for a displayed unit to act —
    proven for mobs via `BuildMonsterAttackPacket`; per-class body `# UNVERIFIED` vs a
    live P2 capture). Kill-switch `DR_ACTION_RELAY=0`.
- **Mob-engagement relay (`managers/mob_engagement_relay.py`).** In the native-mob model
  P2's brain aggros only on *local* proximity, so a mob chasing P1 sat **frozen at
  spawn** on P2's screen. On each P1 `0x50`, the server tells the **non-engaged**
  instance members' copies to Follow P1's avatar (T0 Follow CreateAction `0x04 0x16`,
  `build_monster_follow_packet`, **framed-direct**), 1 Hz throttled. Target = P1's global
  avatar eid (the avatar ENTITY id is NOT remapped per viewer — only component ids are,
  `spawn.py:527` — so it resolves on every member's client). **HP-safe:** the trailer is
  the mob's **spawn max** (a viewer who hasn't touched the mob holds exactly that —
  display copy, server-authoritative HP, matches by construction, §7); the instant a
  viewer attacks the mob they're **excluded** (they now simulate its HP — any assertion
  crashes them), and the engaging owner is never sent anything (breaks its native chase,
  §14.6 6n). Purged on kill/despawn. **Requires a SHARED (group) instance** (same mob
  eids); solo players in separate copies never share mobs. Kill-switch
  `DR_MOB_ENGAGEMENT_RELAY=0`. **[T2 — live-unconfirmed]:** whether P2's native brain
  *accepts* the server Follow or immediately re-idles the mob is unverified (bible §14.6
  round-2 found the client's local Follow can own movement). If P2's brain overrides it,
  true one-copy mobs need the owner-scoped-enrollment path (§7 tail), a larger effort.
  This does NOT sync mob *positions* — at most it makes the engagement visible.
- **Group grey-out — the 0x4C-on-accept gap.** A group formed **in place** (both in
  town) left every roster entry with no zone label, so the client greyed same-zone
  members "as if elsewhere." C# has the **same bug** (0x20 accept sends roster+health,
  no 0x4C). Fix: `GroupManager.accept` now cross-fans the members' `0x4C userChangedZone`
  labels immediately (each carries the real `current_zone_name`, so a co-located member
  matches the viewer's zone and un-greys; one actually elsewhere still greys). **[T1]**

### "Separate process threads per zone" — recommendation

The user suggested OS threads/processes per user zone. **Recommended approach:
logical instances on the existing single asyncio loop, *not* OS threads.** Rationale **[T1]**:

- The server is single-process `asyncio` (CPython GIL); real threads would not run
  world ticks in parallel and would add lock complexity around shared registries
  (`connections`, entity-id allocator, DB).
- The `0x0D` world-clock pacing (§2) is **per-connection** already — each client's
  cadence is independent. Isolation is achieved by the **instance key**, not by an OS
  scheduling boundary.
- The `WorldInstanceRegistry` already gives each `(zone_id, instance_id)` its own
  entities, pathmap, monster set, and teardown. That **is** the "separate zone" — it
  just needs the key populated.
- If CPU ever becomes the limit, scale **out** (multiple game-server processes behind
  the queue handoff, sharding instances by key), not threads-in-process. That is a
  later concern; correctness comes from the key first.

---

## 8. Loot ownership — per-player, even in groups

- **Drops are instance-scoped today.** `loot._broadcast` sends a ground-drop create
  only to connections sharing the killer's `(zone_gc_type, instance_id)`
  (`loot.py:200-204`). **[T0 — code-confirmed]**
- **Per-player loot within a shared (group) instance is NOT yet implemented.** Today a
  drop in a shared instance is visible to everyone in it. The requirement ("certain
  loot dropped for certain players") needs a **per-drop owner filter**: tag each
  `DroppedItem` with an `owner_login` and have `_broadcast` (and the pickup path in
  `movement`/`loot`) gate visibility/pickup to the owner. **[T2 — design]**
- **How DR actually scoped per-player loot is [T3/unknown].** Whether the real client
  shows other players' drops as greyed-out, hidden, or freely lootable must be
  confirmed against the client/extracted content before implementing. Do not copy the
  C# emulator's scheme as truth.

### 8a. Item generation is HYBRID — weapons old-gen, armor new-gen  **[T0 — GCDictionary 2026-06-17]**

The "true drop rates" task surfaced that the running 666 client uses **two item
generations at once** (proven against `extracter/GCDictionary.dict` = what the client can
deserialize):

- **Weapons = OLD numbered gen ONLY.** Dict has `1HAxe1PAL.1HAxe1-N` (the `-N` is a
  **quality tier of one base weapon** — `-1`="Cardboard Hand Axe" gold 50 … `-10`="Uber
  Hand Axe" gold 4550, identical damage). `items.pal.1HAxePAL.*` and the weapon generators
  `items.ig.1HAxe.*` are **0 entries in the dict** → the extracter's weapon IG tree is the
  WRONG generation; dropping its items would hit the §10 "Zone communication error". Weapon
  rarity rides the quality variant + ScaleMod, NOT a separate Normal/Mythic base item.
- **Armor = NEW gen WITH IGs loaded.** Dict HAS `items.pal.<Class><Slot>PAL.<Quality>NNN`
  AND the generators `items.ig.<class>.*`. The `armor` table already holds the 181 new-gen
  rows. So for ARMOR the extracter IG tree **IS** the client's real loot system — already
  resolved to `(item, rarity) -> mods` in the **`item_wire_mods`** table.

**Implemented (flag-gated, OFF):** `loot_roller.load_armor_rarity_pool()` builds a
rarity-indexed pool from `item_wire_mods ⋈ armor`, filtered by
`is_droppable_newgen_armor` (new-gen `items.pal.*pal.*`, excluding
`partialbuilt`/`prebuilt`/`seasonal`). All 87 distinct pool items are dict-confirmed (0
crash-risk). The roller then drops rarity-appropriate REAL armor; weapons stay old-gen.
Gated by `EMIT_NEWGEN_ARMOR_DROPS=False` because the new-gen armor **drop wire body is
unvalidated** (merchants only ever sold dash-suffix gear) → flip on and live-test against
the UNPATCHED client before calling it done. See [[project_loot_generation_split]].

**Drop rate + count (2026-06-17 user feedback "rates too high; always 1 item + money").**
The old roller dropped gold on EVERY GG activation and multiplied the DB `treasure_count`
by the tier item-count → common count-2 mobs almost always dropped both. Now each
generator activation is gated by a **per-tier drop CHANCE** (`_TIER_ITEM_CHANCE`
default 0.12, boss **0.75** / `_TIER_GOLD_CHANCE` default 0.40, boss 1.0) so most trash
kills drop nothing or only occasional gold. **Boss ITEM drops are NOT guaranteed**
(user-confirmed against the live client 2026-06-21): the C# server drops boss loot 100% of
the time, but C# is REFERENCE ONLY, never ground truth, so we don't inherit that guarantee
— boss stays the most generous item tier (a multi-IG boss almost always drops something)
without any single activation being certain. (Boss *gold* stays a reliable 1.0; only the
item chance is probabilistic.) These chances are an **approximation** — the real weights
are the Phase-2 binary-RE item below.

**Drop POSITION (same feedback "loot drops where the mob spawned").** Combat is
client-authoritative, so a mob usually dies entirely on the killing client and the kill
arrives via telemetry/replay, which carries **no position** — leaving the server's tracked
`monster.pos` at the SPAWN anchor. `combat._generate_loot` now drops at the **killer's**
current position unless the server actually drove the chase (`monster_ai` moved `pos` off
`spawn_x/y`), in which case the tracked pos is the real death spot.

**Still approximate / TODO:** the per-generator **rarity weights + drop counts + gold
ranges** are the top-level `DefaultIG/ChampionIG/HeroIG/TreasureChestBossIG`+`*GG` — named
in `GCDictionary` (ids 20035..) but **defined in NO extracted `.gc`** → a binary-RE task
(Phase 2), along with `ItemTimeline.*` MinLevel/MaxLevel bands. Real IG-resolved mods on
the **dropped/picked-up** item (vs the current ScaleMod) is a further wire change.

---

## 9. Putting it together — the build checklist for a desync-free server

1. **Match the regime** (§6). **Regime A** (displayed entities, spawns, layout): reproduce
   from the data the server provided + same `0x0C` seed → values equal the client's by
   construction. **Regime B** (an entity's HP for the client that simulates it — own avatar,
   enrolled mobs): **never originate** that HP; relay only the owner's self-report, on the
   owner's own packets. Originating Regime-B HP races → crash (§6-LIVE.6/8).
2. **Never contradict the client's computed value** (§4). The server's number must
   *already equal* what the client computed. No tolerance; the only skip is the avatar's
   `+0x95` input-authority bit (which the server can't control as a fix — §4).
3. **Honor the message-rate contract** (§2): ≤ 1 ch7 message / 133 ms sustained;
   stream inside the `0x0D` frame; bursts OK.
4. **Bridge RNG via `0x0C`** (§3) — **for Regime A only.** The seed is **not** sent upstream;
   the server *controls* it **downstream** (`0x0C`→`0x5DA870`, proven) as last writer after the
   client's `time(0)` reset, and mirrors the same value. This makes seed-generated state
   reproducible; it does **not** make Regime-B combat reproducible (the draw *position* drifts,
   §6-LIVE.6).
5. **Ignore latency** (§5): no ping comp, no lockstep, no rollback. The model is
   latency-tolerant by construction.
6. **Set `conn.instance_id`** (§7): public→0, group→leader token, solo→private token.
   The rest of instancing already works. ✅ DONE (solo 2026-06-16; groups + the
   Code-6 instance-scoped-sends rule 2026-07-08 — see §7).
7. **Per-player loot** (§8): add an owner tag to drops once the client's scoping
   behavior is confirmed.
8. **Filter owner-only components** out of other players' spawn/destroy streams
   (`QuestManager`, `DialogManager`, `AvatarMetrics`, `Modifiers`, `Skills`,
   `UnitContainer`, `Bank`, `TradeInventory`, `Inventory`). **[T0]**

---

## 10. Evidence catalogue — client functions & fields (all **[T0]**, base `0x400000`)

### Functions verified this session (2026-06-12)

| Address | Role | Note |
|---------|------|------|
| `FUN_0044b1b0` | MT19937 `init_genrand(seed)` | mult `0x6c078965`; our `rng.py` matches |
| `FUN_0044b1f0` | MT19937 generate/twist | matrix `0x9908b0df`; auto-seed `0x1105`; reload @ `0x270` |
| `FUN_005df010` | per-frame entity update | ⚠️ old claim "reseeds MT from `timeGetTime` + serializes `0x0C`+time **upstream**" is **DISPROVEN** (§3/§12): combat MT is seeded from local `time(0)` per zone load (`0x5DDF7D`); `0x0C` is **downstream-only**. This fn's actual role is unconfirmed. |
| `FUN_005da460` | `ClientEntityManager::processMessage` | message switch; case `0xc`/`0xd`/`0x35`/`0x36`/`0x46`… |
| `FUN_005da870` | inbound `0x0C` handler | reads u32, `init_genrand(value)` — RNG resync |
| `FUN_005da7d0` | inbound `0x0D` handler (processInterval) | programs world timestep/cadence |
| `FUN_005db6a0` | inbound `0x36` handler (EntitySyncHP) | componentId lookup; null→**Code 9**; → compare |
| `FUN_005db520` | inbound `0x35` handler (processUpdateComponent) | → compare |
| `FUN_005dd840` | read synch trailer | flags byte→`+0x20`; bit `0x02`→HP u32→`+0x18` |
| `FUN_005dd900` | **zero-tolerance HP compare** | mismatch→"Entity synch error"→crash; skipped by `+0x95` |
| `FUN_005d9e30` | world-clock pump | `if pending>2: elapsed*=3`; 4-tick msg consume |
| `FUN_004d3bf0` | per-entity controller resolver | caches `+0xa4` |
| `FUN_0051cb50` | input gate | same `+0x95 & 1` early-out |
| `FUN_005202f0` | enroll setter | `+0xe5=4`; calls controller-activate (sets `+0x95`) |
| `FUN_005fe000` | UnitAction ctor | sets `+0x95 = 0x10` (bit CLEAR) by default |

### Functions & addresses verified 2026-06-13 (the seed model)  **[T0 — live x64dbg]**

| Address | Role | Note |
|---------|------|------|
| `0x5DDF7D` | entity-mgr reset (seeds the combat MT) | `call init_genrand` @`0x5DDF78` with `&MT=(entitymgr)+0x44`, `seed=time(0)`; runs per-manager on every zone load |
| `0x7220E5` | CRT `time(NULL)` | `GetSystemTimeAsFileTime` `[0x82B234]` → Unix seconds; the combat-seed source |
| `0x5DA870` | inbound `0x0C` seed handler | reads u32, `init_genrand((obj)+0x44, seed)` @`0x5DA894`; dispatch via tables `0x5DA730`/`0x5DA6E0` (opcode `0x0C`→entry `0x07`→`0x5DA58D`) |
| `0x4CFD40` | maze-layout MT seeder (caller) | adopts the server's `13/0x00` layout seed; proven `0x1D77A96E` server==client |
| `0x932FF8` | **global** MT (`.data`) | heavily drawn every frame; role TBD (general/effects RNG?); NOT a heap manager MT |
| `0x4F6580` | `Damage::apply` | ctx in `edi`: src handle `+0x2C`, tgt `+0x34`, type byte `+0x3E`; unit=`[handle+0x14]` |
| `0x59861E` | combat resolver (`Damage::apply` call @`0x598619`) | rolls happen earlier in this fn; tail calls `0x4FD7E0` (dmg-descriptor ctor, no RNG) + `0x508E70` (faction check, no RNG) |

### Combat damage chain verified 2026-06-14 (live full-swing capture)  **[T0 — live x64dbg, PID 3788]**

A single clean element-5 player swing was captured end-to-end and the Python port
(`drserver/combat/client_swing.py`) reproduced the client's applied damage **bit-exact**. Full
formula + the 3 port bugs it surfaced live in `docs/COMBAT_FORMULA.md` §3/§7. The **damage-magnitude**
chain is now T0; hit/miss threshold, block, crit, element-1 remain unvalidated.

| Address | Role | Note |
|---------|------|------|
| `0x597E50` | combat resolver `FUN_00597e50(event)` | `__cdecl`, `event = [esp+4]`; verified-clean BP anchor. Caller `FUN_005921c0` (ret `0x592229`) |
| `0x598FFC` | `call FUN_00598ED0` inside `fd0` | at this insn `eax = in_EAX = b30's return`, `ecx = mit`, `edx = weapon` (fires only on a landed hit) |
| `0x599016` | variance roll (after `genrand` @`0x599011`) | `eax = raw draw`, `edi = lo`, `[esp+0x10] = hi`; `result = (raw % (((hi>>8)<<8)-lo+1)) + lo` |
| `0x598C10` | `FUN_00598c10` mitigation/scale | returns `((v * stat[+0x300]) >> 8) >> 8` (TWO shifts); LIVE = `0x64` (100). element/armor switched (§5) |
| `0x598B30` | `FUN_00598b30` variance "power" term | return = `attacker[+0xfc] + element_term + attacker[+0x180] + armor_term`; passed to `ed0` via `eax`; LIVE = 11 |
| `0x598ED0` | `FUN_00598ed0` variance range | writes `lo`→`*esi`, `hi`→`*param4`; first term uses `in_EAX` (b30), middle step `=t*mit/100` |
| `0x598A30` | `FUN_00598a30` weapon base dmg | feeds the **crit gate ONLY** (`crit = roll1 < clampedBase`); NOT added to applied damage |
| `0x5988D0` | `FUN_005988d0` defender avoidance | **`eax` = defender stats** (not ecx); returns `max(0,(b+100)*a/100)`, element-switched (§5); LIVE def=52 |
| `0x59814E` | hit/block decision (`cmp`) | `eax`=threshold, `[esp+0x30]`=roll1, `edx`=roll2, `[esp+0x28]`=block_chance; HIT iff `roll1<threshold ∧ block_chance≤roll2` |

**Event struct** (`event = param_1`, `int*`)  **[T0]**:

| Index | Field |
|-------|-------|
| `event[0]` | target0 obj → `[0]`=accuracy_target_term, `[1]`=extra_c10, `[6]`≠0 = block_disabled, `[7]`=defender unit |
| `event[1]` | weapon (`+0xd5` element, `+0xe8` armorClass, `+0xec` variance scalar, `+0xf0` spread factor) |
| `event[3]` | per-instance entity-manager (`+0x44` = the combat MT the server's `0x0C` seeds) |
| `event[4]` | attacker unit (`vtbl[+0x128]()` → attacker CombatStats `piVar8`; observed `piVar8 == unit` for the player) |
| `event[5]` | the MT pointer (`= event[3] + 0x44`) |
| `event[6]` (u16) | `hi_flag` — the variance "level/power" input (LIVE = 10; player was server-level 2, so NOT the level) |
| `event[7]` | kind (1/2/3) |

**Damage object** (built size `0x44` via `FUN_004f64f0`, applied by `Damage::apply` `0x4F6580`, ctx in `edi`)  **[T0]**:

| Offset | Field |
|--------|-------|
| `+0x2C` | attacker CombatStats (`piVar8`) |
| `+0x34` | defender unit |
| `+0x38` | **applied damage** (×256), post-crit, floor `0x100` |
| `+0x3c` (short) | crit-reduction = `(target[+0xf4] * attacker[+0x108]) / 100` |
| `+0x3f` (byte) | element |
| `+0x40` (byte) | armor class (`weapon+0xe8`) |
| `+0x41` bit0 | crit flag |

**Captured HIT (damage, bit-exact):** element 5, weapon `+0xec`=154 / `+0xf0`=64, hi_flag=10,
attacker `+0x180`=11 / `+0x300`=256 → mit=100, b30=11, lo=`0x900` (9.0), hi=`0x1000` (16.0); raw
genrand `0x0A0499A0` % `0x701` = 1398, + lo → **`0x0E76` (3702 = 14.46 dmg)** = `Damage+0x38`. crit=0.

**Captured MISS (threshold, bit-exact — NOT-melee path):** attacker `+0xf0`=70 / `+0x314`=2,
defender avoidance `+0x12c`=52 / `+0x314`=2, block_chance `+0x138`=0 → acc=70, def=52, hitPct=57,
rangeAdj=`(2-2)*…`=0 → **threshold=`0x3900` (57.0)**; client rolled roll1=`0x39A2` (14754) ≥ threshold
→ **miss**. The melee-in-range hit path (acc−140 + block CurveTable `FUN_00598810`) is still unvalidated.

### CombatStats origin: it is a deserialized GCObject, not a client-computed formula  **[T0/T1, 2026-06-14]**

Where the attacker/defender combat fields (`+0xf0`…`+0x320`) come from — the §6 question:

- **The CombatStats struct IS the unit object.** `event[4]->vtbl[+0x128]()` returns `this` — verified
  live for both the player (`0x26898510`) and the mob (`0x26CDB1B0`). The `+0xf0`…`+0x320` fields live
  at the unit base. **[T0]**
- **Those fields are deserialized from GCObject data, NOT computed by a stat formula.** `FUN_005ea6a0`
  carries the literal `"GCObject::OnPostLoad(%s, %d) …"`; `FUN_0053b740` is the combat class ctor (sets
  vtbl `PTR_FUN_0087c820` + secondary `PTR_FUN_00892328`@`+0x30`, seeds defaults `+0x12c`=`0x100`,
  `+0x122`=`0x3c`, `+0x131`=3); `FUN_004f9660` is the version-gated OnPostLoad migrator (clamps `+0x314`
  to a global cap). **[T0]**
- **Consequence:** the server already serializes these objects from its content DB, so §6 is a
  field-mapping problem (offset ↔ GCObject property), not formula replication. The server's existing
  `drserver/combat/monster_damage.py` (`MonsterUnitStats`/`PlayerUnitStats`, C#-ported) ALREADY maps
  these offsets and **agrees with the live RE** (+0xF0 AR, +0xFC dmg-bonus, +0x100 dmg-mod, +0x118 crit
  mult, +0x12C/+0x130 defense, +0x138 block, +0x300 scale=256, +0x314 discriminator, weapon
  +0xEC/+0xF0). The `creatures` DB table carries the source columns (`attack_rating`, `defense_rating`,
  `critical_chance`, `damage_mod`, `attack_range`, resists, `max_health`). **[T1]**
- **Still open [T2]:** whether values are pure-deserialized or base+Modifiers-adjusted before combat;
  and the **stat BUILDER** (creatures row + weapon → StatBlock) is unported/unwired. NB: the C#
  `compute_swing` in `monster_damage.py` is **structurally different** from the live-validated
  `client_swing.py` (different variance-range factoring) — treat `client_swing.py` as canonical.

### Reading named CurveTables from the live client  **[T0, 2026-06-14]**

Reusable recipe (used to get the real monster stat curves):
- Registration table at `~0x8063d0` runs, per curve: `push <holder>; mov edx,<name>; call <reg>`. The
  resolved CurveTable ptr is cached at **holder+4**. Holders: **AR `0x932da0`**, **DR `0x932da8`**
  (stride 8). Name strings: `Tables.MonsterAttackRating`@`0x8a390c`, `…DefenseRating`@`0x8a3928`,
  `…Damage`@`0x8a38dc`, `…Health`@`0x8a38f4`.
- CurveTable obj: entry-ptr list `[obj+0x6c .. obj+0x70)`, min/max key `obj+0x7c/+0x80`. Each entry:
  `+0x68`=key (level ×256), `+0x6c`=value (Fixed32). Interp `FUN_005d3790`→`FUN_005d4050`.
- **Live-read values:** DR (`0x1A99EED0`) = L1→35, L15→287, L110→3087; AR (`0x0CB0B540`) = L1→100,
  L110→32800. **Both match `monster_curves.py`.** So the AR curve is CORRECT — the mob's `+0xF0`
  accuracy (live 60) is NOT `(auth×AR_curve)>>16` (=399); the C# `compute_base_ar` mapping is the
  suspect, to be resolved by tracing the client's actual `+0xF0` write. (DR/HP builder PROVEN bit-exact.)

### Field map  **[T0, live-confirmed]**

| Field | Meaning |
|-------|---------|
| `entity+0x80` | entity id (avatar = `0x1FE`) — `FUN_005dd900` reads it as `EDI[0x20]` |
| `entity+0x2F0` | CurrentHPWire — **the local HP the compare reads** (`EDI[0xbc]` as `int*`) |
| `entity+0xbc` | HP byte-view (older char-typed read site); the compare itself uses +0x2F0 |
| `behavior+0x70` | enroll gate (0 = local control allowed) |
| `behavior+0xa4` | cached controller pointer |
| `behavior+0xe5` | control mode (1 display / 4 client-sim) |
| `behavior+0x156` | bit0 control-requested, bit1 client-update-on |
| `controller[0x47]` (`+0x11C`) | active UnitAction |
| `action+0x95` | authority byte; **bit0 = local input authority** (ctor `0x10`) |
| `clock+0xa94` | client world-tick counter (overwritten by `0x0D`) |
| `entitymgr+0xac0` | last error code (9 = Invalid ComponentID / "Code 9") |
| `entitymgr+0x292..0x293` | inbound entity-message queue begin/end |
| `entitymgr+0x44` | **combat/entity MT19937 state** (seeded by local `time(0)`, overridable by server `0x0C`) |
| `entitymgr+0xB18` | world-clock pacing base (`timeGetTime`) — **separate** clock from the RNG seed |

### Entity-stream error codes & GCObject `readType` (2026-06-17, Ghidra + live)  **[T0]**

The **"Zone communication error. Code %d."** crash (display handler `FUN_0047e2a0`, msg `0xdebc2`,
string `0x86193c`; fatal via the NULL-write assert idiom `xor eax,eax; mov [eax],al` gated on flag
`0x930FF3`). The **code** is set in `FUN_005da460` (`ClientEntityManager::processMessage`) at
`entitymgr+0x2b0` — **every code is an entity-channel stream desync**, not a distinct subsystem bug:

| Code | String | Cause |
|------|--------|-------|
| **2** | `"Unexpected message size"` | loop wanted another sub-message but <1 byte left → a sub-handler **over-read** (ate the `0x06` terminator) |
| **3** | `"Unknown message type(%u)"` | next type byte isn't a known case → reader **landed on garbage** mid-payload |
| 4 | `"…didn't end with END or CONNECTED"` | early return, no `0x06`/`0x46` |
| 6 | `processEntityInit "Invalid EntityID"` | `entitymgr+0xac0`=6 (distinct from the §4 "Code 9" Invalid ComponentID) |

The top-level message types (switch in `FUN_005da460`): 1 create, 2 init, 3 update, 5 remove, 6 END,
8 create+init, 0xc seed, 0xd interval, 0x32/0x33 component create/init, 0x35 ComponentUpdate, 0x36
EntitySyncHP, 0x46 CONNECTED. **`0x07` BeginStream is not in this switch** — case-2 `processEntityInit`
(`FUN_005daac0`) reads the entity's whole init (position + embedded GCObject) via the entity vtable
`+0xb8`/`+0xc8`, so an itemobject create is ONE case-2 message, not separate sub-messages.

**GCObject deserialization:** `GCClassRegistry::readType` = `FUN_005e3c40` — reads 1 type-tag byte;
**valid tags are only {0=null/end, 1=u8 TypeID, 2=u16 TypeID, 4=u32 djb2 hash, 0xFF=string-name}**.
Any other value → "Invalid type tag %u" → fatal. The child-list reader is `FUN_00583920`
(`[count:1 | 0xFF+u16] (child via readType)*`). A byte-misaligned child-list reads a payload byte as
a tag → crash. **Live recipe to read the receive buffer:** the stream's read primitive is its
`vtable+0x10` (`FUN_0063E1C0`): `B=[stream+0x28]`; buffer **base=`B+0x28`, end=`B+0x2C`,
cursor=`B+0x38`**. Walk the crashed ebp chain to the reader frame, resolve the stream, dump base+cursor.

**Applied:** the loot pool was dropping the deprecated `items.deprecated.deprecatedchild*` item classes
(no `-N` dash suffix → no client schema) → readType hit 0x64 inside a ScaleMod string → Code 2/3. Fixed
by filtering the loot pool to the merchant's proven client-itemized set (`is_client_droppable_item`).
See [[project_loot_drop_crash_fix]].

**Applied (2026-06-17, Code 9 — Invalid ComponentID):** the merchant-restock watchdog (`merchant_manager.
flush_due_refresh`, run once-a-second from the movement tick loop) is armed with a **per-zone merchant
cid** when the player clicks a vendor, but `_transfer_zone` never disarmed it. Player talked to the
tutorial `HermitVendor`, walked into a dungeon, and ~300s later the watchdog flushed a `0x35 <hermit_cid>
0x1E/0x1F` ComponentUpdate into the dungeon zone — that cid does not resolve there → null component →
"Invalid ComponentID" → Code 9. **Fix:** `_transfer_zone` now clears `active_merchant_npc/cid/due` in the
cross-zone reset block (the same-zone in-place respawn keeps it — the vendor is still valid there).
Rule: any per-zone cid the server keeps in `conn` state must be cleared on cross-zone transfer.

---

## 11. Open questions & live-verification recipes

| # | Question | How to resolve | Tier now |
|---|----------|----------------|----------|
| A | ~~Upstream `0x0C <timeGetTime>` on the wire?~~ **RESOLVED 2026-06-13: NO.** Combat seed = client's **local `time(0)`** (§3), never sent upstream; the server **controls** the seed downstream via `0x0C`→`0x5DA870` (proven). | — | **T0** |
| A' | ~~Which MT does combat draw from?~~ **RESOLVED 2026-06-13 (Ghidra): the seedable per-instance `+0x44` MT, NOT global `0x932FF8`** — resolver `FUN_00597e50` reads `event+0x14`; melee builder `FUN_00594430` sets it `= *(X+0x88)+0x44`. Path-A targeting correct; one live `esi`-read at `0x59804b` to confirm same-instance. | — | **T0** |
| B | ~~With the right MT seeded + bit-exact formulas + same draw order, does the server reproduce client swing damage?~~ **PARTLY RESOLVED 2026-06-15 (§6-LIVE.6): magnitude YES (bit-exact both directions), but "same draw order" is INFEASIBLE — the `+0x44` MT is the shared entity-manager RNG drawn by combat resist/proc rolls AND mob AI/wander (`FUN_0052d680`) between swings, so the variance draw position drifts unrecoverably.** | Re-scope to the authority model (which entities are client-simulated vs server-displayed → where the compare actually fires) | T0/T1 |
| C | How does DR scope per-player loot visibility in a group? | Inspect client drop/pickup handlers + extracted loot content; do NOT trust C# | T3→? |
| D | Group→instance binding: where does the client expect the leader's instance? | Trace group-join + zone-enter on the real client; confirm members get one shared world | T2 |
| E | `Fixed32` scale — 256× or 16.16? | Disasm a `MoveTo`/`Fixed32` site (affects positions + HP if ever wrong) | T2 |
| F | Late-join movement drop ("P1 can't see P2 moving", `AddMovementUpdate`) | Disasm the drop condition / `OnAllUpdatesDropped` | T2 |

### Reusable x64dbg breakpoints (base `0x400000`, no ASLR)

- HP compare entry: `bp 0x5DD900`
- Compare mismatch body: `bp 0x5DD966; bpcnd "[edi+0x80]==0x1FE"` (avatar eid)
- MT reseed: `bp 0x44B1B0` (`eax`=state, `ecx`=seed, `[esp]`=caller: `0x5DDF7D`=local `time(0)` per-mgr, `0x5DA899`=server `0x0C`). Combat MT = `(entitymgr)+0x44`; `genrand`=`bp 0x44B1F0` (state=`esi`, draw→`eax`); CRT `time()`=`0x7220E5`
- `0x0C` inbound seed: `bp 0x5DA870` (reseeds `(obj)+0x44`); combat resolver caller `0x59861E`, `Damage::apply`=`bp 0x4F6580` (ctx in `edi`)
- World-clock pump: `bp 0x5D9E30` (inspect `pending>2` 3× branch)
- Enroll setter: `bp 0x5202F0; bpcnd "[[edi+0x14]+0x80]!=0x1FE"`

---

## 12. Fabricated / unproven lore — distrust list  **[T3]**

Do not treat any of these as truth without a T0 confirmation:

- **"The server cannot reproduce client combat rolls."** — The *old rationale* (client
  hands over its seed upstream) was itself WRONG (§3 — no upstream seed). Reproduction is
  still achievable, but via the **server controlling** the seed *downstream* (`0x0C`, proven)
  plus bit-exact formulas + draw order — **T2** (unproven end-to-end; the server's current
  combat *grossly* diverges — it killed a mob the client kept alive).
- **"Combat seed = `timeGetTime` sent upstream in a per-frame `0x0C`."** — WRONG; this was in
  this file's own §1/§3 until 2026-06-13. The seed is local `time(0)` (seconds); `timeGetTime`
  is the *separate* world-clock pacing base (`entitymgr+0xB18`). The only seed-bearing `0x0C`
  that matters is **server→client downstream**. **[T0]**
- **Every "no HP crash" live result.** Obtained on a client **patched in the HP-sync
  path** (`FUN_005dd900`, §4 caveat). [T3] for HP correctness — re-validate against the
  unpatched exe. "Didn't crash on the patched client" proves nothing about server HP.
- **Anything "because the C# server does it."** DR-Server / old UGS are emulators;
  the HP-synch crash lived in them too. Reference only.
- **Go/RainbowRunner + L2 network notes** (XOR game encryption, lockstep ideas) —
  DEAD per the 2026-06-11 directive. Our game channel is plaintext THCSockets+zlib;
  the client ships `cryPassThroughEncryptor` precisely to allow plaintext. **[T0]**
- **Per-player loot scheme copied from any emulator** — unknown until client-checked.
- **Latency compensation / lockstep / rollback of any kind** — none exists in the
  client (§5). Adding it will fight the zero-tolerance compare.
- **`instance_id` having ever worked** — it has always been `0`; instancing was built
  but never keyed. The system is sound; the key was missing.

---

## 13. Quests — the client-grounded protocol & data model  **[T0 — Ghidra 2026-06-21]**

> Written because the Python quest layer (`managers/quests.py`, `managers/quest_wire.py`) was a
> **port of the C# `QuestManager`** that was *never grounded to the client* — the inbound submessage
> meanings were guessed ("`0x01` empty = accept", "`0x05` = accept-or-turn-in"). This section is the
> **client truth**, decompiled from `DungeonRunners.exe` (base `0x400000`, no ASLR). The C# server is
> **reference only** (CLAUDE.md source hierarchy); every claim here is from the client binary.
>
> Anchors that bootstrapped this: DRS-NET log string `Quest::readObjectives@0x005BD560
> Quest::processUpdate@0x005BD170`; class-factory `FUN_005bf570` registers `"QuestManager"` (class
> type-id cached at `DAT_00930c7c`); ctor `FUN_005c00c0` (object size `0xFC`, vtable
> `PTR_FUN_0089ef18`, base id `+0x68`, offered-quest slot `+0xE4`, checkpoint flags `+0x6d`).

### 13.1 The component & the two opcode spaces  **[T0]**

The `QuestManager` is an **owner-only `EntityComponent`** on the **player OBJECT** (not an HP unit), so
every quest packet's trailing EntitySynchInfo is **flags-only `0x00`** (`write_synch_none`) — asserting
HP here crashes the §4 compare (already learnt, see [[project_unpatched_client_baseline]]). All quest
traffic is `0x35 ComponentUpdate · uint16 questManagerId · <opcode:u8> · <payload> · <synch-none> · 0x06`.

**The opcode after the component id is a RAW byte, and the two directions use DIFFERENT numbering.**
This is the trap the C# port fell into — it assumed one symmetric opcode set.

### 13.2 Server→client (the client RECEIVES) — dispatcher `FUN_005c3550`  **[T0]**

`MOVZX EAX,[opcode]; DEC EAX; CMP EAX,0xB; JA default; JMP [EAX*4 + 0x5c3630]` →
**1-based, valid `0x01–0x0C`**. Decompiled handlers:

| op | handler | meaning | payload after opcode |
|----|---------|---------|----------------------|
| **0x01** | `FUN_005c3810` | **AddQuest** | `readType(quest)` · `u32 instanceId` · `u8 allDone` · objectives |
| **0x02** | `FUN_005c3970` | **RemoveQuest** | `u32 instanceId` |
| **0x03** | `FUN_005c3a20`→`FUN_005bd170` | **UpdateQuest** | `u32 instanceId` · `u8 sub` (`0`→`u8 completeFlag`; `1`→`readObjectives`) |
| **0x04** | `FUN_005c3660` | **SetOfferedQuest** (enables Accept btn) | `readType(quest)` → stored at QM`+0xE4` |
| **0x05** | `FUN_005c3770` | **ClearOfferedQuest** (closes offer) | *(none)* — clears `+0xE4` |
| **0x06** | `FUN_005c3b60` | **ShowTurnInDialog** (query-complete) | `u32 instanceId` |
| **0x07** | `FUN_005c3af0`→`FUN_005c48b0` | **AvailableQuestUpdate** (`!` markers) | `u8 npcCount` · per npc: `cstring npcGcType` · `u8 hashCount` · `readType×n` |
| **0x08** | `FUN_005c3c10` | **Finalize** (celebration; **ALSO removes the quest**) | `u32 instanceId` |
| **0x09** | `FUN_005c3cd0` | AddQuestType (completed/available add) | `readType(quest)` |
| **0x0A** | `FUN_005c3df0` | Checkpoint/town-portal slot A | `u8 bool` · `u32` · `cstring` · `cstring` |
| **0x0B** | `FUN_005c4010` | Quest message/hint string | `cstring` |
| **0x0C** | `FUN_005c3f00` | Checkpoint/town-portal slot B | `u8 bool` · `u32` · `cstring` · `cstring` |

**Objective wire** (`Quest::readObjectives` `FUN_005bd560`, shared by `0x01` AddQuest and `0x03/sub1`):
`u8 count`, then per objective `u8 flags` · `cstring label` · `u16 required`. **`flags` bit `0x01` =
complete; bit `0x02` = "required count follows"** (gated read at the `& 0x02000000` test). The current
`quest_wire._write_objectives` (`flags = 0x02 | complete`, cstring, u16) is **byte-exact correct**.

⇒ **The OUTBOUND side (`quest_wire.py`) is correct as written**: `send_add_packet` (0x01),
`send_remove_packet` (0x02), `send_progress_packet` (0x03), `send_accept_dialog` (0x04),
`send_turn_in_dialog`/`send_complete_packet` (0x06), `send_available_quest_update` (0x07),
`send_finalize_packet` (0x08) all match their handlers. Note 0x08 **already removes** the quest
client-side, so the extra `send_remove_packet` after finalize is redundant (harmless: stale instanceId
→ `FUN_005c11b0` lookup returns null → no-op).

### 13.3 Client→server (the client SENDS) — senders via `FUN_005dcd20(comp, writer, op)`  **[T0]**

`FUN_005dcd20` writes the opcode as the raw submessage byte. The QuestManager's ten senders
(`0x5c04a0–0x5c0b10`), each decompiled, with the UI that fires them:

| op | sender | payload | fired by | meaning |
|----|--------|---------|----------|---------|
| **0x01** | `FUN_005c0580` | *(empty)* | offer dialog **Accept** (`FUN_0046bc40`) | **AcceptOfferedQuest** — gated on QM`+0xE4`≠0 |
| **0x02** | `FUN_005c0620` | *(empty)* | offer dialog **Close** (`FUN_0046bc60`) | **DeclineOfferedQuest** — gated on `+0xE4`≠0 |
| **0x03** | `FUN_005c06c0` | `u32 instanceId` | QuestLog Abandon→2× confirm (`LAB_0046bb80`) | **AbandonQuest** |
| **0x04** | `FUN_005c0780` | `u32 instanceId` | NPC dialog: click an **active** quest (`FUN_00456130`) | **QueryActiveQuest** (ask server to open turn-in) |
| **0x05** | `FUN_005c0840` | `u32 instanceId · u8` | turn-in dialog **Complete** (`FUN_0046bc80`) | **CompleteQuest** (the actual turn-in confirm) |
| **0x06** | `FUN_005c04a0` | `u32 entityId · writeType(quest)` | NPC dialog: click an **offered** quest (`FUN_00456180`) | **QueryOfferedQuest** (ask server for the offer) |
| **0x07** | `FUN_005c0910` | `writeType(quest)` | map/tracker UI (`0x427124`) | TrackQuest |
| **0x08** | `FUN_005c5b30` | `u32 npcEntityId` | NPC dialog **"NPCTeleportOption"** (`LAB_00456230`) | **NPCTeleporter teleport request** — sent via QM vtable`+0xd8` (not `FUN_005dcd20`) |
| **0x0A** | `FUN_005c09d0` | *(empty)* | map (`+0x6d`&1) | town-portal A request |
| **0x0B** | `FUN_005c0a70` | *(empty)* | map (`+0x6d`&2) | town-portal B request |
| **0x0C** | `FUN_005c0b10` | *(empty)* | gated `+0x88` | (checkpoint-related) |

The QuestLog "Complete" button takes a longer road (`FUN_0046bbb0` → console command `"CompleteQuest %u"`
→ command obj `FUN_005bf150`) but lands on the same op-5 turn-in.

### 13.4 The two canonical handshakes  **[T0]**

**Accept** (normal quest): click giver → **C→S `0x06`** (`entityId`+type) → **S→C `0x04`** SetOfferedQuest
(this is what *enables the client's Accept button* — gate `+0xE4`) → click Accept → **C→S `0x01`** (empty)
→ **S→C `0x01`** AddQuest (+ `0x05` to close the offer). The server is **authoritative on instanceId**
(it's assigned in AddQuest); the client adopts it.

**Turn-in**: click giver with a complete quest → **C→S `0x04`** (`instanceId`) → **S→C `0x06`**
ShowTurnInDialog → click Complete → **C→S `0x05`** (`instanceId`+`u8`) → **S→C `0x08`** Finalize
(celebration + remove) and grant rewards.

**★ LIVE-CONFIRMED 2026-06-21 (x64dbg, unpatched client PID 7136, the Wishing Well).** Both
handshakes captured byte-for-byte against the running Python server:
`op6`(`FUN_005c04a0`, from NPC-dialog `FUN_00456180`) → `recv04`(`FUN_005c3660`) →
**Accept** → `op1`(`FUN_005c0580`, from `FUN_0046bc40`) → `recv01`(`FUN_005c3810`, instanceId **1036**);
then `op4`(`FUN_005c0780`, from `FUN_00456130`, instanceId 1036) → `recv06`(`FUN_005c3b60`) →
**Complete** → `op5`(`FUN_005c0840`, `u32 1036 · u8`) → `recv08`(`FUN_005c3c10`) + `recv02`(`FUN_005c3970`).
The turn-in **completes** (the C# port's `pending_turn_in_instance_id` path happened to be primed by the
`op4`→`send_turn_in_dialog` step). The `op5` payload **does** carry the instanceId, so the robust fix
in §13.5 (read it from the payload instead of relying on pending) is still correct — it just isn't the
reason this particular flow worked.

### 13.5 Where the current Python diverges — the real bugs  **[T0 vs code]**

`managers/quests.py::handle_component_update` got `0x03` (abandon) and `0x06` (query) right, but:

1. **`0x05` is mis-modelled (THE turn-in bug).** Client sends `0x05 = u32 instanceId · u8`. The Python
   `0x05` branch treats it as "turn-in via `pending_turn_in_instance_id`, **else** read 9 bytes as an
   accept (`npc+gcType+hash`)". Op-5 is **never** an accept and its payload is **5 bytes, not 9**.
   **Fix: read `u32 instanceId` (then the trailing `u8`) and `handle_turn_in_confirmed(instanceId)` —
   do not depend on pending state, do not parse 9 bytes.** This is why turn-ins that didn't go through
   a server-set `pending_turn_in` (e.g. the wishing well's auto-accept path) silently fail.
2. **`0x01` is accept-only.** The Python `0x01` branch also has turn-in and "quest-log view" fallbacks;
   the client only ever sends `0x01` for **AcceptOfferedQuest** (empty). Keep the accept branch (resolve
   from the offered/pending quest set by the prior `0x06`); the others are dead and misleading.
3. **The `0x06` "toggle-accept" hack should go.** `0x06` is *always* a query — the server answers with
   `0x04` (offer) or `0x06` (turn-in dialog); the accept is a *separate* `0x01`. The current
   "if `pending_quest_hash == hash` accept immediately" branch can double-fire.
4. **`0x04` is correct** (read `instanceId`, open turn-in dialog if completable) — keep it.
5. The server **must** send `0x04` SetOfferedQuest in reply to a `0x06` query before the client can
   accept: the Accept sender (`FUN_005c0580`) is hard-gated on `+0xE4`≠0. `send_accept_dialog` already
   does this.
6. **★ FIXED 2026-06-21 (live) — 0-objective quests couldn't be turned in.** `can_query_complete` gated
   0-objective completion on `AutoAcceptOnQuery`, so a plain 0-objective quest ("Speak with each Skill
   Trainer", `world.town.quest.class.{fi,rg,ma}.Q01_a1`) returned `False` → the `0x04` turn-in query got
   **no `recv06` response** → the NPC dialog just closed (live: `op4` re-fired, never a `recv06`). A
   0-objective quest has nothing left to do; it is turn-in-ready as soon as active. `AutoAcceptOnQuery`
   governs the *accept* step, not completion. Fix: `can_query_complete` returns `True` for 0 objectives.
8. **★ NOT WIRED (live 2026-06-21) — NPC teleporters ("Teleport to Snowman Sanctuary") do nothing.**
   An NPC with an `NPCTeleporter` component (e.g. `world.town.npc.SnowMan1`: `Teleporter extends
   NPCTeleporter { Zone=dungeon_snowman; SpawnPoint=start }`) shows an "NPCTeleportOption" in its dialog
   (client dialog builder `FUN_00454860` gates it on the entity having the `NPCTeleporter` component,
   class-id `DAT_00930c60`). Clicking it (`LAB_00456230`→`FUN_005c5b30`) sends **QM inbound opcode `0x08`
   + `u32 npcEntityId`** (live-captured: snowman eid 1573). The server's `handle_component_update` only
   handles `0x01`–`0x06` → `0x08` is dropped → nothing happens. **Fix:** add a `0x08` branch: read the
   npc entity id → `npc_manager.find_by_entity_id` → resolve its `NPCTeleporter` Zone/SpawnPoint (NOT
   currently imported into `NPCData` — must be parsed from the NPC `.gc` `Teleporter` block) →
   `server.change_zone(conn, zone, spawn_point)`. (Distinct from the world-entity teleporter in
   `movement.py:622`, which is a standalone placed entity, not an on-NPC component.)
9. **Content gap, not a bug — the Wishing Well grants nothing.** `world.town.quest.well.base`'s
   `RewardItemGenerator = OneTimeUseOnlyWishingWellIG` is a **dangling reference** — that IG is defined
   *nowhere* in the extracted content (the `.gc` even comments `//Needs to be hooked up to a really sweet
   IG`). The turn-in completes (live: `op4→recv06→op5→recv08+recv02`) but there is no item to grant. To
   give the well a reward, point it at a real IG — a design decision, not a port fix.

### 13.6 Data model (extracted `.gc`, tier-2 ground truth)  **[T0 — extracter/]**

Quest **instances** live in `extracter/world/**/quest/*.gc` (1290) + tutorial
`quests/base/HelperNoobosaur/*` (the `quests_importer` already loads these into the `quests` table).
Each `extends quests.base.{Quest,QuestToken,QuestCash*,QuestRepeat*,QuestNoReward,…}` (the base classes
set `TokenReward`/`CashReward`/`GrantXPBuff`). A quest = a `Description` block + 0..N **objective**
anonymous children (`* extends quests.base.<X>Objective`):

- **KillObjective** — `MonsterType`, `MonsterType2…N`, `RequiredKills`.
- **ItemObjective** — `ItemType`, `RequiredQuantity`, `RemoveOnFinalize`; **may nest `KillDropTrigger`
  children** (`MonsterType` · `Chance%` · `Item`). The drop-trigger is the server mechanic that turns
  kills into quest-item drops (e.g. "collect 12 Fizz Rock" = ratling kills, 30% drop). Item objectives
  are satisfied by *holding* the item (bag is authoritative), consumed on finalize.
- **GoToObjective** — `TargetZoneName` (zone) + `TargetEntityName` (named entity) + `Range`. ⚠️ The
  Python importer/runtime reads only `TargetEntityName`; it ignores `TargetZoneName`/`Range` and there
  is **no runtime that advances goto** — these objectives never complete server-side. (Open work.)
- **ActivateObjective** — `EntityType` (+ `RequiredQuantity`); advanced by clicking an NCI/shrine. Also
  unwired in the runtime.

`AutoAcceptOnQuery=true` (wishing well, tutorial info quests) + 0 objectives ⇒ querying the giver makes
the quest instantly turn-in-ready. `OnAcceptItemGenerator` grants an item at accept (e.g. the signed
certificate that an ItemObjective then requires). Reward fields: `TokenReward` (King's Coins),
`CashReward`, `GrantXPBuff` (the `QuestXPBonus` EXPMOD buff), `RewardItemGenerator`+`NumRewardItems`,
`ModToAddOnComplete`.

**★ Gold-reward provenance (asked 2026-06-21) — partly grounded, magnitude UNVERIFIED.** The server
grants `gold = round(level × QuestGoldPerLevel × CashReward)` (`quests.py::_apply_rewards`), credited +
shipped live as `0x20` AddCurrency. Breakdown of where each piece comes from:
- `CashReward` — **REAL** [T2]: the per-quest multiplier from the base `.gc` classes (`Quest`=0.5,
  `QuestCash1`=1, `QuestCash2`=3, `QuestCash3`=5, `QuestToken`=1, `QuestTokenMajor`=2, …).
- `QuestGoldPerLevel` + `GlobalKnobs` — **REAL field names** [T0]: both are registered in the client
  binary (`QuestGoldPerLevel` getter `FUN_005d57f0`→str `0x8a3230`; `GlobalKnobs` str `0x8a38bc`; both in
  `GCDictionary.dict`). So the formula *shape* is authentic.
- The VALUE `_QUEST_GOLD_PER_LEVEL = 250` — **NOT verified** [T3, C#-derived]. `GlobalKnobs` has **no
  instance file in the unpacked content** (only the type name, `GCDictionary.dict` id 20690 — searched
  exhaustively 2026-06-21); it is a runtime config singleton loaded into client globals (the
  `QuestGoldPerLevel` property accessor is `FUN_005d8db0` = `mov eax,[0x00930CB8]`, one of a contiguous
  block of knob-object pointers at `0x930CA0+`). A live-memory read attempt was **inconclusive** (the
  followed knob object's +0x14 hash did not match djb2("QuestGoldPerLevel"), so the descriptor→accessor→
  knob chain has an unresolved hop). Also note **gold is granted server-side** — `QuestGoldPerLevel` is a
  *server* knob; the original NCSoft server's value is the truth and may not be recoverable from client
  data at all. `250` (from the C# emulator) is the best available, treat as approximate. To nail it: a
  clean reflection walk of the `0x930CB8` knob, or unpack the real `GlobalKnobs` from `game.pkg`.
- "level" = the quest's `MinLevel` (defaults to 1) — also a C# interpretation; could be the *player's*
  level in the original. Gold is **server-computed** (the client only receives `0x20`), so the exact
  formula can't be read from the client binary — only the knob value can (live-read `GlobalKnobs` in the
  client, or unpack `game.pkg`). Until then, treat the gold *amount* as approximate.

### 13.7 Open quest items (need live trace)  **[T1/T2]**

| # | Question | How to resolve |
|---|----------|----------------|
| Q1 | `AutoAcceptOnQuery` semantics — **partly answered LIVE 2026-06-21**: with our server, querying the Well (`op6`) returns a **normal `recv04` offer dialog**; the client did **NOT** auto-accept (no `op1`/`op4`/`FUN_005c0c50` fired) — it waited for the player to click Accept, then a *second* click to turn in. So `AutoAcceptOnQuery` is **not collapsing the accept step** in this build. STILL OPEN: whether the *original* server collapsed it (server-side auto-accept on `0x06`) or whether the field is a no-op here. Do **not** assume "instant one-click wish" — unproven. To settle: trace the quest-desc `AutoAcceptOnQuery` bit (`0x89ca24`) consumer + `FUN_005c0c50` flag `+0x35&0x40` against the live client. |
| Q2 | The `0x05` trailing `u8` — constant confirm (=1), or a reward-choice index for multi-reward quests? | Trace `FUN_005c0840` arg on a quest with a reward chooser |
| Q3 | GoTo/Activate objective completion — there is no server runtime. Wire `goto` against `TargetZoneName`/`Range` and `activate` against NCI clicks. | implement + test |
| Q4 | `0x09` AddQuestType (recv) semantics — completed-list sync vs available-add? | decompile `FUN_005c6280` callee |

### Reusable quest breakpoints (base `0x400000`)
- Inbound dispatch (S→C receive): `bp 0x5c3550` (`[esp+4]`=opcode, 1-based)
- Turn-in send (C→S `0x05`): `bp 0x5c0840` ; Accept send (`0x01`): `bp 0x5c0580` ; Query send (`0x06`): `bp 0x5c04a0`
- Objective reader: `bp 0x5bd560` ; quest-by-instanceId lookup: `FUN_005c11b0`

---

## 14. Combat balance — monster level, XP, difficulty, wander & boss spawns  **[T0 — Ghidra + live x64dbg + extracter, 2026-06-21]**

> Investigation trigger (user, 2026-06-21): "combat is really off … monsters give too
> much exp, then monsters get too hard at dungeon01 and unbeatable, monsters wander too
> far from spawn, melee mobs run through you, some bosses don't spawn (dungeon06)."
> The findings below are grounded against the **client** (tier-1) and **extracter** (tier-2),
> NOT the C# emulators. Several long-standing values turned out to be **C#-invented**.

### 14.0 The one linchpin: the monster **level** is a pure SERVER choice  **[T0 — Ghidra]**

The server writes a single `level` byte in the monster spawn packet (`Unit::readInit`,
`net/monsters.py:362`). The client stores it at unit **`+0x314`** (the *discriminator*) and
derives **EVERYTHING** from it locally via CurveTable lookups in the activation function
**`FUN_00509960`** (`(char)param_1[0xc5]` == `+0x314` fed to every stat vtable call):
max-HP, attack rating, defense rating, the MonsterDamage curve, **and the XP-per-kill**.

There is **no independent client-side level check** — the HP synch passes only because the
server computes the spawn HP from the *same* level (`monster_health.monster_hp_wire`), so the
two always agree. ⇒ **We can set monster level to anything; HP stays self-consistent (no synch
crash). The level is 100% ours to tune.** This makes the level model the master knob behind
*both* "too much XP" and "dungeon01 unbeatable."

### 14.1 The level FORMULA — `_TIER_LEVELS` is C#-invented; the real offset is the encounter `LevelOffset`  **[T0 — extracter]**

Current code (`managers/monster_health.py`): `monsterLevel = min(100, zoneBaseLevel + GetLevelForTier(tier))` where
- `zoneBaseLevel("dungeonNN…") = NN*4 + 1`  (Python; the C# `DR-Server` uses `NN*4`, **no +1** — a latent discrepancy: our dungeon01 base = 5, C# = 4).
- `GetLevelForTier`: FODDER 0, RECRUIT 1, VETERAN 2, CHAMPION 4, HERO 6, WARMONGER 8.

**Both knobs are emulator inventions, not in client/extracter:**
- The extracted creature tiers (`creatures/base/UnitMelee_*.gc`) define a tier → **multiplier** only
  (`Difficulty` HP-mult: FODDER 0.5 / RECRUIT 1.0 / VETERAN 2.0 / WARMONGER 2.5 / CHAMPION 4.0 / HERO 7.0;
  plus `DamageMod`, `AttackRating`, `MaxHealth`, `CriticalChance`). **No tier→level offset exists in the data.**
- The **real, authored** per-unit level bump is `EncounterUnit.LevelOffset` in the encounter `.gc`
  (e.g. `world/dungeon06/enc/level08_master_encounter.gc`: boss `LevelOffset=2`, guards `=1`).
  Distribution across 151 encounter files: `0`(×152) `1`(×337) `2`(×308) `3`(×19) `5`(×1) `-2`(×1) `-1`(×1).
  Regular dungeon trash encounters (e.g. `dungeon01/enc/base/level01_encounter.gc`) carry **NO `LevelOffset`** ⇒ +0.
  **The server reads `LevelOffset` nowhere** (`grep level_offset → 0 hits`).

**⇒ Grounded model:** `monsterLevel = zoneBaseLevel + EncounterUnit.LevelOffset(default 0)`. The tier scales
HP/damage via its **multipliers** (grounded), and must **NOT** also be added to the level (that double-counts —
see §14.3). The `EncounterUnit.Difficulty` field is the spawn-weight/experience-difficulty, **not** the tier.
*(Open: `zoneBaseLevel`'s `NN*4` slope is still unverified against the client — the dungeon-number→base-level
map is the one piece neither client nor extracter has yet yielded. The `NN*4+1` Python `+1` is unverified drift
vs the C# `NN*4`.)*

### 14.2 XP-per-kill — LIVE-VERIFIED; the math is correct, the lever is the ExperienceMod  **[T0 — live x64dbg, PID 7136, 2026-06-21]**

Combat is client-authoritative for XP: on a kill the client **self-awards** XP (internal message
type `0x13`→`0x14`, NOT a server packet — consistent with "packet-blind"). Captured live (player L1, killed an L2 mob):

- **The `Tables.Experience` threshold curve, read from live client memory** (holder `0x931974`, ptr+4 → curve obj;
  5 entries): **`(L2,10) (L3,25) (L4,45) (L5,65) (L100,5000)`** — an **EXACT** match to `player_state._XP_CURVE`. ✅
- The level-up handler **`FUN_004f82f0`** (msg `0x14`) and threshold helper **`FUN_004faf60`** compute
  `threshold(L) = curveValue(L) × 100` — **EXACT** match to `xp_threshold_for_level` (L2 = 1000 XP). ✅
- Per-kill XP (measured: avatar `+0x320` XP counter went **0 → 500**):
  `gain = base × min(mob,player)/player × ExperienceMod%/256`. With `base` field = `0x500` (1280),
  `ExperienceMod% (+0x2b8)` = 100, ratio = 1.0 → **500 XP**. The server's `xp_per_kill` returns the same 500. ✅
- Each level grants **+5 attribute points** (`avatar +0x33c` (`param_1[0xcf]`) += 5) and refills HP/MP.

**Consequence (key):** `min(mob,player)/player` **caps at 1.0 whenever mob ≥ player**, so over-leveling
a mob does NOT raise its XP — XP is pinned at `base`. **Lowering mob level will NOT reduce XP.** The only
server lever that scales the client's XP *without a client patch* is the **ExperienceMod modifier**
(`avatar +0x2b8`, default 100), exactly the mechanism `avatar.base.FreePlayerExperienceModifier` uses
(free players −15%). Send a modifier with `<100%` to slow leveling globally; mirror the same factor on the
server's `xp_per_kill` so server XP stays in lockstep with the client.

> **Verdict on "too much XP":** the per-kill math is *retail-accurate* (500/kill, 2 kills→L2, 5→L3, 9→L4, 13→L5
> — the curve slows as you climb). "Too much" = the client's own fast early curve. Reduce it via an
> `ExperienceMod` modifier (a design choice for the user — pick the %), not by touching the formula.
> **Residual unknown:** whether `base` (1280) is a flat constant (= `500×256/100`, level-independent — the
> reading that exactly matches the measured 500 and the server) or `640×mobLevel` (level-scaled — would make
> over-leveled mobs give >500). One kill of a **different-level** mob (dungeon01) disambiguates; trap
> `bp 0x4f82f0`, read `[ecx+4]`(base) `[ecx+8]`(mobLevel). Evidence favors **flat** (the live 500 == the
> server's level-independent value).

**Reusable XP breakpoints (base `0x400000`):** XP-award/level-up `bp 0x4f82f0` (`ecx`=msg: `+4`=base
`+8`=mobLevel; avatar=`[esp+4]`, level `+0x314`, XP-counter `+0x320`, ExperienceMod% `+0x2b8`);
threshold(L) `FUN_004faf60`; Experience curve holder `0x931974`.

### 14.3 "dungeon01 unbeatable" = the tier double-count  **[T1 — derived from §14.1 + monster_health.py]**

A high-tier mob currently gets its tier counted **twice**: once on the **level** (CHAMPION `+4`, HERO `+6`
via `_TIER_LEVELS`) *and* once on the **HP/damage multiplier** (CHAMPION `×4.0`, HERO `×7.0` via the grounded
`Difficulty` mult). Because the MonsterHealth/MonsterDamage curves climb steeply with level, the level bump
compounds the multiplier. Concrete (dungeon01, zoneBase 5):
- CHAMPION leader: **current** L9 × 4.0 × 1.25 MaxHealth ≈ `curve(9)·5` ≈ 2480 HP; **grounded** (L5 + LevelOffset)
  × same mults ≈ `curve(5..6)·5` ≈ 1660 HP. Damage scales the same way.
- The HP send stays synch-safe either way (server recomputes HP from whatever level it sends).

**Fix direction (grounded):** drop the tier→level offset, read `EncounterUnit.LevelOffset` instead; keep the
tier *multipliers* (they're the authored difficulty). This is a balance change → confirm the intended
dungeon-level slope with the user before shipping (the `zoneBaseLevel` `NN*4` slope itself is unverified).

### 14.4 dungeon06 (and dungeon01–15) boss not spawning — the importer drops the per-marker EncounterTable  **[T0 — extracter + DB]**

`*_boss.world` files are parsed as **static (non-maze) worlds**. Two spawn styles exist:
- **dungeon00 & dungeon16**: direct **named** creature placements (`RattleTooth` posse) → `static_world_placements`
  (the only two zones with rows: 7 and 74). These **work**.
- **dungeon01–15 incl. dungeon06**: the boss is placed via a **per-marker `EncounterTable` override** inside the
  `.world` `Entities` block, e.g. `dungeon06_level08_boss.world` has
  `* extends base.Encounter { EncounterTable = world.dungeon06.enc.level08_master_encounter; }` and that table
  (`enc/level08_master_encounter.gc`) spawns `world.dungeon06.mob.boss` (**Rotgut**, `DUNGEON_BOSS`, `Difficulty=25`)
  + `boss_guard2 ×3` + `boss_guard3 ×4`.

**The bug:** the static-world importer captured the encounter **markers** (region pos/size) into
`static_world_encounters` but stored `encounter_type` **EMPTY** — it never recorded each marker's
`EncounterTable` override. So for dungeon01–15 the spawner has a region but **no creature table to roll**, and
the boss + guards never spawn. (dungeon06 boss markers in the live DB: 2 rows, both `encounter_type=''`.)
**Fix 1 (DONE 2026-06-21):** `_collect_encounter_markers` read `EncounterType` (absent on boss markers) instead
of the per-marker `EncounterTable`. Now reads `EncounterTable` first. The import driver already collects
`marker.encounter_type` into the parse set (l.835) and the spawner already does `_load_groups(marker.encounter_type)`,
so this one-line read fixes the chain. Verified: `dungeon06_level08_boss` marker 0 now → `level08_master_encounter`
(Rotgut + 7 guards, 8 units parse OK). **This alone fixes 8/13 bosses** (the ones whose `mob.boss` resolves to a
*concrete* creature already in the `creatures` table: d00 broodling.Basic.Champion, **d01 shaman.Poison.Hero**,
d02 orokchieftain.Ice.Hero, d03 seer.Fire.Hero, d04 Widower.Fire.Hero, d11 Balrog.Poison.Boss, d15 ratsputin.Ice.Grunt,
d16 harbinger.Shadow.Boss). Needs a DB re-import (`scripts/import_dungeon_worlds.py`, server stopped — Windows lock).

**Fix 2 (DONE 2026-06-21) — the dungeon06 case + the broader empty-room gap.** Fix 1 alone left 5 bosses
(`d05/06/08/09`, and any mob whose creature resolves to a non-importable base) un-spawnable because the
`creatures` import (`_is_creature`) drops every node under a `base` path segment — both the abstract library
AND the unique bosses / per-species bases that dungeon content spawns directly. Three coordinated imports now
close it (all in `creatures_importer.rebuild_creatures_table` + `dungeon_world_importer.build_mob_creature_map`):
- **boss/leader/quest ENTITIES** (`collect_world_boss_creature_rows`): a `world.*.mob.*` entity that flattens to
  a creature (`_root_extends==StockUnit`) with a MERGED `CreatureDifficulty` **or** numeric `Difficulty` is imported
  keyed by its world path (+ ~70 masters/quest mobs). The chassis DB additionally loads top-level `base/` and `npc/`
  so `…wheelerboss.base→base.RangedUnit→StockUnit` / Frump→`npc.OldMan` flatten.
- **★ ALL 13 bosses spawn AS their named entity** (`_boss_mob_types` = path `…mob.boss` or own
  `CreatureDifficulty=DUNGEON_BOSS`): a boss is imported + self-mapped **even when it resolves to a concrete**
  (Sissirat→`shaman.Poison.Hero`, Algor→`orokchieftain.Ice.Hero`, …). This is REQUIRED for synch: the spawn wire
  carries the boss *entity* (`world.dungeonNN.mob.boss`), so the client loads ITS overridden `Difficulty` (Sissirat
  11, Algor 25, Rotgut 25, Queen 30, Manglefeet 30) — if the server kept computing HP from the generic concrete
  (HERO 7.0) it would entity-synch-crash. Verified: d00 Rattle Tooth 574 HP → d09 Queen of Shadows 124 528 HP,
  scaling with dungeon depth × override `Difficulty`. Ordinary leaders/masters keep their (working) concrete.
- **referenced per-species bases** (`collect_referenced_base_creature_rows`): the 12 `creatures.<species>.base.*`
  (raythemale.base.melee ×86, orokchieftain.base.hero, …) that encounters spawn directly — imported by their
  real path (NOT the abstract `creatures.base.*` library, which stays excluded).
- **self-map** (`build_mob_creature_map`): an entity whose `extends` resolves to the abstract library, to `None`,
  or (for a stat-owning boss) to a per-species base is mapped to ITSELF so the encounter points at the imported
  entity. Safe even if an entity isn't imported — the spawner skips a creature it can't load (`monsters.py:284`).
- **★ the numeric `Difficulty` is the real HP multiplier** (not the tier string): the tier class only SETS its
  default (`UnitMelee_Champion.Difficulty=4.0`); bosses/leaders OVERRIDE it (Rotgut **25**, d16 Menacing
  Manglefeet **30**, Z'lash **5**). For **1255/1261** concrete creatures the numeric == `difficulty_modifier(tier)`
  (a no-op — dungeon00 RECRUIT 114 HP unchanged), so the server now threads the creature's numeric `Difficulty`
  (`MonsterData.difficulty_value` → `monster_hp_wire(difficulty_value=…)` → `calculate_hp` accepts a float) and
  the override mobs finally get HP the client validates against (Rotgut L26 ×25 = 35548 raw vs the wrong ×8 = 11375).
  Result: **299 951 / 299 985 encounter units (99.99%) resolve** to a present creature (the last 34 are a d03
  quest mob + 6 d08 masters with no difficulty basis). 1015 tests pass.
  **✅ numeric-`Difficulty` HP VALIDATED LIVE (2026-06-21):** warping into `dungeon06_level08_boss` spawned Rotgut
  (override `Difficulty=25`, HP 26661) and the entity-synch compare PASSED — the boss-spawn crash was a null-weapon
  ACCESS_VIOLATION, NOT a synch error. So the client does use the numeric override; keep `difficulty_value`.
- **★ boss-spawn crash FIXED (behaviour_type) — `net/monsters.py`.** 17 unique bosses (wheelerboss/Rotgut, …)
  extend the top-level `base.RangedUnit` whose Behavior roots at the RAW `MonsterBehavior2` (not a
  `creatures.base.behavior.*` class). Sent verbatim, the client instantiates the bare behavior which never wires
  up a weapon → its combat-stat setup `FUN_00535950` reads `[ebp+0x82]` on a NULL weapon (`FUN_00520530` returned
  0) → `C0000005` ACCESS_VIOLATION the instant the boss room loads (live, EIP `0x00535A1E`). Fix: at spawn,
  sanitise any `behaviour_type` that isn't a concrete `creatures.base.behavior.*` to one matched to the primary
  weapon kind (ranged→`.Ranged`, melee→`.Melee`, skill-only→`.Caster`). LIVE CODE — restart only. (d01/d09 bosses
  already had `.Caster`/`.Melee`, which is why only d06 crashed.)
- **★ boss/guard WEAPONS + SKILLS hooked up — `data/creature_manipulators_importer.py` (NEW).** The legacy
  `creature_manipulators` table (built by an absent tool) covered only ~1189 creatures, so the new bosses/bases +
  the d06 `basic` guards fell back to the invisible generic melee ("dungeon06 mobs miss weapons" / "bosses lack
  skills"). `import_missing_creature_manipulators` (run from `rebuild_creatures_table`, ADDITIVE — legacy rows kept)
  resolves each MISSING creature's merged `Manipulators`: PrimaryWeapon (gc + `WeaponClass`→range hint) +
  `CreatureSkill*`/bolt blocks (+246 rows). Result: **Rotgut → Mutant_WheelerBoss_Cannon (ranged) + WheelerBossCharge**;
  d06 guards → **2HGun / 1HAxe**; Sissirat → Poison bolt. The reader (`manipulators_for`/`_kind_of`) still gates to
  client-proven wire shapes, so unproven manipulators are skipped. Re-run `rebuild_content_tables.py --table creatures`
  (now fills manipulators) + restart. TODO: thread `LevelOffset`; honor `DoorsToOpenOnDeath`.
- **★ nested guard entities resolve — `build_mob_extends_map` now RECURSES.** Some mob files nest entities >1 level
  deep (`dungeon11/mob/boss_guard.gc`: `boss_guard { Poison { 01 extends creatures.shadowSpawn.Raythe.Poison.Hero } }`
  → `…mob.boss_guard.poison.01`). The old top+1-child scan dropped grandchildren, so d11's 4 poison boss-guards
  (Raythe/Oracle/Reaver/DarkAngel) never spawned — only the lone boss did. Now d11 boss room = boss + 4 guards.
  Needs `import_dungeon_worlds.py` re-run. NB: the *other* sparse boss rooms are content-faithful (d03/d09 author a
  SINGLE encounter marker; d09's master = just the boss) — NOT a bug. Regular-level density is the separate
  `DR_ENCOUNTER_BUDGET` knob (default 2.25 ≈ 2-4 mobs/spot).
- **Re-import (server stopped):** `python scripts/rebuild_content_tables.py creatures` (creatures → ~1392 rows)
  **then** `python scripts/import_dungeon_worlds.py` (dungeon tables). Restart.

### 14.5 Mobs wander too far from spawn — grounded behavior values; it's client-brain  **[T0 — extracter]**

Wander/aggro/leash are **authored** in the creature *behavior* GC, NOT server-side:
`creatures/base/behavior/Melee.gc` → `AgroRange=100`, `WanderRange=150`, `Perception=500`, `ShoutRange=300`,
`IdleAction=WANDER`, `Retreatable=true`. Observed range of authored values: `WanderRange` 0/10/50/100/150/200/500
and **`150000`** (×13 — "roam anywhere" mobs); `LeashRange` 180/200. The client's monster brain reads these from
the `behaviour_type` GCObject the server names in spawn OP3 (`net/monsters.py` `behaviour_type`) and wanders
client-side (per [[project_mob_spawn_refactor]] "wander = client-brain"). **Levers:** (a) make sure the spawned
`behaviour_type` resolves to the creature's intended behavior (a `WanderRange=150000` roamer behavior leaking onto
trash = unbounded wander); (b) the wander is anchored to the **spawn position** we send — a wrong/zero anchor
un-tethers it. A true server-side leash is not in the model; first confirm the sent behaviour_type + anchor
against a live `bp` on the client brain (`FUN_00580be0` mover-goal / `MonsterBehavior2::States @0x51BB30`).

### 14.6 Melee mobs "run through you" — collision, an already-open problem  **[T0 — cross-ref]**

Documented at `net/monsters.py:348-356` and [[project_mob_spawn_refactor]] (10th–12th): the mob entity ships
`worldEntityFlags=0x06` (no blocking bit). `StockUnit`'s swing-range virtual is a `RET-0xC` no-op (mobs have no
range gate) and the avatar has no collider, so the client brain drives the mob to the avatar's center =
"runs through." Both attempted fixes regressed: mob blocking `0x07` stops the avatar's *own* attack approach on
anchored/far mobs; avatar collider `0x05` self-collides/teleports the avatar. This is the same client-brain
approach-goal problem as §14.5; the DRS-NET-style **server-driven chase that stops at `weaponRange+radii`**
([[project_follow_combat_port]]) is the only lever that worked live — verify it's active for melee mobs.

**2026-07-02 recurrence "mobs behave until I attack, then run through" — round 1 (the §2 3× catch-up
hypothesis) DISPROVEN live: the run-through survived the ack fix.** Round 2 re-rooted it to THREE server
send-model gaps that all leave the client mover/action driving the mob into the avatar exactly when melee
starts (all fixed in `monster_ai.py` same day; **[T1 — live re-test pending]**):
1. **Chase corrections aimed at the player's CENTER** (`dest = tx,ty`): the client mover walks the mob ALL the
   way to dest (no range gate — the RET-0xC virtual, above); the stop-at-range existed only in the server's own
   stepping. Distant approaches masked it (contact flips to own-spot pins before the mob nears the center), but
   in melee the player dances across the ~16u ring and every re-chase correction lunges the mob ~11u/0.15s into
   the avatar. Fix: dest = the stop-ring point (`target − dir·effective_range`).
2. **The `0xF0` swing action REPLACES Follow permanently** (injection model, 2026-06-22): CreateAction swaps
   the mob's active action and Follow was only ever sent on target CHANGE — after the first swing the client
   runs AttackTarget2's no-range-gate approach. Fix: re-assert Follow between swings (≥0.4s after a swing so
   the animation isn't cancelled, throttled 1/s).
3. **The player's landed hits fire the mob's local OnDamaged**, which can displace its action state
   client-side. Fix: `aggro_from_attack` (fires on every player `0x50`) re-asserts Follow for an
   already-aggroed mob, throttled 1/s.

**★★ Round 2 fixes ALSO DISPROVEN live (2026-07-02): run-through unchanged. ⇒ server packets are COSMETIC for
these mobs — the client's local Follow action owns movement. Fix MUST be client-side (drhook). Ghidra RE this
session cracked the two blockers prior sessions lacked:**
- **The client-executed `Follow` action (class 0x16) IS the run-through driver.** Its per-frame update is
  **`FUN_00526f70`** (Follow vtable `PTR_LAB_0087a138` slot 42; ctor `FUN_00526e00` allocs 0xA4, factory
  `FUN_00526ad0`, class-reg `FUN_00526b30` name "Follow"). The update reads self + target world position,
  computes horizontal distance, and only switches a **gait MODE** (`follow+0x88` = 2 far / 5 close) at a
  hardcoded **30-unit** threshold (`distSq_fixed < 0x38400`; ×256 ⇒ 30u) — it **never stops**; both modes drive
  the mob to the target centre. That is the run-through. `Follow+0x6d` = mode byte from our packet;
  `Follow+0xA0/+0xA2` = 100 default (copied to/from mobUnit `+0xE0`, an approach param), NOT from the wire.
- **★ A unit's WORLD horizontal position is `unit+0x90` / `unit+0x94`** (int, fixed-point ×256) — the engine
  reads exactly these for BOTH the mob (self) and the avatar (target) in `FUN_00526f70`'s own distance math, so
  it is a shared base-class field, NOT avatar-only. This is the **"offset TBD"** that blocked the position-clamp
  in `combat_hook.c` (the avatar-only `+0x130` cache reads 0 for mobs). **[T1 — static-derived from the AI
  distance math; LIVE-VERIFY writing it moves the mob before shipping a clamp.]** Z is likely `+0x98` (unneeded
  for a horizontal clamp).
- **The DLL fix — BUILT 2026-07-02 (gated `DR_MOB_CLAMP=1`, needs `DR_MONSTER_AI=1`; live-test PENDING):** the
  per-frame pump drain (`inject_hook.c dr_inject_drain`, already game-thread with avatar+mob `Unit*` resolved by
  eid) pins each server-marked aggroed mob: read its `+0x90/+0x94` distance to the avatar and, if inside the
  stop-ring, rewrite `mob+0x90/+0x94` to `avatarPos + ring·dir` (push OUT only — natural approach untouched). It
  re-clamps every frame, so the client mover reclaims ≤1 frame of motion (~0.6u) past the ring — invisible. It is
  **SELF-VALIDATING**: on the first cached avatar it cross-checks `+0x90/+0x94` against the known-good
  `+0x130/+0x134` cache (`POS_VALIDATE_TOL` 8u); a mismatch DISABLES the clamp and logs both triples (so a wrong
  offset is a logged no-op, never a teleport/crash). Server side: `monster_ai._maybe_send_clamp` streams
  `OP_MOB_CLAMP [mob_eid][avatar_eid][ring_wire]` (`net/telemetry.send_mob_clamp`) for each aggroed mob on the
  chase cadence (0.2s); the hook ages a mob out `CLAMP_FRESH_MS` (500ms) after the last refresh. This is the hook
  doing what the server can't: authoritatively positioning the display mob at melee range. Tests: `test_monster_ai`
  + `test_telemetry` (server); the offset is STATIC-derived — the first live run's `[clamp]` DebugView line
  confirms VALIDATED vs the DISABLED-with-values fallback.
- **★ LIVE-CONFIRMED WORKING 2026-07-03:** mobs approach, stop at range, and deal damage — run-through FIXED; the
  `+0x90/+0x94` offset VALIDATED live. Enabling the clamp exposed two injection-coupling bugs, both fixed
  (`docs/MOB_ATTACK_INJECTION.md` Live fixes #3/#4, suite 1079):
  - **Swing animation cancelled by the player attacking (damage kept flowing).** The server's `Follow` re-assert
    (added to fight run-through *before* the clamp) replaces the mob's in-flight `0xF0` swing action client-side.
    With the clamp it is redundant + harmful → gated OFF when `MOB_CLAMP_ENABLED` (`monster_ai._aggro` re-assert +
    the in-contact restore; the initial aggro Follow for approach stays).
  - **Damage continued after the mob died.** The injection layer wasn't told of the client-local kill, so it kept
    applying server-streamed `MOB_ATTACK`/`MOB_CLAMP` (and `dr_on_damage` re-cached the dead mob). Fix: the combat
    detour calls **`dr_inject_forget_mob(victim)`** on every lethal blow — drops the mob's pending attacks + clamp +
    cache and block-lists its eid (`KILLED_BLOCK_MS` 3 s). Client = ground truth for mob death ⇒ instant, robust vs
    kill-telemetry lag.
  - **★ 2nd live round 2026-07-03: damage-after-death PERSISTED — root = v1 kill detection missed lethal blows.**
    The v1 detour predicted the kill BEFORE the original ran (`hp <= [Damage+0x38]` at entry), but a native
    (mode ≠ 4) hit is RECOMPUTED inside `Damage::apply` (`FUN_004f67f0` resist/crit), so the entry damage isn't
    what lands. A missed lethal blow was missed FOREVER (client reports no monster HP on any channel — the
    swing-suffix live-disproof): no forget, no KILL telemetry → the server ghost kept swinging and its own
    contact-pin `0x65` stream kept the corpse's `Unit*` cache-fresh → endless injected damage; that mob's loot/XP
    also silently dropped (both models). Fix: **`combat_hook.c` v2 post-apply kill detect** — the detour
    call-wraps the original (plain-RET/no-stack-args, proven by `dr_call_damage_apply`) and decides
    `hp_before > 0 && hp_after <= 0` from `target+0x2f0` AFTER apply; nesting-safe depth stack; both units
    re-validated via the engine's `FUN_005cb650`. DebugView marker: `v2 post-apply kill detect`.
  - Delivery is now `client_hook/d3d9.dll` only (drhook/drloader/dbghelp variants removed 2026-07-03).
- **★ Enroll + clamp model (`DR_MOB_ENROLL_CLAMP=1`, `DR_MONSTER_AI` off) — the animation fix.** The injection
  model drives the mob's swing with a server `0xF0` action, which the client's own hit-reaction cancels every time
  the player's damage lands (`Damage::apply` FUN_004f6580 dispatches damage events 0x1f5/0x1f6/0x1f7 to the target
  → the mob interrupts its action), and a display-only mob has no brain to resume it → "swing animation stops when
  I attack, damage continues" (live 2026-07-03). Fix = let the mob's OWN client AI run it (the deferred `0x64`
  enroll — the DEFAULT model), so attacks/animation/hit-recovery/movement are all NATIVE, and use ONLY the clamp
  to stop run-through. No injection, no `0xF0`, no Follow/chase — this is the original game's model + the clamp.
  `monster_ai._tick_enroll_clamp` streams `OP_MOB_CLAMP` for enrolled mobs (`simulated_by` non-empty) to the
  simulator's avatar; the actively-fought mob stays hook-cached via the combat detour (every player hit re-caches
  it). Needs the hook's `+0xE5` synch bypass for the client-simulated mob HP (the shim already installs it).
  `CLAMP_CAP` bumped to 64 for dense rooms. Tests: `test_monster_ai` enroll-clamp cases.
- **★ Enroll+clamp live round 1 (2026-07-03): mobs ran through on APPROACH — the passive cache was the hole,
  now closed with a LIVE ENTITY LOOKUP.** As predicted: in this model the server streams nothing about mobs, so
  the hook's eid→`Unit*` cache (fed only by the synch detour + `Damage::apply` events) was EMPTY until blows were
  exchanged → `clamp_mobs` couldn't resolve the approaching mob → the native brain drove it to the avatar's
  center before the first swing (and between swings the ~0.5 s freshness window lapsed → yo-yo). Fix
  (`inject_hook.c` **v2 live entity lookup**): the pump `FUN_005d9e30` is `__fastcall(ClientEntityManager*)` —
  the detour stows ECX into `g_entity_mgr` every frame, and lookups now call the engine's own
  **find-entity-by-id, manager vtbl `+0xC4`** (thiscall, u16 eid, callee-clean — the exact call the `0x05`
  entity-remove handler makes at `0x5dafa5`, which errors `"Invalid EntityID(%u)"` on NULL and zeroes `+0x80` on
  destroy, proving the returned object is the same Unit our offsets target). Properties: an approaching,
  never-hit mob resolves from frame 1 (clamp engages on approach); a DESTROYED entity resolves NULL (no stale
  pointers — strictly safer than the frame-stamped cache); returns are borrowed same-frame on the game thread.
  Consumers switched: `clamp_mobs` (+ corpse guard `hp+0x2f0<=0` → never pin a body), the injection drain
  (+ dead-attacker guard), `validate_pos_offset`. The passive cache remains only as the pre-first-pump fallback.
  DebugView marker: `pump hook installed — drain active (v2 live entity lookup via mgr vtbl+0xC4)`.
  **[live-test pending]**

**★ THE REAL FIX — server-driven mob→player damage injection (2026-06-22, design+build; live-test pending).**
The run-through↔can't-attack dilemma is structural: enrolled mobs attack but run through; server-chased mobs
stop at range but the client is packet-blind for their incoming damage (and the server can't assert avatar HP
— §6/§4). So the **client itself** must apply the damage, on the server's command. Full design + RE:
**`docs/MOB_ATTACK_INJECTION.md`**. Built this session (gated `DR_MONSTER_AI=1` + `DR_MOB_ATTACK_INJECT=1`,
OFF by default):
- **`Damage::apply` = FUN_004f6580** (`EDI = Damage*`, no stack args). The engine's universal damage sink —
  decrements target HP, fires OnDamaged/threat/death, shows combat text. Build+apply ref site `0x50c087..0x50c0f9`.
  **Damage object (0x44):** `+0x00`=vtable(`base+0x46f834`), `+0x04`=refcount 1, `+0x2c`=attacker, `+0x34`=target,
  `+0x38`=damage(wire ×256), `+0x3e`=**4 (exact — skips the `FUN_004f67f0` resist recompute → predictable damage,
  and avoids the branch needing valid `+0x14` sub-objects)**, `+0x3f`=element, `+0x41`=`0x18`. Ctor `FUN_004f64f0`
  just zeroes + sets vtable + `0x18`. `FUN_0047f050` (apply's tail) is only the floating-text/crit-sound display,
  guarded to the local player — NOT the HP sink; the decrement is a side effect of the notifications.
- **Server→client telemetry** `OP_MOB_ATTACK 0x80 [mob_eid][avatar_eid][dmg_wire][element]` (14 B); `monster_ai`
  emits it at the mob's authored cadence once the chase reaches contact, and keeps streaming a throttled in-contact
  synch so the hook has a per-frame drain trigger. `net/telemetry.py` is now bidirectional.
- **drhook drain on the per-frame pump FUN_005d9e30** (prologue `55 8B EC 83 E4 F8`, hookable) — NOT inside the
  synch detour (that would re-enter combat mid-decode). The synch detour only records `eid→Unit*` (frame-stamped;
  the drain uses only fresh entries → a dead mob is never dereferenced). `client_hook/inject_hook.c`.
- **Code-9 race FIXED (shipped)**: `monster_ai.purge_monster` removes a killed mob from `inst.monster_ids` and
  drops its queued chase/follow packets before the `0x05` destroy, so no stale `0x35` for a destroyed component
  reaches the client ("Invalid ComponentID"). Called from `combat._process_monster_kill`. No-op in the default model.

**★★★ 2026-07-04 (rounds 6e–6h): the CLAMP and COLLISION approaches are both CLOSED-DEAD [T0 — live]; the
real lever is the enrolled brain's chase→attack RANGE DECISION, not yet located.** Three infrastructure bugs
had silently kept the clamp from ever writing (a lazily-init'd `CRITICAL_SECTION` crashing/deadlocking zone
load — now init'd in DllMain; the telemetry socket connecting only on the first KILL so every clamp hit a dead
channel — now `dr_telemetry_start()` at DllMain; the self-validator false-negativing avatar `+0x90/+0x94`
against the death-time-only `+0x130` cache — now validated directly). With all three fixed the clamp finally
WROTE `mob+0x90/+0x94` live — and `drhook.log` recorded **REVERT ×109: the client mover overwrites the write
every frame**. `+0x90/+0x94` is a DERIVED OUTPUT of the mover. The mover chain was then caught live
(HW-write-BP on a chasing mob's `+0x90`): position setter **`FUN_0050bd40`** (writes `+0x90/+0x94/+0x98` from
ESI; drags the collider only if `+0xA0&1`) ← commit wrapper `0x51B2F0` ← **`FUN_005093e0`**, the mob
movement/anim update — which is **ANIMATION-ROOT-MOTION driven and performs NO collision-grid query** ⇒ a mob
stops only when its AI swaps the run animation → attack/idle animation (no forward root motion); colliders can
never stop a mob (collision only gates the PLAYER's input-mover — why mob flags `0x07` blocked the player's
approach). Live mob `+0xA0 = 0x406` (blocking bit clear). Ours swap run→attack at distance ≈ 0 (center) instead
of weapon range — the bug is the AI's range decision firing too late. `vtbl+0xbc = 0x404140 RET 0xC` re-confirmed
live, BUT `0x404140` is a **GENERIC shared no-op across many StockUnit vtable slots** (`vtbl+0xbc` AND
`vtbl+0x148` both point at it; an unconditional BP catch landed in `FUN_005689b0`, a modifier/stat applier) —
"vtbl+0xbc = swing range" is UNCONFIRMED as the run-through lever, and BPing the stub is too noisy to isolate the
caller. **Open levers (next):** (a) the mob CONTROLLER's per-frame update (`Unit → FUN_004d3bf0 → controller
vtable`) → find the distance-vs-range compare gating the run→attack action swap; (b) the chase MOVE-GOAL — if the
goal is the avatar's exact center, offset it by weapon range (suspect authored approach params we never ship:
`Follow+0xA0`/`mobUnit+0xE0`, default 100 raw = **0.39 u in ×256 fixed-point ≈ center**); (c) HW-write-BP the
action/state field to catch the swap and read distance (unit`+0x310` was NULL — real field offset unknown).
Fix shape (user-approved): runtime patch in `client_hook/d3d9.dll` (all mobs share vtable `0x872940`), or ship
the missing authored range DATA if the gate turns out data-driven. Clamp code retained but KNOWN-USELESS —
leave `DR_MOB_ENROLL_CLAMP` off. drhook.log Win32 file logger + REVERT instrumentation = keep.

**★★★★ 2026-07-04 (round 6i) — the run-through mechanism LOCATED live, and the inherited premise CORRECTED
[T0 — live x64dbg PID 15468].** Two decisive results from a live session (mobs actively fought):

1. **The mob-brain per-frame update is `FUN_004cfff0`** (controller class, vtable `0x868CD0`, at `unit+0xA4` via the
   `FUN_004d3bf0` getter), called from the world pump `FUN_005d9e30`. Captured by breaking the position setter
   `FUN_0050bd40` and walking the stack: `FUN_0050bd40` ← `FUN_0051B2F0` ← `FUN_005093e0` (animator update — this
   is the anim-root-motion applier, NOT the AI; its `param_1` is the *animator*, so the old "`unit+0x310`=action"
   was wrong) ← `FUN_00503ab0` (StockUnit `vtbl+0xE4`) ← **`FUN_004cfff0`** ← `FUN_005d9e30`. `FUN_004cfff0` runs a
   **fixed 30 Hz timestep** (`+0x124` accumulator, `0x21`=33 ms/step, `0x20`=32 ms threshold) — independently
   **confirms the dev's "at least 30hz"** (§15.5). It dispatches AI events through a state machine at
   `controller+0xc0` (vtable `0x8B0B0C`) and processes the **steering behavior at `controller+0x104`** (vtable
   `0x867354`). Controller decision fields: `+0x74`=aggro/sight range (live 360–400u), `+0x80/+0x84`=**not** the
   target (stay −1 while engaged — the target is held inside the state machine/steering, still unlocated).

2. **★ PREMISE CORRECTED — run-through is MOVER overshoot to centre, NOT "attack at centre".** Two independent
   live measurements, avatar stationary (identical coords each sample):
   - **The ATTACK range-gate WORKS.** Breaking `Damage::apply` (`FUN_004f6580`, `EDI`=`Damage*`; already detoured
     by the combat hook so break at `0x4F6598` past the `jmp`) filtered to a StockUnit attacker
     (`[[edi+0x2c]]==0x872940`) caught native swings (`Damage+0x3e`=1) landing from **50.6u, 22.4u, 26.4u** — never
     centre. `0x32`(=50) sits on the attack stack as a range value. So mobs DO gate their swing on range.
   - **The MOVER does NOT gate on range — it steers to the avatar's exact centre.** A proximity BP on the setter
     (StockUnit within ±16u of the avatar's *live* `+0x90/+0x94`) traced one mob's approach frame-by-frame:
     **15.4 → 12.96 → 8.54 → 6.17 → 3.96 → 2.36u**, monotonic, no stop at any weapon-range ring — it buried itself
     in the avatar (2.4u = model overlap = the visual "run-through"). ⇒ the run-through the user sees is the
     **approach overshoot**, while the swing fires opportunistically from wherever the mob is (≤ ~50u). The old
     "mob switches run→attack anim at distance≈0" model is wrong: the swing switch happens at range; the *mover*
     goal is the bug.

   **⇒ Fix target (precise): the steering behavior (vtable `0x867354`, `controller+0x104`) computes its goal as
   `target.position` with no range offset.** No static goal coordinate is stored in the steering object (scanned
   live) → the goal is `target.pos` read each frame. The native fix = make that goal stop at the ring
   (`goal = target − dir·attackRange`, attackRange ≈ the 50u the swing-gate already uses) via a `d3d9.dll` hook on
   the steering update, OR find/ship the authored stop-distance the retail steering used. **NEXT: analyze vtable
   `0x867354`'s per-frame update to find where it reads `target.pos` into the goal, and offset it.** This supersedes
   the clamp (writes `+0x90` = fought by the mover) and collision (mover does no collision query) dead-ends — it
   patches the *goal*, upstream of both. Ghidra fns created this session: `AttackTarget2_Update` (0x524510),
   `FUN_004cfff0`/`FUN_004cf1a0` region = the mob controller class.

3. **Controller architecture (mapped, for the goal-setter hunt).** Controller ctor `FUN_004cf1a0` builds: event
   emitter at `+0xc0` (`0x625xxx` signals/slots, vtable `0x8B0B0C`), PathMap pathfinder at `+0x104` (vtable
   `0x867354`, strings `"PathMap::Build"`/`"SeedPoint(%.3f,%.3f)"`), + list managers `+0x42`/`+0x43` (vtbls
   `0x876c6c`/`0x89201c`) and a 0xB6C object (`FUN_005dddb0`). The tick `FUN_004cfff0` applies movement in the
   TOP call `(*(holder)+0x98)()` (→ unit `vtbl+0xE4` animator → root motion → setter), then the 30 Hz loop
   dispatches events `0x7d3`(2003)/`0x7cf`(1999) via the `+0xc0` emitter — the **chase decision (face target +
   pick run vs idle/attack anim) is a slot subscribed to those events**, so it's decoupled from both the apply
   and the tick. The attack path off the Damage stack (`FUN_004db070` @0x4Dxxxx) is the controller's **spatial
   grid scan** (grid cells 640/1280u over bbox `+0x20..+0x25` → `FUN_004db3f0`/cell → attack), i.e. "is a target
   inside my scan box," NOT a clean chase-range gate. ⇒ the chase stop-distance is inside the event slot; locating
   it statically means tracing the signals/slots `connect()`s (hard) — **live single-step of `FUN_004cfff0`'s
   event dispatch for a chasing mob is the tractable route**.

**Fix paths (2026-07-04, decide next):** (A) **pure-AI native** — crack the event-slot chase handler and its
stop-distance, patch it to weapon range (respects the goal-prompt "client's AI does the work," but is a deep
multi-session trace). (B) **mover-INPUT clamp** — hook `FUN_0050bd40` and clamp its *incoming* new-pos to the ring
`avatar + dir·range` before it commits. This is CATEGORICALLY DIFFERENT from the round-6e dead-end (which wrote
`+0x90` *after* the mover and got reverted 109×): here the mover writes the clamped value itself, so there is NO
revert — it would work, at the cost of mild foot-slide as the run anim plays against a pinned position. It is still
a per-frame position modification (goal-prompt forbade the *futile* variant; this one isn't futile — the user's
call whether the letter of "no position writes" still applies). (C) server-driven movement (`DR_MONSTER_AI` stops
at range) + separately solve the injection anim-cancel. Recommendation: try **B** for a working fix now (small,
shippable, non-reverting), keep **A** as the "truly native" goal.

**★ B BUILT + DEPLOYED 2026-07-04 (user picked B).** `inject_hook.c`: new detour on the setter `FUN_0050bd40`
(RVA `0x10BD40`, prologue `55 8B EC 83 E4 F8` — same as the pump) → `_detour_setpos` → `dr_clamp_setpos(unit,
newpos)`: for a server-marked aggroed StockUnit whose intended `newpos` is inside the ring, rewrite `newpos` OUT
to `avatar + dir·ring` before the setter commits (mover writes the clamped value → no revert). Reuses the existing
`g_clamp`/`g_avatar_eid`/`g_pos_validated` infra; the drain's `prep_clamp()` snapshots fresh clamp targets +
resolves the avatar once per frame into a game-thread struct the hot detour reads lock-free (no lock/engine-call
in the setter path). The old post-mover `clamp_mobs()` write is DELETED. Inert unless offset-validated + a fresh
`OP_MOB_CLAMP` target + StockUnit (safe during zone load: `g_pos_validated==0` → immediate return). Ring =
`effective_attack_range` = `attack_range(8)+mob_radius(5)+avatar_radius(3)` = 16u melee. Built clean (md5
`e896147b…`, 109785 B), asm verified (`mov 0x14(%esp)`=unit / `0x8(%esp)`=newpos / `jmp *g_trampoline_setpos`),
deployed to `Client 666/d3d9.dll` (old → `d3d9_old_pre6i_*.dll`). **ENABLE: `DR_MOB_ENROLL_CLAMP=1` (enroll model
= native anims+damage) + restart client. Watch: "setpos clamp installed" at launch; `[clamp] ... clamp ACTIVE`;
`[clamp] sum active=N hits=M smp dist≈4096(=16u ring)`. LIVE-TEST PENDING.**

**★ 2026-07-07 (round 6j, static Ghidra + authored-data survey) — the round-6i "NEXT" scoped tighter; the
goal-write is NOT in vtable `0x867354` [T0 static].**
- **`0x867354` is the PathMap/PathManager vtable**, not a steering-behavior class (slots `0x4C3810, 0x404260,
  0x4C48B0, 0x627AA0, 0x627B60, 0x5148D0(=RET no-op), 0x5DCD10×2, 0x627AB0`; the `PathMap::Build` strings sit
  right after it). The 30 Hz controller loop (`FUN_004cfff0`) calls **`FUN_004c3d40`** on it (next to
  `PathManager::ReadBudget FUN_004c3cc0`, budgeted by our `0x0D` PathManager fields) — decompiled, it is a
  path-**REQUEST queue pump** (per-request budget stepper `FUN_005d2af0`, completion dispatch `FUN_004c3fc0`).
  Goals are stored per-request at creation; there is **no per-frame `goal = target.pos` write to hook in the
  pump**. ⇒ the goal write lives in the **chase event slot** (event `0x7d3` dispatched via `controller+0xC0`
  each 30 Hz step) or in the request-submit it performs — the live single-step of that dispatch remains the
  route; don't waste a session hooking the PathMap vtable.
- **Follow mirrors `targetUnit+0xE0` (u16) → `Follow+0xA0` EVERY update** (`FUN_00526f70`), and the sibling
  methods `Follow_m43`/`FUN_005271a0` change-detect it against the `+0xA2` latch and call `target vtbl+0x118`
  = `FUN_00509880` = **stat-block invalidation** (memset `unit+0xC8..+0x2F0` + events 0x17/0x18 + clears
  `+0xA0`-flags bit 15). So `unit+0xE0` (default 100) is a live-propagated TARGET parameter — semantics
  unconfirmed (scale%? approach radius?). **5-min live probe:** poke the avatar's `+0xE0` 100→4096 while a mob
  chases: stops-at-ring ⇒ it IS the approach param (then find/ship its authored source); avatar resizes ⇒
  scale, revert and move on. (The memset ends at +0x2F0 exclusive — avatar HP untouched.)
- **`AttackTarget2_Update` (0x524510) is a stub** (gait mode 0 + dirty bit) — approach during attack is 100%
  controller/steering, confirming the event slot is the single decision site.
- **Authored data DOES carry the ranges** (extracter, tier-2): `MonsterBehavior2Desc` = AttackType, IdleAction,
  Perception (200–500), AgroRange (100–300), ShoutRange, WanderRange, FleeRange (ranged 40), Retreatable,
  CollisionPriority; casters author **`AttackRange = 150`** in the unit Description; melee range rides the
  weapon manipulator (`base.MeleeUnitWeapon` ID 10 — which we already ship). So the client-side data needed to
  stop at range exists; the open question is only which field the chase slot's compare reads (weapon
  manipulator vs behavior desc vs a value the native in-process server set — §15.6's two readings).

**★ 2026-07-07 (round 6k) — enroll + setpos-clamp LIVE result [T0 — drhook.log session analysis]: run-through
pinned, but the clamp BLOCKS the attack animation.** drhook.log (append-mode, uptime timestamps — split sessions
on timestamp RESETS before reading!) for the live run: **zero `[inj] APPLY`** (server flags correct this time —
no injection), **one `[clamp] RX NEW` burst** (86 targets, single timestamp = the first swing's deferred-enroll
wake; the 07-06 one-shot gate HELD — no per-swing re-enroll), **`hits>0` immediately and throughout** (the
mover-input clamp engaging = the "mobs collide into my player model" feel the user reports; ring 4096/3584).
User-visible state: mobs chase to the ring, **deal native damage, but NO attack animation**. Mechanism (fits all
prior T0): the brain's attack ACTION (which carries the anim) only triggers at ~centre (round 6h), while the
damage-swing gate fires opportunistically from ≤~50u independent of the action (round 6i's 22–50u landings) —
the ring now makes the centre unreachable, so the mob is stuck in chase gait (foot-slide) dealing anim-less
gate damage. ⇒ the clamp converted "run-through + attack anims in your face" into "wall + anim-less damage";
BOTH are the same root cause: **the chase→attack decision distance (≈0 instead of attack range)**. The round-6j
fix (chase-slot threshold → attack range) cures both at once and retires the clamp. Interim trade: unset
`DR_MOB_ENROLL_CLAMP` = anims back at the cost of run-through.

**Round-6k prep — the DFC event dispatch is fully mapped [T0 static]; the chase handler is one LIVE MEMORY READ
away (no breakpoints needed).** In `FUN_004cfff0`: `PUSH 0x7d3 @0x4d0070` → event ctor `FUN_004bc5f0` (event:
`+0x00`=id u32, `+0x1C`=payload ptr passed in EDI = the CONTROLLER, `+0x20`=flags) → **dispatch `CALL EAX
@0x4D008E`** with this = the emitter EMBEDDED at `controller+0xC0` (vtable `0x8B0B0C`, slot0 = dispatcher
`FUN_006257B0`; the 1999/0x7CF once-per-tick dispatch is `CALL EAX @0x4D00DB`). Dispatcher layout: emitter
`+0x0C` = registry ptr R, `+0x10` = flags (bit0 dispatching, bit1 pending-removals); `[R+4]` = doubly-linked
sentinel; node = `{+0 next, +4 prev, +8 event-id FILTER (0 = wildcard), +C SLOT ptr (0 = tombstone)}`; matching
nodes invoke `slot->vtbl[0](event*)` (DFC delegate — expect obj/mfn words at slot+4/+8). Byte-search proved NO
static `PUSH 0x7d3` subscription site exists (only the dispatch itself; other d3070000 hits are rel32
displacements) ⇒ handlers subscribe wildcard or table-driven. **Live recipe:** mob `Unit*` straight from
drhook.log `cache NEW` lines (no debugger catch) → `controller=[unit+0xA4]` (verify vtbl `0x868CD0`) → emitter
`controller+0xC0` (verify `[emitter]==0x8B0B0C`) → walk `[emitter+0xC]` registry → collect (filter, slot,
handler=[[slot]], slot+4, slot+8) → decompile handlers statically → find the distance-vs-range compare.

**★★ 2026-07-08 (round 6l, LIVE x64dbg attach PID 11848, read-only, game never paused) — round-6j premises
CORRECTED; the event-slot route is DEAD and the controller is SHARED [T0 — live].** Attached to the running
client mid-fight, resolved live `Unit*`s straight from `drhook.log` cache lines (no breakpoints):
1. **The controller/brain is SHARED per behavior-type, not per-mob.** Three distinct mobs (eids 1544/1734/1739,
   same spawn group) ALL have `[unit+0xA4] = 0x0D1D1940` (one controller object, vtable `0x868CD0` confirmed
   live). `FUN_004d3bf0` confirms why: `+0xA4` is a *cached* controller resolved from `unit+0x88 → +0x14` (the
   shared behavior source) with a layer/mask filter — every mob of a type resolves the SAME brain. This is the
   dev's "the logic was shared by all sim members" (§15.6) in the binary: `FUN_004cfff0` is a per-behaviour-type
   tick that iterates its unit list (`+0x14`) + path-request queue, NOT a per-mob update. (Consequence: a hook
   that patches "the controller" patches ALL mobs of that type at once — fine for a global range fix.)
2. **The `controller+0xC0` event emitter has a NULL registry** (`emitter+0x0C = 0` live) → the `0x7d3`/`0x7cf`
   dispatches in `FUN_004cfff0` are **no-ops**; nothing subscribes. ⇒ **the round-6j "chase decision is in the
   `0x7d3` event slot" route is DEAD.** The steering runs entirely through the **PathMap at `controller+0x104`
   via `FUN_004c3d40`** (called unconditionally each 30 Hz step) — a path-**request queue pump** (per-request
   stepper `FUN_005d2af0`; each request holds its unit at `req+0x10`/`[4]`, a goal-cell latch at `req+0x34`, and
   a target-cell source at `**(req+0x88)`; the `**(req+0x88) != req+0x34` test = "did the goal cell move?").
   ⇒ the goal is set **per-path-request at submission time**; the fix site is **whoever writes the chase
   request's goal from the target position** (offset that write by attack range), found by static RE of the
   request-submit path, NOT a live event-slot single-step.
3. **`unit+0xE0` is a CombatStats field, NOT an approach param — the round-6j `+0xE0` probe is INVALID.** Live
   `avatar+0xE0 = 0x6B (107)`; it sits inside the CombatStats block (`0xC8 + enum·4`, enum 6 — bible §10) that
   `FUN_00509880` memsets (`+0xC8..+0x2F0`) on invalidation. Follow copies `targetUnit+0xE0 → Follow+0xA0` and
   the `Follow_m43` change-detect calls the TARGET's `vtbl+0x118` = `FUN_00509880` = **stat-block recompute** —
   so `+0xE0` is a stat the mob caches off its target, not a steering distance. Writing it would be overwritten
   on the next recompute (and needlessly blank the avatar's CombatStats). **Do not write `+0xE0`.** (Probe
   cancelled before any write — confirmed statically + live read only.)

**⇒ Round-6l corrected next step:** static-RE the PathMap chase-request SUBMISSION (who calls into
`controller+0x104`'s PathMap to set a request goal = `target.pos`), offset that goal by attack range. The shared
controller means one d3d9 hook there fixes every mob of the type at once. Live single-stepping is no longer
needed to *locate* it (the event-slot theory that required it is dead); it's a static data-flow trace from the
PathMap request struct's goal field (`req+0x34` / `req+0x88` source) back to its writer.

**★ Round-6l continued — the shared-controller object mapped live, more dead ends closed, and the problem
RESTATED precisely [T0 — live-read layout + Ghidra].**
- **Live controller object (`0x0D1D1940`, vtable `0x868CD0`), key fields** (ctor `FUN_004cf1a0` default → live
  value; live values are the *authored-data* overwrites, so these are the type's tunables):
  `+0x74` aggro/perception range `0xC800`(200u) → **`0x19000`(400u)**; `+0x80/+0x84` target-cell X/Y
  `-1/-1`(idle) — set when engaged (per-unit scratch, since controller is shared); **`+0x98`** `0x32`(50) →
  **90**, **`+0x9C`** `0x19`(25) → **100** (data-driven range params, exact role still unproven — candidate
  stop/approach distances); `+0xC0` embedded event emitter (registry `+0xCC`=NULL → dead); `+0xF4` =
  **ClientEntityManager** (holder; tick calls its `vtbl+0x98` = the per-unit iteration, with the controller
  stashed in TLS[1]); `+0x104` = **PathMap** (`0x204754B0`, vtable `0x867354`, back-ptr `+0x10`→controller;
  request list header `+0x3c`, **empty when idle** = per-mob goals are transient requests); `+0x124` 30 Hz accum.
- **Dead ends closed this round:** `FUN_004c16a0` (a `+0x9c` reader in the `0x4c` region) = the **MINIMAP
  renderer** (strings "MiniMap"/"mapicon_*"), not chase. `+0xE0` = CombatStats (ruled out, above). Event slot =
  dead (above). Clamp + collision = dead (rounds 6e/6f).
- **★ The problem, restated precisely (supersedes "find the goal-write"):** the mob's *damage* swing already
  gates on weapon range (fires 22–50u, round 6i; the `0x32`=50 range value on the attack stack). What fires too
  late is the **attack ACTION/anim trigger** — it starts the attack animation only at ~centre (≈0–2u), which is
  why (a) pre-clamp: mobs run to centre and attack-anim in your face, (b) enroll+clamp: the 16u ring makes centre
  unreachable → damage still lands (its gate is ~50u) but the attack anim never starts (its trigger is ~0u).
  Same single bug both ways: **the chase→attack-action trigger distance is ~0 instead of weapon range (~50u).**
  The fix = move that trigger out to the weapon range the *damage* gate already uses; then the mob stops at range,
  plays the attack anim there, and the clamp is retired. The per-unit chase/attack update reached from the tick is
  `FUN_004cfff0 → ClientEntityMgr::iterate(vtbl+0x98) → unit vtbl+0xE4 FUN_00503ab0 → FUN_005093e0 (animator
  root-motion) + FUN_00503650 (action-state timers)`; the action-START is `FUN_00536190`/anim-setter `FUN_0050af30`
  (from the anim-cancel RE). **Next live experiment (post-zone-load BP, auto-removed before any transfer):** BP the
  attack-action START filtered to a chasing StockUnit, read its distance-to-target at trigger time + the range
  value it compares against — that catches the trigger threshold AND its source field in one hit, without the
  zone-load-freeze risk of a load-time BP.

**★★ Round-6m (2026-07-08, LIVE conditional BP on `Damage::apply`, mob-attacker filter, auto-removed before any
zone change) — the "damage but no attack animation" mechanism PROVEN at the code level; damage is WEAPON-timer
driven and ACTION-INDEPENDENT [T0 — live].** Set a conditional SW BP at `0x4F6598` (inside `Damage::apply`, past
our d3d9 detour's `E9` jmp; `EDI=Damage*`) with condition `[[edi+0x2c]]==0x872940` (StockUnit attacker) — fires
only when a mob swings, caught one cleanly, released + cleared immediately. Live capture:
- **The swinging mob was in the LOCOMOTION action** (`unit+0x310` → action obj vtable **`0x866090`**), NOT an
  attack action — yet it applied damage (Damage obj: attacker=StockUnit `+0x90`(-558.7,-204.8,30), target=avatar,
  dmg 1405 wire, `+0x3e=1` native). Mob↔avatar = **15.9u = the 16u clamp ring**. ⇒ **confirms** the enroll+clamp
  artifact: pinned at the ring, running-in-place (locomotion → foot-slide), dealing damage, no swing anim.
- **The damage is driven by the MeleeWeapon manipulator's auto-swing, not the body action.** Stack walk from the
  BP (past our d3d9 hook frames `0x6Exxxxxx`) → real caller `FUN_00597e50` = the **weapon swing RESOLVER**
  (attack roll + defense/parry flags + damage roll via the MT `FUN_0044b1f0`, allocs the `0x44` Damage obj, sets
  attacker/target/dmg, calls `Damage::apply`). Its caller `FUN_005921c0` (builds swing params) is a **vtable
  method at `0x8935B0`** whose class RTTI string is **"MeleeWeapon…"** ⇒ the weapon manipulator auto-swings on
  its own cadence timer whenever the target is within weapon range (`0x32`=50 seen on the swing stack — round 6i's
  50u), decoupled from the unit's animation action. This is why the ~16u clamp never stops the damage.
- **⇒ Complete, proven model:** (weapon) auto-swings for DAMAGE at ≤~50u, cadence-timed, action-independent;
  (unit action) the ANIMATION — locomotion vs attack — is driven by the shared-controller AI, which only enters
  the attack action on ARRIVAL at its goal (the avatar centre). Native (no clamp): mob runs to centre → AI enters
  attack action (anim) + weapon swings = both visible, but it ran THROUGH you (stop-dist ≈0). Enroll+clamp: ring
  blocks centre → AI never enters attack action → locomotion foot-slide + anim-less weapon damage. **ONE root:
  the AI chase stop-distance is ≈0 instead of weapon range.** Fix (unchanged, now fully justified): set the AI's
  chase→attack stop-distance to the weapon range (~50u, the value the weapon swing already uses); then the mob
  stops at range, the AI enters the attack action THERE (animation) + the weapon swings, run-through is gone, and
  the clamp is retired. The stop-distance lives in the shared-controller per-unit chase decision (round 6l: goal
  per PathMap request); its exact instruction is the sole remaining unknown — candidate source = the MeleeWeapon
  range field (~50u) the swing already reads, or controller `+0x98`/`+0x9C` (live 90/100). Next session: find
  where the MeleeWeapon range is stored + where the AI chase reads its stop-distance, and equalise them (ship the
  data, or one d3d9 hook on the shared controller = fixes all mobs of a type at once).

**★★★ Round-6n (2026-07-08, LIVE before/after capture + user A/B test) — BREAKTHROUGH: the CLIENT'S NATIVE BRAIN
already runs mobs CORRECTLY; our ENROLL+CLAMP is what BREAKS them. The whole apparatus is fighting a brain that
works. [T0 — live, decisive].** User running `DR_MOB_ENROLL_CLAMP=1` only. Two captures via the `Damage::apply`
mob-attacker BP (auto-removed), plus the user's own input A/B test:
- **PRE-swing (un-enrolled, native): mob behaves CORRECTLY** — user: "mobs chase, approach, and attack me as they
  should, everything works." Live: engaged mob at **~5u (melee-adjacent)**, action `0x866090` (locomotion), anim
  playing, weapon-timer damage. Far mobs (>400u aggro range) = NULL action idle. ⇒ the client's own brain
  proximity-aggros, chases to melee, and attacks with animation **without any server enrollment**. Mobs were
  NEVER "passive display-only" — that inherited premise (behind the entire enroll/inject/clamp program) is WRONG.
- **POST-enroll (user click-attacked a mob → `0x50` UseTarget → `enroll_instance_monsters` → `0x64` burst +
  clamp streaming): mob BREAKS** — live: SAME action `0x866090` (the `0x64` did NOT swap it to Follow), but now
  pinned at **~15.7u = the 16u clamp ring** instead of 5u melee. The mob can't reach its melee position → foot-
  slides (locomotion running against the pin) and never plays the adjacent-melee attack anim, while the weapon
  (range ~50u > 16u) still deals damage = the exact "damage but no attack animation / runs at me" symptom.
- **★ User's decisive A/B input test:** **Shift+left-click** (swing, no target-lock → no `0x50` UseTarget → no
  enroll) = mobs stay CORRECT; **click-on-mob attack** (target-locked `0x50` → enroll) = mobs BREAK. Isolates
  ENROLLMENT as the trigger, and the capture isolates **the CLAMP (16u ring) as the actual breaker** (action
  unchanged; only the clamp-forced distance changed 5u→16u).
- **⇒ THE FIX (paradigm shift, dev-aligned §15 "mobs run in the shared sim, client-driven"): stop fighting the
  native brain — DISABLE ENROLL + CLAMP + INJECTION entirely and let the client run the mobs.** The run-through
  we chased for 15+ rounds was an ARTIFACT of our own enroll/inject/clamp, NOT the native behavior (native stops
  at 5u melee, no run-through). The 16u clamp ring was "fixing" a run-through that only existed because of
  enrollment. Immediate live test: **unset `DR_MOB_ENROLL_CLAMP` (run with NO mob flags), restart client, then
  click-attack** — the clamp gate (`monster_ai.py:347/630`, `MOB_CLAMP_ENABLED or MOB_ENROLL_CLAMP_ENABLED`)
  goes false so no clamp streams; enroll still fires (`movement.py:506`) but the capture showed its `0x64`
  leaves the action as native locomotion, so mobs should stay correct at 5u. If click-attack still breaks with
  no clamp, the residual `0x64` enroll is also harmful → gate `enroll_instance_monsters` off too (pure native).
- **Open (verify next): mob-HP synch safety without enroll.** Enroll's other job was marking the mob
  `simulated_by` so the server never asserts its HP (Regime B, §6). Native un-enrolled mobs are ALSO
  client-simulated (their brain runs them) → the server must likewise never send their HP. The user's shift+click
  mobs took damage with no crash, suggesting it's already safe, but confirm the server isn't broadcasting
  authoritative HP for un-enrolled-but-client-simulated mobs before shipping the enroll removal (`hp_broadcast` /
  `combat.broadcast_monster_hp` gating). This is the one thing to check so removing enroll doesn't reopen an HP
  synch crash.

**★★★ LIVE-CONFIRMED 2026-07-08: `DR_NATIVE_MOBS=1` FIXES combat.** User set the flag (server-only restart,
client + `d3d9.dll` untouched) and click-attacked mobs — the run-through is GONE and mobs behave correctly
(native chase-to-melee + attack anim). The 15+-round run-through saga is resolved by DELETING our band-aid, not
adding one: the client's own monster brain was always correct; every enroll/inject/clamp model was fighting it.
`broadcast_monster_hp` being unwired means the no-enroll path is HP-synch-safe (borne out live — no crash).
**The fix is `net/movement.py` no longer enrolling on the `0x50` attack.** ★SHIPPED AS DEFAULT 2026-07-08 (user:
"everything works make it default"): native is now the unconditional default — the `0x50` path does nothing to
the mobs (client brain owns aggro/chase/attack/kill; kill/loot/XP confirmed working via the telemetry hook). The
old enroll is retained only behind opt-in `DR_LEGACY_ENROLL=1` (`monster_ai.LEGACY_ENROLL_ENABLED`), alongside
the other retired-but-retained debugging models (`DR_MONSTER_AI`, `DR_MOB_*`). Tests
`test_control_reset.test_0x50_attack_is_native_by_default` + `…_enrolls_with_legacy_flag`; suite 1093. The
`d3d9.dll` hook stays for its `+0xE5` synch bypass + kill telemetry only (clamp/inject paths now dead). Optional
later cleanup: physically delete the dead enroll/inject/clamp code + the `d3d9` clamp hook (currently inert).

---

### 14.7 The combat-model ladder — end-to-end state (2026-07-03)  **[T0/T1 consolidation]**

Every mob-combat lever we have tried reduces to ONE structural dilemma (§6, §14.6): the client's own mob
brain attacks + animates + damages natively **but drives to the avatar's center** (no range gate in this
build: `StockUnit` swing-range virtual = RET-0xC no-op, Follow update `FUN_00526f70` never stops, avatar has
no collider); while server-driven mobs stop at range **but cannot hurt the player** (client is packet-blind
for display-mob damage — §6-LIVE.4 — and the server may never assert avatar HP — §4/§6). Every model below is
one choice about which half to keep and how to plug the other half. The **hook levers** (kill detect v2
post-apply on `FUN_004f6580`; live entity lookup via manager vtbl `+0xC4`; stop-ring clamp on `+0x90/+0x94`;
`+0xE5` synch bypass) apply to all of them.

| Model (flags) | Mob brain | Mob→player damage | Anim/recovery | Run-through | Verdict |
|---|---|---|---|---|---|
| **Deferred-enroll** (all flags off) | client (0x64 burst on 1st attack) | native client sim | native | **YES** (brain has no range gate) | the pre-clamp default; unpatched-safe |
| **Server AI + injection** (`DR_MONSTER_AI=1`+`DR_MOB_ATTACK_INJECT=1`+`DR_MOB_CLAMP=1`) | server (display mobs, Follow/chase/0xF0) | hook injection (`OP_MOB_ATTACK`→`Damage::apply`) | **broken**: player's hit-reaction cancels the 0xF0 swing; no brain to resume (live 2026-07-03, unfixable server-side) | fixed by clamp | keep as fallback only |
| **Enroll + clamp** (`DR_MOB_ENROLL_CLAMP=1` ONLY) | client (native) | native client sim | native | **NOT fixed — clamp PROVEN DEAD 2026-07-04** (the mover REVERTs every `+0x90/+0x94` write; §14.6 rounds 6e–6f) | clamp dead; **enroll stays the native brain model** — active work = the native range-gate fix (§14.6 round-6 block) |
| DRS-NET road (reference): full server sim + RNG-ledger prediction of client rolls | server | server-computed, HP predicted bit-exact | server-driven actions | server chase stops at range | §6-LIVE.6 says draw-position prediction is infeasible headless; DRS-NET papers over it with fallback heuristics — do NOT port |

**State 2026-07-04 (round 6i):** the only run-through-free model that WORKS live today is Server AI + injection
(with its unfixable anim-cancel defect). The target model is **deferred-enroll + a native MOVER-goal fix**: §14.6
round 6i live-proved the mob's *swing* already gates on range (lands from 22–50u), but its *mover* steers to the
avatar's exact centre (measured 15.4→2.36u, no ring) — so the fix is the **steering goal** (vtable `0x867354`,
`controller+0x104`: `goal = target.pos` → make it `target − dir·range`), not a swing-range gate. Position clamps
and collision data are both live-disproven dead ends. §15.6 (dev testimony) says mob runtime ran inside the shared
sim — retail mobs stood at range, so the ring stop-distance exists as data or in the shared steering the headless
server also ran.

Open (not blocking solo combat): find the chase event slot's distance-vs-range compare and its threshold source —
round 6j proved vtable `0x867354` is the PathMap request pump (goals per-request, nothing to hook there); the
decision lives in the `0x7d3` event slot off `FUN_004cfff0`, so live single-step that dispatch (§14.6 round 6j);
MP relay of enrolled-mob HP to displayers (client sends no monster HP — §6-LIVE.12).

---

## 15. Original-developer testimony (2026-07-02) — the native sync architecture  **[DEV]**

> **Tier [DEV]** (see §0): first-hand recollection from an **original Dungeon Runners server
> developer** (conversation, 2026-07-02). This outranks every emulator [T3] for **design
> intent**, but it is ~18-year-old memory — he corrected himself mid-conversation on the RNG
> (LCG → Mersenne-Twister) and flagged it himself: *"Rusty memory is rusty memory!"*. Treat
> architecture claims as strong T1-grade guidance; **verify specifics against the client
> [T0] before shipping load-bearing logic.**

### 15.1 The model: one synchronized simulation; the server was a HEADLESS CLIENT

- *"It's a synchronized simulation. The client checks simulation state and craps out if a
  desync occurs."* — confirms §1's dual-simulation model and §4's zero-tolerance gate.
- ★ *"In the original code, **the server was pretty much a headless client with some extra
  systems enabled**."* — the native server satisfied the 1:1-emulation requirement (§1
  mechanism 3) by **running the entire client simulation**, not by re-implementing formulas.
  This is WHY partial re-simulation is infeasible for us (§6-LIVE.6): we don't run the
  client's frame loop, anim-driven AI draws, or proc rolls. Do not try to close that gap
  piecemeal; our Regime-B relay posture (§6) stands.
- *"The clients and server all share the same simulation state. Once synchronized, **no data
  needs to be exchanged aside from input / server-authoritative actions** (i.e. loot drops,
  spawns, etc.)."* — confirms §5 (no ping / no reconciliation) and the lockstep framing (§6b).
- Validation: *"My recollection is that the sim is validated via RNG. Client expects a
  certain value, doesn't get it, freaks out. The specifics there may be wrong, but it's in
  the ballpark."* — consistent with the HP synch compare (§4) as the visible gate.
- *"We had special builds of the client and server that generated detailed logs of simulation
  state to debug divergence. It was a pain in the ass."* — divergence debugging was hard even
  for the original team; our x64dbg/drhook telemetry is the same tool re-invented.

### 15.2 RNG — Mersenne Twister, ONE stream per zone, and late-join sends the FULL STATE

- First recalled *"a simple LCG… custom but the constants were standard"*, then corrected:
  *"Mersenne-Twister sparks some recollection. Aye. That sounds more like it. Some small
  buffer for RNG state."* — our T0 finding stands: MT19937 at `(entitymgr)+0x44` (§3).
- *"I recall there's **one RNG per zone**. Having more than one would be overly complex."* —
  matches the per-entity-manager MT being the shared zone RNG (§3, §6-LIVE.6).
- ★★ *"**The current state was sent. Sending only the seed would require a full replay from
  the start of the zone creation. And that would be madness.**"* / *"The entire MT state had
  to be transferred."* — the native late-join model transfers the **full MT state (buffer +
  index) as of frame X**, not a seed. Our `0x0C` seed override (§3) is correct for a client
  present at zone creation, but a **late joiner needs the CURRENT MT state** or its sim
  diverges from peers that already consumed draws. MP design consequence: on join, either
  transfer MT state (find/build the client's receive path for it — **[open]**) or accept
  per-join zone-RNG reset for all members.

### 15.3 Zone join = full state dump @ frame X + the message stream after X

- *"A client joining a zone gets the current state for every entity, the RNG stream, and
  buffered messages from that point in time. It plays the sim based on that distributed
  input."* / *"So, current state of RNG and all objects as of frame X. Stream of inputs /
  server-authoritative messages after frame X for any joining client."*
- Catch-up/backlog: *"just a TCP connection"* — no special recovery protocol.
- ⇒ **[T2]** hypothesis for the open *"P1 can't see P2 moving"* bug (client drops `0x65` for
  late-join entities): the client may gate movement on an entity until it has received the
  complete mid-zone state handshake it natively expects. Worth testing before another
  `AddMovementUpdate` trace.

### 15.4 Input & movement: buffer locally, stream to server, server REWRITES + echoes to ALL

- *"Client input is buffered and acted upon immediately locally (up to a threshold). That
  recording is streamed to the server which replicates it to other clients / processes it.
  The latency of the synchronized simulation is obscured by this buffering."*
- Threshold: *"you can back into the value by testing the client. If the client is buffering
  too much data, it'll pause input. I want to say ~2s but that's just a guess."*
- ★ Movement: *"The server **replayed the user input and rewrote it if it violated the rules
  of the game. The accepted movement was replicated down to all clients, including the
  source**, and the client handled any discrepancy (jittery movement if done wrong)."* —
  move-echo-to-source is NATIVE (why our move acks are load-bearing — §6-LIVE.13, the
  regime-B ack fix), and movement discrepancy degrades to jitter, unlike HP (fatal, §4).

### 15.5 Tick rate

- *"Not sure of the rate. At least 30hz."* — matches the T0 world clock: one entity message
  per 4 world ticks @ 133 ms ⇒ 33.25 ms/tick ≈ **30 Hz** (§2).

### 15.6 NPCs, mobs, and the Bling Gnome live INSIDE the shared sim

- Spawning: *"The server spawns monsters based on encounter tables… The client will happily
  accept any monsters the server chooses to spawn. **Once they spawn, then the synchronized
  simulation takes over.**"* — spawns are Regime-A server data (§6); mob RUNTIME behavior is
  shared-sim code.
- Pathing: *"Failed path attempts aren't an issue for NPCs since **they are only moved as
  part of the sim**."* Bling Gnome: *"I don't recall making a local fidget system. The logic
  was shared by all sim members."* — confirms wander/fidget = client-brain sim (§14.5).
- ⇒ For the run-through (§14.6/§14.7): natively there was **no L2-style out-of-sim server
  mover** — mob movement decisions ran in the same sim code every peer (including the
  headless server) executed. Retail mobs stood at attack range on this same client engine ⇒
  the chase→attack range gate **exists in the build** and is either **data-parameterized**
  (we may not ship the authored value) or sat in the server's *"extra systems enabled"*
  decision layer (which would make our `DR_MONSTER_AI` server-chase the native shape).
  Both readings keep the §14.6 round-6 investigation (find the client's range decision)
  the right next step. **[T1 from DEV]**

### 15.7 Difficulty budget

- *"The difficulty budget isn't ringing many bells. It sounds like a variable we'd use to
  scale difficulty based on party size."* — our `DR_ENCOUNTER_BUDGET` (2.25 static) is a
  stand-in; native intent = **party-size scaler**. **[DEV]**

---

*Maintainers: keep tiers honest. When a T2 becomes confirmed, cite the address or
capture and move it to T0. When a claim is disproven, move it to §12 with the
evidence. This file is only as useful as its discipline about what is actually proven.*
