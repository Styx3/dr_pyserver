"""Central DDL for the static **content** tables that the ``.gc`` / ``.world``
importers *populate* but do not themselves *create*.

Background
----------
Content tables historically came pre-created inside the shipped
``dungeon_runners.db`` (originally lifted from the C# ``DR_Server`` build). The
``.gc`` importers under ``drserver/data/`` were written to ``INSERT`` into those
pre-existing tables, so several of them have no ``CREATE TABLE`` of their own:
``merchants`` / ``npcs`` / ``creature_manipulators`` / base ``zones`` and the
"orphan" tables that never had an importer at all (``stat_pools``,
``item_resolved_mods``, ``zone_portals`` …). When building a DB *from zero* those
tables must be declared first — that is this module's job.

Tables that an importer already creates itself are intentionally **absent** here:

* player/runtime tables            → ``drserver/db/game_database.py`` (_SCHEMA_DDL)
* creatures / items / weapons / armor / item_wire_mods / skills / quests
                                    → their ``*_importer`` modules
* dungeon_* / static_world_*       → ``dungeon_world_importer.build_schema``
* npc_teleporters                  → ``world_npc_importer``

All DDL is ``CREATE TABLE IF NOT EXISTS`` (idempotent) and mirrors the live-DB
column shapes exactly, so the importers' positional ``INSERT``s line up.
"""
from __future__ import annotations

import sqlite3

# Ordered loosely by reference (creatures/items exist before things that point at
# them) — FKs are not enforced, but it keeps a from-zero build tidy.
CONTENT_SCHEMA: tuple[str, ...] = (
    # ── creature loadout (armed by creature_manipulators_importer) ──────────
    """CREATE TABLE IF NOT EXISTS creature_manipulators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creature_gc_type TEXT NOT NULL,
        slot TEXT NOT NULL,
        gc_type TEXT DEFAULT '',
        equipable TEXT DEFAULT '',
        slot_type TEXT DEFAULT '',
        weapon_class TEXT DEFAULT '',
        weapon_range TEXT DEFAULT '',
        cooldown TEXT DEFAULT '',
        damage TEXT DEFAULT ''
    )""",
    # ── resolved item modifiers (item_stat_database reads this at runtime) ───
    """CREATE TABLE IF NOT EXISTS item_resolved_mods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_gc_key TEXT NOT NULL,
        mod_slot INTEGER NOT NULL,
        attribute TEXT NOT NULL,
        pool_name TEXT NOT NULL,
        value_mult REAL NOT NULL,
        UNIQUE(full_gc_key, mod_slot, attribute)
    )""",
    # ── item stat pools (base/scale/divisor per named pool) ──────────────────
    """CREATE TABLE IF NOT EXISTS stat_pools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pool_name TEXT NOT NULL UNIQUE,
        base_value REAL NOT NULL,
        scale REAL NOT NULL,
        divisor REAL NOT NULL
    )""",
    # ── merchants (merchants_importer) ──────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS merchants (
        id INTEGER PRIMARY KEY,
        npc_gc_type TEXT,
        merchant_gc_type TEXT,
        name TEXT DEFAULT '',
        sell_value_mod REAL DEFAULT 1.0,
        buy_value_mod REAL DEFAULT 1.0,
        regenerate_items INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS merchant_inventories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant_id INTEGER NOT NULL,
        inv_id INTEGER NOT NULL,
        name TEXT DEFAULT '', gc_type TEXT DEFAULT '', label TEXT DEFAULT '',
        static_contents INTEGER DEFAULT 1, auto_generate INTEGER DEFAULT 0,
        item_generator TEXT DEFAULT '', min_item_level INTEGER DEFAULT 0,
        max_item_level INTEGER DEFAULT 0, regen_seconds INTEGER DEFAULT 0,
        width INTEGER DEFAULT 10, height INTEGER DEFAULT 10
    )""",
    """CREATE TABLE IF NOT EXISTS merchant_inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant_id INTEGER NOT NULL,
        inv_id INTEGER NOT NULL,
        item_gc_type TEXT NOT NULL,
        inventory_x INTEGER DEFAULT 0, inventory_y INTEGER DEFAULT 0,
        item_slot_id INTEGER DEFAULT 255, quantity INTEGER DEFAULT 1
    )""",
    # ── world NPCs (world_npc_importer inserts; teleporters it creates itself)
    """CREATE TABLE IF NOT EXISTS npcs (
        id INTEGER PRIMARY KEY, zone_type TEXT, gc_type TEXT, name TEXT,
        pos_x REAL DEFAULT 0, pos_y REAL DEFAULT 0, pos_z REAL DEFAULT 0,
        heading REAL DEFAULT 0, hit_points INTEGER DEFAULT 0, mana_points INTEGER DEFAULT 0
    )""",
    # ── zones (base rows from .zone; zones_importer augments the columns) ────
    """CREATE TABLE IF NOT EXISTS zones (
        id INTEGER PRIMARY KEY, name TEXT, gc_type TEXT, respawn_zone TEXT DEFAULT '',
        spawn_x REAL DEFAULT 0, spawn_y REAL DEFAULT 0, spawn_z REAL DEFAULT 0,
        spawn_heading REAL DEFAULT 0, explored_bit_count INTEGER DEFAULT 0,
        label TEXT, private INTEGER DEFAULT 0, min_level INTEGER, max_level INTEGER,
        respawn_spawn_point TEXT, is_town INTEGER DEFAULT 0, is_legendary INTEGER DEFAULT 0,
        use_elite_generators INTEGER DEFAULT 0, death_penalty INTEGER DEFAULT 0,
        entry_modifier TEXT, pvp_type INTEGER, pvp_match_type TEXT, max_occupancy INTEGER,
        update_frequency INTEGER, allow_pvp_announcements INTEGER DEFAULT 0,
        send_bank_contents INTEGER DEFAULT 0, allow_duel_request INTEGER DEFAULT 0
    )""",
    # ── zone markers: portals / waypoints / obelisk checkpoints ──────────────
    """CREATE TABLE IF NOT EXISTS zone_portals (
        id INTEGER PRIMARY KEY, zone TEXT, name TEXT, gc_type TEXT,
        pos_x REAL, pos_y REAL, pos_z REAL, heading REAL,
        width INTEGER, height INTEGER, target_zone TEXT, spawn_point TEXT, color INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS zone_waypoints (
        id INTEGER PRIMARY KEY, zone TEXT, name TEXT,
        pos_x REAL, pos_y REAL, pos_z REAL, heading REAL
    )""",
    """CREATE TABLE IF NOT EXISTS zone_checkpoints (
        id INTEGER PRIMARY KEY, zone TEXT, name TEXT, gc_type TEXT,
        pos_x REAL, pos_y REAL, pos_z REAL, heading REAL
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        id INTEGER PRIMARY KEY, name TEXT, description TEXT DEFAULT '', zone TEXT DEFAULT '',
        spawn_point TEXT DEFAULT '', map_id TEXT DEFAULT '',
        pos_x REAL DEFAULT 0, pos_y REAL DEFAULT 0, pos_z REAL DEFAULT 0,
        display_order INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
        level_requirement INTEGER DEFAULT 1, unlock_quest TEXT DEFAULT '', image TEXT DEFAULT ''
    )""",
    # ── summons / henchmen (Bait, Snowman, Bling Gnome, …) ───────────────────
    """CREATE TABLE IF NOT EXISTS summons (
        id INTEGER PRIMARY KEY, gc_type TEXT, name TEXT, behaviour_type TEXT DEFAULT '',
        hit_points INTEGER DEFAULT 0, mana_points INTEGER DEFAULT 0, summon_type TEXT DEFAULT '',
        element TEXT DEFAULT '', description TEXT DEFAULT ''
    )""",
    # ── class definitions + starting skills (avatar/classes/*.gc) ────────────
    """CREATE TABLE IF NOT EXISTS class_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_name TEXT NOT NULL UNIQUE,
        display_name TEXT DEFAULT '', description TEXT DEFAULT '',
        weapon TEXT DEFAULT '', armor TEXT DEFAULT '', helmet TEXT DEFAULT '',
        gloves TEXT DEFAULT '', boots TEXT DEFAULT '', shoulders TEXT DEFAULT '',
        shield TEXT DEFAULT '', ring1 TEXT DEFAULT '', ring2 TEXT DEFAULT '', amulet TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS class_starting_skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_name TEXT NOT NULL, skill_gc_type TEXT NOT NULL
    )""",
    # ── quest sub-tables (quests_importer emits these alongside `quests`) ────
    """CREATE TABLE IF NOT EXISTS quest_objective_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quest_id TEXT NOT NULL, name TEXT DEFAULT '', type TEXT DEFAULT '',
        target TEXT DEFAULT '', required_count INTEGER DEFAULT 1, label TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS quest_kill_drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quest_id TEXT NOT NULL, monster_type TEXT NOT NULL,
        item_gc_type TEXT NOT NULL, chance INTEGER NOT NULL
    )""",
    # ── pathmaps (Phase 4: built from tile/cobj geometry; empty is tolerated) ─
    """CREATE TABLE IF NOT EXISTS pathmap_zones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_name TEXT NOT NULL UNIQUE,
        coord_limit_x INTEGER DEFAULT 0, coord_limit_y INTEGER DEFAULT 0,
        world_offset_x REAL DEFAULT 0, world_offset_y REAL DEFAULT 0,
        chunks_per_row INTEGER DEFAULT 0, chunks_per_col INTEGER DEFAULT 0, total_chunks INTEGER DEFAULT 0,
        grid_min_x INTEGER DEFAULT 0, grid_max_x INTEGER DEFAULT 0,
        grid_min_y INTEGER DEFAULT 0, grid_max_y INTEGER DEFAULT 0,
        world_min_x REAL DEFAULT 0, world_max_x REAL DEFAULT 0,
        world_min_y REAL DEFAULT 0, world_max_y REAL DEFAULT 0,
        height_min REAL DEFAULT 0, height_max REAL DEFAULT 0,
        total_nodes INTEGER DEFAULT 0, walkable_nodes INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS pathmap_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_name TEXT NOT NULL,
        gx INTEGER NOT NULL, gy INTEGER NOT NULL,
        wx REAL NOT NULL, wy REAL NOT NULL,
        h REAL DEFAULT 0, c INTEGER DEFAULT 0, s INTEGER DEFAULT 0
    )""",
)

INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_resolved_mods_key ON item_resolved_mods(full_gc_key)",
    "CREATE INDEX IF NOT EXISTS idx_qkd_monster ON quest_kill_drops(monster_type)",
    "CREATE INDEX IF NOT EXISTS idx_qkd_quest ON quest_kill_drops(quest_id)",
    "CREATE INDEX IF NOT EXISTS idx_pn_zone ON pathmap_nodes(zone_name)",
    "CREATE INDEX IF NOT EXISTS idx_pn_grid ON pathmap_nodes(zone_name, gx, gy)",
)

# Tables this module owns — used by build_database.py for its post-build report.
CONTENT_TABLES: tuple[str, ...] = (
    "creature_manipulators", "item_resolved_mods", "stat_pools",
    "merchants", "merchant_inventories", "merchant_inventory_items", "npcs",
    "zones", "zone_portals", "zone_waypoints", "zone_checkpoints", "checkpoints",
    "summons", "class_definitions", "class_starting_skills",
    "quest_objective_templates", "quest_kill_drops", "pathmap_zones", "pathmap_nodes",
)


def create_content_schema(conn: sqlite3.Connection) -> None:
    """Create every content table this module owns (idempotent)."""
    for ddl in CONTENT_SCHEMA:
        conn.execute(ddl)
    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()
