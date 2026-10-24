"""A compiled .tft's checksums. Needs a TFT the Editor compiled: NEXTION_TOOLS_TFT=path/to/gui.tft."""

import os

import pytest

from nextion_tools import tft

TFT = os.environ.get("NEXTION_TOOLS_TFT")
pytestmark = pytest.mark.skipif(not TFT, reason="set NEXTION_TOOLS_TFT to a .tft the Editor compiled")


@pytest.fixture(scope="module")
def data():
    with open(TFT, "rb") as f:
        return f.read()


def test_reproduces_the_editors_checksums(data):
    assert tft.checksums_ok(data)


def test_detects_a_changed_byte(data):
    changed = bytearray(data)
    changed[tft.pictures(data)[-1].start + 100] ^= 1
    assert not tft.checksums_ok(bytes(changed))


def test_resealing_after_a_picture_swap_of_the_same_record_is_a_no_op(data):
    slot = tft.pictures(data)[0]
    assert tft.replace_pictures(data, {slot: data[slot.start:slot.start + slot.size]}) == data


def test_changing_the_orientation_changes_only_header_1_and_the_file_checksum(data):
    turned = tft.with_orientation(data, (tft.orientation(data) + 90) % 360)
    assert tft.checksums_ok(turned)
    assert tft.orientation(turned) == (tft.orientation(data) + 90) % 360
    changed = {i for i in range(len(data)) if data[i] != turned[i]}
    assert changed <= {tft.ORIENTATION} | set(range(0xC4, 0xC8)) | set(range(len(data) - 4, len(data)))
