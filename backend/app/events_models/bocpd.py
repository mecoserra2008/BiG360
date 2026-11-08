"""C2: Bayesian online change-point detection (Adams & MacKay 2007) and job `event_changepoints`.

`BOCPD` is an online detector (one observation at a time, bounded memory, JSON-serializable state) so the realtime
layer can keep one per stream:

    det = BOCPD.from_warmup(history_values, hazard=1/30)       # or BOCPD(mu0=..., ...)
    out = det.update(x)       # {"t", "map_run", "cp_prob", "run_mean", "changed", "pred_mean", "pred_sd"}
    state = det.to_dict(); det = BOCPD.from_dict(state)

Model: Gaussian observations with unknown mean and variance (Normal-Gamma conjugate prior), constant hazard
H = 1 / expected run length, run length truncated at max_run (tail mass folded into the last cell).
"""

from __future__ import annotations

import math
import time
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
from scipy.special import gammaln, logsumexp

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_models import common as C

log = get_logger(__name__)


class BOCPD:
    def __init__(self, hazard: float = 1 / 30, mu0: float = 0.0, kappa0: float = 1.0, alpha0: float = 1.0,
                 beta0: float = 1.0, max_run: int = 400, cp_window: int = 3):
        self.hazard = float(hazard)
        self.prior = (float(mu0), float(kappa0), float(alpha0), float(beta0))
        self.max_run = int(max_run)
        self.cp_window = int(cp_window)
        self.t = 0
        self.log_r = np.array([0.0])                       # log P(r_t | x_1..t), index = run length
        self.mu = np.array([mu0], float)
        self.kappa = np.array([kappa0], float)
        self.alpha = np.array([alpha0], float)
        self.beta = np.array([beta0], float)
        self.sum_x = np.array([0.0])                      # data sums per run length (segment means)
        self.last_map = 0

    # -------------------------------------------------------------------------------------------- construction
    @classmethod
    def from_warmup(cls, xs: Any, hazard: float = 1 / 30, max_run: int = 400, strength: float = 1.0) -> "BOCPD":
        """Weakly-informative prior centred on warm-up data (mean, variance), worth `strength` pseudo-observations."""
        a = np.asarray([x for x in xs if x is not None and np.isfinite(x)], float)
        m = float(a.mean()) if len(a) else 0.0
        v = float(a.var()) if len(a) > 1 else 1.0
        v = v if v > 1e-8 else 1.0
        # E[precision] = alpha / beta = 1 / v with alpha0 = 1
        return cls(hazard=hazard, mu0=m, kappa0=strength, alpha0=1.0, beta0=v, max_run=max_run)

    # -------------------------------------------------------------------------------------------- core
    def _pred_logpdf(self, x: float) -> np.ndarray:
        df = 2 * self.alpha
        scale2 = self.beta * (self.kappa + 1) / (self.alpha * self.kappa)
        z2 = (x - self.mu) ** 2 / scale2
        return (gammaln((df + 1) / 2) - gammaln(df / 2) - 0.5 * np.log(df * math.pi * scale2)
                - (df + 1) / 2 * np.log1p(z2 / df))

    def update(self, x: float | None) -> dict[str, Any]:
        """Consume one observation (None / NaN = missing: state unchanged). Returns the detector summary."""
        if x is None or not np.isfinite(x):
            return self.summary(changed=False)
        x = float(x)
        lp = self._pred_logpdf(x)
        lh, l1h = math.log(self.hazard), math.log1p(-self.hazard)
        growth = self.log_r + lp + l1h
        cp = logsumexp(self.log_r + lp + lh)
        new = np.concatenate([[cp], growth])
        new -= logsumexp(new)
        mu0, k0, a0, b0 = self.prior
        mu_n = (self.kappa * self.mu + x) / (self.kappa + 1)
        b_n = self.beta + self.kappa * (x - self.mu) ** 2 / (2 * (self.kappa + 1))
        self.mu = np.concatenate([[mu0], mu_n])
        self.kappa = np.concatenate([[k0], self.kappa + 1])
        self.alpha = np.concatenate([[a0], self.alpha + 0.5])
        self.beta = np.concatenate([[b0], b_n])
        self.sum_x = np.concatenate([[0.0], self.sum_x + x])
        self.log_r = new
        if len(self.log_r) > self.max_run + 1:          # fold the tail into the last cell
            tail = logsumexp(self.log_r[self.max_run:])
            self.log_r = np.concatenate([self.log_r[: self.max_run], [tail]])
            for k in ("mu", "kappa", "alpha", "beta", "sum_x"):
                setattr(self, k, getattr(self, k)[: self.max_run + 1])
        self.t += 1
        prev = self.last_map
        out = self.summary()
        out["changed"] = bool(out["map_run"] + 1 < prev and out["map_run"] <= self.cp_window)
        self.last_map = out["map_run"]
        return out

    def summary(self, changed: bool = False) -> dict[str, Any]:
        r = int(np.argmax(self.log_r))
        p = np.exp(self.log_r)
        cpp = float(p[: self.cp_window + 1].sum())
        # run length r = number of most recent observations (incl. the current one) in the current regime;
        # r = 0: a change right after the current observation (Adams & MacKay convention)
        mean = float(self.sum_x[r] / r) if r > 0 and r < len(self.sum_x) else None
        df = 2 * self.alpha[r]
        sd = float(math.sqrt(self.beta[r] * (self.kappa[r] + 1) / (self.alpha[r] * self.kappa[r]) * df / max(df - 2, 1e-9)))
        return {"t": self.t, "map_run": r, "cp_prob": cpp, "run_mean": mean, "changed": changed,
                "pred_mean": float(self.mu[r]), "pred_sd": sd}

    # -------------------------------------------------------------------------------------------- state
    def to_dict(self) -> dict[str, Any]:
        return {"v": 1, "hazard": self.hazard, "prior": list(self.prior), "max_run": self.max_run,
                "cp_window": self.cp_window, "t": self.t, "last_map": self.last_map,
                "log_r": self.log_r.tolist(), "mu": self.mu.tolist(), "kappa": self.kappa.tolist(),
                "alpha": self.alpha.tolist(), "beta": self.beta.tolist(), "sum_x": self.sum_x.tolist()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BOCPD":
        o = cls(d["hazard"], *d["prior"], max_run=d["max_run"], cp_window=d.get("cp_window", 3))
        o.t, o.last_map = int(d["t"]), int(d.get("last_map", 0))
        for k in ("log_r", "mu", "kappa", "alpha", "beta", "sum_x"):
            setattr(o, k, np.asarray(d[k], float))
        return o


def detect(values: np.ndarray, hazard: float = 1 / 100, warmup: int = 14, max_run: int = 400,
           min_persist: int = 3, min_shift: float = 1.0) -> list[dict[str, Any]]:
    """Batch helper over one stream (via the online class): change points as
    [{index (first obs of the new regime), cp_prob, run_length_before, mean_before, mean_after, sd_before, n_after}].
    A change is kept when the MAP run length collapses and the new regime persists >= min_persist obs (or reaches
    the end of the series)."""
    v = np.asarray(values, float)
    ok = np.isfinite(v)
    if ok.sum() < warmup + 3:
        return []
    det = BOCPD.from_warmup(v[ok][:warmup], hazard=hazard, max_run=max_run)
    maps = np.full(len(v), -1)
    cpp = np.zeros(len(v))
    for i, x in enumerate(v):
        o = det.update(x)
        maps[i] = o["map_run"] if np.isfinite(x) else (maps[i - 1] + 1 if i else 0)
        cpp[i] = o["cp_prob"]
    # observation index -> position counting only finite obs, to translate run lengths back
    fin_idx = np.nonzero(ok)[0]
    cps: list[int] = []
    for k in range(1, len(fin_idx)):
        i, j = fin_idx[k], fin_idx[k - 1]
        if maps[i] + 3 < maps[j]:   # the MAP run length collapsed (the new regime overtook the old one)
            start_pos = k - maps[i] + 1
            if start_pos <= warmup // 2 or start_pos >= len(fin_idx):
                continue
            start = int(fin_idx[max(start_pos, 0)])
            # persistence: MAP keeps growing from this start for min_persist obs (or until the end)
            end_k = min(k + min_persist, len(fin_idx) - 1)
            if all(maps[fin_idx[m]] >= m - start_pos for m in range(max(k, start_pos), end_k + 1)):
                if not cps or start != cps[-1]:
                    cps.append(start)
    out = []
    bounds = cps + [len(v)]
    prev = 0
    for n, c in enumerate(cps):
        before = v[prev:c][np.isfinite(v[prev:c])]
        after = v[c:bounds[n + 1]][np.isfinite(v[c:bounds[n + 1]])]
        if len(before) < 3 or len(after) < 1:
            continue
        sd_b = float(before.std(ddof=1))
        sd_a = float(after.std(ddof=1)) if len(after) > 2 else None
        shift = abs(after.mean() - before.mean()) / sd_b if sd_b > 1e-12 else np.inf
        vratio = (sd_a / sd_b) if sd_a is not None and sd_b > 1e-12 else 1.0
        if shift < min_shift and 0.5 <= vratio <= 2.0:   # neither the level nor the dispersion moved: spurious
            continue
        out.append({"index": c, "cp_prob": float(cpp[max(c - 1, 0): c + 4].max()),
                    "run_length_before": int(len(before)), "mean_before": float(before.mean()),
                    "mean_after": float(after.mean()), "sd_before": sd_b, "n_after": int(len(after))})
        prev = c
    return out


# ------------------------------------------------------------------------------------------------ job


def _streams(settings: Settings, start: date, min_items: float, top_issuers: int) -> list[tuple[str, str, str, pd.DataFrame]]:
    out = []
    for lt in ("country", "sector", "issuer"):
        t1 = C.load_t1(lt, start=start, settings=settings)
        if t1.is_empty():
            continue
        if lt != "country":
            t1 = t1.filter(pl.col("theme") == -1)
        t1 = t1.with_columns((pl.col("ev_n") + pl.col("art_n")).alias("items"),
                             pl.coalesce("art_tone", "ev_tone").alias("tone"))
        vol = t1.group_by("label", "theme").agg(pl.col("items").mean().alias("m"), pl.len().alias("n"))
        vol = vol.filter(pl.col("m") >= (min_items if lt != "issuer" else 1))
        if lt == "issuer":
            vol = vol.sort("m", descending=True).head(top_issuers)
        t1 = t1.join(vol.select("label", "theme"), on=["label", "theme"], how="semi")
        for (lab, th), g in t1.group_by(["label", "theme"]):
            out.append((lt, lab, "all" if th == -1 else C.THEME_ID.get(int(th), str(th)),
                        g.select("date", "items", "tone").to_pandas()))
    return out


def run_changepoints(settings: Settings | None = None, window_days: int = 365, hazard: float = 1 / 100,
                     min_items: float = 20.0, top_issuers: int = 300) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    rng = C.t1_range(s)
    if rng is None:
        return {"rows": 0}
    start = max(rng[0], rng[1] - timedelta(days=window_days))
    days = pd.date_range(start, rng[1], freq="D")
    now = C.now_utc()
    rows = []
    streams = _streams(s, start, min_items, top_issuers)
    for lt, lab, th, g in streams:
        # refresh-pipeline fixes 2026-10-26: one row per day (a duplicated T1 day or two label ids decoding to the
        # same label made reindex fail with "cannot reindex on an axis with duplicate labels")
        g = (g.assign(date=pd.to_datetime(g["date"])).groupby("date", sort=True)
             .agg(items=("items", "sum"), tone=("tone", "mean")).reindex(days))
        att = np.log1p(g["items"].fillna(0.0).to_numpy())
        # remove the weekly news cycle (label's day-of-week mean deviation over the window)
        dw = pd.Series(att).groupby(days.dayofweek).transform("mean").to_numpy()
        att = att - dw + att.mean()
        tone = g["tone"].to_numpy(float)
        for stream, v in (("attention", att), ("tone", tone)):
            for cp in detect(v, hazard=hazard):
                sd = cp["sd_before"]
                sh = (cp["mean_after"] - cp["mean_before"]) / sd if sd and sd > 1e-9 else None
                rows.append({"label_type": lt, "label": lab, "theme": th, "stream": stream,
                             "date": days[cp["index"]].date(), "cp_prob": cp["cp_prob"],
                             "run_length_before": cp["run_length_before"], "mean_before": cp["mean_before"],
                             "mean_after": cp["mean_after"], "shift_sd": sh,
                             "direction": "flat" if sh is None or abs(sh) < 0.5 else ("up" if sh > 0 else "down"),
                             "n_after": cp["n_after"], "computed_at": now})
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    # replace the window's change points (re-detected with more history each run)
    n = 0
    if df.height:
        try:
            from app.core.storage import read_table  # noqa: PLC0415

            old = read_table("event_changepoints", settings=s)
            old = old.filter(pl.col("date") < start) if old.height else old
        except Exception:  # noqa: BLE001
            old = pl.DataFrame()
        allr = pl.concat([old.select(df.columns).cast(df.schema), df]) if old.height else df
        n = C.write(allr, "event_changepoints", mode="overwrite", settings=s)
    rep = {"rows": n, "new_in_window": df.height, "streams": len(streams), "window": [str(start), str(rng[1])],
           "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_changepoints", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_changepoints(ctx.settings, window_days=int(p.get("window_days", 365)),
                           hazard=float(p.get("hazard", 1 / 100)), min_items=float(p.get("min_items", 20)),
                           top_issuers=int(p.get("top_issuers", 300)))
    return JobResult(status="success", rows_written={"event_changepoints": rep.get("rows", 0)}, message=str(rep))
