"""The TFT's own code running on the display: Program.s, page events, timers and touches."""

import os
import re
import socket
import time

import pytest

from conftest import Mcu
from hmi_builder import build, component, page
from nextion_tools.emu import Display, ManualClock, SocketTransport, UnsupportedCode
from nextion_tools.hmi import Project

OK = b"\x01"
INVALID = b"\x1a"


def area(x, y, w, h, **attrs):
    attrs.update(x=x, y=y, w=w, h=h)
    return attrs


def screen(**attrs):
    attrs.update(x=0, y=0, w=320, h=480)
    return attrs


HMI = Project.parse(build([
    page("page0", events={"load": ["printh 4e 53 50 4d ff ff ff"]}),
    page("home", [
        component("b_both", 98, area(10, 10, 100, 50, sendkey=3, txt="", font=0)),
        component("b_up", 98, area(10, 70, 100, 50, sendkey=1, txt="", font=0),
                  {"down": ["printh 10"], "up": ["printh 11"]}),
        component("h_none", 109, area(10, 130, 100, 50, sendkey=0)),
        component("t_label", 116, area(20, 140, 30, 20, sendkey=0, txt="")),  # drawn over h_none
        component("s_level", 1, area(10, 200, 301, 40, sendkey=1, val=0, minval=0, maxval=100, mode=0),
                  {"slide": ["n_slides.val++"]}),
        component("n_slides", 54, {"val": 0}),
        component("tc0", 5, {"val": 0}, {
            "down": ["printh 20", "va_x.val=tch2", "va_y.val=tch3"],
            "up": ["printh 21", "va_up.val=tch0", "if(tc0.val==2)", "{", "  printh 22", "}"],
        }),
        component("tc1", 5, {"val": 0}, {"up": ["printh 2f"]}),  # ignored: only the lowest id counts
        component("va_x", 52, {"val": 0}),
        component("va_y", 52, {"val": 0}),
        component("va_up", 52, {"val": 5}),
        component("tm0", 51, {"tim": 100, "en": 0}, {"timer": ["n_ticks.val++"]}),
        component("n_ticks", 54, {"val": 0}),
        component("b_go", 98, area(200, 10, 100, 50, sendkey=0, txt="", font=0), {"up": ["page other"]}),
    ], events={"load": ["printh 30"], "loadend": ["printh 31"], "unload": ["printh 32"]}),
    page("other", [
        component("sleep_display", 51, {"tim": 1000, "en": 1}, {"timer": ["printh 92 FF FF FF"]}),
        component("bad", 98, area(0, 0, 320, 480, sendkey=0, txt=""), {"up": ["nosuch.val=1", "printh 40"]}),
    ], events={"load": ["printh 33"]}),
], program=["int sys0=0,gest_type=7", "baud=115200", "page 0"]))

for p in HMI.pages:
    p.components[0].attrs.update(screen())


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def epair(clock):
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=HMI, clock=clock)
    mcu = Mcu(b)
    deadline = time.monotonic() + 2
    while not display.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    yield display, mcu
    display.close()
    b.close()


def frames(mcu, count, since=0):
    return mcu.wait_frames(count, since=since)


def settled(display, mcu):
    """A mark after everything the display has sent so far has arrived."""
    display.wait_until_idle()
    time.sleep(0.05)
    return mcu.mark()


def touch_frames(data):
    return re.findall(rb"\x65...(?=\xff\xff\xff)", data, re.S)


def show(display, mcu, page_name):
    mcu.send("page " + page_name)
    display.wait_for_page(page_name)
    return settled(display, mcu)


def on_home(display, mcu):
    return show(display, mcu, "home")


# ---- Program.s and pages ----------------------------------------------------------------------

def test_boot_runs_program_s(epair):
    display, mcu = epair
    display.boot()
    # Program.s ends with `page 0`, whose load code prints the TFT's banner.
    assert frames(mcu, 1) == [b"NSPM"]
    assert display.globals == {"sys0": 0, "gest_type": 7}
    assert display.value("gest_type") == 7


def test_the_mcu_can_read_a_global(epair):
    display, mcu = epair
    display.boot()
    mark = settled(display, mcu)
    mcu.send("get gest_type")
    assert frames(mcu, 1, mark) == [b"\x71\x07\x00\x00\x00"]


def test_a_page_change_runs_unload_load_and_loadend(epair):
    display, mcu = epair
    mark = on_home(display, mcu)
    mcu.send("page other")
    display.wait_for_page("other")
    assert mcu.wait_bytes(2, since=mark) == b"\x32\x33"  # home's unload, then other's load


def test_load_runs_before_loadend(epair):
    display, mcu = epair
    mark = mcu.mark()
    mcu.send("page home")
    assert mcu.wait_bytes(2, since=mark) == b"\x30\x31"


# ---- timers -------------------------------------------------------------------------------------

def test_a_timer_fires_every_tim_while_enabled(epair, clock):
    display, mcu = epair
    on_home(display, mcu)
    mcu.send("tm0.en=1")
    display.wait_for(lambda d: d.value("home.tm0.en") == 1)
    display.tick()
    for _ in range(3):
        clock.advance(0.1)
        display.tick()
    assert display.value("home.n_ticks.val") == 3
    mcu.send("tm0.en=0")
    display.wait_for(lambda d: d.value("home.tm0.en") == 0)
    clock.advance(1)
    display.tick()
    assert display.value("home.n_ticks.val") == 3


def test_writing_en_restarts_the_countdown(epair, clock):
    display, mcu = epair
    show(display, mcu, "other")
    display.tick()
    clock.advance(0.9)
    display.tick()
    mark = settled(display, mcu)
    mcu.send("sleep_display.en=1")
    display.wait_for_command(r"^sleep_display\.en=1$")
    clock.advance(0.9)
    display.tick()
    assert mcu.quiet(0.1, since=mark) == b""  # 1.8 s since the page loaded, 0.9 s since en=1
    clock.advance(0.2)
    display.tick()
    assert frames(mcu, 1, mark) == [b"\x92"]


def test_only_the_current_pages_timers_run(epair, clock):
    display, mcu = epair
    mcu.send("page other")
    display.wait_for_page("other")
    display.tick()
    mark = mcu.mark()
    mcu.send("page home")
    display.wait_for_page("home")
    clock.advance(5)
    display.tick()
    assert b"\x92" not in mcu.quiet(0.1, since=mark)


def test_expire_timer(epair):
    display, mcu = epair
    mark = show(display, mcu, "other")
    display.expire_timer("sleep_display")
    assert frames(mcu, 1, mark) == [b"\x92"]
    on_home(display, mcu)
    with pytest.raises(RuntimeError, match="not running"):
        display.expire_timer("tm0")


# ---- touches ------------------------------------------------------------------------------------

def test_sendkey_decides_which_edges_are_reported(epair):
    display, mcu = epair
    mark = on_home(display, mcu)
    display.press("b_both", hold=0)
    display.press("b_up", hold=0)
    display.press("h_none", hold=0)
    touches = touch_frames(mcu.quiet(0.1, since=mark))
    assert touches == [b"\x65\x01\x01\x01", b"\x65\x01\x01\x00", b"\x65\x01\x02\x00"]


def test_component_code_runs_before_the_touch_capture(epair):
    display, mcu = epair
    mark = on_home(display, mcu)
    display.press("b_up", hold=0)
    # b_up down (10), tc0 down (20), the release frame, b_up up (11), tc0 up (21), tc0 up's if (22).
    assert mcu.quiet(0.1, since=mark) == b"\x10\x20" + b"\x65\x01\x02\x00\xff\xff\xff" + b"\x11\x21\x22"


def test_the_touch_capture_sees_the_touch_point(epair):
    display, mcu = epair
    on_home(display, mcu)
    display.touch_down(15, 75)
    assert display.value("home.tc0.val") == 2
    assert (display.value("home.va_x.val"), display.value("home.va_y.val")) == (15, 75)
    assert (display.value("tch0"), display.value("tch1")) == (15, 75)
    display.touch_up()
    assert display.value("home.va_up.val") == 0  # tch0 is 0 once the finger lifts
    assert display.value("tch2") == 15


def test_what_is_drawn_last_takes_the_touch(epair):
    display, mcu = epair
    on_home(display, mcu)
    display.touch_down(25, 145)
    display.touch_up()
    assert display.value("home.tc0.val") == 4  # t_label, over h_none
    # point_on finds a part of h_none that the label does not cover.
    x, y = display.point_on("h_none")
    display.touch_down(x, y)
    display.touch_up()
    assert display.value("home.tc0.val") == 3


def test_hidden_components_are_not_touched(epair):
    display, mcu = epair
    on_home(display, mcu)
    mcu.send("vis t_label,0")
    display.wait_for(lambda d: d.value("home.t_label.vis") == 0)
    display.touch_down(25, 145)
    display.touch_up()
    assert display.value("home.tc0.val") == 3
    with pytest.raises(ValueError, match="hidden"):
        display.point_on("t_label")


def test_a_touch_on_nothing_is_the_page(epair):
    display, mcu = epair
    on_home(display, mcu)
    display.touch_down(300, 470)
    display.touch_up()
    assert display.value("home.tc0.val") == 0


def test_slider_follows_the_finger_and_runs_its_slide_code(epair):
    display, mcu = epair
    mark = on_home(display, mcu)
    display.touch_down(10, 220)
    assert display.value("home.s_level.val") == 0
    display.touch_move(160, 220)
    display.touch_move(310, 220)
    display.touch_up()
    assert display.value("home.s_level.val") == 100
    assert display.value("home.n_slides.val") == 2
    assert touch_frames(mcu.quiet(0.1, since=mark)) == [b"\x65\x01\x05\x00"]


def test_slide_to_a_value(epair):
    display, mcu = epair
    on_home(display, mcu)
    assert display.slide("s_level", 73) == 73
    mark = settled(display, mcu)
    mcu.send("get s_level.val")
    assert frames(mcu, 1, mark) == [b"\x71\x49\x00\x00\x00"]


def test_a_release_after_a_page_change_goes_nowhere(epair):
    display, mcu = epair
    on_home(display, mcu)
    display.touch_down(*display.point_on("b_go"))
    mcu.send("page other")
    display.wait_for_page("other")
    display.touch_up()
    assert display.page == "other"


def test_code_can_change_page(epair):
    display, mcu = epair
    on_home(display, mcu)
    display.press("b_go", hold=0)
    assert display.page == "other"


def test_only_the_first_touch_capture_runs(epair):
    display, mcu = epair
    mark = on_home(display, mcu)
    display.press("b_both", hold=0)
    assert b"\x2f" not in mcu.quiet(0.1, since=mark)


def test_drag_and_swipe(epair):
    display, mcu = epair
    on_home(display, mcu)
    display.drag((10, 220), (160, 220), duration=0.02, steps=2, hold=0)
    assert display.value("home.s_level.val") == 50
    display.swipe("left", duration=0.01)
    assert display.value("tch2") == 240 and display.value("tch3") == 240


# ---- errors and overrides --------------------------------------------------------------------

def test_a_failing_statement_reports_0x1a_and_the_code_carries_on(epair):
    display, mcu = epair
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    mark = show(display, mcu, "other")
    display.press("bad", hold=0)
    assert mcu.quiet(0.1, since=mark) == INVALID + b"\xff\xff\xff" + b"\x40"
    assert display.script_errors == [("other.bad.up", "nosuch.val=1", 0x1A)]
    assert ("[other.bad.up] nosuch.val=1", 0x1A) in display.errors


def test_unsupported_code_is_refused_when_the_hmi_is_loaded():
    hmi = Project.parse(build([page("p", [component("b", 98, area(0, 0, 9, 9), {"up": ["while(1==1)", "{", "}"]})])]))
    a, b = socket.socketpair()
    try:
        with pytest.raises(UnsupportedCode, match=r"p\.b\.up line 1"):
            Display(SocketTransport(a), hmi=hmi)
    finally:
        a.close()
        b.close()


def test_an_override_stands_in_for_unsupported_code():
    hmi = Project.parse(build([page("p", [component("b", 98, area(0, 0, 9, 9, sendkey=0), {"up": ["while(1==1)", "{", "}"]})])]))
    calls = []
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=hmi, overrides={"p.b.up": lambda d: calls.append(d.page)})
    try:
        display.press("b", hold=0)
        assert calls == ["p"]
    finally:
        display.close()
        b.close()


def test_scripts_off_sends_bare_frames(clock):
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=HMI, scripts=False)
    mcu = Mcu(b)
    try:
        time.sleep(0.05)
        display.press("h_none", hold=0, page="home")
        assert frames(mcu, 2) == [b"\x65\x01\x03\x01", b"\x65\x01\x03\x00"]
    finally:
        display.close()
        b.close()


# ---- against the real HMI -------------------------------------------------------------------

REAL_HMI = os.environ.get("NEXTION_TOOLS_HMI")
real = pytest.mark.skipif(not REAL_HMI, reason="set NEXTION_TOOLS_HMI to a gui.HMI")


@pytest.fixture
def nspanel():
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=Project.load(REAL_HMI))
    mcu = Mcu(b)
    time.sleep(0.05)
    display.boot()
    yield display, mcu
    display.close()
    b.close()


@real
def test_real_banner(nspanel):
    display, mcu = nspanel
    assert frames(mcu, 1) == [b"NSPM"]


@real
def test_real_screensaver_tap_wakes(nspanel):
    display, mcu = nspanel
    mark = show(display, mcu, "screensaver")
    display.tap(160, 240)
    assert frames(mcu, 1, mark) == [b"\x93"]


@real
def test_real_home_sleep_timer(nspanel):
    display, mcu = nspanel
    mark = show(display, mcu, "home")
    display.expire_timer("sleep_display")
    assert frames(mcu, 1, mark) == [b"\x92"]


@real
def test_real_items_label_tap_is_decoded_by_the_gesture_code(nspanel):
    display, mcu = nspanel
    show(display, mcu, "items4")
    mcu.send("vis i1_label,1")
    display.wait_for(lambda d: d.value("items4.i1_label.vis") == 1)
    mark = settled(display, mcu)
    display.press("i1_label", hold=0.15)
    # The label has no sendkey; the page's gesture code reports it on release, as id 1.
    assert touch_frames(mcu.quiet(0.1, since=mark)) == [b"\x65\x06\x01\x01"]


@real
def test_real_items_button_reports_itself_and_skips_gestures(nspanel):
    display, mcu = nspanel
    show(display, mcu, "items4")
    mcu.send("vis i1_button,1")
    display.wait_for(lambda d: d.value("items4.i1_button.vis") == 1)
    mark = settled(display, mcu)
    display.press("i1_button", hold=0.15)
    # font 11 buttons are excluded from gestures (gest_type -1) and report both edges themselves.
    assert touch_frames(mcu.quiet(0.1, since=mark)) == [b"\x65\x06\x05\x01", b"\x65\x06\x05\x00"]


@real
def test_real_items_swipe_left(nspanel):
    display, mcu = nspanel
    mark = show(display, mcu, "items4")
    display.swipe("left", distance=200, duration=0.2)
    assert frames(mcu, 1, mark) == [b"\x65\x06\x0d\x01"]


@real
def test_real_home_swipe_down_opens_dropdown(nspanel):
    display, mcu = nspanel
    show(display, mcu, "home")
    # The page samples the finger 100 ms after the press, so the drag must be under way by then.
    display.drag((160, 60), (160, 160), duration=0.08, steps=4, hold=0.1)
    display.wait_for_page("dropdown", timeout=2)
