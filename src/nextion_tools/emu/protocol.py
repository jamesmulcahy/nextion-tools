"""The Nextion serial protocol: frames, reply codes, and the shape of an instruction.

Instructions from the MCU are text terminated by FF FF FF. The display answers with frames that
start with a code byte and end with FF FF FF, except during a TFT upload, whose replies are bare
bytes.

What the display actually returns was measured on an NX4832T035 (a Sonoff NSPanel) running the
NSPanel Manager TFT, and is noted at each code. Where that differs from Nextion's documentation,
the measurement wins.
"""

import re
from dataclasses import dataclass
from typing import Optional, Union

TERMINATOR = b"\xff\xff\xff"

# Replies to instructions (gated by bkcmd).
CMD_OK = 0x01
# The display answered 0x1A to every kind of bad instruction that was tried: an unknown
# component, an unknown variable, and plain bad syntax. Never 0x00, 0x02 or 0x1B.
INVALID = 0x1A

# Always sent, whatever bkcmd says.
STRING_DATA = 0x70  # answer to `get` of a string
NUMBER_DATA = 0x71  # answer to `get` of a number: 4 bytes, little-endian, signed
TOUCH_EVENT = 0x65  # page id, component id, 1 = press / 0 = release
BUFFER_OVERFLOW = 0x24  # sent when the display resumes after dropping input, not when it drops it
STARTUP = 0x00  # 00 00 00 FF FF FF at power-on (documented; not yet seen on hardware)
READY = 0x88  # after startup (documented; not yet seen on hardware)
AUTO_SLEEP = 0x86
AUTO_WAKE = 0x87

# TFT upload replies: bare bytes, no terminator.
UPDATE_ACK = 0x05  # ready for the next chunk
UPDATE_JUMP = 0x08  # whmi-wris only: followed by a u32le offset to continue from; 0 = carry on
UPDATE_CHUNK = 4096

BKCMD_DEFAULT = 2  # what a display powers on with: report failures only


def reports_success(bkcmd: int) -> bool:
    return bkcmd in (1, 3)


def reports_failure(bkcmd: int) -> bool:
    return bkcmd in (2, 3)


# Instructions from the Nextion Instruction Set. Anything that is neither one of these nor an
# assignment is bad syntax.
KEYWORDS = frozenset("""
    page ref click ref_stop ref_star get prints printh print vis tsw randset add addt cle rest
    doevents strlen btlen udelete crcrest crcputs crcputh crcputu spstr substr cov covx touch_j
    cls pic picq xpic xstr fill line draw draw3d cir cirs qrcode sendme connect connect_cmd
    wept rept cfgpio setlayer move play twfile delfile refile findfile rdfile newfile newdir
    deldir redir finddir beep wav lock unlock
    whmi-wri whmi-wris
""".split())

_NAME = r"[A-Za-z_][\w.\[\]]*"
_ASSIGN = re.compile(r"^(%s)(=|\+=|-=|\*=|/=|%%=)(.*)$" % _NAME, re.S)
_INCREMENT = re.compile(r"^(%s)(\+\+|--)$" % _NAME)

Value = Union[int, str]


@dataclass
class Instruction:
    text: str
    kind: str  # "assign", "increment", "keyword" or "invalid"
    name: Optional[str] = None  # assign / increment: the target, as written
    op: Optional[str] = None  # assign: "=", "+=", ...; increment: "++" or "--"
    value: Optional[str] = None  # assign: the right-hand side, as written
    keyword: Optional[str] = None  # keyword: e.g. "page"
    args: str = ""  # keyword: everything after it


def parse(text: str) -> Instruction:
    """Classify one instruction (without its terminator)."""
    m = _ASSIGN.match(text)
    if m:
        return Instruction(text, "assign", name=m.group(1), op=m.group(2), value=m.group(3))
    m = _INCREMENT.match(text)
    if m:
        return Instruction(text, "increment", name=m.group(1), op=m.group(2))
    keyword, _, args = text.partition(" ")
    if keyword in KEYWORDS:
        return Instruction(text, "keyword", keyword=keyword, args=args.strip())
    return Instruction(text, "invalid")


def parse_value(text: str) -> Value:
    """The literal on the right of an assignment: a quoted string, an integer, or (until
    expressions are evaluated) the expression's text."""
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\r", "\r")
    try:
        return int(text)
    except ValueError:
        return text


def frame(code: int, payload: bytes = b"") -> bytes:
    return bytes([code]) + payload + TERMINATOR


def number(value: int) -> bytes:
    return frame(NUMBER_DATA, (value & 0xFFFFFFFF).to_bytes(4, "little"))


def string(value: str) -> bytes:
    return frame(STRING_DATA, value.encode("utf-8"))


def printh_bytes(args: str) -> bytes:
    """The bytes `printh` sends: its arguments as hex pairs, with or without spaces."""
    digits = re.sub(r"\s+", "", args)
    if len(digits) % 2 or not re.fullmatch(r"[0-9A-Fa-f]*", digits):
        raise ValueError("printh needs hex pairs, got %r" % args)
    return bytes.fromhex(digits)


class Framer:
    """Splits a byte stream into FF FF FF-terminated frames."""

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes):
        """The complete frames in data (and what came before it), without terminators."""
        self._buf += data
        frames = []
        while TERMINATOR in self._buf:
            raw, self._buf = self._buf.split(TERMINATOR, 1)
            frames.append(raw)
        return frames

    def take_rest(self) -> bytes:
        """Whatever is buffered after the last frame, removing it."""
        rest, self._buf = self._buf, b""
        return rest
