"""demo/demo.HMI and demo/demo.tft: a project made in the Nextion Editor (2026), at display
direction 90, with a font of the newer format and a raw picture."""

import io
import os

import pytest

from nextion_tools import tft
from nextion_tools.hmi import Project

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo")


@pytest.fixture(scope="module")
def project():
    return Project.load(os.path.join(DEMO, "demo.HMI"))


@pytest.fixture(scope="module")
def tft_data():
    with open(os.path.join(DEMO, "demo.tft"), "rb") as f:
        return f.read()


def test_pages_program_and_direction(project):
    assert [p.name for p in project.pages] == ["dummy", "menu", "dial", "paint", "pong"]
    assert project.direction == 90
    assert "thup=1" in project.program and any(line.startswith("page 1") for line in project.program)
    b_up = project.page("dial").component("b_up")
    assert b_up.reports_release and not b_up.reports_press
    assert "j0.val+=10" in [line.strip() for line in b_up.events["down"]]


def test_the_tft_matches_the_project(project, tft_data):
    assert tft.checksums_ok(tft_data)
    assert tft.orientation(tft_data) == project.direction
    assert tft.records(tft_data) == project.pictures()
    # A raw picture with no record header, which `raw` does not claim to decode.
    (picture,) = tft.pictures(tft_data)
    assert picture.size == 2 * picture.w * picture.h and not picture.raw


def _ink_box(coverage):
    import numpy as np
    ys, xs = np.nonzero(coverage)
    return xs.max() - xs.min() + 1, ys.max() - ys.min() + 1


def test_newer_fonts_store_glyphs_turned_by_the_direction(project):
    pytest.importorskip("numpy")
    from nextion_tools import fonts

    (_, block), = project.font_blocks()
    assert block[3] == fonts.ROTATING_FORMAT
    assert fonts.stored_turned(block, project.direction) == 90
    font = project.fonts()[0]
    assert font.turned == 90 and font.height == 40 and font.encoding == "iso-8859-1"
    w, h = _ink_box(font.glyph("l").coverage)
    assert h > 3 * w  # upright: a tall stroke
    w, h = _ink_box(fonts.parse(block).glyph("l").coverage)
    assert not h > 3 * w  # read as if stored upright, it is scrambled


def test_older_fonts_are_upright_at_any_direction():
    pytest.importorskip("numpy")
    from nextion_tools import fonts

    assert fonts.stored_turned(b"\x04\xff\x00\x0a", 270) == 0
    assert fonts.stored_turned(b"\x04\xff\x00\x0b", 270) == 270


def test_the_tft_stores_pictures_turned_as_native_rotation_says(project, tft_data):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    from nextion_tools import render

    (picture,) = tft.pictures(tft_data)
    stored = np.frombuffer(tft_data[picture.start:picture.start + picture.size], dtype="<u2")
    upright = np.array(Image.open(io.BytesIO(project.picture(0).original)).convert("RGB")).astype(np.uint16)
    rgb565 = ((upright[..., 0] >> 3) << 11) | ((upright[..., 1] >> 2) << 5) | (upright[..., 2] >> 3)
    turned = render.rotate(rgb565, render.native_rotation(tft.orientation(tft_data)))
    assert (turned.ravel() == stored).mean() > 0.9  # the rest differ by rounding


def test_every_page_renders_with_its_text(project):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from nextion_tools.render import Renderer

    renderer = Renderer(project)
    for page in project.pages:
        assert renderer.render(page.name).shape == (320, 480, 3)
    fb = renderer.render("menu")
    x, y, w, h = project.page("menu").component("b_dial").rect
    label = fb[y:y + h, x:x + w]
    assert (label.max(axis=2) < 64).sum() > 200  # "Thermostat", in black
    assert not renderer.mismatches
