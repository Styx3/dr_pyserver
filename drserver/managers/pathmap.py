"""PathMap loader — port of C# ``DungeonRunners.Core.PathMap`` / ``PathMapManager``.

Backs the dungeon maze spawner's walkability and terrain-height queries so mobs
don't land in walls or float above/under the floor. Data comes from the
``pathmap_zones`` (header) and ``pathmap_nodes`` (per-cell) SQLite tables.

Faithful details from the C# reference:
  * ``TILE_SIZE = 10`` world units per pathmap cell.
  * ``WorldToGrid = round((world - worldOffset) / 10)`` (banker's rounding in C#;
    cell spacing is 10 so the rounding mode never changes which cell we hit).
  * A node is walkable when ``SolidFlag < 0xFE`` (the ``s`` column).
  * ``GetHeightAt`` returns the exact node height, else searches outward to
    radius 3 for a walkable node, else a caller-supplied default.

Loaded lazily per zone and cached; ~10–13k nodes per dungoen level.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..core import log
from ..db import game_database as db

TILE_SIZE = 10.0
_SOLID_BLOCKED = 0xFE  # SolidFlag >= 0xFE  => not walkable


def _is_procedural_zone(base_key: str) -> bool:
    """Lazy proxy to ``dungeon_spawner.is_procedural_zone`` (imported here to
    avoid the spawner→pathmap import cycle at module load)."""
    try:
        from . import dungeon_spawner
        return dungeon_spawner.is_procedural_zone(base_key)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class PathNode:
    grid_x: int
    grid_y: int
    world_x: float
    world_y: float
    height: float
    connection_flags: int
    solid_flag: int

    @property
    def is_walkable(self) -> bool:
        return self.solid_flag < _SOLID_BLOCKED


class PathMap:
    def __init__(self, zone_name: str, world_offset_x: float, world_offset_y: float,
                 min_world_x: float, max_world_x: float,
                 min_world_y: float, max_world_y: float) -> None:
        self.zone_name = zone_name
        self.world_offset_x = world_offset_x
        self.world_offset_y = world_offset_y
        self.min_world_x = min_world_x
        self.max_world_x = max_world_x
        self.min_world_y = min_world_y
        self.max_world_y = max_world_y
        self._nodes: Dict[Tuple[int, int], PathNode] = {}

    @classmethod
    def create_empty(cls, zone_name: str, min_world_x: float, max_world_x: float,
                     min_world_y: float, max_world_y: float) -> "PathMap":
        """Empty map for the geometry builder to populate node-by-node.

        Port of C# ``PathMap.CreateEmpty``. The world offset is the lower-left
        bound so ``world_to_grid`` reproduces the builder's ``gx``/``gy`` indices
        (node ``world_x = min_world_x + gx * TILE_SIZE``).
        """
        return cls(zone_name, min_world_x, min_world_y,
                   min_world_x, max_world_x, min_world_y, max_world_y)

    def _add(self, node: PathNode) -> None:
        self._nodes[(node.grid_x, node.grid_y)] = node

    def set_node(self, node: PathNode) -> None:
        """Public node insert used by :mod:`drserver.managers.pathmap_builder`."""
        self._nodes[(node.grid_x, node.grid_y)] = node

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    def world_to_grid(self, world_x: float, world_y: float) -> Tuple[int, int]:
        gx = round((world_x - self.world_offset_x) / TILE_SIZE)
        gy = round((world_y - self.world_offset_y) / TILE_SIZE)
        return int(gx), int(gy)

    def get_node_at(self, gx: int, gy: int) -> Optional[PathNode]:
        return self._nodes.get((gx, gy))

    def is_walkable(self, world_x: float, world_y: float) -> bool:
        gx, gy = self.world_to_grid(world_x, world_y)
        node = self._nodes.get((gx, gy))
        return node is not None and node.is_walkable

    def get_height_at(self, world_x: float, world_y: float,
                      default_height: float = 50.0) -> float:
        if (world_x < self.min_world_x or world_x > self.max_world_x or
                world_y < self.min_world_y or world_y > self.max_world_y):
            return default_height

        gx, gy = self.world_to_grid(world_x, world_y)
        node = self._nodes.get((gx, gy))
        if node is not None and node.is_walkable:
            return node.height

        for radius in range(1, 4):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) != radius and abs(dy) != radius:
                        continue
                    node = self._nodes.get((gx + dx, gy + dy))
                    if node is not None and node.is_walkable:
                        return node.height
        return default_height


class PathMapManager:
    """Lazily loads and caches per-zone PathMaps from SQLite."""

    def __init__(self) -> None:
        self._maps: Dict[str, Optional[PathMap]] = {}
        self._instances: Dict[str, PathMap] = {}

    def register_instance(self, key: str, pathmap: PathMap) -> None:
        """Register a per-instance geometry PathMap (port of
        ``PathMapManager.RegisterInstancePathMap``). Keyed by the full instance
        zone key (e.g. ``dungeon01_level01_inst42``) so :meth:`get` returns it
        instead of refusing the procedural instance."""
        if not key or pathmap is None:
            return
        self._instances[key.lower()] = pathmap
        log.info(f"[PATHMAP] registered instance '{key.lower()}' "
                 f"({pathmap.node_count} nodes)")

    def unregister_instance(self, key: str) -> None:
        """Drop a per-instance PathMap on instance teardown to free memory."""
        if not key:
            return
        if self._instances.pop(key.lower(), None) is not None:
            log.info(f"[PATHMAP] unregistered instance '{key.lower()}'")

    def get(self, zone_name: str) -> Optional[PathMap]:
        if not zone_name:
            return None
        key = zone_name.lower()

        # 1) A registered per-instance geometry map wins outright.
        inst = self._instances.get(key)
        if inst is not None:
            return inst

        # 2) An instance key with no registered map: refuse the static base
        #    PathMap for *procedural* zones — the maze layout differs per
        #    instance, so the base static map would mis-snap mobs (mirror
        #    PathMapManager.GetPathMap lines 96-116).
        inst_idx = key.find("_inst")
        if inst_idx > 0:
            base_key = key[:inst_idx]
            if _is_procedural_zone(base_key):
                return None
            return self._get_static(base_key)

        # 3) Plain zone name → static SQLite map (Town/tutorial/dungeon00).
        return self._get_static(key)

    def _get_static(self, zone_name: str) -> Optional[PathMap]:
        key = zone_name.lower()
        if key in self._maps:
            return self._maps[key]
        pm = self._load(zone_name)
        self._maps[key] = pm
        return pm

    def _load(self, zone_name: str) -> Optional[PathMap]:
        try:
            header = db.execute_reader(
                "SELECT world_offset_x, world_offset_y, world_min_x, world_max_x, "
                "world_min_y, world_max_y FROM pathmap_zones "
                "WHERE zone_name = :zone COLLATE NOCASE", {"zone": zone_name}
            ).fetchone()
            if header is None:
                return None

            pm = PathMap(
                zone_name=zone_name,
                world_offset_x=db.get_float(header, "world_offset_x"),
                world_offset_y=db.get_float(header, "world_offset_y"),
                min_world_x=db.get_float(header, "world_min_x"),
                max_world_x=db.get_float(header, "world_max_x"),
                min_world_y=db.get_float(header, "world_min_y"),
                max_world_y=db.get_float(header, "world_max_y"),
            )

            rows = db.execute_reader(
                "SELECT gx, gy, wx, wy, h, c, s FROM pathmap_nodes "
                "WHERE zone_name = :zone COLLATE NOCASE", {"zone": zone_name}
            ).fetchall()
            for r in rows:
                pm._add(PathNode(
                    grid_x=db.get_int(r, "gx"),
                    grid_y=db.get_int(r, "gy"),
                    world_x=db.get_float(r, "wx"),
                    world_y=db.get_float(r, "wy"),
                    height=db.get_float(r, "h"),
                    connection_flags=db.get_int(r, "c"),
                    solid_flag=db.get_int(r, "s"),
                ))
            log.info(f"[PATHMAP] loaded {len(rows)} nodes for '{zone_name}'")
            return pm
        except Exception as ex:  # noqa: BLE001
            log.warn(f"[PATHMAP] load failed for '{zone_name}': {ex}")
            return None


pathmap_manager = PathMapManager()
