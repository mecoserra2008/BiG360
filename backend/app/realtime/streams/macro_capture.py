"""Macro release capture (R1.2), driven by the W2 official calendar -> raw.macro.

Schedule: pending rows of macro_release_events (one per release x tracked series, target `period`) joined to
macro_releases (scheduled_at UTC, time_known, importance, publisher, url), reloaded every `refresh_s` (15 min) for
scheduled_at in [now - 36 h, now + 24 h].

Capture loop (fake-clock testable: the clock, schedule loader and series fetcher are injectable):
* an entry becomes active at its scheduled time (time_known=false: 30 min early, because the publisher's default time
  is used) and stays active for `window_h` (6 h; 24 h when the time is not known);
* while active and not captured, the official source of the series is polled: every 15 s for the first 10 minutes,
  every 60 s until +1 h, then every 300 s (one series fetch per poll; shared per-source rate limiters);
* when the target period appears, the release measure (level / diff / m/m % / y/y %, app.macro.calendar.apply_measure
  on the regularised series) is the ACTUAL; the surprise vs the stored primary MODEL-BASED expectation
  (macro_expectations, latest version; labelled "not consensus") uses the W2 rule of app.macro.surprise:
  z = clip((actual - expectation) / scale, -5, 5), scale = std of the series' last 36 forecast errors (>= 6) else std
  of past release-to-release changes (>= 6); signed_z = z * direction;
* one message per (release_event_id, series_id): key '<release_event_id>|<series_id>', type 'release',
  ts_source = scheduled time (official publication time), payload incl. capture delay and the surprise fields.

Series fetchers (official sources): `fred:<id>` via the FRED API (St. Louis Fed; updated at the release time);
every other catalog source through its nightly fetcher (app.ingestion.macro_common.FETCHERS) for that single series.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope
from app.realtime.streams.base import StreamContext, StreamWorker

log = get_logger(__name__)

MODEL_LABEL = "Model-based expectation (not consensus)"
Z_CLIP = 5.0
ERR_WINDOW = 36
MIN_ERRORS = 6


@dataclass
class Pending:
    release_event_id: str
    series_id: str
    country: str
    period: date
    measure: str
    freq: str
    scheduled_at: datetime
    time_known: bool = True
    release_name: str = ""
    publisher: str = ""
    importance: int = 3
    theme: str | None = None
    direction: int = 1
    prior: float | None = None
    url: str | None = None
    expectation: dict[str, Any] | None = None    # point, lo80, hi80, model, version
    scale: float | None = None
    # runtime
    attempts: int = 0
    next_try: float = 0.0
    captured: bool = False
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.release_event_id}|{self.series_id}"

    def active_from(self) -> datetime:
        return self.scheduled_at - (timedelta(minutes=30) if not self.time_known else timedelta(0))

    def active_until(self, window_h: float) -> datetime:
        return self.scheduled_at + timedelta(hours=window_h if self.time_known else 24)


def poll_every(since_sched_s: float) -> float:
    if since_sched_s < 600:
        return 15.0
    if since_sched_s < 3600:
        return 60.0
    return 300.0


def surprise(actual: float, exp: dict[str, Any] | None, scale: float | None, direction: int) -> dict[str, Any]:
    if not exp or exp.get("point") is None:
        return {"expectation": None, "error": None, "error_std": scale, "surprise_z": None, "signed_z": None}
    err = float(actual) - float(exp["point"])
    z = float(np.clip(err / scale, -Z_CLIP, Z_CLIP)) if scale and scale > 0 else None
    return {"expectation": float(exp["point"]), "lo80": exp.get("lo80"), "hi80": exp.get("hi80"),
            "model": exp.get("model"), "error": err, "error_std": scale, "surprise_z": z,
            "signed_z": z * direction if z is not None else None}


def measure_value(levels: pd.Series, period: date, measure: str, freq: str) -> tuple[float | None, float | None]:
    """levels indexed by period date -> (measure at period, measure at the previous period)."""
    from app.macro.calendar import apply_measure, regularize  # noqa: PLC0415

    if levels is None or levels.empty or period not in set(levels.index):
        return None, None
    s = regularize(levels.sort_index(), freq)
    m = apply_measure(s, measure, freq)
    idx = list(m.index)
    i = idx.index(period)
    cur = m.iloc[i]
    prev = m.iloc[i - 1] if i > 0 else None
    cur = None if cur is None or (isinstance(cur, float) and math.isnan(cur)) else float(cur)
    prev = None if prev is None or (isinstance(prev, float) and math.isnan(prev)) else float(prev)
    return cur, prev


# ------------------------------------------------------------------------------------------------ data access
def load_schedule(settings: Any, now: datetime, back_h: float = 36, ahead_h: float = 24) -> list[Pending]:
    """Pending release events around `now` with their primary expectation and surprise scale."""
    import polars as pl  # noqa: PLC0415

    from app.core.storage import read_table, scan_table  # noqa: PLC0415
    from app.macro.calendar import tracked_series  # noqa: PLC0415

    lo, hi = now - timedelta(hours=back_h), now + timedelta(hours=ahead_h)
    ev = (scan_table("macro_release_events", settings)
          .filter((pl.col("status") == "pending") & (pl.col("scheduled_at") >= lo) & (pl.col("scheduled_at") <= hi))
          .collect())
    if ev.is_empty():
        return []
    rel = (scan_table("macro_releases", settings)
           .filter(pl.col("release_event_id").is_in(ev["release_event_id"].unique().implode()))
           .select("release_event_id", "release_name", "publisher", "time_known", "importance", "url")
           .collect().unique("release_event_id"))
    ev = ev.join(rel, on="release_event_id", how="left")
    sids = ev["series_id"].unique().to_list()
    ex = read_table("macro_expectations", settings=settings,
                    filters=pl.col("is_primary") & pl.col("release_event_id").is_in(ev["release_event_id"].unique().to_list()))
    ex = ex.sort("version", "forecast_made_at").unique(["release_event_id", "series_id"], keep="last")
    exp = {(r["release_event_id"], r["series_id"]): r for r in ex.iter_rows(named=True)}
    try:
        sur = read_table("macro_surprises", settings=settings, columns=["series_id", "release_date", "error", "actual"],
                         filters=pl.col("series_id").is_in(sids))
    except Exception:  # noqa: BLE001
        sur = pl.DataFrame()
    scales: dict[str, float | None] = {}
    for sid in sids:
        g = sur.filter(pl.col("series_id") == sid).sort("release_date") if sur.height else sur
        errs = g["error"].drop_nulls().to_list()[-ERR_WINDOW:] if g.height else []
        acts = g["actual"].drop_nulls().to_list() if g.height else []
        sc = None
        if len(errs) >= MIN_ERRORS:
            sc = float(np.std(errs, ddof=1))
        elif len(acts) > MIN_ERRORS:
            ch = np.diff(acts[-ERR_WINDOW - 1:])
            sc = float(np.std(ch, ddof=1)) if len(ch) >= MIN_ERRORS else None
        scales[sid] = sc
    meta = {t.series_id: t for t in tracked_series(settings)}
    out = []
    for r in ev.iter_rows(named=True):
        t = meta.get(r["series_id"])
        e = exp.get((r["release_event_id"], r["series_id"]))
        if e is not None and e.get("target_period") not in (None, r["period"]):
            e = None   # the forecast must target the period being released
        out.append(Pending(
            release_event_id=r["release_event_id"], series_id=r["series_id"], country=r["country"], period=r["period"],
            measure=r["measure"], freq=t.freq if t else "M", scheduled_at=r["scheduled_at"].replace(tzinfo=UTC)
            if r["scheduled_at"].tzinfo is None else r["scheduled_at"], time_known=bool(r.get("time_known", True)),
            release_name=r.get("release_name") or "", publisher=r.get("publisher") or "",
            importance=int(r.get("importance") or 3), theme=r.get("theme"), direction=int(t.direction) if t else 1,
            prior=r.get("prior"), url=r.get("url"),
            expectation={"point": e["point"], "lo80": e["lo80"], "hi80": e["hi80"], "model": e["model"],
                         "version": e["version"]} if e else None,
            scale=scales.get(r["series_id"])))
    return out


class SeriesFetcher:
    """series_id -> latest raw level series (pd.Series indexed by period date) from the official source."""

    def __init__(self, settings: Any, lookback_years: int = 4):
        self.s = settings
        self.lookback_years = lookback_years
        self._fred: Any = None
        self._specs: dict[str, Any] | None = None

    def __call__(self, series_id: str) -> pd.Series:
        src, _, native = series_id.partition(":")
        start = date.today().replace(day=1) - timedelta(days=365 * self.lookback_years)
        if src == "fred":
            from app.ingestion.fred import FredClient  # noqa: PLC0415

            if self._fred is None:
                self._fred = FredClient(self.s)
            obs = self._fred.observations(native, start=start)
            vals = {}
            for o in obs:
                v = o.get("value")
                if v in (None, ".", ""):
                    continue
                vals[date.fromisoformat(o["date"])] = float(v)
            return pd.Series(vals, dtype=float).sort_index()
        return self._generic(series_id, src, start)

    def _generic(self, series_id: str, src: str, start: date) -> pd.Series:
        from app.ingestion import macro_common as mc  # noqa: PLC0415

        for mod in ("macro_eu", "macro_national", "macro_intl", "macro_global"):
            try:
                __import__(f"app.ingestion.{mod}")
            except Exception:  # noqa: BLE001
                pass
        if self._specs is None:
            self._specs = {sp.series_id: sp for sp in mc.load_catalog(self.s)}
        spec = self._specs.get(series_id)
        fn = mc.FETCHERS.get(src)
        if spec is None or fn is None:
            raise LookupError(f"no live fetcher for {series_id}")
        runner = mc.MacroRunner("realtime_macro", settings=self.s, start=start)
        fn(runner, [spec])
        if not runner.frames:
            raise LookupError(f"{series_id}: {runner.report['failed'].get(series_id, 'no data')}")
        df = runner.frames[-1].drop_nulls("value").sort("date")
        return pd.Series(dict(zip(df["date"].to_list(), df["value"].to_list(), strict=True)), dtype=float).sort_index()


class MacroCaptureStream(StreamWorker):
    name = "macro_capture"
    topic = "raw.macro"
    source_id = "macro"
    interval_s = 30.0

    def __init__(self, schedule_loader: Callable[[datetime], list[Pending]] | None = None,
                 fetcher: Callable[[str], pd.Series] | None = None, **params: Any):
        super().__init__(**params)
        self.schedule_loader = schedule_loader
        self.fetcher = fetcher
        self.window_h = float(params.get("window_h", 6))
        self.refresh_s = float(params.get("refresh_s", 900))
        self.pending: dict[str, Pending] = {}
        self.done: set[str] = set()
        self._loaded_at = -1e18
        self.stats.detail = "W2 calendar -> official source polling"

    def _now_epoch(self, ctx: StreamContext) -> float:
        return ctx.clock().timestamp()

    def reload(self, ctx: StreamContext) -> None:
        now = ctx.clock()
        loader = self.schedule_loader or (lambda n: load_schedule(ctx.settings, n))
        rows = loader(now)
        keep = {}
        for p in rows:
            if p.key in self.done:
                continue
            old = self.pending.get(p.key)
            if old is not None:   # keep runtime state
                p.attempts, p.next_try, p.last_error = old.attempts, old.next_try, old.last_error
            keep[p.key] = p
        self.pending = keep
        self._loaded_at = self._now_epoch(ctx)
        n_act = sum(1 for p in keep.values() if p.active_from() <= now <= p.active_until(self.window_h))
        self.stats.detail = f"{len(keep)} scheduled in window, {n_act} active, {len(self.done)} captured"

    def metrics(self) -> dict[str, Any]:
        """Status-file metrics (Data Health > Live feeds): next scheduled releases, active / captured counts."""
        from app.realtime.streams.base import utcnow  # noqa: PLC0415

        now = utcnow()
        up = sorted((p for p in self.pending.values() if not p.captured and p.scheduled_at >= now - timedelta(hours=6)),
                    key=lambda p: p.scheduled_at)
        return {"active": len(self.active(now)), "captured": len(self.done), "scheduled": len(self.pending),
                "next": [{"release_name": p.release_name, "series_id": p.series_id, "country": p.country,
                          "scheduled_at": p.scheduled_at.isoformat().replace("+00:00", "Z"), "time_known": p.time_known,
                          "attempts": p.attempts, "last_error": p.last_error} for p in up[:5]]}

    def active(self, now: datetime) -> list[Pending]:
        return [p for p in self.pending.values()
                if not p.captured and p.active_from() <= now <= p.active_until(self.window_h)]

    def interval(self, ctx: StreamContext) -> float:
        if "interval_s" in self.params:
            return float(self.params["interval_s"])
        now = ctx.clock()
        nowe = now.timestamp()
        act = self.active(now)
        if act:
            return max(1.0, min(60.0, min(p.next_try for p in act) - nowe))
        upcoming = [p.active_from().timestamp() for p in self.pending.values()
                    if not p.captured and p.active_from() > now]
        nxt = min(upcoming) - nowe if upcoming else 60.0
        return max(1.0, min(60.0, nxt, self._loaded_at + self.refresh_s - nowe))

    def try_capture(self, p: Pending, now: datetime) -> Envelope | None:
        fetch = self.fetcher or SeriesFetcher(None)
        p.attempts += 1
        since = (now - p.scheduled_at).total_seconds()
        p.next_try = now.timestamp() + poll_every(max(0.0, since))
        try:
            levels = fetch(p.series_id)
        except Exception as e:  # noqa: BLE001
            p.last_error = f"{type(e).__name__}: {e}"[:200]
            raise
        actual, prev = measure_value(levels, p.period, p.measure, p.freq)
        if actual is None:
            return None
        p.captured = True
        s = surprise(actual, p.expectation, p.scale, p.direction)
        payload = {
            "release_event_id": p.release_event_id, "series_id": p.series_id, "country": p.country,
            "release_name": p.release_name, "publisher": p.publisher, "importance": p.importance, "theme": p.theme,
            "period": str(p.period), "measure": p.measure, "actual": actual, "prior": p.prior,
            "prior_revised": prev, "prior_revision": (prev - p.prior) if (prev is not None and p.prior is not None) else None,
            "scheduled_at": p.scheduled_at.isoformat().replace("+00:00", "Z"), "time_known": p.time_known,
            "captured_at": now.isoformat().replace("+00:00", "Z"), "capture_delay_s": round(since, 1),
            "attempts": p.attempts, "direction": p.direction, "url": p.url, "model_based": True, "label": MODEL_LABEL,
            "expectation_version": (p.expectation or {}).get("version"), **s,
        }
        return Envelope("raw.macro", p.key, payload, type="release", ts_source=p.scheduled_at)

    def run_due(self, ctx: StreamContext) -> list[Envelope]:
        now = ctx.clock()
        if self._now_epoch(ctx) - self._loaded_at >= self.refresh_s:
            self.reload(ctx)
        if self.fetcher is None:
            self.fetcher = SeriesFetcher(ctx.settings)
        out: list[Envelope] = []
        errors = []
        for p in self.active(now):
            if p.next_try > now.timestamp():
                continue
            try:
                env = self.try_capture(p, now)
            except Exception as e:  # noqa: BLE001 - per-series failures do not stop other releases
                errors.append(f"{p.series_id}: {e}")
                continue
            if env is not None:
                out.append(env)
                self.done.add(p.key)
        for k in [k for k, p in self.pending.items() if p.captured or now > p.active_until(self.window_h)]:
            self.pending.pop(k, None)
        if errors and not out and len(errors) == len(self.active(now)) and len(errors) > 0:
            log.info("macro capture: %d fetch errors (%s)", len(errors), errors[0][:160])
        return out

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        t0 = time.perf_counter()
        out = await ctx.run_sync(self.run_due, ctx)
        if out:
            log.info("macro capture: %d release(s) captured in %.1fs", len(out), time.perf_counter() - t0)
        return out
