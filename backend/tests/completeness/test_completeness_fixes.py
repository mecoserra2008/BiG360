"""Regression fixtures for the data-completeness workstream (docs/DATA_COMPLETENESS.md, 2026-10-27)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.analytics import ratios as R
from app.analytics import scoring as SC
from app.analytics import smart_money as SM
from app.core import availability as AV
from app.ingestion import finra_si
from app.normalize import taxonomy as TX
from app.normalize import us_symbols as U
from app.qa import completeness as C

# ------------------------------------------------------------------------------------------------ ROIC


def _panel(rows: list[dict]) -> pl.DataFrame:
    base = {"issuer_id": "I1", "vintage": date(2026, 8, 1)}
    return pl.DataFrame([{**base, **r} for r in rows])


def test_roic_uses_local_basis_twin_of_usd_nopat():
    """JP regression: NOPAT on the USD basis divided by JPY invested capital gave ROIC 0.04 % (Toyota)."""
    fx = 150.0
    rows = [{"period_end": date(2025, 3, 31), "ebit": 6.0e12, "ebit__usd": 6.0e12 / fx, "invested_capital": 7.5e13,
             "total_assets": 9.0e13},
            {"period_end": date(2026, 3, 31), "ebit": 6.2e12, "ebit__usd": 6.2e12 / fx, "invested_capital": 8.1e13,
             "total_assets": 9.5e13}]
    items = {"nopat": "ebit * (1 - 0.25)", "roic": "safe_div(nopat, avg(invested_capital))"}
    out = R.eval_metrics(_panel(rows), items, ["nopat", "roic"], usd_ids=["nopat"]).sort("period_end")
    roic = out["roic"][-1]
    assert roic == pytest.approx(6.2e12 * 0.75 / ((7.5e13 + 8.1e13) / 2), rel=1e-6)   # ~6 %, not 0.04 %
    assert out["nopat"][-1] == pytest.approx(6.2e12 / fx * 0.75)                       # nopat itself stays USD
    assert out["roic_flag"][-1] == 0.0


@pytest.mark.parametrize("ic, ta, expect_flag", [
    (1.0e8, 2.9e10, 1.0),      # EXPE-like: invested capital net of cash ~0.3 % of assets -> ROIC 1,498 %
    (2.0e9, 3.0e10, 0.0),      # plausible
    (-5.0e8, 3.0e10, None),    # negative denominator -> safe_div null, not flagged
])
def test_roic_sanity_bounds_flag_and_keep_raw(ic, ta, expect_flag):
    rows = [{"period_end": date(2025, 6, 30), "ebit": 2.5e9, "invested_capital": ic, "total_assets": ta},
            {"period_end": date(2026, 6, 30), "ebit": 2.5e9, "invested_capital": ic, "total_assets": ta}]
    items = {"roic": "safe_div(ebit, avg(invested_capital))"}
    out = R.eval_metrics(_panel(rows), items, ["roic"]).sort("period_end")
    assert out["roic_flag"][-1] == expect_flag
    if expect_flag == 1.0:
        assert out["roic"][-1] is None and out["roic_raw"][-1] > 2.0
    elif expect_flag == 0.0:
        assert out["roic"][-1] == pytest.approx(out["roic_raw"][-1])


def test_ratio_rows_emit_bound_flags():
    assert set(R.SANITY_UNITS) == {"roic_raw", "roic_flag", "croic_raw", "croic_flag"}
    assert R.SANITY_UNITS["roic_flag"] == "count"


def test_yfinance_invested_capital_chain_removed():
    """ASML regression: Yahoo 'Invested Capital' (= total debt on quarterly rows) overrode the derived definition."""
    import yaml

    from app.core.config import get_settings

    doc = yaml.safe_load((get_settings().contracts_dir / "canonical_line_items_ext_global.yaml").read_text(encoding="utf-8"))
    items = doc.get("items") or doc
    flat = str(items)
    assert "Invested Capital" not in flat


def test_employees_fallback_recent_periods_only(monkeypatch):
    iss = pl.DataFrame({"issuer_id": ["I1"], "employees": [1000]})
    monkeypatch.setattr(R, "scan_table", lambda name, settings=None: iss.lazy())
    f = pl.DataFrame({"issuer_id": ["I1", "I1"], "period_end": [date(2020, 12, 31), date(2026, 6, 30)],
                      "employees": [None, None], "employees__usd": [None, None]},
                     schema={"issuer_id": pl.Utf8, "period_end": pl.Date, "employees": pl.Float64,
                             "employees__usd": pl.Float64})
    out = R._employees_fallback(f).sort("period_end")
    assert out["employees"].to_list() == [None, 1000.0]
    assert out["employees__usd"].to_list() == [None, 1000.0]


# ------------------------------------------------------------------------------------------------ factor delta_1m


def test_factor_delta_1m_uses_nearest_reference_row(monkeypatch):
    """as_of 10-23: the backward match of 09-25 is 08-31 (53 days) -> every delta_1m was null; 09-30 must be used."""
    hist = pl.DataFrame({"as_of_date": [date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 16)],
                         "security_id": ["S1"] * 3, "factor_id": ["value"] * 3, "score": [40.0, 50.0, 55.0]})
    monkeypatch.setattr(SC, "scan_table", lambda name, settings=None: hist.lazy())
    fs = pl.DataFrame({"as_of_date": [date(2026, 10, 23)], "security_id": ["S1"], "factor_id": ["value"],
                       "score": [60.0]})
    out = SC._delta_1m(fs, None)
    assert out["delta_1m"][0] == pytest.approx(10.0)          # 60 - 50 (09-30), not null
    fs2 = pl.DataFrame({"as_of_date": [date(2026, 10, 16)], "security_id": ["S1"], "factor_id": ["value"],
                        "score": [55.0]})
    assert SC._delta_1m(fs2, None)["delta_1m"][0] == pytest.approx(15.0)  # 09-30 is only 16 d back -> 08-31 (46 d)


# ------------------------------------------------------------------------------------------------ smart money z


def test_smart_flow_z_is_not_degenerate():
    """Regression: z_smart_flow ~ -0.14 for most names (heavy tail: max 1,047 vs median ~0)."""
    rng = np.random.default_rng(7)
    x = np.r_[rng.normal(0, 0.001, 900), rng.normal(0, 0.3, 90), [1046.9, 500.0, 200.0]]
    df = pl.DataFrame({"security_id": [f"s{i}" for i in range(len(x))], "as_of_date": [date(2026, 10, 23)] * len(x),
                       "smart_flow": x, "inst_flow": x, "inst_breadth": x})
    z = SM.standardize(df)["z_smart_flow"].to_numpy()
    q25, q75 = np.percentile(z, [25, 75])
    assert q75 - q25 > 0.8                      # was ~0.03 with the mean / std z
    assert np.nanmax(np.abs(z)) <= SM.Z_CLIP + 1e-9
    assert len(np.unique(np.round(z, 2))) > 100


# ------------------------------------------------------------------------------------------------ FINRA symbols


def test_symbol_variants_compact_class_shares():
    d, c = U.variants("BF.B")
    assert "BF.B" in d and "BF-B" in d and "BF/B" in d
    assert "BFB" in c
    assert U.variants("AAPL") == (["AAPL"], [])


def test_listed_symbol_map_and_resolution(monkeypatch, tmp_data):
    sm = pl.DataFrame({"security_id": ["SBFB", "SBFA", "SBH", "SBHA", "ETF:IWM", "SASML", "SASMLAS", "SOLD"],
                       "security_type": ["common", "common", "common", "common", "etf", "adr", "common", "common"],
                       "is_active": [True, True, True, True, True, True, True, False],
                       "isin": [None] * 8})
    im = pl.DataFrame({"id_type": ["ticker_mic", "ticker_mic", "ticker_mic", "ticker_mic", "yf_ticker", "ticker_mic",
                                   "ticker_mic", "ticker_mic"],
                       "id_value": ["BF.B@XNYS", "BF.A@XNYS", "BH@XNYS", "BH.A@XNYS", "IWM", "ASML@XNAS", "ASML@XAMS",
                                    "OLD@XNAS"],
                       "security_id": ["SBFB", "SBFA", "SBH", "SBHA", "ETF:IWM", "SASML", "SASMLAS", "SOLD"],
                       "valid_to": [None] * 8}, schema_overrides={"valid_to": pl.Date})
    tables = {"securities_master": sm, "identifier_map": im}
    monkeypatch.setattr(U, "scan_table", lambda name, settings=None: tables[name].lazy())
    m = U.listed_symbol_map()
    assert m["BFB"] == "SBFB" and m["BFA"] == "SBFA" and m["BHA"] == "SBHA" and m["BH"] == "SBH"
    assert m["IWM"] == "ETF:IWM"
    assert m["ASML"] == "SASML"            # the US listing (ADR), never the home listing
    assert "OLD" not in m                  # inactive
    df = pl.DataFrame({"symbol": ["BFB", "IWM", "BFAAF", "BHA"], "settlement_date": [date(2026, 9, 30)] * 4,
                       "market_class": ["NYSE", "ARCA", "OTC", "NYSE"]})
    hist = pl.DataFrame({"symbol": ["BHA"], "settlement_date": [date(2026, 9, 15)], "security_id": ["SBHA_OLD"]})
    res = U.resolve_symbols(df, "symbol", "settlement_date", m, None, "market_class").to_list()
    assert res == ["SBFB", "ETF:IWM", None, "SBHA"]
    dated = U.resolve_symbols(df, "symbol", "settlement_date", {}, hist, "market_class").to_list()
    assert dated == [None, None, None, "SBHA_OLD"]     # FTD ticker history within 31 days


def test_finra_market_class_sc_maps_to_nasdaq():
    assert finra_si.MARKET_MIC["SC"] == "XNAS"


# ------------------------------------------------------------------------------------------------ taxonomy


@pytest.mark.parametrize("name, sub", [
    ("의료용품 및 기타 의약 관련제품 제조업", "medtech"),
    ("부동산 임대 및 공급업", "real_estate_development"),
    ("일반 교습 학원", "consumer_services"),
    ("반도체 제조업", "semiconductors"),          # unchanged earlier rule
])
def test_ksic_name_rules_extended(name, sub):
    assert TX.map_ksic(name) == sub


@pytest.mark.parametrize("text, sub", [("Têxtil e Vestuário", "apparel_luxury"), ("Serviços Médicos", "health_providers_services"),
                                       ("Bancos", "banks_regional")])
def test_cvm_keywords_extended(text, sub):
    assert TX.map_cvm(text) == sub


# ------------------------------------------------------------------------------------------------ availability


def _doc() -> dict:
    sweep = {"generated_at": "2026-10-27T00:00:00+00:00", "n": {"full": 100, "mvp_us": 10, "mvp_global": 10},
             "region_n": {"US": 60, "HK": 40},
             "groups": {"roic": "fundamental", "si.days": "smart_money", "px_last": "price"},
             "coverage": {"roic": {"full": 10.0, "mvp_us": 90.0, "mvp_global": 90.0, "regions": {"US": 15.0, "HK": 1.0}},
                          "si.days": {"full": 50.0, "mvp_us": 99.0, "mvp_global": 30.0, "regions": {"US": 99.0, "HK": 0.0}},
                          "px_last": {"full": 100.0, "mvp_us": 100.0, "mvp_global": 100.0, "regions": {"US": 100.0, "HK": 100.0}}}}
    return C.build(sweep)


def test_classification_codes_and_sync_mapping():
    doc = _doc()
    rules = doc["rules"]
    roic = doc["fields"]["roic"]
    assert rules[roic["regions"]["US"]["rule"]]["reason"] == "out_of_scope"
    assert rules[roic["regions"]["HK"]["rule"]]["reason"] == "no_source"
    assert "regions" not in doc["fields"]["px_last"]
    assert rules[doc["fields"]["si.days"]["regions"]["HK"]["rule"]]["reason"] == "not_applicable"
    assert set(doc["reason_codes"]) == set(AV.REASONS)
    assert {v["sync_code"] for v in doc["reason_codes"].values()} <= set(AV.SYNC_CODES)
    md = C.markdown(doc)
    assert "## Fundamentals" in md and "out_of_scope" in md


def test_field_reason_entity_context():
    doc = _doc()
    us = {"region": "US", "has_fundamentals": True, "in_fund_scope": True}
    assert AV.field_reason("roic", {**us, "flags": {"roic_flag"}}, doc=doc).code == "suppressed"
    stale = AV.field_reason("roic", {**us, "px_date": date(2026, 1, 2), "region_px_date": date(2026, 10, 23)}, doc=doc)
    assert stale.code == "stale" and "2026-01-02" in stale.message
    assert AV.field_reason("roic", {"region": "US", "has_fundamentals": False}, doc=doc).code == "out_of_scope"
    assert AV.field_reason("roic", {"region": "HK", "has_fundamentals": False}, doc=doc).code == "no_source"
    # statements loaded: a region-level "no source" does not apply to this company
    assert AV.field_reason("roic", {"region": "HK", "has_fundamentals": True, "in_fund_scope": True}, doc=doc).code \
        == "not_applicable"
    assert AV.field_reason("si.days", {"region": "HK"}, doc=doc).code == "not_applicable"
    with pytest.raises(ValueError):
        AV.make("bogus")


def test_reason_public_shape():
    p = AV.make("needs_key", "Needs OPENDART_APIKEY").public()
    assert p == {"code": "needs_key", "sync_code": "needs_key", "label": "Needs API key",
                 "message": "Needs OPENDART_APIKEY", "next_expected": None, "detail": None}


def test_published_field_availability_is_consistent():
    """contracts/field_availability.yaml: every rule id resolves, codes are known, BQL fields covered."""
    from app.core.config import get_settings

    doc = AV.load(get_settings())
    if not doc:
        pytest.skip("field_availability.yaml not generated")
    for f, e in doc["fields"].items():
        for rg, v in (e.get("regions") or {}).items():
            assert v["rule"] in doc["rules"], (f, rg)
            assert doc["rules"][v["rule"]]["reason"] in AV.REASONS
    assert {"roic", "si.pct_float", "sector"} <= set(doc["fields"])
    _ = timedelta  # noqa: F841


def test_short_interest_backfill_relists_adr_and_guards_ticker_reuse(monkeypatch):
    """identity backfill si(): ADR row moved from the home listing to the US line (same issuer); a row before the
    security's first bar (ticker reuse) goes back to the provisional key; OTC rows stay unresolved."""
    from app.core import storage as ST
    from app.normalize import identity as I

    sm = pl.DataFrame({"security_id": ["SADR", "SHOME", "SNEW"], "issuer_id": ["I1", "I1", "I2"]})
    px = pl.DataFrame({"security_id": ["SADR", "SHOME", "SNEW"] * 2,
                       "date": [date(2020, 1, 2), date(2020, 1, 2), date(2025, 6, 1)] + [date(2026, 10, 26)] * 3})
    tables = {"securities_master": sm, "prices_daily": px}
    monkeypatch.setattr(ST, "scan_table", lambda name, settings=None: tables[name].lazy())
    monkeypatch.setattr(U, "listed_symbol_map", lambda settings=None: {"ASML": "SADR", "NEWCO": "SNEW"})
    monkeypatch.setattr(U, "history_map", lambda settings=None: pl.DataFrame(
        schema={"symbol": pl.Utf8, "settlement_date": pl.Date, "security_id": pl.Utf8}))
    monkeypatch.setattr(I, "lookup_many", lambda id_type, values, *a, **k: pl.DataFrame(
        schema={"input_value": pl.Utf8, "security_id": pl.Utf8, "issuer_id": pl.Utf8}))
    monkeypatch.setattr(I, "clear_cache", lambda: None)
    t0 = datetime_utc = __import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").timezone.utc)
    df = pl.DataFrame({
        "security_id": ["SHOME", "SNEW", "finra:FOOF"], "settlement_date": [date(2026, 9, 30), date(2023, 3, 15), date(2026, 9, 30)],
        "symbol": ["ASML", "NEWCO", "FOOF"], "market_class": ["NNM", "NYSE", "OTC"],
        "security_id_resolved": [True, True, False], "short_interest_shares": [1.0, 2.0, 3.0],
        "ingested_at": [t0, t0, t0]})
    seen = {}

    def fake_rewrite(table, transform, settings=None):
        if table != "short_interest":
            return 0
        new, n = transform(df)
        seen["new"], seen["n"] = new, n
        return n

    monkeypatch.setattr(I, "_rewrite_partitions", fake_rewrite)
    I.backfill_tables()
    new = seen["new"].sort("short_interest_shares")
    assert new.columns == df.columns
    assert new["security_id"].to_list() == ["SADR", "finra:NEWCO", "finra:FOOF"]
    assert new["security_id_resolved"].to_list() == [True, False, False]
    assert seen["n"] == 2
    _ = datetime_utc
