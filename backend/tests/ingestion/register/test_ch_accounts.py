"""Coverage v2 W2: Companies House bulk accounts - iXBRL parser, member naming, archive lists, HTTP Range zip reader,
group / company split and the end-to-end archive processing on an in-memory zip (no network)."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.ingestion import _ixbrl as X
from app.ingestion import ch_accounts as C

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "register"
DOC = (FIX / "ch_accounts_sample.html").read_bytes()


def test_parse_number_formats():
    assert X.parse_number("12,345", "ixt2:numdotdecimal") == 12345.0
    assert X.parse_number("7.000,25", "ixt:numcommadecimal") == 7000.25
    assert X.parse_number("-", "ixt2:zerodash") == 0.0
    assert X.parse_number("1 234", None) == 1234.0
    assert X.parse_number("(1,250.50)", "ixt2:numdotdecimal") == 1250.5
    assert X.parse_number("n/a", None) is None


def test_taxonomy_of_resolves_by_namespace():
    assert X.taxonomy_of("https://xbrl.ifrs.org/taxonomy/2023-03-23/ifrs-full", "ifrs-full", "Revenue") == ("ifrs-full", "Revenue")
    assert X.taxonomy_of("http://xbrl.frc.org.uk/fr/2023-01-01/core", "c", "ProfitLoss") == ("uk-core", "uk-core:ProfitLoss")
    assert X.taxonomy_of("http://xbrl.frc.org.uk/cd/2023-01-01/business", "bus", "EntityCurrentLegalOrRegisteredName") \
        == ("uk-core", "uk-bus:EntityCurrentLegalOrRegisteredName")
    assert X.taxonomy_of("http://acme.example/2025", "acme", "Widgets") == ("other", "acme:Widgets")


def test_parse_ixbrl_fixture():
    p = X.parse_ixbrl(DOC)
    assert p["entity"] == {"scheme": "http://www.companieshouse.gov.uk/", "id": "01234567"}
    assert p["period_end"] == date(2025, 12, 31)
    f = {(x["concept"], x["period_end"], tuple(sorted((x["dims"] or {}).items())), x["unit"]): x["value"] for x in p["facts"]}
    assert f[("uk-core:TurnoverRevenue", date(2025, 12, 31), (), "GBP")] == 12_345_000.0        # scale 3
    assert f[("uk-core:TurnoverRevenue", date(2024, 12, 31), (), "GBP")] == 11_000_000.0
    assert f[("uk-core:ProfitLoss", date(2025, 12, 31), (), "GBP")] == -1250.5                 # sign="-"
    assert f[("uk-core:CostSales", date(2025, 12, 31), (), "GBP")] == 7000.25                  # comma decimal
    assert f[("uk-core:DividendsPaid", date(2025, 12, 31), (), "GBP")] == 0.0                  # zerodash
    assert f[("CashAndCashEquivalents", date(2025, 12, 31), (), "GBP")] == 900.0              # ix:exclude ignored
    assert f[("BasicEarningsLossPerShare", date(2025, 12, 31), (), "GBP/shares")] == 0.12
    assert ("uk-core:Stocks", date(2025, 12, 31), (), "GBP") not in f                          # xsi:nil
    n_turn = [x for x in p["facts"] if x["concept"] == "uk-core:TurnoverRevenue" and not x["dims"]
              and x["period_end"] == date(2025, 12, 31) and x["unit"] == "GBP"]
    assert len(n_turn) == 1                                                                    # repeated fact de-duplicated
    seg = [x for x in p["facts"] if x["dims"] and "core:SegmentsDimension" in x["dims"]]
    assert seg and seg[0]["value"] == 8000.0


def test_member_rows_and_group_split():
    rows, frow = C.member_rows(DOC, "01234567", date(2025, 12, 31), date(2026, 9, 30), True,
                               "Prod224_2609_01234567_20251231.html")
    assert frow["filing_id"] == "companies_house:01234567_20251231_20260930"
    assert frow["form_class"] == "annual" and frow["has_xbrl"] and frow["source_entity_id"] == "01234567"
    df = pl.DataFrame(rows)
    assert set(df["source_id"].unique()) == {"companies_house"}
    assert df["knowledge_date_estimated"].all() and (df["knowledge_date"] == date(2026, 9, 30)).all()
    na = df.filter(pl.col("concept") == "uk-core:NetAssetsLiabilities")
    grp = na.filter(pl.col("dimensions_hash") == "")
    assert grp.height == 1 and grp["value"][0] == 5000.0 and grp["consolidated"][0] is True   # group member -> no dim
    comp = na.filter(pl.col("dimensions_hash") != "")
    assert comp.height == 1 and comp["consolidated"][0] is False                                # company member stays dimensional
    fy = df.filter((pl.col("concept") == "uk-core:TurnoverRevenue") & (pl.col("dimensions_hash") == "")
                   & (pl.col("period_end") == date(2025, 12, 31)))
    assert fy["fiscal_period"][0] == "FY" and fy["fiscal_year"][0] == 2025
    # PIT: no fact is known before its period ends
    assert (df["knowledge_date"] >= df["period_end"]).all()


def test_parse_member_and_numbers():
    assert C.parse_member("Prod224_2609_00000371_20251231.html") == ("00000371", date(2025, 12, 31))
    assert C.parse_member("Prod223_4341_SO308128_20260331.html") == ("SO308128", date(2026, 3, 31))
    assert C.parse_member("Prod223_4341_07905012_20260131_CIC.zip") is None
    assert C.norm_company_number("102498") == "00102498" and C.norm_company_number("SC 123456") == "SC123456"
    assert C.norm_company_number("HRB 719915") is None


def test_list_archives_pages():
    daily = ('<a href="Accounts_Bulk_Data-2026-10-30.zip">x</a> <a href="Accounts_Bulk_Data-2026-10-31.zip">y</a>')
    a = C.list_archives(daily, "daily")
    assert [x["name"] for x in a] == ["Accounts_Bulk_Data-2026-10-31.zip", "Accounts_Bulk_Data-2026-10-30.zip"]
    assert a[0]["day"] == date(2026, 10, 31) and a[0]["month"] == date(2026, 10, 1)
    monthly = ('<a href="Accounts_Monthly_Data-September2026.zip">s</a>'
               '<a href="archive/Accounts_Monthly_Data-JanuaryToDecember2008.zip">old</a>'
               '<a href="archive/Accounts_Monthly_Data-February2010.zip">f</a>')
    m = C.list_archives(monthly, "monthly")
    assert [x["name"] for x in m] == ["Accounts_Monthly_Data-September2026.zip", "Accounts_Monthly_Data-February2010.zip"]
    assert m[0]["day"] == date(2026, 9, 30) and m[1]["url"].endswith("archive/Accounts_Monthly_Data-February2010.zip")


class FakeRangeHttp:
    """Serves bytes with HTTP Range semantics (206 + Content-Range), counting requests."""

    def __init__(self, blob: bytes):
        self.blob = blob
        self.calls = 0

    def request(self, method, url, headers=None, cache_ttl=0, **kw):
        from app.core.http import CachedResponse

        self.calls += 1
        rng = (headers or {}).get("Range", "")
        a, b = rng.replace("bytes=", "").split("-")
        a, b = int(a), min(int(b), len(self.blob) - 1)
        return CachedResponse(206, url, self.blob[a:b + 1], {"content-range": f"bytes {a}-{b}/{len(self.blob)}"})

    def get(self, url, params=None, cache_ttl=None, **kw):
        from app.core.http import CachedResponse

        return CachedResponse(200, url, b"", {})


def _zip_blob(n_other: int = 300) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(n_other):
            z.writestr(f"Prod224_2609_{i:08d}_20251231.html", b"<html>" + b"x" * 2000 + b"</html>")
        z.writestr("Prod224_2609_01234567_20251231.html", DOC)
    return buf.getvalue()


def test_remote_zip_reads_only_what_it_needs():
    blob = _zip_blob()
    http = FakeRangeHttp(blob)
    z, rf = C.open_remote_zip("https://example/archive.zip", http)
    assert len(z.namelist()) == 301
    assert z.read("Prod224_2609_01234567_20251231.html") == DOC
    assert rf.n_bytes <= len(blob) + C.BLOCK  # never more than one pass
    assert http.calls <= 6                     # size probe + end-of-directory + central directory + one member


def test_process_archive_end_to_end(tmp_data):
    from app.core.storage import read_table

    blob = _zip_blob(50)
    ad = C.ChAccounts(tmp_data, http=FakeRangeHttp(blob))
    arc = {"name": "Accounts_Monthly_Data-September2026.zip", "url": "https://example/m.zip", "kind": "monthly",
           "month": date(2026, 9, 1), "day": date(2026, 9, 30)}
    info = ad.process(arc, {"01234567"})
    assert info["members"] == 51 and info["matched"] == 1 and info["parsed"] == 1 and info["rows"] > 5
    raw = read_table("fundamentals_raw", settings=tmp_data)
    assert set(raw["source_entity_id"].unique()) == {"01234567"}
    fi = read_table("filings_index", settings=tmp_data)
    assert fi["filing_id"].to_list() == ["companies_house:01234567_20251231_20260930"]
    assert fi["doc_path"][0].endswith(".html.gz") and (tmp_data.data_dir / fi["doc_path"][0]).exists()
    st = C.load_state(tmp_data)
    assert st["archives"]["Accounts_Monthly_Data-September2026.zip"]["matched"] == 1
    # a month already covered by daily files is not re-read from its monthly archive
    st["archives"]["Accounts_Bulk_Data-2026-09-15.zip"] = {"kind": "daily", "month": "2026-09-01"}
    assert "2026-09-01" in C.ChAccounts(tmp_data, http=FakeRangeHttp(blob)).__class__.daily_months(
        type("S", (), {"state": st})())


def test_ch_facts_map_to_canonical_items(tmp_data):
    """FRC 'core' concepts reach the canonical items through the ifrs_uk chains (canonical_line_items_ext_global)."""
    from app.ingestion._global_common import frame
    from app.normalize import fundamentals as FN

    FN.load_canonical.cache_clear()
    rows, _ = C.member_rows(DOC, "01234567", date(2025, 12, 31), date(2026, 9, 30), True, "m.html")
    raw = frame(rows, "fundamentals_raw").with_columns(pl.lit("I_ACME").alias("issuer_id"))
    issuers = pl.DataFrame({"issuer_id": ["I_ACME"], "region": ["UK"], "reporting_gaap": ["ukgaap"],
                            "reporting_currency": ["GBP"], "subsector_id": [None], "fiscal_year_end_mmdd": ["12-31"]})
    out = FN.map_raw_to_canonical(raw, issuers)
    got = {(r["item_id"], r["period_end"]): r["value"] for r in out.iter_rows(named=True)}
    assert got[("revenue", date(2025, 12, 31))] == 12_345_000.0
    assert got[("revenue", date(2024, 12, 31))] == 11_000_000.0
    assert got[("net_income", date(2025, 12, 31))] == -1250.5
    assert got[("cost_of_revenue", date(2025, 12, 31))] == 7000.25
    assert got[("total_equity", date(2025, 12, 31))] == 5000.0           # group net assets, not the company's 4,000
    assert got[("cash_and_equivalents", date(2025, 12, 31))] == 900.0     # ifrs-full fact of the same document
