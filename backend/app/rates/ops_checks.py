"""Check `published_dates` (gap detector + startup sync rule) for datasets whose entities are published on their own
official schedule (W3: official curves, money-market fixings, OAS, auctions, FIRDS, company debt).

coverage.yaml dataset spec
  table, date_column (default date), key_column (e.g. curve_id / series_id; omit for a single entity '*'),
  rule: default publication rule; entities: {entity: {keys: [values] (default [entity]), rule overrides, source}}
Rule keys (app.rates.registry.Publish semantics):
  cal   exchange MIC | weekdays | target          tz / at  local publication time
  lag   business days after the data date          freq     D (business days) | W (weekly, `weekday` 0=Mon..6)
  lag_days  M: days after the month end (date = 1st of the month) | batch: days after the month end of daily data
An entity is fresh when its latest stored date >= the latest date whose publication time has passed; otherwise
`due` (the sync plans the dataset's fetch jobs; for rates.curves only the sources of the due curves) - and the
attempt ledger turns "fetched but still missing" into not_yet_published, as for the other datasets.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.ops import calendars as cal

DEFAULT_RULE = {"cal": "weekdays", "tz": "UTC", "at": "18:00", "lag": 0, "freq": "D", "lag_days": 0, "weekday": 5}


def _rule(spec: dict[str, Any], ent: dict[str, Any] | None) -> dict[str, Any]:
    r = {**DEFAULT_RULE, **(spec.get("rule") or {})}
    for k in DEFAULT_RULE:
        if ent and k in ent:
            r[k] = ent[k]
    return r


def _business_days(calendar: str, start: date, end: date) -> list[date]:
    if calendar == "target":
        from app.ops.sync.expect import target_day  # noqa: PLC0415

        out, d = [], start
        while d <= end:
            if target_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out
    return cal.sessions(None if calendar in ("weekdays", "daily") else calendar, start, end)


def _pub_dt(d: date, r: dict[str, Any], days: list[date]) -> datetime:
    """Publication time (UTC) of data dated d."""
    tz = ZoneInfo(r["tz"])
    hh, mm = (int(x) for x in str(r["at"]).split(":"))
    if r["freq"] in ("M", "batch"):
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        p = nxt - timedelta(days=1) + timedelta(days=int(r["lag_days"]))
    elif r["freq"] == "W":
        p = d + timedelta(days=int(r["lag_days"]))
    else:
        lag = int(r["lag"])
        if lag:
            later = [x for x in days if x > d]
            p = later[lag - 1] if len(later) >= lag else d + timedelta(days=lag + 2 * (lag // 5 + 1))
        else:
            p = d
    return datetime.combine(p, time(hh, mm), tz).astimezone(UTC)


def expected(r: dict[str, Any], now: datetime | None = None) -> tuple[date | None, datetime | None]:
    """(latest data date that should be public by now, next publication time UTC)."""
    now = now or datetime.now(UTC)
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    today = now.date()
    if r["freq"] == "M":
        exp = nxt = None
        y, m = today.year, today.month
        for _ in range(14):
            d = date(y, m, 1)
            t = _pub_dt(d, r, [])
            if t <= now and exp is None:
                exp = d
            if t > now:
                nxt = t
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
            if exp is not None:
                break
        return exp, nxt
    if r["freq"] == "batch":
        days = _business_days(r["cal"], today - timedelta(days=120), today)
        exp = nxt = None
        for d in reversed(days):
            nd = d + timedelta(days=1)
            if nd.month == d.month:          # only month-end business days carry a batch
                continue
            t = _pub_dt(d, r, days)
            if t <= now:
                exp = d
                break
            nxt = t
        return exp, nxt
    if r["freq"] == "W":
        wd = int(r.get("weekday", 5))
        d = today
        exp = nxt = None
        for _ in range(21):
            if d.weekday() == wd:
                t = _pub_dt(d, r, [])
                if t <= now:
                    exp = d
                    break
                nxt = t
            d -= timedelta(days=1)
        if nxt is None and exp is not None:
            nxt = _pub_dt(exp + timedelta(days=7), r, [])
        return exp, nxt
    days = _business_days(r["cal"], today - timedelta(days=30), today + timedelta(days=30))
    exp = nxt = None
    for d in sorted(days, reverse=True):
        t = _pub_dt(d, r, days)
        if t <= now:
            exp = d
            break
        nxt = t
    return exp, nxt


def periods_between(r: dict[str, Any], a: date | None, b: date | None) -> int | None:
    if a is None or b is None:
        return None
    if b <= a:
        return 0
    if r["freq"] == "M":
        return (b.year - a.year) * 12 + b.month - a.month
    if r["freq"] == "W":
        return (b - a).days // 7
    if r["freq"] == "batch":
        return max(0, (b.year - a.year) * 12 + b.month - a.month)
    return len(_business_days(r["cal"], a + timedelta(days=1), b))


def entity_specs(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ents = spec.get("entities") or {}
    out = {}
    for name, e in (ents.items() if isinstance(ents, dict) else []):
        if name in ("curves", "series"):          # nested form {curves: {ID: rule}}
            for k, v in (e or {}).items():
                out[k] = {**(v or {}), "keys": (v or {}).get("keys") or [k]}
            continue
        e = e or {}
        out[name] = {**e, "keys": e.get("keys") or ([name] if spec.get("key_column") else [])}
    return out or {"*": {"keys": []}}


def latest_dates_sql(spec: dict[str, Any], ents: dict[str, dict[str, Any]], src: str) -> str:
    dcol = spec.get("date_column", "date")
    kc = spec.get("key_column")
    dexpr = f'CAST("{dcol}" AS DATE)'
    if not kc:
        return f"SELECT '*' AS k, max({dexpr}) FROM {src} WHERE {dexpr} <= current_date"
    keys = sorted({k for e in ents.values() for k in e["keys"]})
    lst = ", ".join("'" + str(k).replace("'", "''") + "'" for k in keys)
    return f'SELECT "{kc}" AS k, max({dexpr}) FROM {src} WHERE "{kc}" IN ({lst}) AND {dexpr} <= current_date GROUP BY 1'


def evaluate(spec: dict[str, Any], last_by_key: dict[str, date | None], now: datetime | None = None
             ) -> list[dict[str, Any]]:
    """Pure: per entity {entity, data_as_of, expected, next, lag, status (fresh | due | missing), rule, source}."""
    out = []
    sla = float(spec.get("sla", 1))
    for name, e in entity_specs(spec).items():
        r = _rule(spec, e)
        keys = e["keys"] or ["*"]
        have_vals = [last_by_key.get(k) for k in keys]
        have = max((v for v in have_vals if v is not None), default=None)
        exp, nxt = expected(r, now)
        lag = periods_between(r, have, exp)
        if have is None:
            st = "missing"
        elif lag is not None and lag > 0 and lag >= (1 if spec.get("strict") else sla):
            st = "due"
        else:
            st = "fresh"
        out.append({"entity": name, "data_as_of": have, "expected": exp, "next": nxt, "lag": lag, "status": st,
                    "rule": r, "source": e.get("source"), "job": e.get("job"), "keys": keys})
    return out


# ------------------------------------------------------------------------------------------------ gap detector

def _register() -> None:
    from app.ops.gaps import Gap, check  # noqa: PLC0415

    @check("published_dates")
    def _check_published(cx: Any, spec: dict[str, Any], st: Any) -> list[Any]:
        ents = entity_specs(spec)
        src = cx.spec_src(spec, cx.today - timedelta(days=int(spec.get("lookback_days", 800))))
        if src is None:
            st.status = "missing"
            return [Gap(spec["id"], "*", "missing", note="table has no data", key="nodata")]
        last = {k: v for k, v in cx.q(latest_dates_sql(spec, ents, src))}
        res = evaluate(spec, last, cx.now)
        gaps = []
        for r in res:
            st.entities[r["entity"]] = {"data_as_of": r["data_as_of"].isoformat() if r["data_as_of"] else None,
                                        "expected_as_of": r["expected"].isoformat() if r["expected"] else None,
                                        "lag": r["lag"], "status": r["status"]}
            if r["status"] in ("due", "missing"):
                gaps.append(Gap(spec["id"], r["entity"], "stale" if r["data_as_of"] else "missing",
                                r["data_as_of"] + timedelta(days=1) if r["data_as_of"] else None, r["expected"],
                                r["lag"], 0, note=f"{r['entity']}: data through {r['data_as_of']} (expected "
                                                  f"{r['expected']}, {r['rule']['freq']} {r['rule']['cal']})",
                                key="stale"))
        haves = [r["data_as_of"] for r in res if r["data_as_of"]]
        exps = [r["expected"] for r in res if r["expected"]]
        st.data_as_of = min(haves) if haves else None
        st.expected_as_of = max(exps) if exps else None
        st.sla = float(spec.get("sla", 1))
        st.lag = max((r["lag"] or 0 for r in res), default=None)
        n_bad = sum(1 for r in res if r["status"] != "fresh")
        st.status = "fresh" if not n_bad else ("missing" if n_bad == len(res) and not haves else "stale")
        st.detail = f"{len(res) - n_bad}/{len(res)} entities current"
        return gaps


# ------------------------------------------------------------------------------------------------ startup sync

def scan_published(sc: Any, spec: dict[str, Any], ds: Any) -> None:
    """Scanner rule: per entity fresh / due / not_yet_published; plans the fetch jobs (rates.curves: sources)."""
    from app.ops.sync import policy as P  # noqa: PLC0415
    from app.ops.sync.scan import EntitySync, _iso  # noqa: PLC0415

    did = spec["id"]
    st = sc.observe(spec)
    ents = st.entities or {}
    last = {}
    for name, e in entity_specs(spec).items():
        v = (ents.get(name) or {}).get("data_as_of")
        for k in e["keys"] or ["*"]:
            last[k] = date.fromisoformat(v) if v else None
    res = evaluate(spec, last, sc.now)
    jobs, held = sc.fetch_jobs(did)
    if not jobs and spec.get("live_job") and spec["live_job"] in sc.reg.jobs:
        jobs = [spec["live_job"]]
    ds.entities += held
    due_sources: set[str] = set()
    n_due = 0
    n_dataset_due = 0     # due entities without their own `job` (planned with the dataset's fetch jobs)
    for r in res:
        if r["status"] == "fresh":
            ds.entities.append(EntitySync(r["entity"], "fresh", _iso(r["data_as_of"]), _iso(r["expected"]),
                                          _iso(r["next"]), f"through {r['data_as_of']}"))
            continue
        h = sc.hold(did, r["entity"], _iso(r["expected"]))
        if h is not None:
            h.data_as_of, h.next_expected = _iso(r["data_as_of"]), _iso(r["next"])
            ds.entities.append(h)
            continue
        n_due += 1
        ds.entities.append(EntitySync(r["entity"], "due", _iso(r["data_as_of"]), _iso(r["expected"]), _iso(r["next"]),
                                      detail=f"latest {r['data_as_of']} < expected {r['expected']}"))
        if r.get("source"):
            due_sources.add(r["source"])
        if r.get("job"):
            if r["job"] in sc.reg.jobs:
                sc.add_plan(r["job"], {}, did, f"{did}: {r['entity']} behind",
                            periods={f"{did}|{r['entity']}": [_iso(r["expected"]) or ""]})
        else:
            n_dataset_due += 1
    haves = [r["data_as_of"] for r in res if r["data_as_of"]]
    ds.data_as_of = _iso(max(haves)) if haves else None
    ds.expected_as_of = _iso(max((r["expected"] for r in res if r["expected"]), default=None))
    nx = [r["next"] for r in res if r["next"]]
    ds.next_expected = _iso(min(nx)) if nx else None
    ds.detail = f"{len(res) - n_due}/{len(res)} current" + (f"; due: {', '.join(sorted(due_sources))}" if due_sources else "")
    if not n_dataset_due:
        return
    periods = {f"{did}|{r['entity']}": [_iso(r["expected"]) or ""] for r in res
               if r["status"] != "fresh" and not r.get("job")}
    for j in jobs:
        params: dict[str, Any] = {}
        if j == "rates_curves" and due_sources:
            params = {"sources": sorted(due_sources)}
        sc.add_plan(j, params, did, f"{did}: {n_due} entit{'y' if n_due == 1 else 'ies'} behind", periods=periods)
    _ = P


_register()
