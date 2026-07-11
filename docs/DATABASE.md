# Building the content database

The server's SQLite DB has two layers:

- **Player/account tables** (`accounts`, `characters`, `character_*`, …) — created
  automatically on first boot by `drserver/db/game_database.py`. Nothing to do.
- **Static content tables** (creatures, skills, quests, items, zones, merchants,
  dungeons, …) — built from the **extracted client content** by
  `scripts/build_database.py`. This guide covers that.

The old `scripts/restore_database.py` is **dead** — it needed `DR_Server.zip`, which
no longer exists. Use `build_database.py` instead.

---

## Prerequisites

1. **Python 3.12** with deps installed (`pip install -r requirements.txt`).
2. **The extracted client content** (the `extracter/` folder — 585 `.world`, 575
   `.zone`, and the `skills/ quests/ npc/ items/ creatures/ avatar/` `.gc` trees).
   By default it is auto-resolved as a sibling of the repo
   (`…/Desktop/{dr_pyserver, extracter}`). If it lives elsewhere, set:
   ```bash
   export DR_EXTRACTER_DIR=/path/to/extracter
   ```
3. **The item seed** — `scripts/seed/items_seed.sql.gz` (see
   [Why items are seeded](#why-items-are-seeded)). Ship it as a release asset, or
   regenerate it from any known-good DB (see [below](#regenerating-the-item-seed)).
   It is git-ignored (game data), not committed.

---

## Build it

```bash
# from the repo root
python scripts/build_database.py
```

That creates `Database/dungeon_runners.db` (override with `--out`). Useful flags:

```bash
python scripts/build_database.py --list                 # list the build steps
python scripts/build_database.py --only creatures,zones  # build a subset
python scripts/build_database.py --extracter /path/to/extracter --out /tmp/dr.db
python scripts/build_database.py --item-seed /path/to/items_seed.sql.gz
```

The builder makes an empty DB, applies the player schema
(`game_database`) and content schema (`drserver/data/content_schema.py`), runs each
`.gc`/`.world` importer in dependency order, loads the item seed, verifies
`PRAGMA integrity_check`, and atomically moves the result into place. (It builds on
local tmpfs first to avoid the WSL 9P sqlite lock hang, then swaps.)

## Run against it

`config.yaml`'s `database_path` already points at `Database/dungeon_runners.db`, so:

```bash
python -m drserver
```

---

## What comes from where

| Source | Tables |
|---|---|
| **Extracter** (`.gc`/`.world`/`.zone`) | creatures, creature_manipulators, skills, quests, merchants (+inventories), item_wire_mods, zones, class_definitions (+starting skills), dungeon levels/encounters/rooms, static worlds, npcs, teleporters |
| **Item seed** (`items_seed.sql.gz`) | items, weapons, armor, item_resolved_mods, stat_pools |
| **Auto-created on server boot** | accounts, characters, character_*, and other runtime tables |

### Why items are seeded

`items`/`weapons`/`armor` use the client-validated **numbered-PAL** generation
(`1HAxe1PAL.1HAxe1-1`, …), whose definitions lived only in the C# `DR_Server`
build — now gone. The extracter ships a *different, newer* generation
(`1HAxePAL.Normal001`) that Client666 does **not** accept, and the armor base
classes (`BaseArmorClasses.*`) exist there only as names in `GCDictionary.dict`,
defined in no `.gc` file. So the numbered items cannot be rebuilt from the
extracter without fabricating data (which this project forbids). They are therefore
exported once from a known-good DB and loaded from the seed.

### Regenerating the item seed

```bash
python scripts/export_item_seed.py --db Database/dungeon_runners.db
# writes scripts/seed/items_seed.sql.gz  (~635 KB)
```

---

## Verification

```bash
pytest tests/test_build_database.py          # schema unit test + parity build
```

The parity test builds a subset from the extracter and asserts per-table counts
(creatures 1400, skills 237, quests 1289, item_wire_mods 1148, zones 575, classes
3 + 12 skills, merchants ~150) and, when the seed is present, items 11761 / weapons
1630 / armor 1746. It skips automatically when the extracter is absent.

## Known gaps

The core build (login, character creation, zones, combat, merchants, items, NPCs,
portals/waypoints) is complete. Deliberately empty:

- **`checkpoints`** (the obelisk *recall-menu* list, ~15 rows) — a hand-curated set
  not expressed in `.world`, so it is not generated. (`zone_checkpoints`, the
  per-zone obelisk entities, *are* built.)
- **`quest_objective_templates` / `quest_kill_drops` / `summons`** — not read by the
  runtime (the objective/drop data lives in `quests.raw_json`; summons are defined in
  code), so these are intentionally left empty.
- **pathmaps** — `pathmap_nodes`/`pathmap_zones` are built at runtime from tile/cobj
  geometry, so the offline build leaves them empty.
