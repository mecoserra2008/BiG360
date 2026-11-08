"""Generic fundamental ratios + accounting-quality scores -> `ratios`; drives `sector_kpis` too (owner: analytics-scoring).

Job id `ratios_kpis` (module app.analytics.ratios) writes BOTH `ratios` and `sector_kpis` (see app.analytics.sector_kpis).

Registry: contracts/sector_kpis.yaml (generic_metrics, accounting_scores, kpi_sets). Formulas are evaluated with the
published `app.normalize.formula.evaluate` (vectorized; panel functions lag/avg/growth_yoy/cagr/stdev_fy/mean_fy).

Point-in-time ("vintage panels")
--------------------------------
fundamentals_wide holds one row per (issuer, period_end, period_kind, basis, knowledge_date). For each issuer and each
distinct knowledge date K ("vintage") we rebuild the panel as known at K (latest version per period_end with
knowledge_date <= K) and evaluate all formulas on it, so lag()/avg()/growth use only values public at K (incl.
restated comparatives filed at K). Every period re-evaluated in vintage K is emitted with knowledge_date = K; rows
whose value did not change versus the previous vintage are dropped (dedupe), so the table stays compact and
`pit.asof(ratios, D, [issuer_id, metric_id, period_end, period_kind])` returns exactly the value known at D.

Bases: ratios are computed on the LOCAL (reporting-currency) basis - currency-invariant and free of FX noise in
growth rates. Metrics with unit currency_usd (nopat, revenue_per_employee) are evaluated on the USD basis; formulas
that use one of them (roic = nopat / avg(invested_capital)) read its LOCAL-basis twin `<id>__loc` (before 2026-10-27
roic divided USD NOPAT by local-currency invested capital: JP ROIC ~0.04%, EUR / GBP reporters off by the FX rate).
Plausibility bounds (SANITY_BOUNDS): roic / croic are nulled and flagged (`<id>_flag` = 1, raw value in `<id>_raw`)
when |value| > 200% or the average invested capital is below 5% of total assets (net-of-cash invested capital of
cash-rich / customer-float businesses, e.g. EXPE 1,498%, NTAP 357%).
Periods: TTM (quarterly, primary for scoring) and FY (accounting scores are textbook FY-based; also produced on TTM).
Low-dq inputs: `dq_score` (fundamentals_wide row dq) is carried on every row (additive column, v2) and
`inputs_complete` flags formulas with missing inputs.

Accounting scores (non-financial issuers, sector != FINS):
    piotroski_f   9 binary signals (sector_kpis.yaml); >= 7 evaluable required, scaled to 9 when 1-2 are missing
    altman_z2     Altman Z'' (book version): 6.56 WC/TA + 3.26 RE/TA + 6.72 EBIT/TA + 1.05 BE/TL
    altman_z      original Z for manufacturers (groups industrial_mfg/autos/chemicals/hardware/semis), mcap at K
    beneish_m     8-variable M-score; missing index -> 1.0, tata -> 0; > 2 missing -> null (app.analytics.beneish;
                  also emits the eight indices beneish_<index> and the recalibrated model-based screen
                  beneish_m_growth_adj / beneish_red_flags / beneish_flag_adj as extra ratio rows, never scored)
    sloan_accruals (NI - CFO) / avg TA
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import polars as pl
import yaml

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table
from app.normalize import formula as F

log = get_logger(__name__)

KINDS = ("TTM", "FY")
PANEL = ("issuer_id", "vintage")
WINDOW_YEARS = 7            # lag depth needed by the deepest formula (mean_fy(roic,5) with avg -> 6y) + margin
ALTMAN_Z_GROUPS = {"industrial_mfg", "autos", "chemicals", "hardware", "semis"}
RATIO_UNITS = {"ratio", "pct", "x", "days", "currency_usd", "score", "count", "years"}

PIOTROSKI = {
    "f_roa_pos": "div(net_income, coalesce(lag(total_assets, 1), total_assets)) > 0",
    "f_cfo_pos": "cfo > 0",
    "f_droa": "roa > lag(roa, 1)",
    "f_accrual": "cfo > net_income",
    "f_dleverage": "div(nz(long_term_debt), avg(total_assets)) < lag(div(nz(long_term_debt), avg(total_assets)), 1)",
    "f_dliquidity": "current_ratio > lag(current_ratio, 1)",
    "f_no_dilution": "shares_outstanding <= lag(shares_outstanding, 1) * 1.005",
    "f_dmargin": "gross_margin > lag(gross_margin, 1)",
    "f_dturnover": "asset_turnover > lag(asset_turnover, 1)",
}
BENEISH_W = {"dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892, "depi": 0.115, "sgai": -0.172,
             "tata": 4.679, "lvgi": -0.327}
BENEISH_CONST = -4.84


# ------------------------------------------------------------------------------------------------ registry


@lru_cache(maxsize=4)
def _registry_cached(path: str) -> dict[str, Any]:
    doc = yaml.safe_load(open(path, encoding="utf-8").read())
    generic = list(doc.get("generic_metrics") or [])
    acc = list(doc.get("accounting_scores") or [])
    sets = {ks["id"]: ks for ks in doc.get("kpi_sets") or []}
    sub2set = {sub: ks["id"] for ks in sets.values() for sub in ks.get("subsectors") or []}
    meta: dict[str, dict[str, Any]] = {}
    for m in generic:
        meta[m["id"]] = {"source": "ratios", "unit": m.get("unit", "ratio"), "hib": m.get("higher_is_better"),
                         "target": m.get("target"), "score": m.get("score", True), "formula": m.get("formula")}
    for m in acc:
        meta[m["id"]] = {"source": "ratios", "unit": m.get("unit", "score"), "hib": m.get("higher_is_better"),
                         "target": m.get("target"), "score": True, "applies_to": m.get("applies_to")}
    for ks in sets.values():
        for k in ks.get("kpis") or []:
            meta.setdefault(k["id"], {"source": "sector_kpis", "unit": k.get("unit", "ratio"),
                                      "hib": k.get("higher_is_better"), "target": k.get("target"), "score": True,
                                      "label": k.get("label")})
    order = topo_order({m["id"]: m["formula"] for m in generic})
    return {"doc": doc, "generic": {m["id"]: m for m in generic}, "generic_order": order, "accounting": acc,
            "kpi_sets": sets, "sub2set": sub2set, "meta": meta}


def registry(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return _registry_cached(str(s.contracts_dir / "sector_kpis.yaml"))


def topo_order(formulas: dict[str, str]) -> list[str]:
    """Dependency order of metric formulas that reference other metric ids."""
    deps = {k: {d for d in F.dependencies(v) if d in formulas and d != k} for k, v in formulas.items()}
    out: list[str] = []
    state: dict[str, int] = {}

    def visit(k: str) -> None:
        if state.get(k) == 2:
            return
        if state.get(k) == 1:
            raise ValueError(f"cycle in metric formulas at {k}")
        state[k] = 1
        for d in sorted(deps[k]):
            visit(d)
        state[k] = 2
        out.append(k)

    for k in formulas:
        visit(k)
    return out


def issuer_meta(issuer_ids: Sequence[str] | None = None, settings: Settings | None = None) -> pl.DataFrame:
    """issuer_id, sector_id, subsector_id, region, kpi_set_id, groups (list), is_financial."""
    from app.normalize.taxonomy import subsector_groups

    reg = registry(settings)
    lf = scan_table("issuers", settings).select("issuer_id", "sector_id", "subsector_id", "region", "country",
                                                "reporting_currency")
    if issuer_ids is not None:
        lf = lf.filter(pl.col("issuer_id").is_in(list(issuer_ids)))
    df = lf.collect()
    subs = df["subsector_id"].drop_nulls().unique().to_list()
    gmap = {s: subsector_groups(s) for s in subs}
    return df.with_columns(
        pl.col("subsector_id").replace_strict(reg["sub2set"], default=None, return_dtype=pl.Utf8).alias("kpi_set_id"),
        pl.col("subsector_id").replace_strict(gmap, default=[], return_dtype=pl.List(pl.Utf8)).alias("groups"),
        (pl.col("sector_id") == "FINS").fill_null(False).alias("is_financial"),
    )


# ------------------------------------------------------------------------------------------------ vintage panels


def load_wide(issuer_ids: Sequence[str], kind: str, settings: Settings | None = None) -> pl.DataFrame:
    """LOCAL rows of one period kind + `<item>__usd` copies of a few USD items needed by currency metrics."""
    lf = scan_table("fundamentals_wide", settings).filter(pl.col("issuer_id").is_in(list(issuer_ids)),
                                                          pl.col("period_kind") == kind)
    loc = lf.filter(pl.col("currency_basis") == "LOCAL").collect()
    usd_items = [c for c in ("ebit", "revenue", "total_liabilities", "employees") if c in loc.columns]
    usd = (lf.filter(pl.col("currency_basis") == "USD")
           .select("issuer_id", "period_end", "knowledge_date", *[pl.col(c).alias(c + "__usd") for c in usd_items])
           .collect().unique(subset=["issuer_id", "period_end", "knowledge_date"], keep="last"))
    loc = loc.unique(subset=["issuer_id", "period_end", "knowledge_date"], keep="last")
    out = loc.join(usd, on=["issuer_id", "period_end", "knowledge_date"], how="left")
    return _employees_fallback(out, settings)


EMPLOYEES_FALLBACK_DAYS = 400


def _employees_fallback(frame: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    """Employee counts are rarely tagged in the statements (fundamentals_wide.employees: < 1 % of rows), so
    revenue_per_employee was empty for every issuer. The issuer profile count (issuers.employees, SEC / ESEF /
    Yahoo profile, current) fills the periods of the latest ~13 months only (completeness audit 2026-10-27)."""
    if frame.is_empty() or "employees" not in frame.columns:
        return frame
    try:
        emp = (scan_table("issuers", settings).filter(pl.col("employees").is_not_null() & (pl.col("employees") > 0))
               .select("issuer_id", pl.col("employees").cast(pl.Float64).alias("__emp")).unique("issuer_id").collect())
    except Exception as e:  # noqa: BLE001 - issuers optional in tests
        log.debug("issuer employees unavailable: %s", e)
        return frame
    if emp.is_empty():
        return frame
    last = pl.col("period_end").max().over("issuer_id")
    recent = pl.col("period_end") >= last - timedelta(days=EMPLOYEES_FALLBACK_DAYS)
    f = frame.join(emp, on="issuer_id", how="left")
    fill = [pl.when(pl.col("employees").is_null() & recent).then(pl.col("__emp"))
            .otherwise(pl.col("employees").cast(pl.Float64)).alias("employees")]
    if "employees__usd" in f.columns:
        fill.append(pl.when(pl.col("employees__usd").is_null() & recent).then(pl.col("__emp"))
                    .otherwise(pl.col("employees__usd").cast(pl.Float64)).alias("employees__usd"))
    return f.with_columns(fill).drop("__emp")


def vintage_expand(rows: pl.DataFrame, window_years: int = WINDOW_YEARS) -> pl.DataFrame:
    """Expand rows (one period kind) into vintage panels.

    Returns rows with extra columns `vintage` (date) and `emit` (bool: period is (re-)evaluated in this vintage);
    one row per (issuer_id, vintage, period_end) = latest version with knowledge_date <= vintage.
    """
    if rows.is_empty():
        return rows.with_columns(pl.lit(None, pl.Date).alias("vintage"), pl.lit(None, pl.Boolean).alias("emit"))
    r = rows.with_row_index("__rid")
    keys = r.select("__rid", "issuer_id", "period_end", "knowledge_date")
    tgt = (keys.group_by("issuer_id", "knowledge_date").agg(pl.col("period_end").min().alias("__min_pe"))
           .rename({"knowledge_date": "vintage"}))
    x = (keys.join(tgt, on="issuer_id", how="inner")
         .filter(pl.col("knowledge_date") <= pl.col("vintage"),
                 pl.col("period_end") >= pl.col("__min_pe").dt.offset_by(f"-{window_years}y")))
    x = (x.sort("issuer_id", "vintage", "period_end", "knowledge_date")
         .unique(subset=["issuer_id", "vintage", "period_end"], keep="last", maintain_order=True)
         .with_columns((pl.col("period_end") >= pl.col("__min_pe")).alias("emit"))
         .select("__rid", "vintage", "emit"))
    return x.join(r, on="__rid", how="left").drop("__rid").sort("issuer_id", "vintage", "period_end")


# ------------------------------------------------------------------------------------------------ evaluation


def _complete(frame: pl.DataFrame, expr: str) -> pl.Expr:
    deps = sorted(F.dependencies(expr))
    if not deps:
        return pl.lit(True)
    return pl.all_horizontal([pl.col(d).is_not_null() if d in frame.columns else pl.lit(False) for d in deps])


def _add_analytic_items(frame: pl.DataFrame) -> pl.DataFrame:
    """Fallback derivations for analytic items that fundamentals_wide may not carry (REIT FFO/AFFO)."""
    def col(c: str) -> pl.Expr:
        return pl.col(c).cast(pl.Float64) if c in frame.columns else pl.lit(None, pl.Float64)

    ni = pl.coalesce(col("net_income_common"), col("net_income_parent"))
    dep = pl.coalesce(col("real_estate_depreciation"), col("depreciation_amortization"))
    ffo = pl.coalesce(col("ffo"), ni + dep - col("gain_on_property_sales").fill_null(0) + col("impairment").fill_null(0))
    frame = frame.with_columns(ffo.alias("ffo"))
    affo = pl.coalesce(col("affo"), pl.col("ffo") - col("straight_line_rent").fill_null(0) - 0.15 * dep)
    return frame.with_columns(affo.alias("affo"))


def _ev(frame: pl.DataFrame, expr: str, name: str) -> pl.Series:
    """formula.evaluate on a narrow projection (panel keys + dependencies) of the frame: same result, much less
    data shuffled by the lag joins."""
    deps = [d for d in F.dependencies(expr) if d in frame.columns]
    if "mcap" in expr and "mcap" in frame.columns:
        deps.append("mcap")
    cols = list(dict.fromkeys([*PANEL, "period_end", *deps]))
    out = F.evaluate(expr, frame.select(cols), name=name, panel_keys=PANEL)
    if out.len() != frame.height:  # all-literal expression (every input column absent) -> broadcast the scalar
        out = pl.Series(name, [out[0] if out.len() else None] * frame.height, dtype=pl.Float64)
    return out


#: plausibility bounds: metric -> (|value| cap, minimum average invested capital / total assets)
SANITY_BOUNDS: dict[str, tuple[float, float]] = {"roic": (2.0, 0.05), "croic": (2.0, 0.05)}
#: extra ratio rows emitted by the bounds (never scored)
SANITY_UNITS: dict[str, str] = {**{f"{m}_raw": "pct" for m in SANITY_BOUNDS}, **{f"{m}_flag": "count" for m in SANITY_BOUNDS}}
_IC_SHARE = "safe_div(avg(invested_capital), total_assets)"


def _bounded(frame: pl.DataFrame, mid: str, vals: pl.Series) -> tuple[pl.DataFrame, pl.Series]:
    """Null implausible values of a bounded metric; keep the raw value (`<mid>_raw`) and a 0/1 flag (`<mid>_flag`)."""
    cap, min_share = SANITY_BOUNDS[mid]
    if "invested_capital" in frame.columns and "total_assets" in frame.columns:
        share = _ev(frame, _IC_SHARE, f"__ics_{mid}")
    else:
        share = pl.Series(f"__ics_{mid}", [None] * frame.height, dtype=pl.Float64)
    t = pl.DataFrame({"v": vals.cast(pl.Float64), "s": share.cast(pl.Float64)})
    bad = ((pl.col("v").abs() > cap).fill_null(False) | (pl.col("s") < min_share).fill_null(False)) & pl.col("v").is_not_null()
    r = t.select(pl.col("v").alias(f"{mid}_raw"),
                 pl.when(pl.col("v").is_null()).then(None).otherwise(bad.cast(pl.Float64)).alias(f"{mid}_flag"),
                 pl.when(bad).then(None).otherwise(pl.col("v")).alias(mid))
    return frame.with_columns(r.get_column(f"{mid}_raw"), r.get_column(f"{mid}_flag")), r.get_column(mid)


def eval_metrics(frame: pl.DataFrame, items: dict[str, str], order: Sequence[str] | None = None,
                 usd_ids: Sequence[str] = ()) -> pl.DataFrame:
    """Evaluate formulas {id: expr} in order, adding columns `<id>` and `__c_<id>` (inputs complete).
    `usd_ids`: evaluate on the USD basis (columns `<item>__usd` substituted for items) and store as `<id>`; a LOCAL
    twin `<id>__loc` is kept so local-basis formulas that use the metric never mix currencies.
    Metrics in SANITY_BOUNDS are bounded right after evaluation (dependants such as roic_mean_5y see the bounded
    value)."""
    usd = set(usd_ids)
    for mid in order or list(items):
        expr = items[mid]
        if mid in usd:
            deps = [d for d in F.dependencies(expr) if f"{d}__usd" in frame.columns]
            sub = frame.with_columns([pl.col(f"{d}__usd").alias(d) for d in deps])
            vals = _ev(sub, expr, mid)
            comp = sub.with_columns(_complete(sub, expr).alias("__cc")).get_column("__cc")
            frame = frame.with_columns(_ev(frame, expr, mid).alias(f"{mid}__loc"))
        else:
            loc = [d for d in F.dependencies(expr) if d in usd and f"{d}__loc" in frame.columns]
            sub = frame.with_columns([pl.col(f"{d}__loc").alias(d) for d in loc]) if loc else frame
            vals = _ev(sub, expr, mid)
            comp = sub.with_columns(_complete(sub, expr).alias("__cc")).get_column("__cc")
        if mid in SANITY_BOUNDS:
            frame, vals = _bounded(frame, mid, vals)
        frame = frame.with_columns(vals.alias(mid), comp.alias(f"__c_{mid}"))
    return frame


def accounting_scores(frame: pl.DataFrame) -> pl.DataFrame:
    """Adds piotroski_f, altman_z2, altman_z, beneish_m, sloan_accruals (+ __c_ flags). Needs generic metric columns,
    `groups`, `is_financial`, and optional `mcap` / `total_liabilities__usd`."""
    nonfin = ~pl.col("is_financial").fill_null(False)
    # Piotroski F
    comps = []
    for k, e in PIOTROSKI.items():
        frame = frame.with_columns(_ev(frame, e, k).alias(f"__p_{k}"))
        comps.append(f"__p_{k}")
    n_ok = pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in comps])
    tot = pl.sum_horizontal([pl.col(c).fill_null(0) for c in comps])
    frame = frame.with_columns(
        pl.when(nonfin & (n_ok >= 7)).then((tot * 9.0 / n_ok).round(0)).otherwise(None).alias("piotroski_f"),
        (n_ok == 9).alias("__c_piotroski_f"),
    ).drop(comps)
    # Altman Z'' and Z
    z2 = ("6.56*div(working_capital,total_assets) + 3.26*div(retained_earnings,total_assets) + "
          "6.72*div(ebit,total_assets) + 1.05*div(total_equity,total_liabilities)")
    wc = "coalesce(working_capital, total_current_assets - total_current_liabilities)"
    z2 = z2.replace("working_capital", wc, 1)
    frame = frame.with_columns(_ev(frame, z2, "z2").alias("altman_z2"),
                               _complete(frame, z2).alias("__c_altman_z2"))
    if "mcap" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, pl.Float64).alias("mcap"))
    if "total_liabilities__usd" in frame.columns:
        frame = frame.with_columns(pl.col("total_liabilities__usd").alias("total_liabilities_usd"))
    z = (f"1.2*div({wc},total_assets) + 1.4*div(retained_earnings,total_assets) + 3.3*div(ebit,total_assets) + "
         "0.6*div(mcap(),total_liabilities_usd) + 1.0*div(revenue,total_assets)")
    is_mfg = pl.col("groups").list.eval(pl.element().is_in(list(ALTMAN_Z_GROUPS))).list.any().fill_null(False)
    frame = frame.with_columns(_ev(frame, z, "z").alias("altman_z"),
                               _complete(frame, z.replace("mcap()", "mcap")).alias("__c_altman_z"))
    frame = frame.with_columns(
        pl.when(nonfin).then(pl.col("altman_z2")).otherwise(None).alias("altman_z2"),
        pl.when(nonfin & is_mfg).then(pl.col("altman_z")).otherwise(None).alias("altman_z"),
    )
    # Beneish M (textbook) + components + recalibrated screen (app.analytics.beneish, O2.2a / audit F15)
    from app.analytics import beneish as B  # noqa: PLC0415

    frame = B.polars_columns(frame)
    sl = "div(net_income - cfo, avg(total_assets))"
    frame = frame.with_columns(
        pl.when(nonfin).then(_ev(frame, sl, "s")).otherwise(None).alias("sloan_accruals"),
        _complete(frame, sl).alias("__c_sloan_accruals"))
    return frame


def to_long(frame: pl.DataFrame, ids: Sequence[str], units: dict[str, str], extra_keys: Sequence[str] = ()) -> pl.DataFrame:
    """Wide metric columns -> long rows for emitted periods."""
    f = frame.filter(pl.col("emit"))
    base = ["issuer_id", "period_kind", "fiscal_year", "fiscal_period", "period_end", "vintage", "dq_score", *extra_keys]
    parts = []
    for mid in ids:
        if mid not in f.columns:
            continue
        cc = f"__c_{mid}"
        parts.append(f.select(*base, pl.lit(mid).alias("metric_id"), pl.col(mid).cast(pl.Float64).alias("value"),
                              (pl.col(cc) if cc in f.columns else pl.lit(False)).fill_null(False).alias("inputs_complete"),
                              pl.lit(units.get(mid, "ratio")).alias("unit")))
    if not parts:
        return pl.DataFrame()
    out = pl.concat(parts, how="vertical_relaxed").rename({"vintage": "knowledge_date"})
    return out.with_columns(pl.when(pl.col("value").is_finite()).then(pl.col("value")).otherwise(None).alias("value"))


def dedupe_versions(long: pl.DataFrame, key: str = "metric_id") -> pl.DataFrame:
    """Drop rows whose value equals the previous knowledge_date version for the same (issuer, metric, period)."""
    if long.is_empty():
        return long
    g = ["issuer_id", key, "period_kind", "period_end"]
    long = long.sort(*g, "knowledge_date")
    prev = pl.col("value").shift(1).over(g)
    first = pl.col("knowledge_date").rank("ordinal").over(g) == 1
    same = (prev.is_null() & pl.col("value").is_null()) | (
        (prev - pl.col("value")).abs() <= 1e-9 * (pl.col("value").abs() + 1e-12))
    return long.filter(first | ~same.fill_null(False))


def build_frames(issuer_ids: Sequence[str], kind: str, meta: pl.DataFrame, settings: Settings | None = None,
                 with_mcap: bool = True, mcap: pl.DataFrame | None = None) -> pl.DataFrame:
    """Vintage-expanded panel for one period kind with generic metrics + accounting scores evaluated."""
    reg = registry(settings)
    wide = load_wide(issuer_ids, kind, settings)
    if wide.is_empty():
        return wide
    pan = vintage_expand(wide)
    pan = pan.join(meta.select("issuer_id", "sector_id", "subsector_id", "kpi_set_id", "groups", "is_financial"),
                   on="issuer_id", how="left")
    pan = _add_analytic_items(pan)
    if mcap is not None and not mcap.is_empty():
        pan = pan.join(mcap.select("issuer_id", "vintage", "mcap"), on=["issuer_id", "vintage"], how="left")
    elif with_mcap and mcap is None:
        mfg = pan.filter(pl.col("groups").list.eval(pl.element().is_in(list(ALTMAN_Z_GROUPS))).list.any().fill_null(False),
                         pl.col("emit"))
        if not mfg.is_empty():
            from app.analytics.valuation import issuer_mcap_at
            q = mfg.select("issuer_id", pl.col("vintage").alias("date")).unique()
            try:
                mc = issuer_mcap_at(q, settings).rename({"date": "vintage", "market_cap_usd": "mcap"})
                pan = pan.join(mc, on=["issuer_id", "vintage"], how="left")
            except Exception as e:  # prices missing etc. -> altman_z null
                log.warning("mcap for altman_z unavailable: %s", e)
    gen = reg["generic"]
    usd_ids = [m for m in reg["generic_order"] if gen[m].get("unit") == "currency_usd"]
    pan = eval_metrics(pan, {m: gen[m]["formula"] for m in gen}, reg["generic_order"], usd_ids)
    return accounting_scores(pan)


def ratio_rows(pan: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    from app.analytics import beneish as B  # noqa: PLC0415

    reg = registry(settings)
    ids = list(reg["generic_order"]) + [a["id"] for a in reg["accounting"]]
    units = {m: reg["meta"][m]["unit"] for m in ids}
    extra = [m for m in (*B.COMPONENT_METRIC_IDS, *B.DERIVED_METRIC_IDS) if m not in units]
    units.update({m: B.METRIC_UNITS[m] for m in extra})
    bounds = [m for m in SANITY_UNITS if m in pan.columns and m not in units]
    units.update({m: SANITY_UNITS[m] for m in bounds})
    return dedupe_versions(to_long(pan, ids + extra + bounds, units))


# ------------------------------------------------------------------------------------------------ job


#: issuers per issuer_mcap_at price load in _mcap_at_vintages (A1 2026-11: one load for every Altman-Z issuer of the
#: full universe held decades of daily prices + split factors at once). Not tied to `chunk`: a prices_daily scan has
#: a large fixed part (~1.5 s, and up to ~3 GB transient when the lake files are not in the OS cache, measured
#: 2026-11-07), so smaller chunks only add scans.
MCAP_CHUNK = 200


def _mcap_at_vintages(meta: pl.DataFrame, settings: Settings | None = None, chunk: int = MCAP_CHUNK) -> pl.DataFrame:
    """issuer_id, vintage, mcap for Altman-Z issuers at every knowledge date of their TTM/FY rows.

    Priced in issuer chunks of `chunk`: issuer_mcap_at is per issuer (primary security, as-of join by issuer, split
    factor by security; a chunk's price window starts before every date it asks for and ends at the same overall last
    date as one load of every issuer), so the result is identical, with memory bounded by the chunk."""
    mfg = meta.filter(pl.col("groups").list.eval(pl.element().is_in(list(ALTMAN_Z_GROUPS))).list.any().fill_null(False),
                      ~pl.col("is_financial"))["issuer_id"].to_list()
    empty = pl.DataFrame(schema={"issuer_id": pl.Utf8, "vintage": pl.Date, "mcap": pl.Float64})
    if not mfg:
        return empty
    from app.analytics.valuation import issuer_mcap_at

    q = (scan_table("fundamentals_wide", settings)
         .filter(pl.col("issuer_id").is_in(mfg), pl.col("period_kind").is_in(list(KINDS)))
         .select("issuer_id", pl.col("knowledge_date").alias("date")).unique().collect())
    if q.is_empty():
        return empty
    end = q["date"].max()
    ids = sorted(q["issuer_id"].unique().to_list())
    chunk = max(1, int(chunk))
    out: list[pl.DataFrame] = []
    try:
        for i in range(0, len(ids), chunk):
            qi = q.filter(pl.col("issuer_id").is_in(ids[i:i + chunk]))
            mc = issuer_mcap_at(qi, settings, end=end).rename({"date": "vintage", "market_cap_usd": "mcap"})
            out.append(mc.select("issuer_id", "vintage", "mcap"))
    except Exception as e:  # prices missing -> altman_z null
        log.warning("mcap for altman_z unavailable: %s", e)
        return empty
    return pl.concat(out, how="vertical_relaxed") if out else empty


def drop_stale_sector_rows(current: pl.DataFrame, settings: Settings | None = None) -> int:
    """sector_kpis is partitioned by sector_id: when an issuer's sector changes, the upsert writes its rows into the
    new partition and the old partition keeps the previous copies (cross-partition duplicate primary keys;
    2026-10-31: the ref_universe sector wipe + identity_resolve restore flipped e.g. Henry Schein HLTH -> INDU and
    dq_checks failed sector_kpis/pk_unique). Removes the rows of `current` issuers that sit in another sector's
    partition. current: [issuer_id, sector_id] of the rows just written. Returns rows removed."""
    from app.normalize.identity import _rewrite_partitions

    cur = {r["issuer_id"]: r["sector_id"] for r in current.drop_nulls("issuer_id").iter_rows(named=True)}
    if not cur:
        return 0

    def tf(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        want = pl.col("issuer_id").replace_strict(cur, default="__keep__", return_dtype=pl.Utf8)
        stale = (want != "__keep__") & ((want != pl.col("sector_id")) | pl.col("sector_id").is_null())
        n = int(df.select(stale.fill_null(False).sum()).item() or 0)
        return (df.filter(~stale.fill_null(False)), n) if n else (df, 0)

    n = _rewrite_partitions("sector_kpis", tf, settings)
    if n:
        log.info("sector_kpis: removed %d rows left in a previous sector partition", n)
    return n


def drop_stale_rows(table: str, issuer_ids: Sequence[str], run_ts: datetime, settings: Settings | None = None) -> int:
    """Remove rows of the recomputed `issuer_ids` written before this run (computed_at < run_ts): every row the run
    produced for them carries computed_at = run_ts, so an older row is a vintage / period the current inputs no
    longer yield. Cheap count first; only partitions with stale rows are rewritten. Returns rows removed."""
    from app.normalize.identity import _rewrite_partitions

    ids = list(issuer_ids)
    if not ids:
        return 0
    stale = pl.col("issuer_id").is_in(ids) & (pl.col("computed_at") < run_ts)
    try:
        n = int(scan_table(table, settings).filter(stale).select(pl.len()).collect().item() or 0)
    except Exception:  # noqa: BLE001 - empty / missing table
        return 0
    if not n:
        return 0

    def tf(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        m = stale.fill_null(False)
        k = int(df.select(m.sum()).item() or 0)
        return (df.filter(~m), k) if k else (df, 0)

    removed = _rewrite_partitions(table, tf, settings)
    log.info("%s: removed %d stale rows of %d recomputed issuers", table, removed, len(ids))
    return removed


def _scope(ctx, s: Settings) -> list[str]:  # noqa: ANN001
    from app.analytics.valuation import universe_frame

    p = ctx.params or {}
    uni = universe_frame(p.get("universe", "mvp"), None, s, p.get("tickers"))
    ids = uni["issuer_id"].unique().to_list()
    if p.get("issuers"):
        ids = [i for i in ids if i in set(p["issuers"])]
    return ids


def run(ctx):  # noqa: ANN001 - JobContext
    from app.analytics import sector_kpis as SK
    from app.scheduler.base import JobResult

    s = ctx.settings
    p = ctx.params or {}
    issuers = _scope(ctx, s)
    fw = (scan_table("fundamentals_wide", s).filter(pl.col("issuer_id").is_in(issuers))
          .group_by("issuer_id").agg(pl.col("computed_at").max().alias("ts")).collect())
    full = bool(p.get("rebuild", False))
    now = datetime.now(timezone.utc)
    new_wm = fw["ts"].max().isoformat() if fw.height and fw["ts"].max() is not None else ctx.watermark
    if p.get("issuers") or p.get("tickers"):
        new_wm = ctx.watermark  # a targeted run says nothing about the other issuers: keep the incremental watermark
    n_universe_fund = fw.height
    # incremental: only issuers whose fundamentals were (re)normalized after the last successful run
    if not full and ctx.watermark and not p.get("tickers") and not p.get("issuers"):
        try:
            wm = datetime.fromisoformat(str(ctx.watermark))
            fw = fw.filter(pl.col("ts") > wm)
        except ValueError:
            pass
    have = fw["issuer_id"].sort().to_list()
    meta = issuer_meta(have, s)
    chunk = int(p.get("chunk", 25))
    mcap = _mcap_at_vintages(meta, s, chunk=int(p.get("mcap_chunk", MCAP_CHUNK)))
    rows_r, rows_k = [], []
    tasks = [(kind, have[i:i + chunk]) for kind in KINDS for i in range(0, len(have), chunk)]

    def work(task: tuple[str, list[str]]) -> tuple[pl.DataFrame, pl.DataFrame]:
        kind, ids = task
        ctx.check_cancelled()
        pan = build_frames(ids, kind, meta.filter(pl.col("issuer_id").is_in(ids)), s, mcap=mcap)
        if pan.is_empty():
            return pl.DataFrame(), pl.DataFrame()
        return ratio_rows(pan, s), SK.kpi_rows(pan, s)

    from concurrent.futures import ThreadPoolExecutor

    from app.core.lake_stream import StagedWrite

    stamp = [pl.lit(now).alias("computed_at"), pl.lit(ctx.run_id).alias("run_id")]
    written: dict[str, int] = {}
    mode = "overwrite" if full and not p.get("tickers") and not p.get("issuers") else None
    # A1 2026-11 (OOM at the 6 GB cap since 11-03, retry included): results are staged to Parquet every ~flush_rows
    # rows (app.core.lake_stream) and committed ONCE at the end with a streaming merge. Before, every flush upserted
    # into ratios/period_kind=TTM, i.e. read the whole 18.7 M-row partition into memory (~3 GB), merged and rewrote it
    # (~42 s per flush; the smaller-chunk retry only flushed more often). A full rebuild no longer holds every row
    # until the end either. Peak memory = the compute window + one flush buffer, independent of the table size.
    flush_rows = int(p.get("flush_rows", 500_000))
    stages = {"ratios": StagedWrite("ratios", s), "sector_kpis": StagedWrite("sector_kpis", s)}

    def flush() -> None:
        for name, rows in (("ratios", rows_r), ("sector_kpis", rows_k)):
            if rows:
                stages[name].add(pl.concat(rows, how="vertical_relaxed").with_columns(stamp))
                rows.clear()

    # Polars releases the GIL: a few threads overlap the many small lag joins of formula.evaluate
    n_workers = max(1, int(p.get("workers", 4)))

    def results():  # bounded look-ahead: Executor.map would compute (and hold) every task's result at once
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            step = n_workers * 4
            for i in range(0, len(tasks), step):
                win = tasks[i:i + step]
                yield from zip(win, ex.map(work, win), strict=True)

    replaced: dict[str, int] = {}
    try:
        for (kind, ids), (r, k) in results():
            if not r.is_empty():
                rows_r.append(r)
            if not k.is_empty():
                rows_k.append(k)
            log.info("ratios %s %d issuers: %d ratio rows, %d kpi rows", kind, len(ids), r.height, k.height)
            if sum(x.height for x in rows_r) + sum(x.height for x in rows_k) >= flush_rows:
                flush()
        flush()
        ctx.check_cancelled()
        for sw in stages.values():  # both stages intact before EITHER table is committed (StageIntegrityError)
            sw.verify()
        for name, sw in stages.items():
            if mode == "overwrite":
                if sw.rows:
                    written[name] = sw.commit_overwrite()
                continue
            if not have:
                continue
            # an incremental recompute REPLACES the issuers' rows, in whatever partition they sit: vintages /
            # periods the new inputs no longer produce are removed (W1 2026-10-31: an upsert kept 16,983 ratios rows
            # derived from since-dropped leaky fundamentals rows) and an issuer whose sector changed leaves its old
            # sector_kpis partition (2026-10-31 pk_unique); was upsert + drop_stale_rows + drop_stale_sector_rows
            res = sw.commit_replace("issuer_id", have)
            if res["rows"]:
                written[name] = res["rows"]
            if res["replaced"]:
                replaced[name] = res["replaced"]
    finally:
        for sw in stages.values():
            if not sw.closed:
                sw.discard()
    n_ok = len(have)
    rep = "".join(f"; {n}: {v} previous rows replaced" for n, v in replaced.items())
    return JobResult(status="success" if (written or replaced or not have) else "partial", rows_written=written,
                     entities_ok=n_ok, entities_failed=0, watermark=new_wm,
                     message=f"recomputed {len(have)} issuers; {n_universe_fund}/{len(issuers)} universe issuers "
                             f"have fundamentals{rep}")
