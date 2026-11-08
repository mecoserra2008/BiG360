"""event_daily_agg duplicate primary keys (refresh-pipeline fixes 2026-10-26).

2026-10-25: 70,022 duplicate PKs for 2026-05-22..31 in one month file (m-202605.parquet), so dq_checks failed (snapshot
blocked) and event_changepoints died with "cannot reindex on an axis with duplicate labels". Cause: the day-file store
replaces a day in two steps (d- file + month-file purge / compaction + d- file unlink); a Windows reader holding a file
made the second step fail (PermissionError in the 2026-10-20 gdelt redo log) and the same day stayed in both files;
compact_month and read_range then concatenated both copies and recompute_z wrote them back. Now a d- file is
authoritative for its day everywhere, compactions de-duplicate on the primary key, failed renames / unlinks are retried
and heal_overlaps() repairs a month in place."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.core.schema import ContractViolation
from app.events_v2 import store
from app.events_v2.pipeline import recompute_z

T = "event_daily_agg"


def _agg(d: date, n: int = 5, ev: int = 10, z: float | None = None) -> pl.DataFrame:
    return pl.DataFrame({
        "date": [d] * n, "label_type": ["country"] * n, "label_id": list(range(n)), "theme": [-1] * n,
        "ev_n": [ev] * n, "ev_mentions": [ev] * n, "ev_articles": [ev] * n, "ev_conflict_n": [1] * n,
        "ev_goldstein": [1.0] * n, "ev_tone": [-1.0] * n, "art_n": [ev] * n, "art_tone": [-1.5] * n,
        "art_neg_share": [0.3] * n, "attention_z": [z] * n, "tone_z": [z] * n,
    }).cast({"label_id": pl.Int32, "theme": pl.Int8})


def _month_with_overlap(s, days: list[date], stale: list[date]) -> None:
    """Compacted month + d- files of `stale` days whose rows ALSO remain in the month file (interrupted write)."""
    for d in days:
        store.write_day(T, d, _agg(d), s)
    store.compact_month(T, days[0].strftime("%Y-%m"), None, s)
    for d in stale:  # the second step (month purge) "failed": write the d- file only
        f = store.day_file(T, d, s)
        store._write_atomic(store._prepare(T, _agg(d, ev=20)), f, store.DAY_LEVEL)


def _pk_dups(df: pl.DataFrame) -> int:
    return df.height - df.select("date", "label_type", "label_id", "theme").unique().height


def test_read_range_prefers_the_day_file(tmp_data):
    days = [date(2026, 5, 20) + timedelta(days=i) for i in range(5)]
    _month_with_overlap(tmp_data, days, days[2:])
    df = store.read_range(T, days[0], days[-1], tmp_data)
    assert _pk_dups(df) == 0 and df.height == 25
    # the d- file (newer content, ev_n 20) wins for its day; other days come from the month file
    by = dict(df.group_by("date").agg(pl.col("ev_n").max()).iter_rows())
    assert by[days[0]] == 10 and by[days[3]] == 20


def test_compaction_never_duplicates_and_heal_repairs(tmp_data):
    days = [date(2026, 5, 20) + timedelta(days=i) for i in range(5)]
    _month_with_overlap(tmp_data, days, days[2:])
    r = store.heal_overlaps(T, tmp_data)
    assert r["months_healed"] == 1 and r["rows_removed"] == 15
    m = pl.read_parquet(store.month_file(T, "2026-05", tmp_data))
    assert _pk_dups(m) == 0 and m.height == 25
    assert dict(m.group_by("date").agg(pl.col("ev_n").max()).iter_rows())[days[4]] == 20
    assert not list(store.month_dir(T, "2026-05", tmp_data).glob("d-*.parquet"))
    assert store.heal_overlaps(T, tmp_data)["months_healed"] == 0          # idempotent


def test_heal_removes_duplicates_inside_a_month_file(tmp_data):
    d = date(2026, 5, 22)
    md = store.month_file(T, "2026-05", tmp_data)
    dup = pl.concat([_agg(d, z=0.1), _agg(d, z=0.2)])                     # the 2026-05 file: 2 copies per key
    store._write_atomic(dup.drop("year_month", strict=False), md, store.MONTH_LEVEL)
    assert store.heal_overlaps(T, tmp_data)["rows_removed"] == 5
    m = pl.read_parquet(md)
    assert m.height == 5 and m.get_column("attention_z").unique().to_list() == [pytest.approx(0.2)]   # keep=last


def test_write_day_replaces_and_rejects_duplicate_keys(tmp_data):
    d = date(2026, 6, 3)
    store.write_day(T, d, _agg(d), tmp_data)
    store.write_day(T, d, _agg(d, ev=7), tmp_data)                        # upsert by date: replaced, not appended
    df = store.read_range(T, d, d, tmp_data)
    assert df.height == 5 and df.get_column("ev_n").unique().to_list() == [7]
    with pytest.raises(ContractViolation):
        store.write_day(T, d, pl.concat([_agg(d), _agg(d)]), tmp_data)


def test_write_day_into_compacted_month_keeps_one_copy(tmp_data):
    days = [date(2026, 4, 1) + timedelta(days=i) for i in range(3)]
    for d in days:
        store.write_day(T, d, _agg(d), tmp_data)
    store.compact_month(T, "2026-04", None, tmp_data)
    store.write_day(T, days[1], _agg(days[1], ev=99), tmp_data)          # d- file first, month purged second
    store.compact_month(T, "2026-04", None, tmp_data)
    m = pl.read_parquet(store.month_file(T, "2026-04", tmp_data))
    assert _pk_dups(m) == 0 and m.height == 15
    assert dict(m.group_by("date").agg(pl.col("ev_n").max()).iter_rows())[days[1]] == 99


def test_recompute_z_survives_an_overlapping_month(tmp_data):
    """The 2026-10-20 failure mode: recompute_z read both copies and wrote duplicates (ContractViolation)."""
    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(31)]
    _month_with_overlap(tmp_data, days, days[21:])
    n = recompute_z(days[0], days[-1], tmp_data)
    assert n == 31 * 5
    df = store.read_range(T, days[0], days[-1], tmp_data)
    assert _pk_dups(df) == 0 and df.height == 31 * 5


def test_events_agg_heals_before_recompute(tmp_data):
    from app.events_v2.agg import run_agg

    days = [date(2026, 5, 20) + timedelta(days=i) for i in range(5)]
    _month_with_overlap(tmp_data, days, days[1:3])
    rep = run_agg(tmp_data, days=3, compact=False)
    assert rep["healed"]["months_healed"] == 1
    m = pl.read_parquet(store.month_file(T, "2026-05", tmp_data))
    assert _pk_dups(m) == 0


def test_changepoints_tolerate_duplicate_days(tmp_data, monkeypatch):
    """event_changepoints: a stream with a repeated date (two label ids decoding to one label) must not crash."""
    import numpy as np

    from app.events_models import bocpd
    from app.events_models import common as C

    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    rng = np.random.default_rng(0)
    vals = list(rng.poisson(50, 120).astype(float))
    g = pl.DataFrame({"date": days + days[-5:], "items": vals + vals[-5:], "tone": [-1.0] * 125})
    monkeypatch.setattr(C, "t1_range", lambda s=None: (days[0], days[-1]))
    monkeypatch.setattr(bocpd, "_streams", lambda *a, **k: [("country", "US", "all", g.to_pandas())])
    monkeypatch.setattr(C, "write", lambda df, *a, **k: df.height)
    monkeypatch.setattr(C, "update_state", lambda *a, **k: None)
    rep = bocpd.run_changepoints(tmp_data)
    assert rep["streams"] == 1
