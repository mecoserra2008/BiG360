"""GDELT 2.0 Events (15-minute export files) -> gdelt_events + country x CAMEO-root x day aggregates. Job `gdelt_events`.

File discovery: slot names are deterministic (YYYYMMDDHHMM00 every 15 min) — we read `lastupdate.txt` to know the
newest published slot and never request future slots; `masterfilelist.txt` (hundreds of MB) is not needed for
incremental runs. Files are downloaded concurrently (shared `gdelt_files` bucket, 4 req/s), parsed in memory with
Polars in a thread pool (Polars releases the GIL and uses all cores), and DISCARDED — no raw zips are kept unless
`keep_raw=True` (then a rolling buffer of at most 7 days under data/raw/gdelt/buffer/ is enforced).

Kept rows (contract): NumArticles >= 3 OR |GoldsteinScale| >= 5. FIPS ActionGeo codes -> ISO2, CAMEO actor country
(ISO3) -> ISO2, CAMEO root -> simplified event_category. Aggregates per (date_added day, action country, CAMEO root):
events, sum(NumMentions), sum(NumArticles), mean Goldstein, mean AvgTone, count by QuadClass — written to
data/raw/gdelt_events/country_daily/dt=YYYY-MM-DD/part-*.parquet (no contract table yet: see docs/CONTRACT_CHANGES.md
proposal `gdelt_country_daily`).
"""

from __future__ import annotations

import io
import shutil
import time
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.storage import write_table

log = get_logger(__name__)

BASE = "https://data.gdeltproject.org/gdeltv2"

EVENT_COLS = [
    "GLOBALEVENTID", "SQLDATE", "MonthYear", "Year", "FractionDate",
    *[f"Actor1{c}" for c in ("Code", "Name", "CountryCode", "KnownGroupCode", "EthnicCode", "Religion1Code",
                              "Religion2Code", "Type1Code", "Type2Code", "Type3Code")],
    *[f"Actor2{c}" for c in ("Code", "Name", "CountryCode", "KnownGroupCode", "EthnicCode", "Religion1Code",
                              "Religion2Code", "Type1Code", "Type2Code", "Type3Code")],
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode", "QuadClass", "GoldsteinScale", "NumMentions",
    "NumSources", "NumArticles", "AvgTone",
    *[f"{a}Geo_{c}" for a in ("Actor1", "Actor2", "Action") for c in
      ("Type", "FullName", "CountryCode", "ADM1Code", "ADM2Code", "Lat", "Long", "FeatureID")],
    "DATEADDED", "SOURCEURL",
]
assert len(EVENT_COLS) == 61

# FIPS 10-4 (GDELT geo country codes) -> ISO 3166-1 alpha-2
FIPS_TO_ISO2: dict[str, str] = dict(p.split(":") for p in (
    "AA:AW AC:AG AE:AE AF:AF AG:DZ AJ:AZ AL:AL AM:AM AN:AD AO:AO AQ:AS AR:AR AS:AU AU:AT AV:AI BA:BH BB:BB BC:BW "
    "BD:BM BE:BE BF:BS BG:BD BH:BZ BK:BA BL:BO BM:MM BN:BJ BO:BY BP:SB BR:BR BT:BT BU:BG BX:BN BY:BI CA:CA CB:KH "
    "CD:TD CE:LK CF:CG CG:CD CH:CN CI:CL CJ:KY CK:CC CM:CM CN:KM CO:CO CS:CR CT:CF CU:CU CV:CV CW:CK CY:CY DA:DK "
    "DJ:DJ DO:DM DR:DO EC:EC EG:EG EI:IE EK:GQ EN:EE ER:ER ES:SV ET:ET EZ:CZ FG:GF FI:FI FJ:FJ FK:FK FM:FM FO:FO "
    "FP:PF FR:FR GA:GM GB:GA GG:GE GH:GH GI:GI GJ:GD GK:GG GL:GL GM:DE GP:GP GQ:GU GR:GR GT:GT GV:GN GY:GY GZ:PS "
    "HA:HT HK:HK HO:HN HR:HR HU:HU IC:IS ID:ID IM:IM IN:IN IO:IO IR:IR IS:IL IT:IT IV:CI IZ:IQ JA:JP JE:JE JM:JM "
    "JO:JO KE:KE KG:KG KN:KP KR:KI KS:KR KT:CX KU:KW KV:XK KZ:KZ LA:LA LE:LB LG:LV LH:LT LI:LR LO:SK LS:LI LT:LS "
    "LU:LU LY:LY MA:MG MB:MQ MC:MO MD:MD MF:YT MG:MN MH:MS MI:MW MJ:ME MK:MK ML:ML MN:MC MO:MA MP:MU MR:MR MT:MT "
    "MU:OM MV:MV MX:MX MY:MY MZ:MZ NC:NC NE:NU NF:NF NG:NE NH:VU NI:NG NL:NL NO:NO NP:NP NR:NR NS:SR NU:NI NZ:NZ "
    "OD:SS PA:PY PC:PN PE:PE PK:PK PL:PL PM:PA PO:PT PP:PG PS:PW PU:GW QA:QA RB:RS RE:RE RI:RS RM:MH RN:MF RO:RO "
    "RP:PH RQ:PR RS:RU RW:RW SA:SA SB:PM SC:KN SE:SC SF:ZA SG:SN SH:SH SI:SI SL:SL SM:SM SN:SG SO:SO SP:ES ST:LC "
    "SU:SD SV:SJ SW:SE SY:SY SZ:CH TB:BL TD:TT TH:TH TI:TJ TK:TC TL:TK TN:TO TO:TG TP:ST TS:TN TT:TL TU:TR TV:TV "
    "TW:TW TX:TM TZ:TZ UC:CW UG:UG UK:GB UP:UA US:US UV:BF UY:UY UZ:UZ VC:VC VE:VE VI:VG VM:VN VQ:VI VT:VA WA:NA "
    "WE:PS WF:WF WI:EH WS:WS WZ:SZ YM:YE ZA:ZM ZI:ZW"
).split())


def _iso3_to_iso2() -> dict[str, str]:
    try:
        import pycountry  # noqa: PLC0415

        return {c.alpha_3: c.alpha_2 for c in pycountry.countries}
    except Exception:  # pragma: no cover
        return {}


ISO3_TO_ISO2 = _iso3_to_iso2()

CAMEO_CATEGORY: dict[str, str] = {
    "01": "diplomacy", "02": "diplomacy", "03": "diplomacy", "04": "diplomacy", "05": "diplomacy", "06": "economy",
    "07": "economy", "08": "diplomacy", "09": "legal_regulatory", "10": "diplomacy", "11": "diplomacy",
    "12": "diplomacy", "13": "conflict", "14": "protest", "15": "conflict", "16": "sanctions_trade", "17": "conflict",
    "18": "conflict", "19": "conflict", "20": "conflict",
}
# finer overrides by EventBaseCode
CAMEO_BASE_CATEGORY: dict[str, str] = {"163": "sanctions_trade", "164": "sanctions_trade", "1053": "economy",
                                       "173": "legal_regulatory", "1711": "legal_regulatory", "1712": "legal_regulatory"}


# ------------------------------------------------------------------------------------------------ slots / download


def slot_times(day: date) -> list[datetime]:
    t0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [t0 + timedelta(minutes=15 * i) for i in range(96)]


def slot_url(ts: datetime, stream: str) -> str:
    """stream: 'export' | 'mentions' | 'gkg' | 'translation.export' | 'translation.gkg'."""
    stamp = ts.strftime("%Y%m%d%H%M%S")
    if stream.startswith("translation."):
        kind = stream.split(".", 1)[1]
        return f"{BASE}/{stamp}.translation.{kind}.{'CSV' if kind != 'gkg' else 'csv'}.zip"
    return f"{BASE}/{stamp}.{stream}.{'CSV' if stream != 'gkg' else 'csv'}.zip"


def parse_lastupdate(text: str) -> datetime | None:
    """lastupdate.txt lines 'size md5 url' -> newest slot timestamp."""
    best = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3:
            stamp = parts[2].rsplit("/", 1)[-1][:14]
            try:
                ts = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                continue
            best = ts if best is None or ts > best else best
    return best


class GdeltFiles:
    """Concurrent download + in-memory parse of GDELT 15-minute files with bounded memory."""

    def __init__(self, settings: Settings | None = None, workers: int = 12, keep_raw: bool = False,
                 buffer_days: int = 7):
        self.settings = settings or get_settings()
        self.http = HttpClient("gdelt_files", settings=self.settings, max_retries=3, timeout=120, cache=False)
        self.workers = workers
        self.keep_raw = keep_raw
        self.buffer_days = buffer_days
        self.bytes_downloaded = 0
        self.files_ok = 0
        self.files_missing: list[str] = []
        self.download_s = 0.0
        self.parse_s = 0.0

    def latest_slot(self, translation: bool = False) -> datetime | None:
        url = f"{BASE}/lastupdate{'-translation' if translation else ''}.txt"
        try:
            return parse_lastupdate(self.http.get(url, cache_ttl=0).text)
        except Exception as e:  # noqa: BLE001
            log.warning("gdelt lastupdate failed: %s", e)
            return None

    def _fetch(self, url: str) -> bytes | None:
        t0 = time.perf_counter()
        try:
            r = self.http.get(url, cache_ttl=0)
        except HttpError as e:
            if e.status in (404, 403):
                return None
            raise
        finally:
            self.download_s += time.perf_counter() - t0
        self.bytes_downloaded += len(r.content)
        if self.keep_raw:
            d = self.settings.raw_dir / "gdelt" / "buffer" / url.rsplit("/", 1)[-1][:8]
            d.mkdir(parents=True, exist_ok=True)
            (d / url.rsplit("/", 1)[-1]).write_bytes(r.content)
        return r.content

    def prune_buffer(self) -> int:
        root = self.settings.raw_dir / "gdelt" / "buffer"
        if not root.exists():
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=self.buffer_days)).strftime("%Y%m%d")
        n = 0
        for d in root.iterdir():
            if d.is_dir() and d.name < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n

    def process(self, urls: Iterable[str], parse: Callable[[bytes, str], Any]) -> list[Any]:
        """Download + parse each url in a thread pool; returns parse() results (None results dropped)."""
        out: list[Any] = []

        def job(u: str) -> Any:
            data = self._fetch(u)
            if data is None:
                self.files_missing.append(u.rsplit("/", 1)[-1])
                return None
            t0 = time.perf_counter()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                raw = z.read(z.namelist()[0])
            del data
            res = parse(raw, u)
            self.parse_s += time.perf_counter() - t0
            self.files_ok += 1
            return res

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = [ex.submit(job, u) for u in urls]
            for f in as_completed(futs):
                try:
                    r = f.result()
                except Exception as e:  # noqa: BLE001
                    log.warning("gdelt file failed: %s", e)
                    continue
                if r is not None:
                    out.append(r)
        if self.keep_raw:
            self.prune_buffer()
        return out


# ------------------------------------------------------------------------------------------------ events parsing


def read_tsv(raw: bytes, names: list[str], columns: list[int] | None = None) -> pl.DataFrame:
    """Tab-separated, no header, no quoting (GDELT). Returns Utf8 columns."""
    df = pl.read_csv(io.BytesIO(raw), separator="\t", has_header=False, quote_char=None, infer_schema=False,
                     encoding="utf8-lossy", truncate_ragged_lines=True, columns=columns)
    if columns is not None:
        sel = [names[i] for i in columns]
        return df.rename(dict(zip(df.columns, sel, strict=False)))
    return df.rename(dict(zip(df.columns, names, strict=False)))


_EVENT_USE = ["GLOBALEVENTID", "SQLDATE", "Actor1Name", "Actor1CountryCode", "Actor2Name", "Actor2CountryCode",
              "EventCode", "EventBaseCode", "EventRootCode", "QuadClass", "GoldsteinScale", "NumMentions", "NumSources",
              "NumArticles", "AvgTone", "ActionGeo_CountryCode", "ActionGeo_Lat", "ActionGeo_Long", "DATEADDED",
              "SOURCEURL"]


def parse_events(raw: bytes, min_articles: int = 3, min_abs_goldstein: float = 5.0) -> tuple[pl.DataFrame, pl.DataFrame]:
    """One export file -> (kept gdelt_events rows, partial aggregates)."""
    idx = [EVENT_COLS.index(c) for c in _EVENT_USE]
    df = read_tsv(raw, EVENT_COLS, idx)
    df = df.with_columns(
        pl.col("GLOBALEVENTID").cast(pl.Int64, strict=False),
        pl.col("SQLDATE").str.to_date("%Y%m%d", strict=False).alias("event_date"),
        pl.col("DATEADDED").str.to_datetime("%Y%m%d%H%M%S", strict=False, time_zone="UTC").alias("date_added"),
        pl.col("QuadClass").cast(pl.Int8, strict=False),
        pl.col("GoldsteinScale").cast(pl.Float64, strict=False),
        pl.col("AvgTone").cast(pl.Float64, strict=False),
        *[pl.col(c).cast(pl.Int32, strict=False) for c in ("NumMentions", "NumSources", "NumArticles")],
        pl.col("ActionGeo_Lat").cast(pl.Float64, strict=False), pl.col("ActionGeo_Long").cast(pl.Float64, strict=False),
        pl.col("ActionGeo_CountryCode").replace_strict(FIPS_TO_ISO2, default=None, return_dtype=pl.Utf8).alias("action_country"),
        pl.col("EventRootCode").str.zfill(2).alias("EventRootCode"),
    ).filter(pl.col("GLOBALEVENTID").is_not_null() & pl.col("date_added").is_not_null()
             & pl.col("event_date").is_not_null())
    df = df.with_columns(pl.col("date_added").dt.date().alias("knowledge_date"))
    agg = df.group_by("knowledge_date", "action_country", "EventRootCode").agg(
        pl.len().alias("events"), pl.col("NumMentions").sum().alias("mentions"),
        pl.col("NumArticles").sum().alias("articles"), pl.col("GoldsteinScale").sum().alias("goldstein_sum"),
        pl.col("GoldsteinScale").count().alias("goldstein_n"), pl.col("AvgTone").sum().alias("tone_sum"),
        pl.col("AvgTone").count().alias("tone_n"),
        (pl.col("QuadClass") == 1).sum().alias("q1"), (pl.col("QuadClass") == 2).sum().alias("q2"),
        (pl.col("QuadClass") == 3).sum().alias("q3"), (pl.col("QuadClass") == 4).sum().alias("q4"),
    )
    keep = df.filter((pl.col("NumArticles") >= min_articles) | (pl.col("GoldsteinScale").abs() >= min_abs_goldstein))
    cat = pl.col("EventBaseCode").replace_strict(CAMEO_BASE_CATEGORY, default=None, return_dtype=pl.Utf8)
    root_cat = pl.col("EventRootCode").replace_strict(CAMEO_CATEGORY, default="other", return_dtype=pl.Utf8)
    iso3 = ISO3_TO_ISO2
    rows = keep.select(
        pl.col("GLOBALEVENTID").alias("global_event_id"),
        pl.col("date_added").dt.strftime("%Y-%m").alias("year_month"),
        "event_date", "date_added", "knowledge_date",
        pl.col("EventCode").alias("event_code"), pl.col("EventRootCode").alias("event_root_code"),
        pl.col("QuadClass").fill_null(0).alias("quad_class"), pl.col("GoldsteinScale").alias("goldstein"),
        pl.col("NumMentions").fill_null(0).alias("num_mentions"), pl.col("NumSources").fill_null(0).alias("num_sources"),
        pl.col("NumArticles").fill_null(0).alias("num_articles"), pl.col("AvgTone").alias("avg_tone"),
        pl.col("Actor1Name").alias("actor1_name"),
        pl.col("Actor1CountryCode").replace_strict(iso3, default=None, return_dtype=pl.Utf8).alias("actor1_country"),
        pl.col("Actor2Name").alias("actor2_name"),
        pl.col("Actor2CountryCode").replace_strict(iso3, default=None, return_dtype=pl.Utf8).alias("actor2_country"),
        "action_country", pl.col("ActionGeo_Lat").alias("action_lat"), pl.col("ActionGeo_Long").alias("action_lon"),
        pl.col("SOURCEURL").alias("source_url"),
        pl.coalesce(cat, root_cat).alias("event_category"),
    ).filter(pl.col("event_code").is_not_null() & pl.col("event_root_code").is_not_null())
    return rows, agg


def combine_aggregates(parts: list[pl.DataFrame]) -> pl.DataFrame:
    if not parts:
        return pl.DataFrame()
    a = pl.concat(parts).group_by("knowledge_date", "action_country", "EventRootCode").agg(pl.all().sum())
    return a.select(
        pl.col("knowledge_date").alias("date"), pl.col("action_country").alias("country"),
        pl.col("EventRootCode").alias("cameo_root"),
        pl.col("EventRootCode").replace_strict(CAMEO_CATEGORY, default="other", return_dtype=pl.Utf8).alias("category"),
        "events", "mentions", "articles",
        (pl.col("goldstein_sum") / pl.col("goldstein_n")).alias("goldstein_mean"),
        (pl.col("tone_sum") / pl.col("tone_n")).alias("avg_tone_mean"),
        pl.col("q1").alias("quad1"), pl.col("q2").alias("quad2"), pl.col("q3").alias("quad3"), pl.col("q4").alias("quad4"),
    ).sort("date", "country", "cameo_root")


def run_events(settings: Settings | None = None, days: list[date] | None = None, workers: int = 12,
               write: bool = True, keep_raw: bool = False) -> dict[str, Any]:
    s = settings or get_settings()
    gf = GdeltFiles(s, workers=workers, keep_raw=keep_raw)
    latest = gf.latest_slot()
    days = days or [(latest or datetime.now(UTC)).date() - timedelta(days=1)]
    urls = [slot_url(ts, "export") for d in days for ts in slot_times(d) if latest is None or ts <= latest]
    t0 = time.perf_counter()
    res = gf.process(urls, lambda raw, u: parse_events(raw))
    events = pl.concat([r[0] for r in res]).unique(subset=["global_event_id"], keep="last") if res else pl.DataFrame()
    agg = combine_aggregates([r[1] for r in res])
    rep: dict[str, Any] = {"days": [d.isoformat() for d in days], "files": len(urls), "files_ok": gf.files_ok,
                           "files_missing": len(gf.files_missing), "mb_downloaded": round(gf.bytes_downloaded / 1e6, 1),
                           "events_kept": events.height, "agg_rows": agg.height,
                           "seconds_total": None, "download_thread_s": round(gf.download_s, 1),
                           "parse_thread_s": round(gf.parse_s, 1)}
    if write and events.height:
        rep["rows_written"] = write_table(events.with_columns(pl.lit(datetime.now(UTC)).alias("ingested_at")),
                                          "gdelt_events", settings=s)
    if write and agg.height:
        for (d,), part in agg.partition_by("date", as_dict=True).items():
            out = s.raw_dir / "gdelt_events" / "country_daily" / f"dt={d.isoformat()}"
            out.mkdir(parents=True, exist_ok=True)
            for old in out.glob("*.parquet"):
                old.unlink()
            part.write_parquet(out / "part-0.parquet")
        rep["agg_path"] = str((s.raw_dir / "gdelt_events" / "country_daily").relative_to(s.data_dir))
    rep["seconds_total"] = round(time.perf_counter() - t0, 1)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    days = [date.fromisoformat(x) for x in p["days"]] if p.get("days") else None
    if days is None and not p.get("force"):  # events-v2 alias: gdelt_stream already wrote these outputs
        from app.events_v2.pipeline import legacy_done  # noqa: PLC0415

        y = datetime.now(UTC).date() - timedelta(days=1)
        if legacy_done(y, ctx.settings):
            return JobResult(status="success", watermark=y.isoformat(),
                             message=f"alias of gdelt_stream: legacy %s outputs for {y} already written" % "gdelt_events")
    rep = run_events(ctx.settings, days, workers=int(p.get("workers", 12)), keep_raw=bool(p.get("keep_raw", False)))
    status = "success" if rep["files_ok"] else "failed"
    if rep["files_ok"] and rep["files_missing"]:
        status = "partial"
    return JobResult(status=status, rows_written={"gdelt_events": rep.get("rows_written", 0)},
                     entities_ok=rep["files_ok"], entities_failed=rep["files_missing"], http_requests=rep["files"],
                     watermark=max(rep["days"]), message=str({k: rep[k] for k in ("mb_downloaded", "events_kept",
                                                                                 "agg_rows", "seconds_total")}))


_ = Path
