"""Avatar entity-init (OP12 / OP6) byte-layout regression.

The avatar's ``EntityInit`` block (WorldEntity → Unit → Hero → Avatar WriteInit)
is byte-identical for the player's own avatar (spawn OP12) and a remote player's
avatar (other-player OP6). Per RR ``Unit.WriteInit`` with ``UnitFlags = 0x07``,
the Unit block writes ownerID (bit ``0x01``), then **HP** (bit ``0x02``), then
**MP** (bit ``0x04``). So the FIRST uint32 after ownerID is the avatar's HP — the
value the client adopts as its local synch field ``entity[+0xbc]`` and compares
against every ``0x02`` synch trailer. Writing the ``0xFFFF00`` mana sentinel there
(the old bug) made the client default its avatar HP to 65535 and then fatally
mismatch the trailers on ``dungeon00_level01`` (Avatar synch crash, exit
0xc000013a). HP must be a real ×256 wire value.
"""
import pytest

from drserver.net.spawn import write_avatar_entity_init
from drserver.util.byte_io import LEWriter, LEReader


def _build(hp_wire: int, mana_wire: int, *, level: int = 1, owner_id: int = 0x0011,
           avatar_id: int = 0x1234):
    w = LEWriter()
    write_avatar_entity_init(
        w,
        avatar_id=avatar_id,
        hp_wire=hp_wire,
        mana_wire=mana_wire,
        exp=0,
        level=level,
        owner_id=owner_id,
        heading_wire=0,
        stat_strength=1, stat_agility=2, stat_endurance=3, stat_intellect=4,
        stat_pts_remaining=0, respec_remaining=0,
        pvp_wins=0, pvp_rating=0,
        face=0, hair=0, hair_color=0,
    )
    return w.to_array()


def _read_to_hp_mana(pkt: bytes):
    """Walk the EntityInit header and return (hp, mana, reader-positioned-after)."""
    r = LEReader(pkt)
    assert r.read_byte() == 0x02            # EntityInit opcode
    assert r.read_uint16() == 0x1234        # avatar id
    # visible only — NO blocking bit: a self-collider wedges the avatar in
    # place (live-proven 2026-06-11; see write_avatar_entity_init docstring).
    assert r.read_uint32() == 0x04          # worldEntityFlags
    r.read_int32(); r.read_int32(); r.read_int32()   # pos x/y/z
    r.read_int32()                          # heading
    assert r.read_byte() == 0x01            # worldEntityInitFlags
    assert r.read_uint16() == 0             # Unk1Case
    assert r.read_byte() == 0x07            # unitFlags (0x01 owner | 0x02 HP | 0x04 MP)
    level = r.read_byte()
    assert r.read_uint16() == 0             # UnitUnkUint16_0
    assert r.read_uint16() == 0             # UnitUnkUint16_1
    owner = r.read_uint16()                 # ownerID (UnitFlags 0x01)
    hp = r.read_uint32()                    # HP (UnitFlags 0x02) — synch field
    mana = r.read_uint32()                  # MP (UnitFlags 0x04)
    return hp, mana, level, owner, r


@pytest.mark.unit
def test_hp_is_written_before_mana():
    # Arrange / Act
    hp, mana, _level, _owner, _r = _read_to_hp_mana(_build(68096, 0xFFFF00))

    # Assert — HP comes first (UnitFlags 0x02), mana second (0x04).
    assert hp == 68096
    assert mana == 0xFFFF00


@pytest.mark.unit
def test_hp_field_is_not_the_mana_sentinel():
    # The fatal bug: HP field carried 0xFFFF00 (the mana sentinel) → client read
    # its avatar HP as 65535 and crashed on the synch compare.
    hp, _mana, _level, _owner, _r = _read_to_hp_mana(_build(68096, 0xFFFF00))
    assert hp != 0xFFFF00
    assert hp == 266 * 256


@pytest.mark.unit
def test_ownerid_and_level_preserved():
    _hp, _mana, level, owner, _r = _read_to_hp_mana(_build(68096, 0xFFFF00, level=20))
    assert level == 20
    assert owner == 0x0011


@pytest.mark.unit
def test_avatar_trailer_is_exp_stats_pvp_face_hair():
    # Walk the Hero/Avatar tail after HP/MP and confirm the appearance trailer.
    _hp, _mana, _level, _owner, r = _read_to_hp_mana(_build(68096, 0xFFFF00))
    assert r.read_uint32() == 0             # ExpThisLevel
    assert r.read_uint16() == 1             # STR
    assert r.read_uint16() == 2             # AGI
    assert r.read_uint16() == 3             # END
    assert r.read_uint16() == 4             # INT
    assert r.read_uint16() == 0             # StatPtsRemaining
    assert r.read_uint16() == 0             # respec
    assert r.read_uint32() == 0             # pvpWins
    assert r.read_uint32() == 0             # pvpRating
    assert r.read_byte() == 0               # face
    assert r.read_byte() == 0               # hair
    assert r.read_byte() == 0               # hair_color
    assert r.remaining == 0


if __name__ == "__main__":
    import sys
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
