"""Regulator coverage report (coverage v2 W2): register issuers vs universe vs prices vs statements vs sector.

Job `regulatory_coverage` (nightly, after fundamentals_normalize): for every active regulatory_register row
  in_universe   the issuer (CIK / LEI / ISIN, re-resolved now) has an active primary security in securities_master;
  has_prices    its primary listing has a bar within 10 days of the newest session of its region;
  has_annual    the latest FY in fundamentals_canonical known as of today (knowledge_date <= as_of; items revenue /
                net_income / total_assets) ends within 12 months + the register's filing deadline (US 10-K 3 months,
                20-F / 40-F 4, EU TD 4, UK Official List 4, AIM 6) - plus half a month of grace;
  has_interim   the latest Q1-Q3 / H1 period ends within 5 months (US quarterly) / 10.5 months (half-yearly);
  has_sector    issuers.sector_id set;
and a reason (code: note) for every missing cell. Writes ops/regulatory_coverage (partition market), the summary
data/state/regulatory_coverage.json, the "Regulator coverage" section of docs/DATA_COMPLETENESS.md (between the
`regulator-coverage` markers, above the generated part) and the `regulator_coverage` block of
contracts/field_availability.yaml.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

STATE_FILE = "regulatory_coverage.json"
#: annual statement deadline (months after FY end) per register; filer category overrides for US FPIs
DEADLINE_M = {"sec_listed": 3.0, "esma_rm": 4.0, "fca_official_list": 4.0, "lse_aim": 6.0}
FPI_DEADLINE_M = 4.0
GRACE_M = 0.5
INTERIM_WINDOW_M = {"US": 5.0, "EU": 10.5, "UK": 10.5}
OFFICIAL_SOURCES_EXCLUDED = {"yfinance", "yfinance_fund"}
CORE_ITEMS = ("revenue", "net_income", "total_assets")
TARGET_ANNUAL, TARGET_INTERIM = 95.0, 85.0
MD_START, MD_END = "<!-- regulator-coverage:start -->", "<!-- regulator-coverage:end -->"
SOURCES_USED = (
    "SEC company_tickers_exchange.json + EDGAR full-index master.gz (register), companyfacts bulk zip (statements of every "
    "register CIK) and Financial Statement Data Sets 2009q1 -> (20-F / 40-F filers)",
    "ESMA FIRDS FULINS_E weekly files + ESMA regulated-market register (upreg MIR) + ISO 10383 MIC list (EU register)",
    "FCA FIRDS FULINS_E (data.fca.org.uk artefacts; UK Official List equity on XLON + AIM on AIMX)",
    "GLEIF lei-records API (names, legal country, company numbers), OpenFIGI mapping (ISIN -> home listing, cached) and "
    "Yahoo 3-month downloads (price-source check)",
    "filings.xbrl.org ESEF / UKSEF xBRL-JSON (EU / UK annual reports), FCA NSM tagged AFRs, the national OAM adapters "
    "(DILA, AFM, FSMA, eMarket, Nasdaq Nordic, Oslo, CNMV)",
    "Companies House Accounts Data Product (free daily / monthly iXBRL zips, read with HTTP Range requests: central "
    "directory + the target members only)",
    "Yahoo statements (yfinance, annual + quarterly) as the documented, non-official fallback",
)
SOURCES_REJECTED = (
    "FCA Official List web search (marketsecurities.fca.org.uk): robots.txt disallows all bots",
    "LSE instrument / AIM lists (londonstockexchange.com reports API): answers 403 to non-browser clients (WAF) - not "
    "bypassed; FCA FIRDS covers the same issuers",
    "Companies House REST API: needs COMPANIES_HOUSE_APIKEY (not needed: the bulk product is keyless); its PDF accounts "
    "are not parsed",
    "FCA NSM market-wide search: WAF 403 on paging (per-LEI queries only)",
    "Zagreb, Ljubljana, Sofia, Malta, Cyprus, Luxembourg, Bratislava prices: no free daily source (Yahoo has no market); "
    "those issuers get reference data + statements, prices reason no_source",
)
MARKET_LABEL = {"US": "US (SEC)", "EU": "EU / EEA (ESMA FIRDS)", "UK": "UK (FCA FIRDS)"}
REGISTER_LABEL = {"sec_listed": "SEC listed periodic filers", "esma_rm": "EEA regulated markets",
                  "fca_official_list": "FCA Official List (LSE Main Market)", "lse_aim": "AIM"}


def _months(m: float) -> timedelta:
    return timedelta(days=round(m * 30.44))


# ------------------------------------------------------------------------------------------------ pure core


def classify(reg: pl.DataFrame, sec: pl.DataFrame, iss: pl.DataFrame, px: pl.DataFrame, fy: pl.DataFrame,
             intr: pl.DataFrame, as_of: date, region_last: dict[str, date], extra: dict[str, Any] | None = None) -> pl.DataFrame:
    """Coverage rows for active register rows.

    reg  : register rows (issuer_id re-resolved), sec: securities_master (security_id, issuer_id, yf/polygon flags via
    `has_symbol`), iss: issuers (issuer_id, region, sector_id, sic_code), px: (security_id, last_date),
    fy / intr: (issuer_id, period_end, source_id) of the latest annual / interim known as of `as_of`;
    extra: {"unresolved": {issuer_key: reason}, "raw_entities": set of source entity ids with raw facts,
            "seeded": set of register keys present in the seeds file, "esef_leis": set of LEIs with ESEF filings}."""
    extra = extra or {}
    unres = extra.get("unresolved") or {}
    raw_ents = extra.get("raw_entities") or set()
    seeded = extra.get("seeded") or set()
    esef_leis = extra.get("esef_leis") or set()
    sec_by = {r["security_id"]: r for r in sec.iter_rows(named=True)}
    iss_by = {r["issuer_id"]: r for r in iss.iter_rows(named=True)}
    px_by = dict(zip(px["security_id"].to_list(), px["last_date"].to_list(), strict=True)) if px.height else {}
    fy_by = {r["issuer_id"]: r for r in fy.iter_rows(named=True)} if fy.height else {}
    in_by = {r["issuer_id"]: r for r in intr.iter_rows(named=True)} if intr.height else {}
    rows = []
    for r in reg.filter(pl.col("status") == "active").iter_rows(named=True):
        mk, rg, key = r["market"], r["register"], r["issuer_key"]
        iid, sid = r.get("issuer_id"), r.get("security_id")
        i = iss_by.get(iid) if iid else None
        s = sec_by.get(sid) if sid else None
        in_u = bool(i and s)
        ru = rp = ra = ri = rs = None
        if not in_u:
            if key in unres:
                ru = unres[key]
            elif key in seeded:
                ru = "due: added to seeds/regulatory_register.csv, created by the next ref_universe run"
            elif mk == "US":
                ru = "mapping_gap: CIK not in the reference universe (no exchange listing record in the Massive / SEC lists)"
            else:
                ru = "mapping_gap: register LEI / ISINs not matched (next regulatory_register build adds a seed)"
        last_px = px_by.get(sid) if sid else None
        reg_last = region_last.get((i or {}).get("region") or "", as_of)
        has_px = bool(last_px and last_px >= reg_last - timedelta(days=10))
        if in_u and not has_px:
            if not (s or {}).get("has_symbol"):
                rp = "no_source: no Yahoo / Polygon symbol for the primary listing"
            elif last_px is None:
                rp = "due: new listing, first price history fetch pending (prices_global_yf / gap filler)"
            else:
                rp = f"stale: last bar {last_px} (no trades since; suspended or delisted)"
        elif not in_u:
            rp = "out_of_scope: issuer not in the universe yet (see universe reason)"
        dl = DEADLINE_M.get(rg, 4.0)
        if mk == "US" and (r.get("filer_category") or "").startswith("foreign"):
            dl = FPI_DEADLINE_M
        a_cut = as_of - _months(12 + dl + GRACE_M)
        f = fy_by.get(iid) if iid else None
        has_a = bool(f and f["period_end"] >= a_cut)
        official = bool(f and f["source_id"] not in OFFICIAL_SOURCES_EXCLUDED) if f else None
        if not has_a:
            ra = _annual_reason(r, f, in_u, raw_ents, esef_leis, a_cut)
        q_cut = as_of - _months(INTERIM_WINDOW_M.get(mk, 10.5))
        q = in_by.get(iid) if iid else None
        has_q = bool(q and q["period_end"] >= q_cut)
        if not has_q:
            ri = _interim_reason(r, q, in_u, f, q_cut, raw_ents)
        has_s = bool(i and i.get("sector_id"))
        if not has_s:
            if not in_u:
                rs = "out_of_scope: issuer not in the universe yet"
            elif (i.get("sic_code") or "") in ("6770",):
                rs = "not_applicable: blank-check company (SIC 6770, SPAC) has no operating sector"
            elif (i.get("sic_code") or "") in ("6221", "6722", "6726"):
                rs = "not_applicable: fund / commodity pool SIC"
            else:
                rs = "mapping_gap: no SIC / Yahoo industry mapped to the sector taxonomy"
        rows.append({
            "market": mk, "register": rg, "issuer_key": key, "issuer_name": r["issuer_name"], "country": r.get("country"),
            "issuer_id": iid if in_u else iid, "security_id": sid, "in_universe": in_u, "has_prices": has_px,
            "last_price_date": last_px, "annual_period_end": (f or {}).get("period_end"),
            "annual_source": (f or {}).get("source_id"), "annual_official": official, "has_annual": has_a,
            "interim_period_end": (q or {}).get("period_end"), "interim_source": (q or {}).get("source_id"),
            "has_interim": has_q, "has_sector": has_s, "reason_universe": ru, "reason_prices": rp,
            "reason_annual": ra, "reason_interim": ri, "reason_sector": rs, "as_of": as_of,
        })
    return pl.DataFrame(rows, schema=COV_SCHEMA) if rows else pl.DataFrame(schema=COV_SCHEMA)


COV_SCHEMA: dict[str, Any] = {
    "market": pl.Utf8, "register": pl.Utf8, "issuer_key": pl.Utf8, "issuer_name": pl.Utf8, "country": pl.Utf8,
    "issuer_id": pl.Utf8, "security_id": pl.Utf8, "in_universe": pl.Boolean, "has_prices": pl.Boolean,
    "last_price_date": pl.Date, "annual_period_end": pl.Date, "annual_source": pl.Utf8, "annual_official": pl.Boolean,
    "has_annual": pl.Boolean, "interim_period_end": pl.Date, "interim_source": pl.Utf8, "has_interim": pl.Boolean,
    "has_sector": pl.Boolean, "reason_universe": pl.Utf8, "reason_prices": pl.Utf8, "reason_annual": pl.Utf8,
    "reason_interim": pl.Utf8, "reason_sector": pl.Utf8, "as_of": pl.Date,
}


def _annual_reason(r: dict[str, Any], f: dict[str, Any] | None, in_u: bool, raw_ents: set[str], esef_leis: set[str],
                   cut: date) -> str:
    mk = r["market"]
    if f is not None:
        if mk == "US" and r.get("last_annual_filed") and r["last_annual_filed"] < cut + _months(3):
            return f"stale: latest annual FY {f['period_end']} (last {r.get('last_annual_form')} filed {r['last_annual_filed']}: late filer)"
        return f"stale: latest annual FY {f['period_end']} ({f['source_id']}); the newer one is not loaded yet"
    if not in_u:
        return "out_of_scope: issuer not in the universe yet (statements are loaded but cannot be normalised)"
    if mk == "US":
        if not r.get("last_annual_form"):
            return "not_yet_published: no 10-K / 20-F / 40-F filed yet (only 10-Q since listing)"
        if r.get("cik") not in raw_ents:
            return "no_source: no XBRL financial data in SEC companyfacts for this CIK (register_backfill us_facts pending)"
        return "pipeline_bug: companyfacts loaded but no core line item normalised (custom tags / unit filter)"
    lei = r.get("lei")
    if lei in raw_ents or (r.get("company_number") or "") in raw_ents:
        return "pipeline_bug: official facts loaded but no core line item normalised (taxonomy / unit mapping)"
    if lei in esef_leis:
        return "due: ESEF filing listed on filings.xbrl.org, download pending (register_backfill esef)"
    if mk == "UK" and r["register"] == "lse_aim":
        return ("no_source: AIM companies are not on the FCA NSM / ESEF; Companies House holds PDF accounts only "
                "(not in the iXBRL bulk product); Yahoo fallback pending")
    return ("no_source: no ESEF / national OAM filing found for this LEI and no Companies House iXBRL accounts; "
            "Yahoo statements fallback pending (register_backfill yf)")


def _interim_reason(r: dict[str, Any], q: dict[str, Any] | None, in_u: bool, f: dict[str, Any] | None, cut: date,
                    raw_ents: set[str] | None = None) -> str:
    mk = r["market"]
    if not in_u:
        return "out_of_scope: issuer not in the universe yet"
    if mk == "US":
        if (r.get("filer_category") or "").startswith("foreign"):
            return "not_applicable: foreign private issuer (20-F / 40-F): no 10-Q duty; 6-K interims are rarely XBRL-tagged"
        if q is not None:
            return f"stale: latest quarter {q['period_end']} ({q['source_id']})"
        if r.get("last_interim_filed") is None:
            return "not_yet_published: no 10-Q in the EDGAR index window"
        if raw_ents is not None and r.get("cik") not in raw_ents:
            return "no_source: no XBRL financial data in SEC companyfacts for this CIK (register_backfill us_facts pending)"
        return "pipeline_bug: 10-Q filed but no quarterly core item normalised"
    if q is not None:
        return f"stale: latest interim {q['period_end']} ({q['source_id']}); the next half-year is not loaded yet"
    return ("no_source: half-year reports are not ESEF-tagged (Transparency Directive: annual only) and the OAMs publish "
            "them as PDF; Yahoo quarterly fallback pending")


def latest_statements(canon: pl.DataFrame, as_of: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Point in time: of the canonical core rows known on `as_of` (knowledge_date <= as_of), the latest annual (FY)
    and the latest interim (discrete Q1-Q3 / H1) per issuer -> (fy, interim) frames (issuer_id, period_end,
    source_id). Rows published after `as_of` (restatements, later filings) are invisible."""
    k = canon.filter(pl.col("knowledge_date") <= as_of)
    cols = ["issuer_id", "period_end", "source_id"]
    fy = (k.filter(pl.col("period_kind") == "FY").sort(["issuer_id", "period_end", "knowledge_date"])
          .group_by("issuer_id").agg(pl.col("period_end").last(), pl.col("source_id").last()).select(cols))
    q = (k.filter(pl.col("period_kind").is_in(["Q", "H"]) & pl.col("fiscal_period").is_in(["Q1", "Q2", "Q3", "H1"]))
         .sort(["issuer_id", "period_end", "knowledge_date"])
         .group_by("issuer_id").agg(pl.col("period_end").last(), pl.col("source_id").last()).select(cols))
    return fy, q


def summarize(cov: pl.DataFrame, reg: pl.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mk in ("US", "EU", "UK"):
        c = cov.filter(pl.col("market") == mk)
        rg = reg.filter(pl.col("market") == mk)
        n = c.height
        if not n:
            continue

        def pct(col: str, frame: pl.DataFrame = c) -> float:
            return round(100.0 * frame[col].sum() / frame.height, 1) if frame.height else 0.0

        def reasons(col: str, frame: pl.DataFrame = c) -> dict[str, int]:
            cnt = Counter((x or "").split(":")[0] for x in frame[col].drop_nulls().to_list())
            return dict(cnt.most_common())

        def top_notes(col: str, k: int = 4, frame: pl.DataFrame = c) -> list[list[Any]]:
            cnt = Counter(_note_key(x) for x in frame[col].drop_nulls().to_list())
            return [[a, b] for a, b in cnt.most_common(k)]

        per_reg = {}
        for rname in sorted(c["register"].unique().to_list()):
            cc = c.filter(pl.col("register") == rname)
            per_reg[rname] = {"issuers": cc.height, "universe_pct": pct("in_universe", cc), "prices_pct": pct("has_prices", cc),
                              "annual_pct": pct("has_annual", cc), "interim_pct": pct("has_interim", cc),
                              "sector_pct": pct("has_sector", cc),
                              "annual_official_pct": round(100.0 * cc.filter(pl.col("has_annual") & pl.col("annual_official").fill_null(False)).height / cc.height, 1)}
        out[mk] = {
            "register_issuers": n,
            "register_rows_total": rg.height,
            "excluded": dict(Counter((x or "").split(" (")[0] for x in rg.filter(pl.col("status") == "excluded")["status_reason"].to_list()).most_common()),
            "in_universe": int(c["in_universe"].sum()), "with_prices": int(c["has_prices"].sum()),
            "with_annual": int(c["has_annual"].sum()), "with_interim": int(c["has_interim"].sum()),
            "with_sector": int(c["has_sector"].sum()),
            "annual_official": int(c.filter(pl.col("has_annual") & pl.col("annual_official").fill_null(False)).height),
            "pct": {"universe": pct("in_universe"), "prices": pct("has_prices"), "annual": pct("has_annual"),
                    "interim": pct("has_interim"), "sector": pct("has_sector")},
            "targets": {"annual": TARGET_ANNUAL, "interim": TARGET_INTERIM,
                        "annual_met": pct("has_annual") >= TARGET_ANNUAL, "interim_met": pct("has_interim") >= TARGET_INTERIM},
            "reasons": {"universe": reasons("reason_universe"), "prices": reasons("reason_prices"),
                        "annual": reasons("reason_annual"), "interim": reasons("reason_interim"),
                        "sector": reasons("reason_sector")},
            "top_notes": {"universe": top_notes("reason_universe"), "annual": top_notes("reason_annual"),
                          "interim": top_notes("reason_interim"), "prices": top_notes("reason_prices", 3),
                          "sector": top_notes("reason_sector", 3)},
            "annual_sources": dict(Counter(c.filter(pl.col("has_annual"))["annual_source"].to_list()).most_common()),
            "registers": per_reg,
        }
    return out


def _note_key(x: str) -> str:
    """Reason text without issuer-specific dates / periods (for the top-reasons tables)."""
    import re

    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<date>", x)
    s = re.sub(r"\(last [^)]*\)", "(late filer)", s)
    s = re.sub(r"\((sec_edgar|sec_fsds|esef_xbrl_org|fca_nsm|companies_house|yfinance|[a-z_]+)\)", "", s)
    return s.strip()[:140]


# ------------------------------------------------------------------------------------------------ report writers


def markdown(summary: dict[str, Any], before: dict[str, Any] | None, as_of: str) -> str:
    L = [MD_START, "## Regulator coverage", "",
         f"Coverage v2 W2 (docs/COVERAGE_V2_PLAN.md). Generated by job `regulatory_coverage` on {as_of}. Denominator: the "
         "securities regulators' registers (`regulatory_register`): SEC exchange-listed 10-K / 10-Q / 20-F / 40-F filers "
         "(company_tickers_exchange + EDGAR full-index), issuers of shares admitted to an EEA regulated market at their "
         "request (ESMA FIRDS + ISO 10383 / ESMA regulated-market list), UK regulated market = FCA Official List equity "
         "(FCA FIRDS, venue XLON) and AIM (FCA FIRDS, AIMX). Per issuer row and reason: `ops/regulatory_coverage`; "
         "machine-readable summary: `contracts/field_availability.yaml` `regulator_coverage`. Annual = latest FY within "
         "12 months + the register's filing deadline (+0.5 month); interim = latest Q1-Q3 / H1 within 5 months (US) / "
         "10.5 months (EU / UK). Targets: annual >= 95 %, interim >= 85 %.", "",
         "| Market | Register issuers | In universe | Prices | Latest annual (official) | Latest interim | Sector |",
         "|---|---|---|---|---|---|---|"]
    for mk, v in summary.items():
        p = v["pct"]
        b = ((before or {}).get(mk) or {}).get("pct") or {}

        def cell(k: str, n: int) -> str:
            prev = f" (before {b[k]:.1f})" if k in b else ""
            return f"{n:,} = {p[k]:.1f} %{prev}"

        off = v["annual_official"]
        L.append(f"| {MARKET_LABEL.get(mk, mk)} | {v['register_issuers']:,} | {cell('universe', v['in_universe'])} | "
                 f"{cell('prices', v['with_prices'])} | {cell('annual', v['with_annual'])} ({off:,} official) | "
                 f"{cell('interim', v['with_interim'])} | {cell('sector', v['with_sector'])} |")
    L += ["", "| Register | Issuers | Universe % | Prices % | Annual % | of which official % | Interim % | Sector % |",
          "|---|---|---|---|---|---|---|---|"]
    for mk, v in summary.items():
        for rname, x in v["registers"].items():
            L.append(f"| {REGISTER_LABEL.get(rname, rname)} | {x['issuers']:,} | {x['universe_pct']:.1f} | "
                     f"{x['prices_pct']:.1f} | {x['annual_pct']:.1f} | {x['annual_official_pct']:.1f} | "
                     f"{x['interim_pct']:.1f} | {x['sector_pct']:.1f} |")
    L += ["", "Every missing cell has a reason (code: note). Most frequent reasons per market:", "",
          "| Market | Cell | Missing | Reasons (count) |", "|---|---|---|---|"]
    for mk, v in summary.items():
        n = v["register_issuers"]
        miss = {"universe": n - v["in_universe"], "prices": n - v["with_prices"], "annual": n - v["with_annual"],
                "interim": n - v["with_interim"], "sector": n - v["with_sector"]}
        for cellname, m in miss.items():
            if not m:
                continue
            notes = "; ".join(f"{a} ({b:,})" for a, b in (v["top_notes"].get(cellname) or []))
            L.append(f"| {mk} | {cellname} | {m:,} | {notes} |")
    ex = {mk: v.get("excluded") for mk, v in summary.items() if v.get("excluded")}
    if ex:
        L += ["", "Listed but outside the register definition (status `excluded`): " +
              "; ".join(f"{mk}: " + ", ".join(f"{k} {n:,}" for k, n in e.items()) for mk, e in ex.items()) + "."]
    L += ["", "Sources (W2). Used: " + "; ".join(SOURCES_USED) + ".", "",
          "Rejected / not used: " + "; ".join(SOURCES_REJECTED) + ".", "", MD_END, ""]
    return "\n".join(L)


def write_markdown(md: str, settings: Settings) -> Path:
    p = settings.contracts_dir.parent / "docs" / "DATA_COMPLETENESS.md"
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    if MD_START in txt and MD_END in txt:
        a, rest = txt.split(MD_START, 1)
        _, b = rest.split(MD_END, 1)
        new = a + md.rstrip("\n") + b
    else:
        mark = "<!-- generated: app.qa.completeness -->"
        anchor = "## UI: every empty value explains itself"
        if anchor in txt:
            a, b = txt.split(anchor, 1)
            new = a + md + "\n" + anchor + b
        elif mark in txt:
            a, b = txt.split(mark, 1)
            new = a + md + "\n" + mark + b
        else:
            new = txt + "\n" + md
    tmp = p.with_suffix(".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(p)
    return p


def field_block(summary: dict[str, Any], as_of: str) -> dict[str, Any]:
    return {"as_of": as_of, "source": "job regulatory_coverage (ops/regulatory_coverage, data/state/regulatory_coverage.json)",
            "targets": {"annual_pct": TARGET_ANNUAL, "interim_pct": TARGET_INTERIM},
            "markets": {mk: {"register_issuers": v["register_issuers"], "pct": v["pct"],
                             "annual_official": v["annual_official"], "reasons": v["reasons"],
                             "registers": v["registers"]} for mk, v in summary.items()}}


def write_field_availability(block: dict[str, Any], settings: Settings) -> Path | None:
    import yaml

    p = settings.contracts_dir / "field_availability.yaml"
    if not p.exists():
        return None
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    doc["regulator_coverage"] = block
    p.write_text("# Generated by app.qa.completeness - do not edit by hand (python -m app.qa.completeness --write)\n"
                 + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120, default_flow_style=None),
                 encoding="utf-8")
    return p


# ------------------------------------------------------------------------------------------------ job


def _inputs(s: Settings, as_of: date) -> dict[str, Any]:
    import duckdb

    from app.core.storage import read_table, view_sql
    from app.core.schema import get_contract
    from app.ingestion.regulatory_register import REGISTER_SCHEMA, resolve_to_universe

    reg = read_table("regulatory_register", settings=s)
    idm = read_table("identifier_map", settings=s)
    secs = read_table("securities_master", settings=s)
    reg = resolve_to_universe(reg.select(list(REGISTER_SCHEMA)), idm, secs)
    iss = read_table("issuers", columns=["issuer_id", "region", "sector_id", "sic_code", "cik", "lei"], settings=s)
    lst = read_table("listings", settings=s).filter(pl.col("is_primary") & pl.col("valid_to").is_null())
    sym = lst.with_columns((pl.col("yf_ticker").is_not_null() | pl.col("polygon_ticker").is_not_null()).alias("has_symbol")) \
        .group_by("security_id").agg(pl.col("has_symbol").any())
    sec = secs.filter(pl.col("is_active")).select("security_id", "issuer_id").join(sym, on="security_id", how="left") \
        .with_columns(pl.col("has_symbol").fill_null(False))
    ids = reg["issuer_id"].drop_nulls().unique().to_list()
    sids = reg["security_id"].drop_nulls().unique().to_list()
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'; SET threads=4")
    root = s.data_dir / "normalized" / "prices_daily"
    yrs = ",".join(str(y) for y in (as_of.year - 1, as_of.year))
    glob = (root / "region=*" / f"year={{{yrs}}}" / "*.parquet").as_posix()
    try:
        px_all = con.execute(f"SELECT security_id, region, max(date) AS last_date FROM read_parquet('{glob}', "
                             "hive_partitioning=true, union_by_name=true) GROUP BY 1, 2").pl()
    except Exception:  # noqa: BLE001 - brace globs unsupported -> scan two globs
        parts = []
        for y in (as_of.year - 1, as_of.year):
            g = (root / "region=*" / f"year={y}" / "*.parquet").as_posix()
            parts.append(con.execute(f"SELECT security_id, region, max(date) AS last_date FROM read_parquet('{g}', "
                                     "hive_partitioning=true, union_by_name=true) GROUP BY 1, 2").pl())
        px_all = pl.concat(parts).group_by("security_id", "region").agg(pl.col("last_date").max())
    region_last = dict(px_all.group_by("region").agg(pl.col("last_date").max()).iter_rows())
    px = px_all.filter(pl.col("security_id").is_in(sids)).group_by("security_id").agg(pl.col("last_date").max())
    con.execute(view_sql(get_contract("fundamentals_canonical"), s))
    con.register("ids_t", pl.DataFrame({"issuer_id": ids}).to_arrow())
    items = ",".join(f"'{x}'" for x in CORE_ITEMS)
    canon = con.execute(
        "SELECT c.issuer_id, c.item_id, c.period_kind, c.fiscal_period, c.period_end, c.source_id, c.knowledge_date "
        f"FROM fundamentals_canonical c JOIN ids_t USING (issuer_id) WHERE c.item_id IN ({items}) "
        f"AND c.period_kind IN ('FY', 'Q', 'H')").pl()
    fy, intr = latest_statements(canon, as_of)
    con.execute(view_sql(get_contract("fundamentals_raw"), s))
    raw_ents = set(con.execute("SELECT DISTINCT source_entity_id FROM fundamentals_raw WHERE source_id IN "
                               "('sec_edgar', 'sec_fsds', 'esef_xbrl_org', 'fca_nsm', 'companies_house', 'dila_fr', "
                               "'afm_nl', 'fsma_be', 'emarket_it', 'nasdaq_nordic', 'oslo_newsweb', 'cnmv_es')").pl()
                   ["source_entity_id"].to_list())
    con.close()
    extra: dict[str, Any] = {"raw_entities": raw_ents}
    up = s.data_dir / "reference" / "regulatory" / "unresolved.parquet"
    if up.exists():
        u = pl.read_parquet(up)
        extra["unresolved"] = dict(zip(u["issuer_key"].to_list(), u["reason"].to_list(), strict=True))
    sp = s.data_dir / "reference" / "seeds" / "regulatory_register.csv"
    if sp.exists():
        seeds = pl.read_csv(sp, infer_schema_length=0)
        extra["seeded"] = set(seeds["lei"].drop_nulls().to_list())
    cp = s.data_dir / "reference" / "eu_census" / "esef_filings.parquet"
    if cp.exists():
        extra["esef_leis"] = set(pl.read_parquet(cp, columns=["lei"])["lei"].drop_nulls().to_list())
    return {"reg": reg, "sec": sec, "iss": iss, "px": px, "fy": fy, "intr": intr, "region_last": region_last,
            "extra": extra}


def compute(settings: Settings | None = None, as_of: date | None = None, write: bool = True,
            run_id: str | None = None, docs: bool = True) -> dict[str, Any]:
    from app.core.storage import write_table

    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    x = _inputs(s, as_of)
    cov = classify(x["reg"], x["sec"], x["iss"], x["px"], x["fy"], x["intr"], as_of, x["region_last"], x["extra"])
    summary = summarize(cov, x["reg"])
    sp = s.state_dir / STATE_FILE
    prev = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    before = prev.get("before") or prev.get("summary")
    if write:
        now = datetime.now(UTC)
        out = cov.with_columns(pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at"),
                               pl.lit(run_id).cast(pl.Utf8).alias("run_id"))
        write_table(out, "regulatory_coverage", mode="overwrite_partition", settings=s)
        st = {"as_of": as_of.isoformat(), "computed_at": now.isoformat(timespec="seconds"), "summary": summary,
              "before": before or summary, "history": (prev.get("history") or [])[-30:] +
              [{"as_of": as_of.isoformat(), **{mk: v["pct"] for mk, v in summary.items()}}]}
        tmp = sp.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        tmp.replace(sp)
        if docs:
            write_markdown(markdown(summary, before, as_of.isoformat()), s)
            write_field_availability(field_block(summary, as_of.isoformat()), s)
    return {"summary": summary, "rows": cov.height}


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult

    p = dict(ctx.params or {})
    res = compute(ctx.settings, run_id=ctx.run_id, docs=bool(p.get("docs", True)))
    msg = "; ".join(f"{mk}: annual {v['pct']['annual']:.1f}% interim {v['pct']['interim']:.1f}% universe "
                    f"{v['pct']['universe']:.1f}%" for mk, v in res["summary"].items())
    return JobResult(status="success", rows_written={"regulatory_coverage": res["rows"]}, entities_ok=res["rows"],
                     message=msg)


if __name__ == "__main__":
    import sys

    from app.core.logsetup import setup_logging

    setup_logging(log_file="regulatory_coverage.log", service="cli")
    r = compute(write="--dry-run" not in sys.argv, docs="--no-docs" not in sys.argv)
    print(json.dumps(r["summary"], indent=1, default=str))
