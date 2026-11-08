"""B3 job `event_connectedness`: rolling Diebold-Yilmaz connectedness of country indices and US sector ETFs.

params: start (default 2012-01-01), window (200), step (5), horizon (10), lags (1), ridge (0.05), full (recompute all).
Incremental: window end dates already stored for a universe are skipped.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table
from app.events_models import common as C
from app.modeling._spatial import dy_connectedness

log = get_logger(__name__)

UNIVERSES = ("country_ret", "country_vol", "sector_ret", "sector_vol")


def _ohlc(ids: list[str], start: date, settings: Settings) -> pd.DataFrame:
    con = C.lake_con(["prices_daily"], settings)
    q = (f"SELECT security_id, date, coalesce(adj_close, close) AS px, high, low, close FROM prices_daily "
         f"WHERE security_id IN ({','.join('?' * len(ids))}) AND date >= ?")
    df = con.execute(q, list(ids) + [start]).df()
    df["date"] = pd.to_datetime(df["date"])
    return df


def node_panels(start: date, settings: Settings) -> dict[str, tuple[pd.DataFrame, dict[str, str]]]:
    """universe -> (dates x nodes frame, {node: series_id}). Returns in %, vol = log Parkinson range sd (%)."""
    out: dict[str, tuple[pd.DataFrame, dict[str, str]]] = {}
    for grp, mapping in (("country", {c: v for c, v in C.COUNTRY_INDEX.items()}),
                         ("sector", {s: [e] for s, e in C.SECTOR_ETF.items()})):
        ids = sorted({i for v in mapping.values() for i in v})
        raw = _ohlc(ids, start, settings)
        if raw.empty:
            continue
        cnt = raw.groupby("security_id").size()
        used: dict[str, str] = {}
        for node, cands in mapping.items():
            for sid in cands:
                if cnt.get(sid, 0) >= 250:
                    used[node] = sid
                    break
        sub = raw[raw["security_id"].isin(used.values())].copy()
        inv = {v: k for k, v in used.items()}
        sub["node"] = sub["security_id"].map(inv)
        sub = sub.sort_values(["node", "date"])
        sub["ret"] = sub.groupby("node")["px"].transform(lambda s: np.log(s.where(s > 0)).diff()) * 100
        sub.loc[sub["ret"].abs() > 40, "ret"] = np.nan
        hl = np.log(sub["high"].where(sub["high"] > 0) / sub["low"].where(sub["low"] > 0))
        park = np.sqrt(0.3607 * hl ** 2) * 100
        park = park.where((hl > 0) & (hl < 0.5))
        sub["vol"] = np.log(park.fillna(sub["ret"].abs()).clip(lower=0.01))
        for var in ("ret", "vol"):
            w = sub.pivot_table(index="date", columns="node", values=var, aggfunc="last").sort_index()
            # common calendar: keep days where >= 70% of nodes traded; holidays of the rest -> 0 return / ffill vol
            keep = w.notna().mean(axis=1) >= 0.7
            w = w[keep]
            # holidays filled per window in run_connectedness (ret 0, vol ffill); NaN before a series starts stays NaN
            out[f"{grp}_{var}"] = (w, used)
    return out


def run_connectedness(settings: Settings | None = None, start: date = date(2012, 1, 1), window: int = 200, step: int = 5,
                      horizon: int = 10, lags: int = 1, ridge: float = 0.05, full: bool = False) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    panels = node_panels(start, s)
    try:
        old = read_table("connectedness", columns=["universe", "date", "row_type"], settings=s) \
            .filter(pl.col("row_type") == "system")
    except Exception:  # noqa: BLE001
        old = pl.DataFrame({"universe": [], "date": []}, schema={"universe": pl.Utf8, "date": pl.Date})
    now = C.now_utc()
    rows: list[dict[str, Any]] = []
    rep: dict[str, Any] = {}
    for uni, (df, used) in panels.items():
        done = set() if full else set(old.filter(pl.col("universe") == uni)["date"].to_list())
        dates = list(df.index)
        vals = df.to_numpy(float)
        names = list(df.columns)
        ends = list(range(window, len(df) + 1, step))
        if ends and ends[-1] != len(df):
            ends.append(len(df))
        n_new = 0
        month_end = set(pd.Series(dates).groupby(pd.Series(dates).dt.to_period("M")).max().tolist())
        for e in ends:
            d = dates[e - 1].date()
            if d in done and e != len(df):
                continue
            Y = vals[e - window: e]
            okc = np.isfinite(Y).mean(axis=0) >= 0.9
            Y = Y[:, okc]
            if uni.endswith("_ret"):
                Y = np.where(np.isfinite(Y), Y, 0.0)
            else:
                Y = pd.DataFrame(Y).ffill().bfill().to_numpy()
            Y = Y[np.isfinite(Y).all(axis=1)]
            nm = [n for n, k in zip(names, okc) if k]
            if len(nm) < 3 or len(Y) < window * 0.8:
                continue
            t = dy_connectedness(Y, lags, horizon, ridge)
            base = {"universe": uni, "date": d, "window": window, "horizon": horizon, "lags": lags, "n_nodes": len(nm),
                    "computed_at": now}
            rows.append(base | {"row_type": "system", "node": "*", "other": "*", "total": t["total"], "to_others": None,
                                "from_others": None, "net": None, "series_id": None})
            for i, n in enumerate(nm):
                rows.append(base | {"row_type": "node", "node": n, "other": "*", "total": None,
                                    "to_others": float(t["to"][i]), "from_others": float(t["from"][i]),
                                    "net": float(t["net"][i]), "series_id": used.get(n)})
            if pd.Timestamp(d) in month_end or e == len(df):
                for i, a in enumerate(nm):
                    for j, b in enumerate(nm):
                        if i != j:
                            # theta[i, j] = share of i's variance from j; net flow a -> b = theta[b, a] - theta[a, b]
                            rows.append(base | {"row_type": "pair", "node": a, "other": b, "total": None,
                                                "to_others": None, "from_others": None,
                                                "net": float(t["theta"][j, i] - t["theta"][i, j]), "series_id": None})
            n_new += 1
        rep[uni] = {"windows": n_new, "nodes": len(names), "first": str(dates[0].date()) if dates else None,
                    "last": str(dates[-1].date()) if dates else None}
    n = C.write(pl.DataFrame(rows, infer_schema_length=None), "connectedness", settings=s,
                mode="overwrite_partition" if full else None) if rows else 0
    out = {"rows": n, "universes": rep, "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_connectedness", out | {"at": now.isoformat(timespec="seconds")}, s)
    return out


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_connectedness(ctx.settings, start=date.fromisoformat(str(p.get("start", "2012-01-01"))),
                            window=int(p.get("window", 200)), step=int(p.get("step", 5)),
                            horizon=int(p.get("horizon", 10)), lags=int(p.get("lags", 1)),
                            ridge=float(p.get("ridge", 0.05)),
                            full=str(p.get("full", "false")).lower() in ("1", "true", "yes"))
    return JobResult(status="success", rows_written={"connectedness": rep["rows"]}, message=str(rep))
