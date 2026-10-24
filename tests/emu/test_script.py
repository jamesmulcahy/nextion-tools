"""The script compiler and interpreter on their own, against a dictionary for a display."""

import pytest

from nextion_tools.emu.script import (Assign, Host, If, Interpreter, ScriptError, ScriptSyntaxError, Stmt, compile_code,
                                strip_comment)


class FakeHost(Host):
    def __init__(self, values=None, components=None):
        self.values = dict(values or {})
        self.components = components or {}  # id -> name
        self.out = []
        self.failures = []

    def read(self, name):
        if name not in self.values:
            raise ScriptError("no " + name)
        return self.values[name]

    def write(self, name, value):
        self.values[name] = value

    def component_name(self, component_id):
        if component_id not in self.components:
            raise ScriptError("no id %d" % component_id)
        return self.components[component_id]

    def declare(self, name, value):
        self.values[name] = value

    def instruction(self, keyword, *args):
        self.out.append((keyword,) + args)

    def failed(self, stmt: Stmt, error):
        self.failures.append(stmt.text)


def run(lines, host=None, program=False):
    host = host or FakeHost()
    Interpreter(host).run(compile_code(lines, "test", program=program))
    return host


# ---- compiling --------------------------------------------------------------------------------

def test_comments_are_stripped_outside_strings():
    assert strip_comment('t0.txt="a//b" // note') == 't0.txt="a//b" '
    assert strip_comment("//whole line") == ""


def test_if_else_if_else_chain():
    code = compile_code([
        "if(a==1)", "{", "  b=1", "}else if(a==2)", "{", "  b=2", "}else", "{", "  b=3", "}",
    ], "t")
    assert len(code) == 1 and isinstance(code[0], If)
    assert len(code[0].branches) == 2 and code[0].orelse is not None


def test_a_comment_between_if_and_its_brace():
    # home's tc0 up has exactly this.
    code = compile_code(["if(tch1==0)", "// the real end of the drag", "{", "  x=1", "}"], "t")
    assert isinstance(code[0], If)


def test_trailing_comments_and_indentation():
    code = compile_code(["    gest_type=-1 // excluded component"], "t")
    assert isinstance(code[0], Assign)


@pytest.mark.parametrize("lines, message", [
    (["while(a<3)", "{", "a++", "}"], "while loops"),
    (["for(a=0;a<3;a++)", "{", "}"], "for loops"),
    (["a=b+c*d"], "precedence"),
    (["if(a==1&&b==2||c==3)", "{", "}"], "precedence"),
    (["if(a==1)", "b=1"], "braces"),
    (["if(a==1)", "{", "b=1"], "missing }"),
    (["}"], "unbalanced"),
    (["else", "{", "}"], "else without"),
    (["click b0,1"], "not an assignment"),
    (["printh zz"], "hex"),
    (["int x=0"], "Program.s"),
    (["if(a)", "{", "}"], "comparison"),
])
def test_unsupported_code_fails_when_compiled(lines, message):
    with pytest.raises(ScriptSyntaxError, match=message):
        compile_code(lines, "page.comp.event")


def test_errors_name_the_event_and_line():
    with pytest.raises(ScriptSyntaxError) as e:
        compile_code(["a=1", "while(1==1)", "{", "}"], "home.tc0.up")
    assert e.value.where == "home.tc0.up line 2"
    assert e.value.line == "while(1==1)"


# ---- running ------------------------------------------------------------------------------------

def test_assignment_and_arithmetic():
    h = run(["a=7", "b=a*3", "c=b-a", "d=c/4", "e=c%4", "a+=1", "a++", "b--"], FakeHost({}))
    assert h.values == {"a": 9, "b": 20, "c": 14, "d": 3, "e": 2}


def test_division_truncates_toward_zero():
    assert run(["a=-7/2", "b=-7%2"]).values == {"a": -3, "b": -1}


def test_integers_wrap_at_32_bits():
    assert run(["a=2147483647", "a++"]).values["a"] == -2147483648


def test_negative_literals_and_comparisons():
    h = run(["dx=-150", "if(dx<-100)", "{", "  r=2", "}"])
    assert h.values["r"] == 2


def test_strings_concatenate_and_compare():
    h = run(['a="ab"', 'a+="cd"', 'b=a+"!"', 'if(b=="abcd!")', "{", "  ok=1", "}"])
    assert h.values["b"] == "abcd!" and h.values["ok"] == 1


def test_text_and_numbers_do_not_mix():
    h = run(['a="x"', "b=a+1", "c=2"])
    assert h.failures == ["b=a+1"]
    assert h.values["c"] == 2  # the code carries on


def test_a_failed_condition_skips_its_if():
    h = run(["if(nosuch==1)", "{", "  a=1", "}else", "{", "  a=2", "}", "b=1"])
    assert "a" not in h.values and h.values["b"] == 1
    assert h.failures == ["if(nosuch==1)"]


def test_and_stops_at_the_first_false_term():
    # As in the gesture code: a component without `font` must not fail the touch.
    host = FakeHost({"tc0.val": 1, "p.type": 112}, {1: "p"})
    run(["if(b[tc0.val].type==98&&b[tc0.val].font==11)", "{", "  g=-1", "}else", "{", "  g=0", "}"], host)
    assert host.failures == [] and host.values["g"] == 0


def test_or():
    assert run(["a=3", "if(a==1||a==3)", "{", "  r=1", "}"]).values["r"] == 1


def test_component_by_id():
    host = FakeHost({"tc0.val": 2, "b1.type": 98, "b1.font": 11}, {2: "b1"})
    run(["t=b[tc0.val].type", "b[2].font=5"], host)
    assert host.values["t"] == 98 and host.values["b1.font"] == 5


def test_instructions():
    host = FakeHost({"t.txt": "hi", "n": 0x01020304})
    run(["printh 65 01 02 01 FF FF FF", 'prints t.txt,0', "prints n,0", "prints n,2", "vis b0,0", "lcd_dev fffb 0002"],
        host)
    assert host.out == [
        ("printh", b"\x65\x01\x02\x01\xff\xff\xff"),
        ("prints", b"hi"),
        ("prints", b"\x04\x03\x02\x01"),
        ("prints", b"\x04\x03"),
        ("vis", "b0", 0),
    ]


def test_page_ends_the_event():
    host = run(["a=1", "page home", "a=2"])
    assert host.out == [("page", "home")] and host.values["a"] == 1


def test_program_declares_globals():
    h = run(["int sys0=0,sys1=0", "int gest_type=0", "int x"], program=True)
    assert h.values == {"sys0": 0, "sys1": 0, "gest_type": 0, "x": 0}
