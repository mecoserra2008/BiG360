"""B4 job `event_irf`: panel local projections of country index returns / volatility on own and spatially lagged
event shocks, plus a pooled spatial VAR(1) with simulation-band IRFs.

params: horizon (10), w_kind (contiguity), svar_units (comma ISO2 list for unit-specific IRFs; default the 8 largest
markets present), draws (300).
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_models import common as C
from app.events_models.panel import SHOCKS, build_panel
from app.events_models.weights import get_weights
from app.modeling import _spatial as SP

log = get_logger(__name__)

DEFAULT_UNITS = ("US", "CN", "JP", "DE", "GB", "IN", "FR", "KR")


def _long(Yf: pd.DataFrame, Sf: pd.DataFrame, codes: list[str], W: np.ndarray) -> pd.DataFrame:
    Y = Yf[codes]
    S = Sf.reindex(Yf.index)[codes]
    sd = np.nanstd(S.to_numpy())
    S = (S - np.nanmean(S.to_numpy())) / (sd if sd > 0 else 1.0)
    WS = pd.DataFrame(S.fillna(0.0).to_numpy() @ SP.row_standardize(W).T, index=S.index, columns=codes)
    WS[S.isna()] = np.nan
    d = pd.DataFrame({"date": np.repeat(Y.index.values, len(codes)), "entity_id": np.tile(codes, len(Y)),
                      "y": Y.to_numpy().reshape(-1), "s": S.to_numpy().reshape(-1), "ws": WS.to_numpy().reshape(-1)})
    return d


def run_irf(settings: Settings | None = None, horizon: int = 10, w_kind: str = "contiguity",
            units: tuple[str, ...] = DEFAULT_UNITS, draws: int = 300) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    P = build_panel(s, freq="D")
    if not P:
        return {"rows": 0, "note": "no panel"}
    now = C.now_utc()
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for resp in ("ret", "vol"):
        Yf = P["Y"][resp]
        for shock in SHOCKS:
            Sf = P["S"][shock]
            cover = Sf.reindex(Yf.index).notna().mean()
            codes = [c for c in Yf.columns if cover.get(c, 0) >= 0.4]
            if len(codes) < 8:
                continue
            have, W, meta = get_weights(w_kind, codes)
            codes = list(have)
            d = _long(Yf, Sf, codes, W)
            n_dates = d["date"].nunique()
            base = {"as_of": P["window"]["end"], "window_start": pd.Timestamp(d["date"].min()).date(),
                    "window_end": pd.Timestamp(d["date"].max()).date(), "response": resp, "shock": shock,
                    "w_kind": w_kind, "computed_at": now}
            H = int(min(horizon, max(1, n_dates // 3)))
            lp = SP.local_projection(d, "y", ["s", "ws"], H=H, y_lags=2, cumulative=True, fe="entity")
            for r in lp.itertuples(index=False):
                rows.append(base | {"model": "lp", "component": "own" if r.shock == "s" else "spatial", "scope": "*",
                                    "h": int(r.h), "coef": r.coef, "std_error": r.se, "ci_low": r.lo, "ci_high": r.hi,
                                    "p_value": r.p, "n_obs": int(r.n)})
            # spatial VAR on the balanced block
            Y = Yf[codes].copy()
            Sx = Sf.reindex(Yf.index)[codes]
            live = Sx.notna().mean(axis=1) >= 0.5
            Y, Sx = Y[live], Sx[live]
            sd = np.nanstd(Sx.to_numpy())
            Sx = ((Sx - np.nanmean(Sx.to_numpy())) / (sd if sd > 0 else 1.0)).fillna(0.0)
            Y = Y.fillna(0.0)
            if len(Y) < 10:
                continue
            try:
                fit = SP.spatial_var(Y.to_numpy(), Sx.to_numpy(), W, p=1, fe="entity")
            except Exception as e:  # noqa: BLE001
                log.info("irf: svar %s/%s failed: %s", resp, shock, e)
                continue
            n_obs = fit["n"]
            scopes = [("*", None)] + [(u, codes.index(u)) for u in units if u in codes]
            for scope, ui in scopes:
                comps = [("total", "mean")] if ui is None else [("own", "own"), ("others", "others")]
                for comp, agg in comps:
                    pt, se, lo, hi = SP.spatial_var_irf_bands(fit, H, ui, draws=draws, seed=11, agg=agg)
                    for h in range(H + 1):
                        z = pt[h] / se[h] if np.isfinite(se[h]) and se[h] > 0 else np.nan
                        from scipy import stats  # noqa: PLC0415

                        rows.append(base | {"model": "svar", "component": comp, "scope": scope, "h": h,
                                            "coef": float(pt[h]), "std_error": float(se[h]) if np.isfinite(se[h]) else None,
                                            "ci_low": float(lo[h]) if np.isfinite(lo[h]) else None,
                                            "ci_high": float(hi[h]) if np.isfinite(hi[h]) else None,
                                            "p_value": float(2 * stats.norm.sf(abs(z))) if np.isfinite(z) else None,
                                            "n_obs": n_obs})
            lp0 = lp[(lp["h"] == 0)]
            summary[f"{resp}/{shock}"] = {"N": len(codes), "T": n_dates, "H": H,
                                          "lp_h0": {r.shock: round(r.coef, 4) for r in lp0.itertuples()},
                                          "svar_params": [round(float(v), 4) for v in fit["params"]]}
    n = C.write(pl.DataFrame(rows, infer_schema_length=None), "event_irf", settings=s) if rows else 0
    rep = {"rows": n, "window": {k: str(v) for k, v in P["window"].items()}, "w_kind": w_kind, "models": summary,
           "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_irf", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    units = tuple(u.strip().upper() for u in str(p.get("svar_units", ",".join(DEFAULT_UNITS))).split(",") if u.strip())
    rep = run_irf(ctx.settings, horizon=int(p.get("horizon", 10)), w_kind=str(p.get("w_kind", "contiguity")),
                  units=units, draws=int(p.get("draws", 300)))
    return JobResult(status="success", rows_written={"event_irf": rep.get("rows", 0)}, message=str(rep)[:2000])
