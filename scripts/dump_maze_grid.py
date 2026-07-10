"""Dump a procedural level's server-side maze for client-parity diffing.

Produces the "ours" side of the maze room-node PLACEMENT parity check: the maze
params, the cell each forced room node (mainentrance/exit/…) landed in, and the
full grid via ``MazeGenerator.dump_grid`` (byte-faithful to C# ``DumpGrid``).

Diff this against an x64dbg capture of the live client's placed entrance/exit
cells for the same level + seed to find where ``_place_room_nodes`` /
``_get_room_candidates`` RNG draws or candidate ordering diverge (see
[[project_dungeon_map_alignment]] — the residual dungeon01–06 misplacement is
free-placement entrance/exit nodes landing in a different cell than the client).

Usage::

    python scripts/dump_maze_grid.py dungeon01_level01
    python scripts/dump_maze_grid.py dungeon01_level01 --grid   # +full grid dump
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.db import game_database  # noqa: E402
from drserver.managers import dungeon_spawner as ds  # noqa: E402
from drserver.managers import maze as mz  # noqa: E402


def _open_db() -> None:
    # The live DB on /mnt/c is WAL-locked by the running server; copy to a local
    # temp file first (WSL /mnt/c can't take the WAL pragma) and open the copy.
    import shutil
    import tempfile

    src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "Database", "dungeon_runners.db",
    )
    dest = os.path.join(tempfile.mkdtemp(prefix="drmaze_"), "dr.db")
    shutil.copy(src, dest)
    game_database.initialize(dest)


def dump(zone: str, with_grid: bool) -> str:
    seed = ds.layout_seed(zone)
    level = ds._load_level(zone)
    if level is None:
        return f"{zone}: not a procedural level (no dungeon_levels row)\n"

    lines = [
        f"# maze dump: {zone}",
        f"seed=0x{seed:08X}",
        f"params={level.maze_width}x{level.maze_height} "
        f"rand={level.maze_randomness} sparse={level.maze_sparseness} "
        f"deadend={level.maze_dead_end_removal_chance} prefix={level.tile_prefix}",
    ]

    layout = ds._build_layout(zone, seed)
    rn = {r.source_index: r for r in layout.room_nodes}
    lines.append(
        f"placed={len(layout.placed_room_nodes)}/{len(layout.room_nodes)} room nodes"
    )
    lines.append("# src kind tile grid origin link")
    for p in sorted(layout.placed_room_nodes, key=lambda n: n.source_index):
        d = rn.get(p.source_index)
        cell = layout.cell_by_grid.get((p.grid_x, p.grid_y))
        ox = cell.world_origin_x if cell else float("nan")
        oy = cell.world_origin_y if cell else float("nan")
        lines.append(
            f"  src={p.source_index:>2} "
            f"kind={(d.node_kind if d else '?'):<13} "
            f"tile={p.tile_type:<30} "
            f"grid=({p.grid_x},{p.grid_y}) origin=({ox:.0f},{oy:.0f}) "
            f"link={(d.link_to_zone if d else '')}"
        )

    # Unplaced specs (rolled out by chance, or no candidate cell fit).
    placed_src = {p.source_index for p in layout.placed_room_nodes}
    for r in layout.room_nodes:
        if r.source_index not in placed_src:
            lines.append(
                f"  src={r.source_index:>2} kind={r.node_kind:<13} "
                f"tile_set={r.tile_set:<30} NOT PLACED "
                f"(grid={r.grid_x},{r.grid_y} chance={r.chance})"
            )

    if with_grid:
        gen = mz.MazeGenerator(
            level.maze_width, level.maze_height, seed,
            randomness=level.maze_randomness, sparseness=level.maze_sparseness,
            dead_end_removal_chance=level.maze_dead_end_removal_chance,
        )
        # Re-queue the same room-node specs the spawner used so the grid matches.
        for r in layout.room_nodes:
            gen.add_room_node(r.tile_set, r.grid_x, r.grid_y, r.chance, r.source_index)
        gen.generate(level.tile_prefix)
        lines.append("")
        lines.append(gen.dump_grid())

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Dump a level's server-side maze.")
    ap.add_argument("zone", help="zone name, e.g. dungeon01_level01")
    ap.add_argument("--grid", action="store_true", help="include the full grid dump")
    args = ap.parse_args()

    _open_db()
    sys.stdout.write(dump(args.zone, args.grid))


if __name__ == "__main__":
    main()
