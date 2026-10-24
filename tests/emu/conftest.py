import socket
import threading
import time

import pytest

from nextion_tools.emu import Display, SocketTransport
from nextion_tools.emu.protocol import TERMINATOR


class Mcu:
    """The MCU end of a display's UART: sends instructions, records every reply with the time
    it arrived."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._cond = threading.Condition()
        self.received = bytearray()
        self.arrivals = []  # (time, bytes) per recv
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError:
                return
            if not data:
                return
            with self._cond:
                self.received += data
                self.arrivals.append((time.monotonic(), data))
                self._cond.notify_all()

    def send(self, *commands: str) -> None:
        self.sock.sendall(b"".join(c.encode() + TERMINATOR for c in commands))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def mark(self) -> int:
        with self._cond:
            return len(self.received)

    def wait_bytes(self, count: int, since: int = 0, timeout: float = 5) -> bytes:
        with self._cond:
            if not self._cond.wait_for(lambda: len(self.received) - since >= count, timeout):
                raise TimeoutError("got %r" % bytes(self.received[since:]))
            return bytes(self.received[since:since + count])

    def frames(self, since: int = 0):
        with self._cond:
            data = bytes(self.received[since:])
        return [f for f in data.split(TERMINATOR)[:-1]]

    def wait_frames(self, count: int, since: int = 0, timeout: float = 5):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                frames = bytes(self.received[since:]).split(TERMINATOR)[:-1]
                if len(frames) >= count:
                    return frames[:count]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("wanted %d frames, got %r" % (count, frames))
                self._cond.wait(remaining)

    def quiet(self, seconds: float, since: int = None) -> bytes:
        """Wait, then return what arrived meanwhile."""
        start = self.mark() if since is None else since
        time.sleep(seconds)
        with self._cond:
            return bytes(self.received[start:])

    def frame_time(self, index: int, since: int = 0) -> float:
        """When the end of reply frame `index` (counting from `since`) arrived."""
        with self._cond:
            target = bytes(self.received[since:])
        end = 0
        for _ in range(index + 1):
            end = target.index(TERMINATOR, end) + len(TERMINATOR)
        absolute = since + end
        seen = 0
        for t, data in self.arrivals:
            seen += len(data)
            if seen >= absolute:
                return t
        raise ValueError("frame not received")


@pytest.fixture
def pair():
    """(display, mcu), connected over a socket pair."""
    a, b = socket.socketpair()
    display = Display(SocketTransport(a))
    mcu = Mcu(b)
    deadline = time.monotonic() + 2
    while not display.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    yield display, mcu
    display.close()
    b.close()


@pytest.fixture
def display(pair):
    return pair[0]


@pytest.fixture
def mcu(pair):
    return pair[1]
