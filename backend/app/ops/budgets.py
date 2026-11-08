"""Per-source rate budgets for the gap filler (O1.3).

The live jobs and the filler share the token buckets of app.core.ratelimit (requests per second). On top of that
the filler limits how much of each source it consumes per sliding hour (contracts/coverage.yaml sources.*.budget),
so background filling never starves the nightly / weekly refresh of a quota (Polygon 5/min, OECD 60/h, ...).

    b = RateBudgets.load(settings)          # state data/state/ops/budgets.json
    if b.can_spend("yfinance", 2600): ...   # before starting a task
    b.spend("yfinance", 2600); b.save()     # when it starts (estimate), corrected with actuals later
    b.snapshot()                            # {source: {unit, per_hour, used_1h, pct, events}}
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.ops.paths import ops_state_dir, read_json, write_json_atomic

WINDOW_S = 3600.0


class RateBudgets:
    def __init__(self, sources: dict[str, Any], path: Path | None = None, window_s: float = WINDOW_S,
                 clock: Any = time.time):
        self.sources = sources
        self.path = path
        self.window_s = window_s
        self.clock = clock
        self.events: dict[str, list[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, settings: Settings | None = None, sources: dict[str, Any] | None = None) -> RateBudgets:
        from app.ops.coverage import load_coverage

        srcs = sources if sources is not None else load_coverage(settings)["sources"]
        p = ops_state_dir(settings) / "budgets.json"
        b = cls(srcs, p)
        doc = read_json(p, {}) or {}
        for s, ev in (doc.get("events") or {}).items():
            b.events[s] = [(float(t), float(u)) for t, u in ev]
        b._prune()
        return b

    def limit(self, source: str) -> float | None:
        bud = (self.sources.get(source) or {}).get("budget") or {}
        v = bud.get("per_hour")
        return float(v) if v is not None else None

    def unit(self, source: str) -> str:
        return str(((self.sources.get(source) or {}).get("budget") or {}).get("unit", "units"))

    def _prune(self) -> None:
        cut = self.clock() - self.window_s
        for s in list(self.events):
            self.events[s] = [(t, u) for t, u in self.events[s] if t >= cut]

    def used(self, source: str) -> float:
        with self._lock:
            self._prune()
            return sum(u for _, u in self.events.get(source, []))

    def can_spend(self, source: str | None, units: float | None) -> bool:
        """True when spending `units` keeps the sliding hour within budget. An item larger than the whole budget is
        allowed only when nothing was spent in the window (it then runs alone, never twice an hour)."""
        if not source:
            return True
        lim = self.limit(source)
        if lim is None:
            return True
        u = float(units or 0)
        used = self.used(source)
        if u > lim:
            return used == 0
        return used + u <= lim

    def wait_s(self, source: str | None, units: float | None) -> float:
        """Seconds until can_spend would be True (0 if now)."""
        if self.can_spend(source, units) or not source:
            return 0.0
        lim = self.limit(source) or 0.0
        u = min(float(units or 0), lim)
        with self._lock:
            ev = sorted(self.events.get(source, []))
        now = self.clock()
        total = sum(x for _, x in ev)
        for t, x in ev:
            total -= x
            if total + u <= lim or (u >= lim and total == 0):
                return max(0.0, t + self.window_s - now)
        return self.window_s

    def spend(self, source: str | None, units: float | None) -> None:
        if not source or not units:
            return
        with self._lock:
            self.events.setdefault(source, []).append((self.clock(), float(units)))

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self._prune()
            data = {"events": {s: [[round(t, 1), u] for t, u in ev] for s, ev in self.events.items() if ev}}
        write_json_atomic(self.path, data)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out = {}
        for s, spec in self.sources.items():
            lim = self.limit(s)
            used = self.used(s)
            out[s] = {"unit": self.unit(s), "per_hour": lim, "used_1h": round(used, 2),
                      "pct": round(100 * used / lim, 1) if lim else None, "note": spec.get("note"),
                      "limiter": spec.get("limiter")}
        return out
