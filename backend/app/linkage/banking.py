"""A4 finance (job `linkage_bank`): BIS international banking statistics -> bank_claims (bilateral, quarterly).

Official BIS SDMX v2 API (keyless, stats.bis.org), one CSV request per series family (~30 MB from 2000):
  lbs_claims       WS_LBS_D_PUB  Q.S.C.A.TO1.A.5J.A.{rep}.A.{cp}.N   cross-border claims of banks located in rep on cp
                                                                    (all instruments, currencies, sectors, parents)
  lbs_liabilities  WS_LBS_D_PUB  Q.S.L.A.TO1.A.5J.A.{rep}.A.{cp}.N   cross-border liabilities of banks in rep to cp
  cbs_immediate    WS_CBS_PUB    Q.S.{rep}.4R.F.C.A.A.TO1.A.{cp}     consolidated claims of rep's domestic banks on cp,
                                                                    immediate counterparty basis
  cbs_ultimate     WS_CBS_PUB    Q.S.{rep}.4R.U.C.A.A.TO1.A.{cp}     same, guarantor (ultimate risk) basis
Counterparty / reporter aggregates (codes with digits: 5J, 1C, ...; 'PU' unallocated) are dropped. Values USD
millions (UNIT_MULT 6). knowledge_date: quarter end + 4 months (BIS publishes ~4 months after the quarter; estimated).
"""

from __future__ import annotations

import io
from typing import Any

import polars as pl

from app.core.config import Settings
from app.linkage.common import log, now_utc, settings_of, update_progress, write

BASE = "https://stats.bis.org/api/v2/data/dataflow/BIS/{flow}/1.0/{key}"
SERIES = {
    "lbs_claims": ("WS_LBS_D_PUB", "Q.S.C.A.TO1.A.5J.A..A..N", "L_REP_CTY", "L_CP_COUNTRY"),
    "lbs_liabilities": ("WS_LBS_D_PUB", "Q.S.L.A.TO1.A.5J.A..A..N", "L_REP_CTY", "L_CP_COUNTRY"),
    "cbs_immediate": ("WS_CBS_PUB", "Q.S..4R.F.C.A.A.TO1.A.", "L_REP_CTY", "L_CP_COUNTRY"),
    "cbs_ultimate": ("WS_CBS_PUB", "Q.S..4R.U.C.A.A.TO1.A.", "L_REP_CTY", "L_CP_COUNTRY"),
}


def parse_bis_csv(content: bytes, dataset: str) -> pl.DataFrame:
    _, _, rc, cc = SERIES[dataset]
    df = pl.read_csv(io.BytesIO(content), infer_schema=False)
    if df.is_empty() or not {rc, cc, "TIME_PERIOD", "OBS_VALUE"} <= set(df.columns):
        return pl.DataFrame()
    mult = pl.col("UNIT_MULT").cast(pl.Int32, strict=False).fill_null(6) if "UNIT_MULT" in df.columns else pl.lit(6)
    iso = r"^[A-Z]{2}$"
    out = df.filter(pl.col(rc).str.contains(iso) & pl.col(cc).str.contains(iso) & (pl.col(cc) != "PU")
                    & (pl.col(rc) != pl.col(cc)) & pl.col("OBS_VALUE").is_not_null() & (pl.col("OBS_VALUE") != "NaN"))
    y = pl.col("TIME_PERIOD").str.slice(0, 4).cast(pl.Int32)
    q = pl.col("TIME_PERIOD").str.slice(-1, 1).cast(pl.Int32)
    out = out.select(
        pl.lit(dataset).alias("dataset"), pl.col(rc).alias("reporter_iso2"), pl.col(cc).alias("counterparty_iso2"),
        pl.date(y + (q == 4).cast(pl.Int32), (q * 3) % 12 + 1, 1).dt.offset_by("-1d").alias("period_end"),
        pl.col("TIME_PERIOD").alias("period"),
        (pl.col("OBS_VALUE").cast(pl.Float64, strict=False) * (10.0 ** (mult - 6))).alias("value_usd_m"),
        (pl.col("OBS_STATUS") if "OBS_STATUS" in df.columns else pl.lit(None, pl.Utf8)).alias("obs_status"))
    return out.filter(pl.col("value_usd_m").is_not_null()).with_columns(
        pl.col("period_end").dt.offset_by("4mo").alias("knowledge_date"), pl.lit(True).alias("knowledge_date_estimated"),
        pl.lit(now_utc()).alias("ingested_at"))


def windows(start: int, end: int, size: int = 8) -> list[tuple[int, int]]:
    return [(a, min(end, a + size - 1)) for a in range(start, end + 1, size)]


def run_bank(settings: Settings | None = None, start: str = "2000", datasets: list[str] | None = None) -> dict[str, Any]:
    from app.linkage.common import http  # noqa: PLC0415

    s = settings_of(settings)
    rep: dict[str, Any] = {}
    with http("bis", s, timeout=600) as cl:
        for ds in datasets or list(SERIES):
            flow, key, _, _ = SERIES[ds]
            try:  # explicit start/end windows: open-ended full-history responses came back incomplete (2026-10-12)
                parts = []
                for a, b in windows(int(start), now_utc().year):
                    r = cl.get(BASE.format(flow=flow, key=key),
                               params={"startPeriod": str(a), "endPeriod": str(b), "format": "csv"}, cache_ttl=20 * 3600)
                    parts.append(parse_bis_csv(r.content, ds))
                df = pl.concat([x for x in parts if not x.is_empty()], how="vertical_relaxed").unique(
                    ["dataset", "reporter_iso2", "counterparty_iso2", "period"], keep="last")
            except Exception as e:  # noqa: BLE001
                rep[ds] = f"failed: {type(e).__name__}: {e}"[:200]
                continue
            n = write(df, "bank_claims", s)
            rep[ds] = {"rows": n, "reporters": df["reporter_iso2"].n_unique() if n else 0,
                       "counterparties": df["counterparty_iso2"].n_unique() if n else 0,
                       "last": df["period"].max() if n else None}
    log.info("linkage bank: %s", rep)
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_bank(ctx.settings, start=str(p.get("start", "2000")))
    ok = all(isinstance(v, dict) for v in rep.values())
    update_progress("A4", "done" if ok else "partial", settings=ctx.settings, **rep)
    rows = sum(v["rows"] for v in rep.values() if isinstance(v, dict))
    return JobResult(status="success" if ok else "partial", rows_written={"bank_claims": rows}, message=str(rep))
