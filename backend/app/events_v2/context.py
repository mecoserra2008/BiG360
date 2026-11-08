"""Builds (and caches on disk) the per-process labelling context: alias table + issuer meta (E1 -> E2)."""

from __future__ import annotations

import pickle
import time
from pathlib import Path

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.events_v2 import dicts
from app.events_v2.parse import WorkerContext


def state_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.state_dir / "events_v2"
    d.mkdir(parents=True, exist_ok=True)
    return d


def issuer_meta(idict: pl.DataFrame) -> pl.DataFrame:
    if idict.is_empty():
        return pl.DataFrame(schema={"iid": pl.Int32, "country": pl.Utf8, "sector_mask": pl.Int16, "name": pl.Utf8,
                                    "name_norm": pl.Utf8, "ticker": pl.Utf8, "issuer_id": pl.Utf8})
    tick = [orjson.loads(x).get("ticker") if x else None for x in idict.get_column("extra").to_list()]
    return idict.select(pl.col("id").cast(pl.Int32).alias("iid"), pl.col("grp").alias("country"),
                        pl.col("sector_mask").fill_null(0).cast(pl.Int16), "name",
                        dicts.norm_org_expr(pl.col("name").fill_null("")).alias("name_norm"),
                        pl.Series("ticker", tick, pl.Utf8), pl.col("code").alias("issuer_id"))


def build_context(settings: Settings | None = None, refresh: bool = True) -> tuple[WorkerContext, Path]:
    """Sync dictionaries, build the alias table and pickle the WorkerContext for pool initialisers."""
    s = settings or get_settings()
    path = state_dir(s) / "worker_ctx.pkl"
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < 20 * 3600:
        with open(path, "rb") as fh:
            return pickle.load(fh), path  # noqa: S301 - own file
    dicts.ensure_static(s)
    idict = dicts.sync_issuers(s)
    aliases = dicts.build_alias_table(s, issuer_dict=idict)
    ctx = WorkerContext.build(aliases, issuer_meta(idict))
    try:  # legacy gdelt_gkg dictionary (nightly compatibility outputs)
        from app.ingestion.gdelt_gkg import load_org_dictionary  # noqa: PLC0415

        ctx.legacy_dict = load_org_dictionary(s)
    except Exception:  # noqa: BLE001
        ctx.legacy_dict = None
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(ctx, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return ctx, path
