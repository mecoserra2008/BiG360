"""C5 job `event_exposure`: company event exposure = country weights (geographic revenue from linkage
`spatial_weights` exposure / company_geo_revenue, else home country; + supply-chain customers' countries) x
recent country event pressure, plus the expected impact of active shocks from the event-study library.

params: universe (full), days (7).
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table
from app.events_models import common as C
from app.events_models.event_study import FALLBACKS, shocks

log = get_logger(__name__)

SUPPLY_WEIGHT = 0.25
SHRINK_K = 10    # expected CAR of a library cell with n events is shrunk by n / (n + SHRINK_K)
LEVEL_PREF = ("issuer", "country_index", "sector_etf")


def universe(settings: Settings, uni: str = "full") -> pd.DataFrame:
    con = C.lake_con(["universe_membership", "securities_master", "issuers"], settings)
    return con.execute("""
        SELECT DISTINCT u.security_id, s.issuer_id, i.country, i.sector_id
        FROM universe_membership u JOIN securities_master s ON s.security_id = u.security_id
        JOIN issuers i ON i.issuer_id = s.issuer_id
        WHERE u.universe_id = ? AND u.valid_to IS NULL AND s.issuer_id IS NOT NULL""", [uni]).df()


def geo_weights(issuers: list[str], as_of: Any) -> tuple[dict[str, dict[str, float]], str]:
    """issuer -> {iso2: share} from linkage (bipartite issuer_country 'exposure', row-standardized)."""
    try:
        from app.linkage.weights import get_W  # type: ignore  # noqa: PLC0415

        rows, cols, m = get_W("exposure", "issuer_country", as_of, standardize="r", fmt="sparse")
    except Exception as e:  # noqa: BLE001
        log.info("exposure: no linkage geographic revenue weights (%s)", e)
        return {}, "none"
    want = set(issuers)
    out: dict[str, dict[str, float]] = {}
    m = m.tocsr()
    for r, iid in enumerate(rows):
        if iid not in want:
            continue
        a, b = m.indptr[r], m.indptr[r + 1]
        if b > a:
            out[iid] = {cols[j]: float(v) for j, v in zip(m.indices[a:b], m.data[a:b]) if v > 0}
    return out, "linkage"


def customer_links(issuers: list[str], as_of: Any) -> dict[str, dict[str, float]]:
    """supplier issuer -> {customer issuer: revenue share} (linkage spatial_weights customer)."""
    try:
        from app.linkage.weights import get_W  # type: ignore  # noqa: PLC0415

        nodes, m = get_W("customer", "issuer", as_of, standardize="raw", fmt="sparse")
    except Exception as e:  # noqa: BLE001
        log.info("exposure: no supply-chain links (%s)", e)
        return {}
    m = m.tocsr()
    want = set(issuers)
    out: dict[str, dict[str, float]] = {}
    for r, iid in enumerate(nodes):
        if iid in want and m.indptr[r + 1] > m.indptr[r]:
            a, b = m.indptr[r], m.indptr[r + 1]
            out[iid] = {nodes[j]: float(v) for j, v in zip(m.indices[a:b], m.data[a:b]) if v > 0}
    return out


def country_pressure(t1: pl.DataFrame, days: int) -> tuple[dict[str, float], Any]:
    last = t1["date"].max()
    rec = t1.filter((pl.col("theme") == -1) & (pl.col("date") > last - timedelta(days=days)))
    p = rec.group_by("label").agg((pl.col("attention_z").clip(lower_bound=0).mean().fill_null(0) +
                                   (-pl.col("tone_z")).clip(lower_bound=0).mean().fill_null(0)).alias("p"))
    return dict(zip(p["label"].to_list(), p["p"].to_list())), last


def run_exposure(settings: Settings | None = None, uni: str = "full", days: int = 7) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    t1 = C.load_t1("country", settings=s)
    if t1.is_empty():
        return {"rows": 0}
    press, last = country_pressure(t1, days)
    U = universe(s, uni)
    if U.empty:
        return {"rows": 0, "note": "empty universe"}
    iss = U["issuer_id"].unique().tolist()
    geo, gsrc = geo_weights(iss, last)
    cust = customer_links(iss, last)
    home = dict(zip(U["issuer_id"], U["country"]))
    # active shocks (country x theme) in the window and the expected-reaction library
    sh = shocks(t1.filter(pl.col("date") > last - timedelta(days=days + 15)))
    sh = sh.filter((pl.col("date") > last - timedelta(days=days)) & (pl.col("kind") == "attention") &
                   (pl.col("label") != "GLOBAL")).to_pandas()
    sh["theme"] = sh["theme"].map(lambda b: "all" if b == -1 else C.THEME_ID.get(int(b), str(b)))
    try:
        lib = read_table("event_study_library", settings=s).filter((pl.col("window") == "0,5") &
                                                                    (pl.col("shock_kind") == "attention") &
                                                                    (pl.col("n_events") >= 3))
    except Exception:  # noqa: BLE001
        lib = pl.DataFrame()
    cells: dict[tuple, dict[str, Any]] = {}
    if lib.height:
        for r in lib.iter_rows(named=True):
            cells[(r["level"], r["theme"], r["iso2"], r["sector"])] = r
    cache: dict[tuple, tuple[float | None, int]] = {}

    def expected(theme: str, cc: str, sec: str | None) -> tuple[float | None, int]:
        k = (theme, cc, sec)
        if k in cache:
            return cache[k]
        val: tuple[float | None, int] = (None, 0)
        want = {"theme": theme, "iso2": cc, "sector": sec or "ALL"}
        for keys in FALLBACKS:
            key = tuple(want[x] if x in keys else "ALL" for x in ("theme", "iso2", "sector"))
            hit = next((cells[(lv, *key)] for lv in LEVEL_PREF if (lv, *key) in cells), None)
            if hit is not None:
                val = (float(hit["mean_car"]), int(hit["n_events"]))
                break
        cache[k] = val
        return val

    now = C.now_utc()
    rows = []
    shock_by_c = {c: list(zip(g["theme"].tolist(), g["attention_z"].astype(float).tolist()))
                  for c, g in sh.groupby("label")} if len(sh) else {}
    for r in U.itertuples(index=False):
        w: dict[str, float] = dict(geo.get(r.issuer_id) or {})
        src = "geo_revenue" if w else "home_country"
        if not w and r.country:
            w = {r.country: 1.0}
        if r.issuer_id in cust:
            add: dict[str, float] = {}
            for cid, sh_ in cust[r.issuer_id].items():
                cc = home.get(cid)
                if cc:
                    add[cc] = add.get(cc, 0.0) + SUPPLY_WEIGHT * min(sh_, 1.0)
            if add:
                for cc, v in add.items():
                    w[cc] = w.get(cc, 0.0) + v
                src += "+supply_chain"
        tot = sum(w.values()) or 1.0
        w = {k: v / tot for k, v in w.items()}
        contrib = {cc: wt * press.get(cc, 0.0) for cc, wt in w.items()}
        pressure = float(sum(contrib.values()))
        imp, shocks_l, any_e = 0.0, [], False
        for cc, wt in w.items():
            g = shock_by_c.get(cc)
            if g is None:
                continue
            es = []
            for theme, az in g:
                e, n = expected(theme, cc, r.sector_id)
                if e is not None:
                    es.append(e * n / (n + SHRINK_K))      # shrink small-sample cells towards 0
                shocks_l.append({"iso2": cc, "theme": theme, "attention_z": round(az, 2),
                                 "expected_car": None if e is None else round(e, 3), "n": n})
            if es:   # concurrent shocks of one country overlap (same news flow): average, do not add up
                imp += wt * float(np.mean(es))
                any_e = True
        topc = sorted(((cc, w[cc], press.get(cc, 0.0)) for cc in w), key=lambda t: -t[1] * t[2])[:5]
        rows.append({"as_of": last, "security_id": r.security_id, "issuer_id": r.issuer_id, "country": r.country,
                     "sector_id": r.sector_id, "event_pressure": pressure,
                     "expected_impact": imp if any_e else None, "n_countries": len(w),
                     "top_countries": json.dumps([{"iso2": c, "weight": round(a, 4), "pressure": round(b, 3)} for c, a, b in topc]),
                     "top_shocks": json.dumps(sorted(shocks_l, key=lambda d: -abs(d["expected_car"] or 0))[:8]) if shocks_l else None,
                     "w_source": src, "computed_at": now})
    df = pl.DataFrame(rows, infer_schema_length=None)
    df = df.with_columns((pl.col("event_pressure").rank("average") / pl.len() * 100).alias("exposure_score"))
    n = C.write(df, "company_event_exposure", settings=s)
    rep = {"rows": n, "as_of": str(last), "geo_source": gsrc, "with_geo_revenue": int((df["w_source"].str.starts_with("geo")).sum()),
           "with_supply_chain": int(df["w_source"].str.contains("supply").sum()), "active_shocks": int(len(sh)),
           "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_exposure", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_exposure(ctx.settings, uni=str(p.get("universe", "full")), days=int(p.get("days", 7)))
    return JobResult(status="success", rows_written={"company_event_exposure": rep.get("rows", 0)}, message=str(rep))
