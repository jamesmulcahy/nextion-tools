"""An emulated Nextion display: what the MCU sends goes in, replies and user input come out.

The display keeps a model of what the screen holds, built from the instructions it executes:
the current page, every component attribute assigned (`components["page.comp.attr"]`), and system
variables (`system["dim"]`). It replies as a real display does (see protocol.py for what was
measured), and lets a test act as the user: touches, slider moves.

Two speeds:

- **Ideal** (the default): every instruction executes as soon as it arrives. Nothing is ever
  lost, so tests see exactly what the MCU meant to show.
- **Flow model** (`flow_model = True`): input arrives at line rate into a ~1 KB buffer, and the
  display stalls on page changes, `ref` and `delay`, as a real one does. A burst that overruns
  the buffer during a stall loses commands, and the display reports it with 0x24. See flow.py.

Component keys are always fully qualified, `page.component.attr`. An unqualified name
(`b_scenes.pic=59`) is taken to be on the current page, which is how the display resolves it.

Given the project's HMI (`Display(hmi=Project.load("gui.HMI"))`), the display knows its pages and
components. A name that does not exist where it is used fails with 0x1A (see model.py), page
and component ids are the HMI's, `get` answers the HMI's value for anything not yet assigned,
and loading a page resets its local components, as on a real display. Without an HMI any name
is accepted and page ids are handed out in the order pages are first seen.

With an HMI the display also runs the TFT's own code (script.py): Program.s at boot, each page's
load, loadend and unload, the current page's timers, and what a touch triggers. A touch is a
finger on the screen (`touch_down(x, y)`, `touch_move`, `touch_up`, or `press(name)` at a
component): the display finds the component under it, sends the 0x65 events its `sendkey`
asks for, and runs its code and then the page's touch capture's. So which edges a button
reports, the sleep frame a timer prints and the gestures a page decodes all come from the HMI.
`scripts=False` turns this off, and touches are then bare 0x65 frames as without an HMI.

With `sendxy=1` every press and release is also reported with its coordinates (0x67), and with
`sleep=1` the screen goes dark and touches reach nothing: with `thup=1` the first one wakes the
display (0x87) and is used up doing it. Those follow Nextion's instruction set; neither is
measured, and whether a drag streams 0x67 frames is not known (they are sent only on press and
release here).
"""

import collections
import logging
import re
import struct
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple, Union

from . import protocol
from .devices import NX4832T035, Device
from .flow import DEFAULT_BAUD, FlowModel
from ..hmi import SLIDER_TYPE, Component, Page, Project
from .model import Screen
from .protocol import TERMINATOR, Value
from .script import PROGRAM, Compiled, Host, Interpreter, ScriptError, Stmt, UnsupportedCode, compile_hmi
from .transport import NotConnected, TcpServer, Transport
from .upload import UPLOAD, UploadMixin
from .waiting import wait_for

log = logging.getLogger("nextion_tools.emu")

# The NSPanel's display, the only model measured so far.
COMOK = NX4832T035.comok
REDRAW_S = NX4832T035.redraw_s

_DELAY = re.compile(r"^delay=(\d+)$")

# Instructions that draw straight onto the screen, kept for snapshot() until the page changes.
DRAWING = frozenset({"fill", "cls", "pic", "xstr", "picq", "xpic", "line", "draw", "cir", "cirs"})

# How deep event code may nest (a load that changes page, whose load changes page, ...).
MAX_EVENT_DEPTH = 8

Resolver = Callable[[str], bool]
Override = Callable[["Display"], None]


class Display(UploadMixin):
    def __init__(self, transport: Optional[Transport] = None, *, flow_model: bool = False,
                 device: Device = NX4832T035, baud: int = DEFAULT_BAUD,
                 buffer_capacity: Optional[int] = None, redraw_s: Optional[float] = None,
                 comok: Optional[bytes] = None, banner: bytes = b"",
                 startup_frames: bool = False, flash_size: Optional[int] = None,
                 resolver: Optional[Resolver] = None, hmi: Optional[Project] = None,
                 scripts: bool = True, overrides: Optional[Dict[str, Override]] = None,
                 rotation: int = 0,
                 clock: Callable[[], float] = time.monotonic):
        # The device's measurements, each of which a keyword argument may override.
        self.device = device
        buffer_capacity = device.buffer_capacity if buffer_capacity is None else buffer_capacity
        redraw_s = device.redraw_s if redraw_s is None else redraw_s
        comok = device.comok if comok is None else comok
        flash_size = device.flash_size if flash_size is None else flash_size
        self.transport = transport if transport is not None else TcpServer()
        self._clock = clock
        self._cond = threading.Condition()
        self._closed = False

        self.screen: Optional[Screen] = Screen(hmi) if hmi is not None else None
        self.page: Optional[str] = self.screen.first_page if self.screen else None
        self.page_history: List[str] = []
        self.components: Dict[str, Value] = {}
        self.system: Dict[str, Value] = {}  # dim, sleep, thsp, ...
        self.commands: List[str] = []  # every instruction executed, in order
        self.errors: List[Tuple[str, int]] = []  # (instruction, code) for every one that failed
        # Drawing instructions (fill, xstr, ...) since the page was shown, for snapshot().
        self.drawn: List[str] = []
        # Goes up whenever what the screen shows may have changed; a viewer redraws when it does.
        self.version = 0
        self._renderer = None
        # Degrees clockwise to turn snapshots (and the viewer); 0 shows pages upright, in the
        # HMI's own coordinates. See render.py.
        if rotation % 90:
            raise ValueError("rotation must be a multiple of 90, not %r" % rotation)
        self.rotation = rotation % 360
        self.bkcmd = protocol.BKCMD_DEFAULT
        # Called while waiting; raises to abort the wait (e.g. when the emulated MCU crashed).
        self.abort_check: Optional[Callable[[], None]] = None

        self.comok = comok
        self.banner = banner  # raw bytes the TFT prints when it starts, terminator included, if any
        self.startup_frames = startup_frames  # 00 00 00 and 88 at power-on, per Nextion's docs
        self.redraw_s = redraw_s
        self.command_cost_s = 0.0  # extra time every instruction takes, in the flow model
        # resolver(key) says whether a name exists: a page name, "page.comp.attr", or a system
        # variable. It overrides the HMI's answer; with neither, every name exists. A name that
        # does not exist fails with 0x1A, as on a real display.
        self.resolver = resolver
        self.power_cycles = 0

        # Page ids are what touch events carry. Without the HMI they are handed out in the order
        # pages are first seen.
        self._page_ids: Dict[str, int] = {}

        # The TFT's code. An override ("page.component.event", or "Program.s") runs instead of
        # the HMI's code for that event; overridden events are not compiled, so an override can
        # stand in for code the interpreter does not support.
        self.overrides: Dict[str, Override] = dict(overrides or {})
        self.globals: Dict[str, int] = {}  # Program.s's `int` variables
        self.script_errors: List[Tuple[str, str, int]] = []  # (event, statement, code)
        self._code: Optional[Compiled] = None
        if hmi is not None and scripts:
            self._code = compile_hmi(hmi, skip=lambda where: where in self.overrides)
            if self._code.errors:
                raise UnsupportedCode(self._code.errors)
            for where in sorted(self.overrides):
                log.info("override: %s runs Python instead of the HMI's code", where)
        self._interpreter = Interpreter(_ScriptHost(self))
        self._depth = 0
        self._running: List[str] = []  # the events being run, innermost last
        self._timer_due: Dict[str, float] = {}  # "page.timer" -> when it next fires
        self._touch: Optional[_Touch] = None

        self.flow = FlowModel(self._execute, self._overflowed, baud=baud, capacity=buffer_capacity)
        self._flow_model = flow_model
        self._rx = b""  # ideal mode: bytes not yet terminated

        self._init_upload(flash_size)

        self._outbox: Deque[Tuple[float, bytes, object]] = collections.deque()
        self._out_cond = threading.Condition()
        self._last_send_at = 0.0

        threading.Thread(target=self._write_loop, name="nextion-tx", daemon=True).start()
        threading.Thread(target=self._cpu_loop, name="nextion-cpu", daemon=True).start()
        self.transport.start(self._on_data, on_disconnect=self._on_disconnect)

    # ---- flow model settings -------------------------------------------------

    @property
    def flow_model(self) -> bool:
        return self._flow_model

    @flow_model.setter
    def flow_model(self, on: bool) -> None:
        with self._cond:
            if self._flow_model and not on:
                # Whatever the model still holds is executed at once.
                self._flow_model = False
                self._ingest(self.flow.take_all())
            self._flow_model = on
            self._cond.notify_all()

    @property
    def buffer_capacity(self) -> int:
        return self.flow.capacity

    @buffer_capacity.setter
    def buffer_capacity(self, value: int) -> None:
        with self._cond:
            self.flow.capacity = value

    @property
    def overflows(self) -> int:
        return self.flow.overflows

    @property
    def max_buffered(self) -> int:
        return self.flow.max_buffered

    @property
    def lost(self) -> List[bytes]:
        """Instructions the flow model dropped because the input buffer was full."""
        return self.flow.lost

    # ---- user input ------------------------------------------------------------

    @property
    def runs_scripts(self) -> bool:
        """Whether touches go through the HMI's code (an HMI was given, and scripts=True)."""
        return self._code is not None

    def send_touch_event(self, component: Union[int, str], pressed: bool, page: Optional[str] = None) -> None:
        """A bare 0x65 frame, whatever the HMI would have sent. `component` is an id, or with an
        HMI, a name on `page` (the current page by default)."""
        page = page or self.page or ""
        component_id = self.component_id(component, page)
        self.send_frame(protocol.TOUCH_EVENT, bytes([self.page_id(page), component_id, 1 if pressed else 0]))

    def touch_down(self, x: int, y: int) -> None:
        """A finger lands on the screen at (x, y)."""
        with self._cond:
            if self._touch is not None:
                raise RuntimeError("already touching at %r" % ((self._touch.x, self._touch.y),))
            page = self._current_page()
            if self._asleep():
                # Nothing on the page sees it, nor its moves and release. It wakes the display
                # if thup asks for that.
                self._touch = _Touch(page.name, None, x, y)
                self._report_xy(x, y, True, asleep=True)
                if self.system.get("thup") == 1:
                    self._store("sleep", 0)
                    self._reply(protocol.frame(protocol.AUTO_WAKE))
                self._cond.notify_all()
                return
            comp = self._hit(page, x, y)
            self._touch = _Touch(page.name, comp, x, y)
            self.version += 1
            self._set_touch_point(x, y, x, y)
            self._report_xy(x, y, True)
            log.debug("touch down at (%d, %d): %s.%s", x, y, page.name, comp.name)
            if comp.type == SLIDER_TYPE:
                self._slide_to(page, comp, x, y)
            self._touch_edge(page, comp, True)
            self._cond.notify_all()

    def touch_move(self, x: int, y: int) -> None:
        """The finger moves to (x, y). A slider it pressed follows it and runs its slide code."""
        with self._cond:
            touch = self._held()
            if touch is None or touch.component is None:
                return
            touch.x, touch.y = x, y
            self._set_touch_point(x, y, touch.x0, touch.y0)
            if touch.component.type == SLIDER_TYPE:
                page = self._current_page()
                if self._slide_to(page, touch.component, x, y):
                    self._run_event(page, touch.component, "slide")
            self._cond.notify_all()

    def touch_up(self) -> None:
        """The finger lifts. The release goes to what it pressed, wherever it is now."""
        with self._cond:
            touch, self._touch = self._held(), None
            self.version += 1
            if touch is None:
                return
            if touch.component is None:
                self._report_xy(touch.x, touch.y, False, asleep=True)
                self._cond.notify_all()
                return
            self._set_touch_point(0, 0, touch.x0, touch.y0)
            self._report_xy(touch.x, touch.y, False)
            self._touch_edge(self._current_page(), touch.component, False)
            self._cond.notify_all()

    def tap(self, x: int, y: int, hold: float = 0.1) -> None:
        self.touch_down(x, y)
        time.sleep(hold)
        self.touch_up()

    def drag(self, start: Tuple[int, int], end: Tuple[int, int], duration: float = 0.3, steps: int = 10,
             hold: float = 0.05) -> None:
        """Press at `start`, move to `end` over `duration`, hold there for `hold`, then lift."""
        self.touch_down(*start)
        for i in range(1, steps + 1):
            time.sleep(duration / steps)
            self.touch_move(start[0] + (end[0] - start[0]) * i // steps, start[1] + (end[1] - start[1]) * i // steps)
        time.sleep(hold)
        self.touch_up()

    def swipe(self, direction: str, distance: int = 160, duration: float = 0.2) -> None:
        """A swipe across the middle of the screen: "left", "right", "up" or "down"."""
        with self._cond:
            w, h = self._current_page().size
        cx, cy = w // 2, h // 2
        dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
        half = distance // 2
        self.drag((cx - dx * half, cy - dy * half), (cx + dx * half, cy + dy * half), duration)

    def touch(self, component: Union[int, str], pressed: bool, page: Optional[str] = None) -> None:
        """Press or release a component. With the HMI's code running, a finger goes down on it or
        lifts; otherwise this sends the bare 0x65 frame (send_touch_event)."""
        if not self.runs_scripts:
            self.send_touch_event(component, pressed, page)
        elif pressed:
            self.touch_down(*self.point_on(component, page))
        else:
            self.touch_up()

    def press(self, component: Union[int, str], hold: float = 0.1, page: Optional[str] = None) -> None:
        """A tap on a component: press, hold, release. With the HMI's code running, what the
        display reports is up to the HMI, as on hardware. The gesture code on most NSPanel pages
        needs the finger down for at least one 50 ms tick of its timer to see a press at all."""
        self.touch(component, True, page)
        time.sleep(hold)
        self.touch(component, False, page)

    def long_press(self, component: Union[int, str], hold: float, page: Optional[str] = None) -> None:
        self.press(component, hold, page)

    def set_value(self, name: str, value: Value) -> None:
        """Set what `get <name>.val` returns, without touching anything. `name` is as the MCU
        asks for it, e.g. "home.s_brightness"."""
        with self._cond:
            self.components[self._qualify(name + ".val")] = value
            self._cond.notify_all()

    def slide(self, component: Union[int, str], value: int, page: Optional[str] = None,
              component_id: Optional[int] = None) -> int:
        """Move a slider to `value` and let go. With the HMI's code running, this is a finger on
        the slider at the point for that value, and returns the value the slider ended on (a
        slider narrower than its range cannot reach every value). Otherwise `component` is the
        name the MCU reads ("home.s_brightness"), `component_id` its id, and this sets the value
        and sends the release frame."""
        if not self.runs_scripts:
            self.set_value(str(component), value)
            self.send_touch_event(component_id if component_id is not None else component, False, page)
            return value
        with self._cond:
            pg = self._current_page()
            if page is not None and page != pg.name:
                raise RuntimeError("cannot touch %r: the display shows %r" % (page, pg.name))
            comp = self._component(pg, component)
            if comp.type != SLIDER_TYPE:
                raise ValueError("%s is not a slider" % comp.name)
            x, y = _slider_point(comp, value)
        self.touch_down(x, y)
        time.sleep(0.05)
        self.touch_move(x, y)
        time.sleep(0.05)
        self.touch_up()
        return int(self.value("%s.%s.val" % (pg.name, comp.name)))

    def point_on(self, component: Union[int, str], page: Optional[str] = None) -> Tuple[int, int]:
        """A point where a touch lands on `component` of the current page: its centre, or if
        something drawn above covers that, the nearest point that is not covered."""
        with self._cond:
            pg = self._current_page()
            if page is not None and page != pg.name:
                raise RuntimeError("cannot touch %r: the display shows %r" % (page, pg.name))
            comp = self._component(pg, component)
            rect = comp.rect
            if rect is None:
                raise ValueError("%s.%s has no area to touch" % (pg.name, comp.name))
            if not self._visible(pg, comp):
                raise ValueError("%s.%s is hidden" % (pg.name, comp.name))
            x, y, w, h = rect
            cx, cy = x + w // 2, y + h // 2
            points = sorted(((px, py) for px in range(x, x + w, max(1, w // 16)) for py in range(y, y + h, max(1, h // 16))),
                            key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
            for px, py in [(cx, cy)] + points:
                if self._hit(pg, px, py) is comp:
                    return px, py
            raise ValueError("%s.%s is covered everywhere by components above it" % (pg.name, comp.name))

    def expire_timer(self, name: str) -> None:
        """Let a timer of the current page run out now, as if its `tim` had elapsed."""
        with self._cond:
            page = self._current_page()
            comp = self._component(page, name)
            key = "%s.%s" % (page.name, comp.name)
            if not self._value(key + ".en"):
                raise RuntimeError("%s is not running" % key)
            self._timer_due[key] = self._clock()
            self._run_timers(self._clock())
            self._cond.notify_all()

    def tick(self) -> None:
        """Run whatever is due now. Only needed with a clock that a test advances by hand, which
        the display's own thread cannot wait on."""
        with self._cond:
            now = self._clock()
            if self.flow_model:
                self.flow.run(now)
            self._run_timers(now)
            self._cond.notify_all()

    def send_frame(self, code: int, payload: bytes = b"") -> None:
        """Send code + payload + FF FF FF, as the display or its TFT's code would."""
        self.send_raw(protocol.frame(code, payload))

    def send_raw(self, data: bytes) -> None:
        """Send bytes as they are (what `printh` in the TFT's code does)."""
        if not self.transport.connected:
            raise NotConnected("the MCU side is not connected")
        self._reply(data)

    def boot(self) -> None:
        """The TFT starts: the startup frames, if enabled, and the banner. A display whose TFT is
        broken shows SYSTEM DATA ERROR instead and sends nothing."""
        if self.display_state != "ok":
            log.info("display boot: %s, nothing sent", self.display_state)
            return
        if self.startup_frames:
            self._reply(protocol.frame(protocol.STARTUP, b"\x00\x00"))
            self._reply(protocol.frame(protocol.READY))
        if self.banner:
            self._reply(self.banner)
        if self._code is not None:
            # Program.s: the globals, then usually `page 0`, whose load code may print a banner.
            with self._cond:
                self._run_code(PROGRAM, self._code.program)
                self._cond.notify_all()

    def power_cycle(self) -> None:
        """The display was switched off and on: everything it held is gone, an upload in progress
        is lost, and bkcmd is back to its default."""
        with self._cond:
            self.power_cycles += 1
            self._abandon_upload()
            self._reset()
            self._cond.notify_all()

    # ---- observation -------------------------------------------------------------

    def get(self, key: str, default: Optional[Value] = None) -> Optional[Value]:
        """What the MCU assigned to a component attribute (not the HMI's value; see value())."""
        with self._cond:
            return self.components.get(self._qualify(key), default)

    def value(self, key: str) -> Optional[Value]:
        """What the display holds: what was assigned, else the HMI's value, as `get` answers."""
        with self._cond:
            return self._value(self._qualify(key) if "." in key else key)

    def wait_for(self, predicate: Callable[["Display"], bool], timeout: float = 30, what: str = "condition") -> None:
        with self._cond:
            if not wait_for(self._cond, lambda: predicate(self), timeout, self.abort_check):
                raise TimeoutError("display: %s not met within %.0fs (page=%r)" % (what, timeout, self.page))

    def wait_for_page(self, page: str, timeout: float = 30) -> None:
        self.wait_for(lambda n: n.page == page, timeout, "page %r" % page)

    def wait_for_value(self, key: str, expected: Value, timeout: float = 30) -> None:
        key = self._qualify(key)
        self.wait_for(lambda n: n.components.get(key) == expected, timeout,
                      "%s == %r (is %r)" % (key, expected, self.components.get(key)))

    def wait_for_command(self, pattern: str, timeout: float = 30, since: int = 0) -> str:
        rx = re.compile(pattern)
        found: List[str] = []

        def match(n):
            for c in n.commands[since:]:
                if rx.search(c):
                    found.append(c)
                    return True
            return False

        self.wait_for(match, timeout, "command matching %r" % pattern)
        return found[0]

    def wait_until_idle(self, timeout: float = 30) -> None:
        """Until everything received has been executed and every reply sent."""
        self.wait_for(lambda n: (not n.flow_model or n.flow.idle) and not n._outbox_busy(), timeout, "display idle")

    # ---- what the screen shows ---------------------------------------------------------------

    def scene(self) -> Dict[str, object]:
        """What snapshot() draws, copied under the lock: the page, every attribute assigned on
        it, the component being pressed, and the drawing instructions since it was shown."""
        with self._cond:
            if self.screen is None or self.page is None:
                raise RuntimeError("a snapshot needs an HMI")
            prefix = self.page + "."
            values: Dict[str, Dict[str, Value]] = {}
            for key, value in self.components.items():
                if key.startswith(prefix):
                    comp, _, attr = key[len(prefix):].rpartition(".")
                    values.setdefault(comp, {})[attr] = value
            touch = self._held()
            pressed = touch.component.name if touch is not None and touch.component is not None else None
            return {"page": self.page, "values": values, "drawn": list(self.drawn), "version": self.version,
                    "pressed": pressed, "asleep": self._asleep()}

    def snapshot(self, scene: Optional[Dict[str, object]] = None, rotation: Optional[int] = None):
        """What the screen shows now, as a height x width x 3 numpy array, turned by `rotation`
        (the display's own by default): black while the display sleeps. Needs numpy and Pillow
        (nextion-tools[images])."""
        import numpy as np
        from ..render import Renderer, rotate
        scene = scene or self.scene()
        if self._renderer is None:
            self._renderer = Renderer(self.screen.hmi)
        if scene.get("asleep"):
            w, h = self.screen.hmi.page(scene["page"]).size
            pixels = np.zeros((h, w, 3), dtype=np.uint8)
        else:
            pixels = self._renderer.render(scene["page"], values=scene["values"], pressed=scene["pressed"],
                                           drawn=scene["drawn"])
        return rotate(pixels, self.rotation if rotation is None else rotation)

    def save_png(self, path: str) -> str:
        """Save what the screen shows as a PNG. Returns the path."""
        from ..render import save_png
        save_png(self.snapshot(), path)
        return path

    def mark(self) -> int:
        with self._cond:
            return len(self.commands)

    def page_id(self, page: str) -> int:
        with self._cond:
            if self.screen is not None:
                found = self.screen.page(page)
                if found is None:
                    raise KeyError("the HMI has no page %r" % page)
                return found.id
            return self._page_ids.setdefault(page, len(self._page_ids))

    def component_id(self, component: Union[int, str], page: str) -> int:
        if isinstance(component, int):
            return component
        if self.screen is None:
            raise KeyError("component names need an HMI; pass its id")
        found = self.screen.page(page)
        comp = found.find(component) if found else None
        if comp is None:
            raise KeyError("page %r has no component %r" % (page, component))
        return comp.id

    @property
    def connected(self) -> bool:
        return self.transport.connected

    def close(self) -> None:
        self._closed = True
        self.transport.close()
        with self._cond:
            self._cond.notify_all()
        with self._out_cond:
            self._out_cond.notify_all()

    # ---- receiving -----------------------------------------------------------------

    def _on_data(self, data: bytes) -> None:
        with self._cond:
            self._ingest(data)
            self._cond.notify_all()

    def _on_disconnect(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _ingest(self, data: bytes) -> None:
        """Called with _cond held."""
        while data:
            if self.upload is not None:
                # TFT bytes may contain FF FF FF; nothing is an instruction until the upload ends.
                data = self._receive_upload(data)
                continue
            if self.flow_model:
                self.flow.receive(data, self._clock())
                return
            self._rx += data
            data = b""
            while self.upload is None and TERMINATOR in self._rx:
                raw, self._rx = self._rx.split(TERMINATOR, 1)
                stall, after = self._execute(raw, self._clock())
                if after is not None:
                    after(self._clock())
            if self.upload is not None:
                data, self._rx = self._rx, b""

    def _cpu_loop(self) -> None:
        """Runs the flow model in real time."""
        with self._cond:
            while not self._closed:
                timeout = None
                due = self._run_timers(self._clock())
                if due is not None:
                    timeout = max(0.0, due - self._clock())
                if self.flow_model:
                    now = self._clock()
                    self.flow.run(now)
                    if self.upload is not None:
                        # An upload began: what followed its instruction is TFT data.
                        rest = self.flow.take_all()
                        if rest:
                            self._ingest(rest)
                    nxt = self.flow.next_event()
                    if nxt is not None:
                        wait = max(0.0, nxt - self._clock())
                        timeout = wait if timeout is None else min(timeout, wait)
                    self._cond.notify_all()
                self._cond.wait(timeout if timeout is None or timeout > 0 else 0.0001)

    def _overflowed(self, t: float) -> None:
        log.warning("input buffer overflowed; %d commands lost so far", len(self.flow.lost))
        self._reply(protocol.frame(protocol.BUFFER_OVERFLOW))

    def _reset(self) -> None:
        """Called with _cond held."""
        self.bkcmd = protocol.BKCMD_DEFAULT
        self.page = self.screen.first_page if self.screen else None
        self.components.clear()
        self.system.clear()
        self.globals.clear()
        self._timer_due.clear()
        self._touch = None
        self.drawn.clear()
        self.version += 1
        self.flow.take_all()
        self.flow.paused = False
        self._rx = b""

    def _upload_finished(self) -> None:
        self.flow.paused = False

    # ---- executing -------------------------------------------------------------------

    def _execute(self, raw: bytes, t: float) -> Tuple[float, Optional[Callable[[float], None]]]:
        """Execute one instruction (called with _cond held). Returns how long the display stalls
        afterwards, and what to do when it resumes."""
        text = raw.decode("utf-8", errors="replace")
        self.commands.append(text)
        self.version += 1
        log.debug("<- %s", text)
        try:
            result, stall, deferred = self._apply(protocol.parse(text))
        except Exception:
            log.exception("failed to interpret %r", text)
            result, stall, deferred = protocol.INVALID, 0.0, False
        stall += self.command_cost_s

        def report(_t=None):
            if result is None:
                return
            if result == protocol.CMD_OK:
                if protocol.reports_success(self.bkcmd):
                    self._reply(protocol.frame(protocol.CMD_OK))
            else:
                self.errors.append((text, result))
                if protocol.reports_failure(self.bkcmd):
                    self._reply(protocol.frame(result))

        if deferred:
            return stall, report
        report()
        return stall, None

    def _apply(self, ins: protocol.Instruction) -> Tuple[Optional[int], float, bool]:
        """(result code or None for no result, stall seconds, result only after the stall)."""
        text = ins.text
        if text == "connect":
            self._reply(self.comok + TERMINATOR)
            return None, 0.0, False
        m = UPLOAD.match(text)
        if m:
            # Stop executing: what follows is TFT data, which _cpu_loop hands to the upload.
            self.flow.paused = True
            self._start_upload(m)
            if self.upload is None:
                self.flow.paused = False
            return None, 0.0, False
        m = _DELAY.match(text)
        if m:
            return protocol.CMD_OK, int(m.group(1)) / 1000.0, True
        if ins.kind == "invalid":
            return protocol.INVALID, 0.0, False
        if ins.kind == "keyword":
            return self._apply_keyword(ins)
        key = self._target(ins.name)
        if not self._exists(key):
            return protocol.INVALID, 0.0, False
        if ins.kind == "increment":
            current = self._value(key)
            current = 0 if current is None else current
            if not isinstance(current, int):
                return protocol.INVALID, 0.0, False
            self._store(key, current + (1 if ins.op == "++" else -1))
            return protocol.CMD_OK, 0.0, False
        value = protocol.parse_value(ins.value)
        if not self._type_fits(key, ins.value, value):
            return protocol.INVALID, 0.0, False
        if ins.op != "=":
            current = self._value(key)
            current = 0 if current is None else current
            if not isinstance(current, int) or not isinstance(value, int):
                if ins.op == "+=" and isinstance(current, str) and isinstance(value, str):
                    value = current + value
                else:
                    return protocol.INVALID, 0.0, False
            else:
                value = _arith(ins.op, current, value)
                if value is None:
                    return protocol.INVALID, 0.0, False
        self._store(key, value)
        return protocol.CMD_OK, 0.0, False

    def _apply_keyword(self, ins: protocol.Instruction) -> Tuple[Optional[int], float, bool]:
        kw, args = ins.keyword, ins.args
        if kw == "page":
            name = self._page_name(args)
            if name is None or not self._exists(name):
                return protocol.INVALID, 0.0, False
            self._show_page(name)
            return protocol.CMD_OK, self.redraw_s, False
        if kw == "ref":
            return protocol.CMD_OK, self.redraw_s, False
        if kw == "get":
            key = self._target(args)
            if not self._exists(key):
                return protocol.INVALID, 0.0, False
            value = self._value(key)
            if value is None:
                value = 0
            self._reply(protocol.number(value) if isinstance(value, int) else protocol.string(str(value)))
            return None, 0.0, False
        if kw == "printh":
            try:
                self._reply(protocol.printh_bytes(args))
            except ValueError:
                return protocol.INVALID, 0.0, False
            return None, 0.0, False
        if kw == "vis":
            name, _, state = args.partition(",")
            key = self._qualify(name.strip() + ".vis")
            try:
                visible = int(state.strip())
            except ValueError:
                return protocol.INVALID, 0.0, False
            if not self._exists(key):
                return protocol.INVALID, 0.0, False
            self.components[key] = visible
            return protocol.CMD_OK, 0.0, False
        if kw == "rest":
            self._reset()
            self.boot()
            return None, 0.0, False
        if kw == "sendme":
            if self.page is None:
                return protocol.INVALID, 0.0, False
            self._reply(protocol.frame(protocol.CURRENT_PAGE, bytes([self.page_id(self.page)])))
            return None, 0.0, False
        if kw in DRAWING:
            self.drawn.append(ins.text)
            self.version += 1
        # The rest: recorded, not modelled.
        return protocol.CMD_OK, 0.0, False

    def _store(self, key: str, value: Value) -> None:
        """Assign a qualified key, with what assigning it does. Called with _cond held."""
        if "." not in key:
            if key in self.globals:
                self.globals[key] = value
                return
            self.system[key] = value
            if key == "bkcmd" and isinstance(value, int):
                self.bkcmd = value
            elif key in ("baud", "bauds") and isinstance(value, int) and value > 0:
                self.flow.baud = value
            elif key == "sleep":
                self.version += 1
            return
        self.components[key] = value
        self.version += 1
        timer, _, attr = key.rpartition(".")
        if attr in ("en", "tim"):
            # Assigning either restarts the countdown from now. Not measured; the HMIs rely on
            # it, since they write sleep_display.en=1 on every touch to put the screensaver off.
            tim = self._value(timer + ".tim")
            if isinstance(tim, int) and tim > 0 and self._value(timer + ".en"):
                self._timer_due[timer] = self._clock() + tim / 1000.0
            else:
                self._timer_due.pop(timer, None)

    # ---- the TFT's code --------------------------------------------------------------

    def _current_page(self) -> Page:
        if self.screen is None or self.page is None:
            raise RuntimeError("touching the screen needs an HMI")
        page = self.screen.page(self.page)
        if page is None:
            raise RuntimeError("the display shows %r, which the HMI does not have" % self.page)
        return page

    def _component(self, page: Page, component: Union[int, str]) -> Component:
        comp = page.by_id(component) if isinstance(component, int) else page.find(component)
        if comp is None:
            raise KeyError("page %r has no component %r" % (page.name, component))
        return comp

    def _visible(self, page: Page, comp: Component) -> bool:
        return self.components.get("%s.%s.vis" % (page.name, comp.name), 1) != 0

    def _hit(self, page: Page, x: int, y: int) -> Component:
        """What a touch at (x, y) lands on: the visible component drawn last (the highest id)
        that covers the point, else the page itself."""
        for comp in reversed(page.components):
            rect = comp.rect
            if rect is None or not self._visible(page, comp):
                continue
            cx, cy, w, h = rect
            if cx <= x < cx + w and cy <= y < cy + h:
                return comp
        return page.components[0]

    def _held(self) -> Optional["_Touch"]:
        """The touch in progress, unless the page it began on has gone: a new page never saw
        the press, so it gets neither the moves nor the release."""
        if self._touch is not None and self._touch.page != self.page:
            self._touch = None
        return self._touch

    def _asleep(self) -> bool:
        return self.system.get("sleep") == 1

    def _report_xy(self, x: int, y: int, pressed: bool, asleep: bool = False) -> None:
        """The coordinates of a press or release, if sendxy asks for them. Before the 0x65 event,
        an order not measured."""
        if self.system.get("sendxy") == 1:
            code = protocol.TOUCH_XY_ASLEEP if asleep else protocol.TOUCH_XY
            self._reply(protocol.frame(code, struct.pack(">HHB", x, y, 1 if pressed else 0)))

    def _set_touch_point(self, x: int, y: int, x0: int, y0: int) -> None:
        # tch0/tch1: where the finger is now, 0 once it lifts; tch2/tch3: where it pressed.
        for name, v in (("tch0", x), ("tch1", y), ("tch2", x0), ("tch3", y0)):
            self.system[name] = v

    def _slide_to(self, page: Page, comp: Component, x: int, y: int) -> bool:
        """Move a slider's value to the touch point. Returns whether it changed."""
        key = "%s.%s.val" % (page.name, comp.name)
        value = _slider_value(comp, x, y)
        old = self._value(key)
        self._store(key, value)
        return old != value

    def _touch_edge(self, page: Page, comp: Component, pressed: bool) -> None:
        """A press or release on `comp`: its 0x65 frame if its sendkey asks for one, its code,
        then the page's touch capture's code. That order is not measured."""
        if comp.reports_press if pressed else comp.reports_release:
            self._reply(protocol.frame(protocol.TOUCH_EVENT, bytes([page.id, comp.id, 1 if pressed else 0])))
        event = "down" if pressed else "up"
        self._run_event(page, comp, event)
        capture = page.touch_capture
        if capture is None or self.page != page.name:
            return
        self._store("%s.%s.val" % (page.name, capture.name), comp.id)
        self._run_event(page, capture, event)

    def _show_page(self, name: str) -> None:
        """Change page: the old page's unload, then the new one's load and loadend."""
        if self._code is not None and self.page is not None and self.screen is not None:
            old = self.screen.page(self.page)
            if old is not None:
                self._run_event(old, old.components[0], "unload")
        if self.screen is not None:
            # A page load starts its local components from the HMI again.
            for prefix in self.screen.local_keys_prefix(name):
                for key in [k for k in self.components if k.startswith(prefix)]:
                    del self.components[key]
        self.page = name
        self.page_history.append(name)
        self.drawn.clear()
        self.version += 1
        self._timer_due.clear()
        self._touch = None
        if self._code is not None and self.screen is not None:
            new = self.screen.page(name)
            if new is not None:
                self._run_event(new, new.components[0], "load")
                if self.page == name:
                    self._run_event(new, new.components[0], "loadend")

    def _run_event(self, page: Page, comp: Component, event: str) -> None:
        if self._code is None:
            return
        where = "%s.%s.%s" % (page.name, comp.name, event)
        self._run_code(where, self._code.events.get((page.name, comp.name, event)))

    def _run_code(self, where: str, code) -> None:
        """Run an event's code, or its override. Called with _cond held."""
        override = self.overrides.get(where)
        if override is None and not code:
            return
        if self._depth >= MAX_EVENT_DEPTH:
            log.error("%s: events nested %d deep (%s); not run", where, self._depth, " > ".join(self._running))
            self.script_errors.append((where, "<nested too deep>", protocol.INVALID))
            return
        self._depth += 1
        self._running.append(where)
        try:
            if override is not None:
                override(self)
            else:
                self._interpreter.run(code)
        finally:
            self._running.pop()
            self._depth -= 1

    def _run_timers(self, now: float) -> Optional[float]:
        """Run the current page's timers that are due. Returns when the next one is."""
        if self._code is None or self.screen is None or self.page is None:
            return None
        page = self.screen.page(self.page)
        if page is None:
            return None
        nxt = None
        for comp in page.timers:
            if self.page != page.name:
                return now  # a timer changed page: look again at the new one's
            key = "%s.%s" % (page.name, comp.name)
            tim = self._value(key + ".tim")
            if not self._value(key + ".en") or not isinstance(tim, int) or tim <= 0:
                self._timer_due.pop(key, None)
                continue
            due = self._timer_due.setdefault(key, now + tim / 1000.0)
            if now >= due:
                # A timer that fell behind fires once, not once per period missed.
                self._timer_due[key] = now + tim / 1000.0
                self._run_event(page, comp, "timer")
                due = self._timer_due.get(key)
            if due is not None:
                nxt = due if nxt is None else min(nxt, due)
        return nxt

    def _target(self, name: str) -> str:
        return self._qualify(name.strip()) if "." in name else name.strip()

    def _exists(self, key: str) -> bool:
        if self.resolver is not None:
            return self.resolver(key)
        if key in self.globals:
            return True
        if self.screen is not None:
            if "." not in key and self.screen.page(key) is not None:
                return True
            return self.screen.resolve(key, self.page) is not None
        return True

    def _type_fits(self, key: str, written: str, value: Value) -> bool:
        """A literal must suit the attribute: text for a text attribute, a number for a number.
        Anything else (an expression) is not checked."""
        if self.screen is None or "." not in key:
            return True
        target = self.screen.resolve(key, self.page)
        if target is None or target.default is None:
            return True
        literal_text = written.strip().startswith('"')
        if isinstance(target.default, int):
            return not literal_text
        return literal_text or not isinstance(value, int)

    def _value(self, key: str) -> Optional[Value]:
        """Called with _cond held, with a qualified key."""
        if "." not in key:
            if key == "dp" and self.screen is not None and self.page is not None:
                return self.page_id(self.page)
            if key in self.globals:
                return self.globals[key]
            if key in self.system:
                return self.system[key]
        elif key in self.components:
            return self.components[key]
        if self.screen is not None:
            target = self.screen.resolve(key, self.page)
            if target is not None:
                return target.default
        return None

    def _page_name(self, args: str) -> Optional[str]:
        args = args.strip()
        if not args:
            return None
        if self.screen is not None:
            page = self.screen.page(args)
            return page.name if page is not None else args
        if args.isdigit():
            number = int(args)
            for name, pid in self._page_ids.items():
                if pid == number:
                    return name
            return "page%d" % number
        self._page_ids.setdefault(args, len(self._page_ids))
        return args

    def _qualify(self, key: str) -> str:
        if key.count(".") >= 2 or self.page is None:
            return key
        return "%s.%s" % (self.page, key)

    # ---- sending ---------------------------------------------------------------------

    def _reply(self, data: bytes, delay: float = 0.0, upload=None) -> None:
        """Queue bytes for the MCU, after everything queued before them, `delay` seconds after
        the one before (as a display busy flashing would send them)."""
        with self._out_cond:
            at = max(self._clock(), self._last_send_at) + delay
            self._last_send_at = at
            self._outbox.append((at, data, upload))
            self._out_cond.notify_all()

    def _outbox_busy(self) -> bool:
        with self._out_cond:
            return bool(self._outbox) or self._sending

    _sending = False

    def _write_loop(self) -> None:
        while True:
            with self._out_cond:
                while not self._outbox and not self._closed:
                    self._out_cond.wait()
                if self._closed:
                    return
                at, data, upload = self._outbox[0]
                wait = at - self._clock()
                if wait > 0:
                    self._out_cond.wait(wait)
                    continue
                self._outbox.popleft()
                self._sending = True
            try:
                self.transport.send(data)
                if upload is not None:
                    with self._cond:
                        upload.acks.append((time.monotonic(), data))
            except (OSError, NotConnected) as e:
                log.warning("could not send %s: %s", data[:16].hex(), e)
            finally:
                with self._out_cond:
                    self._sending = False
                with self._cond:
                    self._cond.notify_all()


class _Touch:
    def __init__(self, page: str, component: Optional[Component], x: int, y: int):
        self.page = page
        self.component = component  # None for a touch on a sleeping display, which nothing sees
        self.x0, self.y0 = x, y
        self.x, self.y = x, y


def _slider_range(comp: Component) -> Tuple[int, int, bool]:
    """(minval, maxval, vertical)."""
    a = comp.attrs
    return int(a.get("minval", 0)), int(a.get("maxval", 100)), a.get("mode", 0) == 1


def _slider_value(comp: Component, x: int, y: int) -> int:
    """The value a slider takes for a touch at (x, y): linear along it, the low end left (or,
    vertical, at the bottom). How the thumb's width enters into it is not modelled."""
    lo, hi, vertical = _slider_range(comp)
    cx, cy, w, h = comp.rect
    if vertical:
        pos = (cy + h - 1 - y) / max(1, h - 1)
    else:
        pos = (x - cx) / max(1, w - 1)
    return lo + round(min(1.0, max(0.0, pos)) * (hi - lo))


def _slider_point(comp: Component, value: int) -> Tuple[int, int]:
    """Where to touch a slider for it to take `value`, or the nearest it can."""
    lo, hi, vertical = _slider_range(comp)
    cx, cy, w, h = comp.rect
    pos = (min(hi, max(lo, value)) - lo) / max(1, hi - lo)
    if vertical:
        candidates = [(cx + w // 2, cy + h - 1 - round(pos * (h - 1)) + d) for d in (-1, 0, 1)]
    else:
        candidates = [(cx + round(pos * (w - 1)) + d, cy + h // 2) for d in (-1, 0, 1)]
    candidates = [p for p in candidates if cx <= p[0] < cx + w and cy <= p[1] < cy + h]
    return min(candidates, key=lambda p: abs(_slider_value(comp, *p) - value))


class _ScriptHost(Host):
    """The display as the TFT's code sees it. Called with the display's _cond held."""

    def __init__(self, display: Display):
        self.d = display

    def _key(self, name: str) -> str:
        key = self.d._target(name)
        if not self.d._exists(key):
            raise ScriptError("%s does not exist here (page %s)" % (name, self.d.page))
        return key

    def read(self, name: str) -> Value:
        key = self._key(name)
        value = self.d._value(key)
        if value is None:
            return 1 if key.endswith(".vis") else 0
        return value

    def write(self, name: str, value: Value) -> None:
        key = self._key(name)
        current = self.d._value(key)
        if current is not None and isinstance(current, str) != isinstance(value, str):
            raise ScriptError("%s holds %s, not %r" % (name, "text" if isinstance(current, str) else "a number", value))
        self.d._store(key, value)

    def component_name(self, component_id: int) -> str:
        page = self.d._current_page()
        comp = page.by_id(component_id)
        if comp is None:
            raise ScriptError("page %s has no component %d" % (page.name, component_id))
        return comp.name

    def declare(self, name: str, value: int) -> None:
        self.d.globals[name] = value

    def instruction(self, keyword: str, *args) -> None:
        if keyword in ("printh", "prints"):
            self.d._reply(args[0])
        elif keyword == "vis":
            self.d._store(self._key(args[0] + ".vis"), 1 if args[1] else 0)
        elif keyword == "page":
            name = self.d._page_name(args[0])
            if name is None or not self.d._exists(name):
                raise ScriptError("no page %s" % args[0])
            self.d._show_page(name)
        else:
            raise ScriptError("%s is not supported" % keyword)

    def failed(self, stmt: Stmt, error: ScriptError) -> None:
        d = self.d
        where = d._running[-1] if d._running else "?"
        log.warning("%s: %r failed: %s", where, stmt.text, error)
        d.script_errors.append((where, stmt.text, error.code))
        d.errors.append(("[%s] %s" % (where, stmt.text), error.code))
        # As for an instruction from the MCU. Not measured for event code.
        if protocol.reports_failure(d.bkcmd):
            d._reply(protocol.frame(error.code))


def _arith(op: str, a: int, b: int) -> Optional[int]:
    if op == "+=":
        return a + b
    if op == "-=":
        return a - b
    if op == "*=":
        return a * b
    if op in ("/=", "%=") and b == 0:
        return None
    if op == "/=":
        return int(a / b)
    if op == "%=":
        return a - b * int(a / b)
    return None
