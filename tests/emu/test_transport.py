import os
import select
import socket
import subprocess
import sys
import time

import pytest

from nextion_tools.emu import COMOK, Display, NotConnected, Pty, TcpServer
from nextion_tools.emu.protocol import TERMINATOR


def read_until(fd_read, want: bytes, timeout=3.0) -> bytes:
    deadline = time.monotonic() + timeout
    got = b""
    while want not in got:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(got)
        got += fd_read(remaining)
    return got


def test_tcp_server_serves_and_accepts_again():
    display = Display(TcpServer())
    try:
        for _ in range(2):  # QEMU restarts connect again
            client = socket.create_connection(("127.0.0.1", display.transport.port))
            client.sendall(b"connect" + TERMINATOR)
            client.settimeout(3)
            assert read_until(lambda t: client.recv(4096), COMOK) == COMOK + TERMINATOR
            assert display.connected
            client.shutdown(socket.SHUT_RDWR)
            client.close()
            display.wait_for(lambda d: not d.connected, timeout=3)
        assert display.commands == ["connect", "connect"]
    finally:
        display.close()


def test_tcp_server_without_a_client():
    display = Display(TcpServer())
    try:
        assert not display.connected
        with pytest.raises(NotConnected):
            display.touch(1, True)
    finally:
        display.close()


def test_default_transport_is_a_tcp_server():
    display = Display()
    try:
        assert isinstance(display.transport, TcpServer)
        assert display.transport.port > 0
    finally:
        display.close()


def test_pty_round_trip():
    transport = Pty()
    display = Display(transport)
    fd = os.open(transport.path, os.O_RDWR | os.O_NOCTTY)
    try:
        os.write(fd, b"connect" + TERMINATOR)

        def read(t):
            ready, _, _ = select.select([fd], [], [], t)
            return os.read(fd, 4096) if ready else b""

        assert read_until(read, COMOK) == COMOK + TERMINATOR
        display.send_raw(b"\x00\xff\xfe")  # raw bytes survive: no line discipline
        assert read_until(read, b"\x00\xff\xfe") == b"\x00\xff\xfe"
    finally:
        os.close(fd)
        display.close()


def test_serial_over_a_pty():
    serial = pytest.importorskip("serial")
    from nextion_tools.emu import Serial
    pty = Pty()
    display = Display(Serial(pty.path))
    peer = Display(pty)  # the "MCU": a second emulator on the other end, used as a raw pipe
    try:
        received = []
        peer._ingest = lambda data: received.append(data)  # record instead of executing
        display.send_raw(b"hello")
        deadline = time.monotonic() + 3
        while b"".join(received) != b"hello" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert b"".join(received) == b"hello"
    finally:
        display.close()
        peer.close()


def test_cli_help():
    out = subprocess.run([sys.executable, "-m", "nextion_tools.emu", "--help"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "--tcp" in out.stdout and "--pty" in out.stdout


def test_cli_runs_a_display_on_tcp():
    proc = subprocess.Popen([sys.executable, "-m", "nextion_tools.emu", "--tcp", "0", "--banner", "NSPM"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        line = proc.stdout.readline()
        port = int(line.rsplit(":", 1)[1])
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        # The display powers on once the MCU is there, and its TFT prints the banner.
        assert read_until(lambda t: client.recv(4096), b"NSPM" + TERMINATOR) == b"NSPM" + TERMINATOR
        client.sendall(b"connect" + TERMINATOR)
        assert read_until(lambda t: client.recv(4096), COMOK) == COMOK + TERMINATOR
        client.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
