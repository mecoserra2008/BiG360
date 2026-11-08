"""yfinance snapshot fundamentals (fallback for any non-US issuer without a primary filing source) -> fundamentals_raw.

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §4.4 (yfinance 1.7.0, verified): ~4 annual and ~5 quarterly
periods of income statement / balance sheet / cash flow, `.info` profile. No filing dates => NOT point-in-time.
PIT: knowledge_date imputed = period_end + lag (US 75/45 d, non-US 120/60 d; ARCHITECTURE §7) capped at the first-seen
date, knowledge_date_estimated=true (=> dq penalty, lowest precedence in every region).
Concepts: yfinance row labels as returned ('Total Revenue', 'Net Income Common Stockholders', ...), taxonomy 'yfinance';
unit = info.financialCurrency (reporting currency). source_entity_id = Yahoo ticker. filing_id = 'yfinance_fund:na'.
Profile (sector/industry/country/employees/currencies) is handed to ref-identity as
data/raw/yfinance_fund/latest/profiles.json (no contract table owns it).
Rate limit: shares the process-wide 'yfinance' token bucket with ingest-market's price adapter.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.ratelimit import get_limiter
from app.ingestion._global_common import (fact_row, frame, impute_knowledge_date, period_start_for, region_of_country,
                                          run_adapter_job, safe_call, seed_universe)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

STATEMENTS = {"income_stmt": ("IS", True), "balance_sheet": ("BS", True), "cashflow": ("CF", True),
              "quarterly_income_stmt": ("IS", False), "quarterly_balance_sheet": ("BS", False),
              "quarterly_cashflow": ("CF", False)}
PROFILE_KEYS = ("longName", "shortName", "sector", "industry", "sectorKey", "industryKey", "country", "exchange",
                "currency", "financialCurrency", "fullTimeEmployees", "sharesOutstanding", "marketCap", "quoteType",
                "isin", "website")
PER_SHARE = ("EPS", "Per Share")
SHARE_ROWS = ("Shares", "Share Issued")


def _frame_to_records(df: Any) -> list[dict[str, Any]]:
    """pandas statement (index=labels, columns=period ends) -> [{label, end, value}]"""
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    for col in df.columns:
        end = col.date() if hasattr(col, "date") else None
        if end is None:
            continue
        for label, v in df[col].items():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f != f:
                continue
            out.append({"label": str(label), "end": end.isoformat(), "value": f})
    return out


def yf_records_to_facts(payload: dict[str, Any], *, first_seen: date, region: str,
                        source_id: str = "yfinance_fund") -> list[dict[str, Any]]:
    ticker = payload["ticker"]
    ccy = (payload.get("profile") or {}).get("financialCurrency") or (payload.get("profile") or {}).get("currency") or "USD"
    out = []
    for key, recs in payload.get("statements", {}).items():
        kind, annual = STATEMENTS[key]
        for r in recs:
            end = date.fromisoformat(r["end"])
            label = r["label"]
            if kind == "BS":
                start = None
            else:
                start = period_start_for(end, 12 if annual else 3)
            unit = "shares" if any(s in label for s in SHARE_ROWS) and "Value" not in label else (
                f"{ccy}/shares" if any(s in label for s in PER_SHARE) else ccy)
            kd = impute_knowledge_date(end, annual, region, first_seen)
            out.append(fact_row(
                source_id=source_id, source_entity_id=ticker, filing_id=f"{source_id}:na", form_type=None,
                taxonomy="yfinance", concept=label, label=f"{kind}:{label}", period_start=start, period_end=end,
                value=r["value"], unit=unit, knowledge_date=kd, fiscal_year=end.year,
                fiscal_period=("FY" if annual else None) if kind != "BS" else None, consolidated=True,
                knowledge_date_estimated=True))
    prof = payload.get("profile") or {}
    for k, unit in (("sharesOutstanding", "shares"), ("fullTimeEmployees", "pure")):
        if isinstance(prof.get(k), (int, float)):
            out.append(fact_row(source_id=source_id, source_entity_id=ticker, filing_id=f"{source_id}:na", form_type=None,
                                taxonomy="yfinance", concept=f"info:{k}", label=f"info:{k}", period_start=None,
                                period_end=first_seen, value=float(prof[k]), unit=unit, knowledge_date=first_seen,
                                fiscal_year=first_seen.year, consolidated=True, knowledge_date_estimated=True))
    return out


@register_adapter
class YfFundamentalsAdapter(SourceAdapter):
    source_id = "yfinance_fund"
    regions = ("EU", "UK", "JP", "KR", "CN", "HK", "LATAM", "APAC", "CA", "OTHER")
    produces = ("fundamentals_raw",)

    def __init__(self, *args: Any, pause_every: int = 100, pause_s: float = 45.0, reuse_days: int = 1, **kw: Any):
        super().__init__(*args, **kw)
        self.limiter = get_limiter("yfinance")
        self.pause_every, self.pause_s = int(pause_every), float(pause_s)
        #: resumable: a ticker landed within the last `reuse_days` days is reused instead of re-fetched (0 = always fetch)
        self.reuse_days = int(reuse_days)

    def _landed(self, tk: str) -> Any:
        if self.reuse_days <= 0:
            return None
        today = datetime.now(UTC).date()
        for k in range(self.reuse_days):
            p = self.settings.raw_dir / self.source_id / "statements" / f"dt={(today - timedelta(days=k)).isoformat()}" / f"{tk.replace('.', '_')}.json.gz"
            if p.exists():
                return p
        return None

    def discover(self) -> pl.DataFrame:
        seed = seed_universe(self.settings)
        rows = [{"source_entity_id": r["yf_ticker"], "yf_ticker": r["yf_ticker"], "name": r["name"],
                 "country": r.get("country"), "ticker": r.get("ticker"), "mic": r.get("mic")}
                for r in seed.iter_rows(named=True) if r.get("yf_ticker")]
        return self.discover_frame(rows, self.source_id)

    def _one(self, tk: str) -> dict[str, Any]:
        import yfinance as yf

        t = yf.Ticker(tk)
        payload: dict[str, Any] = {"ticker": tk, "statements": {}, "profile": {}}
        self.limiter.acquire()
        try:
            info = t.info or {}
        except Exception:  # noqa: BLE001
            info = {}
        payload["profile"] = {k: info.get(k) for k in PROFILE_KEYS if info.get(k) is not None}
        for attr in STATEMENTS:
            self.limiter.acquire()
            payload["statements"][attr] = _frame_to_records(getattr(t, attr))
        if not any(payload["statements"].values()):
            raise ValueError("no statements returned")
        return payload

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        seed = seed_universe(self.settings)
        country = {r["yf_ticker"]: r.get("country") for r in seed.iter_rows(named=True) if r.get("yf_ticker")}
        tickers = list(entities) if entities else [t for t, c in country.items() if c and c != "US"]
        landed, profiles = [], {}
        n_live = 0
        for tk in tickers:
            prev = self._landed(tk)
            if prev is not None:  # resume after an interrupted run (job timeout) without re-hitting Yahoo
                landed.append(prev)
                res.entities_ok.append(tk)
                try:
                    profiles[tk] = self.load_raw_json(prev).get("profile") or {}
                except (OSError, ValueError):
                    pass
                continue
            if n_live and self.pause_every and n_live % self.pause_every == 0:
                time.sleep(self.pause_s)
            n_live += 1
            p = safe_call(lambda tk=tk: self._one(tk), res, tk, self.log)
            if p:
                p["country"] = country.get(tk) or p["profile"].get("country")
                landed.append(self.save_raw("statements", tk.replace(".", "_"), p))
                profiles[tk] = p["profile"]
        if profiles:
            latest = self.settings.raw_dir / self.source_id / "latest" / "profiles.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            old = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {}
            old.update({k: {**v, "_seen": datetime.now(UTC).date().isoformat()} for k, v in profiles.items()})
            latest.write_text(json.dumps(old, ensure_ascii=False, indent=0, default=str), encoding="utf-8")
        res.raw_paths = landed
        res.watermark = datetime.now(UTC).date().isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        facts = []
        today = datetime.now(UTC).date()
        for p in fetched.raw_paths:
            payload = self.load_raw_json(p)
            cc = payload.get("country")
            cc2 = {"United States": "US", "Japan": "JP", "Hong Kong": "HK", "China": "CN"}.get(cc, cc)
            facts += yf_records_to_facts(payload, first_seen=today, region=region_of_country(cc2), source_id=self.source_id)
        return {"fundamentals_raw": frame(facts, "fundamentals_raw")}


def run(ctx: Any) -> Any:
    return run_adapter_job(YfFundamentalsAdapter, ctx)
