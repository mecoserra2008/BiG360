"""BQL-lite compiler: AST -> one parameterised DuckDB query over the serving snapshot.

Security model
* Identifiers (tables, columns, SQL fragments of computed fields) come only from the field dictionary
  (contracts/bql_fields.yaml, generated from the contracts) and from this module's function table.
* Every user literal (strings, numbers, dates, ticker lists) is bound as a named parameter ($p1, $p2, ...). Integers
  that must be SQL text (window sizes, interval lengths) are validated Python ints formatted with %d.

Evaluation model (docs/USER_GUIDE.md "BQL console")
* Rows: one per security (cross-section at as_of) or per security x grid date (with dates=...).
* Levels: static (identity) < stream (dated history sampled as of a date) < daily (trading-day panel per security:
  price bars + streams as-of joined) < grid (snapshot columns, cross-sectional functions, sampled values).
  Time-series functions run on the daily panel (or on the grid when their argument is already cross-sectional);
  cross-sectional functions run per grid date over the universe rows that pass where().
* Point in time: a grid date D only sees bars dated <= D and rows whose knowledge / as-of date is <= D; snapshot-only
  columns (no history) are allowed only at the snapshot as-of date.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from . import fields as F
from .grammar import (
    BqlError,
    Bin,
    Bool,
    Call,
    DateLit,
    Dur,
    ListLit,
    Name,
    Node,
    Null,
    Num,
    Query,
    Str,
    Unary,
    parse_expr,
    to_text,
)

# ------------------------------------------------------------------------------------------------ limits / registry

MAX_SPAN_YEARS = 30
MAX_GRID_POINTS = 8000
MAX_CELLS = 2_000_000          # universe x grid dates
MAX_DAILY_ROWS = 40_000_000    # universe x trading days (incl. warm-up)
MAX_UNIVERSE = 25_000
MAX_LIST = 1000
PX_STALE_DAYS = 31

TS_ROLL = {"avg": "avg", "mean": "avg", "std": "stddev_samp", "min": "min", "max": "max", "sum": "sum",
           "median": "median", "count": "count"}
TS_FUNCS = {"chg", "chg_pct", "lag", "zscore", "pct_rank", "rolling", "resample", "ytd", "corr", "beta"} | set(TS_ROLL)
CS_FUNCS = {"rank", "cs_rank", "cs_zscore", "cs_demean", "cs_mean", "cs_median"}
SCALAR_FUNCS = {"abs": 1, "ln": 1, "log": 1, "log10": 1, "exp": 1, "sqrt": 1, "sign": 1, "floor": 1, "ceil": 1,
                "round": -1, "least": -1, "greatest": -1, "coalesce": -1, "nz": 1, "if": 3, "clip": 3, "isnull": 1}
AGGS = {"median": "median", "mean": "avg", "avg": "avg", "sum": "sum", "min": "min", "max": "max",
        "count": "count", "std": "stddev_samp"}
FUNCTION_DOCS: dict[str, str] = {
    "chg(x, n|P)": "x - x n observations ago (or at t - P for a duration)",
    "chg_pct(x, n|P)": "x / x[t-n] - 1 (fraction); chg_pct(px, 1M) = 1 calendar month, chg_pct(px, 21) = 21 sessions",
    "lag(x, n|P)": "x n observations ago / at t - P",
    "avg|std|min|max|sum|median(x, n|P)": "trailing window over n observations or a duration P (min / max with "
                                           "two plain expressions = least / greatest)",
    "zscore(x, n|P)": "(x - trailing mean) / trailing std",
    "pct_rank(x, n|P)": "percentile 0..1 of today's x within its trailing window",
    "rolling(fn, x, n|P)": "same as fn(x, n|P) for fn in avg mean std min max sum median zscore",
    "resample(x, W|M|Q, how=last)": "aggregate x within the calendar week / month / quarter so far (last first mean "
                                    "sum min max)",
    "ytd(x)": "x / last value of the previous calendar year - 1",
    "corr(a, b, n|P)": "rolling correlation of the one-period returns of a and b (returns=false: levels); b can be a "
                       "security reference such as 'SPX Index'",
    "beta(a, b, n|P)": "rolling OLS slope of a's returns on b's returns (same conventions as corr)",
    "rank(x, by=field)": "cross-sectional rank per date, 1 = largest",
    "cs_rank(x, by=field)": "cross-sectional percentile 0..1 per date (ascending)",
    "cs_zscore(x, by=field)": "cross-sectional z-score per date",
    "cs_demean(x, by=field)": "x minus the cross-sectional mean per date",
    "cs_mean(x, by=field) / cs_median": "cross-sectional mean / median per date",
    "abs ln log log10 exp sqrt sign floor ceil round(x, n) least greatest coalesce nz(x) if(c, a, b) clip(x, lo, hi) "
    "isnull(x)": "scalar functions",
}

_ISO_CCY = re.compile(r"^[A-Z]{3}$")


def _iv(n: int, unit: str) -> str:
    """Validated interval literal (n: Python int, unit from the grammar)."""
    n = int(abs(n))
    if unit == "D":
        return f"INTERVAL {n:d} DAY"
    if unit == "W":
        return f"INTERVAL {7 * n:d} DAY"
    if unit == "M":
        return f"INTERVAL {n:d} MONTH"
    if unit == "Q":
        return f"INTERVAL {3 * n:d} MONTH"
    return f"INTERVAL {n:d} YEAR"


def shift_date(d: date, n: int, unit: str) -> date:
    """d + n units (calendar, month ends clipped)."""
    if unit == "D":
        return d + timedelta(days=n)
    if unit == "W":
        return d + timedelta(days=7 * n)
    months = n * {"M": 1, "Q": 3, "Y": 12}[unit]
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def make_grid(start: date, end: date, freq: str) -> list[date]:
    """Grid dates in [start, end]: business days (D), Fridays (W), month ends (M), quarter ends (Q); `end` is always
    the last point (the latest value of a partial period)."""
    out: list[date] = []
    if freq == "D":
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
    elif freq == "W":
        d = start + timedelta(days=(4 - start.weekday()) % 7)
        while d <= end:
            out.append(d)
            d += timedelta(days=7)
    else:
        step = 1 if freq == "M" else 3
        y, m = start.year, start.month
        if freq == "Q":
            m = ((m - 1) // 3) * 3 + 3
        while True:
            d = date(y, m, calendar.monthrange(y, m)[1])
            if d > end:
                break
            if d >= start:
                out.append(d)
            m += step
            if m > 12:
                y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    if not out or out[-1] != end:
        out.append(end)
    return out


# ------------------------------------------------------------------------------------------------ plan


@dataclass
class Column:
    name: str
    unit: str | None = None
    dtype: str = "number"
    role: str = "value"           # key | value | group | date
    field: str | None = None
    description: str | None = None
    currency: str | None = None
    model_based: bool = False

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "unit": self.unit, "dtype": self.dtype, "role": self.role, "field": self.field,
                "description": self.description, "currency": self.currency, "model_based": self.model_based}


@dataclass
class Plan:
    mode: str                     # cross_section | timeseries
    grouped: bool
    as_of: date
    grid: list[date]
    freq: str | None
    fill: str
    currency: str | None
    sql: str
    params: dict[str, Any]
    columns: list[Column]
    universe_ids: list[str]
    universe_sql: str
    universe_params: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    fields_used: list[dict[str, Any]] = field(default_factory=list)
    daily_lo: date | None = None
    needs_daily: bool = False
    fx_target: str | None = None
    est_daily_rows: int = 0
    est_cells: int = 0
    max_rows: int = 5000

    def param_shape(self) -> list[dict[str, str]]:
        """Parameter names and types (explain: never the values)."""
        def kind(v: Any) -> str:
            if isinstance(v, list):
                return "list[" + (type(v[0]).__name__ if v else "str") + "]"
            return type(v).__name__
        return [{"name": k, "type": kind(v)} for k, v in sorted(self.params.items())]


@dataclass
class Env:
    """What the compiler needs from the outside world (snapshot + resolvers)."""
    snap_as_of: date
    resolve_securities: Callable[[list[str]], tuple[list[str], list[str]]]   # tickers -> (ids, unresolved)
    universe_ids: Callable[[str, list[Any], dict[str, Any]], list[str]]        # (sql, params) -> ids
    named_universe: Callable[[str], tuple[list[str] | None, str | None, list[str]]] | None = None  # watchlist:/screen:/peers:
    lake_select: Callable[[str], str] | None = None                            # lake contract -> SELECT sql
    snapshot_tables: set[str] | None = None
    max_rows: int = 5000
    grid: list[date] | None = None                                             # explicit grid (panels)


# ------------------------------------------------------------------------------------------------ helpers

_LEVEL = {"const": 0, "static": 1, "stream": 2, "daily": 3, "grid": 4}


def _maxl(*ls: str) -> str:
    return max(ls, key=lambda x: _LEVEL[x]) if ls else "const"


class _Params:
    def __init__(self, prefix: str = "p") -> None:
        self.d: dict[str, Any] = {}
        self.prefix = prefix

    def add(self, v: Any, cast: str | None = None) -> str:
        k = f"{self.prefix}{len(self.d) + 1}"
        self.d[k] = v
        return f"${k}" + (f"::{cast}" if cast else "")


def _q(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise BqlError(f"internal: unsafe identifier {name!r}")
    return '"' + name + '"'


def _num_arg(n: Node | None) -> int | None:
    if isinstance(n, Num) and n.is_int:
        return int(n.value)
    return None


@dataclass
class _Win:
    """A window size: rows (n observations) or a calendar duration."""
    rows: int | None = None
    dur: Dur | None = None

    @property
    def days(self) -> float:
        if self.dur is not None:
            return self.dur.days
        return (self.rows or 1) * 7 / 5

    def text(self) -> str:
        return f"{self.dur.n}{self.dur.unit}" if self.dur is not None else str(self.rows)


def _win_of(n: Node | None, what: str, default: int | None = None) -> _Win:
    if n is None:
        if default is None:
            raise BqlError(f"{what} needs a window: a number of observations (21) or a duration (1M, 1Y); for a "
                           f"cross-sectional aggregate use group(...).agg({what}) or cs_mean / cs_median")
        return _Win(rows=default)
    if isinstance(n, Dur):
        if n.n <= 0:
            raise BqlError(f"{what}: the window must be positive", n.pos)
        if n.days > 365.25 * MAX_SPAN_YEARS:
            raise BqlError(f"{what}: window too long", n.pos)
        return _Win(dur=Dur(abs(n.n), n.unit))
    k = _num_arg(n)
    if k is None or k < 1 or k > 20000:
        raise BqlError(f"{what}: the window must be a positive integer or a duration such as 3M",
                       getattr(n, "pos", None))
    return _Win(rows=k)


# ------------------------------------------------------------------------------------------------ the compiler


#: mnemonics that duplicate the key columns every cross-section / time-series result already carries
KEY_DUP_FIELDS = frozenset({"ticker", "name", "id"})


def _drop_key_duplicates(q: Query) -> Query:
    """get(name, ...) / get(ticker, ...) without an alias: the result already has security_id / ticker / name key
    columns, so the value column would be a second, identical `name` column (completeness audit 2026-10-27)."""
    if q.group:
        return q
    keep = []
    for gf in q.fields:
        if gf.alias is None and isinstance(gf.expr, Name):
            f = F.resolve(gf.expr.name)
            if f is not None and f.mnemonic in KEY_DUP_FIELDS:
                continue
        keep.append(gf)
    if len(keep) == len(q.fields):
        return q
    return replace(q, fields=tuple(keep))


class Compiler:
    def __init__(self, q: Query, env: Env):
        q = _drop_key_duplicates(q)
        self.q = q
        self.env = env
        self.p = _Params()
        self.warn: list[str] = []
        self.levels: dict[int, str] = {}
        self.units: dict[int, str | None] = {}
        self.dtypes: dict[int, str] = {}
        self.lookback: dict[int, float] = {}
        self.fields: dict[str, F.FieldDef] = {}
        self.macro_cache: dict[str, Node] = {}
        self.macro_stack: list[str] = []
        self.n = 0
        # daily layer state
        self.d_ctes: list[tuple[str, str]] = []
        self.d_cur = "d0"
        self.d_cols: list[str] = []
        self.d_pending: list[tuple[str, str]] = []
        self.px_cols: dict[str, str] = {}      # sql expr over bar columns -> column name
        self.d_streams: dict[str, str] = {}    # stream key -> column
        self.d_bench: dict[str, str] = {}      # security id -> column (bench adj_close)
        self.g_streams: dict[str, str] = {}
        self.g_snap: dict[tuple[str, str], str] = {}  # (table, column expr) -> column
        self.g_static: dict[str, str] = {}
        self.stream_defs: dict[str, str] = {}  # key -> CTE name
        self.ctes_streams: list[tuple[str, str]] = []
        self.compiled: dict[int, str] = {}     # node id -> column name (materialised)
        self.lifted: list[str] = []            # daily columns sampled onto the grid
        # grid layer state
        self.g_ctes: list[tuple[str, str]] = []
        self.g_cur = "x0"
        self.g_pending: list[tuple[str, str]] = []
        self.used_fields: list[dict[str, Any]] = []

    # ----------------------------------------------------------- options
    def options(self) -> None:
        q, env = self.q, self.env
        known = {"dates", "freq", "currency", "fill", "as_of", "max_rows"}
        for k, _ in q.with_:
            if k not in known:
                raise BqlError(f"unknown with() option {k!r}; expected one of {', '.join(sorted(known))}")
        snap = env.snap_as_of
        a = q.opt("as_of")
        if a is None:
            self.as_of = snap
        elif isinstance(a, DateLit):
            self.as_of = a.value
        elif isinstance(a, Dur):
            self.as_of = shift_date(snap, a.n, a.unit)
        else:
            raise BqlError("as_of must be a date (2026-01-31) or a duration relative to the snapshot (-1M)",
                           getattr(a, "pos", None))
        if self.as_of > snap:
            self.warn.append(f"as_of {self.as_of} is after the snapshot as-of date {snap}: using {snap}")
            self.as_of = snap
        if self.as_of < date(1980, 1, 1):
            raise BqlError("as_of before 1980 is not supported")
        self.pit = self.as_of < snap
        fr = q.opt("freq")
        self.freq: str | None = None
        if fr is not None:
            v = (fr.name if isinstance(fr, Name) else fr.value if isinstance(fr, Str) else "").upper()
            if v not in ("D", "W", "M", "Q"):
                raise BqlError("freq must be D, W, M or Q", getattr(fr, "pos", None))
            self.freq = v
        fl = q.opt("fill")
        self.fill = "prev"
        if fl is not None:
            v = (fl.name if isinstance(fl, Name) else fl.value if isinstance(fl, Str) else "").lower()
            if v not in ("prev", "none"):
                raise BqlError("fill must be prev or none", getattr(fl, "pos", None))
            self.fill = v
        cc = q.opt("currency")
        self.ccy: str | None = None
        if cc is not None:
            v = (cc.name if isinstance(cc, Name) else cc.value if isinstance(cc, Str) else "").upper()
            if v not in ("LOCAL",) and not _ISO_CCY.match(v):
                raise BqlError("currency must be an ISO code (USD, EUR, ...) or local", getattr(cc, "pos", None))
            self.ccy = None if v == "LOCAL" else v
        mr = q.opt("max_rows")
        self.max_rows = env.max_rows
        if mr is not None:
            k = _num_arg(mr)
            if k is None or k < 1:
                raise BqlError("max_rows must be a positive integer")
            self.max_rows = min(k, 100_000)
        dts = q.opt("dates")
        if env.grid is not None:
            g = sorted({d for d in env.grid if d <= self.as_of})
            if not g:
                raise BqlError(f"no panel dates on or before {self.as_of}")
            if len(g) > MAX_GRID_POINTS:
                raise BqlError(f"{len(g)} panel dates (max {MAX_GRID_POINTS})")
            self.mode = "timeseries"
            self.grid = g
            self.freq = self.freq or "D"
        elif dts is None:
            self.mode = "cross_section"
            self.grid = [self.as_of]
            if self.freq:
                self.warn.append("freq is ignored without dates=")
        else:
            self.mode = "timeseries"
            start, end = self._date_range(dts)
            if end > self.as_of:
                end = self.as_of
            if start > end:
                raise BqlError(f"empty date range: {start} > {end}")
            if (end - start).days > 365.25 * MAX_SPAN_YEARS:
                raise BqlError(f"date span longer than {MAX_SPAN_YEARS} years")
            self.freq = self.freq or "D"
            self.grid = [start] if start == end else make_grid(start, end, self.freq)
            if len(self.grid) > MAX_GRID_POINTS:
                raise BqlError(f"{len(self.grid)} dates at freq={self.freq} (max {MAX_GRID_POINTS}): use a lower "
                               "frequency (W, M, Q) or a shorter range")

    def _date_of(self, n: Node) -> date:
        if isinstance(n, DateLit):
            return n.value
        if isinstance(n, Dur):
            return shift_date(self.as_of, n.n, n.unit)
        raise BqlError("dates take a date (2026-01-31) or a duration relative to as_of (-2Y, 0D)",
                       getattr(n, "pos", None))

    def _date_range(self, n: Node) -> tuple[date, date]:
        if isinstance(n, Call) and n.fn == "range":
            if len(n.args) != 2 or n.kwargs:
                raise BqlError("range(start, end) takes two dates or durations", n.pos)
            return self._date_of(n.args[0]), self._date_of(n.args[1])
        d = self._date_of(n)
        return d, d

    # ----------------------------------------------------------- name resolution
    def field(self, name: Name) -> F.FieldDef:
        f = F.resolve(name.name)
        if f is None:
            sug = F.suggest(name.name)
            raise BqlError(f"unknown field {name.name!r}" + (f"; did you mean {', '.join(sug)}?" if sug else
                                                             " (see GET /api/v1/bql/fields)"), name.pos)
        if f.mnemonic not in self.fields:
            self.fields[f.mnemonic] = f
            self.used_fields.append({"mnemonic": f.mnemonic, "table": f.table, "unit": f.unit, "group": f.group,
                                     "has_history": f.hist is not None, "model_based": f.model_based,
                                     "pit": f.pit})
            if f.model_based:
                self.warn.append(f"{f.mnemonic} is model-based (not consensus / reported data)")
        return f

    def macro(self, f: F.FieldDef) -> Node:
        if f.mnemonic not in self.macro_cache:
            self.macro_cache[f.mnemonic] = parse_expr(f.hist.expr)  # type: ignore[union-attr]
        return self.macro_cache[f.mnemonic]

    def use_snapshot(self, f: F.FieldDef, series: bool) -> bool:
        """True when the field is served from its snapshot column (latest value at the snapshot date)."""
        if f.static:
            return True
        if f.table is None:
            return False
        if self.mode == "timeseries" or self.pit or series:
            return False
        return True

    # ----------------------------------------------------------- analysis (levels, units, look-back)
    def analyze(self, n: Node, series: bool = False) -> str:
        key = id(n)
        lv, unit, dt, lb = self._analyze(n, series)
        self.levels[key] = lv
        self.units[key] = unit
        self.dtypes[key] = dt
        self.lookback[key] = lb
        return lv

    def _analyze(self, n: Node, series: bool) -> tuple[str, str | None, str, float]:
        if isinstance(n, (Num, Str, DateLit, Bool, Null)):
            dt = "number" if isinstance(n, Num) else "text" if isinstance(n, Str) else "date" if isinstance(n, DateLit) \
                else "bool"
            return "const", ("pct" if isinstance(n, Num) and n.percent else None), dt, 0.0
        if isinstance(n, Dur):
            raise BqlError(f"a duration ({to_text(n)}) is only valid as a window / date argument", n.pos)
        if isinstance(n, ListLit):
            for x in n.items:
                if not isinstance(x, (Num, Str, DateLit, Bool)):
                    raise BqlError("lists take literal values (strings, numbers, dates)", x.pos)
            if len(n.items) > MAX_LIST:
                raise BqlError(f"list longer than {MAX_LIST} items", n.pos)
            for x in n.items:
                self.analyze(x)
            return "const", None, "list", 0.0
        if isinstance(n, Name):
            f = self.field(n)
            if self.use_snapshot(f, series):
                return ("static" if f.static else "grid"), f.unit, f.dtype, 0.0
            if f.hist is None:
                why = ("with dates=" if self.mode == "timeseries" else
                       f"at as_of {self.as_of}" if self.pit else "inside a time-series function")
                raise BqlError(f"{f.mnemonic} has no point-in-time history (snapshot only): it cannot be used {why}",
                               n.pos)
            h = f.hist
            if h.kind == "px":
                return "daily", f.unit, f.dtype, 0.0
            if h.kind in ("event", "period"):
                return "stream", f.unit, f.dtype, 0.0
            if h.kind == "expr":
                if f.mnemonic in self.macro_stack:
                    raise BqlError(f"internal: recursive field definition {f.mnemonic}")
                self.macro_stack.append(f.mnemonic)
                try:
                    m = self.macro(f)
                    lv = self.analyze(m, True)
                finally:
                    self.macro_stack.pop()
                return lv, f.unit, f.dtype, self.lookback[id(m)]
            raise BqlError(f"internal: unknown history kind {h.kind}")
        if isinstance(n, Unary):
            lv = self.analyze(n.operand, series)
            if n.op == "not":
                return lv, "bool", "bool", self.lookback[id(n.operand)]
            return lv, self.units[id(n.operand)], self.dtypes[id(n.operand)], self.lookback[id(n.operand)]
        if isinstance(n, Bin):
            a = self.analyze(n.left, series)
            b = self.analyze(n.right, series)
            lb = max(self.lookback[id(n.left)], self.lookback[id(n.right)])
            if n.op in ("and", "or", "==", "!=", "<", "<=", ">", ">=", "in"):
                if n.op == "in" and not isinstance(n.right, ListLit):
                    raise BqlError("`in` takes a list: sector in ['TECH', 'HLTH']", n.pos)
                return _maxl(a, b), "bool", "bool", lb
            ua, ub = self.units[id(n.left)], self.units[id(n.right)]
            if n.op in ("+", "-"):
                unit = ua if (ua == ub or ub is None) else (ub if ua is None else None)
            elif n.op == "/" and ua == ub and ua is not None:
                unit = "ratio"
            else:
                unit = ua if ub is None and n.op in ("*", "/") and isinstance(n.right, Num) else None
            return _maxl(a, b), unit, "number", lb
        if isinstance(n, Call):
            return self._analyze_call(n, series)
        raise BqlError(f"unsupported expression {type(n).__name__}")

    def _analyze_call(self, n: Call, series: bool) -> tuple[str, str | None, str, float]:
        fn = n.fn
        if fn == "rolling":
            n2 = self._rolling_rewrite(n)
            self.rewrites[id(n)] = n2
            lv = self.analyze(n2, series)
            return lv, self.units[id(n2)], self.dtypes[id(n2)], self.lookback[id(n2)]
        if fn in ("min", "max") and len(n.args) >= 2 and not isinstance(n.args[1], Dur) and _num_arg(n.args[1]) is None:
            n2 = Call("least" if fn == "min" else "greatest", n.args, n.kwargs, pos=n.pos)
            self.rewrites[id(n)] = n2
            lv = self.analyze(n2, series)
            return lv, self.units[id(n2)], self.dtypes[id(n2)], self.lookback[id(n2)]
        if fn in TS_FUNCS:
            return self._analyze_ts(n)
        if fn in CS_FUNCS:
            if not n.args or len(n.args) > 1:
                raise BqlError(f"{fn}(x[, by=field]) takes one expression", n.pos)
            for k, v in n.kwargs:
                if k != "by":
                    raise BqlError(f"{fn}: unknown keyword {k!r}", n.pos)
                if self.analyze(v) not in ("static", "const"):
                    raise BqlError(f"{fn}: by= must be an identity field (sector, region, country, ...)", v.pos)
            self.analyze(n.args[0], series)
            unit = {"rank": "rank", "cs_rank": "pct", "cs_zscore": "z"}.get(fn, self.units[id(n.args[0])])
            return "grid", unit, "number", self.lookback[id(n.args[0])]
        if fn in SCALAR_FUNCS:
            k = SCALAR_FUNCS[fn]
            if k > 0 and len(n.args) != k:
                raise BqlError(f"{fn}() takes {k} argument(s)", n.pos)
            if k < 0 and not n.args:
                raise BqlError(f"{fn}() needs arguments", n.pos)
            if fn == "round" and (len(n.args) > 2 or (len(n.args) == 2 and _num_arg(n.args[1]) is None)):
                raise BqlError("round(x[, digits]) takes an integer number of digits", n.pos)
            if n.kwargs:
                raise BqlError(f"{fn}() takes no keyword arguments", n.pos)
            lvs = [self.analyze(a, series) for a in n.args]
            lb = max(self.lookback[id(a)] for a in n.args)
            first = n.args[1] if fn == "if" else n.args[0]
            unit = None if fn in ("ln", "log", "log10", "exp", "sqrt", "sign") else self.units[id(first)]
            dt = "bool" if fn == "isnull" else self.dtypes[id(first)]
            return _maxl(*lvs), unit, dt, lb
        if fn in ("members", "region", "sector", "subsector", "country", "range", "type"):
            raise BqlError(f"{fn}() is only valid in for(...) / with(dates=...)", n.pos)
        raise BqlError(f"unknown function {fn!r}", n.pos)

    def _rolling_rewrite(self, n: Call) -> Call:
        # rolling(fn, x, P) or rolling(fn(x), P)
        if len(n.args) == 3 and isinstance(n.args[0], Name):
            fn = n.args[0].name.lower()
            x, w = n.args[1], n.args[2]
        elif len(n.args) == 2 and isinstance(n.args[0], Call) and len(n.args[0].args) == 1:
            fn = n.args[0].fn
            x, w = n.args[0].args[0], n.args[1]
        else:
            raise BqlError("rolling(fn, x, window) or rolling(fn(x), window)", n.pos)
        if fn not in TS_ROLL and fn != "zscore":
            raise BqlError(f"rolling(): fn must be one of {', '.join(sorted(set(TS_ROLL) | {'zscore'}))}", n.pos)
        return Call(fn, (x, w), (), pos=n.pos)

    def _analyze_ts(self, n: Call) -> tuple[str, str | None, str, float]:
        fn = n.fn
        allowed_kw = {"resample": {"how"}, "corr": {"returns"}, "beta": {"returns"}}.get(fn, set())
        for k, _ in n.kwargs:
            if k not in allowed_kw:
                raise BqlError(f"{fn}: unknown keyword {k!r}", n.pos)
        if fn == "ytd":
            if len(n.args) != 1:
                raise BqlError("ytd(x) takes one series", n.pos)
            lv = self.analyze(n.args[0], True)
            return self._ts_level(lv, n), "pct", "number", self.lookback[id(n.args[0])] + 372
        if fn == "resample":
            if len(n.args) != 2 or not isinstance(n.args[1], (Name, Str)):
                raise BqlError("resample(x, W|M|Q[, how=last])", n.pos)
            fr = (n.args[1].name if isinstance(n.args[1], Name) else n.args[1].value).upper()
            if fr not in ("W", "M", "Q"):
                raise BqlError("resample frequency must be W, M or Q", n.pos)
            how = n.kw("how")
            if how is not None and (not isinstance(how, (Name, Str)) or
                                    (how.name if isinstance(how, Name) else how.value).lower() not in
                                    ("last", "first", "mean", "avg", "sum", "min", "max", "median")):
                raise BqlError("resample how= last|first|mean|sum|min|max|median", n.pos)
            lv = self.analyze(n.args[0], True)
            return self._ts_level(lv, n), self.units[id(n.args[0])], "number", self.lookback[id(n.args[0])] + 95
        if fn in ("corr", "beta"):
            if len(n.args) != 3:
                raise BqlError(f"{fn}(a, b, window)", n.pos)
            w = _win_of(n.args[2], fn)
            la = self.analyze(n.args[0], True)
            if isinstance(n.args[1], Str):
                lb_ = "daily"
                self.levels[id(n.args[1])] = "const"
                self.lookback[id(n.args[1])] = 0.0
            else:
                lb_ = self.analyze(n.args[1], True)
            lb = max(self.lookback[id(n.args[0])], self.lookback.get(id(n.args[1]), 0.0)) + w.days + 7
            lv = self._ts_level(_maxl(la, lb_), n)
            return lv, ("ratio" if fn == "corr" else "x"), "number", lb
        # chg chg_pct lag + rolling aggregates, zscore, pct_rank
        if not n.args or len(n.args) > 2:
            raise BqlError(f"{fn}(x, window)", n.pos)
        default = 1 if fn in ("chg", "chg_pct", "lag") else None
        w = _win_of(n.args[1] if len(n.args) > 1 else None, fn, default)
        lv = self.analyze(n.args[0], True)
        ux = self.units[id(n.args[0])]
        unit = {"chg_pct": "pct", "zscore": "z", "pct_rank": "pct", "count": "count"}.get(fn, ux)
        dt = "number" if fn != "lag" else self.dtypes[id(n.args[0])]
        return self._ts_level(lv, n), unit, dt, self.lookback[id(n.args[0])] + w.days + 5

    def _ts_level(self, arg_level: str, n: Node) -> str:
        if arg_level in ("const", "static"):
            raise BqlError(f"{to_text(n)}: a time-series function needs a series argument", n.pos)
        return "grid" if arg_level == "grid" else "daily"

    # ----------------------------------------------------------- daily layer
    def _d_col(self, expr: str, window: bool = False, deps: tuple[str, ...] = ()) -> str:
        """Add a computed column to the daily layer; flushes pending columns first when the expression needs them."""
        pend = {c for c, _ in self.d_pending}
        deps = tuple(deps) + self._deps(expr)
        if any(d in pend for d in deps):  # independent windows share a layer
            self._d_flush()
        self.n += 1
        c = f"c{self.n}"
        self.d_pending.append((c, expr))
        return c

    def _d_flush(self) -> None:
        if not self.d_pending:
            return
        name = f"d{len(self.d_ctes) + 1}"
        sel = ", ".join(f"{e} AS {c}" for c, e in self.d_pending)
        self.d_ctes.append((name, f"SELECT *, {sel} FROM {self.d_cur}"))
        self.d_cur = name
        self.d_pending = []

    def _g_col(self, expr: str, window: bool = False, deps: tuple[str, ...] = ()) -> str:
        pend = {c for c, _ in self.g_pending}
        deps = tuple(deps) + self._deps(expr)
        if any(d in pend for d in deps):  # independent windows share a layer
            self._g_flush()
        self.n += 1
        c = f"c{self.n}"
        self.g_pending.append((c, expr))
        return c

    def _g_flush(self) -> None:
        if not self.g_pending:
            return
        name = f"x{len(self.g_ctes) + 1}"
        sel = ", ".join(f"{e} AS {c}" for c, e in self.g_pending)
        self.g_ctes.append((name, f"SELECT *, {sel} FROM {self.g_cur}"))
        self.g_cur = name
        self.g_pending = []

    # ----------------------------------------------------------- streams
    def stream_cte(self, f: F.FieldDef) -> tuple[str, str]:
        """(CTE name, key column of the base) for an event / period history."""
        h = f.hist
        assert h is not None
        skey = f"{h.kind}|{h.table}|{h.key}|{h.date}|{h.value}|{h.where}|{h.period}|{h.prefer}"
        if skey in self.stream_defs:
            return self.stream_defs[skey], h.key
        if h.table.startswith("lake:"):
            if self.env.lake_select is None:
                raise BqlError(f"{f.mnemonic}: lake history unavailable")
            src = f"({self.env.lake_select(h.table[5:])})"
        else:
            if self.env.snapshot_tables is not None and h.table not in self.env.snapshot_tables:
                raise BqlError(f"{f.mnemonic}: history table {h.table} is not in this snapshot")
            src = _q(h.table)
        kcol = _q(h.key)
        ucol = "issuer_id" if h.key == "issuer_id" else "security_id"
        cond = [f"{kcol} IN (SELECT {ucol} FROM u)", f"CAST({_q(h.date)} AS DATE) <= $asof"]
        if h.where:
            cond.append(f"({h.where})")
        value = h.value if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", h.value) is None else _q(h.value)
        if f.dtype == "number":
            value = f"TRY_CAST({value} AS DOUBLE)"
        name = f"s{len(self.stream_defs) + 1}"
        if h.kind == "event":
            sql = (f"SELECT {kcol} AS k, CAST({_q(h.date)} AS DATE) AS d, {value} AS v FROM {src} "
                   f"WHERE {' AND '.join(cond)}")
        else:
            prefer = f"({h.prefer})" if h.prefer else "FALSE"
            inner = (f"SELECT {kcol} AS k, CAST({_q(h.date)} AS DATE) AS d, {_q(h.period)} AS pe, {value} AS v, "
                     f"{prefer} AS pr FROM {src} WHERE {' AND '.join(cond)} AND {value} IS NOT NULL")
            sql = (f"SELECT k, d, v FROM (SELECT * FROM ({inner}) "
                   f"QUALIFY pe >= max(pe) OVER (PARTITION BY k ORDER BY d, pe ROWS BETWEEN UNBOUNDED PRECEDING AND "
                   f"CURRENT ROW)) QUALIFY row_number() OVER (PARTITION BY k, d ORDER BY pe DESC, pr DESC) = 1")
        self.ctes_streams.append((name, sql))
        self.stream_defs[skey] = name
        return name, h.key

    # ----------------------------------------------------------- expression compilation
    def comp(self, n: Node, level: str) -> str:
        """SQL of `n` evaluated at `level` ('daily' or 'grid') - inline expression over that layer's columns."""
        n = self.rewrites.get(id(n), n)
        lv = self.levels[id(n)]
        if level == "grid" and lv == "daily":
            return self.lift(n)
        if isinstance(n, Num):
            return self.p.add(float(n.value), "DOUBLE")
        if isinstance(n, Str):
            return self.p.add(n.value, "VARCHAR")
        if isinstance(n, DateLit):
            return self.p.add(n.value, "DATE")
        if isinstance(n, Bool):
            return "TRUE" if n.value else "FALSE"
        if isinstance(n, Null):
            return "NULL"
        if isinstance(n, Name):
            return self.comp_name(n, level)
        if isinstance(n, Unary):
            x = self.comp(n.operand, level)
            return f"(NOT {x})" if n.op == "not" else f"(-{x})"
        if isinstance(n, Bin):
            return self.comp_bin(n, level)
        if isinstance(n, Call):
            return self.comp_call(n, level)
        raise BqlError(f"unsupported expression {type(n).__name__}")

    def comp_bin(self, n: Bin, level: str) -> str:
        if n.op == "in":
            x = self.comp(n.left, level)
            assert isinstance(n.right, ListLit)
            if not n.right.items:
                return "FALSE"
            vals = ", ".join(self.comp(i, level) for i in n.right.items)
            return f"({x} IN ({vals}))"
        if n.op in ("==", "!=") and (isinstance(n.right, Null) or isinstance(n.left, Null)):
            other = n.left if isinstance(n.right, Null) else n.right
            x = self.comp(other, level)
            return f"({x} IS {'NOT ' if n.op == '!=' else ''}NULL)"
        a = self.comp(n.left, level)
        b = self.comp(n.right, level)
        if n.op == "and":
            return f"({a} AND {b})"
        if n.op == "or":
            return f"({a} OR {b})"
        if n.op == "==":
            return f"({a} = {b})"
        if n.op == "!=":
            return f"({a} <> {b})"
        if n.op in ("<", "<=", ">", ">=", "+", "-", "*"):
            return f"({a} {n.op} {b})"
        if n.op == "/":
            return f"({a} / NULLIF({b}, 0))"
        if n.op == "^":
            return f"power({a}, {b})"
        raise BqlError(f"unsupported operator {n.op}")

    def comp_name(self, n: Name, level: str) -> str:
        f = self.fields[F.resolve(n.name).mnemonic]  # type: ignore[union-attr]
        lv = self.levels[id(n)]
        if lv == "static":
            return self.snap_col(f, level)
        if lv == "grid":
            return self.snap_col(f, level)
        h = f.hist
        assert h is not None
        if h.kind == "expr":
            m = self.macro(f)
            return self.comp(m, level)
        if h.kind == "px":
            if level != "daily":
                raise BqlError(f"internal: price field {f.mnemonic} outside the daily layer")
            return self.px_col(f)
        # stream
        if level == "daily":
            return self.d_stream_col(f)
        return self.g_stream_col(f)

    def px_col(self, f: F.FieldDef) -> str:
        h = f.hist
        assert h is not None
        expr = _q(h.value) if re.fullmatch(r"[a-z_]+", h.value) else h.value
        conv = self.fx_factor_daily(f.unit)
        if conv:
            expr = f"({expr}) * {conv}"
        key = expr
        if key not in self.px_cols:
            self.px_cols[key] = f"b{len(self.px_cols) + 1}"
        return self.px_cols[key]

    def fx_factor_daily(self, unit: str) -> str:
        if not self.ccy or unit not in F.CONVERTIBLE_UNITS:
            return ""
        local = "(close_usd / NULLIF(close, 0))" if unit in ("price", "currency") else "1.0"
        if self.ccy == "USD":
            return local
        return f"{local} / NULLIF(_tfx, 0)"

    def d_stream_col(self, f: F.FieldDef) -> str:
        name, key = self.stream_cte(f)
        tag = f"{name}"
        if tag not in self.d_streams:
            self.d_streams[tag] = f"v_{name}"
            self.d_stream_keys[tag] = key
        col = self.d_streams[tag]
        if self.ccy and f.unit in F.CONVERTIBLE_UNITS:
            if f.unit == "currency_usd":
                return col if self.ccy == "USD" else f"({col} / NULLIF(_tfx, 0))"
            return f"({col} * {self.fx_factor_daily(f.unit)})"
        return col

    def g_stream_col(self, f: F.FieldDef) -> str:
        name, key = self.stream_cte(f)
        if name not in self.g_streams:
            self.g_streams[name] = f"w_{name}"
            self.g_stream_keys[name] = key
        col = self.g_streams[name]
        return self.convert_grid(col, f, latest_fx=f.unit in ("price", "currency"))

    def snap_col(self, f: F.FieldDef, level: str) -> str:
        if level == "daily":
            if f.static and f.table == "sec":
                # identity attributes on the daily panel come from the base join
                k = ("sec", f.column or "")
                if k not in self.d_static:
                    self.d_static[k] = f"t{len(self.d_static) + 1}"
                return self.d_static[k]
            raise BqlError(f"{f.mnemonic} (snapshot value) cannot be used inside a time-series function")
        key = (f.table or "", f.column or "")
        if key not in self.g_snap:
            self.g_snap[key] = f"z{len(self.g_snap) + 1}"
        col = self.g_snap[key]
        if f.static:
            return col
        return self.convert_grid(col, f, latest_fx=True)

    def convert_grid(self, col: str, f: F.FieldDef, latest_fx: bool) -> str:
        if not self.ccy or f.unit not in F.CONVERTIBLE_UNITS:
            return col
        tgt = "1.0" if self.ccy == "USD" else "_tfx"
        if f.unit == "currency_usd":
            return col if self.ccy == "USD" else f"({col} / NULLIF({tgt}, 0))"
        if latest_fx and (self.mode == "timeseries" or self.pit):
            w = f"{f.mnemonic}: converted with the latest FX rate (no FX history for this field)"
            if w not in self.warn:
                self.warn.append(w)
        return f"({col} * _sfx / NULLIF({tgt}, 0))"

    def lift(self, n: Node) -> str:
        """Compile a daily-level subtree and sample it onto the grid (as of each grid date)."""
        key = id(n)
        if key not in self.compiled:
            expr = self.comp(n, "daily")
            if re.fullmatch(r"(c|b|t|bm)\d+|v_s\d+", expr):
                col = expr
            else:
                # evaluated while sampling onto the grid (keeps the daily window chain shallow: DuckDB's filter
                # pushdown degrades badly on long chains of inlined projection layers)
                self.n += 1
                col = f"c{self.n}"
                self.lift_exprs[col] = expr
            self.compiled[key] = col
            if col not in self.lifted:
                self.lifted.append(col)
        return self.compiled[key]

    # ----------------------------------------------------------- calls
    def comp_call(self, n: Call, level: str) -> str:
        fn = n.fn
        if fn in SCALAR_FUNCS:
            args = [self.comp(a, level) for a in n.args]
            if fn == "if":
                return f"(CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END)"
            if fn == "nz":
                return f"coalesce({args[0]}, 0)"
            if fn == "isnull":
                return f"({args[0]} IS NULL)"
            if fn == "clip":
                return f"least(greatest({args[0]}, {args[1]}), {args[2]})"
            if fn == "round":
                nd = _num_arg(n.args[1]) if len(n.args) > 1 else 0
                return f"round({args[0]}, {int(nd or 0):d})"
            if fn in ("ln", "log"):
                return f"(CASE WHEN {args[0]} > 0 THEN ln({args[0]}) END)"
            if fn == "log10":
                return f"(CASE WHEN {args[0]} > 0 THEN log10({args[0]}) END)"
            if fn == "sqrt":
                return f"(CASE WHEN {args[0]} >= 0 THEN sqrt({args[0]}) END)"
            return f"{fn}({', '.join(args)})"
        if fn in CS_FUNCS:
            if level != "grid":
                raise BqlError(f"{fn}() is cross-sectional: it cannot be used inside a time-series function", n.pos)
            return self.comp_cs(n)
        if fn in TS_FUNCS:
            return self.comp_ts(n, level)
        raise BqlError(f"unknown function {fn!r}", n.pos)

    def comp_cs(self, n: Call) -> str:
        x = self.comp(n.args[0], "grid")
        xc = self._g_col(x, deps=self._deps(x))
        part = ["d"]
        by = n.kw("by")
        if by is not None:
            part.append(self._g_col(self.comp(by, "grid")))
        p = ", ".join(part)
        fn = n.fn
        if fn == "rank":
            e = f"CASE WHEN {xc} IS NULL THEN NULL ELSE rank() OVER (PARTITION BY {p}, {xc} IS NULL ORDER BY {xc} DESC) END"
        elif fn == "cs_rank":
            e = (f"CASE WHEN {xc} IS NULL THEN NULL ELSE percent_rank() OVER (PARTITION BY {p}, {xc} IS NULL "
                 f"ORDER BY {xc}) END")
        elif fn == "cs_zscore":
            e = (f"({xc} - avg({xc}) OVER (PARTITION BY {p})) / NULLIF(stddev_samp({xc}) OVER (PARTITION BY {p}), 0)")
        elif fn == "cs_demean":
            e = f"({xc} - avg({xc}) OVER (PARTITION BY {p}))"
        elif fn == "cs_mean":
            e = f"avg({xc}) OVER (PARTITION BY {p})"
        else:
            e = f"median({xc}) OVER (PARTITION BY {p})"
        return self._g_col(e, window=True)

    @staticmethod
    def _deps(expr: str) -> tuple[str, ...]:
        return tuple(re.findall(r"\b(c\d+)\b", expr))

    def comp_ts(self, n: Call, level: str) -> str:
        """Time-series function at `level` (daily: partition security_id order date; grid: order d)."""
        fn = n.fn
        col_fn = self._d_col if level == "daily" else self._g_col
        oc = "date" if level == "daily" else "d"

        def mat(x: Node) -> str:
            e = self.comp(x, level)
            if re.fullmatch(r"(c|b|t|z)\d+|v_s\d+|w_s\d+", e):
                return e
            return col_fn(e, deps=self._deps(e))

        def over(frame: str = "", part: str = "") -> str:
            return f"OVER (PARTITION BY security_id{part} ORDER BY {oc}{(' ' + frame) if frame else ''})"

        def frame(w: _Win) -> str:
            if w.dur is not None:
                return f"RANGE BETWEEN {_iv(w.dur.n, w.dur.unit)} PRECEDING AND CURRENT ROW"
            return f"ROWS BETWEEN {w.rows - 1:d} PRECEDING AND CURRENT ROW"

        def min_obs(w: _Win) -> int:
            if w.dur is not None:
                per = 1.0 if level == "daily" else max(1.0, self.grid_step_days)
                exp = w.dur.days * (5 / 7 if level == "daily" else 1) / per
                return max(1, int(exp * 0.6))
            return max(1, int(w.rows * 0.8)) if (w.rows or 1) >= 5 else (w.rows or 1)

        def at_lag(xc: str, w: _Win) -> str:
            if w.dur is not None:
                return (f"last_value({xc} IGNORE NULLS) {over(f'RANGE BETWEEN UNBOUNDED PRECEDING AND {_iv(w.dur.n, w.dur.unit)} PRECEDING')}")
            return f"lag({xc}, {w.rows:d}) {over()}"

        if fn in ("chg", "chg_pct", "lag"):
            w = _win_of(n.args[1] if len(n.args) > 1 else None, fn, 1)
            xc = mat(n.args[0])
            lagc = col_fn(at_lag(xc, w), window=True)
            if fn == "lag":
                return lagc
            if fn == "chg":
                return f"({xc} - {lagc})"
            return f"({xc} / NULLIF({lagc}, 0) - 1)"
        if fn in TS_ROLL or fn in ("zscore", "pct_rank"):
            w = _win_of(n.args[1] if len(n.args) > 1 else None, fn)
            xc = mat(n.args[0])
            fr = frame(w)
            k = min_obs(w)
            cnt = col_fn(f"count({xc}) {over(fr)}", window=True)
            if fn in TS_ROLL:
                agg = TS_ROLL[fn]
                if fn == "std":
                    k = max(k, 2)
                e = col_fn(f"{agg}({xc}) {over(fr)}", window=True)
                if fn == "count":
                    return cnt
                return f"(CASE WHEN {cnt} >= {k:d} THEN {e} END)"
            if fn == "zscore":
                m = col_fn(f"avg({xc}) {over(fr)}", window=True)
                s = col_fn(f"stddev_samp({xc}) {over(fr)}", window=True)
                return f"(CASE WHEN {cnt} >= {max(k, 3):d} THEN ({xc} - {m}) / NULLIF({s}, 0) END)"
            lst = col_fn(f"list({xc}) {over(fr)}", window=True)
            return (f"(CASE WHEN {cnt} >= {max(k, 3):d} AND {xc} IS NOT NULL THEN "
                    f"(len(list_filter({lst}, e -> e < {xc})) + 0.0) / NULLIF(len(list_filter({lst}, e -> e IS NOT NULL)) - 1, 0) END)")
        if fn == "ytd":
            xc = mat(n.args[0])
            flag = col_fn(f"(year({oc}) <> year(lead({oc}) {over()}))", window=True)
            ye = col_fn(f"last_value(CASE WHEN {flag} THEN {xc} END IGNORE NULLS) "
                        f"{over('ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING')}", window=True)
            return f"({xc} / NULLIF({ye}, 0) - 1)"
        if fn == "resample":
            xc = mat(n.args[0])
            fr = n.args[1]
            unit = {"W": "week", "M": "month", "Q": "quarter"}[
                (fr.name if isinstance(fr, Name) else fr.value).upper()]  # type: ignore[union-attr]
            how_n = n.kw("how")
            how = (how_n.name if isinstance(how_n, Name) else how_n.value).lower() if how_n is not None else "last"  # type: ignore[union-attr]
            part = f", date_trunc('{unit}', {oc})"
            fr_s = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
            agg = {"last": f"last_value({xc} IGNORE NULLS)", "first": f"first_value({xc} IGNORE NULLS)",
                   "mean": f"avg({xc})", "avg": f"avg({xc})", "sum": f"sum({xc})", "min": f"min({xc})",
                   "max": f"max({xc})", "median": f"median({xc})"}[how]
            return col_fn(f"{agg} {over(fr_s, part)}", window=True)
        if fn in ("corr", "beta"):
            w = _win_of(n.args[2], fn)
            rets = n.kw("returns")
            use_ret = not (isinstance(rets, Bool) and not rets.value)
            ac = mat(n.args[0])
            if isinstance(n.args[1], Str):
                if level != "daily":
                    raise BqlError(f"{fn}: a security reference needs a daily series as first argument", n.pos)
                bc = self.bench_col(n.args[1])
            else:
                bc = mat(n.args[1])
            if use_ret:
                ra = col_fn(f"({ac} / NULLIF(lag({ac}) {over()}, 0) - 1)", window=True)
                rb = col_fn(f"({bc} / NULLIF(lag({bc}) {over()}, 0) - 1)", window=True)
            else:
                ra, rb = ac, bc
            fr = frame(w)
            k = max(min_obs(w), 3)
            cnt = col_fn(f"regr_count({ra}, {rb}) {over(fr)}", window=True)
            agg = "corr" if fn == "corr" else "regr_slope"
            e = col_fn(f"{agg}({ra}, {rb}) {over(fr)}", window=True)
            return f"(CASE WHEN {cnt} >= {k:d} THEN {e} END)"
        raise BqlError(f"unknown function {fn!r}", n.pos)

    def bench_col(self, s: Str) -> str:
        ids, bad = self.env.resolve_securities([s.value])
        if not ids:
            raise BqlError(f"unknown security {s.value!r}", s.pos)
        sid = ids[0]
        if sid not in self.d_bench:
            self.d_bench[sid] = f"bm{len(self.d_bench) + 1}"
        return self.d_bench[sid]

    # ----------------------------------------------------------- universe
    def universe(self, node: Node | None) -> tuple[str, dict[str, Any], list[str]]:
        if node is None:
            raise BqlError("a query needs for(universe): members('mvp_us'), ['AAPL US', 'SAP GY'], region('EU'), ...")
        up = _Params("u")
        notes: list[str] = []
        sql = self._u(node, up, notes)
        return sql, up.d, notes

    _EQ = ("coalesce(s.security_type, 'common') NOT IN ('fx', 'future', 'index', 'etf') "
           "AND coalesce(s.is_active, TRUE)")

    def _u(self, n: Node, up: _Params, notes: list[str]) -> str:
        if isinstance(n, Bin) and n.op in ("and", "or", "-"):
            a = self._u(n.left, up, notes)
            b = self._u(n.right, up, notes)
            op = {"and": "INTERSECT", "or": "UNION", "-": "EXCEPT"}[n.op]
            return f"(({a}) {op} ({b}))"
        if isinstance(n, (ListLit, Str)):
            items = n.items if isinstance(n, ListLit) else (n,)
            vals = []
            for it in items:
                if not isinstance(it, Str):
                    raise BqlError("ticker lists take strings: ['AAPL US', 'SAP GY']", it.pos)
                vals.append(it.value)
            if len(vals) > MAX_LIST:
                raise BqlError(f"ticker list longer than {MAX_LIST}")
            ids, bad = self.env.resolve_securities(vals)
            if bad:
                notes.append(f"unresolved securities: {', '.join(bad[:20])}" + (" ..." if len(bad) > 20 else ""))
            return f"SELECT unnest({up.add(ids, 'VARCHAR[]')}) AS security_id"
        if isinstance(n, Call):
            fn = n.fn
            vals = []
            for a in n.args:
                if not isinstance(a, Str) and not isinstance(a, Name):
                    raise BqlError(f"{fn}() takes names: {fn}('EU')", a.pos)
                vals.append(a.value if isinstance(a, Str) else a.name)
            if not vals:
                raise BqlError(f"{fn}() needs at least one value", n.pos)
            if fn == "members":
                parts = [self._members(v, up, notes, n.pos) for v in vals]
                return " UNION ".join(f"({p})" for p in parts)
            col = {"region": "region", "sector": "sector_id", "subsector": "subsector_id", "country": "country",
                   "type": "security_type", "exchange": "mic", "mic": "mic", "currency": "currency"}.get(fn)
            if col is None:
                raise BqlError(f"unknown universe function {fn!r}; use members, region, sector, subsector, country, "
                               "type, exchange, currency or a ticker list", n.pos)
            if fn == "sector":
                vals = [self._sector_id(v, n.pos) for v in vals]
            elif fn == "subsector":
                vals = [self._subsector_id(v, n.pos) for v in vals]
            elif fn in ("region", "country", "currency", "mic", "exchange"):
                vals = [v.upper() for v in vals]
            elif fn == "type":
                vals = [v.lower() for v in vals]
            base = "coalesce(s.is_active, TRUE)" if fn == "type" else \
                self._EQ + " AND coalesce(s.is_primary_security, TRUE)"
            return f"SELECT s.security_id FROM sec s WHERE {base} AND s.{col} IN ({', '.join(up.add(v, 'VARCHAR') for v in vals)})"
        raise BqlError("for() takes members(...), region(...), sector(...), country(...), a ticker list, or "
                       "combinations with & | -", getattr(n, "pos", None))

    _UNIVERSE_ALIASES = {
        "mvp_us": ("mvp_us", None), "s&p 500": ("mvp_us", "S&P 500"), "spx": ("mvp_us", "S&P 500"),
        "spx index": ("mvp_us", "S&P 500"), "sp500": ("mvp_us", "S&P 500"), "us large caps": ("mvp_us", None),
        "mvp_global": ("mvp_global", None), "global": ("mvp_global", None),
        "eu_broad": ("eu_broad", None), "stoxx 600": ("eu_broad", "STOXX Europe 600"),
        "sxxp": ("eu_broad", "STOXX Europe 600"), "sxxp index": ("eu_broad", "STOXX Europe 600"),
        "europe": ("eu_broad", None), "full": ("full", None), "all": ("full", None),
    }

    def _members(self, v: str, up: _Params, notes: list[str], pos: int) -> str:
        low = v.strip().lower()
        if low in ("watch", "watchlist", "watchlists"):
            ids, why, n2 = self.env.named_universe("watch:") if self.env.named_universe else ([], None, [])
            notes.extend(n2)
            return (f"SELECT s.security_id FROM sec s WHERE s.u_watch UNION "
                    f"SELECT unnest({up.add(ids or [], 'VARCHAR[]')})")
        if ":" in low and low.split(":", 1)[0] in ("watchlist", "screen", "peers", "peer", "wl"):
            if self.env.named_universe is None:
                raise BqlError(f"members({v!r}) needs the app state", pos)
            ids, why, n2 = self.env.named_universe(v.strip())
            notes.extend(n2)
            if ids is None:
                raise BqlError(why or f"unknown {v!r}", pos)
            return f"SELECT unnest({up.add(ids, 'VARCHAR[]')}) AS security_id"
        hit = self._UNIVERSE_ALIASES.get(low)
        if hit is None:
            raise BqlError(f"unknown universe {v!r}: use mvp_us ('S&P 500'), mvp_global, eu_broad ('STOXX 600'), "
                           "full, watch, 'watchlist:NAME', 'screen:NAME' or 'peers:GROUP' (index constituents other "
                           "than these approximations are not available)", pos)
        uid, approx = hit
        if approx:
            notes.append(f"members('{v}') = the {uid} universe (approximation of the {approx}: top names by market "
                         "cap, current membership; not the official index constituents)")
        if uid == "full":
            return f"SELECT s.security_id FROM sec s WHERE s.u_full AND {self._EQ}"
        return f"SELECT security_id FROM universe WHERE universe_id = {up.add(uid, 'VARCHAR')}"

    def _sector_id(self, v: str, pos: int) -> str:
        from app.api.routers._common import taxonomy

        tax = taxonomy()["sectors"]
        u = v.strip()
        if u.upper() in tax:
            return u.upper()
        low = u.lower()
        syn = {"tech": "TECH", "technology": "TECH", "it": "TECH", "health": "HLTH", "healthcare": "HLTH",
               "financials": "FINS", "finance": "FINS", "banks": "FINS", "energy": "ENRG", "utilities": "UTIL",
               "materials": "MATR", "industrials": "INDU", "real estate": "REAL", "reits": "REAL",
               "communication": "COMM", "communications": "COMM", "media": "COMM", "staples": "CSTA",
               "consumer staples": "CSTA", "discretionary": "CDIS", "consumer discretionary": "CDIS"}
        if low in syn and syn[low] in tax:
            return syn[low]
        for sid, name in tax.items():
            if name.lower() == low or name.lower().startswith(low):
                return sid
        raise BqlError(f"unknown sector {v!r}; one of {', '.join(f'{k} ({n})' for k, n in tax.items())}", pos)

    def _subsector_id(self, v: str, pos: int) -> str:
        from app.api.routers._common import taxonomy

        subs = taxonomy()["subsectors"]
        low = v.strip().lower()
        if low in subs:
            return low
        for sid, s in subs.items():
            if s["name"].lower() == low or s["name"].lower().startswith(low):
                return sid
        raise BqlError(f"unknown subsector {v!r}", pos)

    # ----------------------------------------------------------- build
    def build(self) -> Plan:
        q = self.q
        self.rewrites: dict[int, Node] = {}
        self.d_stream_keys: dict[str, str] = {}
        self.g_stream_keys: dict[str, str] = {}
        self.d_static: dict[tuple[str, str], str] = {}
        self.lift_exprs: dict[str, str] = {}
        self.options()
        self.grid_step_days = {"D": 1.4, "W": 7, "M": 30.44, "Q": 91.3}.get(self.freq or "D", 1.4)
        # universe (resolved now: the compiled query takes the ids as one list parameter)
        usql, uparams, unotes = self.universe(q.universe)
        self.warn.extend(unotes)
        ids = self.env.universe_ids(usql, [], uparams)
        if len(ids) > MAX_UNIVERSE:
            raise BqlError(f"universe of {len(ids):,} securities (max {MAX_UNIVERSE:,}): narrow for(...)")
        # analysis
        exprs: list[Node] = [f.expr for f in q.fields]
        if q.where is not None:
            exprs.append(q.where)
        for gf in q.fields:
            self.analyze(gf.expr)
        if q.where is not None:
            lw = self.analyze(q.where)
            if self.dtypes[id(q.where)] != "bool" and lw != "const":
                raise BqlError("where(...) needs a condition (comparison / and / or / not)", q.where.pos)
        for g in q.group:
            self.analyze(g)
        if q.group and not q.aggs:
            raise BqlError("group(...) needs .agg(fn): median, mean, sum, min, max, count, std")
        for a in q.aggs:
            if a not in AGGS:
                raise BqlError(f"unknown aggregation {a!r}; one of {', '.join(AGGS)}")
        if q.aggs and not q.group:
            raise BqlError(".agg() needs group(...)")
        names = self.column_names()
        sort_nodes: list[tuple[Node | str, bool]] = []
        for s in q.sort:
            ref = self._sort_ref(s.expr, names)
            if ref is not None:
                sort_nodes.append((ref, s.desc))
            else:
                if q.group:
                    raise BqlError(f"sort({to_text(s.expr)}): grouped queries sort by an output column "
                                   f"({', '.join(names)})", s.expr.pos)
                self.analyze(s.expr)
                sort_nodes.append((s.expr, s.desc))
        lookback = max([self.lookback.get(id(e), 0.0) for e in [f.expr for f in q.fields] +
                        ([q.where] if q.where is not None else []) + [s for s, _ in sort_nodes if isinstance(s, Node)]]
                       + [0.0])
        # ---- grid layer: where -> filter -> fields
        if q.where is not None:
            wsql = self.comp(q.where, "grid")
            wcol = self._g_col(wsql, deps=self._deps(wsql))
        else:
            wcol = None
        fcols: list[str] = []
        for gf in q.fields:
            fcols.append(self._g_col_of(gf.expr))
        gcols = [self._g_col_of(g) for g in q.group]
        scols: list[tuple[str, bool]] = []
        for s, desc in sort_nodes:
            if isinstance(s, str):
                scols.append((s, desc))
            else:
                scols.append((self._g_col_of(s), desc))
        self._g_flush()
        self._d_flush()
        needs_daily = bool(self.lifted)
        # ---- estimates / limits
        start = self.grid[0]
        lo = start - timedelta(days=int(lookback * 1.05) + 12)
        n_u = len(ids)
        est_cells = n_u * len(self.grid)
        est_daily = int(n_u * ((self.as_of - lo).days + 1) * 252 / 365) if needs_daily else 0
        if est_cells > MAX_CELLS:
            raise BqlError(f"query too large: {n_u:,} securities x {len(self.grid):,} dates = {est_cells:,} cells "
                           f"(max {MAX_CELLS:,}); narrow the universe or use a lower frequency / shorter range")
        if est_daily > MAX_DAILY_ROWS:
            raise BqlError(f"query too large: ~{est_daily:,} daily bars ({n_u:,} securities x "
                           f"{(self.as_of - lo).days:,} days incl. look-back; max {MAX_DAILY_ROWS:,})")
        if needs_daily and lo < self.env.snap_as_of - timedelta(days=int(365.25 * 15) - 5):
            self.warn.append("before the 15-year daily window the history uses weekly bars (prices_w): windows "
                             "counted in observations span weeks there")
        sql = self.assemble(ids, needs_daily, lo, fcols, gcols, scols, wcol)
        cols = self.output_columns(names, fcols)
        return Plan(self.mode, bool(q.group), self.as_of, self.grid, self.freq if self.mode == "timeseries" else None,
                    self.fill, self.ccy, sql, self.p.d, cols, ids, usql, uparams, self.warn, self.used_fields,
                    lo if needs_daily else None, needs_daily, self.ccy if self.ccy and self.ccy != "USD" else None,
                    est_daily, est_cells, self.max_rows)

    def _g_col_of(self, e: Node) -> str:
        sql = self.comp(e, "grid")
        if re.fullmatch(r"c\d+", sql):
            return sql
        return self._g_col(sql, deps=self._deps(sql))

    def _sort_ref(self, e: Node, names: list[str]) -> str | None:
        """sort key referring to an output column: alias / canonical text / bare function or field name."""
        t = to_text(e)
        if t in names:
            return f"__out{names.index(t)}"
        if self.q.group and isinstance(e, Name) and e.name.lower() in ("n", "count"):
            return "__n"  # the group size column (count(*) AS _n)
        if isinstance(e, Name):
            heads = [i for i, nm in enumerate(names) if nm.split("(")[0] == e.name or nm == e.name]
            if len(heads) == 1:
                return f"__out{heads[0]}"
            if isinstance(e, Name) and F.resolve(e.name) is not None:
                return None
            if len(heads) > 1:
                raise BqlError(f"sort({e.name}) is ambiguous: {', '.join(names[i] for i in heads)}", e.pos)
            raise BqlError(f"sort({e.name}): not an output column or a field", e.pos)
        return None

    def column_names(self) -> list[str]:
        out: list[str] = []
        q = self.q
        if q.group:
            for g in q.group:
                out.append(to_text(g))
            for gf in q.fields:
                base = gf.alias or to_text(gf.expr)
                for a in q.aggs:
                    out.append(base if len(q.aggs) == 1 else f"{a}({base})")
        else:
            for gf in q.fields:
                out.append(gf.alias or to_text(gf.expr))
        seen: dict[str, int] = {}
        res = []
        for nm in out:
            if nm in seen:
                seen[nm] += 1
                nm = f"{nm}_{seen[nm]}"
            else:
                seen[nm] = 1
            res.append(nm)
        return res

    def output_columns(self, names: list[str], fcols: list[str]) -> list[Column]:
        q = self.q
        cols: list[Column] = []
        if not q.group:
            cols.append(Column("security_id", None, "text", "key"))
            cols.append(Column("ticker", None, "text", "key"))
            cols.append(Column("name", None, "text", "key"))
        if self.mode == "timeseries":
            cols.append(Column("date", "date", "date", "date"))
        if q.group:
            for g, nm in zip(q.group, names, strict=False):
                cols.append(Column(nm, self.units.get(id(g)), self.dtypes.get(id(g), "text"), "group"))
            cols.append(Column("n", "count", "number", "value", description="securities in the group"))
            i = len(q.group)
            for gf in q.fields:
                for a in q.aggs:
                    unit = "count" if a == "count" else self.units.get(id(gf.expr))
                    cols.append(self._meta_col(names[i], gf.expr, unit))
                    i += 1
        else:
            for gf, nm in zip(q.fields, names, strict=True):
                cols.append(self._meta_col(nm, gf.expr, self.units.get(id(gf.expr))))
        return cols

    def _meta_col(self, nm: str, e: Node, unit: str | None) -> Column:
        e = self.rewrites.get(id(e), e)
        f = F.resolve(e.name) if isinstance(e, Name) else None
        ccy = None
        if unit in F.CONVERTIBLE_UNITS:
            ccy = self.ccy or ("USD" if unit == "currency_usd" else "local")
        return Column(nm, unit, self.dtypes.get(id(e), "number"), "value", f.mnemonic if f else None,
                      f.description if f else None, ccy, bool(f.model_based) if f else self._model_based(e))

    def _model_based(self, e: Node) -> bool:
        from .grammar import walk

        for x in walk(e):
            if isinstance(x, Name):
                f = F.resolve(x.name)
                if f is not None and f.model_based:
                    return True
        return False

    # ----------------------------------------------------------- SQL assembly
    def assemble(self, ids: list[str], needs_daily: bool, lo: date, fcols: list[str], gcols: list[str],
                 scols: list[tuple[str, bool]], wcol: str | None) -> str:
        P = self.p
        ctes: list[tuple[str, str]] = []
        ctes.append(("u", f"SELECT s.security_id, s.issuer_id FROM sec s WHERE s.security_id IN "
                          f"(SELECT unnest({P.add(ids, 'VARCHAR[]')}))"))
        ctes.append(("g", f"SELECT unnest({P.add(self.grid, 'DATE[]')}) AS d"))
        ctes.append(("b", "SELECT u.security_id, u.issuer_id, g.d, lag(g.d) OVER (PARTITION BY u.security_id "
                          "ORDER BY g.d) AS pd FROM u CROSS JOIN g"))
        ctes.extend(self.ctes_streams)
        fx_target = self.ccy and self.ccy != "USD"
        if fx_target:
            ctes.append(("tfx", "SELECT CAST(date AS DATE) AS fd, usd_per_unit AS fx FROM _bql_fx"))
        if needs_daily:
            bar_cols = ["security_id", "date", "close", "close_usd"] + [c for c in ("adj_close", "open", "high", "low",
                                                                                   "volume")]
            sel = ", ".join(_q(c) for c in bar_cols)
            lo_p, hi_p = P.add(lo, "DATE"), "$asof"
            px = (f"SELECT {sel} FROM prices WHERE security_id IN (SELECT security_id FROM u) "
                  f"AND date BETWEEN {lo_p} AND {hi_p}")
            if lo < self.env.snap_as_of - timedelta(days=int(365.25 * 15) - 5):
                wsel = ("security_id, date, close, CAST(NULL AS DOUBLE) AS close_usd, adj_close, open, high, low, volume")
                px += (f" UNION ALL SELECT {wsel} FROM prices_w w WHERE w.security_id IN (SELECT security_id FROM u) "
                       f"AND w.date BETWEEN {lo_p} AND {hi_p} AND w.date < coalesce((SELECT min(p2.date) FROM prices p2 "
                       f"WHERE p2.security_id = w.security_id), DATE '2100-01-01')")
            ctes.append(("px", px))
            static_sel = "".join(f", s.{_q(col)} AS {c}" for (_, col), c in self.d_static.items())
            cur = "dp"
            ctes.append((cur, f"SELECT px.*, u.issuer_id{static_sel} FROM px JOIN u USING (security_id)"
                              + (" JOIN sec s USING (security_id)" if self.d_static else "")))
            if fx_target:
                nxt = "dfx"
                ctes.append((nxt, f"SELECT {cur}.*, tfx.fx AS _tfx FROM {cur} ASOF LEFT JOIN tfx ON tfx.fd <= {cur}.date"))
                cur = nxt
            for sid, bc in self.d_bench.items():
                nxt = f"dbm_{bc}"
                ctes.append((nxt, f"SELECT {cur}.*, bmx.adj_close AS {bc} FROM {cur} LEFT JOIN (SELECT date, adj_close "
                                  f"FROM prices WHERE security_id = {P.add(sid, 'VARCHAR')} AND date BETWEEN {lo_p} AND "
                                  f"{hi_p}) bmx ON bmx.date = {cur}.date"))
                cur = nxt
            for sname, vcol in self.d_streams.items():
                key = self.d_stream_keys[sname]
                nxt = f"d_{sname}"
                ctes.append((nxt, f"SELECT {cur}.*, {sname}.v AS {vcol} FROM {cur} ASOF LEFT JOIN {sname} "
                                  f"ON {sname}.k = {cur}.{key} AND {sname}.d <= {cur}.date"))
                cur = nxt
            pxsel = ", ".join(f"{e} AS {c}" for e, c in self.px_cols.items())
            ctes.append(("d0", f"SELECT {cur}.*" + (f", {pxsel}" if pxsel else "") + f" FROM {cur}"))
            ctes.extend(self.d_ctes)
            dlast = self.d_cur
        # grid base
        cur = "gb"
        sfx = ", s.fx_to_usd AS _sfx" if self.ccy else ""
        ctes.append((cur, f"SELECT b.*{sfx} FROM b JOIN sec s USING (security_id)"))
        if fx_target:
            if self.mode == "cross_section" and not self.pit:
                ctes.append(("gfx", f"SELECT {cur}.*, (SELECT usd_per_unit FROM fx_latest WHERE currency = "
                                    f"{P.add(self.ccy, 'VARCHAR')}) AS _tfx FROM {cur}"))
            else:
                ctes.append(("gfx", f"SELECT {cur}.*, tfx.fx AS _tfx FROM {cur} ASOF LEFT JOIN tfx ON tfx.fd <= {cur}.d"))
            cur = "gfx"
        if needs_daily:
            def dl_expr(c: str) -> str:
                e = self.lift_exprs.get(c)
                if e is None:
                    return f"dl.{c}"
                return re.sub(r"(?<![\w$.])((?:c|b|t|bm)\d+|v_s\d+|_tfx)\b", r"dl.\1", e)

            lift = ", ".join(f"CASE WHEN dl.date >= {cur}.d - INTERVAL {PX_STALE_DAYS:d} DAY"
                             + (f" AND (dl.date > {cur}.pd OR {cur}.pd IS NULL)" if self.fill == "none" else "")
                             + f" THEN {dl_expr(c)} END AS {c}" for c in self.lifted)
            ctes.append(("gd", f"SELECT {cur}.*, {lift} FROM {cur} ASOF LEFT JOIN {dlast} dl "
                               f"ON dl.security_id = {cur}.security_id AND dl.date <= {cur}.d"))
            cur = "gd"
        for sname, wcol_ in self.g_streams.items():
            key = self.g_stream_keys[sname]
            nxt = f"g_{sname}"
            cond = f" AND ({sname}.d > {cur}.pd OR {cur}.pd IS NULL)" if self.fill == "none" else ""
            ctes.append((nxt, f"SELECT {cur}.*, CASE WHEN {sname}.d IS NOT NULL{cond} THEN {sname}.v END AS {wcol_} "
                              f"FROM {cur} ASOF LEFT JOIN {sname} ON {sname}.k = {cur}.{key} AND {sname}.d <= {cur}.d"))
            cur = nxt
        # snapshot joins
        joins: dict[str, tuple[str, str]] = {}
        sel = []
        for (table, colexpr), c in self.g_snap.items():
            if table not in joins:
                alias = f"j{len(joins) + 1}"
                f = next((x for x in self.fields.values() if x.table == table), None)
                key = f.key if f else "security_id"
                if table == "sec":
                    src = "sec"
                    key = "security_id"
                elif table in F.VIRTUAL_TABLES:
                    src = F.VIRTUAL_TABLES[table]
                else:
                    if self.env.snapshot_tables is not None and table not in self.env.snapshot_tables:
                        raise BqlError(f"table {table} is not in this snapshot")
                    src = _q(table)
                joins[table] = (alias, f"LEFT JOIN {src} {alias} ON {alias}.{_q(key)} = {cur}.{_q(key)}")
            alias = joins[table][0]
            if "{t}" in colexpr:
                sel.append(f"{colexpr.replace('{t}', alias)} AS {c}")
            else:
                sel.append(f"{alias}.{_q(colexpr)} AS {c}")
        ctes.append(("x0", f"SELECT {cur}.*" + (", " + ", ".join(sel) if sel else "") + f" FROM {cur} "
                     + " ".join(j for _, j in joins.values())))
        # grid layers: where columns were added first; filter layer inserted after the where column
        layers = list(self.g_ctes)
        if wcol is not None:
            # find the layer that defines wcol and filter right after it (later layers read from the filter)
            idx = next(i for i, (_, s) in enumerate(layers) if f" AS {wcol}" in s)
            fname = "xf"
            filt = (fname, f"SELECT * FROM {layers[idx][0]} WHERE coalesce({wcol}, FALSE)")
            rest = [(nm, s.replace(f"FROM {layers[idx][0]}", f"FROM {fname}", 1) if i == idx + 1 else s)
                    for i, (nm, s) in enumerate(layers)]
            layers = rest[:idx + 1] + [filt] + rest[idx + 1:]
            last = layers[-1][0]
        else:
            last = layers[-1][0] if layers else "x0"
        ctes.extend(layers)
        final = last
        # output
        names = self.column_names()
        out_alias = {f"__out{i}": f"o{i}" for i in range(len(names))}
        lim = self.q.limit
        if self.q.group:
            gsel = ", ".join(f"{c} AS o{i}" for i, c in enumerate(gcols))
            asel = []
            i = len(gcols)
            for c in fcols:
                for a in self.q.aggs:
                    asel.append(f"{AGGS[a]}({c}) AS o{i}")
                    i += 1
            dsel = "d AS _date, " if self.mode == "timeseries" else ""
            gby = ", ".join((["d"] if self.mode == "timeseries" else []) + [f"o{i}" for i in range(len(gcols))])
            order = []
            if self.mode == "timeseries":
                order.append("_date")
            for ref, desc in scols:
                key = "_n" if ref == "__n" else out_alias.get(ref, ref)
                order.append(f"{key} {'DESC' if desc else 'ASC'} NULLS LAST")
            order += [f"o{i}" for i in range(len(gcols))]
            body = (f"SELECT {dsel}{gsel}, count(*) AS _n, {', '.join(asel)} FROM {final} GROUP BY {gby} "
                    f"ORDER BY {', '.join(order)}" + (f" LIMIT {int(lim):d}" if lim else ""))
        else:
            vsel = "".join(f", {c} AS o{i}" for i, c in enumerate(fcols))
            ssel = "".join(f", {c} AS _s{i}" for i, (c, _) in enumerate(scols) if not c.startswith("__out"))
            base = (f"SELECT f.security_id, s.ticker AS _ticker, s.name AS _name, f.d AS _date{vsel}{ssel} "
                    f"FROM {final} f JOIN sec s USING (security_id)")
            ctes.append(("res", base))
            order_keys = []
            for i, (c, desc) in enumerate(scols):
                k = out_alias[c] if c.startswith("__out") else f"_s{i}"
                order_keys.append(f"{k} {'DESC' if desc else 'ASC'} NULLS LAST")
            if self.mode == "cross_section":
                ob = ", ".join(order_keys + ["_ticker", "security_id"])
                body = f"SELECT * FROM res ORDER BY {ob} LIMIT {int(min(lim or self.max_rows, self.max_rows)) + 1:d}"
            else:
                if order_keys or lim:
                    ob = ", ".join(order_keys + ["_ticker", "security_id"])
                    n_sec = int(lim or len(ids))
                    ctes.append(("top", f"SELECT security_id, row_number() OVER (ORDER BY {ob}) AS _rk FROM res "
                                        f"WHERE _date = (SELECT max(_date) FROM res)"))
                    body = (f"SELECT res.* FROM res JOIN top USING (security_id) WHERE top._rk <= {n_sec:d} "
                            f"ORDER BY top._rk, res._date LIMIT {int(self.max_rows) + 1:d}")
                else:
                    body = f"SELECT * FROM res ORDER BY _ticker, security_id, _date LIMIT {int(self.max_rows) + 1:d}"
        with_sql = ",\n".join(f"{nm} AS ({s})" for nm, s in ctes)
        sql = f"WITH {with_sql}\n{body}"
        if "$asof" in sql:
            self.p.d["asof"] = self.as_of
        return sql


def compile_query(q: Query, env: Env) -> Plan:
    return Compiler(q, env).build()
