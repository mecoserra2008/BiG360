"""CFTC COT parser (recorded Socrata CSV fixtures, Sep-Dec 2025 incl. the shutdown backlog), release schedule, mapping."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.ingestion import cftc
from app.ingestion.cftc_mapping import CURATED, asset_class_for, name_match, norm_name, primary_report_for

FX = Path(__file__).parent / "fixtures"


def _parse(rt: str):
    return cftc.parse_csv((FX / f"{rt}.csv").read_bytes(), rt, now=datetime(2026, 10, 19, tzinfo=UTC))


@pytest.mark.parametrize("rt", list(cftc.DATASETS))
def test_parse_all_report_types(rt):
    pos, meta = _parse(rt)
    fam = cftc.family(rt)
    assert pos.height > 0 and meta.height > 0
    assert set(pos.get_column("category").unique()) <= set(cftc.CATEGORIES[fam])
    assert pos.get_column("report_type").unique().to_list() == [rt]
    assert pos.schema["long_pos"] == pl.Int64 and pos.schema["traders_long"] == pl.Int32
    assert pos.schema["pct_oi_long"] == pl.Float32
    # percent -> fraction
    assert pos.get_column("pct_oi_long").drop_nulls().max() <= 1.0
    # PK unique
    assert pos.select(pl.struct("market_code", "report_date", "category").n_unique()).item() == pos.height


def test_legacy_values_and_market_fields():
    pos, _ = _parse("legacy_fut")
    r = pos.filter((pl.col("market_code") == "067651") & (pl.col("report_date") == date(2025, 9, 2))
                   & (pl.col("category") == "noncomm")).row(0, named=True)
    assert (r["long_pos"], r["short_pos"], r["spread_pos"]) == (293055, 190627, 807992)
    assert r["open_interest"] == 1987861 and r["traders_long"] == 106
    assert r["conc_net_4_long"] == pytest.approx(0.112, abs=1e-6)
    # nonrept has no trader counts; comm has no spreading
    nr = pos.filter((pl.col("category") == "nonrept")).get_column("traders_long")
    assert nr.null_count() == nr.len()
    assert pos.filter(pl.col("category") == "comm").get_column("spread_pos").null_count() > 0


def test_cit_mixed_case_headers():
    pos, _ = _parse("cit_comb")
    cats = set(pos.get_column("category").unique())
    assert {"noncomm", "comm", "cit", "tot_rept", "nonrept"} <= cats
    r = pos.filter((pl.col("category") == "noncomm") & (pl.col("report_date") == date(2025, 9, 2))).row(0, named=True)
    assert (r["long_pos"], r["short_pos"], r["spread_pos"]) == (147532, 364528, 516643)
    assert pos.filter(pl.col("category") == "comm").get_column("short_pos").null_count() == 0


def test_knowledge_observed_and_shutdown_backlog():
    pos, _ = _parse("tff_fut")
    k = pos.group_by("report_date").agg(pl.col("knowledge_ts").first(), pl.col("knowledge_date").first(),
                                        pl.col("knowledge_date_estimated").first()).sort("report_date")
    normal = k.filter(pl.col("report_date") == date(2025, 9, 2)).row(0, named=True)
    assert normal["knowledge_date"] == date(2025, 9, 5) and not normal["knowledge_date_estimated"]
    # 2025 shutdown: 2025-09-30 positions were published 2025-11-19 (observed :created_at)
    late = k.filter(pl.col("report_date") == date(2025, 9, 30)).row(0, named=True)
    assert late["knowledge_date"] == date(2025, 11, 19) and not late["knowledge_date_estimated"]
    assert (k.get_column("knowledge_date") > k.get_column("report_date")).all()


def test_knowledge_bulk_load_is_estimated():
    head = "\":created_at\",\"cftc_contract_market_code\",\"report_date_as_yyyy_mm_dd\",\"open_interest_all\",\"noncomm_positions_long_all\",\"noncomm_positions_short_all\"\n"
    rows = "".join(f"\"2022-09-13T14:16:09.004Z\",\"067651\",\"{d}T00:00:00.000\",\"100\",\"60\",\"40\"\n"
                   for d in ("2022-08-16", "2022-08-23", "2022-08-30", "2022-09-06"))
    pos, _ = cftc.parse_csv((head + rows).encode(), "legacy_fut")
    assert pos.get_column("knowledge_date_estimated").all()
    assert pos.filter(pl.col("report_date") == date(2022, 8, 30)).get_column("knowledge_date").item() == date(2022, 9, 2)


@pytest.mark.parametrize(("rep", "rel"), [
    (date(2026, 10, 13), date(2026, 10, 16)),   # normal Friday
    (date(2026, 11, 24), date(2026, 11, 30)),   # Thanksgiving -> Monday (CFTC 2026 schedule)
    (date(2026, 11, 10), date(2026, 11, 16)),   # Veterans Day (Wed) -> Monday
    (date(2026, 6, 16), date(2026, 6, 22)),     # Juneteenth (Fri) -> Monday
    (date(2025, 12, 30), date(2026, 1, 5)),     # New Year (Thu) -> Monday
    (date(2019, 1, 8), date(2019, 3, 8)),       # 2018-19 shutdown backlog (conservative)
])
def test_release_schedule(rep, rel):
    ts = cftc.release_schedule(rep)
    assert ts.tzinfo is not None
    assert ts.astimezone(cftc.ET).date() == rel and ts.astimezone(cftc.ET).hour == 15


def test_select_columns_cover_specs():
    for rt in cftc.DATASETS:
        cols = cftc.select_columns(rt)
        assert ":created_at" in cols and "cftc_contract_market_code" in cols
        assert len(cols) == len(set(cols))


def test_mapping_curated_and_classes():
    assert CURATED["067651"].sid == "IDX:CL=F" and CURATED["13874A"].root == "ES"
    assert asset_class_for("099741", None, "EURO FX") == "fx"
    assert asset_class_for("XXXXXX", "PRECIOUS METALS", "whatever") == "metals"
    assert asset_class_for("XXXXXX", None, "NANO BITCOIN PERP STYLE - COINBASE") == "crypto"
    assert asset_class_for("XXXXXX", None, "UST 10Y NOTE - CHICAGO BOARD OF TRADE") == "rates"
    assert asset_class_for("XXXXXX", None, "MYSTERY THING") == "other"
    assert primary_report_for("energy", ["legacy_fut", "disagg_fut"]) == "disagg"
    assert primary_report_for("equity", ["legacy_fut", "tff_fut"]) == "tff"
    assert primary_report_for("other", ["legacy_fut"]) == "legacy"
    # every curated futures id follows the IDX:<ROOT>=F convention
    assert all(c.sid == f"IDX:{c.root}=F" for c in CURATED.values() if c.sid)


def test_name_match_exact_only():
    fut = {"IDX:GC=F": "Gold (continuous front month)", "IDX:SI=F": "Silver (continuous front month)"}
    assert norm_name("Gold (continuous front month)") == "GOLD"
    assert name_match("GOLD", fut) == "IDX:GC=F"
    assert name_match("MICRO GOLD", fut) is None


def test_build_markets(tmp_data):
    frames, metas = [], []
    for rt in ("legacy_fut", "disagg_fut", "tff_fut"):
        pos, meta = _parse(rt)
        frames.append(pos)
        metas.append(meta.with_columns(pl.lit(rt).alias("report_type")))
    n = cftc.build_markets(metas, tmp_data, positions=pl.concat(frames, how="vertical_relaxed").lazy(),
                           futures={"IDX:CL=F": "WTI crude oil (continuous front month)"})
    assert n == 4  # 067651, 13874A, 088691, 099741
    from app.core.storage import read_table

    m = read_table("cftc_markets", settings=tmp_data)
    cl = m.filter(pl.col("market_code") == "067651").row(0, named=True)
    assert cl["security_id"] == "IDX:CL=F" and cl["mapping_method"] == "curated" and cl["primary_report"] == "disagg"
    assert set(cl["report_types"]) == {"legacy_fut", "disagg_fut"} and cl["asset_class"] == "energy"
    assert "IDX:QM=F" in cl["linked_security_ids"]
    es = m.filter(pl.col("market_code") == "13874A").row(0, named=True)
    assert es["primary_report"] == "tff" and es["exchange"] == "CHICAGO MERCANTILE EXCHANGE"
