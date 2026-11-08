"""Model Studio method registry + study spec (owner: model-studio).

Method protocol (see app/modeling/methods/*):
    class MyMethod(Method):
        id = "ols"; family = "regression"; title = "..."; description = "..."
        shapes = ("timeseries", "panel", "cross_section")      # dataset kinds accepted
        needs_y = True; min_x = 1; max_x = None; uses_weights = False
        Params = MyParams                                      # pydantic model -> JSON schema for the UI
        def cost(self, n_rows, n_x, params) -> float: ...      # rough seconds (sync vs async decision)
        def fit(self, data: FitData, params) -> Result: ...

`describe()` gives the catalog entry (param JSON schema, data shape, outputs, diagnostics).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar

import numpy as np
import pandas as pd
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .datasets import NAME_RX, DatasetSpec
from .results import GuardrailError, Result

PRESETS_DIR = Path(__file__).resolve().parent / "presets"


class StudySpec(BaseModel):
    """One model run: a dataset spec + a method + role assignment + method params."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    dataset: DatasetSpec
    method: str
    y: str | None = Field(default=None, description="dependent variable (column of the dataset)")
    x: list[str] = Field(default_factory=list, description="regressors / features (columns of the dataset)")
    weights: str | None = Field(default=None, description="WLS / GLM weights column")
    params: dict[str, Any] = Field(default_factory=dict)
    seed: int = 42
    timeout_s: int = Field(default=900, ge=5, le=3600)

    @field_validator("x")
    @classmethod
    def _uniq(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("x contains duplicates")
        return v

    def spec_hash(self) -> str:
        d = self.model_dump(mode="json", exclude={"name", "timeout_s"})
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()


@dataclass
class FitData:
    frame: pl.DataFrame
    meta: dict[str, Any]
    y: str | None
    x: list[str]
    weights: str | None
    seed: int
    check: Callable[[], None] = field(default=lambda: None)

    @property
    def kind(self) -> str:
        return self.meta["kind"]

    @property
    def freq(self) -> str:
        return self.meta["frequency"]

    @property
    def is_panel(self) -> bool:
        return "entity_id" in self.frame.columns

    def columns(self, extra: list[str] | None = None) -> list[str]:
        cols = ([self.y] if self.y else []) + list(self.x) + ([self.weights] if self.weights else []) + (extra or [])
        return list(dict.fromkeys(cols))

    def complete(self, extra: list[str] | None = None) -> tuple[pd.DataFrame, int]:
        """Rows with no null/NaN in the used columns (listwise deletion), as pandas; returns (df, n_dropped)."""
        cols = self.columns(extra)
        keep = ["date"] + (["entity_id", "entity_label"] if self.is_panel else [])
        f = self.frame.select(keep + cols)
        n0 = f.height
        cond = pl.lit(True)
        for c in cols:
            e = pl.col(c).cast(pl.Float64)
            cond = cond & e.is_not_null() & e.is_finite()
        f = f.filter(cond)
        df = f.to_pandas()
        for c in cols:
            df[c] = df[c].astype(float)
        return df, n0 - f.height


class Method:
    id: ClassVar[str] = ""
    family: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    shapes: ClassVar[tuple[str, ...]] = ("timeseries", "panel", "cross_section")
    needs_y: ClassVar[bool] = True
    min_x: ClassVar[int] = 0
    max_x: ClassVar[int | None] = None
    uses_weights: ClassVar[bool] = False
    outputs: ClassVar[tuple[str, ...]] = ()
    diagnostics: ClassVar[tuple[str, ...]] = ()
    Params: ClassVar[type[BaseModel]]

    def parse_params(self, params: dict[str, Any]) -> BaseModel:
        return self.Params.model_validate(params or {})

    def validate_roles(self, study: StudySpec) -> None:
        if study.dataset.kind not in self.shapes:
            raise GuardrailError("bad_shape", f"{self.id} needs a {' / '.join(self.shapes)} dataset, got {study.dataset.kind}")
        if self.needs_y and not study.y:
            raise GuardrailError("missing_y", f"{self.id} needs a dependent variable (y)")
        if len(study.x) < self.min_x:
            raise GuardrailError("missing_x", f"{self.id} needs at least {self.min_x} regressor/feature column(s)")
        if self.max_x is not None and len(study.x) > self.max_x:
            raise GuardrailError("too_many_x", f"{self.id} accepts at most {self.max_x} x columns")
        if study.weights and not self.uses_weights:
            raise GuardrailError("weights_unsupported", f"{self.id} does not use weights")

    def cost(self, n_rows: int, n_x: int, params: BaseModel) -> float:
        return 0.05 + n_rows * max(n_x, 1) * 2e-7

    def extra_columns(self, params: BaseModel) -> list[str]:
        """Dataset columns referenced inside params (instruments, treatment indicator, ...)."""
        return []

    def regressor_columns(self, params: BaseModel) -> list[str]:
        """Params columns that act as regressors/instruments (subject to the look-ahead guardrail)."""
        return []

    def fit(self, data: FitData, params: BaseModel) -> Result:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "family": self.family, "title": self.title, "description": self.description,
                "shapes": list(self.shapes), "needs_y": self.needs_y, "min_x": self.min_x, "max_x": self.max_x,
                "uses_weights": self.uses_weights, "outputs": list(self.outputs),
                "diagnostics": list(self.diagnostics), "params_schema": self.Params.model_json_schema()}


_REGISTRY: dict[str, Method] = {}


def register(cls: type[Method]) -> type[Method]:
    _REGISTRY[cls.id] = cls()
    return cls


_LOADED = False


def _load() -> None:
    # an explicit flag, not "registry empty": importing a single methods module elsewhere (e.g. app.macro.expectations
    # reusing the SARIMAX helpers) registers a few methods and must not stop the rest from loading
    global _LOADED
    if not _LOADED:
        from .methods import causal, clustering, glm, ml, multivariate, panel, regression, timeseries, volatility  # noqa: F401
        from .methods import spatial  # noqa: F401  (event-models B5)
        from .methods import conformal  # noqa: F401  (conformal P3)
        from .methods import advml, controls, neural  # noqa: F401  (Studio v2 S2: causal / controls, advanced ML, neural nets)
        from .methods import probabilistic  # noqa: F401  (Studio v2 S3: Bayesian / GP / quantile / NGBoost / hierarchical)
        _LOADED = True


def get_method(method_id: str) -> Method:
    _load()
    if method_id not in _REGISTRY:
        raise GuardrailError("unknown_method", f"unknown method {method_id!r}; one of {sorted(_REGISTRY)}")
    return _REGISTRY[method_id]


def all_methods() -> list[Method]:
    _load()
    return list(_REGISTRY.values())


# ------------------------------------------------------------------------------------------------ presets


def list_presets() -> list[dict[str, Any]]:
    out = []
    for p in sorted(PRESETS_DIR.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        StudySpec.model_validate(doc["study"])  # presets must stay valid
        out.append({"id": p.stem, "title": doc["title"], "description": doc.get("description", ""),
                    "method": doc["study"]["method"], "tags": doc.get("tags", []), "study": doc["study"],
                    # Studio v2 S3 recipes (v1.50, optional): question group, method note, expected outputs, measured runtime
                    "question": doc.get("question"), "method_note": doc.get("method_note"),
                    "expected_outputs": doc.get("expected_outputs") or [], "runtime_s": doc.get("runtime_s"),
                    "validated": doc.get("validated")})
    return out


def get_preset(preset_id: str) -> dict[str, Any] | None:
    return next((p for p in list_presets() if p["id"] == preset_id), None)


def as_float_matrix(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    return df[cols].to_numpy(dtype=float)


__all__ = ["StudySpec", "FitData", "Method", "register", "get_method", "all_methods", "list_presets", "get_preset",
           "NAME_RX"]
