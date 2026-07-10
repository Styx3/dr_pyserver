"""Tests for the tile/cobj resolver's room-node variant expansion + index safety.

``variants_for`` reproduces the client's per-tileset variant vector: the
exit-suffixed ``.tile`` leaves sorted alphabetically. The ordering rule was
proven against the live client via x64dbg (2026-06-09) — ``cat_up_`` →
``[1e,1n,1s,1w]`` and ``…cart_`` → ``[1e1s1w_b, 1n_a, 1s_b]``. These tests inject
a synthetic index so they don't depend on the extracter being present.
"""
import drserver.managers.tile_cobj_resolver as r


def _seed_index(leaves):
    """Force a synthetic in-memory tile index (no disk scan)."""
    r._state._tile = {leaf.lower(): f"/fake/{leaf}.tile" for leaf in leaves}
    r._state._cobj = {}  # non-None so _ensure_indexed short-circuits


def teardown_function(_):
    r.reset_index()


def test_variants_sorted_alphabetically_and_suffix_filtered():
    # Arrange: 4 real variants plus a non-variant tile under the same prefix.
    _seed_index([
        "cat_up_1n", "cat_up_1w", "cat_up_1e", "cat_up_1s", "cat_up_special",
    ])

    # Act
    out = r.variants_for("cat_up_")

    # Assert: the 4 exit-suffixed leaves, alphabetical; "special" excluded.
    assert out == ["cat_up_1e", "cat_up_1n", "cat_up_1s", "cat_up_1w"]


def test_variants_match_client_cart_order():
    """Mixed exit-counts + _a/_b shape letters sort exactly as the client's vector."""
    base = "dungeon_eleven_levels_five_to_seven_cart_"
    _seed_index([base + "1n_a", base + "1s_b", base + "1e1s1w_b"])

    out = r.variants_for(base)

    assert out == [base + "1e1s1w_b", base + "1n_a", base + "1s_b"]


def test_variants_handle_multi_exit_and_plain_suffix():
    _seed_index([
        "elmforest_questfindring_1w", "elmforest_questfindring_1n1e1w",
    ])

    out = r.variants_for("elmforest_questfindring_")

    assert out == [
        "elmforest_questfindring_1n1e1w", "elmforest_questfindring_1w",
    ]


def test_variants_empty_when_no_match():
    _seed_index(["othertheme_1n", "othertheme_1s"])

    assert r.variants_for("cat_up_") == []


def test_empty_index_is_not_cached(monkeypatch):
    """A transiently-unreachable extracter root must not poison the process-wide
    cache with an empty index — the next call must re-scan."""
    r.reset_index()
    monkeypatch.setattr(r._state, "_resolve_root", lambda: None)

    r._state._ensure_indexed()

    assert r._state._cobj is None  # not cached → will retry
    assert r._state._tile is None


# ── per-tileset TileSize (cell stride) ──────────────────────────────────────

def _write_world_base(tmp_path, name, tileset, tilesize):
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    decl = f"\tTileSet = {tileset};\n" if tileset else ""
    (base / f"{name}.gc").write_text(
        f"{name} extends base.world\n{{\n"
        f"{decl}\tTileSize = {tilesize};\n}}\n",
        encoding="utf-8",
    )


def test_tile_size_for_reads_per_theme_size(tmp_path, monkeypatch):
    """``tile_size_for`` parses ``TileSet``/``TileSize`` from ``base/World_*.gc``
    so the maze spaces cells by the client's per-tileset stride (cave 360 ≠
    elmforest 400)."""
    _write_world_base(tmp_path, "World_elmforest", "elmforest_tileset_", 400)
    _write_world_base(tmp_path, "World_cave_small", "cave_small_tileset_", 360)
    _write_world_base(tmp_path, "World_ruins", "ruins_tileset_", 280)
    monkeypatch.setattr(r._state, "_resolve_root", lambda: str(tmp_path))

    assert r.tile_size_for("cave_small_tileset_") == 360
    assert r.tile_size_for("ruins_tileset_") == 280
    assert r.tile_size_for("elmforest_tileset_") == 400  # case-insensitive prefix
    assert r.tile_size_for("ELMFOREST_TILESET_") == 400


def test_tile_size_for_defaults_to_400_when_unknown(tmp_path, monkeypatch):
    """Unknown prefix, empty prefix, and an unreachable root all fall back to the
    elmforest default (400) rather than raising."""
    _write_world_base(tmp_path, "World_cave_small", "cave_small_tileset_", 360)
    monkeypatch.setattr(r._state, "_resolve_root", lambda: str(tmp_path))

    assert r.tile_size_for("nonexistent_tileset_") == r.DEFAULT_TILE_SIZE == 400
    assert r.tile_size_for("") == 400

    # Unreachable root must not cache an empty map (re-scan next time).
    r.reset_index()
    monkeypatch.setattr(r._state, "_resolve_root", lambda: None)
    assert r.tile_size_for("cave_small_tileset_") == 400
    assert r._state._tile_sizes is None


def test_tile_size_keyed_by_filename_family_not_declared_tileset(tmp_path, monkeypatch):
    """The authoritative key is the filename family (``World_<fam>`` →
    ``<fam>_tileset_``), matching the importer's ``tile_prefix`` — even when the
    file's declared ``TileSet`` differs (``World_lavacaves`` declares
    ``lavapool_tileset_``). Without this, lavacaves (6 levels) would mis-resolve
    to the 400 default instead of 360."""
    _write_world_base(tmp_path, "World_lavacaves", "lavapool_tileset_", 360)
    monkeypatch.setattr(r._state, "_resolve_root", lambda: str(tmp_path))

    assert r.tile_size_for("lavacaves_tileset_") == 360   # family key (DB prefix)
    assert r.tile_size_for("lavapool_tileset_") == 360     # declared key (bonus)


def test_boss_worlds_skipped_so_collision_is_deterministic(tmp_path, monkeypatch):
    """``World_boss_*.gc`` re-declare a prefix with a CONFLICTING size
    (crypt 200 vs 280, shadow 480 vs 400). They are single-room arenas, not
    procedural mazes, and must be skipped so the canonical world's size wins
    regardless of filesystem iteration order."""
    _write_world_base(tmp_path, "World_crypt", "crypt_tileset_", 280)
    _write_world_base(tmp_path, "World_boss_dungeon04", "crypt_tileset_", 200)
    _write_world_base(tmp_path, "World_shadow", "shadow_tileset_", 400)
    _write_world_base(tmp_path, "World_boss_dungeon09", "shadow_tileset_", 480)
    monkeypatch.setattr(r._state, "_resolve_root", lambda: str(tmp_path))

    assert r.tile_size_for("crypt_tileset_") == 280   # World_crypt, not boss 200
    assert r.tile_size_for("shadow_tileset_") == 400  # World_shadow, not boss 480
    # Boss files are skipped entirely — they register no key at all.
    assert r.tile_size_for("boss_dungeon04_tileset_") == r.DEFAULT_TILE_SIZE
