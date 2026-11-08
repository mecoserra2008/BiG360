"""API latency benchmark (owner: api). ARCHITECTURE §12 budget: p95 < 300 ms per call; Company 360 overview < 1.5 s.

Run from backend/:
    ..\\.venv\\Scripts\\python.exe -m tests.perf_api                       # synthetic fixture (300 securities, 10y)
    ..\\.venv\\Scripts\\python.exe -m tests.perf_api --n 1000 --iters 40
    ..\\.venv\\Scripts\\python.exe -m tests.perf_api --data-dir ..\\data    # against the live snapshot (read-only)

Each endpoint is called `iters` times in-process (TestClient; includes routing, middleware, JSON, gzip) with the
response cache cleared before every call ("cold" = real DuckDB work) and once more warm (cache hit). Company endpoints
rotate over random securities. Prints p50 / p95 / max per endpoint and exits non-zero if any cold p95 >= budget.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="fixture securities")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--data-dir", help="use an existing data dir with a published snapshot instead of the fixture")
    ap.add_argument("--budget-ms", type=float, default=300.0)
    a = ap.parse_args()

    tmp = None
    if a.data_dir:
        os.environ["BIG360_DATA_DIR"] = str(Path(a.data_dir).resolve())
    else:
        tmp = tempfile.mkdtemp(prefix="perf_api_")
        os.environ["BIG360_DATA_DIR"] = str(Path(tmp) / "data")
    from app.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    s = get_settings()
    s.ensure_dirs()
    if not a.data_dir:
        from app.api.serving import build_serving_db
        from tests.fixtures.serving_small import build_lake

        t0 = time.perf_counter()
        build_lake(s, n_securities=a.n, years=a.years)
        t1 = time.perf_counter()
        rep = build_serving_db(s)
        print(f"fixture lake: {t1 - t0:.1f}s   snapshot build: {rep.seconds:.1f}s   "
              f"({rep.tables.get('sec', 0)} securities, {rep.tables.get('prices', 0):,} price rows, "
              f"{rep.path.stat().st_size / 1e6:.0f} MB)")

    from fastapi.testclient import TestClient

    from app.api.deps import current_snapshot, response_cache
    from app.api.main import create_app

    client = TestClient(create_app())
    snap = current_snapshot()
    cur = snap.con.cursor()
    sids = [r[0] for r in cur.execute("SELECT security_id FROM sec WHERE close IS NOT NULL ORDER BY market_cap_usd DESC NULLS LAST LIMIT 200").fetchall()]
    sector = cur.execute("SELECT sector_id FROM sec WHERE sector_id IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 1").fetchone()
    run_id = cur.execute("SELECT model_run_id FROM ml_prod ORDER BY horizon = '3M' DESC LIMIT 1").fetchone()
    if not sids:
        print("snapshot has no securities with prices; nothing to benchmark")
        return 1
    sector = sector[0] if sector else "TECH"
    rnd = random.Random(1)

    G = "GET"
    cases: list[tuple[str, str, str, dict | None, dict | None]] = [
        ("market/overview", G, "/api/v1/market/overview", None, None),
        ("market/overview US+EU", G, "/api/v1/market/overview", {"regions": ["US", "EU"]}, None),
        ("macro/regime", G, "/api/v1/macro/regime", None, None),
        ("macro/dashboard", G, "/api/v1/macro/dashboard", None, None),
        ("macro/series/{id}", G, "/api/v1/macro/series/fred%3ADGS10", {"transform": "yoy"}, None),
        ("sectors", G, "/api/v1/sectors", None, None),
        ("sectors/{id}", G, f"/api/v1/sectors/{sector}", None, None),
        ("sectors/{id}/scatter", G, f"/api/v1/sectors/{sector}/scatter", {"x": "roic", "y": "value"}, None),
        ("search", G, "/api/v1/search", {"q": "ap"}, None),
        ("screener/query default 100", "POST", "/api/v1/screener/query", None, {}),
        ("screener/query custom 1000", "POST", "/api/v1/screener/query", None,
         {"factor_weights": {"momentum": 2, "value": 1, "quality": 1}, "limit": 1000,
          "columns": ["isin", "ret_12m", "pe_ntm", "ev_ebit", "fcf_yield", "roic", "revenue_growth", "net_debt_ebitda",
                      "dividend_yield", "ml_expected_excess_3m"],
          "filters": [{"field": "market_cap_usd", "op": "gt", "value": 0}]}),
        ("screener/export", "POST", "/api/v1/screener/export", None, {"columns": ["roic"]}),
        ("insiders/feed", G, "/api/v1/insiders/feed", {"days": 365, "min_value_usd": 0}, None),
        ("insiders/top-signals", G, "/api/v1/insiders/top-signals", None, None),
        ("insiders/clusters", G, "/api/v1/insiders/clusters", {"days": 90}, None),
        ("events/feed", G, "/api/v1/events/feed", {"days": 30}, None),
        ("events/map", G, "/api/v1/events/map", {"days": 30}, None),
        ("events/timeline", G, "/api/v1/events/timeline", None, None),
        ("events/attention", G, "/api/v1/events/attention", None, None),
        ("models/runs", G, "/api/v1/models/runs", None, None),
        ("models/predictions", G, "/api/v1/models/predictions", {"limit": 200}, None),
        ("system/status", G, "/api/v1/system/status", None, None),
    ]
    if run_id:
        cases.append(("models/runs/{id}", G, f"/api/v1/models/runs/{run_id[0]}", None, None))
    company = ["overview", "prices", "financials", "ratios", "scores", "peers", "forecast", "moat", "insiders", "sentiment",
               "events", "technicals", "model", "macro", "filings"]
    for c in company:
        cases.append((f"companies/{{id}}/{c}", G, "/api/v1/companies/{sid}/" + c, None, None))
    cases.append(("companies/{id}/prices 10y W", G, "/api/v1/companies/{sid}/prices", {"from": "2016-01-01", "interval": "W"}, None))
    cases.append(("companies/{id}/financials Q USD", G, "/api/v1/companies/{sid}/financials", {"period": "Q", "currency": "USD"}, None))

    def once(method: str, url: str, params: dict | None, body: dict | None) -> float:
        u = url.replace("{sid}", rnd.choice(sids))
        t = time.perf_counter()
        r = client.request(method, u, params=params, json=body, headers={"Accept-Encoding": "gzip"})
        dt = (time.perf_counter() - t) * 1000
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {u} -> {r.status_code} {r.text[:200]}")
        return dt

    for _, m, u, p, b in cases[:3]:  # warm-up (imports, first queries)
        once(m, u, p, b)
    print(f"\n{'endpoint':40s} {'p50 ms':>8s} {'p95 ms':>8s} {'max ms':>8s} {'warm ms':>8s}")
    worst = 0.0
    rows = []
    for name, m, u, p, b in cases:
        ts = []
        for _ in range(a.iters):
            response_cache.clear()
            ts.append(once(m, u, p, b))
        warm = once(m, u.replace("{sid}", sids[0]), p, b) if "{sid}" not in u else None
        if warm is not None:
            warm = once(m, u, p, b)
        ts.sort()
        p50 = statistics.median(ts)
        p95 = ts[min(len(ts) - 1, int(round(0.95 * (len(ts) - 1))))]
        worst = max(worst, p95)
        rows.append((name, p50, p95, ts[-1], warm))
        print(f"{name:40s} {p50:8.1f} {p95:8.1f} {ts[-1]:8.1f} {warm if warm is not None else float('nan'):8.1f}")
    # Company 360 first paint: overview + prices in parallel (sequential upper bound here)
    page = []
    for _ in range(a.iters):
        response_cache.clear()
        sid = rnd.choice(sids)
        t = time.perf_counter()
        client.get(f"/api/v1/companies/{sid}/overview")
        client.get(f"/api/v1/companies/{sid}/prices")
        page.append((time.perf_counter() - t) * 1000)
    page.sort()
    p95_page = page[int(round(0.95 * (len(page) - 1)))]
    print(f"\nCompany 360 first paint (overview + prices, sequential) p50 {statistics.median(page):.0f} ms, p95 {p95_page:.0f} ms "
          f"(budget 1500 ms)")
    print(f"worst endpoint p95 {worst:.0f} ms (budget {a.budget_ms:.0f} ms) -> {'OK' if worst < a.budget_ms else 'OVER BUDGET'}")
    from app.api.deps import snapshots

    snapshots.reset()
    return 0 if worst < a.budget_ms and p95_page < 1500 else 2


if __name__ == "__main__":
    sys.exit(main())
