"""app.normalize.fx: period-end vs period-average conversion, sub-units, staleness."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.normalize import fx


def _rates() -> pl.DataFrame:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(0, 366)]
    rows = []
    for i, d in enumerate(days):
        if d.weekday() >= 5:
            continue  # no weekend fixings
        rows.append({"date": d, "currency": "EUR", "usd_per_unit": 1.0 + i / 1000.0, "source_id": "ecb"})
        rows.append({"date": d, "currency": "GBP", "usd_per_unit": 1.25, "source_id": "fred"})
    return pl.DataFrame(rows, schema=fx.RATE_SCHEMA)


def test_rate_on_uses_last_fixing_on_or_before():
    r = _rates()
    sat = date(2024, 3, 2)  # Saturday -> Friday's fixing
    rate, src = fx.rate_on("EUR", sat, r)
    fri = r.filter((pl.col("currency") == "EUR") & (pl.col("date") == date(2024, 3, 1))).item(0, "usd_per_unit")
    assert rate == pytest.approx(fri) and src == "ecb"
    assert fx.rate_on("USD", sat, r) == (1.0, "identity")
    assert fx.rate_on("EUR", date(2026, 1, 1), r) is None       # too stale


def test_period_average_and_subunits():
    r = _rates()
    avg, src = fx.period_average("EUR", date(2024, 1, 1), date(2024, 3, 31), r)
    sub = r.filter((pl.col("currency") == "EUR") & (pl.col("date") <= date(2024, 3, 31)))
    assert avg == pytest.approx(sub.get_column("usd_per_unit").mean())
    assert fx.rate_on("GBp", date(2024, 6, 3), r)[0] == pytest.approx(0.0125)
    assert fx.to_usd(100.0, "GBP", d=date(2024, 6, 3), rates=r) == pytest.approx(125.0)


def test_convert_frame_methods():
    r = _rates()
    df = pl.DataFrame({
        "value": [100.0, 200.0, 50.0, 7.0, 1.0],
        "currency": ["EUR", "EUR", "USD", None, "JPY"],
        "period_start": [None, date(2024, 1, 1), date(2024, 1, 1), None, None],
        "period_end": [date(2024, 3, 31), date(2024, 3, 31), date(2024, 3, 31), date(2024, 3, 31), date(2024, 3, 31)],
        "period_kind": ["INSTANT", "Q", "Q", "INSTANT", "INSTANT"],
    }, schema_overrides={"period_start": pl.Date})
    out = fx.convert_frame(df, r)
    assert out.get_column("fx_method").to_list() == ["period_end", "period_average", "period_average", "none", "none"]
    pe = fx.rate_on("EUR", date(2024, 3, 31), r)[0]
    av = fx.period_average("EUR", date(2024, 1, 1), date(2024, 3, 31), r)[0]
    assert out.item(0, "value_usd") == pytest.approx(100 * pe)
    assert out.item(1, "value_usd") == pytest.approx(200 * av)
    assert out.item(2, "value_usd") == 50.0 and out.item(2, "fx_source") == "identity"
    assert out.item(3, "value_usd") is None and out.item(4, "value_usd") is None   # non-monetary / no JPY rates
    assert out.height == df.height
