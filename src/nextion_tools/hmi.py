"""Read Nextion Editor .HMI projects, and edit numeric component attributes in place.

A project gives its pages, each page's components with their attributes, ids, geometry and event
code, the project's start-up code (Program.s), and its fonts and pictures. Reading needs nothing
beyond the standard library; `Project.fonts()` needs numpy (the `images` extra).

The format, as far as it is used here:

- The file starts with a directory: a u32 entry count, then 28-byte entries of name[16],
  offset:u32, size:u32, flags:u32. An entry whose flags have bit 0 set, or whose name is empty,
  is stale (left over from an earlier save).
- `main.HMI` lists the project's resources in order, from offset 96, 16 bytes each with the
  block's name at +8: pictures (.i), fonts (.zi) and pages (.pa). A resource's id is its
  position among the blocks of its own kind. So a page's id (what `page N` and touch events use)
  and a font's index (what a component's `font` holds) are positions in that list, not the
  numbers in the blocks' names.
- A page block (`N.pa`) has a 56-byte header (u32 stamp, u32 block length, u32 header length
  = 56, u32 component count, ..., name[16] at 24) and a table of (offset, length, _) per
  component, offsets from the end of the header.
- A component is a run of records: u32 length, then a 16-byte attribute name and its value
  (length >= 16), a marker (length < 16), or the end (0). An event marker `codes<event>-N` is
  followed by N records of code, one line each.
- Every component carries an explicit `id` equal to its position on the page. The page itself
  is component 0 (type 121), and the page's own events (load, loadend, unload) are stored there.
- `Program.s` is the start-up code as plain text (CRLF lines): its globals (`int a=0,b=0`), then
  usually `page 0`. It runs once at power-on.

`apply_edits` only changes numeric attributes (same width, so no block moves), keeps endx/endy
in step with x/y/w/h, and re-stamps each edited page.

The Editor has opened edited files that hold stale blocks from earlier saves, and files whose
edited page has a different save ID from the rest. But it once rejected an edit ("Version
Mismatch", then "Loading Page Failed") that left the page byte-identical to its own save, for a
reason not yet known; a Save As, which compacts the file, made the same edit open.

Format notes: https://github.com/vladtsvetkov880-hub/nextion-hmi-format (docs/); what this
relies on was checked against files saved by the Editor.
"""

import re
import struct
from typing import Dict, List, Optional, Tuple, Union

from . import crc

DIRECTORY_ENTRY = 28
PAGE_HEADER = 56
STALE = 1  # bit 0 of a directory entry's flags

# Attributes stored as strings whatever their length; never edited here.
STRING_ATTRS = frozenset({"objname", "txt", "path", "from", "val0", "val1"})
# Derived from x/y/w/h by the Editor; recomputed rather than set.
END_ATTRS = {"endx": ("x", "w"), "endy": ("y", "h")}

# A component's `type`, named as in the Editor's toolbox.
TYPES = {
    1: "slider",
    5: "touchcap",
    51: "timer",
    52: "variable",
    53: "dual-state button",
    54: "number",
    98: "button",
    106: "progress bar",
    109: "hotspot",
    112: "picture",
    113: "crop",
    116: "text",
    121: "page",
}
PAGE_TYPE = 121
SLIDER_TYPE = 1
TOUCHCAP_TYPE = 5
TIMER_TYPE = 51

PROGRAM_BLOCK = "Program.s"

# `sendkey` bits: which touch edges the display reports for the component ("Send Component ID").
# Inferred from NSPanel Manager's firmware, whose handlers act only on release for sendkey 1 and
# on both edges for sendkey 3.
SENDKEY_RELEASE = 1
SENDKEY_PRESS = 2

_EVENT_MARKER = re.compile(rb"codes([a-z]+)-(\d+)")

Value = Union[int, str]
Field = Tuple[int, int]  # (offset in the page block, length)


def _stamp_core(block: bytes) -> int:
    """The Nextion CRC of block[4:] and 10 zero bytes. The stored stamp is this XOR a constant
    that depends only on the block's length, so a same-length edit can take the constant from
    the old stamp."""
    return crc.nextion(block[4:] + bytes(10))


def restamp(old_block: bytes, new_block: bytes) -> bytes:
    """new_block with the stamp the Editor would give it. Lengths must match."""
    if len(old_block) != len(new_block):
        raise ValueError("restamp needs blocks of the same length")
    k = struct.unpack_from("<I", old_block, 0)[0] ^ _stamp_core(old_block)
    out = bytearray(new_block)
    struct.pack_into("<I", out, 0, _stamp_core(out) ^ k)
    return bytes(out)


class Entry:
    """One directory entry: a block's name, where it is, and its flags."""

    def __init__(self, index: int, name: str, offset: int, size: int, flags: int):
        self.index, self.name, self.offset, self.size, self.flags = index, name, offset, size, flags

    @property
    def stale(self) -> bool:
        return bool(self.flags & STALE) or not self.name


def directory(data: bytes) -> List[Entry]:
    count = struct.unpack_from("<I", data, 0)[0]
    entries = []
    for i in range(count):
        o = 4 + DIRECTORY_ENTRY * i
        name = data[o:o + 16].split(b"\0")[0].decode("latin1")
        offset, size, flags = struct.unpack_from("<III", data, o + 16)
        entries.append(Entry(i, name, offset, size, flags))
    return entries


def decode(name: str, raw: bytes) -> Value:
    """An attribute's value from its stored bytes."""
    if name in STRING_ATTRS:
        return raw.split(b"\0")[0].decode("utf-8")
    return int.from_bytes(raw, "little")


class Component:
    def __init__(self, fields: Dict[str, Field], events: Dict[str, List[str]], block: bytes, position: int):
        self.fields = fields  # attribute -> where its value is in the page block
        self.events = events  # event (down, up, slide, timer, load, ...) -> [line of code]
        # As the HMI holds them: the values a page load starts from.
        self.attrs: Dict[str, Value] = {k: decode(k, block[o:o + n]) for k, (o, n) in fields.items()}
        self.name = str(self.attrs.get("objname", ""))
        self.id = int(self.attrs.get("id", position))
        self.type = int(self.attrs.get("type", 0))

    def __repr__(self) -> str:
        return "<Component %d %s (%s)>" % (self.id, self.name, self.kind)

    def get(self, name: str) -> Value:
        return self.attrs[name]

    @property
    def kind(self) -> str:
        return TYPES.get(self.type, "type %d" % self.type)

    @property
    def is_global(self) -> bool:
        """Reachable as page.component from any page (the Editor's vscope "global")."""
        return self.attrs.get("vscope") == 1

    @property
    def reports_press(self) -> bool:
        return bool(self.attrs.get("sendkey", 0) & SENDKEY_PRESS)

    @property
    def reports_release(self) -> bool:
        return bool(self.attrs.get("sendkey", 0) & SENDKEY_RELEASE)

    @property
    def rect(self) -> Optional[Tuple[int, int, int, int]]:
        """(x, y, w, h) on screen, or None for a component with no area (timer, variable,
        touch capture) and for the page itself."""
        if self.type == PAGE_TYPE or not all(k in self.attrs for k in ("x", "y", "w", "h")):
            return None
        a = self.attrs
        return int(a["x"]), int(a["y"]), int(a["w"]), int(a["h"])


class Page:
    def __init__(self, block: bytes, page_id: int, entry: Optional[Entry] = None, where: str = ""):
        self.block = block
        self.id = page_id
        self.entry = entry
        where = where or (entry.name if entry else "page")
        if struct.unpack_from("<I", block, 4)[0] != len(block) or struct.unpack_from("<I", block, 8)[0] != PAGE_HEADER:
            raise ValueError("%s: unexpected page header" % where)
        self.name = block[24:40].split(b"\0")[0].decode("latin1")
        count = struct.unpack_from("<I", block, 12)[0]
        self.components: List[Component] = []
        for i in range(count):
            off, length, _ = struct.unpack_from("<III", block, PAGE_HEADER + 12 * i)
            fields, events = _records(block, PAGE_HEADER + off, length, where)
            self.components.append(Component(fields, events, block, i))
        self._by_name = {c.name: c for c in self.components}

    def __repr__(self) -> str:
        return "<Page %d %s>" % (self.id, self.name)

    def component(self, name: str) -> Component:
        comp = self._by_name.get(name)
        if comp is None:
            raise KeyError("page %s has no component %s" % (self.name, name))
        return comp

    def find(self, name: str) -> Optional[Component]:
        return self._by_name.get(name)

    def by_id(self, component_id: int) -> Optional[Component]:
        if 0 <= component_id < len(self.components):
            return self.components[component_id]
        return None

    @property
    def events(self) -> Dict[str, List[str]]:
        """The page's own events (load, loadend, unload, and touches on the page itself), which
        the Editor stores on the page's component, the one of type 121."""
        return self.components[0].events if self.components else {}

    @property
    def size(self) -> Tuple[int, int]:
        """(w, h) of the screen, from the page component."""
        a = self.components[0].attrs if self.components else {}
        return int(a.get("w", 480)), int(a.get("h", 272))

    @property
    def timers(self) -> List[Component]:
        return [c for c in self.components if c.type == TIMER_TYPE]

    @property
    def touch_capture(self) -> Optional[Component]:
        """The touch capture component that receives every touch on the page. Only the one with
        the lowest id does; the Editor ignores any others."""
        return next((c for c in self.components if c.type == TOUCHCAP_TYPE), None)


def _records(block: bytes, start: int, length: int, where: str):
    """(fields, events) of one component."""
    fields: Dict[str, Field] = {}
    events: Dict[str, List[str]] = {}
    p = start
    code: List[str] = []
    code_lines = 0
    while True:
        n = struct.unpack_from("<I", block, p)[0]
        p += 4
        if n == 0:
            break
        record = block[p:p + n]
        if code_lines:
            code.append(record.decode("utf-8"))
            code_lines -= 1
        else:
            event = _EVENT_MARKER.fullmatch(record)
            if event:
                code = events.setdefault(event.group(1).decode("latin1"), [])
                code_lines = int(event.group(2))
            elif n >= 16:
                fields[record[:16].split(b"\0")[0].decode("latin1")] = (p + 16, n - 16)
        p += n
    if p - start != length:
        raise ValueError("%s: a component at %d did not parse to its recorded length" % (where, start))
    if code_lines:
        raise ValueError("%s: a component at %d ended inside its event code" % (where, start))
    return fields, events


class Project:
    """A .HMI project: its pages by name and by id, and its resources."""

    def __init__(self, data: bytes, path: Optional[str] = None):
        self.data = data
        self.path = path
        self.entries = directory(data)
        self.stale = [e for e in self.entries if e.stale]
        self.live = {e.name: e for e in self.entries if not e.stale}
        if "main.HMI" not in self.live:
            raise ValueError("no main.HMI block: not an HMI project")
        self.pages: List[Page] = []
        for page_id, name in enumerate(n for n in self.resources() if n.endswith(".pa")):
            if name not in self.live:
                raise ValueError("main.HMI lists %s, which the directory does not hold" % name)
            entry = self.live[name]
            self.pages.append(Page(self.block(entry), page_id, entry))
        self._by_name = {p.name: p for p in self.pages}
        program = self.live.get(PROGRAM_BLOCK)
        self.program: List[str] = self.block(program).decode("utf-8").splitlines() if program else []
        self._fonts = None

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, "rb") as f:
            return cls(f.read(), path)

    @classmethod
    def parse(cls, data: bytes, path: Optional[str] = None) -> "Project":
        return cls(data, path)

    def page(self, name: str) -> Page:
        page = self._by_name.get(name)
        if page is None:
            raise KeyError("no page %s (pages: %s)" % (name, ", ".join(sorted(self._by_name))))
        return page

    def find_page(self, name: str) -> Optional[Page]:
        return self._by_name.get(name)

    def page_by_id(self, page_id: int) -> Optional[Page]:
        return self.pages[page_id] if 0 <= page_id < len(self.pages) else None

    def resources(self) -> List[str]:
        """The block names main.HMI lists, in its order."""
        main = self.block(self.live["main.HMI"])
        count = struct.unpack_from("<I", main, 28)[0]
        return [main[96 + 16 * i + 8:96 + 16 * i + 16].split(b"\0")[0].decode("latin1") for i in range(count)]

    @property
    def picture_count(self) -> int:
        return sum(1 for r in self.resources() if r.endswith(".i"))

    @property
    def font_count(self) -> int:
        return sum(1 for r in self.resources() if r.endswith(".zi"))

    def pictures(self) -> List[bytes]:
        """The picture records in id order, each exactly as a compiled TFT stores it (record
        header and data, compressed or not). Each .i block is a header followed by the record."""
        records = []
        for name in self.resources():
            if name.endswith(".i"):
                block = self.block(self.live[name])
                records.append(block[struct.unpack_from("<I", block, 8)[0]:])
        return records

    def font_blocks(self) -> List[Tuple[str, bytes]]:
        """The .zi blocks in font index order, byte for byte (re-importable into the Editor)."""
        return [(name, self.block(self.live[name])) for name in self.resources() if name.endswith(".zi")]

    def fonts(self):
        """The fonts in index order: fonts()[i] is what a component with font=i draws with.
        Needs numpy."""
        if self._fonts is None:
            from . import fonts
            self._fonts = [fonts.parse(block) for _, block in self.font_blocks()]
        return self._fonts

    def block(self, entry: Entry) -> bytes:
        return self.data[entry.offset:entry.offset + entry.size]


def apply_edits(project: Project, edits) -> Tuple[bytes, list]:
    """(new file bytes, [(page, obj, attr, old, new)]) for edits of (page, object, attribute,
    value). Raises ValueError if any edit is not allowed."""
    data = bytearray(project.data)
    changes = []
    by_page: Dict[str, list] = {}
    for page_name, obj, attr, value in edits:
        by_page.setdefault(page_name, []).append((obj, attr, value))
    for page_name, page_edits in by_page.items():
        page = project.page(page_name)
        block = bytearray(page.block)

        def read(comp, attr):
            o, n = comp.fields[attr]
            return decode(attr, bytes(block[o:o + n]))

        touched = []
        for obj, attr, value in page_edits:
            comp = page.component(obj)
            if attr not in comp.fields:
                raise ValueError("%s.%s has no attribute %s" % (page_name, obj, attr))
            if attr in STRING_ATTRS or attr in END_ATTRS:
                raise ValueError("%s.%s.%s cannot be set here" % (page_name, obj, attr))
            o, n = comp.fields[attr]
            if n not in (1, 2, 4) or not 0 <= value < 1 << (8 * n):
                raise ValueError("%s.%s.%s=%d does not fit its %d-byte field" % (page_name, obj, attr, value, n))
            changes.append((page_name, obj, attr, read(comp, attr), value))
            block[o:o + n] = value.to_bytes(n, "little")
            touched.append(comp)
        for comp in touched:
            for end, (start, size) in END_ATTRS.items():
                if end in comp.fields and start in comp.fields and size in comp.fields:
                    o, n = comp.fields[end]
                    old = read(comp, end)
                    new = read(comp, start) + read(comp, size) - 1
                    if old != new:
                        block[o:o + n] = new.to_bytes(n, "little")
                        changes.append((page_name, comp.name, end, old, new))
        e = page.entry
        data[e.offset:e.offset + e.size] = restamp(page.block, bytes(block))
    return bytes(data), changes


def check_written(before: Project, after_path: str, edits) -> None:
    """Re-read the written file: same size, nothing changed outside the edited pages, and every
    edited value reads back."""
    after = Project.load(after_path)
    if len(after.data) != len(before.data):
        raise RuntimeError("written file changed size")
    spans = sorted({(before.page(p).entry.offset, before.page(p).entry.offset + before.page(p).entry.size)
                    for p, _, _, _ in edits})
    pos = 0
    for lo, hi in spans + [(len(before.data), len(before.data))]:
        if before.data[pos:lo] != after.data[pos:lo]:
            raise RuntimeError("bytes outside the edited pages changed")
        pos = hi
    for page_name, obj, attr, value in edits:
        if after.page(page_name).component(obj).attrs[attr] != value:
            raise RuntimeError("%s.%s.%s did not read back as %d" % (page_name, obj, attr, value))
