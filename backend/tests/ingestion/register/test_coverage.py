"""Coverage v2 W2: regulator coverage report - point-in-time statement selection, reasons per missing cell, summary,
docs / field_availability writers, the gap check and the universe rule (no network)."""

from __future__ import annotations

import json
from datetime import date

import polars as pl

from app.ingestion import regulatory_register as R
from app.qa import regulatory_coverage as Q

AS_OF = date(2026, 10, 31)


def _reg(rows):
    base = {k: None for k in R.REGISTER_SCHEMA}
    return pl.DataFrame([{**base, "status": "active", "as_of": AS_OF, **r} for r in rows], schema=R.REGISTER_SCHEMA)


def test_latest_statements_is_point_in_time():
    canon = pl.DataFrame({
        "issuer_id": ["I1"] * 5 + ["I2"],
        "item_id": ["revenue"] * 6,
        "period_kind": ["FY", "FY", "Q", "H", "FY", "FY"],
        "fiscal_period": ["FY", "FY", "Q2", "H1", "FY", "FY"],
        "period_end": [date(2024, 12, 31), date(2025, 12, 31), date(2026, 6, 30), date(2026, 6, 30),
                       date(2026, 9, 30), date(2025, 12, 31)],
        "source_id": ["sec_edgar", "sec_edgar", "sec_edgar", "esef_xbrl_org", "sec_edgar", "yfinance"],
        "knowledge_date": [date(2025, 2, 1), date(2026, 2, 20), date(2026, 8, 1), date(2026, 9, 1),
                           date(2026, 11, 15), date(2026, 3, 1)],     # the FY 2026-09 row is published after AS_OF
    })
    fy, q = Q.latest_statements(canon, AS_OF)
    f = dict(zip(fy["issuer_id"].to_list(), fy["period_end"].to_list(), strict=True))
    assert f == {"I1": date(2025, 12, 31), "I2": date(2025, 12, 31)}
    assert q.filter(pl.col("issuer_id") == "I1")["period_end"][0] == date(2026, 6, 30)
    fy_early, _ = Q.latest_statements(canon, date(2026, 1, 31))
    assert fy_early.filter(pl.col("issuer_id") == "I1")["period_end"][0] == date(2024, 12, 31)


def _inputs():
    reg = _reg([
        {"market": "US", "register": "sec_listed", "issuer_key": "C1", "issuer_name": "Fresh Inc", "cik": "C1",
         "issuer_id": "I1", "security_id": "S1", "filer_category": "domestic", "last_annual_form": "10-K",
         "last_annual_filed": date(2026, 2, 20), "last_interim_filed": date(2026, 8, 1)},
        {"market": "US", "register": "sec_listed", "issuer_key": "C2", "issuer_name": "NoXbrl Inc", "cik": "C2",
         "issuer_id": "I2", "security_id": "S2", "filer_category": "domestic", "last_annual_form": "10-K",
         "last_annual_filed": date(2026, 3, 1), "last_interim_filed": date(2026, 8, 1)},
        {"market": "US", "register": "sec_listed", "issuer_key": "C3", "issuer_name": "FPI plc", "cik": "C3",
         "issuer_id": "I3", "security_id": "S3", "filer_category": "foreign_private_20f", "last_annual_form": "20-F"},
        {"market": "EU", "register": "esma_rm", "issuer_key": "L1", "issuer_name": "Euro SA", "lei": "L1",
         "issuer_id": "I4", "security_id": "S4"},
        {"market": "UK", "register": "lse_aim", "issuer_key": "L2", "issuer_name": "Aimco plc", "lei": "L2"},
        {"market": "EU", "register": "esma_rm", "issuer_key": "L3", "issuer_name": "Zagreb dd", "lei": "L3"},
        {"market": "US", "register": "sec_listed", "issuer_key": "C9", "issuer_name": "Otc Co", "cik": "C9",
         "status": "excluded", "status_reason": "OTC quotation only (not exchange-listed)"},
    ])
    sec = pl.DataFrame({"security_id": ["S1", "S2", "S3", "S4"], "issuer_id": ["I1", "I2", "I3", "I4"],
                        "has_symbol": [True, True, True, False]})
    iss = pl.DataFrame({"issuer_id": ["I1", "I2", "I3", "I4"], "region": ["US", "US", "US", "EU"],
                        "sector_id": ["TECH", None, "ENRG", None], "sic_code": ["7372", "6770", None, None]})
    px = pl.DataFrame({"security_id": ["S1", "S2", "S3"], "last_date": [date(2026, 10, 30), date(2026, 10, 30), date(2026, 8, 1)]})
    fy = pl.DataFrame({"issuer_id": ["I1", "I3", "I4"], "period_end": [date(2025, 12, 31), date(2024, 12, 31), date(2025, 12, 31)],
                       "source_id": ["sec_edgar", "sec_fsds", "yfinance"]})
    q = pl.DataFrame({"issuer_id": ["I1"], "period_end": [date(2026, 6, 30)], "source_id": ["sec_edgar"]})
    extra = {"unresolved": {"L3": "no_source: no free daily price source for Zagreb (Yahoo has no market there)"},
             "raw_entities": {"C1", "C3"}, "seeded": {"L2"}, "esef_leis": set()}
    return reg, sec, iss, px, fy, q, {"US": date(2026, 10, 30), "EU": date(2026, 10, 30)}, extra


def test_classify_reasons_for_every_missing_cell():
    reg, sec, iss, px, fy, q, last, extra = _inputs()
    cov = Q.classify(reg, sec, iss, px, fy, q, AS_OF, last, extra)
    assert cov.height == 6                                    # the excluded OTC row is not in the denominator
    r = {x["issuer_key"]: x for x in cov.iter_rows(named=True)}
    assert r["C1"]["in_universe"] and r["C1"]["has_prices"] and r["C1"]["has_annual"] and r["C1"]["has_interim"]
    assert r["C1"]["annual_official"] is True and all(r["C1"][k] is None for k in
                                                       ("reason_universe", "reason_prices", "reason_annual", "reason_interim", "reason_sector"))
    assert r["C2"]["reason_annual"].startswith("no_source: no XBRL") and r["C2"]["reason_interim"].startswith("no_source")
    assert r["C2"]["reason_sector"].startswith("not_applicable: blank-check")
    assert r["C3"]["reason_annual"].startswith("stale: latest annual FY 2024-12-31")
    assert r["C3"]["reason_interim"].startswith("not_applicable: foreign private issuer")
    assert r["C3"]["reason_prices"].startswith("stale: last bar 2026-08-01")
    assert r["L1"]["has_annual"] and r["L1"]["annual_official"] is False             # Yahoo fallback is not official
    assert r["L1"]["reason_prices"].startswith("no_source") and r["L1"]["reason_sector"].startswith("mapping_gap")
    assert r["L1"]["reason_interim"].startswith("no_source: half-year reports")
    assert r["L2"]["reason_universe"].startswith("due: added to seeds")
    assert r["L3"]["reason_universe"].startswith("no_source: no free daily price source for Zagreb")
    for x in cov.iter_rows(named=True):                       # every missing cell carries a reason
        for cell, reason in (("in_universe", "reason_universe"), ("has_prices", "reason_prices"),
                             ("has_annual", "reason_annual"), ("has_interim", "reason_interim"),
                             ("has_sector", "reason_sector")):
            assert x[cell] or x[reason], (x["issuer_key"], cell)


def test_annual_window_by_register_deadline():
    reg = _reg([{"market": "UK", "register": "lse_aim", "issuer_key": "A", "issuer_name": "A", "issuer_id": "IA",
                 "security_id": "SA"},
                {"market": "UK", "register": "fca_official_list", "issuer_key": "B", "issuer_name": "B",
                 "issuer_id": "IB", "security_id": "SB"}])
    sec = pl.DataFrame({"security_id": ["SA", "SB"], "issuer_id": ["IA", "IB"], "has_symbol": [True, True]})
    iss = pl.DataFrame({"issuer_id": ["IA", "IB"], "region": ["UK", "UK"], "sector_id": ["X", "X"], "sic_code": [None, None]})
    fy = pl.DataFrame({"issuer_id": ["IA", "IB"], "period_end": [date(2025, 5, 31), date(2025, 5, 31)],
                       "source_id": ["esef_xbrl_org", "esef_xbrl_org"]})
    empty = pl.DataFrame(schema={"issuer_id": pl.Utf8, "period_end": pl.Date, "source_id": pl.Utf8})
    px = pl.DataFrame({"security_id": ["SA", "SB"], "last_date": [AS_OF, AS_OF]})
    cov = Q.classify(reg, sec, iss, px, fy, empty, AS_OF, {"UK": AS_OF}, {})
    got = dict(zip(cov["issuer_key"].to_list(), cov["has_annual"].to_list(), strict=True))
    assert got == {"A": True, "B": False}                    # AIM has 6 months to publish, the Official List 4


def test_summary_markdown_and_field_block(tmp_path):
    reg, sec, iss, px, fy, q, last, extra = _inputs()
    cov = Q.classify(reg, sec, iss, px, fy, q, AS_OF, last, extra)
    summ = Q.summarize(cov, reg)
    assert summ["US"]["register_issuers"] == 3 and summ["US"]["with_annual"] == 1
    assert summ["US"]["excluded"] == {"OTC quotation only": 1}
    assert summ["US"]["pct"]["annual"] == 33.3 and not summ["US"]["targets"]["annual_met"]
    md = Q.markdown(summ, {"US": {"pct": {"annual": 10.0, "interim": 5.0, "universe": 90.0, "prices": 90.0, "sector": 50.0}}},
                    AS_OF.isoformat())
    assert md.startswith(Q.MD_START) and md.rstrip().endswith(Q.MD_END)
    assert "(before 10.0)" in md and "| US | annual |" in md
    doc = tmp_path / "DATA_COMPLETENESS.md"
    doc.write_text("# Data completeness\n\n## UI: every empty value explains itself\n\ntext\n", encoding="utf-8")

    class S:  # minimal settings stand-in for write_markdown
        contracts_dir = tmp_path / "contracts"

    (tmp_path / "contracts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DATA_COMPLETENESS.md").write_text(doc.read_text(encoding="utf-8"), encoding="utf-8")
    p = Q.write_markdown(md, S)
    txt = p.read_text(encoding="utf-8")
    assert txt.index("## Regulator coverage") < txt.index("## UI: every empty value explains itself")
    Q.write_markdown(md.replace("33.3", "44.4"), S)            # re-run replaces the section in place
    txt2 = p.read_text(encoding="utf-8")
    assert txt2.count("## Regulator coverage") == 1 and "44.4" in txt2
    (tmp_path / "contracts" / "field_availability.yaml").write_text("version: 1\nfields: {}\n", encoding="utf-8")
    Q.write_field_availability(Q.field_block(summ, AS_OF.isoformat()), S)
    import yaml

    fa = yaml.safe_load((tmp_path / "contracts" / "field_availability.yaml").read_text(encoding="utf-8"))
    assert fa["fields"] == {} and fa["regulator_coverage"]["markets"]["US"]["register_issuers"] == 3


def test_gap_check_register_coverage(tmp_data):
    from app.ops import gaps as G

    st_json = {"as_of": AS_OF.isoformat(), "summary": {"US": {
        "register_issuers": 100, "with_annual": 96, "with_interim": 70, "pct": {"annual": 96.0, "interim": 70.0, "universe": 99.0},
        "top_notes": {"interim": [["not_applicable: foreign private issuer", 20]]}}}}
    (tmp_data.state_dir / "regulatory_coverage.json").write_text(json.dumps(st_json), encoding="utf-8")
    cx = G.Ctx(tmp_data, None, AS_OF, None, None, {})
    st = G.DatasetStatus("statements.register.US", "x", "fundamentals")
    spec = {"id": "statements.register.US", "market": "US", "sla": 2, "entities": {"annual_target": 95, "interim_target": 85}}
    gaps = G.CHECKS["register_coverage"](cx, spec, st)
    assert [g.gap_id for g in gaps] == ["statements.register.US|US.interim|interim"]
    assert st.status == "partial" and st.entities["annual"]["pct"] == 96.0
    old = dict(st_json, as_of="2026-10-01")
    (tmp_data.state_dir / "regulatory_coverage.json").write_text(json.dumps(old), encoding="utf-8")
    st2 = G.DatasetStatus("statements.register.US", "x", "fundamentals")
    gaps2 = G.CHECKS["register_coverage"](cx, spec, st2)
    assert any(g.gap_class == "stale" for g in gaps2)


def test_register_securities_rule(tmp_data, monkeypatch):
    from app.core.storage import write_table
    from app.normalize.identity import register_securities

    reg = _reg([{"market": "US", "register": "sec_listed", "issuer_key": "0000000001", "issuer_name": "A", "cik": "0000000001"},
                {"market": "US", "register": "sec_listed", "issuer_key": "0000000002", "issuer_name": "Spac", "cik": "0000000002"},
                {"market": "EU", "register": "esma_rm", "issuer_key": "LEI1", "issuer_name": "B", "lei": "LEI1"},
                {"market": "EU", "register": "esma_rm", "issuer_key": "LEI9", "issuer_name": "Gone", "lei": "LEI9",
                 "status": "removed"}])
    write_table(reg.with_columns(pl.lit(None, pl.Datetime("us", "UTC")).alias("updated_at")).with_columns(
        pl.col("updated_at").fill_null(pl.lit(AS_OF).cast(pl.Datetime("us", "UTC")))), "regulatory_register",
        mode="overwrite", settings=tmp_data)
    issuers = pl.DataFrame({"issuer_id": ["IA", "IS", "IB", "IG"], "cik": ["0000000001", "0000000002", None, None],
                            "lei": [None, None, "LEI1", "LEI9"], "is_active": [True, True, True, True]})
    prim = pl.DataFrame({"security_id": ["SA", "SS", "SB", "SG"], "issuer_id": ["IA", "IS", "IB", "IG"],
                         "sic_code": ["7372", "6770", None, None], "security_type": ["common"] * 4})
    got = register_securities(issuers, prim, tmp_data)
    assert got == {"SA": "sec_listed", "SB": "esma_rm"}         # SPAC (SIC 6770) and removed issuers stay out
    monkeypatch.setenv("BIG360_REGISTER_UNIVERSE", "0")
    assert register_securities(issuers, prim, tmp_data) == {}
