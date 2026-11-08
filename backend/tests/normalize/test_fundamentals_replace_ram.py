"""ram_fix 2026-10-31: fundamentals_normalize replaces partitions one at a time and flushes every flush_issuers issuers
(it peaked at ~15 GB rewriting whole tables per 40-issuer batch). The written tables must not depend on either."""

from __future__ import annotations

import polars as pl

from app.core import config as config_mod
from app.core import schema as schema_mod
from app.core.storage import read_table
from app.normalize import fundamentals as F
from tests.normalize.test_fundamentals import EMPTY_RATES, ISS, _raw

ISS3 = pl.concat([ISS, ISS.filter(pl.col("issuer_id") == "IAAA").with_columns(pl.lit("IBBB").alias("issuer_id")),
                  ISS.filter(pl.col("issuer_id") == "IAAA").with_columns(pl.lit("ICCC").alias("issuer_id"))])


def _raw3() -> pl.DataFrame:
    a = _raw()
    b = a.with_columns(pl.lit("IBBB").alias("issuer_id"), pl.lit("0000000002").alias("source_entity_id"),
                       (pl.col("value") * 2).alias("value"))
    c = a.with_columns(pl.lit("ICCC").alias("issuer_id"), pl.lit("0000000003").alias("source_entity_id"),
                       (pl.col("value") * 3).alias("value"))
    return pl.concat([a, b, c])


def _tables(order=("issuer_id", "item_id", "period_kind", "period_end", "knowledge_date")) -> dict[str, pl.DataFrame]:
    out = {}
    for t in ("fundamentals_canonical", "fundamentals_wide"):
        df = read_table(t)
        drop = [c for c in ("computed_at", "run_id", "ingested_at") if c in df.columns]
        out[t] = df.drop(drop).sort([c for c in df.columns if c not in drop and df.schema[c] != pl.List])
    return out


def _run(tmp_path, monkeypatch, name, **kw) -> dict[str, pl.DataFrame]:
    monkeypatch.setenv("BIG360_DATA_DIR", str(tmp_path / name))
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    s = config_mod.get_settings()
    s.ensure_dirs()
    F.normalize(raw=_raw3(), issuers=ISS3, write=True, rates=EMPTY_RATES, settings=s, **kw)
    # second run for one issuer only: its rows are replaced, the other issuers' rows stay
    F.normalize(["IBBB"], raw=_raw3(), issuers=ISS3, write=True, rates=EMPTY_RATES, settings=s, **kw)
    return _tables()


def test_partitionwise_replace_and_flush_size_do_not_change_the_tables(tmp_path, monkeypatch):
    one_by_one = _run(tmp_path, monkeypatch, "a", batch_size=1, flush_issuers=1)
    flushed = _run(tmp_path, monkeypatch, "b", batch_size=1, flush_issuers=400)
    big_batch = _run(tmp_path, monkeypatch, "c", batch_size=40, flush_issuers=400)
    for t in ("fundamentals_canonical", "fundamentals_wide"):
        assert one_by_one[t].height > 0
        assert one_by_one[t].equals(flushed[t]), t
        assert one_by_one[t].equals(big_batch[t]), t
        assert set(one_by_one[t]["issuer_id"].unique()) == {"IAAA", "IBBB", "ICCC"}
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
