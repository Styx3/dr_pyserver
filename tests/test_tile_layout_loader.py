"""Tests for ``.tile`` encounter-marker extraction.

The dungeon spawner places mobs at each tile's ``base.Encounter<N>`` markers
(verified against client content, 2026-06-09). ``Encounter_Empty`` markers and
non-encounter placements must be excluded. Uses synthetic GC text so the test
doesn't depend on the extracter.
"""
from drserver.managers import tile_layout_loader as tll

_TILE = """
* extends base.world
{
    Map
    {
        * extends base.Encounter200
        {
            Heading = 90;
            Position = 170,170,5;
        }
        * extends base.Encounter
        {
            Position = 40,60,0;
        }
        * extends base.Encounter_Empty
        {
            Position = 50,50,0;
        }
        * extends terrain.cat.floor.cat_floor_1
        {
            Position = 10,10,0;
        }
    }
}
"""


def test_encounter_markers_extracted_with_budget_and_position():
    layout = tll.load_from_text(_TILE, "synthetic.tile")

    markers = layout.encounter_markers

    # base.Encounter200 (budget 200) + base.Encounter (budget 0); Encounter_Empty
    # and the terrain placement are excluded.
    assert [(m.x, m.y, m.z, m.heading, m.budget) for m in markers] == [
        (170.0, 170.0, 5.0, 90.0, 200),
        (40.0, 60.0, 0.0, 0.0, 0),
    ]


def test_encounter_empty_and_terrain_are_not_markers():
    layout = tll.load_from_text(_TILE, "synthetic.tile")

    paths = [m.x for m in layout.encounter_markers]
    assert 50.0 not in paths  # Encounter_Empty
    assert 10.0 not in paths  # terrain placement


# An entrance tile's Entities block: a player SpawnPoint waypoint + the portal
# model. Mirrors elmforest_undergroundentrance_1n's real content (values verified
# against the extracter, 2026-06-09).
_ENTRANCE_TILE = """
* extends base.World
{
    Entities
    {
        Name = GCObject;
        * extends misc.ZonePortal_agg
        {
            Width = 50;
            Position = 236,128,14;
        }
        * extends misc.Waypoint
        {
            Heading = -180;
            Position = 234,73,10;
            Name = SpawnPoint;
        }
    }
}
"""

# A bare portal room (hub/up/down): only a ZonePortal, no SpawnPoint waypoint.
_PORTAL_ROOM_TILE = """
* extends base.World
{
    Entities
    {
        * extends misc.zoneportal_elite
        {
            Heading = 90;
            Position = 310,80,25;
        }
    }
}
"""


def test_player_spawn_anchor_reads_waypoint_named_spawnpoint():
    layout = tll.load_from_text(_ENTRANCE_TILE, "entrance.tile")

    anchor = layout.player_spawn_anchor

    assert anchor == tll.AuthoredAnchor(234.0, 73.0, 10.0, -180.0)


def test_zone_portal_anchor_matches_any_zoneportal_variant():
    entrance = tll.load_from_text(_ENTRANCE_TILE, "entrance.tile")
    portal_room = tll.load_from_text(_PORTAL_ROOM_TILE, "portal.tile")

    # ZonePortal_agg and zoneportal_elite both resolve via the substring match.
    assert entrance.zone_portal_anchor == tll.AuthoredAnchor(236.0, 128.0, 14.0, 0.0)
    assert portal_room.zone_portal_anchor == tll.AuthoredAnchor(310.0, 80.0, 25.0, 90.0)


def test_bare_portal_room_has_no_player_waypoint():
    layout = tll.load_from_text(_PORTAL_ROOM_TILE, "portal.tile")

    # Only entrance tiles carry a SpawnPoint; the spawner derives the player
    # position from the portal anchor here.
    assert layout.player_spawn_anchor is None
    assert layout.zone_portal_anchor is not None
