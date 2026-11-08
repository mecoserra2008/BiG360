"""API observability (O4 Stage A): request ids, JSON access log, Prometheus metrics, readiness.

* `RequestContextMiddleware` (pure ASGI, outermost): takes `X-Request-ID` from the request when it is a safe token
  (<= 128 chars of [A-Za-z0-9._:-]) or makes one (32 hex chars), stores it in `app.core.logsetup.request_id_var` so
  every log line of the request carries it, echoes it in the `X-Request-ID` response header, counts the request and
  its latency per *route template* (never the raw URL: bounded label cardinality) and writes one access record to
  logger `app.api.access` (JSON in data/logs/api.log; probes at DEBUG). Server-Sent Events responses are counted as
  connections (gauge) and kept out of the latency histogram.
* `METRICS` - a tiny thread-safe registry (counters + histograms). No prometheus_client dependency.
* `render_metrics()` - Prometheus text exposition format 0.0.4: HTTP metrics plus cheap state gauges read from files
  the processes already write (serving snapshot, worker heartbeat, filler state, gap queue, coverage status, job_runs).
  The state part is cached for `STATE_TTL_S` so a 15 s scrape interval costs nothing measurable.
* `readiness()` - GET /ready: 200 when a serving snapshot is published and (unless BIG360_READY_REQUIRE_WORKER=0) the
  worker heartbeat is fresh; 503 otherwise. GET /health stays the liveness probe (200 while the process serves).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import request_id_var

access_log = logging.getLogger("app.api.access")
log = logging.getLogger("app.api.observability")

_RID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
PROBE_PATHS = frozenset({"/health", "/ready", "/metrics"})
STATE_TTL_S = 15.0


# ------------------------------------------------------------------------------------------------ registry
class MetricsRegistry:
    """Counters and fixed-bucket histograms keyed by label tuples. Cheap: one lock, dict updates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, str], int] = {}
        self.hist: dict[tuple[str, str], list[float]] = {}  # (method, route) -> [bucket counts..., +Inf, sum]
        self.in_flight = 0
        self.sse_open = 0
        self.sse_total = 0
        self.started = time.time()

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.hist.clear()
            self.in_flight = self.sse_open = self.sse_total = 0

    def begin(self) -> None:
        with self._lock:
            self.in_flight += 1

    def end(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def sse(self, delta: int) -> None:
        with self._lock:
            self.sse_open += delta
            if delta > 0:
                self.sse_total += 1

    def observe(self, method: str, route: str, status: int, seconds: float | None) -> None:
        key = (method, route, str(status))
        with self._lock:
            self.requests[key] = self.requests.get(key, 0) + 1
            if seconds is None:
                return
            h = self.hist.get((method, route))
            if h is None:
                h = self.hist[(method, route)] = [0.0] * (len(LATENCY_BUCKETS) + 2)
            for i, b in enumerate(LATENCY_BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[len(LATENCY_BUCKETS)] += 1      # +Inf (= count)
            h[len(LATENCY_BUCKETS) + 1] += seconds  # sum

    def snapshot(self) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], list[float]], int, int, int]:
        with self._lock:
            return (dict(self.requests), {k: list(v) for k, v in self.hist.items()}, self.in_flight, self.sse_open,
                    self.sse_total)


METRICS = MetricsRegistry()


_tpl_lock = threading.Lock()
_templates: list[tuple[re.Pattern[str], str]] | None = None


def _contract_templates() -> list[tuple[re.Pattern[str], str]]:
    """Path templates of contracts/api.yaml as regexes, literal paths first (cache hits never reach the router)."""
    global _templates  # noqa: PLW0603
    with _tpl_lock:
        if _templates is None:
            out: list[tuple[re.Pattern[str], str]] = []
            try:
                import yaml  # noqa: PLC0415

                spec = yaml.safe_load((get_settings().contracts_dir / "api.yaml").read_text(encoding="utf-8")) or {}
                paths = sorted(spec.get("paths") or {}, key=lambda x: (x.count("{"), -len(x)))
                for tpl in paths:
                    out.append((re.compile("^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(tpl)) + "$"), tpl))
            except Exception as e:  # noqa: BLE001
                log.debug("metrics: contract templates unavailable: %s", e)
            _templates = out
        return _templates


def route_label(scope: dict[str, Any], status: int) -> str:
    """Route template (e.g. /api/v1/companies/{security_id}/overview), never the raw URL; bounded fallbacks."""
    p = scope.get("path", "")
    for rx, tpl in _contract_templates():
        if rx.match(p):
            return tpl
    route = scope.get("route")
    rpath = getattr(route, "path", None) if route is not None else None
    if rpath and status != 404:
        rpath = re.sub(r":\w+}", "}", rpath)
        if p.startswith("/api/") and not rpath.startswith("/api/"):  # included router: template lacks the prefix
            n = rpath.strip("/").count("/") + 1
            segs = p.strip("/").split("/")
            rpath = "/" + "/".join(segs[: max(0, len(segs) - n)]) + rpath
        return rpath if "{" in rpath or rpath.count("/") < 8 else "other"
    if status == 404:
        return "unmatched"
    if not p.startswith("/api/"):
        return "static"  # built UI assets / SPA fallback (app mode)
    return "other"


def _request_id(scope: dict[str, Any]) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"x-request-id":
            cand = v.decode("latin-1").strip()
            if _RID_RE.match(cand):
                return cand
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Pure ASGI middleware: request id + metrics + access log. See module docstring."""

    def __init__(self, app: Any, access: bool = True):
        self.app = app
        self.access = access

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = _request_id(scope)
        token = request_id_var.set(rid)
        state: dict[str, Any] = {"status": 500, "sse": False, "bytes": 0}
        t0 = time.perf_counter()
        METRICS.begin()

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message["status"])
                headers = list(message.get("headers") or [])
                for k, v in headers:
                    if k.lower() == b"content-type" and v.startswith(b"text/event-stream"):
                        state["sse"] = True
                        METRICS.sse(+1)
                        break
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dt = time.perf_counter() - t0
            METRICS.end()
            if state["sse"]:
                METRICS.sse(-1)
            route = route_label(scope, state["status"])
            method = scope.get("method", "GET")
            METRICS.observe(method, route, state["status"], None if state["sse"] else dt)
            if self.access:
                path = scope.get("path", "")
                level = logging.DEBUG if path in PROBE_PATHS else logging.INFO
                if access_log.isEnabledFor(level):
                    client = scope.get("client")
                    access_log.log(level, "%s %s %s %.1fms", method, path, state["status"], dt * 1000.0,
                                   extra={"fields": {"method": method, "path": path, "route": route,
                                                     "status": state["status"], "duration_ms": round(dt * 1000.0, 2),
                                                     "bytes": state["bytes"], "sse": state["sse"],
                                                     "client": client[0] if client else None,
                                                     "principal": (scope.get("state") or {}).get("principal_name")}})
            request_id_var.reset(token)


# ------------------------------------------------------------------------------------------------ state gauges
_state_lock = threading.Lock()
_state_cache: dict[str, Any] = {"at": 0.0, "lines": []}


def _esc(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kw: Any) -> str:
    items = [f'{k}="{_esc(v)}"' for k, v in kw.items() if v is not None]
    return "{" + ",".join(items) + "}" if items else ""


def _num(v: Any) -> str:
    if v is None:
        return "NaN"
    f = float(v)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    return repr(f) if not f.is_integer() else str(int(f)) if abs(f) < 1e15 else repr(f)


def _aware(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.declared: set[str] = set()

    def metric(self, name: str, kind: str, help_: str, samples: list[tuple[dict[str, Any], Any]]) -> None:
        if name not in self.declared:
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {kind}")
            self.declared.add(name)
        for lab, val in samples:
            self.lines.append(f"{name}{_labels(**lab)} {_num(val)}")


def _collect_state(s: Settings) -> list[str]:
    out = _Out()
    now = datetime.now(UTC)
    # serving snapshot
    try:
        from app.api.deps import current_snapshot  # noqa: PLC0415

        snap = current_snapshot()
        out.metric("big360_serving_snapshot_present", "gauge", "1 when a serving snapshot is published",
                   [({}, 0 if snap.is_empty else 1)])
        if not snap.is_empty:
            out.metric("big360_serving_snapshot_info", "gauge", "Current serving snapshot (value 1)",
                       [({"run_id": snap.run_id, "as_of": snap.as_of.isoformat()}, 1)])
            if snap.built_at:
                out.metric("big360_serving_snapshot_age_seconds", "gauge", "Seconds since the snapshot was built",
                           [({}, round((now - _aware(snap.built_at)).total_seconds(), 1))])  # type: ignore[operator]
            out.metric("big360_serving_as_of_timestamp_seconds", "gauge", "Snapshot as-of date (unix seconds, 00:00 UTC)",
                       [({}, _aware(snap.as_of).timestamp())])  # type: ignore[union-attr]
            try:
                cur = snap.con.cursor()
                try:
                    rows = cur.execute("SELECT table_name, region, last_date, row_count FROM _freshness").fetchall()
                finally:
                    cur.close()
                lag, cnt = [], []
                for t, region, last, n in rows:
                    lab = {"table": t, "region": region or "ALL"}
                    d = _aware(last)
                    if d is not None:
                        lag.append((lab, round((now - d).total_seconds() / 86400.0, 2)))
                    if n is not None:
                        cnt.append((lab, n))
                out.metric("big360_data_age_days", "gauge", "Days since the latest data date per table / region "
                           "(snapshot _freshness)", lag)
                out.metric("big360_data_rows", "gauge", "Rows per table / region in the snapshot", cnt)
            except Exception as e:  # noqa: BLE001 - older snapshots without _freshness
                log.debug("metrics: _freshness unavailable: %s", e)
    except Exception as e:  # noqa: BLE001
        log.debug("metrics: snapshot unavailable: %s", e)
    # coverage registry status (gap detector output)
    try:
        from app.ops.paths import ops_state_dir, read_json  # noqa: PLC0415

        cov = read_json(ops_state_dir(s) / "coverage_status.json", {}) or {}
        ds = cov.get("datasets") or {}
        if ds:
            st = []
            lags = []
            for did, x in sorted(ds.items()):
                st.append(({"dataset": did, "status": x.get("status") or "unknown"}, 1))
                if x.get("lag") is not None:
                    lags.append(({"dataset": did}, x.get("lag")))
            out.metric("big360_dataset_status", "gauge", "Coverage-registry dataset status (value 1; label status = "
                       "fresh | stale | partial | missing | ...)", st)
            out.metric("big360_dataset_lag_periods", "gauge", "Dataset lag vs its expected as-of (periods of its "
                       "calendar)", lags)
        # filler
        fl = read_json(ops_state_dir(s) / "filler.json", {}) or {}
        hb = _aware(fl.get("heartbeat_at"))
        age = (now - hb).total_seconds() if hb else None
        stopped = _aware(fl.get("stopped_at"))
        alive = age is not None and age < 120 and not (stopped is not None and hb is not None and stopped >= hb)
        out.metric("big360_filler_up", "gauge", "1 when the gap filler heartbeat is fresh (< 120 s)", [({}, 1 if alive else 0)])
        if age is not None:
            out.metric("big360_filler_heartbeat_age_seconds", "gauge", "Seconds since the filler heartbeat", [({}, round(age, 1))])
        gov = fl.get("governor") or {}
        if gov.get("state"):
            out.metric("big360_filler_governor_state", "gauge", "Filler resource-governor state (value 1)",
                       [({"state": gov.get("state")}, 1)])
        out.metric("big360_filler_running_tasks", "gauge", "Gap-filler tasks running", [({}, len(fl.get("running") or {}))])
        camps = []
        for cid, c in sorted((fl.get("campaign_status") or {}).items()):
            pr = c.get("progress") or {}
            if pr.get("pct") is not None:
                camps.append(({"campaign": cid}, round(float(pr["pct"]) / 100.0, 4)))
        out.metric("big360_campaign_progress_ratio", "gauge", "Backfill campaign progress 0..1", camps)
    except Exception as e:  # noqa: BLE001
        log.debug("metrics: ops state unavailable: %s", e)
    # gap queue
    try:
        from app.ops.gaps import read_gaps  # noqa: PLC0415

        g = read_gaps(s)
        if g.height:
            agg = g.group_by(["status", "gap_class"]).len().sort(["status", "gap_class"])
            out.metric("big360_data_gaps", "gauge", "Gap-queue rows by status and class",
                       [({"status": r["status"], "class": r["gap_class"]}, r["len"]) for r in agg.iter_rows(named=True)])
        else:
            out.metric("big360_data_gaps", "gauge", "Gap-queue rows by status and class", [])
    except Exception as e:  # noqa: BLE001
        log.debug("metrics: gaps unavailable: %s", e)
    # live news service (roadmap v3 N2.5): publish -> bus latency per source, items / hour, GDELT release lag
    try:
        from app.realtime.supervisor import read_status  # noqa: PLC0415

        rt = read_status(s) or {}
        hbt = _aware(rt.get("heartbeat_at"))
        up = bool(rt.get("alive")) and hbt is not None and (now - hbt).total_seconds() < 60
        out.metric("big360_live_news_up", "gauge", "1 when the realtime / live news service heartbeat is fresh",
                   [({}, 1 if up else 0)])
        desk = (rt.get("metrics") or {}).get("newsdesk") or {}
        lat, cnt = [], []
        for src, x in (desk.get("sources") or {}).items():
            for q, k in (("0.5", "publish_to_bus_s_p50"), ("0.95", "publish_to_bus_s_p95")):
                if x.get(k) is not None:
                    lat.append(({"source": src, "quantile": q}, x[k]))
            cnt.append(({"source": src}, int(x.get("items_1h") or 0)))
        ov = desk.get("overall") or {}
        for q, k in (("0.5", "publish_to_bus_s_p50"), ("0.95", "publish_to_bus_s_p95")):
            if ov.get(k) is not None:
                lat.append(({"source": "all", "quantile": q}, ov[k]))
        out.metric("big360_news_publish_to_bus_seconds", "gauge",
                   "Live news latency: news.item publish time - source publication time (quantiles over recent items)", lat)
        out.metric("big360_news_items_last_hour", "gauge", "Live news items published in the last hour by source", cnt)
        gd = (rt.get("metrics") or {}).get("gdelt") or {}
        gl = [({"quantile": q}, gd[k]) for q, k in (("0.5", "release_lag_s_p50"), ("0.95", "release_lag_s_p95"))
              if gd.get(k) is not None]
        out.metric("big360_gdelt_release_to_hot_seconds", "gauge",
                   "GDELT 15-min file publication -> parsed into the hot store (quantiles)", gl)
        if rt.get("rss_mb") is not None:
            out.metric("big360_live_news_resident_memory_bytes", "gauge", "Realtime service process tree memory",
                       [({}, int(float(rt["rss_mb"]) * 1024 * 1024))])
    except Exception as e:  # noqa: BLE001
        log.debug("metrics: live news status unavailable: %s", e)
    # worker / scheduler
    try:
        from app.scheduler.worker import read_heartbeat  # noqa: PLC0415

        hb = read_heartbeat(s) or {}
        t = _aware(hb.get("heartbeat_at"))
        out.metric("big360_worker_up", "gauge", "1 when the worker holds its lock", [({}, 1 if hb.get("alive") else 0)])
        if t is not None:
            out.metric("big360_worker_heartbeat_age_seconds", "gauge", "Seconds since the worker heartbeat",
                       [({}, round((now - t).total_seconds(), 1))])
        out.metric("big360_worker_active_dags", "gauge", "DAG runs in progress", [({}, len(hb.get("active") or []))])
    except Exception as e:  # noqa: BLE001
        log.debug("metrics: worker heartbeat unavailable: %s", e)
    # job outcomes (job_runs, last 7 days) + last success per job
    try:
        import polars as pl  # noqa: PLC0415

        from app.api.routers.system import _job_runs  # noqa: PLC0415  (3 s cached reader)

        runs = _job_runs()
        if runs.height:
            since: Any = now - timedelta(days=7)
            if not getattr(runs["started_at"].dtype, "time_zone", None):
                since = since.replace(tzinfo=None)
            recent = runs.filter(pl.col("started_at") >= since)
            agg = recent.group_by(["job_id", "status"]).len().sort(["job_id", "status"])
            out.metric("big360_job_runs_7d", "gauge", "Job runs started in the last 7 days by outcome",
                       [({"job_id": r["job_id"], "status": r["status"]}, r["len"]) for r in agg.iter_rows(named=True)])
            ok = (runs.filter(pl.col("status").is_in(["success", "partial"]))
                  .group_by("job_id").agg(pl.col("finished_at").max().alias("t"),
                                          pl.col("duration_s").sort_by("finished_at").last().alias("d")).sort("job_id"))
            last_ok, last_d = [], []
            for r in ok.iter_rows(named=True):
                t = _aware(r["t"])
                if t is not None:
                    last_ok.append(({"job_id": r["job_id"]}, round(t.timestamp(), 0)))
                if r["d"] is not None:
                    last_d.append(({"job_id": r["job_id"]}, round(float(r["d"]), 1)))
            out.metric("big360_job_last_success_timestamp_seconds", "gauge", "Finish time of the last successful (or "
                       "partial) run per job", last_ok)
            out.metric("big360_job_last_success_duration_seconds", "gauge", "Duration of that run", last_d)
            last = runs.sort("started_at").group_by("job_id").agg(pl.col("status").last()).sort("job_id")
            out.metric("big360_job_last_status", "gauge", "Status of the latest run per job (value 1)",
                       [({"job_id": r["job_id"], "status": r["status"]}, 1) for r in last.iter_rows(named=True)])
    except Exception as e:  # noqa: BLE001
        log.debug("metrics: job_runs unavailable: %s", e)
    return out.lines


def _state_lines(s: Settings) -> list[str]:
    with _state_lock:
        if time.monotonic() - _state_cache["at"] < STATE_TTL_S and _state_cache.get("dir") == str(s.data_dir):
            return _state_cache["lines"]
    lines = _collect_state(s)
    with _state_lock:
        _state_cache.update(at=time.monotonic(), lines=lines, dir=str(s.data_dir))
    return lines


def reset_state_cache() -> None:
    with _state_lock:
        _state_cache.update(at=0.0, lines=[], dir=None)


def _process_lines() -> list[str]:
    out = _Out()
    out.metric("big360_process_start_time_seconds", "gauge", "API process start (unix seconds)", [({}, round(METRICS.started, 0))])
    try:
        import os  # noqa: PLC0415

        from app.ops import winsys  # noqa: PLC0415

        mb = winsys.process_memory_mb(os.getpid())
        if mb is not None:
            out.metric("big360_process_resident_memory_bytes", "gauge", "API working set", [({}, int(mb * 1024 * 1024))])
    except Exception:  # noqa: BLE001
        pass
    try:
        t = time.process_time()
        out.metric("big360_process_cpu_seconds_total", "counter", "API process CPU time", [({}, round(t, 3))])
    except Exception:  # noqa: BLE001
        pass
    return out.lines


def render_metrics(settings: Settings | None = None, api_version: str = "", contract_version: str = "") -> str:
    s = settings or get_settings()
    req, hist, in_flight, sse_open, sse_total = METRICS.snapshot()
    out = _Out()
    out.metric("big360_build_info", "gauge", "Build information (value 1)",
               [({"version": api_version, "contract_version": contract_version}, 1)])
    out.metric("big360_http_requests_total", "counter", "HTTP requests by method, route template and status",
               [({"method": m, "route": r, "status": st}, n) for (m, r, st), n in sorted(req.items())])
    name = "big360_http_request_duration_seconds"
    out.lines.append(f"# HELP {name} HTTP request latency by method and route template (SSE excluded)")
    out.lines.append(f"# TYPE {name} histogram")
    for (m, r), h in sorted(hist.items()):
        for i, b in enumerate(LATENCY_BUCKETS):
            out.lines.append(f"{name}_bucket{_labels(method=m, route=r, le=_num(b))} {_num(h[i])}")
        out.lines.append(f"{name}_bucket{_labels(method=m, route=r, le='+Inf')} {_num(h[len(LATENCY_BUCKETS)])}")
        out.lines.append(f"{name}_sum{_labels(method=m, route=r)} {round(h[len(LATENCY_BUCKETS) + 1], 6)}")
        out.lines.append(f"{name}_count{_labels(method=m, route=r)} {_num(h[len(LATENCY_BUCKETS)])}")
    out.metric("big360_http_requests_in_flight", "gauge", "Requests being served (incl. open SSE streams)", [({}, in_flight)])
    out.metric("big360_sse_connections", "gauge", "Open Server-Sent Events streams (/api/v1/live/stream)", [({}, sse_open)])
    out.metric("big360_sse_connections_total", "counter", "SSE streams opened since start", [({}, sse_total)])
    lines = out.lines + _process_lines() + _state_lines(s)
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ readiness
def readiness(settings: Settings | None = None) -> tuple[bool, dict[str, Any]]:
    s = settings or get_settings()
    now = datetime.now(UTC)
    checks: list[dict[str, Any]] = []
    from app.api.deps import current_snapshot  # noqa: PLC0415

    snap = current_snapshot()
    checks.append({"name": "serving_snapshot", "required": True, "ok": not snap.is_empty,
                   "detail": (f"{snap.run_id} as of {snap.as_of.isoformat()}" if not snap.is_empty
                              else "no serving snapshot published (data/db/CURRENT)")})
    hb: dict[str, Any] = {}
    try:
        from app.scheduler.worker import read_heartbeat  # noqa: PLC0415

        hb = read_heartbeat(s) or {}
    except Exception as e:  # noqa: BLE001
        hb = {"error": str(e)[:200]}
    t = _aware(hb.get("heartbeat_at"))
    age = round((now - t).total_seconds(), 1) if t else None
    w_ok = bool(hb.get("alive")) and age is not None and age <= s.ready_worker_max_age_s
    checks.append({"name": "worker_heartbeat", "required": bool(s.ready_require_worker), "ok": w_ok,
                   "detail": ("alive, heartbeat %ss ago" % age) if w_ok else
                   ("no worker heartbeat" if t is None else f"worker {'alive' if hb.get('alive') else 'not running'}, "
                    f"heartbeat {age}s ago (max {s.ready_worker_max_age_s:g}s)")})
    try:
        from app.ops.paths import ops_state_dir, read_json  # noqa: PLC0415

        fl = read_json(ops_state_dir(s) / "filler.json", {}) or {}
        ft = _aware(fl.get("heartbeat_at"))
        fage = round((now - ft).total_seconds(), 1) if ft else None
        checks.append({"name": "gap_filler", "required": False, "ok": fage is not None and fage < 120,
                       "detail": "no filler heartbeat" if fage is None else f"heartbeat {fage}s ago"})
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "gap_filler", "required": False, "ok": False, "detail": str(e)[:200]})
    ready = all(c["ok"] for c in checks if c["required"])
    return ready, {
        "status": "ready" if ready else "not_ready",
        "time": now.isoformat(),
        "serving_run_id": None if snap.is_empty else snap.run_id,
        "serving_as_of": None if snap.is_empty else snap.as_of.isoformat(),
        "worker_heartbeat_age_s": age,
        "checks": checks,
    }
