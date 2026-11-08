"""sec_form4: ownership XML parsing edge cases, SGML header, amendments, TSV backfill parity (offline)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import orjson
import polars as pl
import pytest

from app.core.schema import conform
from app.ingestion.base import FetchResult
from app.ingestion.sec_form4 import (
    SecForm4Adapter,
    flag_10b5_1,
    holding_change,
    mark_superseded,
    parse_insider_tsv_zip,
    parse_ownership_xml,
    role_norm,
    split_submission,
    tsv_dataset_links,
    txn_frame,
)

FX = Path(__file__).parent / "fixtures"


def _parse(name: str, **kw):
    m = json.loads((FX / f"{name}.json").read_text())
    rows, meta = parse_ownership_xml((FX / f"{name}.xml").read_bytes(), m["accession"], date.fromisoformat(m["filed"]), **kw)
    return rows, meta, m


def test_derivative_and_nonderivative_rows():
    rows, meta, m = _parse("form4_deriv")
    df = conform(txn_frame(rows), "insider_transactions")
    assert meta["issuer_cik"] == "0000002488" and meta["period"] == date(2026, 8, 9)
    assert df["txn_id"].to_list() == [f"sec_edgar:{m['accession']}:{t}" for t in ("nd:1", "nd:2", "dv:1", "dv:2")]
    nd2 = df.filter(pl.col("txn_id").str.ends_with("nd:2")).row(0, named=True)
    assert nd2["txn_code"] == "F" and nd2["txn_type"] == "tax_withholding" and nd2["acquired_disposed"] == "D"
    assert nd2["price"] == 483.36 and nd2["value_usd"] == pytest.approx(2652 * 483.36)
    assert nd2["holding_change_pct"] == pytest.approx(-2652 / (108210 + 2652))
    dv = df.filter(pl.col("is_derivative"))
    assert (dv["security_title"] == "Restricted Stock Units").all() and (dv["underlying_shares"] > 0).all()
    assert not df["is_open_market"].any()
    assert (df["knowledge_date"] == date(2026, 8, 11)).all()        # filing date, never the trade date
    assert (df["transaction_date"] <= df["knowledge_date"]).all()


def test_price_only_in_footnote_is_null():
    rows, _, _ = _parse("form4_noprice")
    (r,) = rows
    assert r["price"] is None and r["value_local"] is None and r["txn_type"] == "grant"
    assert r["role_norm"] == "director" and r["is_director"] and not r["is_officer"]


def test_multiple_reporting_owners_not_duplicated():
    rows, meta, _ = _parse("form4_multi_owner")
    assert len(meta["owner_ciks"]) == 2 and len(rows) == 2           # one row per transaction, not per owner
    assert all(r["n_reporting_owners"] == 2 for r in rows)
    assert meta["issuer_cik"] == "0000844790"                         # issuer from XML, not the index filer CIK
    buy, sell = rows
    assert buy["txn_type"] == "buy" and buy["is_open_market"] and sell["txn_type"] == "sell"
    assert buy["is_ten_pct_owner"] and buy["role_norm"] == "owner" and buy["ownership_nature"] == "indirect"
    assert sell["price"] == pytest.approx(6.5359)


def test_10b5_1_checkbox_beats_footnote_mention():
    rows, _, _ = _parse("form4_fn10b51")
    # footnote says "in compliance with, but not subject to, the ... Rule 10b5-1 trading plan" and the checkbox is 0
    assert all(r["is_10b5_1"] is False for r in rows)
    assert all(r["txn_type"] == "gift" for r in rows)


def test_flag_10b5_1_rules():
    assert flag_10b5_1(False, [True, False], True) == [False, False]
    assert flag_10b5_1(True, [True, False], False) == [True, False]
    assert flag_10b5_1(True, [False, False], False) == [True, True]
    assert flag_10b5_1(None, [True, False], False) == [True, None]
    assert flag_10b5_1(None, [False], True) == [True]


def test_split_submission_header_and_xml():
    txt = (FX / "submission_form4.txt").read_text(encoding="utf-8")
    hdr, xml = split_submission(txt)
    assert hdr["acceptance"] == "20261009163243" and hdr["form"] == "4" and hdr["period"] == "20261008"
    rows, meta = parse_ownership_xml(xml, "0001624651-26-000053", date(2026, 10, 9))
    assert meta["issuer_cik"] == "0001014473" and rows and rows[0]["issuer_ticker"] == "VRSN"


def _amendment_xml(price: str) -> bytes:
    x = (FX / "form4_noprice.xml").read_text(encoding="utf-8")
    return x.replace("<documentType>4</documentType>", "<documentType>4/A</documentType>").encode()


def test_amendment_supersedes_original():
    o_rows, _, m = _parse("form4_noprice")
    a_rows, _ = parse_ownership_xml(_amendment_xml("1"), "0000050863-26-000999", date(2026, 8, 20), form_type="4/A")
    assert a_rows[0]["is_amendment"] and a_rows[0]["form_type"] == "4/A"
    # original already stored, amendment arrives in a later run
    existing = txn_frame(o_rows)
    out = mark_superseded(txn_frame(a_rows), existing)
    orig = out.filter(pl.col("filing_id") == f"sec_edgar:{m['accession']}")
    amd = out.filter(pl.col("filing_id") == "sec_edgar:0000050863-26-000999")
    assert orig.height == 1 and orig["superseded_by_filing_id"][0] == "sec_edgar:0000050863-26-000999"
    assert amd["superseded_by_filing_id"].is_null().all()
    # same batch
    out2 = mark_superseded(txn_frame(o_rows + a_rows))
    assert out2.filter(pl.col("superseded_by_filing_id").is_not_null()).height == 1
    conform(out, "insider_transactions")


def test_amendment_does_not_supersede_unrelated_rows():
    rows, _, _ = _parse("form4_deriv")
    a_rows, _ = parse_ownership_xml(_amendment_xml("1"), "0000050863-26-000999", date(2026, 8, 20), form_type="4/A")
    out = mark_superseded(txn_frame(a_rows), txn_frame(rows))
    assert out.filter(pl.col("superseded_by_filing_id").is_not_null()).height == 0


def test_tsv_backfill_matches_xml():
    tsv = parse_insider_tsv_zip(str(FX / "insider_tsv_small.zip"))
    df = conform(tsv, "insider_transactions")
    assert df.height > 10 and df["txn_id"].is_unique().all()
    for name in ("form4_deriv", "form4_multi_owner", "form4_fn10b51", "form4_noprice"):
        rows, _, _ = _parse(name)
        x = txn_frame(rows)
        j = x.join(df, on="txn_id", suffix="_t")
        assert j.height == x.height, name                    # same txn_ids from both sources
        for c in ("txn_code", "transaction_date", "insider_id", "acquired_disposed", "is_10b5_1", "is_derivative",
                  "security_title", "n_reporting_owners"):
            assert (j[c] == j[f"{c}_t"]).fill_null(True).all(), (name, c)
        # TSV rounds to 2 decimals
        assert ((j["shares"] - j["shares_t"]).abs() < 0.01).all()


def test_tsv_issuer_filter_and_links():
    tsv = parse_insider_tsv_zip(str(FX / "insider_tsv_small.zip"), issuer_filter={"0000002488"})
    assert set(tsv["source_entity_id"]) == {"0000002488"}
    html = '<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q3_form345.zip">x</a>' \
           '<a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">y</a>'
    links = tsv_dataset_links(html)
    assert links["2026q3"].startswith("https://www.sec.gov/files/datastandardsinnovation/")
    assert links["2026q1"].startswith("https://www.sec.gov/files/structureddata/")


def test_role_and_holding_change():
    assert role_norm(False, True, False, "Chief Executive Officer") == "ceo"
    assert role_norm(True, True, False, "EVP & CFO") == "cfo"
    assert role_norm(True, False, False, None) == "director"
    assert role_norm(False, False, True, None) == "owner"
    assert role_norm(False, True, False, "SVP, General Counsel") == "officer_other"
    assert holding_change(100, 1100, "A") == pytest.approx(0.1)
    assert holding_change(100, 900, "D") == pytest.approx(-0.1)
    assert holding_change(100, 100, "A") is None                     # no prior holdings


def test_adapter_normalize_offline(tmp_data):
    """End-to-end normalize from a landed raw bundle (no network): universe filter, region, filings_index update."""
    st = tmp_data.data_dir / "state"
    st.mkdir(parents=True, exist_ok=True)
    (st / "sec_issuer_meta.json").write_bytes(orjson.dumps({
        "0000002488": {"region": "US"}, "0001321655": {"region": "US"}, "0000050863": {"region": "US"}}))
    recs = []
    for name in ("form4_deriv", "form4_fn10b51", "form4_multi_owner"):
        m = json.loads((FX / f"{name}.json").read_text())
        recs.append({**m, "xml": (FX / f"{name}.xml").read_text(encoding="utf-8")})
    ad = SecForm4Adapter(settings=tmp_data, params={"ciks": ["0000002488", "0001321655", "0000050863"]})
    p = ad.save_raw("form345", "test.jsonl", b"\n".join(orjson.dumps(r) for r in recs))
    res = FetchResult(source_id="sec_edgar", started_at=datetime.now(UTC), raw_paths=[p])
    ad.params = {}  # universe filter active -> the multi-owner fund filing (issuer 0000844790) is dropped
    ad.universe_ciks = lambda params=None: ["0000002488", "0001321655", "0000050863"]  # type: ignore[method-assign]
    out = ad.normalize(res)
    df = conform(out["insider_transactions"], "insider_transactions")
    assert set(df["source_entity_id"]) == {"0000002488", "0001321655"}
    assert res.meta["n_rows_non_universe_issuer"] == 2
    assert df["knowledge_ts"].is_not_null().all()
    assert (df["region"] == "US").all()


# ----------------------------------------------------------------------------------------------- A5 typo dates


@pytest.mark.parametrize("td,filed,period,expect", [
    # real 2026-11 lake cases (EDGAR XML / SEC data sets carry the filer's typo verbatim)
    (date(2047, 6, 7), date(2017, 6, 9), date(2017, 6, 7), (date(2017, 6, 7), "year")),       # PEGA RSU, 2047
    (date(16, 3, 16), date(2016, 3, 18), date(2016, 3, 16), (date(2016, 3, 16), "century")),   # CWT "0016"
    (date(24, 1, 7), date(2024, 1, 9), date(24, 1, 7), (date(2024, 1, 7), "century")),        # period typo'd too
    (date(2027, 2, 17), date(2024, 2, 20), date(2024, 2, 17), (date(2024, 2, 17), "year")),    # LSCC
    (date(2033, 2, 14), date(2023, 2, 15), date(2023, 2, 14), (date(2023, 2, 14), "year")),    # option expiry year
    (date(2026, 12, 31), date(2026, 1, 2), date(2025, 12, 31), (date(2025, 12, 31), "year")),  # year-end grant
    # kept as filed: within 31 days after filing (no unambiguous fix; dq warns), or old but possible
    (date(2026, 11, 20), date(2026, 11, 10), date(2026, 11, 9), (date(2026, 11, 20), None)),
    (date(1987, 10, 7), date(2017, 9, 29), date(1987, 10, 7), (date(1987, 10, 7), None)),
    (date(2026, 8, 9), date(2026, 8, 11), date(2026, 8, 9), (date(2026, 8, 9), None)),
    # Form 4 month slip (NCR 2023-11-03 filed 2023-01-04): no candidate in [period, filed + 1] -> the period of
    # report, i.e. the earliest transaction on the form (labelled approximation)
    (date(2023, 11, 3), date(2023, 1, 4), date(2023, 1, 3), (date(2023, 1, 3), "period_of_report")),
    (date(2032, 2, 29), date(2026, 3, 23), date(2026, 3, 20), (date(2026, 3, 20), "period_of_report")),   # no leap
    # ... only for a form filed at most TXN_PERIOD_FALLBACK_MAX_DAYS (10) after its period
    (date(2023, 11, 3), date(2023, 1, 13), date(2023, 1, 3), (date(2023, 1, 3), "period_of_report")),     # 10 d
    (date(2023, 11, 3), date(2023, 1, 14), date(2023, 1, 3), (None, "dropped")),                          # 11 d
    # A5 review, real lake rows: the period is years before the filing (a late batch, or itself a typo) -> dropped,
    # never stored 5 years off (PRIM 0001179110-19-004698:dv:14 would have become 2014-03-24)
    (date(2019, 9, 28), date(2019, 4, 3), date(2014, 3, 24), (None, "dropped")),
    (date(2025, 7, 17), date(2024, 7, 19), date(2021, 7, 19), (None, "dropped")),     # TWFG: period typo'd 2021
    (date(2019, 7, 25), date(2019, 2, 27), date(2018, 7, 29), (None, "dropped")),     # HON
    # no usable period and no candidate -> dropped
    (date(2032, 2, 29), date(2026, 3, 23), None, (None, "dropped")),
    (date(2029, 2, 19), date(2026, 3, 23), None, (date(2026, 2, 19), "year")),
    (None, date(2026, 8, 11), None, (None, None)),
])
def test_plausible_txn_date(td, filed, period, expect):
    from app.ingestion.sec_form4 import plausible_txn_date

    got = plausible_txn_date(td, filed, period, "4")
    assert got == expect
    if got[0] is not None and got[1]:
        assert got[0] <= filed + timedelta(days=1)          # a correction never moves a trade after it was public


def test_form5_window_is_the_fiscal_year():
    from app.ingestion.sec_form4 import plausible_txn_date

    # Form 5 (ACGL gifts, period = fiscal year end 2022-12-31, filed 2023-02-13): 2023-11-15 -> 2022-11-15
    assert plausible_txn_date(date(2023, 11, 15), date(2023, 2, 13), date(2022, 12, 31), "5") == (date(2022, 11, 15), "year")
    assert plausible_txn_date(date(2023, 11, 15), date(2023, 2, 13), date(2022, 12, 31), "5/A")[1] == "year"
    # two same-month-day candidates in the window and no day-level bound -> dropped (never the period for a Form 5)
    assert plausible_txn_date(date(2025, 1, 10), date(2023, 2, 13), date(2022, 12, 31), "5") == (None, "dropped")


def test_xml_typo_dates_are_corrected_or_dropped():
    m = json.loads((FX / "form4_deriv.json").read_text())
    x = (FX / "form4_deriv.xml").read_text(encoding="utf-8")
    for typo in ("2036-08-09", "0026-08-09", "2026-12-25"):           # nd:1, nd:2, dv:1; dv:2 stays as filed
        x = x.replace("<value>2026-08-09</value>", f"<value>{typo}</value>", 1)
    rows, meta = parse_ownership_xml(x.encode(), m["accession"], date.fromisoformat(m["filed"]))
    assert [r["txn_id"].rsplit(":", 2)[-2:] for r in rows] == [["nd", "1"], ["nd", "2"], ["dv", "1"], ["dv", "2"]]
    assert {r["transaction_date"] for r in rows} == {date(2026, 8, 9)} and meta["n_skipped"] == 0
    assert meta["period"] == date(2026, 8, 9)
    assert [(f["filed"], f["stored"], f["fix"]) for f in meta["date_fixes"]] == [
        (date(2036, 8, 9), date(2026, 8, 9), "year"), (date(26, 8, 9), date(2026, 8, 9), "century"),
        (date(2026, 12, 25), date(2026, 8, 9), "period_of_report")]
    # the label is in the row (A5 review): the fix and the date as filed; null on the row stored as filed
    assert [(r["transaction_date_fix"], r["transaction_date_as_filed"]) for r in rows] == [
        ("year", date(2036, 8, 9)), ("century", date(26, 8, 9)), ("period_of_report", date(2026, 12, 25)),
        (None, None)]
    df = conform(txn_frame(rows), "insider_transactions")      # contract columns + enum values
    assert df.filter(pl.col("transaction_date_fix") == "period_of_report")["transaction_date"].to_list() == [
        date(2026, 8, 9)]
    assert (df["knowledge_date"] == date(2026, 8, 11)).all()   # knowledge_date never changes
    # a period years before the filing gives no approximation: the certainly-wrong row is dropped (counted)
    z = (FX / "form4_deriv.xml").read_text(encoding="utf-8")
    z = z.replace("<periodOfReport>2026-08-09</periodOfReport>", "<periodOfReport>2021-08-09</periodOfReport>")
    z = z.replace("<value>2026-08-09</value>", "<value>2026-12-25</value>", 1)
    rows, meta = parse_ownership_xml(z.encode(), m["accession"], date.fromisoformat(m["filed"]))
    assert meta["period"] == date(2021, 8, 9) and meta["n_skipped"] == 1 and len(rows) == 3
    assert [f["fix"] for f in meta["date_fixes"]] == ["dropped"]
    assert all(r["transaction_date_fix"] is None and r["transaction_date_as_filed"] is None for r in rows)
    # the period of report unusable as well: a date with no candidate in [filed - 366 d, filed + 1] is dropped
    y = (FX / "form4_deriv.xml").read_text(encoding="utf-8")
    y = y.replace("<periodOfReport>2026-08-09</periodOfReport>", "<periodOfReport>2026-13-45</periodOfReport>")
    y = y.replace("<value>2026-08-09</value>", "<value>2028-02-29</value>", 1)
    rows, meta = parse_ownership_xml(y.encode(), m["accession"], date.fromisoformat(m["filed"]))
    assert meta["period"] is None and meta["n_skipped"] == 1 and len(rows) == 3
    assert [f["fix"] for f in meta["date_fixes"]] == ["dropped"]


def test_tsv_typo_dates_are_corrected(tmp_path):
    import zipfile

    src = zipfile.ZipFile(FX / "insider_tsv_small.zip")
    out = tmp_path / "typo.zip"
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            b = src.read(n)
            if n == "NONDERIV_TRANS.tsv":
                lines = b.decode("utf-8").split("\n")
                for i, ln in enumerate(lines):
                    if "\t9042799\t" in ln:
                        lines[i] = ln.replace("\t09-AUG-2026\t", "\t09-AUG-2047\t")
                    elif "\t9042798\t" in ln:
                        lines[i] = ln.replace("\t09-AUG-2026\t", "\t09-AUG-0026\t")
                b = "\n".join(lines).encode("utf-8")
            z.writestr(n, b)
    fixes: list[dict] = []
    df = parse_insider_tsv_zip(str(out), date_fixes=fixes)
    base = parse_insider_tsv_zip(str(FX / "insider_tsv_small.zip"))
    assert df.height == base.height and sorted(f["fix"] for f in fixes) == ["century", "year"]
    acc = df.filter(pl.col("filing_id") == "sec_edgar:0000002488-26-000125")
    assert (acc["transaction_date"] == date(2026, 8, 9)).all()
    j = df.join(base, on="txn_id", suffix="_b")
    assert (j["transaction_date"] == j["transaction_date_b"]).all()       # identical to the clean file
    # ... except for the in-row label (A5 review): the fix and the date as filed on the two typo rows only
    lab = df.filter(pl.col("transaction_date_fix").is_not_null()).sort("transaction_date_fix")
    assert lab.select("transaction_date_fix", "transaction_date_as_filed").rows() == [
        ("century", date(26, 8, 9)), ("year", date(2047, 8, 9))]
    assert base["transaction_date_fix"].is_null().all() and base["transaction_date_as_filed"].is_null().all()
    assert df.filter(pl.col("transaction_date_fix").is_null())["transaction_date_as_filed"].is_null().all()
    conform(df, "insider_transactions")


def test_finalize_reads_partitions_written_before_the_label_columns(tmp_data):
    """Lake partitions written before A5 have no transaction_date_as_filed / _fix: the upsert path (_finalize ->
    mark_superseded) and the readers fill them with nulls; a re-ingested row then carries its label."""
    from app.core.schema import get_contract
    from app.core.storage import partition_dir, scan_table, table_dir, write_table

    rows, _, m = _parse("form4_deriv")
    old = conform(txn_frame(rows), "insider_transactions").drop("transaction_date_as_filed", "transaction_date_fix")
    d = partition_dir(table_dir(get_contract("insider_transactions"), tmp_data), ["region", "year"], ["US", 2026])
    d.mkdir(parents=True, exist_ok=True)
    old.drop("region", "year").write_parquet(d / "part-old.parquet")          # the pre-A5 physical layout
    assert "transaction_date_fix" not in scan_table("insider_transactions", tmp_data).collect_schema().names()
    x = (FX / "form4_deriv.xml").read_text(encoding="utf-8").replace("<value>2026-08-09</value>",
                                                                       "<value>2036-08-09</value>", 1)
    new, _meta = parse_ownership_xml(x.encode(), m["accession"], date.fromisoformat(m["filed"]))
    ad = SecForm4Adapter(settings=tmp_data, params={})
    out = ad._finalize(txn_frame(new))
    assert out.height == len(new) and {"transaction_date_as_filed", "transaction_date_fix"} <= set(out.columns)
    write_table(out, "insider_transactions", settings=tmp_data)
    back = scan_table("insider_transactions", tmp_data).collect().sort("txn_id")
    assert back.height == old.height
    fixed = back.filter(pl.col("transaction_date_fix").is_not_null())
    assert fixed.select("txn_id", "transaction_date", "transaction_date_as_filed", "transaction_date_fix").rows() == [
        (rows[0]["txn_id"], date(2026, 8, 9), date(2036, 8, 9), "year")]


def test_adapter_labels_date_fixes():
    ad = SecForm4Adapter.__new__(SecForm4Adapter)
    import logging

    ad.log = logging.getLogger("test")
    res = FetchResult(source_id="sec_edgar", started_at=datetime.now(UTC))
    fixes = [{"txn_id": "a", "filed": date(2047, 6, 7), "stored": date(2017, 6, 7), "fix": "year"},
             {"txn_id": "c", "filed": date(2023, 11, 3), "stored": date(2023, 1, 3), "fix": "period_of_report"},
             {"txn_id": "b", "filed": date(2026, 12, 25), "stored": None, "fix": "dropped"}]
    ad._note_date_fixes(res, fixes)
    assert res.meta["n_txn_date_fixed"] == 2 and res.meta["n_txn_date_approx"] == 1
    assert res.meta["n_txn_date_dropped"] == 1
