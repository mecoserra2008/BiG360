"""Full-universe scores (job `scoring_full`, owner analytics-scoring; full-scope extension 2026-10-30).

Factor scores, composites, grades and ranks for every equity security OUTSIDE the MVP model universe (the serving
`u_full` set: universe full / watch members plus securities without any universe membership, minus fx / futures /
indices / ETFs), written to factor_scores_ext / composite_scores_ext / peer_scores_ext. The MVP tables and the MVP
`scoring` job are untouched, so MVP results stay identical.

Anchoring (why the MVP stays identical and the scales agree)
* The scoring pipeline is cross-sectional (peer percentiles, a market-cap regression per peer group, factor
  re-percentiles, quintile grades). Re-scoring everyone together would move every MVP score. Instead, for the as-of
  date D, the MVP pipeline is re-run in memory (`scoring.score_date(..., capture=...)`, identical to the `scoring`
  job) and kept as the REFERENCE cross-section. Every extended security is scored as if it were added alone to the
  MVP universe (leave-one-in): metric percentile = (#ref below + 0.5 #ref equal + 0.5) / (n_ref + 1) within the first
  peer level with >= min_peers (incl. itself); the valuation regression of its peer group is fitted on the reference
  peers and applied to it; factor re-percentiles likewise; grade = the quintile its composite would take in the MVP
  distribution. Self-history scores are per security and unchanged.
* Consequences: an extended score depends only on its own inputs and the reference, so securities can be scored in
  any chunking (by peer region) and incrementally; and an extended "A" means "top quintile by MVP standards".
* Ranks (rank_global, rank_sector, factor rank_in_sector / sector_count) of extended rows are ranks among ALL scored
  securities (MVP + extended) of the date within sector x peer region (region x sector), one row per issuer.
* Securities of MVP issuers that are not MVP members themselves (other listings, ADRs) get a copy of the issuer's
  MVP rows (scope 'mvp').
* A null composite carries reason `insufficient_data` and `missing_inputs` ('<factor>:<cause>', 'factors:n/5',
  'core:n/2'), as does every profile row; null factors are explained there too.

Incremental mode (sync)
* A fingerprint per extended security (its price / ratios / valuation / insider / sentiment / moat / forecast /
  beta inputs as of D) and a digest of the MVP reference are kept in data/state/scoring_full/fp.parquet. When the
  as_of date and the reference digest are unchanged, only securities whose fingerprint changed are re-scored; ranks
  are then recomputed for the whole date (cheap). A new as_of date or a changed reference -> every security.

Params: as_of (ctx), mode latest|history (month-ends, like scoring), start, end, years, dates, incremental (default
true for latest), regions (peer regions), max_rss_mb (soft guard, default 4000), write_peer (default true for latest).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.analytics import scoring as S
from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger(__name__)

EXCLUDED_TYPES = ("fx", "future", "index", "etf")
FULL_UNIVERSES = ("full", "watch")
EXT_TABLES = ("factor_scores_ext", "composite_scores_ext", "peer_scores_ext")
KEEP_DAILY_DAYS = 10
#: bump when the anchored scoring logic changes (part of the reference digest: forces one full re-score)
CODE_VERSION = 2
FUND_FACTORS = ("quality", "profitability", "growth", "balance_sheet")


# ------------------------------------------------------------------------------------------------ universe


def mvp_securities(settings: Settings) -> pl.DataFrame:
    from app.analytics.valuation import universe_frame  # noqa: PLC0415

    return universe_frame("mvp", None, settings)


def extended_universe(settings: Settings, mvp: pl.DataFrame | None = None) -> pl.DataFrame:
    """security_id, issuer_id, mvp_issuer (bool): equities of the serving `u_full` set outside the MVP universe."""
    mvp = mvp if mvp is not None else mvp_securities(settings)
    sm = (scan_table("securities_master", settings)
          .select("security_id", "issuer_id", "security_type").collect())
    um = scan_table("universe_membership", settings).select("security_id", "universe_id").collect()
    any_member = um["security_id"].unique()
    full_member = um.filter(pl.col("universe_id").is_in(list(FULL_UNIVERSES)))["security_id"].unique()
    out = sm.filter(
        pl.col("issuer_id").is_not_null(),
        ~pl.col("security_type").fill_null("common").is_in(list(EXCLUDED_TYPES)),
        pl.col("security_id").is_in(full_member) | ~pl.col("security_id").is_in(any_member),
        ~pl.col("security_id").is_in(mvp["security_id"]),
    ).select("security_id", "issuer_id").unique()
    return out.with_columns(pl.col("issuer_id").is_in(mvp["issuer_id"]).alias("mvp_issuer"))


def peer_region_of(issuers: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    prm = S.spec(settings)["peer_groups"]["peer_region_map"]
    iss = scan_table("issuers", settings).select("issuer_id", "region").collect()
    return (issuers.select("issuer_id").unique().join(iss, on="issuer_id", how="left")
            .with_columns(pl.col("region").replace_strict(prm, default="GLOBAL", return_dtype=pl.Utf8)
                          .alias("peer_region")).select("issuer_id", "peer_region"))


# ------------------------------------------------------------------------------------------------ reference


class Reference:
    """The MVP cross-section at D (identical to the `scoring` job's computation) used as the anchor."""

    def __init__(self, D: date, settings: Settings, inp: S.Inputs | None = None):
        t0 = time.time()
        self.D = D
        self.inp = inp or S.Inputs("mvp", settings, None, D, D)
        self.cap: dict[str, Any] = {}
        self.res = S.score_date(self.inp, D, capture=self.cap)
        self.digest = self._digest()
        self.seconds = time.time() - t0

    def _digest(self) -> str:
        h = hashlib.sha1(f"v{CODE_VERSION}".encode())  # a code change forces a full re-score
        for name, key, cols in (("fs", ["security_id", "factor_id"], ["score_raw", "score"]),
                                ("comp", ["security_id", "profile_id"], ["score"]),
                                ("ms", ["security_id", "metric_id"], ["v", "metric_score"])):
            df = self.cap[name].select(*key, *[pl.col(c).cast(pl.Float64).round(6) for c in cols]).sort(key)
            h.update(df.write_csv().encode())
        return h.hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ anchored stats


def _ok_keys(keys: list[str]) -> pl.Expr:
    return pl.all_horizontal([pl.col(k).is_not_null() for k in keys]) if keys else pl.lit(True)


def ref_counts(ext: pl.DataFrame, ref: pl.DataFrame, by: list[str], val: str) -> pl.DataFrame:
    """For every ext row (__rid) with a non-null `val` and non-null keys: n_ref, less (#ref < x), le (#ref <= x) of
    the reference values in the same `by` group. Rows without a match get n_ref = 0."""
    r = ref.filter(pl.col(val).is_not_null() & _ok_keys(by)).select(
        *by, pl.col(val).cast(pl.Float64).alias("__x"), pl.lit(1, pl.Int32).alias("__r"),
        pl.lit(None, pl.UInt32).alias("__rid"))
    e = ext.filter(pl.col(val).is_not_null() & _ok_keys(by)).select(
        *by, pl.col(val).cast(pl.Float64).alias("__x"), pl.lit(0, pl.Int32).alias("__r"), "__rid")
    if e.is_empty():
        return pl.DataFrame(schema={"__rid": pl.UInt32, "n_ref": pl.UInt32, "less": pl.Int32, "le": pl.Int32})
    u = pl.concat([r, e.with_columns(pl.col("__rid").cast(pl.UInt32))], how="vertical_relaxed")
    grp = by if by else None
    cs = pl.col("__r").cum_sum().over(grp) if grp else pl.col("__r").cum_sum()
    less = (u.sort([*by, "__x", "__r"]).with_columns(cs.alias("__c"))
            .filter(pl.col("__r") == 0).select("__rid", pl.col("__c").cast(pl.Int32).alias("less")))
    le = (u.sort([*by, "__x", "__r"], descending=[False] * (len(by) + 1) + [True]).with_columns(cs.alias("__c"))
          .filter(pl.col("__r") == 0).select("__rid", pl.col("__c").cast(pl.Int32).alias("le")))
    n = r.group_by(by).agg(pl.len().cast(pl.UInt32).alias("n_ref")) if by else \
        pl.DataFrame({"n_ref": [r.height]}, schema={"n_ref": pl.UInt32})
    out = e.select("__rid", *by)
    out = out.join(n, on=by, how="left") if by else out.join(n, how="cross")
    return out.select("__rid", pl.col("n_ref").fill_null(0)).join(less, on="__rid").join(le, on="__rid")


def insertion_pct(less: pl.Expr, le: pl.Expr, n_ref: pl.Expr) -> pl.Expr:
    """Average-rank percentile the value would get if inserted into the reference set (scoring.pct_rank formula)."""
    return (less + 0.5 * (le - less) + 0.5) / (n_ref + 1) * 100.0


def anchored_peer_percentiles(ext: pl.DataFrame, ref: pl.DataFrame, levels: list[tuple[str, list[str]]],
                              min_peers: int) -> pl.DataFrame:
    """scoring.peer_percentiles for ext rows against the reference rows (same columns: metric_id, v, raw_value and
    the level keys). Level choice: first level with n_ref + (own v present) >= min_peers, else the broadest level
    with >= 3; peer_count = that number; peer_median = median raw value of the reference group."""
    ext = ext.with_row_index("__rid")
    last = len(levels) - 1
    for i, (lid, keys) in enumerate(levels):
        by = ["metric_id", *keys]
        rv = ref.filter(pl.col("v").is_not_null() & _ok_keys(by))
        n = rv.group_by(by).agg(pl.len().cast(pl.Int64).alias(f"__nr{i}"), pl.col("raw_value").median().alias(f"__m{i}"))
        cnt = ref_counts(ext, ref, by, "v").select("__rid", pl.col("less").alias(f"__l{i}"), pl.col("le").alias(f"__e{i}"))
        ext = ext.join(n, on=by, how="left").join(cnt, on="__rid", how="left")
        key = pl.concat_str([pl.lit(f"{lid}:")] + ([pl.concat_str([pl.col(k).cast(pl.Utf8) for k in keys], separator="|")]
                                                   if keys else [pl.lit("all")]))
        ok = _ok_keys(keys)
        ext = ext.with_columns(
            pl.when(ok).then(pl.col(f"__nr{i}").fill_null(0) + pl.col("v").is_not_null().cast(pl.Int64))
            .otherwise(0).alias(f"__n{i}"), key.alias(f"__k{i}"),
            pl.when(ok & pl.col("v").is_not_null())
            .then(insertion_pct(pl.col(f"__l{i}").fill_null(0), pl.col(f"__e{i}").fill_null(0),
                                pl.col(f"__nr{i}").fill_null(0))).otherwise(None).alias(f"__p{i}"))
    choice = pl.lit(None, pl.Int32)
    for i in reversed(range(len(levels))):
        choice = pl.when(pl.col(f"__n{i}") >= min_peers).then(pl.lit(i)).otherwise(choice)
    choice = pl.when(choice.is_null() & (pl.col(f"__n{last}") >= 3)).then(pl.lit(last)).otherwise(choice)
    ext = ext.with_columns(choice.alias("__lv"))

    def pick(prefix: str) -> pl.Expr:
        e = pl.lit(None)
        for i in reversed(range(len(levels))):
            e = pl.when(pl.col("__lv") == i).then(pl.col(f"{prefix}{i}")).otherwise(e)
        return e

    names = [lid for lid, _ in levels]
    ext = ext.with_columns(
        pl.when(pl.col("v").is_not_null()).then(pick("__p")).otherwise(None).cast(pl.Float64).alias("peer_percentile"),
        pick("__n").cast(pl.Int32).alias("peer_count"),
        pick("__m").cast(pl.Float64).alias("peer_median"),
        pl.col("__lv").replace_strict(dict(enumerate(names)), default=None, return_dtype=pl.Utf8).alias("peer_group_level"),
        pick("__k").cast(pl.Utf8).alias("peer_group_key"))
    drop = [c for c in ext.columns if c[:3] in ("__p", "__n", "__m", "__k", "__l", "__e") or c in ("__lv", "__rid")]
    return ext.drop(drop)


def _mcap_y(df: pl.DataFrame) -> pl.DataFrame:
    """__y (log multiple or yield) and __lm (log market cap) as in scoring.mcap_adjusted."""
    log_metrics = set(S.spec()["transforms"].get("log_before_regression") or [])
    df = df.with_columns(
        pl.when(pl.col("metric_id").is_in(list(log_metrics)))
        .then(pl.when(pl.col("raw_value") > 0).then(pl.col("raw_value").log()).otherwise(None))
        .otherwise(pl.col("raw_value")).alias("__y"),
        pl.col("market_cap_usd").log().alias("__lm"))
    valid = pl.col("__y").is_not_null() & pl.col("__lm").is_not_null() & pl.col("__y").is_finite()
    return df.with_columns(pl.when(valid).then(pl.col("__y")).otherwise(None).alias("__y"))


def anchored_mcap(ext: pl.DataFrame, ref: pl.DataFrame, min_obs: int) -> pl.DataFrame:
    """scoring.mcap_adjusted for ext rows: group = first level whose reference rows (+ itself) reach min_obs (global:
    8); the OLS of that reference group (same winsorizing / fills) is applied to the ext row and its direction-adjusted
    residual is placed among the reference residuals (insertion percentile)."""
    if ext.is_empty():
        return ext.with_columns(pl.lit(None, pl.Float64).alias("mcap_adj_score"))
    ext = _mcap_y(ext).with_row_index("__rid")
    ref = _mcap_y(ref)
    levels = S.LEVELS + [("global", [])]
    last = len(levels) - 1
    for i, (_, keys) in enumerate(levels):
        by = ["metric_id", *keys]
        n = ref.filter(pl.col("__y").is_not_null() & _ok_keys(keys)).group_by(by).agg(
            pl.len().cast(pl.Int64).alias(f"__rn{i}"))
        ext = ext.join(n, on=by, how="left").with_columns(
            pl.when(_ok_keys(keys)).then(pl.col(f"__rn{i}").fill_null(0) + pl.col("__y").is_not_null().cast(pl.Int64))
            .otherwise(0).alias(f"__rn{i}"))
    g = pl.lit(None, pl.Int32)
    for i in reversed(range(len(levels))):
        g = pl.when(pl.col(f"__rn{i}") >= (min_obs if i < last else 8)).then(pl.lit(i)).otherwise(g)
    ext = ext.with_columns(g.alias("__lv")).drop([c for c in ext.columns if c.startswith("__rn")])
    res = np.full(ext.height, np.nan)
    pct = np.full(ext.height, np.nan)
    todo = ext.filter(pl.col("__y").is_not_null() & pl.col("__lv").is_not_null())
    for (mid, lv), part in todo.group_by("metric_id", "__lv"):
        keys = levels[int(lv)][1]
        for kv, sub in (part.group_by(keys) if keys else [((), part)]):
            kv = kv if isinstance(kv, tuple) else (kv,)
            rsub = ref.filter(pl.col("metric_id") == mid, pl.col("__y").is_not_null(),
                              *[pl.col(k) == v for k, v in zip(keys, kv, strict=True)])
            if rsub.height < 8:
                continue
            y = rsub["__y"].to_numpy().astype(float)
            lo, hi = np.nanquantile(y, [0.01, 0.99])
            y = np.clip(y, lo, hi)
            q = rsub["quality_score"].fill_null(50.0).to_numpy().astype(float)
            gr = rsub["revenue_cagr_3y"].to_numpy().astype(float)
            med = np.nanmedian(gr) if np.isfinite(gr).any() else 0.0
            gr = np.where(np.isfinite(gr), gr, med)
            glo, ghi = np.quantile(gr, [0.01, 0.99])
            X = np.column_stack([np.ones(rsub.height), rsub["__lm"].to_numpy().astype(float), q, np.clip(gr, glo, ghi)])
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            hib = int(rsub["hib"][0]) if rsub["hib"][0] is not None else 1
            rr = y - X @ beta
            radj = np.sort(-rr if hib == -1 else rr)
            ye = np.clip(sub["__y"].to_numpy().astype(float), lo, hi)
            ge = sub["revenue_cagr_3y"].to_numpy().astype(float)
            ge = np.clip(np.where(np.isfinite(ge), ge, med), glo, ghi)
            Xe = np.column_stack([np.ones(sub.height), sub["__lm"].to_numpy().astype(float),
                                  sub["quality_score"].fill_null(50.0).to_numpy().astype(float), ge])
            re_ = ye - Xe @ beta
            adj = -re_ if hib == -1 else re_
            less = np.searchsorted(radj, adj, side="left")
            le = np.searchsorted(radj, adj, side="right")
            idx = sub["__rid"].to_numpy()
            res[idx] = re_
            pct[idx] = (less + 0.5 * (le - less) + 0.5) / (len(radj) + 1) * 100.0
    ext = ext.with_columns(pl.Series("mcap_adj_score", pct, dtype=pl.Float64).fill_nan(None))
    return ext.drop("__y", "__lm", "__lv", "__rid")


def anchored_factor_scores(fraw: pl.DataFrame, ref_fs: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """scoring.factor_scores_frame re-percentile for ext rows against the reference factor rows (score_raw)."""
    sp = S.spec(settings)
    neutral = [x["id"] for x in sp["factors"] if x.get("missing_policy") == "neutral_50"]
    pt = S.PASSTHRU_FACTORS
    f = fraw.with_columns(pl.when(pl.col("factor_id").is_in(pt)).then(None).otherwise(pl.col("score_raw")).alias("__v"))
    f = f.with_row_index("__rid")
    ref = ref_fs.with_columns(pl.when(pl.col("factor_id").is_in(pt)).then(None).otherwise(pl.col("score_raw"))
                              .alias("__v"))
    for i, by in enumerate((["factor_id", "peer_region", "sector_id"], ["factor_id", "peer_region"], ["factor_id"])):
        c = ref_counts(f, ref, by, "__v").select("__rid", pl.col("n_ref").alias(f"__nr{i}"),
                                                 insertion_pct(pl.col("less"), pl.col("le"), pl.col("n_ref")).alias(f"__p{i}"))
        f = f.join(c, on="__rid", how="left")
    rep = (pl.when(pl.col("__nr0").fill_null(0) + 1 >= 8).then(pl.col("__p0"))
           .when(pl.col("__nr1").fill_null(0) + 1 >= 8).then(pl.col("__p1")).otherwise(pl.col("__p2")))
    f = f.with_columns(pl.when(pl.col("factor_id").is_in(pt)).then(pl.col("score_raw"))
                       .when(pl.col("__v").is_not_null()).then(rep).otherwise(None).alias("score"))
    f = f.with_columns(
        pl.when(pl.col("score").is_null() & pl.col("factor_id").is_in(neutral)).then(50.0)
        .otherwise(pl.col("score")).alias("score"),
        pl.when(pl.col("score").is_null() & pl.col("factor_id").is_in(neutral)).then(0.0)
        .otherwise(pl.col("coverage")).alias("coverage"))
    return f.drop([c for c in f.columns if c.startswith("__")])


def composite_unranked(fs: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """scoring.composite_frame without the cross-sectional ranks: weighted means + the >= 5 factors / 2 core rule."""
    sp = S.spec(settings)
    neutral = [x["id"] for x in sp["factors"] if x.get("missing_policy") == "neutral_50"]
    usable = pl.col("score").is_not_null() & ((pl.col("coverage") >= 0.30) | pl.col("factor_id").is_in(neutral))
    valid = fs.group_by("security_id").agg(
        pl.col("score").is_not_null().sum().alias("__nf"),
        (pl.col("score").is_not_null() & pl.col("factor_id").is_in(S.CORE)).sum().alias("__nc"))
    out = []
    for pid, w in sp["composite"]["profiles"].items():
        if any(k.startswith("_") for k in w):
            continue
        wdf = pl.DataFrame({"factor_id": list(w.keys()), "w": [float(x) for x in w.values()]})
        c = fs.join(wdf, on="factor_id", how="inner").group_by("security_id", "issuer_id", "sector_id", "peer_region").agg(
            ((pl.col("w") * pl.col("score")).filter(usable).sum() / pl.col("w").filter(usable).sum())
            .fill_nan(None).alias("score"),
            ((pl.col("w") * pl.col("coverage")).sum() / pl.col("w").sum()).alias("coverage"))
        c = c.join(valid, on="security_id", how="left").with_columns(
            pl.when((pl.col("__nf") >= 5) & (pl.col("__nc") >= 2)).then(pl.col("score")).otherwise(None).alias("score"))
        out.append(c.with_columns(pl.lit(pid).alias("profile_id")).drop("__nf", "__nc"))
    return pl.concat(out, how="vertical_relaxed")


def anchored_grades(c: pl.DataFrame, ref_comp: pl.DataFrame) -> pl.DataFrame:
    """grade = the quintile the score would take in the MVP distribution of its profile (scoring.add_ranks rule with
    the security inserted: q = floor((#ref above) * 5 / (n_ref + 1)))."""
    c = c.with_row_index("__rid")
    above = ref_counts(c.with_columns((-pl.col("score")).alias("__ns")),
                       ref_comp.with_columns((-pl.col("score")).alias("__ns")), ["profile_id"], "__ns")
    c = c.join(above.select("__rid", "n_ref", "less"), on="__rid", how="left")
    q = (pl.col("less") * 5 / (pl.col("n_ref") + 1)).floor().cast(pl.Int32)
    grade = q.replace_strict({0: "A", 1: "B", 2: "C", 3: "D", 4: "E"}, default="E", return_dtype=pl.Utf8)
    return c.with_columns(pl.when(pl.col("score").is_null()).then(None).otherwise(grade).alias("grade")) \
        .drop("__rid", "n_ref", "less")


# ------------------------------------------------------------------------------------------------ missing inputs


NOSECTOR_SET = "__no_sector__"
#: factors that do not depend on the industry (market / alternative data): scored even without a sector
SECTOR_FREE_FACTORS = ("momentum", "insider", "sentiment", "macro")


def add_nosector_set(inp: S.Inputs, template: str = "industrials") -> None:
    """Issuers without an industry classification (no kpi set: e.g. HK, unmapped SIC codes) get a pseudo kpi set
    carrying only the industry-independent factors (momentum, insider, sentiment, macro) with the template set's
    members, so their market factors are scored; fundamental factors stay null with cause `no_sector`. The MVP
    universe is fully classified, so the reference is unaffected."""
    if inp.meta.filter(pl.col("kpi_set_id").is_null()).is_empty():
        return
    inp.meta = inp.meta.with_columns(pl.col("kpi_set_id").fill_null(NOSECTOR_SET))
    mem = inp.members.filter(pl.col("kpi_set_id") == template, pl.col("factor_id").is_in(list(SECTOR_FREE_FACTORS)))
    inp.members = pl.concat([inp.members, mem.with_columns(pl.lit(NOSECTOR_SET).alias("kpi_set_id"))])
    mids = mem["metric_id"].unique().to_list()
    inp.scored = pl.concat([inp.scored, pl.DataFrame({"kpi_set_id": [NOSECTOR_SET] * len(mids), "metric_id": mids})],
                           how="vertical_relaxed")
    ms = inp.mfac_set.filter(pl.col("kpi_set_id") == template, pl.col("metric_id").is_in(mids))
    inp.mfac_set = pl.concat([inp.mfac_set, ms.with_columns(pl.lit(NOSECTOR_SET).alias("kpi_set_id"))])


def missing_inputs(inp: S.Inputs, fs: pl.DataFrame, comp: pl.DataFrame, D: date) -> pl.DataFrame:
    """security_id -> missing_inputs list for every scoring security (explains null factors and composites)."""
    sec = inp.score_sec
    has_fund = pl.DataFrame({"issuer_id": inp.fund["issuer_id"].unique()}) if not inp.fund.is_empty() else \
        pl.DataFrame(schema={"issuer_id": pl.Utf8})
    stale = int(S.spec(inp.s)["as_of"]["staleness_days"])
    fresh_iss = inp.fund.filter(pl.col("knowledge_date") <= D, pl.col("period_end") >= D - timedelta(days=stale),
                                pl.col("value").is_not_null())["issuer_id"].unique() if not inp.fund.is_empty() else []
    mcap = (inp.val.filter(pl.col("date") <= D, pl.col("date") > D - timedelta(days=10),
                           pl.col("market_cap_usd").is_not_null())["security_id"].unique()
            if not inp.val.is_empty() else [])
    px_last = (scan_table("prices_daily", inp.s).filter(pl.col("security_id").is_in(sec["security_id"]),
                                                        pl.col("date") <= D, pl.col("date") >= D - timedelta(days=800))
               .group_by("security_id").agg(pl.col("date").max().alias("px_last"), pl.len().alias("px_n")).collect())
    st = (sec.join(px_last, on="security_id", how="left")
          .with_columns(pl.col("issuer_id").is_in(has_fund["issuer_id"]).alias("has_stmt"),
                        pl.col("issuer_id").is_in(list(fresh_iss)).alias("fresh_stmt"),
                        pl.col("security_id").is_in(list(mcap)).alias("has_mcap"),
                        pl.col("issuer_id").is_in(inp.meta.filter(pl.col("kpi_set_id") == NOSECTOR_SET)["issuer_id"])
                        .alias("no_sector")))
    nulls = fs.filter(pl.col("score").is_null()).select("security_id", "factor_id")
    j = nulls.join(st, on="security_id", how="left")
    cause = (pl.when(pl.col("factor_id") == "momentum").then(
        pl.when(pl.col("px_last").is_null()).then(pl.lit("no_prices"))
        .when(pl.col("px_last") < D - timedelta(days=7)).then(pl.lit("stale_prices"))
        .otherwise(pl.lit("short_history")))
        .when(pl.col("factor_id") == "value").then(
        pl.when(~pl.col("has_mcap")).then(pl.lit("no_market_cap"))
        .when(~pl.col("has_stmt")).then(pl.lit("no_statements")).otherwise(pl.lit("low_coverage")))
        .when(pl.col("factor_id") == "moat").then(
        pl.when(~pl.col("has_stmt")).then(pl.lit("no_statements")).otherwise(pl.lit("no_moat")))
        .otherwise(pl.when(~pl.col("has_stmt")).then(pl.lit("no_statements"))
                   .when(pl.col("no_sector")).then(pl.lit("no_sector"))
                   .when(~pl.col("fresh_stmt")).then(pl.lit("stale_statements")).otherwise(pl.lit("low_coverage"))))
    items = j.with_columns(pl.concat_str([pl.col("factor_id"), pl.lit(":"), cause]).alias("item")) \
        .group_by("security_id").agg(pl.col("item").sort())
    cnt = fs.group_by("security_id").agg(
        pl.concat_str([pl.lit("factors:"), pl.col("score").is_not_null().sum().cast(pl.Utf8), pl.lit("/5")]).alias("__f"),
        pl.concat_str([pl.lit("core:"), (pl.col("score").is_not_null() & pl.col("factor_id").is_in(S.CORE)).sum()
                       .cast(pl.Utf8), pl.lit("/2")]).alias("__c"))
    out = sec.select("security_id").join(items, on="security_id", how="left").join(cnt, on="security_id", how="left")
    return out.with_columns(
        pl.concat_list([pl.col("item").fill_null(pl.lit([], pl.List(pl.Utf8))), pl.concat_list(["__f", "__c"])])
        .alias("missing_inputs")).select("security_id", "missing_inputs")


# ------------------------------------------------------------------------------------------------ one chunk


def _blended(parts: dict[str, str], w: dict[str, float]) -> pl.Expr:
    num = pl.sum_horizontal([pl.col(c).fill_null(0) * w[k] for k, c in parts.items()])
    den = pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Float64) * w[k] for k, c in parts.items()])
    return pl.when(den > 0).then(num / den).otherwise(None)


def score_chunk(inp: S.Inputs, ref: Reference, D: date) -> dict[str, pl.DataFrame]:
    """Anchored scores of one chunk of extended issuers: {'peer', 'factor', 'composite'} (scoring securities only;
    ranks are added by the caller)."""
    sp = S.spec(inp.s)
    min_peers = int(sp["peer_groups"]["min_peers"])
    sh = sp["self_history"]
    zclip = float(sp["transforms"]["z"]["clip"])
    blend = sp["metric_blend"]
    a = S.assemble_metrics(inp, D)
    df, fcur, fhist, vhist, base, catf, fund = (a["df"], a["fcur"], a["fhist"], a["vhist"], a["base"], a["catf"],
                                                a["fund"])
    fv = df.filter(pl.col("kind").is_in(["fundamental", "valuation"]))
    mk = df.filter(pl.col("kind").is_in(["market", "other"]))
    fv = anchored_peer_percentiles(fv, ref.cap["fv"], S.LEVELS, min_peers)
    mk = anchored_peer_percentiles(mk, ref.cap["mk"], S.MKT_LEVELS, min_peers)
    fh = fhist.join(base.select("issuer_id", "security_id"), on="issuer_id").drop("issuer_id") \
        if not fhist.is_empty() else pl.DataFrame()
    fpart = S.self_scores(fv.filter(pl.col("kind") == "fundamental"), fh, ["security_id"], catf, int(sh["min_obs"]),
                          float(sh["min_window_years"]), zclip)
    vpart = S.self_scores(fv.filter(pl.col("kind") == "valuation"), vhist, ["security_id"], catf, int(sh["min_obs"]),
                          float(sh["min_window_years"]), zclip)
    mk = mk.with_columns(pl.lit(None, pl.Float64).alias("self_z"), pl.lit(None, pl.Int32).alias("self_hist_n"),
                         pl.lit(None, pl.Float64).alias("self_score"))
    fpart = fpart.with_columns(pl.lit(None, pl.Float64).alias("mcap_adj_score"),
                               _blended({"peer": "peer_percentile", "self": "self_score"}, blend["fundamental"])
                               .alias("metric_score"))
    mk = mk.with_columns(pl.lit(None, pl.Float64).alias("mcap_adj_score"),
                         pl.when(pl.col("scaled")).then(pl.col("raw_value").clip(0, 100))
                         .otherwise(pl.col("peer_percentile")).alias("metric_score"))
    pre = pl.concat([fpart, mk], how="diagonal_relaxed").with_columns(
        pl.when(pl.col("imputed")).then(S.WORST).otherwise(pl.col("metric_score")).alias("metric_score"))
    fs0 = anchored_factor_scores(S.factor_raw_frame(pre, inp), ref.cap["fs0"], inp.s)
    qual = fs0.filter(pl.col("factor_id") == "quality").select("security_id", pl.col("score").alias("quality_score"))
    rc = fcur.filter(pl.col("metric_id") == "revenue_cagr_3y").select("issuer_id",
                                                                      pl.col("raw_value").alias("revenue_cagr_3y"))
    vpart = vpart.join(qual, on="security_id", how="left").join(rc, on="issuer_id", how="left")
    vpart = anchored_mcap(vpart, ref.cap["vpart"], int(sp["market_cap_adjustment"]["min_obs"]))
    vpart = vpart.with_columns(
        _blended({"peer": "peer_percentile", "self": "self_score", "mcap_adj": "mcap_adj_score"}, blend["valuation"])
        .alias("metric_score")).drop("quality_score", "revenue_cagr_3y")
    ms = pl.concat([fpart, vpart, mk], how="diagonal_relaxed").with_columns(
        pl.when(pl.col("imputed")).then(S.WORST).otherwise(pl.col("metric_score")).alias("metric_score"),
        pl.when(pl.col("imputed")).then(S.WORST).otherwise(pl.col("peer_percentile")).alias("peer_percentile"))
    fs = anchored_factor_scores(S.factor_raw_frame(ms, inp), ref.cap["fs"], inp.s).with_columns(pl.lit(D).alias("as_of_date"))
    dq = fund.filter(pl.col("raw_value").is_not_null()).group_by("issuer_id").agg(pl.col("dq_score").mean().alias("dq_score"))
    comp = composite_unranked(fs, inp.s)
    comp = anchored_grades(comp, ref.cap["comp"])
    comp = comp.join(dq, on="issuer_id", how="left").with_columns(pl.col("dq_score").fill_null(0.0),
                                                                  pl.lit(D).alias("as_of_date"))
    miss = missing_inputs(inp, fs, comp, D)
    comp = comp.join(miss, on="security_id", how="left").with_columns(
        pl.when(pl.col("score").is_null()).then(pl.lit("insufficient_data")).otherwise(None).alias("reason"),
        pl.when(pl.col("score").is_null()).then(pl.col("missing_inputs"))
        .otherwise(pl.col("missing_inputs").list.eval(pl.element().filter(~pl.element().str.starts_with("factors:")
                                                                          & ~pl.element().str.starts_with("core:"))))
        .alias("missing_inputs"))
    peer = ms.filter(pl.col("raw_value").is_not_null() | pl.col("imputed")).join(
        inp.mfac_set, on=["kpi_set_id", "metric_id"], how="left").with_columns(
        pl.coalesce(pl.col("factor_id"), pl.col("metric_id").replace_strict(inp.mfac, default="quality",
                                                                            return_dtype=pl.Utf8)).alias("factor_id"),
        pl.lit(D).alias("as_of_date"))
    return {"peer": peer, "factor": fs, "composite": comp}


# ------------------------------------------------------------------------------------------------ ranks


def full_ranks(fs: pl.DataFrame, comp: pl.DataFrame, ref: Reference) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Ranks of the extended scoring rows among MVP + extended scoring rows (one per issuer) of the date."""
    rf = ref.res["factor"].select("security_id", "factor_id", "sector_id", "peer_region", "score") \
        .with_columns(pl.lit(True).alias("__ref"))
    allf = pl.concat([rf, fs.select("security_id", "factor_id", "sector_id", "peer_region", "score")
                      .with_columns(pl.lit(False).alias("__ref"))], how="vertical_relaxed")
    g = ["factor_id", "sector_id", "peer_region"]
    allf = allf.with_columns(pl.col("score").rank("ordinal", descending=True).over(g).cast(pl.Int32).alias("rank_in_sector"),
                             pl.col("score").count().over(g).cast(pl.Int32).alias("sector_count")) \
        .filter(~pl.col("__ref")).select("security_id", "factor_id", "rank_in_sector", "sector_count")
    fs = fs.drop([c for c in ("rank_in_sector", "sector_count") if c in fs.columns]).join(
        allf, on=["security_id", "factor_id"], how="left").with_columns(
        pl.when(pl.col("score").is_null()).then(None).otherwise(pl.col("rank_in_sector")).alias("rank_in_sector"))
    rc = ref.res["composite"].select("security_id", "profile_id", "sector_id", "peer_region", "score") \
        .with_columns(pl.lit(True).alias("__ref"))
    allc = pl.concat([rc, comp.select("security_id", "profile_id", "sector_id", "peer_region", "score")
                      .with_columns(pl.lit(False).alias("__ref"))], how="vertical_relaxed")
    gs = ["profile_id", "sector_id", "peer_region"]
    allc = allc.with_columns(
        pl.col("score").rank("ordinal", descending=True).over("profile_id").cast(pl.Int32).alias("rank_global"),
        pl.col("score").rank("ordinal", descending=True).over(gs).cast(pl.Int32).alias("rank_sector"),
        pl.col("score").count().over(gs).cast(pl.Int32).alias("sector_count"),
        pl.col("score").count().over("profile_id").cast(pl.Int32).alias("n_scored")) \
        .filter(~pl.col("__ref")).select("security_id", "profile_id", "rank_global", "rank_sector", "sector_count",
                                         "n_scored")
    comp = comp.drop([c for c in ("rank_global", "rank_sector", "sector_count", "n_scored") if c in comp.columns]).join(
        allc, on=["security_id", "profile_id"], how="left").with_columns(
        *[pl.when(pl.col("score").is_null()).then(None).otherwise(pl.col(c)).alias(c)
          for c in ("rank_global", "rank_sector")])
    return fs, comp


# ------------------------------------------------------------------------------------------------ fingerprints


def fingerprints(ext: pl.DataFrame, D: date, settings: Settings) -> pl.DataFrame:
    """security_id -> fp: digest of the security's own scoring inputs as of D (cheap lazy aggregates)."""
    sids = ext["security_id"].to_list()
    iss = ext["issuer_id"].unique().to_list()
    parts: list[pl.DataFrame] = []

    def agg(table: str, key: str, date_col: str, cols: list[str], lo_days: int, ids: list[str]) -> None:
        try:
            lf = scan_table(table, settings).filter(pl.col(key).is_in(ids), pl.col(date_col) <= D,
                                                    pl.col(date_col) > D - timedelta(days=lo_days))
            df = lf.group_by(key).agg(pl.col(date_col).max().cast(pl.Utf8).alias("d"), pl.len().alias("n"),
                                      *[pl.col(c).cast(pl.Float64).sum().round(8).alias(c) for c in cols]).collect()
        except Exception as e:  # noqa: BLE001 - optional input
            log.info("fingerprint input %s unavailable: %s", table, e)
            return
        v = df.select(key, pl.concat_str([pl.lit(table + ":"), pl.col("d"), pl.lit("|"), pl.col("n").cast(pl.Utf8),
                                          *[pl.concat_str([pl.lit("|"), pl.col(c).cast(pl.Utf8).fill_null("~")]) for c in cols]])
                      .alias("part"))
        if key == "issuer_id":
            v = ext.select("security_id", "issuer_id").join(v, on="issuer_id").select("security_id", "part")
        parts.append(v.rename({key: "security_id"}) if key != "issuer_id" else v)

    agg("prices_daily", "security_id", "date", ["adj_close"], 420, sids)
    agg("valuation_daily", "security_id", "date", ["market_cap_usd", "pe_ttm", "fcf_yield"], 3700, sids)
    agg("ratios", "issuer_id", "knowledge_date", ["value"], 5000, iss)
    agg("sector_kpis", "issuer_id", "knowledge_date", ["value"], 5000, iss)
    agg("insider_signals", "security_id", "as_of_date", ["signal_score"], 10, sids)
    agg("sentiment_daily", "security_id", "date", ["sentiment_ewm_30d", "gdelt_tone_ewm_7d"], 10, sids)
    agg("moat", "issuer_id", "as_of_date", ["moat_score"], 400, iss)
    agg("forecast_valuation", "issuer_id", "as_of_date", ["implied_vs_hist_growth_pct"], 60, iss)
    agg("macro_betas", "security_id", "as_of_date", ["beta"], 400, sids)
    # industry classification (a sector change moves the peer groups and the kpi set)
    try:
        cls = (scan_table("issuers", settings).filter(pl.col("issuer_id").is_in(iss))
               .select("issuer_id", pl.concat_str([pl.lit("cls:"), pl.col("sector_id").fill_null("~"), pl.lit("|"),
                                                   pl.col("subsector_id").fill_null("~"), pl.lit("|"),
                                                   pl.col("region").fill_null("~")]).alias("part")).collect())
        parts.append(ext.select("security_id", "issuer_id").join(cls, on="issuer_id").select("security_id", "part"))
    except Exception as e:  # noqa: BLE001
        log.info("fingerprint classification unavailable: %s", e)
    base = ext.select("security_id")
    if not parts:
        return base.with_columns(pl.lit("").alias("fp"))
    allp = pl.concat(parts, how="vertical_relaxed").sort("security_id", "part").group_by("security_id").agg(
        pl.col("part").str.join(";").alias("p"))
    return base.join(allp, on="security_id", how="left").with_columns(
        pl.col("p").fill_null("").map_elements(lambda x: hashlib.sha1(x.encode()).hexdigest()[:16],
                                               return_dtype=pl.Utf8).alias("fp")).select("security_id", "fp")


def select_changed(fp_new: pl.DataFrame, old: pl.DataFrame, D: date, digest: str, incremental: bool
                   ) -> tuple[str, list[str] | None]:
    """('full', None) unless incremental and the stored fingerprints are of the same as_of date AND the same MVP
    reference digest; then ('incremental', securities whose fingerprint changed or that are new)."""
    if not incremental or old.is_empty():
        return "full", None
    same = old.filter(pl.col("as_of_date") == D, pl.col("ref_digest") == digest)
    if not same.height:
        return "full", None
    changed = (fp_new.join(same.select("security_id", pl.col("fp").alias("__o")), on="security_id", how="left")
               .filter(pl.col("__o").is_null() | (pl.col("__o") != pl.col("fp"))).sort("security_id")["security_id"]
               .to_list())
    return "incremental", changed


def _fp_path(settings: Settings) -> Path:
    d = settings.state_dir / "scoring_full"
    d.mkdir(parents=True, exist_ok=True)
    return d / "fp.parquet"


def _peak_mb() -> float | None:
    try:
        from app.events_v2.parse import peak_rss_mb  # noqa: PLC0415

        return float(peak_rss_mb())
    except Exception:  # noqa: BLE001
        return None


def _rss_mb() -> float | None:
    try:
        from app.ops.winsys import process_memory_detail  # noqa: PLC0415

        m = process_memory_detail(os.getpid())
        return float(m["rss_mb"]) if m else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------------------------------ date run


def _expand(df: pl.DataFrame, extra: pl.DataFrame) -> pl.DataFrame:
    if extra.is_empty() or df.is_empty():
        return df
    x = df.join(extra.rename({"security_id": "__s2"}), on="issuer_id", how="inner") \
        .with_columns(pl.col("__s2").alias("security_id")).drop("__s2")
    return pl.concat([df, x.select(df.columns)], how="vertical_relaxed")


def score_full_date(D: date, settings: Settings, ctx: Any = None, incremental: bool = False,
                    regions: list[str] | None = None, write_peer: bool = True, run_id: str | None = None,
                    ref: Reference | None = None, chunk_issuers: int = 3000) -> dict[str, Any]:
    """chunk_issuers (params.chunk_issuers, ram_fix 2026-10-31): issuers per scoring chunk; the runner's one retry
    after a memory-cap hit uses a smaller value (jobs.yaml oom_params). Results do not depend on it."""
    t0 = time.time()
    chunk_issuers = max(100, int(chunk_issuers or 3000))
    ref = ref or Reference(D, settings)
    mvp = mvp_securities(settings)
    ext = extended_universe(settings, mvp)
    pr = peer_region_of(ext, settings)
    ext = ext.join(pr, on="issuer_id", how="left")
    if regions:
        ext = ext.filter(pl.col("peer_region").is_in(regions))
    copies = ext.filter(pl.col("mvp_issuer"))
    work = ext.filter(~pl.col("mvp_issuer"))
    stats: dict[str, Any] = {"as_of": D.isoformat(), "extended_securities": ext.height, "mvp_issuer_copies": copies.height,
                             "reference_digest": ref.digest, "reference_seconds": round(ref.seconds, 1)}
    # ---- incremental selection
    fp_new = fingerprints(work, D, settings)
    fpp = _fp_path(settings)
    old = pl.read_parquet(fpp) if fpp.exists() else pl.DataFrame(
        schema={"security_id": pl.Utf8, "fp": pl.Utf8, "as_of_date": pl.Date, "ref_digest": pl.Utf8})
    mode, changed = select_changed(fp_new, old, D, ref.digest, incremental)
    todo = work if changed is None else work.filter(pl.col("security_id").is_in(changed))
    stats.update(mode=mode, rescored_securities=todo.height)
    # ---- score chunks (by peer region, large regions split)
    fac, com, peer = [], [], []
    peak = 0.0
    if todo.height:
        iss_region = todo.select("issuer_id", "peer_region").unique()
        for reg in sorted(iss_region["peer_region"].drop_nulls().unique().to_list()) + (
                [None] if iss_region["peer_region"].null_count() else []):
            if ctx is not None:
                ctx.check_cancelled()
            iss = iss_region.filter(pl.col("peer_region") == reg if reg is not None else pl.col("peer_region").is_null())
            ids = sorted(iss["issuer_id"].to_list())
            for k in range(0, len(ids), chunk_issuers):
                sub = todo.filter(pl.col("issuer_id").is_in(ids[k:k + chunk_issuers]))
                tc = time.time()
                inp = S.Inputs("full", settings, None, D, D, uni=sub.select("security_id", "issuer_id"))
                add_nosector_set(inp)
                r = score_chunk(inp, ref, D)
                fac.append(_expand(r["factor"], inp.extra_sec))
                com.append(_expand(r["composite"], inp.extra_sec))
                if write_peer:
                    peer.append(_expand(r["peer"], inp.extra_sec))
                # ranks need the scoring rows only: keep a marker of the scoring securities
                fac[-1] = fac[-1].with_columns(pl.col("security_id").is_in(inp.score_sec["security_id"]).alias("__scoring"))
                com[-1] = com[-1].with_columns(pl.col("security_id").is_in(inp.score_sec["security_id"]).alias("__scoring"))
                rss = _rss_mb() or 0.0
                peak = max(peak, rss)
                log.info("scoring_full %s chunk %s/%d: %d securities, %d composites in %.1fs (rss %.0f MB)", D, reg,
                         k // chunk_issuers, sub.height, r["composite"].filter(pl.col("profile_id") == "balanced",
                                                                      pl.col("score").is_not_null()).height,
                         time.time() - tc, rss)
                del inp, r
    fs_new = pl.concat(fac, how="diagonal_relaxed") if fac else None
    cs_new = pl.concat(com, how="diagonal_relaxed") if com else None
    # ---- unchanged rows of this date (incremental) are re-ranked together with the new ones
    if mode == "incremental":
        keep_ids = work.filter(~pl.col("security_id").is_in(todo["security_id"]))["security_id"]
        fs_old = _read_date("factor_scores_ext", D, settings).filter(pl.col("security_id").is_in(keep_ids),
                                                                     pl.col("scope") == "extended")
        cs_old = _read_date("composite_scores_ext", D, settings).filter(pl.col("security_id").is_in(keep_ids),
                                                                        pl.col("scope") == "extended")
        sec_meta = _sec_meta(work, settings)
        fs_old = fs_old.join(sec_meta, on="security_id", how="left")
        cs_old = cs_old.join(sec_meta, on="security_id", how="left")
        old_scoring = _scoring_secs(work.filter(pl.col("security_id").is_in(keep_ids)))
        fs_old = fs_old.with_columns(pl.col("security_id").is_in(old_scoring).alias("__scoring"))
        cs_old = cs_old.with_columns(pl.col("security_id").is_in(old_scoring).alias("__scoring"))
        fs_new = pl.concat([x for x in (fs_new, fs_old) if x is not None and not x.is_empty()], how="diagonal_relaxed") \
            if (fs_new is not None or not fs_old.is_empty()) else None
        cs_new = pl.concat([x for x in (cs_new, cs_old) if x is not None and not x.is_empty()], how="diagonal_relaxed") \
            if (cs_new is not None or not cs_old.is_empty()) else None
    rows: dict[str, int] = {}
    if fs_new is not None and cs_new is not None and fs_new.height:
        fsr, csr = full_ranks(fs_new.filter(pl.col("__scoring")), cs_new.filter(pl.col("__scoring")), ref)
        # extra listings take their issuer's ranks
        rk_f = fsr.select("issuer_id", "factor_id", "rank_in_sector", "sector_count")
        rk_c = csr.select("issuer_id", "profile_id", "rank_global", "rank_sector", "sector_count", "n_scored")
        fs_all = fs_new.drop([c for c in ("rank_in_sector", "sector_count") if c in fs_new.columns]).join(
            rk_f, on=["issuer_id", "factor_id"], how="left")
        cs_all = cs_new.drop([c for c in ("rank_global", "rank_sector", "sector_count", "n_scored") if c in cs_new.columns]
                             ).join(rk_c, on=["issuer_id", "profile_id"], how="left")
        rows.update(_write(fs_all.with_columns(pl.lit("extended").alias("scope")),
                           cs_all.with_columns(pl.lit("extended").alias("scope")),
                           pl.concat(peer, how="diagonal_relaxed").with_columns(pl.lit("extended").alias("scope"))
                           if peer else None, settings, run_id))
        stats["balanced_scored"] = int(cs_all.filter(pl.col("profile_id") == "balanced", pl.col("score").is_not_null()).height)
        stats["balanced_rows"] = int(cs_all.filter(pl.col("profile_id") == "balanced").height)
    # ---- non-MVP listings of MVP issuers: copies of the issuer's MVP rows
    if copies.height:
        rows_c = _write_copies(copies, ref, D, settings, run_id, write_peer)
        for k, v in rows_c.items():
            rows[k] = rows.get(k, 0) + v
    # ---- fingerprints
    fp_rows = fp_new.with_columns(pl.lit(D).alias("as_of_date"), pl.lit(ref.digest).alias("ref_digest"))
    if mode == "incremental":
        fp_rows = pl.concat([old.filter(pl.col("as_of_date") == D, ~pl.col("security_id").is_in(fp_rows["security_id"])),
                             fp_rows], how="vertical_relaxed")
    tmp = fpp.with_suffix(".tmp")
    fp_rows.write_parquet(tmp)
    os.replace(tmp, fpp)
    stats.update(rows=rows, seconds=round(time.time() - t0, 1), peak_rss_mb=round(max(peak, _rss_mb() or 0.0)),
                 process_peak_ws_mb=_peak_mb())
    return stats


def _sec_meta(work: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    iss = scan_table("issuers", settings).select("issuer_id", "sector_id").collect()
    return work.select("security_id", "issuer_id", "peer_region").join(iss, on="issuer_id", how="left") \
        .select("security_id", "sector_id", "peer_region")


def _scoring_secs(work: pl.DataFrame) -> list[str]:
    """The scoring security per issuer, chosen like scoring.Inputs (primary preferred, deterministic)."""
    from app.core.storage import scan_table as _st  # noqa: PLC0415

    sm = _st("securities_master").select("security_id", "is_primary_security").collect()
    u = work.select("security_id", "issuer_id").join(sm, on="security_id", how="left")
    return (u.sort(["is_primary_security", "security_id"], nulls_last=False)
            .unique(subset=["issuer_id"], keep="last", maintain_order=True)["security_id"].to_list())


def _read_date(table: str, D: date, settings: Settings) -> pl.DataFrame:
    return scan_table(table, settings).filter(pl.col("as_of_month") == D.strftime("%Y-%m"),
                                              pl.col("as_of_date") == D).collect()


def _write(fs: pl.DataFrame, cs: pl.DataFrame, ps: pl.DataFrame | None, settings: Settings,
           run_id: str | None) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    stamp = [pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id"),
             pl.col("as_of_date").dt.strftime("%Y-%m").alias("as_of_month")]
    out: dict[str, int] = {}
    fs = S._delta_1m(fs.drop([c for c in ("delta_1m",) if c in fs.columns]), settings, table="factor_scores_ext")
    out["factor_scores_ext"] = write_table(fs.with_columns(*stamp).select(
        "as_of_date", "as_of_month", "security_id", "issuer_id", "factor_id", "score", "score_raw",
        pl.col("coverage").cast(pl.Float32), "rank_in_sector", "sector_count", "delta_1m", "scope", "computed_at",
        "run_id"), "factor_scores_ext", settings=settings)
    out["composite_scores_ext"] = write_table(cs.with_columns(*stamp).select(
        "as_of_date", "as_of_month", "security_id", "issuer_id", "profile_id", "score", "grade", "rank_global",
        "rank_sector", "sector_count", "n_scored", pl.col("coverage").cast(pl.Float32),
        pl.col("dq_score").cast(pl.Float32), "scope", "reason", "missing_inputs", "computed_at", "run_id"),
        "composite_scores_ext", settings=settings)
    if ps is not None and ps.height:
        out["peer_scores_ext"] = write_table(ps.with_columns(*stamp).select(
            "as_of_date", "as_of_month", "security_id", "issuer_id", "metric_id", "factor_id", "raw_value",
            "peer_group_key", "peer_group_level", "peer_count", "peer_percentile", "peer_median", "self_z",
            "self_hist_n", "self_score", "mcap_adj_score", "metric_score", "scope", pl.col("imputed").fill_null(False),
            "computed_at", "run_id"), "peer_scores_ext", settings=settings)
    return out


def _write_copies(copies: pl.DataFrame, ref: Reference, D: date, settings: Settings, run_id: str | None,
                  write_peer: bool) -> dict[str, int]:
    """Non-MVP listings of MVP issuers: the issuer's MVP rows (expanded like scoring._expand_extra)."""
    pairs = copies.select("security_id", "issuer_id")

    def cp(df: pl.DataFrame) -> pl.DataFrame:
        return df.drop("security_id").join(pairs, on="issuer_id", how="inner")

    fs = cp(ref.res["factor"]).with_columns(pl.lit("mvp").alias("scope"))
    cs = cp(ref.res["composite"]).with_columns(
        pl.lit("mvp").alias("scope"), pl.lit(None, pl.Utf8).alias("reason"),
        pl.lit(None, pl.List(pl.Utf8)).alias("missing_inputs"),
        pl.col("score").count().over("profile_id", "sector_id", "peer_region").cast(pl.Int32).alias("sector_count"),
        pl.lit(None, pl.Int32).alias("n_scored"))
    ps = cp(ref.res["peer"]).with_columns(pl.lit("mvp").alias("scope")) if write_peer else None
    return _write(fs, cs, ps, settings, run_id)


# ------------------------------------------------------------------------------------------------ retention


def prune(D: date, settings: Settings, keep_days: int = KEEP_DAILY_DAYS) -> int:
    """Extended tables: keep the last `keep_days` days daily, older dates month-ends only."""
    removed = 0
    cutoff = D - timedelta(days=keep_days)
    is_me = (pl.col("as_of_date") + timedelta(days=1)).dt.month() != pl.col("as_of_date").dt.month()
    for t in EXT_TABLES:
        try:
            months = (scan_table(t, settings).filter(pl.col("as_of_date") < cutoff, ~is_me)
                      .select("as_of_month").unique().collect()["as_of_month"].to_list())
        except Exception:  # noqa: BLE001
            continue
        for m in months:
            part = scan_table(t, settings).filter(pl.col("as_of_month") == m).collect()
            kept = part.filter((pl.col("as_of_date") >= cutoff) | is_me)
            removed += part.height - kept.height
            write_table(kept, t, mode="overwrite_partition", settings=settings)
    return removed


# ------------------------------------------------------------------------------------------------ job


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    mode = p.get("mode", "latest")
    end = date.fromisoformat(str(p["end"])) if p.get("end") else ctx.as_of
    if p.get("dates"):
        dates = sorted(date.fromisoformat(str(d)) for d in p["dates"])
    elif mode == "history":
        start = date.fromisoformat(str(p["start"])) if p.get("start") else date(end.year - int(p.get("years", 10)),
                                                                                  end.month, 1)
        dates = S.month_ends(start, end)
    else:
        dates = [end]
    incremental = bool(p.get("incremental", mode == "latest"))
    regions = p.get("regions")
    out: list[dict[str, Any]] = []
    for D in dates:
        ctx.check_cancelled()
        st = score_full_date(D, s, ctx, incremental=incremental and len(dates) == 1, regions=regions,
                             write_peer=bool(p.get("write_peer", mode == "latest")), run_id=ctx.run_id,
                             chunk_issuers=int(p.get("chunk_issuers", 3000)))
        log.info("scoring_full %s: %s", D, json.dumps(st, default=str))
        out.append(st)
    pruned = 0
    if mode == "latest" and p.get("prune", True):
        try:
            pruned = prune(dates[-1], s)
        except Exception as e:  # noqa: BLE001 - housekeeping
            log.warning("scoring_full prune failed: %s", e)
    last = out[-1]
    rows: dict[str, int] = {}
    for st in out:
        for k, v in (st.get("rows") or {}).items():
            rows[k] = rows.get(k, 0) + v
    return JobResult(status="success", rows_written=rows, entities_ok=int(last.get("balanced_scored") or 0),
                     entities_failed=0, watermark=dates[-1].isoformat(),
                     message=(f"{len(dates)} date(s) {dates[0]}..{dates[-1]}; {last['mode']}: rescored "
                              f"{last['rescored_securities']} of {last['extended_securities']} extended securities; "
                              f"balanced composite for {last.get('balanced_scored')} of {last.get('balanced_rows')}; "
                              f"{last['seconds']}s, peak rss {last.get('peak_rss_mb')} MB; pruned {pruned}"))

