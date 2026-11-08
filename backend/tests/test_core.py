from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.core.config import Settings
from app.core.logsetup import redact
from app.core.ratelimit import RateLimit, TokenBucket
from app.core.schema import ContractViolation, conform, validate
from app.core.storage import duckdb_connect, read_table, register_views, write_table


def _fx(rows):
    return pl.DataFrame(
        {
            "date": [r[0] for r in rows],
            "year": [r[0].year for r in rows],
            "currency": [r[1] for r in rows],
            "usd_per_unit": [r[2] for r in rows],
            "units_per_usd": [1 / r[2] for r in rows],
            "source_id": ["ecb"] * len(rows),
            "ingested_at": [datetime.now(UTC)] * len(rows),
        }
    )


def test_settings_case_insensitive_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("Polygon_APIKEY=abc12345\nfred_apikey=\nSEC_USER_AGENT=Test me@x.com\n", encoding="utf-8")
    for k in ("POLYGON_APIKEY", "Polygon_APIKEY", "FRED_APIKEY"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=str(env))
    assert s.secret("polygon_apikey") == "abc12345"
    assert s.keys_present()["polygon"] is True
    assert s.keys_present()["fred"] is False
    assert "abc12345" not in repr(s)


def test_redact():
    assert redact("https://x/api?api_key=SECRET1&q=1", []) == "https://x/api?api_key=***&q=1"
    assert redact("token is SECRET99", ["SECRET99"]) == "token is ***"


def test_conform_casts_and_rejects(tmp_data):
    df = _fx([(date(2024, 1, 2), "EUR", 1.1)])
    out = conform(df, "fx_rates")
    assert out.schema["year"] == pl.Int16
    assert validate(out, "fx_rates") == []
    with pytest.raises(ContractViolation):
        conform(df.with_columns(pl.lit("bogus").alias("source_id")), "fx_rates")  # enum violation
    with pytest.raises(ContractViolation):
        conform(df.drop("usd_per_unit"), "fx_rates")  # missing required
    with pytest.raises(ContractViolation):
        conform(pl.concat([df, df]), "fx_rates")  # duplicate PK


def test_write_upsert_roundtrip_and_duckdb_view(tmp_data):
    write_table(_fx([(date(2024, 1, 2), "EUR", 1.10), (date(2023, 12, 29), "EUR", 1.09)]), "fx_rates")
    write_table(_fx([(date(2024, 1, 2), "EUR", 1.11), (date(2024, 1, 3), "JPY", 0.007)]), "fx_rates")
    df = read_table("fx_rates").sort("date")
    assert df.height == 3
    assert df.filter((pl.col("date") == date(2024, 1, 2)) & (pl.col("currency") == "EUR"))["usd_per_unit"][0] == 1.11
    assert df.schema["year"] == pl.Int16
    con = duckdb_connect()
    register_views(con, ["fx_rates", "prices_daily"])
    assert con.execute("select count(*) from fx_rates").fetchone()[0] == 3
    assert con.execute("select count(*) from prices_daily").fetchone()[0] == 0  # typed empty view


def test_token_bucket_sync_and_async():
    b = TokenBucket(RateLimit(20.0, 1))
    t0 = time.monotonic()
    for _ in range(5):
        b.acquire()
    assert time.monotonic() - t0 >= 0.18  # 4 waits of 50 ms

    async def go():
        a = TokenBucket(RateLimit(50.0, 2))
        await asyncio.gather(*(a.acquire_async() for _ in range(6)))
        return a.total_acquired

    assert asyncio.run(go()) == 6
    assert TokenBucket(RateLimit(0.01, 1)).acquire(timeout=0.01) is True
