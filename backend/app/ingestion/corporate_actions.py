"""Corporate actions: Polygon dividends/splits (US, market-wide by date, 2y on the free tier) +
yfinance actions (global; staged by app.ingestion.yf_prices from the same downloads) -> corporate_actions_staging,
then resolved to security_id -> corporate_actions (app.normalize.prices.finalize_corporate_actions).

Polygon endpoints (legacy v3 reference, free tier): /v3/reference/dividends?ex_dividend_date.gte=...,
/v3/reference/splits?execution_date.gte=... (1000 rows per page; each page = 1 call of the shared 5/min budget).
Dividend types: CD regular cash, SC special cash, LT/ST capital gains (kept as cash_dividend with details).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.storage import write_table
from app.ingestion.base import FetchResult, MissingApiKey, SourceAdapter, register_adapter
from app.ingestion.polygon_prices import PolygonClient
from app.normalize.prices import finalize_corporate_actions

log = get_logger(__name__)

_DIV_TYPE = {"CD": "cash_dividend", "SC": "special_dividend", "LT": "cash_dividend", "ST": "cash_dividend"}


def _d(x: Any) -> date | None:
    if not x:
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def polygon_dividends_to_rows(results: list[dict[str, Any]]) -> pl.DataFrame:
    now = datetime.now(UTC)
    rows = []
    for r in results:
        ex = _d(r.get("ex_dividend_date"))
        amt = r.get("cash_amount")
        if not r.get("ticker") or ex is None or amt is None:
            continue
        rows.append({
            "source_id": "polygon", "symbol": r["ticker"], "ex_date": ex,
            "action_type": _DIV_TYPE.get(str(r.get("dividend_type") or "CD"), "cash_dividend"),
            "ratio": None, "amount": float(amt), "amount_basis": "raw", "currency": r.get("currency") or "USD",
            "record_date": _d(r.get("record_date")), "pay_date": _d(r.get("pay_date")),
            "declared_date": _d(r.get("declaration_date")),
            "details": orjson.dumps({k: r.get(k) for k in ("frequency", "dividend_type", "id") if r.get(k) is not None}).decode(),
            "ingested_at": now,
        })
    return _frame(rows)


def polygon_splits_to_rows(results: list[dict[str, Any]]) -> pl.DataFrame:
    now = datetime.now(UTC)
    rows = []
    for r in results:
        ex = _d(r.get("execution_date"))
        sf, st = r.get("split_from"), r.get("split_to")
        if not r.get("ticker") or ex is None or not sf or not st:
            continue
        rows.append({
            "source_id": "polygon", "symbol": r["ticker"], "ex_date": ex, "action_type": "split",
            "ratio": float(st) / float(sf), "amount": None, "amount_basis": None, "currency": None,
            "record_date": None, "pay_date": None, "declared_date": None,
            "details": orjson.dumps({"split_from": sf, "split_to": st}).decode(), "ingested_at": now,
        })
    return _frame(rows)


_SCHEMA = {"source_id": pl.Utf8, "symbol": pl.Utf8, "ex_date": pl.Date, "action_type": pl.Utf8, "ratio": pl.Float64,
           "amount": pl.Float64, "amount_basis": pl.Utf8, "currency": pl.Utf8, "record_date": pl.Date,
           "pay_date": pl.Date, "declared_date": pl.Date, "details": pl.Utf8, "ingested_at": pl.Datetime("us", "UTC")}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=_SCHEMA) if rows else pl.DataFrame(schema=_SCHEMA)
    return df.unique(subset=["source_id", "symbol", "ex_date", "action_type"], keep="last")


@register_adapter
class PolygonCorporateActionsAdapter(SourceAdapter):
    source_id = "polygon_actions"
    regions = ("US",)
    produces = ("corporate_actions_staging",)
    requires_keys = ("polygon_apikey",)

    def __init__(self, settings: Settings | None = None, client: PolygonClient | None = None, **kw: Any):
        super().__init__(settings, **kw)
        self._client = client

    @property
    def client(self) -> PolygonClient:
        if self._client is None:
            self._client = PolygonClient(self.settings)
        return self._client

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None,
              max_pages: int = 5) -> FetchResult:
        fr = FetchResult(self.source_id, datetime.now(UTC))
        if isinstance(since, str):
            since = date.fromisoformat(since[:10])
        if isinstance(since, datetime):
            since = since.date()
        start = since or (datetime.now(UTC).date() - timedelta(days=730))
        for kind, path, field in (("dividends", "/v3/reference/dividends", "ex_dividend_date"),
                                  ("splits", "/v3/reference/splits", "execution_date")):
            params: dict[str, Any] = {f"{field}.gte": start.isoformat(), "limit": 1000, "order": "asc", "sort": field}
            if entities and len(entities) == 1:
                params["ticker"] = entities[0]
            try:
                res = self.client.paginate(path, params, max_pages=max_pages)
            except Exception as e:  # noqa: BLE001
                fr.entities_failed[kind] = f"{type(e).__name__}: {e}"[:300]
                continue
            fr.raw_paths.append(self.save_raw(kind, f"{kind}_since_{start.isoformat()}", res))
            fr.entities_ok.append(kind)
            fr.meta[f"{kind}_rows"] = len(res)
        fr.meta["polygon_calls"] = self.client.n_calls
        fr.watermark = datetime.now(UTC).date().isoformat()
        return fr

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        frames = []
        for p in fetched.raw_paths:
            data = self.load_raw_json(p)
            frames.append(polygon_dividends_to_rows(data) if p.name.startswith("dividends") else
                          polygon_splits_to_rows(data))
        frames = [f for f in frames if f.height]
        return {"corporate_actions_staging": pl.concat(frames)} if frames else {}


def run(ctx):  # noqa: ANN001 - JobContext
    """Job corporate_actions: Polygon (US) staging refresh + resolution of all staged actions -> corporate_actions."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rows: dict[str, int] = {}
    msg = []
    ok = failed = 0
    ad = PolygonCorporateActionsAdapter(ctx.settings)
    try:
        ad.check_keys()
        since = date.fromisoformat(p["since"]) if p.get("since") else (
            date.fromisoformat(ctx.watermark[:10]) - timedelta(days=10) if ctx.watermark else None)
        fr = ad.fetch(since=since, max_pages=int(p.get("max_pages", 5)))
        for k, v in ad.normalize(fr).items():
            rows[k] = write_table(v, k, settings=ctx.settings)
        ok, failed = len(fr.entities_ok), len(fr.entities_failed)
        msg.append(f"polygon dividends={fr.meta.get('dividends_rows')} splits={fr.meta.get('splits_rows')}")
    except MissingApiKey as e:
        msg.append(f"polygon skipped: {e}")
    rows["corporate_actions"] = finalize_corporate_actions(ctx.settings)
    status = "partial" if failed else "success"
    return JobResult(status=status, rows_written=rows, entities_ok=ok, entities_failed=failed,
                     watermark=datetime.now(UTC).date().isoformat(), message="; ".join(msg))
