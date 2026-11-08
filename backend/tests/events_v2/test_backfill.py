"""Backfill driver: newest-first batches, checkpoint resume (skips done days), one streamer per run, batch records,
STOP file, chained --wait-for."""

from __future__ import annotations

from datetime import date

from app.events_v2 import backfill as B
from app.events_v2.pipeline import read_state, update_state


class _FakeStreamer:
    created = 0

    def __init__(self, *a, **k):
        _FakeStreamer.created += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _fake_run_days(calls):
    def run_days(days, s, **kw):
        calls.append(([d.isoformat() for d in days], kw.get("streamer")))
        reps = []
        for d in days:
            rep = {"day": d.isoformat(), "files": 576, "files_ok": 576, "files_missing": 0, "errors": 0, "gb_zip": 1.0,
                   "events_parsed": 10, "events_kept": 2, "articles_kept": 5, "bytes": {"news_stories": 100}}
            reps.append(rep)

            def upd(st, rep=rep):
                st.setdefault("days", {})[rep["day"]] = {"label": kw.get("label"), "files_ok": 576}
            update_state(upd, s)
        return {"days": [r["day"] for r in reps], "reports": reps, "seconds": 1.0, "days_per_hour": 1.0}
    return run_days


def test_backfill_batches_resume_and_stop(tmp_data, monkeypatch):
    calls: list = []
    monkeypatch.setattr(B, "Streamer", _FakeStreamer)
    monkeypatch.setattr(B, "run_days", _fake_run_days(calls))
    monkeypatch.setattr(B, "recompute_z", lambda *a, **k: 0)
    _FakeStreamer.created = 0
    update_state(lambda st: st.setdefault("days", {}).update({"2026-01-08": {"label": "x"}}), tmp_data)
    rc = B.main(["--label", "t", "--start", "2026-01-01", "--end", "2026-01-10", "--batch-days", "4"])
    assert rc == 0
    assert [c[0] for c in calls] == [["2026-01-10", "2026-01-09", "2026-01-07", "2026-01-06"],
                                     ["2026-01-05", "2026-01-04", "2026-01-03", "2026-01-02"], ["2026-01-01"]]
    assert _FakeStreamer.created == 1 and len({id(c[1]) for c in calls}) == 1        # one pool for the whole run
    st = read_state(tmp_data)
    run = st["backfill_runs"]["t"]
    assert run["status"] == "done" and run["remaining_days"] == 0 and len(run["batches"]) == 3
    assert run["batches"][0]["raw_on_disk_bytes"] == 0 and run["batches"][0]["events_kept"] == 8
    # resume: nothing left to do
    calls.clear()
    assert B.main(["--label", "t", "--start", "2026-01-01", "--end", "2026-01-10"]) == 0 and calls == []
    # STOP file: exits before the first batch
    stop = tmp_data.state_dir / "events_v2" / "STOP"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.touch()
    assert B.main(["--label", "u", "--start", "2025-12-01", "--end", "2025-12-05"]) == 0 and calls == []
    assert read_state(tmp_data)["backfill_runs"]["u"]["status"] == "stopped"
    stop.unlink()


def test_backfill_wait_for(tmp_data, monkeypatch):
    calls: list = []
    monkeypatch.setattr(B, "Streamer", _FakeStreamer)
    monkeypatch.setattr(B, "run_days", _fake_run_days(calls))
    monkeypatch.setattr(B, "recompute_z", lambda *a, **k: 0)
    update_state(lambda st: st.setdefault("backfill_runs", {}).update({"pilot": {"status": "done"}}), tmp_data)
    assert B.main(["--label", "e6", "--start", "2025-01-01", "--end", "2025-01-02", "--wait-for", "pilot"]) == 0
    assert [c[0] for c in calls] == [["2025-01-02", "2025-01-01"]]
    assert date.fromisoformat(read_state(tmp_data)["backfill_runs"]["e6"]["end"]) == date(2025, 1, 2)
