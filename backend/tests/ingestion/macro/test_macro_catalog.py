"""contracts/macro_catalog.yaml validation + macro engine behaviour (reconcile, keyed skip, audit). No network."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.ingestion import macro_eu, macro_intl, macro_national  # noqa: F401 - registers fetchers
from app.ingestion.macro_common import (
    FETCHERS,
    SCALES,
    GSeries,
    MacroRunner,
    catalog_doc,
    expand_entries,
    load_catalog,
    reconcile_observations,
)

FREQS = {"D", "W", "M", "Q", "A"}
CATEGORIES = {"growth", "inflation", "rates", "credit", "fx", "commodities", "labor", "housing", "sentiment", "liquidity",
              "trade", "fiscal", "other"}
TRANSFORMS = {"level", "diff", "pct_change", "yoy", "log_diff"}


@pytest.fixture(scope="module")
def cat() -> list[GSeries]:
    return load_catalog()


@pytest.fixture(scope="module")
def doc() -> dict:
    return catalog_doc()


def test_catalog_schema(cat: list[GSeries], doc: dict) -> None:
    ids = [s.series_id for s in cat]
    assert not [k for k, v in Counter(ids).items() if v > 1], "duplicate series ids"
    regions, inds = set(doc["regions"]), set(doc["indicators"])
    for s in cat:
        assert s.series_id.startswith(f"{s.source_id}:"), s.series_id
        assert s.frequency in FREQS and s.category in CATEGORIES and s.transform in TRANSFORMS, s.series_id
        assert s.scale_eff in SCALES, s.series_id
        assert s.country in regions, (s.series_id, s.country)
        assert all(i in inds for i in s.indicators), (s.series_id, s.indicator)
        assert s.rank >= 1 and s.lag >= 0
        assert "%" not in s.series_id and " " not in s.series_id, s.series_id   # ids are used in API URL paths
    assert len(cat) >= 900


def test_every_source_has_fetcher_and_one_job(cat: list[GSeries], doc: dict) -> None:
    groups = {"macro_eu": set(macro_eu.SOURCES), "macro_intl": set(macro_intl.SOURCES),
              "macro_national": set(macro_national.SOURCES)}
    for src in {s.source_id for s in cat} - {"fred"}:
        assert src in FETCHERS, src
        assert sum(src in g for g in groups.values()) == 1, src
        assert src in doc["sources"] or src in FETCHERS
    keyed = {s.requires_key for s in cat if s.requires_key}
    from app.core.config import SECRET_FIELDS

    assert keyed <= set(SECRET_FIELDS)


def test_percent_indicators_declare_percent_scale(cat: list[GSeries]) -> None:
    pct = {"policy_rate", "rate_3m", "yield_2y", "yield_10y", "yield_curve", "unemployment", "gov_debt", "gov_deficit",
           "current_account", "credit_gap"}
    for s in cat:
        if (s.indicators and s.indicators[0] in pct and s.transform == "level" and "index" not in s.units.lower()
                and s.req.get("measure") != "level"):
            assert s.scale_eff == "percent", (s.series_id, s.scale_eff, s.units)


def test_every_target_region_has_core_indicators(cat: list[GSeries], doc: dict) -> None:
    """policy rate, CPI, GDP, unemployment and a 10Y yield (or a documented gap) for every target region."""
    have = {(s.country, i) for s in cat for i in s.indicators}
    gaps = doc.get("gaps") or {}
    must = ["policy_rate", "cpi_yoy", "gdp_yoy", "unemployment", "yield_10y"]
    missing = []
    for reg, meta in doc["regions"].items():
        if not (meta or {}).get("target", True) or reg in ("G7", "EM", "WLD"):
            continue
        for ind in must:
            if (reg, ind) not in have and ind not in (gaps.get(reg) or {}):
                missing.append((reg, ind))
    assert not missing, missing


def test_consumer_series_ids_are_produced(cat: list[GSeries]) -> None:
    """Series ids referenced by the regime model, macro betas, cost of capital and the API curves exist in a catalog."""
    from app.analytics.cost_of_capital import RF_SERIES
    from app.api.routers.macro import CURVES
    from app.ingestion.fred import full_catalog
    from app.ml.macro_betas import FACTOR_SERIES as BETA_FACTORS

    produced = {s.series_id for s in cat} | {f"fred:{s.native}" for s in full_catalog()}
    used = {sid for ids in RF_SERIES.values() for sid in ids}
    used |= {sid for c in CURVES for _, _, cands in c[4] for sid in cands}
    used |= {v[0] for v in BETA_FACTORS.values()}
    from app.ml import macro_regime

    for name in dir(macro_regime):
        obj = getattr(macro_regime, name)
        if isinstance(obj, dict) and name.isupper():
            used |= {v[0] for v in obj.values() if isinstance(v, tuple) and isinstance(v[0], str) and ":" in v[0]}
    assert not sorted(used - produced)


def test_each_template_expansion() -> None:
    doc = {"regions": {"DE": {"name": "Germany"}, "EA": {"name": "Euro area"}},
           "series": [{"id": "x:{code}.{region}", "src": "x", "title": "{name} y", "each": {"DE": "DE", "EA_10Y": {
               "code": "SR_10Y", "region": "EA"}}}]}
    out = expand_entries(doc)
    assert [(e["id"], e["region"], e["title"]) for e in out] == [("x:DE.DE", "DE", "Germany y"),
                                                                 ("x:SR_10Y.EA", "EA", "Euro area y")]


# ------------------------------------------------------------------------------------------------ engine


def test_missing_key_skips_cleanly(tmp_data) -> None:  # noqa: ANN001
    sp = GSeries("banxico:SF61745", "banxico", "SF61745", "t", "MX", "rates", "D", "percent", "level", 0,
                 requires_key="banxico_token", req={"code": "SF61745"})
    r = MacroRunner("t", tmp_data)
    r.run_sources([sp], ["banxico"])
    assert not r.report["failed"] and "missing key BANXICO_TOKEN" in r.report["skipped"]["banxico"]
    assert r.report["skipped_series"]["banxico:SF61745"] == "missing key BANXICO_TOKEN"


def test_reconcile_is_revision_aware(tmp_data) -> None:  # noqa: ANN001
    from app.core.storage import read_table, write_table

    now = datetime.now(UTC)
    old = pl.DataFrame({"series_id": ["s:a"] * 2, "source_id": ["s"] * 2, "date": [date(2026, 7, 1), date(2026, 8, 1)],
                        "value": [1.0, 2.0], "knowledge_date": [date(2026, 8, 20), date(2026, 9, 20)],
                        "knowledge_date_estimated": [True, True], "ingested_at": [now, now]})
    write_table(old, "macro_observations", settings=tmp_data)
    new = pl.DataFrame({"series_id": ["s:a"] * 3, "source_id": ["s"] * 3,
                        "date": [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)],
                        "value": [1.0, 2.5, 3.0], "knowledge_date": [date(2026, 8, 20), date(2026, 9, 20), date(2026, 10, 11)],
                        "knowledge_date_estimated": [True, True, True], "ingested_at": [now] * 3})
    out, st = reconcile_observations(new, tmp_data, today=date(2026, 10, 11))
    assert st == {"input": 3, "unchanged_dropped": 1, "revised": 1, "new": 1}
    rev = out.filter(pl.col("date") == date(2026, 8, 1)).row(0, named=True)
    assert rev["knowledge_date"] == date(2026, 10, 11) and rev["knowledge_date_estimated"] is False  # first seen today
    write_table(out, "macro_observations", settings=tmp_data)
    allrows = read_table("macro_observations", settings=tmp_data).filter(pl.col("date") == date(2026, 8, 1))
    assert sorted(allrows.get_column("value").to_list()) == [2.0, 2.5]   # old vintage kept -> PIT as-of works


def test_audit_helpers() -> None:
    from app.ingestion.macro_audit import comparable, detect_frequency, staleness

    assert detect_frequency([date(2026, m, 1) for m in range(1, 10)]) == "M"
    assert detect_frequency([date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 1), date(2026, 10, 1)]) == "Q"
    assert staleness(date(2026, 8, 1), "M", 20, date(2026, 10, 11))[0] == "fresh"
    assert staleness(date(2026, 1, 1), "Q", 45, date(2026, 10, 11))[0] == "stale"
    assert staleness(date(2023, 1, 1), "M", 20, date(2026, 10, 11))[0] == "discontinued"
    idx = pl.DataFrame({"date": [date(2025, m, 1) for m in range(1, 13)] + [date(2026, m, 1) for m in range(1, 13)],
                        "value": [100.0] * 12 + [103.0] * 12})
    y = comparable(idx, {"scale": "index", "default_transform": "yoy", "frequency": "M"})
    assert y is not None and y.get_column("value").to_list()[-1] == pytest.approx(3.0)
    frac = pl.DataFrame({"date": [date(2026, 1, 1)], "value": [0.05]})
    assert comparable(frac, {"scale": "fraction", "default_transform": "level", "frequency": "M"}).item(0, "value") == 5.0
