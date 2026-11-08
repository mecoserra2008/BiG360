"""GDP nowcasts (W2.4). Job `macro_nowcast` (nightly; re-estimates only when the input panel changed).

Our model (method=dfm_mq, model-based): statsmodels DynamicFactorMQ (mixed monthly/quarterly, Mariano-Murasawa
aggregation, Banbura-Modugno EM), 1 global factor with AR(2) dynamics, AR(1) idiosyncratic errors, standardized inputs.
Inputs are point-in-time (as-of the nowcast date: ALFRED vintages for US vintage series, realtime knowledge dates
otherwise), sample from 2000-01, pandemic months 2020-03..2020-12 (2020Q2-Q4) masked as missing (documented: they
dominate EM estimates). Panels (official sources only):
  US  quarterly real GDP (GDPC1, q/q %, reported annualized) | monthly: IP (INDPRO), payrolls (PAYEMS), real retail
      sales (RRSFS), unemployment rate (UNRATE), real PCE (PCEC96), real income ex transfers (W875RX1), housing starts
      (HOUST), durable goods orders (DGORDER), initial claims (ICSA, monthly mean), capacity utilization (TCU), real
      manufacturing & trade sales (CMRMTSPL), UMich sentiment (UMCSENT), Philadelphia / Dallas Fed manufacturing
      surveys, OECD CLI (US)
  EA  quarterly real GDP q/q % (ECB MNA) | IP index (Eurostat B-D), retail y/y, unemployment, EC ESI / industry /
      consumer confidence (Eurostat), OECD BCI
  GB  quarterly real GDP q/q % (ONS IHYQ) | IP (K222), retail volumes (J5EK), monthly GDP index (ECY2),
      unemployment (MGSX), employment rate (LF24), OECD BCI/CCI
  JP  quarterly real GDP q/q % (OECD QNA) | IP y/y, retail y/y, unemployment (OECD KEI), OECD BCI/CCI/CLI
Transforms: 'dlog' = 100 * log difference, 'diff' = first difference, 'level' = as published (diffusion / y/y series).
Targets: every quarter from the first one without a published GDP (as of the date) to the current quarter.
Interval: 80% from the smoothed prediction variance (incl. idiosyncratic noise). News: res_now.news(res_prev) with the
current parameters for both vintages -> contribution of each updated series; the parameter re-estimation effect is
reported as its own item so contributions add up to (nowcast - prev_nowcast).

Official benchmarks (method=official_benchmark, model_based=false), downloaded verbatim:
  Atlanta Fed GDPNow (GDPTrackingModelDataAndForecasts.xlsx: TrackingArchives + CurrentQtrEvolution)
  New York Fed Staff Nowcast (New-York-Fed-Staff-Nowcast_download_data.xlsx: Forecasts By Quarter; 80% bands of
  concluded quarters from nowcast-history-meta-data.csv).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.macro.calendar import asof_series, load_obs

log = get_logger(__name__)

SAMPLE_START = "2000-01"
COVID_M = (pd.Period("2020-03", "M"), pd.Period("2020-12", "M"))
COVID_Q = (pd.Period("2020Q2", "Q"), pd.Period("2020Q4", "Q"))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126 Safari/537.36 BiG-Equity-360 research"}
GDPNOW_URL = ("https://www.atlantafed.org/-/media/Project/Atlanta/FRBA/Documents/research-and-data/data/gdpnow/"
              "GDPTrackingModelDataAndForecasts.xlsx")
NYFED_URL = ("https://www.newyorkfed.org/medialibrary/Research/Interactives/Data/NowCast/downloads/"
             "New-York-Fed-Staff-Nowcast_download_data.xlsx")
NYFED_META_URL = "https://www.newyorkfed.org/medialibrary/Research/Interactives/Data/NowCast/data/nowcast-history-meta-data.csv"
OUR_LABEL = "Model-based nowcast (ours, DynamicFactorMQ)"


@dataclass(frozen=True)
class Panel:
    cc: str
    model_id: str
    gdp: tuple[str, str]                     # (series_id, transform: pct | level)
    monthly: tuple[tuple[str, str], ...]     # (series_id, transform: dlog | diff | level | dlog_wk)
    units: str
    annualize: bool


PANELS: dict[str, Panel] = {
    "US": Panel("US", "dfm_mq_us_v1", ("fred:GDPC1", "pct"), (
        ("fred:INDPRO", "dlog"), ("fred:PAYEMS", "dlog"), ("fred:RRSFS", "dlog"), ("fred:UNRATE", "diff"),
        ("fred:PCEC96", "dlog"), ("fred:W875RX1", "dlog"), ("fred:HOUST", "dlog"), ("fred:DGORDER", "dlog"),
        ("fred:ICSA", "dlog_wk"), ("fred:TCU", "diff"), ("fred:CMRMTSPL", "dlog"), ("fred:UMCSENT", "diff"),
        ("fred:GACDFSA066MSFRBPHI", "level"), ("fred:BACTSAMFRBDAL", "level"), ("oecd:CLI.USA.LI", "diff")),
        "q/q % SAAR", True),
    "EA": Panel("EA", "dfm_mq_ea_v1", ("ecb:MNA.Q.Y.I10.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.G1", "level"), (
        ("eurostat:sts_inpr_m.EA.B-D", "dlog"), ("eurostat:sts_trtu_m.EA.PCH_SM", "level"),
        ("eurostat:une_rt_m.EA.SA.TOTAL", "diff"), ("eurostat:ei_bssi_m_r2.EA.ESI", "level"),
        ("eurostat:ei_bssi_m_r2.EA.ICI", "level"), ("eurostat:ei_bssi_m_r2.EA.CSMCI", "level"),
        ("oecd:CLI.EA20.BCICP", "level")), "q/q %", False),
    "GB": Panel("GB", "dfm_mq_gb_v1", ("ons:IHYQ", "level"), (
        ("ons:K222", "dlog"), ("ons:J5EK", "dlog"), ("ons:ECY2", "dlog"), ("ons:MGSX", "diff"), ("ons:LF24", "diff"),
        ("oecd:CLI.GBR.BCICP", "level"), ("oecd:CLI.GBR.CCICP", "level")), "q/q %", False),
    "JP": Panel("JP", "dfm_mq_jp_v1", ("oecd:QNA.JPN.G1", "level"), (
        ("oecd:KEI.JPN.PRVM.GY", "level"), ("oecd:KEI.JPN.TOVM.GY", "level"), ("oecd:KEI.JPN.UNEMP", "diff"),
        ("oecd:CLI.JPN.BCICP", "level"), ("oecd:CLI.JPN.CCICP", "level"), ("oecd:CLI.JPN.LI", "diff")), "q/q %", False),
}
GDP_NAME = "GDP"


# ------------------------------------------------------------------------------------------------ data


def _monthly(obs: pd.DataFrame | None, as_of: date, tf: str, idx: pd.PeriodIndex) -> pd.Series | None:
    if obs is None or obs.empty:
        return None
    if tf == "dlog_wk":
        s = asof_series(obs, as_of + timedelta(days=1), "W", strict=True).dropna()
        if s.empty:
            return None
        s.index = pd.PeriodIndex([pd.Period(d, "M") for d in s.index], freq="M")
        s = s.groupby(level=0).mean()
        # drop the in-progress month (incomplete weeks)
        s = s[s.index < pd.Period(as_of, "M")] if len(s) else s
        tf = "dlog"
    else:
        s = asof_series(obs, as_of + timedelta(days=1), "M", strict=True)
        if s.dropna().empty:
            return None
        s.index = pd.PeriodIndex([pd.Period(d, "M") for d in s.index], freq="M")
    s = s.astype(float)
    if tf == "dlog":
        s = 100.0 * np.log(s.where(s > 0)).diff()
    elif tf == "diff":
        s = s.diff()
    s = s.reindex(idx)
    s[(s.index >= COVID_M[0]) & (s.index <= COVID_M[1])] = np.nan
    return s


def _quarterly(obs: pd.DataFrame | None, as_of: date, tf: str, idx: pd.PeriodIndex) -> pd.Series | None:
    if obs is None or obs.empty:
        return None
    s = asof_series(obs, as_of + timedelta(days=1), "Q", strict=True)
    if s.dropna().empty:
        return None
    s.index = pd.PeriodIndex([pd.Period(d, "Q") for d in s.index], freq="Q")
    s = s.astype(float)
    if tf == "pct":
        s = 100.0 * (s / s.shift(1) - 1.0)
    s = s.reindex(idx)
    s[(s.index >= COVID_Q[0]) & (s.index <= COVID_Q[1])] = np.nan
    return s


def build_dataset(p: Panel, obs: dict[str, pd.DataFrame], as_of: date, end_q: pd.Period | None = None
                  ) -> tuple[pd.DataFrame, pd.DataFrame]:
    end_q = end_q or pd.Period(as_of, "Q")
    midx = pd.period_range(SAMPLE_START, end_q.asfreq("M", "end"), freq="M")
    qidx = pd.period_range(pd.Period(SAMPLE_START, "M").asfreq("Q"), end_q, freq="Q")
    cols = {}
    for sid, tf in p.monthly:
        s = _monthly(obs.get(sid), as_of, tf, midx)
        if s is not None and s.notna().sum() >= 60:
            cols[sid] = s
    m = pd.DataFrame(cols, index=midx)
    q = _quarterly(obs.get(p.gdp[0]), as_of, p.gdp[1], qidx)
    qd = pd.DataFrame({GDP_NAME: q if q is not None else np.nan}, index=qidx)
    return m, qd


def fingerprint(m: pd.DataFrame, q: pd.DataFrame) -> str:
    h = hashlib.sha1()
    h.update(pd.util.hash_pandas_object(m.round(8), index=True).values.tobytes())
    h.update(pd.util.hash_pandas_object(q.round(8), index=True).values.tobytes())
    return h.hexdigest()[:16]


def last_gdp_quarter(q: pd.DataFrame) -> pd.Period | None:
    v = q[GDP_NAME].dropna()
    return v.index[-1] if len(v) else None


def target_quarters(q: pd.DataFrame, as_of: date) -> list[pd.Period]:
    cur = pd.Period(as_of, "Q")
    last = last_gdp_quarter(q)
    start = (last + 1) if last is not None else cur
    out, t = [], start
    while t <= cur:
        out.append(t)
        t += 1
    return out[-2:]


# ------------------------------------------------------------------------------------------------ model


def fit(m: pd.DataFrame, q: pd.DataFrame, maxiter: int = 200):
    from statsmodels.tsa.statespace.dynamic_factor_mq import DynamicFactorMQ  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mod = DynamicFactorMQ(m, endog_quarterly=q, factors=1, factor_orders=2, idiosyncratic_ar1=True, standardize=True)
        return mod.fit(disp=False, maxiter=maxiter, tolerance=1e-5)


def apply(res, m: pd.DataFrame, q: pd.DataFrame):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return res.apply(m, endog_quarterly=q, retain_standardization=True)


def extract(res, targets: list[pd.Period], annualize: bool) -> dict[pd.Period, tuple[float, float, float]]:
    """Smoothed GDP estimate (and 80% interval) at the last month of each target quarter."""
    out = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pred = res.get_prediction(information_set="smoothed")
        mean = pred.predicted_mean[GDP_NAME]
        ci = pred.conf_int(alpha=0.2)
    lo_c = [c for c in ci.columns if GDP_NAME in str(c) and "lower" in str(c)][0]
    hi_c = [c for c in ci.columns if GDP_NAME in str(c) and "upper" in str(c)][0]
    for t in targets:
        mm = t.asfreq("M", "end")
        if mm not in mean.index:
            continue
        v, lo, hi = float(mean.loc[mm]), float(ci.loc[mm, lo_c]), float(ci.loc[mm, hi_c])
        if annualize:
            v, lo, hi = (((1 + x / 100.0) ** 4 - 1) * 100.0 for x in (v, lo, hi))
        out[t] = (v, lo, hi)
    return out


def news_items(res_new, res_prev, target: pd.Period, annualize: bool, titles: dict[str, str]) -> list[dict[str, Any]]:
    """Contribution of each updated series (and data revisions) to the change of the target nowcast (current params)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        nw = res_new.news(res_prev, impact_date=target.asfreq("M", "end"), impacted_variable=GDP_NAME,
                          comparison_type="previous")
    items: dict[str, float] = {}
    try:
        det = nw.details_by_impact
        if det is not None and len(det):
            lvl = [n for n in det.index.names if n and "updated" in n.lower() and "variable" in n.lower()][0]
            for k, v in det["impact"].groupby(level=lvl).sum().items():
                items[str(k)] = float(v)
    except Exception as e:  # noqa: BLE001
        log.info("news details unavailable: %s", e)
    try:
        rev = float(np.nansum(np.asarray(nw.revision_impacts.loc[:, GDP_NAME] if nw.revision_impacts is not None
                                         else 0.0)))
        if rev:
            items["(data revisions)"] = rev
    except Exception:  # noqa: BLE001
        pass
    fac = 4.0 if annualize else 1.0   # first-order annualization of contributions
    return [{"series_id": k, "title": titles.get(k, k), "contribution": round(v * fac, 4)}
            for k, v in sorted(items.items(), key=lambda kv: -abs(kv[1])) if abs(v) > 1e-9]


# ------------------------------------------------------------------------------------------------ runs


SCHEMA = {"model_id": pl.Utf8, "country": pl.Utf8, "method": pl.Utf8, "target_period": pl.Date, "target_label": pl.Utf8,
          "as_of": pl.Date, "nowcast": pl.Float64, "lo": pl.Float64, "hi": pl.Float64, "units": pl.Utf8,
          "prev_nowcast": pl.Float64, "news": pl.Utf8, "source": pl.Utf8, "model_based": pl.Boolean, "label": pl.Utf8,
          "computed_at": pl.Datetime("us", "UTC"), "run_id": pl.Utf8}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SCHEMA, orient="row") if rows else pl.DataFrame(schema=SCHEMA)


def _qlabel(t: pd.Period) -> str:
    return f"{t.year}Q{t.quarter}"


def nowcast_history(p: Panel, obs: dict[str, pd.DataFrame], as_ofs: list[date], titles: dict[str, str],
                    refit: str = "quarterly", prev: dict[pd.Period, tuple[float, date]] | None = None,
                    run_id: str | None = None) -> tuple[list[dict[str, Any]], Any]:
    """Nowcasts for a sequence of as-of dates (ascending). Parameters re-estimated at the first date of each quarter
    (refit='quarterly') or at every date ('always'); other dates reuse them (apply). News vs the previous date."""
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    res = None
    res_q: pd.Period | None = None
    prev = dict(prev or {})
    prev_data: tuple[pd.DataFrame, pd.DataFrame] | None = None
    for d in as_ofs:
        m, q = build_dataset(p, obs, d)
        if m.shape[1] < 3 or q[GDP_NAME].notna().sum() < 20:
            continue
        cq = pd.Period(d, "Q")
        newfit = res is None or refit == "always" or cq != res_q
        try:
            res_d = fit(m, q) if newfit else apply(res, m, q)
        except Exception as e:  # noqa: BLE001
            log.warning("dfm %s %s failed: %s", p.model_id, d, e)
            continue
        if newfit:
            res, res_q = res_d, cq
        tg = target_quarters(q, d)
        est = extract(res_d, tg, p.annualize)
        for t, (v, lo, hi) in est.items():
            news = None
            pv = prev.get(t)
            if prev_data is not None and pv is not None:
                try:
                    pm, pq = prev_data
                    pm = pm.reindex(m.index)
                    pq = pq.reindex(q.index)
                    res_prev = apply(res_d, pm, pq)
                    news = news_items(res_d, res_prev, t, p.annualize, titles)
                    same = extract(res_prev, [t], p.annualize).get(t)
                    if same is not None:
                        reest = (pv[0] - same[0])
                        resid = (v - pv[0]) - sum(n["contribution"] for n in news)
                        if abs(resid) > 1e-6:
                            news.append({"series_id": "(re-estimation & nonlinearity)", "title":
                                         "Parameter re-estimation / annualization residual", "contribution": round(resid, 4)})
                        del reest
                except Exception as e:  # noqa: BLE001
                    log.info("news %s %s: %s", p.model_id, d, e)
            rows.append({"model_id": p.model_id, "country": p.cc, "method": "dfm_mq", "target_period": t.start_time.date(),
                         "target_label": _qlabel(t), "as_of": d, "nowcast": v, "lo": lo, "hi": hi, "units": p.units,
                         "prev_nowcast": pv[0] if pv else None, "news": json.dumps(news) if news is not None else None,
                         "source": "BiG Equity 360 model (official inputs: " + ", ".join(sorted(m.columns)) + ")",
                         "model_based": True, "label": OUR_LABEL, "computed_at": now, "run_id": run_id})
            prev[t] = (v, d)
        prev_data = (m, q)
    return rows, res


def _titles(settings: Settings) -> dict[str, str]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        ms = read_table("macro_series", columns=["series_id", "title"], settings=settings)
        return dict(zip(ms["series_id"].to_list(), ms["title"].to_list(), strict=False))
    except Exception:  # noqa: BLE001
        return {}


def _prev_rows(settings: Settings, model_id: str, before: date) -> tuple[dict[pd.Period, tuple[float, date]], date | None]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        nc = read_table("macro_nowcasts", columns=["model_id", "target_period", "as_of", "nowcast"],
                        filters=(pl.col("model_id") == model_id) & (pl.col("as_of") < before), settings=settings)
    except Exception:  # noqa: BLE001
        return {}, None
    if nc.is_empty():
        return {}, None
    last = nc["as_of"].max()
    out = {}
    for r in nc.filter(pl.col("as_of") == last).iter_rows(named=True):
        out[pd.Period(r["target_period"], "Q")] = (r["nowcast"], r["as_of"])
    return out, last


def run_nowcast(settings: Settings | None = None, countries: list[str] | None = None, as_of: date | None = None,
                history_weeks: int = 0, force: bool = False, run_id: str | None = None,
                benchmarks: bool = True) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    titles = _titles(s)
    rep: dict[str, Any] = {"models": {}}
    state_p = s.data_dir / "state" / "macro_nowcast.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    all_rows: list[dict[str, Any]] = []
    for cc in countries or list(PANELS):
        p = PANELS[cc]
        t0 = time.perf_counter()
        obs = load_obs([p.gdp[0]] + [x for x, _ in p.monthly], s)
        try:
            if history_weeks:
                d0 = as_of - timedelta(weeks=history_weeks)
                dates = [d0 + timedelta(days=i) for i in range((as_of - d0).days + 1)]
                dates = [d for d in dates if d.weekday() == 4] + [as_of]
                rows, _ = nowcast_history(p, obs, sorted(set(dates)), titles, "quarterly", run_id=run_id)
            else:
                m, q = build_dataset(p, obs, as_of)
                fp = fingerprint(m, q)
                if not force and state.get(p.model_id, {}).get("fingerprint") == fp:
                    rep["models"][p.model_id] = {"status": "unchanged (no new data)", "fingerprint": fp}
                    continue
                prev, prev_asof = _prev_rows(s, p.model_id, as_of)
                ds = [prev_asof, as_of] if prev_asof else [as_of]
                rows, _ = nowcast_history(p, obs, ds, titles, "always", run_id=run_id)
                rows = [r for r in rows if r["as_of"] == as_of]
                state[p.model_id] = {"fingerprint": fp, "as_of": as_of.isoformat()}
            all_rows += rows
            latest = {r["target_label"]: round(r["nowcast"], 2) for r in rows if r["as_of"] == max(x["as_of"] for x in rows)} if rows else {}
            rep["models"][p.model_id] = {"status": "ok", "rows": len(rows), "latest": latest,
                                         "seconds": round(time.perf_counter() - t0, 1)}
        except Exception as e:  # noqa: BLE001
            rep["models"][p.model_id] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
            log.warning("nowcast %s failed: %s", cc, e)
    if benchmarks:
        b, brep = fetch_benchmarks(s)
        all_rows += b
        rep["benchmarks"] = brep
    if all_rows:
        rep["rows_written"] = write_table(_frame(all_rows), "macro_nowcasts", mode="upsert", settings=s)
    state_p.parent.mkdir(parents=True, exist_ok=True)
    state_p.write_text(json.dumps(state, indent=1))
    return rep


# ------------------------------------------------------------------------------------------------ official benchmarks


def _q_of(d: date) -> pd.Period:
    return pd.Period(d, "Q")


def parse_gdpnow(content: bytes, now: datetime | None = None) -> list[dict[str, Any]]:
    """Atlanta Fed GDPNow workbook -> rows (TrackingArchives history + CurrentQtrEvolution for the live quarter)."""
    import openpyxl  # noqa: PLC0415

    now = now or datetime.now(UTC)
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    pts: dict[tuple[pd.Period, date], float] = {}
    ws = wb["TrackingArchives"]
    rows = ws.iter_rows(values_only=True)
    hdr = list(next(rows))
    ci = hdr.index("GDP Nowcast")
    for r in rows:
        if not r or not isinstance(r[0], datetime) or not isinstance(r[1], datetime) or r[ci] is None:
            continue
        pts[(_q_of(r[1].date()), r[0].date())] = float(r[ci])
    if "CurrentQtrEvolution" in wb.sheetnames:
        ws = wb["CurrentQtrEvolution"]
        rows = list(ws.iter_rows(values_only=True))
        title_q = None
        for r in rows[1:]:
            for j in range(0, len(r) - 2, 3):
                d, lab, v = r[j], r[j + 1], r[j + 2]
                if isinstance(d, datetime) and isinstance(v, (int, float)):
                    if isinstance(lab, str) and "Initial GDPNow" in lab:
                        import re  # noqa: PLC0415

                        mm = re.search(r"(\d{2}):Q(\d)", lab)
                        if mm:
                            title_q = pd.Period(f"20{mm.group(1)}Q{mm.group(2)}", "Q")
                    q = title_q or _q_of((d.date() - timedelta(days=45)))
                    pts.setdefault((q, d.date()), float(v))
    out = []
    for (q, d), v in sorted(pts.items()):
        out.append({"model_id": "gdpnow_atlanta_fed", "country": "US", "method": "official_benchmark",
                    "target_period": q.start_time.date(), "target_label": _qlabel(q), "as_of": d, "nowcast": v,
                    "lo": None, "hi": None, "units": "q/q % SAAR", "prev_nowcast": None, "news": None,
                    "source": "Federal Reserve Bank of Atlanta, GDPNow (" + GDPNOW_URL + ")", "model_based": False,
                    "label": "Atlanta Fed GDPNow (official, reference)", "computed_at": now, "run_id": None})
    return out


def parse_nyfed(content: bytes, meta_csv: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """NY Fed Staff Nowcast workbook (Forecasts By Quarter) -> rows; 80% bands from the history meta CSV for the
    final nowcast of concluded quarters."""
    import csv  # noqa: PLC0415

    import openpyxl  # noqa: PLC0415

    now = now or datetime.now(UTC)
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = list(wb["Forecasts By Quarter"].iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(rows) if r and isinstance(r[0], str) and r[0].strip().lower() == "forecast date")
    quarters = {j: pd.Period(str(v).replace(":", ""), "Q") for j, v in enumerate(rows[hi]) if j and v}
    bands: dict[str, tuple[float, float, str]] = {}
    if meta_csv:
        for r in csv.DictReader(io.StringIO(meta_csv.lstrip("﻿"))):
            try:
                bands[f"{r['year']}{r['quarter']}"] = (float(r["80_lb"]), float(r["80_ub"]), r["finalNowcastReleaseDate"][:10])
            except (KeyError, ValueError):
                continue
    out = []
    for r in rows[hi + 1:]:
        if not r or not isinstance(r[0], datetime):
            continue
        for j, q in quarters.items():
            v = r[j] if j < len(r) else None
            if not isinstance(v, (int, float)):
                continue
            lab = _qlabel(q)
            b = bands.get(lab)
            lo = hi_ = None
            if b and b[2] == r[0].date().isoformat():
                lo, hi_ = b[0], b[1]
            out.append({"model_id": "nyfed_staff_nowcast", "country": "US", "method": "official_benchmark",
                        "target_period": q.start_time.date(), "target_label": lab, "as_of": r[0].date(),
                        "nowcast": float(v), "lo": lo, "hi": hi_, "units": "q/q % SAAR", "prev_nowcast": None,
                        "news": None, "source": "Federal Reserve Bank of New York, Staff Nowcast (" + NYFED_URL + ")",
                        "model_based": False, "label": "New York Fed Staff Nowcast (official, reference)",
                        "computed_at": now, "run_id": None})
    return out


def fetch_benchmarks(settings: Settings | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.core.http import HttpClient  # noqa: PLC0415
    from app.core.ratelimit import RateLimit  # noqa: PLC0415

    s = settings or get_settings()
    rep: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    raw = s.raw_dir / "macro_v2" / "nowcast_benchmarks"
    raw.mkdir(parents=True, exist_ok=True)
    with HttpClient("atlantafed", settings=s, headers=UA, rate_limit=RateLimit(0.5, 1)) as h:
        try:
            b = h.get(GDPNOW_URL, cache_ttl=6 * 3600).content
            (raw / "gdpnow.xlsx").write_bytes(b)
            r = parse_gdpnow(b)
            rows += r
            rep["gdpnow_atlanta_fed"] = {"status": "ok", "rows": len(r), "last": r[-1]["as_of"].isoformat() if r else None}
        except Exception as e:  # noqa: BLE001
            rep["gdpnow_atlanta_fed"] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    with HttpClient("newyorkfed", settings=s, headers=UA, rate_limit=RateLimit(0.5, 1)) as h:
        try:
            b = h.get(NYFED_URL, cache_ttl=6 * 3600).content
            (raw / "nyfed_staff_nowcast.xlsx").write_bytes(b)
            try:
                meta = h.get(NYFED_META_URL, cache_ttl=6 * 3600).text
            except Exception:  # noqa: BLE001
                meta = None
            r = parse_nyfed(b, meta)
            rows += r
            rep["nyfed_staff_nowcast"] = {"status": "ok", "rows": len(r), "last": max(x["as_of"] for x in r).isoformat() if r else None}
        except Exception as e:  # noqa: BLE001
            rep["nyfed_staff_nowcast"] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    return rows, rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_nowcast. params: countries (list|csv), history_weeks (int, backfill), force (bool)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    cs = p.get("countries")
    if isinstance(cs, str):
        cs = [c.strip().upper() for c in cs.split(",") if c.strip()]
    rep = run_nowcast(ctx.settings, cs, history_weeks=int(p.get("history_weeks", 0) or 0),
                      force=str(p.get("force", "")).lower() in ("1", "true", "yes"), run_id=ctx.run_id)
    bad = [k for k, v in rep["models"].items() if v.get("status") == "failed"]
    return JobResult(status="partial" if bad else "success", rows_written={"macro_nowcasts": rep.get("rows_written", 0)},
                     entities_ok=len(rep["models"]) - len(bad), entities_failed=len(bad),
                     message=json.dumps(rep, default=str)[:4000])
