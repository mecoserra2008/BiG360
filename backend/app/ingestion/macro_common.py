"""Shared engine for the catalog-driven macro jobs (macro_eu, macro_intl, macro_national; macro_global = umbrella).

* `contracts/macro_catalog.yaml` is the single source of truth: regions, indicators, every non-FRED series (plus FRED
  additions / indicator mappings) with its request parameters and metadata (indicator, scale, SA, transform, release
  lag, rank = 1 primary / 2+ fallback). `load_catalog()` expands `each:` templates (one entry -> one series per region).
* Source fetchers register with `@fetcher("<src>")` and receive all catalog specs of that source, so they can batch
  (Eurostat multi-geo, OECD/BIS/IMF '+' keys). A fetcher calls `runner.add(spec, pairs)` per series and
  `runner.fail(spec, msg)` for misses; one failing source never blocks the others.
* PIT: no non-US source serves vintages -> knowledge_date = min(period_end + release_lag_days, retrieval date),
  knowledge_date_estimated = true. `reconcile_observations()` makes re-runs revision-aware: an unchanged value is not
  re-written; a *changed* value (revision) gets knowledge_date = retrieval date (first seen, not back-dated), so later
  runs never leak revised values into the past.
"""

from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from app.core.config import Settings, get_settings
from app.core.http import HttpClient
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BiG-Equity-360 research"}
SCALES = ("percent", "fraction", "index", "level")
OBS_SCHEMA = {"series_id": pl.Utf8, "source_id": pl.Utf8, "date": pl.Date, "value": pl.Float64,
              "knowledge_date": pl.Date, "knowledge_date_estimated": pl.Boolean, "ingested_at": pl.Datetime("us", "UTC")}


# ------------------------------------------------------------------------------------------------ dates / numbers


def period_end(d: date, freq: str) -> date:
    if freq == "M":
        return date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
    if freq == "Q":
        m = ((d.month - 1) // 3 + 1) * 3
        return date(d.year + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    if freq == "A":
        return date(d.year, 12, 31)
    if freq == "W":
        return d + timedelta(days=6) if d.weekday() == 0 else d
    return d


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MONTHS.update({"ene": 1, "abr": 4, "ago": 8, "set": 9, "dic": 12, "fev": 2, "mai": 5, "out": 10, "dez": 12})


def parse_period(s: str) -> date:
    """'2026-08' / '2026-Q3' / '2026Q3' / '2026-M08' / '2026M08' / '2026' / '2026-10-07' / '20261007' / '2026-W40'
    / '2026 SEP' / 'Sep 2026' / '2026-S1' -> period start date."""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    if len(s) == 6 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), 1)
    m = re.fullmatch(r"(\d{4})-?Q([1-4])", s)
    if m:
        return date(int(m[1]), (int(m[2]) - 1) * 3 + 1, 1)
    m = re.fullmatch(r"(\d{4})-?M(\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), 1)
    m = re.fullmatch(r"(\d{4})-?S([12])", s)
    if m:
        return date(int(m[1]), 1 if m[2] == "1" else 7, 1)
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", s)
    if m:
        return date.fromisocalendar(int(m[1]), int(m[2]), 1)
    if len(s) == 7 and s[4] == "-":
        return date(int(s[:4]), int(s[5:7]), 1)
    if len(s) == 4 and s.isdigit():
        return date(int(s), 1, 1)
    m = re.fullmatch(r"(\d{4})\s+([A-Za-z]{3})", s)
    if m and m[2].lower() in _MONTHS:
        return date(int(m[1]), _MONTHS[m[2].lower()], 1)
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?[\s\-]*(\d{2,4})", s)
    if m and m[1].lower() in _MONTHS:
        y = int(m[2])
        return date(y + 2000 if y < 100 else y, _MONTHS[m[1].lower()], 1)
    return date.fromisoformat(s[:10])


def to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f else None
    s = str(v).strip().replace("−", "-")
    if s in ("", "NaN", "nan", ".", "-", "..", "...", "n.d.", "n.a.", "NA", "null", "None", "x", ":"):
        return None
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None


_f = to_float


def dedupe_pairs(pairs: Iterable[tuple[date, float | None]]) -> list[tuple[date, float | None]]:
    """One value per date (a non-null value wins over a null one; later wins otherwise), sorted by date."""
    out: dict[date, float | None] = {}
    for d, v in pairs:
        if v is not None or d not in out:
            out[d] = v
    return sorted(out.items())


# ------------------------------------------------------------------------------------------------ parsers (pure)


def parse_sdmx_csv(text: str, key_cols: tuple[str, ...] = ()) -> dict[tuple[str, ...], list[tuple[date, float | None]]]:
    """SDMX CSV (ECB csvdata / BIS / IMF / OECD csvfile): multi-line quoted text attributes handled by csv."""
    out: dict[tuple[str, ...], list[tuple[date, float | None]]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        tp, v = r.get("TIME_PERIOD"), r.get("OBS_VALUE")
        if not tp:
            continue
        k = tuple(r.get(c, "") for c in key_cols)
        try:
            out.setdefault(k, []).append((parse_period(tp), _f(v)))
        except ValueError:
            continue
    return out


_NONDIM = {"STRUCTURE", "STRUCTURE_ID", "STRUCTURE_NAME", "ACTION", "KEY", "DATAFLOW"}


def parse_sdmx_keyed(text: str, ndims: int | None = None) -> dict[str, list[tuple[date, float | None]]]:
    """Generic SDMX CSV -> {'D1.D2...': [(date, value)]}: the series key is rebuilt from the dimension columns that
    precede TIME_PERIOD (works for ECB csvdata, BIS v2 csv, IMF sdmx-csv 1.0, OECD csvfile)."""
    text = text.lstrip("\ufeff")
    first = text.split("\n", 1)[0]
    rdr = csv.reader(io.StringIO(text), delimiter=";" if first.count(";") > first.count(",") else ",")
    try:
        header = [h.strip() for h in next(rdr)]
    except StopIteration:
        return {}
    if "TIME_PERIOD" not in header or "OBS_VALUE" not in header:
        return {}
    ti, vi = header.index("TIME_PERIOD"), header.index("OBS_VALUE")
    dims = [i for i, h in enumerate(header[:ti]) if h.upper() not in _NONDIM and ":" not in h]
    if ndims:  # BIS (and others) put series attributes between the dimensions and TIME_PERIOD
        dims = dims[:ndims]
    out: dict[str, list[tuple[date, float | None]]] = {}
    for r in rdr:
        if len(r) <= max(ti, vi):
            continue
        try:
            d = parse_period(r[ti])
        except (ValueError, IndexError):
            continue
        out.setdefault(".".join(r[i] for i in dims), []).append((d, _f(r[vi])))
    return {k: sorted(v) for k, v in out.items()}


def combine_keys(keys: Iterable[str]) -> str:
    """['M.US', 'M.GB'] -> 'M.US+GB' (per-dimension union; an empty position stays a wildcard)."""
    parts = [k.split(".") for k in keys]
    n = len(parts[0])
    if any(len(p) != n for p in parts):
        raise ValueError(f"keys of different length: {keys}")
    out = []
    for i in range(n):
        vals = list(dict.fromkeys(p[i] for p in parts))
        out.append("" if "" in vals else "+".join(vals))
    return ".".join(out)


def match_key(data: dict[str, Any], key: str) -> Any:
    """Exact key, else wildcard match ('' positions match anything); first match in sorted order."""
    if key in data:
        return data[key]
    kp = key.split(".")
    for k in sorted(data):
        p = k.split(".")
        if len(p) == len(kp) and all(a in ("", b) for a, b in zip(kp, p, strict=True)):
            return data[k]
    return None


def parse_jsonstat(j: dict[str, Any]) -> list[tuple[date, float | None]]:
    """Eurostat JSON-stat 2.0 with every non-time dimension fixed to one value."""
    tdim = j["dimension"]["time"]["category"]["index"]
    vals = j.get("value", {})
    if any(sz == 0 for sz in j["size"]):
        return []
    return sorted((parse_period(t), _f(vals.get(str(i)) if isinstance(vals, dict) else vals[i])) for t, i in tdim.items())


def parse_jsonstat_multi(j: dict[str, Any], by: str = "geo") -> dict[str, list[tuple[date, float | None]]]:
    """Eurostat JSON-stat 2.0 with one free dimension `by` (e.g. several geo values) + time; others fixed to 1."""
    ids, size = j["id"], j["size"]
    if any(sz == 0 for sz in size):
        return {}
    vals = j.get("value", {})
    cat = {d: j["dimension"][d]["category"]["index"] for d in ids}
    for d in ids:
        if isinstance(cat[d], list):
            cat[d] = {k: i for i, k in enumerate(cat[d])}
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * size[i + 1]
    bi, ti = ids.index(by), ids.index("time")
    out: dict[str, list[tuple[date, float | None]]] = {}
    for g, gi in cat[by].items():
        rows = []
        for t, tix in cat["time"].items():
            flat = gi * strides[bi] + tix * strides[ti]
            v = vals.get(str(flat)) if isinstance(vals, dict) else (vals[flat] if flat < len(vals) else None)
            rows.append((parse_period(t), _f(v)))
        out[g] = sorted(rows)
    return out


def parse_boe_csv(text: str) -> list[tuple[date, float | None]]:
    return parse_boe_csv_multi(text).get("__first__", [])


def parse_boe_csv_multi(text: str) -> dict[str, list[tuple[date, float | None]]]:
    """BoE IADB CSV (CSVF=TN): 'DATE,CODE1,CODE2' header, '01 Sep 2026' dates. '__first__' = first value column."""
    out: dict[str, list[tuple[date, float | None]]] = {}
    header: list[str] = []
    for r in csv.reader(io.StringIO(text)):
        if not r:
            continue
        if r[0].strip().upper() == "DATE":
            header = [c.strip() for c in r]
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d %b %Y").date()
        except ValueError:
            continue
        for i, x in enumerate(r[1:], start=1):
            code = header[i] if i < len(header) else f"col{i}"
            out.setdefault(code, []).append((d, _f(x)))
            if i == 1:
                out.setdefault("__first__", []).append((d, _f(x)))
    return out


def parse_boj(j: dict[str, Any]) -> list[tuple[date, float | None]]:
    return [p for v in parse_boj_multi(j).values() for p in v]


def parse_boj_multi(j: dict[str, Any]) -> dict[str, list[tuple[date, float | None]]]:
    out: dict[str, list[tuple[date, float | None]]] = {}
    for rs in j.get("RESULTSET", []):
        code = str(rs.get("SERIES_CODE", ""))
        v = rs.get("VALUES", {})
        for d, x in zip(v.get("SURVEY_DATES", []), v.get("VALUES", []), strict=False):
            s = str(d)
            try:
                if len(s) == 5:      # quarterly/half-year: YYYYQ (1..4)
                    dd = date(int(s[:4]), (int(s[4]) - 1) * 3 + 1, 1)
                elif len(s) == 4:
                    dd = date(int(s), 1, 1)
                else:
                    dd = date(int(s[:4]), int(s[4:6]), int(s[6:8]) if len(s) >= 8 else 1)
            except ValueError:
                continue
            out.setdefault(code, []).append((dd, _f(x)))
    return out


def parse_bcb(j: list[dict[str, Any]]) -> list[tuple[date, float | None]]:
    out = []
    for r in j:
        try:
            d, m, y = r["data"].split("/")
            out.append((date(int(y), int(m), int(d)), _f(r.get("valor"))))
        except (KeyError, ValueError):
            continue
    return out


# ------------------------------------------------------------------------------------------------ units / scale

_MULT = (("trillion", 1e12), ("tril", 1e12), ("billion", 1e9), ("bil", 1e9), ("million", 1e6), ("mil", 1e6),
         ("thousand", 1e3), ("thous", 1e3))


def derive_scale(units: str | None, category: str | None = None) -> str:
    u = (units or "").lower().strip()
    if u in ("fraction", "ratio") or u.startswith("fraction"):
        return "fraction"
    if ("percent" in u or u.startswith("%") or "% " in u or u.endswith("%") or "pct" in u or "growth rate" in u
            or "percentage" in u or u.startswith("% of") or "per annum" in u or "% chg" in u or "p.p." in u):
        return "percent"
    if u.startswith("index") or "index" in u or u in ("balance", "net balance", "diffusion index", "z-score", "+1 or 0"):
        return "index"
    if not u and category in ("rates", "credit"):
        return "percent"
    return "level"


def derive_multiplier(units: str | None) -> float:
    u = (units or "").lower()
    for k, m in _MULT:
        if re.search(rf"\b{k}", u):
            return m
    return 1.0


# ------------------------------------------------------------------------------------------------ specs / catalog


@dataclass(frozen=True)
class GSeries:
    series_id: str          # '{source}:{native}'
    source_id: str
    native: str
    title: str
    country: str
    category: str
    frequency: str
    units: str
    transform: str
    lag: int
    core: bool = False
    scale: str = ""                      # percent | fraction | index | level ('' -> derived from units)
    sa: str | None = None                # SA | NSA | SCA | SAAR | TREND ...
    indicator: str | None = None         # catalog indicator key(s), comma separated
    multiplier: float = 1.0
    rank: int = 1                        # 1 primary, 2+ fallback within (region, indicator)
    requires_key: str | None = None      # Settings secret name; missing -> skipped with a "missing key" status
    req: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def scale_eff(self) -> str:
        return self.scale or derive_scale(self.units, self.category)

    @property
    def indicators(self) -> list[str]:
        return [x for x in (self.indicator or "").split(",") if x]


STD_KEYS = {"id", "src", "region", "ind", "title", "freq", "units", "scale", "sa", "tf", "lag", "core", "rank", "cat",
            "each", "mult", "requires_key", "note", "native", "name"}


def catalog_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).contracts_dir / "macro_catalog.yaml"


@lru_cache(maxsize=4)
def _load_yaml(path: str, mtime: float) -> dict[str, Any]:  # noqa: ARG001 - mtime busts the cache
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def catalog_doc(settings: Settings | None = None) -> dict[str, Any]:
    p = catalog_path(settings)
    return _load_yaml(str(p), p.stat().st_mtime) if p.exists() else {}


def _fmt(v: Any, ctx: dict[str, str]) -> Any:
    if isinstance(v, str):
        try:
            return v.format_map(ctx)
        except (KeyError, IndexError, ValueError):
            return v
    if isinstance(v, dict):
        return {k: _fmt(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [_fmt(x, ctx) for x in v]
    return v


def expand_entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """`series:` entries with `each: {REGION: code | {code:.., overrides..}}` -> one entry per region."""
    regions = doc.get("regions", {}) or {}
    out: list[dict[str, Any]] = []
    for e in doc.get("series", []) or []:
        each = e.get("each")
        if not each:
            reg = e.get("region", "")
            ctx = {"region": reg, "code": e.get("code", reg), "name": (regions.get(reg) or {}).get("name", reg)}
            out.append(_fmt({k: v for k, v in e.items()}, ctx))
            continue
        for key, spec in each.items():
            over = spec if isinstance(spec, dict) else {"code": spec}
            reg = str(over.get("region") or e.get("region") or key)   # each key may be a label (EA_10Y)
            code = str(over.get("code", key))
            ctx = {"region": reg, "code": code, "name": (regions.get(reg) or {}).get("name", reg)}
            base = {k: v for k, v in e.items() if k != "each"}
            base.update({k: v for k, v in over.items()})
            base.setdefault("code", code)
            base["region"] = reg
            out.append(_fmt(base, ctx))
    return out


def entry_to_spec(e: dict[str, Any], indicators: dict[str, Any]) -> GSeries:
    ind = e.get("ind")
    ind_list = ind if isinstance(ind, list) else ([ind] if ind else [])
    idef = indicators.get(ind_list[0], {}) if ind_list else {}
    cat = e.get("cat") or idef.get("category") or "other"
    units = str(e.get("units") or idef.get("units") or "")
    native = str(e.get("native") or e["id"].split(":", 1)[1])
    req = {k: v for k, v in e.items() if k not in STD_KEYS}
    return GSeries(
        series_id=e["id"], source_id=e["src"], native=native, title=str(e.get("title") or e["id"]),
        country=str(e.get("region") or ""), category=cat, frequency=str(e.get("freq") or idef.get("freq") or "M"),
        units=units, transform=str(e.get("tf") or idef.get("tf") or "level"), lag=int(e.get("lag", idef.get("lag", 30))),
        core=bool(e.get("core", False)), scale=str(e.get("scale") or idef.get("scale") or ""),
        sa=e.get("sa"), indicator=",".join(ind_list) or None, multiplier=float(e.get("mult", derive_multiplier(units))),
        rank=int(e.get("rank", 1)), requires_key=e.get("requires_key"), req=req)


def load_catalog(settings: Settings | None = None, sources: Iterable[str] | None = None) -> list[GSeries]:
    doc = catalog_doc(settings)
    inds = doc.get("indicators", {}) or {}
    specs = [entry_to_spec(e, inds) for e in expand_entries(doc)]
    if sources is not None:
        src = set(sources)
        specs = [s for s in specs if s.source_id in src]
    return specs


# ------------------------------------------------------------------------------------------------ fetcher registry

Fetcher = Callable[["MacroRunner", list[GSeries]], None]
FETCHERS: dict[str, Fetcher] = {}


def fetcher(src: str) -> Callable[[Fetcher], Fetcher]:
    def deco(fn: Fetcher) -> Fetcher:
        FETCHERS[src] = fn
        return fn
    return deco


# ------------------------------------------------------------------------------------------------ runner


def obs_frame(spec: GSeries, pairs: Iterable[tuple[date, float | None]], today: date | None = None) -> pl.DataFrame:
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    rows = [(spec.series_id, spec.source_id, d, v, min(period_end(d, spec.frequency) + timedelta(days=spec.lag), today),
             True, now) for d, v in pairs if d <= today]
    return pl.DataFrame(rows, schema=OBS_SCHEMA, orient="row").unique(subset=["series_id", "date", "knowledge_date"],
                                                                         keep="last")


class MacroRunner:
    """Collects specs + observation frames from many fetchers, then writes macro_series / macro_observations."""

    def __init__(self, job: str, settings: Settings | None = None, start: date | None = None):
        self.job = job
        self.s = settings or get_settings()
        self.start = start or date(1990, 1, 1)
        self.full = start is not None   # an explicit start means "re-fetch from there"
        self.today = datetime.now(UTC).date()
        self.clients: dict[str, HttpClient] = {}
        self.specs: dict[str, GSeries] = {}
        self.frames: list[pl.DataFrame] = []
        self.report: dict[str, Any] = {"job": job, "ok": {}, "failed": {}, "skipped": {}, "seconds": {}}

    # ---- helpers for fetchers
    def http(self, src: str, **kw: Any) -> HttpClient:
        if src not in self.clients:
            opts = {"headers": UA, "max_retries": 3, "timeout": 60, **kw}
            self.clients[src] = HttpClient(src, settings=self.s, **opts)
        return self.clients[src]

    def since(self, fmt: str = "%Y-%m") -> str:
        return self.start.strftime(fmt)

    def last_obs(self) -> dict[str, date]:
        if not hasattr(self, "_last_obs"):
            try:
                from app.core.storage import read_table  # noqa: PLC0415

                ms = read_table("macro_series", columns=["series_id", "last_observation_date"], settings=self.s)
                self._last_obs = {k: v for k, v in ms.iter_rows() if v is not None}
            except Exception:  # noqa: BLE001
                self._last_obs = {}
        return self._last_obs

    def since_for(self, specs: Iterable[GSeries], lookback_days: int = 400) -> date:
        """Incremental start for sources whose cost grows with history (UST yearly files, SGS windows, BCRA pages):
        full history when any series is new (or params.full), else last stored observation - lookback."""
        if self.full:
            return self.start
        lo = self.last_obs()
        ds = [lo.get(s.series_id) for s in specs]
        if not ds or any(d is None for d in ds):
            return self.start
        return max(self.start, min(ds) - timedelta(days=lookback_days))

    def add(self, spec: GSeries, pairs: Iterable[tuple[date, float | None]]) -> int:
        pairs = [p for p in pairs if p[0] >= self.start]
        if spec.req.get("actual_only"):  # e.g. IMF WEO: drop current-year and projection rows
            pairs = [p for p in pairs if p[0].year < self.today.year]
        if spec.req.get("value_factor"):  # unit normalisation at ingestion, e.g. BanRep TES fraction -> percent
            f = float(spec.req["value_factor"])
            pairs = [(d, None if v is None else v * f) for d, v in pairs]
        if spec.req.get("shift_months"):  # e.g. INE Chile rolling quarter labelled by its first month -> last month
            n = int(spec.req["shift_months"])
            pairs = [(date(d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1, 1), v) for d, v in pairs]
        df = obs_frame(spec, pairs, self.today)
        if df.is_empty() or df.get_column("value").is_null().all():
            self.fail(spec, "no observations")
            return 0
        self.specs[spec.series_id] = spec
        self.frames.append(df)
        self.report["ok"][spec.series_id] = df.height
        self.report["failed"].pop(spec.series_id, None)
        return df.height

    def fail(self, spec: GSeries | str, msg: str) -> None:
        sid = spec if isinstance(spec, str) else spec.series_id
        if sid not in self.report["ok"]:
            self.report["failed"][sid] = str(msg)[:240]

    def skip(self, name: str, reason: str) -> None:
        self.report["skipped"][name] = reason

    def attempt(self, name: str, fn: Callable[[], Any], specs: Iterable[GSeries] = ()) -> Any:
        t0 = time.perf_counter()
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one source failing must not block the others
            msg = f"{type(e).__name__}: {e}"[:240]
            specs = list(specs)
            if specs:
                for sp in specs:
                    self.fail(sp, msg)
            else:
                self.report["failed"][name] = msg
            log.warning("%s %s failed: %s", self.job, name, msg)
            return None
        finally:
            self.report["seconds"][name] = round(self.report["seconds"].get(name, 0) + time.perf_counter() - t0, 1)

    # ---- orchestration
    def run_sources(self, specs: list[GSeries], sources: Iterable[str] | None = None) -> None:
        want = list(sources) if sources else list(dict.fromkeys(s.source_id for s in specs))
        for src in want:
            group = [s for s in specs if s.source_id == src]
            if not group:
                continue
            fn = FETCHERS.get(src)
            if fn is None:
                self.skip(src, "no fetcher registered")
                continue
            keyed = [s for s in group if s.requires_key]
            missing = sorted({s.requires_key for s in keyed if not self.s.secret(s.requires_key)})
            if missing:
                group = [s for s in group if not s.requires_key or s.requires_key not in missing]
                self.skip(src, f"missing key {', '.join(m.upper() for m in missing)}"
                               f" ({len(keyed) - sum(1 for s in group if s.requires_key)} series skipped)")
                for s in keyed:
                    if s.requires_key in missing:
                        self.report.setdefault("skipped_series", {})[s.series_id] = f"missing key {s.requires_key.upper()}"
            if not group:
                continue
            t0 = time.perf_counter()
            self.attempt(src, lambda fn=fn, group=group: fn(self, group), group)
            self.report["seconds"][src] = round(time.perf_counter() - t0, 1)
            skipped = self.report.get("skipped_series") or {}   # e.g. A9 source_disallowed (frozen) series
            for s in group:  # series silently ignored by a fetcher
                if (s.series_id not in self.report["ok"] and s.series_id not in self.report["failed"]
                        and s.series_id not in skipped):
                    self.fail(s, "not returned by source")

    def series_frame(self, obs: pl.DataFrame) -> pl.DataFrame:
        now = datetime.now(UTC)
        last: dict[str, date] = {}
        if obs.height:
            last = dict(obs.drop_nulls("value").group_by("series_id").agg(pl.col("date").max()).iter_rows())
        return series_rows(list(self.specs.values()), last, now)

    def finalize(self) -> tuple[pl.DataFrame, pl.DataFrame]:
        obs = pl.concat(self.frames) if self.frames else pl.DataFrame(schema=OBS_SCHEMA)
        return self.series_frame(obs), obs

    def write(self, write: bool = True) -> dict[str, Any]:
        series, obs = self.finalize()
        rep = self.report
        rep["requests"] = sum(c.n_requests for c in self.clients.values())
        rep["requests_by_source"] = {k: c.n_requests for k, c in self.clients.items()}
        for c in self.clients.values():
            c.close()
        if write and series.height:
            rep["rows_series"] = write_table(series, "macro_series", mode="upsert", settings=self.s)
            obs2, st = reconcile_observations(obs, self.s)
            rep["reconcile"] = st
            rep["rows_observations"] = write_table(obs2, "macro_observations", settings=self.s) if obs2.height else 0
            rep["countries"] = sorted(c for c in series.get_column("country").unique().to_list() if c)
        return rep


def sdmx_fetch(r: MacroRunner, specs: list[GSeries], src: str, url_for: Callable[[str, str], str],
               params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, cache_ttl: int = 3600,
               max_batch: int = 60, key_field: str = "key", flow_field: str = "flow",
               single_dim: bool = False) -> None:
    """Batch SDMX-CSV fetch: one request per (flow, <= max_batch keys) with per-dimension '+' unions; per-key retry
    of the misses if a combined request fails. single_dim=True only batches keys that differ in ONE dimension
    (no cartesian over-fetch; the ECB rejects some multi-dimension unions)."""
    from app.core.http import HttpError  # noqa: PLC0415

    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req[flow_field]), []).append(sp)
    for flow, grp in groups.items():
        by_len: dict[int, list[GSeries]] = {}
        for sp in grp:
            by_len.setdefault(len(str(sp.req[key_field]).split(".")), []).append(sp)
        subs = list(by_len.values())
        if single_dim:
            subs = [g for sub in subs for g in single_dim_groups(sub, key_field)]
        for sub in subs:
            for i in range(0, len(sub), max_batch):
                chunk = sub[i:i + max_batch]
                keys = [str(sp.req[key_field]) for sp in chunk]

                def get(key: str) -> dict[str, list[tuple[date, float | None]]]:
                    resp = r.http(src).get(url_for(flow, key), params, headers=headers, cache_ttl=cache_ttl)
                    return parse_sdmx_keyed(resp.text, len(key.split(".")))

                try:
                    data = get(combine_keys(keys))
                except (HttpError, ValueError) as e:
                    if len(chunk) == 1:
                        r.fail(chunk[0], f"{type(e).__name__}: {e}")
                        continue
                    data = {}
                    for sp in chunk:
                        try:
                            data.update(get(str(sp.req[key_field])))
                        except Exception as e2:  # noqa: BLE001
                            r.fail(sp, f"{type(e2).__name__}: {e2}")
                for sp in chunk:
                    pairs = match_key(data, str(sp.req[key_field]))
                    if pairs:
                        r.add(sp, pairs)
                    else:
                        r.fail(sp, f"key {sp.req[key_field]} not in {flow} response")


def single_dim_groups(specs: list[GSeries], key_field: str = "key") -> list[list[GSeries]]:
    """Greedy partition: each group's keys are identical except in one dimension position."""
    groups: list[tuple[list[str], int | None, list[GSeries]]] = []
    for sp in specs:
        k = str(sp.req[key_field]).split(".")
        for gi, (tmpl, pos, members) in enumerate(groups):
            diff = [i for i, (a, b) in enumerate(zip(tmpl, k, strict=True)) if a != b]
            if not diff or (len(diff) == 1 and (pos is None or pos == diff[0])):
                members.append(sp)
                groups[gi] = (tmpl, pos if pos is not None or not diff else diff[0], members)
                break
        else:
            groups.append((k, None, [sp]))
    return [m for _, _, m in groups]


def series_rows(specs: list[GSeries], last: dict[str, date], now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    schema = {"series_id": pl.Utf8, "source_id": pl.Utf8, "native_code": pl.Utf8, "title": pl.Utf8, "country": pl.Utf8,
              "category": pl.Utf8, "frequency": pl.Utf8, "units": pl.Utf8, "seasonal_adjustment": pl.Utf8,
              "default_transform": pl.Utf8, "release_lag_days": pl.Int16, "is_core": pl.Boolean,
              "last_observation_date": pl.Date, "updated_at": pl.Datetime("us", "UTC"), "scale": pl.Utf8,
              "unit_multiplier": pl.Float64, "indicator": pl.Utf8}
    return pl.DataFrame([{
        "series_id": sp.series_id, "source_id": sp.source_id, "native_code": sp.native, "title": sp.title,
        "country": sp.country or None, "category": sp.category, "frequency": sp.frequency, "units": sp.units,
        "seasonal_adjustment": sp.sa, "default_transform": sp.transform, "release_lag_days": sp.lag,
        "is_core": sp.core, "last_observation_date": last.get(sp.series_id), "updated_at": now,
        "scale": sp.scale_eff, "unit_multiplier": sp.multiplier, "indicator": sp.indicator,
    } for sp in specs], schema=schema)


# ------------------------------------------------------------------------------------------------ PIT reconcile


def reconcile_observations(obs: pl.DataFrame, settings: Settings | None = None,
                           today: date | None = None) -> tuple[pl.DataFrame, dict[str, int]]:
    """Make re-runs revision-aware for estimated rows (see module doc). Observed (vintage) rows pass through."""
    today = today or datetime.now(UTC).date()
    st = {"input": obs.height, "unchanged_dropped": 0, "revised": 0, "new": 0}
    if obs.is_empty():
        return obs, st
    ids = obs.get_column("series_id").unique().to_list()
    try:
        ex = (scan_table("macro_observations", settings).filter(pl.col("series_id").is_in(ids))
              .select("series_id", "date", "value", "knowledge_date").collect())
    except Exception as e:  # noqa: BLE001
        log.warning("reconcile: cannot read existing macro_observations: %s", e)
        return obs, st
    if ex.is_empty():
        st["new"] = obs.height
        return obs, st
    latest = (ex.sort("knowledge_date").group_by("series_id", "date")
              .agg(pl.col("value").last().alias("__v"), pl.col("knowledge_date").last().alias("__k")))
    j = obs.join(latest, on=["series_id", "date"], how="left")
    est = pl.col("knowledge_date_estimated")
    seen = pl.col("__k").is_not_null()
    same = ((pl.col("value").is_null() & pl.col("__v").is_null())
            | ((pl.col("value") - pl.col("__v")).abs() <= 1e-9 * pl.max_horizontal(pl.lit(1.0), pl.col("__v").abs())))
    drop = est & seen & same.fill_null(False)
    revised = est & seen & ~same.fill_null(False)
    st["unchanged_dropped"] = j.filter(drop).height
    st["revised"] = j.filter(revised).height
    st["new"] = j.filter(~seen).height
    out = (j.filter(~drop)
           .with_columns(pl.when(revised).then(pl.max_horizontal(pl.lit(today), pl.col("__k")))
                         .otherwise(pl.col("knowledge_date")).alias("knowledge_date"),
                         pl.when(revised).then(False).otherwise(est).alias("knowledge_date_estimated"))
           .drop("__v", "__k"))
    return out, st


def job_result(rep: dict[str, Any]) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ok, bad = len(rep.get("ok", {})), len(rep.get("failed", {}))
    status = "success" if not bad else ("partial" if ok else "failed")
    return JobResult(status=status,
                     rows_written={"macro_series": rep.get("rows_series", 0),
                                   "macro_observations": rep.get("rows_observations", 0)},
                     entities_ok=ok, entities_failed=bad, http_requests=rep.get("requests", 0),
                     watermark=datetime.now(UTC).date().isoformat(),
                     message=(f"ok={ok} failed={bad} skipped={rep.get('skipped', {})} "
                              f"failed_ids={list(rep.get('failed', {}))[:30]}")[:2000])


def run_group(job: str, sources: list[str], settings: Settings | None = None, start: date | None = None,
              only: Iterable[str] | None = None, write: bool = True) -> dict[str, Any]:
    """Run every catalog series of `sources` (optionally a subset `only` of sources / series ids)."""
    s = settings or get_settings()
    specs = load_catalog(s, sources)
    if only:
        o = set(only)
        specs = [x for x in specs if x.source_id in o or x.series_id in o]
    r = MacroRunner(job, s, start)
    r.run_sources(specs, [x for x in sources if not only or any(sp.source_id == x for sp in specs)])
    rep = r.write(write)
    if write:
        rep["retired"] = retire_series(sources, s)
        rep["frozen"] = freeze_series(sources, s)
        _save_report(rep, s)   # only real runs: macro_global uses the report age to skip fresh groups
    return rep


def retire_series(sources: Iterable[str], settings: Settings | None = None) -> dict[str, str]:
    """catalog `retired: {old_id: replacement_id}`: keep the old rows/history (PIT), but demote them in macro_series
    (is_core = false, title suffixed with the replacement) so dashboards and the audit stop treating them as live."""
    s = settings or get_settings()
    retired = {k: v for k, v in ((catalog_doc(s).get("retired") or {}).items()) if k.split(":", 1)[0] in set(sources)}
    if not retired:
        return {}
    try:
        from app.core.storage import read_table  # noqa: PLC0415

        ms = read_table("macro_series", settings=s).filter(pl.col("series_id").is_in(list(retired)))
    except Exception:  # noqa: BLE001
        return {}
    if ms.is_empty():
        return {}
    ms = ms.with_columns(
        pl.lit(False).alias("is_core"),
        pl.struct("series_id", "title").map_elements(
            lambda x: x["title"] if "[discontinued" in (x["title"] or "")
            else f"{x['title']} [discontinued; replaced by {retired[x['series_id']]}]", return_dtype=pl.Utf8).alias("title"),
        pl.lit(datetime.now(UTC)).cast(pl.Datetime("us", "UTC")).alias("updated_at"))
    write_table(ms, "macro_series", mode="upsert", settings=s)
    return {k: retired[k] for k in ms.get_column("series_id").to_list()}


def frozen_series(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """catalog `frozen: {series_id: {since, reason, note}}`: the source stopped and nothing replaces the series (A9
    2026-11-07: BoE IADB-only codes). Not fetched; the stored history stays served (unlike `retired`)."""
    out: dict[str, dict[str, Any]] = {}
    for k, v in ((catalog_doc(settings).get("frozen") or {}).items()):
        out[str(k)] = {kk: str(vv) for kk, vv in v.items()} if isinstance(v, dict) else {"note": str(v)}
    return out


def frozen_label(info: dict[str, Any]) -> str:
    return f"[frozen since {info.get('since') or '?'}: {info.get('reason') or 'source stopped'}]"


def freeze_series(sources: Iterable[str], settings: Settings | None = None) -> dict[str, str]:
    """catalog `frozen` series: macro_series title = catalog title + ' [frozen since <date>: <reason>]' (this also
    undoes a '[discontinued ...]' suffix written while the series was listed under `retired`), is_core as in the
    catalog. Observations untouched. Idempotent: only rows whose title or is_core change are written."""
    s = settings or get_settings()
    src = set(sources)
    fz = {k: v for k, v in frozen_series(s).items() if k.split(":", 1)[0] in src}
    if not fz:
        return {}
    try:
        from app.core.storage import read_table  # noqa: PLC0415

        ms = read_table("macro_series", settings=s).filter(pl.col("series_id").is_in(list(fz)))
    except Exception:  # noqa: BLE001
        return {}
    if ms.is_empty():
        return {}
    cat = {g.series_id: g for g in load_catalog(s, src)}

    def title(sid: str, cur: str | None) -> str:
        base = cat[sid].title if sid in cat else re.sub(r"\s*\[(discontinued|frozen)[^\]]*\]\s*$", "", cur or sid)
        return f"{base} {frozen_label(fz[sid])}"

    want = {r["series_id"]: (title(r["series_id"], r["title"]),
                             bool(cat[r["series_id"]].core) if r["series_id"] in cat else bool(r["is_core"]))
            for r in ms.iter_rows(named=True)}
    ch = ms.filter(pl.struct("series_id", "title", "is_core").map_elements(
        lambda x: (x["title"], bool(x["is_core"])) != want[x["series_id"]], return_dtype=pl.Boolean))
    if ch.height:
        ch = ch.with_columns(
            pl.col("series_id").replace_strict({k: v[0] for k, v in want.items()}, return_dtype=pl.Utf8).alias("title"),
            pl.col("series_id").replace_strict({k: v[1] for k, v in want.items()}, return_dtype=pl.Boolean)
            .alias("is_core"),
            pl.lit(datetime.now(UTC)).cast(pl.Datetime("us", "UTC")).alias("updated_at"))
        write_table(ch, "macro_series", mode="upsert", settings=s)
    return {k: frozen_label(fz[k]) for k in ms.get_column("series_id").to_list()}


def _save_report(rep: dict[str, Any], s: Settings) -> None:
    try:
        import orjson  # noqa: PLC0415

        d = s.logs_dir / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rep['job']}_{datetime.now(UTC).date().isoformat()}.json").write_bytes(
            orjson.dumps(rep, option=orjson.OPT_INDENT_2, default=str))
    except Exception as e:  # noqa: BLE001
        log.warning("could not save %s report: %s", rep.get("job"), e)


def run_job(ctx: Any, job: str, sources: list[str]) -> Any:
    p = ctx.params or {}
    start = date.fromisoformat(p["start"]) if p.get("start") else None
    only = p.get("sources") or p.get("series")
    rep = run_group(job, sources, ctx.settings, start, only)
    return job_result(rep)
