"""Gap detector (O1.2): job `data_gaps_detect` -> ops/data_gaps + data/state/ops/coverage_status.json.

For every contracts/coverage.yaml dataset it compares expected (calendar x entities) with actual and classifies each
gap: missing | stale | partial | suspicious | unfillable. Known gaps (coverage.yaml known_gaps) are seeded. The
filler's bookkeeping (attempts, back-off, status) survives re-detection; gaps that are no longer detected become
`filled` (resolved_at). Checks only read narrow column sets through DuckDB with a 1 GB memory limit.

    python -m app.ops.gaps [--dataset prices.EU] [--dry-run]
    detect(settings) -> DetectReport
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import _parquet_files, duckdb_connect, read_table, register_views, table_dir, write_table
from app.ops import calendars as cal
from app.ops.coverage import effective_sla, load_coverage
from app.ops.paths import ops_state_dir, read_json, write_json_atomic

log = get_logger("app.ops.gaps")

OPEN_STATUSES = ("open", "queued", "filling", "failed")
HEATMAP_DAYS = 21


@dataclass
class Gap:
    dataset: str
    entity: str
    gap_class: str
    period_start: date | None = None
    period_end: date | None = None
    expected: int | None = None
    actual: int | None = None
    note: str | None = None
    status: str | None = None             # override (manual / unfillable / filled for resolved seeds)
    fill_kind: str | None = None
    fill_params: dict[str, Any] | None = None
    key: str | None = None                # gap_id suffix override (stale gaps)
    origin: str = "detector"
    priority: int | None = None
    source: str | None = None
    cost_units: float | None = None

    @property
    def gap_id(self) -> str:
        if self.key:
            return f"{self.dataset}|{self.entity}|{self.key}"
        a = self.period_start.isoformat() if self.period_start else ""
        b = self.period_end.isoformat() if self.period_end else ""
        return f"{self.dataset}|{self.entity}|{a}|{b}"


@dataclass
class DatasetStatus:
    dataset: str
    label: str
    group: str
    status: str = "unknown"               # fresh | stale | partial | missing | unknown | error
    data_as_of: date | None = None
    expected_as_of: date | None = None
    lag: float | None = None              # sessions (or days) behind expected
    sla: float | None = None
    detail: str | None = None
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)   # per region / currency / source
    cells: dict[str, float | None] = field(default_factory=dict)        # heatmap: iso date -> coverage ratio

    def to_dict(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "label": self.label, "group": self.group, "status": self.status,
                "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
                "expected_as_of": self.expected_as_of.isoformat() if self.expected_as_of else None,
                "lag": self.lag, "sla": self.sla, "detail": self.detail, "entities": self.entities, "cells": self.cells}


@dataclass
class Ctx:
    settings: Settings
    con: Any
    today: date
    now: datetime
    profile_sla_days: float | None
    reg: dict[str, Any]
    _views: set[str] = field(default_factory=set)

    def view(self, table: str) -> bool:
        """Register the lake view of `table`; False when the table has no files."""
        if table in self._views:
            return True
        if not _parquet_files(table_dir(table, self.settings)):
            return False
        register_views(self.con, [table], settings=self.settings)
        self._views.add(table)
        return True

    def q(self, sql: str) -> list[tuple]:
        return self.con.execute(sql).fetchall()

    def years(self, start: date) -> list[str]:
        return [str(y) for y in range(start.year, self.today.year + 1)]

    def src(self, table: str, parts: dict[str, list[Any]] | None = None) -> str | None:
        """read_parquet() over only the partition directories matching `parts` ({col: [values]}; a partition column
        without values matches every directory) - avoids scanning e.g. 93 M price rows for one region's last month.
        Columns are read physically (fine for the plain columns the checks use). None when no file matches."""
        from app.core.schema import get_contract

        c = get_contract(table)
        root = table_dir(c, self.settings)
        parts = parts or {}
        dirs = [root]
        for col in c.partition_by:
            vals = parts.get(col)
            nxt = []
            for d in dirs:
                if vals is None:
                    nxt += [p for p in d.glob(f"{col}=*") if p.is_dir()]
                else:
                    nxt += [d / f"{col}={v}" for v in vals if (d / f"{col}={v}").is_dir()]
            dirs = nxt
        files = sorted(f for d in dirs for f in d.glob("*.parquet"))
        if not files:
            return None
        lst = ", ".join("'" + f.as_posix().replace("'", "''") + "'" for f in files)
        hive = "true" if c.partition_by else "false"
        return f"read_parquet([{lst}], union_by_name = true, hive_partitioning = {hive})"

    def spec_src(self, spec: dict[str, Any], start: date | None = None) -> str | None:
        """Source for a dataset spec: coverage.yaml `parts` plus `year` / `year_month` limited to the lookback."""
        from app.core.schema import get_contract

        table = spec["table"]
        c = get_contract(table)
        parts = {k: [str(v) for v in vs] for k, vs in (spec.get("parts") or {}).items()}
        s0 = start or (self.today - timedelta(days=int(spec.get("lookback_days", 45))))
        if "year" in c.partition_by and "year" not in parts:
            parts["year"] = self.years(s0)
        if "year_month" in c.partition_by and "year_month" not in parts:
            ms, d = [], date(s0.year, s0.month, 1)
            while d <= self.today:
                ms.append(f"{d:%Y-%m}")
                d = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
            parts["year_month"] = ms
        return self.src(table, parts)


CheckFn = Callable[[Ctx, dict[str, Any], DatasetStatus], list[Gap]]
CHECKS: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn
    return deco


def _lit(d: date) -> str:
    return f"DATE '{d.isoformat()}'"


def _expected_latest(calendar: str | None, today: date, settings: Settings) -> date | None:
    """Latest date whose data should exist by now (the session before today: nightly ingestion runs after close)."""
    if calendar in (None, "daily"):
        return today - timedelta(days=1)
    return cal.last_session_on_or_before(calendar, today - timedelta(days=1), settings)


def _sessions_between(calendar: str | None, a: date, b: date, settings: Settings) -> int:
    """Number of sessions in (a, b]."""
    if b <= a:
        return 0
    if calendar in (None, "daily"):
        return (b - a).days
    return len(cal.sessions(calendar, a + timedelta(days=1), b, settings))


# ------------------------------------------------------------------------------------------------ checks


@check("sessions")
def _check_sessions(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    """Per-session bar counts of one region vs its exchange calendar: missing / partial sessions + staleness."""
    table, region, mic = spec.get("table", "prices_daily"), spec["region"], spec.get("calendar")
    ent = spec.get("entities") or {}
    lookback = int(spec.get("lookback_days", 45))
    start = cx.today - timedelta(days=lookback)
    src = cx.src(table, {"region": [region], "year": cx.years(start)})
    if src is None:
        st.status, st.detail = "missing", "no data"
        return [Gap(spec["id"], region, "missing", note="table has no data", key="nodata")]
    rows = cx.q(f"""SELECT date, count(DISTINCT security_id) FROM {src} WHERE date >= {_lit(start)}
                    AND date < {_lit(cx.today)} AND security_id NOT LIKE 'IDX:%' AND security_id NOT LIKE 'ETF:%'
                    GROUP BY 1""")
    counts = {r[0]: int(r[1]) for r in rows}
    sess = cal.sessions(mic, start, cx.today - timedelta(days=1), cx.settings)
    sess_counts = [counts.get(d, 0) for d in sess]
    nonzero = [n for n in sess_counts[-25:] if n > 0]
    typical = statistics.median(nonzero) if nonzero else 0
    typical = max(typical, 1)
    part, miss = float(ent.get("partial_below", 0.8)), float(ent.get("missing_below", 0.05))
    good = [d for d in sess if counts.get(d, 0) >= part * typical]
    latest_good = max(good) if good else None
    exp = _expected_latest(mic, cx.today, cx.settings)
    sla = effective_sla(spec, cx.profile_sla_days)
    st.sla, st.expected_as_of, st.data_as_of = sla, exp, latest_good
    st.entities[region] = {"typical": int(typical), "latest": max(counts).isoformat() if counts else None,
                           "data_as_of": latest_good.isoformat() if latest_good else None}
    gaps: list[Gap] = []
    for d in sess:
        if latest_good is None or d > latest_good:
            continue
        n = counts.get(d, 0)
        if n < miss * typical:
            gaps.append(Gap(spec["id"], region, "missing", d, d, int(typical), n,
                            note=f"{n} bars on an open {mic} session (typical {int(typical)})"))
        elif n < part * typical:
            gaps.append(Gap(spec["id"], region, "partial", d, d, int(typical), n,
                            note=f"{n}/{int(typical)} bars ({n / typical:.0%})"))
    sset = set(sess)
    for i in range(HEATMAP_DAYS):
        d = cx.today - timedelta(days=HEATMAP_DAYS - i)
        st.cells[d.isoformat()] = (round(min(counts.get(d, 0) / typical, 1.5), 3) if d in sset else None)
    lag = _sessions_between(mic, latest_good, exp, cx.settings) if (latest_good and exp) else None
    st.lag = lag
    if latest_good is None:
        st.status = "missing"
        gaps.append(Gap(spec["id"], region, "stale", None, exp, note="no complete session in the lookback", key="stale"))
    elif lag is not None and lag > sla:
        st.status = "stale"
        first = next((d for d in sess if d > latest_good), latest_good + timedelta(days=1))
        gaps.append(Gap(spec["id"], region, "stale", first, exp, lag and int(lag), 0,
                        note=f"{lag} sessions behind (SLA {sla:g})", key="stale"))
    else:
        st.status = "partial" if gaps else "fresh"
    st.detail = f"latest complete session {latest_good}; {len(gaps)} gap(s)"
    return gaps


@check("currency_dates")
def _check_currency(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table = spec.get("table", "fx_rates")
    ent = spec.get("entities") or {}
    look = int(ent.get("lookback_days", 60))
    start = cx.today - timedelta(days=look)
    src = cx.src(table, {"year": cx.years(start)})
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
    rows = cx.q(f"""SELECT currency, max(date), count(*) FILTER (WHERE usd_per_unit IS NULL), count(*)
                    FROM {src} WHERE date >= {_lit(start)} AND date <= {_lit(cx.today)} GROUP BY 1""")
    days = cx.q(f"""SELECT date, count(DISTINCT currency) FROM {src} WHERE date >= {_lit(cx.today - timedelta(days=HEATMAP_DAYS))}
                    AND date < {_lit(cx.today)} GROUP BY 1""")
    exp = _expected_latest("weekdays", cx.today, cx.settings)
    sla = effective_sla(spec, cx.profile_sla_days)
    slow, retired = dict(ent.get("slow_sources") or {}), dict(ent.get("retired") or {})
    gaps: list[Gap] = []
    lags = []
    n_cur = max(1, len(rows))
    for cur, last, nulls, n in rows:
        if cur in retired or cur == "USD":
            continue
        lag = _sessions_between("weekdays", last, exp, cx.settings) if exp else 0
        st.entities[cur] = {"last": last.isoformat(), "lag": lag, "null_usd_per_unit": int(nulls)}
        if lag > sla:
            lags.append(lag)
            if cur in slow:
                gaps.append(Gap(spec["id"], cur, "stale", last + timedelta(days=1), exp, note=slow[cur],
                                status="manual", key="stale"))
            else:
                gaps.append(Gap(spec["id"], cur, "stale", last + timedelta(days=1), exp, lag, 0,
                                note=f"{cur} {lag} business days behind (SLA {sla:g})", key="stale"))
        if nulls:
            gaps.append(Gap(spec["id"], cur, "suspicious", None, None, int(n), int(n - nulls),
                            note=f"{nulls} recent rows with usd_per_unit null", key="null_usd_recent"))
    lasts = [r[1] for r in rows if r[0] not in retired]
    st.data_as_of = max(lasts) if lasts else None
    st.expected_as_of, st.sla = exp, sla
    st.lag = max(lags) if lags else 0
    for d, n in days:
        if d.weekday() < 5:
            st.cells[d.isoformat()] = round(n / n_cur, 3)
    stale = [g for g in gaps if g.gap_class == "stale" and g.status != "manual"]
    st.status = "stale" if stale else ("partial" if gaps else "fresh")
    st.detail = f"{len(rows)} currencies; {len(stale)} stale beyond SLA"
    return gaps


@check("macro_releases")
def _check_macro_releases(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table = spec.get("table", "macro_release_events")
    src = cx.src(table)
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="no release calendar yet", key="nodata")]
    a, b = cx.today - timedelta(days=30), cx.today - timedelta(days=int(effective_sla(spec, cx.profile_sla_days)))
    rows = cx.q(f"""SELECT coalesce(country, '?'), count(*) FILTER (WHERE status = 'pending'),
                           count(*) FILTER (WHERE status = 'released'), count(*), min(release_date) FILTER (WHERE status = 'pending'),
                           max(release_date) FILTER (WHERE status = 'pending')
                    FROM {src} WHERE release_date >= {_lit(a)} AND release_date <= {_lit(b)} GROUP BY 1""")
    last = cx.q(f"""SELECT max(release_date) FROM {src} WHERE status = 'released' AND release_date <= {_lit(cx.today)}""")
    gaps = []
    for country, pend, rel, n, d0, d1 in rows:
        st.entities[country] = {"pending": int(pend), "released": int(rel), "events": int(n)}
        if pend:
            gaps.append(Gap(spec["id"], country, "missing", d0, d1, int(n), int(n - pend),
                            note=f"{pend} release(s) dated {d0}..{d1} not ingested yet", key="pending"))
    st.data_as_of = last[0][0] if last and last[0][0] else None
    st.expected_as_of, st.sla = b, effective_sla(spec, cx.profile_sla_days)
    pend_total = sum(int(r[1]) for r in rows)
    tot = sum(int(r[3]) for r in rows) or 1
    st.lag = pend_total
    st.status = "stale" if pend_total / tot > 0.25 else ("partial" if pend_total else "fresh")
    st.detail = f"{pend_total} of {tot} releases in the last 30 days still pending"
    return gaps


@check("latest_ingest")
def _check_latest_ingest(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table = spec["table"]
    src = cx.src(table, {k: [str(v) for v in vs] for k, vs in (spec.get("parts") or {}).items()})
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
    ent = spec.get("entities") or {}
    ignore, weekly = set(ent.get("ignore") or []), set(ent.get("weekly") or [])
    by = spec.get("by")
    col = spec.get("ingest_column", "ingested_at")
    sla = effective_sla(spec, cx.profile_sla_days)
    if by:
        rows = cx.q(f'SELECT CAST("{by}" AS VARCHAR), max("{col}") FROM {src} GROUP BY 1')
    else:
        rows = [("*", cx.q(f'SELECT max("{col}") FROM {src}')[0][0])]
    gaps, worst = [], 0.0
    for key, ts in rows:
        if key in ignore or ts is None:
            continue
        d = ts.date() if isinstance(ts, datetime) else ts
        lag = (cx.today - d).days
        lim = max(sla, 10) if key in weekly else sla
        st.entities[str(key)] = {"last_ingest": d.isoformat(), "lag_days": lag}
        if lag > lim:
            worst = max(worst, lag)
            gaps.append(Gap(spec["id"], str(key), "stale", d + timedelta(days=1), cx.today, lag, 0,
                            note=f"last ingest {d} ({lag} d, SLA {lim:g})", key="stale"))
    ds = [date.fromisoformat(v["last_ingest"]) for v in st.entities.values()]
    st.data_as_of = max(ds) if ds else None
    st.expected_as_of, st.sla, st.lag = cx.today, sla, worst
    st.status = "stale" if gaps else "fresh"
    st.detail = f"{len(gaps)} of {len(st.entities)} {by or 'table'}(s) stale"
    return gaps


def _gdelt_state(settings: Settings) -> dict[str, Any]:
    return read_json(settings.state_dir / "events_v2.json", {}) or {}


@check("gdelt_days")
def _check_gdelt(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    ent = spec.get("entities") or {}
    files = int(ent.get("files_per_day", 576))
    state = _gdelt_state(cx.settings)
    days: dict[str, Any] = state.get("days") or {}
    gaps = []
    for k, rec in days.items():
        if not isinstance(rec, dict):
            continue
        miss = int(rec.get("files_missing") or 0)
        if miss <= 0:
            continue
        d = date.fromisoformat(k)
        ok = int(rec.get("files_ok") or 0)
        tot = int(rec.get("files") or files)
        cls = "partial" if ok > 0 else "missing"
        note = f"{ok}/{tot} files ({miss} missing, {int(rec.get('errors') or 0)} errors)"
        if ok == 0 and not rec.get("errors"):
            note += " - all files 404: probable GDELT outage"
        gaps.append(Gap(spec["id"], k, cls, d, d, tot, ok, note=note, fill_kind="gdelt_redo",
                        fill_params={"day": k, "outage": ok == 0 and not rec.get("errors")}))
    look = int(ent.get("stream_lookback_days", 14))
    exp = cx.today - timedelta(days=1)
    recent = [date.fromisoformat(k) for k in days if k >= (cx.today - timedelta(days=look)).isoformat()]
    wm = max(recent) if recent else (max(date.fromisoformat(k) for k in days) if days else None)
    sla = effective_sla(spec, cx.profile_sla_days)
    lag = (exp - wm).days if wm else None
    st.data_as_of, st.expected_as_of, st.sla, st.lag = wm, exp, sla, lag
    if wm is None or (lag is not None and lag > sla):
        gaps.append(Gap(spec["id"], "stream", "stale", (wm + timedelta(days=1)) if wm else None, exp, lag, 0,
                        note=f"nightly gdelt_stream watermark {wm} ({lag} d behind, SLA {sla:g})", key="stale",
                        fill_kind="job", fill_params={"jobs": [{"job": "gdelt_stream", "params": {"max_days": 10}}]}))
    for i in range(HEATMAP_DAYS):
        d = cx.today - timedelta(days=HEATMAP_DAYS - i)
        rec = days.get(d.isoformat())
        st.cells[d.isoformat()] = round(int(rec.get("files_ok") or 0) / files, 3) if isinstance(rec, dict) else 0.0
    hs = date.fromisoformat(str(ent.get("history_start", "2020-01-01")))
    total = (exp - hs).days + 1
    have = sum(1 for k in days if k >= hs.isoformat() and k <= exp.isoformat())
    st.entities["history"] = {"start": hs.isoformat(), "days_done": have, "days_total": total,
                              "pct": round(100 * have / max(total, 1), 1)}
    stale = any(g.gap_class == "stale" for g in gaps)
    st.status = "stale" if stale else ("partial" if gaps else "fresh")
    st.detail = f"{len(days)} days checkpointed; history {have}/{total}; {len(gaps)} gap(s)"
    return gaps


@check("daily_counts")
def _check_daily_counts(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table, dcol = spec["table"], spec.get("date_column", "date")
    ent = spec.get("entities") or {}
    look = int(ent.get("lookback_days", 21))
    start = cx.today - timedelta(days=look)
    src = cx.spec_src(spec, start)
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
    rows = cx.q(f"""SELECT CAST("{dcol}" AS DATE), count(*) FROM {src} WHERE CAST("{dcol}" AS DATE) >= {_lit(start)}
                    AND CAST("{dcol}" AS DATE) < {_lit(cx.today)} GROUP BY 1""")
    counts = {r[0]: int(r[1]) for r in rows}
    days = cal.business_days(start, cx.today - timedelta(days=1))
    nz = [counts.get(d, 0) for d in days if counts.get(d, 0) > 0]
    typical = max(statistics.median(nz) if nz else 1, 1)
    good = [d for d in days if counts.get(d, 0) >= float(ent.get("partial_below", 0.2)) * typical]
    latest = max(good) if good else None
    exp = _expected_latest("weekdays", cx.today, cx.settings)
    sla = effective_sla(spec, cx.profile_sla_days)
    gaps = []
    for d in days:
        if latest is None or d > latest:
            continue
        n = counts.get(d, 0)
        if n < float(ent.get("missing_below", 0.05)) * typical:
            gaps.append(Gap(spec["id"], "*", "missing", d, d, int(typical), n, note=f"{n} items (typical {int(typical)})"))
    for i in range(HEATMAP_DAYS):
        d = cx.today - timedelta(days=HEATMAP_DAYS - i)
        st.cells[d.isoformat()] = round(min(counts.get(d, 0) / typical, 1.5), 3) if d.weekday() < 5 else None
    lag = _sessions_between("weekdays", latest, exp, cx.settings) if latest and exp else None
    st.data_as_of, st.expected_as_of, st.sla, st.lag = latest, exp, sla, lag
    if latest is None or (lag is not None and lag > sla):
        gaps.append(Gap(spec["id"], "*", "stale", latest + timedelta(days=1) if latest else None, exp, lag, 0,
                        note=f"latest full day {latest} ({lag} business days behind, SLA {sla:g})", key="stale"))
        st.status = "stale"
    else:
        st.status = "partial" if gaps else "fresh"
    st.detail = f"typical {int(typical)}/day; {len(gaps)} gap(s)"
    return gaps


@check("latest_date")
def _check_latest_date(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table, dcol = spec["table"], spec.get("date_column", "date")
    src = cx.spec_src(spec, cx.today - timedelta(days=int(spec.get("lookback_days", 400))))
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
    where = f" AND ({spec['where']})" if spec.get("where") else ""
    last = cx.q(f'SELECT max(CAST("{dcol}" AS DATE)) FROM {src} WHERE CAST("{dcol}" AS DATE) <= {_lit(cx.today)}{where}')[0][0]
    mic = spec.get("calendar")
    exp = _expected_latest(mic if mic != "weekdays" else "weekdays", cx.today, cx.settings)
    sla = effective_sla(spec, cx.profile_sla_days)
    lag = _sessions_between(mic, last, exp, cx.settings) if (last and exp) else None
    st.data_as_of, st.expected_as_of, st.sla, st.lag = last, exp, sla, lag
    if last is None or (lag is not None and lag > sla):
        st.status = "stale" if last else "missing"
        live = spec.get("live_job")
        fill = spec.get("fill")
        return [Gap(spec["id"], "*", "stale", last + timedelta(days=1) if last else None, exp, lag, 0,
                    note=f"data through {last} ({lag} behind, SLA {sla:g})" + (f"; refreshed by {live}" if live else ""),
                    key="stale", fill_kind=None if fill else "dag")]
    st.status = "fresh"
    st.detail = f"data through {last}"
    return []


@check("live_news")
def _check_live_news(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    """roadmap v3 N2-N3: the realtime / live news service is up (when switched on), the hourly fold is recent and
    complete UTC days with hot data get finalised. Heatmap cell = hours folded / 24 per day."""
    from app.news_live.compaction import read_state  # noqa: PLC0415
    from app.news_live.hot import hot_days, news_hot_dir  # noqa: PLC0415
    from app.ops.supervisor import live_news_enabled  # noqa: PLC0415
    from app.realtime.supervisor import read_status  # noqa: PLC0415

    ent = spec.get("entities") or {}
    gaps: list[Gap] = []
    enabled = live_news_enabled(cx.settings)
    rt = read_status(cx.settings) or {}
    hb = rt.get("heartbeat_at")
    age = (cx.now - datetime.fromisoformat(hb)).total_seconds() if hb else None
    up = bool(rt.get("alive")) and age is not None and age <= float(ent.get("max_heartbeat_age_s", 120))
    st.entities["service"] = {"enabled": enabled, "running": up, "heartbeat_age_s": round(age, 1) if age else None}
    if enabled and not up:
        gaps.append(Gap(spec["id"], "service", "stale", note="live news service not running (supervisor child "
                        "`realtime`; tray: Live news on/off)", key="service"))
    nl = read_state(cx.settings)
    folded = nl.get("folded") or {}
    fin = set((nl.get("finalised") or {}).keys())
    last = (nl.get("last_hourly") or {}).get("at")
    fold_age_h = (cx.now - datetime.fromisoformat(last)).total_seconds() / 3600 if last else None
    days = hot_days(news_hot_dir(cx.settings))
    st.entities["hourly"] = {"last_fold_at": last, "age_h": round(fold_age_h, 2) if fold_age_h is not None else None}
    if days and (fold_age_h is None or fold_age_h > float(ent.get("max_fold_age_h", 3))):
        gaps.append(Gap(spec["id"], "hourly", "stale", note=f"hourly fold last ran {last or 'never'} (job news_hourly)",
                        key="hourly", fill_kind="job", fill_params={"jobs": [{"job": "news_hourly"}]}))
    late = float(ent.get("finalise_after_h", 26))
    for d in days:
        due = datetime(d.year, d.month, d.day, tzinfo=cx.now.tzinfo) + timedelta(hours=24 + late)
        if d.isoformat() not in fin and cx.now > due and d >= cx.today - timedelta(days=9):
            gaps.append(Gap(spec["id"], d.isoformat(), "partial", d, d, 24, len(folded.get(d.isoformat()) or {}),
                            note="UTC day not finalised (job news_finalise)", fill_kind="job",
                            fill_params={"jobs": [{"job": "news_finalise", "params": {"days": [d.isoformat()]}}]}))
    for i in range(HEATMAP_DAYS):
        d = cx.today - timedelta(days=HEATMAP_DAYS - i)
        n = len(folded.get(d.isoformat()) or {})
        st.cells[d.isoformat()] = 1.0 if d.isoformat() in fin else round(n / 24, 3)
    st.data_as_of = max((date.fromisoformat(x) for x in fin), default=None)
    st.expected_as_of = cx.today - timedelta(days=1)
    st.status = "fresh" if not gaps else "stale"
    st.detail = ("switched off (tray)" if not enabled else ("running" if up else "not running")) +         (f"; hourly fold {round(fold_age_h, 1)} h ago" if fold_age_h is not None else "; no hourly fold yet")
    return gaps


@check("cftc_weekly")
def _check_cftc(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table = spec.get("table", "cftc_positions")
    ent = spec.get("entities") or {}
    rt = ent.get("report_type", "legacy_fut")
    weeks = int(ent.get("history_weeks", 104))
    src = cx.src(table, {"report_type": [rt]})
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], rt, "missing", note="table has no data", key="nodata")]
    start = cx.today - timedelta(weeks=weeks)
    rows = cx.q(f"""SELECT CAST(report_date AS DATE), count(DISTINCT market_code) FROM {src}
                    WHERE report_date >= {_lit(start - timedelta(days=3))} GROUP BY 1""")
    have = {r[0]: int(r[1]) for r in rows}
    exp_dates = cal.cftc_report_dates(start, cx.now)
    med = statistics.median(list(have.values())) if have else 0
    gaps = []
    for tue in exp_dates:
        hit = next((d for d in (tue, tue - timedelta(days=1), tue + timedelta(days=1)) if d in have), None)
        if hit is None:
            gaps.append(Gap(spec["id"], rt, "missing", tue, tue, int(med), 0, note=f"no {rt} report as of {tue}"))
        elif have[hit] < 0.5 * med:
            gaps.append(Gap(spec["id"], rt, "partial", tue, tue, int(med), have[hit], note=f"{have[hit]} markets (median {int(med)})"))
    last = max(have) if have else None
    exp = exp_dates[-1] if exp_dates else None
    st.data_as_of, st.expected_as_of, st.sla = last, exp, effective_sla(spec, cx.profile_sla_days)
    st.lag = (exp - last).days if (exp and last) else None
    recent_missing = [g for g in gaps if g.period_start and exp and g.period_start == exp]
    st.status = "stale" if (last is None or (st.lag or 0) > st.sla) else ("partial" if gaps else "fresh")
    if recent_missing and st.status == "stale":
        recent_missing[0].gap_class = "stale"
    st.detail = f"{len(have)} weekly reports in {weeks} weeks; {len(gaps)} missing/partial"
    return gaps


@check("quarterly_13f")
def _check_13f(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table = spec.get("table", "institutional_holdings")
    ent = spec.get("entities") or {}
    nq = int(ent.get("quarters", 8))
    expq = cal.expected_13f_period(cx.now)
    qs = [expq]
    for _ in range(nq - 1):
        q = qs[-1]
        qs.append(date(q.year, q.month - 2, 1) - timedelta(days=1) if q.month > 3 else date(q.year - 1, 12, 31))
    src = cx.src(table, {"period_end_year": cx.years(min(qs))})
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
    rows = cx.q(f"""SELECT CAST(period_end AS DATE), count(DISTINCT filer_cik) FROM {src}
                    WHERE period_end >= {_lit(min(qs))} GROUP BY 1""")
    have = {r[0]: int(r[1]) for r in rows}
    med = statistics.median([have[q] for q in qs if q in have]) if any(q in have for q in qs) else 0
    gaps = []
    for q in sorted(qs):
        n = have.get(q, 0)
        if n == 0:
            gaps.append(Gap(spec["id"], "filers", "missing", q, q, int(med), 0, note=f"no 13F holdings for period {q}"))
        elif n < float(ent.get("partial_below", 0.8)) * med and q != expq:
            gaps.append(Gap(spec["id"], "filers", "partial", q, q, int(med), n, note=f"{n} filers (median {int(med)})"))
    last = max(have) if have else None
    st.data_as_of, st.expected_as_of, st.sla = last, expq, effective_sla(spec, cx.profile_sla_days)
    st.lag = (expq - last).days if last else None
    st.status = "stale" if (last is None or last < expq) else ("partial" if gaps else "fresh")
    st.detail = f"quarters {', '.join(f'{q:%Y-%m}:{have.get(q, 0)}' for q in sorted(qs)[-4:])}"
    return gaps


@check("finra_settlements")
def _check_finra(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table, dcol = spec.get("table", "short_interest"), spec.get("date_column", "settlement_date")
    start = cx.today - timedelta(days=150)
    src = cx.src(table, {"year": cx.years(start)})
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
    rows = cx.q(f'SELECT DISTINCT CAST("{dcol}" AS DATE) FROM {src} WHERE "{dcol}" >= {_lit(start)}')
    have = {r[0] for r in rows}
    exp = cal.finra_settlements(start + timedelta(days=20), cx.now, cx.settings)
    gaps = []
    for d in exp:
        if not any(abs((d - h).days) <= 3 for h in have):
            gaps.append(Gap(spec["id"], "*", "missing", d, d, None, 0, note=f"no settlement near {d}"))
    last = max(have) if have else None
    st.data_as_of, st.expected_as_of, st.sla = last, (exp[-1] if exp else None), effective_sla(spec, cx.profile_sla_days)
    st.lag = (st.expected_as_of - last).days if (last and st.expected_as_of) else None
    st.status = "stale" if (last is None or (st.lag or 0) > st.sla) else ("partial" if gaps else "fresh")
    st.detail = f"{len(have)} settlements in 150 days"
    return gaps


@check("sentinel_rows")
def _check_sentinel(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    table, dcol, sent = spec["table"], spec.get("date_column", "date"), spec.get("sentinel", "1900-01-01")
    src = cx.src(table)
    if src is None:
        st.status = "missing"
        return []
    n, last = cx.q(f"""SELECT count(*) FILTER (WHERE CAST("{dcol}" AS DATE) = DATE '{sent}'),
                       max(CAST(knowledge_date AS DATE)) FILTER (WHERE CAST(knowledge_date AS DATE) <= {_lit(cx.today)})
                       FROM {src}""")[0]
    st.data_as_of, st.status = last, "fresh"
    st.detail = f"{n} rows dated {sent}"
    if n:
        st.status = "partial"
        return [Gap(spec["id"], "sentinel", "suspicious", None, None, None, int(n),
                    note=f"{n} rows with {dcol} = {sent} (sentinel; needs an ingest fix)", status="manual", key="sentinel")]
    return []


@check("unpriced_securities")
def _check_unpriced(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    """Securities of the published snapshot without any daily bar, per region (serving sec.close IS NULL)."""
    import duckdb  # noqa: PLC0415

    from app.core.storage import current_serving_db  # noqa: PLC0415

    p = current_serving_db(cx.settings)
    if p is None:
        st.status = "unknown"
        return []
    try:
        con = duckdb.connect(str(p), read_only=True)
        try:
            rows = con.execute("SELECT region, count(*) FILTER (WHERE close IS NULL), count(*) FROM sec GROUP BY 1").fetchall()
        finally:
            con.close()
    except duckdb.Error as e:
        st.status, st.detail = "unknown", f"snapshot unreadable: {e}"[:200]
        return []
    gaps = []
    for region, n_null, n in rows:
        st.entities[str(region)] = {"unpriced": int(n_null), "securities": int(n)}
        if n_null:
            gaps.append(Gap(spec["id"], str(region), "missing", None, None, int(n), int(n - n_null),
                            note=f"{n_null} of {n} securities without price history", key="unpriced",
                            fill_kind="campaign", fill_params={"campaign": "yahoo_price_history", "region": region}))
    tot = sum(int(r[1]) for r in rows)
    st.status = "partial" if tot else "fresh"
    st.detail = f"{tot} unpriced securities"
    st.lag = tot
    return gaps


@check("register_coverage")
def _check_register_coverage(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    """Coverage v2 W2: share of a market's regulatory-register issuers with a current annual / interim statement
    (job regulatory_coverage -> data/state/regulatory_coverage.json) below its target -> one `partial` gap per cell,
    filled by the market's statements campaign (coverage.yaml fill); a report older than the SLA -> `stale`."""
    mk = str(spec.get("market") or "")
    js = read_json(cx.settings.state_dir / "regulatory_coverage.json", {}) or {}
    v = (js.get("summary") or {}).get(mk)
    if not v:
        st.status, st.detail = "missing", "regulatory_coverage not computed yet"
        return [Gap(spec["id"], mk, "missing", note="regulatory_coverage not computed yet", key="nodata",
                    fill_kind="job", fill_params={"jobs": [{"job": "regulatory_coverage"}]})]
    ent = spec.get("entities") or {}
    targets = {"annual": float(ent.get("annual_target", 95)), "interim": float(ent.get("interim_target", 85))}
    n = int(v.get("register_issuers") or 0)
    gaps: list[Gap] = []
    for cell, tgt in targets.items():
        pct = float((v.get("pct") or {}).get(cell) or 0.0)
        have = int(v.get(f"with_{cell}") or 0)
        st.entities[cell] = {"pct": pct, "target": tgt, "issuers": n, "with": have}
        if pct < tgt:
            top = ((v.get("top_notes") or {}).get(cell) or [[None, 0]])[0]
            gaps.append(Gap(spec["id"], f"{mk}.{cell}", "partial", None, None, n, have,
                            note=f"{pct:.1f} % of {n} register issuers (target {tgt:.0f} %); top reason: {top[0]}"[:300],
                            key=cell))
    st.entities["universe"] = {"pct": (v.get("pct") or {}).get("universe"), "issuers": n}
    as_of = date.fromisoformat(js["as_of"]) if js.get("as_of") else None
    st.data_as_of, st.expected_as_of = as_of, cx.today
    sla = effective_sla(spec, cx.profile_sla_days)
    if as_of is None or (cx.today - as_of).days > sla:
        gaps.append(Gap(spec["id"], mk, "stale", as_of, cx.today, None, None, key="report",
                        note=f"coverage report of {as_of} older than the SLA ({sla:g} d)",
                        fill_kind="job", fill_params={"jobs": [{"job": "regulatory_coverage"}]}))
    st.status = "partial" if any(g.gap_class == "partial" for g in gaps) else ("stale" if gaps else "fresh")
    st.detail = (f"{n} register issuers: annual {v['pct']['annual']:.1f} %, interim {v['pct']['interim']:.1f} %, "
                 f"universe {v['pct']['universe']:.1f} %")
    return gaps


@check("conformal_coverage")
def _check_conformal_coverage(cx: Ctx, spec: dict[str, Any], st: DatasetStatus) -> list[Gap]:
    """Conformal coverage audit (docs/CONFORMAL_TECHNICALS_PLAN.md P2): freshness of the latest audit plus
    under-coverage of the rolling audit (overall joint coverage more than `overall_tol_pp` under target, or a
    region / sector / regime / VIX segment more than `segment_tol_pp` under, n >= 200). Model issues, not data
    gaps the filler can repair: status manual."""
    table, dcol = spec.get("table", "conformal_coverage"), spec.get("date_column", "as_of_date")
    src = cx.src(table)
    if src is None:
        st.status = "missing"
        return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata", fill_kind="dag")]
    ent = spec.get("entities") or {}
    tol_o, tol_s = float(ent.get("overall_tol_pp", 1.5)), float(ent.get("segment_tol_pp", 5.0))
    min_seg_n = int(ent.get("min_segment_n", 2000))
    rows = cx.q(f"""
        WITH c AS (SELECT * FROM {src} WHERE application NOT LIKE '%_wf'),
             l AS (SELECT application, max(CAST("{dcol}" AS DATE)) AS d FROM c GROUP BY 1)
        SELECT c.application, c.filter, c.segment_type, c.segment, c.level, c.n, c.coverage, c.gap_pp,
               CAST(c."{dcol}" AS DATE)
        FROM c JOIN l ON c.application = l.application AND CAST(c."{dcol}" AS DATE) = l.d
        WHERE c.horizon = 'joint' AND c.n >= 200""")
    last = max((r[8] for r in rows), default=None)
    mic = spec.get("calendar")
    exp = _expected_latest(mic if mic != "weekdays" else "weekdays", cx.today, cx.settings)
    sla = effective_sla(spec, cx.profile_sla_days)
    lag = _sessions_between(mic, last, exp, cx.settings) if (last and exp) else None
    st.data_as_of, st.expected_as_of, st.sla, st.lag = last, exp, sla, lag
    gaps: list[Gap] = []
    if last is None or (lag is not None and lag > sla):
        st.status = "stale" if last else "missing"
        gaps.append(Gap(spec["id"], "*", "stale", None, exp, lag, 0, note=f"coverage audit through {last}",
                        key="stale", fill_kind="dag"))
    under = []
    parts = []
    for app, flt, stype, seg, lvl, n, cov, gap, _d in rows:
        if stype == "overall" and str(lvl) == "90":
            parts.append(f"{app} {flt} 90%: {100 * cov:.1f}%")
            st.entities[f"{app}:{flt}"] = {"coverage_90": round(float(cov), 4), "n": int(n)}
        tol = tol_o if stype == "overall" else tol_s
        if stype != "overall" and n < min_seg_n:      # thin segments (a handful of securities) are noise
            continue
        if gap is not None and gap < -tol and stype in ("overall", "region", "sector", "regime", "vix"):
            under.append((app, flt, stype, seg, lvl, n, cov, gap))
    for app, flt, stype, seg, lvl, n, cov, gap in under:
        gaps.append(Gap(spec["id"], f"{app}:{seg}", "suspicious", None, last, int(n), None,
                        note=f"{app} {flt} {stype}={seg}: realised {100 * cov:.1f}% vs target {lvl}% ({gap:+.1f} pp, n={n})",
                        status="manual", key=f"under:{app}:{flt}:{stype}:{seg}:{lvl}"))
    if st.status not in ("stale", "missing"):
        st.status = "partial" if under else "fresh"
    st.detail = (" | ".join(sorted(parts)) or "no audit rows") + (f" | {len(under)} under-covered segment(s)" if under else "")
    return gaps


# ------------------------------------------------------------------------------------------------ seeds


# W3 rates & debt (owner rates-w3): check `published_dates` (official publication schedules) lives in app.rates
import app.rates.ops_checks  # noqa: E402,F401  (registers CHECKS["published_dates"])


def _seed_gaps(reg: dict[str, Any], detected: list[Gap], settings: Settings) -> list[Gap]:
    det_ids = {(g.dataset, g.period_start) for g in detected}
    gstate = None
    out = []
    for k in reg.get("known_gaps") or []:
        ds = reg["datasets"].get(k["dataset"], {})
        g = Gap(k["dataset"], str(k.get("entity", "*")), k["gap_class"],
                date.fromisoformat(str(k["start"])) if k.get("start") else None,
                date.fromisoformat(str(k["end"])) if k.get("end") else None,
                note=k.get("note"), origin="seed", key=str(k["id"]))
        g.key = k["id"]
        fill = k.get("fill")
        if fill:
            g.fill_kind = fill.get("kind")
            g.fill_params = {x: v for x, v in fill.items() if x != "kind"}
        if (k.get("cost") or {}).get("source"):
            g.source, g.cost_units = k["cost"]["source"], float(k["cost"].get("units") or 1)
        if k.get("manual"):
            g.status = "manual"
        if k["gap_class"] == "unfillable":
            g.status = "unfillable"  # recorded so nothing retries it (the note says what would fix it)
        rule = k.get("resolve")
        if rule == "gdelt_day_complete":
            gstate = gstate if gstate is not None else _gdelt_state(settings)
            rec = (gstate.get("days") or {}).get(str(k.get("start")))
            if isinstance(rec, dict) and int(rec.get("files_missing") or 0) == 0 and int(rec.get("files_ok") or 0) > 0:
                g.status = "filled"
                g.note = (g.note or "") + " | resolved: day now has all files"
        elif rule == "detector":
            if (k["dataset"], g.period_start) not in det_ids:
                g.status = "filled"
                g.note = (g.note or "") + " | resolved: no longer detected"
            else:
                g.status = "ignored"  # the detector row for the same period carries the work
        g.priority = int(k.get("priority", ds.get("priority", 50)))
        out.append(g)
    return out


# ------------------------------------------------------------------------------------------------ store


def gaps_lock(settings: Settings | None = None) -> FileLock:
    s = settings or get_settings()
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(s.locks_dir / "data_gaps_rw.lock"), timeout=120)


def read_gaps(settings: Settings | None = None) -> pl.DataFrame:
    from app.core.schema import empty_frame

    try:
        return read_table("data_gaps", settings=settings)
    except Exception:
        return empty_frame("data_gaps")


def _fill_defaults(spec: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None, float | None]:
    fill = spec.get("fill")
    cost = spec.get("cost") or {}
    if not fill:
        return "dag" if spec.get("live_job") else "none", None, cost.get("source"), None
    params = {k: v for k, v in fill.items() if k != "kind"}
    units = cost.get("units")
    return fill.get("kind"), params or None, cost.get("source"), (float(units) if isinstance(units, (int, float)) else None)


def merge_gaps(existing: pl.DataFrame, found: list[Gap], checked: set[str], reg: dict[str, Any], run_id: str | None,
               now: datetime | None = None) -> pl.DataFrame:
    """New table state: detected rows upserted (filler fields kept), undetected open rows of checked datasets
    resolved -> filled. Pure function (unit-tested)."""
    now = now or datetime.now(UTC)
    old = {r["gap_id"]: r for r in existing.iter_rows(named=True)} if not existing.is_empty() else {}
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for g in found:
        spec = reg["datasets"].get(g.dataset, {})
        fk, fp, src, units = _fill_defaults(spec)
        gid = g.gap_id
        seen.add(gid)
        prev = old.get(gid)
        row = {
            "gap_id": gid, "dataset": g.dataset, "gap_group": spec.get("group"), "entity": g.entity,
            "gap_class": g.gap_class, "period_start": g.period_start, "period_end": g.period_end,
            "expected": g.expected, "actual": g.actual,
            "priority": int(g.priority if g.priority is not None else spec.get("priority", 50)),
            "fill_kind": g.fill_kind or fk, "fill_params": json.dumps(g.fill_params if g.fill_params is not None else fp, default=str) if (g.fill_params or fp) else None,
            "source": g.source or src, "cost_units": g.cost_units if g.cost_units is not None else units,
            "note": g.note, "origin": g.origin, "last_seen": now, "detector_run_id": run_id, "updated_at": now,
        }
        if prev is None:
            row.update(first_seen=now, attempts=0, last_attempt_at=None, next_attempt_at=None, last_error=None,
                       resolved_at=None, status=g.status or ("manual" if row["fill_kind"] in ("none",) else "open"))
        else:
            row.update(first_seen=prev["first_seen"], attempts=prev["attempts"], last_attempt_at=prev["last_attempt_at"],
                       next_attempt_at=prev["next_attempt_at"], last_error=prev["last_error"])
            pst = prev["status"]
            if g.status in ("manual", "ignored", "filled"):
                st = g.status
            elif pst in ("unfillable", "manual", "ignored", "queued", "filling", "failed"):
                st = pst
            elif g.status == "unfillable":
                st = "unfillable"
            else:
                st = "open"  # open, or re-opened after being filled
            row["status"] = st
            row["resolved_at"] = now if st == "filled" else (None if st in OPEN_STATUSES else prev["resolved_at"])
        if row["status"] == "filled" and row.get("resolved_at") is None:
            row["resolved_at"] = now
        if row["fill_kind"] == "none" and row["status"] == "open":
            row["status"] = "manual"
        out[gid] = row
    for gid, prev in old.items():
        if gid in out:
            continue
        r = dict(prev)
        if r["origin"] == "detector" and r["dataset"] in checked and r["status"] in OPEN_STATUSES + ("manual",):
            r.update(status="filled", resolved_at=now, updated_at=now)
        out[gid] = r
    from app.core.schema import get_contract

    c = get_contract("data_gaps")
    return pl.DataFrame(list(out.values()), schema=c.polars_schema()) if out else pl.DataFrame(schema=c.polars_schema())


@dataclass
class DetectReport:
    run_id: str | None
    gaps: int
    open: int
    by_dataset: dict[str, dict[str, int]]
    statuses: dict[str, dict[str, Any]]
    errors: dict[str, str]
    seconds: float

    def summary(self) -> str:
        return (f"data_gaps: {self.gaps} rows, {self.open} open; datasets {len(self.statuses)}; "
                f"errors {list(self.errors)}; {self.seconds:.1f}s")


def detect(settings: Settings | None = None, datasets: list[str] | None = None, run_id: str | None = None,
           write: bool = True, now: datetime | None = None, profile_sla_days: Any = "auto") -> DetectReport:
    """profile_sla_days: "auto" = the active schedule profile's sla_days; None = daily semantics (dataset SLA only)."""
    s = settings or get_settings()
    t0 = time.perf_counter()
    now = now or datetime.now(UTC)
    today = now.date()
    reg = load_coverage(s)
    if profile_sla_days == "auto":
        profile_sla_days = None
        try:
            from app.ops.schedule import active_profile, profile_spec

            profile_sla_days = float(profile_spec(active_profile(s)[0], s).get("sla_days") or 0) or None
        except Exception:
            profile_sla_days = None
    con = duckdb_connect(None, settings=s)
    con.execute("SET memory_limit = '1GB'")
    con.execute("SET threads = 4")
    con.execute("SET enable_progress_bar = false")
    con.execute("SET TimeZone = 'UTC'")
    cx = Ctx(s, con, today, now, profile_sla_days, reg)
    found: list[Gap] = []
    statuses: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    checked: set[str] = set()
    try:
        for did, spec in reg["datasets"].items():
            if datasets and did not in datasets:
                continue
            st = DatasetStatus(did, spec.get("label", did), spec.get("group", "other"))
            fn = CHECKS.get(spec.get("check"))
            if fn is None:
                continue
            try:
                g = fn(cx, spec, st)
                for x in g:
                    x.priority = x.priority if x.priority is not None else int(spec.get("priority", 50))
                found.extend(g)
                checked.add(did)
            except Exception as e:  # one broken check never stops the others
                errors[did] = f"{type(e).__name__}: {e}"[:300]
                st.status, st.detail = "error", errors[did]
                log.warning("gap check %s failed: %s\n%s", did, e, traceback.format_exc())
            statuses[did] = st.to_dict()
    finally:
        con.close()
    found.extend(_seed_gaps(reg, found, s))
    if datasets is None:
        checked |= {g.dataset for g in found}
    by_ds: dict[str, dict[str, int]] = {}
    with gaps_lock(s):
        new = merge_gaps(read_gaps(s), found, checked, reg, run_id, now)
        if write:
            write_table(new, "data_gaps", mode="overwrite", settings=s)
    for r in new.iter_rows(named=True):
        d = by_ds.setdefault(r["dataset"], {})
        d[r["status"]] = d.get(r["status"], 0) + 1
    n_open = int(new.filter(pl.col("status").is_in(list(OPEN_STATUSES))).height) if new.height else 0
    rep = DetectReport(run_id, new.height, n_open, by_ds, statuses, errors, round(time.perf_counter() - t0, 2))
    if write:
        prev = read_json(ops_state_dir(s) / "coverage_status.json", {}) or {}
        merged = dict(prev.get("datasets") or {}) if datasets else {}
        merged.update(statuses)
        write_json_atomic(ops_state_dir(s) / "coverage_status.json", {
            "generated_at": now.isoformat(timespec="seconds"), "run_id": run_id, "profile_sla_days": profile_sla_days,
            "datasets": merged, "errors": errors, "seconds": rep.seconds})
    log.info(rep.summary())
    return rep


def coverage_status(settings: Settings | None = None) -> dict[str, Any]:
    return read_json(ops_state_dir(settings) / "coverage_status.json", {}) or {}


def gap_fingerprint(gap_id: str) -> str:
    return hashlib.sha1(gap_id.encode()).hexdigest()[:10]


# ------------------------------------------------------------------------------------------------ job entry


def run(ctx: Any) -> Any:
    """jobs.yaml `data_gaps_detect`. params: datasets (list) to limit the run."""
    from app.scheduler.base import JobResult

    p = ctx.params or {}
    ds = p.get("datasets")
    rep = detect(ctx.settings, datasets=list(ds) if ds else None, run_id=ctx.run_id)
    status = "partial" if rep.errors else "success"
    return JobResult(status=status, rows_written={"data_gaps": rep.gaps}, entities_ok=len(rep.statuses) - len(rep.errors),
                     entities_failed=len(rep.errors), message=rep.summary()[:2000])


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="BiG Equity 360 gap detector")
    ap.add_argument("--dataset", action="append")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    from app.core.logsetup import setup_logging

    setup_logging(log_file="ops.log")
    r = detect(datasets=a.dataset, write=not a.dry_run)
    print(r.summary())
    for k, v in r.statuses.items():
        print(f"  {k:24s} {v['status']:8s} as_of={v['data_as_of']} exp={v['expected_as_of']} lag={v['lag']} {v['detail']}")
    for k, v in r.by_dataset.items():
        print(f"  gaps {k:24s} {v}")
