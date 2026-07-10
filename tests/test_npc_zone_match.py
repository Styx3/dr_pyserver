"""``NPCManager.get_for_zone`` must match the zone name EXACTLY.

Regression for the hub→portal leak: with a ``thehub`` NPC set loaded, the old
substring fallback (``"thehub" in "thehubportals_dungeon01"``) spilled the hub's
NPCs into all 13 dungeon-portal sub-hubs, which the client authors empty. Exact
match keeps the portal sub-hubs NPC-free.
"""
import _paths  # noqa: F401  (sets up sys.path)
from drserver.managers.npcs import NPCData, NPCManager


def _npc(gc_type: str, zone: str) -> NPCData:
    return NPCData(
        id=1, gc_type=gc_type, name=gc_type, zone_type=zone,
        pos_x=0, pos_y=0, pos_z=0, heading=0,
        hit_points=100, mana_points=0, hp_wire=25600, mp_wire=0,
    )


def _manager_with(*zones: str) -> NPCManager:
    mgr = NPCManager()
    for z in zones:
        mgr._npcs_by_zone.setdefault(z, []).append(_npc(f"world.{z}.npc.A", z))
    mgr._loaded = True
    return mgr


def test_exact_zone_match_returns_its_npcs():
    mgr = _manager_with("thehub")
    assert [n.zone_type for n in mgr.get_for_zone("thehub")] == ["thehub"]
    # case-insensitive on the zone name
    assert len(mgr.get_for_zone("TheHub")) == 1


def test_thehub_does_not_leak_into_portal_sub_hubs():
    mgr = _manager_with("thehub", "town")
    assert mgr.get_for_zone("thehubportals_dungeon01") == []
    assert mgr.get_for_zone("thehub_oldlinks") == []


def test_unknown_zone_is_empty():
    mgr = _manager_with("town", "thehub", "pvp_hub")
    assert mgr.get_for_zone("bughub") == []
    assert mgr.get_for_zone("dungeon01_level01") == []


if __name__ == "__main__":
    import sys
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
