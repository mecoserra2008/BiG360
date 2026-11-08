"""Export templates for the Studio v2 S3 probabilistic methods (owner: model-studio): body of fit(frame), calling the
embedded app/modeling/_prob.py functions exactly like app.modeling.methods.probabilistic and emitting the out-of-fold
point metrics and the CRPS."""

from __future__ import annotations

from typing import Any

from ._export_v2 import J, _ml_folds

PHASE_S3 = {"bayes_ridge", "gp_regression", "quantile_gbm", "ngboost", "hier_pool"}


def phase_s3_code(study: Any, env: dict[str, Any]) -> list[str]:
    from .registry import get_method

    m = study.method
    q = get_method(m).parse_params(dict(study.params)).model_dump()
    res = env.get("result") or {}
    sm, ev = res.get("summary") or {}, res.get("evaluation") or {}
    y, x, seed = study.y, list(study.x), int(study.seed)
    L: list[str] = []
    add = L.append
    cols = list(dict.fromkeys([y] + x))
    if m == "hier_pool":
        grp = q["group"]
        add("    import polars as pl")
        add(f"    f = frame.filter(pl.col({J(grp)}).is_not_null())")
        add(f"    keep = ['date'] + (['entity_id', 'entity_label'] if 'entity_id' in f.columns else []) + [{J(grp)}]")
        add(f"    df = f.select(list(dict.fromkeys(keep + {J(cols)}))).to_pandas()")
        add(f"    for c in {J(cols)}:")
        add("        df[c] = df[c].astype(float).replace([np.inf, -np.inf], np.nan)")
        add(f"    df = df.dropna(subset={J(cols)}).reset_index(drop=True)")
        add(f"    df['_grp'] = df[{J(grp)}].astype(str)")
        add("    cnt = df['_grp'].value_counts()")
        add(f"    df = df[df['_grp'].isin(cnt[cnt >= {int(q['min_group_n'])}].index)].reset_index(drop=True)")
        add("    g = df['_grp'].to_numpy()")
    else:
        add(f"    df = complete(frame, {J(cols)})")
    add(f"    y = df[{J(y)}].to_numpy(float)")
    add(f"    X = df[{J(x)}].to_numpy(float)")
    add(_ml_folds(ev, sm, q, seed))
    if m == "bayes_ridge":
        add(f"    out = bayes_linear_cv(X, y, folds, {J(q['model'])}, {int(q['max_iter'])})")
    elif m == "gp_regression":
        add(f"    out = gp_cv(X, y, folds, {J(q['kernel'])}, {q['ard']!r}, {int(q['restarts'])}, {seed})")
    elif m == "ngboost":
        prm = {k: v for k, v in q.items() if k != "cv"}
        add(f"    out = ngboost_cv(X, y, folds, {J(prm)}, {seed}, dates=pd.to_datetime(df['date']).to_numpy())")
    elif m == "hier_pool":
        est = q["estimator"]
        gcv = {"n_iter": max(500, q["n_iter"] // 4), "burn": max(250, q["n_iter"] // 8), "chains": 2}
        add(f"    out = pool_cv(X, y, g, folds, {J(est)}, {J(gcv)}, {seed})")
    if m == "quantile_gbm":
        prm = {k: v for k, v in q.items() if k not in ("cv", "quantiles")}
        taus = list(q["quantiles"])
        add(f"    taus = {J(taus)}")
        add(f"    Q = quantile_lgbm_cv(X, y, folds, taus, {J(prm)}, {seed})['Q']")
        add("    mu = Q[:, taus.index(0.5)]")
        add("    t = np.isfinite(Q).all(1)")
        add("    crps = float(np.mean(crps_quantiles(y[t], Q[t], taus)))")
    else:
        add("    mu, sd = out['mu'], out['sd']")
        add("    t = np.isfinite(mu) & np.isfinite(sd)")
        add("    crps = float(np.mean(crps_gauss(y[t], mu[t], sd[t])))")
    add("    met = reg_metrics(y[t], mu[t])")
    add("    print(met, 'CRPS', crps)")
    add("    emit({'metrics': met, 'crps': crps})")
    return L
