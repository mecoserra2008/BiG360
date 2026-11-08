"""Live alert evaluation (R1.3). Consumer group `alerts` over filings, macro and signals.

Rules come from app_state.duckdb `alerts` (the same rules the nightly alerts_eval job evaluates), refreshed every 60 s
with a brief read-only open (the API also opens the file briefly, so both retry on the DuckDB file lock). Live rule
types and their trigger (params as documented in contracts/schemas/alerts.yaml):
* new_filing    {form_classes}: a filing of a target security's issuer (default classes annual, quarterly,
                 current_report); as_of_date = filing date (local);
* insider_buy   {min_value_usd, cluster_only}: a live Form 4 with an open-market buy whose rescored 30-day net buy
                 value >= min_value_usd (and cluster_buy when cluster_only); as_of_date = signal date (ET);
* macro_release {max_importance, countries, series_ids}: a captured release; target 'macro:<CC>';
* macro_surprise {threshold_z, max_importance, countries, themes, series_ids}: |surprise_z| >= threshold (model-based
                 expectation, not consensus);
* activist_13d  {include_amendments, include_switch}: a live Schedule 13D on a target security (signals type activism,
                 app.realtime.activism_live): new 13D, 13G -> 13D switch (default on), 13D/A on request; as_of_date =
                 filing date, like the nightly evaluator (workstream B);
* watchlist_news / story_velocity (roadmap v3 N4.3, app.news_live.alerts): news.item linked to a target security /
                 a burst of articles on a story (news.story); one event per (alert, target, story), not per day.
Other rule types (prices, scores, models) stay nightly. Idempotency matches the nightly evaluator: one event per
(alert_id, security_id, as_of_date) and cooldown_days per (alert, security), checked against existing alert_events,
so the nightly job will not fire the same event again. Fired events are appended to alert_events (payload.live =
true, payload.rule_type) and published on `alerts` (key '<alert_id>|<security_id>|<as_of_date>').
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer, ts_of
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)
LIVE_RULES = ("new_filing", "insider_buy", "macro_release", "macro_surprise", "watchlist_news", "story_velocity",
              "activist_13d")
ET = ZoneInfo("America/New_York")


def _in(v: Any, allowed: Any) -> bool:
    return not allowed or v in set(allowed)


def _date(s: Any) -> date | None:
    if s is None:
        return None
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def triggers_for(rule: dict[str, Any], targets: list[str], msg: Message, sec_issuer: dict[str, str]) -> list[dict]:
    """Pure: one alert rule x one message -> trigger dicts {security_id, as_of_date, message, payload}."""
    rt = rule["rule_type"]
    params = rule.get("params") or {}
    if isinstance(params, str):
        params = json.loads(params or "{}")
    p = msg.payload
    out: list[dict[str, Any]] = []
    if rt == "new_filing" and msg.topic == "filings":
        classes = set(params.get("form_classes") or ["annual", "quarterly", "current_report"])
        if p.get("form_class") not in classes:
            return []
        for sid in targets:
            if sid == p.get("security_id") or (p.get("issuer_id") and sec_issuer.get(sid) == p.get("issuer_id")):
                d = _date(p.get("filed_date")) or msg.ingest_dt.astimezone(ET).date()
                items = f" (Items {', '.join(p['items'])})" if p.get("items") and p.get("source_id") == "sec_edgar" else ""
                out.append({"security_id": sid, "as_of_date": d,
                            "message": f"new filing: {p.get('form_type')}{items} - live",
                            "payload": {"filings": [{"filing_id": p.get("filing_id"), "form_type": p.get("form_type"),
                                                     "url": p.get("url"), "knowledge_date": str(d)}]}})
    elif rt == "insider_buy" and msg.topic == "signals" and msg.type == "insider":
        sig = p.get("signal") or {}
        v = float(sig.get("net_buy_value_usd_30d") or 0.0)
        if not p.get("has_open_market_buy") or v <= 0 or v < float(params.get("min_value_usd", 0)):
            return []
        if params.get("cluster_only") and not sig.get("cluster_buy"):
            return []
        for sid in targets:
            if sid == p.get("security_id") or (p.get("issuer_id") and sec_issuer.get(sid) == p.get("issuer_id")):
                out.append({"security_id": sid, "as_of_date": _date(p.get("as_of_date")),
                            "message": f"insider buying ${v:,.0f} (30d)" + (" - cluster buy" if sig.get("cluster_buy")
                                                                             else "") + " - live",
                            "payload": {"net_buy_value_usd_30d": v, "cluster_buy": sig.get("cluster_buy"),
                                        "c_suite_buy": sig.get("c_suite_buy"), "signal_score": sig.get("signal_score"),
                                        "filing_id": p.get("filing_id")}})
    elif rt == "activist_13d" and msg.topic == "signals" and msg.type == "activism":
        types = {"new_13d"}
        if params.get("include_switch", True):
            types.add("switch_13g_to_13d")
        if params.get("include_amendments", False):
            types.add("amend_13d")
        if p.get("event_type") not in types:
            return []
        labels = {"new_13d": "new 13D", "switch_13g_to_13d": "13G -> 13D switch", "amend_13d": "13D amendment"}
        for sid in targets:
            if sid == p.get("security_id") or (p.get("issuer_id") and sec_issuer.get(sid) == p.get("issuer_id")):
                out.append({"security_id": sid, "as_of_date": _date(p.get("filed_date")),
                            "message": f"{labels[p['event_type']]} filed by {p.get('filer_name') or 'unknown filer'} "
                                       f"({p.get('form_type')}, SEC) - live",
                            "payload": {"filing_id": p.get("filing_id"), "event_type": p.get("event_type"),
                                        "filer_name": p.get("filer_name"), "url": p.get("url")}})
    elif rt in ("watchlist_news", "story_velocity"):
        from app.news_live.alerts import news_triggers  # noqa: PLC0415

        return news_triggers(rule, params, targets, msg.topic, msg.type, p, sec_issuer, msg.ingest_dt)
    elif rt in ("macro_release", "macro_surprise") and msg.topic == "macro" and msg.type == "release":
        imp = int(p.get("importance") or 3)
        if not (_in(p.get("country"), params.get("countries")) and _in(p.get("series_id"), params.get("series_ids"))):
            return []
        rel_day = _date(p.get("scheduled_at"))
        base = {"release_event_id": p.get("release_event_id"), "series_id": p.get("series_id"),
                "actual": p.get("actual"), "prior": p.get("prior"), "importance": imp}
        if rt == "macro_release":
            if imp > int(params.get("max_importance", 1)):
                return []
            prior = p.get("prior")
            out.append({"security_id": f"macro:{p.get('country')}", "as_of_date": rel_day,
                        "message": f"{p.get('country')} {p.get('release_name')}: {p.get('series_id')} = "
                                   f"{p.get('actual'):.4g} (prior {prior if prior is None else format(prior, '.4g')}) - live",
                        "payload": base})
        else:
            z = p.get("surprise_z")
            if z is None or abs(z) < abs(float(params.get("threshold_z", 2.0))) or imp > int(params.get("max_importance", 3)) \
                    or not _in(p.get("theme"), params.get("themes")):
                return []
            out.append({"security_id": f"macro:{p.get('country')}", "as_of_date": rel_day,
                        "message": f"{p.get('country')} {p.get('series_id')} surprise z={z:+.2f}: actual {p.get('actual'):.4g} "
                                   f"vs model-based expectation {p.get('expectation'):.4g} (not consensus) - live",
                        "payload": base | {"surprise_z": z, "expectation": p.get("expectation"), "model_based": True}})
    return out


class AlertConsumer(Consumer):
    name = "alerts"
    group = "alerts"
    topics = ("filings", "macro", "signals", "news.item", "news.story")
    batch = 300

    def __init__(self, store: Any = None, **params: Any):
        super().__init__(**params)
        self.store = store            # tests inject an in-memory store
        self.rules: list[dict[str, Any]] = []
        self.members: dict[str, list[str]] = {}
        self.fired: set[tuple[str, str, Any]] = set()
        self.per_day: dict[tuple[str, str, date | None], int] = {}
        self.last_fire: dict[tuple[str, str], datetime] = {}
        self._loaded = 0.0
        self.refresh_s = float(params.get("refresh_s", 60))

    # ---- app_state access
    def _load(self, ctx: StreamContext) -> None:
        if time.time() - self._loaded < self.refresh_s and self._loaded:
            return
        if self.store is not None:
            self.rules, self.members, existing = self.store.load()
        else:
            self.rules, self.members, existing = load_app_state(ctx.settings)
        for ev in existing:
            k = (ev["alert_id"], ev["security_id"])
            fa = ev["fired_at"] if ev["fired_at"].tzinfo else ev["fired_at"].replace(tzinfo=UTC)
            if k not in self.last_fire or fa > self.last_fire[k]:
                self.last_fire[k] = fa
            if ev.get("dedupe"):   # news rules: one event per story
                self.fired.add((ev["alert_id"], ev["security_id"], "story:" + str(ev["dedupe"])))
                dk = (ev["alert_id"], ev["security_id"], _date(ev["as_of_date"]))
                self.per_day[dk] = self.per_day.get(dk, 0) + 1
            else:
                self.fired.add((ev["alert_id"], ev["security_id"], _date(ev["as_of_date"])))
        self._loaded = time.time()
        self.stats.detail = f"{len(self.rules)} live-evaluable rules"

    def handle_batch(self, ctx: StreamContext, msgs: list[Message]) -> list[Envelope]:
        self._load(ctx)
        if not self.rules:
            return []
        sec_issuer: dict[str, str] = {}
        if ctx.linker is not None:
            meta = ctx.linker._load_meta()
            sec_issuer = {v["security_id"]: k for k, v in meta.items() if v.get("security_id")}
        now = ctx.clock()
        events: list[dict[str, Any]] = []
        batch_keys: set[tuple[str, str, date]] = set()
        batch_last: dict[tuple[str, str], datetime] = {}
        for m in msgs:
            for a in self.rules:
                targets = [a["security_id"]] if a.get("security_id") else self.members.get(a.get("watchlist_id") or "", [])
                if a["rule_type"] in ("macro_release", "macro_surprise"):
                    targets = ["macro"]
                untargeted = a["rule_type"] == "story_velocity" and not a.get("security_id") and not a.get("watchlist_id")
                if not targets and not untargeted:
                    continue
                try:
                    trigs = triggers_for(a, targets, m, sec_issuer)
                except Exception as e:  # noqa: BLE001 - one broken rule must not stop the others
                    log.warning("live alert %s failed: %s", a.get("alert_id"), e)
                    continue
                cooldown = timedelta(days=int(a.get("cooldown_days") or 0))
                for t in trigs:
                    if t["as_of_date"] is None:
                        continue
                    news = bool(t.get("dedupe"))
                    k3 = (a["alert_id"], t["security_id"], ("story:" + t["dedupe"]) if news else t["as_of_date"])
                    if k3 in self.fired or k3 in batch_keys:
                        continue
                    lf = batch_last.get(k3[:2]) or self.last_fire.get(k3[:2])
                    if lf is not None and cooldown and now - lf < cooldown:
                        continue
                    if news:
                        params = a.get("params") or {}
                        if isinstance(params, str):
                            params = json.loads(params or "{}")
                        dk = (a["alert_id"], t["security_id"], t["as_of_date"])
                        if self.per_day.get(dk, 0) >= int(params.get("max_per_day") or 10):
                            continue
                        self.per_day[dk] = self.per_day.get(dk, 0) + 1
                        t["payload"] = {**t["payload"], "dedupe": t["dedupe"]}
                    batch_keys.add(k3)
                    batch_last[k3[:2]] = now
                    events.append({"event_id": uuid.uuid4().hex, "alert_id": a["alert_id"],
                                   "security_id": t["security_id"], "fired_at": now, "as_of_date": t["as_of_date"],
                                   "message": t["message"],
                                   "payload": json.dumps({"rule_type": a["rule_type"], "live": True, "bus_id": m.id,
                                                          **t["payload"]}, default=str),
                                   "read": False, "_msg": m})
        if not events:
            return []
        rows = [{k: v for k, v in e.items() if k != "_msg"} for e in events]
        if self.store is not None:
            self.store.append(rows)
        else:
            append_events(ctx.settings, rows)
        self.fired |= batch_keys            # only after the events are durable (a failed append is retried)
        self.last_fire.update(batch_last)
        return [Envelope("alerts", f"{e['alert_id']}|{e['security_id']}|{e['as_of_date']}", {
            "event_id": e["event_id"], "alert_id": e["alert_id"], "security_id": e["security_id"],
            "rule_type": json.loads(e["payload"])["rule_type"], "as_of_date": str(e["as_of_date"]),
            "fired_at": e["fired_at"].isoformat().replace("+00:00", "Z"), "message": e["message"],
            "payload": json.loads(e["payload"]), "source_topic": e["_msg"].topic, "source_key": e["_msg"].key,
        }, type=json.loads(e["payload"])["rule_type"], ts_source=ts_of(e["_msg"])) for e in events]


def load_app_state(settings: Any) -> tuple[list[dict], dict[str, list[str]], list[dict]]:
    from app.core.storage import connect_app_state  # noqa: PLC0415

    try:
        con = connect_app_state(read_only=True, attempts=6, delay=0.5, settings=settings)
    except Exception as e:  # noqa: BLE001 - file locked / missing: keep the previous rules
        log.debug("app_state unavailable: %s", e)
        raise
    try:
        tabs = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        if "alerts" not in tabs:
            return [], {}, []
        rules = con.execute("SELECT * FROM alerts WHERE enabled AND rule_type IN "
                            f"({','.join(repr(x) for x in LIVE_RULES)})").pl().to_dicts()
        members: dict[str, list[str]] = {}
        if "watchlist_items" in tabs:
            for w, s in con.execute("SELECT watchlist_id, security_id FROM watchlist_items").fetchall():
                members.setdefault(w, []).append(s)
        existing = []
        if "alert_events" in tabs:
            existing = con.execute("SELECT alert_id, security_id, fired_at, as_of_date, "
                                   "json_extract_string(payload, '$.dedupe') AS dedupe FROM alert_events "
                                   "WHERE fired_at >= now() - INTERVAL 400 DAY").pl().to_dicts()
        return rules, members, existing
    finally:
        con.close()


def append_events(settings: Any, rows: list[dict[str, Any]]) -> None:
    import polars as pl  # noqa: PLC0415

    from app.core.storage import connect_app_state, ensure_duckdb_tables  # noqa: PLC0415

    con = connect_app_state(attempts=20, delay=0.5, settings=settings)
    try:
        ensure_duckdb_tables(con, ["alert_events"])
        df = pl.DataFrame(rows)
        con.register("_live_events", df.to_arrow())
        con.execute("""INSERT INTO alert_events (event_id, alert_id, security_id, fired_at, as_of_date, message, payload, read)
                       SELECT n.event_id, n.alert_id, n.security_id, n.fired_at, n.as_of_date, n.message, n.payload, n.read
                       FROM _live_events n WHERE NOT EXISTS (SELECT 1 FROM alert_events e WHERE e.alert_id = n.alert_id
                       AND e.security_id = n.security_id AND e.as_of_date = n.as_of_date
                       AND coalesce(json_extract_string(e.payload, '$.dedupe'), '')
                           = coalesce(json_extract_string(n.payload, '$.dedupe'), ''))""")
        con.unregister("_live_events")
        for a, f in df.group_by("alert_id").agg(pl.col("fired_at").max()).iter_rows():
            con.execute("UPDATE alerts SET last_fired_at = ? WHERE alert_id = ?", [f, a])
    finally:
        con.close()
