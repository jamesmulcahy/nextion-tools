"""FlowModel in virtual time: every timing here is exact."""

import pytest

from nextion_tools.emu.flow import BITS_PER_BYTE, FlowModel
from nextion_tools.emu.protocol import TERMINATOR

BAUD = 115200
BYTE = BITS_PER_BYTE / BAUD


class Sim:
    """A FlowModel with a recording executor. Stalls come from `stalls`, by instruction."""

    def __init__(self, capacity=1088, stalls=None):
        self.executed = []  # (time, raw)
        self.resumed = []  # (time, raw) when an `after` ran
        self.overflow_at = []
        self.stalls = stalls or {}
        self.model = FlowModel(self.execute, self.overflow_at.append, baud=BAUD, capacity=capacity)

    def execute(self, raw, t):
        self.executed.append((t, raw))
        stall = self.stalls.get(raw, 0.0)
        if raw.startswith(b"delay="):
            stall = int(raw[6:]) / 1000

            def after(t2, raw=raw):
                self.resumed.append((t2, raw))
            return stall, after
        return stall, None

    def send(self, *commands, at=0.0):
        self.model.receive(b"".join(c.encode() + TERMINATOR for c in commands), at)

    def run(self, until):
        self.model.run(until)

    @property
    def names(self):
        return [raw.decode() for _, raw in self.executed]


def wire_time(*commands):
    return sum(len(c) + 3 for c in commands) * BYTE


def test_bytes_arrive_at_line_rate():
    sim = Sim()
    sim.send("a=1")  # 6 bytes
    sim.run(6 * BYTE - 1e-9)
    assert sim.executed == []
    sim.run(6 * BYTE)
    assert sim.executed == [(pytest.approx(6 * BYTE), b"a=1")]


def test_back_to_back_writes_queue_on_the_wire():
    sim = Sim()
    sim.send("a=1", at=0.0)
    sim.send("b=2", at=0.0)  # written at once, but can only follow the first on the wire
    sim.run(1.0)
    assert [t for t, _ in sim.executed] == [pytest.approx(6 * BYTE), pytest.approx(12 * BYTE)]


def test_a_write_after_the_wire_went_idle_starts_then():
    sim = Sim()
    sim.send("a=1", at=0.0)
    sim.send("b=2", at=1.0)
    sim.run(2.0)
    assert sim.executed[1][0] == pytest.approx(1.0 + 6 * BYTE)


def test_without_a_stall_the_display_keeps_up():
    sim = Sim(capacity=64)
    commands = ["n%d.val=%d" % (i, i) for i in range(500)]
    sim.send(*commands)
    sim.run(10.0)
    assert sim.names == commands
    assert sim.model.overflows == 0
    assert sim.model.max_buffered < 16  # never more than the command arriving
    assert sim.executed[-1][0] == pytest.approx(wire_time(*commands))


def test_a_stall_delays_what_follows():
    sim = Sim(stalls={b"page home": 0.4})
    sim.send("page home", "t0.txt=\"x\"")
    sim.run(10.0)
    t_page, t_next = sim.executed[0][0], sim.executed[1][0]
    assert t_page == pytest.approx(wire_time("page home"))
    assert t_next == pytest.approx(t_page + 0.4)


def test_the_after_callback_runs_when_the_stall_ends():
    sim = Sim()
    sim.send("delay=200", "a=1")
    sim.run(10.0)
    start = sim.executed[0][0]
    assert sim.resumed == [(pytest.approx(start + 0.2), b"delay=200")]
    assert sim.executed[1][0] == pytest.approx(start + 0.2)


def test_overflow_keeps_what_is_queued_and_drops_what_arrives_after():
    # Office E3: stall with delay=1000, then a burst of 12-byte commands.
    sim = Sim(capacity=1088)
    burst = ["sya0=%04d" % i for i in range(120)]  # 9 characters + terminator = 12 bytes
    assert len(burst[0]) + 3 == 12
    sim.send("delay=1000", *burst)
    sim.run(10.0)
    survivors = sim.names[1:]
    assert len(survivors) == 1088 // 12  # 90, as on the panel
    assert survivors == burst[:90]  # the first ones: tail drop
    # Every later command is lost; each one gets as far as the 8 bytes left free, and is discarded.
    assert len(sim.model.lost) == 30
    for raw, command in zip(sim.model.lost, burst[90:]):
        assert raw == command.encode()[:1088 - 90 * 12]
    assert sim.model.overflows == 1


def test_a_command_that_loses_any_byte_is_lost_whole_and_does_not_fuse():
    sim = Sim(capacity=20)
    long = "a" * 20 + "=1"  # 25 bytes: 20 are stored, the rest (with the terminator) dropped
    sim.send("delay=100", long, "b=2")
    sim.run(10.0)
    # The stored head of the lost command does not join b=2: framing went on through the drop.
    assert sim.names == ["delay=100", "b=2"]
    assert sim.model.lost == [long.encode()[:20]]


def test_0x24_is_reported_when_the_display_resumes():
    sim = Sim(capacity=100)
    sim.send("delay=1000", *["x%d=1" % i for i in range(40)])
    sim.run(10.0)
    start = sim.executed[0][0]
    assert sim.overflow_at == [pytest.approx(start + 1.0)]  # not when the bytes were dropped
    assert sim.resumed[0][0] <= sim.overflow_at[0]  # after the delay's own reply


def test_one_report_per_stall():
    sim = Sim(capacity=50)
    sim.send("delay=100", *["x%d=1" % i for i in range(20)])
    sim.run(5.0)
    sim.send("delay=100", *["y%d=1" % i for i in range(20)], at=5.0)
    sim.run(10.0)
    assert len(sim.overflow_at) == 2
    assert sim.model.overflows == 2


def test_a_command_longer_than_the_buffer_is_reported_at_once():
    sim = Sim(capacity=10)
    sim.send('t0.txt="%s"' % ("x" * 40))
    sim.run(1.0)
    assert sim.executed == []
    assert len(sim.model.lost) == 1
    assert sim.overflow_at == [pytest.approx(len('t0.txt="%s"' % ("x" * 40)) * BYTE + 3 * BYTE)]


def test_buffered_and_max_buffered():
    sim = Sim()
    sim.send("delay=1000", *["v%d=1" % i for i in range(10)])  # 7 bytes each after the 12-byte delay
    sim.run(0.5)
    assert sim.model.buffered == 10 * 7
    assert sim.model.max_buffered == 10 * 7
    sim.run(2.0)
    assert sim.model.buffered == 0
    assert sim.model.max_buffered == 10 * 7


def test_idle_and_next_event():
    sim = Sim()
    assert sim.model.idle
    assert sim.model.next_event() is None
    sim.send("delay=100", at=1.0)
    assert not sim.model.idle
    assert sim.model.next_event() == pytest.approx(1.0 + BYTE)
    sim.run(1.0 + 12 * BYTE + 1e-9)
    assert sim.model.next_event() == pytest.approx(1.0 + 12 * BYTE + 0.1)
    sim.run(5.0)
    assert sim.model.idle


def test_paused_stops_execution_but_not_arrival():
    sim = Sim()
    sim.model.paused = True
    sim.send("a=1", "b=2")
    sim.run(1.0)
    assert sim.executed == []
    assert sim.model.buffered == 12
    sim.model.paused = False
    sim.run(1.0)
    assert sim.names == ["a=1", "b=2"]
    assert sim.executed[0][0] == pytest.approx(12 * BYTE)


def test_take_all_returns_everything_unexecuted_in_order():
    sim = Sim()
    sim.send("delay=100", "a=1", "b=2")
    sim.model.receive(b"TFT\xff\xff", 0.0)  # raw bytes, no terminator
    sim.run(12 * BYTE + 6 * BYTE + 2 * BYTE)  # delay executing, a=1 queued, b= arriving
    rest = sim.model.take_all()
    assert rest == b"a=1" + TERMINATOR + b"b=2" + TERMINATOR + b"TFT\xff\xff"
    assert sim.model.buffered == 0
    sim.run(10.0)
    assert sim.names == ["delay=100"]


def test_baud_changes_the_line_rate():
    sim = Sim()
    sim.model.baud = 9600
    sim.send("a=1")
    sim.run(1.0)
    assert sim.executed[0][0] == pytest.approx(6 * 10 / 9600)


def test_an_empty_write_is_nothing():
    sim = Sim()
    sim.model.receive(b"", 0.0)
    assert sim.model.idle
