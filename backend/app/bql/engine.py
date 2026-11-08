"""BQL-lite execution: compile -> run on the read-only serving snapshot with limits, timeout and a result cache;
exports (CSV / XLSX / JSON) and the PIT panel helper used by Model Studio and the Backtest Lab.

    from app.bql.engine import run, explain
    res = run("get(px_last, pe_fwd) for(['AAPL US']) ", db)       # db: app.api.deps.Db

Limits: universe <= 25,000 securities, universe x dates <= 2 M cells, ~40 M daily bars incl. look-back, output rows
<= max_rows (default 5,000, max 100,000), a query timeout (BIG360_BQL_TIMEOUT_S, default 30 s; the DuckDB statement
is interrupted). Cache: LRU keyed by (snapshot run id, normalised AST hash, resolved universe hash)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from . import compiler as C
from . import fields as F
from . import tickers as T
from .grammar import BqlError, Query, ast_hash, parse, parse_expr, query_text, to_dict, to_text

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0
CACHE_ITEMS = 128
CACHE_MAX_ROWS = 50_000


class BqlTimeout(BqlError):
    pass


def timeout_s() -> float:
    try:
        v = float(os.environ.get("BIG360_BQL_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        v = DEFAULT_TIMEOUT_S
    return max(0.5, min(v, MAX_TIMEOUT_S))


# ------------------------------------------------------------------------------------------------ cache


class _Cache:
    def __init__(self, n: int = CACHE_ITEMS):
        self.n = n
        self.d: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, k: str) -> dict[str, Any] | None:
        with self.lock:
            v = self.d.get(k)
            if v is None:
                self.misses += 1
                return None
            self.d.move_to_end(k)
            self.hits += 1
            return v

    def put(self, k: str, v: dict[str, Any]) -> None:
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            while len(self.d) > self.n:
                self.d.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.d.clear()


cache = _Cache()


# ------------------------------------------------------------------------------------------------ environment


def _lake_select(table: str) -> str:
    from app.core.schema import get_contract
    from app.core.storage import view_sql

    c = get_contract(table)
    sql = view_sql(c)
    prefix = f'CREATE OR REPLACE VIEW "{c.table}" AS '
    if not sql.startswith(prefix):
        raise BqlError(f"internal: unexpected lake view for {table}")
    return sql[len(prefix):]


def _named_universe_fn(cur: Any, depth: int) -> Any:
    def fn(ref: str) -> tuple[list[str] | None, str | None, list[str]]:
        kind, _, name = ref.partition(":")
        kind = kind.strip().lower()
        name = name.strip()
        notes: list[str] = []
        if kind == "watch":
            from app.api import app_state

            try:
                return app_state.all_watch_security_ids(), None, notes
            except Exception:  # noqa: BLE001 - app_state locked / missing: the u_watch flag still applies
                return [], None, notes
        if kind in ("watchlist", "wl"):
            from app.api import app_state

            wls = app_state.list_watchlists()
            hit = next((w for w in wls if w["watchlist_id"] == name or w["name"].lower() == name.lower()), None)
            if hit is None:
                return None, f"unknown watchlist {name!r} (existing: {', '.join(w['name'] for w in wls) or 'none'})", notes
            return [r["security_id"] for r in app_state.watchlist_items(hit["watchlist_id"])], None, notes
        if kind == "screen":
            from . import store

            sv = store.find_by_name(name)
            if sv is None:
                return None, f"unknown saved screen {name!r} (save a query with kind=screen in the BQL console)", notes
            if depth >= 2:
                return None, f"screen {name!r}: screens nested more than 2 levels", notes
            try:
                q = parse(sv["query"])
                ids = universe_of(q, cur, depth + 1)
            except BqlError as e:
                return None, f"screen {name!r}: {e}", notes
            notes.append(f"members('screen:{name}') = the {len(ids)} securities of the saved query {sv['name']!r}")
            return ids, None, notes
        if kind in ("peers", "peer"):
            return _peers(cur, name, notes)
        return None, f"unknown universe reference {ref!r}", notes

    return fn


def _peers(cur: Any, name: str, notes: list[str]) -> tuple[list[str] | None, str | None, list[str]]:
    # 1) a saved Studio peer set (name or id)
    try:
        from app.modeling import store as ms

        for ps in ms.list_peer_sets():
            if ps["peer_set_id"] == name or str(ps.get("name", "")).lower() == name.lower():
                full = ms.get_peer_set(ps["peer_set_id"]) or {}
                ids = [m["security_id"] for m in full.get("members") or []]
                notes.append(f"members('peers:{name}') = Studio peer set {ps['name']!r} ({len(ids)} members)")
                return ids, None, notes
    except Exception:  # noqa: BLE001 - app_state unavailable
        pass
    # 2) a conformal peer group id
    try:
        cur.execute("SELECT DISTINCT security_id FROM cf_peer_groups WHERE group_id = ?", [name])
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            notes.append(f"members('peers:{name}') = conformal peer group {name} ({len(ids)} members)")
            return ids, None, notes
    except duckdb.Error:
        pass
    # 3) a security: same subsector and region, primary equities, top 30 by market cap
    hit = T.resolve_one(cur, name)
    if hit:
        cur.execute("""SELECT p.security_id FROM sec s JOIN sec p ON p.subsector_id = s.subsector_id AND p.region = s.region
                       WHERE s.security_id = ? AND coalesce(p.is_primary_security, TRUE) AND coalesce(p.is_active, TRUE)
                         AND coalesce(p.security_type, 'common') NOT IN ('fx', 'future', 'index', 'etf')
                       ORDER BY (p.security_id = s.security_id) DESC, p.market_cap_usd DESC NULLS LAST LIMIT 31""",
                    [hit["security_id"]])
        ids = [r[0] for r in cur.fetchall()]
        notes.append(f"members('peers:{name}') = {hit['ticker']} and its subsector x region peers (top {len(ids) - 1} by "
                     "market cap)")
        return ids, None, notes
    return None, f"unknown peer group {name!r} (a Studio peer set, a conformal peer group id or a ticker)", notes


def _snapshot_tables(cur: Any) -> set[str]:
    cur.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'")
    return {r[0] for r in cur.fetchall()}


def make_env(cur: Any, snap_as_of: date, max_rows: int = 5000, depth: int = 0) -> C.Env:
    def resolve(refs: list[str]) -> tuple[list[str], list[str]]:
        ids, bad, _ = T.resolve_many(cur, refs)
        return ids, bad

    def uids(sql: str, _: list[Any], params: dict[str, Any]) -> list[str]:
        cur.execute(f"SELECT DISTINCT security_id FROM ({sql}) WHERE security_id IS NOT NULL ORDER BY 1", params)
        return [r[0] for r in cur.fetchall()]

    return C.Env(snap_as_of, resolve, uids, _named_universe_fn(cur, depth), _lake_select, _snapshot_tables(cur),
                 max_rows)


def universe_of(q: Query, cur: Any, depth: int = 0) -> list[str]:
    """Securities of a query's result (members('screen:NAME')): the cross-section rows after where / sort / limit."""
    snap_as_of = _snap_as_of(cur)
    env = make_env(cur, snap_as_of, 25_000, depth)
    plan = C.compile_query(Query(q.fields, q.universe, tuple((k, v) for k, v in q.with_ if k != "dates"), q.where,
                                 (), (), q.sort, q.limit), env)
    rows = _execute(cur, plan, timeout_s())[: plan_rows_cap(plan, q.limit)]  # (the query fetches one extra row)
    return list(dict.fromkeys(r[0] for r in rows))


def _snap_as_of(cur: Any) -> date:
    cur.execute("SELECT value FROM _meta WHERE key = 'as_of'")
    r = cur.fetchone()
    return date.fromisoformat(r[0]) if r else date.today()


# ------------------------------------------------------------------------------------------------ execution


def _register_fx(cur: Any, plan: C.Plan) -> str | None:
    """Target-currency FX history (USD per unit) as _bql_fx: lake fx_rates, else the snapshot's latest rate."""
    import pyarrow as pa

    ccy = plan.fx_target
    if not ccy:
        return None
    lo = (plan.daily_lo or plan.grid[0]) - timedelta(days=15)
    rows: list[tuple[date, float]] = []
    note = None
    if plan.mode == "timeseries" or plan.needs_daily or plan.as_of < _snap_as_of(cur):
        try:
            sel = _lake_select("fx_rates")
            con = duckdb.connect()
            try:
                rows = con.execute(f"SELECT CAST(date AS DATE), CAST(usd_per_unit AS DOUBLE) FROM ({sel}) "
                                   "WHERE currency = ? AND date BETWEEN ? AND ? AND usd_per_unit > 0 ORDER BY 1",
                                   [ccy, lo, plan.as_of]).fetchall()
            finally:
                con.close()
        except Exception:  # noqa: BLE001 - lake unavailable
            rows = []
    if not rows:
        cur.execute("SELECT usd_per_unit FROM fx_latest WHERE currency = ?", [ccy])
        r = cur.fetchone()
        if r is None or not r[0]:
            raise BqlError(f"no FX rate for currency {ccy}")
        rows = [(date(1900, 1, 1), float(r[0]))]
        if plan.mode == "timeseries" or plan.needs_daily:
            note = f"currency={ccy}: no FX history available, the latest rate is applied to every date"
    tbl = pa.table({"date": pa.array([d for d, _ in rows], pa.date32()),
                    "usd_per_unit": pa.array([v for _, v in rows], pa.float64())})
    cur.register("_bql_fx", tbl)
    return note


def _execute(cur: Any, plan: C.Plan, tmo: float) -> list[tuple[Any, ...]]:
    timer = threading.Timer(tmo, cur.interrupt)
    timer.daemon = True
    t0 = time.monotonic()
    timer.start()
    try:
        cur.execute(plan.sql, plan.params)
        return cur.fetchall()
    except duckdb.InterruptException as e:
        raise BqlTimeout(f"query timed out after {tmo:.0f} s: narrow the universe or the date range") from e
    except duckdb.Error as e:
        if time.monotonic() - t0 >= tmo - 0.05:
            raise BqlTimeout(f"query timed out after {tmo:.0f} s") from e
        raise BqlError(f"query failed: {str(e).splitlines()[0][:300]}") from e
    finally:
        timer.cancel()


def _clean(v: Any) -> Any:
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item"):  # numpy scalars / Decimal-like
        try:
            return _clean(v.item())
        except Exception:  # noqa: BLE001
            return str(v)
    return v


def _shape(plan: C.Plan, raw: list[tuple[Any, ...]], limit: int | None) -> tuple[list[dict[str, Any]], list[list[Any]], bool]:
    cols = [c.public() for c in plan.columns]
    n_out = len(cols)
    rows: list[list[Any]] = []
    cap = plan_rows_cap(plan, limit)
    truncated = len(raw) > cap and not (plan.mode == "cross_section" and not plan.grouped and limit and limit <= cap)
    raw = raw[:cap]
    if plan.grouped:
        for r in raw:
            rows.append([_clean(x) for x in r[:n_out]])
    else:
        nv = n_out - (4 if plan.mode == "timeseries" else 3)
        for r in raw:
            sid, tic, name, d = r[0], r[1], r[2], r[3]
            vals = [_clean(x) for x in r[4:4 + nv]]
            if plan.mode == "timeseries":
                rows.append([sid, tic, name, _clean(d)] + vals)
            else:
                rows.append([sid, tic, name] + vals)
    return cols, rows, truncated


def plan_rows_cap(plan: C.Plan, limit: int | None) -> int:
    mx = plan.max_rows
    if plan.mode == "cross_section" and not plan.grouped and limit:
        return min(limit, mx)
    return mx


def _compile(text: str, cur: Any, max_rows: int) -> tuple[Query, C.Plan, date]:
    q = parse(text)
    snap = _snap_as_of(cur)
    env = make_env(cur, snap, max_rows)
    plan = C.compile_query(q, env)
    return q, plan, snap


def run(text: str, db: Any, *, max_rows: int = 5000, use_cache: bool = True, tmo: float | None = None) -> dict[str, Any]:
    """Execute a BQL query against the serving snapshot of `db` (app.api.deps.Db). Raises BqlError."""
    t0 = time.perf_counter()
    snap = db.snap
    cur = snap.con.cursor()
    try:
        cur.execute("SET TimeZone = 'UTC'")
        q, plan, snap_as_of = _compile(text, cur, max_rows)
        mr = plan.max_rows
        key = hashlib.sha1(json.dumps([snap.run_id, ast_hash(q), sorted(plan.universe_ids)[:50_000], mr],
                                      default=str).encode()).hexdigest()
        if use_cache:
            hit = cache.get(key)
            if hit is not None:
                return {**hit, "cached": True, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        notes = []
        fxn = _register_fx(cur, plan)
        if fxn:
            notes.append(fxn)
        raw = _execute(cur, plan, tmo or timeout_s())
        cols, rows, truncated = _shape(plan, raw, q.limit)
        warnings = list(dict.fromkeys(plan.warnings + notes))
        if truncated:
            warnings.append(f"result truncated to {len(rows):,} rows (max_rows={mr:,}); add limit(...) or "
                            "with(max_rows=...)")
        out = {
            "query": query_text(q), "mode": plan.mode, "grouped": plan.grouped, "as_of": plan.as_of.isoformat(),
            "snapshot_as_of": snap_as_of.isoformat(), "serving_run_id": snap.run_id,
            "dates": {"start": plan.grid[0].isoformat(), "end": plan.grid[-1].isoformat(), "freq": plan.freq,
                      "n": len(plan.grid)} if plan.mode == "timeseries" else None,
            "currency": plan.currency, "fill": plan.fill, "universe_size": len(plan.universe_ids),
            "columns": cols, "rows": rows, "n_rows": len(rows), "truncated": truncated,
            "units": {c["name"]: c["unit"] for c in cols}, "warnings": warnings,
            "fields": plan.fields_used, "cached": False,
        }
        if len(rows) <= CACHE_MAX_ROWS:
            cache.put(key, out)
        out = {**out, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return out
    finally:
        try:
            cur.unregister("_bql_fx")
        except Exception:  # noqa: BLE001
            pass
        cur.close()


def explain(text: str, db: Any, max_rows: int = 5000) -> dict[str, Any]:
    """AST + compiled SQL shape (parameter names and types, never their values)."""
    cur = db.snap.con.cursor()
    try:
        q, plan, snap_as_of = _compile(text, cur, max_rows)
        return {
            "query": query_text(q), "ast": to_dict(q), "mode": plan.mode, "grouped": plan.grouped,
            "as_of": plan.as_of.isoformat(), "snapshot_as_of": snap_as_of.isoformat(),
            "dates": {"start": plan.grid[0].isoformat(), "end": plan.grid[-1].isoformat(), "freq": plan.freq,
                      "n": len(plan.grid)} if plan.mode == "timeseries" else None,
            "universe_sql": plan.universe_sql, "universe_params": [{"name": k, "type": type(v).__name__}
                                                                  for k, v in sorted(plan.universe_params.items())],
            "universe_size": len(plan.universe_ids), "sql": plan.sql, "params": plan.param_shape(),
            "columns": [c.public() for c in plan.columns], "fields": plan.fields_used,
            "estimates": {"cells": plan.est_cells, "daily_rows": plan.est_daily_rows,
                          "needs_daily_panel": plan.needs_daily,
                          "daily_from": plan.daily_lo.isoformat() if plan.daily_lo else None},
            "warnings": plan.warnings,
        }
    finally:
        cur.close()


# ------------------------------------------------------------------------------------------------ exports


def to_csv(res: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c["name"] for c in res["columns"]])
    for r in res["rows"]:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def to_json(res: dict[str, Any]) -> str:
    names = [c["name"] for c in res["columns"]]
    return json.dumps({"query": res["query"], "as_of": res["as_of"], "columns": res["columns"],
                       "rows": [dict(zip(names, r, strict=True)) for r in res["rows"]], "warnings": res["warnings"]},
                      default=str)


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def to_xlsx(res: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "BQL"
    ws.append([c["name"] for c in res["columns"]])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in res["rows"]:
        ws.append([v if not isinstance(v, (list, dict)) else json.dumps(v) for v in r])
    meta = wb.create_sheet("query")
    meta.append(["query", res["query"]])
    meta.append(["as_of", res["as_of"]])
    meta.append(["serving_run_id", res.get("serving_run_id")])
    for c in res["columns"]:
        meta.append(["column", c["name"], c.get("unit"), c.get("description") or ""])
    for wmsg in res["warnings"]:
        meta.append(["warning", wmsg])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------------------------------------ panel helper


def panel(expr: str, db_or_con: Any, ids: list[str], start: date | None = None, end: date | None = None,
          freq: str = "M", universe: str | None = None, tmo: float | None = None, max_rows: int = 2_000_000,
          dates: list[date] | None = None) -> tuple[list[tuple[str, date, Any]], list[str]]:
    """Evaluate one BQL expression point-in-time on a (security x date) panel: rows (security_id, date, value).

    The grid is `dates` (e.g. the rebalance dates of a backtest) or range(start, end) at `freq`. Every date D sees only
    data known at D (bars <= D, knowledge dates <= D); cross-sectional functions run over the panel's securities at each
    date. `universe` (BQL universe text) overrides `ids`."""
    from .grammar import Call, DateLit, GetField, ListLit, Name, Num, Str

    con = db_or_con.snap.con if hasattr(db_or_con, "snap") else db_or_con
    cur = con.cursor()
    try:
        cur.execute("SET TimeZone = 'UTC'")
        e = parse_expr(expr)
        uni = parse(f"get(1) for({universe})").universe if universe else ListLit(tuple(Str(i) for i in ids))
        snap = _snap_as_of(cur)
        opts: list[tuple[str, Any]] = [("max_rows", Num(float(max_rows)))]
        if dates is None:
            if start is None or end is None:
                raise BqlError("panel needs dates or start / end")
            end = min(end, snap)
            if start > end:
                raise BqlError(f"empty panel window {start} > {end}")
            opts += [("dates", Call("range", (DateLit(start), DateLit(end)))), ("freq", Name(freq))]
        q = Query((GetField(e, "v"),), uni, tuple(opts))
        env = make_env(cur, snap, max_rows)
        env.grid = list(dates) if dates is not None else None
        plan = C.compile_query(q, env)
        note = _register_fx(cur, plan)
        raw = _execute(cur, plan, tmo or max(timeout_s(), 90.0))
        out = [(r[0], r[3], _clean(r[4])) for r in raw if r[4] is not None]
        warns = list(plan.warnings) + ([note] if note else [])
        if len(raw) > max_rows:
            warns.append(f"panel truncated at {max_rows:,} rows")
        return out, warns
    finally:
        try:
            cur.unregister("_bql_fx")
        except Exception:  # noqa: BLE001
            pass
        cur.close()


def serving_connection() -> Any:
    """Read-only connection to the current serving snapshot (Studio / Backtest workers)."""
    from app.core.storage import current_serving_db

    p = current_serving_db()
    if p is None:
        raise BqlError("no serving snapshot published yet: BQL needs the snapshot")
    con = duckdb.connect(str(p), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


def field_text(f: F.FieldDef) -> str:
    return f"{f.mnemonic}: {f.description}"


__all__ = ["BqlError", "BqlTimeout", "cache", "explain", "panel", "run", "to_csv", "to_json", "to_xlsx", "to_text"]
