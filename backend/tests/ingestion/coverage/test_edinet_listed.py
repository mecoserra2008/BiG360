"""COVERAGE_V3 B1: EDINET statements for every listed JP issuer (app.ingestion.edinet_listed).

Fixtures only (no network): the recorded Toyota type=5 zip of tests/ingestion/global/fixtures plus a synthetic
non-consolidated filer's CSV; day lists are written into the tmp raw dir; HTTP is patched."""

from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.ingestion import edinet, edinet_listed as EL
from app.ingestion._global_common import load_state, save_state

TOYOTA_ZIP = Path(__file__).parents[1] / "global" / "fixtures" / "edinet_S100YE2C_type5.zip"
HEADER = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]


def _doc(doc_id, code="120", edinet_code="E01777", sec="72030", submit="2026-06-18 15:00", **kw):
    d = {"seqNumber": 1, "docID": doc_id, "edinetCode": edinet_code, "secCode": sec, "filerName": "x",
         "docTypeCode": code, "periodStart": "2025-04-01", "periodEnd": "2026-03-31", "submitDateTime": submit,
         "parentDocID": None, "withdrawalStatus": "0", "xbrlFlag": "1", "csvFlag": "1", "docDescription": "yuho"}
    d.update(kw)
    return d


def _csv(consolidated_prepared: str = "false", entity: str = "E99999") -> bytes:
    rows = [HEADER,
            ["jpdei_cor:CurrentFiscalYearStartDateDEI", "", "FilingDateInstant", "提出日時点", "その他", "時点", "", "", "2025-04-01"],
            ["jpdei_cor:CurrentFiscalYearEndDateDEI", "", "FilingDateInstant", "提出日時点", "その他", "時点", "", "", "2026-03-31"],
            ["jpdei_cor:CurrentPeriodEndDateDEI", "", "FilingDateInstant", "提出日時点", "その他", "時点", "", "", "2026-03-31"],
            ["jpdei_cor:TypeOfCurrentPeriodDEI", "", "FilingDateInstant", "提出日時点", "その他", "時点", "", "", "FY"],
            ["jpdei_cor:AccountingStandardsDEI", "", "FilingDateInstant", "提出日時点", "その他", "時点", "", "", "Japan GAAP"],
            ["jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "", "FilingDateInstant", "提出日時点",
             "その他", "時点", "", "", consolidated_prepared],
            ["jppfs_cor:NetSales", "売上高", "CurrentYearDuration", "当期", "個別", "期間", "JPY", "円", "1000000000"],
            ["jppfs_cor:NetSales", "売上高", "Prior1YearDuration", "前期", "個別", "期間", "JPY", "円", "900000000"],
            ["jppfs_cor:OperatingIncome", "営業利益", "CurrentYearDuration", "当期", "個別", "期間", "JPY", "円", "50000000"],
            ["jppfs_cor:Assets", "資産", "CurrentYearInstant", "当期末", "個別", "時点", "JPY", "円", "800000000"],
            ["jpcrp_cor:NetSalesSummaryOfBusinessResults", "売上高、経営指標等", "CurrentYearDuration", "当期", "その他",
             "期間", "JPY", "円", "1000000000"],
            ["jpcrp_cor:NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc", "発行済株式",
             "FilingDateInstant", "提出日時点", "その他", "時点", "shares", "株", "1000000"],
            ["jpcrp_cor:NetSalesSegmentInformation", "売上高", f"CurrentYearDuration_jpcrp030000-asr_{entity}-000FoodReportableSegmentMember",
             "当期", "その他", "期間", "JPY", "円", "600000000"],
            ["jppfs_cor:ShareholdersEquity", "株主資本", "CurrentYearInstant_RetainedEarningsMember", "当期末", "個別", "時点",
             "JPY", "円", "100"]]
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t", lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-16")


def _zip(csv_bytes: bytes, entity: str = "E99999") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"XBRL_TO_CSV/jpcrp030000-asr-001_{entity}-000_2026-03-31_01_2026-06-18.csv", csv_bytes)
        z.writestr(f"XBRL_TO_CSV/jpaud-aar-cn-001_{entity}-000_2026-03-31_01_2026-06-18.csv", csv_bytes)
    return out.getvalue()


def _land_list(settings, day: date, docs, landed: str = "2026-06-20") -> None:
    d = settings.raw_dir / "edinet" / "lists" / f"dt={landed}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"documents_{day.isoformat()}.json.gz").write_bytes(gzip.compress(json.dumps(docs).encode()))


def _identifier_map(settings, rows) -> None:
    from app.core.schema import conform
    from app.core.storage import write_table

    now = datetime.now(UTC)
    df = pl.DataFrame([{"id_type": t, "id_value": v, "entity_level": "issuer", "issuer_id": i, "security_id": None,
                        "listing_id": None, "valid_from": date(1900, 1, 1), "valid_to": None, "source_id": "test",
                        "confidence": c, "updated_at": now} for t, v, i, c in rows])
    write_table(conform(df, "identifier_map"), "identifier_map", settings=settings)


ZIPS = {"S100YE2C": TOYOTA_ZIP.read_bytes(), "S100SMAL": _zip(_csv("false"))}


@pytest.fixture()
def lake(tmp_data, monkeypatch):
    from pydantic import SecretStr

    tmp_data.edinet_apikey = SecretStr("x")  # never sent anywhere: HTTP is patched
    _identifier_map(tmp_data, [("edinet_code", "E01777", "I_TOYOTA", 1.0), ("jp_sec_code", "7203", "I_TOYOTA", 1.0),
                               ("jp_sec_code", "9999", "I_SMALL", 1.0),     # no EDINET code mapping: via secCode
                               ("jp_sec_code", "5555", "I_FUZZY", 0.5)])    # below min confidence: ignored
    _land_list(tmp_data, date(2026, 6, 18), [
        _doc("S100YE2C"),
        _doc("S100SMAL", edinet_code="E99999", sec="99990", submit="2026-06-18 16:30"),
        _doc("S100UNMP", edinet_code="E88888", sec="11110"),                     # listed code, not in reference
        _doc("S100UNLS", edinet_code="E77777", sec=None),                        # unlisted filer
        _doc("S100WDRN", withdrawalStatus="2"),
        _doc("S100NCSV", csvFlag="0"),
        _doc("S100HOLD", code="350"),
        _doc("S100FUZZ", edinet_code="E55555", sec="55550"),
    ])
    calls: list[str] = []

    class R:
        def __init__(self, content):
            self.content = content

    def get(self, url, params=None, **kw):
        doc = str(url).rsplit("/", 1)[-1]
        calls.append(doc)
        assert params and params.get("type") == 5
        return R(ZIPS.get(doc, b'{"metadata": {"status": "404"}}'))

    import app.core.http as H

    monkeypatch.setattr(H.HttpClient, "get", get)
    return SimpleNamespace(s=tmp_data, calls=calls)


P = {"since": "2026-06-01", "until": "2026-06-30"}


# ------------------------------------------------------------------------------------------------ pure helpers


def test_sec4_and_doc_types():
    assert EL.sec4("72030") == "7203" and EL.sec4("285A0") == "285A" and EL.sec4("7203") == "7203"
    assert EL.sec4(" ") is None and EL.sec4(None) is None
    assert EL.doc_types_for(["annual"]) == {"120", "130"} and EL.doc_types_for(None) == edinet.FUND_DOCS
    with pytest.raises(ValueError):
        EL.doc_types_for(["holdings"])


def test_universe_mapping_edinet_sec_codelist_and_ambiguity():
    im = pl.DataFrame({"id_type": ["edinet_code", "jp_sec_code", "jp_sec_code", "jp_sec_code", "jp_sec_code", "edinet_code", "edinet_code"],
                       "id_value": ["E01777", "7203", "285A", "1234", "1234", "E00001", "E00002"],
                       "issuer_id": ["I1", "I1", "I2", "I3", "I4", "I5", "I6"],
                       "valid_to": [None] * 6 + [date(2020, 1, 1)], "confidence": [1.0] * 7})
    uni = EL.build_universe(im, codelist={"E33333": "285A"}, seed_tickers=["7203"])
    assert "1234" not in uni.by_sec  # ambiguous (two issuers) -> dropped, never guessed
    assert "E00002" not in uni.by_edinet  # closed mapping
    assert uni.seed_issuers == {"I1"}
    docs = pl.DataFrame({"docID": ["a", "b", "c", "d", "e"], "edinetCode": ["E01777", "E99999", "E33333", None, "E00001"],
                         "secCode": ["72030", "285A0", None, "11110", "72030"]})
    m = EL.map_docs(docs, uni)
    got = {r["docID"]: (r["issuer_id"], r["map_via"]) for r in m.iter_rows(named=True)}
    assert got["a"] == ("I1", "edinet_code")
    assert got["b"] == ("I2", "jp_sec_code")          # 5-digit alphanumeric code
    assert got["c"] == ("I2", "jp_sec_code")          # no secCode on the list row -> code list
    assert got["d"] == (None, None)
    assert got["e"] == ("I5", "edinet_code")          # EDINET code wins over a disagreeing secCode
    assert m.filter(pl.col("docID") == "e")["conflict"].item() is True
    r = uni.restrict(["285A.T", "E01777"])
    assert r.issuers == {"I1", "I2"}


def test_scan_lists_latest_landing_wins(tmp_data):
    _land_list(tmp_data, date(2026, 6, 18), [_doc("A"), _doc("B")], landed="2026-06-19")
    _land_list(tmp_data, date(2026, 6, 18), [_doc("A"), _doc("B", withdrawalStatus="2")], landed="2026-06-25")
    _land_list(tmp_data, date(2026, 5, 1), [_doc("C", code="160")], landed="2026-06-19")
    files = EL.list_files(tmp_data.raw_dir, date(2026, 6, 1), None)
    assert list(files) == [date(2026, 6, 18)] and "dt=2026-06-25" in str(files[date(2026, 6, 18)])
    st = Counter()
    df = EL.scan_lists(files, edinet.FUND_DOCS, st)
    assert df["docID"].to_list() == ["A"] and st["withdrawn"] == 1


def test_parse_non_consolidated_filer_is_entity_level():
    kw = dict(source_entity_id="E99999", filing_id="edinet:X", form_type="120", knowledge_date=date(2026, 6, 18))
    rows, dei = edinet.parse_edinet_csv(_csv("false"), **kw)
    assert dei["WhetherConsolidatedFinancialStatementsArePreparedDEI"] == "false"
    assert rows and all(r["consolidated"] is False for r in rows)
    rows2, _ = edinet.parse_edinet_csv(_csv("true"), **kw)  # consolidated filer: unchanged behaviour
    cons = {r["concept"]: r["consolidated"] for r in rows2 if r["dimensions_hash"] == ""}
    assert cons["jppfs_cor:NetSales"] is False and cons["jpcrp_cor:NetSalesSummaryOfBusinessResults"] is True


def test_non_consolidated_filer_reaches_canonical():
    """Regression (B1 validation 2301.T GAKUJO): parent-only statements of a filer without consolidated statements
    were dropped because its cover rows were flagged consolidated."""
    from app.ingestion._global_common import frame
    from app.normalize import fundamentals as F

    rows, _ = edinet.parse_edinet_csv(_csv("false"), source_entity_id="E99999", filing_id="edinet:X", form_type="120",
                                      knowledge_date=date(2026, 6, 18))
    raw = frame(rows, "fundamentals_raw").with_columns(pl.lit("I_SMALL").alias("issuer_id"))
    issuers = pl.DataFrame({"issuer_id": ["I_SMALL"], "region": ["JP"], "reporting_currency": ["JPY"]})
    cand = F.map_raw_to_canonical(raw, issuers)
    rev = cand.filter((pl.col("item_id") == "revenue") & (pl.col("period_end") == date(2026, 3, 31)))
    assert rev.height == 1 and rev["value"].item() == 1e9 and rev["concept"].item() == "jppfs_cor:NetSales"
    assert cand.filter(pl.col("item_id") == "total_assets")["value"].to_list() == [8e8]


def test_filter_dims():
    df = pl.DataFrame({"dimensions_hash": ["", "h1", "h2", None],
                       "dimensions_json": [None, '{"members": "X-000FoodReportableSegmentMember"}',
                                           '{"members": "RetainedEarningsMember"}', None]})
    assert EL.filter_dims(df, "segments").height == 3
    assert EL.filter_dims(df, "none").height == 2
    assert EL.filter_dims(df, "all").height == 4
    with pytest.raises(ValueError):
        EL.filter_dims(df, "some")


def test_new_jp_chains_are_lowest_precedence():
    from app.normalize.fundamentals import load_canonical

    load_canonical.cache_clear()
    s = load_canonical()["singles"].filter(pl.col("family") == "jp")
    rev = {r["key"]: r["prio"] for r in s.filter(pl.col("item_id") == "revenue").iter_rows(named=True)}
    assert rev["jppfs_cor:NetSales"] == 0
    for k in ("jppfs_cor:OperatingIncomeINS", "jppfs_cor:OperatingRevenueCMD", "jpcrp_cor:RevenuesUSGAAPSummaryOfBusinessResults"):
        assert rev[k] > rev["jpcrp_cor:RevenueIFRSSummaryOfBusinessResults"]
    cfo = {r["key"]: r["prio"] for r in s.filter(pl.col("item_id") == "cfo").iter_rows(named=True)}
    assert cfo["jpcrp_cor:NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults"] > cfo["jppfs_cor:NetCashProvidedByUsedInOperatingActivities"]
    # the J-GAAP summary cash (cash equivalents) is NOT a fallback of cash_and_equivalents (BS cash and deposits)
    assert "jpcrp_cor:CashAndCashEquivalentsSummaryOfBusinessResults" not in set(
        s.filter(pl.col("item_id") == "cash_and_equivalents")["key"].to_list())


# ------------------------------------------------------------------------------------------------ the job


def _edinet_raw(settings) -> pl.DataFrame:
    from app.core.storage import scan_table

    return scan_table("fundamentals_raw", settings).filter(pl.col("source_id") == "edinet").collect()


def test_dry_run_plans_without_writing(lake):
    out = EL.ListedRun(lake.s, params={**P, "dry_run": True}).run()
    s = out["summary"]
    assert out["status"] == "success" and lake.calls == []
    assert s["candidates"] == 5 and s["mapped"] == 2 and s["todo"] == 2 and s["todo_downloads"] == 2
    assert s["unmapped"] == {"mapping_gap": 2, "out_of_scope": 1}  # fuzzy mapping (conf 0.5) is not used
    assert s["issuers_todo"] == 2 and s["todo_by_class"] == {"annual": 2}
    assert not (lake.s.data_dir / "normalized" / "fundamentals_raw").exists()


def test_run_loads_commits_and_is_idempotent(lake):
    res = EL.run(SimpleNamespace(settings=lake.s, params={**P, "commit_docs": 1, "chunk_docs": 1}, watermark=None))
    assert res.status == "success", res.message
    assert sorted(lake.calls) == ["S100SMAL", "S100YE2C"]  # newest first, unmapped / withdrawn / no-csv never fetched
    assert lake.calls[0] == "S100SMAL"
    raw = _edinet_raw(lake.s)
    assert raw.height == res.rows_written["fundamentals_raw"] > 10
    assert set(raw["issuer_id"].unique()) == {"I_TOYOTA", "I_SMALL"}  # filled at ingest (edinet code / secCode)
    assert (raw["knowledge_date"] == date(2026, 6, 18)).all() and not raw["knowledge_date_estimated"].any()
    assert (raw["knowledge_date"] >= raw["period_end"]).all()
    small = raw.filter(pl.col("issuer_id") == "I_SMALL")
    assert not small["consolidated"].any()
    assert small.filter(pl.col("concept") == "jppfs_cor:ShareholdersEquity").height == 0  # dims=segments
    assert small.filter(pl.col("dimensions_hash") != "").height == 1                    # the segment fact
    files = sorted(p.name for p in (lake.s.data_dir / "normalized" / "fundamentals_raw").rglob("*.parquet"))
    assert files and all(f.startswith("part-edl-") for f in files)
    from app.core.storage import scan_table

    fi = scan_table("filings_index", lake.s).filter(pl.col("source_id") == "edinet").collect()
    assert fi.height == 2 and fi["doc_path"].null_count() == 0 and set(fi["issuer_id"]) == {"I_TOYOTA", "I_SMALL"}
    ts = fi.filter(pl.col("filing_id") == "edinet:S100SMAL")["knowledge_ts"].item()
    assert ts.replace(tzinfo=None) == datetime(2026, 6, 18, 7, 30)  # 16:30 JST
    st = load_state("edinet_listed", lake.s)
    assert st["seq"] == 3 and st["committed_docs"] == 2 and "pending" not in st
    # second run: nothing to do, no request
    lake.calls.clear()
    res2 = EL.run(SimpleNamespace(settings=lake.s, params=dict(P), watermark=None))
    assert res2.status == "success" and "nothing to load" in res2.message and lake.calls == []
    assert _edinet_raw(lake.s).height == raw.height


def test_dead_commit_is_redone_without_duplicates(lake):
    EL.ListedRun(lake.s, params=dict(P)).run()
    n = _edinet_raw(lake.s).height
    # simulate a crash inside commit seq 1: files written, filings_index written, state not advanced
    st = load_state("edinet_listed", lake.s)
    st.update({"seq": 1, "pending": {"seq": 1, "docs": ["S100YE2C", "S100SMAL"]}})
    save_state("edinet_listed", st, lake.s)
    lake.calls.clear()
    out = EL.ListedRun(lake.s, params=dict(P)).run()
    assert out["docs_ok"] == 2 and lake.calls == []  # zips on disk are reused
    assert _edinet_raw(lake.s).height == n
    assert "pending" not in load_state("edinet_listed", lake.s)


def test_download_failure_is_recorded_and_retried(lake):
    ZIPS_BAK = dict(ZIPS)
    try:
        ZIPS.pop("S100SMAL")
        out = EL.ListedRun(lake.s, params=dict(P)).run()
        assert out["docs_ok"] == 1 and out["docs_failed"] == 1
        f = load_state("edinet_listed", lake.s)["failed"]["S100SMAL"]
        assert f["n"] == 1 and f["reason"] == "source_unavailable" and "x" not in f["err"].split()
        ZIPS["S100SMAL"] = ZIPS_BAK["S100SMAL"]
        out2 = EL.ListedRun(lake.s, params=dict(P)).run()
        assert out2["docs_ok"] == 1 and "S100SMAL" not in load_state("edinet_listed", lake.s)["failed"]
        out3 = EL.ListedRun(lake.s, params={**P, "max_attempts": 1, "dry_run": True}).run()
        assert out3["summary"]["todo"] == 0
    finally:
        ZIPS.clear()
        ZIPS.update(ZIPS_BAK)


def test_issuers_param_and_time_budget(lake):
    out = EL.ListedRun(lake.s, params={**P, "issuers": "7203.T"}).run()
    assert out["docs_ok"] == 1 and lake.calls == ["S100YE2C"]
    out2 = EL.ListedRun(lake.s, params={**P, "max_minutes": 1e-9}).run()  # budget exhausted before the first batch
    assert out2["docs_ok"] == 0 and "time budget" in out2["message"]


def test_compaction_merges_chunk_files(lake):
    EL.ListedRun(lake.s, params={**P, "commit_docs": 1, "chunk_docs": 1, "compact_files": 0}).run()
    n = _edinet_raw(lake.s).height
    r = EL.ListedRun(lake.s, params=dict(P))
    multi = [d for d in r._raw_root().glob("fiscal_year=*") if len(list(d.glob("part-edl-*.parquet"))) >= 2]
    assert multi
    assert r.compact(2) >= 2
    for d in multi:
        files = list(d.glob("*.parquet"))
        assert len(files) == 1 and files[0].name.startswith("part-edl-m")
    assert _edinet_raw(lake.s).height == n


def test_missing_key_is_skipped(lake):
    lake.s.edinet_apikey = None
    res = EL.run(SimpleNamespace(settings=lake.s, params=dict(P), watermark=None))
    assert res.status == "skipped" and "missing key" in res.message


def test_jobs_registered():
    import yaml
    from app.core.config import get_settings

    jobs = {j["id"]: j for j in yaml.safe_load((get_settings().contracts_dir / "jobs.yaml").read_text(encoding="utf-8"))["jobs"]}
    n, b = jobs["edinet_listed"], jobs["edinet_listed_backfill"]
    assert n["module"] == b["module"] == "app.ingestion.edinet_listed"
    assert n["dag"] == "nightly" and n["params"]["mode"] == "recent" and "edinet_filings" in n["depends_on"]
    assert b["dag"] == "manual" and b["params"]["mode"] == "backfill"
    assert n["timeout_min"] > n["params"]["max_minutes"] and b["timeout_min"] > b["params"]["max_minutes"]
    # the statements land before identity / normalisation and the earnings events (as edinet_filings did)
    for down in ("identity_resolve", "earnings_events"):
        assert "edinet_listed" in jobs[down]["depends_on"] and "edinet_listed" in jobs[down]["soft_depends_on"]


def test_dead_commit_outside_the_window_is_kept_for_redo(lake):
    EL.ListedRun(lake.s, params=dict(P)).run()
    n = _edinet_raw(lake.s).height
    st = load_state("edinet_listed", lake.s)
    st.update({"seq": 1, "pending": {"seq": 1, "docs": ["S100YE2C", "S100SMAL"]}})
    save_state("edinet_listed", st, lake.s)
    # a nightly-style run whose window does not contain the dead commit's docs: files purged, docs kept for redo
    out = EL.ListedRun(lake.s, params={"since": "2026-07-01", "until": "2026-07-31"}).run()
    assert out["docs_ok"] == 0 and _edinet_raw(lake.s).height == 0
    st = load_state("edinet_listed", lake.s)
    assert sorted(st["redo"]) == ["S100SMAL", "S100YE2C"] and "pending" not in st
    assert EL.ledger_docs(lake.s, st["seq"]) == set()  # the dead seq's ledger lines are gone
    out2 = EL.ListedRun(lake.s, params=dict(P)).run()
    assert out2["docs_ok"] == 2 and _edinet_raw(lake.s).height == n
    assert not load_state("edinet_listed", lake.s).get("redo")


def test_ledger_prevents_duplicates_when_filings_index_is_clobbered(lake):
    from app.core.schema import conform
    from app.core.storage import scan_table, write_table

    EL.ListedRun(lake.s, params=dict(P)).run()
    n = _edinet_raw(lake.s).height
    fi = scan_table("filings_index", lake.s).filter(pl.col("source_id") == "edinet").collect()
    write_table(conform(fi.with_columns(pl.lit(None, pl.Utf8).alias("doc_path")), "filings_index"), "filings_index",
                settings=lake.s)  # another writer drops doc_path
    assert EL.loaded_doc_ids(lake.s) == set()
    out = EL.ListedRun(lake.s, params=dict(P)).run()
    assert out["docs_ok"] == 0 and "nothing to load" in out["message"] and _edinet_raw(lake.s).height == n


def test_edinet_filings_incremental_leaves_statements_to_edinet_listed(tmp_data, monkeypatch):
    """B1: the seed nightly no longer downloads / upserts statement CSVs (edinet_listed appends them); the legacy
    manual backfill (mode=backfill) still does."""
    from pydantic import SecretStr

    tmp_data.edinet_apikey = SecretStr("x")
    hold = _doc("S100HOLD", code="350", edinet_code="E11111", sec=None, subjectEdinetCode="E01777")
    monkeypatch.setattr(edinet.EdinetAdapter, "day_list", lambda self, d: [_doc("S100YE2C"), hold] if d == date(2026, 6, 18) else [])
    monkeypatch.setattr(edinet.EdinetAdapter, "_universe", lambda self, e: ({"E01777"}, {"72030"}))
    calls: list[str] = []

    class R:
        content = TOYOTA_ZIP.read_bytes()

    import app.core.http as H

    monkeypatch.setattr(H.HttpClient, "get", lambda self, url, *a, **k: calls.append(url) or R())
    inc = edinet.EdinetAdapter(settings=tmp_data, until="2026-06-18")
    assert inc.statements is False
    res = inc.fetch(since="2026-06-18")
    assert [x["docID"] for x in res.meta["docs"]] == ["S100HOLD"] and calls == []
    tables = inc.normalize(res)
    assert tables["fundamentals_raw"].is_empty() and tables["filings_index"]["form_type"].to_list() == ["350"]
    bf = edinet.EdinetAdapter(settings=tmp_data, until="2026-06-18", mode="backfill")
    assert bf.statements is True
    res2 = bf.fetch(since="2026-06-18")
    assert {x["docID"] for x in res2.meta["docs"]} == {"S100YE2C", "S100HOLD"} and len(calls) == 1
    assert edinet.EdinetAdapter(settings=tmp_data, statements="true").statements is True


# ------------------------------------------------------------------------------------------------ review fixes (B1)


def _dotfiles(settings) -> list[str]:
    root = settings.data_dir / "normalized" / "fundamentals_raw"
    return [str(p.relative_to(root)) for p in root.rglob(".*")] if root.exists() else []


@pytest.mark.parametrize("lose", ["state", "state+ledger"])
def test_lost_state_never_reuses_a_committed_seq(lake, lose):
    """Review: a lost / reset state file restarted seq at 1 and the next commit purged the committed part-edl-000001
    files (facts lost for good: their docs stay marked loaded)."""
    from app.ingestion._global_common import state_path

    EL.ListedRun(lake.s, params={**P, "issuers": "7203.T"}).run()  # seq 1
    toyota = _edinet_raw(lake.s).height
    state_path("edinet_listed", lake.s).unlink()
    if lose == "state+ledger":
        EL.ledger_path(lake.s).unlink()
    out = EL.ListedRun(lake.s, params=dict(P)).run()
    raw = _edinet_raw(lake.s)
    assert out["docs_ok"] == 1 and raw.filter(pl.col("issuer_id") == "I_TOYOTA").height == toyota
    assert raw.filter(pl.col("issuer_id") == "I_SMALL").height > 0
    assert load_state("edinet_listed", lake.s)["last_commit"]["seq"] == 2  # from the ledger, else from the files
    assert not _dotfiles(lake.s)


def test_unreadable_state_fails_the_run_without_touching_data(lake):
    from app.ingestion._global_common import state_path

    EL.ListedRun(lake.s, params={**P, "issuers": "7203.T"}).run()
    n = _edinet_raw(lake.s).height
    state_path("edinet_listed", lake.s).write_text("{truncated", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        EL.ListedRun(lake.s, params=dict(P)).run()
    assert _edinet_raw(lake.s).height == n and lake.calls == ["S100YE2C"]


def test_dead_commit_with_lost_state_is_found_through_its_files(lake):
    """Commit 2 died after its files and its filings_index row, before its ledger lines, and the state file was lost:
    the files above the ledger's highest seq are purged and their documents redone (no duplicate, no loss)."""
    from app.ingestion._global_common import state_path

    EL.ListedRun(lake.s, params={**P, "commit_docs": 1, "chunk_docs": 1}).run()  # seq 1 S100SMAL, seq 2 S100YE2C
    n = _edinet_raw(lake.s).height
    lp = EL.ledger_path(lake.s)
    lp.write_text("".join(f"{ln}\n" for ln in lp.read_text(encoding="utf-8").splitlines() if not ln.startswith("2\t")),
                  encoding="utf-8")
    state_path("edinet_listed", lake.s).unlink()
    assert "S100YE2C" in EL.loaded_doc_ids(lake.s)  # filings_index says loaded
    lake.calls.clear()
    out = EL.ListedRun(lake.s, params=dict(P)).run()
    assert out["docs_ok"] == 1 and lake.calls == []  # redone from the zip on disk
    assert _edinet_raw(lake.s).height == n
    loaded, redo, top = EL.read_ledger(lake.s)
    assert set(loaded) == {"S100SMAL", "S100YE2C"} and redo == set() and top == 2


def test_redo_survives_a_state_loss(lake):
    """recover() writes the dead commit's docs as ledger 'redo' lines: durable even if the state file goes too."""
    from app.ingestion._global_common import state_path

    EL.ListedRun(lake.s, params=dict(P)).run()
    n = _edinet_raw(lake.s).height
    st = load_state("edinet_listed", lake.s)
    st.update({"seq": 1, "pending": {"seq": 1, "docs": ["S100YE2C", "S100SMAL"]}})
    save_state("edinet_listed", st, lake.s)
    EL.ListedRun(lake.s, params={"since": "2026-07-01", "until": "2026-07-31"}).run()  # recover, nothing in window
    assert _edinet_raw(lake.s).height == 0
    state_path("edinet_listed", lake.s).unlink()
    assert EL.read_ledger(lake.s)[1] == {"S100YE2C", "S100SMAL"}
    out = EL.ListedRun(lake.s, params=dict(P)).run()
    assert out["docs_ok"] == 2 and _edinet_raw(lake.s).height == n and EL.read_ledger(lake.s)[1] == set()


def _two_file_partitions(lake):
    EL.ListedRun(lake.s, params={**P, "commit_docs": 1, "chunk_docs": 1, "compact_files": 0}).run()
    r = EL.ListedRun(lake.s, params=dict(P))
    multi = sorted(d for d in r._raw_root().glob("fiscal_year=*") if len(list(d.glob("part-edl-*.parquet"))) >= 2)
    assert multi
    return r, multi, _edinet_raw(lake.s).height


def test_compaction_killed_after_publishing_is_rolled_back(lake, monkeypatch):
    r, multi, n = _two_file_partitions(lake)

    def killed(self, m):
        raise RuntimeError("worker killed")

    orig = EL.ListedRun._finish_compaction
    monkeypatch.setattr(EL.ListedRun, "_finish_compaction", killed)
    with pytest.raises(RuntimeError, match="killed"):
        r.compact(2)
    monkeypatch.setattr(EL.ListedRun, "_finish_compaction", orig)
    assert "compacting" in load_state("edinet_listed", lake.s) and _edinet_raw(lake.s).height > n  # duplicates visible
    EL.ListedRun(lake.s, params=dict(P)).recover()
    assert _edinet_raw(lake.s).height == n and "compacting" not in load_state("edinet_listed", lake.s)
    assert not list(multi[0].glob("part-edl-m*")) and len(list(multi[0].glob("part-edl-*.parquet"))) == 2
    assert not _dotfiles(lake.s)


def test_compaction_killed_while_moving_inputs_is_finished(lake, monkeypatch):
    r, multi, n = _two_file_partitions(lake)

    def one_move_then_killed(self, m):
        trash = Path(m["trash"])
        trash.mkdir(parents=True, exist_ok=True)
        (Path(m["pdir"]) / m["inputs"][0]).replace(trash / m["inputs"][0])
        raise RuntimeError("worker killed")

    orig = EL.ListedRun._finish_compaction
    monkeypatch.setattr(EL.ListedRun, "_finish_compaction", one_move_then_killed)
    with pytest.raises(RuntimeError):
        r.compact(2)
    monkeypatch.setattr(EL.ListedRun, "_finish_compaction", orig)
    EL.ListedRun(lake.s, params=dict(P)).recover()
    assert _edinet_raw(lake.s).height == n
    files = [f.name for f in multi[0].glob("*.parquet")]
    assert len(files) == 1 and files[0].startswith("part-edl-m") and not _dotfiles(lake.s)


def test_compaction_rollback_leaves_no_copy_in_the_table(lake, monkeypatch):
    """Review: the rollback renamed the merged copy to '.old-...' inside the partition, which DuckDB globs and
    pathlib '*.parquet' globs read (rows double-counted)."""
    r, multi, n = _two_file_partitions(lake)
    real = EL._move
    seen = {"n": 0}

    def in_use(src, dst):
        if "trash" in str(dst) and EL.SEQ_RE.match(src.name):
            seen["n"] += 1
            if seen["n"] == 2:  # 2nd input of the first partition is held open by a reader
                raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(EL, "_move", in_use)
    r.compact(2)
    assert _edinet_raw(lake.s).height == n and "compacting" not in load_state("edinet_listed", lake.s)
    assert sorted(f.name for f in multi[0].glob("*.parquet")) == ["part-edl-000001.parquet", "part-edl-000002.parquet"]
    assert not _dotfiles(lake.s)


def test_compaction_is_left_to_the_next_run_when_cancelled(lake):
    import threading

    r, multi, n = _two_file_partitions(lake)
    ev = threading.Event()
    ev.set()
    rc = EL.ListedRun(lake.s, params=dict(P), cancel=ev)
    assert rc.compact(2) == 0 and rc.stats["compaction_deferred"] == 1
    assert len(list(multi[0].glob("part-edl-*.parquet"))) == 2


def test_parse_failure_is_all_or_nothing_and_retry_failed_requeues_it(lake, monkeypatch):
    """Review probe: a document whose CSV failed to parse kept its doc_path / ledger line, so it counted as loaded and
    was never retried. Now it carries no facts and no doc_path; retry_failed=parse re-queues it."""
    real = edinet.parse_edinet_csv

    def boom(*a, **k):
        raise ValueError("EDINET CSV header changed")

    monkeypatch.setattr(edinet, "parse_edinet_csv", boom)
    out = EL.ListedRun(lake.s, params={**P, "issuers": "7203.T"}).run()
    assert out["docs_ok"] == 0 and out["parse_failed"] == 1 and out["status"] == "failed"
    assert _edinet_raw(lake.s).height == 0 and "S100YE2C" not in EL.loaded_doc_ids(lake.s)
    assert load_state("edinet_listed", lake.s)["failed"]["S100YE2C"]["reason"] == "pipeline_bug"
    monkeypatch.setattr(edinet, "parse_edinet_csv", real)  # the parser is fixed
    dry = {**P, "issuers": "7203.T", "dry_run": True}
    assert EL.ListedRun(lake.s, params=dry).run()["summary"]["todo"] == 0
    assert EL.ListedRun(lake.s, params={**dry, "retry_failed": "parse"}).run()["summary"]["todo"] == 1
    out2 = EL.ListedRun(lake.s, params={**P, "issuers": "7203.T", "retry_failed": "parse"}).run()
    assert out2["docs_ok"] == 1 and _edinet_raw(lake.s).height > 0
    assert "S100YE2C" not in load_state("edinet_listed", lake.s)["failed"]
    with pytest.raises(ValueError):
        EL.ListedRun(lake.s, params={"retry_failed": "everything"})


# ------------------------------------------------------------------------------------------------ JP bank revenue


def _bank_csv() -> bytes:
    """Consolidated J-GAAP bank (JPY millions x 1e6; shaped on 島根銀行 FY2026-03)."""
    dei = [["jpdei_cor:CurrentFiscalYearStartDateDEI", "2025-04-01"], ["jpdei_cor:CurrentFiscalYearEndDateDEI", "2026-03-31"],
           ["jpdei_cor:CurrentPeriodEndDateDEI", "2026-03-31"], ["jpdei_cor:TypeOfCurrentPeriodDEI", "FY"],
           ["jpdei_cor:AccountingStandardsDEI", "Japan GAAP"],
           ["jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", "true"]]
    pl_ = {"OrdinaryIncomeBNK": 12243, "InterestIncomeOIBNK": 7586, "FeesAndCommissionsOIBNK": 1054,
           "OtherOrdinaryIncomeOIBNK": 930, "OtherIncomeOIBNK": 2673, "OrdinaryExpensesBNK": 11827,
           "InterestExpensesOEBNK": 1691, "FeesAndCommissionsPaymentsOEBNK": 1002, "OtherOrdinaryExpensesOEBNK": 818,
           "GeneralAndAdministrativeExpensesOEBNK": 5721, "OtherExpensesOEBNK": 2595,
           "ProfitLossAttributableToOwnersOfParent": 370}
    rows = [HEADER] + [[k, "", "FilingDateInstant", "提出日時点", "その他", "時点", "", "", v] for k, v in dei]
    rows += [[f"jppfs_cor:{k}", "", "CurrentYearDuration", "当期", "連結", "期間", "JPY", "円", str(v * 1_000_000)]
             for k, v in pl_.items()]
    rows += [["jpcrp_cor:OrdinaryIncomeSummaryOfBusinessResults", "経常収益", "CurrentYearDuration", "当期", "その他", "期間",
              "JPY", "円", str(12243 * 1_000_000)]]
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t", lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-16")


@pytest.mark.parametrize("subsector", ["banks_regional", None])
def test_jp_bank_revenue_is_gross_business_profit_not_ordinary_income(subsector):
    """Review: 経常収益 (OrdinaryIncomeBNK: gross interest + fees + other income) is not revenue. JP bank revenue =
    net_interest_income + noninterest_income (業務粗利益), the definition of the megabanks and US / EU banks."""
    from app.ingestion._global_common import frame
    from app.normalize import fundamentals as F

    F.load_canonical.cache_clear()
    rows, _ = edinet.parse_edinet_csv(_bank_csv(), source_entity_id="E03679", filing_id="edinet:SBANK", form_type="120",
                                      knowledge_date=date(2026, 6, 26))
    raw = frame(rows, "fundamentals_raw").with_columns(pl.lit("I_BANK").alias("issuer_id"))
    iss = pl.DataFrame({"issuer_id": ["I_BANK"], "region": ["JP"], "reporting_gaap": [None], "reporting_currency": ["JPY"],
                        "subsector_id": [subsector], "fiscal_year_end_mmdd": ["03-31"]},
                       schema={"issuer_id": pl.Utf8, "region": pl.Utf8, "reporting_gaap": pl.Utf8,
                               "reporting_currency": pl.Utf8, "subsector_id": pl.Utf8, "fiscal_year_end_mmdd": pl.Utf8})
    rates = pl.DataFrame(schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64, "source_id": pl.Utf8})
    c = F.normalize(raw=raw, issuers=iss, write=False, rates=rates)["frames"]["canonical"]
    fy = {r["item_id"]: r for r in c.filter((pl.col("period_kind") == "FY") & (pl.col("period_end") == date(2026, 3, 31)))
          .iter_rows(named=True)}
    nii, nonint = (7586 - 1691) * 1e6, (1054 - 1002 + 930 - 818) * 1e6
    assert fy["net_interest_income"]["value"] == pytest.approx(nii)
    assert fy["noninterest_income"]["value"] == pytest.approx(nonint)
    assert fy["revenue"]["value"] == pytest.approx(nii + nonint) and fy["revenue"]["value"] != 12243e6
    assert fy["revenue"]["derivation"] == "derived_formula" and fy["revenue"]["knowledge_date"] == date(2026, 6, 26)
    jp_rev = set(F.load_canonical()["items"]["revenue"]["jp"])
    assert not {"jppfs_cor:OrdinaryIncomeBNK", "jpcrp_cor:OrdinaryIncomeSummaryOfBusinessResults"} & jp_rev


def test_normalize_watermark_overlap_picks_up_a_commit_published_during_the_previous_scan(tmp_data, monkeypatch):
    """Review: fundamentals_normalize took `started` after its raw scan and filtered ingested_at > watermark, so rows
    stamped just before a run started but published after its scan were skipped for good. Now `started` precedes the
    scan and the filter is ingested_at > watermark - WM_OVERLAP."""
    import logging
    from datetime import timedelta

    from app.core.schema import conform
    from app.core.storage import write_table
    from app.ingestion._global_common import frame
    from app.normalize import fundamentals as F

    t0 = datetime(2026, 11, 7, 2, 0, tzinfo=UTC)  # the previous run's start (= its stored watermark)
    rows, _ = edinet.parse_edinet_csv(_csv("false"), source_entity_id="E99999", filing_id="edinet:X", form_type="120",
                                      knowledge_date=date(2026, 6, 18))
    raw = frame(rows, "fundamentals_raw").with_columns(pl.lit("I_SMALL").alias("issuer_id"),
                                                       pl.lit(t0 - timedelta(minutes=5)).alias("ingested_at"))
    write_table(conform(raw, "fundamentals_raw"), "fundamentals_raw", settings=tmp_data)
    seen: list = []
    monkeypatch.setattr(F, "normalize", lambda ids, **kw: seen.append(ids) or {"canonical": 0, "wide": 0, "issuers": len(ids or [])})
    ctx = SimpleNamespace(params={}, watermark=t0.isoformat(), settings=tmp_data, run_id="t", log=logging.getLogger("t"))
    before = datetime.now(UTC)
    res = F.run(ctx)
    assert seen[-1] == ["I_SMALL"] and before <= datetime.fromisoformat(res.watermark) <= datetime.now(UTC)
    ctx.params = {"watermark_overlap_min": 0}  # the old behaviour: the row is skipped
    F.run(ctx)
    assert seen[-1] == []
