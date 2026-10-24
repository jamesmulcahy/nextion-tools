"""What differs between Nextion display models, as measured on each.

A Device holds the numbers the emulator needs that belong to the display, not to the project it
runs or the MCU that drives it. Only one model has been measured so far. Another gets its own
profile once someone measures it: replay the probes in tests/emu/test_calibration.py against
the real display and record what it does.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    model: str
    comok: bytes  # the reply to `connect`, without its terminator
    flash_size: int  # bytes, for TFT uploads
    buffer_capacity: int  # bytes of input the display holds before it drops what arrives
    redraw_s: float  # how long a full-page redraw (`page`, `ref`) blocks the next instruction


# The Sonoff NSPanel's display, measured on a panel ("Office") 2026-10-23/24: the input buffer
# held 1080-1092 bytes, and a page change blocked the next instruction for about 400 ms.
NX4832T035 = Device(
    model="NX4832T035_011R",
    comok=b"comok 2,30614-0,NX4832T035_011R,163,61488,D264B8204F0E1828,16777216",
    flash_size=16777216,
    buffer_capacity=1088,
    redraw_s=0.4,
)
