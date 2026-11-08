"""ESEF annual financial reports from filings.xbrl.org (XBRL International) -> filings_index + fundamentals_raw.

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §5.1.
  * JSON:API index  GET https://filings.xbrl.org/api/filings?sort=-date_added&include=entity&page[size]=..&page[number]=..
                    GET https://filings.xbrl.org/api/entities/{LEI}/filings
  * facts           GET https://filings.xbrl.org{json_url}  (OIM xBRL-JSON)
  * Coverage is patchy (DE/IE empty; ES/PT/SE/PL lag). Treated as a bonus source behind the national OAMs.
PIT: knowledge_date = date_added (repository ingestion time, later than publication => conservative).
Keys: none. source_entity_id = LEI. Accounting standard: IFRS (ifrs-full), reporting currency = fact unit.

Modes (param `mode`, coverage-B fix 2026-10-11; COVERAGE_OPTIONS F1/F2):
  * recent  : the newest filings repository-wide since the watermark (capped by max_filings) - cheap nightly path;
  * per_lei : `/api/entities/{LEI}/filings` for EVERY EU/UK universe issuer LEI (incl. VERIFIED_LEIS for issuers whose
              LEI is missing in reference data, and known ESEF filer LEIs such as AXA's) - complete, no cap;
  * auto    : (default) recent + a per-LEI sweep when the last sweep is older than `sweep_days` (7) or on a backfill
              (no watermark). issuer_id is filled at ingest for universe LEIs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.http import HttpError
from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, load_state, run_adapter_job, safe_call, save_state,
                                          to_date)
from app.ingestion._xbrl_json import numeric_facts, parse_xbrl_json
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

BASE = "https://filings.xbrl.org"


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=UTC)
    except ValueError:
        return None


@register_adapter
class EsefXbrlOrgAdapter(SourceAdapter):
    source_id = "esef_xbrl_org"
    regions = ("EU", "UK")
    produces = ("filings_index", "fundamentals_raw")
    base_url = BASE
    rate_limit = RateLimit(1.0, 2)

    def __init__(self, *args: Any, max_filings: int = 400, page_size: int = 100, mode: str = "auto",
                 sweep_days: int = 7, coverage_universe: str = "mvp", **kw: Any):
        # job params such as universe/tickers are accepted and ignored (run_adapter_job passes all params)
        super().__init__(*args, **{k: v for k, v in kw.items() if k in ("settings", "http")})
        self.max_filings = int(max_filings)
        self.page_size = int(page_size)
        self.mode = str(mode or "auto").lower()
        self.sweep_days = int(sweep_days)
        #: universe_id pattern (regex) of the issuers swept in per_lei mode; "mvp|eu_broad" for the EU expansion
        self.coverage_universe = str(coverage_universe or "mvp")

    def universe_leis(self) -> dict[str, str]:
        """LEI -> issuer_id for every EU/UK universe issuer (reference LEI + verified/filer LEIs)."""
        from app.ingestion._oam_common import coverage_universe

        out: dict[str, str] = {}
        for i in coverage_universe(self.settings, regions=("EU", "UK"), universe=self.coverage_universe):
            for lei in i.get("leis") or []:
                out.setdefault(lei, i["issuer_id"])
        return out

    # ------------------------------------------------------------------ discover
    def discover(self) -> pl.DataFrame:
        rows: list[dict[str, Any]] = []
        page = 1
        while page < 200:
            j = self.http.get("/api/entities", params={"page[size]": 1000, "page[number]": page}, cache_ttl=86400).json()
            data = j.get("data") or []
            for e in data:
                a = e.get("attributes") or {}
                rows.append({"source_entity_id": a.get("identifier"), "lei": a.get("identifier"), "name": a.get("name")})
            if len(data) < 1000:
                break
            page += 1
        return self.discover_frame([r for r in rows if r["source_entity_id"]], self.source_id)

    # ------------------------------------------------------------------ fetch
    def _list_recent(self, since: date) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while len(out) < self.max_filings:
            j = self.http.get("/api/filings", params={"sort": "-date_added", "include": "entity",
                                                       "page[size]": self.page_size, "page[number]": page},
                              cache_ttl=3600).json()
            ents = {e["id"]: (e.get("attributes") or {}) for e in j.get("included") or [] if e.get("type") == "entity"}
            data = j.get("data") or []
            stop = False
            for f in data:
                a = f.get("attributes") or {}
                added = to_date(a.get("date_added"))
                if added is None or added < since:
                    stop = True
                    break
                rel = ((f.get("relationships") or {}).get("entity") or {}).get("data") or {}
                a = {**a, "_lei": (ents.get(rel.get("id")) or {}).get("identifier"),
                     "_entity_name": (ents.get(rel.get("id")) or {}).get("name"), "_id": f.get("id")}
                out.append(a)
            if stop or len(data) < self.page_size:
                break
            page += 1
        return out[: self.max_filings]

    def _list_entity(self, lei: str) -> list[dict[str, Any]]:
        try:
            j = self.http.get(f"/api/entities/{lei}/filings", params={"page[size]": 200}, cache_ttl=6 * 3600).json()
        except HttpError as e:
            if e.status == 404:  # LEI not in the repository (e.g. DE/IE issuers): no filings, not an error
                return []
            raise
        return [{**(f.get("attributes") or {}), "_lei": lei, "_id": f.get("id")} for f in j.get("data") or []]

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        since_d = to_date(since) if since else None
        filings: list[dict[str, Any]] = []
        lei_map = safe_call(self.universe_leis, res, "universe", self.log) or {}
        res.entities_ok = [e for e in res.entities_ok if e != "universe"]
        sweep_due = (self.mode == "per_lei" or (self.mode == "auto" and (
            since is None and not state.get("watermark") or
            (to_date(state.get("last_sweep")) or date.min) <= datetime.now(UTC).date() - timedelta(days=self.sweep_days))))
        if not entities and sweep_due and lei_map:
            entities = sorted(lei_map)
            res.meta["per_lei_sweep"] = True
            if self.mode == "auto" and (since or state.get("watermark")):
                recent_since = to_date(since) or to_date(state.get("watermark"))
                filings += safe_call(lambda: self._list_recent(recent_since), res, "list:recent", self.log) or []
        if entities:
            for lei in entities:
                got = safe_call(lambda lei=lei: self._list_entity(lei), res, f"list:{lei}", self.log)
                for f in got or []:
                    if res.meta.get("per_lei_sweep") or since_d is None or (to_date(f.get("date_added")) or date.min) >= since_d:
                        filings.append(f)
        else:
            since_d = since_d or to_date(state.get("watermark")) or (datetime.now(UTC).date() - timedelta(days=30))
            filings = safe_call(lambda: self._list_recent(since_d), res, "list:recent", self.log) or []
        # one filing per fxo_id (recent + per-LEI listings overlap)
        filings = list({f.get("fxo_id") or f.get("_id"): f for f in filings}.values())
        self.save_raw("filings", f"index_{datetime.now(UTC):%H%M%S}", filings)
        landed: list[dict[str, Any]] = []
        cap = len(filings) if res.meta.get("per_lei_sweep") or entities else self.max_filings
        # coverage v2 W2 (storage): a filing whose compact numeric copy is already landed is re-emitted from that copy
        # (same facts; new universe LEIs get their issuer_id) instead of being downloaded again, and downloads are no
        # longer kept in the HTTP cache (the full xBRL-JSON responses had grown to 32 GB; the landed copies are ~100 MB)
        known = landed_filings(self.settings)
        res.meta["reused_landed"] = 0
        for f in filings[:cap]:
            json_url, fxo = f.get("json_url"), f.get("fxo_id")
            if not json_url or not fxo or not f.get("_lei"):
                continue
            if fxo in known:
                landed.append({**f, "_raw_path": str(known[fxo]), "_issuer_id": lei_map.get(f["_lei"])})
                res.meta["reused_landed"] += 1
                continue

            def get_one(f=f, json_url=json_url, fxo=fxo) -> None:
                doc = self.http.get(BASE + json_url, cache_ttl=0).json()
                path = self.save_raw("xbrl_json", fxo, numeric_facts(doc))
                landed.append({**f, "_raw_path": str(path), "_issuer_id": lei_map.get(f["_lei"])})

            safe_call(get_one, res, fxo, self.log)
        res.meta["filings"] = landed
        res.meta["n_listed"] = len(filings)
        added = [to_date(f.get("date_added")) for f in filings if f.get("date_added")]
        if added and (not entities or res.meta.get("per_lei_sweep")):
            res.watermark = max(d for d in added if d).isoformat()
            state["watermark"] = max(res.watermark, state.get("watermark") or "")
        if res.meta.get("per_lei_sweep"):
            state["last_sweep"] = datetime.now(UTC).date().isoformat()
            res.meta["n_leis"] = len(entities or [])
        save_state(self.source_id, state, self.settings)
        return res

    # ------------------------------------------------------------------ normalize
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        frows: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        from pathlib import Path

        for f in fetched.meta.get("filings", []):
            lei, fxo = f["_lei"], f["fxo_id"]
            added = to_date(f.get("date_added")) or datetime.now(UTC).date()
            pe = to_date(f.get("period_end"))
            seq = fxo.rsplit("-", 1)[-1]
            fid = f"{self.source_id}:{fxo}"
            frows.append(filing_row(
                source_id=self.source_id, native_key=fxo, source_entity_id=lei, form_type="AFR", form_class="annual",
                knowledge_date=added, knowledge_ts=_ts(f.get("date_added")), period_end=pe,
                fiscal_year=pe.year if pe else None, fiscal_period="FY", is_amendment=seq.isdigit() and int(seq) > 0,
                url=BASE + f["report_url"] if f.get("report_url") else None,
                doc_path=str(Path(f["_raw_path"]).relative_to(self.settings.data_dir)) if f.get("_raw_path") else None,
                has_xbrl=True))
            frows[-1]["issuer_id"] = f.get("_issuer_id")
            doc = self.load_raw_json(Path(f["_raw_path"]))
            rows = parse_xbrl_json(doc, source_id=self.source_id, source_entity_id=lei, filing_id=fid, form_type="AFR",
                                   knowledge_date=added, report_period_end=pe)
            for r in rows:
                r["issuer_id"] = f.get("_issuer_id")
            facts += rows
        return {"filings_index": frame(frows, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}


def landed_filings(settings: Any) -> dict[str, Path]:
    """fxo_id -> newest landed compact xBRL-JSON copy (raw/esef_xbrl_org/xbrl_json/dt=*/<fxo_id>.json.gz)."""
    root = settings.raw_dir / "esef_xbrl_org" / "xbrl_json"
    out: dict[str, Path] = {}
    if root.exists():
        for p in sorted(root.glob("dt=*/*.json.gz")):
            out[p.name[: -len(".json.gz")]] = p
    return out


def prune_http_cache(settings: Any = None, dry_run: bool = False) -> dict[str, Any]:
    """Delete cached filings.xbrl.org xBRL-JSON responses whose filing has a landed compact copy (processed raw).
    Index / entity listing responses (short TTL) are kept. Returns counts and bytes freed."""
    import json as _json

    from app.core.config import get_settings

    s = settings or get_settings()
    known = landed_filings(s)
    by_name = {}
    for fxo in known:
        by_name[fxo] = fxo
    root = s.cache_dir / "http" / "esef_xbrl_org"
    n = freed = kept = 0
    if not root.exists():
        return {"deleted": 0, "freed_bytes": 0, "kept": 0}
    for meta in root.glob("*/*.json"):
        try:
            url = _json.loads(meta.read_text(encoding="utf-8")).get("url") or ""
        except (OSError, ValueError):
            continue
        if "/api/" in url:
            kept += 1
            continue
        # https://filings.xbrl.org/<LEI>/<period end>/<ESEF>/<CC>/<seq>/<file>.json -> fxo_id "<LEI>-<period>-ESEF-<CC>-<seq>"
        path = url.split("filings.xbrl.org/", 1)[-1].split("/")
        fxo = "-".join(path[:5]) if len(path) >= 6 else None
        fxo = fxo if fxo in by_name else None
        body = meta.with_suffix(".bin")
        if fxo is None:
            kept += 1
            continue
        size = body.stat().st_size if body.exists() else 0
        if not dry_run:
            body.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
        n += 1
        freed += size
    return {"deleted": n, "freed_bytes": freed, "kept": kept, "landed_copies": len(known)}


def run(ctx: Any) -> Any:
    return run_adapter_job(EsefXbrlOrgAdapter, ctx)
