"""Job company_debt: issuer debt profiles from filings -> company_debt + debt_maturity_walls (contracts/schemas).

Inputs
  fundamentals_canonical  total_debt / short_term_debt / long_term_debt / cash_and_equivalents / net_debt (INSTANT),
                          ebit / ebitda / interest_expense (TTM, FY otherwise) - PIT versions, the latest is used
  SEC companyfacts        maturity / fixed-floating / weighted-rate tags (SEC_TAGS) of every resolvable CIK, read
                          from the bulk companyfacts.zip already downloaded by the SEC job (data/raw/sec_edgar/
                          companyfacts_bulk/dt=*/companyfacts.zip; nothing is downloaded here). Extracted facts are
                          cached in data/raw/rates/sec_debt_facts.parquet and re-extracted only when a newer zip appears
  fundamentals_raw        IFRS maturity analyses (MaturityAxis members) and fixed / floating (TypesOfInterestRatesAxis)
  bond_reference          listed, not matured bonds per issuer
  yield_curves + macro    MODEL-BASED refinancing rate: local 5y government yield + ICE BofA IG / HY OAS proxy
params: refresh_sec (bool: force the companyfacts extraction), max_ciks (int, tests).
"""

from __future__ import annotations

import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger

from .common import now_utc, read_state, update_state

log = get_logger("app.rates.company_debt")

SEC_TAGS: dict[str, str] = {
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths": "mat_y1",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo": "mat_y2",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree": "mat_y3",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour": "mat_y4",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive": "mat_y5",
    "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive": "mat_after5",
    "LongTermDebtPercentageBearingFixedInterestAmount": "fixed_debt",
    "LongTermDebtPercentageBearingVariableInterestAmount": "floating_debt",
    "DebtWeightedAverageInterestRate": "weighted_avg_rate",
    "LongtermDebtWeightedAverageInterestRate": "weighted_avg_rate_lt",
}
IFRS_MAT_CONCEPTS = ("BorrowingsUndiscountedCashFlows", "BankBorrowingsUndiscountedCashFlows", "Borrowings",
                     "NonderivativeFinancialLiabilitiesUndiscountedCashFlows", "NoncurrentBorrowings")
IFRS_MATURITY: dict[str, str] = {
    "ifrs-full:NotLaterThanOneYearMember": "mat_y1",
    "ifrs-full:LaterThanOneYearAndNotLaterThanTwoYearsMember": "mat_y2",
    "ifrs-full:LaterThanTwoYearsAndNotLaterThanThreeYearsMember": "mat_y3",
    "ifrs-full:LaterThanThreeYearsAndNotLaterThanFourYearsMember": "mat_y4",
    "ifrs-full:LaterThanFourYearsAndNotLaterThanFiveYearsMember": "mat_y5",
    "ifrs-full:LaterThanFiveYearsMember": "mat_after5",
    "ifrs-full:LaterThanOneYearAndNotLaterThanFiveYearsMember": "mat_y2_5",
}
CANON = ("total_debt", "short_term_debt", "long_term_debt", "cash_and_equivalents", "net_debt", "ebit", "ebitda",
         "interest_expense")
FLOWS = ("ebit", "ebitda", "interest_expense")
MAT_COLS = ("mat_y1", "mat_y2", "mat_y3", "mat_y4", "mat_y5", "mat_after5")
MID = {"mat_y1": 0.5, "mat_y2": 1.5, "mat_y3": 2.5, "mat_y4": 3.5, "mat_y5": 4.5, "mat_after5": 7.5, "mat_y2_5": 3.0}
SEC_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT")


# =============================================================================================== SEC companyfacts

def extract_companyfacts(doc: dict[str, Any], cik: str) -> list[dict[str, Any]]:
    """Debt tags of one companyfacts document -> rows (cik, field, end, val, filed, form); the latest filed value per
    (field, end) wins (amendments supersede)."""
    best: dict[tuple[str, date], dict[str, Any]] = {}
    gaap = ((doc.get("facts") or {}).get("us-gaap")) or {}
    for tag, field in SEC_TAGS.items():
        node = gaap.get(tag)
        if not node:
            continue
        for unit, facts in (node.get("units") or {}).items():
            pct = field.startswith("weighted")
            if pct != (unit == "pure"):
                continue
            for f in facts:
                if f.get("form") not in SEC_FORMS:
                    continue
                try:
                    end = date.fromisoformat(f["end"])
                    filed = date.fromisoformat(f["filed"])
                    val = float(f["val"])
                except (KeyError, TypeError, ValueError):
                    continue
                k = (field, end)
                cur = best.get(k)
                if cur is None or filed >= cur["filed"]:
                    best[k] = {"cik": cik, "field": field, "end": end, "val": val, "filed": filed,
                               "form": f.get("form"), "unit": unit}
    return list(best.values())


def latest_bulk_zip(s: Settings) -> Path | None:
    root = s.raw_dir / "sec_edgar" / "companyfacts_bulk"
    zs = sorted(root.glob("dt=*/companyfacts.zip")) if root.exists() else []
    for z in reversed(zs):
        try:
            with zipfile.ZipFile(z) as zz:   # opening reads the central directory: a truncated download fails here
                if zz.namelist():
                    return z
        except zipfile.BadZipFile:
            continue
    return None


def sec_debt_facts(s: Settings, refresh: bool = False, max_ciks: int | None = None) -> pl.DataFrame:
    """Cached extraction of SEC_TAGS for every CIK of the bulk zip that resolves to one of our issuers."""
    import orjson  # noqa: PLC0415

    from app.normalize.identity import lookup_many  # noqa: PLC0415

    cache = s.raw_dir / "rates" / "sec_debt_facts.parquet"
    z = latest_bulk_zip(s)
    st = read_state(s).get("company_debt") or {}
    if z is None:   # no bulk zip on disk (the SEC job keeps it only transiently): newest per-CIK companyfacts files
        if cache.exists() and not refresh:
            return pl.read_parquet(cache)
        return _per_cik_facts(s, cache, max_ciks)
    key = f"{z.parent.name}:{z.stat().st_size}"
    if cache.exists() and not refresh:
        age_d = (time.time() - cache.stat().st_mtime) / 86400
        if st.get("sec_zip") == key or age_d < 7:   # a newer daily zip is re-extracted at most weekly
            return pl.read_parquet(cache)
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(z) as zz:
        names = [n for n in zz.namelist() if n.startswith("CIK") and n.endswith(".json")]
        ciks = {n[3:13]: n for n in names}
        res = lookup_many("cik", list(ciks))
        ok = set(res.filter(pl.col("issuer_id").is_not_null())["input_value"].to_list()) if res.height else set()
        want = [c for c in ciks if c in ok or c.lstrip("0") in ok]
        if max_ciks:
            want = want[:max_ciks]
        for i, c in enumerate(want):
            try:
                doc = orjson.loads(zz.read(ciks[c]))
            except Exception:  # noqa: BLE001
                continue
            rows += extract_companyfacts(doc, c)
            if i and i % 1000 == 0:
                log.info("company_debt: companyfacts %d/%d CIKs (%.0fs)", i, len(want), time.perf_counter() - t0)
    df = pl.DataFrame(rows, schema={"cik": pl.Utf8, "field": pl.Utf8, "end": pl.Date, "val": pl.Float64,
                                    "filed": pl.Date, "form": pl.Utf8, "unit": pl.Utf8}) if rows else pl.DataFrame(
        schema={"cik": pl.Utf8, "field": pl.Utf8, "end": pl.Date, "val": pl.Float64, "filed": pl.Date,
                "form": pl.Utf8, "unit": pl.Utf8})
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(cache, compression="zstd")
    update_state({"company_debt": {"sec_zip": key, "sec_ciks": len(want), "sec_rows": df.height,
                                   "sec_extract_s": round(time.perf_counter() - t0, 1)}}, s)
    log.info("company_debt: %d debt facts from %d CIKs in %.0fs", df.height, len(want), time.perf_counter() - t0)
    return df


def _per_cik_facts(s: Settings, cache: Path, max_ciks: int | None = None) -> pl.DataFrame:
    """Fallback: data/raw/sec_edgar/companyfacts/dt=*/CIK##########.gz (gzip JSON written by the SEC job), newest per CIK."""
    import gzip  # noqa: PLC0415

    import orjson  # noqa: PLC0415

    root = s.raw_dir / "sec_edgar" / "companyfacts"
    newest: dict[str, Path] = {}
    for f in sorted(root.glob("dt=*/CIK*")):
        newest[f.name[3:13]] = f
    rows: list[dict[str, Any]] = []
    for i, (c, f) in enumerate(sorted(newest.items())):
        if max_ciks and i >= max_ciks:
            break
        try:
            raw = f.read_bytes()
            doc = orjson.loads(gzip.decompress(raw) if f.suffix == ".gz" else raw)
        except Exception:  # noqa: BLE001
            continue
        rows += extract_companyfacts(doc, c)
    df = pl.DataFrame(rows, schema={"cik": pl.Utf8, "field": pl.Utf8, "end": pl.Date, "val": pl.Float64,
                                    "filed": pl.Date, "form": pl.Utf8, "unit": pl.Utf8})
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(cache, compression="zstd")
    update_state({"company_debt": {"sec_zip": "per_cik", "sec_ciks": len(newest), "sec_rows": df.height}}, s)
    return df


def sec_profiles(facts: pl.DataFrame) -> pl.DataFrame:
    """facts -> (issuer_id, mat_period_end, mat_*, fixed/floating, weighted rate, knowledge_date) per balance date."""
    from app.normalize.identity import lookup_many  # noqa: PLC0415

    if facts.is_empty():
        return pl.DataFrame()
    res = lookup_many("cik", facts["cik"].unique().to_list())
    m = res.select(pl.col("input_value").alias("cik"), "issuer_id").drop_nulls()
    wide = (facts.pivot(on="field", index=["cik", "end"], values="val", aggregate_function="last")
            .join(facts.group_by("cik", "end").agg(pl.col("filed").max().alias("mat_kd")), on=["cik", "end"]))
    wide = wide.join(m, on="cik", how="inner")
    for c in (*MAT_COLS, "fixed_debt", "floating_debt", "weighted_avg_rate", "weighted_avg_rate_lt"):
        if c not in wide.columns:
            wide = wide.with_columns(pl.lit(None, pl.Float64).alias(c))
    return wide.with_columns(
        pl.coalesce("weighted_avg_rate", "weighted_avg_rate_lt").alias("weighted_avg_rate"),
        pl.lit("us_gaap").alias("mat_source"), pl.lit(None, pl.Float64).alias("mat_y2_5"),
    ).rename({"end": "mat_period_end"}).drop("weighted_avg_rate_lt", "cik").unique(
        ["issuer_id", "mat_period_end"], keep="last")


# =============================================================================================== IFRS (fundamentals_raw)

def ifrs_profiles(s: Settings) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        lf = (scan_table("fundamentals_raw", s)
              .filter(pl.col("issuer_id").is_not_null() & pl.col("concept").is_in(list(IFRS_MAT_CONCEPTS))
                      & pl.col("dimensions_json").str.contains("MaturityAxis|TypesOfInterestRatesAxis"))
              .select("issuer_id", "concept", "period_end", "value", "dimensions_json", "knowledge_date", "unit"))
        df = lf.collect()
    except Exception as e:  # noqa: BLE001
        log.warning("company_debt: fundamentals_raw unreadable: %s", e)
        return pl.DataFrame()
    if df.is_empty():
        return pl.DataFrame()
    rank = {c: i for i, c in enumerate(IFRS_MAT_CONCEPTS)}
    rows: dict[tuple[str, date], dict[str, Any]] = {}
    import json  # noqa: PLC0415

    for iid, concept, pe, val, dj, kd, _u in df.iter_rows():
        try:
            dims = json.loads(dj or "{}")
        except ValueError:
            continue
        if len(dims) != 1:
            continue
        (ax, mem), = dims.items()
        r = rows.setdefault((iid, pe), {"issuer_id": iid, "mat_period_end": pe, "mat_kd": kd, "_rank": {}})
        r["mat_kd"] = max(r["mat_kd"], kd)
        if ax.endswith("MaturityAxis") and mem in IFRS_MATURITY:
            f = IFRS_MATURITY[mem]
            if f not in r or rank[concept] < r["_rank"].get(f, 99):
                r[f] = val
                r["_rank"][f] = rank[concept]
        elif ax.endswith("TypesOfInterestRatesAxis"):
            if "Fixed" in mem:
                r["fixed_debt"] = val
            elif "Floating" in mem or "Variable" in mem:
                r["floating_debt"] = val
    out = []
    for r in rows.values():
        r.pop("_rank", None)
        out.append(r)
    pf = pl.DataFrame(out, infer_schema_length=None)
    for c in (*MAT_COLS, "mat_y2_5", "fixed_debt", "floating_debt"):
        if c not in pf.columns:
            pf = pf.with_columns(pl.lit(None, pl.Float64).alias(c))
    return pf.with_columns(pl.lit(None, pl.Float64).alias("weighted_avg_rate"), pl.lit("ifrs").alias("mat_source"))


# =============================================================================================== canonical balances / flows

def canonical_panel(s: Settings) -> pl.DataFrame:
    """(issuer_id, period_end) panel of the latest version of each canonical item: instants + TTM flows (FY flows
    where no TTM)."""
    from app.core.storage import scan_table  # noqa: PLC0415

    lf = (scan_table("fundamentals_canonical", s)
          .filter(pl.col("item_id").is_in(list(CANON)) & pl.col("period_kind").is_in(["INSTANT", "TTM", "FY"]))
          .select("issuer_id", "item_id", "period_end", "period_kind", "value", "currency", "fx_rate",
                  "knowledge_date", "knowledge_date_estimated"))
    df = lf.collect()
    if df.is_empty():
        return pl.DataFrame()
    df = df.sort("knowledge_date").unique(["issuer_id", "item_id", "period_end", "period_kind"], keep="last")
    inst = df.filter(pl.col("period_kind") == "INSTANT")
    flow = df.filter(pl.col("period_kind") != "INSTANT")
    fy_ends = flow.filter(pl.col("period_kind") == "FY").select("issuer_id", "period_end").unique().with_columns(
        pl.lit(True).alias("_fy"))
    # TTM preferred, FY otherwise (same period_end)
    flow = flow.with_columns((pl.col("period_kind") == "TTM").alias("_ttm")).sort("_ttm").unique(
        ["issuer_id", "item_id", "period_end"], keep="last").drop("_ttm")
    both = pl.concat([inst, flow], how="vertical_relaxed")
    wide = both.pivot(on="item_id", index=["issuer_id", "period_end"], values="value", aggregate_function="last")
    meta = both.group_by("issuer_id", "period_end").agg(
        pl.col("currency").drop_nulls().first(), pl.col("fx_rate").filter(pl.col("period_kind") == "INSTANT")
        .drop_nulls().first().alias("fx_to_usd"), pl.col("knowledge_date").max(),
        pl.col("knowledge_date_estimated").any())
    wide = wide.join(meta, on=["issuer_id", "period_end"]).join(fy_ends, on=["issuer_id", "period_end"], how="left")
    for c in CANON:
        if c not in wide.columns:
            wide = wide.with_columns(pl.lit(None, pl.Float64).alias(c))
    return wide.with_columns(pl.when(pl.col("_fy")).then(pl.lit("FY")).otherwise(pl.lit("Q")).alias("period_kind")
                             ).drop("_fy").filter(pl.col("total_debt").is_not_null() | pl.col("interest_expense").is_not_null())


# =============================================================================================== market rates (model-based)

def market_rates(s: Settings) -> dict[str, float]:
    """{currency: 5y govt yield (fraction)} + {'IG': oas, 'HY': oas} (latest, from yield_curves / macro lake)."""
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.macro.cb.common import lake_series  # noqa: PLC0415

    out: dict[str, float] = {}
    ccy_curve = {"USD": ("US_TSY", "par"), "EUR": ("DE_BUND", "par"), "GBP": ("GB_GILT", "spot"),
                 "JPY": ("JP_JGB", "par"), "CHF": ("CH_CONF", "spot"), "CAD": ("CA_GOC", "yield"),
                 "AUD": ("AU_ACGB", "yield"), "SEK": ("SE_SGB", "yield"), "NOK": ("NO_NGB", "yield")}
    try:
        lf = scan_table("yield_curves", s).filter((pl.col("tenor_months") == 60)
                                                  & (pl.col("date") >= date.today() - timedelta(days=60)))
        yc = lf.select("curve_id", "type", "date", "value_bp_x100").collect()
        for ccy, (cid, typ) in ccy_curve.items():
            x = yc.filter((pl.col("curve_id") == cid) & (pl.col("type") == typ)).sort("date")
            if x.height:
                out[ccy] = x["value_bp_x100"][-1] / 1e6
    except Exception as e:  # noqa: BLE001
        log.info("company_debt: yield_curves unavailable: %s", e)
    oas = lake_series(["fred:BAMLC0A0CM", "fred:BAMLH0A0HYM2"], s, date.today() - timedelta(days=60))
    for k, sid in (("IG", "fred:BAMLC0A0CM"), ("HY", "fred:BAMLH0A0HYM2")):
        if oas.get(sid):
            out[k] = oas[sid][-1][1] / 100.0
    return out


# =============================================================================================== build

def _safe_div(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.when(b.is_not_null() & (b != 0)).then(a / b).otherwise(None)


def build_company_debt(panel: pl.DataFrame, profiles: pl.DataFrame, bonds: pl.DataFrame | None,
                       rates: dict[str, float]) -> pl.DataFrame:
    """Pure: canonical panel x maturity profiles (as-of join: latest profile with mat_period_end <= period_end
    within 400 days) -> company_debt rows with the derived ratios."""
    if panel.is_empty():
        return pl.DataFrame()
    df = panel.sort("issuer_id", "period_end")
    # prior-year total debt for the average-debt denominator
    prev = df.select("issuer_id", (pl.col("period_end") + pl.duration(days=365)).alias("_pe"),
                     pl.col("total_debt").alias("_td_prev")).sort("issuer_id", "_pe")
    df = df.join_asof(prev, left_on="period_end", right_on="_pe", by="issuer_id", strategy="backward", check_sortedness=False,
                      tolerance="45d").drop("_pe", strict=False)
    if profiles is not None and not profiles.is_empty():
        pr = profiles.sort("issuer_id", "mat_period_end").with_columns(pl.col("mat_period_end").alias("_mpe"))
        df = df.join_asof(pr, left_on="period_end", right_on="_mpe", by="issuer_id", strategy="backward", check_sortedness=False,
                          tolerance="400d").drop("_mpe", strict=False)
    for c in (*MAT_COLS, "mat_y2_5", "fixed_debt", "floating_debt", "weighted_avg_rate"):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Float64).alias(c))
    for c, t in (("mat_source", pl.Utf8), ("mat_period_end", pl.Date), ("mat_kd", pl.Date)):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, t).alias(c))
    td = pl.col("total_debt")
    avg_debt = pl.when(pl.col("_td_prev").is_not_null()).then((td + pl.col("_td_prev")) / 2).otherwise(td)
    mats = pl.sum_horizontal([pl.col(c).fill_null(0) for c in (*MAT_COLS, "mat_y2_5")])
    wam = pl.sum_horizontal([pl.col(c).fill_null(0) * MID[c] for c in (*MAT_COLS, "mat_y2_5")])
    df = df.with_columns(
        pl.col("cash_and_equivalents").alias("cash"),
        pl.coalesce("net_debt", td - pl.col("cash_and_equivalents").fill_null(0)).alias("net_debt"),
        pl.col("short_term_debt").alias("debt_current"), pl.col("long_term_debt").alias("debt_noncurrent"),
        pl.when((avg_debt > 0) & (pl.col("interest_expense") > 0)).then(pl.col("interest_expense").abs() / avg_debt)
        .otherwise(None).alias("avg_cost_of_debt"),
        _safe_div(pl.col("ebit"), pl.col("interest_expense").abs()).alias("interest_coverage"),
        _safe_div(pl.col("ebitda"), pl.col("interest_expense").abs()).alias("ebitda_interest_coverage"),
        pl.when(mats > 0).then(wam / mats).otherwise(None).alias("wam_years"),
        pl.when(mats > 0).then((pl.col("mat_y1").fill_null(0) + pl.col("mat_y2").fill_null(0)) / mats)
        .otherwise(None).alias("refi_share_24m"),
        _safe_div(pl.col("floating_debt"), pl.col("fixed_debt").fill_null(0) + pl.col("floating_debt")).alias("floating_share"),
        pl.when(mats > 0).then(pl.col("mat_source")).otherwise(pl.lit("none")).alias("mat_source"),
    )
    df = df.with_columns(
        pl.when(pl.col("ebitda") > 0).then(pl.col("net_debt") / pl.col("ebitda")).otherwise(None).alias("net_debt_to_ebitda"),
        pl.when(pl.col("ebitda") > 0).then(td / pl.col("ebitda")).otherwise(None).alias("debt_to_ebitda"),
        pl.max_horizontal("knowledge_date", "mat_kd").alias("knowledge_date"),
    )
    # MODEL-BASED refinancing rate: local 5y govt + IG / HY OAS proxy by interest coverage
    gov = pl.col("currency").replace_strict({k: v for k, v in rates.items() if len(k) == 3}, default=None,
                                            return_dtype=pl.Float64)
    spread = pl.when(pl.col("interest_coverage") >= 4).then(pl.lit(rates.get("IG"))).otherwise(pl.lit(rates.get("HY")))
    df = df.with_columns((gov + spread).alias("refi_market_rate"))
    df = df.with_columns(
        ((pl.col("refi_market_rate") - pl.col("avg_cost_of_debt")) * 1e4).alias("refi_rate_gap_bp"),
        ((pl.col("refi_market_rate") - pl.col("avg_cost_of_debt"))
         * (pl.col("mat_y1").fill_null(0) + pl.col("mat_y2").fill_null(0))).alias("refi_cost_24m"),
    )
    if bonds is not None and not bonds.is_empty():
        nb = bonds.group_by("issuer_id").agg(pl.len().cast(pl.Int32).alias("n_bonds_listed"))
        df = df.join(nb, on="issuer_id", how="left")
    else:
        df = df.with_columns(pl.lit(None, pl.Int32).alias("n_bonds_listed"))
    df = df.with_columns(pl.col("period_end").dt.year().cast(pl.Int16).alias("fiscal_year"),
                         pl.col("knowledge_date_estimated").fill_null(False), pl.lit(now_utc()).alias("computed_at"))
    from app.core.schema import get_contract  # noqa: PLC0415

    cols = get_contract("company_debt").column_names
    return df.select([c for c in cols if c in df.columns])


def build_walls(cd: pl.DataFrame, meta: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """Latest profile per issuer (period_end within 18 months) x issuer region / sector -> walls (USD)."""
    if cd.is_empty():
        return pl.DataFrame()
    lat = (cd.filter(pl.col("period_end") >= as_of - timedelta(days=548)).sort("period_end")
           .unique("issuer_id", keep="last").join(meta, on="issuer_id", how="inner"))
    fx = pl.col("fx_to_usd").fill_null(pl.when(pl.col("currency") == "USD").then(1.0).otherwise(None))
    usd = {c: (pl.col(c) * fx) for c in (*MAT_COLS, "total_debt", "refi_cost_24m")}
    lat = lat.with_columns([e.alias(f"{c}_usd") for c, e in usd.items()])
    out: list[dict[str, Any]] = []
    groups = [("ALL", "ALL", lat)]
    for (reg,), g in lat.group_by(["region"]):
        groups.append((reg, "ALL", g))
    for (sec,), g in lat.group_by(["sector_id"]):
        groups.append(("ALL", sec, g))
    for (reg, sec), g in lat.group_by(["region", "sector_id"]):
        groups.append((reg, sec, g))
    for reg, sec, g in groups:
        if reg is None or sec is None:
            continue
        prof = g.filter(pl.col("mat_source") != "none")
        tot_m = float(sum((prof[f"{c}_usd"].fill_null(0).sum() for c in MAT_COLS), 0.0)) if prof.height else 0.0
        for c in MAT_COLS:
            amt = float(prof[f"{c}_usd"].fill_null(0).sum()) if prof.height else None
            out.append({"region": reg, "sector_id": sec, "bucket": c.replace("mat_", ""), "amount_usd": amt,
                        "share": (amt / tot_m) if tot_m and amt is not None else None, "n_issuers": prof.height})
        tdu = g["total_debt_usd"]
        w = g.filter(pl.col("avg_cost_of_debt").is_not_null() & pl.col("total_debt_usd").is_not_null())
        fl = g.filter(pl.col("floating_share").is_not_null() & pl.col("total_debt_usd").is_not_null())
        out.append({"region": reg, "sector_id": sec, "bucket": "total", "amount_usd": tot_m or None, "share": 1.0 if tot_m else None,
                    "n_issuers": prof.height, "n_issuers_debt": int(g.filter(pl.col("total_debt") > 0).height),
                    "total_debt_usd": float(tdu.fill_null(0).sum()) if tdu.len() else None,
                    "wavg_cost_of_debt": float((w["avg_cost_of_debt"] * w["total_debt_usd"]).sum() / w["total_debt_usd"].sum())
                    if w.height and w["total_debt_usd"].sum() else None,
                    "median_interest_coverage": g["interest_coverage"].median(),
                    "median_net_debt_to_ebitda": g["net_debt_to_ebitda"].median(),
                    "refi_share_24m": float((prof["mat_y1_usd"].fill_null(0).sum() + prof["mat_y2_usd"].fill_null(0).sum()) / tot_m)
                    if tot_m else None,
                    "floating_share": float((fl["floating_share"] * fl["total_debt_usd"]).sum() / fl["total_debt_usd"].sum())
                    if fl.height and fl["total_debt_usd"].sum() else None,
                    "refi_cost_24m_usd": float(g["refi_cost_24m_usd"].drop_nulls().sum()) if g["refi_cost_24m_usd"].drop_nulls().len() else None})
    df = pl.DataFrame(out, infer_schema_length=None)
    return df.with_columns(pl.lit(as_of).alias("as_of"), pl.lit(now_utc()).alias("computed_at"),
                           pl.col("n_issuers").cast(pl.Int32))


def issuer_meta(s: Settings) -> pl.DataFrame:
    from app.core.storage import read_table  # noqa: PLC0415

    iss = read_table("issuers", columns=["issuer_id", "country", "sector_id", "updated_at"], settings=s)
    iss = iss.sort("updated_at").unique("issuer_id", keep="last")
    try:
        from app.ingestion.macro_common import catalog_doc  # noqa: PLC0415

        regions = {k: (v or {}).get("api_region") for k, v in (catalog_doc().get("regions") or {}).items()}
    except Exception:  # noqa: BLE001
        regions = {}
    eu = {"AT", "BE", "DK", "FI", "FR", "DE", "GR", "IE", "IT", "LU", "NL", "NO", "PT", "ES", "SE", "CH", "PL", "CZ",
          "HU", "IS", "EE", "LV", "LT", "SI", "SK", "HR", "RO", "BG", "CY", "MT"}
    reg = {**{c: "EU" for c in eu}, **{k: v for k, v in regions.items() if v}, "GB": "UK", "US": "US", "JP": "JP",
           "CA": "CA", "KR": "KR", "CN": "CN", "HK": "HK"}
    return iss.select("issuer_id", pl.col("country").replace_strict(reg, default="OTHER").alias("region"),
                      pl.col("sector_id").fill_null("unclassified"))


def run_company_debt(s: Settings, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    params = params or {}
    t0 = time.perf_counter()
    panel = canonical_panel(s)
    facts = sec_debt_facts(s, bool(params.get("refresh_sec")), params.get("max_ciks"))
    prof = [p for p in (sec_profiles(facts), ifrs_profiles(s)) if p is not None and not p.is_empty()]
    profiles = pl.concat(prof, how="diagonal_relaxed") if prof else pl.DataFrame()
    if not profiles.is_empty():
        profiles = profiles.sort("mat_source", descending=True).unique(["issuer_id", "mat_period_end"], keep="first")
    try:
        bonds = read_table("bond_reference", columns=["issuer_id", "maturity_date"], settings=s).filter(
            pl.col("issuer_id").is_not_null() & (pl.col("maturity_date").is_null() | (pl.col("maturity_date") >= date.today())))
    except Exception:  # noqa: BLE001
        bonds = None
    rates = market_rates(s)
    cd = build_company_debt(panel, profiles, bonds, rates)
    n = write_table(cd, "company_debt", mode="overwrite", settings=s) if cd.height else 0
    walls = build_walls(cd, issuer_meta(s), date.today()) if cd.height else pl.DataFrame()
    m = write_table(walls, "debt_maturity_walls", mode="overwrite", settings=s) if walls.height else 0
    cov = {"issuers": int(cd["issuer_id"].n_unique()) if cd.height else 0,
           "issuers_with_maturities": int(cd.filter(pl.col("mat_source") != "none")["issuer_id"].n_unique()) if cd.height else 0,
           "issuers_with_floating": int(cd.filter(pl.col("floating_share").is_not_null())["issuer_id"].n_unique()) if cd.height else 0,
           "issuers_with_cost": int(cd.filter(pl.col("avg_cost_of_debt").is_not_null())["issuer_id"].n_unique()) if cd.height else 0,
           "rows": n, "walls": m, "sec_fact_rows": facts.height, "seconds": round(time.perf_counter() - t0, 1),
           "rates": rates}
    update_state({"company_debt": {"last_run": now_utc().isoformat(timespec="seconds"), **{k: v for k, v in cov.items() if k != "rates"}}}, s)
    return cov


def run(ctx):  # noqa: ANN001
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_company_debt(ctx.settings, dict(ctx.params or {}))
    return JobResult(status="success" if rep["rows"] else "partial",
                     rows_written={"company_debt": rep["rows"], "debt_maturity_walls": rep["walls"]},
                     entities_ok=rep["issuers"],
                     message=f"{rep['issuers']} issuers; maturities {rep['issuers_with_maturities']}, "
                             f"floating {rep['issuers_with_floating']}, cost of debt {rep['issuers_with_cost']}")
