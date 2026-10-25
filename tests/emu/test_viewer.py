"""The browser viewer, driven over HTTP as its page drives it."""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from conftest import Mcu  # noqa: E402
from hmi_builder import build, component, page  # noqa: E402
from nextion_tools.emu import Display, SocketTransport  # noqa: E402
from nextion_tools.emu.viewer import Viewer  # noqa: E402
from nextion_tools.hmi import Project  # noqa: E402

HMI = Project.parse(build([
    page("p", [
        # A button in the top left quarter of a 40 x 20 screen.
        component("b", 98, {"x": 0, "y": 0, "w": 20, "h": 10, "sendkey": 3, "sta": 1, "bco": 0xF800,
                            "bco2": 0x001F, "txt": "", "font": 0}),
    ]),
]))
HMI.pages[0].components[0].attrs.update(x=0, y=0, w=40, h=20, sta=1, bco=0)


@pytest.fixture(params=[0, 90])
def rig(request):
    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=HMI, rotation=request.param)
    mcu = Mcu(b)
    viewer = Viewer(display)
    time.sleep(0.05)
    yield display, mcu, viewer
    viewer.close()
    display.close()
    b.close()


def get(viewer, path):
    with urllib.request.urlopen(viewer.url.rstrip("/") + path, timeout=5) as r:
        return r.status, r.headers.get_content_type(), r.read()


def touch(viewer, kind, x, y):
    req = urllib.request.Request(viewer.url + "touch", data=json.dumps({"kind": kind, "x": x, "y": y}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def test_page_state_and_frame(rig):
    display, mcu, viewer = rig
    status, kind, body = get(viewer, "/")
    assert status == 200 and kind == "text/html" and b"<canvas" not in body and b"frame.png" in body
    state = json.loads(get(viewer, "/state")[2])
    assert state["page"] == "p" and state["rotation"] == display.rotation and state["error_count"] == 0
    status, kind, png = get(viewer, "/frame.png")
    assert kind == "image/png" and png.startswith(b"\x89PNG")
    from PIL import Image
    import io
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == ((40, 20) if display.rotation == 0 else (20, 40))


def test_a_click_is_a_touch_where_it_appears(rig):
    display, mcu, viewer = rig
    # The button's centre, (10, 5), as it appears after the display's rotation.
    x, y = (10, 5) if display.rotation == 0 else (14, 10)
    mark = mcu.mark()
    assert touch(viewer, "down", x, y) == {"ok": True}
    assert display.scene()["pressed"] == "b"
    assert touch(viewer, "up", x, y) == {"ok": True}
    assert mcu.wait_frames(2, since=mark) == [b"\x65\x00\x01\x01", b"\x65\x00\x01\x00"]


def test_a_new_frame_only_when_something_changed(rig):
    display, mcu, viewer = rig
    first = viewer.frame()
    assert viewer.frame() is first
    mcu.send("b.bco=2016")
    display.wait_for(lambda d: d.value("p.b.bco") == 2016)
    assert viewer.frame() != first


def test_bad_touches_are_refused(rig):
    display, mcu, viewer = rig
    with pytest.raises(urllib.error.HTTPError) as e:
        touch(viewer, "wiggle", 1, 1)
    assert e.value.code == 409
