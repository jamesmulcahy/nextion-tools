"""Drawing pages from a project built in code."""

import socket
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from hmi_builder import build, component, make_zi, page, picture_block  # noqa: E402
from nextion_tools import render  # noqa: E402
from nextion_tools.hmi import Project  # noqa: E402
from nextion_tools.render import Renderer, rgb  # noqa: E402

RED, GREEN, BLUE, WHITE, BLACK = 0xF800, 0x07E0, 0x001F, 0xFFFF, 0x0000

# A 2-pixel-high font: "a" fills a 2 x 2 box, "b" a 3 x 2 box with a 1-pixel advance.
FONT = make_zi("tiny", 2, [
    (ord("a"), 2, 0, 0, bytes([0x01, 0b00_1_00100])),
    (ord("1"), 2, 0, 0, bytes([0x01, 0b00_1_00100])),
])


def area(x, y, w, h, **attrs):
    attrs.update(x=x, y=y, w=w, h=h)
    return attrs


@pytest.fixture(scope="module")
def project():
    pages = [
        page("p", [
            component("bg", 112, area(0, 0, 8, 6, pic=0)),
            component("crop", 113, area(2, 2, 2, 2, picc=1)),
            component("t", 116, area(0, 0, 4, 2, sta=1, bco=BLACK, pco=WHITE, font=0, txt="a", xcen=1, ycen=0)),
            component("n", 54, area(4, 4, 4, 2, sta=3, pco=RED, font=0, val=1, xcen=2, ycen=0)),
        ]),
        page("q", [
            component("b2", 53, area(0, 0, 4, 2, sta=1, bco=RED, bco2=GREEN, val=0, txt="", font=0)),
            component("btn", 98, area(4, 0, 4, 2, sta=1, bco=RED, bco2=BLUE, txt="", font=0)),
            component("bar", 106, area(0, 2, 8, 1, sta=0, bco=BLACK, pco=GREEN, val=50)),
            component("s", 1, area(0, 4, 8, 2, sta=1, bco=BLACK, bco1=BLUE, val=50, minval=0, maxval=100,
                                   wid=2, hig=2, pco=WHITE, psta=0, mode=0)),
        ]),
    ]
    data = build(pages, pictures=[picture_block(8, 6, BLUE), picture_block(8, 6, GREEN)], fonts=[FONT])
    project = Project.parse(data)
    for p in project.pages:  # an 8 x 6 screen
        p.components[0].attrs.update(x=0, y=0, w=8, h=6, sta=1, bco=BLACK)
    return project


def colour(fb, x, y):
    return tuple(int(c) for c in fb[y, x])


def test_pictures_crops_text_and_numbers(project):
    fb = Renderer(project).render("p")
    assert fb.shape == (6, 8, 3)
    assert colour(fb, 7, 0) == rgb(BLUE)  # the picture
    assert colour(fb, 2, 2) == rgb(GREEN)  # picture 1 cropped to the component
    assert colour(fb, 0, 1) == rgb(BLACK)  # the text's solid background
    assert colour(fb, 1, 0) == rgb(WHITE) and colour(fb, 2, 1) == rgb(WHITE)  # "a", centred
    assert colour(fb, 6, 4) == rgb(RED) and colour(fb, 7, 5) == rgb(RED)  # "1", right-aligned
    assert colour(fb, 4, 4) == rgb(BLUE)  # sta 3: no background, the picture shows through


def test_values_override_the_hmi(project):
    fb = Renderer(project).render("p", values={"t": {"txt": "", "bco": RED}, "crop": {"vis": 0}})
    assert colour(fb, 1, 0) == rgb(RED)
    assert colour(fb, 2, 2) == rgb(BLUE)  # the crop is hidden


def test_dual_state_button_pressed_button_bar_and_slider(project):
    r = Renderer(project)
    fb = r.render("q")
    assert colour(fb, 0, 0) == rgb(RED) and colour(fb, 4, 0) == rgb(RED)
    assert colour(fb, 3, 2) == rgb(GREEN) and colour(fb, 4, 2) == rgb(BLACK)  # 50 %
    assert colour(fb, 1, 4) == rgb(BLUE) and colour(fb, 7, 4) == rgb(BLACK)  # slider fill
    assert colour(fb, 4, 4) == rgb(WHITE)  # the cursor
    fb = r.render("q", values={"b2": {"val": 1}}, pressed="btn")
    assert colour(fb, 0, 0) == rgb(GREEN) and colour(fb, 4, 0) == rgb(BLUE)


def test_drawing_instructions(project):
    fb = Renderer(project).render("q", drawn=["fill 0,0,2,1,WHITE", "fill 6,5,9,9,%d" % GREEN, "nonsense 1,2"])
    assert colour(fb, 1, 0) == rgb(WHITE) and colour(fb, 2, 0) == rgb(RED)
    assert colour(fb, 7, 5) == rgb(GREEN)
    assert colour(Renderer(project).render("q", drawn=["cls BLUE"]), 3, 3) == rgb(BLUE)


def test_lines_rectangles_and_circles(project):
    def ink(drawn):
        fb = Renderer(project).render("q", drawn=["cls BLACK"] + drawn)
        return {(x, y) for y in range(fb.shape[0]) for x in range(fb.shape[1]) if colour(fb, x, y) != rgb(BLACK)}

    assert ink(["line 0,0,3,3,WHITE"]) == {(0, 0), (1, 1), (2, 2), (3, 3)}
    assert ink(["line 1,5,6,5,WHITE"]) == {(x, 5) for x in range(1, 7)}
    box = ink(["draw 1,1,4,3,WHITE"])
    assert box == {(x, y) for x in range(1, 5) for y in range(1, 4)} - {(2, 2), (3, 2)}
    dot = ink(["cirs 3,3,1,WHITE"])  # whether the corners are in is not known for a real display
    assert {(3, 2), (2, 3), (3, 3), (4, 3), (3, 4)} <= dot <= {(x, y) for x in (2, 3, 4) for y in (2, 3, 4)}
    ring = ink(["cir 3,3,2,WHITE"])
    assert (3, 3) not in ring and {(1, 3), (5, 3), (3, 1), (3, 5)} <= ring
    assert ink(["cirs 7,5,3,WHITE"]) <= {(x, y) for x in range(8) for y in range(6)}  # clipped, not wrapped


def test_a_font_much_taller_than_its_field_is_replaced_and_recorded():
    big = make_zi("big", 9, [(ord("a"), 2, 0, 0, bytes([0x01]))])
    hmi = Project.parse(build([page("p", [component("t", 116, area(0, 0, 4, 2, sta=1, font=1, txt="a"))])],
                              fonts=[FONT, big]))
    r = Renderer(hmi)
    r.render("p")
    assert r.mismatches == {("p", "t", 1)}


def test_rotation():
    fb = np.zeros((2, 3, 3), dtype=np.uint8)
    fb[0, 2] = 255  # top right of a 3 x 2 page
    assert render.rotate(fb, 90).shape == (3, 2, 3)
    for rotation in (0, 90, 180, 270):
        turned = render.rotate(fb, rotation)
        ys, xs = np.nonzero(turned[..., 0])
        assert render.unrotate_point(int(xs[0]), int(ys[0]), rotation, (3, 2)) == (2, 0)
        assert render.rotated_size((3, 2), rotation) == turned.shape[1::-1]
    with pytest.raises(ValueError):
        render.rotate(fb, 45)
    assert render.native_rotation(90) == 90 and render.native_rotation(0) == 0


def test_display_snapshot_follows_what_the_mcu_sends(project, tmp_path):
    from conftest import Mcu
    from nextion_tools.emu import Display, SocketTransport

    a, b = socket.socketpair()
    display = Display(SocketTransport(a), hmi=project, rotation=90)
    mcu = Mcu(b)
    try:
        time.sleep(0.05)
        before = display.version
        mcu.send("page q", "b2.val=1", "fill 7,5,1,1,WHITE")
        display.wait_for_command("^fill")
        assert display.version > before
        fb = display.snapshot(rotation=0)
        assert colour(fb, 0, 0) == rgb(GREEN) and colour(fb, 7, 5) == rgb(WHITE)
        assert display.snapshot().shape == (8, 6, 3)  # turned 90 degrees
        mcu.send("page p")
        display.wait_for_page("p")
        assert display.drawn == []  # a page change clears what was drawn
        path = display.save_png(str(tmp_path / "p.png"))
        from PIL import Image
        with Image.open(path) as im:
            assert im.size == (6, 8)
    finally:
        display.close()
        b.close()


REAL_HMI = __import__("os").environ.get("NEXTION_TOOLS_HMI")


@pytest.mark.skipif(not REAL_HMI, reason="set NEXTION_TOOLS_HMI to a gui.HMI")
def test_every_page_of_a_real_project_renders():
    project = Project.load(REAL_HMI)
    r = Renderer(project)
    for p in project.pages:
        fb = r.render(p.name, values={c.name: {"txt": "Ag 12"} for c in p.components if "txt" in c.attrs})
        w, h = p.size
        assert fb.shape == (h, w, 3), p.name
        if len(p.components) > 1:
            assert fb.any(), p.name  # something was drawn
