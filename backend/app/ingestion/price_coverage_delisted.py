"""Price coverage P3: US delisted names (partial survivorship fix).

1. Polygon/Massive /v3/reference/tickers?active=false (shared machine-wide 4/min bucket; pages cached under
   data/raw/polygon/ref_inactive_tickers/) -> common stocks / ADRs on US primary venues delisted inside the free-tier
   grouped-daily window (~2 y).
2. Renames / class changes are dropped (CIK, composite or share-class FIGI still active in the reference).
3. Kept names need >= `min_bars` Polygon grouped-daily bars in prices_staging on or before the delisting date
   (task `polygon` of app.ingestion.price_coverage loads the window).
4. register_delisted(): additive write into issuers / securities_master / listings: ids minted through the identity
   Registry (keys cik:<cik> or dl:<ticker>@<mic>:<yyyymmdd>, never an active issuer), issuer/security inactive with
   inactive_date / last_trade_date = delisting date, primary listing valid_to = delisting date. ref_universe keeps
   these rows on later rebuilds (_merge_previous carries vanished issuers/securities and closed listing rows).
   prices_finalize prices them inside their window (load_listings(include_delisted=True)).
Seed/audit file: data/reference/seeds/us_delisted.csv.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.ingestion.price_coverage import _pid, _s, load_state, update_state

log = get_logger("app.ingestion.price_coverage")

US_MICS = ("XNAS", "XNYS", "XASE", "BATS", "ARCX", "XTXS")
POLY_TYPES = {"CS": "common", "ADRC": "adr", "ADRP": "adr", "GDR": "gdr", "NYRS": "common"}
_COLS = ["ticker", "name", "primary_exchange", "type", "cik", "composite_figi", "share_class_figi", "delisted_utc",
         "currency_name"]


def fetch_inactive(settings: Settings | None = None, since: date | None = None, max_pages: int = 200) -> pl.DataFrame:
    """Inactive US stock tickers delisted on/after `since` (newest first when the API honours sort=delisted_utc)."""
    from app.normalize.identity_universe import _polygon_client, land_raw  # noqa: PLC0415

    s = _s(settings)
    h = _polygon_client(s)
    if h is None:
        raise RuntimeError("POLYGON_APIKEY missing")
    out: list[dict[str, Any]] = []
    url = "/v3/reference/tickers"
    params: dict[str, Any] | None = {"market": "stocks", "active": "false", "limit": 1000, "sort": "delisted_utc",
                                     "order": "desc"}
    sorted_ok = True
    for page in range(max_pages):
        try:
            r = h.get(url, params=params, cache_ttl=7 * 86400)
        except Exception as e:  # noqa: BLE001 - sort not supported -> plain pagination
            if page == 0 and sorted_ok:
                log.warning("sorted inactive listing failed (%s); falling back to unsorted pagination", e)
                sorted_ok = False
                params = {"market": "stocks", "active": "false", "limit": 1000}
                r = h.get(url, params=params, cache_ttl=7 * 86400)
            else:
                raise
        j = r.json()
        res = j.get("results") or []
        out.extend(res)
        update_state("p3_delisted", s, pages=page + 1, inactive_rows=len(out))
        if sorted_ok and since and res:
            last = (res[-1].get("delisted_utc") or "")[:10]
            if last and last < since.isoformat():
                break
        nxt = j.get("next_url")
        if not nxt:
            break
        url, params = nxt, None
    land_raw("polygon", "ref_inactive_tickers", "tickers.json", json.dumps(out).encode(), s)
    df = pl.DataFrame([{c: (str(x.get(c)) if x.get(c) is not None else None) for c in _COLS} for x in out],
                      schema={c: pl.Utf8 for c in _COLS})
    return df.with_columns(pl.col("delisted_utc").str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False)
                           .alias("delisted_date"))


def select_delisted(inactive: pl.DataFrame, listings: pl.DataFrame, secs: pl.DataFrame, issuers: pl.DataFrame,
                    staged: pl.DataFrame, since: date, min_bars: int = 20) -> pl.DataFrame:
    """Pure selection (testable). staged: polygon prices_staging rows [symbol, date]."""
    act_sec = secs.filter(pl.col("is_active"))
    act_cik = set(issuers.filter(pl.col("is_active")).get_column("cik").drop_nulls().to_list())
    act_figi = set(act_sec.get_column("figi").drop_nulls().to_list())
    known = set(listings.filter(pl.col("valid_to").is_not_null()).select(
        (pl.col("polygon_ticker") + "|" + pl.col("valid_to").cast(pl.Utf8)).alias("k")).get_column("k").drop_nulls()
        .to_list())
    df = inactive.filter(pl.col("type").is_in(list(POLY_TYPES)) & pl.col("primary_exchange").is_in(list(US_MICS))
                         & pl.col("delisted_date").is_not_null() & (pl.col("delisted_date") >= since))
    df = df.with_columns(pl.col("cik").str.zfill(10))
    df = df.filter(~pl.col("cik").fill_null("").is_in(list(act_cik))
                   & ~pl.col("composite_figi").fill_null("").is_in(list(act_figi))
                   & ~pl.col("share_class_figi").fill_null("").is_in(list(act_figi)))
    # one row per (ticker, delisting date); bars on/before the delisting date
    df = df.sort("delisted_date", descending=True).unique(subset=["ticker", "delisted_date"], keep="first")
    st = staged.join(df.select(pl.col("ticker").alias("symbol"), "delisted_date"), on="symbol", how="inner").filter(
        pl.col("date") <= pl.col("delisted_date")).group_by("symbol", "delisted_date").agg(
        pl.len().alias("n_bars"), pl.col("date").min().alias("first_bar"), pl.col("date").max().alias("last_bar"))
    df = df.join(st.rename({"symbol": "ticker"}), on=["ticker", "delisted_date"], how="inner").filter(
        pl.col("n_bars") >= min_bars)
    # several delistings of one ticker inside the window (re-listings): keep each, ids differ by date
    df = df.filter(~(pl.col("ticker") + "|" + pl.col("delisted_date").cast(pl.Utf8)).is_in(list(known)))
    return df.sort("delisted_date", "ticker")


def register_delisted(sel: pl.DataFrame, settings: Settings | None = None) -> dict[str, int]:
    """Additive write of the selected delisted names into issuers / securities_master / listings."""
    from app.core.schema import conform  # noqa: PLC0415
    from app.core.storage import read_table, write_table  # noqa: PLC0415
    from app.normalize.identity import Registry, _short_name, clear_cache, make_listing_id  # noqa: PLC0415
    from app.normalize.identity_figi import mic_info  # noqa: PLC0415

    s = _s(settings)
    if sel.is_empty():
        return {"issuers": 0, "securities": 0, "listings": 0}
    today = datetime.now(UTC).date()
    now = datetime.now(UTC)
    reg = Registry(s)
    iss = read_table("issuers", settings=s)
    secs = read_table("securities_master", settings=s)
    lst = read_table("listings", settings=s)
    have_i = set(iss.get_column("issuer_id").to_list())
    have_s = set(secs.get_column("security_id").to_list())
    have_l = set(lst.get_column("listing_id").to_list())
    ni, ns, nl = [], [], []
    for r in sel.iter_rows(named=True):
        d = r["delisted_date"]
        dl_key = f"dl:{r['ticker']}@{r['primary_exchange']}:{d.strftime('%Y%m%d')}"
        keys = ([f"cik:{r['cik']}"] if r.get("cik") else []) + [dl_key]
        iid = reg.resolve(keys, keys[0], today)
        sid = reg.security([f"tmdl:{r['ticker']}@{r['primary_exchange']}:{d.strftime('%Y%m%d')}"]
                           + ([f"scfigi:{r['share_class_figi']}"] if r.get("share_class_figi") else []),
                           iid, "common" if POLY_TYPES[r["type"]] == "common" else f"adr:{r['ticker']}")
        lid = make_listing_id(sid, r["primary_exchange"])
        name = r.get("name") or r["ticker"]
        if iid not in have_i:
            have_i.add(iid)
            ni.append({"issuer_id": iid, "name": name, "short_name": _short_name(name), "country": "US", "region": "US",
                       "cik": r.get("cik"), "filer_category": None, "is_active": False, "inactive_date": d,
                       "updated_at": now, "run_id": "price_coverage_p3", "classification_source": "unknown"})
        if sid not in have_s:
            have_s.add(sid)
            ns.append({"security_id": sid, "issuer_id": iid, "security_type": POLY_TYPES[r["type"]],
                       "figi": r.get("share_class_figi"), "primary_listing_id": lid, "display_ticker": r["ticker"],
                       "name": name + (" ADR" if POLY_TYPES[r["type"]] in ("adr", "gdr") else ""),
                       "trading_currency": "USD", "is_primary_security": True, "is_active": False,
                       "first_trade_date": None, "last_trade_date": r.get("last_bar") or d, "delisting_reason": None,
                       "updated_at": now, "run_id": "price_coverage_p3"})
        if lid not in have_l:
            have_l.add(lid)
            m = mic_info(r["primary_exchange"])
            nl.append({"listing_id": lid, "security_id": sid, "mic": r["primary_exchange"], "exchange_country": "US",
                       "local_symbol": r["ticker"], "yf_ticker": r["ticker"].replace(".", "-"),
                       "polygon_ticker": r["ticker"], "quote_currency": "USD", "price_divisor": 1.0, "is_primary": True,
                       "lot_size": None, "timezone": m["tz"] or "America/New_York", "valid_from": date(1900, 1, 1),
                       "valid_to": d, "updated_at": now})
    rows = {"issuers": len(ni), "securities": len(ns), "listings": len(nl)}
    if ni:
        write_table(pl.concat([iss, conform(pl.DataFrame(ni, infer_schema_length=None), "issuers", strict=False)],
                              how="diagonal_relaxed"), "issuers", mode="overwrite", settings=s)
    if ns:
        write_table(pl.concat([secs, conform(pl.DataFrame(ns, infer_schema_length=None), "securities_master",
                                             strict=False)], how="diagonal_relaxed"),
                    "securities_master", mode="overwrite", settings=s)
    if nl:
        write_table(pl.concat([lst, conform(pl.DataFrame(nl, infer_schema_length=None), "listings", strict=False)],
                              how="diagonal_relaxed"), "listings", mode="overwrite", settings=s)
    reg.save(today)
    clear_cache()
    return rows


def task_delisted(settings: Settings | None = None, window_days: int = 760, min_bars: int = 20) -> dict[str, Any]:
    from app.core.storage import read_table, scan_table  # noqa: PLC0415
    from app.normalize.identity_universe import seeds_dir  # noqa: PLC0415

    s = _s(settings)
    task = "p3_delisted"
    pg = load_state(s).get("tasks", {}).get("p3_polygon_grouped", {}).get("status")
    if pg not in ("done", "done_with_failures", "stopped_at_plan_limit"):
        update_state(task, s, status="waiting_polygon_grouped", polygon_grouped=pg)
        return {"status": "waiting", "polygon_grouped": pg}
    t0 = time.time()
    since = datetime.now(UTC).date() - timedelta(days=window_days)
    update_state(task, s, status="running", pid=_pid(), since=since.isoformat())
    inactive = fetch_inactive(s, since)
    staged = (scan_table("prices_staging", s).filter(pl.col("source_id") == "polygon").select("symbol", "date")
              .collect())
    sel = select_delisted(inactive, read_table("listings", settings=s), read_table("securities_master", settings=s),
                          read_table("issuers", settings=s), staged, since, min_bars)
    p = seeds_dir(s) / "us_delisted.csv"
    sel.select("ticker", "primary_exchange", "name", "type", "cik", "composite_figi", "share_class_figi",
               "delisted_date", "n_bars", "first_bar", "last_bar").write_csv(p)
    from app.ingestion.eu_expansion import running_jobs  # noqa: PLC0415

    for _ in range(120):  # never write the reference tables under a running ref_universe / identity job
        busy = set(running_jobs()) & {"ref_universe", "identity_resolve", "universe_rebalance"}
        if not busy:
            break
        time.sleep(60)
    rows = register_delisted(sel, s)
    summary = {"inactive_fetched": inactive.height, "selected": sel.height, "registered": rows,
               "by_year": dict(sel.group_by(pl.col("delisted_date").dt.year()).len().sort("delisted_date").iter_rows()),
               "seed_file": str(p), "runtime_s": round(time.time() - t0)}
    update_state(task, s, status="done", summary=summary)
    return summary
