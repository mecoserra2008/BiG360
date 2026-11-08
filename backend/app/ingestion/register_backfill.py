"""Statements for every regulatory-register issuer (coverage v2 W2): resumable backfill campaign.

Job `register_backfill` (manual dag; filler campaigns register_statements_*), params:
  steps        list, default all, in this order:
     us_facts    SEC companyfacts bulk zip (local copy <= 6 days old, else downloaded once) -> fundamentals_raw for every
                 active US register CIK not done yet, in chunks of `chunk_ciks` (one upsert per chunk, FYE row from the
                 cached submissions API); the zip is deleted when every target is processed.
     us_fsds     SEC Financial Statement Data Sets (landed quarterly extracts 2009q1 ->) re-normalised for the foreign
                 private issuers of the register (20-F / 40-F; companyfacts misses their IFRS 2025 facts).
     esef        filings.xbrl.org index refreshed (resumable census cursor), then every ESEF filing (period end >= 2019)
                 of an EU / UK register LEI that is not in filings_index yet: xBRL-JSON downloaded once (no HTTP cache;
                 the compact numeric copy lands under raw/esef_xbrl_org/xbrl_json), normalised in chunks.
     fca_nsm     FCA NSM annual financial reports (tagged ESEF / UKSEF) for UK Official List LEIs without a current
                 ESEF annual, per LEI (no PDMR notices).
     oam         national OAM adapters (coverage_backfill sources) over universe `full` (the register issuers are
                 members once ref_universe has added them): only filings not converted yet are fetched.
     ch          Companies House bulk accounts: daily files + monthly archives of the last `ch_months` months.
     yf          Yahoo statements (annual + quarterly, the documented fallback) for EU / UK register issuers in the
                 universe whose latest annual or interim has no official source; resumable per ticker.
     normalize   fundamentals_normalize for the register issuers whose raw facts changed during this campaign.
  max_minutes  time budget per run (default 300); a run stops between chunks and the next run resumes.
State: data/state/register_backfill.json (done CIK / LEI / filing / ticker sets per step, last run per step).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

STATE_FILE = "register_backfill.json"
ALL_STEPS = ("us_facts", "us_fsds", "esef", "fca_nsm", "oam", "ch", "yf", "profile", "normalize")


def state_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / STATE_FILE


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict[str, Any], settings: Settings | None = None, keys: Sequence[str] | None = None) -> None:
    """Merge-save: only `keys` (this process's steps; None = all) are written over the file on disk, under a file
    lock, so register_backfill and register_fetch running side by side never overwrite each other's progress.
    `runs` is merged per step."""
    from filelock import FileLock

    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        disk = load_state(settings)
        for k in (keys if keys is not None else [k for k in st if k not in ("updated_at",)]):
            if k == "runs":
                disk.setdefault("runs", {}).update(st.get("runs") or {})
            elif k in st:
                disk[k] = st[k]
        disk["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(disk, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)


def _register(settings: Settings, market: str | None = None) -> pl.DataFrame:
    from app.core.storage import read_table

    reg = read_table("regulatory_register", settings=settings).filter(pl.col("status") == "active")
    return reg.filter(pl.col("market") == market) if market else reg


class Budget:
    def __init__(self, minutes: float | None):
        self.t0 = time.time()
        self.minutes = minutes

    def left(self) -> bool:
        return self.minutes is None or (time.time() - self.t0) / 60 < self.minutes


# ------------------------------------------------------------------------------------------------ steps


SMALL_TARGETS = 200


def _fresh_bulk_zip(s: Settings, max_age_days: int = 6) -> bool:
    root = s.raw_dir / "sec_edgar" / "companyfacts_bulk"
    today = datetime.now(UTC).date()
    for p in root.glob("dt=*/companyfacts.zip") if root.exists() else []:
        try:
            if (today - date.fromisoformat(p.parent.name[3:])).days <= max_age_days and p.stat().st_size > 10_000_000:
                return True
        except ValueError:
            continue
    return False


def step_us_facts(s: Settings, st: dict[str, Any], budget: Budget, chunk: int = 300) -> dict[str, Any]:
    import zipfile

    from app.ingestion.sec_xbrl import SecXbrlAdapter, companyfacts_to_frame, fiscal_year_end_row, upsert_fundamentals_raw

    sst = st.setdefault("us_facts", {})
    done = set(sst.get("done", []))
    reg = _register(s, "US")
    targets = sorted(set(reg["cik"].drop_nulls().to_list()) - done)
    out: dict[str, Any] = {"targets": len(targets), "processed": 0, "no_xbrl": 0, "facts": 0}
    if not targets:
        out["status"] = "done"
        return out
    ad = SecXbrlAdapter(settings=s, params={"mode": "bulk_companyfacts_zip"})
    if len(targets) <= SMALL_TARGETS and not _fresh_bulk_zip(s):
        # a handful of new register CIKs: the companyfacts API (one request each) instead of a 1.4 GB bulk download
        ad_api = SecXbrlAdapter(settings=s, params={"mode": "nightly"})
        fetched_api, rows = ad_api.run(entities=targets)
        done |= set(targets)
        sst["done"] = sorted(done)
        save_state(st, s, ("us_facts",))
        return {**out, "processed": len(targets), "facts": rows.get("fundamentals_raw", 0), "mode": "api",
                "no_xbrl": len(fetched_api.meta.get("no_xbrl", [])), "status": "done"}
    fetched = ad.fetch(entities=targets)
    zpath = fetched.raw_paths[0]
    members = set(fetched.meta.get("members") or [])
    keep = ad.keep
    now = datetime.now(UTC)
    with zipfile.ZipFile(zpath) as z:
        for i in range(0, len(targets), chunk):
            if not budget.left():
                out["stopped"] = "time budget"
                break
            part = targets[i:i + chunk]
            frames = []
            fye = []
            for cik in part:
                name = f"CIK{cik}.json"
                if name not in members:
                    out["no_xbrl"] += 1
                    continue
                try:
                    import orjson

                    frames.append(companyfacts_to_frame(orjson.loads(z.read(name)), keep, now, cik=cik))
                    subs = ad.submissions(cik, ttl=7 * 86400)
                    r = fiscal_year_end_row(cik, subs) if subs else None
                    if r:
                        fye.append(r)
                except Exception as e:  # noqa: BLE001
                    sst.setdefault("failed", {})[cik] = f"{type(e).__name__}: {e}"[:200]
            if fye:
                from app.ingestion.sec_xbrl import RAW_SCHEMA

                frames.append(pl.DataFrame(fye, schema=RAW_SCHEMA, orient="row"))
            frames = [f for f in frames if f.height]
            if frames:
                df = pl.concat(frames, how="vertical_relaxed")
                out["facts"] += upsert_fundamentals_raw(df, s)
                del df
            frames.clear()
            done |= set(part)
            out["processed"] += len(part)
            sst["done"] = sorted(done)
            sst["last_chunk_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            save_state(st, s, ("us_facts",))
            log.info("register us_facts: %d/%d CIKs, %d facts", len(done & set(targets)) , len(targets), out["facts"])
    remaining = sorted(set(reg["cik"].drop_nulls().to_list()) - done)
    out["remaining"] = len(remaining)
    out["status"] = "done" if not remaining else "partial"
    if not remaining and not st.get("keep_zip"):
        # processed: the 1.4 GB bulk zip is not needed any more (the weekly sec_xbrl_bulk downloads a fresh one)
        try:
            Path(zpath).unlink(missing_ok=True)
            out["zip_deleted"] = str(zpath)
        except OSError as e:
            out["zip_delete_error"] = str(e)[:200]
    return out


def step_us_fsds(s: Settings, st: dict[str, Any], budget: Budget) -> dict[str, Any]:
    """FSDS facts of the register's 20-F / 40-F filers from the landed quarterly extracts (2009q1 ->), one quarter at
    a time (bounded memory; the adapter's all-quarters re-normalisation was OOM-killed at the 7.5 GB cap)."""
    import gc

    from app.core.storage import write_table
    from app.ingestion.sec_fsds import SecFsdsAdapter, fsds_to_facts

    sst = st.setdefault("us_fsds", {})
    reg = _register(s, "US").filter(pl.col("filer_category").is_in(["foreign_private_20f", "foreign_40f"]))
    ciks = {str(int(c)) for c in reg["cik"].drop_nulls().to_list()}
    ad = SecFsdsAdapter(settings=s)
    keep = ad.keep()
    root = s.raw_dir / "sec_fsds" / "extract"
    done_q = set(sst.get("quarters", []))
    out: dict[str, Any] = {"targets": len(ciks), "rows": 0, "quarters": 0}
    for sp in sorted(root.glob("*_sub.parquet")):
        q = sp.name.split("_")[0]
        if q in done_q:
            continue
        if not budget.left():
            out["stopped"] = "time budget"
            break
        np_ = sp.with_name(f"{q}_num.parquet")
        df = fsds_to_facts(pl.read_parquet(sp), pl.read_parquet(np_), ciks=ciks, keep=keep, source_id="sec_fsds")
        if df.height:
            out["rows"] += write_table(df, "fundamentals_raw", settings=s)
        del df
        gc.collect()
        done_q.add(q)
        out["quarters"] += 1
        sst["quarters"] = sorted(done_q)
        save_state(st, s, ("us_fsds",))
    out["status"] = "partial" if out.get("stopped") else "done"
    return out


def esef_targets(s: Settings, census: pl.DataFrame, min_period: str = "2019-01-01") -> pl.DataFrame:
    """Census filings of EU / UK register LEIs (period end >= min_period) not yet in filings_index."""
    from app.core.storage import scan_table

    reg = _register(s).filter(pl.col("market").is_in(["EU", "UK"]))
    leis = set(reg["lei"].drop_nulls().to_list())
    have = set(scan_table("filings_index", s).filter(pl.col("source_id") == "esef_xbrl_org")
               .select("filing_id").collect()["filing_id"].to_list())
    c = census.filter(pl.col("lei").is_in(list(leis)) & pl.col("json_url").is_not_null()
                      & (pl.col("period_end").fill_null("") >= min_period))
    c = c.with_columns(("esef_xbrl_org:" + pl.col("fxo_id")).alias("filing_id"))
    return c.filter(~pl.col("filing_id").is_in(list(have))).sort(["lei", "period_end"], descending=[False, True])


def step_esef(s: Settings, st: dict[str, Any], budget: Budget, chunk: int = 60) -> dict[str, Any]:
    from app.core.schema import conform
    from app.core.storage import write_table
    from app.ingestion import eu_expansion as E
    from app.ingestion._xbrl_json import numeric_facts
    from app.ingestion.base import FetchResult
    from app.ingestion.esef import BASE, EsefXbrlOrgAdapter

    sst = st.setdefault("esef", {})
    out: dict[str, Any] = {}
    try:
        census = E.esef_census(s)
        out["census_filings"] = census.height
    except Exception as e:  # noqa: BLE001 - keep going with the landed census
        out["census_error"] = f"{type(e).__name__}: {e}"[:200]
        p = E.census_dir(s) / "esef_filings.parquet"
        census = pl.read_parquet(p) if p.exists() else pl.DataFrame()
    if census.is_empty():
        return {**out, "status": "failed"}
    failed = set(sst.get("failed", []))
    todo = esef_targets(s, census).filter(~pl.col("fxo_id").is_in(list(failed)))
    out.update(targets=todo.height, landed=0, facts=0)
    if todo.is_empty():
        out["status"] = "done"
        return out
    from app.core.storage import read_table

    lei_iid: dict[str, str] = {}
    im = read_table("identifier_map", settings=s).filter((pl.col("id_type") == "lei") & pl.col("valid_to").is_null())
    lei_iid = dict(zip(im["id_value"].to_list(), im["issuer_id"].to_list(), strict=True))
    ad = EsefXbrlOrgAdapter(settings=s)
    rows = todo.to_dicts()
    for i in range(0, len(rows), chunk):
        if not budget.left():
            out["stopped"] = "time budget"
            break
        landed = []
        for f in rows[i:i + chunk]:
            try:
                doc = ad.http.get(BASE + f["json_url"], cache_ttl=0).json()
                path = ad.save_raw("xbrl_json", f["fxo_id"], numeric_facts(doc))
                del doc
                landed.append({**f, "_lei": f["lei"], "_raw_path": str(path), "_issuer_id": lei_iid.get(f["lei"]),
                               "report_url": None})
            except Exception as e:  # noqa: BLE001
                failed.add(f["fxo_id"])
                sst.setdefault("errors", {})[f["fxo_id"]] = f"{type(e).__name__}: {e}"[:160]
        if landed:
            fr = FetchResult(ad.source_id, started_at=datetime.now(UTC))
            fr.meta["filings"] = landed
            tabs = ad.normalize(fr)
            for name, df in tabs.items():
                n = write_table(conform(df, name), name, settings=s)
                if name == "fundamentals_raw":
                    out["facts"] += n
            out["landed"] += len(landed)
            sst.setdefault("leis", [])
            sst["leis"] = sorted(set(sst["leis"]) | {f["lei"] for f in landed})
        sst["failed"] = sorted(failed)
        sst["last_chunk_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        save_state(st, s, ("esef",))
        log.info("register esef: %d/%d filings landed", out["landed"], todo.height)
    out["status"] = "partial" if out.get("stopped") else "done"
    return out


def step_fca_nsm(s: Settings, st: dict[str, Any], budget: Budget, chunk: int = 40) -> dict[str, Any]:
    from app.core.schema import conform
    from app.core.storage import scan_table, write_table
    from app.ingestion.fca_nsm import FcaNsmAdapter

    sst = st.setdefault("fca_nsm", {})
    reg = _register(s, "UK").filter(pl.col("register") == "fca_official_list")
    cutoff = date.today() - timedelta(days=500)
    have = (scan_table("filings_index", s).filter(pl.col("source_id").is_in(["esef_xbrl_org", "fca_nsm"])
                                                  & (pl.col("period_end") >= cutoff))
            .select("source_entity_id").unique().collect()["source_entity_id"].to_list())
    done = set(sst.get("done", []))
    leis = sorted(set(reg["lei"].drop_nulls().to_list()) - set(have) - done)
    out: dict[str, Any] = {"targets": len(leis), "facts": 0, "filings": 0}
    ad = FcaNsmAdapter(settings=s, include_dsh=False, include_esef=True, max_docs=10000)
    for i in range(0, len(leis), chunk):
        if not budget.left():
            out["stopped"] = "time budget"
            break
        part = leis[i:i + chunk]
        try:
            fetched = ad.fetch(since=date(2021, 1, 1), entities=part)
            tabs = ad.normalize(fetched)
            for name, df in tabs.items():
                if name == "insider_transactions" or df.is_empty():
                    continue
                n = write_table(conform(df, name), name, settings=s)
                out["facts" if name == "fundamentals_raw" else "filings"] += n
        except Exception as e:  # noqa: BLE001
            sst.setdefault("errors", {})[part[0]] = f"{type(e).__name__}: {e}"[:200]
            log.warning("register fca_nsm chunk failed: %s", e)
            continue
        done |= set(part)
        sst["done"] = sorted(done)
        save_state(st, s, ("fca_nsm",))
    out["status"] = "partial" if out.get("stopped") else "done"
    return out


def step_oam(s: Settings, st: dict[str, Any], budget: Budget, sources: Sequence[str] | None = None) -> dict[str, Any]:
    import importlib

    from app.ingestion.base import ADAPTERS
    from app.ingestion.coverage_backfill import SOURCES

    sst = st.setdefault("oam", {})
    want = list(sources or ["dila_fr", "afm_nl", "fsma_be", "emarket_it", "nasdaq_nordic", "oslo_newsweb", "cnmv_es"])
    out: dict[str, Any] = {}
    for sid in want:
        if sid in sst.get("done", []):
            continue
        if not budget.left():
            out["stopped"] = "time budget"
            break
        try:
            importlib.import_module(SOURCES[sid])
            ad = ADAPTERS[sid](settings=s, coverage_universe="full|eu_broad|mvp", max_new=100000)
            fetched, rows = ad.run()
            out[sid] = {"status": fetched.status, "rows": rows}
        except Exception as e:  # noqa: BLE001
            out[sid] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200]}
            continue
        sst.setdefault("done", []).append(sid)
        save_state(st, s, ("oam",))
    out["status"] = "partial" if out.get("stopped") else "done"
    return out


def step_ch(s: Settings, st: dict[str, Any], budget: Budget, months: int = 24) -> dict[str, Any]:
    from app.ingestion.ch_accounts import ChAccounts

    ad = ChAccounts(s)
    mins = None if budget.minutes is None else max(5.0, budget.minutes - (time.time() - budget.t0) / 60)
    a = ad.run(mode="daily", max_minutes=mins)
    b = ad.run(mode="monthly", months=months, max_minutes=mins)
    st.setdefault("ch", {})["last_run_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    save_state(st, s, ("ch",))
    return {"daily": {k: a.get(k) for k in ("todo", "remaining", "status")},
            "monthly": {k: b.get(k) for k in ("todo", "remaining", "status")},
            "matched": sum(x.get("matched", 0) for x in list(a.get("archives", {}).values()) + list(b.get("archives", {}).values())),
            "status": "done" if not a.get("remaining") and not b.get("remaining") else "partial"}


def yf_targets(s: Settings) -> list[str]:
    """Yahoo tickers of EU / UK register issuers in the universe without a current official annual + interim."""
    from app.core.storage import read_table

    p = s.data_dir / "ops" / "regulatory_coverage"
    if not p.exists():
        return []
    cov = read_table("regulatory_coverage", settings=s).filter(pl.col("market").is_in(["EU", "UK"])
                                                               & pl.col("in_universe"))
    need = cov.filter(~pl.col("has_annual") | ~pl.col("annual_official").fill_null(False) | ~pl.col("has_interim"))
    sids = set(need["security_id"].drop_nulls().to_list())
    lst = read_table("listings", settings=s).filter(pl.col("is_primary") & pl.col("valid_to").is_null()
                                                    & pl.col("security_id").is_in(list(sids)))
    return sorted(set(lst["yf_ticker"].drop_nulls().to_list()))


def step_yf(s: Settings, st: dict[str, Any], budget: Budget, chunk: int = 40) -> dict[str, Any]:
    from app.core.schema import conform
    from app.core.storage import write_table
    from app.ingestion.yf_fundamentals import YfFundamentalsAdapter

    sst = st.setdefault("yf", {})
    done = set(sst.get("done", []))
    todo = [t for t in yf_targets(s) if t not in done]
    out: dict[str, Any] = {"targets": len(todo), "facts": 0, "ok": 0, "failed": 0}
    ad = YfFundamentalsAdapter(settings=s, reuse_days=14)
    for i in range(0, len(todo), chunk):
        if not budget.left():
            out["stopped"] = "time budget"
            break
        part = todo[i:i + chunk]
        fetched = ad.fetch(entities=part)
        df = ad.normalize(fetched)["fundamentals_raw"]
        if df.height:
            out["facts"] += write_table(conform(df, "fundamentals_raw"), "fundamentals_raw", settings=s)
        out["ok"] += len(fetched.entities_ok)
        out["failed"] += len(fetched.entities_failed)
        done |= set(part)
        sst["done"] = sorted(done)
        sst["last_chunk_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        save_state(st, s, ("yf",))
        log.info("register yf: %d/%d tickers", min(i + chunk, len(todo)), len(todo))
    out["status"] = "partial" if out.get("stopped") else "done"
    return out


def step_normalize(s: Settings, st: dict[str, Any], budget: Budget, flush_issuers: int = 400) -> dict[str, Any]:
    """fundamentals_normalize for register issuers with raw facts ingested since the campaign's last normalize."""
    from app.core.storage import scan_table
    from app.normalize import fundamentals as FN

    sst = st.setdefault("normalize", {})
    since = sst.get("watermark")
    started = datetime.now(UTC)
    # entity lists through DuckDB (out of core, 2 GB): a Polars unique() over the ~45 M raw rows was OOM-killed at the
    # 7.5 GB cap within seconds
    import duckdb

    def q(sql: str) -> pl.DataFrame:
        for attempt in range(6):
            con = duckdb.connect()
            try:
                con.execute("SET memory_limit='2GB'; SET threads=4")
                tmp = (s.data_dir / "tmp" / "duckdb").as_posix()
                con.execute(f"SET temp_directory='{tmp}'")
                return con.execute(sql).pl()
            except (duckdb.IOException, FileNotFoundError):
                if attempt == 5:
                    raise
                time.sleep(5 * (attempt + 1))
            finally:
                con.close()
        return pl.DataFrame()

    raw_glob = (s.data_dir / "normalized" / "fundamentals_raw" / "**" / "*.parquet").as_posix()
    src = f"read_parquet('{raw_glob}', hive_partitioning=true, union_by_name=true)"
    ents_all = FN.resolve_issuers(q(f"SELECT DISTINCT source_id, source_entity_id, issuer_id FROM {src}"))
    ids_all = set(ents_all["issuer_id"].drop_nulls().to_list())
    if since:
        new = q(f"SELECT DISTINCT source_id, source_entity_id, issuer_id FROM {src} WHERE ingested_at > TIMESTAMPTZ '{since}'")
        ids_new = set(FN.resolve_issuers(new)["issuer_id"].drop_nulls().to_list())
    else:
        ids_new = ids_all
    can_glob = (s.data_dir / "normalized" / "fundamentals_canonical" / "**" / "*.parquet").as_posix()
    canon = set(q(f"SELECT DISTINCT issuer_id FROM read_parquet('{can_glob}', hive_partitioning=true, union_by_name=true)")
                ["issuer_id"].to_list())
    reg = _register(s)
    reg_ids = set(reg["issuer_id"].drop_nulls().to_list())
    # register issuers matched after the last register build (new seeds) resolve through the issuers table
    from app.core.storage import read_table

    iss = read_table("issuers", columns=["issuer_id", "cik", "lei"], settings=s)
    ciks, leis = set(reg["cik"].drop_nulls().to_list()), set(reg["lei"].drop_nulls().to_list())
    reg_ids |= set(iss.filter(pl.col("cik").is_in(list(ciks)) | pl.col("lei").is_in(list(leis)))["issuer_id"].to_list())
    # new raw facts since the last campaign normalize, plus resolvable issuers that have no canonical rows yet
    todo = sorted(reg_ids & (ids_new | (ids_all - canon)) if since else reg_ids & (ids_all - canon))
    if not todo:
        sst["watermark"] = started.isoformat()
        save_state(st, s, ("normalize",))
        return {"status": "done", "issuers": 0}
    res = None
    for attempt in range(4):  # a concurrent writer swapping a fundamentals_raw partition -> re-list and retry
        try:
            res = FN.normalize(todo, settings=s, flush_issuers=flush_issuers)
            break
        except FileNotFoundError:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))
    sst["watermark"] = started.isoformat()
    sst["last_run"] = {"issuers": res.get("issuers"), "canonical": res.get("canonical"), "wide": res.get("wide"),
                       "at": datetime.now(UTC).isoformat(timespec="seconds")}
    save_state(st, s, ("normalize",))
    return {"status": "done", "issuers": res.get("issuers"), "canonical": res.get("canonical"), "wide": res.get("wide")}


def step_profile(s: Settings, st: dict[str, Any], budget: Budget, chunk: int = 200) -> dict[str, Any]:
    """Yahoo .info profiles (sector / industry / currency / market cap) of the register issuers without a sector,
    cached in registry/yf_info.parquet (~1 ticker/s, shared limiter); the next ref_universe run classifies them."""
    from app.core.storage import read_table
    from app.normalize import identity_universe as U

    sst = st.setdefault("profile", {})
    reg = _register(s).filter(pl.col("market").is_in(["EU", "UK"]))
    iss = read_table("issuers", columns=["issuer_id", "lei", "sector_id"], settings=s)
    need = set(iss.filter(pl.col("lei").is_in(reg["lei"].drop_nulls().to_list()) & pl.col("sector_id").is_null())["issuer_id"].to_list())
    secs = read_table("securities_master", settings=s).filter(pl.col("issuer_id").is_in(list(need)) & pl.col("is_primary_security"))
    lst = read_table("listings", settings=s).filter(pl.col("security_id").is_in(secs["security_id"].to_list())
                                                    & pl.col("is_primary") & pl.col("valid_to").is_null())
    tickers = sorted(set(lst["yf_ticker"].drop_nulls().to_list()))
    out: dict[str, Any] = {"targets": len(tickers), "fetched": 0}
    for i in range(0, len(tickers), chunk):
        if not budget.left():
            out["stopped"] = "time budget"
            break
        got = U.fetch_yf_info(tickers[i:i + chunk], settings=s, max_age_days=120)
        out["fetched"] += got.filter(pl.col("ok").fill_null(False)).height if got.height else 0
        sst["last_chunk_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        save_state(st, s, ("profile",))
    out["status"] = "partial" if out.get("stopped") else "done"
    return out


STEP_FN = {"profile": step_profile, "us_facts": step_us_facts, "us_fsds": step_us_fsds, "esef": step_esef, "fca_nsm": step_fca_nsm,
           "oam": step_oam, "ch": step_ch, "yf": step_yf, "normalize": step_normalize}


def run_steps(settings: Settings | None = None, steps: Sequence[str] = ALL_STEPS, max_minutes: float | None = 300,
              **kw: Any) -> dict[str, Any]:
    s = settings or get_settings()
    st = load_state(s)
    budget = Budget(max_minutes)
    out: dict[str, Any] = {}
    for step in steps:
        if step not in STEP_FN:
            out[step] = {"status": "unknown step"}
            continue
        if not budget.left() and step != "normalize":
            out[step] = {"status": "skipped (time budget)"}
            continue
        t0 = time.time()
        try:
            extra: dict[str, Any] = {}
            if step == "ch" and kw.get("ch_months"):
                extra = {"months": int(kw["ch_months"])}
            elif step == "oam" and kw.get("sources"):
                srcs = kw["sources"]
                extra = {"sources": srcs.split(",") if isinstance(srcs, str) else list(srcs)}
            res = STEP_FN[step](s, st, budget, **extra)
        except Exception as e:  # noqa: BLE001 - one broken step must not stop the campaign
            log.exception("register_backfill %s failed", step)
            res = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
        res["seconds"] = round(time.time() - t0, 1)
        out[step] = res
        st["runs"] = {step: {**{k: v for k, v in res.items() if not isinstance(v, (list, dict))},
                             "at": datetime.now(UTC).isoformat(timespec="seconds")}}
        save_state(st, s, ("runs",))
    return out


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult

    p = dict(ctx.params or {})
    steps = p.get("steps") or list(ALL_STEPS)
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.split(",") if x.strip()]
    res = run_steps(ctx.settings, steps, max_minutes=p.get("max_minutes", 300),
                    **{k: v for k, v in p.items() if k not in ("steps", "max_minutes")})
    failed = [k for k, v in res.items() if v.get("status") == "failed"]
    partial = [k for k, v in res.items() if v.get("status") == "partial"]
    status = "failed" if failed and len(failed) == len(res) else ("partial" if failed or partial else "success")
    msg = "; ".join(f"{k}: {v.get('status')}" + (f" ({v.get('error')})" if v.get("error") else "") for k, v in res.items())
    return JobResult(status=status, rows_written={}, entities_ok=len(res) - len(failed), entities_failed=len(failed),
                     message=msg[:1500])


FETCH_STEPS = ("esef", "fca_nsm", "oam", "ch", "yf", "profile")
HEAVY_STEPS = ("us_facts", "us_fsds", "normalize")


def run_fetch(ctx: Any) -> Any:
    """Job register_fetch: the network-bound steps (esef, fca_nsm, oam, ch, yf) under a small RAM budget; the heavy
    steps (us_facts, us_fsds, normalize) belong to job register_backfill. New raw facts are normalised by the nightly
    fundamentals_normalize (incremental) or the next register_backfill normalize step."""
    p = dict(ctx.params or {})
    steps = p.get("steps") or list(FETCH_STEPS)
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.split(",") if x.strip()]
    bad = [x for x in steps if x not in FETCH_STEPS]
    if bad:
        raise ValueError(f"register_fetch runs only {FETCH_STEPS}; {bad} belong to register_backfill")
    ctx.params = {**p, "steps": steps}
    return run(ctx)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.ingestion.register_backfill")
    ap.add_argument("--steps", default=",".join(ALL_STEPS))
    ap.add_argument("--max-minutes", type=float, default=300)
    ap.add_argument("--ch-months", type=int, default=24)
    a = ap.parse_args(argv)
    from app.core.logsetup import setup_logging

    setup_logging(log_file="register_backfill.log", service="cli")
    res = run_steps(steps=[x.strip() for x in a.steps.split(",") if x.strip()], max_minutes=a.max_minutes,
                    ch_months=a.ch_months)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
