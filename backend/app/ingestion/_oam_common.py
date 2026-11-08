"""Shared base for the coverage-B national OAM / registry adapters (EU ESEF + LATAM XBRL).

Owner: coverage-B. Used by dila_fr, afm_nl, fsma_be, emarket_it, nasdaq_nordic, oslo_newsweb, cnmv_es, biva_mx,
cmf_cl, smv_pe (and esef.py for the per-LEI universe).

Pattern (docs/COMPANY_COVERAGE_OPTIONS.md §3/§5): per universe issuer, list periodic-report filings from the official
storage mechanism with their **deposit / publication timestamp** (= knowledge_ts, PIT), download the ESEF report package
(zip/.xbri) or XBRL instance, convert it with Arelle (`_arelle.to_xbrl_json`, child process + timeout) to OIM
xBRL-JSON and parse it with `_xbrl_json.parse_xbrl_json` (ifrs-full concepts kept without prefix -> canonical chains).

Resumable: data/state/<source_id>.json keeps {"done": {native_key: {...filing, "_raw_path"}}, "failed": {...}}; a
filing already converted is never downloaded again (pass reparse=True to re-emit facts from the landed raw JSON).
issuer_id is filled at ingest from the universe (the universe comes from the reference tables, so it is resolvable).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import polars as pl

from app.core.config import Settings, get_settings
from app.ingestion._arelle import ArelleError, report_period_end, to_xbrl_json
from app.ingestion._global_common import (filing_row, fiscal_year_for, frame, load_state, safe_call, save_state,
                                          to_date)
from app.ingestion._xbrl_json import numeric_facts, parse_xbrl_json
from app.ingestion.base import FetchResult, SourceAdapter

#: LEIs missing from the reference issuers table, verified 2026-10-11 with GLEIF `lei-records?filter[isin]=<ISIN>`
#: (exact ISIN match, not a name match; COVERAGE_OPTIONS F2). yf_ticker -> (LEI, ISIN).
VERIFIED_LEIS: dict[str, tuple[str, str]] = {
    "BNP.PA": ("R0MUWSFPU8MPRO8K5P83", "FR0000131104"),
    "HEIA.AS": ("724500K5PTPSST86UQ23", "NL0000009165"),
    "III.L": ("35GDVHRBMFE7NWATNM84", "GB00B1YW4409"),
    "TEF.MC": ("549300EEJH4FEPDBBR25", "ES0178430E18"),
    "ORA.PA": ("969500MCOONR8990S771", "FR0000133308"),
    "EL.PA": ("549300M3VH1A3ER1TB49", "FR0000121667"),
    "VOLV-B.ST": ("549300HGV012CNC8JD22", "SE0000115446"),
    "SIE.DE": ("W38RGI023J3WT1HWRP32", "DE0007236101"),
    "ALV.DE": ("529900K9B0N5BT694847", "DE0008404005"),
    "ENR.DE": ("5299005CHJZ14D4FDJ62", "DE000ENER6Y0"),
    "RO.SW": ("549300U41AUUVOAAOB37", "CH0012032113"),
    "RACE.MI": ("549300RIVY5EX8RCON76", "NL0011585146"),
}

#: ESEF filer LEI that differs from the issuer LEI in our reference data (filings are filed under this LEI as well).
EXTRA_FILER_LEIS: dict[str, tuple[str, ...]] = {
    "CS.PA": ("9695007ZQTEMPGIL5N67",),  # AXA ESEF filer LEI (COVERAGE_OPTIONS F2)
}


def _read(settings: Settings, table: str) -> pl.DataFrame:
    p = settings.data_dir / "reference" / table
    files = list(p.rglob("*.parquet")) if p.exists() else []
    return pl.read_parquet(files, hive_partitioning=False) if files else pl.DataFrame()


def coverage_universe(settings: Settings | None = None, countries: Sequence[str] | None = None,
                      regions: Sequence[str] | None = None, universe: str = "mvp") -> list[dict[str, Any]]:
    """Universe issuers (non-US) with LEI / ISIN / tickers.

    One dict per issuer: issuer_id, name, country, region, lei, leis (all known LEIs incl. ESEF filer LEIs), isin,
    yf_ticker, yf_tickers, tickers (['SYM@MIC', ...]), fye_month. LEIs missing in reference data are filled from
    VERIFIED_LEIS."""
    s = settings or get_settings()
    um, sm, iss, im = (_read(s, t) for t in ("universe_membership", "securities_master", "issuers", "identifier_map"))
    if um.is_empty() or iss.is_empty():
        return []
    um = um.filter(pl.col("universe_id").str.contains(universe) & pl.col("valid_to").is_null())
    secs = um.join(sm.select("security_id", "issuer_id", "isin"), on="security_id", how="inner")
    ids = secs.get_column("issuer_id").unique().to_list()
    iss = iss.filter(pl.col("issuer_id").is_in(ids) & (pl.col("region") != "US"))
    if countries:
        iss = iss.filter(pl.col("country").is_in(list(countries)))
    if regions:
        iss = iss.filter(pl.col("region").is_in(list(regions)))
    imf = im.filter(pl.col("issuer_id").is_in(iss.get_column("issuer_id").to_list()) & pl.col("valid_to").is_null())
    by: dict[str, dict[str, list[str]]] = {}
    for r in imf.select("issuer_id", "id_type", "id_value").iter_rows(named=True):
        by.setdefault(r["issuer_id"], {}).setdefault(r["id_type"], []).append(r["id_value"])
    isin_of = {r["issuer_id"]: r["isin"] for r in secs.filter(pl.col("isin").is_not_null()).iter_rows(named=True)}
    out = []
    for r in iss.sort("country", "name").iter_rows(named=True):
        m = by.get(r["issuer_id"], {})
        yfs = sorted(set(m.get("yf_ticker", [])))
        # prefer the local listing ticker (has a '.' suffix) over the US ADR ticker
        yf = next((t for t in yfs if "." in t), yfs[0] if yfs else None)
        lei = r.get("lei")
        isin = isin_of.get(r["issuer_id"]) or next((v for v in m.get("isin", []) if v[:2] == r["country"]), None)
        if not lei:
            for t in yfs:
                if t in VERIFIED_LEIS:
                    lei, isin = VERIFIED_LEIS[t][0], isin or VERIFIED_LEIS[t][1]
        leis = [x for x in dict.fromkeys([lei, *m.get("lei", []), *[x for t in yfs for x in EXTRA_FILER_LEIS.get(t, ())]])
                if x]
        mmdd = r.get("fiscal_year_end_mmdd") or ""
        out.append({"issuer_id": r["issuer_id"], "name": r["name"], "country": r["country"], "region": r["region"],
                    "lei": lei, "leis": leis, "isin": isin, "yf_ticker": yf, "yf_tickers": yfs,
                    "tickers": sorted(set(m.get("ticker_mic", []))),
                    "fye_month": int(mmdd[:2]) if len(mmdd) == 4 and mmdd[:2].isdigit() else None})
    return out


def _as_utc(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class PackageFilingAdapter(SourceAdapter):
    """Base adapter: list filings per issuer -> download package -> Arelle -> xBRL-JSON -> fundamentals_raw.

    Subclasses set source_id, countries, base_url, rate_limit and implement `list_filings(issuer)` returning dicts:
        native_key (str, unique within source), knowledge_ts (datetime/ISO, UTC preferred) or knowledge_date,
        doc_url (package / instance URL; None = metadata only), suffix ('.zip' | '.xbri' | '.xbrl' | '.xhtml'),
        form_type, form_class ('annual' | 'semiannual' | 'quarterly'), fiscal_period ('FY' | 'H1' | 'Q1'..),
        period_end (date | None), url (landing page), language (ISO 639-1), is_amendment (bool)
    Optional overrides: `download(filing)`, `to_doc(filing, payload)`, `facts_from_doc(...)`, `entity_key(issuer)`.
    """

    produces: ClassVar[tuple[str, ...]] = ("filings_index", "fundamentals_raw")
    countries: ClassVar[tuple[str, ...]] = ()
    cache_ttl: ClassVar[int] = 6 * 3600
    #: Arelle timeout per package (s)
    arelle_timeout_s: ClassVar[float] = 420.0
    #: list only filings published on/after this year (ESEF starts FY2020/21)
    min_year: ClassVar[int] = 2017
    #: a filing whose download/conversion failed this many runs is skipped afterwards (listed in state "failed")
    max_retries_per_filing: ClassVar[int] = 3

    def __init__(self, *args: Any, max_new: int = 400, reparse: bool = False, issuers: Any = None,
                 include_metadata_only: bool = True, coverage_universe: str = "mvp", **kw: Any):
        # job params such as universe/tickers are accepted and ignored (run_adapter_job passes all params)
        super().__init__(*args, **{k: v for k, v in kw.items() if k in ("settings", "http")})
        self.max_new = int(max_new)
        self.reparse = str(reparse).lower() in ("1", "true", "yes")
        self.include_metadata_only = str(include_metadata_only).lower() not in ("0", "false", "no")
        self._issuers_override = issuers
        #: universe_id pattern (regex, str.contains) of the issuers to cover; "mvp|eu_broad" for the EU expansion
        self.coverage_universe = str(coverage_universe or "mvp")

    # ------------------------------------------------------------------ universe
    def universe(self) -> list[dict[str, Any]]:
        if self._issuers_override is not None:
            return list(self._issuers_override)
        return coverage_universe(self.settings, countries=self.countries or None, universe=self.coverage_universe)

    def entity_key(self, issuer: dict[str, Any]) -> str | None:
        """source_entity_id for an issuer (default: LEI)."""
        return issuer.get("lei")

    def discover(self) -> pl.DataFrame:
        rows = [{"source_entity_id": self.entity_key(i), "name": i["name"], "country": i["country"], "lei": i.get("lei"),
                 "isin": i.get("isin"), "yf_ticker": i.get("yf_ticker")} for i in self.universe()]
        return self.discover_frame([r for r in rows if r["source_entity_id"]], self.source_id)

    # ------------------------------------------------------------------ hooks
    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def download(self, filing: dict[str, Any]) -> bytes:
        return self.http.get(filing["doc_url"], cache_ttl=0).content

    def to_doc(self, filing: dict[str, Any], payload: bytes) -> dict[str, Any]:
        return to_xbrl_json(payload, suffix=filing.get("suffix") or ".zip", timeout_s=self.arelle_timeout_s)

    def facts_from_doc(self, filing: dict[str, Any], doc: dict[str, Any], issuer_fye: int | None,
                       fid: str, kd: date) -> list[dict[str, Any]]:
        pe = to_date(filing.get("period_end"))
        fye = issuer_fye or (pe.month if pe and filing.get("form_class") == "annual" else None)
        return parse_xbrl_json(doc, source_id=self.source_id, source_entity_id=filing["_entity"], filing_id=fid,
                               form_type=filing.get("form_type") or "AFR", knowledge_date=kd,
                               report_period_end=date(2000, fye, 1) if fye else None)

    # ------------------------------------------------------------------ fetch
    def _select(self, entities: Sequence[str] | None) -> list[dict[str, Any]]:
        uni = self.universe()
        if not entities:
            return uni
        want = {e.strip().upper() for e in entities}
        return [i for i in uni if want & {str(x).upper() for x in
                                          [i["issuer_id"], i.get("lei"), i.get("yf_ticker"), self.entity_key(i),
                                           *i.get("yf_tickers", []), *i.get("leis", [])] if x}]

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        done: dict[str, Any] = state.setdefault("done", {})
        failed: dict[str, Any] = state.setdefault("failed", {})
        since_d = to_date(since) if since else None
        out: list[dict[str, Any]] = []
        n_new = 0
        issuers = self._select(entities)
        res.meta["n_issuers"] = len(issuers)
        for iss in issuers:
            ent = self.entity_key(iss)
            if not ent:  # nothing to fetch for this issuer yet: warning only (does not mark the job partial)
                res.meta.setdefault("warnings", {})[iss["name"]] = "no source key (LEI / native id) for issuer"
                self.log.warning("%s: no source key (LEI / native id) for issuer %s - skipped", self.source_id,
                                 iss["name"])
                continue
            listed = safe_call(lambda iss=iss: self.list_filings(iss), res, f"list:{ent}", self.log) or []
            # newest first; within an optional `group` (alternative deposits of the same report, e.g. FR + EN URD)
            # earliest first, and only until one of them converts (fallback when the earliest package is broken)
            def _k(x: dict[str, Any]) -> str:
                return str(x.get("knowledge_ts") or x.get("knowledge_date") or "")
            blocks: dict[Any, list[dict[str, Any]]] = {}
            for i, x in enumerate(listed):
                blocks.setdefault(("g", x["group"]) if x.get("group") is not None else ("s", i), []).append(x)
            ordered = sorted((sorted(v, key=_k) for v in blocks.values()), key=lambda v: max(_k(x) for x in v),
                             reverse=True)
            listed = [x for v in ordered for x in v]
            done_groups = {(v.get("_entity"), v.get("group")) for v in done.values() if v.get("group") is not None}
            for f in listed:
                if f.get("group") is not None and (ent, f["group"]) in done_groups and f["native_key"] not in done:
                    continue
                kts = _as_utc(f.get("knowledge_ts"))
                kd = kts.date() if kts else to_date(f.get("knowledge_date"))
                if kd is None or kd.year < self.min_year:
                    continue
                if since_d and kd < since_d and f["native_key"] not in done:
                    continue
                f = {**f, "_entity": ent, "_issuer_id": iss["issuer_id"], "_fye": iss.get("fye_month"),
                     "knowledge_ts": kts.isoformat() if kts else None, "knowledge_date": kd.isoformat()}
                key = f["native_key"]
                if key in done:
                    if self.reparse:  # landed fields (period_end, fiscal_period ...) win over empty listing fields
                        out.append({**done[key], **{k: v for k, v in f.items() if v is not None}})
                    continue
                if not f.get("doc_url"):
                    if self.include_metadata_only:
                        out.append(f)
                    continue
                if n_new >= self.max_new:
                    res.meta["truncated"] = True
                    continue
                n_new += 1
                if int((failed.get(key) or {}).get("n", 0)) >= self.max_retries_per_filing and not self.reparse:
                    continue  # permanently broken package (e.g. empty zip at the source): stop retrying
                got = safe_call(lambda f=f: self._land(f), res, key, self.log)
                if got is None:
                    failed[key] = {"err": res.entities_failed.get(key, "")[:300], "at": datetime.now(UTC).isoformat(),
                                   "doc_url": f.get("doc_url"), "n": int((failed.get(key) or {}).get("n", 0)) + 1}
                    continue
                failed.pop(key, None)
                done[key] = {k: v for k, v in got.items() if not k.startswith("__")}
                if f.get("group") is not None:
                    done_groups.add((ent, f["group"]))
                out.append(got)
            save_state(self.source_id, state, self.settings)
        res.meta["filings"] = out
        res.meta["arelle_failures"] = {k: v for k, v in failed.items()}
        kds = [to_date(f["knowledge_date"]) for f in out if f.get("knowledge_date")]
        if kds:
            res.watermark = max(d for d in kds if d).isoformat()
            state["watermark"] = max(res.watermark, state.get("watermark") or "")
        save_state(self.source_id, state, self.settings)
        return res

    def _land(self, f: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        payload = self.download(f)
        safe_key = f["native_key"].replace("/", "_").replace(":", "_")[:120]
        pkg = self.raw_dir("packages") / f"{safe_key}{f.get('suffix') or '.zip'}"
        pkg.write_bytes(payload)
        try:
            doc = self.to_doc(f, payload)
        except ArelleError as e:
            raise ArelleError(f"{e} | {e.log_tail[-300:]}") from e
        if not f.get("period_end"):
            pe = report_period_end(doc)
            f = {**f, "period_end": pe.isoformat() if pe else None}
        path = self.save_raw("xbrl_json", safe_key, numeric_facts(doc))
        self.log.info("%s %s: %d facts (%.1fs)", self.source_id, f["native_key"], len(doc.get("facts") or {}),
                      time.time() - t0)
        return {**f, "_raw_path": str(path)}

    # ------------------------------------------------------------------ normalize
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        frows: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        for f in fetched.meta.get("filings", []):
            kd = to_date(f["knowledge_date"])
            kts = _as_utc(f.get("knowledge_ts"))
            pe = to_date(f.get("period_end"))
            fye = f.get("_fye") or (pe.month if pe and f.get("form_class") == "annual" else None)
            fid = f"{self.source_id}:{f['native_key']}"
            raw = Path(f["_raw_path"]) if f.get("_raw_path") else None
            row = filing_row(
                source_id=self.source_id, native_key=f["native_key"], source_entity_id=f["_entity"],
                form_type=f.get("form_type") or "AFR", form_class=f.get("form_class") or "annual",
                knowledge_date=kd, knowledge_ts=kts, period_end=pe,
                fiscal_year=fiscal_year_for(pe, fye) if pe else None, fiscal_period=f.get("fiscal_period"),
                is_amendment=bool(f.get("is_amendment")), language=f.get("language"), url=f.get("url"),
                doc_path=str(raw.relative_to(self.settings.data_dir)) if raw and raw.is_relative_to(self.settings.data_dir) else None,
                has_xbrl=raw is not None)
            row["issuer_id"] = f.get("_issuer_id")
            frows.append(row)
            if raw is None or not raw.exists():
                continue
            doc = self.load_raw_json(raw)
            rows = self.facts_from_doc(f, doc, f.get("_fye"), fid, kd)
            for r in rows:
                r["issuer_id"] = f.get("_issuer_id")
            facts += rows
        return {"filings_index": frame(frows, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}
