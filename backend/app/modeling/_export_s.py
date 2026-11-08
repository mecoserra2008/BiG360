"""Export templates for the spatial / spillover methods (owner: event-models, plan B5): body of fit(frame), calling
the embedded app/modeling/_spatial.py functions exactly like the Studio wrappers. The spatial weights used by the
run are embedded from the result summary ("w": codes + non-zero triplets), so the script needs no backend."""

from __future__ import annotations

import json
from typing import Any

PHASE_S = {"spatial_lag", "spatial_error", "spatial_durbin", "moran_lisa", "connectedness", "local_projection"}


def _w_lines(summ: dict[str, Any]) -> list[str]:
    w = summ.get("w") or {}
    return [f"    W_CODES = {json.dumps(w.get('codes', []))}",
            f"    W_IJ = {json.dumps(w.get('ij', []))}",
            "    W = np.zeros((len(W_CODES), len(W_CODES)))",
            "    for a, b, v in W_IJ:",
            "        W[a, b] = v"]


def phase_s_code(study: Any, env: dict[str, Any]) -> list[str]:
    m, p = study.method, dict(study.params)
    y, x = study.y, list(study.x)
    summ = (env.get("result") or {}).get("summary") or {}
    J = json.dumps
    L: list[str] = []
    add = L.append
    if m in ("spatial_lag", "spatial_error", "spatial_durbin"):
        L += _w_lines(summ)
        add(f"    df = complete(frame, {J([y] + x)})")
        add("    df = df[df['entity_id'].isin(W_CODES)]")
        add("    per = df.groupby('date')['entity_id'].nunique()")
        add("    df = df[df['date'].isin(per[per == len(W_CODES)].index)].sort_values(['date', 'entity_id'])")
        add("    order = sorted(W_CODES); ix = [W_CODES.index(c) for c in order]; W = W[np.ix_(ix, ix)]")
        add("    N = len(order)")
        add(f"    y = df[{J(y)}].to_numpy(float); X = df[{J(x)}].to_numpy(float)")
        fe = summ.get("fe") or p.get("fe", "entity")
        if m == "spatial_error":
            add(f"    f = sem_panel(y, X, W, N, fe={J(fe)})")
        else:
            add(f"    f = sar_panel(y, X, W, N, fe={J(fe)}, durbin={m == 'spatial_durbin'})")
        add(f"    eff = effects(f, {len(x)}, draws={int(p.get('effects_draws', 500))}, seed={study.seed})")
        add("    print('rho/lambda', f['rho'], 'beta', f['beta'], 'llf', f['llf'])")
        add("    for e in eff: print(e)")
        add("    emit({'rho': f['rho'], 'beta': f['beta'].tolist(), 'llf': f['llf'], "
            "'effects': [{k: e[k] for k in ('direct', 'indirect', 'total')} for e in eff]})")
    elif m == "moran_lisa":
        L += _w_lines(summ)
        add(f"    df = complete(frame, {J([y])})")
        if (summ.get("period") or p.get("period", "latest")) == "latest":
            add("    df = df[df['date'] == df['date'].max()]")
        add(f"    v = df.groupby('entity_id')[{J(y)}].mean().reindex(W_CODES)")
        add("    xv = v.to_numpy(float)")
        add(f"    mg = moran_perm(xv, W, {int(p.get('permutations', 999))}, {study.seed})")
        add(f"    ml = lisa_perm(xv, W, {int(p.get('permutations', 999))}, {study.seed})")
        add("    gz, gp = gi_star(xv, W)")
        add("    print('Moran I', mg)")
        add("    emit({'moran_i': mg['I'], 'p_sim': mg['p_sim'], 'gi_z': gz.tolist()})")
    elif m == "connectedness":
        cols = ([y] if y else []) + [c for c in x if c != y]
        add(f"    Y = complete(frame, {J(cols)})[{J(cols)}].to_numpy(float)")
        add(f"    t = dy_connectedness(Y, {int(p.get('lags', 1))}, {int(p.get('horizon', 10))}, {float(p.get('ridge', 0.0))})")
        add("    print('total', t['total']); print('net', t['net'])")
        add("    emit({'total': t['total'], 'to': t['to'].tolist(), 'from': t['from'].tolist(), 'net': t['net'].tolist()})")
    elif m == "local_projection":
        shock, controls = x[0], x[1:]
        add(f"    df = complete(frame, {J([y] + x)})")
        shocks = [shock]
        if p.get("spatial"):
            L += _w_lines(summ)
            add("    Wr = row_standardize(W)")
            add(f"    wide = df.pivot_table(index='date', columns='entity_id', values={J(shock)}, aggfunc='last').reindex(columns=W_CODES)")
            add("    lagged = pd.DataFrame(wide.fillna(0.0).to_numpy() @ Wr.T, index=wide.index, columns=pd.Index(W_CODES, name='entity_id'))")
            add("    lagged[wide.isna()] = np.nan; lagged.index.name = 'date'")
            add(f"    df = df.merge(lagged.stack().rename({J('W_' + shock)}).reset_index(), on=['date', 'entity_id'], how='inner')")
            shocks.append("W_" + shock)
        fe = summ.get("fe") or p.get("fe", "entity")
        add(f"    out = local_projection(df, {J(y)}, {J(shocks)}, {J(controls)}, {int(p.get('horizon', 12))}, "
            f"{int(p.get('y_lags', 2))}, {int(p.get('shock_lags', 0))}, cumulative={bool(p.get('cumulative', True))}, "
            f"fe={J(fe)})")
        add("    print(out.to_string())")
        add("    emit({'irf': out[out['shock'] == " + J(shock) + "]['coef'].tolist()})")
    return L
