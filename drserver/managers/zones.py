"""Zone registry — loads the ``zones`` table from the shipped DB.

Ported from UnityGameServer.Zone / the _zones dictionary. The game server uses
this to resolve a zone name to its numeric id (sent to the client during the
zone-join handshake) and to look up per-zone explored-bit counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from ..core import log
from ..db import game_database

# Town zone id — fallback used by the C# server when a named zone is missing.
TOWN_ZONE_ID = 2781714545


@dataclass(frozen=True)
class Zone:
    id: int
    name: str
    gc_type: str
    spawn_x: float = 0.0
    spawn_y: float = 0.0
    spawn_z: float = 0.0
    spawn_heading: float = 0.0
    respawn_zone: str = ""
    explored_bit_count: int = 0
    # Faithful client ``.zone`` fields (see zones_importer). Optional so the
    # registry still loads against an un-augmented DB.
    label: str = ""
    min_level: Optional[int] = None
    max_level: Optional[int] = None
    is_town: bool = False
    private: bool = False
    respawn_spawn_point: str = ""


class ZoneRegistry:
    def __init__(self) -> None:
        self._by_id: Dict[int, Zone] = {}
        self._by_name: Dict[str, Zone] = {}

    def load(self) -> int:
        """Load all zones from the DB. Returns the number loaded."""
        self._by_id.clear()
        self._by_name.clear()
        try:
            # SELECT * so the faithful .zone columns (label/min_level/…) load when
            # present; the typed accessors default gracefully on an un-augmented DB.
            cur = game_database.execute_reader("SELECT * FROM zones")
        except Exception as ex:  # noqa: BLE001
            log.warn(f"[ZONES] failed to load zones table: {ex}")
            return 0

        for row in cur.fetchall():
            min_lvl = game_database.get_int(row, "min_level", -1)
            max_lvl = game_database.get_int(row, "max_level", -1)
            zone = Zone(
                id=game_database.get_int(row, "id"),
                name=game_database.get_string(row, "name"),
                gc_type=game_database.get_string(row, "gc_type"),
                spawn_x=game_database.get_float(row, "spawn_x"),
                spawn_y=game_database.get_float(row, "spawn_y"),
                spawn_z=game_database.get_float(row, "spawn_z"),
                spawn_heading=game_database.get_float(row, "spawn_heading"),
                respawn_zone=game_database.get_string(row, "respawn_zone"),
                explored_bit_count=game_database.get_int(row, "explored_bit_count"),
                label=game_database.get_string(row, "label"),
                min_level=None if min_lvl < 0 else min_lvl,
                max_level=None if max_lvl < 0 else max_lvl,
                is_town=game_database.get_bool(row, "is_town"),
                private=game_database.get_bool(row, "private"),
                respawn_spawn_point=game_database.get_string(row, "respawn_spawn_point"),
            )
            self._by_id[zone.id] = zone
            self._by_name[zone.name.lower()] = zone

        log.info(f"[ZONES] loaded {len(self._by_id)} zones")
        return len(self._by_id)

    def get_by_id(self, zone_id: int) -> Optional[Zone]:
        return self._by_id.get(zone_id)

    def find_by_name(self, name: str) -> Optional[Zone]:
        if not name:
            return None
        return self._by_name.get(name.lower())


zone_registry = ZoneRegistry()
