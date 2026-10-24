"""The display's serial input: the wire, a bounded input buffer, and a processor that can stall.

Pure logic in virtual time, with no threads or clock of its own, so tests can drive it exactly.
`Display` drives it in real time.

What it models, measured on an NX4832T035 at 115200 baud:

- Bytes arrive at line rate (10 bits per byte). QEMU's UART does not pace what the firmware
  writes, so without this an emulated display would see bursts no real wire can deliver.
- The display executes ordinary instructions as fast as the wire delivers them. It only falls
  behind while it stalls: on `page` and `ref` (a full redraw, about 400 ms) and on `delay=N`.
- Queued input that has not been executed lives in a buffer of about 1080-1092 bytes. Once it is
  full, bytes that arrive are dropped and what is already queued is kept.
- `0x24` is sent when the display resumes after dropping input, not when it drops it.

One assumption is not measured: a command that loses any of its bytes is discarded as a whole,
and the framing (FF FF FF) is still followed through the bytes that were dropped. So a lost
command never fuses with the next one.
"""

import collections
from typing import Callable, Deque, List, Optional, Tuple

from .protocol import TERMINATOR

BITS_PER_BYTE = 10  # 8N1
DEFAULT_BAUD = 115200
DEFAULT_CAPACITY = 1088  # an NX4832T035's (devices.py)

# execute(raw, t) -> (stall seconds, callback at the end of the stall or None)
Execute = Callable[[bytes, float], Tuple[float, Optional[Callable[[float], None]]]]


class FlowModel:
    def __init__(self, execute: Execute, overflowed: Callable[[float], None],
                 baud: int = DEFAULT_BAUD, capacity: int = DEFAULT_CAPACITY):
        self._execute = execute
        self._overflowed = overflowed
        self.baud = baud
        self.capacity = capacity

        # On the wire: [arrival time of first byte, byte interval, data, bytes delivered].
        self._wire: Deque[list] = collections.deque()
        self._wire_free = 0.0  # when the last queued byte finishes arriving
        # Arrived, not yet executed.
        self._queued: Deque[bytes] = collections.deque()
        self._queued_bytes = 0
        self._partial = bytearray()  # bytes of the command now arriving, as stored
        self._partial_seen = b""  # the last bytes seen for it, dropped or not, to find FF FF FF
        self._partial_dropped = False

        self._now = 0.0  # virtual time of the last event handled
        self._busy_until: Optional[float] = None
        self._after: Optional[Callable[[float], None]] = None
        self._overflow_pending = False
        self.paused = False  # stop executing (the display is busy with something else)

        # Statistics.
        self.overflows = 0  # times input was dropped (each followed by one 0x24)
        self.dropped_bytes = 0
        self.lost: List[bytes] = []  # commands discarded because some of their bytes were dropped
        self.max_buffered = 0

    # ---- input ---------------------------------------------------------------

    @property
    def byte_time(self) -> float:
        return BITS_PER_BYTE / self.baud

    def receive(self, data: bytes, now: float) -> None:
        """Bytes the MCU wrote at `now`. They arrive one byte time apart, after anything still on
        the wire."""
        if not data:
            return
        start = max(self._wire_free, now)
        interval = self.byte_time
        self._wire.append([start + interval, interval, bytes(data), 0])
        self._wire_free = start + interval * len(data)

    @property
    def buffered(self) -> int:
        """Bytes in the input buffer: queued commands and the one arriving."""
        return self._queued_bytes + len(self._partial)

    @property
    def idle(self) -> bool:
        """Nothing on the wire, nothing queued, nothing executing."""
        return not self._wire and not self._queued and not self._partial and self._busy_until is None

    def next_event(self) -> Optional[float]:
        """The virtual time at which run() next has something to do, or None."""
        times = []
        if self._wire:
            first = self._wire[0]
            times.append(first[0] + first[1] * first[3])
        if self._busy_until is not None:
            times.append(self._busy_until)
        elif self._queued and not self.paused:
            times.append(self._now)
        return min(times) if times else None

    def take_all(self) -> bytes:
        """Remove and return everything not yet executed, in order: queued commands (with their
        terminators), the partial command, and what is still on the wire. For a switch to raw
        input, e.g. a TFT upload."""
        out = bytearray()
        for raw in self._queued:
            out += raw + TERMINATOR
        out += self._partial
        for start, interval, data, delivered in self._wire:
            out += data[delivered:]
        self._queued.clear()
        self._queued_bytes = 0
        self._partial = bytearray()
        self._partial_seen = b""
        self._partial_dropped = False
        self._wire.clear()
        return bytes(out)

    # ---- time ----------------------------------------------------------------

    def run(self, now: float) -> None:
        """Handle every event due by `now`, in time order."""
        while True:
            if self._busy_until is None and self._queued and not self.paused:
                self._start_next()
                continue
            t_byte = None
            if self._wire:
                first = self._wire[0]
                t_byte = first[0] + first[1] * first[3]
            t_done = self._busy_until
            candidates = [t for t in (t_byte, t_done) if t is not None and t <= now]
            if not candidates:
                return
            if t_done is not None and t_done <= now and (t_byte is None or t_done <= t_byte):
                self._now = t_done
                self._finish_stall()
            else:
                self._now = t_byte
                self._deliver_byte()

    def _start_next(self) -> None:
        raw = self._queued.popleft()
        self._queued_bytes -= len(raw) + len(TERMINATOR)
        stall, after = self._execute(raw, self._now)
        if stall > 0 or after is not None:
            self._busy_until = self._now + max(stall, 0.0)
            self._after = after
        # A zero-time command with nothing after it is done at once.

    def _finish_stall(self) -> None:
        after, self._after = self._after, None
        self._busy_until = None
        if after is not None:
            after(self._now)
        self._resume()

    def _resume(self) -> None:
        if self._overflow_pending:
            self._overflow_pending = False
            self._overflowed(self._now)

    def _deliver_byte(self) -> None:
        chunk = self._wire[0]
        b = chunk[2][chunk[3]]
        chunk[3] += 1
        if chunk[3] == len(chunk[2]):
            self._wire.popleft()
        self._partial_seen = (self._partial_seen + bytes([b]))[-len(TERMINATOR):]
        if self.buffered >= self.capacity:
            self.dropped_bytes += 1
            self._partial_dropped = True
            if not self._overflow_pending:
                self._overflow_pending = True
                self.overflows += 1
        else:
            self._partial.append(b)
            self.max_buffered = max(self.max_buffered, self.buffered)
        if self._partial_seen == TERMINATOR:
            self._end_command()
            if self._busy_until is None and self._overflow_pending and not self._queued:
                # Dropped while not stalled (a command longer than the whole buffer): nothing to
                # resume from, so report it now.
                self._resume()

    def _end_command(self) -> None:
        raw = bytes(self._partial)
        if raw.endswith(TERMINATOR):
            raw = raw[:-len(TERMINATOR)]
        if self._partial_dropped:
            # What was stored of it; part of the terminator may be among what was dropped.
            self.lost.append(raw.rstrip(b"\xff"))
        else:
            self._queued.append(raw)
            self._queued_bytes += len(raw) + len(TERMINATOR)
        self._partial = bytearray()
        self._partial_seen = b""
        self._partial_dropped = False
