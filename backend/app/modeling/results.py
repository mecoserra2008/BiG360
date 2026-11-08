"""Result container shared by every Model Studio method (owner: model-studio).

A Result is plain JSON (no images): summary key/values, coefficient rows, fit statistics, hypothesis tests,
forecasts, clusters, plot data arrays and warnings. `to_dict()` sanitizes NaN/inf to null and numpy scalars to Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np

MAX_PLOT_POINTS = 2000


class GuardrailError(ValueError):
    """A study violates a hard guardrail (look-ahead regressor, too few observations, singular design)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def clean(v: Any) -> Any:
    """JSON-safe copy: NaN/inf -> None, numpy -> python, dates -> ISO."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, datetime):  # pandas Timestamps too: results use plain ISO dates (YYYY-MM-DD) for daily data
        return v.date().isoformat() if (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, np.datetime64):
        return str(v)[:10]
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()[:10]
        except Exception:
            return str(v)
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, np.ndarray):
        return [clean(x) for x in v.tolist()]
    if hasattr(v, "tolist"):
        return clean(v.tolist())
    return str(v)


def downsample_idx(n: int, max_points: int = MAX_PLOT_POINTS) -> np.ndarray:
    if n <= max_points:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, max_points).astype(int))


@dataclass
class Result:
    method: str
    family: str
    title: str
    summary: dict[str, Any] = field(default_factory=dict)
    coefficients: list[dict[str, Any]] = field(default_factory=list)
    fit_stats: dict[str, Any] = field(default_factory=dict)
    tests: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    forecast: dict[str, Any] | None = None
    clusters: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    plots: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    #: non-JSON run artifacts written next to result.json by the engine (e.g. oos_predictions -> parquet); v1 backtest lab
    artifacts: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    def warn(self, code: str, message: str, severity: str = "warning") -> None:
        if not any(w["code"] == code and w["message"] == message for w in self.warnings):
            self.warnings.append({"code": code, "severity": severity, "message": message})

    def info(self, code: str, message: str) -> None:
        self.warn(code, message, "info")

    def add_test(self, name: str, stat: Any, p_value: Any = None, df: Any = None, null: str | None = None,
                 conclusion: str | None = None, group: str = "diagnostics", note: str | None = None,
                 passed: bool | None | str = "auto") -> None:
        """passed: True = the model passes this check. "auto": for group diagnostics the null is the desirable state
        (homoskedastic, no autocorrelation, normal, ...) so passed = p >= 0.05; other groups -> None unless given."""
        p = clean(p_value)
        if conclusion is None and p is not None:
            conclusion = "reject H0 at 5%" if p < 0.05 else "do not reject H0 at 5%"
        if passed == "auto":
            passed = (p >= 0.05) if (group == "diagnostics" and p is not None) else None
        self.tests.append({"name": name, "group": group, "statistic": clean(stat), "p_value": p, "df": clean(df),
                           "null_hypothesis": null, "conclusion": conclusion, "note": note,
                           "passed": None if passed is None else bool(passed)})

    def add_table(self, id: str, title: str, columns: list[str], rows: list[list[Any]], note: str | None = None) -> None:
        self.tables.append({"id": id, "title": title, "columns": columns, "rows": rows, "note": note})

    def line_plot(self, key: str, title: str, series: list[dict[str, Any]], x_label: str = "", y_label: str = "",
                  kind: str = "line", **extra: Any) -> None:
        self.plots[key] = {"kind": kind, "title": title, "x_label": x_label, "y_label": y_label, "series": series,
                           **extra}

    def to_dict(self) -> dict[str, Any]:
        return clean({
            "method": self.method, "family": self.family, "title": self.title, "summary": self.summary,
            "coefficients": self.coefficients, "fit_stats": self.fit_stats, "tests": self.tests,
            "tables": self.tables, "forecast": self.forecast, "clusters": self.clusters,
            "evaluation": self.evaluation, "plots": self.plots, "warnings": self.warnings, "metrics": self.metrics,
        })


def ts_band(res: "Result", key: str, title: str, x: Any, lines: list[tuple[str, Any]],
            bands: list[tuple[str, Any, Any]] | None = None, x_label: str = "date", y_label: str = "",
            hlines: list[dict[str, Any]] | None = None, vlines: list[dict[str, Any]] | None = None) -> None:
    """Plot kind ts_band: shared x, center lines and shaded [lower, upper] bands."""
    idx = downsample_idx(len(x))
    pick = lambda a: [a[i] for i in idx] if a is not None else None  # noqa: E731
    xs = list(x)
    res.plots[key] = clean({"kind": "ts_band", "title": title, "x_label": x_label, "y_label": y_label,
                            "x": pick(xs), "series": [{"name": n, "x": pick(xs), "y": pick(list(v))} for n, v in lines],
                            "bands": [{"name": n, "lower": pick(list(lo)), "upper": pick(list(hi))} for n, lo, hi in (bands or [])],
                            "hlines": hlines or [], "vlines": vlines or []})


def series(name: str, x: Any, y: Any, **extra: Any) -> dict[str, Any]:
    return {"name": name, "x": clean(list(x)), "y": clean(list(y)), **extra}


def coef_rows(names: list[str], est: Any, se: Any, stat: Any, p: Any, ci: Any, **extra_cols: Any) -> list[dict[str, Any]]:
    est, se, stat, p = (np.asarray(a, dtype=float) for a in (est, se, stat, p))
    ci = np.asarray(ci, dtype=float)
    out = []
    for i, n in enumerate(names):
        row = {"term": n, "estimate": est[i], "std_error": se[i], "statistic": stat[i], "p_value": p[i],
               "ci_low": ci[i, 0], "ci_high": ci[i, 1]}
        for k, v in extra_cols.items():
            row[k] = v[i] if isinstance(v, (list, tuple, np.ndarray)) else v
        out.append(clean(row))
    return out


def bh_adjust(p: list[float | None]) -> list[float | None]:
    """Benjamini-Hochberg adjusted p-values (None kept)."""
    idx = [i for i, v in enumerate(p) if v is not None]
    if not idx:
        return list(p)
    vals = np.array([p[i] for i in idx], dtype=float)
    m = len(vals)
    order = np.argsort(vals)
    adj = np.empty(m)
    cummin = 1.0
    for rank in range(m, 0, -1):
        j = order[rank - 1]
        cummin = min(cummin, vals[j] * m / rank)
        adj[j] = cummin
    out: list[float | None] = list(p)
    for k, i in enumerate(idx):
        out[i] = float(min(1.0, adj[k]))
    return out
