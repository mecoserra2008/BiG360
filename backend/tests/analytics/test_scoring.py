"""app.analytics.scoring: percentiles, winsorize, peer fallback chain, self-history, PIT selection, ranks/grades,
factor/composite missing-data policy; scoring_ml_blend.blend; valuation.latest_known."""

from __future__ import annotations

import math
from datetime import date

import polars as pl
import pytest

from app.analytics import scoring as S
from app.analytics.scoring_ml_blend import blend
from app.analytics.valuation import latest_known


def test_pct_rank_average_ties_and_nulls():
    df = pl.DataFrame({"g": ["a"] * 5, "v": [1.0, 2.0, 2.0, 4.0, None]})
    pct, n = S.pct_rank(pl.col("v"), ["g"])
    out = df.with_columns(pct.alias("p"), n.alias("n"))
    assert out["n"].to_list() == [4] * 5
    assert out["p"].to_list()[:4] == pytest.approx([12.5, 50.0, 50.0, 87.5])   # (rank - 0.5) / n * 100
    assert out["p"][4] is None


def test_winsorize_clips_tails():
    df = pl.DataFrame({"g": ["a"] * 101, "v": [float(i) for i in range(100)] + [1e9]})
    w = df.select(S.winsorize(pl.col("v"), ["g"]).alias("w"))["w"]
    assert w.max() < 1e9 and w.max() == pytest.approx(df["v"].quantile(0.99))


def test_norm_cdf_matches_erf():
    zs = [-3.0, -1.2, 0.0, 0.5, 2.0]
    got = pl.DataFrame({"z": zs}).select(S.norm_cdf(pl.col("z")).alias("p"))["p"].to_list()
    for z, p in zip(zs, got, strict=True):
        assert p == pytest.approx(0.5 * (1 + math.erf(z / math.sqrt(2))), abs=2e-7)


def test_direction_expr_band_and_lower_is_better():
    df = pl.DataFrame({"x": [0.85, 0.95, 2.0], "hib": [0, 0, -1], "t": [0.85, 0.85, None]},
                      schema={"x": pl.Float64, "hib": pl.Int8, "t": pl.Float64})
    v = df.select(S.direction_expr(pl.col("x"), pl.col("hib"), pl.col("t")).alias("v"))["v"].to_list()
    assert v == pytest.approx([0.0, -0.1, -2.0])


def _peer_frame(n_sub: int, n_sector_other: int) -> pl.DataFrame:
    rows = []
    for i in range(n_sub):
        rows.append({"security_id": f"S{i}", "subsector_id": "semis", "sector_id": "TECH", "peer_region": "NA",
                     "size_bucket": "large", "v": float(i), "raw_value": float(i)})
    for j in range(n_sector_other):
        rows.append({"security_id": f"T{j}", "subsector_id": "software", "sector_id": "TECH", "peer_region": "NA",
                     "size_bucket": "large", "v": float(100 + j), "raw_value": float(100 + j)})
    return pl.DataFrame(rows).with_columns(pl.lit("m").alias("metric_id"))


def test_peer_fallback_subsector_when_enough_peers():
    out = S.peer_percentiles(_peer_frame(8, 10), S.LEVELS, 8)
    s = out.filter(pl.col("subsector_id") == "semis")
    assert set(s["peer_group_level"].to_list()) == {"subsector_region_size"}
    assert s["peer_count"].to_list() == [8] * 8
    assert s["peer_group_key"][0] == "subsector_region_size:semis|NA|large"
    assert sorted(s["peer_percentile"].to_list()) == pytest.approx([(r - 0.5) / 8 * 100 for r in range(1, 9)])


def test_peer_fallback_to_sector_when_subsector_too_small():
    out = S.peer_percentiles(_peer_frame(3, 10), S.LEVELS, 8)
    s = out.filter(pl.col("subsector_id") == "semis")
    assert set(s["peer_group_level"].to_list()) == {"sector_region"}
    assert s["peer_count"].to_list() == [13] * 3
    # the 3 semis have the lowest values in the 13-name sector group
    assert sorted(s["peer_percentile"].to_list()) == pytest.approx([(r - 0.5) / 13 * 100 for r in (1, 2, 3)])
    assert s["peer_median"][0] == pytest.approx(103.0)


def test_peer_fallback_size_bucket_missing_skips_level():
    df = _peer_frame(9, 0).with_columns(pl.lit(None, pl.Utf8).alias("size_bucket"))
    out = S.peer_percentiles(df, S.LEVELS, 8)
    assert set(out["peer_group_level"].to_list()) == {"subsector_region"}


def test_self_scores_z_and_min_obs():
    catf = pl.DataFrame({"metric_id": ["m"], "hib": [1], "target": [None], "positive_only": [False]},
                        schema={"metric_id": pl.Utf8, "hib": pl.Int8, "target": pl.Float64, "positive_only": pl.Boolean})
    hist_vals = [0.10, 0.12, 0.08, 0.11, 0.09, 0.10, 0.12, 0.08, 0.10, 0.10]
    hist = pl.DataFrame({"security_id": ["A"] * 10, "metric_id": ["m"] * 10, "value": hist_vals,
                         "period_end": [date(2014 + i, 6, 30) for i in range(10)]})
    cur = pl.DataFrame({"security_id": ["A", "B"], "metric_id": ["m", "m"], "v": [0.13, 0.13]})
    out = S.self_scores(cur, hist, ["security_id"], catf, 8, 5, 3.0).sort("security_id")
    mu = sum(hist_vals) / 10
    sd = (sum((x - mu) ** 2 for x in hist_vals) / 9) ** 0.5
    assert out["self_z"][0] == pytest.approx((0.13 - mu) / sd)
    assert out["self_score"][0] == pytest.approx(100 * 0.5 * (1 + math.erf(out["self_z"][0] / math.sqrt(2))), abs=1e-4)
    assert out["self_z"][1] is None                                   # no history -> no self component
    short = S.self_scores(cur, hist.head(7), ["security_id"], catf, 8, 5, 3.0)
    assert short["self_z"].null_count() == 2                          # < 8 observations


def test_add_ranks_grades_quintiles():
    c = pl.DataFrame({"profile_id": ["balanced"] * 10, "sector_id": ["TECH"] * 5 + ["FINS"] * 5,
                      "peer_region": ["NA"] * 10, "score": [float(x) for x in range(10, 0, -1)]})
    out = S.add_ranks(c).sort("rank_global")
    assert out["grade"].to_list() == ["A", "A", "B", "B", "C", "C", "D", "D", "E", "E"]
    assert out["rank_sector"].to_list() == [1, 2, 3, 4, 5, 1, 2, 3, 4, 5]


def test_month_ends():
    assert S.month_ends(date(2024, 1, 15), date(2024, 4, 30)) == [date(2024, 1, 31), date(2024, 2, 29),
                                                                  date(2024, 3, 31), date(2024, 4, 30)]


class _FakeInputs:
    def __init__(self, fund: pl.DataFrame):
        from app.core.config import get_settings

        self.fund = fund
        self.s = get_settings()


def test_fundamental_values_no_look_ahead_and_ttm_preference():
    f = pl.DataFrame({
        "issuer_id": ["I"] * 5, "metric_id": ["roe"] * 5,
        "period_kind": ["TTM", "TTM", "TTM", "FY", "TTM"],
        "period_end": [date(2024, 3, 31), date(2024, 6, 30), date(2024, 6, 30), date(2023, 12, 31), date(2024, 9, 30)],
        "knowledge_date": [date(2024, 5, 1), date(2024, 8, 1), date(2024, 11, 1), date(2024, 2, 20), date(2024, 11, 5)],
        "value": [0.10, 0.11, 0.12, 0.09, 0.13], "inputs_complete": [True] * 5, "dq_score": [1.0] * 5})
    cur, hist = S.fundamental_values(_FakeInputs(f), date(2024, 10, 1))
    r = cur.row(0, named=True)
    assert r["raw_value"] == 0.11 and r["period_kind"] == "TTM"      # restatement (11-01) and Q3 (11-05) unseen
    assert hist["value"].to_list() == [0.10]
    cur2, _ = S.fundamental_values(_FakeInputs(f), date(2024, 11, 2))
    assert cur2["raw_value"][0] == 0.12                                # restated value once known


def test_fundamental_values_falls_back_to_fy_when_ttm_null():
    f = pl.DataFrame({
        "issuer_id": ["I"] * 2, "metric_id": ["revenue_growth_yoy"] * 2, "period_kind": ["TTM", "FY"],
        "period_end": [date(2024, 6, 30), date(2024, 3, 31)],
        "knowledge_date": [date(2024, 8, 10), date(2024, 6, 20)], "value": [None, 0.05],
        "inputs_complete": [True, True], "dq_score": [1.0, 1.0]}, schema_overrides={"value": pl.Float64})
    cur, _ = S.fundamental_values(_FakeInputs(f), date(2024, 9, 1))
    assert cur["raw_value"][0] == 0.05 and cur["period_kind"][0] == "FY"


def test_factor_members_respect_overrides_and_exclusions():
    fm = S.factor_member_table()
    banks_prof = fm.filter(pl.col("kpi_set_id") == "banks", pl.col("factor_id") == "profitability")
    assert set(banks_prof["metric_id"].to_list()) == {"rote", "roe", "ppop_to_assets"}
    banks_q = fm.filter(pl.col("kpi_set_id") == "banks", pl.col("factor_id") == "quality")
    assert "gross_margin" not in banks_q["metric_id"].to_list()
    assert "piotroski_f" not in banks_q["metric_id"].to_list()        # excluded_generic for banks
    sw = fm.filter(pl.col("kpi_set_id") == "software_internet", pl.col("factor_id") == "quality")
    assert sw.filter(pl.col("block") == "kpi")["weight"].sum() == pytest.approx(1.0)
    # rnd_to_revenue only where rnd_expected
    assert "rnd_to_revenue" not in fm.filter(pl.col("kpi_set_id") == "retail")["metric_id"].to_list()


def test_ml_blend_renormalizes_missing_ml():
    bal = pl.DataFrame({"as_of_date": [date(2024, 1, 31)] * 3, "security_id": ["A", "B", "C"],
                        "issuer_id": ["a", "b", "c"], "sector_id": ["TECH"] * 3, "peer_region": ["NA"] * 3,
                        "score": [60.0, 40.0, None], "coverage": [0.8, 0.8, 0.2], "dq_score": [1.0] * 3})
    preds = pl.DataFrame({"as_of_date": [date(2024, 1, 31)] * 2, "security_id": ["A", "C"], "ml": [100.0, 90.0]})
    out = blend(bal, preds, 0.5, 0.5).sort("security_id")
    assert out["score"].to_list() == [80.0, 40.0, None]
    assert out["coverage"].to_list() == pytest.approx([0.9, 0.4, 0.6])
    assert out["profile_id"].unique().to_list() == ["ml_blend"]


def test_latest_known_skips_late_history_rows():
    df = pl.DataFrame({"issuer_id": ["I"] * 3,
                       "period_end": [date(2024, 3, 31), date(2023, 12, 31), date(2024, 6, 30)],
                       "knowledge_date": [date(2024, 5, 1), date(2024, 6, 1), date(2024, 8, 1)], "x": [1, 2, 3]})
    out = latest_known(df)
    assert out["x"].to_list() == [1, 3]
