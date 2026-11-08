"""Export a Model Studio run as a standalone Python script (owner: model-studio).

The script embeds (verbatim) the dataset engine `app/modeling/_core.py` and the PIT helper
`app.normalize.pit.asof_join`, so it rebuilds exactly the same dataset from the Parquet lake without the backend
installed, then refits the model with plain statsmodels / scikit-learn calls (the orders / k chosen by the run's
search are fixed in the script). It prints a summary and a final `RESULT_JSON {...}` line (key estimates) so the
reproduction can be checked mechanically.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.schema import get_contract
from app.normalize import pit

from . import _advml, _algos, _causal2, _conformal, _core, _ml, _nn, _prob, _spatial
from ._export_b import PHASE_B, phase_b_code
from ._export_c import PHASE_C, phase_c_code
from ._export_cf import PHASE_CF, phase_cf_code
from ._export_s import PHASE_S, phase_s_code
from ._export_v2 import PHASE_V2, phase_v2_code
from ._export_s3 import PHASE_S3, phase_s3_code
from .registry import StudySpec

HEADER = '''#!/usr/bin/env python
"""BiG Equity 360 - Model Studio reproducible script.

Run:      {run_id}   ({title})
Method:   {method}
Created:  {created}  (snapshot {snapshot}, spec hash {spec_hash}, seed {seed})

Rebuilds the dataset read-only from the Parquet lake under --data-dir (default: the data dir of the run) with the
embedded Model Studio dataset engine, then fits the model with statsmodels / scikit-learn.
`--from-artifact` loads the exact dataset saved with the run (dataset.parquet) instead of rebuilding it.

Requires: duckdb polars numpy pandas scipy statsmodels scikit-learn
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

STUDY = json.loads(r"""{study_json}""")
DATASET_SPEC = STUDY["dataset"]
LAKE_TABLES = json.loads(r"""{tables_json}""")
DEFAULT_DATA_DIR = r"{data_dir}"
RUN_DIR = r"{run_dir}"
'''

LOADER = '''

# ============================================================================== lake access


def lake_connection(data_dir: str):
    import duckdb

    con = duckdb.connect(":memory:")
    root = Path(data_dir)
    for table, t in LAKE_TABLES.items():
        d = root / t["path"]
        files = list(d.rglob("*.parquet"))
        if not files:
            raise SystemExit(f"no Parquet files for {table} under {d}")
        glob = (d.as_posix() + ("/**/*.parquet" if t["hive_types"] else "/*.parquet")).replace("'", "''")
        opts = ["union_by_name = true"]
        if t["hive_types"]:
            types = ", ".join(f"'{k}': {v}" for k, v in t["hive_types"].items())
            opts += ["hive_partitioning = true", "hive_types = {" + types + "}"]
        src = f'read_parquet(\\'{glob}\\', {", ".join(opts)})'
        sel = t.get("select")  # narrowed float32 / derived columns (app.core.storage.logical_select_sql)
        body = sel.replace("__SRC__", src) if sel else f"SELECT * FROM {src}"
        con.execute(f'CREATE OR REPLACE VIEW "{table}" AS {body}')
    return con


def load_dataset(data_dir: str, from_artifact: bool = False):
    import polars as pl

    if from_artifact:
        return pl.read_parquet(Path(RUN_DIR) / "dataset.parquet")
    con = lake_connection(data_dir)
    frame, meta = build(DATASET_SPEC, con)
    for w in meta["warnings"]:
        print("dataset warning:", w)
    print(f"dataset: {meta['n_rows']} rows, {meta['start']} .. {meta['end']} ({meta['kind']}, {meta['frequency']})")
    return frame


def complete(frame, cols):
    """Listwise deletion on the used columns (null / NaN / inf), as pandas."""
    import numpy as np

    keep = ["date"] + (["entity_id", "entity_label"] if "entity_id" in frame.columns else [])
    df = frame.select(list(dict.fromkeys(keep + cols))).to_pandas()
    for c in cols:
        df[c] = df[c].astype(float).replace([np.inf, -np.inf], np.nan)
    return df.dropna(subset=cols).reset_index(drop=True)


def emit(obj):
    print("RESULT_JSON " + json.dumps(obj, default=float))
'''

MAIN = '''

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--from-artifact", action="store_true")
    a = ap.parse_args()
    fit(load_dataset(a.data_dir, a.from_artifact))
'''


def _strip_future(src: str) -> str:
    return re.sub(r"^from __future__ import annotations\n", "", src, flags=re.M)


def _embedded_engine() -> str:
    pit_src = "\n\n# ============================================================================== PIT helper " \
              "(app.normalize.pit, verbatim)\nfrom collections.abc import Sequence\nimport polars as pl\n\n\n" + \
              inspect.getsource(pit._as_lazy) + "\n\n" + inspect.getsource(pit.asof_join)
    core_src = Path(_core.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    core_src = _strip_future(core_src)
    algos_src = _strip_future(Path(_algos.__file__).read_text(encoding="utf-8").replace("\r\n", "\n"))
    sep = "\n\n# " + "=" * 78
    return (pit_src + sep + " dataset engine (app/modeling/_core.py, verbatim)\n" + core_src
            + sep + " estimation core (app/modeling/_algos.py, verbatim)\n" + algos_src
            + sep + " ML core (app/modeling/_ml.py, verbatim)\n"
            + _strip_future(Path(_ml.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")))


def _tables(spec: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for t in _core.tables_needed(spec):
        c = get_contract(t)
        hive = {p: c.column(p).duckdb for p in c.partition_by} if c.partition_by else None
        out[t] = {"path": c.path, "hive_types": hive}
        if c.narrowed_columns or c.derived_columns:
            from app.core.storage import logical_select_sql, physical_columns  # noqa: PLC0415

            out[t]["select"] = logical_select_sql(c, physical_columns(c), "__SRC__")
    return out


# ------------------------------------------------------------------------------------------------ method code


def _lin_cov(p: dict[str, Any], panel: bool, n_expr: str = "len(df)") -> str:
    ct = p.get("cov_type", "HC1")
    if ct == "HAC" and not panel:
        lags = p.get("hac_maxlags")
        lag_expr = str(lags) if lags is not None else f"int(np.floor(4 * ({n_expr} / 100.0) ** (2.0 / 9.0)))"
        return f'dict(cov_type="HAC", cov_kwds={{"maxlags": {lag_expr}, "use_correction": True}})'
    if ct == "HAC" or ct == "cluster":
        by = "entity" if ct == "HAC" else p.get("cluster_by", "entity")
        if by == "both":
            g = 'np.column_stack([pd.factorize(df["entity_id"])[0], pd.factorize(df["date"])[0]])'
        else:
            g = f'pd.factorize(df["{"entity_id" if by == "entity" else "date"}"])[0]'
        return f'dict(cov_type="cluster", cov_kwds={{"groups": {g}}})'
    return f'dict(cov_type="{ct}")'


def _method_code(study: StudySpec, env: dict[str, Any]) -> str:
    m, p = study.method, dict(study.params)
    y, x, w = study.y, list(study.x), study.weights
    res = (env.get("result") or {})
    summ = res.get("summary") or {}
    panel = study.dataset.kind != "timeseries"
    lines = ["", "", "# ============================================================================== model", "",
             "def fit(frame):", "    import numpy as np", "    import pandas as pd"]
    add = lines.append
    if m in ("ols", "wls", "gls", "distributed_lag"):
        add("    import statsmodels.api as sm")
        regs = list(x)
        if m == "distributed_lag":
            L = int(p.get("lags", 3))
            over = p.get("lag_overrides", {}) or {}
            c0 = 0 if p.get("include_contemporaneous", True) else 1
            lagmap = {v: list(range(c0, int(over.get(v, L)) + 1)) for v in x}
            if int(p.get("y_lags", 0)):
                lagmap[y] = list(range(1, int(p["y_lags"]) + 1))
            add(f"    lag_map = {json.dumps(lagmap)}")
            add('    import polars as pl')
            add('    over = "entity_id" if "entity_id" in frame.columns else None')
            add("    exprs, regs = [], []")
            add("    for v, lags in lag_map.items():")
            add("        for L in lags:")
            add("            nm = v if L == 0 else f\"{v}_L{L}\"")
            add("            if L:")
            add("                e = pl.col(v).shift(L)")
            add("                exprs.append((e.over(over) if over else e).alias(nm))")
            add("            regs.append(nm)")
            add("    frame = frame.with_columns(exprs) if exprs else frame")
        else:
            add(f"    regs = {json.dumps(regs)}")
        cols = f'[{json.dumps(y)}] + regs' + (f' + [{json.dumps(w)}]' if w else "")
        add(f"    df = complete(frame, list(dict.fromkeys({cols})))")
        add(f"    X = df[regs]")
        if p.get("add_constant", True):
            add("    X = sm.add_constant(X, has_constant='add')")
        add(f"    yv = df[{json.dumps(y)}]")
        cov = _lin_cov(p, panel)
        if m == "gls":
            add(f"    model = sm.GLSAR(yv, X, rho={int(p.get('ar_order', 1))})")
            add(f"    r = model.iterative_fit(maxiter={int(p.get('max_iter', 20))})")
            if p.get("cov_type", "HC1") != "nonrobust":
                add(f"    r = model.fit(**{cov})")
        elif m == "wls":
            add(f"    r = sm.WLS(yv, X, weights=df[{json.dumps(w)}]).fit(**{cov})")
        else:
            add(f"    r = sm.OLS(yv, X).fit(**{cov})")
        add("    print(r.summary())")
        if m == "distributed_lag":
            add("    for v, lags in lag_map.items():")
            add("        names = [v if L == 0 else f\"{v}_L{L}\" for L in lags]")
            add("        R = np.zeros((len(names), len(r.params)))")
            add("        for i, nm in enumerate(names):")
            add("            R[i, list(r.params.index).index(nm)] = 1.0")
            add("        print(v, 'joint lags = 0:', r.wald_test(R, scalar=True, use_f=True))")
        add("    emit({'params': r.params.to_dict(), 'bse': r.bse.to_dict(), 'n_obs': int(r.nobs)})")
    elif m == "quantile":
        add("    import statsmodels.api as sm")
        add(f"    df = complete(frame, {json.dumps([y] + x)})")
        add(f"    X = df[{json.dumps(x)}]")
        if p.get("add_constant", True):
            add("    X = sm.add_constant(X, has_constant='add')")
        add("    out = {}")
        add(f"    for q in {json.dumps(sorted(p.get('quantiles', [0.1, 0.25, 0.5, 0.75, 0.9])))}:")
        add(f"        r = sm.QuantReg(df[{json.dumps(y)}], X).fit(q=q, max_iter={int(p.get('max_iter', 2000))})")
        add("        print(f'--- quantile {q}'); print(r.summary())")
        add("        out[str(q)] = r.params.to_dict()")
        add("    emit({'params_by_quantile': out, 'n_obs': len(df)})")
    elif m == "glm":
        fam = p.get("family", "gaussian")
        add("    import statsmodels.api as sm")
        add("    from statsmodels.genmod.families import links as L")
        off = p.get("offset")
        cols = [y] + x + ([w] if w else []) + ([off] if off else [])
        add(f"    df = complete(frame, {json.dumps(list(dict.fromkeys(cols)))})")
        add(f"    X = df[{json.dumps(x)}]")
        if p.get("add_constant", True):
            add("    X = sm.add_constant(X, has_constant='add')")
        add(f"    yv = df[{json.dumps(y)}]")
        add(f"    offset = df[{json.dumps(off)}] if {bool(off)} else None")
        link = p.get("link", "default")
        link_expr = {"identity": "L.Identity()", "log": "L.Log()", "logit": "L.Logit()", "probit": "L.Probit()",
                     "cloglog": "L.CLogLog()", "inverse_power": "L.InversePower()"}.get(link)
        lk = f"link={link_expr}" if link_expr else ""
        if fam == "negative_binomial":
            if p.get("nb_alpha"):
                add(f"    alpha = {float(p['nb_alpha'])}")
            else:
                add("    alpha = float(sm.NegativeBinomial(yv, X, offset=offset).fit(disp=0).params.iloc[-1])")
            add(f"    family = sm.families.NegativeBinomial({lk + ', ' if lk else ''}alpha=alpha)")
        else:
            add(f"    family = sm.families.{ {'gaussian': 'Gaussian', 'binomial': 'Binomial', 'poisson': 'Poisson', 'gamma': 'Gamma'}[fam]}({lk})")
        wexpr = f"df[{json.dumps(w)}]" if w else "None"
        add(f"    r = sm.GLM(yv, X, family=family, offset=offset, freq_weights={wexpr}).fit(**{_lin_cov(p, panel)}, maxiter={int(p.get('max_iter', 100))})")
        add("    print(r.summary())")
        if fam == "binomial":
            add("    from sklearn.metrics import roc_auc_score")
            add("    print('AUC', roc_auc_score((yv > 0.5).astype(int), r.fittedvalues))")
        add("    emit({'params': r.params.to_dict(), 'bse': r.bse.to_dict(), 'n_obs': int(r.nobs)})")
    elif m == "arima":
        order = summ.get("order") or p.get("order", [1, 0, 0])
        seas = summ.get("seasonal_order") or p.get("seasonal_order", [0, 0, 0, 0])
        trend = summ.get("trend") or p.get("trend") or "c"
        add("    from statsmodels.tsa.statespace.sarimax import SARIMAX")
        add(f"    full = frame.select(['date', {json.dumps(y)}] + {json.dumps(x)}).to_pandas()")
        add(f"    ok = full[{json.dumps(y)}].notna()")
        add("    first, last = ok.values.argmax(), len(ok) - 1 - ok.values[::-1].argmax()")
        add("    body = full.iloc[first:last + 1].dropna()")
        add(f"    yv = body[{json.dumps(y)}].to_numpy(float)")
        add(f"    ex = body[{json.dumps(x)}].to_numpy(float) if {bool(x)} else None")
        add(f"    # order selected by the run{' (search: ' + str(p.get('criterion', 'aic')) + ')' if p.get('auto') else ''}")
        add(f"    m = SARIMAX(yv, exog=ex, order={tuple(order)}, seasonal_order={tuple(seas)}, trend={json.dumps(trend)},")
        add("                enforce_stationarity=True, enforce_invertibility=True)")
        add(f"    r = m.fit(disp=False, maxiter={int(p.get('max_iter', 200))})")
        add("    print(r.summary())")
        h = int(p.get("forecast_steps", 12))
        if h:
            if x:
                add(f"    fx = np.repeat(ex[-1:], {h}, axis=0)  # future regressors: last value (see the run for other modes)")
            add(f"    fc = r.get_forecast(steps={h}{', exog=fx' if x else ''})")
            add(f"    print(fc.summary_frame(alpha={float(p.get('alpha', 0.05))}))")
        add("    emit({'params': dict(zip(m.param_names, r.params.tolist())), 'aic': float(r.aic), 'n_obs': int(r.nobs)})")
    elif m == "stationarity":
        cols = ([y] if y else []) + x
        add("    from statsmodels.tsa.stattools import adfuller, kpss")
        add("    out = {}")
        add(f"    for c in {json.dumps(cols)}:")
        add("        v = frame[c].drop_nulls().to_numpy()")
        add("        v = v[np.isfinite(v)]")
        add(f"        a, k = adfuller(v, regression={json.dumps(p.get('regression', 'c'))}, autolag='AIC'), kpss(v, regression={json.dumps(p.get('regression', 'c'))}, nlags='auto')")
        add("        print(c, 'ADF stat %.3f p %.3f | KPSS stat %.3f p %.3f' % (a[0], a[1], k[0], k[1]))")
        add("        out[c] = {'adf_p': float(a[1]), 'kpss_p': float(k[1])}")
        add("    emit(out)")
    elif m in ("kmeans", "hierarchical", "pca"):
        layout = p.get("layout", "rows")
        std = p.get("standardize", True)
        if layout == "pivot":
            var = p.get("pivot_var") or x[0]
            add(f"    f = frame.select('entity_id', 'entity_label', 'date', {json.dumps(var)}).to_pandas()")
            add(f"    wide = f.pivot_table(index=['entity_id', 'entity_label'], columns='date', values={json.dumps(var)}, aggfunc='first')")
            add("    wide = wide.loc[:, wide.notna().mean(axis=0) >= 0.5]")
            add(f"    wide = wide.loc[wide.isna().mean(axis=1) <= {float(p.get('max_missing_share', 0.2))}]")
            add("    raw = wide.to_numpy(float)")
            add("    raw = np.where(np.isnan(raw), np.nanmean(raw, axis=0), raw)")
            add("    labels = [str(i[1]) for i in wide.index]")
        else:
            add(f"    cols = {json.dumps(x)}")
            add("    df = frame.to_pandas()")
            add("    for c in cols:")
            add("        df[c] = df[c].astype(float).replace([np.inf, -np.inf], np.nan)")
            if p.get("impute", "drop") == "median":
                add("    df[cols] = df[cols].fillna(df[cols].median())")
            add("    df = df.dropna(subset=cols)")
            add("    raw = df[cols].to_numpy(float)")
            add("    labels = (df['entity_label'] if 'entity_label' in df else df['date']).astype(str).tolist()")
        if std:
            add("    sd = raw.std(axis=0); sd[sd == 0] = 1.0")
            add("    X = (raw - raw.mean(axis=0)) / sd")
        else:
            add("    X = raw")
        if m == "kmeans":
            k = summ.get("k") or p.get("k") or 3
            add("    from sklearn.cluster import KMeans")
            add("    from sklearn.metrics import silhouette_score")
            add(f"    km = KMeans(n_clusters={int(k)}, n_init={int(p.get('n_init', 10))}, max_iter={int(p.get('max_iter', 300))}, random_state={study.seed}).fit(X)")
            add("    print('k =', km.n_clusters, 'silhouette =', silhouette_score(X, km.labels_))")
            add("    for c in range(km.n_clusters):")
            add("        print(c, [l for l, g in zip(labels, km.labels_) if g == c][:20])")
            add("    emit({'k': int(km.n_clusters), 'inertia': float(km.inertia_), 'sizes': np.bincount(km.labels_).tolist()})")
        elif m == "hierarchical":
            k = summ.get("k") or p.get("n_clusters") or 3
            add("    from scipy.cluster.hierarchy import fcluster, linkage")
            add("    from scipy.spatial.distance import pdist, squareform")
            if p.get("metric") == "correlation" and layout == "pivot":
                var = p.get("pivot_var") or x[0]
                add(f"    piv = frame.select('entity_id', 'date', {json.dumps(var)}).to_pandas().pivot_table(index='date', columns='entity_id', values={json.dumps(var)})")
                add("    piv = piv[[i[0] for i in wide.index]]")
                add(f"    C = np.nan_to_num(piv.corr(min_periods={int(p.get('min_periods', 12))}).to_numpy(), nan=0.0)")
                add("    np.fill_diagonal(C, 1.0)")
                add("    D = np.clip(1.0 - C, 0, 2); np.fill_diagonal(D, 0.0); D = (D + D.T) / 2")
                add("    dvec = squareform(D, checks=False)")
            else:
                add(f"    dvec = np.nan_to_num(pdist(X, metric={json.dumps(p.get('metric', 'euclidean'))}), nan=0.0)")
            if p.get("linkage", "ward") == "ward" and p.get("metric", "euclidean") == "euclidean":
                add("    Z = linkage(X, method='ward')")
            else:
                add(f"    Z = linkage(dvec, method={json.dumps(p.get('linkage', 'ward'))})")
            if p.get("distance_threshold") and not p.get("n_clusters"):
                add(f"    lab = fcluster(Z, t={float(p['distance_threshold'])}, criterion='distance')")
            else:
                add(f"    lab = fcluster(Z, t={int(k)}, criterion='maxclust')")
            add("    for c in sorted(set(lab)):")
            add("        print(c, [l for l, g in zip(labels, lab) if g == c][:30])")
            add("    emit({'k': int(len(set(lab))), 'sizes': sorted(np.bincount(lab)[1:].tolist())})")
        else:
            nc = summ.get("n_components") or p.get("n_components") or 2
            add("    from sklearn.decomposition import PCA")
            add(f"    pc = PCA(n_components={int(nc)}, random_state={study.seed}).fit(X)")
            add("    print('explained variance ratio', pc.explained_variance_ratio_)")
            add("    emit({'explained_variance_ratio': pc.explained_variance_ratio_.tolist(), 'n_obs': int(X.shape[0])})")
    elif m in PHASE_B:
        lines += phase_b_code(study, env)
    elif m in PHASE_C:
        lines += phase_c_code(study, env)
    elif m in PHASE_S:
        lines += phase_s_code(study, env)
    elif m in PHASE_CF:
        lines += phase_cf_code(study, env)
    elif m in PHASE_V2:
        lines += phase_v2_code(study, env)
    elif m in PHASE_S3:
        lines += phase_s3_code(study, env)
    else:  # pragma: no cover
        add(f"    raise SystemExit('no script template for method {m}')")
    return "\n".join(lines) + "\n"


def export_script(env: dict[str, Any], run_dir: Path, data_dir: Path) -> str:
    """Python source for a finished run envelope (result.json)."""
    study = StudySpec.model_validate(env["study"])
    norm = study.model_dump(mode="json")
    rep = env.get("reproducibility") or {}
    head = HEADER.format(run_id=env["run_id"], title=(env.get("result") or {}).get("title") or study.method,
                         method=study.method, created=datetime.now(UTC).isoformat(timespec="seconds"),
                         snapshot=rep.get("snapshot_id"), spec_hash=rep.get("spec_hash"), seed=study.seed,
                         study_json=json.dumps(norm, indent=1).replace('"""', '\\"\\"\\"'),
                         tables_json=json.dumps(_tables(norm["dataset"]), indent=1),
                         data_dir=str(data_dir), run_dir=str(run_dir))
    engine = _embedded_engine()
    if study.method in PHASE_S:  # spatial / spillover methods: embed the spatial estimation core too
        engine += ("\n\n# " + "=" * 78 + " spatial core (app/modeling/_spatial.py, verbatim)\n"
                   + _strip_future(Path(_spatial.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")))
    if study.method in PHASE_CF:  # filtered conformal ellipsoid: embed the conformal core
        engine += ("\n\n# " + "=" * 78 + " conformal core (app/modeling/_conformal.py, verbatim)\n"
                   + _strip_future(Path(_conformal.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")))
    if study.method in PHASE_V2:  # Studio v2 S2: causal / advanced ML / neural cores
        for name, mod in (("causal & controls", _causal2), ("advanced ML", _advml), ("neural networks", _nn)):
            src = Path(mod.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
            engine += f"\n\n# {'=' * 78} {name} core (app/modeling/{Path(mod.__file__).name}, verbatim)\n" + _strip_future(src)
    if study.method in PHASE_S3:  # Studio v2 S3: conformal quantile + causal threads + LightGBM builder + probabilistic core
        for name, mod in (("conformal", _conformal), ("causal & controls", _causal2), ("advanced ML", _advml), ("probabilistic", _prob)):
            src = Path(mod.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
            engine += f"\n\n# {'=' * 78} {name} core (app/modeling/{Path(mod.__file__).name}, verbatim)\n" + _strip_future(src)
    return head + engine + LOADER + _method_code(study, env) + MAIN
