"""Serving snapshot builder (job `build_serving_db`, owner: api). ARCHITECTURE §4 / §12.

Builds an immutable DuckDB file `data/db/serving_<run_id>.duckdb` from the Parquet lake and publishes it by rewriting
`data/db/CURRENT` (atomic replace). The API (app.api.deps) only ever opens the CURRENT file read-only.

Design
* Every Parquet contract table is exposed through a *typed* view `lake.<table>` that has exactly the contract columns
  (missing columns -> typed NULL, missing table -> typed empty view). The builder therefore never breaks on a fresh or
  partially populated lake: absent upstream tables just yield empty serving tables with the right schema, and
  `_tables` records which inputs were empty so the API can answer `data_status: pending`.
* Serving tables are materialized (no Parquet scans at request time) and sorted by their lookup key so DuckDB
  zone maps prune. The central denormalized table is `sec` (one row per security: identity, quote stats, valuation,
  factor/composite scores for all profiles, latest metrics pivoted, moat/ML/insider/sentiment/zone summaries).
* Build: in-memory DuckDB with lake views -> `ATTACH serving_<run>.duckdb.tmp AS snap` -> CREATE TABLE snap.* ->
  DETACH (checkpointed) -> rename to `.duckdb` -> verify (open read-only, check `_meta`) -> flip CURRENT ->
  cleanup (keep the newest KEEP_SNAPSHOTS files plus the live one; files still open by a reader are skipped and
  retried on the next build — Windows-safe).
* Concurrent lake writers (atomic partition swaps) can make a scan fail mid-build: each table is retried after
  re-globbing the lake views.
* Memory (refresh-pipeline fixes 2026-10-26): the build has its own DuckDB budget (serving_build_connection():
  BIG360_SERVING_DUCKDB_MEMORY / _THREADS / BIG360_SERVING_TEMP_DIR, preserve_insertion_order=false), runs alone in
  the exclusive heavy slot (app.ops.heavy_slot), and every section reads bounded inputs (latest rows / history
  windows, sliced prices and story aggregates). Each section and every table >= 5 s is logged with its duration and
  the process memory, so the job log is the build profile.

Run:  python -m app.cli run-job build_serving_db      (or)      python -m app.api.serving [--no-publish]
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import yaml

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.schema import TableContract, load_contracts
from app.core.storage import _parquet_files, publish_serving_db, table_dir

log = get_logger("app.api.serving")

SNAPSHOT_SCHEMA_VERSION = 3
KEEP_SNAPSHOTS = 2  # O1.7: keep the last 2 (CURRENT is never deleted)
PEER_LAW_SESSIONS = 127  # conformal P3: member laws kept for the marginal pair-band calibration (~126 pairs)
FACTORS = ("quality", "profitability", "growth", "value", "balance_sheet", "momentum", "insider", "sentiment", "macro",
           "moat")
HORIZONS = ("1M", "3M", "6M")
REGION_BENCHMARK = {"US": "IDX:^GSPC", "CA": "IDX:^GSPC", "EU": "IDX:^STOXX50E", "UK": "IDX:^FTSE", "JP": "IDX:^N225",
                    "KR": "IDX:^KS11", "HK": "IDX:^HSI", "CN": "IDX:000300.SS", "LATAM": "IDX:^BVSP",
                    "APAC": "IDX:^GSPC", "OTHER": "IDX:^GSPC"}
MODEL_PREFERENCE = ("ensemble", "xgboost_rank", "ft_transformer", "cs_attention")
EXCHANGE_NAMES = {
    "XNYS": "NYSE", "XNAS": "Nasdaq", "ARCX": "NYSE Arca", "XASE": "NYSE American", "BATS": "Cboe BZX",
    "XTSE": "Toronto Stock Exchange", "XTSX": "TSX Venture", "XLON": "London Stock Exchange", "XETR": "Xetra",
    "XFRA": "Frankfurt Stock Exchange", "XPAR": "Euronext Paris", "XAMS": "Euronext Amsterdam",
    "XBRU": "Euronext Brussels", "XLIS": "Euronext Lisbon", "XDUB": "Euronext Dublin", "XOSL": "Oslo Bors",
    "XMIL": "Borsa Italiana", "MTAA": "Borsa Italiana", "XMAD": "BME Madrid", "XSWX": "SIX Swiss Exchange",
    "XVTX": "SIX Swiss Exchange", "XSTO": "Nasdaq Stockholm", "XCSE": "Nasdaq Copenhagen", "XHEL": "Nasdaq Helsinki",
    "XWBO": "Wiener Borse", "XWAR": "Warsaw Stock Exchange", "XATH": "Athens Stock Exchange",
    "XTKS": "Tokyo Stock Exchange", "XKRX": "Korea Exchange", "XKOS": "KOSDAQ", "XHKG": "Hong Kong Exchanges",
    "XSHG": "Shanghai Stock Exchange", "XSHE": "Shenzhen Stock Exchange", "BVMF": "B3", "XMEX": "Bolsa Mexicana",
    "XSGO": "Santiago Stock Exchange", "XLIM": "Bolsa de Valores de Lima", "XBOG": "Bolsa de Valores de Colombia",
    "XBUE": "Bolsa y Mercados Argentinos", "XASX": "ASX", "XNSE": "NSE India", "XBOM": "BSE India",
    "XTAI": "Taiwan Stock Exchange", "XSES": "Singapore Exchange", "XJSE": "Johannesburg Stock Exchange",
    "XIST": "Borsa Istanbul", "XTAE": "Tel Aviv Stock Exchange", "XSAU": "Saudi Exchange",
}


# ------------------------------------------------------------------------------------------------ registries


@lru_cache(maxsize=4)
def _yaml(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def contract_yaml(name: str, settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return _yaml(str(s.contracts_dir / name))


def metric_registry(settings: Settings | None = None) -> dict[str, list[str]]:
    """{'generic': [...], 'accounting': [...], 'kpi': [...]} metric ids from contracts/sector_kpis.yaml."""
    kp = contract_yaml("sector_kpis.yaml", settings)
    generic = [m["id"] for m in kp.get("generic_metrics", [])]
    acc = [m["id"] for m in kp.get("accounting_scores", [])]
    kpis: list[str] = []
    for ks in kp.get("kpi_sets", []):
        for k in ks.get("kpis", []) or []:
            if k["id"] not in kpis and k["id"] not in generic:
                kpis.append(k["id"])
    return {"generic": generic, "accounting": acc, "kpi": kpis}


def profiles(settings: Settings | None = None) -> dict[str, dict[str, float]]:
    """Composite profiles from scoring.yaml (ml_blend resolved to its base profile weights)."""
    sc = contract_yaml("scoring.yaml", settings)
    out: dict[str, dict[str, float]] = {}
    raw = sc["composite"]["profiles"]
    for pid, w in raw.items():
        if "_base_profile" in w:
            out[pid] = {k: float(v) for k, v in raw[w["_base_profile"]].items() if not k.startswith("_")}
        else:
            out[pid] = {k: float(v) for k, v in w.items() if not k.startswith("_")}
    return out


def subsector_kpi_set(settings: Settings | None = None) -> dict[str, str]:
    kp = contract_yaml("sector_kpis.yaml", settings)
    return {sub: ks["id"] for ks in kp["kpi_sets"] for sub in ks["subsectors"]}


# ------------------------------------------------------------------------------------------------ helpers


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _private_mb() -> int | None:
    """Private (committed) memory of this process in MB, for the per-table build profile (None if unavailable)."""
    try:
        from app.ops.winsys import process_memory_detail

        d = process_memory_detail(os.getpid())
        return int(d["private_mb"]) if d else None
    except Exception:  # pragma: no cover - diagnostics only
        return None


def _latest_sql(cols: str, src: str, cond: str, key: str, dcol: str = "as_of_date") -> str:
    """Rows of the latest `dcol` per `key` (refresh-pipeline fixes 2026-10-26): a hash aggregate + semi join instead
    of `QUALIFY dcol = max(dcol) OVER (PARTITION BY key)`, which partitioned and sorted every row of the source (tech
    levels, level stats, setup / factor tables: millions of rows; 30-70 s each in the 2026-10-26 profile)."""
    return (f"SELECT {cols} FROM (SELECT * FROM {src} WHERE {cond}) t "
            f"SEMI JOIN (SELECT {key}, max({dcol}) AS {dcol} FROM {src} WHERE {cond} GROUP BY {key}) m "
            f"USING ({key}, {dcol})")


# F39: CJK / kana / hangul / full-width characters (accented Latin names such as "Nestlé" are NOT matched)
_CJK_RE = r"[\x{3000}-\x{9FFF}\x{AC00}-\x{D7AF}\x{FF00}-\x{FFEF}]"
_FULLWIDTH = "".join(chr(c) for c in range(0xFF01, 0xFF5F)) + "　"
_HALFWIDTH = "".join(chr(c) for c in range(0x21, 0x7F)) + " "


def _display_name_sql(name: str, aliases: str) -> str:
    """F39: primary name in Latin script - a full-width Latin name is folded to ASCII ("Ｓｏｌｖｖｙ" -> "Solvvy"); a
    CJK / kana / hangul name is replaced by the first Latin-script alias (EDINET / DART / yfinance English names);
    otherwise the stored name. The local-script name stays available as name_local."""
    folded = f"translate({name}, {_lit(_FULLWIDTH)}, {_lit(_HALFWIDTH)})"
    alias = (f"list_filter(coalesce({aliases}, []), a -> length(a) >= 3 AND NOT regexp_matches(a, {_lit(_CJK_RE)}) "
             f"AND regexp_matches(a, '[A-Za-z]'))[1]")
    return (f"CASE WHEN {name} IS NOT NULL AND regexp_matches({name}, {_lit(_CJK_RE)}) THEN coalesce("
            f"CASE WHEN NOT regexp_matches({folded}, {_lit(_CJK_RE)}) THEN {folded} END, {alias}, {name}) "
            f"ELSE {name} END")


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


@lru_cache(maxsize=1)
def _peer_region_map() -> dict[str, str]:
    try:
        sp = yaml.safe_load((get_settings().contracts_dir / "scoring.yaml").read_text(encoding="utf-8"))
        return dict(sp["peer_groups"]["peer_region_map"])
    except Exception:  # noqa: BLE001
        return {}


def _peer_region_case(col: str) -> str:
    """scoring.yaml peer_region_map as SQL (scores rank within sector x peer region)."""
    m = _peer_region_map()
    if not m:
        return f"coalesce({col}, 'GLOBAL')"
    return "CASE " + " ".join(f"WHEN {col} = {_lit(k)} THEN {_lit(v)}" for k, v in m.items()) + " ELSE 'GLOBAL' END"


def composite_sql(weights: dict[str, float], prefix: str = "", min_frac: float = 0.5) -> str:
    """SQL expression: weighted mean of available factor scores (weights renormalized over non-null factors).

    Null when the weight share of non-null factors is below `min_frac` (scoring.yaml requires >= 5 of 10 factors for
    the default profiles; for arbitrary custom weights the equivalent is half of the requested weight)."""
    w = {f: float(x) for f, x in weights.items() if f in FACTORS and x and float(x) > 0}
    if not w:
        return "CAST(NULL AS DOUBLE)"
    tot = sum(w.values())
    num = " + ".join(f"COALESCE({prefix}f_{f}, 0) * {x / tot!r}" for f, x in w.items())
    den = " + ".join(f"(CASE WHEN {prefix}f_{f} IS NOT NULL THEN {x / tot!r} ELSE 0 END)" for f, x in w.items())
    return f"(CASE WHEN ({den}) >= {min_frac!r} THEN ({num}) / NULLIF({den}, 0) END)"


@dataclass
class BuildReport:
    run_id: str
    path: Path | None
    as_of: date
    published: bool
    seconds: float
    tables: dict[str, int] = field(default_factory=dict)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    sections: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------ lake views


def _typed_select(c: TableContract, have: dict[str, str] | None, raw: str) -> str:
    """Typed SELECT of every contract column (missing -> NULL, mismatched -> TRY_CAST); narrowed float32 columns are
    widened + rounded and derived columns computed (app.core.storage.logical_select_sql)."""
    from app.core.storage import logical_select_sql

    return logical_select_sql(c, have, raw)


def register_lake(con: duckdb.DuckDBPyConnection, settings: Settings | None = None, empty: bool = False,
                  only: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Create `lake.<table>` typed views for all Parquet contracts. Returns {table: {rows, status, error?}}."""
    s = settings or get_settings()
    con.execute("CREATE SCHEMA IF NOT EXISTS lake")
    con.execute("CREATE SCHEMA IF NOT EXISTS rawlake")
    out: dict[str, dict[str, Any]] = {}
    for name, c in load_contracts().items():
        if not c.is_parquet or (only is not None and name not in only):
            continue
        st: dict[str, Any] = {"rows": 0, "status": "empty"}
        have: dict[str, str] | None = None
        root = table_dir(c, s)
        if not empty and _parquet_files(root):
            raw = f"rawlake.{_q(name)}"
            glob = root.as_posix().replace("'", "''") + ("/**/*.parquet" if c.partition_by else "/*.parquet")
            opts = ["union_by_name = true"]
            if c.partition_by:
                types = ", ".join(f"'{p}': {c.column(p).duckdb}" for p in c.partition_by)
                opts += ["hive_partitioning = true", f"hive_types = {{{types}}}"]
            for attempt in range(3):
                try:
                    con.execute(f"CREATE OR REPLACE VIEW {raw} AS SELECT * FROM read_parquet('{glob}', {', '.join(opts)})")
                    have = {r[0]: r[1] for r in con.execute(f"DESCRIBE {raw}").fetchall()}
                    st["rows"] = int(con.execute(f"SELECT count(*) FROM {raw}").fetchone()[0])
                    st["status"] = "ok" if st["rows"] else "empty"
                    break
                except duckdb.Error as e:  # concurrent partition swap or unreadable file
                    have = None
                    st = {"rows": 0, "status": "error", "error": str(e)[:300]}
                    time.sleep(0.5 * (attempt + 1))
        con.execute(f"CREATE OR REPLACE VIEW lake.{_q(name)} AS {_typed_select(c, have, f'rawlake.{_q(name)}')}")
        out[name] = st
    return out


#: lake tables added by the full-universe extension (2026-10-30)
EXT_LAKE_TABLES = ("factor_scores_ext", "composite_scores_ext", "peer_scores_ext", "ml_scope_status")


def ensure_lake_views(con: duckdb.DuckDBPyConnection, names: Iterable[str], settings: Settings | None = None) -> None:
    """A long-lived process (the worker) caches the contract set it started with (schema.load_contracts is
    lru_cached), so tables whose contracts were added later have no `lake.<t>` view there. Register them from the
    contract file directly (data when present, else a typed empty view) so the builder never fails on them."""
    have = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE schema_name = 'lake'").fetchall()}
    missing = [n for n in names if n not in have]
    if not missing:
        return
    from app.core.schema import parse_contract  # noqa: PLC0415

    s = settings or get_settings()
    for n in missing:
        f = s.schemas_dir / f"{n}.yaml"
        if not f.exists():
            continue
        c = parse_contract(yaml.safe_load(f.read_text(encoding="utf-8")), s.project_root)
        root = table_dir(c, s)
        have_cols: dict[str, str] | None = None
        raw = f"rawlake.{_q(n)}"
        if _parquet_files(root):
            glob = root.as_posix().replace("'", "''") + ("/**/*.parquet" if c.partition_by else "/*.parquet")
            opts = ["union_by_name = true"]
            if c.partition_by:
                types = ", ".join(f"'{q}': {c.column(q).duckdb}" for q in c.partition_by)
                opts += ["hive_partitioning = true", f"hive_types = {{{types}}}"]
            try:
                con.execute(f"CREATE OR REPLACE VIEW {raw} AS SELECT * FROM read_parquet('{glob}', {', '.join(opts)})")
                have_cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE {raw}").fetchall()}
            except duckdb.Error as e:
                log.warning("serving: %s unreadable (%s); empty view", n, e)
                have_cols = None
        con.execute(f"CREATE OR REPLACE VIEW lake.{_q(n)} AS {_typed_select(c, have_cols, raw)}")
        log.info("serving: registered lake.%s from its contract file (contract added after process start)", n)


# ------------------------------------------------------------------------------------------------ table builders


class _Builder:
    def __init__(self, con: duckdb.DuckDBPyConnection, target: str, as_of: date, settings: Settings,
                 inputs: dict[str, dict[str, Any]], empty: bool):
        self.con, self.t, self.as_of, self.s, self.inputs, self.empty = con, target, as_of, settings, inputs, empty
        self.timings: dict[str, float] = {}
        self.sections: dict[str, float] = {}
        self.A = _lit(as_of)

    def create(self, name: str, select_sql: str, order_by: str | None = None) -> None:
        sql = f"CREATE OR REPLACE TABLE {self.t}{_q(name)} AS {select_sql}" + (f" ORDER BY {order_by}" if order_by else "")
        t0 = time.perf_counter()
        self._exec_retry(sql, name)
        self._done(name, t0)

    def insert(self, name: str, select_sql: str, order_by: str | None = None) -> None:
        """Append a further slice to a table created by create() (partitioned builds: bounded memory per slice)."""
        sql = f"INSERT INTO {self.t}{_q(name)} {select_sql}" + (f" ORDER BY {order_by}" if order_by else "")
        self._exec_retry(sql, name)

    def _exec_retry(self, sql: str, name: str) -> None:
        for attempt in range(3):
            try:
                self.con.execute(sql)
                return
            except duckdb.IOException:
                if attempt == 2:
                    raise
                log.warning("serving: retrying %s after IO error (concurrent lake write?)", name)
                time.sleep(1.0 + attempt)
                register_lake(self.con, self.s, self.empty)

    def _done(self, name: str, t0: float) -> None:
        secs = round(time.perf_counter() - t0, 3)
        self.timings[name] = secs
        if not self.empty and secs >= 5.0:  # per-table profile in the job log (section timing + process memory)
            log.info("serving: %s built in %.1fs (process %s MB)", name, secs, _private_mb())

    def section(self, name: str, fn: Any) -> None:
        t0 = time.perf_counter()
        fn()
        secs = round(time.perf_counter() - t0, 2)
        self.sections[name] = secs
        if not self.empty:
            log.info("serving: section %s done in %.1fs (process %s MB)", name, secs, _private_mb())

    # ---------------------------------------------------------------- reference
    def reference(self) -> None:
        A = self.A
        self.create("issuers", "SELECT * FROM lake.issuers QUALIFY row_number() OVER (PARTITION BY issuer_id "
                               "ORDER BY updated_at DESC) = 1", "issuer_id")
        # isin / cusip fall back to identifier_map (identity_resolve adds FTD CUSIPs and CUSIP-derived US/CA ISINs
        # there; securities_master is only rebuilt by ref_universe)
        self.create("securities", f"""
            SELECT s.* REPLACE (coalesce(s.isin, d.isin) AS isin, coalesce(s.cusip, d.cusip) AS cusip)
            FROM lake.securities_master s
            LEFT JOIN (SELECT security_id,
                              arg_max(id_value, confidence) FILTER (WHERE id_type = 'isin') AS isin,
                              arg_max(id_value, confidence) FILTER (WHERE id_type = 'cusip') AS cusip
                       FROM lake.identifier_map
                       WHERE security_id IS NOT NULL AND id_type IN ('isin', 'cusip')
                         AND (valid_to IS NULL OR valid_to >= {A})
                       GROUP BY 1) d USING (security_id)
            QUALIFY row_number() OVER (PARTITION BY s.security_id ORDER BY s.updated_at DESC) = 1""", "security_id")
        self.create("listings", f"SELECT * FROM lake.listings WHERE valid_from <= {A} OR valid_from IS NULL "
                                "QUALIFY row_number() OVER (PARTITION BY listing_id ORDER BY valid_from DESC) = 1",
                    "security_id")
        self.create("identifiers", f"""
            SELECT issuer_id, security_id, listing_id, entity_level, id_type, id_value, confidence, source_id
            FROM lake.identifier_map WHERE valid_to IS NULL OR valid_to >= {A}
            QUALIFY row_number() OVER (PARTITION BY issuer_id, coalesce(security_id, ''), id_type
                                       ORDER BY confidence DESC, valid_from DESC) = 1""", "issuer_id")
        self.create("universe", f"""
            SELECT DISTINCT universe_id, security_id FROM lake.universe_membership
            -- the snapshot serves the *current* membership: valid_from is the (later) creation date of the
            -- membership row, so filtering on it would hide members whose data date precedes it
            WHERE valid_to IS NULL OR valid_to > {A}""", "security_id")
        self.create("fx_latest", f"""
            SELECT currency, arg_max(usd_per_unit, date) AS usd_per_unit, max(date) AS date FROM lake.fx_rates
            WHERE date <= {A} AND currency <> 'USD' GROUP BY currency
            UNION ALL SELECT 'USD', 1.0, {A}""", "currency")

    # ---------------------------------------------------------------- prices
    def _key_ranges(self, n: int, col: str = "security_id", src: str = "lake.securities_master") -> list[str]:
        """SQL conditions splitting `col` into `n` contiguous, ordered ranges (quantiles of the keys in `src`)."""
        try:
            qs = self.con.execute(
                f"SELECT quantile_disc({col}, {[round(i / n, 4) for i in range(1, n)]}) "
                f"FROM (SELECT DISTINCT {col} FROM {src})").fetchone()[0] or []
        except duckdb.Error:
            qs = []
        qs = sorted({q for q in qs if q})
        if not qs:
            return ["TRUE"]
        conds = [f"{col} < {_lit(qs[0])}"]
        conds += [f"{col} >= {_lit(a)} AND {col} < {_lit(b)}" for a, b in zip(qs, qs[1:], strict=False)]
        conds.append(f"({col} >= {_lit(qs[-1])} OR {col} IS NULL)")   # the slices cover every row
        return conds

    def _dedupe_prices(self) -> None:
        """A (security_id, date) can only repeat when a security has bars in two region partitions (region change
        between finalize runs). Only those few securities are re-read with the old ingested_at dedupe."""
        A = self.A
        try:
            multi = [r[0] for r in self.con.execute(
                f"SELECT security_id FROM lake.prices_daily WHERE date <= {A} AND date >= {A} - INTERVAL 15 YEAR "
                "GROUP BY 1 HAVING count(DISTINCT region) > 1").fetchall()]
        except duckdb.Error as e:  # pragma: no cover - advisory
            log.info("serving: region-overlap check skipped (%s)", e)
            return
        if not multi:
            return
        log.warning("serving: %d securities have bars in two prices_daily regions; de-duplicating them", len(multi))
        ids = ", ".join(_lit(x) for x in multi)
        self.con.execute(f"DELETE FROM {self.t}prices WHERE security_id IN ({ids})")
        self.con.execute(f"""INSERT INTO {self.t}prices
            SELECT security_id, date, open, high, low, close, adj_close, adj_factor, split_factor, volume, currency,
                   close_usd FROM lake.prices_daily
            WHERE security_id IN ({ids}) AND date <= {A} AND date >= {A} - INTERVAL 15 YEAR
            QUALIFY row_number() OVER (PARTITION BY security_id, date ORDER BY ingested_at DESC) = 1
            ORDER BY security_id, date""")

    def prices(self) -> None:
        A = self.A
        y15 = f"year({A}) - 15"
        # refresh-pipeline fixes 2026-10-26: prices_daily grew to ~100 M rows (long Yahoo history) and the old
        # per-(security, date) QUALIFY row_number() dedupe window (+ ingested_at) made this the first memory hog.
        # prices_daily's primary key (security_id, date) is unique by construction (prices_finalize writes one row per
        # key; DqCheck PkUnique guards it), so the dedupe is gone; `year` (hive partition) prunes the files; slices
        # of security_id keep each sort small.
        sel = (f"SELECT security_id, date, open, high, low, close, adj_close, adj_factor, split_factor, volume, currency, "
               f"close_usd FROM lake.prices_daily WHERE year >= {y15} AND year <= year({A}) AND date <= {A} "
               f"AND date >= {A} - INTERVAL 15 YEAR AND ({{cond}})")
        conds = self._key_ranges(8)
        t0 = time.perf_counter()
        self.create("prices", sel.format(cond=conds[0]), "security_id, date")
        for cond in conds[1:]:
            self.insert("prices", sel.format(cond=cond), "security_id, date")
        self._dedupe_prices()
        self._done("prices", t0)
        # history before the 15-year daily window, compacted to weekly bars (first open / max high / min low /
        # last close, sum volume; adj_* = per-day adjusted before aggregating) -> /prices serves the full range
        wsel = (f"""SELECT security_id, max(date) AS date, arg_min(open, date) AS open, max(high) AS high, min(low) AS low,
                       arg_max(close, date) AS close, arg_max(adj_close, date) AS adj_close,
                       arg_min(open * f, date) AS adj_open, max(high * f) AS adj_high, min(low * f) AS adj_low,
                       sum(volume) AS volume, arg_max(currency, date) AS currency, count(*)::SMALLINT AS n_days
                FROM (SELECT security_id, date, open, high, low, close, adj_close, volume, currency,
                             adj_close / NULLIF(close, 0) AS f
                      FROM lake.prices_daily WHERE year <= {y15} AND date < {A} - INTERVAL 15 YEAR AND ({{cond}}))
                GROUP BY security_id, date_trunc('week', date)""")
        t0 = time.perf_counter()
        wconds = self._key_ranges(4)
        self.create("prices_w", wsel.format(cond=wconds[0]), "security_id, date")  # ~6.7 M rows
        for cond in wconds[1:]:
            self.insert("prices_w", wsel.format(cond=cond), "security_id, date")
        self._done("prices_w", t0)
        self.create("corp_actions", f"""
            SELECT security_id, ex_date, action_type, ratio, amount, currency FROM lake.corporate_actions
            WHERE ex_date <= {A} AND ex_date >= {A} - INTERVAL 15 YEAR""", "security_id, ex_date")
        T = self.t
        self.create("px_stats", f"""
            WITH p0 AS (
                SELECT security_id, date, close, adj_close, close_usd, volume, currency,
                       coalesce(close_usd * volume, close * volume) AS dv,
                       adj_close / NULLIF(close, 0) AS f,
                       coalesce(high, close) * adj_close / NULLIF(close, 0) AS ah,
                       coalesce(low, close) * adj_close / NULLIF(close, 0) AS al
                FROM {T}prices WHERE date >= {A} - INTERVAL 800 DAY
            ), p1 AS (
                -- rn = position from the latest bar (1 = latest); from the same ascending window as lag() so one
                -- sort serves all three (was a second, descending window: refresh-pipeline fixes 2026-10-26)
                SELECT *, ln(adj_close / NULLIF(lag(adj_close) OVER w, 0)) AS lr,
                       greatest(ah - al, abs(ah - lag(adj_close) OVER w), abs(al - lag(adj_close) OVER w)) AS tr,
                       count(*) OVER (PARTITION BY security_id) - row_number() OVER w + 1 AS rn
                FROM p0 WINDOW w AS (PARTITION BY security_id ORDER BY date)
            ), last AS (SELECT security_id, max(date) AS last_date FROM p1 GROUP BY 1),
            agg AS (
                SELECT p1.security_id,
                    max(date) FILTER (rn = 1) AS px_date,
                    max(close) FILTER (rn = 1) AS close,
                    max(close_usd) FILTER (rn = 1) AS close_usd,
                    max(currency) FILTER (rn = 1) AS px_currency,
                    max(volume) FILTER (rn = 1) AS volume,
                    max(adj_close) FILTER (rn = 1) AS a1, max(adj_close) FILTER (rn = 2) AS a2,
                    max(adj_close) FILTER (rn = 6) AS a6, max(adj_close) FILTER (rn = 22) AS a22,
                    max(adj_close) FILTER (rn = 64) AS a64, max(adj_close) FILTER (rn = 127) AS a127,
                    max(adj_close) FILTER (rn = 253) AS a253,
                    arg_max(adj_close, date) FILTER (year(date) < year(l.last_date)) AS a_ye,
                    max(ah) FILTER (rn <= 252) AS hi52, min(al) FILTER (rn <= 252) AS lo52,
                    max(ah) FILTER (rn = 1) AS ah1, min(al) FILTER (rn = 1) AS al1,
                    avg(adj_close) FILTER (rn <= 50) AS sma50, avg(adj_close) FILTER (rn <= 200) AS sma200,
                    count(*) FILTER (rn <= 200) AS n200,
                    stddev_samp(lr) FILTER (rn <= 252) * sqrt(252) AS vol_1y,
                    avg(dv) FILTER (rn <= 20) AS adv_usd_20d,
                    avg(volume) FILTER (rn BETWEEN 2 AND 21) AS vavg, stddev_samp(volume) FILTER (rn BETWEEN 2 AND 21) AS vsd,
                    avg(tr) FILTER (rn <= 14) / NULLIF(max(f) FILTER (rn = 1), 0) AS atr14,
                    count(*) AS n_bars
                FROM p1 JOIN last l USING (security_id) GROUP BY 1
            )
            SELECT security_id, px_date, close, close_usd, px_currency, volume,
                a1 / NULLIF(a2, 0) - 1 AS chg_1d, a1 / NULLIF(a6, 0) - 1 AS chg_1w, a1 / NULLIF(a22, 0) - 1 AS chg_1m,
                a1 / NULLIF(a64, 0) - 1 AS chg_3m, a1 / NULLIF(a127, 0) - 1 AS chg_6m, a1 / NULLIF(a253, 0) - 1 AS ret_12m,
                a1 / NULLIF(a_ye, 0) - 1 AS chg_ytd,
                CASE WHEN n_bars >= 126 THEN a22 / NULLIF(a253, 0) - 1 END AS mom_12_1,
                CASE WHEN n_bars >= 126 THEN a22 / NULLIF(a127, 0) - 1 END AS mom_6_1,
                hi52 * close / NULLIF(a1, 0) AS high_52w, lo52 * close / NULLIF(a1, 0) AS low_52w,
                a1 / NULLIF(hi52, 0) AS high_52w_proximity,
                CASE WHEN n200 >= 200 THEN a1 / NULLIF(sma200, 0) - 1 END AS trend_200d,
                a1 > sma50 AS above_50d, CASE WHEN n200 >= 200 THEN a1 > sma200 END AS above_200d,
                ah1 >= hi52 AND n_bars >= 200 AS new_high_52w, al1 <= lo52 AND n_bars >= 200 AS new_low_52w,
                sma50 * close / NULLIF(a1, 0) AS sma50, sma200 * close / NULLIF(a1, 0) AS sma200,
                vol_1y, adv_usd_20d, (volume - vavg) / NULLIF(vsd, 0) AS volume_z, atr14, n_bars
            FROM agg""", "security_id")
        # 1y beta vs the region benchmark (daily log returns, >= 120 overlapping days)
        bench = " ".join(f"WHEN {_lit(r)} THEN {_lit(b)}" for r, b in REGION_BENCHMARK.items())
        self.create("px_beta", f"""
            WITH r AS (
                SELECT security_id, date, ln(adj_close / NULLIF(lag(adj_close) OVER
                       (PARTITION BY security_id ORDER BY date), 0)) AS r
                FROM {T}prices WHERE date >= {A} - INTERVAL 400 DAY
            ), m AS (
                SELECT s.security_id, CASE i.region {bench} ELSE 'IDX:^GSPC' END AS bench_id
                FROM {T}securities s LEFT JOIN {T}issuers i USING (issuer_id)
            )
            SELECT r.security_id, regr_slope(r.r, b.r) AS beta_1y, count(*) AS n
            FROM r JOIN m USING (security_id) JOIN r b ON b.security_id = m.bench_id AND b.date = r.date
            WHERE r.date > {A} - INTERVAL 365 DAY AND r.r IS NOT NULL AND b.r IS NOT NULL
            GROUP BY 1 HAVING count(*) >= 120""", "security_id")
        self.create("valuation_latest", f"""
            SELECT * EXCLUDE (year) FROM lake.valuation_daily WHERE date <= {A}
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY date DESC) = 1""", "security_id")
        # short_pct_float: FINRA publishes no float, so the raw column is always null; use the smart-money float proxy
        # (short interest / shares outstanding of the same settlement, si_signals.si_pct_out) - completeness audit
        # 2026-10-27 (sec.short_pct_float / BQL si.pct_float were empty for every security)
        self.create("short_interest_latest", f"""
            WITH r AS (
                SELECT security_id, settlement_date, short_interest_shares, days_to_cover, short_pct_float
                FROM lake.short_interest WHERE knowledge_date <= {A} AND security_id NOT LIKE 'finra:%'
                QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY settlement_date DESC) = 1),
            p AS (SELECT security_id, settlement_date, si_pct_out FROM lake.si_signals WHERE knowledge_date <= {A})
            SELECT r.security_id, r.settlement_date, r.short_interest_shares, r.days_to_cover,
                   coalesce(r.short_pct_float, p.si_pct_out) AS short_pct_float
            FROM r LEFT JOIN p USING (security_id, settlement_date)""", "security_id")

    # ---------------------------------------------------------------- fundamentals / metrics
    def fundamentals(self) -> None:
        A, T = self.A, self.t
        # ~15 M rows sorted on 5 keys: built in 4 ordered issuer_id slices (same table; the single sort was the
        # build's memory peak, ~6 GB; refresh-pipeline fixes 2026-10-26)
        fsel = f"""
            SELECT issuer_id, item_id, fiscal_year, fiscal_period, period_kind, period_start, period_end, value, currency,
                   value_usd, gaap, derivation, knowledge_date, knowledge_date_estimated, is_restatement, dq_score
            FROM lake.fundamentals_canonical WHERE period_end >= {A} - INTERVAL 16 YEAR AND ({{cond}})"""
        forder = "issuer_id, period_kind, period_end, item_id, knowledge_date"
        t0 = time.perf_counter()
        fconds = self._key_ranges(4, "issuer_id", "lake.issuers")
        self.create("fund", fsel.format(cond=fconds[0]), forder)
        for cond in fconds[1:]:
            self.insert("fund", fsel.format(cond=cond), forder)
        self._done("fund", t0)
        self.create("metrics", f"""
            SELECT * FROM (
                SELECT issuer_id, metric_id, 'ratios' AS src, CAST(NULL AS VARCHAR) AS kpi_set_id, period_kind,
                       fiscal_year, fiscal_period, period_end, value, unit, knowledge_date FROM lake.ratios
                UNION ALL
                SELECT issuer_id, kpi_id, 'sector_kpis', kpi_set_id, period_kind, fiscal_year, fiscal_period,
                       period_end, value, unit, knowledge_date FROM lake.sector_kpis)
            WHERE knowledge_date <= {A}
            QUALIFY row_number() OVER (PARTITION BY issuer_id, metric_id, period_kind, period_end
                                       ORDER BY knowledge_date DESC) = 1""",
                    "issuer_id, period_kind, period_end, metric_id")
        # a bounded metric (roic / croic, app.analytics.ratios.SANITY_BOUNDS) withheld on a newer period must not fall
        # back to an older period's value (EXPE showed a 2025 ROIC while the latest TTM is flagged; completeness C)
        self.create("metric_latest", f"""
            WITH fl AS (SELECT issuer_id, replace(metric_id, '_flag', '') AS mid, period_kind, max(period_end) AS fpe
                        FROM {T}metrics WHERE metric_id IN ('roic_flag', 'croic_flag') AND value >= 1 GROUP BY 1, 2, 3)
            SELECT m.issuer_id, m.metric_id, m.src, m.kpi_set_id, m.period_kind, m.period_end, m.fiscal_year, m.value, m.unit
            FROM {T}metrics m LEFT JOIN fl ON fl.issuer_id = m.issuer_id AND fl.mid = m.metric_id
                 AND fl.period_kind = m.period_kind
            WHERE m.period_kind IN ('TTM', 'FY') AND m.value IS NOT NULL AND isfinite(m.value)
              AND (fl.fpe IS NULL OR m.period_end > fl.fpe)
            QUALIFY row_number() OVER (PARTITION BY m.issuer_id, m.metric_id ORDER BY
                (m.period_kind = 'TTM' AND m.period_end >= {A} - INTERVAL 400 DAY) DESC, m.period_end DESC,
                (m.period_kind = 'TTM') DESC) = 1""", "issuer_id, metric_id")
        self.create("rev_share", f"""
            WITH r AS (SELECT issuer_id, fiscal_year, arg_max(value_usd, knowledge_date) AS revenue_usd FROM {T}fund
                       WHERE item_id = 'revenue' AND period_kind = 'FY' AND knowledge_date <= {A} GROUP BY 1, 2)
            SELECT r.*, i.sector_id, i.subsector_id,
                   r.revenue_usd / NULLIF(sum(r.revenue_usd) OVER (PARTITION BY i.subsector_id, r.fiscal_year), 0) AS share
            FROM r JOIN {T}issuers i USING (issuer_id) WHERE r.revenue_usd > 0""", "issuer_id, fiscal_year")
        self.create("fund_meta", f"""
            WITH f AS (
                SELECT issuer_id, max(period_end) AS fund_period_end, max(knowledge_date) AS fund_knowledge_date,
                       arg_max(value, period_end) FILTER (item_id = 'shares_outstanding') AS shares_outstanding,
                       max(period_end) FILTER (item_id = 'shares_outstanding') AS shares_period_end,
                       arg_max(knowledge_date, period_end) FILTER (item_id = 'shares_outstanding') AS shares_knowledge_date,
                       arg_max(gaap, period_end) AS gaap, arg_max(currency, period_end) AS fund_currency
                FROM {T}fund WHERE knowledge_date <= {A} GROUP BY 1),
            fl AS (
                SELECT issuer_id, max(knowledge_date) AS last_filing_date FROM lake.filings_index
                WHERE issuer_id IS NOT NULL AND knowledge_date <= {A}
                  AND form_class IN ('annual', 'semiannual', 'quarterly', 'current_report') GROUP BY 1)
            SELECT * FROM f FULL OUTER JOIN fl USING (issuer_id)""", "issuer_id")

    # ---------------------------------------------------------------- scores
    def scores(self) -> None:
        A, T = self.A, self.t
        ensure_lake_views(self.con, EXT_LAKE_TABLES, self.s)
        # full-universe extension (2026-10-30, app.analytics.scoring_full): the *_ext tables hold every security outside
        # the MVP universe, anchored to the MVP reference (scope extended) or copies of an MVP issuer's rows (scope mvp);
        # a security is in exactly one of the two sources. MVP rows: scope 'mvp' (model universe).
        peer_cols = ("as_of_date, security_id, issuer_id, metric_id, factor_id, raw_value, peer_group_key, peer_group_level, "
                     "peer_count, peer_percentile, peer_median, self_z, self_hist_n, self_score, mcap_adj_score, "
                     "metric_score, imputed, computed_at, run_id")
        self.create("peer_latest", f"""
            {_latest_sql(peer_cols, "lake.peer_scores", f"as_of_date <= {A}", "security_id")}
            UNION ALL
            {_latest_sql(peer_cols, "lake.peer_scores_ext", f"as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 60 DAY",
                         "security_id")}""", "security_id, metric_id")
        self.create("factor_hist", f"""
            SELECT as_of_date, security_id, issuer_id, factor_id, score, score_raw, coverage, rank_in_sector,
                   sector_count, delta_1m, 'mvp' AS scope FROM lake.factor_scores
            WHERE as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 1830 DAY
            UNION ALL
            SELECT as_of_date, security_id, issuer_id, factor_id, score, score_raw, coverage, rank_in_sector,
                   sector_count, delta_1m, scope FROM lake.factor_scores_ext
            WHERE as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 1830 DAY""", "security_id, as_of_date, factor_id")
        self.create("composite_hist", f"""
            SELECT as_of_date, security_id, issuer_id, profile_id, score, grade, rank_global, rank_sector, coverage,
                   dq_score, 'mvp' AS scope, CAST(NULL AS VARCHAR) AS reason, CAST(NULL AS VARCHAR[]) AS missing_inputs
            FROM lake.composite_scores
            WHERE as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 1830 DAY
            UNION ALL
            SELECT as_of_date, security_id, issuer_id, profile_id, score, grade, rank_global, rank_sector, coverage,
                   dq_score, scope, reason, missing_inputs FROM lake.composite_scores_ext
            WHERE as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 1830 DAY""", "security_id, as_of_date, profile_id")
        # latest row per security; full-universe ranks (region x sector, all scored securities MVP + extended, one row
        # per issuer) for every scored security: rank_*_full / n_*_full
        prm = _peer_region_case("i.region")
        self.create("factor_latest", f"""
            WITH l AS (SELECT * FROM {T}factor_hist
                       QUALIFY row_number() OVER (PARTITION BY security_id, factor_id ORDER BY as_of_date DESC) = 1),
            k AS (SELECT l.issuer_id, l.factor_id, any_value(l.score) AS score, any_value(i.sector_id) AS sector_id,
                         any_value({prm}) AS peer_region
                  FROM l LEFT JOIN {T}issuers i USING (issuer_id) WHERE l.score IS NOT NULL GROUP BY 1, 2),
            r AS (SELECT issuer_id, factor_id,
                         row_number() OVER (PARTITION BY factor_id, sector_id, peer_region ORDER BY score DESC, issuer_id)
                             ::INTEGER AS rank_in_sector_full,
                         count(*) OVER (PARTITION BY factor_id, sector_id, peer_region)::INTEGER AS sector_count_full
                  FROM k)
            SELECT l.*, r.rank_in_sector_full, r.sector_count_full FROM l
            LEFT JOIN r ON r.issuer_id = l.issuer_id AND r.factor_id = l.factor_id AND l.score IS NOT NULL""",
                    "security_id, factor_id")
        self.create("composite_latest", f"""
            WITH l AS (SELECT * FROM {T}composite_hist
                       QUALIFY row_number() OVER (PARTITION BY security_id, profile_id ORDER BY as_of_date DESC) = 1),
            k AS (SELECT l.issuer_id, l.profile_id, any_value(l.score) AS score, any_value(i.sector_id) AS sector_id,
                         any_value({prm}) AS peer_region
                  FROM l LEFT JOIN {T}issuers i USING (issuer_id) WHERE l.score IS NOT NULL GROUP BY 1, 2),
            r AS (SELECT issuer_id, profile_id,
                         row_number() OVER (PARTITION BY profile_id ORDER BY score DESC, issuer_id)::INTEGER AS rank_global_full,
                         count(*) OVER (PARTITION BY profile_id)::INTEGER AS n_global_full,
                         row_number() OVER (PARTITION BY profile_id, sector_id, peer_region ORDER BY score DESC, issuer_id)
                             ::INTEGER AS rank_sector_full,
                         count(*) OVER (PARTITION BY profile_id, sector_id, peer_region)::INTEGER AS n_sector_full
                  FROM k)
            SELECT l.*, r.rank_global_full, r.n_global_full, r.rank_sector_full, r.n_sector_full FROM l
            LEFT JOIN r ON r.issuer_id = l.issuer_id AND r.profile_id = l.profile_id AND l.score IS NOT NULL""",
                    "security_id, profile_id")
        self.create("moat", "SELECT * FROM lake.moat", "issuer_id, as_of_date")
        self.create("fv_latest", _latest_sql("*", "lake.forecast_valuation", f"as_of_date <= {A}", "issuer_id"),
                    "issuer_id, scenario")
        self.create("coc_latest", f"""SELECT * FROM lake.cost_of_capital WHERE as_of_date <= {A}
            QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY as_of_date DESC) = 1""", "issuer_id")
        self.create("forecasts_latest", _latest_sql("*", "lake.forecasts", f"as_of_date <= {A}", "issuer_id"),
                    "issuer_id, scenario, item_id, fiscal_year")

    # ---------------------------------------------------------------- alt data
    def alt(self) -> None:
        A, T = self.A, self.t
        self.create("insider_txn", f"""
            WITH t AS (
                SELECT x.*, coalesce(x.security_id, s.security_id) AS sec_id
                FROM lake.insider_transactions x
                LEFT JOIN (SELECT issuer_id, arg_max(security_id, is_primary_security::INT) AS security_id
                           FROM {T}securities GROUP BY 1) s ON s.issuer_id = x.issuer_id
                WHERE x.transaction_date >= {A} - INTERVAL 5 YEAR AND x.knowledge_date <= {A}
                  AND x.superseded_by_filing_id IS NULL
            ), b AS (
                SELECT txn_id, sec_id, insider_id, transaction_date AS d,
                       CASE WHEN date_diff('day', lag(transaction_date) OVER (PARTITION BY sec_id ORDER BY transaction_date),
                                           transaction_date) > 30 THEN 1 ELSE 0 END AS brk
                FROM t WHERE txn_type = 'buy' AND is_open_market AND sec_id IS NOT NULL
            ), g AS (
                SELECT *, sum(brk) OVER (PARTITION BY sec_id ORDER BY d, txn_id ROWS UNBOUNDED PRECEDING) AS grp FROM b
            ), isl AS (
                SELECT sec_id, grp, min(d) AS first_d, count(DISTINCT insider_id) AS nb FROM g GROUP BY 1, 2
            )
            SELECT t.*, coalesce(sc.cluster_id,
                       CASE WHEN isl.nb >= 2 THEN t.sec_id || ':' || strftime(isl.first_d, '%Y%m%d') END) AS cluster_id,
                   sc.anomaly_score AS txn_anomaly_score, sc.cluster_n_insiders, sc.role_weight, sc.n_insiders_same_dir_30d,
                   sc.first_buy_2y, sc.routine, sc.size_vs_hist, sc.pre_drawdown_252d, sc.pre_ret_126d
            FROM t LEFT JOIN g ON g.txn_id = t.txn_id LEFT JOIN isl ON isl.sec_id = g.sec_id AND isl.grp = g.grp
            LEFT JOIN lake.insider_txn_scores sc ON sc.txn_id = t.txn_id""",
                    "sec_id, transaction_date DESC")
        # feed view: recent filings sorted by knowledge_date (zone maps prune the Insider Radar date filter)
        self.create("insider_feed", f"""SELECT * FROM {T}insider_txn WHERE knowledge_date >= {A} - INTERVAL 400 DAY""",
                    "knowledge_date DESC, value_usd DESC NULLS LAST")
        self.create("insider_signals", f"""SELECT * EXCLUDE (year) FROM lake.insider_signals
            WHERE as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 400 DAY""", "security_id, as_of_date")
        self.create("insider_latest", f"""SELECT * FROM {T}insider_signals
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY as_of_date DESC) = 1""", "security_id")
        self.create("news", f"""
            WITH n AS (
                SELECT *, unnest(range(1, len(issuer_ids) + 1)) AS i FROM lake.news_items
                WHERE published_at >= {A} - INTERVAL 180 DAY AND knowledge_date <= {A} AND len(issuer_ids) > 0)
            SELECT issuer_ids[i] AS issuer_id, item_id, published_at, title, url, publisher, source_id, language,
                   sentiment_label, sentiment_score, gdelt_tone,
                   coalesce(issuer_relevance[i], link_confidence[i], relevance) AS relevance
            FROM n""", "issuer_id, published_at DESC")
        self.create("sentiment_daily", f"""SELECT * EXCLUDE (year) FROM lake.sentiment_daily
            WHERE date <= {A} AND date >= {A} - INTERVAL 730 DAY""", "security_id, date")
        self.create("sentiment_latest", f"""SELECT * FROM {T}sentiment_daily
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY date DESC) = 1""", "security_id")
        self.create("text_changes", f"""
            SELECT t.*, f.form_type FROM lake.filing_text_changes t LEFT JOIN lake.filings_index f USING (filing_id)
            WHERE t.knowledge_date <= {A}""", "issuer_id, knowledge_date DESC")
        self.create("gdelt_events", f"""SELECT * EXCLUDE (year_month) FROM lake.gdelt_events
            WHERE event_date >= {A} - INTERVAL 120 DAY AND knowledge_date <= {A}""", "event_date, global_event_id")
        self.create("gdelt_issuer", f"""SELECT unnest(issuer_ids) AS issuer_id, global_event_id, event_date
            FROM {T}gdelt_events WHERE len(issuer_ids) > 0""", "issuer_id, event_date")
        self.create("gdelt_company_daily", f"""SELECT * EXCLUDE (year) FROM lake.gdelt_company_daily
            WHERE date <= {A} AND date >= {A} - INTERVAL 400 DAY""", "issuer_id, date")
        self.create("filings", f"""SELECT * EXCLUDE (year) FROM lake.filings_index
            WHERE issuer_id IS NOT NULL AND knowledge_date <= {A} AND knowledge_date >= {A} - INTERVAL 5 YEAR""",
                    "issuer_id, knowledge_date DESC")

    # ---------------------------------------------------------------- technicals
    def technicals(self) -> None:
        A = self.A
        for name, src in (("tech_levels", "tech_levels"), ("zones", "confluence_zones"),
                          ("level_stats", "level_type_stats"), ("setups", "tech_setups")):
            self.create(name, _latest_sql("*", f"lake.{src}", f"as_of_date <= {A}", "security_id"), "security_id")

    # ---------------------------------------------------------------- ML / macro
    def ml_macro(self) -> None:
        A, T = self.A, self.t
        self.create("model_runs", "SELECT * FROM lake.model_runs", "created_at DESC")
        self.create("model_eval", "SELECT * FROM lake.model_eval_series", "model_run_id, metric, date, bucket")
        self.create("ml_pred", f"""SELECT * FROM lake.ml_predictions
            WHERE (is_live OR is_oos) AND date <= {A} AND date >= {A} - INTERVAL 730 DAY""",
                    "security_id, horizon, date")
        pref = " ".join(f"WHEN {_lit(m)} THEN {i}" for i, m in enumerate(MODEL_PREFERENCE))
        promos: list[dict[str, Any]] = []
        if not self.empty:
            try:
                from app.api.app_state import model_promotions

                promos = model_promotions()
            except Exception as e:  # app_state locked/missing: promotions are advisory
                log.info("model promotions unavailable: %s", e)
        self.con.execute("CREATE OR REPLACE TEMP TABLE _promos (model_run_id VARCHAR)")
        if promos:
            self.con.executemany("INSERT INTO _promos VALUES (?)", [[p["model_run_id"]] for p in promos])
        # production model per horizon: API promotion > is_production > preference order > latest live predictions
        self.create("ml_prod", f"""
            WITH cand AS (
                SELECT r.horizon, r.model_run_id, r.model_type, r.is_production, r.created_at,
                       (SELECT max(date) FROM {T}ml_pred p WHERE p.model_run_id = r.model_run_id) AS last_date
                FROM {T}model_runs r
                WHERE r.horizon IS NOT NULL AND r.status IN ('completed', 'archived')
                  AND r.model_type IN ({', '.join(_lit(m) for m in MODEL_PREFERENCE)}))
            SELECT horizon, model_run_id, model_type, is_production, last_date FROM cand WHERE last_date IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY horizon ORDER BY
                model_run_id IN (SELECT model_run_id FROM _promos) DESC, is_production DESC,
                CASE model_type {pref} ELSE 9 END, last_date DESC, created_at DESC) = 1""", "horizon")
        # OOS decile return distribution per model (expected excess return + interval for a prediction's decile)
        self.create("ml_decile_ret", f"""
            SELECT model_run_id, TRY_CAST(bucket AS INTEGER) AS decile, avg(value) AS mean,
                   quantile_cont(value, 0.1) AS p10, quantile_cont(value, 0.9) AS p90, count(*) AS n
            FROM {T}model_eval WHERE metric = 'decile_return' AND TRY_CAST(bucket AS INTEGER) BETWEEN 1 AND 10
              AND value IS NOT NULL GROUP BY 1, 2""", "model_run_id, decile")
        # extrapolated predictions (full-universe extension, scope 'extrapolated'): the MVP OOS decile returns are not
        # validated outside the model universe, so no expected excess return / interval is attached to them
        # (a worker started before ml_predictions v3 has no `scope` column in its lake view: treat as mvp)
        has_scope = "scope" in {r[0] for r in self.con.execute(f"DESCRIBE {T}ml_pred").fetchall()}
        sc = "p.scope" if has_scope else "CAST(NULL AS VARCHAR)"
        self.create("ml_latest", f"""
            SELECT p.model_run_id, p.date, p.security_id, p.horizon, p.score, p.rank_pct, p.decile, p.shap_top,
                   CASE WHEN {sc} = 'extrapolated' THEN NULL ELSE d.mean END AS exp_excess,
                   CASE WHEN {sc} = 'extrapolated' THEN NULL ELSE d.p10 END AS p10,
                   CASE WHEN {sc} = 'extrapolated' THEN NULL ELSE d.p90 END AS p90,
                   coalesce({sc}, 'mvp') AS scope
            FROM {T}ml_pred p LEFT JOIN {T}ml_decile_ret d ON d.model_run_id = p.model_run_id AND d.decile = p.decile
            QUALIFY row_number() OVER (PARTITION BY p.security_id, p.horizon, p.model_run_id
                                       ORDER BY p.is_live DESC, p.date DESC) = 1""", "security_id, horizon")
        ensure_lake_views(self.con, ("ml_scope_status",), self.s)
        self.create("ml_scope", "SELECT * EXCLUDE (computed_at) FROM lake.ml_scope_status", "security_id, horizon")
        self.create("macro_series", "SELECT * FROM lake.macro_series", "series_id")
        self.create("macro_obs", f"""SELECT series_id, date, value FROM lake.macro_observations WHERE knowledge_date <= {A}
            QUALIFY row_number() OVER (PARTITION BY series_id, date ORDER BY knowledge_date DESC) = 1""",
                    "series_id, date")
        self.create("macro_regimes", f"SELECT * FROM lake.macro_regimes WHERE date <= {A}", "model_id, date")
        self.create("macro_betas", _latest_sql("*", "lake.macro_betas", f"as_of_date <= {A}", "security_id"),
                    "security_id, factor_id")

    # ---------------------------------------------------------------- macro v2 wave 1 (macro-w1; additive)
    def macro_v2_w1(self) -> None:
        """Heatmap cells (PIT snapshots), canonical history, cycle clock, revisions and ALFRED vintages."""
        A = self.A
        self.create("macro_cells", f"SELECT * FROM lake.macro_cells WHERE as_of_date <= {A}", "as_of_date, country, indicator")
        self.create("macro_cell_history", f"SELECT * FROM lake.macro_cell_history WHERE date <= {A}",
                    "country, series_id, measure, date")
        self.create("macro_cycle", f"SELECT * FROM lake.macro_cycle WHERE date <= {A}", "country, date")
        self.create("macro_revisions", "SELECT * FROM lake.macro_revisions", "series_id, date")
        self.create("macro_vintages", f"""SELECT o.series_id, o.date, o.value, o.knowledge_date, o.knowledge_date_estimated
            FROM lake.macro_observations o
            WHERE o.knowledge_date <= {A} AND o.series_id IN (SELECT DISTINCT series_id FROM lake.macro_revisions
                                                              WHERE vintage_kind = 'alfred')""",
                    "series_id, date, knowledge_date")

    # ---------------------------------------------------------------- macro v2 wave 2 (macro-w2; additive)
    def macro_v2_w2(self) -> None:
        """Official release calendar (incl. scheduled future releases), release outcomes, MODEL-BASED expectations
        (not consensus), surprises + surprise index, nowcasts (ours + official benchmarks), recession panel. Calendar
        tables are not cut at as_of: schedules and outcomes are what was known at build time."""
        self.create("macro_releases", "SELECT * FROM lake.macro_releases", "release_date, scheduled_at")
        self.create("macro_release_events", "SELECT * FROM lake.macro_release_events", "series_id, release_date")
        self.create("macro_expectations", "SELECT * FROM lake.macro_expectations", "series_id, scheduled_at, model")
        self.create("macro_surprises", "SELECT * FROM lake.macro_surprises", "series_id, release_date")
        self.create("macro_surprise_index", "SELECT * FROM lake.macro_surprise_index", "country, theme, date")
        self.create("macro_nowcasts", "SELECT * FROM lake.macro_nowcasts", "model_id, target_period, as_of")
        self.create("macro_recession", "SELECT * FROM lake.macro_recession", "method, country, date")

    # ---------------------------------------------------------------- macro v3 wave 3 (macro-w3; additive)
    def macro_v3(self) -> None:
        """Central-bank monitor: policy rates/corridors (business days since 1990), decisions, meetings (incl.
        scheduled future ones), rate-cycle phase, implied paths from official curves (approximation; ~2y history),
        display curves, projections, stance (model-based Taylor rules), balance sheets, tone (model-based). Not cut at
        as_of: these are what was known at build time (schedules / latest curves)."""
        self.create("cb_policy_rates", "SELECT * FROM lake.cb_policy_rates", "bank, date")
        self.create("cb_decisions", "SELECT * FROM lake.cb_decisions", "bank, decision_date")
        self.create("cb_meetings", "SELECT * FROM lake.cb_meetings", "bank, meeting_date")
        self.create("cb_rate_cycle", "SELECT * FROM lake.cb_rate_cycle", "bank, date")
        self.create("cb_implied_paths", "SELECT * FROM lake.cb_implied_paths", "bank, as_of, target_date")
        self.create("cb_implied_meta", "SELECT * FROM lake.cb_implied_meta", "bank")
        self.create("cb_curves", "SELECT * FROM lake.cb_curves", "bank, snapshot, tenor_years")
        self.create("cb_projections", "SELECT * FROM lake.cb_projections", "bank, release_date, variable, horizon")
        self.create("cb_stance", "SELECT * FROM lake.cb_stance", "bank, date")
        self.create("cb_balance_sheets", "SELECT * FROM lake.cb_balance_sheets", "bank, component, date")
        self.create("cb_tone", "SELECT * FROM lake.cb_tone", "bank, date")

    # ---------------------------------------------------------------- macro v2 wave 4 (macro-w4; additive)
    def macro_v4(self) -> None:
        """Macro -> markets: factor set, rolling exposures (aggregates: full PIT history; securities: latest as_of),
        latest fits for the scenario engine, regime-conditional performance, episode replay, screener pivot mx_sec."""
        A, T = self.A, self.t
        self.create("macro_factors", f"SELECT * FROM lake.macro_factors WHERE date <= {A}", "frequency, factor_id, date")
        self.create("macro_exposures", f"""SELECT * FROM lake.macro_exposures WHERE as_of_date <= {A}
            AND (entity_type <> 'security' OR as_of_date = (SELECT max(as_of_date) FROM lake.macro_exposures
                                                           WHERE entity_type = 'security' AND as_of_date <= {A}))""",
                    "entity_type, entity_id, model, factor_id, weighting, as_of_date")
        self.create("macro_exposure_fits", "SELECT * FROM lake.macro_exposure_fits", "entity_type, entity_id, model")
        self.create("regime_performance", "SELECT * FROM lake.regime_performance",
                    "regime_type, scope, entity_type, entity_id, weighting")
        self.create("macro_episode_replay", "SELECT * FROM lake.macro_episode_replay", "episode_id, entity_type, entity_id")
        self.create("mx_sec", f"""SELECT entity_id AS security_id,
                   max(beta) FILTER (model = 'univariate' AND factor_id = 'd10y') AS mx_beta_rates,
                   max(beta) FILTER (model = 'univariate' AND factor_id = 'usd') AS mx_beta_usd,
                   max(beta) FILTER (model = 'univariate' AND factor_id = 'oil') AS mx_beta_oil,
                   max(beta) FILTER (model = 'univariate' AND factor_id = 'dcredit') AS mx_beta_credit,
                   max(beta) FILTER (model = 'surprise' AND factor_id = 'surp_growth') AS mx_beta_growth,
                   exp(-max(beta) FILTER (model = 'joint' AND factor_id = 'd2y')) - 1 AS mx_rates_down_100,
                   max(as_of_date) AS mx_as_of
            FROM {T}macro_exposures WHERE entity_type = 'security' GROUP BY 1""", "security_id")

    # ---------------------------------------------------------------- the denormalized security table
    def sec(self) -> None:
        A, T = self.A, self.t
        reg = metric_registry(self.s)
        prof = profiles(self.s)
        kpi_of = subsector_kpi_set(self.s)
        kcase = " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in kpi_of.items()) or "WHEN NULL THEN NULL"
        exch = " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in EXCHANGE_NAMES.items())
        fcols = ", ".join(
            f"max(score) FILTER (factor_id = '{f}') AS f_{f}, max(coverage) FILTER (factor_id = '{f}') AS fc_{f}, "
            f"max(rank_in_sector) FILTER (factor_id = '{f}') AS fr_{f}, "
            f"max(sector_count) FILTER (factor_id = '{f}') AS fn_{f}, max(delta_1m) FILTER (factor_id = '{f}') AS fd_{f}, "
            f"max(rank_in_sector_full) FILTER (factor_id = '{f}') AS frf_{f}, "
            f"max(sector_count_full) FILTER (factor_id = '{f}') AS fnf_{f}"
            for f in FACTORS)
        ccols = ", ".join(
            f"max(score) FILTER (profile_id = '{p}') AS cs_{p}, max(grade) FILTER (profile_id = '{p}') AS gs_{p}, "
            f"max(rank_global) FILTER (profile_id = '{p}') AS rg_{p}, "
            f"max(rank_sector) FILTER (profile_id = '{p}') AS rs_{p}, max(coverage) FILTER (profile_id = '{p}') AS cc_{p}, "
            f"max(rank_global_full) FILTER (profile_id = '{p}') AS rgf_{p}, "
            f"max(n_global_full) FILTER (profile_id = '{p}') AS ngf_{p}, "
            f"max(rank_sector_full) FILTER (profile_id = '{p}') AS rsf_{p}, "
            f"max(n_sector_full) FILTER (profile_id = '{p}') AS nsf_{p}, "
            f"max(reason) FILTER (profile_id = '{p}') AS cr_{p}"
            for p in prof)
        # full-universe extension: scope of the balanced composite (mvp | extended) and its missing inputs
        ccols += (", max(scope) FILTER (profile_id = 'balanced') AS score_scope, "
                  "max(array_to_string(missing_inputs, ',')) FILTER (profile_id = 'balanced') AS score_missing")
        mids = reg["generic"] + reg["accounting"]
        # PIVOT (hash-based) is ~50x faster than one FILTER aggregate per metric
        for name, ids, prefix, src in (("metric_wide", mids, "m_", "ratios"), ("kpi_wide", reg["kpi"], "k_", "sector_kpis")):
            ins = ", ".join(_lit(m) for m in ids)
            outer = ", ".join(f"{_q(m)} AS {_q(prefix + m)}" for m in ids)
            self.create(name, f"""SELECT issuer_id, {outer} FROM (
                PIVOT (SELECT issuer_id, metric_id, value FROM {T}metric_latest WHERE src = '{src}')
                ON metric_id IN ({ins}) USING max(value) GROUP BY issuer_id)""", "issuer_id")
        mlcols = ", ".join(
            f"max(l.rank_pct) FILTER (l.horizon = '{h}') AS ml_rp_{h}, max(l.decile) FILTER (l.horizon = '{h}') AS ml_dec_{h}, "
            f"max(l.score) FILTER (l.horizon = '{h}') AS ml_score_{h}, max(l.model_run_id) FILTER (l.horizon = '{h}') AS ml_run_{h}, "
            f"max(l.date) FILTER (l.horizon = '{h}') AS ml_date_{h}, max(l.exp_excess) FILTER (l.horizon = '{h}') AS ml_exp_{h}, "
            f"bool_or(l.scope = 'extrapolated') FILTER (l.horizon = '{h}') AS ml_extrap_{h}"
            for h in HORIZONS)
        mlscols = ", ".join(
            f"max(status) FILTER (horizon = '{h}') AS ml_status_{h}, max(reason) FILTER (horizon = '{h}') AS ml_reason_{h}, "
            f"max(detail) FILTER (horizon = '{h}') AS ml_detail_{h}" for h in HORIZONS)
        # last split date per security (shares known before it are on a stale basis): the first bar after the last
        # bar whose split factor differs from the latest one (same as the lag() change point, without a window sort;
        # refresh-pipeline fixes 2026-10-26)
        self.create("_spl", f"""
            WITH p AS (SELECT security_id, date, split_factor FROM {T}prices WHERE date >= {A} - INTERVAL 800 DAY),
            l AS (SELECT security_id, arg_max(split_factor, date) AS lsf FROM p GROUP BY 1),
            b AS (SELECT p.security_id, max(p.date) AS before FROM p JOIN l USING (security_id)
                  WHERE p.split_factor IS NOT NULL AND abs(p.split_factor / NULLIF(l.lsf, 0) - 1) > 1e-6 GROUP BY 1)
            SELECT p.security_id, min(p.date) AS last_split_date FROM p JOIN b USING (security_id)
            WHERE p.date > b.before AND p.split_factor IS NOT NULL GROUP BY 1""")
        base = f"""
            WITH lst AS (SELECT listing_id, mic, exchange_country FROM {T}listings),
            -- issuer-level market cap of the primary security (valuation_daily: PIT, split- and ADR-consistent)
            ivm AS (SELECT s2.issuer_id, max(v2.market_cap_usd) AS issuer_mcap_usd
                    FROM {T}securities s2 JOIN {T}valuation_latest v2 USING (security_id)
                    WHERE s2.is_primary_security GROUP BY 1),
            spl AS (SELECT * FROM {T}_spl),
            uni AS (SELECT security_id, bool_or(universe_id = 'mvp_us') AS u_mvp_us,
                           bool_or(universe_id = 'mvp_global') AS u_mvp_global, bool_or(universe_id = 'full') AS u_full,
                           bool_or(universe_id = 'watch') AS u_watch FROM {T}universe GROUP BY 1),
            fp AS (SELECT security_id, max(as_of_date) AS score_date, {fcols} FROM {T}factor_latest GROUP BY 1),
            cp AS (SELECT security_id, max(dq_score) FILTER (profile_id = 'balanced') AS dq_score_c, {ccols}
                   FROM {T}composite_latest GROUP BY 1),
            mp AS (SELECT * FROM {T}metric_wide FULL OUTER JOIN {T}kpi_wide USING (issuer_id)),
            mo AS (SELECT issuer_id, moat_class, confidence AS moat_confidence, moat_score, as_of_date AS moat_as_of
                   FROM {T}moat QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY as_of_date DESC) = 1),
            ml AS (SELECT l.security_id, {mlcols} FROM {T}ml_latest l JOIN {T}ml_prod pr
                   ON pr.model_run_id = l.model_run_id AND pr.horizon = l.horizon GROUP BY 1),
            mls AS (SELECT security_id, {mlscols} FROM {T}ml_scope GROUP BY 1),
            sn AS (SELECT security_id, sum(news_count) AS news_count_30d FROM {T}sentiment_daily
                   WHERE date > {A} - INTERVAL 30 DAY GROUP BY 1),
            gd AS (SELECT issuer_id, arg_max(attention_z, date) AS attention_z FROM {T}gdelt_company_daily
                   WHERE date > {A} - INTERVAL 14 DAY GROUP BY 1),
            tz AS (SELECT security_id, zone_id AS tz_zone_id, side AS tz_side, center AS tz_center,
                          distance_atr AS tz_distance_atr, reward_risk AS tz_rr
                   FROM {T}zones QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY rank, abs(distance_atr)) = 1),
            fv0 AS (SELECT issuer_id, sum(fair_value_per_share * probability) FILTER (scenario IN ('bear','base','bull'))
                          / NULLIF(sum(probability) FILTER (scenario IN ('bear','base','bull')
                                   AND fair_value_per_share IS NOT NULL), 0) AS fv_weighted,
                          max(implied_revenue_cagr_5y) FILTER (scenario = 'market_implied') AS fv_implied_cagr,
                          max(currency) AS fv_currency, max(price) AS fv_price, max(security_id) AS fv_sid
                   FROM {T}fv_latest GROUP BY 1),
            eps0 AS (SELECT f.issuer_id, f.value AS eps_next, f.currency AS eps_ccy FROM {T}forecasts_latest f
                    WHERE f.scenario = 'base' AND f.item_id = 'eps_diluted' AND NOT f.is_actual
                    QUALIFY row_number() OVER (PARTITION BY f.issuer_id ORDER BY f.fiscal_year) = 1),
            -- refresh-pipeline fixes 2026-10-26: the issuer-level fv / eps rows are matched to securities here with a
            -- hash join on issuer_id + filter. As LEFT JOIN conditions (issuer_id = AND (a OR b) / coalesce(..) = ..)
            -- DuckDB ran them as blockwise nested-loop joins: ~87 of sec_base's 130 s. Same rows (one per issuer).
            fv AS (SELECT s.security_id, f.* EXCLUDE (issuer_id) FROM {T}securities s JOIN fv0 f ON f.issuer_id = s.issuer_id
                   WHERE f.fv_sid = s.security_id OR f.fv_currency = s.trading_currency),
            eps AS (SELECT s.security_id, e.eps_next FROM {T}securities s JOIN eps0 e ON e.issuer_id = s.issuer_id
                    WHERE coalesce(e.eps_ccy, s.trading_currency) = s.trading_currency)
            SELECT s.security_id, s.issuer_id, s.display_ticker AS ticker, {_display_name_sql('s.name', 'i.aliases')} AS name,
                   coalesce(i.name_local, CASE WHEN regexp_matches(s.name, {_lit(_CJK_RE)}) THEN s.name END) AS name_local,
                   i.short_name,
                   i.aliases, l.mic, CASE l.mic {exch} END AS exchange_name, l.exchange_country, s.isin, s.cusip, s.figi,
                   i.lei,
                   -- F33: mainland A-share listings trade in the CN market whatever the issuer's home region (A+H issuers)
                   CASE WHEN l.mic IN ('XSHG', 'XSHE') THEN 'CN' ELSE coalesce(i.region, 'OTHER') END AS region,
                   coalesce(i.country, l.exchange_country, '') AS country,
                   i.sector_id, i.subsector_id, CASE i.subsector_id {kcase} END AS kpi_set_id,
                   s.trading_currency AS currency, s.security_type, s.is_active, s.is_primary_security,
                   CAST(NULL AS VARCHAR) AS logo_url, i.reporting_currency, i.reporting_gaap, i.fiscal_year_end_mmdd,
                   i.description, i.website, i.employees, i.cik, i.lei, i.is_active AS issuer_active,
                   coalesce(u.u_mvp_us, false) AS u_mvp_us, coalesce(u.u_mvp_global, false) AS u_mvp_global,
                   coalesce(u.u_full, false) OR (u.security_id IS NULL AND coalesce(s.security_type, 'common')
                       NOT IN ('fx', 'future', 'index', 'etf')) AS u_full,  -- market instruments: never 'full'
                   coalesce(u.u_watch, false) AS u_watch,
                   px.* EXCLUDE (security_id), b.beta_1y,
                   fx.usd_per_unit AS fx_to_usd,
                   v.date AS val_date, coalesce(v.shares_out, fm.shares_outstanding) AS shares_out,
                   -- fallback (no valuation_daily row, e.g. non-primary ADRs): issuer shares are ordinary shares, so an
                   -- ADR price must be divided by the depositary ratio (TM: 10 ordinary per ADS)
                   -- 1) own valuation row 2) issuer-level mcap of the primary security (non-primary ADRs / classes)
                   -- 3) price x latest reported shares, only when those shares are fresh (<= 800 days, as in
                   --    valuation_daily) and known after the last split; ADR prices / depositary ratio
                   coalesce(v.market_cap_usd, ivm.issuer_mcap_usd,
                            CASE WHEN fm.shares_period_end >= {A} - INTERVAL 800 DAY
                                  AND fm.shares_knowledge_date >= coalesce(spl.last_split_date, DATE '1900-01-01')
                                 THEN px.close_usd * coalesce(v.shares_out, fm.shares_outstanding)
                                      / CASE WHEN s.security_type IN ('adr', 'gdr') THEN coalesce(s.adr_ratio, 1.0)
                                             ELSE 1.0 END END)
                       AS market_cap_usd,
                   v.enterprise_value_usd, v.pe_ttm, v.earnings_yield, v.pb, v.ps_ttm, v.ev_sales, v.ev_ebitda, v.ev_ebit,
                   v.fcf_yield, v.dividend_yield, v.shareholder_yield, v.p_ffo, v.p_tbv, v.size_bucket AS size_bucket_v,
                   fp.* EXCLUDE (security_id), cp.* EXCLUDE (security_id), mp.* EXCLUDE (issuer_id),
                   fm.fund_period_end, fm.fund_knowledge_date, fm.last_filing_date, fm.shares_outstanding, fm.gaap AS fund_gaap,
                   mo.moat_class, mo.moat_confidence, mo.moat_score, mo.moat_as_of,
                   ml.* EXCLUDE (security_id), mls.* EXCLUDE (security_id),
                   il.signal_score AS insider_signal, il.cluster_buy AS ins_cluster_buy, il.c_suite_buy AS ins_c_suite_buy,
                   il.net_buy_value_usd_90d AS ins_net_buy_90d, il.anomaly_score AS ins_anomaly, il.as_of_date AS ins_date,
                   sl.sentiment_score, sl.date AS sent_date, sn.news_count_30d, gd.attention_z,
                   tz.* EXCLUDE (security_id),
                   fv.fv_weighted, fv.fv_weighted / NULLIF(px.close, 0) - 1 AS fv_upside, fv.fv_implied_cagr, fv.fv_currency,
                   px.close / NULLIF(eps.eps_next, 0) AS pe_ntm,
                   si.short_pct_float, si.days_to_cover,
                   mx.* EXCLUDE (security_id)
            FROM {T}securities s
            LEFT JOIN {T}issuers i USING (issuer_id)
            LEFT JOIN lst l ON l.listing_id = s.primary_listing_id
            LEFT JOIN uni u USING (security_id)
            LEFT JOIN {T}px_stats px USING (security_id)
            LEFT JOIN {T}px_beta b USING (security_id)
            LEFT JOIN {T}fx_latest fx ON fx.currency = s.trading_currency
            LEFT JOIN {T}valuation_latest v USING (security_id)
            LEFT JOIN fp USING (security_id) LEFT JOIN cp USING (security_id)
            LEFT JOIN mp ON mp.issuer_id = s.issuer_id
            LEFT JOIN {T}fund_meta fm ON fm.issuer_id = s.issuer_id
            LEFT JOIN ivm ON ivm.issuer_id = s.issuer_id
            LEFT JOIN spl ON spl.security_id = s.security_id
            LEFT JOIN mo ON mo.issuer_id = s.issuer_id
            LEFT JOIN ml USING (security_id)
            LEFT JOIN mls USING (security_id)
            LEFT JOIN {T}insider_latest il USING (security_id)
            LEFT JOIN {T}sentiment_latest sl USING (security_id)
            LEFT JOIN sn USING (security_id) LEFT JOIN gd ON gd.issuer_id = s.issuer_id
            LEFT JOIN tz USING (security_id)
            -- issuer-level valuations are in the home listing's currency: attach only to that listing / same currency (not ADRs)
            LEFT JOIN fv ON fv.security_id = s.security_id
            LEFT JOIN eps ON eps.security_id = s.security_id
            LEFT JOIN {T}short_interest_latest si USING (security_id)
            LEFT JOIN {T}mx_sec mx USING (security_id)"""
        self.create("sec_base", base)
        # composites: stored score when present, else computed from factor scores with the profile weights
        comp = []
        for p, w in prof.items():
            expr = composite_sql(w)
            if p == "ml_blend":
                expr = (f"(CASE WHEN {expr} IS NOT NULL AND ml_rp_3M IS NOT NULL THEN 0.5 * {expr} + 0.5 * ml_rp_3M * 100 "
                        f"ELSE {expr} END)")
            # an explained null (insufficient_data row of the full-universe extension) is not replaced by the
            # factor-weighted fallback
            comp.append(f"CASE WHEN cr_{p} IS NOT NULL THEN NULL ELSE coalesce(cs_{p}, {expr}) END AS c_{p}")
        grades = []
        for p in prof:
            grades.append(f"coalesce(gs_{p}, CASE WHEN c_{p} IS NOT NULL THEN "
                          f"['A','B','C','D','E'][ntile(5) OVER (PARTITION BY c_{p} IS NULL ORDER BY c_{p} DESC)] END) AS g_{p}")
            grades.append(f"coalesce(rg_{p}, CASE WHEN c_{p} IS NOT NULL THEN "
                          f"row_number() OVER (PARTITION BY c_{p} IS NULL ORDER BY c_{p} DESC) END)::INTEGER AS rk_{p}")
            grades.append(f"coalesce(rs_{p}, CASE WHEN c_{p} IS NOT NULL THEN "
                          f"row_number() OVER (PARTITION BY sector_id, c_{p} IS NULL ORDER BY c_{p} DESC) END)::INTEGER AS rks_{p}")
        self.create("sec_c", f"SELECT *, {', '.join(comp)} FROM {T}sec_base")
        size = ("coalesce(size_bucket_v, CASE WHEN market_cap_usd >= 1e10 THEN 'large' WHEN market_cap_usd >= 2e9 "
                "THEN 'mid' WHEN market_cap_usd IS NOT NULL THEN 'small' END) AS size_bucket")
        self.create("sec", f"""
            SELECT *, {', '.join(grades)}, {size},
                   coalesce(dq_score_c, CASE WHEN fund_period_end IS NULL THEN NULL ELSE 1.0 END) AS dq_score,
                   lower(concat_ws(' ', ticker, name, name_local, short_name, isin, array_to_string(aliases, ' '))) AS search_text
            FROM {T}sec_c""", "security_id")
        for tmp in ("sec_base", "sec_c", "metric_wide", "kpi_wide", "_spl"):
            self.con.execute(f"DROP TABLE {T}{tmp}")

    # ---------------------------------------------------------------- events v2 (events-v2 agent, E7)
    def events_v2(self) -> None:
        """Global Events v2: dictionaries, T1 aggregates (3 y), stories aggregated over their days (salient subset:
        last 120 d n_articles >= 2 / issuer-linked / salience >= 2.2; 120-400 d issuer-linked or >= 5 articles),
        story x issuer index and members of multi-article stories (120 d)."""
        A, T = self.A, self.t
        self.create("ev2_dict", """SELECT dict_type, id, code, name, grp, theme_mask, sector_mask FROM lake.event_labels
            WHERE dict_type IN ('country', 'sector', 'theme', 'source', 'lang', 'model')""", "dict_type, id")
        self.create("ev2_issuer", f"""
            SELECT d.id AS iid, d.code AS issuer_id, s.security_id, coalesce(s.ticker, json_extract_string(d.extra, '$.ticker')) AS ticker,
                   coalesce(s.name, d.name) AS name, s.sector_id, coalesce(s.country, d.grp) AS country
            FROM lake.event_labels d
            LEFT JOIN (SELECT * FROM {T}sec QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY is_primary_security DESC,
                       market_cap_usd DESC NULLS LAST) = 1) s ON s.issuer_id = d.code
            WHERE d.dict_type = 'issuer'""", "iid")
        self.create("ev2_agg", f"""SELECT * EXCLUDE (year_month) FROM lake.event_daily_agg
            WHERE date <= {A} AND date > {A} - INTERVAL 3 YEAR""", "label_type, label_id, theme, date")
        # refresh-pipeline fixes 2026-10-26: news_stories carries ~15.6 M story-days in the 400-day window (un-pruned
        # recent months + CC-NEWS / E6 merges) and ONE GROUP BY story_id over all of them with list-valued arg_max /
        # list() aggregates (urls, domain_ids, countries, issuer lists; they cannot spill) ran > 1 h and hit the
        # 11.1 GiB limit (2026-10-25/26). 98 % of the stories have a single story-day, so:
        #  1. a fixed-width pass per story (days, articles, salience, last date, issuer-linked on any day) keeps the
        #     stories that can pass the salient filter (a superset of the final WHERE),
        #  2. single-day stories are projected straight from their row (the aggregate of one row is the row itself),
        #  3. only the multi-day stories go through the original list aggregate,
        # then the unchanged final WHERE applies to both. Same rows and values as before.
        W = f"date <= {A} AND date > {A} - INTERVAL 400 DAY"
        t0 = time.perf_counter()
        self.con.execute(f"""CREATE OR REPLACE TEMP TABLE _ev2_s AS
            SELECT story_id, count(*) AS nd FROM lake.news_stories WHERE {W} GROUP BY story_id
            HAVING (max(date) > {A} - INTERVAL 120 DAY AND (sum(n_articles) >= 2 OR bool_or(len(issuer_ids) > 0)
                                                            OR max(salience) >= 2.2))
                OR bool_or(len(issuer_ids) > 0) OR sum(n_articles) >= 5""")
        final = (f"(last_date > {A} - INTERVAL 120 DAY AND (n_articles >= 2 OR len(issuer_ids) > 0 OR salience >= 2.2)) "
                 "OR len(issuer_ids) > 0 OR n_articles >= 5")
        single = f"""
            SELECT n.story_id, n.date AS first_date, n.date AS last_date, n.first_ts, n.last_ts, n.title, n.lang_id,
                   n.n_articles::INTEGER AS n_articles, n.n_sources::INTEGER AS n_sources, 1::INTEGER AS n_days,
                   n.source_mask, n.domain_ids, n.urls,
                   (n.tone_x100 * n.n_articles) / nullif(CASE WHEN n.tone_x100 IS NOT NULL THEN n.n_articles END, 0)
                       / 100.0 AS tone,
                   n.sentiment_x100 / 100.0 AS sentiment, n.sentiment_model,
                   n.theme_mask, n.sector_mask, list_distinct(n.countries) AS countries,
                   n.issuer_ids, n.issuer_rel, n.n_events::INTEGER AS n_events, n.salience::DOUBLE AS salience
            FROM lake.news_stories n JOIN (SELECT story_id FROM _ev2_s WHERE nd = 1) k USING (story_id)
            WHERE n.{W.replace(" AND date", " AND n.date")}"""
        multi = f"""
                SELECT story_id, min(date) AS first_date, max(date) AS last_date, min(first_ts) AS first_ts,
                       max(last_ts) AS last_ts, arg_max(title, n_articles) AS title, arg_max(lang_id, n_articles) AS lang_id,
                       sum(n_articles)::INTEGER AS n_articles, max(n_sources)::INTEGER AS n_sources, count(*)::INTEGER AS n_days,
                       bit_or(source_mask) AS source_mask, arg_max(domain_ids, n_articles) AS domain_ids,
                       arg_max(urls, n_articles) AS urls,
                       sum(tone_x100 * n_articles) / nullif(sum(CASE WHEN tone_x100 IS NOT NULL THEN n_articles END), 0) / 100.0 AS tone,
                       arg_max(sentiment_x100, n_articles) FILTER (WHERE sentiment_x100 IS NOT NULL) / 100.0 AS sentiment,
                       arg_max(sentiment_model, n_articles) FILTER (WHERE sentiment_model IS NOT NULL) AS sentiment_model,
                       bit_or(theme_mask) AS theme_mask, bit_or(sector_mask) AS sector_mask,
                       list_distinct(flatten(list(countries))) AS countries,
                       arg_max(issuer_ids, n_articles) AS issuer_ids, arg_max(issuer_rel, n_articles) AS issuer_rel,
                       sum(n_events)::INTEGER AS n_events, max(salience)::DOUBLE AS salience
                FROM lake.news_stories WHERE {W} AND story_id IN (SELECT story_id FROM _ev2_s WHERE nd > 1)
                GROUP BY story_id"""
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE _ev2_g AS SELECT * FROM ({single}) WHERE {final}")
        self._exec_retry(f"INSERT INTO _ev2_g BY NAME SELECT * FROM ({multi}) WHERE {final}", "ev2_stories")
        self.con.execute("DROP TABLE _ev2_s")
        self.create("ev2_stories", "SELECT * FROM _ev2_g", "last_date DESC, salience DESC")
        self.con.execute("DROP TABLE _ev2_g")
        self._done("ev2_stories", t0)
        self.create("ev2_story_issuer", f"""
            SELECT story_id, unnest(issuer_ids) AS iid, unnest(issuer_rel) AS rel, last_date, salience, tone, sentiment
            FROM {T}ev2_stories WHERE len(issuer_ids) > 0""", "iid, last_date DESC")
        self.create("ev2_members", f"""
            SELECT m.story_id, m.date, m.source_id, m.domain_id, m.minute, m.tone_x100, m.lang_id
            FROM lake.story_members m SEMI JOIN (SELECT story_id FROM {T}ev2_stories WHERE n_articles >= 2) s
              ON s.story_id = m.story_id
            WHERE m.date <= {A} AND m.date > {A} - INTERVAL 120 DAY""", "story_id, date, minute")
        self.create("ev2_domains", f"""
            SELECT id, code FROM lake.event_labels WHERE dict_type = 'domain' AND id IN (
                SELECT DISTINCT unnest(domain_ids) FROM {T}ev2_stories UNION SELECT DISTINCT domain_id FROM {T}ev2_members)""",
                    "id")

    # ---------------------------------------------------------------- event models (event-models agent, B/C)
    def cftc(self) -> None:
        """CFTC positioning (COT, official): markets, signal + position history of served markets (active, curated
        or mapped; ~all of the history), latest row per report type x market x category, event-study cells."""
        self.create("cftc_markets", "SELECT * EXCLUDE (source_id, updated_at) FROM lake.cftc_markets", "market_code")
        self.create("cftc_series", """
            SELECT s.report_type, s.market_code, s.report_date, s.category, s.net, s.net_pct_oi, s.long_share, s.chg_1w,
                   s.chg_4w, s.chg_13w, s.flow_z, s.cot_index_1y, s.cot_index_3y, s.z_1y, s.z_3y, s.pct_rank_3y, s.extreme,
                   s.crowded, s.ret_13w, s.ret_26w, s.divergence_13w, s.divergence_26w, s.divergence_flag,
                   s.knowledge_date, p.long_pos, p.short_pos, p.spread_pos, p.traders_long, p.traders_short,
                   p.traders_spread, p.pct_oi_long, p.pct_oi_short, p.open_interest, p.chg_open_interest, p.traders_total,
                   p.conc_gross_4_long, p.conc_gross_4_short, p.conc_gross_8_long, p.conc_gross_8_short,
                   p.conc_net_4_long, p.conc_net_4_short, p.conc_net_8_long, p.conc_net_8_short, p.knowledge_ts,
                   p.knowledge_date_estimated
            FROM lake.cftc_signals s
            LEFT JOIN lake.cftc_positions p USING (report_type, market_code, report_date, category)
            WHERE s.market_code IN (SELECT market_code FROM lake.cftc_markets
                                    WHERE is_active OR is_major OR security_id IS NOT NULL)""",
                    "market_code, report_type, category, report_date")
        self.create("cftc_latest", """SELECT * FROM cftc_series
            QUALIFY row_number() OVER (PARTITION BY report_type, market_code, category ORDER BY report_date DESC) = 1"""
                    .replace("FROM cftc_series", f"FROM {self.t}cftc_series"), "report_type, market_code, category")
        self.create("cftc_events", "SELECT * EXCLUDE (computed_at) FROM lake.cftc_event_stats",
                    "market_code, report_type, category, signal, horizon_w")

    def cross_asset(self) -> None:
        """Cross-asset dashboard (roadmap #5, app.analytics.cross_asset): asset registry (code), the daily signal
        history (with the CFTC positioning join) and the latest row per asset, correlation regime / risk composite
        (model-based), correlation matrices, Yahoo futures term structures (approximate expiry) and their coverage."""
        from app.analytics.cross_asset import asset_registry

        T, A = self.t, self.A
        t0 = time.perf_counter()
        self.con.register("_xa_assets_df", asset_registry().to_arrow())
        try:
            self.con.execute(f"CREATE OR REPLACE TABLE {T}xa_assets AS SELECT * FROM _xa_assets_df ORDER BY sort_order")
        finally:
            self.con.unregister("_xa_assets_df")
        self.timings["xa_assets"] = round(time.perf_counter() - t0, 3)
        self.create("xa_signals", f"SELECT * EXCLUDE (computed_at, run_id) FROM lake.cross_asset_signals WHERE date <= {A}",
                    "asset_id, date")
        self.create("xa_latest", f"""SELECT * FROM {T}xa_signals
            QUALIFY row_number() OVER (PARTITION BY asset_id ORDER BY date DESC) = 1""", "asset_id")
        self.create("xa_regime", f"SELECT * EXCLUDE (computed_at, run_id) FROM lake.cross_asset_regime WHERE date <= {A}",
                    "date")
        self.create("xa_corr", "SELECT * EXCLUDE (computed_at) FROM lake.cross_asset_corr", 'snapshot, "window", asset_i, asset_j')
        self.create("xa_curve", f"""SELECT root, contract_month, date, contract, symbol, expiry, days_to_expiry, close, volume
            FROM lake.futures_curve WHERE date <= {A}""", "root, date, contract_month")
        self.create("xa_contracts", "SELECT * EXCLUDE (updated_at) FROM lake.futures_contracts", "root, contract_month")

    def rates(self) -> None:
        """W3 rates & debt (rates-w3; docs/COVERAGE_V2_PLAN.md): curve registry (code), official yield curves (full
        history, integer bp x 100), Svensson parameters, per-country latest benchmark yields + the per-security
        context table rates_sec (domicile-country yields, spreads, ICE OAS) for BQL, sovereign auctions, listed bonds
        of our issuers, FIRDS issuance by month, issuer debt profiles (+ latest per issuer), sector maturity walls,
        TRACE aggregates. Not cut at as_of (curves / calendars as known at build time)."""
        from app.rates import registry as RR

        T = self.t
        ensure_lake_views(self.con, ("yield_curves", "yield_curve_params", "sovereign_auctions", "bond_reference",
                                     "company_debt", "debt_maturity_walls", "credit_market_stats"), self.s)
        import pyarrow as pa

        t0 = time.perf_counter()
        rows = RR.registry_rows()
        self.con.register("_rates_reg_df", pa.Table.from_pylist(rows))
        try:
            self.con.execute(f"CREATE OR REPLACE TABLE {T}rates_registry AS SELECT * FROM _rates_reg_df ORDER BY curve_id")
        finally:
            self.con.unregister("_rates_reg_df")
        self.timings["rates_registry"] = round(time.perf_counter() - t0, 3)
        self.create("yc", "SELECT date, curve_id, tenor_months, type, value_bp_x100 FROM lake.yield_curves",
                    "curve_id, type, tenor_months, date")
        self.create("yc_meta", f"""SELECT curve_id, type, min(date) AS first_date, max(date) AS last_date,
                   count(DISTINCT date) AS n_dates, count(*) AS n_rows FROM {T}yc GROUP BY 1, 2""", "curve_id, type")
        self.create("yc_params", "SELECT * EXCLUDE (ingested_at) FROM lake.yield_curve_params", "curve_id, date")
        # latest benchmark yields per country: the country benchmark curve (monitor type), else its monthly LT10
        self.create("rates_country", f"""
            WITH b AS (SELECT r.country, r.curve_id, r.monitor_type AS type FROM {T}rates_registry r
                       WHERE r.is_country_benchmark OR r.grp = 'lt10'),
                 l AS (SELECT y.curve_id, y.type, max(y.date) AS d FROM {T}yc y JOIN b USING (curve_id, type) GROUP BY 1, 2),
                 v AS (SELECT y.curve_id, y.type, l.d AS date,
                              max(y.value_bp_x100) FILTER (y.tenor_months = 24) / 1e6 AS y2,
                              max(y.value_bp_x100) FILTER (y.tenor_months = 60) / 1e6 AS y5,
                              max(y.value_bp_x100) FILTER (y.tenor_months = 120) / 1e6 AS y10,
                              max(y.value_bp_x100) FILTER (y.tenor_months = 360) / 1e6 AS y30
                       FROM {T}yc y JOIN l ON y.curve_id = l.curve_id AND y.type = l.type AND y.date = l.d GROUP BY 1, 2, 3),
                 c AS (SELECT b.country, b.curve_id, v.type, v.date, v.y2, v.y5, v.y10, v.y30,
                              (b.curve_id NOT LIKE '%_LT10') AS daily FROM b JOIN v USING (curve_id, type))
            SELECT * EXCLUDE (rk) FROM (SELECT *, row_number() OVER (PARTITION BY country ORDER BY daily DESC, date DESC) AS rk FROM c)
            WHERE rk = 1""", "country")
        self.create("rates_sec", f"""
            WITH de AS (SELECT y10 FROM {T}rates_country WHERE country = 'DE'),
                 us AS (SELECT y10 FROM {T}rates_country WHERE country = 'US'),
                 o AS (SELECT max(value) FILTER (series_id = 'fred:BAMLC0A0CM') / 100 AS ig,
                              max(value) FILTER (series_id = 'fred:BAMLH0A0HYM2') / 100 AS hy
                       FROM (SELECT series_id, value FROM {T}macro_obs WHERE series_id IN ('fred:BAMLC0A0CM', 'fred:BAMLH0A0HYM2')
                             QUALIFY row_number() OVER (PARTITION BY series_id ORDER BY date DESC) = 1))
            SELECT s.security_id, rc.curve_id AS rates_curve_id, rc.date AS rates_date, rc.y2 AS gov_2y, rc.y5 AS gov_5y,
                   rc.y10 AS gov_10y, rc.y30 AS gov_30y, (rc.y10 - rc.y2) * 1e4 AS curve_2s10s_bp,
                   (rc.y10 - (SELECT y10 FROM de)) * 1e4 AS spread_10y_vs_bund_bp,
                   (rc.y10 - (SELECT y10 FROM us)) * 1e4 AS spread_10y_vs_ust_bp,
                   (SELECT ig FROM o) AS us_ig_oas, (SELECT hy FROM o) AS us_hy_oas
            FROM {T}sec s LEFT JOIN {T}rates_country rc ON rc.country = s.country""", "security_id")
        self.create("sov_auctions", "SELECT * EXCLUDE (ingested_at) FROM lake.sovereign_auctions", "agency, auction_date")
        self.create("bonds_iss", """SELECT * EXCLUDE (ingested_at) FROM lake.bond_reference WHERE issuer_id IS NOT NULL""",
                    "issuer_id, maturity_date")
        self.create("bond_issuance_m", """SELECT source, CAST(date_trunc('month', first_trade_date) AS DATE) AS month,
                   coalesce(issue_amount_currency, currency) AS currency, coupon_type, security_type,
                   count(*) AS n, sum(issue_amount) AS amount
            FROM lake.bond_reference WHERE source = 'firds' AND first_trade_date >= DATE '2015-01-01'
            GROUP BY ALL""", "month, currency")
        self.create("company_debt", "SELECT * EXCLUDE (computed_at) FROM lake.company_debt", "issuer_id, period_end")
        self.create("debt_latest", f"""SELECT * FROM {T}company_debt
            QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY period_end DESC) = 1""", "issuer_id")
        self.create("debt_walls", "SELECT * EXCLUDE (computed_at) FROM lake.debt_maturity_walls", "region, sector_id, bucket")
        self.create("credit_stats", "SELECT * EXCLUDE (ingested_at) FROM lake.credit_market_stats", "dataset, category, metric, date")

    def earnings(self) -> None:
        """Earnings & corporate-events calendar (roadmap #6, app.earnings.*): released earnings per period with the
        price reaction and the primary MODEL-BASED expectations / surprises (not consensus) for EPS and revenue
        (earn_events), the upcoming calendar labelled by date source (earn_calendar), all expectation rows of the last
        12 years (earn_expectations), corporate events of the last 3 years + all future-dated ones (corp_events),
        season aggregates (earn_season) and the PEAD signal of the last 2 years (earn_pead)."""
        A = self.A

        def prim(item: str, p: str) -> str:
            return f"""LEFT JOIN (SELECT issuer_id, period_end, model AS {p}_model, expectation AS {p}_expected,
                                 lo80 AS {p}_lo80, hi80 AS {p}_hi80, actual AS {p}_actual, surprise AS {p}_surprise,
                                 surprise_pct AS {p}_surprise_pct, surprise_z AS {p}_surprise_z, currency AS {p}_currency,
                                 actual_knowledge_date AS {p}_actual_date
                          FROM lake.earnings_expectations WHERE is_primary AND item_id = '{item}') {p}
                       ON {p}.issuer_id = e.issuer_id AND {p}.period_end = e.period_end"""

        self.create("earn_events", f"""
            SELECT e.* EXCLUDE (computed_at, run_id),
                   r.reaction_date AS trade_reaction_date, r.benchmark_id, r.beta, r.sigma_daily, r.ret_m1_p1, r.car_m1_p1,
                   r.car_0_p5, r.car_p1_p20, r.bhar_m1_p1, r.bhar_0_p5, r.bhar_p1_p20, r.ar_day0, r.baseline_move_m1_p1,
                   r.move_ratio, r.gap_pct, r.gap_filled, r.drift_continuation, r.complete AS reaction_complete,
                   eps.* EXCLUDE (issuer_id, period_end), rev.* EXCLUDE (issuer_id, period_end, rev_currency)
            FROM lake.earnings_events e
            LEFT JOIN lake.earnings_reactions r ON r.issuer_id = e.issuer_id AND r.period_end = e.period_end
            {prim('eps_diluted', 'eps')}
            {prim('revenue', 'rev')}
            WHERE e.release_date <= {A}""", "issuer_id, period_end")
        self.create("earn_calendar", """
            SELECT c.* EXCLUDE (computed_at, run_id),
                   eps.expectation AS eps_expected, eps.lo80 AS eps_lo80, eps.hi80 AS eps_hi80,
                   rev.expectation AS rev_expected, rev.lo80 AS rev_lo80, rev.hi80 AS rev_hi80, rev.currency AS rev_currency
            FROM lake.earnings_calendar c
            LEFT JOIN (SELECT * FROM lake.earnings_expectations WHERE is_primary AND item_id = 'eps_diluted') eps
                   ON eps.issuer_id = c.issuer_id AND eps.period_end = c.period_end
            LEFT JOIN (SELECT * FROM lake.earnings_expectations WHERE is_primary AND item_id = 'revenue') rev
                   ON rev.issuer_id = c.issuer_id AND rev.period_end = c.period_end""", "expected_date, issuer_id")
        self.create("earn_expectations", f"""SELECT * EXCLUDE (computed_at, run_id, label) FROM lake.earnings_expectations
            WHERE period_end >= {A} - INTERVAL 12 YEAR""", "issuer_id, item_id, period_end, model")
        self.create("corp_events", f"""SELECT * EXCLUDE (computed_at, year) FROM lake.corporate_events
            WHERE event_date >= {A} - INTERVAL 3 YEAR""", "event_date, issuer_id")
        self.create("earn_season", "SELECT * EXCLUDE (computed_at) FROM lake.earnings_season", "season, region, sector_id")
        self.create("earn_pead", f"""SELECT * EXCLUDE (computed_at, run_id, label) FROM lake.earnings_pead
            WHERE signal_date >= {A} - INTERVAL 2 YEAR""", "security_id, signal_date")

    def smart_money(self) -> None:
        """Smart money (roadmap #7; app.analytics.smart_money*, docs/SMART_MONEY.md): 13F ownership per quarter (PIT on
        the 13F deadline cutoff) + latest, top-100 holders of the latest two quarters per security (with filer flags),
        filers of the latest quarter, FINRA short-interest analytics (3 years) + latest, 13D / 13G events (3 years),
        EU / UK short disclosures (all) + current active positions / aggregates, the MODEL-BASED smart-money score
        (2 years) + latest, and sector aggregates. Everything filtered knowledge_date <= as_of."""
        T, A = self.t, self.A
        self.create("sm_inst", f"""SELECT * EXCLUDE (computed_at, run_id) FROM lake.inst_ownership
            WHERE knowledge_date <= {A}""", "security_id, period_end")
        self.create("sm_inst_latest", f"""SELECT * FROM {T}sm_inst
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY period_end DESC) = 1""", "security_id")
        self.create("sm_filers", f"""SELECT * EXCLUDE (computed_at, run_id) FROM lake.inst_filers
            WHERE knowledge_date <= {A} AND period_end >= (SELECT max(period_end) FROM lake.inst_filers
                                                          WHERE knowledge_date <= {A}) - INTERVAL 100 DAY""",
                    "period_end, filer_cik")
        # holders of the latest two quarters (cutoffs from sm_inst), top 100 by shares per security and quarter
        self.create("sm_holders", f"""
            WITH q AS (SELECT DISTINCT period_end, knowledge_date AS cutoff FROM {T}sm_inst),
            ql AS (SELECT * FROM q ORDER BY period_end DESC LIMIT 2),
            h AS (SELECT h.security_id, h.period_end, h.filer_cik, any_value(h.filer_name) AS filer_name,
                         sum(h.shares) AS shares, sum(h.value_usd) AS value_usd, max(h.knowledge_date) AS filed_date
                  FROM lake.institutional_holdings h JOIN ql ON ql.period_end = h.period_end AND h.knowledge_date <= ql.cutoff
                  WHERE h.security_id IS NOT NULL AND h.put_call = 'none' AND coalesce(h.amount_type, 'SH') = 'SH'
                    AND h.shares > 0
                  GROUP BY 1, 2, 3),
            r AS (SELECT h.*, row_number() OVER (PARTITION BY h.security_id, h.period_end ORDER BY h.shares DESC) AS rk,
                         h.period_end = (SELECT max(period_end) FROM ql) AS is_latest FROM h),
            top AS (SELECT DISTINCT security_id, filer_cik FROM r WHERE rk <= 100)
            SELECT r.* EXCLUDE (is_latest), f.hedge_fund_like, f.in_smart_cohort, f.aum_usd AS filer_aum_usd,
                   r.value_usd / NULLIF(f.aum_usd, 0) AS weight_in_portfolio
            FROM r LEFT JOIN {T}sm_filers f ON f.filer_cik = r.filer_cik AND f.period_end = r.period_end
            -- each quarter: every filer in the top 100 of EITHER quarter (so changes / exits are exact for them)
            JOIN top t ON t.security_id = r.security_id AND t.filer_cik = r.filer_cik""",
                    "security_id, period_end, shares DESC")
        self.create("sm_si", f"""SELECT * EXCLUDE (computed_at, run_id, year) FROM lake.si_signals
            WHERE knowledge_date <= {A} AND settlement_date >= {A} - INTERVAL 3 YEAR""", "security_id, settlement_date")
        self.create("sm_si_latest", f"""SELECT * FROM {T}sm_si
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY settlement_date DESC) = 1""", "security_id")
        self.create("sm_events", f"""SELECT * EXCLUDE (computed_at, year) FROM lake.activist_events
            WHERE knowledge_date <= {A} AND event_date >= {A} - INTERVAL 3 YEAR""", "event_date DESC, filing_id")
        self.create("sm_eu", f"""SELECT * EXCLUDE (source_id, ingested_at) FROM lake.eu_short_positions
            WHERE knowledge_date <= {A}""", "isin, position_date")
        # current register: latest notification per holder (>= 0.5%, within 2 years) + latest regulator aggregates
        self.create("sm_eu_current", f"""
            WITH l AS (SELECT *, count(*) OVER (PARTITION BY regulator, position_type, holder_name, isin) AS n_notifications,
                              lead(position_pct) OVER (PARTITION BY regulator, position_type, holder_name, isin
                                                       ORDER BY position_date DESC, knowledge_date DESC) AS prev_pct,
                              lead(position_date) OVER (PARTITION BY regulator, position_type, holder_name, isin
                                                        ORDER BY position_date DESC, knowledge_date DESC) AS prev_date
                       FROM {T}sm_eu
                       QUALIFY row_number() OVER (PARTITION BY regulator, position_type, holder_name, isin
                                                  ORDER BY position_date DESC, knowledge_date DESC) = 1)
            -- a regulator whose named-holder register stopped (UK FCA since 10 Jul 2026: aggregates only) has no
            -- current named positions: its last individual notification is > 30 days older than its aggregates
            , frozen AS (SELECT regulator FROM {T}sm_eu GROUP BY 1
                         HAVING max(position_date) FILTER (WHERE position_type = 'individual')
                                < max(position_date) FILTER (WHERE position_type = 'aggregate') - INTERVAL 30 DAY)
            SELECT * FROM l WHERE position_date > {A} - INTERVAL 730 DAY
              AND ((position_type = 'individual' AND NOT below_threshold AND position_pct >= 0.5
                    AND regulator NOT IN (SELECT regulator FROM frozen))
                   OR (position_type = 'aggregate' AND coalesce(position_pct, 0) > 0))""",
                    "position_pct DESC NULLS LAST")
        self.create("sm_signals", f"""SELECT * EXCLUDE (computed_at, run_id, year) FROM lake.smart_money_signals
            WHERE as_of_date <= {A} AND as_of_date >= {A} - INTERVAL 2 YEAR""", "security_id, as_of_date")
        self.create("sm_signals_latest", f"""SELECT * FROM {T}sm_signals
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY as_of_date DESC) = 1""", "security_id")
        self.create("sm_sector", f"""
            WITH s AS (SELECT security_id, sector_id, region, market_cap_usd FROM {T}sec
                       WHERE is_primary_security AND sector_id IS NOT NULL),
            io AS (SELECT * FROM {T}sm_inst_latest WHERE period_end >= (SELECT max(period_end) FROM {T}sm_inst) - INTERVAL 100 DAY),
            si AS (SELECT * FROM {T}sm_si_latest WHERE settlement_date >= {A} - INTERVAL 45 DAY),
            eu AS (SELECT security_id, max(eu_pct) AS eu_pct FROM (
                       SELECT security_id, max(position_pct) AS eu_pct FROM {T}sm_eu_current
                       WHERE position_type = 'aggregate' AND security_id IS NOT NULL GROUP BY 1
                       UNION ALL
                       SELECT security_id, sum(position_pct) FROM {T}sm_eu_current WHERE position_type = 'individual'
                         AND security_id IS NOT NULL AND regulator NOT IN (
                             SELECT DISTINCT regulator FROM {T}sm_eu_current WHERE position_type = 'aggregate')
                       GROUP BY 1) GROUP BY 1),
            sg AS (SELECT * FROM {T}sm_signals_latest WHERE as_of_date >= {A} - INTERVAL 14 DAY)
            SELECT s.sector_id, s.region, count(*) AS n_securities,
                   count(io.security_id) AS n_13f, median(io.inst_own_pct) AS median_inst_own_pct,
                   median(io.inst_own_chg) AS median_inst_own_chg,
                   avg(io.breadth) AS avg_breadth, sum(io.smart_buyers) AS smart_buyers, sum(io.smart_sellers) AS smart_sellers,
                   avg(io.crowding_pct) AS avg_crowding_pct,
                   count(si.security_id) AS n_si, median(si.si_pct_out) AS median_si_pct_out,
                   avg(si.days_to_cover) AS avg_days_to_cover, count(*) FILTER (WHERE si.squeeze_flag) AS n_squeeze,
                   count(eu.security_id) AS n_eu_shorted, avg(eu.eu_pct) AS avg_eu_short_pct,
                   avg(sg.score) AS avg_score, count(sg.security_id) AS n_scored,
                   max(io.period_end) AS inst_period_end, max(io.knowledge_date) AS inst_cutoff
            FROM s LEFT JOIN io USING (security_id) LEFT JOIN si USING (security_id)
            LEFT JOIN eu USING (security_id) LEFT JOIN sg USING (security_id)
            GROUP BY GROUPING SETS ((s.sector_id, s.region), (s.sector_id))""", "sector_id, region")

    def events_models(self) -> None:
        """Spatial / dynamic event models (all model-based): hotspots (last 3 y of weeks), spillovers / IRFs (last
        400 d of runs), connectedness (system + node history, pair rows of the latest window per universe),
        escalation (last 8 runs), change points (last 2 y), GPR (all), event-study library, company exposure (last 26
        snapshots) and the latest GPR validation report (data/logs/reports/gpr_validation_*.json)."""
        T = self.t
        self.create("em_hotspots", """SELECT * EXCLUDE (year, input_hash, computed_at) FROM lake.event_hotspots
            WHERE week >= (SELECT max(week) FROM lake.event_hotspots) - INTERVAL 1100 DAY""", "theme, measure, week, iso2")
        self.create("em_spillovers", """SELECT * EXCLUDE (computed_at) FROM lake.spatial_spillovers
            WHERE as_of >= (SELECT max(as_of) FROM lake.spatial_spillovers) - INTERVAL 400 DAY""",
                    "response, shock, w_kind, as_of, model")
        self.create("em_connectedness", """SELECT * EXCLUDE (computed_at) FROM lake.connectedness c
            WHERE row_type <> 'pair' OR date = (SELECT max(date) FROM lake.connectedness c2
                                                WHERE c2.universe = c.universe AND c2.row_type = 'pair')""",
                    "universe, row_type, date, node")
        self.create("em_irf", """SELECT * EXCLUDE (computed_at) FROM lake.event_irf
            WHERE as_of >= (SELECT max(as_of) FROM lake.event_irf) - INTERVAL 400 DAY""",
                    "model, response, shock, scope, component, h")
        self.create("em_escalation", """SELECT * EXCLUDE (computed_at) FROM lake.event_escalation
            WHERE as_of IN (SELECT DISTINCT as_of FROM lake.event_escalation ORDER BY as_of DESC LIMIT 8)""",
                    "as_of, theme_group, iso2")
        self.create("em_changepoints", """SELECT * EXCLUDE (computed_at) FROM lake.event_changepoints
            WHERE date >= (SELECT max(date) FROM lake.event_changepoints) - INTERVAL 730 DAY""", "date, label_type, label")
        self.create("em_gpr", "SELECT * EXCLUDE (computed_at) FROM lake.gpr_index", "iso2, date")
        self.create("em_event_studies", "SELECT * EXCLUDE (computed_at) FROM lake.event_study_library",
                    "theme, shock_kind, \"window\", level, iso2, sector")
        self.create("em_exposure", """SELECT * EXCLUDE (computed_at) FROM lake.company_event_exposure
            WHERE as_of IN (SELECT DISTINCT as_of FROM lake.company_event_exposure ORDER BY as_of DESC LIMIT 26)""",
                    "security_id, as_of")
        rep_date, rep = None, None
        try:
            files = sorted((self.s.data_dir / "logs" / "reports").glob("gpr_validation_*.json"))
            if files:
                rep = files[-1].read_text(encoding="utf-8")
                rep_date = datetime.strptime(files[-1].stem.rsplit("_", 1)[-1], "%Y%m%d").date()
        except Exception:  # noqa: BLE001 - optional report
            rep_date, rep = None, None
        t0 = time.perf_counter()
        self.con.execute(f"CREATE OR REPLACE TABLE {T}em_gpr_validation (report_date DATE, report JSON)")
        if rep is not None:
            self.con.execute(f"INSERT INTO {T}em_gpr_validation VALUES (?, ?)", [rep_date, rep])
        self.timings["em_gpr_validation"] = round(time.perf_counter() - t0, 3)

    def supply_chain(self) -> None:
        """Supply-chain & revenue-geography map (roadmap #9, app.supply_chain.*, docs/SUPPLY_CHAIN.md): latest snapshot
        of the disclosed edges (sc_edges), per-issuer metrics (sc_metrics), direct / second-order / look-through
        exposure (sc_exposure), the model-based country risk overlay of the latest run (sc_country_risk) and the
        STATISTICAL return-correlation neighbours (sc_neighbours)."""
        self.create("sc_edges", """SELECT * EXCLUDE (computed_at) FROM lake.sc_edges
            WHERE as_of = (SELECT max(as_of) FROM lake.sc_edges)""", "supplier_key, customer_key")
        self.create("sc_metrics", """SELECT * EXCLUDE (computed_at) FROM lake.sc_metrics
            WHERE as_of = (SELECT max(as_of) FROM lake.sc_metrics)""", "issuer_id")
        self.create("sc_exposure", """SELECT * EXCLUDE (computed_at) FROM lake.sc_exposure
            WHERE as_of = (SELECT max(as_of) FROM lake.sc_exposure)""", "issuer_id, dim, key")
        self.create("sc_country_risk", """SELECT * EXCLUDE (computed_at) FROM lake.country_risk
            WHERE as_of = (SELECT max(as_of) FROM lake.country_risk)""", "iso2")
        self.create("sc_neighbours", """SELECT * EXCLUDE (computed_at, layer) FROM lake.company_neighbours
            WHERE as_of = (SELECT max(as_of) FROM lake.company_neighbours)""", "issuer_id, method, rank")

    def conformal(self) -> None:
        """Conformal ellipsoids (docs/CONFORMAL_TECHNICALS_PLAN.md P3, scope per the P4 gate in
        docs/CONFORMAL_VALIDATION.md; all model-based): cf_envelopes = the latest conformal_envelopes origin per
        security, compact (marginal per-horizon radii qm_*, shape v_* / mu_*, next-bar bands for the experimental
        views; the joint simultaneous b{h}_* bands FAILED the gate and are not served), cf_touch_curves (latest
        calibration), cf_peer_groups (latest definition version), cf_peer_scores (last year), cf_peer_members (the
        latest PEER_LAW_SESSIONS sessions per group, float32: the latest gives the attribution and the next-session
        law; the history calibrates marginal pair / basket bands at query time) and cf_coverage (the latest audit per
        application, incl. the P4 walk-forward rows)."""
        A = self.A
        hz, lv = (1, 5, 10, 20), (80, 90, 95)
        cols = ["as_of_date", "security_id", "region", "pool_key", "sector_id", "filter", "radius_mode", "last_close",
                "sigma_d", "earn_jump_var", "earn_days_ahead", "earn_in_window", "q_n_cal", "q_pool_kind", "nb_earn"]
        cols += [f"{p}_{h}" for h in hz for p in ("mu", "v")]
        cols += [f"qm_{h}_{x}" for h in hz for x in lv]
        cols += [f"nb_{k}_{x}_{e}" for k in "hlc" for x in lv for e in ("lo", "hi")]
        cols += ["fan_v"] + [f"fan_qm_{x}" for x in lv]       # Technicals v2 T1 fan (NULL in rows written before it)
        self.create("cf_envelopes", f"""SELECT {', '.join(cols)} FROM lake.conformal_envelopes
            WHERE as_of_date <= {A} AND last_close IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY as_of_date DESC) = 1""", "security_id")
        self.create("cf_touch_curves", """SELECT * EXCLUDE (computed_at) FROM lake.conformal_touch_curves
            WHERE as_of_date = (SELECT max(as_of_date) FROM lake.conformal_touch_curves)""",
                    "pool_key, horizon, side, kind")
        self.create("cf_peer_groups", """SELECT * EXCLUDE (computed_at) FROM lake.conformal_peer_groups
            WHERE version = (SELECT max(version) FROM lake.conformal_peer_groups)""", "group_id, member_rank")
        self.create("cf_peer_scores", f"""SELECT * EXCLUDE (computed_at, run_id, year) FROM lake.conformal_peer_scores
            WHERE as_of_date <= {A} AND as_of_date > {A} - INTERVAL 366 DAY""", "group_id, as_of_date")
        num = ", ".join(f"CAST({c} AS REAL) AS {c}" for c in ("r", "z_marg", "z_cond", "contrib", "contrib_share", "mu_next",
                                                               "d_next", "l1_next", "l2_next", "l3_next", "l4_next"))
        self.create("cf_peer_members", f"""SELECT as_of_date, group_id, security_id, {num} FROM lake.conformal_peer_members
            WHERE as_of_date <= {A} AND as_of_date > {A} - INTERVAL 220 DAY
            QUALIFY dense_rank() OVER (PARTITION BY group_id ORDER BY as_of_date DESC) <= {PEER_LAW_SESSIONS}""",
                    "group_id, as_of_date, security_id")
        self.create("cf_coverage", """SELECT * EXCLUDE (computed_at, model_based) FROM lake.conformal_coverage c
            WHERE as_of_date = (SELECT max(as_of_date) FROM lake.conformal_coverage c2
                                WHERE c2.application = c.application)""",
                    "application, filter, segment_type, segment, horizon, level")

    def technicals_prob(self) -> None:
        """Technicals v2 probability models (docs/TECHNICALS_V2_PLAN.md T2-T4; gate docs/TECHNICALS_V2_VALIDATION.md;
        model-based): tp_setups / tp_zones / tp_scores = the latest nightly rows per security (T2 two-barrier setup
        probabilities per horizon, T3 zone hold / edge vs random, T4 percentile sub-scores + score-bucket stats),
        tp_surfaces = the latest T2 surface fit (query-time probabilities for thesis target / stop), tp_score_buckets
        = the latest T4 decile table, tp_gates = the latest gate verdicts (production / experimental)."""
        A = self.A
        for name, src in (("tp_setups", "tech_prob_setups"), ("tp_zones", "tech_prob_zones"), ("tp_scores", "tech_prob_scores")):
            self.create(name, _latest_sql("* EXCLUDE (computed_at)", f"lake.{src}", f"as_of_date <= {A}", "security_id"),
                        "security_id")
        for name, src, order in (("tp_surfaces", "tech_prob_surfaces", "region, vol_regime, earn_window, horizon"),
                                 ("tp_score_buckets", "tech_prob_score_buckets", "score_col, horizon, decile"),
                                 ("tp_gates", "tech_prob_gates", "output")):
            self.create(name, f"""SELECT * EXCLUDE (computed_at) FROM lake.{src}
                WHERE fit_date = (SELECT max(fit_date) FROM lake.{src} WHERE fit_date <= {A} + INTERVAL 7 DAY)""", order)

    def technicals_meta(self) -> None:
        """Technicals v3 #2 meta-labelled setups (gate docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling"; model-based):
        tm_setups = the latest nightly tech_meta_setups rows per security (meta P(win) / P(loss) / P(neither), meta
        expected R, take / skip, quarter-Kelly risk fraction, drivers, context features) for H = 10 / 20 / 40, tm_gates
        = the latest gate verdicts (meta_h* calibration gate, decision_h* economic check)."""
        A = self.A
        self.create("tm_setups", _latest_sql("* EXCLUDE (computed_at)", "lake.tech_meta_setups", f"as_of_date <= {A}",
                                             "security_id"), "security_id, horizon")
        self.create("tm_gates", f"""SELECT * EXCLUDE (computed_at) FROM lake.tech_meta_gates
            WHERE fit_date = (SELECT max(fit_date) FROM lake.tech_meta_gates WHERE fit_date <= {A} + INTERVAL 7 DAY)""", "output")

    def technicals_v3(self) -> None:
        """Technicals v3 #1 / #5 (docs/TECHNICALS_V3_IDEAS.md; gate docs/TECHNICALS_V3_VALIDATION.md): tv3_factors = the
        latest tech_factors row per security (raw factors, percentiles, composite), tv3_eval = the walk-forward
        validation rows of tech_factor_eval (IC / gate per factor x horizon x region x segment; breakout gate rows
        bo_*) + the latest monitor run, tv3_bo_state = the latest breakout state per security, tv3_bo_events = the
        breakout events of the last 400 days."""
        A = self.A
        self.create("tv3_factors", _latest_sql(
            "* EXCLUDE (computed_at, year)", "lake.tech_factors",
            f"as_of_date <= {A} AND year >= year({A}) - 1 AND as_of_date > {A} - INTERVAL 20 DAY", "security_id"),
            "security_id")
        self.create("tv3_eval", """SELECT * EXCLUDE (computed_at) FROM lake.tech_factor_eval
            WHERE eval_kind = 'validation' OR eval_date = (SELECT max(eval_date) FROM lake.tech_factor_eval
                                                         WHERE eval_kind = 'monitor')""",
                    "signal, horizon, region, segment_type, segment")
        self.create("tv3_bo_state", _latest_sql("* EXCLUDE (computed_at)", "lake.tech_breakout_state",
                                                f"as_of_date <= {A}", "security_id"), "security_id")
        self.create("tv3_bo_events", f"""SELECT * EXCLUDE (computed_at, year) FROM lake.tech_breakouts
            WHERE date <= {A} AND date > {A} - INTERVAL 400 DAY AND year >= year({A}) - 2""", "security_id, date")

    def meta(self, run_id: str, built_at: datetime) -> None:
        T = self.t
        rows = [("run_id", run_id), ("built_at", built_at.isoformat()), ("as_of", self.as_of.isoformat()),
                ("schema_version", str(SNAPSHOT_SCHEMA_VERSION))]
        self.con.execute(f"CREATE OR REPLACE TABLE {T}_meta (key VARCHAR, value VARCHAR)")
        self.con.executemany(f"INSERT INTO {T}_meta VALUES (?, ?)", rows)
        self.con.execute(f"CREATE OR REPLACE TABLE {T}_inputs (table_name VARCHAR, row_count BIGINT, status VARCHAR)")
        self.con.executemany(f"INSERT INTO {T}_inputs VALUES (?, ?, ?)",
                             [(k, int(v.get("rows", 0)), v.get("status", "empty")) for k, v in sorted(self.inputs.items())])
        # freshness per lake table (and per region where the table has one) for GET /system/status
        self.con.execute(f"CREATE OR REPLACE TABLE {T}_freshness (table_name VARCHAR, region VARCHAR, last_date DATE, "
                         "last_ingested_at TIMESTAMPTZ, row_count BIGINT)")
        contracts = load_contracts()
        for name, st in sorted(self.inputs.items()):
            if st.get("status") != "ok":
                continue
            cols = set(contracts[name].column_names)
            dcol = freshness_date_column(name, cols)
            icol = next((c for c in ("ingested_at", "computed_at", "updated_at") if c in cols), None)
            reg = "region" if "region" in cols else None
            # sentinel dates (identifier_map / listings use 1900-01-01 for "unknown start") never mean "data through"
            dexpr = (f"max(CASE WHEN CAST({_q(dcol)} AS DATE) > DATE '1901-01-01' THEN CAST({_q(dcol)} AS DATE) END)"
                     if dcol else "CAST(NULL AS DATE)")
            iexpr = f"max({_q(icol)})" if icol else "CAST(NULL AS TIMESTAMPTZ)"
            rsel = f"CAST({reg} AS VARCHAR)" if reg else "CAST(NULL AS VARCHAR)"
            try:
                self.con.execute(f"INSERT INTO {T}_freshness SELECT {_lit(name)}, {rsel}, {dexpr}, {iexpr}, count(*) "
                                 f"FROM lake.{_q(name)}" + (f" GROUP BY {reg}" if reg else ""))
            except duckdb.Error as e:
                log.info("freshness for %s unavailable: %s", name, e)
        # O1 / F16: per-section staleness (market data vs analytics) so a stale analytics section is visible
        try:
            from app.ops.sections import write_sections

            write_sections(self.con, T, self.as_of, self.s)
        except Exception as e:  # never fail a build on metadata
            log.warning("serving_sections unavailable: %s", e)


# "data through" column per lake table for GET /system/status. Slowly-changing reference tables (SCD2 valid_from /
# valid_to) carry the *validity start* of each row - often the 1900-01-01 sentinel - which says nothing about how
# current the table is: their freshness is the last update.
_FRESHNESS_DATE_OVERRIDES: dict[str, str] = {"listings": "updated_at", "identifier_map": "updated_at",
                                             "universe_membership": "updated_at", "securities_master": "updated_at",
                                             "issuers": "updated_at"}
_FRESHNESS_DATE_CANDIDATES = ("date", "as_of_date", "event_date", "settlement_date", "knowledge_date", "period_end",
                              "published_at", "checked_at", "started_at")


def freshness_date_column(table: str, cols: set[str]) -> str | None:
    """Column whose max() is the table's 'data through' date (None -> unknown). Never valid_from (see above)."""
    o = _FRESHNESS_DATE_OVERRIDES.get(table)
    if o and o in cols:
        return o
    c = next((c for c in _FRESHNESS_DATE_CANDIDATES if c in cols), None)
    if c:
        return c
    return next((c for c in ("updated_at", "ingested_at", "computed_at") if c in cols), None)


#: builder sections in build order (later sections read tables of earlier ones: sec <- prices / scores / ...,
#: events_v2 and smart_money <- sec)
SECTIONS = ("reference", "prices", "fundamentals", "scores", "alt", "technicals", "ml_macro", "macro_v2_w1",
            "macro_v2_w2", "macro_v3", "macro_v4", "sec", "events_v2", "events_models", "cftc", "cross_asset", "rates",
            "earnings",
            "smart_money", "supply_chain", "conformal", "technicals_prob", "technicals_meta", "technicals_v3")


def build_tables(con: duckdb.DuckDBPyConnection, target: str, as_of: date, run_id: str, settings: Settings,
                 inputs: dict[str, dict[str, Any]], empty: bool = False,
                 sections_out: dict[str, float] | None = None) -> dict[str, float]:
    """Create every serving table in `target` (catalog prefix, e.g. 'snap.' or ''). Returns per-table timings;
    `sections_out` receives the per-section timings."""
    con.execute("SET TimeZone = 'UTC'")
    b = _Builder(con, target, as_of, settings, inputs, empty)
    for name in SECTIONS:
        b.section(name, getattr(b, name))
    b.section("meta", lambda: b.meta(run_id, datetime.now(UTC)))
    if sections_out is not None:
        sections_out.update(b.sections)
    return b.timings


def _default_as_of(con: duckdb.DuckDBPyConnection) -> date:
    today = datetime.now(UTC).date()
    try:
        d = con.execute(f"SELECT max(date) FROM lake.prices_daily WHERE date <= {_lit(today)} "
                        "AND security_id NOT LIKE 'IDX:%' AND security_id NOT LIKE 'ETF:%'").fetchone()[0]
    except duckdb.Error:
        d = None
    return d or today


def empty_snapshot(settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """In-memory snapshot with every serving table present but empty (API dev fallback before the first build)."""
    s = settings or get_settings()
    con = duckdb.connect(":memory:")
    con.execute("SET enable_progress_bar = false")
    inputs = register_lake(con, s, empty=True)
    build_tables(con, "", datetime.now(UTC).date(), "empty", s, inputs, empty=True)
    return con


# ------------------------------------------------------------------------------------------------ build + publish


def snapshot_files(settings: Settings | None = None) -> list[Path]:
    s = settings or get_settings()
    return sorted(s.db_dir.glob("serving_*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True)


def cleanup_snapshots(keep: int = KEEP_SNAPSHOTS, settings: Settings | None = None) -> list[str]:
    """Delete all but the newest `keep` snapshots (never the live one). Locked files are skipped (Windows readers)."""
    from app.core.storage import current_serving_db

    s = settings or get_settings()
    live = current_serving_db(s)
    removed = []
    files = snapshot_files(s)
    for p in files[keep:]:
        if live is not None and p.resolve() == live.resolve():
            continue
        for extra in (p, p.with_name(p.name + ".wal")):
            try:
                if extra.exists():
                    extra.unlink()
                    removed.append(extra.name)
            except OSError as e:  # still open by a reader (Windows) -> try next time
                log.info("serving cleanup: %s in use (%s), kept", extra.name, e.__class__.__name__)
    now = time.time()
    for p in s.db_dir.glob("serving_*.duckdb.tmp*"):
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
                removed.append(p.name)
        except OSError:
            pass
    return removed


def _new_run_id() -> str:
    return "SV" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + secrets.token_hex(2).upper()


def warm_catalogs(s: Settings) -> None:
    """W4.7 (macro-w4): precompute the Model Studio variable catalog (macro series search) for the snapshot just
    published, so the API's first catalog / search request reads the disk cache instead of scanning the lake (~6-10 s)."""
    try:
        from app.modeling.datasets import build_catalog

        t0 = time.perf_counter()
        build_catalog(s)
        log.info("studio catalog warmed in %.1fs", time.perf_counter() - t0)
    except Exception as e:  # never fail a snapshot build on the cache warm-up
        log.warning("catalog warm-up skipped: %s", e)


def serving_temp_dir(settings: Settings) -> Path:
    """Spill directory of the build (BIG360_SERVING_TEMP_DIR, default data/tmp/duckdb_serving on the data disk)."""
    raw = str(getattr(settings, "serving_temp_dir", "") or "").strip()
    return Path(raw) if raw else settings.data_dir / "tmp" / "duckdb_serving"


def serving_build_connection(settings: Settings) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB for the snapshot build with the explicit serving budget (refresh-pipeline fixes 2026-10-26):
    memory_limit = BIG360_SERVING_DUCKDB_MEMORY (7GB; DuckDB "GB" = 10^9 bytes), threads =
    BIG360_SERVING_DUCKDB_THREADS (12), temp_directory on the data disk (max 60 GB of spill), and
    preserve_insertion_order = false (every table that needs an order has an explicit ORDER BY; unordered CTAS / joins
    may then stream and spill). Before: the generic job connection (BIG360_DUCKDB_MEMORY 12GB = 11.18 GiB, 16 threads,
    preserve_insertion_order = true)."""
    tmp = serving_temp_dir(settings)
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.glob("*.tmp"):  # spill files of a killed build
        try:
            old.unlink()
        except OSError:
            pass
    con = duckdb.connect(":memory:")
    con.execute(f"SET threads = {max(1, int(getattr(settings, 'serving_duckdb_threads', 12) or 12))}")
    con.execute(f"SET memory_limit = '{getattr(settings, 'serving_duckdb_memory_limit', '') or '7GB'}'")
    con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    con.execute("SET max_temp_directory_size = '60GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET enable_progress_bar = false")
    return con


def build_serving_db(settings: Settings | None = None, as_of: date | None = None, run_id: str | None = None,
                     publish: bool = True) -> BuildReport:
    """Build a snapshot from the current lake; publish it (flip CURRENT) unless publish=False."""
    s = settings or get_settings()
    s.db_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or _new_run_id()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    t0 = time.perf_counter()
    started = datetime.now(UTC)
    final = s.db_dir / f"serving_{run_id}.duckdb"
    tmp = s.db_dir / f"serving_{run_id}.duckdb.tmp"
    for p in (tmp, Path(str(tmp) + ".wal")):
        if p.exists():
            p.unlink()
    con = serving_build_connection(s)
    sections: dict[str, float] = {}
    try:
        inputs = register_lake(con, s)
        as_of = as_of or _default_as_of(con)
        con.execute(f"ATTACH '{tmp.as_posix()}' AS snap")
        timings = build_tables(con, "snap.", as_of, run_id, s, inputs, sections_out=sections)
        counts = {r[0]: int(r[1]) for r in con.execute(
            "SELECT table_name, estimated_size FROM duckdb_tables() WHERE database_name = 'snap'").fetchall()}
        t_cp = time.perf_counter()
        con.execute("CHECKPOINT snap")
        con.execute("DETACH snap")
        sections["checkpoint"] = round(time.perf_counter() - t_cp, 2)
    finally:
        con.close()
    os.replace(tmp, final)
    # verify: the published file must open read-only and carry our run id
    vcon = duckdb.connect(str(final), read_only=True)
    try:
        got = vcon.execute("SELECT value FROM _meta WHERE key = 'run_id'").fetchone()
        n_sec = vcon.execute("SELECT count(*) FROM sec").fetchone()[0]
    finally:
        vcon.close()
    if not got or got[0] != run_id:
        raise RuntimeError(f"snapshot verification failed for {final.name}")
    published = False
    if publish:
        publish_serving_db(final, s)
        published = True
        cleanup_snapshots(settings=s)
        warm_catalogs(s)
        try:  # startup sync (docs/STARTUP_SYNC_PLAN.md A3): section fingerprints of this snapshot for the incremental publish
            from app.ops.sync.publish import record_build

            record_build(run_id, started, as_of, "full", sections, None, s)
        except Exception as e:  # noqa: BLE001 - never fail a build on the record
            log.warning("serving publish record skipped: %s", e)
    secs = round(time.perf_counter() - t0, 2)
    log.info("serving snapshot %s built in %.1fs (as_of=%s, sec=%d, published=%s)", final.name, secs, as_of, n_sec,
             published)
    return BuildReport(run_id, final, as_of, published, secs, counts, inputs, timings, sections)


# ------------------------------------------------------------------------------------------------ job entry point


def _dq_failed_in_dag(ctx: Any) -> bool:
    """True when dq_checks ran in the same dag run and failed (blocking dq errors). Since O1 build_serving_db only
    soft-depends on dq_checks, so analytics failures upstream of dq (dq skipped) no longer block publishing fresh
    market data, but a real dq failure still keeps the previous snapshot live."""
    if not getattr(ctx, "dag_run_id", None):
        return False
    try:
        import polars as pl

        from app.core.storage import read_table

        jr = read_table("job_runs", columns=["job_id", "dag_run_id", "status", "started_at"], settings=ctx.settings)
        jr = jr.filter((pl.col("job_id") == "dq_checks") & (pl.col("dag_run_id") == ctx.dag_run_id))
        return bool(jr.height) and jr.sort("started_at")["status"][-1] == "failed"
    except Exception:  # pragma: no cover - dq bookkeeping is advisory here
        return False


def run(ctx: Any) -> Any:
    """jobs.yaml `build_serving_db`. Params: as_of (YYYY-MM-DD, default latest price date), publish (default true),
    force (publish even if the latest dq run has blocking errors; the DAG runner already skips us when dq_checks failed)."""
    from app.scheduler.base import JobResult

    params = ctx.params or {}
    publish = str(params.get("publish", "true")).lower() not in ("0", "false", "no")
    force = str(params.get("force", "false")).lower() in ("1", "true", "yes")
    if publish and not force and ctx.trigger not in ("manual", "api") and _dq_failed_in_dag(ctx):
        ctx.log.warning("build_serving_db: dq_checks failed in this dag run (blocking dq errors); building without "
                        "publishing - the previous snapshot stays live (pass force=true to publish anyway)")
        publish = False
    if publish and not force and ctx.trigger in ("manual", "api"):
        try:
            from app.core.dq import dq_summary

            if dq_summary(settings=ctx.settings).get("blocking"):
                ctx.log.warning("build_serving_db: latest dq run has blocking errors; building without publishing "
                                "(pass force=true to publish anyway)")
                publish = False
        except Exception as e:  # pragma: no cover - dq is advisory here
            ctx.log.info("dq summary unavailable: %s", e)
    as_of = date.fromisoformat(str(params["as_of"])) if params.get("as_of") else None
    rep = build_serving_db(ctx.settings, as_of=as_of, run_id=ctx.run_id, publish=publish)
    pending = sorted(k for k, v in rep.inputs.items() if v["status"] != "ok")
    msg = (f"{rep.path.name if rep.path else '-'} as_of={rep.as_of} in {rep.seconds}s published={rep.published}; "
           f"sec={rep.tables.get('sec', 0)} prices={rep.tables.get('prices', 0)}; empty inputs: {len(pending)}")
    ctx.log.info(msg)
    slow = sorted(rep.timings.items(), key=lambda kv: -kv[1])[:5]
    ctx.log.info("slowest serving tables: %s", ", ".join(f"{k}={v}s" for k, v in slow))
    if rep.sections:
        ctx.log.info("serving sections: %s", ", ".join(f"{k}={v}s" for k, v in rep.sections.items()))
    return JobResult(status="success", rows_written={"serving_snapshot": sum(rep.tables.values())},
                     entities_ok=rep.tables.get("sec", 0), message=msg)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Build the BiG Equity 360 serving snapshot")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--as-of")
    a = ap.parse_args()
    r = build_serving_db(as_of=date.fromisoformat(a.as_of) if a.as_of else None, publish=not a.no_publish)
    print(f"{r.path} as_of={r.as_of} {r.seconds}s published={r.published}")
    for k, v in sorted(r.timings.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {k:24s} {v:7.2f}s  rows={r.tables.get(k, 0)}")
    print("inputs:", {k: v["status"] for k, v in r.inputs.items() if v["status"] != "empty"})
