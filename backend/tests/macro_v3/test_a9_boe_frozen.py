"""COVERAGE_V3 A9: the frozen BoE weekly balance-sheet rows (IADB, robots-disallowed since 2026-11-07, not re-fetchable)
survive the nightly overwrite of cb_balance_sheets: they are snapshotted to data/raw/macro/cb once and a read error
never turns into an empty BoE set. Synthetic rows, no network."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.core import storage
from app.macro.cb import balance_sheets as bs


def _stored(s) -> None:
    rows = [("boe", date(2026, 10, d), comp, v, "boe:IADB") for d in (14, 21, 28)
            for comp, v in (("reserves", 650.0 + d), ("banknotes", 90.0))]
    rows += [("fed", date(2026, 10, 22), "total_assets", 6500.0, "fred:WALCL")]
    df = pl.DataFrame(rows, schema={"bank": pl.Utf8, "date": pl.Date, "component": pl.Utf8, "value_bn": pl.Float64,
                                    "source": pl.Utf8}, orient="row")
    df = bs.add_pace(df).with_columns(pl.col("bank").replace_strict(bs.CCY, default=None).alias("currency"),
                                      pl.lit(bs.METHOD).alias("method_version"), pl.lit(bs.now_utc()).alias("computed_at"))
    storage.write_table(df, "cb_balance_sheets", settings=s)


@pytest.fixture
def offline(monkeypatch):
    """FRED gives one new Fed week; ECB / BoJ fail (no network); any IADB request would fail the test."""
    import app.macro.cb.rates as rt

    monkeypatch.setattr(rt, "fetch_fred", lambda ids, s, start=None: {"WALCL": [(date(2026, 10, 29), 6_490_000.0)]})

    def http_get(src, url, *a, **k):
        assert "boeapps" not in url.lower() and "iadb" not in url.lower(), url
        raise RuntimeError("offline")

    monkeypatch.setattr(bs, "http_get", http_get)


def _boe(s) -> pl.DataFrame:
    return storage.read_table("cb_balance_sheets", settings=s).filter(pl.col("bank") == "boe")


def test_frozen_boe_rows_survive_rebuild_and_are_snapshotted(tmp_data, offline, monkeypatch):
    _stored(tmp_data)
    rep = bs.build_balance_sheets(tmp_data)
    assert rep["boe"].startswith("source_disallowed") and "kept 6" in rep["boe"]
    assert _boe(tmp_data).height == 6 and set(_boe(tmp_data)["source"]) == {"boe:IADB"}
    snap = bs.boe_snapshot_path(tmp_data)
    assert snap.exists() and pl.read_parquet(snap).height == 6
    # from now on the snapshot is the source: an unreadable table (or a rebuilt one without BoE rows) changes nothing
    monkeypatch.setattr(storage, "read_table", lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    rep = bs.build_balance_sheets(tmp_data)
    monkeypatch.undo()
    assert rep["boe"].startswith("source_disallowed") and _boe(tmp_data).height == 6
    fed = storage.read_table("cb_balance_sheets", settings=tmp_data).filter(pl.col("bank") == "fed")
    assert fed["date"].to_list() == [date(2026, 10, 29)]           # the other banks are still rebuilt (overwrite)


def test_read_error_without_snapshot_never_drops_boe_rows(tmp_data, offline, monkeypatch):
    _stored(tmp_data)
    monkeypatch.setattr(storage, "read_table", lambda *a, **k: (_ for _ in ()).throw(OSError("file replaced")))
    rep = bs.build_balance_sheets(tmp_data)
    monkeypatch.undo()
    assert rep["boe"].startswith("failed") and not bs.boe_snapshot_path(tmp_data).exists()
    assert _boe(tmp_data).height == 6                               # upsert kept them
    got = storage.read_table("cb_balance_sheets", settings=tmp_data)
    assert set(got.filter(pl.col("bank") == "fed")["date"]) == {date(2026, 10, 22), date(2026, 10, 29)}


def test_unreadable_snapshot_is_an_error_not_an_empty_set(tmp_data, offline):
    _stored(tmp_data)
    snap = bs.boe_snapshot_path(tmp_data)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"not parquet")
    with pytest.raises(bs.FrozenReadError):
        bs.boe_frozen_rows(tmp_data)
    rep = bs.build_balance_sheets(tmp_data)
    assert rep["boe"].startswith("failed") and _boe(tmp_data).height == 6


def test_fresh_install_has_nothing_to_keep(tmp_data, offline):
    assert bs.boe_frozen_rows(tmp_data) == []
    rep = bs.build_balance_sheets(tmp_data)
    assert rep["boe"].startswith("source_disallowed") and "kept 0" in rep["boe"] and _boe(tmp_data).height == 0
