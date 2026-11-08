"""Export template for the filtered conformal ellipsoid (owner: conformal): body of fit(frame), calling the embedded
app/modeling/_conformal.py functions exactly like the Studio wrapper (app.modeling.methods.conformal)."""

from __future__ import annotations

import json
from typing import Any

PHASE_CF = {"conformal_ellipsoid"}


def phase_cf_code(study: Any, env: dict[str, Any]) -> list[str]:
    p = dict(study.params)
    J = json.dumps
    dflt = {"filter": "ewma", "transform": "none", "alpha": 0.1, "pretrain_frac": 0.3, "window": 250, "min_cal": 50,
            "aci_gamma": 0.005, "lam": 0.94, "lam_corr": 0.985, "shrink": 0.1, "panel_variable": None, "max_entities": 40}
    q = {**dflt, **p}
    L: list[str] = []
    add = L.append
    if study.dataset.kind == "panel":
        var = q["panel_variable"] or study.y or (study.x[0] if study.x else None)
        add(f"    f = frame.select('date', 'entity_id', {J(var)}).to_pandas()")
        add(f"    W = f.pivot_table(index='date', columns='entity_id', values={J(var)}, aggfunc='last').sort_index()")
        add(f"    W = W[W.notna().mean().sort_values(ascending=False).index[:{int(q['max_entities'])}]]")
    else:
        cols = ([study.y] if study.y else []) + [c for c in study.x if c != study.y]
        add(f"    W = frame.select(['date'] + {J(cols)}).to_pandas().set_index('date').sort_index()")
    add("    W = W.astype(float)")
    tr = q["transform"]
    if tr == "diff":
        add("    W = W.diff()")
    elif tr == "log_diff":
        add("    W = np.log(W.where(W > 0)).diff()")
    elif tr == "pct_change":
        add("    W = W.pct_change(fill_method=None)")
    add("    W = W.replace([np.inf, -np.inf], np.nan).dropna(how='any')")
    add(f"    out = run(W.to_numpy(float), {J(q['filter'])}, {float(q['alpha'])!r}, {float(q['pretrain_frac'])!r}, "
        f"{int(q['window'])}, {int(q['min_cal'])}, {float(q['aci_gamma'])!r}, {float(q['lam'])!r}, {float(q['lam_corr'])!r}, "
        f"{float(q['shrink'])!r})")
    add("    print('coverage', out['coverage'], 'n_test', out['n_test'], 'w_proj', out['w_proj'], 'q_next', out['q_next'])")
    add("    emit({'coverage': out['coverage'], 'n_test': out['n_test'], 'w_proj': out['w_proj'], "
        "'log_volume': out['log_volume'], 'q_next': out['q_next']})")
    return L
