# The demo

A small Nextion project, made in the Nextion Editor, and a Python MCU for it. Together they
show and test nextion-tools end to end without any other firmware.

```
pip install -e ".[images]"
python demo/run.py                       # then open http://127.0.0.1:8080/
python demo/run.py --rotation 90         # as the LCD itself holds the page
```

`run.py` starts an emulated display running `demo.HMI`, connects `companion.py` to it as the
MCU, and serves the screen in a browser, where the mouse is a finger.

The companion also drives a real display, flashed with `demo.tft` (pyserial, the `serial`
extra):

```
python demo/companion.py --serial /dev/ttyUSB0 --fps 8     # demo.tft talks at 9600 baud
python demo/companion.py --tcp 127.0.0.1:4000              # or: python -m nextion_tools.emu.viewer --hmi demo/demo.HMI --tcp 4000
```

## The files

- `demo.HMI`: the project. Nextion Editor (2026), device NX4832T035 (the NSPanel's), display
  direction 90 (landscape, 480 x 320), one font (Arial, 40 px, iso-8859-1) and one picture.
- `demo.tft`: compiled from it.
- `companion.py`: the MCU. It finds pages and components by name in the HMI.
- `run.py`: the emulator, the companion and the viewer in one process.

## The pages

| Page | What the display does by itself | What the companion does |
|---|---|---|
| `dummy` | nothing: `Program.s` goes straight past it to `page 1` | |
| `menu` | a picture background; each button reports its release | changes page; Sleep sends `sleep=1` |
| `dial` | Up and Down add or take 10 on the bar (`+=`, `-=`, `if`/`else`), copy it to the number, and an idle timer goes back to the menu after 10 s | reads the bar back (`get j0.val`) and turns it red from 80 |
| `paint` | nothing | `sendxy=1`; dots and lines where the finger is, polled through `tch0`/`tch1` |
| `pong` | nothing | `sendxy=1`; a ball and a paddle drawn with `fill`; the score in `n_score` |

`Program.s` also sets `thup=1`, so after Sleep the first touch wakes the display, and goes no
further.

## What it showed

The project was the first one the library read that a recent Editor made at direction 90, and
it turned up three things NSPanel Manager's projects never did:

- **The display direction** is in the HMI too (`main.HMI` byte 0x0C), not only the TFT.
- **The TFT stores pictures in the LCD's own frame**: at direction 90, turned 90 degrees
  clockwise. That settled which way `render.native_rotation` turns, which had been a guess,
  the other way round. The raw picture also has no record header, unlike the older Editor's.
- **Newer fonts are stored turned the same way** (header byte 3 is 0x0B, not 0x0A), so their
  glyphs came out scrambled until the font reader learned to turn them back.

And it drove the emulator to model `sendxy`, `sleep` and `thup`, `sendme`, and to draw `line`,
`draw`, `cir` and `cirs`.

## Open questions

A real display would answer these; the Editor's Debug simulator cannot drag, and draws nothing
the MCU sends unless typed in by hand:

- Does a drag with `sendxy=1` stream 0x67 frames, or only press and release (as modelled)?
- Do `cirs` circles of radius 1 include their corners?

Settled: on `dial`, touching the page runs `tm_idle.en=1`, which in the simulator does not
restart the 10 s timer (it goes back to the menu 10 s after the page opened). On a real NSPanel
the same write does restart a running timer, which is what keeps its screensaver off while you
tap. The emulator follows the hardware.
