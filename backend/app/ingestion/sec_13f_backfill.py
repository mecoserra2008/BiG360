"""SEC Form 13F history backfill, memory-bounded (roadmap #7 smart money; job `sec_13f_backfill`, manual).

The weekly `sec_13f` job parses one ~100 MB window zip in memory (INFOTABLE ~400 MB of text) and upserts the
`period_end_year` partition through Polars. That is fine for one window but not for a dozen windows while other heavy
jobs run, so the backfill does the same thing in two bounded passes:

  1. stage: per dataset window (oldest first) download the zip, resolve submissions / amendments with the SAME code as
     the weekly job (`sec_13f.resolve_submissions`), extract INFOTABLE.tsv to a temp file and aggregate it with DuckDB
     (memory_limit, spill to disk) into one staged Parquet per window (identical rules to `parse_13f_zip`: lines summed
     per filer x period x CUSIP x put/call, VALUE in thousands before 2023-01-03, knowledge_date = FILING_DATE of the
     filing that supplied the row). The zip is deleted afterwards unless it was already on disk before the run.
  2. merge: per `period_end_year` touched by staged rows, DuckDB unions the existing partition files with the staged
     rows, keeps one row per primary key (latest knowledge_date, then filing_id, then newest window) and writes the
     partition into a temp dir that is swapped in under the table lock (atomic per partition, as write_table).
     security_id: existing value, else identity lookup by CUSIP.

Resumable: state in data/state/sec_13f_backfill.json (staged windows, merged). Staged files live in data/tmp/x7_13f/
and are removed after a successful merge. Windows merged are added to sec_13f.json datasets_done so the weekly job does
not re-download them. filings_index rows of the 13F submissions are written with write_table (small).

Run:  python -m app.cli jobs run sec_13f_backfill --param datasets='["2023q2","2023q3"]'   (or --param since=2023-04-01)
"""

from __future__ import annotations

import shutil
import uuid
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.schema import get_contract
from app.core.storage import (
    _swap_in,
    duckdb_connect,
    partition_dir,
    table_dir,
    table_lock,
    write_table,
)
from app.ingestion.sec_13f import (
    LANDING,
    VALUE_IN_DOLLARS_FROM,
    _window_end,
    dataset_links,
    filings_index_rows,
    resolve_submissions,
)
from app.ingestion.sec_index import SEC_RATE, SEC_SOURCE, try_lookup_many

log = get_logger("app.ingestion.sec_13f_backfill")

TABLE = "institutional_holdings"
PK = ("filer_cik", "period_end", "cusip", "put_call")
DUCK_MEMORY = "1500MB"
DUCK_THREADS = 4


# ------------------------------------------------------------------------------------------------ state


def _state_path(s: Settings) -> Path:
    return s.state_dir / "sec_13f_backfill.json"


def load_state(s: Settings) -> dict[str, Any]:
    p = _state_path(s)
    return orjson.loads(p.read_bytes()) if p.exists() else {"staged": {}, "merged": []}


def save_state(s: Settings, st: dict[str, Any]) -> None:
    p = _state_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = datetime.now(UTC).isoformat()
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(orjson.dumps(st, option=orjson.OPT_INDENT_2))
    tmp.replace(p)


def _stage_dir(s: Settings) -> Path:
    d = s.data_dir / "tmp" / "x7_13f"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _duck(s: Settings):
    con = duckdb_connect(None, settings=s)
    tmp = _stage_dir(s) / "duck_spill"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit = '{DUCK_MEMORY}'")
    con.execute(f"SET threads = {DUCK_THREADS}")
    con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


# ------------------------------------------------------------------------------------------------ stage


def _member(z: zipfile.ZipFile, suffix: str) -> str:
    return next(n for n in z.namelist() if n.upper().endswith(suffix))


STAGE_SQL = """
WITH info AS (
    SELECT ACCESSION_NUMBER AS acc, trim(NAMEOFISSUER) AS issuer_name, trim(TITLEOFCLASS) AS title_of_class,
           upper(trim(CUSIP)) AS cusip, nullif(trim({figi}), '') AS figi,
           TRY_CAST(replace(VALUE, ',', '') AS DOUBLE) AS v, TRY_CAST(replace(SSHPRNAMT, ',', '') AS DOUBLE) AS sh,
           trim(SSHPRNAMTTYPE) AS amount_type,
           CASE lower(trim(coalesce(PUTCALL, ''))) WHEN 'put' THEN 'put' WHEN 'call' THEN 'call' ELSE 'none' END AS put_call
    FROM read_csv('{path}', delim = '\t', header = true, all_varchar = true, quote = '', escape = '',
                  null_padding = true, strict_mode = false)
), j AS (
    SELECT i.*, u.cik, u.period, u.filed, u.mgr,
           CASE WHEN u.filed < DATE '{dollars_from}' THEN i.v * 1000 ELSE i.v END AS val
    FROM info i JOIN _use u ON u.acc = i.acc
    WHERE length(i.cusip) >= 6
)
SELECT cik AS filer_cik,
       coalesce(arg_max(mgr, (filed, acc)), 'UNKNOWN') AS filer_name,
       period AS period_end, cusip, put_call,
       coalesce(sum(sh), 0.0) AS shares, coalesce(sum(val), 0.0) AS value_usd,
       '{source}:' || arg_max(acc, (filed, acc)) AS filing_id,
       max(filed) AS knowledge_date,
       arg_min(issuer_name, (filed, acc)) AS issuer_name,
       arg_min(title_of_class, (filed, acc)) AS title_of_class,
       arg_min(figi, (filed, acc)) FILTER (WHERE figi IS NOT NULL) AS figi,
       arg_min(amount_type, (filed, acc)) AS amount_type
FROM j GROUP BY cik, period, cusip, put_call
"""


def stage_zip(zip_path: Path, out: Path, s: Settings | None = None) -> tuple[int, pl.DataFrame]:
    """One 13F window zip -> staged holdings Parquet (contract columns except security_id / ingested_at /
    period_end_year). Returns (rows, submission metadata for filings_index)."""
    s = s or get_settings()
    z = zipfile.ZipFile(zip_path)
    use, meta = resolve_submissions(z)
    work = _stage_dir(s) / f"work-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True)
    try:
        info_name = _member(z, "INFOTABLE.TSV")
        info_path = work / "INFOTABLE.tsv"
        with z.open(info_name) as src, open(info_path, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        header = info_path.open("r", encoding="utf-8", errors="replace").readline().rstrip("\r\n").split("\t")
        con = _duck(s)
        try:
            u = use.select(pl.col("ACCESSION_NUMBER").alias("acc"), "cik", "period", "filed",
                           pl.col("FILINGMANAGER_NAME").alias("mgr") if "FILINGMANAGER_NAME" in use.columns
                           else pl.lit(None, pl.Utf8).alias("mgr"))
            con.register("_use_df", u.to_arrow())
            con.execute("CREATE TEMP TABLE _use AS SELECT * FROM _use_df")
            sql = STAGE_SQL.format(path=info_path.as_posix(), dollars_from=VALUE_IN_DOLLARS_FROM.isoformat(),
                                   source=SEC_SOURCE, figi="FIGI" if "FIGI" in header else "CAST(NULL AS VARCHAR)")
            tmp_out = out.with_suffix(".part")
            con.execute(f"COPY ({sql}) TO '{tmp_out.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
            n = int(con.execute(f"SELECT count(*) FROM read_parquet('{tmp_out.as_posix()}')").fetchone()[0])
        finally:
            con.close()
        tmp_out.replace(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        z.close()
    fil = meta.select(pl.col("ACCESSION_NUMBER").alias("accession"), "cik", pl.col("SUBMISSIONTYPE").alias("form"),
                      "filed", "period", pl.col("FILINGMANAGER_NAME").alias("filer_name"))
    return n, fil


# ------------------------------------------------------------------------------------------------ merge


def _existing_files(s: Settings, year: int) -> list[Path]:
    c = get_contract(TABLE)
    d = partition_dir(table_dir(c, s), list(c.partition_by), [year])
    return sorted(d.glob("*.parquet")) if d.exists() else []


def _cusip_map(con, staged: list[Path]) -> pl.DataFrame:
    globs = ", ".join(f"'{p.as_posix()}'" for p in staged)
    cus = [r[0] for r in con.execute(f"SELECT DISTINCT cusip FROM read_parquet([{globs}])").fetchall()]
    m = try_lookup_many("cusip", cus)
    rows = [(k, v.get("primary_security_id") or v.get("security_id")) for k, v in m.items()]
    rows = [r for r in rows if r[1]]
    return pl.DataFrame(rows, schema={"cusip": pl.Utf8, "sid": pl.Utf8}, orient="row")


def merge_staged(staged: dict[str, Path], s: Settings | None = None, now: datetime | None = None) -> dict[int, int]:
    """Merge staged window files into institutional_holdings partitions. `staged` = {window_key: path}; later
    windows (by window end) win ties. Returns {year: rows in the partition after the merge}."""
    s = s or get_settings()
    now = now or datetime.now(UTC)
    c = get_contract(TABLE)
    cols = [x.name for x in c.columns if x.name not in c.partition_by]
    keys = sorted(staged, key=_window_end)
    con = _duck(s)
    out: dict[int, int] = {}
    try:
        cmap = _cusip_map(con, [staged[k] for k in keys])
        con.register("_cmap_df", cmap.to_arrow())
        con.execute("CREATE TEMP TABLE _cmap AS SELECT * FROM _cmap_df")
        parts = " UNION ALL ".join(
            f"SELECT *, {i + 1} AS _rank FROM read_parquet('{staged[k].as_posix()}')" for i, k in enumerate(keys))
        con.execute(f"CREATE TEMP VIEW _staged AS SELECT st.*, m.sid AS security_id, "
                    f"TIMESTAMPTZ '{now.isoformat()}' AS ingested_at FROM ({parts}) st LEFT JOIN _cmap m USING (cusip)")
        years = [int(r[0]) for r in con.execute("SELECT DISTINCT year(period_end) FROM _staged ORDER BY 1").fetchall()]
        sel = ", ".join(f'"{x}"' for x in cols)
        root = table_dir(c, s)
        for y in years:
            files = _existing_files(s, y)
            with table_lock(TABLE, s):
                files = _existing_files(s, y)  # re-read under the lock
                src = [f"SELECT {sel}, 0 AS _rank FROM _staged WHERE false"]
                if files:
                    fl = ", ".join(f"'{f.as_posix()}'" for f in files)
                    src = [f"SELECT {sel}, 0 AS _rank FROM read_parquet([{fl}], union_by_name = true)"]
                src.append(f"SELECT {sel}, _rank FROM _staged WHERE year(period_end) = {y}")
                outc = ", ".join("coalesce(u.security_id, m.sid) AS security_id" if x == "security_id"
                                 else f'u."{x}"' for x in cols)
                sql = f"""
                    SELECT {outc}
                    FROM (SELECT * FROM ({' UNION ALL '.join(src)})
                          QUALIFY row_number() OVER (PARTITION BY {', '.join(PK)}
                                                     ORDER BY knowledge_date DESC, filing_id DESC, _rank DESC) = 1) u
                    LEFT JOIN _cmap m ON m.cusip = u.cusip
                    ORDER BY u.filer_cik, u.period_end, u.cusip"""
                pdir = partition_dir(root, list(c.partition_by), [y])
                tmp = pdir.parent / f".tmp-{pdir.name}-{uuid.uuid4().hex[:8]}"
                tmp.mkdir(parents=True)
                f_out = tmp / f"part-{uuid.uuid4().hex[:12]}.parquet"
                con.execute(f"COPY ({sql}) TO '{f_out.as_posix()}' (FORMAT parquet, COMPRESSION zstd, "
                            f"COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 250000)")
                n = int(con.execute(f"SELECT count(*) FROM read_parquet('{f_out.as_posix()}')").fetchone()[0])
                _swap_in(tmp, pdir)
            out[y] = n
            log.info("13F backfill: merged period_end_year=%d rows=%d", y, n)
    finally:
        con.close()
    return out


# ------------------------------------------------------------------------------------------------ driver


def pick_windows(links: dict[str, str], params: dict[str, Any], done: set[str]) -> list[str]:
    """Explicit `datasets`, else every window whose end is >= `since` (default 2023-04-01) and not done."""
    if params.get("datasets"):
        return [k.lower() for k in params["datasets"]]
    since = date.fromisoformat(str(params.get("since") or "2023-04-01"))
    until = date.fromisoformat(str(params["until"])) if params.get("until") else date(2100, 1, 1)
    ks = [k for k in links if since <= _window_end(k) <= until and k not in done]
    return sorted(ks, key=_window_end)


def run(ctx):  # -> JobResult
    from app.core.http import HttpClient  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s: Settings = ctx.settings
    params = dict(ctx.params or {})
    st = load_state(s)
    http = HttpClient(SEC_SOURCE, rate_limit=SEC_RATE, settings=s)
    links = dataset_links(http.get(LANDING, cache_ttl=6 * 3600).text)
    weekly = s.state_dir / "sec_13f.json"
    wk = orjson.loads(weekly.read_bytes()) if weekly.exists() else {}
    done = set(wk.get("datasets_done") or []) | set(st.get("merged") or [])
    windows = pick_windows(links, params, done if not params.get("force") else set())
    ok, failed, fil_rows = [], {}, 0
    now = datetime.now(UTC)
    for k in windows:
        ctx.check_cancelled()
        if k in st["staged"] and Path(st["staged"][k]["path"]).exists():
            ok.append(k)
            continue
        if k not in links:
            failed[k] = "not on landing page"
            continue
        dest = s.raw_dir / SEC_SOURCE / "form13f" / f"{k}_form13f.zip"
        pre_existing = dest.exists()
        try:
            if not pre_existing:
                http.download(links[k], dest)
            out = _stage_dir(s) / f"{k}.parquet"
            n, fil = stage_zip(dest, out, s)
            fi = filings_index_rows(fil, s, now)
            fil_rows += write_table(fi, "filings_index", settings=s)
            st["staged"][k] = {"path": str(out), "rows": n, "staged_at": datetime.now(UTC).isoformat()}
            save_state(s, st)
            ok.append(k)
            ctx.log.info("13F backfill: staged %s rows=%d", k, n)
        except Exception as e:  # noqa: BLE001
            failed[k] = str(e)[:300]
            ctx.log.warning("13F backfill: %s failed: %s", k, e)
        finally:
            if not pre_existing and dest.exists() and not params.get("keep_zips"):
                dest.unlink()
    merged: dict[int, int] = {}
    to_merge = {k: Path(v["path"]) for k, v in st["staged"].items() if Path(v["path"]).exists()}
    if to_merge and not params.get("stage_only"):
        merged = merge_staged(to_merge, s, now)
        st["merged"] = sorted(set(st.get("merged") or []) | set(to_merge))
        st["staged"] = {}
        st["merged_years"] = {str(y): n for y, n in merged.items()}
        save_state(s, st)
        wk["datasets_done"] = sorted(set(wk.get("datasets_done") or []) | set(to_merge))
        weekly.write_bytes(orjson.dumps(wk, option=orjson.OPT_INDENT_2))
        for p in to_merge.values():
            p.unlink(missing_ok=True)
        shutil.rmtree(_stage_dir(s) / "duck_spill", ignore_errors=True)
    status = "success" if not failed else ("partial" if ok else "failed")
    return JobResult(status=status, rows_written={TABLE: sum(merged.values()), "filings_index": fil_rows},
                     entities_ok=len(ok), entities_failed=len(failed), http_requests=http.n_requests,
                     message=f"windows staged={ok} failed={failed} merged_years={merged}")
