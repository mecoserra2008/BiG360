"""Hand-made serving snapshot for BQL tests: the empty snapshot (exact serving schema) + a few securities with
deterministic prices, knowledge-dated ratios / fundamentals (incl. restatements), monthly factor scores and FX."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest

from app.core import config as config_mod
from app.core import schema as schema_mod

AS_OF = date(2026, 3, 31)
START = date(2024, 1, 1)
SECS = [  # sid, issuer, ticker, mic, region, country, sector, currency, type, fx(USD per unit)
    ("SAAA00000001", "IA", "AAA", "XNAS", "US", "US", "TECH", "USD", "common", 1.0),
    ("SBBB00000002", "IB", "BBB", "XNYS", "US", "US", "TECH", "USD", "common", 1.0),
    ("SCCC00000003", "IC", "CCC.DE", "XETR", "EU", "DE", "HLTH", "EUR", "common", 1.10),
    ("SDDD00000004", "ID", "DDD", "XNYS", "US", "US", "HLTH", "USD", "common", 1.0),
    ("IDX:^GSPC", None, "^GSPC", "XOFF", "OTHER", "US", None, "USD", "index", 1.0),
]


def bdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = bdays(START, AS_OF)


def price(sid: str, i: int) -> float:
    return {"SAAA00000001": 100 * 1.001 ** i, "SBBB00000002": 50 + 0.1 * i, "SCCC00000003": 200 - 0.05 * i,
            "SDDD00000004": 10.0 + (i % 7), "IDX:^GSPC": 4000 * 1.0005 ** i}[sid]


def px_at(sid: str, d: date) -> float:
    """Close of the last business day <= d."""
    i = max(k for k, x in enumerate(DAYS) if x <= d)
    return price(sid, i)


@dataclass
class Snap:
    con: Any
    run_id: str


@dataclass
class FakeDb:
    snap: Snap


def _insert(con: Any, table: str, rows: list[dict[str, Any]]) -> None:
    cols = list(rows[0])
    con.executemany(f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" for _ in cols)})',
                    [[r[c] for c in cols] for r in rows])


@pytest.fixture(scope="module")
def bql_env(tmp_path_factory):
    d = tmp_path_factory.mktemp("bql") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    config_mod.get_settings().ensure_dirs()
    from app.api.serving import empty_snapshot
    from app.bql import engine

    con = empty_snapshot()
    con.execute("UPDATE _meta SET value = ? WHERE key = 'as_of'", [AS_OF.isoformat()])
    con.execute("DELETE FROM fx_latest")
    _insert(con, "fx_latest", [{"currency": "USD", "usd_per_unit": 1.0, "date": AS_OF},
                               {"currency": "EUR", "usd_per_unit": 1.10, "date": AS_OF}])
    sec = []
    for sid, iss, tic, mic, reg, cty, sector, ccy, typ, fx in SECS:
        last = price(sid, len(DAYS) - 1)
        sec.append({"security_id": sid, "issuer_id": iss, "ticker": tic, "name": f"{tic} Corp", "mic": mic,
                    "region": reg, "country": cty, "sector_id": sector, "subsector_id": None, "currency": ccy,
                    "security_type": typ, "is_active": True, "is_primary_security": True, "close": last,
                    "close_usd": last * fx, "px_date": AS_OF, "fx_to_usd": fx,
                    "market_cap_usd": {"SAAA00000001": 3e11, "SBBB00000002": 2e11, "SCCC00000003": 1e11,
                                       "SDDD00000004": 5e10}.get(sid), "u_full": typ == "common",
                    "u_mvp_us": reg == "US" and typ == "common",
                    "m_roic": {"IA": 0.26, "IB": 0.10, "IC": 0.30, "ID": 0.05}.get(iss or ""),
                    "chg_1m": None, "pe_ntm": 20.0 if typ == "common" else None})
    _insert(con, "sec", sec)
    _insert(con, "universe", [{"universe_id": "mvp_us", "security_id": s[0]} for s in SECS if s[4] == "US" and s[8] == "common"])
    rows = []
    for sid, *_rest in SECS:
        fx = _rest[-1]
        for i, dd in enumerate(DAYS):
            c = price(sid, i)
            rows.append((sid, dd, c, c * 1.01, c * 0.99, c, c, 1.0, 1.0, 1000.0 + i, _rest[6], c * fx))
    con.executemany("INSERT INTO prices (security_id, date, open, high, low, close, adj_close, adj_factor, split_factor, "
                    "volume, currency, close_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    # ratios (serving `metrics` keeps the latest knowledge version of each period)
    _insert(con, "metrics", [
        # 2024-12 TTM restated: only the restated row (kd 2025-06-01) remains in the snapshot
        {"issuer_id": "IA", "metric_id": "roic", "src": "ratios", "period_kind": "TTM", "fiscal_year": 2024,
         "period_end": date(2024, 12, 31), "value": 0.18, "unit": "pct", "knowledge_date": date(2025, 6, 1)},
        {"issuer_id": "IA", "metric_id": "roic", "src": "ratios", "period_kind": "TTM", "fiscal_year": 2025,
         "period_end": date(2025, 3, 31), "value": 0.22, "unit": "pct", "knowledge_date": date(2025, 5, 1)},
        {"issuer_id": "IA", "metric_id": "roic", "src": "ratios", "period_kind": "FY", "fiscal_year": 2025,
         "period_end": date(2025, 12, 31), "value": 0.25, "unit": "pct", "knowledge_date": date(2026, 2, 20)},
        {"issuer_id": "IA", "metric_id": "roic", "src": "ratios", "period_kind": "TTM", "fiscal_year": 2025,
         "period_end": date(2025, 12, 31), "value": 0.26, "unit": "pct", "knowledge_date": date(2026, 2, 20)},
        {"issuer_id": "IB", "metric_id": "roic", "src": "ratios", "period_kind": "TTM", "fiscal_year": 2025,
         "period_end": date(2025, 12, 31), "value": 0.10, "unit": "pct", "knowledge_date": date(2026, 2, 1)},
        {"issuer_id": "IC", "metric_id": "roic", "src": "ratios", "period_kind": "FY", "fiscal_year": 2025,
         "period_end": date(2025, 12, 31), "value": 0.30, "unit": "pct", "knowledge_date": date(2026, 3, 10)},
        {"issuer_id": "ID", "metric_id": "roic", "src": "ratios", "period_kind": "TTM", "fiscal_year": 2025,
         "period_end": date(2025, 12, 31), "value": 0.05, "unit": "pct", "knowledge_date": date(2026, 2, 1)},
    ])
    # fundamentals keep every knowledge version (same-period restatement replaces from its knowledge date)
    _insert(con, "fund", [
        {"issuer_id": "IA", "item_id": "revenue", "fiscal_year": 2025, "fiscal_period": "Q3", "period_kind": "TTM",
         "period_end": date(2025, 9, 30), "value": 1000.0, "currency": "USD", "value_usd": 1000.0,
         "knowledge_date": date(2025, 11, 1)},
        {"issuer_id": "IA", "item_id": "revenue", "fiscal_year": 2025, "fiscal_period": "Q3", "period_kind": "TTM",
         "period_end": date(2025, 9, 30), "value": 1010.0, "currency": "USD", "value_usd": 1010.0,
         "knowledge_date": date(2025, 12, 1)},
        {"issuer_id": "IA", "item_id": "revenue", "fiscal_year": 2025, "fiscal_period": "FY", "period_kind": "FY",
         "period_end": date(2025, 12, 31), "value": 1100.0, "currency": "USD", "value_usd": 1100.0,
         "knowledge_date": date(2026, 2, 20)},
    ])
    fh = []
    for k, m in enumerate([date(2025, 10, 31), date(2025, 11, 30), date(2025, 12, 31), date(2026, 1, 31),
                           date(2026, 2, 28), date(2026, 3, 31)]):
        for j, (sid, iss, *_r) in enumerate(SECS[:4]):
            fh.append({"as_of_date": m, "security_id": sid, "issuer_id": iss, "factor_id": "quality",
                       "score": 40.0 + 5 * k + 10 * j})
    _insert(con, "factor_hist", fh)
    engine.cache.clear()
    db = FakeDb(Snap(con, "TESTRUN1"))
    yield {"db": db, "con": con}
    con.close()
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def col(res: dict[str, Any], name: str) -> list[Any]:
    i = [c["name"] for c in res["columns"]].index(name)
    return [r[i] for r in res["rows"]]


def by_ticker(res: dict[str, Any], name: str) -> dict[str, Any]:
    t = [c["name"] for c in res["columns"]].index("ticker")
    i = [c["name"] for c in res["columns"]].index(name)
    return {r[t]: r[i] for r in res["rows"]}
