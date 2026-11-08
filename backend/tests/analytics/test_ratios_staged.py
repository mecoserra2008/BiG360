"""ratios_kpis write path (coverage v3 A1, OOM at the 6 GB cap since 2026-11-03): results are staged to Parquet and
committed once with a streaming merge. The job's output must not depend on the memory knobs (chunk / workers /
flush_rows, the oom_params retry) and an incremental run must leave exactly what upsert + drop_stale_rows +
drop_stale_sector_rows left: recomputed issuers' rows replaced, everyone else's untouched."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.analytics import ratios as R
from app.analytics import sector_kpis as SK
from app.core.storage import read_table, table_dir, write_table
from app.scheduler.base import JobContext

ISSUERS = ["IA", "IB", "IC"]
T_OLD = datetime(2026, 10, 1, tzinfo=UTC)
T_NEW = datetime(2026, 11, 1, tzinfo=UTC)


def _wide_rows(iid: str, scale: float, ts: datetime) -> list[dict]:
    rows = []
    for basis in ("LOCAL", "USD"):
        fx = 1.0 if basis == "LOCAL" else 0.9
        for kind in ("TTM", "FY"):
            ends = ([date(2022 + q // 4, 3 * (q % 4) + 1, 1) - timedelta(days=1) for q in range(1, 13)] if kind == "TTM"
                    else [date(y, 12, 31) for y in range(2020, 2025)])
            for k, pe in enumerate(ends):
                g = (1.03 ** k) * scale * fx
                rev = 1000.0 * g
                base = {"issuer_id": iid, "currency_basis": basis, "period_kind": kind, "fiscal_year": pe.year,
                        "fiscal_period": "TTM" if kind == "TTM" else "FY", "period_end": pe, "currency": "USD",
                        "gaap": "us-gaap", "knowledge_date_estimated": False, "coverage": 1.0, "dq_score": 1.0,
                        "computed_at": ts, "revenue": rev, "cost_of_revenue": 0.55 * rev, "gross_profit": 0.45 * rev,
                        "sga": 0.15 * rev, "ebit": 0.2 * rev, "net_income": 0.12 * rev, "cfo": 0.18 * rev,
                        "capex": 0.04 * rev, "fcf": 0.14 * rev, "sbc": 0.02 * rev, "deferred_revenue": 0.1 * rev,
                        "total_assets": 2.0 * rev, "total_current_assets": 0.8 * rev,
                        "total_current_liabilities": 0.4 * rev, "total_liabilities": rev, "total_equity": rev,
                        "retained_earnings": 0.6 * rev, "long_term_debt": 0.3 * rev, "invested_capital": 1.3 * rev,
                        "shares_outstanding": 100.0}
                rows.append(dict(base, knowledge_date=pe + timedelta(days=40)))
                if k % 4 == 1:  # a restatement filed a year later (new vintage of an old period)
                    rows.append(dict(base, knowledge_date=pe + timedelta(days=400), revenue=rev * 1.01))
    return rows


@pytest.fixture()
def seeded(tmp_data, monkeypatch):
    now = datetime.now(UTC)
    iss = pl.DataFrame({"issuer_id": ISSUERS, "name": ["A", "B", "C"], "country": ["US"] * 3, "region": ["US"] * 3,
                        "is_active": [True] * 3, "updated_at": [now] * 3, "sector_id": ["TECH"] * 3,
                        "subsector_id": ["software_application"] * 3, "reporting_currency": ["USD"] * 3})
    write_table(iss, "issuers", mode="overwrite", settings=tmp_data)
    rows = (_wide_rows("IA", 1.0, T_NEW) + _wide_rows("IB", 2.0, T_OLD) + _wide_rows("IC", 3.0, T_OLD))
    write_table(pl.DataFrame(rows, infer_schema_length=None), "fundamentals_wide", settings=tmp_data)
    monkeypatch.setattr(R, "_scope", lambda ctx, s: list(ISSUERS))
    return tmp_data


def _run(s, params: dict | None = None, watermark: str | None = None, run_id: str = "R1"):
    return R.run(JobContext(job_id="ratios_kpis", run_id=run_id, as_of=date(2026, 11, 5), params=dict(params or {}),
                            watermark=watermark, settings=s))


def _table(name: str, s) -> pl.DataFrame:
    df = read_table(name, settings=s).drop("computed_at", "run_id")
    key = "metric_id" if name == "ratios" else "kpi_id"
    return df.select(sorted(df.columns)).sort("issuer_id", key, "period_kind", "period_end", "knowledge_date")


def _wipe(s) -> None:
    from app.core.storage import _rmtree

    for t in ("ratios", "sector_kpis"):
        _rmtree(table_dir(t, s))


def test_output_does_not_depend_on_memory_knobs(seeded):
    r = _run(seeded)
    assert r.status == "success" and r.rows_written["ratios"] > 0 and r.rows_written["sector_kpis"] > 0
    base_r, base_k = _table("ratios", seeded), _table("sector_kpis", seeded)
    _wipe(seeded)
    # the jobs.yaml oom_params retry ({flush_rows: 300000, workers: 1, chunk: 4}) taken to the extreme: one issuer
    # per task, a staged file per task, one issuer per market-cap load
    r2 = _run(seeded, {"flush_rows": 1, "workers": 1, "chunk": 1, "mcap_chunk": 1}, run_id="R2")
    assert r2.status == "success" and r2.rows_written == r.rows_written
    assert _table("ratios", seeded).equals(base_r)
    assert _table("sector_kpis", seeded).equals(base_k)
    # rebuild (overwrite) gives the same table again
    r3 = _run(seeded, {"rebuild": True, "flush_rows": 10}, run_id="R3")
    assert r3.status == "success" and _table("ratios", seeded).equals(base_r)
    assert not list(table_dir("ratios", seeded).parent.glob(".tmp-*"))


def test_rows_equal_the_in_memory_computation(seeded):
    _run(seeded)
    meta = R.issuer_meta(ISSUERS, seeded)
    exp_r, exp_k = [], []
    for kind in R.KINDS:
        pan = R.build_frames(ISSUERS, kind, meta, seeded, mcap=pl.DataFrame(
            schema={"issuer_id": pl.Utf8, "vintage": pl.Date, "mcap": pl.Float64}))
        exp_r.append(R.ratio_rows(pan, seeded))
        exp_k.append(SK.kpi_rows(pan, seeded))
    got = _table("ratios", seeded)
    exp = pl.concat(exp_r, how="diagonal_relaxed")
    assert got.height == exp.height
    j = got.join(exp.select("issuer_id", "metric_id", "period_kind", "period_end", "knowledge_date",
                            pl.col("value").alias("exp")),
                 on=["issuer_id", "metric_id", "period_kind", "period_end", "knowledge_date"], how="left")
    assert j.filter(pl.col("value").is_null() != pl.col("exp").is_null()).is_empty()
    assert j.filter((pl.col("value") - pl.col("exp")).abs() > 1e-12).is_empty()
    assert _table("sector_kpis", seeded).height == pl.concat(exp_k, how="diagonal_relaxed").height


def test_incremental_run_replaces_only_recomputed_issuers(seeded):
    _run(seeded)
    full_r = _table("ratios", seeded)
    # rows the current inputs no longer produce: an extra IA vintage (must go) and an out-of-scope issuer (stays)
    extra = (read_table("ratios", settings=seeded).filter(pl.col("issuer_id") == "IA").head(1)
             .with_columns(pl.lit(date(2030, 1, 1)).alias("knowledge_date"), pl.lit(T_OLD).alias("computed_at")))
    other = extra.with_columns(pl.lit("IZ").alias("issuer_id"))
    write_table(pl.concat([extra, other]), "ratios", settings=seeded)
    ib_before = read_table("ratios", settings=seeded).filter(pl.col("issuer_id") == "IB")
    # watermark between IB/IC's and IA's fundamentals computed_at: only IA is recomputed
    r = _run(seeded, {"flush_rows": 50}, watermark=(T_OLD + timedelta(days=1)).isoformat(), run_id="R9")
    assert r.status == "success" and r.entities_ok == 1
    out = read_table("ratios", settings=seeded)
    assert out.filter(pl.col("issuer_id") == "IA")["run_id"].unique().to_list() == ["R9"]
    assert out.filter(pl.col("knowledge_date") == date(2030, 1, 1))["issuer_id"].to_list() == ["IZ"]
    assert out.filter(pl.col("issuer_id") == "IB").sort(ib_before.columns[:5]).equals(ib_before.sort(ib_before.columns[:5]))
    assert _table("ratios", seeded).filter(pl.col("issuer_id") != "IZ").equals(full_r)


def test_failed_run_leaves_tables_untouched(seeded, monkeypatch):
    _run(seeded)
    before = _table("ratios", seeded)
    calls = {"n": 0}
    real = SK.kpi_rows

    def boom(pan, s=None):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("worker died")
        return real(pan, s)

    monkeypatch.setattr(SK, "kpi_rows", boom)
    with pytest.raises(RuntimeError):
        _run(seeded, {"flush_rows": 1, "workers": 1, "chunk": 1, "rebuild": True}, run_id="RX")
    assert _table("ratios", seeded).equals(before)
    assert not list(table_dir("ratios", seeded).parent.glob(".tmp-*"))
