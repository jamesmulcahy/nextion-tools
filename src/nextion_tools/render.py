"""Draw what a Nextion page shows, from its HMI and the values its components hold.

    from nextion_tools.render import Renderer
    renderer = Renderer(project)
    pixels = renderer.render("home")                  # height x width x 3, uint8
    renderer.render("home", values={"room": {"txt": "Kitchen"}}, hidden={"p_lockceiling"})

Needs numpy and Pillow (the `images` extra). The emulator uses this for Display.snapshot().

What is drawn, per component type, as the Nextion Editor's attributes describe it:

- the page: `sta` 1 a solid `bco`, 2 picture `pic`;
- text, number, button: a background by `sta` (0 the page's picture cropped to the component,
  `picc`; 1 solid `bco`; 2 picture `pic`; 3 none), then `txt` (a number's `val`) in font `font`,
  colour `pco`, aligned by `xcen` / `ycen`, wrapped if `isbr`;
- dual-state button: the same, from `bco2` / `pic2` / `picc2` / `pco2` when `val` is 1; a plain
  button uses those while it is pressed;
- picture `pic`, crop `picc`;
- slider: background by `sta` (from `bco` / `pic` / `picc`), the part below the value from
  `bco1` / `pic1` / `picc1`, and a `wid` x `hig` cursor in `pco` (or picture `pic2` if `psta`
  is 1);
- progress bar: `sta` 0 `bco` with `val` percent in `pco`, 1 pictures `bpic` / `ppic`;
- hotspots, timers, variables and touch captures draw nothing;
- then the drawing instructions since the page was shown, in order: `fill`, `cls`, `pic`,
  `xstr`.

Pixel-exact for pictures and glyphs, which come from the HMI as they are; everything is rounded
to RGB565 as the panel does. Not modelled: anti-aliasing against the background beyond simple
blending, the Editor's compressed pictures (drawn from the file they were imported from instead),
borders (`style`, `borderw`), and component types not listed, which draw nothing.

Text is clipped to its component. A font index that names a font more than twice as tall as its
field (NSPanel's `bootscreen.t_ip` has the 96 px weather icon font in a 30 px field) is taken to
be a mistake: the text is drawn with the largest font that fits, and the field is recorded in
`Renderer.mismatches`.
"""

import io
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image

from .hmi import PAGE_TYPE, SLIDER_TYPE, Component, Page, Project, Value

TEXT_TYPES = frozenset({116, 54, 98, 53})  # text, number, button, dual-state button
PICTURE, CROP, PROGRESS, DUAL, BUTTON, NUMBER = 112, 113, 106, 53, 98, 54

# Colour names a drawing instruction may use instead of a number.
COLOURS = {"BLACK": 0, "WHITE": 65535, "RED": 63488, "BLUE": 31, "GRAY": 33840, "BROWN": 48192,
           "GREEN": 2016, "YELLOW": 65504}


def rgb(colour: int) -> Tuple[int, int, int]:
    """An RGB565 colour as 8-bit RGB."""
    r, g, b = (colour >> 11) & 31, (colour >> 5) & 63, colour & 31
    return r * 255 // 31, g * 255 // 63, b * 255 // 31


def to_565(pixels: np.ndarray) -> np.ndarray:
    """Pixels as the panel shows them: each channel truncated to RGB565, then widened back."""
    p = pixels.astype(np.uint16)
    r, g, b = p[..., 0] >> 3, p[..., 1] >> 2, p[..., 2] >> 3
    return np.stack([r * 255 // 31, g * 255 // 63, b * 255 // 31], axis=-1).astype(np.uint8)


def from_rgb565(data: bytes, width: int, height: int) -> np.ndarray:
    v = np.frombuffer(data, dtype="<u2").reshape(height, width).astype(np.uint32)
    return np.stack([((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31],
                    axis=-1).astype(np.uint8)


class Renderer:
    def __init__(self, project: Project):
        self.project = project
        self._pictures: Dict[int, Optional[np.ndarray]] = {}
        self._fonts = None
        self.mismatches: Set[Tuple[str, str, int]] = set()  # (page, component, font index)

    # ---- resources -----------------------------------------------------------------------------

    def picture(self, picture_id: int) -> Optional[np.ndarray]:
        """Picture `picture_id` as height x width x 3, or None if there is no such picture."""
        if picture_id not in self._pictures:
            try:
                pic = self.project.picture(picture_id)
            except KeyError:
                self._pictures[picture_id] = None
                return None
            if pic.raw:
                pixels = from_rgb565(pic.rgb565, pic.width, pic.height)
            elif pic.original:
                with Image.open(io.BytesIO(pic.original)) as im:
                    pixels = to_565(np.asarray(im.convert("RGB")))
            else:
                pixels = None
            self._pictures[picture_id] = pixels
        return self._pictures[picture_id]

    @property
    def fonts(self):
        if self._fonts is None:
            self._fonts = self.project.fonts()
        return self._fonts

    def _font(self, index: int, height: int, where: Tuple[str, str]):
        fonts = self.fonts
        if not fonts:
            return None
        font = fonts[index] if 0 <= index < len(fonts) else None
        # A font a little taller than its field is clipped, as on the display. One more than
        # twice as tall is almost certainly the wrong index (an icon font in a text field).
        if font is not None and font.height <= 2 * height:
            return font
        self.mismatches.add((where[0], where[1], index))
        fitting = [f for f in fonts if f.height <= height and f.glyph("0") is not None]
        return max(fitting, key=lambda f: f.height) if fitting else font

    # ---- the page -----------------------------------------------------------------------------

    def render(self, page_name: str, values: Optional[Dict[str, Dict[str, Value]]] = None,
               hidden: Iterable[str] = (), pressed: Optional[str] = None,
               drawn: Sequence[str] = ()) -> np.ndarray:
        """The page as height x width x 3. `values` overrides attributes per component (what the
        display holds now; the HMI's values otherwise), `hidden` are components hidden with
        `vis`, `pressed` a component being touched, and `drawn` the drawing instructions run
        since the page was shown."""
        page = self.project.page(page_name)
        values = values or {}
        hidden = set(hidden)
        attrs = {c.name: dict(c.attrs, **values.get(c.name, {})) for c in page.components}
        w, h = page.size
        fb = np.zeros((h, w, 3), dtype=np.uint8)
        background = self._page_background(page, attrs[page.components[0].name], w, h)
        fb[:] = background
        for comp in page.components[1:]:
            if comp.name in hidden or attrs[comp.name].get("vis", 1) == 0:
                continue
            self._component(fb, background, page, comp, attrs[comp.name], comp.name == pressed)
        for instruction in drawn:
            self._instruction(fb, instruction, page)
        return fb

    def _page_background(self, page: Page, a: Dict[str, Value], w: int, h: int) -> np.ndarray:
        bg = np.zeros((h, w, 3), dtype=np.uint8)
        if a.get("sta") == 2:
            self._blit(bg, self.picture(int(a.get("pic", -1))), 0, 0)
        elif a.get("sta") == 1:
            bg[:] = rgb(int(a.get("bco", 0)))
        return bg

    def _component(self, fb, background, page: Page, comp: Component, a: Dict[str, Value], pressed: bool) -> None:
        rect = comp.rect
        if rect is None:
            return
        x, y, w, h = rect
        t = comp.type
        if t == PICTURE:
            self._blit(fb, self.picture(int(a.get("pic", -1))), x, y)
        elif t == CROP:
            self._crop(fb, int(a.get("picc", -1)), x, y, w, h)
        elif t == SLIDER_TYPE:
            self._slider(fb, a, x, y, w, h)
        elif t == PROGRESS:
            self._progress(fb, a, x, y, w, h)
        elif t in TEXT_TYPES:
            second = (t == DUAL and a.get("val") == 1) or (t == BUTTON and pressed)
            suffix = "2" if second else ""
            self._background(fb, a, x, y, w, h, suffix)
            text = _text(t, a)
            if text:
                colour = int(a.get("pco" + suffix, a.get("pco", 0)))
                self._text(fb, text, a, x, y, w, h, colour, (page.name, comp.name))

    def _background(self, fb, a, x, y, w, h, suffix: str = "") -> None:
        sta = a.get("sta", 1)
        if sta == 0:
            self._crop(fb, int(a.get("picc" + suffix, a.get("picc", -1))), x, y, w, h)
        elif sta == 1:
            self._fill(fb, x, y, w, h, int(a.get("bco" + suffix, a.get("bco", 0))))
        elif sta == 2:
            self._blit(fb, self.picture(int(a.get("pic" + suffix, a.get("pic", -1)))), x, y)

    def _slider(self, fb, a, x, y, w, h) -> None:
        lo, hi = int(a.get("minval", 0)), int(a.get("maxval", 100))
        val = min(hi, max(lo, int(a.get("val", lo))))
        frac = (val - lo) / (hi - lo) if hi > lo else 0.0
        vertical = a.get("mode", 0) == 1
        self._background(fb, a, x, y, w, h)
        if vertical:
            filled = round(frac * h)
            self._background_part(fb, a, x, y + h - filled, w, filled, "1")
        else:
            filled = round(frac * w)
            self._background_part(fb, a, x, y, filled, h, "1")
        cw, ch = int(a.get("wid", 0)), int(a.get("hig", 0))
        if cw <= 0 or ch <= 0:
            return
        if vertical:
            cx, cy = x + (w - cw) // 2, y + h - filled - ch // 2
        else:
            cx, cy = x + filled - cw // 2, y + (h - ch) // 2
        cx, cy = min(max(cx, x), x + w - cw), min(max(cy, y), y + h - ch)
        if a.get("psta") == 1:
            self._blit(fb, self.picture(int(a.get("pic2", -1))), cx, cy)
        else:
            self._fill(fb, cx, cy, cw, ch, int(a.get("pco", 0)))

    def _background_part(self, fb, a, x, y, w, h, suffix) -> None:
        """Part of a slider's area drawn from its `1` attributes (the part below the value)."""
        if w <= 0 or h <= 0:
            return
        self._background(fb, a, x, y, w, h, suffix)

    def _progress(self, fb, a, x, y, w, h) -> None:
        val = min(100, max(0, int(a.get("val", 0))))
        vertical = a.get("dez", 0) == 1
        if a.get("sta") == 1:
            self._crop_picture(fb, int(a.get("bpic", -1)), x, y, w, h, 0, 0)
            filled = round(val * (h if vertical else w) / 100)
            if vertical:
                self._crop_picture(fb, int(a.get("ppic", -1)), x, y + h - filled, w, filled, 0, h - filled)
            else:
                self._crop_picture(fb, int(a.get("ppic", -1)), x, y, filled, h, 0, 0)
        else:
            self._fill(fb, x, y, w, h, int(a.get("bco", 0)))
            filled = round(val * (h if vertical else w) / 100)
            if vertical:
                self._fill(fb, x, y + h - filled, w, filled, int(a.get("pco", 0)))
            else:
                self._fill(fb, x, y, filled, h, int(a.get("pco", 0)))

    # ---- text ----------------------------------------------------------------------------------

    def _text(self, fb, text: str, a, x, y, w, h, colour: int, where) -> None:
        font = self._font(int(a.get("font", 0)), h, where)
        if font is None:
            return
        spax, spay = int(a.get("spax", 0)), int(a.get("spay", 0))
        lines = _wrap(font, text, w, spax) if a.get("isbr") else text.split("\r")
        line_h = font.height + spay
        total = line_h * len(lines) - spay
        ycen = a.get("ycen", 0)
        top = y if ycen == 0 else y + (h - total) // 2 if ycen == 1 else y + h - total
        clip = (x, y, x + w, y + h)
        for i, line in enumerate(lines):
            width = _line_width(font, line, spax)
            xcen = a.get("xcen", 0)
            left = x if xcen == 0 else x + (w - width) // 2 if xcen == 1 else x + w - width
            self._draw_line(fb, font, line, left, top + i * line_h, spax, colour, clip)

    def _draw_line(self, fb, font, line, x, y, spax, colour, clip) -> None:
        ink = np.array(rgb(colour), dtype=np.float32)
        pen = x
        for ch in line:
            glyph = font.glyph(ch)
            if glyph is None:
                continue
            gx = pen - glyph.left
            cov = glyph.coverage.astype(np.float32) / 255.0
            self._blend(fb, cov, ink, gx, y, clip)
            pen += glyph.advance + spax

    @staticmethod
    def _blend(fb, cov, ink, x, y, clip) -> None:
        cx0, cy0, cx1, cy1 = clip
        h, w = cov.shape
        x0, y0, x1, y1 = max(x, cx0, 0), max(y, cy0, 0), min(x + w, cx1, fb.shape[1]), min(y + h, cy1, fb.shape[0])
        if x0 >= x1 or y0 >= y1:
            return
        c = cov[y0 - y:y1 - y, x0 - x:x1 - x, None]
        region = fb[y0:y1, x0:x1].astype(np.float32)
        fb[y0:y1, x0:x1] = (region * (1 - c) + ink * c).astype(np.uint8)

    # ---- primitives ----------------------------------------------------------------------------

    @staticmethod
    def _fill(fb, x, y, w, h, colour: int) -> None:
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, fb.shape[1]), min(y + h, fb.shape[0])
        if x0 < x1 and y0 < y1:
            fb[y0:y1, x0:x1] = rgb(colour)

    @staticmethod
    def _blit(fb, pixels: Optional[np.ndarray], x: int, y: int) -> None:
        if pixels is None:
            return
        h, w = pixels.shape[:2]
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, fb.shape[1]), min(y + h, fb.shape[0])
        if x0 < x1 and y0 < y1:
            fb[y0:y1, x0:x1] = pixels[y0 - y:y1 - y, x0 - x:x1 - x]

    def _crop(self, fb, picture_id: int, x, y, w, h) -> None:
        """A crop picture: the part of a (full-screen) picture under the component."""
        self._crop_picture(fb, picture_id, x, y, w, h, x, y)

    def _crop_picture(self, fb, picture_id: int, x, y, w, h, sx, sy) -> None:
        """Draw at (x, y) the w x h part of a picture that starts at (sx, sy) in it."""
        pixels = self.picture(picture_id)
        if pixels is None or w <= 0 or h <= 0:
            return
        part = pixels[max(sy, 0):sy + h, max(sx, 0):sx + w]
        self._blit(fb, part, x, y)

    def _instruction(self, fb, text: str, page: Page) -> None:
        keyword, _, args = text.partition(" ")
        parts = _split_args(args)
        try:
            if keyword == "fill" and len(parts) == 5:
                x, y, w, h = (int(v) for v in parts[:4])
                self._fill(fb, x, y, w, h, _colour(parts[4]))
            elif keyword == "cls" and len(parts) == 1:
                fb[:] = rgb(_colour(parts[0]))
            elif keyword == "pic" and len(parts) == 3:
                self._blit(fb, self.picture(int(parts[2])), int(parts[0]), int(parts[1]))
            elif keyword == "xstr" and len(parts) >= 11:
                x, y, w, h, font = (int(v) for v in parts[:5])
                a = {"font": font, "xcen": int(parts[7]), "ycen": int(parts[8]), "isbr": 0}
                sta = int(parts[9])
                if sta == 1:
                    self._fill(fb, x, y, w, h, _colour(parts[6]))
                elif sta == 0:
                    self._crop(fb, int(parts[6]), x, y, w, h)
                elif sta == 2:
                    self._blit(fb, self.picture(int(parts[6])), x, y)
                text_arg = parts[10]
                if len(text_arg) >= 2 and text_arg[0] == '"' and text_arg[-1] == '"':
                    text_arg = text_arg[1:-1]
                self._text(fb, text_arg, a, x, y, w, h, _colour(parts[5]), (page.name, "xstr"))
        except ValueError:
            pass  # an instruction the display would have rejected; it drew nothing


def _text(type_: int, a: Dict[str, Value]) -> str:
    if type_ == NUMBER:
        val = int(a.get("val", 0))
        if val >= 1 << 31:
            val -= 1 << 32
        fmt = a.get("format", 0)
        s = "%X" % val if fmt == 2 else str(val)
        length = int(a.get("lenth", 0))
        return s.zfill(length) if length else s
    text = str(a.get("txt", ""))
    if a.get("pw") == 1:
        return "*" * len(text)
    return text


def _line_width(font, line: str, spax: int) -> int:
    glyphs = [g for g in map(font.glyph, line) if g is not None]
    return sum(g.advance for g in glyphs) + spax * max(0, len(glyphs) - 1)


def _wrap(font, text: str, width: int, spax: int) -> List[str]:
    """Lines of text that fit `width`, broken at spaces where possible."""
    lines: List[str] = []
    for paragraph in text.split("\r"):
        line = ""
        for word in paragraph.split(" "):
            candidate = word if not line else line + " " + word
            if _line_width(font, candidate, spax) <= width or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
            while _line_width(font, line, spax) > width and len(line) > 1:
                cut = len(line)
                while cut > 1 and _line_width(font, line[:cut], spax) > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line)
    return lines


def _split_args(args: str) -> List[str]:
    """Comma-separated arguments, with commas inside a quoted string kept."""
    out, cur, quoted = [], "", False
    for ch in args:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip() or out:
        out.append(cur.strip())
    return out


def _colour(text: str) -> int:
    text = text.strip()
    return COLOURS[text] if text in COLOURS else int(text)


# ---- orientation ---------------------------------------------------------------------------------
#
# A page's coordinates are the view of someone looking at a panel mounted as its TFT intends:
# NSPanel's US pages are 320 x 480, its EU pages 480 x 320, and touch coordinates are in the same
# frame. So pictures are upright by default. `rotation` (degrees clockwise) turns them, for a view
# of the panel as it actually hangs, or as the LCD itself sits (see native_rotation).


def rotate(pixels: np.ndarray, rotation: int) -> np.ndarray:
    """pixels turned `rotation` degrees clockwise (0, 90, 180 or 270)."""
    if rotation % 90:
        raise ValueError("rotation must be a multiple of 90, not %r" % rotation)
    return np.ascontiguousarray(np.rot90(pixels, k=-(rotation // 90) % 4))


def rotated_size(size: Tuple[int, int], rotation: int) -> Tuple[int, int]:
    w, h = size
    return (h, w) if rotation % 180 else (w, h)


def unrotate_point(x: int, y: int, rotation: int, size: Tuple[int, int]) -> Tuple[int, int]:
    """The page coordinates of point (x, y) of a picture turned `rotation` degrees clockwise.
    `size` is the page's (w, h)."""
    w, h = size
    r = rotation % 360
    if r == 0:
        return x, y
    if r == 90:
        return y, h - 1 - x
    if r == 180:
        return w - 1 - x, h - 1 - y
    if r == 270:
        return w - 1 - y, x
    raise ValueError("rotation must be a multiple of 90, not %r" % rotation)


def native_rotation(tft_orientation: int) -> int:
    """The rotation that shows a page as the LCD's own frame holds it, from the orientation a TFT
    records (nextion_tools.tft.orientation): the display turns its frame by that much to draw
    the page upright, so turning the page back undoes it. Which way the display turns (clockwise
    or not) is assumed, not checked on hardware; a 90 / 270 view may come out upside down."""
    return (-tft_orientation) % 360


def save_png(pixels: np.ndarray, path: str) -> None:
    Image.fromarray(pixels).save(path, "PNG")
