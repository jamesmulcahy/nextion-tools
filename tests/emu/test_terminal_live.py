"""The emulated terminal against the real sender, live: terminal_vectors/live_sender.cpp, the
StreamEngine from nspanel-terminal (or the NSPanel firmware's copy), talking to a Display over
TCP in real time: the setup, a burst, 2.5 s idle on PINGs (longer than the sender's 1.8 s reverse
timeout), a touch, another rectangle, EXIT.

Needs a C++17 compiler and the sender's source, so it is skipped unless given one of:

    NSPANEL_TERMINAL=path/to/nspanel-terminal
    NEXTION_TERMINAL=path/to/firmware/lib/NextionTerminal
"""

import os
import shutil
import subprocess
import threading
import time

import pytest

from nextion_tools.emu import Display, TcpServer

HERE = os.path.join(os.path.dirname(__file__), "terminal_vectors")
SOURCES = [(name, os.environ.get(name)) for name in ("NSPANEL_TERMINAL", "NEXTION_TERMINAL")]
SOURCES = [(name, path) for name, path in SOURCES if path]

pytestmark = pytest.mark.skipif(not SOURCES or shutil.which("c++") is None,
                                reason="set NSPANEL_TERMINAL or NEXTION_TERMINAL, and have c++")


def build(name, path, out):
    if name == "NSPANEL_TERMINAL":
        flags = ["-I" + os.path.join(path, "components", "nspanel_terminal")]
    else:
        flags = ["-DNT_FIRMWARE", "-I" + path]
    subprocess.run(["c++", "-std=c++17", "-O1", "-w"] + flags + [os.path.join(HERE, "live_sender.cpp"), "-o", out],
                   check=True)
    return out


@pytest.mark.timeout(60)
@pytest.mark.parametrize("source", SOURCES, ids=[name for name, _ in SOURCES])
def test_live_session(source, tmp_path):
    sender = build(source[0], source[1], str(tmp_path / "live_sender"))
    display = Display(TcpServer(), terminal="us")
    lines = []
    try:
        proc = subprocess.Popen([sender, str(display.transport.port), "4660", "2500", str(tmp_path / "out.fb")],
                                stdout=subprocess.PIPE, text=True)
        reader = threading.Thread(target=lambda: lines.extend(line.strip() for line in proc.stdout), daemon=True)
        reader.start()
        display.wait_for_rectangles(3, timeout=20)
        time.sleep(0.5)
        display.tap(150, 200, hold=0.2)
        assert proc.wait(timeout=30) == 0, lines
        reader.join(5)
        assert lines[0].startswith("comok ") and lines[1] == "ready"
        assert lines[2:] == ["done 3", "touch 1 150 200", "touch 0 150 200", "done 4", "stopped"], lines
        rx = display.terminal
        assert rx.stopped == "exit" and rx.faults == [] and rx.rectangles == 4
        assert rx.records["PING"] >= 20  # 2.5 s idle, one every 80 ms
        with open(tmp_path / "out.fb", "rb") as f:
            assert display.terminal_rgb565() == f.read()
    finally:
        display.close()
