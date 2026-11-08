"""A3 input-output (job `linkage_io`): OECD Inter-Country Input-Output tables -> io_flows (our sector taxonomy).

Source: OECD ICIO, keyless zip of yearly CSVs (USD millions; rows / columns 'ISO3_INDUSTRY', final-demand columns
'ISO3_HFCE|NPISH|GGFC|GFCF|INVNT|DPABR', rows TLS / VA / OUT).
  default  2023 edition, 2016-2020, "SML" (CN / MX not split): stats.oecd.org/wbos/fileview2.aspx?IDFile=d1ab2315-...
           (~85 MB; verified 2026-10-12). The 2025 edition (to 2022) is only linked from www.oecd.org, which answers
           403 to scripted clients: drop its zip (e.g. 2016-2022_SML.zip) into data/raw/linkage/icio/ (or pass
           params url=...) and the newest edition / years found there are used (edition from the file name, else
           params edition).
The 45 ICIO industries (ISIC rev.4 groups) are mapped to the 11 sectors of contracts/sector_taxonomy.yaml
(ICIO_TO_SECTOR; O / P / T -> NONMKT). Countries -> ISO2 ('ROW' kept as 'XR' = rest of world; CN1/CN2, MX1/MX2
merged). One row per (edition, year, src country x sector, dst country x sector) of intermediate use, plus
dst_sector 'FD' = final demand of the destination country.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.linkage.common import iso3_to_iso2_map, log, now_utc, raw_dir, settings_of, update_progress, write

ICIO_2023_URL = "https://stats.oecd.org/wbos/fileview2.aspx?IDFile=d1ab2315-298c-4e93-9a81-c6f2273139fe"
ICIO_2023_FILE = "ICIO2023_2016-2020_SML.zip"
FD_CODES = {"HFCE", "NPISH", "GGFC", "GFCF", "INVNT", "DPABR", "NONRES", "FD"}
ROW = "XR"
ICIO_TO_SECTOR = {
    "A01_02": "CSTA", "A01": "CSTA", "A02": "MATR", "A03": "CSTA", "B05_06": "ENRG", "B05": "ENRG", "B06": "ENRG",
    "B07_08": "MATR", "B07": "MATR", "B08": "MATR", "B09": "ENRG", "C10T12": "CSTA", "C13T15": "CDIS", "C16": "MATR",
    "C17_18": "MATR", "C19": "ENRG", "C20": "MATR", "C20_21": "MATR", "C21": "HLTH", "C22": "MATR", "C23": "MATR",
    "C24": "MATR", "C24A": "MATR", "C24B": "MATR", "C25": "INDU", "C26": "TECH", "C27": "INDU", "C28": "INDU",
    "C29": "CDIS", "C30": "INDU", "C301": "INDU", "C302T309": "INDU", "C31T33": "CDIS", "D": "UTIL", "D35": "UTIL",
    "E": "UTIL", "E36T39": "UTIL", "F": "INDU", "G": "CDIS", "H49": "INDU", "H50": "INDU", "H51": "INDU", "H52": "INDU",
    "H53": "INDU", "I": "CDIS", "J58T60": "COMM", "J61": "COMM", "J62_63": "TECH", "K": "FINS", "L": "REAL",
    "M": "INDU", "N": "INDU", "O": "NONMKT", "P": "NONMKT", "Q": "HLTH", "R": "CDIS", "S": "CDIS", "T": "NONMKT",
}


def icio_country(code: str, iso3: dict[str, str]) -> str | None:
    c = re.sub(r"\d$", "", code)  # CN1 / CN2, MX1 / MX2 (extended tables)
    if c == "ROW":
        return ROW
    if len(c) == 2:
        return c
    return iso3.get(c)


def find_zips(settings: Settings | None = None) -> list[Path]:
    return sorted(raw_dir("icio", settings).glob("*.zip"))


def edition_of(p: Path, default: int | None = None) -> int:
    m = re.search(r"ICIO(20\d\d)", p.name)
    if m:
        return int(m.group(1))
    yrs = [int(x) for x in re.findall(r"(20\d\d)", p.name)]
    return default or (max(yrs) + 3 if yrs else date.today().year)  # editions trail their last year by ~3 years


def aggregate_year(raw: bytes, iso3: dict[str, str]) -> pl.DataFrame:
    """One ICIO year CSV -> long frame of country x sector flows (intermediate + 'FD')."""
    hdr = raw[: raw.find(b"\n")].decode("utf-8", "replace").strip().replace('"', "").split(",")
    df = pl.read_csv(io.BytesIO(raw), schema_overrides={c: pl.Float64 for c in hdr[1:]})
    lab = df.columns[0]
    rows = df.get_column(lab).to_list()
    cols = df.columns[1:]

    def key(code: str) -> tuple[str | None, str | None]:
        if "_" not in code:
            return None, None
        c, ind = code.split("_", 1)
        cc = icio_country(c, iso3)
        if ind in FD_CODES:
            return cc, "FD"
        return cc, ICIO_TO_SECTOR.get(ind)

    rk = [key(r) for r in rows]
    ck = [key(c) for c in cols]
    rsel = [i for i, (c, s) in enumerate(rk) if c and s and s != "FD"]
    csel = [j for j, (c, s) in enumerate(ck) if c and s]
    mat = df.select(cols).to_numpy()[np.ix_(rsel, csel)]
    rlab = [f"{rk[i][0]}|{rk[i][1]}" for i in rsel]
    clab = [f"{ck[j][0]}|{ck[j][1]}" for j in csel]
    ru, rinv = np.unique(rlab, return_inverse=True)
    cu, cinv = np.unique(clab, return_inverse=True)
    from scipy import sparse  # noqa: PLC0415

    sr = sparse.csr_matrix((np.ones(len(rlab)), (rinv, np.arange(len(rlab)))), shape=(len(ru), len(rlab)))
    sc = sparse.csr_matrix((np.ones(len(clab)), (np.arange(len(clab)), cinv)), shape=(len(clab), len(cu)))
    agg = sr @ np.nan_to_num(mat) @ sc
    agg = np.asarray(agg)
    ii, jj = np.nonzero(agg)
    src = [ru[i].split("|") for i in ii]
    dst = [cu[j].split("|") for j in jj]
    return pl.DataFrame({"src_iso2": [s[0] for s in src], "src_sector": [s[1] for s in src],
                         "dst_iso2": [d[0] for d in dst], "dst_sector": [d[1] for d in dst],
                         "value_usd_m": agg[ii, jj]})


def run_io(settings: Settings | None = None, url: str | None = None, edition: int | None = None,
           years: list[int] | None = None) -> dict[str, Any]:
    from app.linkage.common import http  # noqa: PLC0415

    s = settings_of(settings)
    d = raw_dir("icio", s)
    if url:
        dest = d / (Path(url.split("?")[0]).name if url.endswith(".zip") else f"ICIO{edition or 'X'}_custom.zip")
        if not dest.exists():
            with http("oecd_icio", s) as cl:
                cl.download(url, dest)
    zips = find_zips(s)
    if not zips:
        with http("oecd_icio", s) as cl:
            cl.download(ICIO_2023_URL, d / ICIO_2023_FILE)
        zips = find_zips(s)
    # newest edition wins per year
    iso3 = iso3_to_iso2_map(s)
    best: dict[int, tuple[int, Path, str]] = {}
    for z in zips:
        ed = edition_of(z, edition)
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                m = re.match(r"^(?:.*/)?(\d{4})(?:_SML)?\.csv$", n)
                if m and (not years or int(m.group(1)) in years):
                    y = int(m.group(1))
                    if y not in best or best[y][0] < ed:
                        best[y] = (ed, z, n)
    rep: dict[str, Any] = {"years": {}, "zips": [z.name for z in zips]}
    for y, (ed, z, n) in sorted(best.items()):
        with zipfile.ZipFile(z) as zf:
            df = aggregate_year(zf.read(n), iso3)
        df = df.with_columns(pl.lit(ed, pl.Int16).alias("edition"), pl.lit(y, pl.Int16).alias("year"),
                             pl.lit(date(ed, 11, 30)).alias("knowledge_date"), pl.lit(now_utc()).alias("ingested_at"))
        rep["years"][y] = {"edition": ed, "rows": write(df, "io_flows", s)}
        log.info("linkage io %s (edition %s): %d rows", y, ed, df.height)
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_io(ctx.settings, url=p.get("url"), edition=int(p["edition"]) if p.get("edition") else None)
    n = sum(v["rows"] for v in rep["years"].values())
    update_progress("A3", "done" if n else "partial", settings=ctx.settings, **rep)
    return JobResult(status="success" if n else "partial", rows_written={"io_flows": n}, message=str(rep))
