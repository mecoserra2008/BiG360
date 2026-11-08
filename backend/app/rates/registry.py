"""Curve catalogue (W3): every curve stored in `yield_curves`, its official source, measures, tenors, history start and
publication rule. Pure data; the startup-sync / gap-detector rule `curve_dates` reads the publication rules from
contracts/coverage.yaml (dataset rates.curves, entities) and falls back to `Curve.publish` here.

Vocabulary
  curve_id  stable id, '{CC}_{NAME}' (US_TSY, EA_AAA, DE_BUND, GB_GILT, ...); monthly long-term benchmark yields of
            the rest of the world are '{CC}_LT10' (one tenor, 120 months).
  type      measure: par (par / constant-maturity yield of a fitted or interpolated curve), spot (zero-coupon,
            continuously compounded unless the source says otherwise), forward (instantaneous forward), yield (a
            benchmark / market average yield that is not a fitted curve point)
  kind      nominal | real (inflation-linked) | inflation (implied inflation, BoE) | ois | money_market
  tenor     months (int16); overnight = 0
Rates are stored as integer basis points x 100 (4.2512 % -> 42512).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Publish:
    """Data for business day D is public at `at` local time `lag` business days of `cal` after D.
    freq 'M' curves (monthly averages / end-of-month) publish `lag_days` calendar days after the month end."""
    cal: str = "weekdays"              # exchange MIC | weekdays | target
    tz: str = "UTC"
    at: str = "18:00"
    lag: int = 0
    freq: str = "D"
    lag_days: int = 0                  # freq M: days after month end; batch: days after the month end of the data


@dataclass(frozen=True)
class Curve:
    curve_id: str
    country: str                       # ISO2 / EA
    label: str
    kind: str
    types: tuple[str, ...]
    source_id: str
    source: str
    frequency: str                     # D | M
    history_start: str
    publish: Publish = field(default_factory=Publish)
    tenors: tuple[int, ...] = ()       # stored tenors (months)
    licence: str = "Official source; attribution"
    notes: str = ""
    benchmark: bool = True             # shown in the multi-country monitor
    group: str = "sovereign"           # sovereign | money_market | lt10

    def public(self) -> dict[str, Any]:
        return {"curve_id": self.curve_id, "country": self.country, "label": self.label, "kind": self.kind,
                "types": list(self.types), "source_id": self.source_id, "source": self.source,
                "frequency": self.frequency, "history_start": self.history_start, "tenors": list(self.tenors),
                "licence": self.licence, "notes": self.notes, "group": self.group,
                "publish": {"cal": self.publish.cal, "tz": self.publish.tz, "at": self.publish.at,
                            "lag": self.publish.lag, "freq": self.publish.freq, "lag_days": self.publish.lag_days}}


#: standard grid for fitted (Svensson / spline) curves, months
FIT_GRID: tuple[int, ...] = (3, 6, 9, 12, 18, 24, 36, 48, 60, 72, 84, 96, 108, 120, 144, 180, 240, 300, 360)
BOE_GRID: tuple[int, ...] = (1, 3, 6, 9, 12, 18, 24, 36, 48, 60, 72, 84, 96, 108, 120, 144, 180, 240, 300, 360, 480)
UST_TENORS: tuple[int, ...] = (1, 2, 3, 4, 6, 12, 24, 36, 60, 84, 120, 240, 360)

NY = "America/New_York"
_US = Publish("XNYS", NY, "18:00", 0)
_ECB = Publish("target", "Europe/Berlin", "12:30", 1)
_BUBA = Publish("XETR", "Europe/Berlin", "13:00", 1)
_BOE = Publish("XLON", "Europe/London", "12:30", 1)

CURVES: tuple[Curve, ...] = (
    Curve("US_TSY", "US", "US Treasury par yield curve", "nominal", ("par",), "ust",
          "US Treasury daily par yield curve rates (home.treasury.gov XML)", "D", "1990-01-02", _US, UST_TENORS,
          notes="Bills (1-6M) are bond-equivalent yields; the 6-week bill (1.5M) is not stored (integer months)."),
    Curve("US_TSY_REAL", "US", "US Treasury real (TIPS) par yield curve", "real", ("par",), "ust",
          "US Treasury daily real yield curve rates (home.treasury.gov XML)", "D", "2003-01-02", _US, (60, 84, 120, 240, 360)),
    Curve("US_H15", "US", "Fed H.15 Treasury constant maturities", "nominal", ("par",), "fred",
          "Federal Reserve H.15 Selected Interest Rates (constant maturity; via FRED DGS*)", "D", "1962-01-02",
          Publish("XNYS", NY, "10:00", 1), (1, 3, 6, 12, 24, 36, 60, 84, 120, 240, 360), benchmark=False,
          notes="Deep history (1962-); needs job macro_fred (FRED series DGS1MO..DGS30)."),
    Curve("US_H15_TIPS", "US", "Fed H.15 TIPS constant maturities", "real", ("par",), "fred",
          "Federal Reserve H.15 inflation-indexed constant maturities (via FRED DFII*)", "D", "2003-01-02",
          Publish("XNYS", NY, "10:00", 1), (60, 84, 120, 240, 360), benchmark=False),
    Curve("EA_AAA", "EA", "Euro area AAA government curve (ECB)", "nominal", ("spot", "par", "forward"), "ecb",
          "ECB euro area yield curves, AAA-rated central government bonds (Svensson parameters, ECB Data Portal YC)",
          "D", "2004-09-06", _ECB, FIT_GRID,
          notes="Reconstructed from the published Svensson parameters (spot continuously compounded; par = "
                "continuous-coupon par yield, matches ECB PY_*)."),
    Curve("EA_ALL", "EA", "Euro area all-issuer government curve (ECB)", "nominal", ("spot", "par", "forward"), "ecb",
          "ECB euro area yield curves, all euro area central government bonds (Svensson parameters)", "D",
          "2004-09-06", _ECB, FIT_GRID),
    Curve("DE_BUND", "DE", "German Bund curve (Bundesbank Svensson)", "nominal", ("spot", "par", "forward"),
          "bundesbank", "Deutsche Bundesbank term structure of listed Federal securities (Svensson parameters, BBSIS)",
          "D", "1972-09-01", _BUBA, FIT_GRID,
          notes="Full curve reconstructed from the daily Svensson parameters; before Aug-1997 end-of-month only."),
    Curve("GB_GILT", "GB", "UK nominal gilt curve (BoE)", "nominal", ("spot", "forward"), "boe",
          "Bank of England yield curve archive: nominal government liability curve (Anderson-Sleath spline)", "D",
          "1979-01-02", _BOE, BOE_GRID),
    Curve("GB_GILT_REAL", "GB", "UK real (index-linked) gilt curve (BoE)", "real", ("spot", "forward"), "boe",
          "Bank of England yield curve archive: real government liability curve", "D", "1985-01-02", _BOE, BOE_GRID,
          benchmark=False),
    Curve("GB_INFLATION", "GB", "UK implied RPI inflation curve (BoE)", "inflation", ("spot", "forward"), "boe",
          "Bank of England yield curve archive: implied inflation (RPI) curve", "D", "1985-01-02", _BOE, BOE_GRID,
          benchmark=False),
    Curve("GB_OIS", "GB", "UK SONIA OIS curve (BoE)", "ois", ("spot", "forward"), "boe",
          "Bank of England yield curve archive: OIS (SONIA) curve", "D", "2009-01-02", _BOE, BOE_GRID, benchmark=False),
    Curve("JP_JGB", "JP", "Japan JGB benchmark yields (MoF)", "nominal", ("par",), "mof_jp",
          "Ministry of Finance Japan, interest rates on JGBs (jgbcme_all.csv)", "D", "1974-09-24",
          Publish("XTKS", "Asia/Tokyo", "19:00", 0),
          (12, 24, 36, 48, 60, 72, 84, 96, 108, 120, 180, 240, 300, 360, 480)),
    Curve("ES_BONO", "ES", "Spain government bond yields (Banco de Espana)", "nominal", ("yield",), "bde",
          "Banco de Espana, secondary-market yields on Letras / Bonos / Obligaciones del Estado (BIE TI_1_3)", "D",
          "1987-07-01", Publish("XMAD", "Europe/Madrid", "12:00", 3), (6, 12, 36, 60, 120, 180),
          notes="Average market yields by residual-maturity bucket (BME / Banco de Espana), not a fitted curve."),
    Curve("PT_OT", "PT", "Portugal OT yields (Banco de Portugal / IGCP)", "nominal", ("yield",), "bpstat",
          "Banco de Portugal BPstat: yields on fixed-rate Treasury bonds (OT) by residual maturity", "D", "1993-01-04",
          Publish("XLIS", "Europe/Lisbon", "18:00", 6), (24, 36, 48, 60, 84, 120),
          notes="IGCP (Portuguese debt agency) OT benchmarks as compiled by Banco de Portugal."),
    Curve("CH_CONF", "CH", "Swiss Confederation spot curve (SNB)", "nominal", ("spot",), "snb",
          "Swiss National Bank, spot interest rates on Confederation bonds (data.snb.ch cube rendeiduebd)", "D",
          "1988-01-04", Publish("XSWX", "Europe/Zurich", "14:30", 0, "batch", 5),
          (12, 24, 36, 48, 60, 72, 84, 96, 108, 120, 240, 360),
          notes="Daily values published once a month (~1 month lag)."),
    Curve("CA_GOC", "CA", "Government of Canada yields (BoC)", "nominal", ("yield",), "boc",
          "Bank of Canada Valet: Treasury bill yields and benchmark bond yields", "D", "2001-01-02",
          Publish("XTSE", "America/Toronto", "12:00", 1), (1, 3, 6, 12, 24, 36, 60, 84, 120, 360)),
    Curve("AU_ACGB", "AU", "Australian Government bond yields (RBA)", "nominal", ("yield", "spot"), "rba",
          "Reserve Bank of Australia F2 (interpolated CGS yields) and F17 (zero-coupon analytical series)", "D",
          "1992-07-01", Publish("XASX", "Australia/Sydney", "17:00", 2), (3, 6, 12, 24, 36, 60, 84, 120, 180, 240)),
    Curve("SE_SGB", "SE", "Swedish government yields (Riksbank)", "nominal", ("yield",), "riksbank",
          "Sveriges Riksbank SWEA: Treasury bills and government bond yields", "D", "1987-01-02",
          Publish("XSTO", "Europe/Stockholm", "17:00", 0), (3, 6, 24, 60, 84, 120), benchmark=False),
    Curve("NO_NGB", "NO", "Norwegian government yields (Norges Bank)", "nominal", ("yield",), "norges",
          "Norges Bank GOVT_GENERIC_RATES (generic Treasury bill / bond rates)", "D", "1999-01-04",
          Publish("XOSL", "Europe/Oslo", "17:00", 1), (3, 6, 12, 36, 60, 120), benchmark=False),
    Curve("EA_EURIBOR", "EA", "EURIBOR (EMMI, via Banco de Espana)", "money_market", ("yield",), "bde",
          "EURIBOR 1M/3M/6M/12M as republished by Banco de Espana (BIE TI_1_7; source EMMI / Refinitiv)", "D",
          "1998-12-30", Publish("target", "Europe/Madrid", "12:00", 2), (1, 3, 6, 12), group="money_market",
          benchmark=False, licence="EMMI terms of use (https://www.emmi-benchmarks.eu/terms-of-use): personal use; "
                                   "a bank needs an EMMI licence"),
)

#: monthly long-term (~10y) government benchmark yields: ECB IRS (EU convergence yields) for EU members,
#: OECD (MEI/KEI IRLT, via the macro lake) for the rest
LT10_ECB = ("AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT",
            "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK")
LT10_OECD = {"AU": "AUS", "BR": "BRA", "CA": "CAN", "CH": "CHE", "CL": "CHL", "CN": "CHN", "CO": "COL", "GB": "GBR",
             "IN": "IND", "JP": "JPN", "KR": "KOR", "MX": "MEX", "US": "USA", "NZ": "NZL", "IL": "ISR", "IS": "ISL",
             "NO": "NOR", "ZA": "ZAF", "CR": "CRI", "ID": "IDN", "RU": "RUS", "TR": "TUR"}
COUNTRY_NAMES = {"AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany",
                 "DK": "Denmark", "EE": "Estonia", "ES": "Spain", "FI": "Finland", "FR": "France", "GR": "Greece",
                 "HR": "Croatia", "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LT": "Lithuania", "LU": "Luxembourg",
                 "LV": "Latvia", "MT": "Malta", "NL": "Netherlands", "PL": "Poland", "PT": "Portugal", "RO": "Romania",
                 "SE": "Sweden", "SI": "Slovenia", "SK": "Slovakia", "AU": "Australia", "BR": "Brazil", "CA": "Canada",
                 "CH": "Switzerland", "CL": "Chile", "CN": "China", "CO": "Colombia", "GB": "United Kingdom",
                 "IN": "India", "JP": "Japan", "KR": "Korea", "MX": "Mexico", "US": "United States",
                 "NZ": "New Zealand", "IL": "Israel", "IS": "Iceland", "NO": "Norway", "ZA": "South Africa",
                 "CR": "Costa Rica", "ID": "Indonesia", "RU": "Russia", "TR": "Turkey", "EA": "Euro area"}


def lt10_curves() -> tuple[Curve, ...]:
    out = []
    for cc in LT10_ECB:
        out.append(Curve(f"{cc}_LT10", cc, f"{COUNTRY_NAMES.get(cc, cc)} 10y government yield (monthly, ECB)",
                         "nominal", ("yield",), "ecb", "ECB Data Portal IRS: long-term interest rates for "
                         "convergence purposes (EU Maastricht criterion, ~10y government bonds, monthly average)",
                         "M", "1993-01-01", Publish("target", "Europe/Berlin", "15:00", 0, "M", 25), (120,),
                         benchmark=False, group="lt10"))
    for cc in LT10_OECD:
        if cc in LT10_ECB:
            continue
        out.append(Curve(f"{cc}_LT10", cc, f"{COUNTRY_NAMES.get(cc, cc)} 10y government yield (monthly, OECD)",
                         "nominal", ("yield",), "oecd", "OECD Main Economic Indicators / Key Economic Indicators: "
                         "long-term interest rates (IRLT, ~10y government bonds, monthly)", "M", "1960-01-01",
                         Publish("weekdays", "Europe/Paris", "12:00", 0, "M", 45), (120,), benchmark=False,
                         group="lt10"))
    return tuple(out)


ALL_CURVES: tuple[Curve, ...] = CURVES + lt10_curves()
BY_ID: dict[str, Curve] = {c.curve_id: c for c in ALL_CURVES}
#: per-source loader groups (one network pass per source)
SOURCES: tuple[str, ...] = ("ust", "fred", "ecb", "bundesbank", "boe", "mof_jp", "bde", "bpstat", "snb", "boc", "rba",
                            "riksbank", "norges", "ecb_irs", "oecd")
#: country -> its benchmark curve in the monitor (2s10s, spreads)
BENCHMARK_CURVE: dict[str, str] = {"US": "US_TSY", "DE": "DE_BUND", "EA": "EA_AAA", "GB": "GB_GILT", "JP": "JP_JGB",
                                   "ES": "ES_BONO", "PT": "PT_OT", "CH": "CH_CONF", "CA": "CA_GOC", "AU": "AU_ACGB",
                                   "SE": "SE_SGB", "NO": "NO_NGB"}
#: nominal / real pairs for breakevens (real vs nominal)
REAL_PAIRS: dict[str, tuple[str, str, str]] = {"US": ("US_TSY", "par", "US_TSY_REAL"),
                                               "GB": ("GB_GILT", "spot", "GB_GILT_REAL")}


def get(curve_id: str) -> Curve | None:
    return BY_ID.get(curve_id)


def by_source(source_id: str) -> list[Curve]:
    if source_id == "ecb_irs":
        return [c for c in ALL_CURVES if c.group == "lt10" and c.source_id == "ecb"]
    if source_id == "ecb":
        return [c for c in ALL_CURVES if c.source_id == "ecb" and c.group != "lt10"]
    return [c for c in ALL_CURVES if c.source_id == source_id]


#: benchmark measure per curve for the monitor (first available of the curve's types)
MONITOR_TYPE: dict[str, str] = {"US_TSY": "par", "DE_BUND": "par", "EA_AAA": "par", "GB_GILT": "spot", "JP_JGB": "par",
                                "ES_BONO": "yield", "PT_OT": "yield", "CH_CONF": "spot", "CA_GOC": "yield",
                                "AU_ACGB": "yield", "SE_SGB": "yield", "NO_NGB": "yield"}


def registry_rows() -> list[dict]:
    out = []
    for c in ALL_CURVES:
        p = c.public()
        out.append({"curve_id": c.curve_id, "country": c.country, "label": c.label, "kind": c.kind,
                    "types": ",".join(c.types), "source_id": c.source_id, "source": c.source,
                    "frequency": c.frequency, "history_start": c.history_start,
                    "tenors": ",".join(str(t) for t in c.tenors), "licence": c.licence, "notes": c.notes,
                    "grp": c.group, "benchmark": c.benchmark,
                    "monitor_type": MONITOR_TYPE.get(c.curve_id, c.types[0]),
                    "is_country_benchmark": BENCHMARK_CURVE.get(c.country) == c.curve_id,
                    "publish": f"{p['publish']['cal']} {p['publish']['at']} {p['publish']['tz']} +{p['publish']['lag']}"
                               + (f" ({p['publish']['freq']}, {p['publish']['lag_days']} d)" if p['publish']['freq'] != "D" else "")})
    return out
