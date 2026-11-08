"""Curve reconstruction: Svensson spot / forward / par against the values the ECB and the Bundesbank publish for the
same parameter sets (recorded fixtures; tolerances: ECB 1e-6 % on spot / forward, 0.05 bp on par; Bundesbank 0.5 bp,
it publishes 2 decimals)."""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from app.rates import svensson as SV
from app.rates.sources import parse_bundesbank_csv, parse_ecb_params, svensson_rows

F = Path(__file__).parent / "fixtures"


def _published() -> dict[tuple[str, str, date], float]:
    out = {}
    for r in csv.DictReader(io.StringIO((F / "ecb_yc_published.csv").read_text(encoding="utf-8"))):
        cid = {"G_N_A": "EA_AAA", "G_N_C": "EA_ALL"}[r["INSTRUMENT_FM"]]
        out[(cid, r["DATA_TYPE_FM"], date.fromisoformat(r["TIME_PERIOD"]))] = float(r["OBS_VALUE"])
    return out


@pytest.mark.parametrize("measure", ["SR", "IF", "PY"])
def test_ecb_reconstruction_matches_published(measure):
    params = parse_ecb_params((F / "ecb_yc_params.csv").read_text(encoding="utf-8"))
    pub = _published()
    n = 0
    for (cid, d), p in params.items():
        pv = (p["BETA0"], p["BETA1"], p["BETA2"], p["BETA3"], p["TAU1"], p["TAU2"])
        for lab, t in (("3M", 0.25), ("2Y", 2.0), ("10Y", 10.0), ("30Y", 30.0)):
            key = (cid, f"{measure}_{lab}", d)
            if key not in pub:
                continue
            if measure == "SR":
                got, tol = SV.spot([t], *pv)[0], 1e-6
            elif measure == "IF":
                got, tol = SV.forward([t], *pv)[0], 1e-6
            else:
                got, tol = SV.par_continuous([t], *pv)[0], 0.0005
            assert got == pytest.approx(pub[key], abs=tol), key
            n += 1
    assert n >= 6


def test_bundesbank_reconstruction_matches_published_zero_yields():
    params = parse_bundesbank_csv((F / "buba_svensson.csv").read_text(encoding="utf-8"))
    zero = {}
    for line in (F / "buba_zero.csv").read_text(encoding="utf-8").splitlines():
        parts = line.split(";")
        try:
            d = date.fromisoformat(parts[0])
        except ValueError:
            continue
        zero[d] = [float(x.replace(",", ".")) for x in parts[1::2] if x]
    assert zero
    for d, (z2, z10, z30) in zero.items():
        p = params[d]
        pv = (p["BETA0"], p["BETA1"], p["BETA2"], p["BETA3"], p["TAU1"], p["TAU2"])
        got = SV.spot([2.0, 10.0, 30.0], *pv)
        assert np.allclose(got, [z2, z10, z30], atol=0.005 + 1e-9), (d, got)


def test_par_identities_and_nelson_siegel():
    p = (3.0, -1.0, 2.0, 0.0, 1.5, 5.0)            # beta3 = 0 -> Nelson-Siegel
    t = np.array([0.5, 2.0, 10.0])
    assert np.allclose(SV.spot(t, *p), SV.spot(t, 3.0, -1.0, 2.0, 0.0, 1.5, 99.0))
    # flat curve: spot = forward = par
    flat = (4.0, 0.0, 0.0, 0.0, 1.0, 1.0)
    assert np.allclose(SV.spot(t, *flat), 4.0) and np.allclose(SV.forward(t, *flat), 4.0)
    assert np.allclose(SV.par_continuous(t, *flat), 4.0, atol=1e-4)
    assert SV.par_annual([10.0], *flat)[0] == pytest.approx((np.exp(0.04) - 1) * 100, abs=1e-6)
    # spot is the average of the forward curve
    grid = np.linspace(1e-6, 10.0, 20001)
    assert np.trapezoid(SV.forward(grid, *p), grid) / 10.0 == pytest.approx(SV.spot([10.0], *p)[0], abs=1e-4)


def test_svensson_rows_grid_and_params():
    prm = {date(2026, 10, 30): {"BETA0": 3.97906, "BETA1": -1.54899, "BETA2": 25.7058, "BETA3": -26.35599,
                                "TAU1": 1.48317, "TAU2": 1.58613}}
    res = svensson_rows("DE_BUND", "bundesbank", prm, date(2026, 1, 1))
    assert len(res.params) == 1 and res.params[0]["model"] == "svensson"
    by = {(r["type"], r["tenor_months"]): r["value_bp_x100"] for r in res.rows}
    assert by[("spot", 120)] == 34028 and by[("spot", 24)] == 30583      # = published 3.40 / 3.06 (2 dp)
    assert len(res.rows) == 3 * 19 and all(isinstance(v, int) for v in by.values())
    assert svensson_rows("DE_BUND", "bundesbank", prm, date(2027, 1, 1)).rows == []    # window start respected
