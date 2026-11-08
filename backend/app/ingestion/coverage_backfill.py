"""Weekly / manual backfill sweep over the coverage-B official-filing adapters (EU national OAMs + LATAM).

Each adapter re-lists every universe issuer's periodic reports from `since` (default: full history) and only downloads
filings not yet converted (state data/state/<source_id>.json), so a weekly run is cheap and catches anything the
nightly incremental runs missed (late listings, amended deposits, issuers added to the universe).

Params: sources=dila_fr,afm_nl,... (default all), since=YYYY-MM-DD, entities=..., plus adapter params (e.g. history=true
for cnmv_es IPP history, reparse=true to re-emit facts from landed raw JSON).
"""

from __future__ import annotations

import importlib
import time
from typing import Any

from app.core.logsetup import get_logger

log = get_logger("app.ingestion.coverage_backfill")

#: source_id -> module (adapter class is the module's registered PackageFilingAdapter subclass)
SOURCES: dict[str, str] = {
    "dila_fr": "app.ingestion.dila_fr",
    "afm_nl": "app.ingestion.afm_nl",
    "fsma_be": "app.ingestion.fsma_be",
    "emarket_it": "app.ingestion.emarket_it",
    "nasdaq_nordic": "app.ingestion.nasdaq_nordic",
    "oslo_newsweb": "app.ingestion.oslo_newsweb",
    "cnmv_es": "app.ingestion.cnmv_es",
    "biva_mx": "app.ingestion.biva_mx",
    "cmf_cl": "app.ingestion.cmf_cl",
    "smv_pe": "app.ingestion.smv_pe",
}


def run(ctx: Any) -> Any:
    from app.ingestion.base import ADAPTERS, MissingApiKey
    from app.scheduler.base import JobResult

    params = dict(getattr(ctx, "params", {}) or {})
    params.setdefault("max_new", 100000)  # backfill: no per-run cap on new filings (adapters default to 400)
    want = params.pop("sources", None)
    if isinstance(want, str):
        want = [w.strip() for w in want.split(",") if w.strip()]
    since = params.pop("since", None)
    entities = params.pop("entities", None)
    if isinstance(entities, str):
        entities = [e.strip() for e in entities.split(",") if e.strip()]
    rows: dict[str, int] = {}
    ok = failed = n_req = n_hit = 0
    msgs: list[str] = []
    t0 = time.time()
    for sid, mod in SOURCES.items():
        if want and sid not in want:
            continue
        try:
            importlib.import_module(mod)
            cls = ADAPTERS[sid]
            adapter = cls(settings=ctx.settings, **params)
            fetched, r = adapter.run(since=since, entities=entities)
        except MissingApiKey as e:
            msgs.append(f"{sid}: skipped ({e})")
            continue
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the sweep
            failed += 1
            msgs.append(f"{sid}: {type(e).__name__}: {str(e)[:200]}")
            log.exception("coverage backfill %s failed", sid)
            continue
        for k, v in r.items():
            rows[k] = rows.get(k, 0) + v
        ok += len(fetched.entities_ok)
        failed += len(fetched.entities_failed)
        n_req += fetched.n_requests
        n_hit += fetched.n_cache_hits
        msgs.append(f"{sid}:{fetched.status}:{sum(r.values())}")
    status = "success" if not failed else ("partial" if ok else "failed")
    return JobResult(status=status, rows_written=rows, entities_ok=ok, entities_failed=failed, http_requests=n_req,
                     cache_hits=n_hit, message="; ".join(msgs)[:1500] + f" ({time.time() - t0:.0f}s)")
