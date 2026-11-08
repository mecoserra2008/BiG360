"""Job credit_trace: FINRA TRACE market aggregates via the keyed FINRA API -> credit_market_stats.

Auth: OAuth2 client credentials (FINRA API console, free account for public datasets): POST
https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials (HTTP Basic client_id:secret)
-> bearer token; GET https://api.finra.org/data/group/fixedIncomeMarket/name/<dataset>. Without credentials the job
returns `skipped` with a "missing FINRA_API_CLIENT_ID" message (the startup sync reports needs_key). The FINRA Fixed
Income Data website (finra.org/finra-data/fixed-income) is not used: its terms forbid robots / automated copying.
Records are stored long and schema-agnostic (parse_records), so field renames on FINRA's side never break the job.
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger

from .common import client, now_utc

log = get_logger("app.rates.trace")
TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
DATA_URL = "https://api.finra.org/data/group/fixedIncomeMarket/name/{ds}"
DATASETS = ("corporateAndAgencyDebtMarketBreadth", "corporateAndAgencyDebtMarketSentiment",
            "corporate144AMarketBreadth", "corporate144AMarketSentiment", "treasuryDailyAggregates",
            "treasuryWeeklyAggregates")


def _date_of(rec: dict[str, Any]) -> date | None:
    for k, v in rec.items():
        if "date" in k.lower() and isinstance(v, str):
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                continue
    return None


def parse_records(dataset: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{tradeReportDate, productCategory, advances, declines, ...}] -> long rows (numeric fields = metrics)."""
    out = []
    for r in records or []:
        d = _date_of(r)
        if d is None:
            continue
        cat = "|".join(f"{v}" for k, v in sorted(r.items())
                       if isinstance(v, str) and "date" not in k.lower() and v.strip()) or "*"
        for k, v in r.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            out.append({"dataset": dataset, "date": d, "category": cat, "metric": k, "value": float(v),
                        "knowledge_date": d})
    return out


def token(s: Settings) -> str | None:
    cid, sec = s.secret("finra_api_client_id"), s.secret("finra_api_client_secret")
    if not cid or not sec:
        return None
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    c = client("finra_api", s, headers={"Authorization": f"Basic {basic}"})
    j = c.post(TOKEN_URL + "?grant_type=client_credentials").json()
    return j.get("access_token")


def run_trace(s: Settings, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    tok = token(s)
    if tok is None:
        return {"status": "skipped", "rows": 0, "message": "missing FINRA_API_CLIENT_ID / FINRA_API_CLIENT_SECRET in .env "
                                                          "(free FINRA API account; TRACE aggregates not fetched)"}
    c = client("finra_api_data", s, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
    rows, notes = [], []
    for ds in (params or {}).get("datasets") or DATASETS:
        try:
            recs = c.get(DATA_URL.format(ds=ds), params={"limit": 5000}, cache_ttl=0).json()
            rows += parse_records(ds, recs if isinstance(recs, list) else recs.get("data") or [])
        except Exception as e:  # noqa: BLE001
            notes.append(f"{ds}: {type(e).__name__}: {e}"[:200])
    n = 0
    if rows:
        df = pl.DataFrame(rows).unique(["dataset", "date", "category", "metric"], keep="last").with_columns(
            pl.lit(now_utc()).alias("ingested_at"))
        n = write_table(df, "credit_market_stats", mode="upsert", settings=s)
    return {"status": "success" if n else ("failed" if notes else "success"), "rows": n,
            "message": "; ".join(notes) or None}


def run(ctx):  # noqa: ANN001
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_trace(ctx.settings, dict(ctx.params or {}))
    return JobResult(status=rep["status"], rows_written={"credit_market_stats": rep["rows"]}, message=rep["message"],
                     retryable=rep["status"] != "skipped")
