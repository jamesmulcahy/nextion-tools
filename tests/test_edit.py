"""Editing numeric attributes in place, on a project built in code."""

import pytest

from hmi_builder import build, component, page
from nextion_tools import hmi
from nextion_tools.hmi import Project


@pytest.fixture
def project():
    return Project.parse(build([
        page("page0"),
        page("items", [
            component("slider_save", 106, {"x": 93, "y": 46, "w": 184, "h": 3, "endx": 276, "endy": 48, "val": 0}),
            component("t0", 116, {"txt": "hi"}),
        ]),
    ], stale=("junk.pa",)))


def test_edit_changes_only_the_attribute_its_end_and_the_stamp(project):
    p = project.page("items")
    comp = p.component("slider_save")
    data, changes = hmi.apply_edits(project, [("items", "slider_save", "x", 96)])
    assert changes == [("items", "slider_save", "x", 93, 96), ("items", "slider_save", "endx", 276, 279)]
    e = p.entry
    changed = {i - e.offset for i in range(len(data)) if data[i] != project.data[i]}
    allowed = set(range(4))
    for attr in ("x", "endx"):
        o, n = comp.fields[attr]
        allowed |= set(range(o, o + n))
    assert changed and changed <= allowed
    edited = Project.parse(data).page("items").component("slider_save")
    assert (edited.attrs["x"], edited.attrs["endx"]) == (96, 279)


def test_edit_and_back_restores_the_file(project):
    once, _ = hmi.apply_edits(project, [("items", "slider_save", "x", 96)])
    twice, _ = hmi.apply_edits(Project.parse(once), [("items", "slider_save", "x", 93)])
    assert twice == project.data


def test_restamping_an_unchanged_block_is_a_no_op(project):
    block = project.page("items").block
    assert hmi.restamp(block, block) == block


@pytest.mark.parametrize("attr, value", [("objname", 1), ("endx", 10), ("x", 1 << 32), ("x", -1), ("nonesuch", 1)])
def test_refuses_strings_derived_attributes_and_values_that_do_not_fit(project, attr, value):
    with pytest.raises(ValueError):
        hmi.apply_edits(project, [("items", "slider_save", attr, value)])


def test_refuses_unknown_names(project):
    with pytest.raises(KeyError):
        hmi.apply_edits(project, [("items", "nonesuch", "x", 1)])
    with pytest.raises(KeyError):
        hmi.apply_edits(project, [("nonesuch", "slider_save", "x", 1)])


def test_check_written(project, tmp_path):
    edits = [("items", "slider_save", "x", 96)]
    data, _ = hmi.apply_edits(project, edits)
    path = tmp_path / "out.HMI"
    path.write_bytes(data)
    hmi.check_written(project, str(path), edits)
    path.write_bytes(project.data)
    with pytest.raises(RuntimeError, match="read back"):
        hmi.check_written(project, str(path), edits)
