"""Export templates for the Studio v2 S2 methods (owner: model-studio): body of fit(frame), calling the embedded
app/modeling/_causal2.py, _advml.py and _nn.py functions exactly like the Studio wrappers (app.modeling.methods.controls /
advml / neural) and emitting the headline numbers."""

from __future__ import annotations

from typing import Any

PHASE_V2 = {"dml", "propensity", "rdd", "coef_stability", "lightgbm", "optuna_tune", "stacking", "ltr", "mlp", "rnn",
            "autoencoder"}
J = repr  # Python literals (True / None), not JSON


def _groups(summary: dict[str, Any]) -> str:
    cl = summary.get("cluster") or "none"
    if cl == "none":
        return "None"
    if cl == "date":
        return "pd.to_datetime(df['date']).to_numpy()"
    return "df['entity_id'].astype(str).to_numpy()"


def _ml_folds(ev: dict[str, Any], summary: dict[str, Any], q: dict[str, Any], seed: int, frame: str = "df") -> str:
    cv = q.get("cv") or {}
    scheme = summary.get("cv_scheme") or ev.get("scheme")
    return (f"    folds = cv_splits({frame}['date'], {J(scheme)}, {int(cv.get('folds', 5))}, {int(ev.get('purge') or 1)}, "
            f"{int(cv.get('embargo', 0))}, seed={seed})")


def phase_v2_code(study: Any, env: dict[str, Any]) -> list[str]:
    from .registry import get_method

    m = study.method
    q = get_method(m).parse_params(dict(study.params)).model_dump()
    res = env.get("result") or {}
    sm, ev = res.get("summary") or {}, res.get("evaluation") or {}
    y, x, seed = study.y, list(study.x), int(study.seed)
    L: list[str] = []
    add = L.append
    add("    import pandas as pd")
    extra = {"dml": [q.get("treatment")], "propensity": [q.get("treatment")], "coef_stability": [q.get("treatment")],
             "rdd": [q.get("running")] + ([q["treatment"]] if q.get("treatment") else [])}.get(m, [])
    cols = ([y] if y else []) + x + [c for c in extra if c]
    add(f"    df = complete(frame, {J(list(dict.fromkeys(cols)))})")
    if y:
        add(f"    y = df[{J(y)}].to_numpy(float)")
    add(f"    X = df[{J(x)}].to_numpy(float) if {J(x)} else np.empty((len(df), 0))")
    if m in ("dml", "propensity", "rdd"):
        cf = q.get("crossfit") or {}
        reps = int(sm.get("n_rep") or 1)
        add(f"    groups = {_groups(sm)}")
        if m != "rdd":
            add(f"    sets = [cv_splits(df['date'], {J(sm.get('cv_scheme'))}, {int(cf.get('folds', 5))}, {int(sm.get('purge') or 1)}, "
                f"{int(cf.get('embargo', 0))}, seed={seed} + r) for r in range({reps})]")
    if m == "dml":
        add(f"    d = df[{J(q['treatment'])}].to_numpy(float)")
        add(f"    out = dml(y, d, X, sets, {J(q['model'])}, {J(q['learner_y'])}, {J(q['learner_d'])}, {J(q['estimand'])}, "
            f"{q['trim']!r}, groups, {seed})")
        add("    print('theta', out['theta'], 'se', out['se'], 'p', out['p'])")
        add("    emit({'theta': out['theta'], 'se': out['se']})")
    elif m == "propensity":
        add(f"    D = df[{J(q['treatment'])}].to_numpy(float) > 0.5")
        ps = "ols" if q["ps_model"] == "logit" else q["ps_model"]
        add(f"    e_raw = crossfit({J(ps)}, 'classification', X, D.astype(float), sets[0], {seed})")
        add(f"    e = np.clip(e_raw, {max(q['trim'], 1e-6)!r}, 1 - {max(q['trim'], 1e-6)!r})")
        add("    out = {}")
        if q["estimator"] in ("all", "ipw"):
            add(f"    out['ipw'] = ipw(y, D, e, {J(q['estimand'])}, groups)['tau']")
        if q["estimator"] in ("all", "aipw"):
            add(f"    g0 = crossfit({J(q['outcome_model'])}, 'regression', X, y, sets[0], {seed}, subset=~D)")
            add(f"    g1 = crossfit({J(q['outcome_model'])}, 'regression', X, y, sets[0], {seed}, subset=D)")
            add(f"    out['aipw'] = aipw(y, D, e, g0, g1, {J(q['estimand'])}, groups)['tau']")
        if q["estimator"] in ("all", "matching"):
            add(f"    out['matching'] = nn_match(y, D, e, {int(q['k_neighbors'])}, {q['caliper']!r})['tau']")
        add("    print(out)")
        add("    emit(out)")
    elif m == "rdd":
        add(f"    xc = df[{J(q['running'])}].to_numpy(float) - {float(q['cutoff'])!r}")
        add(f"    d = df[{J(q['treatment'])}].to_numpy(float) if {J(q['design'] == 'fuzzy')} else None")
        add(f"    h = {float(sm.get('h'))!r}  # bandwidth of the run (IK plug-in: ik_bandwidth(xc, y, {J(q['kernel'])})['h'])")
        add(f"    conv = rd_estimate(xc, y, h, {J(q['kernel'])}, 1, d, groups)")
        add(f"    rob = rd_estimate(xc, y, h, {J(q['kernel'])}, 2, d, groups)")
        add("    print('conventional', conv['tau'], conv['se'], 'robust', rob['tau'], rob['se'])")
        add(f"    emit({{'estimate': (rob if {J(q['bandwidth'] == 'cct')} else conv)['tau'], 'estimate_robust': rob['tau'], 'h': h}})")
    elif m == "coef_stability":
        add(f"    groups = {_groups(sm)}")
        add(f"    steps = coef_stability(y, df[{J(q['treatment'])}].to_numpy(float), X, groups)")
        add(f"    o = oster(steps[0]['beta'], steps[0]['r2'], steps[-1]['beta'], steps[-1]['r2'], {float(sm.get('r_max'))!r}, {q['delta']!r})")
        add("    print(steps, o)")
        add("    emit({'beta_short': steps[0]['beta'], 'beta_full': steps[-1]['beta'], 'beta_star': o['beta_star'], "
            "'delta_for_zero': o['delta_for_zero']})")
    elif m == "lightgbm":
        prm = {k: v for k, v in q.items() if k not in ("cv", "task", "shap", "shap_sample", "early_stopping", "val_frac", "stopping_rounds")}
        add(_ml_folds(ev, sm, q, seed))
        add(f"    cv = lgbm_cv(X, y, df['date'].to_numpy(), folds, {J(sm.get('task'))}, {J(prm)}, {seed}, {q['early_stopping']!r}, "
            f"{q['val_frac']!r}, {int(ev.get('purge') or 1)}, {int(q['stopping_rounds'])})")
        add("    t = np.isfinite(cv['oos'])")
        add(f"    met = (clf_metrics if {J(sm.get('task'))} == 'classification' else reg_metrics)(y[t], cv['oos'][t])")
        add("    print(met)")
        add("    emit({'metrics': met})")
    elif m == "optuna_tune":
        cv = q.get("cv") or {}
        purge = int(sm.get("purge") or 1)
        add(f"    tune, hold = holdout_split(df['date'].to_numpy(), {q['holdout_frac']!r}, {purge}, {seed})")
        add(f"    inner = cv_splits(df['date'].to_numpy()[tune], {J(sm.get('cv_scheme'))}, {int(cv.get('folds', 4))}, {purge}, "
            f"{int(cv.get('embargo', 0))}, seed={seed})")
        add(f"    st = optuna_tune({J(q['model'])}, {J(sm.get('task'))}, X[tune], y[tune], inner, {int(q['n_trials'])}, "
            f"{float(q['timeout_s'])!r}, {seed})")
        add("    print('best', st.best_value, st.best_params)")
        add("    emit({'best_params': st.best_params, 'cv_best_loss': st.best_value})")
    elif m == "stacking":
        add(_ml_folds(ev, sm, q, seed))
        cv = q.get("cv") or {}
        add(f"    out = stack_cv(X, y, df['date'].to_numpy(), folds, {J(sm.get('bases'))}, {J(q['meta'])}, {J(sm.get('task'))}, "
            f"{J(sm.get('cv_scheme'))}, {int(q['inner_folds'])}, {int(ev.get('purge') or 1)}, {int(cv.get('embargo', 0))}, {seed})")
        add("    t = np.isfinite(out['stack'])")
        add(f"    met = (clf_metrics if {J(sm.get('task'))} == 'classification' else reg_metrics)(y[t], out['stack'][t])")
        add("    print(met)")
        add("    emit({'metrics': met})")
    elif m == "ltr":
        prm = {k: q[k] for k in ("n_estimators", "learning_rate", "num_leaves", "min_child_samples", "subsample", "colsample_bytree")}
        add(_ml_folds(ev, sm, q, seed))
        add(f"    s = ltr_cv(X, y, df['date'].to_numpy(), folds, {J(prm)}, {int(q['n_grades'])}, {seed})")
        add("    t = np.isfinite(s)")
        add(f"    per = per_date_scores(df['date'].to_numpy()[t], y[t], s[t], {int(q['n_grades'])}, {int(q['ndcg_k'])}, {int(q['quantiles'])})")
        add("    print(per.mean(numeric_only=True))")
        add("    emit({'rank_ic': float(per['ic'].dropna().mean()), 'ndcg': float(per['ndcg'].mean())})")
    elif m in ("mlp", "rnn"):
        task = sm.get("task")
        if m == "mlp":
            add(f"    make = lambda: mlp_net(X.shape[1], {J(q['hidden'])}, {q['dropout']!r}, {J(q['activation'])})")
            add(_ml_folds(ev, sm, q, seed))
            add(f"    out = nn_cv(make, X, y, df['date'].to_numpy(), folds, {J(task)}, {J({k: q[k] for k in ('lr', 'weight_decay', 'batch_size', 'max_epochs', 'patience', 'val_frac', 'n_seeds')})}, {seed}, {int(ev.get('purge') or 1)})")
        else:
            feats = list(sm.get("inputs") or x)
            if q["include_y_lags"]:
                lag = int(sm.get("y_lag_offset") or 1)
                add("    key = df['entity_id'].astype(str) if 'entity_id' in df.columns else pd.Series(0, index=df.index)")
                add("    df = df.assign(_k=key.to_numpy()).sort_values(['_k', 'date'])")
                add(f"    df[{J(feats[-1])}] = df.groupby('_k')[{J(y)}].shift({lag})")
                add("    df = df.drop(columns='_k').reset_index(drop=True)")
            add(f"    ends, W = make_windows(df, {J(feats)}, {int(q['window'])})")
            add("    df = df.iloc[ends].reset_index(drop=True)")
            add(f"    y = df[{J(y)}].to_numpy(float)")
            add(f"    make = lambda: rnn_net(W.shape[2], {J(q['cell'])}, {int(q['hidden_size'])}, {int(q['layers'])}, {q['dropout']!r})")
            add(_ml_folds(ev, sm, q, seed))
            add(f"    out = nn_cv(make, W, y, df['date'].to_numpy(), folds, {J(task)}, {J({k: q[k] for k in ('lr', 'weight_decay', 'batch_size', 'max_epochs', 'patience', 'val_frac', 'n_seeds')})}, {seed}, {int(ev.get('purge') or 1)})")
        add("    t = np.isfinite(out['oos'])")
        add(f"    met = (clf_metrics if {J(task)} == 'classification' else reg_metrics)(y[t], out['oos'][t])")
        add("    print(met)")
        add("    emit({'metrics': met})")
    elif m == "autoencoder":
        add(f"    tr, hold = holdout_split(df['date'].to_numpy(), {q['holdout_frac']!r}, 0, {seed})")
        add(f"    fi, vi = inner_val_split(tr, df['date'].to_numpy(), {q['val_frac']!r}, 0, {seed})")
        add(f"    out = ae_fit(X, fi, vi, {int(q['n_factors'])}, {J({k: q[k] for k in ('hidden', 'activation', 'lr', 'weight_decay', 'batch_size', 'max_epochs', 'patience', 'n_seeds')})}, {seed})")
        add("    Xs, R = out['Xs'], out['recon']")
        add("    r2 = float(1 - np.sum((Xs[hold] - R[hold]) ** 2) / np.sum(Xs[hold] ** 2))")
        add("    print('holdout R2', r2)")
        add("    emit({'holdout_r2': r2})")
    return L
