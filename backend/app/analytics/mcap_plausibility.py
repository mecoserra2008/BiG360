"""Market-cap plausibility checks (coverage v3 A6). Owner: analytics-scoring. DQ hook `mcap_plausibility`.

Two warn-level checks on the latest valuation_daily row of every valued security (last 10 days):

1. `mcap_vs_exchange_total`: one market cap per issuer (its largest valued security; every class carries the issuer's
   total) summed per exchange country of the primary listing, compared with an APPROXIMATE domestic market
   capitalisation of that country's exchanges (EXCHANGE_TOTALS_USD_TN). Flags a country whose valued sum exceeds
   TOTAL_RATIO_MAX x its total (our coverage can never exceed the market), and a single issuer larger than its
   country's MAX_SINGLE_SHARE of the total (8766.T at USD 1.6 tn = 23 % of Japan; Toyota is ~5 %).
   The totals are order-of-magnitude figures (WFE / exchange statistics, around end-2025) used only as bounds - they
   are labelled approximations, never shown as data.
2. `mcap_price_shares_bounds`: market_cap_usd must equal close_usd x shares_out / adr_ratio (2 %), the share count
   must agree with the traded volume (median daily volume of the last 90 days / shares in listing units, implied
   turnover >= MIN_TURNOVER when >= 20 sessions traded: a 1000x XBRL share-scale error or a missing ADR ratio gives
   1e-7 .. 1e-6; a share count cut 1000x too far, or a share-class / ADS unit mix-up, gives a median daily volume
   above MAX_TURNOVER of ALL shares, which no listed share trades: `high_turnover`), and must stay below MAX_SHARES.
   ADRs valued without a known ratio are counted in the details.
"""

from __future__ import annotations

from typing import Any

import polars as pl

#: approximate domestic equity market capitalisation by exchange country, USD trillion (order of magnitude, around
#: end-2025; WFE / exchange statistics). APPROXIMATION: plausibility bound only.
EXCHANGE_TOTALS_USD_TN: dict[str, float] = {
    "US": 62.0, "CN": 12.5, "JP": 7.0, "HK": 5.5, "IN": 5.2, "CA": 3.6, "GB": 3.4, "FR": 3.3, "TW": 3.0, "DE": 2.6,
    "SA": 2.5, "CH": 2.1, "KR": 2.0, "AU": 1.9, "NL": 1.4, "SE": 1.2, "ZA": 1.1, "ES": 0.95, "IT": 0.95, "AE": 0.9,
    "BR": 0.75, "ID": 0.75, "DK": 0.6, "SG": 0.6, "RU": 0.6, "MX": 0.45, "TH": 0.45, "MY": 0.45, "BE": 0.4,
    "NO": 0.35, "TR": 0.35, "IL": 0.35, "FI": 0.3, "PL": 0.3, "PH": 0.25, "VN": 0.25, "CL": 0.2, "QA": 0.17,
    "IE": 0.15, "AT": 0.15, "KW": 0.15, "GR": 0.12, "NZ": 0.12, "PT": 0.1, "PE": 0.08, "AR": 0.06, "CO": 0.06,
    "RO": 0.06, "NG": 0.06, "KZ": 0.06, "HU": 0.05, "CZ": 0.05, "LU": 0.05, "EG": 0.04, "IS": 0.03, "PK": 0.03,
    "HR": 0.03, "KE": 0.02, "SI": 0.015, "LT": 0.01, "BG": 0.01, "EE": 0.005, "SK": 0.005, "CY": 0.005, "MT": 0.005,
    "LV": 0.002,
}
#: largest plausible single issuer as a share of its country's total (default DEFAULT_MAX_SHARE); concentrated
#: markets get room for their real giants (TSMC ~45 % of Taiwan, Aramco ~70 % of Saudi Arabia, Novo, ASML)
MAX_SINGLE_SHARE: dict[str, float] = {
    "US": 0.15, "JP": 0.12, "CN": 0.12, "IN": 0.15, "CA": 0.15, "GB": 0.2, "FR": 0.2, "DE": 0.25, "HK": 0.3,
    "TW": 0.6, "SA": 0.85, "DK": 0.65, "NL": 0.55, "KR": 0.45, "CH": 0.35, "FI": 0.4, "BE": 0.45, "NO": 0.45,
    "IE": 0.6, "PT": 0.45, "AR": 0.5, "IS": 0.5,
}
#: default by market size: one or two giants dominate small exchanges (CEZ ~60 % of Prague, OTP of Budapest)
DEFAULT_MAX_SHARE = 0.35
SMALL_MARKET_TN, SMALL_MAX_SHARE = 0.2, 0.75
MID_MARKET_TN, MID_MAX_SHARE = 1.0, 0.5
TOTAL_RATIO_MAX = 1.5
CONSISTENCY_TOL = 0.02
MIN_TURNOVER = 1e-6
#: a large cap that barely trades: implied turnover below this AND market cap above LARGE_MCAP_USD (RPAY / TBLA
#: XBRL 1000x share scale: ~5e-6 at USD 380-620 bn; TLK ADR without its 100:1 ratio: 8e-6 at USD 1.1 tn)
LARGE_MIN_TURNOVER, LARGE_MCAP_USD = 1e-5, 5e10
MIN_TRADED_SESSIONS = 20
#: median daily volume above this share of all (listing-unit) shares is implausible (A6 review 2026-11-07: the
#: unvalidated unit guard valued BRK-B on 2.14e6 shares and GERN on 6.4e5, i.e. 2x and 8x their shares a day)
MAX_TURNOVER = 0.5
MAX_SHARES = 2e12
WINDOW_DAYS = 10
VOLUME_DAYS = 90


def _adr_div() -> pl.Expr:
    return (pl.when(pl.col("security_type").is_in(["adr", "gdr"])).then(pl.col("adr_ratio").fill_null(1.0))
            .otherwise(1.0))


def security_flags(val: pl.DataFrame, sec: pl.DataFrame, vol: pl.DataFrame) -> pl.DataFrame:
    """Pure. val [security_id, issuer_id, market_cap_usd, close_usd, shares_out]; sec [security_id, security_type,
    adr_ratio, display_ticker]; vol [security_id, med_volume, traded_sessions]. One row per valued security with
    flag columns inconsistent / low_turnover / high_turnover / too_many_shares / adr_ratio_missing /
    implied_turnover."""
    d = (val.join(sec, on="security_id", how="left").join(vol, on="security_id", how="left")
         .with_columns(_adr_div().alias("__div")))
    recomputed = pl.col("close_usd") * pl.col("shares_out") / pl.col("__div")
    listing_shares = pl.col("shares_out") / pl.col("__div")
    d = d.with_columns(
        ((pl.col("market_cap_usd") - recomputed).abs() > CONSISTENCY_TOL * pl.col("market_cap_usd").abs())
        .fill_null(False).alias("inconsistent"),
        (pl.col("med_volume") / listing_shares).alias("implied_turnover"),
        (pl.col("shares_out") > MAX_SHARES).fill_null(False).alias("too_many_shares"),
        (pl.col("security_type").is_in(["adr", "gdr"]) & pl.col("adr_ratio").is_null()).fill_null(False)
        .alias("adr_ratio_missing"))
    traded = pl.col("traded_sessions").fill_null(0) >= MIN_TRADED_SESSIONS
    low = (pl.col("implied_turnover") < MIN_TURNOVER) | (
        (pl.col("implied_turnover") < LARGE_MIN_TURNOVER) & (pl.col("market_cap_usd") >= LARGE_MCAP_USD))
    d = d.with_columns((traded & low).fill_null(False).alias("low_turnover"),
                       (traded & (pl.col("implied_turnover") > MAX_TURNOVER)).fill_null(False).alias("high_turnover"))
    return d.with_columns((pl.col("inconsistent") | pl.col("low_turnover") | pl.col("high_turnover")
                           | pl.col("too_many_shares")).alias("flagged"))


def exchange_flags(val: pl.DataFrame, sec: pl.DataFrame, country: pl.DataFrame,
                   totals: dict[str, float] | None = None, max_share: dict[str, float] | None = None
                   ) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Pure. country [security_id, exchange_country] (primary listing). Returns (per-country frame [exchange_country,
    issuers, valued_usd_tn, total_usd_tn, ratio, flagged], per-issuer frame of names above their max share)."""
    totals = totals or EXCHANGE_TOTALS_USD_TN
    max_share = max_share or MAX_SINGLE_SHARE
    d = val.join(country, on="security_id", how="left").join(sec.select("security_id", "display_ticker"),
                                                             on="security_id", how="left")
    # one value per issuer: its largest valued security, counted in that security's exchange country
    iss = d.sort("market_cap_usd", descending=True).unique(subset=["issuer_id"], keep="first")
    tot = pl.DataFrame({"exchange_country": list(totals), "total_usd_tn": list(totals.values())})
    ms = pl.DataFrame({"exchange_country": list(max_share), "max_share": list(max_share.values())})
    by = (iss.group_by("exchange_country").agg(pl.len().alias("issuers"),
                                               (pl.col("market_cap_usd").sum() / 1e12).alias("valued_usd_tn"))
          .join(tot, on="exchange_country", how="left")
          .with_columns((pl.col("valued_usd_tn") / pl.col("total_usd_tn")).alias("ratio")))
    by = by.with_columns((pl.col("ratio") > TOTAL_RATIO_MAX).fill_null(False).alias("flagged")).sort("exchange_country")
    default = (pl.when(pl.col("total_usd_tn") < SMALL_MARKET_TN).then(SMALL_MAX_SHARE)
               .when(pl.col("total_usd_tn") < MID_MARKET_TN).then(MID_MAX_SHARE).otherwise(DEFAULT_MAX_SHARE))
    big = (iss.join(tot, on="exchange_country", how="inner").join(ms, on="exchange_country", how="left")
           .with_columns(pl.coalesce(pl.col("max_share"), default).alias("max_share"),
                         (pl.col("market_cap_usd") / (pl.col("total_usd_tn") * 1e12)).alias("share"))
           .filter(pl.col("share") > pl.col("max_share"))
           .select("security_id", "issuer_id", "display_ticker", "exchange_country", "market_cap_usd", "share",
                   "max_share").sort("share", descending=True))
    return by, big


def _latest_valuation(ctx: Any) -> pl.DataFrame:
    return ctx.con.execute(
        "SELECT security_id, issuer_id, market_cap_usd, close_usd, shares_out FROM ("
        "SELECT *, row_number() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn FROM valuation_daily "
        f"WHERE date >= current_date - INTERVAL {WINDOW_DAYS} DAY AND market_cap_usd IS NOT NULL) WHERE rn = 1").pl()


def _volume(ctx: Any, ids: list[str]) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415
    from datetime import date, timedelta  # noqa: PLC0415

    lo = date.today() - timedelta(days=VOLUME_DAYS)
    from app.analytics.shares_pit import isin_ids  # noqa: PLC0415

    return (scan_table("prices_daily", ctx.settings).filter(pl.col("date") >= lo, isin_ids("security_id", ids))
            .select("security_id", "volume").filter(pl.col("volume") > 0)
            .group_by("security_id").agg(pl.col("volume").median().alias("med_volume"),
                                         pl.len().alias("traded_sessions")).collect())


def plausibility_outcomes(ctx: Any) -> list[Any]:
    from app.core.dq import DqOutcome  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415

    t = "valuation_daily"
    if not ctx.has_data(t):
        return []
    val = _latest_valuation(ctx)
    if val.is_empty():
        return [DqOutcome(t, "mcap_plausibility", "info", True, 0.0, None, {"reason": "no recent valuation rows"})]
    s = ctx.settings
    sec = scan_table("securities_master", s).select("security_id", "security_type", "adr_ratio", "display_ticker",
                                                    "primary_listing_id").collect()
    lst = scan_table("listings", s).select("listing_id", "exchange_country").unique("listing_id").collect()
    country = sec.join(lst, left_on="primary_listing_id", right_on="listing_id", how="left").select(
        "security_id", "exchange_country")
    ids = val.get_column("security_id").to_list()
    vol = _volume(ctx, ids) if ctx.has_data("prices_daily") else pl.DataFrame(
        schema={"security_id": pl.Utf8, "med_volume": pl.Float64, "traded_sessions": pl.UInt32})
    sf = security_flags(val, sec.drop("primary_listing_id"), vol)
    by, big = exchange_flags(val, sec, country)

    bad_c = by.filter(pl.col("flagged"))
    n_ex = bad_c.height + big.height
    ex_det = {"countries_over_total": [{"country": r["exchange_country"], "valued_usd_tn": round(r["valued_usd_tn"], 2),
                                        "approx_total_usd_tn": r["total_usd_tn"], "ratio": round(r["ratio"], 2)}
                                       for r in bad_c.iter_rows(named=True)],
              "names_over_max_share": [{"ticker": r["display_ticker"], "country": r["exchange_country"],
                                        "mcap_usd_bn": round(r["market_cap_usd"] / 1e9, 1), "share": round(r["share"], 3),
                                        "max_share": r["max_share"]} for r in big.head(20).iter_rows(named=True)],
              "totals_basis": "approximate domestic market capitalisation (WFE / exchange statistics, ~end-2025)",
              "countries_checked": by.filter(pl.col("total_usd_tn").is_not_null()).height}
    fl = sf.filter(pl.col("flagged")).sort("market_cap_usd", descending=True)
    ps_det = {"inconsistent": int(sf.get_column("inconsistent").sum()),
              "low_turnover": int(sf.get_column("low_turnover").sum()),
              "high_turnover": int(sf.get_column("high_turnover").sum()),
              "too_many_shares": int(sf.get_column("too_many_shares").sum()),
              "adr_ratio_missing_valued": int(sf.get_column("adr_ratio_missing").sum()),
              "valued": sf.height,
              "examples": [{"ticker": r["display_ticker"], "mcap_usd_bn": round(r["market_cap_usd"] / 1e9, 1),
                            "shares_out": r["shares_out"],
                            "implied_turnover": None if r["implied_turnover"] is None else float(f"{r['implied_turnover']:.3g}"),
                            "why": [k for k in ("inconsistent", "low_turnover", "high_turnover", "too_many_shares")
                                    if r[k]]}
                           for r in fl.head(20).iter_rows(named=True)],
              "rule": f"|mcap - close_usd x shares / adr_ratio| <= {CONSISTENCY_TOL:.0%}; median volume / shares >= "
                      f"{MIN_TURNOVER:g} (>= {LARGE_MIN_TURNOVER:g} above USD {LARGE_MCAP_USD / 1e9:.0f} bn) and <= "
                      f"{MAX_TURNOVER:g} when >= {MIN_TRADED_SESSIONS} sessions traded; shares <= {MAX_SHARES:g}"}
    return [DqOutcome(t, "mcap_vs_exchange_total", "warn", n_ex == 0, float(n_ex), 0.0, ex_det),
            DqOutcome(t, "mcap_price_shares_bounds", "warn", fl.height == 0, float(fl.height), 0.0, ps_det)]
