"""Recorded live responses (captured 2026-10-12) for the official series added in macro v2 W1.4, plus static
consistency of the theme / country configuration with the catalog (no network)."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from app.ingestion.macro_common import load_catalog, parse_sdmx_keyed
from app.ingestion.macro_national import parse_acm_rows, parse_ons
from app.macro.countries import COUNTRY_META, SECTION_OF_INDICATOR, SECTIONS
from app.macro.indicators import CELL_INDICATORS, COUNTRIES, EXCLUDED_SOURCES
from app.macro.themes import THEME_PANELS, THEMES, apply_transform

FIX = Path(__file__).parent / "fixtures"
NEW_IDS = {
    "fred:DFII5", "fred:WTREGEN", "fred:CPIUFDSL", "fred:CPIENGSL", "fred:CUSR0000SAH1", "fred:CUSR0000SACL1E",
    "fred:CUSR0000SASLE", "fred:CUSR0000SASL2RS", "fred:CPIMEDSL", "fred:DRTSCIS", "fred:DRTSCLCC", "fred:DRSDCILM",
    "fred:SUBLPDRCSC", "fred:JTSJOR", "fred:JTSQUR", "fred:JTSHIR", "ecb:HICP.M.U2.N.NRGY00.4D0.ANR",
    "ecb:HICP.M.U2.N.FOOD00.4D0.ANR", "ecb:HICP.M.U2.N.IGXE00.4D0.ANR", "ecb:HICP.M.U2.N.SERV00.4D0.ANR",
    "ecb:ILM.W.U2.C.T000000.Z5.Z01", "ecb:BLS.Q.U2.ALL.O.E.Z.B3.ST.S.WFNET", "ecb:BLS.Q.U2.ALL.O.E.Z.B3.ZZ.D.WFNET",
    "ecb:BLS.Q.U2.ALL.Z.H.H.B3.ST.S.WFNET", "ecb:BLS.Q.U2.ALL.Z.H.H.B3.ZZ.D.WFNET", "ons:D7G8", "ons:D7GB", "ons:D7GE",
    "ons:D7NM", "ons:D7NN", "nyfed:ACMTP02", "nyfed:ACMTP05", "nyfed:ACMTP10", "nyfed:ACMRNY10", "nyfed:ACMY10"}


def test_new_series_in_catalog() -> None:
    ids = {g.series_id for g in load_catalog()}
    assert NEW_IDS <= ids, sorted(NEW_IDS - ids)


def test_acm_parser_on_recorded_rows() -> None:
    rows = list(csv.reader((FIX / "acm_daily_tail.csv").read_text(encoding="utf-8").splitlines()))
    out = parse_acm_rows(rows)
    assert {"ACMTP10", "ACMRNY10", "ACMY10", "ACMTP02"} <= set(out)
    tp = out["ACMTP10"]
    assert len(tp) == 25 and tp[-1][0] == date(2026, 10, 8) and -3 < tp[-1][1] < 5
    y, rn = dict(out["ACMY10"]), dict(out["ACMRNY10"])
    d = tp[-1][0]
    assert abs(y[d] - rn[d] - tp[-1][1]) < 0.02   # fitted yield = risk-neutral yield + term premium


def test_ecb_bls_and_hicp_components_csv() -> None:
    bls = parse_sdmx_keyed((FIX / "ecb_bls_csv.csv").read_text(encoding="utf-8"), 11)
    k = next(iter(bls))
    assert k.endswith("O.E.Z.B3.ST.S.WFNET") and len(bls[k]) == 6 and bls[k][-1][0].month in (1, 4, 7, 10)
    h = parse_sdmx_keyed((FIX / "ecb_hicp_components.csv").read_text(encoding="utf-8"), 6)
    assert {x.split(".")[3] for x in h} == {"NRGY00", "SERV00"} and all(len(v) == 6 for v in h.values())


def test_ons_component_json() -> None:
    j = json.loads((FIX / "ons_d7nn.json").read_text(encoding="utf-8"))
    pts = parse_ons(j, "M")
    assert len(pts) == 8 and all(v is not None for _, v in pts) and pts[-1][0] >= date(2026, 1, 1)


def test_theme_series_exist_in_catalog() -> None:
    from app.ingestion.fred import CATALOG

    ids = {g.series_id for g in load_catalog()} | {f"fred:{s.native}" for s in CATALOG}
    assert set(THEMES) == set(THEME_PANELS)
    for theme, spec in THEME_PANELS.items():
        for p in spec["panels"]:
            for s in p.get("series", []):
                assert s["series_id"] in ids, (theme, s["series_id"])
                assert s["series_id"].split(":")[0] not in EXCLUDED_SOURCES
            if p["kind"] == "compare":
                assert p["indicator"] in CELL_INDICATORS and set(p["countries"]) <= set(COUNTRIES)


def test_country_config() -> None:
    assert set(COUNTRY_META) == set(COUNTRIES) and len(COUNTRIES) == 24
    keys = {k for k, _ in SECTIONS}
    assert all(SECTION_OF_INDICATOR[i] in keys for i in CELL_INDICATORS)


def test_display_transforms() -> None:
    d = [date(2024, m, 1) for m in range(1, 13)] + [date(2025, m, 1) for m in range(1, 13)]
    pts = [(x, 100.0 + i) for i, x in enumerate(d)]
    yoy = apply_transform(pts, "yoy", "M")
    assert len(yoy) == 12 and yoy[0][1] == (112 / 100 - 1) * 100
    assert apply_transform(pts, "diff", "M")[0][1] == 1.0
    assert abs(apply_transform(pts, "mom", "M")[0][1] - 1.0) < 1e-12
    z = apply_transform(pts, "z", "M")
    assert z and z[-1][1] > 1.5
