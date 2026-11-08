"""Online change-point detection (R1.3) on live GDELT attention.

Detector: Bayesian Online Change-Point Detection (Adams & MacKay 2007) with a Normal-Gamma prior (Student-t
predictive), constant hazard 1/lambda, run length truncated at r_max. `make_detector()` prefers the event-models
agent's implementation (app.events_models.bocpd.BOCPD, adapted by EventModelsBOCPD) when it is importable; the
minimal MiniBOCPD below is the fallback.

ChangepointConsumer (group `changepoint`) consumes `events` slot summaries (one per GDELT 15-min slot): series =
log1p(article count) per market theme (+ `_total` and `_linked`). It emits a `signals` message (type `changepoint`)
when P(change within the last 2 slots) >= `threshold` (0.5), the previous regime lasted >= 12 slots and the new level
differs from the previous run mean by >= 1 predictive std; cooldown 4 h per series. Detector states are pickled to
data/realtime/changepoint_state.pkl after each slot (restart-safe; replayed slots are skipped by slot stamp).
"""

from __future__ import annotations

import math
import pickle
import time
from pathlib import Path
from typing import Any

import numpy as np

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer, ts_of
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)


class MiniBOCPD:
    """Adams-MacKay BOCPD, Gaussian with unknown mean and variance (Normal-Gamma conjugate)."""

    def __init__(self, hazard_lambda: float = 250.0, mu0: float = 0.0, kappa0: float = 0.1, alpha0: float = 1.0,
                 beta0: float = 1.0, r_max: int = 400):
        self.h = 1.0 / hazard_lambda
        self.prior = (mu0, kappa0, alpha0, beta0)
        self.r_max = r_max
        self.t = 0
        self.R = np.array([1.0])                       # run-length posterior
        self.mu = np.array([mu0])
        self.kappa = np.array([kappa0])
        self.alpha = np.array([alpha0])
        self.beta = np.array([beta0])
        self.sum_x = np.array([0.0])                   # per run length: sum of x (for the run mean)
        self.ewm: float | None = None                  # long-run level: prior mean of a new run

    def _pred(self, x: float) -> tuple[np.ndarray, np.ndarray]:
        from scipy.stats import t as student_t  # noqa: PLC0415

        scale = np.sqrt(self.beta * (self.kappa + 1) / (self.alpha * self.kappa))
        return student_t.pdf(x, 2 * self.alpha, loc=self.mu, scale=scale), scale

    def update(self, x: float) -> dict[str, Any]:
        if self.t == 0:   # first observation: centre the prior on it
            self.mu[:] = x
        self.ewm = x if self.ewm is None else 0.97 * self.ewm + 0.03 * x
        pred, scale = self._pred(x)
        exp_mean = float(np.sum(self.R * self.mu))
        exp_std = float(np.sum(self.R * scale))
        growth = self.R * pred * (1 - self.h)
        cp = float(np.sum(self.R * pred * self.h))
        newR = np.append(cp, growth)
        tot = newR.sum()
        newR = newR / tot if tot > 0 else np.append(1.0, np.zeros_like(growth))
        mu0, k0, a0, b0 = self.prior
        mu_new = (self.kappa * self.mu + x) / (self.kappa + 1)
        beta_new = self.beta + self.kappa * (x - self.mu) ** 2 / (2 * (self.kappa + 1))
        self.mu = np.append(self.ewm, mu_new)
        self.kappa = np.append(k0, self.kappa + 1)
        self.alpha = np.append(a0, self.alpha + 0.5)
        self.beta = np.append(b0, beta_new)
        self.sum_x = np.append(0.0, self.sum_x + x)
        self.R = newR
        if len(self.R) > self.r_max:   # truncate (renormalise)
            for name in ("R", "mu", "kappa", "alpha", "beta", "sum_x"):
                setattr(self, name, getattr(self, name)[: self.r_max])
            self.R = self.R / self.R.sum()
        self.t += 1
        map_r = int(np.argmax(self.R))
        return {"t": self.t, "x": x, "p_cp_recent": float(self.R[:3].sum()), "map_run_length": map_r,
                "expected_mean": exp_mean, "expected_std": exp_std,
                "z": (x - exp_mean) / exp_std if exp_std > 0 else 0.0}


class EventModelsBOCPD:
    """Adapter around the event-models agent's detector (app.events_models.bocpd.BOCPD, C2) with the MiniBOCPD
    output keys. The prior is centred on the first observation (BOCPD.from_warmup); z uses the predictive mean / sd
    before the update."""

    def __init__(self, hazard_lambda: float = 250.0, max_run: int = 400):
        self.hazard = 1.0 / hazard_lambda
        self.max_run = max_run
        self.det: Any = None

    def update(self, x: float) -> dict[str, Any]:
        from app.events_models.bocpd import BOCPD  # noqa: PLC0415

        if self.det is None:
            self.det = BOCPD.from_warmup([x], hazard=self.hazard, max_run=self.max_run)
        prev = self.det.summary()
        out = self.det.update(float(x))
        sd = prev.get("pred_sd") or 0.0
        return {"t": out["t"], "x": x, "p_cp_recent": float(out["cp_prob"]), "map_run_length": int(out["map_run"]),
                "expected_mean": float(prev["pred_mean"]), "expected_std": float(sd),
                "z": (x - prev["pred_mean"]) / sd if sd > 0 else 0.0, "changed": bool(out.get("changed"))}


def make_detector(**kw: Any) -> Any:
    """The event-models agent's BOCPD (app.events_models.bocpd) when importable, else the minimal MiniBOCPD."""
    try:
        import app.events_models.bocpd  # noqa: F401,PLC0415

        return EventModelsBOCPD(**kw)
    except Exception:  # noqa: BLE001
        return MiniBOCPD(**kw)


class ChangepointConsumer(Consumer):
    name = "changepoint"
    group = "changepoint"
    topics = ("events",)
    batch = 500

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.threshold = float(params.get("threshold", 0.5))
        self.min_run = int(params.get("min_run", 12))
        self.cooldown_s = float(params.get("cooldown_s", 4 * 3600))
        self.min_z = float(params.get("min_z", 1.0))
        self.state: dict[str, Any] = {"det": {}, "last_slot": None, "last_fire": {}, "run": {}}
        self.path: Path | None = None

    async def setup(self, ctx: StreamContext) -> None:
        self.path = Path(ctx.settings.data_dir) / "realtime" / "changepoint_state.pkl"
        if self.path.exists():
            try:
                with open(self.path, "rb") as fh:
                    self.state = pickle.load(fh)  # noqa: S301 - own file
            except Exception as e:  # noqa: BLE001
                log.warning("changepoint state unreadable, starting fresh: %s", e)

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(self.state, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self.path)

    def series_of(self, p: dict[str, Any]) -> dict[str, float]:
        s = {f"theme:{k}": math.log1p(v) for k, v in (p.get("themes_articles") or {}).items()}
        s["_total"] = math.log1p(p.get("n_articles") or 0)
        s["_linked"] = math.log1p(p.get("n_linked_articles") or 0)
        return s

    def handle_one(self, ctx: StreamContext, msg: Message) -> list[Envelope]:
        if msg.type != "slot":
            return []
        p = msg.payload
        slot = p.get("slot")
        if self.state.get("last_slot") and slot and slot <= self.state["last_slot"]:
            return []   # replay / duplicate slot
        out: list[Envelope] = []
        now = time.time()
        series = self.series_of(p)
        for name in set(series) | {k for k in self.state["det"] if k.startswith("theme:")}:
            x = series.get(name, 0.0)
            det = self.state["det"].get(name)
            if det is None:
                det = self.state["det"][name] = make_detector()
            prev_run = self.state["run"].get(name, 0)
            r = det.update(float(x))
            self.state["run"][name] = r.get("map_run_length", 0)
            fire = (r["p_cp_recent"] >= self.threshold and prev_run >= self.min_run and abs(r.get("z", 0)) >= self.min_z
                    and now - self.state["last_fire"].get(name, 0) >= self.cooldown_s)
            if fire:
                self.state["last_fire"][name] = now
                out.append(Envelope("signals", f"cp:{name}:{slot}", {
                    "kind": "changepoint", "series": name, "slot": slot, "value": x, "count": round(math.expm1(x)),
                    "p_change": round(r["p_cp_recent"], 4), "prev_run_slots": prev_run,
                    "expected_mean": round(r["expected_mean"], 4), "expected_std": round(r["expected_std"], 4),
                    "z": round(r.get("z", 0.0), 3), "direction": "up" if r.get("z", 0) > 0 else "down",
                    "detector": type(det).__name__, "published_at": p.get("published_at"),
                    "label": "Model-based (online BOCPD on GDELT attention)",
                }, type="changepoint", ts_source=ts_of(msg), ckey=f"cp:{name}"))
        self.state["last_slot"] = slot
        self._save()
        return out
