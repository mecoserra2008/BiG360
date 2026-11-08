"""Regression guard (docs/OPERATIONS_ROADMAP.md O2.3): keeps the app's pages and APIs at 100%.

Three suites, all read-only against the app:
  * api_sweep  - every GET operation of the OpenAPI contract with real ids (resolved from list endpoints / search):
                 status, response shape vs the served OpenAPI schema, plausibility (no NaN / inf, as-of dates not in
                 the future, snapshot / region as-of within the coverage.yaml SLA).
  * golden     - a small set of stable or bounded key facts (golden.yaml) with tolerances.
  * smoke      - headless Chrome (DevTools protocol) over every route and tab of the UI in both themes: console
                 errors, failed requests, error / empty panels where data is expected, load time, horizontal overflow.
                 Skipped (not failed) when Chrome or the UI is not available.

Entry points: job `qa_regression` (app.qa.regression.run), CLI `python -m app.qa` and scripts/smoke_ui.py.
Results: data/reports/qa/<run>/ (+ data/reports/smoke/<run>/ for the UI suite), summary in data/state/ops/qa_regression.json
(System / Data Health panel and an ops alert on failure).
"""
