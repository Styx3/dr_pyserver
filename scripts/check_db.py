import os
import sqlite3

# Local copy of the C# build's content DB, relative to the server directory.
DB = os.path.join(os.path.dirname(__file__), "..", "Database", "dungeon_runners.db")
conn = sqlite3.connect(DB)
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for t in tables:
    print(t)
print(f"\nTotal: {len(tables)} tables")

# Check key tables for Phase 7 data
for check in ("item_resolved_mods", "stat_pools", "creatures", "zones", "items", "monster_behaviors", "zone_behaviors", "zone_world_entities", "loot_generators", "encounter_tables"):
    if check in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{check}]").fetchone()[0]
        print(f"  {check}: {count} rows")
    else:
        print(f"  {check}: NOT FOUND")
conn.close()
