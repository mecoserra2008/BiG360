"""Screener (tag: screener): field registry, query with on-the-fly composite re-weighting, CSV export.

Composite with custom `factor_weights`: weighted mean of the latest factor scores over the factors that are non-null,
weights renormalized (scoring.yaml composite.custom_weights); computed vectorized in DuckDB over the `sec` table.
Grade (quintiles) and rank are computed over the requested universe before filters. With a stored profile the
scoring job's composite/grade/rank are used (falling back to the same on-the-fly formula when scores are missing).
"""

from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api import app_state
from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import (
    FACTORS,
    equity_only_sql,
    fnum,
    invalid,
    parse_regions,
    profile_weights,
    screener_fields,
    sec_ref,
    sec_ref_sql,
)
from app.api.serving import composite_sql

router = APIRouter(tags=["screener"], route_class=FastRoute)

Op = Literal["eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "not_in", "is_null", "not_null"]


class ScreenerFilter(BaseModel):
    field: str
    op: Op
    value: Any = None
    value2: float | None = None


class ScreenerQuery(BaseModel):
    universe_id: Literal["mvp_us", "mvp_global", "full", "watch"] = "full"
    regions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    sector_ids: list[str] = Field(default_factory=list)
    subsector_ids: list[str] = Field(default_factory=list)
    size_buckets: list[Literal["large", "mid", "small"]] = Field(default_factory=list)
    min_market_cap_usd: float | None = None
    filters: list[ScreenerFilter] = Field(default_factory=list)
    profile_id: str | None = None
    factor_weights: dict[str, float] | None = None
    columns: list[str] = Field(default_factory=list)
    sort_by: str = "composite"
    sort_dir: Literal["asc", "desc"] = "desc"
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0)


_COMPUTED = {"composite": "composite_x", "grade": "grade_x", "rank": "rank_x"}
ROW_COLS = ("close", "chg_1d", "chg_1m", "moat_class", "ml_rp_3M", "insider_signal", "dq_score", "ml_exp_3M",
            "score_scope", "ml_extrap_3M", "score_missing", "u_mvp_us", "u_mvp_global")


def _sql_of(fid: str) -> str:
    f = screener_fields().get(fid)
    if f is None:
        raise invalid(f"unknown screener field {fid!r} (see GET /api/v1/screener/fields)")
    return _COMPUTED.get(f["sql"], f["sql"])


def _filter_sql(f: ScreenerFilter, params: list[Any]) -> str:
    col = _sql_of(f.field)
    meta = screener_fields()[f.field]
    num = meta["type"] == "number"

    def val(v: Any) -> Any:
        if v is None:
            raise invalid(f"filter on {f.field!r} with op {f.op!r} needs a value")
        if num:
            x = fnum(v)
            if x is None:
                raise invalid(f"filter on {f.field!r} expects a number, got {v!r}")
            return x
        if meta["type"] == "boolean":
            return bool(v)
        return str(v)

    if f.op == "is_null":
        return f"{col} IS NULL"
    if f.op == "not_null":
        return f"{col} IS NOT NULL"
    if f.op in ("in", "not_in"):
        vals = f.value if isinstance(f.value, list) else [f.value]
        if not vals:
            return "FALSE" if f.op == "in" else "TRUE"
        params.extend(val(v) for v in vals)
        ph = ", ".join("?" for _ in vals)
        return f"{col} IN ({ph})" if f.op == "in" else f"({col} IS NULL OR {col} NOT IN ({ph}))"
    if f.op == "between":
        hi = f.value2
        if hi is None and isinstance(f.value, list) and len(f.value) == 2:
            lo, hi = f.value
        else:
            lo = f.value
        params.extend([val(lo), val(hi)])
        return f"{col} BETWEEN ? AND ?"
    sym = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[f.op]
    params.append(val(f.value))
    if f.op == "neq":
        return f"({col} IS NULL OR {col} <> ?)"
    return f"{col} {sym} ?"


def weights_for(q_weights: dict[str, float] | None, profile_id: str | None) -> tuple[dict[str, float], bool]:
    """(normalized weights, is_custom)."""
    if q_weights:
        bad = [k for k in q_weights if k not in FACTORS]
        if bad:
            raise invalid(f"unknown factor id(s) in factor_weights: {bad}")
        if any(v < 0 for v in q_weights.values()):
            raise invalid("factor_weights must be >= 0")
        tot = sum(v for v in q_weights.values())
        if tot > 0:
            return {k: round(v / tot, 6) for k, v in q_weights.items() if v > 0}, True
    w = profile_weights(profile_id)
    tot = sum(w.values()) or 1.0
    return {k: round(v / tot, 6) for k, v in w.items()}, False


def run_query(db: Db, q: ScreenerQuery, export: bool = False, ids: list[str] | None = None) -> dict[str, Any]:
    pid = q.profile_id or "balanced"
    weights, custom = weights_for(q.factor_weights, pid)
    if custom:
        comp = composite_sql(weights, "s.")
        grade = ("CASE WHEN composite_x IS NOT NULL THEN ['A','B','C','D','E'][ntile(5) OVER "
                 "(PARTITION BY composite_x IS NULL ORDER BY composite_x DESC)] END")
        rank = ("CASE WHEN composite_x IS NOT NULL THEN row_number() OVER "
                "(PARTITION BY composite_x IS NULL ORDER BY composite_x DESC) END")
    else:
        comp, grade, rank = f"s.c_{pid}", f"g_{pid}", f"rk_{pid}"
        if q.universe_id not in ("mvp_us", "mvp_global"):
            # full-universe extension (v1.44): outside the MVP universes rank among ALL scored securities
            rank = f"coalesce(rgf_{pid}, rk_{pid})"
    uparams: list[Any] = []
    ucond = ["coalesce(s.is_active, true)"]
    if ids is None:  # equity screener: FX / futures / indices / ETFs only when asked for by id
        ucond.append(equity_only_sql("s"))
    if ids is not None:
        ucond.append(f"s.security_id IN ({', '.join('?' for _ in ids) or 'NULL'})")
        uparams.extend(ids)
    elif q.universe_id == "mvp_us":
        ucond.append("s.u_mvp_us")
    elif q.universe_id == "mvp_global":
        ucond.append("s.u_mvp_global")
    elif q.universe_id == "watch":
        wl = app_state.all_watch_security_ids()
        ucond.append(f"(s.u_watch OR s.security_id IN ({', '.join('?' for _ in wl) or 'NULL'}))")
        uparams.extend(wl)
    params: list[Any] = []
    conds = ["TRUE"]
    regs = parse_regions(None, q.regions)
    for col, vals in (("region", regs), ("country", [c.upper() for c in q.countries]), ("sector_id", q.sector_ids),
                      ("subsector_id", q.subsector_ids), ("size_bucket", list(q.size_buckets))):
        if vals:
            conds.append(f"{col} IN ({', '.join('?' for _ in vals)})")
            params.extend(vals)
    if q.min_market_cap_usd is not None:
        conds.append("market_cap_usd >= ?")
        params.append(q.min_market_cap_usd)
    for f in q.filters:
        conds.append(_filter_sql(f, params))
    sort = _sql_of(q.sort_by) if q.sort_by else "composite_x"
    direction = "ASC" if q.sort_dir == "asc" else "DESC"
    cols = [c for c in q.columns if c]
    fields = screener_fields()
    extra = [(c, _COMPUTED.get(fields[c]["sql"], fields[c]["sql"])) for c in cols if c in fields]
    fsel = ", ".join(f"f_{f}" for f in FACTORS)
    xsel = "".join(f", {sql} AS v_{i}" for i, (_, sql) in enumerate(extra))
    lim = "" if export else f"LIMIT {int(q.limit)} OFFSET {int(q.offset)}"
    sql = f"""
        WITH u AS (SELECT s.*, {comp} AS composite_x FROM sec s WHERE {' AND '.join(ucond)}),
        g AS (SELECT *, {grade} AS grade_x, ({rank})::INTEGER AS rank_x FROM u)
        SELECT {sec_ref_sql('g')}, {', '.join(ROW_COLS)}, composite_x, grade_x, rank_x, {fsel}{xsel},
               count(*) OVER () AS _total
        FROM g WHERE {' AND '.join(conds)}
        ORDER BY {sort} {direction} NULLS LAST, market_cap_usd DESC NULLS LAST, security_id {lim}"""
    rows = db.all(sql, uparams + params)
    if rows:
        total = int(rows[0]["_total"])
    else:
        total = int(db.scalar(f"""WITH u AS (SELECT s.*, {comp} AS composite_x FROM sec s WHERE {' AND '.join(ucond)}),
                                   g AS (SELECT *, {grade} AS grade_x, ({rank})::INTEGER AS rank_x FROM u)
                                   SELECT count(*) FROM g WHERE {' AND '.join(conds)}""", uparams + params) or 0)
    return {"total": total, "weights": weights, "rows": rows, "extra": extra, "requested": cols}


def screener_row(r: dict[str, Any], extra: list[tuple[str, str]] | None = None,
                 requested: list[str] | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {c: None for c in (requested or [])}
    for i, (c, _) in enumerate(extra or []):
        v = r.get(f"v_{i}")
        values[c] = v if isinstance(v, (str, bool, int)) or v is None else fnum(v)
    grade = r.get("grade_x")
    return {
        "security": sec_ref(r),
        "price": fnum(r.get("close")),
        "chg_1d": fnum(r.get("chg_1d")),
        "chg_1m": fnum(r.get("chg_1m")),
        "composite": fnum(r.get("composite_x")),
        "grade": grade if grade in ("A", "B", "C", "D", "E") else None,
        "rank": int(r["rank_x"]) if r.get("rank_x") is not None else None,
        "factor_scores": {f: fnum(r.get(f"f_{f}")) for f in FACTORS},
        "moat_class": r.get("moat_class"),
        "ml_rank_pct_3m": fnum(r.get("ml_rp_3M")),
        "insider_signal": fnum(r.get("insider_signal")),
        "dq_score": fnum(r.get("dq_score")),
        "ml_expected_excess_3m": fnum(r.get("ml_exp_3M")),
        "values": values,
        # v1.44 full-universe extension
        "score_scope": r.get("score_scope") or ("mvp" if (r.get("u_mvp_us") or r.get("u_mvp_global")) else None),
        "ml_extrapolated_3m": bool(r.get("ml_extrap_3M")) if r.get("ml_rp_3M") is not None else None,
        "score_reason": _score_reason(r.get("score_missing")) if r.get("composite_x") is None else None,
    }


def _score_reason(missing: str | None) -> dict[str, Any] | None:
    if not missing:
        return None
    from app.core import availability as AV  # noqa: PLC0415

    return AV.make("insufficient_data", AV.describe_missing(missing)).public()


def rows_for_ids(db: Db, ids: list[str], profile_id: str | None, columns: list[str] | None = None) -> list[dict[str, Any]]:
    q = ScreenerQuery(profile_id=profile_id, columns=columns or [], limit=1000, sort_by="composite")
    res = run_query(db, q, ids=ids)
    return [screener_row(r, res["extra"], res["requested"]) for r in res["rows"]]


def data_status(db: Db) -> str:
    if not db.snap.input_ok("securities_master"):
        return "pending"
    return db.snap.status("securities_master", "prices_daily", "factor_scores")


@router.get("/screener/fields", operation_id="listScreenerFields")
def list_screener_fields() -> list[dict[str, Any]]:
    return [{k: v for k, v in f.items() if k != "sql"} for f in screener_fields().values()]


@router.post("/screener/query", operation_id="runScreener")
def run_screener(q: ScreenerQuery, db: Db = Depends(get_db)) -> dict[str, Any]:
    res = run_query(db, q)
    return {
        "as_of": db.as_of.isoformat(), "total": res["total"], "limit": q.limit, "offset": q.offset,
        "weights_used": res["weights"], "rows": [screener_row(r, res["extra"], res["requested"]) for r in res["rows"]],
        "data_status": data_status(db), "composite_label": _composite_label(q, res["rows"]),
    }


def _composite_label(q: ScreenerQuery, rows: list[dict[str, Any]]) -> str:
    """coverage v3 A8: 'descriptive' unless the stored profile ranked on passed its out-of-sample gate AND every
    composite on the page lies inside the population the gate tested (MVP scope, a security of the gate panel: the
    default universe 'full' mixes in extended-scope and untested composites); re-weighted (custom factor_weights)
    composites are never validated."""
    from app.analytics.composite_gate import LABEL, label_for  # noqa: PLC0415

    if weights_for(q.factor_weights, q.profile_id)[1]:
        return LABEL
    shown = [r for r in rows if r.get("composite_x") is not None]
    scopes = {r.get("score_scope") or ("mvp" if (r.get("u_mvp_us") or r.get("u_mvp_global")) else None) for r in shown}
    if scopes != {"mvp"}:
        return LABEL
    return label_for(q.profile_id or "balanced", "mvp", [r.get("ref_security_id") for r in shown])


@router.post("/screener/export", operation_id="exportScreener")
def export_screener(q: ScreenerQuery, db: Db = Depends(get_db)) -> Response:
    res = run_query(db, q, export=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    head = ["rank", "ticker", "name", "security_id", "isin", "region", "country", "sector_id", "subsector_id", "currency",
            "market_cap_usd", "price", "chg_1d", "chg_1m", "composite", "grade"] + list(FACTORS) + \
           ["moat_class", "ml_rank_pct_3m", "ml_expected_excess_3m", "insider_signal", "dq_score"] + \
           [c for c, _ in res["extra"]]
    w.writerow(head)
    for r in res["rows"]:
        row = screener_row(r, res["extra"], res["requested"])
        s = row["security"]
        w.writerow([row["rank"], s["ticker"], s["name"], s["security_id"], s.get("isin"), s["region"], s["country"],
                    s["sector_id"], s["subsector_id"], s["currency"], s["market_cap_usd"], row["price"], row["chg_1d"],
                    row["chg_1m"], row["composite"], row["grade"]] + [row["factor_scores"][f] for f in FACTORS] +
                   [row["moat_class"], row["ml_rank_pct_3m"], row["ml_expected_excess_3m"], row["insider_signal"],
                    row["dq_score"]] + [row["values"].get(c) for c, _ in res["extra"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="screener_{db.as_of.isoformat()}.csv"'})
