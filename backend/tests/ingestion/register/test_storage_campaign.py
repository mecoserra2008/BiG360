"""Coverage v2 W2: storage rules (ESEF landed copies re-used, processed HTTP cache pruned, FIRDS raw zips deleted
after parsing with the processed file set recorded) and the campaign plumbing (step split, probe, esef targets)."""

from __future__ import annotations

import gzip
import json
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import orjson
import polars as pl
import pytest

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "register"


def _land(settings, fxo: str) -> Path:
    d = settings.raw_dir / "esef_xbrl_org" / "xbrl_json" / "dt=2026-10-11"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{fxo}.json.gz"
    p.write_bytes(gzip.compress(orjson.dumps({"facts": {}})))
    return p


def test_esef_reuses_landed_copy_and_prunes_cache(tmp_data):
    from app.core.http import CachedResponse, DiskCache, cache_key
    from app.ingestion import esef as E

    fxo = "5967007LIEEXZXJWMW49-2024-12-31-ESEF-NO-0"
    p = _land(tmp_data, fxo)
    assert E.landed_filings(tmp_data) == {fxo: p}
    cache = DiskCache(tmp_data.cache_dir / "http")
    url = "https://filings.xbrl.org/5967007LIEEXZXJWMW49/2024-12-31/ESEF/NO/0/hexagon-2024-12-31-en.json"
    cache.put("esef_xbrl_org", cache_key("GET", url, None), CachedResponse(200, url, b"x" * 1000, {}, False, 1.0))
    api = "https://filings.xbrl.org/api/entities/X/filings"
    cache.put("esef_xbrl_org", cache_key("GET", api, None), CachedResponse(200, api, b"{}", {}, False, 1.0))
    other = "https://filings.xbrl.org/OTHERLEI00000000000/2024-12-31/ESEF/FR/0/x.json"
    cache.put("esef_xbrl_org", cache_key("GET", other, None), CachedResponse(200, other, b"y", {}, False, 1.0))
    dry = E.prune_http_cache(tmp_data, dry_run=True)
    assert dry["deleted"] == 1 and dry["freed_bytes"] == 1000
    res = E.prune_http_cache(tmp_data)
    assert res["deleted"] == 1 and res["kept"] == 2              # API listings and not-yet-landed filings stay
    assert cache.get("esef_xbrl_org", cache_key("GET", url, None), -1) is None
    assert cache.get("esef_xbrl_org", cache_key("GET", other, None), -1) is not None


def test_esef_fetch_skips_download_of_landed_filing(tmp_data, monkeypatch):
    from app.ingestion import esef as E

    fxo = "529900TESTLEI0000000-2025-12-31-ESEF-FR-0"
    _land(tmp_data, fxo)

    class NoNet:
        n_requests = n_cache_hits = 0

        def get(self, url, params=None, cache_ttl=None, **kw):
            if "/api/entities/" in url:
                from app.core.http import CachedResponse

                body = {"data": [{"id": "1", "attributes": {"fxo_id": fxo, "json_url": "/a/b.json",
                                                             "date_added": "2026-04-01T10:00:00Z",
                                                             "period_end": "2025-12-31"}}]}
                return CachedResponse(200, url, orjson.dumps(body), {})
            raise AssertionError(f"unexpected download {url}")

    ad = E.EsefXbrlOrgAdapter(settings=tmp_data, mode="per_lei")
    ad.http = NoNet()
    monkeypatch.setattr(ad, "universe_leis", lambda: {"529900TESTLEI0000000": "I1"})
    res = ad.fetch(entities=["529900TESTLEI0000000"])
    assert res.meta["reused_landed"] == 1 and res.meta["filings"][0]["_issuer_id"] == "I1"


def test_firds_equity_parses_once_and_deletes_raw(tmp_data, monkeypatch):
    from app.ingestion import firds as F

    calls = {"download": 0}

    class Http:
        def download(self, url, dest):
            calls["download"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("FULINS_E_20261031_01of01.xml", (FIX / "firds_fulins_e_sample.xml").read_bytes())
            return dest

    files = [{"file_name": "FULINS_E_20261031_01of01.zip", "url": "https://x/FULINS_E_20261031_01of01.zip",
              "publication_date": "2026-10-31"}]
    monkeypatch.setattr(F, "list_full_files", lambda source, s=None, http=None, lookback_days=15: files)
    state: dict = {}
    df, info = F.firds_equity("fca", tmp_data, state, http=Http())
    assert info["status"] == "parsed" and df.height == 9 and calls["download"] == 1
    assert not list((tmp_data.raw_dir / "fca_firds" / "fulins_e").glob("*.zip"))      # raw zip deleted after parsing
    assert state["firds_processed"]["fca"]["files"] == ["FULINS_E_20261031_01of01.zip"]
    df2, info2 = F.firds_equity("fca", tmp_data, state, http=Http())
    assert info2["status"] == "unchanged" and calls["download"] == 1 and df2.height == 9
    pq = pl.read_parquet(F.reg_dir(tmp_data) / "firds_fca_equity.parquet")
    assert pq.height == 9


def test_register_fetch_rejects_heavy_steps():
    from app.ingestion import register_backfill as B

    ctx = SimpleNamespace(params={"steps": ["esef", "normalize"]}, settings=None)
    with pytest.raises(ValueError):
        B.run_fetch(ctx)
    assert set(B.FETCH_STEPS).isdisjoint(B.HEAVY_STEPS)
    assert set(B.FETCH_STEPS) | set(B.HEAVY_STEPS) == set(B.ALL_STEPS)


def test_register_probe(tmp_data):
    from app.ops.tasks import probe

    st = {"runs": {"esef": {"status": "done", "at": "2026-10-31T17:00:00+00:00"},
                   "fca_nsm": {"status": "partial", "at": "2026-10-31T17:10:00+00:00"}}}
    (tmp_data.state_dir / "register_backfill.json").write_text(json.dumps(st), encoding="utf-8")
    c = {"progress": {"probe": "register_backfill", "steps": ["esef", "fca_nsm", "ch"]}}
    pr = probe(c, tmp_data)
    assert pr["done"] is False and pr["done_units"] == 1 and pr["total_units"] == 3 and pr["last_run_at"] is None
    st["runs"]["fca_nsm"]["status"] = "done"
    st["runs"]["ch"] = {"status": "done", "at": "2026-10-31T18:00:00+00:00"}
    (tmp_data.state_dir / "register_backfill.json").write_text(json.dumps(st), encoding="utf-8")
    pr2 = probe(c, tmp_data)
    assert pr2["done"] and pr2["last_run_at"] == "2026-10-31T18:00:00+00:00"


def test_esef_targets_only_register_leis_not_loaded(tmp_data):
    from app.core.storage import write_table
    from app.ingestion import register_backfill as B
    from app.ingestion import regulatory_register as R

    base = {k: None for k in R.REGISTER_SCHEMA}
    reg = pl.DataFrame([{**base, "market": "EU", "register": "esma_rm", "issuer_key": "L1", "issuer_name": "A", "lei": "L1",
                         "status": "active", "as_of": date(2026, 10, 31)}], schema=R.REGISTER_SCHEMA)
    write_table(reg.with_columns(pl.lit(date(2026, 10, 31)).cast(pl.Datetime("us", "UTC")).alias("updated_at")),
                "regulatory_register", mode="overwrite", settings=tmp_data)
    fi = pl.DataFrame({"filing_id": ["esef_xbrl_org:L1-2024-12-31-ESEF-FR-0"], "source_id": ["esef_xbrl_org"], "year": [2025],
                       "source_entity_id": ["L1"], "form_type": ["AFR"], "form_class": ["annual"],
                       "knowledge_date": [date(2025, 4, 1)], "is_amendment": [False], "has_xbrl": [True],
                       "ingested_at": [date(2025, 4, 1)]}).with_columns(pl.col("ingested_at").cast(pl.Datetime("us", "UTC")))
    from app.core.schema import conform

    write_table(conform(fi, "filings_index"), "filings_index", settings=tmp_data)
    census = pl.DataFrame({"fxo_id": ["L1-2024-12-31-ESEF-FR-0", "L1-2025-12-31-ESEF-FR-0", "L2-2025-12-31-ESEF-FR-0",
                                      "L1-2017-12-31-ESEF-FR-0"],
                           "lei": ["L1", "L1", "L2", "L1"], "period_end": ["2024-12-31", "2025-12-31", "2025-12-31", "2017-12-31"],
                           "json_url": ["/a", "/b", "/c", "/d"]})
    t = B.esef_targets(tmp_data, census)
    assert t["fxo_id"].to_list() == ["L1-2025-12-31-ESEF-FR-0"]


def test_campaign_state_merge_save(tmp_data):
    """register_backfill and register_fetch run side by side: each saves only its own steps."""
    from app.ingestion import register_backfill as B

    a = B.load_state(tmp_data)
    b = B.load_state(tmp_data)
    a["us_facts"] = {"done": ["1", "2"]}
    a["runs"] = {"us_facts": {"status": "partial"}}
    B.save_state(a, tmp_data, ("us_facts", "runs"))
    b["esef"] = {"failed": ["x"]}
    b["runs"] = {"esef": {"status": "done"}}
    B.save_state(b, tmp_data, ("esef", "runs"))          # b's copy has no us_facts: it must not erase it
    disk = B.load_state(tmp_data)
    assert disk["us_facts"]["done"] == ["1", "2"] and disk["esef"]["failed"] == ["x"]
    assert set(disk["runs"]) == {"us_facts", "esef"}
