"""OpenFIGI mapping + exchange reference tables. Owner: ref-identity. Job: openfigi_refresh (monthly).

* `MICS`: per operating MIC -> country, region, timezone, currency, yfinance suffix, Bloomberg exchange code.
* `map_jobs(jobs)`: POST /v3/mapping in batches (100 jobs/request with OPENFIGI_APIKEY, else 10), shared
  cross-process rate limiter ('openfigi'), results cached in data/reference/registry/openfigi_cache.parquet
  (keyed by the job JSON; refreshed after `max_age_days`).
* `to_bbg_ticker()` / `from_yf_ticker()`: symbol format conversions (Yahoo <-> Bloomberg <-> exchange-local).
* `run(ctx)`: refresh FIGI mappings for every active listing and upsert figi/composite_figi/share_class_figi rows
  into identifier_map (via app.normalize.identity.merge_identifier_rows).

The key is read with Settings.secret('openfigi_apikey') and only placed in the request header; never logged.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.ratelimit import RateLimit

log = get_logger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# operating MIC -> (country, region, timezone, currency, yf_suffix, bbg_exch_code)
# region values follow _conventions.region (EU = continental Europe incl. CH/NO).
MICS: dict[str, dict[str, str]] = {
    "XNAS": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "XNYS": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "XASE": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "ARCX": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "BATS": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "XCBO": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "XTXS": dict(country="US", region="US", tz="America/Chicago", ccy="USD", yf="", bbg="US"),
    "OTCM": dict(country="US", region="US", tz="America/New_York", ccy="USD", yf="", bbg="US"),
    "XTSE": dict(country="CA", region="CA", tz="America/Toronto", ccy="CAD", yf=".TO", bbg="CN"),
    "XTSX": dict(country="CA", region="CA", tz="America/Toronto", ccy="CAD", yf=".V", bbg="CN"),
    "BVMF": dict(country="BR", region="LATAM", tz="America/Sao_Paulo", ccy="BRL", yf=".SA", bbg="BZ"),
    "XMEX": dict(country="MX", region="LATAM", tz="America/Mexico_City", ccy="MXN", yf=".MX", bbg="MM"),
    "XSGO": dict(country="CL", region="LATAM", tz="America/Santiago", ccy="CLP", yf=".SN", bbg="CI"),
    "XBOG": dict(country="CO", region="LATAM", tz="America/Bogota", ccy="COP", yf=".CL", bbg="CB"),
    "XLIM": dict(country="PE", region="LATAM", tz="America/Lima", ccy="PEN", yf=".LM", bbg="PE"),
    "XBUE": dict(country="AR", region="LATAM", tz="America/Argentina/Buenos_Aires", ccy="ARS", yf=".BA", bbg="AR"),
    "XLON": dict(country="GB", region="UK", tz="Europe/London", ccy="GBp", yf=".L", bbg="LN"),
    "XPAR": dict(country="FR", region="EU", tz="Europe/Paris", ccy="EUR", yf=".PA", bbg="FP"),
    "XAMS": dict(country="NL", region="EU", tz="Europe/Amsterdam", ccy="EUR", yf=".AS", bbg="NA"),
    "XBRU": dict(country="BE", region="EU", tz="Europe/Brussels", ccy="EUR", yf=".BR", bbg="BB"),
    "XLIS": dict(country="PT", region="EU", tz="Europe/Lisbon", ccy="EUR", yf=".LS", bbg="PL"),
    "XDUB": dict(country="IE", region="EU", tz="Europe/Dublin", ccy="EUR", yf=".IR", bbg="ID"),
    "XETR": dict(country="DE", region="EU", tz="Europe/Berlin", ccy="EUR", yf=".DE", bbg="GY"),
    "XFRA": dict(country="DE", region="EU", tz="Europe/Berlin", ccy="EUR", yf=".F", bbg="GF"),
    "XMIL": dict(country="IT", region="EU", tz="Europe/Rome", ccy="EUR", yf=".MI", bbg="IM"),
    "XMAD": dict(country="ES", region="EU", tz="Europe/Madrid", ccy="EUR", yf=".MC", bbg="SM"),
    "XSWX": dict(country="CH", region="EU", tz="Europe/Zurich", ccy="CHF", yf=".SW", bbg="SE"),
    "XSTO": dict(country="SE", region="EU", tz="Europe/Stockholm", ccy="SEK", yf=".ST", bbg="SS"),
    "XCSE": dict(country="DK", region="EU", tz="Europe/Copenhagen", ccy="DKK", yf=".CO", bbg="DC"),
    "XHEL": dict(country="FI", region="EU", tz="Europe/Helsinki", ccy="EUR", yf=".HE", bbg="FH"),
    "XOSL": dict(country="NO", region="EU", tz="Europe/Oslo", ccy="NOK", yf=".OL", bbg="NO"),
    "XWBO": dict(country="AT", region="EU", tz="Europe/Vienna", ccy="EUR", yf=".VI", bbg="AV"),
    "XWAR": dict(country="PL", region="EU", tz="Europe/Warsaw", ccy="PLN", yf=".WA", bbg="PW"),
    "XATH": dict(country="GR", region="EU", tz="Europe/Athens", ccy="EUR", yf=".AT", bbg="GA"),
    # eu_broad expansion (2026-10-11): remaining EEA primary venues with Yahoo coverage
    "XPRA": dict(country="CZ", region="EU", tz="Europe/Prague", ccy="CZK", yf=".PR", bbg="CP"),
    "XBUD": dict(country="HU", region="EU", tz="Europe/Budapest", ccy="HUF", yf=".BD", bbg="HB"),
    "XBSE": dict(country="RO", region="EU", tz="Europe/Bucharest", ccy="RON", yf=".RO", bbg="RE"),
    "XICE": dict(country="IS", region="EU", tz="Atlantic/Reykjavik", ccy="ISK", yf=".IC", bbg="IR"),
    "XTAL": dict(country="EE", region="EU", tz="Europe/Tallinn", ccy="EUR", yf=".TL", bbg="ET"),
    "XRIS": dict(country="LV", region="EU", tz="Europe/Riga", ccy="EUR", yf=".RG", bbg="LR"),
    "XLIT": dict(country="LT", region="EU", tz="Europe/Vilnius", ccy="EUR", yf=".VS", bbg="LH"),
    "XTKS": dict(country="JP", region="JP", tz="Asia/Tokyo", ccy="JPY", yf=".T", bbg="JT"),
    "XKRX": dict(country="KR", region="KR", tz="Asia/Seoul", ccy="KRW", yf=".KS", bbg="KS"),
    "XKOS": dict(country="KR", region="KR", tz="Asia/Seoul", ccy="KRW", yf=".KQ", bbg="KQ"),
    "XHKG": dict(country="HK", region="HK", tz="Asia/Hong_Kong", ccy="HKD", yf=".HK", bbg="HK"),
    "XSHG": dict(country="CN", region="CN", tz="Asia/Shanghai", ccy="CNY", yf=".SS", bbg="CG"),
    "XSHE": dict(country="CN", region="CN", tz="Asia/Shanghai", ccy="CNY", yf=".SZ", bbg="CS"),
    "XTAI": dict(country="TW", region="APAC", tz="Asia/Taipei", ccy="TWD", yf=".TW", bbg="TT"),
    "ROCO": dict(country="TW", region="APAC", tz="Asia/Taipei", ccy="TWD", yf=".TWO", bbg="TT"),  # TPEx (price coverage)
    "XASX": dict(country="AU", region="APAC", tz="Australia/Sydney", ccy="AUD", yf=".AX", bbg="AT"),
    "XNSE": dict(country="IN", region="APAC", tz="Asia/Kolkata", ccy="INR", yf=".NS", bbg="IS"),
    "XBOM": dict(country="IN", region="APAC", tz="Asia/Kolkata", ccy="INR", yf=".BO", bbg="IB"),
    "XSES": dict(country="SG", region="APAC", tz="Asia/Singapore", ccy="SGD", yf=".SI", bbg="SP"),
    "XJSE": dict(country="ZA", region="OTHER", tz="Africa/Johannesburg", ccy="ZAc", yf=".JO", bbg="SJ"),
    # coverage v2 W2 (2026-10-31): EEA regulated markets of the ESMA register without a Yahoo market (reference data
    # and statements only; no free daily price source -> prices reason no_source)
    "XZAG": dict(country="HR", region="EU", tz="Europe/Zagreb", ccy="EUR", yf="", bbg="CZ"),
    "XLJU": dict(country="SI", region="EU", tz="Europe/Ljubljana", ccy="EUR", yf="", bbg="SV"),
    "XBUL": dict(country="BG", region="EU", tz="Europe/Sofia", ccy="EUR", yf="", bbg="BU"),
    "XMAL": dict(country="MT", region="EU", tz="Europe/Malta", ccy="EUR", yf="", bbg="MV"),
    "XCYS": dict(country="CY", region="EU", tz="Asia/Nicosia", ccy="EUR", yf="", bbg="CY"),
    "XLUX": dict(country="LU", region="EU", tz="Europe/Luxembourg", ccy="EUR", yf="", bbg="LX"),
    "XBRA": dict(country="SK", region="EU", tz="Europe/Bratislava", ccy="EUR", yf="", bbg="SK"),
}

#: SEC company_tickers_exchange 'exchange' -> MIC
SEC_EXCHANGE_MIC = {"Nasdaq": "XNAS", "NYSE": "XNYS", "CBOE": "BATS", "TXSE": "XTXS", "OTC": "OTCM"}

#: yfinance suffix -> MIC (inverse of MICS, first wins)
YF_SUFFIX_MIC: dict[str, str] = {}
for _mic, _m in MICS.items():
    if _m["yf"] and _m["yf"] not in YF_SUFFIX_MIC:
        YF_SUFFIX_MIC[_m["yf"]] = _mic
YF_SUFFIX_MIC[".IL"] = "XLON"
YF_SUFFIX_MIC[".KQ"] = "XKOS"

#: Bloomberg/OpenFIGI security types -> securities_master.security_type
FIGI_SECTYPE = {
    "Common Stock": "common", "REIT": "common", "Ordinary Shares": "common", "Dutch Cert": "common",
    "Savings Share": "preferred", "Preferred": "preferred", "Pref": "preferred", "Preference": "preferred",
    "ADR": "adr", "GDR": "gdr", "NY Reg Shrs": "common", "Receipt": "adr",
    "ETP": "etf", "ETF": "etf", "Open-End Fund": "fund", "Closed-End Fund": "fund", "Unit": "other",
    "MLP": "other", "Ltd Part": "other", "Right": "other", "Warrant": "other",
}


def mic_info(mic: str | None) -> dict[str, str]:
    return MICS.get((mic or "").upper(), dict(country="", region="OTHER", tz="UTC", ccy="", yf="", bbg=""))


def from_yf_ticker(yf_ticker: str) -> tuple[str, str | None]:
    """'7203.T' -> ('7203', 'XTKS'); 'BRK-B' -> ('BRK.B', None) (US, MIC unknown); 'NOVO-B.CO' -> ('NOVO B', 'XCSE')."""
    t = yf_ticker.strip().upper()
    if "." in t:
        base, suf = t.rsplit(".", 1)
        mic = YF_SUFFIX_MIC.get("." + suf)
        if mic:
            if mic == "XHKG":
                base = base.lstrip("0").zfill(4)
            elif mic in ("XSTO", "XCSE", "XHEL", "XOSL"):
                base = base.replace("-", " ")
            return base, mic
    return t.replace("-", "."), None


def to_bbg_ticker(local_symbol: str, mic: str) -> str:
    """Exchange-local symbol -> Bloomberg-style ticker for OpenFIGI TICKER jobs (best effort)."""
    s = local_symbol.strip().upper()
    if mic == "XHKG":
        return s.lstrip("0") or "0"
    if mic in ("XSTO", "XCSE", "XHEL", "XOSL"):
        return s.replace(" ", "").replace("-", "")       # 'VOLV B' -> 'VOLVB'
    if mic in ("XNAS", "XNYS", "XASE", "ARCX", "BATS", "XTXS", "XLON", "XSGO"):
        return s.replace(".", "/").replace("-", "/")      # 'BRK.B' -> 'BRK/B'
    return s


def yf_to_local(yf_ticker: str) -> str:
    return from_yf_ticker(yf_ticker)[0]


# ------------------------------------------------------------------------------------------------ OpenFIGI client


def _cache_path(settings: Settings) -> Path:
    return settings.data_dir / "reference" / "registry" / "openfigi_cache.parquet"


def _job_key(job: dict[str, Any]) -> str:
    return json.dumps(job, sort_keys=True, separators=(",", ":"))


def load_cache(settings: Settings | None = None) -> pl.DataFrame:
    s = settings or get_settings()
    p = _cache_path(s)
    if p.exists():
        return pl.read_parquet(p)
    return pl.DataFrame(schema={"job_key": pl.Utf8, "fetched_at": pl.Datetime("us", "UTC"), "ok": pl.Boolean,
                                "result_json": pl.Utf8})


def _save_cache(df: pl.DataFrame, settings: Settings) -> None:
    p = _cache_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.replace(p)


def map_jobs(jobs: Sequence[dict[str, Any]], *, settings: Settings | None = None, max_age_days: int = 30,
             http: HttpClient | None = None) -> dict[str, list[dict[str, Any]]]:
    """Run OpenFIGI mapping jobs (each like {"idType":"TICKER","idValue":"AAPL","exchCode":"US"}).

    Returns {job_key: [figi records]} ([] when OpenFIGI had no match). Cached results younger than max_age_days
    are reused. Failures (HTTP errors) are not cached and simply omitted from the result.
    """
    s = settings or get_settings()
    cache = load_cache(s)
    now = datetime.now(UTC)
    fresh = cache.filter(pl.col("fetched_at") >= now - timedelta(days=max_age_days))
    known = {k: json.loads(v) for k, v in zip(fresh.get_column("job_key"), fresh.get_column("result_json"), strict=True)}
    out: dict[str, list[dict[str, Any]]] = {}
    todo: list[dict[str, Any]] = []
    for j in jobs:
        k = _job_key(j)
        if k in known:
            out[k] = known[k]
        elif k not in {_job_key(t) for t in todo[-200:]}:
            todo.append(j)
    if not todo:
        return out
    key = s.secret("openfigi_apikey")
    batch = 100 if key else 10
    limit = RateLimit(25 / 6.5, 3) if key else RateLimit.per_minute(24, 1)
    own_http = http is None
    client = http or HttpClient("openfigi", rate_limit=limit, settings=s, cache=False)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    new_rows = []
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            try:
                r = client.request("POST", OPENFIGI_URL, json_body=chunk, headers=headers, cache_ttl=0)
            except HttpError as e:
                log.warning("openfigi batch %d failed: HTTP %s", i // batch, e.status)
                if e.status == 429:
                    time.sleep(10)
                continue
            res = r.json()
            for j, item in zip(chunk, res, strict=False):
                k = _job_key(j)
                data = item.get("data") or []
                ok = "error" not in item or bool(data)
                out[k] = data
                new_rows.append({"job_key": k, "fetched_at": now, "ok": ok, "result_json": json.dumps(data)})
            if (i // batch) % 20 == 19:
                log.info("openfigi: %d/%d jobs mapped", min(i + batch, len(todo)), len(todo))
    finally:
        if own_http:
            client.close()
    if new_rows:
        add = pl.DataFrame(new_rows, schema=cache.schema)
        merged = pl.concat([cache, add]).unique(subset=["job_key"], keep="last", maintain_order=True)
        _save_cache(merged, s)
    return out


def figi_job_for(local_symbol: str | None, mic: str | None, isin: str | None = None) -> dict[str, Any] | None:
    """Preferred mapping job for a listing: ISIN (+exchCode filter) when known, else TICKER + exchCode."""
    m = mic_info(mic)
    if isin:
        job: dict[str, Any] = {"idType": "ID_ISIN", "idValue": isin}
        if m["bbg"]:
            job["exchCode"] = m["bbg"]
        return job
    if local_symbol and m["bbg"]:
        return {"idType": "TICKER", "idValue": to_bbg_ticker(local_symbol, (mic or "").upper()), "exchCode": m["bbg"],
                "marketSecDes": "Equity"}
    return None


def pick_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the best FIGI record among mapping results (equity, common/ADR first)."""
    if not records:
        return None
    def score(r: dict[str, Any]) -> tuple:
        st = r.get("securityType") or ""
        typ = FIGI_SECTYPE.get(st, "other")
        return (r.get("marketSector") == "Equity", typ in ("common", "adr", "gdr"), typ == "common")
    return sorted(records, key=score, reverse=True)[0]


def map_listings(listings: pl.DataFrame, *, settings: Settings | None = None, max_age_days: int = 30) -> pl.DataFrame:
    """Map a frame with columns (listing_key, local_symbol, mic, isin?) to FIGI columns.

    Returns listing_key, figi, composite_figi, share_class_figi, figi_security_type, figi_name, figi_ticker.
    """
    rows = listings.to_dicts()
    jobs: dict[str, dict[str, Any]] = {}
    for r in rows:
        j = figi_job_for(r.get("local_symbol"), r.get("mic"), r.get("isin"))
        if j:
            jobs[r["listing_key"]] = j
    res = map_jobs(list(jobs.values()), settings=settings, max_age_days=max_age_days)
    # second pass: ISIN jobs that found nothing on the exchange -> retry by ticker
    retry: dict[str, dict[str, Any]] = {}
    for lk, j in jobs.items():
        if j["idType"] == "ID_ISIN" and not res.get(_job_key(j)):
            r = next(x for x in rows if x["listing_key"] == lk)
            j2 = figi_job_for(r.get("local_symbol"), r.get("mic"), None)
            if j2:
                retry[lk] = j2
    if retry:
        res.update(map_jobs(list(retry.values()), settings=settings, max_age_days=max_age_days))
    out = []
    for lk, j in jobs.items():
        recs = res.get(_job_key(retry[lk])) if lk in retry else res.get(_job_key(j))
        best = pick_record(recs or [])
        if best:
            out.append({
                "listing_key": lk, "figi": best.get("figi"), "composite_figi": best.get("compositeFIGI"),
                "share_class_figi": best.get("shareClassFIGI"), "figi_security_type": best.get("securityType"),
                "figi_name": best.get("name"), "figi_ticker": best.get("ticker"),
            })
    schema = {"listing_key": pl.Utf8, "figi": pl.Utf8, "composite_figi": pl.Utf8, "share_class_figi": pl.Utf8,
              "figi_security_type": pl.Utf8, "figi_name": pl.Utf8, "figi_ticker": pl.Utf8}
    return pl.DataFrame(out, schema=schema) if out else pl.DataFrame(schema=schema)


# ------------------------------------------------------------------------------------------------ job


def run(ctx: Any) -> Any:
    """openfigi_refresh: map every active listing, write figi/composite_figi/share_class_figi identifier rows."""
    from app.core.storage import read_table
    from app.normalize import identity
    from app.scheduler.base import JobResult

    lst = read_table("listings").filter(pl.col("valid_to").is_null())
    sm = read_table("securities_master").select("security_id", "issuer_id", "isin", pl.col("figi").alias("__sm_figi"))
    lst = lst.join(sm, on="security_id", how="left")
    scope = ctx.params.get("scope") or ("all" if ctx.settings.secret("openfigi_apikey") else "missing")
    if scope == "missing":  # keyless budget (25 req/min x 10 jobs): only listings without a FIGI yet (non-US; Massive covers US)
        have = read_table("identifier_map").filter(pl.col("id_type") == "figi").select("listing_id").drop_nulls()
        lst = lst.filter(pl.col("__sm_figi").is_null() & ~pl.col("listing_id").is_in(have.get_column("listing_id").implode()))
    frame = lst.select(pl.col("listing_id").alias("listing_key"), "local_symbol", "mic", "isin")
    figi = map_listings(frame, settings=ctx.settings, max_age_days=int(ctx.params.get("max_age_days", 30)))
    j = lst.join(figi, left_on="listing_id", right_on="listing_key", how="inner")
    rows: list[dict[str, Any]] = []
    for r in j.iter_rows(named=True):
        for t in ("figi", "composite_figi", "share_class_figi"):
            if r.get(t):
                rows.append(dict(id_type=t, id_value=r[t], entity_level="listing" if t == "figi" else "security",
                                 issuer_id=r["issuer_id"], security_id=r["security_id"],
                                 listing_id=r["listing_id"] if t == "figi" else None, source_id="openfigi",
                                 confidence=1.0))
    n = identity.merge_identifier_rows(rows, run_id=ctx.run_id)
    return JobResult(status="success", rows_written={"identifier_map": n}, entities_ok=figi.height,
                     entities_failed=frame.height - figi.height)
