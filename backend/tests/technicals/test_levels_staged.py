"""tech_levels write path (coverage v3 A1, OOM at the 7 GB cap since 2026-11-03): batches are staged to Parquet and
the month-end snapshots of the current table are streamed into the new one. The output must equal the previous
in-memory path (per-security levels_for_security + month_end_retention of the old table) and must not depend on
`batch` / `workers` (the oom_params retry)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.core.storage import read_table, table_dir, write_table
from app.scheduler.base import JobContext

_spec = importlib.util.spec_from_file_location("_tt", Path(__file__).with_name("test_technicals.py"))
_tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tt)

PK = ["as_of_date", "security_id", "level_key", "anchor_date"]


def _norm(df: pl.DataFrame) -> pl.DataFrame:
    df = df.drop("computed_at", strict=False)
    return df.select(sorted(df.columns)).sort(PK)


def _seed(n_sec: int = 5):
    bars = [_tt.make_bars(900, seed=s, start="2023-01-02", sid=f"S{s:012d}") for s in range(n_sec)]
    _tt._write_prices(bars)
    return bars


def test_levels_equal_in_memory_path_and_month_end_retention(tmp_data):
    from app.technicals import levels as L
    from app.technicals.data import iter_bars, load_ath_prior, load_meta, load_price_panel, years_before

    bars = _seed()
    as_of = bars[0].dates[-1].astype("datetime64[D]").item()
    first = date(as_of.year, as_of.month, 1)
    d_mid = date(first.year - (first.month == 1), first.month - 1 or 12, 10)   # previous month, mid-month: dropped
    d_end = d_mid.replace(day=27)                                               # previous month's last snapshot: kept
    old = pl.concat([pl.DataFrame({
        "as_of_date": [d] * 2, "security_id": ["OLD1", "OLD2"], "level_type": ["prev_high"] * 2,
        "timeframe": ["D"] * 2, "level_key": ["prev_high@D"] * 2, "price": [10.0, 20.0], "anchor_date": [d, d],
        "distance_atr": [1.0, 2.0], "distance_pct": [0.1, 0.2], "side": ["resistance"] * 2,
        "computed_at": [datetime(2026, 1, 1, tzinfo=UTC)] * 2}) for d in (d_mid, d_end, first)])   # first: this month
    write_table(old, "tech_levels", mode="overwrite", settings=tmp_data)
    # reliability stats: own rows for two securities, a pool row, universe rows; an older snapshot that must be ignored
    keys = ["prev_high@D", "prev_low@D", "high_52w@NA", "sma_200@NA"]
    st_rows = [(as_of, bars[0].security_id, k, 0.9) for k in keys[:2]] + [(as_of, bars[3].security_id, keys[2], 0.8)] \
        + [(as_of, "*NA|US", k, 0.6) for k in keys[1:3]] + [(as_of, "*", k, 0.4) for k in keys] \
        + [(date(2020, 1, 3), bars[1].security_id, k, 0.1) for k in keys]
    write_table(pl.DataFrame({
        "as_of_date": [r[0] for r in st_rows], "security_id": [r[1] for r in st_rows],
        "level_key": [r[2] for r in st_rows], "score": [r[3] for r in st_rows], "n_events": [100] * len(st_rows),
        "window_start": [date(2019, 1, 1)] * len(st_rows), "computed_at": [datetime(2026, 1, 1, tzinfo=UTC)] * len(st_rows),
        "pool_key": [None if r[1].startswith("*") else "NA|US" for r in st_rows]}), "level_type_stats", mode="overwrite",
        settings=tmp_data)
    expected_keep = L.month_end_retention(read_table("tech_levels", settings=tmp_data), as_of)
    assert expected_keep["as_of_date"].unique().to_list() == [d_end]

    r = L.run(JobContext(job_id="tech_levels", run_id="R1", as_of=as_of, params={"workers": 1, "batch": 2},
                         settings=tmp_data))
    assert r.status == "success", r.message
    out = read_table("tech_levels", settings=tmp_data)
    new = out.filter(pl.col("as_of_date") == as_of)
    assert r.rows_written["tech_levels"] == out.height == new.height + expected_keep.height
    assert _norm(out.filter(pl.col("as_of_date") != as_of)).equals(_norm(expected_keep))

    # previous path: every security's levels computed in memory with the same inputs
    win = years_before(as_of, 3.2)
    sids = sorted(b.security_id for b in bars)
    meta = load_meta(sids)
    panel = load_price_panel(sids, start=win, end=as_of)
    stats = L._stats_lookup(as_of)
    frames = [L.levels_for_security(b, stats, as_of) for b in iter_bars(panel, meta, {}, load_ath_prior(sids, win), 60)]
    exp = pl.concat([f for f in frames if not f.is_empty()], how="vertical_relaxed")
    exp = exp.select([c for c in out.columns if c in exp.columns])
    assert _norm(new.select(exp.columns)).equals(_norm(exp))
    trs = new.filter(pl.col("security_id") == bars[0].security_id, pl.col("level_key") == "prev_high@D")
    assert trs.height and trs["type_rank_score"].unique().to_list() == [0.9]          # own row used
    assert new["type_rank_score"].drop_nulls().is_in([0.9, 0.8, 0.6, 0.4]).all()      # never the stale snapshot

    # the oom_params-style knobs do not change the snapshot; re-running in the same month keeps the same snapshots
    r2 = L.run(JobContext(job_id="tech_levels", run_id="R2", as_of=as_of, params={"workers": 2, "batch": 3000},
                          settings=tmp_data))
    assert r2.status == "success" and r2.rows_written == r.rows_written
    assert _norm(read_table("tech_levels", settings=tmp_data)).equals(_norm(out))
    assert not list(table_dir("tech_levels", tmp_data).parent.glob(".tmp-*"))
