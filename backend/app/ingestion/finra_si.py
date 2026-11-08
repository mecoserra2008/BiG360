"""FINRA equity short interest -> short_interest, SEC fails-to-deliver -> fails_to_deliver. Job `finra_short_interest`.

Short interest: `https://cdn.finra.org/equity/otcmarket/biweekly/shrt<YYYYMMDD>.csv` (pipe-delimited, quoted header;
exchange-listed + OTC since June 2021). Settlement dates are mid-month and month-end; the adapter probes candidate
dates after the watermark (missing files answer 403/404). knowledge_date = CDN Last-Modified date (observed
publication), else settlement + 8 business days (knowledge_date_estimated=true).
security_id: resolved through ref-identity (ticker_mic) when possible, otherwise the provisional key
'finra:<SYMBOL>' with security_id_resolved=false (the contract PK requires a non-null security_id).

Fails-to-deliver: `cnsfailsYYYYMM{a|b}.zip` links scraped from the SEC landing page (pipe-delimited
SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE). knowledge_date = zip Last-Modified date, else
end of the half-month + 14 days (estimated).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import polars as pl

from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.ratelimit import RateLimit
from app.core.schema import empty_frame
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter
from app.ingestion.sec_index import SEC_RATE, SEC_SOURCE, SEC_WWW, parse_since, try_lookup_many

log = get_logger(__name__)

FINRA_SI_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d}.csv"
FTD_LANDING = f"{SEC_WWW}/data-research/sec-markets-data/fails-deliver-data"
MARKET_MIC = {"NYSE": "XNYS", "NNM": "XNAS", "NAS": "XNAS", "NCM": "XNAS", "NGM": "XNAS", "NSM": "XNAS",
              "Nasdaq": "XNAS", "SC": "XNAS", "ARCA": "ARCX", "AMEX": "XASE", "BZX": "BATS", "IEXG": "IEXG",
              "TXSE": "XTXS"}  # SC = Nasdaq Capital Market (1,666 symbols; mapped to XNYS before 2026-10-27)


def business_days_after(d: date, n: int) -> date:
    x = d
    while n > 0:
        x += timedelta(days=1)
        if x.weekday() < 5:
            n -= 1
    return x


def candidate_settlement_dates(start: date, end: date) -> list[date]:
    """Mid-month (around the 15th) and month-end settlement candidates between start (excl.) and end (incl.).
    FINRA uses the 15th and the last business day, shifted earlier for weekends/holidays -> probe a few days back."""
    out: set[date] = set()
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        mid = date(y, m, 15)
        nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        eom = nxt - timedelta(days=1)
        for anchor in (mid, eom):
            for back in range(0, 4):
                d = anchor - timedelta(days=back)
                if d.weekday() < 5 and start < d <= end:
                    out.add(d)
        y, m = nxt.year, nxt.month
    return sorted(out, reverse=True)


def _last_modified(headers: dict[str, str]) -> date | None:
    lm = headers.get("last-modified") or headers.get("Last-Modified")
    if not lm:
        return None
    try:
        return parsedate_to_datetime(lm).date()
    except (TypeError, ValueError):
        return None


def parse_finra_si(content: bytes, knowledge_date: date, estimated: bool, now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    df = pl.read_csv(io.BytesIO(content), separator="|", infer_schema=False, quote_char='"', truncate_ragged_lines=True,
                     encoding="utf8-lossy")
    df = df.filter(pl.col("symbolCode").is_not_null() & (pl.col("symbolCode").str.strip_chars() != ""))
    num = lambda c: pl.col(c).str.replace_all(",", "").cast(pl.Float64, strict=False)  # noqa: E731
    out = df.select(
        pl.col("symbolCode").str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.col("settlementDate").str.to_date("%Y-%m-%d", strict=False).alias("settlement_date"),
        num("currentShortPositionQuantity").alias("short_interest_shares"),
        num("averageDailyVolumeQuantity").alias("avg_daily_volume"),
        num("daysToCoverQuantity").alias("days_to_cover"),
        pl.col("issueName").str.strip_chars().alias("issue_name"),
        pl.col("marketClassCode").str.strip_chars().alias("market_class"),
    ).filter(pl.col("settlement_date").is_not_null() & pl.col("short_interest_shares").is_not_null())
    # one row per symbol & date (a symbol can appear for several market classes in rare cases -> keep the largest)
    out = out.sort("short_interest_shares", descending=True).unique(["symbol", "settlement_date"], keep="first")
    return out.with_columns(
        pl.col("settlement_date").dt.year().cast(pl.Int16).alias("year"),
        pl.lit(None, pl.Float64).alias("short_pct_float"),
        pl.lit(knowledge_date).alias("knowledge_date"),
        pl.lit("finra").alias("source_id"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
        pl.lit(estimated).alias("knowledge_date_estimated"),
    )


def resolve_security_ids(df: pl.DataFrame) -> pl.DataFrame:
    """security_id of the US LISTING (SYMBOL@MIC, then the compact class-share / ETF symbol map of
    app.normalize.us_symbols), else provisional 'finra:<SYMBOL>'. The listing's own security is used, never the
    issuer's primary security: an ADR's short interest (ASML, BABA, ABEV) was attributed to the home listing before
    2026-10-27 (completeness audit)."""
    keys = [f"{s}@{MARKET_MIC.get(mc or '', 'XNYS')}" for s, mc in zip(df["symbol"], df["market_class"], strict=True)]
    m = try_lookup_many("ticker_mic", keys)
    sec = {k: (v.get("security_id") or v.get("primary_security_id")) for k, v in m.items()} if m else {}
    ids = [sec.get(k) for k in keys]
    if any(i is None for i in ids):
        try:
            from app.normalize import us_symbols  # noqa: PLC0415

            smap = us_symbols.listed_symbol_map()
            fb = us_symbols.resolve_symbols(df, "symbol", "settlement_date", smap, None, "market_class").to_list()
            ids = [i or f for i, f in zip(ids, fb, strict=True)]
        except Exception as e:  # noqa: BLE001 - identity layer not ready: provisional keys, identity_resolve retries
            log.debug("us_symbols fallback unavailable: %s", e)
    return df.with_columns(
        pl.Series("security_id", [i or f"finra:{s}" for i, s in zip(ids, df["symbol"], strict=True)], dtype=pl.Utf8),
        pl.Series("security_id_resolved", [i is not None for i in ids], dtype=pl.Boolean),
    )


def parse_ftd_zip(content: bytes, file_name: str, knowledge_date: date, estimated: bool,
                  now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    z = zipfile.ZipFile(io.BytesIO(content))
    member = next(n for n in z.namelist() if not n.endswith("/"))
    raw = z.read(member).decode("latin-1")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    rows = []
    for ln in lines[1:]:
        parts = ln.split("|")
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue  # trailer lines ("Trailer record count ...")
        desc = "|".join(parts[4:-1]) if len(parts) > 6 else parts[4]
        rows.append((parts[0].strip(), parts[1].strip().upper(), parts[2].strip() or None, parts[3].strip(), desc.strip(),
                     parts[-1].strip()))
    df = pl.DataFrame(rows, schema=["d", "cusip", "symbol", "q", "description", "p"], orient="row")
    df = df.select(
        pl.col("cusip"),
        pl.col("d").str.to_date("%Y%m%d", strict=False).alias("settlement_date"),
        pl.col("symbol"),
        pl.col("description"),
        pl.col("q").cast(pl.Float64, strict=False).alias("quantity_fails"),
        pl.col("p").cast(pl.Float64, strict=False).alias("price"),
    ).filter(pl.col("settlement_date").is_not_null() & pl.col("quantity_fails").is_not_null() & (pl.col("cusip") != ""))
    df = df.group_by(["cusip", "settlement_date"], maintain_order=True).agg(
        pl.col("symbol").first(), pl.col("description").first(), pl.col("quantity_fails").sum(), pl.col("price").first())
    return df.with_columns(
        pl.col("settlement_date").dt.year().cast(pl.Int16).alias("year"),
        pl.lit(None, pl.Utf8).alias("security_id"),
        pl.lit(file_name).alias("file_name"),
        pl.lit(knowledge_date).alias("knowledge_date"),
        pl.lit(estimated).alias("knowledge_date_estimated"),
        pl.lit("sec_ftd").alias("source_id"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
    )


def ftd_period_end(name: str) -> date | None:
    m = re.search(r"cnsfails(\d{4})(\d{2})([ab])", name)
    if not m:
        return None
    y, mo, half = int(m.group(1)), int(m.group(2)), m.group(3)
    if half == "a":
        return date(y, mo, 15)
    nxt = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    return nxt - timedelta(days=1)


@register_adapter
class FinraShortInterestAdapter(SourceAdapter):
    source_id = "finra_si"
    regions = ("US",)
    produces = ("short_interest", "fails_to_deliver")
    rate_limit = RateLimit(2.0, 2)

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, sec_http: HttpClient | None = None, **kw: Any):
        if "http" not in kw or kw["http"] is None:
            from app.core.config import get_settings  # noqa: PLC0415

            s = kw.get("settings") or get_settings()
            kw["http"] = HttpClient("finra", rate_limit=RateLimit(2.0, 2), settings=s)
        super().__init__(*a, **kw)
        self.params = params or {}
        self.sec_http = sec_http or HttpClient(SEC_SOURCE, rate_limit=SEC_RATE, settings=self.settings)

    def raw_dir(self, dataset: str, day: date | None = None):
        d = day or datetime.now(UTC).date()
        src = "sec_edgar" if dataset == "ftd" else "finra"
        p = self.settings.raw_dir / src / dataset / f"dt={d.isoformat()}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], "finra")

    @property
    def state_path(self):
        return self.settings.data_dir / "state" / f"{self.source_id}.json"

    def load_state(self) -> dict:
        import orjson  # noqa: PLC0415

        p = self.state_path
        return orjson.loads(p.read_bytes()) if p.exists() else {}

    def save_state(self, **kv: Any) -> None:
        import orjson  # noqa: PLC0415

        st = self.load_state()
        st.update(kv)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_bytes(orjson.dumps(st, option=orjson.OPT_INDENT_2))

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id="finra", started_at=datetime.now(UTC))
        today = datetime.now(UTC).date()
        st = self.load_state()
        si_since = parse_since(since) or parse_since(st.get("si_watermark")) or (
            today - timedelta(days=int(self.params.get("lookback_days", 20))))
        files: list[dict] = []
        found: list[date] = []
        for d in candidate_settlement_dates(si_since, today):
            if any(abs((d - f).days) <= 4 for f in found):
                continue  # already found the settlement file of this half-month
            url = FINRA_SI_URL.format(d=d.strftime("%Y%m%d"))
            try:
                r = self.http.get(url, cache_ttl=-1, raise_for_status=False)
            except HttpError as e:
                res.entities_failed[f"si:{d}"] = str(e)[:200]
                continue
            if r.status_code != 200 or len(r.content) < 1000:
                continue
            lm = _last_modified(r.headers)
            kd, est = (lm, False) if lm else (business_days_after(d, 8), True)
            p = self.save_raw("short_interest", f"shrt{d.strftime('%Y%m%d')}.csv", r.content, day=d)
            files.append({"kind": "si", "path": p, "settlement": d, "kd": kd, "est": est})
            found.append(d)
            res.entities_ok.append(f"si:{d}")
        res.meta["si_watermark"] = max(found).isoformat() if found else si_since.isoformat()
        # fails-to-deliver
        if not self.params.get("skip_ftd"):
            html = self.sec_http.get(FTD_LANDING, cache_ttl=6 * 3600).text
            links = {}
            for href in re.findall(r'href="([^"]*cnsfails\d{6}[ab]\.zip)"', html):
                links[href.rsplit("/", 1)[-1][:-4]] = href if href.startswith("http") else SEC_WWW + href
            done = set(st.get("ftd_done") or [])
            names = sorted(links, reverse=True)[: int(self.params.get("ftd_n_latest", 1))]
            names = [n for n in names if n not in done or self.params.get("force")]
            for n in names:
                try:
                    r = self.sec_http.get(links[n], cache_ttl=-1)
                    lm = _last_modified(r.headers)
                    pe = ftd_period_end(n) or today
                    kd, est = (lm, False) if lm else (pe + timedelta(days=14), True)
                    p = self.save_raw("ftd", n + ".zip", r.content, compress=False)
                    files.append({"kind": "ftd", "path": p, "name": n, "kd": kd, "est": est})
                    res.entities_ok.append(f"ftd:{n}")
                except Exception as e:  # noqa: BLE001
                    res.entities_failed[f"ftd:{n}"] = str(e)[:200]
        res.meta["files"] = files
        res.watermark = res.meta["si_watermark"]
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        now = datetime.now(UTC)
        si, ftd = [], []
        for f in fetched.meta.get("files", []):
            if f["kind"] == "si":
                si.append(parse_finra_si(self.load_raw(f["path"]), f["kd"], f["est"], now))
            else:
                ftd.append(parse_ftd_zip(f["path"].read_bytes(), f["name"], f["kd"], f["est"], now))
        out: dict[str, pl.DataFrame] = {}
        if si:
            df = resolve_security_ids(pl.concat(si, how="vertical_relaxed"))
            df = df.unique(["security_id", "settlement_date"], keep="first")
            fetched.meta["n_si"] = df.height
            fetched.meta["n_si_resolved"] = int(df["security_id_resolved"].sum())
            out["short_interest"] = df
        else:
            out["short_interest"] = empty_frame("short_interest")
        if ftd:
            df = pl.concat(ftd, how="vertical_relaxed").unique(["cusip", "settlement_date"], keep="last")
            m = try_lookup_many("cusip", df["cusip"].unique().to_list())
            if m:
                sec = {k: v.get("primary_security_id") or v.get("security_id") for k, v in m.items()}
                df = df.with_columns(pl.col("cusip").replace_strict(sec, default=None).alias("security_id"))
            fetched.meta["n_ftd"] = df.height
            out["fails_to_deliver"] = df
        else:
            out["fails_to_deliver"] = empty_frame("fails_to_deliver")
        return out


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = FinraShortInterestAdapter(settings=ctx.settings, params=dict(ctx.params))
    since = ctx.params.get("since") or ctx.watermark
    fetched, rows = ad.run(since=since)
    if fetched.status != "failed":
        ftd_done = set(ad.load_state().get("ftd_done") or []) | {
            e.split(":", 1)[1] for e in fetched.entities_ok if e.startswith("ftd:")}
        ad.save_state(si_watermark=fetched.meta.get("si_watermark"), ftd_done=sorted(ftd_done))
    return JobResult(status=fetched.status if (fetched.entities_ok or fetched.entities_failed) else "success",
                     rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=fetched.watermark,
                     http_requests=ad.http.n_requests + ad.sec_http.n_requests,
                     cache_hits=ad.http.n_cache_hits + ad.sec_http.n_cache_hits,
                     message=f"si_rows={fetched.meta.get('n_si')} resolved={fetched.meta.get('n_si_resolved')} ftd_rows={fetched.meta.get('n_ftd')}")
