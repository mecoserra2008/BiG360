"""A2 trade (job `linkage_trade`): IMF IMTS (Direction of Trade successor) + UN Comtrade -> trade_bilateral.

IMF IMTS  https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IMTS/{ISO3+...}.XG_FOB_USD+MG_CIF_USD..A
          (keyless SDMX 2.1, detail=dataonly; OBS_VALUE in USD). All Natural Earth countries as reporters, all
          partners, annual from 2000; batches of 10 reporters (~10 MB / call). Partner aggregates (G001, TX..., GX...)
          are dropped except W00 (world) -> partner 'W0'.
Comtrade  keyless preview https://comtradeapi.un.org/public/v1/preview/C/A/HS (<= 500 records, ONE period per call,
          ~1 call / 4 s or HTTP 429): per (reporter, year) bilateral totals (cmdCode TOTAL, flows M,X ~470 rows) and HS2
          chapters vs world (cmdCode AG2, partner 0). Reporters = countries we cover + major partners. Resumable
          (data/state/linkage_comtrade.json), newest years first, params max_calls per run. With COMTRADE_APIKEY the
          keyed /data/v1/get endpoint is used (same per-year tasks, higher quota). Keyless quota: HTTP 403 "Out of call
          volume quota ... replenished in HH:MM:SS" after ~250 calls -> the run stops (detached loop sleeps and resumes).
Codes: Comtrade M49 -> ISO2 from the official reference files (Reporters.json / partnerAreas.json; 490 'Other Asia,
nes' = Taiwan). knowledge_date: rule Dec 31 of year + 6 months (annual bilateral data is published with that lag;
knowledge_date_estimated = true).
"""

from __future__ import annotations

import io
import re
import time
from datetime import date
from typing import Any

import polars as pl

from app.core.config import Settings
from app.linkage.common import (iso3_to_iso2_map, load_state, log, now_utc, save_state, settings_of, update_progress,
                                write)

IMF_URL = "https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IMTS/{key}"
CT_PREVIEW = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
CT_KEYED = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
CT_REF = "https://comtradeapi.un.org/files/v1/app/reference/{f}.json"
IMF_INDICATORS = {"XG_FOB_USD": ("X", "FOB"), "MG_CIF_USD": ("M", "CIF")}
#: major partners always included as Comtrade reporters (besides the countries we cover)
MAJOR_PARTNERS = ["US", "CN", "DE", "JP", "GB", "FR", "KR", "IT", "NL", "CA", "MX", "IN", "BR", "RU", "SA", "AE", "TR",
                  "ZA", "AU", "ID", "VN", "TH", "MY", "SG", "PH", "CH", "ES", "PL", "BE", "SE", "AT", "IE", "TW", "HK",
                  "NO", "DK", "FI", "CL", "AR", "CO", "PE", "IL", "CZ", "HU", "PT", "GR", "RO", "NZ", "EG", "NG",
                  "KZ", "QA", "BD", "PK"]
TAX_HAVENS = {"BM", "KY", "JE", "GG", "IM", "VG"}
QUOTA_RX = re.compile(r"replenished in (\d+):(\d+):(\d+)")
SCHEMA = {"source": pl.Utf8, "reporter_iso2": pl.Utf8, "partner_iso2": pl.Utf8, "year": pl.Int16, "flow": pl.Utf8,
          "cmd": pl.Utf8, "value_usd": pl.Float64, "valuation": pl.Utf8}


def _finish(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    return df.with_columns(
        pl.date(pl.col("year").cast(pl.Int32) + 1, 6, 30).alias("knowledge_date"),
        pl.lit(True).alias("knowledge_date_estimated"), pl.lit(now_utc()).alias("ingested_at"),
    ).unique(["source", "reporter_iso2", "partner_iso2", "year", "flow", "cmd"], keep="last")


# ================================================================================================ IMF IMTS
def parse_imf_csv(content: bytes, iso3: dict[str, str]) -> pl.DataFrame:
    df = pl.read_csv(io.BytesIO(content), infer_schema=False, columns=None)
    need = {"COUNTRY", "INDICATOR", "COUNTERPART_COUNTRY", "TIME_PERIOD", "OBS_VALUE"}
    if df.is_empty() or not need <= set(df.columns):
        return pl.DataFrame(schema=SCHEMA)
    m = {**iso3, "W00": "W0"}
    df = df.filter(pl.col("INDICATOR").is_in(list(IMF_INDICATORS)) & pl.col("OBS_VALUE").is_not_null())
    out = df.select(
        pl.lit("imf_imts").alias("source"),
        pl.col("COUNTRY").replace_strict(m, default=None).alias("reporter_iso2"),
        pl.col("COUNTERPART_COUNTRY").replace_strict(m, default=None).alias("partner_iso2"),
        pl.col("TIME_PERIOD").str.slice(0, 4).cast(pl.Int16, strict=False).alias("year"),
        pl.col("INDICATOR").replace_strict({k: v[0] for k, v in IMF_INDICATORS.items()}).alias("flow"),
        pl.lit("TOTAL").alias("cmd"), pl.col("OBS_VALUE").cast(pl.Float64, strict=False).alias("value_usd"),
        pl.col("INDICATOR").replace_strict({k: v[1] for k, v in IMF_INDICATORS.items()}).alias("valuation"))
    return out.filter(pl.col("reporter_iso2").is_not_null() & pl.col("partner_iso2").is_not_null()
                      & pl.col("year").is_not_null() & pl.col("value_usd").is_not_null()
                      & (pl.col("reporter_iso2") != pl.col("partner_iso2")))


def fetch_imf(settings: Settings | None = None, start: int = 2000, batch: int = 10) -> pl.DataFrame:
    from app.core.storage import read_table  # noqa: PLC0415
    from app.linkage.common import http  # noqa: PLC0415

    s = settings_of(settings)
    iso3 = iso3_to_iso2_map(s)
    geo = read_table("geo_countries", columns=["iso2", "iso3"], settings=s)
    reporters = sorted({a for a in geo["iso3"].drop_nulls().to_list()} | {"KOS", "TWN"})
    parts, failed = [], []
    with http("imf", s) as cl:
        for i in range(0, len(reporters), batch):
            key = "+".join(reporters[i:i + batch]) + "." + "+".join(IMF_INDICATORS) + "..A"
            try:
                r = cl.get(IMF_URL.format(key=key), params={"startPeriod": str(start), "detail": "dataonly"},
                           headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}, cache_ttl=20 * 3600)
                parts.append(parse_imf_csv(r.content, iso3))
            except Exception as e:  # noqa: BLE001 - one batch failing must not stop the run
                failed.append(f"{reporters[i]}..: {type(e).__name__}: {e}"[:200])
    if failed:
        log.warning("imf imts failed batches: %s", failed)
    df = pl.concat(parts, how="vertical_relaxed") if parts else pl.DataFrame(schema=SCHEMA)
    return _finish(df)


# ================================================================================================ Comtrade
def comtrade_codes(cl: Any) -> tuple[dict[int, str], dict[int, str]]:
    rep = cl.get(CT_REF.format(f="Reporters"), cache_ttl=30 * 86400).json()
    par = cl.get(CT_REF.format(f="partnerAreas"), cache_ttl=30 * 86400).json()
    rmap = {int(x["reporterCode"]): x.get("reporterCodeIsoAlpha2") for x in rep.get("results", [])
            if x.get("reporterCodeIsoAlpha2") and not x.get("isGroup")}
    pmap = {int(x["PartnerCode"]): x.get("PartnerCodeIsoAlpha2") for x in par.get("results", [])
            if x.get("PartnerCodeIsoAlpha2") and not x.get("isGroup")}
    rmap[490] = pmap[490] = "TW"
    pmap[0] = "W0"
    return rmap, pmap


def parse_comtrade(j: dict[str, Any], rmap: dict[int, str], pmap: dict[int, str]) -> pl.DataFrame:
    rows = []
    for d in j.get("data") or []:
        if (d.get("partner2Code") not in (0, None) or str(d.get("motCode", 0)) != "0"
                or str(d.get("mosCode", "0")) != "0" or d.get("customsCode") not in ("C00", None)):
            continue
        rep, par = rmap.get(int(d["reporterCode"])), pmap.get(int(d["partnerCode"]))
        flow = d.get("flowCode")
        val = d.get("primaryValue")
        if not rep or not par or flow not in ("X", "M") or val is None or rep == par:
            continue
        rows.append({"source": "comtrade", "reporter_iso2": rep, "partner_iso2": par, "year": int(d["refYear"]),
                     "flow": flow, "cmd": str(d.get("cmdCode")), "value_usd": float(val),
                     "valuation": "FOB" if flow == "X" else "CIF"})
    return pl.DataFrame(rows, schema=SCHEMA)


def comtrade_reporters(settings: Settings | None = None) -> list[str]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        cov = set(read_table("issuers", columns=["country"], settings=settings)["country"].drop_nulls().to_list())
    except Exception:  # noqa: BLE001
        cov = set()
    return sorted((cov | set(MAJOR_PARTNERS)) - TAX_HAVENS)


def comtrade_tasks(reporters: list[str], start: int, end: int, hs2: bool = True) -> list[str]:
    """'ISO2|year|kind' newest year first (kind: bil = bilateral totals, hs2 = HS chapters vs world)."""
    kinds = ["bil", "hs2"] if hs2 else ["bil"]
    return [f"{r}|{y}|{k}" for y in range(end, start - 1, -1) for k in kinds for r in reporters]


def fetch_comtrade(settings: Settings | None = None, start: int = 2000, end: int | None = None, max_calls: int = 400,
                   hs2: bool = True, refresh_years: int = 2) -> tuple[pl.DataFrame, dict[str, Any]]:
    from app.core.http import HttpError  # noqa: PLC0415
    from app.linkage.common import http  # noqa: PLC0415

    s = settings_of(settings)
    key = s.comtrade_apikey.get_secret_value() if getattr(s, "comtrade_apikey", None) else None
    end = end or date.today().year - 1
    st = load_state("comtrade", s)
    done: dict[str, Any] = st.setdefault("done", {})
    # recent years are re-fetched (late reporters / revisions)
    stale = {t for t, v in done.items() if int(t.split("|")[1]) >= end - refresh_years + 1
             and v.get("at", "") < (now_utc().date().replace(day=1)).isoformat()}
    reporters = comtrade_reporters(s)
    with http("comtrade", s) as cl:
        rmap, pmap = comtrade_codes(cl)
        inv = {v: k for k, v in rmap.items()}
        todo = [t for t in comtrade_tasks(reporters, start, end, hs2) if (t not in done or t in stale)
                and t.split("|")[0] in inv]
        parts, calls, fails, consecutive, quota_wait = [], 0, {}, 0, 0
        for t in todo:
            if calls >= max_calls or consecutive >= 5:
                break
            iso, year, kind = t.split("|")
            params: dict[str, Any] = {"reporterCode": inv[iso], "period": year, "flowCode": "M,X",
                                      "cmdCode": "TOTAL" if kind == "bil" else "AG2"}
            if kind == "hs2":
                params["partnerCode"] = "0"
            params.update({"partner2Code": "0", "motCode": "0", "customsCode": "C00"} if key else {})
            calls += 1
            try:
                if key:
                    r = cl.get(CT_KEYED, params=params, headers={"Ocp-Apim-Subscription-Key": key}, cache_ttl=0)
                else:
                    r = cl.get(CT_PREVIEW, params=params, cache_ttl=0)
                j = r.json()
            except HttpError as e:
                m = QUOTA_RX.search(str(e))
                if e.status == 403 and m:  # rolling call-volume quota (~250 calls / hour keyless): stop this run
                    h, mi, se = (int(x) for x in m.groups())
                    quota_wait = h * 3600 + mi * 60 + se
                    calls -= 1
                    break
                fails[t] = f"HTTP {e.status}"
                consecutive += 1
                continue
            except Exception as e:  # noqa: BLE001
                fails[t] = f"{type(e).__name__}"
                consecutive += 1
                continue
            consecutive = 0
            df = parse_comtrade(j, rmap, pmap)
            parts.append(df)
            done[t] = {"rows": df.height, "count": j.get("count"), "at": now_utc().date().isoformat()}
            if calls % 25 == 0:  # checkpoint (rows are written by the caller at the end; state only after write)
                pass
    out = _finish(pl.concat(parts, how="vertical_relaxed")) if parts else pl.DataFrame()
    rep = {"calls": calls, "failed": len(fails), "remaining": max(0, len(todo) - calls), "reporters": len(reporters),
           "keyed": bool(key), "quota_wait_s": quota_wait}
    return out, {"state": st, **rep, "fail_sample": dict(list(fails.items())[:5])}


def run_trade(settings: Settings | None = None, imf: bool = True, comtrade: bool = True, max_calls: int = 400,
              start: int = 2000, hs2: bool = True) -> dict[str, Any]:
    s = settings_of(settings)
    rep: dict[str, Any] = {}
    if imf:
        df = fetch_imf(s, start=start)
        rep["imf_rows"] = write(df, "trade_bilateral", s)
        rep["imf_reporters"] = df.get_column("reporter_iso2").n_unique() if df.height else 0
        rep["imf_years"] = [int(df["year"].min()), int(df["year"].max())] if df.height else None
    if comtrade:
        df, crep = fetch_comtrade(s, start=start, max_calls=max_calls, hs2=hs2)
        st = crep.pop("state")
        rep["comtrade_rows"] = write(df, "trade_bilateral", s) if df.height else 0
        save_state("comtrade", st, s)  # only after the rows are safely written
        rep["comtrade"] = crep
    log.info("linkage trade: %s", rep)
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    b = lambda k, d: str(p.get(k, d)).lower() in ("1", "true", "yes")  # noqa: E731
    rep = run_trade(ctx.settings, imf=b("imf", True), comtrade=b("comtrade", True),
                    max_calls=int(p.get("max_calls", 400)), start=int(p.get("start", 2000)), hs2=b("hs2", True))
    rem = (rep.get("comtrade") or {}).get("remaining", 0)
    update_progress("A2", "done" if not rem else "partial", settings=ctx.settings, **rep)
    return JobResult(status="success", rows_written={"trade_bilateral": rep.get("imf_rows", 0) + rep.get("comtrade_rows", 0)},
                     message=str(rep))


if __name__ == "__main__":  # detached Comtrade backfill: python -m app.linkage.trade comtrade [max_calls]
    import sys

    from app.core.logsetup import setup_logging  # noqa: PLC0415

    setup_logging()
    a = sys.argv[1:]
    what = a[0] if a else "all"
    mc = int(a[1]) if len(a) > 1 else 400
    chunk = 100
    if what == "comtrade":  # loop in chunks so progress is written every ~7 minutes
        total = 0
        while total < mc:
            r = run_trade(imf=False, comtrade=True, max_calls=min(chunk, mc - total))
            c = r["comtrade"]
            total += c["calls"]
            print(r, flush=True)
            if c["remaining"] == 0 or c["failed"] >= 5:
                break
            if c["quota_wait_s"]:  # keyless quota exhausted: sleep until it is replenished, then resume
                time.sleep(c["quota_wait_s"] + 30)
    else:
        print(run_trade(imf=what in ("all", "imf"), comtrade=what == "all", max_calls=mc))
