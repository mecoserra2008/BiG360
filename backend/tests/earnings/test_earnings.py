"""Unit tests for the earnings package (roadmap #6): period assignment, calendar projection, sessions, SEC
submissions parsing, reactions (CAR / BHAR), model-based expectations, season aggregates, alert rules, live consumer."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.earnings import common as C
from app.earnings import events as E
from app.earnings import expectations as X
from app.earnings import sec_history as SH
from app.earnings.fetch import jpx_period_end
from app.earnings.reactions import security_reactions
from app.earnings.signals import build_season

US = {"region": "US", "timezone": "America/New_York", "security_id": "S1", "country": "US", "fiscal_year_end_mmdd": "1231",
      "sector_id": "TECH"}
JP = {"region": "JP", "timezone": "Asia/Tokyo", "security_id": "S2", "country": "JP", "fiscal_year_end_mmdd": None,
      "sector_id": "INDU"}


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ common


def test_sessions_and_reaction_day():
    assert C.classify_session(utc(2026, 7, 30, 20, 30), "America/New_York") == "post_market"   # 16:30 ET
    assert C.classify_session(utc(2026, 7, 30, 11, 0), "America/New_York") == "pre_market"     # 07:00 ET
    assert C.classify_session(utc(2026, 7, 30, 15, 0), "America/New_York") == "intraday"
    assert C.classify_session(utc(2026, 9, 10, 6, 30), "Asia/Tokyo") == "post_market"         # 15:30 JST
    assert C.classify_session(None, "America/New_York") == "unknown"
    assert C.reaction_day(date(2026, 10, 23), "post_market") == date(2026, 10, 26)            # Friday close -> Monday
    assert C.reaction_day(date(2026, 10, 24), "pre_market") == date(2026, 10, 26)             # Saturday -> Monday
    assert C.reaction_day(date(2026, 10, 21), "intraday") == date(2026, 10, 21)


def test_period_arithmetic():
    assert C.next_period_end(date(2026, 6, 30), 3) == date(2026, 9, 30)
    assert C.next_period_end(date(2026, 6, 27), 3, prev_year=date(2025, 9, 27)) == date(2026, 9, 26)  # 52/53-week
    assert C.calendar_quarter_end_before(date(2026, 8, 4)) == date(2026, 6, 30)
    assert C.calendar_quarter_end_before(date(2026, 1, 5)) == date(2025, 9, 30)  # needs >= 10 days of lag
    assert C.season_of(date(2026, 9, 26)) == "2026Q3"
    assert jpx_period_end(date(2027, 3, 31), "Q2") == date(2026, 9, 30)
    assert jpx_period_end(date(2026, 12, 20), "Q3") == date(2026, 9, 20)


def test_period_from_titles():
    assert E.period_from_title("tdnet", "2027年1月期 第2四半期(中間期)決算短信〔日本基準〕(連結)") == date(2026, 7, 31)
    assert E.period_from_title("tdnet", "2026年7月期 決算短信〔日本基準〕(連結)") == date(2026, 7, 31)
    assert E.period_from_title("tdnet", "2027年3月期 第1四半期決算短信") == date(2026, 6, 30)
    assert E.period_from_title("hkex", "ANNOUNCEMENT OF THE RESULTS FOR THE THREE AND SIX MONTHS ENDED 30 JUNE 2026") == date(2026, 6, 30)
    assert E.period_from_title("hkex", "POLL RESULTS FOR THE 2026 AGM") is None


# ------------------------------------------------------------------------------------------------ events


def _periodic(pe: date, filed: date, form: str = "10-Q") -> dict:
    return {"period_end": pe, "fiscal_period": None, "fiscal_year": None, "ts": utc(filed.year, filed.month, filed.day, 20, 5),
            "kdate": filed, "source_id": "sec_edgar", "form_type": form, "filing_id": f"sec_edgar:{pe}", "url": None}


def _release(ts: datetime) -> dict:
    return {"ts": ts, "kdate": ts.date(), "source_id": "sec_edgar", "form_type": "8-K", "filing_id": f"sec_edgar:8k{ts.date()}",
            "url": None, "fiscal_period": None}


def test_issuer_events_prefer_8k_and_infer_unfiled_period():
    periodic = [_periodic(date(2025, 12, 31), date(2026, 2, 20), "10-K"), _periodic(date(2026, 3, 31), date(2026, 5, 5)),
                _periodic(date(2026, 6, 30), date(2026, 8, 4))]
    rel = [_release(utc(2026, 1, 28, 21, 5)), _release(utc(2026, 4, 29, 20, 5)), _release(utc(2026, 7, 29, 20, 4)),
           _release(utc(2026, 10, 28, 20, 4))]  # last one: 10-Q not filed yet
    labels = [(date(2026, 3, 31), "Q1", 2026), (date(2026, 6, 30), "Q2", 2026), (date(2025, 12, 31), "FY", 2025)]
    evs = {e["period_end"]: e for e in E.build_issuer_events("I1", US, periodic, rel, [], labels)}
    q2 = evs[date(2026, 6, 30)]
    assert q2["date_source"] == "official_release" and q2["release_date"] == date(2026, 7, 29)
    assert q2["session"] == "post_market" and q2["reaction_date"] == date(2026, 7, 30)
    assert q2["report_filing_date"] == date(2026, 8, 4) and q2["fiscal_period"] == "Q2"
    assert evs[date(2025, 12, 31)]["fiscal_period"] == "Q4"                     # FY label of a quarterly reporter
    q3 = evs[date(2026, 9, 30)]
    assert q3["period_inferred"] and q3["release_date"] == date(2026, 10, 28)


def test_issuer_events_provider_and_filing_fallbacks():
    periodic = [_periodic(date(2026, 3, 31), date(2026, 5, 5)), _periodic(date(2026, 6, 30), date(2026, 8, 4))]
    prov = [{"event_date": date(2026, 7, 16), "event_ts": utc(2026, 7, 16, 11, 0)}]
    evs = {e["period_end"]: e for e in E.build_issuer_events("I1", US, periodic, [], prov, [])}
    assert evs[date(2026, 6, 30)]["date_source"] == "provider"              # earlier than the 10-Q
    assert evs[date(2026, 6, 30)]["report_filing_date"] == date(2026, 8, 4)
    assert evs[date(2026, 3, 31)]["date_source"] == "official_filing"


def test_midnight_timestamps_are_dates_only():
    p = [_periodic(date(2026, 6, 30), date(2026, 8, 4)) | {"ts": utc(2026, 8, 4), "source_id": "esef_xbrl_org"}]
    ev = E.build_issuer_events("I1", {**US, "region": "EU", "timezone": "Europe/Paris"}, p, [], [], [])[0]
    assert ev["release_ts"] is None and ev["session"] == "unknown"


def test_calendar_projection_and_overrides():
    evs = []
    for pe, rd in ((date(2025, 9, 30), date(2025, 10, 22)), (date(2025, 12, 31), date(2026, 1, 28)),
                   (date(2026, 3, 31), date(2026, 4, 22)), (date(2026, 6, 30), date(2026, 7, 22))):
        evs.append({"period_end": pe, "release_date": rd, "session": "post_market", "fiscal_period": "Q", "date_source": "official_release"})
    rows = E.project_calendar("I1", US, evs, date(2026, 10, 1), [], [])
    assert rows[0]["period_end"] == date(2026, 9, 30) and rows[0]["date_source"] == "model_projection"
    assert rows[0]["expected_date"] == date(2026, 10, 21)                     # a year earlier + 364 days
    assert rows[0]["window_lo"] < rows[0]["expected_date"] < rows[0]["window_hi"]
    assert rows[0]["usual_session"] == "post_market"
    prov = [{"event_date": date(2026, 10, 27), "event_ts": utc(2026, 10, 27, 20, 0)}]
    rows = E.project_calendar("I1", US, evs, date(2026, 10, 1), [], prov)
    assert rows[0]["date_source"] == "provider_estimate" and rows[0]["expected_date"] == date(2026, 10, 27)
    ann = [{"event_date": date(2026, 10, 29), "period_end": date(2026, 9, 30), "source_id": "jpx_schedule", "fiscal_period": "Q2"}]
    rows = E.project_calendar("I1", US, evs, date(2026, 10, 1), ann, prov)
    assert rows[0]["date_source"] == "company_announced" and rows[0]["source_label"] == C.LABEL_COMPANY


def test_parse_submissions():
    doc = {"filings": {"recent": {
        "accessionNumber": ["0000320193-26-000018", "0000320193-26-000019", "0000320193-26-000020"],
        "form": ["8-K", "10-Q", "4"], "filingDate": ["2026-07-30", "2026-07-31", "2026-07-31"],
        "acceptanceDateTime": ["2026-07-31T00:30:28.000Z", "2026-07-31T14:01:02.000Z", "2026-07-31T20:00:00.000Z"],
        "reportDate": ["2026-07-30", "2026-06-27", ""], "items": ["2.02,9.01", "", ""],
        "primaryDocument": ["a.htm", "b.htm", "c.xml"]}}}
    rows = SH.parse_submissions(doc, "0000320193")
    assert [r["form"] for r in rows] == ["8-K", "10-Q"]                       # Form 4 dropped
    # workstream B fix: EDGAR serves the settled acceptanceDateTime late by the ET offset (Apple's 16:30 ET release
    # reads 00:30:28Z); the parser corrects it to the true 20:30:28Z (app.ingestion.sec_acceptance)
    assert rows[0]["items"] == ["2.02", "9.01"] and rows[0]["acceptance_ts"] == utc(2026, 7, 30, 20, 30, 28)
    assert rows[1]["report_date"] == date(2026, 6, 27)
    assert rows[0]["url"].endswith("/320193/000032019326000018/a.htm")


# ------------------------------------------------------------------------------------------------ reactions


def test_reaction_car_and_baseline():
    rng = np.random.default_rng(3)
    days = [d for d in (date(2025, 1, 1) + timedelta(days=i) for i in range(500)) if d.weekday() < 5]
    m = rng.normal(0, 0.01, len(days))
    r = 0.0002 + 1.2 * m + rng.normal(0, 0.01, len(days))
    i0 = 300
    r[i0] += 0.08                                                                # earnings jump on day 0
    px = 100 * np.cumprod(1 + r)
    bpx = 100 * np.cumprod(1 + m)
    df = pl.DataFrame({"date": days, "open": px * (1 + 0.0), "high": px * 1.01, "low": px * 0.99, "close": px, "adj": px})
    df = df.with_columns(pl.Series("open", np.r_[px[0], px[:-1]] * np.r_[1, np.where(np.arange(1, len(px)) == i0, 1.07, 1.0)]))
    bench = (pl.Series(days).cast(pl.Date).cast(pl.Int32).to_numpy(), bpx)
    ev = [{"issuer_id": "I1", "security_id": "S1", "region": "US", "period_end": date(2025, 3, 31),
           "release_date": days[i0 - 1], "reaction_date": days[i0]}]
    out = security_reactions(df, bench, ev, "IDX:^GSPC")
    assert len(out) == 1
    o = out[0]
    assert o["reaction_date"] == days[i0]
    assert 1.0 < o["beta"] < 1.4
    assert o["car_m1_p1"] == pytest.approx(0.08, abs=0.04)
    assert o["baseline_move_m1_p1"] == pytest.approx(o["sigma_daily"] * math.sqrt(3))
    assert o["move_ratio"] > 2
    assert o["gap_pct"] > 0.05 and o["complete"] is True
    assert o["knowledge_date"] == days[i0 + 20]


# ------------------------------------------------------------------------------------------------ expectations


def test_foster_forecast_recovers_seasonal_pattern():
    season = np.array([1.0, 1.2, 0.9, 1.5])
    y = np.array([season[i % 4] + 0.05 * (i // 4) for i in range(24)])  # seasonal + growth
    f = X.sarima_forecast(y, 1, 4, "level")
    assert f is not None and f["model"] == "sarima"
    assert f["point"] == pytest.approx(season[0] + 0.05 * 6, abs=0.05)
    assert f["lo80"] < f["point"] < f["hi80"]
    n = X.naive_forecast(y, 1, 4, "level")
    assert n["point"] == pytest.approx(season[0] + 0.05 * 6, abs=0.02)
    assert X.sarima_forecast(y[:8], 1, 4, "level") is None                  # too short


def test_clean_series_guards():
    y = np.array([1.0, 1.2, 0.9, 1.5] * 4)
    y2 = y.copy()
    y2[5] = 40.0                                                            # pre-split / broken derivation value
    out = X.clean_series(y2, 4)
    assert np.isnan(out[5]) and np.isfinite(out[-1])
    gappy = y.copy()
    gappy[-8:-1] = np.nan                                                   # stale series: 1 of the last 8 known
    assert X.clean_series(gappy, 4) is None


def test_asof_values_pit_and_split_adjustment():
    vers = [(date(2026, 3, 31), date(2026, 5, 1), 4.0, "reported"),
            (date(2026, 3, 31), date(2026, 8, 1), 4.2, "reported"),       # restated later
            (date(2026, 6, 30), date(2026, 8, 1), 2.1, "reported")]
    v = dict(X.asof_values(vers, date(2026, 7, 1), date(2026, 9, 30), [], True))
    assert v == {date(2026, 3, 31): 4.0}                                    # nothing known after the origin
    v = dict(X.asof_values(vers, date(2026, 7, 1), date(2026, 9, 30), [(date(2026, 6, 10), 2.0)], True))
    assert v[date(2026, 3, 31)] == pytest.approx(2.0)                      # 2-for-1 split after the knowledge date
    assert X.first_actual(vers, date(2026, 6, 30)) == (2.1, date(2026, 8, 1), False)


def test_issuer_targets_and_surprise():
    vers = []
    pe = date(2020, 3, 31)
    ends = []
    for i in range(24):
        ends.append(pe)
        vers.append((pe, pe + timedelta(days=30), 100.0 * (1.02 ** i) * (1.1 if i % 4 == 3 else 1.0), "reported"))
        pe = C.add_months_end(pe, 3)
    target = ends[-1]
    task = {"issuer_id": "I1", "months": 3, "series": {"revenue": vers}, "eps_basis": None, "splits": [],
            "targets": [{"period_end": target, "fiscal_period": "Q4", "fiscal_year": 2025, "release_date": target + timedelta(days=25),
                         "origin": target + timedelta(days=24)}], "currency": "USD", "fpath": {}}
    rows = X.issuer_targets(task)
    assert {r["model"] for r in rows} == {"sarima", "seasonal_naive"}
    df = X.finish(pl.DataFrame(rows))
    assert df.filter(pl.col("is_primary")).height == 1
    r = df.filter(pl.col("model") == "sarima").row(0, named=True)
    assert r["status"] == "released" and r["label"] == C.LABEL_EXPECTATION
    assert abs(r["surprise_pct"]) < 0.05
    assert r["origin_date"] < r["actual_knowledge_date"]


# ------------------------------------------------------------------------------------------------ season


def test_build_season_counts():
    ev = pl.DataFrame({"issuer_id": ["A", "B", "C"], "security_id": ["SA", "SB", "SC"], "region": ["US", "US", "JP"],
                       "country": ["US", "US", "JP"], "period_end": [date(2026, 6, 30)] * 3,
                       "release_date": [date(2026, 7, 20), date(2026, 7, 25), date(2026, 8, 10)]})
    prev = ev.with_columns(pl.col("period_end").dt.offset_by("-1y"), pl.col("release_date").dt.offset_by("-1y"))
    ev = pl.concat([ev, prev.with_columns(pl.lit("D").alias("issuer_id"))])
    sur = pl.DataFrame({"issuer_id": ["A", "B"], "period_end": [date(2026, 6, 30)] * 2, "eps_surprise": [0.1, -0.2],
                        "eps_surprise_z": [1.0, -0.5], "rev_surprise": [1.0, 2.0], "rev_surprise_pct": [0.01, 0.02]})
    rx = pl.DataFrame({"issuer_id": ["A", "B", "C"], "period_end": [date(2026, 6, 30)] * 3, "car_m1_p1": [0.03, -0.01, 0.02],
                       "move_ratio": [1.5, 0.5, 1.0]})
    s = build_season(ev, pl.DataFrame(), sur, rx, {"A": "TECH", "B": "TECH", "C": "INDU", "D": "TECH"}, date(2026, 8, 15))
    tot = s.filter((pl.col("season") == "2026Q2") & (pl.col("region") == "ALL") & (pl.col("sector_id") == "ALL")).row(0, named=True)
    assert tot["n_reported"] == 3 and tot["n_expected"] == 4                 # D reported a year earlier
    assert tot["pct_beat_eps"] == pytest.approx(0.5) and tot["reaction_breadth"] == pytest.approx(2 / 3)
    us = s.filter((pl.col("season") == "2026Q2") & (pl.col("region") == "US") & (pl.col("sector_id") == "TECH")).row(0, named=True)
    assert us["n_reported"] == 2


# ------------------------------------------------------------------------------------------------ alerts + live


def test_alert_rules_earnings():
    from app.scheduler import alerts as A

    cal = pl.DataFrame({"issuer_id": ["I1"], "security_id": ["S1"], "period_end": [date(2026, 9, 30)], "fiscal_period": ["Q3"],
                        "expected_date": [date(2026, 10, 21)], "session": ["post_market"], "date_source": ["model_projection"],
                        "source_id": ["cadence_model"], "source_label": [C.LABEL_PROJECTION],
                        "window_lo": [date(2026, 10, 18)], "window_hi": [date(2026, 10, 24)], "first_seen_date": [date(2026, 10, 1)],
                        "computed_at": [utc(2026, 10, 1)]})
    exp = pl.DataFrame({"issuer_id": ["I1"], "item_id": ["eps_diluted"], "period_end": [date(2026, 6, 30)], "model": ["sarima"],
                        "is_primary": [True], "expectation": [1.0], "actual": [1.5], "surprise_pct": [0.5], "surprise_z": [3.1],
                        "status": ["released"], "actual_knowledge_date": [date(2026, 10, 20)], "origin_date": [date(2026, 7, 20)],
                        "label": [C.LABEL_EXPECTATION], "computed_at": [utc(2026, 10, 20)]})
    sm = pl.DataFrame({"security_id": ["S1"], "issuer_id": ["I1"]})
    dp = A.frames_provider({"earnings_calendar": cal, "earnings_expectations": exp, "securities_master": sm})
    alerts = [{"alert_id": "a1", "security_id": "S1", "watchlist_id": None, "rule_type": "earnings_upcoming",
               "params": {"days_ahead": 3}, "enabled": True, "cooldown_days": 0},
              {"alert_id": "a2", "security_id": "S1", "watchlist_id": None, "rule_type": "earnings_surprise",
               "params": {"threshold_z": 2.0}, "enabled": True, "cooldown_days": 0}]
    ev = A.evaluate_alerts(alerts, dp, date(2026, 10, 20))
    by = {e["alert_id"]: e for e in ev}
    assert "Model-based projection" in by["a1"]["message"] and by["a1"]["as_of_date"] == date(2026, 10, 21)
    assert "not consensus" in by["a2"]["message"]
    assert A.evaluate_alerts(alerts[:1], dp, date(2026, 10, 10)) == []        # outside days_ahead


def test_live_earnings_consumer():
    from app.realtime.bus import Message
    from app.realtime.earnings_live import EarningsConsumer, is_earnings_release

    p = {"source_id": "sec_edgar", "form_type": "8-K", "items": ["2.02", "9.01"], "issuer_id": "I1", "security_id": "S1",
         "accession": "0001-26-1", "filing_id": "sec_edgar:0001-26-1", "filed_date": "2026-10-28", "ticker": "AAA"}
    assert is_earnings_release(p) and not is_earnings_release(p | {"items": ["5.02"]})
    c = EarningsConsumer(lookup=lambda i, d: {"period_end": "2026-09-30", "fiscal_period": "Q3",
                                              "items": {"eps_diluted": {"expectation": 1.2, "lo80": 1.0, "hi80": 1.4}}})
    msg = Message(1, "filings", "sec_edgar:0001-26-1", "filing", "2026-10-28T20:05:00Z", 0.0, p)

    class Ctx:
        settings = None

        @staticmethod
        def clock():
            return utc(2026, 10, 28, 20, 6)

    out = c.handle_one(Ctx(), msg)
    assert len(out) == 1 and out[0].topic == "signals" and out[0].type == "earnings"
    assert out[0].payload["session"] == "post_market" and out[0].payload["expectations"]["eps_diluted"]["expectation"] == 1.2
    assert "not consensus" in out[0].payload["label"]
    assert c.handle_one(Ctx(), Message(2, "filings", "k", "filing", None, 0.0, p | {"items": ["8.01"]})) == []
