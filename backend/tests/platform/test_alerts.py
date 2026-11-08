"""Alert rule engine with fixture frames: crossings, zone entry, score change, insider signal, sentiment, filings,
model decile, watchlist expansion, cooldown/idempotency and app_state persistence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import duckdb
import polars as pl

from app.core.storage import ensure_duckdb_tables
from app.scheduler import alerts as al

D1, D2 = date(2026, 10, 8), date(2026, 10, 9)
NOW = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


def _bars(sid, rows):
    """rows: (date, low, high, close)"""
    n = len(rows)
    return pl.DataFrame({
        "security_id": [sid] * n, "listing_id": ["L"] * n, "date": [r[0] for r in rows], "region": ["US"] * n,
        "year": [r[0].year for r in rows], "open": [r[3] for r in rows], "high": [r[2] for r in rows],
        "low": [r[1] for r in rows], "close": [r[3] for r in rows], "adj_factor": [1.0] * n, "split_factor": [1.0] * n,
        "adj_close": [r[3] for r in rows], "currency": ["USD"] * n, "source_id": ["polygon"] * n,
        "ingested_at": [NOW] * n,
    })


def _alert(rule_type, params, security_id="S1", watchlist_id=None, cooldown=0, alert_id=None):
    return {"alert_id": alert_id or f"A_{rule_type}", "security_id": security_id, "watchlist_id": watchlist_id,
            "rule_type": rule_type, "params": json.dumps(params), "enabled": True, "cooldown_days": cooldown,
            "last_fired_at": None, "created_at": NOW}


def _provider():
    prices = pl.concat([
        _bars("S1", [(D1, 95, 99, 98.0), (D2, 98, 103, 102.0)]),       # crosses 100 upward
        _bars("S2", [(D1, 48, 52, 50.0), (D2, 44, 47, 45.0)]),         # drops below 46, enters 44-46 zone
    ])
    zones = pl.DataFrame({
        "as_of_date": [D2, D2], "security_id": ["S2", "S2"], "zone_id": [1, 2], "side": ["support", "support"],
        "zone_low": [44.0, 30.0], "zone_high": [46.0, 31.0], "center": [45.0, 30.5], "density": [0.9, 0.5],
        "strength": [80.0, 40.0], "rank": [1, 2], "distance_atr": [-1.0, -5.0], "member_level_keys": [["a"], ["b"]],
        "member_count": [3, 1], "atr14": [1.5, 1.5], "last_close": [45.0, 45.0], "computed_at": [NOW, NOW],
    }).with_columns(pl.col("zone_id").cast(pl.Int16), pl.col("rank").cast(pl.Int16), pl.col("member_count").cast(pl.Int16))
    comp = pl.DataFrame({
        "as_of_date": [D1, D2, D1, D2], "year": [2026] * 4, "security_id": ["S1", "S1", "S2", "S2"],
        "issuer_id": ["I1", "I1", "I2", "I2"], "profile_id": ["balanced"] * 4, "score": [50.0, 65.0, 70.0, 68.0],
        "coverage": [1.0] * 4, "dq_score": [1.0] * 4, "computed_at": [NOW] * 4,
    })
    insider = pl.DataFrame({
        "as_of_date": [D1, D2], "year": [2026, 2026], "security_id": ["S1", "S1"], "issuer_id": ["I1", "I1"],
        "net_buy_value_usd_30d": [0.0, 2_500_000.0], "cluster_buy": [False, True], "c_suite_buy": [False, True],
        "signal_score": [50.0, 85.0], "computed_at": [NOW, NOW],
    })
    days = [D2 - timedelta(days=i) for i in range(30, -1, -1)]
    sent = pl.DataFrame({
        "date": days, "year": [2026] * len(days), "security_id": ["S1"] * len(days), "issuer_id": ["I1"] * len(days),
        "news_count": [3] * len(days),
        "sentiment_mean": [0.0 + (0.01 if i % 2 else -0.01) for i in range(len(days) - 1)] + [0.6],
        "sentiment_ewm_7d": [0.0] * (len(days) - 1) + [0.3], "computed_at": [NOW] * len(days),
    })
    filings = pl.DataFrame({
        "filing_id": ["sec_edgar:1", "sec_edgar:2", "sec_edgar:3"], "source_id": ["sec_edgar"] * 3, "year": [2026] * 3,
        "source_entity_id": ["1"] * 3, "issuer_id": ["I1", "I1", "I2"], "form_type": ["10-Q", "4", "10-K"],
        "form_class": ["quarterly", "insider", "annual"], "knowledge_date": [D2, D2, D1 - timedelta(days=10)],
        "is_amendment": [False] * 3, "has_xbrl": [True] * 3, "url": ["u1", "u2", "u3"], "ingested_at": [NOW] * 3,
    })
    secm = pl.DataFrame({
        "security_id": ["S1", "S2"], "issuer_id": ["I1", "I2"], "security_type": ["common", "common"],
        "primary_listing_id": ["L1", "L2"], "display_ticker": ["AAA", "BBB"], "name": ["A", "B"],
        "trading_currency": ["USD", "USD"], "is_primary_security": [True, True], "is_active": [True, True],
        "updated_at": [NOW, NOW],
    })
    preds = pl.DataFrame({
        "model_run_id": ["M1"] * 4, "date": [D1, D2, D1, D2], "security_id": ["S1", "S1", "S2", "S2"],
        "horizon": ["3M"] * 4, "score": [0.1, 0.9, 0.95, 0.96], "rank_pct": [0.5, 0.95, 0.97, 0.98],
        "decile": [5, 10, 10, 10], "is_oos": [False] * 4, "is_live": [True] * 4, "computed_at": [NOW] * 4,
    }).with_columns(pl.col("decile").cast(pl.Int8))
    return al.frames_provider({"prices_daily": prices, "confluence_zones": zones, "composite_scores": comp,
                               "insider_signals": insider, "sentiment_daily": sent, "filings_index": filings,
                               "securities_master": secm, "ml_predictions": preds})


def test_each_rule_type_fires_on_fixture():
    dp = _provider()
    rules = [
        _alert("price_cross", {"level": 100, "direction": "above"}),
        _alert("price_cross", {"level": 100, "direction": "below"}, alert_id="A_below"),   # must not fire
        _alert("zone_touch", {"zone_rank_max": 2}, security_id="S2"),
        _alert("score_change", {"profile_id": "balanced", "delta": 10}),
        _alert("score_change", {"profile_id": "balanced", "delta": 10}, security_id="S2", alert_id="A_sc2"),  # -2
        _alert("insider_buy", {"min_value_usd": 1_000_000, "cluster_only": True}),
        _alert("sentiment_shift", {"threshold_z": 2.0}),
        _alert("new_filing", {"form_classes": ["quarterly", "annual"]}),
        _alert("model_decile", {"horizon": "3M", "min_decile": 10}),
        _alert("model_decile", {"horizon": "3M", "min_decile": 10}, security_id="S2", alert_id="A_md2"),  # was already 10
    ]
    events = al.evaluate_alerts(rules, dp, D2, now=NOW)
    fired = {e["alert_id"]: e for e in events}
    assert set(fired) == {"A_price_cross", "A_zone_touch", "A_score_change", "A_insider_buy", "A_sentiment_shift",
                          "A_new_filing", "A_model_decile"}
    assert "crossed above 100" in fired["A_price_cross"]["message"]
    z = json.loads(fired["A_zone_touch"]["payload"])
    assert z["rank"] == 1 and z["rule_type"] == "zone_touch"
    assert json.loads(fired["A_score_change"]["payload"])["change"] == 15.0
    assert "cluster buy" in fired["A_insider_buy"]["message"]
    assert json.loads(fired["A_sentiment_shift"]["payload"])["z"] > 2
    assert "10-Q" in fired["A_new_filing"]["message"] and "10-K" not in fired["A_new_filing"]["message"]
    for e in events:
        assert e["read"] is False and len(e["event_id"]) == 32 and e["fired_at"] == NOW


def test_watchlist_expansion_cooldown_and_idempotency():
    dp = _provider()
    rule = _alert("model_decile", {"horizon": "3M", "min_decile": 9}, security_id=None, watchlist_id="W1", cooldown=3)
    items = [{"watchlist_id": "W1", "security_id": "S1"}, {"watchlist_id": "W1", "security_id": "S2"}]
    ev1 = al.evaluate_alerts([rule], dp, D2, items, now=NOW)
    assert [e["security_id"] for e in ev1] == ["S1"]  # S2 was already in the top decile yesterday
    # same data date again -> idempotent
    assert al.evaluate_alerts([rule], dp, D2, items, existing_events=ev1, now=NOW + timedelta(days=5)) == []
    # a new data date within the cooldown -> suppressed; after the cooldown -> fires
    prior = [{**ev1[0], "as_of_date": D1}]
    assert al.evaluate_alerts([rule], dp, D2, items, existing_events=prior, now=NOW + timedelta(days=1)) == []
    assert len(al.evaluate_alerts([rule], dp, D2, items, existing_events=prior, now=NOW + timedelta(days=4))) == 1
    disabled = {**rule, "enabled": False}
    assert al.evaluate_alerts([disabled], dp, D2, items, now=NOW) == []


def test_broken_rule_does_not_stop_others():
    dp = _provider()
    rules = [_alert("price_cross", {"direction": "above"}, alert_id="A_broken"),  # missing level -> KeyError
             _alert("price_cross", {"level": 100})]
    events = al.evaluate_alerts(rules, dp, D2, now=NOW)
    assert [e["alert_id"] for e in events] == ["A_price_cross"]


def test_evaluate_and_store_in_app_state(tmp_data):
    con = duckdb.connect(str(tmp_data.app_state_db_path))
    ensure_duckdb_tables(con, ["alerts", "watchlist_items"])
    a = _alert("price_cross", {"level": 100, "direction": "above"})
    con.execute("INSERT INTO alerts (alert_id, security_id, watchlist_id, rule_type, params, enabled, cooldown_days, "
                "last_fired_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # alerts v6 adds thesis_id
                [a["alert_id"], a["security_id"], None, a["rule_type"], a["params"], True, 1, None, NOW])
    events = al.evaluate_and_store(D2, provider=_provider(), settings=tmp_data, con=con)
    assert len(events) == 1
    stored = con.execute("SELECT alert_id, security_id, as_of_date, read FROM alert_events").fetchall()
    assert stored == [("A_price_cross", "S1", D2, False)]
    assert con.execute("SELECT last_fired_at FROM alerts").fetchone()[0] is not None
    assert al.evaluate_and_store(D2, provider=_provider(), settings=tmp_data, con=con) == []  # idempotent
    con.close()


def test_job_entry_without_rules(tmp_data):
    from app.scheduler.base import JobContext

    res = al.run(JobContext(job_id="alerts_eval", run_id="R1", as_of=D2, settings=tmp_data))
    assert res.status == "success" and res.rows_written == {"alert_events": 0}


# ---------------------------------------------------------------- alerts contract v2


def test_contract_v2_rule_types_all_have_evaluators():
    from app.core.schema import get_contract

    enum = set(get_contract("alerts").column("rule_type").values)
    assert "price_change_pct_1d" in enum
    assert enum <= set(al.EVALUATORS) and enum <= set(al.RULE_TYPES)


def test_price_change_pct_1d():
    dp = _provider()  # S1 98 -> 102 (+4.1%), S2 50 -> 45 (-10%)
    items = [{"watchlist_id": "W", "security_id": "S1"}, {"watchlist_id": "W", "security_id": "S2"}]

    def fired(params):
        rule = _alert("price_change_pct_1d", params, security_id=None, watchlist_id="W")
        return {e["security_id"]: json.loads(e["payload"]) for e in al.evaluate_alerts([rule], dp, D2, items, now=NOW)}

    assert set(fired({"pct": 0.04})) == {"S1", "S2"}                 # default direction any (abs)
    assert set(fired({"pct": 0.05})) == {"S2"}
    assert set(fired({"pct": 0.04, "direction": "up"})) == {"S1"}
    assert set(fired({"pct": 0.04, "direction": "down"})) == {"S2"}
    p = fired({"pct": 0.05, "direction": "down"})["S2"]
    assert abs(p["pct_change"] + 0.10) < 1e-9 and p["rule_type"] == "price_change_pct_1d"


def test_score_change_level_mode():
    dp = _provider()  # balanced: S1 50 -> 65, S2 70 -> 68

    def ids(params, sid):
        return [e["security_id"] for e in al.evaluate_alerts([_alert("score_change", params, security_id=sid)], dp, D2,
                                                              now=NOW)]

    assert ids({"profile_id": "balanced", "mode": "level", "level": 60}, "S1") == ["S1"]                 # crossed up
    assert ids({"profile_id": "balanced", "mode": "level", "level": 60, "direction": "down"}, "S1") == []
    assert ids({"profile_id": "balanced", "mode": "level", "level": 70}, "S1") == []                     # not reached
    assert ids({"profile_id": "balanced", "mode": "level", "level": 69, "direction": "down"}, "S2") == ["S2"]
    assert ids({"profile_id": "balanced", "mode": "level", "level": 69, "direction": "up"}, "S2") == []
    # explicit delta mode unchanged
    assert ids({"profile_id": "balanced", "mode": "delta", "delta": 10}, "S1") == ["S1"]
    assert ids({"profile_id": "balanced", "mode": "delta", "delta": 20}, "S1") == []
