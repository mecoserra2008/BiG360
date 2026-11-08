"""Companies House Accounts Data Product (free bulk iXBRL accounts, no key) -> fundamentals_raw + filings_index.

Coverage v2 W2, UK statements. Source: https://download.companieshouse.gov.uk/ (robots.txt `Disallow:` empty)
  * daily   en_accountsdata.html            Accounts_Bulk_Data-YYYY-MM-DD.zip  (last ~60 days, ~30k accounts each)
  * monthly en_monthlyaccountsdata.html     Accounts_Monthly_Data-<Month><YYYY>.zip (last ~13 months, 2-4 GB each)
  * history historicmonthlyaccountsdata.html archive/Accounts_Monthly_Data-<Month><YYYY>.zip (2008 ->)
Every member is one iXBRL document named Prod<n>_<run>_<company number>_<period end YYYYMMDD>.html.

Creative but polite: the zips are never downloaded whole. The server (S3 / CloudFront) supports HTTP Range requests,
so the adapter reads the zip's central directory (one ranged read of a few MB - 40 MB for a monthly archive), picks
the members whose company number is a target, and range-reads only those members (one request each). Targets = the
company numbers of the UK register (regulatory_register, GLEIF registeredAs of GB entities) and of the reference
issuers (issuers.uk_company_number). Matched documents are kept gzip-compressed under
raw/companies_house/accounts/<number>/<member>.gz (small: the listed issuers' accounts only); nothing else is stored.

PIT: knowledge_date = the daily file date (the day Companies House processed the filing). Monthly archives carry no
per-filing date: knowledge_date = the last day of the archive month, knowledge_date_estimated = true (conservative).
A month whose daily files were processed is never re-read from its monthly archive (no duplicate filings).
filing_id = companies_house:<number>_<period end>_<knowledge date> (an amended filing gets its own id).
Group / company split: FRC `GroupCompanyDataDimension` and IFRS `ConsolidatedAndSeparateFinancialStatementsAxis`
members become the `consolidated` flag (the normaliser prefers consolidated facts of a filing).

State: data/state/ch_accounts.json {archives: {name: {processed_at, members, matched, rows}}} - resumable; a re-run
skips processed archives. Jobs: ch_accounts (nightly, new daily files), ch_accounts_backfill (manual / filler
campaign, params months=N or since=YYYY-MM, monthly + historic archives newest first).
"""

from __future__ import annotations

import gzip
import io
import json
import re
import time
import zipfile
from calendar import monthrange
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ingestion._ixbrl import parse_ixbrl, ixbrl_rows

log = get_logger(__name__)

SOURCE_ID = "companies_house"
BASE = "https://download.companieshouse.gov.uk/"
DAILY_PAGE = "en_accountsdata.html"
MONTHLY_PAGE = "en_monthlyaccountsdata.html"
HISTORIC_PAGE = "historicmonthlyaccountsdata.html"
STATE_FILE = "ch_accounts.json"
MEMBER_RE = re.compile(r"(?:^|/)Prod\d+_\d+_([A-Z0-9]{8})_(\d{8})\.(?:html|xhtml|xml)$", re.I)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
DAILY_RE = re.compile(r'href="((?:archive/)?Accounts_Bulk_Data-(\d{4}-\d{2}-\d{2})\.zip)"')
MONTHLY_RE = re.compile(r'href="((?:archive/)?Accounts_Monthly_Data-([A-Za-z]+)(\d{4})\.zip)"')
BLOCK = 4 << 20


# ------------------------------------------------------------------------------------------------ remote zip


class RemoteFile(io.RawIOBase):
    """Seekable read-only file over HTTP Range requests (aligned BLOCK read-ahead, one cached block)."""

    def __init__(self, url: str, http: Any, size: int | None = None, block: int = BLOCK):
        self.url, self.http, self.block = url, http, block
        self.pos = 0
        self.size = size if size is not None else self._head_size()
        self._buf_start, self._buf = -1, b""
        self.n_requests = 0
        self.n_bytes = 0

    def _head_size(self) -> int:
        r = self.http.request("GET", self.url, headers={"Range": "bytes=0-0"}, cache_ttl=0)
        cr = r.headers.get("content-range") or r.headers.get("Content-Range") or ""
        if "/" not in cr:
            raise OSError(f"no Range support for {self.url}")
        return int(cr.rsplit("/", 1)[1])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def _fetch(self, start: int, end: int) -> bytes:
        r = self.http.request("GET", self.url, headers={"Range": f"bytes={start}-{end}"}, cache_ttl=0)
        self.n_requests += 1
        self.n_bytes += len(r.content)
        if r.status_code != 206 and not (start == 0 and len(r.content) == self.size):
            raise OSError(f"range request returned HTTP {r.status_code}")
        return r.content

    def readinto(self, b: Any) -> int:
        n = len(b)
        if self.pos >= self.size or n == 0:
            return 0
        if not (self._buf_start <= self.pos < self._buf_start + len(self._buf)):
            start = self.pos
            end = min(self.size, start + max(n, self.block)) - 1
            self._buf, self._buf_start = self._fetch(start, end), start
        off = self.pos - self._buf_start
        chunk = self._buf[off: off + n]
        b[: len(chunk)] = chunk
        self.pos += len(chunk)
        return len(chunk)


def open_remote_zip(url: str, http: Any) -> tuple[zipfile.ZipFile, RemoteFile]:
    rf = RemoteFile(url, http)
    return zipfile.ZipFile(io.BufferedReader(rf, buffer_size=BLOCK)), rf


# ------------------------------------------------------------------------------------------------ pure helpers


def parse_member(name: str) -> tuple[str, date] | None:
    m = MEMBER_RE.search(name)
    if not m:
        return None
    try:
        return m.group(1).upper(), datetime.strptime(m.group(2), "%Y%m%d").date()
    except ValueError:
        return None


def norm_company_number(v: str | None) -> str | None:
    if not v:
        return None
    u = re.sub(r"[\s-]", "", str(v).strip().upper())
    if u.isdigit():
        u = u.zfill(8)
    return u if re.fullmatch(r"[A-Z0-9]{8}", u) else None


def list_archives(html: str, kind: str) -> list[dict[str, Any]]:
    """Index page HTML -> [{name, url, kind, day | month_start, month_end}] (newest first)."""
    out: list[dict[str, Any]] = []
    if kind == "daily":
        for href, d in DAILY_RE.findall(html):
            day = date.fromisoformat(d)
            out.append({"name": href.rsplit("/", 1)[-1], "url": BASE + href, "kind": "daily", "day": day,
                        "month": day.replace(day=1)})
    else:
        for href, mon, yr in MONTHLY_RE.findall(html):
            if mon not in MONTHS:  # JanuaryToDecember2008 style yearly bundles
                continue
            m = date(int(yr), MONTHS.index(mon) + 1, 1)
            out.append({"name": href.rsplit("/", 1)[-1], "url": BASE + href, "kind": "monthly", "month": m,
                        "day": m.replace(day=monthrange(m.year, m.month)[1])})
    return sorted({a["name"]: a for a in out}.values(), key=lambda a: a["day"], reverse=True)


_GROUP_DIM = re.compile(r"GroupCompanyDataDimension|ConsolidatedAndSeparateFinancialStatementsAxis", re.I)


def split_group_company(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group / company dimension -> consolidated flag. A fact whose only group-dimension member is the consolidated
    group loses that dimension (consolidated=True) and wins over an undimensioned duplicate (rows are returned with
    those first, so the PK de-duplication keeps them); company / separate-entity members stay dimensional (the
    normaliser ignores dimensional facts), other dimensions are kept."""
    from app.ingestion._global_common import dimensions_hash, dimensions_json

    first, rest = [], []
    for r in rows:
        dj = r.get("dimensions_json")
        if not dj:
            rest.append(r)
            continue
        dims = json.loads(dj)
        keys = [k for k in dims if _GROUP_DIM.search(k)]
        if not keys:
            rest.append(r)
            continue
        mem = str(dims[keys[0]])
        if re.search(r"Consolidated|Group", mem, re.I) and not re.search(r"Separate|Parent", mem, re.I):
            dims.pop(keys[0])
            first.append({**r, "consolidated": True, "dimensions_hash": dimensions_hash(dims or None),
                          "dimensions_json": dimensions_json(dims or None)})
        else:
            rest.append({**r, "consolidated": False})
    return first + rest


def member_rows(content: bytes, number: str, period_end: date, knowledge_date: date, estimated: bool,
                member: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One iXBRL accounts document -> (fundamentals_raw rows, filings_index row)."""
    from app.ingestion._global_common import filing_row

    parsed = parse_ixbrl(content)
    native = f"{number}_{period_end:%Y%m%d}_{knowledge_date:%Y%m%d}"
    fid = f"{SOURCE_ID}:{native}"
    rows = ixbrl_rows(parsed, source_id=SOURCE_ID, source_entity_id=number, filing_id=fid, form_type="AA",
                      knowledge_date=knowledge_date, knowledge_date_estimated=estimated, report_period_end=period_end)
    rows = split_group_company(rows)
    taxes = {r["taxonomy"] for r in rows}
    frow = filing_row(source_id=SOURCE_ID, native_key=native, source_entity_id=number, form_type="AA",
                      form_class="annual", knowledge_date=knowledge_date, period_end=period_end,
                      fiscal_year=period_end.year, fiscal_period="FY", language="en", url=None, has_xbrl=bool(rows),
                      items=[member, ",".join(sorted(taxes))])
    return rows, frow


# ------------------------------------------------------------------------------------------------ adapter


def state_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / STATE_FILE


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict[str, Any], settings: Settings | None = None) -> None:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def target_numbers(settings: Settings) -> set[str]:
    """UK register company numbers (GB entities) + reference issuers' Companies House numbers."""
    from app.core.storage import read_table

    out: set[str] = set()
    try:
        reg = read_table("regulatory_register", settings=settings).filter(
            (pl.col("market") == "UK") | (pl.col("country") == "GB"))
        for num, ctry, jur in reg.select("company_number", "country", "jurisdiction").iter_rows():
            if (ctry == "GB" or (jur or "").startswith("GB")) and norm_company_number(num):
                out.add(norm_company_number(num))
    except Exception as e:  # noqa: BLE001 - register not built yet
        log.warning("regulatory_register unavailable: %s", e)
    try:
        iss = read_table("issuers", columns=["uk_company_number"], settings=settings)
        out |= {n for n in (norm_company_number(x) for x in iss["uk_company_number"].to_list()) if n}
    except Exception:  # noqa: BLE001
        pass
    return out


class ChAccounts:
    def __init__(self, settings: Settings | None = None, http: Any = None):
        from app.core.http import HttpClient
        from app.core.ratelimit import RateLimit

        self.s = settings or get_settings()
        self.http = http or HttpClient("companies_house_bulk", settings=self.s, rate_limit=RateLimit(2.0, 2),
                                       timeout=120)
        self.state = load_state(self.s)
        self.state.setdefault("archives", {})

    def archives(self, kinds: Iterable[str] = ("daily", "monthly")) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for kind in kinds:
            pages = [DAILY_PAGE] if kind == "daily" else ([MONTHLY_PAGE, HISTORIC_PAGE] if kind == "monthly" else [])
            for pg in pages:
                html = self.http.get(BASE + pg, cache_ttl=6 * 3600).text
                out += list_archives(html, "daily" if kind == "daily" else "monthly")
        return sorted({a["name"]: a for a in out}.values(), key=lambda a: a["day"], reverse=True)

    def daily_months(self) -> set[str]:
        return {a.get("month") for a in self.state["archives"].values() if a.get("kind") == "daily" and a.get("month")}

    def process(self, arc: dict[str, Any], targets: set[str], write: bool = True) -> dict[str, Any]:
        from app.core.schema import conform
        from app.core.storage import write_table
        from app.ingestion._global_common import frame

        t0 = time.time()
        z, rf = open_remote_zip(arc["url"], self.http)
        names = z.namelist()
        hits = []
        for n in names:
            pm = parse_member(n)
            if pm and pm[0] in targets:
                hits.append((n, *pm))
        daily = arc["kind"] == "daily"
        kd = arc["day"]
        facts: list[dict[str, Any]] = []
        filings: list[dict[str, Any]] = []
        failed: dict[str, str] = {}
        raw_root = self.s.raw_dir / SOURCE_ID / "accounts"
        for n, num, pe in hits:
            try:
                content = z.read(n)
                d = raw_root / num
                d.mkdir(parents=True, exist_ok=True)
                (d / (Path(n).name + ".gz")).write_bytes(gzip.compress(content, 6))
                rows, frow = member_rows(content, num, pe, kd, not daily, n)
                frow["url"] = f"{arc['url']}#{Path(n).name}"
                frow["doc_path"] = str((d / (Path(n).name + ".gz")).relative_to(self.s.data_dir))
                facts += rows
                filings.append(frow)
            except Exception as e:  # noqa: BLE001
                failed[n] = f"{type(e).__name__}: {e}"[:200]
        n_rows = 0
        if write:
            if filings:
                write_table(frame(filings, "filings_index"), "filings_index", settings=self.s)
            if facts:
                n_rows = write_table(conform(frame(facts, "fundamentals_raw"), "fundamentals_raw"), "fundamentals_raw",
                                     settings=self.s)
        info = {"kind": arc["kind"], "month": arc["month"].isoformat(), "day": kd.isoformat(), "members": len(names),
                "matched": len(hits), "parsed": len(filings), "failed": len(failed), "rows": n_rows,
                "requests": rf.n_requests, "mb": round(rf.n_bytes / 1e6, 1), "seconds": round(time.time() - t0, 1),
                "processed_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if failed:
            info["failed_sample"] = dict(list(failed.items())[:3])
        if write:
            self.state["archives"][arc["name"]] = info
            save_state(self.state, self.s)
        log.info("ch_accounts %s: %d members, %d matched, %d rows (%d requests, %.1f MB, %.0fs)", arc["name"],
                 len(names), len(hits), n_rows, rf.n_requests, rf.n_bytes / 1e6, time.time() - t0)
        return info

    def run(self, mode: str = "daily", months: int | None = None, since: str | None = None,
            max_archives: int | None = None, max_minutes: float | None = None, write: bool = True,
            targets: set[str] | None = None) -> dict[str, Any]:
        targets = targets if targets is not None else target_numbers(self.s)
        if not targets:
            return {"status": "skipped", "message": "no target company numbers (register / issuers empty)"}
        done = set(self.state["archives"])
        if mode == "daily":
            todo = [a for a in self.archives(("daily",)) if a["name"] not in done]
        else:
            dm = self.daily_months()
            arcs = self.archives(("monthly",))
            lo = None
            if since:
                lo = date.fromisoformat(since + "-01" if len(since) == 7 else since)
            elif months:
                today = datetime.now(UTC).date().replace(day=1)
                y, m = today.year, today.month - int(months)
                while m <= 0:
                    y, m = y - 1, m + 12
                lo = date(y, m, 1)
            todo = [a for a in arcs if a["name"] not in done and a["month"].isoformat() not in dm
                    and (lo is None or a["month"] >= lo)]
        if max_archives is not None:
            todo = todo[: int(max_archives)]
        t0 = time.time()
        res: dict[str, Any] = {"mode": mode, "targets": len(targets), "todo": len(todo), "archives": {}, "errors": {}}
        for a in todo:
            if max_minutes is not None and (time.time() - t0) / 60 > max_minutes:
                res["stopped"] = "time budget"
                break
            try:
                res["archives"][a["name"]] = self.process(a, targets, write=write)
            except Exception as e:  # noqa: BLE001
                res["errors"][a["name"]] = f"{type(e).__name__}: {e}"[:300]
                log.exception("ch_accounts %s failed", a["name"])
        res["remaining"] = len(todo) - len(res["archives"]) - len(res["errors"])
        res["status"] = "failed" if res["errors"] and not res["archives"] else ("partial" if res["errors"] or res["remaining"] else "success")
        return res


# ------------------------------------------------------------------------------------------------ jobs


def _job(ctx: Any, mode: str) -> Any:
    from app.scheduler.base import JobResult

    p = dict(ctx.params or {})
    ad = ChAccounts(ctx.settings)
    res = ad.run(mode=p.get("mode", mode), months=p.get("months"), since=p.get("since"),
                 max_archives=p.get("max_archives"), max_minutes=p.get("max_minutes"))
    if res.get("status") == "skipped":
        return JobResult(status="skipped", message=res["message"])
    rows = sum(a.get("rows", 0) for a in res["archives"].values())
    matched = sum(a.get("matched", 0) for a in res["archives"].values())
    msg = (f"{mode}: {len(res['archives'])} archives, {matched} matched accounts, {rows} facts, "
           f"{res['remaining']} archives left; targets {res['targets']}")
    if res["errors"]:
        msg += f"; errors {list(res['errors'].items())[:2]}"
    return JobResult(status=res["status"], rows_written={"fundamentals_raw": rows}, entities_ok=matched,
                     entities_failed=len(res["errors"]), watermark=datetime.now(UTC).date().isoformat(), message=msg[:900])


def run(ctx: Any) -> Any:
    """Job ch_accounts: new daily Accounts Data Product files."""
    return _job(ctx, "daily")


def run_backfill(ctx: Any) -> Any:
    """Job ch_accounts_backfill: monthly + historic archives (params months / since / max_archives / max_minutes)."""
    return _job(ctx, "monthly")


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.ingestion.ch_accounts")
    ap.add_argument("mode", choices=["daily", "monthly", "status"])
    ap.add_argument("--months", type=int)
    ap.add_argument("--since")
    ap.add_argument("--max-archives", type=int)
    ap.add_argument("--max-minutes", type=float)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    from app.core.logsetup import setup_logging

    setup_logging(log_file="ch_accounts.log", service="cli")
    if a.mode == "status":
        st = load_state()
        print(json.dumps({k: v for k, v in st.get("archives", {}).items()}, indent=1)[:20000])
        return 0
    res = ChAccounts().run(mode=a.mode, months=a.months, since=a.since, max_archives=a.max_archives,
                           max_minutes=a.max_minutes, write=not a.dry_run)
    print(json.dumps(res, indent=1, default=str)[:20000])
    return 0 if res.get("status") in ("success", "partial", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
