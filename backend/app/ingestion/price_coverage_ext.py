"""Price coverage, part 2: identity seeds for the new markets (`seeds`), paced yfinance backfills (`yf`, P2) and the US
delisted pass (`delisted`, P3). CLI via app.ingestion.price_coverage; state in data/state/price_coverage.json."""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.ingestion.price_coverage import _append, _pid, _s, mark_done, read_done, update_state

log = get_logger("app.ingestion.price_coverage")

SEED_COLS = ["ticker", "mic", "yf_ticker", "isin", "name", "country", "region", "adr_ticker", "issuer_group",
             "share_class", "notes", "lei", "fye_mmdd", "native_industry", "coverage_source"]
ALL_MARKETS = ("cn", "tw", "br", "jp", "kr", "hk")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _rows(df: pl.DataFrame) -> pl.DataFrame:
    for c in SEED_COLS:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Utf8).alias(c))
    return df.select([pl.col(c).cast(pl.Utf8) for c in SEED_COLS])


# ------------------------------------------------------------------------------------------------ per-market lists


def seeds_cn(s: Settings) -> pl.DataFrame:
    from app.ingestion.price_coverage import cn_list  # noqa: PLC0415

    df = cn_list(s, max_age_h=24 * 7)
    return _rows(df.select(
        pl.col("yf_ticker").str.slice(0, 6).alias("ticker"),
        pl.when(pl.col("yf_ticker").str.ends_with(".SS")).then(pl.lit("XSHG")).otherwise(pl.lit("XSHE")).alias("mic"),
        "yf_ticker", "name", pl.lit("CN").alias("country"), pl.lit("CN").alias("region"),
        ("ipo " + pl.col("ipo_date").fill_null("")).alias("notes"),
        pl.col("csrc_industry").alias("native_industry"), pl.lit("baostock").alias("coverage_source")))


def tw_isin(code: str) -> str:
    from app.normalize.identity_universe import isin_check_digit  # noqa: PLC0415

    body = f"TW000{code}00"
    return body + isin_check_digit(body)


def parse_tw_companies(twse: list[dict[str, Any]], tpex: list[dict[str, Any]]) -> pl.DataFrame:
    """TWSE t187ap03_L + TPEx mopsfin_t187ap03_O company masters -> seed rows (ordinary shares, 4-digit codes).
    ISIN TW000<code>00<check> only for domestic companies (KY / foreign-registered issuers have non-TW ISINs)."""
    out: list[dict[str, Any]] = []

    def pick(r: dict[str, Any], *keys: str) -> str | None:
        for k in keys:
            v = r.get(k)
            if v is not None and str(v).strip() not in ("", "－", "-"):
                return str(v).strip()
        return None

    for src, rows, mic, suf in (("twse", twse, "XTAI", ".TW"), ("tpex", tpex, "ROCO", ".TWO")):
        for r in rows or []:
            code = pick(r, "公司代號", "SecuritiesCompanyCode")
            if not code or not re.fullmatch(r"[1-9]\d{3}", code):
                continue
            foreign = pick(r, "外國企業註冊地國", "Registration")
            name = pick(r, "英文簡稱", "EnglishAbbreviation", "Symbol", "EnglishName", "英文全名") or pick(
                r, "公司簡稱", "CompanyAbbreviation") or code
            out.append({"ticker": code, "mic": mic, "yf_ticker": code + suf,
                        "isin": None if foreign else tw_isin(code), "name": name, "country": "TW", "region": "APAC",
                        "notes": f"{src} list; local name {pick(r, '公司名稱', 'CompanyName') or ''}".strip(),
                        "native_industry": pick(r, "產業別", "SecuritiesIndustryCode"), "coverage_source": src})
    return _rows(pl.DataFrame(out, infer_schema_length=None)) if out else _rows(pl.DataFrame())


def seeds_tw(s: Settings) -> pl.DataFrame:
    from app.core.http import HttpClient  # noqa: PLC0415

    h1 = HttpClient("twse", settings=s, headers={"User-Agent": _UA}, timeout=90)
    h2 = HttpClient("tpex", settings=s, headers={"User-Agent": _UA}, timeout=90)
    twse = h1.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", cache_ttl=86400).json()
    tpex = h2.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", cache_ttl=86400).json()
    d = s.raw_dir / "twse" / "company_list"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t187ap03_L.json").write_text(json.dumps(twse, ensure_ascii=False), encoding="utf-8")
    (d / "mopsfin_t187ap03_O.json").write_text(json.dumps(tpex, ensure_ascii=False), encoding="utf-8")
    return parse_tw_companies(twse, tpex)


_B3_SEED_LAYOUT = [("TIPREG", 1, 2), ("DATA", 3, 10), ("CODBDI", 11, 12), ("CODNEG", 13, 24), ("TPMERC", 25, 27),
                   ("NOMRES", 28, 39), ("ESPECI", 40, 49), ("CODISI", 231, 242)]
_B3_CLASSES = {"ON", "PN", "PNA", "PNB", "PNC", "PND", "PNE", "PNF", "UNT"}


def parse_b3_listed(text: str, recent_days: int = 120) -> pl.DataFrame:
    """COTAHIST text -> one seed row per equity ticker (cash market, standard lot, ON/PN*/UNT) traded within the last
    `recent_days` of the file. issuer_group B3:<4-letter root> (PETR3/PETR4 -> one issuer)."""
    lines = pl.DataFrame({"l": text.splitlines()}).filter(pl.col("l").str.starts_with("01"))
    if lines.is_empty():
        return _rows(pl.DataFrame())
    df = lines.select([pl.col("l").str.slice(a - 1, b - a + 1).str.strip_chars().alias(n) for n, a, b in _B3_SEED_LAYOUT])
    df = df.filter((pl.col("TPMERC") == "010") & (pl.col("CODBDI") == "02")).with_columns(
        pl.col("DATA").str.to_date("%Y%m%d").alias("d"),
        pl.col("ESPECI").str.split(" ").list.first().alias("cls"))
    last = df.get_column("d").max()
    df = df.filter((pl.col("d") >= last - timedelta(days=recent_days)) & pl.col("cls").is_in(list(_B3_CLASSES)))
    df = df.sort("d").unique(subset=["CODNEG"], keep="last")
    return _rows(df.select(
        pl.col("CODNEG").alias("ticker"), pl.lit("BVMF").alias("mic"), (pl.col("CODNEG") + ".SA").alias("yf_ticker"),
        pl.col("CODISI").alias("isin"), pl.col("NOMRES").alias("name"), pl.lit("BR").alias("country"),
        pl.lit("LATAM").alias("region"), ("B3:" + pl.col("CODNEG").str.slice(0, 4)).alias("issuer_group"),
        pl.col("cls").alias("share_class"), ("b3 especi " + pl.col("ESPECI")).alias("notes"),
        pl.lit("b3_cotahist").alias("coverage_source")))


def seeds_br(s: Settings) -> pl.DataFrame:
    d = s.raw_dir / "b3" / "cotahist"
    files = sorted(d.glob("COTAHIST_A*.ZIP"), key=lambda p: p.name)
    if not files:
        raise FileNotFoundError("no COTAHIST yearly file landed yet (run price_coverage br first)")
    z = zipfile.ZipFile(io.BytesIO(files[-1].read_bytes()))
    return parse_b3_listed(z.read(z.namelist()[0]).decode("latin-1"))


def seeds_jp(s: Settings) -> pl.DataFrame:
    df = pl.read_csv(s.raw_dir / "jpx_list" / "latest" / "jpx_listed.csv", infer_schema_length=0)
    df = df.filter(pl.col("market").str.contains("内国株式"))
    return _rows(df.select(pl.col("code").alias("ticker"), pl.lit("XTKS").alias("mic"),
                           (pl.col("code") + ".T").alias("yf_ticker"), "name", pl.lit("JP").alias("country"),
                           pl.lit("JP").alias("region"), ("jpx " + pl.col("market")).alias("notes"),
                           pl.col("tse33_code").alias("native_industry"), pl.lit("jpx").alias("coverage_source")))


def seeds_kr(s: Settings) -> pl.DataFrame:
    df = pl.read_csv(s.raw_dir / "kind_list" / "latest" / "kind_listed.csv", infer_schema_length=0)
    df = df.filter(pl.col("market").str.starts_with("유가") | (pl.col("market") == "코스닥")).with_columns(
        pl.col("stock_code").str.zfill(6))
    return _rows(df.select(
        pl.col("stock_code").alias("ticker"),
        pl.when(pl.col("market") == "코스닥").then(pl.lit("XKOS")).otherwise(pl.lit("XKRX")).alias("mic"),
        (pl.col("stock_code") + pl.when(pl.col("market") == "코스닥").then(pl.lit(".KQ")).otherwise(pl.lit(".KS")))
        .alias("yf_ticker"), "name", pl.lit("KR").alias("country"), pl.lit("KR").alias("region"),
        ("kind " + pl.col("market")).alias("notes"), pl.col("ksic_industry").alias("native_industry"),
        pl.lit("kind").alias("coverage_source")))


def seeds_hk(s: Settings) -> pl.DataFrame:
    from app.normalize.identity_universe import fetch_hkex_list  # noqa: PLC0415

    df = fetch_hkex_list(s)
    df = df.filter(pl.col("sub_category").fill_null("").str.contains("Main Board")
                   & (pl.col("hk_code").cast(pl.Int64, strict=False) < 10000))
    return _rows(df.select(
        pl.col("hk_code").cast(pl.Int64).cast(pl.Utf8).str.zfill(4).alias("ticker"), pl.lit("XHKG").alias("mic"),
        (pl.col("hk_code").cast(pl.Int64).cast(pl.Utf8).str.zfill(4) + ".HK").alias("yf_ticker"), "isin", "name",
        pl.lit("HK").alias("country"), pl.lit("HK").alias("region"), pl.lit("hkex main board").alias("notes"),
        pl.lit("hkex").alias("coverage_source")))


SEED_FNS = {"cn": seeds_cn, "tw": seeds_tw, "br": seeds_br, "jp": seeds_jp, "kr": seeds_kr, "hk": seeds_hk}


def task_seeds(markets: list[str], settings: Settings | None = None) -> dict[str, Any]:
    """(Re)write data/reference/seeds/price_coverage.csv. Markets not requested keep their previous rows."""
    from app.normalize.identity_universe import seeds_dir  # noqa: PLC0415

    s = _s(settings)
    p = seeds_dir(s) / "price_coverage.csv"
    prev = pl.read_csv(p, infer_schema_length=0) if p.exists() else _rows(pl.DataFrame())
    frames, counts, errors = [], {}, {}
    src_of = {"cn": ["baostock"], "tw": ["twse", "tpex"], "br": ["b3_cotahist"], "jp": ["jpx"], "kr": ["kind"],
              "hk": ["hkex"]}
    for m in markets:
        try:
            f = SEED_FNS[m](s)
            frames.append(f)
            counts[m] = f.height
        except Exception as e:  # noqa: BLE001 - keep previous rows of a failed market
            errors[m] = f"{type(e).__name__}: {e}"[:300]
            log.exception("seeds %s failed", m)
    keep_src = [x for m in markets if m not in errors for x in src_of[m]]
    kept = _rows(prev).filter(~pl.col("coverage_source").is_in(keep_src))
    out = pl.concat([kept, *frames], how="vertical").unique(subset=["yf_ticker"], keep="last", maintain_order=True)
    tmp = p.with_suffix(".tmp")
    out.write_csv(tmp)
    tmp.replace(p)
    by_src = dict(out.group_by("coverage_source").len().iter_rows())
    update_state("seeds", s, status="done", path=str(p), rows=out.height, by_source=by_src, last_markets=markets,
                 errors=errors)
    return {"rows": out.height, "by_source": by_src, "counts": counts, "errors": errors}


# ------------------------------------------------------------------------------------------------ P2 paced yfinance


def eu_chain_busy(settings: Settings | None = None) -> str | None:
    """Name of the EU-expansion step that uses Yahoo right now (3_universe / 4_prices running), else None."""
    p = _s(settings).data_dir / "state" / "eu_expansion.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8")).get("steps", {})
    except Exception:  # noqa: BLE001
        return None
    for k in ("3_universe", "4_prices"):
        if (st.get(k) or {}).get("status") not in ("done", "pending", None):
            return k
    if (st.get("3_universe") or {}).get("status") == "done" and (st.get("4_prices") or {}).get("status") == "pending":
        return "4_prices(next)"  # chain will start its Yahoo price step right away
    return None


def yf_symbols(group: str, settings: Settings | None = None) -> list[str]:
    from app.ingestion.price_coverage_lists import extended_benchmarks  # noqa: PLC0415
    from app.normalize.identity_universe import seeds_dir  # noqa: PLC0415

    s = _s(settings)
    if group in ("etf", "idx", "fut", "bench"):
        kinds = {"etf": {"etf"}, "idx": {"index_ext"}, "fut": {"future"}, "bench": {"etf", "index_ext", "future"}}[group]
        return [t for t, _n, _r, _c, k, _cal in extended_benchmarks() if k in kinds]
    src = {"jp": "jpx", "kr": "kind", "hk": "hkex"}[group]
    df = pl.read_csv(seeds_dir(s) / "price_coverage.csv", infer_schema_length=0)
    return sorted(df.filter(pl.col("coverage_source") == src).get_column("yf_ticker").to_list())


def task_yf(group: str, chunk: int = 100, settings: Settings | None = None, max_wait_h: float = 24.0) -> dict[str, Any]:
    """Paced period=max yfinance backfill of one group, resumable per chunk. Waits while the EU chain uses Yahoo; a
    chunk that returns (almost) nothing is treated as throttling (HTTP 429) -> 10/20/40/60-min backoff, then retry."""
    from app.ingestion.yf_prices import YFinancePricesAdapter  # noqa: PLC0415

    s = _s(settings)
    task = f"p2_yf_{group}"
    syms = yf_symbols(group, s)
    done = read_done(task, s)
    todo = [x for x in syms if x not in done]
    update_state(task, s, status="running", pid=_pid(), n_total=len(syms), n_done=len(syms) - len(todo))
    t0 = time.time()
    rows = acts = 0
    throttle_events: list[str] = []
    nodata: list[str] = []
    i = 0
    backoff = 0
    while i < len(todo):
        busy = eu_chain_busy(s)
        if busy:
            if time.time() - t0 > max_wait_h * 3600:
                update_state(task, s, status="paused", reason=f"EU chain busy ({busy}) > {max_wait_h} h")
                return {"status": "paused"}
            update_state(task, s, status="waiting_eu_chain", waiting_for=busy)
            time.sleep(300)
            continue
        part = todo[i:i + chunk]
        ad = YFinancePricesAdapter(s, batch_size=chunk, pause_s=5.0)
        fr = ad.fetch(entities=part, period="max")
        ok = set(fr.entities_ok)
        if len(ok) < max(1, len(part) // 10) and backoff < 4:  # throttled (yfinance swallows 429 -> empty frames)
            wait = [600, 1200, 2400, 3600][backoff]
            backoff += 1
            throttle_events.append(f"{datetime.now(UTC).isoformat(timespec='seconds')} chunk@{i}: {len(ok)}/{len(part)} "
                                   f"-> sleep {wait}s")
            update_state(task, s, status="backoff", throttle_events=throttle_events[-20:], n_throttle=len(throttle_events))
            time.sleep(wait)
            continue
        backoff = 0
        tables = ad.normalize(fr)
        rows += _append(tables.get("prices_staging"), "prices_staging", s)
        acts += _append(tables.get("corporate_actions_staging"), "corporate_actions_staging", s)
        nodata += [x for x in part if x not in ok]
        mark_done(task, part, s)
        i += len(part)
        el = time.time() - t0
        update_state(task, s, status="running", n_done=len(syms) - len(todo) + i, rows=rows, actions=acts,
                     n_nodata=len(nodata), nodata_sample=nodata[-15:], n_throttle=len(throttle_events),
                     throttle_events=throttle_events[-20:], elapsed_s=round(el),
                     eta_s=round(el / max(1, i) * (len(todo) - i)))
    update_state(task, s, status="done", rows=rows, actions=acts, runtime_s=round(time.time() - t0),
                 n_nodata=len(nodata), nodata_sample=nodata[-30:])
    return {"status": "done", "rows": rows, "nodata": len(nodata)}


# ------------------------------------------------------------------------------------------------ dispatch


def dispatch(task: str, args: Any) -> dict[str, Any]:
    if task == "seeds":
        markets = [m.strip() for m in (args.markets or "cn,tw,br").split(",") if m.strip()]
        return task_seeds(markets)
    if task == "yf":
        return task_yf(args.group or "bench")
    if task == "delisted":
        from app.ingestion.price_coverage_delisted import task_delisted  # noqa: PLC0415

        return task_delisted()
    raise SystemExit(f"unknown task {task}")


__all__ = ["task_seeds", "task_yf", "parse_tw_companies", "parse_b3_listed", "eu_chain_busy", "yf_symbols"]
