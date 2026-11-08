"""Export templates for Phase C methods (owner: model-studio): body of fit(frame), calling the embedded
app/modeling/_ml.py functions exactly like the Studio wrappers."""

from __future__ import annotations

import json
from typing import Any

PHASE_C = {"regularized", "tree_ensemble", "gmm", "density_clustering", "factor_model", "leaderboard"}


def _folds_line(study: Any, env: dict[str, Any], p: dict[str, Any]) -> str:
    res = env.get("result") or {}
    ev = res.get("evaluation") or {}
    cv = p.get("cv") or {}
    scheme = (res.get("summary") or {}).get("cv_scheme") or ev.get("scheme") or "expanding"
    return (f"    folds = cv_splits(df['date'], {json.dumps(scheme)}, {int(cv.get('folds', 5))}, {int(ev.get('purge') or 1)}, "
            f"{int(cv.get('embargo', 0))}, seed={study.seed})")


def _features(study: Any, p: dict[str, Any]) -> list[str]:
    x = list(study.x)
    L = [f"    cols = {json.dumps(x)}", "    df = frame.to_pandas()",
         "    for c in cols:", "        df[c] = df[c].astype(float).replace([np.inf, -np.inf], np.nan)"]
    if p.get("impute", "drop") == "median":
        L.append("    df[cols] = df[cols].fillna(df[cols].median())")
    L += ["    df = df.dropna(subset=cols)", "    raw = df[cols].to_numpy(float)"]
    if p.get("standardize", True):
        L += ["    sd = raw.std(axis=0); sd[sd == 0] = 1.0", "    X = (raw - raw.mean(axis=0)) / sd"]
    else:
        L.append("    X = raw")
    return L


def phase_c_code(study: Any, env: dict[str, Any]) -> list[str]:
    m, p = study.method, dict(study.params)
    y, x = study.y, list(study.x)
    summ = (env.get("result") or {}).get("summary") or {}
    J = json.dumps
    L: list[str] = []
    add = L.append
    if m == "regularized":
        add(f"    df = complete(frame, {J([y] + x)})")
        add(f"    X, y = df[{J(x)}].to_numpy(float), df[{J(y)}].to_numpy(float)")
        add(f"    coef, b0, _ = fit_regularized(X, y, {J(p.get('model', 'elasticnet'))}, {float(summ['alpha'])}, {float(p.get('l1_ratio', 0.5))})")
        add(f"    print('alpha', {float(summ['alpha'])}); print(dict(zip({J(x)}, coef)))")
        add(f"    emit({{'params': {{'const': b0, **dict(zip({J(x)}, coef.tolist()))}}}})")
    elif m == "tree_ensemble":
        add(f"    df = complete(frame, {J([y] + x)})")
        add(f"    X, y = df[{J(x)}].to_numpy(float), df[{J(y)}].to_numpy(float)")
        add(_folds_line(study, env, p))
        task = summ.get("task", "regression")
        add(f"    prm = {J({k: v for k, v in p.items() if k not in ('cv',)})}")
        add(f"    oos = cv_predict(lambda: make_tree({J(p.get('model', 'hgb'))}, {J(task)}, prm, {study.seed}), X, y, folds, {J(task)})")
        add("    tested = np.isfinite(oos)")
        add(f"    met = ({'clf_metrics' if task == 'classification' else 'reg_metrics'})(y[tested], oos[tested])")
        add("    print(met); emit({'oos': met})")
    elif m in ("gmm", "density_clustering"):
        L += _features(study, p)
        if m == "gmm":
            add(f"    g, rows = gmm_select(X, {int(p.get('k_min', 1))}, min({int(p.get('k_max', 8))}, len(X) - 1), "
                f"{J(p.get('covariance_type', 'full'))}, {study.seed})")
            add("    print(rows); emit({'k': int(g.n_components), 'bic': float(g.bic(X))})")
        else:
            add(f"    lab = density_cluster(X, {J(p.get('algorithm', 'hdbscan'))}, {float(p.get('eps', 0.5))}, "
                f"{int(p.get('min_samples', 5))}, {int(p.get('min_cluster_size', 10))})")
            add("    emit({'k': int(len(set(lab[lab >= 0].tolist()))), 'noise': int((lab < 0).sum())})")
    elif m == "factor_model":
        fs = list(p.get("factor_scores", []))
        add(f"    df = complete(frame, {J(list(dict.fromkeys([y] + x + fs)))})")
        add("    F = {}")
        add(f"    for s in {J(fs)}:")
        add(f"        F[s + '_LS'] = long_short_factor(df, s, {J(y)}, {int(p.get('quantiles', 5))}).set_index('date')['ls']")
        if p.get("include_market", True):
            add(f"    F['MKT'] = df.groupby('date')[{J(y)}].mean()")
        add(f"    for c in {J(x)}:")
        add("        F[c] = df.groupby('date')[c].first()")
        add("    F = pd.DataFrame(F).sort_index().dropna()")
        if p.get("test_assets", "portfolios") == "portfolios":
            add(f"    R = pd.concat([quantile_portfolios(df, s, {J(y)}, {int(p.get('quantiles', 5))}) for s in {J(fs)}], axis=1)")
        else:
            add(f"    W = df.pivot_table(index='date', columns='entity_label', values={J(y)}, aggfunc='first')")
            add(f"    R = W.loc[:, W.notna().mean() >= 0.95].dropna().iloc[:, :{int(p.get('max_entities', 60))}]")
        add(f"    ts = ts_factor_regressions(R, F, {p.get('hac_lags')})")
        add("    print(ts['table'].to_string()); print('GRS', ts['grs'], 'p', ts['grs_p'])")
        add("    emit({'grs': ts['grs'], 'grs_p': ts['grs_p']})")
    elif m == "leaderboard":
        cands = p.get("candidates", [])
        allx = sorted(set(x) | {c for cd in cands for c in (cd.get("x") or [])})
        add(f"    df = complete(frame, {J([y] + allx)})")
        add(f"    y = df[{J(y)}].to_numpy(float)")
        add(_folds_line(study, env, p))
        task = summ.get("task", "regression")
        add(f"    out = {{}}")
        add(f"    for cd in {J(cands)}:")
        add(f"        cols = cd.get('x') if cd.get('x') is not None else {J(x)}")
        add("        X = df[cols].to_numpy(float) if cols else np.empty((len(y), 0))")
        add("        pr = np.full(len(y), np.nan)")
        add("        for tr, te in folds:")
        add(f"            pr[te] = candidate_predict(cd, X[tr], y[tr], X[te], {J(task)}, {study.seed})")
        add("        ok = np.isfinite(pr)")
        add(f"        out[cd['name']] = ({'clf_metrics' if task == 'classification' else 'reg_metrics'})(y[ok], pr[ok])")
        add("        print(cd['name'], out[cd['name']])")
        add("    emit({'metrics': out})")
    return L
