import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from nextion_tools import images  # noqa: E402


def test_bmp_round_trip(tmp_path):
    pixels = np.array([[(i, j, i ^ j) for i in range(5)] for j in range(3)], dtype=np.uint8)  # odd width pads rows
    path = str(tmp_path / "x.bmp")
    images.write_bmp(path, pixels)
    np.testing.assert_array_equal(images.read(path), pixels)


def test_rgb565_truncates_each_channel():
    pixels = np.array([[(0xFF, 0x00, 0x00), (0x07, 0xFF, 0x07), (0x08, 0x04, 0xF8)]], dtype=np.uint8)
    assert images.rgb565(pixels) == bytes([0x00, 0xF8, 0xE0, 0x07, 0x3F, 0x08])
