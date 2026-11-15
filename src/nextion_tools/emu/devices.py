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


# The Sonoff NSPanel's display, the same model in US and EU panels: NX4832F035_011C, MCU code
# 61744 (0xF130). Measured on a panel ("Office") 2026-10-23/24: the input buffer held 1080-1092
# bytes, and a page change blocked the next instruction for about 400 ms. The `connect` reply's
# fourth field is the TFT's native version (72 here, as NSPanel Manager's TFT reports on one
# panel); the serial number differs per panel.
NX4832F035 = Device(
    model="NX4832F035_011C",
    comok=b"comok 2,30614-0,NX4832F035_011C,72,61744,018C3401CB9C6761,16777216",
    flash_size=16777216,
    buffer_capacity=1088,
    redraw_s=0.4,
)

# The name this profile had in 0.1.x, when it carried a model and MCU code (NX4832T035_011R,
# 61488) that no NSPanel reports. Kept so code that imports it still works.
NX4832T035 = NX4832F035
