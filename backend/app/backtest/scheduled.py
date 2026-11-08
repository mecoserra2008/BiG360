"""`backtest_scheduled` job (weekly DAG, owner: backtest): re-run paper strategies (schedule = weekly) on the latest
data, compare with the previous completed run and append a `backtest_drift` alert event on drift / decay.

Drift / decay rules (detect_drift):
  * metric drift: full-period Sharpe or CAGR falls by more than drift_threshold (relative), or the max drawdown deepens
    by more than drift_threshold (relative);
  * live decay: the returns of the days added since the previous run sit more than 2 standard deviations below the
    previous run's daily mean (z of the cumulative excess over the new segment);
  * drawdown breach: the current drawdown is deeper than the worst drawdown of the previous run;
  * holdings drift (informational): one-way turnover between the two runs' latest books.
Like studio_scheduled it writes app_state (backtest_runs, strategies.last_scheduled_at, alert_events) through short
retried transactions.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any


def detect_drift(old: dict[str, Any], new: dict[str, Any], threshold: float = 0.3) -> dict[str, Any]:
    ro, rn = old.get("result") or {}, new.get("result") or {}
    mo, mn = (ro.get("metrics") or {}).get("full") or {}, (rn.get("metrics") or {}).get("full") or {}
    issues, changes = [], []
    for k, better in (("sharpe", "higher"), ("cagr", "higher"), ("max_drawdown", "higher")):
        a, b = mo.get(k), mn.get(k)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        rel = (b - a) / abs(a) if a else 0.0
        changes.append({"metric": k, "old": a, "new": b, "rel_change": rel})
        if k == "max_drawdown":
            if a < 0 and b < a * (1 + threshold):
                issues.append(f"max drawdown deepened {a:.1%} -> {b:.1%}")
        elif rel < -threshold and a > 0:
            issues.append(f"{k} {a:.3g} -> {b:.3g} ({rel:+.0%})")
    so, sn = ro.get("series") or {}, rn.get("series") or {}
    live: dict[str, Any] | None = None
    if so.get("dates") and sn.get("dates") and sn.get("nav"):
        last_old = so["dates"][-1]
        nav_n = sn["nav"]
        try:
            k = sn["dates"].index(last_old)
        except ValueError:
            k = None
        if k is not None and k < len(nav_n) - 1:
            n_new = len(nav_n) - 1 - k
            seg = nav_n[-1] / nav_n[k] - 1.0
            vo = mo.get("vol")
            mu_d = (1 + (mo.get("cagr") or 0.0)) ** (1 / 252) - 1
            sd_d = (vo or 0.0) / math.sqrt(252)
            z = ((math.log1p(seg) - n_new * math.log1p(mu_d)) / (sd_d * math.sqrt(n_new))) if sd_d > 0 else None
            live = {"new_days": n_new, "return": seg, "z": z, "since": last_old}
            if z is not None and z < -2:
                issues.append(f"live decay: {seg:+.1%} over {n_new} new days (z {z:.1f} vs the backtest)")
        dd = sn.get("drawdown") or []
        if dd and isinstance(mo.get("max_drawdown"), (int, float)) and dd[-1] is not None and dd[-1] < mo["max_drawdown"]:
            issues.append(f"current drawdown {dd[-1]:.1%} beyond the previous worst {mo['max_drawdown']:.1%}")
    ho = {h["entity_id"]: h["weight"] for h in ro.get("holdings_last") or []}
    hn = {h["entity_id"]: h["weight"] for h in rn.get("holdings_last") or []}
    hold_to = 0.5 * sum(abs(hn.get(e, 0.0) - ho.get(e, 0.0)) for e in set(ho) | set(hn)) if (ho or hn) else None
    return {"degraded": bool(issues), "issues": issues, "metric_changes": changes, "live": live,
            "holdings_turnover": hold_to}


def run(ctx: Any) -> Any:
    from app.backtest import runs, store
    from app.backtest.spec import BacktestSpec
    from app.scheduler.base import JobResult

    s = ctx.settings
    done = failed = alerts = 0
    msgs: list[str] = []
    for st in store.due_strategies(datetime.now(UTC)):
        ctx.check_cancelled()
        try:
            spec = BacktestSpec.model_validate(st["spec"])
            prior = store.prior_trials(st["strategy_id"], spec.spec_hash())
            prep = runs.prepare(spec, "async", st["strategy_id"], s, prior=prior, scheduled=True)
            out = runs.execute(prep["run_id"], s, external_check=ctx.check_cancelled)
            store.upsert_run(out, spec.normalized())
            store.update_strategy(st["strategy_id"], {"last_scheduled_at": datetime.now(UTC)})
            if out["status"] != "completed":
                failed += 1
                msgs.append(f"{st['name']}: {out.get('error')}")
                continue
            done += 1
            prev = store.previous_completed_run(st["strategy_id"], prep["run_id"])
            if prev is None:
                continue
            drift = detect_drift(runs.read_result(prev["run_id"]) or {}, runs.read_result(prep["run_id"]) or {},
                                 float(st.get("drift_threshold") or 0.3))
            if drift["degraded"] and st.get("alert_id"):
                store.add_alert_event(st["alert_id"], f"Paper strategy '{st['name']}': " + "; ".join(drift["issues"]),
                                      {"strategy_id": st["strategy_id"], "run_id": prep["run_id"],
                                       "previous_run_id": prev["run_id"], **drift}, date.today())
                alerts += 1
        except Exception as e:  # noqa: BLE001 - one strategy must not stop the others
            failed += 1
            msgs.append(f"{st.get('name')}: {type(e).__name__}: {e}")
    return JobResult(status="partial" if failed else "success", entities_ok=done, entities_failed=failed,
                     message=f"{done} paper strategies re-run, {alerts} drift alerts" + (f"; failures: {msgs[:5]}" if msgs else ""),
                     retryable=False)
