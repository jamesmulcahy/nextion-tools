"""Nextion .zi fonts, as an .HMI project stores them.

A font is a 44-byte header, the font and encoding names, a character map, and one coded glyph per
character:

   0x00  3   magic 04 FF 00
   0x03  1   0x0A in fonts from older Editors, 0x0B in fonts the 2026 Editor stores (see below)
   0x04  2   code page (0x218 utf-8, 3 iso-8859-1)
   0x06  1   fixed character width, 0 = variable
   0x07  1   character height
   0x0C  4   number of characters
   0x10  1   format version (6 in every font here)
   0x11  1   len(font name) + len(encoding name)
   0x20  4   len(font name)
   0x2C      the font name, then the encoding name ("utf-8"), with no separator or terminator

The character map follows the names, 10 bytes per character: u16 code (a UTF-16 code unit), u8
advance width, u8 left overhang, u8 right overhang, u24 offset of the glyph's data from the start
of the map, u16 length of that data.

The overhangs are columns of ink outside the advance width, so a glyph's box is left + width +
right columns wide. Most glyphs have none. Where they exist, the stream codes exactly the wider
box, and the right overhang is on the glyphs whose accent sits to the right (d and t with caron,
which this font draws as an apostrophe).

A glyph's data is a mode byte (0x01 black and white, 0x03 anti-aliased) and a token stream, one
token per byte, that fills the box row by row from the top left:

   00 0xxxxx   x transparent
   00 1xxxxx   x opaque
   01 0xxxxx   x transparent, then 1 opaque
   01 1xxxxx   x transparent, then 2 opaque
   10 xxxccc   x transparent, then 1 pixel of 3-bit coverage ccc
   11 cccddd   2 pixels of 3-bit coverage, ccc then ddd

A stream may stop before the box is full, and the rest of the box is transparent.

A 0x0B font in a project whose display direction is not 0 stores each glyph turned clockwise
by that direction, as the display's own frame holds it (the TFT's pictures are turned the same
way): at 90 the stream fills the box's columns, bottom to top, rather than its rows. That is
from one project, made at 90 (demo/demo.HMI); 0x0A fonts are stored upright at every direction.

The token encoding is from hagronnestad/nextion-font-editor ("Nextion Font Format Specification ZI
version 5"). Where the map starts, and the overhangs, were worked out from this project's fonts.
"""
import struct

import numpy as np

HEADER = 44
MAGIC = b'\x04\xff\x00'
ROTATING_FORMAT = 0x0B  # header byte 3 from which glyphs are stored as the display's frame holds them


class Glyph:
    def __init__(self, code, advance, left, right, coverage, coded):
        self.code = code
        self.advance = advance  # pixels the pen moves on
        self.left = left  # columns of ink drawn left of the pen
        self.right = right  # columns of ink beyond the advance
        self.coverage = coverage  # height x (left + advance + right) uint8, 0 clear .. 255 ink
        self.coded = coded  # pixels the stream coded; fewer than the box when it stops early

    @property
    def char(self):
        return chr(self.code)


class Font:
    def __init__(self, name, encoding, height, version, glyphs, turned=0):
        self.name = name
        self.encoding = encoding
        self.height = height
        self.version = version
        self.turned = turned  # degrees clockwise the glyphs were stored turned
        self.glyphs = glyphs  # code -> Glyph

    def glyph(self, char):
        """The glyph for one character, or None if the font has none."""
        return self.glyphs.get(ord(char))

    def missing(self, text):
        """The characters of text the font cannot draw, in order of first appearance."""
        return ''.join(dict.fromkeys(c for c in text if ord(c) not in self.glyphs))

    def width(self, text):
        """Advance in pixels of text drawn on one line, skipping characters the font lacks."""
        return sum(g.advance for g in map(self.glyph, text) if g is not None)


def decode(data, width, height, turned=0):
    """(coverage, pixels coded) for one glyph's data: coverage is a height x width uint8 array.
    Pixels past the box are dropped; pixels the stream does not reach are clear. `turned` is how
    far clockwise the glyph is stored turned, in degrees; coverage is upright whatever it is."""
    px = []
    for b in data[1:]:
        kind = b >> 6
        if kind == 0:
            px.extend([255 if b & 0x20 else 0] * (b & 0x1f))
        elif kind == 1:
            px.extend([0] * (b & 0x1f))
            px.extend([255] * (2 if b & 0x20 else 1))
        elif kind == 2:
            px.extend([0] * ((b >> 3) & 0x7))
            px.append(_level(b & 0x7))
        else:
            px.append(_level((b >> 3) & 0x7))
            px.append(_level(b & 0x7))
    n = width * height
    out = np.zeros(n, dtype=np.uint8)
    out[:min(n, len(px))] = px[:n]
    if turned % 180:
        width, height = height, width
    return np.ascontiguousarray(np.rot90(out.reshape(height, width), k=turned // 90)), len(px)


def _level(c):
    return c * 255 // 7


def stored_turned(data, direction):
    """Degrees clockwise a .zi's glyphs are stored turned, in a project whose display direction
    is `direction` (Project.direction)."""
    return direction % 360 if data[3] >= ROTATING_FORMAT else 0


def parse(data, turned=0):
    """A Font from the bytes of one .zi whose glyphs are stored `turned` degrees clockwise (see
    stored_turned)."""
    if turned % 90:
        raise ValueError("turned must be a multiple of 90, not %r" % turned)
    if data[:3] != MAGIC:
        raise ValueError('not a .zi font')
    height = data[7]
    count = struct.unpack_from('<I', data, 0x0C)[0]
    names = data[0x11]
    name_length = struct.unpack_from('<I', data, 0x20)[0]
    name = data[HEADER:HEADER + name_length].decode('latin1')
    encoding = data[HEADER + name_length:HEADER + names].decode('latin1')
    table = HEADER + names
    glyphs = {}
    for i in range(count):
        o = table + 10 * i
        code, advance, left, right = struct.unpack_from('<HBBB', data, o)
        offset = int.from_bytes(data[o + 5:o + 8], 'little')
        length = struct.unpack_from('<H', data, o + 8)[0]
        coverage, coded = decode(data[table + offset:table + offset + length], left + advance + right, height,
                                 turned)
        glyphs[code] = Glyph(code, advance, left, right, coverage, coded)
    return Font(name, encoding, height, data[0x10], glyphs, turned)
