"""Interactive studio lane (docs/STUDIO_V2_PLAN.md S1.1). Owner: model-studio.

    python -m app.modeling.lane            # supervisor child `studio` (app.ops.supervisor)

A warm process that runs Model Studio and backtest runs outside the worker's file queue and the exclusive heavy slot,
so an interactive run never waits behind a data job.

* Pool: BIG360_STUDIO_POOL (default 2) spawned worker processes with numpy / pandas / polars / duckdb / scipy /
  statsmodels / sklearn and the modeling + backtest engines imported once (no 4-5 s spawn + import per run). A worker
  whose memory grew beyond BIG360_STUDIO_RECYCLE_MB after a run is replaced while idle.
* CPU: the lane process (and so its workers) is pinned to cores - 2; thread pools (BLAS / OpenMP / Polars) and the
  tree `n_jobs` (BIG360_STUDIO_NJOBS) are sized to the lane's cores per worker. While a heavy worker job or the
  exclusive serving build runs (app.ops.heavy_slot markers), the lane lowers its priority to below normal.
* RAM: the supervisor runs it in its own Job Object with a committed-memory cap (BIG360_STUDIO_RAM_MB, default 6144).
  Each run is admitted through app.ops.ram_budget (budget BIG360_STUDIO_RUN_GB, default 1 GB, reservation held while
  it runs); a run blocked by RAM starts anyway after BIG360_STUDIO_RAM_WAIT_S (default 15 s) - the Job cap still
  protects the machine.
* Queue: files in data/state/studio_lane/inbox/<created>_<kind>_<run_id>.json. The lane claims one by an atomic rename
  into claimed/ (the API's fallback to the worker claims it the same way, so a run is executed once). FIFO.
* Status: data/state/studio_lane/lane.json (heartbeat every second: pid, workers, running runs, queue, priority,
  blocked_by). The API routes async runs here only while the heartbeat is fresh (`available()`).
* Each run executes with preview=True: a 10-20 % sample result first (preview.json), then the full run.
* Kill switch: BIG360_STUDIO_LANE=0 -> the API keeps using the worker jobs studio_run / backtest_run (contracts/jobs.yaml).
* Stop: file data/state/studio_lane/STOP (supervisor graceful stop) or SIGINT / SIGTERM / SIGBREAK.
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.modeling.lane")

HEARTBEAT_S = 1.0
FRESH_S = 10.0             # lane.json older than this = lane unavailable (API falls back to the worker)
TICK_S = 0.05
PRIORITY_EVERY_S = 5.0
CANCEL_KILL_S = 20.0       # a cancelled run that does not stop cooperatively within this is killed (worker recycled)
FALLBACK_AFTER_S = 30.0    # a lane run still queued this long while the lane is unavailable moves to the worker
KINDS = ("studio", "backtest")


def enabled() -> bool:
    return os.environ.get("BIG360_STUDIO_LANE", "1") != "0"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def pool_size() -> int:
    return max(1, min(_int_env("BIG360_STUDIO_POOL", 2), 4))


def lane_cores(cpu: int | None = None) -> int:
    cpu = cpu or os.cpu_count() or 4
    return max(1, cpu - 2)


def lane_dir(settings: Settings | None = None) -> Path:
    d = (settings or get_settings()).state_dir / "studio_lane"
    for sub in ("inbox", "claimed"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def stop_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "studio_lane" / "STOP"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read(p: Path) -> dict[str, Any] | None:
    for _ in range(3):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):
            time.sleep(0.02)
    return None


def _write(p: Path, obj: Any) -> None:
    tmp = p.with_name(f".{p.name}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    for i in range(8):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.02 * (2 ** i))
    os.replace(tmp, p)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        from app.ops.winsys import pid_running

        return bool(pid_running(int(pid)))
    except Exception:  # noqa: BLE001
        return True


# ------------------------------------------------------------------------------------------------ client side (API)


_status_cache: dict[str, Any] = {"at": 0.0, "key": None, "val": None}


def status(settings: Settings | None = None, max_age_s: float = 0.5) -> dict[str, Any] | None:
    """lane.json with `alive` (fresh heartbeat + live pid); None when the lane never ran. Cached for max_age_s."""
    s = settings or get_settings()
    key = str(s.state_dir)
    now = time.monotonic()
    if _status_cache["key"] == key and now - _status_cache["at"] < max_age_s:
        return _status_cache["val"]
    st = _read(s.state_dir / "studio_lane" / "lane.json")
    if st is not None:
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(st["heartbeat_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = 1e9
        st["heartbeat_age_s"] = round(age, 2)
        st["alive"] = bool(age < FRESH_S and not st.get("stopping") and _pid_alive(st.get("pid")))
    _status_cache.update(at=now, key=key, val=st)
    return st


def available(settings: Settings | None = None) -> bool:
    if not enabled():
        return False
    st = status(settings)
    return bool(st and st.get("alive"))


def _entry_name(kind: str, run_id: str) -> str:
    return f"{time.time_ns()}_{kind}_{run_id}.json"


def submit(kind: str, run_id: str, estimate_s: float | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """Queue a prepared run (status.json queued) on the lane."""
    if kind not in KINDS:
        raise ValueError(kind)
    d = lane_dir(settings)
    rec = {"kind": kind, "run_id": run_id, "estimate_s": estimate_s, "queued_at": _now()}
    _write(d / "inbox" / _entry_name(kind, run_id), rec)
    _status_cache["at"] = 0.0
    return rec


def _inbox(settings: Settings | None = None) -> list[Path]:
    try:
        return sorted((lane_dir(settings) / "inbox").glob("*.json"))
    except OSError:
        return []


def _entry_run(p: Path) -> tuple[str, str]:
    parts = p.stem.split("_", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("", "")


def withdraw(kind: str, run_id: str, settings: Settings | None = None) -> bool:
    """Claim a queued lane run back (fallback to the worker). True = it was still waiting and is now ours."""
    for p in _inbox(settings):
        if _entry_run(p) == (kind, run_id):
            try:
                os.replace(p, p.with_name(p.name + ".withdrawn"))
                (p.with_name(p.name + ".withdrawn")).unlink(missing_ok=True)
                return True
            except OSError:
                return False
    return False


def queue_s(settings: Settings | None = None, ahead_of: str | None = None) -> float:
    """Seconds until a new run (or `ahead_of`, a queued run id) would start: FIFO over the lane's slots."""
    st = status(settings)
    if not st or not st.get("alive"):
        return 0.0
    slots = [max(float(r.get("remaining_s") or 0.5), 0.2) for r in st.get("running") or []]
    slots += [0.0] * max(int(st.get("pool") or 1) - len(slots), 0)
    if not slots:
        slots = [0.0]
    for q in st.get("queue") or []:
        if ahead_of and q.get("run_id") == ahead_of:
            break
        i = min(range(len(slots)), key=lambda j: slots[j])
        slots[i] += float(q.get("estimate_s") or 1.0)
    return round(min(slots), 2)


def live_info(st: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Queue position / blocked-by / ETA of a run status (lane or worker path) for the API."""
    out: dict[str, Any] = {}
    est = st.get("estimate_s")
    if st.get("status") == "queued":
        if st.get("lane") == "lane":
            ls = status(settings) or {}
            ids = [q.get("run_id") for q in ls.get("queue") or []]
            pos = ids.index(st["run_id"]) + 1 if st["run_id"] in ids else (1 if ls.get("alive") else None)
            wait = queue_s(settings, ahead_of=st["run_id"])
            running = [f"{r.get('kind')} {r.get('run_id')}" for r in ls.get("running") or []]
            blocked = ls.get("blocked_by") or (", ".join(running) if running and wait > 0 else None)
            if not ls.get("alive"):
                blocked = "studio lane not running (falls back to the worker after 30 s)"
            run_s = float((st.get("estimate") or {}).get("run_s") or est or 0)
            out = {"queue": {"position": pos, "ahead": max((pos or 1) - 1, 0), "blocked_by": blocked,
                             "queue_s": wait}, "eta_s": round(wait + run_s, 1)}
        else:
            out = {"queue": _worker_queue(st, settings), "eta_s": est}
    elif st.get("status") == "running":
        try:
            el = (datetime.now(UTC) - datetime.fromisoformat(st["started_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            el = 0.0
        run_s = float((st.get("estimate") or {}).get("run_s") or est or 0)
        out = {"eta_s": round(max(run_s - el, 0.0), 1)}
    return out


def _worker_queue(st: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Worker path: what the run waits behind (exclusive heavy slot, processing jobs, queue position)."""
    blocked = None
    pos = None
    try:
        from app.core.jobqueue import list_requests
        from app.ops.heavy_slot import exclusive_holder

        ex = exclusive_holder(settings)
        proc = [r.get("job_id") for r in list_requests("processing", settings)]
        pend = list_requests("pending", settings)
        rid = st.get("queue_request_id")
        ids = [r.get("request_id") for r in pend]
        pos = ids.index(rid) + 1 if rid in ids else None
        if ex:
            blocked = f"exclusive heavy slot: {ex.get('job_id')}"
        elif proc:
            blocked = "worker busy: " + ", ".join(str(p) for p in proc[:3])
    except Exception:  # noqa: BLE001
        pass
    return {"position": pos, "ahead": max((pos or 1) - 1, 0), "blocked_by": blocked, "queue_s": None}


# ------------------------------------------------------------------------------------------------ worker process


def _warm() -> None:
    """Import the heavy libraries and both engines once (the point of a warm lane)."""
    for m in ("numpy", "pandas", "polars", "duckdb", "scipy", "scipy.stats", "statsmodels.api", "sklearn.ensemble",
              "sklearn.linear_model", "sklearn.model_selection"):
        try:
            __import__(m)
        except Exception:  # noqa: BLE001, S110 - optional
            pass
    from app.backtest import runs  # noqa: F401
    from app.modeling import engine  # noqa: F401
    from app.modeling.registry import all_methods

    all_methods()
    # first-use costs measured in a fresh worker (~5 s, mostly parsing the ~190 table contracts): pay them here
    try:
        from app.core.schema import load_contracts
        from app.modeling.datasets import snapshot_id

        load_contracts()
        snapshot_id()
        import duckdb

        duckdb.connect().execute("SELECT 1").fetchall()
    except Exception:  # noqa: BLE001, S110 - warm-up is best effort
        pass


def _worker_main(conn: Any, slot: int) -> None:  # pragma: no cover - runs in a spawned process
    from app.core.logsetup import setup_logging

    try:
        setup_logging(log_file="studio_lane.log", service=f"studio-w{slot}")
    except Exception:  # noqa: BLE001
        pass
    _warm()
    conn.send(("ready", os.getpid()))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        kind, run_id = msg
        st = "failed"
        try:
            if kind == "studio":
                from app.modeling import engine

                st = engine.execute(run_id, preview=True).get("status", "failed")
            else:
                from app.backtest import runs

                st = runs.execute(run_id, preview=True).get("status", "failed")
        except BaseException as e:  # noqa: BLE001 - the run's own errors become status=failed inside execute
            log.exception("lane run %s crashed outside execute", run_id)
            st = f"failed: {type(e).__name__}"
        mem = None
        try:
            from app.ops.winsys import process_memory_mb

            mem = process_memory_mb(os.getpid())
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.send(("done", run_id, st, mem))
        except (EOFError, OSError):
            return


# ------------------------------------------------------------------------------------------------ lane process


def _set_affinity(n_cores: int) -> bool:
    """Pin this process (and the workers it spawns: they inherit) to n_cores, leaving cores 0..1 to the rest."""
    cpu = os.cpu_count() or 1
    if n_cores >= cpu:
        return False
    cores = list(range(cpu - n_cores, cpu))
    try:
        if os.name == "nt":
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            mask = sum(1 << c for c in cores)
            return bool(k32.SetProcessAffinityMask(k32.GetCurrentProcess(), mask))
        os.sched_setaffinity(0, set(cores))  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        return False


def lane_env(pool: int | None = None, cpu: int | None = None) -> dict[str, str]:
    """Thread pools of each lane worker: the lane's cores shared by the pool."""
    n = max(1, lane_cores(cpu) // (pool or pool_size()))
    env = {k: str(n) for k in ("POLARS_MAX_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                               "NUMEXPR_MAX_THREADS", "BIG360_DUCKDB_THREADS")}
    env["BIG360_STUDIO_NJOBS"] = str(n)
    # DuckDB memory per worker: 40 % of the lane's Job Object cap shared by the pool (the cap is hard)
    cap_mb = _float_env("BIG360_STUDIO_RAM_MB", 6144.0)
    env["BIG360_DUCKDB_MEMORY"] = env["BIG360_SERVING_DUCKDB_MEMORY"] = f"{max(256, int(cap_mb * 0.4 / (pool or pool_size())))}MiB"
    env["BIG360_IN_STUDIO_LANE"] = "1"
    return env


class _Slot:
    def __init__(self, ctx: Any, idx: int) -> None:
        self.ctx, self.idx = ctx, idx
        self.proc: Any = None
        self.conn: Any = None
        self.ready = False
        self.pid: int | None = None
        self.run: dict[str, Any] | None = None   # {kind, run_id, started, estimate_s, reservation, cancel_seen}
        self.recycle = False
        self.started_at = 0.0

    def start(self) -> None:
        parent, child = self.ctx.Pipe()
        self.proc = self.ctx.Process(target=_worker_main, args=(child, self.idx), name=f"studio-lane-w{self.idx}",
                                     daemon=True)
        self.proc.start()
        child.close()
        self.conn, self.ready, self.pid, self.run, self.recycle = parent, False, self.proc.pid, None, False
        self.started_at = time.monotonic()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def stop(self, kill: bool = False) -> None:
        if self.proc is None:
            return
        try:
            if not kill and self.conn is not None:
                self.conn.send(None)
                self.proc.join(3)
        except (OSError, EOFError, BrokenPipeError):
            pass
        if self.proc.is_alive():
            self.proc.kill()
            self.proc.join(3)
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.proc = None


class Lane:
    def __init__(self, settings: Settings | None = None, pool: int | None = None) -> None:
        import multiprocessing as mp

        self.s = settings or get_settings()
        self.dir = lane_dir(self.s)
        self.ctx = mp.get_context("spawn")
        self.pool = pool or pool_size()
        self.slots = [_Slot(self.ctx, i) for i in range(self.pool)]
        self.stop_event = threading.Event()
        self.started_at = _now()
        self.priority = "normal"
        self.blocked_by: str | None = None
        self.blocked_since: float | None = None
        self.done = 0
        self.failed = 0
        self.recycle_mb = _float_env("BIG360_STUDIO_RECYCLE_MB", 1500.0)
        self.run_gb = _float_env("BIG360_STUDIO_RUN_GB", 1.0)
        self.ram_wait_s = _float_env("BIG360_STUDIO_RAM_WAIT_S", 15.0)
        self.last_runs: list[dict[str, Any]] = []
        self._last_hb = 0.0
        self._last_prio = 0.0

    # ---- recovery of a previous instance
    def recover(self) -> None:
        for p in sorted((self.dir / "claimed").glob("*.json")):
            kind, rid = _entry_run(p)
            st = self._status(kind, rid)
            if st and st.get("status") == "queued":
                os.replace(p, self.dir / "inbox" / p.name)
                continue
            if st and st.get("status") == "running":
                self._fail(kind, rid, "the studio lane restarted during the run", "stale")
            p.unlink(missing_ok=True)

    def _mod(self, kind: str) -> Any:
        if kind == "studio":
            from app.modeling import engine

            return engine
        from app.backtest import runs

        return runs

    def _status(self, kind: str, rid: str) -> dict[str, Any] | None:
        try:
            return self._mod(kind).read_status(rid, self.s)
        except Exception:  # noqa: BLE001
            return None

    def _fail(self, kind: str, rid: str, msg: str, code: str = "internal") -> None:
        mod = self._mod(kind)
        upd = getattr(mod, "_update_status", None) or getattr(mod, "_update")
        try:
            st = mod.read_status(rid, self.s) or {}
            if st.get("status") not in mod.TERMINAL:
                cancelled = (mod.run_dir(rid, self.s) / "CANCEL").exists()
                upd(rid, self.s, status="cancelled" if cancelled else "failed", error=msg,
                    error_code="cancelled" if cancelled else code, finished_at=_now())
        except Exception:  # noqa: BLE001
            log.exception("lane: could not mark %s %s failed", kind, rid)

    # ---- main loop
    def run_forever(self) -> None:
        self.recover()
        for sl in self.slots:
            sl.start()
        log.info("studio lane: pool=%d cores=%d pid=%d", self.pool, lane_cores(), os.getpid())
        while not self.stop_event.is_set():
            if stop_file(self.s).exists():
                log.info("studio lane: STOP file")
                break
            self.tick()
            self.stop_event.wait(TICK_S)
        self.shutdown()

    def tick(self) -> None:
        now = time.monotonic()
        for sl in self.slots:
            self._poll_slot(sl)
        self._dispatch()
        if now - self._last_prio >= PRIORITY_EVERY_S:
            self._last_prio = now
            self._adjust_priority()
        if now - self._last_hb >= HEARTBEAT_S:
            self._last_hb = now
            self.heartbeat()

    def _poll_slot(self, sl: _Slot) -> None:
        if sl.proc is None:
            sl.start()
            return
        try:
            while sl.conn.poll(0):
                msg = sl.conn.recv()
                if msg[0] == "ready":
                    sl.ready, sl.pid = True, int(msg[1])
                    self._set_priority_pid(sl.pid, self.priority)
                elif msg[0] == "done":
                    self._finish(sl, msg[2], msg[3] if len(msg) > 3 else None)
        except (EOFError, OSError):
            pass
        if not sl.alive():
            if sl.run:
                code = sl.proc.exitcode if sl.proc is not None else None
                self._fail(sl.run["kind"], sl.run["run_id"],
                           f"studio lane worker exited (code {code}; RAM cap {os.environ.get('BIG360_STUDIO_RAM_MB', '6144')} MB?)",
                           "internal")
                self._release(sl)
                self.failed += 1
            log.warning("studio lane: worker %d died, restarting", sl.idx)
            sl.stop(kill=True)
            sl.start()
            return
        if sl.run:
            self._cancel_watch(sl)
        elif sl.recycle and sl.ready:
            log.info("studio lane: recycling worker %d (memory)", sl.idx)
            sl.stop()
            sl.start()

    def _cancel_watch(self, sl: _Slot) -> None:
        r = sl.run
        assert r is not None
        try:
            flag = self._mod(r["kind"]).run_dir(r["run_id"], self.s) / "CANCEL"
        except ValueError:
            return
        if flag.exists():
            r["cancel_seen"] = r.get("cancel_seen") or time.monotonic()
            if time.monotonic() - r["cancel_seen"] > CANCEL_KILL_S:
                log.warning("studio lane: %s did not stop after cancel: killing worker %d", r["run_id"], sl.idx)
                self._fail(r["kind"], r["run_id"], "cancelled by user (stopped)", "cancelled")
                self._release(sl)
                sl.stop(kill=True)
                sl.start()

    def _finish(self, sl: _Slot, st: str, mem: float | None) -> None:
        r = sl.run or {}
        self._release(sl)
        self.done += 1
        self.failed += str(st).startswith("failed")
        el = time.monotonic() - float(r.get("started") or time.monotonic())
        self.last_runs = ([{"run_id": r.get("run_id"), "kind": r.get("kind"), "status": st, "run_s": round(el, 2),
                            "estimate_s": r.get("estimate_s"), "finished_at": _now()}] + self.last_runs)[:10]
        if mem is not None and mem > self.recycle_mb:
            sl.recycle = True

    def _release(self, sl: _Slot) -> None:
        r = sl.run
        if r and r.get("reservation") is not None:
            try:
                r["reservation"].release()
            except Exception:  # noqa: BLE001
                pass
        if r and r.get("claim") is not None:
            Path(r["claim"]).unlink(missing_ok=True)
        sl.run = None

    def _admit(self) -> bool:
        try:
            from app.ops import ram_budget as RB
        except Exception:  # noqa: BLE001
            return True
        if not RB.enabled():
            self.blocked_by = None
            return True
        ok, det = RB.check(self.run_gb, self.s)
        if ok:
            self.blocked_by, self.blocked_since = None, None
            return True
        self.blocked_since = self.blocked_since or time.monotonic()
        waited = time.monotonic() - self.blocked_since
        self.blocked_by = (f"RAM admission: {det.get('free_gb')} GB free, {det.get('outstanding_gb')} GB reserved, "
                           f"{det.get('headroom_gb')} GB headroom (starts anyway in {max(self.ram_wait_s - waited, 0):.0f} s)")
        if waited >= self.ram_wait_s:
            log.warning("studio lane: RAM admission waited %.0f s: starting anyway (%s)", waited, det)
            self.blocked_by, self.blocked_since = None, None
            return True
        return False

    def _dispatch(self) -> None:
        free = [sl for sl in self.slots if sl.ready and sl.run is None and not sl.recycle and sl.alive()]
        if not free:
            return
        for p in _inbox(self.s):
            if not free:
                break
            kind, rid = _entry_run(p)
            if kind not in KINDS:
                p.unlink(missing_ok=True)
                continue
            st = self._status(kind, rid)
            if not st or st.get("status") != "queued":
                p.unlink(missing_ok=True)  # cancelled while queued / moved to the worker
                continue
            if not self._admit():
                return
            claim = self.dir / "claimed" / p.name
            try:
                os.replace(p, claim)
            except OSError:
                continue  # withdrawn by the API (worker fallback) in the meantime
            rec = _read(claim) or {}
            sl = free.pop(0)
            res = None
            try:
                from app.ops import ram_budget as RB

                res = RB.reserve(f"studio_lane:{rid}", self.run_gb, kind="process", ttl_s=2 * 3600, settings=self.s)
                res.set_child(sl.pid)
            except Exception:  # noqa: BLE001
                res = None
            sl.run = {"kind": kind, "run_id": rid, "started": time.monotonic(), "estimate_s": rec.get("estimate_s"),
                      "reservation": res, "claim": str(claim), "started_at": _now()}
            try:
                sl.conn.send((kind, rid))
            except (OSError, EOFError, BrokenPipeError):
                self._fail(kind, rid, "studio lane worker unavailable", "internal")
                self._release(sl)

    def _heavy_running(self) -> list[str]:
        try:
            from app.ops.heavy_slot import holders

            out = [str(h.get("job_id")) for k in ("exclusive", "heavy") for h in holders(k, self.s)]
        except Exception:  # noqa: BLE001
            return []
        return [j for j in out if j not in ("studio_run", "backtest_run")]

    def _set_priority_pid(self, pid: int | None, cls: str) -> None:
        if not pid:
            return
        try:
            from app.ops.winsys import set_priority

            set_priority(int(pid), cls)
        except Exception:  # noqa: BLE001
            pass

    def _adjust_priority(self) -> None:
        heavy = self._heavy_running()
        want = "below_normal" if heavy else "normal"
        self.heavy = heavy
        if want == self.priority:
            return
        log.info("studio lane: priority %s -> %s (heavy: %s)", self.priority, want, ", ".join(heavy) or "none")
        self.priority = want
        self._set_priority_pid(os.getpid(), want)
        for sl in self.slots:
            self._set_priority_pid(sl.pid, want)

    def heartbeat(self, stopping: bool = False) -> None:
        queue = []
        for p in _inbox(self.s):
            kind, rid = _entry_run(p)
            rec = _read(p) or {}
            queue.append({"kind": kind, "run_id": rid, "estimate_s": rec.get("estimate_s"), "queued_at": rec.get("queued_at")})
        running = []
        for sl in self.slots:
            if sl.run:
                el = time.monotonic() - sl.run["started"]
                est = float(sl.run.get("estimate_s") or 0)
                running.append({"kind": sl.run["kind"], "run_id": sl.run["run_id"], "slot": sl.idx,
                                "started_at": sl.run.get("started_at"), "elapsed_s": round(el, 1),
                                "remaining_s": round(max(est - el, 0.5), 1)})
        mem = None
        try:
            from app.ops.winsys import tree_memory_mb

            mem = round(tree_memory_mb(os.getpid()), 0)
        except Exception:  # noqa: BLE001
            pass
        _write(self.dir / "lane.json", {
            "pid": os.getpid(), "started_at": self.started_at, "heartbeat_at": _now(), "stopping": stopping,
            "pool": self.pool, "cores": lane_cores(), "priority": self.priority, "heavy_jobs": getattr(self, "heavy", []),
            "workers": [{"slot": sl.idx, "pid": sl.pid, "ready": sl.ready, "busy": bool(sl.run)} for sl in self.slots],
            "running": running, "queue": queue, "blocked_by": self.blocked_by, "done": self.done, "failed": self.failed,
            "memory_mb": mem, "ram_cap_mb": _float_env("BIG360_STUDIO_RAM_MB", 6144.0), "last_runs": self.last_runs})

    def shutdown(self, wait_s: float = 25.0) -> None:
        self.heartbeat(stopping=True)
        t0 = time.monotonic()
        while any(sl.run for sl in self.slots) and time.monotonic() - t0 < wait_s:
            for sl in self.slots:
                if sl.run:
                    self._poll_slot(sl)
            time.sleep(0.1)
        for sl in self.slots:
            if sl.run:  # still running after the grace period: failed (the user can re-run it)
                self._fail(sl.run["kind"], sl.run["run_id"], "the studio lane stopped during the run", "stale")
                self._release(sl)
            sl.stop()
        self.heartbeat(stopping=True)
        stop_file(self.s).unlink(missing_ok=True)
        log.info("studio lane stopped (%d runs, %d failed)", self.done, self.failed)


def main(argv: list[str] | None = None) -> int:
    from filelock import FileLock, Timeout

    from app.core.logsetup import setup_logging

    s = get_settings()
    s.ensure_dirs()
    setup_logging(log_file="studio_lane.log", service="studio_lane")
    if not enabled():
        print("studio lane disabled (BIG360_STUDIO_LANE=0)", file=sys.stderr)
        return 0
    lock = FileLock(str(s.state_dir / "studio_lane.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("studio lane already running", file=sys.stderr)
        return 2
    stop_file(s).unlink(missing_ok=True)
    pool = pool_size()
    os.environ.update(lane_env(pool))
    pinned = _set_affinity(lane_cores())
    log.info("studio lane starting pid=%d pool=%d cores=%d pinned=%s", os.getpid(), pool, lane_cores(), pinned)
    lane = Lane(s, pool)

    def handler(signum: int, _frame: Any) -> None:
        log.info("studio lane: signal %s", signum)
        lane.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    try:
        lane.run_forever()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


def live_fields(st: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Extra public fields of a run status (StudioRunStatus / BacktestRunStatus, S1.1-S1.3): lane | sync | worker,
    stage, estimate breakdown, preview block, queue (position, blocked_by, queue_s) and a live ETA. All null for a
    run without them (older runs, app_state rows)."""
    out: dict[str, Any] = {"lane": st.get("lane"), "stage": st.get("stage"), "estimate": st.get("estimate"),
                           "preview": st.get("preview"), "queue": None, "eta_s": None,
                           "prior_estimate_s": st.get("prior_estimate_s")}
    if out["estimate"] is not None and not isinstance(out["estimate"], dict):
        try:
            out["estimate"] = json.loads(out["estimate"])
        except (TypeError, ValueError):
            out["estimate"] = None
    if isinstance(out["preview"], str):
        try:
            out["preview"] = json.loads(out["preview"])
        except ValueError:
            out["preview"] = None
    if st.get("lane") is not None and st.get("status") in ("queued", "running"):
        try:
            out.update(live_info(st, settings))
        except Exception:  # noqa: BLE001 - live info never breaks a status read
            pass
    return out
