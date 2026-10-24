"""The measurements made on a real NX4832T035 (Office, 2026-10-23/24), replayed against the flow
model. If a change to the model makes one of these fail, the model no longer matches hardware.

E1/E2: how the display replies. E3: the input buffer. E4: drain rate without a stall.
E5: a credit window in front of a stall.
"""

import time

import pytest

from nextion_tools.emu.protocol import TERMINATOR

OK = b"\x01"
INVALID = b"\x1a"
OVERFLOW = b"\x24"


@pytest.fixture
def flow(pair):
    display, mcu = pair
    display.flow_model = True
    display.resolver = lambda key: not key.startswith("nosuch")
    mcu.send("bkcmd=3")
    assert mcu.wait_frames(1) == [OK]
    return display, mcu


def acks_since(mcu, mark):
    return [f for f in mcu.frames(since=mark) if f == OK]


# ---- E1/E2: replies -------------------------------------------------------------------------

def test_e1_assignment_acks(flow):
    display, mcu = flow
    mark = mcu.mark()
    mcu.send("sya0=sya0")
    assert mcu.wait_frames(1, since=mark) == [OK]


def test_e1_get_answers_its_value_instead_of_an_ack(flow):
    display, mcu = flow
    mark = mcu.mark()
    mcu.send("get sya0")
    assert mcu.wait_bytes(8, since=mark)[:1] == b"\x71"
    assert mcu.quiet(0.2, since=mark)[8:] == b""


def test_e1_printh_echoes_without_an_ack(flow):
    display, mcu = flow
    mark = mcu.mark()
    mcu.send("printh a5 01 ff ff ff")
    assert mcu.quiet(0.3, since=mark) == b"\xa5\x01\xff\xff\xff"


@pytest.mark.parametrize("bad", ['nosuch.txt="x"', "nosuchvar=1", "this is not an instruction"])
def test_e1_every_kind_of_bad_instruction_is_0x1a(flow, bad):
    display, mcu = flow
    mark = mcu.mark()
    mcu.send(bad)
    assert mcu.wait_frames(1, since=mark) == [INVALID]


def test_e1_ref_acks_at_once_but_blocks_the_next_instruction(flow):
    display, mcu = flow
    mark = mcu.mark()
    start = time.monotonic()
    mcu.send("ref 0", "sya0=1")
    mcu.wait_frames(2, since=mark)
    first, second = mcu.frame_time(0, since=mark), mcu.frame_time(1, since=mark)
    assert first - start < 0.1
    assert 0.38 <= second - first < 0.6


def test_e1_page_acks_before_its_redraw(flow):
    display, mcu = flow
    mark = mcu.mark()
    mcu.send("page home", "sya0=1")
    mcu.wait_frames(2, since=mark)
    assert mcu.frame_time(1, since=mark) - mcu.frame_time(0, since=mark) >= 0.38
    assert display.page == "home"


def test_e1_delay_acks_when_it_ends(flow):
    display, mcu = flow
    mark = mcu.mark()
    start = time.monotonic()
    mcu.send("delay=200")
    mcu.wait_frames(1, since=mark)
    assert mcu.frame_time(0, since=mark) - start >= 0.2


def test_e1_bkcmd_3_acks_itself(pair):
    display, mcu = pair
    display.flow_model = True
    mcu.send("bkcmd=3")
    assert mcu.wait_frames(1) == [OK]


def test_e2_at_bkcmd_0_only_answers_and_echoes_come_back(flow):
    display, mcu = flow
    mcu.send("bkcmd=0")
    display.wait_for(lambda d: d.bkcmd == 0, timeout=2)
    time.sleep(0.1)
    mark = mcu.mark()
    mcu.send("sya0=1", "nosuchvar=1", "get sya0", "printh a5 02")
    assert mcu.quiet(0.4, since=mark) == b"\x71\x01\x00\x00\x00" + TERMINATOR + b"\xa5\x02"


def test_e2_nothing_unsolicited_when_idle(flow):
    display, mcu = flow
    assert mcu.quiet(1.0) == b""


# ---- E3: the input buffer -------------------------------------------------------------------

def burst(n, prefix="sya0"):
    commands = ["%s=%04d" % (prefix, i) for i in range(n)]
    assert all(len(c) + len(TERMINATOR) == 12 for c in commands)
    return commands


def test_e3_ninety_12_byte_commands_survive_a_stall(flow):
    display, mcu = flow
    commands = burst(120)
    mark = mcu.mark()
    mcu.send("delay=1000", *commands)
    time.sleep(1.6)
    frames = mcu.frames(since=mark)
    # The delay's own ack, then 0x24 as the display resumes, then the 90 that were kept.
    assert frames[0] == OK
    assert frames[1] == OVERFLOW
    assert frames[2:] == [OK] * 90
    assert display.commands[-90:] == commands[:90]  # the first ones: what arrived later was lost
    assert display.overflows == 1


def test_e3_the_overflow_is_reported_on_resume_not_when_bytes_are_lost(flow):
    display, mcu = flow
    mark = mcu.mark()
    start = time.monotonic()
    mcu.send("delay=1000", *burst(120))
    mcu.wait_frames(2, since=mark, timeout=3)
    # The burst overflows ~0.1 s in; 0x24 comes once the delay is over.
    assert mcu.frame_time(1, since=mark) - start >= 1.0


def test_e3_1056_bytes_and_a_25_byte_marker_fit(flow):
    display, mcu = flow
    marker = "printh fe 01 02 030405"
    assert len(marker) + len(TERMINATOR) == 25
    mark = mcu.mark()
    mcu.send("delay=1000", *burst(88), marker)  # 1056 + 25 = 1081 bytes queued behind the delay
    time.sleep(1.5)
    assert display.overflows == 0
    assert len(acks_since(mcu, mark)) == 1 + 88
    assert mcu.quiet(0, since=mark).endswith(b"\xfe\x01\x02\x03\x04\x05")


def test_e3_a_burst_that_fits_is_never_reported(flow):
    display, mcu = flow
    mark = mcu.mark()
    mcu.send("delay=500", *burst(80))
    time.sleep(1.0)
    assert OVERFLOW not in mcu.frames(since=mark)
    assert len(acks_since(mcu, mark)) == 81


# ---- E4: drain rate without a stall ---------------------------------------------------------

@pytest.mark.parametrize("command, per_second", [
    ("sya0=%04d", 960),  # 12 bytes: line rate, as measured
    ("sya0=%04d+sya1+sya1+sya1+sya1+sya", 320),  # 36 bytes
])
def test_e4_the_display_keeps_up_with_the_wire(flow, command, per_second):
    display, mcu = flow
    commands = [command % i for i in range(300)]
    size = len(commands[0]) + 3
    assert abs(115200 / 10 / size - per_second) < 1
    mark = mcu.mark()
    mcu.send(*commands)
    mcu.wait_frames(300, since=mark, timeout=5)
    elapsed = mcu.frame_time(299, since=mark) - mcu.frame_time(0, since=mark)
    rate = 299 / elapsed
    assert per_second * 0.85 <= rate <= per_second * 1.1
    assert display.overflows == 0


# ---- E5: a credit window in front of a stall ------------------------------------------------

def windowed(display, mcu, window, count=300, stall_ms=300):
    """Send a stall then `count` 12-byte commands, keeping at most `window` bytes unacknowledged
    (0: no window). Returns (acks received, overflows)."""
    before = display.overflows
    mark = mcu.mark()
    commands = ["delay=%d" % stall_ms] + burst(count)
    sizes = [len(c) + 3 for c in commands]
    sent = 0
    deadline = time.monotonic() + 10
    while sent < len(commands) and time.monotonic() < deadline:
        acked = len(acks_since(mcu, mark))
        in_flight = sum(sizes[acked:sent])
        if window == 0 or in_flight + sizes[sent] <= window:
            mcu.send(commands[sent])
            sent += 1
        else:
            time.sleep(0.002)
    time.sleep(stall_ms / 1000 + 0.5)
    return len(acks_since(mcu, mark)), display.overflows - before


def test_e5_no_window_overflows(flow):
    acks, overflows = windowed(*flow, window=0)
    assert overflows == 1 and acks < 301


def test_e5_a_2k_window_overflows(flow):
    acks, overflows = windowed(*flow, window=2048)
    assert overflows == 1 and acks < 301


@pytest.mark.parametrize("window", [1024, 768, 512, 256])
def test_e5_a_window_under_the_buffer_never_overflows(flow, window):
    acks, overflows = windowed(*flow, window=window)
    assert overflows == 0
    assert acks == 301
