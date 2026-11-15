"""The nspanel-terminal pixel stream (protocol 8), display side.

nspanel-terminal (https://github.com/ptaru/nspanel-terminal) is a TFT for the NSPanel's display
whose native code adds a receiver for RGB565 rectangles: the MCU draws with LVGL and streams the
pixels at 2 or 4 Mbaud instead of sending Nextion instructions. This module emulates that
receiver, as documented in its docs/PROTOCOL.md and implemented by its firmware/src
(stream_protocol.h, terminal_receiver.c). Display (display.py) runs one when a `touch_j` starts
a session; Receiver can also be used on its own.

The wire format, briefly:

- **Entry**, in Nextion command mode: `sys0` = baud rate (2000000 or 4000000), `sys1` = touch
  polling (0 or 1), `sys2` = a nonzero 16-bit session id, then `touch_j`. The receiver answers
  with a READY_BRIGHTNESS report at the new baud rate.
- **Forward records**, MCU to display: COBS-encoded, each ended by one 0x00. A 20-byte
  little-endian header ('R' 'S', version 8, kind, session, sequence, body length, x, y, w, h,
  CRC16-CCITT of bytes 0..17) and at most 512 body bytes. RECT opens a window, DATA carries
  PackBits RGB565 pixels (high byte first) for it, END closes it (END x=1 also ends a burst and
  asks for DONE). PING keeps an idle session alive, EXIT ends it, SYNC clears a fault,
  BRIGHTNESS sets the backlight.
- **Reverse reports**, display to MCU: 32 bytes, not framed ('S' 'R', version, status, session,
  next expected sequence, cumulative bytes consumed, rectangles completed, flags, tick, touch
  mask/freshness/x/y, window 512, ring high-water, CRC16). One on every event (DONE, SYNCED,
  BYE, ERROR, BRIGHTNESS_DONE), one after every 256 bytes consumed (PROGRESS), and one per fresh
  touch sample.
- **Credits**: the MCU may have at most 512 encoded bytes outstanding beyond the last consumed
  count it was told. More is a credit violation, which ends the session.
- **Errors**: a bad record (CRC, sequence, geometry, PackBits, COBS) reports ERROR once and
  faults: nothing is drawn until a SYNC. A credit violation or 3 s without input ends the session
  with ERROR and a flag (0x1000, 0x2000); the display then returns to its stock runtime.

What is the emulator's own choice, not documented upstream (see the README's list):

- Bytes are consumed as they arrive, so the ring never fills; instead the credit rule is checked
  as the protocol states it (more than 512 bytes beyond the last report is a violation), which a
  conforming sender never trips and a real receiver might not catch.
- Pixels reach the framebuffer when a record's delimiter arrives, not byte by byte; what is drawn
  in the end (including from a record that turns out to be bad) is the same.
- A touch sample is reported once, with freshness 1; other reports carry the current mask and
  position with freshness 0.

Parts of this module are a port of nspanel-terminal's firmware/src/stream_protocol.h and
terminal_receiver.c, used under the MIT licence:

    Copyright (c) 2026 NSPanel public contributors

    Permission is hereby granted, free of charge, to any person obtaining a copy of this software
    and associated documentation files (the "Software"), to deal in the Software without
    restriction, including without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all copies or
    substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
    BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import collections
import logging
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("nextion_tools.emu.terminal")

VERSION = 8
# Forward record kinds.
RECT, DATA, END, PING, EXIT, SYNC, BRIGHTNESS = range(1, 8)
KIND_NAMES = {RECT: "RECT", DATA: "DATA", END: "END", PING: "PING", EXIT: "EXIT", SYNC: "SYNC",
              BRIGHTNESS: "BRIGHTNESS"}
# Reverse report statuses.
READY, BYE, PROGRESS, ERROR, SYNCED, DONE, READY_BRIGHTNESS, BRIGHTNESS_DONE = range(128, 136)
STATUS_NAMES = {READY: "READY", BYE: "BYE", PROGRESS: "PROGRESS", ERROR: "ERROR", SYNCED: "SYNCED",
                DONE: "DONE", READY_BRIGHTNESS: "READY_BRIGHTNESS", BRIGHTNESS_DONE: "BRIGHTNESS_DONE"}
# Report flags.
FLAG_DMA = 0x0800
FLAG_CREDIT = 0x1000
FLAG_TIMEOUT = 0x2000

HEADER = 20
PAYLOAD = 512  # body bytes per record
MAX_ENCODED = 535  # COBS bytes per record, without its delimiter
WINDOW = 512  # bytes the MCU may have outstanding
REPORT = 32
PROGRESS_EVERY = 256
WATCHDOG_S = 3.0
BAUDS = (2000000, 4000000)
STOCK_BAUD = 115200
# The `connect` reply's native version for nspanel-terminal's protocol-8 TFTs (stock: 72).
NATIVE_VERSION = 94
# What the TFT's patched touch_j continuation prints when the receiver returns to the stock
# runtime (nspanel-terminal firmware/src/README.md, "Native patches").
RETURN_FRAME = b"\xa9\xff\xff\xff"
# The receiver waits this long after touch_j before it changes baud and reports READY.
ENTRY_DELAY_S = 0.05


@dataclass(frozen=True)
class TerminalProfile:
    """A receiver's geometry, which its TFT fixes, and how its touch reports are offset.

    `width` x `height` is the area RECT accepts, in the receiver's own coordinates: 320 x 480
    (portrait) for nspanel-terminal's US TFT, 454 x 320 (landscape) for its EU one, whose
    controller has 480 lines of which the receiver reaches 454.

    `touch_offset` is subtracted from x in touch reports: a touch at drawing x is reported at
    x - touch_offset. 0 on the panel each TFT is for. The NSPanel firmware's NextionTerminal adds 26
    to the EU receiver's touch x, measured with the EU TFT on a US panel, whose touch controller
    counts all 480 lines: TERMINAL_EU_ON_US models that.
    """
    name: str
    width: int
    height: int
    touch_offset: int = 0

    @property
    def size(self) -> Tuple[int, int]:
        return self.width, self.height


TERMINAL_US = TerminalProfile("us", 320, 480)
TERMINAL_EU = TerminalProfile("eu", 454, 320)
TERMINAL_EU_ON_US = TerminalProfile("eu-on-us", 454, 320, touch_offset=26)
PROFILES = {p.name: p for p in (TERMINAL_US, TERMINAL_EU, TERMINAL_EU_ON_US)}


def profile(which) -> TerminalProfile:
    """A TerminalProfile, or the one named "us", "eu" or "eu-on-us"."""
    if isinstance(which, TerminalProfile):
        return which
    try:
        return PROFILES[str(which).lower()]
    except KeyError:
        raise ValueError("no terminal profile %r (%s)" % (which, ", ".join(PROFILES))) from None


def terminal_comok(comok: bytes, native: int = NATIVE_VERSION) -> bytes:
    """A `connect` reply with its native version (the fourth field) replaced by `native`."""
    fields = comok.split(b",")
    if len(fields) < 4:
        raise ValueError("not a comok reply: %r" % comok)
    fields[3] = str(native).encode()
    return b",".join(fields)


# ---- encoding, for senders and tests ------------------------------------------------------------

def _crc_table() -> List[int]:
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ (0x1021 if c & 0x8000 else 0)) & 0xFFFF
        table.append(c)
    return table


_CRC_TABLE = _crc_table()


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC16-CCITT (polynomial 0x1021, initial 0xFFFF, no final XOR), as both ends use."""
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[(crc >> 8) ^ b]
    return crc


def cobs_encode(raw: bytes) -> bytes:
    """COBS, with the 0x00 delimiter appended."""
    out = bytearray()
    block = bytearray()
    for b in raw:
        if b == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(b)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    out.append(0)
    return bytes(out)


def header(session: int, seq: int, kind: int, x: int = 0, y: int = 0, w: int = 0, h: int = 0,
           length: int = 0) -> bytes:
    head = struct.pack("<2sBBHHHHHHH", b"RS", VERSION, kind, session & 0xFFFF, seq & 0xFFFF, length, x, y, w, h)
    return head + struct.pack("<H", crc16(head))


def encode_record(session: int, seq: int, kind: int, x: int = 0, y: int = 0, w: int = 0, h: int = 0,
                  body: bytes = b"") -> bytes:
    """One forward record, COBS-encoded with its delimiter."""
    if len(body) > PAYLOAD:
        raise ValueError("a body holds at most %d bytes, not %d" % (PAYLOAD, len(body)))
    return cobs_encode(header(session, seq, kind, x, y, w, h, len(body)) + body)


def packbits_encode(pixels: Sequence[int], payload: int = PAYLOAD) -> List[bytes]:
    """RGB565 pixels as PackBits DATA bodies of at most `payload` bytes each. A plain encoder
    (repeat runs of two or more, literals otherwise), not byte-identical to the firmware's."""
    bodies: List[bytes] = []
    body = bytearray()
    i, n = 0, len(pixels)
    while i < n:
        v = pixels[i]
        run = 1
        while run < 128 and i + run < n and pixels[i + run] == v:
            run += 1
        if run >= 2:
            token = bytes([127 + run, v >> 8 & 255, v & 255])
            i += run
        else:
            lit = [v]
            j = i + 1
            while len(lit) < 128 and j < n and not (j + 1 < n and pixels[j] == pixels[j + 1]):
                lit.append(pixels[j])
                j += 1
            room = (payload - len(body) - 1) // 2
            if room < 1:
                bodies.append(bytes(body))
                body = bytearray()
                continue
            lit = lit[:room]
            token = bytes([len(lit) - 1]) + b"".join(bytes([p >> 8 & 255, p & 255]) for p in lit)
            i += len(lit)
        if len(body) + len(token) > payload:
            bodies.append(bytes(body))
            body = bytearray()
        body += token
    if body:
        bodies.append(bytes(body))
    return bodies


@dataclass
class Report:
    status: int
    session: int
    seq: int  # the next sequence the receiver expects
    consumed: int  # encoded bytes consumed since the session began, modulo 2**32
    rectangles: int  # rectangles completed, modulo 2**16
    flags: int
    tick: int  # the display's millisecond tick
    mask: int  # touch mask: bit 0 = a finger is down; bit 5 = a touch fault (sticky)
    fresh: int  # 0 no new sample, 1 a valid one, 2 malformed contact data, 3 an I2C failure
    x: int  # contact 0, signed
    y: int
    window: int
    high: int  # ring high-water

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, str(self.status))

    @property
    def touching(self) -> bool:
        return bool(self.mask & 1)

    def pack(self) -> bytes:
        body = struct.pack("<2sBBHHIHHIBBhhHH", b"SR", VERSION, self.status, self.session, self.seq,
                           self.consumed & 0xFFFFFFFF, self.rectangles & 0xFFFF, self.flags,
                           self.tick & 0xFFFFFFFF, self.mask, self.fresh, self.x, self.y, self.window, self.high)
        return body + struct.pack("<H", crc16(body))


def parse_report(data: bytes) -> Report:
    """A 32-byte reverse report. Raises ValueError for a bad magic, version or CRC."""
    if len(data) != REPORT:
        raise ValueError("a report is %d bytes, not %d" % (REPORT, len(data)))
    magic, version, status, session, seq, consumed, rects, flags, tick, mask, fresh, x, y, window, high, crc = \
        struct.unpack("<2sBBHHIHHIBBhhHHH", data)
    if magic != b"SR" or version != VERSION:
        raise ValueError("not a protocol-%d report: %s" % (VERSION, data[:4].hex()))
    if crc16(data[:30]) != crc:
        raise ValueError("report CRC mismatch")
    return Report(status, session, seq, consumed, rects, flags, tick, mask, fresh, x, y, window, high)


def split_reports(data: bytes) -> Tuple[List[Report], bytes]:
    """The whole reports at the start of `data`, and what is left over."""
    reports = []
    while len(data) >= REPORT:
        reports.append(parse_report(data[:REPORT]))
        data = data[REPORT:]
    return reports, data


# ---- the framebuffer -----------------------------------------------------------------------------

class Framebuffer:
    """What the LCD shows, as RGB565 pixels, high byte first (as they arrive), row by row."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.data = bytearray(2 * width * height)

    @property
    def size(self) -> Tuple[int, int]:
        return self.width, self.height

    def clear(self, color: int = 0) -> None:
        self.data[:] = bytes([color >> 8 & 255, color & 255]) * (self.width * self.height)

    def pixel(self, x: int, y: int) -> int:
        """The RGB565 value at (x, y)."""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError("(%d, %d) is outside %d x %d" % (x, y, self.width, self.height))
        i = 2 * (y * self.width + x)
        return self.data[i] << 8 | self.data[i + 1]

    def rgb565(self) -> bytes:
        return bytes(self.data)

    def rgb(self):
        """height x width x 3 uint8 numpy array (needs numpy)."""
        import numpy as np
        v = np.frombuffer(bytes(self.data), dtype=">u2").reshape(self.height, self.width).astype(np.uint16)
        r = (v >> 11) & 31
        g = (v >> 5) & 63
        b = v & 31
        out = np.empty((self.height, self.width, 3), dtype=np.uint8)
        out[..., 0] = (r << 3) | (r >> 2)
        out[..., 1] = (g << 2) | (g >> 4)
        out[..., 2] = (b << 3) | (b >> 2)
        return out


class _Window:
    """Where the next pixel of the active rectangle goes."""

    def __init__(self, fb: Framebuffer, x: int, y: int, w: int, h: int):
        self.fb, self.x, self.y, self.w, self.h = fb, x, y, w, h
        self.at = 0  # pixels written

    def write(self, pixels: bytes) -> None:
        fb, data, stride = self.fb, self.fb.data, 2 * self.fb.width
        total = self.w * self.h
        view = memoryview(pixels)
        while view:
            at = self.at % total  # the LCD wraps within its window
            row, col = divmod(at, self.w)
            n = min(self.w - col, len(view) // 2)
            off = (self.y + row) * stride + 2 * (self.x + col)
            data[off:off + 2 * n] = view[:2 * n]
            view = view[2 * n:]
            self.at += n


# ---- the receiver --------------------------------------------------------------------------------

class Receiver:
    """One session of the terminal receiver: feed it what the MCU sends, send what it returns.

        rx = Receiver(session=0x1234, profile=TERMINAL_US, now=t)
        mcu.send(rx.ready())
        for report in rx.feed(data, now): mcu.send(report)
        rx.check(now)       # the 3 s inactivity watchdog
        rx.touch(True, x, y)  # a finger, reported if touch polling is on

    After a BYE, a fatal error or the watchdog, `stopped` says why ("exit", "credit", "timeout")
    and the receiver takes nothing more: the display is back in its stock runtime.
    """

    def __init__(self, session: int, profile: TerminalProfile = TERMINAL_US,
                 framebuffer: Optional[Framebuffer] = None, touch_enabled: bool = True, now: float = 0.0,
                 baud: int = 4000000, strict_credits: bool = True):
        if not 0 < session <= 0xFFFF:
            raise ValueError("session must be 1-65535, not %r" % session)
        self.session = session
        self.profile = profile
        self.fb = framebuffer if framebuffer is not None else Framebuffer(profile.width, profile.height)
        if self.fb.size != profile.size:
            raise ValueError("framebuffer is %dx%d, the profile %dx%d" % (self.fb.size + profile.size))
        self.touch_enabled = touch_enabled
        self.baud = baud
        self.strict_credits = strict_credits
        self.started_at = now
        self.last_input = now

        # The decoder (stream_protocol.h's StreamPackBits).
        self.seq = 0
        self.rectangles = 0
        self.fault = False
        self._window: Optional[_Window] = None
        self._remaining = 0  # pixel bytes still due in the active rectangle
        self._frame = bytearray()
        self._encoded = 0  # nonzero bytes of the current record

        # The loop (terminal_receiver.c).
        self.received = 0
        self.consumed = 0
        self.published = 0
        self.high = 0
        self.stopped: Optional[str] = None
        self.flags = 0

        # Touch, as the Goodix controller last reported it.
        self.mask = 0
        self.touch_x = 0
        self.touch_y = 0

        # What happened, for tests.
        self.records: Dict[str, int] = collections.Counter()  # valid records by kind name
        self.faults: List[str] = []  # why each bad record was bad
        self.reports: List[Report] = []  # every report sent
        self.brightness: Optional[int] = None  # the last BRIGHTNESS percentage
        self.pixels_written = 0
        self.changes = 0  # goes up whenever a record changed the framebuffer

    @property
    def active(self) -> bool:
        return self.stopped is None

    # ---- reports ---------------------------------------------------------------------------

    def _report(self, status: int, now: float, fresh: int = 0, flags: int = 0) -> bytes:
        r = Report(status, self.session, self.seq, self.consumed, self.rectangles, flags,
                   int(now * 1000) & 0xFFFFFFFF, self.mask, fresh, _s16(self.touch_x - self.profile.touch_offset),
                   _s16(self.touch_y), WINDOW, min(self.high, 0xFFFF))
        self.reports.append(r)
        self.published = self.consumed
        if status != PROGRESS:
            log.debug("terminal %04x: %s (seq %d, consumed %d, rectangles %d, flags %#x)", self.session,
                      r.status_name, r.seq, r.consumed, r.rectangles, flags)
        return r.pack()

    def ready(self, now: Optional[float] = None) -> bytes:
        """The report the receiver sends once it is listening (READY_BRIGHTNESS)."""
        return self._report(READY_BRIGHTNESS, self.started_at if now is None else now)

    # ---- input -----------------------------------------------------------------------------

    def feed(self, data: bytes, now: float) -> List[bytes]:
        """Take bytes from the MCU. Returns the reports to send back, in order."""
        out: List[bytes] = []
        if self.stopped is not None or not data:
            return out
        self.last_input = now
        self.received += len(data)
        self.high = max(self.high, min(len(data), WINDOW))
        # Bytes that arrive together were all sent before the MCU could see a report made while
        # they are consumed, so the MCU knew of no more credit than the last report gave.
        limit = self.published + WINDOW
        view = memoryview(data)
        while view:
            if self.strict_credits:
                allowed = limit - self.consumed
                if allowed <= 0:
                    self._stop("credit", out, now, ERROR, FLAG_CREDIT)
                    log.warning("terminal %04x: credit violation: %d bytes beyond the last report (consumed %d)",
                                self.session, self.received - self.published, self.published)
                    return out
            else:
                allowed = len(view)
            step = min(len(view), allowed, self.published + PROGRESS_EVERY - self.consumed)
            if step <= 0:
                step = min(len(view), allowed)
            zero = bytes(view[:step]).find(0)
            if zero >= 0:
                step = zero + 1
            chunk = view[:step]
            view = view[step:]
            if zero >= 0:
                self._take(chunk[:-1])
                self.consumed += step
                event = self._end_frame()
            else:
                self._take(chunk)
                self.consumed += step
                event = 0
            if event or self.consumed - self.published >= PROGRESS_EVERY:
                out.append(self._report(event or PROGRESS, now))
                if event == BYE:
                    self.stopped = "exit"
                    return out
        return out

    def check(self, now: float) -> List[bytes]:
        """The watchdog: 3 s without input ends the session with ERROR (flag 0x2000)."""
        out: List[bytes] = []
        if self.stopped is None and now - self.last_input >= WATCHDOG_S:
            log.warning("terminal %04x: no input for %.1f s; back to the stock runtime", self.session,
                        now - self.last_input)
            self._stop("timeout", out, now, ERROR, FLAG_TIMEOUT)
        return out

    @property
    def watchdog_due(self) -> Optional[float]:
        return None if self.stopped is not None else self.last_input + WATCHDOG_S

    def touch(self, down: bool, x: int, y: int, now: float) -> List[bytes]:
        """A touch sample at drawing (x, y): reported with freshness 1 if touch polling is on.
        A lift keeps the last position, as the Goodix controller reports no contact then."""
        if self.stopped is not None or not self.touch_enabled:
            return []  # without touch polling (sys1=0) the receiver never reads the controller
        self.mask = (self.mask & 32) | (1 if down else 0)
        if down:
            self.touch_x, self.touch_y = x, y
        return [self._report(PROGRESS, now, fresh=1)]

    def stop(self, reason: str = "power") -> None:
        """End the session without a word (the display lost power)."""
        if self.stopped is None:
            self.stopped = reason

    def _stop(self, reason: str, out: List[bytes], now: float, status: int, flags: int) -> None:
        self.flags = flags
        self.faults.append(reason)
        out.append(self._report(status, now, flags=flags))
        self.stopped = reason

    # ---- the decoder ---------------------------------------------------------------------------

    def _take(self, chunk) -> None:
        """Nonzero bytes of the current record."""
        room = MAX_ENCODED + 1 - len(self._frame)
        if room > 0:
            self._frame += chunk[:room]
        self._encoded += len(chunk)

    def _end_frame(self) -> int:
        """A delimiter: decode the record. Returns the event to report, or 0."""
        encoded, frame = self._encoded, bytes(self._frame)
        self._frame.clear()
        self._encoded = 0
        if not encoded:
            return 0  # an empty frame (a bare delimiter, as before SYNC)
        why = self._decode(frame, encoded)
        if why is not None:
            self.faults.append(why)
            self._window = None
            first = not self.fault
            self.fault = True
            if first:
                log.info("terminal %04x: record fault: %s", self.session, why)
            return ERROR if first else 0
        head = self._head
        kind = head[3]
        self.records[KIND_NAMES[kind]] += 1
        self.seq = (head[6] | head[7] << 8) + 1 & 0xFFFF
        x = head[10] | head[11] << 8
        if kind == SYNC:
            self.fault = False
            self._window = None
            self._remaining = 0
            return SYNCED
        if kind == RECT:
            y, w, h = struct.unpack_from("<HHH", head, 12)
            self._window = _Window(self.fb, x, y, w, h)
            self._remaining = 2 * w * h
            return 0
        if kind == END:
            self._window = None
            self.rectangles = self.rectangles + 1 & 0xFFFF
            return DONE if x else 0
        if kind == EXIT:
            return BYE
        if kind == BRIGHTNESS:
            self.brightness = x
            return BRIGHTNESS_DONE
        return 0

    def _decode(self, frame: bytes, encoded: int) -> Optional[str]:
        """COBS-decode and check one record, drawing its pixels. Returns why it is bad, or None.
        Follows stream_feed()/stream_decoded(): the header is checked before any body byte is
        used, pixels are drawn as their tokens complete, and anything left over is a fault."""
        overlong = encoded > MAX_ENCODED
        frame = frame[:MAX_ENCODED]  # the receiver stops decoding there, having drawn what came before
        # COBS: a code byte, then code-1 data bytes; a zero between blocks unless the code was 255.
        parts = []
        i, n, zero, missing = 0, len(frame), False, 0
        while i < n:
            code = frame[i]
            i += 1
            if zero:
                parts.append(b"\0")
            block = frame[i:i + code - 1]
            parts.append(block)
            i += len(block)
            missing = code - 1 - len(block)
            zero = code != 255
        decoded = b"".join(parts)
        self._head = head = decoded[:HEADER]
        length = 0
        if len(head) == HEADER:
            length = head[8] | head[9] << 8
            if crc16(head[:18]) != (head[18] | head[19] << 8):
                bad = "header CRC"
            else:
                bad = self._header_problem(head)
        else:
            bad = "short record (%d bytes)" % len(decoded)
        if bad is None and head[3] == DATA:
            bad = self._pixels(decoded[HEADER:HEADER + length])
        if bad is None and overlong:
            bad = "record longer than %d encoded bytes" % MAX_ENCODED
        if bad is None and len(decoded) > HEADER + length:
            bad = "body longer than its length %d" % length
        if bad is None and missing:
            bad = "COBS block cut short"
        if bad is None and len(decoded) != HEADER + length:
            bad = "body shorter than its length %d" % length
        return bad

    def _header_problem(self, p: bytes) -> Optional[str]:
        """stream_header_ok(): why a header with a good CRC is refused, or None."""
        kind = p[3]
        session, seq, n, x, y, w, h = struct.unpack_from("<HHHHHHH", p, 4)
        if p[0:2] != b"RS" or p[2] != VERSION:
            return "not a protocol-%d header" % VERSION
        if session != self.session:
            return "session %d, not %d" % (session, self.session)
        if n > PAYLOAD:
            return "body length %d" % n
        if kind != SYNC:
            if self.fault:
                return "faulted: %s before SYNC" % KIND_NAMES.get(kind, kind)
            if seq != self.seq:
                return "sequence %d, expected %d" % (seq, self.seq)
        W, H = self.profile.size
        active = self._window is not None
        if kind == RECT:
            if active or n or not w or not h or x >= W or y >= H or w > W - x or h > H - y:
                return "RECT %d,%d %dx%d%s" % (x, y, w, h, " with a rectangle active" if active else "")
            return None
        if kind == END:
            if not active or self._remaining or n or x > 1 or y or w or h:
                return "END with %s" % ("no rectangle" if not active else "%d pixel bytes due" % self._remaining
                                        if self._remaining else "bad fields")
            return None
        if kind == BRIGHTNESS:
            if active or n or x > 100 or y or w or h:
                return "BRIGHTNESS %d" % x
            return None
        if x or y or w or h:
            return "%s with geometry" % KIND_NAMES.get(kind, kind)
        if kind == DATA:
            return None if active and n else "DATA with %s" % ("no rectangle" if not active else "no body")
        if n:
            return "%s with a body" % KIND_NAMES.get(kind, kind)
        if kind in (PING, EXIT, SYNC):
            return None
        return "unknown kind %d" % kind

    def _pixels(self, body: bytes) -> Optional[str]:
        """PackBits: a token byte, high bit set for a repeat; low seven bits are the run - 1. A
        repeat is followed by one pixel, a literal by run pixels."""
        window = self._window
        out = []
        i, n, remaining, problem = 0, len(body), self._remaining, None
        while i < n:
            t = body[i]
            i += 1
            run = (t & 127) + 1
            if 2 * run > remaining:
                problem = "run of %d with %d pixels due" % (run, remaining // 2)
                break
            if t & 128:
                if i + 2 > n:
                    problem = "incomplete repeat"
                    break
                out.append(body[i:i + 2] * run)
                i += 2
                remaining -= 2 * run
            else:
                take = min(run, (n - i) // 2)
                out.append(body[i:i + 2 * take])
                i += 2 * take
                remaining -= 2 * take
                if take < run:
                    problem = "incomplete literal"
                    break
        pixels = b"".join(out)
        if pixels:
            window.write(pixels)
            self.pixels_written += len(pixels) // 2
            self.changes += 1
        self._remaining = remaining
        return problem


def _s16(v: int) -> int:
    return (v + 0x8000) % 0x10000 - 0x8000
