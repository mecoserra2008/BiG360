"""2026-10-31: ref_universe must never blank an issuer's sector (merge with the current snapshot) + the >1 % sector-loss
DQ check / alert. Before the fix every nightly ref_universe dropped the SIC-derived sectors of ~4,300 US issuers
(SEC submissions are only fetched for the top ~1,500 candidates) until identity_resolve restored them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from app.core.storage import read_table
from app.normalize import identity as I
from tests.normalize.test_identity import SEEDS, _build, _sources, _write_seeds


def _exxon(settings) -> dict:
    iid = I.lookup("cik", "34088")["issuer_id"]
    return read_table("issuers", settings=settings).filter(pl.col("issuer_id") == iid).row(0, named=True)


def _no_yf_for_exxon(src: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    src["yf_info"] = src["yf_info"].filter(pl.col("yf_ticker") != "XOMOLD")
    return src


def test_ref_universe_keeps_sic_sector_when_submissions_not_fetched(tmp_data):
    _write_seeds(tmp_data, SEEDS)
    # run 1: Exxon (no yfinance info) is classified from its SEC SIC code 2911
    _build(tmp_data, sources=_no_yf_for_exxon(_sources()))
    x1 = _exxon(tmp_data)
    assert x1["sic_code"] == "2911" and x1["classification_source"] == "sic" and x1["sector_id"]
    # run 2: Exxon is outside the submissions budget (no SIC in this build) - before the fix: sector_id = None
    src = _no_yf_for_exxon(_sources())
    src["submissions"] = src["submissions"].filter(pl.col("cik") != "0000034088")
    res = _build(tmp_data, sources=src, as_of=date(2026, 10, 10))
    x2 = _exxon(tmp_data)
    assert x2["sic_code"] == "2911", "the SIC code is carried over from the snapshot"
    assert (x2["sector_id"], x2["subsector_id"], x2["classification_source"]) == \
        (x1["sector_id"], x1["subsector_id"], "sic")
    loss = res["stats"]["sector_loss"]
    assert loss["passed"] and loss["lost"] == 0 and loss["carried_inputs"] >= 1
    assert not any("sector loss" in w for w in res["warnings"])


def test_ref_universe_better_source_wins(tmp_data):
    _write_seeds(tmp_data, SEEDS)
    _build(tmp_data, sources=_no_yf_for_exxon(_sources()))
    assert _exxon(tmp_data)["classification_source"] == "sic"
    # run 2 has a yfinance industry for Exxon: the higher-precedence source replaces the SIC classification
    src = _sources()
    src["submissions"] = src["submissions"].filter(pl.col("cik") != "0000034088")
    _build(tmp_data, sources=src, as_of=date(2026, 10, 10))
    x = _exxon(tmp_data)
    assert x["classification_source"] == "yf_industry" and x["yf_industry"] == "Oil & Gas Integrated"


def test_keep_previous_classification_unknown_only():
    prev = pl.DataFrame({"issuer_id": ["a", "b", "c"], "sector_id": ["energy", "tech", "fin"],
                         "subsector_id": ["oil", "software", "banks"], "classification_source": ["sic", "sic", "sic"]})
    new = pl.DataFrame({"issuer_id": ["a", "b", "c", "d"], "sector_id": [None, "health", None, None],
                        "subsector_id": [None, "pharma", None, None],
                        "classification_source": ["unknown", "yf_industry", "sic", "unknown"]})
    out, n = I.keep_previous_classification(new, prev)
    got = {r["issuer_id"]: (r["sector_id"], r["classification_source"]) for r in out.iter_rows(named=True)}
    assert n == 1
    assert got["a"] == ("energy", "sic")           # blanked by a partial source -> previous kept
    assert got["b"] == ("health", "yf_industry")   # real new classification wins
    assert got["c"] == (None, "sic")               # deliberate exclusion (e.g. SIC 6770) wins
    assert got["d"] == (None, "unknown")           # never classified


def test_sector_loss_check_threshold():
    prev = pl.DataFrame({"issuer_id": [f"i{k}" for k in range(200)], "sector_id": ["s"] * 200,
                         "is_active": [True] * 200, "country": ["US"] * 200})
    ok = prev.with_columns(pl.when(pl.col("issuer_id") == "i0").then(None).otherwise(pl.col("sector_id")).alias("sector_id"))
    r = I.sector_loss(prev, ok)
    assert r["passed"] and r["lost"] == 1 and r["rate"] == 0.005
    bad = prev.with_columns(pl.when(pl.int_range(pl.len()) < 5).then(None).otherwise(pl.col("sector_id")).alias("sector_id"))
    r = I.sector_loss(prev, bad)
    assert not r["passed"] and r["lost"] == 5 and r["by_country"] == {"US": 5}
    assert I.sector_loss(None, bad)["passed"]


def test_sector_loss_dq_hook_and_alert(tmp_data):
    from app.core import dq
    from app.ops.health import sector_loss_alert

    I.record_sector_loss({"passed": False, "lost": 1900, "prev_classified": 20000, "rate": 0.095, "threshold": 0.01,
                          "examples": ["x"], "by_country": {"US": 1900}}, "R1", tmp_data)
    ctx = dq.DqContext("t", settings=tmp_data, con=__import__("duckdb").connect())
    out = dq.issuer_sector_loss_hook(ctx)
    assert len(out) == 1 and out[0].severity == "error" and not out[0].passed and out[0].metric_value == 0.095
    a = sector_loss_alert(tmp_data)
    assert a and a["id"] == "sector_loss" and "1900 of 20000" in a["detail"]
    # ages out after 8 days; a passing run clears it
    assert sector_loss_alert(tmp_data, now=datetime.now(UTC) + timedelta(days=9)) is None
    I.record_sector_loss({"passed": True, "lost": 0, "prev_classified": 20000, "rate": 0.0, "threshold": 0.01}, "R2",
                         tmp_data)
    assert sector_loss_alert(tmp_data) is None and dq.issuer_sector_loss_hook(ctx)[0].passed
