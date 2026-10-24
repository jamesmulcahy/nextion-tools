"""Tools for Nextion HMI displays: the Editor's project files and the display itself.

- `nextion_tools.hmi`: .HMI projects: pages, components, ids, geometry, event code, Program.s;
  numeric edits and re-stamping
- `nextion_tools.fonts`: .zi fonts: glyph bitmaps and metrics (numpy)
- `nextion_tools.images`: pictures as RGB arrays, RGB565 (numpy, Pillow)
- `nextion_tools.tft`: a compiled .tft's picture table, checksums and orientation
- `nextion_tools.crc`: the CRC behind TFT checksums and HMI stamps
- `nextion_tools.emu`: an emulated display, driven by an HMI, for testing MCU firmware
"""

__version__ = "0.1.0"
