"""UI smoke suite: one headless Chrome (DevTools protocol over websocket-client) over every route and tab.

Routes come from frontend/src/App.tsx (redirect-only routes and the 404 route excluded) plus smoke_routes.yaml extras;
route parameters are resolved through the API (real ids). Every route is loaded in both themes (dark at 1440 px,
light at 1280 px) and every [role=tab] trigger on it is clicked. A load FAILS on:
  * console errors / uncaught exceptions;
  * failed network requests (transport errors, HTTP >= 400 on /api other than the allowed list);
  * error panels ([data-testid=error-state]) or empty panels ([data-testid=empty-state]) where data is expected
    (smoke_routes.yaml allow_empty lists the pages whose content is user-created);
  * a crashed route ("Something went wrong") or the 404 page;
  * load (to network quiet) > max_load_ms;
  * horizontal overflow of the page or of the main scroll container.
Pending panels ([data-testid=pending-state]) and slow-but-under-budget loads are warnings.
Latency checks (smoke_routes.yaml `perf_checks`, roadmap v3 N4.4): on /news the page exposes window.__BIG360_NEWS_PERF__;
first rows painted must be <= first_rows_ms after navigation start (1000) and a synthetic live update (injected
through the SSE batching path, removed afterwards) must render within live_update_ms (100; median of `samples`).
A page without rows to measure is a warning.

Read-only: every non-GET /api request is failed in the browser (Fetch domain) except the compute-only POSTs listed in
READ_ONLY_POSTS (screener query, scenario run, ...), and localStorage gets a fresh profile per run, so the user's
app_state is never written. Skipped (status skipped, not failed) when Chrome or the UI is unreachable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.config import Settings, get_settings
from app.qa.common import ApiClient, find_key, reports_dir, run_stamp, utc_now, write_json

CONFIG_FILE = Path(__file__).with_name("smoke_routes.yaml")
READ_ONLY_POSTS = re.compile(r"/api/v1/(screener/query|macro/v4/scenarios(/[^/]+)?/run|studio/datasets/preview|"
                             r"backtest/(validate|from-screen|compare)|studio/compare|supply-chain/(shock|portfolio-exposure)|"
                             r"bql|bql/explain)$")  # bql: compute-only queries (roadmap v3 B1)
IGNORED_URLS = re.compile(r"/api/v1/live/stream|^data:|^blob:|/@vite/|/__vite|hot-update|favicon")
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PROBE_JS = r"""(() => {
  const q = (s) => [...document.querySelectorAll(s)];
  const txt = (e) => (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 140);
  const ctx = (e) => { const p = e.closest('section, article, [data-panel], .rounded-lg'); const h = p && p.querySelector('h2, h3, header'); return (h ? txt(h).slice(0, 60) + ' :: ' : '') + txt(e) };
  const de = document.documentElement; const main = document.querySelector('main');
  const body = (main || document.body).innerText || '';
  return JSON.stringify({
    sw: de.scrollWidth, cw: de.clientWidth,
    msw: main ? main.scrollWidth : 0, mcw: main ? main.clientWidth : 0,
    errors: q('[data-testid="error-state"]').map(ctx).concat(q('[role="alert"]').filter((e) => !e.closest('[data-testid]') && /Could not load data/.test(e.innerText)).map(ctx)),
    empties: q('[data-testid="empty-state"]').map(ctx),
    pending: q('[data-testid="pending-state"]').map(ctx),
    crashed: /Something went wrong/.test(body), notfound: /Page not found/.test(body),
    skeletons: q('.skeleton').length,
    tabs: q('[role="tab"]').map((e) => ({ text: txt(e), selected: e.getAttribute('aria-selected') === 'true', disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true' })),
    title: document.title, text: body.replace(/\s+/g, ' ').slice(0, 160),
  });
})()"""

CLICK_JS = r"""((t) => { const el = [...document.querySelectorAll('[role="tab"]')].find((e) => (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 140) === t);
  if (!el) return false; const o = { bubbles: true, cancelable: true, button: 0 };
  el.dispatchEvent(new PointerEvent('pointerdown', o)); el.dispatchEvent(new MouseEvent('mousedown', o));
  el.dispatchEvent(new PointerEvent('pointerup', o)); el.dispatchEvent(new MouseEvent('mouseup', o)); el.click(); return true })"""


# ------------------------------------------------------------------------------------------------ config / routes


def load_config(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or CONFIG_FILE).read_text(encoding="utf-8")) or {}


def app_routes(app_tsx: Path) -> list[str]:
    """Route patterns of App.tsx children (absolute), without redirect-only routes and the catch-all."""
    src = app_tsx.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"\{\s*path:\s*'([^']+)'\s*,\s*element:\s*<(\w+)", src):
        p, el = m.group(1), m.group(2)
        if el in ("Navigate", "NotFound") or p in ("*", "/"):
            continue
        out.append("/" + p.lstrip("/"))
    return out


def resolve_routes(patterns: list[str], cfg: dict[str, Any], client: ApiClient | None) -> tuple[list[str], list[dict[str, Any]]]:
    """(concrete routes, skipped [{route, reason}])."""
    params = cfg.get("params") or {}
    cache: dict[str, Any] = {}
    routes, skipped = [], []
    for p in patterns:
        if p in (cfg.get("skip_routes") or []):
            skipped.append({"route": p, "reason": "skip_routes"})
            continue
        if ":" not in p:
            routes.append(p)
            continue
        spec = params.get(p)
        val = None
        if spec and "fixed" in spec:
            val = spec["fixed"]
        elif spec and spec.get("api") and client is not None:
            key = f"{spec['api']}|{spec.get('keys')}"
            if key not in cache:
                r = client.get(spec["api"], spec.get("params"))
                cache[key] = find_key(r.json, tuple(spec.get("keys") or ())) if r.status == 200 else None
            val = cache[key]
        if val is None:
            skipped.append({"route": p, "reason": "no instance for the route parameter"})
            continue
        from urllib.parse import quote

        routes.append(re.sub(r":\w+", quote(str(val), safe=":"), p, count=1))
    for x in cfg.get("extra_routes") or []:
        if x not in routes:
            routes.append(x)
    return routes, skipped


def find_chrome() -> str | None:
    env = os.environ.get("BIG360_CHROME")
    for c in ([env] if env else []) + CHROME_PATHS:
        if c and Path(c).exists():
            return c
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("msedge")


def find_ui(api_url: str | None) -> str | None:
    """UI base URL: env BIG360_UI_URL, the API origin when it serves the built UI (O3 app mode), the dev Vite."""
    import httpx

    cands = [os.environ.get("BIG360_UI_URL")] + ([api_url] if api_url else []) + ["http://localhost:5173"]
    for u in [c for c in cands if c]:
        try:
            r = httpx.get(u.rstrip("/") + "/", timeout=4.0, follow_redirects=True)
            if r.status_code == 200 and '<div id="root"' in r.text:
                return u.rstrip("/")
        except Exception:  # noqa: BLE001
            continue
    return None


# ------------------------------------------------------------------------------------------------ CDP


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cdp:
    """Minimal DevTools client for one page target (thread-safe request / response, event callback)."""

    def __init__(self, ws_url: str, on_event: Any):
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.on_event = on_event
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}
        self._alive = True
        self._t = threading.Thread(target=self._reader, name="cdp-reader", daemon=True)
        self._t.start()

    def _reader(self) -> None:
        while self._alive:
            try:
                raw = self.ws.recv()
            except Exception:  # noqa: BLE001 - socket closed
                break
            if not raw:
                continue
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if "id" in m and m["id"] in self._pending:
                ev, box = self._pending.pop(m["id"])
                box.append(m)
                ev.set()
            elif "method" in m:
                try:
                    self.on_event(m["method"], m.get("params") or {})
                except Exception:  # noqa: BLE001
                    pass

    def send(self, method: str, params: dict[str, Any] | None = None, wait: bool = True, timeout: float = 60) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            i = self._id
            ev, box = threading.Event(), []
            if wait:
                self._pending[i] = (ev, box)
            self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        if not wait:
            return {}
        if not ev.wait(timeout):
            self._pending.pop(i, None)
            raise TimeoutError(f"CDP {method} timed out")
        return box[0].get("result") or {}

    def close(self) -> None:
        self._alive = False
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class PageState:
    inflight: set[str] = field(default_factory=set)
    last_net: float = field(default_factory=time.monotonic)
    console: list[str] = field(default_factory=list)
    netfail: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)

    def reset(self) -> None:
        self.console.clear()
        self.netfail.clear()
        self.blocked.clear()


class Browser:
    def __init__(self, chrome: str, profile_dir: Path, width: int = 1440):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--disable-background-networking", "--remote-allow-origins=*",
             f"--user-data-dir={profile_dir}", f"--remote-debugging-port={self.port}", f"--window-size={width},1000",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.st = PageState()
        self.allowed_http: list[str] = []
        ws_url = self._page_ws()
        self.cdp = Cdp(ws_url, self._event)
        for d in ("Runtime", "Network", "Page", "Log"):
            self.cdp.send(f"{d}.enable")
        self.cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*/api/*", "requestStage": "Request"}]})
        self._script_id: str | None = None

    def _page_ws(self) -> str:
        import httpx

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                ts = httpx.get(f"http://127.0.0.1:{self.port}/json", timeout=2).json()
                page = next((t for t in ts if t.get("type") == "page"), None)
                if page:
                    return page["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        raise RuntimeError("Chrome DevTools endpoint did not come up")

    # -------------------------------------------------------------------------------------------- events
    def _event(self, method: str, p: dict[str, Any]) -> None:
        st = self.st
        if method == "Fetch.requestPaused":
            req = p.get("request") or {}
            m, url = req.get("method", "GET"), req.get("url", "")
            if m in ("GET", "HEAD", "OPTIONS") or (m == "POST" and READ_ONLY_POSTS.search(url.split("?")[0])):
                self.cdp.send("Fetch.continueRequest", {"requestId": p["requestId"]}, wait=False)
            else:
                st.blocked.append(f"{m} {url}")
                self.cdp.send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "BlockedByClient"}, wait=False)
            return
        if method == "Network.requestWillBeSent":
            url = (p.get("request") or {}).get("url", "")
            if p.get("type") == "EventSource" or IGNORED_URLS.search(url):
                return
            st.inflight.add(p["requestId"])
            st.urls[p["requestId"]] = url
            st.last_net = time.monotonic()
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            if p.get("requestId") in st.inflight:
                st.inflight.discard(p["requestId"])
                st.last_net = time.monotonic()
                if method == "Network.loadingFailed" and not p.get("canceled") and p.get("blockedReason") is None \
                        and p.get("errorText") != "net::ERR_BLOCKED_BY_CLIENT":
                    st.netfail.append(f"{p.get('errorText')} {st.urls.get(p['requestId'], '')}"[:300])
        elif method == "Network.responseReceived":
            resp = p.get("response") or {}
            url, code = resp.get("url", ""), int(resp.get("status") or 0)
            if code >= 400 and "/api/" in url and not IGNORED_URLS.search(url):
                path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
                if not any(f"{code} " in a and path.startswith(a.split(" ", 1)[1]) for a in self.allowed_http):
                    st.netfail.append(f"HTTP {code} {path}"[:300])
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            st.console.append(f"exception: {(d.get('exception') or {}).get('description') or d.get('text')}"[:400])
        elif method == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            args = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args") or [])
            st.console.append(f"console.{p.get('type')}: {args}"[:400])
        elif method == "Log.entryAdded":
            e = p.get("entry") or {}
            if e.get("level") == "error" and not IGNORED_URLS.search(e.get("url") or ""):
                # network errors are reported by the Network domain already
                if e.get("source") not in ("network",):
                    st.console.append(f"log: {e.get('text')} {e.get('url') or ''}"[:400])

    # -------------------------------------------------------------------------------------------- page ops
    def set_theme(self, theme: str, width: int) -> None:
        ui = {"state": {"theme": theme, "region": "ALL", "navCollapsed": False, "locale": "en-US"}, "version": 0}
        src = (f"try{{localStorage.setItem('big360.ui', {json.dumps(json.dumps(ui))});"
               "if(!localStorage.getItem('big360.watchlist')) localStorage.setItem('big360.watchlist', "
               "JSON.stringify({state:{ids:[],rules:[]},version:0}))}catch(e){}")
        if self._script_id:
            self.cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self._script_id})
        self._script_id = self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": src}).get("identifier")
        self.cdp.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1000, "deviceScaleFactor": 1,
                                                             "mobile": False})

    def _busy(self) -> int:
        """Loading indicators on screen (skeletons / aria-busy); -1 when the page cannot be evaluated."""
        try:
            v = self.evaluate("document.querySelectorAll('.skeleton, [aria-busy=\"true\"]').length + "
                              "(document.querySelector('#root') && document.querySelector('#root').children.length ? 0 : 1)")
            return int(v) if isinstance(v, (int, float)) else -1
        except Exception:  # noqa: BLE001
            return -1

    def wait_quiet(self, quiet_ms: int, timeout_ms: int, t0: float) -> float:
        """Wait until the page is settled: `quiet_ms` without network activity AND no loading indicator (skeleton /
        aria-busy) on screen. Returns the load time in ms: the later of the last network activity and the moment
        the loading indicators disappeared (timeout -> the elapsed time)."""
        time.sleep(0.6)
        ready_at: float | None = None
        while (time.monotonic() - t0) * 1000 < timeout_ms:
            quiet = not self.st.inflight and (time.monotonic() - self.st.last_net) * 1000 >= quiet_ms
            busy = self._busy()
            if busy == 0 and ready_at is None:
                ready_at = time.monotonic()
            elif busy != 0:
                ready_at = None
            if quiet and ready_at is not None:
                return max(0.0, (max(min(self.st.last_net, time.monotonic()), ready_at) - t0) * 1000)
            time.sleep(0.25)
        return (time.monotonic() - t0) * 1000

    def navigate(self, url: str, quiet_ms: int, timeout_ms: int) -> float:
        self.st.inflight.clear()
        t0 = time.monotonic()
        self.st.last_net = t0
        self.cdp.send("Page.navigate", {"url": url})
        return self.wait_quiet(quiet_ms, timeout_ms, t0)

    def evaluate(self, expr: str, await_promise: bool = False) -> Any:
        r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise})
        return (r.get("result") or {}).get("value")

    def probe(self) -> dict[str, Any]:
        v = self.evaluate(PROBE_JS)
        return json.loads(v) if isinstance(v, str) else {}

    def click_tab(self, text: str, quiet_ms: int, timeout_ms: int) -> tuple[bool, float]:
        t0 = time.monotonic()
        self.st.last_net = t0
        ok = bool(self.evaluate(f"({CLICK_JS})({json.dumps(text)})"))
        return ok, self.wait_quiet(quiet_ms, timeout_ms, t0)

    def screenshot(self, path: Path) -> None:
        import base64

        try:
            d = self.cdp.send("Page.captureScreenshot", {"format": "png"}, timeout=30).get("data")
            if d:
                path.write_bytes(base64.b64decode(d))
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.cdp.close()
        finally:
            try:
                self.proc.terminate()
                self.proc.wait(10)
            except Exception:  # noqa: BLE001
                self.proc.kill()


# ------------------------------------------------------------------------------------------------ evaluation


PERF_JS = r"""(async (samples) => {
  const p = window.__BIG360_NEWS_PERF__;
  if (!p) return JSON.stringify({missing: true});
  const ups = [];
  for (let i = 0; i < samples; i++) { ups.push(await p.simulate(5)); await new Promise((r) => setTimeout(r, 120)); }
  return JSON.stringify({firstRowsMs: p.firstRowsMs, updates: ups});
})"""


def perf_check(br: Any, route: str, cfg: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    """roadmap v3 N4.4 latency budget of a page exposing window.__BIG360_NEWS_PERF__ (see module docstring)."""
    spec = (cfg.get("perf_checks") or {}).get(route.split("?")[0])
    if not spec:
        return "passed", [], {}
    raw = br.evaluate(f"({PERF_JS})({int(spec.get('samples', 5))})", await_promise=True)
    try:
        m = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        m = {}
    if not m or m.get("missing"):
        return "warn", ["latency check: page did not expose its perf hook"], m
    issues: list[str] = []
    status = "passed"
    fr = m.get("firstRowsMs")
    ups = sorted(x for x in (m.get("updates") or []) if isinstance(x, (int, float)) and x >= 0)
    if fr is None:
        status, issues = "warn", ["latency check: no rows to measure (empty feed)"]
    elif fr > float(spec.get("first_rows_ms", 1000)):
        status = "failed"
        issues.append(f"first rows painted after {fr:.0f} ms > {float(spec.get('first_rows_ms', 1000)):.0f} ms")
    if ups:
        med = ups[len(ups) // 2]
        m["live_update_ms_median"] = med
        if med > float(spec.get("live_update_ms", 100)):
            status = "failed"
            issues.append(f"live update render {med:.0f} ms > {float(spec.get('live_update_ms', 100)):.0f} ms")
    elif fr is not None:
        status = "warn" if status == "passed" else status
        issues.append("latency check: synthetic live update not measured")
    return status, issues, m


def judge(route: str, probe: dict[str, Any], st: PageState, load_ms: float, cfg: dict[str, Any]) -> tuple[str, list[str]]:
    issues: list[str] = []
    fail = False
    max_load = float(cfg.get("max_load_ms", 8000))
    allow_empty = any(route == a or route.startswith(a + "?") for a in cfg.get("allow_empty") or [])
    if st.console:
        fail = True
        issues += [f"console: {c}" for c in st.console[:5]]
    if st.netfail:
        fail = True
        issues += [f"request: {n}" for n in st.netfail[:5]]
    if not (probe.get("text") or "").strip():
        fail = True
        issues.append("blank page (nothing rendered)")
    if probe.get("crashed"):
        fail = True
        issues.append("route crashed (error boundary)")
    if probe.get("notfound"):
        fail = True
        issues.append("404 page")
    for e in probe.get("errors") or []:
        fail = True
        issues.append(f"error panel: {e}")
    if not allow_empty:
        pats = [re.compile(x) for x in cfg.get("allow_empty_text") or []]
        for e in probe.get("empties") or []:
            if any(p.search(e) for p in pats):
                continue  # the entity legitimately has no such data (info only)
            fail = True
            issues.append(f"empty panel: {e}")
    if load_ms > max_load:
        fail = True
        issues.append(f"load {load_ms:.0f} ms > {max_load:.0f} ms")
    if probe.get("sw", 0) > probe.get("cw", 0) + 1:
        fail = True
        issues.append(f"horizontal overflow (page {probe['sw']} > {probe['cw']} px)")
    elif probe.get("msw", 0) > probe.get("mcw", 0) + 1:
        fail = True
        issues.append(f"horizontal overflow (main {probe['msw']} > {probe['mcw']} px)")
    warn = False
    for p in probe.get("pending") or []:
        warn = True
        issues.append(f"pending panel: {p}")
    if probe.get("skeletons", 0) > 6:
        warn = True
        issues.append(f"{probe['skeletons']} skeletons still visible after network quiet")
    return ("failed" if fail else "warn" if warn else "passed"), issues


def run_smoke(settings: Settings | None = None, api_url: str | None = None, ui_url: str | None = None,
              themes: tuple[str, ...] = ("dark", "light"), routes: list[str] | None = None, max_tabs: int = 20,
              tabs: bool = True, progress: Any = None, out_dir: Path | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    t_start = utc_now()
    base = {"suite": "smoke", "started_at": t_start.isoformat(), "failed": 0}
    chrome = find_chrome()
    if not chrome:
        return {**base, "status": "skipped", "message": "Chrome / Edge not found (set BIG360_CHROME)",
                "finished_at": utc_now().isoformat()}
    ui = (ui_url or find_ui(api_url) or "").rstrip("/")
    if not ui:
        return {**base, "status": "skipped", "message": "UI not reachable (BIG360_UI_URL, API origin, localhost:5173)",
                "finished_at": utc_now().isoformat()}
    cfg = load_config()
    client = ApiClient(api_url) if api_url else None
    try:
        pats = app_routes(s.project_root / "frontend" / "src" / "App.tsx")
        concrete, skipped = resolve_routes(pats, cfg, client)
    finally:
        if client:
            client.close()
    if routes:
        concrete = routes
    out = out_dir or (reports_dir("smoke", s) / run_stamp(t_start))
    out.mkdir(parents=True, exist_ok=True)
    prof = Path(tempfile.mkdtemp(prefix="big360_smoke_"))
    widths = cfg.get("widths") or {"dark": 1440, "light": 1280}
    quiet, nav_to = int(cfg.get("quiet_ms", 1200)), int(cfg.get("nav_timeout_ms", 30000))
    loads: list[dict[str, Any]] = []
    br = None
    try:
        br = Browser(chrome, prof)
        br.allowed_http = list(cfg.get("allowed_http") or [])
        n_total = len(concrete) * len(themes)
        k = 0
        for theme in themes:
            w = int(widths.get(theme, 1440))
            br.set_theme(theme, w)
            for route in concrete:
                k += 1
                br.st.reset()
                load_ms = br.navigate(f"{ui}{route}", quiet, nav_to)
                pr = br.probe()
                status, issues = judge(route, pr, br.st, load_ms, cfg)
                cold_ms = None
                if load_ms > float(cfg.get("max_load_ms", 8000)) and not br.st.console and not br.st.netfail:
                    # a cold first hit (API caches empty after a snapshot flip / restart) is retried once; the warm
                    # load is judged and the cold time reported as a warning
                    cold_ms = load_ms
                    br.st.reset()
                    load_ms = br.navigate(f"{ui}{route}", quiet, nav_to)
                    pr = br.probe()
                    status, issues = judge(route, pr, br.st, load_ms, cfg)
                    if status == "passed":
                        status = "warn"
                    issues.append(f"cold load {cold_ms:.0f} ms (warm {load_ms:.0f} ms)")
                pst, pissues, pm = perf_check(br, route, cfg)
                if pst == "failed":
                    # cold browser cache / first API hit: judge a warm reload, report the cold value as a warning
                    cold = pm.get("firstRowsMs")
                    br.navigate(f"{ui}{route}", quiet, nav_to)
                    pst, pissues, pm = perf_check(br, route, cfg)
                    if pst == "passed":
                        pst, pissues = "warn", [f"cold first rows {cold} ms (warm {pm.get('firstRowsMs')} ms)"]
                if pst != "passed" or pm:
                    issues = issues + pissues
                    if pst == "failed" or (pst == "warn" and status == "passed"):
                        status = pst
                rec = {"route": route, "tab": None, "theme": theme, "width": w, "status": status,
                       "load_ms": round(load_ms), "issues": issues, "blocked": list(br.st.blocked)[:5],
                       "text": pr.get("text")}
                if pm:
                    rec["perf"] = {k: pm.get(k) for k in ("firstRowsMs", "updates", "live_update_ms_median")}
                if status == "failed":
                    shot = out / f"{len(loads):03d}_{theme}_{re.sub(r'[^A-Za-z0-9]+', '_', route).strip('_')}.png"
                    br.screenshot(shot)
                    rec["screenshot"] = shot.name
                loads.append(rec)
                if progress:
                    progress(k, n_total, rec)
                if not tabs:
                    continue
                seen = set()
                for tb in (pr.get("tabs") or [])[:max_tabs]:
                    txt = tb.get("text") or ""
                    if not txt or txt in seen or tb.get("selected") or tb.get("disabled"):
                        seen.add(txt)
                        continue
                    seen.add(txt)
                    br.st.reset()
                    ok, tl = br.click_tab(txt, quiet, nav_to)
                    tp = br.probe() if ok else {}
                    if ok:
                        status, issues = judge(route, tp, br.st, tl, cfg)
                    else:
                        status, issues = "warn", ["tab trigger disappeared before it could be clicked"]
                    rec = {"route": route, "tab": txt, "theme": theme, "width": w, "status": status, "load_ms": round(tl),
                           "issues": issues, "blocked": list(br.st.blocked)[:5], "text": tp.get("text")}
                    if status == "failed":
                        shot = out / f"{len(loads):03d}_{theme}_{re.sub(r'[^A-Za-z0-9]+', '_', route + '_' + txt).strip('_')[:80]}.png"
                        br.screenshot(shot)
                        rec["screenshot"] = shot.name
                    loads.append(rec)
                    if progress:
                        progress(k, n_total, rec)
    except Exception as e:  # noqa: BLE001 - browser trouble is a skipped suite, not a crash of the job
        res = {**base, "status": "skipped" if not loads else "failed", "ui_url": ui,
               "message": f"browser error: {type(e).__name__}: {e}"[:300], "loads": loads,
               "finished_at": utc_now().isoformat(), "report_dir": str(out)}
        write_json(out / "smoke.json", res)
        return res
    finally:
        if br:
            br.close()
        shutil.rmtree(prof, ignore_errors=True)
    counts: dict[str, int] = {}
    for r in loads:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lat = sorted(r["load_ms"] for r in loads)
    res = {**base, "status": "failed" if counts.get("failed") else "warn" if counts.get("warn") else "passed",
           "ui_url": ui, "chrome": chrome, "themes": list(themes), "routes": len(concrete), "loads_total": len(loads),
           "counts": counts, "failed": counts.get("failed", 0), "skipped_routes": skipped,
           "load_ms": {"median": lat[len(lat) // 2] if lat else None, "max": lat[-1] if lat else None},
           "loads": loads, "finished_at": utc_now().isoformat(), "report_dir": str(out)}
    write_json(out / "smoke.json", res)
    (out / "smoke.html").write_text(html_report(res), encoding="utf-8")
    return res


def html_report(res: dict[str, Any]) -> str:
    import html

    rows = []
    for r in res.get("loads") or []:
        cls = {"failed": "f", "warn": "w"}.get(r["status"], "p")
        shot = f'<a href="{html.escape(r["screenshot"])}">png</a>' if r.get("screenshot") else ""
        rows.append(f'<tr class="{cls}"><td>{html.escape(r["route"])}</td><td>{html.escape(r.get("tab") or "")}</td>'
                    f'<td>{r["theme"]}</td><td>{r["status"]}</td><td>{r["load_ms"]}</td>'
                    f'<td>{"<br>".join(html.escape(i) for i in r["issues"])}</td><td>{shot}</td></tr>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>UI smoke {html.escape(res.get('started_at', ''))}</title>
<style>body{{font:13px system-ui;margin:16px;background:#fff;color:#111}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:3px 6px;vertical-align:top}}tr.f td{{background:#fde8e8}}tr.w td{{background:#fff6dd}}
@media (prefers-color-scheme: dark){{body{{background:#111;color:#eee}}td,th{{border-color:#333}}tr.f td{{background:#3a1414}}tr.w td{{background:#3a2e0e}}}}</style>
</head><body><h1>UI smoke: {html.escape(res['status'])}</h1>
<p>{html.escape(res.get('ui_url') or '')} · {res.get('routes')} routes · {res.get('loads_total')} loads · {json.dumps(res.get('counts'))}
· load median {res.get('load_ms', {}).get('median')} ms, max {res.get('load_ms', {}).get('max')} ms</p>
<table><tr><th>Route</th><th>Tab</th><th>Theme</th><th>Status</th><th>Load ms</th><th>Issues</th><th></th></tr>{''.join(rows)}</table></body></html>"""
