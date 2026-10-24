"""Display in ideal mode: what it models, and how it replies."""

import socket
import struct
import time

import pytest

from nextion_tools.emu import COMOK, NotConnected, protocol
from nextion_tools.emu.protocol import TERMINATOR

OK = b"\x01"
INVALID = b"\x1a"


def test_connect_answers_comok(display, mcu):
    mcu.send("connect")
    assert mcu.wait_frames(1) == [COMOK]


def test_bkcmd_starts_at_2_failures_only(display, mcu):
    assert display.bkcmd == 2
    mark = mcu.mark()
    mcu.send("dim=50", "DRAKJHSUYDGBNCJHGJKSHBDN")
    assert mcu.wait_frames(1, since=mark) == [INVALID]
    assert mcu.quiet(0.2, since=mark) == INVALID + TERMINATOR


@pytest.mark.parametrize("level, expected", [
    (0, b""),
    (1, OK + TERMINATOR),
    (2, INVALID + TERMINATOR),
    (3, OK + TERMINATOR + INVALID + TERMINATOR),
])
def test_bkcmd_levels(display, mcu, level, expected):
    mcu.send("bkcmd=%d" % level)
    display.wait_for(lambda d: d.bkcmd == level)
    time.sleep(0.1)
    mark = mcu.mark()
    mcu.send("dim=50", "bad syntax here")
    assert mcu.quiet(0.3, since=mark) == expected


def test_bkcmd_3_acks_its_own_setting(display, mcu):
    mcu.send("bkcmd=3")
    assert mcu.wait_frames(1) == [OK]


def test_assignments_build_the_model(display, mcu):
    mcu.send("page home", 'home.room.txt="Kitchen"', "b_scenes.pic=59", "dim=80", "vis b_x,0")
    display.wait_for(lambda d: d.mark() == 5)
    assert display.page == "home"
    assert display.page_history == ["home"]
    assert display.components["home.room.txt"] == "Kitchen"
    assert display.components["home.b_scenes.pic"] == 59  # unqualified: the current page
    assert display.components["home.b_x.vis"] == 0
    assert display.system["dim"] == 80
    assert display.get("room.txt") == "Kitchen"
    assert display.commands == ["page home", 'home.room.txt="Kitchen"', "b_scenes.pic=59", "dim=80", "vis b_x,0"]


def test_qualified_names_are_kept_across_pages(display, mcu):
    mcu.send("page home", "screensaver.curTemp.txt=\"20\"", "page screensaver")
    display.wait_for_page("screensaver")
    assert display.get("curTemp.txt") == "20"
    assert display.page_history == ["home", "screensaver"]


def test_compound_assignment_and_increment(display, mcu):
    mcu.send("page p", "n0.val=5", "n0.val+=3", "n0.val*=2", "n0.val-=1", "n0.val/=2", "n0.val%=4",
             "n0.val++", "t0.txt=\"a\"", "t0.txt+=\"b\"", "sys0=1", "sys0--")
    display.wait_for(lambda d: d.mark() == 12)
    assert display.get("n0.val") == ((5 + 3) * 2 - 1) // 2 % 4 + 1
    assert display.get("t0.txt") == "ab"
    assert display.system["sys0"] == 0


def test_bad_arithmetic_fails(display, mcu):
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = mcu.mark()
    mcu.send("page p", "n0.val=1", "n0.val/=0", 't0.txt="x"', "t0.txt-=\"y\"", "t0.txt++")
    assert mcu.wait_frames(6, since=mark) == [OK, OK, INVALID, OK, INVALID, INVALID]
    assert [code for _, code in display.errors] == [0x1A, 0x1A, 0x1A]


def test_get_answers_numbers_and_strings_without_an_ack(display, mcu):
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    display.set_value("home.s_brightness", 42)
    mark = mcu.mark()
    mcu.send("page home", "t0.txt=\"20°\"", "get home.s_brightness.val", "get t0.txt", "get nothing.val")
    frames = mcu.wait_frames(5, since=mark)
    assert frames[0] == OK and frames[1] == OK
    assert frames[2] == b"\x71" + struct.pack("<i", 42)
    assert frames[3] == b"\x70" + "20°".encode()
    assert frames[4] == b"\x71" + struct.pack("<i", 0)  # nothing assigned yet: 0
    assert mcu.quiet(0.2, since=mark) == b"".join(f + TERMINATOR for f in frames)


def test_get_of_a_negative_number(display, mcu):
    display.set_value("p.n0", -5)
    mcu.send("get p.n0.val")
    # The value is all FF bytes, so read by length, as the MCU must.
    assert mcu.wait_bytes(8) == b"\x71" + struct.pack("<i", -5) + TERMINATOR


def test_printh_echoes_without_an_ack(display, mcu):
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = mcu.mark()
    mcu.send("printh a5 01 ff ff ff")
    assert mcu.wait_bytes(5, since=mark) == b"\xa5\x01\xff\xff\xff"
    assert mcu.quiet(0.2, since=mark) == b"\xa5\x01\xff\xff\xff"


def test_bad_printh_fails(display, mcu):
    mcu.send("printh 9")
    assert mcu.wait_frames(1) == [INVALID]


def test_drawing_instructions_are_recorded_and_acked(display, mcu):
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = mcu.mark()
    mcu.send("fill 0,0,10,10,0", "cls BLACK", "ref 0", "tsw b0,0")
    assert mcu.wait_frames(4, since=mark) == [OK] * 4
    assert display.commands[-4:] == ["fill 0,0,10,10,0", "cls BLACK", "ref 0", "tsw b0,0"]


def test_delay_acks_after_it_ends_even_in_ideal_mode(display, mcu):
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = mcu.mark()
    mcu.send("delay=200")
    assert mcu.wait_frames(1, since=mark) == [OK]


def test_page_by_number(display, mcu):
    mcu.send("page home", "page screensaver", "page 0")
    display.wait_for(lambda d: len(d.page_history) == 3)
    assert display.page_history == ["home", "screensaver", "home"]
    assert display.page_id("home") == 0


def test_resolver_rejects_unknown_names_with_0x1a(display, mcu):
    known = {"home", "home.room.txt", "dim", "bkcmd"}
    display.resolver = known.__contains__
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = mcu.mark()
    mcu.send("page home", 'room.txt="x"', 'forDay1.txt="Mon"', "dim=5", "nosuch=1", "page nowhere",
             "get forDay1.txt", "vis forDay1,0")
    assert mcu.wait_frames(8, since=mark) == [OK, OK, INVALID, OK, INVALID, INVALID, INVALID, INVALID]
    assert "home.forDay1.txt" not in display.components
    assert display.page == "home"
    assert [text for text, _ in display.errors] == ['forDay1.txt="Mon"', "nosuch=1", "page nowhere",
                                                    "get forDay1.txt", "vis forDay1,0"]


def test_touch_press_and_slide(display, mcu):
    mcu.send("page home")
    display.wait_for_page("home")
    mark = mcu.mark()
    display.press(22, hold=0.01)
    assert mcu.wait_frames(2, since=mark) == [b"\x65\x00\x16\x01", b"\x65\x00\x16\x00"]
    mark = mcu.mark()
    display.slide("home.s_brightness", 77, component_id=2)
    assert mcu.wait_frames(1, since=mark) == [b"\x65\x00\x02\x00"]
    assert display.get("home.s_brightness.val") == 77


def test_touch_on_another_page(display, mcu):
    mcu.send("page home")
    display.wait_for_page("home")
    mark = mcu.mark()
    display.touch(3, True, page="screensaver")
    assert mcu.wait_frames(1, since=mark) == [b"\x65\x01\x03\x01"]


def test_send_frame_and_raw(display, mcu):
    display.send_frame(0x92)
    display.send_raw(b"NSPM")
    assert mcu.wait_bytes(8) == b"\x92\xff\xff\xffNSPM"


def test_boot_sends_startup_frames_and_banner(display, mcu):
    display.startup_frames = True
    display.banner = b"NSPM"
    display.boot()
    assert mcu.wait_bytes(14) == b"\x00\x00\x00\xff\xff\xff\x88\xff\xff\xffNSPM"


def test_boot_sends_nothing_by_default(display, mcu):
    display.boot()
    assert mcu.quiet(0.2) == b""


def test_power_cycle_forgets_everything(display, mcu):
    mcu.send("bkcmd=0", "page home", "t0.txt=\"x\"", "dim=10")
    display.wait_for(lambda d: d.mark() == 4)
    display.power_cycle()
    assert display.bkcmd == 2
    assert display.page is None
    assert display.components == {} and display.system == {}
    assert display.page_history == ["home"]  # history is the test's, not the display's
    assert display.power_cycles == 1


def test_rest_restarts_the_display(display, mcu):
    display.banner = b"NSPM"
    mcu.send("bkcmd=3", "page home", "rest")
    display.wait_for(lambda d: d.mark() == 3)
    assert display.bkcmd == 2 and display.page is None
    assert mcu.wait_bytes(len(b"\x01\xff\xff\xff" * 2 + b"NSPM"))[-4:] == b"NSPM"


def test_instructions_split_across_writes(display, mcu):
    mcu.send_raw(b"page ho")
    time.sleep(0.05)
    mcu.send_raw(b"me\xff\xff")
    time.sleep(0.05)
    mcu.send_raw(b"\xffdim=3\xff\xff\xff")
    display.wait_for(lambda d: d.mark() == 2)
    assert display.commands == ["page home", "dim=3"]


def test_waits(display, mcu):
    mcu.send("page home", 'home.room.txt="Lounge"')
    display.wait_for_page("home", timeout=2)
    display.wait_for_value("room.txt", "Lounge", timeout=2)
    assert display.wait_for_command(r"room\.txt", timeout=2) == 'home.room.txt="Lounge"'
    with pytest.raises(TimeoutError):
        display.wait_for_page("nowhere", timeout=0.2)
    with pytest.raises(TimeoutError):
        display.wait_for_command("never", timeout=0.2, since=display.mark())


def test_abort_check_ends_a_wait(display):
    def dead():
        raise RuntimeError("mcu crashed")
    display.abort_check = dead
    with pytest.raises(RuntimeError, match="crashed"):
        display.wait_for_page("home", timeout=5)


def test_wait_until_idle(display, mcu):
    mcu.send(*["n%d.val=1" % i for i in range(100)])
    display.wait_for(lambda d: d.mark() > 0)
    display.wait_until_idle(timeout=2)
    assert display.mark() == 100


def test_unicode_text(display, mcu):
    mcu.send('page p', 't0.txt="22°C"')
    display.wait_for_value("p.t0.txt", "22°C", timeout=2)


def test_sending_when_disconnected_raises(display, mcu):
    mcu.sock.shutdown(socket.SHUT_RDWR)
    display.wait_for(lambda d: not d.connected, timeout=2)
    with pytest.raises(NotConnected):
        display.touch(1, True)


def test_replies_keep_their_order(display, mcu):
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = mcu.mark()
    mcu.send(*["get p.n%d.val" % (i % 3) if i % 2 else "n%d.val=%d" % (i, i) for i in range(40)])
    frames = mcu.wait_frames(40, since=mark)
    for i, f in enumerate(frames):
        assert f[:1] == (b"\x71" if i % 2 else OK), i


def test_flow_settings_are_exposed(display):
    display.buffer_capacity = 300
    assert display.flow.capacity == 300
    assert display.overflows == 0 and display.max_buffered == 0 and display.lost == []
    assert protocol.BKCMD_DEFAULT == display.bkcmd


def test_a_device_profile_sets_what_the_display_measures_as():
    import dataclasses
    import socket

    from nextion_tools.emu import NX4832T035, Display, SocketTransport

    other = dataclasses.replace(NX4832T035, model="X", comok=b"comok 1,x", buffer_capacity=512, redraw_s=0.1)
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), device=other, redraw_s=0.2)
    try:
        assert (display.comok, display.buffer_capacity, display.redraw_s) == (b"comok 1,x", 512, 0.2)
        assert display.device is other
    finally:
        display.close()
        b.close()
