"""The demo project on the emulator: its own code, sendxy and sleep, then with the companion
(demo/companion.py) as its MCU, used as a person would use it."""

import importlib.util
import os
import socket
import struct
import time

import pytest

from conftest import Mcu
from nextion_tools.emu import Display, SocketTransport
from nextion_tools.hmi import Project

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "demo")
HMI = Project.load(os.path.join(DEMO, "demo.HMI"))
STARTUP = b"\x00\x00\x00\xff\xff\xff\x88\xff\xff\xff"


def _load(name):
    spec = importlib.util.spec_from_file_location("demo_" + name, os.path.join(DEMO, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def booted():
    """(display, mcu) after power-on, with the test as the MCU."""
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=HMI)
    mcu = Mcu(b)
    deadline = time.monotonic() + 2
    while not display.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    display.boot()
    yield display, mcu
    display.close()
    b.close()


@pytest.fixture
def demo():
    """(display, companion): the demo as demo/run.py runs it."""
    display, companion = _load("run").start(os.path.join(DEMO, "demo.HMI"))
    yield display, companion
    companion.stop()
    display.close()


def settle(display):
    display.wait_until_idle()
    time.sleep(0.2)


# ---- the display alone ----------------------------------------------------------------------

def test_program_s_runs_at_power_on_and_skips_the_dummy_page(booted):
    display, mcu = booted
    assert mcu.wait_bytes(len(STARTUP)) == STARTUP
    assert display.page == "menu" and display.page_history == ["menu"]
    assert display.system["thup"] == 1


def test_up_and_down_run_the_hmis_own_code(booted):
    display, mcu = booted
    mcu.send("page dial")
    display.wait_for_page("dial")
    assert display.value("dial.n_number.val") == 50  # loadend copied the bar
    for expected in (60, 70, 80, 90, 100, 100):
        display.press("b_up", hold=0.02)
        assert display.value("dial.j0.val") == expected == display.value("dial.n_number.val")
    for _ in range(12):
        display.press("b_down", hold=0.02)
    assert display.value("dial.j0.val") == 0
    settle(display)
    mark = mcu.mark()
    mcu.send("get j0.val")
    assert mcu.wait_frames(1, since=mark) == [b"\x71\x00\x00\x00\x00"]
    assert not display.script_errors


def test_each_button_reports_its_release(booted):
    display, mcu = booted
    mcu.wait_bytes(len(STARTUP))
    mark = mcu.mark()
    display.press("b_dial", hold=0.02)
    assert mcu.wait_frames(1, since=mark) == [bytes([0x65, 1, HMI.page("menu").component("b_dial").id, 0])]
    assert display.page == "menu"  # going anywhere is the MCU's job


def test_sendxy_reports_where_presses_and_releases_are(booted):
    display, mcu = booted
    mcu.wait_bytes(len(STARTUP))
    mcu.send("sendxy=1")
    settle(display)
    mark = mcu.mark()
    display.touch_down(5, 300)
    display.touch_move(9, 310)
    display.touch_up()
    assert mcu.wait_frames(2, since=mark) == [b"\x67" + struct.pack(">HHB", 5, 300, 1),
                                              b"\x67" + struct.pack(">HHB", 9, 310, 0)]
    mcu.send("sendxy=0")
    settle(display)
    mark = mcu.mark()
    display.tap(5, 300, hold=0.02)
    assert mcu.quiet(0.2, since=mark) == b""


def test_sendme_answers_the_page_id(booted):
    display, mcu = booted
    mcu.wait_bytes(len(STARTUP))
    mark = mcu.mark()
    mcu.send("sendme")
    assert mcu.wait_frames(1, since=mark) == [b"\x66\x01"]


def test_asleep_a_touch_only_wakes_the_display(booted):
    display, mcu = booted
    mcu.wait_bytes(len(STARTUP))
    mcu.send("sleep=1")
    settle(display)
    mark = mcu.mark()
    display.press("b_dial", hold=0.02)  # thup=1, from Program.s
    assert mcu.wait_frames(1, since=mark) == [b"\x87"]
    assert not display.scene()["asleep"] and display.page == "menu"
    mark = mcu.mark()
    display.press("b_dial", hold=0.02)  # awake now: an ordinary press
    assert mcu.wait_frames(1, since=mark)[0][0] == 0x65


def test_asleep_without_thup_a_touch_does_nothing(booted):
    display, mcu = booted
    mcu.wait_bytes(len(STARTUP))
    mcu.send("thup=0", "sleep=1")
    settle(display)
    mark = mcu.mark()
    display.press("b_dial", hold=0.02)
    assert mcu.quiet(0.2, since=mark) == b"" and display.scene()["asleep"]
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    assert display.snapshot().max() == 0  # dark


# ---- with the companion -----------------------------------------------------------------------

def test_the_companion_knows_the_page_and_follows_the_menu(demo):
    display, companion = demo
    display.wait_for(lambda d: companion.page == "menu", timeout=5, what="sendme answered")
    for button, page in (("b_dial", "dial"), ("b_back", "menu"), ("b_pong", "pong"), ("b_back", "menu"),
                         ("b_paint", "paint"), ("b_back", "menu")):
        display.press(button, hold=0.02)
        display.wait_for_page(page, timeout=5)
    assert display.system.get("sendxy") == 0  # leaving paint turned it off


def test_the_thermostat_bar_turns_red_near_the_top(demo):
    display, companion = demo
    display.press("b_dial", hold=0.02)
    display.wait_for_page("dial", timeout=5)
    green = HMI.page("dial").component("j0").get("pco")
    for _ in range(3):
        display.press("b_up", hold=0.02)
    display.wait_for(lambda d: d.get("dial.j0.pco") == 63488, timeout=5, what="bar red at 80")
    display.press("b_down", hold=0.02)
    display.wait_for(lambda d: d.get("dial.j0.pco") == green, timeout=5, what="bar back to its colour")


def test_paint_draws_along_a_drag_and_not_on_the_back_button(demo):
    display, companion = demo
    display.press("b_paint", hold=0.02)
    display.wait_for_page("paint", timeout=5)
    display.wait_for(lambda d: d.system.get("sendxy") == 1, timeout=5, what="sendxy on")
    display.drag((100, 150), (300, 250), duration=0.6, steps=12)
    display.wait_for(lambda d: any(i.startswith("line ") for i in d.drawn), timeout=5, what="a line drawn")
    assert display.drawn[0] == "cirs 100,150,3,0"
    display.press("b_back", hold=0.02)
    display.wait_for_page("menu", timeout=5)
    x, y, w, h = HMI.page("paint").component("b_back").rect
    dots = [tuple(int(v) for v in c.split()[1].split(",")[:2]) for c in display.commands if c.startswith("cirs ")]
    assert dots and not [(dx, dy) for dx, dy in dots if x <= dx < x + w and y <= dy < y + h]


def test_pong_moves_the_ball_and_the_paddle_follows_the_finger(demo):
    display, companion = demo
    display.press("b_pong", hold=0.02)
    display.wait_for_page("pong", timeout=5)
    display.wait_for(lambda d: companion.game is not None, timeout=5, what="game started")
    game = companion.game
    start = list(game.ball)
    display.wait_for(lambda d: game.ball != start, timeout=5, what="the ball moving")
    display.touch_down(470, 300)
    try:
        display.wait_for(lambda d: game.paddle_x == 480 - game.PADDLE_W, timeout=5, what="paddle at the right")
        display.touch_move(50, 300)
        display.wait_for(lambda d: game.paddle_x == 5, timeout=5, what="paddle following left")
    finally:
        display.touch_up()


def test_sleep_from_the_menu_and_wake_with_a_touch(demo):
    display, companion = demo
    display.press("b_sleep", hold=0.02)
    display.wait_for(lambda d: d.scene()["asleep"], timeout=5, what="asleep")
    display.press("b_pong", hold=0.02)
    display.wait_for(lambda d: not d.scene()["asleep"], timeout=5, what="awake")
    time.sleep(0.2)
    assert display.page == "menu"  # the waking touch went nowhere
    assert (0x87, b"") in companion.received


def test_paint_ignores_a_sample_of_0_0_which_is_what_the_display_reads_with_no_finger(demo):
    display, companion = demo
    display.press("b_paint", hold=0.02)
    display.wait_for(lambda d: d.page == "paint" and d.system.get("sendxy") == 1, timeout=5, what="paint")
    display.touch_down(200, 200)
    try:
        display.touch_move(0, 0)
        time.sleep(0.3)
        display.touch_move(220, 240)
        display.wait_for(lambda d: "line 200,200,220,240,0" in d.drawn, timeout=5, what="a line from the press")
    finally:
        display.touch_up()
    points = []
    for instruction in display.drawn:
        keyword, args = instruction.split(" ", 1)
        v = [int(a) for a in args.split(",")]
        points += [(v[0], v[1]), (v[2], v[3])] if keyword == "line" else [(v[0], v[1])]
    assert (0, 0) not in points
