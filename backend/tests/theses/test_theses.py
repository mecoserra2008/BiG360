"""Thesis tracker (roadmap #8): rules, status evaluator, performance math, store and API against a small synthetic
serving snapshot (tests/fixtures/serving_small.py) in a temp data dir (never the user's app_state)."""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots
from app.core import config as config_mod
from app.core import schema as schema_mod
from app.theses import export as tex
from app.theses import perf as tperf
from app.theses import rules as trules
from app.theses.evaluate import check, evaluate_status

# ------------------------------------------------------------------------------------------------ pure units


def _t(**kw):
    base = {"thesis_id": "TH1", "title": "T", "subject_type": "security", "subject_id": "S1", "security_id": "S1",
            "price_id": "S1", "direction": "long", "horizon": "12M", "conviction": 3, "status": "active", "kpis": [],
            "catalysts": [], "invalidation": [], "entry_lo": 95.0, "entry_hi": 105.0, "target": 130.0, "stop": 80.0,
            "inception_price": 100.0}
    base.update(kw)
    return base


def test_rules_security_long_us():
    t = _t(kpis=[{"kpi_id": "k1", "label": "ROIC"}], review_every_days=30,
           catalysts=[{"catalyst_id": "c1", "label": "Investor day", "date": "2026-11-01", "kind": "corporate"}])
    rs = trules.build_rules(t, {"region": "US"})
    pc = {r["params"]["band"]: r for r in rs if r["rule_type"] == "price_cross"}
    assert pc["target"]["params"]["direction"] == "above" and pc["target"]["params"]["level"] == 130.0
    assert pc["stop"]["params"]["direction"] == "below" and pc["stop"]["security_id"] == "S1"
    assert {"entry_hi", "entry_lo"} <= set(pc)
    types = {r["rule_type"] for r in rs}
    assert {"score_change", "earnings_upcoming", "earnings_surprise", "activist_13d", "short_interest_spike",
            "thesis_kpi", "thesis_review", "thesis_catalyst", "thesis_status", "thesis_event_shock"} <= types
    assert "eu_short_position" not in types
    assert all(r["params"]["thesis_id"] == "TH1" for r in rs)


def test_rules_short_and_other_subjects():
    rs = trules.build_rules(_t(direction="short", target=70.0, stop=120.0), {"region": "UK"})
    pc = {r["params"]["band"]: r["params"]["direction"] for r in rs if r["rule_type"] == "price_cross"}
    assert pc["target"] == "below" and pc["stop"] == "above"
    assert "eu_short_position" in {r["rule_type"] for r in rs}
    c = trules.build_rules(_t(subject_type="country", subject_id="DE", security_id=None, price_id=None), {})
    assert {r["rule_type"] for r in c} == {"macro_surprise", "regime_change", "thesis_event_shock", "thesis_status"}
    assert next(r for r in c if r["rule_type"] == "macro_surprise")["params"]["countries"] == ["DE"]
    m = trules.build_rules(_t(subject_type="macro_theme", subject_id="US:inflation", security_id=None, price_id=None), {})
    ms = next(r for r in m if r["rule_type"] == "macro_surprise")["params"]
    assert ms["themes"] == ["inflation"] and ms["countries"] == ["US"]
    x = trules.build_rules(_t(subject_type="cross_asset", subject_id="CL", security_id=None, price_id="IDX:CL=F"),
                           {"futures_root": "CL"})
    assert "thesis_positioning" in {r["rule_type"] for r in x}
    assert all(r["security_id"] is None for r in x if r["rule_type"].startswith("thesis_"))


def test_check_ops():
    assert check(5, "gt", 4) and not check(4, "gt", 4) and check(4, "gte", 4)
    assert check(3, "lt", 4) and check(4, "lte", 4) and check(5, "between", 4, 6) and not check(7, "between", 4, 6)
    assert check(None, "gt", 1) is None


def test_status_rules():
    d = date(2026, 10, 9)
    st, rs = evaluate_status(_t(), 110.0, [], d)
    assert st == "on_track" and any(r["code"] == "price_in_favour" for r in rs)
    st, rs = evaluate_status(_t(), 79.0, [], d)
    assert st == "invalidated" and rs[0]["code"] == "stop_hit"
    st, rs = evaluate_status(_t(), 85.0, [], d)  # 5 of 20 points above the stop -> within a third
    assert st == "at_risk" and any(r["code"] == "near_stop" for r in rs)
    st, rs = evaluate_status(_t(), 131.0, [], d)
    assert st == "on_track" and any(r["code"] == "target_reached" for r in rs)
    st, _ = evaluate_status(_t(direction="short", target=70.0, stop=120.0), 121.0, [], d)
    assert st == "invalidated"
    k = {"kpi_id": "k1", "label": "ROIC", "op": "gte", "threshold": 0.1, "threshold_hi": None, "value": 0.05,
         "ok": False, "role": "confirm", "error": None}
    st, rs = evaluate_status(_t(), 110.0, [k], d)
    assert st == "at_risk" and any(r["code"] == "kpi_breach:k1" for r in rs)
    st, _ = evaluate_status(_t(), 110.0, [{**k, "role": "invalidate"}], d)
    assert st == "invalidated"
    st, _ = evaluate_status(_t(invalidation=[{"condition_id": "c", "label": "x", "kind": "kpi", "kpi_id": "k1"}]),
                            110.0, [{**k, "role": "watch"}], d)
    assert st == "invalidated"
    st, rs = evaluate_status(_t(), 110.0, [], d, score_now=40.0, score_ref=60.0)
    assert st == "at_risk" and any(r["code"] == "score_change" for r in rs)
    st, _ = evaluate_status(_t(horizon_date="2026-01-01"), 110.0, [], d)
    assert st == "at_risk"
    st, _ = evaluate_status(_t(price_id=None, subject_type="macro_theme"), None, [], d)
    assert st == "unknown"


def test_perf_helpers_and_hit_stats():
    assert tperf.max_drawdown([1.0, 1.2, 0.9, 1.1]) == pytest.approx(0.9 / 1.2 - 1)
    assert tperf.derive_outcome(0.1) == "win" and tperf.derive_outcome(-0.1) == "loss" and tperf.derive_outcome(0.0) == "flat"
    ts = [{"thesis_id": f"T{i}", "status": "closed", "direction": d, "conviction": 3, "subject_type": "security",
           "close_reason": cr, "inception_price": 100.0, "exit_price": ex, "outcome": None, "inception_date": "2026-01-01",
           "closed_at": "2026-03-02T00:00:00+00:00"}
          for i, (d, ex, cr) in enumerate([("long", 120.0, "target_hit"), ("long", 90.0, "stopped"),
                                           ("short", 80.0, "target_hit")])]
    ts.append({"thesis_id": "T9", "status": "active", "direction": "long", "conviction": 3, "subject_type": "security",
               "auto_status": "at_risk"})
    s = tperf.hit_stats(ts, {})
    assert s["n_closed"] == 3 and s["overall"]["wins"] == 2 and s["overall"]["losses"] == 1
    assert s["overall"]["hit_rate"] == pytest.approx(2 / 3)
    assert s["overall"]["avg_return"] == pytest.approx((0.2 - 0.1 + 0.2) / 3)
    assert s["target_hit_rate"] == pytest.approx(2 / 3) and s["avg_days_held"] == 60
    assert {g["key"]: g["n"] for g in s["by_direction"]} == {"long": 2, "short": 1}
    assert s["auto_status"]["at_risk"] == 1


def test_markdown_export_sections():
    t = {**_t(), "created_at": "2026-10-01T00:00:00+00:00", "inception_date": "2026-10-01", "closed_at": None,
         "subject_label": "AAA Corp", "thesis_md": "Margins expand.", "key_drivers": ["pricing"], "tags": [],
         "kpis": [{"kpi_id": "k1", "label": "ROIC", "kind": "metric", "ref": "roic", "op": "gte", "threshold": 0.1}]}
    md = tex.to_markdown(t, [{"entry_date": "2026-10-02", "created_at": "x", "kind": "note", "body_md": "First note",
                              "links": [{"kind": "url", "label": "doc", "url": "https://example.org"}]}], [], [],
                         {"start_date": "2026-10-01", "end_date": "2026-10-09", "days": 8, "basis": "local",
                          "thesis_return": 0.05}, {"auto_status": "on_track", "reasons": [], "kpis": []})
    for s in ("# T", "## Thesis", "## Key drivers", "## Price plan", "## KPIs to watch", "## Performance since inception",
              "## Journal", "First note", "+5.0%"):
        assert s in md
    pm = tex.post_mortem_template({**t, "close_reason": "target_hit", "outcome": "win"}, {"thesis_return": 0.2}, None, None)
    assert "Post-mortem" in pm and "Right for the right reasons?" in pm and "+20.0%" in pm


# ------------------------------------------------------------------------------------------------ API + store


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("theses") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    snapshots.reset()
    from app.theses import store

    store.reset_cache()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from app.api.serving import build_serving_db
    from tests.fixtures.serving_small import build_lake

    info = build_lake(s, n_securities=120, years=3)
    build_serving_db(s)
    from app.api.main import create_app

    yield {"client": TestClient(create_app()), "info": info, "settings": s}
    snapshots.reset()
    store.reset_cache()
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def _ok(r, code=200):
    assert r.status_code == code, r.text[:600]
    return r.json() if r.content and r.headers["content-type"].startswith("application/json") else r.text


def _db():
    from app.api.deps import Db, current_snapshot

    return Db(current_snapshot())


def test_api_flow(env):
    c = env["client"]
    sec = next(x for x in env["info"]["securities"] if x["region"] == "US")
    sid = sec["security_id"]
    db = _db()
    last = db.one("SELECT close FROM prices WHERE security_id = ? ORDER BY date DESC LIMIT 1", [sid])["close"]
    row = db.one("SELECT CAST(m_roic AS DOUBLE) AS roic, CAST(c_balanced AS DOUBLE) AS comp FROM sec WHERE security_id = ?",
                 [sid])
    dfl = _ok(c.get("/api/v1/theses/subject-defaults", params={"subject_type": "security", "subject_id": sid}))
    assert dfl["price_id"] == sid and dfl["benchmark_id"] == "IDX:^GSPC" and dfl["last_price"] == pytest.approx(last)
    _ok(c.get("/api/v1/theses/subject-defaults", params={"subject_type": "security", "subject_id": "NOPE"}), 404)
    body = {"title": "Quality compounder", "subject_type": "security", "subject_id": sid, "direction": "long",
            "horizon": "12M", "conviction": 4, "status": "idea", "thesis_md": "## Why\nMargins.",
            "key_drivers": ["pricing power", " "], "entry_lo": last * 0.95, "entry_hi": last * 1.02,
            "target": last * 1.3, "stop": last * 0.8, "review_every_days": 30, "tags": ["Quality", "US"],
            "kpis": [{"label": "Composite", "kind": "metric", "ref": "composite", "op": "gte", "threshold": 0},
                     {"label": "ROIC x2", "kind": "expr", "expr": "roic * 2", "op": "gte", "threshold": -100,
                      "role": "watch"}],
            "catalysts": [{"label": "Investor day", "date": (date(2026, 10, 9) + timedelta(days=2)).isoformat(),
                           "kind": "corporate"}],
            "origin": {"kind": "company", "ref": sid}}
    bad = {**body, "target": last * 0.7}
    _ok(c.post("/api/v1/theses", json=bad), 422)
    _ok(c.post("/api/v1/theses", json={**body, "kpis": [{"label": "x", "kind": "metric", "ref": "nope", "threshold": 1}]}), 422)
    t = _ok(c.post("/api/v1/theses", json=body), 201)
    tid = t["thesis_id"]
    assert t["inception_price"] == pytest.approx(last) and t["price_currency"] and t["tags"] == ["quality", "us"]
    assert t["key_drivers"] == ["pricing power"] and t["auto_status"] in ("on_track", "at_risk")
    assert t["pending_review"]["status"] == "scheduled" and len(t["snapshots"]) == 1
    types = {a["rule_type"] for a in t["alerts"]}
    assert {"price_cross", "score_change", "earnings_upcoming", "thesis_kpi", "thesis_review", "thesis_status"} <= types
    alerts = _ok(c.get("/api/v1/alerts"))
    assert sum(1 for a in alerts if a["thesis_id"] == tid) == len(t["alerts"])
    st = _ok(c.get(f"/api/v1/theses/{tid}/status"))
    k = {x["label"]: x for x in st["kpis"]}
    assert k["Composite"]["value"] == pytest.approx(row["comp"])
    if row["roic"] is not None:
        assert k["ROIC x2"]["value"] == pytest.approx(2 * row["roic"])
    # board listing + filters
    lst = _ok(c.get("/api/v1/theses", params={"status": "idea", "tag": "quality"}))
    assert lst["total"] == 1 and lst["counts"]["idea"] == 1 and lst["items"][0]["card"]["last_price"] == pytest.approx(last)
    assert _ok(c.get("/api/v1/theses", params={"q": "zzz"}))["total"] == 0
    # update: rules re-sync (unchanged rules keep their ids), activation snapshot
    keep = {a["alert_id"] for a in t["alerts"] if a["rule_type"] == "score_change"}
    t2 = _ok(c.patch(f"/api/v1/theses/{tid}", json={"status": "active", "target": last * 1.4,
                                                       "kpis": [{"label": "Composite", "ref": "composite",
                                                                 "threshold": 0}]}))
    assert t2["status"] == "active" and t2["opened_at"] and {"creation", "activation"} <= {s["kind"] for s in t2["snapshots"]}
    assert keep <= {a["alert_id"] for a in t2["alerts"]}
    assert next(a for a in t2["alerts"] if a["params"].get("band") == "target")["params"]["level"] == pytest.approx(last * 1.4)
    assert sum(a["rule_type"] == "thesis_kpi" for a in t2["alerts"]) == 1
    _ok(c.patch(f"/api/v1/theses/{tid}", json={"conviction": 9}), 422)
    # journal CRUD
    e = _ok(c.post(f"/api/v1/theses/{tid}/journal", json={"body_md": "Read the 10-Q", "entry_date": "2026-10-05",
                                                          "links": [{"kind": "url", "ref": "x", "url": "https://x.org"}]}), 201)
    e2 = _ok(c.patch(f"/api/v1/theses/{tid}/journal/{e['entry_id']}", json={"body_md": "Read the 10-Q twice"}))
    assert e2["body_md"] == "Read the 10-Q twice" and e2["links"][0]["kind"] == "url"
    j = _ok(c.get(f"/api/v1/theses/{tid}/journal"))
    assert any(x["kind"] == "status_change" for x in j) and any(x["entry_id"] == e["entry_id"] for x in j)
    _ok(c.delete(f"/api/v1/theses/{tid}/journal/{e['entry_id']}"), 204)
    _ok(c.delete(f"/api/v1/theses/{tid}/journal/{e['entry_id']}"), 404)
    # review: snapshot + next schedule
    rv = _ok(c.post(f"/api/v1/theses/{tid}/reviews", json={"decision": "hold", "notes_md": "fine", "conviction_after": 3}), 201)
    assert rv["status"] == "completed" and rv["snapshot_id"]
    rvs = _ok(c.get(f"/api/v1/theses/{tid}/reviews"))
    assert [r["status"] for r in rvs["items"]].count("scheduled") == 1 and rvs["next_review_date"]
    _ok(c.post(f"/api/v1/theses/{tid}/snapshots", json={"note": "manual"}), 201)
    # timeline / evidence / export
    tl = _ok(c.get(f"/api/v1/theses/{tid}/timeline"))
    assert tl["bars"] and tl["bands"]["target"] == pytest.approx(last * 1.4)
    assert {m["kind"] for m in tl["markers"]} >= {"journal", "catalyst", "review"}
    ev = _ok(c.get(f"/api/v1/theses/{tid}/evidence"))
    assert ev["data_status"] == "ok" and isinstance(ev["filings"], list)
    r = c.get(f"/api/v1/theses/{tid}/export.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "# Quality compounder" in r.text and "## Reviews" in r.text
    # performance math vs the serving prices (inception moved back 120 days)
    from app.theses import store

    incep = db.as_of - timedelta(days=120)
    store.update_thesis(tid, {"inception_date": incep})
    p = _ok(c.get(f"/api/v1/theses/{tid}/performance"))
    a0 = db.scalar("SELECT adj_close FROM prices WHERE security_id = ? AND date <= ? ORDER BY date DESC LIMIT 1", [sid, incep])
    a1 = db.scalar("SELECT adj_close FROM prices WHERE security_id = ? ORDER BY date DESC LIMIT 1", [sid])
    b0 = db.scalar("SELECT adj_close FROM prices WHERE security_id = 'IDX:^GSPC' AND date <= ? ORDER BY date DESC LIMIT 1", [incep])
    b1 = db.scalar("SELECT adj_close FROM prices WHERE security_id = 'IDX:^GSPC' ORDER BY date DESC LIMIT 1")
    assert p["basis"] == "local" and p["subject"]["return"] == pytest.approx(a1 / a0 - 1, rel=1e-6)
    assert p["excess_vs_benchmark"] == pytest.approx((a1 / a0 - 1) - (b1 / b0 - 1), rel=1e-6)
    assert p["series"][0]["subject"] == pytest.approx(100.0) and p["max_drawdown"] <= 0
    # evaluate now: force a stop breach -> invalidated + thesis_status event
    inv = _ok(c.patch(f"/api/v1/theses/{tid}", json={"stop": last * 1.01, "target": last * 1.5}))
    assert inv["auto_status"] == "invalidated" and any(r["code"] == "stop_hit" for r in inv["status_reasons"])
    evs = _ok(c.get("/api/v1/alerts/events", params={"limit": 500}))
    assert any("-> invalidated" in x["message"] and x["alert_id"] in {a["alert_id"] for a in inv["alerts"]} for x in evs)
    ev_ = _ok(c.post(f"/api/v1/theses/{tid}/evaluate"))
    assert ev_["auto_status"] == "invalidated" and ev_["events_fired"] == 0  # idempotent per data date
    # close -> post-mortem template, exit price, outcome, alerts disabled
    cl = _ok(c.patch(f"/api/v1/theses/{tid}", json={"status": "closed", "close_reason": "stopped"}))
    assert cl["closed_at"] and cl["exit_price"] == pytest.approx(last) and cl["outcome"] in ("win", "loss", "flat")
    assert "Post-mortem" in cl["post_mortem_md"] and all(not a["enabled"] for a in cl["alerts"])
    assert any(s["kind"] == "close" and s["extra"].get("performance") for s in cl["snapshots"])
    stats = _ok(c.get("/api/v1/theses/stats"))
    assert stats["n_closed"] == 1 and stats["overall"]["n"] == 1
    _ok(c.post(f"/api/v1/theses/{tid}/reviews", json={"decision": "hold"}), 422)
    # delete cascades rules + events
    _ok(c.delete(f"/api/v1/theses/{tid}"), 204)
    _ok(c.get(f"/api/v1/theses/{tid}"), 404)
    assert not [a for a in _ok(c.get("/api/v1/alerts")) if a.get("thesis_id") == tid]
    db.close()


def test_other_subjects_and_job(env):
    c = env["client"]
    sec = env["info"]["securities"][0]
    t = _ok(c.post("/api/v1/theses", json={"title": "Sector view", "subject_type": "sector", "subject_id": sec["sector_id"],
                                           "kpis": [{"label": "Median comp", "ref": "composite", "threshold": 0}]}), 201)
    st = _ok(c.get(f"/api/v1/theses/{t['thesis_id']}/status"))
    assert st["kpis"][0]["value"] is not None and st["kpis"][0]["basis"] == "median of members"
    m = _ok(c.post("/api/v1/theses", json={"title": "US inflation sticky", "subject_type": "macro_theme",
                                           "subject_id": "US:inflation", "status": "active"}), 201)
    assert m["price_id"] is None and {a["rule_type"] for a in m["alerts"]} >= {"macro_surprise", "regime_change"}
    _ok(c.post("/api/v1/theses", json={"title": "x", "subject_type": "country", "subject_id": "123"}), 422)
    # nightly job over the open theses
    from app.theses import evaluate

    ctx = SimpleNamespace(settings=env["settings"], as_of=date(2026, 10, 9), check_cancelled=lambda: None,
                          log=SimpleNamespace(info=lambda *a, **k: None))
    res = evaluate.run(ctx)
    assert res.status == "success" and res.entities_ok == 2
    # alerts_eval tolerates the thesis_* rules (no-op) and evaluates the generated standard rules
    from app.scheduler.alerts import EVALUATORS, THESIS_RULE_TYPES

    assert all(EVALUATORS[r]({}, ["x"], None, date(2026, 10, 9)) == [] for r in THESIS_RULE_TYPES)  # type: ignore[arg-type]
