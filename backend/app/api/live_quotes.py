"""Read side of the delayed quotes service + live feed health (workstream B; used by app.api.routers.live).

* quotes(ids, db)   last DELAYED quotes from data/realtime/quotes.sqlite (read-only), resolved from security ids,
                    tickers (display_ticker) or bare index / FX / futures symbols (^GSPC -> IDX:^GSPC), with the
                    market state (open / closed + reason + next open) and a stale flag; records the ids as "viewed" so
                    the quotes service polls them (data/state/quotes_interest.json, throttled)
* quotes_summary()  the service block for /live/status and the header LIVE strip
* feeds()           per-feed health for Data Health > Live feeds: delayed quotes (Yahoo / Polygon), EDGAR filings,
                    official macro releases, news (RSS / Polygon / GDELT), exchange disclosures - last poll, latency,
                    items / hour, errors, status + reason
Never raises: a missing store / status file gives data_status pending with a reason.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.logsetup import get_logger

log = get_logger("app.api.live_quotes")

#: header ticker (LIVE strip): main indices, rates / vol, FX, commodities, crypto
HEADER_SET = ("IDX:^GSPC", "IDX:^NDX", "IDX:^DJI", "IDX:^STOXX50E", "IDX:^GDAXI", "IDX:^FTSE", "IDX:^N225", "IDX:^HSI",
              "IDX:^VIX", "IDX:^TNX", "IDX:EURUSD=X", "IDX:USDJPY=X", "IDX:GBPUSD=X", "IDX:GC=F", "IDX:CL=F",
              "IDX:BTC=F")
LABEL = "Delayed quote (not for trading; personal use)"
STALE_OPEN_S = 15 * 60          # open market, no fresh fetch for 15 min -> stale
HEARTBEAT_DEAD_S = 90

_store_lock = threading.Lock()
_store: Any = None
_interest_last: dict[str, float] = {}
_interest_lock = threading.Lock()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ro_store() -> Any:
    global _store  # noqa: PLW0603
    with _store_lock:
        if _store is None:
            from app.realtime.quotes.store import QuoteStore, default_path  # noqa: PLC0415

            p = default_path(get_settings())
            if not p.exists():
                return None
            _store = QuoteStore(p, read_only=True)
        return _store


def reset() -> None:
    """Tests: forget the cached store handle."""
    global _store  # noqa: PLW0603
    with _store_lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:  # noqa: BLE001
                pass
        _store = None
    _interest_last.clear()


def quotes_service_status() -> dict[str, Any]:
    from app.ops.supervisor import live_quotes_enabled  # noqa: PLC0415
    from app.realtime.quotes.service import read_status  # noqa: PLC0415

    s = get_settings()
    st = read_status(s) or {}
    hb = st.get("heartbeat_at")
    age = (datetime.now(UTC) - datetime.fromisoformat(hb.replace("Z", "+00:00"))).total_seconds() if hb else None
    running = bool(st.get("alive")) and age is not None and age < HEARTBEAT_DEAD_S
    enabled = live_quotes_enabled(s)
    yf = (st.get("feeds") or {}).get("yahoo") or {}
    if not enabled:
        state, reason = "off", "switched off (tray: Live quotes on/off)"
    elif not st:
        state, reason = "not_started", "the quotes service has not run yet (starts with the app after a restart)"
    elif not running:
        state, reason = "stopped", f"no heartbeat since {hb}" if hb else "stopped"
    elif yf.get("status") == "backoff":
        state, reason = "source_down", yf.get("reason") or yf.get("last_error")
    else:
        state, reason = st.get("state") or "ok", st.get("reason")
    return {"running": running, "enabled": enabled, "state": state, "reason": reason, "heartbeat_at": hb,
            "heartbeat_age_s": round(age, 1) if age is not None else None, "pid": st.get("pid"),
            "rss_mb": st.get("rss_mb"), "markets_open": st.get("markets_open"),
            "n_securities": (st.get("universe") or {}).get("n"), "last_fetch_at": yf.get("last_ok_at"),
            "delayed": True, "_raw": st}


def quotes_summary() -> dict[str, Any]:
    try:
        st = quotes_service_status()
    except Exception as e:  # noqa: BLE001
        return {"running": False, "enabled": True, "state": "unknown", "reason": f"{type(e).__name__}", "delayed": True}
    st.pop("_raw", None)
    return st


def _record(ids: list[str]) -> None:
    now = time.time()
    with _interest_lock:
        fresh = [i for i in ids if now - _interest_last.get(i, 0) > 60]
        for i in fresh:
            _interest_last[i] = now
    if fresh:
        try:
            from app.realtime.quotes.store import record_interest  # noqa: PLC0415

            record_interest(fresh, get_settings(), now)
        except Exception as e:  # noqa: BLE001
            log.debug("quote interest not recorded: %s", e)


def resolve_ids(raw: list[str], db: Any) -> dict[str, str]:
    """requested id -> security_id (security ids pass through; tickers / bare symbols are looked up)."""
    out: dict[str, str] = {}
    need: list[str] = []
    for r in raw:
        if r.startswith(("IDX:", "ETF:")):
            out[r] = r
        else:
            need.append(r)
    if need and db is not None and not getattr(getattr(db, "snap", None), "is_empty", True):
        try:
            ph = ",".join("?" * len(need))
            rows = db.all(f"SELECT security_id, display_ticker FROM securities WHERE security_id IN ({ph}) "
                          f"OR display_ticker IN ({ph}) OR security_id IN ({ph})",
                          [*need, *need, *[f"IDX:{x}" for x in need]])
            by_sid = {r["security_id"]: r["security_id"] for r in rows}
            by_tk: dict[str, str] = {}
            for r in rows:
                by_tk.setdefault(r["display_ticker"], r["security_id"])
            for r in need:
                sid = by_sid.get(r) or by_sid.get(f"IDX:{r}") or by_tk.get(r)
                if sid:
                    out[r] = sid
        except Exception as e:  # noqa: BLE001
            log.debug("quote id resolution failed: %s", e)
    for r in need:   # bare index / FX / futures symbols are IDX: securities
        out.setdefault(r, f"IDX:{r}" if r.startswith("^") or r.endswith(("=X", "=F")) else r)
    return out


def quote_item(req_id: str, r: dict[str, Any], markets: dict[str, dict[str, Any]], now: float) -> dict[str, Any]:
    mk = markets.get(r.get("market") or "") or {}
    is_open = bool(mk.get("open"))
    fetched = r.get("fetched_at")
    stale = bool(is_open and fetched is not None and now - float(fetched) > STALE_OPEN_S)
    return {
        "id": req_id, "security_id": r["security_id"], "symbol": r.get("symbol"), "price": r.get("price"),
        "change": r.get("change"), "change_pct": r.get("change_pct"), "prev_close": r.get("prev_close"),
        "currency": r.get("currency"), "market": r.get("market"), "market_state": r.get("market_state"),
        "market_open": is_open, "market_reason": mk.get("reason"), "next_open_at": mk.get("opens_at"),
        "quote_ts": _iso(r.get("quote_ts")), "fetched_at": _iso(fetched), "delay_min": r.get("delay_min"),
        "source": r.get("source"), "day_high": r.get("day_high"), "day_low": r.get("day_low"),
        "volume": r.get("volume"), "stale": stale, "delayed": True,
    }


def quotes(ids: list[str] | None, set_name: str | None, db: Any, record: bool = True) -> dict[str, Any]:
    want = list(dict.fromkeys([*(ids or []), *(HEADER_SET if set_name == "header" else ())]))[:300]
    svc = quotes_summary()
    base = {"generated_at": _iso(time.time()), "delayed": True, "label": LABEL, "service": svc}
    store = _ro_store()
    if store is None:
        return {**base, "data_status": "pending", "reason": svc.get("reason") or "no quotes stored yet", "items": [],
                "missing": want, "markets": []}
    res = resolve_ids(want, db)
    try:
        rows = store.last(list(set(res.values())))
        markets = store.markets()
    except Exception as e:  # noqa: BLE001 - store being created / locked
        log.debug("quotes store read failed: %s", e)
        return {**base, "data_status": "pending", "reason": "quotes store unavailable", "items": [], "missing": want,
                "markets": []}
    now = time.time()
    items, missing = [], []
    for r in want:
        sid = res.get(r, r)
        q = rows.get(sid)
        if q is None:
            missing.append(r)
        else:
            items.append(quote_item(r, q, markets, now))
    if record and ids:   # the service resolves them against the snapshot (unknown ids are ignored there)
        _record([res[i] for i in ids if res.get(i)])
    return {**base, "data_status": "ok" if items else "pending",
            "reason": None if items else (svc.get("reason") or "not polled yet"), "items": items, "missing": missing,
            "markets": sorted(markets.values(), key=lambda m: m["market"])}


# ------------------------------------------------------------------------------------------------ feeds
_GROUP = {"edgar": ("filings", "SEC EDGAR latest filings (Atom)"), "fca_nsm": ("filings", "FCA National Storage Mechanism"),
          "tdnet": ("filings", "TDnet (JPX)"), "hkex": ("filings", "HKEXnews"), "dart": ("filings", "OpenDART (FSS)"),
          "rss_official": ("news", "Official RSS / Atom feeds"), "polygon_news": ("news", "Polygon news"),
          "gdelt": ("news", "GDELT 15-minute files"), "macro_capture": ("macro", "Official macro releases (calendar)")}


def feeds() -> dict[str, Any]:
    from app.realtime.supervisor import read_status  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    rt = read_status() or {}
    hb = rt.get("heartbeat_at")
    rt_age = (datetime.now(UTC) - datetime.fromisoformat(hb)).total_seconds() if hb else None
    rt_alive = bool(rt.get("alive")) and rt_age is not None and rt_age < 60
    for name, x in (rt.get("streams") or {}).items():
        grp, label = _GROUP.get(name, ("other", name))
        st = x.get("status") if rt_alive else ("disabled" if x.get("status") == "disabled" else "stopped")
        reason = x.get("detail") if st == "disabled" else (x.get("last_error") if st == "backoff" else
                                                            (None if rt_alive else "realtime service not running"))
        out.append({"feed": name, "group": grp, "label": label, "service": "realtime", "status": st, "reason": reason,
                    "detail": x.get("detail"), "last_poll_at": x.get("last_poll_at"), "last_ok_at": x.get("last_ok_at"),
                    "next_poll_at": x.get("next_poll_at"), "items_1h": x.get("events_1h", 0),
                    "items_total": x.get("events_total", 0), "latency_s_p50": x.get("lag_s_p50"),
                    "latency_s_p95": x.get("lag_s_p95"), "fetch_ms_p50": None, "errors": x.get("errors", 0),
                    "consecutive_errors": x.get("consecutive_errors", 0), "last_error": x.get("last_error"),
                    "last_error_at": x.get("last_error_at"), "requests_1h": None})
    mc = (rt.get("metrics") or {}).get("macro_capture") or {}
    try:
        q = quotes_service_status()
    except Exception as e:  # noqa: BLE001
        q = {"running": False, "enabled": True, "state": "unknown", "reason": type(e).__name__, "_raw": {}}
    raw = q.pop("_raw", {}) or {}
    for name, f in (raw.get("feeds") or {}).items():
        st = f.get("status") if q["running"] else ("disabled" if not q["enabled"] else "stopped")
        reason = f.get("reason") if q["running"] else q.get("reason")
        if q["running"] and st in ("ok", "idle") and raw.get("state") == "idle":
            st, reason = "idle", raw.get("reason") or reason
        out.append({"feed": f"quotes_{name}", "group": "quotes",
                    "label": "Delayed quotes - Yahoo (batched)" if name == "yahoo" else "Delayed quotes - Polygon snapshot",
                    "service": "quotes", "status": st, "reason": reason, "detail": None,
                    "last_poll_at": f.get("last_poll_at"), "last_ok_at": f.get("last_ok_at"),
                    "next_poll_at": None, "items_1h": f.get("items_1h", 0), "items_total": None,
                    "latency_s_p50": f.get("quote_age_s_p50"), "latency_s_p95": f.get("quote_age_s_p95"),
                    "fetch_ms_p50": f.get("latency_ms_p50"), "errors": f.get("errors", 0),
                    "consecutive_errors": f.get("consecutive_errors", 0), "last_error": f.get("last_error"),
                    "last_error_at": f.get("last_error_at"), "requests_1h": f.get("requests_1h")})
    if not raw.get("feeds"):
        out.append({"feed": "quotes_yahoo", "group": "quotes", "label": "Delayed quotes - Yahoo (batched)",
                    "service": "quotes", "status": "disabled" if not q["enabled"] else "stopped",
                    "reason": q.get("reason"), "detail": None, "last_poll_at": None, "last_ok_at": None,
                    "next_poll_at": None, "items_1h": 0, "items_total": None, "latency_s_p50": None,
                    "latency_s_p95": None, "fetch_ms_p50": None, "errors": 0, "consecutive_errors": 0,
                    "last_error": None, "last_error_at": None, "requests_1h": None})
    markets = [{"market": k, **{kk: v.get(kk) for kk in ("open", "reason", "reason_text", "opens_at", "closes_at",
                                                         "n_targets", "every_s", "next_poll_at")}}
               for k, v in sorted((raw.get("markets") or {}).items())]
    order = {"quotes": 0, "filings": 1, "macro": 2, "news": 3, "other": 4}
    out.sort(key=lambda r: (order.get(r["group"], 9), r["feed"]))
    news_ok = [f for f in out if f["group"] == "news" and f["status"] in ("ok", "idle")]
    return {
        "data_status": "ok" if (rt or raw) else "pending", "generated_at": _iso(time.time()), "feeds": out,
        "markets": markets, "quotes": {k: v for k, v in q.items()},
        "news": {"running": rt_alive, "state": "ok" if rt_alive and news_ok else ("stopped" if not rt_alive else "degraded"),
                 "reason": None if rt_alive else "realtime service not running", "heartbeat_at": hb,
                 "feeds_ok": len(news_ok), "feeds": sum(1 for f in out if f["group"] == "news")},
        "macro_next": mc.get("next") or [],
        "universe": raw.get("universe"),
    }
