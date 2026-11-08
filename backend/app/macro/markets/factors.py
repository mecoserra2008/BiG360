"""W4.1 macro factor set (job `macro_factors`, nightly) -> table macro_factors.

Inputs are OFFICIAL series already in the lake (latest vintage per observation date):
  d2y / d10y        fred:DGS2 / fred:DGS10        Treasury constant-maturity yields (Fed H.15)         change, pp
  dcurve            fred:T10Y2Y                   10y - 2y                                             change, pp
  dreal10           fred:DFII10                   10y TIPS real yield (H.15)                           change, pp
  dbe10             fred:T10YIE                   10y breakeven inflation (FRB St. Louis from H.15)    change, pp
  dcredit           fred:BAA10Y                   Moody's Baa - 10y Treasury (long history; the ICE BofA
                                                  OAS series on FRED only keep 3 years)                change, pp
  usd               fred:DTWEXBGS                 Fed nominal broad dollar index (2006+)               % change
  oil               fred:DCOILWTICO               WTI spot (EIA)                                       % change
  commodities       fred:PALLFNFINDEXM            IMF all-commodity price index (monthly only)        % change
  copper            fred:PCOPPUSDM                IMF copper price (monthly only)                      % change
  cash              fred:DGS3MO                   3m bill yield / periods per year (cash return)       frac
  equity            price lake IDX:^GSPC          S&P 500 return (market data, not macro)              frac
  surp_<theme>_<CC> macro_surprise_index (W2)     change of the surprise index / 100 (model-based)     idx100
States (monthly, `state` column): cycle_quadrant_US / _GLOBAL (W1 macro_cycle; GLOBAL = quadrant of the mean of the
US, EA, JP, GB, CN momenta), hmm_regime_US / _GLOBAL (macro_regimes, model-based), rate_cycle_<bank> (macro-w3
cb_rate_cycle when it exists, else the provisional rule below on BIS official policy rates), inflation_regime_US
(CPI-U y/y buckets).

Provisional rate-cycle rule (labelled model_based): monthly BIS policy rate r_m; d = r_m - r_{m-6};
hiking if d > +0.10pp, cutting if d < -0.10pp, else pausing.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table

log = get_logger(__name__)

START = date(1990, 1, 1)
MARKET_ID = "IDX:^GSPC"

#: factor_id -> (series_id, transform, unit, frequencies, label)
SERIES_FACTORS: dict[str, tuple[str, str, str, str, str]] = {
    "d2y": ("fred:DGS2", "diff", "pp", "DWM", "2y Treasury yield change"),
    "d10y": ("fred:DGS10", "diff", "pp", "DWM", "10y Treasury yield change"),
    "dcurve": ("fred:T10Y2Y", "diff", "pp", "DWM", "10y-2y curve change"),
    "dreal10": ("fred:DFII10", "diff", "pp", "DWM", "10y real (TIPS) yield change"),
    "dbe10": ("fred:T10YIE", "diff", "pp", "DWM", "10y breakeven inflation change"),
    "dcredit": ("fred:BAA10Y", "diff", "pp", "DWM", "Baa-Treasury credit spread change"),
    "usd": ("fred:DTWEXBGS", "pct", "frac", "DWM", "Broad trade-weighted USD (Fed) % change"),
    "oil": ("fred:DCOILWTICO", "pct", "frac", "DWM", "WTI crude oil % change"),
    "commodities": ("fred:PALLFNFINDEXM", "pct", "frac", "M", "IMF all-commodity index % change"),
    "copper": ("fred:PCOPPUSDM", "pct", "frac", "M", "IMF copper price % change"),
}
CASH_SERIES = "fred:DGS3MO"
CPI_SERIES = "fred:CPIAUCSL"
FACTOR_LABELS = {k: v[4] for k, v in SERIES_FACTORS.items()} | {
    "equity": "S&P 500 return (price lake)", "equity_resid": "Equity return not explained by the macro block",
    "cash": "3m T-bill cash return", "surp_growth": "Growth surprise index change", "surp_inflation":
    "Inflation surprise index change", "surp_labor": "Labor surprise index change"}
SURPRISE_THEMES = ("growth", "inflation", "labor")
FFILL = {"D": 0, "W": 2, "M": 1}
PERIODS_PER_YEAR = {"D": 252, "W": 52, "M": 12}
GLOBAL_CYCLE = ("US", "EA", "JP", "GB", "CN")
#: BIS policy-rate area -> central bank id (provisional rate cycle; ids aligned with macro-w3 bank ids)
BIS_BANKS = {"US": "fed", "XM": "ecb", "GB": "boe", "JP": "boj", "CH": "snb", "CA": "boc", "AU": "rba", "KR": "bok",
             "CN": "pboc", "BR": "bcb", "MX": "banxico", "IN": "rbi", "CL": "bcch", "CO": "banrep", "PE": "bcrp",
             "HK": "hkma"}
RATE_CYCLE_THRESHOLD = 0.10
RATE_CYCLE_MONTHS = 6
INFLATION_BUCKETS = ((2.0, "below_2"), (3.0, "2_to_3"), (5.0, "3_to_5"), (float("inf"), "above_5"))

SCHEMA: dict[str, Any] = {"frequency": pl.Utf8, "date": pl.Date, "factor_id": pl.Utf8, "value": pl.Float64,
                          "level": pl.Float64, "state": pl.Utf8, "unit": pl.Utf8, "source": pl.Utf8,
                          "model_based": pl.Boolean, "computed_at": pl.Datetime("us", "UTC")}


# ------------------------------------------------------------------------------------------------ helpers


def period_key(col: str, freq: str) -> pl.Expr:
    if freq == "W":
        return pl.col(col).dt.truncate("1w")
    if freq == "M":
        return pl.col(col).dt.truncate("1mo")
    return pl.col(col)


def latest_vintage(series_ids: list[str], settings: Settings, as_of: date | None = None) -> pl.DataFrame:
    """[series_id, date, value]: the latest-known value per observation date (knowledge_date <= as_of if given)."""
    lf = scan_table("macro_observations", settings).filter(pl.col("series_id").is_in(series_ids))
    if as_of is not None:
        lf = lf.filter(pl.col("knowledge_date") <= as_of)
    df = lf.select("series_id", "date", "value", "knowledge_date").drop_nulls("value").collect()
    if df.is_empty():
        return df.select("series_id", "date", "value")
    return (df.sort("series_id", "date", "knowledge_date").group_by("series_id", "date", maintain_order=True)
            .agg(pl.col("value").last()).filter(pl.col("value").is_finite()))


def grid(freq: str, start: date, end: date) -> pl.Series:
    if freq == "W":
        s = start - timedelta(days=start.weekday())
        return pl.date_range(s, end, "1w", eager=True).alias("date")
    if freq == "M":
        return pl.date_range(date(start.year, start.month, 1), end, "1mo", eager=True).alias("date")
    raise ValueError(freq)


def levels_on_grid(obs: pl.DataFrame, freq: str, end: date) -> pl.DataFrame:
    """obs [date, value] -> [date (period key), level] (last observation of the period, carried forward FFILL[freq]
    periods across gaps such as holidays). freq D returns the observations themselves."""
    if obs.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "level": pl.Float64, "has_obs": pl.Boolean})
    o = obs.sort("date")
    if freq == "D":
        return o.select("date", pl.col("value").alias("level"), pl.lit(True).alias("has_obs"))
    per = o.with_columns(period_key("date", freq).alias("p")).group_by("p").agg(pl.col("value").last().alias("level"))
    g = grid(freq, o["date"].min(), end).to_frame()
    out = g.join(per.rename({"p": "date"}), on="date", how="left").sort("date")
    return out.with_columns(pl.col("level").is_not_null().alias("has_obs"),
                            pl.col("level").forward_fill(limit=FFILL[freq]))


def changes(lv: pl.DataFrame, kind: str) -> pl.DataFrame:
    prev = pl.col("level").shift(1)
    if kind == "diff":
        v = pl.col("level") - prev
    else:
        v = pl.when(prev > 0).then(pl.col("level") / prev - 1.0).otherwise(None)
    # a period without its own observation (holiday week, data not yet published) has no change of its own
    return lv.with_columns(pl.when(pl.col("has_obs")).then(v).otherwise(None).alias("value"))


def _rows(df: pl.DataFrame, freq: str, fid: str, unit: str, source: str, model_based: bool) -> pl.DataFrame:
    return df.select(pl.lit(freq).alias("frequency"), pl.col("date").cast(pl.Date), pl.lit(fid).alias("factor_id"),
                     pl.col("value").cast(pl.Float64), pl.col("level").cast(pl.Float64),
                     pl.lit(None, dtype=pl.Utf8).alias("state"), pl.lit(unit).alias("unit"),
                     pl.lit(source).alias("source"), pl.lit(model_based).alias("model_based"))


def _state_rows(df: pl.DataFrame, fid: str, source: str, model_based: bool) -> pl.DataFrame:
    """df [date (month start), state]."""
    return df.select(pl.lit("M").alias("frequency"), pl.col("date").cast(pl.Date), pl.lit(fid).alias("factor_id"),
                     pl.lit(None, dtype=pl.Float64).alias("value"), pl.lit(None, dtype=pl.Float64).alias("level"),
                     pl.col("state").cast(pl.Utf8), pl.lit("state").alias("unit"), pl.lit(source).alias("source"),
                     pl.lit(model_based).alias("model_based"))


# ------------------------------------------------------------------------------------------------ numeric factors


def equity_levels(settings: Settings, security_id: str = MARKET_ID) -> pl.DataFrame:
    return (scan_table("prices_daily", settings).filter(pl.col("security_id") == security_id)
            .select("date", pl.col("adj_close").alias("value")).drop_nulls().sort("date").collect())


def numeric_factors(settings: Settings, end: date, as_of: date | None = None) -> pl.DataFrame:
    sids = [v[0] for v in SERIES_FACTORS.values()] + [CASH_SERIES]
    obs = latest_vintage(sids, settings, as_of).filter(pl.col("date") <= end)
    parts: list[pl.DataFrame] = []
    for fid, (sid, kind, unit, freqs, _) in SERIES_FACTORS.items():
        o = obs.filter(pl.col("series_id") == sid).select("date", "value")
        if o.is_empty():
            log.warning("macro_factors: no observations for %s", sid)
            continue
        for f in freqs:
            lv = changes(levels_on_grid(o, f, end), kind).filter(pl.col("date") >= START)
            parts.append(_rows(lv, f, fid, unit, sid, False))
    cash = obs.filter(pl.col("series_id") == CASH_SERIES).select("date", "value")
    for f in "DWM":
        lv = levels_on_grid(cash, f, end).filter(pl.col("date") >= START)
        lv = lv.with_columns((pl.col("level").shift(1) / 100.0 / PERIODS_PER_YEAR[f]).alias("value"))
        parts.append(_rows(lv, f, "cash", "frac", CASH_SERIES, False))
    eq = equity_levels(settings).filter(pl.col("date") <= end)
    for f in "DWM":
        lv = changes(levels_on_grid(eq, f, end), "pct").filter(pl.col("date") >= START)
        parts.append(_rows(lv, f, "equity", "frac", f"price lake {MARKET_ID}", False))
    return pl.concat(parts)


def surprise_factors(settings: Settings, end: date) -> pl.DataFrame:
    try:
        si = (scan_table("macro_surprise_index", settings).filter(pl.col("theme").is_in(list(SURPRISE_THEMES)))
              .select("country", "theme", "date", "value").drop_nulls("value").collect())
    except Exception as e:  # noqa: BLE001 - table not built yet
        log.info("macro_factors: surprise index unavailable: %s", e)
        return pl.DataFrame(schema={k: v for k, v in SCHEMA.items() if k != "computed_at"})
    parts: list[pl.DataFrame] = []
    for (cc, th), g in si.group_by("country", "theme"):
        o = g.select("date", "value").filter(pl.col("date") <= end)
        for f in "DWM":
            lv = levels_on_grid(o, f, end).with_columns(
                pl.when(pl.col("has_obs")).then((pl.col("level") - pl.col("level").shift(1)) / 100.0).alias("value"))
            parts.append(_rows(lv.drop_nulls("level"), f, f"surp_{th}_{cc}", "idx100", "macro_surprise_index", True))
    return pl.concat(parts) if parts else pl.DataFrame(schema={k: v for k, v in SCHEMA.items() if k != "computed_at"})


# ------------------------------------------------------------------------------------------------ states


def quadrant(g: float | None, i: float | None) -> str | None:
    from app.macro.cycle import quadrant as _q  # noqa: PLC0415

    return _q(g, i)


def cycle_states(settings: Settings) -> pl.DataFrame:
    """[date, scope, state] monthly quadrants for US and GLOBAL."""
    try:
        cy = read_table("macro_cycle", columns=["country", "date", "growth_momentum", "inflation_momentum", "quadrant"],
                        settings=settings)
    except Exception as e:  # noqa: BLE001
        log.info("macro_factors: macro_cycle unavailable: %s", e)
        return pl.DataFrame(schema={"date": pl.Date, "scope": pl.Utf8, "state": pl.Utf8})
    us = cy.filter((pl.col("country") == "US") & pl.col("quadrant").is_not_null()).select(
        "date", pl.lit("US").alias("scope"), pl.col("quadrant").alias("state"))
    gl = (cy.filter(pl.col("country").is_in(list(GLOBAL_CYCLE))).group_by("date")
          .agg(pl.col("growth_momentum").mean().alias("g"), pl.col("inflation_momentum").mean().alias("i"),
               pl.col("country").n_unique().alias("n"))
          .filter(pl.col("n") >= 3).sort("date"))
    gl = gl.with_columns(pl.struct("g", "i").map_elements(lambda r: quadrant(r["g"], r["i"]), return_dtype=pl.Utf8)
                         .alias("state")).select("date", pl.lit("GLOBAL").alias("scope"), "state")
    return pl.concat([us, gl]).drop_nulls("state").sort("scope", "date")


def hmm_states(settings: Settings) -> pl.DataFrame:
    try:
        rg = (scan_table("macro_regimes", settings).filter(pl.col("model_id").is_in(["hmm4_us_v1", "hmm4_global_v1"]))
              .select("model_id", "date", "regime_label").collect())
    except Exception as e:  # noqa: BLE001
        log.info("macro_factors: macro_regimes unavailable: %s", e)
        return pl.DataFrame(schema={"date": pl.Date, "scope": pl.Utf8, "state": pl.Utf8})
    rg = rg.with_columns(pl.col("date").dt.truncate("1mo").alias("m")).sort("model_id", "date")
    out = rg.group_by("model_id", "m", maintain_order=True).agg(pl.col("regime_label").last().alias("state"))
    return out.select(pl.col("m").alias("date"),
                      pl.when(pl.col("model_id") == "hmm4_us_v1").then(pl.lit("US")).otherwise(pl.lit("GLOBAL"))
                      .alias("scope"), "state").sort("scope", "date")


def _w3_rate_cycle(settings: Settings) -> pl.DataFrame | None:
    """macro-w3's cb_rate_cycle (bank, date, phase) when it exists: contract table first, then the agreed file path."""
    df: pl.DataFrame | None = None
    try:
        from app.core.schema import get_contract  # noqa: PLC0415

        get_contract("cb_rate_cycle")
        df = read_table("cb_rate_cycle", settings=settings)
    except Exception:  # noqa: BLE001 - not contracted / not built yet
        for p in (settings.data_dir / "lake" / "macro" / "cb" / "cb_rate_cycle.parquet",
                  settings.data_dir / "scores" / "cb_rate_cycle"):
            if Path(p).exists():
                try:
                    df = pl.read_parquet(p if Path(p).is_file() else str(Path(p) / "**" / "*.parquet"))
                    break
                except Exception:  # noqa: BLE001
                    df = None
    if df is None or df.is_empty() or not {"bank", "date", "phase"} <= set(df.columns):
        return None
    df = df.select(pl.col("bank").cast(pl.Utf8).str.to_lowercase(), pl.col("date").cast(pl.Date),
                   pl.col("phase").cast(pl.Utf8).str.to_lowercase()).drop_nulls()
    df = df.with_columns(pl.col("date").dt.truncate("1mo").alias("m")).sort("bank", "date")
    return (df.group_by("bank", "m", maintain_order=True).agg(pl.col("phase").last().alias("state"))
            .select(pl.col("m").alias("date"), pl.col("bank").alias("scope"), "state"))


def provisional_rate_cycle(rates: pl.DataFrame) -> pl.DataFrame:
    """rates [date (month), level] -> [date, state]: 6-month change rule on the official policy rate."""
    r = rates.sort("date").with_columns((pl.col("level") - pl.col("level").shift(RATE_CYCLE_MONTHS)).alias("d"))
    st = (pl.when(pl.col("d") > RATE_CYCLE_THRESHOLD).then(pl.lit("hiking"))
          .when(pl.col("d") < -RATE_CYCLE_THRESHOLD).then(pl.lit("cutting")).otherwise(pl.lit("pausing")))
    return r.drop_nulls("d").select("date", st.alias("state"))


def rate_cycle_states(settings: Settings, end: date) -> tuple[pl.DataFrame, str]:
    """[date, scope (bank), state], source label. Uses macro-w3 cb_rate_cycle when present."""
    w3 = _w3_rate_cycle(settings)
    if w3 is not None and w3.height:
        return w3.sort("scope", "date"), "cb_rate_cycle"
    obs = latest_vintage([f"bis:WS_CBPOL.M.{a}" for a in BIS_BANKS], settings).filter(pl.col("date") <= end)
    parts = []
    for area, bank in BIS_BANKS.items():
        o = obs.filter(pl.col("series_id") == f"bis:WS_CBPOL.M.{area}").select("date", "value")
        if o.height < RATE_CYCLE_MONTHS + 1:
            continue
        lv = levels_on_grid(o, "M", end)
        parts.append(provisional_rate_cycle(lv.drop_nulls("level")).with_columns(pl.lit(bank).alias("scope")))
    if not parts:
        return pl.DataFrame(schema={"date": pl.Date, "scope": pl.Utf8, "state": pl.Utf8}), "none"
    return pl.concat(parts).select("date", "scope", "state").sort("scope", "date"), "provisional_bis"


def inflation_bucket(yoy: float | None) -> str | None:
    if yoy is None or not np.isfinite(yoy):
        return None
    for hi, lab in INFLATION_BUCKETS:
        if yoy < hi:
            return lab
    return None


def inflation_states(settings: Settings, end: date) -> pl.DataFrame:
    o = latest_vintage([CPI_SERIES], settings).filter(pl.col("date") <= end).select("date", "value").sort("date")
    if o.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "scope": pl.Utf8, "state": pl.Utf8})
    lv = levels_on_grid(o, "M", end).with_columns(
        ((pl.col("level") / pl.col("level").shift(12) - 1.0) * 100.0).alias("yoy")).drop_nulls("yoy")
    return lv.select("date", pl.lit("US").alias("scope"),
                     pl.col("yoy").map_elements(inflation_bucket, return_dtype=pl.Utf8).alias("state"))


def monthly_states(settings: Settings, end: date) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Long [regime_type, scope, date, state, model_based] + info."""
    rc, rc_src = rate_cycle_states(settings, end)
    parts = [cycle_states(settings).with_columns(pl.lit("cycle_quadrant").alias("regime_type"),
                                                 pl.lit(False).alias("model_based")),
             hmm_states(settings).with_columns(pl.lit("hmm_regime").alias("regime_type"), pl.lit(True).alias("model_based")),
             rc.with_columns(pl.lit("rate_cycle").alias("regime_type"), pl.lit(rc_src != "cb_rate_cycle").alias("model_based")),
             inflation_states(settings, end).with_columns(pl.lit("inflation_regime").alias("regime_type"),
                                                          pl.lit(False).alias("model_based"))]
    out = pl.concat([p.select("regime_type", "scope", pl.col("date").cast(pl.Date), "state", "model_based") for p in parts])
    out = out.filter((pl.col("date") >= START) & (pl.col("date") <= end)).drop_nulls("state")
    return out, {"rate_cycle_source": rc_src}


STATE_FID = {"cycle_quadrant": "cycle_quadrant_{}", "hmm_regime": "hmm_regime_{}", "rate_cycle": "rate_cycle_{}",
             "inflation_regime": "inflation_regime_{}"}
STATE_SOURCE = {"cycle_quadrant": "macro_cycle", "hmm_regime": "macro_regimes", "inflation_regime": CPI_SERIES}


# ------------------------------------------------------------------------------------------------ build / job


def build_factors(settings: Settings, end: date) -> tuple[pl.DataFrame, dict[str, Any]]:
    now = datetime.now(UTC)
    num = numeric_factors(settings, end)
    sur = surprise_factors(settings, end)
    states, info = monthly_states(settings, end)
    st_rows = []
    for (rt, scope, mb), g in states.group_by("regime_type", "scope", "model_based"):
        src = STATE_SOURCE.get(rt) or (info["rate_cycle_source"] if info["rate_cycle_source"] == "cb_rate_cycle"
                                       else f"bis:WS_CBPOL.M (provisional rule, {scope})")
        st_rows.append(_state_rows(g.select("date", "state"), STATE_FID[rt].format(scope), src, bool(mb)))
    out = pl.concat([num, sur] + st_rows, how="vertical_relaxed")
    out = out.filter(pl.col("value").is_not_null() | pl.col("state").is_not_null())
    out = out.with_columns(pl.col("value").fill_nan(None), pl.lit(now).alias("computed_at")).unique(
        ["frequency", "date", "factor_id"], keep="last")
    return out.select(list(SCHEMA)).cast(SCHEMA), info  # type: ignore[arg-type]


def load_factors(settings: Settings, freq: str) -> pl.DataFrame:
    return scan_table("macro_factors", settings).filter(pl.col("frequency") == freq).collect()


def wide(fac: pl.DataFrame, freq: str, factor_ids: list[str]) -> pl.DataFrame:
    """Long macro_factors rows -> [date, f1, f2, ...] for numeric factors (missing ones absent)."""
    f = fac.filter((pl.col("frequency") == freq) & pl.col("factor_id").is_in(factor_ids) & pl.col("value").is_not_null())
    if f.is_empty():
        return pl.DataFrame(schema={"date": pl.Date})
    return f.pivot(on="factor_id", index="date", values="value", aggregate_function="last").sort("date")


def states_long(fac: pl.DataFrame) -> pl.DataFrame:
    """macro_factors state rows -> [regime_type, scope, date, state, model_based]."""
    s = fac.filter((pl.col("frequency") == "M") & (pl.col("unit") == "state"))
    rt = (pl.when(pl.col("factor_id").str.starts_with("cycle_quadrant_")).then(pl.lit("cycle_quadrant"))
          .when(pl.col("factor_id").str.starts_with("hmm_regime_")).then(pl.lit("hmm_regime"))
          .when(pl.col("factor_id").str.starts_with("rate_cycle_")).then(pl.lit("rate_cycle"))
          .otherwise(pl.lit("inflation_regime")))
    s = s.with_columns(rt.alias("regime_type"))
    scope = pl.col("factor_id").str.replace(r"^(cycle_quadrant|hmm_regime|rate_cycle|inflation_regime)_", "")
    return s.select("regime_type", scope.alias("scope"), "date", "state", "model_based")


def _update_state(settings: Settings, **kv: Any) -> None:
    p = settings.data_dir / "state" / "macro_w4.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        st = {}
    st.update(kv)
    p.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")


def run_factors(settings: Settings | None = None, end: date | None = None, write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    end = end or datetime.now(UTC).date()
    t0 = time.perf_counter()
    out, info = build_factors(s, end)
    rep: dict[str, Any] = {"rows": out.height, "factors": out["factor_id"].n_unique(), **info}
    if write:
        rep["rows_written"] = write_table(out, "macro_factors", mode="overwrite", settings=s)
        _update_state(s, rate_cycle_source=("cb_rate_cycle (macro-w3)" if info["rate_cycle_source"] == "cb_rate_cycle"
                                            else "provisional: BIS official policy rates, 6m change rule "
                                                 "(switches to macro-w3 cb_rate_cycle automatically when present)"))
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_factors (nightly, after macro_fred / macro_global / macro_cycle / macro_surprise / prices)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_factors(ctx.settings, getattr(ctx, "as_of", None))
    log.info("macro_factors: %s", rep)
    return JobResult(status="success" if rep["rows"] else "failed",
                     rows_written={"macro_factors": rep.get("rows_written", 0)}, entities_ok=rep["factors"],
                     watermark=datetime.now(UTC).date().isoformat(), message=json.dumps(rep, default=str)[:2000])
