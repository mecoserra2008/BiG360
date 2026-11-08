"""W3.2 official curve loaders (verified live 2026-10-12). Every loader returns a ``CurveSet`` with daily curves
for the requested window. Sources (official only):

  fed     US Treasury daily par yield curve incl. bills (home.treasury.gov CSV, ingested as ust:1M..30Y by
          macro_national) -> bootstrapped DF knots (bills <= 6M simple/BEY, 1Y BEY zero, 2-10Y semiannual par);
          NY Fed EFFR (official, via FRED EFFR / markets.newyorkfed.org) for the overnight anchor; NY Fed ACM term
          premia TP01..TP10 (ACMTermPremium.xls, 'ACM Daily') for the term-premium-adjusted path.
  boe     Bank of England UK OIS (SONIA) instantaneous forward curve, short end (monthly maturities 1-60m) from the
          BoE yield-curve archive (latest-yield-curve-data.zip + oisddata.zip) - closest to a true OIS path.
  ecb     ECB euro-area AAA government yield curve, Svensson parameters (YC B.U2.EUR.4F.G_N_A.SV_C_YM.BETA0..TAU2)
          -> closed-form instantaneous forwards. Govvie curve (not OIS): carries collateral/scarcity premia.
  boj     MoF JGB benchmark par yields 1Y-10Y (jgbcme.csv + jgbcme_all.csv) -> bootstrapped; no bills -> 12/24m only.
  boc     Bank of Canada Valet: T-bill yields 1M/3M/6M/1Y (V80691342/44/45/46) + benchmark bonds 2Y/3Y/5Y.
  rba     RBA F1 bank-bill (BAB/NCD) 1M/3M/6M + F2 CGS 2Y/3Y/5Y (RBA OIS series discontinued Dec 2022) - bank bills
          include a bank credit premium (absorbed by the basis).
  snb     SNB Confederation spot (zero) yields 1Y-10Y (cube rendeiduebd, published monthly) -> 12/24m only.
  riksbank Riksbank SWEA: Swedish T-bills 3M/6M + government bonds 2Y/5Y.
  norges  Norges Bank GOVT_GENERIC_RATES: T-bills 3M/6M/12M + government bonds 3Y/5Y.
Other banks: no official machine-readable short-end curve -> "not available".
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.logsetup import get_logger

from .common import http_get, lake_series, to_float
from .curves import KnotCurve, bootstrap_par, grid_forward_curve, svensson_curve

log = get_logger(__name__)

ACM_URL = "https://www.newyorkfed.org/medialibrary/media/research/data_indicators/ACMTermPremium.xls"
NYFED_EFFR_URL = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/{n}.json"
BOE_LATEST_ZIP = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip"
BOE_OIS_ZIP = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/oisddata.zip"
ECB_YC_URL = "https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.BETA0+BETA1+BETA2+BETA3+TAU1+TAU2"
MOF_BASE = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate"
VALET_URL = "https://www.bankofcanada.ca/valet/observations/{codes}/json"
RBA_URL = "https://www.rba.gov.au/statistics/tables/csv/{t}-data.csv"
SNB_URL = "https://data.snb.ch/api/cube/rendeiduebd/data/csv/en"
RIKSBANK_URL = "https://api.riksbank.se/swea/v1/Observations/{s}/{start}"
NORGES_URL = "https://data.norges-bank.no/api/data/GOVT_GENERIC_RATES/B.{tenors}.?format=csv&startPeriod={start}&locale=en"

TENOR_Y = {"ON": 1 / 365, "1M": 1 / 12, "2M": 2 / 12, "3M": 0.25, "4M": 4 / 12, "6M": 0.5, "12M": 1.0, "1Y": 1.0, "2Y": 2.0,
           "3Y": 3.0, "5Y": 5.0, "7Y": 7.0, "10Y": 10.0}


@dataclass
class CurveSet:
    bank: str
    source: str
    method: str
    caveats: list[str]
    curves: dict[date, Any] = field(default_factory=dict)       # date -> KnotCurve | FuncCurve
    tp: dict[date, list[tuple[float, float]]] = field(default_factory=dict)  # US: ACM TP knots
    meetings_ok: bool = True     # short end resolves individual meetings
    horizons: tuple[tuple[str, int], ...] = (("3m", 3), ("6m", 6), ("12m", 12), ("24m", 24))
    effective_lag_days: int = 1
    status: str = "ok"


COMMON_CAVEATS = [
    "Approximation from official curves, not an OIS/futures-implied probability; forwards include term premia "
    "unless stated, and the basis to the policy rate is assumed constant along the path.",
    "Basis = average forward over the stub before the next decision minus today's policy rate (when the next decision "
    "is < 10 days away or no calendar exists, the first month is used and may contain a priced move).",
]


def _panel(pairs_by_tenor: dict[str, list[tuple[date, float]]], start: date, ffill: dict[str, int] | None = None
           ) -> dict[date, dict[str, float]]:
    """{date: {tenor: value}}; tenors in ``ffill`` (e.g. weekly T-bills) are carried forward up to N days onto the
    dates of the other tenors."""
    out: dict[date, dict[str, float]] = {}
    for ten, pairs in pairs_by_tenor.items():
        for d, v in pairs:
            if d >= start - timedelta(days=max((ffill or {}).values(), default=0)) and v is not None:
                out.setdefault(d, {})[ten] = v
    for ten, ndays in (ffill or {}).items():
        last: tuple[date, float] | None = None
        for d in sorted(out):
            if ten in out[d]:
                last = (d, out[d][ten])
            elif last and (d - last[0]).days <= ndays:
                out[d][ten] = last[1]
    return {d: r for d, r in out.items() if d >= start}


def _bootstrap_panel(panel: dict[date, dict[str, float]], zero: dict[str, str], par: list[str], freq: int,
                     min_zero: int = 1, min_par: int = 1) -> dict[date, KnotCurve]:
    """zero: {tenor: compounding}; par: tenors (coupon bonds)."""
    out = {}
    for d, row in panel.items():
        zp = [(TENOR_Y[t], row[t], c) for t, c in zero.items() if t in row]
        pp = [(TENOR_Y[t], row[t]) for t in par if t in row]
        if len(zp) < min_zero or len(pp) < min_par:
            continue
        try:
            out[d] = KnotCurve(bootstrap_par(zp, pp, freq=freq))
        except Exception:  # noqa: BLE001
            continue
    return out


# =============================================================================================== US

def parse_acm_tp(content: bytes) -> dict[date, list[tuple[float, float]]]:
    from app.ingestion.macro_national import parse_acm_xls  # noqa: PLC0415

    cols = parse_acm_xls(content, "ACM Daily")
    out: dict[date, list[tuple[float, float]]] = {}
    for n in range(1, 11):
        for d, v in cols.get(f"ACMTP{n:02d}", []):
            if v is not None:
                out.setdefault(d, []).append((float(n), float(v)))
    return {d: sorted(v) for d, v in out.items() if len(v) >= 2}


def load_us(s: Settings, start: date) -> CurveSet:
    cs = CurveSet("fed", "US Treasury daily par yield curve incl. bills (home.treasury.gov) + NY Fed ACM term premia",
                  "UST par curve bootstrapped to zero/forward rates (bills <= 6M as simple 365-day yields, 1Y as "
                  "bond-equivalent zero, 2-10Y semiannual par); implied fed funds midpoint = average forward per "
                  "inter-meeting period minus the stub basis; term-premium-adjusted path subtracts NY Fed ACM "
                  "forward term premia (model-based, official NY Fed estimates).",
                  COMMON_CAVEATS + ["Treasury bills trade with a supply/convenience basis to fed funds that varies "
                                    "over time (e.g. around debt-ceiling episodes and quarter ends).",
                                    "ACM term premia are model-based (Adrian-Crump-Moench) and published with a lag; "
                                    "the adjusted path uses the latest available ACM date on or before the curve date."])
    tens = ["1M", "2M", "3M", "4M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y"]
    lake = lake_series([f"ust:{t}" for t in tens], s, start)
    panel = _panel({k.split(":")[1]: v for k, v in lake.items()}, start)
    cs.curves = _bootstrap_panel(panel, {"1M": "simple", "2M": "simple", "3M": "simple", "4M": "simple",
                                         "6M": "simple", "1Y": "semi"}, ["2Y", "3Y", "5Y", "7Y", "10Y"], 2,
                                 min_zero=3, min_par=3)
    try:
        cs.tp = parse_acm_tp(http_get("nyfed_acm", ACM_URL, ttl=12 * 3600, settings=s, timeout=240))
    except Exception as e:  # noqa: BLE001
        log.warning("cb curves: ACM failed: %s", e)
    cs.status = "ok" if cs.curves else "no data"
    return cs


def nyfed_effr(s: Settings, n: int = 30) -> list[tuple[date, float]]:
    """Official NY Fed EFFR (markets.newyorkfed.org API) - used to freshen the lake EFFR."""
    try:
        j = json.loads(http_get("nyfed_markets", NYFED_EFFR_URL.format(n=n), ttl=6 * 3600, settings=s))
        return sorted((date.fromisoformat(r["effectiveDate"]), float(r["percentRate"])) for r in j.get("refRates", []))
    except Exception as e:  # noqa: BLE001
        log.warning("cb curves: NY Fed EFFR failed: %s", e)
        return []


# =============================================================================================== UK

def parse_boe_ois_fwd(content: bytes) -> dict[date, list[tuple[float, float]]]:
    """BoE OIS workbook, sheet '1. fwds, short end': a 'years:' row with maturities, then date rows."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    name = next((n for n in wb.sheetnames if "fwds, short end" in n), None)
    if not name:
        return {}
    mats: list[float | None] = []
    out: dict[date, list[tuple[float, float]]] = {}
    for r in wb[name].iter_rows(values_only=True):
        if not r:
            continue
        if isinstance(r[0], str) and r[0].strip().lower().startswith("years"):
            mats = [to_float(x) for x in r[1:]]
            continue
        if mats and isinstance(r[0], datetime):
            pts = [(m, float(v)) for m, v in zip(mats, r[1:], strict=False)
                   if m is not None and isinstance(v, (int, float))]
            if len(pts) >= 6:
                out[r[0].date()] = pts
    return out


def load_uk(s: Settings, start: date) -> CurveSet:
    cs = CurveSet("boe", "Bank of England UK OIS instantaneous forward curve, short end (BoE yield-curve archive)",
                  "Official BoE SONIA OIS instantaneous forwards (monthly maturities); implied Bank Rate = average "
                  "forward per inter-meeting period minus the stub basis (SONIA - Bank Rate spread). No term-premium "
                  "adjustment (OIS term premia are small at the short end).",
                  COMMON_CAVEATS + ["BoE fits the OIS curve with a smoothing spline (Anderson-Sleath); very short "
                                    "maturities are smoothed and the curve is published with a ~1 business-day lag."],
                  effective_lag_days=0)
    curves: dict[date, list[tuple[float, float]]] = {}
    try:
        z = zipfile.ZipFile(io.BytesIO(http_get("boe_yc", BOE_OIS_ZIP, ttl=24 * 3600, settings=s, browser=True,
                                                timeout=240)))
        for n in z.namelist():
            if "present" in n.lower() or str(start.year) in n:
                curves.update(parse_boe_ois_fwd(z.read(n)))
    except Exception as e:  # noqa: BLE001
        log.warning("cb curves: BoE OIS archive failed: %s", e)
    try:
        z = zipfile.ZipFile(io.BytesIO(http_get("boe_yc", BOE_LATEST_ZIP, ttl=6 * 3600, settings=s, browser=True)))
        for n in z.namelist():
            if n.lower().startswith("ois"):
                curves.update(parse_boe_ois_fwd(z.read(n)))
    except Exception as e:  # noqa: BLE001
        log.warning("cb curves: BoE OIS latest failed: %s", e)
    cs.curves = {d: grid_forward_curve(p) for d, p in curves.items() if d >= start}
    cs.status = "ok" if cs.curves else "no data"
    return cs


# =============================================================================================== EA

def parse_ecb_nss(text: str) -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        v = to_float(r.get("OBS_VALUE"))
        try:
            d = date.fromisoformat(r.get("TIME_PERIOD", "")[:10])
        except ValueError:
            continue
        if v is not None:
            out.setdefault(d, {})[r.get("DATA_TYPE_FM", "")] = v
    return {d: p for d, p in out.items() if len(p) == 6}


def load_ea(s: Settings, start: date) -> CurveSet:
    cs = CurveSet("ecb", "ECB euro area AAA government yield curve (Svensson parameters, ECB Data Portal YC)",
                  "Instantaneous forwards from the ECB's published Svensson parameters (AAA-rated euro area central "
                  "government bonds); implied deposit facility rate = average forward per inter-meeting period minus "
                  "the stub basis (anchored to today's DFR).",
                  COMMON_CAVEATS + ["Government (AAA) curve, not an EUR STR OIS curve: collateral scarcity / "
                                    "convenience and credit premia move the basis; the Svensson fit starts at 3M so "
                                    "the stub is extrapolated by the model."],
                  effective_lag_days=6)
    raw = http_get("ecb", ECB_YC_URL, {"format": "csvdata", "detail": "dataonly", "startPeriod": start.isoformat()},
                   ttl=6 * 3600, settings=s)
    cs.curves = {d: svensson_curve(p) for d, p in parse_ecb_nss(raw.decode("utf-8", errors="replace")).items()}
    cs.status = "ok" if cs.curves else "no data"
    return cs


# =============================================================================================== JP

def load_jp(s: Settings, start: date) -> CurveSet:
    from app.ingestion.macro_national import parse_mof_csv  # noqa: PLC0415

    cs = CurveSet("boj", "Ministry of Finance JGB benchmark par yields 1Y-10Y",
                  "JGB par yields bootstrapped (1Y treated as zero, 2-10Y semiannual par), anchored at the BoJ "
                  "uncollateralized overnight call rate (t = 1 day); no official bill curve -> only 12m/24m horizons "
                  "(meeting-by-meeting path not resolvable).",
                  COMMON_CAVEATS + ["JGB yields are affected by BoJ purchases/yield-curve policy legacy and include "
                                    "term premia; between the overnight anchor and 1Y the "
                                    "forward curve is interpolated (monotone cubic)."], meetings_ok=False, horizons=(("12m", 12), ("24m", 24)))
    data: dict[str, dict[date, float]] = {}
    for url, ttl in ((f"{MOF_BASE}/historical/jgbcme_all.csv", 3 * 86400), (f"{MOF_BASE}/jgbcme.csv", 6 * 3600)):
        try:
            for k, rows in parse_mof_csv(http_get("mof_jp", url, ttl=ttl, settings=s)).items():
                data.setdefault(k, {}).update({d: v for d, v in rows if v is not None and d >= start})
        except Exception as e:  # noqa: BLE001
            log.warning("cb curves: MoF %s failed: %s", url, e)
    data["ON"] = dict(lake_series(["boj:FM01.STRDCLUCON"], s, start - timedelta(days=10)).get("boj:FM01.STRDCLUCON", []))
    panel = _panel({k: sorted(v.items()) for k, v in data.items() if k in TENOR_Y or k == "ON"}, start,
                   ffill={"ON": 7})
    cs.curves = _bootstrap_panel(panel, {"ON": "simple", "1Y": "semi"}, ["2Y", "3Y", "5Y", "7Y", "10Y"], 2, 1, 2)
    cs.status = "ok" if cs.curves else "no data"
    return cs


# =============================================================================================== CA

def load_ca(s: Settings, start: date) -> CurveSet:
    from .rates import parse_valet  # noqa: PLC0415

    cs = CurveSet("boc", "Bank of Canada Valet: Treasury bill yields 1M-1Y + benchmark bond yields 2Y/3Y/5Y",
                  "T-bills as simple 365-day zero yields, benchmark bonds as semiannual par yields, bootstrapped; "
                  "implied target overnight rate = average forward per inter-meeting period minus the stub basis.",
                  COMMON_CAVEATS + ["Benchmark bond yields are for specific issues (coupon/roll effects).",
                                    "Valet T-bill yields are weekly (Wednesday); carried forward up to 7 days."])
    codes = {"V80691342": "1M", "V80691344": "3M", "V80691345": "6M", "V80691346": "1Y",
             "BD.CDN.2YR.DQ.YLD": "2Y", "BD.CDN.3YR.DQ.YLD": "3Y", "BD.CDN.5YR.DQ.YLD": "5Y"}
    j = json.loads(http_get("boc", VALET_URL.format(codes=",".join(codes)), {"start_date": start.isoformat()}, settings=s))
    panel = _panel({codes[k]: v for k, v in parse_valet(j).items() if k in codes}, start,
                   ffill={"1M": 7, "3M": 7, "6M": 7, "1Y": 7})
    cs.curves = _bootstrap_panel(panel, {"1M": "simple", "3M": "simple", "6M": "simple", "1Y": "simple"},
                                 ["2Y", "3Y", "5Y"], 2, 2, 2)
    cs.status = "ok" if cs.curves else "no data"
    return cs


# =============================================================================================== AU

def load_au(s: Settings, start: date) -> CurveSet:
    from app.ingestion.macro_national import parse_rba_csv  # noqa: PLC0415

    cs = CurveSet("rba", "RBA F1 bank-bill rates (1M/3M/6M) + F2 Commonwealth Government bond yields (2Y/3Y/5Y)",
                  "Bank bills as simple zero yields, CGS as semiannual par yields, bootstrapped; implied cash rate = "
                  "average forward per inter-meeting period minus the stub basis.",
                  COMMON_CAVEATS + ["Bank-bill rates include a bank credit/term premium (RBA OIS series were "
                                    "discontinued in Dec 2022); the basis absorbs its current level only."])
    data: dict[str, list[tuple[date, float]]] = {}
    for t, codes in (("f1", {"FIRMMBAB30D": "1M", "FIRMMBAB90D": "3M", "FIRMMBAB180D": "6M"}),
                     ("f2", {"FCMYGBAG2D": "2Y", "FCMYGBAG3D": "3Y", "FCMYGBAG5D": "5Y"})):
        raw = http_get("rba", RBA_URL.format(t=t), ttl=6 * 3600, settings=s).decode("utf-8", errors="replace")
        for k, v in parse_rba_csv(raw).items():
            if k in codes:
                data[codes[k]] = v
    panel = _panel(data, start)
    cs.curves = _bootstrap_panel(panel, {"1M": "simple", "3M": "simple", "6M": "simple"}, ["2Y", "3Y", "5Y"], 2, 2, 2)
    cs.status = "ok" if cs.curves else "no data"
    return cs


# =============================================================================================== CH

def parse_snb_spot(text: str) -> dict[date, list[tuple[float, float]]]:
    out: dict[date, list[tuple[float, float]]] = {}
    for line in text.lstrip("﻿").splitlines():
        parts = [p.strip().strip('"') for p in line.split(";")]
        if len(parts) != 4 or parts[1] != "CHF" or not parts[2].endswith("J"):
            continue
        try:
            d = date.fromisoformat(parts[0])
            t = float(parts[2][:-1])
        except ValueError:
            continue
        v = to_float(parts[3])
        if v is not None and t <= 10:
            out.setdefault(d, []).append((t, v))
    return out


def load_ch(s: Settings, start: date) -> CurveSet:
    cs = CurveSet("snb", "SNB Confederation bond spot (zero) yields 1Y-10Y (data.snb.ch cube rendeiduebd)",
                  "Official zero-coupon spot rates (annual compounding) -> DF knots, anchored at the SNB policy "
                  "rate (BIS daily compilation) at t = 1 day; only 12m/24m horizons.",
                  COMMON_CAVEATS + ["Published monthly with up to ~1 month lag; first knot at 1Y (no official "
                                    "money-market curve in the window); forwards inside the first year are "
                                    "interpolated between the policy-rate anchor and 1Y."],
                  meetings_ok=False, horizons=(("12m", 12), ("24m", 24)))
    raw = http_get("snb", SNB_URL, {"fromDate": start.isoformat()}, ttl=12 * 3600, settings=s)
    pts = parse_snb_spot(raw.decode("utf-8", errors="replace"))
    on = lake_series(["bis:WS_CBPOL.D.CH"], s, start - timedelta(days=400)).get("bis:WS_CBPOL.D.CH", [])
    from .common import step_value  # noqa: PLC0415

    cs.curves = {}
    for d, p in pts.items():
        if len(p) < 3:
            continue
        zp = [(t, y, "annual") for t, y in p]
        o = step_value(on, d)
        if o is not None:
            zp.append((TENOR_Y["ON"], o, "simple"))
        cs.curves[d] = KnotCurve(bootstrap_par(zp, []))
    cs.status = "ok" if cs.curves else "no data"
    return cs


# =============================================================================================== SE / NO

def load_se(s: Settings, start: date) -> CurveSet:
    from .rates import parse_riksbank  # noqa: PLC0415

    cs = CurveSet("riksbank", "Riksbank SWEA: Swedish Treasury bills 3M/6M + government bonds 2Y/5Y",
                  "T-bills as simple zero yields, government bonds as annual par yields, bootstrapped.",
                  COMMON_CAVEATS + ["Sparse curve (4 points): meetings beyond 6 months are interpolated."])
    data = {}
    for code, ten in (("SETB3MBENCH", "3M"), ("SETB6MBENCH", "6M"), ("SEGVB2YC", "2Y"), ("SEGVB5YC", "5Y")):
        try:
            data[ten] = parse_riksbank(json.loads(http_get("riksbank", RIKSBANK_URL.format(s=code, start=start.isoformat()),
                                                           ttl=6 * 3600, settings=s)))
        except Exception as e:  # noqa: BLE001
            log.warning("cb curves: Riksbank %s failed: %s", code, e)
    cs.curves = _bootstrap_panel(_panel(data, start), {"3M": "simple", "6M": "simple"}, ["2Y", "5Y"], 1, 2, 1)
    cs.status = "ok" if cs.curves else "no data"
    return cs


def load_no(s: Settings, start: date) -> CurveSet:
    cs = CurveSet("norges", "Norges Bank GOVT_GENERIC_RATES: Treasury bills 3M/6M/12M + government bonds 3Y/5Y",
                  "T-bills as simple zero yields, government bonds as annual par yields, bootstrapped.",
                  COMMON_CAVEATS + ["Generic (interpolated) government rates computed by Norges Bank."])
    raw = http_get("norges", NORGES_URL.format(tenors="3M+6M+12M+3Y+5Y", start=start.isoformat()), ttl=6 * 3600,
                   settings=s).decode("utf-8", errors="replace")
    data: dict[str, list[tuple[date, float]]] = {}
    for r in csv.DictReader(io.StringIO(raw.lstrip("﻿")), delimiter=";"):
        v = to_float(r.get("OBS_VALUE"))
        try:
            d = date.fromisoformat((r.get("TIME_PERIOD") or "")[:10])
        except ValueError:
            continue
        if v is not None:
            data.setdefault(r.get("TENOR", ""), []).append((d, v))
    cs.curves = _bootstrap_panel(_panel(data, start), {"3M": "simple", "6M": "simple", "12M": "simple"},
                                 ["3Y", "5Y"], 1, 2, 1)
    cs.status = "ok" if cs.curves else "no data"
    return cs


LOADERS = {"fed": load_us, "boe": load_uk, "ecb": load_ea, "boj": load_jp, "boc": load_ca, "rba": load_au,
           "snb": load_ch, "riksbank": load_se, "norges": load_no}

NOT_AVAILABLE = {
    "bcb": "No official machine-readable short-end curve (DI futures/ANBIMA curves are commercial).",
    "banxico": "Banxico SIE (CETES/TIIE curves) requires an API token; not configured.",
    "bcch": "BCCh BDE API requires credentials; not configured.",
    "banrep": "No official forward/zero curve with short-end coverage via an open API.",
    "bcrp": "No official short-end curve via an open API.",
    "rbi": "No keyless official T-bill/OIS curve API.",
    "bok": "BoK ECOS requires an API key; not configured.",
    "pboc": "Official ChinaBond/CFETS curves are not available via an open official API.",
}


def load(bank: str, s: Settings, start: date) -> CurveSet | None:
    fn = LOADERS.get(bank)
    return fn(s, start) if fn else None


_ = timedelta
