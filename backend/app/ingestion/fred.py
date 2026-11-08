"""FRED / ALFRED -> macro_series + macro_observations. Job `macro_fred`.

* Recommended series set: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §5.2 (all verified alive 2026-10-10); dead/stale IDs
  of §5.3 are excluded (ISM NAPM, LBMA gold, TEDRATE, USSLIND, OECD MEI CPIs, ...).
* Point-in-time: ALFRED vintages (realtime_start=1776-07-04, realtime_end=9999-12-31) for the revised core series
  (GDP, payrolls, CPI, PCE, IP, retail sales, unemployment): knowledge_date = realtime_start (observed).
  All other series: knowledge_date = min(period_end + release_lag_days, retrieval date), knowledge_date_estimated=true.
* ICE BofA OAS series (BAML*): FRED keeps only 3 years since Apr-2026, so every fetch is also merged into an
  APPEND-ONLY local archive data/raw/fred/oas_archive/<ID>.parquet (first-seen values are never overwritten; later
  differing values are kept in last_value/last_seen). macro_observations is upserted (older rows are never deleted).
* 120 req/min documented; shared bucket "fred" at 1.5 req/s. api_key is a query param (excluded from cache keys and
  redacted in logs by app.core.http / logsetup).
* Catalog = CATALOG below + `src: fred` entries of contracts/macro_catalog.yaml (additions such as the full UST curve,
  OECD 10y/3m yields for CA/AU/CH/FR/IT/ES/PT/NL/SE/NO/CL/ZA, EM OAS; plus indicator/scale metadata for existing ids).
  Series not yet in macro_series are always fetched with full history (the incremental `start` only applies to known
  ids). Estimated-knowledge rows go through app.ingestion.macro_common.reconcile_observations (revision-aware).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.storage import write_table
from app.ingestion.base import MissingApiKey

log = get_logger(__name__)

BASE = "https://api.stlouisfed.org/fred"


@dataclass(frozen=True)
class SeriesSpec:
    native: str
    category: str
    transform: str = "level"
    lag: int = 1              # release lag in days after period end
    core: bool = False
    country: str = "US"
    vintages: bool = False


def _s(ids: str, category: str, transform: str = "level", lag: int = 1, core: bool = False, country: str = "US",
       vintages: bool = False) -> list[SeriesSpec]:
    return [SeriesSpec(i, category, transform, lag, core, country, vintages) for i in ids.split()]


CATALOG: list[SeriesSpec] = [
    # growth / activity
    *_s("GDPC1", "growth", "log_diff", 30, True, vintages=True),
    *_s("A191RL1Q225SBEA", "growth", "level", 30, False, vintages=True),
    *_s("GDPNOW", "growth", "level", 1),
    *_s("INDPRO", "growth", "yoy", 17, True, vintages=True),
    *_s("TCU", "growth", "level", 17),
    *_s("RRSFS RSAFS", "growth", "yoy", 16, vintages=True),
    *_s("PCEC96 W875RX1", "growth", "yoy", 30),
    *_s("DGORDER ANDENO", "growth", "yoy", 27),
    *_s("CMRMTSPL", "growth", "yoy", 60),
    *_s("CFNAI", "growth", "level", 25, True),
    *_s("CFNAIMA3", "growth", "level", 25),
    *_s("WEI", "growth", "level", 5),
    *_s("GACDFSA066MSFRBPHI BACTSAMFRBDAL", "sentiment", "level", 0),
    *_s("RECPROUSM156N", "growth", "level", 35),
    *_s("USREC", "growth", "level", 35),
    # inflation
    *_s("CPIAUCSL CPILFESL", "inflation", "yoy", 14, True, vintages=True),
    *_s("PCEPI PCEPILFE", "inflation", "yoy", 30, vintages=True),
    *_s("PPIFIS", "inflation", "yoy", 14),
    *_s("CORESTICKM159SFRBATL MEDCPIM158SFRBCLE", "inflation", "level", 15),
    *_s("MICH", "inflation", "level", 0),
    *_s("T5YIE T10YIE T5YIFR", "inflation", "level", 1),
    # labor
    *_s("PAYEMS", "labor", "diff", 6, True, vintages=True),
    *_s("UNRATE", "labor", "level", 6, True, vintages=True),
    *_s("U6RATE CIVPART AWHMAN SAHMREALTIME", "labor", "level", 6),
    *_s("MANEMP", "labor", "diff", 6),
    *_s("CES0500000003", "labor", "yoy", 6),
    *_s("ICSA CCSA", "labor", "level", 5),
    *_s("JTSJOL", "labor", "level", 37),
    # rates / curve
    *_s("DFEDTARU EFFR SOFR DGS3MO DGS2 DGS5 DGS30 T10Y3M T10YFF DFII10 DPRIME", "rates", "level", 1),
    *_s("DGS10 T10Y2Y", "rates", "level", 1, True),
    *_s("THREEFYTP10", "rates", "level", 7),
    *_s("MORTGAGE30US", "rates", "level", 1),
    # credit (BAML* = ICE BofA, 3y history only -> archived)
    *_s("BAMLH0A0HYM2", "credit", "level", 1, True),
    *_s("BAMLH0A3HYC BAMLC0A0CM BAMLC0A4CBBB BAMLEMCBPIOAS BAMLHE00EHYIOAS", "credit", "level", 1),
    *_s("BAA10Y", "credit", "level", 1, True),
    *_s("AAA10Y", "credit", "level", 1),
    *_s("DRTSCILM", "credit", "level", 35),
    # financial conditions / vol
    *_s("NFCI ANFCI STLFSI4", "liquidity", "level", 6),
    *_s("KCFSI", "liquidity", "level", 10),
    *_s("VIXCLS", "sentiment", "level", 1, True),
    *_s("OVXCLS GVZCLS", "sentiment", "level", 1),
    # USD / FX (H.10 weekly, ~1 week lag)
    *_s("DTWEXBGS", "fx", "log_diff", 8, True),
    *_s("DTWEXAFEGS DTWEXEMEGS DEXJPUS DEXKOUS DEXCHUS DEXHKUS DEXTAUS DEXINUS DEXBZUS DEXMXUS DEXUSUK DEXUSEU",
        "fx", "log_diff", 8),
    # commodities
    *_s("DCOILWTICO", "commodities", "log_diff", 7, True),
    *_s("DCOILBRENTEU DHHNGSP", "commodities", "log_diff", 7),
    *_s("PCOPPUSDM PIORECRUSDM PWHEAMTUSDM PALLFNFINDEXM", "commodities", "yoy", 20),
    # housing
    *_s("HOUST PERMIT", "housing", "level", 18),
    *_s("CSUSHPISA", "housing", "yoy", 60),
    *_s("MSPUS", "housing", "yoy", 30),
    # sentiment / liquidity
    *_s("UMCSENT", "sentiment", "level", 0),
    *_s("M2SL", "liquidity", "yoy", 25),
    *_s("WALCL WRESBAL", "liquidity", "level", 1),
    *_s("RRPONTSYD", "liquidity", "level", 1),
    *_s("BUSLOANS TOTALSL", "credit", "yoy", 10),
    # international on FRED (OECD CLI = PMI proxy; call money; 10y; HICP; unemployment; GDP)
    *[SeriesSpec(f"{c}LOLITOAASTSAM", "sentiment", "level", 40, c == "USA", cc) for c, cc in
      (("USA", "US"), ("JPN", "JP"), ("KOR", "KR"), ("CHN", "CN"), ("DEU", "DE"), ("GBR", "GB"), ("BRA", "BR"),
       ("MEX", "MX"), ("G7", "G7"))],
    SeriesSpec("ECBDFR", "rates", "level", 1, False, "EA"),
    *[SeriesSpec(f"IRSTCI01{c}M156N", "rates", "level", 30, False, c) for c in ("JP", "KR", "GB", "MX", "BR")],
    *[SeriesSpec(f"IRLTLT01{c}M156N", "rates", "level", 30, False, c) for c in ("JP", "KR", "DE", "GB", "MX")],
    SeriesSpec("INTGSTBRM193N", "rates", "level", 30, False, "BR"),
    SeriesSpec("INTDSRBRM193N", "rates", "level", 30, False, "BR"),
    SeriesSpec("CP0000EZ19M086NEST", "inflation", "yoy", 17, False, "EA"),
    *[SeriesSpec(f"LRHUTTTT{c}M156S", "labor", "level", 45, False, c) for c in ("JP", "KR", "GB", "MX")],
    SeriesSpec("CLVMNACSCAB1GQEA19", "growth", "log_diff", 45, False, "EA"),
    SeriesSpec("NAEXKP01JPQ657S", "growth", "level", 50, False, "JP"),
    SeriesSpec("NAEXKP01KRQ657S", "growth", "level", 50, False, "KR"),
]
CATALOG_BY_ID: dict[str, SeriesSpec] = {s.native: s for s in CATALOG}


def yaml_meta(settings: Settings | None = None) -> dict[str, Any]:
    """native FRED id -> GSeries metadata from contracts/macro_catalog.yaml (src: fred entries)."""
    from app.ingestion.macro_common import load_catalog  # noqa: PLC0415

    try:
        return {g.native: g for g in load_catalog(settings, ["fred"])}
    except Exception as e:  # noqa: BLE001
        log.warning("macro_catalog.yaml unreadable for fred: %s", e)
        return {}


def full_catalog(settings: Settings | None = None) -> list[SeriesSpec]:
    out = list(CATALOG)
    for native, g in yaml_meta(settings).items():
        if native not in CATALOG_BY_ID:
            out.append(SeriesSpec(native, g.category, g.transform, g.lag, g.core, g.country or "US",
                                  bool(g.req.get("vintages", False))))
    return out
OAS_PREFIX = "BAML"
FREQ_MAP = {"D": "D", "W": "W", "BW": "W", "M": "M", "Q": "Q", "SA": "A", "A": "A"}


def period_end(d: date, freq: str) -> date:
    if freq == "M":
        nm = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return nm - timedelta(days=1)
    if freq == "Q":
        m = ((d.month - 1) // 3 + 1) * 3
        return date(d.year + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    if freq == "A":
        return date(d.year, 12, 31)
    return d


def _num(v: Any) -> float | None:
    if v in (".", "", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def observations_rows(series_id: str, obs: list[dict[str, Any]], freq: str, lag: int, vintages: bool,
                      today: date | None = None) -> pl.DataFrame:
    """FRED observations JSON -> macro_observations rows."""
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    rows = []
    for o in obs:
        d = date.fromisoformat(o["date"])
        if vintages:
            k = date.fromisoformat(o["realtime_start"])
            est = False
        else:
            k = min(period_end(d, freq) + timedelta(days=lag), today)
            est = True
        rows.append((series_id, "fred", d, _num(o.get("value")), k, est, now))
    df = pl.DataFrame(rows, schema={"series_id": pl.Utf8, "source_id": pl.Utf8, "date": pl.Date, "value": pl.Float64,
                                    "knowledge_date": pl.Date, "knowledge_date_estimated": pl.Boolean,
                                    "ingested_at": pl.Datetime("us", "UTC")}, orient="row")
    df = df.unique(subset=["series_id", "date", "knowledge_date"], keep="last")
    if vintages and df.height:
        # ALFRED's archive starts in the 1990s for most series: the first vintage of an old observation is the archive
        # start, not its release. If the first vintage is > 1 year after the expected release, impute the release
        # date (period_end + lag) for that first vintage and flag it as estimated.
        pe = pl.col("date").map_elements(lambda d: period_end(d, freq), return_dtype=pl.Date)
        expected = pe + pl.duration(days=lag)
        first = pl.col("knowledge_date") == pl.col("knowledge_date").min().over("date")
        artefact = first & (pl.col("knowledge_date") > expected + pl.duration(days=365))
        df = df.with_columns(
            pl.when(artefact).then(expected).otherwise(pl.col("knowledge_date")).alias("knowledge_date"),
            pl.when(artefact).then(True).otherwise(pl.col("knowledge_date_estimated")).alias("knowledge_date_estimated"))
    return df


def merge_oas_archive(path, fresh: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, int]]:
    """Append-only archive merge. fresh: [date, value]. Existing first-seen values are never overwritten."""
    now = datetime.now(UTC)
    fresh = fresh.drop_nulls("value").select("date", "value")
    if path.exists():
        old = pl.read_parquet(path)
    else:
        old = pl.DataFrame(schema={"date": pl.Date, "value": pl.Float64, "first_seen": pl.Datetime("us", "UTC"),
                                   "last_value": pl.Float64, "last_seen": pl.Datetime("us", "UTC")})
    j = old.join(fresh.rename({"value": "__new"}), on="date", how="full", coalesce=True)
    is_new = pl.col("value").is_null() & pl.col("__new").is_not_null()
    out = j.with_columns(
        pl.when(is_new).then(pl.col("__new")).otherwise(pl.col("value")).alias("value"),
        pl.when(is_new).then(pl.lit(now)).otherwise(pl.col("first_seen")).alias("first_seen"),
        pl.coalesce(pl.col("__new"), pl.col("last_value")).alias("last_value"),
        pl.when(pl.col("__new").is_not_null()).then(pl.lit(now)).otherwise(pl.col("last_seen")).alias("last_seen"),
    ).drop("__new").sort("date")
    stats = {"archived_rows": out.height, "new_rows": out.height - old.height,
             "revised_rows": out.filter((pl.col("last_value") - pl.col("value")).abs() > 1e-12).height,
             "only_in_archive": out.height - fresh.height}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    out.write_parquet(tmp)
    tmp.replace(path)
    return out, stats


class FredClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.key = self.settings.secret("fred_apikey")
        if not self.key:
            raise MissingApiKey("fred: missing FRED_APIKEY in .env")
        self.http = HttpClient("fred", settings=self.settings, backoff_base=2.0)

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.http.get(f"{BASE}/{path}", {**params, "api_key": self.key, "file_type": "json"}, cache_ttl=0).json()

    def series_meta(self, sid: str) -> dict[str, Any]:
        return (self._get("series", series_id=sid).get("seriess") or [{}])[0]

    def observations(self, sid: str, start: date | None = None, vintages: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"series_id": sid, "limit": 100000}
        if start:
            params["observation_start"] = start.isoformat()
        if vintages:
            params.update(realtime_start="1776-07-04", realtime_end="9999-12-31")
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._get("series/observations", **params, offset=offset)
            obs = page.get("observations", [])
            out.extend(obs)
            if len(obs) < 100000:
                return out
            offset += len(obs)


def run_fred(settings: Settings | None = None, series: list[str] | None = None, start: date | None = None,
             vintages: bool = True, write: bool = True) -> dict[str, Any]:
    from app.ingestion.macro_common import derive_multiplier, derive_scale, reconcile_observations  # noqa: PLC0415

    s = settings or get_settings()
    fc = FredClient(s)
    full = full_catalog(s)
    by_id = {x.native: x for x in full}
    meta_yaml = yaml_meta(s)
    specs = [by_id.get(x) or SeriesSpec(x, "other") for x in series] if series else full
    known: set[str] = set()
    if start is not None:
        try:
            from app.core.storage import read_table  # noqa: PLC0415

            known = set(read_table("macro_series", columns=["series_id"]).get_column("series_id").to_list())
        except Exception:  # noqa: BLE001
            known = set()
    meta_rows, obs_frames = [], []
    report: dict[str, Any] = {"ok": [], "failed": {}, "oas": {}}
    now = datetime.now(UTC)
    raw_dir = s.raw_dir / "fred" / "observations" / f"dt={now.date().isoformat()}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    for sp in specs:
        try:
            m = fc.series_meta(sp.native)
            if not m:
                raise HttpError(404, sp.native, "no metadata")
            freq = FREQ_MAP.get(m.get("frequency_short", "M"), "M")
            use_v = vintages and sp.vintages
            st_eff = start if f"fred:{sp.native}" in known else None   # new series -> full history
            obs = fc.observations(sp.native, start=None if use_v else st_eff, vintages=use_v)
            import gzip  # noqa: PLC0415

            import orjson  # noqa: PLC0415

            (raw_dir / f"{sp.native}{'_vintages' if use_v else ''}.json.gz").write_bytes(
                gzip.compress(orjson.dumps({"meta": m, "observations": obs}), 5))
            df = observations_rows(f"fred:{sp.native}", obs, freq, sp.lag, use_v)
            if use_v:  # also mark the latest-vintage value with estimated=False rows (already included)
                pass
            obs_frames.append(df)
            last = max((date.fromisoformat(o["date"]) for o in obs if _num(o.get("value")) is not None), default=None)
            ym = meta_yaml.get(sp.native)
            meta_rows.append({
                "series_id": f"fred:{sp.native}", "source_id": "fred", "native_code": sp.native,
                "title": m.get("title") or sp.native, "country": sp.country, "category": sp.category,
                "frequency": freq, "units": m.get("units"), "seasonal_adjustment": m.get("seasonal_adjustment_short"),
                "default_transform": sp.transform, "release_lag_days": sp.lag, "is_core": sp.core,
                "last_observation_date": last, "updated_at": now,
                "scale": (ym.scale if ym and ym.scale else derive_scale(m.get("units"), sp.category)),
                "unit_multiplier": derive_multiplier(m.get("units")),
                "indicator": ym.indicator if ym else None,
            })
            if sp.native.startswith(OAS_PREFIX):
                fresh = pl.DataFrame({"date": [date.fromisoformat(o["date"]) for o in obs],
                                      "value": [_num(o.get("value")) for o in obs]},
                                     schema={"date": pl.Date, "value": pl.Float64})
                _, st = merge_oas_archive(s.raw_dir / "fred" / "oas_archive" / f"{sp.native}.parquet", fresh)
                report["oas"][sp.native] = st
            report["ok"].append(sp.native)
        except Exception as e:  # noqa: BLE001
            report["failed"][sp.native] = f"{type(e).__name__}: {e}"[:200]
            log.warning("fred %s failed: %s", sp.native, report["failed"][sp.native])
    report["seconds"] = round(time.perf_counter() - t0, 1)
    report["requests"] = fc.http.n_requests
    if write and meta_rows:
        report["rows_series"] = write_table(pl.DataFrame(meta_rows), "macro_series", mode="upsert", settings=s)
        obs_all = pl.concat(obs_frames)
        report["vintage_rows"] = obs_all.filter(~pl.col("knowledge_date_estimated")).height
        obs_all, report["reconcile"] = reconcile_observations(obs_all, s)
        report["rows_observations"] = write_table(obs_all, "macro_observations", settings=s) if obs_all.height else 0
    return report


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_fred. params: series (list), start (ISO date; default watermark-30d), vintages (bool)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    if not ctx.settings.secret("fred_apikey"):
        return JobResult(status="skipped", message="FRED_APIKEY missing")
    start = date.fromisoformat(p["start"]) if p.get("start") else (
        date.fromisoformat(ctx.watermark[:10]) - timedelta(days=30) if ctx.watermark else None)
    rep = run_fred(ctx.settings, p.get("series"), start, bool(p.get("vintages", True)))
    from app.ingestion.macro_common import _save_report  # noqa: PLC0415

    _save_report({"job": "macro_fred", **rep}, ctx.settings)
    ok, bad = len(rep["ok"]), len(rep["failed"])
    return JobResult(status="partial" if bad else "success",
                     rows_written={"macro_series": rep.get("rows_series", 0),
                                   "macro_observations": rep.get("rows_observations", 0)},
                     entities_ok=ok, entities_failed=bad, http_requests=rep["requests"],
                     watermark=datetime.now(UTC).date().isoformat(), message=f"failed={rep['failed']}")
