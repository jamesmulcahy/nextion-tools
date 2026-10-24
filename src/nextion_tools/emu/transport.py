"""Where the display's UART goes: a TCP socket (QEMU's `-serial tcp:`), a pty, or a serial port.

A transport delivers what the MCU writes to `on_data`, and sends what the display writes. It
calls `on_connect` / `on_disconnect` as the MCU side comes and goes; a TCP listener accepts again
after a disconnect, so QEMU can be restarted against the same display.
"""

import logging
import os
import socket
import threading
from typing import Callable, Optional

log = logging.getLogger("nextion_tools.emu.transport")

OnData = Callable[[bytes], None]
OnEvent = Callable[[], None]


class NotConnected(RuntimeError):
    pass


class Transport:
    def __init__(self):
        self._on_data: OnData = lambda data: None
        self._on_connect: OnEvent = lambda: None
        self._on_disconnect: OnEvent = lambda: None
        self._send_lock = threading.Lock()

    def start(self, on_data: OnData, on_connect: Optional[OnEvent] = None,
              on_disconnect: Optional[OnEvent] = None) -> None:
        self._on_data = on_data
        self._on_connect = on_connect or (lambda: None)
        self._on_disconnect = on_disconnect or (lambda: None)
        self._start()

    def _start(self) -> None:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _thread(self, target, name: str) -> None:
        threading.Thread(target=target, name=name, daemon=True).start()


class _SocketMixin:
    _sock: Optional[socket.socket] = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def send(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise NotConnected("the MCU side is not connected")
        with self._send_lock:
            sock.sendall(data)

    def _serve(self, sock: socket.socket) -> None:
        self._sock = sock
        self._on_connect()
        try:
            while True:
                try:
                    data = sock.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                self._on_data(data)
        finally:
            if self._sock is sock:
                self._sock = None
            self._on_disconnect()


class TcpServer(_SocketMixin, Transport):
    """Listens on host:port (port 0 picks one; see `.port`) and serves one MCU at a time."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        super().__init__()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(1)
        self.host = host
        self.port = self._server.getsockname()[1]

    def _start(self) -> None:
        self._thread(self._accept_loop, "nextion-tcp")

    def _accept_loop(self) -> None:
        while True:
            try:
                sock, _ = self._server.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._serve(sock)

    def close(self) -> None:
        for s in (self._sock, self._server):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass


class SocketTransport(_SocketMixin, Transport):
    """An already connected socket, e.g. one end of socket.socketpair()."""

    def __init__(self, sock: socket.socket):
        super().__init__()
        self._given = sock

    def _start(self) -> None:
        self._thread(lambda: self._serve(self._given), "nextion-socket")

    def close(self) -> None:
        try:
            self._given.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._given.close()


class Pty(Transport):
    """A pseudo-terminal. Point the MCU side (a program, or QEMU's `-serial /dev/pts/N`) at
    `.path`. Both ends are raw: no echo, no line discipline."""

    def __init__(self):
        super().__init__()
        import tty
        self._master, self._slave = os.openpty()
        tty.setraw(self._master)
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        self._closed = False

    def _start(self) -> None:
        self._thread(self._read_loop, "nextion-pty")
        self._on_connect()

    @property
    def connected(self) -> bool:
        return not self._closed

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                data = os.read(self._master, 4096)
            except OSError:
                break
            if not data:
                break
            self._on_data(data)

    def send(self, data: bytes) -> None:
        if self._closed:
            raise NotConnected("pty is closed")
        with self._send_lock:
            view = memoryview(data)
            while view:
                view = view[os.write(self._master, view):]

    def close(self) -> None:
        self._closed = True
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass


class Serial(Transport):
    """A real serial port, through pyserial (`pip install nextion-emu[serial]`)."""

    def __init__(self, device: str, baud: int = 115200):
        super().__init__()
        import serial  # noqa: optional dependency
        self._port = serial.Serial(device, baud, timeout=0.1)
        self.device = device

    def _start(self) -> None:
        self._thread(self._read_loop, "nextion-serial")
        self._on_connect()

    @property
    def connected(self) -> bool:
        return self._port.is_open

    def _read_loop(self) -> None:
        while self._port.is_open:
            try:
                data = self._port.read(4096)
            except Exception:  # pyserial raises its own SerialException on close
                break
            if data:
                self._on_data(data)

    def send(self, data: bytes) -> None:
        if not self._port.is_open:
            raise NotConnected("serial port is closed")
        with self._send_lock:
            self._port.write(data)

    def set_baud(self, baud: int) -> None:
        self._port.baudrate = baud

    def close(self) -> None:
        self._port.close()
