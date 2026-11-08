"""API contract sweep: every GET operation of the served OpenAPI contract, called with real ids.

Per operation:
  * status 2xx (501 = contract operation without implementation, 5xx / timeouts = failure);
  * JSON responses validated against the operation's 200 schema (JSON Schema 2020-12, OpenAPI 3.1 components);
  * plausibility: no NaN / +-inf, as-of style dates (`as_of`, `data_as_of`, `px_date`, ...) not in the future;
  * latency (warn above `slow_ms`).
Global checks: the served snapshot / region data-as-of are within the coverage.yaml SLA of the active schedule profile,
and the served contract version equals contracts/api.yaml (a running API older than the contract = warn, restart due).

Path ids are resolved from list endpoints (first id found) or search; operations whose id has no instance (no theses,
no saved strategies, ...) are reported as `skipped`, not failed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import yaml

from app.core.config import Settings, get_settings
from app.qa.common import ApiClient, Resp, find_key, non_finite, parse_date, utc_now, walk

#: JSON keys whose date values describe "data as of" and must never be in the future (forward-looking fields such as
#: expected_date, event_date, window_hi, meeting dates and forecast periods are deliberately not listed)
AS_OF_KEYS = frozenset({"as_of", "data_as_of", "serving_as_of", "px_date", "observation_date", "latest_date", "common_date",
                        "as_of_min", "data_as_of_overall", "val_date", "score_date", "last_observation_date",
                        "filing_date", "computed_at", "built_at", "serving_built_at"})
FUTURE_TOLERANCE_DAYS = 1           # time zones: Asia closes "tomorrow" in UTC-5 terms

#: value for required query parameters without default / enum
KNOWN_QUERY = {"q": "apple", "indicator": "cpi_yoy", "x": "roic", "y": "value", "label_type": "country",
               "label_id": "US", "subject_type": "security"}
#: path param -> fixed value (stable ids)
FIXED_PATH = {"cc": "US", "series_id": "fred:CPIAUCSL", "label_type": "country", "label_id": "US"}
#: path param -> (list endpoint, params, keys searched depth-first in the answer); path-prefix specific entries first
LIST_SOURCES: list[tuple[str, str, str, dict[str, Any], tuple[str, ...]]] = [
    # (param, path prefix, list path, params, keys)
    ("security_id", "/api/v1/instruments", "/api/v1/instruments", {}, ("security_id",)),
    ("run_id", "/api/v1/studio", "/api/v1/studio/runs", {}, ("run_id",)),
    ("run_id", "/api/v1/backtest", "/api/v1/backtest/runs", {}, ("run_id",)),
    ("release_event_id", "", "/api/v1/macro/v2/calendar", {}, ("release_event_id",)),
    ("bank", "", "/api/v1/macro/v3/central-banks", {}, ("bank", "bank_id")),
    ("scenario_id", "", "/api/v1/macro/v4/scenarios", {}, ("scenario_id",)),
    ("episode_id", "", "/api/v1/macro/v4/episodes", {}, ("episode_id",)),
    ("sector_id", "", "/api/v1/meta/taxonomy", {}, ("sector_id", "id")),
    ("market_code", "", "/api/v1/positioning/markets", {}, ("market_code",)),
    ("asset_id", "", "/api/v1/cross-asset/cockpit", {}, ("asset_id",)),
    ("root", "", "/api/v1/cross-asset/curves", {}, ("root",)),
    ("story_id", "", "/api/v1/events/v2/stories", {}, ("story_id",)),
    ("model_run_id", "", "/api/v1/models/runs", {}, ("model_run_id",)),
    ("watchlist_id", "", "/api/v1/watchlists", {}, ("watchlist_id",)),
    ("study_id", "", "/api/v1/studio/studies", {}, ("study_id",)),
    ("peer_set_id", "", "/api/v1/studio/peer-sets", {}, ("peer_set_id",)),
    ("strategy_id", "", "/api/v1/backtest/strategies", {}, ("strategy_id",)),
    ("thesis_id", "", "/api/v1/theses", {}, ("thesis_id",)),
]
SEARCH_TICKER = "AAPL"
DONE_STATUSES = frozenset({"completed", "succeeded", "success", "done", "finished"})


@dataclass
class OpResult:
    operation_id: str
    path: str
    url: str | None = None
    status: str = "passed"            # passed | warn | failed | skipped
    http_status: int | None = None
    elapsed_ms: float | None = None
    bytes: int | None = None
    data_status: str | None = None
    issues: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.status = "failed"
        self.issues.append(msg)

    def warn(self, msg: str) -> None:
        if self.status == "passed":
            self.status = "warn"
        self.issues.append(msg)


# ------------------------------------------------------------------------------------------------ spec


def load_spec(client: ApiClient, settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    """The contract the running API serves (/openapi.json), else contracts/api.yaml."""
    r = client.get("/openapi.json")
    if r.status == 200 and isinstance(r.json, dict) and r.json.get("paths"):
        return r.json, "served"
    s = settings or get_settings()
    return yaml.safe_load((s.contracts_dir / "api.yaml").read_text(encoding="utf-8")), "contract_file"


def contract_version(settings: Settings | None = None) -> str | None:
    s = settings or get_settings()
    try:
        for line in (s.contracts_dir / "api.yaml").read_text(encoding="utf-8").splitlines()[:8]:
            m = re.match(r"\s+version:\s*([0-9.]+)", line)
            if m:
                return m.group(1)
    except OSError:
        return None
    return None


def get_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    comp = (spec.get("components") or {}).get("parameters") or {}
    out = []
    for path, item in (spec.get("paths") or {}).items():
        op = item.get("get")
        if not op:
            continue
        params = []
        for p in list(item.get("parameters") or []) + list(op.get("parameters") or []):
            if "$ref" in p:
                p = comp.get(p["$ref"].rsplit("/", 1)[-1], {})
            params.append(p)
        resp = (op.get("responses") or {}).get("200") or next(
            (v for k, v in (op.get("responses") or {}).items() if str(k).startswith("2")), {})
        content = (resp or {}).get("content") or {}
        ctype = next(iter(content), None)
        schema = (content.get("application/json") or {}).get("schema")
        out.append({"path": path, "operation_id": op.get("operationId") or path, "params": params,
                    "content_type": ctype, "schema": schema})
    return out


# ------------------------------------------------------------------------------------------------ id resolution


class Resolver:
    def __init__(self, client: ApiClient):
        self.client = client
        self.cache: dict[tuple[str, str], Any] = {}
        self.calls = 0

    def _list_value(self, list_path: str, params: dict[str, Any], keys: tuple[str, ...]) -> Any:
        key = (list_path, ",".join(keys))
        if key not in self.cache:
            self.calls += 1
            r = self.client.get(list_path, params)
            doc = r.json if r.status == 200 else None
            if isinstance(doc, list) and any(isinstance(x, dict) and "status" in x for x in doc):
                # prefer a finished instance (exports / results exist only for completed runs)
                done = [x for x in doc if isinstance(x, dict) and x.get("status") in DONE_STATUSES]
                doc = done or doc
            self.cache[key] = find_key(doc, keys) if doc is not None else None
        return self.cache[key]

    def security_id(self, ticker: str = SEARCH_TICKER) -> str | None:
        key = ("search", ticker)
        if key not in self.cache:
            self.calls += 1
            r = self.client.get("/api/v1/search", {"q": ticker, "limit": 5})
            rows = r.json if r.status == 200 and isinstance(r.json, list) else []
            hit = next((x for x in rows if str(x.get("ticker", "")).upper() == ticker.upper()), rows[0] if rows else None)
            self.cache[key] = hit.get("security_id") if hit else None
        return self.cache[key]

    def path_value(self, name: str, path: str, schema: dict[str, Any] | None) -> Any:
        sc = schema or {}
        if sc.get("enum"):
            return sc["enum"][0]
        if name in FIXED_PATH:
            return FIXED_PATH[name]
        for pname, prefix, list_path, params, keys in LIST_SOURCES:
            if pname == name and path.startswith(prefix):
                return self._list_value(list_path, params, keys)
        if name == "security_id":
            return self.security_id()
        return sc.get("default")

    def query_value(self, p: dict[str, Any]) -> Any:
        sc = p.get("schema") or {}
        if "default" in sc:
            return sc["default"]
        if sc.get("enum"):
            return sc["enum"][0]
        if p["name"] == "subject_id":
            return self.security_id()
        return KNOWN_QUERY.get(p["name"])


# ------------------------------------------------------------------------------------------------ checks


def _validator(spec: dict[str, Any], schema: dict[str, Any]) -> Any:
    from jsonschema import Draft202012Validator

    root = dict(schema)
    root["components"] = spec.get("components") or {}
    return Draft202012Validator(root)


def schema_errors(spec: dict[str, Any], schema: dict[str, Any], doc: Any, limit: int = 5) -> list[str]:
    try:
        v = _validator(spec, schema)
        errs = []
        for e in v.iter_errors(doc):
            loc = "$" + "".join(f"[{x}]" if isinstance(x, int) else f".{x}" for x in e.absolute_path)
            errs.append(f"{loc}: {e.message[:200]}")
            if len(errs) >= limit:
                break
        return errs
    except Exception as e:  # noqa: BLE001 - unresolvable schema is reported, not raised
        return [f"schema check error: {type(e).__name__}: {e}"[:300]]


def future_dates(doc: Any, today: date, tolerance_days: int = FUTURE_TOLERANCE_DAYS) -> list[str]:
    lim = today + timedelta(days=tolerance_days)
    out = []
    for p, k, v in walk(doc):
        if k in AS_OF_KEYS:
            d = parse_date(v)
            if d and d > lim:
                out.append(f"{p}={v}")
    return out


def _url(path: str, query: dict[str, Any]) -> str:
    if not query:
        return path
    from urllib.parse import urlencode

    return f"{path}?{urlencode(query)}"


def check_operation(client: ApiClient, spec: dict[str, Any], op: dict[str, Any], res: Resolver, today: date,
                    slow_ms: float = 3000.0) -> OpResult:
    out = OpResult(op["operation_id"], op["path"])
    if op["content_type"] == "text/event-stream":
        out.status = "skipped"
        out.issues.append("event stream (not swept)")
        return out
    path = op["path"]
    query: dict[str, Any] = {}
    for p in op["params"]:
        if p.get("in") == "path":
            v = res.path_value(p["name"], path, p.get("schema"))
            if v is None:
                out.status = "skipped"
                out.issues.append(f"no instance for {{{p['name']}}}")
                return out
            from urllib.parse import quote

            path = path.replace("{" + p["name"] + "}", quote(str(v), safe=":"))
        elif p.get("in") == "query" and p.get("required"):
            v = res.query_value(p)
            if v is None:
                out.status = "skipped"
                out.issues.append(f"no value for required query {p['name']}")
                return out
            query[p["name"]] = v
    out.url = _url(path, query)
    r: Resp = client.get(path, query)
    out.http_status, out.elapsed_ms, out.bytes = r.status, round(r.elapsed_ms, 1), len(r.text or "")
    if r.error:
        out.fail(f"request error: {r.error}")
        return out
    if r.status == 501:
        out.fail("501 not implemented (contract operation without an implementation)")
        return out
    if not 200 <= r.status < 300:
        detail = (r.json or {}).get("detail") if isinstance(r.json, dict) else None
        out.fail(f"HTTP {r.status}{': ' + str(detail)[:160] if detail else ''}")
        return out
    if r.elapsed_ms > slow_ms:
        out.warn(f"slow: {r.elapsed_ms:.0f} ms > {slow_ms:.0f} ms")
    if op["schema"] is None:
        if not r.text:
            out.fail("empty body")
        return out
    if not r.is_json or r.json is None and r.text.strip() not in ("null",):
        out.fail(f"not JSON (content-type {r.content_type!r})")
        return out
    doc = r.json
    if isinstance(doc, dict):
        out.data_status = doc.get("data_status") if isinstance(doc.get("data_status"), str) else None
    for e in schema_errors(spec, op["schema"], doc):
        out.fail(f"schema {e}")
    nf = non_finite(doc)
    if nf:
        out.fail(f"non-finite numbers at {', '.join(nf[:3])}{' ...' if len(nf) > 3 else ''}")
    fut = future_dates(doc, today)
    if fut:
        out.fail(f"as-of date in the future: {', '.join(fut[:3])}{' ...' if len(fut) > 3 else ''}")
    if out.data_status == "pending":
        out.warn("data_status=pending (upstream tables not populated)")
    return out


# ------------------------------------------------------------------------------------------------ freshness vs SLA


def sla_limits(settings: Settings | None = None) -> dict[str, int]:
    """dataset id -> max calendar-day age of its data-as-of: max(dataset sla, profile sla_days) sessions, converted
    to calendar days (x 7/5, + 2 days for a weekend boundary)."""
    from app.ops.coverage import effective_sla, load_coverage

    s = settings or get_settings()
    try:
        from app.ops.schedule import active_profile, profile_spec

        psla: float | None = float(profile_spec(active_profile(s)[0], s).get("sla_days") or 1)
    except Exception:  # noqa: BLE001
        psla = None
    out = {}
    for did, spec in (load_coverage(s).get("datasets") or {}).items():
        eff = effective_sla(spec, psla)
        out[did] = int(math.ceil(eff * 7 / 5)) + 2
    return out


def freshness_checks(client: ApiClient, today: date, settings: Settings | None = None) -> list[OpResult]:
    out = []
    r = client.get("/api/v1/system/status")
    st = r.json if r.status == 200 and isinstance(r.json, dict) else {}
    try:
        lim = sla_limits(settings)
    except Exception as e:  # noqa: BLE001
        lim = {}
        bad = OpResult("sla_limits", "contracts/coverage.yaml")
        bad.warn(f"coverage.yaml unreadable: {e}"[:200])
        out.append(bad)
    snap = OpResult("freshness:serving_as_of", "/api/v1/system/status")
    d = parse_date(st.get("serving_as_of"))
    max_age = lim.get("prices.US", 9)
    if d is None:
        snap.fail("serving_as_of missing")
    elif (today - d).days > max_age:
        snap.fail(f"snapshot data as of {d} is {(today - d).days} days old (SLA {max_age} days)")
    out.append(snap)
    regions = st.get("data_as_of") or {}
    if isinstance(regions, dict) and regions:
        reg = OpResult("freshness:data_as_of_by_region", "/api/v1/system/status")
        for rg, v in sorted(regions.items()):
            rd = parse_date(v)
            ml = lim.get(f"prices.{rg}")
            if rd and ml is not None and (today - rd).days > ml:
                reg.fail(f"{rg} data as of {rd} ({(today - rd).days} d > SLA {ml} d)")
        out.append(reg)
    return out


# ------------------------------------------------------------------------------------------------ run


def run_sweep(client: ApiClient, settings: Settings | None = None, only: list[str] | None = None,
              slow_ms: float = 3000.0, progress: Any = None) -> dict[str, Any]:
    s = settings or get_settings()
    today = utc_now().date()
    t0 = utc_now()
    spec, spec_source = load_spec(client, s)
    served_version = (spec.get("info") or {}).get("version")
    file_version = contract_version(s)
    ops = get_operations(spec)
    if only:
        ops = [o for o in ops if o["operation_id"] in only or o["path"] in only]
    res = Resolver(client)
    results: list[OpResult] = []
    for i, op in enumerate(ops):
        r = check_operation(client, spec, op, res, today, slow_ms)
        results.append(r)
        if progress:
            progress(i + 1, len(ops), r)
    results += freshness_checks(client, today, s)
    ver = OpResult("contract_version", "/openapi.json")
    if served_version and file_version and served_version != file_version:
        ver.warn(f"running API serves contract {served_version}, contracts/api.yaml is {file_version} (API restart due)")
    results.append(ver)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    status = "failed" if counts.get("failed") else "warn" if counts.get("warn") else "passed"
    lat = sorted(r.elapsed_ms for r in results if r.elapsed_ms is not None)
    return {
        "suite": "api_sweep", "status": status, "started_at": t0.isoformat(), "finished_at": utc_now().isoformat(),
        "client": client.mode, "base_url": client.base_url, "spec_source": spec_source,
        "served_version": served_version, "contract_version": file_version,
        "operations": len(ops), "counts": counts, "resolver_calls": res.calls,
        "latency_ms": {"median": lat[len(lat) // 2] if lat else None,
                       "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None, "max": lat[-1] if lat else None},
        "failed": counts.get("failed", 0),
        "results": [r.__dict__ for r in results],
    }
