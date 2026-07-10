"""Instance-id assignment policy (bible §7 / §9 item 6).

Before this, ``conn.instance_id`` was declared ``= 0`` and NEVER reassigned, so
every player in a given zone keyed to ``(zone_id, 0)`` — one shared instance.
Two players soloing the same dungeon saw each other / duplicated the world. The
per-instance machinery (``world_instance``) was already correct; only the key was
missing. These tests pin the policy that now sets the key:

  * peaceful/shared zones (town, tutorial, the connecting hubs)  -> instance 0
  * dungeon content (incl. is_town=1 entrances + quest rooms)    -> private token
  * two solo players in the same dungeon                         -> distinct tokens
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.net.game_server import GameServer, PUBLIC_INSTANCE_ID


# ── Test doubles ─────────────────────────────────────────────────────────────
class FakeZone:
    def __init__(self, name, is_town=False):
        self.name = name
        self.is_town = is_town


class FakeConn:
    def __init__(self, login, zone_id, zone_name):
        self.login_name = login
        self.current_zone_id = zone_id
        self.current_zone_name = zone_name
        self.instance_id = 0  # the old always-0 default


def _server():
    """A GameServer with just the instance allocator primed (no heavy init)."""
    srv = GameServer.__new__(GameServer)
    srv.next_instance_id = 1
    return srv


# ── _is_public_zone (the data-driven peaceful/shared predicate) ───────────────
def test_town_and_tutorial_are_public():
    assert GameServer._is_public_zone("town", is_town=True)
    assert GameServer._is_public_zone("tutorial", is_town=True)


def test_connecting_hubs_are_public():
    # thehub + the per-dungeon portal rooms are is_town=1 in the content.
    for name in ("thehub", "thehub_oldlinks", "thehubportals_dungeon01",
                 "thehubportals_dungeon16"):
        assert GameServer._is_public_zone(name, is_town=True), name


def test_hub_fallback_token_covers_is_town_zero_hubs():
    # pvp_hub / bughub are social but flagged is_town=0 — the name token saves them.
    assert GameServer._is_public_zone("pvp_hub", is_town=False)
    assert GameServer._is_public_zone("bughub", is_town=False)


def test_dungeon_content_is_private_even_when_is_town():
    # dungeonNN_level00 entrances + dNN_lMM_q## quest rooms are is_town=1, but a
    # dungeon must never be shared (bible requirement + Regime-B mob safety).
    assert not GameServer._is_public_zone("dungeon06_level00", is_town=True)
    assert not GameServer._is_public_zone("d06_l01_q05", is_town=True)
    # ordinary combat floors / elite / amazon / epic / squeakeasy
    for name in ("dungeon01_level01", "elite01_intro", "amazon_level02",
                 "epic01_level01", "squeakeasy_level03"):
        assert not GameServer._is_public_zone(name, is_town=False), name


# ── allocate_instance_id (monotonic, reserves 0) ──────────────────────────────
def test_allocate_instance_id_is_monotonic_from_one():
    srv = _server()
    assert srv.allocate_instance_id() == 1
    assert srv.allocate_instance_id() == 2
    assert srv.allocate_instance_id() == 3
    # 0 is reserved for the shared public instance.
    assert PUBLIC_INSTANCE_ID == 0


# ── _assign_instance_id (the wired policy) ────────────────────────────────────
def test_public_zone_assigns_shared_instance_zero():
    srv = _server()
    conn = FakeConn("Alice", zone_id=1, zone_name="town")
    srv._assign_instance_id(conn, FakeZone("town", is_town=True))
    assert conn.instance_id == PUBLIC_INSTANCE_ID
    # a public assignment must NOT consume a private token.
    assert srv.next_instance_id == 1


def test_private_dungeon_mints_a_token():
    srv = _server()
    conn = FakeConn("Alice", zone_id=42, zone_name="dungeon01_level01")
    srv._assign_instance_id(conn, FakeZone("dungeon01_level01"))
    assert conn.instance_id == 1


def test_two_solo_players_same_dungeon_get_distinct_instances():
    srv = _server()
    a = FakeConn("Alice", zone_id=42, zone_name="dungeon01_level01")
    b = FakeConn("Bob", zone_id=42, zone_name="dungeon01_level01")
    srv._assign_instance_id(a, FakeZone("dungeon01_level01"))
    srv._assign_instance_id(b, FakeZone("dungeon01_level01"))
    assert a.instance_id != b.instance_id
    # ...so their world-instance keys differ -> separate copies.
    assert (a.current_zone_id, a.instance_id) != (b.current_zone_id, b.instance_id)


def _transfer(srv, conn, zone):
    """Mirror the real zone-transfer order: point the conn at the destination
    FIRST (current_zone_name/_id), THEN assign the instance key."""
    conn.current_zone_name = zone.name
    srv._assign_instance_id(conn, zone)


def test_reentering_a_dungeon_mints_a_fresh_token():
    """DR dungeons are ephemeral: a new run from a public zone is a new instance."""
    srv = _server()
    conn = FakeConn("Alice", zone_id=42, zone_name="dungeon01_level01")
    _transfer(srv, conn, FakeZone("dungeon01_level01"))
    first = conn.instance_id
    # back to town (shared) then re-enter the dungeon
    _transfer(srv, conn, FakeZone("town", is_town=True))
    assert conn.instance_id == PUBLIC_INSTANCE_ID
    _transfer(srv, conn, FakeZone("dungeon01_level01"))
    assert conn.instance_id != first


def test_group_token_seam_returns_none_until_groups_wired():
    srv = _server()
    conn = FakeConn("Alice", zone_id=42, zone_name="dungeon01_level01")
    # The seam exists but is inert; a grouped player falls through to a private
    # token until the group subsystem is wired into instancing.
    assert srv._group_instance_token(conn) is None


if __name__ == "__main__":  # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
