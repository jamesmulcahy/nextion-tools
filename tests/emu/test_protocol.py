import pytest

from nextion_tools.emu import protocol
from nextion_tools.emu.protocol import Framer, parse, parse_value


@pytest.mark.parametrize("text, kind, name, op, value", [
    ('home.room.txt="Kitchen"', "assign", "home.room.txt", "=", '"Kitchen"'),
    ("b_scenes.pic=59", "assign", "b_scenes.pic", "=", "59"),
    ("bkcmd=3", "assign", "bkcmd", "=", "3"),
    ("dim=100", "assign", "dim", "=", "100"),
    ("va0.val+=5", "assign", "va0.val", "+=", "5"),
    ("t0.txt+=\"x\"", "assign", "t0.txt", "+=", '"x"'),
    ("sys0-=1", "assign", "sys0", "-=", "1"),
    ("n0.val*=2", "assign", "n0.val", "*=", "2"),
    ("n0.val/=2", "assign", "n0.val", "/=", "2"),
    ("n0.val%=2", "assign", "n0.val", "%=", "2"),
    ("t0.txt=\"a=b\"", "assign", "t0.txt", "=", '"a=b"'),
    ("b[3].val=1", "assign", "b[3].val", "=", "1"),
])
def test_assignments(text, kind, name, op, value):
    ins = parse(text)
    assert (ins.kind, ins.name, ins.op, ins.value) == (kind, name, op, value)


@pytest.mark.parametrize("text, op", [("gest_time++", "++"), ("n0.val--", "--")])
def test_increments(text, op):
    ins = parse(text)
    assert (ins.kind, ins.op) == ("increment", op)


@pytest.mark.parametrize("text, keyword, args", [
    ("page home", "page", "home"),
    ("page 2", "page", "2"),
    ("vis b_x,0", "vis", "b_x,0"),
    ("get home.s_brightness.val", "get", "home.s_brightness.val"),
    ("printh a5 01 ff ff ff", "printh", "a5 01 ff ff ff"),
    ("fill 0,0,480,320,0", "fill", "0,0,480,320,0"),
    ("ref 0", "ref", "0"),
    ("cls BLACK", "cls", "BLACK"),
    ("connect", "connect", ""),
    ("rest", "rest", ""),
    ("whmi-wri 100,115200,0", "whmi-wri", "100,115200,0"),
    ("whmi-wris 100,115200,0", "whmi-wris", "100,115200,0"),
])
def test_keywords(text, keyword, args):
    ins = parse(text)
    assert (ins.kind, ins.keyword, ins.args) == ("keyword", keyword, args)


@pytest.mark.parametrize("text", [
    "DRAKJHSUYDGBNCJHGJKSHBDN",  # the firmware's "reset" jibberish
    "",
    "nosuchcommand 1,2",
    "=5",
    "1abc=2",
    "x+",
])
def test_bad_syntax(text):
    assert parse(text).kind == "invalid"


def test_parse_value():
    assert parse_value('"Kitchen"') == "Kitchen"
    assert parse_value('"say \\"hi\\""') == 'say "hi"'
    assert parse_value('"two\\rlines"') == "two\rlines"
    assert parse_value("42") == 42
    assert parse_value(" -7 ") == -7
    assert parse_value("sya0") == "sya0"  # an expression, kept as text
    assert parse_value('""') == ""


def test_frames():
    assert protocol.frame(protocol.CMD_OK) == b"\x01\xff\xff\xff"
    assert protocol.number(1) == b"\x71\x01\x00\x00\x00\xff\xff\xff"
    assert protocol.number(-1) == b"\x71\xff\xff\xff\xff\xff\xff\xff"
    assert protocol.string("°C") == b"\x70" + "°C".encode() + b"\xff\xff\xff"


def test_printh_bytes():
    assert protocol.printh_bytes("92 FF FF FF") == b"\x92\xff\xff\xff"
    assert protocol.printh_bytes("a501") == b"\xa5\x01"
    with pytest.raises(ValueError):
        protocol.printh_bytes("9")
    with pytest.raises(ValueError):
        protocol.printh_bytes("zz")


def test_bkcmd_levels():
    assert [protocol.reports_success(b) for b in range(4)] == [False, True, False, True]
    assert [protocol.reports_failure(b) for b in range(4)] == [False, False, True, True]
    assert protocol.BKCMD_DEFAULT == 2


def test_framer_splits_across_reads():
    f = Framer()
    assert f.feed(b"page ho") == []
    assert f.feed(b"me\xff\xff") == []
    assert f.feed(b"\xffvis a,1\xff\xff\xffget") == [b"page home", b"vis a,1"]
    assert f.take_rest() == b"get"
    assert f.take_rest() == b""
