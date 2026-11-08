"""Hot store -> Parquet lake (R1.5). Consumer group `lake`, run hourly by the supervisor (and `cli realtime compact`).

For each public topic, all messages after the `lake` offset are read in pages, mapped to the live_* contract
(filings -> live_filings, news -> live_news, events -> live_events, macro -> live_macro, signals -> live_signals;
alerts are already durable in app_state.alert_events), rows whose primary key already exists are dropped (idempotent:
the first-seen row wins, so knowledge_ts stays the FIRST time the live path saw the item), the rest is appended
(daily partitions) and the offset is committed. Retention of the hot store never deletes messages the `lake` group
has not committed (bus.retention protect_groups).

`reconcile()` compares live keys with the nightly tables by natural key (filings_index.filing_id,
news_items.item_id, macro_release_events (release_event_id, series_id) with status released) for items older than
`min_age_h` (the nightly batch had a chance to see them): agreement = share of live keys also present nightly; plus
the median lead of the live first-seen time over the nightly knowledge time where both exist.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.realtime.bus import Message, SqliteBus

log = get_logger(__name__)

GROUP = "lake"


def _dt(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _date(s: Any) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _common(m: Message, now: datetime) -> dict[str, Any]:
    p = m.payload
    kts = _dt(p.get("first_seen_at")) or m.ingest_dt
    return {"date": kts.date(), "source_ts": _dt(m.ts_source), "knowledge_ts": kts, "bus_id": m.id,
            "payload": orjson.dumps(p, default=str).decode(), "compacted_at": now}


def _lst(x: Any) -> list[str] | None:
    if x is None:
        return None
    return [str(v) for v in x if v is not None]


def row_filing(m: Message, now: datetime) -> dict[str, Any]:
    p = m.payload
    return {"filing_id": p.get("filing_id") or m.key, "source_id": p.get("source_id") or "unknown",
            "source_entity_id": p.get("source_entity_id"), "issuer_id": p.get("issuer_id"),
            "security_id": p.get("security_id"), "ticker": p.get("ticker"), "company": p.get("company"),
            "form_type": p.get("form_type"), "form_class": p.get("form_class"), "title": p.get("title"),
            "items": _lst(p.get("items")), "url": p.get("url"), "country": p.get("country"), **_common(m, now)}


def row_news(m: Message, now: datetime) -> dict[str, Any]:
    p = m.payload
    return {"item_id": p.get("item_id") or m.key, "source_id": p.get("source_id") or "unknown",
            "category": p.get("category"), "publisher": p.get("publisher"), "title": p.get("title") or "",
            "url": p.get("url"), "language": p.get("language"), "issuer_ids": _lst(p.get("issuer_ids")),
            "tickers": _lst(p.get("tickers")), "countries": _lst(p.get("countries")), "themes": _lst(p.get("themes")),
            "story_id": p.get("story_id"), "sentiment_label": p.get("sentiment_label"),
            "sentiment_score": p.get("sentiment_score"), **_common(m, now)}


def row_event(m: Message, now: datetime) -> dict[str, Any]:
    p = m.payload
    return {"event_key": m.key, "kind": p.get("kind") or m.type or "article", "slot": p.get("slot"),
            "title": p.get("title"), "url": p.get("url"), "domain": p.get("domain"), "language": p.get("language"),
            "tone": p.get("tone"), "themes": _lst(p.get("themes")), "countries": _lst(p.get("countries")),
            "issuer_ids": _lst(p.get("issuer_ids")), "story_id": p.get("story_id"),
            "sentiment_score": p.get("sentiment_score"), **_common(m, now)}


def row_macro(m: Message, now: datetime) -> dict[str, Any]:
    p = m.payload
    return {"release_event_id": p.get("release_event_id"), "series_id": p.get("series_id"), "country": p.get("country"),
            "release_name": p.get("release_name"), "period": _date(p.get("period")), "measure": p.get("measure"),
            "actual": p.get("actual"), "prior": p.get("prior"), "expectation": p.get("expectation"),
            "surprise_z": p.get("surprise_z"), "signed_z": p.get("signed_z"), "importance": p.get("importance"),
            "capture_delay_s": p.get("capture_delay_s"), **_common(m, now)}


def row_signal(m: Message, now: datetime) -> dict[str, Any]:
    p = m.payload
    sig = p.get("signal") or {}
    score = sig.get("signal_score") if p.get("kind") == "insider" else p.get("p_change")
    return {"signal_key": m.key, "kind": p.get("kind") or m.type, "issuer_id": p.get("issuer_id"),
            "security_id": p.get("security_id"), "series": p.get("series"),
            "score": float(score) if score is not None else None, **_common(m, now)}


TOPIC_TABLES: dict[str, tuple[str, Callable[[Message, datetime], dict[str, Any]]]] = {
    "filings": ("live_filings", row_filing), "news": ("live_news", row_news), "events": ("live_events", row_event),
    "macro": ("live_macro", row_macro), "signals": ("live_signals", row_signal),
}


def _frame(rows: list[dict[str, Any]], table: str) -> pl.DataFrame:
    from app.core.schema import get_contract, polars_dtype  # noqa: PLC0415

    c = get_contract(table)

    schema = {col.name: polars_dtype(col.type) for col in c.columns}
    return pl.DataFrame([{k: r.get(k) for k in schema} for r in rows], schema=schema, strict=False)


def _existing_keys(table: str, pk: list[str], df: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    from app.core.storage import scan_table, table_dir  # noqa: PLC0415

    if not table_dir(table, settings).exists():
        return df.head(0).select(pk)
    try:
        lf = scan_table(table, settings)
        days = df.get_column("date").unique().to_list()
        lo = min(days) - timedelta(days=7)    # the same key re-seen within a week; older dups are filtered by bus dedupe
        cond = pl.col("date") >= lo
        if len(pk) == 1:
            cond = cond & pl.col(pk[0]).is_in(df.get_column(pk[0]).unique().implode())
        return lf.filter(cond).select(pk).collect()
    except Exception as e:  # noqa: BLE001 - empty / new table
        log.debug("existing keys %s: %s", table, e)
        return df.head(0).select(pk)


def compact(bus: SqliteBus, settings: Settings | None = None, page: int = 5000, max_pages: int = 200,
            now: datetime | None = None) -> dict[str, Any]:
    from app.core.schema import get_contract  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    rep: dict[str, Any] = {}
    for topic, (table, fn) in TOPIC_TABLES.items():
        pk = list(get_contract(table).primary_key)
        n_in = n_written = 0
        for _ in range(max_pages):
            msgs = bus.poll(GROUP, [topic], page)
            if not msgs:
                break
            ts = now or datetime.now(UTC)
            rows = []
            for m in msgs:
                try:
                    rows.append(fn(m, ts))
                except Exception as e:  # noqa: BLE001 - a malformed message must not block the table
                    log.warning("compact %s: message %s skipped: %s", topic, m.id, e)
            df = _frame(rows, table) if rows else None
            if df is not None and df.height:
                df = df.filter(pl.all_horizontal([pl.col(k).is_not_null() for k in pk]))
                df = df.sort("bus_id").unique(pk, keep="first", maintain_order=True)
                ex = _existing_keys(table, pk, df, s)
                if ex.height:
                    df = df.join(ex.unique(), on=pk, how="anti")
                if df.height:
                    write_table(df, table, mode="append", settings=s)
                n_written += df.height
            n_in += len(msgs)
            bus.commit(GROUP, topic, msgs[-1].id)
            if len(msgs) < page:
                break
        rep[table] = {"messages": n_in, "rows_written": n_written}
    release_unlaked(bus)
    return rep


#: topics the lake compactor never stores (raw inputs of enrich, durable alerts, news desk output kept in the news
#: hot store / intraday DuckDB): the `lake` group advances over them so bus retention can age them out (a group that
#: never committed a topic protects all of it)
RAW_TOPICS_BY_GROUP = {"raw.filings": "enrich", "raw.news": "enrich", "raw.events": "enrich", "raw.macro": "enrich"}
DURABLE_ELSEWHERE = ("alerts", "news.item", "news.story", "quotes.tick", "quotes.session")


def release_unlaked(bus: SqliteBus) -> dict[str, int]:
    """Advance the lake offset of raw.* to what `enrich` has processed and of alerts / news.* to the head."""
    out: dict[str, int] = {}
    try:
        offs = bus.offsets()
        head = bus.head_id()
    except Exception as e:  # noqa: BLE001
        log.debug("release_unlaked: %s", e)
        return out
    for t, g in RAW_TOPICS_BY_GROUP.items():
        o = (offs.get(g) or {}).get(t)
        if o:
            bus.commit(GROUP, t, o)
            out[t] = o
    for t in DURABLE_ELSEWHERE:
        if head:
            bus.commit(GROUP, t, head)
            out[t] = head
    return out


def reconcile(settings: Settings | None = None, min_age_h: float = 36, lookback_days: int = 14) -> dict[str, Any]:
    """Agreement of the live path with the nightly batch by natural keys (see module docstring)."""
    from app.core.storage import scan_table, table_dir  # noqa: PLC0415

    s = settings or get_settings()
    now = datetime.now(UTC)
    hi, lo = now - timedelta(hours=min_age_h), now - timedelta(days=lookback_days)
    out: dict[str, Any] = {}
    pairs = (("live_filings", "filings_index", ["filing_id"], "knowledge_ts"),
             ("live_news", "news_items", ["item_id"], "published_at"),
             ("live_macro", "macro_release_events", ["release_event_id", "series_id"], None))
    for live, nightly, pk, nts in pairs:
        if not table_dir(live, s).exists():
            out[live] = {"live_keys": 0}
            continue
        try:
            lv = (scan_table(live, s).filter((pl.col("knowledge_ts") <= hi) & (pl.col("knowledge_ts") >= lo))
                  .select([*pk, "knowledge_ts"]).collect())
            if lv.is_empty():
                out[live] = {"live_keys": 0}
                continue
            nl = scan_table(nightly, s)
            cols = nl.collect_schema().names()
            sel = [*pk] + ([nts] if nts and nts in cols else [])
            filt = pl.col(pk[0]).is_in(lv.get_column(pk[0]).unique().implode())
            if nightly == "macro_release_events":
                filt = filt & (pl.col("status") == "released")
            nv = nl.filter(filt).select(sel).collect().unique(pk)
            j = lv.join(nv, on=pk, how="left", coalesce=True, suffix="_n")
            found = j.filter(pl.col(nts).is_not_null()) if nts and nts in cols else j.head(0)
            n_found = lv.join(nv.select(pk), on=pk, how="semi").height
            rec: dict[str, Any] = {"live_keys": lv.height, "in_nightly": n_found,
                                   "agreement": round(n_found / lv.height, 4)}
            if nts and nts in cols and found.height:
                lead = (found.get_column(nts).cast(pl.Datetime("us", "UTC")) - found.get_column("knowledge_ts")) \
                    .dt.total_seconds()
                lead = lead.drop_nulls()
                if lead.len():
                    rec["median_live_lead_s"] = float(lead.median())
            out[live] = rec
        except Exception as e:  # noqa: BLE001
            out[live] = {"error": f"{type(e).__name__}: {e}"[:200]}
    return out
