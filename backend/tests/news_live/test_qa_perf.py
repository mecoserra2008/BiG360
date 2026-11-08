"""QA smoke latency check for /news (first rows <= 1 s, live update render <= 100 ms)."""

from __future__ import annotations

import json

from app.qa import smoke as S


class FakeBrowser:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def evaluate(self, expr, await_promise=False):
        self.calls.append((expr, await_promise))
        return json.dumps(self.payload) if self.payload is not None else None


CFG = {"perf_checks": {"/news": {"first_rows_ms": 1000, "live_update_ms": 100, "samples": 3}}}


def test_perf_check_budget():
    assert S.load_config()["perf_checks"]["/news"]["first_rows_ms"] == 1000
    ok = FakeBrowser({"firstRowsMs": 620, "updates": [18.2, 25.0, 30.1]})
    st, issues, m = S.perf_check(ok, "/news", CFG)
    assert st == "passed" and issues == [] and m["live_update_ms_median"] == 25.0 and ok.calls[0][1] is True
    st, issues, _ = S.perf_check(FakeBrowser({"firstRowsMs": 1800, "updates": [20, 20, 20]}), "/news", CFG)
    assert st == "failed" and "first rows painted after 1800 ms" in issues[0]
    st, issues, _ = S.perf_check(FakeBrowser({"firstRowsMs": 500, "updates": [140, 150, 90]}), "/news?view=top", CFG)
    assert st == "failed" and "live update render 140 ms" in issues[0]
    assert S.perf_check(FakeBrowser({"firstRowsMs": None, "updates": []}), "/news", CFG)[0] == "warn"
    assert S.perf_check(FakeBrowser({"missing": True}), "/news", CFG)[0] == "warn"
    assert S.perf_check(FakeBrowser(None), "/macro", CFG) == ("passed", [], {})
