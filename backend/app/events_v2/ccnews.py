"""N1 CC-NEWS historical backfill (docs/ROADMAP_V3_PLAN.md N1): Common Crawl news WARCs -> derived metadata only.

    python -m app.events_v2.ccnews --months 2026-09                 pilot: one month end to end
    python -m app.events_v2.ccnews --start-month 2016-08            campaign (gap filler): newest month first
    python -m app.events_v2.ccnews --finalize-only                  merge spooled rows, no downloads
    python -m app.events_v2.ccnews --status                         print the state summary

Source: data.commoncrawl.org/crawl-data/CC-NEWS/YYYY/MM/warc.paths.gz (one ~1 GB WARC every ~1-2 h of crawl).

Pipeline per month (newest first; resumable at file granularity; state data/state/ccnews.json):
  1. index: warc.paths.gz (cached in data/tmp/ccnews/paths/, refreshed for the two newest months);
  2. download + parse: a process pool (default 4 workers = 4 polite connections) streams each WARC IN MEMORY
     through app.events_v2.ccnews_parse (HTML head only, never the body text) -> labelled rows ("label, then
     forget": kept iff a market theme or an issuer link). Raw WARC bytes never touch the disk, so the
     "<= 20 GB of unprocessed WARC" cap holds by construction; the cap is still enforced on data/tmp/ccnews (spool);
  3. spool: kept rows per publication day -> data/tmp/ccnews/spool/<date>/<month>-<idx>.parquet (derived only);
  4. finalize (writer thread, overlaps the next month's downloads): per day -> app.events_v2.hist_merge
     (ccnews_articles + story layer: URL-hash / MinHash dedupe against GDELT and the other sources, new source id
     `ccnews`), then batched FinBERT / XLM-R story sentiment (one model loaded at a time, per-month cache);
     the spool of merged days is deleted.
Stops on data/state/ccnews/STOP: no new WARC starts, in-flight WARCs get STOP_DRAIN_S (60 s) to finish, then the
download pool is terminated (unfinished WARCs are not marked done: redone next run - incident 2026-10-24, a STOP
used to wait for every in-flight ~1 GB WARC and was only enforced by the filler's 15-min kill); pauses while
build_serving_db runs; with --yield-to
<campaign> (gap-filler mode) it exits with code 3 when that campaign (E6) is waiting for the filler, so the GDELT
backfill keeps its priority. One instance at a time (data/.locks/ccnews_backfill.lock).
Common Crawl terms of use: docs/it/DATA_LICENSING.md (derived metadata only; publishers keep their copyright).

DISABLED 2026-11-07 (docs/COVERAGE_V3_PLAN.md A9, docs/it/ROBOTS_COMPLIANCE.md): data.commoncrawl.org robots.txt is
`User-Agent: *` / `Disallow: /`. No index or WARC is requested any more (app.core.robots_policy; the raw httpx calls
below check it too); the campaign probe reports done with reason source_disallowed so the gap filler stops starting
it. --finalize-only, --relink, --status and --local-dir (no download) still work; ccnews rows already merged stay.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.events_v2 import ccnews_parse as P

FIRST_MONTH = "2016-08"           # first CC-NEWS month
INDEX_URL = P.BASE_URL + "crawl-data/CC-NEWS/{y}/{m:02d}/warc.paths.gz"
DISK_CAP_BYTES = 20 * 1024**3     # unprocessed / spooled bytes in data/tmp/ccnews
EXIT_BLOCKED = 3
STOP_DRAIN_S = 60.0               # in-flight WARCs may finish this long after a STOP, then the pool is terminated
CHILD_TIMEOUT_S = 3 * 3600.0      # finalize / sentiment child processes are killed after this


def _abort_pool(pool: ProcessPoolExecutor) -> int:
    """Terminate the workers of a download pool (stop requested: their WARCs are redone next run)."""
    procs = list((getattr(pool, "_processes", None) or {}).values())
    for pr in procs:
        try:
            pr.terminate()
        except Exception:  # noqa: BLE001
            pass
    pool.shutdown(wait=False, cancel_futures=True)
    return len(procs)
SPOOL_SCHEMA = {"url": pl.Utf8, "url_hash": pl.Int64, "domain": pl.Utf8, "lang": pl.Utf8, "ts": pl.Int64,
                "ts_source": pl.Int8, "title": pl.Utf8, "description": pl.Utf8, "theme_mask": pl.Int64,
                "sector_mask": pl.Int16, "countries": pl.List(pl.Utf8), "issuer_ids": pl.List(pl.Int32),
                "issuer_conf": pl.List(pl.Int8), "crawl_ts": pl.Int64}


# ================================================================================================ paths / state
def tmp_dir(s: Settings) -> Path:
    p = s.data_dir / "tmp" / "ccnews"
    p.mkdir(parents=True, exist_ok=True)
    return p


def spool_dir(s: Settings) -> Path:
    p = tmp_dir(s) / "spool"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(s: Settings) -> Path:
    return s.state_dir / "ccnews.json"


def stop_path(s: Settings) -> Path:
    return s.state_dir / "ccnews" / "STOP"


def read_state(s: Settings | None = None) -> dict[str, Any]:
    p = state_path(s or get_settings())
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def update_state(fn: Any, s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    p = state_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        st = read_state(s)
        st.setdefault("plan", "docs/ROADMAP_V3_PLAN.md N1 (CC-NEWS historical, derived metadata only)")
        fn(st)
        st["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, p)
    return st


def ranges_to_set(r: str | None) -> set[int]:
    out: set[int] = set()
    for part in (r or "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def set_to_ranges(s: set[int]) -> str:
    xs = sorted(s)
    out, i = [], 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(str(xs[i]) if i == j else f"{xs[i]}-{xs[j]}")
        i = j + 1
    return ",".join(out)


def month_range(start: str, end: str) -> list[str]:
    """Months start..end inclusive, NEWEST first."""
    y, m = int(end[:4]), int(end[5:7])
    out = []
    while f"{y:04d}-{m:02d}" >= start:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def month_closed(ym: str, today: date | None = None, grace_days: int = 3) -> bool:
    y, m = int(ym[:4]), int(ym[5:7])
    first_next = date(y + (m == 12), m % 12 + 1, 1)
    return (today or datetime.now(UTC).date()) >= first_next + timedelta(days=grace_days)


# ================================================================================================ index
def month_index(ym: str, s: Settings, refresh: bool | None = None) -> list[str]:
    """WARC paths of a month (cached; the open / newest months are re-fetched when older than 6 h)."""
    cache = tmp_dir(s) / "paths" / f"{ym}.txt"
    stale = not cache.exists() or (not month_closed(ym, grace_days=7) and time.time() - cache.stat().st_mtime > 6 * 3600)
    if refresh is True or (refresh is None and stale):
        import httpx  # noqa: PLC0415

        url = INDEX_URL.format(y=int(ym[:4]), m=int(ym[5:7]))
        from app.core.robots_policy import guard  # noqa: PLC0415

        guard(url)  # A9: data.commoncrawl.org is robots-disallowed -> SourceDisallowed, no request
        for attempt in range(6):
            try:
                r = httpx.get(url, headers={"User-Agent": P.USER_AGENT}, timeout=60, follow_redirects=True)
                if r.status_code == 404:
                    return []
                r.raise_for_status()
                paths = [x.strip() for x in gzip.decompress(r.content).decode().splitlines() if x.strip()]
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text("\n".join(paths), encoding="utf-8")
                return paths
            except Exception:  # noqa: BLE001
                if attempt == 5:
                    if cache.exists():
                        break
                    raise
                time.sleep(5 * 2 ** attempt)
    return [x for x in cache.read_text(encoding="utf-8").splitlines() if x]


# ================================================================================================ labelling ctx
_TLD_SKIP = {"io", "ai", "co", "tv", "me", "fm", "ly", "to", "cc", "ws", "gg", "la", "am", "eu", "su", "nu", "tk",
             "ml", "ga", "cf", "gq", "is", "it", "in", "id", "so", "sh", "ag", "vc", "lc", "gd", "ms", "st", "md"}
_COUNTRY_SKIP = {"jordan", "chad", "georgia", "guinea", "niger", "turkey", "mali", "peru", "togo", "oman", "panama",
                 "cuba"}
_COUNTRY_EXTRA = {
    "united states": "US", "usa": "US", "us": "US", "america": "US", "uk": "GB", "britain": "GB",
    "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "russia": "RU", "south korea": "KR",
    "north korea": "KP", "iran": "IR", "syria": "SY", "vietnam": "VN", "taiwan": "TW", "hong kong": "HK",
    "macau": "MO", "czech republic": "CZ", "laos": "LA", "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ",
    "moldova": "MD", "ivory coast": "CI", "uae": "AE", "united arab emirates": "AE", "saudi arabia": "SA",
    "palestine": "PS", "gaza": "PS", "turkiye": "TR", "netherlands": "NL", "holland": "NL", "philippines": "PH",
    "american": "US", "british": "GB", "chinese": "CN", "japanese": "JP", "german": "DE", "french": "FR",
    "italian": "IT", "spanish": "ES", "canadian": "CA", "mexican": "MX", "brazilian": "BR", "indian": "IN",
    "russian": "RU", "ukrainian": "UA", "australian": "AU", "south korean": "KR", "north korean": "KP",
    "israeli": "IL", "iranian": "IR", "saudi": "SA", "turkish": "TR", "swiss": "CH", "dutch": "NL",
    "swedish": "SE", "norwegian": "NO", "danish": "DK", "finnish": "FI", "polish": "PL", "irish": "IE",
    "belgian": "BE", "austrian": "AT", "greek": "GR", "portuguese": "PT", "argentine": "AR", "argentinian": "AR",
    "chilean": "CL", "colombian": "CO", "peruvian": "PE", "egyptian": "EG", "nigerian": "NG", "south african": "ZA",
    "pakistani": "PK", "indonesian": "ID", "malaysian": "MY", "singaporean": "SG", "thai": "TH",
    "vietnamese": "VN", "filipino": "PH", "taiwanese": "TW", "beijing": "CN", "tokyo": "JP", "moscow": "RU",
    "kremlin": "RU", "kyiv": "UA", "kiev": "UA", "wall street": "US", "new york": "US", "shanghai": "CN",
    "shenzhen": "CN", "mumbai": "IN", "new delhi": "IN", "seoul": "KR", "taipei": "TW", "tehran": "IR",
    "jerusalem": "IL", "tel aviv": "IL", "ankara": "TR", "riyadh": "SA", "berlin": "DE", "paris": "FR",
    "london": "GB", "frankfurt": "DE", "brasilia": "BR", "ottawa": "CA", "canberra": "AU", "pyongyang": "KP",
}


def _gazetteers() -> tuple[dict[str, str], dict[str, str]]:
    names: dict[str, str] = {}
    tld: dict[str, str] = {}
    try:
        import pycountry  # noqa: PLC0415

        for c in pycountry.countries:
            for n in (c.name, getattr(c, "common_name", None), getattr(c, "official_name", None)):
                k = P.norm_text(n) if n else ""
                if k and k not in _COUNTRY_SKIP and len(k) >= 4:
                    names.setdefault(k, c.alpha_2)
            if c.alpha_2.lower() not in _TLD_SKIP:
                tld[c.alpha_2.lower()] = c.alpha_2
    except Exception:  # noqa: BLE001 - pycountry optional
        pass
    names.update(_COUNTRY_EXTRA)
    tld["uk"] = "GB"
    tld.pop("gb", None)
    return names, tld


def build_label_ctx(s: Settings, max_age_h: float = 72.0) -> P.LabelCtx:
    """LabelCtx from the events-v2 worker context (alias table + issuer dictionary; rebuilt when stale)."""
    import pickle  # noqa: PLC0415

    from app.events_v2.context import build_context  # noqa: PLC0415

    pkl = s.state_dir / "events_v2" / "worker_ctx.pkl"
    if pkl.exists() and time.time() - pkl.stat().st_mtime < max_age_h * 3600:
        with open(pkl, "rb") as fh:
            wctx = pickle.load(fh)  # noqa: S301 - own file
    else:
        wctx, _ = build_context(s, refresh=True)
    return label_ctx_from_worker(wctx)


def label_ctx_from_worker(wctx: Any) -> P.LabelCtx:
    """events_v2.parse.WorkerContext (polars) -> plain-Python LabelCtx for the CC-NEWS workers."""
    from app.events_v2 import dicts  # noqa: PLC0415
    from app.events_v2 import taxonomy as tx  # noqa: PLC0415
    from app.nlp.gdelt_link import BRAND_CUES, CONTRA_CUES, FINANCE_CUES  # noqa: PLC0415

    aliases: dict[str, tuple[tuple[int, ...], float, bool]] = {}
    for org, ids, conf, amb in wctx.aliases.select("org_norm", "issuer_ids", "conf", "ambiguous").iter_rows():
        if org and ids:
            aliases[org] = (tuple(int(i) for i in ids), float(conf), bool(amb))
    iss = wctx.issuers
    tick: dict[str, list[int]] = {}
    for iid, t in iss.select("iid", "ticker").iter_rows():
        if t and 2 <= len(t) <= 6 and re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]*", t):
            tick.setdefault(t.upper(), []).append(int(iid))
    names, tld = _gazetteers()
    return P.LabelCtx(
        aliases=aliases, names=dict(wctx.names),
        issuer_country={int(i): c for i, c in iss.select("iid", "country").iter_rows() if c},
        issuer_sector={int(i): int(m or 0) for i, m in iss.select("iid", "sector_mask").iter_rows()},
        tickers={k: tuple(v) for k, v in tick.items()},
        theme_rx=[(b, re.compile(rx)) for b, rx in tx._TITLE_RX],
        theme_sector=list(tx._SECTOR_BY_BIT), countries=names, tld=tld,
        finance_cues=FINANCE_CUES, brand_cues=dict(BRAND_CUES), contra_cues=dict(CONTRA_CUES),
        common_words=frozenset(dicts._common_words()) | frozenset(dicts._COMMON), lower_share=P.load_case_words())


# ================================================================================================ spool
def rows_frame(rows: list[tuple]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=SPOOL_SCHEMA)
    cols = list(zip(*rows, strict=True))
    return pl.DataFrame({k: list(cols[i]) for i, k in enumerate(SPOOL_SCHEMA)}, schema=SPOOL_SCHEMA)


def write_spool(month: str, idx: int, rows: list[tuple], s: Settings) -> int:
    """Kept rows of one WARC -> spool/<pub date>/<month>-<idx>.parquet. Returns bytes written."""
    df = rows_frame(rows)
    if df.is_empty():
        return 0
    df = df.with_columns(pl.from_epoch("ts", time_unit="s").dt.date().alias("__d"))
    n = 0
    for (d,), part in df.partition_by("__d", as_dict=True).items():
        p = spool_dir(s) / d.isoformat() / f"{month}-{idx:05d}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        part.drop("__d").write_parquet(tmp, compression="zstd", compression_level=3)
        os.replace(tmp, p)
        n += p.stat().st_size
    return n


def spool_days(s: Settings) -> list[date]:
    out = []
    for p in spool_dir(s).iterdir():
        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and any(p.glob("*.parquet")):
            out.append(date.fromisoformat(p.name))
    return sorted(out, reverse=True)


def read_spool_day(d: date, s: Settings) -> tuple[pl.DataFrame, list[Path]]:
    files = sorted((spool_dir(s) / d.isoformat()).glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=SPOOL_SCHEMA), []
    return pl.concat([pl.read_parquet(f) for f in files], how="vertical_relaxed"), files


def dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


# ================================================================================================ driver
def _logger(s: Settings) -> logging.Logger:
    lg = logging.getLogger("events_v2.ccnews")
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        path = s.logs_dir / "events_v2" / "ccnews.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(sh)
    return lg


def _filler_waiting_for(campaign: str, s: Settings) -> bool:
    """True when the gap filler has `campaign` pending (not running, not done, not backing off) - we yield to it."""
    try:
        f = json.loads((s.state_dir / "ops" / "filler.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    c = (f.get("campaign_status") or {}).get(campaign) or {}
    if c.get("running") or (c.get("progress") or {}).get("done"):
        return False
    nxt = c.get("next_attempt_at")
    if nxt:
        try:
            if datetime.fromisoformat(nxt) > datetime.now(UTC):
                return False
        except ValueError:
            pass
    return f"campaign:{campaign}" in (f.get("waiting") or {})


class Runner:
    def __init__(self, s: Settings, a: argparse.Namespace):
        self.s = s
        self.a = a
        self.log = _logger(s)
        self.stop_file = stop_path(s)
        self.ctx_path = tmp_dir(s) / "label_ctx.pkl"
        self.writer = ThreadPoolExecutor(1, thread_name_prefix="ccnews-final")
        self.final_fut: Future | None = None
        self.final_lock = threading.Lock()
        self.blocked = False
        self._last_yield_check = 0.0
        self.t_start = time.perf_counter()
        self.files_run = 0
        self.bytes_run = 0

    # ---------------------------------------------------------------- control
    def should_stop(self) -> bool:
        if self.stop_file.exists():
            return True
        if self.a.yield_to and time.time() - self._last_yield_check > 60:
            self._last_yield_check = time.time()
            if _filler_waiting_for(self.a.yield_to, self.s):
                self.log.info("campaign %s is waiting for the filler - yielding (exit %d)", self.a.yield_to, EXIT_BLOCKED)
                self.blocked = True
        return self.blocked

    def pause(self) -> None:
        from app.core.jobqueue import job_is_running  # noqa: PLC0415

        said = False
        while job_is_running("build_serving_db", self.s) and not self.stop_file.exists():
            if not said:
                self.log.info("build_serving_db running - pausing")
                said = True
            time.sleep(30)

    # ---------------------------------------------------------------- finalize
    def finalize(self, days: list[date] | None = None, months: list[str] | None = None) -> dict[str, Any]:
        """Merge spooled days into ccnews_articles + the story layer, then story sentiment. Serialised."""
        from app.events_v2 import hist_merge as H  # noqa: PLC0415

        with self.final_lock:
            days = days if days is not None else spool_days(self.s)
            if not days:
                return {"days": 0}
            t0 = time.perf_counter()
            if self.a.inline_finalize:
                rep = H.finalize_ccnews_days(days, self.s, read_spool_day, log=self.log,
                                             sentiment=not self.a.no_sentiment, max_sentiment_per_day=self.a.max_sentiment)
            else:  # child process: the merge's memory goes back to the OS (<= 2 GB budget with the download pool)
                rep = run_finalize_child(days, self.s, not self.a.no_sentiment, self.a.max_sentiment)
            rep["seconds"] = round(time.perf_counter() - t0, 1)

            def upd(st: dict[str, Any]) -> None:
                st.setdefault("finalize_runs", [])
                st["finalize_runs"] = (st["finalize_runs"] + [{k: v for k, v in rep.items() if k != "per_day"}
                                                              | {"at": datetime.now(UTC).isoformat(timespec="seconds")}])[-30:]
                for m in months or []:
                    rec = st.setdefault("months", {}).setdefault(m, {})
                    rec["finalized_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            update_state(upd, self.s)
            self.log.info("finalize: %s", {k: v for k, v in rep.items() if k != "per_day"})
            return rep


    # ---------------------------------------------------------------- months
    def run_month(self, ym: str, pool: ProcessPoolExecutor) -> str:
        """Download + parse the month's remaining WARCs. Returns done | partial | stopped | empty."""
        paths = month_index(ym, self.s)
        if not paths:
            update_state(lambda st: st.setdefault("months", {}).setdefault(ym, {}).update(files=0, status="empty"), self.s)
            return "empty"
        done = ranges_to_set((read_state(self.s).get("months", {}).get(ym) or {}).get("done"))
        step = max(1, int(getattr(self.a, "file_step", 1) or 1))
        want = [i for i in range(len(paths)) if i % step == 0]
        todo = [i for i in want if i not in done]
        if self.a.max_files:
            todo = todo[: max(0, self.a.max_files - len(done))]
        update_state(lambda st: st.setdefault("months", {}).setdefault(ym, {}).update(files=len(paths)), self.s)
        self.log.info("month %s: %d files, %d done, %d to do", ym, len(paths), len(done), len(todo))
        if not todo:
            return "done" if set(want) <= done or self.a.max_files else "partial"
        inflight: dict[Future, int] = {}
        it = iter(todo)
        stopped = False
        stop_at = 0.0
        pending_state: list[dict[str, Any]] = []
        last_flush = time.time()

        def flush_state() -> None:
            recs = list(pending_state)
            pending_state.clear()
            if not recs:
                return

            def upd(st: dict[str, Any]) -> None:
                m = st.setdefault("months", {}).setdefault(ym, {})
                ok = ranges_to_set(m.get("done"))
                agg = Counter(m.get("stats") or {})
                for r in recs:
                    if r["error"] is None:
                        ok.add(r["idx"])
                    else:
                        m.setdefault("errors", {})[str(r["idx"])] = r["error"]
                    for k, v in r["stats"].items():
                        if not k.startswith("lang:") and isinstance(v, (int, float)):
                            agg[k] += v
                    agg["spool_bytes"] += r.get("spool_bytes", 0)
                    agg["wall_s"] += r["seconds"]
                m["done"] = set_to_ranges(ok)
                m["parsed"] = len(ok)
                m["stats"] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in agg.items()}
                langs = Counter(m.get("langs") or {})
                for r in recs:
                    for k, v in r["stats"].items():
                        if k.startswith("lang:"):
                            langs[k[5:]] += v
                m["langs"] = dict(langs.most_common(25))
                m["status"] = "parsed" if set(want) <= ok else "partial"
                m["file_step"] = step
                rate = st.setdefault("rate", {})
                el = time.perf_counter() - self.t_start
                if self.files_run and el > 60:
                    rate.update(files_per_h=round(self.files_run / el * 3600, 1), mb_per_s=round(self.bytes_run / el / 1e6, 1),
                                at=datetime.now(UTC).isoformat(timespec="seconds"))
            update_state(upd, self.s)

        def submit_next() -> bool:
            try:
                i = next(it)
            except StopIteration:
                return False
            task = {"month": ym, "idx": i, "path": paths[i], "ctx_path": str(self.ctx_path)}
            if self.a.local_dir:
                task["file"] = str(Path(self.a.local_dir) / Path(paths[i]).name)
            inflight[pool.submit(P.fetch_and_parse, task)] = i
            return True

        for _ in range(self.a.workers):
            if not submit_next():
                break
        while inflight:
            dn, _ = wait(list(inflight), timeout=10, return_when=FIRST_COMPLETED)
            for f in dn:
                inflight.pop(f)
                try:
                    res = f.result()
                except Exception as e:  # noqa: BLE001 - a worker crash fails one file only
                    self.log.warning("worker failed: %s", e)
                    continue
                if res["error"]:
                    self.log.warning("%s #%d failed: %s", ym, res["idx"], res["error"])
                    res["spool_bytes"] = 0
                else:
                    res["spool_bytes"] = write_spool(ym, res["idx"], res["rows"], self.s)
                    self.files_run += 1
                    self.bytes_run += int(res["stats"].get("bytes_in", 0))
                st = res["stats"]
                self.log.info("%s #%d %s: %.0f MB in %.0fs (%.1f MB/s) recs %d html %d titled %d kept %d linked %d retries %d",
                              ym, res["idx"], Path(res["path"]).name, st.get("bytes_in", 0) / 1e6, res["seconds"],
                              st.get("bytes_in", 0) / 1e6 / max(res["seconds"], 1e-6), st.get("records", 0),
                              st.get("html", 0), st.get("titled", 0), st.get("kept", 0), st.get("linked", 0),
                              res.get("retries", 0))
                res.pop("rows", None)
                pending_state.append(res)
            if time.time() - last_flush > 20:
                flush_state()
                last_flush = time.time()
            if self.should_stop() and not stopped:
                stopped, stop_at = True, time.monotonic()
                self.log.info("stop requested: no new WARC; %d in flight get %.0fs", len(inflight), STOP_DRAIN_S)
            if stopped and inflight and time.monotonic() - stop_at > STOP_DRAIN_S:
                n = _abort_pool(pool)
                self.log.info("stop: abandoned %d in-flight WARC(s) (%d workers terminated; redone next run)",
                              len(inflight), n)
                inflight.clear()
                break
            self.pause()
            while not stopped and len(inflight) < self.a.workers:
                if dir_bytes(tmp_dir(self.s)) > DISK_CAP_BYTES:
                    self.log.warning("data/tmp/ccnews above the %.0f GB cap - finalizing before more downloads",
                                     DISK_CAP_BYTES / 1024**3)
                    flush_state()
                    self.finalize(months=[])
                    if dir_bytes(tmp_dir(self.s)) > DISK_CAP_BYTES:
                        break
                if not submit_next():
                    break
        flush_state()
        if stopped:
            return "stopped"
        m = read_state(self.s).get("months", {}).get(ym) or {}
        return "done" if set(want) <= ranges_to_set(m.get("done")) else "partial"

    def run(self) -> int:
        a = self.a
        if a.finalize_only:
            self.finalize(months=[])
            return 0
        P.LabelCtx.dump(build_label_ctx(self.s), str(self.ctx_path))
        today = datetime.now(UTC).date()
        months = a.months or month_range(a.start_month, a.end_month or today.strftime("%Y-%m"))
        st = read_state(self.s)

        def runmark(stt: dict[str, Any]) -> None:
            stt["run"] = {"pid": os.getpid(), "status": "running", "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                          "months": [months[0], months[-1]], "workers": a.workers, "yield_to": a.yield_to}
            stt.setdefault("window", {"start_month": a.start_month, "first_cc_news_month": FIRST_MONTH})
        update_state(runmark, self.s)
        self.log.info("ccnews run: %d months %s..%s, workers %d", len(months), months[-1], months[0], a.workers)
        status = "done"
        # one month at a time: download pool -> shut down -> finalize child (never both in memory at once)
        for ym in months:
            rec = st.get("months", {}).get(ym) or {}
            if rec.get("status") == "done" and month_closed(ym) and not a.redo:
                continue
            if self.should_stop():
                status = "stopped"
                break
            with ProcessPoolExecutor(a.workers, initializer=P.init_worker, initargs=(str(self.ctx_path),)) as pool:
                res = self.run_month(ym, pool)
            if res in ("done", "partial", "empty"):
                self.finalize(months=[ym])
                if res == "done" and month_closed(ym) and not a.max_files:
                    update_state(lambda stt, ym=ym: stt["months"][ym].update(status="done"), self.s)
            if res == "stopped":
                status = "stopped"
                break
        self.writer.shutdown(wait=True)
        if status != "stopped" and spool_days(self.s):
            self.finalize(months=[])
        status = "blocked" if self.blocked else status

        def fin(stt: dict[str, Any]) -> None:
            stt.setdefault("run", {}).update(status=status, finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                                             files=self.files_run, gb=round(self.bytes_run / 1e9, 2),
                                             hours=round((time.perf_counter() - self.t_start) / 3600, 2))
            stt["last_run_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        update_state(fin, self.s)
        self.log.info("ccnews run finished: %s, %d files, %.1f GB", status, self.files_run, self.bytes_run / 1e9)
        return EXIT_BLOCKED if self.blocked else 0


def probe_progress(pr: dict[str, Any], s: Settings | None = None, today: date | None = None) -> dict[str, Any]:
    """Gap-filler probe (coverage.yaml progress.probe = ccnews_months): files parsed / total WARC files of the window
    (months without a fetched index are estimated at the mean of the known ones), rate from the driver's moving
    files/hour, ETA. `done` once every closed month is done and the last run is < 20 h old (the campaign then runs
    about daily to pick up the new files of the open month)."""
    s = s or get_settings()
    today = today or datetime.now(UTC).date()
    st = read_state(s)
    start = str(pr.get("start_month") or FIRST_MONTH)
    months = month_range(start, today.strftime("%Y-%m"))
    rec = st.get("months") or {}
    step = max(1, int(pr.get("file_step") or 1))
    rec = {m: dict(rec.get(m) or {}) for m in months}
    for m in months:  # months not visited yet: the cached warc.paths index (no network in a probe)
        if rec[m].get("files") is None:
            cache = s.data_dir / "tmp" / "ccnews" / "paths" / f"{m}.txt"
            if cache.exists():
                rec[m]["files"] = sum(1 for x in cache.read_text(encoding="utf-8").splitlines() if x)
    known = [int((rec.get(m) or {}).get("files") or 0) for m in months if (rec.get(m) or {}).get("files")]
    avg = (sum(known) / len(known)) if known else 400.0
    total = sum(-(-int((rec.get(m) or {}).get("files") or 0) // step) if (rec.get(m) or {}).get("files") is not None
                else avg / step for m in months)
    done = sum(int((rec.get(m) or {}).get("parsed") or 0) for m in months)
    rate = (st.get("rate") or {}).get("files_per_h")
    rem = max(0.0, total - done)
    closed_done = all((rec.get(m) or {}).get("status") in ("done", "empty") for m in months if month_closed(m, today))
    last = st.get("last_run_at")
    fresh = False
    if last:
        try:
            fresh = datetime.now(UTC) - datetime.fromisoformat(last) < timedelta(hours=20)
        except ValueError:
            fresh = False
    run = st.get("run") or {}
    gb = sum(float((rec.get(m) or {}).get("stats", {}).get("bytes_in", 0)) for m in months) / 1e9
    out = {"done": bool(closed_done and fresh), "pct": round(100 * done / total, 1) if total else None,
           "done_units": done, "total_units": int(round(total)), "unit": "WARC files",
           "rate_per_h": rate, "eta_h": round(rem / rate, 1) if rate and rem > 0 else (0 if rem <= 0 else None),
           "last_run_at": last,
           "detail": f"run {run.get('status', '-')} pid {run.get('pid', '-')}; {done}/{int(round(total))} files "
                     f"({gb:.0f} GB streamed); newest month first from {months[0]}"}
    from app.core.robots_policy import disabled_note  # noqa: PLC0415

    note = disabled_note("ccnews")
    if note:  # A9 2026-11-07: robots-disallowed -> nothing left the filler could do (stops starting the campaign)
        out.update(done=True, eta_h=None, reason="source_disallowed", detail=f"{note[:240]} | {out['detail']}")
    return out


def _finalize_child(days_iso: list[str], sentiment: bool, max_sentiment: int, out_path: str) -> None:
    from app.events_v2 import hist_merge as H  # noqa: PLC0415

    s = get_settings()
    rep = H.finalize_ccnews_days([date.fromisoformat(x) for x in days_iso], s, read_spool_day, log=_logger(s),
                                 sentiment=sentiment, max_sentiment_per_day=max_sentiment)
    Path(out_path).write_text(json.dumps(rep, default=str), encoding="utf-8")


def run_finalize_child(days: list[date], s: Settings, sentiment: bool, max_sentiment: int) -> dict[str, Any]:
    """finalize_ccnews_days in a spawned child process (sentiment spawns one more child per model)."""
    import multiprocessing as mp  # noqa: PLC0415

    work = tmp_dir(s) / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"finalize-{os.getpid()}-{int(time.time())}.json"
    pr = mp.get_context("spawn").Process(target=_finalize_child,
                                         args=([d.isoformat() for d in days], sentiment, max_sentiment, str(out)))
    pr.start()
    pr.join(CHILD_TIMEOUT_S)
    if pr.is_alive():
        pr.kill()
        pr.join(30)
        raise RuntimeError(f"finalize child timed out after {CHILD_TIMEOUT_S / 3600:.0f} h (killed)")
    if pr.exitcode != 0 or not out.exists():
        raise RuntimeError(f"finalize child failed (exit {pr.exitcode})")
    rep = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    return rep


def status_summary(s: Settings | None = None) -> dict[str, Any]:
    st = read_state(s)
    months = st.get("months") or {}
    tot = sum(int(m.get("files") or 0) for m in months.values())
    done = sum(int(m.get("parsed") or 0) for m in months.values())
    return {"months": len(months), "files": tot, "parsed": done, "run": st.get("run"), "rate": st.get("rate"),
            "days": len(st.get("days") or {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-month", default=FIRST_MONTH, help="oldest month (YYYY-MM)")
    ap.add_argument("--end-month", default=None, help="newest month (default: current)")
    ap.add_argument("--months", nargs="*", default=None, help="explicit months (pilot)")
    ap.add_argument("--workers", type=int, default=4, help="download+parse processes (= connections)")
    ap.add_argument("--max-files", type=int, default=0, help="per-month file cap (testing)")
    ap.add_argument("--file-step", type=int, default=1,
                    help="process every K-th WARC of a month (uniform time sample; 1 = everything)")
    ap.add_argument("--max-sentiment", type=int, default=1500, help="stories scored per day (most salient first)")
    ap.add_argument("--inline-finalize", action="store_true", help="finalize in this process (tests / debugging)")
    ap.add_argument("--no-sentiment", action="store_true")
    ap.add_argument("--finalize-only", action="store_true")
    ap.add_argument("--redo", action="store_true", help="re-visit months marked done")
    ap.add_argument("--yield-to", default=None, help="gap-filler campaign id to yield to (exit 3 while it waits)")
    ap.add_argument("--local-dir", default=None, help="read WARCs from this folder instead of HTTP (tests)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--relink", nargs=2, metavar=("START", "END"), default=None,
                    help="re-run the issuer linker (current rules) over stored ccnews_articles of [START, END]")
    a = ap.parse_args(argv)
    s = get_settings()
    try:  # BelowNormal CPU priority (the gap filler sets it too; workers inherit it)
        from app.ops import winsys  # noqa: PLC0415

        winsys.set_priority(os.getpid(), "below_normal")
    except Exception:  # noqa: BLE001
        pass
    if a.status:
        print(json.dumps(status_summary(s), indent=1, default=str))
        return 0
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(s.locks_dir / "ccnews_backfill.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("another ccnews backfill is running", file=sys.stderr)
        return 2
    try:
        if a.relink:
            from app.events_v2 import hist_merge as H  # noqa: PLC0415

            print(json.dumps(H.relink_ccnews(date.fromisoformat(a.relink[0]), date.fromisoformat(a.relink[1]), s)))
            return 0
        if a.yield_to and _filler_waiting_for(a.yield_to, s):
            print(f"campaign {a.yield_to} is waiting for the filler - not starting", file=sys.stderr)
            return EXIT_BLOCKED
        from app.core.robots_policy import disabled_note  # noqa: PLC0415

        note = disabled_note("ccnews")
        if note and not a.finalize_only and not a.local_dir:  # A9: no download from a robots-disallowed host
            print(f"ccnews: {note}", file=sys.stderr)
            return 0
        return Runner(s, a).run()
    finally:
        lock.release()
        shutil.rmtree(tmp_dir(s) / "work", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
