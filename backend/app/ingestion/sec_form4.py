"""SEC Forms 3/4/5 (+/A) -> insider_transactions (job `sec_form4`), plus quarterly TSV backfill.

Nightly: filings_index rows (sec_edgar, form 3/4/5 and amendments) with knowledge_date > watermark, issuer CIK in the
universe (params.all_issuers=true for every issuer). One request per filing: the complete submission `<acc>.txt`,
which carries the SGML header (ACCEPTANCE-DATETIME, US/Eastern -> knowledge_ts) and the ownership XML between
<XML> tags. Raw XML is landed as one jsonl.gz bundle per run under data/raw/sec_edgar/form345/.

Backfill (params.mode = "tsv_backfill", params.quarters = ["2026q3", ...] or params.n_quarters): SEC Insider
Transactions Data Sets (Forms 3/4/5 since 2006, quarterly zips). Links are scraped from the landing page because the
SEC moved new quarters to /files/datastandardsinnovation/ while older ones stay under /files/structureddata/.

Parsing rules
  * One row per transaction line (non-derivative 'nd' and derivative 'dv' tables, holdings rows are skipped because
    they have no transaction date). txn_id = "sec_edgar:<acc>:<nd|dv>:<n>" (n = 1-based order within the table, the
    XML document order == ascending *_SK in the TSV datasets, so both paths produce the same ids).
  * Joint filings (several reporting owners): insider_* = first owner; is_director/officer/10% = OR over owners;
    n_reporting_owners recorded. Transactions are NOT duplicated per owner (avoids double counting).
  * Prices given only in footnotes (weighted averages) -> price null. Missing share counts -> row skipped (counted).
  * Transaction dates (A5 2026-11, plausible_txn_date): kept as filed up to 31 days after the filing date; a date
    later than that or before 1900 is a filer typo (07-JUN-2047, 01-FEB-0024) corrected when exactly one
    same-month-and-day candidate lies in the form's window (Forms 3/4: [period of report, filing date + 1 day]; Form 5:
    the fiscal year before the period), else Forms 3/4 filed at most TXN_PERIOD_FALLBACK_MAX_DAYS after their period
    of report take that period (the earliest transaction on the form; an approximation) and other rows are dropped.
    Labelled in the row: transaction_date_fix (century | year | period_of_report; null = as filed) and
    transaction_date_as_filed (the filer's date, set only when the parser changed it). Counted in the job message
    (txn_date_fixed / approx_period_of_report / txn_date_dropped) and logged per txn_id.
  * is_10b5_1: True if the form's aff10b5One checkbox is true or a footnote referenced by the row (or the remarks)
    mentions Rule 10b5-1; False if the checkbox is present and false; else null.
  * Amendments: rows of a 4/A supersede earlier rows with the same (issuer, owner, transaction_date, code, table,
    security_title); the earlier rows get superseded_by_filing_id (kept for PIT).
  * region = issuer region from the submissions API for universe issuers (foreign private issuers filing under the
    HFIAA, e.g. PBR -> LATAM); "US" otherwise.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl
from lxml import etree

from app.core.http import HttpError
from app.core.storage import scan_table
from app.ingestion.base import FetchResult, register_adapter
from app.ingestion.sec_index import (
    ARCHIVES,
    ET,
    SEC_SOURCE,
    SEC_WWW,
    SecAdapter,
    acc_nodash,
    base_form,
    cik10,
    filings_frame,
    form_class,
    is_amendment,
    make_filing_id,
    parse_header_acceptance,
    parse_since,
    read_existing_filings,
    to_date,
)

INSIDER_FORMS = {"3", "4", "5"}
TSV_LANDING = f"{SEC_WWW}/data-research/sec-markets-data/insider-transactions-data-sets"
RE_10B51 = re.compile(r"10b5-?1|10b-5-1|10b5\s*-\s*1", re.I)

TXN_TYPE = {
    "P": "buy", "S": "sell", "A": "grant", "M": "option_exercise", "X": "option_exercise", "O": "option_exercise",
    "F": "tax_withholding", "G": "gift", "C": "conversion",
}

TXN_SCHEMA = {
    "txn_id": pl.Utf8, "region": pl.Utf8, "year": pl.Int16, "filing_id": pl.Utf8, "source_id": pl.Utf8,
    "source_entity_id": pl.Utf8, "issuer_id": pl.Utf8, "security_id": pl.Utf8, "insider_id": pl.Utf8,
    "insider_name": pl.Utf8, "is_director": pl.Boolean, "is_officer": pl.Boolean, "is_ten_pct_owner": pl.Boolean,
    "officer_title": pl.Utf8, "role_norm": pl.Utf8, "transaction_date": pl.Date, "knowledge_date": pl.Date,
    "knowledge_ts": pl.Datetime("us", "UTC"), "txn_code": pl.Utf8, "txn_type": pl.Utf8, "is_open_market": pl.Boolean,
    "is_derivative": pl.Boolean, "is_10b5_1": pl.Boolean, "shares": pl.Float64, "price": pl.Float64,
    "currency": pl.Utf8, "value_local": pl.Float64, "value_usd": pl.Float64, "shares_owned_after": pl.Float64,
    "holding_change_pct": pl.Float64, "ownership_nature": pl.Utf8, "is_amendment": pl.Boolean,
    "ingested_at": pl.Datetime("us", "UTC"), "form_type": pl.Utf8, "security_title": pl.Utf8,
    "acquired_disposed": pl.Utf8, "issuer_ticker": pl.Utf8, "underlying_shares": pl.Float64,
    "exercise_price": pl.Float64, "n_reporting_owners": pl.Int16, "superseded_by_filing_id": pl.Utf8,
    # A5 2026-11 provenance of a changed transaction date (plausible_txn_date); null on rows stored as filed
    "transaction_date_as_filed": pl.Date, "transaction_date_fix": pl.Utf8,
}


# ----------------------------------------------------------------------------------------------- helpers


def role_norm(is_dir: bool, is_off: bool, is_ten: bool, title: str | None, other: bool = False) -> str:
    t = (title or "").lower()
    if is_off or t:
        if re.search(r"\bchief executive\b|\bceo\b", t):
            return "ceo"
        if re.search(r"\bchief financial\b|\bcfo\b|principal financial", t):
            return "cfo"
        if re.search(r"\bchief operating\b|\bcoo\b", t):
            return "coo"
        if re.search(r"\bchair", t):
            return "chair"
        if re.search(r"\bpresident\b", t):
            return "president"
        if is_off:
            return "officer_other"
    if is_dir:
        return "director"
    if is_ten:
        return "owner"
    return "other"


def _f(x: Any) -> float | None:
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _b(x: Any) -> bool:
    return str(x or "").strip().lower() in ("1", "true", "yes", "y", "x")


def holding_change(shares: float, after: float | None, ad: str | None) -> float | None:
    if after is None or not shares:
        return None
    if ad == "A":
        before = after - shares
        return shares / before if before > 0 else None
    if ad == "D":
        before = after + shares
        return -shares / before if before > 0 else None
    return None


#: A5 2026-11 transaction-date plausibility. A Form 3/4/5 reports transactions that already happened, yet EDGAR's
#: ownership XML and the SEC Insider Transactions Data Sets carry filer typos verbatim (07-JUN-2047 for 2017-06-07,
#: 01-FEB-0024 for 2024-02-01, 24-JUL-2028 for 2018-07-24, an option's expiration year typed into the transaction
#: date) and `to_date` accepts any year. Up to TXN_AFTER_FILING_TOL_DAYS after the filing date a date is kept as filed
#: (pre-reported scheduled vesting, day / month slips: no unambiguous fix; dq knowledge_before:transaction_date warns);
#: beyond it, or before TXN_DATE_FLOOR, the date is certainly wrong.
TXN_DATE_FLOOR = date(1900, 1, 1)
TXN_AFTER_FILING_TOL_DAYS = 31
#: lower bound of the repair window when the period of report is itself implausible (late filings stay possible)
TXN_REPAIR_LOOKBACK_DAYS = 366
#: the period-of-report approximation only for a form filed at most this many days after its period (Form 4 is due
#: within two business days; slack for holidays and short delays). A form filed months or years after its period (a
#: late batch, or a typo'd period: 2021-07-19 on a 2024-07-19 filing) says nothing about the row's date -> dropped
TXN_PERIOD_FALLBACK_MAX_DAYS = 10
#: plausible_txn_date fixes that store a changed date (labelled in transaction_date_fix)
TXN_DATE_FIXES = ("century", "year", "period_of_report")


def plausible_txn_date(td: date | None, filed: date, period: date | None = None,
                       form: str | None = None) -> tuple[date | None, str | None]:
    """(transaction date to store, fix) for a filed transaction date. Every stored date is <= filing date + 1 day, so
    a fix never moves a transaction after it became public (knowledge_date is the filing date either way).
      None               kept as filed (plausible, or at most TXN_AFTER_FILING_TOL_DAYS after the filing)
      'century'/'year'   certainly wrong; exactly one candidate with the same month and day (a two-digit year as
                         19xx / 20xx, or another year) lies in the window: Forms 3/4 [period of report = the form's
                         earliest transaction, filing date + 1]; Form 5 (period = fiscal year end) [period - 366 d,
                         filing date + 1]; period implausible [filing date - 366 d, filing date + 1]
      'period_of_report' Forms 3/4 filed <= TXN_PERIOD_FALLBACK_MAX_DAYS after their period, no unique candidate (e.g.
                         a month slip 2023-11-03 for 2023-01-03): the period of report, i.e. the earliest transaction
                         date on the form (an approximation, labelled in the row's transaction_date_fix)
      'dropped'          certainly wrong, no candidate and no usable period -> None (the row is skipped)"""
    if td is None:
        return None, None
    if TXN_DATE_FLOOR <= td <= filed + timedelta(days=TXN_AFTER_FILING_TOL_DAYS):
        return td, None
    hi = filed + timedelta(days=1)
    per_ok = period is not None and TXN_DATE_FLOOR <= period <= hi
    annual = (form or "").split("/")[0].strip() == "5"
    if per_ok:
        lo = period - timedelta(days=TXN_REPAIR_LOOKBACK_DAYS) if annual else period
    else:
        lo = filed - timedelta(days=TXN_REPAIR_LOOKBACK_DAYS)
    years = (2000 + td.year, 1900 + td.year) if td.year < 100 else range(lo.year, hi.year + 1)
    cands = set()
    for y in years:
        try:
            c = date(y, td.month, td.day)
        except ValueError:  # 29 Feb in a non-leap year
            continue
        if lo <= c <= hi:
            cands.add(c)
    if len(cands) == 1:
        return cands.pop(), ("century" if td.year < 100 else "year")
    if per_ok and not annual and (filed - period).days <= TXN_PERIOD_FALLBACK_MAX_DAYS:
        return period, "period_of_report"
    return None, "dropped"


def date_fix_fields(raw: date | None, fix: str | None) -> dict[str, Any]:
    """The row's provenance columns for a plausible_txn_date result (both null when the date is stored as filed)."""
    if fix in TXN_DATE_FIXES:
        return {"transaction_date_as_filed": raw, "transaction_date_fix": fix}
    return {"transaction_date_as_filed": None, "transaction_date_fix": None}


def _plausible_period(period: date | None, filed: date) -> date | None:
    """Period of report of a Form 3/4/5 (date of the earliest transaction): same rules as a transaction date, without
    a window of its own (the filing date bounds it); certainly wrong and ambiguous -> None."""
    return plausible_txn_date(period, filed)[0]


def flag_10b5_1(checkbox: bool | None, row_mentions: list[bool], remarks_mention: bool) -> list[bool | None]:
    """Per-row Rule 10b5-1 flag. The filing-level checkbox (aff10b5One, 2023+ forms) is authoritative:
    unchecked -> False for every row; checked -> rows whose footnotes mention 10b5-1 (or every row when no row-level
    footnote mentions it); absent (older forms) -> True where a footnote/remark mentions 10b5-1, else None."""
    any_row = any(row_mentions)
    out: list[bool | None] = []
    for m in row_mentions:
        if checkbox is False:
            out.append(False)
        elif checkbox is True:
            out.append(m or not any_row)
        else:
            out.append(True if (m or remarks_mention) else None)
    return out


def insider_key(cik: str | None, name: str) -> str:
    return f"{SEC_SOURCE}:{cik}" if cik else f"{SEC_SOURCE}:" + re.sub(r"\W+", "_", (name or "UNKNOWN").upper()).strip("_")


def order_owners(owners: list[dict]) -> list[dict]:
    """Deterministic primary owner (XML and TSV list joint owners in different orders): officers/directors first,
    then 10% owners, then by CIK."""
    return sorted(owners, key=lambda o: (not (o["is_officer"] or o["is_director"]), not o["is_ten"], o["cik"] or "~", o["name"]))


def _unq(s: str | None) -> str | None:
    """Undo CSV-style quoting left in the SEC TSV datasets (a value wrapped in double quotes with doubled inner
    quotes is unwrapped)."""
    if s is None:
        return None
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('""', '"')
    return s or None


def _row(**kw: Any) -> dict[str, Any]:
    base = dict.fromkeys(TXN_SCHEMA)
    base.update(kw)
    return base


def finish_row(r: dict[str, Any]) -> dict[str, Any] | None:
    """Derive txn_type / values; return None if the row has no share count."""
    if r.get("shares") is None or r.get("transaction_date") is None:
        return None
    r["shares"] = abs(r["shares"])
    code = (r.get("txn_code") or "").upper() or None
    r["txn_code"] = code
    r["txn_type"] = TXN_TYPE.get(code or "", "other")
    r["is_open_market"] = code in ("P", "S")
    r["currency"] = "USD"
    if r.get("price") is not None:
        r["value_local"] = r["shares"] * r["price"]
        r["value_usd"] = r["value_local"]
    r["holding_change_pct"] = holding_change(r["shares"], r.get("shares_owned_after"), r.get("acquired_disposed"))
    if r.get("acquired_disposed") not in ("A", "D"):
        r["acquired_disposed"] = None
    on = (r.get("ownership_nature") or "").strip().upper()
    r["ownership_nature"] = {"D": "direct", "I": "indirect"}.get(on)
    r["year"] = r["knowledge_date"].year
    return r


# ----------------------------------------------------------------------------------------------- XML parsing


def split_submission(txt: str) -> tuple[dict[str, str], bytes | None]:
    """Complete submission .txt -> (header fields, ownership XML bytes)."""
    hdr: dict[str, str] = {}
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", txt)
    if m:
        hdr["acceptance"] = m.group(1)
    for key, pat in (("form", r"CONFORMED SUBMISSION TYPE:\s*(\S+)"), ("period", r"CONFORMED PERIOD OF REPORT:\s*(\d{8})"),
                     ("filed", r"FILED AS OF DATE:\s*(\d{8})")):
        mm = re.search(pat, txt)
        if mm:
            hdr[key] = mm.group(1)
    xm = re.search(r"<XML>\s*(.*?)\s*</XML>", txt, re.S | re.I)
    xml = None
    if xm and "ownershipDocument" in xm.group(1):
        xml = xm.group(1).strip().encode("utf-8")
    else:  # some filings carry several documents; find the one with the ownership document
        for blk in re.findall(r"<XML>(.*?)</XML>", txt, re.S | re.I):
            if "ownershipDocument" in blk:
                xml = blk.strip().encode("utf-8")
                break
    return hdr, xml


_PARSER = etree.XMLParser(recover=True, remove_blank_text=True, resolve_entities=False, no_network=True)


def _txt(el: etree._Element | None, path: str) -> str | None:
    if el is None:
        return None
    n = el.find(path)
    if n is None:
        return None
    v = n.find("value")
    s = (v.text if v is not None else n.text) or ""
    s = s.strip()
    return s or None


def _fn_ids(el: etree._Element) -> set[str]:
    return {f.get("id") for f in el.iter("footnoteId") if f.get("id")}


def parse_ownership_xml(xml: bytes, accession: str, filed: date, acceptance: datetime | None = None,
                        form_type: str | None = None, region: str = "US",
                        now: datetime | None = None) -> tuple[list[dict], dict[str, Any]]:
    """Parse one ownershipDocument. Returns (transaction rows, filing meta {issuer_cik, period, form, n_skipped})."""
    now = now or datetime.now(UTC)
    root = etree.fromstring(xml.lstrip(), _PARSER)
    if root is None:
        raise ValueError("unparseable ownership XML")
    if root.tag != "ownershipDocument":
        found = root.find(".//ownershipDocument")
        root = found if found is not None else root
    doc_type = (root.findtext("documentType") or form_type or "").strip()
    form = form_type or doc_type
    issuer = root.find("issuer")
    issuer_cik = cik10(issuer.findtext("issuerCik")) if issuer is not None and issuer.findtext("issuerCik") else None
    ticker = (issuer.findtext("issuerTradingSymbol") or "").strip().upper() if issuer is not None else ""
    owners = []
    for ro in root.findall("reportingOwner"):
        rid = ro.find("reportingOwnerId")
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "cik": cik10(rid.findtext("rptOwnerCik")) if rid is not None and (rid.findtext("rptOwnerCik") or "").strip() else None,
            "name": ((rid.findtext("rptOwnerName") if rid is not None else None) or "").strip(),
            "is_director": _b(rel.findtext("isDirector")) if rel is not None else False,
            "is_officer": _b(rel.findtext("isOfficer")) if rel is not None else False,
            "is_ten": _b(rel.findtext("isTenPercentOwner")) if rel is not None else False,
            "is_other": _b(rel.findtext("isOther")) if rel is not None else False,
            "title": ((rel.findtext("officerTitle") if rel is not None else None) or "").strip() or None,
        })
    if not owners:
        owners = [{"cik": None, "name": "UNKNOWN", "is_director": False, "is_officer": False, "is_ten": False,
                   "is_other": False, "title": None}]
    owners = order_owners(owners)
    first = owners[0]
    any_dir = any(o["is_director"] for o in owners)
    any_off = any(o["is_officer"] for o in owners)
    any_ten = any(o["is_ten"] for o in owners)
    title = next((o["title"] for o in owners if o["title"]), None)
    insider_id = insider_key(first["cik"], first["name"])
    footnotes = {f.get("id"): (f.text or "") for f in root.iter("footnote") if f.get("id")}
    remarks = root.findtext("remarks") or ""
    aff = root.find("aff10b5One")
    aff_val = _b(aff.text) if aff is not None and (aff.text or "").strip() else None
    fid = make_filing_id(accession)
    common = {
        "region": region, "filing_id": fid, "source_id": SEC_SOURCE, "source_entity_id": issuer_cik,
        "insider_id": insider_id, "insider_name": first["name"] or "UNKNOWN", "is_director": any_dir,
        "is_officer": any_off, "is_ten_pct_owner": any_ten, "officer_title": title,
        "role_norm": role_norm(any_dir, any_off, any_ten, title), "knowledge_date": filed, "knowledge_ts": acceptance,
        "is_amendment": is_amendment(form), "ingested_at": now, "form_type": form, "issuer_ticker": ticker or None,
        "n_reporting_owners": len(owners),
    }
    rows: list[dict] = []
    skipped = 0
    period = _plausible_period(to_date(root.findtext("periodOfReport")), filed)
    date_fixes: list[dict[str, Any]] = []
    for table, tag, is_deriv in (("nd", "nonDerivativeTable/nonDerivativeTransaction", False),
                                 ("dv", "derivativeTable/derivativeTransaction", True)):
        for i, t in enumerate(root.findall(tag), start=1):
            ids = _fn_ids(t)
            mention = any(RE_10B51.search(footnotes.get(x, "")) for x in ids)
            r = _row(**common)
            raw_td = to_date(_txt(t, "transactionDate"))
            td, fix = plausible_txn_date(raw_td, filed, period, form)
            if fix:
                date_fixes.append({"txn_id": f"{fid}:{table}:{i}", "filed": raw_td, "stored": td, "fix": fix})
            r.update({
                "txn_id": f"{fid}:{table}:{i}",
                "security_title": _txt(t, "securityTitle"),
                "transaction_date": td,
                "txn_code": (t.findtext("transactionCoding/transactionCode") or "").strip() or None,
                "shares": _f(_txt(t, "transactionAmounts/transactionShares")),
                "price": _f(_txt(t, "transactionAmounts/transactionPricePerShare")),
                "acquired_disposed": _txt(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares_owned_after": _f(_txt(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                "ownership_nature": _txt(t, "ownershipNature/directOrIndirectOwnership"),
                "is_derivative": is_deriv,
                "__mention": mention,
                **date_fix_fields(raw_td, fix),
            })
            if is_deriv:
                r["underlying_shares"] = _f(_txt(t, "underlyingSecurity/underlyingSecurityShares"))
                r["exercise_price"] = _f(_txt(t, "conversionOrExercisePrice"))
            out = finish_row(r)
            if out is None:
                skipped += 1
                continue
            rows.append(out)
    flags = flag_10b5_1(aff_val, [r.pop("__mention") for r in rows], bool(RE_10B51.search(remarks)))
    for r, f in zip(rows, flags, strict=True):
        r["is_10b5_1"] = f
    meta = {"issuer_cik": issuer_cik, "period": period, "form": form,
            "n_skipped": skipped, "owner_ciks": [o["cik"] for o in owners if o["cik"]], "date_fixes": date_fixes}
    return rows, meta


def txn_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=TXN_SCHEMA, orient="row") if rows else pl.DataFrame(schema=TXN_SCHEMA)


# ----------------------------------------------------------------------------------------------- amendments


AMEND_KEY = ["source_entity_id", "insider_id", "transaction_date", "txn_code", "is_derivative", "security_title"]


def mark_superseded(new: pl.DataFrame, existing: pl.DataFrame | None = None) -> pl.DataFrame:
    """Set superseded_by_filing_id on rows restated by a LATER amendment (4/A) with the same AMEND_KEY.
    Returns the union of `new` and the touched `existing` rows (to be upserted)."""
    allr = new if existing is None or existing.is_empty() else pl.concat(
        [existing.select(new.columns), new], how="vertical_relaxed").unique("txn_id", keep="last", maintain_order=True)
    amends = allr.filter(pl.col("is_amendment")).select(
        AMEND_KEY + [pl.col("filing_id").alias("__amd_fid"), pl.col("knowledge_date").alias("__amd_kd")])
    if amends.is_empty():
        return new
    j = allr.join(amends, on=AMEND_KEY, how="left", nulls_equal=True)
    # an amendment supersedes rows of OTHER filings known earlier (or same day with a smaller accession)
    j = j.filter(pl.col("__amd_fid").is_null() | (pl.col("__amd_fid") != pl.col("filing_id")))
    cand = j.filter(pl.col("__amd_fid").is_not_null()
                    & ((pl.col("__amd_kd") > pl.col("knowledge_date"))
                       | ((pl.col("__amd_kd") == pl.col("knowledge_date")) & (pl.col("__amd_fid") > pl.col("filing_id")))))
    # earliest superseding amendment per row
    sup = cand.sort(["__amd_kd", "__amd_fid"]).group_by("txn_id", maintain_order=True).agg(pl.col("__amd_fid").first().alias("__sup"))
    out = allr.join(sup, on="txn_id", how="left").with_columns(
        pl.coalesce(pl.col("__sup"), pl.col("superseded_by_filing_id")).alias("superseded_by_filing_id")).drop("__sup")
    touched = set(new["txn_id"].to_list()) | set(sup["txn_id"].to_list())
    return out.filter(pl.col("txn_id").is_in(list(touched)))


# ----------------------------------------------------------------------------------------------- TSV backfill


def _fn_list(x: str | None) -> list[str]:
    return [s.strip() for s in (x or "").split(",") if s.strip()]


def parse_insider_tsv_zip(zbytes_or_path: Any, region_of: dict[str, str] | None = None,
                          issuer_filter: set[str] | None = None, now: datetime | None = None,
                          date_fixes: list[dict[str, Any]] | None = None) -> pl.DataFrame:
    """SEC Insider Transactions Data Set zip -> insider_transactions rows (same txn_id scheme as the XML path).
    Transaction dates go through plausible_txn_date (PERIOD_OF_REPORT bounds the repair); corrected / dropped dates
    are appended to `date_fixes` when given."""
    now = now or datetime.now(UTC)
    region_of = region_of or {}
    z = zipfile.ZipFile(io.BytesIO(zbytes_or_path) if isinstance(zbytes_or_path, bytes) else zbytes_or_path)

    def tsv(name: str) -> pl.DataFrame:
        member = next(n for n in z.namelist() if n.upper().endswith(name))
        return pl.read_csv(z.read(member), separator="\t", infer_schema=False, quote_char=None,
                           truncate_ragged_lines=True, encoding="utf8-lossy")

    sub = tsv("SUBMISSION.TSV")
    if issuer_filter is not None:
        sub = sub.filter(pl.col("ISSUERCIK").str.zfill(10).is_in(list(issuer_filter)))
    accs = set(sub["ACCESSION_NUMBER"].to_list())
    own = tsv("REPORTINGOWNER.TSV").filter(pl.col("ACCESSION_NUMBER").is_in(list(accs)))
    fns = tsv("FOOTNOTES.TSV").filter(pl.col("ACCESSION_NUMBER").is_in(list(accs)))
    nd = tsv("NONDERIV_TRANS.TSV").filter(pl.col("ACCESSION_NUMBER").is_in(list(accs)))
    dv = tsv("DERIV_TRANS.TSV").filter(pl.col("ACCESSION_NUMBER").is_in(list(accs)))
    fn_map: dict[str, dict[str, str]] = {}
    for a, i, t in fns.select("ACCESSION_NUMBER", "FOOTNOTE_ID", "FOOTNOTE_TXT").iter_rows():
        fn_map.setdefault(a, {})[i] = _unq(t) or ""
    owners: dict[str, list[dict]] = {}
    for r in own.iter_rows(named=True):
        rel = (r.get("RPTOWNER_RELATIONSHIP") or "").lower()
        owners.setdefault(r["ACCESSION_NUMBER"], []).append({
            "cik": cik10(r["RPTOWNERCIK"]) if (r.get("RPTOWNERCIK") or "").strip() else None,
            "name": _unq(r.get("RPTOWNERNAME")) or "",
            "is_director": "director" in rel, "is_officer": "officer" in rel, "is_ten": "tenpercent" in rel,
            "title": _unq(r.get("RPTOWNER_TITLE")),
        })
    subs = {r["ACCESSION_NUMBER"]: r for r in sub.iter_rows(named=True)}
    rows: list[dict] = []
    for table, df, is_deriv in (("nd", nd, False), ("dv", dv, True)):
        sk = "DERIV_TRANS_SK" if is_deriv else "NONDERIV_TRANS_SK"
        df = df.with_columns(pl.col(sk).cast(pl.Int64, strict=False)).sort(["ACCESSION_NUMBER", sk])
        df = df.with_columns(pl.int_range(1, pl.len() + 1).over("ACCESSION_NUMBER").alias("__line"))
        for r in df.iter_rows(named=True):
            acc = r["ACCESSION_NUMBER"]
            s = subs.get(acc)
            if s is None:
                continue
            filed = to_date(s.get("FILING_DATE"))
            if filed is None:
                continue
            raw_td = to_date(r.get("TRANS_DATE"))
            td, fix = plausible_txn_date(raw_td, filed, _plausible_period(to_date(s.get("PERIOD_OF_REPORT")), filed),
                                         s.get("DOCUMENT_TYPE"))
            if fix and date_fixes is not None:
                date_fixes.append({"txn_id": f"{make_filing_id(acc)}:{table}:{r['__line']}", "filed": raw_td,
                                   "stored": td, "fix": fix})
            os_ = owners.get(acc) or [{"cik": None, "name": "UNKNOWN", "is_director": False, "is_officer": False,
                                        "is_ten": False, "title": None}]
            os_ = order_owners(os_)
            first = os_[0]
            any_dir, any_off, any_ten = (any(o[k] for o in os_) for k in ("is_director", "is_officer", "is_ten"))
            title = next((o["title"] for o in os_ if o["title"]), None)
            issuer = cik10(s["ISSUERCIK"])
            fnotes = fn_map.get(acc, {})
            ids = set()
            for col, val in r.items():
                if col.endswith("_FN"):
                    ids.update(_fn_list(val))
            aff_raw = (s.get("AFF10B5ONE") or "").strip()
            aff_val = _b(aff_raw) if aff_raw else None
            mention = any(RE_10B51.search(fnotes.get(x, "")) for x in ids)
            form = (s.get("DOCUMENT_TYPE") or "").strip()
            fid = make_filing_id(acc)
            row = _row(
                txn_id=f"{fid}:{table}:{r['__line']}", region=region_of.get(issuer, "US"), filing_id=fid,
                source_id=SEC_SOURCE, source_entity_id=issuer,
                insider_id=insider_key(first["cik"], first["name"]),
                insider_name=first["name"] or "UNKNOWN", is_director=any_dir, is_officer=any_off,
                is_ten_pct_owner=any_ten, officer_title=title, role_norm=role_norm(any_dir, any_off, any_ten, title),
                transaction_date=td, knowledge_date=filed, knowledge_ts=None,
                txn_code=(r.get("TRANS_CODE") or "").strip() or None, is_derivative=is_deriv,
                shares=_f(r.get("TRANS_SHARES")), price=_f(r.get("TRANS_PRICEPERSHARE")),
                shares_owned_after=_f(r.get("SHRS_OWND_FOLWNG_TRANS")),
                ownership_nature=r.get("DIRECT_INDIRECT_OWNERSHIP"), is_amendment=is_amendment(form),
                ingested_at=now, form_type=form, security_title=_unq(r.get("SECURITY_TITLE")),
                acquired_disposed=(r.get("TRANS_ACQUIRED_DISP_CD") or "").strip() or None,
                issuer_ticker=(s.get("ISSUERTRADINGSYMBOL") or "").strip().upper() or None,
                underlying_shares=_f(r.get("UNDLYNG_SEC_SHARES")) if is_deriv else None,
                exercise_price=_f(r.get("CONV_EXERCISE_PRICE")) if is_deriv else None,
                n_reporting_owners=len(os_),
                **date_fix_fields(raw_td, fix),
            )
            out = finish_row(row)
            if out is not None:
                out["__mention"], out["__aff"] = mention, aff_val
                out["__rem"] = bool(RE_10B51.search(_unq(s.get("REMARKS")) or ""))
                rows.append(out)
    by_acc: dict[str, list[dict]] = {}
    for r in rows:
        by_acc.setdefault(r["filing_id"], []).append(r)
    for rs in by_acc.values():
        flags = flag_10b5_1(rs[0]["__aff"], [r["__mention"] for r in rs], rs[0]["__rem"])
        for r, f in zip(rs, flags, strict=True):
            r["is_10b5_1"] = f
    for r in rows:
        for k in ("__mention", "__aff", "__rem"):
            r.pop(k, None)
    return txn_frame(rows)


def tsv_dataset_links(html: str) -> dict[str, str]:
    """Landing page HTML -> {'2026q3': absolute URL}."""
    out = {}
    for href in re.findall(r'href="([^"]+?(\d{4}q[1-4])_form345\.zip)"', html, re.I):
        url, key = href
        out[key.lower()] = url if url.startswith("http") else SEC_WWW + url
    return out


# ----------------------------------------------------------------------------------------------- adapter


@register_adapter
class SecForm4Adapter(SecAdapter):
    source_id = "sec_form4"
    produces = ("insider_transactions", "filings_index")

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}

    def _get_submission(self, cik: str, acc: str) -> tuple[str, dict, bytes | None]:
        url = f"{ARCHIVES}/data/{int(cik)}/{acc_nodash(acc)}/{acc}.txt"
        r = self.http.get(url, cache_ttl=-1)
        hdr, xml = split_submission(r.content.decode("utf-8", errors="replace"))
        return acc, hdr, xml

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SEC_SOURCE, started_at=datetime.now(UTC))
        if self.params.get("mode") == "tsv_backfill":
            return self._fetch_tsv(res)
        s = parse_since(since) or (datetime.now(ET).date() - timedelta(days=int(self.params.get("lookback_days", 90))))
        fi = read_existing_filings(self.settings, filters=pl.col("knowledge_date") > s)
        # the watermark follows the filings index (all forms), so quiet days still advance it
        res.watermark = (fi["knowledge_date"].max() if fi.height else s).isoformat()
        fi = fi.filter(pl.col("form_type").map_elements(base_form, return_dtype=pl.Utf8).is_in(list(INSIDER_FORMS)))
        if entities:
            fi = fi.filter(pl.col("source_entity_id").is_in([cik10(c) for c in entities]))
        elif not self.params.get("all_issuers"):
            fi = fi.filter(pl.col("source_entity_id").is_in(self.universe_ciks(self.params)))
        todo = fi.select("filing_id", "source_entity_id", "form_type", "knowledge_date").to_dicts()
        bundle: list[dict] = []
        with ThreadPoolExecutor(max_workers=int(self.params.get("workers", 8))) as ex:
            futs = {ex.submit(self._get_submission, r["source_entity_id"], r["filing_id"].split(":", 1)[1]): r for r in todo}
            for fut in as_completed(futs):
                r = futs[fut]
                acc = r["filing_id"].split(":", 1)[1]
                try:
                    _, hdr, xml = fut.result()
                    if xml is None:
                        res.entities_failed[acc] = "no ownership XML in submission"
                        continue
                    bundle.append({"accession": acc, "index_cik": r["source_entity_id"], "form": r["form_type"],
                                   "filed": r["knowledge_date"].isoformat(), "acceptance": hdr.get("acceptance"),
                                   "xml": xml.decode("utf-8", errors="replace")})
                    res.entities_ok.append(acc)
                except HttpError as e:
                    res.entities_failed[acc] = str(e)[:300]
                except Exception as e:  # noqa: BLE001
                    res.entities_failed[acc] = f"{type(e).__name__}: {str(e)[:250]}"
        if bundle:
            payload = b"\n".join(orjson.dumps(b) for b in sorted(bundle, key=lambda x: x["accession"]))
            name = f"form345_{s.isoformat()}_{datetime.now(UTC).strftime('%H%M%S')}.jsonl"
            res.raw_paths.append(self.save_raw("form345", name, payload))
        res.meta["n_filings"] = len(todo)
        return res

    def _fetch_tsv(self, res: FetchResult) -> FetchResult:
        html = self.http.get(TSV_LANDING, cache_ttl=6 * 3600).text
        links = tsv_dataset_links(html)
        quarters = [q.lower() for q in self.params.get("quarters") or []]
        if not quarters:
            quarters = sorted(links, reverse=True)[: int(self.params.get("n_quarters", 1))]
        for q in quarters:
            if q not in links:
                res.entities_failed[q] = "dataset link not on landing page"
                continue
            dest = self.settings.raw_dir / SEC_SOURCE / "insider_tsv" / f"{q}_form345.zip"
            try:
                if not dest.exists():
                    self.http.download(links[q], dest)
                res.raw_paths.append(dest)
                res.entities_ok.append(q)
            except Exception as e:  # noqa: BLE001
                res.entities_failed[q] = str(e)[:300]
        res.meta["mode"] = "tsv_backfill"
        return res

    def _regions(self, ciks: set[str]) -> dict[str, str]:
        universe = set(self.universe_ciks(self.params))
        meta = self.issuer_meta(ciks & universe)
        return {c: m.get("region") or "US" for c, m in meta.items()}

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        now = datetime.now(UTC)
        if fetched.meta.get("mode") == "tsv_backfill":
            universe = set(self.universe_ciks(self.params))
            regions = self._regions(universe)
            filt = universe if self.params.get("universe_only") else None
            fixes: list[dict[str, Any]] = []
            frames = [parse_insider_tsv_zip(p, regions, filt, now, date_fixes=fixes) for p in fetched.raw_paths]
            df = pl.concat(frames, how="vertical_relaxed") if frames else txn_frame([])
            fetched.meta["n_rows"] = df.height
            self._note_date_fixes(fetched, fixes)
            return {"insider_transactions": self._finalize(df, keep_existing=True)}
        rows: list[dict] = []
        fil_rows: list[dict] = []
        skipped = 0
        fixes = []
        recs = []
        for p in fetched.raw_paths:
            for line in self.load_raw(p).splitlines():
                if line.strip():
                    recs.append(orjson.loads(line))
        for rec in recs:
            try:
                acc = rec["accession"]
                filed = date.fromisoformat(rec["filed"])
                acc_ts = parse_header_acceptance(rec.get("acceptance"))
                tx, meta = parse_ownership_xml(rec["xml"].encode("utf-8"), acc, filed, acc_ts, rec.get("form"),
                                               "US", now)
                issuer = meta["issuer_cik"] or rec["index_cik"]
                for t in tx:
                    t["source_entity_id"] = issuer
                rows.extend(tx)
                skipped += meta["n_skipped"]
                fixes.extend(meta.get("date_fixes") or [])
                fil_rows.append({"filing_id": make_filing_id(acc), "source_entity_id": issuer, "period_end": meta["period"],
                                 "knowledge_ts": acc_ts, "form_type": rec.get("form") or meta["form"]})
            except Exception as e:  # noqa: BLE001
                fetched.entities_failed[rec.get("accession", "?")] = f"parse: {type(e).__name__}: {str(e)[:200]}"
        fetched.meta["n_rows_skipped_no_shares"] = skipped
        self._note_date_fixes(fetched, fixes)
        if not self.params.get("all_issuers") and not self.params.get("ciks"):
            # the index row may carry the reporting owner's CIK (e.g. BAC reporting trades in its own funds):
            # keep only transactions whose ISSUER (from the XML) is in the universe
            uni = set(self.universe_ciks(self.params))
            n0 = len(rows)
            rows = [r for r in rows if r["source_entity_id"] in uni]
            fetched.meta["n_rows_non_universe_issuer"] = n0 - len(rows)
        regions = self._regions({r["source_entity_id"] for r in rows})
        for r in rows:
            r["region"] = regions.get(r["source_entity_id"], "US")
        df = self._finalize(txn_frame(rows))
        if getattr(self, "identity_note", None):
            fetched.meta["identity_note"] = self.identity_note
        out = {"insider_transactions": df}
        fi = self._filings_updates(fil_rows)
        if fi.height:
            out["filings_index"] = fi
        return out

    def _note_date_fixes(self, fetched: FetchResult, fixes: list[dict[str, Any]]) -> None:
        """Report the certainly-wrong transaction dates of this run (plausible_txn_date): counts in the job message,
        every txn_id with the date as filed and as stored in the log. The rows carry the label themselves
        (transaction_date_fix / transaction_date_as_filed); the raw XML / TSV keep the original."""
        by = {k: sum(1 for f in fixes if f["fix"] == k) for k in ("century", "year", "period_of_report", "dropped")}
        fetched.meta["n_txn_date_fixed"] = by["century"] + by["year"] + by["period_of_report"]
        fetched.meta["n_txn_date_approx"] = by["period_of_report"]
        fetched.meta["n_txn_date_dropped"] = by["dropped"]
        for f in fixes:
            self.log.warning("sec_form4: %s transaction date as filed %s -> %s (%s)", f["txn_id"], f["filed"],
                             f["stored"] or "row dropped",
                             {"dropped": "ambiguous", "period_of_report": "approximation: period of report"}.get(
                                 f["fix"], f"{f['fix']} fix"))

    def _finalize(self, df: pl.DataFrame, keep_existing: bool = False) -> pl.DataFrame:
        """Dedupe, optional identity resolution, amendment linking. keep_existing=True (TSV backfill): rows already in
        the table (parsed from the XML, full precision + knowledge_ts) are not overwritten."""
        if df.is_empty():
            return df
        df = df.unique("txn_id", keep="last", maintain_order=True)
        # resolve issuer/security if the identity layer can (optional, but never silent: F24)
        self.identity_note = None
        try:
            from app.ingestion.sec_index import try_lookup_many  # noqa: PLC0415

            m = try_lookup_many("cik", df["source_entity_id"].unique().to_list())
            if m:
                iss = {k: v.get("issuer_id") for k, v in m.items()}
                sec = {k: v.get("primary_security_id") or v.get("security_id") for k, v in m.items()}
                df = df.with_columns(
                    pl.col("source_entity_id").replace_strict(iss, default=None).alias("issuer_id"),
                    pl.col("source_entity_id").replace_strict(sec, default=None).alias("security_id"))
            n_unmapped = df.filter(pl.col("issuer_id").is_null())["source_entity_id"].n_unique() if "issuer_id" in df.columns else None
            if n_unmapped:
                self.identity_note = f"{n_unmapped} issuer CIK(s) not in identifier_map (issuer_id null)"
        except Exception as e:  # noqa: BLE001
            self.identity_note = f"identity resolution failed: {type(e).__name__}: {str(e)[:200]}"
            self.log.warning("sec_form4: %s", self.identity_note)
        issuers = df["source_entity_id"].unique().to_list()
        try:
            existing = (scan_table("insider_transactions", self.settings)
                        .filter((pl.col("source_id") == SEC_SOURCE) & pl.col("source_entity_id").is_in(issuers))
                        .collect())
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None and existing.height:
            # partitions written before a column was added (A5: transaction_date_as_filed / _fix) lack it: null
            existing = existing.with_columns([pl.lit(None, dtype=t).alias(k) for k, t in TXN_SCHEMA.items()
                                              if k not in existing.columns]).select(list(TXN_SCHEMA))
            # region is a partition column and depends on the universe at write time; a txn already stored keeps its
            # region so a re-ingest can never place the same txn_id in two partitions (upsert dedupes per partition)
            prev_region = dict(zip(existing["txn_id"].to_list(), existing["region"].to_list(), strict=True))
            df = df.with_columns(pl.col("txn_id").replace_strict(prev_region, default=pl.col("region"))
                                 .alias("region"))
            if keep_existing:
                df = df.filter(~pl.col("txn_id").is_in(existing["txn_id"].implode()))
        return mark_superseded(df.select(list(TXN_SCHEMA)), existing)

    def _filings_updates(self, fil_rows: list[dict]) -> pl.DataFrame:
        """Correct Form 3/4/5 filings_index rows: issuer CIK, period of report, acceptance timestamp."""
        if not fil_rows:
            return filings_frame([])
        upd = {r["filing_id"]: r for r in fil_rows}
        prev = read_existing_filings(self.settings, list(upd))
        if prev.is_empty():
            return filings_frame([])
        recs = []
        for r in prev.to_dicts():
            u = upd[r["filing_id"]]
            r["source_entity_id"] = u["source_entity_id"] or r["source_entity_id"]
            r["period_end"] = u["period_end"] or r["period_end"]
            r["knowledge_ts"] = u["knowledge_ts"] or r["knowledge_ts"]
            r["form_class"] = form_class(r["form_type"])
            recs.append(r)
        return filings_frame(recs)


# ----------------------------------------------------------------------------------------------- job entry


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = SecForm4Adapter(settings=ctx.settings, params=dict(ctx.params))
    tsv = ctx.params.get("mode") == "tsv_backfill"
    since = None if tsv else (ctx.params.get("since") or ctx.watermark or ad.load_state().get("watermark"))
    fetched, rows = ad.run(since=since, entities=ctx.params.get("ciks"))
    if not tsv and fetched.status != "failed" and fetched.watermark:
        ad.save_state(watermark=fetched.watermark)
    return JobResult(status=fetched.status, rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), watermark=None if tsv else fetched.watermark,
                     http_requests=ad.http.n_requests, cache_hits=ad.http.n_cache_hits,
                     message=(f"filings={fetched.meta.get('n_filings')} skipped_no_shares={fetched.meta.get('n_rows_skipped_no_shares')}"
                              + (f" txn_date_fixed={fetched.meta.get('n_txn_date_fixed', 0)} "
                                 f"(approx_period_of_report={fetched.meta.get('n_txn_date_approx', 0)}) "
                                 f"txn_date_dropped={fetched.meta.get('n_txn_date_dropped', 0)}"
                                 if fetched.meta.get("n_txn_date_fixed") or fetched.meta.get("n_txn_date_dropped") else "")
                              + (f" identity: {fetched.meta['identity_note']}" if fetched.meta.get("identity_note") else "")))
