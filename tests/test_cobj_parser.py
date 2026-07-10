"""Unit tests for the ``.cobj`` collision-file parser (Phase B geometry pipeline).

The parser is a faithful port of C# ``CobjParser`` and the format is fully
specified, so these are real round-trip tests: synthesise the documented
little-endian layout, parse it back, and assert every field. One extra test
parses a real extracted ``.cobj`` (gated on the client content being present).
"""
import os

import pytest

import _paths  # noqa: F401  (sets up sys.path)
from drserver.data import cobj_parser
from drserver.util.byte_io import LEWriter

_CLASS = b"HybridCollisionObject"


def _build_cobj(*, tag=0x05, dfc_hash=0xDEADBEEF,
                cell_size1=5, origin1=(0, 0), dims1=(2, 2), heightmap=None,
                cell_size2=5, origin2=(0, 0, 0), dims2=(1, 2), bbox_cells=None):
    """Serialise a cobj per the documented layout, for round-trip parsing."""
    w1, h1 = dims1
    if heightmap is None:
        heightmap = [0] * (w1 * h1)
    w2, h2 = dims2
    if bbox_cells is None:
        bbox_cells = [[] for _ in range(w2 * h2)]

    wr = LEWriter()
    wr.write_byte(tag)
    wr.write_bytes(_CLASS)
    wr.write_byte(0x00)
    wr.write_uint32(dfc_hash)

    wr.write_int32(cell_size1)
    wr.write_int32(origin1[0])
    wr.write_int32(origin1[1])
    wr.write_int32(w1)
    wr.write_int32(h1)
    for v in heightmap:
        wr.write_uint16(v)

    wr.write_int32(cell_size2)
    wr.write_int32(origin2[0])
    wr.write_int32(origin2[1])
    wr.write_int32(origin2[2])
    wr.write_int32(w2)
    wr.write_int32(h2)
    wr.write_int32(0)  # depth2
    for cell in bbox_cells:
        wr.write_uint16(len(cell))
        for z_low, z_high in cell:
            wr.write_uint16(z_low & 0xFFFF)
            wr.write_uint16(z_high & 0xFFFF)
    return wr.to_array()


def test_parses_header_and_heightmap():
    # Arrange
    data = _build_cobj(dims1=(2, 2), heightmap=[10, 20, 30, 99], cell_size1=5,
                       origin1=(-40, -40))

    # Act
    cobj = cobj_parser.parse(data)

    # Assert
    assert cobj.dfc_hash == 0xDEADBEEF
    assert cobj.width1 == 2 and cobj.height1 == 2
    assert cobj.cell_size1 == 5
    assert cobj.origin_x1 == -40 and cobj.origin_y1 == -40
    assert cobj.heightmap == (10, 20, 30, 99)
    assert cobj.bytes_consumed == len(data)


def test_get_height_indexes_row_major_and_clamps():
    cobj = cobj_parser.parse(_build_cobj(dims1=(2, 2), heightmap=[1, 2, 3, 4]))
    assert cobj.get_height(0, 0) == 1
    assert cobj.get_height(1, 0) == 2
    assert cobj.get_height(0, 1) == 3
    assert cobj.get_height(1, 1) == 4
    assert cobj.get_height(-1, 0) == 0  # out of range -> 0
    assert cobj.get_height(5, 5) == 0


def test_subshape2_bbox_signed_conversion():
    # A bbox stack with a negative zLow exercises the (short) cast.
    cells = [[(-12, 40)], []]  # cell 0 has one bbox, cell 1 empty
    data = _build_cobj(dims2=(1, 2), bbox_cells=cells)
    cobj = cobj_parser.parse(data)
    assert cobj.width2 == 1 and cobj.height2 == 2
    c0 = cobj.get_cell(0, 0)
    assert len(c0.bboxes) == 1
    assert c0.bboxes[0].z_low == -12  # signed, not 0xFFF4
    assert c0.bboxes[0].z_high == 40
    assert cobj.get_cell(0, 1).bboxes == ()
    assert cobj.bytes_consumed == len(data)


def test_rejects_wrong_class_name():
    data = bytearray(_build_cobj())
    data[1] = ord("X")  # corrupt the class name
    with pytest.raises(ValueError):
        cobj_parser.parse(bytes(data))


def test_rejects_truncated_buffer():
    with pytest.raises(ValueError):
        cobj_parser.parse(b"\x05short")


def test_parses_real_extracted_cobj():
    extracter = os.path.normpath(os.path.join(_paths.REPO_ROOT, "..", "extracter"))
    sample = os.path.join(extracter, "AutumnForest_Ruins_PillarStraight_2.cobj")
    if not os.path.isfile(sample):
        pytest.skip("extracter content not present")

    cobj = cobj_parser.parse_file(sample)

    # Consumes the whole file and yields sane grid dimensions.
    assert cobj.bytes_consumed == os.path.getsize(sample)
    assert 0 <= cobj.width1 <= 1024 and 0 <= cobj.height1 <= 1024
    assert 0 <= cobj.width2 <= 1024 and 0 <= cobj.height2 <= 1024
    assert len(cobj.heightmap) == cobj.width1 * cobj.height1
    assert len(cobj.cells) == cobj.width2 * cobj.height2
