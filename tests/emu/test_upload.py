"""TFT uploads: chunk acks, v1.2 jumps, misbehaviour, and how an upload ends."""

import hashlib
import os
import time

import pytest

from nextion_tools.emu.protocol import TERMINATOR, UPDATE_CHUNK

ACK = b"\x05"


def tft(size, seed=1):
    """Test data that contains FF FF FF, which must not be taken for a terminator."""
    data = bytearray(os.urandom(size))
    data[100:103] = TERMINATOR
    return bytes(data)


def upload(display, mcu, image, protocol="wri", wait_acks=True):
    mark = mcu.mark()
    mcu.send("whmi-%s %d,115200,0" % (protocol, len(image)))
    display.wait_for_upload_start(timeout=2)
    if not wait_acks:
        return mark
    mcu.wait_bytes(1, since=mark)
    sent = 0
    acks = 1
    while sent < len(image):
        chunk = image[sent:sent + UPDATE_CHUNK]
        mcu.send_raw(chunk)
        sent += len(chunk)
        if sent < len(image):
            acks += 1
            mcu.wait_bytes(acks, since=mark)
    display.wait_for(lambda d: d.upload is None, timeout=5)
    return mark


def test_a_v11_upload_lands_byte_for_byte(display, mcu):
    image = tft(3 * UPDATE_CHUNK + 500)
    mark = upload(display, mcu, image)
    u = display.last_upload
    assert u.complete and not u.abandoned
    assert u.protocol == "v1.1" and u.size == len(image) and u.baud == 115200
    assert u.md5 == hashlib.md5(image).hexdigest()
    assert display.flash == image
    assert display.display_state == "ok"
    assert [length for _, length, _ in u.chunks] == [UPDATE_CHUNK] * 3 + [500]
    # One ack for the instruction and one per chunk but the last.
    assert mcu.quiet(0.2, since=mark) == ACK * 4
    assert [data for _, data in u.acks] == [ACK] * 4


def test_tft_bytes_are_not_instructions(display, mcu):
    upload(display, mcu, tft(UPDATE_CHUNK + 10))
    assert display.commands == ["whmi-wri %d,115200,0" % (UPDATE_CHUNK + 10)]


def test_instructions_resume_after_the_upload(display, mcu):
    image = tft(200)
    mcu.send("whmi-wri %d,115200,0" % len(image))
    mcu.wait_bytes(1)
    mcu.send_raw(image + b"page home" + TERMINATOR)  # the next instruction in the same write
    display.wait_for_page("home", timeout=2)
    assert display.flash == image


def test_v12_answers_the_first_chunk_with_a_jump(display, mcu):
    image = tft(3 * UPDATE_CHUNK)
    mark = upload(display, mcu, image, protocol="wris")
    u = display.last_upload
    assert u.protocol == "v1.2"
    replies = mcu.quiet(0.2, since=mark)
    assert replies == ACK + b"\x08" + b"\x00\x00\x00\x00" + ACK
    # Sent as two writes, as a real display does: the offset arrives after the 0x08.
    jump_writes = [data for _, data in u.acks]
    assert jump_writes[1:3] == [b"\x08", b"\x00\x00\x00\x00"]
    assert display.flash == image


def test_v12_jump_skips_what_the_display_holds(display, mcu):
    old = tft(4 * UPDATE_CHUNK)
    upload(display, mcu, old)
    new = old[:2 * UPDATE_CHUNK] + tft(2 * UPDATE_CHUNK)
    display.jump_offset = 2 * UPDATE_CHUNK
    mark = mcu.mark()
    mcu.send("whmi-wris %d,115200,0" % len(new))
    mcu.wait_bytes(1, since=mark)
    mcu.send_raw(new[:UPDATE_CHUNK])
    jump = mcu.wait_bytes(6, since=mark)
    assert jump == ACK + b"\x08" + (2 * UPDATE_CHUNK).to_bytes(4, "little")
    mcu.send_raw(new[2 * UPDATE_CHUNK:3 * UPDATE_CHUNK])
    mcu.wait_bytes(7, since=mark)
    mcu.send_raw(new[3 * UPDATE_CHUNK:])
    display.wait_for(lambda d: d.upload is None, timeout=5)
    assert display.flash == new


def test_unsplit_jump(display, mcu):
    display.split_jump = False
    upload(display, mcu, tft(2 * UPDATE_CHUNK), protocol="wris")
    assert [data for _, data in display.last_upload.acks][1] == b"\x08\x00\x00\x00\x00"


def test_a_dropped_ack_is_dropped_once(display, mcu):
    display.drop_ack_at = {1}
    image = tft(2 * UPDATE_CHUNK + 1)
    mark = mcu.mark()
    mcu.send("whmi-wri %d,115200,0" % len(image))
    mcu.wait_bytes(1, since=mark)
    mcu.send_raw(image[:UPDATE_CHUNK])
    assert mcu.quiet(0.3, since=mark) == ACK  # chunk 1 not acknowledged
    assert display.drop_ack_at == set()


def test_always_drop(display, mcu):
    display.always_drop_ack_at = {1}
    for _ in range(2):
        mark = mcu.mark()
        mcu.send("whmi-wri %d,115200,0" % (2 * UPDATE_CHUNK))
        mcu.wait_bytes(1, since=mark)
        mcu.send_raw(tft(UPDATE_CHUNK))
        assert mcu.quiet(0.3, since=mark) == ACK
        display.power_cycle()


def test_duplicated_ack_and_injected_bytes(display, mcu):
    display.dup_ack_at = {1}
    display.inject_at = {2: b"\x99junk"}
    image = tft(3 * UPDATE_CHUNK)
    mark = upload(display, mcu, image)
    replies = mcu.quiet(0.3, since=mark)
    assert replies == ACK + ACK + ACK + ACK + b"\x99junk"
    assert display.flash == image


def test_reject_upload_never_acks(display, mcu):
    display.reject_upload = True
    mark = mcu.mark()
    mcu.send("whmi-wri 100,115200,0")
    display.wait_for_upload_start(timeout=2)
    assert mcu.quiet(0.3, since=mark) == b""


def test_an_upload_too_big_for_flash_is_refused(display, mcu):
    mark = mcu.mark()
    mcu.send("whmi-wri %d,115200,0" % (display.flash_size + 1))
    u = display.wait_for_upload_start(timeout=2)
    assert u.abandoned and display.upload is None
    assert mcu.quiet(0.3, since=mark) == b""


def test_power_cycle_mid_upload_breaks_the_tft(display, mcu):
    display.banner = b"NSPM"
    image = tft(3 * UPDATE_CHUNK)
    mcu.send("whmi-wri %d,115200,0" % len(image))
    mcu.wait_bytes(1)
    mcu.send_raw(image[:UPDATE_CHUNK])
    mcu.wait_bytes(2)  # chunk 1 acknowledged
    display.power_cycle()
    assert display.last_upload.abandoned
    assert display.display_state == "data_error"
    mark = mcu.mark()
    display.boot()
    assert mcu.quiet(0.2, since=mark) == b""  # SYSTEM DATA ERROR: no banner


def test_an_image_the_manager_never_served_is_a_data_error(display, mcu):
    display.valid_md5s = {"0" * 32}
    upload(display, mcu, tft(100))
    assert display.display_state == "data_error"


def test_a_served_image_is_fine(display, mcu):
    image = tft(100)
    display.valid_md5s = {hashlib.md5(image).hexdigest()}
    upload(display, mcu, image)
    assert display.display_state == "ok"


def test_ack_delay(display, mcu):
    display.ack_delay = 0.3
    start = time.monotonic()
    mcu.send("whmi-wri 100,115200,0")
    mcu.wait_bytes(1)
    assert time.monotonic() - start >= 0.29


def test_an_uploader_that_ignores_a_missing_ack_still_completes(display, mcu):
    # The display cannot tell a chunk sent without waiting for its ack from one sent after it.
    display.drop_ack_at = {1}
    image = tft(2 * UPDATE_CHUNK)
    mcu.send("whmi-wri %d,115200,0" % len(image))
    mcu.wait_bytes(1)
    mcu.send_raw(image)
    display.wait_for(lambda d: d.last_upload.complete, timeout=2)
    assert display.flash == image


def test_bytes_after_the_image_are_instructions_again(display, mcu):
    image = tft(10)
    mcu.send("whmi-wri %d,115200,0" % len(image))
    mcu.wait_bytes(1)
    mcu.send_raw(image + b"dim=7" + TERMINATOR)
    display.wait_for(lambda d: d.system.get("dim") == 7, timeout=2)
    assert display.last_upload.overrun == 0


def test_an_upload_under_the_flow_model(display, mcu):
    display.flow_model = True
    image = tft(2 * UPDATE_CHUNK + 7)
    upload(display, mcu, image)
    assert display.flash == image
    assert display.commands == ["whmi-wri %d,115200,0" % len(image)]
    mcu.send("page home")
    display.wait_for_page("home", timeout=2)  # the model runs again afterwards
    assert not display.flow.paused


def test_power_cycle_unpauses_the_flow_model(display, mcu):
    display.flow_model = True
    mcu.send("whmi-wri 100000,115200,0")
    display.wait_for_upload_start(timeout=2)
    assert display.flow.paused
    display.power_cycle()
    assert not display.flow.paused
    mcu.send("page home")
    display.wait_for_page("home", timeout=2)


@pytest.mark.parametrize("protocol", ["wri", "wris"])
def test_two_uploads_in_a_row(display, mcu, protocol):
    first, second = tft(UPDATE_CHUNK + 1), tft(2 * UPDATE_CHUNK + 1)
    upload(display, mcu, first, protocol)
    upload(display, mcu, second, protocol)
    assert len(display.uploads) == 2
    assert display.flash == second
