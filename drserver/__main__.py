"""DR Python server entry point.

Loads config, opens the SQLite database, applies dynamic settings, then starts
the auth + queue servers and the game server concurrently.

Run:  python -m drserver  (from the project root, with config.yaml present)
"""
from __future__ import annotations

import asyncio
import sys

from .core import dispatcher, log, settings
from .core.config import ServerConfig
from .core.sessions import queue_bridge
from .db import game_database
from .net.auth_server import AuthServer
from .net.game_server import GameServer
from .admin.admin_server import start_admin_server


async def amain() -> None:
    config = ServerConfig.load("config.yaml")
    game_database.initialize(config.database_path)  # binds settings DB overlay
    settings.load()
    queue_bridge.max_players = settings.get_int("maxPlayers", config.max_players)
    dispatcher.bind(asyncio.get_running_loop())

    log.info("=== DR Python server starting ===")
    log.info(f"version {config.server_version}  max_players {queue_bridge.max_players}")

    auth = AuthServer(config)
    game = GameServer(config)
    await game.start()

    if config.admin_panel_enabled:
        start_admin_server(config, game)

    tasks = [auth.start(), game.serve_forever()]
    if config.telemetry_enabled:
        from .net.telemetry import TelemetryServer
        telemetry = TelemetryServer(game, config.telemetry_ip, config.telemetry_port)
        game.telemetry = telemetry          # combat checks has_active_hook() for kill authority
        tasks.append(telemetry.start())

    await asyncio.gather(*tasks)


def _raise_windows_timer_resolution() -> None:
    """Raise the Windows system timer resolution to 1 ms for the server process.

    Windows' default timer granularity is ~15.6 ms, so asyncio's event-loop timer
    quantizes ``asyncio.sleep(0.033)`` (the movement tick) to ~15.6 ms boundaries —
    the 33 ms tick actually fires at an irregular ~31/47 ms, which jitters the
    ``0x0D`` WorldInterval cadence the client paces its world clock off and renders
    as choppy movement / "fast mobs" even when the wire bytes match the reference.
    Games — including the Unity C# server we mirror — call ``timeBeginPeriod(1)``
    for a steady 1 ms timer. No-op off Windows. Verify the effect with
    ``DR_TICK_HEALTH=1`` (movement._tick_loop): max_gap should drop to ~33 ms.
    """
    if sys.platform != "win32":
        return
    try:
        import atexit
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        if winmm.timeBeginPeriod(1) == 0:        # TIMERR_NOERROR
            atexit.register(winmm.timeEndPeriod, 1)
            log.info("[CLOCK] Windows timer resolution raised to 1 ms (timeBeginPeriod)")
        else:
            log.warn("[CLOCK] timeBeginPeriod(1) rejected — movement tick may jitter")
    except Exception as ex:  # noqa: BLE001
        log.warn(f"[CLOCK] could not raise Windows timer resolution: {ex}")


def main() -> None:
    _raise_windows_timer_resolution()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
