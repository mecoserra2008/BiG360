"""Official curve sources (W3): pure parsers (tested on recorded fixtures, tests/rates/fixtures) + loaders.

Every loader `load_<source>(s, start, backfill)` returns a `Loaded` with yield_curves rows (percent values converted
to integer bp x 100 by app.rates.common.curve_rows) and, for Svensson sources, yield_curve_params rows. Loaders never
raise for one bad file: they record a note and return what they have. Politeness: <= 1 request / second per host
(app.rates.common.client), on-disk HTTP cache, large archives downloaded once into data/raw/rates/<source>/.

Sources (verified live 2026-10-31; robots.txt / terms checked):
  ust         home.treasury.gov daily par / real yield curve XML (OData feed, one request per year)
  fred        Fed H.15 constant maturities via FRED (DGS*, DFII*), read from the macro lake (job macro_fred)
  ecb         ECB Data Portal YC: Svensson parameters of the AAA and all-issuer euro area curves
  bundesbank  Bundesbank BBSIS Svensson parameters of listed Federal securities
  boe         Bank of England yield-curve archive zips (nominal / real / inflation / OIS; media files, not IADB)
  mof_jp      Ministry of Finance Japan JGB interest rates CSV (full history + current month)
  bde         Banco de Espana BIE CSV tables TI_1_3 (government bond yields) and TI_1_7 (EURIBOR)
  bpstat      Banco de Portugal BPstat API, OT yields by residual maturity
  snb         data.snb.ch cube rendeiduebd (Confederation spot rates)
  boc         Bank of Canada Valet (benchmark bonds, T-bills)
  rba         RBA statistical tables F2 (CGS) and F17 (zero-coupon yields)
  riksbank    Riksbank SWEA API; norges: Norges Bank SDMX GOVT_GENERIC_RATES
  ecb_irs     ECB IRS long-term convergence yields (monthly, EU27)
  oecd        OECD KEI / MEI long-term rates from the macro lake (oecd:KEI.*.IRLT, fred:IRLTLT01*)
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.logsetup import get_logger

from . import registry as R
from . import svensson as SV
from .common import client, curve_rows, http_get, tenor_months_from_label

log = get_logger("app.rates.sources")


@dataclass
class Loaded:
    rows: list[dict[str, Any]] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    requests: int = 0

    def extend(self, o: "Loaded") -> "Loaded":
        self.rows += o.rows
        self.params += o.params
        self.notes += o.notes
        self.requests += o.requests
        return self


def _f(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f else None
    s = str(v).strip().replace("\u2212", "-")
    if s in ("", "_", "-", "--", ".", "NA", "N/A", "n.a.", "#N/A", "#VALUE!", "nan", "NaN", "null", "None", ":", ".."):
        return None
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None


# =============================================================================================== US Treasury

UST_NOMINAL = {"BC_1MONTH": 1, "BC_2MONTH": 2, "BC_3MONTH": 3, "BC_4MONTH": 4, "BC_6MONTH": 6, "BC_1YEAR": 12,
               "BC_2YEAR": 24, "BC_3YEAR": 36, "BC_5YEAR": 60, "BC_7YEAR": 84, "BC_10YEAR": 120, "BC_20YEAR": 240,
               "BC_30YEAR": 360}
UST_REAL = {"TC_5YEAR": 60, "TC_7YEAR": 84, "TC_10YEAR": 120, "TC_20YEAR": 240, "TC_30YEAR": 360}
UST_XML = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
_PROP = re.compile(r"<m:properties>(.*?)</m:properties>", re.S)
_FIELD = re.compile(r"<d:([A-Z0-9_]+)[^>]*?(?:/>|>([^<]*)</d:\1>)")


def parse_ust_xml(text: str, cols: dict[str, int]) -> list[tuple[date, int, float | None]]:
    """Treasury OData XML feed -> (date, tenor_months, percent). Unknown columns (BC_1_5MONTH, *DISPLAY) ignored."""
    out: list[tuple[date, int, float | None]] = []
    for blk in _PROP.findall(text):
        f = {k: v for k, v in _FIELD.findall(blk)}
        try:
            d = date.fromisoformat((f.get("NEW_DATE") or "")[:10])
        except ValueError:
            continue
        for k, m in cols.items():
            v = _f(f.get(k))
            if v is not None:
                out.append((d, m, v))
    return out


def load_ust(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    today = date.today()
    for data, cid, cols in (("daily_treasury_yield_curve", "US_TSY", UST_NOMINAL),
                            ("daily_treasury_real_yield_curve", "US_TSY_REAL", UST_REAL)):
        c = R.BY_ID[cid]
        y0 = max(start.year, int(c.history_start[:4]))
        for y in range(y0, today.year + 1):
            try:
                txt = http_get("ust", UST_XML, {"data": data, "field_tdr_date_value": str(y)},
                               ttl=3 * 3600 if y >= today.year - 0 else 30 * 86400, settings=s).decode("utf-8", "replace")
                res.requests += 1
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"{cid} {y}: {type(e).__name__}: {e}"[:200])
                continue
            pts = [p for p in parse_ust_xml(txt, cols) if p[0] >= start]
            res.rows += curve_rows(cid, c.kind, "ust", "par", pts)
    return res


# =============================================================================================== Fed H.15 (FRED lake)

H15_NOMINAL = {"DGS1MO": 1, "DGS3MO": 3, "DGS6MO": 6, "DGS1": 12, "DGS2": 24, "DGS3": 36, "DGS5": 60, "DGS7": 84,
               "DGS10": 120, "DGS20": 240, "DGS30": 360}
H15_TIPS = {"DFII5": 60, "DFII7": 84, "DFII10": 120, "DFII20": 240, "DFII30": 360}


def load_fred(s: Settings, start: date, backfill: bool = False) -> Loaded:
    """H.15 constant maturities from the macro lake (FRED is the Fed's official redistribution of H.15; the H.15
    Data Download Program is being retired in Nov 2026)."""
    from app.macro.cb.common import lake_series  # noqa: PLC0415

    res = Loaded()
    for cid, cols in (("US_H15", H15_NOMINAL), ("US_H15_TIPS", H15_TIPS)):
        c = R.BY_ID[cid]
        lake = lake_series([f"fred:{k}" for k in cols], s, start)
        if not lake:
            res.notes.append(f"{cid}: no FRED series in the macro lake yet (job macro_fred)")
        for sid, pairs in lake.items():
            m = cols[sid.split(":", 1)[1]]
            res.rows += curve_rows(cid, c.kind, "fred_h15", "par", [(d, m, v) for d, v in pairs])
    return res


# =============================================================================================== Svensson (ECB / Bundesbank)

ECB_YC = "https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.{inst}.SV_C_YM.BETA0+BETA1+BETA2+BETA3+TAU1+TAU2"
ECB_INST = {"G_N_A": "EA_AAA", "G_N_C": "EA_ALL"}


def parse_ecb_params(text: str) -> dict[tuple[str, date], dict[str, float]]:
    """ECB csvdata -> {(curve_id, date): {BETA0..TAU2}} (complete sets only)."""
    out: dict[tuple[str, date], dict[str, float]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        cid = ECB_INST.get(r.get("INSTRUMENT_FM", ""))
        v = _f(r.get("OBS_VALUE"))
        try:
            d = date.fromisoformat((r.get("TIME_PERIOD") or "")[:10])
        except ValueError:
            continue
        if cid and v is not None:
            out.setdefault((cid, d), {})[r.get("DATA_TYPE_FM", "")] = v
    return {k: p for k, p in out.items() if all(x in p for x in SV.PARAMS)}


BUBA_URL = "https://api.statistiken.bundesbank.de/rest/data/BBSIS/D.I.ZST.B0+B1+B2+B3+T1+T2.EUR.S1311.B.A604._Z.R.A.A._Z._Z.A"
BUBA_MAP = {"B0": "BETA0", "B1": "BETA1", "B2": "BETA2", "B3": "BETA3", "T1": "TAU1", "T2": "TAU2"}


def parse_bundesbank_csv(text: str) -> dict[date, dict[str, float]]:
    """Bundesbank BBk CSV: German locale (';' separated, decimal comma) or English (',' / decimal point); metadata
    rows, then 'YYYY-MM-DD;v;flag;v;flag...'."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines:
        return {}
    sep = ";" if lines[0].count(";") > lines[0].count(",") else ","
    head = [h.strip().strip('"') for h in lines[0].split(sep)]
    cols: dict[int, str] = {}
    for i, h in enumerate(head):
        if h.endswith("_FLAGS"):
            continue
        m = re.search(r"\.ZST\.(B[0-3]|T[12])\.", h)
        if m:
            cols[i] = BUBA_MAP[m.group(1)]
    out: dict[date, dict[str, float]] = {}
    for ln in lines[1:]:
        parts = [p.strip().strip('"') for p in ln.split(sep)]
        try:
            d = date.fromisoformat(parts[0][:10])
        except (ValueError, IndexError):
            continue
        p = {k: _f(parts[i].replace(",", ".") if sep == ";" else parts[i]) for i, k in cols.items() if i < len(parts)}
        p = {k: v for k, v in p.items() if v is not None}
        if all(x in p for x in SV.PARAMS):
            out[d] = p
    return out


def svensson_rows(curve_id: str, source: str, params: dict[date, dict[str, float]], start: date) -> Loaded:
    c = R.BY_ID[curve_id]
    res = Loaded()
    for d, p in sorted(params.items()):
        if d < start or not SV.valid(p):
            continue
        res.params.append({"date": d, "curve_id": curve_id, "model": "svensson" if p.get("BETA3") else "nelson_siegel",
                           "beta0": p["BETA0"], "beta1": p["BETA1"], "beta2": p["BETA2"], "beta3": p.get("BETA3", 0.0),
                           "tau1": p["TAU1"], "tau2": p.get("TAU2"), "source": source})
        try:
            pts = SV.curve_points(p, c.tenors, c.types)
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"{curve_id} {d}: {e}")
            continue
        for typ, m in pts.items():
            res.rows += curve_rows(curve_id, c.kind, source, typ, [(d, t, v) for t, v in m.items()])
    return res


def load_ecb(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    st = max(start, date(2004, 9, 6))
    for inst, cid in ECB_INST.items():
        try:
            txt = http_get("ecb", ECB_YC.format(inst=inst), {"format": "csvdata", "detail": "dataonly",
                                                            "startPeriod": st.isoformat()},
                           ttl=3 * 3600, settings=s, timeout=300).decode("utf-8", "replace")
            res.requests += 1
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"{cid}: {type(e).__name__}: {e}"[:200])
            continue
        prm = {d: p for (c, d), p in parse_ecb_params(txt).items() if c == cid}
        res.extend(svensson_rows(cid, "ecb", prm, start))
    return res


def load_bundesbank(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    try:
        txt = http_get("bundesbank", BUBA_URL, {"format": "csv", "startPeriod": start.isoformat(), "lang": "en"},
                       ttl=3 * 3600, settings=s, timeout=300).decode("utf-8", "replace")
        res.requests += 1
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"DE_BUND: {type(e).__name__}: {e}"[:200])
        return res
    return res.extend(svensson_rows("DE_BUND", "bundesbank", parse_bundesbank_csv(txt), start))


# =============================================================================================== Bank of England

BOE_BASE = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/"
BOE_ARCHIVES = {"GB_GILT": "glcnominalddata.zip", "GB_GILT_REAL": "glcrealddata.zip",
                "GB_INFLATION": "glcinflationddata.zip", "GB_OIS": "oisddata.zip"}
BOE_FILE_KEY = {"GB_GILT": "nominal", "GB_GILT_REAL": "real", "GB_INFLATION": "inflation", "GB_OIS": "ois"}
#: sheet-name fragments (older archive workbooks prefix the curve kind: "2. nominal fwd curve", "4. real spot curve")
BOE_SHEETS = {"spot": ("spot curve", "spot, short end"), "forward": ("fwd curve", "fwds, short end")}


def _boe_sheet(ws: Any, monthly: bool) -> dict[date, dict[int, float]]:
    """'years:' / 'months:' header row with maturities, then one row per date (datetime in column A)."""
    mats: list[int | None] = []
    out: dict[date, dict[int, float]] = {}
    for r in ws.iter_rows(values_only=True):
        if not r:
            continue
        a = r[0]
        if isinstance(a, str) and a.strip().lower().startswith(("years", "months")):
            unit_m = a.strip().lower().startswith("months")
            mats = []
            for x in r[1:]:
                v = _f(x)
                mats.append(None if v is None else int(round(v if (unit_m or monthly) else v * 12)))
            continue
        if mats and isinstance(a, (datetime, date)):
            d = a.date() if isinstance(a, datetime) else a
            row = {}
            for m, v in zip(mats, r[1:], strict=False):
                fv = _f(v) if not isinstance(v, str) else None
                if m is not None and fv is not None:
                    row[m] = fv
            if row:
                out[d] = row
    return out


def parse_boe_workbook(content: bytes, tenors: tuple[int, ...]) -> dict[str, dict[date, dict[int, float]]]:
    """One BoE GLC / OIS workbook -> {measure: {date: {tenor_months: percent}}}; the long sheet wins over the short
    end where both have a tenor, short end fills the monthly tenors (1M, 3M, 9M, ...)."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    want = set(tenors)
    out: dict[str, dict[date, dict[int, float]]] = {}
    try:
        for measure, (long_name, short_name) in BOE_SHEETS.items():
            agg: dict[date, dict[int, float]] = {}
            for frag, monthly in ((short_name, True), (long_name, False)):
                name = next((n for n in wb.sheetnames if frag in n.lower()), None)
                if name is None:
                    continue
                for d, row in _boe_sheet(wb[name], monthly).items():
                    tgt = agg.setdefault(d, {})
                    for m, v in row.items():
                        if m in want:
                            tgt[m] = v
            if agg:
                out[measure] = agg
    finally:
        wb.close()
    return out


def _boe_rows(cid: str, parsed: dict[str, dict[date, dict[int, float]]], start: date) -> list[dict[str, Any]]:
    c = R.BY_ID[cid]
    rows: list[dict[str, Any]] = []
    for measure, by_date in parsed.items():
        rows += curve_rows(cid, c.kind, "boe", measure,
                           [(d, m, v) for d, row in by_date.items() if d >= start for m, v in row.items()])
    return rows


def load_boe(s: Settings, start: date, backfill: bool = False) -> Loaded:
    """Nightly: the current-month workbooks (latest-yield-curve-data.zip). When `start` precedes the current month
    (backfill / catch-up): the full archive zips (downloaded to data/raw/rates/boe/, refreshed weekly) - only the
    workbooks whose period overlaps the window are parsed."""
    res = Loaded()
    today = date.today()
    month0 = date(today.year, today.month, 1)
    try:
        z = zipfile.ZipFile(io.BytesIO(http_get("boe_yc", BOE_BASE + "latest-yield-curve-data.zip", ttl=3 * 3600,
                                                settings=s, browser=True)))
        res.requests += 1
        for n in z.namelist():
            low = n.lower()
            cid = next((k for k, key in BOE_FILE_KEY.items() if low.startswith(f"glc {key}") or
                        (key == "ois" and low.startswith("ois"))), None)
            if cid:
                res.rows += _boe_rows(cid, parse_boe_workbook(z.read(n), R.BY_ID[cid].tenors), start)
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"BoE latest: {type(e).__name__}: {e}"[:200])
    if start >= month0 - timedelta(days=3) and not backfill:
        return res
    raw = s.raw_dir / "rates" / "boe"
    raw.mkdir(parents=True, exist_ok=True)
    for cid, fname in BOE_ARCHIVES.items():
        dest = raw / fname
        try:
            fresh = dest.exists() and (datetime.now().timestamp() - dest.stat().st_mtime) < 7 * 86400
            if not fresh:
                client("boe_yc", s, browser=True, timeout=600).download(BOE_BASE + fname, dest)
                res.requests += 1
            with zipfile.ZipFile(dest) as z:
                for n in z.namelist():
                    yrs = [int(y) for y in re.findall(r"(19\d\d|20\d\d)", n)]
                    if "present" in n.lower():
                        yrs.append(today.year)
                    if yrs and max(yrs) < start.year:
                        continue
                    res.rows += _boe_rows(cid, parse_boe_workbook(z.read(n), R.BY_ID[cid].tenors), start)
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"BoE archive {fname}: {type(e).__name__}: {e}"[:200])
    return res


# =============================================================================================== Japan MoF

MOF_BASE = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate"


def load_mof_jp(s: Settings, start: date, backfill: bool = False) -> Loaded:
    from app.ingestion.macro_national import parse_mof_csv  # noqa: PLC0415

    res = Loaded()
    c = R.BY_ID["JP_JGB"]
    today = date.today()
    urls = [(f"{MOF_BASE}/jgbcme.csv", 3 * 3600)]
    if start < date(today.year, today.month, 1):
        urls.insert(0, (f"{MOF_BASE}/historical/jgbcme_all.csv", 2 * 86400))
    for url, ttl in urls:
        try:
            data = parse_mof_csv(http_get("mof_jp", url, ttl=ttl, settings=s, timeout=300))
            res.requests += 1
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"MoF {url.rsplit('/', 1)[-1]}: {type(e).__name__}: {e}"[:200])
            continue
        pts = []
        for lab, pairs in data.items():
            m = tenor_months_from_label(lab)
            if m is None or m not in c.tenors:
                continue
            pts += [(d, m, v) for d, v in pairs if d >= start and v is not None]
        res.rows += curve_rows("JP_JGB", c.kind, "mof_jp", "par", pts)
    return res


# =============================================================================================== Spain (Banco de Espana)

BDE_CSV = "https://www.bde.es/webbe/es/estadisticas/compartido/datos/csv/{t}.csv"
BDE_ES = {"D_DTES00S7": 6, "D_DTES00U7": 12, "D_G0B1F0ZN": 36, "D_G0B1F0ZO": 60, "D_G0B1F0ZP": 120, "D_G0B1F0ZQ": 180}
BDE_EURIBOR = {"D_DNBAC172": 1, "D_DNBAD172": 3, "D_DNBAE172": 6, "D_DNBAF172": 12}
_ES_MON = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10,
           "NOV": 11, "DIC": 12}


def _bde_date(s: str) -> date | None:
    m = re.fullmatch(r"(\d{1,2}) ([A-Z]{3}) (\d{4})", s.strip().upper())
    if not m or m.group(2) not in _ES_MON:
        return None
    try:
        return date(int(m.group(3)), _ES_MON[m.group(2)], int(m.group(1)))
    except ValueError:
        return None


def parse_bde_csv(text: str) -> dict[str, list[tuple[date, float]]]:
    """BdE BIE table CSV: first row 'CODIGO DE LA SERIE', codes...; metadata rows; then 'DD MMM YYYY', values
    ('_' = missing); trailing FUENTE / NOTAS rows."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return {}
    codes = [c.strip() for c in rows[0][1:]]
    out: dict[str, list[tuple[date, float]]] = {c: [] for c in codes}
    for r in rows[1:]:
        if not r:
            continue
        d = _bde_date(r[0])
        if d is None:
            continue
        for c, v in zip(codes, r[1:], strict=False):
            fv = _f(v)
            if fv is not None:
                out[c].append((d, fv))
    return {k: v for k, v in out.items() if v}


def load_bde(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    for table, cid, cols in (("ti_1_3", "ES_BONO", BDE_ES), ("ti_1_7", "EA_EURIBOR", BDE_EURIBOR)):
        c = R.BY_ID[cid]
        try:
            txt = http_get("bde", BDE_CSV.format(t=table), ttl=6 * 3600, settings=s, browser=True,
                           timeout=300).decode("latin-1")
            res.requests += 1
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"{cid}: {type(e).__name__}: {e}"[:200])
            continue
        data = parse_bde_csv(txt)
        for code, m in cols.items():
            res.rows += curve_rows(cid, c.kind, "bde", "yield", [(d, m, v) for d, v in data.get(code, []) if d >= start])
    return res


# =============================================================================================== Portugal (BPstat)

BPSTAT_URL = "https://bpstat.bportugal.pt/data/v1/domains/26/datasets/690b7b36fd36c0dbe249c48cbbc39524/"
BPSTAT_PT = {"12099454": 24, "12099455": 36, "12099456": 48, "12099457": 60, "12099458": 84, "12099459": 120}


def load_bpstat(s: Settings, start: date, backfill: bool = False) -> Loaded:
    from app.ingestion.macro_eu import parse_bpstat  # noqa: PLC0415

    res = Loaded()
    c = R.BY_ID["PT_OT"]
    try:
        j = json.loads(http_get("bpstat", BPSTAT_URL, {"lang": "EN", "series_ids": ",".join(BPSTAT_PT),
                                                       "obs_since": start.isoformat()}, ttl=6 * 3600, settings=s,
                                timeout=300))
        res.requests += 1
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"PT_OT: {type(e).__name__}: {e}"[:200])
        return res
    for sid, pairs in parse_bpstat(j).items():
        m = BPSTAT_PT.get(sid)
        if m:
            res.rows += curve_rows("PT_OT", c.kind, "bpstat", "yield", [(d, m, v) for d, v in pairs if d >= start])
    return res


# =============================================================================================== SNB

SNB_URL = "https://data.snb.ch/api/cube/rendeiduebd/data/csv/en"


def parse_snb_spot_all(text: str) -> list[tuple[date, int, float]]:
    out = []
    for line in text.lstrip("\ufeff").splitlines():
        parts = [p.strip().strip('"') for p in line.split(";")]
        if len(parts) != 4 or parts[1] != "CHF" or not parts[2].endswith("J"):
            continue
        try:
            d = date.fromisoformat(parts[0])
            m = int(round(float(parts[2][:-1]) * 12))
        except ValueError:
            continue
        v = _f(parts[3])
        if v is not None:
            out.append((d, m, v))
    return out


def load_snb(s: Settings, start: date, backfill: bool = False) -> Loaded:
    """The cube answers at most ~50k rows per request (a 1988 -> request stops in 2009): 4-year windows."""
    res = Loaded()
    c = R.BY_ID["CH_CONF"]
    a = start
    today = date.today()
    while a <= today:
        b = min(date(a.year + 4, a.month, 1) - timedelta(days=1), today)
        try:
            txt = http_get("snb", SNB_URL, {"fromDate": a.isoformat(), "toDate": b.isoformat()},
                           ttl=12 * 3600 if b >= today - timedelta(days=60) else 30 * 86400, settings=s,
                           timeout=300).decode("utf-8", "replace")
            res.requests += 1
            res.rows += curve_rows("CH_CONF", c.kind, "snb", "spot",
                                   [p for p in parse_snb_spot_all(txt) if p[1] in c.tenors and a <= p[0] <= b])
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"CH_CONF {a}..{b}: {type(e).__name__}: {e}"[:200])
        a = b + timedelta(days=1)
    return res


# =============================================================================================== Bank of Canada

VALET = "https://www.bankofcanada.ca/valet/observations/{codes}/json"
BOC_CODES = {"V80691342": 1, "V80691344": 3, "V80691345": 6, "V80691346": 12, "BD.CDN.2YR.DQ.YLD": 24,
             "BD.CDN.3YR.DQ.YLD": 36, "BD.CDN.5YR.DQ.YLD": 60, "BD.CDN.7YR.DQ.YLD": 84, "BD.CDN.10YR.DQ.YLD": 120,
             "BD.CDN.LONG.DQ.YLD": 360}


def load_boc(s: Settings, start: date, backfill: bool = False) -> Loaded:
    from app.macro.cb.rates import parse_valet  # noqa: PLC0415

    res = Loaded()
    c = R.BY_ID["CA_GOC"]
    try:
        j = json.loads(http_get("boc", VALET.format(codes=",".join(BOC_CODES)), {"start_date": start.isoformat()},
                                ttl=6 * 3600, settings=s, timeout=300))
        res.requests += 1
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"CA_GOC: {type(e).__name__}: {e}"[:200])
        return res
    for code, pairs in parse_valet(j).items():
        m = BOC_CODES.get(code)
        if m:
            res.rows += curve_rows("CA_GOC", c.kind, "boc", "yield", [(d, m, v) for d, v in pairs if d >= start])
    return res


# =============================================================================================== RBA

RBA_CSV = "https://www.rba.gov.au/statistics/tables/csv/{t}.csv"
RBA_F2 = {"FCMYGBAG2D": 24, "FCMYGBAG3D": 36, "FCMYGBAG5D": 60, "FCMYGBAG10D": 120}


def parse_rba_table(text: str) -> dict[str, list[tuple[date, float]]]:
    """RBA statistical table CSV: metadata rows, 'Series ID,...' row, then 'DD-Mon-YYYY,...' rows."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    ids: list[str] = []
    out: dict[str, list[tuple[date, float]]] = {}
    for r in rows:
        if not r:
            continue
        if r[0].strip() == "Series ID":
            ids = [x.strip() for x in r[1:]]
            continue
        if not ids:
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        for k, v in zip(ids, r[1:], strict=False):
            fv = _f(v)
            if fv is not None:
                out.setdefault(k, []).append((d, fv))
    return out


def _f17_months(code: str) -> int | None:
    m = re.fullmatch(r"FZCY(\d+)D", code)
    return int(round(int(m.group(1)) / 100 * 12)) if m else None


def load_rba(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    c = R.BY_ID["AU_ACGB"]
    for t, typ in (("f2-data", "yield"), ("f17-yields", "spot")):
        try:
            txt = http_get("rba", RBA_CSV.format(t=t), ttl=6 * 3600, settings=s, timeout=300).decode("utf-8", "replace")
            res.requests += 1
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"AU_ACGB {t}: {type(e).__name__}: {e}"[:200])
            continue
        for code, pairs in parse_rba_table(txt).items():
            m = RBA_F2.get(code) if typ == "yield" else _f17_months(code)
            if m is None or m not in c.tenors:
                continue
            res.rows += curve_rows("AU_ACGB", c.kind, "rba", typ, [(d, m, v) for d, v in pairs if d >= start])
    return res


# =============================================================================================== Riksbank / Norges Bank

RIKSBANK_URL = "https://api.riksbank.se/swea/v1/Observations/{s}/{start}"
RIKSBANK = {"SETB3MBENCH": 3, "SETB6MBENCH": 6, "SEGVB2YC": 24, "SEGVB5YC": 60, "SEGVB7YC": 84, "SEGVB10YC": 120}
NORGES_URL = "https://data.norges-bank.no/api/data/GOVT_GENERIC_RATES/B.{tenors}.?format=csv&startPeriod={start}&locale=en"
NORGES = {"3M": 3, "6M": 6, "12M": 12, "3Y": 36, "5Y": 60, "10Y": 120}


def load_riksbank(s: Settings, start: date, backfill: bool = False) -> Loaded:
    from app.macro.cb.rates import parse_riksbank  # noqa: PLC0415

    res = Loaded()
    c = R.BY_ID["SE_SGB"]
    for code, m in RIKSBANK.items():
        try:
            pairs = parse_riksbank(json.loads(http_get("riksbank", RIKSBANK_URL.format(s=code, start=start.isoformat()),
                                                       ttl=6 * 3600, settings=s)))
            res.requests += 1
        except Exception as e:  # noqa: BLE001
            res.notes.append(f"SE_SGB {code}: {type(e).__name__}: {e}"[:200])
            continue
        res.rows += curve_rows("SE_SGB", c.kind, "riksbank", "yield", [(d, m, v) for d, v in pairs if d >= start])
    return res


def load_norges(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    c = R.BY_ID["NO_NGB"]
    try:
        raw = http_get("norges", NORGES_URL.format(tenors="+".join(NORGES), start=start.isoformat()), ttl=6 * 3600,
                       settings=s, timeout=300).decode("utf-8", "replace")
        res.requests += 1
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"NO_NGB: {type(e).__name__}: {e}"[:200])
        return res
    pts = []
    for r in csv.DictReader(io.StringIO(raw.lstrip("\ufeff")), delimiter=";"):
        m = NORGES.get(r.get("TENOR", ""))
        v = _f(r.get("OBS_VALUE"))
        try:
            d = date.fromisoformat((r.get("TIME_PERIOD") or "")[:10])
        except ValueError:
            continue
        if m and v is not None and d >= start:
            pts.append((d, m, v))
    res.rows += curve_rows("NO_NGB", c.kind, "norges", "yield", pts)
    return res


# =============================================================================================== monthly 10y (ECB IRS, OECD)

ECB_IRS = "https://data-api.ecb.europa.eu/service/data/IRS/M.{cc}.L.L40.CI.0000.EUR.N.Z"


def parse_ecb_irs(text: str) -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        v = _f(r.get("OBS_VALUE"))
        tp = (r.get("TIME_PERIOD") or "").strip()
        try:
            d = date(int(tp[:4]), int(tp[5:7]), 1)
        except ValueError:
            continue
        if v is not None:
            out.setdefault(r.get("REF_AREA", ""), []).append((d, v))
    return out


def load_ecb_irs(s: Settings, start: date, backfill: bool = False) -> Loaded:
    res = Loaded()
    try:
        txt = http_get("ecb", ECB_IRS.format(cc="+".join(R.LT10_ECB)), {"format": "csvdata", "detail": "dataonly",
                                                                        "startPeriod": f"{start:%Y-%m}"},
                       ttl=12 * 3600, settings=s, timeout=300).decode("utf-8", "replace")
        res.requests += 1
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"ECB IRS: {type(e).__name__}: {e}"[:200])
        return res
    for cc, pairs in parse_ecb_irs(txt).items():
        cid = f"{cc}_LT10"
        if cid in R.BY_ID:
            res.rows += curve_rows(cid, "nominal", "ecb_irs", "yield", [(d, 120, v) for d, v in pairs if d >= start])
    return res


def load_oecd(s: Settings, start: date, backfill: bool = False) -> Loaded:
    """OECD long-term rates from the macro lake: oecd:KEI.{ISO3}.IRLT, else fred:IRLTLT01{CC}M156N (OECD MEI via
    FRED). Countries with neither are reported in the notes."""
    from app.macro.cb.common import lake_series  # noqa: PLC0415

    res = Loaded()
    ids = {cc: (f"oecd:KEI.{iso3}.IRLT", f"fred:IRLTLT01{cc}M156N") for cc, iso3 in R.LT10_OECD.items()
           if cc not in R.LT10_ECB}
    lake = lake_series([x for p in ids.values() for x in p], s, start)
    missing = []
    for cc, (a, b) in ids.items():
        pairs = lake.get(a) or lake.get(b)
        if not pairs:
            missing.append(cc)
            continue
        src = "oecd" if lake.get(a) else "fred_oecd_mei"
        res.rows += curve_rows(f"{cc}_LT10", "nominal", src, "yield", [(date(d.year, d.month, 1), 120, v) for d, v in pairs])
    if missing:
        res.notes.append(f"LT10 without a lake series: {','.join(sorted(missing))}")
    return res


LOADERS: dict[str, Callable[[Settings, date, bool], Loaded]] = {
    "ust": load_ust, "fred": load_fred, "ecb": load_ecb, "bundesbank": load_bundesbank, "boe": load_boe,
    "mof_jp": load_mof_jp, "bde": load_bde, "bpstat": load_bpstat, "snb": load_snb, "boc": load_boc, "rba": load_rba,
    "riksbank": load_riksbank, "norges": load_norges, "ecb_irs": load_ecb_irs, "oecd": load_oecd,
}

#: rejected / not machine-readable sources (reported by the API and the final report)
REJECTED: dict[str, str] = {
    "FR (Banque de France TEC)": "webstat.banque-france.fr keyless API serves the catalogue only (has_records=false); "
                                 "observations need the keyed BdF API -> monthly ECB IRS convergence yield used (FR_LT10)",
    "IT (Banca d'Italia)": "infostat.bancaditalia.it answers 'Risorsa Protetta'; daily BTP benchmarks are MTS "
                           "(commercial) -> monthly ECB IRS used (IT_LT10)",
    "US TreasuryDirect TA_WS": "treasurydirect.gov robots.txt disallows all agents except usasearch -> Fiscal Data "
                               "API (api.fiscaldata.treasury.gov, official Treasury) used for auctions",
    "Fed H.15 Data Download Program": "DDP is being retired (week of 2026-11-09) -> H.15 via FRED",
    "EMMI EURIBOR files": "emmi-benchmarks.eu robots.txt disallows /media and /assets (the download paths) -> "
                          "EURIBOR as republished by Banco de Espana",
    "BoE IADB database": "robots.txt disallows /boeapps/database/_iadb-FromShowColumns.asp -> the yield-curve archive "
                         "media files are used for every BoE curve",
    "FINRA Fixed Income Data pages": "Terms of use forbid robots / automated copying ('use any robot, spider, other "
                                     "automatic ... process to monitor or copy the Data') -> not used; TRACE "
                                     "aggregates only through the keyed FINRA API, US bond reference via OpenFIGI",
    "AFT (France debt agency)": "Cloudflare bot challenge (403 'Just a moment') -> not used",
}
