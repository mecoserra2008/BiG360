"""Delayed quotes service (workstream B): market-hours logic, recorded Yahoo / Polygon responses (no network), the
polling loop with a fake clock (cadence, idle when closed, on-demand snapshot, publish only changes, session
transitions), budgets and back-off, bus pruning, interest file, universe resolution, supervisor child + tray toggle."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.realtime.quotes import sessions as S
from app.realtime.quotes.service import QuotesService, read_status
from app.realtime.quotes.sources import PolygonSnapshot, SourceError, YahooQuotes, parse_polygon_snapshot, parse_yahoo
from app.realtime.quotes.store import QuoteStore, read_interest, record_interest
from app.realtime.quotes.universe import Target, Universe

FIX = Path(__file__).parent / "fixtures" / "yahoo_quote.json"
YDOC = json.loads(FIX.read_text(encoding="utf-8"))


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


CAL = S.SessionCalendar(days={"XNYS": {date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 30),
                                       date(2026, 11, 2)},   # no 10-29 (holiday)
                              "XTKS": {date(2026, 10, 27), date(2026, 10, 28)}})


# ------------------------------------------------------------------------------------------------ sessions
def test_exchange_hours_holidays_and_tail():
    st = S.state("XNYS", utc(2026, 10, 27, 14, 0), CAL)          # 10:00 ET
    assert st.open and st.reason == "open" and st.closes_at == utc(2026, 10, 27, 20, 0)
    st = S.state("XNYS", utc(2026, 10, 27, 12, 0), CAL)          # 08:00 ET
    assert not st.open and st.reason == "before_open" and st.opens_at == utc(2026, 10, 27, 13, 30)
    st = S.state("XNYS", utc(2026, 10, 27, 20, 10), CAL)         # 16:10 ET: delayed closing print
    assert st.open and st.in_tail and st.reason == "closing_print"
    st = S.state("XNYS", utc(2026, 10, 27, 21, 0), CAL)          # 17:00 ET
    assert not st.open and st.reason == "after_close" and st.opens_at == utc(2026, 10, 28, 13, 30)
    st = S.state("XNYS", utc(2026, 10, 29, 15, 0), CAL)          # holiday in the fixed calendar
    assert not st.open and st.reason == "closed_holiday"
    st = S.state("XNYS", utc(2026, 10, 31, 15, 0), CAL)          # Saturday
    assert not st.open and st.reason == "closed_weekend"
    assert S.state("XTKS", utc(2026, 10, 27, 1, 0), CAL).open                    # 10:00 Tokyo
    assert not S.state("XTKS", utc(2026, 10, 27, 7, 0), CAL).open                # 16:00 Tokyo


def test_fx_and_globex_hours():
    assert S.state(S.FX, utc(2026, 10, 27, 3, 0), CAL).open                      # Tuesday night
    st = S.state(S.FX, utc(2026, 10, 31, 12, 0), CAL)                            # Saturday
    assert not st.open and st.reason == "closed_weekend" and st.opens_at == utc(2026, 11, 1, 22, 0)   # Sun 17:00 NY (EST)
    st = S.state(S.GLOBEX, utc(2026, 10, 27, 21, 30), CAL)                       # 17:30 ET maintenance break
    assert not st.open and st.reason == "daily_break"
    assert S.state(S.GLOBEX, utc(2026, 10, 27, 22, 30), CAL).open


def test_market_key():
    assert S.market_key("XOFF", "Europe/London", "fx") == S.FX
    assert S.market_key("XCME", "America/Chicago", "future") == S.GLOBEX
    assert S.market_key("XNAS", "America/New_York", "common") == "XNAS"
    assert S.market_key(None, "Asia/Tokyo", "index") == "XTKS"
    assert S.market_key("ZZZZ", None, None) == "XNYS"


# ------------------------------------------------------------------------------------------------ parsers
def test_parse_yahoo_recorded():
    qs, bad = parse_yahoo(YDOC)
    by = {q.symbol: q for q in qs}
    assert {"^GSPC", "EURUSD=X", "ES=F", "AAPL", "ASML.AS", "7203.T"} <= set(by)
    a = by["AAPL"]
    assert a.price == 347.1 and a.currency == "USD" and a.delay_min == 15 and a.source == "yahoo"
    assert a.change_pct == pytest.approx(0.00927569) and a.market_state == "POST" and a.quote_ts == 1793131201
    assert by["7203.T"].delay_min == 20 and by["ES=F"].quote_type == "FUTURE"
    assert "NOSUCH.ZZ" not in by


def test_parse_polygon_snapshot():
    doc = {"status": "OK", "tickers": [
        {"ticker": "AAPL", "todaysChange": 3.19, "todaysChangePerc": 0.93, "updated": 1793131201000000000,
         "day": {"c": 347.0, "h": 349.0, "l": 343.0, "v": 5.1e7}, "lastTrade": {"p": 347.1, "t": 1793131201000000000},
         "min": {"c": 347.05}, "prevDay": {"c": 343.91}},
        {"ticker": "BAD", "day": {}, "lastTrade": {}, "min": {}, "prevDay": {}}]}
    qs = parse_polygon_snapshot(doc)
    assert len(qs) == 1 and qs[0].price == 347.1 and qs[0].change_pct == pytest.approx(0.0093)
    assert qs[0].quote_ts == pytest.approx(1793131201.0) and qs[0].delay_min == 15


def test_polygon_plan_not_included_disables_for_a_day():
    p = PolygonSnapshot("k", getter=lambda t: {"status": "NOT_AUTHORIZED", "message": "plan"})
    with pytest.raises(SourceError) as e:
        p.fetch(["AAPL"], now=1000.0)
    assert e.value.fatal
    ok, why = p.available(now=1000.0 + 3600)
    assert not ok and "plan does not include" in why
    assert p.available(now=1000.0 + 25 * 3600)[0]
    assert PolygonSnapshot(None).available()[1] == "POLYGON_API_KEY missing"


# ------------------------------------------------------------------------------------------------ service
class Clock:
    def __init__(self, t: datetime):
        self.t = t.timestamp()

    def __call__(self) -> float:
        return self.t

    def adv(self, s: float) -> None:
        self.t += s


class FakeBus:
    def __init__(self):
        self.msgs: list = []
        self.commits: list = []
        self.pruned: list = []

    def publish_many(self, envs):
        self.msgs += envs
        return list(range(len(self.msgs) - len(envs) + 1, len(self.msgs) + 1))

    def head_id(self):
        return len(self.msgs)

    def commit(self, g, t, i):
        self.commits.append((g, t, i))

    def prune_topic(self, t, h, keep_min=0):
        self.pruned.append((t, h))
        return 0


class Lim:
    def acquire(self, *a, **k):
        return True


def universe() -> Universe:
    ts = [Target("S_AAPL", "AAPL", "AAPL", "XNAS", "common", "USD", "AAPL"),
          Target("IDX:^GSPC", "^GSPC", None, "XNYS", "index", "USD", "^GSPC"),
          Target("IDX:EURUSD=X", "EURUSD=X", None, S.FX, "fx", "USD", "EURUSD=X"),
          Target("SZ_7203", "7203.T", None, "XTKS", "common", "JPY", "7203.T")]
    for t in ts:
        t.groups = ["watchlist"]
    return Universe(targets={t.security_id: t for t in ts}, counts={"watchlist": 4})


def make(tmp_data, clock, getter=None, polygon=None, **kw):
    calls: list[list[str]] = []

    def g(symbols):
        calls.append(list(symbols))
        if getter is not None:
            return getter(symbols)
        return {"quoteResponse": {"result": [r for r in YDOC["quoteResponse"]["result"] if r["symbol"] in symbols]}}

    svc = QuotesService(tmp_data, bus=FakeBus(), store=QuoteStore(settings=tmp_data),
                        yahoo=YahooQuotes(batch=50, limiter=Lim(), getter=g), polygon=polygon or PolygonSnapshot(None),
                        calendar=CAL, universe_fn=lambda interest: universe(), interest_fn=lambda: [], clock=clock,
                        use_lock=False, **kw)
    return svc, calls


def test_open_market_cadence_store_and_publish_changes(tmp_data):
    clock = Clock(utc(2026, 10, 27, 14, 0))          # US open, Tokyo closed, FX open
    svc, calls = make(tmp_data, clock)
    rep = svc.tick()
    assert rep["rows"] == 4                           # 3 open + one snapshot for the closed Tokyo name (no quote yet)
    assert sorted(calls[0]) == ["7203.T", "AAPL", "EURUSD=X", "^GSPC"] and len(calls) == 1   # one batched request
    last = svc.store.last()
    assert last["S_AAPL"]["price"] == 347.1 and last["S_AAPL"]["fetched_at"] == clock()
    assert svc.bus.msgs[0].topic == "quotes.tick" and svc.bus.msgs[0].payload["n"] == 4
    assert svc.bus.msgs[0].payload["delayed"] is True
    clock.adv(30)
    assert svc.tick()["due"] == 0 and len(calls) == 1          # cadence: nothing before 60 s
    clock.adv(31)
    rep = svc.tick()
    assert rep["due"] == 2 and rep["published"] == 0           # US polled again; same prices -> nothing published
    clock.adv(60)
    assert svc.tick()["due"] == 3                              # FX every 120 s joins
    st = svc.status()
    assert st["feeds"]["yahoo"]["status"] == "ok" and st["feeds"]["yahoo"]["requests_1h"] == 3
    assert st["markets"]["XTKS"]["open"] is False and st["markets"]["XNAS"]["every_s"] == 60
    assert st["feeds"]["polygon"]["status"] == "disabled" and st["feeds"]["polygon"]["reason"] == "POLYGON_API_KEY missing"


def test_closed_markets_idle_with_reason_and_session_transition(tmp_data):
    clock = Clock(utc(2026, 10, 31, 15, 0))          # Saturday: everything closed
    svc, calls = make(tmp_data, clock)
    svc.tick()                                       # one snapshot (no stored quotes yet)
    n = len(calls)
    clock.adv(600)
    assert svc.tick()["due"] == 0 and len(calls) == n
    st = svc.status()
    assert st["state"] == "idle" and st["reason"] == "all markets closed"
    assert st["markets"]["XNAS"]["reason"] == "closed_weekend" and st["markets"]["XNAS"]["opens_at"]
    assert st["feeds"]["yahoo"]["status"] == "idle"
    # Monday open -> quotes.session transition + immediate poll
    clock.t = utc(2026, 11, 2, 14, 31).timestamp()
    svc.cal = S.SessionCalendar(days={"XNYS": {date(2026, 11, 2)}})
    rep = svc.tick()
    assert rep["transitions"] >= 2 and rep["due"] >= 2
    sess = [m for m in svc.bus.msgs if m.topic == "quotes.session"]
    assert any(m.payload["market"] == "XNAS" and m.payload["open"] for m in sess)


def test_backoff_on_429_and_budget(tmp_data):
    clock = Clock(utc(2026, 10, 27, 14, 0))

    def boom(symbols):
        raise SourceError("yahoo: rate limited (HTTP 429)", status=429, retry_after_s=300)

    svc, calls = make(tmp_data, clock, getter=boom)
    rep = svc.tick()
    assert "error" in rep["yahoo"]
    f = svc.feeds["yahoo"]
    assert f.status == "backoff" and f.backoff_until == pytest.approx(clock() + 300) and "retry at" in f.reason
    clock.adv(61)
    assert svc.tick()["yahoo"]["skipped"] == "backoff"
    assert len(calls) == 1                                   # no request while backing off
    # budget: 1 request per hour allowed
    svc2, calls2 = make(tmp_data, Clock(utc(2026, 10, 27, 14, 0)), yahoo_per_hour=1)
    svc2.tick()
    svc2.clock.adv(61)
    assert svc2.tick()["yahoo"]["skipped"] == "budget" and len(calls2) == 1
    assert "budget" in svc2.feeds["yahoo"].reason


def test_polygon_used_for_us_equities_when_plan_allows(tmp_data):
    clock = Clock(utc(2026, 10, 27, 14, 0))
    doc = {"status": "OK", "tickers": [{"ticker": "AAPL", "lastTrade": {"p": 350.0, "t": 1793131201000}, "day": {},
                                        "min": {}, "prevDay": {"c": 343.91}}]}
    poly = PolygonSnapshot("k", limiter=Lim(), getter=lambda t: doc)
    svc, calls = make(tmp_data, clock, polygon=poly)
    svc.tick()
    assert svc.store.last(["S_AAPL"])["S_AAPL"]["source"] == "polygon"
    assert "AAPL" not in calls[0]
    # plan not included -> disabled with the reason, Yahoo takes over
    poly2 = PolygonSnapshot("k", limiter=Lim(), getter=lambda t: {"status": "NOT_AUTHORIZED"})
    svc2, calls2 = make(tmp_data, Clock(utc(2026, 10, 27, 14, 0)), polygon=poly2)
    svc2.tick()
    assert svc2.feeds["polygon"].status == "disabled" and "plan" in svc2.feeds["polygon"].reason
    assert "AAPL" in calls2[0]


def test_bus_maintenance_and_status_file(tmp_data):
    clock = Clock(utc(2026, 10, 27, 14, 0))
    svc, _ = make(tmp_data, clock)
    svc.tick()
    assert ("lake", "quotes.tick", 1) in svc.bus.commits and ("quotes.tick", 6.0) in svc.bus.pruned
    svc.write_status()
    st = read_status(tmp_data)
    assert st["universe"]["n"] == 4 and st["alive"] is False and st["delayed"] is True


def test_real_bus_prune_topic(tmp_data):
    import time as _t

    from app.realtime.bus import SqliteBus

    bus = SqliteBus(settings=tmp_data)
    for i in range(5):
        bus.publish("quotes.tick", f"q{i}", {"i": i})
    bus.publish("filings", "f1", {"x": 1})
    bus._con().execute("UPDATE messages SET ts_ingest = ? WHERE topic = 'quotes.tick'", (_t.time() - 7 * 3600,))
    assert bus.prune_topic("quotes.tick", 6, keep_min=2) == 3
    assert len(bus.read(["quotes.tick"])) == 2 and len(bus.read(["filings"])) == 1
    bus.close()


def test_interest_file_ttl(tmp_data):
    record_interest(["S1", "S2"], tmp_data, now=1000.0)
    assert sorted(read_interest(tmp_data, now=1100.0)) == ["S1", "S2"]
    assert read_interest(tmp_data, now=1000.0 + 31 * 60) == []


def test_universe_resolution(tmp_data, monkeypatch):
    import duckdb

    from app.realtime.quotes import universe as U

    db = tmp_data.data_dir / "db" / "serving_test.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE securities (security_id VARCHAR, security_type VARCHAR, trading_currency VARCHAR, "
                "display_ticker VARCHAR, name VARCHAR, primary_listing_id VARCHAR, is_active BOOLEAN)")
    con.execute("CREATE TABLE listings (listing_id VARCHAR, yf_ticker VARCHAR, polygon_ticker VARCHAR, mic VARCHAR, "
                "timezone VARCHAR)")
    con.execute("CREATE TABLE xa_assets (instrument_id VARCHAR, linked_ids VARCHAR[])")
    con.execute("""INSERT INTO securities VALUES ('S_MSFT','common','USD','MSFT','Microsoft','L1',true),
        ('IDX:^GSPC','index','USD','^GSPC','S&P 500','L2',true), ('IDX:ES=F','future','USD','ES=F','E-mini','L3',true),
        ('IDX:EURUSD=X','fx','USD','EURUSD=X','EUR/USD','L4',true), ('ETF:SPY','etf','USD','SPY','SPY','L5',true)""")
    con.execute("""INSERT INTO listings VALUES ('L1','MSFT','MSFT','XNAS','America/New_York'),
        ('L2','^GSPC',NULL,'XNYS','America/New_York'), ('L3','ES=F',NULL,'XCME','America/Chicago'),
        ('L4','EURUSD=X',NULL,'XOFF','Europe/London'), ('L5','SPY','SPY','ARCX','America/New_York')""")
    con.execute("INSERT INTO xa_assets VALUES ('IDX:^GSPC', ['IDX:^GSPC','IDX:ES=F','ETF:SPY'])")
    con.close()
    monkeypatch.setattr("app.core.storage.current_serving_db", lambda settings=None: db)
    u = U.serving_universe({"watchlist": ["S_MSFT", "S_GONE"], "thesis": [], "viewed": []}, tmp_data)
    t = u.targets
    assert t["S_MSFT"].market == "XNAS" and t["S_MSFT"].polygon == "MSFT" and t["S_MSFT"].groups == ["watchlist"]
    assert t["IDX:ES=F"].market == S.GLOBEX and t["IDX:EURUSD=X"].market == S.FX and t["IDX:EURUSD=X"].polygon is None
    assert "cockpit" in t["ETF:SPY"].groups and t["IDX:^GSPC"].groups == ["index", "cockpit"]
    assert u.unresolved and "S_GONE" in u.unresolved and u.counts["fx"] == 1


def test_supervisor_quotes_child_and_tray_toggle(tmp_data, monkeypatch):
    from app.ops import supervisor as SUP

    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False, live_news=True, live_quotes=True)
    names = [c.name for c in sup.children]
    assert names == ["realtime", "quotes"]
    q = sup.children[1]
    assert q.command[-3:] == ["app.cli", "realtime", "quotes"] and q.ram_cap_mb == SUP.LIVE_QUOTES_RAM_CAP_MB
    assert q.stop_file.name == "quotes.STOP" and q.toggle.name == "live_quotes.OFF"
    monkeypatch.setattr(sup, "start_child", lambda c: setattr(c, "state", "running"))
    monkeypatch.setattr(sup, "stop_child", lambda c, restart=False: None)
    SUP.set_live_quotes(False, tmp_data)
    q.state = "stopped"
    sup.check_child(q)
    assert q.state == "disabled"
    SUP.set_live_quotes(True, tmp_data)
    sup.check_child(q)
    assert q.state == "running"

    from app.desktop.tray_actions import TrayController

    class Api:
        def get(self, path, **kw):
            return {"data_as_of_overall": "2026-10-20", "scheduler": {}, "ops_alerts": []}                 if path.endswith("/system/status") else {"active": "weekly", "profiles": [], "items": []}

    ctl = TrayController(tmp_data, api=Api(), open_url=lambda u: None, notify=lambda *n: None,
                         cfg={"api_host": "127.0.0.1", "api_port": 8095, "serve_ui": True, "filler": False,
                              "realtime": False, "start_supervisor": True, "status_poll_s": 60, "notifications": True,
                              "notify_severities": ["error"], "update_dir": "", "update_check_h": 24,
                              "live_news": True, "live_quotes": True})
    ids = [m.id for m in ctl.menu()]
    assert "livequotes" in ids and next(m for m in ctl.menu() if m.id == "livequotes").checked
    assert "off" in ctl.dispatch("livequotes") and not ctl.live_quotes_on()
    assert "on" in ctl.dispatch("livequotes") and ctl.live_quotes_on()
    assert "--live-quotes" in ctl.supervisor_command()
    assert any(ln.startswith("Live quotes:") for ln in ctl.status_lines())
