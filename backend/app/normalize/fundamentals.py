"""fundamentals_raw (any source) -> fundamentals_canonical (long, PIT) -> fundamentals_wide. Owner: ref-identity.
Job: fundamentals_normalize -> run(ctx).

Pipeline (ARCHITECTURE §11, canonical_line_items.yaml conventions):
  1. select raw facts: no dimensions; consolidated preferred over parent-only (per issuer/source/filing);
     resolve issuer_id (identity.resolve_frame on source_entity_id, confidence >= 0.9).
  2. map to canonical items by the per-taxonomy tag chain (first tag = 'reported', later tags = 'fallback_tag';
     'A + B' / nz(...) chain entries are evaluated with app.normalize.formula); source precedence per region (§8);
     unit filter (money = ISO ccy, per_share = 'CCY/shares', shares, pure) and dominant reporting currency per filing.
  3. sign normalization (positive / outflow_positive -> abs; us_gaap_negate -> -x; signed kept).
  4. PIT versions per (issuer, item, period): best (source rank, tag priority) per knowledge_date, lower-precedence
     sources never override a higher one already known, a new row only when the value moves > 1% (is_restatement).
  5. discrete periods from the fiscal calendar (FY facts): Q1..Q4 (Q2 = 6M-Q1, Q3 = 9M-6M, Q4 = FY-9M), H1/H2 for
     semiannual reporters (H2 = FY-H1), FY; INSTANT rows for balance-sheet items (dei cover facts snapped to the
     filing's period end). Derived periods are PIT: re-evaluated at every knowledge date of any component.
  6. FX (app.normalize.fx.convert_frame): INSTANT at period-end rate, flows at period-average.
  7. TTM: sum of the last 4 discrete quarters (local and USD separately: TTM USD = sum of quarter USD values);
     semiannual: H1 + previous H2; annual-only: FY. ttm_rule 'average' items (weighted shares) average instead.
  8. derived items ('derive' formulas) in dependency order on the as-known wide rows, only where the item is missing;
     banks: revenue = net_interest_income + noninterest_income when derivable (sector_kpis revenue_rule).
  9. data quality: dq_score = 1 - penalties (fallback 0.10, derived formula 0.15, derived period 0.05, estimated
     knowledge date 0.20, failed identity 0.30 (assets = liabilities + equity +-2%; CFO+CFI+CFF = d(cash) +-5% of
     assets), cross-source disagreement > 2% 0.10, sign sanity 0.30), clipped to [0,1].
 10. write fundamentals_canonical (rows of processed issuers replaced) and pivot to fundamentals_wide
     (LOCAL + USD; INSTANT items joined onto each FY/Q/H/TTM row with the same period_end; one row per knowledge date).

Public API
    normalize(issuer_ids=None, *, raw=None, issuers=None, rates=None, write=True, run_id=None, batch_size=40, settings=None)
        -> {"canonical": n_rows, "wide": n_rows, "issuers": n, "dq": {...}}
    map_raw_to_canonical(raw, issuers=None) -> pl.DataFrame          (steps 1-4; for tests / diagnostics)
    load_canonical() -> dict                                          (parsed contract)
    run(ctx) -> JobResult
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import time

import polars as pl
import yaml

from app.core.config import get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

# ------------------------------------------------------------------------------------------------ constants

FAMILY_OF_TAXONOMY = {"us-gaap": "us_gaap", "srt": "us_gaap", "ifrs-full": "ifrs", "jppfs": "jp", "jpcrp": "jp",
                      "jpigp": "jp", "dart": "kr", "kifrs": "kr", "cas_label": "cn", "cvm_account": "br_cvm",
                      "yfinance": "yfinance"}
GAAP_OF_TAXONOMY = {"us-gaap": "us_gaap", "srt": "us_gaap", "ifrs-full": "ifrs", "jppfs": "jgaap", "jpcrp": "jgaap",
                    "jpigp": "ifrs", "dart": "kifrs", "kifrs": "kifrs", "cas_label": "cas", "cvm_account": "brgaap",
                    "uk-core": "ukgaap", "yfinance": "other", "other": "other"}
SOURCE_ALIASES = {"sec": "sec_edgar", "sec_xbrl": "sec_edgar", "esef": "esef_xbrl_org", "cn_akshare": "akshare",
                  "yf_fundamentals": "yfinance", "yfinance_fund": "yfinance", "dart": "opendart"}
SOURCE_PRECEDENCE: dict[str, list[str]] = {
    "US": ["sec_edgar", "yfinance"],
    # coverage-B: national OAM ESEF (exact deposit timestamps) rank above the filings.xbrl.org mirror (date_added)
    "EU": ["dila_fr", "afm_nl", "fsma_be", "emarket_it", "nasdaq_nordic", "oslo_newsweb", "cnmv_es", "esef_xbrl_org", "fca_nsm", "sec_edgar", "companies_house", "yfinance"],
    "UK": ["esef_xbrl_org", "fca_nsm", "sec_edgar", "companies_house", "yfinance"],
    "JP": ["edinet", "sec_edgar", "yfinance"],
    "KR": ["opendart", "sec_edgar", "yfinance"],
    "CN": ["akshare", "sec_edgar", "yfinance"],
    "HK": ["akshare", "sec_edgar", "yfinance"],
    "LATAM": ["cvm", "biva_mx", "cmf_cl", "smv_pe", "sec_edgar", "yfinance"],
}
DEFAULT_PRECEDENCE = ["sec_edgar", "dila_fr", "afm_nl", "fsma_be", "emarket_it", "nasdaq_nordic", "oslo_newsweb", "cnmv_es", "esef_xbrl_org", "fca_nsm", "edinet", "opendart", "cvm", "biva_mx", "cmf_cl", "smv_pe", "akshare", "yfinance"]


def _insert_after(order: list[str], anchor: str, src: str) -> None:
    if src not in order and anchor in order:
        order.insert(order.index(anchor) + 1, src)


# coverage-A sources (additive): SEC Financial Statement Data Sets right after companyfacts (same filings, fills the
# IFRS-2025 gap of companyfacts); TDnet tanshin after EDINET; DART bulk zips after OpenDART; Taiwan MOPS first for APAC.
for _order in [*SOURCE_PRECEDENCE.values(), DEFAULT_PRECEDENCE]:
    _insert_after(_order, "sec_edgar", "sec_fsds")
    _insert_after(_order, "edinet", "tdnet")
    _insert_after(_order, "opendart", "dart_bulk")
if "mops" not in DEFAULT_PRECEDENCE:
    DEFAULT_PRECEDENCE.insert(DEFAULT_PRECEDENCE.index("yfinance"), "mops")
SOURCE_PRECEDENCE.setdefault("APAC", ["mops"] + [s for s in DEFAULT_PRECEDENCE if s != "mops"])
PENALTY = {"fallback_tag": 0.10, "derived_formula": 0.15, "derived_period": 0.05, "estimated": 0.20, "identity": 0.30,
           "disagree": 0.10, "sign": 0.30}
RESTATE_TOL = 0.01
# incremental run: raw rows with ingested_at > (last start - WM_OVERLAP), as prices_incremental. A writer stamps
# ingested_at shortly before its file becomes visible (edinet_listed: per partition file inside its commit), so rows
# published while the previous run was listing files would otherwise be skipped for good (COVERAGE_V3 B1 review).
# Re-normalising an issuer is idempotent; params.watermark_overlap_min overrides.
WM_OVERLAP = timedelta(minutes=15)
MONTH = 30.4375

CANON_COLS = ["issuer_id", "item_id", "fiscal_year", "fiscal_period", "period_kind", "period_start", "period_end", "value",
              "currency", "value_usd", "fx_rate", "fx_method", "fx_source", "gaap", "derivation", "source_concept",
              "source_id", "filing_id", "knowledge_date", "knowledge_date_estimated", "is_restatement", "dq_score",
              "ingested_at", "run_id"]

# internal long-row schema used between steps
_V = {"issuer_id": pl.Utf8, "item_id": pl.Utf8, "period_start": pl.Date, "period_end": pl.Date, "kd": pl.Date,
      "kd_est": pl.Boolean, "value": pl.Float64, "currency": pl.Utf8, "gaap": pl.Utf8, "derivation": pl.Utf8,
      "concept": pl.Utf8, "source_id": pl.Utf8, "filing_id": pl.Utf8, "rank": pl.Int32, "prio": pl.Int32}


# ------------------------------------------------------------------------------------------------ contract


def _norm_cn(label: str) -> str:
    return re.sub(r"[\s\u3000（）()：:、，,．.]", "", label or "")


_FUNCS = {"nz", "coalesce", "abs", "min", "max"}
_TOKEN = re.compile(r"[A-Za-z_\u0080-\uffff][\w:\-\.\u0080-\uffff]*")


def _is_expr(entry: str) -> bool:
    return bool(re.search(r"\s[+\-*/]\s|\(", entry))


def _expr_tags(entry: str) -> list[str]:
    out = []
    for m in _TOKEN.finditer(entry):
        t = m.group(0)
        nxt = entry[m.end():m.end() + 1]
        if t in _FUNCS and nxt == "(":
            continue
        out.append(t)
    return list(dict.fromkeys(out))


@lru_cache(maxsize=1)
def load_canonical() -> dict[str, Any]:
    path = get_settings().contracts_dir / "canonical_line_items.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = {it["id"]: it for it in doc["items"]}
    singles: list[dict[str, Any]] = []
    exprs: list[dict[str, Any]] = []
    for it in doc["items"]:
        for fam in ("us_gaap", "ifrs", "jp", "kr", "cn", "br_cvm", "yfinance"):
            chain = list(it.get(fam) or [])
            neg = list(it.get(f"{fam}_negate") or [])
            for prio, entry in enumerate(chain + neg):
                negate = prio >= len(chain)
                entry = str(entry)
                if _is_expr(entry):
                    tags = _expr_tags(entry)
                    exprs.append({"family": fam, "item_id": it["id"], "prio": prio, "expr": entry, "tags": tags,
                                  "negate": negate})
                else:
                    key = _norm_cn(entry) if fam == "cn" else entry
                    singles.append({"family": fam, "key": key, "item_id": it["id"], "prio": prio, "negate": negate})
    # supplementary global chains (owner ingest-global): appended AFTER each item's own chains (lowest precedence)
    ext_path = get_settings().contracts_dir / "canonical_line_items_ext_global.yaml"
    if ext_path.exists():
        ext = yaml.safe_load(ext_path.read_text(encoding="utf-8")) or {}
        fam_map = {"yfinance": "yfinance", "baostock": "baostock", "kr_label": "kr", "ifrs_uk": "ifrs_uk",
                   "tdnet": "tdnet", "tifrs": "tifrs", "cnmv_ipp": "cnmv_ipp"}
        own_len: dict[tuple[str, str], int] = {}
        for x in singles:
            k = (x["family"], x["item_id"])
            own_len[k] = max(own_len.get(k, 0), x["prio"] + 1)
        for x in exprs:
            k = (x["family"], x["item_id"])
            own_len[k] = max(own_len.get(k, 0), x["prio"] + 1)
        for iid, chains in (ext.get("items") or {}).items():
            if iid not in items:
                continue
            for efam, lst in (chains or {}).items():
                fam = fam_map.get(efam, efam)
                base = own_len.get((fam, iid), 0)
                for j, entry in enumerate(lst or []):
                    entry = str(entry)
                    prio = base + j + (1 if fam in ("kr",) else 0)
                    if _is_expr(entry):
                        exprs.append({"family": fam, "item_id": iid, "prio": max(prio, 1), "expr": entry,
                                      "tags": _expr_tags(entry), "negate": False})
                    else:
                        singles.append({"family": fam, "key": entry, "item_id": iid, "prio": max(prio, 1) if fam == "kr" else prio,
                                        "negate": False})
    unit_kind = {}
    for iid, it in items.items():
        u = it.get("unit")
        unit_kind[iid] = {"per_share": "per_share", "shares": "shares", "pure": "pure", "count": "count"}.get(u, "money")
    return {
        "doc": doc, "items": items, "item_ids": list(items),
        "singles": pl.DataFrame(singles, schema={"family": pl.Utf8, "key": pl.Utf8, "item_id": pl.Utf8, "prio": pl.Int32,
                                                 "negate": pl.Boolean}).unique(subset=["family", "key", "item_id"], keep="first"),
        "exprs": exprs, "unit_kind": unit_kind,
        "instant": {i for i, it in items.items() if it["period"] == "instant"},
        "duration": {i for i, it in items.items() if it["period"] == "duration"},
        "core": [i for i, it in items.items() if it.get("core")],
        "derive": {i: it["derive"] for i, it in items.items() if it.get("derive")},
        "sign": {i: it["sign"] for i, it in items.items()},
        "ttm_rule": {i: (it.get("ttm_rule") or ("sum" if it["period"] == "duration" else "none")) for i, it in items.items()},
        "applies_to": {i: list(it.get("applies_to") or []) for i, it in items.items()},
    }


# ------------------------------------------------------------------------------------------------ step 1-4


def _issuer_meta(issuers: pl.DataFrame | None, ids: Sequence[str] | None = None) -> pl.DataFrame:
    if issuers is None:
        from app.core.storage import read_table

        issuers = read_table("issuers")
    cols = ["issuer_id", "region", "reporting_gaap", "reporting_currency", "subsector_id", "fiscal_year_end_mmdd"]
    for c in cols:
        if c not in issuers.columns:
            issuers = issuers.with_columns(pl.lit(None, pl.Utf8).alias(c))
    out = issuers.select(cols).unique(subset=["issuer_id"])
    if ids is not None:
        out = out.filter(pl.col("issuer_id").is_in(list(ids)))
    return out


def resolve_issuers(raw: pl.DataFrame) -> pl.DataFrame:
    """Fill issuer_id from source_entity_id via identifier_map (only confident mappings)."""
    from app.normalize.identity import entity_id_type_expr, resolve_frame

    if raw.is_empty():
        return raw
    if "issuer_id" not in raw.columns:
        raw = raw.with_columns(pl.lit(None, pl.Utf8).alias("issuer_id"))
    if raw.get_column("issuer_id").null_count() == 0:
        return raw
    raw = raw.with_columns(entity_id_type_expr().alias("__idt"))
    try:
        raw = resolve_frame(raw, "source_entity_id", id_type_col="__idt", min_confidence=0.9, out_cols=("issuer_id",))
    except Exception as e:  # noqa: BLE001
        log.warning("issuer resolution failed: %s", e)
    return raw.drop("__idt")


def _prepare_facts(raw: pl.DataFrame) -> pl.DataFrame:
    """Filter + annotate raw facts: family, match key, gaap, cover-fact snapping."""
    if raw.is_empty():
        return raw
    raw = raw.filter(pl.col("issuer_id").is_not_null())
    # multi-class cover shares (dei EntityCommonStockSharesOutstanding by class) -> one summed fact per filing when
    # the filing has no undimensioned total
    is_sh = (pl.col("concept") == "EntityCommonStockSharesOutstanding") & (pl.col("taxonomy") == "dei")
    nodim = pl.col("dimensions_hash").is_null() | (pl.col("dimensions_hash") == "")
    dims = raw.filter(is_sh & ~nodim)
    if dims.height:
        tot = raw.filter(is_sh & nodim).select("issuer_id", "filing_id").unique()
        keys = [c for c in raw.columns if c not in ("value", "dimensions_hash", "dimensions_json", "label", "decimals", "frame", "is_latest")]
        summed = (dims.join(tot, on=["issuer_id", "filing_id"], how="anti").group_by(keys).agg(pl.col("value").sum())
                  .with_columns(pl.lit("").alias("dimensions_hash")))
        if summed.height:
            raw = pl.concat([raw, summed], how="diagonal_relaxed")
    df = raw.filter(nodim)
    if "is_forecast" in df.columns:  # company guidance (TDnet tanshin forecasts) is never an actual
        df = df.filter(~pl.col("is_forecast").fill_null(False))
    # PIT guard: a fact cannot be known before its period ends (issuer tagging errors, e.g. future dividend periods)
    bad = df.filter(pl.col("knowledge_date") < pl.col("period_end")).height
    if bad:
        log.info("fundamentals: dropped %d raw facts with knowledge_date < period_end (PIT violation)", bad)
        df = df.filter(pl.col("knowledge_date") >= pl.col("period_end"))
    df = df.with_columns(pl.col("source_id").replace(SOURCE_ALIASES).alias("source_id"))
    df = fix_yf_currency(df)
    # consolidated preferred: drop parent-only facts when the same filing has consolidated ones
    df = df.with_columns(pl.col("consolidated").fill_null(True))
    df = df.with_columns(pl.col("consolidated").any().over(["issuer_id", "source_id", "filing_id"]).alias("__has_cons"))
    df = df.filter(pl.col("consolidated") | ~pl.col("__has_cons")).drop("__has_cons")
    fam = (pl.when(pl.col("taxonomy") == "dei")
           .then(pl.when(pl.col("source_id") == "edinet").then(pl.lit("jp")).otherwise(pl.lit("us_gaap")))
           .when(pl.col("concept").str.starts_with("baostock:")).then(pl.lit("baostock"))
           .when(pl.col("concept").str.starts_with("tse-ed-t:")).then(pl.lit("tdnet"))
           .when(pl.col("concept").str.starts_with("tifrs-")).then(pl.lit("tifrs"))
           .when(pl.col("concept").str.starts_with("cnmv_ipp")).then(pl.lit("cnmv_ipp"))  # coverage-B: Spain IPP
           .when(pl.col("taxonomy") == "uk-core").then(pl.lit("ifrs_uk"))
           .otherwise(pl.col("taxonomy").replace_strict(FAMILY_OF_TAXONOMY, default=None)))
    df = df.with_columns(fam.alias("family"))
    key = (pl.when(pl.col("family") == "cn").then(pl.col("concept").map_elements(_norm_cn, return_dtype=pl.Utf8))
           .when(pl.col("concept").str.contains(":")).then(pl.col("concept"))
           .when(pl.col("taxonomy").is_in(["dei", "srt"])).then(pl.col("taxonomy") + pl.lit(":") + pl.col("concept"))
           .otherwise(pl.col("concept")))
    df = df.with_columns(key.alias("key"),
                         pl.col("taxonomy").replace_strict(GAAP_OF_TAXONOMY, default=None).alias("gaap"))
    # snap dei/cover instants (dated at the cover date) to the filing's report period end
    rep_end = (df.filter(pl.col("period_type") == "duration").group_by("filing_id")
               .agg(pl.col("period_end").max().alias("__rep_end")))
    df = df.join(rep_end, on="filing_id", how="left").with_columns(
        pl.when((pl.col("taxonomy") == "dei") & (pl.col("period_type") == "instant") & pl.col("__rep_end").is_not_null())
        .then(pl.col("__rep_end")).otherwise(pl.col("period_end")).alias("period_end")).drop("__rep_end")
    return df.filter(pl.col("family").is_not_null())


YF_CCY_LOG: dict[str, tuple[str, str]] = {}  # issuer_id -> (labelled, actual) yfinance statement currency


def fix_yf_currency(df: pl.DataFrame, tol: float = 5e-4, min_matches: int = 3) -> pl.DataFrame:
    """Yahoo labels statements with info.financialCurrency, but for some foreign SEC filers it serves the numbers of
    the USD 20-F (PETR4.SA: 'BRL' revenue 124.47bn == the 20-F's USD 124.47bn, vs ~490bn BRL in the CVM DFP). Such
    facts survive the one-currency filter under the wrong label and mix scales across years.

    Detection per issuer: yfinance money facts whose value equals (|ratio-1| < tol) a fact of another source for the
    same period_end in a different currency U, on >= min_matches distinct values, and more often than they equal
    same-currency facts -> relabel the issuer's yfinance units X -> U and 'X/shares' -> 'U/shares'."""
    if df.is_empty() or "unit" not in df.columns:
        return df
    money = pl.col("unit").str.contains(r"^[A-Z]{3}$") & (pl.col("value").abs() >= 1e7)
    yf = df.filter((pl.col("source_id") == "yfinance") & money).select("issuer_id", "period_end", "value",
                                                                         pl.col("unit").alias("__yu"))
    if yf.is_empty():
        return df
    ot = df.filter((pl.col("source_id") != "yfinance") & money & pl.col("issuer_id").is_in(yf["issuer_id"].unique().implode()))         .select("issuer_id", "period_end", pl.col("value").alias("__ov"), pl.col("unit").alias("__ou")).unique()
    if ot.is_empty():
        return df
    # only issuers where another source reports in a currency different from the yfinance label
    cand = ot.join(yf.select("issuer_id", "__yu").unique(), on="issuer_id").filter(pl.col("__ou") != pl.col("__yu"))
    if cand.is_empty():
        return df
    ids = cand["issuer_id"].unique().implode()
    m = (yf.filter(pl.col("issuer_id").is_in(ids)).join(ot, on=["issuer_id", "period_end"])
         .filter(((pl.col("value") / pl.col("__ov")) - 1).abs() < tol)
         .group_by("issuer_id", "__yu", "__ou").agg(pl.col("value").n_unique().alias("n")))
    fixes: dict[str, tuple[str, str]] = {}
    for iid, g in m.partition_by("issuer_id", as_dict=True).items():
        iid = iid[0] if isinstance(iid, tuple) else iid
        yu = g["__yu"][0]
        same = int(g.filter(pl.col("__ou") == yu)["n"].sum() or 0)
        other = g.filter(pl.col("__ou") != yu).sort("n", descending=True)
        if other.height and int(other["n"][0]) >= min_matches and int(other["n"][0]) > 2 * same:
            fixes[iid] = (yu, other["__ou"][0])
    if not fixes:
        return df
    YF_CCY_LOG.update(fixes)
    log.info("fundamentals: yfinance currency relabelled for %d issuers: %s", len(fixes),
             ", ".join(f"{k} {a}->{b}" for k, (a, b) in list(fixes.items())[:20]))
    fx = pl.DataFrame([{"issuer_id": k, "__from": a, "__to": b} for k, (a, b) in fixes.items()])
    out = df.join(fx, on="issuer_id", how="left")
    hit = (pl.col("source_id") == "yfinance") & pl.col("__to").is_not_null()
    unit = (pl.when(hit & (pl.col("unit") == pl.col("__from"))).then(pl.col("__to"))
            .when(hit & (pl.col("unit") == pl.col("__from") + "/shares")).then(pl.col("__to") + "/shares")
            .otherwise(pl.col("unit")))
    return out.with_columns(unit.alias("unit")).drop("__from", "__to")


def _unit_ok(kind: str) -> pl.Expr:
    u = pl.col("unit")
    if kind == "money":
        return u.str.contains(r"^[A-Za-z]{3}$")
    if kind == "per_share":
        return u.str.contains(r"^[A-Za-z]{3}/")
    if kind == "shares":
        return u.str.to_lowercase() == "shares"
    if kind == "pure":
        return u.str.to_lowercase().is_in(["pure", "percent", "ratio"])
    return pl.lit(True)


def _candidates(facts: pl.DataFrame, meta: pl.DataFrame) -> pl.DataFrame:
    """Facts -> candidate canonical rows (one per matching fact / evaluated chain expression)."""
    C = load_canonical()
    base_cols = ["issuer_id", "source_id", "filing_id", "family", "gaap", "period_type", "period_start", "period_end",
                 "unit", "knowledge_date", "knowledge_date_estimated", "form_type"]
    out: list[pl.DataFrame] = []
    single = facts.join(C["singles"], on=["family", "key"], how="inner")
    if single.height:
        out.append(single.select(base_cols + ["item_id", "prio", "negate", pl.col("concept"), "value"]))
    for e in C["exprs"]:
        sub = facts.filter((pl.col("family") == e["family"]) & pl.col("key").is_in(e["tags"]))
        if sub.is_empty():
            continue
        idx = [c for c in base_cols if c != "gaap"]
        wide = sub.with_columns(pl.col("gaap")).pivot(on="key", index=idx, values="value", aggregate_function="first")
        names = {t: f"t{i}" for i, t in enumerate(e["tags"])}
        expr = e["expr"]
        for t in sorted(names, key=len, reverse=True):
            expr = re.sub(rf"(?<![\w:\-\.]){re.escape(t)}(?![\w:\-\.])", names[t], expr)
        wide = wide.rename({t: n for t, n in names.items() if t in wide.columns})
        from app.normalize.formula import evaluate

        vals = evaluate(expr, wide)
        res = wide.with_columns(vals.alias("value")).filter(pl.col("value").is_not_null())
        if res.is_empty():
            continue
        g = sub.group_by("filing_id").agg(pl.col("gaap").first())
        res = res.join(g, on="filing_id", how="left")
        out.append(res.select(base_cols + [pl.lit(e["item_id"]).alias("item_id"), pl.lit(e["prio"], pl.Int32).alias("prio"),
                                           pl.lit(e["negate"]).alias("negate"), pl.lit(e["expr"]).alias("concept"), "value"]))
    if not out:
        return pl.DataFrame(schema=_V)
    cand = pl.concat(out, how="vertical_relaxed")
    # unit filter per item kind + currency
    kinds = cand.get_column("item_id").replace_strict(C["unit_kind"], default="money")
    cand = cand.with_columns(kinds.alias("__kind"))
    ok = (pl.when(pl.col("__kind") == "money").then(_unit_ok("money"))
          .when(pl.col("__kind") == "per_share").then(_unit_ok("per_share"))
          .when(pl.col("__kind") == "shares").then(_unit_ok("shares"))
          .when(pl.col("__kind") == "pure").then(_unit_ok("pure")).otherwise(True))
    cand = cand.filter(ok).with_columns(
        pl.when(pl.col("__kind") == "money").then(pl.col("unit").str.to_uppercase())
        .when(pl.col("__kind") == "per_share").then(pl.col("unit").str.split("/").list.first().str.to_uppercase())
        .otherwise(None).alias("currency"))
    # dominant currency per (issuer, filing): drop convenience translations
    dom = (cand.filter(pl.col("__kind") == "money").group_by(["issuer_id", "filing_id", "currency"]).len()
           .sort("len", descending=True).unique(subset=["issuer_id", "filing_id"], keep="first")
           .select("issuer_id", "filing_id", pl.col("currency").alias("__dom")))
    cand = cand.join(dom, on=["issuer_id", "filing_id"], how="left").filter(
        pl.col("currency").is_null() | pl.col("__dom").is_null() | (pl.col("currency") == pl.col("__dom")))
    cand = _one_currency(cand, meta)
    # sign normalization
    sign = cand.get_column("item_id").replace_strict(C["sign"], default="signed")
    cand = cand.with_columns(sign.alias("__sign")).with_columns(
        pl.when(pl.col("negate")).then(-pl.col("value"))
        .when(pl.col("__sign").is_in(["positive", "outflow_positive"])).then(pl.col("value").abs())
        .otherwise(pl.col("value")).alias("value"))
    # period-type consistency (instant items need instant facts, duration items duration facts)
    inst = list(C["instant"])
    cand = cand.filter(pl.when(pl.col("item_id").is_in(inst)).then(pl.col("period_type") == "instant")
                       .otherwise((pl.col("period_type") == "duration") & pl.col("period_start").is_not_null()))
    cand = cand.with_columns(pl.when(pl.col("period_type") == "instant").then(None).otherwise(pl.col("period_start"))
                             .cast(pl.Date).alias("period_start"))
    # source precedence rank by issuer region
    cand = cand.join(meta.select("issuer_id", "region"), on="issuer_id", how="left")
    rank_rows = []
    for reg in list(SOURCE_PRECEDENCE) + [None]:
        order = SOURCE_PRECEDENCE.get(reg or "", DEFAULT_PRECEDENCE)
        for i, src in enumerate(order):
            rank_rows.append({"region": reg, "source_id": src, "rank": i})
    ranks = pl.DataFrame(rank_rows, schema={"region": pl.Utf8, "source_id": pl.Utf8, "rank": pl.Int32})
    cand = cand.join(ranks, on=["region", "source_id"], how="left", nulls_equal=True).with_columns(
        pl.col("rank").fill_null(50).cast(pl.Int32))
    return cand.select(
        "issuer_id", "item_id", "period_start", "period_end", pl.col("knowledge_date").alias("kd"),
        pl.col("knowledge_date_estimated").fill_null(False).alias("kd_est"), "value", "currency", "gaap",
        pl.when(pl.col("concept").str.contains("SummaryOfBusinessResults")).then(pl.lit("source_summary"))
        .when(pl.col("prio") == 0).then(pl.lit("reported")).otherwise(pl.lit("fallback_tag")).alias("derivation"),
        "concept", "source_id", "filing_id", "rank", pl.col("prio").cast(pl.Int32),
    ).cast(_V)  # type: ignore[arg-type]


CCY_LOG: dict[str, int] = {}


def _one_currency(cand: pl.DataFrame, meta: pl.DataFrame) -> pl.DataFrame:
    """One reporting currency per issuer: the dominant money currency of the highest-precedence source present
    (e.g. Petrobras: CVM BRL beats the USD 20-F). Money/per-share candidates in other currencies are dropped
    (counted in CCY_LOG) so LOCAL rows never mix currencies."""
    m = cand.filter(pl.col("currency").is_not_null())
    if m.is_empty():
        return cand
    m = m.join(meta.select("issuer_id", "region"), on="issuer_id", how="left")
    order = []
    for reg in list(SOURCE_PRECEDENCE) + [None]:
        for i, src in enumerate(SOURCE_PRECEDENCE.get(reg or "", DEFAULT_PRECEDENCE)):
            order.append({"region": reg, "source_id": src, "__rk": i})
    rk = pl.DataFrame(order, schema={"region": pl.Utf8, "source_id": pl.Utf8, "__rk": pl.Int32})
    m = m.join(rk, on=["region", "source_id"], how="left", nulls_equal=True).with_columns(pl.col("__rk").fill_null(50))
    best = m.filter(pl.col("__rk") == pl.col("__rk").min().over("issuer_id"))
    ic = (best.group_by(["issuer_id", "currency"]).len().sort("len", descending=True)
          .unique(subset=["issuer_id"], keep="first").select("issuer_id", pl.col("currency").alias("__icc")))
    out = cand.join(ic, on="issuer_id", how="left")
    mism = pl.col("currency").is_not_null() & pl.col("__icc").is_not_null() & (pl.col("currency") != pl.col("__icc"))
    bad = out.filter(mism)
    for r in bad.group_by("issuer_id").len().iter_rows(named=True):
        CCY_LOG[r["issuer_id"]] = CCY_LOG.get(r["issuer_id"], 0) + r["len"]
    return out.filter(~mism).drop("__icc")


def _versions(cand: pl.DataFrame, keys: Sequence[str] = ("issuer_id", "item_id", "period_start", "period_end")) -> pl.DataFrame:
    """PIT version series: best candidate per knowledge date, monotone source precedence, >1% change => new row."""
    if cand.is_empty():
        return cand.with_columns(pl.lit(False).alias("is_restatement"), pl.lit(False).alias("disagree"))
    keys = list(keys)
    # cross-source disagreement (> 2%) flag on the group (latest value per source)
    lat = cand.sort("kd").group_by(keys + ["source_id"]).agg(pl.col("value").last())
    dis = lat.group_by(keys).agg(pl.col("value").max().alias("__mx"), pl.col("value").min().alias("__mn"), pl.len().alias("__n"))
    dis = dis.with_columns(((pl.col("__n") > 1) & ((pl.col("__mx") - pl.col("__mn")).abs()
                                                    > 0.02 * pl.max_horizontal(pl.col("__mx").abs(), pl.col("__mn").abs()))).alias("disagree")
                           ).select(keys + ["disagree"])
    best = cand.sort(keys + ["kd", "rank", "prio"], nulls_last=True).unique(subset=keys + ["kd"], keep="first",
                                                                              maintain_order=True)
    best = best.with_columns(pl.col("rank").cum_min().over(keys).alias("__runmin"))
    best = best.filter(pl.col("rank") <= pl.col("__runmin")).drop("__runmin")
    prev = pl.col("value").shift(1).over(keys)
    changed = prev.is_null() | ((pl.col("value") - prev).abs() > RESTATE_TOL * pl.max_horizontal(prev.abs(), pl.lit(1e-9)))
    best = best.with_columns(changed.alias("__keep"), prev.is_not_null().alias("is_restatement"))
    best = best.filter(pl.col("__keep")).drop("__keep")
    return best.join(dis, on=keys, how="left").with_columns(pl.col("disagree").fill_null(False))


def map_raw_to_canonical(raw: pl.DataFrame, issuers: pl.DataFrame | None = None) -> pl.DataFrame:
    """Steps 1-4: raw facts -> versioned candidate rows (period_start/period_end as reported, not yet discretized)."""
    raw = resolve_issuers(raw)
    facts = _prepare_facts(raw)
    if facts.is_empty():
        return pl.DataFrame(schema=_V)
    meta = _issuer_meta(issuers, facts.get_column("issuer_id").unique().to_list())
    return _versions(_candidates(facts, meta))


# ------------------------------------------------------------------------------------------------ step 5: periods


def _fy_calendar(vers: pl.DataFrame, meta: pl.DataFrame) -> pl.DataFrame:
    """Per issuer fiscal years (fy_start, fy_end, fiscal_year) from FY-length duration facts, extended one year
    forward (in-progress year) and backward; fallback to issuers.fiscal_year_end_mmdd or Dec."""
    d = vers.filter(pl.col("period_start").is_not_null()).with_columns(
        ((pl.col("period_end") - pl.col("period_start")).dt.total_days() + 1).alias("__days"))
    # candidate fiscal years = 12-month durations, weighted by statement breadth (distinct items). True FY statements
    # carry dozens of items; TTM disclosures in 10-Qs (e.g. Amazon's trailing FCF) carry a handful and must not
    # become fiscal-year ends.
    fy = (d.filter((pl.col("__days") >= 350) & (pl.col("__days") <= 380))
          .group_by(["issuer_id", "period_end"]).agg(pl.col("period_start").mode().first().alias("fy_start"),
                                                     pl.col("item_id").n_unique().alias("__n"))
          .rename({"period_end": "fy_end"}))
    rows: list[dict[str, Any]] = []
    have = set()
    for (iid,), g in fy.group_by(["issuer_id"], maintain_order=True):
        mx = g.get_column("__n").max() or 0
        g = g.filter(pl.col("__n") >= max(3, int(0.25 * mx))).sort("fy_end")
        kept: list[dict[str, Any]] = []
        for r in g.iter_rows(named=True):
            if kept and (r["fy_end"] - kept[-1]["fy_end"]).days < 300:
                if r["__n"] > kept[-1]["__n"]:
                    kept[-1] = r          # same fiscal year slot: keep the broader statement
                continue
            kept.append(r)
        if not kept:
            continue
        ends = [(r["fy_end"], r["fy_start"]) for r in kept]
        for e, st in ends:
            rows.append({"issuer_id": iid, "fy_start": st, "fy_end": e})
        # fill gaps (missing years) with synthetic years of the same calendar
        for (e0, _), (e1, _) in zip(ends, ends[1:]):
            k = 1
            while (e1 - _add_years(e0, k)).days > 200:
                rows.append({"issuer_id": iid, "fy_start": _add_years(e0, k - 1) + timedelta(days=1), "fy_end": _add_years(e0, k)})
                k += 1
        last_e = ends[-1][0]
        first_s = ends[0][1]
        for k in (1, 2):
            rows.append({"issuer_id": iid, "fy_start": _add_years(last_e, k - 1) + timedelta(days=1), "fy_end": _add_years(last_e, k)})
        for k in (1, 2, 3):
            rows.append({"issuer_id": iid, "fy_start": _add_years(first_s, -k), "fy_end": _add_years(first_s, -k + 1) - timedelta(days=1)})
        have.add(iid)
    # issuers without FY facts: from fiscal_year_end_mmdd (default 12-31)
    for iid in set(vers.get_column("issuer_id").unique().to_list()) - have:
        mmdd = (meta.filter(pl.col("issuer_id") == iid).get_column("fiscal_year_end_mmdd").to_list() or [None])[0] or "12-31"
        mm, dd = (int(x) for x in mmdd.split("-"))
        for y in range(1995, date.today().year + 2):
            e = _safe_date(y, mm, dd)
            rows.append({"issuer_id": iid, "fy_start": _add_years(e, -1) + timedelta(days=1), "fy_end": e})
    cal = pl.DataFrame(rows, schema={"issuer_id": pl.Utf8, "fy_start": pl.Date, "fy_end": pl.Date})
    cal = cal.unique(subset=["issuer_id", "fy_end"]).sort(["issuer_id", "fy_end"])
    # drop synthetic years overlapping a detected one (keep first occurrence in greedy end order)
    keep = []
    for (iid,), g in cal.group_by(["issuer_id"], maintain_order=True):
        last = None
        for r in g.sort("fy_end").iter_rows(named=True):
            if last is None or (r["fy_end"] - last).days >= 300:
                keep.append(r)
                last = r["fy_end"]
    cal = pl.DataFrame(keep, schema={"issuer_id": pl.Utf8, "fy_start": pl.Date, "fy_end": pl.Date})
    return cal.with_columns(pl.col("fy_end").dt.year().cast(pl.Int16).alias("fiscal_year"))


def _safe_date(y: int, m: int, d: int) -> date:
    import calendar

    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def _add_years(d: date, n: int) -> date:
    return _safe_date(d.year + n, d.month, d.day)


def _assign_spans(vers: pl.DataFrame, cal: pl.DataFrame) -> pl.DataFrame:
    """Attach fiscal_year + span (Q1..Q4, YTD6, YTD9, FY, H2, I3/I6/I9/I12 for instants) to versioned rows."""
    calj = cal.sort("fy_start")
    dur = vers.filter(pl.col("period_start").is_not_null())
    inst = vers.filter(pl.col("period_start").is_null())
    out = []
    if dur.height:
        d = dur.with_columns((pl.col("period_start") + pl.duration(days=15)).alias("__ps_adj")).sort("__ps_adj")
        d = d.join_asof(calj, left_on="__ps_adj", right_on="fy_start", by="issuer_id", strategy="backward",
                        check_sortedness=False)
        d = d.filter(pl.col("fy_end").is_not_null() & (pl.col("period_end") <= pl.col("fy_end") + pl.duration(days=15)))
        d = d.with_columns(
            (((pl.col("period_end") - pl.col("period_start")).dt.total_days() + 1) / MONTH).alias("__len"),
            ((pl.col("period_start") - pl.col("fy_start")).dt.total_days() / MONTH).alias("__off"))
        d = d.with_columns(pl.col("__len").round(0).cast(pl.Int32).alias("__lm"), pl.col("__off").round(0).cast(pl.Int32).alias("__om"),
                           ((pl.col("__len") - pl.col("__len").round(0)).abs() * MONTH).alias("__lerr"))
        span = (pl.when((pl.col("__lm") == 3) & (pl.col("__om") == 0)).then(pl.lit("Q1"))
                .when((pl.col("__lm") == 3) & (pl.col("__om") == 3)).then(pl.lit("Q2"))
                .when((pl.col("__lm") == 3) & (pl.col("__om") == 6)).then(pl.lit("Q3"))
                .when((pl.col("__lm") == 3) & (pl.col("__om") == 9)).then(pl.lit("Q4"))
                .when((pl.col("__lm") == 6) & (pl.col("__om") == 0)).then(pl.lit("YTD6"))
                .when((pl.col("__lm") == 6) & (pl.col("__om") == 6)).then(pl.lit("H2"))
                .when((pl.col("__lm") == 9) & (pl.col("__om") == 0)).then(pl.lit("YTD9"))
                .when((pl.col("__lm") == 12) & (pl.col("__om") == 0)).then(pl.lit("FY"))
                .otherwise(None))
        d = d.with_columns(span.alias("span")).filter(pl.col("span").is_not_null() & (pl.col("__lerr") <= 20))
        out.append(d.drop(["__ps_adj", "__len", "__off", "__lm", "__om", "__lerr"]))
    if inst.height:
        i = inst.with_columns((pl.col("period_end") - pl.duration(days=15)).alias("__pe_adj")).sort("__pe_adj")
        i = i.join_asof(cal.sort("fy_end"), left_on="__pe_adj", right_on="fy_end", by="issuer_id", strategy="forward",
                        check_sortedness=False)
        i = i.filter(pl.col("fy_start").is_not_null() & (pl.col("period_end") >= pl.col("fy_start") - pl.duration(days=15)))
        i = i.with_columns((((pl.col("period_end") - pl.col("fy_start")).dt.total_days() + 1) / MONTH).alias("__m"))
        i = i.with_columns(pl.col("__m").round(0).cast(pl.Int32).alias("__mr"),
                           ((pl.col("__m") - pl.col("__m").round(0)).abs() * MONTH).alias("__err"))
        i = i.filter(pl.col("__mr").is_in([3, 6, 9, 12]) & (pl.col("__err") <= 20)).with_columns(
            (pl.lit("I") + pl.col("__mr").cast(pl.Utf8)).alias("span"))
        out.append(i.drop(["__pe_adj", "__m", "__mr", "__err"]))
    if not out:
        return vers.with_columns(pl.lit(None, pl.Utf8).alias("span"), pl.lit(None, pl.Int16).alias("fiscal_year"),
                                 pl.lit(None, pl.Date).alias("fy_start"), pl.lit(None, pl.Date).alias("fy_end"))
    return pl.concat(out, how="vertical_relaxed")


_SERIES = ["kd", "value", "kd_est"]


def _combine(key_cols: list[str], parts: list[tuple[pl.DataFrame, float]], mode: str = "sum") -> pl.DataFrame:
    """PIT combination of version series: timeline = union of component knowledge dates; at each date every component
    takes its latest version (as-of); value = sum(coef * v) (mode 'sum') or mean (mode 'mean'); null if any missing.
    Consecutive identical values are collapsed. Each part: key_cols + kd + value + kd_est (+ value_usd optional)."""
    if not parts or any(p.is_empty() for p, _ in parts):
        return pl.DataFrame(schema={**{k: pl.Utf8 for k in key_cols}, "kd": pl.Date, "value": pl.Float64, "kd_est": pl.Boolean})
    has_usd = all("value_usd" in p.columns for p, _ in parts)
    tl = pl.concat([p.select(key_cols + ["kd"]) for p, _ in parts], how="vertical_relaxed").unique().sort("kd")
    acc = tl
    for i, (p, _) in enumerate(parts):
        cols = key_cols + ["kd", pl.col("value").alias(f"__v{i}"), pl.col("kd_est").alias(f"__e{i}")]
        if has_usd:
            cols.append(pl.col("value_usd").alias(f"__u{i}"))
        pi = p.select(cols).sort("kd")
        acc = acc.sort("kd").join_asof(pi, on="kd", by=key_cols, strategy="backward", check_sortedness=False)
    n = len(parts)
    vs = [pl.col(f"__v{i}") * c for i, (_, c) in enumerate(parts)]
    val = pl.sum_horizontal(vs)
    if mode == "mean":
        val = val / n
    nulls = pl.any_horizontal([pl.col(f"__v{i}").is_null() for i in range(n)])
    acc = acc.with_columns(pl.when(nulls).then(None).otherwise(val).alias("value"),
                           pl.any_horizontal([pl.col(f"__e{i}").fill_null(False) for i in range(n)]).alias("kd_est"))
    if has_usd:
        uv = pl.sum_horizontal([pl.col(f"__u{i}") * c for i, (_, c) in enumerate(parts)])
        if mode == "mean":
            uv = uv / n
        unull = pl.any_horizontal([pl.col(f"__u{i}").is_null() for i in range(n)])
        acc = acc.with_columns(pl.when(unull | nulls).then(None).otherwise(uv).alias("value_usd"))
    acc = acc.filter(pl.col("value").is_not_null()).sort(key_cols + ["kd"])
    prev = pl.col("value").shift(1).over(key_cols)
    acc = acc.filter(prev.is_null() | ((pl.col("value") - prev).abs() > 1e-6 * pl.max_horizontal(prev.abs(), pl.lit(1.0))))
    keep = key_cols + ["kd", "value", "kd_est"] + (["value_usd"] if has_usd else [])
    return acc.select(keep)


def _discrete(spans: pl.DataFrame) -> pl.DataFrame:
    """Build discrete FY / Q / H rows (+ INSTANT rows) with PIT-correct derived periods."""
    C = load_canonical()
    if spans.is_empty():
        return spans
    K = ["issuer_id", "item_id", "fiscal_year"]
    meta_cols = ["currency", "gaap", "source_id", "concept", "filing_id", "derivation", "disagree", "is_restatement"]
    dur = spans.filter(~pl.col("span").str.starts_with("I"))
    avg_items = [i for i, r in C["ttm_rule"].items() if r == "average"]
    out: list[pl.DataFrame] = []

    def series(span: str) -> pl.DataFrame:
        return dur.filter(pl.col("span") == span).select(K + ["kd", "value", "kd_est"])

    def present(span: str) -> pl.DataFrame:
        return dur.filter(pl.col("span") == span).select(K).unique()

    def derived(span: str, parts: list[tuple[str, float]], exclude: list[pl.DataFrame]) -> pl.DataFrame:
        ps = [(series(s), c) for s, c in parts]
        if any(p.is_empty() for p, _ in ps):
            return pl.DataFrame()
        res = _combine(K, ps).filter(~pl.col("item_id").is_in(avg_items))
        for ex in exclude:
            res = res.join(ex, on=K, how="anti")
        return res.with_columns(pl.lit(span).alias("span"))

    # direct rows keep their metadata
    direct = dur.with_columns(pl.col("span").alias("span"))
    out.append(direct)
    # derived quarters / halves
    dq2 = derived("Q2", [("YTD6", 1.0), ("Q1", -1.0)], [present("Q2")])
    dq3a = derived("Q3", [("YTD9", 1.0), ("YTD6", -1.0)], [present("Q3")])
    q3_have = pl.concat([present("Q3"), dq3a.select(K).unique()]) if dq3a.height else present("Q3")
    q2_all = pl.concat([series("Q2"), dq2.select(K + ["kd", "value", "kd_est"])]) if dq2.height else series("Q2")
    dq3b = pl.DataFrame()
    if q2_all.height:
        ps = [(series("YTD9"), 1.0), (series("Q1"), -1.0), (q2_all, -1.0)]
        if all(p.height for p, _ in ps):
            dq3b = _combine(K, ps).filter(~pl.col("item_id").is_in(avg_items)).join(q3_have, on=K, how="anti").with_columns(
                pl.lit("Q3").alias("span"))
    dq4a = derived("Q4", [("FY", 1.0), ("YTD9", -1.0)], [present("Q4")])
    q4_have = pl.concat([present("Q4"), dq4a.select(K).unique()]) if dq4a.height else present("Q4")
    q3_all = pl.concat([x.select(K + ["kd", "value", "kd_est"]) for x in (series("Q3"), dq3a, dq3b) if x.height]) \
        if any(x.height for x in (series("Q3"), dq3a, dq3b)) else pl.DataFrame()
    dq4b = pl.DataFrame()
    if q2_all.height and q3_all.height:
        ps = [(series("FY"), 1.0), (series("Q1"), -1.0), (q2_all, -1.0), (q3_all, -1.0)]
        if all(p.height for p, _ in ps):
            dq4b = _combine(K, ps).filter(~pl.col("item_id").is_in(avg_items)).join(q4_have, on=K, how="anti").with_columns(
                pl.lit("Q4").alias("span"))
    dh2 = derived("H2", [("FY", 1.0), ("YTD6", -1.0)], [present("H2")])
    for d in (dq2, dq3a, dq3b, dq4a, dq4b, dh2):
        if d.height:
            out.append(d.with_columns(pl.lit("derived_period").alias("derivation")))
    allr = pl.concat(out, how="diagonal_relaxed")
    # fill metadata of derived rows from the item's direct rows in the same fiscal year
    md = dur.sort("kd").group_by(K).agg([pl.col(c).last().alias(f"__m_{c}") for c in ("currency", "gaap", "source_id")])
    allr = allr.join(md, on=K, how="left").with_columns(
        [pl.coalesce(pl.col(c), pl.col(f"__m_{c}")).alias(c) for c in ("currency", "gaap", "source_id")]
    ).drop([f"__m_{c}" for c in ("currency", "gaap", "source_id")])
    allr = allr.with_columns(pl.col("disagree").fill_null(False), pl.col("is_restatement").fill_null(False))
    # semiannual reporter years: no quarterly spans in that fiscal year
    qyears = allr.filter(pl.col("span").is_in(["Q1", "Q3", "YTD9"])).select("issuer_id", "fiscal_year").unique().with_columns(
        pl.lit(True).alias("__quarterly"))
    allr = allr.join(qyears, on=["issuer_id", "fiscal_year"], how="left").with_columns(pl.col("__quarterly").fill_null(False))
    kind = (pl.when(pl.col("span") == "FY").then(pl.lit("FY"))
            .when(pl.col("span").is_in(["Q1", "Q2", "Q3", "Q4"])).then(pl.lit("Q"))
            .when((pl.col("span") == "YTD6") & ~pl.col("__quarterly")).then(pl.lit("H"))
            .when((pl.col("span") == "H2") & ~pl.col("__quarterly")).then(pl.lit("H"))
            .otherwise(None))
    fp = (pl.when(pl.col("span") == "YTD6").then(pl.lit("H1")).otherwise(pl.col("span")))
    allr = allr.with_columns(kind.alias("period_kind"), fp.alias("fiscal_period")).filter(pl.col("period_kind").is_not_null())
    # period bounds from the fiscal calendar for derived rows
    fyb = spans.select("issuer_id", "fiscal_year", pl.col("fy_start").alias("__fs"), pl.col("fy_end").alias("__fe")).unique(
        subset=["issuer_id", "fiscal_year"])
    allr = allr.join(fyb, on=["issuer_id", "fiscal_year"], how="left").with_columns(
        pl.coalesce("fy_start", "__fs").alias("fy_start"), pl.coalesce("fy_end", "__fe").alias("fy_end")).drop(["__fs", "__fe"])
    # actual span boundaries reported by the issuer (mode over items) -> exact dates for derived spans (52/53-week years)
    bounds = (dur.group_by(["issuer_id", "fiscal_year", "span"]).agg(pl.col("period_end").mode().first().alias("__e"))
              .pivot(on="span", index=["issuer_id", "fiscal_year"], values="__e"))
    for sp in ("Q1", "Q2", "Q3", "Q4", "YTD6", "YTD9", "FY", "H2"):
        if sp not in bounds.columns:
            bounds = bounds.with_columns(pl.lit(None, pl.Date).alias(sp))
    bounds = bounds.select("issuer_id", "fiscal_year", *[pl.col(sp).alias(f"__b_{sp}") for sp in ("Q1", "Q2", "Q3", "Q4", "YTD6", "YTD9", "FY", "H2")])
    allr = allr.join(bounds, on=["issuer_id", "fiscal_year"], how="left")
    one = pl.duration(days=1)
    q2e = pl.coalesce("__b_Q2", "__b_YTD6")
    q3e = pl.coalesce("__b_Q3", "__b_YTD9")
    fye = pl.coalesce("__b_FY", "fy_end")
    d_start = (pl.when(pl.col("span") == "Q2").then(pl.col("__b_Q1") + one)
               .when(pl.col("span") == "Q3").then(q2e + one)
               .when(pl.col("span") == "Q4").then(q3e + one)
               .when(pl.col("span") == "H2").then(pl.col("__b_YTD6") + one))
    d_end = (pl.when(pl.col("span") == "Q2").then(q2e).when(pl.col("span") == "Q3").then(q3e)
             .when(pl.col("span").is_in(["Q4", "H2"])).then(pl.coalesce("__b_Q4", fye)))
    allr = allr.with_columns(
        pl.coalesce(pl.col("period_start"), d_start, _span_start()).alias("period_start"),
        pl.coalesce(pl.col("period_end"), d_end, _span_end()).alias("period_end"),
    ).drop(["__quarterly"] + [f"__b_{sp}" for sp in ("Q1", "Q2", "Q3", "Q4", "YTD6", "YTD9", "FY", "H2")])
    # instants
    inst = spans.filter(pl.col("span").str.starts_with("I"))
    if inst.height:
        qy = allr.filter(pl.col("period_kind") == "Q").select("issuer_id", "fiscal_year").unique().with_columns(pl.lit(True).alias("__q"))
        inst = inst.join(qy, on=["issuer_id", "fiscal_year"], how="left").with_columns(
            pl.lit("INSTANT").alias("period_kind"),
            pl.when(pl.col("span") == "I12").then(pl.lit("FY"))
            .when(pl.col("span") == "I3").then(pl.lit("Q1"))
            .when(pl.col("span") == "I9").then(pl.lit("Q3"))
            .when(pl.col("__q").fill_null(False)).then(pl.lit("Q2")).otherwise(pl.lit("H1")).alias("fiscal_period"),
            pl.lit(None, pl.Date).alias("period_start")).drop("__q")
        allr = pl.concat([allr, inst], how="diagonal_relaxed")
    # PIT invariant (W1 2026-10-31, dq ratios pit_violation 0.00105 > 0.001): a DERIVED duration period cannot be
    # known before it ends. Such rows come from 52/53-week issuers whose YTD facts of two different fiscal years
    # share a fiscal_year label (e.g. Q3 = YTD9 of the year before - YTD6 of this year, known in 2010 for a quarter
    # ending 2011-10-01): the value is garbage, not an early disclosure. 4,293 Q + 3,586 TTM canonical rows of 54
    # US issuers (MIDD, DAR, FOSL, JNJ, CDNS, ...) -> 16,983 ratios rows. Dropped here, so TTM / ratios never see them.
    bad = (pl.col("derivation") == "derived_period") & pl.col("kd").is_not_null() & pl.col("period_end").is_not_null() \
        & (pl.col("kd") < pl.col("period_end"))
    allr = allr.filter(~bad.fill_null(False))
    return allr.drop(["span"], strict=False)


def _span_start() -> pl.Expr:
    off = (pl.when(pl.col("span").is_in(["Q1", "YTD6", "YTD9", "FY"])).then(0)
           .when(pl.col("span") == "Q2").then(3).when(pl.col("span").is_in(["Q3", "H2"])).then(6)
           .when(pl.col("span") == "Q4").then(9).otherwise(0))
    return pl.col("fy_start").dt.offset_by(pl.format("{}mo", off))


def _span_end() -> pl.Expr:
    end_m = (pl.when(pl.col("span") == "Q1").then(3).when(pl.col("span").is_in(["Q2", "YTD6"])).then(6)
             .when(pl.col("span").is_in(["Q3", "YTD9"])).then(9).otherwise(12))
    calc = pl.col("fy_start").dt.offset_by(pl.format("{}mo", end_m)) - pl.duration(days=1)
    return pl.when(pl.col("span").is_in(["Q4", "H2", "FY"])).then(pl.col("fy_end")).otherwise(calc)


# ------------------------------------------------------------------------------------------------ step 7: TTM


def _ttm(disc: pl.DataFrame) -> pl.DataFrame:
    C = load_canonical()
    rules = C["ttm_rule"]
    sum_items = [i for i, r in rules.items() if r == "sum"]
    avg_items = [i for i, r in rules.items() if r == "average"]
    flows = disc.filter(pl.col("period_kind").is_in(["Q", "H", "FY"]) & pl.col("item_id").is_in(sum_items + avg_items))
    if flows.is_empty():
        return pl.DataFrame()
    out = []
    K = ["issuer_id", "item_id", "anchor"]
    q = flows.filter(pl.col("period_kind") == "Q").with_columns(
        (pl.col("fiscal_year").cast(pl.Int32) * 4 + pl.col("fiscal_period").str.slice(1, 1).cast(pl.Int32) - 1).alias("__idx"))
    if q.height:
        parts = []
        for lag in range(4):
            parts.append((q.with_columns((pl.col("__idx") + lag).alias("anchor")).select(K + ["kd", "value", "kd_est", "value_usd"]), 1.0))
        s = _combine(K, parts).join(q.filter(pl.col("item_id").is_in(sum_items)).select("issuer_id", "item_id").unique(),
                                    on=["issuer_id", "item_id"], how="semi")
        a = _combine(K, parts, mode="mean").join(q.filter(pl.col("item_id").is_in(avg_items)).select("issuer_id", "item_id").unique(),
                                                 on=["issuer_id", "item_id"], how="semi")
        ttm = pl.concat([x for x in (s, a) if x.height], how="vertical_relaxed") if (s.height or a.height) else pl.DataFrame()
        if ttm.height:
            anchors = q.select("issuer_id", "item_id", pl.col("__idx").alias("anchor"), "period_end", "fiscal_year", "currency",
                               "gaap", "source_id").unique(subset=["issuer_id", "item_id", "anchor"])
            starts = q.select("issuer_id", "item_id", (pl.col("__idx") + 3).alias("anchor"), pl.col("period_start").alias("__ps")).unique(
                subset=["issuer_id", "item_id", "anchor"])
            ttm = ttm.join(anchors, on=K, how="inner").join(starts, on=K, how="left")
            out.append(ttm.with_columns(pl.col("__ps").alias("period_start")).drop(["__ps", "anchor"]))
    # semiannual: H1 + previous H2 ; FY-anchored TTM for issuers/years without quarters
    h = flows.filter(pl.col("period_kind") == "H").with_columns(
        (pl.col("fiscal_year").cast(pl.Int32) * 2 + pl.col("fiscal_period").str.slice(1, 1).cast(pl.Int32) - 1).alias("__idx"))
    if h.height:
        parts = [(h.with_columns((pl.col("__idx") + lag).alias("anchor")).select(K + ["kd", "value", "kd_est", "value_usd"]), 1.0)
                 for lag in range(2)]
        hs = _combine(K, parts)
        if hs.height:
            anchors = h.filter(pl.col("fiscal_period") == "H1").select("issuer_id", "item_id", pl.col("__idx").alias("anchor"),
                                                                     "period_end", "fiscal_year", "currency", "gaap", "source_id").unique(
                subset=["issuer_id", "item_id", "anchor"])
            hs = hs.join(anchors, on=K, how="inner").drop("anchor").with_columns(
                (pl.col("period_end").dt.offset_by("-1y") + pl.duration(days=1)).alias("period_start"))
            out.append(hs)
    fy = flows.filter(pl.col("period_kind") == "FY")
    if fy.height:
        have = pl.concat([x.select("issuer_id", "item_id", "period_end") for x in out], how="vertical_relaxed") if out else None
        f = fy.select("issuer_id", "item_id", "kd", "value", "kd_est", "value_usd", "period_end", "period_start", "fiscal_year",
                      "currency", "gaap", "source_id")
        if have is not None and have.height:
            f = f.join(have.unique(), on=["issuer_id", "item_id", "period_end"], how="anti")
        out.append(f)
    if not out:
        return pl.DataFrame()
    res = pl.concat(out, how="diagonal_relaxed")
    return res.with_columns(pl.lit("TTM").alias("period_kind"), pl.lit("TTM").alias("fiscal_period"),
                            pl.lit("ttm_sum").alias("derivation"), pl.lit(None, pl.Utf8).alias("filing_id"),
                            pl.lit("ttm").alias("concept"), pl.lit(False).alias("disagree"), pl.lit(False).alias("is_restatement"))


# ------------------------------------------------------------------------------------------------ step 8: wide + derive

KEYW = ["issuer_id", "period_kind", "period_end"]


def _wide_events(long: pl.DataFrame, value_col: str, kinds: Sequence[str]) -> pl.DataFrame:
    """As-known wide rows: one row per (issuer, period_kind, period_end, kd), items as columns (forward-filled across
    knowledge dates). INSTANT items are attached to every duration-kind row with the same period_end."""
    C = load_canonical()
    frames = []
    inst = long.filter(pl.col("period_kind") == "INSTANT")
    for k in kinds:
        if k == "INSTANT":
            base = inst
        else:
            dur = long.filter(pl.col("period_kind") == k)
            if dur.is_empty():
                continue
            keys = dur.select("issuer_id", "period_end").unique()
            base = pl.concat([dur, inst.join(keys, on=["issuer_id", "period_end"], how="semi").with_columns(pl.lit(k).alias("period_kind"))],
                             how="vertical_relaxed")
        if base.is_empty():
            continue
        frames.append(base.select(KEYW + ["kd", "item_id", value_col]))
    if not frames:
        return pl.DataFrame()
    ev = pl.concat(frames, how="vertical_relaxed").unique(subset=KEYW + ["kd", "item_id"], keep="last")
    w = ev.pivot(on="item_id", index=KEYW + ["kd"], values=value_col, aggregate_function="first").sort(KEYW + ["kd"])
    items = [c for c in w.columns if c in C["items"]]
    return w.with_columns([pl.col(c).forward_fill().over(KEYW) for c in items])


def _derive(long: pl.DataFrame, meta: pl.DataFrame) -> pl.DataFrame:
    """Derived-formula items (local and USD) where the item is missing; returns new long rows."""
    from app.normalize.formula import compile_expr, dependencies

    C = load_canonical()
    if long.is_empty():
        return pl.DataFrame()
    banks = set(meta.filter(pl.col("subsector_id").is_in(["banks_global", "banks_regional"])).get_column("issuer_id").to_list())
    groups_of = {}
    from app.normalize.taxonomy import subsector_groups

    for r in meta.iter_rows(named=True):
        groups_of[r["issuer_id"]] = set(subsector_groups(r.get("subsector_id"))) | ({r["reporting_gaap"]} if r.get("reporting_gaap") else set())
    order = _derive_order()
    res = []
    for basis, vcol in (("LOCAL", "value"), ("USD", "value_usd")):
        for kinds, items in ((["INSTANT"], [i for i in order if i in C["instant"]]),
                             (["FY", "Q", "H", "TTM"], [i for i in order if i in C["duration"]])):
            w = _wide_events(long, vcol, kinds)
            if w.is_empty():
                continue
            if kinds != ["INSTANT"]:
                w = w.filter(pl.col("period_kind") != "INSTANT")
            orig = set(w.columns)
            for _pass in range(2):
                for item in items:
                    expr = C["derive"][item]
                    deps = dependencies(expr)
                    for dname in deps | {item}:
                        if dname not in w.columns:
                            w = w.with_columns(pl.lit(None, pl.Float64).alias(dname))
                    e = compile_expr(expr)
                    app = C["applies_to"].get(item) or []
                    # never fabricate a value from nothing: nz()/coalesce() sums need at least one real input
                    dl = sorted(deps)
                    has_nonzero = pl.any_horizontal([pl.col(dn).is_not_null() & (pl.col(dn) != 0) for dn in dl])
                    any_missing = pl.any_horizontal([pl.col(dn).is_null() for dn in dl])
                    # insufficient evidence: only zero-valued inputs while others are missing -> leave null
                    cond = pl.col(item).is_null() & pl.any_horizontal([pl.col(dn).is_not_null() for dn in dl]) & (has_nonzero | ~any_missing)
                    if item == "revenue" and banks:
                        cond = cond | pl.col("issuer_id").is_in(list(banks))
                    if app:
                        ok_ids = [i for i, g in groups_of.items() if g & set(app)]
                        cond = cond & pl.col("issuer_id").is_in(ok_ids)
                    w = w.with_columns(pl.when(cond & e.is_not_null()).then(e).otherwise(pl.col(item)).alias(item),
                                       pl.when(cond & e.is_not_null()).then(pl.lit(True)).otherwise(
                                           pl.col(f"__d_{item}") if f"__d_{item}" in w.columns else pl.lit(False)).alias(f"__d_{item}"))
            for item in items:
                flag = f"__d_{item}"
                if flag not in w.columns:
                    continue
                d = w.filter(pl.col(flag)).select(KEYW + ["kd", pl.col(item).alias("v")]).with_columns(pl.lit(item).alias("item_id"))
                if d.is_empty():
                    continue
                d = d.sort(KEYW + ["kd"])
                prev = pl.col("v").shift(1).over(KEYW + ["item_id"])
                d = d.filter(prev.is_null() | ((pl.col("v") - prev).abs() > 1e-6 * pl.max_horizontal(prev.abs(), pl.lit(1.0))))
                res.append(d.with_columns(pl.lit(basis).alias("basis"), pl.lit(C["derive"][item]).alias("concept")))
            _ = orig
    if not res:
        return pl.DataFrame()
    allv = pl.concat(res, how="vertical_relaxed")
    loc = allv.filter(pl.col("basis") == "LOCAL").drop("basis").rename({"v": "value"})
    usd = allv.filter(pl.col("basis") == "USD").drop(["basis", "concept"]).rename({"v": "value_usd"})
    out = loc.join(usd, on=KEYW + ["kd", "item_id"], how="full", coalesce=True)
    return out.with_columns(pl.lit("derived_formula").alias("derivation"))


@lru_cache(maxsize=1)
def _derive_order() -> list[str]:
    from app.normalize.formula import dependencies

    C = load_canonical()
    deps = {i: dependencies(e) & set(C["derive"]) for i, e in C["derive"].items()}
    order: list[str] = []
    seen: set[str] = set()

    def visit(i: str, stack: tuple[str, ...] = ()) -> None:
        if i in seen or i in stack:
            return
        for d in sorted(deps.get(i, ())):
            visit(d, stack + (i,))
        seen.add(i)
        order.append(i)

    for i in C["derive"]:
        visit(i)
    return order


# ------------------------------------------------------------------------------------------------ step 9: dq


def _identity_flags(long: pl.DataFrame) -> pl.DataFrame:
    """(issuer_id, period_end, item_id) failing accounting identities (latest known values)."""
    out = []
    bs = long.filter((pl.col("period_kind") == "INSTANT") & pl.col("item_id").is_in(
        ["total_assets", "total_liabilities", "total_equity", "cash_and_equivalents"]))
    if bs.height:
        lat = bs.sort("knowledge_date").group_by(["issuer_id", "period_end", "item_id"]).agg(pl.col("value").last())
        w = lat.pivot(on="item_id", index=["issuer_id", "period_end"], values="value")
        for c in ("total_assets", "total_liabilities", "total_equity"):
            if c not in w.columns:
                w = w.with_columns(pl.lit(None, pl.Float64).alias(c))
        bad = w.filter((pl.col("total_assets") - pl.col("total_liabilities") - pl.col("total_equity")).abs()
                       > 0.02 * pl.col("total_assets").abs())
        for c in ("total_assets", "total_liabilities", "total_equity"):
            out.append(bad.select("issuer_id", "period_end", pl.lit(c).alias("item_id")))
        # cash-flow identity on FY rows: cfo + cfi + cff ~ d(cash), tolerance 5% of total assets
        cf = long.filter((pl.col("period_kind") == "FY") & pl.col("item_id").is_in(["cfo", "cfi", "cff"]))
        if cf.height and "cash_and_equivalents" in w.columns:
            cfl = cf.sort("knowledge_date").group_by(["issuer_id", "period_end", "item_id", "period_start"]).agg(pl.col("value").last())
            cw = cfl.pivot(on="item_id", index=["issuer_id", "period_end", "period_start"], values="value")
            if all(c in cw.columns for c in ("cfo", "cfi", "cff")):
                cash = w.select("issuer_id", "period_end", "cash_and_equivalents", "total_assets").sort("period_end")
                cw = cw.join(cash, on=["issuer_id", "period_end"], how="left")
                prevc = cash.rename({"period_end": "__pe0", "cash_and_equivalents": "__c0"}).drop("total_assets")
                cw = cw.with_columns((pl.col("period_start") - pl.duration(days=1)).alias("__pe0")).sort("__pe0")
                cw = cw.join_asof(prevc.sort("__pe0"), on="__pe0", by="issuer_id", strategy="nearest", tolerance="10d",
                                  check_sortedness=False)
                badcf = cw.filter(((pl.col("cfo") + pl.col("cfi") + pl.col("cff")) - (pl.col("cash_and_equivalents") - pl.col("__c0"))).abs()
                                  > 0.05 * pl.col("total_assets").abs())
                for c in ("cfo", "cfi", "cff"):
                    out.append(badcf.select("issuer_id", "period_end", pl.lit(c).alias("item_id")))
    if not out:
        return pl.DataFrame(schema={"issuer_id": pl.Utf8, "period_end": pl.Date, "item_id": pl.Utf8})
    return pl.concat(out).unique()


def _dq(long: pl.DataFrame) -> pl.DataFrame:
    flags = _identity_flags(long).with_columns(pl.lit(True).alias("__idfail"))
    long = long.join(flags, on=["issuer_id", "period_end", "item_id"], how="left").with_columns(pl.col("__idfail").fill_null(False))
    sign_bad = ((pl.col("item_id").is_in(["total_assets", "revenue", "shares_outstanding", "shares_diluted_weighted"]) & (pl.col("value") < 0))
                | ((pl.col("item_id") == "total_assets") & (pl.col("value") == 0)))
    pen = (pl.when(pl.col("derivation") == "fallback_tag").then(PENALTY["fallback_tag"]).otherwise(0.0)
           + pl.when(pl.col("derivation") == "derived_formula").then(PENALTY["derived_formula"]).otherwise(0.0)
           + pl.when(pl.col("derivation") == "derived_period").then(PENALTY["derived_period"]).otherwise(0.0)
           + pl.when(pl.col("knowledge_date_estimated")).then(PENALTY["estimated"]).otherwise(0.0)
           + pl.when(pl.col("__idfail")).then(PENALTY["identity"]).otherwise(0.0)
           + pl.when(pl.col("disagree").fill_null(False)).then(PENALTY["disagree"]).otherwise(0.0)
           + pl.when(sign_bad).then(PENALTY["sign"]).otherwise(0.0))
    return long.with_columns((1.0 - pen).clip(0.0, 1.0).cast(pl.Float32).alias("dq_score")).drop("__idfail")


# ------------------------------------------------------------------------------------------------ orchestration


def _process_batch(raw: pl.DataFrame, meta: pl.DataFrame, rates: pl.DataFrame | None, run_id: str | None) -> pl.DataFrame:
    from app.normalize import fx as fxm

    C = load_canonical()
    facts = _prepare_facts(raw)
    if facts.is_empty():
        return pl.DataFrame()
    vers = _versions(_candidates(facts, meta))
    if vers.is_empty():
        return pl.DataFrame()
    cal = _fy_calendar(vers, meta)
    spans = _assign_spans(vers, cal)
    disc = _discrete(spans)
    if disc.is_empty():
        return pl.DataFrame()
    # FX on discrete rows (money + per share)
    disc = disc.rename({"kd": "knowledge_date", "kd_est": "knowledge_date_estimated"})
    disc = fxm.convert_frame(disc, rates, value_col="value", currency_col="currency", start_col="period_start",
                             end_col="period_end", kind_col="period_kind")
    disc = disc.rename({"knowledge_date": "kd", "knowledge_date_estimated": "kd_est"})
    ttm = _ttm(disc)
    long = pl.concat([disc, ttm], how="diagonal_relaxed") if ttm.height else disc
    # derived formula items
    der = _derive(long, meta)
    if der.height:
        pmeta = long.select("issuer_id", "period_kind", "period_end", "fiscal_year", "fiscal_period", "period_start").unique(
            subset=["issuer_id", "period_kind", "period_end"])
        imeta = (long.filter(pl.col("currency").is_not_null()).group_by("issuer_id")
                 .agg(pl.col("currency").mode().first().alias("__ccy"), pl.col("gaap").drop_nulls().mode().first().alias("__gaap"),
                      pl.col("source_id").mode().first().alias("__src")))
        kdest = long.group_by(["issuer_id", "period_kind", "period_end"]).agg(pl.col("kd_est").any().alias("kd_est"))
        der = der.join(pmeta, on=["issuer_id", "period_kind", "period_end"], how="left").join(imeta, on="issuer_id", how="left").join(
            kdest, on=["issuer_id", "period_kind", "period_end"], how="left")
        nonmoney = [i for i, k in C["unit_kind"].items() if k in ("shares", "pure", "count")]
        der = der.with_columns(
            pl.when(pl.col("item_id").is_in(nonmoney)).then(None).otherwise(pl.col("__ccy")).alias("currency"),
            pl.col("__gaap").alias("gaap"), pl.col("__src").alias("source_id"), pl.lit(None, pl.Utf8).alias("filing_id"),
            pl.lit(False).alias("disagree"), pl.lit(False).alias("is_restatement"), pl.col("kd_est").fill_null(False),
        ).drop(["__ccy", "__gaap", "__src"])
        der = der.with_columns(
            (pl.col("value_usd") / pl.col("value")).alias("fx_rate"),
            pl.when(pl.col("currency").is_null()).then(pl.lit("none"))
            .when(pl.col("period_kind") == "INSTANT").then(pl.lit("period_end")).otherwise(pl.lit("period_average")).alias("fx_method"),
            pl.lit("derived").alias("fx_source"))
        long = pl.concat([long, der], how="diagonal_relaxed")
    # bank revenue rule: derived revenue replaces the tag value -> drop the tag rows where a derived revenue exists
    long = long.with_columns(pl.col("derivation").fill_null("reported"))
    dr = long.filter((pl.col("item_id") == "revenue") & (pl.col("derivation") == "derived_formula")).select(
        "issuer_id", "period_kind", "period_end").unique().with_columns(pl.lit(True).alias("__drev"))
    if dr.height:
        long = long.join(dr, on=["issuer_id", "period_kind", "period_end"], how="left").filter(
            ~((pl.col("item_id") == "revenue") & pl.col("__drev").fill_null(False) & (pl.col("derivation") != "derived_formula"))
        ).drop("__drev")
    # non-monetary rows: no currency / USD
    nonmoney = [i for i, k in C["unit_kind"].items() if k in ("shares", "pure", "count")]
    long = long.with_columns(
        pl.when(pl.col("item_id").is_in(nonmoney)).then(None).otherwise(pl.col("currency")).alias("currency"))
    long = long.with_columns(
        pl.when(pl.col("currency").is_null()).then(None).otherwise(pl.col("value_usd")).alias("value_usd"),
        pl.when(pl.col("currency").is_null()).then(pl.lit("none")).otherwise(pl.col("fx_method")).alias("fx_method"))
    long = _shares_fallback(long)
    long = long.rename({"kd": "knowledge_date", "kd_est": "knowledge_date_estimated"})
    long = _dq(long)
    # dq: reporting currency differs from the issuer's declared financial currency (issuers.reporting_currency)
    rc = meta.select("issuer_id", pl.col("reporting_currency").alias("__rc"))
    long = long.join(rc, on="issuer_id", how="left").with_columns(
        pl.when(pl.col("currency").is_not_null() & pl.col("__rc").is_not_null() & (pl.col("currency") != pl.col("__rc")))
        .then((pl.col("dq_score") - PENALTY["disagree"]).clip(0.0, 1.0)).otherwise(pl.col("dq_score")).cast(pl.Float32).alias("dq_score")
    ).drop("__rc")
    now = datetime.now(UTC)
    long = long.with_columns(
        pl.col("concept").alias("source_concept"), pl.lit(now).alias("ingested_at"), pl.lit(run_id).cast(pl.Utf8).alias("run_id"),
        pl.col("gaap").fill_null("other"), pl.col("source_id").fill_null("derived"),
        pl.col("knowledge_date_estimated").fill_null(False), pl.col("is_restatement").fill_null(False),
        pl.col("fiscal_year").cast(pl.Int16))
    long = long.filter(pl.col("value").is_not_null() & pl.col("value").is_finite() & pl.col("fiscal_year").is_not_null())
    long = long.select([c for c in CANON_COLS])
    return long.sort(["issuer_id", "item_id", "period_kind", "period_end", "knowledge_date"]).unique(
        subset=["issuer_id", "item_id", "period_end", "period_kind", "knowledge_date"], keep="first", maintain_order=True)


def _shares_fallback(long: pl.DataFrame) -> pl.DataFrame:
    """shares_outstanding INSTANT missing at a period end -> weighted-average diluted (else basic) shares of the
    FY/Q/H period ending there (derivation derived_formula, dq penalty applies)."""
    so = long.filter((pl.col("item_id") == "shares_outstanding") & (pl.col("period_kind") == "INSTANT")).select("issuer_id", "period_end").unique()
    src = (long.filter(pl.col("item_id").is_in(["shares_diluted_weighted", "shares_basic_weighted"]) & pl.col("period_kind").is_in(["Q", "FY", "H"]))
           .with_columns(pl.when(pl.col("item_id") == "shares_diluted_weighted").then(0).otherwise(1).alias("__p"),
                         pl.when(pl.col("period_kind") == "Q").then(0).otherwise(1).alias("__k")))
    # implied diluted shares = net income / diluted EPS (multi-class filers such as Visa report share counts only
    # by class, which companyfacts omits)
    ni = long.filter(pl.col('item_id').is_in(['net_income_common', 'net_income_parent']) & pl.col('period_kind').is_in(['Q', 'FY', 'H']))
    eps = long.filter((pl.col('item_id') == 'eps_diluted') & pl.col('period_kind').is_in(['Q', 'FY', 'H']))
    if ni.height and eps.height:
        k = ['issuer_id', 'period_kind', 'period_end']
        nil = ni.sort('kd').group_by(k).agg(pl.col('value').last().alias('__ni'), pl.col('kd').max().alias('__kd1'))
        epl = eps.sort('kd').group_by(k).agg(pl.col('value').last().alias('__eps'), pl.col('kd').max().alias('__kd2'))
        imp = nil.join(epl, on=k).filter(pl.col('__eps').abs() > 1e-9).with_columns(
            (pl.col('__ni') / pl.col('__eps')).alias('value'), pl.max_horizontal('__kd1', '__kd2').alias('kd'),
            pl.lit('implied_diluted_shares').alias('item_id'), pl.lit(2).alias('__p'),
            pl.when(pl.col('period_kind') == 'Q').then(0).otherwise(1).alias('__k'), pl.lit(False).alias('kd_est'))
        imp = imp.filter(pl.col('value') > 0).drop(['__ni', '__eps', '__kd1', '__kd2'])
        src = pl.concat([src, imp], how='diagonal_relaxed')
    if src.is_empty():
        return _shares_from_yf(long)
    best = (src.sort(["__p", "__k"]).group_by(["issuer_id", "period_end"], maintain_order=True)
            .agg(pl.col("item_id").first().alias("__it"), pl.col("period_kind").first().alias("__pk")))
    pick = src.join(best, on=["issuer_id", "period_end"]).filter((pl.col("item_id") == pl.col("__it")) & (pl.col("period_kind") == pl.col("__pk")))
    add = pick.join(so, on=["issuer_id", "period_end"], how="anti").with_columns(
        pl.lit("shares_outstanding").alias("item_id"), pl.lit("INSTANT").alias("period_kind"), pl.lit(None, pl.Date).alias("period_start"),
        pl.lit("derived_formula").alias("derivation"), (pl.lit("fallback:") + pl.col("__it")).alias("concept"),
        pl.lit(None, pl.Utf8).alias("currency"), pl.lit(None, pl.Float64).alias("value_usd"), pl.lit("none").alias("fx_method"),
    ).drop(["__p", "__k", "__it", "__pk"])
    long = pl.concat([long, add], how="diagonal_relaxed") if add.height else long
    return _shares_from_yf(long)


def yf_equivalent_shares() -> pl.Expr:
    """Share count for the yfinance fallback. info.sharesOutstanding is the *listed class* only (BRK-B 1.41bn class B
    shares, GOOGL 5.9bn class A) while marketCap covers all classes; marketCap / price (price in pence for GBp / ZAc /
    ILA quotes) gives class-equivalent total shares (BRK 2.14bn B-equivalent). Used when it exceeds the listed count by
    3%..20x; otherwise (single class, or inconsistent quote) sharesOutstanding is kept."""
    so = pl.col("sharesOutstanding").cast(pl.Float64)
    px = pl.col("regularMarketPrice").cast(pl.Float64)
    px = pl.when(pl.col("currency").is_in(["GBp", "ZAc", "ILA"])).then(px / 100.0).otherwise(px)
    implied = pl.col("marketCap").cast(pl.Float64) / px
    ratio = implied / so
    return pl.when(implied.is_not_null() & implied.is_finite() & ratio.is_between(1.03, 20.0)).then(implied).otherwise(so)


def _shares_from_yf(long: pl.DataFrame) -> pl.DataFrame:
    """Last resort for issuers with no share count at their latest balance-sheet date (multi-class filers report
    shares/EPS only by class): yfinance sharesOutstanding, dated at its fetch date (knowledge_date_estimated=True)."""
    try:
        from app.core.storage import read_table
        from app.normalize.identity_universe import _cache_read

        yf = _cache_read("yf_info")
        if yf is None or yf.is_empty():
            return long
        ids = long.get_column("issuer_id").unique().to_list()
        lst = (read_table("listings").filter(pl.col("is_primary") & pl.col("valid_to").is_null())
               .join(read_table("securities_master").filter(pl.col("is_primary_security")).select("security_id", "issuer_id"), on="security_id")
               .filter(pl.col("issuer_id").is_in(ids)).select("issuer_id", "yf_ticker"))
        yfs = lst.join(yf.select("yf_ticker", "sharesOutstanding", "fetched_at",
                                 *[c for c in ("marketCap", "regularMarketPrice", "currency") if c in yf.columns]),
                       on="yf_ticker").filter(pl.col("sharesOutstanding") > 0)
        if {"marketCap", "regularMarketPrice", "currency"} <= set(yfs.columns):
            yfs = yfs.with_columns(yf_equivalent_shares().alias("sharesOutstanding"))
    except Exception:  # noqa: BLE001
        return long
    if yfs.is_empty():
        return long
    inst = long.filter(pl.col("period_kind") == "INSTANT")
    last = (inst.filter(pl.col("fiscal_period").is_not_null() & pl.col("fiscal_year").is_not_null()).sort("period_end")
            .group_by("issuer_id").agg(pl.col("period_end").last(), pl.col("fiscal_year").last(), pl.col("fiscal_period").last()))
    have = inst.filter(pl.col("item_id") == "shares_outstanding").select("issuer_id", "period_end").unique()
    need = last.join(have, on=["issuer_id", "period_end"], how="anti").join(yfs, on="issuer_id")
    if need.is_empty():
        return long
    add = need.select(
        "issuer_id", "period_end", "fiscal_year", "fiscal_period", pl.lit("shares_outstanding").alias("item_id"),
        pl.lit("INSTANT").alias("period_kind"), pl.lit(None, pl.Date).alias("period_start"),
        pl.max_horizontal(pl.col("fetched_at").dt.date(), pl.col("period_end")).alias("kd"), pl.lit(True).alias("kd_est"),
        pl.col("sharesOutstanding").cast(pl.Float64).alias("value"), pl.lit(None, pl.Utf8).alias("currency"),
        pl.lit("derived_formula").alias("derivation"), pl.lit("yfinance:sharesOutstanding").alias("concept"),
        pl.lit("yfinance").alias("source_id"), pl.lit("none").alias("fx_method"), pl.lit(None, pl.Float64).alias("value_usd"))
    return pl.concat([long, add], how="diagonal_relaxed")


def build_wide(canon: pl.DataFrame) -> pl.DataFrame:
    """fundamentals_canonical (long) -> fundamentals_wide (one row per issuer, period, basis, knowledge date)."""
    C = load_canonical()
    if canon.is_empty():
        return pl.DataFrame()
    long = canon.rename({"knowledge_date": "kd"})
    out = []
    core = C["core"]
    for basis, vcol in (("LOCAL", "value"), ("USD", "value_usd")):
        w = _wide_events(long, vcol, ["FY", "Q", "H", "TTM"])
        if w.is_empty():
            continue
        dqw = _wide_events(long.with_columns(pl.col("dq_score").cast(pl.Float64)), "dq_score", ["FY", "Q", "H", "TTM"])
        estw = _wide_events(long.with_columns(pl.col("knowledge_date_estimated").cast(pl.Float64)), "knowledge_date_estimated",
                            ["FY", "Q", "H", "TTM"])
        items = [c for c in w.columns if c in C["items"]]
        core_present = [c for c in core if c in w.columns]
        w = w.with_columns(
            (pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in core_present]) / max(len(core), 1)).cast(pl.Float32).alias("coverage")
            if core_present else pl.lit(0.0, pl.Float32).alias("coverage"))
        dq_items = [c for c in core_present if c in dqw.columns]
        if dq_items:
            dqw = dqw.with_columns(pl.mean_horizontal([pl.col(c) for c in dq_items]).cast(pl.Float32).alias("dq_score")).select(KEYW + ["kd", "dq_score"])
        else:
            dqw = dqw.select(KEYW + ["kd"]).with_columns(pl.lit(None, pl.Float32).alias("dq_score"))
        est_items = [c for c in estw.columns if c in C["items"]]
        estw = estw.with_columns(pl.any_horizontal([pl.col(c).fill_null(0) > 0 for c in est_items]).alias("knowledge_date_estimated")
                                 ).select(KEYW + ["kd", "knowledge_date_estimated"])
        w = w.join(dqw, on=KEYW + ["kd"], how="left").join(estw, on=KEYW + ["kd"], how="left")
        if basis == "USD":
            w = w.filter(pl.any_horizontal([pl.col(c).is_not_null() for c in items])) if items else w
        pm = long.filter(pl.col("period_kind") != "INSTANT").select("issuer_id", "period_kind", "period_end", "fiscal_year",
                                                                    "fiscal_period", "period_start").unique(subset=KEYW)
        gm = long.group_by("issuer_id").agg(pl.col("gaap").mode().first().alias("gaap"))
        rcy = (long.filter(pl.col("currency").is_not_null()).group_by(["issuer_id", "period_end"])
               .agg(pl.col("currency").mode().first().alias("__ccy")))
        w = w.join(pm, on=KEYW, how="inner").join(gm, on="issuer_id", how="left").join(rcy, on=["issuer_id", "period_end"], how="left")
        w = w.with_columns(pl.lit(basis).alias("currency_basis"),
                           (pl.lit("USD") if basis == "USD" else pl.col("__ccy").fill_null("USD")).alias("currency"),
                           pl.col("dq_score").fill_null(0.0), pl.col("knowledge_date_estimated").fill_null(False),
                           pl.col("gaap").fill_null("other"), pl.lit(datetime.now(UTC)).alias("computed_at")).drop("__ccy")
        out.append(w.rename({"kd": "knowledge_date"}))
    if not out:
        return pl.DataFrame()
    wide = pl.concat(out, how="diagonal_relaxed")
    cols = ["issuer_id", "currency_basis", "period_kind", "fiscal_year", "fiscal_period", "period_start", "period_end", "currency",
            "gaap", "knowledge_date", "knowledge_date_estimated", "coverage", "dq_score", "computed_at"]
    item_cols = [c for c in C["item_ids"] if c in wide.columns]
    return wide.select(cols + item_cols)


def _part_pred(pcols: Sequence[str], row: dict[str, Any]) -> pl.Expr:
    e = pl.lit(True)
    for c in pcols:
        v = row[c]
        e = e & (pl.col(c).is_null() if v is None else (pl.col(c) == v))
    return e


def _replace_rows(table: str, new: pl.DataFrame, issuer_ids: Sequence[str], settings=None) -> int:
    """Replace all rows of `issuer_ids` in the touched partitions (and partitions where they had rows) with `new`.

    ram_fix 2026-10-31: one partition at a time. The old version collected every touched partition at once (all
    fiscal years of fundamentals_canonical, all 8 currency x period partitions of fundamentals_wide = the whole
    tables) and rewrote them for every 40-issuer batch: the job peaked at ~15 GB inside the worker. Same rows, same
    partitions written; only the peak is one partition."""
    from app.core.schema import conform, get_contract
    from app.core.storage import scan_table, write_table

    c = get_contract(table)
    pcols = list(c.partition_by)
    ids = list(issuer_ids)
    if not pcols:
        return write_table(conform(new, c), table, settings=settings) if new.height else 0
    existing = scan_table(table, settings).filter(pl.col("issuer_id").is_in(ids)).select(pcols).unique().collect()
    touched = pl.concat([new.select(pcols).unique(), existing], how="vertical_relaxed").unique()
    n = 0
    for row in touched.sort(pcols, nulls_last=True).iter_rows(named=True):
        pred = _part_pred(pcols, row)
        keep = scan_table(table, settings).filter(pred & ~pl.col("issuer_id").is_in(ids)).collect()
        part_new = new.filter(pred)
        merged = pl.concat([keep, part_new], how="diagonal_relaxed") if keep.height else part_new
        if not merged.height:
            # the partition became empty (all rows were for these issuers and nothing new): write empty is
            # skipped; it is cleaned by the next full rebuild (unchanged behaviour)
            continue
        n += write_table(conform(merged, c), table, mode="overwrite_partition", settings=settings)
        del keep, merged
    return n


def normalize(issuer_ids: Sequence[str] | None = None, *, raw: pl.DataFrame | None = None, issuers: pl.DataFrame | None = None,
              rates: pl.DataFrame | None = None, write: bool = True, run_id: str | None = None, batch_size: int = 40,
              settings=None, flush_issuers: int = 400) -> dict[str, Any]:
    """Normalize raw facts for the given issuers (None = every issuer present in fundamentals_raw).

    flush_issuers (ram_fix 2026-10-31): processed batches are written every ~flush_issuers issuers (the partition
    rewrite is the expensive part; one rewrite per 40-issuer batch re-wrote the whole tables 70 times per run)."""
    from app.core.schema import conform
    from app.core.storage import scan_table
    from app.normalize import fx as fxm

    s = settings or get_settings()
    meta_all = _issuer_meta(issuers)
    if raw is None:
        lf = scan_table("fundamentals_raw", s)
        raw_all = None
    else:
        lf = raw.lazy()
        raw_all = raw
    # resolve ids on the entity list first (cheap), then select by issuer
    for attempt in range(6):
        try:
            ents = lf.select("source_id", "source_entity_id", "issuer_id").unique().collect(engine="streaming")  # W2: ~45 M raw rows
            break
        except FileNotFoundError:  # partition swapped by a concurrent writer: re-list files and retry
            if raw is not None or attempt == 5:
                raise
            time.sleep(2 + attempt * 3)
            lf = scan_table("fundamentals_raw", s)
    ents = resolve_issuers(ents)
    if issuer_ids is not None:
        ents = ents.filter(pl.col("issuer_id").is_in(list(issuer_ids)))
    ents = ents.filter(pl.col("issuer_id").is_not_null())
    todo = sorted(ents.get_column("issuer_id").unique().to_list())
    if rates is None:
        try:
            rates = fxm.load_rates(settings=s)
        except Exception:  # noqa: BLE001
            rates = None
    total_c, total_w = 0, 0
    frames_c, frames_w = [], []
    pend_c: list[pl.DataFrame] = []
    pend_w: list[pl.DataFrame] = []
    pend_ids: list[str] = []      # issuers of batches with canonical rows (their canonical rows are replaced)
    pend_ids_w: list[str] = []    # issuers of batches that also produced wide rows (their wide rows are replaced)

    def flush() -> tuple[int, int]:
        """Write the pending batches with exactly the issuer sets the per-batch writes used: a batch without
        canonical rows was never pending, a batch without wide rows leaves fundamentals_wide alone (unchanged)."""
        nc = nw = 0
        if pend_c:
            nc = _replace_rows("fundamentals_canonical", pl.concat(pend_c, how="diagonal_relaxed"), list(pend_ids), s)
        if pend_w:
            nw = _replace_rows("fundamentals_wide", pl.concat(pend_w, how="diagonal_relaxed"), list(pend_ids_w), s)
        pend_c.clear()
        pend_w.clear()
        pend_ids.clear()
        pend_ids_w.clear()
        return nc, nw

    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        e = ents.filter(pl.col("issuer_id").is_in(batch)).select("source_id", "source_entity_id", pl.col("issuer_id").alias("__iid"))
        for attempt in range(6):
            # pushed-down entity filter first (W1 2026-10-31: the bare join scanned every raw row - a 60-issuer
            # targeted run was OOM-killed at its 7.5 GB cap in 10 s)
            base = (raw_all.lazy() if raw_all is not None else lf).filter(
                pl.col("source_entity_id").is_in(e.get_column("source_entity_id").unique().to_list()))
            src = base.join(e.lazy(), on=["source_id", "source_entity_id"], how="inner")
            try:
                r = src.with_columns(pl.coalesce("issuer_id", "__iid").alias("issuer_id")).drop("__iid").collect()
                break
            except FileNotFoundError:
                # a concurrent writer (e.g. a long backfill of another source) swapped a partition after the scan was
                # planned: re-list the files and retry the batch (coverage-B, 2026-10-11)
                if raw_all is not None or attempt == 5:
                    raise
                time.sleep(2 + attempt * 3)
                lf = scan_table("fundamentals_raw", s)
        canon = _process_batch(r, meta_all.filter(pl.col("issuer_id").is_in(batch)), rates, run_id)
        if canon.is_empty():
            continue
        canon = conform(canon, "fundamentals_canonical")
        wide = build_wide(canon)
        wide = conform(wide, "fundamentals_wide") if wide.height else wide
        if write:
            pend_c.append(canon)
            pend_ids.extend(batch)
            if wide.height:
                pend_w.append(wide)
                pend_ids_w.extend(batch)
            if len(pend_ids) >= flush_issuers:
                c_, w_ = flush()
                total_c, total_w = total_c + c_, total_w + w_
        else:
            frames_c.append(canon)
            if wide.height:
                frames_w.append(wide)
            total_c += canon.height
            total_w += wide.height
        log.info("fundamentals: batch %d/%d issuers=%d canonical=%d wide=%d", i // batch_size + 1,
                 (len(todo) + batch_size - 1) // batch_size, len(batch), canon.height, wide.height)
    if write:
        c_, w_ = flush()
        total_c, total_w = total_c + c_, total_w + w_
    out: dict[str, Any] = {"canonical": total_c, "wide": total_w, "issuers": len(todo)}
    if not write:
        out["frames"] = {"canonical": pl.concat(frames_c) if frames_c else pl.DataFrame(),
                         "wide": pl.concat(frames_w, how="diagonal_relaxed") if frames_w else pl.DataFrame()}
    return out


def run(ctx: Any) -> Any:
    """fundamentals_normalize: issuers with raw facts ingested since the watermark (params.full=True -> all)."""
    from app.core.storage import scan_table
    from app.scheduler.base import JobResult

    params = dict(ctx.params or {})
    started = datetime.now(UTC)  # before the raw scan: rows ingested while it runs are picked up by the next run
    ids = params.get("issuer_ids")
    if ids is None and not params.get("full") and ctx.watermark:
        try:
            wm = datetime.fromisoformat(ctx.watermark)
            if wm.tzinfo is None:
                wm = wm.replace(tzinfo=UTC)
            ov = params.get("watermark_overlap_min")
            wm -= WM_OVERLAP if ov is None else timedelta(minutes=float(ov))
            ents = scan_table("fundamentals_raw", ctx.settings).filter(pl.col("ingested_at") > wm).select(
                "source_id", "source_entity_id", "issuer_id").unique().collect()
            ents = resolve_issuers(ents)
            ids = ents.get_column("issuer_id").drop_nulls().unique().to_list()
        except Exception as e:  # noqa: BLE001
            ctx.log.warning("watermark parse failed (%s); full run", e)
            ids = None
    uni = params.get("universe")
    if uni in ("mvp", "full"):
        from app.core.storage import read_table

        wanted = ["mvp_us", "mvp_global"] if uni == "mvp" else ["full", "mvp_us", "mvp_global"]
        um = read_table("universe_membership", settings=ctx.settings).filter(
            pl.col("universe_id").is_in(wanted) & pl.col("valid_to").is_null())
        uids = (read_table("securities_master", settings=ctx.settings).join(um.select("security_id"), on="security_id")
                .get_column("issuer_id").unique().to_list())
        ids = uids if ids is None else [i for i in ids if i in set(uids)]
    CCY_LOG.clear()
    res = normalize(ids, run_id=ctx.run_id, settings=ctx.settings,
                    flush_issuers=int(params.get("flush_issuers", 400)))
    msg = f"currency-mismatch facts dropped: {sum(CCY_LOG.values())} across {len(CCY_LOG)} issuers" if CCY_LOG else None
    return JobResult(status="success", rows_written={"fundamentals_canonical": res["canonical"], "fundamentals_wide": res["wide"]},
                     entities_ok=res["issuers"],
                     # a targeted run (params.issuer_ids) says nothing about the other issuers: keep the watermark
                     watermark=ctx.watermark if params.get("issuer_ids") is not None else started.isoformat(),
                     message=msg)
