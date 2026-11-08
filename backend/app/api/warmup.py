"""API warm-up (owner: api; perf pass, v1.31).

After startup, and again after every serving-snapshot switch, one low-priority background thread replays the main
pages' API calls in-process, through the full ASGI stack (httpx ASGITransport). A user's first visit then finds:
  * the one-time Python costs paid: lazy imports inside the routers, the job registry, contracts, dq/TTL caches;
  * the snapshot columns those pages read in DuckDB's buffer pool and in the OS file cache;
  * the snapshot response cache (deps.SnapshotCacheMiddleware, 10 min TTL) filled for the exact URLs the UI sends.

This replaces the earlier full-table warm-up (every column of every serving table, about 6.5 GB, pulled into the
buffer pool). That one held the API at about 7 GB RSS, and it saturated DuckDB's shared worker pool for about 25 s
just when the first requests arrived.

Low priority and capped: one request at a time; before each request it waits (up to YIELD_WAIT_S) while user requests
are in flight (observability.METRICS: in-flight minus open SSE streams); it stops after WARM_BUDGET_S, when the
snapshot switches (the watcher then starts over on the new one), or on a 401/403. It never runs on the empty
snapshot. It is off with BIG360_API_WARMUP=0, with BIG360_AUTH=oidc, and under pytest unless BIG360_API_WARMUP=1.
DuckDB itself is capped by BIG360_API_DUCKDB_MEMORY (deps._apply_limits).

The request list mirrors the UI's default queries: the shell (top bar, live ticker, alerts), then the landing page
(/macro), then the main pages. Date-relative parameters use the same UTC rules as the frontend (isoDaysFromNow,
rangeStart '5Y', ISO week). If the UI changes a default, only that response-cache hit is lost; the DuckDB and import
warm-up still happen.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.logsetup import get_logger

log = get_logger("app.api.warmup")

START_DELAY_S = 2.0
WATCH_INTERVAL_S = 20.0
WARM_BUDGET_S = 180.0
YIELD_WAIT_S = 3.0
PAUSE_S = 0.02

#: POST /screener/query exactly as the Screener page sends it with default columns / weights (JSON.stringify order)
SCREENER_DEFAULT_BODY = (
    '{"universe_id":"full","columns":["isin","ret_12m","pe_ntm","ev_ebit","fcf_yield","roic","revenue_growth",'
    '"net_debt_ebitda","dividend_yield","macro_beta_rates","macro_beta_usd","macro_beta_oil","macro_beta_credit",'
    '"macro_beta_growth_surprise","macro_impact_rates_down_100bp"],"sort_by":"composite","sort_dir":"desc",'
    '"limit":1000,"offset":0,"factor_weights":{"quality":12,"profitability":12,"growth":11,"value":13,'
    '"balance_sheet":9,"momentum":11,"insider":7,"sentiment":7,"macro":8,"moat":10}}'
)

EVENT_THEMES = ("conflict_military", "geopolitical_tension", "sanctions", "trade_tariffs", "elections_politics",
                "protest_unrest", "monetary_policy", "inflation", "oil_gas", "natural_disaster", "cyber",
                "banking_financial_stress")

#: heavy modules that endpoints import lazily on first use and that no warm-up request reaches (needs a real run id):
#: Studio run export (statsmodels & co., ~3.5 s on the first GET /studio/runs/{id}/export.py)
WARM_IMPORTS = ("app.modeling.engine", "app.modeling.export")

Req = tuple[str, str, str | None]  # (method, path?query under /api/v1, raw JSON body)


def _years_back(d: date, n: int) -> date:
    """rangeStart('5Y') in the UI: same UTC month/day n years earlier (JS rolls 29 Feb over to 1 Mar)."""
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return date(d.year - n, 3, 1)


def warm_requests(today: date | None = None) -> list[Req]:
    """Main pages' API calls, in priority order (shell, landing page, then the other main pages)."""
    t = today or datetime.now(UTC).date()

    def d(n: int) -> str:
        return (t + timedelta(days=n)).isoformat()

    y5 = _years_back(t, 5).isoformat()
    mon = t - timedelta(days=t.weekday())

    def g(p: str) -> Req:
        return ("GET", p, None)

    shell = [g("/watchlists"), g("/system/status"), g("/live/status"), g("/live/filings?limit=15"),
             g("/live/news?limit=15"), g("/live/macro?limit=15"), g("/live/alerts?limit=15"), g("/alerts")]
    landing = [g("/live/macro?limit=8"), g("/macro/v2/heatmap"), g("/macro/v2/cycle?trail=12"),
               g("/macro/v2/surprise?theme=all&freq=W"), g("/macro/v2/recession?history_months=120"),
               g(f"/macro/v2/calendar?from={d(0)}&to={d(7)}")]
    pages = [
        # /macro/calendar, /macro/positioning(/:code), /screener (the slow pages of the 2026-10-20 QA run)
        g(f"/macro/v2/calendar?from={d(0)}&to={d(45)}"), g("/macro/v2/countries"),
        g("/positioning/markets"),
        g("/positioning/heatmap?report=legacy&metric=cot_index_3y&combined=false"),
        g("/positioning/crowded?combined=false&major=true&limit=12"),
        g(f"/positioning/markets/067651?combined=false&from={y5}"),
        g("/meta/taxonomy"), g("/meta/factors"), ("POST", "/screener/query", SCREENER_DEFAULT_BODY),
        # /macro/cb, /macro/cb/fed
        g("/macro/v3/central-banks"), g("/macro/v3/implied-paths"), g("/macro/v3/central-banks/fed?history_years=10"),
        g("/macro/v2/series/fred%3AUSREC?transform=level&from=1990-01-01"),
        # /macro/markets
        *[g(f"/market/overview?regions={r}") for r in ("US", "EU", "UK", "JP", "KR")],
        g("/market/overview?regions=CN&regions=HK"), g("/market/overview?regions=LATAM"), g("/market/overview"),
        g("/macro/regime?scope=GLOBAL"), g("/cross-asset/cockpit"),
        # /macro/cross-asset
        g("/cross-asset/risk"), g("/cross-asset/curves"), g("/cross-asset/curves/CL"),
        g("/cross-asset/correlation?window=120"), g(f"/cross-asset/assets/CL?from={y5}"),
        # /macro/country/US, /macro/theme/growth, /macro/nowcast, /macro/series
        g("/macro/v2/countries/US?years=10"), g("/macro/v2/surprise?countries=US&theme=all&freq=W"),
        g("/macro/v2/nowcast/US?quarters=4"), g(f"/macro/v2/calendar?from={d(0)}&to={d(30)}"),
        g("/events/v2/taxonomy"), g("/events/v2/labels/country/US?days=90&freq=day&stories=10"),
        g("/macro/v2/themes/growth?years=10"),
        g("/macro/v2/series/fred%3ACPIAUCSL?transform=level&vintages=false"),
        g("/macro/v2/compare?indicator=cpi_yoy&countries=US%2CEA%2CGB%2CJP&years=10"),
        # /sectors
        g("/sectors"), g("/sectors/TECH"),
        # /smart-money/*, /insiders
        g("/smart-money/overview?limit=15"), g("/smart-money/eu-shorts?limit=200"),
        g("/smart-money/activism?days=90&event_types=new_13d%2Cswitch_13g_to_13d%2Camend_13d"),
        g("/smart-money/sectors"),
        g("/insiders/feed?days=90&open_market_only=false&min_value_usd=50000&limit=500"),
        g("/insiders/top-signals?direction=bullish&limit=200"), g("/insiders/clusters?days=90"),
        # /events
        g("/events/v2/map?days=7&slider_days=90&slider_freq=day"), g("/events/v2/shocks?days=14&min_z=2&limit=50"),
        g("/events/models/changepoints?days=30&limit=100"), g("/events/v2/stories?days=7&sort=salience&limit=60"),
        g("/events/v2/map?days=7&slider_days=7"),
        *[g(f"/events/v2/map?days=7&theme={th}&slider_days=7") for th in EVENT_THEMES],
        # /earnings, /earnings/season
        g(f"/earnings/calendar?from={mon.isoformat()}&to={(mon + timedelta(days=6)).isoformat()}"
          "&universe=mvp_us&types=earnings%2Cdividend%2Csplit"),
        g("/earnings/season"),
        # /models, /studio, /backtest, /theses, /watchlist, /system, /system/data-health
        g("/models/runs"), g("/studio/catalog/methods"), g("/studio/catalog/datasets"), g("/backtest/strategies"),
        g("/backtest/catalog"), g("/theses"), g("/theses/stats"), g("/alerts/events?limit=200"),
        g("/system/jobs/runs?limit=100"), g("/system/schedule"), g("/system/data-health"),
    ]
    return shell + landing + pages


def enabled() -> bool:
    v = os.environ.get("BIG360_API_WARMUP", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if "pytest" in sys.modules and v not in ("1", "true", "yes", "on"):
        return False  # tests build many apps over temp snapshots and assert on metrics / caches
    try:  # bearer auth on: unauthenticated replays would only collect 401s
        from app.core.config import get_settings

        if getattr(get_settings(), "auth_mode", "none") != "none":
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def _busy() -> int:
    from app.api.observability import METRICS

    return METRICS.in_flight - METRICS.sse_open


async def run_once(app: Any, run_id: str | None = None, budget_s: float = WARM_BUDGET_S,
                   requests: list[Req] | None = None) -> dict[str, Any]:
    """Replay the warm-up requests once against `app` (sequentially, yielding to user traffic)."""
    import httpx

    from app.api.deps import current_snapshot

    t0 = time.monotonic()
    reqs = list(requests if requests is not None else warm_requests())
    out: dict[str, Any] = {"ok": 0, "failed": 0, "stopped": None, "slowest": []}
    timings: list[tuple[float, str]] = []
    headers = {"accept": "application/json", "accept-encoding": "identity"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1/api/v1", timeout=120.0) as c:
        i = 0
        while i < len(reqs):
            if time.monotonic() - t0 > budget_s:
                out["stopped"] = "budget"
                break
            if run_id is not None and current_snapshot().run_id != run_id:
                out["stopped"] = "snapshot switched"
                break
            wait_until = time.monotonic() + YIELD_WAIT_S
            while _busy() > 0 and time.monotonic() < wait_until:
                await asyncio.sleep(0.1)
            method, url, body = reqs[i]
            i += 1
            h = {**headers, "x-request-id": f"warmup-{i}"}
            if body is not None:
                h["content-type"] = "application/json"
            t = time.monotonic()
            try:
                r = await c.request(method, url, content=body.encode() if body is not None else None, headers=h)
            except Exception as e:  # noqa: BLE001 - one bad endpoint never stops the warm-up
                out["failed"] += 1
                log.debug("warm-up %s %s: %s", method, url, e)
                continue
            timings.append(((time.monotonic() - t) * 1000.0, url))
            if r.status_code in (401, 403):
                out["stopped"] = "auth required"
                break
            out["ok" if r.status_code < 400 else "failed"] += 1
            if method == "GET" and url == "/watchlists" and r.status_code == 200:
                try:  # the shell also loads the first watchlist (uncached: only pays its first-call costs)
                    items = r.json()
                    items = items.get("items", items) if isinstance(items, dict) else items
                    wid = next((x.get("watchlist_id") for x in items if isinstance(x, dict)), None)
                    if wid:
                        reqs.insert(i, ("GET", f"/watchlists/{wid}", None))
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(PAUSE_S)
    out["requests"] = out["ok"] + out["failed"]
    out["seconds"] = round(time.monotonic() - t0, 2)
    out["slowest"] = [f"{u} {ms:.0f}ms" for ms, u in sorted(timings, reverse=True)[:5]]
    return out


def _warm_imports() -> None:
    import importlib

    for m in WARM_IMPORTS:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            log.debug("warm-up import %s: %s", m, e)


def _loop(app: Any) -> None:
    from app.api.deps import current_snapshot

    time.sleep(START_DELAY_S)
    warmed: str | None = None
    while True:
        try:
            snap = current_snapshot()
            if not snap.is_empty and snap.run_id != warmed:
                warmed = snap.run_id
                res = asyncio.run(run_once(app, snap.run_id))
                _warm_imports()
                log.info("API warm-up of %s: %s requests ok, %s failed in %.1f s%s; slowest: %s", snap.run_id,
                         res["ok"], res["failed"], res["seconds"],
                         f" (stopped: {res['stopped']})" if res["stopped"] else "", ", ".join(res["slowest"]))
                if res["stopped"] == "snapshot switched":
                    warmed = None
                    continue
        except Exception as e:  # noqa: BLE001 - the warm-up must never take the API down
            log.warning("API warm-up failed: %s", e)
        time.sleep(WATCH_INTERVAL_S)


_started = threading.Event()


def start_warmup(app: Any) -> bool:
    """Start the warm-up watcher thread once per process (no-op when disabled)."""
    if _started.is_set() or not enabled():
        return False
    _started.set()
    threading.Thread(target=_loop, args=(app,), name="api-warmup", daemon=True).start()
    return True
