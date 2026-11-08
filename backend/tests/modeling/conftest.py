"""Model Studio test fixtures: a tiny, fully controlled lake (exact expected values for PIT / lag tests)."""

from __future__ import annotations

import calendar
import os
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.core import config as config_mod
from app.core import schema as schema_mod
from app.core.storage import write_table

NOW = datetime(2026, 1, 5, tzinfo=UTC)
START, END = date(2015, 1, 1), date(2021, 12, 31)
SECTORS = {1: "TECH", 2: "TECH", 3: "TECH", 4: "FINS", 5: "FINS", 6: "FINS"}


def sid(i: int) -> str:
    return f"S{i:012d}"


def iid(i: int) -> str:
    return f"I{i:012d}"


def bdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def price(i: int, k: int) -> float:
    """adj_close of security i on business day number k (deterministic, strictly positive)."""
    return 100.0 + i * 10 + 0.05 * k * i + (k % 7) * 0.01


def build_mini_lake(s) -> dict:
    days = bdays(START, END)
    write_table(pl.DataFrame([{"issuer_id": iid(i), "name": f"Co {i}", "country": "US", "region": "US",
                               "sector_id": SECTORS[i], "subsector_id": None, "is_active": True, "updated_at": NOW}
                              for i in SECTORS]), "issuers", mode="overwrite", settings=s)
    write_table(pl.DataFrame([{"security_id": sid(i), "issuer_id": iid(i), "security_type": "common", "display_ticker": f"T{i}",
                               "primary_listing_id": f"L{i}", "trading_currency": "USD", "name": f"Co {i}", "is_primary_security": True, "is_active": True, "updated_at": NOW}
                              for i in SECTORS]), "securities_master", mode="overwrite", settings=s)
    uni = []
    for i in SECTORS:
        vf = date(2015, 1, 1) if i != 6 else date(2020, 1, 1)
        vt = date(2019, 12, 31) if i == 5 else None
        uni.append({"universe_id": "mvp_us", "security_id": sid(i), "valid_from": vf, "valid_to": vt, "updated_at": NOW})
    write_table(pl.DataFrame(uni), "universe_membership", mode="overwrite", settings=s)
    px = []
    for i in SECTORS:
        for k, d in enumerate(days):
            p = price(i, k)
            px.append({"security_id": sid(i), "listing_id": f"L{i}", "date": d, "region": "US", "year": d.year,
                       "open": p, "high": p, "low": p, "close": p, "volume": 1000.0 + i, "adj_factor": 1.0, "split_factor": 1.0,
                       "adj_close": p, "currency": "USD", "close_usd": p, "dollar_volume_usd": p * 1000,
                       "source_id": "polygon", "ingested_at": NOW})
    write_table(pl.DataFrame(px), "prices_daily", mode="overwrite", settings=s)
    # ratios for issuer 1: new periods + revisions (older-period revision must not change "latest known")
    rr = [(date(2019, 12, 31), date(2020, 2, 15), 0.10), (date(2020, 3, 31), date(2020, 5, 10), 0.12),
          (date(2019, 12, 31), date(2020, 6, 1), 0.11), (date(2020, 6, 30), date(2020, 8, 14), 0.15),
          (date(2020, 6, 30), date(2020, 9, 1), 0.16)]
    ratios = [{"issuer_id": iid(1), "metric_id": "roe", "period_kind": "TTM", "fiscal_year": pe.year, "fiscal_period": "TTM",
               "period_end": pe, "value": v, "unit": "pct", "inputs_complete": True, "knowledge_date": kd, "dq_score": 1.0,
               "computed_at": NOW, "run_id": "R"} for pe, kd, v in rr]
    for i in (2, 3, 4, 5, 6):
        for q in range(24):
            y_, m_ = 2016 + q // 4, 3 * (q % 4 + 1)
            pe = date(y_, m_, calendar.monthrange(y_, m_)[1])
            ratios.append({"issuer_id": iid(i), "metric_id": "roe", "period_kind": "TTM", "fiscal_year": pe.year,
                           "fiscal_period": "TTM", "period_end": pe, "value": 0.05 * i + 0.001 * q, "unit": "pct",
                           "inputs_complete": True, "knowledge_date": pe + timedelta(days=45), "dq_score": 1.0,
                           "computed_at": NOW, "run_id": "R"})
    write_table(pl.DataFrame(ratios), "ratios", mode="overwrite", settings=s)
    # macro: monthly series observed on the 1st, published 45 days later; one revision of 2020-01 published 2020-04-20
    mo = []
    m, idx = date(2015, 1, 1), 0
    while m <= date(2021, 11, 1):
        mo.append({"series_id": "test:M", "source_id": "fred", "date": m, "value": float(idx),
                   "knowledge_date": m + timedelta(days=45), "knowledge_date_estimated": False, "ingested_at": NOW})
        idx += 1
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    mo.append({"series_id": "test:M", "source_id": "fred", "date": date(2020, 1, 1), "value": 999.0,
               "knowledge_date": date(2020, 4, 20), "knowledge_date_estimated": False, "ingested_at": NOW})
    for k, d in enumerate(days):
        mo.append({"series_id": "test:D", "source_id": "fred", "date": d, "value": 2.0 + 0.001 * k,
                   "knowledge_date": d, "knowledge_date_estimated": False, "ingested_at": NOW})
    write_table(pl.DataFrame(mo), "macro_observations", mode="overwrite", settings=s)
    write_table(pl.DataFrame([{"series_id": sid_, "source_id": "fred", "native_code": sid_, "title": sid_, "country": "US",
                               "category": "rates", "frequency": f, "units": "Percent", "default_transform": "level",
                               "release_lag_days": 1, "is_core": True, "updated_at": NOW}
                              for sid_, f in (("test:M", "M"), ("test:D", "D"))]), "macro_series", mode="overwrite", settings=s)
    fs = []
    for i in SECTORS:
        m = date(2015, 1, 31)
        n = 0
        while m <= date(2021, 12, 31):
            fs.append({"as_of_date": m, "year": m.year, "security_id": sid(i), "issuer_id": iid(i), "factor_id": "value",
                       "score": float(i * 10 + n % 5), "coverage": 1.0, "computed_at": NOW, "run_id": "R"})
            n += 1
            nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
            m = date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1) - timedelta(days=1)
    write_table(pl.DataFrame(fs), "factor_scores", mode="overwrite", settings=s)
    return {"days": days}


@pytest.fixture(scope="module")
def mini_lake(tmp_path_factory):
    d = tmp_path_factory.mktemp("studio") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    s = config_mod.get_settings()
    s.ensure_dirs()
    info = build_mini_lake(s)
    yield {"settings": s, **info}
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
