"""Learned run-time estimates for Model Studio and backtest runs (docs/STUDIO_V2_PLAN.md S1.2). Owner: model-studio.

    from app.modeling import estimator
    est = estimator.estimate_studio(study, path="lane")    # {"total_s", "queue_s", "overhead_s", "build_s", "fit_s", ...}

* Prior: the static formulas (`engine.prior_parts`: dataset build + `Method.cost()`; `backtest.runs.prior_parts`) plus
  the heavy extras (SHAP, placebo, bootstrap, permutations, sweeps), with real universe sizes from the snapshot
  (`entity_count`). The prior is used as is until a method has run history.
* Learned (studio): log(fit_s) ~ log(prior fit + extras) + log(rows) + log(1 + n_x), ridge regression with per-method
  intercept / log-rows slope offsets shrunk toward the global fit (few runs per method). Dataset build:
  log(build_s) ~ log(prior build) for uncached datasets, the median for cached ones. Backtests: log(run_s) ~
  log(prior), intercept shrunk toward 0.
* total = queue (runs ahead in the lane) + overhead of the path (sync | lane | worker: spawn, imports, queue
  wait, from history) + build + fit + post (result writing).
* History: one sample per finished run, extracted once from data/{modeling,backtest}/runs/<id>/{status,spec,result}.json
  into data/modeling/estimator/samples_<kind>.json (incremental), refitted when it grows. Extras actually run are
  read from the result (a run before the opt-in change computed SHAP by default).
* Accuracy: `accuracy(kind)` = observed / estimated ratio over recent runs (diagnostics, the lane status endpoint).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.modeling.estimator")

MODEL_VERSION = 3
#: fallback universe sizes (primary listings, 2026-11) when the snapshot cannot be read
UNIVERSE_SIZE_FALLBACK = {"mvp_us": 500, "mvp_global": 271, "full": 26160, "eu_broad": 1708, "watch": 50}
#: per-path overhead priors (seconds) before history exists: inline API call, warm studio lane, worker spawn + imports
OVERHEAD_PRIOR = {"sync": 0.05, "lane": 0.35, "worker": 4.8}
POST_PRIOR_S = 0.08
PREVIEW_RATIO_PRIOR = 0.3   # preview fit time / full fit time (10-20 % sample, extras off) before history exists
LAMBDA_METHOD = 0.5      # ridge penalty of per-method intercepts (pseudo-observations)
LAMBDA_SLOPE = 6.0       # ridge penalty of per-method log-rows slopes
LAMBDA_GLOBAL = 0.5      # light penalty on the global slopes (toward the prior: slope 1 on log prior, 0 otherwise)

_lock = threading.Lock()
_models: dict[str, dict[str, Any]] = {}
_entity_cache: dict[str, Any] = {}


# ------------------------------------------------------------------------------------------------ helpers


def est_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.data_dir / "modeling" / "estimator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(p: Path, obj: Any) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:  # pragma: no cover - the estimator never blocks a run
        pass


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _lg(v: float) -> float:
    return math.log(max(float(v), 0.0) + 0.02)


# ------------------------------------------------------------------------------------------------ universe sizes


def _entity_index(settings: Settings | None = None) -> dict[str, Any] | None:
    """Per snapshot: primary securities (sector, region, country) + current universe members. None without a lake."""
    s = settings or get_settings()
    try:
        from .datasets import lake_connection, snapshot_id

        snap = snapshot_id(s)
    except Exception:  # noqa: BLE001
        return None
    key = f"{s.data_dir}|{snap}"
    hit = _entity_cache.get(key)
    if hit is not None:
        return hit or None
    idx: dict[str, Any] = {}
    try:
        con = lake_connection(["securities_master", "issuers", "universe_membership"], s)
        try:
            rows = con.execute(
                "SELECT s.security_id, i.sector_id, CAST(i.region AS VARCHAR), i.country, "
                "COALESCE(s.is_primary_security, TRUE) FROM securities_master s LEFT JOIN issuers i USING (issuer_id)"
            ).fetchall()
            mem = con.execute("SELECT universe_id, security_id FROM universe_membership WHERE valid_to IS NULL").fetchall()
        finally:
            con.close()
        sec = {r[0]: (r[1], r[2], r[3], bool(r[4])) for r in rows}
        unis: dict[str, set[str]] = {}
        for u, sid in mem:
            unis.setdefault(str(u), set()).add(sid)
        idx = {"sec": sec, "uni": unis}
    except Exception as e:  # noqa: BLE001 - no lake (tests) / locked: fall back to the static sizes
        log.debug("entity index unavailable: %s", e)
        idx = {}
    if len(_entity_cache) > 4:
        _entity_cache.clear()
    _entity_cache[key] = idx
    return idx or None


def entity_count(flt: Any, max_entities: int = 3000, settings: Settings | None = None) -> int:
    """Securities matched by an entity filter (studio EntityFilter / backtest UniverseSpec / dict), capped like the
    dataset builder (max_entities). Real counts from the snapshot; static fallback sizes without one."""
    g = (lambda k: flt.get(k)) if isinstance(flt, dict) else (lambda k: getattr(flt, k, None))
    n_explicit = sum(len(g(k) or []) for k in ("security_ids", "issuer_ids", "tickers"))
    if n_explicit:
        return n_explicit
    universe = g("universe") or g("universe_id")
    sectors, regions, countries = (set(g(k) or []) for k in ("sectors", "regions", "countries"))
    countries = {c.upper() for c in countries}
    if not universe and not (sectors or regions or countries):
        universe = "full"
    idx = _entity_index(settings)
    if idx is None:
        n = UNIVERSE_SIZE_FALLBACK.get(universe or "", 0) or 3000
        if sectors or regions or countries:
            n = max(20, int(n * (0.15 if universe else 0.03) * (max(len(sectors), 1) if sectors else 1)))
        return int(min(n, max_entities))
    sec = idx["sec"]
    ids = idx["uni"].get(universe) if universe else None
    pool = ids if ids is not None else (set() if universe else sec.keys())
    n = 0
    for sid in pool:
        meta = sec.get(sid)
        if meta is None:
            continue
        sector, region, country, primary = meta
        if sectors and sector not in sectors:
            continue
        if regions and region not in regions:
            continue
        if countries and (country or "").upper() not in countries:
            continue
        if not primary and g("primary_only") is not False:
            continue
        n += 1
    if universe and ids is None:
        n = UNIVERSE_SIZE_FALLBACK.get(universe, 3000)
    return int(min(max(n, 1), max_entities))


# ------------------------------------------------------------------------------------------------ extras


def extras(method: str, params: Any, rows: int, n_x: int) -> list[dict[str, Any]]:
    """Heavy optional outputs of a method with their prior cost (s) and whether they are on. Accepts any method:
    unknown ones have none. `params` is the parsed params model (or a dict)."""
    g = (lambda k, d=None: params.get(k, d)) if isinstance(params, dict) else (lambda k, d=None: getattr(params, k, d))
    out: list[dict[str, Any]] = []
    if g("shap") is not None:
        n = min(int(g("shap_sample", 2000) or 2000), max(rows, 1))
        trees = int(g("n_estimators", 300) or 300)
        out.append({"name": "shap", "label": "SHAP values", "enabled": bool(g("shap")),
                    "cost_s": round(0.4 + n * max(n_x, 1) * trees * 6e-8, 2)})
    if g("placebo") is not None:
        d = int(g("max_donors", 60) or 60)
        out.append({"name": "placebo", "label": "Placebo inference", "enabled": bool(g("placebo")),
                    "cost_s": round(d * d * 0.004 + 0.2, 2)})
    if method == "did" and g("design") == "staggered" and g("bootstrap") is not None:
        b = int(g("bootstrap") or 0)
        out.append({"name": "bootstrap", "label": f"Bootstrap (B={b})", "enabled": b > 0,
                    "cost_s": round(rows * 4e-6 * b / 50, 2)})
    return out


def _extras_from_result(res: dict[str, Any] | None) -> set[str]:
    """Which heavy extras a finished run actually computed (history: defaults changed over time)."""
    if not res:
        return set()
    names = {t.get("name") for t in (res.get("tables") or []) if isinstance(t, dict)}
    plots = set((res.get("plots") or {}).keys()) if isinstance(res.get("plots"), dict) else set()
    tests = " ".join(str(t.get("name", "")) for t in (res.get("tests") or []) if isinstance(t, dict)).lower()
    out = set()
    if "shap" in names or "shap_beeswarm" in plots:
        out.add("shap")
    if "placebo" in tests:
        out.add("placebo")
    if ((res.get("summary") or {}).get("bootstrap")):
        out.add("bootstrap")
    return out


# ------------------------------------------------------------------------------------------------ history samples


def _path_of(st: dict[str, Any]) -> str:
    if st.get("lane") in ("lane", "studio"):
        return "lane"
    return "sync" if st.get("mode") == "sync" else "worker"


def _studio_sample(d: Path, st: dict[str, Any], s: Settings) -> dict[str, Any] | None:
    from . import engine
    from .registry import StudySpec, get_method

    doc, env = _read_json(d / "spec.json"), _read_json(d / "result.json")
    if not doc or not env:
        return None
    try:
        study = StudySpec.model_validate(doc["study"])
        cached = bool((env.get("dataset") or {}).get("cached"))
        pp = engine.prior_parts(study, s, cached=cached)
        m = get_method(study.method)
        params = m.parse_params(study.params)
        ran = _extras_from_result(env.get("result"))
        ex = sum(e["cost_s"] for e in extras(study.method, params, pp["rows"], len(study.x)) if e["name"] in ran)
    except Exception:  # noqa: BLE001 - spec of an older contract / unknown method
        return None
    t = (env.get("reproducibility") or {}).get("timings") or {}
    c, s0, f = _ts(st.get("created_at")), _ts(st.get("started_at")), _ts(st.get("finished_at"))
    run_s = float(st.get("duration_s") or 0)
    if not (c and s0 and f) or run_s <= 0 or t.get("fit_s") is None:
        return None
    return {"run_id": st["run_id"], "created_at": st["created_at"], "method": study.method, "path": _path_of(st),
            "spec_hash": st.get("spec_hash"), "rows": pp["rows"], "n_x": len(study.x), "prior_fit": pp["fit_s"], "prior_build": pp["build_s"],
            "extras_s": ex, "cached": cached, "build_s": float(t.get("dataset_s") or 0), "fit_s": float(t["fit_s"]),
            "preview_s": float(t.get("preview_s") or 0), "run_s": run_s, "wait_s": max((s0 - c).total_seconds(), 0.0),
            "e2e_s": max((f - c).total_seconds(), run_s), "estimate_s": st.get("estimate_s")}


def _backtest_sample(d: Path, st: dict[str, Any], s: Settings) -> dict[str, Any] | None:
    from app.backtest import runs
    from app.backtest.spec import BacktestSpec

    doc, env = _read_json(d / "spec.json"), _read_json(d / "result.json")
    if not doc or not env:
        return None
    res = env.get("result") or {}
    try:
        spec = BacktestSpec.model_validate(doc["spec"])
        cached = bool((res.get("panel") or {}).get("cached"))
        pp = runs.prior_parts(spec, s, cached=cached)
    except Exception:  # noqa: BLE001
        return None
    c, s0, f = _ts(st.get("created_at")), _ts(st.get("started_at")), _ts(st.get("finished_at"))
    run_s = float(st.get("duration_s") or 0)
    if not (c and s0 and f) or run_s <= 0:
        return None
    t = res.get("timings") or {}
    return {"run_id": st["run_id"], "created_at": st["created_at"], "method": spec.mode, "path": _path_of(st),
            "prior": pp["total_s"], "prior_panel": pp["panel_s"], "cached": cached, "run_s": run_s,
            "build_s": float(t.get("panel_s") or 0), "wait_s": max((s0 - c).total_seconds(), 0.0),
            "e2e_s": max((f - c).total_seconds(), run_s), "estimate_s": st.get("estimate_s")}


def samples(kind: str, settings: Settings | None = None, refresh: bool = True) -> list[dict[str, Any]]:
    """Training samples of finished (completed) runs, oldest first. kind = studio | backtest."""
    s = settings or get_settings()
    root = s.data_dir / ("modeling" if kind == "studio" else "backtest") / "runs"
    cache_p = est_dir(s) / f"samples_{kind}.json"
    cache = _read_json(cache_p) or {}
    if cache.get("v") != MODEL_VERSION:
        cache = {"v": MODEL_VERSION, "rows": {}, "skip": []}
    rows: dict[str, Any] = cache["rows"]
    skip = set(cache.get("skip") or [])
    changed = False
    if refresh and root.exists():
        for d in root.iterdir():
            rid = d.name
            if rid in rows or rid in skip:
                continue
            st = _read_json(d / "status.json")
            if not st or st.get("status") not in ("completed", "failed", "cancelled"):
                continue  # in flight: look again later
            smp = None
            if st.get("status") == "completed":
                smp = (_studio_sample if kind == "studio" else _backtest_sample)(d, st, s)
            if smp is None:
                skip.add(rid)
            else:
                rows[rid] = smp
            changed = True
    if changed:
        cache["skip"] = sorted(skip)
        _write_json(cache_p, cache)
    return sorted(rows.values(), key=lambda r: r["created_at"])


# ------------------------------------------------------------------------------------------------ fitting


def _ridge(X: np.ndarray, y: np.ndarray, pen: np.ndarray, target: np.ndarray) -> np.ndarray:
    """min |y - Xb|^2 + sum pen_j (b_j - target_j)^2."""
    A = X.T @ X + np.diag(pen)
    b = X.T @ y + pen * target
    return np.linalg.solve(A, b)


def _median(v: list[float], default: float) -> float:
    v = [x for x in v if x is not None and math.isfinite(x)]
    return float(np.median(v)) if v else default


def fit_studio(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Learned studio model from samples (pure; see module docstring)."""
    model: dict[str, Any] = {"kind": "studio", "n": len(rows), "methods": {}, "global": None,
                             "overhead": dict(OVERHEAD_PRIOR), "post_s": POST_PRIOR_S, "cached_build_s": 0.06,
                             "build": None, "resid_sd": None}
    if not rows:
        return model
    for path in ("sync", "lane", "worker"):
        w = [r["e2e_s"] - r["run_s"] for r in rows if r["path"] == path]
        if len(w) >= 3:
            model["overhead"][path] = round(_median(w, OVERHEAD_PRIOR[path]), 3)
    model["post_s"] = round(_median([r["run_s"] - r["build_s"] - r["fit_s"] - r.get("preview_s", 0) for r in rows],
                                    POST_PRIOR_S), 3)
    pv = [r["preview_s"] / r["fit_s"] for r in rows if r.get("preview_s") and r["fit_s"] > 0]
    model["preview_ratio"] = round(_median(pv, PREVIEW_RATIO_PRIOR), 3) if len(pv) >= 3 else PREVIEW_RATIO_PRIOR
    model["cached_build_s"] = round(_median([r["build_s"] for r in rows if r["cached"]], 0.06), 3)
    unc = [r for r in rows if not r["cached"]]
    if len(unc) >= 4:
        X = np.array([[1.0, _lg(r["prior_build"])] for r in unc])
        y = np.array([_lg(r["build_s"]) for r in unc])
        b = _ridge(X, y, np.array([0.0, 2.0]), np.array([0.0, 1.0]))
        model["build"] = [float(b[0]), float(b[1])]
    methods = sorted({r["method"] for r in rows})
    mi = {m: i for i, m in enumerate(methods)}
    k, M = 4, len(methods)
    X = np.zeros((len(rows), k + 2 * M))
    y = np.zeros(len(rows))
    for i, r in enumerate(rows):
        lr = math.log1p(r["rows"])
        X[i, :k] = [1.0, _lg(r["prior_fit"] + r["extras_s"]), lr, math.log1p(r["n_x"])]
        j = mi[r["method"]]
        X[i, k + j] = 1.0
        X[i, k + M + j] = lr - 6.0  # centred near ~400 rows so the slope offset does not fight the intercept
        y[i] = _lg(r["fit_s"])
    pen = np.r_[0.0, LAMBDA_GLOBAL, LAMBDA_GLOBAL, LAMBDA_GLOBAL, [LAMBDA_METHOD] * M, [LAMBDA_SLOPE] * M]
    tgt = np.r_[0.0, 1.0, 0.0, 0.0, [0.0] * (2 * M)]
    b = _ridge(X, y, pen, tgt)
    model["global"] = [float(v) for v in b[:k]]
    counts = {m: 0 for m in methods}
    for r in rows:
        counts[r["method"]] += 1
    model["methods"] = {m: {"a": float(b[k + mi[m]]), "s": float(b[k + M + mi[m]]), "n": counts[m]} for m in methods}
    specs: dict[str, list[float]] = {}
    for r in rows:  # same spec seen before: its observed fit times (last 3) dominate the prediction
        if r.get("spec_hash"):
            specs.setdefault(r["spec_hash"], []).append(round(_lg(r["fit_s"]), 4))
    model["specs"] = {h: v[-3:] for h, v in specs.items()}
    resid = y - X @ b
    model["resid_sd"] = float(np.std(resid)) if len(rows) > 5 else None
    return model


def fit_backtest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    model: dict[str, Any] = {"kind": "backtest", "n": len(rows), "a": 0.0, "overhead": dict(OVERHEAD_PRIOR)}
    if rows:
        y = np.array([_lg(r["run_s"]) - _lg(r["prior"]) for r in rows])
        model["a"] = float(y.sum() / (len(y) + 2.0))  # shrunk toward 0 (prior unbiased) with 2 pseudo-runs
        for path in ("sync", "lane", "worker"):
            w = [r["e2e_s"] - r["run_s"] for r in rows if r["path"] == path]
            if len(w) >= 3:
                model["overhead"][path] = round(_median(w, OVERHEAD_PRIOR[path]), 3)
    return model


def model(kind: str, settings: Settings | None = None) -> dict[str, Any]:
    """Cached learned model; refitted when the history grows (checked at most every 30 s)."""
    s = settings or get_settings()
    key = f"{kind}|{s.data_dir}"
    with _lock:
        cur = _models.get(key)
        if cur and time.monotonic() - cur["_checked"] < 30:
            return cur
        try:
            rows = samples(kind, s)
        except Exception as e:  # noqa: BLE001
            log.warning("estimator history (%s) unreadable: %s", kind, e)
            rows = []
        if cur and cur.get("n") == len(rows):
            cur["_checked"] = time.monotonic()
            return cur
        m = fit_studio(rows) if kind == "studio" else fit_backtest(rows)
        m["_checked"] = time.monotonic()
        m["fitted_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _models[key] = m
        _write_json(est_dir(s) / f"model_{kind}.json", {k: v for k, v in m.items() if not k.startswith("_")})
        return m


def reset_cache() -> None:
    with _lock:
        _models.clear()
        _entity_cache.clear()


# ------------------------------------------------------------------------------------------------ prediction


def predict_fit(m: dict[str, Any], method: str, rows: int, n_x: int, prior_fit: float, extras_s: float,
                spec_hash: str | None = None) -> float:
    if not m.get("global"):
        return prior_fit + extras_s
    g = m["global"]
    lr = math.log1p(rows)
    z = g[0] + g[1] * _lg(prior_fit + extras_s) + g[2] * lr + g[3] * math.log1p(n_x)
    mm = (m.get("methods") or {}).get(method)
    if mm:
        z += mm["a"] + mm["s"] * (lr - 6.0)
    else:  # a method without history (e.g. a new S2 method): trust its own cost() more
        z = 0.5 * z + 0.5 * _lg(prior_fit + extras_s)
    seen = (m.get("specs") or {}).get(spec_hash or "")
    if seen:
        w = len(seen) / (len(seen) + 0.5)
        z = w * float(np.mean(seen)) + (1 - w) * z
    return max(math.exp(z) - 0.02, 0.01)


def predict_build(m: dict[str, Any], prior_build: float, cached: bool) -> float:
    if cached:
        return float(m.get("cached_build_s") or 0.06)
    b = m.get("build")
    if not b:
        return prior_build
    return max(math.exp(b[0] + b[1] * _lg(prior_build)) - 0.02, 0.02)


def estimate_studio(study: Any, settings: Settings | None = None, path: str = "lane", queue_s: float = 0.0,
                    params: Any = None) -> dict[str, Any]:
    """Estimate of a studio run: total = queue + overhead + build + fit (+ post). `path` sync | lane | worker."""
    from . import engine
    from .registry import get_method

    s = settings or get_settings()
    pp = engine.prior_parts(study, s)
    m = model("studio", s)
    try:
        params = params if params is not None else get_method(study.method).parse_params(study.params)
        ex = extras(study.method, params, pp["rows"], len(study.x))
    except Exception:  # noqa: BLE001
        ex = []
    ex_on = sum(e["cost_s"] for e in ex if e["enabled"])
    try:
        h = study.spec_hash()
    except Exception:  # noqa: BLE001
        h = None
    fit = predict_fit(m, study.method, pp["rows"], len(study.x), pp["fit_s"], ex_on, h)
    build = predict_build(m, pp["build_s"], pp["cached"])
    over = float((m.get("overhead") or OVERHEAD_PRIOR).get(path, OVERHEAD_PRIOR.get(path, 0.3)))
    post = float(m.get("post_s") or POST_PRIOR_S)
    from .engine import PREVIEW_MIN_FIT_S

    preview = fit * float(m.get("preview_ratio") or PREVIEW_RATIO_PRIOR)         if path != "sync" and fit >= PREVIEW_MIN_FIT_S and pp["rows"] >= 200 else 0.0
    run_s = build + preview + fit + post
    mm = (m.get("methods") or {}).get(study.method) or {}
    # scale the extras' prior cost by the method's learned speed so the UI shows a coherent breakdown
    scale = fit / max(pp["fit_s"] + ex_on, 1e-3) if m.get("global") else 1.0
    for e in ex:
        e["cost_s"] = round(e["cost_s"] * min(max(scale, 0.2), 5.0), 2)
    return {"total_s": round(queue_s + over + run_s, 2), "run_s": round(run_s, 2), "queue_s": round(queue_s, 2),
            "overhead_s": round(over, 2), "build_s": round(build, 2), "fit_s": round(fit, 2), "post_s": round(post, 2),
            "preview_s": round(preview, 2),
            "prior_s": round(pp["build_s"] + pp["fit_s"] + ex_on, 2), "rows": pp["rows"], "cached": pp["cached"],
            "path": path, "model": "learned" if mm else ("global" if m.get("global") else "prior"),
            "n_history": int(mm.get("n") or 0), "extras": ex}


def estimate_backtest(spec: Any, settings: Settings | None = None, path: str = "lane", queue_s: float = 0.0) -> dict[str, Any]:
    from app.backtest import runs

    s = settings or get_settings()
    pp = runs.prior_parts(spec, s)
    m = model("backtest", s)
    run_s = max(math.exp(_lg(pp["total_s"]) + float(m.get("a") or 0.0)) - 0.02, 0.05)
    over = float((m.get("overhead") or OVERHEAD_PRIOR).get(path, OVERHEAD_PRIOR.get(path, 0.3)))
    ratio = run_s / max(pp["total_s"], 1e-3)
    ex = [{**e, "cost_s": round(e["cost_s"] * ratio, 2)} for e in pp["extras"]]
    return {"total_s": round(queue_s + over + run_s, 2), "run_s": round(run_s, 2), "queue_s": round(queue_s, 2),
            "overhead_s": round(over, 2), "build_s": round(pp["panel_s"] * ratio, 2),
            "fit_s": round((pp["total_s"] - pp["panel_s"]) * ratio, 2), "post_s": 0.0, "prior_s": pp["total_s"],
            "rows": pp["n"], "cached": pp["cached"], "path": path,
            "model": "learned" if m.get("n") else "prior", "n_history": int(m.get("n") or 0), "extras": ex}


# ------------------------------------------------------------------------------------------------ diagnostics


def accuracy(kind: str, settings: Settings | None = None, last: int = 50) -> dict[str, Any]:
    """Observed / estimated end-to-end ratio of recent finished runs (estimate as shown at submit time)."""
    rows = [r for r in samples(kind, settings) if r.get("estimate_s")][-last:]
    ratios = [r["e2e_s"] / float(r["estimate_s"]) for r in rows if float(r["estimate_s"]) > 0]
    if not ratios:
        return {"n": 0, "median_ratio": None, "within_30pct": None, "median_abs_err": None}
    return {"n": len(ratios), "median_ratio": round(float(np.median(ratios)), 3),
            "within_30pct": round(sum(0.7 <= x <= 1.3 for x in ratios) / len(ratios), 3),
            "median_abs_err": round(float(np.median([abs(x - 1) for x in ratios])), 3)}


__all__ = ["accuracy", "entity_count", "estimate_backtest", "estimate_studio", "extras", "fit_backtest", "fit_studio",
           "model", "predict_build", "predict_fit", "reset_cache", "samples"]
