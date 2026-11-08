"""Bond reference data -> bond_reference. Jobs bonds_reference (weekly, FIRDS) and bonds_us_openfigi (resumable
filler campaign, OpenFIGI).

FIRDS (ESMA, official; robots allow; reuse with attribution): the weekly FULINS_D full file (4 zip parts, ~2 GB of
XML) is streamed with lxml iterparse - nothing but the kept fields stays in memory. Bonds only (CFI DB / DC / DW /
DT / DY / DG / DA / DN / DM); structured products / certificates (CFI DE / DS) are dropped. One row per ISIN (the
first venue record; n_venues counts them). Issuers are joined by LEI (issuers.lei).

OpenFIGI (free API, Bloomberg Open Symbology, no key: 5 searches / minute): per US issuer of our universe, /v3/search
query=<ticker> securityType2=Corp, keeping exchCode TRACE rows whose FIGI name matches the issuer; coupon / maturity
parsed from the security description. A state file (rates.json openfigi.done) makes the campaign resumable; each run
spends at most `max_requests` searches (default 120 = 24 min at the free rate).
"""

from __future__ import annotations

import re
import time
import zipfile
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger

from .common import client, now_utc, read_state, to_bp100, update_state

log = get_logger("app.rates.bonds")

FIRDS_SOLR = "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
BOND_CFI = ("DB", "DC", "DW", "DT", "DY", "DG", "DA", "DN", "DM")
SENIORITY = {"SNDB": "senior", "MZZD": "mezzanine", "SBOD": "subordinated", "JUND": "junior"}
SCHEMA = {"source": pl.Utf8, "bond_id": pl.Utf8, "isin": pl.Utf8, "figi": pl.Utf8, "issuer_lei": pl.Utf8,
          "issuer_id": pl.Utf8, "issuer_name": pl.Utf8, "full_name": pl.Utf8, "short_name": pl.Utf8, "cfi": pl.Utf8,
          "security_type": pl.Utf8, "currency": pl.Utf8, "coupon_type": pl.Utf8, "coupon_bp_x100": pl.Int32,
          "floating_reference": pl.Utf8, "floating_spread_bp": pl.Float32, "maturity_date": pl.Date,
          "first_trade_date": pl.Date, "issue_amount": pl.Float64, "issue_amount_currency": pl.Utf8,
          "nominal_per_unit": pl.Float64, "seniority": pl.Utf8, "venue_mic": pl.Utf8, "n_venues": pl.Int16,
          "country": pl.Utf8, "as_of": pl.Date, "knowledge_date": pl.Date}
CFI_GROUP = {"DB": "bond", "DC": "convertible", "DW": "bond with warrants", "DT": "medium-term note",
             "DY": "money market", "DG": "mortgage-backed", "DA": "asset-backed", "DN": "municipal", "DM": "other debt"}


def _date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def parse_firds_debt(fh: Any, as_of: date, keep_cfi: tuple[str, ...] = BOND_CFI) -> list[dict[str, Any]]:
    """Stream one FULINS_D XML (file object) -> one dict per RefData of a bond CFI (duplicates per venue kept;
    collapse with collapse_isins)."""
    from lxml import etree  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for _, el in etree.iterparse(fh, events=("end",), tag="{*}RefData", huge_tree=True):
        try:
            g = el.find("{*}FinInstrmGnlAttrbts")
            cfi = g.findtext("{*}ClssfctnTp") if g is not None else None
            if not cfi or cfi[:2] not in keep_cfi:
                continue
            isin = g.findtext("{*}Id")
            dbt = el.find("{*}DebtInstrmAttrbts")
            tv = el.find("{*}TradgVnRltdAttrbts")
            tech = el.find("{*}TechAttrbts")
            ctype, cpn, fref, fspr = None, None, None, None
            amt = amt_ccy = mat = npu = sen = None
            if dbt is not None:
                tot = dbt.find("{*}TtlIssdNmnlAmt")
                if tot is not None:
                    amt, amt_ccy = _num(tot.text), tot.get("Ccy")
                mat = _date(dbt.findtext("{*}MtrtyDt"))
                npu = _num(dbt.findtext("{*}NmnlValPerUnit"))
                sen = SENIORITY.get((dbt.findtext("{*}DebtSnrty") or "").strip(), None)
                ir = dbt.find("{*}IntrstRate")
                if ir is not None:
                    fx = ir.findtext("{*}Fxd")
                    if fx is not None:
                        v = _num(fx)
                        ctype = "zero" if v == 0 else "fixed"
                        cpn = to_bp100(v)
                    else:
                        fl = ir.find("{*}Fltg")
                        if fl is not None:
                            ctype = "floating"
                            ref = fl.find("{*}RefRate")
                            nm = None
                            if ref is not None:
                                nm = ref.findtext("{*}Nm") or ref.findtext("{*}Indx") or ref.findtext("{*}ISIN")
                            term = fl.find("{*}Term")
                            tl = ""
                            if term is not None:
                                tl = f" {term.findtext('{*}Val') or ''}{(term.findtext('{*}Unit') or '')[:1]}"
                            fref = ((nm or "") + tl).strip() or None
                            fspr = _num(fl.findtext("{*}BsisPtSprd"))
            out.append({"isin": isin, "full_name": g.findtext("{*}FullNm"), "short_name": g.findtext("{*}ShrtNm"),
                        "cfi": cfi, "currency": g.findtext("{*}NtnlCcy"), "issuer_lei": el.findtext("{*}Issr"),
                        "venue": tv.findtext("{*}Id") if tv is not None else None,
                        "first_trade_date": _date(tv.findtext("{*}FrstTradDt")) if tv is not None else None,
                        "rel_venue": tech.findtext("{*}RlvntTradgVn") if tech is not None else None,
                        "coupon_type": ctype or "other", "coupon_bp_x100": cpn, "floating_reference": fref,
                        "floating_spread_bp": fspr, "maturity_date": mat, "issue_amount": amt,
                        "issue_amount_currency": amt_ccy, "nominal_per_unit": npu, "seniority": sen, "as_of": as_of})
        finally:
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
    return out


def collapse_isins(rows: list[dict[str, Any]] | pl.DataFrame) -> pl.DataFrame:
    df = rows if isinstance(rows, pl.DataFrame) else pl.DataFrame(rows, infer_schema_length=None)
    if df.is_empty():
        return pl.DataFrame(schema=SCHEMA)
    agg = (df.sort("first_trade_date", nulls_last=True)
           .group_by("isin", maintain_order=True)
           .agg(pl.all().exclude("isin", "venue").first(), pl.col("venue").n_unique().alias("n_venues")))
    return agg.with_columns(
        pl.lit("firds").alias("source"), pl.col("isin").alias("bond_id"), pl.lit(None, pl.Utf8).alias("figi"),
        pl.lit(None, pl.Utf8).alias("issuer_id"), pl.lit(None, pl.Utf8).alias("issuer_name"),
        pl.col("cfi").str.slice(0, 2).replace_strict(CFI_GROUP, default="other debt").alias("security_type"),
        pl.col("rel_venue").alias("venue_mic"), pl.col("n_venues").cast(pl.Int16),
        pl.col("isin").str.slice(0, 2).alias("country"), pl.col("as_of").alias("knowledge_date"),
    ).select(list(SCHEMA))


def latest_full_files(s: Settings) -> tuple[str | None, list[dict[str, Any]]]:
    end = date.today()
    start = end - timedelta(days=13)
    q = {"q": "*", "fq": f"publication_date:[{start.isoformat()}T00:00:00Z TO {end.isoformat()}T23:59:59Z]",
         "wt": "json", "rows": 500}
    docs = client("firds", s).get(FIRDS_SOLR, params=q, cache_ttl=6 * 3600).json()["response"]["docs"]
    full = [d for d in docs if str(d.get("file_name", "")).startswith("FULINS_D_")]
    if not full:
        return None, []
    latest = max(d["file_name"].split("_")[2] for d in full)
    return latest, sorted([d for d in full if d["file_name"].split("_")[2] == latest], key=lambda d: d["file_name"])


def join_issuers(df: pl.DataFrame, s: Settings) -> pl.DataFrame:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        iss = (read_table("issuers", columns=["issuer_id", "lei", "name", "updated_at"], settings=s)
               .drop_nulls("lei").sort("updated_at").unique("lei", keep="last")
               .select(pl.col("lei").str.to_uppercase().alias("issuer_lei"), pl.col("issuer_id").alias("_iid"),
                       pl.col("name").alias("_nm")))
    except Exception as e:  # noqa: BLE001
        log.warning("bonds: issuers unreadable: %s", e)
        return df
    return (df.join(iss, on="issuer_lei", how="left")
            .with_columns(pl.coalesce("issuer_id", "_iid").alias("issuer_id"),
                          pl.coalesce("issuer_name", "_nm").alias("issuer_name")).drop("_iid", "_nm"))


def run_firds(s: Settings, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    params = params or {}
    latest, parts = latest_full_files(s)
    if not parts:
        return {"rows": 0, "notes": ["FIRDS: no FULINS_D file in the last 2 weeks"], "requests": 1}
    as_of = datetime.strptime(latest, "%Y%m%d").date()
    st = read_state(s).get("firds") or {}
    if st.get("as_of") == as_of.isoformat() and not params.get("force"):
        return {"rows": 0, "notes": [f"FIRDS {latest} already loaded"], "requests": 1, "as_of": latest}
    raw = s.raw_dir / "rates" / "firds" / f"dt={as_of.isoformat()}"
    raw.mkdir(parents=True, exist_ok=True)
    frames = []
    n_req = 1
    for d in parts:
        dest = raw / d["file_name"]
        if not dest.exists():
            client("firds", s, timeout=900).download(d["download_link"], dest)
            n_req += 1
        t0 = time.perf_counter()
        with zipfile.ZipFile(dest) as z, z.open(z.namelist()[0]) as fh:
            rows = parse_firds_debt(fh, as_of)
        frames.append(collapse_isins(rows))
        log.info("FIRDS %s: %d bond records -> %d ISINs in %.0fs", d["file_name"], len(rows), frames[-1].height,
                 time.perf_counter() - t0)
        del rows
    df = pl.concat(frames, how="vertical_relaxed").unique("bond_id", keep="first")
    df = join_issuers(df, s).with_columns(pl.lit(now_utc()).alias("ingested_at"))
    n = write_table(df, "bond_reference", mode="overwrite_partition", settings=s)
    matched = int(df.filter(pl.col("issuer_id").is_not_null()).height)
    update_state({"firds": {"as_of": as_of.isoformat(), "rows": n, "matched_issuers": int(
        df.filter(pl.col("issuer_id").is_not_null())["issuer_id"].n_unique()), "matched_bonds": matched,
        "loaded_at": now_utc().isoformat(timespec="seconds")}}, s)
    # keep only the two newest raw file sets
    olds = sorted(p for p in (s.raw_dir / "rates" / "firds").glob("dt=*") if p.is_dir())[:-2]
    for p in olds:
        for f in p.glob("*"):
            f.unlink(missing_ok=True)
        p.rmdir()
    return {"rows": n, "notes": [], "requests": n_req, "as_of": latest, "matched_bonds": matched}


# =============================================================================================== OpenFIGI (US)

OPENFIGI = "https://api.openfigi.com/v3/search"
_DESC = re.compile(r"^(?P<tk>\S+)\s+(?P<cpn>F(?:LOAT)?|\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s+(?P<mat>\d{2}/\d{2}/\d{2,4}|PERP)")


def parse_figi_description(desc: str) -> dict[str, Any]:
    """'AAPL 3 1/4 08/08/29' -> coupon 3.25, maturity 2029-08-08; 'AAPL Float 08/28/19' -> floating;
    'X 0 01/15/30' -> zero; 'PERP' -> no maturity."""
    out: dict[str, Any] = {"coupon_type": "other", "coupon": None, "maturity_date": None}
    s = (desc or "").upper().replace("FLOAT", "F")
    m = _DESC.match(s)
    if not m:
        return out
    c = m.group("cpn")
    if c.startswith("F"):
        out["coupon_type"] = "floating"
    else:
        parts = c.split()
        v = 0.0
        for p in parts:
            if "/" in p:
                a, b = p.split("/")
                v += int(a) / int(b)
            else:
                v += float(p)
        out["coupon"] = v
        out["coupon_type"] = "zero" if v == 0 else "fixed"
    mt = m.group("mat")
    if mt != "PERP":
        mm, dd, yy = mt.split("/")
        y = int(yy)
        if y < 100:
            y += 2000 if y < 70 else 1900
        try:
            out["maturity_date"] = date(y, int(mm), int(dd))
        except ValueError:
            pass
    return out


def _norm(n: str | None) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", (n or "").upper())
    n = re.sub(r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|LLC|HOLDINGS?|GROUP|THE|NV|SA|AG)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def figi_rows(data: list[dict[str, Any]], issuer_id: str, ticker: str, issuer_name: str, as_of: date) -> list[dict[str, Any]]:
    want = _norm(issuer_name)
    out = []
    for d in data:
        if d.get("exchCode") != "TRACE" or d.get("securityType2") != "Corp":
            continue
        tk = (d.get("ticker") or "").split()
        if not tk or tk[0] != ticker.upper():
            continue
        nm = _norm(d.get("name"))
        if want and nm and not (nm.startswith(want[:12]) or want.startswith(nm[:12])):
            continue
        p = parse_figi_description(d.get("securityDescription") or d.get("ticker") or "")
        out.append({"source": "openfigi", "bond_id": d["figi"], "isin": None, "figi": d["figi"], "issuer_lei": None,
                    "issuer_id": issuer_id, "issuer_name": d.get("name"), "full_name": d.get("securityDescription"),
                    "short_name": d.get("ticker"), "cfi": None, "security_type": d.get("securityType"),
                    "currency": "USD", "coupon_type": p["coupon_type"], "coupon_bp_x100": to_bp100(p["coupon"]),
                    "floating_reference": None, "floating_spread_bp": None, "maturity_date": p["maturity_date"],
                    "first_trade_date": None, "issue_amount": None, "issue_amount_currency": None,
                    "nominal_per_unit": None, "seniority": None, "venue_mic": "TRACE", "n_venues": 1, "country": "US",
                    "as_of": as_of, "knowledge_date": as_of})
    return out


def us_targets(s: Settings, limit: int) -> list[tuple[str, str, str]]:
    """(issuer_id, ticker, name) of US primary common stocks, largest first (serving snapshot sec table)."""
    from app.core.storage import current_serving_db  # noqa: PLC0415

    p = current_serving_db(s)
    if p is None:
        return []
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(str(p), read_only=True)
    try:
        rows = con.execute("""SELECT issuer_id, ticker, name FROM sec WHERE region = 'US' AND ticker IS NOT NULL
                              AND issuer_id IS NOT NULL ORDER BY market_cap_usd DESC NULLS LAST LIMIT ?""", [limit]).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("openfigi: serving sec unreadable: %s", e)
        rows = []
    finally:
        con.close()
    seen, out = set(), []
    for iid, tk, nm in rows:
        if iid in seen:
            continue
        seen.add(iid)
        out.append((iid, str(tk).split()[0].split(".")[0], nm))
    return out


def run_openfigi(s: Settings, params: dict[str, Any] | None = None, cancel: Any = None) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    params = params or {}
    budget = int(params.get("max_requests", 120))
    pages = int(params.get("max_pages", 3))
    refresh_days = int(params.get("refresh_days", 90))
    st = read_state(s).get("openfigi") or {}
    done: dict[str, str] = dict(st.get("done") or {})
    today = date.today()
    targets = us_targets(s, int(params.get("universe", 1500)))
    todo = [t for t in targets if not done.get(t[0]) or
            (today - date.fromisoformat(done[t[0]])).days >= refresh_days]
    c = client("openfigi", s, headers={"Content-Type": "application/json"})
    used, rows, n_iss = 0, [], 0
    for iid, tk, nm in todo:
        if used >= budget:
            break
        if cancel is not None:
            cancel()
        got: list[dict[str, Any]] = []
        nxt = None
        for _ in range(pages):
            if used >= budget:
                break
            body: dict[str, Any] = {"query": tk, "securityType2": "Corp"}
            if nxt:
                body["start"] = nxt
            try:
                j = c.post(OPENFIGI, json_body=body).json()
            except Exception as e:  # noqa: BLE001
                log.warning("openfigi %s: %s", tk, e)
                used += 1
                break
            used += 1
            got += figi_rows(j.get("data") or [], iid, tk, nm, today)
            nxt = j.get("next")
            if not nxt:
                break
        rows += got
        done[iid] = today.isoformat()
        n_iss += 1
    n = 0
    if rows:
        df = pl.DataFrame(rows, schema=SCHEMA).unique("bond_id", keep="last").with_columns(
            pl.lit(now_utc()).alias("ingested_at"))
        n = write_table(df, "bond_reference", mode="upsert", settings=s)
    update_state({"openfigi": {"done": done, "targets": len(targets), "last_run": now_utc().isoformat(timespec="seconds"),
                               "remaining": max(0, len(todo) - n_iss)}}, s)
    return {"rows": n, "issuers": n_iss, "requests": used, "remaining": max(0, len(todo) - n_iss), "notes": []}


def run(ctx):  # noqa: ANN001 - job bonds_reference (FIRDS)
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_firds(ctx.settings, dict(ctx.params or {}))
    ok = rep["rows"] > 0 or any("already loaded" in x for x in rep["notes"])
    return JobResult(status="success" if ok else "failed", rows_written={"bond_reference": rep["rows"]},
                     http_requests=rep["requests"], message="; ".join(rep["notes"]) or None)


def run_openfigi_job(ctx):  # noqa: ANN001 - job bonds_us_openfigi
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_openfigi(ctx.settings, dict(ctx.params or {}), ctx.check_cancelled)
    return JobResult(status="success", rows_written={"bond_reference": rep["rows"]}, http_requests=rep["requests"],
                     entities_ok=rep["issuers"], message=f"{rep['issuers']} issuers, {rep['remaining']} remaining")


def probe_progress(settings: Settings | None = None) -> dict[str, Any]:
    st = read_state(settings).get("openfigi") or {}
    tot = int(st.get("targets") or 0)
    done = len(st.get("done") or {})
    return {"done": tot > 0 and not st.get("remaining"), "pct": round(100 * done / tot, 1) if tot else None,
            "done_units": done, "total_units": tot or None, "unit": "issuers", "last_run_at": st.get("last_run"),
            "detail": f"US issuers searched on OpenFIGI: {done}/{tot or '?'}"}
