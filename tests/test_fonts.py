"""The .zi decoder on hand-made data."""

import struct

import pytest

pytest.importorskip("numpy")

from nextion_tools import fonts  # noqa: E402


def make_zi(name, height, glyphs, encoding="utf-8"):
    """A .zi holding glyphs of (code, advance, left, right, data), laid out as the Editor does."""
    names = name.encode() + encoding.encode()
    header = bytearray(fonts.HEADER)
    header[0:3] = fonts.MAGIC
    header[7] = height
    struct.pack_into("<I", header, 0x0C, len(glyphs))
    header[0x10] = 6
    header[0x11] = len(names)
    struct.pack_into("<I", header, 0x20, len(name))
    table = bytearray()
    data = bytearray()
    for code, advance, left, right, glyph_data in glyphs:
        offset = 10 * len(glyphs) + len(data)
        table += struct.pack("<HBBB", code, advance, left, right) + offset.to_bytes(3, "little")
        table += struct.pack("<H", len(glyph_data))
        data += glyph_data
    return bytes(header) + names + bytes(table) + bytes(data)


def decode(tokens, width, height):
    return fonts.decode(bytes([0x03]) + bytes(tokens), width, height)


def test_runs_of_clear_and_ink():
    cov, coded = decode([0b00_0_00011, 0b00_1_00010], 5, 1)
    assert cov.tolist() == [[0, 0, 0, 255, 255]]
    assert coded == 5


def test_clear_then_one_or_two_ink():
    cov, _ = decode([0b01_0_00010, 0b01_1_00001], 6, 1)
    assert cov.tolist() == [[0, 0, 255, 0, 255, 255]]


def test_clear_then_one_partial_pixel():
    cov, _ = decode([0b10_011_111, 0b10_000_011], 6, 1)
    assert cov.tolist() == [[0, 0, 0, 255, 255 * 3 // 7, 0]]


def test_two_partial_pixels():
    cov, _ = decode([0b11_001_110], 2, 1)
    assert cov.tolist() == [[255 * 1 // 7, 255 * 6 // 7]]


def test_fills_row_by_row():
    cov, _ = decode([0b00_1_00011, 0b00_0_00011], 3, 2)
    assert cov.tolist() == [[255, 255, 255], [0, 0, 0]]


def test_a_short_stream_leaves_the_rest_clear():
    cov, coded = decode([0b00_1_00010], 3, 2)
    assert cov.tolist() == [[255, 255, 0], [0, 0, 0]]
    assert coded == 2


def test_a_long_stream_is_cut_to_the_box_and_says_so():
    cov, coded = decode([0b00_1_00101], 2, 2)
    assert cov.tolist() == [[255, 255], [255, 255]]
    assert coded == 5


def test_the_mode_byte_is_not_pixels():
    black_and_white, _ = fonts.decode(bytes([0x01, 0b00_1_00001]), 1, 1)
    assert black_and_white.tolist() == [[255]]


def test_empty_data_is_a_clear_box():
    cov, coded = fonts.decode(b"", 4, 3)
    assert cov.shape == (3, 4)
    assert not cov.any()
    assert coded == 0


@pytest.fixture
def font():
    return fonts.parse(make_zi("tiny", 2, [
        (ord("a"), 2, 0, 0, bytes([0x01, 0b00_1_00100])),
        (ord("b"), 1, 1, 1, bytes([0x01, 0b00_1_00011, 0b00_0_00011])),
        (0x0129, 3, 0, 0, bytes([0x01])),
    ]))


def test_names_have_no_separator(font):
    assert (font.name, font.encoding) == ("tiny", "utf-8")


def test_header_fields(font):
    assert (font.height, font.version, len(font.glyphs)) == (2, 6, 3)


def test_glyph_box_includes_the_overhangs(font):
    b = font.glyph("b")
    assert (b.left, b.advance, b.right) == (1, 1, 1)
    assert b.coverage.tolist() == [[255, 255, 255], [0, 0, 0]]


def test_glyph_data_offsets_are_from_the_map(font):
    assert font.glyph("a").coverage.tolist() == [[255, 255], [255, 255]]


def test_codes_beyond_latin1(font):
    assert font.glyph("ĩ").coverage.shape == (2, 3)


def test_missing_and_width(font):
    assert font.glyph("z") is None
    assert font.missing("abzaz?") == "z?"
    assert font.width("abz") == 3


def test_rejects_other_files():
    with pytest.raises(ValueError):
        fonts.parse(b"\x00" * 64)
