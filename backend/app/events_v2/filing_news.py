"""N1 official corporate news -> story layer: SEC 8-K items back to 2001 (+ TDnet / HKEX announcements in the lake).

Job `news_filings` (nightly incremental; the gap-filler campaign `filing_news_history` runs it once with
backfill=true). Steps:
  1. SEC: EDGAR submissions history per CIK of the issuer universe (app.earnings.sec_history: landed raw JSON +
     immutable older pages from the HTTP cache, fetched through the shared SEC limiter when missing) -> every 8-K
     (amendments excluded) since `since` (backfill: 2001-01-01; nightly: last 10 days, recent filers refreshed);
     filings with only Item 9.01 (exhibits) are skipped. One story per filing:
       title     "<issuer> 8-K: <item label> (<item>)[; <second item>]"
       theme     by item (2.02 / 2.06 / 4.02 earnings, 1.01 / 2.01 / 5.01 M&A, 1.03 / 2.04 bankruptcy-default,
                 2.03 / 3.02 / 3.03 / 3.01 capital markets, 1.05 cyber, 5.02 / 5.07 / 7.01 / 8.01 none) + headline rules
       sentiment item polarity where defensible (negative: 1.03, 2.04, 2.06, 3.01, 4.02, 1.05), else 0 (neutral);
                 sentiment_model "rule:sec_8k_items" (no model)
     Pre-August-2004 8-K items (1-12 numbering) are mapped too (3 bankruptcy, 2 acquisition, 12 results, ...).
  2. Exchanges (filings_index, titled rows only): TDnet (TANSHIN / FORECAST_REVISION -> earnings, BUYBACK -> capital
     markets, Japanese keyword rules for M&A) and HKEXnews (ANNOUNCEMENT / RESULTS / CIRCULAR; English headline
     rules), source `exchange_ann`. CVM / FCA NSM rows carry no headline (periodic report metadata only) - skipped.
  3. filing_news upsert (one row per filing) and the story layer merge of GDELT-ready days (app.events_v2.hist_merge;
     not-ready days stay story_layer=pending_gdelt and are merged by a later run).
  4. Repair: pending filing / CC-NEWS days whose GDELT day is now done, and days whose rows vanished after a GDELT
     re-write (hist_merge.repair_ccnews + repair_filings).
params: backfill (false), since (YYYY-MM-DD), max_requests (SEC requests, 600 nightly / 4000 backfill),
        sources ([sec, exchanges]), repair (true).
State: data/state/news_filings.json (per-source counts, backfill status, merged days).
"""

from __future__ import annotations

import gc
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_v2 import dicts
from app.events_v2 import hist_merge as H
from app.events_v2 import taxonomy as tx
from app.events_v2.ccnews_parse import url_hash
from app.events_v2.parse import hash64
from app.events_v2.stories import STORY_SCHEMA, salience_expr, simhash_many, hash_title

log = get_logger(__name__)

T = tx.THEME_BIT
# item -> (label, themes, polarity x100 or None)
ITEMS_8K: dict[str, tuple[str, tuple[str, ...], int | None]] = {
    "1.01": ("Entry into a material definitive agreement", ("m_and_a",), None),
    "1.02": ("Termination of a material definitive agreement", (), None),
    "1.03": ("Bankruptcy or receivership", ("bankruptcy_default",), -60),
    "1.04": ("Mine safety - shutdowns and patterns of violations", ("recall_safety",), None),
    "1.05": ("Material cybersecurity incident", ("cyber",), -40),
    "2.01": ("Completion of acquisition or disposition of assets", ("m_and_a",), None),
    "2.02": ("Results of operations and financial condition", ("earnings",), None),
    "2.03": ("Creation of a direct financial obligation", ("capital_markets",), None),
    "2.04": ("Triggering events that accelerate a financial obligation", ("bankruptcy_default",), -50),
    "2.05": ("Costs associated with exit or disposal activities", (), None),
    "2.06": ("Material impairments", ("earnings",), -40),
    "3.01": ("Notice of delisting or failure to satisfy a listing rule", ("capital_markets",), -40),
    "3.02": ("Unregistered sales of equity securities", ("capital_markets",), None),
    "3.03": ("Material modification to rights of security holders", ("capital_markets",), None),
    "4.01": ("Changes in registrant's certifying accountant", (), None),
    "4.02": ("Non-reliance on previously issued financial statements", ("earnings",), -60),
    "5.01": ("Changes in control of registrant", ("m_and_a",), None),
    "5.02": ("Departure / election of directors or officers", (), None),
    "5.03": ("Amendments to articles or bylaws; change in fiscal year", (), None),
    "5.04": ("Temporary suspension of trading under employee benefit plans", (), None),
    "5.05": ("Amendments to the code of ethics", (), None),
    "5.06": ("Change in shell company status", (), None),
    "5.07": ("Submission of matters to a vote of security holders", (), None),
    "5.08": ("Shareholder director nominations", (), None),
    "6.01": ("ABS informational and computational material", (), None),
    "6.02": ("Change of servicer or trustee", (), None),
    "6.03": ("Change in credit enhancement", (), None),
    "6.04": ("Failure to make a required distribution", ("bankruptcy_default",), None),
    "6.05": ("Securities Act updating disclosure", (), None),
    "7.01": ("Regulation FD disclosure", (), None),
    "8.01": ("Other events", (), None),
    "9.01": ("Financial statements and exhibits", (), None),
    # pre-2004-08-23 numbering
    "1": ("Changes in control of registrant", ("m_and_a",), None),
    "2": ("Acquisition or disposition of assets", ("m_and_a",), None),
    "3": ("Bankruptcy or receivership", ("bankruptcy_default",), -60),
    "4": ("Changes in registrant's certifying accountant", (), None),
    "5": ("Other events", (), None),
    "6": ("Resignations of registrant's directors", (), None),
    "7": ("Financial statements and exhibits", (), None),
    "8": ("Change in fiscal year", (), None),
    "9": ("Regulation FD disclosure", (), None),
    "10": ("Amendments to the code of ethics", (), None),
    "11": ("Temporary suspension of trading under employee benefit plans", (), None),
    "12": ("Results of operations and financial condition", ("earnings",), None),
}
EXHIBIT_ONLY = {"9.01", "7"}
RULE_8K = "rule:sec_8k_items"
RULE_NEUTRAL = "rule:neutral"
_JA_RULES = [(re.compile(r"合併|買収|公開買付|株式取得|子会社化|経営統合|株式交換|事業譲渡"), "m_and_a"),
             (re.compile(r"決算|業績|配当予想|業績予想"), "earnings"),
             (re.compile(r"自己株式|自社株|新株予約権|株式分割|増資|社債"), "capital_markets"),
             (re.compile(r"破産|民事再生|会社更生|上場廃止"), "bankruptcy_default")]


def norm_item(x: str) -> str:
    x = x.strip()
    if re.fullmatch(r"\d+\.\d+", x):
        a, b = x.split(".")
        return f"{int(a)}.{b[:2].ljust(2, '0')}"
    if re.fullmatch(r"\d+", x):
        return str(int(x))
    return x


def map_items(items: list[str] | None) -> tuple[str, int, int | None, str | None]:
    """(headline part, theme mask, polarity x100 | None, rule) for an 8-K's items."""
    its = [norm_item(i) for i in (items or []) if i and i.strip()]
    main = [i for i in its if i not in EXHIBIT_ONLY] or its
    tm = 0
    pol: int | None = None
    rule = None
    labels = []
    for i in main:
        lab, themes, p = ITEMS_8K.get(i, (f"Item {i}", (), None))
        labels.append(f"{lab} ({i})")
        for t in themes:
            tm |= 1 << T[t]
        if p is not None and (pol is None or p < pol):
            pol, rule = p, f"{RULE_8K}:{i}"
    head = "; ".join(labels[:2]) + (f" +{len(labels) - 2}" if len(labels) > 2 else "")
    return head, tm, pol, rule


# ================================================================================================ state
def state_path(s: Settings):  # noqa: ANN201
    return s.state_dir / "news_filings.json"


def read_state(s: Settings) -> dict[str, Any]:
    p = state_path(s)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def update_state(fn: Any, s: Settings) -> dict[str, Any]:
    p = state_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        st = read_state(s)
        st.setdefault("plan", "docs/ROADMAP_V3_PLAN.md N1 (official corporate news -> story layer)")
        fn(st)
        st["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, p)
    return st


# ================================================================================================ sources
def _issuer_maps(s: Settings) -> tuple[dict[str, int], dict[int, int], dict[int, str], pl.DataFrame]:
    from app.earnings import common as C  # noqa: PLC0415

    idict = dicts.load_dict("issuer", s)
    iid = dict(zip(idict.get_column("code").to_list(), idict.get_column("id").to_list(), strict=True)) if idict.height else {}
    sec = {int(i): int(m or 0) for i, m in idict.select("id", "sector_mask").iter_rows()} if idict.height else {}
    cty = {int(i): c for i, c in idict.select("id", "grp").iter_rows() if c} if idict.height else {}
    meta = C.issuer_meta(s).select("issuer_id", "name", "cik", "country")
    return iid, sec, cty, meta


def sec_rows(s: Settings, since: date, backfill: bool, max_requests: int, meta: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, Any]]:
    """8-K filings (SH.SCHEMA + issuer_id, name) since `since`."""
    from app.earnings import sec_history as SH  # noqa: PLC0415

    m = meta.filter(pl.col("cik").is_not_null())
    ciks = m.get_column("cik").to_list()
    refresh = [] if backfill else SH.recent_filers(4, s)
    df, st = SH.load_history(ciks, refresh=refresh, fetch_missing=backfill, older_pages=True,
                             min_year=since.year, settings=s, max_requests=max_requests)
    df = df.filter((pl.col("form") == "8-K") & (pl.col("filing_date") >= since))
    cmap = {SH.cik10(c): (i, n) for c, i, n in m.select("cik", "issuer_id", "name").iter_rows()}
    iss = [cmap.get(c, (None, None)) for c in df.get_column("cik").to_list()]
    df = df.with_columns(pl.Series("issuer_id", [x[0] for x in iss], pl.Utf8), pl.Series("name", [x[1] for x in iss], pl.Utf8))
    return df.filter(pl.col("issuer_id").is_not_null()), st


def build_sec(df: pl.DataFrame, iid: dict[str, int], sec: dict[int, int], cty: dict[int, str]) -> list[dict[str, Any]]:
    rows = []
    for r in df.iter_rows(named=True):
        its = [norm_item(i) for i in (r["items"] or [])]
        if its and all(i in EXHIBIT_ONLY for i in its):
            continue
        head, tm, pol, rule = map_items(its)
        name = (r["name"] or "").strip() or f"CIK {int(r['cik'])}"
        title = f"{name} 8-K: {head}" if head else f"{name} 8-K current report"
        tm |= tx.title_theme_mask(title) if not its else 0
        ts = r["acceptance_ts"] or datetime(r["filing_date"].year, r["filing_date"].month, r["filing_date"].day, tzinfo=UTC)
        ii = iid.get(r["issuer_id"])
        rows.append({"filing_id": f"sec_edgar:{r['accession']}", "ts": ts, "source_id": "sec_8k",
                     "issuer_id": r["issuer_id"], "iid": ii, "form_type": "8-K", "items": its or None,
                     "title": title[:300], "url": r["url"], "theme_mask": tm,
                     "sector_mask": sec.get(ii, 0) if ii else 0,
                     "country": cty.get(ii) if ii else "US", "sentiment_x100": pol or 0, "polarity_rule": rule,
                     "lang": "en", "domain": "sec.gov"})
    return rows


def exchange_rows(s: Settings, since: date, iid: dict[str, int], sec: dict[int, int], cty: dict[int, str],
                  meta: pl.DataFrame) -> list[dict[str, Any]]:
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.nlp.common import has_data  # noqa: PLC0415

    if not has_data("filings_index", s):
        return []
    fi = (scan_table("filings_index", s)
          .filter(pl.col("source_id").is_in(["tdnet", "hkex"]) & (pl.col("knowledge_date") >= since)
                  & pl.col("form_type").is_in(["TDNET", "TANSHIN", "TANSHIN_AMEND", "BUYBACK", "FORECAST_REVISION",
                                               "ANNOUNCEMENT", "RESULTS", "CIRCULAR"])
                  & pl.col("issuer_id").is_not_null())
          .select("filing_id", "source_id", "issuer_id", "form_type", "knowledge_date", "knowledge_ts", "url", "items")
          .collect())
    names = dict(meta.select("issuer_id", "name").iter_rows())
    out = []
    for r in fi.iter_rows(named=True):
        head = ((r["items"] or [""])[0] or "").strip()
        if not head or "=" in head[:20]:
            continue
        head = re.sub(r"\s+", " ", head.replace("&#x2f;", "/"))
        tm = 0
        if r["source_id"] == "tdnet":
            for rx, th in _JA_RULES:
                if rx.search(head):
                    tm |= 1 << T[th]
            if r["form_type"] in ("TANSHIN", "TANSHIN_AMEND", "FORECAST_REVISION"):
                tm |= 1 << T["earnings"]
            if r["form_type"] == "BUYBACK":
                tm |= 1 << T["capital_markets"]
            lang = "ja"
        else:
            tm = tx.title_theme_mask(head)
            if r["form_type"] == "RESULTS":
                tm |= 1 << T["earnings"]
            lang = "en"
        ii = iid.get(r["issuer_id"])
        nm = (names.get(r["issuer_id"]) or "").strip()
        ts = r["knowledge_ts"] or datetime(r["knowledge_date"].year, r["knowledge_date"].month, r["knowledge_date"].day, tzinfo=UTC)
        out.append({"filing_id": r["filing_id"], "ts": ts, "source_id": r["source_id"], "issuer_id": r["issuer_id"],
                    "iid": ii, "form_type": r["form_type"], "items": None,
                    "title": (f"{nm}: {head}" if nm else head)[:300], "url": r["url"], "theme_mask": tm,
                    "sector_mask": sec.get(ii, 0) if ii else 0, "country": cty.get(ii) if ii else None,
                    "sentiment_x100": 0, "polarity_rule": None, "lang": lang,
                    "domain": "release.tdnet.info" if r["source_id"] == "tdnet" else "hkexnews.hk"})
    return out


# ================================================================================================ frames
FN_SCHEMA = {"filing_id": pl.Utf8, "year": pl.Int16, "date": pl.Date, "ts": pl.Datetime("us", "UTC"),
             "source_id": pl.Utf8, "issuer_id": pl.Utf8, "iid": pl.Int32, "form_type": pl.Utf8,
             "items": pl.List(pl.Utf8), "title": pl.Utf8, "url": pl.Utf8, "url_hash": pl.Int64, "theme_mask": pl.Int64,
             "sector_mask": pl.Int16, "countries": pl.List(pl.Int16), "sentiment_x100": pl.Int8,
             "polarity_rule": pl.Utf8, "story_id": pl.Int64, "story_layer": pl.Utf8, "ingested_at": pl.Datetime("us", "UTC")}


def filing_frame(rows: list[dict[str, Any]], gd: set[str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=FN_SCHEMA)
    now = datetime.now(UTC)
    recs = []
    for r in rows:
        ts = r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=UTC)
        ts = ts.astimezone(UTC)
        d = ts.date()
        c = r.get("country")
        recs.append({"filing_id": r["filing_id"], "year": d.year, "date": d, "ts": ts, "source_id": r["source_id"],
                     "issuer_id": r["issuer_id"], "iid": r["iid"], "form_type": r["form_type"], "items": r["items"],
                     "title": r["title"], "url": r["url"], "url_hash": url_hash(r["url"]) if r["url"] else None,
                     "theme_mask": int(r["theme_mask"]), "sector_mask": int(r["sector_mask"]),
                     "countries": [tx.COUNTRY_ID[c]] if c in tx.COUNTRY_ID else [tx.GLOBAL_ID],
                     "sentiment_x100": int(r["sentiment_x100"]), "polarity_rule": r["polarity_rule"],
                     "story_id": hash64(f"filing:{r['filing_id']}"),
                     "story_layer": "merged" if H.day_ready(d, gd) else "pending_gdelt", "ingested_at": now,
                     "__lang": r["lang"], "__domain": r["domain"]})
    return pl.DataFrame(recs, schema={**FN_SCHEMA, "__lang": pl.Utf8, "__domain": pl.Utf8})


def story_rows(fn: pl.DataFrame, s: Settings) -> tuple[pl.DataFrame, pl.DataFrame]:
    """filing_news rows (with __lang / __domain) -> (STORY_SCHEMA rows + date, story_members rows)."""
    src = H.source_ids(s)
    dom = dicts.assign_ids("domain", fn.get_column("__domain").drop_nulls().unique().to_list(), s)
    lang = dicts.assign_ids("lang", fn.get_column("__lang").drop_nulls().unique().to_list(), s)
    sbit = {"sec_8k": src[H.SRC_8K], "tdnet": src[H.SRC_EXCH], "hkex": src[H.SRC_EXCH]}
    titles = fn.get_column("title").to_list()
    st = fn.select(
        "date", "story_id", "title", pl.Series("title_hash", simhash_many([hash_title(t) for t in titles]), pl.Int64),
        pl.col("__lang").replace_strict(lang, default=None, return_dtype=pl.Int8).alias("lang_id"),
        pl.col("ts").alias("first_ts"), pl.col("ts").alias("last_ts"), pl.lit(1, pl.Int32).alias("n_articles"),
        pl.lit(1, pl.Int16).alias("n_sources"),
        pl.col("source_id").replace_strict({k: 1 << v for k, v in sbit.items()}, default=0, return_dtype=pl.Int16).alias("source_mask"),
        pl.concat_list([pl.col("__domain").replace_strict(dom, default=None, return_dtype=pl.Int32)]).list.drop_nulls().alias("domain_ids"),
        pl.when(pl.col("url").is_not_null()).then(pl.concat_list([pl.col("url")])).otherwise(pl.lit([], pl.List(pl.Utf8))).alias("urls"),
        pl.lit(None, pl.Int16).alias("tone_x100"), "sentiment_x100",
        pl.when(pl.col("polarity_rule").is_not_null()).then(pl.lit(RULE_8K)).otherwise(pl.lit(RULE_NEUTRAL)).alias("sentiment_model"),
        "theme_mask", "sector_mask", "countries",
        pl.when(pl.col("iid").is_not_null()).then(pl.concat_list([pl.col("iid")])).otherwise(pl.lit([], pl.List(pl.Int32))).alias("issuer_ids"),
        pl.when(pl.col("iid").is_not_null()).then(pl.lit([100], pl.List(pl.Int8))).otherwise(pl.lit([], pl.List(pl.Int8))).alias("issuer_rel"),
        pl.lit(0, pl.Int16).alias("n_events"),
    ).with_columns(salience_expr().alias("salience"))
    st = st.select([pl.col(c).cast(t) for c, t in STORY_SCHEMA.items()])
    mem = fn.filter(pl.col("url_hash").is_not_null()).select(
        "date", "story_id", "url_hash",
        pl.col("source_id").replace_strict(sbit, default=None, return_dtype=pl.Int8).alias("source_id"),
        pl.col("__domain").replace_strict(dom, default=None, return_dtype=pl.Int32).alias("domain_id"),
        (pl.col("ts").dt.hour().cast(pl.Int16) * 60 + pl.col("ts").dt.minute().cast(pl.Int16)).alias("minute"),
        pl.lit(None, pl.Int16).alias("tone_x100"),
        pl.col("__lang").replace_strict(lang, default=None, return_dtype=pl.Int8).alias("lang_id"))
    return st, mem


def merge_filings(fn: pl.DataFrame, s: Settings) -> dict[str, Any]:
    """Merge story_layer=merged rows into news_stories (month / week blocks); returns counts."""
    ready = fn.filter(pl.col("story_layer") == "merged")
    rep = {"days": 0, "stories": 0}
    if ready.is_empty():
        return rep
    st, mem = story_rows(ready, s)
    days = sorted(set(st.get_column("date").to_list()))
    for _ym, block in H.day_blocks(days, s, block_days=3):  # whole compacted months, else 3-day blocks (RAM)
        b = st.filter(pl.col("date").is_in(block))
        m = mem.filter(pl.col("date").is_in(block))
        r = H.merge_story_rows_days(b, m, s)
        del b, m
        gc.collect()
        rep["days"] += r["days"]
        rep["stories"] += r["stories"]
    now = datetime.now(UTC).isoformat(timespec="seconds")

    def upd(stt: dict[str, Any]) -> None:
        dd = stt.setdefault("days", {})
        cnt = Counter(d.isoformat() for d in st.get_column("date").to_list())
        for k, n in cnt.items():
            dd[k] = {"merged_at": now, "stories": int((dd.get(k) or {}).get("stories", 0)) + n}
    update_state(upd, s)
    return rep


def write_filing_news(fn: pl.DataFrame, s: Settings) -> int:
    from app.core.storage import write_table  # noqa: PLC0415

    if fn.is_empty():
        return 0
    n = 0
    for (_y,), part in fn.drop("__lang", "__domain").partition_by("year", as_dict=True).items():  # bounded memory
        n += write_table(part, "filing_news", mode="upsert", settings=s)
    return n


def repair_filings(s: Settings, today: date | None = None) -> dict[str, Any]:
    """Pending filing rows whose GDELT day is now done -> merge; days whose filing stories vanished -> re-merge."""
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.events_v2 import store  # noqa: PLC0415
    from app.nlp.common import has_data  # noqa: PLC0415

    if not has_data("filing_news", s):
        return {"pending": 0, "lost": 0}
    gd = H.gdelt_days(s)
    pend = scan_table("filing_news", s).filter(pl.col("story_layer") == "pending_gdelt").collect()
    pend = pend.filter(pl.col("date").map_elements(lambda d: H.day_ready(d, gd), return_dtype=pl.Boolean))
    st = read_state(s)
    lost_days = []
    for k, rec in (st.get("days") or {}).items():
        d = date.fromisoformat(k)
        f = store.day_file("news_stories", d, s)
        mf = store.month_file("news_stories", d.strftime("%Y-%m"), s)
        mt = max([x.stat().st_mtime for x in (f, mf) if x.exists()] or [0])
        try:
            at = datetime.fromisoformat(rec.get("merged_at")).timestamp()
        except (TypeError, ValueError):
            at = 0
        if mt > at + 120:
            lost_days.append(d)
    src = H.source_ids(s)
    bits = (1 << src[H.SRC_8K]) | (1 << src[H.SRC_EXCH])
    lost = []
    for d in lost_days[:2000]:
        sm = H.load_days("news_stories", d, d, s, columns=["source_mask"])
        if sm.is_empty() or sm.filter((pl.col("source_mask").cast(pl.Int32) & bits) != 0).is_empty():
            lost.append(d)
    rows = pend
    if lost:
        rows = pl.concat([rows, scan_table("filing_news", s).filter(pl.col("date").is_in(lost)).collect()],
                         how="vertical_relaxed").unique("filing_id")
    if rows.is_empty():
        return {"pending": 0, "lost": 0}
    rows = rows.with_columns(pl.lit("merged").alias("story_layer"),
                             pl.when(pl.col("source_id") == "tdnet").then(pl.lit("ja")).otherwise(pl.lit("en")).alias("__lang"),
                             pl.col("source_id").replace_strict({"sec_8k": "sec.gov", "tdnet": "release.tdnet.info",
                                                                 "hkex": "hkexnews.hk"}, default=None).alias("__domain"))
    rep = merge_filings(rows, s)
    write_filing_news(rows, s)
    return {"pending": pend.height, "lost": len(lost), **rep}


def probe_progress(pr: dict[str, Any], s: Settings | None = None) -> dict[str, Any]:
    """Gap-filler probe (progress.probe = news_filings): done once the one-time backfill finished."""
    s = s or get_settings()
    st = read_state(s)
    bf = st.get("backfill") or {}
    done = bool(bf.get("done_at"))
    return {"done": done, "pct": 100.0 if done else None, "done_units": bf.get("rows"), "unit": "filings",
            "last_run_at": bf.get("done_at"),
            "detail": (f"backfill since {bf.get('since')} done {bf.get('done_at')}: {bf.get('rows')} filings, "
                       f"{bf.get('pending_gdelt')} waiting for GDELT days") if done else "backfill not run yet"}


# ================================================================================================ job
def run_news_filings(s: Settings, p: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    backfill = str(p.get("backfill", "false")).lower() in ("1", "true", "yes")
    today = datetime.now(UTC).date()
    since = date.fromisoformat(p["since"]) if p.get("since") else (date(2001, 1, 1) if backfill else today - timedelta(days=10))
    sources = p.get("sources") or ["sec", "exchanges"]
    max_req = int(p.get("max_requests", 4000 if backfill else 600))
    iid, sec, cty, meta = _issuer_maps(s)
    gd = H.gdelt_days(s)
    rep: dict[str, Any] = {"since": since.isoformat(), "backfill": backfill}
    rows: list[dict[str, Any]] = []
    frames: list[pl.DataFrame] = []
    if "sec" in sources:  # CIK chunks: bounded memory (the 2001-> history is ~800k 8-Ks)
        chunk = int(p.get("cik_chunk", 400))
        mc = meta.filter(pl.col("cik").is_not_null())
        st_all: Counter = Counter()
        n8 = 0
        for i in range(0, mc.height, chunk):
            left = max(0, max_req - int(st_all.get("fetched", 0)) - int(st_all.get("older_pages", 0)))
            df, st = sec_rows(s, since, backfill, left, mc.slice(i, chunk))
            st_all.update({k: v for k, v in st.items() if isinstance(v, int)})
            sr = build_sec(df, iid, sec, cty)
            n8 += len(sr)
            frames.append(filing_frame(sr, gd))
            del df, sr
        rep["sec_history"] = dict(st_all)
        rep["sec_8k"] = n8
    if "exchanges" in sources:
        er = exchange_rows(s, since if not backfill else date(2001, 1, 1), iid, sec, cty, meta)
        rep["exchange_ann"] = len(er)
        frames.append(filing_frame(er, gd))
    frames = [f for f in frames if f.height]
    # one accession can be filed by several co-registrant CIKs (different chunks): one story per filing
    fn = pl.concat(frames, how="vertical_relaxed").unique("filing_id", keep="first") if frames else filing_frame([], gd)
    del frames, rows
    rep["rows"] = fn.height
    rep["pending_gdelt"] = int(fn.filter(pl.col("story_layer") == "pending_gdelt").height)
    rep["linked"] = int(fn.filter(pl.col("iid").is_not_null()).height)
    rep["by_year"] = {str(k): v for k, v in sorted(Counter(fn.get_column("year").to_list()).items())}
    rep["by_source"] = dict(Counter(fn.get_column("source_id").to_list()))
    rep["merge"] = merge_filings(fn, s)
    rep["written"] = write_filing_news(fn, s)
    if str(p.get("repair", "true")).lower() not in ("0", "false", "no"):
        rep["repair_filings"] = repair_filings(s)
        rep["repair_ccnews"] = H.repair_ccnews(s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)

    def upd(stt: dict[str, Any]) -> None:
        runs = stt.setdefault("runs", [])
        stt["runs"] = (runs + [{k: v for k, v in rep.items() if k not in ("by_year",)}
                               | {"at": datetime.now(UTC).isoformat(timespec="seconds")}])[-20:]
        if backfill:
            stt["backfill"] = {"done_at": datetime.now(UTC).isoformat(timespec="seconds"), "since": since.isoformat(),
                               "rows": fn.height, "by_year": rep["by_year"], "by_source": rep["by_source"],
                               "pending_gdelt": rep["pending_gdelt"], "linked": rep["linked"]}
        stt["last_run_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    update_state(upd, s)
    log.info("news_filings: %s", {k: v for k, v in rep.items() if k != "by_year"})
    return rep


def run(ctx):  # noqa: ANN001 - JobContext (job news_filings)
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_news_filings(ctx.settings, ctx.params or {})
    return JobResult(status="success", rows_written={"filing_news": rep["rows"], "news_stories": rep["merge"]["stories"]},
                     entities_ok=rep["rows"], message=str({k: v for k, v in rep.items() if k not in ("by_year", "sec_history")}))


_ = defaultdict
