"""Gap detector: prices sessions (missing / partial / stale), FX staleness, GDELT partial days, seeds, merge rules."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.ops import calendars as cal
from app.ops import gaps as G
from app.ops.coverage import effective_sla, load_coverage, validate_coverage

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)  # Tuesday


@pytest.fixture(autouse=True)
def _weekday_calendars(monkeypatch):
    # exchange_calendars is slow to build: tests use Mon-Fri for every MIC
    monkeypatch.setattr(cal, "sessions", lambda mic, a, b, settings=None: cal.business_days(a, b))


def _write_prices(s, region: str, counts: dict[date, int]) -> None:
    rows = [{"security_id": f"S{i:04d}", "date": d} for d, n in counts.items() for i in range(n)]
    df = pl.DataFrame(rows, schema={"security_id": pl.Utf8, "date": pl.Date})
    for y in sorted({d.year for d in counts}):
        p = s.data_dir / "normalized" / "prices_daily" / f"region={region}" / f"year={y}"
        p.mkdir(parents=True, exist_ok=True)
        df.filter(pl.col("date").dt.year() == y).write_parquet(p / "part-test.parquet")


def _days(a: date, b: date) -> list[date]:
    return [d for d in (a + timedelta(days=i) for i in range((b - a).days + 1)) if d.weekday() < 5]


def test_registry_is_valid():
    from app.scheduler.base import load_job_registry

    reg = load_coverage()
    ids = {j["id"] for j in load_job_registry()["jobs"]}
    assert validate_coverage(reg, ids) == []
    assert {"prices.US", "prices.EU", "fx.rates", "gdelt.days", "cftc.cot", "inst.13f"} <= set(reg["datasets"])
    assert {"gdelt_e6", "sec_13f_history", "cftc_history", "earnings_dates_history", "yahoo_price_history"} <= set(reg["campaigns"])
    e6 = reg["campaigns"]["gdelt_e6"]  # same parameters as the hand-launched E6 backfill
    assert e6["args"][e6["args"].index("--workers") + 1] == "3" and e6["args"][e6["args"].index("--dl-threads") + 1] == "4"


def test_effective_sla_widens_with_the_profile():
    assert effective_sla({"sla": 1, "calendar": "XNYS"}, None) == 1
    assert effective_sla({"sla": 1, "calendar": "XNYS"}, 9) == 6      # 9 days ~ 6 sessions
    assert effective_sla({"sla": 2, "calendar": "daily"}, 3) == 3
    assert effective_sla({"sla": 14, "calendar": "13f"}, 9) == 14


def test_prices_missing_partial_and_stale(tmp_data):
    s = tmp_data
    counts = {d: 100 for d in _days(date(2026, 9, 7), date(2026, 10, 16))}
    counts[date(2026, 10, 12)] = 2      # hole on an open session (audit F05)
    counts[date(2026, 10, 7)] = 50      # partial
    counts[date(2026, 10, 19)] = 3      # a few early bars for the latest session (not complete)
    _write_prices(s, "EU", counts)
    rep = G.detect(s, datasets=["prices.EU"], now=NOW, profile_sla_days=None)
    gaps = {r["gap_id"]: r for r in G.read_gaps(s).iter_rows(named=True)}
    miss = gaps["prices.EU|EU|2026-10-12|2026-10-12"]
    assert miss["gap_class"] == "missing" and miss["actual"] == 2 and miss["expected"] == 100 and miss["status"] == "open"
    assert miss["fill_kind"] == "yf_refetch" and miss["priority"] == 11
    assert gaps["prices.EU|EU|2026-10-07|2026-10-07"]["gap_class"] == "partial"
    st = rep.statuses["prices.EU"]
    assert st["data_as_of"] == "2026-10-16" and st["status"] == "partial" and st["lag"] == 1
    assert st["cells"]["2026-10-12"] == 0.02 and st["cells"]["2026-10-11"] is None
    # daily SLA 1 session: the 10-19 session is late but within SLA; with no data for 3 sessions it is stale
    rep2 = G.detect(s, datasets=["prices.EU"], now=NOW + timedelta(days=3), profile_sla_days=None)
    assert rep2.statuses["prices.EU"]["status"] == "stale"
    stale = G.read_gaps(s).filter(pl.col("gap_id") == "prices.EU|EU|stale").row(0, named=True)
    assert stale["gap_class"] == "stale" and stale["period_start"] == date(2026, 10, 19)
    # the weekly profile tolerates ~6 sessions
    rep3 = G.detect(s, datasets=["prices.EU"], now=NOW + timedelta(days=3), profile_sla_days=9)
    assert rep3.statuses["prices.EU"]["status"] == "partial"


def test_fill_bookkeeping_survives_and_resolution(tmp_data):
    s = tmp_data
    counts = {d: 100 for d in _days(date(2026, 9, 7), date(2026, 10, 16))}
    counts[date(2026, 10, 12)] = 0
    _write_prices(s, "UK", counts)
    G.detect(s, datasets=["prices.UK"], now=NOW)
    gid = "prices.UK|UK|2026-10-12|2026-10-12"
    df = G.read_gaps(s).with_columns(
        pl.when(pl.col("gap_id") == gid).then(3).otherwise(pl.col("attempts")).cast(pl.Int16).alias("attempts"),
        pl.when(pl.col("gap_id") == gid).then(pl.lit("failed")).otherwise(pl.col("status")).alias("status"))
    from app.core.storage import write_table

    write_table(df, "data_gaps", mode="overwrite", settings=s)
    G.detect(s, datasets=["prices.UK"], now=NOW + timedelta(hours=1))
    r = G.read_gaps(s).filter(pl.col("gap_id") == gid).row(0, named=True)
    assert r["attempts"] == 3 and r["status"] == "failed"          # filler state kept on re-detection
    counts[date(2026, 10, 12)] = 100                                 # the filler fixed it
    _write_prices(s, "UK", counts)
    G.detect(s, datasets=["prices.UK"], now=NOW + timedelta(hours=2))
    r = G.read_gaps(s).filter(pl.col("gap_id") == gid).row(0, named=True)
    assert r["status"] == "filled" and r["resolved_at"] is not None


def test_fx_stale_currency_and_known_slow_source(tmp_data):
    s = tmp_data
    rows = []
    for d in _days(date(2026, 9, 1), date(2026, 10, 19)):
        rows.append({"date": d, "currency": "EUR", "usd_per_unit": 1.1})
        if d <= date(2026, 10, 2):
            rows.append({"date": d, "currency": "TWD", "usd_per_unit": 0.031})
            rows.append({"date": d, "currency": "SEK", "usd_per_unit": None})
    df = pl.DataFrame(rows, schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64})
    p = s.data_dir / "normalized" / "fx_rates" / "year=2026"
    p.mkdir(parents=True)
    df.write_parquet(p / "part.parquet")
    rep = G.detect(s, datasets=["fx.rates"], now=NOW)
    g = {r["entity"] + ":" + r["gap_class"]: r for r in G.read_gaps(s).iter_rows(named=True) if r["dataset"] == "fx.rates"}
    assert g["TWD:stale"]["status"] == "manual" and "CBC" in g["TWD:stale"]["note"]   # known weekly-only source
    assert g["SEK:stale"]["status"] == "open"
    assert g["SEK:suspicious"]["actual"] == 0                                        # usd_per_unit nulls
    assert rep.statuses["fx.rates"]["status"] == "stale"
    assert g["TWD:unfillable"]["status"] == "unfillable"                             # seeded known gap


def test_gdelt_partial_days_from_state_and_seed_resolution(tmp_data):
    s = tmp_data
    days = {}
    for i in range(1, 15):
        d = (NOW.date() - timedelta(days=i)).isoformat()
        days[d] = {"files": 576, "files_ok": 576, "files_missing": 0, "errors": 0}
    days["2026-10-15"] = {"files": 576, "files_ok": 512, "files_missing": 64, "errors": 3}
    days["2025-06-20"] = {"files": 576, "files_ok": 0, "files_missing": 576, "errors": 0}
    days["2025-05-13"] = {"files": 576, "files_ok": 576, "files_missing": 0, "errors": 0}
    (s.state_dir / "events_v2.json").write_text(json.dumps({"days": days}), encoding="utf-8")
    rep = G.detect(s, datasets=["gdelt.days"], now=NOW)
    g = {r["gap_id"]: r for r in G.read_gaps(s).iter_rows(named=True)}
    part = g["gdelt.days|2026-10-15|2026-10-15|2026-10-15"]
    assert part["gap_class"] == "partial" and part["actual"] == 512 and part["fill_kind"] == "gdelt_redo"
    out = g["gdelt.days|2025-06-20|2025-06-20|2025-06-20"]
    assert out["gap_class"] == "missing" and json.loads(out["fill_params"])["outage"] is True
    assert g["gdelt.days|2025-05-13|seed:gdelt:2025-05-13"]["status"] == "filled"     # seed resolved by the state
    st = rep.statuses["gdelt.days"]
    assert st["data_as_of"] == "2026-10-19" and st["cells"]["2026-10-15"] == round(512 / 576, 3)


def test_merge_seeds_manual_and_unfillable_never_reopen(tmp_data):
    reg = load_coverage(tmp_data)
    found = [G.Gap("prices.US", "US", "missing", date(2026, 10, 1), date(2026, 10, 1), 100, 0)]
    found += G._seed_gaps(reg, found, tmp_data)
    df = G.merge_gaps(G.read_gaps(tmp_data), found, {"prices.US"}, reg, "R1", NOW)
    rows = {r["gap_id"]: r for r in df.iter_rows(named=True)}
    assert rows["prices.US|US|2026-10-01|2026-10-01"]["status"] == "open"
    seed = next(r for k, r in rows.items() if k.endswith("seed:fx:TWD"))
    assert seed["status"] == "unfillable" and seed["origin"] == "seed"
    assert next(r for k, r in rows.items() if k.endswith("seed:13f:oom"))["status"] == "manual"
    # an unfillable gap stays unfillable when detected again
    df = df.with_columns(pl.when(pl.col("gap_id") == "prices.US|US|2026-10-01|2026-10-01").then(pl.lit("unfillable"))
                         .otherwise(pl.col("status")).alias("status"))
    df2 = G.merge_gaps(df, found, {"prices.US"}, reg, "R2", NOW + timedelta(hours=1))
    assert df2.filter(pl.col("gap_id") == "prices.US|US|2026-10-01|2026-10-01")["status"][0] == "unfillable"
    # undetected detector gaps of a checked dataset resolve; other datasets are left alone
    df3 = G.merge_gaps(df2, [], {"prices.US"}, reg, "R3", NOW + timedelta(hours=2))
    assert df3.filter(pl.col("gap_id") == "prices.US|US|2026-10-01|2026-10-01")["status"][0] == "unfillable"


def test_one_failing_check_does_not_stop_the_rest(tmp_data, monkeypatch):
    def boom(cx, spec, st):
        raise RuntimeError("broken check")

    monkeypatch.setitem(G.CHECKS, "sessions", boom)
    rep = G.detect(tmp_data, datasets=["prices.EU", "fx.rates"], now=NOW)
    assert "prices.EU" in rep.errors and rep.statuses["prices.EU"]["status"] == "error"
    assert rep.statuses["fx.rates"]["status"] == "missing"
