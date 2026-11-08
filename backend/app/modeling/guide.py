"""Studio v2 S3 guided builder (owner: model-studio): a structured research question -> a suggested StudySpec (method,
controls, cross-validation) by explicit rules (no LLM), and a pre-run linter for look-ahead, leakage, survivorship and
tiny-sample problems. Both are pure functions over the StudySpec; POST /studio/guide/suggest and /studio/lint expose them.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._core import LEAD_OPS, SOURCES
from .registry import StudySpec, get_method

Goal = Literal["effect", "predict", "rank", "uncertainty", "heterogeneity", "regime"]

#: rules-only control sets on real catalog fields (security-level panels): size, value, momentum, profitability,
#: volatility; industry enters as sector dummies
DEFAULT_CONTROLS: list[dict[str, Any]] = [
    {"name": "log_mcap", "source": "valuation", "field": "market_cap_usd", "transforms": [{"op": "log"}, {"op": "cs_zscore"}], "why": "size"},
    {"name": "value_score", "source": "factor_scores", "field": "value", "transforms": [{"op": "cs_zscore"}], "why": "value"},
    {"name": "mom_12_1", "source": "tech_factors", "field": "mom", "transforms": [{"op": "winsorize"}, {"op": "cs_zscore"}], "why": "momentum (12-1)"},
    {"name": "gross_prof", "source": "ratios", "field": "gross_profitability", "transforms": [{"op": "winsorize"}, {"op": "cs_zscore"}], "why": "profitability"},
    {"name": "ivol", "source": "tech_factors", "field": "ivol", "transforms": [{"op": "winsorize"}, {"op": "cs_zscore"}], "why": "idiosyncratic volatility"},
]
#: macro time-series controls (official FRED series): rates, credit, risk appetite
MACRO_CONTROLS: list[dict[str, Any]] = [
    {"name": "d_10y", "source": "macro", "field": "fred:DGS10", "transforms": [{"op": "diff"}], "why": "rates level shock"},
    {"name": "d_baa", "source": "macro", "field": "fred:BAA10Y", "transforms": [{"op": "diff"}], "why": "credit spread shock"},
    {"name": "d_vix", "source": "macro", "field": "fred:VIXCLS", "transforms": [{"op": "diff"}], "why": "risk appetite"},
]


class QVar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    field: str
    name: str | None = Field(default=None, max_length=63)
    transforms: list[dict[str, Any]] | None = Field(default=None, description="explicit transforms (default: rules)")
    binary: bool = Field(default=False, description="driver is a 0/1 treatment / event indicator")


class Question(BaseModel):
    """'Does <driver> move <outcome> over <horizon>, controlling for <controls>, in <universe>?' in structured form."""
    model_config = ConfigDict(extra="forbid")
    goal: Goal = Field(default="effect", description="effect: causal / partial effect of the driver; predict: forecast "
                       "the outcome; rank: order entities each date; uncertainty: predictive distribution / intervals; "
                       "heterogeneity: does the effect differ by sector; regime: state-dependent behaviour")
    outcome: QVar
    outcome_kind: Literal["return", "level", "change", "binary"] = "return"
    driver: QVar | None = None
    controls: list[QVar] = Field(default_factory=list)
    suggest_controls: bool = True
    horizon: int = Field(default=1, ge=0, le=60, description="periods ahead the outcome is measured (0 = same period)")
    frequency: Literal["D", "W", "M", "Q"] = "M"
    universe: str | None = Field(default="mvp_us", description="security universe (panel); null = macro time series")
    sectors: list[str] | None = None
    kind: Literal["auto", "panel", "timeseries", "cross_section"] = "auto"
    start: date | None = None
    end: date | None = None


def _is_security(source: str) -> bool:
    return source == "bql" or (SOURCES.get(source) or {}).get("entity") in ("security", "issuer")


def _vname(v: QVar, default: str) -> str:
    if v.name:
        return v.name
    base = v.field.split(":")[-1].lower()
    out = "".join(ch if ch.isalnum() else "_" for ch in base).strip("_") or default
    return (out if out[0].isalpha() else f"v_{out}")[:40]


def _outcome_var(q: Question, kind: str) -> tuple[dict[str, Any], str]:
    o = q.outcome
    nm = _vname(o, "y")
    tr: list[dict[str, Any]] = list(o.transforms or [])
    if o.transforms is None:
        h = q.horizon
        if q.outcome_kind in ("return", "binary") and o.source == "prices" and o.field in ("ret", "log_ret"):
            if h >= 1:
                tr.append({"op": "fwd_compound" if o.field == "ret" else "fwd_sum", "k": h})
        elif q.outcome_kind == "change":
            tr.append({"op": "diff"})
            if h >= 1:
                tr.append({"op": "lead", "k": h})
        elif h >= 1:
            tr.append({"op": "lead", "k": h})
        if q.outcome_kind == "binary":
            if kind == "panel":
                tr.append({"op": "cs_demean"})
            tr.append({"op": "binarize", "threshold": 0.0})
        elif kind == "panel" and q.outcome_kind == "return":
            tr.append({"op": "winsorize"})
    nm = f"{nm}_fwd{q.horizon}" if q.horizon and o.transforms is None else nm
    v = {"name": nm[:63], "source": o.source, "field": o.field, "transforms": tr}
    if kind == "timeseries" and _is_security(o.source):
        v["entity_agg"] = "mean"
        v["entities"] = {"universe": q.universe or "mvp_us", **({"sectors": q.sectors} if q.sectors else {})}
    return v, v["name"]


def _x_var(v: QVar, kind: str, q: Question, default: str) -> dict[str, Any]:
    tr = v.transforms
    if tr is None:
        if kind == "panel" and _is_security(v.source) and not v.binary:
            tr = [{"op": "winsorize"}, {"op": "cs_zscore"}]
        elif v.source == "macro" and not v.binary:
            tr = [{"op": "diff"}]
        else:
            tr = []
    out = {"name": _vname(v, default), "source": v.source, "field": v.field, "transforms": tr}
    if kind == "timeseries" and _is_security(v.source):
        out["entity_agg"] = "mean"
        out["entities"] = {"universe": q.universe or "mvp_us", **({"sectors": q.sectors} if q.sectors else {})}
    return out


def _cv(kind: str, h: int) -> dict[str, Any]:
    p = max(1, h)
    if kind == "panel":
        return {"folds": 5, "scheme": "purged_kfold", "purge": p, "embargo": p}
    if kind == "timeseries":
        return {"folds": 5, "scheme": "expanding", "purge": p}
    return {"folds": 5, "scheme": "kfold"}


def suggest(q: Question) -> dict[str, Any]:
    """Rules: dataset kind from the outcome's entity, the forward transform from the horizon, controls from the
    default sets (size / value / momentum / profitability / volatility + sector dummies for panels; rates / credit /
    VIX for macro series), the method from the goal x data shape, and a purged CV with purge = embargo = horizon."""
    kind = q.kind
    if kind == "auto":
        kind = "panel" if (_is_security(q.outcome.source) and q.universe) else "timeseries"
    why: list[str] = [f"dataset: {kind} ({'security outcome over universe ' + str(q.universe) if kind == 'panel' else 'one series per date'})"]
    yv, y = _outcome_var(q, kind)
    if q.horizon:
        why.append(f"target {y}: outcome measured {q.horizon} period(s) ahead (forward transform; only the target may look forward)")
    variables = [yv]
    x: list[str] = []
    driver = None
    if q.driver:
        dv = _x_var(q.driver, kind, q, "driver")
        variables.append(dv)
        driver = dv["name"]
    ctrl_names: list[str] = []
    for i, c in enumerate(q.controls):
        cv_ = _x_var(c, kind, q, f"c{i + 1}")
        variables.append(cv_)
        ctrl_names.append(cv_["name"])
    dummies: list[str] = []
    if q.suggest_controls and not q.controls:
        pool = DEFAULT_CONTROLS if kind in ("panel", "cross_section") else MACRO_CONTROLS
        taken = {(v["source"], v["field"]) for v in variables}
        for c in pool:
            if (c["source"], c["field"]) in taken:
                continue
            variables.append({k: c[k] for k in ("name", "source", "field", "transforms")})
            ctrl_names.append(c["name"])
        why.append("controls (rules): " + ", ".join(f"{c['name']} = {c['why']}" for c in pool if c["name"] in ctrl_names))
        if kind == "panel" and q.goal in ("effect", "predict") and not q.sectors:
            dummies = ["sector"]
            why.append("industry: sector dummies (absorb sector-wide shocks)")
    names = {v["name"] for v in variables}
    if len(names) < len(variables):  # de-duplicate names
        seen: set[str] = set()
        for v in variables:
            while v["name"] in seen:
                v["name"] = (v["name"] + "_2")[:63]
            seen.add(v["name"])
        y, driver = variables[0]["name"], (variables[1]["name"] if q.driver else None)
        ctrl_names = [v["name"] for v in variables[(2 if q.driver else 1):]]
    cv = _cv(kind, q.horizon)
    method, params, alts = _method(q, kind, driver, cv, why)
    if method in ("dml", "propensity", "coef_stability") and driver:
        x = ctrl_names
    else:
        x = ([driver] if driver else []) + ctrl_names
    ds: dict[str, Any] = {"kind": kind, "frequency": q.frequency, "variables": variables}
    if q.start:
        ds["start"] = q.start.isoformat()
    if q.end:
        ds["end"] = q.end.isoformat()
    if kind != "timeseries":
        ds["entities"] = {"universe": q.universe or "mvp_us", **({"sectors": q.sectors} if q.sectors else {})}
        if dummies:
            ds["dummies"] = dummies
    study = {"name": f"{q.goal}: {y}" + (f" on {driver}" if driver else ""), "method": method, "y": y, "x": x,
             "params": params, "dataset": ds}
    if dummies and method not in ("dml", "propensity", "coef_stability", "hier_pool", "ltr"):
        why.append("note: sector dummies are added to the dataset; tick them as regressors to use them")
    spec = StudySpec.model_validate(study)
    return {"study": spec.model_dump(mode="json", exclude_none=True), "method": method, "rationale": why,
            "alternatives": alts, "lint": lint(spec)}


def _method(q: Question, kind: str, driver: str | None, cv: dict[str, Any], why: list[str]) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
    g = q.goal
    alts: list[dict[str, str]] = []
    if g == "effect":
        if kind == "panel" and driver:
            if q.driver and q.driver.binary:
                why.append("binary driver on a panel with controls: propensity scores (IPW / AIPW) with a balance table")
                alts = [{"method": "dml", "why": "interactive regression model (IRM) on the same controls"},
                        {"method": "did", "why": "if the driver switches on at known dates (event study)"}]
                return "propensity", {"treatment": driver, "crossfit": {"folds": 5}}, alts
            why.append("continuous driver + many controls on a panel: double machine learning (partially linear, "
                       "cross-fitted LightGBM nuisances), errors clustered by date")
            alts = [{"method": "fama_macbeth", "why": "classic cross-sectional premium with Newey-West over dates"},
                    {"method": "coef_stability", "why": "how much the driver's beta moves as controls are added (Oster delta)"},
                    {"method": "panel", "why": "two-way fixed effects linear benchmark"}]
            return "dml", {"treatment": driver, "learner_y": "lgbm", "learner_d": "lgbm", "crossfit": {"folds": 5}, "cluster": "date"}, alts
        if kind == "timeseries":
            lags = max(1, min(q.horizon or 3, 6))
            why.append(f"one series per date: distributed-lag OLS with HAC errors ({lags} lags) for the dynamic effect")
            alts = [{"method": "local_projection", "why": "impulse response by horizon (Jorda)"},
                    {"method": "markov_switching", "why": "if the effect is regime dependent"}]
            return "distributed_lag", {"lags": lags, "cov_type": "HAC"}, alts
        why.append("cross-section: OLS with heteroskedasticity-robust errors")
        return "ols", {"cov_type": "HC3"}, [{"method": "dml", "why": "flexible controls"}]
    if g == "predict":
        if kind == "panel":
            why.append("panel forecast: LightGBM on purged / embargoed K-fold (purge = embargo = horizon); a linear "
                       "baseline and a Diebold-Mariano test are reported")
            alts = [{"method": "regularized", "why": "elastic net, interpretable"}, {"method": "stacking", "why": "ensemble with a DM test vs the best base"},
                    {"method": "mlp", "why": "neural net with a mandatory linear baseline"}]
            return "lightgbm", {"cv": cv, "n_estimators": 400, "learning_rate": 0.05, "min_child_samples": 200}, alts
        why.append("time-series forecast: ridge on an expanding window (walk-forward, purge = horizon)")
        alts = [{"method": "arima", "why": "univariate benchmark"}, {"method": "lightgbm", "why": "non-linear"}]
        return "regularized", {"model": "ridge", "cv": cv}, alts
    if g == "rank":
        why.append("ranking each date: LightGBM LambdaRank grouped by date, scored by rank IC / NDCG and quintile spreads")
        return "ltr", {"cv": cv}, [{"method": "lightgbm", "why": "pointwise regression then rank"}]
    if g == "uncertainty":
        try:
            from . import engine

            rows = engine.prior_parts(StudySpec.model_validate({"method": "bayes_ridge", "y": "y", "x": [], "dataset": {
                "kind": kind, "frequency": q.frequency, "variables": [{"name": "y", "source": q.outcome.source, "field": q.outcome.field}],
                **({"entities": {"universe": q.universe}} if kind != "timeseries" else {})}}))["rows"]
        except Exception:  # noqa: BLE001
            rows = 10_000
        cvc = {**cv, "conformal_alpha": 0.1}
        if rows <= 1500:
            why.append(f"about {rows} rows: Gaussian-process regression (full predictive distribution; O(n^3) is fine here) "
                       "with 90 % conformal intervals")
            return "gp_regression", {"cv": cvc}, [{"method": "bayes_ridge", "why": "linear Bayesian benchmark"},
                                                  {"method": "ngboost", "why": "heteroskedastic boosting"}]
        why.append("predictive distribution: quantile LightGBM (5 levels) + CQR conformal 90 % intervals; CRPS / PIT vs "
                   "a linear-Gaussian baseline")
        return "quantile_gbm", {"cv": cvc}, [{"method": "ngboost", "why": "parametric (Normal) heteroskedastic"},
                                             {"method": "bayes_ridge", "why": "linear Bayesian benchmark"}]
    if g == "heterogeneity":
        if kind != "timeseries":
            why.append("effect by sector: hierarchical partial pooling (Gibbs, 4 chains, R-hat / ESS), vs complete and no pooling")
            return "hier_pool", {"group": "sector_id", "estimator": "gibbs", "cv": cv}, [
                {"method": "panel", "why": "interactions driver x sector dummies"}]
        why.append("time series: Markov-switching regression (state-dependent coefficients)")
        return "markov_switching", {"k_regimes": 2}, []
    why.append("regime-dependent behaviour: 2-state Markov-switching regression")
    return "markov_switching", {"k_regimes": 2}, [{"method": "gmm", "why": "clustering of macro states"}]


# ------------------------------------------------------------------------------------------------ linter


_UNI_START: dict[str, Any] = {}


def _universe_start(universe: str) -> date | None:
    """First valid_from of a universe in universe_membership (cached per process; None when unknown)."""
    if universe not in _UNI_START:
        try:
            from .datasets import lake_connection

            con = lake_connection(["universe_membership"])
            r = con.execute("SELECT min(valid_from) FROM universe_membership WHERE universe_id = ?", [universe]).fetchone()
            _UNI_START[universe] = r[0] if r else None
        except Exception:  # noqa: BLE001 - no lake (tests) / table missing
            _UNI_START[universe] = None
    return _UNI_START[universe]


def _tr_key(v: Any) -> tuple:
    return (v.source, v.field, tuple((t.op, t.k) for t in v.pre_transforms + v.transforms if t.op not in LEAD_OPS))


def lint(study: StudySpec) -> dict[str, Any]:
    """Pre-run checks. severity error = the run would be invalid (blocked by the engine or meaningless); warning = a
    likely bias; info = worth knowing. Each issue carries a fix."""
    issues: list[dict[str, Any]] = []

    def add(code: str, sev: str, msg: str, fix: str, cols: list[str] | None = None) -> None:
        issues.append({"code": code, "severity": sev, "message": msg, "fix": fix, "columns": cols or []})

    ds = study.dataset
    var = {v.name: v for v in ds.variables}
    try:
        m = get_method(study.method)
        params = m.parse_params(study.params)
        regs = list(study.x) + m.regressor_columns(params) + ([study.weights] if study.weights else [])
    except Exception as e:  # noqa: BLE001
        add("bad_params", "error", f"parameters do not validate: {e}", "fix the method parameters")
        m, params, regs = None, None, list(study.x)
    # ---- look-ahead
    leads = [c for c in regs if c in var and var[c].uses_future]
    if leads:
        add("lookahead_regressor", "error", f"{', '.join(leads)} use a lead / forward transform: a regressor would see the future",
            "remove the lead / fwd_* transform from regressors (only the target may look forward)", leads)
    if ds.macro_alignment == "period" and any(v.source == "macro" for v in ds.variables):
        add("macro_period_alignment", "warning", "macro_alignment = period places each macro value on its reference period, "
            "before it was published (look-ahead of 2-8 weeks for most releases)", "use macro_alignment = pit (or auto)")
    stale = [v.name for v in ds.variables if v.source in ("ratios", "fundamentals") and v.max_stale_days is None and v.name in regs]
    if stale and ds.kind != "timeseries":
        add("stale_fundamentals", "info", f"{', '.join(stale)}: point-in-time fundamentals are carried forward until the next "
            "filing; without max_stale_days a dead company's last value persists", "set max_stale_days (e.g. 400)", stale)
    # ---- leakage
    y = var.get(study.y) if study.y else None
    if y is not None:
        dup = [c for c in study.x if c in var and _tr_key(var[c]) == _tr_key(y) and not y.uses_future and c != study.y]
        if dup:
            add("target_in_features", "error", f"{', '.join(dup)} is the target itself (same source, field and transforms)",
                "drop it, or lag it with {op: lag}", dup)
        k = max([t.k or 1 for t in y.pre_transforms + y.transforms if t.op in LEAD_OPS] or [0])
        cvp = getattr(params, "cv", None) if params is not None else None
        if cvp is None and params is not None:
            cvp = getattr(params, "crossfit", None)
        if cvp is not None and k > 1:
            purge = getattr(cvp, "purge", None)
            if purge is not None and purge < k:
                add("overlap_purge", "warning", f"the target spans {k} periods but the CV purge is {purge}: overlapping labels leak "
                    "between training and test folds", f"set cv.purge >= {k} (and embargo = {k} on panels)")
            if getattr(cvp, "scheme", None) == "kfold" and ds.kind != "cross_section":
                add("shuffled_cv", "warning", "shuffled K-fold on dated data trains on the future of the test rows",
                    "use scheme auto (expanding for time series, purged_kfold for panels)")
        elif cvp is not None and getattr(cvp, "scheme", None) == "kfold" and ds.kind != "cross_section":
            add("shuffled_cv", "warning", "shuffled K-fold on dated data trains on the future of the test rows",
                "use scheme auto (expanding for time series, purged_kfold for panels)")
        if k == 0 and m is not None and m.family in ("ml", "neural", "probabilistic") and study.x and ds.kind != "cross_section":
            same = [c for c in study.x if c in var and not any(t.op == "lag" for t in var[c].transforms)
                    and var[c].source in ("prices", "technicals", "tech_factors", "sentiment")]
            if same:
                add("contemporaneous_fit", "info", f"the target is not forward-looking and {', '.join(same)} are measured in "
                    "the same period: this is an in-period fit, not a forecast", "add a fwd_compound / lead transform to the target", same)
    glob = [v.name for v in ds.variables if v.name in regs and any(t.op in ("standardize", "winsorize") and t.scope == "all" for t in v.transforms)]
    if glob and ds.kind != "cross_section":
        add("global_normalization", "warning", f"{', '.join(glob)}: standardize / winsorize with scope all uses full-sample "
            "moments (including the future)", "use scope cross_section (panels) or a rolling_z transform", glob)
    # ---- survivorship
    ef = ds.entities
    if ds.kind != "timeseries" and (ef.tickers or ef.security_ids or ef.issuer_ids):
        add("survivorship_list", "warning", "a hand-picked list of entities is usually today's survivors (delisted names are "
            "missing)", "use a universe (point-in-time membership) instead of explicit tickers")
    if ds.kind != "timeseries" and not ds.pit_universe:
        add("survivorship_universe", "warning", "pit_universe = false keeps entities outside the periods they were members "
            "(survivorship / look-ahead in the universe)", "set pit_universe = true")
    unis = {ef.universe} if ds.kind != "timeseries" and ef.universe else set()
    unis |= {v.entities.universe for v in ds.variables if v.entities and v.entities.universe}
    for u in sorted(x for x in unis if x):
        first = _universe_start(u)
        if first is not None and (ds.start is None or ds.start < first):
            add("survivorship_history", "warning", f"universe {u!r} membership history starts {first.isoformat()}: earlier "
                "dates use today's constituents (delisted / dropped names are missing), so returns before then carry "
                "survivorship bias", f"start at {first.isoformat()}, or treat early-period premia as upper bounds")
    for v in ds.variables:
        if v.entities and (v.entities.tickers or v.entities.security_ids):
            add("survivorship_list", "info", f"{v.name}: aggregated over a fixed list of names (survivors)", "use a universe filter", [v.name])
    # ---- tiny sample
    try:
        from . import engine

        pp = engine.prior_parts(study)
        rows, n_ent = pp["rows"], pp["n_ent"]
    except Exception:  # noqa: BLE001
        rows, n_ent = None, None
    k_par = len(study.x) + 1
    if rows is not None:
        periods = rows if ds.kind == "timeseries" else max(1, rows // max(n_ent or 1, 1))
        if rows < 10 * k_par:
            add("tiny_sample", "error" if rows < 3 * k_par else "warning", f"about {rows} rows for {k_par} parameters (< 10 per parameter)",
                "lengthen the window, lower the frequency's granularity, or use fewer regressors")
        if ds.kind != "cross_section" and periods < 36 and ds.frequency in ("M", "Q"):
            add("few_periods", "warning", f"about {periods} periods: time-series inference (HAC, walk-forward CV) is fragile",
                "use a longer window or a higher frequency")
        if y is not None:
            k = max([t.k or 1 for t in y.pre_transforms + y.transforms if t.op in LEAD_OPS] or [0])
            if k > 1 and periods / k < 30:
                add("overlapping_horizon", "warning", f"a {k}-period target over about {periods} periods leaves ~{periods // k} "
                    "independent observations", "shorten the horizon or lengthen the window")
        if m is not None and study.method == "gp_regression" and rows > getattr(params, "max_n", 2000):
            add("gp_too_large", "error", f"about {rows} rows exceed the Gaussian-process cap ({getattr(params, 'max_n', 2000)})",
                "subset the universe / window, or use bayes_ridge / ngboost / quantile_gbm")
    if len(study.x) > 20 and m is not None and m.family in ("regression", "panel", "causal"):
        add("many_regressors", "info", f"{len(study.x)} regressors: expect ~{len(study.x) // 20} false positive(s) at 5 %",
            "read the BH-adjusted p-values, or use a regularized model")
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    return {"ok": not any(i["severity"] == "error" for i in issues), "issues": issues, "estimated_rows": rows,
            "n_entities": n_ent}
