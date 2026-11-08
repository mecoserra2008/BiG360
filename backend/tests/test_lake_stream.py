"""app.core.lake_stream.StagedWrite (coverage v3 A1): staged, streamed commits give exactly the table the previous
in-memory write paths produced (upsert + drop_stale_rows / drop_stale_sector_rows; overwrite + month-end retention),
leave the table untouched on failure, and never rewrite partitions they do not need to."""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.core.lake_stream import StagedWrite, distinct_values
from app.core.schema import conform
from app.core.storage import read_table, table_dir, write_table

OLD, NEW = datetime(2026, 10, 30, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC)


def _ratio(iid: str, metric: str, kd: date, value: float, ts: datetime, kind: str = "TTM") -> dict:
    return {"issuer_id": iid, "metric_id": metric, "period_kind": kind, "fiscal_year": 2025, "fiscal_period": kind,
            "period_end": date(2025, 12, 31), "value": value, "unit": "pct", "inputs_complete": True,
            "knowledge_date": kd, "dq_score": 1.0, "computed_at": ts, "run_id": "r"}


def _norm(df: pl.DataFrame, pk: list[str]) -> pl.DataFrame:
    return df.select(sorted(df.columns)).sort(pk)


RPK = ["issuer_id", "metric_id", "period_end", "period_kind", "knowledge_date"]


def _existing_ratios() -> pl.DataFrame:
    return pl.DataFrame([
        _ratio("IA", "net_margin", date(2026, 2, 1), 0.10, OLD),            # re-produced with a new value
        _ratio("IA", "net_margin", date(2026, 3, 1), 0.11, OLD),            # vintage no longer produced -> removed
        _ratio("IA", "roe", date(2026, 2, 1), 0.20, OLD, kind="FY"),        # FY partition, not re-produced -> removed
        _ratio("IB", "net_margin", date(2026, 2, 1), 0.30, OLD),            # other issuer: untouched
        _ratio("IC", "roe", date(2026, 2, 1), 0.40, OLD, kind="FY"),        # other issuer: untouched
    ])


def _new_ratios() -> pl.DataFrame:
    return pl.DataFrame([_ratio("IA", "net_margin", date(2026, 2, 1), 0.12, NEW),
                         _ratio("IA", "net_margin", date(2026, 4, 1), 0.13, NEW)])


def test_commit_replace_equals_upsert_plus_drop_stale(tmp_data, tmp_path, monkeypatch):
    from app.analytics.ratios import drop_stale_rows

    # previous path, in a second data dir
    monkeypatch.setenv("BIG360_DATA_DIR", str(tmp_path / "old"))
    from app.core import config as config_mod
    from app.core import schema as schema_mod

    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    s_old = config_mod.get_settings()
    s_old.ensure_dirs()
    write_table(_existing_ratios(), "ratios", settings=s_old)
    write_table(_new_ratios(), "ratios", settings=s_old)
    drop_stale_rows("ratios", ["IA"], NEW, s_old)
    expected = read_table("ratios", settings=s_old)

    write_table(_existing_ratios(), "ratios", settings=tmp_data)
    with StagedWrite("ratios", tmp_data) as sw:
        sw.add(_new_ratios())
        res = sw.commit_replace("issuer_id", ["IA"])
    got = read_table("ratios", settings=tmp_data)
    assert res == {"rows": 2, "replaced": 3, "partitions": 2}
    assert _norm(got, RPK).equals(_norm(expected, RPK))
    assert got.filter(pl.col("issuer_id") == "IA")["value"].sort().to_list() == [0.12, 0.13]
    assert not list(table_dir("ratios", tmp_data).parent.glob(".tmp-*"))


def test_commit_replace_moves_issuer_out_of_old_sector_partition(tmp_data):
    def rows(iid: str, sector: str, run: str) -> pl.DataFrame:
        df = pl.DataFrame({"issuer_id": [iid, iid], "kpi_id": ["k1", "k2"], "period_end": [date(2025, 12, 31)] * 2,
                           "period_kind": ["FY", "FY"], "knowledge_date": [date(2026, 2, 1)] * 2,
                           "sector_id": [sector] * 2, "subsector_id": ["x"] * 2, "kpi_set_id": ["s"] * 2,
                           "fiscal_year": [2025] * 2, "fiscal_period": ["FY"] * 2, "unit": ["ratio"] * 2,
                           "inputs_complete": [True] * 2, "computed_at": [datetime.now(UTC)] * 2, "run_id": [run] * 2})
        return conform(df, "sector_kpis", strict=False)

    write_table(pl.concat([rows("A", "HLTH", "R1"), rows("B", "HLTH", "R1"), rows("C", "ENRG", "R1")]), "sector_kpis",
                settings=tmp_data)
    enrg = table_dir("sector_kpis", tmp_data) / "sector_id=ENRG"
    before = sorted(p.name for p in enrg.glob("*.parquet"))
    with StagedWrite("sector_kpis", tmp_data) as sw:
        sw.add(rows("A", "INDU", "R2"))
        res = sw.commit_replace("issuer_id", ["A"])
    out = read_table("sector_kpis", settings=tmp_data)
    assert res["replaced"] == 2 and res["partitions"] == 2          # HLTH rewritten, INDU created; ENRG untouched
    assert out.filter(pl.col("issuer_id") == "A")["sector_id"].unique().to_list() == ["INDU"]
    assert out.filter(pl.col("issuer_id") == "B").height == 2 and out.filter(pl.col("issuer_id") == "C").height == 2
    assert sorted(p.name for p in enrg.glob("*.parquet")) == before


def test_commit_replace_drops_issuers_that_produce_no_rows(tmp_data):
    write_table(_existing_ratios(), "ratios", settings=tmp_data)
    with StagedWrite("ratios", tmp_data) as sw:
        res = sw.commit_replace("issuer_id", ["IA", "IB"])
    out = read_table("ratios", settings=tmp_data)
    assert res == {"rows": 0, "replaced": 4, "partitions": 2}
    assert out["issuer_id"].to_list() == ["IC"]


def test_staged_chunks_validate_like_write_table(tmp_data):
    dup = pl.concat([_new_ratios(), _new_ratios()])
    with StagedWrite("ratios", tmp_data) as sw, pytest.raises(Exception, match="primary key|duplicate"):
        sw.add(dup)
    assert not list(table_dir("ratios", tmp_data).parent.glob(".tmp-*"))


def _levels(as_of: date, sid: str, n: int = 3) -> pl.DataFrame:
    return pl.DataFrame({
        "as_of_date": [as_of] * n, "security_id": [sid] * n, "level_type": ["prev_high"] * n, "timeframe": ["D"] * n,
        "level_key": ["prev_high@D"] * n, "price": [100.0 + i for i in range(n)],
        "anchor_date": [date(2026, 1, 1 + i) for i in range(n)], "distance_atr": [1.0] * n, "distance_pct": [0.01] * n,
        "side": ["resistance"] * n, "computed_at": [OLD] * n})


def test_commit_overwrite_streams_month_end_retention(tmp_data):
    from app.technicals.levels import month_end_dates, month_end_retention

    old = pl.concat([_levels(date(2026, 9, 15), "S1"), _levels(date(2026, 9, 30), "S1"), _levels(date(2026, 10, 15), "S1"),
                     _levels(date(2026, 10, 30), "S1"), _levels(date(2026, 10, 30), "S2"), _levels(date(2026, 11, 4), "S1")])
    write_table(old, "tech_levels", mode="overwrite", settings=tmp_data)
    as_of = date(2026, 11, 5)
    dates = distinct_values("tech_levels", "as_of_date", tmp_data)
    keep = month_end_dates(dates, as_of)
    assert keep == [date(2026, 9, 30), date(2026, 10, 30)]
    expected_keep = month_end_retention(read_table("tech_levels", settings=tmp_data), as_of)
    new = pl.concat([_levels(as_of, "S1"), _levels(as_of, "S3")])
    with StagedWrite("tech_levels", tmp_data) as sw:
        sw.add(new.head(3))
        sw.add(new.tail(3))                                          # one file per batch
        rows = sw.commit_overwrite(retain=pl.col("as_of_date").is_in(keep))
    out = read_table("tech_levels", settings=tmp_data)
    pk = ["as_of_date", "security_id", "level_key", "anchor_date"]
    assert rows == out.height == expected_keep.height + new.height == 15
    assert _norm(out.filter(pl.col("as_of_date") < date(2026, 11, 1)), pk).equals(_norm(expected_keep, pk))
    assert sorted(out["as_of_date"].unique().to_list()) == [date(2026, 9, 30), date(2026, 10, 30), as_of]


def test_failure_leaves_table_untouched_and_stage_removed(tmp_data):
    write_table(_levels(date(2026, 10, 30), "S1"), "tech_levels", mode="overwrite", settings=tmp_data)
    before = read_table("tech_levels", settings=tmp_data)
    with pytest.raises(RuntimeError), StagedWrite("tech_levels", tmp_data) as sw:
        sw.add(_levels(date(2026, 11, 5), "S1"))
        raise RuntimeError("job died")
    assert read_table("tech_levels", settings=tmp_data).equals(before)
    assert not list(table_dir("tech_levels", tmp_data).parent.glob(".tmp-*"))


def test_stale_stage_dirs_are_swept(tmp_data):
    root = table_dir("tech_levels", tmp_data)
    root.parent.mkdir(parents=True, exist_ok=True)
    stale = root.parent / ".tmp-tech_levels-stage-deadbeef"
    stale.mkdir()
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))
    fresh = root.parent / ".tmp-tech_levels-stage-cafe"
    fresh.mkdir()
    StagedWrite("tech_levels", tmp_data).discard()
    assert not stale.exists() and fresh.exists()


def test_layout_tables_are_rejected(tmp_data):
    with pytest.raises(ValueError, match="narrowed"):
        StagedWrite("prices_daily", tmp_data)


# ---------------------------------------------------------------- review 2026-11-07: stage integrity, contract shape


def _age(p, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_lost_stage_fails_commit_and_leaves_table_untouched(tmp_data):
    """Reviewer repro: the worker-start leftover sweep removes a partitioned stage whose root mtime went stale; the
    commit must fail (job retried) instead of committing 'replace IA by nothing' as a success."""
    from app.core.lake_stream import StageIntegrityError
    from app.core.storage import cleanup_leftovers

    write_table(_existing_ratios(), "ratios", settings=tmp_data)
    before = read_table("ratios", settings=tmp_data)
    with StagedWrite("ratios", tmp_data) as sw:
        sw.add(_new_ratios())
        _age(sw.stage, 2 * 3600)
        assert cleanup_leftovers(tmp_data) >= 1 and not sw.stage.exists()
        with pytest.raises(StageIntegrityError, match="gone"):
            sw.commit_replace("issuer_id", ["IA"])
    assert _norm(read_table("ratios", settings=tmp_data), RPK).equals(_norm(before, RPK))

    # overwrite path: a stage that lost a file is never swapped in (later add() calls would re-create the dir)
    write_table(_levels(date(2026, 10, 30), "S1"), "tech_levels", mode="overwrite", settings=tmp_data)
    lv_before = read_table("tech_levels", settings=tmp_data)
    with StagedWrite("tech_levels", tmp_data) as sw:
        sw.add(_levels(date(2026, 11, 5), "S1"))
        sw.add(_levels(date(2026, 11, 5), "S2"))
        next(sw.stage.glob("*.parquet")).unlink()
        with pytest.raises(StageIntegrityError, match="holds 3 rows in 1 files"):
            sw.commit_overwrite(retain=pl.col("as_of_date").is_in([date(2026, 10, 30)]))
    assert read_table("tech_levels", settings=tmp_data).equals(lv_before)
    assert not list(table_dir("tech_levels", tmp_data).parent.glob(".tmp-*"))


def test_add_keeps_partitioned_stage_fresh_for_leftover_sweeps(tmp_data):
    from app.core.storage import cleanup_leftovers

    with StagedWrite("ratios", tmp_data) as sw:
        sw.add(_new_ratios().head(1))
        _age(sw.stage, 2 * 3600)                     # NTFS: writes into an existing partition dir keep the root mtime
        sw.add(_new_ratios().tail(1))                # -> add() touches the root
        assert time.time() - sw.stage.stat().st_mtime < 60
        assert cleanup_leftovers(tmp_data) == 0 and sw.stage.exists()
        assert sw.commit_replace("issuer_id", ["IA"])["rows"] == 2
    assert read_table("ratios", settings=tmp_data).height == 2


def _file_shape(c, pcols=()) -> list[tuple[str, pl.DataType]]:
    return [(col.name, col.polars) for col in c.columns if col.name not in pcols]


def test_commit_overwrite_projects_retained_rows_onto_current_contract(tmp_data):
    """Retained month-end rows written before an additive column existed (and with a since-widened dtype) are
    re-written in the current contract shape: the unpartitioned table stays one schema and scan_table works."""
    from app.core.schema import get_contract
    from app.core.storage import scan_table

    c = get_contract("tech_levels")
    root = table_dir("tech_levels", tmp_data)
    root.mkdir(parents=True, exist_ok=True)
    old = conform(_levels(date(2026, 10, 30), "S1").with_columns(pl.lit(3).alias("swing_order")), c)
    old.drop("weight").with_columns(pl.col("swing_order").cast(pl.Int64)).write_parquet(root / "part-old.parquet")
    with StagedWrite("tech_levels", tmp_data) as sw:
        sw.add(_levels(date(2026, 11, 5), "S1"))
        rows = sw.commit_overwrite(retain=pl.col("as_of_date").is_in([date(2026, 10, 30)]))
    files = sorted(root.glob("*.parquet"))
    assert len(files) == 2 and rows == 6
    for f in files:
        assert list(pl.read_parquet_schema(f).items()) == _file_shape(c)
    out = scan_table("tech_levels", tmp_data).collect()
    kept = out.filter(pl.col("as_of_date") == date(2026, 10, 30)).sort("price")
    assert kept["weight"].null_count() == 3 and kept["swing_order"].to_list() == [3, 3, 3]
    assert kept.drop("weight").equals(old.drop("weight").sort("price"))


def test_commit_replace_projects_other_entities_rows_onto_current_contract(tmp_data):
    from app.core.schema import get_contract

    c = get_contract("ratios")
    root = table_dir("ratios", tmp_data)
    ex = conform(_existing_ratios(), c)
    for kind, part in ex.partition_by("period_kind", as_dict=True).items():
        d = root / f"period_kind={kind[0]}"
        d.mkdir(parents=True)
        part.drop("period_kind", "dq_score", "run_id").write_parquet(d / "part-old.parquet")   # pre-dq_score file
    with StagedWrite("ratios", tmp_data) as sw:
        sw.add(_new_ratios())
        res = sw.commit_replace("issuer_id", ["IA"])
    assert res == {"rows": 2, "replaced": 3, "partitions": 2}
    for f in root.rglob("*.parquet"):
        assert list(pl.read_parquet_schema(f).items()) == _file_shape(c, c.partition_by)
    out = read_table("ratios", settings=tmp_data)
    other = out.filter(pl.col("issuer_id") != "IA")
    assert other["dq_score"].null_count() == 2 and other["run_id"].null_count() == 2
    assert _norm(other.drop("dq_score", "run_id"), RPK).equals(
        _norm(ex.filter(pl.col("issuer_id") != "IA").drop("dq_score", "run_id"), RPK))
    assert out.filter(pl.col("issuer_id") == "IA")["run_id"].to_list() == ["r", "r"]


def test_stored_rows_missing_a_required_column_fail_the_commit(tmp_data):
    from app.core.schema import ContractViolation, get_contract

    root = table_dir("tech_levels", tmp_data)
    root.mkdir(parents=True, exist_ok=True)
    conform(_levels(date(2026, 10, 30), "S1"), get_contract("tech_levels")).drop("side").write_parquet(root / "p.parquet")
    with StagedWrite("tech_levels", tmp_data) as sw:
        sw.add(_levels(date(2026, 11, 5), "S1"))
        with pytest.raises(ContractViolation, match="side"):
            sw.commit_overwrite(retain=pl.col("as_of_date").is_in([date(2026, 10, 30)]))
    assert [p.name for p in root.glob("*.parquet")] == ["p.parquet"]
