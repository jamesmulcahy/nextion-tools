"""The whole demo in one process: an emulated display running demo.HMI, the companion as its MCU,
and the display in a browser.

    python demo/run.py [--http 8080] [--rotation 0|90|180|270]

Needs nextion-tools[images]. Open the URL it prints, and use the panel with the mouse.
"""

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from companion import Companion, SocketLink  # noqa: E402
from nextion_tools.emu import Display, SocketTransport  # noqa: E402
from nextion_tools.emu.viewer import Viewer  # noqa: E402
from nextion_tools.hmi import Project  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def start(hmi_path: str = os.path.join(HERE, "demo.HMI"), rotation: int = 0):
    """(display, companion), connected, booted and running."""
    hmi = Project.load(hmi_path)
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=hmi, rotation=rotation)
    deadline = time.monotonic() + 2
    while not display.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    companion = Companion(SocketLink(b), hmi)
    display.boot()
    companion.start()
    return display, companion


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="demo/run.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--http", type=int, default=8080, metavar="PORT")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--rotation", type=int, default=0, choices=(0, 90, 180, 270),
                        help="turn the screen this many degrees clockwise")
    args = parser.parse_args(argv)
    display, companion = start(rotation=args.rotation)
    viewer = Viewer(display, args.bind, args.http)
    print("demo up: open %s" % viewer.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        viewer.close()
        companion.stop()
        display.close()


if __name__ == "__main__":
    sys.exit(main())
