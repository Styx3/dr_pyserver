/*
 * telemetry_client.c — client→server combat telemetry sender.
 *
 * Combat is client-authoritative and packet-blind (bible §6-LIVE.4): the client
 * kills mobs locally but the native protocol never tells the server, so loot/XP
 * never fire and the server's replay-based kill mis-times the despawn (§6-LIVE.6).
 * This sends the client's ground-truth combat events to our server over a small
 * dedicated TCP socket (the "telemetry channel"); the server runs its existing
 * authoritative reward path on them.
 *
 * Design: the game-thread detour (combat_hook.c) only ENQUEUES tiny records; a
 * background thread owns the socket and drains the queue, so the game frame never
 * blocks on connect/send. The server HOST is read from the client's OWN config
 * (config\DungeonRunners.cfg, the [AuthServer] Address) so telemetry always
 * follows the game server; the PORT is hardcoded (not configurable by design).
 *
 * Wire records (little-endian), matching drserver/net/telemetry.py:
 *   C→S  KILL  0x02 [victim_eid u32][killer_eid u32]   (9 bytes)
 *   S→C  MOB_ATTACK 0x80 [mob u32][avatar u32][dmg_wire u32][element u8] (14 bytes)
 *
 * The same background thread owns both directions on the one socket: it drains
 * the outbound queue (blocking sends) and polls the socket for inbound
 * server-driven mob attacks (select, non-blocking), handing each to the
 * injection layer (inject_hook.c) which applies it on the game thread.
 */
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include "hook.h"

#define OP_MOB_ATTACK           0x80          /* S→C: server-driven mob hit (14 bytes) */
#define MOB_ATTACK_LEN          14
#define OP_ZONE_RESET           0x81          /* S→C: clear cache on zone change (1 byte) */
#define OP_MOB_CLAMP            0x82          /* S→C: pin aggroed mob to stop-ring (13 bytes) */
#define MOB_CLAMP_LEN           13

#define TELEMETRY_PORT          2700          /* HARDCODED — must match the server */
#define TELEMETRY_PORT_STR      "2700"        /* same value, for getaddrinfo() */
#define TELEMETRY_DEFAULT_HOST  "127.0.0.1"   /* fallback if the cfg can't be read */
#define QUEUE_CAP  256
#define REC_MAX    32          /* OP_KILL_AT_XP is 27 bytes (KILL_AT 23, KILL 9) */

typedef struct { unsigned char bytes[REC_MAX]; int len; } rec_t;

static rec_t            g_queue[QUEUE_CAP];
static volatile LONG    g_head = 0;          /* consumer index (monotonic) */
static volatile LONG    g_tail = 0;          /* producer index (monotonic) */
static CRITICAL_SECTION g_lock;
static HANDLE           g_wake = NULL;
static volatile LONG    g_started = 0;
static char             g_host[64] = TELEMETRY_DEFAULT_HOST;

static void read_config(void)
{
    /* Build the absolute path to <game>\config\DungeonRunners.cfg (next to the
     * EXE), so it resolves regardless of the working directory. */
    char cfg[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, cfg, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return;
    char *slash = strrchr(cfg, '\\');
    if (!slash) return;
    if ((size_t)(slash + 1 - cfg) + sizeof("config\\DungeonRunners.cfg") >= MAX_PATH) return;
    strcpy(slash + 1, "config\\DungeonRunners.cfg");

    char addr[64];
    /* [AuthServer] Address = <ip> — same server the client auths against. */
    DWORD len = GetPrivateProfileStringA("AuthServer", "Address", TELEMETRY_DEFAULT_HOST,
                                         addr, sizeof(addr), cfg);
    if (len == 0) return;
    /* Trim surrounding whitespace ("Address = 1.2.3.4"). */
    char *s = addr;
    while (*s == ' ' || *s == '\t') s++;
    char *e = s + strlen(s);
    while (e > s && (e[-1] == ' ' || e[-1] == '\t' || e[-1] == '\r' || e[-1] == '\n')) *--e = '\0';
    if (*s) { strncpy(g_host, s, sizeof(g_host) - 1); g_host[sizeof(g_host) - 1] = '\0'; }
    OutputDebugStringA("[drhook/telemetry] host read from config\\DungeonRunners.cfg [AuthServer]");
}

static SOCKET connect_server(void)
{
    /* getaddrinfo() resolves BOTH a dotted-quad IP and a DNS hostname. The old
     * inet_addr(g_host) only parsed dotted-quads: once the client's [AuthServer]
     * Address became a hostname (auth.styx3.com) it returned INADDR_NONE, so the
     * connect always failed, the hook never connected, and the server silently
     * fell back to its deprecated swing-replay kills. */
    struct addrinfo hints, *res = NULL, *ai;
    SOCKET s = INVALID_SOCKET;

    memset(&hints, 0, sizeof(hints));
    hints.ai_family   = AF_INET;          /* server telemetry listener is IPv4 */
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;

    if (getaddrinfo(g_host, TELEMETRY_PORT_STR, &hints, &res) != 0 || res == NULL)
        return INVALID_SOCKET;

    for (ai = res; ai != NULL; ai = ai->ai_next) {
        s = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (s == INVALID_SOCKET) continue;
        if (connect(s, ai->ai_addr, (int)ai->ai_addrlen) == 0) break;
        closesocket(s);
        s = INVALID_SOCKET;
    }
    freeaddrinfo(res);
    return s;
}

/* Inbound accumulator (background thread only). Parses complete server→client
 * records out of a byte stream that may split/coalesce across recv() calls. */
static unsigned char g_rx[512];
static int           g_rxlen = 0;

static void rx_feed(const unsigned char *data, int n)
{
    for (int i = 0; i < n && g_rxlen < (int)sizeof(g_rx); ++i)
        g_rx[g_rxlen++] = data[i];

    int off = 0;
    while (off < g_rxlen) {
        unsigned char op = g_rx[off];
        if (op == OP_MOB_ATTACK) {
            if (g_rxlen - off < MOB_ATTACK_LEN) break;       /* wait for the rest */
            unsigned mob, avatar, dmg;
            memcpy(&mob,    g_rx + off + 1, 4);
            memcpy(&avatar, g_rx + off + 5, 4);
            memcpy(&dmg,    g_rx + off + 9, 4);
            unsigned char elem = g_rx[off + 13];
            dr_inject_enqueue(mob, avatar, dmg, elem);
            off += MOB_ATTACK_LEN;
        } else if (op == OP_MOB_CLAMP) {
            if (g_rxlen - off < MOB_CLAMP_LEN) break;        /* wait for the rest */
            unsigned mob, avatar, ring;
            memcpy(&mob,    g_rx + off + 1, 4);
            memcpy(&avatar, g_rx + off + 5, 4);
            memcpy(&ring,   g_rx + off + 9, 4);
            dr_inject_set_clamp(mob, avatar, ring);
            off += MOB_CLAMP_LEN;
        } else if (op == OP_ZONE_RESET) {
            dr_inject_reset();                               /* drop stale-zone units */
            off += 1;
        } else {
            off += 1;                                        /* unknown — resync */
        }
    }
    if (off > 0) {                                           /* shift remainder down */
        memmove(g_rx, g_rx + off, g_rxlen - off);
        g_rxlen -= off;
    }
}

/* Drain any inbound server records without blocking the send cadence. Returns
 * the (possibly closed) socket: INVALID_SOCKET on a dead peer. */
static SOCKET poll_inbound(SOCKET s)
{
    for (;;) {
        fd_set rfds;
        struct timeval tv = { 0, 0 };
        FD_ZERO(&rfds);
        FD_SET(s, &rfds);
        int sel = select(0, &rfds, NULL, NULL, &tv);
        if (sel <= 0 || !FD_ISSET(s, &rfds)) break;          /* nothing ready */
        unsigned char buf[256];
        int r = recv(s, (char *)buf, sizeof(buf), 0);
        if (r <= 0) { closesocket(s); return INVALID_SOCKET; }
        rx_feed(buf, r);
    }
    return s;
}

static DWORD WINAPI sender_thread(LPVOID arg)
{
    WSADATA wsa;
    SOCKET  s = INVALID_SOCKET;
    (void)arg;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    read_config();
    for (;;) {
        /* Short poll so server-driven mob attacks are read promptly (~50 ms);
         * SetEvent(g_wake) still wakes us immediately for outbound records. */
        WaitForSingleObject(g_wake, 50);
        if (s == INVALID_SOCKET) {
            s = connect_server();
            if (s == INVALID_SOCKET) { Sleep(2000); continue; }
            g_rxlen = 0;                       /* fresh inbound stream */
            OutputDebugStringA("[drhook/telemetry] connected to server");
        }
        for (;;) {
            rec_t rec;
            int have = 0;
            EnterCriticalSection(&g_lock);
            if (g_head != g_tail) { rec = g_queue[g_head % QUEUE_CAP]; g_head++; have = 1; }
            LeaveCriticalSection(&g_lock);
            if (!have) break;
            int sent = 0;
            while (sent < rec.len) {
                int r = send(s, (const char *)rec.bytes + sent, rec.len - sent, 0);
                if (r <= 0) { closesocket(s); s = INVALID_SOCKET; break; }
                sent += r;
            }
            if (s == INVALID_SOCKET) break;   /* reconnect next loop; record is lost (rare) */
        }
        if (s != INVALID_SOCKET)
            s = poll_inbound(s);              /* apply server-driven mob attacks */
    }
    return 0;
}

static void ensure_started(void)
{
    /* One-time start of the socket thread. Idempotent (InterlockedCompareExchange
     * gate), so it is safe to call from BOTH the DllMain eager-start and the
     * first outbound enqueue. */
    if (InterlockedCompareExchange(&g_started, 1, 0) != 0) return;
    InitializeCriticalSection(&g_lock);
    g_wake = CreateEventA(NULL, FALSE, FALSE, NULL);
    CreateThread(NULL, 0, sender_thread, NULL, 0, NULL);
}

/* Start the telemetry socket thread NOW — called from DllMain (dr_install_all)
 * so the channel connects at client launch, long before the first fight.
 *
 * ★ 2026-07-04: the thread used to start lazily on the first KILL enqueue, so in
 * the enroll+clamp model the server's OP_MOB_CLAMP sends (fired the instant the
 * player attacks and the mobs enroll) hit a client with NO telemetry socket yet
 * — "no hook connected", every clamp dropped, mobs ran through on approach. The
 * socket must be up BEFORE combat, not after the first kill. The thread self-
 * heals if the server isn't listening yet (retries every 2 s). */
void dr_telemetry_start(void)
{
    ensure_started();
}

static void enqueue(const unsigned char *bytes, int len)
{
    if (len > REC_MAX) return;
    ensure_started();
    EnterCriticalSection(&g_lock);
    if ((g_tail - g_head) < QUEUE_CAP) {        /* room? else drop (rare) */
        memcpy(g_queue[g_tail % QUEUE_CAP].bytes, bytes, len);
        g_queue[g_tail % QUEUE_CAP].len = len;
        g_tail++;
    }
    LeaveCriticalSection(&g_lock);
    if (g_wake) SetEvent(g_wake);
}

void dr_telemetry_report_kill(unsigned int victim_eid, unsigned int killer_eid)
{
    unsigned char rec[9];
    rec[0] = 0x02;                              /* OP_KILL (legacy, no pos/level) */
    memcpy(rec + 1, &victim_eid, 4);
    memcpy(rec + 5, &killer_eid, 4);
    enqueue(rec, 9);
}

/* KILL with the victim's death position (Fixed32 ×256) and the killer's current
 * level — the server drops loot at the real death spot and snaps the character's
 * level to the client's. Wire: [op 0x04][victim u32][killer u32][px i32][py i32]
 * [pz i32][level u16] = 23 bytes. See combat_hook.c / net/telemetry.py. */
void dr_telemetry_report_kill_at(unsigned int victim_eid, unsigned int killer_eid,
                                 int px, int py, int pz, unsigned short level)
{
    unsigned char rec[23];
    rec[0] = 0x04;                              /* OP_KILL_AT */
    memcpy(rec + 1, &victim_eid, 4);
    memcpy(rec + 5, &killer_eid, 4);
    memcpy(rec + 9, &px, 4);
    memcpy(rec + 13, &py, 4);
    memcpy(rec + 17, &pz, 4);
    memcpy(rec + 21, &level, 2);
    enqueue(rec, 23);
}

/* KILL_AT plus the killer's exact Experience (progress-into-level, entity +0x320).
 * Wire: [op 0x05][victim u32][killer u32][px i32][py i32][pz i32][level u16]
 * [experience u32] = 27 bytes. The server adopts the experience as the sole XP
 * authority so the zone-transfer re-send matches the client. See net/telemetry.py
 * (OP_KILL_AT_XP). */
void dr_telemetry_report_kill_at_xp(unsigned int victim_eid, unsigned int killer_eid,
                                    int px, int py, int pz, unsigned short level,
                                    unsigned int experience)
{
    unsigned char rec[27];
    rec[0] = 0x05;                              /* OP_KILL_AT_XP */
    memcpy(rec + 1, &victim_eid, 4);
    memcpy(rec + 5, &killer_eid, 4);
    memcpy(rec + 9, &px, 4);
    memcpy(rec + 13, &py, 4);
    memcpy(rec + 17, &pz, 4);
    memcpy(rec + 21, &level, 2);
    memcpy(rec + 23, &experience, 4);
    enqueue(rec, 27);
}
