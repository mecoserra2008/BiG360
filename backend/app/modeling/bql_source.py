"""Model Studio dataset source `bql` (roadmap v3 B1): a variable whose `field` is a BQL-lite expression, evaluated
point-in-time on the dataset grid (every grid date D sees only data known at D; cross-sectional functions such as
cs_zscore / rank run over the dataset's entities at each date) on the serving snapshot.

    {"name": "mom_z", "source": "bql", "field": "cs_zscore(chg_pct(px, 3M))"}
    {"name": "val", "source": "bql", "field": "pe_ttm", "options": {"universe": "members('S&P 500')"}}

Security-level panels and cross-sections only use the dataset's entity filter (options.universe = a BQL universe
overrides it); time-series datasets aggregate the panel across the entities per date (entity_agg, default mean).
Non-BQL variables of the same dataset are built by the standalone core and joined on (entity_id, date). Not
available in exported standalone scripts (the BQL engine needs the backend and the serving snapshot)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

import polars as pl

from . import _core
from ._core import SpecError

SOURCE = "bql"
META = {"source": SOURCE, "table": "bql", "title": "BQL query (expression, point-in-time on the serving snapshot)",
        "entity_type": "security", "kind": "knowledge", "layout": "wide", "native_frequency": "D",
        "date_column": None, "pit_column": None, "key_column": None, "options": {}}


def has_bql(spec: dict[str, Any]) -> bool:
    return any(v.get("source") == SOURCE for v in spec.get("variables") or [])


def catalog_source(n_variables: int) -> dict[str, Any]:
    return {**META, "rows": 0, "n_entities": 0, "first_date": None, "last_date": None, "n_variables": n_variables}


def catalog_variables(q: str | None = None) -> list[dict[str, Any]]:
    """Dictionary fields with point-in-time history as Studio variables (field = the BQL mnemonic)."""
    from app.bql import fields as F

    _, items = F.search(q, None, 2000, 0)
    out = []
    for f in items:
        if f.hist is None or f.dtype not in ("number", "bool"):
            continue
        out.append({"source": SOURCE, "field": f.mnemonic, "table": f.table or (f.hist.table or "prices"),
                    "title": f"{f.mnemonic}: {f.description}"[:200], "country": None,
                    "frequency": "D" if f.hist.frequency not in ("W", "M", "Q") else f.hist.frequency,
                    "units": f.unit, "category": f.group, "entity_type": "security", "n_obs": 0, "n_entities": None,
                    "first_date": None, "last_date": None, "pit": True})
    return out


def _panel(expr: str, con: Any, ids: list[str], dates: list[date], universe: str | None) -> tuple[pl.DataFrame, list[str]]:
    from app.bql.engine import panel
    from app.bql.grammar import BqlError

    try:
        rows, warns = panel(expr, con, ids, dates=dates, universe=universe, tmo=600.0)
    except BqlError as e:
        raise SpecError(f"BQL {expr!r}: {e}") from e
    vals = [(r[0], r[1] if isinstance(r[1], date) else date.fromisoformat(str(r[1])[:10]),
             None if r[2] is None else float(r[2])) for r in rows if not isinstance(r[2], str)]
    df = pl.DataFrame(vals, schema={"entity_id": pl.Utf8, "date": pl.Date, "v": pl.Float64}, orient="row")
    return df, warns


def build(spec: dict[str, Any], lake_con: Any, serving_con: Any, check: Callable[[], None] | None = None,
          today: date | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    """(frame, meta) for a normalized DatasetSpec containing `bql` variables."""
    check = check or (lambda: None)
    kind, freq = spec["kind"], spec["frequency"]
    bql_vars = [v for v in spec["variables"] if v["source"] == SOURCE]
    core_vars = [v for v in spec["variables"] if v["source"] != SOURCE]
    if (spec.get("entity_level") or "security") != "security":
        raise SpecError("bql variables need entity_level = security")
    warnings: list[str] = ["bql variables are evaluated point-in-time on the serving snapshot (BQL-lite); exported "
                           "scripts cannot rebuild them"]
    cur = serving_con.cursor()
    try:
        snap = date.fromisoformat(cur.execute("SELECT value FROM _meta WHERE key = 'as_of'").fetchone()[0])
    finally:
        cur.close()
    today = min(today or date.today(), snap)
    core_frame, core_meta = None, None
    if core_vars:
        core_frame, core_meta = _core.build({**spec, "variables": core_vars}, lake_con, check=check, today=today)
        warnings += core_meta.get("warnings", [])
    # ---- grid + entities
    s_end = date.fromisoformat(spec["end"]) if spec.get("end") else today
    s_end = min(s_end, snap)
    if kind == "cross_section":
        as_of = date.fromisoformat(spec["as_of"]) if spec.get("as_of") else s_end
        grid = [min(as_of, snap)]
        s_start = grid[0]
    else:
        s_start = date.fromisoformat(spec["start"]) if spec.get("start") else s_end - timedelta(days=365 * 5)
        grid = _core.make_grid(s_start, s_end, freq)
        if not grid or grid[-1] < s_end - timedelta(days=_core.FREQ_DAYS[freq] * 1.5):
            grid = grid + [s_end]
    if core_frame is not None and "date" in core_frame.columns and core_frame.height:
        grid = sorted(set(core_frame["date"].to_list()))
    ents, members, w = _core.resolve_entities(lake_con, spec.get("entities") or {}, s_start, s_end,
                                              int(spec.get("max_entities") or 3000))
    warnings += [x for x in w if x not in warnings]
    ids = ents["security_id"].to_list()
    gdf = pl.DataFrame({"date": grid}, schema={"date": pl.Date})
    base = ents.select(pl.col("security_id").alias("entity_id"), "issuer_id").join(gdf, how="cross").sort(["entity_id", "date"])
    base = _core._member_mask(base, members if spec.get("pit_universe", True) else None)
    n_rows = base.height
    if n_rows > int(spec.get("max_rows") or 5_000_000):
        raise SpecError(f"dataset would have {n_rows:,} rows (> max_rows): narrow the entities or the window")
    # ---- evaluate each bql variable
    names = []
    for v in bql_vars:
        uni = (v.get("options") or {}).get("universe")
        df, w = _panel(str(v["field"]), serving_con, ids, grid, uni)
        warnings += [f"{v['name']}: {x}" for x in w if f"{v['name']}: {x}" not in warnings]
        if df.is_empty():
            raise SpecError(f"{v['name']}: the BQL expression {v['field']!r} returned no values for these entities / dates")
        f = base.join(df.rename({"v": v["name"]}), on=["entity_id", "date"], how="left")
        f = _core._apply_transforms(f.sort(["entity_id", "date"]), v["name"], v.get("pre_transforms") or [], True, "_member")
        if kind != "timeseries":
            f = _core._apply_transforms(f, v["name"], v.get("transforms") or [], True, "_member")
        base = base.with_columns(f[v["name"]]) if f.height == base.height else base.join(
            f.select("entity_id", "date", v["name"]), on=["entity_id", "date"], how="left")
        names.append(v["name"])
        check()
    # ---- assemble
    if kind == "timeseries":
        aggs = []
        for v in bql_vars:
            nm = v["name"]
            e = pl.col(nm)
            ea = {"mean": e.mean(), "median": e.median(), "sum": e.sum(), "count": e.count().cast(pl.Float64),
                  "min": e.min(), "max": e.max()}[v.get("entity_agg") or "mean"]
            aggs.append(ea.alias(nm))
        out = base.filter(pl.col("_member")).group_by("date").agg(aggs).sort("date")
        out = gdf.join(out, on="date", how="left").sort("date")
        for v in bql_vars:
            out = _core._apply_transforms(out, v["name"], v.get("transforms") or [], False, None)
        if core_frame is not None:
            out = core_frame.join(out, on="date", how="left")
        lead = ["date"]
    else:
        if core_frame is not None:
            out = core_frame.join(base.select(["entity_id", "date"] + names), on=["entity_id", "date"], how="left")
        else:
            lab = ents.select(pl.col("security_id").alias("entity_id"),
                              pl.coalesce(pl.col("ticker"), pl.col("security_id")).alias("entity_label"),
                              "sector_id", "region", "country", pl.col("name").alias("entity_name"))
            out = base.filter(pl.col("_member")).drop("_member").join(lab, on="entity_id", how="left")
        if kind == "cross_section" and out.height:
            last = out["date"].max()
            out = out.filter(pl.col("date") == last)
        lead = ["date", "entity_id", "entity_label"]
    all_vars = [v["name"] for v in spec["variables"]]
    extra = [c for c in out.columns if c not in lead + all_vars]
    attr = [c for c in ("issuer_id", "sector_id", "region", "country", "entity_name") if c in extra] if kind != "timeseries" else []
    if core_meta is not None:
        others = core_meta.get("dummies", []) + core_meta.get("interactions", [])
    else:
        others = []
    out = out.select(lead + attr + [c for c in all_vars if c in out.columns] + [c for c in others if c in out.columns])
    if (spec.get("missing") or {}).get("drop") == "any":
        out = out.drop_nulls(subset=all_vars)
    meta = {
        "core_version": _core.CORE_VERSION, "kind": kind, "frequency": freq,
        "macro_alignment": core_meta["macro_alignment"] if core_meta else "pit",
        "start": out["date"].min().isoformat() if out.height else grid[0].isoformat(),
        "end": out["date"].max().isoformat() if out.height else grid[-1].isoformat(),
        "n_periods": int(out["date"].n_unique()) if out.height else 0,
        "n_entities": int(out["entity_id"].n_unique()) if "entity_id" in out.columns and out.height else (0 if kind != "timeseries" else 1),
        "n_rows": out.height, "entity_level": "security", "variables": all_vars,
        "dummies": core_meta.get("dummies", []) if core_meta else [],
        "interactions": core_meta.get("interactions", []) if core_meta else [],
        "interaction_pairs": core_meta.get("interaction_pairs", {}) if core_meta else {},
        "attributes": attr, "lead_variables": core_meta.get("lead_variables", []) if core_meta else [],
        "warnings": warnings, "sql": (core_meta or {}).get("sql", []),
        "lead_k": {**((core_meta or {}).get("lead_k") or {}), **{v["name"]: 0 for v in bql_vars}},
        "bql": [{"name": v["name"], "expr": v["field"]} for v in bql_vars],
    }
    return out, meta
