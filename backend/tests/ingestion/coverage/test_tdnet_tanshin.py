"""TDnet kessan-tanshin XBRL -> fundamentals_raw on a recorded (trimmed) package: Fast Retailing FY Aug-2026
(posted 2026-10-08), Summary iXBRL + BS/PL attachments."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.core.schema import conform
from app.ingestion import tdnet
from app.ingestion._global_common import frame

FX = Path(__file__).parent / "fixtures"
ZIP = FX / "tdnet_tanshin_9983_fy2026.zip"


def _facts() -> pl.DataFrame:
    rows, meta = tdnet.parse_tanshin_zip(ZIP.read_bytes(), source_entity_id="9983", filing_id="tdnet:140120261008000001",
                                         knowledge_date=date(2026, 10, 8), title_fp="FY")
    assert meta["fye"] == date(2026, 8, 31) and meta["files"] == 3
    return conform(frame(rows, "fundamentals_raw"), "fundamentals_raw")


def test_summary_actuals_and_forecast():
    df = _facts()
    sales = df.filter((pl.col("concept") == "tse-ed-t:SalesIFRS") & (pl.col("dimensions_hash") == ""))
    fy26 = sales.filter(pl.col("period_end") == date(2026, 8, 31))
    assert fy26.height == 1 and fy26["value"][0] == 3_963_453_000_000 or abs(fy26["value"][0] - 3.9634e12) < 1e9
    assert fy26["period_start"][0] == date(2025, 9, 1) and fy26["fiscal_period"][0] == "FY"
    assert fy26["fiscal_year"][0] == 2026 and fy26["unit"][0] == "JPY" and fy26["taxonomy"][0] == "other"
    assert not fy26["is_forecast"][0] and fy26["consolidated"][0]
    fc = df.filter((pl.col("concept") == "tse-ed-t:SalesIFRS") & pl.col("is_forecast"))
    assert fc.height >= 1 and fc["period_end"].max() == date(2027, 8, 31)
    assert abs(fc.filter(pl.col("dimensions_json").str.contains("ForecastMember"))["value"][0] - 4.45e12) < 1e9
    # forecasts are dimensional and dated after the filing -> excluded from canonical twice over
    assert (fc["dimensions_hash"] != "").all() and (fc["knowledge_date"] < fc["period_end"]).all()
    eps = df.filter((pl.col("concept") == "tse-ed-t:BasicEarningsPerShareIFRS") & (pl.col("period_end") == date(2026, 8, 31))
                    & ~pl.col("is_forecast"))
    assert eps.height == 1 and abs(eps["value"][0] - 1768.08) < 0.01 and eps["unit"][0] == "JPY/shares"


def test_attachment_statements_use_jp_qnames():
    df = _facts()
    rev = df.filter((pl.col("concept") == "jpigp_cor:RevenueIFRS") & (pl.col("dimensions_hash") == "")
                    & (pl.col("period_end") == date(2026, 8, 31)))
    assert rev.height == 1 and rev["taxonomy"][0] == "jpigp" and abs(rev["value"][0] - 3.9634e12) < 1e9
    assets = df.filter((pl.col("concept") == "jpigp_cor:AssetsIFRS") & (pl.col("dimensions_hash") == ""))
    assert assets.height >= 2 and assets["period_type"].unique().to_list() == ["instant"]
    # non-consolidated summary facts of a consolidated filer are kept but dimensional
    nonc = df.filter(~pl.col("consolidated"))
    assert nonc.height > 0 and (nonc["dimensions_json"].str.contains("NonConsolidated")).all()


def test_number_formats():
    from lxml import etree

    el = etree.fromstring('<x xmlns:ix="http://www.xbrl.org/2008/inlineXBRL" scale="6" sign="-" '
                          'format="ixt:numdotdecimal">1,234</x>')
    assert tdnet._ix_number(el) == -1_234_000_000
    el = etree.fromstring('<x format="ixt:zerodash">-</x>')
    assert tdnet._ix_number(el) == 0.0
    el = etree.fromstring('<x xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>')
    assert tdnet._ix_number(el) is None


def test_adapter_normalize_lands_and_parses(tmp_data, monkeypatch):
    a = tdnet.TdnetAdapter(settings=tmp_data)
    row = {"date": date(2026, 10, 8), "time": "15:30", "code5": "99830", "name": "FAST RETAILING",
           "title": "2026年8月期 決算短信〔IFRS〕(連結)", "pdf": "140120261008000001.pdf",
           "xbrl": "081220261008000001.zip", "exchange": "東", "id": "140120261008000001"}

    class R:
        content = ZIP.read_bytes()

    monkeypatch.setattr(a.http, "get", lambda *a_, **k: R())
    p = a.land_xbrl(row)
    assert Path(p).exists() and a.land_xbrl(row) == p  # second call: already landed
    row["_xbrl_path"] = p
    from app.ingestion.base import FetchResult
    from datetime import UTC, datetime

    f = FetchResult("tdnet", started_at=datetime.now(UTC))
    f.meta["rows"] = [row]
    out = a.normalize(f)
    assert out["filings_index"].height == 1 and out["filings_index"]["filing_id"][0] == "tdnet:140120261008000001"
    fr = out["fundamentals_raw"]
    assert fr.height > 200 and set(fr["filing_id"].unique()) == {"tdnet:140120261008000001"}
    assert fr["source_entity_id"].unique().to_list() == ["9983"] and fr["is_forecast"].sum() > 0
    assert not f.entities_failed


def test_tdnet_family_and_forecast_exclusion_in_normalizer():
    from app.normalize import fundamentals as F

    df = _facts().with_columns(pl.lit("I000000000001").alias("issuer_id"))
    prep = F._prepare_facts(df)
    assert prep.filter(pl.col("concept").str.starts_with("tse-ed-t:"))["family"].unique().to_list() == ["tdnet"]
    assert prep.filter(pl.col("is_forecast")).height == 0
    singles = F.load_canonical()["singles"]
    assert singles.filter((pl.col("family") == "tdnet") & (pl.col("key") == "tse-ed-t:SalesIFRS"))["item_id"].to_list() == ["revenue"]
    assert F.SOURCE_PRECEDENCE["JP"].index("tdnet") == F.SOURCE_PRECEDENCE["JP"].index("edinet") + 1
    assert F.SOURCE_PRECEDENCE["JP"].index("tdnet") < F.SOURCE_PRECEDENCE["JP"].index("yfinance")


def test_job_writes_fundamentals_raw():
    import yaml
    from app.core.config import get_settings

    jobs = yaml.safe_load((get_settings().contracts_dir / "jobs.yaml").read_text(encoding="utf-8"))["jobs"]
    j = next(x for x in jobs if x["id"] == "tdnet_disclosures")
    assert j["dag"] == "nightly" and "fundamentals_raw" in j["writes"]
    assert SimpleNamespace  # keep import used
