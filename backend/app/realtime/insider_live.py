"""Form 4 -> insider-signal rescoring for the affected issuer (R1.3). Consumer group `insider`.

For each new SEC Form 4 / 4/A on `filings` (any issuer the producer kept):
1. fetch the complete submission `<acc>.txt` (1 request, shared `sec_edgar` limiter, SEC User-Agent) and parse it with
   the nightly parser (app.ingestion.sec_form4.split_submission + parse_ownership_xml);
2. rescore: the issuer's open-market history from insider_transactions (last 400 days, same CIK; superseded rows
   dropped) + the new rows -> app.analytics.insider.transaction_features + compute_signals for today (the nightly
   rule-based score; the XGBoost blend needs the nightly model run and is not applied live);
3. publish `signals` (type `insider`, key insider:<accession>, ckey insider:<issuer>) with the parsed transactions
   (code, shares, price, value, role, 10b5-1) and the rescored signal (signal_score, net_buy_value_usd_30d,
   cluster_buy, c_suite_buy, n_buyers_90d ...). The alert consumer turns qualifying buys into insider_buy alerts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer, ts_of
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)
ET = ZoneInfo("America/New_York")
SIGNAL_FIELDS = ("signal_score", "net_buy_value_usd_30d", "net_buy_value_usd_90d", "n_buyers_90d", "n_sellers_90d",
                 "cluster_buy", "c_suite_buy", "opportunistic_buy_value_usd_90d", "opportunistic_sell_value_usd_90d")
TXN_FIELDS = ("txn_id", "insider_name", "role_norm", "transaction_date", "txn_code", "txn_type", "is_open_market",
              "is_derivative", "is_10b5_1", "shares", "price", "value_usd", "shares_owned_after", "holding_change_pct")


def fetch_form4(http: Any, cik: str, acc: str) -> str:
    from app.ingestion.sec_index import ARCHIVES, acc_nodash  # noqa: PLC0415

    return http.get(f"{ARCHIVES}/data/{int(cik)}/{acc_nodash(acc)}/{acc}.txt", cache_ttl=-1).text


def parse_form4(txt: str, acc: str, form_type: str | None) -> list[dict[str, Any]]:
    from app.ingestion.sec_form4 import finish_row, parse_ownership_xml, split_submission  # noqa: PLC0415

    hdr, xml = split_submission(txt)
    if xml is None:
        return []
    acc_dt = None
    if hdr.get("acceptance"):
        acc_dt = datetime.strptime(hdr["acceptance"], "%Y%m%d%H%M%S").replace(tzinfo=ET).astimezone(UTC)
    filed = datetime.strptime(hdr["filed"], "%Y%m%d").date() if hdr.get("filed") else \
        (acc_dt.astimezone(ET).date() if acc_dt else datetime.now(ET).date())
    rows, _meta = parse_ownership_xml(xml, acc, filed, acc_dt, form_type or hdr.get("form"))
    out = []
    for r in rows:
        if "txn_type" not in r or r.get("txn_type") is None:
            r = finish_row(r) or r
        out.append(r)
    return out


def rescore(settings: Any, cik: str, new_rows: list[dict[str, Any]], as_of: date) -> dict[str, Any] | None:
    import polars as pl  # noqa: PLC0415

    from app.analytics.insider import _TXN_COLS, compute_signals, transaction_features  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415

    hist = pl.DataFrame()
    try:
        lf = scan_table("insider_transactions", settings)
        names = lf.collect_schema().names()
        hist = (lf.filter((pl.col("source_id") == "sec_edgar") & (pl.col("source_entity_id") == cik)
                          & (pl.col("knowledge_date") > as_of - timedelta(days=400)))
                .select([c for c in _TXN_COLS if c in names]).collect())
        if "superseded_by_filing_id" in hist.columns:
            hist = hist.filter(pl.col("superseded_by_filing_id").is_null())
    except Exception as e:  # noqa: BLE001
        log.debug("insider history unavailable for %s: %s", cik, e)
    new = pl.DataFrame([{k: r.get(k) for k in _TXN_COLS} for r in new_rows], infer_schema_length=None) \
        if new_rows else pl.DataFrame()
    if hist.height and new.height:
        new = new.select([c for c in hist.columns if c in new.columns]).cast(
            {c: hist.schema[c] for c in hist.columns if c in new.columns}, strict=False)
    df = pl.concat([x for x in (hist, new) if x.height], how="diagonal_relaxed") if (hist.height or new.height) \
        else pl.DataFrame()
    if df.is_empty():
        return None
    df = df.unique("txn_id", keep="last", maintain_order=True)
    om = df.filter(pl.col("txn_type").is_in(["buy", "sell"]) & pl.col("is_open_market").fill_null(False)
                   & ~pl.col("is_derivative").fill_null(False) & (pl.col("shares") > 0))
    if om.is_empty():
        return {"signal_score": 50.0, "net_buy_value_usd_30d": 0.0, "note": "no open-market activity"}
    om = om.with_columns(
        pl.when(pl.col("txn_type") == "buy").then(1).otherwise(-1).cast(pl.Int8).alias("direction"),
        pl.col("role_norm").fill_null("other"),
        pl.coalesce(pl.col("issuer_id") if "issuer_id" in om.columns else pl.lit(None, pl.Utf8),
                    pl.lit(f"sec_edgar:{cik}")).alias("issuer_key"))
    feat = transaction_features(om, None)
    sig = compute_signals(feat, [as_of])
    if sig.is_empty():
        return {"signal_score": 50.0, "net_buy_value_usd_30d": 0.0, "note": "no activity in 90 days"}
    r = sig.row(0, named=True)
    return {k: r.get(k) for k in SIGNAL_FIELDS} | {"n_open_market_90d": int(feat.filter(
        pl.col("knowledge_date") > as_of - timedelta(days=90)).height)}


class InsiderConsumer(Consumer):
    name = "insider"
    group = "insider"
    topics = ("filings",)
    batch = 50
    max_attempts = 4

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.http: Any = None

    def _http(self) -> Any:
        if self.http is None:
            from app.core.http import HttpClient  # noqa: PLC0415

            self.http = HttpClient("sec_edgar", max_retries=2, timeout=30)
        return self.http

    def handle_one(self, ctx: StreamContext, msg: Message) -> list[Envelope]:
        p = msg.payload
        if p.get("source_id") != "sec_edgar" or (p.get("form_type") or "").upper() not in ("4", "4/A"):
            return []
        cik, acc = p.get("source_entity_id"), p.get("accession")
        txt = fetch_form4(self._http(), cik, acc)
        rows = parse_form4(txt, acc, p.get("form_type"))
        today = ctx.clock().astimezone(ET).date()
        for r in rows:
            r.setdefault("issuer_id", p.get("issuer_id"))
            r.setdefault("security_id", p.get("security_id"))
            r["source_entity_id"] = cik
        sig = None
        try:
            sig = rescore(ctx.settings, cik, rows, today)
        except Exception as e:  # noqa: BLE001 - rescoring is best effort; the transactions still go out
            log.warning("insider rescoring failed for %s: %s", cik, e)
        txns = [{k: (str(r[k]) if isinstance(r.get(k), (date, datetime)) else r.get(k)) for k in TXN_FIELDS}
                for r in rows]
        buys = [t for t in txns if t.get("txn_type") == "buy" and t.get("is_open_market")]
        sells = [t for t in txns if t.get("txn_type") == "sell" and t.get("is_open_market")]
        payload = {
            "kind": "insider", "filing_id": p.get("filing_id"), "accession": acc, "form_type": p.get("form_type"),
            "issuer_id": p.get("issuer_id"), "security_id": p.get("security_id"), "ticker": p.get("ticker"),
            "company": p.get("company"), "source_entity_id": cik, "reporting_owner": p.get("reporting_owner"),
            "knowledge_ts": p.get("knowledge_ts"), "as_of_date": str(today), "transactions": txns,
            "open_market_buy_usd": sum((t.get("value_usd") or 0) for t in buys),
            "open_market_sell_usd": sum((t.get("value_usd") or 0) for t in sells),
            "has_open_market_buy": bool(buys), "signal": sig, "url": p.get("url"),
            "label": "Rule-based insider score, rescored live (XGBoost blend nightly only)",
        }
        ck = f"insider:{p.get('issuer_id') or cik}"
        return [Envelope("signals", f"insider:{acc}", payload, type="insider", ts_source=ts_of(msg), ckey=ck)]
