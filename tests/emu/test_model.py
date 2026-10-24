"""Display with an HMI: which names exist where, what they hold, and what a page load does."""

import os
import socket
import struct
import time

import pytest

from conftest import Mcu
from hmi_builder import build, component, page
from nextion_tools.emu import Display, SocketTransport
from nextion_tools.hmi import Project

OK = b"\x01"
INVALID = b"\x1a"

HMI = Project.parse(build([
    page("page0"),
    page("home", [
        component("room", 98, {"txt": "Room", "vscope": 1, "sendkey": 1}),  # global
        component("n_ceiling", 54, {"val": 50, "vscope": 0}),  # local
        component("s_brightness", 1, {"val": 30, "vscope": 1, "sendkey": 1}),
        component("b_scenes", 98, {"txt": "", "pic": 59, "vscope": 0, "sendkey": 1}),
        component("t_title", 116, {"txt": "Home", "vscope": 0}),
    ]),
    page("screensaver", [
        component("forDay1", 116, {"txt": "", "vscope": 1}),
        component("curTime", 116, {"txt": "", "vscope": 0}),
    ]),
]))


@pytest.fixture
def hpair():
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=HMI)
    mcu = Mcu(b)
    deadline = time.monotonic() + 2
    while not display.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    mcu.send("bkcmd=3")
    mcu.wait_frames(1)
    yield display, mcu
    display.close()
    b.close()


def replies(mcu, *commands, count=None):
    mark = mcu.mark()
    mcu.send(*commands)
    return mcu.wait_frames(count or len(commands), since=mark)


def test_the_display_starts_on_page_0(hpair):
    display, mcu = hpair
    assert display.page == "page0"


def test_pages_by_name_and_by_id(hpair):
    display, mcu = hpair
    assert replies(mcu, "page home", "page 2", "page nowhere", "page 7") == [OK, OK, INVALID, INVALID]
    assert display.page_history == ["home", "screensaver"]


def test_unqualified_names_are_on_the_current_page(hpair):
    display, mcu = hpair
    assert replies(mcu, "page home", 'room.txt="Kitchen"', 'curTime.txt="12:00"') == [OK, OK, INVALID]
    assert display.components == {"home.room.txt": "Kitchen"}


def test_the_screensaver_forecast_bug(hpair):
    """The firmware writes forDay1 without its page: fine on the screensaver, 0x1A elsewhere."""
    display, mcu = hpair
    assert replies(mcu, "page home", 'forDay1.txt="Mon"', 'screensaver.forDay1.txt="Mon"') == [OK, INVALID, OK]
    assert replies(mcu, "page screensaver", 'forDay1.txt="Tue"') == [OK, OK]
    assert display.errors == [('forDay1.txt="Mon"', 0x1A)]


def test_a_local_component_of_another_page_does_not_exist(hpair):
    display, mcu = hpair
    assert replies(mcu, "page screensaver", "home.n_ceiling.val=3", "home.room.txt=\"x\"") == [OK, INVALID, OK]


def test_the_attribute_must_exist(hpair):
    display, mcu = hpair
    assert replies(mcu, "page home", "room.pic=3", "room.nonsense=1", "b_scenes.pic=60") == [OK, INVALID, INVALID, OK]


def test_system_variables(hpair):
    display, mcu = hpair
    assert replies(mcu, "dim=50", "sys0=3", "thsp=30", "va0=1", "tm0=1") == [OK, OK, OK, INVALID, INVALID]


def test_vis_needs_the_component(hpair):
    display, mcu = hpair
    assert replies(mcu, "page home", "vis b_scenes,0", "vis nosuch,0") == [OK, OK, INVALID]
    assert display.components["home.b_scenes.vis"] == 0


def test_get_answers_the_hmi_value_until_one_is_assigned(hpair):
    display, mcu = hpair
    replies(mcu, "page home")
    mark = mcu.mark()
    mcu.send("get s_brightness.val", "get t_title.txt")
    assert mcu.wait_bytes(8 + 8, since=mark) == b"\x71" + struct.pack("<i", 30) + b"\xff\xff\xff" + b"\x70Home\xff\xff\xff"
    replies(mcu, "s_brightness.val=80")
    mark = mcu.mark()
    mcu.send("get s_brightness.val")
    assert mcu.wait_bytes(8, since=mark)[1:5] == struct.pack("<i", 80)
    assert display.value("home.s_brightness.val") == 80
    assert display.value("home.t_title.txt") == "Home"
    assert display.get("home.t_title.txt") is None  # get() is what the MCU assigned


def test_get_dp_is_the_current_page(hpair):
    display, mcu = hpair
    replies(mcu, "page screensaver")
    mark = mcu.mark()
    mcu.send("get dp")
    assert mcu.wait_bytes(8, since=mark)[1:5] == struct.pack("<i", 2)


def test_get_of_a_name_that_does_not_exist(hpair):
    display, mcu = hpair
    assert replies(mcu, "get nosuch.val") == [INVALID]


def test_a_page_load_resets_its_local_components_but_not_globals(hpair):
    display, mcu = hpair
    replies(mcu, "page home", "n_ceiling.val=7", 'room.txt="Kitchen"', 'b_scenes.txt="x"', "page screensaver", "page home")
    assert display.value("home.n_ceiling.val") == 50  # back to the HMI's value
    assert "home.b_scenes.txt" not in display.components
    assert display.components["home.room.txt"] == "Kitchen"  # global: kept


def test_compound_assignment_starts_from_the_hmi_value(hpair):
    display, mcu = hpair
    replies(mcu, "page home", "n_ceiling.val+=5", "n_ceiling.val++")
    assert display.value("home.n_ceiling.val") == 56


def test_literals_must_suit_the_attribute(hpair):
    display, mcu = hpair
    assert replies(mcu, "page home", 'n_ceiling.val="5"', "t_title.txt=5", 'room.txt="5"', "n_ceiling.val=5") == \
        [OK, INVALID, INVALID, OK, OK]


def test_touch_events_by_name_carry_the_hmi_ids(hpair):
    display, mcu = hpair
    replies(mcu, "page home")
    mark = mcu.mark()
    display.send_touch_event("b_scenes", True)
    display.send_touch_event("forDay1", True, page="screensaver")
    assert mcu.wait_frames(2, since=mark) == [b"\x65\x01\x04\x01", b"\x65\x02\x01\x01"]


def test_touch_by_an_unknown_name(hpair):
    display, mcu = hpair
    replies(mcu, "page home")
    with pytest.raises(KeyError):
        display.touch("nosuch", True)
    with pytest.raises(KeyError):
        display.send_touch_event("nosuch", True, page="home")
    with pytest.raises(KeyError):
        display.page_id("nowhere")


def test_power_cycle_goes_back_to_page_0(hpair):
    display, mcu = hpair
    replies(mcu, "page home")
    display.power_cycle()
    assert display.page == "page0"


def test_a_resolver_overrides_the_hmi(hpair):
    display, mcu = hpair
    display.resolver = lambda key: True
    assert replies(mcu, "nosuch=1") == [OK]


def test_without_an_hmi_touch_by_name_needs_one(display):
    with pytest.raises(KeyError, match="HMI"):
        display.touch("b_scenes", True)


# ---- against the real HMI -------------------------------------------------------------------

REAL_HMI = os.environ.get("NEXTION_TOOLS_HMI")


@pytest.mark.skipif(not REAL_HMI, reason="set NEXTION_TOOLS_HMI to a gui.HMI")
def test_real_hmi_forecast_labels():
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=Project.load(REAL_HMI))
    mcu = Mcu(b)
    try:
        time.sleep(0.05)
        mcu.send("bkcmd=3")
        mcu.wait_frames(1)
        assert replies(mcu, "page home", 'forDay1.txt="Mon"', 'screensaver.forDay1.txt="Mon"',
                       'room.txt="Kitchen"', "page screensaver", 'forDay1.txt="Mon"') == [OK, INVALID, OK, OK, OK, OK]
    finally:
        display.close()
        b.close()
