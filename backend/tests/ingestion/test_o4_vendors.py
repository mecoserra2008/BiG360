"""O4: licensed-vendor adapter interface (app.ingestion.vendors) - protocol, file-drop adapter, staging contract."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.ingestion import vendors as v


def test_protocol_conformance():
    assert isinstance(v.FileDropVendor(), v.MarketDataVendor)
    for name in ("bloomberg", "lseg", "factset", "internal"):
        inst = v.get_vendor(name)
        assert isinstance(inst, v.MarketDataVendor) and inst.licence.vendor
        with pytest.raises(NotImplementedError, match="entitlement"):
            inst.eod_prices(["X"], date(2026, 1, 1), date(2026, 1, 2))
    assert v.get_vendor("none") is None
    with pytest.raises(KeyError):
        v.get_vendor("nope")


def test_file_drop_prices_to_staging(tmp_data):
    root = tmp_data.data_dir / "vendor_drop" / "bbg"
    (root / "prices").mkdir(parents=True)
    pl.DataFrame({"TICKER": ["AAPL US Equity", "AAPL US Equity", "VOD LN Equity"],
                  "DATE": ["2026-10-15", "2026-10-16", "2026-10-16"],
                  "PX_LAST": [231.5, 233.1, 72.4], "PX_VOLUME": [1.0e7, 1.1e7, 3.0e7],
                  "CRNCY": ["USD", "USD", "GBp"], "IGNORED": [1, 2, 3]}).write_csv(root / "prices" / "eod_20261016.csv")
    vend = v.FileDropVendor(vendor_id="bbg", root=root, column_map={"TICKER": "symbol", "DATE": "date",
                                                                     "PX_LAST": "close", "PX_VOLUME": "volume",
                                                                     "CRNCY": "currency"})
    assert "eod_prices" in vend.capabilities() and "fundamentals" not in vend.capabilities()
    df = v.prices_frame(vend, ["AAPL US Equity"], date(2026, 10, 1), date(2026, 10, 31))
    assert df.height == 2 and set(df["source_id"]) == {"other"} and set(df["price_basis"]) == {"raw"}
    assert df["year"].to_list() == [2026, 2026] and "IGNORED" not in df.columns
    n = v.ingest_prices(vend, [], date(2026, 10, 16), date(2026, 10, 16), settings=tmp_data)
    assert n == 2
    from app.core.storage import read_table

    got = read_table("prices_staging", settings=tmp_data)
    assert got.filter(pl.col("symbol") == "VOD LN Equity")["currency"].to_list() == ["GBp"]


def test_file_drop_fundamentals_point_in_time(tmp_data):
    root = tmp_data.data_dir / "vendor_drop" / "fs"
    (root / "fundamentals").mkdir(parents=True)
    pl.DataFrame({"source_entity_id": ["AAPL-US"], "concept": ["SALES"], "period_type": ["duration"],
                  "period_start": ["2024-09-29"], "period_end": ["2025-09-27"], "fiscal_period": ["FY"],
                  "value": [4.16e11], "unit": ["USD"], "knowledge_date": ["2025-10-31"]}
                 ).write_csv(root / "fundamentals" / "f.csv")
    vend = v.FileDropVendor(vendor_id="fs", root=root)
    assert v.ingest_fundamentals(vend, ["AAPL-US"], settings=tmp_data, write=False) == 1
    assert v.ingest_fundamentals(vend, ["AAPL-US"], since=date(2026, 1, 1), settings=tmp_data, write=False) == 0
