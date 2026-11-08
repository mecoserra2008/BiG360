"""app.ml.full_scope: anchored feature transform (= cs_transform with the row inserted alone into the MVP cross-
section), anchored rank_pct, segment gate, liquidity buckets."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.ml import full_scope as FS
from app.ml import train as T
from app.ml.features import live_feature_ids, load_registry


def _panel(n: int, d: date, seed: int, prefix: str) -> pl.DataFrame:
    reg = load_registry()
    rng = np.random.default_rng(seed)
    cols = {f"f_{f}": rng.normal(size=n) for f in live_feature_ids(reg)}
    return pl.DataFrame({"date": [d] * n, "security_id": [f"{prefix}{i}" for i in range(n)], **cols})


def test_anchored_transform_is_leave_one_in_cs_transform():
    reg = load_registry()
    d = date(2026, 10, 29)
    ref = _panel(40, d, 1, "M")
    ext = _panel(3, d, 2, "X")
    got = FS.anchored_cs_transform(ext, ref, reg)
    for i in range(3):
        row = ext[i]
        both = T.cs_transform(pl.concat([ref, row]), reg).filter(pl.col("security_id") == row["security_id"][0])
        for fid in live_feature_ids(reg):
            c = f"f_{fid}"
            a, b = got[c][i], both[c][0]
            assert (a is None) == (b is None)
            if a is not None:
                assert a == pytest.approx(b, abs=1e-5)


def test_anchored_transform_needs_reference():
    reg = load_registry()
    ref = _panel(3, date(2026, 10, 29), 1, "M")    # < MIN_REF reference values -> null, not a fake percentile
    ext = _panel(1, date(2026, 10, 29), 2, "X")
    fid = next(f for f in live_feature_ids(reg) if reg["by_id"][f]["transform"] == "cs_rank_gauss")
    assert FS.anchored_cs_transform(ext, ref, reg)[f"f_{fid}"][0] is None


def test_anchored_rank_pct_matches_rank_rule():
    ref = np.array([0.1, 0.2, 0.3, 0.4])
    rp = FS.anchored_rank_pct(np.array([0.25, 0.5, 0.0]), ref)
    # rank / n with the value inserted (n = 5): 0.25 -> rank 3 -> 0.6; 0.5 -> 1.0; 0.0 -> 0.2
    assert rp == pytest.approx([0.6, 1.0, 0.2])


def test_segment_gate_and_buckets():
    ev = {"segments": {"3M": {"NA|large": {"served": True, "rank_ic": 0.031, "t": 2.4, "n_dates": 300},
                              "ASIA_EX_JP|micro": {"served": False, "rank_ic": -0.004, "t": -0.3, "n_dates": 200}}}}
    assert FS.segment_gate(ev, "3M", "NA", "large")[0]
    ok, det = FS.segment_gate(ev, "3M", "ASIA_EX_JP", "micro")
    assert not ok and "-0.004" in det
    assert not FS.segment_gate(ev, "1M", "NA", "large")[0]
    df = pl.DataFrame({"adv_usd_20d": [1e8, 2e7, 5e6, 1e5, None]})
    assert df.select(FS.adv_bucket_expr())["literal"].to_list() == ["large", "mid", "small", "micro", None] or \
        df.select(FS.adv_bucket_expr().alias("b"))["b"].to_list() == ["large", "mid", "small", "micro", None]


def test_segment_gate_respects_failed_leakage_check():
    ev = {"extrapolation_leakage_passed": False,
          "segments": {"1M": {"NA|large": {"served": True, "rank_ic": 0.04, "t": 3.0, "n_dates": 500}}}}
    ok, det = FS.segment_gate(ev, "1M", "NA", "large")
    assert not ok and "leakage" in det
