"""Export templates for Phase B methods (owner: model-studio): the body of fit(frame) in exported scripts.
Each template calls the same app/modeling/_algos.py functions (embedded verbatim) as the Studio wrapper."""

from __future__ import annotations

import json
from typing import Any

PHASE_B = {"panel", "fama_macbeth", "iv2sls", "did", "synthetic_control", "event_car", "var", "vecm", "cointegration",
           "garch", "markov_switching", "state_space", "tv_beta"}


def phase_b_code(study: Any, env: dict[str, Any]) -> list[str]:
    m, p = study.method, dict(study.params)
    y, x = study.y, list(study.x)
    summ = (env.get("result") or {}).get("summary") or {}
    J = json.dumps
    L: list[str] = []
    add = L.append
    if m == "panel":
        add(f"    df = complete(frame, {J([y] + x)})")
        add(f"    r = panel_fit(df, {J(y)}, {J(x)}, {J(p.get('estimator', 'fe'))}, {p.get('entity_effects', True)}, "
            f"{p.get('time_effects', False)}, {J(p.get('cov_type', 'clustered'))}, {p.get('cluster_entity', True)}, "
            f"{p.get('cluster_time', False)}, {p.get('kernel_bandwidth')})")
        add("    print(r)")
        add("    emit({'params': r.params.to_dict(), 'bse': r.std_errors.to_dict(), 'n_obs': int(r.nobs)})")
    elif m == "fama_macbeth":
        add(f"    df = complete(frame, {J([y] + x)})")
        add(f"    fm = fama_macbeth(df, {J(y)}, {J(x)}, {p.get('nw_lags')}, {p.get('min_obs')})")
        add("    print(fm['table'].to_string()); print('average R2', fm['r2'].mean())")
        add("    emit({'params': dict(zip(fm['table']['term'], fm['table']['estimate'])), "
            "'bse': dict(zip(fm['table']['term'], fm['table']['std_error']))})")
    elif m == "iv2sls":
        en, ins = p.get("endog", []), p.get("instruments", [])
        add(f"    df = complete(frame, {J(list(dict.fromkeys([y] + x + en + ins)))})")
        cl = "pd.factorize(df['entity_id'])[0]" if p.get("cov_type") == "clustered" else "None"
        add(f"    r = iv_fit(df, {J(y)}, {J(x)}, {J(en)}, {J(ins)}, {J(p.get('estimator', '2sls'))}, "
            f"{J(p.get('cov_type', 'robust'))}, {cl})")
        add("    print(r); print(r.first_stage)")
        add("    emit({'params': r.params.to_dict(), 'bse': r.std_errors.to_dict(), 'n_obs': int(r.nobs)})")
    elif m in ("did", "synthetic_control", "event_car"):
        t = p.get("treatment") or {}
        extra = [c for c in (t.get("column"), p.get("event_column"), p.get("market")) if c]
        add(f"    df = complete(frame, {J([y] + extra)})")
        add("    for c in [c for c in ('sector_id', 'region', 'country') if c in frame.columns]:")
        add("        df = df.merge(frame.select('entity_id', c).unique('entity_id').to_pandas(), on='entity_id', how='left')")
        if m == "did":
            add(f"    t = {J(t, default=str)}")
            add("    tdates = resolve_treatment_dates(df, t.get('mode', 'entities'), t.get('entities'), t.get('attribute'), "
                "t.get('values'), t.get('column'), t.get('date') and pd.Timestamp(t['date']).date(), "
                "[(e['entity'], pd.Timestamp(e['date']).date()) for e in t.get('events', [])])")
            add("    tp = treat_period(df, tdates)")
            d = p.get("design", "event_study")
            if d == "2x2":
                add(f"    out = did_2x2(df, {J(y)}, pd.Series({{e: 1.0 for e in tdates}}), next(iter(tdates.values())))")
                add("    print(out['res'].summary()); emit({'att': float(out['res'].params['did'])})")
            elif d == "twfe":
                add("    pos, _ = period_index(df['date'])")
                add("    D = (df['date'].map(pos) >= df['entity_id'].map(tp)).astype(float)")
                add(f"    r = twfe(df, {J(y)}, D, {J(x)}); print(r); emit({{'att': float(r.params['D'])}})")
            elif d == "event_study":
                add(f"    out = event_study(df, {J(y)}, tp, {int(p.get('leads', 6))}, {int(p.get('lags', 12))}, {J(x)})")
                add("    print(out['res']); print('pre-trend', out['pre_test'])")
                add("    emit({'path': {str(k): b for k, b, *_ in out['path']}})")
            else:
                add(f"    cs = callaway_santanna(df, {J(y)}, tp, {J(p.get('control', 'never'))}, "
                    f"{int(p.get('bootstrap', 199))}, {study.seed})")
                add("    print(cs['dynamic']); emit({'att': cs['overall'][0]})")
        elif m == "synthetic_control":
            donors = p.get("donors") or []
            add(f"    W = df.pivot_table(index='date', columns='entity_id', values={J(y)}, aggfunc='first').sort_index()")
            add(f"    tid = next(iter(match_entities(df, [{J(p['treated'])}])))")
            add(f"    donors = sorted(match_entities(df, {J(donors)})) if {bool(donors)} else [c for c in W.columns if c != tid]")
            add("    W = W[[tid] + [d for d in donors if d != tid]].dropna(axis=1, how='any')"
                f".iloc[:, :{int(p.get('max_donors', 60)) + 1}]")
            add(f"    t0 = next(i for i, d in enumerate(W.index) if pd.Timestamp(d) >= pd.Timestamp({J(str(p['treatment_date']))}))")
            add(f"    sc = synthetic_control(W, tid, t0, {p.get('placebo', True)})")
            add("    print(sc['weights'].sort_values(ascending=False).head(10)); print('pre RMSPE', sc['pre_rmspe'], 'p', sc.get('p_value'))")
            add("    emit({'avg_post_gap': float(np.mean(sc['gap'][t0:])), 'weights': sc['weights'].to_dict()})")
        else:
            mk = p.get("market")
            add(f"    R = df.pivot_table(index='date', columns='entity_id', values={J(y)}, aggfunc='first').sort_index()")
            add(f"    mkt = df.groupby('date')[{J(mk)}].first().reindex(R.index)" if mk else "    mkt = R.mean(axis=1)")
            evs = [(e["entity"], str(e["date"])) for e in p.get("events", [])]
            add(f"    events = [(e, pd.Timestamp(d).date()) for name, d in {J(evs)} for e in match_entities(df, [name])]")
            ec = p.get("event_column")
            if ec:
                add(f"    g = df.sort_values(['entity_id', 'date']); on = g[{J(ec)}].fillna(0) > 0.5")
                add("    st = on & ~on.groupby(g['entity_id']).shift(1, fill_value=False)")
                add("    events = list(zip(g.loc[st, 'entity_id'], g.loc[st, 'date'])) + events")
            add(f"    out = car_study(R, mkt, events[:{int(p.get('max_events', 2000))}], {tuple(p.get('est_window', [-250, -30]))}, "
                f"{tuple(p.get('event_window', [-5, 5]))}, {J(p.get('model', 'market'))})")
            add("    print('CAAR', out['caar'][-1], 't', out['t_cs'], 'Patell', out['z_patell'])")
            add("    emit({'caar': float(out['caar'][-1]), 'n_events': out['n_events']})")
    elif m in ("var", "vecm", "cointegration"):
        cols = ([y] if y else []) + [c for c in x if c != y]
        add(f"    Y = complete(frame, {J(cols)})[{J(cols)}]")
        if m == "var":
            add(f"    r, sel, p = var_fit(Y, {int(p.get('maxlags', 8))}, {J(p.get('ic', 'bic'))}, {p.get('lags')}, {J(p.get('trend', 'c'))})")
            add("    print(r.summary())")
            add("    emit({'lags': p, 'params': {f'{eq}: {n}': float(r.params.loc[n, eq]) for eq in r.params.columns for n in r.params.index}})")
        elif m == "vecm":
            det = {-1: "n", 0: "co", 1: "lo"}[int(p.get("det_order", 0))]
            add("    from statsmodels.tsa.vector_ar.vecm import VECM")
            add(f"    j = johansen(Y, {int(p.get('det_order', 0))}, {int(p.get('k_ar_diff', 1))}); print(j['table'])")
            add(f"    r = VECM(Y.to_numpy(), k_ar_diff={int(p.get('k_ar_diff', 1))}, coint_rank={int(summ.get('rank') or 1)}, "
                f"deterministic={J(det)}).fit()")
            add("    print(r.summary()); emit({'beta': r.beta.tolist(), 'alpha': r.alpha.tolist()})")
        else:
            add(f"    ps = pairs_spread(Y.iloc[:, 0].to_numpy(), Y.iloc[:, 1].to_numpy(), {int(p.get('z_window', 60))})")
            add(f"    j = johansen(Y, {int(p.get('det_order', 0))}, {int(p.get('k_ar_diff', 1))}); print(j['table'])")
            add("    print('hedge', ps['hedge_ratio'], 'EG p', ps['eg_p'], 'half-life', ps['half_life'])")
            add("    emit({'hedge_ratio': ps['hedge_ratio'], 'eg_p': ps['eg_p'], 'rank': j['rank']})")
    else:
        add(f"    df = complete(frame, {J([y] + x)})")
        if m == "garch":
            add(f"    g = garch_fit(df[{J(y)}].to_numpy(float) * {float(p.get('scale', 100.0))}, {J(p.get('vol', 'garch'))}, "
                f"{int(p.get('p', 1))}, {int(p.get('q', 1))}, {J(p.get('dist', 't'))}, {J(p.get('mean', 'constant'))}, "
                f"{int(p.get('ar_lags', 1))})")
            add("    print(g.summary()); emit({'params': g.params.to_dict()})")
        elif m == "markov_switching":
            ex = f"df[{J(x)}].to_numpy(float)" if x and p.get("model", "regression") == "regression" else "None"
            add(f"    r = markov_fit(df[{J(y)}].to_numpy(float), {int(p.get('k_regimes', 2))}, {J(p.get('model', 'regression'))}, "
                f"{int(p.get('order', 1))}, {ex}, {p.get('switching_variance', True)}, {int(p.get('search_reps', 10))}, {study.seed})")
            add("    print(r.summary()); print('expected durations', r.expected_durations)")
            add("    emit({'params': dict(zip(r.model.param_names, map(float, r.params))), 'llf': float(r.llf)})")
        elif m == "state_space":
            ex = f"df[{J(x)}].to_numpy(float)" if x else "None"
            add(f"    r = ucm_fit(df[{J(y)}].to_numpy(float), {J(p.get('level', 'local level'))}, {p.get('seasonal')}, {ex})")
            add("    print(r.summary()); emit({'params': dict(zip(r.model.param_names, map(float, r.params)))})")
        else:
            add(f"    X = df[{J(x)}].to_numpy(float)")
            if p.get("add_constant", True):
                add("    X = np.column_stack([np.ones(len(X)), X])")
            add(f"    r = tv_beta(df[{J(y)}].to_numpy(float), X)")
            add("    print(r.summary()); emit({'last_beta': r.smoothed_state[:, -1].tolist(), 'params': r.params.tolist()})")
    return L
