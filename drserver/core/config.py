"""Static server configuration.

Ported from C# ServerConfig (a Unity ScriptableObject). These are the fixed
startup values (ports, encryption keys, version). Dynamic gameplay tunables live
in ServerSettings (core/settings.py). Loaded from a YAML file; every field falls
back to the C# default if absent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

import yaml


@dataclass
class ServerConfig:
    # Auth server
    auth_server_ip: str = "0.0.0.0"
    auth_server_port: int = 2110
    # Game server
    game_server_ip: str = "0.0.0.0"
    game_server_port: int = 2603
    game_server_name: str = "Dungeon Runners Server"
    # Encryption keys (extracted from the client binary)
    blowfish_key: str = "[;'.]94-31==-%&@!^+]"
    des_key: str = "TEST"
    # Server info
    max_players: int = 100
    server_version: str = "1.0.0"
    enable_debug_logging: bool = True
    # World
    default_world_id: int = 1
    default_spawn_position: tuple[float, float, float] = (100.0, 0.0, 100.0)
    default_zone_id: int = 1
    # Admin panel
    admin_panel_enabled: bool = True
    admin_panel_port: int = 8080
    # Combat telemetry channel (client_hook/ reports kills here; see net/telemetry.py)
    telemetry_enabled: bool = True
    telemetry_ip: str = "0.0.0.0"
    telemetry_port: int = 2700
    # When True, the client telemetry channel is the SOLE kill authority: the
    # server's swing-replay still tracks HP for diagnostics but no longer
    # originates kills (it mis-times them — §6-LIVE.6 — so loot dropped early /
    # the mob despawned before the player killed it). Telemetry reports the real
    # kill. Set False to fall back to replay-driven kills.
    telemetry_authoritative_kills: bool = True
    # Paths
    database_path: str = "Database/dungeon_runners.db"
    gc_data_dir: str = "Database/gc"
    pathmap_dir: str = "Database/PathMaps"

    @classmethod
    def load(cls, path: str | None = None) -> "ServerConfig":
        cfg = cls()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            known = {f.name for f in fields(cls)}
            for key, value in raw.items():
                if key in known:
                    if key == "default_spawn_position" and isinstance(value, (list, tuple)):
                        value = tuple(float(v) for v in value)
                    setattr(cfg, key, value)
        return cfg
