"""Avatar (player) max HP / mana — port of C# Networking/PlayerState.cs (runtime).

The client computes its own avatar max HP locally and, on certain zones (notably
``dungeon00_level01``), compares it against the value the server sends in the
avatar create packet and the ``0x02`` synch trailers. A mismatch fires
``ClientEntityManager::processComponentUpdate`` "Entity synch error" — a *fatal*
crash on the Avatar type (``exit 0xc000013a``), not just a popup.

Ground truth (live 2026-06-01): Styx3 = Level 1 Mage reads HP 266 / MP 175. That
is ``66 * 256 == 68096`` HP wire and ``175 * 256 == 44800`` mana wire — so the
avatar synch field is **×256 WIRE, not raw**. The base is class-INDEPENDENT;
class/gear differences enter as equipment + modifier bonuses on top.

Formula (C# Networking/PlayerState.cs):

    CalculateBaseHP()  = BASE_HP_WIRE(68096) + (level-1) * heroHealthPerLevel(16)*256
    CalculateMaxMana() = 44800             + (level-1) * powerPerLevel(5)*256
"""
import pytest

from drserver.data.player_state import (
    compute_avatar_max_hp_wire,
    compute_avatar_max_mana_wire,
    resolve_synch_hp_wire,
)

_HP_PER_LEVEL_WIRE = 16 * 256   # 4096
_MANA_PER_LEVEL_WIRE = 5 * 256  # 1280


@pytest.mark.unit
def test_level_1_base_hp_is_68096_wire():
    # Ground truth: Styx3 L1 Mage -> HP 266 == 68096 wire.
    assert compute_avatar_max_hp_wire(1) == 68096
    assert compute_avatar_max_hp_wire(1) // 256 == 266


@pytest.mark.unit
def test_level_1_base_mana_is_44800_wire():
    # Ground truth: Styx3 L1 Mage -> MP 175 == 44800 wire.
    assert compute_avatar_max_mana_wire(1) == 44800
    assert compute_avatar_max_mana_wire(1) // 256 == 175


@pytest.mark.unit
def test_hp_is_class_independent():
    # Base HP carries no class/stat term — only level matters.
    assert compute_avatar_max_hp_wire(1) == 68096


@pytest.mark.unit
def test_hp_adds_4096_wire_per_level():
    # heroHealthPerLevel 16 * 256 = 4096 wire per level beyond the first.
    assert compute_avatar_max_hp_wire(2) == 68096 + _HP_PER_LEVEL_WIRE
    assert compute_avatar_max_hp_wire(10) == 68096 + 9 * _HP_PER_LEVEL_WIRE


@pytest.mark.unit
def test_mana_adds_1280_wire_per_level():
    # powerPerLevel 5 * 256 = 1280 wire per level. Verified: 175 + 9*5 = 220 @ L10.
    assert compute_avatar_max_mana_wire(10) == 44800 + 9 * _MANA_PER_LEVEL_WIRE
    assert compute_avatar_max_mana_wire(10) // 256 == 220


@pytest.mark.unit
def test_level_20_hp_wire():
    # J4FUN (drserver DB): Level 20 -> 68096 + 19*4096 = 145920 wire (570 HP).
    assert compute_avatar_max_hp_wire(20) == 68096 + 19 * _HP_PER_LEVEL_WIRE
    assert compute_avatar_max_hp_wire(20) // 256 == 570


@pytest.mark.unit
def test_is_wire_not_raw():
    # Wire HP is the ×256 value, so it is far larger than a raw HP count.
    assert compute_avatar_max_hp_wire(1) == 266 * 256
    assert compute_avatar_max_hp_wire(1) > 200  # not a raw-HP figure


@pytest.mark.unit
def test_level_zero_treated_as_level_one():
    assert compute_avatar_max_hp_wire(0) == compute_avatar_max_hp_wire(1)
    assert compute_avatar_max_mana_wire(0) == compute_avatar_max_mana_wire(1)


@pytest.mark.unit
def test_equipment_hp_bonus_adds_on_top():
    bonus = 25 * 256  # +25 HP from gear, in wire format
    assert compute_avatar_max_hp_wire(1, equipment_hp_bonus_wire=bonus) == 68096 + bonus


@pytest.mark.unit
def test_equipment_mana_bonus_adds_on_top():
    bonus = 10 * 256
    assert compute_avatar_max_mana_wire(1, equipment_mana_bonus_wire=bonus) == 44800 + bonus


@pytest.mark.unit
def test_negative_equipment_bonus_clamped_to_zero():
    assert compute_avatar_max_hp_wire(1, equipment_hp_bonus_wire=-9999) == 68096
    assert compute_avatar_max_mana_wire(1, equipment_mana_bonus_wire=-9999) == 44800


# ── resolve_synch_hp_wire (C# PlayerState.SynchHP) ───────────────────────────
# SynchHP = HasClientHP && current < sync ? current : sync. The level-up refresh
# must not clobber a damaged client-reported HP back up to the level max (the
# heartbeat would then ship a value above the client's local HP -> zero-tolerance
# FUN_005dd900 mismatch -> fatal Avatar synch crash).

@pytest.mark.unit
def test_resolve_returns_max_when_no_client_report():
    # Fresh spawn: no client report yet -> send the level-derived max.
    assert resolve_synch_hp_wire(72192, None) == 72192


@pytest.mark.unit
def test_resolve_prefers_lower_client_hp():
    # Client self-sims damage below max -> echo the client's value, not the max.
    assert resolve_synch_hp_wire(72192, 69750) == 69750


@pytest.mark.unit
def test_resolve_returns_max_when_client_at_or_above_max():
    # Client refilled to (or reports at/above) max -> send the max.
    assert resolve_synch_hp_wire(72192, 72192) == 72192
    assert resolve_synch_hp_wire(72192, 80000) == 72192


@pytest.mark.unit
def test_resolve_ignores_zero_client_hp():
    # 0 = death (handled via respawn), never echoed as a live synch value.
    assert resolve_synch_hp_wire(72192, 0) == 72192
