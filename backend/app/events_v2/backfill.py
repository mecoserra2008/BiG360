"""E5 pilot / E6 deep backfill driver (detached, newest-first, resumable, polite).

    python -m app.events_v2.backfill --label pilot --start 2026-07-14 --end 2026-10-11
    python -m app.events_v2.backfill --label e6 --start 2020-01-01 --end 2026-07-13 --batch-days 7

* Days already checkpointed in data/state/events_v2.json (`days`) are skipped (resumable). Order: newest first.
* Raw GDELT files are never written to disk (in-memory streaming): the E6 "<= 20 GB unprocessed raw on disk" cap is
  satisfied by construction (0 bytes); in-flight memory is bounded to one downloading day + one assembling day
  (~1.5 GB zipped worst case, typically < 1 GB RSS in the main process).
* Batches of `--batch-days` days: after each batch the T1 z-scores of the batch are recomputed, a batch record
  (rows, bytes, throughput, checksums) is appended to the state's `backfill.batches`, and progress is logged to
  data/logs/events_v2/backfill.log. Processed tables are usable as soon as each day lands.
* Pauses while build_serving_db runs (runner job lock) and while data/state/events_v2/PAUSE is set by the nightly
  gdelt_stream (checked before every file download and between days; see app.events_v2.pause); stops cleanly when data/state/events_v2/STOP exists;
  one instance at a time (data/.locks/events_v2_backfill.lock).
* --wait-for <label>: start only after that run is done (E6 chained after the E5 pilot); --retention: compact /
  prune finished months older than the T2 window after each batch (events_retention rules), so the deep backfill
  lands directly in its long-term (salient-only) form. A final `jobs run events_agg --param full=true` converges
  the z-scores.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from filelock import FileLock, Timeout

from app.core.config import get_settings
from app.core.jobqueue import job_is_running
from app.events_v2 import store
from app.events_v2.pause import pause_active
from app.events_v2.pipeline import Streamer, read_state, recompute_z, run_days, update_state


def _logger(path) -> logging.Logger:  # noqa: ANN001
    lg = logging.getLogger("events_v2.backfill")
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(sh)
    return lg


def day_checksum(d: date, settings: Any) -> str:
    """sha1 over the day files' sizes + names (cheap integrity fingerprint recorded per batch)."""
    h = hashlib.sha1()
    for t in store.V2_TABLES:
        f = store.day_file(t, d, settings)
        if f.exists():
            h.update(f"{t}:{f.stat().st_size};".encode())
    return h.hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", required=True, help="pilot | e6 | ... (state section)")
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--batch-days", type=int, default=7)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--dl-threads", type=int, default=8)
    ap.add_argument("--redo", action="store_true", help="re-process days already checkpointed")
    ap.add_argument("--wait-for", default=None, help="wait until backfill_runs.<label> is done (chained runs)")
    ap.add_argument("--retention", action="store_true", help="apply events_retention to finished old months after each batch")
    a = ap.parse_args(argv)
    s = get_settings()
    log = _logger(s.logs_dir / "events_v2" / "backfill.log")
    lock = FileLock(str(s.locks_dir / "events_v2_backfill.lock"), timeout=0)
    if a.wait_for:
        log.info("backfill %s: waiting for run %r to finish", a.label, a.wait_for)
        while True:
            run = read_state(s).get("backfill_runs", {}).get(a.wait_for, {})
            if run.get("status") in ("done", "stopped", "failed") and not (s.state_dir / "events_v2" / "STOP").exists():
                break
            time.sleep(60)
    try:
        lock.acquire(timeout=3600 if a.wait_for else 0)
    except Timeout:
        log.error("another backfill holds %s - exiting", lock.lock_file)
        return 2
    stop = s.state_dir / "events_v2" / "STOP"
    try:
        done = set(read_state(s).get("days", {})) if not a.redo else set()
        days = [a.end - timedelta(days=i) for i in range((a.end - a.start).days + 1)]
        todo = [d for d in days if d.isoformat() not in done]
        log.info("backfill %s: %s..%s, %d days, %d to do (newest first), batch %d", a.label, a.start, a.end,
                 len(days), len(todo), a.batch_days)

        def mark(st: dict[str, Any]) -> None:
            sec = st.setdefault("backfill_runs", {}).setdefault(a.label, {})
            sec.update({"start": a.start.isoformat(), "end": a.end.isoformat(), "status": "running",
                        "pid": __import__("os").getpid(), "started_at": datetime.now(UTC).isoformat(timespec="seconds")})
        update_state(mark, s)

        paused = {"flag": False}

        def flag_pause() -> bool:
            """PAUSE flag (nightly gdelt_stream has priority): checked before every file download and every day."""
            on = pause_active(s)
            if on != paused["flag"]:
                paused["flag"] = on
                log.info("PAUSE flag %s - %s", "set" if on else "cleared",
                         "yielding GDELT bandwidth to the nightly stream" if on else "resuming downloads")
            return on

        def pause() -> bool:
            busy = job_is_running("build_serving_db", s)
            if busy:
                log.info("build_serving_db running - pausing")
            return busy or flag_pause()

        def on_day(rep: dict[str, Any]) -> None:
            log.info("day %s: files %d/%d gb_zip %.2f events %d->%d articles %d (linked %d) stories %d "
                     "wall %.0fs dl %.0fs cpu %.0fs assemble %.0fs bytes %s", rep["day"], rep["files_ok"], rep["files"],
                     rep["gb_zip"], rep["events_parsed"], rep["events_kept"], rep["articles_kept"],
                     rep["articles_linked"], rep["stories"], rep["wall_s"], rep["download_wall_s"],
                     rep["worker_cpu_s"], rep["assemble_s"], rep.get("bytes"))

        streamer = None  # one process pool / labelling context for the whole run (no per-batch start-up cost)
        for i in range(0, len(todo), a.batch_days):
            if stop.exists():
                log.info("STOP file present - exiting")
                break
            batch = todo[i:i + a.batch_days]
            t0 = time.perf_counter()
            if streamer is None:
                streamer = Streamer(s, workers=a.workers, dl_threads=a.dl_threads).__enter__()
                streamer.pause_check = flag_pause
            prune_before = None
            if a.retention:
                from app.events_v2.retention import DEFAULTS  # noqa: PLC0415

                prune_before = datetime.now(UTC).date() - timedelta(days=DEFAULTS["t2_days"])
            res = run_days(batch, s, legacy=False, label=a.label, stop_file=stop, on_day=on_day, should_pause=pause,
                           streamer=streamer, prune_before=prune_before)
            got = [date.fromisoformat(x) for x in res["days"]]
            if got:
                recompute_z(min(got), max(got), s)
            if a.retention and got:
                from app.events_v2.retention import run_retention  # noqa: PLC0415

                rr = run_retention(s, {})
                log.info("retention: %s", {t: [m["month"] for m in v if isinstance(m, dict)] for t, v in rr["months"].items()})
            secs = time.perf_counter() - t0
            reps = res["reports"]
            rec = {"days": [d.isoformat() for d in batch], "processed": res["days"], "seconds": round(secs, 1),
                   "days_per_hour": round(len(got) / secs * 3600, 2) if got else 0,
                   "gb_zip": round(sum(r["gb_zip"] for r in reps), 3),
                   "events_parsed": sum(r["events_parsed"] for r in reps),
                   "events_kept": sum(r["events_kept"] for r in reps),
                   "articles_kept": sum(r["articles_kept"] for r in reps),
                   "files_missing": sum(r["files_missing"] for r in reps), "errors": sum(r["errors"] for r in reps),
                   "bytes": {t: sum((r.get("bytes") or {}).get(t, 0) for r in reps) for t in store.V2_TABLES},
                   "checksums": {d.isoformat(): day_checksum(d, s) for d in got},
                   "raw_on_disk_bytes": 0, "finished_at": datetime.now(UTC).isoformat(timespec="seconds")}

            def add(st: dict[str, Any], rec: dict[str, Any] = rec) -> None:
                st.setdefault("backfill_runs", {}).setdefault(a.label, {}).setdefault("batches", []).append(rec)
            update_state(add, s)
            log.info("batch done: %s", {k: rec[k] for k in ("processed", "seconds", "days_per_hour", "gb_zip",
                                                            "events_kept", "articles_kept", "bytes")})
            if len(got) < len(batch) and stop.exists():
                break
        if streamer is not None:
            streamer.__exit__(None, None, None)
        remaining = [d for d in todo if d.isoformat() not in set(read_state(s).get("days", {}))]
        if a.retention and not remaining:  # deep backfill complete: converge T1 z-scores over the whole history
            from app.events_v2.agg import run_agg  # noqa: PLC0415

            log.info("final events_agg (full): %s", run_agg(s, full=True))

        def fin(st: dict[str, Any]) -> None:
            sec = st.setdefault("backfill_runs", {}).setdefault(a.label, {})
            sec.update({"status": "done" if not remaining else "stopped", "remaining_days": len(remaining),
                        "finished_at": datetime.now(UTC).isoformat(timespec="seconds")})
        update_state(fin, s)
        log.info("backfill %s finished: remaining %d days", a.label, len(remaining))
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
