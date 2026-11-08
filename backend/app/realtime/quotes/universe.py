"""What the delayed quotes service polls (refreshed every `refresh_s`, default 10 min).

Groups (a security can be in several; the first group wins for its label):
  watchlist   every security of every watchlist (app_state.watchlist_items)
  thesis      subjects of theses that are not closed (security_id / price_id)
  viewed      ids the UI requested quotes for in the last 30 min (data/state/quotes_interest.json)
  index       main equity / volatility / rate indices (securities of type index)
  fx          FX majors and the cockpit's FX pairs
  future      futures front months (securities of type future, continuous `=F` contracts)
  cockpit     the cross-asset cockpit assets (serving xa_assets: instrument_id + linked ids, ETFs included)

Every id is resolved through the serving snapshot (securities + primary listing): Yahoo symbol (listing yf_ticker,
`IDX:` / `ETF:` ids carry it after the colon), Polygon ticker (US listings), MIC / timezone -> session key. Ids that do
not resolve are skipped and counted (reason "unresolved").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.realtime.quotes.sessions import FX, GLOBEX, market_key

log = get_logger(__name__)

FX_MAJORS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "EURCHF",
             "USDCNY", "USDHKD", "USDKRW", "USDINR", "USDBRL", "USDMXN")
GROUP_ORDER = ("watchlist", "thesis", "viewed", "index", "fx", "future", "cockpit")


@dataclass
class Target:
    security_id: str
    yahoo: str | None
    polygon: str | None = None
    market: str = "XNYS"
    security_type: str | None = None
    currency: str | None = None
    ticker: str | None = None
    name: str | None = None
    groups: list[str] = field(default_factory=list)


@dataclass
class Universe:
    targets: dict[str, Target] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)
    errors: list[str] = field(default_factory=list)

    def by_market(self) -> dict[str, list[Target]]:
        out: dict[str, list[Target]] = {}
        for t in self.targets.values():
            out.setdefault(t.market, []).append(t)
        return out

    def summary(self) -> dict[str, Any]:
        return {"n": len(self.targets), "groups": self.counts, "unresolved": len(self.unresolved),
                "unresolved_sample": self.unresolved[:10], "markets": {k: len(v) for k, v in self.by_market().items()},
                "errors": self.errors[:5], "built_at": self.built_at}


def _yahoo_of(security_id: str, yf: str | None, display: str | None) -> str | None:
    if yf:
        return yf
    if ":" in security_id and security_id.split(":", 1)[0] in ("IDX", "ETF"):
        return security_id.split(":", 1)[1]
    return display


def app_state_ids(settings: Settings | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """{watchlist: [...], thesis: [...]} from app_state (read-only, short open; retried on the DuckDB lock)."""
    from app.core.storage import connect_app_state  # noqa: PLC0415

    out: dict[str, list[str]] = {"watchlist": [], "thesis": []}
    errs: list[str] = []
    try:
        con = connect_app_state(read_only=True, attempts=6, delay=0.5, settings=settings)
    except Exception as e:  # noqa: BLE001 - locked / missing: keep going with the static groups
        return out, [f"app_state unavailable: {type(e).__name__}"]
    try:
        tabs = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        if "watchlist_items" in tabs:
            out["watchlist"] = [r[0] for r in con.execute("SELECT DISTINCT security_id FROM watchlist_items").fetchall()
                                if r[0]]
        if "theses" in tabs:
            rows = con.execute("SELECT security_id, price_id FROM theses WHERE coalesce(status, '') <> 'closed'").fetchall()
            out["thesis"] = list(dict.fromkeys(x for r in rows for x in r if x))
    except Exception as e:  # noqa: BLE001
        errs.append(f"app_state read failed: {type(e).__name__}: {e}"[:200])
    finally:
        con.close()
    return out, errs


def serving_universe(want: dict[str, list[str]], settings: Settings | None = None,
                     include_static: bool = True) -> Universe:
    """Resolve the wanted ids (+ the static groups) against the current serving snapshot."""
    import duckdb  # noqa: PLC0415
    import pyarrow as pa  # noqa: PLC0415

    from app.core.storage import current_serving_db  # noqa: PLC0415

    u = Universe()
    p = current_serving_db(settings)
    if p is None:
        u.errors.append("no serving snapshot yet")
        return u
    con = duckdb.connect(str(p), read_only=True)
    try:
        tabs = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        groups: dict[str, list[str]] = {g: list(v) for g, v in want.items()}
        if include_static:
            groups["index"] = [r[0] for r in con.execute(
                "SELECT security_id FROM securities WHERE security_type = 'index' AND coalesce(is_active, true)").fetchall()]
            groups["future"] = [r[0] for r in con.execute(
                "SELECT security_id FROM securities WHERE security_type = 'future' AND coalesce(is_active, true)").fetchall()]
            groups["fx"] = [f"IDX:{p}=X" for p in FX_MAJORS]
            if "xa_assets" in tabs:
                ck: list[str] = []
                for inst, linked in con.execute("SELECT instrument_id, linked_ids FROM xa_assets").fetchall():
                    ck += [x for x in [inst, *(linked or [])] if x]
                groups["cockpit"] = list(dict.fromkeys(ck))
        ids = list(dict.fromkeys(i for g in GROUP_ORDER for i in groups.get(g, [])))
        if not ids:
            return u
        con.register("_want", pa.table({"security_id": ids}))
        rows = con.execute("""
            SELECT w.security_id, s.security_type, s.trading_currency, s.display_ticker, s.name,
                   l.yf_ticker, l.polygon_ticker, l.mic, l.timezone
            FROM _want w JOIN securities s USING (security_id)
            LEFT JOIN listings l ON l.listing_id = s.primary_listing_id""").fetchall()
        found = {}
        for sid, st, ccy, disp, name, yf, poly, mic, tz in rows:
            yahoo = _yahoo_of(sid, yf, disp)
            if not yahoo:
                continue
            mk = market_key(mic, tz, st)
            found[sid] = Target(security_id=sid, yahoo=yahoo, polygon=poly if mk not in (FX, GLOBEX) else None,
                                market=mk, security_type=st, currency=ccy, ticker=disp, name=name)
        for g in GROUP_ORDER:
            n = 0
            for i in groups.get(g, []):
                t = found.get(i)
                if t is None:
                    continue
                if g not in t.groups:
                    t.groups.append(g)
                n += 1
            u.counts[g] = n
        u.targets = found
        u.unresolved = [i for i in ids if i not in found]
    finally:
        con.close()
    return u


def build_universe(settings: Settings | None = None, interest: list[str] | None = None) -> Universe:
    from app.realtime.quotes.store import read_interest  # noqa: PLC0415

    s = settings or get_settings()
    want, errs = app_state_ids(s)
    want["viewed"] = list(interest if interest is not None else read_interest(s))
    u = serving_universe(want, s)
    u.errors = errs + u.errors
    return u
