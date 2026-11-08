"""EU universe expansion (docs/EU_EXPANSION_PLAN.md): listing selection, symbol mapping, seed dedupe (no network)."""

from __future__ import annotations

import polars as pl

from app.ingestion import eu_expansion as eu
from app.normalize import identity


def _rec(exch, ticker, st="Common Stock", sector="Equity"):
    return {"exchCode": exch, "ticker": ticker, "securityType": st, "marketSector": sector, "figi": f"BBG{exch}{ticker}"}


def test_issuer_country_maps_crown_dependencies():
    assert eu.issuer_country("JE00B4T3BW64") == "GB"
    assert eu.issuer_country("FR0000121014") == "FR"
    assert eu.issuer_country("LI0315487269") == "CH"
    assert eu.issuer_country("US0378331005") is None


def test_bbg_to_yf_candidates():
    assert eu.bbg_to_yf_candidates("MC", "FP") == ["MC.PA"]
    assert eu.bbg_to_yf_candidates("BT/A", "LN") == ["BT-A.L"]
    assert eu.bbg_to_yf_candidates("RR/", "LN") == ["RR.L"]
    assert eu.bbg_to_yf_candidates("ERICB", "SS") == ["ERICB.ST", "ERIC-B.ST"]
    assert eu.bbg_to_yf_candidates("NOVOB", "DC") == ["NOVOB.CO", "NOVO-B.CO"]
    assert eu.bbg_to_yf_candidates("NESN", "SE") == ["NESN.SW"]
    assert eu.bbg_to_yf_candidates("X", "ZZ") == []


def test_pick_home_listing_prefers_home_exchange():
    recs = [_rec("GF", "SAP"), _rec("GY", "SAP"), _rec("US", "SAP", st="ADR"), _rec("LN", "0NW4")]
    best = eu.pick_home_listing(recs, "DE", "XETR")
    assert best["_exch"] == "GY" and best["ticker"] == "SAP"


def test_pick_home_listing_uses_firds_venue_for_foreign_incorporated():
    # NL-incorporated, listed in Milan (e.g. Ferrari / Stellantis pattern)
    recs = [_rec("IM", "RACE"), _rec("GY", "2FE"), _rec("US", "RACE")]
    assert eu.pick_home_listing(recs, "NL", "MTAA")["_exch"] == "IM"


def test_pick_home_listing_rejects_mtf_only_and_non_equity():
    # IE-incorporated US company traded in Europe only on MTFs -> not a primary European listing
    assert eu.pick_home_listing([_rec("GF", "LIN"), _rec("US", "LIN")], "IE", "FRAB") is None
    assert eu.pick_home_listing([_rec("FP", "MC", st="Warrant", sector="Equity"),
                                 _rec("FP", "MC", st="Common Stock", sector="Corp")], "FR", "XPAR") is None


def test_match_flags():
    r = {"isin": "FR0000121014", "yf_ticker": "MC.PA", "lei": "IOG4E947OATN6RYGZD80"}
    f = eu._match_flags(r, {"yf:MC.PA"}, {"isin:FR0000121014"})
    assert f == {"seed_match": True, "ref_match": True}
    assert eu._match_flags(r, set(), set()) == {"seed_match": False, "ref_match": False}


def _seed_df(rows):
    cols = ["ticker", "mic", "yf_ticker", "isin", "name", "country", "region", "adr_ticker", "issuer_group",
            "share_class", "notes", "lei", "fye_mmdd", "lei_source"]
    return pl.DataFrame([{c: r.get(c) for c in cols} for r in rows], schema={c: pl.Utf8 for c in cols})


def test_eu_broad_records_dedupes_against_curated_seeds():
    curated = identity._seed_records(_seed_df([
        {"ticker": "MC", "mic": "XPAR", "yf_ticker": "MC.PA", "name": "LVMH", "country": "FR", "region": "EU"}]).drop("lei_source"))
    curated[0]["isin"], curated[0]["lei"] = "FR0000121014", "IOG4E947OATN6RYGZD80"  # as after FIRDS/GLEIF enrichment
    eu_df = _seed_df([
        {"ticker": "MC", "mic": "XPAR", "yf_ticker": "MC.PA", "isin": "FR0000121014", "name": "LVMH", "country": "FR",
         "region": "EU", "lei": "IOG4E947OATN6RYGZD80", "lei_source": "gleif_isin"},
        {"ticker": "SOI", "mic": "XPAR", "yf_ticker": "SOI.PA", "isin": "FR0013227113", "name": "Soitec", "country": "FR",
         "region": "EU", "lei": "969500LZ3ZRD4H5MOV63", "lei_source": "esma_firds"},
        {"ticker": "SOI2", "mic": "XPAR", "yf_ticker": "SOI2.PA", "isin": "FR0013227113", "name": "dup", "country": "FR",
         "region": "EU", "lei": "969500LZ3ZRD4H5MOV63", "lei_source": "esma_firds"},
    ])
    new = identity.eu_broad_records(curated, None, eu_df)
    assert curated[0].get("eu_broad") is True and not curated[0].get("eu_broad_only")
    assert [r["local_symbol"] for r in new] == ["SOI"]
    assert new[0]["eu_broad_only"] and new[0]["lei_src"] == "seed_esma_firds" and new[0]["seed_order"] == 1
    assert identity.eu_broad_records(curated, None, None) == []


def test_eu_venues_in_mic_table():
    from app.normalize.identity_figi import mic_info

    for mic, (m, suf) in {"XPRA": ("XPRA", ".PR"), "XBUD": ("XBUD", ".BD")}.items():
        assert mic_info(mic)["yf"] == suf
    for ex, (mic, _suf) in eu.BBG_VENUE.items():
        assert mic_info(mic)["country"], ex
