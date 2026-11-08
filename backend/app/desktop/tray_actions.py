"""Tray menu model + handlers, free of any GUI code (unit-tested; app.desktop.tray only draws it).

    ctl = TrayController()
    ctl.poll()                 # GET /api/v1/system/status (+ /system/schedule), local ops files -> notifications
    ctl.menu()                 # [MenuItem] for the current state
    ctl.dispatch("refresh")    # run a command id -> message for a notification (or None)

Command ids: open, status:refresh, refresh, sync, pause, livenews, livequotes, news, schedule:<profile>, health,
logs, autostart, update, quit. sync = "Sync now" (startup sync: data/state/ops/sync_now.json, taken by the supervisor);
a finished sync with failures / skips becomes a toast (data/state/ops/sync_status.json). livenews toggles the live news service (data/state/ops/live_news.OFF, the supervisor's 4th
child), livequotes the delayed quotes service (data/state/ops/live_quotes.OFF, 5th child); news alerts
(watchlist_news / story_velocity, GET /api/v1/live/alerts) become toasts (cfg notify_news_alerts, default on).
Local state used directly (same machine, no API needed): data/state/ops/filler.PAUSE (governor pause flag),
data/state/ops/refresh_now.json (refresh request taken by the worker), data/state/worker.json (heartbeat),
data/state/ops/supervisor.lock / supervisor.STOP, data/logs/app/supervisor.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.desktop import autostart
from app.desktop.appconfig import app_config, app_version
from app.ops.paths import app_logs_dir, ops_state_dir, read_json

Notify = Callable[[str, str, str], None]          # (title, message, severity info|warn|error)


@dataclass
class MenuItem:
    id: str = ""
    label: str = ""
    enabled: bool = True
    checked: bool = False
    default: bool = False
    separator: bool = False
    children: list[MenuItem] = field(default_factory=list)


SEP = MenuItem(separator=True)


class ApiClient:
    """Tiny JSON client for the local API (urllib; no extra dependencies)."""

    def __init__(self, base: str, timeout: float = 8.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - localhost only
            raw = r.read()
        return json.loads(raw) if raw else None

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def put(self, path: str, body: Any) -> Any:
        return self._req("PUT", path, body)


def _aware(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def fmt_when(v: Any) -> str:
    """ISO time -> 'Sat 24 Oct 08:00' in local time ('-' when unknown)."""
    t = _aware(v)
    if t is None:
        return "-"
    return t.astimezone().strftime("%a %d %b %H:%M")


def fmt_date(v: Any) -> str:
    t = _aware(v)
    return t.date().isoformat() if t else (str(v)[:10] if v else "-")


def _lock_held(path: Path) -> bool:
    from filelock import FileLock, Timeout  # noqa: PLC0415

    if not path.exists():
        return False
    lk = FileLock(str(path), timeout=0)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True
    lk.release()
    return False


class TrayController:
    def __init__(self, settings: Settings | None = None, api: Any = None, open_url: Callable[[str], Any] | None = None,
                 open_path: Callable[[str], Any] | None = None, notify: Notify | None = None,
                 cfg: dict[str, Any] | None = None, popen: Callable[..., Any] | None = None):
        self.s = settings or get_settings()
        self.cfg = cfg or app_config(self.s)
        self.base = f"http://{self.cfg['api_host']}:{int(self.cfg['api_port'])}"
        self.api = api or ApiClient(self.base)
        self.open_url = open_url or webbrowser.open
        self.open_path = open_path or (getattr(os, "startfile", None) or (lambda p: subprocess.Popen(["xdg-open", p])))
        self.notify: Notify = notify or (lambda *_a: None)
        self._popen = popen or subprocess.Popen
        self.status: dict[str, Any] | None = None
        self.schedule: dict[str, Any] | None = None
        self.api_error: str | None = None
        self.last_poll: float = 0.0
        self.seen_alerts: set[str] | None = None
        self.seen_runs: dict[str, str] | None = None
        self.update: dict[str, Any] | None = None
        self.last_update_check: float = 0.0
        self.quitting = False
        self.supervisor_pid: int | None = None
        self.sup_restarts = 0
        self.sup_next_try = 0.0
        self.quiet_until = 0.0          # monotonic time before which ops alerts are not notified (start-up)
        self.seen_news_alerts: set[str] | None = None
        self.seen_sync: str | None = None   # run_id of the last finished sync already notified

    # ------------------------------------------------------------------ paths / local state
    @property
    def ops_dir(self) -> Path:
        return ops_state_dir(self.s)

    @property
    def pause_flag(self) -> Path:
        return self.ops_dir / "filler.PAUSE"

    def is_paused(self) -> bool:
        """The pause flag is set and not expired (coverage v3 A4, app.ops.pause: an expired file no longer pauses)."""
        from app.ops.pause import read_pause  # noqa: PLC0415

        return read_pause(self.pause_flag, "filler", warn=False).active

    def supervisor_running(self) -> bool:
        return _lock_held(self.ops_dir / "supervisor.lock")

    def supervisor_status(self) -> dict[str, Any]:
        return read_json(app_logs_dir(self.s) / "supervisor.json", {}) or {}

    def worker_heartbeat(self) -> dict[str, Any]:
        hb = read_json(self.s.state_dir / "worker.json", {}) or {}
        t = _aware(hb.get("heartbeat_at"))
        hb["alive"] = bool(t and (datetime.now(UTC) - t).total_seconds() < 120)
        return hb

    def filler_state(self) -> str:
        f = read_json(self.ops_dir / "filler.json", {}) or {}
        t = _aware(f.get("heartbeat_at"))
        if t is None or (datetime.now(UTC) - t).total_seconds() > 300:
            return "paused (flag set), not running" if self.is_paused() else "not running"
        gov = f.get("governor") or {}
        st = gov.get("state") or "running"
        reasons = "; ".join(gov.get("reasons") or [])
        n = len(f["running"]) if isinstance(f.get("running"), (list, dict)) else None
        out = st + (f" ({reasons})" if reasons else "")
        if n:
            out += f", {n} task(s)"
        return out

    # ------------------------------------------------------------------ polling
    def poll(self) -> list[tuple[str, str, str]]:
        """Refresh the cached API state; returns (and sends) the notifications for what changed."""
        self.last_poll = time.monotonic()
        try:
            self.status = self.api.get("/api/v1/system/status")
            self.api_error = None
        except Exception as e:  # API down / starting
            self.status = None
            self.api_error = _short_err(e)
        try:
            self.schedule = self.api.get("/api/v1/system/schedule") if self.status is not None else None
        except Exception:
            self.schedule = None
        notes = self._alert_notes() + self._run_notes() + self._news_alert_notes() + self._sync_notes()
        if time.monotonic() - self.last_update_check > float(self.cfg.get("update_check_h", 24)) * 3600 \
                or self.last_update_check == 0.0:
            notes += self.check_update()
        if self.cfg.get("notifications", True):
            for n in notes:
                self.notify(*n)
        return notes

    def _alerts_unknown(self) -> bool:
        """Coverage v3 A7: /system/status is served from a background cache. While it is warming / failed, or its ops
        block failed to compute (cache.section_errors 'ops_status: ...'), ops_alerts is not the current alert set:
        diffing it would forget every seen alert and re-toast all of them on the next good poll."""
        cache = (self.status or {}).get("cache") or {}
        if cache.get("state") in ("warming", "error"):
            return True
        return any(str(e).startswith("ops_status") for e in (cache.get("section_errors") or []))

    def _alert_notes(self) -> list[tuple[str, str, str]]:
        if self.status is None or time.monotonic() < self.quiet_until or self._alerts_unknown():
            return []   # seen_alerts unchanged
        sev = set(self.cfg.get("notify_severities") or ["error", "warn"])
        alerts = [a for a in (self.status.get("ops_alerts") or []) if a.get("severity") in sev]
        ids = {a.get("id") for a in alerts}
        out: list[tuple[str, str, str]] = []
        if self.seen_alerts is None:  # first poll: one summary of what is already wrong
            errs = [a for a in alerts if a.get("severity") == "error"]
            if errs:
                out.append((f"BiG360: {len(errs)} ops alert(s)", "; ".join(a.get("title", "") for a in errs)[:250], "error"))
        else:
            for a in alerts:
                if a.get("id") not in self.seen_alerts:
                    out.append((f"BiG360: {a.get('title')}", str(a.get("detail") or "")[:250],
                                "error" if a.get("severity") == "error" else "warn"))
        self.seen_alerts = ids
        return out

    def _news_alert_notes(self) -> list[tuple[str, str, str]]:
        """roadmap v3 N4.3: live news alerts (watchlist_news / story_velocity) -> toasts (each event once)."""
        if self.status is None or not self.cfg.get("notify_news_alerts", True):
            return []
        try:
            page = self.api.get("/api/v1/live/alerts?limit=30")
        except Exception:
            return []
        evs = [a for a in (page or {}).get("items") or [] if a.get("rule_type") in ("watchlist_news", "story_velocity")]
        ids = {str(a.get("event_id")) for a in evs}
        out: list[tuple[str, str, str]] = []
        if self.seen_news_alerts is not None:
            for a in evs:
                if str(a.get("event_id")) not in self.seen_news_alerts and not a.get("read"):
                    kind = "News" if a.get("rule_type") == "watchlist_news" else "Story burst"
                    out.append((f"BiG360 · {kind}", str(a.get("message") or "")[:250], "info"))
        self.seen_news_alerts = (self.seen_news_alerts or set()) | ids
        return out[:3]

    def live_news_on(self) -> bool:
        from app.ops.supervisor import live_news_enabled  # noqa: PLC0415

        return live_news_enabled(self.s)

    def live_news_state(self) -> str:
        ch = ((self.supervisor_status() or {}).get("children") or {}).get("realtime")
        if not self.live_news_on():
            return "off"
        if not ch:
            return "on (starts with the supervisor)" if self.cfg.get("live_news", True) else "not configured"
        st = ch.get("state") or "-"
        mem = f", {ch['rss_mb']} MB" if ch.get("rss_mb") else ""
        return f"on ({st}{mem})"

    def live_quotes_on(self) -> bool:
        from app.ops.supervisor import live_quotes_enabled  # noqa: PLC0415

        return live_quotes_enabled(self.s)

    def live_quotes_state(self) -> str:
        ch = ((self.supervisor_status() or {}).get("children") or {}).get("quotes")
        if not self.live_quotes_on():
            return "off"
        if not ch:
            return "on (starts with the supervisor)" if self.cfg.get("live_quotes", True) else "not configured"
        st = ch.get("state") or "-"
        mem = f", {ch['rss_mb']} MB" if ch.get("rss_mb") else ""
        return f"on ({st}{mem})"

    def toggle_live_quotes(self) -> str:
        from app.ops.supervisor import set_live_quotes  # noqa: PLC0415

        on = not self.live_quotes_on()
        set_live_quotes(on, self.s, by="tray")
        return ("Live quotes on: delayed prices for watchlists, indices, FX and futures refresh every 1-2 minutes "
                "while markets are open.") if on else             "Live quotes off: the quotes service stops (the last delayed prices stay, labelled with their time)."

    def toggle_live_news(self) -> str:
        from app.ops.supervisor import set_live_news  # noqa: PLC0415

        on = not self.live_news_on()
        set_live_news(on, self.s, by="tray")
        return ("Live news on: GDELT 15-min files, RSS feeds and Polygon news stream into the News Terminal within a "
                "few seconds of the supervisor picking it up.") if on else \
            "Live news off: the realtime service stops (data already received stays)."

    def _run_notes(self) -> list[tuple[str, str, str]]:
        """Refresh finished / failed (data/state/ops/schedule_runs.json, written by the worker)."""
        runs = read_json(self.ops_dir / "schedule_runs.json", {}) or {}
        cur = {k: str(v.get("finished_at")) for k, v in runs.items() if isinstance(v, dict) and v.get("finished_at")
               and v.get("notify") is not False}   # quiet entries (hourly news compaction): no toast per run
        out: list[tuple[str, str, str]] = []
        if self.seen_runs is not None:
            for k, fin in cur.items():
                if self.seen_runs.get(k) != fin:
                    st = runs[k].get("status")
                    dags = ", ".join(f"{d} {s}" for d, s in (runs[k].get("dags") or {}).items())
                    sevr = "info" if st == "success" else ("warn" if st == "partial" else "error")
                    out.append((f"BiG360: refresh {k} {st}", dags[:250] or str(st), sevr))
        self.seen_runs = cur
        return out

    def sync_status(self) -> dict[str, Any]:
        from app.ops.sync.state import read_status  # noqa: PLC0415

        try:
            return read_status(self.s) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _sync_notes(self) -> list[tuple[str, str, str]]:
        """Startup sync finished: a toast when it had failures / skips (warn), or "Data up to date as of ..." after a
        startup / manual sync that brought data (info). Each finished run is notified once."""
        st = self.sync_status()
        rid, fin = st.get("run_id"), st.get("finished_at")
        if not rid or not fin or st.get("state") != "idle":
            return []
        first = self.seen_sync is None
        if self.seen_sync == rid:
            return []
        self.seen_sync = rid
        t_fin = _aware(fin)
        if first and (t_fin is None or (datetime.now(UTC) - t_fin).total_seconds() > 600):   # old run at tray start
            return []
        counts = st.get("counts") or {}
        asof = fmt_date((st.get("data_as_of") or {}).get("overall"))
        failed = int(counts.get("failed", 0) or 0)
        skipped = sum(int(counts.get(k, 0) or 0) for k in ("not_yet_published", "source_unavailable", "needs_key"))
        if failed or st.get("result") == "failed":
            bad = [d.get("dataset") for d in st.get("datasets") or [] if d.get("reason") == "failed"][:4]
            return [(f"BiG360: data sync finished with {failed} failure(s)",
                     f"Data as of {asof}. Failed: {', '.join(bad) or '-'}"
                     + (f"; {skipped} skipped (reasons on Data health)" if skipped else ""), "warn")]
        if st.get("trigger") in ("startup", "manual", "tray", "api"):
            fetched = int((st.get("progress") or {}).get("fetched") or 0)
            msg = f"Data up to date as of {asof}" + (f" ({fetched} dataset(s) refreshed" if fetched else "")
            msg += (f", {skipped} skipped - see Data health)" if skipped else (")" if fetched else ""))
            return [("BiG360: data sync done", msg, "info")]
        return []

    def run_sync(self) -> str:
        from app.ops.sync.state import request_sync, sync_running  # noqa: PLC0415

        if sync_running(self.s):
            st = self.sync_status()
            pr = st.get("progress") or {}
            return f"A data sync is already running ({pr.get('jobs_done', 0)}/{pr.get('jobs_total', 0)} steps)."
        request_sync(self.s, by="tray")
        if not self.supervisor_running():
            return "Sync requested: it starts when BiG360 is running."
        return "Data sync starting within a few seconds: it fetches only what is missing (Data health > Data sync)."

    def check_update(self) -> list[tuple[str, str, str]]:
        from app.desktop.updates import check_for_update  # noqa: PLC0415

        self.last_update_check = time.monotonic()
        prev = (self.update or {}).get("latest")
        try:
            self.update = check_for_update(app_version(self.s), self.cfg.get("update_dir") or None)
        except Exception as e:  # pragma: no cover - defensive
            self.update = {"available": False, "error": str(e)}
        if self.update.get("available") and self.update.get("latest") != prev:
            return [("BiG360 update available", f"Version {self.update['latest']} in {self.update.get('folder')}", "info")]
        return []

    # ------------------------------------------------------------------ presentation
    def _primary_entry(self) -> dict[str, Any] | None:
        ents = ((self.status or {}).get("scheduler") or {}).get("entries") or (self.schedule or {}).get("entries") or []
        return next((e for e in ents if e.get("kind") == "chain"), None) or \
            next((e for e in ents if "nightly" in (e.get("dags") or [])), None) or (ents[0] if ents else None)

    def status_lines(self) -> list[str]:
        lines = [f"BiG Equity 360 {app_version(self.s)}"]
        sup = self.supervisor_running()
        lines.append("Supervisor: running" if sup else "Supervisor: not running")
        if self.status is None:
            lines.append(f"Dashboard API {self.base}: not reachable" + (f" ({self.api_error})" if self.api_error else ""))
        else:
            st = self.status
            lines.append(f"Data as of {fmt_date(st.get('data_as_of_overall') or st.get('serving_as_of'))}")
            sch = st.get("scheduler") or {}
            if sch:
                label = sch.get("profile_label") or sch.get("profile") or "-"
                state = "on" if sch.get("enabled") else f"OFF ({sch.get('reason') or 'scheduler disabled'})"
                lines.append(f"Schedule: {label}, scheduler {state}")
            else:  # API older than 1.28 (no scheduler block)
                profs, active = self.profiles()
                label = next((p.get("label") for p in profs if p.get("id") == active), active) or "-"
                lines.append(f"Schedule: {label}, scheduler state unknown (API without /system/status scheduler)")
            pe = self._primary_entry()
            if pe:
                lines.append(f"Next refresh: {fmt_when(pe.get('next_run'))}")
        lines.append(f"Gap filler: {self.filler_state()}")
        sy = self.sync_status()
        if sy:
            pr = sy.get("progress") or {}
            if sy.get("state") in ("running", "scanning", "publishing"):
                lines.append(f"Data sync: running ({pr.get('jobs_done', 0)}/{pr.get('jobs_total', 0)})")
            else:
                lines.append(f"Data sync: {sy.get('result') or sy.get('state') or '-'} at {fmt_when(sy.get('finished_at'))}")
        lines.append(f"Live news: {self.live_news_state()}")
        lines.append(f"Live quotes: {self.live_quotes_state()}")
        if self.status is not None:
            alerts = self.status.get("ops_alerts") or []
            errs = sum(1 for a in alerts if a.get("severity") == "error")
            warns = sum(1 for a in alerts if a.get("severity") == "warn")
            lines.append(f"Alerts: {errs} error, {warns} warning" if (errs or warns) else "Alerts: none")
        if (self.update or {}).get("available"):
            lines.append(f"Update available: {self.update['latest']}")
        return lines

    def tooltip(self) -> str:
        if self.status is None:
            tip = "BiG Equity 360 - starting / API not reachable" if self.supervisor_running() else "BiG Equity 360 - stopped"
        else:
            sch = self.status.get("scheduler") or {}
            tip = (f"BiG Equity 360 - data {fmt_date(self.status.get('data_as_of_overall'))}"
                   f" - {sch.get('profile') or '-'}{'' if sch.get('enabled') else ' (scheduler off)'}")
            if self.is_paused():
                tip += " - paused"
        return tip[:127]

    def profiles(self) -> tuple[list[dict[str, Any]], str | None]:
        if self.schedule:
            return list(self.schedule.get("profiles") or []), self.schedule.get("active")
        try:  # API down: local config / state (same files the API reads)
            from app.ops import schedule as sched  # noqa: PLC0415

            cfg = sched.load_config(self.s)
            active, _ = sched.active_profile(self.s)
            return ([{"id": k, "label": (v or {}).get("label", k), "placeholder": bool((v or {}).get("placeholder"))}
                     for k, v in cfg["profiles"].items()], active)
        except Exception:
            return [], None

    def menu(self) -> list[MenuItem]:
        status = MenuItem("status", "Status", children=[MenuItem("", ln, enabled=False) for ln in self.status_lines()]
                          + [SEP, MenuItem("status:refresh", "Refresh status")])
        profs, active = self.profiles()
        sched_items = [MenuItem(f"schedule:{p['id']}", str(p.get("label") or p["id"]), checked=p["id"] == active,
                                enabled=not p.get("placeholder")) for p in profs]
        items = [MenuItem("open", "Open dashboard", default=True), status, SEP,
                 MenuItem("refresh", "Run refresh now"),
                 MenuItem("sync", "Sync now (fetch what is missing)"),
                 MenuItem("pause", "Resume background jobs" if self.is_paused() else "Pause background jobs",
                          checked=self.is_paused()),
                 MenuItem("livenews", "Live news on/off", checked=self.live_news_on()),
                 MenuItem("livequotes", "Live quotes on/off", checked=self.live_quotes_on()),
                 MenuItem("schedule", "Schedule", children=sched_items or [MenuItem("", "(unavailable)", enabled=False)]),
                 SEP, MenuItem("news", "News Terminal"), MenuItem("health", "Data health"),
                 MenuItem("logs", "Open logs folder"),
                 MenuItem("autostart", "Start at login", checked=autostart.is_enabled())]
        if (self.update or {}).get("available"):
            items.append(MenuItem("update", f"Update available: {self.update['latest']}"))
        items += [SEP, MenuItem("quit", "Quit BiG360")]
        return items

    # ------------------------------------------------------------------ handlers
    def dispatch(self, cmd: str) -> str | None:
        if cmd == "open":
            return self.open_dashboard()
        if cmd == "status:refresh":
            self.poll()
            return None
        if cmd == "refresh":
            return self.run_refresh()
        if cmd == "sync":
            return self.run_sync()
        if cmd == "pause":
            return self.toggle_pause()
        if cmd == "livenews":
            return self.toggle_live_news()
        if cmd == "livequotes":
            return self.toggle_live_quotes()
        if cmd == "news":
            return self.open_dashboard("/news")
        if cmd.startswith("schedule:"):
            return self.set_schedule(cmd.split(":", 1)[1])
        if cmd == "health":
            return self.open_dashboard("/system/data-health")
        if cmd == "logs":
            return self.open_logs()
        if cmd == "autostart":
            return self.toggle_autostart()
        if cmd == "update":
            return self.open_update()
        if cmd == "quit":
            return self.quit()
        return None

    def open_dashboard(self, path: str = "/") -> str | None:
        self.open_url(self.base + path)
        if self.status is None and not self.supervisor_running():
            return "BiG360 is not running: start it from the BiG360 shortcut."
        return None

    def open_logs(self) -> None:
        p = self.s.logs_dir
        p.mkdir(parents=True, exist_ok=True)
        self.open_path(str(p))
        return None

    def run_refresh(self, entry_id: str | None = None) -> str:
        from app.ops.worker_schedule import request_refresh  # noqa: PLC0415

        hb = self.worker_heartbeat()
        if hb.get("alive") and not (hb.get("accepts_refresh_request") and hb.get("scheduler_enabled")):
            # worker without the refresh hook (older build / --no-schedule): queue the nightly DAG instead
            from app.core.jobqueue import enqueue_dag  # noqa: PLC0415

            req = enqueue_dag("nightly", params={"max_days": 10}, trigger="manual", settings=self.s)
            return f"Refresh queued (nightly DAG, request {req['request_id']})."
        request_refresh(self.s, entry_id, by="tray")
        if not hb.get("alive"):
            return "Refresh requested: it starts as soon as the worker is running."
        pe = self._primary_entry()
        what = (pe.get("description") or pe.get("id")) if pe else "the active profile's refresh"
        return f"Refresh starting within a few seconds: {what}."

    def toggle_pause(self) -> str:
        if self.is_paused():
            try:
                self.pause_flag.unlink()
            except FileNotFoundError:
                pass
            return "Background jobs resumed (gap filler)."
        from app.ops.pause import DEFAULT_HOURS, write_pause  # noqa: PLC0415

        st = write_pause(self.pause_flag, reason="paused from the tray", by="tray", hours=DEFAULT_HOURS)
        return (f"Background jobs paused until {(st.until or '')[:16].replace('T', ' ')} UTC: the gap filler stops its "
                "tasks within ~15 s. Scheduled refreshes still run.")

    def set_schedule(self, profile: str) -> str:
        try:
            self.schedule = self.api.put("/api/v1/system/schedule", {"profile": profile})
            active = (self.schedule or {}).get("active", profile)
        except urllib.error.HTTPError as e:
            return f"Schedule not changed: {_short_err(e)}"
        except Exception:
            from app.ops.schedule import ScheduleError, set_active_profile  # noqa: PLC0415

            try:  # API down: write the same state file (the worker hot-reloads it)
                set_active_profile(profile, self.s, by="tray")
            except ScheduleError as e:
                return f"Schedule not changed: {e}"
            active = profile
        return f"Schedule set to {active}. The worker applies it within a few seconds."

    def toggle_autostart(self) -> str:
        if autostart.is_enabled():
            autostart.disable()
            return "BiG360 will no longer start at login."
        cmd = autostart.enable(autostart.launch_command(self.s.project_root))
        return f"BiG360 starts at login ({cmd})."

    def open_update(self) -> str | None:
        u = self.update or {}
        target = u.get("file") or u.get("folder")
        if target:
            self.open_path(str(Path(target).parent if u.get("file") else target))
        return None

    def _send_stop(self) -> None:
        (self.ops_dir / "supervisor.STOP").write_text(f"stop by tray {datetime.now(UTC).isoformat()}", encoding="utf-8")
        self._stop_sent = True

    def _own_supervisor_alive(self) -> bool:
        """The supervisor this tray started is still alive (it may not hold its lock yet while importing)."""
        if not self.supervisor_pid:
            return False
        from app.ops import winsys  # noqa: PLC0415

        return winsys.pid_alive(self.supervisor_pid)

    def quit(self) -> str:
        self.quitting = True
        self._stop_sent = False
        if self.supervisor_running():
            self._send_stop()
            return "Stopping BiG360: running jobs finish or cancel (up to ~5 min)."
        if self._own_supervisor_alive():
            return "Stopping BiG360 (still starting)..."
        return "BiG360 stopped."

    def wait_stopped(self, timeout: float = 420.0, step: float = 1.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.supervisor_running():
                if self.quitting and not getattr(self, "_stop_sent", False):
                    self._send_stop()  # it took its lock after Quit was chosen (start-up race)
            elif not (self.quitting and self._own_supervisor_alive()):
                return True
            time.sleep(step)
        return not self.supervisor_running()

    # ------------------------------------------------------------------ supervisor management
    def supervisor_command(self) -> list[str]:
        exe = Path(sys.executable)
        py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists() else exe
        cmd = [str(py), "-m", "app.ops.supervisor", "--app", "--api-host", str(self.cfg["api_host"]),
               "--api-port", str(int(self.cfg["api_port"]))]
        cmd.append("--serve-ui" if self.cfg.get("serve_ui", True) else "--no-serve-ui")
        if not self.cfg.get("filler", True):
            cmd.append("--no-filler")
        if not self.cfg.get("schedule", True):
            cmd.append("--no-schedule")
        if self.cfg.get("realtime"):
            cmd.append("--realtime")
        cmd.append("--live-news" if self.cfg.get("live_news", True) else "--no-live-news")
        cmd.append("--live-quotes" if self.cfg.get("live_quotes", True) else "--no-live-quotes")
        return cmd

    def start_supervisor(self) -> int | None:
        """Start the supervisor detached with a hidden console of its own (CTRL+BREAK to its children works)."""
        if self.supervisor_running():
            return None
        logs = app_logs_dir(self.s)
        out = open(logs / "supervisor.stdout.log", "ab")  # noqa: SIM115 - handed to the child
        kw: dict[str, Any] = {"cwd": str(self.s.project_root / "backend"), "stdout": out, "stderr": subprocess.STDOUT,
                              "stdin": subprocess.DEVNULL, "env": {**os.environ, "BIG360_APP": "1"}}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kw["startupinfo"] = si
            kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            p = self._popen(self.supervisor_command(), **kw)
        finally:
            out.close()
        self.supervisor_pid = getattr(p, "pid", None)
        return self.supervisor_pid

    def ensure_supervisor(self) -> str | None:
        """Called periodically: (re)start the supervisor with back-off when it is not running. Returns a message when
        it had to be restarted."""
        if self.quitting or not self.cfg.get("start_supervisor", True) or self.supervisor_running():
            return None
        now = time.monotonic()
        if now < self.sup_next_try:
            return None
        first = self.supervisor_pid is None and self.sup_restarts == 0
        self.start_supervisor()
        # the first window also covers a slow cold start (imports) before the new supervisor holds its lock
        self.sup_next_try = now + (45, 60, 180, 600)[min(self.sup_restarts, 3)]
        self.sup_restarts += 1
        return None if first else "BiG360 supervisor was not running: restarted."


def ensure_models_link(settings: Settings | None = None) -> str | None:
    """Installed / portable app: <root>/models_store must reach the trained models, which live next to the data folder
    (or BIG360_MODELS_DIR). Creates a directory junction (no admin rights) when <root>/models_store is missing.
    Returns the linked source, or None when nothing was done."""
    s = settings or get_settings()
    link = s.project_root / "models_store"
    if sys.platform != "win32":
        return None
    src = Path(os.environ.get("BIG360_MODELS_DIR") or (s.data_dir.parent / "models_store"))
    if not src.is_dir() or (link.exists() and src.resolve() == link.resolve()):
        return None
    if link.exists():
        if link.is_junction() or link.is_symlink() or any(link.iterdir()):
            return None
        link.rmdir()  # empty placeholder (e.g. created by Settings.ensure_dirs): replace it with the junction
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(src)], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return str(src) if r.returncode == 0 else None


def _short_err(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = json.loads(e.read() or b"{}")
            return f"HTTP {e.code}: {body.get('detail') or body.get('title') or e.reason}"
        except Exception:
            return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)[:80]
    return f"{type(e).__name__}"[:80]
