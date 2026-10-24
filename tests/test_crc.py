from nextion_tools import crc


def test_is_the_standard_crc32_mpeg2():
    # The catalogued check value: CRC-32/MPEG-2 of b"123456789".
    assert crc.crc32_mpeg2(b"123456789") == 0x0376E6E7


def test_widens_each_byte_to_a_big_endian_word():
    assert crc.widen(b"\x01\xff") == b"\0\0\0\x01\0\0\0\xff"
