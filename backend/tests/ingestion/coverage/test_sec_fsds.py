"""SEC Financial Statement Data Sets loader on a recorded mini quarter zip (real TSMC 20-F rows from 2026q2 + a US 10-K,
a dimensional row and a custom-tag row that must be dropped)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import polars as pl

from app.ingestion import sec_fsds

FX = Path(__file__).parent / "fixtures" / "fsds_2026q2_tsm.zip"
TSM = "0001046179"


def test_quarter_helpers():
    assert sec_fsds.quarters_between("2009q3", "2010q2") == ["2009q3", "2009q4", "2010q1", "2010q2"]
    assert sec_fsds.last_published_quarter(date(2026, 10, 11)) == "2026q3"
    assert sec_fsds.last_published_quarter(date(2026, 2, 1)) == "2025q4"


def test_extract_filters_fpi_forms_dims_and_custom_tags():
    sub, num = sec_fsds.extract_quarter(FX.read_bytes(), ciks=None)
    assert sub["form"].to_list() == ["20-F"]  # the US 10-K is not an FPI form
    assert num["adsh"].unique().to_list() == sub["adsh"].to_list()
    assert "CustomThing" not in num["tag"].to_list()  # version == adsh (custom)
    assert num.filter((pl.col("tag") == "Revenue")).height == 0  # only the dimensional Revenue row existed
    sub2, _ = sec_fsds.extract_quarter(FX.read_bytes(), ciks={"320193"})
    assert set(sub2["form"]) == {"20-F", "10-K"}  # universe CIKs keep any form


def test_facts_values_units_and_pit():
    sub, num = sec_fsds.extract_quarter(FX.read_bytes(), ciks=None)
    df = sec_fsds.fsds_to_facts(sub, num, ciks={"1046179"}, keep=None)
    pl_ = df.filter((pl.col("concept") == "ProfitLoss") & (pl.col("period_end") == date(2025, 12, 31)) & (pl.col("unit") == "TWD"))
    assert pl_.height == 1 and pl_["value"][0] == 1_695_124_900_000
    r = pl_.row(0, named=True)
    assert r["source_entity_id"] == TSM and r["filing_id"] == "sec_edgar:0001628280-26-025362" and r["taxonomy"] == "ifrs-full"
    assert r["knowledge_date"] == date(2026, 4, 16) and not r["knowledge_date_estimated"]
    assert r["period_start"] == date(2025, 1, 1) and r["fiscal_year"] == 2025 and r["fiscal_period"] == "FY"
    assets = df.filter((pl.col("concept") == "Assets") & (pl.col("unit") == "TWD") & (pl.col("period_end") == date(2025, 12, 31)))
    assert assets["value"][0] == 7_932_842_500_000 and assets["period_type"][0] == "instant" and assets["period_start"][0] is None
    eps = df.filter(pl.col("concept") == "BasicEarningsLossPerShare")
    assert set(eps["unit"]) == {"TWD/shares", "USD/shares"}  # FSDS bare-currency per-share uom fixed
    assert df["dimensions_hash"].unique().to_list() == [""]
    assert sec_fsds.fsds_to_facts(sub, num, ciks={"999"}, keep=None).height == 0


def test_run_resumable_state(tmp_data, monkeypatch):
    a = sec_fsds.SecFsdsAdapter(settings=tmp_data, start="2026q1", end="2026q2", ciks="1046179")
    monkeypatch.setattr(a, "universe_ciks", lambda: {"1046179"})

    def fake_download(url, dest, **kw):
        if "2026q1" in url:
            from app.core.http import HttpError

            raise HttpError(404, url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FX, dest)
        return dest

    monkeypatch.setattr(a.http, "download", fake_download)
    f, rows = a.run()
    assert f.entities_ok == ["2026q2"] and rows["fundamentals_raw"] > 0
    assert f.meta["unpublished"] == ["2026q1"] and not f.entities_failed
    from app.ingestion._global_common import load_state

    st = load_state("sec_fsds", tmp_data)
    assert list(st["done"]) == ["2026q2"] and st["ciks_done"] == ["1046179"]
    assert a.pending_quarters() == ["2026q1"]  # done quarter skipped on the next run
    from app.core.storage import read_table

    fr = read_table("fundamentals_raw", settings=tmp_data)
    assert set(fr["source_id"]) == {"sec_fsds"} and fr["is_latest"].all()


def test_registered_identity_and_precedence():
    from app.normalize import fundamentals as F
    from app.normalize.identity import SOURCE_ENTITY_ID_TYPE

    assert SOURCE_ENTITY_ID_TYPE["sec_fsds"] == "cik"
    for reg, order in F.SOURCE_PRECEDENCE.items():
        if "sec_edgar" in order:
            assert order.index("sec_fsds") == order.index("sec_edgar") + 1, reg
            assert order.index("sec_fsds") < order.index("yfinance")
