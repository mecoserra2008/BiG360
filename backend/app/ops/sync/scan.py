"""Freshness scan of the startup sync (docs/STARTUP_SYNC_PLAN.md A1): what is missing from the latest data?

For every contracts/coverage.yaml dataset (plus one job-level dataset `job:<id>` per root fetch job that no dataset
covers) the scan
  1. measures what is stored, with the gap detector's checks (app.ops.gaps.CHECKS: watermarks / max dates per
     dataset, region, currency, source),
  2. computes the expected latest period from real calendars (app.ops.sync.expect: exchange closes, ECB fixing, the
     macro release calendar, CFTC Friday release, 13F deadline, FINRA publication, GDELT day close),
  3. assigns a reason code per entity and per dataset:
        fresh | due | not_yet_published | source_unavailable | no_source | needs_key | failed
     (the attempt ledger turns "still missing after a fetch" into not_yet_published / source_unavailable / failed
     with a retry time, and parks an item after repeated failures),
  4. returns a delta plan: fetch jobs with incremental params (dates, symbols, sources, since) for the `due` items.

    res = scan(settings)              # ScanResult: datasets (reason per dataset / entity), plan {job: PlanItem}
    python -m app.ops.sync scan       # print it (read-only)

The scan never raises for one dataset: a broken check gives that dataset `failed` with the error as detail.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.sync import expect as X
from app.ops.sync import policy as P
from app.ops.sync.state import REASON_RANK, Ledger, parse_ts

log = get_logger("app.ops.sync.scan")


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _d(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@dataclass
class EntitySync:
    entity: str
    reason: str
    data_as_of: str | None = None
    expected_as_of: str | None = None
    next_expected: str | None = None
    detail: str | None = None
    missing: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    retry_at: str | None = None
    attempts: int | None = None
    secondary: bool = False          # an alternative source: never decides the dataset reason


@dataclass
class DatasetSync:
    dataset: str
    label: str
    group: str
    kind: str = "coverage"           # coverage | job
    reason: str = "fresh"
    data_as_of: str | None = None
    expected_as_of: str | None = None
    next_expected: str | None = None
    detail: str | None = None
    entities: list[EntitySync] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    retry_at: str | None = None
    attempts: int | None = None
    check_s: float | None = None

    def finish(self) -> DatasetSync:
        """Dataset reason = the most actionable primary entity reason (failed > due > source_unavailable >
        needs_key > not_yet_published > no_source > fresh)."""
        prim = [e for e in self.entities if not e.secondary] or self.entities
        if prim:
            worst = min(prim, key=lambda e: REASON_RANK.get(e.reason, 9))
            if REASON_RANK.get(worst.reason, 9) < REASON_RANK.get(self.reason, 9):
                self.reason = worst.reason
            if self.reason == worst.reason:
                self.retry_at = self.retry_at or worst.retry_at
                self.attempts = self.attempts or worst.attempts
            self.keys = sorted({k for e in self.entities for k in e.keys} | set(self.keys))
            nxt = [e.next_expected for e in self.entities if e.next_expected]
            if not self.next_expected and nxt:
                self.next_expected = min(nxt)
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanItem:
    job: str
    params: dict[str, Any] = field(default_factory=dict)
    datasets: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    tier: str = "core"
    trigger: str = "dataset"         # dataset | cadence | fingerprint
    periods: dict[str, list[str]] = field(default_factory=dict)   # dataset|entity -> periods this run should bring

    def merge(self, params: dict[str, Any]) -> None:
        for k, v in params.items():
            old = self.params.get(k)
            if isinstance(v, list) and isinstance(old, list):
                self.params[k] = sorted(set(old) | set(v))
            elif k == "period" and old:
                order = ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"]
                self.params[k] = max(old, v, key=lambda p: order.index(p) if p in order else 0)
            elif k in ("start", "since") and old:
                self.params[k] = min(str(old), str(v))
            elif k == "lookback_days" and old:
                self.params[k] = max(int(old), int(v))
            else:
                self.params[k] = v


@dataclass
class ScanResult:
    generated_at: str
    as_of: str | None                 # sync target date (latest published US session)
    datasets: list[DatasetSync]
    plan: dict[str, PlanItem]
    price_as_of: dict[str, str | None]
    seconds: float
    errors: dict[str, str] = field(default_factory=dict)
    metered: bool = False

    def by_id(self) -> dict[str, DatasetSync]:
        return {d.dataset: d for d in self.datasets}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.datasets:
            out[d.reason] = out.get(d.reason, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"generated_at": self.generated_at, "as_of": self.as_of, "seconds": self.seconds,
                "counts": self.counts(), "price_as_of": self.price_as_of, "errors": self.errors,
                "metered": self.metered, "datasets": [d.to_dict() for d in self.datasets],
                "plan": {k: asdict(v) for k, v in self.plan.items()}}


# ------------------------------------------------------------------------------------------------ scanner


class Scanner:
    def __init__(self, settings: Settings | None = None, now: datetime | None = None, ledger: Ledger | None = None,
                 registry: Any = None, job_runs: pl.DataFrame | None = None, datasets: list[str] | None = None,
                 metered: bool | None = None, key_present: Any = None):
        from app.ops.coverage import load_coverage
        from app.scheduler.runner import JobRegistry

        self.s = settings or get_settings()
        self.now = now or datetime.now(UTC)
        self.today = self.now.date()
        self.ledger = ledger if ledger is not None else Ledger(self.s)
        self.reg = registry or JobRegistry.load(settings=self.s)
        self.cov = load_coverage(self.s)
        self.only = set(datasets) if datasets else None
        self._jr = job_runs
        self.plan: dict[str, PlanItem] = {}
        self.errors: dict[str, str] = {}
        self.price_as_of: dict[str, str | None] = {}
        self._metered = metered
        self._key_present = key_present
        self._yf_cache: dict[str, list[str]] = {}
        self._yf_universe: set[str] | None = None
        self.macro_full: set[str] = set()        # macro group jobs due as a whole (no `sources` subset)

    # ---------------------------------------------------------------- helpers
    @property
    def job_runs(self) -> pl.DataFrame:
        if self._jr is None:
            from app.scheduler.runner import JobRunStore

            try:
                self._jr = JobRunStore(self.s).read()
            except Exception:  # pragma: no cover
                self._jr = pl.DataFrame()
        return self._jr

    def last_ok_start(self, job: str) -> datetime | None:
        jr = self.job_runs
        if jr.is_empty() or "job_id" not in jr.columns:
            return None
        x = jr.filter((pl.col("job_id") == job) & pl.col("status").is_in(["success", "partial"]))
        if x.is_empty():
            return None
        return parse_ts(x.get_column("started_at").max())

    def last_run(self, job: str) -> dict[str, Any] | None:
        jr = self.job_runs
        if jr.is_empty() or "job_id" not in jr.columns:
            return None
        x = jr.filter((pl.col("job_id") == job) & (pl.col("status") != "running")).sort("started_at")
        return x.row(x.height - 1, named=True) if x.height else None

    def missing_keys(self, names: tuple[str, ...] | list[str]) -> list[str]:
        out = []
        for n in names:
            have = self._key_present(n) if self._key_present is not None else (self.s.secret(n) is not None)
            if not have:
                out.append(n.upper())
        return out

    @property
    def metered(self) -> bool:
        if self._metered is None:
            try:
                from app.ops import winsys

                self._metered = bool(winsys.network_cost().get("metered"))
            except Exception:  # pragma: no cover
                self._metered = False
        return self._metered

    def add_plan(self, job: str, params: dict[str, Any], dataset: str, why: str, trigger: str = "dataset",
                 periods: dict[str, list[str]] | None = None) -> None:
        if job not in self.reg.jobs:
            return
        it = self.plan.get(job)
        if it is None:
            it = self.plan[job] = PlanItem(job, {}, [], [], "core", trigger)
        it.merge(params)
        if dataset not in it.datasets:
            it.datasets.append(dataset)
        it.why.append(why)
        for k, v in (periods or {}).items():
            it.periods[k] = sorted(set(it.periods.get(k, [])) | set(v))

    def hold(self, ds: str, entity: str, period: Any) -> EntitySync | None:
        """A ledger entry still holding (dataset, entity, period) back -> the entity with that reason."""
        rec = self.ledger.active(ds, entity, period, self.now)
        if not rec:
            return None
        why = {"not_yet_published": "source has not published it yet",
               "source_unavailable": "source down / rate-limited",
               "failed": "parked after repeated failures"}.get(rec.get("reason"), rec.get("reason"))
        return EntitySync(entity, rec.get("reason") or "failed", expected_as_of=_iso(period),
                          detail=f"{why} (attempt {rec.get('attempts')}{': ' + rec['error'][:160] if rec.get('error') else ''}); "
                                 f"retry {rec.get('retry_at')}",
                          retry_at=rec.get("retry_at"), attempts=rec.get("attempts"),
                          keys=[k for k in [rec.get("key")] if k])

    # ---------------------------------------------------------------- main
    def run(self) -> ScanResult:
        import duckdb  # noqa: F401,PLC0415 - fail early if missing

        from app.core.storage import duckdb_connect
        from app.ops import gaps as G

        t0 = time.perf_counter()
        con = duckdb_connect(None, settings=self.s)
        con.execute("SET memory_limit = '1GB'")
        con.execute("SET threads = 4")
        con.execute("SET enable_progress_bar = false")
        con.execute("SET TimeZone = 'UTC'")
        self.cx = G.Ctx(self.s, con, self.today, self.now, None, self.cov)
        out: list[DatasetSync] = []
        try:
            # prices first: the analytics datasets compare against the price as-of
            ids = sorted(self.cov["datasets"], key=lambda k: (0 if k.startswith("prices.") else 1, k))
            for did in ids:
                if self.only and did not in self.only:
                    continue
                spec = self.cov["datasets"][did]
                ds = DatasetSync(did, spec.get("label", did), spec.get("group", "other"))
                t1 = time.perf_counter()
                try:
                    self.dataset(spec, ds)
                except Exception as e:  # one broken check never stops the scan
                    self.errors[did] = f"{type(e).__name__}: {e}"[:300]
                    log.warning("sync scan %s failed: %s\n%s", did, e, traceback.format_exc())
                    ds.reason, ds.detail = "failed", f"freshness check failed: {self.errors[did]}"
                ds.check_s = round(time.perf_counter() - t1, 2)
                out.append(ds.finish())
            if not self.only or any(x.startswith("job:") for x in self.only):
                out += self.job_datasets()
        finally:
            con.close()
        for j in self.macro_full:
            if j in self.plan:
                self.plan[j].params.pop("sources", None)
        if self.metered:
            for it in list(self.plan.values()):
                for d in out:
                    if d.dataset in it.datasets and d.reason == "due":
                        d.reason = "source_unavailable"
                        d.detail = "metered network (phone hotspot?): downloads deferred until an unmetered connection"
                        d.jobs = []
            self.plan = {}
        for d in out:
            d.jobs = sorted(j for j, it in self.plan.items() if d.dataset in it.datasets)
        as_of = X.published_session("XNYS", self.now, P.PRICE_PUBLISH_DELAY_MIN["default"])
        return ScanResult(self.now.isoformat(timespec="seconds"), _iso(as_of), out, self.plan, self.price_as_of,
                          round(time.perf_counter() - t0, 2), self.errors, self.metered)

    def dataset(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        if did in P.OWNED_ELSEWHERE:
            return self.owned(spec, ds)
        chk = spec.get("check")
        fn = {"sessions": self.sessions, "currency_dates": self.fx, "macro_releases": self.macro_releases,
              "latest_ingest": self.latest_ingest, "gdelt_days": self.gdelt, "daily_counts": self.daily_counts,
              "latest_date": self.latest_date, "cftc_weekly": self.cftc, "quarterly_13f": self.q13f,
              "finra_settlements": self.finra, "sentinel_rows": self.cadence_dataset,
              "conformal_coverage": self.latest_date, "published_dates": self.published_dates}.get(chk)
        if fn is None:
            ds.detail = f"no sync rule for check {chk!r}"
            return None
        return fn(spec, ds)

    def observe(self, spec: dict[str, Any]) -> Any:
        """Run the gap detector's check for `spec` (read-only) -> its DatasetStatus."""
        from app.ops import gaps as G

        st = G.DatasetStatus(spec["id"], spec.get("label", spec["id"]), spec.get("group", "other"))
        fn = G.CHECKS.get(spec.get("check"))
        if fn is not None:
            cx = self.cx
            if spec.get("check") == "sessions":
                # the detector counts bars before today (nightly semantics); the sync runs minutes after a close, so
                # today's session counts too (bars of sessions still open are never staged: drop_unfinished_bars)
                from dataclasses import replace

                cx = replace(self.cx, today=self.today + timedelta(days=1), _views=self.cx._views)
            fn(cx, spec, st)
        return st

    # ---------------------------------------------------------------- fetch-plan helpers
    def fetch_jobs(self, did: str) -> tuple[list[str], list[EntitySync]]:
        """(runnable fetch jobs of a dataset, needs_key entities for the jobs whose key is missing)."""
        jobs, held = [], []
        for j in P.DATASET_FETCH.get(did, ()):
            miss = self.missing_keys(P.JOB_KEYS.get(j, ()))
            if miss:
                held.append(EntitySync(f"{j}", "needs_key", keys=miss, secondary=True,
                                       detail=f"{j} skipped: set {', '.join(miss)} (Credential Manager or .env)"))
            else:
                jobs.append(j)
        return jobs, held

    # ---------------------------------------------------------------- checks
    def owned(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        try:
            st = self.observe(spec)
        except Exception as e:  # noqa: BLE001
            st = None
            ds.detail = f"{P.OWNED_ELSEWHERE[spec['id']]} (check failed: {e})"
        ds.data_as_of = _iso(getattr(st, "data_as_of", None))
        ds.expected_as_of = _iso(getattr(st, "expected_as_of", None))
        status = getattr(st, "status", "unknown")
        note = P.OWNED_ELSEWHERE[spec["id"]]
        if spec["id"] == "news.live" and status not in ("fresh", "unknown"):
            ents = getattr(st, "entities", {}) or {}
            svc = ents.get("service") or {}
            hourly = ents.get("hourly") or {}
            fold_age = hourly.get("age_h")
            ds.next_expected = _iso(self.now + timedelta(hours=1))
            if fold_age is not None and fold_age > 3 and self.ledger.active(spec["id"], "hourly", _iso(self.today)) is None:
                # the hourly DAG did not fold for > 3 h (worker scheduler stalled / PC asleep): the sync folds now
                ds.entities.append(EntitySync("hourly", "due", detail=f"hourly fold {fold_age:.1f} h old: news_hourly "
                                                                      "runs in the sync"))
                self.add_plan("news_hourly", {}, spec["id"], f"news.live: hourly fold {fold_age:.1f} h old",
                              periods={f"{spec['id']}|hourly": [_iso(self.today) or ""]})
            if svc.get("enabled") and not svc.get("running"):
                ds.entities.append(EntitySync("service", "source_unavailable", next_expected=ds.next_expected,
                                              detail="live news service not running (supervisor child realtime)"))
            if not ds.entities:
                ds.reason = "source_unavailable"
            ds.detail = f"{getattr(st, 'detail', '')}; {note}"
        else:
            ds.reason = "fresh"
            ds.detail = f"{note}; {getattr(st, 'detail', '') or ''}".strip("; ")

    def sessions(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did, region = spec["id"], spec["region"]
        mic = spec.get("calendar") or P.REGION_CALENDAR.get(region, "XNYS")
        st = self.observe(spec)
        have = _d(st.data_as_of)
        self.price_as_of[region] = _iso(have)
        jobs, held = self.fetch_jobs(did)
        delay = max([P.PRICE_PUBLISH_DELAY_MIN.get(j, P.PRICE_PUBLISH_DELAY_MIN["default"]) for j in jobs[:1]]
                    or [P.PRICE_PUBLISH_DELAY_MIN["default"]])
        exp = X.published_session(mic, self.now, delay)
        nxt = X.next_publication(mic, self.now, delay)
        ds.data_as_of, ds.expected_as_of, ds.next_expected = _iso(have), _iso(exp), _iso(nxt)
        ds.entities += held + [EntitySync(k, "no_source", detail=v, secondary=True)
                               for k, v in (P.NO_SOURCE.get(did) or {}).items() if k not in {e.entity for e in held}]
        mstate = X.market_state(mic, self.now)
        miss = X.sessions_between(mic, have, exp) if have else ([exp] if exp else [])
        if not miss:
            ds.entities.append(EntitySync(region, "fresh", _iso(have), _iso(exp), _iso(nxt),
                                          f"through {have}" + (f"; {mstate}" if mstate else "")))
            ds.detail = f"latest session {have} on {mic}" + (f"; {mstate}" if mstate else "") + \
                f"; next bar expected {nxt:%Y-%m-%d %H:%M} UTC" if nxt else ""
            return
        h = self.hold(did, region, exp)
        if h is not None:
            h.data_as_of, h.next_expected, h.missing = _iso(have), _iso(nxt), [_iso(x) for x in miss]
            ds.entities.append(h)
            ds.detail = f"{len(miss)} session(s) missing ({miss[0]}..{miss[-1]}); {h.detail}"
            return
        if not jobs:
            ds.entities.append(EntitySync(region, "needs_key" if held else "no_source", _iso(have), _iso(exp),
                                          missing=[_iso(x) for x in miss], keys=[k for e in held for k in e.keys],
                                          detail="no runnable price source"))
            return
        ent = EntitySync(region, "due", _iso(have), _iso(exp), _iso(nxt), missing=[_iso(x) for x in miss],
                         detail=f"{len(miss)} session(s) to fetch: {miss[0]}..{miss[-1]}")
        ds.entities.append(ent)
        ds.detail = ent.detail
        periods = {f"{did}|{region}": [_iso(exp)]}
        why = f"{did}: {len(miss)} session(s) {miss[0]}..{miss[-1]}"
        for j in jobs:
            params: dict[str, Any] = {}
            if j == "prices_us_polygon":
                params = {"dates": [x.isoformat() for x in miss[-30:]]}
            elif j == "prices_global_yf":
                params = {"symbols": self.yf_symbols(region), "period": self.yf_period(miss[0]), "batch_size": 250}
                if not params["symbols"]:
                    continue
            elif j in ("prices_cn_baostock", "prices_tw"):
                params = {"start": (miss[0] - timedelta(days=3)).isoformat()}
            elif j == "prices_br_b3":
                params = {"lookback_days": max(5, (self.today - miss[0]).days + 2)}
            elif j == "prices_kr_krx":
                params = {"days": [x.isoformat() for x in miss[-30:]]}
            self.add_plan(j, params, did, why, periods=periods)
        self.add_plan("prices_finalize", {"regions": [region]}, did, why, trigger="upstream", periods=periods)

    def yf_period(self, oldest: date) -> str:
        age = (self.today - oldest).days
        if age <= 6:
            return "5d"
        for lim, p in ((25, "1mo"), (85, "3mo"), (170, "6mo"), (350, "1y"), (700, "2y"), (1800, "5y")):
            if age <= lim:
                return p
        return "max"

    def yf_symbols(self, region: str) -> list[str]:
        """Yahoo tickers to refresh for a region: its securities with recent bars that the nightly Yahoo job would
        fetch (official-bulk markets: curated seeds only), plus the region's benchmark indices / ETFs. US equities come
        from Polygon: only the US benchmarks are fetched from Yahoo."""
        if region in self._yf_cache:
            return self._yf_cache[region]
        from app.normalize.prices import BENCHMARKS

        bms = [b.yf_ticker for b in BENCHMARKS if b.region == region and b.kind in ("index", "etf", "index_ext")]
        syms: list[str] = []
        if region != "US":
            try:
                from app.ingestion.yf_prices import YFinancePricesAdapter
                from app.ops.taskrun import region_yf_symbols

                if self._yf_universe is None:
                    self._yf_universe = set(YFinancePricesAdapter(self.s).universe_symbols())
                syms = [x for x in region_yf_symbols(region, settings=self.s) if x in self._yf_universe]
            except Exception as e:  # noqa: BLE001
                log.warning("sync: Yahoo symbols for %s unavailable (%s)", region, e)
        out = list(dict.fromkeys(bms + syms))
        self._yf_cache[region] = out
        return out

    def fx(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        exp = X.ecb_expected(self.now)
        nxt = X.next_ecb(self.now)
        no_src = P.NO_SOURCE.get(did) or {}
        lasts = {c: _d(v.get("last")) for c, v in st.entities.items()}
        have = max([d for c, d in lasts.items() if d and c not in no_src], default=None)
        ds.data_as_of, ds.expected_as_of, ds.next_expected = _iso(have), _iso(exp), _iso(nxt)
        for c, d in sorted(lasts.items()):
            if c in no_src:
                ds.entities.append(EntitySync(c, "no_source", _iso(d), detail=no_src[c], secondary=True))
            elif d and have and (have - d).days > 7:
                ds.entities.append(EntitySync(c, "not_yet_published", _iso(d), _iso(exp), secondary=True,
                                              detail=f"{c} published with a lag by its source (last {d})"))
        if have and have >= exp:
            ds.entities.append(EntitySync("fixings", "fresh", _iso(have), _iso(exp), _iso(nxt), f"through {have}"))
            ds.detail = f"{len(lasts)} currencies through {have}; next ECB fixing {nxt:%Y-%m-%d %H:%M} UTC"
            return
        h = self.hold(did, "fixings", exp)
        if h is not None:
            h.data_as_of = _iso(have)
            ds.entities.append(h)
            return
        jobs, held = self.fetch_jobs(did)
        ds.entities += held
        ds.entities.append(EntitySync("fixings", "due", _iso(have), _iso(exp), _iso(nxt),
                                      detail=f"fixings {have}..{exp} to fetch"))
        for j in jobs:
            self.add_plan(j, {}, did, f"{did}: fixings after {have} (expected {exp})",
                          periods={f"{did}|fixings": [_iso(exp)]})

    def _macro_job_of(self) -> dict[str, str]:
        if not hasattr(self, "_mj"):
            mj: dict[str, str] = {"fred": "macro_fred"}
            for job in ("macro_eu", "macro_intl", "macro_national"):
                try:
                    import importlib

                    mod = importlib.import_module(self.reg.jobs[job].module)
                    for src in getattr(mod, "SOURCES", []) or []:
                        mj.setdefault(src, job)
                except Exception:  # noqa: BLE001
                    for src in self.reg.jobs[job].sources:
                        mj.setdefault(src, job)
            self._mj = mj
        return self._mj

    def _macro_plan(self, did: str, by_source: dict[str, list[str]], why: str, periods: dict[str, list[str]]) -> list[EntitySync]:
        """Plan macro group jobs for {source_id: [series ids]}; keyed sources without a key -> needs_key."""
        mj = self._macro_job_of()
        held: list[EntitySync] = []
        groups: dict[str, set[str]] = {}
        for src, series in by_source.items():
            miss = self.missing_keys(P.SOURCE_KEYS.get(src, ()))
            if miss:
                held.append(EntitySync(src, "needs_key", keys=miss,
                                       detail=f"{src} needs {', '.join(miss)} ({len(series)} series)"))
                continue
            job = mj.get(src)
            if job is None:
                held.append(EntitySync(src, "no_source", detail=f"no fetch job for macro source {src}"))
                continue
            groups.setdefault(job, set()).add(src)
        for job, srcs in groups.items():
            params = {} if job == "macro_fred" else {"sources": sorted(srcs)}
            self.add_plan(job, params, did, f"{why}: {', '.join(sorted(srcs))}", periods=periods)
        return held

    def macro_releases(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        from app.core.storage import read_table

        ev = read_table("macro_release_events", columns=["release_event_id", "series_id", "country", "scheduled_at",
                                                          "status", "release_date"], settings=self.s)
        if ev.is_empty():
            ds.reason, ds.detail = "due", "no release calendar yet"
            self.add_plan("macro_calendar", {}, did, f"{did}: no release calendar", trigger="dataset")
            return
        grace = timedelta(hours=2)
        now = self.now
        win = ev.filter(pl.col("scheduled_at") >= now - timedelta(days=30))
        past_pending = win.filter((pl.col("status") == "pending") & (pl.col("scheduled_at") <= now - grace))
        lagging = win.filter((pl.col("status") == "pending") & (pl.col("scheduled_at") > now - grace)
                             & (pl.col("scheduled_at") <= now))
        future = ev.filter((pl.col("status") == "pending") & (pl.col("scheduled_at") > now))
        released = ev.filter(pl.col("status") == "released")
        last_rel = released.get_column("release_date").max() if released.height else None
        nxt = future.get_column("scheduled_at").min() if future.height else None
        ds.data_as_of, ds.next_expected = _iso(last_rel), _iso(nxt)
        ds.expected_as_of = _iso(now.date())
        src_of = self._series_sources(list(set(past_pending.get_column("series_id").to_list())))
        held: list[EntitySync] = []
        for country, grp in sorted(past_pending.group_by("country"), key=lambda kv: str(kv[0])):
            c = str(country[0] if isinstance(country, tuple) else country)
            last_sched = grp.get_column("scheduled_at").max()
            h = self.hold(did, c, _iso(last_sched))
            if h is not None:
                h.missing = sorted({_iso(x) or "" for x in grp.get_column("release_date").to_list()})
                ds.entities.append(h)
                continue
            by_src: dict[str, list[str]] = {}
            for sid in grp.get_column("series_id").to_list():
                by_src.setdefault(src_of.get(sid, sid.split(":", 1)[0]), []).append(sid)
            held += self._macro_plan(did, by_src, f"{did}: {grp.height} release(s) of {c} not ingested",
                                     {f"{did}|{c}": [_iso(last_sched) or ""]})
            ds.entities.append(EntitySync(c, "due", expected_as_of=_iso(last_sched),
                                          missing=sorted({_iso(x) or "" for x in grp.get_column("release_date").to_list()}),
                                          detail=f"{grp.height} release(s) past their time, not ingested: "
                                                 + ", ".join(sorted(set(grp.get_column("series_id").to_list()))[:6])))
        for country, grp in lagging.group_by("country"):
            c = str(country[0] if isinstance(country, tuple) else country)
            t = grp.get_column("scheduled_at").max()
            ds.entities.append(EntitySync(c, "not_yet_published", expected_as_of=_iso(t), next_expected=_iso(t + grace),
                                          detail=f"{grp.height} release(s) at {t:%H:%M} UTC: publication lag, picked up "
                                                 "by the next sync"))
        ds.entities += held
        if nxt is not None:
            nf = future.filter(pl.col("scheduled_at") == nxt)
            ds.entities.append(EntitySync("next release", "fresh", next_expected=_iso(nxt), secondary=True,
                                          detail=f"{nf.height} release(s) next at {nxt:%Y-%m-%d %H:%M} UTC ("
                                                 + ", ".join(sorted(set(nf.get_column('country').to_list()))) + ")"))
        if not ds.entities or all(e.reason == "fresh" for e in ds.entities):
            ds.detail = f"every release of the last 30 days ingested; last {last_rel}"
        else:
            ds.detail = f"{past_pending.height} release(s) pending past their time, {lagging.height} within the publication lag"

    def _series_sources(self, series: list[str]) -> dict[str, str]:
        if not series:
            return {}
        try:
            from app.core.storage import read_table

            ms = read_table("macro_series", columns=["series_id", "source_id"], settings=self.s)
            ms = ms.filter(pl.col("series_id").is_in(series))
            return dict(zip(ms.get_column("series_id").to_list(), ms.get_column("source_id").to_list(), strict=False))
        except Exception:  # noqa: BLE001
            return {}

    def latest_ingest(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        ds.data_as_of = _iso(st.data_as_of)
        ds.expected_as_of = _iso(self.today)
        if did != "macro.ingest":
            for j in self.fetch_jobs(did)[0]:
                e = self._cadence_entity(did, j, root=True)
                ds.entities.append(e)
                if e.reason == "due":
                    self.add_plan(j, {}, did, f"{did}: last ingest {ds.data_as_of}", trigger="cadence",
                                  periods={f"{did}|{j}": [_iso(self.today) or ""]})
            ds.detail = st.detail
            return None
        # macro sources: the group job's daily cadence decides; a source whose job ran but whose data is old has
        # nothing newer at its publisher (informational, secondary)
        ent = spec.get("entities") or {}
        weekly, ignore = set(ent.get("weekly") or []), set(ent.get("ignore") or [])
        mj = self._macro_job_of()
        groups = {j: self._cadence_entity(did, j, root=True) for j in P.MACRO_GROUP_JOBS}
        due_groups = sorted(j for j, e in groups.items() if e.reason == "due")
        for key, v in sorted(st.entities.items()):
            if key in ignore:
                continue
            last = _d(v.get("last_ingest"))
            job = mj.get(key)
            miss = self.missing_keys(P.SOURCE_KEYS.get(key, ()))
            if miss:
                ds.entities.append(EntitySync(key, "needs_key", _iso(last), keys=miss, secondary=True,
                                              detail=f"{key} needs {', '.join(miss)}"))
                continue
            lim = 35 if key in weekly else 7
            if job in due_groups:
                ds.entities.append(EntitySync(key, "due", _iso(last), detail=f"{job}: daily pull (last ingest {last})"))
            elif last and (self.today - last).days > lim:
                ds.entities.append(EntitySync(key, "not_yet_published", _iso(last), secondary=True,
                                              next_expected=groups.get(job).next_expected if job in groups else None,
                                              detail=f"pulled by {job}; no new observations since {last}"))
            else:
                ds.entities.append(EntitySync(key, "fresh", _iso(last), detail=f"ingested {last}", secondary=True))
        for j, e in groups.items():
            e.entity = j
            ds.entities.append(e)
            if e.reason == "due":
                self.add_plan(j, {}, did, f"{did}: {j} daily pull ({e.detail})", trigger="cadence",
                              periods={f"{did}|{j}": [_iso(self.today) or ""]})
                self.macro_full.add(j)
        for src in sorted(P.SOURCE_KEYS):
            if src in st.entities or src in ignore:
                continue
            miss = self.missing_keys(P.SOURCE_KEYS[src])
            if miss and mj.get(src):
                ds.entities.append(EntitySync(src, "needs_key", keys=miss, secondary=True,
                                              detail=f"{src} ({mj[src]}) needs {', '.join(miss)}"))
        ds.detail = (f"{len(due_groups)} group job(s) due: {', '.join(due_groups)}" if due_groups
                     else f"{len(st.entities)} sources; every group pulled within 20 h")
        return None

    def gdelt(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        from app.news_live.hot import hot_days, news_hot_dir

        st = self.observe(spec)
        exp = X.gdelt_expected(self.now)
        nxt = X.next_gdelt(self.now)
        state_days = self._gdelt_days()
        done = sorted(d for d in state_days if d <= exp.isoformat())
        have = date.fromisoformat(done[-1]) if done else _d(st.data_as_of)
        ds.data_as_of, ds.expected_as_of, ds.next_expected = _iso(have), _iso(exp), _iso(nxt)
        # the last 10 UTC days must be assembled; older holes and partial days are the gap filler's (gdelt_redo)
        miss = sorted(d for d in (exp - timedelta(days=k) for k in range(P.GDELT_WINDOW_DAYS))
                      if d.isoformat() not in state_days)
        later = miss[:-P.GDELT_MAX_DAYS_PER_RUN] if len(miss) > P.GDELT_MAX_DAYS_PER_RUN else []
        miss = miss[-P.GDELT_MAX_DAYS_PER_RUN:]
        if not miss:
            ds.entities.append(EntitySync("days", "fresh", _iso(have), _iso(exp), _iso(nxt),
                                          f"every UTC day through {have}"))
            ds.detail = f"complete through {have}; history depth by the gap filler"
            return
        h = self.hold(did, "days", _iso(exp))
        if h is not None:
            ds.entities.append(h)
            return
        try:
            hot = {d.isoformat() for d in hot_days(news_hot_dir(self.s))}
        except Exception:  # noqa: BLE001
            hot = set()
        fin = [d.isoformat() for d in miss if d.isoformat() in hot]
        stream = [d.isoformat() for d in miss if d.isoformat() not in hot]
        periods = {f"{did}|days": [_iso(exp) or ""]}
        why = f"{did}: day(s) {', '.join(d.isoformat() for d in miss)} not complete"
        if fin:
            self.add_plan("news_finalise", {"days": fin}, did, why, periods=periods)
        if stream:
            self.add_plan("gdelt_stream", {"days": stream}, did, why, periods=periods)
        ds.entities.append(EntitySync("days", "due", _iso(have), _iso(exp), _iso(nxt), missing=fin + stream,
                                      detail=f"{len(miss)} day(s) to assemble ({len(fin)} from the live hot store)"
                                             + (f"; {len(later)} older day(s) on the next sync" if later else "")))

    def _gdelt_days(self) -> dict[str, Any]:
        from app.ops.paths import read_json

        return (read_json(self.s.state_dir / "events_v2.json", {}) or {}).get("days") or {}

    def daily_counts(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        have = _d(st.data_as_of)
        exp = self.today - timedelta(days=1)
        while exp.weekday() >= 5:
            exp -= timedelta(days=1)
        ds.data_as_of, ds.expected_as_of = _iso(have), _iso(exp)
        last_ok = self.last_ok_start("news_ingest")
        nxt_t = (last_ok + timedelta(hours=P.min_interval_h("news_ingest", True))) if last_ok else self.now
        ds.next_expected = _iso(max(nxt_t, self.now))
        if have and have >= exp and last_ok and self.now - last_ok < timedelta(hours=P.min_interval_h("news_ingest", True)):
            ds.entities.append(EntitySync("*", "fresh", _iso(have), _iso(exp), ds.next_expected, st.detail))
            ds.detail = f"full days through {have}; news also streams live"
            return
        h = self.hold(did, "*", _iso(exp))
        if h is not None:
            ds.entities.append(h)
            return
        if have and have >= exp:
            ds.entities.append(EntitySync("*", "due", _iso(have), _iso(exp),
                                          detail="periodic news pull (RSS / Polygon / GDELT DOC) since the watermark"))
        else:
            ds.entities.append(EntitySync("*", "due", _iso(have), _iso(exp), detail=f"latest full day {have} < {exp}"))
        for j in self.fetch_jobs(did)[0]:
            self.add_plan(j, {}, did, f"{did}: news since the watermark", periods={f"{did}|*": [_iso(exp) or ""]})

    def latest_date(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        have = _d(st.data_as_of)
        ds.data_as_of = _iso(have)
        if spec.get("group") == "analytics" or spec.get("check") == "conformal_coverage":
            return self.analytics(spec, ds, have, st)
        # filings: the EDGAR daily index of session D is complete ~22:00 ET (close + 6 h)
        exp = X.published_session("XNYS", self.now, 360)
        nxt = X.next_publication("XNYS", self.now, 360)
        ds.expected_as_of, ds.next_expected = _iso(exp), _iso(nxt)
        if have and exp and have >= exp:
            ds.entities.append(EntitySync("*", "fresh", _iso(have), _iso(exp), _iso(nxt), f"through {have}"))
            ds.detail = f"through {have}"
            return
        h = self.hold(did, "*", _iso(exp))
        if h is not None:
            h.data_as_of = _iso(have)
            ds.entities.append(h)
            return
        jobs, held = self.fetch_jobs(did)
        ds.entities += held
        miss = X.sessions_between("XNYS", have, exp)
        ds.entities.append(EntitySync("*", "due", _iso(have), _iso(exp), _iso(nxt), missing=[_iso(x) or "" for x in miss],
                                      detail=f"filings after {have} (expected through {exp})"))
        for j in jobs:
            self.add_plan(j, {}, did, f"{did}: after {have}", periods={f"{did}|*": [_iso(exp) or ""]})

    def analytics(self, spec: dict[str, Any], ds: DatasetSync, have: date | None, st: Any) -> None:
        """Derived datasets: expected = the newest price session in the lake (they are recomputed by the derive
        pass when their inputs change, never fetched)."""
        did = spec["id"]
        live = spec.get("live_job")
        core = [_d(v) for k, v in self.price_as_of.items() if v and k in ("US", "EU", "UK", "JP")]
        exp = max([d for d in core if d], default=None)
        sla = float(spec.get("sla", 1))
        ds.expected_as_of = _iso(exp)
        in_sync = live in self.reg.jobs and self.reg.jobs[live].dag in P.SYNC_DAGS and live not in P.EXCLUDE
        if exp is None or (have and have >= exp) or (have and sla >= 7 and (exp - have).days <= sla):
            ds.entities.append(EntitySync("*", "fresh", _iso(have), _iso(exp), detail=f"through {have}"))
            ds.detail = f"through {have}" + ("" if in_sync else " (refreshed by the weekly refresh)")
            return
        if not in_sync:
            ds.entities.append(EntitySync("*", "not_yet_published", _iso(have), _iso(exp),
                                          detail=f"{live} runs in the weekly refresh (lag {(exp - have).days if have else '?'} d)"))
            return
        ds.entities.append(EntitySync("*", "due", _iso(have), _iso(exp),
                                      detail=f"recomputed by {live} once its inputs are current (derive pass)"))
        self.add_plan(live, {}, did, f"{did}: analytics through {have} < prices {exp}", trigger="fingerprint",
                      periods={f"{did}|*": [_iso(exp) or ""]})

    def cftc(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        have = _d(st.data_as_of)
        exp = X.cftc_expected(self.now)
        nxt = X.next_cftc(self.now)
        ds.data_as_of, ds.expected_as_of, ds.next_expected = _iso(have), _iso(exp), _iso(nxt)
        if have and exp and have >= exp - timedelta(days=1):
            ds.entities.append(EntitySync("legacy_fut", "fresh", _iso(have), _iso(exp), _iso(nxt),
                                          f"report as of {have}; next release {nxt:%a %Y-%m-%d %H:%M} UTC"))
            ds.detail = f"report as of {have}; next release Friday 15:30 ET"
            return
        h = self.hold(did, "legacy_fut", _iso(exp))
        if h is not None:
            ds.entities.append(h)
            return
        ds.entities.append(EntitySync("legacy_fut", "due", _iso(have), _iso(exp), _iso(nxt),
                                      detail=f"report(s) after {have} released (expected {exp})"))
        for j in self.fetch_jobs(did)[0]:
            self.add_plan(j, {}, did, f"{did}: reports after {have}", periods={f"{did}|legacy_fut": [_iso(exp) or ""]})

    def q13f(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        have = _d(st.data_as_of)
        exp = X.expected_13f(self.now)
        nxt = X.next_13f(self.now)
        ds.data_as_of, ds.expected_as_of, ds.next_expected = _iso(have), _iso(exp), _iso(nxt)
        nq = X.cal.quarter_end(exp + timedelta(days=1))
        if have and have >= exp:
            ds.entities.append(EntitySync("filers", "fresh", _iso(have), _iso(exp), _iso(nxt), f"quarter {have}"))
            ds.entities.append(EntitySync(f"{nq:%Y}-Q{(nq.month - 1) // 3 + 1}", "not_yet_published", None, _iso(nq),
                                          _iso(nxt), secondary=True,
                                          detail=f"filing deadline {(nq + timedelta(days=45)).isoformat()} not reached"))
            ds.detail = f"latest quarter {have}; next deadline {(nq + timedelta(days=45)).isoformat()}"
            return
        h = self.hold(did, "filers", _iso(exp))
        if h is not None:
            ds.entities.append(h)
            return
        ds.entities.append(EntitySync("filers", "due", _iso(have), _iso(exp), _iso(nxt),
                                      detail=f"13F quarter {exp} past its deadline, not loaded"))
        for j in self.fetch_jobs(did)[0]:
            self.add_plan(j, {}, did, f"{did}: quarter {exp}", periods={f"{did}|filers": [_iso(exp) or ""]})

    def finra(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        did = spec["id"]
        st = self.observe(spec)
        have = _d(st.data_as_of)
        exp = X.finra_expected(self.now)
        nxt = X.next_finra(self.now)
        ds.data_as_of, ds.expected_as_of, ds.next_expected = _iso(have), _iso(exp), _iso(nxt)
        if have and exp and have >= exp - timedelta(days=3):
            ds.entities.append(EntitySync("*", "fresh", _iso(have), _iso(exp), _iso(nxt), f"settlement {have}"))
            ds.detail = f"settlement {have}; next publication {nxt:%Y-%m-%d %H:%M} UTC" if nxt else f"settlement {have}"
            return
        h = self.hold(did, "*", _iso(exp))
        if h is not None:
            ds.entities.append(h)
            return
        ds.entities.append(EntitySync("*", "due", _iso(have), _iso(exp), _iso(nxt),
                                      detail=f"settlement {exp} published, stored through {have}"))
        for j in self.fetch_jobs(did)[0]:
            self.add_plan(j, {}, did, f"{did}: settlement {exp}", periods={f"{did}|*": [_iso(exp) or ""]})

    def published_dates(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        """W3 (rates-w3): entities with their own official publication schedule (app.rates.ops_checks)."""
        from app.rates.ops_checks import scan_published

        return scan_published(self, spec, ds)

    def cadence_dataset(self, spec: dict[str, Any], ds: DatasetSync) -> None:
        """Datasets without a calendar period (earnings dates): due when the fetch job's daily cadence elapsed."""
        did = spec["id"]
        st = self.observe(spec)
        ds.data_as_of = _iso(st.data_as_of)
        jobs, held = self.fetch_jobs(did)
        ds.entities += held
        for j in jobs:
            e = self._cadence_entity(did, j, root=True)
            ds.entities.append(e)
            if e.reason == "due":
                self.add_plan(j, {}, did, f"{did}: daily refresh ({e.detail})", trigger="cadence",
                              periods={f"{did}|{j}": [_iso(self.today) or ""]})
        ds.detail = st.detail

    def _cadence_entity(self, did: str, job: str, root: bool) -> EntitySync:
        iv = P.min_interval_h(job, root)
        last = self.last_ok_start(job)
        lr = self.last_run(job)
        nxt = (last + timedelta(hours=iv)) if last else self.now
        if last and self.now - last < timedelta(hours=iv):
            return EntitySync(job, "fresh", _iso(last), next_expected=_iso(nxt),
                              detail=f"ran {last:%Y-%m-%d %H:%M} UTC; every {iv:g} h")
        h = self.hold(did, job, _iso(self.today))
        if h is not None:
            h.data_as_of = _iso(last)
            return h
        msg = (lr or {}).get("error_message") or ""
        if lr and lr.get("status") == "skipped" and P.missing_key_names(msg):
            keys = P.missing_key_names(msg)
            if self.missing_keys([k.lower() for k in keys]):
                return EntitySync(job, "needs_key", _iso(last), keys=keys, detail=msg[:200])
        return EntitySync(job, "due", _iso(last), detail=f"last ok run {last:%Y-%m-%d %H:%M} UTC" if last else "never ran")

    # ---------------------------------------------------------------- job-level datasets
    def root_fetch_jobs(self) -> list[str]:
        covered = {j for js in P.DATASET_FETCH.values() for j in js} | set(P.DYNAMIC_FETCH)
        out = []
        for j in self.reg.jobs.values():
            if j.dag not in P.SYNC_DAGS or j.id in P.EXCLUDE or j.id in covered or not j.writes:
                continue
            if not is_fetch(j):
                continue
            ups = [d for d in j.depends_on if d in self.reg.jobs and d not in P.EXCLUDE]
            if ups:
                continue
            out.append(j.id)
        return out

    def job_datasets(self) -> list[DatasetSync]:
        out = []
        for jid in self.root_fetch_jobs():
            did = f"job:{jid}"
            if self.only and did not in self.only and "job:*" not in self.only:
                continue
            spec = self.reg.jobs[jid]
            ds = DatasetSync(did, jid.replace("_", " "), "sources", kind="job")
            miss = self.missing_keys(P.JOB_KEYS.get(jid, ()))
            from app.core.robots_policy import disabled_job_note  # noqa: PLC0415

            off = disabled_job_note(jid)   # COVERAGE_V3 A9: source robots-disallowed -> not planned (source_disallowed)
            if off:
                ds.entities.append(EntitySync(jid, "no_source", detail=off[:200]))
            elif miss:
                ds.entities.append(EntitySync(jid, "needs_key", keys=miss, detail=f"set {', '.join(miss)}"))
            else:
                e = self._cadence_entity(did, jid, root=True)
                ds.entities.append(e)
                if e.reason == "due":
                    self.add_plan(jid, {}, did, f"{did}: daily cadence ({e.detail})", trigger="cadence",
                                  periods={f"{did}|{jid}": [_iso(self.today) or ""]})
            for k, v in (P.NO_SOURCE.get(did) or {}).items():
                ds.entities.append(EntitySync(k, "no_source", detail=v, secondary=True))
            last = self.last_ok_start(jid)
            ds.data_as_of = _iso(last)
            ds.detail = (spec.note or spec.module)[:160]
            out.append(ds.finish())
        return out


def is_fetch(spec: Any) -> bool:
    return spec.module.startswith("app.ingestion.") or spec.id in P.FETCH_EXTRA


def scan(settings: Settings | None = None, now: datetime | None = None, datasets: list[str] | None = None,
         ledger: Ledger | None = None, **kw: Any) -> ScanResult:
    return Scanner(settings, now, ledger, datasets=datasets, **kw).run()
