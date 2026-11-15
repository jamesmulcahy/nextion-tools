"""nspanel-terminal's protocol 8: the receiver against golden vectors from the real sender and
decoder (terminal_vectors/, made by its generate.sh), and the display running a session."""

import gzip
import json
import os
import random
import socket
import struct
import time

import pytest

from conftest import Mcu
from nextion_tools.emu import COMOK, Display, ManualClock, SocketTransport
from nextion_tools.emu import terminal as t
from nextion_tools.emu.protocol import TERMINATOR

VECTORS = os.path.join(os.path.dirname(__file__), "terminal_vectors")
MANIFEST = json.load(open(os.path.join(VECTORS, "manifest.json")))
CASES = {c["name"]: c for c in MANIFEST["cases"]}


def vector(name):
    case = CASES[name]
    with open(os.path.join(VECTORS, name + ".stream"), "rb") as f:
        stream = f.read()
    with gzip.open(os.path.join(VECTORS, name + ".fb.gz")) as f:
        fb = f.read()
    return case, stream, fb


def summary(reports):
    return [[r.status, r.consumed, r.seq, r.rectangles] for r in reports]


def replay(rx, stream, chunks=None, seed=0):
    """Feed `stream` as a sender would: never more than 512 bytes beyond the last report it has
    seen, in pieces of `chunks` bytes (random ones by default). Returns the reports."""
    rng = random.Random(seed)
    reports = []
    pos, credit = 0, 0
    while pos < len(stream) and rx.active:
        limit = min(len(stream), credit + t.WINDOW)
        if pos >= limit:
            raise AssertionError("no credit at %d (consumed %d)" % (pos, credit))
        n = chunks or rng.randint(1, 600)
        piece = stream[pos:min(limit, pos + n)]
        pos += len(piece)
        for data in rx.feed(piece, 0.0):
            reports.append(t.parse_report(data))
            credit = reports[-1].consumed
    return reports


# ---- golden vectors -----------------------------------------------------------------------------

def test_the_vectors_cover_both_profiles_and_every_event():
    profiles = {c["profile"] for c in CASES.values()}
    statuses = {r[0] for c in CASES.values() for r in c["reports"]}
    assert profiles == {"us", "eu"}
    assert statuses == {t.READY_BRIGHTNESS, t.PROGRESS, t.DONE, t.BYE, t.ERROR, t.SYNCED}


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("chunks", [1, 7, None, t.WINDOW], ids=["bytes", "7", "random", "window"])
def test_golden_vector(name, chunks):
    """Pixel for pixel what the real decoder drew, and report for report what it sent."""
    case, stream, fb = vector(name)
    rx = t.Receiver(case["session"], t.profile(case["profile"]))
    assert (rx.fb.width, rx.fb.height) == (case["width"], case["height"])
    reports = [t.parse_report(rx.ready())] + replay(rx, stream, chunks, seed=len(name))
    assert summary(reports) == case["reports"]
    assert rx.fb.rgb565() == fb
    assert rx.stopped == "exit"
    assert rx.consumed == len(stream) == case["stream_bytes"]


def test_a_vector_in_one_piece_breaks_the_credit_window():
    case, stream, fb = vector("us_gradient")
    rx = t.Receiver(case["session"], t.TERMINAL_US)
    rx.ready()
    reports = [t.parse_report(r) for r in rx.feed(stream, 0.0)]
    assert reports[-1].status == t.ERROR and reports[-1].flags == t.FLAG_CREDIT
    assert reports[-1].consumed == t.WINDOW  # byte 513 is the violation
    assert rx.stopped == "credit"
    # Without the check, the same bytes draw the same picture.
    rx = t.Receiver(case["session"], t.TERMINAL_US, strict_credits=False)
    rx.feed(stream, 0.0)
    assert rx.fb.rgb565() == fb and rx.stopped == "exit"


def test_resync_vector():
    case, stream, fb = vector("us_resync")
    rx = t.Receiver(case["session"], t.TERMINAL_US)
    rx.ready()
    replay(rx, stream)
    assert rx.faults == ["header CRC"]
    assert rx.records["SYNC"] == 1 and rx.records["RECT"] == 1  # the faulted RECT is not counted
    assert rx.fb.rgb565() == fb


def test_eu_receiver_reaches_column_453_and_no_further():
    rx = t.Receiver(9, t.TERMINAL_EU)
    rx.ready()
    out = rx.feed(t.encode_record(9, 0, t.RECT, 453, 319, 1, 1), 0.0)
    out += rx.feed(t.encode_record(9, 1, t.DATA, body=b"\x00\xf8\x00"), 0.0)
    out += rx.feed(t.encode_record(9, 2, t.END, 1), 0.0)
    assert [t.parse_report(r).status for r in out] == [t.DONE]
    assert rx.fb.pixel(453, 319) == 0xF800
    out = rx.feed(t.encode_record(9, 3, t.RECT, 454, 0, 1, 1), 0.0)
    assert [t.parse_report(r).status for r in out] == [t.ERROR]
    assert rx.faults == ["RECT 454,0 1x1"]


# ---- the decoder's rules -------------------------------------------------------------------------

def statuses(out):
    return [t.parse_report(r).status_name for r in out]


@pytest.fixture
def rx():
    r = t.Receiver(0x77, t.TERMINAL_US)
    r.ready()
    return r


def test_cobs_and_crc_match_the_reference():
    # The record nspanel-terminal's protocol_reference.h builds for RECT 1,2 3x4, session 0x77, seq 5.
    raw = t.header(0x77, 5, t.RECT, 1, 2, 3, 4)
    assert raw[:18] == b"RS\x08\x01\x77\x00\x05\x00\x00\x00\x01\x00\x02\x00\x03\x00\x04\x00"
    assert struct.unpack("<H", raw[18:])[0] == t.crc16(raw[:18])
    assert t.crc16(b"123456789") == 0x29B1  # CRC-16/CCITT-FALSE
    assert t.cobs_encode(b"\x11\x00\x00\x22") == b"\x02\x11\x01\x02\x22\x00"
    assert t.cobs_encode(bytes(range(1, 256))) == b"\xff" + bytes(range(1, 255)) + b"\x02\xff\x00"


def test_a_rectangle_in_several_data_records(rx):
    pixels = [(i * 37) & 0xFFFF for i in range(20 * 30)]
    bodies = t.packbits_encode(pixels, payload=100)
    assert len(bodies) > 5
    out = rx.feed(t.encode_record(0x77, 0, t.RECT, 300, 450, 20, 30), 0.0)
    for i, body in enumerate(bodies):
        out += rx.feed(t.encode_record(0x77, 1 + i, t.DATA, body=body), 0.0)
    out += rx.feed(t.encode_record(0x77, 1 + len(bodies), t.END, 1), 0.0)
    assert statuses(out)[-1] == "DONE" and "ERROR" not in statuses(out)
    assert [rx.fb.pixel(300 + i % 20, 450 + i // 20) for i in range(600)] == pixels


@pytest.mark.parametrize("record, fault", [
    (t.encode_record(0x77, 3, t.PING), "sequence 3, expected 0"),
    (t.encode_record(0x78, 0, t.PING), "session 120, not 119"),
    (t.encode_record(0x77, 0, t.PING, 1), "PING with geometry"),
    (t.encode_record(0x77, 0, t.END), "END with no rectangle"),
    (t.encode_record(0x77, 0, t.DATA, body=b"\x00\x00\x01"), "DATA with no rectangle"),
    (t.encode_record(0x77, 0, t.RECT, 0, 0, 0, 5), "RECT 0,0 0x5"),
    (t.encode_record(0x77, 0, t.RECT, 0, 400, 10, 81), "RECT 0,400 10x81"),
    (t.encode_record(0x77, 0, 9), "unknown kind 9"),
    (t.encode_record(0x77, 0, t.BRIGHTNESS, 101), "BRIGHTNESS 101"),
    (t.cobs_encode(t.header(0x77, 0, t.PING)[:18] + b"\x00\x00"), "header CRC"),
    (b"\x05RS\x08\x04\x00", "short record (4 bytes)"),
])
def test_bad_records_fault_once(rx, record, fault):
    assert statuses(rx.feed(record, 0.0)) == ["ERROR"]
    assert rx.faults == [fault]
    # Faulted: nothing else is taken, nor reported, until a SYNC (whatever its sequence).
    assert statuses(rx.feed(t.encode_record(0x77, 0, t.PING), 0.0)) == []
    out = rx.feed(b"\x00" + t.encode_record(0x77, 500, t.SYNC), 0.0)
    assert statuses(out) == ["SYNCED"] and t.parse_report(out[0]).seq == 501
    assert statuses(rx.feed(t.encode_record(0x77, 501, t.PING), 0.0)) == []
    assert rx.faults == [fault, "faulted: PING before SYNC"]


def test_packbits_errors(rx):
    rx.feed(t.encode_record(0x77, 0, t.RECT, 0, 0, 2, 1), 0.0)
    # A repeat of 3 pixels in a 2-pixel rectangle: nothing drawn.
    assert statuses(rx.feed(t.encode_record(0x77, 1, t.DATA, body=b"\x82\xff\xff"), 0.0)) == ["ERROR"]
    assert rx.faults == ["run of 3 with 2 pixels due"] and rx.fb.pixel(0, 0) == 0
    rx.feed(t.encode_record(0x77, 9, t.SYNC), 0.0)
    rx.feed(t.encode_record(0x77, 10, t.RECT, 0, 0, 2, 1), 0.0)
    # A literal of 2 with only one pixel: the one is drawn, then the record faults.
    assert statuses(rx.feed(t.encode_record(0x77, 11, t.DATA, body=b"\x01\x12\x34"), 0.0)) == ["ERROR"]
    assert rx.faults[-1] == "incomplete literal" and rx.fb.pixel(0, 0) == 0x1234
    rx.feed(t.encode_record(0x77, 20, t.SYNC), 0.0)
    rx.feed(t.encode_record(0x77, 21, t.RECT, 0, 0, 2, 1), 0.0)
    rx.feed(t.encode_record(0x77, 22, t.DATA, body=b"\x00\xab\xcd"), 0.0)
    # END with a pixel still due.
    assert statuses(rx.feed(t.encode_record(0x77, 23, t.END, 1), 0.0)) == ["ERROR"]
    assert rx.faults[-1] == "END with 2 pixel bytes due"


def test_progress_every_256_bytes_and_events(rx):
    pings = b"".join(t.encode_record(0x77, i, t.PING) for i in range(30))
    replay(rx, pings, chunks=50)
    reports = rx.reports[1:]
    assert [r.status for r in reports] == [t.PROGRESS] * (len(pings) // 256)
    assert [r.consumed for r in reports] == [256 * (i + 1) for i in range(len(reports))]
    assert all(r.window == 512 and r.high <= 512 and r.flags == 0 for r in reports)


def test_brightness(rx):
    out = rx.feed(t.encode_record(0x77, 0, t.BRIGHTNESS, 40), 0.0)
    assert statuses(out) == ["BRIGHTNESS_DONE"] and rx.brightness == 40
    rx.feed(t.encode_record(0x77, 1, t.RECT, 0, 0, 1, 1), 0.0)
    assert statuses(rx.feed(t.encode_record(0x77, 2, t.BRIGHTNESS, 50), 0.0)) == ["ERROR"]  # not mid-rectangle


def test_watchdog():
    rx = t.Receiver(5, t.TERMINAL_US, now=100.0)
    rx.ready()
    assert rx.check(102.9) == []
    rx.feed(t.encode_record(5, 0, t.PING), 102.0)
    assert rx.check(104.9) == []
    out = rx.check(105.0)
    report = t.parse_report(out[0])
    assert report.status == t.ERROR and report.flags == t.FLAG_TIMEOUT and rx.stopped == "timeout"
    assert rx.feed(t.encode_record(5, 1, t.PING), 106.0) == []


def test_touch_reports():
    rx = t.Receiver(5, t.TERMINAL_EU_ON_US)
    rx.ready()
    down = t.parse_report(rx.touch(True, 100, 50, 0.0)[0])
    assert (down.status, down.fresh, down.mask, down.x, down.y) == (t.PROGRESS, 1, 1, 74, 50)
    edge = t.parse_report(rx.touch(True, 3, 319, 0.0)[0])
    assert (edge.x, edge.y) == (-23, 319)  # signed, as the touch controller's frame starts above
    up = t.parse_report(rx.touch(False, 0, 0, 0.0)[0])
    assert (up.fresh, up.mask, up.x, up.y) == (1, 0, -23, 319)
    # Other reports carry the state, not a new sample.
    later = t.parse_report(rx.feed(t.encode_record(5, 0, t.BRIGHTNESS, 1), 0.0)[0])
    assert (later.fresh, later.mask) == (0, 0)
    quiet = t.Receiver(5, t.TERMINAL_US, touch_enabled=False)
    assert quiet.touch(True, 1, 1, 0.0) == []


def test_firmware_reply_parser_rules():
    """What TerminalProtocol.hpp's reply() checks: magic, version, session, window 512, CRC,
    status range, high-water <= 512."""
    rx = t.Receiver(0xABCD, t.TERMINAL_US)
    data = rx.ready()
    assert len(data) == 32 and data[:3] == b"SR\x08"
    r = t.parse_report(data)
    assert (r.session, r.window, r.status, r.seq, r.consumed, r.rectangles) == (0xABCD, 512, t.READY_BRIGHTNESS, 0, 0, 0)
    with pytest.raises(ValueError):
        t.parse_report(data[:-1] + bytes([data[-1] ^ 1]))


def test_terminal_comok():
    assert t.terminal_comok(COMOK).split(b",")[2:5] == [b"NX4832F035_011C", b"94", b"61744"]


# ---- the display ---------------------------------------------------------------------------------

class TerminalMcu:
    """The firmware's side of a session, over a display's UART."""

    def __init__(self, mcu: Mcu):
        self.mcu = mcu
        self.start = 0
        self.session = 0

    def enter(self, session=0x4242, baud=4000000, touch=1, timeout=5):
        self.mcu.send("bkcmd=0", "sys0=%d" % baud, "sys1=%d" % touch, "sys2=%d" % session)
        self.start = self.mcu.mark()
        self.session = session
        self.mcu.send("touch_j")
        ready = self.reports(1, timeout=timeout)[0]
        assert ready.status == t.READY_BRIGHTNESS and ready.session == session
        return ready

    def reports(self, count=None, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            with self.mcu._cond:
                data = bytes(self.mcu.received[self.start:])
            reports, _ = t.split_reports(data)
            if count is None or len(reports) >= count:
                return reports
            if time.monotonic() > deadline:
                raise TimeoutError("wanted %d reports, got %d" % (count, len(reports)))
            time.sleep(0.005)

    def samples(self, until, timeout=5):
        """The touch samples reported, once `until(samples)` holds."""
        deadline = time.monotonic() + timeout
        while True:
            samples = [r for r in self.reports() if r.fresh]
            if until(samples):
                return samples
            if time.monotonic() > deadline:
                raise TimeoutError("touch samples: %r" % samples)
            time.sleep(0.005)

    def stream(self, data, timeout=10):
        """Send within the credit window, as StreamEngine does."""
        pos = 0
        deadline = time.monotonic() + timeout
        while pos < len(data):
            reports = self.reports()
            credit = reports[-1].consumed if reports else 0
            limit = min(len(data), credit + t.WINDOW)
            if limit > pos:
                self.mcu.send_raw(data[pos:limit])
                pos = limit
            elif time.monotonic() > deadline:
                raise TimeoutError("no credit at %d" % pos)
            else:
                time.sleep(0.002)


@pytest.fixture
def terminal_pair(request):
    """(display, TerminalMcu) for a display running a terminal TFT (param: the profile)."""
    profile = getattr(request, "param", "us")
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), terminal=profile)
    mcu = Mcu(b)
    deadline = time.monotonic() + 2
    while not display.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    yield display, TerminalMcu(mcu)
    display.close()
    b.close()


def test_connect_reports_the_terminal_native_version(terminal_pair):
    display, tm = terminal_pair
    tm.mcu.send("connect")
    reply = tm.mcu.wait_frames(1)[0]
    assert reply.split(b",")[2:5] == [b"NX4832F035_011C", b"94", b"61744"]


@pytest.mark.parametrize("name", ["us_burst", "us_resync"])
def test_a_session_end_to_end(terminal_pair, name):
    display, tm = terminal_pair
    case, stream, fb = vector(name)
    tm.enter(session=case["session"])
    assert display.terminal_active and display.showing_terminal
    tm.stream(stream)
    assert display.wait_for_terminal_end() == "exit"
    reports = tm.reports(len(case["reports"]))
    assert summary(reports) == case["reports"]
    assert display.terminal_rgb565() == fb
    # Back in the stock runtime: the return frame, then instructions again.
    end = tm.start + 32 * len(reports)
    assert tm.mcu.wait_bytes(4, since=end) == t.RETURN_FRAME
    mark = tm.mcu.mark()
    tm.mcu.send("connect")
    assert tm.mcu.wait_frames(1, since=mark)[0].startswith(b"comok")
    assert display.commands[-1] == "connect"


@pytest.mark.parametrize("terminal_pair", ["eu"], indirect=True)
def test_an_eu_session(terminal_pair):
    display, tm = terminal_pair
    case, stream, fb = vector("eu_right_edge")
    tm.enter(session=case["session"])
    tm.stream(stream)
    display.wait_for_terminal_end()
    assert display.terminal_rgb565() == fb and display.size == (454, 320)


def test_records_right_after_touch_j(terminal_pair):
    """touch_j and the first records in one write: the records still reach the receiver."""
    display, tm = terminal_pair
    tm.mcu.send("sys0=4000000", "sys1=0", "sys2=3")
    tm.start = tm.mcu.mark()
    tm.mcu.send_raw(b"touch_j" + TERMINATOR + t.encode_record(3, 0, t.BRIGHTNESS, 30))
    reports = tm.reports(2)
    assert [r.status for r in reports] == [t.READY_BRIGHTNESS, t.BRIGHTNESS_DONE]
    assert display.terminal.brightness == 30 and display.system["dim"] == 30


def test_flow_model_session(terminal_pair):
    display, tm = terminal_pair
    display.flow_model = True
    case, stream, fb = vector("us_runs")
    tm.enter(session=case["session"])
    tm.stream(stream)
    display.wait_for_terminal_end()
    assert display.terminal_rgb565() == fb


@pytest.mark.parametrize("sys0, sys1, sys2", [(115200, 1, 5), (4000000, 2, 5), (4000000, 1, 65536)])
def test_touch_j_with_bad_arguments_stays_in_command_mode(terminal_pair, sys0, sys1, sys2):
    display, tm = terminal_pair
    tm.mcu.send("sys0=%d" % sys0, "sys1=%d" % sys1, "sys2=%d" % sys2)
    mark = tm.mcu.mark()
    tm.mcu.send("touch_j")
    assert tm.mcu.wait_bytes(4, since=mark) == t.RETURN_FRAME
    assert not display.terminal_active and display.terminal is None


def test_touch_j_on_a_stock_tft_is_just_an_instruction(pair):
    display, mcu = pair
    mcu.send("sys0=4000000", "sys1=1", "sys2=5", "touch_j")
    display.wait_for(lambda d: d.mark() == 4)
    assert display.terminal is None and mcu.quiet(0.2) == b""


def test_credit_violation_ends_the_session(terminal_pair):
    display, tm = terminal_pair
    tm.enter()
    pings = b"".join(t.encode_record(tm.session, i, t.PING) for i in range(30))
    tm.mcu.send_raw(pings)  # 660 bytes with no credit beyond the first 512
    assert display.wait_for_terminal_end() == "credit"
    reports = tm.reports(4)  # READY, PROGRESS at 256 and 512, then the violation
    assert [r.status for r in reports] == [t.READY_BRIGHTNESS, t.PROGRESS, t.PROGRESS, t.ERROR]
    assert reports[-1].flags == t.FLAG_CREDIT and reports[-1].consumed == 512


def test_watchdog_through_the_display():
    clock = ManualClock()
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), terminal="us", clock=clock)
    try:
        tm = TerminalMcu(Mcu(b))
        time.sleep(0.05)
        tm.mcu.send("sys0=4000000", "sys1=1", "sys2=9")
        tm.start = tm.mcu.mark()
        tm.mcu.send("touch_j")
        display.wait_for(lambda d: d.terminal_active)
        clock.advance(0.1)  # the READY delay, on this clock
        tm.reports(1)
        clock.advance(2.5)
        display.tick()
        assert display.terminal_active
        clock.advance(0.5)
        display.tick()
        assert not display.terminal_active and display.terminal.stopped == "timeout"
        assert tm.reports(2)[-1].flags == t.FLAG_TIMEOUT
    finally:
        display.close()
        b.close()


def test_touches_during_a_session(terminal_pair):
    display, tm = terminal_pair
    tm.enter()
    display.touch_down(10, 20)
    display.touch_move(12, 25)
    display.touch_up()
    display.tap(300, 470, hold=0.01)
    samples = tm.samples(lambda s: len(s) >= 5)
    assert [(r.mask, r.x, r.y) for r in samples] == [(1, 10, 20), (1, 12, 25), (0, 12, 25), (1, 300, 470), (0, 300, 470)]
    with pytest.raises(RuntimeError):
        display.touch_down(1, 1)
        display.touch_down(1, 1)


def test_touches_without_polling_are_not_reported(terminal_pair):
    display, tm = terminal_pair
    tm.enter(touch=0)
    display.tap(10, 10, hold=0.01)
    assert tm.mcu.quiet(0.1) == b"" and len(tm.reports()) == 1


@pytest.mark.parametrize("terminal_pair", ["eu-on-us"], indirect=True)
def test_swipe_and_offset_on_the_eu_receiver(terminal_pair):
    display, tm = terminal_pair
    tm.enter()
    display.swipe("left", distance=200, duration=0.05)
    samples = tm.samples(lambda s: s and s[-1].mask == 0)
    assert samples[0].x == 227 + 100 - 26 and samples[0].y == 160
    assert samples[-1].mask == 0 and samples[-1].x == 227 - 100 - 26


def test_power_cycle_ends_the_session_and_clears_the_screen(terminal_pair):
    display, tm = terminal_pair
    case, stream, fb = vector("us_solid_full")
    tm.enter(session=case["session"])
    tm.stream(stream[:2000])
    display.wait_for(lambda d: d.terminal.pixels_written > 0)
    display.power_cycle()
    assert display.terminal.stopped == "power" and not display.terminal_active
    assert display.terminal_rgb565() == bytes(len(fb))
    # What was still on its way arrives as instructions (rejected, 0x1A), and may swallow the
    # first connect, as on a real display; the firmware retries.
    mark = tm.mcu.mark()
    for _ in range(5):
        tm.mcu.send_raw(TERMINATOR)
        tm.mcu.send("connect")
        time.sleep(0.1)
        if any(f.startswith(b"comok") for f in tm.mcu.frames(mark)):
            break
    else:
        raise AssertionError(tm.mcu.frames(mark))


def test_snapshot_shows_the_framebuffer_turned(terminal_pair):
    pytest.importorskip("numpy")
    display, tm = terminal_pair
    case, stream, fb = vector("us_burst")
    tm.enter(session=case["session"])
    tm.stream(stream)
    display.wait_for_terminal_end()
    display.rotation = 180
    shot = display.snapshot()
    assert shot.shape == (480, 320, 3)
    # The white pixel at 100,200, turned half a turn.
    assert tuple(shot[479 - 200, 319 - 100]) == (255, 255, 255)
    assert tuple(display.terminal_snapshot(rotation=0)[200, 100]) == (255, 255, 255)
    assert display.unrotate(319 - 100, 479 - 200) == (100, 200)
    assert display.terminal_image().size == (320, 480)
