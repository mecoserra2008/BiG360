"""Regression tests for the Phase 3 final bug-hunt list (docs/PHASE3_TODO.md)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ 1. change_7d vs partially-ingested windows


class _FakeDb:
    def __init__(self, counts: dict[date, int]):
        self.counts = counts

    def all(self, sql, params):  # noqa: ANN001
        lo, hi = params
        return [{"d": d, "n": n} for d, n in self.counts.items() if lo < d <= hi]


def test_change_7d_null_when_prior_week_not_ingested():
    from app.api.routers.events import _covered_windows

    a = date(2026, 10, 9)
    # full ingestion only from a-6 (prior week = sparse back-fill with ~1% of the volume) -> prior window not covered
    sparse = {a - timedelta(days=k): (85_000 if k <= 6 else 500) for k in range(14)}
    assert _covered_windows(_FakeDb(sparse), a) == (True, False)
    full = {a - timedelta(days=k): 80_000 + 1000 * k for k in range(14)}
    assert _covered_windows(_FakeDb(full), a) == (True, True)
    # the as_of day itself may be partially ingested
    full[a] = 3_000
    assert _covered_windows(_FakeDb(full), a) == (True, True)


# ------------------------------------------------------------------ 1/2. api.yaml documents the scales


def _api_schema(name: str) -> dict:
    doc = yaml.safe_load((ROOT / "contracts" / "api.yaml").read_text(encoding="utf-8"))
    return doc["components"]["schemas"][name]["properties"]


def test_api_documents_sentiment_and_change_scales():
    s = _api_schema("CompanySentiment")["sentiment_score"]
    assert s["minimum"] == 0 and s["maximum"] == 100 and "percentile" in s["description"]
    n = _api_schema("NewsItem")["sentiment_score"]
    assert n["minimum"] == -1 and n["maximum"] == 1
    assert "fraction" in _api_schema("CountryEventAgg")["change_7d"]["description"]


# ------------------------------------------------------------------ 5. GDELT slug headlines


@pytest.mark.parametrize("url,expected", [
    ("https://www.salisburyjournal.co.uk/news/26612665.peter-egan-joins-wiltshire-premiere-unbreakable-film/",
     "Peter Egan Joins Wiltshire Premiere Unbreakable Film"),
    ("http://www.bignewsnetwork.com/news/279362841/1st-ld-writethru-us-sanctions-on-icc-draw-criticism-from-un-member-states",
     "US Sanctions on ICC Draw Criticism from UN Member States"),
    ("https://x.com/2026/10/09/fed-holds-rates-steady.html", "Fed Holds Rates Steady"),
    ("https://www.fox13now.com/news/local/50k-approved-as-flooded-families-await-a-way-home",
     "50k Approved as Flooded Families Await a Way Home"),
    ("https://example.com/a/b", None),
    ("https://x.com/news/2026-10-08-police-investigate-possible-serial-killer", "Police Investigate Possible Serial Killer"),
    ("https://x.com/2026/10/08/5-things-to-know-today", "5 Things to Know Today"),
])
def test_headline_from_url_strips_ids_and_title_cases(url, expected):
    from app.api.routers.events import headline_from_url as api_h
    from app.nlp.gdelt_link import headline_from_url

    assert headline_from_url(url) == expected
    assert api_h(url) == expected  # one implementation


def test_stored_slug_headline_is_cleaned_at_read_time():
    from app.api.routers.events import clean_headline

    url = "https://www.bucksfreepress.co.uk/news/26618914.buckinghamshires-terror-links-include-isis-recruits/"
    assert clean_headline("26618914.buckinghamshires terror links include isis recruits", url) == \
        "Buckinghamshires Terror Links Include Isis Recruits"
    assert clean_headline("Real GKG title", url) == "Real GKG title"


# ------------------------------------------------------------------ 6. freshness date column


def test_freshness_date_column_never_uses_valid_from():
    from app.api.serving import freshness_date_column
    from app.core.schema import load_contracts

    c = load_contracts()
    for t in ("listings", "identifier_map", "universe_membership", "securities_master", "issuers"):
        assert freshness_date_column(t, set(c[t].column_names)) == "updated_at"
    assert freshness_date_column("prices_daily", set(c["prices_daily"].column_names)) == "date"
    assert all(freshness_date_column(n, set(t.column_names)) != "valid_from" for n, t in c.items())


# ------------------------------------------------------------------ 7. ISIN from CUSIP


def test_isin_from_cusip():
    from app.normalize.identity import isin_from_cusip
    from app.normalize.identity_universe import is_valid_isin

    assert isin_from_cusip("037833100", "US") == "US0378331005"   # AAPL
    assert isin_from_cusip("892331307", "JP") == "US8923313071"   # TM ADR -> US ISIN
    assert isin_from_cusip("82509L107", "CA") == "CA82509L1076"   # SHOP
    assert isin_from_cusip("G1151C101", "IE") is None             # CINS: domicile prefix unknown
    assert isin_from_cusip("037833101", "US") is None             # bad CUSIP check digit
    assert is_valid_isin(isin_from_cusip("594918104", "US"))      # MSFT


def _write(df: pl.DataFrame, table: str, s) -> None:  # noqa: ANN001
    from app.core.schema import conform
    from app.core.storage import write_table

    write_table(conform(df, table, strict=False), table, mode="overwrite", settings=s)


def test_derive_isins_from_cusips(tmp_data):
    from app.normalize.identity import derive_isins_from_cusips

    now = datetime.now(UTC)
    _write(pl.DataFrame([
        {"id_type": "cusip", "id_value": "037833100", "entity_level": "security", "issuer_id": "I1", "security_id": "S1",
         "valid_from": date(1900, 1, 1), "source_id": "sec_ftd", "confidence": 0.95, "updated_at": now},
        {"id_type": "cusip", "id_value": "594918104", "entity_level": "security", "issuer_id": "I2", "security_id": "S2",
         "valid_from": date(1900, 1, 1), "source_id": "sec_ftd", "confidence": 0.95, "updated_at": now},
    ]), "identifier_map", tmp_data)
    _write(pl.DataFrame([
        {"security_id": "S1", "issuer_id": "I1", "security_type": "common", "primary_listing_id": "L1", "display_ticker": "AAPL",
         "name": "Apple", "trading_currency": "USD", "is_primary_security": True, "is_active": True, "updated_at": now},
        {"security_id": "S2", "issuer_id": "I2", "security_type": "common", "primary_listing_id": "L2", "display_ticker": "MSFT",
         "name": "Microsoft", "trading_currency": "USD", "is_primary_security": True, "is_active": True, "updated_at": now,
         "isin": "US5949181045"},
    ]), "securities_master", tmp_data)
    _write(pl.DataFrame([{"issuer_id": i, "name": n, "country": "US", "region": "US", "is_active": True, "updated_at": now}
                         for i, n in (("I1", "Apple"), ("I2", "Microsoft"))]), "issuers", tmp_data)
    rows = derive_isins_from_cusips(tmp_data)
    assert rows == [{"id_type": "isin", "id_value": "US0378331005", "entity_level": "security", "issuer_id": "I1",
                     "security_id": "S1", "source_id": "derived_cusip", "confidence": 0.95}]  # MSFT already has one


# ------------------------------------------------------------------ 3. ADR ratio seed


def test_adr_ratio_seed_applied_to_securities(tmp_data):
    from app.normalize.identity import apply_adr_ratio_seed
    from app.normalize.identity_universe import seeds_dir

    seeds_dir(tmp_data).mkdir(parents=True, exist_ok=True)
    (seeds_dir(tmp_data) / "adr_ratios.csv").write_text(
        "adr_ticker,adr_ratio,underlying,verified,note\nTM,10,7203.T,price,x\nPBR-A,2,PETR4.SA,price,x\nMELI,1,,public,x\n",
        encoding="utf-8")
    sec = pl.DataFrame({"display_ticker": ["TM", "PBR.A", "MELI", "AAPL"], "security_type": ["adr", "adr", "common", "common"],
                        "adr_ratio": [None, None, None, None]}, schema_overrides={"adr_ratio": pl.Float64})
    out, n = apply_adr_ratio_seed(sec, tmp_data)
    assert out["adr_ratio"].to_list() == [10.0, 2.0, None, None] and n == 2
    _, n2 = apply_adr_ratio_seed(out, tmp_data)
    assert n2 == 0  # idempotent


def test_repo_adr_seed_covers_requested_adrs():
    from app.normalize.identity_universe import adr_ticker_key, load_adr_ratios

    r = load_adr_ratios()
    if not r:
        pytest.skip("data/reference/seeds/adr_ratios.csv not present")
    expect = {"TM": 10, "PBR": 2, "TSM": 5, "BABA": 8, "NVO": 1, "ASML": 1, "SAP": 1, "BHP": 2, "SONY": 1, "HDB": 3,
              "INFY": 1, "ITUB": 1, "VALE": 1}
    assert {k: r[adr_ticker_key(k)] for k in expect} == expect


# ------------------------------------------------------------------ 4. yfinance currency mislabel (PETR4)


def test_fix_yf_currency_relabels_usd_statements_labelled_brl():
    from app.normalize.fundamentals import fix_yf_currency

    pe = [date(2022, 12, 31), date(2023, 12, 31), date(2024, 12, 31)]
    usd = [124.47e9, 102.41e9, 91.42e9]
    rows = []
    for d, v in zip(pe, usd, strict=True):
        rows += [{"issuer_id": "P", "source_id": "yfinance", "period_end": d, "value": v, "unit": "BRL"},
                 {"issuer_id": "P", "source_id": "yfinance", "period_end": d, "value": 1.5, "unit": "BRL/shares"},
                 {"issuer_id": "P", "source_id": "sec_edgar", "period_end": d, "value": v, "unit": "USD"}]
    rows.append({"issuer_id": "P", "source_id": "cvm", "period_end": pe[2], "value": 490.83e9, "unit": "BRL"})
    # a correctly labelled issuer (values agree in the same currency) is untouched
    rows += [{"issuer_id": "Q", "source_id": "yfinance", "period_end": d, "value": 5e9 + i, "unit": "EUR"} for i, d in enumerate(pe)]
    rows += [{"issuer_id": "Q", "source_id": "esef_xbrl_org", "period_end": d, "value": 5e9 + i, "unit": "EUR"} for i, d in enumerate(pe)]
    out = fix_yf_currency(pl.DataFrame(rows))
    yfp = out.filter((pl.col("issuer_id") == "P") & (pl.col("source_id") == "yfinance"))
    assert set(yfp["unit"].to_list()) == {"USD", "USD/shares"}
    assert set(out.filter(pl.col("issuer_id") == "Q")["unit"].to_list()) == {"EUR"}
    assert out.filter(pl.col("source_id") == "cvm")["unit"].to_list() == ["BRL"]


# ------------------------------------------------------------------ 9. splits, class-equivalent shares, EPS outliers


def test_split_adjust_puts_pre_split_shares_on_current_basis():
    from app.analytics.cost_of_capital import split_adjust

    prices = pl.DataFrame({"security_id": ["S"] * 3, "date": [date(2025, 6, 2), date(2026, 3, 2), date(2026, 4, 6)],
                           "split_factor": [0.04, 0.04, 1.0]})
    issuers = pl.DataFrame({"issuer_id": ["I"], "security_id": ["S"]})
    # BKNG: FY2025 values are pre-split although the wide row's knowledge_date (max over items) is after the split;
    # FY2024 is a restated comparative already on the post-split basis; Q2 2026 is post-split.
    fy = pl.DataFrame({"issuer_id": ["I"] * 3, "period_end": [date(2024, 12, 31), date(2025, 12, 31), date(2026, 6, 30)],
                       "knowledge_date": [date(2026, 8, 4), date(2026, 4, 10), date(2026, 8, 4)],
                       "shares_outstanding": [800.0e6, 31.67e6, 751.4e6], "eps_diluted": [5.9, 165.57, 2.53]})
    out = split_adjust(fy, prices, issuers, date(2026, 10, 9))
    assert out["shares_outstanding"].to_list() == pytest.approx([800.0e6, 791.75e6, 751.4e6])
    assert out["eps_diluted"].to_list() == pytest.approx([5.9, 6.6228, 2.53])
    assert out.columns == fy.columns
    # no post-split period in the panel (FY-only): pre-split rows are adjusted
    only = split_adjust(fy.head(2).tail(1), prices, issuers, date(2026, 10, 9))
    assert only["eps_diluted"][0] == pytest.approx(6.6228)


def test_yf_equivalent_shares_for_multi_class_and_pence():
    from app.normalize.fundamentals import yf_equivalent_shares

    df = pl.DataFrame({"yf_ticker": ["BRK-B", "BP.L", "X"], "sharesOutstanding": [1.408e9, 15.45e9, 1e9],
                       "marketCap": [1.1038e12, 90.6e9, 50e12], "regularMarketPrice": [515.62, 586.3, 10.0],
                       "currency": ["USD", "GBp", "USD"]})
    got = df.select(yf_equivalent_shares()).to_series().to_list()
    assert got[0] == pytest.approx(2.1407e9, rel=1e-3)   # class-B-equivalent total
    assert got[1] == pytest.approx(15.45e9)              # GBp quote handled, single class -> listed count
    assert got[2] == pytest.approx(1e9)                  # implausible ratio -> listed count


def test_market_basics_tiny_profit_is_not_a_share_basis_error():
    from app.analytics.forecast import market_basics, prepare_panel

    # BP FY2025: NI $55M, EPS $0.0034, ~16bn shares, price $7.75 -> P/E ~2,300 but P/B ~1.6
    h = prepare_panel(pl.DataFrame({"issuer_id": ["I"], "fiscal_year": [2025], "eps_diluted": [0.0034],
                                    "net_income_parent": [55e6], "total_equity_parent": [77e9]}))
    ctx = {"price": 7.75, "shares": 16.2e9, "fx_rep_to_px": 1.0, "currency": "USD", "reporting_currency": "USD"}
    mb = market_basics(h, ctx)
    assert "share_basis_mismatch" not in mb["flags"] and "eps_outlier" in mb["flags"]
    # a genuine basis error (post-split price vs pre-split EPS and book/share) is still flagged
    ctx2 = {"price": 160.0, "shares": 30e6, "fx_rep_to_px": 1.0, "currency": "USD", "reporting_currency": "USD"}
    h2 = prepare_panel(pl.DataFrame({"issuer_id": ["I"], "fiscal_year": [2025], "eps_diluted": [200.0],
                                     "net_income_parent": [6e9], "total_equity_parent": [100e9]}))
    assert "share_basis_mismatch" in market_basics(h2, ctx2)["flags"]


# ------------------------------------------------------------------ 8. SIC -> taxonomy back-fill for insider issuers


def test_backfill_sic_taxonomy(tmp_data, monkeypatch):
    from app.normalize import identity_universe
    from app.normalize.identity import backfill_sic_taxonomy

    now = datetime.now(UTC)
    _write(pl.DataFrame([
        {"issuer_id": "I1", "name": "Boise Cascade Co", "country": "US", "region": "US", "is_active": True, "updated_at": now,
         "cik": "0001328581"},
        {"issuer_id": "I2", "name": "No Insiders Inc", "country": "US", "region": "US", "is_active": True, "updated_at": now,
         "cik": "0000000002"},
        {"issuer_id": "I3", "name": "Blank Check Corp", "country": "US", "region": "US", "is_active": True, "updated_at": now,
         "cik": "0000000003"},
    ]), "issuers", tmp_data)
    _write(pl.DataFrame([{"as_of_date": date(2026, 10, 9), "year": 2026, "security_id": f"S{i}", "issuer_id": f"I{i}",
                          "cluster_buy": False, "c_suite_buy": False, "computed_at": now} for i in (1, 3)]),
           "insider_signals", tmp_data)
    asked: list[list[str]] = []

    def fake_subs(ciks, settings=None, budget=None, **kw):  # noqa: ANN001, ANN003
        asked.append(list(ciks))
        return pl.DataFrame({"cik": ["0001328581", "0000000003"], "sic": ["5031", "6770"]})

    monkeypatch.setattr(identity_universe, "fetch_sec_submissions", fake_subs)
    n, classified = backfill_sic_taxonomy(tmp_data)
    assert asked == [["0001328581", "0000000003"]] or sorted(asked[0]) == ["0000000003", "0001328581"]  # only insider issuers
    from app.core.storage import read_table

    iss = {r["issuer_id"]: r for r in read_table("issuers", settings=tmp_data).iter_rows(named=True)}
    assert n == 2 and classified == 1
    assert iss["I1"]["sic_code"] == "5031" and iss["I1"]["sector_id"] and iss["I1"]["classification_source"] == "sic"
    assert iss["I3"]["sic_code"] == "6770" and iss["I3"]["sector_id"] is None  # SPAC stays excluded
    assert iss["I2"]["sic_code"] is None


def test_brand_cue_regexes_have_word_boundaries():
    """gdelt_link shipped facebook/google cues with literal backspace bytes instead of \b -> they never matched."""
    import app.nlp.gdelt_link as gl

    src = Path(gl.__file__).read_bytes()
    assert bytes([8]) not in src
    assert gl.BRAND_CUES["google"].search("Alphabet's Google unveils Gemini")
    assert gl.BRAND_CUES["facebook"].search("posted on Instagram")
