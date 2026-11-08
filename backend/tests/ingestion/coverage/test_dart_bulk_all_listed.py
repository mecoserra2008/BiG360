"""dart_bulk B2 (COVERAGE_V3): all-listed scope, delta / resumable normalization, replace-writes, PIT receipt dates
(amendment-aware, statutory-deadline fallback), corp_code <-> stock_code crosswalk and the OpenDART API path.

Fixtures: the recorded 2026_2Q zips + detailSearch page of test_dart_bulk, plus dart_bulk_api_list.json (a synthetic
OpenDART list.json page in the documented field layout: the receipt numbers of the recorded search page, one synthetic
[기재정정] amendment of 삼성SDI dated 2026-09-02 and one withdrawn row). No network anywhere.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from pydantic import SecretStr

from app.core.storage import read_table, write_table
from app.ingestion import dart_bulk
from app.ingestion._global_common import load_state
from tests.ingestion.coverage.test_dart_bulk import FIX, SCOPE, SEARCH_HTML, ZIPS, _adapter

API_JSON = json.loads((FIX / "dart_bulk_api_list.json").read_text(encoding="utf-8"))
ALL4 = ["005930", "006400", "105560", "095570"]
ISSUER = {"005930": "I_SEC", "006400": "I_SDI", "105560": "I_KB", "095570": "I_AJ"}
H1_REV = ((pl.col("concept") == "ifrs-full_Revenue") & pl.col("consolidated") & (pl.col("fiscal_period") == "H1"))


def _identifier_map(tmp_data, codes, extra: list[dict] | None = None) -> None:
    now = datetime.now(UTC)
    rows = [{"id_type": "kr_stock_code", "id_value": c, "entity_level": "security", "issuer_id": ISSUER[c],
             "security_id": "S" + ISSUER[c], "listing_id": None, "valid_from": date(1900, 1, 1), "valid_to": None,
             "source_id": "ref_universe", "confidence": 1.0, "updated_at": now} for c in codes]
    for x in extra or []:
        rows.append({"entity_level": "issuer", "security_id": None, "listing_id": None, "valid_from": date(1900, 1, 1),
                     "valid_to": None, "source_id": "opendart", "confidence": 1.0, "updated_at": now, **x})
    write_table(pl.DataFrame(rows), "identifier_map", mode="overwrite", settings=tmp_data)


def _amended_search_html() -> str:
    """SEARCH_HTML + a [기재정정] re-filing of 삼성SDI's H1 report on 2026-09-02 (before the 2026-10-08 zip date)."""
    sdi = next(t for t in re.findall(r"<tr>.*?</tr>", SEARCH_HTML, re.S) if "20260814003578" in t)
    amend = sdi.replace("20260814003578", "20260902000111").replace("2026.08.14", "2026.09.02")
    amend = re.sub(r"반기보고서(\s*\(2026\.06\))", r"[기재정정]반기보고서\1", amend, count=1)
    return SEARCH_HTML.replace("</tbody>", amend + "\n</tbody>")


def _frames(tmp_data) -> tuple[pl.DataFrame, pl.DataFrame]:
    return read_table("fundamentals_raw", settings=tmp_data), read_table("filings_index", settings=tmp_data)


def _no_corpcode(adapter) -> None:
    """Keep the crosswalk offline (no corpCode.xml request) when a test sets a fake key."""
    adapter._xw = {}


def test_statutory_deadlines_and_cap():
    assert dart_bulk.kr_statutory_deadline(date(2025, 12, 31), 12) == date(2026, 4, 7)   # 03-31 + 5 business days
    assert dart_bulk.kr_statutory_deadline(date(2026, 6, 30), 6) == date(2026, 8, 31)    # Sat 08-29 -> Mon
    assert dart_bulk.kr_statutory_deadline(date(2026, 3, 31), 3) == date(2026, 6, 1)     # Sat 05-30 -> Mon
    assert dart_bulk.statutory_knowledge_date(date(2026, 6, 30), 6, date(2026, 8, 20)) == date(2026, 8, 20)
    assert dart_bulk.statutory_knowledge_date(date(2026, 6, 30), 6, date(2026, 6, 30)) == date(2026, 7, 1)
    assert dart_bulk.add_business_days(date(2026, 11, 6), 1) == date(2026, 11, 9)


def test_resolver_join_order_versions_and_guards():
    end, fs = date(2026, 6, 30), date(2026, 10, 8)
    base = {"kind": "A002", "ym": "2026.06", "market": "kospi"}
    rows = [
        {**base, "corp_code": "00000001", "corp_name": "새이름", "rcp_no": "20260814000001", "rcept_dt": "2026-08-14",
         "report_nm": "반기보고서 (2026.06)"},
        {**base, "corp_code": "00000001", "corp_name": "새이름", "rcp_no": "20260902000001", "rcept_dt": "2026-09-02",
         "report_nm": "[기재정정]반기보고서 (2026.06)", "is_amendment": True},
        {**base, "corp_code": "00000002", "corp_name": "삼성SDI", "rcp_no": "20260814000002", "rcept_dt": "2026-08-14"},
        {**base, "corp_code": "00000003", "corp_name": "같은이름", "rcp_no": "20260814000003", "rcept_dt": "2026-08-14"},
        {**base, "corp_code": "00000004", "corp_name": "같은 이름", "rcp_no": "20260814000004", "rcept_dt": "2026-08-14"},
        {**base, "corp_code": "00000005", "corp_name": "API사", "stock_code": "333333", "rcp_no": "20260814000005",
         "rcept_dt": "2026-08-14"},
    ]
    xw = {"111111": {"dart_corp_code": "00000001"}}
    r = dart_bulk.FilingResolver(rows, first_seen=fs, crosswalk=xw)
    # renamed company: the bulk carries the old name, the crosswalk the corp_code -> versions of that corp
    a = r.resolve("111111", "옛이름", end, 6, 2026)
    assert a["basis"] == "corp_code" and a["native"] == "20260814000001" and not a["est"]
    assert a["kd_filing"] == date(2026, 8, 14) and a["kd_facts"] == date(2026, 9, 2)  # amendment before the zip date
    assert dart_bulk.FilingResolver(rows, first_seen=date(2026, 8, 20), crosswalk=xw).resolve(
        "111111", "옛이름", end, 6, 2026)["kd_facts"] == date(2026, 8, 14)              # amendment after the zip date
    assert dart_bulk.FilingResolver(rows, first_seen=fs, crosswalk=xw, amended_kd="original").resolve(
        "111111", "옛이름", end, 6, 2026)["kd_facts"] == date(2026, 8, 14)
    # unique-name join is learned into the crosswalk
    b = r.resolve("006400", "(주)삼성 SDI", end, 6, 2026)
    assert b["basis"] == "name" and r.learned["006400"]["dart_corp_code"] == "00000002"
    # API rows join on the stock code whatever the name
    assert r.resolve("333333", "전혀다른이름", end, 6, 2026)["basis"] == "stock_code"
    # guards -> conservative statutory deadline, flagged
    c = dart_bulk.FilingResolver(rows, first_seen=fs, crosswalk={"222222": {"dart_corp_code": "00000002"}}).resolve(
        "006400", "삼성SDI", end, 6, 2026)
    assert c["est"] and c["reason"] == "name_hit_claimed_by_other_code" and c["kd_facts"] == date(2026, 8, 31)
    assert c["native"] == "006400:2026H1:20260630" and c["basis"] == "kr_statutory_deadline"
    assert r.resolve("444444", "같은이름", end, 6, 2026)["reason"] == "ambiguous_name"
    late = dart_bulk.FilingResolver(rows, first_seen=date(2026, 8, 1), crosswalk=xw).resolve("111111", "x", end, 6, 2026)
    assert late["reason"] == "receipt_after_zip_date" and late["kd_facts"] == date(2026, 8, 1)  # capped at the zip date
    assert r.stats["join:corp_code"] == 1 and r.stats["unmatched:ambiguous_name"] == 1


def test_plan_windows_and_cover():
    today = date(2026, 11, 7)
    plan = dart_bulk.plan_windows({(date(2026, 6, 30), 6), (date(2025, 12, 31), 6)}, {"P", "A", "E"}, today, 180)
    assert {p[1] for p in plan} == {"P", "A"} and plan[0][2] == date(2026, 1, 1)
    assert max(p[3] for p in plan) == date(2026, 10, 28) and all((p[3] - p[2]).days < 180 for p in plan)
    api = dart_bulk.plan_windows({(date(2026, 6, 30), 6)}, {"P"}, today, dart_bulk.API_MAX_DAYS)
    assert [(p[2], p[3]) for p in api] == [(date(2026, 7, 1), date(2026, 9, 27)), (date(2026, 9, 28), date(2026, 10, 28))]
    cached = [{"start": "2026-07-01", "end": "2026-10-28", "crawled": "2026-08-20",
               "rows": [{"rcept_dt": "2026-08-14", "rcp_no": "1"}, {"rcept_dt": "2026-08-20", "rcp_no": "2"}]}]
    gaps, rows = dart_bulk.window_cover(date(2026, 7, 1), date(2026, 10, 28), cached, today)
    # final up to the day before the crawl: the crawl day and later are re-crawled, its partial rows dropped
    assert gaps == [(date(2026, 8, 20), date(2026, 10, 28))] and [r["rcp_no"] for r in rows] == ["1"]
    gaps, rows = dart_bulk.window_cover(date(2026, 7, 1), date(2026, 10, 28), [{**cached[0], "crawled": "2026-11-07"}],
                                        today)
    assert gaps == [] and len(rows) == 2


def test_bulk_kind_wins_for_six_month_fiscal_years():
    # a REIT with 6-month fiscal years (결산월 06, closes Jun + Dec): in the 4Q (사업보고서) bulk its Dec period is 6
    # months into the 'FY' by the months rule (-> 반기 A002), but the filing is a 사업보고서 (A001)
    assert dart_bulk.bulk_kind("2025_4Q") == "A001" and dart_bulk.bulk_kind("2026_3Q") == "A003"
    end = date(2025, 12, 31)
    rows = [{"corp_code": "01363818", "corp_name": "롯데리츠", "kind": "A001", "ym": "2025.12", "rcp_no": "20260310000001",
             "rcept_dt": "2026-03-10", "report_nm": "사업보고서 (2025.12)", "market": "kospi"}]
    r = dart_bulk.FilingResolver(rows, first_seen=date(2026, 11, 3))
    hit = r.resolve("330590", "롯데리츠", end, 6, 2026, kinds=["A001", "A002"])
    assert hit["kind"] == "A001" and hit["kd_facts"] == date(2026, 3, 10) and dart_bulk.reprt_code("A001", 6) == "11011"
    miss = dart_bulk.FilingResolver([], first_seen=date(2026, 11, 3)).resolve("330590", "x", end, 6, 2026,
                                                                            kinds=["A001", "A002"])
    assert miss["kind"] == "A001" and miss["kd_facts"] == date(2026, 4, 7)  # annual deadline of the assumed kind
    assert dart_bulk.reprt_code("A003", 3) == "11013" and dart_bulk.reprt_code("A003", 9) == "11014"


def test_synthetic_ids_unique_and_replace_drops_pk_collisions(tmp_data):
    # 6-month fiscal years: Mar-31 and Sep-30 are both 'Q3' of the same fiscal year -> the id carries the period end
    r = dart_bulk.FilingResolver([], first_seen=date(2026, 9, 9))
    a = r.resolve("355690", "리츠", date(2024, 3, 31), 9, 2024)["native"]
    b = r.resolve("355690", "리츠", date(2024, 9, 30), 9, 2024)["native"]
    assert a == "355690:2024Q3:20240331" and b == "355690:2024Q3:20240930"

    def fil(code, end, kd, fid):
        row = dart_bulk.filing_row(source_id="dart_bulk", native_key=fid, source_entity_id=code, form_type="11011",
                                   form_class="annual", knowledge_date=kd, period_end=end, fiscal_year=end.year,
                                   fiscal_period="FY", items=["x"])
        return dart_bulk.frame([row], "filings_index")

    old = fil("111111", date(2024, 12, 31), date(2025, 3, 20), "20250320000001")
    write_table(old, "filings_index", settings=tmp_data)
    new = fil("222222", date(2025, 12, 31), date(2026, 3, 20), "20250320000001")  # same filing id, other company
    keys = new.select("source_entity_id", "period_end")
    dart_bulk.replace_rows(new, "filings_index", keys, [("dart_bulk", 2025)], tmp_data)
    fi = read_table("filings_index", settings=tmp_data)
    assert fi.height == 1 and fi["source_entity_id"][0] == "222222" and fi["year"][0] == 2026


def test_split_targets_and_per_company_lookups(tmp_data):
    dec, mar, jun = date(2025, 12, 31), date(2025, 3, 31), date(2025, 6, 30)
    entries = ([(f"{i:06d}", f"회사{i}", "A", dec, "A001") for i in range(60)]
               + [(f"{i:06d}", f"회사{i}", "P", mar, "A001") for i in range(100, 145)]       # >= RARE_MAX: market-wide
               + [("900001", "삼월회사", "P", jun, "A001"), ("900002", "옛이름", "A", jun, "A001")])
    main, rare = dart_bulk.split_targets(entries)
    assert main == {(dec, "A001"), (mar, "A001")} and [r[0] for r in rare] == ["900001", "900002"]
    a = dart_bulk.DartBulkAdapter(settings=tmp_data)
    a._xw = {"900001": {"dart_corp_code": "00000901"}}
    calls = []

    def search_page(*, ptype, ctype, start, end, page, company=None):
        calls.append((ptype, ctype, start, end, company))
        return "<table></table>"

    a.search_page = search_page
    a.filing_rows_for_period("2025_4Q", {(dec, "A001")}, {"P"}, rare=rare)
    company = [c for c in calls if c[4]]
    assert [(c[0], c[1], c[4]) for c in company] == [("A", "", "cik:00000901"), ("A", "", "nm:옛이름")]
    assert company[0][2] == date(2025, 7, 1) and company[0][3] == date(2025, 11, 27)  # end + 1 .. end + 150 d
    assert {c[1] for c in calls if not c[4]} == {"P"}
    # resume: every window is cached -> no request
    calls.clear()
    a.filing_rows_for_period("2025_4Q", {(dec, "A001")}, {"P"}, rare=rare)
    assert calls == []


def test_parse_api_list_and_quota(tmp_data, monkeypatch):
    rows, pages = dart_bulk.parse_api_list(API_JSON)
    assert pages == 1 and len(rows) == 4 and {r["stock_code"] for r in rows} == {"005930", "006400", "105560"}
    am = next(r for r in rows if r["rcp_no"] == "20260902000111")
    assert am["is_amendment"] and am["kind"] == "A002" and am["ym"] == "2026.06" and am["rcept_dt"] == "2026-09-02"
    assert am["market"] == "P"
    assert dart_bulk.parse_api_list({"status": "013"}) == ([], 0)
    with pytest.raises(dart_bulk.DartBulkError):
        dart_bulk.parse_api_list({"status": "020", "message": "사용한도를 초과하였습니다."})

    class NoQuota:
        def try_consume(self, n=1):
            return False

    import app.core.ratelimit as rl
    monkeypatch.setattr(rl, "get_daily_quota", lambda source_id: NoQuota())
    a = dart_bulk.DartBulkAdapter(settings=tmp_data)
    with pytest.raises(dart_bulk.DartQuotaExceeded):  # refused before any request is built
        a.api_page(ptype="A002", ctype="P", start=date(2026, 7, 1), end=date(2026, 9, 27), page=1)


def test_streaming_zip_rows_and_codes():
    p = FIX / f"dart_bulk_{ZIPS[1]}"
    every = list(dart_bulk.iter_zip_rows(p))
    some = list(dart_bulk.iter_zip_rows(p, {"006400"}))
    assert some == [r for r in every if r["code"] == "006400"] and len(some) > 20
    assert dart_bulk.zip_codes(p) == set(ALL4)


def test_all_listed_scope_delta_replace_and_sticky(tmp_data):
    # 1) one company without filing dates: synthetic filing id, statutory date, period marked dates-incomplete
    a = _adapter(tmp_data, filing_dates=False)
    a.run(entities=["006400"])
    fr, fi = _frames(tmp_data)
    assert set(fr["filing_id"].unique()) == {"dart_bulk:006400:2026H1:20260630"} and fr["knowledge_date_estimated"].all()
    assert fi.height == 1 and "knowledge_date_basis=kr_statutory_deadline" in fi["items"][0].to_list()
    # 2) all_listed over a securities master of two codes: the incomplete period is redone and the synthetic rows of
    #    006400 are REPLACED by the matched filing (no duplicate facts); KOSPI + KOSDAQ crawled; issuer_id filled
    _identifier_map(tmp_data, ["005930", "006400"])
    b = _adapter(tmp_data, all_listed=True)
    f2, _ = b.run()
    assert f2.meta["stats"]["2026_2Q"]["codes_todo"] == 2 and f2.meta["all_listed"]
    fr, fi = _frames(tmp_data)
    assert set(fr["source_entity_id"].unique()) == {"005930", "006400"}
    assert "dart_bulk:006400:2026H1:20260630" not in set(fr["filing_id"]) and "dart_bulk:006400:2026H1:20260630" not in set(fi["filing_id"])
    sdi = fr.filter((pl.col("source_entity_id") == "006400") & H1_REV)
    assert sdi.height == 1 and sdi["knowledge_date"][0] == date(2026, 8, 14) and not sdi["knowledge_date_estimated"][0]
    assert set(fr["issuer_id"].unique()) == {"I_SEC", "I_SDI"} and set(fi["issuer_id"]) == {"I_SEC", "I_SDI"}
    assert {c[1] for c in b.search_calls} == {"P", "A"}
    side = b.load_sidecar("2026_2Q")
    assert side["codes"] == ["005930", "006400"] and side["provider"] == "dart_search" and side["dates_complete"]
    st = load_state("dart_bulk", tmp_data)
    assert st["scope_mode"] == "all_listed" and st["normalized"]["2026_2Q"]["scope"] == "all_listed"
    # crosswalk learned from unique name matches, linked to the securities master
    xw = pl.read_parquet(b.crosswalk_path())
    row = xw.filter(pl.col("kr_stock_code") == "006400").row(0, named=True)
    assert row["dart_corp_code"] == "00126362" and row["src"] == "name_match" and row["issuer_id"] == "I_SDI"
    # 3) the securities master grows: only the new codes are normalized (delta), existing rows untouched
    before = fr.filter(pl.col("source_entity_id") == "005930").select("concept", "period_start", "ingested_at")
    _identifier_map(tmp_data, ALL4)
    c = _adapter(tmp_data)  # all_listed unset -> sticky scope_mode
    f3, _ = c.run()
    assert f3.meta["all_listed"] and f3.meta["stats"]["2026_2Q"]["codes_todo"] == 2
    fr, fi = _frames(tmp_data)
    assert set(fr["source_entity_id"].unique()) == set(ALL4)
    after = fr.filter(pl.col("source_entity_id") == "005930").select("concept", "period_start", "ingested_at")
    assert before.sort(["concept", "period_start"]).equals(after.sort(["concept", "period_start"]))
    aj = fi.filter(pl.col("source_entity_id") == "095570")  # not on the search page -> statutory deadline
    assert aj.height == 1 and "unmatched_reason=no_filing_found" in aj["items"][0].to_list()
    assert fr.filter(pl.col("source_entity_id") == "095570")["knowledge_date_estimated"].all()
    # 4) nothing left: no work, no writes
    f4, rows4 = _adapter(tmp_data).run()
    assert f4.meta["periods"] == [] and rows4["fundamentals_raw"] == 0
    # 5) renormalize: every code again (amendment-aware dates), then the generation is closed
    e = _adapter(tmp_data, search_html=_amended_search_html(), renormalize=True)
    shutil.rmtree(e.root / "filings")  # force a re-crawl so the amended page is seen
    f5, _ = e.run()
    assert f5.meta["stats"]["2026_2Q"]["codes_todo"] == 4
    assert f5.meta["stats"]["2026_2Q"]["facts_kd_from_amendment"] == 1
    assert "renorm_since" not in load_state("dart_bulk", tmp_data)
    fr, fi = _frames(tmp_data)
    sdi = fr.filter((pl.col("source_entity_id") == "006400") & H1_REV)
    assert sdi.height == 1 and sdi["knowledge_date"][0] == date(2026, 9, 2)
    assert sdi["filing_id"][0] == "dart_bulk:20260814003578"
    fsdi = fi.filter(pl.col("source_entity_id") == "006400")
    assert fsdi.height == 1 and fsdi["knowledge_date"][0] == date(2026, 8, 14)  # the filing itself: original receipt
    assert {"facts_knowledge_date=2026-09-02", "n_versions=2"} <= set(fsdi["items"][0].to_list())
    assert fr.filter(pl.col("knowledge_date") <= pl.col("period_end")).height == 0  # PIT: after the period end
    pk = ["source_entity_id", "filing_id", "concept", "period_start", "period_end", "unit", "dimensions_hash"]
    assert fr.select(pk).is_duplicated().sum() == 0 and fi["filing_id"].is_duplicated().sum() == 0


def test_api_path_activates_with_key_and_upgrades(tmp_data, monkeypatch):
    a = _adapter(tmp_data)
    a.run(entities=SCOPE)
    assert a.load_sidecar("2026_2Q")["provider"] == "dart_search"
    secret = "test-key-0123456789"
    monkeypatch.setattr(tmp_data, "opendart_apikey", SecretStr(secret))
    api_calls = []

    def api_page(*, ptype, ctype, start, end, page):
        api_calls.append((ptype, ctype, start, end, page))
        return API_JSON if (ptype, ctype, start) == ("A002", "P", date(2026, 7, 1)) else {"status": "013"}

    b = _adapter(tmp_data)
    assert b.api_key_present()
    b.api_page = api_page
    _no_corpcode(b)
    f2, _ = b.run(entities=SCOPE)  # no renormalize: the key alone upgrades the period
    st = f2.meta["stats"]["2026_2Q"]
    assert st["kd_provider"] == "opendart_api" and st["join:stock_code"] == 3 and st["imputed"] == 0
    assert not b.search_calls and {c[0] for c in api_calls} == {"A002"} and len(api_calls) == 2
    _, fi = _frames(tmp_data)
    sdi = fi.filter(pl.col("source_entity_id") == "006400")
    assert "kd_provider=opendart_api" in sdi["items"][0].to_list() and sdi["knowledge_date"][0] == date(2026, 8, 14)
    assert b.load_sidecar("2026_2Q")["provider"] == "opendart_api"
    xw = pl.read_parquet(b.crosswalk_path())
    assert xw.filter(pl.col("kr_stock_code") == "105560")["src"][0] == "opendart_list"
    # the key value is never persisted (state, sidecars, caches, crosswalk, lake)
    for p in Path(tmp_data.data_dir).rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            if p.suffix == ".gz":
                data = gzip.decompress(data)
            assert secret.encode() not in data, p
    # resume: nothing left
    c = _adapter(tmp_data)
    c.api_page = api_page
    _no_corpcode(c)
    assert c.run(entities=SCOPE)[0].meta["periods"] == []


def test_key_upgrade_ignores_the_watermark_for_history(tmp_data, monkeypatch):
    a = _adapter(tmp_data, filing_dates=False)
    a.run(entities=SCOPE)  # provider 'none'
    late = "2027-12-31"    # a watermark that puts 2026_2Q outside the incremental window
    assert _adapter(tmp_data).fetch(since=late, entities=SCOPE).meta["periods"] == []
    monkeypatch.setattr(tmp_data, "opendart_apikey", SecretStr("history-test-key"))
    b = _adapter(tmp_data)
    b.api_page = lambda **kw: API_JSON if kw["start"] == date(2026, 7, 1) else {"status": "013"}
    _no_corpcode(b)
    f, _ = b.run(entities=SCOPE, since=late)
    assert [p for p in f.meta["stats"]] == ["2026_2Q"] and f.meta["stats"]["2026_2Q"]["kd_provider"] == "opendart_api"


def test_api_quota_falls_back_to_keyless_search(tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data, "opendart_apikey", SecretStr("another-test-key"))
    a = _adapter(tmp_data)

    def api_page(**_):
        raise dart_bulk.DartQuotaExceeded("OpenDART daily quota reached")

    a.api_page = api_page
    _no_corpcode(a)
    f, rows = a.run(entities=SCOPE)
    assert f.meta["stats"]["2026_2Q"]["kd_provider"] == "dart_search" and "2026_2Q" in f.meta["api_fallback"]
    assert f.meta["stats"]["2026_2Q"]["imputed"] == 0 and rows["filings_index"] == 3
    assert a.load_sidecar("2026_2Q")["provider"] == "dart_search"  # upgraded by the next run with quota


def test_offline_cache_only_and_ram_guard(tmp_data):
    a = _adapter(tmp_data, offline=True, filing_dates="cache")

    def no_list():
        raise AssertionError("offline run must not call list.do")

    a.list_files = no_list
    f, _ = a.run(entities=SCOPE)
    assert not a.search_calls and f.meta["stats"]["2026_2Q"]["imputed"] == 3  # no cached windows, nothing crawled
    assert a.load_sidecar("2026_2Q")["dates_complete"] is False
    # a later crawling run completes the dates, but the RAM guard defers it first (resumable, nothing written)
    b = _adapter(tmp_data)
    b._ram_ok = lambda: (False, "test floor")
    f2, rows2 = b.run(entities=SCOPE)
    assert f2.status == "partial" and f2.entities_failed["2026_2Q"].startswith("deferred")
    assert rows2["fundamentals_raw"] == 0 and f2.watermark is None
    c = _adapter(tmp_data)
    f3, _ = c.run(entities=SCOPE)
    assert f3.meta["stats"]["2026_2Q"]["imputed"] == 0 and c.load_sidecar("2026_2Q")["dates_complete"] is True
    fr, fi = _frames(tmp_data)
    assert not fr["knowledge_date_estimated"].any() and fi.height == 3  # imputed rows replaced, not duplicated


def test_crosswalk_from_identifier_map_and_discover(tmp_data):
    _identifier_map(tmp_data, ALL4, extra=[{"id_type": "dart_corp_code", "id_value": "00126380", "issuer_id": "I_SEC"}])
    a = _adapter(tmp_data, search_html="<table></table>")
    xw = a.crosswalk()
    assert xw["005930"]["dart_corp_code"] == "00126380" and xw["005930"]["src"] == "identifier_map"
    assert a.scope_codes(None) == set()  # all_listed off and no seed file -> seed scope (empty here)
    a.all_listed = True
    assert a.scope_codes(None) == set(ALL4)
    d = a.discover()
    sec = d.filter(pl.col("source_entity_id") == "005930").row(0, named=True)
    assert sec["dart_corp_code"] == "00126380" and sec["mic"] == "XKRX"
