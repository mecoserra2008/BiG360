"""P4 reliability check of the calibrated touch probabilities (US mvp, 2011-2026, EWMA envelope).

For each test year Y, the touch curves (quantiles of the standardised running maximum of highs / minimum of lows) are
calibrated on the origins of year Y-1 whose paths completed before the test origin (pooled US), exactly as the
nightly job does; for every test origin and a grid of hypothetical levels at standardised distances u, the predicted
P(touch within h) is compared with the realised touch frequency. Output: data/reports/conformal/touch_reliability.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402

from app.conformal import envelope as V  # noqa: E402
from app.conformal import validation as W  # noqa: E402

U_GRID = (0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)


def main() -> None:
    p = W.prepare("US")
    s2 = V.ewma_var(p.a.r, p.a.E)
    Vh = V.horizon_V(s2, V.jump_var(p.a.r, p.a.E, s2), p.ke)
    up, dn = V.running_extremes(p.a)
    Mu = up / np.sqrt(Vh)
    Md = dn / np.sqrt(Vh)
    out = {"by_horizon": {}, "note": "predicted = 1 - F_cal(u) (calibration year Y-1, pooled US mvp); realised = share of test "
           "origins whose standardised running extreme reached u; both sides pooled"}
    for k, h in enumerate(V.HORIZONS):
        rows = {u: [0.0, 0.0, 0] for u in U_GRID}
        for y in range(2011, 2027):
            t0, t1 = W.year_bounds(p.dates, y)
            c0, _ = W.year_bounds(p.dates, y - 1)
            if t0 < 0 or c0 < 0:
                continue
            cal = np.concatenate([Mu[c0:t0 - h - 1, :, k].ravel(), Md[c0:t0 - h - 1, :, k].ravel()])
            cal = np.sort(cal[np.isfinite(cal)])
            test = np.concatenate([Mu[t0:t1, :, k].ravel(), Md[t0:t1, :, k].ravel()])
            test = test[np.isfinite(test)]
            if cal.size < 1000 or test.size < 1000:
                continue
            for u in U_GRID:
                pred = 1.0 - np.searchsorted(cal, u, side="left") / cal.size
                real = float((test >= u).mean())
                r = rows[u]
                r[0] += pred * test.size
                r[1] += real * test.size
                r[2] += test.size
        out["by_horizon"][str(h)] = {str(u): {"predicted": r[0] / r[2], "realised": r[1] / r[2], "n": r[2]}
                                     for u, r in rows.items() if r[2]}
    f = W.report_dir() / "touch_reliability.json"
    f.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
