"""W3.5 Balance sheets & liquidity (job cb_balance_sheets), official sources only.

  fed  H.4.1 via FRED (weekly, Wednesday levels; millions USD): WALCL total assets, TREAST Treasuries, WSHOMCB MBS,
       WRESBAL reserve balances, WTREGEN Treasury General Account; RRPONTSYD ON RRP (daily, billions)
  ecb  Eurosystem weekly financial statement (ECB Data Portal ILM, millions EUR): total assets, securities held for
       monetary-policy purposes, lending to credit institutions (MPOs), deposit facility, banknotes
  boe  Bank of England weekly (IADB, millions GBP): reserve balances RPWB56A, notes in circulation RPWB55A, short-term
       repo RPWB67A, indexed long-term repo RPWZ4TJ (APF gilt stock is not in a weekly machine-readable series)
       FROZEN 2026-11-07 (A9): the IADB is robots-disallowed -> stored rows kept, no request (source_disallowed)
  boj  Bank of Japan Accounts (BoJ stat-search BS01, month end, 100 million JPY): total assets MABJMTA, JGBs MABJMA5
Values stored in billions of local currency. QT pace (method ``bs_v1``): change vs ~13 weeks (91 days) earlier,
annualised (x 365/91), and change vs ~1 year earlier, per component.
"""

from __future__ import annotations

import csv
import io
import json
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from .common import http_get, now_utc, to_float, update_state, write

log = get_logger(__name__)
METHOD = "bs_v1"
START = date(2003, 1, 1)

FED = {"WALCL": ("total_assets", 1e-3), "TREAST": ("treasuries", 1e-3), "WSHOMCB": ("mbs", 1e-3),
       "WRESBAL": ("reserves", 1e-3), "WTREGEN": ("tga", 1e-3), "RRPONTSYD": ("on_rrp", 1.0)}
ECB = {"T000000.Z5.Z01": "total_assets", "A070100.U2.EUR": "securities_mp", "A050000.U2.EUR": "lending_mpo",
       "L020200.U2.EUR": "deposit_facility", "L010000.Z5.EUR": "banknotes"}
BOE = {"RPWB56A": "reserves", "RPWB55A": "banknotes", "RPWB67A": "short_term_repo", "RPWZ4TJ": "indexed_long_term_repo"}
BOJ = {"MABJMTA": "total_assets", "MABJMA5": "jgbs"}
CCY = {"fed": "USD", "ecb": "EUR", "boe": "GBP", "boj": "JPY"}
SOURCE = {"fed": "FRED (Federal Reserve H.4.1)", "ecb": "ECB weekly financial statement (ILM)",
          "boe": "Bank of England weekly (IADB)", "boj": "Bank of Japan Accounts (stat-search BS01)"}

Row = tuple[str, date, str, float, str]
#: A9 2026-11-07 (docs/it/ROBOTS_COMPLIANCE.md): www.bankofengland.co.uk robots.txt disallows the IADB
#: (/boeapps/database/_iadb-FromShowColumns.asp); no other allowed machine-readable weekly BoE balance-sheet source
BOE_DISALLOWED = ("source_disallowed: BoE weekly balance sheet is IADB-only (robots.txt disallows the IADB, checked "
                  "2026-11-07); history kept, not updated")


class FrozenReadError(RuntimeError):
    """The frozen BoE rows exist (snapshot or table files) but could not be read: never overwrite them away."""


def boe_snapshot_path(s: Settings) -> Path:
    """Raw snapshot of the frozen BoE IADB rows, independent of the nightly-overwritten cb_balance_sheets table."""
    return Path(s.raw_dir) / "macro" / "cb" / "boe_iadb_frozen.parquet"


def _boe_rows(df: pl.DataFrame) -> list[Row]:
    if df is None or df.is_empty() or "bank" not in df.columns:
        return []
    df = df.filter(pl.col("bank") == "boe")
    src = pl.col("source") if "source" in df.columns else pl.lit("boe:IADB")
    return [(b, d, c, float(v), str(x or "boe:IADB")) for b, d, c, v, x in
            df.select("bank", "date", "component", "value_bn", src.alias("source")).drop_nulls("value_bn").iter_rows()]


def boe_frozen_rows(s: Settings) -> list[Row]:
    """BoE rows ingested from the IADB before 2026-11-07, unchanged. Read from the raw snapshot
    (data/raw/macro/cb/boe_iadb_frozen.parquet); the first run after the switch creates that snapshot from the stored
    cb_balance_sheets rows, so a later failed read or a rebuilt table can no longer drop them. [] only when neither the
    snapshot nor any table file exists (fresh install: nothing to keep). Raises FrozenReadError when files exist but
    cannot be read (the caller must then not overwrite the table)."""
    import os  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from app.core.storage import read_table, table_dir  # noqa: PLC0415

    snap = boe_snapshot_path(s)
    if snap.exists():
        try:
            rows = _boe_rows(pl.read_parquet(snap))
        except Exception as e:  # noqa: BLE001
            raise FrozenReadError(f"snapshot {snap.name} unreadable: {type(e).__name__}: {e}") from e
        if rows:
            return rows
    root = table_dir("cb_balance_sheets", s)
    if not (root.exists() and any(f for f in root.rglob("*.parquet")
                                  if not any(x.startswith((".tmp-", ".old-")) for x in f.parts))):
        return []
    try:
        df = read_table("cb_balance_sheets", settings=s, filters=pl.col("bank") == "boe")
    except Exception as e:  # noqa: BLE001
        raise FrozenReadError(f"cb_balance_sheets unreadable: {type(e).__name__}: {e}") from e
    rows = _boe_rows(df)
    if rows:
        try:   # one-off snapshot (atomic replace), the source of the frozen rows from now on
            snap.parent.mkdir(parents=True, exist_ok=True)
            tmp = snap.with_name(f".tmp-{uuid.uuid4().hex[:8]}-{snap.name}")
            pl.DataFrame(rows, schema={"bank": pl.Utf8, "date": pl.Date, "component": pl.Utf8, "value_bn": pl.Float64,
                                       "source": pl.Utf8}, orient="row").write_parquet(tmp)
            os.replace(tmp, snap)
            log.info("cb_balance_sheets: frozen BoE IADB rows snapshotted to %s (%d rows)", snap, len(rows))
        except Exception as e:  # noqa: BLE001 - the table still holds them; retried next run
            log.warning("cb_balance_sheets: BoE snapshot not written: %s", e)
    return rows


def parse_ilm(text: str) -> list[tuple[str, date, float]]:
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        key = r.get("KEY", "")
        item = ".".join(key.split(".")[4:])
        v = to_float(r.get("OBS_VALUE"))
        tp = r.get("TIME_PERIOD", "")
        try:
            y, w = tp.split("-W")
            d = date.fromisocalendar(int(y), int(w), 5)  # ISO week -> Friday (statement reference week end)
        except ValueError:
            continue
        if item in ECB and v is not None:
            out.append((ECB[item], d, v / 1000.0))
    return out


def parse_iadb(text: str) -> list[tuple[str, date, float]]:
    from datetime import datetime  # noqa: PLC0415

    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 3 or r[0] == "DATE":
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d %b %Y").date()
        except ValueError:
            continue
        v = to_float(r[2])
        if r[1] in BOE and v is not None:
            out.append((BOE[r[1]], d, v / 1000.0))
    return out


def parse_boj_json(j: dict[str, Any]) -> list[tuple[str, date, float]]:
    out = []
    for rs in j.get("RESULTSET", []) or []:
        comp = BOJ.get(rs.get("SERIES_CODE", ""))
        vals = rs.get("VALUES", {}) or {}
        for d, v in zip(vals.get("SURVEY_DATES", []), vals.get("VALUES", []), strict=False):
            fv = to_float(v)
            if comp and fv is not None:
                s = str(d)
                out.append((comp, date(int(s[:4]), int(s[4:6]), 1), fv / 10.0))  # 100m JPY -> bn JPY
    return out


def add_pace(df: pl.DataFrame) -> pl.DataFrame:
    """change_13w (bn), pace_annualized (bn/yr) and change_1y (bn) per (bank, component) via as-of lookups."""
    df = df.sort("bank", "component", "date")
    base = df.select("bank", "component", "date", "value_bn")
    out = df
    for col, days, tol in (("_v13", 91, "20d"), ("_v52", 365, "40d")):
        k = out.with_columns((pl.col("date") - pl.duration(days=days)).alias("_k")).sort("_k")
        prev = base.rename({"date": "_k", "value_bn": col}).sort("_k")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # sortedness within 'by' groups is guaranteed by the sorts
            out = k.join_asof(prev, on="_k", by=["bank", "component"], strategy="backward", tolerance=tol).drop("_k")
    return (out.with_columns((pl.col("value_bn") - pl.col("_v13")).alias("change_13w_bn"),
                             ((pl.col("value_bn") - pl.col("_v13")) * 365 / 91).alias("pace_annualized_bn"),
                             (pl.col("value_bn") - pl.col("_v52")).alias("change_1y_bn"))
            .drop("_v13", "_v52").sort("bank", "component", "date"))


def build_balance_sheets(s: Settings | None = None, write_out: bool = True) -> dict[str, Any]:
    from .rates import fetch_fred  # noqa: PLC0415

    s = s or get_settings()
    rep: dict[str, Any] = {}
    rows: list[Row] = []
    try:
        for sid, pairs in fetch_fred(FED, s, START).items():
            comp, mult = FED[sid]
            rows += [("fed", d, comp, v * mult, f"fred:{sid}") for d, v in pairs]
        rep["fed"] = "ok"
    except Exception as e:  # noqa: BLE001
        rep["fed"] = f"failed: {e}"
    try:
        raw = http_get("ecb", "https://data-api.ecb.europa.eu/service/data/ILM/W.U2.C." +
                       "+".join(sorted({k.split('.')[0] for k in ECB})) + "." + "+".join(sorted({k.split('.')[1] for k in ECB}))
                       + "." + "+".join(sorted({k.split('.')[2] for k in ECB})),
                       {"startPeriod": START.isoformat(), "format": "csvdata"}, ttl=12 * 3600, settings=s)
        rows += [("ecb", d, c, v, "ecb:ILM") for c, d, v in parse_ilm(raw.decode("utf-8", errors="replace"))]
        rep["ecb"] = "ok"
    except Exception as e:  # noqa: BLE001
        rep["ecb"] = f"failed: {e}"
    # A9 2026-11-07: the BoE weekly series live only in the IADB, which robots.txt disallows -> no request; the rows
    # stored before the switch are kept (frozen, source boe:IADB) and the bank is reported as source_disallowed
    mode = None
    try:
        kept = boe_frozen_rows(s)
        rep["boe"] = f"{BOE_DISALLOWED} (kept {len(kept)} stored rows, not updated)"
    except FrozenReadError as e:
        # a read error is not "no rows": upsert instead of overwrite, so the stored BoE rows stay in the table (they
        # cannot be re-fetched); if the table itself cannot be read the upsert raises and nothing is written
        kept, mode = [], "upsert"
        rep["boe"] = f"failed: frozen BoE rows unreadable ({e}); upsert keeps the stored BoE rows"
        log.warning("cb_balance_sheets: %s", rep["boe"])
    rows += kept
    try:
        raw = http_get("boj", "https://www.stat-search.boj.or.jp/api/v1/getDataCode",
                       {"format": "json", "lang": "en", "db": "BS01", "code": ",".join(BOJ), "startDate": "200301"},
                       ttl=12 * 3600, settings=s)
        rows += [("boj", d, c, v, "boj:BS01") for c, d, v in parse_boj_json(json.loads(raw))]
        rep["boj"] = "ok"
    except Exception as e:  # noqa: BLE001
        rep["boj"] = f"failed: {e}"
    df = pl.DataFrame(rows, schema={"bank": pl.Utf8, "date": pl.Date, "component": pl.Utf8, "value_bn": pl.Float64,
                                    "source": pl.Utf8}, orient="row").unique(["bank", "component", "date"], keep="last")
    df = add_pace(df).with_columns(pl.col("bank").replace_strict(CCY, default=None).alias("currency"),
                                   pl.lit(METHOD).alias("method_version"), pl.lit(now_utc()).alias("computed_at"))
    rep["rows"] = df.height
    rep["latest"] = {f"{r['bank']}.{r['component']}": (str(r["date"]), round(r["value_bn"], 1),
                                                       None if r["pace_annualized_bn"] is None
                                                       else round(r["pace_annualized_bn"], 1))
                     for r in df.group_by("bank", "component").last().to_dicts()}
    if write_out:
        rep["written"] = write(df, "cb_balance_sheets", s, mode=mode)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = build_balance_sheets(ctx.settings)
    bad = [k for k in ("fed", "ecb", "boe", "boj") if str(rep.get(k, "")).startswith("failed")]
    update_state({"items": {"W3.5": "done"}, "last_runs": {"cb_balance_sheets": {
        "at": now_utc().isoformat(timespec="seconds"), "rows": rep["rows"], "failed": bad}}}, ctx.settings)
    return JobResult(status="partial" if bad else "success", rows_written={"cb_balance_sheets": rep.get("written", 0)},
                     entities_ok=4 - len(bad), entities_failed=len(bad), message=json.dumps(rep, default=str)[:2000])


_ = timedelta
