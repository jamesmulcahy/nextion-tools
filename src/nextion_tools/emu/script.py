"""The code a Nextion TFT carries: its event code (load, down, up, slide, timer, ...) and Program.s.

Only the part of the language that the HMIs tested here use is implemented. Everything an HMI
holds is compiled when it is loaded, so code outside that part is refused at once, naming the
event and the line, rather than when (or if) a test happens to run it. A per-event override
(see Display) stands in for code that cannot be compiled yet.

What is implemented, from a survey of every event of NSPanel Manager's EU and US gui.HMI:

- statements: `name=expr` and `+=` `-=` `*=` `/=` `%=`, `name++` / `name--`, `int a=0,b=0`
  (Program.s only), `if(cond)` / `else if(cond)` / `else` with braces, and the instructions
  `printh`, `prints`, `vis`, `page` and `lcd_dev`;
- names: `comp.attr`, `page.comp.attr`, system variables, Program.s globals, and `b[expr].attr`
  (a component of the current page by id);
- expressions: integers (negative literals too), strings, `+ - * / %`, comparisons, and
  `&&` / `||`;
- `//` comments, whole-line or trailing.

Not measured on hardware, so refused rather than guessed at:

- **Precedence.** An expression that mixes `+`/`-` with `*`/`/`/`%`, or a condition that mixes
  `&&` with `||`. Nextion's documentation says expressions are evaluated left to right with no
  precedence; until that is confirmed, code that depends on it is not accepted.

Chosen, also not measured (they make no difference to the code surveyed):

- `&&` and `||` stop at the first operand that decides the result. The gesture code tests
  `b[tc0.val].type==98&&b[tc0.val].font==11`, and a component with no `font` would otherwise
  fail on every touch that is not a button.
- A failed statement is reported (as the display reports a failed instruction) and the code
  carries on with the next one. A failed condition skips its whole `if`.
- `page` ends the event that ran it. In both HMIs it is always the last statement of its branch.
"""

import re
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .protocol import INVALID, printh_bytes

Value = Union[int, str]

INT_MIN, INT_MAX = -(1 << 31), (1 << 31) - 1

# Instructions the event code may run, with how their arguments are read.
INSTRUCTIONS = ("printh", "prints", "vis", "page", "lcd_dev")

# lcd_dev configures the LCD panel's controller (EU Program.s); there is nothing to emulate.
NO_OPS = frozenset({"lcd_dev"})


class ScriptSyntaxError(ValueError):
    def __init__(self, where: str, line: str, message: str):
        super().__init__("%s: %s: %r" % (where, message, line))
        self.where = where
        self.line = line
        self.message = message


class UnsupportedCode(ValueError):
    """An HMI holds code that cannot be compiled: every event that failed, at once."""

    def __init__(self, errors: List[ScriptSyntaxError]):
        super().__init__("%d events use code the interpreter does not support (override them, or pass "
                         "scripts=False):\n  %s" % (len(errors), "\n  ".join(str(e) for e in errors)))
        self.errors = errors


class ScriptError(Exception):
    """A statement failed when it ran: an unknown name, a type mismatch, division by zero."""

    def __init__(self, message: str, code: int = INVALID):
        super().__init__(message)
        self.code = code


class _Stop(Exception):
    """`page` ran: the event ends."""


# ---- syntax tree ---------------------------------------------------------------------------


@dataclass
class Name:
    """`comp.attr`, `page.comp.attr`, or a variable, as written."""
    text: str


@dataclass
class Indexed:
    """`b[index].attr`: a component of the current page, by id."""
    index: "Expr"
    attr: str


Ref = Union[Name, Indexed]


@dataclass
class Const:
    value: Value


@dataclass
class Load:
    ref: Ref


@dataclass
class Arith:
    first: "Expr"
    rest: List[Tuple[str, "Expr"]]  # evaluated left to right; never mixes precedence levels


@dataclass
class Compare:
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class Logic:
    op: str  # "&&" or "||"
    terms: List[Compare]


Expr = Union[Const, Load, Arith]
Cond = Union[Compare, Logic]


@dataclass
class Stmt:
    text: str  # the source line, for errors


@dataclass
class Assign(Stmt):
    target: Ref
    op: str
    expr: Expr


@dataclass
class Step(Stmt):
    target: Ref
    op: str  # "++" or "--"


@dataclass
class Declare(Stmt):
    names: List[Tuple[str, int]]


@dataclass
class If(Stmt):
    branches: List[Tuple[Cond, List[Stmt]]]
    orelse: Optional[List[Stmt]]


@dataclass
class Instruction(Stmt):
    keyword: str
    args: tuple


Block = List[Stmt]


# ---- lexing --------------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<str>"(?:[^"\\]|\\.)*")
    | (?P<num>\d+)
    | (?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)
    | (?P<attr>\.[A-Za-z_]\w*)
    | (?P<op>==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|%=|[-+*/%<>=\[\],()])
    )""", re.X)

ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=")
ARITH_OPS = ("+", "-", "*", "/", "%")
COMPARE_OPS = ("==", "!=", "<", ">", "<=", ">=")
_ADDITIVE = frozenset("+-")


def _tokens(text: str) -> List[Tuple[str, str]]:
    out = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError("cannot read %r" % text[pos:])
        pos = m.end()
        out.append((m.lastgroup, m.group(m.lastgroup)))
    return out


def strip_comment(line: str) -> str:
    """The line without a `//` comment (outside string literals)."""
    quoted = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and quoted:
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", i):
            return line[:i]
        i += 1
    return line


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]]):
        self.t = tokens
        self.i = 0

    def peek(self, value: Optional[str] = None) -> bool:
        if self.i >= len(self.t):
            return False
        return value is None or self.t[self.i][1] == value

    def take(self, value: Optional[str] = None) -> Tuple[str, str]:
        if self.i >= len(self.t) or (value is not None and self.t[self.i][1] != value):
            raise ValueError("expected %r" % value if value else "unexpected end")
        tok = self.t[self.i]
        self.i += 1
        return tok

    def done(self) -> bool:
        return self.i >= len(self.t)

    def ref(self) -> Ref:
        kind, text = self.take()
        if kind != "name":
            raise ValueError("expected a name, got %r" % text)
        if text == "b" and self.peek("["):
            self.take("[")
            index = self.expr()
            self.take("]")
            kind, attr = self.take()
            if kind != "attr":
                raise ValueError("expected .attribute after b[...]")
            return Indexed(index, attr[1:])
        return Name(text)

    def operand(self) -> Expr:
        kind, text = self.t[self.i] if self.i < len(self.t) else (None, None)
        if kind == "num":
            self.i += 1
            return Const(int(text))
        if kind == "op" and text == "-" and self.i + 1 < len(self.t) and self.t[self.i + 1][0] == "num":
            self.i += 2
            return Const(-int(self.t[self.i - 1][1]))
        if kind == "str":
            self.i += 1
            return Const(text[1:-1].replace('\\"', '"').replace("\\r", "\r"))
        if kind == "name":
            return Load(self.ref())
        raise ValueError("expected a value, got %r" % text)

    def expr(self) -> Expr:
        first = self.operand()
        rest = []
        while self.peek() and self.t[self.i][1] in ARITH_OPS:
            op = self.take()[1]
            rest.append((op, self.operand()))
        if not rest:
            return first
        levels = {op in _ADDITIVE for op, _ in rest}
        if len(levels) > 1:
            raise ValueError("mixes + or - with * / or %: precedence is not modelled")
        return Arith(first, rest)

    def compare(self) -> Compare:
        left = self.expr()
        if not self.peek() or self.t[self.i][1] not in COMPARE_OPS:
            raise ValueError("expected a comparison")
        op = self.take()[1]
        return Compare(op, left, self.expr())

    def cond(self) -> Cond:
        terms = [self.compare()]
        ops = set()
        while self.peek("&&") or self.peek("||"):
            ops.add(self.take()[1])
            terms.append(self.compare())
        if not ops:
            return terms[0]
        if len(ops) > 1:
            raise ValueError("mixes && with ||: precedence is not modelled")
        return Logic(ops.pop(), terms)


def _parse_simple(line: str, program: bool) -> Stmt:
    keyword, _, rest = line.partition(" ")
    if keyword == "int":
        if not program:
            raise ValueError("int declares globals, which only Program.s can do")
        names = []
        for part in rest.split(","):
            name, eq, value = part.strip().partition("=")
            if not re.fullmatch(r"[A-Za-z_]\w*", name.strip()):
                raise ValueError("bad declaration %r" % part)
            names.append((name.strip(), int(value) if eq else 0))
        return Declare(line, names)
    if keyword in INSTRUCTIONS:
        return Instruction(line, keyword, _instruction_args(keyword, rest.strip()))
    p = _Parser(_tokens(line))
    target = p.ref()
    if p.peek("++") or p.peek("--"):
        op = p.take()[1]
        if not p.done():
            raise ValueError("unexpected text after %s" % op)
        return Step(line, target, op)
    if not p.peek() or p.t[p.i][1] not in ASSIGN_OPS:
        raise ValueError("not an assignment or a supported instruction")
    op = p.take()[1]
    expr = p.expr()
    if not p.done():
        raise ValueError("unexpected %r" % p.t[p.i][1])
    return Assign(line, target, op, expr)


def _instruction_args(keyword: str, rest: str) -> tuple:
    if keyword == "printh":
        return (printh_bytes(rest),)
    if keyword == "page":
        if not rest or " " in rest:
            raise ValueError("page needs one page name or number")
        return (rest,)
    if keyword in NO_OPS:
        return (rest,)
    p = _Parser(_tokens(rest))
    if keyword == "prints":
        value = p.expr()
        p.take(",")
        length = p.expr()
    else:  # vis
        value = p.ref()
        p.take(",")
        length = p.expr()
    if not p.done():
        raise ValueError("unexpected %r" % p.t[p.i][1])
    return (value, length)


def _split_braces(lines: Sequence[str]) -> List[Tuple[int, str]]:
    """(line number, piece) with every `{` and `}` a piece of its own."""
    out = []
    for n, raw in enumerate(lines, 1):
        line = strip_comment(raw).strip()
        while line.startswith("}"):
            out.append((n, "}"))
            line = line[1:].strip()
        tail = line.endswith("{") and line.count('"') % 2 == 0
        if tail:
            line = line[:-1].strip()
        if line:
            out.append((n, line))
        if tail:
            out.append((n, "{"))
    return out


_IF = re.compile(r"^(else\s*)?if\s*\((.*)\)$")


def compile_code(lines: Sequence[str], where: str, program: bool = False) -> Block:
    """A block from an event's lines. Raises ScriptSyntaxError on anything not supported."""
    pieces = _split_braces(lines)
    pos = 0

    def fail(n: int, message: str):
        raise ScriptSyntaxError("%s line %d" % (where, n), lines[n - 1].strip(), message)

    def block(top: bool) -> Block:
        nonlocal pos
        out: Block = []
        while pos < len(pieces):
            n, piece = pieces[pos]
            if piece == "}":
                if top:
                    fail(n, "unbalanced }")
                pos += 1
                return out
            if piece == "{":
                fail(n, "a { that opens no if")
            m = _IF.match(piece)
            if m and m.group(1):
                fail(n, "else without an if")
            if m:
                out.append(if_chain(n, piece))
                continue
            if piece == "else":
                fail(n, "else without an if")
            if re.match(r"^(while|for)\b", piece):
                fail(n, "%s loops are not supported" % piece.split("(")[0])
            try:
                out.append(_parse_simple(piece, program))
            except ValueError as e:
                fail(n, str(e))
            pos += 1
        if not top:
            fail(pieces[-1][0] if pieces else 1, "missing }")
        return out

    def braced(n: int) -> Block:
        nonlocal pos
        if pos >= len(pieces) or pieces[pos][1] != "{":
            fail(n, "expected { (Nextion needs braces)")
        pos += 1
        return block(False)

    def if_chain(n: int, piece: str) -> If:
        nonlocal pos
        first = lines[n - 1].strip()
        branches = []
        orelse = None
        while True:
            m = _IF.match(piece)
            try:
                cond = _Parser(_tokens(m.group(2)))
                c = cond.cond()
                if not cond.done():
                    raise ValueError("unexpected %r" % cond.t[cond.i][1])
            except ValueError as e:
                fail(n, str(e))
            pos += 1
            branches.append((c, braced(n)))
            if pos < len(pieces) and _IF.match(pieces[pos][1]) and _IF.match(pieces[pos][1]).group(1):
                n, piece = pieces[pos]
                continue
            if pos < len(pieces) and pieces[pos][1] == "else":
                n = pieces[pos][0]
                pos += 1
                orelse = braced(n)
            return If(first, branches, orelse)

    return block(True)


# ---- running -------------------------------------------------------------------------------


class Host:
    """What the code acts on: the display. Every method raises ScriptError on failure."""

    def read(self, name: str) -> Value:
        raise NotImplementedError

    def write(self, name: str, value: Value) -> None:
        raise NotImplementedError

    def component_name(self, component_id: int) -> str:
        """`comp` for a component id on the current page."""
        raise NotImplementedError

    def declare(self, name: str, value: int) -> None:
        raise NotImplementedError

    def instruction(self, keyword: str, *args) -> None:
        """printh(bytes), prints(bytes), vis(name, state), page(name)."""
        raise NotImplementedError

    def failed(self, stmt: Stmt, error: ScriptError) -> None:
        raise NotImplementedError


def _wrap(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v > INT_MAX else v


def _arith(op: str, a: Value, b: Value) -> Value:
    if isinstance(a, str) or isinstance(b, str):
        if op == "+" and isinstance(a, str) and isinstance(b, str):
            return a + b
        raise ScriptError("%r %s %r: text and numbers do not mix" % (a, op, b))
    if op == "+":
        return _wrap(a + b)
    if op == "-":
        return _wrap(a - b)
    if op == "*":
        return _wrap(a * b)
    if b == 0:
        raise ScriptError("division by zero")
    q = int(a / b)
    return _wrap(q) if op == "/" else _wrap(a - b * q)


class Interpreter:
    def __init__(self, host: Host):
        self.host = host

    def run(self, code: Block) -> None:
        try:
            self._block(code)
        except _Stop:
            pass

    def _block(self, code: Block) -> None:
        for stmt in code:
            try:
                self._stmt(stmt)
            except ScriptError as e:
                self.host.failed(stmt, e)

    def _stmt(self, stmt: Stmt) -> None:
        if isinstance(stmt, If):
            for cond, body in stmt.branches:
                if self._cond(cond):
                    self._block(body)
                    return
            if stmt.orelse is not None:
                self._block(stmt.orelse)
        elif isinstance(stmt, Assign):
            name = self._name(stmt.target)
            value = self._eval(stmt.expr)
            if stmt.op != "=":
                value = _arith(stmt.op[0], self.host.read(name), value)
            self.host.write(name, value)
        elif isinstance(stmt, Step):
            name = self._name(stmt.target)
            self.host.write(name, _arith(stmt.op[0], self.host.read(name), 1))
        elif isinstance(stmt, Declare):
            for name, value in stmt.names:
                self.host.declare(name, value)
        elif isinstance(stmt, Instruction):
            self._instruction(stmt)
        else:
            raise TypeError(stmt)

    def _instruction(self, stmt: Instruction) -> None:
        kw = stmt.keyword
        if kw in NO_OPS:
            return
        if kw == "printh":
            self.host.instruction("printh", stmt.args[0])
        elif kw == "prints":
            value = self._eval(stmt.args[0])
            length = self._eval(stmt.args[1])
            if not isinstance(length, int):
                raise ScriptError("prints length must be a number")
            if isinstance(value, str):
                data = value.encode("utf-8")
            else:
                data = (value & 0xFFFFFFFF).to_bytes(4, "little")
            self.host.instruction("prints", data[:length] if length else data)
        elif kw == "vis":
            state = self._eval(stmt.args[1])
            if not isinstance(state, int):
                raise ScriptError("vis needs 0 or 1")
            self.host.instruction("vis", self._name(stmt.args[0]), state)
        elif kw == "page":
            self.host.instruction("page", stmt.args[0])
            raise _Stop()

    def _name(self, ref: Ref) -> str:
        if isinstance(ref, Name):
            return ref.text
        index = self._eval(ref.index)
        if not isinstance(index, int):
            raise ScriptError("b[] needs a component id")
        return "%s.%s" % (self.host.component_name(index), ref.attr)

    def _eval(self, expr: Expr) -> Value:
        if isinstance(expr, Const):
            return expr.value
        if isinstance(expr, Load):
            return self.host.read(self._name(expr.ref))
        value = self._eval(expr.first)
        for op, operand in expr.rest:
            value = _arith(op, value, self._eval(operand))
        return value

    def _cond(self, cond: Cond) -> bool:
        if isinstance(cond, Compare):
            a, b = self._eval(cond.left), self._eval(cond.right)
            if isinstance(a, str) != isinstance(b, str):
                raise ScriptError("%r %s %r: text and numbers do not compare" % (a, cond.op, b))
            if isinstance(a, str) and cond.op not in ("==", "!="):
                raise ScriptError("text compares only with == and !=")
            return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[cond.op]
        if cond.op == "&&":
            return all(self._cond(t) for t in cond.terms)
        return any(self._cond(t) for t in cond.terms)


# ---- a whole project -----------------------------------------------------------------------

PROGRAM = "Program.s"

EventKey = Tuple[str, str, str]  # page, component, event


@dataclass
class Compiled:
    events: Dict[EventKey, Block]
    program: Block
    errors: List[ScriptSyntaxError]


def compile_hmi(hmi, skip: Callable[[str], bool] = lambda where: False) -> Compiled:
    """Every event of every page, and Program.s. `skip(where)` leaves out an event (one that is
    overridden); `where` is "page.component.event" or "Program.s"."""
    events: Dict[EventKey, Block] = {}
    errors: List[ScriptSyntaxError] = []
    for page in hmi.pages:
        for comp in page.components:
            for event, lines in comp.events.items():
                where = "%s.%s.%s" % (page.name, comp.name, event)
                if not lines or skip(where):
                    continue
                try:
                    events[(page.name, comp.name, event)] = compile_code(lines, where)
                except ScriptSyntaxError as e:
                    errors.append(e)
    program: Block = []
    if not skip(PROGRAM):
        try:
            program = compile_code(hmi.program, PROGRAM, program=True)
        except ScriptSyntaxError as e:
            errors.append(e)
    return Compiled(events, program, errors)


def survey(hmi) -> Dict[str, int]:
    """How often each statement form appears, over every event and Program.s."""
    counts: Dict[str, int] = {}

    def add(form):
        counts[form] = counts.get(form, 0) + 1

    def walk(code: Block):
        for s in code:
            if isinstance(s, If):
                add("if")
                for _ in s.branches[1:]:
                    add("else if")
                if s.orelse:
                    add("else")
                for _, body in s.branches:
                    walk(body)
                if s.orelse:
                    walk(s.orelse)
            elif isinstance(s, Assign):
                add("assign " + s.op + (" b[]" if isinstance(s.target, Indexed) else ""))
            elif isinstance(s, Step):
                add(s.op)
            elif isinstance(s, Declare):
                add("int")
            elif isinstance(s, Instruction):
                add(s.keyword)

    compiled = compile_hmi(hmi)
    for code in compiled.events.values():
        walk(code)
    walk(compiled.program)
    for e in compiled.errors:
        add("UNSUPPORTED " + e.message)
    return counts


def main(argv=None) -> int:
    """python -m nextion_tools.emu.script gui.HMI ...: compile every event, list the statement forms
    used, and fail if any event cannot be compiled."""
    from ..hmi import Project

    status = 0
    for path in (argv if argv is not None else sys.argv[1:]):
        hmi = Project.load(path)
        compiled = compile_hmi(hmi)
        print("%s: %d events, Program.s %d statements" % (path, len(compiled.events), len(compiled.program)))
        for form, count in sorted(survey(hmi).items()):
            print("  %5d  %s" % (count, form))
        for e in compiled.errors:
            print("  unsupported: %s" % e)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
