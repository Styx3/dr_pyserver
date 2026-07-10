import os
import sqlite3

# Local copy of the C# build's content DB, relative to the server directory.
DB = os.path.join(os.path.dirname(__file__), "..", "Database", "dungeon_runners.db")
conn = sqlite3.connect(DB)

# Check creatures table schema and sample data
print("=== creatures (sample 5 rows) ===")
schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='creatures'").fetchone()[0]
print(f"Schema: {schema[:200]}...")
rows = conn.execute("SELECT * FROM creatures LIMIT 5").fetchall()
cols = [d[0] for d in conn.execute("PRAGMA table_info(creatures)").fetchall()]
print(f"Columns: {cols}")
for r in rows:
    print(dict(zip(cols, r)))

print("\n=== zones (sample 3 rows) ===")
rows = conn.execute("SELECT * FROM zones LIMIT 3").fetchall()
cols = [d[0] for d in conn.execute("PRAGMA table_info(zones)").fetchall()]
print(f"Columns: {cols}")
for r in rows:
    print(dict(zip(cols, r)))

print("\n=== zone_world_entities (all rows) ===")
rows = conn.execute("SELECT * FROM zone_world_entities").fetchall()
cols = [d[0] for d in conn.execute("PRAGMA table_info(zone_world_entities)").fetchall()]
print(f"Columns: {cols}")
for r in rows:
    print(dict(zip(cols, r)))

print("\n=== stat_pools (all rows) ===")
for r in conn.execute("SELECT * FROM stat_pools").fetchall():
    print(dict(r))

print("\n=== item_resolved_mods (sample 5) ===")
rows = conn.execute("SELECT * FROM item_resolved_mods LIMIT 5").fetchall()
cols = [d[0] for d in conn.execute("PRAGMA table_info(item_resolved_mods)").fetchall()]
print(f"Columns: {cols}")
for r in rows:
    print(dict(zip(cols, r)))

print("\n=== items (sample 3) ===")
rows = conn.execute("SELECT * FROM items LIMIT 3").fetchall()
cols = [d[0] for d in conn.execute("PRAGMA table_info(items)").fetchall()]
print(f"Columns: {cols}")
for r in rows:
    print(dict(zip(cols, r)))

conn.close()
