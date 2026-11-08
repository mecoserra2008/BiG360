"""FX rates -> fx_rates (USD per unit, daily). Job `fx_rates`.

Source of truth (docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §6): ECB euro reference rates, full history zip
(eurofxref-hist.zip, 1999->, ~30 currencies, ~16:00 CET each TARGET day). Crosses via EUR:
    usd_per_unit(C) = USD_per_EUR / C_per_EUR ; usd_per_unit(EUR) = USD_per_EUR ; USD = 1.0
Gap fillers (currencies the ECB does not publish), lower precedence:
    TWD  FRED DEXTAUS (H.10, ~1 week lag)                       source_id=fred
    (FRED H.10 DEX* series also supply 1971-1998 history for ~20 majors before the ECB set starts)
    CLP  mindicador.cl dolar observado (BCCh relay)              source_id=central_bank
    ARS  BCRA estadisticascambiarias v1.0 USD                     source_id=central_bank
    COP  datos.gov.co TRM (32sa-8pi3, validity ranges expanded)  source_id=central_bank
    PEN  BCRP PD04640PD (SBS venta)                               source_id=central_bank
Precedence per (date, currency): ecb > fred > central_bank > yfinance; then each currency is forward-filled over
calendar gaps of at most 5 days (source_id=filled) so weekend/holiday lookups resolve.

Helpers for consumers: `load_rates`, `cross_rate` (any pair incl. EUR crosses), `daily_rate_matrix` (inputs for
app.normalize.fx.period_average).
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
from app.core.storage import scan_table, write_table
from app.normalize.prices import _table_has_data

log = get_logger(__name__)

ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
ECB_DAILY_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
PRECEDENCE = {"ecb": 0, "fred": 1, "central_bank": 2, "yfinance": 3, "polygon": 4, "filled": 9}
MAX_FILL_DAYS = 5

#: FRED H.10 daily noon buying rates: currency -> (series id, True if quoted as units per USD else USD per unit)
FRED_H10: dict[str, tuple[str, bool]] = {
    "TWD": ("DEXTAUS", True), "JPY": ("DEXJPUS", True), "GBP": ("DEXUSUK", False), "EUR": ("DEXUSEU", False),
    "CHF": ("DEXSZUS", True), "CAD": ("DEXCAUS", True), "KRW": ("DEXKOUS", True), "HKD": ("DEXHKUS", True),
    "CNY": ("DEXCHUS", True), "BRL": ("DEXBZUS", True), "MXN": ("DEXMXUS", True), "INR": ("DEXINUS", True),
    "SGD": ("DEXSIUS", True), "AUD": ("DEXUSAL", False), "NZD": ("DEXUSNZ", False), "SEK": ("DEXSDUS", True),
    "NOK": ("DEXNOUS", True), "DKK": ("DEXDNUS", True), "ZAR": ("DEXSFUS", True), "THB": ("DEXTHUS", True),
    "MYR": ("DEXMAUS", True),
}

_SPANISH_MONTHS = {"Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6, "Jul": 7, "Ago": 8, "Set": 9, "Sep": 9,
                   "Oct": 10, "Nov": 11, "Dic": 12}


# ------------------------------------------------------------------------------------------------ parsers (pure)


def parse_ecb_hist_csv(text: str) -> pl.DataFrame:
    """eurofxref-hist.csv -> long [date, currency, per_eur] (N/A dropped)."""
    df = pl.read_csv(io.StringIO(text), infer_schema_length=0, null_values=["N/A", ""])
    df = df.select([c for c in df.columns if c and not c.startswith("_")])
    long = df.unpivot(index="Date", variable_name="currency", value_name="per_eur").drop_nulls("per_eur")
    return long.select(pl.col("Date").str.to_date().alias("date"), pl.col("currency").str.strip_chars(),
                       pl.col("per_eur").cast(pl.Float64)).filter(pl.col("per_eur") > 0)


def ecb_to_usd(long: pl.DataFrame) -> pl.DataFrame:
    """[date, currency, per_eur] -> fx rows (usd_per_unit) incl. EUR and USD."""
    usd = long.filter(pl.col("currency") == "USD").select("date", pl.col("per_eur").alias("usd_per_eur"))
    j = long.filter(pl.col("currency") != "USD").join(usd, on="date", how="inner")
    rows = j.select("date", "currency", (pl.col("usd_per_eur") / pl.col("per_eur")).alias("usd_per_unit"))
    eur = usd.select("date", pl.lit("EUR").alias("currency"), pl.col("usd_per_eur").alias("usd_per_unit"))
    one = usd.select("date", pl.lit("USD").alias("currency"), pl.lit(1.0).alias("usd_per_unit"))
    return pl.concat([rows, eur, one]).with_columns(pl.lit("ecb").alias("source_id"))


def parse_mindicador(payload: dict[str, Any]) -> pl.DataFrame:
    rows = [(date.fromisoformat(s["fecha"][:10]), float(s["valor"])) for s in payload.get("serie", []) if s.get("valor")]
    return _units_per_usd_rows(rows, "CLP")


def parse_bcra(payload: dict[str, Any]) -> pl.DataFrame:
    rows = []
    for r in payload.get("results", []):
        for d in r.get("detalle", []):
            if d.get("codigoMoneda") == "USD" and d.get("tipoCotizacion"):
                rows.append((date.fromisoformat(r["fecha"][:10]), float(d["tipoCotizacion"])))
    return _units_per_usd_rows(rows, "ARS")


def parse_trm(payload: list[dict[str, Any]], until: date | None = None) -> pl.DataFrame:
    """TRM rows have validity ranges [vigenciadesde, vigenciahasta]; expand to calendar days."""
    rows = []
    until = until or datetime.now(UTC).date()
    for r in payload:
        try:
            v = float(r["valor"])
            d0 = date.fromisoformat(r["vigenciadesde"][:10])
            d1 = date.fromisoformat(r.get("vigenciahasta", r["vigenciadesde"])[:10])
        except (KeyError, ValueError):
            continue
        d = d0
        while d <= min(d1, until):
            rows.append((d, v))
            d += timedelta(days=1)
    return _units_per_usd_rows(rows, "COP")


def parse_bcrp(payload: dict[str, Any]) -> pl.DataFrame:
    rows = []
    for p in payload.get("periods", []):
        try:
            dd, mon, yy = p["name"].split(".")
            v = p["values"][0]
            if v in ("n.d.", "", None):
                continue
            rows.append((date(2000 + int(yy) if int(yy) < 80 else 1900 + int(yy), _SPANISH_MONTHS[mon], int(dd)), float(v)))
        except (KeyError, ValueError, IndexError):
            continue
    return _units_per_usd_rows(rows, "PEN")


def parse_fred_units_per_usd(payload: dict[str, Any], currency: str, invert: bool = True) -> pl.DataFrame:
    rows = [(date.fromisoformat(o["date"]), float(o["value"])) for o in payload.get("observations", [])
            if o.get("value") not in (".", None, "")]
    if invert:
        df = _units_per_usd_rows(rows, currency)
    else:
        df = pl.DataFrame({"date": [r[0] for r in rows], "currency": [currency] * len(rows),
                           "usd_per_unit": [r[1] for r in rows]},
                          schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64})
    return df.with_columns(pl.lit("fred").alias("source_id"))


def _units_per_usd_rows(rows: list[tuple[date, float]], currency: str) -> pl.DataFrame:
    rows = [(d, v) for d, v in rows if v and v > 0]
    return pl.DataFrame({"date": [r[0] for r in rows], "currency": [currency] * len(rows),
                         "usd_per_unit": [1.0 / r[1] for r in rows]},
                        schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64}
                        ).with_columns(pl.lit("central_bank").alias("source_id"))


def combine_and_fill(frames: list[pl.DataFrame], max_fill_days: int = MAX_FILL_DAYS,
                     until: date | None = None) -> pl.DataFrame:
    """Precedence merge + forward fill of calendar gaps <= max_fill_days. Returns fx_rates-shaped rows."""
    df = pl.concat([f.select("date", "currency", "usd_per_unit", "source_id") for f in frames if f.height])
    df = df.with_columns(pl.col("source_id").replace_strict(PRECEDENCE, default=8).alias("__p")).sort("__p") \
        .unique(subset=["date", "currency"], keep="first").drop("__p")
    until = until or datetime.now(UTC).date()
    df = df.filter(pl.col("date") <= until)
    filled = []
    for (ccy,), g in df.group_by("currency"):
        g = g.sort("date")
        cal = pl.DataFrame({"date": pl.date_range(g["date"].min(), min(until, g["date"].max() + timedelta(days=max_fill_days)),
                                                  "1d", eager=True)})
        x = cal.join(g, on="date", how="left").with_columns(pl.col("date").alias("__obs"))
        x = x.with_columns(pl.when(pl.col("usd_per_unit").is_null()).then(None).otherwise(pl.col("__obs")).alias("__obs"))
        x = x.with_columns(pl.col("__obs").forward_fill(), pl.col("usd_per_unit").forward_fill())
        gap = (pl.col("date") - pl.col("__obs")).dt.total_days()
        x = x.filter(pl.col("source_id").is_not_null() | (gap <= max_fill_days))
        x = x.with_columns(pl.col("source_id").fill_null("filled"), pl.lit(ccy).alias("currency"))
        filled.append(x.select("date", "currency", "usd_per_unit", "source_id"))
    out = pl.concat(filled) if filled else df
    return out.with_columns(
        pl.col("date").dt.year().cast(pl.Int16).alias("year"),
        (1.0 / pl.col("usd_per_unit")).alias("units_per_usd"),
        pl.lit(datetime.now(UTC)).alias("ingested_at"),
    ).select("date", "year", "currency", "usd_per_unit", "units_per_usd", "source_id", "ingested_at")


# ------------------------------------------------------------------------------------------------ fetch


class FxFetcher:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.ecb = HttpClient("ecb", settings=self.settings)
        self.cb = HttpClient("fx_central_banks", settings=self.settings, max_retries=2,
                             headers={"User-Agent": "Mozilla/5.0 (BiG Equity 360 research)"})
        self.fred = HttpClient("fred", settings=self.settings)
        self.raw_root = self.settings.raw_dir / "ecb" / "fx"
        self.errors: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _land(self, name: str, content: bytes) -> None:
        d = self.raw_root / f"dt={datetime.now(UTC).date().isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(content)

    def ecb_history(self) -> pl.DataFrame:
        r = self.ecb.get(ECB_HIST_URL, cache_ttl=3600)
        self._land("eurofxref-hist.zip", r.content)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        txt = z.read(z.namelist()[0]).decode("utf-8")
        return ecb_to_usd(parse_ecb_hist_csv(txt))

    def gap_fillers(self, since: date) -> list[pl.DataFrame]:
        out: list[pl.DataFrame] = []
        today = datetime.now(UTC).date()
        key = self.settings.secret("fred_apikey")
        jobs: list[tuple[str, Any]] = []
        if key:
            # FRED H.10: TWD gap filler + pre-1999 history (1971->) for the majors; lower precedence than the ECB
            fred_since = since if since > date(1999, 1, 1) else date(1971, 1, 1)
            for ccy, (sid, units_per_usd) in FRED_H10.items():
                jobs.append((f"fred:{ccy}", lambda sid=sid, ccy=ccy, inv=units_per_usd: parse_fred_units_per_usd(
                    self.fred.get("https://api.stlouisfed.org/fred/series/observations",
                                  {"series_id": sid, "api_key": key, "file_type": "json",
                                   "observation_start": fred_since.isoformat()}, cache_ttl=3600).json(), ccy, inv)))

        def clp() -> pl.DataFrame:
            fr = []
            for y in range(max(since.year, 1999), today.year + 1):
                fr.append(parse_mindicador(self.cb.get(f"https://mindicador.cl/api/dolar/{y}", cache_ttl=3600 if y == today.year else -1).json()))
            return pl.concat(fr)

        def ars() -> pl.DataFrame:
            fr = []
            y0 = max(since.year, 2003)
            for y in range(y0, today.year + 1):
                d0 = max(date(y, 1, 1), since)
                d1 = min(date(y, 12, 31), today)
                fr.append(parse_bcra(self.cb.get(
                    "https://api.bcra.gob.ar/estadisticascambiarias/v1.0/Cotizaciones/USD",
                    {"fechadesde": d0.isoformat(), "fechahasta": d1.isoformat(), "limit": 1000},
                    cache_ttl=3600 if y == today.year else -1).json()))
            return pl.concat(fr)

        def cop() -> pl.DataFrame:
            return parse_trm(self.cb.get("https://www.datos.gov.co/resource/32sa-8pi3.json",
                                         {"$limit": 50000, "$order": "vigenciadesde DESC",
                                          "$where": f"vigenciahasta >= '{since.isoformat()}T00:00:00'"},
                                         cache_ttl=3600).json())

        def pen() -> pl.DataFrame:
            return parse_bcrp(self.cb.get(
                f"https://estadisticas.bcrp.gob.pe/estadisticas/series/api/PD04640PD/json/{since.isoformat()}/{today.isoformat()}",
                cache_ttl=3600).json())

        jobs += [("CLP", clp), ("ARS", ars), ("COP", cop), ("PEN", pen)]
        for ccy, fn in jobs:
            try:
                df = fn()
                out.append(df)
                self.counts[ccy] = df.height
            except Exception as e:  # noqa: BLE001 - one filler failing must not block ECB
                self.errors[ccy] = f"{type(e).__name__}: {e}"[:200]
                log.warning("fx gap filler %s failed: %s", ccy, self.errors[ccy])
        return out


def build_fx(settings: Settings | None = None, full: bool = True, since: date | None = None) -> tuple[pl.DataFrame, dict]:
    f = FxFetcher(settings)
    ecb = f.ecb_history()
    f.counts["ecb"] = ecb.height
    since = since or (date(1999, 1, 1) if full else datetime.now(UTC).date() - timedelta(days=60))
    frames = [ecb, *f.gap_fillers(since)]
    if not full:
        frames = [x.filter(pl.col("date") >= since - timedelta(days=10)) for x in frames]
    out = combine_and_fill(frames)
    meta = {"counts": f.counts, "errors": f.errors, "rows": out.height,
            "currencies": out.get_column("currency").n_unique(),
            "by_source": dict(out.group_by("source_id").len().iter_rows())}
    return out, meta


# ------------------------------------------------------------------------------------------------ consumer helpers


def load_rates(currencies: list[str] | None = None, start: date | None = None, end: date | None = None,
               settings: Settings | None = None) -> pl.DataFrame:
    if not _table_has_data("fx_rates", settings):
        return pl.DataFrame(schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64})
    lf = scan_table("fx_rates", settings).select("date", "currency", "usd_per_unit", "source_id")
    if currencies:
        lf = lf.filter(pl.col("currency").is_in(currencies))
    if start:
        lf = lf.filter(pl.col("date") >= start)
    if end:
        lf = lf.filter(pl.col("date") <= end)
    return lf.collect().sort("currency", "date")


def cross_rate(base: str, quote: str, on: date, settings: Settings | None = None) -> float | None:
    """Units of `quote` per 1 unit of `base` on/before `on` (<= 5 days stale), e.g. cross_rate('EUR','JPY', d)."""
    if base == quote:
        return 1.0
    df = load_rates([base, quote], on - timedelta(days=MAX_FILL_DAYS + 1), on, settings)
    vals = {}
    for c in (base, quote):
        if c == "USD":
            vals[c] = 1.0
            continue
        s = df.filter(pl.col("currency") == c)
        if s.is_empty():
            return None
        vals[c] = s.sort("date").get_column("usd_per_unit")[-1]
    return vals[base] / vals[quote]


def daily_rate_matrix(currencies: list[str], start: date, end: date, vs: str = "USD",
                      settings: Settings | None = None) -> pl.DataFrame:
    """Wide [date, <ccy>...] of units-of-`vs` per unit of each currency (vs='USD' or 'EUR'); the arithmetic mean of a
    column over [period_start, period_end] is the flow-item period average (ARCHITECTURE §8)."""
    need = list(set(currencies) | ({vs} if vs != "USD" else set()))
    df = load_rates(need, start, end, settings)
    if vs != "USD":
        base = df.filter(pl.col("currency") == vs).select("date", pl.col("usd_per_unit").alias("__vs"))
        df = df.join(base, on="date", how="inner").with_columns((pl.col("usd_per_unit") / pl.col("__vs")).alias("usd_per_unit"))
    return df.filter(pl.col("currency").is_in(currencies)).pivot(on="currency", index="date", values="usd_per_unit").sort("date")


# ------------------------------------------------------------------------------------------------ job


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    full = bool(p.get("full", not _table_has_data("fx_rates", ctx.settings)))
    out, meta = build_fx(ctx.settings, full=full)
    n = write_table(out, "fx_rates", settings=ctx.settings)
    status = "partial" if meta["errors"] else "success"
    return JobResult(status=status, rows_written={"fx_rates": n}, entities_ok=meta["currencies"],
                     entities_failed=len(meta["errors"]), watermark=str(out.get_column("date").max()),
                     message=f"by_source={meta['by_source']} errors={meta['errors']}")
