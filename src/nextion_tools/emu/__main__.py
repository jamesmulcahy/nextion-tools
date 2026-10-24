"""Run an emulated display and log what it is sent.

  python -m nextion_tools.emu --tcp 4000          # listen; e.g. qemu ... -serial tcp:127.0.0.1:4000
  python -m nextion_tools.emu --pty               # prints the pty to point a program at
  python -m nextion_tools.emu --serial /dev/ttyUSB0
  python -m nextion_tools.emu --tcp 4000 --hmi gui.HMI   # the project's pages and code
"""

import argparse
import logging
import time

from .display import Display
from ..hmi import Project
from .protocol import TERMINATOR
from .transport import Pty, Serial, TcpServer


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m nextion_tools.emu", description=__doc__.split("\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--tcp", type=int, metavar="PORT", help="listen on 127.0.0.1:PORT (0 picks one)")
    where.add_argument("--pty", action="store_true", help="create a pseudo-terminal")
    where.add_argument("--serial", metavar="DEVICE", help="use a serial port (needs pyserial)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--flow", action="store_true", help="model the input buffer and stalls")
    parser.add_argument("--banner", default="", help="a frame the TFT prints when it starts, e.g. NSPM")
    parser.add_argument("--hmi", help="the project's .HMI: its pages, ids and code")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
    if args.tcp is not None:
        transport = TcpServer(port=args.tcp)
        print("listening on %s:%d" % (transport.host, transport.port), flush=True)
    elif args.pty:
        transport = Pty()
        print("pty: %s" % transport.path, flush=True)
    else:
        transport = Serial(args.serial, args.baud)
    banner = args.banner.encode() + TERMINATOR if args.banner else b""
    display = Display(transport, flow_model=args.flow, baud=args.baud, banner=banner,
                      hmi=Project.load(args.hmi) if args.hmi else None)
    try:
        # Power on (the banner, Program.s) once the MCU is there to hear it.
        while not display.connected:
            time.sleep(0.1)
        display.boot()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        display.close()


if __name__ == "__main__":
    main()
