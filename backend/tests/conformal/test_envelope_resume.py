"""conformal_envelopes (refresh-pipeline fixes 2026-10-26): a run interrupted after some regions resumes with the
remaining ones (checkpoint per region), and the merged audit equals the audit of one uninterrupted run."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.conformal import envelope_job as EJ

AS_OF = date(2026, 10, 23)


def _fake_task(calls: list[str], fail_on: str | None = None):
    def task(args: tuple) -> dict:
        rg = args[0]
        if rg == fail_on:
            raise RuntimeError("killed")
        calls.append(rg)
        a = EJ.Audit()
        a.add("envelope", "ewma", "overall", "all", "joint", 90, np.array([1.0, 0.0, 1.0]), np.datetime64("2026-01-01"),
              np.datetime64("2026-06-01"))
        a.add("envelope", "ewma", "region", rg, "joint", 90, np.array([1.0]), np.datetime64("2026-01-01"),
              np.datetime64("2026-06-01"))
        return {"region": rg, "info": {"region": rg, "rows": 2}, "rows": 2, "last": AS_OF,
                "curves": [{"as_of_date": AS_OF, "pool_key": rg, "region": rg, "horizon": 1, "side": "up",
                            "kind": "normal", "n": 300, "probs": [0.5], "u": [1.0], "computed_at": None}],
                "audit": a.c}
    return task


@pytest.fixture()
def stubs(tmp_data, monkeypatch):
    monkeypatch.setattr(EJ.D, "universe_ids", lambda *a, **k: pl.DataFrame({"security_id": ["X"], "sector_id": ["S"]}))
    monkeypatch.setattr(EJ.D, "price_regions", lambda s: ["EU", "JP", "US"])
    monkeypatch.setattr(EJ.D, "vix_daily", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(EJ.D, "macro_states", lambda s: pl.DataFrame())
    monkeypatch.setattr(EJ, "prune_old", lambda s, cutoff: 0)
    writes: list[str] = []
    monkeypatch.setattr("app.core.storage.write_table", lambda df, table, **k: writes.append(table) or df.height)
    return tmp_data, writes


def test_resume_after_an_interrupted_run(stubs, monkeypatch):
    s, _ = stubs
    calls: list[str] = []
    monkeypatch.setattr(EJ, "_region_task", _fake_task(calls, fail_on="US"))
    with pytest.raises(RuntimeError):
        EJ.run_envelopes(s, as_of=AS_OF, parallel=1)            # killed in the third region
    assert calls == ["EU", "JP"]
    ck = EJ._ckpt_path(s, AS_OF, EJ.EMIT_DAYS, EJ.RADIUS_MODE)
    assert ck.exists()
    calls.clear()
    monkeypatch.setattr(EJ, "_region_task", _fake_task(calls))
    rep = EJ.run_envelopes(s, as_of=AS_OF, parallel=1)        # the retry: only US is computed
    assert calls == ["US"] and rep["resumed_regions"] == ["EU", "JP"]
    assert rep["rows"] == 6 and rep["curves"] == 3 and not ck.exists()
    # the audit equals one uninterrupted run: 3 regions x 3 overall scores
    calls.clear()
    full = EJ.run_envelopes(s, as_of=AS_OF, parallel=1)
    assert calls == ["EU", "JP", "US"]
    assert rep["coverage_rows"] == full["coverage_rows"]
    assert rep["overall"] == full["overall"]


def test_merge_audit_sums_counts():
    a, b = EJ.Audit(), EJ.Audit()
    a.add("e", "f", "overall", "all", "joint", 90, np.array([1.0, 0.0]), np.datetime64("2026-01-05"),
          np.datetime64("2026-02-01"))
    b.add("e", "f", "overall", "all", "joint", 90, np.array([1.0]), np.datetime64("2026-01-01"),
          np.datetime64("2026-03-01"))
    EJ._merge_audit(a, b.c)
    n, cv, d0, d1 = a.c[("e", "f", "overall", "all", "joint", "90")]
    assert (n, cv) == (3, 2) and d0 == np.datetime64("2026-01-01") and d1 == np.datetime64("2026-03-01")
