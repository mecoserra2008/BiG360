"""B3 COTAHIST (official historical quotes, free) -> prices_staging. Manual job `prices_br_b3` (fallback for BR).

Files (research §8.1): daily COTAHIST_DDDMMYYYY.ZIP (~0.5 MB) and annual COTAHIST_AYYYY.ZIP (~90 MB) at
https://bvmf.bmfbovespa.com.br/InstDados/SerHist/ . Fixed-width 245-char records (type 01 = quote); prices are
in BRL with 2 implied decimals divided by FATCOT (quote factor). Prices are UNADJUSTED. Only the cash market
(TPMERC=010) standard lot (CODBDI 02) and real-estate funds/units (12) are kept. Staged symbol = CODNEG + ".SA".
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient
from app.core.logsetup import get_logger
from app.core.storage import write_table

log = get_logger(__name__)
BASE = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist"

# (name, start, end) 1-based inclusive positions from the B3 layout
_LAYOUT = [("TIPREG", 1, 2), ("DATA", 3, 10), ("CODBDI", 11, 12), ("CODNEG", 13, 24), ("TPMERC", 25, 27),
           ("NOMRES", 28, 39), ("ESPECI", 40, 49), ("MODREF", 53, 56), ("PREABE", 57, 69), ("PREMAX", 70, 82),
           ("PREMIN", 83, 95), ("PREMED", 96, 108), ("PREULT", 109, 121), ("TOTNEG", 148, 152),
           ("QUATOT", 153, 170), ("VOLTOT", 171, 188), ("FATCOT", 211, 217), ("CODISI", 231, 242)]


def parse_cotahist(text: str, codbdi: tuple[str, ...] = ("02", "12")) -> pl.DataFrame:
    lines = pl.DataFrame({"l": text.splitlines()}).filter(pl.col("l").str.starts_with("01"))
    if lines.is_empty():
        return pl.DataFrame()
    df = lines.select([pl.col("l").str.slice(a - 1, b - a + 1).str.strip_chars().alias(n) for n, a, b in _LAYOUT])
    df = df.filter((pl.col("TPMERC") == "010") & pl.col("CODBDI").is_in(list(codbdi)))
    fat = pl.col("FATCOT").cast(pl.Float64, strict=False).fill_null(1.0)
    px = lambda c: (pl.col(c).cast(pl.Float64, strict=False) / 100.0 / fat)  # noqa: E731
    now = datetime.now(UTC)
    out = df.select(
        pl.lit("b3_cotahist").alias("source_id"), (pl.col("CODNEG") + ".SA").alias("symbol"),
        pl.col("DATA").str.to_date("%Y%m%d").alias("date"),
        px("PREABE").alias("open"), px("PREMAX").alias("high"), px("PREMIN").alias("low"), px("PREULT").alias("close"),
        pl.lit(None, pl.Float64).alias("adj_close"), pl.col("QUATOT").cast(pl.Float64, strict=False).alias("volume"),
        px("PREMED").alias("vwap"), pl.col("TOTNEG").cast(pl.Int64, strict=False).alias("trades"),
        pl.lit("BRL").alias("currency"), pl.lit("raw").alias("price_basis"), pl.lit(False).alias("is_benchmark"),
        pl.lit(now).alias("ingested_at"),
    ).with_columns(pl.col("date").dt.year().cast(pl.Int16).alias("year"))
    return out.filter(pl.col("close") > 0).unique(subset=["symbol", "date"], keep="last")


def run_b3(settings: Settings | None = None, day: date | None = None, year: int | None = None,
           write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    http = HttpClient("b3", settings=s, timeout=300)
    if year:
        names = [f"COTAHIST_A{year}.ZIP"]
    else:
        d = day or datetime.now(UTC).date() - timedelta(days=1)
        names = [f"COTAHIST_D{d.strftime('%d%m%Y')}.ZIP"]
    frames, failed = [], {}
    for n in names:
        try:
            r = http.get(f"{BASE}/{n}", cache_ttl=-1)
            raw_dir = s.raw_dir / "b3" / "cotahist"
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / n).write_bytes(r.content)
            z = zipfile.ZipFile(io.BytesIO(r.content))
            frames.append(parse_cotahist(z.read(z.namelist()[0]).decode("latin-1")))
        except Exception as e:  # noqa: BLE001
            failed[n] = f"{type(e).__name__}: {e}"[:200]
    df = pl.concat([f for f in frames if f.height]) if any(f.height for f in frames) else pl.DataFrame()
    rep: dict[str, Any] = {"status": "failed" if failed and not df.height else ("partial" if failed else "success"),
                           "files": names, "rows": df.height, "failed": failed}
    if write and df.height:
        rep["rows_written"] = write_table(df, "prices_staging", settings=s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    if not p.get("day") and not p.get("year"):  # nightly: the last few weekdays (holidays have no file -> skipped)
        today = datetime.now(UTC).date()
        days = [today - timedelta(days=k) for k in range(1, int(p.get("lookback_days", 5)) + 1)]
        reps = [run_b3(ctx.settings, d) for d in days if d.weekday() < 5]
        ok = [r for r in reps if r["status"] != "failed"]
        rows = sum(r.get("rows_written", 0) for r in ok)
        return JobResult(status="success" if ok or not reps else "failed", rows_written={"prices_staging": rows},
                         message=f"days ok={len(ok)}/{len(reps)} rows={rows}")
    rep = run_b3(ctx.settings, date.fromisoformat(p["day"]) if p.get("day") else None, p.get("year"))
    return JobResult(status=rep["status"], rows_written={"prices_staging": rep.get("rows_written", 0)},
                     entities_failed=len(rep["failed"]), message=f"files={rep['files']} rows={rep['rows']}")
