"""Thesis evaluation against the latest serving snapshot (roadmap #8; job `thesis_eval`, nightly after alerts_eval).

* subject_info / defaults: subject label, price instrument (price_id), currency, benchmark and sector benchmark.
* kpi_values: KPI specs resolved with the formula engine (app.normalize.formula) over screener field ids of the
  serving `sec` table (security subjects: the security row; sector / country subjects: the median of the members),
  cross-asset latest signals (xa_latest) and macro heatmap cells (macro_cells, latest snapshot).
* evaluate_status (pure): automatic status on_track / at_risk / invalidated / unknown with reasons, from price vs
  stop / target / entry, invalidation conditions, KPI breaches (role confirm -> at risk, invalidate -> invalidated),
  composite-score deterioration since inception and the horizon / review dates.
* fire_events: thesis_* alert events (KPI breach transitions, status turning at_risk / invalidated, review due,
  dated catalysts, CFTC positioning extremes, events-v2 shocks) appended to alert_events under the thesis rules.

A KPI threshold describes the HEALTHY condition (e.g. gross_margin gte 0.4); a breach is the condition failing.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

from app.core.logsetup import get_logger

from . import store

log = get_logger("app.theses.evaluate")

OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "between": "between"}
SPECIAL_FIELDS = {"composite": "c_balanced", "rank": "rk_balanced"}
NEAR_STOP_FRAC = 1 / 3
SCORE_DROP = 15.0
DRAWDOWN_RISK = 0.10
REVIEW_GRACE_DAYS = 7
SHOCK_MIN_VOLUME = 20
WORLD = ("ETF:ACWI", "ETF:VT", "IDX:^GSPC")


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _d(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


# ------------------------------------------------------------------------------------------------ serving helpers


def _cols(db: Any, table: str) -> set[str]:
    key = (getattr(getattr(db, "snap", None), "run_id", ""), table)
    c = _COLS.get(key)
    if c is None:
        try:
            c = set(db.columns(table))
        except Exception:  # table not in this snapshot
            c = set()
        _COLS[key] = c
    return c


_COLS: dict[tuple[str, str], set[str]] = {}


def has_prices(db: Any, pid: str | None) -> bool:
    return bool(pid) and db.scalar("SELECT 1 FROM prices WHERE security_id = ? LIMIT 1", [pid]) is not None


def latest_bar(db: Any, pid: str | None, on: date | None = None) -> dict[str, Any] | None:
    """Last daily bar of pid on or before `on` (default: snapshot as_of)."""
    if not pid:
        return None
    return db.one("""SELECT date, close, adj_close, close_usd, currency FROM prices
                     WHERE security_id = ? AND date <= ? ORDER BY date DESC LIMIT 1""", [pid, on or db.as_of])


@lru_cache(maxsize=1)
def _taxonomy() -> dict[str, Any]:
    from app.api.routers._common import taxonomy  # noqa: PLC0415

    return taxonomy()


def _country_name(cc: str) -> str:
    from app.macro.countries import COUNTRY_META  # noqa: PLC0415

    if cc in COUNTRY_META:
        return COUNTRY_META[cc]["name"]
    try:
        from app.events_v2.taxonomy import country_name  # noqa: PLC0415

        return country_name(cc) or cc
    except Exception:  # noqa: BLE001
        return cc


def subject_info(db: Any, subject_type: str, subject_id: str) -> dict[str, Any] | None:
    """Defaults for a subject: {subject_id, subject_label, security_id, price_id, price_currency, benchmark_id,
    sector_benchmark_id, region, country, sector_id, futures_root}. None when the subject is unknown."""
    from app.api.serving import REGION_BENCHMARK  # noqa: PLC0415
    from app.nlp.common import SECTOR_ETF  # noqa: PLC0415

    out: dict[str, Any] = {"subject_id": subject_id, "security_id": None, "price_id": None, "price_currency": None,
                           "benchmark_id": "IDX:^GSPC", "sector_benchmark_id": None, "region": None, "country": None,
                           "sector_id": None, "futures_root": None, "subject_label": subject_id}
    if subject_type == "security":
        r = db.one("""SELECT security_id, ticker, name, region, country, sector_id, currency FROM sec
                      WHERE security_id = ? OR upper(ticker) = upper(?)
                      ORDER BY security_id = ? DESC, coalesce(is_primary_security, false) DESC,
                               market_cap_usd DESC NULLS LAST LIMIT 1""", [subject_id, subject_id, subject_id])
        if r is None:
            return None
        out.update({"subject_id": r["security_id"], "security_id": r["security_id"], "price_id": r["security_id"],
                    "price_currency": r["currency"], "region": r["region"], "country": r["country"],
                    "sector_id": r["sector_id"], "subject_label": f"{r['ticker']} {r['name'] or ''}".strip(),
                    "benchmark_id": REGION_BENCHMARK.get(r["region"] or "", "IDX:^GSPC"),
                    "sector_benchmark_id": SECTOR_ETF.get(r["sector_id"] or "")})
    elif subject_type == "sector":
        sid = subject_id.upper()
        name = _taxonomy()["sectors"].get(sid)
        if name is None:
            return None
        out.update({"subject_id": sid, "sector_id": sid, "subject_label": name, "price_id": SECTOR_ETF.get(sid)})
    elif subject_type == "country":
        from app.events_models.common import COUNTRY_INDEX  # noqa: PLC0415

        cc = subject_id.upper()
        if not (len(cc) == 2 and cc.isalpha()):
            return None
        out.update({"subject_id": cc, "country": cc, "subject_label": _country_name(cc),
                    "price_id": next((p for p in COUNTRY_INDEX.get(cc, []) if has_prices(db, p)), None),
                    "benchmark_id": next((p for p in WORLD if has_prices(db, p)), "IDX:^GSPC")})
    elif subject_type == "macro_theme":
        from .rules import parse_macro_subject  # noqa: PLC0415

        cc, theme = parse_macro_subject(subject_id)
        if not theme:
            return None
        out.update({"subject_id": f"{cc}:{theme}" if cc else theme, "country": cc,
                    "subject_label": theme.replace("_", " ").capitalize() + (f" ({cc})" if cc else "")})
    elif subject_type == "cross_asset":
        if "asset_id" not in _cols(db, "xa_assets"):
            return None
        r = db.one("SELECT asset_id, name, instrument_id, futures_root, asset_class FROM xa_assets WHERE asset_id = ?",
                   [subject_id])
        if r is None:
            return None
        out.update({"subject_id": r["asset_id"], "subject_label": r["name"] or r["asset_id"],
                    "price_id": r["instrument_id"], "futures_root": r["futures_root"]})
    else:
        return None
    if out["price_id"] and not out["price_currency"]:
        b = latest_bar(db, out["price_id"])
        out["price_currency"] = b["currency"] if b else None
        if b is None and subject_type != "security":
            out["price_id"] = out["price_id"] if has_prices(db, out["price_id"]) else None
    return out


# ------------------------------------------------------------------------------------------------ KPIs


def _field_column(fid: str) -> str | None:
    if fid in SPECIAL_FIELDS:
        return SPECIAL_FIELDS[fid]
    from app.api.routers._common import screener_fields  # noqa: PLC0415

    f = screener_fields().get(fid)
    if f is None or f["type"] not in ("number", "boolean"):
        return None
    return f["sql"]


def kpi_dependencies(k: dict[str, Any]) -> list[str]:
    from app.normalize.formula import dependencies  # noqa: PLC0415

    if k.get("kind") == "expr":
        return sorted(dependencies(k.get("expr") or ""))
    return [k.get("ref") or ""]


def validate_kpi(k: dict[str, Any]) -> str | None:
    """Static check of a KPI spec; returns an error message or None."""
    from app.normalize.formula import FormulaError, validate_expr  # noqa: PLC0415

    kind = k.get("kind")
    if k.get("op") not in OPS:
        return f"op must be one of {sorted(OPS)}"
    if _f(k.get("threshold")) is None:
        return "threshold (number) is required"
    if k.get("op") == "between" and _f(k.get("threshold_hi")) is None:
        return "threshold_hi is required for op between"
    if kind == "metric":
        if not k.get("ref") or _field_column(k["ref"]) is None:
            return f"unknown numeric metric {k.get('ref')!r} (screener field id, see GET /api/v1/screener/fields)"
    elif kind == "expr":
        try:
            validate_expr(k.get("expr") or "")
        except FormulaError as e:
            return f"invalid expression: {e}"
        bad = [x for x in kpi_dependencies(k) if _field_column(x) is None]
        if bad:
            return f"unknown metric(s) in expression: {bad}"
    elif kind == "xa":
        if not k.get("ref"):
            return "ref '<asset_id>:<signal>' or '<signal>' is required"
    elif kind == "macro":
        if not k.get("ref") or ":" not in k["ref"]:
            return "ref '<CC>:<indicator>' is required (macro heatmap cell)"
    else:
        return "kind must be metric, expr, xa or macro"
    return None


def check(value: float | None, op: str, thr: float | None, thr_hi: float | None = None) -> bool | None:
    if value is None or thr is None:
        return None
    if op == "gt":
        return value > thr
    if op == "gte":
        return value >= thr
    if op == "lt":
        return value < thr
    if op == "lte":
        return value <= thr
    if op == "between":
        return thr_hi is not None and thr <= value <= thr_hi
    return None


def _member_where(t: dict[str, Any]) -> tuple[str, list[Any]] | None:
    st = t["subject_type"]
    if st == "security":
        return "security_id = ?", [t.get("security_id") or t["subject_id"]]
    if st == "sector":
        return "sector_id = ? AND coalesce(is_primary_security, true)", [t["subject_id"]]
    if st == "country":
        return "country = ? AND coalesce(is_primary_security, true)", [t["subject_id"]]
    return None


def _metric_values(db: Any, t: dict[str, Any], fids: list[str]) -> tuple[dict[str, float | None], str]:
    """Values of screener fields for the subject (members' median for sector / country)."""
    have = _cols(db, "sec")
    cols = {f: _field_column(f) for f in fids}
    cols = {f: c for f, c in cols.items() if c and c in have}
    out: dict[str, float | None] = dict.fromkeys(fids)
    mw = _member_where(t)
    if not cols or mw is None:
        return out, "n/a"
    where, params = mw
    agg = "{}" if t["subject_type"] == "security" else "median({})"
    sel = ", ".join(agg.format(f"CAST({c} AS DOUBLE)") + f' AS "{f}"' for f, c in cols.items())
    r = db.one(f"SELECT {sel} FROM sec WHERE {where}", params) or {}
    return {f: _f(r.get(f)) for f in fids}, ("security" if t["subject_type"] == "security" else "median of members")


def kpi_value(db: Any, t: dict[str, Any], k: dict[str, Any]) -> tuple[float | None, str, str | None]:
    """(value, basis, error)."""
    err = validate_kpi(k)
    if err:
        return None, "n/a", err
    kind = k["kind"]
    try:
        if kind == "metric":
            vals, basis = _metric_values(db, t, [k["ref"]])
            return vals[k["ref"]], basis, None
        if kind == "expr":
            from app.normalize.formula import evaluate  # noqa: PLC0415

            deps = kpi_dependencies(k)
            vals, basis = _metric_values(db, t, deps)
            return _f(evaluate(k["expr"], vals)), basis, None
        if kind == "xa":
            ref = k["ref"]
            asset, col = ref.split(":", 1) if ":" in ref else (t["subject_id"], ref)
            if col not in _cols(db, "xa_latest"):
                return None, "n/a", f"unknown cross-asset signal {col!r}"
            r = db.one(f'SELECT CAST("{col}" AS DOUBLE) AS v FROM xa_latest WHERE asset_id = ?', [asset])
            return _f(r["v"]) if r else None, "cross-asset latest", None
        if kind == "macro":
            cc, ind = k["ref"].split(":", 1)
            r = db.one("""SELECT value FROM macro_cells WHERE country = ? AND indicator = ? AND as_of_date =
                              (SELECT max(as_of_date) FROM macro_cells WHERE as_of_date <= ?)""", [cc.upper(), ind, db.as_of])
            return _f(r["value"]) if r else None, "macro cell (latest)", None
    except Exception as e:  # noqa: BLE001 - a broken KPI must not stop the evaluation
        return None, "n/a", f"{type(e).__name__}: {e}"
    return None, "n/a", "unsupported"


def kpi_values(db: Any, t: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k in t.get("kpis") or []:
        v, basis, err = kpi_value(db, t, k)
        ok = check(v, k.get("op", "gte"), _f(k.get("threshold")), _f(k.get("threshold_hi")))
        out.append({"kpi_id": k["kpi_id"], "label": k.get("label") or k["kpi_id"], "kind": k.get("kind"),
                    "role": k.get("role") or "confirm", "op": k.get("op"), "threshold": _f(k.get("threshold")),
                    "threshold_hi": _f(k.get("threshold_hi")), "unit": k.get("unit"), "value": v, "ok": ok,
                    "basis": basis, "error": err})
    return out


# ------------------------------------------------------------------------------------------------ status


def _r(level: str, code: str, message: str) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message}


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:,.4g}"


def evaluate_status(t: dict[str, Any], price: float | None, kpis: list[dict[str, Any]], as_of: date,
                    score_now: float | None = None, score_ref: float | None = None,
                    price_date: date | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Pure status rules. Returns (auto_status, reasons)."""
    reasons: list[dict[str, Any]] = []
    d = t.get("direction") or "long"
    stop, target = _f(t.get("stop")), _f(t.get("target"))
    lo, hi = _f(t.get("entry_lo")), _f(t.get("entry_hi"))
    ref = _f(t.get("inception_price")) or ((lo + hi) / 2 if lo is not None and hi is not None else (lo or hi))
    if price is not None and d in ("long", "short"):
        sgn = 1 if d == "long" else -1
        at = f" ({price_date})" if price_date else ""
        if stop is not None and sgn * (price - stop) <= 0:
            reasons.append(_r("invalid", "stop_hit", f"price {_fmt(price)}{at} breached the stop {_fmt(stop)}"))
        elif stop is not None and ref is not None and sgn * (ref - stop) > 0 \
                and sgn * (price - stop) < NEAR_STOP_FRAC * sgn * (ref - stop):
            reasons.append(_r("risk", "near_stop", f"price {_fmt(price)} is within a third of the distance from entry "
                                                   f"{_fmt(ref)} to the stop {_fmt(stop)}"))
        if target is not None and sgn * (price - target) >= 0:
            reasons.append(_r("info", "target_reached", f"price {_fmt(price)} reached the target {_fmt(target)} - "
                                                        "consider closing or raising the target"))
        if ref:
            move = sgn * (price / ref - 1)
            if stop is None and move <= -DRAWDOWN_RISK:
                reasons.append(_r("risk", "adverse_move", f"price moved {move:+.1%} against the thesis since entry "
                                                          f"{_fmt(ref)} (no stop set)"))
            elif abs(move) < 0.0005:
                reasons.append(_r("ok", "price_unchanged", f"price unchanged since entry {_fmt(ref)}"))
            elif move > 0:
                reasons.append(_r("ok", "price_in_favour", f"price {move:+.1%} in favour since entry {_fmt(ref)}"))
            else:
                reasons.append(_r("info", "price_against", f"price {move:+.1%} against the thesis since entry {_fmt(ref)}"))
        if lo is not None and hi is not None and lo <= price <= hi and t.get("status") == "idea":
            reasons.append(_r("info", "in_entry_band", f"price {_fmt(price)} is inside the entry band "
                                                       f"{_fmt(lo)}-{_fmt(hi)}"))
    by_id = {k["kpi_id"]: k for k in kpis}
    for k in kpis:
        if k.get("error"):
            reasons.append(_r("info", f"kpi_error:{k['kpi_id']}", f"KPI {k['label']}: {k['error']}"))
            continue
        if k["ok"] is None:
            reasons.append(_r("info", f"kpi_missing:{k['kpi_id']}", f"KPI {k['label']}: no data"))
            continue
        cond = f"{_fmt(k['value'])} vs {OPS.get(k['op'], k['op'])} {_fmt(k['threshold'])}" + (
            f"-{_fmt(k['threshold_hi'])}" if k["op"] == "between" else "")
        if k["ok"]:
            reasons.append(_r("ok", f"kpi_ok:{k['kpi_id']}", f"KPI {k['label']} on track ({cond})"))
        else:
            lvl = {"invalidate": "invalid", "confirm": "risk"}.get(k["role"], "info")
            reasons.append(_r(lvl, f"kpi_breach:{k['kpi_id']}", f"KPI {k['label']} breached ({cond})"))
    for c in t.get("invalidation") or []:
        kind, label = c.get("kind"), c.get("label") or c.get("kind")
        lvl_ = _f(c.get("level"))
        hit = False
        if kind == "price_below" and price is not None and lvl_ is not None:
            hit = price < lvl_
        elif kind == "price_above" and price is not None and lvl_ is not None:
            hit = price > lvl_
        elif kind == "kpi" and c.get("kpi_id") in by_id:
            hit = by_id[c["kpi_id"]]["ok"] is False
        elif kind == "manual":
            hit = bool(c.get("triggered"))
        elif kind == "date" and _d(c.get("date")) and as_of > _d(c.get("date")) and not c.get("triggered"):  # type: ignore[operator]
            reasons.append(_r("risk", f"deadline:{c.get('condition_id')}", f"deadline passed: {label} ({c.get('date')}) - "
                                                                        "confirm or mark it"))
        if hit:
            reasons.append(_r("invalid", f"invalidation:{c.get('condition_id')}", f"invalidation condition met: {label}"))
    if score_now is not None and score_ref is not None:
        delta = score_now - score_ref
        if (d != "short" and delta <= -SCORE_DROP) or (d == "short" and delta >= SCORE_DROP):
            reasons.append(_r("risk", "score_change", f"composite score {score_ref:.0f} -> {score_now:.0f} "
                                                      f"({delta:+.0f}) since inception"))
    hz = _d(t.get("horizon_date"))
    if hz and as_of > hz and t.get("status") == "active":
        reasons.append(_r("risk", "horizon_passed", f"horizon ended {hz} - review the time stop"))
    nr = _d(t.get("next_review_date"))
    if nr and as_of > nr + timedelta(days=REVIEW_GRACE_DAYS) and t.get("status") != "closed":
        reasons.append(_r("info", "review_overdue", f"review overdue since {nr}"))
    levels = {r["level"] for r in reasons}
    if "invalid" in levels:
        st = "invalidated"
    elif "risk" in levels:
        st = "at_risk"
    elif price is None and not any(k["ok"] is not None for k in kpis):
        st = "unknown"
        reasons.append(_r("info", "no_data", "no price instrument and no KPI values to evaluate"))
    else:
        st = "on_track"
    return st, reasons


# ------------------------------------------------------------------------------------------------ evaluation


def _score(db: Any, t: dict[str, Any]) -> tuple[float | None, str | None]:
    if t["subject_type"] != "security" or not t.get("security_id"):
        return None, None
    have = _cols(db, "sec")
    if "c_balanced" not in have:
        return None, None
    r = db.one(f"SELECT c_balanced{', g_balanced' if 'g_balanced' in have else ''} FROM sec WHERE security_id = ?",
               [t["security_id"]]) or {}
    return _f(r.get("c_balanced")), r.get("g_balanced")


def _inception_score(t: dict[str, Any]) -> float | None:
    snaps = store.list_snapshots(t["thesis_id"])
    for kind in ("activation", "creation"):
        for s in snaps:
            if s["kind"] == kind and s.get("composite_score") is not None:
                return s["composite_score"]
    return None


def evaluate(db: Any, t: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    """Live evaluation (no writes): {auto_status, reasons, kpis, price, price_date, score, grade, as_of}."""
    as_of = as_of or db.as_of
    bar = latest_bar(db, t.get("price_id"))
    price = _f(bar["close"]) if bar else None
    kpis = kpi_values(db, t)
    score, grade = _score(db, t)
    st, reasons = evaluate_status(t, price, kpis, as_of, score, _inception_score(t) if score is not None else None,
                                  _d(bar["date"]) if bar else None)
    return {"auto_status": st, "reasons": reasons, "kpis": kpis, "price": price,
            "price_date": _d(bar["date"]) if bar else None, "price_currency": bar["currency"] if bar else None,
            "score": score, "grade": grade, "as_of": as_of}


def capture(db: Any, t: dict[str, Any], kind: str, ev: dict[str, Any] | None = None,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accountability snapshot (price, benchmarks, score, KPI values) -> thesis_snapshots row."""
    ev = ev or evaluate(db, t)
    bar = latest_bar(db, t.get("price_id"))
    bb, sb = latest_bar(db, t.get("benchmark_id")), latest_bar(db, t.get("sector_benchmark_id"))
    vals = {"as_of": db.as_of, "price_date": bar["date"] if bar else None, "price": _f(bar["close"]) if bar else None,
            "price_usd": _f(bar["close_usd"]) if bar else None,
            "benchmark_price": _f(bb["adj_close"]) if bb else None, "sector_price": _f(sb["adj_close"]) if sb else None,
            "composite_score": ev.get("score"), "grade": ev.get("grade"),
            "kpi_values": [{k: x.get(k) for k in ("kpi_id", "label", "value", "ok", "threshold", "op")}
                           for x in ev["kpis"]],
            "auto_status": ev["auto_status"],
            "extra": {"serving_run_id": getattr(getattr(db, "snap", None), "run_id", None),
                      "reasons": ev["reasons"], **(extra or {})}}
    return store.add_snapshot(t["thesis_id"], kind, vals)


def _codes(reasons: list[dict[str, Any]] | None) -> set[str]:
    return {r.get("code", "") for r in reasons or []}


def fire_events(db: Any, t: dict[str, Any], ev: dict[str, Any], as_of: date) -> int:
    """Append thesis_* alert events for this evaluation (idempotent per alert x target x data date)."""
    tid = t["thesis_id"]
    rules = [a for a in store.thesis_alerts(tid) if a["enabled"] and a["rule_type"].startswith("thesis_")]
    if not rules:
        return 0
    prev_codes, prev_status = _codes(t.get("status_reasons")), t.get("auto_status")
    title = t.get("title") or tid
    n = 0

    def emit(a: dict[str, Any], d: date, msg: str, payload: dict[str, Any]) -> None:
        nonlocal n
        tgt = a.get("security_id") or f"thesis:{tid}"
        if store.add_alert_event(a["alert_id"], tgt, d, f"[{title}] {msg}",
                                 {"rule_type": a["rule_type"], "thesis_id": tid, **payload}):
            n += 1

    for a in rules:
        rt, p = a["rule_type"], a["params"]
        try:
            if rt == "thesis_kpi":
                k = next((x for x in ev["kpis"] if x["kpi_id"] == p.get("kpi_id")), None)
                if k and k["ok"] is False and f"kpi_breach:{k['kpi_id']}" not in prev_codes:
                    emit(a, as_of, f"KPI {k['label']} breached: {_fmt(k['value'])} vs {OPS.get(k['op'])} "
                                   f"{_fmt(k['threshold'])}", {"kpi_id": k["kpi_id"], "value": k["value"],
                                                               "threshold": k["threshold"], "role": k["role"]})
            elif rt == "thesis_status":
                if ev["auto_status"] in ("at_risk", "invalidated") and ev["auto_status"] != prev_status:
                    why = "; ".join(r["message"] for r in ev["reasons"] if r["level"] in ("risk", "invalid"))[:400]
                    emit(a, as_of, f"status {prev_status or 'new'} -> {ev['auto_status']}: {why}",
                         {"from": prev_status, "to": ev["auto_status"]})
            elif rt == "thesis_review":
                nr = _d(t.get("next_review_date"))
                if nr and nr <= as_of and t.get("status") != "closed":
                    emit(a, nr, f"review due {nr}", {"due_date": str(nr)})
            elif rt == "thesis_catalyst":
                days = int(p.get("days_ahead", 3))
                for c in t.get("catalysts") or []:
                    cd = _d(c.get("date"))
                    if cd and c.get("kind") != "earnings" and not c.get("done") and as_of <= cd <= as_of + timedelta(days=days):
                        emit(a, cd, f"catalyst {cd}: {c.get('label')}", {"catalyst_id": c.get("catalyst_id"),
                                                                        "date": str(cd)})
            elif rt == "thesis_positioning":
                if {"cot_index_3y", "cot_report_date"} <= _cols(db, "xa_latest"):
                    r = db.one("SELECT cot_index_3y, cot_report_date, cot_category FROM xa_latest WHERE asset_id = ?",
                               [p.get("asset_id")])
                    ci = _f(r["cot_index_3y"]) if r else None
                    if ci is not None and (ci <= float(p.get("lo", 0.1)) or ci >= float(p.get("hi", 0.9))):
                        side = "crowded long" if ci >= float(p.get("hi", 0.9)) else "crowded short"
                        emit(a, _d(r["cot_report_date"]) or as_of,
                             f"CFTC positioning extreme on {p.get('asset_id')}: COT index 3y {ci:.0%} ({side}, "
                             f"{r['cot_category']}; official CFTC data)", {"cot_index_3y": ci, "side": side})
            elif rt == "thesis_event_shock":
                for s in event_shocks(db, p.get("label_type", ""), p.get("label_id", ""), as_of,
                                      float(p.get("threshold_z", 3.0)), days=3):
                    emit(a, s["date"], f"event shock ({p.get('label_type')} {p.get('label_id')}): attention z "
                                       f"{_fmt(s['attention_z'])}, tone z {_fmt(s['tone_z'])} on {s['n']} items "
                                       "(GDELT, model-based z)", {k: v for k, v in s.items() if k != "date"})
        except Exception as e:  # noqa: BLE001 - one rule must not stop the others
            log.warning("thesis %s rule %s failed: %s", tid, rt, e)
    return n


def _ev2_label(db: Any, label_type: str, label_id: str) -> int | None:
    if not _cols(db, "ev2_dict"):
        return None
    if label_type == "issuer":
        r = db.one("""SELECT iid FROM ev2_issuer WHERE security_id = ? OR issuer_id = (SELECT issuer_id FROM sec
                      WHERE security_id = ? LIMIT 1) LIMIT 1""", [label_id, label_id])
        return int(r["iid"]) if r else None
    r = db.one("SELECT id FROM ev2_dict WHERE dict_type = ? AND upper(code) = upper(?)", [label_type, label_id])
    return int(r["id"]) if r else None


def event_shocks(db: Any, label_type: str, label_id: str, as_of: date, threshold_z: float = 3.0,
                 days: int = 3, start: date | None = None) -> list[dict[str, Any]]:
    """events-v2 T1 rows (all themes) with attention z >= threshold or |tone z| >= threshold in (as_of - days, as_of]."""
    li = _ev2_label(db, label_type, label_id)
    if li is None or "attention_z" not in _cols(db, "ev2_agg"):
        return []
    lo = start or (as_of - timedelta(days=days))
    rows = db.all("""SELECT date, attention_z, tone_z, art_n + ev_n AS n, coalesce(art_tone, ev_tone) AS tone
                     FROM ev2_agg WHERE label_type = ? AND label_id = ? AND theme = -1 AND date > ? AND date <= ?
                       AND (attention_z >= ? OR abs(tone_z) >= ?) AND art_n + ev_n >= ?
                     ORDER BY date""", [label_type, li, lo, as_of, threshold_z, threshold_z,
                                        5 if label_type == "issuer" else SHOCK_MIN_VOLUME])
    return [{"date": _d(r["date"]), "attention_z": _f(r["attention_z"]), "tone_z": _f(r["tone_z"]), "n": int(r["n"]),
             "tone": _f(r["tone"])} for r in rows]


def evaluate_and_store(db: Any, t: dict[str, Any], as_of: date | None = None, fire: bool = True) -> dict[str, Any]:
    as_of = as_of or db.as_of
    ev = evaluate(db, t, as_of)
    n = fire_events(db, t, ev, as_of) if fire and t.get("status") != "closed" else 0
    store.set_evaluation(t["thesis_id"], ev["auto_status"], ev["reasons"], datetime.now(UTC))
    ev["events_fired"] = n
    return ev


def run(ctx: Any) -> Any:
    """jobs.yaml `thesis_eval` (nightly after alerts_eval): evaluate every open thesis on the current snapshot."""
    from app.api.deps import Db, current_snapshot  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    db = Db(current_snapshot())
    ok = failed = fired = 0
    msgs = []
    try:
        for t in store.list_theses():
            if t["status"] == "closed":
                continue
            ctx.check_cancelled()
            try:
                ev = evaluate_and_store(db, t, db.as_of, fire=True)
                fired += ev["events_fired"]
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                msgs.append(f"{t['thesis_id']}: {type(e).__name__}: {e}")
    finally:
        db.close()
    msg = f"{ok} theses evaluated, {fired} thesis alert events" + (f"; failures: {msgs[:5]}" if msgs else "")
    ctx.log.info(msg)
    return JobResult(status="partial" if failed else "success", entities_ok=ok, entities_failed=failed,
                     rows_written={"alert_events": fired}, message=msg, retryable=False)


def dumps(v: Any) -> str:
    return json.dumps(v, default=str)
