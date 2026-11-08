"""Thesis tracker (tag: theses, roadmap v2 #8): CRUD, journal, reviews, automatic status, performance, timeline,
linked evidence, Markdown export and hit-rate stats. State lives in app_state (app.theses.store); market data comes
from the serving snapshot. Creating / updating a thesis regenerates its linked alert rules (alerts.thesis_id) and
re-evaluates its automatic status; the nightly thesis_eval job does the same and fires thesis_* alert events.
"""

from __future__ import annotations

import datetime as dt
import secrets
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.api import pricebars
from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import fnum, invalid, iso, not_found
from app.theses import evaluate as tev
from app.theses import export as tex
from app.theses import perf as tperf
from app.theses import rules as trules
from app.theses import store

router = APIRouter(tags=["theses"], route_class=FastRoute)

SubjectType = Literal["security", "sector", "country", "macro_theme", "cross_asset"]
Direction = Literal["long", "short", "neutral"]
Horizon = Literal["1M", "3M", "6M", "12M", "24M", "36M+"]
Status = Literal["idea", "active", "closed"]
CloseReason = Literal["target_hit", "stopped", "invalidated", "time_expired", "thesis_changed", "other"]
Outcome = Literal["win", "loss", "flat"]
AutoStatus = Literal["on_track", "at_risk", "invalidated", "unknown"]


def _sid(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


class Kpi(BaseModel):
    kpi_id: str | None = Field(default=None, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    kind: Literal["metric", "expr", "xa", "macro"] = "metric"
    ref: str | None = Field(default=None, max_length=120)
    expr: str | None = Field(default=None, max_length=400)
    op: Literal["gt", "gte", "lt", "lte", "between"] = "gte"
    threshold: float
    threshold_hi: float | None = None
    role: Literal["confirm", "invalidate", "watch"] = "confirm"
    unit: str | None = Field(default=None, max_length=20)


class Catalyst(BaseModel):
    catalyst_id: str | None = Field(default=None, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    date: dt.date | None = None
    kind: Literal["earnings", "macro", "corporate", "product", "regulatory", "custom"] = "custom"
    source: str | None = Field(default=None, max_length=80)
    source_ref: str | None = Field(default=None, max_length=200)
    done: bool = False


class Invalidation(BaseModel):
    condition_id: str | None = Field(default=None, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["price_below", "price_above", "kpi", "date", "manual"] = "manual"
    level: float | None = None
    kpi_id: str | None = None
    date: dt.date | None = None
    triggered: bool = False


class Origin(BaseModel):
    kind: Literal["manual", "company", "screener", "studio", "cross_asset", "backtest"] = "manual"
    ref: str | None = Field(default=None, max_length=400)
    label: str | None = Field(default=None, max_length=200)


class ThesisFields(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    price_id: str | None = Field(default=None, max_length=60)
    direction: Direction | None = None
    horizon: Horizon | None = None
    conviction: int | None = Field(default=None, ge=1, le=5)
    status: Status | None = None
    thesis_md: str | None = Field(default=None, max_length=50_000)
    key_drivers: list[str] | None = Field(default=None, max_length=30)
    catalysts: list[Catalyst] | None = Field(default=None, max_length=50)
    kpis: list[Kpi] | None = Field(default=None, max_length=30)
    entry_lo: float | None = None
    entry_hi: float | None = None
    target: float | None = None
    stop: float | None = None
    invalidation: list[Invalidation] | None = Field(default=None, max_length=30)
    scenario_id: str | None = Field(default=None, max_length=80)
    study_id: str | None = Field(default=None, max_length=80)
    studio_run_id: str | None = Field(default=None, max_length=80)
    backtest_strategy_id: str | None = Field(default=None, max_length=80)
    backtest_run_id: str | None = Field(default=None, max_length=80)
    benchmark_id: str | None = Field(default=None, max_length=60)
    sector_benchmark_id: str | None = Field(default=None, max_length=60)
    tags: list[str] | None = Field(default=None, max_length=20)
    review_every_days: int | None = Field(default=None, ge=1, le=730)
    close_reason: CloseReason | None = None
    outcome: Outcome | None = None
    post_mortem_md: str | None = Field(default=None, max_length=50_000)


class ThesisCreate(ThesisFields):
    title: str = Field(min_length=1, max_length=160)
    subject_type: SubjectType
    subject_id: str = Field(min_length=1, max_length=60)
    direction: Direction = "long"
    horizon: Horizon = "12M"
    conviction: int = Field(default=3, ge=1, le=5)
    status: Status = "idea"
    origin: Origin | None = None


class ThesisPatch(ThesisFields):
    pass


class JournalIn(BaseModel):
    body_md: str = Field(min_length=1, max_length=50_000)
    entry_date: date | None = None
    kind: Literal["note", "catalyst", "review", "status_change", "alert", "post_mortem"] = "note"
    links: list[dict[str, Any]] | None = Field(default=None, max_length=50)


class JournalPatch(BaseModel):
    body_md: str | None = Field(default=None, min_length=1, max_length=50_000)
    entry_date: date | None = None
    kind: Literal["note", "catalyst", "review", "status_change", "alert", "post_mortem"] | None = None
    links: list[dict[str, Any]] | None = Field(default=None, max_length=50)


class ReviewIn(BaseModel):
    decision: Literal["hold", "add", "trim", "close", "none"] = "hold"
    notes_md: str | None = Field(default=None, max_length=50_000)
    conviction_after: int | None = Field(default=None, ge=1, le=5)
    next_review_date: date | None = None


class SnapshotIn(BaseModel):
    note: str | None = Field(default=None, max_length=400)


# ------------------------------------------------------------------------------------------------ helpers


def _get(thesis_id: str) -> dict[str, Any]:
    t = store.get_thesis(thesis_id)
    if t is None:
        raise not_found(f"unknown thesis {thesis_id!r}")
    return t


def _normalize_lists(f: dict[str, Any]) -> None:
    if f.get("kpis") is not None:
        seen = set()
        for k in f["kpis"]:
            k["kpi_id"] = k.get("kpi_id") or _sid("k")
            if k["kpi_id"] in seen:
                raise invalid(f"duplicate kpi_id {k['kpi_id']!r}")
            seen.add(k["kpi_id"])
            err = tev.validate_kpi(k)
            if err:
                raise invalid(f"KPI {k['label']!r}: {err}")
    for key, idk in (("catalysts", "catalyst_id"), ("invalidation", "condition_id")):
        for c in f.get(key) or []:
            c[idk] = c.get(idk) or _sid(idk[0])
            if c.get("date") is not None:
                c["date"] = str(c["date"])
    if f.get("key_drivers") is not None:
        f["key_drivers"] = [s.strip() for s in f["key_drivers"] if s and s.strip()]
    if f.get("tags") is not None:
        f["tags"] = sorted({s.strip().lower() for s in f["tags"] if s and s.strip()})


def _check_bands(t: dict[str, Any]) -> None:
    lo, hi, tg, sp, d = t.get("entry_lo"), t.get("entry_hi"), t.get("target"), t.get("stop"), t.get("direction")
    for k in ("entry_lo", "entry_hi", "target", "stop"):
        if t.get(k) is not None and t[k] <= 0:
            raise invalid(f"{k} must be > 0")
    if lo is not None and hi is not None and lo > hi:
        raise invalid("entry_lo must be <= entry_hi")
    if tg is not None and sp is not None and d in ("long", "short"):
        if (d == "long" and tg <= sp) or (d == "short" and tg >= sp):
            raise invalid(f"a {d} thesis needs target {'above' if d == 'long' else 'below'} stop")


def _close_price(db: Db, pid: str | None) -> float | None:
    b = tev.latest_bar(db, pid)
    return fnum(b["close"]) if b else None


def _sync(db: Db, t: dict[str, Any]) -> dict[str, Any]:
    """Regenerate the linked alert rules and re-evaluate the automatic status."""
    info = tev.subject_info(db, t["subject_type"], t["subject_id"]) or {}
    store.sync_alerts(t["thesis_id"], trules.build_rules(t, info), enabled=t["status"] != "closed")
    if t.get("review_every_days") and t["status"] != "closed" and t.get("next_review_date"):
        pend = store.pending_review(t["thesis_id"])
        if pend is None or pend["due_date"] != t["next_review_date"]:
            store.schedule_review(t["thesis_id"], date.fromisoformat(t["next_review_date"]))
    elif not t.get("review_every_days") or t["status"] == "closed":
        store.schedule_review(t["thesis_id"], None)
    # fires due thesis_* alerts too (status turning at risk / invalidated, KPI breaches, reviews, catalysts):
    # idempotent per data date, so the nightly thesis_eval run does not repeat them
    tev.evaluate_and_store(db, store.get_thesis(t["thesis_id"]) or t, fire=True)
    return store.get_thesis(t["thesis_id"])  # type: ignore[return-value]


def _alert_out(a: dict[str, Any]) -> dict[str, Any]:
    return {"alert_id": a["alert_id"], "rule_type": a["rule_type"], "security_id": a.get("security_id"),
            "params": a.get("params") or {}, "enabled": bool(a["enabled"]), "cooldown_days": int(a["cooldown_days"]),
            "last_fired_at": iso(a.get("last_fired_at"))}


def _card(db: Db, t: dict[str, Any]) -> dict[str, Any]:
    b = tev.latest_bar(db, t.get("price_id"))
    last = fnum(b["close"]) if b else None
    ip = fnum(t.get("inception_price"))
    ret = None
    if t["status"] == "closed" and ip and t.get("exit_price"):
        ret = tperf.sign(t["direction"]) * (float(t["exit_price"]) / ip - 1)
    elif ip and last:
        ret = tperf.sign(t["direction"]) * (last / ip - 1)
    return {"last_price": last, "price_date": iso(b["date"]) if b else None, "return_since_inception": ret}


def _thesis_out(t: dict[str, Any], card: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {k: t.get(k) for k in (
        "thesis_id", "title", "subject_type", "subject_id", "subject_label", "security_id", "price_id", "price_currency",
        "direction", "horizon", "horizon_date", "conviction", "status", "thesis_md", "key_drivers", "catalysts", "kpis",
        "entry_lo", "entry_hi", "target", "stop", "invalidation", "scenario_id", "study_id", "studio_run_id",
        "backtest_strategy_id", "backtest_run_id", "benchmark_id", "sector_benchmark_id", "tags", "origin",
        "review_every_days", "next_review_date", "inception_date", "inception_price", "opened_at", "closed_at",
        "exit_price", "close_reason", "outcome", "post_mortem_md", "auto_status", "status_reasons", "evaluated_at",
        "created_at", "updated_at")}
    out["conviction"] = int(out["conviction"] or 3)
    for k in ("key_drivers", "catalysts", "kpis", "invalidation", "tags", "status_reasons"):
        out[k] = out[k] or []
    out["auto_status"] = out["auto_status"] or "unknown"
    if card is not None:
        out["card"] = card
    return out


def _detail(db: Db, t: dict[str, Any]) -> dict[str, Any]:
    out = _thesis_out(t, _card(db, t))
    out["alerts"] = [_alert_out(a) for a in store.thesis_alerts(t["thesis_id"])]
    out["pending_review"] = store.pending_review(t["thesis_id"])
    out["snapshots"] = store.list_snapshots(t["thesis_id"])
    out["recent_events"] = store.thesis_events(t["thesis_id"], 20)
    out["n_journal"] = len(store.list_journal(t["thesis_id"]))
    return out


def _status_out(t: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    return {"thesis_id": t["thesis_id"], "auto_status": ev["auto_status"], "reasons": ev["reasons"],
            "kpis": ev["kpis"], "price": ev["price"], "price_date": iso(ev["price_date"]),
            "price_currency": ev.get("price_currency"), "score": ev.get("score"), "grade": ev.get("grade"),
            "as_of": iso(ev["as_of"]), "events_fired": ev.get("events_fired", 0), "model_based": True}


def _journal(thesis_id: str, kind: str, body: str, d: date | None = None) -> None:
    store.add_entry(thesis_id, body, kind, d)


# ------------------------------------------------------------------------------------------------ board / stats


@router.get("/theses", operation_id="listTheses")
def list_theses(status: Status | None = None, subject_type: SubjectType | None = None, direction: Direction | None = None,
                auto_status: AutoStatus | None = None, q: str | None = Query(None, max_length=80),
                tag: str | None = Query(None, max_length=40), min_conviction: int | None = Query(None, ge=1, le=5),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    all_ = store.list_theses()
    items = []
    for t in all_:
        if (status and t["status"] != status) or (subject_type and t["subject_type"] != subject_type) \
                or (direction and t["direction"] != direction) or (tag and tag.lower() not in (t.get("tags") or [])) \
                or (auto_status and (t.get("auto_status") or "unknown") != auto_status) \
                or (min_conviction and int(t["conviction"]) < min_conviction):
            continue
        if q and q.lower() not in " ".join(str(t.get(k) or "") for k in ("title", "subject_label", "subject_id",
                                                                           "thesis_md")).lower():
            continue
        items.append(_thesis_out(t, _card(db, t)))
    return {"items": items, "total": len(items),
            "counts": {s: sum(1 for t in items if t["status"] == s) for s in ("idea", "active", "closed")},
            "tags": sorted({x for t in all_ for x in (t.get("tags") or [])}), "as_of": iso(db.as_of),
            "data_status": "ok"}


@router.get("/theses/stats", operation_id="getThesisStats")
def thesis_stats() -> dict[str, Any]:
    ts = store.list_theses()
    snaps = {t["thesis_id"]: store.list_snapshots(t["thesis_id"]) for t in ts if t["status"] == "closed"}
    return {**tperf.hit_stats(ts, snaps), "data_status": "ok"}


@router.get("/theses/subject-defaults", operation_id="getThesisSubjectDefaults")
def subject_defaults(subject_type: SubjectType, subject_id: str = Query(min_length=1, max_length=60),
                     db: Db = Depends(get_db)) -> dict[str, Any]:
    info = tev.subject_info(db, subject_type, subject_id)
    if info is None:
        raise not_found(f"unknown {subject_type} {subject_id!r}")
    b = tev.latest_bar(db, info["price_id"])
    cats = []
    if info.get("security_id"):
        for r in db.all("""SELECT expected_date, fiscal_period, source_label FROM earn_calendar
                           WHERE security_id = ? AND expected_date >= ? ORDER BY expected_date LIMIT 4""",
                        [info["security_id"], db.as_of]) if "expected_date" in tev._cols(db, "earn_calendar") else []:
            cats.append({"label": f"Earnings {r['fiscal_period'] or ''}".strip() + f" ({r['source_label']})",
                         "date": iso(r["expected_date"]), "kind": "earnings", "source": "earnings_calendar"})
    sugg = [{"label": "Composite score", "kind": "metric", "ref": "composite", "op": "gte", "threshold": 50.0}] \
        if subject_type in ("security", "sector", "country") else []
    return {"subject_type": subject_type, "subject_id": info["subject_id"], "subject_label": info["subject_label"],
            "security_id": info["security_id"], "price_id": info["price_id"], "price_currency": info["price_currency"],
            "benchmark_id": info["benchmark_id"], "sector_benchmark_id": info["sector_benchmark_id"],
            "region": info["region"], "country": info["country"], "sector_id": info["sector_id"],
            "last_price": fnum(b["close"]) if b else None, "price_date": iso(b["date"]) if b else None,
            "suggested_catalysts": cats, "suggested_kpis": sugg, "as_of": iso(db.as_of)}


# ------------------------------------------------------------------------------------------------ CRUD


@router.post("/theses", operation_id="createThesis", status_code=201)
def create_thesis(body: ThesisCreate, db: Db = Depends(get_db)) -> JSONResponse:
    info = tev.subject_info(db, body.subject_type, body.subject_id)
    if info is None:
        raise invalid(f"unknown {body.subject_type} {body.subject_id!r}")
    f = body.model_dump(mode="json", exclude_none=True)
    _normalize_lists(f)
    f["subject_id"] = info["subject_id"]
    f.setdefault("subject_label", info["subject_label"])
    f["security_id"] = info["security_id"]
    for k in ("price_id", "benchmark_id", "sector_benchmark_id"):
        if k not in f:
            f[k] = info[k]
    if f.get("price_id") and f["price_id"] != info["price_id"]:
        if not tev.has_prices(db, f["price_id"]):
            raise invalid(f"price_id {f['price_id']!r} has no prices in the serving snapshot")
    pb = tev.latest_bar(db, f.get("price_id"))
    f["price_currency"] = pb["currency"] if pb else info["price_currency"]
    if f["status"] == "closed":
        raise invalid("a new thesis starts as idea or active")
    _check_bands(f)
    today = db.as_of
    f["inception_date"] = today.isoformat()
    f["inception_price"] = fnum(pb["close"]) if pb else None
    f["horizon_date"] = iso(store.horizon_date(today, f["horizon"]))
    if f["status"] == "active":
        f["opened_at"] = store.app_state.now()
    if f.get("review_every_days"):
        f["next_review_date"] = iso(tperf.next_review(today, f["review_every_days"]))
    t = store.create_thesis(f)
    tev.capture(db, t, "creation")
    if t["status"] == "active":
        tev.capture(db, t, "activation")
    origin = (f.get("origin") or {}).get("kind", "manual")
    _journal(t["thesis_id"], "status_change", f"Thesis created as **{t['status']}** (origin: {origin}).", today)
    t = _sync(db, t)
    return JSONResponse(_detail(db, t), status_code=201)


@router.get("/theses/{thesis_id}", operation_id="getThesis")
def get_thesis(thesis_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    return _detail(db, _get(thesis_id))


@router.patch("/theses/{thesis_id}", operation_id="updateThesis")
def update_thesis(thesis_id: str, body: ThesisPatch, db: Db = Depends(get_db)) -> dict[str, Any]:
    cur = _get(thesis_id)
    f = body.model_dump(mode="json", exclude_unset=True)
    for k in ("catalysts", "kpis", "invalidation"):  # nested items keep their defaults
        if f.get(k) is not None:
            f[k] = [x.model_dump(mode="json") for x in getattr(body, k)]
    for k in ("title", "direction", "horizon", "conviction", "status"):
        if k in f and f[k] is None:
            raise invalid(f"{k} cannot be null")
    _normalize_lists(f)
    merged = {**cur, **f}
    _check_bands(merged)
    if f.get("price_id") and f["price_id"] != cur.get("price_id"):
        pb = tev.latest_bar(db, f["price_id"])
        if pb is None:
            raise invalid(f"price_id {f['price_id']!r} has no prices in the serving snapshot")
        f["price_currency"] = pb["currency"]
    today = db.as_of
    new, old = merged["status"], cur["status"]
    notes = []
    if new != old:
        if new == "active":
            f["opened_at"] = store.app_state.now()
            if old == "idea":  # performance is measured from activation
                f["inception_date"] = today.isoformat()
                f["inception_price"] = _close_price(db, merged.get("price_id"))
            f.update({"closed_at": None, "exit_price": None, "close_reason": None, "outcome": None} if old == "closed" else {})
        if new == "closed":
            f["next_review_date"] = None  # no reviews / reminders once closed
            f["closed_at"] = store.app_state.now()
            f["exit_price"] = _close_price(db, merged.get("price_id"))
            f["close_reason"] = merged.get("close_reason") or "other"
        if new == "idea" and old == "closed":
            f.update({"closed_at": None, "exit_price": None, "close_reason": None, "outcome": None})
        notes.append(f"Status **{old} -> {new}**" + (f" ({f['close_reason']})" if new == "closed" else "") + ".")
    if "horizon" in f or "inception_date" in f:
        incep = date.fromisoformat(str(f.get("inception_date") or merged["inception_date"])[:10]) \
            if (f.get("inception_date") or merged.get("inception_date")) else today
        f["horizon_date"] = iso(store.horizon_date(incep, merged["horizon"]))
    if "review_every_days" in f or (new != old and old == "closed"):
        f["next_review_date"] = iso(tperf.next_review(today, merged.get("review_every_days"))) if new != "closed" else None
    t = store.update_thesis(thesis_id, f)
    assert t is not None
    if new != old and new == "active":
        tev.capture(db, t, "activation")
    if new != old and new == "closed":
        p = tperf.performance(db, t)
        outcome = t.get("outcome") or tperf.derive_outcome(p.get("thesis_return"))
        upd: dict[str, Any] = {"outcome": outcome}
        snaps = store.list_snapshots(thesis_id)
        first = next((s for s in snaps if s["kind"] == "activation"), snaps[0] if snaps else None)
        last = tev.capture(db, t, "close", extra={"performance": tperf.perf_summary(p)})
        if not (t.get("post_mortem_md") or "").strip():
            upd["post_mortem_md"] = tex.post_mortem_template({**t, "outcome": outcome}, p, first, last)
        t = store.update_thesis(thesis_id, upd) or t
    for n in notes:
        _journal(thesis_id, "status_change", n, today)
    return _detail(db, _sync(db, t))


@router.delete("/theses/{thesis_id}", operation_id="deleteThesis", status_code=204)
def delete_thesis(thesis_id: str) -> Response:
    if not store.delete_thesis(thesis_id):
        raise not_found(f"unknown thesis {thesis_id!r}")
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ journal


@router.get("/theses/{thesis_id}/journal", operation_id="listThesisJournal")
def list_journal(thesis_id: str) -> list[dict[str, Any]]:
    _get(thesis_id)
    return store.list_journal(thesis_id)


@router.post("/theses/{thesis_id}/journal", operation_id="addThesisJournalEntry", status_code=201)
def add_journal(thesis_id: str, body: JournalIn) -> JSONResponse:
    _get(thesis_id)
    e = store.add_entry(thesis_id, body.body_md, body.kind, body.entry_date, body.links)
    return JSONResponse(e, status_code=201)


def _entry(thesis_id: str, entry_id: str) -> dict[str, Any]:
    e = store.get_entry(entry_id)
    if e is None or e["thesis_id"] != thesis_id:
        raise not_found(f"unknown journal entry {entry_id!r}")
    return e


@router.patch("/theses/{thesis_id}/journal/{entry_id}", operation_id="updateThesisJournalEntry")
def update_journal(thesis_id: str, entry_id: str, body: JournalPatch) -> dict[str, Any]:
    _entry(thesis_id, entry_id)
    e = store.update_entry(entry_id, body.model_dump(exclude_unset=True, exclude_none=True))
    assert e is not None
    return e


@router.delete("/theses/{thesis_id}/journal/{entry_id}", operation_id="deleteThesisJournalEntry", status_code=204)
def delete_journal(thesis_id: str, entry_id: str) -> Response:
    _entry(thesis_id, entry_id)
    store.delete_entry(entry_id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ reviews / snapshots


@router.get("/theses/{thesis_id}/reviews", operation_id="listThesisReviews")
def list_reviews(thesis_id: str) -> dict[str, Any]:
    t = _get(thesis_id)
    return {"thesis_id": thesis_id, "review_every_days": t.get("review_every_days"),
            "next_review_date": t.get("next_review_date"), "items": store.list_reviews(thesis_id),
            "snapshots": store.list_snapshots(thesis_id)}


@router.post("/theses/{thesis_id}/reviews", operation_id="completeThesisReview", status_code=201)
def complete_review(thesis_id: str, body: ReviewIn, db: Db = Depends(get_db)) -> JSONResponse:
    t = _get(thesis_id)
    if t["status"] == "closed":
        raise invalid("the thesis is closed")
    snap = tev.capture(db, t, "review")
    r = store.complete_review(thesis_id, body.decision, body.notes_md, body.conviction_after, snap["snapshot_id"])
    upd: dict[str, Any] = {}
    if body.conviction_after:
        upd["conviction"] = body.conviction_after
    nxt = body.next_review_date or tperf.next_review(db.as_of, t.get("review_every_days"))
    upd["next_review_date"] = iso(nxt)
    t = store.update_thesis(thesis_id, upd) or t
    conv = f", conviction {t['conviction']}/5" if body.conviction_after else ""
    _journal(thesis_id, "review", f"Review: **{body.decision}**{conv}." + (f"\n\n{body.notes_md}" if body.notes_md else ""),
             db.as_of)
    _sync(db, t)
    return JSONResponse(r, status_code=201)


@router.post("/theses/{thesis_id}/snapshots", operation_id="captureThesisSnapshot", status_code=201)
def capture_snapshot(thesis_id: str, body: SnapshotIn | None = None, db: Db = Depends(get_db)) -> JSONResponse:
    t = _get(thesis_id)
    s = tev.capture(db, t, "manual", extra={"note": body.note} if body and body.note else None)
    return JSONResponse(s, status_code=201)


# ------------------------------------------------------------------------------------------------ status / performance


@router.get("/theses/{thesis_id}/status", operation_id="getThesisStatus")
def get_status(thesis_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    t = _get(thesis_id)
    return _status_out(t, tev.evaluate(db, t))


@router.post("/theses/{thesis_id}/evaluate", operation_id="evaluateThesis")
def evaluate_now(thesis_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    """Run the nightly evaluator for one thesis now: persist the automatic status and fire due thesis_* alerts."""
    t = _get(thesis_id)
    return _status_out(t, tev.evaluate_and_store(db, t, fire=True))


@router.get("/theses/{thesis_id}/performance", operation_id="getThesisPerformance")
def get_performance(thesis_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    t = _get(thesis_id)
    return {"thesis_id": thesis_id, **tperf.performance(db, t)}


@router.get("/theses/{thesis_id}/timeline", operation_id="getThesisTimeline")
def get_timeline(thesis_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    """Price bars of price_id (unadjusted close, the unit of the bands) around the thesis life, the bands and the
    markers to draw on it: journal entries, triggered alerts, catalysts, reviews and snapshots."""
    t = _get(thesis_id)
    created = date.fromisoformat(t["created_at"][:10])
    incep = date.fromisoformat(t["inception_date"]) if t.get("inception_date") else created
    end = date.fromisoformat(t["closed_at"][:10]) + timedelta(days=30) if t.get("closed_at") else db.as_of
    end = min(end, db.as_of)
    start = min(created, incep) - timedelta(days=270)
    interval: Literal["D", "W"] = "W" if (end - start).days > 3 * 365 else "D"
    bars = [{"date": iso(b["date"]), "close": fnum(b["c"])} for b in
            (pricebars.bars(db, t["price_id"], start, end, interval, adjusted=False) if t.get("price_id") else [])
            if b.get("c") is not None]
    markers: list[dict[str, Any]] = []
    for e in store.list_journal(thesis_id):
        markers.append({"date": e["entry_date"], "kind": "journal", "subkind": e["kind"],
                        "label": e["body_md"].strip().splitlines()[0][:140] if e["body_md"].strip() else e["kind"],
                        "ref": e["entry_id"]})
    for ev in store.thesis_events(thesis_id, 300):
        markers.append({"date": ev["as_of_date"], "kind": "alert", "subkind": ev["rule_type"], "label": ev["message"][:200],
                        "ref": ev["event_id"]})
    for c in t.get("catalysts") or []:
        if c.get("date"):
            markers.append({"date": c["date"], "kind": "catalyst", "subkind": c.get("kind", "custom"),
                            "label": c["label"], "ref": c.get("catalyst_id")})
    for r in store.list_reviews(thesis_id):
        markers.append({"date": r["due_date"] if r["status"] != "completed" else (r["completed_at"] or r["due_date"])[:10],
                        "kind": "review", "subkind": r["status"], "label": f"Review {r['status']}"
                        + (f": {r['decision']}" if r.get("decision") else ""), "ref": r["review_id"]})
    markers.sort(key=lambda m: m["date"])
    return {"thesis_id": thesis_id, "price_id": t.get("price_id"), "currency": t.get("price_currency"),
            "interval": interval, "bars": bars,
            "bands": {k: t.get(k) for k in ("entry_lo", "entry_hi", "target", "stop", "inception_price")},
            "inception_date": t.get("inception_date"), "closed_date": (t.get("closed_at") or "")[:10] or None,
            "markers": markers, "as_of": iso(db.as_of), "data_status": "ok" if bars or not t.get("price_id") else "pending"}


@router.get("/theses/{thesis_id}/evidence", operation_id="getThesisEvidence")
def get_evidence(thesis_id: str, days: int = Query(365, ge=30, le=2600), db: Db = Depends(get_db)) -> dict[str, Any]:
    """Linked evidence for the subject over the thesis life (at least `days` back): filings, news stories, event
    shocks (GDELT, model-based z), earnings (history + upcoming) and macro surprises (model-based expectations)."""
    t = _get(thesis_id)
    start = min(date.fromisoformat(t["created_at"][:10]), db.as_of - timedelta(days=days))
    sec = t.get("security_id")
    out: dict[str, Any] = {"thesis_id": thesis_id, "start": iso(start), "as_of": iso(db.as_of), "filings": [],
                           "stories": [], "shocks": [], "earnings": [], "upcoming_earnings": [], "macro_surprises": []}
    st = t["subject_type"]
    if sec:
        iss = db.scalar("SELECT issuer_id FROM sec WHERE security_id = ?", [sec])
        if iss and "filing_id" in tev._cols(db, "filings"):
            out["filings"] = [{"filing_id": r["filing_id"], "form_type": r["form_type"], "form_class": r["form_class"],
                               "knowledge_date": iso(r["knowledge_date"]), "period_end": iso(r.get("period_end")),
                               "url": r.get("url")}
                              for r in db.all("""SELECT filing_id, form_type, form_class, knowledge_date, period_end, url
                                                 FROM filings WHERE issuer_id = ? AND knowledge_date >= ?
                                                 ORDER BY knowledge_date DESC LIMIT 40""", [iss, start])]
        li = tev._ev2_label(db, "issuer", sec)
        if li is not None and "story_id" in tev._cols(db, "ev2_story_issuer"):
            out["stories"] = [{"story_id": r["story_id"], "title": r["title"], "last_date": iso(r["last_date"]),
                               "n_articles": int(r["n_articles"] or 0), "tone": fnum(r["tone"]),
                               "url": (list(r["urls"] or []) or [None])[0]}
                              for r in db.all("""SELECT s.story_id, s.title, s.last_date, s.n_articles, s.tone, s.urls
                                                 FROM ev2_story_issuer i JOIN ev2_stories s USING (story_id)
                                                 WHERE i.iid = ? AND s.last_date >= ?
                                                 ORDER BY s.last_date DESC, s.salience DESC LIMIT 30""", [li, start])]
        if "release_date" in tev._cols(db, "earn_events"):
            out["earnings"] = [{"period_end": iso(r["period_end"]), "fiscal_period": r["fiscal_period"],
                                "release_date": iso(r["release_date"]), "eps_surprise_z": fnum(r.get("eps_surprise_z")),
                                "car_m1_p1": fnum(r.get("car_m1_p1")), "date_source": r["date_source"]}
                               for r in db.all("""SELECT * FROM earn_events WHERE security_id = ? AND release_date >= ?
                                                  ORDER BY release_date DESC LIMIT 12""", [sec, start - timedelta(days=200)])]
        if "expected_date" in tev._cols(db, "earn_calendar"):
            out["upcoming_earnings"] = [{"expected_date": iso(r["expected_date"]), "fiscal_period": r["fiscal_period"],
                                         "date_source": r["date_source"], "source_label": r["source_label"]}
                                        for r in db.all("""SELECT * FROM earn_calendar WHERE security_id = ?
                                                           AND expected_date >= ? ORDER BY expected_date LIMIT 4""",
                                                        [sec, db.as_of])]
    lt, lid = {"security": ("issuer", sec), "country": ("country", t["subject_id"]),
               "sector": ("sector", t["subject_id"])}.get(st, (None, None))
    if st == "macro_theme":
        cc, _ = trules.parse_macro_subject(t["subject_id"])
        lt, lid = ("country", cc) if cc else (None, None)
    if lt and lid:
        out["shocks"] = [{**s, "date": iso(s["date"])} for s in
                         tev.event_shocks(db, lt, lid, db.as_of, 2.5, start=start)][-60:][::-1]
    if st in ("country", "macro_theme") and "surprise_z" in tev._cols(db, "macro_surprises"):
        cc, theme = (t["subject_id"].upper(), None) if st == "country" else trules.parse_macro_subject(t["subject_id"])
        conds, params = ["release_date >= ?", "surprise_z IS NOT NULL", "abs(surprise_z) >= 1.5"], [start]
        if cc:
            conds.append("country = ?")
            params.append(cc)
        if theme in trules.MACRO_THEMES:
            conds.append("theme = ?")
            params.append(theme)
        out["macro_surprises"] = [{"release_date": iso(r["release_date"]), "country": r["country"],
                                   "series_id": r["series_id"], "theme": r["theme"], "actual": fnum(r["actual"]),
                                   "expectation": fnum(r["expectation"]), "surprise_z": fnum(r["surprise_z"])}
                                  for r in db.all(f"""SELECT * FROM macro_surprises WHERE {' AND '.join(conds)}
                                                      ORDER BY release_date DESC LIMIT 40""", params)]
    out["data_status"] = "ok"
    return out


@router.get("/theses/{thesis_id}/export.md", operation_id="exportThesisMarkdown")
def export_markdown(thesis_id: str, db: Db = Depends(get_db)) -> PlainTextResponse:
    t = _get(thesis_id)
    md = tex.to_markdown(t, store.list_journal(thesis_id), store.list_reviews(thesis_id), store.list_snapshots(thesis_id),
                         tperf.performance(db, t), tev.evaluate(db, t), store.thesis_alerts(thesis_id),
                         store.thesis_events(thesis_id, 100))
    fn = "".join(c if c.isalnum() or c in "-_" else "_" for c in t["title"])[:60] or thesis_id
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{fn}.md"'})
