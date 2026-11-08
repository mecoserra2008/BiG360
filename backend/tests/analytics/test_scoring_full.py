"""app.analytics.scoring_full: anchored (leave-one-in) percentiles, level choice, factor re-percentiles, grades and
ranks - an extended security gets exactly what the MVP pipeline would give it if it were added alone to the MVP
cross-section, and the MVP rows are never touched."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.analytics import scoring as S
from app.analytics import scoring_full as F
from app.core import availability as AV


def _peer_frame(n_sub: int, n_other: int, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_sub):
        v = float(rng.normal())
        rows.append({"security_id": f"S{i}", "subsector_id": "semis", "sector_id": "TECH", "peer_region": "NA",
                     "size_bucket": "large", "v": v, "raw_value": v})
    for j in range(n_other):
        v = float(rng.normal(1.0))
        rows.append({"security_id": f"T{j}", "subsector_id": "software", "sector_id": "TECH", "peer_region": "NA",
                     "size_bucket": "large", "v": v, "raw_value": v})
    return pl.DataFrame(rows).with_columns(pl.lit("m").alias("metric_id"))


@pytest.mark.parametrize("n_sub,n_other,size", [(10, 5, "large"), (3, 12, "large"), (10, 5, "small"), (0, 4, "large")])
def test_anchored_peer_percentile_is_leave_one_in(n_sub, n_other, size):
    ref = _peer_frame(n_sub, n_other)
    x = pl.DataFrame([{"security_id": "X", "subsector_id": "semis", "sector_id": "TECH", "peer_region": "NA",
                       "size_bucket": size, "v": 0.123, "raw_value": 0.123, "metric_id": "m"}])
    got = F.anchored_peer_percentiles(x, ref, S.LEVELS, 8).row(0, named=True)
    full = S.peer_percentiles(pl.concat([ref, x], how="diagonal_relaxed"), S.LEVELS, 8) \
        .filter(pl.col("security_id") == "X").row(0, named=True)
    assert got["peer_group_level"] == full["peer_group_level"]
    assert got["peer_group_key"] == full["peer_group_key"]
    assert got["peer_count"] == full["peer_count"]
    if full["peer_percentile"] is None:
        assert got["peer_percentile"] is None
    else:
        assert got["peer_percentile"] == pytest.approx(full["peer_percentile"])
    # the reference rows themselves are untouched by construction (only ext rows are returned)
    assert got["security_id"] == "X"


def test_anchored_percentile_ties_and_null_value():
    ref = pl.DataFrame({"metric_id": ["m"] * 9, "v": [1.0, 2.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
                        "raw_value": [1.0] * 9, "peer_region": ["NA"] * 9})
    x = pl.DataFrame({"metric_id": ["m", "m"], "v": [2.0, None], "raw_value": [2.0, None], "peer_region": ["NA", "NA"]})
    out = F.anchored_peer_percentiles(x, ref, S.MKT_LEVELS, 8)
    # insert 2.0 into [1,2,2,3..8]: average rank of the 3 twos = 3 -> (3 - 0.5) / 10 * 100 = 25
    assert out["peer_percentile"][0] == pytest.approx(25.0)
    assert out["peer_percentile"][1] is None and out["peer_group_level"][1] == "region_all"
    assert out["peer_count"].to_list() == [10, 9]


def test_anchored_factor_scores_match_repercentile_with_insertion():
    rng = np.random.default_rng(3)
    ref = pl.DataFrame({"security_id": [f"M{i}" for i in range(20)], "factor_id": ["value"] * 20,
                        "peer_region": ["NA"] * 12 + ["EUROPE"] * 8, "sector_id": ["TECH"] * 6 + ["FINS"] * 14,
                        "score_raw": rng.uniform(0, 100, 20), "coverage": [1.0] * 20})
    ext = pl.DataFrame({"security_id": ["X", "Y"], "issuer_id": ["IX", "IY"], "factor_id": ["value", "insider"],
                        "peer_region": ["NA", "NA"], "sector_id": ["TECH", "TECH"], "score_raw": [42.0, None],
                        "coverage": [0.8, 0.0]})
    out = F.anchored_factor_scores(ext, ref, S.get_settings()).sort("security_id")
    # X: TECH x NA has 6 reference names (+1 < 8) -> peer_region NA (12 + 1 >= 8)
    na = ref.filter(pl.col("peer_region") == "NA")["score_raw"].to_numpy()
    expect = ((na < 42.0).sum() + 0.5 * (na == 42.0).sum() + 0.5) / (len(na) + 1) * 100
    assert out["score"][0] == pytest.approx(expect)
    assert out["score"][1] == 50.0 and out["coverage"][1] == 0.0   # neutral_50 policy for a missing insider factor


def test_anchored_grades_quintile_of_reference():
    ref = pl.DataFrame({"profile_id": ["balanced"] * 9, "score": [90.0, 80, 70, 60, 50, 40, 30, 20, 10]})
    c = pl.DataFrame({"profile_id": ["balanced"] * 4, "score": [95.0, 65.0, 5.0, None]})
    out = F.anchored_grades(c, ref)
    # inserted into 9 ref scores (n = 10): 95 -> rank 1 -> A; 65 -> 3 above, rank 4 -> floor(3*5/10) = 1 -> B; 5 -> E
    assert out["grade"].to_list() == ["A", "B", "E", None]
    both = S.add_ranks(pl.concat([ref, c.head(2).tail(1)]).with_columns(pl.lit("T").alias("sector_id"), pl.lit("NA").alias("peer_region")))
    assert both.filter(pl.col("score") == 65.0)["grade"][0] == "B"   # same as the MVP rule with the row inserted


def test_missing_inputs_text():
    miss = ["growth:no_statements", "momentum:short_history", "quality:no_statements", "value:no_market_cap",
            "factors:4/5", "core:0/2"]
    txt = AV.describe_missing(miss)
    assert "has 4 incl. 0 core" in txt and "no financial statements loaded" in txt and "market capitalisation" in txt
    assert "momentum" in AV.describe_missing(",".join(miss), "momentum")
    r = AV.field_reason("composite", {"group": "score", "score_missing": ",".join(miss), "score_null": True})
    assert r.code == "insufficient_data" and "core" in r.message
    r = AV.field_reason("f.value", {"group": "factor", "score_missing": ",".join(miss)})
    assert r.code == "insufficient_data" and "value" in r.message
    assert AV.REASONS["insufficient_data"][0] == "no_source"


def test_ref_counts_global_group():
    ext = pl.DataFrame({"__rid": [0, 1], "v": [1.5, 9.0]}).with_columns(pl.col("__rid").cast(pl.UInt32))
    ref = pl.DataFrame({"v": [1.0, 2.0, 3.0]})
    out = F.ref_counts(ext, ref, [], "v").sort("__rid")
    assert out["n_ref"].to_list() == [3, 3]
    assert out["less"].to_list() == [1, 3] and out["le"].to_list() == [1, 3]


def test_incremental_selection():
    from datetime import date as _d

    D = _d(2026, 10, 29)
    old = pl.DataFrame({"security_id": ["A", "B", "C"], "fp": ["1", "2", "3"], "as_of_date": [D] * 3,
                        "ref_digest": ["r"] * 3})
    new = pl.DataFrame({"security_id": ["A", "B", "C", "D"], "fp": ["1", "9", "3", "4"]})
    assert F.select_changed(new, old, D, "r", True) == ("incremental", ["B", "D"])
    assert F.select_changed(new, old, D, "other-reference", True) == ("full", None)   # MVP reference moved
    assert F.select_changed(new, old, _d(2026, 10, 30), "r", True) == ("full", None)  # new as_of date
    assert F.select_changed(new, old, D, "r", False) == ("full", None)


def test_nosector_set_scores_market_factors_only():
    class _Inp:
        pass

    inp = _Inp()
    inp.meta = pl.DataFrame({"issuer_id": ["I1", "I2"], "kpi_set_id": ["industrials", None]})
    inp.members = S.factor_member_table()
    inp.scored = S.scored_metric_table()
    inp.mfac_set = S.metric_factor_frame()
    F.add_nosector_set(inp)
    assert inp.meta["kpi_set_id"].to_list() == ["industrials", F.NOSECTOR_SET]
    facs = set(inp.members.filter(pl.col("kpi_set_id") == F.NOSECTOR_SET)["factor_id"].to_list())
    assert facs and facs <= set(F.SECTOR_FREE_FACTORS) and "momentum" in facs
    assert "mom_12_1" in inp.scored.filter(pl.col("kpi_set_id") == F.NOSECTOR_SET)["metric_id"].to_list()
    assert "no_sector" in AV.CAUSE_TEXT
