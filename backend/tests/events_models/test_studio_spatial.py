"""Model Studio spatial methods (B5): registry, known-parameter recovery, export scripts."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import date

import numpy as np
import polars as pl
import pytest

from app.modeling import _spatial as SP
from app.modeling.registry import FitData, all_methods, get_method
from tests.events_models.test_spatial_core import lattice
from tests.modeling.test_methods import coef, fit, months, ts_data

CODES = ["AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU", "IE", "IT", "LT",
         "LU", "LV", "NL", "PL", "PT", "RO", "SE"]
SPATIAL = {"spatial_lag", "spatial_error", "spatial_durbin", "moran_lisa", "connectedness", "local_projection"}


@pytest.fixture()
def lattice_w(monkeypatch):
    W = lattice(5)

    def fake(kind, codes, as_of=None, k=5, prefer_linkage=True):
        have = [c for c in CODES if c in codes]
        ix = [CODES.index(c) for c in have]
        return have, W[np.ix_(ix, ix)], {"source": "test", "kind": kind, "n": len(have)}

    monkeypatch.setattr("app.events_models.weights.get_weights", fake)
    return W


def country_panel(rho: float, beta: float, T: int = 30, seed: int = 0, durbin: float = 0.0) -> FitData:
    g = np.random.default_rng(seed)
    N = len(CODES)
    Wr = SP.row_standardize(lattice(5))
    A = np.linalg.inv(np.eye(N) - rho * Wr)
    alpha = g.normal(size=N)
    xs, ys = [], []
    for _ in range(T):
        x = g.normal(size=N)
        ys.append(A @ (alpha + beta * x + durbin * Wr @ x + g.normal(size=N)))
        xs.append(x)
    ds = months(T, date(2015, 1, 31))
    f = pl.DataFrame({"date": [d for d in ds for _ in range(N)], "entity_id": CODES * T, "entity_label": CODES * T,
                      "y": np.concatenate(ys), "x": np.concatenate(xs)})
    return FitData(f, {"kind": "panel", "frequency": "M", "variables": ["y", "x"], "lead_variables": []}, "y", ["x"], None, 42)


def test_registry_has_spatial_methods():
    ids = {m.id for m in all_methods()}
    assert SPATIAL <= ids
    for mid in SPATIAL:
        d = get_method(mid).describe()
        assert d["params_schema"]["type"] == "object"
    assert "w_kind" in get_method("spatial_lag").describe()["params_schema"]["properties"]


def test_spatial_lag_recovers_rho(lattice_w):
    res = fit("spatial_lag", country_panel(0.5, 1.0, T=60), effects_draws=200)
    assert coef(res, "rho")["estimate"] == pytest.approx(0.5, abs=0.08)
    assert coef(res, "x")["estimate"] == pytest.approx(1.0, abs=0.08)
    eff = {r[1]: r[2] for r in next(t for t in res["tables"] if t["id"] == "effects")["rows"]}
    assert eff["total"] == pytest.approx(1.0 / (1 - 0.5), abs=0.35) and eff["indirect"] > 0
    assert res["summary"]["w"]["codes"] == sorted(CODES)
    assert any(t["name"] == "LM lag" for t in res["tests"])


def test_spatial_error_and_durbin(lattice_w):
    res = fit("spatial_error", country_panel(0.0, 2.0, seed=3), effects_draws=0)
    assert coef(res, "x")["estimate"] == pytest.approx(2.0, abs=0.12)
    res = fit("spatial_durbin", country_panel(0.3, 1.0, T=60, seed=4, durbin=0.8), effects_draws=100)
    assert coef(res, "W*x")["estimate"] == pytest.approx(0.8, abs=0.25)
    assert coef(res, "rho")["estimate"] == pytest.approx(0.3, abs=0.12)


def test_spatial_needs_countries():
    d = country_panel(0.2, 1.0)
    d.frame = d.frame.with_columns(pl.col("entity_id").str.to_lowercase() + "x")
    with pytest.raises(Exception, match="country"):
        fit("spatial_lag", d)


def test_moran_lisa_method(lattice_w):
    g = np.random.default_rng(5)
    x = g.normal(size=25)
    x[[0, 1, 5, 6]] += 4    # planted cluster in the grid corner
    f = pl.DataFrame({"date": [date(2020, 12, 31)] * 25, "entity_id": CODES, "entity_label": CODES, "y": x})
    d = FitData(f, {"kind": "cross_section", "frequency": "M", "variables": ["y"], "lead_variables": []}, "y", [], None, 1)
    res = fit("moran_lisa", d, permutations=499)
    assert res["summary"]["moran_i"] > 0.2 and res["summary"]["p_sim"] < 0.05
    lisa = {r[0]: r for r in next(t for t in res["tables"] if t["id"] == "lisa")["rows"]}
    assert lisa["AT"][4] == "HH" or lisa["AT"][7].startswith("hot")


def test_connectedness_method():
    g = np.random.default_rng(6)
    T = 1500
    A = np.array([[0.5, 0, 0], [0.4, 0.2, 0], [0.4, 0, 0.2]])
    Y = np.zeros((T, 3))
    for t in range(1, T):
        Y[t] = A @ Y[t - 1] + g.normal(size=3)
    d = ts_data({"a": Y[:, 0], "b": Y[:, 1], "c": Y[:, 2]}, None, ["a", "b", "c"])
    res = fit("connectedness", d, window=300, step=50)
    rows = {r[0]: r for r in next(t for t in res["tables"] if t["id"] == "directional")["rows"]}
    assert rows["a"][3] > 10 and rows["b"][3] < 0
    assert 10 < res["summary"]["total"] < 60 and "rolling_total" in res["plots"]


def test_local_projection_method():
    g = np.random.default_rng(7)
    T = 1200
    s = g.normal(size=T)
    y = np.convolve(s, [0.8, 0.5, 0.2])[:T] + g.normal(scale=0.3, size=T)
    res = fit("local_projection", ts_data({"y": y, "s": s}, "y", ["s"]), horizon=3, y_lags=0, cumulative=False)
    irf = [r[2] for r in next(t for t in res["tables"] if t["id"] == "irf")["rows"]]
    assert irf == pytest.approx([0.8, 0.5, 0.2, 0.0], abs=0.06)


def test_spatial_local_projection(lattice_w):
    d = country_panel(0.0, 1.0, T=80, seed=8)
    res = fit("local_projection", d, horizon=2, spatial=True, y_lags=0, cumulative=False)
    shocks = {r[0] for r in next(t for t in res["tables"] if t["id"] == "irf")["rows"]}
    assert shocks == {"x", "W_x"}
    assert coef(res, "x h=0")["estimate"] == pytest.approx(1.0, abs=0.1)


def _run_script(src: str, frame: pl.DataFrame) -> dict:
    ns: dict = {"__name__": "exported"}
    exec(compile(src, "exported.py", "exec"), ns)  # noqa: S102 - our own generated script
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ns["fit"](frame)
    line = next(ln for ln in buf.getvalue().splitlines() if ln.startswith("RESULT_JSON "))
    return json.loads(line[len("RESULT_JSON "):])


@pytest.mark.parametrize("method,params", [("spatial_lag", {"effects_draws": 50}), ("spatial_durbin", {"effects_draws": 50}),
                                           ("spatial_error", {"effects_draws": 0})])
def test_export_reproduces_spatial(lattice_w, tmp_path, method, params):
    from app.modeling.datasets import DatasetSpec
    from app.modeling.export import export_script
    from app.modeling.registry import StudySpec

    d = country_panel(0.4, 1.0, T=40, seed=9)
    res = fit(method, d, **params)
    spec = StudySpec(dataset=DatasetSpec.model_validate({"kind": "panel", "entity_level": "country", "variables": [
        {"name": "y", "source": "prices", "field": "ret"}, {"name": "x", "source": "prices", "field": "ret"}]}),
        method=method, y="y", x=["x"], params=params)
    env = {"run_id": "Rtest", "study": spec.model_dump(mode="json"), "result": res, "reproducibility": {}}
    src = export_script(env, tmp_path, tmp_path)
    out = _run_script(src, d.frame)
    rho = coef(res, "lambda" if method == "spatial_error" else "rho")["estimate"]
    assert out["rho"] == pytest.approx(rho, abs=1e-6)


def test_export_connectedness_and_lp(tmp_path):
    from app.modeling.datasets import DatasetSpec
    from app.modeling.export import export_script
    from app.modeling.registry import StudySpec

    g = np.random.default_rng(10)
    Y = g.normal(size=(400, 3))
    d = ts_data({"a": Y[:, 0], "b": Y[:, 1], "c": Y[:, 2]}, None, ["a", "b", "c"])
    res = fit("connectedness", d)
    ds = DatasetSpec.model_validate({"kind": "timeseries", "variables": [{"name": v, "source": "prices", "field": "ret"}
                                                                          for v in "abc"]})
    spec = StudySpec(dataset=ds, method="connectedness", x=["a", "b", "c"])
    src = export_script({"run_id": "R1", "study": spec.model_dump(mode="json"), "result": res, "reproducibility": {}},
                        tmp_path, tmp_path)
    assert _run_script(src, d.frame)["total"] == pytest.approx(res["summary"]["total"], abs=1e-9)
    spec = StudySpec(dataset=ds, method="local_projection", y="a", x=["b"], params={"horizon": 2})
    res = fit("local_projection", ts_data({"a": Y[:, 0], "b": Y[:, 1]}, "a", ["b"]), horizon=2)
    src = export_script({"run_id": "R2", "study": spec.model_dump(mode="json"), "result": res, "reproducibility": {}},
                        tmp_path, tmp_path)
    out = _run_script(src, d.frame)
    assert out["irf"][0] == pytest.approx(coef(res, "b h=0")["estimate"], abs=1e-9)
