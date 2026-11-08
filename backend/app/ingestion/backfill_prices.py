"""Manual job `backfill_prices`: one-off long-history price load.

1. yfinance `period="max"` for the whole universe + benchmarks (raw bars + splits/dividends) - ~15-20 min for 7k
   symbols in batches of 100-200.
2. Polygon grouped-daily for every missing XNYS session of the last ~2 years (free-tier depth). At the shared 4/min
   budget ~500 sessions take ~2 h; the job is resumable (already-loaded dates are skipped) and can be capped with
   `params.polygon_days`.
3. Polygon dividends/splits for 2 years + resolution into corporate_actions.
4. prices_finalize (full rebuild of prices_daily).

params: yf_period (default "max"), polygon_days (default 504; 0 disables), symbols (subset), skip_yf, skip_finalize.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logsetup import get_logger
from app.core.storage import write_table
from app.ingestion.base import MissingApiKey

log = get_logger(__name__)


def run(ctx):  # noqa: ANN001 - JobContext
    from app.ingestion.corporate_actions import PolygonCorporateActionsAdapter  # noqa: PLC0415
    from app.ingestion.polygon_prices import PolygonPricesAdapter  # noqa: PLC0415
    from app.ingestion.yf_prices import YFinancePricesAdapter  # noqa: PLC0415
    from app.normalize.prices import finalize, finalize_corporate_actions  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p: dict[str, Any] = ctx.params or {}
    s = ctx.settings
    rows: dict[str, int] = {}
    msgs: list[str] = []
    failed = 0
    t0 = time.perf_counter()
    if not p.get("skip_yf"):
        yf = YFinancePricesAdapter(s, batch_size=int(p.get("batch_size", 100)))
        fr = yf.fetch(entities=p.get("symbols"), period=p.get("yf_period", "max"))
        for k, v in yf.normalize(fr).items():
            rows[k] = rows.get(k, 0) + write_table(v, k, settings=s)
        failed += len(fr.entities_failed)
        msgs.append(f"yfinance ok={len(fr.entities_ok)} failed={len(fr.entities_failed)}")
    n_days = int(p.get("polygon_days", 504))
    if n_days > 0:
        try:
            pa = PolygonPricesAdapter(s)
            pa.check_keys()
            fr = pa.fetch(days=n_days)
            for k, v in pa.normalize(fr).items():
                rows[k] = rows.get(k, 0) + write_table(v, k, settings=s)
            failed += len(fr.entities_failed)
            msgs.append(f"polygon dates={len(fr.entities_ok)} failed={len(fr.entities_failed)}")
            ca = PolygonCorporateActionsAdapter(s)
            fr2 = ca.fetch(since=None, max_pages=int(p.get("ca_max_pages", 20)))
            for k, v in ca.normalize(fr2).items():
                rows[k] = rows.get(k, 0) + write_table(v, k, settings=s)
        except MissingApiKey as e:
            msgs.append(f"polygon skipped: {e}")
    rows["corporate_actions"] = finalize_corporate_actions(s)
    if not p.get("skip_finalize"):
        rep = finalize(s)
        rows["prices_daily"] = rep.get("rows_written", 0)
        msgs.append(f"finalize securities={rep.get('securities_out')} disagree={rep.get('reconciliation', {}).get('flagged_rows')}")
    msgs.append(f"{time.perf_counter() - t0:.0f}s")
    return JobResult(status="partial" if failed else "success", rows_written=rows, entities_failed=failed,
                     message="; ".join(msgs))
