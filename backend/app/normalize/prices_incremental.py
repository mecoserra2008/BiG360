"""Incremental prices_finalize (refresh-pipeline fixes 2026-10-26). Owner: ingest-market.

The O2.2a full rebuild (every region from the whole 130 M-row staging history, re-written partition by partition)
took ~2 h 53 min for 98.5 M rows / 24,950 securities on 2026-10-25. A weekly increment only changes the last few
sessions of most securities, so `prices_finalize` now (mode=auto, the default) re-builds per region only what changed
since the region's last finalised **watermark** (max staging `ingested_at` it has seen, data/state/prices_finalize.json):

* dirty securities: staging rows ingested after the watermark (15 min overlap), resolved to securities;
* **full** re-build (whole history, exactly the full-mode code path) for a dirty security when
    - it has no prices_daily rows yet (new listing) or its universe row changed (symbol / window / divisor remap),
    - the new rows reach more than REVISION_DAYS before its last finalised bar (history revision / back-fill),
    - a corporate action of it was ingested after the watermark, or one goes ex on / after its first new bar (the
      backward adjustment factors of its whole history change),
    - the manual identity-break overrides changed for it,
    - and **escalation**: the identity-break or unit-repair guard fires (or an unresolved 10^k jump appears) inside
      its tail window - those guards rewrite history, so the security is rebuilt from its full series;
* otherwise a **tail** re-build: staging rows from TAIL_CONTEXT_DAYS before its first new bar (context for the
  guards: unit flip pairs <= 90 bars, 20-bar volume windows, identity-break gaps, the previous close of dividends)
  are run through the same build_prices(); only bars dated on / after the first new bar replace the stored ones.
  Stitch-guard decisions (official source vs Yahoo history, CN / KR / TW / BR) are computed on the FULL staging
  history of those securities (decision pass, same functions) and passed in, so a tail never decides on a window;
* securities that left the universe are removed from the region.

Only the (region, year) partitions that receive or lose rows are re-written (merge under the table lock). Regions run
in parallel threads (`parallel`, default 2; each region's build is bounded by TAIL_CHUNK / FINALIZE_CHUNK_SECURITIES).
mode=full keeps the O2.2a full rebuild for back-fills (it also sets the watermarks). The first incremental run of a
region without a watermark bootstraps it from prices_daily (max ingested_at - 6 h: every row of a full run carries
its build time).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.schema import conform, get_contract
from app.core.storage import (_parquet_files, _scan_files, _swap_in, _write_partition_files, partition_dir, scan_table,
                              table_dir, table_lock)

log = get_logger("app.normalize.prices_incremental")

STATE_FILE = "prices_finalize.json"
UNIVERSE_FP_FILE = "prices_finalize_universe.parquet"
#: state key: security ids to re-build from their full staging history on the next incremental run (A6)
FORCE_FULL_KEY = "force_full_securities"
#: A6 review: forced full re-builds taken per incremental run (in request order; the rest stay queued). The 68-id A6
#: request was re-included in full by every retry of the 2026-11-07 nightly run, which hit the 10 GB cap twice
#: (a full build of 25 forced ids measured ~0.9 GB on top of the shared staging scan). `rebuild_securities` drains a
#: larger request outside the job, region by region.
FORCE_FULL_MAX_PER_RUN = 15
TAIL_CONTEXT_DAYS = 200
REVISION_DAYS = 45
WM_OVERLAP = timedelta(minutes=15)
BOOTSTRAP_BACK = timedelta(hours=6)
TAIL_CHUNK = 6000
DEFAULT_PARALLEL = 2
#: ram_fix 2026-10-31: securities per stitch-decision scan. Each part holds the FULL staging history (Yahoo + official
#: sources) of its securities; 4,000 per part peaked the job at ~20 GB inside the worker on a 4,600-security tail
#: (CN / EU / US after a normal nightly fetch). Decisions are per security, so the part size never changes them.
STITCH_PART_SECURITIES = 300
_UNI_FP_COLS = ("key", "yf_ticker", "polygon_ticker", "listing_id", "region", "quote_currency", "currency",
                "price_divisor", "calendar", "px_from", "px_to")


# ------------------------------------------------------------------------------------------------ state


def _state_path(s: Settings) -> Path:
    return s.state_dir / STATE_FILE


def load_state(s: Settings) -> dict[str, Any]:
    from app.ops.paths import read_json  # noqa: PLC0415

    st = read_json(_state_path(s), {}) or {}
    st.setdefault("regions", {})
    return st


def save_state(s: Settings, st: dict[str, Any]) -> None:
    from app.ops.paths import write_json_atomic  # noqa: PLC0415

    s.state_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_state_path(s), st)


def overrides_sha(s: Settings) -> str:
    f = s.data_dir / "reference" / "seeds" / "price_identity_breaks.csv"
    return hashlib.sha1(f.read_bytes()).hexdigest()[:16] if f.exists() else "none"


def universe_fp(uni: pl.DataFrame) -> pl.DataFrame:
    """One fingerprint per security of the universe columns that decide how its staging rows resolve."""
    cols = [c for c in _UNI_FP_COLS if c in uni.columns]
    return (uni.select("security_id", pl.concat_str([pl.col(c).cast(pl.Utf8).fill_null("~") for c in cols],
                                                    separator="|").alias("__s"))
            .group_by("security_id").agg(pl.col("__s").sort().str.join(";").hash().alias("fp")))


def _load_uni_fp(s: Settings) -> pl.DataFrame | None:
    f = s.state_dir / UNIVERSE_FP_FILE
    try:
        return pl.read_parquet(f) if f.exists() else None
    except Exception:  # noqa: BLE001 - rebuilt on the next run
        return None


def _save_uni_fp(s: Settings, fp: pl.DataFrame, regions: Sequence[str], region_of: pl.DataFrame) -> None:
    """Replace the fingerprints of the processed regions (others kept)."""
    old = _load_uni_fp(s)
    new = fp.join(region_of, on="security_id", how="left")
    if old is not None and "region" in old.columns:
        new = pl.concat([old.filter(~pl.col("region").is_in(list(regions))), new.filter(pl.col("region").is_in(list(regions)))],
                        how="vertical_relaxed")
    f = s.state_dir / UNIVERSE_FP_FILE
    tmp = f.with_suffix(".tmp")
    new.write_parquet(tmp)
    tmp.replace(f)


# ------------------------------------------------------------------------------------------------ scans


def scan_partitions(table: str, s: Settings, keep: dict[str, Any]) -> pl.LazyFrame:
    """Lazy scan of only the hive partitions whose values pass `keep` {column: predicate(str value) -> bool}."""
    c = get_contract(table)
    root = table_dir(c, s)
    files = []
    for f in _parquet_files(root):
        kv = dict(p.split("=", 1) for p in f.relative_to(root).parts[:-1] if "=" in p)
        if all(fn(kv.get(col, "")) for col, fn in keep.items()):
            files.append(f)
    if not files:
        from app.core.schema import empty_frame  # noqa: PLC0415

        return empty_frame(c).lazy()
    return _scan_files(files, c, root)


def _key_filter(u: pl.DataFrame) -> pl.Expr:
    """Staging rows of the universe rows `u` (same rule as prices._staged_for)."""
    keys_poly = u.get_column("polygon_ticker").drop_nulls().to_list()
    keys_yf = u.get_column("key").drop_nulls().to_list()
    return (((pl.col("source_id") == "polygon") & (pl.col("symbol").is_in(keys_poly)
                                                    | pl.col("symbol").str.replace_all(r"\.", "-").is_in(keys_yf)))
            | ((pl.col("source_id") != "polygon") & pl.col("symbol").is_in(keys_yf)))


def region_watermark(s: Settings, st: dict[str, Any], region: str) -> datetime | None:
    rec = (st.get("regions") or {}).get(region) or {}
    if rec.get("watermark"):
        try:
            return datetime.fromisoformat(rec["watermark"])
        except ValueError:
            pass
    # bootstrap from the last full run: every row it wrote carries its build time
    try:
        mx = scan_partitions("prices_daily", s, {"region": lambda v: v == region}).select(
            pl.col("ingested_at").max()).collect().item()
    except Exception:  # noqa: BLE001
        mx = None
    if mx is None:
        return None
    if mx.tzinfo is None:
        mx = mx.replace(tzinfo=UTC)
    return mx - BOOTSTRAP_BACK


# ------------------------------------------------------------------------------------------------ plan


def plan_region(dirty: pl.DataFrame, last: pl.DataFrame, ca_new: pl.DataFrame, ov_sids: set[str],
                changed_uni: set[str], universe_sids: set[str], revision_days: int = REVISION_DAYS,
                max_delete_share: float = 0.05) -> dict[str, Any]:
    """Pure decision (unit-tested).

    dirty  [security_id, d_new]   first staging date ingested after the watermark
    last   [security_id, last_date] last stored bar
    ca_new [security_id, ex_date] corporate actions ingested after the watermark

    Per security the re-build starts at d = min(d_new, earliest new ex_date) (a security with only a new action:
    min(ex_date, last_date), so the previous close of a future dividend is in the window). Full re-build when the
    security is new, when d lies more than `revision_days` before its last stored bar (history revision / old action
    back-filled), or its overrides / universe row changed. Returns {"tail": {sid: d}, "full": set, "delete": set,
    "why": {reason: n}, "dirty": n}."""
    why: dict[str, int] = {}
    full: set[str] = set()

    def add(sids: set[str], reason: str) -> None:
        new = sids - full
        if new:
            why[reason] = why.get(reason, 0) + len(new)
            full.update(new)

    ca = (ca_new.filter(pl.col("security_id").is_in(list(universe_sids))).group_by("security_id")
          .agg(pl.col("ex_date").min().alias("__ex")) if ca_new.height else
          pl.DataFrame(schema={"security_id": pl.Utf8, "__ex": pl.Date}))
    d = (dirty.join(ca, on="security_id", how="full", coalesce=True)
         .join(last, on="security_id", how="left")
         .filter(pl.col("security_id").is_in(list(universe_sids))))
    d = d.with_columns(pl.min_horizontal(
        pl.col("d_new"),
        pl.when(pl.col("d_new").is_null() & pl.col("last_date").is_not_null())
        .then(pl.min_horizontal("__ex", "last_date")).otherwise(pl.col("__ex"))).alias("d"))
    add(set(d.filter(pl.col("last_date").is_null()).get_column("security_id").to_list()), "new_security")
    add(set(d.filter(pl.col("d") < pl.col("last_date") - pl.duration(days=revision_days))
            .get_column("security_id").to_list()), "history_revision")
    add(ov_sids & universe_sids, "override_changed")
    add(changed_uni & universe_sids, "universe_changed")
    tail = {r[0]: r[1] for r in d.select("security_id", "d").iter_rows() if r[0] not in full and r[1] is not None}
    known = set(last.get_column("security_id").to_list())
    delete = known - universe_sids
    if known and len(delete) > max_delete_share * len(known):  # a broken universe load must never empty a region
        why["delete_suppressed"] = len(delete)
        delete = set()
    return {"tail": tail, "full": full, "delete": delete, "why": why, "dirty": dirty.height}


def escalations(rep: dict[str, Any], d_new: dict[str, date]) -> set[str]:
    """Tail securities whose history-rewriting guards fired in the window -> full re-build."""
    out = set(rep.get("_break_sids") or set()) | set(rep.get("_repair_sids") or set())
    for sid, dt in ((rep.get("unit_repairs") or {}).get("_unresolved") or []):
        dn = d_new.get(sid)
        if dn is not None and dt is not None and dt >= dn:
            out.add(sid)
    return {x for x in out if x in d_new}


def factor_rescale(stored_ref: pl.DataFrame, window: pl.DataFrame, tol: float = 1e-6) -> tuple[pl.DataFrame, set[str]]:
    """Ratios new / stored of adj_factor and split_factor at each tail security's reference bar (its last stored bar
    before the re-build start). Every stored bar before the start is multiplied by them: the actions that changed
    (new, or a pending dividend whose previous close moved) all go ex on / after the start, so the change of the
    backward factor is the same for all earlier bars. stored_ref / window: [security_id, date, adj_factor,
    split_factor]. Returns (rows [security_id, __ra, __rs] where a ratio differs from 1, securities without a usable
    reference bar in the window -> full re-build)."""
    j = stored_ref.join(window.select("security_id", "date", pl.col("adj_factor").alias("__fa"),
                                      pl.col("split_factor").alias("__fs")), on=["security_id", "date"], how="left")
    missing = set(j.filter(pl.col("__fa").is_null() | (pl.col("adj_factor").fill_null(0) == 0)
                           | (pl.col("split_factor").fill_null(0) == 0)).get_column("security_id").to_list())
    r = j.filter(~pl.col("security_id").is_in(list(missing))).with_columns(
        (pl.col("__fa") / pl.col("adj_factor")).alias("__ra"), (pl.col("__fs") / pl.col("split_factor")).alias("__rs"))
    r = r.filter(((pl.col("__ra") - 1).abs() > tol) | ((pl.col("__rs") - 1).abs() > tol))
    return r.select("security_id", "__ra", "__rs"), missing


# ------------------------------------------------------------------------------------------------ build


def _stitch_decisions(s: Settings, u: pl.DataFrame, actions: pl.DataFrame | None, tolerance: float) -> pl.DataFrame | None:
    """Full-history stitch-guard decisions for the securities of `u` (those with an official bulk source in staging):
    same code as the full build (resolve -> units -> unfinished / identity-break guards -> _stitch_guard). One scan
    of the Yahoo + official-source staging partitions with the columns the guards need; chunks in memory."""
    from app.normalize import prices as P  # noqa: PLC0415

    if u.is_empty():
        return None
    srcs = {"yfinance", *P.STITCH_GUARD_SOURCES}
    cols = ["source_id", "symbol", "date", "close", "volume", "currency", "price_basis", "ingested_at"]
    lf = scan_partitions("prices_staging", s, {"source_id": lambda v: v in srcs}).filter(_key_filter(u))
    names = lf.collect_schema().names()
    st_all = lf.select([c for c in cols if c in names]).collect(engine="streaming")
    if st_all.is_empty() or not st_all.filter(pl.col("source_id").is_in(list(P.STITCH_GUARD_SOURCES))).height:
        return None
    st_all = st_all.with_columns([pl.lit(None, pl.Float64).alias(c) for c in ("open", "high", "low", "vwap")])
    m = P.resolve_symbols(st_all.select("source_id", "symbol").unique(), u).select("source_id", "symbol", "security_id")
    out: list[pl.DataFrame] = []
    sids = u.get_column("security_id").unique().sort().to_list()
    for part in P._iter_chunks(sids, P.FINALIZE_CHUNK_SECURITIES * 2):
        uu = u.filter(pl.col("security_id").is_in(list(part)))
        keys = m.filter(pl.col("security_id").is_in(list(part))).select("source_id", "symbol").unique()
        st = st_all.join(keys, on=["source_id", "symbol"], how="semi")
        if st.is_empty():
            continue
        rep: dict[str, Any] = {}
        rc = P._resolve_stage(st, uu, actions, s, rep)
        if rc is None:
            continue
        res = rc[0].with_columns(pl.col("source_id").replace_strict(P.SOURCE_PRIORITY, default=9).alias("__p"))
        _, g = P._stitch_guard(res, tolerance)
        dec = g.get("_decisions")
        if dec is not None and dec.height:
            out.append(dec)
    return pl.concat(out, how="vertical_relaxed") if out else None


def _tail_staged(s: Settings, u: pl.DataFrame, d_new: dict[str, date], context_days: int) -> pl.DataFrame:
    """Staging rows of the tail securities from (re-build start - context) on (year partitions pruned)."""
    if u.is_empty() or not d_new:
        return pl.DataFrame()
    lo_all = min(d_new.values()) - timedelta(days=context_days)
    lf = scan_partitions("prices_staging", s, {"year": lambda v: v.isdigit() and int(v) >= lo_all.year})
    st = lf.filter(_key_filter(u) & (pl.col("date") >= lo_all)).collect(engine="streaming")
    if st.is_empty():
        return st
    # per-security window: map (source_id, symbol) -> earliest window start of the securities it may resolve to
    from app.normalize import prices as P  # noqa: PLC0415

    pairs = st.select("source_id", "symbol").unique()
    m = P.resolve_symbols(pairs, u).select("source_id", "symbol", "security_id")
    lo = pl.DataFrame({"security_id": list(d_new), "__lo": [d - timedelta(days=context_days) for d in d_new.values()]},
                      schema={"security_id": pl.Utf8, "__lo": pl.Date})
    w = m.join(lo, on="security_id", how="inner").group_by("source_id", "symbol").agg(pl.col("__lo").min())
    return st.join(w, on=["source_id", "symbol"], how="inner").filter(pl.col("date") >= pl.col("__lo")).drop("__lo")


def _existing_index(s: Settings, region: str) -> pl.DataFrame:
    """[security_id, last_date, years] of the region's stored bars."""
    lf = scan_partitions("prices_daily", s, {"region": lambda v: v == region})
    # ram_fix 2026-10-31: aggregate inside the streaming engine (it used to materialise every stored bar of the
    # region first: ~1 GB per large region, kept by the allocator for the rest of the job); `years` is used as a set
    return (lf.select("security_id", "date", "year")
            .group_by("security_id").agg(pl.col("date").max().alias("last_date"), pl.col("year").unique().alias("years"))
            .collect(engine="streaming"))


def _dn_frame(tail: dict[str, date]) -> pl.DataFrame:
    return pl.DataFrame({"security_id": list(tail), "__dn": list(tail.values())},
                        schema={"security_id": pl.Utf8, "__dn": pl.Date})


def _merge_write(s: Settings, region: str, new: pl.DataFrame, full: set[str], tail: dict[str, date],
                 delete: set[str], existing: pl.DataFrame, rescale: pl.DataFrame | None = None) -> int:
    """Re-write only the touched (region, year) partitions: stored rows of `full` / `delete` securities and tail rows
    dated on / after their re-build start are replaced by `new`; stored rows before the start of the `rescale`
    securities [security_id, __ra, __rs] get adj_factor / adj_close x __ra and split_factor x __rs."""
    c = get_contract("prices_daily")
    root = table_dir(c, s)
    gone = full | delete
    rs = rescale if rescale is not None and rescale.height else None
    years: set[int] = set()
    if new.height:
        years |= set(new.get_column("year").unique().to_list())
    touch = gone | (set(rs.get_column("security_id").to_list()) if rs is not None else set())
    if touch and existing.height:
        for ys in existing.filter(pl.col("security_id").is_in(list(touch))).get_column("years").to_list():
            years |= set(ys or [])
    tail_df = _dn_frame(tail)
    pcols = list(c.partition_by)
    n = 0
    with table_lock("prices_daily", s):
        for y in sorted(years):
            pdir = partition_dir(root, pcols, [region, y])
            files = [f for f in pdir.glob("*.parquet")] if pdir.exists() else []
            old = _scan_files(files, c, root).collect() if files else None
            add = new.filter(pl.col("year") == y) if new.height else None
            parts = []
            if old is not None and old.height:
                keep = old.filter(~pl.col("security_id").is_in(list(gone)))
                if tail_df.height:
                    keep = keep.join(tail_df, on="security_id", how="left").filter(
                        pl.col("__dn").is_null() | (pl.col("date") < pl.col("__dn"))).drop("__dn")
                if rs is not None:
                    keep = keep.join(rs, on="security_id", how="left").with_columns(
                        (pl.col("adj_factor") * pl.col("__ra").fill_null(1.0)).alias("adj_factor"),
                        (pl.col("adj_close") * pl.col("__ra").fill_null(1.0)).alias("adj_close"),
                        (pl.col("split_factor") * pl.col("__rs").fill_null(1.0)).alias("split_factor")
                    ).drop("__ra", "__rs")
                parts.append(keep)
            if add is not None and add.height:
                parts.append(add)
            parts = [p for p in parts if p.height]
            if not parts:
                if pdir.exists() and old is not None:
                    import shutil  # noqa: PLC0415

                    shutil.rmtree(pdir, ignore_errors=True)
                continue
            df = pl.concat([conform(p.select(c.column_names), c) for p in parts], how="vertical_relaxed")
            df = df.sort("security_id", "date")
            tmp = pdir.parent / f".tmp-{pdir.name}-{uuid.uuid4().hex[:8]}"
            _write_partition_files(tmp, df.drop(pcols), c)
            _swap_in(tmp, pdir)
            n += add.height if add is not None else 0
    return n


@dataclass
class RegionWork:
    """State of one region through the phases of an incremental run."""
    region: str
    uni: pl.DataFrame
    existing: pl.DataFrame
    plan: dict[str, Any]
    tail: dict[str, date] = field(default_factory=dict)
    full: set[str] = field(default_factory=set)
    rep: dict[str, Any] = field(default_factory=dict)
    reps: list[dict[str, Any]] = field(default_factory=list)
    window: pl.DataFrame = field(default_factory=pl.DataFrame)
    rescale: pl.DataFrame | None = None
    t0: float = 0.0


def _plan_phase(s: Settings, region: str, uni_r: pl.DataFrame, dirty_r: pl.DataFrame, ca_new: pl.DataFrame,
                ov_sids: set[str], changed_uni: set[str]) -> RegionWork:
    t0 = time.perf_counter()
    existing = _existing_index(s, region)
    uni_sids = set(uni_r.get_column("security_id").to_list())
    d = dirty_r.group_by("security_id").agg(pl.col("date").min().alias("d_new"))
    plan = plan_region(d, existing.select("security_id", "last_date"), ca_new, ov_sids, changed_uni, uni_sids)
    w = RegionWork(region, uni_r, existing, plan, dict(plan["tail"]), set(plan["full"]), t0=t0)
    w.rep = {"region": region, "mode": "incremental", "dirty_rows": plan["dirty"], "why_full": plan["why"]}
    return w


def _tail_phase(s: Settings, w: RegionWork, st_tail: pl.DataFrame, dec: pl.DataFrame | None,
                actions: pl.DataFrame | None, tolerance: float, context_days: int) -> None:
    """Tail builds of one region (window rows from the shared tail scan), escalations, factor rescale."""
    from app.normalize import prices as P  # noqa: PLC0415

    t1 = time.perf_counter()
    if not w.tail:
        w.rep["seconds_tail"] = 0.0
        return
    ut = w.uni.filter(pl.col("security_id").is_in(list(w.tail)))
    pairs = (P.resolve_symbols(st_tail.select("source_id", "symbol").unique(), ut)
             .select("source_id", "symbol", "security_id")) if st_tail.height else None
    outs: list[pl.DataFrame] = []
    esc: set[str] = set()
    sids = sorted(w.tail)
    for part in P._iter_chunks(sids, TAIL_CHUNK):
        uu = ut.filter(pl.col("security_id").is_in(list(part)))
        if pairs is None:
            break
        keys = pairs.filter(pl.col("security_id").is_in(list(part))).select("source_id", "symbol").unique()
        stp = st_tail.join(keys, on=["source_id", "symbol"], how="semi")
        if stp.is_empty():
            continue
        o, r = P.build_prices(stp, uu, actions, tolerance=tolerance, settings=s, stitch_decisions=dec)
        esc |= escalations(r, w.tail)
        w.reps.append(r)
        if o.height:
            outs.append(o)
    w.window = pl.concat(outs, how="vertical_relaxed") if outs else pl.DataFrame()
    if w.window.height:
        # factor rescale of the stored history (actions that go ex on / after the re-build start)
        lo_year = (min(w.tail.values()) - timedelta(days=context_days)).year
        ref = (scan_partitions("prices_daily", s, {"region": lambda v: v == w.region,
                                                   "year": lambda v: v.isdigit() and int(v) >= lo_year})
               .select("security_id", "date", "adj_factor", "split_factor").collect()
               .join(_dn_frame(w.tail), on="security_id", how="inner").filter(pl.col("date") < pl.col("__dn"))
               .sort("date").group_by("security_id").last().drop("__dn"))
        w.rescale, no_ref = factor_rescale(ref, w.window)
        esc |= no_ref & set(w.tail)       # no usable reference bar in the window: full re-build
    if esc:
        w.full |= esc
        for x in esc:
            w.tail.pop(x, None)
    if w.rescale is not None and w.rescale.height:
        w.rescale = w.rescale.filter(pl.col("security_id").is_in(list(w.tail)))
    w.rep.update({"escalated": len(esc), "escalated_top": sorted(esc)[:20],
                  "rescaled_securities": 0 if w.rescale is None else w.rescale.height,
                  "seconds_tail": round(time.perf_counter() - t1, 1)})


def _finish_phase(s: Settings, w: RegionWork, st_full: pl.DataFrame, actions: pl.DataFrame | None, tolerance: float,
                  write: bool, t_scan: datetime) -> dict[str, Any]:
    """Full-history builds of one region (rows from the shared full scan) + the partition merge."""
    from app.normalize import prices as P  # noqa: PLC0415

    t2 = time.perf_counter()
    uni_sids = set(w.uni.get_column("security_id").to_list())
    full_sids = sorted(w.full & uni_sids)
    full_outs: list[pl.DataFrame] = []
    if full_sids and st_full.height:
        ufull = w.uni.filter(pl.col("security_id").is_in(full_sids))
        pairs = P.resolve_symbols(st_full.select("source_id", "symbol").unique(), ufull).select(
            "source_id", "symbol", "security_id")
        for part in P._iter_chunks(full_sids, P.FINALIZE_CHUNK_SECURITIES):
            uu = ufull.filter(pl.col("security_id").is_in(list(part)))
            keys = pairs.filter(pl.col("security_id").is_in(list(part))).select("source_id", "symbol").unique()
            o, r = P.build_prices(st_full.join(keys, on=["source_id", "symbol"], how="semi"), uu, actions,
                                  tolerance=tolerance, settings=s)
            w.reps.append(r)
            if o.height:
                full_outs.append(o)
    full_out = pl.concat(full_outs, how="vertical_relaxed") if full_outs else pl.DataFrame()
    tail_out = pl.DataFrame()
    if w.window.height and w.tail:
        tail_out = w.window.join(_dn_frame(w.tail), on="security_id", how="inner").filter(
            pl.col("date") >= pl.col("__dn")).drop("__dn")
    rep = w.rep
    rep["seconds_full"] = round(time.perf_counter() - t2, 1)
    frames = [x for x in (tail_out, full_out) if x.height]
    new = pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame()
    rep.update({"tail_securities": len(w.tail), "full_securities": len(full_sids),
                "deleted_securities": len(w.plan["delete"]), "rows_out": new.height, "tail_rows": tail_out.height,
                "full_rows": full_out.height})
    merged = P._merge_reports(w.reps) if w.reps else {}
    for k in ("identity_breaks", "unit_repairs", "stitch_guard", "reconciliation", "dq_flag_counts", "winner_by_source",
              "missing_fx_rows", "unfinished_bars_dropped"):
        if k in merged:
            rep[k] = merged[k]
    rep["securities_out"] = new.get_column("security_id").n_unique() if new.height else 0
    rs = w.rescale
    if write and (new.height or w.plan["delete"] or full_sids or (rs is not None and rs.height)):
        t3 = time.perf_counter()
        rep["rows_written"] = _merge_write(s, w.region, new, set(full_sids), w.tail, set(w.plan["delete"]),
                                           w.existing, rs)
        rep["seconds_write"] = round(time.perf_counter() - t3, 1)
    else:
        rep["rows_written"] = 0
    rep["watermark"] = t_scan.isoformat()
    rep["seconds_total"] = round(time.perf_counter() - w.t0, 1)
    return rep


def staged_rows(s: Settings, u: pl.DataFrame) -> pl.DataFrame:
    """Whole staging history of the universe rows `u`, file by file: a plain symbol filter (imploded Series; every
    staging file is sorted by symbol, so row-group statistics prune it) per parquet file, then the exact
    prices._staged_for rule on the small result. Same rows as prices._staged_for(u, s).

    A6 review round 2: the one-shot streaming scan with a short key list (the 67 forced A6 twins) peaked at ~10 GB
    (10 ids: 16.7 s / 3.5 GB commit; the same predicate with an imploded Series in the streaming engine: 17 GB),
    which is what OOM-killed the 2026-11-07 nightly prices_finalize twice; per file it is 3 s / 1.3 GB for all 67."""
    if u.is_empty():
        return pl.DataFrame()
    c = get_contract("prices_staging")
    root = table_dir(c, s)
    keys_poly = u.get_column("polygon_ticker").drop_nulls().unique().to_list()
    keys_yf = u.get_column("key").drop_nulls().unique().to_list()
    syms = sorted(set(keys_poly) | set(keys_yf) | {k.replace("-", ".") for k in keys_yf})
    if not syms:
        return pl.DataFrame()
    want = pl.Series(values=syms, dtype=pl.Utf8).implode()
    parts = []
    for f in _parquet_files(root):
        x = _scan_files([f], c, root).filter(pl.col("symbol").is_in(want)).collect()
        if x.height:
            parts.append(x)
    if not parts:
        return pl.DataFrame()
    st = pl.concat(parts, how="vertical_relaxed")
    return st.filter(
        ((pl.col("source_id") == "polygon") & (pl.col("symbol").is_in(keys_poly)
                                                | pl.col("symbol").str.replace_all(r"\.", "-").is_in(keys_yf)))
        | ((pl.col("source_id") != "polygon") & pl.col("symbol").is_in(keys_yf)))


def _full_staged(s: Settings, u: pl.DataFrame) -> pl.DataFrame:
    """Whole staging history of the securities of `u` (staged_rows: file by file, bounded memory; chunks of
    FINALIZE_CHUNK_SECURITIES x 4 at most)."""
    from app.normalize import prices as P  # noqa: PLC0415

    if u.is_empty():
        return pl.DataFrame()
    parts = []
    sids = u.get_column("security_id").unique().sort().to_list()
    for part in P._iter_chunks(sids, max(P.FINALIZE_CHUNK_SECURITIES, 4000)):
        parts.append(staged_rows(s, u.filter(pl.col("security_id").is_in(list(part)))))
    parts = [x for x in parts if x.height]
    return pl.concat(parts, how="vertical_relaxed") if parts else pl.DataFrame()


# ------------------------------------------------------------------------------------------------ forced re-builds (A6)


def forced_selection(requested: Sequence[str], uni: pl.DataFrame, cap: int | None = None) -> dict[str, set[str]]:
    """Pure. {region: ids} of the forced full re-builds one run takes: the first `cap` (FORCE_FULL_MAX_PER_RUN when
    None) requested ids, in request order, that are in `uni` [security_id, region]."""
    n = FORCE_FULL_MAX_PER_RUN if cap is None else max(0, int(cap))
    reg = dict(uni.select("security_id", "region").unique(subset=["security_id"], keep="first").iter_rows())
    out: dict[str, set[str]] = {}
    taken = 0
    for x in dict.fromkeys(requested):          # request order, duplicates once
        if taken >= n:
            break
        r = reg.get(x)
        if r is None:
            continue
        out.setdefault(r, set()).add(x)
        taken += 1
    return out


def _drop_forced(st: dict[str, Any], done: set[str]) -> None:
    if not done:
        return
    left = [x for x in (st.get(FORCE_FULL_KEY) or []) if x not in done]
    if left:
        st[FORCE_FULL_KEY] = left
    else:
        st.pop(FORCE_FULL_KEY, None)


def rebuild_securities(settings: Settings | None = None, security_ids: Sequence[str] | None = None,
                       write: bool = True, tolerance: float = 0.01, chunk_securities: int | None = None,
                       clear_request: bool = True) -> dict[str, Any]:
    """Full-history re-build of these securities only, region by region, with the incremental job's own full-build
    path (build_prices on the subset universe) and partition writer (_merge_write: touched (region, year) partitions,
    prices_daily table lock, atomic swap). No watermark moves: the next incremental run still sees every staging row
    ingested since the last one. Default ids: the FORCE_FULL_KEY request. A security whose build yields no rows keeps
    its stored bars (`no_output`). With write and clear_request, every written id leaves the request.
    Used by app.normalize.price_collisions restore-twins (A6: 37 US twins without any bar after the repair)."""
    from app.normalize import prices as P  # noqa: PLC0415

    s = settings or get_settings()
    st = load_state(s)
    ids = list(dict.fromkeys(security_ids if security_ids is not None else (st.get(FORCE_FULL_KEY) or [])))
    rep: dict[str, Any] = {"requested": len(ids), "by_region": {}, "rows_written": 0, "written": [], "no_output": [],
                           "not_in_universe": []}
    if not ids:
        return rep
    uni = P.load_universe(s, include_delisted=True)
    u = uni.filter(pl.col("security_id").is_in(ids))
    rep["not_in_universe"] = sorted(set(ids) - set(u.get_column("security_id").to_list()))
    if u.is_empty():
        return rep
    staged = _full_staged(s, u)
    actions = scan_table("corporate_actions_staging", s).collect() if P._table_has_data(
        "corporate_actions_staging", s) else None
    n_chunk = int(chunk_securities or P.FINALIZE_CHUNK_SECURITIES)
    for region in sorted(r for r in u.get_column("region").unique().to_list() if r):
        t0 = time.perf_counter()
        ur = u.filter(pl.col("region") == region)
        sids = sorted(set(ur.get_column("security_id").to_list()))
        outs: list[pl.DataFrame] = []
        if staged.height:
            pairs = P.resolve_symbols(staged.select("source_id", "symbol").unique(), ur).select(
                "source_id", "symbol", "security_id")
            for part in P._iter_chunks(sids, n_chunk):
                keys = pairs.filter(pl.col("security_id").is_in(list(part))).select("source_id", "symbol").unique()
                stp = staged.join(keys, on=["source_id", "symbol"], how="semi")
                if stp.is_empty():
                    continue
                o, _ = P.build_prices(stp, ur.filter(pl.col("security_id").is_in(list(part))), actions,
                                      tolerance=tolerance, settings=s)
                if o.height:
                    outs.append(o)
        new = pl.concat(outs, how="vertical_relaxed") if outs else pl.DataFrame()
        got = set(new.get_column("security_id").unique().to_list()) if new.height else set()
        rr: dict[str, Any] = {"securities": len(sids), "securities_out": len(got), "rows_out": new.height,
                              "no_output": sorted(set(sids) - got)}
        if write and got:
            existing = _existing_index(s, region)
            rr["rows_written"] = _merge_write(s, region, new.filter(pl.col("security_id").is_in(sorted(got))), got, {},
                                              set(), existing)
            rep["rows_written"] += rr["rows_written"]
            rep["written"] += sorted(got)
            if clear_request:
                st = load_state(s)
                _drop_forced(st, got)
                save_state(s, st)
        rr["seconds"] = round(time.perf_counter() - t0, 1)
        rep["by_region"][region] = rr
        rep["no_output"] += rr["no_output"]
    return rep


# ------------------------------------------------------------------------------------------------ driver


def finalize_incremental(settings: Settings | None = None, regions: Sequence[str] | None = None,
                         tolerance: float = 0.01, write: bool = True, chunk_securities: int | None = None,
                         parallel: int | None = None, context_days: int = TAIL_CONTEXT_DAYS,
                         force_full_max: int | None = None) -> dict[str, Any]:
    """mode=auto: incremental per region (full rebuild for regions without a watermark). Returns the report.
    force_full_max: forced full re-builds taken from the state request this run (default FORCE_FULL_MAX_PER_RUN).

    Phases (staging scans are shared by all regions: one scan costs ~1 min on the 130 M-row staging):
      1 plan per region (stored index, dirty securities, corporate actions, overrides, universe changes)
      2 one year-pruned tail scan for every tail security, one stitch-decision scan (official-source securities)
      3 tail builds per region (parallel threads) -> escalations + factor rescale
      4 one full-history scan for every full / escalated security, full builds + partition merge per region"""
    from app.normalize import prices as P  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    if not P._table_has_data("prices_staging", s):
        return {"status": "skipped", "reason": "prices_staging empty"}
    t_scan = datetime.now(UTC)
    uni = P.load_universe(s, include_delisted=True)
    todo = list(regions) if regions else sorted(r for r in uni.get_column("region").unique().to_list() if r)
    st = load_state(s)
    wms = {r: region_watermark(s, st, r) for r in todo}
    agg: dict[str, Any] = {"mode": "incremental", "by_region": {}, "rows_written": 0, "rows_out": 0,
                           "securities_out": 0, "identity_series_split": 0, "unit_securities_repaired": 0,
                           "reconciliation": {"overlap_rows": 0, "flagged_rows": 0}, "phases": {}}
    # regions without any watermark: full rebuild (first run on an empty prices_daily)
    for r in [r for r in todo if wms[r] is None]:
        rep = P._finalize_region(s, uni.filter(pl.col("region") == r), tolerance, write, time.perf_counter(),
                                 chunk_securities)
        rep["mode"] = "full"
        _record(agg, r, rep)
        if write:
            st["regions"][r] = {"watermark": t_scan.isoformat(), "mode": "full", "at": datetime.now(UTC).isoformat()}
            save_state(s, st)
    inc = [r for r in todo if wms[r] is not None]
    errors: list[str] = []
    if inc:
        ph = agg["phases"]
        tp = time.perf_counter()
        wm_min = min(wms[r] for r in inc) - WM_OVERLAP  # type: ignore[type-var]
        lf = scan_table("prices_staging", s).filter(pl.col("ingested_at") > wm_min)
        dirty = lf.select("source_id", "symbol", "date", "ingested_at").collect(engine="streaming")
        uni_inc = uni.filter(pl.col("region").is_in(inc))
        dres = P.resolve_symbols(dirty, uni_inc).select("security_id", "region", "date", "ingested_at") \
            if dirty.height else pl.DataFrame(schema={"security_id": pl.Utf8, "region": pl.Utf8, "date": pl.Date,
                                                      "ingested_at": pl.Datetime("us", "UTC")})
        actions = scan_table("corporate_actions_staging", s).collect() if P._table_has_data(
            "corporate_actions_staging", s) else None
        if actions is not None and actions.height:
            ca_res = P.resolve_symbols(actions.select("source_id", "symbol", "ex_date", "ingested_at"), uni_inc).select(
                "security_id", "region", "ex_date", "ingested_at")
        else:
            ca_res = pl.DataFrame(schema={"security_id": pl.Utf8, "region": pl.Utf8, "ex_date": pl.Date,
                                          "ingested_at": pl.Datetime("us", "UTC")})
        sha = overrides_sha(s)
        ov_sids: set[str] = set()
        if st.get("overrides_sha") and st.get("overrides_sha") != sha:
            from app.normalize import price_integrity as PI  # noqa: PLC0415

            ov = PI.load_break_overrides(s)
            keys = set(ov.get_column("key").to_list()) if ov.height else set()
            ov_sids = keys & set(uni_inc.get_column("security_id").to_list())
            ov_sids |= set(uni_inc.filter(pl.col("key").is_in(list(keys)) | pl.col("polygon_ticker").is_in(list(keys)))
                           .get_column("security_id").to_list())
        fp = universe_fp(uni_inc)
        old_fp = _load_uni_fp(s)
        changed_uni: set[str] = set()
        if old_fp is not None:
            j = fp.join(old_fp.select("security_id", pl.col("fp").alias("__o")), on="security_id", how="inner")
            changed_uni = set(j.filter(pl.col("fp") != pl.col("__o")).get_column("security_id").to_list())
        # explicit full re-builds requested through the state file (A6 ticker-collision repair:
        # app.normalize.price_collisions.request_full_rebuild): at most `force_full_max` per run, oldest request first;
        # each region's share is cleared as soon as that region is written (below), the rest stay queued
        forced_by_region = forced_selection(st.get(FORCE_FULL_KEY) or [], uni_inc, force_full_max)
        forced = {x for v in forced_by_region.values() for x in v}
        changed_uni |= forced
        agg["forced_full"] = len(forced)
        agg["forced_full_queued"] = len(st.get(FORCE_FULL_KEY) or [])
        agg["dirty_staging_rows"] = dirty.height
        works: dict[str, RegionWork] = {}
        for r in inc:
            w = wms[r] - WM_OVERLAP  # type: ignore[operator]
            ur = uni_inc.filter(pl.col("region") == r)
            dr = dres.filter((pl.col("region") == r) & (pl.col("ingested_at") > w))
            ca_new = ca_res.filter((pl.col("region") == r) & (pl.col("ingested_at") > w)).select("security_id", "ex_date")
            works[r] = _plan_phase(s, r, ur, dr, ca_new, ov_sids, changed_uni)
        ph["plan_s"] = round(time.perf_counter() - tp, 1)
        # ---- shared tail scan + stitch decisions
        tp = time.perf_counter()
        tail_all = {k: v for wk in works.values() for k, v in wk.tail.items()}
        u_tail = uni_inc.filter(pl.col("security_id").is_in(list(tail_all)))
        st_tail = _tail_staged(s, u_tail, tail_all, context_days) if tail_all else pl.DataFrame()
        ph["tail_scan_s"] = round(time.perf_counter() - tp, 1)
        ph["tail_staged_rows"] = st_tail.height
        tp = time.perf_counter()
        dec = None
        stitch_part = max(50, min(STITCH_PART_SECURITIES, int(chunk_securities or STITCH_PART_SECURITIES)))
        if st_tail.height:
            offm = st_tail.filter(pl.col("source_id").is_in(list(P.STITCH_GUARD_SOURCES))).select(
                "source_id", "symbol").unique()
            if offm.height:
                off_sids = sorted(set(P.resolve_symbols(offm, u_tail).get_column("security_id").to_list()))
                decs = [d for d in (_stitch_decisions(s, u_tail.filter(pl.col("security_id").is_in(list(part))),
                                                      actions, tolerance)
                                    for part in P._iter_chunks(off_sids, stitch_part)) if d is not None]
                dec = pl.concat(decs, how="vertical_relaxed") if decs else None
        ph["stitch_decisions_s"] = round(time.perf_counter() - tp, 1)
        ph["stitch_decision_securities"] = 0 if dec is None else dec.height
        # ---- tail builds per region
        tp = time.perf_counter()
        n_par = max(1, int(parallel or DEFAULT_PARALLEL))
        sizes = {r: len(wk.tail) for r, wk in works.items()}
        order = sorted(works, key=lambda r: -sizes[r])

        def tail_one(r: str) -> str:
            try:
                _tail_phase(s, works[r], st_tail, dec, actions, tolerance, context_days)
            except Exception as e:  # noqa: BLE001 - one region must not lose the others' work
                log.exception("prices_finalize incremental %s (tail) failed", r)
                works[r].rep["error"] = f"{type(e).__name__}: {e}"[:500]
            return r

        with ThreadPoolExecutor(max_workers=n_par, thread_name_prefix="finalize") as ex:
            list(ex.map(tail_one, order))
        del st_tail
        ph["tail_build_s"] = round(time.perf_counter() - tp, 1)
        # ---- shared full-history scan, full builds + merge per region
        tp = time.perf_counter()
        full_all = sorted({x for wk in works.values() if "error" not in wk.rep for x in wk.full})
        st_full = _full_staged(s, uni_inc.filter(pl.col("security_id").is_in(full_all))) if full_all else pl.DataFrame()
        ph["full_scan_s"] = round(time.perf_counter() - tp, 1)
        ph["full_securities"] = len(full_all)
        tp = time.perf_counter()

        def finish_one(r: str) -> tuple[str, dict[str, Any]]:
            wk = works[r]
            if "error" in wk.rep:
                return r, wk.rep
            try:
                rep = _finish_phase(s, wk, st_full, actions, tolerance, write, t_scan)
            except Exception as e:  # noqa: BLE001
                log.exception("prices_finalize incremental %s failed", r)
                rep = {**wk.rep, "error": f"{type(e).__name__}: {e}"[:500]}
            log.info("prices_finalize %s: %s", r, {k: v for k, v in rep.items() if k in (
                "mode", "dirty_rows", "tail_securities", "full_securities", "escalated", "rescaled_securities",
                "rows_written", "seconds_total", "why_full", "error")})
            return r, rep

        with ThreadPoolExecutor(max_workers=n_par, thread_name_prefix="finalize") as ex:
            for r, rep in ex.map(finish_one, sorted(works, key=lambda r: -len(works[r].full))):
                _record(agg, r, rep)
                if write and "error" not in rep:
                    st = load_state(s)
                    st["regions"][r] = {"watermark": rep["watermark"], "mode": "incremental",
                                        "at": datetime.now(UTC).isoformat(),
                                        **{k: rep.get(k) for k in ("tail_securities", "full_securities", "rows_written",
                                                                   "seconds_total")}}
                    # A6 review: this region's forced re-builds are written: drop them now, not only after an
                    # error-free run (a failing region elsewhere used to re-queue the whole request every retry)
                    _drop_forced(st, forced_by_region.get(r, set()))
                    save_state(s, st)
        ph["finish_s"] = round(time.perf_counter() - tp, 1)
        errors = [r for r, v in agg["by_region"].items() if v.get("error")]
        if write and not errors:
            st = load_state(s)
            st["overrides_sha"] = sha
            # requests for ids no longer in the universe at all can never be served: dropped (a request added
            # while this run was going stays)
            _drop_forced(st, set(st.get(FORCE_FULL_KEY) or []) - set(uni.get_column("security_id").to_list()))
            save_state(s, st)
            _save_uni_fp(s, fp, inc, uni_inc.select("security_id", "region").unique("security_id"))
    agg["errors"] = errors
    agg["seconds_total"] = round(time.perf_counter() - t0, 2)
    P._write_report("prices_finalize_all", agg, s)
    return agg


def _record(agg: dict[str, Any], r: str, rep: dict[str, Any]) -> None:
    agg["by_region"][r] = {k: rep.get(k) for k in ("mode", "dirty_rows", "tail_securities", "full_securities",
                                                   "escalated", "rescaled_securities", "seconds_stitch_decisions",
                                                   "why_full", "rows_out", "rows_written", "securities_out",
                                                   "seconds_tail", "seconds_full", "seconds_write", "seconds_total",
                                                   "error") if k in rep}
    agg["rows_written"] += int(rep.get("rows_written") or 0)
    agg["rows_out"] += int(rep.get("rows_out") or 0)
    agg["securities_out"] += int(rep.get("securities_out") or 0)
    agg["identity_series_split"] += int((rep.get("identity_breaks") or {}).get("series_split") or 0)
    agg["unit_securities_repaired"] += int((rep.get("unit_repairs") or {}).get("securities_repaired") or 0)
    for k in ("overlap_rows", "flagged_rows"):
        agg["reconciliation"][k] += int((rep.get("reconciliation") or {}).get(k) or 0)


def mark_full_watermarks(s: Settings, regions: Sequence[str], t_scan: datetime) -> None:
    """After a mode=full run: every rebuilt region is finalised up to the moment its staging scan started."""
    st = load_state(s)
    for r in regions:
        st["regions"][r] = {"watermark": t_scan.isoformat(), "mode": "full", "at": datetime.now(UTC).isoformat()}
    st["overrides_sha"] = overrides_sha(s)
    save_state(s, st)
