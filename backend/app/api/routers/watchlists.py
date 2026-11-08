"""Watchlists (tag: watchlists): CRUD in app_state.duckdb; the detail view joins items with the serving snapshot."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api import app_state
from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import check_profile, iso, not_found
from app.api.routers.screener import rows_for_ids

router = APIRouter(tags=["watchlists"], route_class=FastRoute)


class WatchlistUpsert(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = None


class WatchlistItemCreate(BaseModel):
    security_id: str = Field(..., min_length=1)
    note: str | None = None


def _wl_out(w: dict[str, Any]) -> dict[str, Any]:
    return {"watchlist_id": w["watchlist_id"], "name": w["name"], "description": w.get("description"),
            "item_count": int(w.get("item_count") or 0), "created_at": iso(w["created_at"]), "updated_at": iso(w["updated_at"])}


@router.get("/watchlists", operation_id="listWatchlists")
def list_watchlists() -> list[dict[str, Any]]:
    return [_wl_out(w) for w in app_state.list_watchlists()]


@router.post("/watchlists", operation_id="createWatchlist", status_code=201)
def create_watchlist(body: WatchlistUpsert) -> JSONResponse:
    if not body.name:
        from app.api.routers._common import invalid

        raise invalid("name is required")
    return JSONResponse(_wl_out(app_state.create_watchlist(body.name, body.description)), status_code=201)


@router.get("/watchlists/{watchlist_id}", operation_id="getWatchlist")
def get_watchlist(watchlist_id: str, profile_id: str = "balanced", db: Db = Depends(get_db)) -> dict[str, Any]:
    pid = check_profile(profile_id)
    w = app_state.get_watchlist(watchlist_id)
    if w is None:
        raise not_found(f"unknown watchlist {watchlist_id!r}")
    items = app_state.watchlist_items(watchlist_id)
    ids = [i["security_id"] for i in items]
    rows = rows_for_ids(db, ids, pid) if ids else []
    order = {sid: n for n, sid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["security"]["security_id"], 1 << 30))
    notes = {i["security_id"]: i.get("note") for i in items}
    for r in rows:
        r["values"]["note"] = notes.get(r["security"]["security_id"])
    missing = [sid for sid in ids if sid not in {r["security"]["security_id"] for r in rows}]
    return {"watchlist": _wl_out(w), "rows": rows, "missing_security_ids": missing}


@router.patch("/watchlists/{watchlist_id}", operation_id="updateWatchlist")
def update_watchlist(watchlist_id: str, body: WatchlistUpsert) -> dict[str, Any]:
    w = app_state.update_watchlist(watchlist_id, body.model_dump(exclude_unset=True))
    if w is None:
        raise not_found(f"unknown watchlist {watchlist_id!r}")
    return _wl_out(w)


@router.delete("/watchlists/{watchlist_id}", operation_id="deleteWatchlist", status_code=204)
def delete_watchlist(watchlist_id: str) -> Response:
    if not app_state.delete_watchlist(watchlist_id):
        raise not_found(f"unknown watchlist {watchlist_id!r}")
    return Response(status_code=204)


@router.post("/watchlists/{watchlist_id}/items", operation_id="addWatchlistItem", status_code=201)
def add_watchlist_item(watchlist_id: str, body: WatchlistItemCreate, db: Db = Depends(get_db)) -> JSONResponse:
    if not db.snap.is_empty and db.scalar("SELECT count(*) FROM sec") and \
            not db.scalar("SELECT 1 FROM sec WHERE security_id = ?", [body.security_id]):
        raise not_found(f"unknown security_id {body.security_id!r}")
    it = app_state.add_item(watchlist_id, body.security_id, body.note)
    if it is None:
        raise not_found(f"unknown watchlist {watchlist_id!r}")
    return JSONResponse({**it, "added_at": iso(it["added_at"])}, status_code=201)


@router.delete("/watchlists/{watchlist_id}/items/{security_id}", operation_id="removeWatchlistItem", status_code=204)
def remove_watchlist_item(watchlist_id: str, security_id: str) -> Response:
    if not app_state.remove_item(watchlist_id, security_id):
        raise not_found(f"{security_id!r} is not in watchlist {watchlist_id!r}")
    return Response(status_code=204)
