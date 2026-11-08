"""BQL-lite grammar: lexer + recursive-descent parser (no lark dependency) -> immutable AST.

    query    := clause+
    clause   := get(field, ...) | for(universe) | with(key=value, ...) | where(expr)
              | group(expr, ...)[.agg(fn, ...)] | sort(expr [asc|desc], ...) | limit(n)
    field    := expr [as name]
    universe := expr over members(...) | region(...) | sector(...) | country(...) | subsector(...) | type(...)
                | ['AAPL US', 'SAP GY'] | 'AAPL US' combined with & (intersection) | (union) - (difference)
    expr     := or_expr ;  or: `or` / `|` ; and: `and` / `&` ; not ; comparison (== = != <> < <= > >= in, not in)
                additive (+ -) ; multiplicative (* /) ; unary (- +) ; power (^) ; primary
    primary  := number | number% | duration (1D 2W 3M 1Q 5Y) | date (2026-01-01) | 'string' | true | false | null
                | name (dotted: tech.hi52_pct) | name(args) | (expr) | [expr, ...]
    args     := expr | name=expr (keyword arguments)

Literals never become SQL text: the compiler binds every string / number / date as a parameter. The lexer rejects any
character outside the grammar (no `;`, comments or quotes other than '...' / "...")."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterator

MAX_QUERY_CHARS = 8000
MAX_TOKENS = 3000
MAX_DEPTH = 48
CLAUSES = ("get", "for", "with", "where", "group", "sort", "limit")
KEYWORDS = frozenset({"and", "or", "not", "in", "true", "false", "null", "asc", "desc", "as"})
DUR_UNITS = ("D", "W", "M", "Q", "Y")


class BqlError(ValueError):
    """Any BQL error (HTTP 422). `pos` = character offset in the query when known."""

    def __init__(self, msg: str, pos: int | None = None):
        super().__init__(msg if pos is None else f"{msg} (at character {pos + 1})")
        self.pos = pos
        self.msg = msg


class BqlSyntaxError(BqlError):
    pass


# ------------------------------------------------------------------------------------------------ AST


class Node:
    pos: int

    def children(self) -> tuple["Node", ...]:
        return ()


@dataclass(frozen=True)
class Num(Node):
    value: float
    percent: bool = False
    pos: int = field(default=0, compare=False)

    @property
    def is_int(self) -> bool:
        return not self.percent and float(self.value).is_integer()


@dataclass(frozen=True)
class Str(Node):
    value: str
    pos: int = field(default=0, compare=False)


@dataclass(frozen=True)
class DateLit(Node):
    value: date
    pos: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Dur(Node):
    n: int
    unit: str
    pos: int = field(default=0, compare=False)

    @property
    def days(self) -> float:
        return abs(self.n) * {"D": 1, "W": 7, "M": 30.4375, "Q": 91.3125, "Y": 365.25}[self.unit]


@dataclass(frozen=True)
class Bool(Node):
    value: bool
    pos: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Null(Node):
    pos: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Name(Node):
    name: str
    pos: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Call(Node):
    fn: str
    args: tuple[Node, ...] = ()
    kwargs: tuple[tuple[str, Node], ...] = ()
    pos: int = field(default=0, compare=False)

    def children(self) -> tuple[Node, ...]:
        return self.args + tuple(v for _, v in self.kwargs)

    def kw(self, key: str) -> Node | None:
        return next((v for k, v in self.kwargs if k == key), None)


@dataclass(frozen=True)
class Bin(Node):
    op: str
    left: Node
    right: Node
    pos: int = field(default=0, compare=False)

    def children(self) -> tuple[Node, ...]:
        return (self.left, self.right)


@dataclass(frozen=True)
class Unary(Node):
    op: str
    operand: Node
    pos: int = field(default=0, compare=False)

    def children(self) -> tuple[Node, ...]:
        return (self.operand,)


@dataclass(frozen=True)
class ListLit(Node):
    items: tuple[Node, ...]
    pos: int = field(default=0, compare=False)

    def children(self) -> tuple[Node, ...]:
        return self.items


@dataclass(frozen=True)
class GetField:
    expr: Node
    alias: str | None = None


@dataclass(frozen=True)
class SortKey:
    expr: Node
    desc: bool = False


@dataclass(frozen=True)
class Query:
    fields: tuple[GetField, ...]
    universe: Node | None = None
    with_: tuple[tuple[str, Node], ...] = ()
    where: Node | None = None
    group: tuple[Node, ...] = ()
    aggs: tuple[str, ...] = ()
    sort: tuple[SortKey, ...] = ()
    limit: int | None = None

    def opt(self, key: str) -> Node | None:
        return next((v for k, v in self.with_ if k == key), None)


def walk(node: Node) -> Iterator[Node]:
    yield node
    for c in node.children():
        yield from walk(c)


# ------------------------------------------------------------------------------------------------ lexer

_TOKEN_RX = re.compile(r"""
    (?P<ws>\s+)
  | (?P<date>\d{4}-\d{2}-\d{2})(?![\w])
  | (?P<dur>\d+[DWMQYdwmqy])(?![A-Za-z0-9_])
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?%?)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<op><=|>=|==|!=|<>|[-+*/^&|<>=(),\[\].])
""", re.VERBOSE)


@dataclass(frozen=True)
class Tok:
    kind: str  # date dur num str ident op eof
    text: str
    pos: int
    value: Any = None


def tokenize(text: str) -> list[Tok]:
    if len(text) > MAX_QUERY_CHARS:
        raise BqlSyntaxError(f"query too long ({len(text)} characters, max {MAX_QUERY_CHARS})")
    out: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        m = _TOKEN_RX.match(text, i)
        if not m:
            ch = text[i]
            raise BqlSyntaxError(f"unexpected character {ch!r}", i)
        kind = m.lastgroup or ""
        s = m.group(0)
        if kind != "ws":
            if kind == "date":
                try:
                    v: Any = date.fromisoformat(s)
                except ValueError:
                    raise BqlSyntaxError(f"invalid date {s!r}", i) from None
            elif kind == "dur":
                v = (int(s[:-1]), s[-1].upper())
            elif kind == "num":
                pct = s.endswith("%")
                v = (float(s[:-1] if pct else s), pct)
            elif kind == "str":
                q = s[0]
                v = s[1:-1].replace(q + q, q)
                if len(v) > 500:
                    raise BqlSyntaxError("string literal too long (max 500 characters)", i)
            else:
                v = s
            out.append(Tok(kind, s, i, v))
            if len(out) > MAX_TOKENS:
                raise BqlSyntaxError(f"query too long (more than {MAX_TOKENS} tokens)")
        i = m.end()
    out.append(Tok("eof", "", n))
    return out


# ------------------------------------------------------------------------------------------------ parser


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.depth = 0

    # -- helpers
    @property
    def cur(self) -> Tok:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def is_op(self, *ops: str) -> bool:
        return self.cur.kind == "op" and self.cur.text in ops

    def is_kw(self, *kws: str) -> bool:
        return self.cur.kind == "ident" and self.cur.text.lower() in kws

    def expect_op(self, op: str) -> Tok:
        if not self.is_op(op):
            got = self.cur.text or "end of query"
            raise BqlSyntaxError(f"expected {op!r}, got {got!r}", self.cur.pos)
        return self.next()

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise BqlSyntaxError(f"expression nested too deeply (max {MAX_DEPTH})", self.cur.pos)

    def leave(self) -> None:
        self.depth -= 1

    # -- query
    def query(self) -> Query:
        fields: list[GetField] = []
        universe = where = None
        with_: list[tuple[str, Node]] = []
        group: list[Node] = []
        aggs: list[str] = []
        sort: list[SortKey] = []
        limit: int | None = None
        seen: set[str] = set()
        if self.cur.kind == "eof":
            raise BqlSyntaxError("empty query: start with get(...)", 0)
        while self.cur.kind != "eof":
            t = self.cur
            name = t.text.lower() if t.kind == "ident" else ""
            if name not in CLAUSES:
                raise BqlSyntaxError(f"expected a clause ({', '.join(CLAUSES)}), got {t.text!r}", t.pos)
            if name in seen:
                raise BqlSyntaxError(f"duplicate clause {name}()", t.pos)
            seen.add(name)
            self.next()
            self.expect_op("(")
            if name == "get":
                fields = self.get_list()
            elif name == "for":
                universe = self.expr()
            elif name == "with":
                with_ = self.kwargs_list()
            elif name == "where":
                where = self.expr()
            elif name == "group":
                group = [self.expr()]
                while self.is_op(","):
                    self.next()
                    group.append(self.expr())
            elif name == "sort":
                sort = self.sort_list()
            elif name == "limit":
                lt = self.next()
                if lt.kind != "num" or lt.value[1] or not float(lt.value[0]).is_integer() or lt.value[0] < 1:
                    raise BqlSyntaxError("limit(n) needs a positive integer", lt.pos)
                limit = int(lt.value[0])
            self.expect_op(")")
            if name == "group" and self.is_op("."):
                self.next()
                at = self.next()
                if at.kind != "ident" or at.text.lower() != "agg":
                    raise BqlSyntaxError("expected .agg(fn, ...) after group(...)", at.pos)
                self.expect_op("(")
                while True:
                    a = self.next()
                    if a.kind != "ident":
                        raise BqlSyntaxError("agg() takes aggregation names (median, mean, sum, min, max, count, std)", a.pos)
                    aggs.append(a.text.lower())
                    if not self.is_op(","):
                        break
                    self.next()
                self.expect_op(")")
        if not fields:
            raise BqlSyntaxError("a query needs get(field, ...)", 0)
        return Query(tuple(fields), universe, tuple(with_), where, tuple(group), tuple(aggs), tuple(sort), limit)

    def get_list(self) -> list[GetField]:
        out = []
        while True:
            e = self.expr()
            alias = None
            if self.is_kw("as"):
                self.next()
                a = self.next()
                if a.kind != "ident" or "." in a.text or a.text.lower() in KEYWORDS:
                    raise BqlSyntaxError("expected a column name after `as`", a.pos)
                alias = a.text
            out.append(GetField(e, alias))
            if not self.is_op(","):
                return out
            self.next()

    def sort_list(self) -> list[SortKey]:
        out = []
        while True:
            e = self.expr()
            desc = False
            if self.is_kw("asc", "desc"):
                desc = self.next().text.lower() == "desc"
            out.append(SortKey(e, desc))
            if not self.is_op(","):
                return out
            self.next()

    def kwargs_list(self) -> list[tuple[str, Node]]:
        out: list[tuple[str, Node]] = []
        if self.is_op(")"):
            return out
        while True:
            k = self.next()
            if k.kind != "ident" or "." in k.text:
                raise BqlSyntaxError("with() takes key=value options (dates, freq, currency, fill, as_of)", k.pos)
            self.expect_op("=")
            out.append((k.text.lower(), self.expr()))
            if not self.is_op(","):
                return out
            self.next()

    # -- expressions
    def expr(self) -> Node:
        self.enter()
        try:
            return self.or_expr()
        finally:
            self.leave()

    def or_expr(self) -> Node:
        left = self.and_expr()
        while self.is_kw("or") or self.is_op("|"):
            p = self.next().pos
            left = Bin("or", left, self.and_expr(), pos=p)
        return left

    def and_expr(self) -> Node:
        left = self.not_expr()
        while self.is_kw("and") or self.is_op("&"):
            p = self.next().pos
            left = Bin("and", left, self.not_expr(), pos=p)
        return left

    def not_expr(self) -> Node:
        if self.is_kw("not") and not (self.peek().kind == "ident" and self.peek().text.lower() == "in"):
            p = self.next().pos
            self.enter()
            try:
                return Unary("not", self.not_expr(), pos=p)
            finally:
                self.leave()
        return self.comparison()

    def comparison(self) -> Node:
        left = self.additive()
        if self.is_op("==", "=", "!=", "<>", "<", "<=", ">", ">="):
            t = self.next()
            op = {"=": "==", "<>": "!="}.get(t.text, t.text)
            return Bin(op, left, self.additive(), pos=t.pos)
        if self.is_kw("in"):
            p = self.next().pos
            return Bin("in", left, self.additive(), pos=p)
        if self.is_kw("not") and self.peek().kind == "ident" and self.peek().text.lower() == "in":
            p = self.next().pos
            self.next()
            return Unary("not", Bin("in", left, self.additive(), pos=p), pos=p)
        return left

    def additive(self) -> Node:
        left = self.multiplicative()
        while self.is_op("+", "-"):
            t = self.next()
            left = Bin(t.text, left, self.multiplicative(), pos=t.pos)
        return left

    def multiplicative(self) -> Node:
        left = self.unary()
        while self.is_op("*", "/"):
            t = self.next()
            left = Bin(t.text, left, self.unary(), pos=t.pos)
        return left

    def unary(self) -> Node:
        if self.is_op("-", "+"):
            t = self.next()
            self.enter()
            try:
                x = self.unary()
            finally:
                self.leave()
            if t.text == "+":
                return x
            if isinstance(x, Num):
                return Num(-x.value, x.percent, pos=t.pos)
            if isinstance(x, Dur):
                return Dur(-x.n, x.unit, pos=t.pos)
            return Unary("-", x, pos=t.pos)
        return self.power()

    def power(self) -> Node:
        base = self.primary()
        if self.is_op("^"):
            t = self.next()
            self.enter()
            try:
                return Bin("^", base, self.unary(), pos=t.pos)
            finally:
                self.leave()
        return base

    def primary(self) -> Node:
        t = self.cur
        if t.kind == "num":
            self.next()
            v, pct = t.value
            return Num(v / 100.0 if pct else v, pct, pos=t.pos)
        if t.kind == "dur":
            self.next()
            return Dur(t.value[0], t.value[1], pos=t.pos)
        if t.kind == "date":
            self.next()
            return DateLit(t.value, pos=t.pos)
        if t.kind == "str":
            self.next()
            return Str(t.value, pos=t.pos)
        if t.kind == "ident":
            low = t.text.lower()
            if low in ("true", "false"):
                self.next()
                return Bool(low == "true", pos=t.pos)
            if low == "null":
                self.next()
                return Null(pos=t.pos)
            if low in KEYWORDS:
                raise BqlSyntaxError(f"unexpected keyword {t.text!r}", t.pos)
            self.next()
            if self.is_op("("):
                if "." in t.text:
                    raise BqlSyntaxError(f"{t.text!r} is not a function", t.pos)
                return self.call(low, t.pos)
            return Name(t.text, pos=t.pos)
        if self.is_op("("):
            self.next()
            e = self.expr()
            self.expect_op(")")
            return e
        if self.is_op("["):
            p = self.next().pos
            items: list[Node] = []
            if not self.is_op("]"):
                while True:
                    items.append(self.expr())
                    if not self.is_op(","):
                        break
                    self.next()
            self.expect_op("]")
            return ListLit(tuple(items), pos=p)
        got = t.text or "end of query"
        raise BqlSyntaxError(f"unexpected {got!r}", t.pos)

    def call(self, fn: str, pos: int) -> Node:
        self.expect_op("(")
        args: list[Node] = []
        kwargs: list[tuple[str, Node]] = []
        if not self.is_op(")"):
            while True:
                if self.cur.kind == "ident" and self.peek().kind == "op" and self.peek().text == "=" \
                        and "." not in self.cur.text:
                    k = self.next().text.lower()
                    self.next()
                    if any(k == x for x, _ in kwargs):
                        raise BqlSyntaxError(f"duplicate keyword argument {k!r}", pos)
                    kwargs.append((k, self.expr()))
                else:
                    if kwargs:
                        raise BqlSyntaxError("positional argument after a keyword argument", self.cur.pos)
                    args.append(self.expr())
                if not self.is_op(","):
                    break
                self.next()
        self.expect_op(")")
        return Call(fn, tuple(args), tuple(kwargs), pos=pos)


def parse(text: str) -> Query:
    """Parse a BQL query. Raises BqlSyntaxError."""
    return _Parser(text).query()


def parse_expr(text: str) -> Node:
    """Parse a single expression (Backtest / Studio BQL signals)."""
    p = _Parser(text)
    e = p.expr()
    if p.cur.kind != "eof":
        raise BqlSyntaxError(f"unexpected {p.cur.text!r} after the expression", p.cur.pos)
    return e


# ------------------------------------------------------------------------------------------------ canonical text / dict / hash

_PREC = {"or": 1, "and": 2, "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4, "in": 4, "+": 5, "-": 5, "*": 6, "/": 6,
         "^": 8}


def _fmt_num(v: float) -> str:
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(float(v))


def to_text(n: Node, parent: int = 0) -> str:
    """Canonical text of an expression (column names, normalisation)."""
    if isinstance(n, Num):
        return (_fmt_num(round(n.value * 100, 10)) + "%") if n.percent else _fmt_num(n.value)
    if isinstance(n, Str):
        return "'" + n.value.replace("'", "''") + "'"
    if isinstance(n, DateLit):
        return n.value.isoformat()
    if isinstance(n, Dur):
        return f"{n.n}{n.unit}"
    if isinstance(n, Bool):
        return "true" if n.value else "false"
    if isinstance(n, Null):
        return "null"
    if isinstance(n, Name):
        return n.name
    if isinstance(n, Call):
        parts = [to_text(a) for a in n.args] + [f"{k}={to_text(v)}" for k, v in n.kwargs]
        return f"{n.fn}({','.join(parts)})"
    if isinstance(n, ListLit):
        return "[" + ",".join(to_text(x) for x in n.items) + "]"
    if isinstance(n, Unary):
        if n.op == "not":
            s = "not " + to_text(n.operand, 3)
            return f"({s})" if parent > 3 else s
        return "-" + to_text(n.operand, 7)
    if isinstance(n, Bin):
        p = _PREC[n.op]
        op = f" {n.op} " if n.op in ("and", "or", "in") else n.op
        s = to_text(n.left, p) + op + to_text(n.right, p + 1)
        return f"({s})" if p < parent else s
    raise TypeError(type(n).__name__)


def to_dict(n: Any) -> Any:
    """JSON-able AST (golden tests, explain, hashing)."""
    if isinstance(n, Query):
        return {"get": [{"expr": to_dict(f.expr), **({"as": f.alias} if f.alias else {})} for f in n.fields],
                "for": to_dict(n.universe) if n.universe is not None else None,
                "with": {k: to_dict(v) for k, v in n.with_},
                "where": to_dict(n.where) if n.where is not None else None,
                "group": [to_dict(g) for g in n.group], "agg": list(n.aggs),
                "sort": [{"expr": to_dict(s.expr), "desc": s.desc} for s in n.sort], "limit": n.limit}
    if isinstance(n, Num):
        return {"num": n.value, **({"pct": True} if n.percent else {})}
    if isinstance(n, Str):
        return {"str": n.value}
    if isinstance(n, DateLit):
        return {"date": n.value.isoformat()}
    if isinstance(n, Dur):
        return {"dur": f"{n.n}{n.unit}"}
    if isinstance(n, Bool):
        return {"bool": n.value}
    if isinstance(n, Null):
        return {"null": True}
    if isinstance(n, Name):
        return {"name": n.name}
    if isinstance(n, Call):
        d: dict[str, Any] = {"call": n.fn, "args": [to_dict(a) for a in n.args]}
        if n.kwargs:
            d["kwargs"] = {k: to_dict(v) for k, v in n.kwargs}
        return d
    if isinstance(n, ListLit):
        return {"list": [to_dict(x) for x in n.items]}
    if isinstance(n, Unary):
        return {"op": n.op, "x": to_dict(n.operand)}
    if isinstance(n, Bin):
        return {"op": n.op, "l": to_dict(n.left), "r": to_dict(n.right)}
    raise TypeError(type(n).__name__)


def query_text(q: Query) -> str:
    """Canonical one-line text of a query (normalised form: lower-case clauses / functions, no extra spaces)."""
    parts = ["get(" + ",".join(to_text(f.expr) + (f" as {f.alias}" if f.alias else "") for f in q.fields) + ")"]
    if q.universe is not None:
        parts.append(f"for({to_text(q.universe)})")
    if q.with_:
        parts.append("with(" + ",".join(f"{k}={to_text(v)}" for k, v in q.with_) + ")")
    if q.where is not None:
        parts.append(f"where({to_text(q.where)})")
    if q.group:
        parts.append("group(" + ",".join(to_text(g) for g in q.group) + ")" + (f".agg({','.join(q.aggs)})" if q.aggs else ""))
    if q.sort:
        parts.append("sort(" + ",".join(to_text(s.expr) + (" desc" if s.desc else " asc") for s in q.sort) + ")")
    if q.limit is not None:
        parts.append(f"limit({q.limit})")
    return " ".join(parts)


def ast_hash(q: Query, extra: Any = None) -> str:
    raw = json.dumps({"q": to_dict(q), "x": extra}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()
