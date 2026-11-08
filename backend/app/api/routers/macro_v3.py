"""Macro v3 / Wave 3 endpoints (tag: macro-v3; owner macro-w3): central-bank monitor ("what's priced in").

Official inputs only (central banks, Treasuries, NY Fed, BIS, OECD, FRED/ALFRED). Market-implied paths are
APPROXIMATIONS from official curves (method + caveats returned with every path); Taylor rules, expected inflation
(except the Cleveland Fed series) and communication tone are MODEL-BASED and labelled as such. Data comes from the
serving snapshot tables built by app.api.serving._Builder.macro_v3 (cb_* tables, written by app.macro.cb jobs).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import iso, jload, not_found
from app.macro.cb.banks import BANKS, get_bank

router = APIRouter(tags=["macro-v3"], route_class=FastRoute)

IMPLIED_LABEL = "Approximation from official curves (not futures/OIS-implied probabilities)"
MODEL_LABEL = "Model-based"
LABELS = {
    "implied_path": IMPLIED_LABEL,
    "implied_path_tp_adjusted": "Term-premium-adjusted with NY Fed ACM term premia (model-based, official NY Fed estimates)",
    "taylor_rule": "Model-based (Taylor rule; official inputs)",
    "expected_inflation": "Model-based (Cleveland Fed model for the US; our AR(1) elsewhere)",
    "tone": "Model-based (local RoBERTa hawkish/dovish classifier + lexicon)",
    "rate_cycle": "Rule-based: most recent policy-rate change within 183 days decides hiking/cutting, else pausing",
    "decisions": "Derived from official rate changes and official meeting dates",
}
TABLES = ("cb_policy_rates", "cb_decisions", "cb_meetings", "cb_rate_cycle", "cb_implied_paths", "cb_implied_meta",
          "cb_curves", "cb_projections", "cb_stance", "cb_balance_sheets", "cb_tone")


def _has(db: Db, table: str) -> bool:
    return bool(db.scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]))


def _nonempty(db: Db, table: str) -> bool:
    return _has(db, table) and bool(db.scalar(f'SELECT count(*) > 0 FROM "{table}"'))


def _status(db: Db, *tables: str) -> str:
    have = [t for t in tables if _nonempty(db, t)]
    return "ok" if len(have) == len(tables) else ("partial" if have else "pending")


def _r(v: Any, nd: int = 4) -> Any:
    return round(float(v), nd) if isinstance(v, (int, float)) and v is not None else v


def _csv(v: list[str] | None) -> list[str]:
    out: list[str] = []
    for x in v or []:
        out += [p.strip().lower() for p in x.split(",") if p.strip()]
    return out


# =============================================================================================== loaders

def _latest_rates(db: Db) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_policy_rates"):
        return {}
    rows = db.all("""SELECT * FROM cb_policy_rates QUALIFY row_number() OVER (PARTITION BY bank ORDER BY date DESC) = 1""")
    return {r["bank"]: r for r in rows}


def _moves(db: Db) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_decisions"):
        return {}
    rows = db.all("""SELECT * FROM cb_decisions WHERE action <> 'hold'
                     QUALIFY row_number() OVER (PARTITION BY bank ORDER BY decision_date DESC) = 1""")
    return {r["bank"]: r for r in rows}


def _next_meetings(db: Db, today: date) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_meetings"):
        return {}
    rows = db.all("""SELECT * FROM cb_meetings WHERE meeting_date > ?
                     QUALIFY row_number() OVER (PARTITION BY bank ORDER BY meeting_date) = 1""", [today])
    return {r["bank"]: r for r in rows}


def _phase(db: Db, bank: str | None = None) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_rate_cycle"):
        return {}
    w = "WHERE bank = ?" if bank else ""
    rows = db.all(f"""WITH x AS (SELECT bank, date, phase, last_change_date, last_change_bp,
                                       phase IS DISTINCT FROM lag(phase) OVER (PARTITION BY bank ORDER BY date) AS chg
                                FROM cb_rate_cycle {w})
                     SELECT bank, arg_max(phase, date) AS phase, max(date) AS date,
                            arg_max(last_change_date, date) AS last_change_date,
                            max(date) FILTER (WHERE chg) AS phase_since
                     FROM x GROUP BY bank""", [bank] if bank else [])
    return {r["bank"]: r for r in rows}


def _implied_latest(db: Db, banks: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    if not _has(db, "cb_implied_paths"):
        return {}
    w = f" AND bank IN ({', '.join('?' for _ in banks)})" if banks else ""
    rows = db.all(f"""SELECT p.* FROM cb_implied_paths p
                      JOIN (SELECT bank AS b, max(as_of) AS m FROM cb_implied_paths GROUP BY bank) l
                        ON p.bank = l.b AND p.as_of = l.m
                      WHERE TRUE {w} ORDER BY bank, target_date, point_type""", list(banks or []))
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r["bank"]].append(r)
    return out


def _implied_meta(db: Db) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_implied_meta"):
        return {}
    return {r["bank"]: r for r in db.all("SELECT * FROM cb_implied_meta")}


def _point(r: dict[str, Any]) -> dict[str, Any]:
    return {"point_type": r["point_type"], "label": r["label"], "target_date": iso(r["target_date"]),
            "implied_rate": _r(r["implied_rate"]), "implied_rate_tp_adj": _r(r.get("implied_rate_tp_adj")),
            "change_bp": _r(r["change_bp"], 2), "moves_priced": _r(r["moves_priced"], 3),
            "incremental_bp": _r(r.get("incremental_bp"), 2), "change_1w_bp": _r(r.get("change_1w_bp"), 2),
            "change_1m_bp": _r(r.get("change_1m_bp"), 2)}


def _priced_summary(points: list[dict[str, Any]], step_bp: float) -> dict[str, Any]:
    meets = [p for p in points if p["point_type"] == "meeting"]
    hz = {p["label"]: p for p in points if p["point_type"] == "horizon"}
    nxt = meets[0] if meets else None
    h12 = hz.get("12m")

    def brief(p: dict[str, Any] | None) -> dict[str, Any] | None:
        if not p:
            return None
        return {"label": p["label"], "target_date": iso(p["target_date"]), "implied_rate": _r(p["implied_rate"]),
                "change_bp": _r(p["change_bp"], 2), "moves_priced": _r(p["moves_priced"], 3),
                "change_1w_bp": _r(p.get("change_1w_bp"), 2), "change_1m_bp": _r(p.get("change_1m_bp"), 2)}
    direction = None
    if h12 is not None:
        c = h12["change_bp"]
        direction = "hikes" if c >= step_bp / 2 else ("cuts" if c <= -step_bp / 2 else "on hold")
    return {"next_meeting": brief(nxt), "h12m": brief(h12), "direction_12m": direction,
            "as_of": iso(points[0]["as_of"]) if points else None}


def _stance_latest(db: Db, bank: str | None = None) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_stance"):
        return {}
    w = " AND bank = ?" if bank else ""
    rows = db.all(f"""SELECT * FROM cb_stance WHERE (cpi_yoy IS NOT NULL OR taylor_1993 IS NOT NULL) {w}
                     QUALIFY row_number() OVER (PARTITION BY bank ORDER BY date DESC) = 1""", [bank] if bank else [])
    return {r["bank"]: r for r in rows}


def _tone_latest(db: Db) -> dict[str, dict[str, Any]]:
    if not _has(db, "cb_tone"):
        return {}
    rows = db.all("SELECT * FROM cb_tone QUALIFY row_number() OVER (PARTITION BY bank ORDER BY date DESC) = 1")
    return {r["bank"]: r for r in rows}


def _tone_trends(db: Db, n: int = 12) -> dict[str, list[dict[str, Any]]]:
    if not _has(db, "cb_tone"):
        return {}
    rows = db.all(f"""SELECT t.bank, t.date, t.tone_model, t.tone_lexicon, d.action FROM cb_tone t
                      LEFT JOIN cb_decisions d ON d.bank = t.bank AND d.decision_date = t.date
                      QUALIFY row_number() OVER (PARTITION BY t.bank ORDER BY t.date DESC) <= {int(n)}
                      ORDER BY t.bank, t.date""")
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r["bank"]].append({"date": iso(r["date"]), "tone_model": _r(r["tone_model"]),
                               "tone_lexicon": _r(r["tone_lexicon"]), "action": r["action"]})
    return out


def _stance_out(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if not r:
        return None
    keys = ("policy_rate", "cpi_yoy", "core_yoy", "infl_taylor", "expected_infl", "real_rate_cpi", "real_rate_core",
            "real_rate_expected", "output_gap", "r_star", "inflation_target", "taylor_1993", "taylor_rstar",
            "taylor_balanced")
    out = {"date": iso(r["date"]), **{k: _r(r.get(k), 3) for k in keys},
           "infl_taylor_measure": r.get("infl_taylor_measure"), "expected_infl_source": r.get("expected_infl_source"),
           "output_gap_source": r.get("output_gap_source"), "r_star_source": r.get("r_star_source")}
    tr = r.get("taylor_rstar") if r.get("taylor_rstar") is not None else r.get("taylor_1993")
    out["policy_minus_taylor_bp"] = _r((r["policy_rate"] - tr) * 100, 1) if tr is not None else None
    out["labels"] = {"taylor": LABELS["taylor_rule"], "expected_infl": LABELS["expected_inflation"]}
    return out


def _tone_out(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if not r:
        return None
    return {"date": iso(r["date"]), "doc_type": r["doc_type"], "url": r["url"], "tone_model": _r(r.get("tone_model")),
            "tone_lexicon": _r(r.get("tone_lexicon")), "hawkish_share": _r(r.get("hawkish_share")),
            "dovish_share": _r(r.get("dovish_share")), "n_sentences": r.get("n_sentences"),
            "vote_text": r.get("vote_text"), "dissents": r.get("dissents"), "model_id": r.get("model_id"),
            "label": LABELS["tone"]}


def _corridor(r: dict[str, Any] | None) -> dict[str, Any]:
    keys = ("lower", "upper", "deposit", "lending", "mro", "overnight", "bis_rate")
    return {k: _r((r or {}).get(k)) for k in keys}


# =============================================================================================== endpoints

@router.get("/macro/v3/central-banks", operation_id="getMacroV3CentralBanks")
def list_central_banks(db: Db = Depends(get_db)) -> dict[str, Any]:
    today = date.today()
    rates, moves, nxt, ph = _latest_rates(db), _moves(db), _next_meetings(db, today), _phase(db)
    imp, meta = _implied_latest(db), _implied_meta(db)
    st, tn, tt = _stance_latest(db), _tone_latest(db), _tone_trends(db)
    out = []
    for b in BANKS:
        r = rates.get(b.code)
        mv, nm, p, m = moves.get(b.code), nxt.get(b.code), ph.get(b.code), meta.get(b.code)
        pts = imp.get(b.code, [])
        out.append({
            **b.to_dict(),
            "policy_rate": _r(r["policy_rate"]) if r else None, "rate_as_of": iso(r.get("obs_date")) if r else None,
            "rate_source": r["source"] if r else None, "corridor": _corridor(r),
            "last_move": {"decision_date": iso(mv["decision_date"]), "change_bp": _r(mv["change_bp"], 2),
                          "action": mv["action"], "rate_after": _r(mv["rate_after"])} if mv else None,
            "next_meeting": {"date": iso(nm["meeting_date"]), "scheduled_at": iso(nm["scheduled_at"]),
                             "time_known": nm["time_known"], "source_id": nm["source_id"]} if nm else None,
            "phase": p["phase"] if p else None, "phase_since": iso(p["phase_since"]) if p else None,
            "priced": {**_priced_summary(pts, b.step_bp), "status": m["status"] if m else "pending",
                       "source": m["source"] if m else None, "label": IMPLIED_LABEL},
            "stance": _stance_out(st.get(b.code)), "tone": _tone_out(tn.get(b.code)),
            "tone_trend": tt.get(b.code, [])})
    return {"as_of": iso(db.as_of), "labels": LABELS, "banks": out,
            "data_status": _status(db, "cb_policy_rates", "cb_decisions", "cb_rate_cycle")}


@router.get("/macro/v3/implied-paths", operation_id="getMacroV3ImpliedPaths")
def implied_paths(banks: list[str] | None = Query(None, description="Bank codes (comma-separated or repeated)"),
                  db: Db = Depends(get_db)) -> dict[str, Any]:
    want = _csv(banks)
    imp, meta, rates, nxt = _implied_latest(db, want or None), _implied_meta(db), _latest_rates(db), _next_meetings(db, date.today())
    out = []
    for b in BANKS:
        if want and b.code not in want:
            continue
        m, pts = meta.get(b.code), imp.get(b.code, [])
        nm = nxt.get(b.code)
        out.append({"bank": b.code, "name": b.name, "country": b.country, "currency": b.currency,
                    "status": m["status"] if m else "pending", "as_of": iso(m["as_of"]) if m else None,
                    "policy_rate": _r(pts[0]["policy_rate"]) if pts else (_r(rates[b.code]["policy_rate"]) if b.code in rates else None),
                    "step_bp": b.step_bp, "next_meeting": iso(nm["meeting_date"]) if nm else None,
                    "source": m["source"] if m else None, "method": m["method"] if m else None,
                    "caveats": jload(m["caveats"], []) if m else [], "meetings_resolved": bool(m["meetings_resolved"]) if m else False,
                    "tp_adjusted": bool(m["tp_adjusted"]) if m else False,
                    "basis_bp": _r(pts[0]["basis_bp"], 2) if pts else None, "anchor": pts[0]["anchor"] if pts else None,
                    "summary": _priced_summary(pts, b.step_bp), "points": [_point(p) for p in pts]})
    return {"as_of": iso(db.as_of), "label": IMPLIED_LABEL, "labels": LABELS, "banks": out,
            "data_status": _status(db, "cb_implied_paths", "cb_implied_meta")}


@router.get("/macro/v3/central-banks/{bank}", operation_id="getMacroV3CentralBank")
def central_bank(bank: str, history_years: int = Query(10, ge=1, le=40),
                 implied_history_days: int = Query(365, ge=0, le=760), db: Db = Depends(get_db)) -> dict[str, Any]:
    b = get_bank(bank)
    if b is None:
        raise not_found(f"unknown central bank '{bank}' (one of {', '.join(x.code for x in BANKS)})")
    code = b.code
    today = date.today()
    since = today - timedelta(days=int(365.25 * history_years))
    out: dict[str, Any] = {"as_of": iso(db.as_of), "bank": b.to_dict(), "labels": LABELS}

    # ---- rates (weekly sampled history: last business day of each ISO week + the latest day)
    rates: dict[str, Any] = {"latest": None, "history": []}
    if _has(db, "cb_policy_rates"):
        lat = db.one("SELECT * FROM cb_policy_rates WHERE bank = ? ORDER BY date DESC LIMIT 1", [code])
        if lat:
            rates["latest"] = {"date": iso(lat["date"]), "observed": iso(lat.get("obs_date")),
                               "policy_rate": _r(lat["policy_rate"]), "source": lat["source"],
                               **_corridor(lat)}
        hist = db.all("""SELECT date, policy_rate, lower, upper, deposit, lending, mro, overnight, bis_rate
                         FROM cb_policy_rates WHERE bank = ? AND date >= ?
                         QUALIFY row_number() OVER (PARTITION BY date_trunc('week', date) ORDER BY date DESC) = 1
                         ORDER BY date""", [code, since])
        rates["history"] = [{"date": iso(h["date"]), "policy_rate": _r(h["policy_rate"]),
                             **{k: _r(h[k]) for k in ("lower", "upper", "deposit", "lending", "mro", "overnight", "bis_rate")}}
                            for h in hist]
    out["rates"] = rates

    # ---- decisions + meetings + phase
    votes = {}
    if _has(db, "cb_tone"):
        votes = {r["date"]: r for r in db.all("SELECT date, vote_text, dissents, url FROM cb_tone WHERE bank = ?", [code])}
    decs = []
    if _has(db, "cb_decisions"):
        for d in db.all("SELECT * FROM cb_decisions WHERE bank = ? AND decision_date >= ? ORDER BY decision_date DESC",
                        [code, since]):
            v = votes.get(d["decision_date"])
            vote, vsrc = d.get("vote"), d.get("vote_source")
            if not vote and v and v.get("vote_text"):
                vote, vsrc = v["vote_text"], f"official statement ({v['url']})"
            decs.append({"decision_date": iso(d["decision_date"]), "effective_date": iso(d["effective_date"]),
                         "rate_before": _r(d["rate_before"]), "rate_after": _r(d["rate_after"]),
                         "change_bp": _r(d["change_bp"], 2), "action": d["action"], "scheduled": d["scheduled"],
                         "date_basis": d["date_basis"], "vote": vote, "vote_source": vsrc,
                         "vote_detail": jload(d.get("vote_detail"), None)})
    out["decisions"] = decs
    meetings: dict[str, Any] = {"upcoming": [], "past": []}
    if _has(db, "cb_meetings"):
        for m in db.all("SELECT * FROM cb_meetings WHERE bank = ? AND meeting_date >= ? ORDER BY meeting_date", [code, since]):
            row = {"date": iso(m["meeting_date"]), "scheduled_at": iso(m["scheduled_at"]), "time_known": m["time_known"],
                   "source_id": m["source_id"], "name": m["name"], "url": m["url"], "sep": m["sep"]}
            meetings["upcoming" if m["meeting_date"] > today else "past"].append(row)
        meetings["past"] = meetings["past"][-24:]
    out["meetings"] = meetings
    ph = _phase(db, code).get(code)
    cycle_hist = []
    if _has(db, "cb_rate_cycle"):
        cycle_hist = db.all("""SELECT min(date) AS start, max(date) AS end, phase FROM (
                                 SELECT date, phase, sum(chg) OVER (ORDER BY date) AS grp FROM (
                                   SELECT date, phase, CASE WHEN phase IS DISTINCT FROM lag(phase) OVER (ORDER BY date)
                                                            THEN 1 ELSE 0 END AS chg
                                   FROM cb_rate_cycle WHERE bank = ? AND date >= ?))
                               GROUP BY grp, phase ORDER BY start""", [code, since])
    out["rate_cycle"] = {"phase": ph["phase"] if ph else None, "phase_since": iso(ph["phase_since"]) if ph else None,
                         "method": LABELS["rate_cycle"],
                         "segments": [{"start": iso(c["start"]), "end": iso(c["end"]), "phase": c["phase"]} for c in cycle_hist]}

    # ---- implied path
    meta = _implied_meta(db).get(code)
    pts = _implied_latest(db, [code]).get(code, [])
    ih = []
    if pts and implied_history_days and _has(db, "cb_implied_paths"):
        ih = db.all("""SELECT as_of, label, implied_rate, implied_rate_tp_adj, change_bp FROM cb_implied_paths
                       WHERE bank = ? AND point_type = 'horizon' AND as_of >= ? ORDER BY as_of, label""",
                    [code, today - timedelta(days=implied_history_days)])
    hist_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for h in ih:
        hist_by[h["label"]].append({"as_of": iso(h["as_of"]), "implied_rate": _r(h["implied_rate"]),
                                    "implied_rate_tp_adj": _r(h["implied_rate_tp_adj"]), "change_bp": _r(h["change_bp"], 2)})
    curves: dict[str, list[dict[str, Any]]] = defaultdict(list)
    curve_dates: dict[str, Any] = {}
    if _has(db, "cb_curves"):
        for c in db.all("SELECT * FROM cb_curves WHERE bank = ? ORDER BY snapshot, tenor_years", [code]):
            curves[c["snapshot"]].append({"tenor_years": _r(c["tenor_years"]), "zero_rate": _r(c["zero_rate"]),
                                          "forward_rate": _r(c["forward_rate"])})
            curve_dates[c["snapshot"]] = iso(c["as_of"])
    out["implied_path"] = {
        "status": meta["status"] if meta else "pending", "as_of": iso(meta["as_of"]) if meta else None,
        "label": IMPLIED_LABEL, "source": meta["source"] if meta else None, "method": meta["method"] if meta else None,
        "caveats": jload(meta["caveats"], []) if meta else [], "meetings_resolved": bool(meta["meetings_resolved"]) if meta else False,
        "tp_adjusted": bool(meta["tp_adjusted"]) if meta else False,
        "policy_rate": _r(pts[0]["policy_rate"]) if pts else None, "basis_bp": _r(pts[0]["basis_bp"], 2) if pts else None,
        "anchor": pts[0]["anchor"] if pts else None, "summary": _priced_summary(pts, b.step_bp),
        "points": [_point(p) for p in pts],
        "history": [{"label": k, "points": v} for k, v in sorted(hist_by.items())],
        "curves": [{"snapshot": k, "as_of": curve_dates.get(k), "points": v} for k, v in curves.items()]}

    # ---- projections (what the bank says) vs market
    out["projections"] = _projections(db, code, pts)

    # ---- stance
    stance: dict[str, Any] = {"latest": None, "history": []}
    if _has(db, "cb_stance"):
        sh = db.all("SELECT * FROM cb_stance WHERE bank = ? AND date >= ? ORDER BY date", [code, since])
        stance["latest"] = _stance_out(_stance_latest(db, code).get(code))
        stance["history"] = [{"date": iso(s["date"]), **{k: _r(s.get(k), 3) for k in (
            "policy_rate", "cpi_yoy", "core_yoy", "real_rate_cpi", "real_rate_core", "real_rate_expected", "output_gap",
            "r_star", "taylor_1993", "taylor_rstar", "taylor_balanced")}} for s in sh]
    stance["labels"] = {"taylor": LABELS["taylor_rule"], "expected_infl": LABELS["expected_inflation"]}
    out["stance"] = stance

    # ---- balance sheet
    bs: dict[str, Any] = {"available": False, "components": []}
    if _has(db, "cb_balance_sheets"):
        rows = db.all("""SELECT * FROM cb_balance_sheets WHERE bank = ? AND date >= ? ORDER BY component, date""",
                      [code, since])
        by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by[r["component"]].append(r)
        comps = []
        for comp, rs in by.items():
            last = rs[-1]
            comps.append({"component": comp, "currency": last["currency"], "source": last["source"],
                          "latest": {"date": iso(last["date"]), "value_bn": _r(last["value_bn"], 2),
                                     "change_13w_bn": _r(last["change_13w_bn"], 2),
                                     "pace_annualized_bn": _r(last["pace_annualized_bn"], 2),
                                     "change_1y_bn": _r(last["change_1y_bn"], 2)},
                          "history": [{"date": iso(r["date"]), "value_bn": _r(r["value_bn"], 2)} for r in rs]})
        bs = {"available": bool(comps), "unit": "billions of local currency",
              "qt_pace_method": "change vs ~13 weeks earlier x 365/91 (negative = run-off)", "components": comps}
    out["balance_sheet"] = bs

    # ---- tone
    tone: dict[str, Any] = {"available": False, "label": LABELS["tone"], "history": []}
    if _has(db, "cb_tone"):
        th = db.all("""SELECT t.*, d.action, d.change_bp FROM cb_tone t
                       LEFT JOIN cb_decisions d ON d.bank = t.bank AND d.decision_date = t.date
                       WHERE t.bank = ? ORDER BY t.date""", [code])
        tone = {"available": bool(th), "label": LABELS["tone"],
                "model_id": next((t["model_id"] for t in th if t.get("model_id")), None),
                "history": [{**_tone_out(t), "action": t.get("action"), "change_bp": _r(t.get("change_bp"), 2)} for t in th]}
    out["tone"] = tone
    out["data_status"] = _status(db, "cb_policy_rates", "cb_decisions", "cb_implied_paths", "cb_stance")
    return out


def _projections(db: Db, code: str, pts: list[dict[str, Any]]) -> dict[str, Any]:
    if code not in ("fed", "ecb") or not _has(db, "cb_projections"):
        return {"available": False, "note": "No machine-readable official projections for this bank."
                if code not in ("fed", "ecb") else "pending"}
    rel = db.all("SELECT DISTINCT release_date FROM cb_projections WHERE bank = ? ORDER BY release_date DESC LIMIT 2",
                 [code])
    if not rel:
        return {"available": False, "note": "pending"}
    latest = rel[0]["release_date"]
    prev = rel[1]["release_date"] if len(rel) > 1 else None
    rows = db.all("SELECT * FROM cb_projections WHERE bank = ? AND release_date IN (?, ?) ORDER BY variable, horizon, stat",
                  [code, latest, prev or latest])
    series: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["variable"], r["horizon"])
        e = series.setdefault(k, {"variable": r["variable"], "horizon": r["horizon"], "unit": r["unit"]})
        tag = "latest" if r["release_date"] == latest else "previous"
        e.setdefault(tag, {})[r["stat"]] = _r(r["value"], 3)
        e.setdefault("source", r["source"])
    vs_market = []
    if code == "fed" and pts:
        meets = [p for p in pts if p["point_type"] == "meeting"]
        for (var, hz), e in sorted(series.items()):
            if var != "policy_rate" or not hz.isdigit() or "latest" not in e:
                continue
            ys = [p for p in meets if p["target_date"].year == int(hz)]
            if not ys:
                continue
            mk = ys[-1]
            dots = e["latest"].get("median")
            vs_market.append({"year": hz, "sep_median": dots, "market_meeting": iso(mk["target_date"]),
                              "market_implied": _r(mk["implied_rate"]), "market_implied_tp_adj": _r(mk["implied_rate_tp_adj"]),
                              "market_minus_sep_bp": _r((mk["implied_rate"] - dots) * 100, 1) if dots is not None else None})
    return {"available": True, "latest_release": iso(latest), "previous_release": iso(prev),
            "label": "Official projections (Fed SEP via FRED/ALFRED; ECB/Eurosystem staff via ECB MPD)",
            "series": list(series.values()), "vs_market": vs_market,
            "vs_market_label": "SEP year-end median vs market-implied rate at the last FOMC meeting of that year "
                               "(approximation from official curves)" if code == "fed" else None}

