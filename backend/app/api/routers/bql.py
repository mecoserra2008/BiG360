"""BQL-lite (tag: bql; roadmap v3 B1): POST /bql, POST /bql/explain, GET /bql/fields, POST /bql/export, GET /bql/resolve,
saved queries (app_state bql_saved). Engine: app.bql (grammar / fields / compiler / engine)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import fnum, not_found, sec_ref, sec_ref_sql
from app.bql import compiler as C
from app.bql import fields as F
from app.bql import store
from app.bql import tickers as T
from app.bql.engine import BqlTimeout, explain, run, to_csv, to_json, to_xlsx, xlsx_available
from app.bql.grammar import BqlError, parse

router = APIRouter(tags=["bql"], route_class=FastRoute)
PROBLEM = "application/problem+json"


def _problem(status: int, title: str, e: Exception) -> JSONResponse:
    pos = getattr(e, "pos", None)
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status, "detail": str(e)}
    if pos is not None:
        body["errors"] = [{"loc": ["query", str(pos)], "msg": getattr(e, "msg", str(e))}]
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def _guard(fn: Any) -> Any:
    try:
        return fn()
    except BqlTimeout as e:
        return _problem(408, "Query timed out", e)
    except BqlError as e:
        return _problem(422, "Invalid BQL query", e)


class BqlRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    max_rows: int = Field(5000, ge=1, le=100_000)
    use_cache: bool = True


class BqlExportRequest(BqlRequest):
    format: Literal["csv", "xlsx", "json"] = "csv"


class BqlSavedIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=8000)
    kind: Literal["query", "screen"] = "query"
    description: str | None = Field(None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class BqlSavedPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    query: str | None = Field(None, min_length=1, max_length=8000)
    kind: Literal["query", "screen"] | None = None
    description: str | None = Field(None, max_length=2000)
    tags: list[str] | None = Field(None, max_length=20)


@router.post("/bql", operation_id="runBql")
def run_bql(body: BqlRequest, db: Db = Depends(get_db)) -> Any:
    return _guard(lambda: run(body.query, db, max_rows=body.max_rows, use_cache=body.use_cache))


@router.post("/bql/explain", operation_id="explainBql")
def explain_bql(body: BqlRequest, db: Db = Depends(get_db)) -> Any:
    return _guard(lambda: explain(body.query, db, max_rows=body.max_rows))


@router.post("/bql/export", operation_id="exportBql")
def export_bql(body: BqlExportRequest, db: Db = Depends(get_db)) -> Any:
    def go() -> Any:
        if body.format == "xlsx" and not xlsx_available():
            raise BqlError("XLSX export needs openpyxl (not installed): use csv or json")
        res = run(body.query, db, max_rows=body.max_rows, use_cache=body.use_cache)
        stamp = res["as_of"]
        if body.format == "csv":
            return Response(to_csv(res), media_type="text/csv",
                            headers={"content-disposition": f'attachment; filename="bql_{stamp}.csv"'})
        if body.format == "json":
            return Response(to_json(res), media_type="application/json",
                            headers={"content-disposition": f'attachment; filename="bql_{stamp}.json"'})
        return Response(to_xlsx(res), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"content-disposition": f'attachment; filename="bql_{stamp}.xlsx"'})

    return _guard(go)


@router.get("/bql/fields", operation_id="listBqlFields")
def list_bql_fields(q: str | None = Query(None, max_length=100), group: str | None = Query(None, max_length=40),
                    limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    total, items = F.search(q, group, limit, offset)
    d = F.dictionary()
    return {
        "total": total, "limit": limit, "offset": offset, "groups": F.groups(),
        "counts": {"fields": len(d), "aliases": sum(len(f.aliases) for f in d.values()),
                   "with_history": sum(1 for f in d.values() if f.hist is not None)},
        "functions": [{"signature": k, "description": v} for k, v in C.FUNCTION_DOCS.items()],
        "items": [f.public() for f in items],
    }


@router.get("/bql/resolve", operation_id="resolveBqlSecurity")
def resolve_bql_security(q: str = Query(..., min_length=1, max_length=64), db: Db = Depends(get_db)) -> dict[str, Any]:
    """Bloomberg-style reference ('AAPL US Equity', 'SAP GY', 'SPX Index', 'EURUSD Curncy', 'CL1 Comdty') -> security."""
    ref = T.parse_ref(q)
    hit = T.resolve_one(db.cur, q)
    sec = None
    if hit:
        r = db.one(f"SELECT {sec_ref_sql('s')} FROM sec s WHERE s.security_id = ?", [hit["security_id"]])
        sec = sec_ref(r) if r else None
    cls = None
    if sec is not None:
        st = hit.get("security_type") if hit else None
        cls = {"fx": "Curncy", "future": "Comdty", "index": "Index", "etf": "Equity"}.get(st or "", "Equity")
    return {"query": q, "base": ref["base"], "market": ref["market"],
            "yellow_key": ref["yellow"].capitalize() if ref["yellow"] else None,
            "match": hit.get("match") if hit else None, "asset_class": cls, "security": sec,
            "market_cap_usd": fnum(sec["market_cap_usd"]) if sec else None}


def _saved_out(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r.get(k) for k in store.COLS}
    for k in ("created_at", "updated_at", "last_run_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = (v if v.tzinfo else v.replace(tzinfo=UTC)).isoformat()
    out["tags"] = list(out.get("tags") or [])
    return out


def _validate_query(text: str) -> None:
    parse(text)  # syntax only: fields / universes are checked when it runs (the snapshot may change)


_TAG = re.compile(r"^[\w .:-]{1,40}$")


@router.get("/bql/saved", operation_id="listBqlSaved")
def list_bql_saved(kind: Literal["query", "screen"] | None = None) -> list[dict[str, Any]]:
    return [_saved_out(r) for r in store.list_saved(kind)]


@router.post("/bql/saved", operation_id="createBqlSaved", status_code=201)
def create_bql_saved(body: BqlSavedIn) -> Any:
    def go() -> Any:
        _validate_query(body.query)
        if store.name_taken(body.name):
            return JSONResponse({"type": "about:blank", "title": "Conflict", "status": 409,
                                 "detail": f"a saved query named {body.name!r} already exists"},
                                status_code=409, media_type=PROBLEM)
        if any(not _TAG.match(t) for t in body.tags):
            raise BqlError("tags: letters, digits, space . : - _ (<= 40 characters)")
        return JSONResponse(_saved_out(store.create_saved(body.name.strip(), body.query, body.kind, body.description,
                                                          body.tags)), status_code=201)

    return _guard(go)


@router.get("/bql/saved/{query_id}", operation_id="getBqlSaved")
def get_bql_saved(query_id: str) -> dict[str, Any]:
    r = store.get_saved(query_id)
    if r is None:
        raise not_found(f"saved query {query_id} not found")
    return _saved_out(r)


@router.put("/bql/saved/{query_id}", operation_id="updateBqlSaved")
def update_bql_saved(query_id: str, body: BqlSavedPatch) -> Any:
    def go() -> Any:
        if body.query is not None:
            _validate_query(body.query)
        if body.name is not None and store.name_taken(body.name, exclude_id=query_id):
            return JSONResponse({"type": "about:blank", "title": "Conflict", "status": 409,
                                 "detail": f"a saved query named {body.name!r} already exists"},
                                status_code=409, media_type=PROBLEM)
        if body.tags and any(not _TAG.match(t) for t in body.tags):
            raise BqlError("tags: letters, digits, space . : - _ (<= 40 characters)")
        r = store.update_saved(query_id, body.model_dump(exclude_unset=True))
        if r is None:
            raise not_found(f"saved query {query_id} not found")
        return _saved_out(r)

    return _guard(go)


@router.delete("/bql/saved/{query_id}", operation_id="deleteBqlSaved", status_code=204)
def delete_bql_saved(query_id: str) -> Response:
    if not store.delete_saved(query_id):
        raise not_found(f"saved query {query_id} not found")
    return Response(status_code=204)
