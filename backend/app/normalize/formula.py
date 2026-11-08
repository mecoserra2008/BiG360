"""AST-whitelisted formula evaluator for canonical_line_items.yaml `derive` rules and sector_kpis.yaml formulas.
Owner: ref-identity. Used by normalize.fundamentals and by analytics (ratios / sector_kpis / scoring).

Public, stable API
------------------
    evaluate(expr, row_or_frame, *, name="value", panel_keys=PANEL_KEYS, date_col="period_end", history=None)
        * dict/Mapping row      -> float | None        (scalar; `history` = optional prior rows for lag/avg)
        * pl.DataFrame          -> pl.Series (len == frame height, same row order, Float64, named `name`)
        * pl.LazyFrame          -> pl.Series (collected)
    compile_expr(expr) -> pl.Expr        lazy-friendly expression for formulas WITHOUT panel functions
    dependencies(expr) -> set[str]       identifiers referenced (items / metrics), for DAG ordering
    uses_panel_functions(expr) -> bool
    validate_expr(expr) -> None          raises FormulaError on syntax outside the whitelist

Language (Python expression syntax)
-----------------------------------
    names           canonical item ids, metric ids, or any frame column (missing column => null)
    literals        int / float
    operators       + - * / ** unary-, comparisons (< <= > >= == !=) -> 1.0/0.0, and/or/not, `a if c else b`
    null semantics  any null input -> null, unless wrapped in nz()/coalesce(). Division by 0 -> null.
                    Non-finite results (inf/NaN) -> null.
    functions
        coalesce(a, b, ...)   first non-null          nz(x)          coalesce(x, 0)
        abs(x)                                          min(a, b, ...) / max(a, b, ...)  (null if any input null)
        div(a, b)             a/b, null if b == 0       safe_div(a, b) a/b, null if b <= 0
        clip(x, lo, hi[, default])  clip to [lo,hi]; default (or null) when x is null
        -- panel functions (need columns period_end + panel keys; one row per (keys, period_end)) --
        lag(x, n)             x at period_end - n years (±20 days) in the same panel (issuer, basis, period_kind).
                              For TTM/Q rows this is "4n quarters back".
        avg(x)                mean(x, lag(x,1)); falls back to x if prior missing
        growth_yoy(x)         x / lag(x,1) - 1, null if lag <= 0
        cagr(x, n)            (x / lag(x,n))**(1/n) - 1, null if either <= 0
        stdev_fy(x, n)        sample stdev of x over the last n yearly observations (lags 0..n-1), min n-1 obs
        mean_fy(x, n)         mean over the same window, min n-1 obs
        mcap()                market cap USD from a `mcap` / `market_cap_usd` column if the frame has one, else null

PANEL_KEYS default = ("issuer_id", "currency_basis", "period_kind"); keys absent from the frame are ignored.
Callers must pass ONE version per (keys, period_end) (apply app.normalize.pit.asof first).
"""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

import polars as pl

__all__ = [
    "FormulaError", "evaluate", "compile_expr", "dependencies", "uses_panel_functions", "validate_expr",
    "PANEL_KEYS", "SCALAR_FUNCS", "PANEL_FUNCS",
]

PANEL_KEYS: tuple[str, ...] = ("issuer_id", "currency_basis", "period_kind")
SCALAR_FUNCS = frozenset({"coalesce", "nz", "abs", "min", "max", "div", "safe_div", "clip", "mcap"})
PANEL_FUNCS = frozenset({"lag", "avg", "growth_yoy", "cagr", "stdev_fy", "mean_fy"})
_ALL_FUNCS = SCALAR_FUNCS | PANEL_FUNCS
_LAG_TOLERANCE = "20d"

_BIN = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_CMP = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


class FormulaError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ parsing


@lru_cache(maxsize=4096)
def _parse(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise FormulaError("empty formula")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"syntax error in {expr!r}: {e.msg}") from e
    _check(tree.body, expr)
    return tree


def _check(node: ast.AST, expr: str) -> None:
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BIN):
            raise FormulaError(f"operator {type(node.op).__name__} not allowed in {expr!r}")
        _check(node.left, expr), _check(node.right, expr)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            raise FormulaError(f"unary {type(node.op).__name__} not allowed in {expr!r}")
        _check(node.operand, expr)
    elif isinstance(node, ast.Compare):
        if not all(isinstance(o, _CMP) for o in node.ops):
            raise FormulaError(f"comparison not allowed in {expr!r}")
        _check(node.left, expr)
        for c in node.comparators:
            _check(c, expr)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            _check(v, expr)
    elif isinstance(node, ast.IfExp):
        _check(node.test, expr), _check(node.body, expr), _check(node.orelse, expr)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALL_FUNCS:
            fn = getattr(node.func, "id", type(node.func).__name__)
            raise FormulaError(f"function {fn!r} not allowed in {expr!r}")
        if node.keywords:
            raise FormulaError(f"keyword arguments not allowed in {expr!r}")
        for a in node.args:
            _check(a, expr)
    elif isinstance(node, ast.Name):
        if node.id.startswith("__"):
            raise FormulaError(f"name {node.id!r} not allowed")
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError(f"only numeric literals allowed in {expr!r}")
    else:
        raise FormulaError(f"syntax {type(node).__name__} not allowed in {expr!r}")


def validate_expr(expr: str) -> None:
    """Raise FormulaError if `expr` is outside the whitelist."""
    _parse(expr)


def dependencies(expr: str) -> set[str]:
    """Identifiers referenced by the formula (canonical items / metric ids), excluding function names."""
    tree = _parse(expr)
    func_nodes = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in func_nodes}


def uses_panel_functions(expr: str) -> bool:
    return any(isinstance(n, ast.Call) and n.func.id in PANEL_FUNCS for n in ast.walk(_parse(expr)))  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------------ compiler

_NULL = pl.lit(None, dtype=pl.Float64)


def _finite(e: pl.Expr) -> pl.Expr:
    return pl.when(e.is_infinite() | e.is_nan()).then(_NULL).otherwise(e)


def _any_null(args: Sequence[pl.Expr]) -> pl.Expr:
    m = args[0].is_null()
    for a in args[1:]:
        m = m | a.is_null()
    return m


def _int_arg(node: ast.AST, fn: str) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_int_arg(node.operand, fn)
    raise FormulaError(f"{fn}(): window/lag argument must be an integer literal")


class _Compiler:
    def __init__(self, frame: pl.DataFrame | None, panel_keys: Sequence[str], date_col: str):
        self.frame = frame
        self.columns: set[str] = set(frame.columns) if frame is not None else set()
        self.panel_keys = [k for k in panel_keys if frame is not None and k in frame.columns]
        self.date_col = date_col
        self._n = 0
        self._lag_cache: dict[tuple[str, int], str] = {}
        self._mat_cache: dict[str, str] = {}

    # -- helpers -------------------------------------------------------------------------------------
    def _tmp(self) -> str:
        self._n += 1
        return f"__f{self._n}"

    def _materialize(self, node: ast.AST) -> str:
        key = ast.dump(node)
        if key in self._mat_cache:
            return self._mat_cache[key]
        if isinstance(node, ast.Name) and node.id in self.columns:
            col = node.id
            self._mat_cache[key] = col
            return col
        e = self.c(node)
        name = self._tmp()
        assert self.frame is not None
        self.frame = self.frame.with_columns(e.alias(name))
        self.columns.add(name)
        self._mat_cache[key] = name
        return name

    def _lag(self, node: ast.AST, n: int) -> pl.Expr:
        if self.frame is None:
            raise FormulaError("panel functions (lag/avg/growth_yoy/cagr/stdev_fy/mean_fy) need a frame; use evaluate()")
        if n == 0:
            return self.c(node)
        if self.date_col not in self.columns:
            return _NULL
        src = self._materialize(node)
        ck = (src, n)
        if ck in self._lag_cache:
            return pl.col(self._lag_cache[ck])
        out = self._tmp()
        keys = self.panel_keys
        f = self.frame.with_row_index("__row")
        left = f.select(["__row", *keys, pl.col(self.date_col).dt.offset_by(f"-{n}y").alias("__target")])
        right = (
            f.select([*keys, pl.col(self.date_col).alias("__target"), pl.col(src).cast(pl.Float64).alias(out)])
            .filter(pl.col("__target").is_not_null())
            .unique(subset=[*keys, "__target"], keep="last", maintain_order=True)
            .sort("__target")
        )
        left = left.filter(pl.col("__target").is_not_null()).sort("__target")
        j = left.join_asof(right, on="__target", by=keys or None, strategy="nearest", tolerance=_LAG_TOLERANCE,
                             check_sortedness=False)
        lagged = f.select("__row").join(j.select(["__row", out]), on="__row", how="left").sort("__row")
        self.frame = self.frame.with_columns(lagged.get_column(out))
        self.columns.add(out)
        self._lag_cache[ck] = out
        return pl.col(out)

    # -- node compiler --------------------------------------------------------------------------------
    def c(self, node: ast.AST) -> pl.Expr:  # noqa: C901 (dispatcher)
        if isinstance(node, ast.Constant):
            return pl.lit(float(node.value), dtype=pl.Float64)
        if isinstance(node, ast.Name):
            if self.frame is not None and node.id not in self.columns:
                return _NULL
            return pl.col(node.id).cast(pl.Float64, strict=False)
        if isinstance(node, ast.UnaryOp):
            v = self.c(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
            return pl.when(v.is_null()).then(_NULL).otherwise((v == 0).cast(pl.Float64))
        if isinstance(node, ast.BinOp):
            a, b = self.c(node.left), self.c(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                return pl.when(b == 0).then(_NULL).otherwise(a / b)
            return _finite(a.pow(b))
        if isinstance(node, ast.Compare):
            left = self.c(node.left)
            res: pl.Expr | None = None
            nulls = [left]
            for op, comp in zip(node.ops, node.comparators, strict=True):
                right = self.c(comp)
                nulls.append(right)
                r = {
                    ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right,
                    ast.GtE: left >= right, ast.Eq: left == right, ast.NotEq: left != right,
                }[type(op)]
                res = r if res is None else (res & r)
                left = right
            assert res is not None
            return pl.when(_any_null(nulls)).then(_NULL).otherwise(res.cast(pl.Float64))
        if isinstance(node, ast.BoolOp):
            vals = [self.c(v) for v in node.values]
            truth = [(v != 0) for v in vals]
            acc = truth[0]
            for t in truth[1:]:
                acc = (acc & t) if isinstance(node.op, ast.And) else (acc | t)
            return pl.when(_any_null(vals)).then(_NULL).otherwise(acc.cast(pl.Float64))
        if isinstance(node, ast.IfExp):
            t = self.c(node.test)
            return pl.when(t.is_null()).then(_NULL).when(t != 0).then(self.c(node.body)).otherwise(self.c(node.orelse))
        if isinstance(node, ast.Call):
            return self._call(node)
        raise FormulaError(f"unsupported node {type(node).__name__}")

    def _call(self, node: ast.Call) -> pl.Expr:
        fn = node.func.id  # type: ignore[union-attr]
        args = node.args
        na = len(args)

        def need(lo: int, hi: int | None = None) -> None:
            if na < lo or (hi is not None and na > hi):
                raise FormulaError(f"{fn}() takes {lo}{'' if hi == lo else '..' + str(hi) if hi else '+'} args, got {na}")

        if fn == "coalesce":
            need(1)
            return pl.coalesce([self.c(a) for a in args])
        if fn == "nz":
            need(1, 1)
            return self.c(args[0]).fill_null(0.0)
        if fn == "abs":
            need(1, 1)
            return self.c(args[0]).abs()
        if fn in ("min", "max"):
            need(1)
            vs = [self.c(a) for a in args]
            agg = pl.min_horizontal(vs) if fn == "min" else pl.max_horizontal(vs)
            return pl.when(_any_null(vs)).then(_NULL).otherwise(agg)
        if fn in ("div", "safe_div"):
            need(2, 2)
            a, b = self.c(args[0]), self.c(args[1])
            bad = (b == 0) if fn == "div" else (b <= 0)
            return pl.when(bad).then(_NULL).otherwise(a / b)
        if fn == "clip":
            need(3, 4)
            x, lo, hi = self.c(args[0]), self.c(args[1]), self.c(args[2])
            dflt = self.c(args[3]) if na == 4 else _NULL
            clipped = pl.when(x < lo).then(lo).when(x > hi).then(hi).otherwise(x)
            return pl.when(x.is_null()).then(dflt).otherwise(clipped)
        if fn == "mcap":
            need(0, 0)
            for cand in ("mcap", "market_cap_usd"):
                if cand in self.columns:
                    return pl.col(cand).cast(pl.Float64, strict=False)
            return _NULL
        # ---- panel functions
        if fn == "lag":
            need(2, 2)
            return self._lag(args[0], _int_arg(args[1], fn))
        if fn == "avg":
            need(1, 1)
            x, p = self.c(args[0]), self._lag(args[0], 1)
            return pl.when(p.is_null()).then(x).otherwise((x + p) / 2)
        if fn == "growth_yoy":
            need(1, 1)
            x, p = self.c(args[0]), self._lag(args[0], 1)
            return pl.when(p <= 0).then(_NULL).otherwise(x / p - 1)
        if fn == "cagr":
            need(2, 2)
            n = _int_arg(args[1], fn)
            if n <= 0:
                raise FormulaError("cagr(): n must be > 0")
            x, p = self.c(args[0]), self._lag(args[0], n)
            return pl.when((x <= 0) | (p <= 0)).then(_NULL).otherwise(_finite((x / p).pow(1.0 / n) - 1))
        if fn in ("stdev_fy", "mean_fy"):
            need(2, 2)
            n = _int_arg(args[1], fn)
            if n <= 0:
                raise FormulaError(f"{fn}(): n must be > 0")
            vals = [self._lag(args[0], k) for k in range(n)]
            lst = pl.concat_list(vals).list.drop_nulls()
            cnt = lst.list.len()
            agg = lst.list.std(ddof=1) if fn == "stdev_fy" else lst.list.mean()
            return pl.when(cnt < max(n - 1, 1)).then(_NULL).otherwise(agg)
        raise FormulaError(f"function {fn!r} not implemented")


# ------------------------------------------------------------------------------------------------ public


def compile_expr(expr: str) -> pl.Expr:
    """Polars expression for a formula without panel functions (usable in LazyFrame.with_columns).
    Referenced columns must exist in the frame it is applied to (missing columns raise in Polars)."""
    tree = _parse(expr)
    if uses_panel_functions(expr):
        raise FormulaError(f"{expr!r} uses panel functions; use evaluate(expr, frame)")
    return _finite(_Compiler(None, (), "period_end").c(tree.body))


def evaluate(expr: str, row_or_frame: Mapping[str, Any] | pl.DataFrame | pl.LazyFrame, *, name: str = "value",
             panel_keys: Sequence[str] = PANEL_KEYS, date_col: str = "period_end",
             history: Sequence[Mapping[str, Any]] | None = None) -> float | None | pl.Series:
    """Evaluate a formula over a row (-> float|None) or vectorized over a Polars frame (-> pl.Series).

    Frame mode keeps row order and length. Missing columns evaluate to null. See module docstring for the language.
    Row mode accepts `history` (prior rows, any order) so lag/avg/growth functions work on a single row.
    """
    tree = _parse(expr)
    if isinstance(row_or_frame, pl.LazyFrame):
        row_or_frame = row_or_frame.collect()
    if isinstance(row_or_frame, pl.DataFrame):
        frame = row_or_frame
        if frame.height == 0:
            return pl.Series(name, [], dtype=pl.Float64)
        comp = _Compiler(frame, panel_keys, date_col)
        e = _finite(comp.c(tree.body))
        assert comp.frame is not None
        return comp.frame.select(e.cast(pl.Float64).alias(name)).get_column(name)
    if isinstance(row_or_frame, Mapping):
        rows = [dict(r) for r in (history or [])] + [dict(row_or_frame)]
        frame = _rows_to_frame(rows)
        comp = _Compiler(frame, panel_keys, date_col)
        e = _finite(comp.c(tree.body))
        assert comp.frame is not None
        v = comp.frame.select(e.cast(pl.Float64).alias(name)).get_column(name)[-1]
        return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)
    raise TypeError(f"evaluate() expects a Mapping, DataFrame or LazyFrame, got {type(row_or_frame)!r}")


def _rows_to_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    data: dict[str, list[Any]] = {k: [r.get(k) for r in rows] for k in keys}
    cols = []
    for k, vals in data.items():
        non_null = [v for v in vals if v is not None]
        if non_null and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
            cols.append(pl.Series(k, [None if v is None else float(v) for v in vals], dtype=pl.Float64))
        else:
            try:
                cols.append(pl.Series(k, vals, strict=False))
            except Exception:
                cols.append(pl.Series(k, [None if v is None else str(v) for v in vals], dtype=pl.Utf8))
    return pl.DataFrame(cols) if cols else pl.DataFrame({"__dummy": [None] * len(rows)})
