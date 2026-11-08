"""O2.2a / audit F15: Beneish M-score components and the recalibrated (model-based) manipulation screen.

Regression fixture NVDA: TTM to 2026-07-26 from fundamentals_wide (values as stored on 2026-10-19), which the
textbook rule flagged CRITICAL (M = -0.39)."""

from __future__ import annotations

import polars as pl
import pytest

from app.analytics import beneish as B

# NVDA TTM 2026-07-26 vs TTM 2025-07-27 (USD): revenue, receivables, gross margin, current assets, PP&E, total
# assets, D&A, SG&A, net income (continuing), CFO, total liabilities
NVDA = {
    "cur": dict(revenue=3.0297e11, accounts_receivable=6.3059e10, gross_margin=0.746746, total_current_assets=1.9741e11,
                ppe_net=1.4285e10, total_assets=3.2027e11, depreciation_amortization=3.687e9, sga=5.07e9,
                net_income_continuing=1.9288e11, cfo=1.3436e11, total_liabilities=9.1288e10),
    "prev": dict(revenue=1.6522e11, accounts_receivable=2.7808e10, gross_margin=0.698471, total_current_assets=1.0222e11,
                 ppe_net=9.141e9, total_assets=1.4074e11, depreciation_amortization=2.301e9, sga=4.035e9,
                 net_income_continuing=8.6597e10, cfo=7.7035e10, total_liabilities=4.0609e10),
}


def _indices(cur: dict, prev: dict) -> dict[str, float]:
    def q(d, a, b):
        return d[a] / d[b]
    aq = lambda d: 1 - (d["total_current_assets"] + d["ppe_net"]) / d["total_assets"]  # noqa: E731
    dep = lambda d: d["depreciation_amortization"] / (d["depreciation_amortization"] + d["ppe_net"])  # noqa: E731
    return {"dsri": q(cur, "accounts_receivable", "revenue") / q(prev, "accounts_receivable", "revenue"),
            "gmi": prev["gross_margin"] / cur["gross_margin"], "aqi": aq(cur) / aq(prev),
            "sgi": cur["revenue"] / prev["revenue"], "depi": dep(prev) / dep(cur),
            "sgai": q(cur, "sga", "revenue") / q(prev, "sga", "revenue"),
            "tata": (cur["net_income_continuing"] - cur["cfo"]) / cur["total_assets"],
            "lvgi": q(cur, "total_liabilities", "total_assets") / q(prev, "total_liabilities", "total_assets")}


def test_nvda_textbook_score_matches_stored_value():
    comp = _indices(NVDA["cur"], NVDA["prev"])
    assert B.m_score(comp) == pytest.approx(-0.3899, abs=2e-3)  # serving m_beneish_m for NVDA = -0.38988
    assert comp["sgi"] == pytest.approx(1.834, abs=1e-3) and comp["tata"] == pytest.approx(0.1827, abs=1e-3)


#: NVDA outside-the-model signals in the 2026-10-19 snapshot: Piotroski 7, CFO / NI 0.70, Altman Z'' 12.2
NVDA_SIGNALS = {"piotroski_f": 7.0, "cfo_to_net_income": 0.6966, "net_margin": 0.637, "altman_z2": 12.17}


def test_nvda_is_growth_driven_not_flagged():
    comp = _indices(NVDA["cur"], NVDA["prev"])
    a = B.assess(comp, sector_id="TECH", signals=NVDA_SIGNALS)
    assert a["textbook_flag"] is True and a["flag"] is False and a["level"] == "watch"
    assert a["model_based"] is True and "model-based" in a["method"]
    assert a["red_flags"] == ["aqi", "tata"]              # non-current non-PP&E assets + accruals
    assert a["corroboration"] == []                       # strong cash conversion, F-score 7, safe Z''
    assert a["m_score_growth_adj"] == pytest.approx(-1.134, abs=2e-3)
    assert a["drivers"][:2] == ["tata", "sgi"]            # growth + accruals carry the score
    assert a["contributions"]["sgi"] == pytest.approx(0.892 * (comp["sgi"] - 1), abs=1e-3)


def test_corroborated_score_is_flagged():
    # receivables ballooning, margins down, asset quality worse, high accruals, modest growth
    comp = {"dsri": 1.8, "gmi": 1.25, "aqi": 1.3, "sgi": 1.15, "depi": 1.0, "sgai": 1.0, "tata": 0.09, "lvgi": 1.0}
    a = B.assess(comp, sector_id="INDU", signals={"piotroski_f": 3, "cfo_to_net_income": 0.3, "net_margin": 0.05})
    assert a["m_score"] > B.THRESHOLD and a["m_score_growth_adj"] > B.THRESHOLD
    assert a["flag"] is True and a["level"] == "flag" and set(a["red_flags"]) >= {"dsri", "gmi", "aqi", "tata"}
    assert a["corroboration"] == ["piotroski_low", "weak_cash_conversion"]
    # the same M-score without any outside warning sign stays a watch
    assert B.assess(comp, sector_id="INDU", signals={"piotroski_f": 8, "altman_z2": 5.0})["level"] == "watch"


def test_growth_neutral_score_removes_only_sgi():
    comp = {"dsri": 1.0, "gmi": 1.0, "aqi": 1.0, "sgi": 3.0, "depi": 1.0, "sgai": 1.0, "tata": 0.0, "lvgi": 1.0}
    m, mg = B.m_score(comp), B.m_score(comp, neutral_growth=True)
    assert m - mg == pytest.approx(0.892 * 2.0)
    a = B.assess(comp)
    assert a["textbook_flag"] is True and a["growth_driven"] is True and a["flag"] is False


def test_sector_exclusions_and_missing_inputs():
    comp = {"dsri": 2.0, "gmi": 1.3, "aqi": 1.4, "sgi": 1.1, "depi": 1.0, "sgai": 1.0, "tata": 0.1, "lvgi": 1.0}
    assert B.assess(comp, is_financial=True)["level"] == "not_applicable"
    assert B.assess(comp, sector_id="REAL")["level"] == "not_applicable"
    assert B.m_score({"sgi": 1.2}) is None                                    # > 2 indices missing
    assert B.m_score({"sgi": 1.2, "dsri": 1.0, "gmi": 1.0, "aqi": 1.0, "depi": 1.0, "sgai": 1.0}) is not None
    only_m = B.assess({}, m_reported=-1.0)                                     # stored M only: never escalated
    assert only_m["textbook_flag"] is True and only_m["flag"] is False and only_m["level"] == "watch"


def test_polars_columns_match_scalar_assess():
    from app.analytics.ratios import accounting_scores

    rows = []
    for vintage, d in (("2025-08-27", NVDA["prev"]), ("2026-08-27", NVDA["cur"])):
        rows.append({"issuer_id": "NVDA", "vintage": vintage, "period_end": vintage[:4] + "-07-26",
                     "is_financial": False, "sector_id": "TECH", "groups": ["semis"], **d,
                     "net_income": d["net_income_continuing"], "cfo_to_net_income": d["cfo"] / d["net_income_continuing"],
                     "net_margin": d["net_income_continuing"] / d["revenue"]})
    frame = pl.DataFrame(rows).with_columns(pl.col("period_end").str.to_date(), pl.col("vintage").str.to_date())
    # one panel (same vintage) holding both periods, as vintage_expand produces
    frame = frame.with_columns(pl.lit(frame["vintage"].max()).alias("vintage"))
    out = accounting_scores(frame).sort("period_end")
    last = out.row(-1, named=True)
    assert last["beneish_m"] == pytest.approx(-0.3899, abs=2e-3)
    assert last["beneish_sgi"] == pytest.approx(1.834, abs=1e-3)
    assert last["beneish_red_flags"] == 2.0 and last["beneish_flag_adj"] == 0.0  # aqi + tata, no corroboration
    assert last["beneish_m_growth_adj"] == pytest.approx(last["beneish_m"] - 0.892 * (last["beneish_sgi"] - 1), abs=1e-6)
