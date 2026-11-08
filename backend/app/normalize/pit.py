"""Point-in-time (PIT) helpers. Owner: ref-identity.

Public, stable API (other agents code against these signatures):

    asof(df_or_table, as_of, key_cols, knowledge_col="knowledge_date")
        -> rows visible at `as_of`: knowledge_col <= as_of, then the latest version per key_cols.
    asof_join(left, right, left_date_col, by, knowledge_col="knowledge_date", ...)
        -> panel as-of join: for each left row (entity, date D) attach the right rows known at D.
    impute_knowledge_date(df, ...)
        -> fill missing knowledge_date from period_end + lag rule (ARCHITECTURE §7), flag knowledge_date_estimated.
    lag_days(region, period_kind) -> int   (the lag rule table)

Rules (ARCHITECTURE §7 / _conventions.pit_rule): a fact with knowledge_date K is usable on date D iff K <= D.
For versioned facts the as-of value is the row with max(knowledge_date) <= D per natural key. Ties on
knowledge_date are broken by `knowledge_ts` (if present) and then by input order (last wins).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import overload

import polars as pl

__all__ = ["asof", "asof_join", "impute_knowledge_date", "lag_days", "LAG_RULES", "to_date"]

#: imputed-knowledge-date lag rules in days after period end (ARCHITECTURE §7)
LAG_RULES: dict[tuple[str, str], int] = {
    ("US", "Q"): 45,
    ("US", "H"): 45,
    ("US", "FY"): 75,
    ("NONUS", "Q"): 60,
    ("NONUS", "H"): 60,
    ("NONUS", "FY"): 120,
}


def lag_days(region: str | None, period_kind: str | None) -> int:
    """Days after period_end at which an undated fact is assumed public.

    region: 'US' (incl. CA) or anything else (non-US). period_kind: FY | Q | H | TTM | INSTANT | YTD | 9M.
    TTM/INSTANT/unknown are treated as quarterly (the conservative choice for INSTANT is the filing it came with;
    callers that know the period should pass FY for annual balance sheets).
    """
    reg = "US" if (region or "").upper() in ("US", "CA") else "NONUS"
    pk = (period_kind or "Q").upper()
    pk = "FY" if pk in ("FY", "A", "ANNUAL") else ("H" if pk in ("H", "H1", "H2") else "Q")
    return LAG_RULES[(reg, pk)]


def to_date(d: date | datetime | str) -> date:
    """Coerce date-ish input (date, datetime, 'YYYY-MM-DD') to a date."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _as_lazy(df_or_table: pl.DataFrame | pl.LazyFrame | str) -> tuple[pl.LazyFrame, bool]:
    if isinstance(df_or_table, str):
        from app.core.storage import scan_table

        return scan_table(df_or_table), True
    if isinstance(df_or_table, pl.LazyFrame):
        return df_or_table, True
    if isinstance(df_or_table, pl.DataFrame):
        return df_or_table.lazy(), False
    raise TypeError(f"asof() expects a table name, DataFrame or LazyFrame, got {type(df_or_table)!r}")


@overload
def asof(df_or_table: pl.DataFrame, as_of: date | datetime | str, key_cols: Sequence[str],
         knowledge_col: str = "knowledge_date") -> pl.DataFrame: ...
@overload
def asof(df_or_table: pl.LazyFrame | str, as_of: date | datetime | str, key_cols: Sequence[str],
         knowledge_col: str = "knowledge_date") -> pl.LazyFrame: ...


def asof(df_or_table, as_of, key_cols, knowledge_col="knowledge_date"):
    """Point-in-time snapshot of a versioned table.

    Parameters
    ----------
    df_or_table : contract table name (scanned lazily via app.core.storage.scan_table), DataFrame or LazyFrame.
    as_of       : date (or datetime / ISO string). Rows with knowledge_col > as_of are invisible.
    key_cols    : natural key (e.g. ["issuer_id", "item_id", "period_end", "period_kind"]). Empty -> just filter.
    knowledge_col : column holding the knowledge date (Date or Datetime).

    Returns the same flavour as the input (DataFrame in -> DataFrame out; table name / LazyFrame -> LazyFrame),
    one row per key (the version with the greatest knowledge date <= as_of). Column order is preserved.
    """
    lf, lazy = _as_lazy(df_or_table)
    d = to_date(as_of)
    schema = lf.collect_schema()
    kdt = schema.get(knowledge_col)
    if kdt is None:
        raise KeyError(f"knowledge column {knowledge_col!r} not in frame")
    kexpr = pl.col(knowledge_col)
    if isinstance(kdt, pl.Datetime):
        kexpr = kexpr.dt.date()
    out = lf.filter(kexpr <= pl.lit(d))
    keys = list(key_cols or [])
    if keys:
        sort_cols = [knowledge_col] + (["knowledge_ts"] if "knowledge_ts" in schema else [])
        out = (
            out.with_row_index("__ord")
            .sort(sort_cols + ["__ord"], nulls_last=False)
            .unique(subset=keys, keep="last", maintain_order=True)
            .sort("__ord")
            .drop("__ord")
        )
    return out if lazy else out.collect()


def asof_join(left: pl.DataFrame | pl.LazyFrame, right: pl.DataFrame | pl.LazyFrame | str, left_date_col: str,
              by: Sequence[str], knowledge_col: str = "knowledge_date", version_keys: Sequence[str] | None = None,
              suffix: str = "_r") -> pl.DataFrame:
    """Panel as-of join: for every left row (by..., D=left_date_col) attach right rows with knowledge_col <= D.

    * version_keys=None: right is a *single-series-per-entity* table (e.g. fundamentals_wide filtered to one
      period_kind/currency_basis): each left row gets the right row with the greatest knowledge_col <= D
      (polars join_asof, backward). Use this for "latest known TTM statement as of D".
    * version_keys given (e.g. ["period_end"]): for each left row and each version key, the latest version
      known at D is kept (can produce several rows per left row). Implemented by a cross-date filter; use on
      moderate sizes.
    """
    lf_left = left.lazy() if isinstance(left, pl.DataFrame) else left
    lf_right, _ = _as_lazy(right)
    by = list(by)
    rs = lf_right.collect_schema()
    rk = pl.col(knowledge_col).dt.date() if isinstance(rs[knowledge_col], pl.Datetime) else pl.col(knowledge_col)
    lf_right = lf_right.with_columns(rk.alias("__kd"))
    ls = lf_left.collect_schema()
    ld = pl.col(left_date_col).dt.date() if isinstance(ls[left_date_col], pl.Datetime) else pl.col(left_date_col)
    lf_left = lf_left.with_columns(ld.alias("__ld"))
    if version_keys is None:
        l_df = lf_left.collect().sort(by + ["__ld"]) if by else lf_left.collect().sort("__ld")
        r_df = lf_right.collect()
        # within identical knowledge dates keep the last row so join_asof is deterministic
        r_df = r_df.with_row_index("__ord").sort(by + ["__kd", "__ord"]).unique(
            subset=by + ["__kd"], keep="last", maintain_order=True).drop("__ord")
        out = l_df.join_asof(r_df, left_on="__ld", right_on="__kd", by=by or None, strategy="backward", suffix=suffix,
                             check_sortedness=False)
        return out.drop([c for c in ("__ld", "__kd") if c in out.columns])
    vk = list(version_keys)
    j = lf_left.join(lf_right, on=by, how="inner", suffix=suffix).filter(pl.col("__kd") <= pl.col("__ld"))
    lcols = [c for c in ls.names()]
    j = j.sort("__kd").unique(subset=lcols + vk, keep="last", maintain_order=True)
    return j.drop(["__ld", "__kd"]).collect()


def impute_knowledge_date(df: pl.DataFrame | pl.LazyFrame, *, period_end_col: str = "period_end",
                          period_kind_col: str | None = "period_kind", region_col: str | None = "region",
                          region: str | None = None, knowledge_col: str = "knowledge_date",
                          estimated_col: str = "knowledge_date_estimated") -> pl.DataFrame | pl.LazyFrame:
    """Fill null knowledge dates with period_end + lag rule and set the estimated flag.

    Rows that already have a knowledge date keep it (estimated flag kept if present, else False).
    `region` (constant) is used when `region_col` is missing from the frame. Period kinds: FY/Q/H/TTM/INSTANT
    (INSTANT and TTM use the quarterly lag unless fiscal_period == 'FY' is present).
    """
    is_lazy = isinstance(df, pl.LazyFrame)
    lf = df if is_lazy else df.lazy()
    names = lf.collect_schema().names()
    if knowledge_col not in names:
        lf = lf.with_columns(pl.lit(None, dtype=pl.Date).alias(knowledge_col))
    if estimated_col not in names:
        lf = lf.with_columns(pl.lit(False).alias(estimated_col))
    reg_expr = pl.col(region_col) if region_col and region_col in names else pl.lit(region or "OTHER")
    us = reg_expr.cast(pl.Utf8).str.to_uppercase().is_in(["US", "CA"])
    if period_kind_col and period_kind_col in names:
        pk = pl.col(period_kind_col).cast(pl.Utf8).str.to_uppercase()
        if "fiscal_period" in names:
            pk = pl.when(pl.col("fiscal_period") == "FY").then(pl.lit("FY")).otherwise(pk)
        annual = pk.is_in(["FY", "A"])
    else:
        annual = pl.lit(False)
    lag = (
        pl.when(us & annual).then(LAG_RULES[("US", "FY")])
        .when(us).then(LAG_RULES[("US", "Q")])
        .when(annual).then(LAG_RULES[("NONUS", "FY")])
        .otherwise(LAG_RULES[("NONUS", "Q")])
    )
    missing = pl.col(knowledge_col).is_null()
    lf = lf.with_columns(
        pl.when(missing).then(pl.col(period_end_col) + pl.duration(days=lag)).otherwise(pl.col(knowledge_col))
        .cast(pl.Date).alias(knowledge_col),
        (pl.col(estimated_col).fill_null(False) | missing).alias(estimated_col),
    )
    return lf if is_lazy else lf.collect()


def shift_years(d: date, n: int) -> date:
    """d moved n years (Feb-29 -> Feb-28)."""
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)
