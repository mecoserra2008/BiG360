"""Job `conformal_coverage` (nightly, after conformal_envelopes and conformal_peer_groups): rolling coverage audit
of the peer-group sets from conformal_peer_scores / conformal_peer_members (joint and per-member coverage per region
bloc, group kind, year, W4 macro regime and high-VIX flag) -> conformal_coverage (application 'peer'). The
envelope / nextbar audit rows are written by the envelope job itself (it holds the per-origin scores in memory).

Also: summary() for the O1 coverage registry check `conformal_coverage` (app.ops.gaps) and the Data Health page.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.conformal import data as D
from app.conformal.envelope_job import REGIME_SPECS, Audit, _month_states, _state
from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger("app.conformal.coverage")

AUDIT_DAYS = 365


def peer_audit(as_of: date, settings: Settings | None = None, days: int = AUDIT_DAYS) -> pl.DataFrame:
    s = settings or get_settings()
    lo = as_of - timedelta(days=days)
    sc = scan_table("conformal_peer_scores", s).filter((pl.col("as_of_date") > lo) & (pl.col("as_of_date") <= as_of)).collect()
    if sc.is_empty():
        return pl.DataFrame()
    grp = scan_table("conformal_peer_groups", s).select("version", "group_id", "group_kind").unique().collect()
    sc = sc.join(grp, on=["version", "group_id"], how="left")
    vix = D.vix_daily(lo - timedelta(days=10), as_of, s)
    states = _month_states(D.macro_states(s))
    dts = np.array(sc["as_of_date"].to_list(), dtype="datetime64[D]")
    if not vix.is_empty():
        vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
        k = np.clip(np.searchsorted(vd, dts, side="right") - 2, 0, len(vd) - 1)     # VIX known at the previous close
        hv = vix["vix"].to_numpy()[k] > 25.0
    else:
        hv = np.zeros(len(dts), dtype=bool)
    months = dts.astype("datetime64[M]")
    au = Audit()
    d0, d1 = dts.min(), dts.max()
    flt = sc["filter"][0]
    score = sc["score"].to_numpy().astype(np.float64)
    for lvl, qc in ((80, "q_80"), (90, "q_90"), (95, "q_95")):
        q = sc[qc].to_numpy().astype(np.float64)
        cov = np.where(np.isfinite(score) & np.isfinite(q), (score <= q).astype(float), np.nan)
        au.add("peer", flt, "overall", "all", "joint", lvl, cov, d0, d1)
        for col, st in (("region_bloc", "region"), ("group_kind", "group_kind")):
            v = np.array(sc[col].fill_null("NA").to_list())
            for x in np.unique(v):
                au.add("peer", flt, st, str(x), "joint", lvl, cov[v == x], d0, d1)
        yrs = dts.astype("datetime64[Y]").astype(int) + 1970
        for y in np.unique(yrs):
            au.add("peer", flt, "year", str(y), "joint", lvl, cov[yrs == y], d0, d1)
        au.add("peer", flt, "vix", "high_vix", "joint", lvl, cov[hv], d0, d1)
        au.add("peer", flt, "vix", "normal_vix", "joint", lvl, cov[~hv], d0, d1)
        for rt, scp in REGIME_SPECS:
            mp = states.get((rt, scp), {})
            lab = np.array([mp.get(m, "") for m in months])
            for x in np.unique(lab):
                if x:
                    au.add("peer", flt, "regime", f"{rt}:{scp}={x}", "joint", lvl, cov[lab == x], d0, d1)
    if "q_aci_90" in sc.columns:
        q = sc["q_aci_90"].to_numpy().astype(np.float64)
        cov = np.where(np.isfinite(score) & np.isfinite(q), (score <= q).astype(float), np.nan)
        au.add("peer", flt + "+aci", "overall", "all", "joint", 90, cov, d0, d1)
        v = np.array(sc["region_bloc"].fill_null("NA").to_list())
        for x in np.unique(v):
            au.add("peer", flt + "+aci", "region", str(x), "joint", 90, cov[v == x], d0, d1)
    # per-member (per-coordinate) coverage: |r_i - mu_i| <= sqrt(q Sigma_ii), law from the previous session's row
    mem = scan_table("conformal_peer_members", s).filter((pl.col("as_of_date") > lo - timedelta(days=7)) & (pl.col("as_of_date") <= as_of)).collect()
    if mem.height:
        mem = mem.with_columns((pl.col("d_next") + pl.col("l1_next") ** 2 + pl.col("l2_next") ** 2 + pl.col("l3_next") ** 2
                                + pl.col("l4_next") ** 2).alias("var_next")).sort(["group_id", "security_id", "as_of_date"])
        mem = mem.with_columns(pl.col("mu_next").shift(1).over(["group_id", "security_id"]).alias("mu_prev"),
                               pl.col("var_next").shift(1).over(["group_id", "security_id"]).alias("var_prev"),
                               pl.col("as_of_date").shift(1).over(["group_id", "security_id"]).alias("d_prev"))
        qprev = sc.select("group_id", "as_of_date", "q_90").sort(["group_id", "as_of_date"]).with_columns(
            pl.col("as_of_date").shift(-1).over("group_id").alias("next_date")).drop_nulls("next_date")
        mm = mem.join(qprev.select("group_id", pl.col("next_date").alias("as_of_date"), pl.col("q_90").alias("q_prev")),
                      on=["group_id", "as_of_date"], how="inner").filter(pl.col("as_of_date") > lo)
        if mm.height:
            c = ((mm["r"] - mm["mu_prev"]).abs() <= (mm["q_prev"] * mm["var_prev"]).sqrt()).cast(pl.Float64).to_numpy()
            ok = np.isfinite(mm["var_prev"].to_numpy().astype(float))
            au.add("peer", flt, "overall", "all", "member", 90, np.where(ok, c, np.nan), d0, d1)
    return au.frame(as_of)


def summary(settings: Settings | None = None) -> dict[str, Any]:
    """Latest overall rows per application / filter / level (for Data Health and the gap detector)."""
    s = settings or get_settings()
    try:
        df = scan_table("conformal_coverage", s).collect()
    except Exception:  # noqa: BLE001
        return {"as_of": None, "rows": []}
    if df.is_empty():
        return {"as_of": None, "rows": []}
    out = {}
    for app in df["application"].unique().to_list():
        d = df.filter(pl.col("application") == app)
        last = d["as_of_date"].max()
        out[app] = d.filter(pl.col("as_of_date") == last)
    rows = pl.concat(list(out.values()), how="diagonal_relaxed")
    return {"as_of": str(rows["as_of_date"].max()), "rows": rows.to_dicts()}


def run_coverage(settings: Settings | None = None, as_of: date | None = None, write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    df = peer_audit(as_of, s)
    rep: dict[str, Any] = {"as_of": str(as_of), "peer_rows": df.height}
    if write and df.height:
        write_table(df, "conformal_coverage", mode="upsert", settings=s)
    if df.height:
        o = df.filter((pl.col("segment_type") == "overall"))
        rep["overall"] = {f"{r['filter']}|{r['horizon']}|{r['level']}": round(r["coverage"], 4) for r in o.iter_rows(named=True)}
    _state(s, last_coverage_run=rep)
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_coverage(ctx.settings, as_of=date.fromisoformat(p["as_of"]) if p.get("as_of") else ctx.as_of)
    return JobResult(status="success", rows_written={"conformal_coverage": rep.get("peer_rows", 0)},
                     message=json.dumps(rep, default=str)[:4000])


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run_coverage(as_of=date.fromisoformat(a.as_of) if a.as_of else None, write=not a.dry_run),
                     indent=1, default=str))
