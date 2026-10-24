"""Reading .HMI projects: built ones here, and a real one when NEXTION_TOOLS_HMI names it."""

import os
import struct

import pytest

from hmi_builder import build, component, page
from nextion_tools.hmi import PAGE_TYPE, Project

REAL_HMI = os.environ.get("NEXTION_TOOLS_HMI")


@pytest.fixture
def project():
    return Project.parse(build([
        page("page0"),
        page("home", [
            component("t_time", 116, {"txt": "12:00", "vscope": 1, "font": 0, "x": 5}),
            component("b_scenes", 98, {"txt": "", "pic": 59, "sendkey": 1}, {"up": ["printh 99 FF FF FF"]}),
            component("tm0", 51, {"tim": 50, "en": 0}, {"timer": ["tm0.en=0", "if(tch1!=0)", "{", "}"]}),
        ], events={"load": ["vis p_x,0"]}),
        page("screensaver", [component("curTemp", 116, {"txt": "-°", "vscope": 1})]),
    ], pictures=3, fonts=2, stale=["1.pa", "9.zi"]))


def test_pages_in_main_hmi_order_whatever_their_block_names(project):
    assert [(p.id, p.name) for p in project.pages] == [(0, "page0"), (1, "home"), (2, "screensaver")]
    assert project.page("home").id == 1
    assert project.page_by_id(2).name == "screensaver"
    assert project.find_page("nowhere") is None
    with pytest.raises(KeyError, match="no page with id 9"):
        project.page_by_id(9)
    with pytest.raises(KeyError, match="no page nowhere"):
        project.page("nowhere")


def test_resources(project):
    assert project.picture_count == 3
    assert project.font_count == 2


def test_components_ids_types_and_attributes(project):
    home = project.page("home")
    assert [c.name for c in home.components] == ["home", "t_time", "b_scenes", "tm0"]
    assert home.components[0].type == PAGE_TYPE
    t_time = home.component("t_time")
    assert (t_time.id, t_time.kind, t_time.is_global) == (1, "text", True)
    assert t_time.attrs["txt"] == "12:00" and t_time.attrs["x"] == 5
    assert home.by_id(2) is home.component("b_scenes")
    assert home.by_id(99) is None and home.by_id(-1) is None
    assert home.find("nope") is None
    with pytest.raises(KeyError, match="no component nope"):
        home.component("nope")


def test_text_is_utf8(project):
    assert project.page("screensaver").component("curTemp").attrs["txt"] == "-°"


def test_events(project):
    home = project.page("home")
    assert home.events == {"load": ["vis p_x,0"]}
    assert home.component("b_scenes").events == {"up": ["printh 99 FF FF FF"]}
    assert home.component("tm0").events["timer"] == ["tm0.en=0", "if(tch1!=0)", "{", "}"]
    # Code is not attributes, even lines 16 characters or longer.
    assert set(home.component("tm0").attrs) == {"objname", "type", "id", "tim", "en"}


def test_sendkey_edges(project):
    b = project.page("home").component("b_scenes")
    assert b.reports_release and not b.reports_press
    assert not project.page("home").component("t_time").reports_release


def test_stale_entries_are_ignored(project):
    assert len(project.pages) == 3  # the stale 1.pa holds garbage and would not parse


def test_not_an_hmi():
    with pytest.raises(ValueError, match="main.HMI"):
        Project.parse(struct.pack("<I", 0) + b"\0" * 64)


def test_a_listed_page_that_is_missing():
    data = bytearray(build([page("home")]))
    # Rename the page's directory entry so main.HMI's listing no longer finds it.
    i = data.index(b"0.pa\0")
    data[i:i + 4] = b"7.pa"
    with pytest.raises(ValueError, match="does not hold"):
        Project.parse(bytes(data))


def test_a_corrupt_page():
    data = bytearray(build([page("home", [component("t0", 116, {"txt": "x"})])]))
    i = data.index(b"objname")
    struct.pack_into("<I", data, i - 4, 2000)  # a record running past its component
    with pytest.raises((ValueError, struct.error)):
        Project.parse(bytes(data))


def test_load_from_a_file(tmp_path):
    path = tmp_path / "gui.HMI"
    path.write_bytes(build([page("home")]))
    project = Project.load(str(path))
    assert project.path == str(path) and project.page("home").id == 0


# ---- the real thing -------------------------------------------------------------------------

real = pytest.mark.skipif(not REAL_HMI, reason="set NEXTION_TOOLS_HMI to a gui.HMI")


@pytest.fixture(scope="module")
def nspm():
    return Project.load(REAL_HMI)


@real
def test_real_pages(nspm):
    assert [p.name for p in nspm.pages][:4] == ["page0", "bootscreen", "home", "screensaver"]
    assert nspm.page("thermostat").id == 11
    assert nspm.picture_count > 100 and nspm.font_count == 17


@real
def test_real_component_ids_are_positions(nspm):
    for p in nspm.pages:
        assert p.components[0].type == PAGE_TYPE and p.components[0].name == p.name
        for i, c in enumerate(p.components):
            assert c.id == i, (p.name, c.name)


@real
def test_real_code(nspm):
    home = nspm.page("home")
    assert home.component("sleep_display").events["timer"] == ["printh 92 FF FF FF"]
    assert home.events["load"] == ["vis p_lockceiling,0", "vis p_locktable,0"]


@real
def test_real_scopes(nspm):
    assert nspm.page("screensaver").component("forDay1").is_global
    assert not nspm.page("home").component("h_ceiling").is_global
    assert nspm.page("home").component("h_ceiling").id == 22
