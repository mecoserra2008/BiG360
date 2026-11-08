"""Shared fixture builder for the live news tests: a GDELT hot slot store from the recorded events_v2 rows, live news
hour files (items + story assignments) and the events_v2 dictionaries of the tiny test issuer universe."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from app.events_v2 import dicts
from app.events_v2.parse import parse_events, parse_gkg, parse_mentions
from app.news_live import hot
from tests.events_v2.helpers import fixture_bytes, issuer_dict, worker_ctx


def seed_dicts(s) -> None:
    from app.core.storage import scan_table

    dicts.ensure_static(s)
    cur = scan_table("event_labels", s).collect()
    dicts.write_dict(pl.concat([cur, issuer_dict()], how="vertical_relaxed"), s)


def gdelt_frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    arts, _ = parse_gkg(fixture_bytes("gkg"), worker_ctx(), False)
    ev = parse_events(fixture_bytes("export"))
    men = parse_mentions(fixture_bytes("mentions"))
    return arts, ev, men


def seed_gdelt(s, d: date, hours: list[int]) -> dict[str, int]:
    """Spread the recorded rows over the 15-min slots of the given hours of day d (translation kinds: missing)."""
    arts, ev, men = gdelt_frames()
    slots = [datetime(d.year, d.month, d.day, h, 15 * i, tzinfo=UTC) for h in hours for i in range(4)]
    n = len(slots)
    out = {"articles": 0, "events": 0}
    for k, slot in enumerate(slots):
        a = arts.slice(k * arts.height // n, (k + 1) * arts.height // n - k * arts.height // n).with_columns(
            pl.lit(d).alias("date"), pl.lit(slot.hour * 60 + slot.minute, pl.Int16).alias("minute"))
        e = ev.slice(k * ev.height // n, (k + 1) * ev.height // n - k * ev.height // n).with_columns(
            pl.lit(d).alias("date"))
        m = men.slice(k * men.height // n, (k + 1) * men.height // n - k * men.height // n)
        rel = slot + timedelta(minutes=1)
        hot.write_slot_kind(slot, "gkg", a, None, "ok", rel, s)
        hot.write_slot_kind(slot, "export", e, None, "ok", rel, s)
        hot.write_slot_kind(slot, "mentions", m, None, "ok", rel, s)
        for kind in ("translation.export", "translation.mentions", "translation.gkg"):
            hot.write_slot_kind(slot, kind, None, None, "missing", None, s)
        out["articles"] += a.height
        out["events"] += e.height
    return out


def news_rows(d: date, hour: int, n0: int = 0) -> list[dict]:
    ts = datetime(d.year, d.month, d.day, hour, 5, tzinfo=UTC)
    base = {"kind": "news", "tier": 2, "category": "publisher", "snippet": "s", "published_estimated": False,
            "linked_by": None, "security_ids": [], "tickers": [], "sectors": [], "tone": None,
            "sentiment_label": None, "story_sources": 1, "story_first_seen": ts.isoformat(), "salience": 1.0,
            "domain": None, "bus_raw_id": 1}
    rows = [
        {**base, "item_key": f"k{n0 + 1}", "url_hash": 9001 + n0, "source_id": "rss_ft_home", "publisher": "Financial Times",
         "title": "Nvidia shares fall as export curbs bite chip sales", "url": f"https://ft.com/n{n0 + 1}",
         "language": "en", "published_at": (ts - timedelta(seconds=40)).isoformat(), "first_seen_at": ts.isoformat(),
         "issuer_ids": ["INVDA"], "countries": ["US"], "themes": ["trade_tariffs"], "sentiment_score": -0.6,
         "sentiment_model": "ProsusAI/finbert", "story_id": "7001", "story_size": 1, "story_title": "x"},
        {**base, "item_key": f"k{n0 + 2}", "url_hash": 9002 + n0, "source_id": "rss_wsj_markets", "publisher": "WSJ",
         "title": "Nvidia shares fall as export curbs bite chip sales - WSJ", "url": f"https://wsj.com/n{n0 + 2}",
         "language": "en", "published_at": (ts - timedelta(seconds=20)).isoformat(),
         "first_seen_at": (ts + timedelta(seconds=30)).isoformat(), "issuer_ids": ["INVDA"], "countries": ["US"],
         "themes": ["trade_tariffs"], "sentiment_score": -0.4, "sentiment_model": "ProsusAI/finbert",
         "story_id": "7001", "story_size": 2, "story_title": "x"},
        {**base, "item_key": f"k{n0 + 3}", "url_hash": 9003 + n0, "source_id": "rss_valor", "publisher": "Valor Economico",
         "title": "Petrobras anuncia novo plano de investimentos", "url": f"https://valor.globo.com/n{n0 + 3}",
         "language": "pt", "published_at": ts.isoformat(), "first_seen_at": (ts + timedelta(seconds=60)).isoformat(),
         "issuer_ids": [], "countries": ["BR"], "themes": [], "sentiment_score": None, "sentiment_model": None,
         "story_id": "7002", "story_size": 1, "story_title": "y", "tier": 2},
    ]
    return rows


def seed_news(s, d: date, hour: int, n0: int = 0) -> list[dict]:
    rows = news_rows(d, hour, n0)
    hot.write_parquet_atomic(pl.DataFrame(rows, infer_schema_length=None), hot.items_path(d, hour, s))
    asg = [{"url_hash": r["url_hash"], "story_id": int(r["story_id"]), "title": r["title"], "ts": 0.0,
            "source": r["publisher"], "item_key": r["item_key"], "kind": "news"} for r in rows]
    hot.write_parquet_atomic(pl.DataFrame(asg), hot.assign_path(d, hour, s))
    return rows
