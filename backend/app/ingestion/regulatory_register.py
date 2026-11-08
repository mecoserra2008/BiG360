"""Regulatory registers of listed equity issuers: US (SEC), EU (ESMA FIRDS), UK (FCA FIRDS + AIM) -> regulatory_register.

Coverage v2 W2 (docs/COVERAGE_V2_PLAN.md). The register is the denominator of "complete coverage":

  US  `sec_listed`        every CIK of SEC company_tickers_exchange.json with an exchange listing (Nasdaq, NYSE,
                          Cboe, TXSE) that filed a 10-K / 10-Q / 20-F / 40-F (+/A, 10-KT) in the last 9 quarters of the
                          EDGAR full-index (master.gz, one file per quarter: closed quarters are immutable and cached).
                          OTC-only tickers, investment companies (N-CSR) and listed registrants without a periodic
                          report yet are kept as status `excluded` with the reason.
  EU  `esma_rm`           issuer LEIs of shares (CFI ES/EP/EC/EF/EL) admitted to an EEA regulated market at the
                          issuer's request (FIRDS IssrReq), not terminated (app.ingestion.firds).
  UK  `fca_official_list` the same rule on the UK regulated markets in FCA FIRDS (LSE Main Market = the equity part of
                          the FCA Official List; the FCA Official List web search itself is robots-disallowed).
      `lse_aim`           shares admitted to AIM (FCA FIRDS venue AIMX, the AIM segment MIC of XLON).

Names, legal country / jurisdiction and the national company number (Companies House number for GB entities) come
from the GLEIF API (batched, cached 30 days by app.normalize.identity_universe.fetch_gleif_records).

Universe mapping: CIK / LEI / ISIN through identifier_map -> issuer_id + its primary active security. EU / UK register
issuers that are not in securities_master yet are turned into seed rows (data/reference/seeds/regulatory_register.csv,
ISIN -> home listing via OpenFIGI (cached) and the eu_census listings, Yahoo symbol verified by a 3-month download);
ref_universe adds them like the price-coverage seeds (members of `full`).

    python -m app.ingestion.regulatory_register [--markets US,EU,UK] [--no-seeds] [--dry-run]
    job regulatory_register (nightly; FIRDS / SEC lists are re-parsed only when a new file is published)
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ingestion import firds as F

log = get_logger(__name__)

STATE_FILE = "regulatory_register.json"
US_LISTED_EXCHANGES = ("Nasdaq", "NYSE", "CBOE", "TXSE")
PERIODIC_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
FUND_FORMS = {"N-CSR", "N-CSRS", "N-CSR/A", "N-CSRS/A", "NPORT-P", "N-PX", "N-2", "N-2/A", "N-CEN", "485BPOS"}
NEW_REGISTRANT_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B1", "8-A12B", "8-A12B/A", "S-11", "10-12B", "F-4", "S-4"}
INDEX_QUARTERS = 9
UK_AIM_MICS = ("AIMX",)
REGISTER_MARKET = {"sec_listed": "US", "esma_rm": "EU", "fca_official_list": "UK", "lse_aim": "UK"}

REGISTER_SCHEMA: dict[str, Any] = {
    "market": pl.Utf8, "register": pl.Utf8, "issuer_key": pl.Utf8, "issuer_name": pl.Utf8, "lei": pl.Utf8,
    "cik": pl.Utf8, "company_number": pl.Utf8, "country": pl.Utf8, "jurisdiction": pl.Utf8,
    "instruments": pl.List(pl.Utf8), "tickers": pl.List(pl.Utf8), "venues": pl.List(pl.Utf8),
    "primary_venue": pl.Utf8, "competent_authority": pl.Utf8, "first_admission": pl.Date, "filer_category": pl.Utf8,
    "last_annual_form": pl.Utf8, "last_annual_filed": pl.Date, "last_interim_filed": pl.Date, "status": pl.Utf8,
    "status_reason": pl.Utf8, "issuer_id": pl.Utf8, "security_id": pl.Utf8, "match_method": pl.Utf8,
    "as_of": pl.Date, "source_file": pl.Utf8,
}


# ------------------------------------------------------------------------------------------------ state


def state_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / STATE_FILE


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any], settings: Settings | None = None) -> None:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def _today() -> date:
    return datetime.now(UTC).date()


# ------------------------------------------------------------------------------------------------ US (pure parts)


def quarters_back(today: date, n: int) -> list[tuple[int, int]]:
    """The current quarter and the n-1 before it, oldest first: [(2024, 4), ..., (2026, 4)]."""
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out[::-1]


def periodic_summary(entries: pl.DataFrame) -> pl.DataFrame:
    """EDGAR index entries (cik, form, filed) -> per CIK: last annual form / date, last 10-Q date, latest periodic
    form, any fund / new-registrant form (for the exclusion reasons)."""
    if entries.is_empty():
        return pl.DataFrame(schema={"cik": pl.Utf8, "last_annual_form": pl.Utf8, "last_annual_filed": pl.Date,
                                    "last_interim_filed": pl.Date, "last_periodic_form": pl.Utf8,
                                    "last_periodic_filed": pl.Date, "fund_forms": pl.Boolean, "new_registrant": pl.Date})
    e = entries.with_columns(pl.col("filed").cast(pl.Date))
    ann = (e.filter(pl.col("form").is_in(list(ANNUAL_FORMS))).sort("filed")
           .group_by("cik").agg(pl.col("form").last().alias("last_annual_form"),
                                pl.col("filed").last().alias("last_annual_filed")))
    q = (e.filter(pl.col("form").is_in(["10-Q", "10-Q/A"])).group_by("cik")
         .agg(pl.col("filed").max().alias("last_interim_filed")))
    per = (e.filter(pl.col("form").is_in(list(PERIODIC_FORMS))).sort("filed")
           .group_by("cik").agg(pl.col("form").last().alias("last_periodic_form"),
                                pl.col("filed").last().alias("last_periodic_filed")))
    fund = e.filter(pl.col("form").is_in(list(FUND_FORMS))).select("cik").unique().with_columns(pl.lit(True).alias("fund_forms"))
    new = (e.filter(pl.col("form").is_in(list(NEW_REGISTRANT_FORMS))).group_by("cik")
           .agg(pl.col("filed").max().alias("new_registrant")))
    ciks = e.select("cik").unique()
    out = ciks
    for f in (ann, q, per, fund, new):
        out = out.join(f, on="cik", how="left")
    return out.with_columns(pl.col("fund_forms").fill_null(False))


def filer_category_of(form: str | None) -> str | None:
    f = (form or "").upper()
    if f.startswith(("10-K", "10-Q")):
        return "domestic"
    if f.startswith("20-F"):
        return "foreign_private_20f"
    if f.startswith("40-F"):
        return "foreign_40f"
    return None


def build_us(tickers: pl.DataFrame, periodic: pl.DataFrame, today: date, source_file: str | None = None,
             months: int = 24) -> pl.DataFrame:
    """SEC tickers (cik, name, ticker, exchange, mic, sec_rank) + periodic_summary -> US register rows."""
    t = tickers.filter(pl.col("cik").is_not_null())
    g = (t.sort("sec_rank").group_by("cik", maintain_order=True)
         .agg(pl.col("name").first().alias("issuer_name"), pl.col("ticker").unique(maintain_order=True).alias("tickers"),
              pl.col("exchange").drop_nulls().unique(maintain_order=True).alias("exchanges"),
              pl.col("mic").drop_nulls().unique(maintain_order=True).alias("venues"),
              pl.col("exchange").is_in(list(US_LISTED_EXCHANGES)).any().alias("listed")))
    g = g.join(periodic, on="cik", how="left")
    cutoff = today - timedelta(days=int(months * 30.44))
    recent_new = today - timedelta(days=365)
    rows = []
    for r in g.iter_rows(named=True):
        venues = [v for v in (r["venues"] or []) if v != "OTCM"]
        if not r["listed"]:
            status, reason = "excluded", "OTC quotation only (not exchange-listed)"
        elif r.get("last_periodic_filed") and r["last_periodic_filed"] >= cutoff:
            status, reason = "active", None
        elif r.get("fund_forms"):
            status, reason = "excluded", "investment company (files N-CSR / N-PORT, not 10-K / 10-Q)"
        elif r.get("new_registrant") and r["new_registrant"] >= recent_new:
            status, reason = "excluded", f"new registrant: no periodic report yet (registration {r['new_registrant']})"
        elif r.get("last_periodic_filed"):
            status, reason = "excluded", f"no periodic report since {r['last_periodic_filed']} (delinquent / deregistering)"
        else:
            status, reason = "excluded", f"no 10-K / 10-Q / 20-F / 40-F in the EDGAR index since {cutoff}"
        rows.append({
            "market": "US", "register": "sec_listed", "issuer_key": r["cik"], "issuer_name": r["issuer_name"] or r["cik"],
            "lei": None, "cik": r["cik"], "company_number": None, "country": None, "jurisdiction": None,
            "instruments": None, "tickers": r["tickers"], "venues": venues or (r["venues"] or None),
            "primary_venue": (venues or [None])[0], "competent_authority": "US", "first_admission": None,
            "filer_category": filer_category_of(r.get("last_periodic_form")),
            "last_annual_form": r.get("last_annual_form"), "last_annual_filed": r.get("last_annual_filed"),
            "last_interim_filed": r.get("last_interim_filed"), "status": status, "status_reason": reason,
            "issuer_id": None, "security_id": None, "match_method": None, "as_of": today, "source_file": source_file,
        })
    return pl.DataFrame(rows, schema=REGISTER_SCHEMA) if rows else pl.DataFrame(schema=REGISTER_SCHEMA)


# ------------------------------------------------------------------------------------------------ EU / UK (pure)


def _mode(values: Iterable[Any]) -> Any:
    counts: dict[Any, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: (counts[k], str(k))) if counts else None


def build_firds_register(adm: pl.DataFrame, register: str, market: str, gleif: pl.DataFrame | None, today: date,
                         source_file: str | None = None) -> pl.DataFrame:
    """Per-issuer rows from FIRDS admissions (rm_admissions output, or AIM records) joined to GLEIF records."""
    if adm.is_empty():
        return pl.DataFrame(schema=REGISTER_SCHEMA)
    rm_mics = set(adm.get_column("venue_mic").unique().to_list())
    gl = {r["lei"]: r for r in (gleif.iter_rows(named=True) if gleif is not None and gleif.height else [])}
    rows = []
    for (lei,), part in adm.filter(pl.col("issuer_lei").is_not_null()).group_by(["issuer_lei"], maintain_order=True):
        recs = part.sort(["cfi", "isin"]).to_dicts()
        shares = [r for r in recs if (r["cfi"] or "").startswith("ES")] or recs
        rel = _mode(r["relevant_venue"] for r in recs if r.get("relevant_venue") in rm_mics)
        venues = list(dict.fromkeys(r["venue_mic"] for r in recs if r.get("venue_mic")))
        firsts = [d for r in recs for d in (r.get("admission_approved"), r.get("first_trade")) if d]
        g = gl.get(lei) or {}
        country = g.get("country") or None
        jur = g.get("jurisdiction") or None
        num = g.get("registered_as") or None
        rows.append({
            "market": market, "register": register, "issuer_key": lei, "lei": lei, "cik": None,
            "issuer_name": g.get("legal_name") or shares[0].get("full_name") or lei,
            "company_number": num, "country": country, "jurisdiction": jur,
            "instruments": list(dict.fromkeys(r["isin"] for r in shares if r.get("isin"))) +
                           [r["isin"] for r in recs if r not in shares and r.get("isin")],
            "tickers": None, "venues": venues, "primary_venue": rel or (venues[0] if venues else None),
            "competent_authority": _mode(r.get("relevant_authority") for r in recs),
            "first_admission": min(firsts) if firsts else None, "filer_category": None, "last_annual_form": None,
            "last_annual_filed": None, "last_interim_filed": None, "status": "active", "status_reason": None,
            "issuer_id": None, "security_id": None, "match_method": None, "as_of": today, "source_file": source_file,
        })
    df = pl.DataFrame(rows, schema=REGISTER_SCHEMA)
    return df.with_columns(pl.col("instruments").list.unique(maintain_order=True))


def aim_admissions(firds: pl.DataFrame, today: date) -> pl.DataFrame:
    return firds.filter(pl.col("venue_mic").is_in(list(UK_AIM_MICS))
                        & pl.col("cfi").str.slice(0, 2).is_in(list(F.ISSUER_CFI))
                        & (pl.col("termination").is_null() | (pl.col("termination") > today))
                        & pl.col("issuer_request").fill_null(True))


def carry_removed(new: pl.DataFrame, prev: pl.DataFrame | None, today: date, keep_days: int = 365) -> pl.DataFrame:
    """Issuers of the previous snapshot that left the register stay as status `removed` (kept keep_days)."""
    if prev is None or prev.is_empty():
        return new
    keys = set(zip(new["register"].to_list(), new["issuer_key"].to_list(), strict=True))
    gone = prev.filter(~pl.struct("register", "issuer_key").map_elements(
        lambda s: (s["register"], s["issuer_key"]) in keys, return_dtype=pl.Boolean))
    if gone.is_empty():
        return new
    gone = gone.with_columns(
        pl.when(pl.col("status") == "removed").then(pl.col("status_reason"))
        .otherwise(pl.lit(f"not in the register snapshot of {today}")).alias("status_reason"),
        pl.when(pl.col("status") == "removed").then(pl.col("as_of")).otherwise(pl.lit(today)).alias("as_of"),
        pl.lit("removed").alias("status"))
    gone = gone.filter(pl.col("as_of") >= today - timedelta(days=keep_days))
    return pl.concat([new, gone.select(new.columns).cast(REGISTER_SCHEMA)], how="vertical")


# ------------------------------------------------------------------------------------------------ universe mapping


def resolve_to_universe(reg: pl.DataFrame, idm: pl.DataFrame, secs: pl.DataFrame) -> pl.DataFrame:
    """issuer_id / security_id / match_method from identifier_map (current rows): CIK, LEI, then any ISIN."""
    if reg.is_empty():
        return reg
    cur = idm.filter(pl.col("valid_to").is_null()) if "valid_to" in idm.columns else idm
    prim = (secs.filter(pl.col("is_primary_security") & pl.col("is_active"))
            .select("issuer_id", pl.col("security_id").alias("_prim")).unique("issuer_id", keep="first"))

    def by(t: str) -> dict[str, str]:
        d = cur.filter(pl.col("id_type") == t).sort("confidence", descending=True).unique("id_value", keep="first")
        return dict(zip(d["id_value"].to_list(), d["issuer_id"].to_list(), strict=True))

    cik_m, lei_m, isin_m = by("cik"), by("lei"), by("isin")
    iids, meth = [], []
    for r in reg.select("cik", "lei", "instruments", "tickers").iter_rows(named=True):
        iid, m = None, "none"
        if r["cik"] and r["cik"] in cik_m:
            iid, m = cik_m[r["cik"]], "cik"
        elif r["lei"] and r["lei"] in lei_m:
            iid, m = lei_m[r["lei"]], "lei"
        else:
            for isin in r["instruments"] or []:
                if isin in isin_m:
                    iid, m = isin_m[isin], "isin"
                    break
        iids.append(iid)
        meth.append(m)
    out = reg.with_columns(pl.Series("issuer_id", iids, dtype=pl.Utf8), pl.Series("match_method", meth, dtype=pl.Utf8))
    out = out.drop("security_id").join(prim, on="issuer_id", how="left").rename({"_prim": "security_id"})
    return out.select(list(REGISTER_SCHEMA))


# ------------------------------------------------------------------------------------------------ network steps


def sec_index_entries(settings: Settings, today: date, n_quarters: int = INDEX_QUARTERS, http: Any = None) -> pl.DataFrame:
    """EDGAR full-index master.gz of the last n quarters -> (cik, form, filed) of periodic / fund / registration forms.
    Closed quarters are cached forever as Parquet (reference/regulatory/sec_index/<YYYY>Q<q>.parquet); the current
    quarter is re-read when older than 20 h."""
    from app.core.http import HttpClient
    from app.ingestion.sec_index import parse_master_idx

    h = http or HttpClient("sec_edgar", settings=settings)
    keep = PERIODIC_FORMS | FUND_FORMS | NEW_REGISTRANT_FORMS
    out_dir = F.reg_dir(settings) / "sec_index"
    out_dir.mkdir(parents=True, exist_ok=True)
    cur = quarters_back(today, 1)[0]
    frames = []
    for y, q in quarters_back(today, n_quarters):
        p = out_dir / f"{y}Q{q}.parquet"
        fresh = p.exists() and ((y, q) != cur or time.time() - p.stat().st_mtime < 20 * 3600)
        if not fresh:
            r = h.get(f"https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/master.gz", cache_ttl=0)
            text = gzip.decompress(r.content).decode("latin-1")
            ents = [e for e in parse_master_idx(text) if e["form"] in keep]
            df = pl.DataFrame([{"cik": e["cik"], "form": e["form"], "filed": e["filed"]} for e in ents],
                              schema={"cik": pl.Utf8, "form": pl.Utf8, "filed": pl.Utf8})
            df = df.with_columns(pl.col("filed").str.to_date(strict=False)).unique()
            F.write_parquet(df, p)
        frames.append(pl.read_parquet(p))
    return pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame(
        schema={"cik": pl.Utf8, "form": pl.Utf8, "filed": pl.Date})


def us_register(settings: Settings, today: date, http: Any = None) -> pl.DataFrame:
    from app.normalize import identity_universe as U

    tk = U.fetch_sec_tickers(settings)
    if tk is None or tk.is_empty():
        raise RuntimeError("SEC company_tickers_exchange.json unavailable")
    ent = sec_index_entries(settings, today, http=http)
    reg = build_us(tk, periodic_summary(ent), today, source_file=f"company_tickers_exchange {today}")
    # legal country for the active registrants from the cached SEC submissions (registry/sec_submissions.parquet)
    p = settings.data_dir / "reference" / "registry" / "sec_submissions.parquet"
    if p.exists():
        subs = pl.read_parquet(p).select("cik", "biz_country", "state_inc", "lei")
        cc = subs.with_columns(pl.col("biz_country").alias("country_raw"))
        reg = reg.join(cc.select("cik", pl.col("lei").alias("_lei"), pl.col("country_raw"), pl.col("state_inc")),
                       on="cik", how="left")
        from app.normalize.identity import country_iso2

        reg = reg.with_columns(
            pl.coalesce("lei", "_lei").alias("lei"),
            pl.col("country_raw").map_elements(lambda x: country_iso2(x) if x else None, return_dtype=pl.Utf8)
            .fill_null("US").alias("country"),
            pl.col("state_inc").alias("jurisdiction")).drop("_lei", "country_raw", "state_inc")
    return apply_listing_types(reg.select(list(REGISTER_SCHEMA)), polygon_types(settings))


#: Massive / Polygon reference ticker types that are not issuer equity (notes, preferred-only, trusts, funds' ETPs)
NON_EQUITY_TYPES = {"PFD", "SP", "ETV", "ETF", "ETN", "ETS", "WARRANT", "RIGHT", "UNIT", "BOND", "AGEN", "EQLK"}


def polygon_types(settings: Settings) -> dict[str, str]:
    """ticker (SEC '-' class format) -> Massive type, from the newest landed /v3/reference/tickers page set."""
    root = settings.raw_dir / "polygon" / "ref_reference_tickers"
    files = sorted(root.glob("dt=*/tickers.json*")) if root.exists() else []
    if not files:
        return {}
    b = files[-1].read_bytes()
    try:
        b = gzip.decompress(b)
    except OSError:
        pass
    return {str(x.get("ticker") or "").replace(".", "-"): str(x.get("type") or "") for x in json.loads(b)}


def apply_listing_types(reg: pl.DataFrame, types: dict[str, str]) -> pl.DataFrame:
    """US rows whose every ticker is a non-equity line (baby bonds of utility subsidiaries, preferred-only issuers,
    physical-metal trusts, ETPs) leave the denominator: status excluded with the reason."""
    if not types or reg.is_empty():
        return reg
    st, why = [], []
    for status, reason, tks in reg.select("status", "status_reason", "tickers").iter_rows():
        kinds = {types[t] for t in (tks or []) if t in types}
        if status == "active" and kinds and kinds <= NON_EQUITY_TYPES:
            st.append("excluded")
            why.append(f"no listed common equity (exchange lines are {', '.join(sorted(kinds))} only)")
        else:
            st.append(status)
            why.append(reason)
    return reg.with_columns(pl.Series("status", st, dtype=pl.Utf8), pl.Series("status_reason", why, dtype=pl.Utf8))


def firds_registers(settings: Settings, today: date, state: dict[str, Any], markets: Sequence[str],
                    force: bool = False) -> dict[str, pl.DataFrame]:
    from app.normalize import identity_universe as U

    mics = F.mic_registry(settings)
    out: dict[str, pl.DataFrame] = {}
    parts: list[tuple[str, str, pl.DataFrame, str]] = []
    if "EU" in markets:
        esma, info = F.firds_equity("esma", settings, state, force=force)
        parts.append(("esma_rm", "EU", F.rm_admissions(esma, mics, today, countries=F.EEA), f"ESMA FULINS_E {info.get('set')}"))
    if "UK" in markets:
        fca, info = F.firds_equity("fca", settings, state, force=force)
        parts.append(("fca_official_list", "UK", F.rm_admissions(fca, mics, today, countries=frozenset({"GB"})),
                      f"FCA FULINS_E {info.get('set')}"))
        parts.append(("lse_aim", "UK", aim_admissions(fca, today), f"FCA FULINS_E {info.get('set')} (AIMX)"))
    leis = sorted({x for _, _, a, _ in parts for x in a.get_column("issuer_lei").drop_nulls().to_list()})
    gleif = U.fetch_gleif_records(leis, settings=settings) if leis else None
    for register, market, adm, src in parts:
        df = build_firds_register(adm, register, market, gleif, today, src)
        out[market] = pl.concat([out[market], df]) if market in out else df
    return out


# ------------------------------------------------------------------------------------------------ seeds for missing


#: OpenFIGI security types that are listed issuer equity (pick_listing); funds / ETPs / rights / warrants are not
REG_EQUITY_TYPES = {"Common Stock", "REIT", "Ordinary Shares", "Dutch Cert", "NY Reg Shrs", "Stapled Security",
                    "Preference", "Closed-End Fund", "Unit", "Savings Share", "Foreign Sh.", "Partnership Shares",
                    "Tracking Stk", "Royalty Trst", "MLP"}
#: German regional exchanges (Bloomberg code -> Yahoo suffix); listings keep MIC XFRA (same calendar / currency)
DE_REGIONAL = {"GF": ".F", "GD": ".DU", "GM": ".MU", "GS": ".SG", "GH": ".HM", "GI": ".HA", "GB": ".BE"}
#: FIRDS RM venue (segment MIC) -> Bloomberg code, beyond eu_expansion.FIRDS_VENUE_BBG
EXTRA_VENUE_BBG = {"XETU": "GY", "XEMA": "GY", "FRAU": "GF", "DUSA": "GD", "DUSC": "GD", "MUNA": "GM", "MUNC": "GM",
                   "STUA": "GS", "HAMA": "GH", "HAMP": "GH", "HANA": "GI", "BERA": "GB", "XNGM": "SS", "MSTO": "SS",
                   "DSTO": "SS", "MHEL": "FH", "DHEL": "FH", "MCSE": "DC", "DCSE": "DC", "DMAD": "SM"}
#: register venues without a Yahoo Finance market (no free daily price source found)
NO_YAHOO_VENUES = {"XZAG": "Zagreb", "XLJU": "Ljubljana", "XBUL": "Sofia", "ABUL": "Sofia", "ZBUL": "Sofia",
                   "XMAL": "Malta", "XCYS": "Cyprus", "XLUX": "Luxembourg", "XBRA": "Bratislava"}
NO_YAHOO_OPERATING = {"ABUL": "XBUL", "ZBUL": "XBUL"}


def no_price_seed(r: dict[str, Any], figi: dict[str, list[dict[str, Any]]], venue: str) -> dict[str, Any]:
    """Seed row for a register issuer on a venue without a free price source: reference data + statements only
    (yf_ticker null; the coverage report gives prices reason no_source)."""
    from app.normalize.identity_figi import _job_key, mic_info

    op = NO_YAHOO_OPERATING.get(venue, venue)
    bbg = mic_info(op).get("bbg")
    tk, isin = None, (r.get("instruments") or [None])[0]
    for i in (r.get("instruments") or [])[:3]:
        for rec in figi.get(_job_key({"idType": "ID_ISIN", "idValue": i})) or []:
            if rec.get("exchCode") == bbg and rec.get("ticker"):
                tk, isin = rec["ticker"].strip().upper(), i
                break
        if tk:
            break
    return {"ticker": tk or isin, "mic": op, "yf_ticker": None, "isin": isin, "name": r["issuer_name"],
            "country": (r.get("country") or mic_info(op).get("country") or "")[:2] or None, "region": "EU",
            "adr_ticker": None, "issuer_group": None, "share_class": None,
            "notes": f"regulatory register {r['register']} ({r.get('source_file')}); no free price source ({NO_YAHOO_VENUES[venue]})",
            "lei": r["lei"], "fye_mmdd": None, "native_industry": None,
            "coverage_source": f"regulatory_register:{r['register']}:no_price"}


def pick_listing(records: list[dict[str, Any]], country: str | None, venue: str | None,
                 market: str) -> tuple[str, dict[str, Any], list[str]] | None:
    """OpenFIGI records of an ISIN -> (Bloomberg exchange code, record, Yahoo candidates) of the home / regulated venue.
    Wider than eu_expansion.pick_home_listing: preference shares, investment trusts (closed-end funds), units and the
    German regional exchanges count (registers include them)."""
    from app.ingestion import eu_expansion as E

    eq = [r for r in records or [] if r.get("marketSector") == "Equity" and r.get("ticker")
          and (r.get("securityType") in REG_EQUITY_TYPES or r.get("securityType2") in ("Common Stock", "Preferred Stock"))]
    by_ex: dict[str, dict[str, Any]] = {}
    for r in eq:
        by_ex.setdefault(r.get("exchCode") or "", r)
    vb = EXTRA_VENUE_BBG.get((venue or "").upper()) or E.FIRDS_VENUE_BBG.get((venue or "").upper())
    prefs: list[str] = []
    if market == "UK":
        prefs.append("LN")
    prefs += list(E.HOME_BBG.get(country or "", ()))
    if vb:
        prefs.append(vb)
        prefs += {"SQ": ["SM"], "SM": ["SQ"], "SE": ["SW", "VX"]}.get(vb, [])
    if (country == "DE") or vb in ("GY", *DE_REGIONAL):
        prefs += ["GY", *DE_REGIONAL]
    for ex in dict.fromkeys(prefs):
        r = by_ex.get(ex)
        if r is None:
            continue
        t = r["ticker"].strip().upper().replace(" ", "-").replace("/", "-").rstrip("-")
        if ex == "GY":
            return ex, r, [t + ".DE", t + ".F"]
        if ex in DE_REGIONAL:
            return ex, r, list(dict.fromkeys([t + ".F", t + DE_REGIONAL[ex], t + ".DE"]))
        if ex in E.BBG_VENUE:
            return ex, r, E.bbg_to_yf_candidates(r["ticker"], ex)
    return None


def _candidate_isin(row: dict[str, Any]) -> str | None:
    ins = row.get("instruments") or []
    return ins[0] if ins else None


def missing_seed_rows(reg: pl.DataFrame, settings: Settings, firds_by_isin: dict[str, dict[str, Any]] | None = None,
                      verify: bool = True, max_figi: int | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """EU / UK register issuers with no reference match -> (seed rows, unresolved rows with reason).

    ISIN -> listing: eu_census/listings.parquet (OpenFIGI + verified Yahoo symbol, built 2026-10-11), else OpenFIGI
    (cached 60 days; 25 req/min x 10 ISINs without key) and app.ingestion.eu_expansion.pick_home_listing, then the
    Yahoo symbol is verified with a 3-month download (eu_expansion.verify_prices, cached)."""
    from app.ingestion import eu_expansion as E
    from app.normalize.identity_figi import _job_key, map_jobs, mic_info

    miss = reg.filter(pl.col("market").is_in(["EU", "UK"]) & (pl.col("status") == "active") & pl.col("issuer_id").is_null())
    seed_cols = ["ticker", "mic", "yf_ticker", "isin", "name", "country", "region", "adr_ticker", "issuer_group",
                 "share_class", "notes", "lei", "fye_mmdd", "native_industry", "coverage_source"]
    empty = pl.DataFrame(schema={c: pl.Utf8 for c in seed_cols})
    unres_schema = {"market": pl.Utf8, "register": pl.Utf8, "issuer_key": pl.Utf8, "isin": pl.Utf8, "reason": pl.Utf8}
    if miss.is_empty():
        return empty, pl.DataFrame(schema=unres_schema)
    firds_by_isin = firds_by_isin or {}
    lst_p = E.census_dir(settings) / "listings.parquet"
    known: dict[str, dict[str, Any]] = {}
    if lst_p.exists():
        for r in pl.read_parquet(lst_p).filter(pl.col("yf_ticker").is_not_null()).iter_rows(named=True):
            known.setdefault(r["isin"], r)
    todo_isins: list[str] = []
    for r in miss.iter_rows(named=True):
        for isin in (r["instruments"] or [])[:3]:
            if isin not in known:
                todo_isins.append(isin)
    todo_isins = list(dict.fromkeys(todo_isins))
    if max_figi is not None:
        todo_isins = todo_isins[:max_figi]
    jobs = [{"idType": "ID_ISIN", "idValue": i} for i in todo_isins]
    figi: dict[str, list[dict[str, Any]]] = {}
    for k in range(0, len(jobs), 200):
        figi.update(map_jobs(jobs[k:k + 200], settings=settings, max_age_days=60))
        log.info("register seeds: OpenFIGI %d/%d", min(k + 200, len(jobs)), len(jobs))
    picks: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    nopx: list[dict[str, Any]] = []
    for r in miss.iter_rows(named=True):
        chosen = None
        for isin in (r["instruments"] or [])[:3]:
            if isin in known:
                k = known[isin]
                chosen = {"isin": isin, "bbg_ticker": k.get("bbg_ticker"), "mic": k.get("mic"),
                          "cands": [k["yf_ticker"]], "name": k.get("figi_name") or k.get("firds_name")}
                break
            recs = figi.get(_job_key({"idType": "ID_ISIN", "idValue": isin}))
            if recs is None:
                continue
            fr = firds_by_isin.get(isin) or {}
            cc = E.issuer_country(isin) or (r.get("country") or "")[:2]
            got = pick_listing(recs, cc, r.get("primary_venue") or fr.get("relevant_venue"), r["market"])
            if got:
                ex, best, cands = got
                mic = E.BBG_VENUE[ex][0] if ex in E.BBG_VENUE else "XFRA"
                chosen = {"isin": isin, "bbg_ticker": best.get("ticker"), "mic": mic, "cands": cands,
                          "name": best.get("name")}
                break
        if chosen is None or not chosen["cands"]:
            nov = [NO_YAHOO_VENUES[v] for v in (r.get("venues") or []) if v in NO_YAHOO_VENUES]
            if nov:
                v0 = next(v for v in (r.get("venues") or []) if v in NO_YAHOO_VENUES)
                if r.get("lei"):
                    nopx.append(no_price_seed(r, figi, v0))
                    continue
                why = f"no_source: no free daily price source for {nov[0]} (Yahoo has no market there)"
            elif any(i in figi for i in [_job_key({"idType": "ID_ISIN", "idValue": x}) for x in (r["instruments"] or [])[:3]]):
                why = "unmapped: OpenFIGI has no equity listing on the issuer's regulated / home venue"
            else:
                why = "unmapped: ISIN not yet looked up (OpenFIGI budget)"
            unresolved.append({"market": r["market"], "register": r["register"], "issuer_key": r["issuer_key"],
                               "isin": _candidate_isin(r), "reason": why})
            continue
        picks.append({**chosen, "reg": r})
    verified: dict[str, dict[str, Any]] = {}
    if verify and picks:
        px = E.verify_prices(sorted({c for p in picks for c in p["cands"]}), settings)
        verified = {x["yf_ticker"]: x for x in px.filter(pl.col("n_obs") >= 5).iter_rows(named=True)}
    rows = []
    for p in picks:
        r = p["reg"]
        yf = next((c for c in p["cands"] if c in verified), None) if verify else p["cands"][0]
        if yf is None:
            v0 = next((v for v in (r.get("venues") or []) if v in NO_YAHOO_VENUES), None)
            if v0 and r.get("lei"):
                nopx.append(no_price_seed(r, figi, v0))
                continue
            unresolved.append({"market": r["market"], "register": r["register"], "issuer_key": r["issuer_key"],
                               "isin": p["isin"], "reason": f"no_source: Yahoo has no bars for {', '.join(p['cands'])} "
                                                         "(suspended / delisted / unlisted share line)"})
            continue
        mic = "XETR" if yf.endswith(".DE") else p["mic"]
        m = mic_info(mic)
        country = (r.get("country") or E.issuer_country(p["isin"]) or m.get("country") or "").upper()[:2] or None
        local = (p.get("bbg_ticker") or (yf.rsplit(".", 1)[0] if "." in yf else yf)).strip().upper()
        rows.append({"ticker": local, "mic": mic, "yf_ticker": yf,
                     "isin": p["isin"], "name": r["issuer_name"], "country": country,
                     "region": "UK" if r["market"] == "UK" else (m.get("region") or "EU"),
                     "adr_ticker": None, "issuer_group": None, "share_class": None,
                     "notes": f"regulatory register {r['register']} ({r.get('source_file')})", "lei": r["lei"],
                     "fye_mmdd": None, "native_industry": None, "coverage_source": f"regulatory_register:{r['register']}"})
    rows += nopx
    seeds = pl.DataFrame(rows, schema={c: pl.Utf8 for c in seed_cols}) if rows else empty
    return seeds.unique("isin", keep="first"), pl.DataFrame(unresolved, schema=unres_schema)


def write_seeds(seeds: pl.DataFrame, settings: Settings) -> Path:
    """Merge into seeds/regulatory_register.csv (rows keep their first appearance; removed issuers stay until the
    register drops them for good: ref_universe retires securities that stop trading)."""
    from app.normalize.identity_universe import seeds_dir

    p = seeds_dir(settings) / "regulatory_register.csv"
    if p.exists():
        old = pl.read_csv(p, infer_schema_length=0)
        seeds = pl.concat([old.select(seeds.columns), seeds], how="vertical").unique("isin", keep="first",
                                                                                      maintain_order=True)
    tmp = p.with_suffix(".tmp")
    seeds.write_csv(tmp)
    tmp.replace(p)
    return p


# ------------------------------------------------------------------------------------------------ build


def build(settings: Settings | None = None, markets: Sequence[str] = ("US", "EU", "UK"), seeds: bool = True,
          verify_prices: bool = True, write: bool = True, run_id: str | None = None, force: bool = False,
          max_figi: int | None = None) -> dict[str, Any]:
    from app.core.storage import read_table, write_table

    s = settings or get_settings()
    today = _today()
    state = load_state(s)
    t0 = time.time()
    frames: dict[str, pl.DataFrame] = {}
    errors: dict[str, str] = {}
    if "US" in markets:
        try:
            frames["US"] = us_register(s, today)
        except Exception as e:  # noqa: BLE001
            errors["US"] = f"{type(e).__name__}: {e}"[:300]
            log.exception("US register failed")
    eu_uk = [m for m in markets if m in ("EU", "UK")]
    if eu_uk:
        try:
            frames.update(firds_registers(s, today, state, eu_uk, force=force))
        except Exception as e:  # noqa: BLE001
            errors["EU/UK"] = f"{type(e).__name__}: {e}"[:300]
            log.exception("FIRDS registers failed")
    idm = read_table("identifier_map", settings=s)
    secs = read_table("securities_master", settings=s)
    try:
        prev_all = read_table("regulatory_register", settings=s)
    except Exception:  # noqa: BLE001 - first run
        prev_all = pl.DataFrame(schema=REGISTER_SCHEMA)
    summary: dict[str, Any] = {"as_of": today.isoformat(), "markets": {}, "errors": errors}
    now = datetime.now(UTC)
    for market, df in frames.items():
        df = resolve_to_universe(df, idm, secs)
        prev = prev_all.filter(pl.col("market") == market) if prev_all.height else None
        df = carry_removed(df, prev.select(list(REGISTER_SCHEMA)) if prev is not None and prev.height else None, today)
        frames[market] = df
        act = df.filter(pl.col("status") == "active")
        summary["markets"][market] = {
            "rows": df.height, "active": act.height,
            "by_register": dict(act.group_by("register").len().iter_rows()),
            "excluded": dict(df.filter(pl.col("status") == "excluded")
                             .with_columns(pl.col("status_reason").str.split(" (").list.first().str.split(" since").list.first())
                             .group_by("status_reason").len().iter_rows()),
            "matched": act.filter(pl.col("issuer_id").is_not_null()).height,
            "matched_by": dict(act.group_by("match_method").len().iter_rows()),
            "with_security": act.filter(pl.col("security_id").is_not_null()).height,
        }
        if write:
            out = df.with_columns(pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("updated_at"),
                                  pl.lit(run_id).cast(pl.Utf8).alias("run_id"))
            write_table(out, "regulatory_register", mode="overwrite_partition", settings=s)
    if seeds and any(m in frames for m in ("EU", "UK")):
        allreg = pl.concat([frames[m] for m in ("EU", "UK") if m in frames], how="vertical")
        fb: dict[str, dict[str, Any]] = {}
        for src in ("esma", "fca"):
            p = F.reg_dir(s) / f"firds_{src}_equity.parquet"
            if p.exists():
                for r in (pl.read_parquet(p, columns=["isin", "relevant_venue"]).unique("isin").iter_rows(named=True)):
                    fb.setdefault(r["isin"], r)
        sd, unres = missing_seed_rows(allreg, s, fb, verify=verify_prices, max_figi=max_figi)
        if write:
            if sd.height:
                summary["seeds_file"] = str(write_seeds(sd, s))
            F.write_parquet(unres, F.reg_dir(s) / "unresolved.parquet")
        summary["seeds_new"] = sd.height
        summary["unresolved"] = dict(unres.with_columns(pl.col("reason").str.split(":").list.first())
                                     .group_by("market", "reason").len().select(
                                         pl.concat_str(["market", "reason"], separator=" ").alias("k"), "len").iter_rows())
    state["last_build"] = {"at": now.isoformat(timespec="seconds"), "summary": summary,
                           "runtime_s": round(time.time() - t0, 1)}
    if write:
        save_state(state, s)
    return summary


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult

    p = dict(ctx.params or {})
    markets = p.get("markets") or ["US", "EU", "UK"]
    if isinstance(markets, str):
        markets = [m.strip().upper() for m in markets.split(",") if m.strip()]
    res = build(ctx.settings, markets=markets, seeds=bool(p.get("seeds", True)),
                verify_prices=bool(p.get("verify_prices", True)), run_id=ctx.run_id, force=bool(p.get("force", False)),
                max_figi=p.get("max_figi"))
    rows = {"regulatory_register": sum(v.get("rows", 0) for v in res["markets"].values())}
    status = "partial" if res["errors"] and res["markets"] else ("failed" if res["errors"] else "success")
    msg = "; ".join(f"{m}: {v['active']} active, {v['matched']} matched" for m, v in res["markets"].items())
    if res.get("seeds_new"):
        msg += f"; {res['seeds_new']} new seeds"
    if res["errors"]:
        msg += f"; errors: {res['errors']}"
    return JobResult(status=status, rows_written=rows, entities_ok=sum(v["active"] for v in res["markets"].values()),
                     entities_failed=len(res["errors"]), watermark=res["as_of"], message=msg[:900])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingestion.regulatory_register")
    ap.add_argument("--markets", default="US,EU,UK")
    ap.add_argument("--no-seeds", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-figi", type=int, default=None)
    a = ap.parse_args(argv)
    from app.core.logsetup import setup_logging

    setup_logging(log_file="regulatory_register.log", service="cli")
    res = build(markets=[m.strip().upper() for m in a.markets.split(",")], seeds=not a.no_seeds,
                verify_prices=not a.no_verify, write=not a.dry_run, force=a.force, max_figi=a.max_figi)
    print(json.dumps(res, indent=1, default=str))
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
