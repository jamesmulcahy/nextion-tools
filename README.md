# nextion-tools

Tools for [Nextion](https://nextion.tech) HMI displays: the Nextion Editor's project files, the
TFTs it compiles, and an emulator of the display itself for testing the firmware that drives one.

| Module | What |
|---|---|
| `nextion_tools.hmi` | `.HMI` projects: pages, components, ids, geometry, attributes, event code, `Program.s`; numeric edits with re-stamping |
| `nextion_tools.fonts` | `.zi` fonts: metrics and glyph bitmaps, and which font a component's `font` index means |
| `nextion_tools.images` | pictures as RGB arrays, and the panel's RGB565 |
| `nextion_tools.tft` | a compiled `.tft`: its picture table, checksums and orientation, and same-size picture swaps |
| `nextion_tools.crc` | the CRC behind TFT checksums and HMI page stamps |
| `nextion_tools.emu` | an emulated display, driven by the project's HMI (below) |

```
pip install nextion-tools            # hmi, tft, crc and the emulator: no dependencies
pip install nextion-tools[images]    # fonts and pictures: numpy and Pillow
pip install nextion-tools[serial]    # the emulator on a real serial port: pyserial
```

```python
from nextion_tools.hmi import Project

project = Project.load("gui.HMI")
home = project.page("home")                 # KeyError if there is none; find_page() gives None
print(home.id, [(c.id, c.name, c.kind, c.rect) for c in home.components])
print(home.component("tm0").events["timer"])
print(project.program)                      # Program.s, line by line
```

The format notes are in the module docstrings, and were checked against files the Editor saved.
Nextion and the Nextion Editor are ITEAD's; this project is not affiliated with them.

## The emulator

An emulated Nextion HMI display, for testing the firmware that drives one. Point the MCU's UART
at it (QEMU's `-serial tcp:`, a pty, or a real serial port). It executes what it is sent, keeps a
model of what the screen would show, replies as a real display does, and lets a test act as the
user.

```python
from nextion_tools.emu import Display, TcpServer
from nextion_tools.hmi import Project

display = Display(TcpServer(port=4000), hmi=Project.load("gui.HMI"))  # qemu ... -serial tcp:127.0.0.1:4000
display.wait_for_page("home")
display.press("h_ceiling")                  # a finger on it, by name or id: display.press(22)
display.wait_for_value("home.room.txt", "Kitchen")
assert not display.errors                   # nothing the display would have rejected
```

Or from a shell, to watch what firmware sends:

```
python -m nextion_tools.emu --tcp 4000
python -m nextion_tools.emu --pty                 # prints /dev/pts/N
python -m nextion_tools.emu --serial /dev/ttyUSB0 # needs pyserial: pip install nextion-tools[serial]
python -m nextion_tools.emu --tcp 4000 --hmi gui.HMI   # with the project's pages and code
python -m nextion_tools.emu.script gui.HMI        # can every event of this HMI be run?
```

## What it models

Replies follow what an NX4832T035 (the Sonoff NSPanel's display) was measured to do, which differs
from Nextion's documentation in places:

| Instruction | `bkcmd=3` | `bkcmd=0` |
|---|---|---|
| assignment | `01` | nothing |
| `get x` | `71` + 4 bytes (or `70` + text), **no** `01` | the same |
| `printh …` | the bytes, **no** `01` | the same |
| unknown component, variable, or bad syntax | `1a` (never `02`/`1b`) | nothing |
| `page` / `ref` | `01` at once, then the redraw blocks the next instruction ~400 ms | — |
| `delay=N` | `01` when it ends | — |

`bkcmd` starts at 2, as on a display that has just powered on, and `power_cycle()` puts it back.

**Ideal or flow model.** By default every instruction executes the moment it arrives, so tests
see exactly what the firmware meant to draw. With `flow_model=True` the display behaves as the
real one does:

- input arrives at line rate (QEMU's UART does not pace writes);
- it waits in a buffer of 1088 bytes;
- the display stalls on page changes, `ref` and `delay`;
- a burst that overruns the buffer during a stall loses commands (`display.lost`), and the
  display sends `0x24` when it resumes.

`tests/emu/test_calibration.py` replays the hardware measurements against the model, so it cannot
drift from them unnoticed.

**TFT uploads** (`whmi-wri`, `whmi-wris`): the display acknowledges 4096-byte chunks, answers
v1.2's first chunk with a jump, and keeps the image in `display.flash`. It can be told to
misbehave: slow, dropped or duplicated acks, jumps, stray bytes, a power cycle mid-upload.

**The HMI.** Given the project (`hmi=Project.load(...)`, read straight from the Nextion Editor's
`.HMI`), the display knows what exists and resolves names as the real one does:

- `comp.attr` without a page means the current page;
- `page.comp.attr` reaches another page only for a global component;
- the attribute must be one the component has, and a literal must suit it (text or number);
- page and component ids, for `page N` and touch events, are the HMI's;
- `get` answers the HMI's value for anything not yet assigned (`display.value(key)`);
- loading a page resets its local components;
- the display starts on page 0.

Anything that does not resolve fails with `1a` and is recorded in `display.errors`. On a real
display running `bkcmd=0`, that is a label that is silently never drawn. Without an HMI every
name is accepted. `resolver=` overrides the HMI with a function of your own.

**The TFT's own code.** With an HMI, the display also runs the code in it (`emu/script.py`):

- `Program.s` at `boot()`: its globals, then `page 0`, whose load code may print a banner;
- each page's `unload`, `load` and `loadend` on a page change, whoever asked for it;
- the current page's timers, every `tim` ms while `en` is set, on the display's clock;
- touches. `touch_down(x, y)` / `touch_move` / `touch_up` are a finger on the screen, and
  `press(name)`, `slide(name, value)`, `drag`, `swipe` and `tap` are built on them. The touch
  lands on the visible component drawn last at that point (else the page). It sends the 0x65
  edges the component's `sendkey` asks for, then runs the component's `down` / `up` (and a
  slider's `slide`), then the page's touch capture's, with `tch0..3` and the capture's `val`
  set as the display sets them.

All of an HMI's code is compiled when the display loads it, and code outside what the
interpreter supports is refused then (`UnsupportedCode`, naming the event and line).
`python -m nextion_tools.emu.script gui.HMI` lists what an HMI uses and what it cannot run. An override
(`overrides={"page.component.event": fn}`, logged at startup) runs Python instead of an event's
code; `scripts=False` turns the code off, and touches are then bare 0x65 frames
(`send_touch_event`). A statement that fails when it runs (an unknown name, say) is reported as
an instruction would be (`1a`, `display.errors`, `display.script_errors`), and the code carries on.

For tests that need timers to fire exactly, pass `clock=ManualClock()`, then `clock.advance(s)`
and `display.tick()`. `expire_timer(name)` runs a timer out now, whatever the clock.

Not measured on hardware yet, so each is a choice (see `emu/script.py` and `display.py`):

- the order on a touch: the 0x65 frame, then the component's code, then the touch capture's;
- assigning `en` or `tim` restarts a timer's countdown;
- `&&` and `||` stop at the first operand that decides; a failed condition skips its `if`;
- an expression mixing `+`/`-` with `*`/`/` is refused, since precedence is not known;
- `page` ends the event that ran it;
- a slider's value is linear in the touch position, ignoring the thumb's width;
- whether a failed statement in event code sends `1a` at `bkcmd` 2 and 3 (assumed yes).

## Not modelled (yet)

- **Rendering.** The model is attributes, not pixels.
- **Drawing instructions** in event code (`xstr`, `fill`, ...) and `click`, `tsw`, loops: the
  HMIs tested do not use them, so they are refused rather than guessed at.

## Tests

```
pip install -e ".[images,serial,test]"
python -m pytest
```

Most tests build small projects in code (`tests/hmi_builder.py`). The rest need files the
Editor made, and are skipped unless given them:

```
NEXTION_TOOLS_HMI=path/to/gui.HMI NEXTION_TOOLS_TFT=path/to/gui.tft python -m pytest
```

## License

MIT; see [LICENSE](LICENSE).
