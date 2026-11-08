"""coverage-B: filings.xbrl.org per-LEI sweep (COVERAGE_OPTIONS F1/F2) on recorded payload shapes (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.ingestion import esef
from app.ingestion._global_common import load_state, save_state
from app.ingestion._oam_common import coverage_universe

FX = Path(__file__).parent / "fixtures"
LEI = "724500K5PTPSST86UQ23"  # Heineken N.V. (missing in reference data, filled from VERIFIED_LEIS)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _fake(calls):
    doc = json.loads((FX / "dila_danone_oim_small.json").read_text(encoding="utf-8"))
    filing = {"type": "filing", "id": "1", "attributes": {
        "fxo_id": f"{LEI}-2025-12-31-ESEF-NL-0", "date_added": "2026-02-20 10:00:00", "period_end": "2025-12-31",
        "json_url": f"/{LEI}/2025-12-31/ESEF/NL/0/x.json", "report_url": "/x.xhtml"}}
    recent = {"type": "filing", "id": "2", "attributes": {
        "fxo_id": "OTHERLEI000000000000-2025-12-31-ESEF-FR-0", "date_added": "2026-10-01 10:00:00",
        "period_end": "2025-12-31", "json_url": "/o.json"}, "relationships": {"entity": {"data": {"id": "9"}}}}

    def get(url, params=None, **kw):
        calls.append(url)
        if url == f"/api/entities/{LEI}/filings":
            return _Resp({"data": [filing]})
        if url.startswith("/api/entities/"):
            raise esef.HttpError(404, url)
        if url == "/api/filings":
            return _Resp({"data": [recent], "included": [{"type": "entity", "id": "9",
                                                         "attributes": {"identifier": "OTHERLEI000000000000"}}]})
        return _Resp(doc)
    return get


def test_per_lei_sweep_fills_issuer_and_sets_sweep_state(tmp_data, monkeypatch):
    calls: list[str] = []
    a = esef.EsefXbrlOrgAdapter(settings=tmp_data, mode="per_lei", universe="mvp")  # job params tolerated
    monkeypatch.setattr(a, "universe_leis", lambda: {LEI: "IHEINEKEN0001", "NOFILINGS0000000000X": "IOTHER"})
    monkeypatch.setattr(a.http, "get", _fake(calls))
    fetched, rows = a.run(write=True)
    assert fetched.meta["per_lei_sweep"] and fetched.meta["n_leis"] == 2
    assert rows["fundamentals_raw"] > 0 and rows["filings_index"] == 1
    t = a.normalize(fetched)
    assert t["filings_index"]["issuer_id"].to_list() == ["IHEINEKEN0001"]
    assert (t["fundamentals_raw"]["issuer_id"] == "IHEINEKEN0001").all()
    st = load_state("esef_xbrl_org", tmp_data)
    # esef.py stamps the sweep with the UTC date; the local date differs around midnight
    assert st["last_sweep"] == datetime.now(UTC).date().isoformat() and st["watermark"] == "2026-02-20"
    assert "/api/filings" not in calls  # pure per-LEI mode: no repository-wide listing


def test_auto_mode_runs_recent_and_weekly_sweep(tmp_data, monkeypatch):
    calls: list[str] = []
    save_state("esef_xbrl_org", {"watermark": "2026-09-30", "last_sweep": "2026-01-01"}, tmp_data)
    a = esef.EsefXbrlOrgAdapter(settings=tmp_data)
    monkeypatch.setattr(a, "universe_leis", lambda: {LEI: "IHEINEKEN0001"})
    monkeypatch.setattr(a.http, "get", _fake(calls))
    f = a.fetch()
    assert f.meta.get("per_lei_sweep") and "/api/filings" in calls and f"/api/entities/{LEI}/filings" in calls
    assert {x["fxo_id"][:10] for x in f.meta["filings"]} == {LEI[:10], "OTHERLEI00"}
    # sweep done today -> next auto run is incremental only
    calls.clear()
    a.fetch()
    assert not any(c.startswith("/api/entities/") for c in calls)


@pytest.mark.parametrize("ticker,lei", [("BNP.PA", "R0MUWSFPU8MPRO8K5P83"), ("III.L", "35GDVHRBMFE7NWATNM84")])
def test_universe_uses_verified_leis(tmp_data, ticker, lei):
    import polars as pl

    ref = tmp_data.data_dir / "reference"
    for t, df in {
        "universe_membership": pl.DataFrame({"universe_id": ["mvp_global"], "security_id": ["S1"], "valid_to": [None]},
                                            schema_overrides={"valid_to": pl.Date}),
        "securities_master": pl.DataFrame({"security_id": ["S1"], "issuer_id": ["I1"], "isin": [None]},
                                          schema_overrides={"isin": pl.Utf8}),
        "issuers": pl.DataFrame({"issuer_id": ["I1"], "name": ["X"], "country": ["FR"], "region": ["EU"], "lei": [None],
                                 "fiscal_year_end_mmdd": ["1231"]}, schema_overrides={"lei": pl.Utf8}),
        "identifier_map": pl.DataFrame({"issuer_id": ["I1"], "id_type": ["yf_ticker"], "id_value": [ticker],
                                        "valid_to": [None]}, schema_overrides={"valid_to": pl.Date}),
    }.items():
        (ref / t).mkdir(parents=True, exist_ok=True)
        df.write_parquet(ref / t / "part.parquet")
    u = coverage_universe(tmp_data, regions=("EU", "UK"))
    assert u[0]["lei"] == lei and lei in u[0]["leis"] and u[0]["fye_month"] == 12
