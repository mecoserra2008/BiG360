"""US exchange symbol -> security_id resolution for FINRA short interest and SEC fails-to-deliver (owner: ref-identity).

FINRA (`shrt<date>.csv` symbolCode) and the SEC FTD files publish the NASDAQ-style compact symbol: class shares
without a separator (BFB = Brown-Forman B, HEIA = HEICO A, MKCV, LENB, UHALB), preferreds as <root>PR<series>, ETFs by
their plain ticker. The identifier map keys US listings as `BF.B@XNYS` (ticker_mic) / `BF.B` (polygon_ticker) and
ETFs only as yf_ticker (`ETF:SPY`), so the former resolution (polygon_ticker with '-' -> '.') missed every class
share, every ETF and every Nasdaq Capital Market name whose FINRA market class (SC) had no MIC mapping: 17 % of the
FINRA rows and ~1,000 US listings (completeness audit 2026-10-27).

Resolution order (first hit wins; the caller tries polygon_ticker first):
  1. direct key: the listing symbol as stored (dot form) and its '-' / '/' variants;
  2. compact class-share form (separator removed), only when no other listing uses that symbol directly;
  3. ETFs: the yf_ticker of `ETF:*` securities (US ETFs have no suffix);
  4. OpenFIGI cache (data/reference/registry/openfigi_cache.parquet): exchCode US tickers (`BF/B`) of cached ISIN
     lookups, joined to securities_master.isin;
  5. date-aware ticker history (`history_map`): an FTD row with the same symbol whose CUSIP resolved to a security,
     settlement dates within 31 days (handles ticker changes and reuse over time).
OTC market-class rows are never matched to exchange listings (foreign ordinaries / ADRs on OTC use other symbols).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table

log = get_logger(__name__)

US_MICS = frozenset({"XNYS", "XNAS", "XASE", "ARCX", "BATS", "IEXG", "XCHI", "XPHL", "XBOS", "EDGX", "EDGA"})
HISTORY_DAYS = 31
_SEP = re.compile(r"[-./ ]")


def variants(sym: str) -> tuple[list[str], list[str]]:
    """(direct keys, compact keys) of one listing symbol."""
    s = sym.strip().upper()
    if not s:
        return [], []
    direct = list(dict.fromkeys([s, s.replace("-", "."), s.replace(".", "-"), s.replace(".", "/"), s.replace("-", "/")]))
    compact: list[str] = []
    if _SEP.search(s):
        root, cls = _SEP.split(s, maxsplit=1)
        compact.append(root + _SEP.sub("", cls))
        m = re.fullmatch(r"([A-Z]+)[-./]P(?:R)?[-./]?([A-Z])", s)
        if m:  # preferred series: XYZ-PA / XYZ.PR.A -> XYZPRA
            compact.append(f"{m.group(1)}PR{m.group(2)}")
    return direct, compact


def _openfigi_rows(settings: Settings) -> list[tuple[str, str]]:
    """(US ticker, ISIN) pairs from the cached OpenFIGI ISIN lookups."""
    p = settings.data_dir / "reference" / "registry" / "openfigi_cache.parquet"
    if not p.exists():
        return []
    try:
        c = pl.read_parquet(p, columns=["job_key", "ok", "result_json"]).filter(pl.col("ok"))
    except Exception as e:  # noqa: BLE001 - optional cache
        log.info("openfigi cache unreadable: %s", e)
        return []
    out: list[tuple[str, str]] = []
    for jk, rj in zip(c["job_key"], c["result_json"], strict=True):
        try:
            job = json.loads(jk)
            if job.get("idType") != "ID_ISIN":
                continue
            for r in json.loads(rj) or []:
                if r.get("exchCode") == "US" and r.get("ticker"):
                    out.append((str(r["ticker"]).upper(), str(job.get("idValue"))))
        except (TypeError, ValueError):
            continue
    return out


def listed_symbol_map(settings: Settings | None = None) -> dict[str, str]:
    """Compact / dotted US exchange symbol -> security_id for the active US listings (+ ETFs, OpenFIGI cache)."""
    s = settings or get_settings()
    sm = scan_table("securities_master", s).select("security_id", "security_type", "is_active", "isin").collect()
    active = set(sm.filter(pl.col("is_active").fill_null(True))["security_id"].to_list())
    im = (scan_table("identifier_map", s).filter(pl.col("id_type").is_in(["ticker_mic", "yf_ticker"]),
                                                 pl.col("valid_to").is_null(), pl.col("security_id").is_not_null())
          .select("id_type", "id_value", "security_id").collect())
    direct: dict[str, str] = {}
    compact: dict[str, set[str]] = {}

    def add(sym: str, sid: str) -> None:
        d, c = variants(sym)
        for k in d:
            direct.setdefault(k, sid)
        for k in c:
            compact.setdefault(k, set()).add(sid)

    for r in im.filter(pl.col("id_type") == "ticker_mic").iter_rows(named=True):
        sym, _, mic = str(r["id_value"]).rpartition("@")
        if mic in US_MICS and r["security_id"] in active:
            add(sym, r["security_id"])
    for r in im.filter((pl.col("id_type") == "yf_ticker") & pl.col("security_id").str.starts_with("ETF:")).iter_rows(named=True):
        if r["security_id"] in active and "." not in str(r["id_value"]):
            add(str(r["id_value"]), r["security_id"])
    isin_sid = {i: sid for sid, i in zip(sm["security_id"], sm["isin"], strict=True) if i and sid in active}
    for tic, isin in _openfigi_rows(s):
        sid = isin_sid.get(isin)
        if sid:
            add(tic, sid)
    out = dict(direct)
    for k, sids in compact.items():
        if k not in out and len(sids) == 1:
            out[k] = next(iter(sids))
    return out


def history_map(settings: Settings | None = None) -> pl.DataFrame:
    """symbol, settlement_date, security_id from CUSIP-resolved fails-to-deliver rows (ticker history)."""
    s = settings or get_settings()
    try:
        return (scan_table("fails_to_deliver", s).filter(pl.col("security_id").is_not_null(), pl.col("symbol").is_not_null())
                .select(pl.col("symbol").str.to_uppercase(), "settlement_date", "security_id").unique().collect())
    except Exception as e:  # noqa: BLE001 - table optional
        log.info("fails_to_deliver unavailable for symbol history: %s", e)
        return pl.DataFrame(schema={"symbol": pl.Utf8, "settlement_date": pl.Date, "security_id": pl.Utf8})


def resolve_symbols(df: pl.DataFrame, sym_col: str, date_col: str, symbol_map: dict[str, str],
                    hist: pl.DataFrame | None = None, market_col: str | None = None) -> pl.Series:
    """security_id per row (null when unresolved): static symbol map, then the dated FTD history (+-31 days)."""
    sym = df.get_column(sym_col).cast(pl.Utf8).str.strip_chars().str.to_uppercase()
    otc = (df.get_column(market_col).cast(pl.Utf8) == "OTC").fill_null(False) if market_col and market_col in df.columns \
        else pl.Series([False] * df.height)
    static = pl.Series([None if o or s is None else symbol_map.get(s) for s, o in zip(sym, otc, strict=True)],
                       dtype=pl.Utf8)
    if hist is None or hist.is_empty() or date_col not in df.columns:
        return static
    q = pl.DataFrame({"__i": range(df.height), "symbol": sym, "d": df.get_column(date_col), "otc": otc}) \
        .filter(~pl.col("otc") & pl.col("symbol").is_not_null() & pl.col("d").is_not_null()).sort("d")
    h = hist.rename({"settlement_date": "hd"}).sort("hd")
    j = q.join_asof(h, left_on="d", right_on="hd", by="symbol", strategy="nearest", check_sortedness=False)
    j = j.filter((pl.col("d") - pl.col("hd")).abs() <= timedelta(days=HISTORY_DAYS))
    hmap: dict[int, Any] = dict(zip(j["__i"].to_list(), j["security_id"].to_list(), strict=True))
    return pl.Series([s if s is not None else hmap.get(i) for i, s in enumerate(static)], dtype=pl.Utf8)
