"""Entity linking for live items (R1.3 labelling/linking): source-native keys -> issuer / security, cached.

* `resolve(source_id, source_entity_id)` uses app.nlp.common.resolve_entities (identity layer, min confidence 0.9):
  CIK (sec_edgar), LEI (fca_nsm), TSE code (tdnet), HK code (hkex), DART corp code (opendart).
* `tickers(tickers)` maps Polygon tickers through app.ingestion.news._ticker_issuers.
* Results (hits and misses) are cached for `ttl_s` (default 6 h) so the hot path costs a dict lookup.
* `issuer_meta(issuer_id)` -> name / ticker / sector / primary security from app.nlp.common.security_map.
Every lookup fails soft (identity tables missing -> unresolved, never an exception).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)


class Linker:
    def __init__(self, settings: Settings | None = None, ttl_s: float = 6 * 3600):
        self.s = settings or get_settings()
        self.ttl_s = ttl_s
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}
        self._tick: dict[str, tuple[float, str | None]] = {}
        self._meta: dict[str, dict[str, Any]] | None = None
        self._meta_at = 0.0
        self._lock = threading.Lock()

    def available(self) -> bool:
        """True when the identity layer has data (otherwise nothing can be resolved: keep everything)."""
        if getattr(self, "_avail", None) is None:
            try:
                from app.nlp.common import has_data  # noqa: PLC0415

                self._avail = bool(has_data("identifier_map", self.s))
            except Exception:  # noqa: BLE001
                self._avail = False
        return bool(self._avail)

    # ---- native keys
    def resolve_many(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any] | None]:
        now = time.time()
        keys = list(dict.fromkeys(k for k in keys if k[0] and k[1]))
        out: dict[tuple[str, str], dict[str, Any] | None] = {}
        todo = []
        with self._lock:
            for k in keys:
                hit = self._cache.get(k)
                if hit and now - hit[0] < self.ttl_s:
                    out[k] = hit[1]
                else:
                    todo.append(k)
        if todo:
            res: dict[tuple[str, str], dict[str, Any] | None] = {k: None for k in todo}
            try:
                from app.nlp.common import resolve_entities  # noqa: PLC0415

                df = pl.DataFrame({"source_id": [k[0] for k in todo], "source_entity_id": [k[1] for k in todo]},
                                  schema={"source_id": pl.Utf8, "source_entity_id": pl.Utf8})
                r = resolve_entities(df)
                for row in r.iter_rows(named=True):
                    if row.get("issuer_id"):
                        res[(row["source_id"], row["source_entity_id"])] = {
                            "issuer_id": row["issuer_id"], "security_id": row.get("security_id")}
            except Exception as e:  # noqa: BLE001
                log.debug("linker: resolve failed: %s", e)
            with self._lock:
                for k, v in res.items():
                    self._cache[k] = (now, self._decorate(v))
                    out[k] = self._cache[k][1]
        return out

    def resolve(self, source_id: str, source_entity_id: str | None) -> dict[str, Any] | None:
        if not source_entity_id:
            return None
        return self.resolve_many([(source_id, source_entity_id)]).get((source_id, source_entity_id))

    # ---- tickers (Polygon)
    def tickers(self, tickers: Iterable[str]) -> dict[str, str]:
        now = time.time()
        tick = [t for t in dict.fromkeys(tickers) if t]
        out: dict[str, str] = {}
        todo = []
        for t in tick:
            hit = self._tick.get(t)
            if hit and now - hit[0] < self.ttl_s:
                if hit[1]:
                    out[t] = hit[1]
            else:
                todo.append(t)
        if todo:
            got: dict[str, str] = {}
            try:
                from app.ingestion.news import _ticker_issuers  # noqa: PLC0415

                got = _ticker_issuers(todo, self.s)
            except Exception as e:  # noqa: BLE001
                log.debug("linker: ticker lookup failed: %s", e)
            for t in todo:
                self._tick[t] = (now, got.get(t))
                if got.get(t):
                    out[t] = got[t]
        return out

    # ---- issuer metadata
    def _load_meta(self) -> dict[str, dict[str, Any]]:
        if self._meta is not None and time.time() - self._meta_at < self.ttl_s:
            return self._meta
        meta: dict[str, dict[str, Any]] = {}
        try:
            from app.nlp.common import security_map  # noqa: PLC0415

            sm = security_map(self.s)
            cols = sm.columns
            for r in sm.iter_rows(named=True):
                meta[r["issuer_id"]] = {"security_id": r.get("security_id"), "ticker": r.get("ticker"),
                                        "name": r.get("name") if "name" in cols else None,
                                        "sector": r.get("sector_id") if "sector_id" in cols else r.get("sector")}
        except Exception as e:  # noqa: BLE001
            log.debug("linker: security_map unavailable: %s", e)
        self._meta, self._meta_at = meta, time.time()
        return meta

    def issuer_meta(self, issuer_id: str | None) -> dict[str, Any]:
        if not issuer_id:
            return {}
        return self._load_meta().get(issuer_id, {})

    def securities_of(self, issuer_ids: Iterable[str]) -> list[str]:
        m = self._load_meta()
        return [m[i]["security_id"] for i in issuer_ids if i in m and m[i].get("security_id")]

    def _decorate(self, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if not v:
            return v
        meta = self.issuer_meta(v["issuer_id"])
        return {**v, "security_id": v.get("security_id") or meta.get("security_id"), "ticker": meta.get("ticker"),
                "name": meta.get("name")}
