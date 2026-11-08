"""COVERAGE_V3 A9: BoE Bankstats codes (IADB-only, robots-disallowed, no replacement) are catalog `frozen`, not
`retired`: they are no longer fetched but stay macro-panel candidates with their stored history; only the par yields
(replaced over the full history by the zero-coupon series) are retired. Real catalog, synthetic lake, no network."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.core.storage import read_table, write_table
from app.ingestion.macro_common import catalog_doc, freeze_series, frozen_series, load_catalog, series_rows

BANKSTATS = ["boe:LPMVQJW", "boe:LPMBC57", "boe:LPMVTVX", "boe:IUMBV34"]
PAR = ["boe:IUDMNPY", "boe:IUDSNPY", "boe:IUDLNPY"]
OLD_SUFFIX = " [discontinued; replaced by no allowed source (BoE IADB disallowed by robots.txt)]"


def test_catalog_freezes_bankstats_and_retires_par_only(tmp_data):
    doc = catalog_doc(tmp_data)
    assert set(BANKSTATS) <= set(doc["frozen"]) and not set(BANKSTATS) & set(doc["retired"])
    assert {k: doc["retired"][k] for k in PAR} == {"boe:IUDMNPY": "boe:IUDMNZC", "boe:IUDSNPY": "boe:IUDSNZC",
                                                    "boe:IUDLNPY": "boe:IUDLNZC"}
    fz = frozen_series(tmp_data)
    assert all(fz[k]["reason"] == "source_disallowed" and fz[k]["since"] == "2026-11-07" for k in BANKSTATS)


def _store(s) -> dict:
    cat = {g.series_id: g for g in load_catalog(s, ["boe"])}
    ids = BANKSTATS + ["boe:IUDMNPY"]
    ms = series_rows([cat[i] for i in ids], {i: date(2026, 9, 1) for i in ids})
    # what the first A9 run wrote while the Bankstats codes were listed under `retired`
    ms = ms.with_columns(pl.when(pl.col("series_id").is_in(BANKSTATS + ["boe:IUDMNPY"]))
                         .then(pl.col("title") + OLD_SUFFIX).otherwise(pl.col("title")).alias("title"),
                         pl.lit(False).alias("is_core"))
    write_table(ms, "macro_series", settings=s)
    return cat


def test_freeze_series_restores_titles_idempotently(tmp_data):
    cat = _store(tmp_data)
    assert set(freeze_series(["boe"], tmp_data)) == set(BANKSTATS)
    got = {r["series_id"]: r for r in read_table("macro_series", settings=tmp_data).iter_rows(named=True)}
    for sid in BANKSTATS:
        assert got[sid]["title"] == f"{cat[sid].title} [frozen since 2026-11-07: source_disallowed]"
    assert got["boe:IUDMNPY"]["title"].endswith(OLD_SUFFIX)          # the retired par yield is untouched
    stamp = {k: v["updated_at"] for k, v in got.items()}
    freeze_series(["boe"], tmp_data)
    again = {r["series_id"]: r["updated_at"] for r in read_table("macro_series", settings=tmp_data).iter_rows(named=True)}
    assert again == stamp                                            # nothing rewritten the second time


def test_frozen_series_stay_macro_candidates(tmp_data):
    from app.macro.indicators import load_meta, select_candidates

    _store(tmp_data)                     # titles still carry the old '[discontinued' suffix (before freeze_series)
    meta = load_meta(tmp_data, ["GB"])
    assert all(not meta[s].retired and "[frozen since 2026-11-07" in meta[s].title for s in BANKSTATS)
    assert meta["boe:IUDMNPY"].retired
    latest = pl.DataFrame({"series_id": BANKSTATS + ["boe:IUDMNPY"], "date": [date(2026, 9, 1)] * 5,
                           "value": [3.1, 470000.0, 65000.0, 4.6, 4.4]})
    cand = select_candidates(meta, latest, date(2026, 11, 7))
    assert cand[("GB", "money_supply")] == ["boe:LPMVQJW"]
    assert cand[("GB", "private_credit")] == ["boe:LPMBC57"]       # (lending_rate is not a macro_cells indicator)
    assert "boe:IUDMNPY" not in (cand.get(("GB", "yield_10y")) or [])


def test_freshness_check_skips_frozen_series(tmp_data):
    from app.ingestion.macro_audit import freshness_outcomes

    rows = [(sid, "M", 30, True, date(2025, 1, 1)) for sid in BANKSTATS]
    con = SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchall=lambda: rows))
    ctx = SimpleNamespace(has_data=lambda t: True, con=con, settings=tmp_data,
                          now=SimpleNamespace(date=lambda: date(2026, 11, 7)))
    out = freshness_outcomes(ctx)
    assert out[0].details == {"checked": 0, "stale": []} and out[0].passed
