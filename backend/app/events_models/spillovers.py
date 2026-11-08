"""B2 job `event_spillovers`: spatial spillover models (OLS-FE, SLX, SAR, SEM, SDM) of event shocks in linked
countries on equity index returns, volatility and macro-surprise changes; direct / indirect / total effects and
per-country transmit / receive spillovers.

params: w_kinds (comma list, default "contiguity,inverse_distance"), freq (auto | D | W), draws (500), fe (twoway).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_models import common as C
from app.events_models.panel import SHOCKS, balanced, build_panel
from app.events_models.weights import get_weights
from app.modeling import _spatial as SP

log = get_logger(__name__)

MODELS = ("ols", "slx", "sar", "sem", "sdm")


def _fit(model: str, y: np.ndarray, x: np.ndarray, W: np.ndarray, N: int, fe: str) -> dict[str, Any]:
    X = x[:, None]
    if model == "ols":
        return SP.slx_panel(y, X, W, N, fe=fe, lagged=False)
    if model == "slx":
        return SP.slx_panel(y, X, W, N, fe=fe)
    if model == "sar":
        return SP.sar_panel(y, X, W, N, fe=fe)
    if model == "sem":
        return SP.sem_panel(y, X, W, N, fe=fe)
    return SP.sar_panel(y, X, W, N, fe=fe, durbin=True)


def _row(base: dict[str, Any], effect: str, est: Any, se: Any = None, lo: Any = None, hi: Any = None, p: Any = None,
         iso2: str = "*") -> dict[str, Any]:
    f = lambda v: None if v is None or not np.isfinite(v) else float(v)  # noqa: E731
    if lo is None and se is not None and est is not None and np.isfinite(se):
        lo, hi = est - 1.959964 * se, est + 1.959964 * se
    if p is None and se is not None and se and np.isfinite(se) and est is not None:
        from scipy import stats  # noqa: PLC0415

        p = 2 * stats.norm.sf(abs(est / se))
    return base | {"effect": effect, "iso2": iso2, "estimate": f(est), "std_error": f(se), "ci_low": f(lo),
                   "ci_high": f(hi), "p_value": f(p)}


def run_spillovers(settings: Settings | None = None, w_kinds: tuple[str, ...] = ("contiguity", "inverse_distance"),
                   freq: str = "auto", draws: int = 500, fe: str = "twoway") -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    P = build_panel(s, freq=freq)
    if not P:
        return {"rows": 0, "note": "no panel"}
    now = C.now_utc()
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for wk in w_kinds:
        for resp, Yf in P["Y"].items():
            if Yf is None or len(Yf) == 0:
                continue
            for shock in SHOCKS:
                Xf = P["S"][shock]
                codes, dates, y, x = balanced(Yf, Xf)
                if len(codes) < 8 or len(dates) < 8:
                    continue
                have, Wraw, meta = get_weights(wk, codes)
                if len(have) != len(codes):  # drop units the weights cannot place
                    keep = [codes.index(c) for c in have]
                    T = len(dates)
                    y = y.reshape(T, -1)[:, keep].reshape(-1)
                    x = x.reshape(T, -1)[:, keep].reshape(-1)
                    codes = have
                N, T = len(codes), len(dates)
                sx = np.nanstd(x)
                x = (x - np.nanmean(x)) / (sx if sx > 0 else 1.0)   # per 1-sd shock
                base = {"as_of": P["window"]["end"], "window_start": dates[0].date(), "window_end": dates[-1].date(),
                        "freq": P["freq"], "response": resp, "shock": shock, "w_kind": wk, "w_source": meta["source"],
                        "n_units": N, "n_periods": T, "computed_at": now}
                fe_m = fe if resp != "macro_surprise" else "entity"
                try:
                    lm = SP.lm_tests(y, x[:, None], Wraw, N, fe=fe_m)
                except Exception:  # noqa: BLE001
                    lm = {}
                for m in MODELS:
                    try:
                        f = _fit(m, y, x, Wraw, N, fe_m)
                    except Exception as e:  # noqa: BLE001
                        log.info("spillovers: %s %s %s %s failed: %s", wk, resp, shock, m, e)
                        continue
                    b = base | {"model": m, "llf": float(f["llf"]), "aic": float(f["aic"]), "pseudo_r2": float(f["pseudo_r2"])}
                    k = f["k"]
                    c0 = 1 if f.get("has_const") else 0
                    rows.append(_row(b, "beta", f["beta"][c0], f["se"][c0]))
                    if f.get("durbin") and m != "ols":
                        rows.append(_row(b, "theta", f["beta"][c0 + 1], f["se"][c0 + 1]))
                    if m in ("sar", "sdm"):
                        rows.append(_row(b, "rho", f["rho"], f["se"][k]))
                    if m == "sem":
                        rows.append(_row(b, "lambda", f["lambda"], f["se"][k]))
                    if m != "ols":
                        eff = SP.effects(f, 1, draws=draws, seed=7)[0]
                        for nm in ("direct", "indirect", "total"):
                            rows.append(_row(b, nm, eff[nm], eff[f"{nm}_se"], eff[f"{nm}_lo"], eff[f"{nm}_hi"], eff[f"{nm}_p"]))
                    if m == "ols":
                        for key, nm in (("lm_lag", "lm_lag"), ("lm_error", "lm_error"), ("rlm_lag", "rlm_lag"),
                                        ("rlm_error", "rlm_error")):
                            if key in lm:
                                rows.append(_row(b, nm, lm[key][0], None, None, None, lm[key][1]))
                    if m == "sdm":
                        S = SP.impact_matrix(f, 0, 1)
                        off = S - np.diag(np.diag(S))
                        for i, c in enumerate(codes):
                            rows.append(_row(b, "transmit", off[:, i].sum(), iso2=c))
                            rows.append(_row(b, "receive", off[i, :].sum(), iso2=c))
                        summary[f"{wk}/{resp}/{shock}"] = {"N": N, "T": T, "rho": round(f["rho"], 3),
                                                            "beta": round(float(f["beta"][c0]), 4),
                                                            "theta": round(float(f["beta"][c0 + 1]), 4)}
    n = C.write(pl.DataFrame(rows, infer_schema_length=None), "spatial_spillovers", settings=s) if rows else 0
    rep = {"rows": n, "freq": P["freq"], "window": {k: str(v) for k, v in P["window"].items()},
           "countries": len(P["countries"]), "sdm": summary, "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_spillovers", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    kinds = tuple(k.strip() for k in str(p.get("w_kinds", "contiguity,inverse_distance")).split(",") if k.strip())
    rep = run_spillovers(ctx.settings, w_kinds=kinds, freq=str(p.get("freq", "auto")), draws=int(p.get("draws", 500)),
                         fe=str(p.get("fe", "twoway")))
    return JobResult(status="success", rows_written={"spatial_spillovers": rep.get("rows", 0)}, message=str(rep)[:2000])
