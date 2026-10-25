"""The demo's MCU: what an ESP32 would do with demo.HMI, written in Python.

    python demo/companion.py --tcp 127.0.0.1:4000    # an emulated display
    python demo/companion.py --serial /dev/ttyUSB0   # a real one, running demo.tft

It answers the display's touch events. The menu's buttons only report themselves, so the
companion changes page for them. The thermostat's bar turns red near the top, Paint draws where
the finger goes, and Pong plays a ball against a paddle that follows the finger.

It finds pages and components by name in the HMI, so it keeps working when the project is
edited, as long as the names stay.
"""

import argparse
import collections
import socket
import struct
import sys
import threading
import time
from typing import Callable, Deque, Optional, Tuple

from nextion_tools.hmi import Project

TERMINATOR = b"\xff\xff\xff"
BLACK, RED, WHITE = 0, 63488, 65535

# Replies with a fixed-length payload, which may itself hold FF bytes. Anything else runs to the
# terminator.
PAYLOAD = {0x65: 3, 0x66: 1, 0x67: 5, 0x68: 5, 0x71: 4}


class SocketLink:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def read(self, timeout: float) -> bytes:
        self.sock.settimeout(max(timeout, 0.001))  # 0 would make the socket non-blocking
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        if not data:
            raise EOFError("the display hung up")
        return data

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self.sock.close()


class SerialLink:
    def __init__(self, device: str, baud: int):
        import serial  # pyserial: nextion-tools[serial]
        self.port = serial.Serial(device, baud, timeout=0)

    def read(self, timeout: float) -> bytes:
        self.port.timeout = timeout
        return self.port.read(self.port.in_waiting or 1)

    def write(self, data: bytes) -> None:
        self.port.write(data)

    def close(self) -> None:
        self.port.close()


class Companion:
    def __init__(self, link, hmi: Project, fps: int = 20):
        self.link = link
        self.hmi = hmi
        self.period = 1.0 / fps
        self.page: Optional[str] = None  # what the display shows, as far as the companion knows
        self.touching: Optional[Tuple[int, int]] = None  # the finger, while one is down (sendxy)
        self.strokes = 0  # presses and releases seen (sendxy), so a poll can tell it is stale
        self.game = None  # Paint or Pong, on their pages
        self.received: Deque[Tuple[int, bytes]] = collections.deque(maxlen=1000)  # (code, payload)
        self._buf = b""
        self._gets: Deque[Callable[[int], None]] = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- talking to the display ----------------------------------------------------------

    def send(self, *instructions: str) -> None:
        with self._lock:
            self.link.write(b"".join(i.encode("latin-1") + TERMINATOR for i in instructions))

    def get(self, name: str, then: Callable[[int], None]) -> None:
        """Ask for a number; `then` gets it when the 0x71 comes back."""
        with self._lock:
            self._gets.append(then)
            self.link.write(b"get " + name.encode("latin-1") + TERMINATOR)

    @property
    def waiting(self) -> bool:
        return bool(self._gets)

    def show(self, page: str) -> None:
        if self.game is not None:
            self.send("sendxy=0")
            self.game = None
        self.send("page " + page)
        self.page = page
        self.touching = None
        if page in GAMES:
            self.send("sendxy=1")
            self.game = GAMES[page](self)

    # ---- the loop -------------------------------------------------------------------------

    def start(self) -> "Companion":
        self._thread = threading.Thread(target=self.run, name="companion", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)

    def run(self) -> None:
        self.send("sendme")
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                data = self.link.read(max(0.0, next_tick - time.monotonic()))
            except (EOFError, OSError):
                return
            self._feed(data)
            if time.monotonic() >= next_tick:
                next_tick += self.period
                if self.game is not None:
                    self.game.tick()

    def _feed(self, data: bytes) -> None:
        self._buf += data
        while self._buf:
            code = self._buf[0]
            if code in PAYLOAD:
                end = 1 + PAYLOAD[code]
                if len(self._buf) < end + 3:
                    return
                if self._buf[end:end + 3] != TERMINATOR:  # out of step: skip to the next frame
                    self._buf = self._buf[self._buf.find(TERMINATOR, 1) + 3:] if TERMINATOR in self._buf[1:] else b""
                    continue
                payload, self._buf = self._buf[1:end], self._buf[end + 3:]
            else:
                end = self._buf.find(TERMINATOR)
                if end < 0:
                    return
                payload, self._buf = self._buf[1:end], self._buf[end + 3:]
            self.received.append((code, payload))
            self._handle(code, payload)

    def _handle(self, code: int, payload: bytes) -> None:
        if code == 0x66:
            self.page = self.hmi.page_by_id(payload[0]).name
        elif code == 0x65:
            page = self.hmi.page_by_id(payload[0])
            comp = page.by_id(payload[1])
            self.page = page.name
            if payload[2] == 0 and comp is not None:
                self._released(page.name, comp.name)
        elif code == 0x67:
            x, y, state = struct.unpack(">HHB", payload)
            self.touching = (x, y) if state else None
            self.strokes += 1
            if self.game is not None:
                self.game.touched(x, y, bool(state))
        elif code == 0x71 and self._gets:
            self._gets.popleft()(struct.unpack("<i", payload)[0])
        elif code in (0x1A, 0x1B, 0x1C, 0x1E) and self._gets:
            # A failed get answers with an error instead of 0x71.
            self._gets.popleft()

    def _released(self, page: str, comp: str) -> None:
        if comp == "b_back":
            self.show("menu")
        elif page == "menu" and comp == "b_sleep":
            self.send("sleep=1")
        elif page == "menu" and comp.startswith("b_") and self.hmi.find_page(comp[2:]):
            self.show(comp[2:])
        elif page == "dial" and comp in ("b_up", "b_down"):
            self.get("j0.val", self._colour_bar)

    def _colour_bar(self, value: int) -> None:
        normal = self.hmi.page("dial").component("j0").get("pco")
        self.send("j0.pco=%d" % (RED if value >= 80 else normal))


class Game:
    """A page the finger plays on. While it is down, tch0 and tch1 are polled for where it is
    now, since the display reports only where a touch starts and ends."""

    def __init__(self, c: Companion):
        self.c = c
        page = c.hmi.page(c.page)
        self.size = page.size
        self.background = page.components[0].get("bco")
        # Where the page's own components are, which the game keeps off.
        self.reserved = [comp.rect for comp in page.components[1:] if comp.rect is not None]

    def free(self, x: int, y: int) -> bool:
        return not any(rx <= x < rx + w and ry <= y < ry + h for rx, ry, w, h in self.reserved)

    def touched(self, x: int, y: int, down: bool) -> None:
        pass

    def tick(self) -> None:
        if self.c.touching is not None and not self.c.waiting:
            strokes = self.c.strokes
            point = []
            self.c.get("tch0", point.append)
            self.c.get("tch1", lambda y: self._polled(strokes, point[0], y))

    def _polled(self, strokes: int, x: int, y: int) -> None:
        # Only a sample from the same touch as the poll, with the finger still down: the display
        # reads 0, 0 once it lifts.
        if self.c.touching is not None and self.c.strokes == strokes and (x, y) != (0, 0):
            self.c.touching = (x, y)
            self.moved(x, y)

    def moved(self, x: int, y: int) -> None:
        pass


class Paint(Game):
    def __init__(self, c: Companion):
        super().__init__(c)
        self.last: Optional[Tuple[int, int]] = None

    def touched(self, x: int, y: int, down: bool) -> None:
        self.last = None
        if down and self.free(x, y):
            self.c.send("cirs %d,%d,3,%d" % (x, y, BLACK))
            self.last = (x, y)

    def moved(self, x: int, y: int) -> None:
        if not self.free(x, y):
            self.last = None
            return
        if self.last is not None and self.last != (x, y):
            self.c.send("line %d,%d,%d,%d,%d" % (self.last + (x, y, BLACK)))
        self.c.send("cirs %d,%d,3,%d" % (x, y, BLACK))
        self.last = (x, y)


class Pong(Game):
    BALL = 10
    PADDLE_W, PADDLE_H = 90, 10

    def __init__(self, c: Companion):
        super().__init__(c)
        w, h = self.size
        self.top = max((ry + rh for _, ry, _, rh in self.reserved), default=0) + 4
        self.paddle_y = h - 3 * self.PADDLE_H
        self.paddle_x = (w - self.PADDLE_W) // 2
        self.score = 0
        self.c.send("fill 0,%d,%d,%d,%d" % (self.top - 2, w, 2, BLACK))  # the top wall
        self._serve()
        self._draw_paddle(None)

    def _serve(self) -> None:
        w, _ = self.size
        self.ball = [w // 2, self.top + 20]
        self.velocity = [7, 6]

    def _fill(self, x: int, y: int, w: int, h: int, colour: int) -> None:
        self.c.send("fill %d,%d,%d,%d,%d" % (x, y, w, h, colour))

    def _draw_paddle(self, old: Optional[int]) -> None:
        if old is not None:
            self._fill(old, self.paddle_y, self.PADDLE_W, self.PADDLE_H, self.background)
        self._fill(self.paddle_x, self.paddle_y, self.PADDLE_W, self.PADDLE_H, BLACK)

    def moved(self, x: int, y: int) -> None:
        w, _ = self.size
        old = self.paddle_x
        self.paddle_x = min(max(0, x - self.PADDLE_W // 2), w - self.PADDLE_W)
        if self.paddle_x != old:
            self._draw_paddle(old)

    def touched(self, x: int, y: int, down: bool) -> None:
        if down:
            self.moved(x, y)

    def tick(self) -> None:
        super().tick()
        w, h = self.size
        b = self.BALL
        old = tuple(self.ball)
        x, y = self.ball[0] + self.velocity[0], self.ball[1] + self.velocity[1]
        if x < 0 or x + b > w:
            self.velocity[0] = -self.velocity[0]
            x = min(max(x, 0), w - b)
        if y < self.top:
            self.velocity[1] = -self.velocity[1]
            y = self.top
        if self.velocity[1] > 0 and y + b >= self.paddle_y and old[1] + b <= self.paddle_y:
            if self.paddle_x - b < x < self.paddle_x + self.PADDLE_W:
                self.velocity[1] = -self.velocity[1]
                y = self.paddle_y - b
                self.score += 1
                self.c.send("n_score.val=%d" % self.score)
        self._fill(old[0], old[1], b, b, self.background)
        if y > h:
            self.score = 0
            self.c.send("n_score.val=0")
            self._serve()
        else:
            self.ball = [x, y]
        self._fill(self.ball[0], self.ball[1], b, b, BLACK)
        self._draw_paddle(None)  # the ball may have clipped it


GAMES = {"paint": Paint, "pong": Pong}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="demo/companion.py", description=__doc__.split("\n\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--tcp", metavar="HOST:PORT", help="an emulated display listening there")
    where.add_argument("--serial", metavar="DEVICE", help="a real display (needs pyserial)")
    parser.add_argument("--baud", type=int, default=9600, help="the serial port's speed (demo.tft: 9600)")
    parser.add_argument("--fps", type=int, default=20,
                        help="game frames a second (a real display at 9600 baud keeps up with about 8)")
    parser.add_argument("--hmi", default=__file__.rsplit("/", 1)[0] + "/demo.HMI")
    args = parser.parse_args(argv)
    if args.tcp:
        host, _, port = args.tcp.rpartition(":")
        link = SocketLink(socket.create_connection((host or "127.0.0.1", int(port))))
    else:
        link = SerialLink(args.serial, args.baud)
    companion = Companion(link, Project.load(args.hmi), fps=args.fps)
    try:
        companion.run()
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
