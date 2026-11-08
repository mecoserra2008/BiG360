"""Shared plumbing for the regression guard: API client (live server or in-process), report / state files, alert."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings

STATE_FILE = "qa_regression.json"
HEALTH_TIMEOUT_S = 4.0


# ------------------------------------------------------------------------------------------------ API client


@dataclass
class Resp:
    status: int
    content_type: str
    text: str
    elapsed_ms: float
    error: str | None = None
    _json: Any = field(default=None, repr=False)
    _parsed: bool = field(default=False, repr=False)

    @property
    def json(self) -> Any:
        if not self._parsed:
            self._parsed = True
            try:
                self._json = json.loads(self.text) if self.text else None
            except ValueError:
                self._json = None
        return self._json

    @property
    def is_json(self) -> bool:
        return "json" in (self.content_type or "")


class ApiClient:
    """GET-only client. `base_url` = a running API (http://127.0.0.1:8010) or None for an in-process TestClient over
    the current serving snapshot (used by the job when no API process is up). Never sends non-GET requests."""

    def __init__(self, base_url: str | None, timeout_s: float = 90.0):
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_s = timeout_s
        self._http: Any = None
        self._tc: Any = None
        if self.base_url:
            import httpx

            self._http = httpx.Client(base_url=self.base_url, timeout=timeout_s, headers={"Accept": "application/json"})
        else:
            from fastapi.testclient import TestClient

            from app.api.main import create_app

            self._tc = TestClient(create_app())

    @property
    def mode(self) -> str:
        return "live" if self.base_url else "in_process"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Resp:
        t0 = time.perf_counter()
        try:
            r = (self._http or self._tc).get(path, params=params or None)
            return Resp(r.status_code, r.headers.get("content-type", ""), r.text, (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001 - network error / timeout is a result, not a crash
            return Resp(0, "", "", (time.perf_counter() - t0) * 1000, error=f"{type(e).__name__}: {e}"[:300])

    def close(self) -> None:
        try:
            if self._http is not None:
                self._http.close()
            if self._tc is not None:
                self._tc.close()
        except Exception:  # noqa: BLE001
            pass


def candidate_api_urls(settings: Settings | None = None) -> list[str]:
    s = settings or get_settings()
    env = os.environ.get("BIG360_QA_API_URL")
    out = [env] if env else []
    for port in (s.api_port, 8010, 8000):
        u = f"http://127.0.0.1:{port}"
        if u not in out:
            out.append(u)
    return out


def find_live_api(settings: Settings | None = None) -> str | None:
    """First API base URL whose /health answers (None -> use the in-process client)."""
    import httpx

    for u in candidate_api_urls(settings):
        try:
            r = httpx.get(f"{u}/health", timeout=HEALTH_TIMEOUT_S)
            if r.status_code == 200 and "serving_run_id" in r.text:
                return u
        except Exception:  # noqa: BLE001
            continue
    return None


# ------------------------------------------------------------------------------------------------ JSON helpers


def walk(o: Any, path: str = "$"):  # noqa: ANN201 - generator of (path, key, value)
    """Yield (path, key, value) for every dict entry / list item in a JSON document."""
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}"
            yield p, k, v
            yield from walk(v, p)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            p = f"{path}[{i}]"
            yield p, None, v
            yield from walk(v, p)


def find_key(o: Any, keys: tuple[str, ...]) -> Any:
    """First non-empty scalar value stored under any of `keys` (depth-first, document order)."""
    for _, k, v in walk(o):
        if k in keys and isinstance(v, (str, int)) and str(v):
            return v
    return None


def non_finite(o: Any) -> list[str]:
    """Paths of float NaN / +-inf values (a JSON parser that accepts NaN / Infinity tokens yields them)."""
    return [p for p, _, v in walk(o) if isinstance(v, float) and not math.isfinite(v)]


def parse_date(v: Any) -> date | None:
    if not isinstance(v, str) or len(v) < 10 or v[4] != "-" or v[7] != "-":
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------------ reports / state


def reports_dir(kind: str, settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "reports" / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_stamp(now: datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def state_path(settings: Settings | None = None) -> Path:
    from app.ops.paths import ops_state_dir

    return ops_state_dir(settings) / STATE_FILE


def read_state(settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(settings).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def write_state(summary: dict[str, Any], settings: Settings | None = None) -> None:
    write_json(state_path(settings), summary)


#: alert hygiene 2026-10-30: a suite result older than this is stale (api / golden run after every publishing sync,
#: the UI smoke at most once a day) - its failure no longer raises a warn / error alert, only an info one
STALE_AFTER_H: dict[str, float] = {"api": 26.0, "golden": 26.0, "smoke": 50.0}


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _age_h(t: Any, now: datetime) -> float | None:
    ts = _ts(t)
    return round((now - ts).total_seconds() / 3600, 1) if ts else None


def suite_views(st: dict[str, Any], now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per suite of the merged state: its own run (run_id, finished_at), age_h, superseded_by (a NEWER QA run exists
    that did not re-run this suite) and stale (superseded, or older than STALE_AFTER_H). Legacy state files (one run,
    no per-suite run ids) take the run's id / time."""
    now = now or utc_now()
    latest = st.get("run_id")
    out: dict[str, dict[str, Any]] = {}
    for k, v in (st.get("suites") or {}).items():
        rid = v.get("run_id") or latest
        fin = v.get("finished_at") or st.get("finished_at")
        age = _age_h(fin, now)
        sup = latest if (rid and latest and rid != latest) else None
        old = age is not None and age > STALE_AFTER_H.get(k, 26.0)
        out[k] = {**v, "run_id": rid, "finished_at": fin, "age_h": age, "superseded_by": sup,
                  "stale": bool(sup or old), "expired": bool(old)}
    return out


def merged_status(views: dict[str, dict[str, Any]]) -> str:
    """Overall status of the merged suites: a stale failure counts as `warn` (not current, not a verified pass)."""
    sts = []
    for v in views.values():
        s = v.get("status")
        sts.append("warn" if (v.get("stale") and s in ("failed", "warn")) else s)
    return overall_status(sts)


def qa_alert(settings: Settings | None = None, state: dict[str, Any] | None = None,
             now: datetime | None = None) -> dict[str, Any] | None:
    """Ops alert (OpsAlert shape) for the regression guard (alert hygiene 2026-10-30: results age out).
    * a failing suite of the latest QA run (not older than its STALE_AFTER_H): error (failed) / warn (warnings);
    * a failing suite from an earlier run that a newer QA run did not re-check: warn, marked stale + superseded_by;
    * a failing suite older than its STALE_AFTER_H: info only ("stale result"), with its age;
    * nothing when every suite passed or nothing ran yet. A newer run of the same suite replaces the old result."""
    st = state if state is not None else read_state(settings)
    if not st:
        return None
    now = now or utc_now()
    vs = suite_views(st, now)
    bad = {k: v for k, v in vs.items() if v.get("status") in ("failed", "warn")}
    if not bad:
        return None

    def line(k: str, v: dict[str, Any]) -> str:
        tag = ""
        if v.get("stale"):
            tag = f", stale: {v.get('age_h')} h old" + (f", not re-run by {v['superseded_by']}" if v.get("superseded_by") else "")
        else:
            tag = f", {v.get('age_h')} h ago" if v.get("age_h") is not None else ""
        return f"{k}: {v.get('status')} ({v.get('failed', 0)} failed{tag})"

    cur = {k: v for k, v in bad.items() if not v.get("stale")}
    sup = {k: v for k, v in bad.items() if v.get("stale") and not v.get("expired")}
    detail = "; ".join(line(k, v) for k, v in bad.items())
    oldest = min((v.get("finished_at") for v in bad.values() if v.get("finished_at")), default=st.get("finished_at"))
    age = max((v.get("age_h") or 0 for v in bad.values()), default=None)
    base = {"id": "qa_regression", "detail": (detail or st.get("message") or "")[:600], "since": oldest,
            "link": "/system/data-health", "age_h": age}
    if cur:
        failed = any(v.get("status") == "failed" for v in cur.values())
        return {**base, "severity": "error" if failed else "warn",
                "title": "Regression guard failed" if failed else "Regression guard warnings", "stale": False}
    if sup:
        return {**base, "severity": "warn", "title": "Regression guard: earlier failure not re-checked yet",
                "stale": True, "superseded_by": st.get("run_id")}
    return {**base, "severity": "info", "title": "Regression guard: stale result", "stale": True,
            "superseded_by": next((v["superseded_by"] for v in bad.values() if v.get("superseded_by")), None)}


def overall_status(statuses: list[str]) -> str:
    """failed > warn > passed; skipped suites do not change the result (all skipped -> skipped)."""
    s = [x for x in statuses if x and x != "skipped"]
    if not s:
        return "skipped"
    if "failed" in s:
        return "failed"
    if "warn" in s:
        return "warn"
    return "passed"
