"""Identity & entity resolution. Owner: ref-identity.

Public, stable API (other agents code against these):

    lookup(id_type, value, as_of=None) -> dict | None
        Resolve one external identifier through reference/identifier_map. Returns
        {issuer_id, security_id, listing_id, entity_level, id_type, id_value, confidence, source_id,
         valid_from, valid_to, primary_security_id, primary_listing_id}
        or None. Merged issuers (issuer_alias) are followed to the surviving issuer. When several mappings
        match, the one valid at `as_of` (default: today) with the highest confidence wins.
    lookup_many(id_type, values, as_of=None) -> pl.DataFrame
        Vectorized lookup; columns: input_value, id_value, issuer_id, security_id, listing_id, confidence, ...
    resolve_frame(df, value_col, id_type, *, as_of_col=None, min_confidence=0.9, out_cols=("issuer_id",))
        Back-fill internal ids onto a frame keyed by a source-native id (e.g. fundamentals_raw.source_entity_id).
    normalize_id(id_type, value) -> str | None
        Canonical spelling used in identifier_map.id_value (CIK zero-padded to 10, ticker_mic 'SYM@MIC', ...).
    make_issuer_id(seed) / make_security_id(issuer_id, share_class_key) / make_listing_id(security_id, mic)
        Deterministic id hashing (ARCHITECTURE §6). NOTE: issuer ids are assigned ONCE via the persistent
        registry (IssuerRegistry) and never recomputed; make_issuer_id is only used for brand-new issuers.
    source_id_type(source_id) -> id_type of the source-native entity key written by adapters.

`id_type` values: see contracts/schemas/identifier_map.yaml (cik, ticker_mic, yf_ticker, polygon_ticker, isin,
cusip, figi, composite_figi, share_class_figi, lei, edinet_code, jp_sec_code, dart_corp_code, kr_stock_code,
cvm_code, cnpj, cn_code, hk_code, uk_company_number, permid, name_country, issuer_alias, gdelt_org_name).
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

#: identity sources that need an API key (source name in build_reference -> Settings secret): skipped, not a warning
KEYED_SOURCES = {"dart": "opendart_apikey"}

__all__ = [
    "lookup", "lookup_many", "resolve_frame", "normalize_id", "normalize_name", "make_issuer_id",
    "make_security_id", "make_listing_id", "source_id_type", "clear_cache", "ID_TYPES", "SOURCE_ENTITY_ID_TYPE",
]

ID_TYPES: tuple[str, ...] = (
    "cik", "ticker_mic", "yf_ticker", "polygon_ticker", "isin", "cusip", "figi", "composite_figi",
    "share_class_figi", "lei", "edinet_code", "jp_sec_code", "dart_corp_code", "kr_stock_code", "cvm_code",
    "cnpj", "cn_code", "hk_code", "uk_company_number", "permid", "name_country", "issuer_alias", "gdelt_org_name",
)

#: id_type of `source_entity_id` per adapter source_id (fundamentals_raw, filings_index, insider_transactions)
SOURCE_ENTITY_ID_TYPE: dict[str, str] = {
    "sec_edgar": "cik",
    "sec": "cik",
    "esef_xbrl_org": "lei",
    "esef": "lei",
    "gleif": "lei",
    "edinet": "edinet_code",
    "opendart": "dart_corp_code",
    "dart": "dart_corp_code",
    "cvm": "cvm_code",
    "akshare": "cn_code",
    "cn_akshare": "cn_code",
    "companies_house": "uk_company_number",
    "yfinance": "yf_ticker",
    "yfinance_fund": "yf_ticker",
    "yf_fundamentals": "yf_ticker",
    "baostock": "cn_code",
    "fca_nsm": "lei",
    "tdnet": "jp_sec_code",
    "hkex": "hk_code",
    "hkex_filings": "hk_code",
    "polygon": "polygon_ticker",
    "openfigi": "figi",
    # coverage-B EU OAM adapters key rows by issuer LEI (LATAM biva_mx/cmf_cl/smv_pe fill issuer_id at ingest)
    "dila_fr": "lei", "afm_nl": "lei", "fsma_be": "lei", "emarket_it": "lei", "nasdaq_nordic": "lei",
    "oslo_newsweb": "lei", "cnmv_es": "lei",
    # coverage-A: SEC FSDS (CIK10), DART bulk zips (6-digit KRX stock code), Taiwan MOPS (Yahoo ticker 2330.TW)
    "sec_fsds": "cik", "dart_bulk": "kr_stock_code", "mops": "yf_ticker",
}


def source_id_type(source_id: str) -> str | None:
    return SOURCE_ENTITY_ID_TYPE.get(source_id)


#: sources whose source_entity_id is an LEI or, for UK filers without LEI scheme, a Companies House number
_LEI_OR_UKCH = ("esef_xbrl_org", "esef", "fca_nsm", "companies_house")


def entity_id_type_expr(source_col: str = "source_id", value_col: str = "source_entity_id") -> pl.Expr:
    """Per-row id_type of source_entity_id (SOURCE_ENTITY_ID_TYPE, refined by value shape for ESEF/FCA:
    20-char LEI vs 8-char UK company number)."""
    base = pl.col(source_col).replace_strict(dict(SOURCE_ENTITY_ID_TYPE), default=None)
    v = pl.col(value_col).cast(pl.Utf8).str.strip_chars()
    return (pl.when(pl.col(source_col).is_in(list(_LEI_OR_UKCH)) & v.str.contains(r"^[A-Z0-9]{18}[0-9]{2}$")).then(pl.lit("lei"))
            .when(pl.col(source_col).is_in(list(_LEI_OR_UKCH)) & v.str.contains(r"^[A-Z0-9]{2}\d{6}$|^\d{8}$")).then(pl.lit("uk_company_number"))
            .otherwise(base))


# ------------------------------------------------------------------------------------------------ id hashing

_B32 = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", "0123456789ABCDEFGHJKMNPQRSTVWXYZ")  # RFC4648 -> Crockford


def _b32(seed: str, n: int = 12) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").translate(_B32)[:n]


def make_issuer_id(seed: str) -> str:
    """'I' + 12 Crockford-base32 chars of sha1(seed). seed like 'lei:5493...' or 'cik:0000320193'."""
    return "I" + _b32(seed)


def make_security_id(issuer_id: str, share_class_key: str = "common") -> str:
    return "S" + _b32(f"{issuer_id}|{share_class_key}")


def make_listing_id(security_id: str, mic: str) -> str:
    return "L" + _b32(f"{security_id}|{mic.upper()}")


# ------------------------------------------------------------------------------------------------ normalization

_LEGAL_SUFFIXES = (
    "INCORPORATED", "INC", "CORPORATION", "CORP", "COMPANY", "CO", "LIMITED", "LTD", "PLC", "LLC", "LP", "L P",
    "AG", "SE", "SA", "S A", "NV", "N V", "BV", "SPA", "S P A", "AB", "ASA", "AS", "A S", "OYJ", "OY", "GMBH",
    "KGAA", "HOLDINGS", "HOLDING", "GROUP", "THE", "CO LTD", "KK", "K K", "TBK", "BHD", "BERHAD", "PJSC", "OAO",
    "SAB DE CV", "SAB", "DE CV", "S A B DE C V", "NEW", "DEL", "DE", "CL A", "CLASS A", "CL B", "ADR", "ADS",
    "SPON", "SPONSORED", "PLC ADR", "REIT", "TRUST", "SOCIETE ANONYME", "SOCIEDAD ANONIMA", "AKTIENGESELLSCHAFT",
    "COMPANHIA", "CIA", "S/A", "SAS", "NYRT", "PCL", "LTDA",
)
_SUFFIX_RE = re.compile(r"(?:\b(?:" + "|".join(re.escape(s) for s in sorted(_LEGAL_SUFFIXES, key=len, reverse=True)) + r")\b\s*)+$")


def normalize_name(name: str | None, strip_suffixes: bool = True) -> str:
    """Upper-case ASCII name without punctuation and (optionally) trailing legal-form suffixes.
    'Apple Inc.' -> 'APPLE'; 'Toyota Motor Corporation' -> 'TOYOTA MOTOR'; 'Petróleo Brasileiro S.A.' -> 'PETROLEO BRASILEIRO'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii").upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if strip_suffixes:
        full = s
        prev = None
        while prev != s and s:
            prev = s
            s = _SUFFIX_RE.sub("", s).strip()
        s = s or full  # a name made only of legal-form words keeps its full form
    return s


def normalize_id(id_type: str, value: Any) -> str | None:
    """Canonical spelling of an external id for identifier_map.id_value (None if empty/invalid)."""
    if value is None:
        return None
    v = str(value).strip()
    if not v or v.lower() in ("nan", "none", "null"):
        return None
    t = id_type
    if t == "cik":
        d = re.sub(r"\D", "", v)
        return d.zfill(10) if d else None
    if t == "ticker_mic":
        if "@" not in v:
            return v.upper()
        sym, mic = v.rsplit("@", 1)
        return f"{sym.strip().upper()}@{mic.strip().upper()}"
    if t in ("dart_corp_code",):
        d = re.sub(r"\D", "", v)
        return d.zfill(8) if d else None
    if t == "kr_stock_code":
        d = v.upper().split(".")[0]
        return d.zfill(6) if d.isdigit() else d
    if t == "jp_sec_code":
        d = v.upper().split(".")[0]
        if len(d) == 5 and d.endswith("0"):
            d = d[:4]  # EDINET 5-digit securities code '72030' -> '7203'
        return d
    if t == "hk_code":
        d = v.upper().split(".")[0]
        return d.zfill(5) if d.isdigit() else d
    if t == "cvm_code":
        d = re.sub(r"\D", "", v)
        return str(int(d)) if d else None
    if t == "cnpj":
        d = re.sub(r"\D", "", v)
        return d.zfill(14) if d else None
    if t == "cn_code":
        u = v.upper()
        exch = {"SS": "SSE", "SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
        m = re.match(r"^(\d{6})\.(SS|SH|SZ|BJ)$", u)
        if m:
            return f"{exch[m.group(2)]}:{m.group(1)}"
        m = re.match(r"^(SH|SZ|BJ)\.?(\d{6})$", u)
        if m:
            return f"{exch[m.group(1)]}:{m.group(2)}"
        if re.fullmatch(r"\d{6}", u):  # bare code: 6/9 -> SSE, 0/2/3 -> SZSE, 4/8 -> BSE
            return f"{'SSE' if u[0] in '69' else ('BSE' if u[0] in '48' else 'SZSE')}:{u}"
        return u
    if t == "edinet_code":
        return v.upper()
    if t == "name_country":
        if "@" in v:
            n, cc = v.rsplit("@", 1)
            return f"{normalize_name(n)}@{cc.strip().upper()}"
        return normalize_name(v)
    if t == "gdelt_org_name":
        return re.sub(r"\s+", " ", v.lower()).strip()
    if t == "issuer_alias":
        return v.upper()
    if t == "uk_company_number":
        u = v.upper()
        return u.zfill(8) if u.isdigit() else u
    return v.upper()


# ------------------------------------------------------------------------------------------------ cached map

_lock = threading.Lock()
_cache: dict[str, Any] = {"sig": None, "map": None, "alias": None, "primary": None}


def _dir_signature(paths: Iterable[Path]) -> tuple:
    sig = []
    for p in paths:
        if p.exists():
            files = sorted(p.rglob("*.parquet"))
            sig.append((str(p), tuple((f.name, f.stat().st_mtime_ns) for f in files)))
        else:
            sig.append((str(p), ()))
    return tuple(sig)


def clear_cache() -> None:
    with _lock:
        _cache.update({"sig": None, "map": None, "alias": None, "primary": None})


def _load() -> tuple[pl.DataFrame, dict[str, str], pl.DataFrame]:
    from app.core.storage import read_table, table_dir

    s = get_settings()
    sig = (str(s.data_dir), _dir_signature([table_dir("identifier_map", s), table_dir("securities_master", s)]))
    with _lock:
        if _cache["sig"] == sig and _cache["map"] is not None:
            return _cache["map"], _cache["alias"], _cache["primary"]
    idm = read_table("identifier_map")
    alias_rows = idm.filter(pl.col("id_type") == "issuer_alias")
    alias = dict(zip(alias_rows.get_column("id_value").to_list(), alias_rows.get_column("issuer_id").to_list(), strict=True))
    try:
        sm = read_table("securities_master", columns=["issuer_id", "security_id", "primary_listing_id", "is_primary_security"])
        primary = sm.filter(pl.col("is_primary_security")).select(
            "issuer_id", pl.col("security_id").alias("primary_security_id"), pl.col("primary_listing_id")
        ).unique(subset=["issuer_id"], keep="first")
    except Exception:  # pragma: no cover - table missing columns on a fresh install
        primary = pl.DataFrame(schema={"issuer_id": pl.Utf8, "primary_security_id": pl.Utf8, "primary_listing_id": pl.Utf8})
    with _lock:
        _cache.update({"sig": sig, "map": idm, "alias": alias, "primary": primary})
    return idm, alias, primary


def _follow_alias(issuer_id: str | None, alias: dict[str, str]) -> str | None:
    seen = set()
    while issuer_id in alias and issuer_id not in seen:
        seen.add(issuer_id)
        issuer_id = alias[issuer_id]
    return issuer_id


def _to_date(d: date | datetime | str | None) -> date:
    if d is None:
        return date.today()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


# ------------------------------------------------------------------------------------------------ public lookups


def lookup(id_type: str, value: str, as_of: date | None = None) -> dict | None:
    """Resolve one external identifier. See module docstring for the returned dict. None if unknown."""
    if id_type not in ID_TYPES:
        raise ValueError(f"unknown id_type {id_type!r}; expected one of {ID_TYPES}")
    v = normalize_id(id_type, value)
    if v is None:
        return None
    d = _to_date(as_of)
    idm, alias, primary = _load()
    if idm.is_empty():
        return None
    cand = idm.filter(
        (pl.col("id_type") == id_type) & (pl.col("id_value") == v)
        & (pl.col("valid_from") <= d) & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= d))
    )
    if cand.is_empty():
        return None
    level_rank = {"listing": 0, "security": 1, "issuer": 2}
    best = sorted(cand.to_dicts(), key=lambda r: (-(r["confidence"] or 0), level_rank.get(r["entity_level"], 3),
                                                  -(r["valid_from"].toordinal() if r["valid_from"] else 0)))[0]
    issuer_id = _follow_alias(best["issuer_id"], alias)
    out = {
        "issuer_id": issuer_id,
        "security_id": best.get("security_id"),
        "listing_id": best.get("listing_id"),
        "entity_level": best["entity_level"],
        "id_type": id_type,
        "id_value": v,
        "confidence": float(best["confidence"]) if best["confidence"] is not None else None,
        "source_id": best["source_id"],
        "valid_from": best["valid_from"],
        "valid_to": best["valid_to"],
        "primary_security_id": None,
        "primary_listing_id": None,
    }
    if issuer_id != best["issuer_id"]:
        out["merged_from_issuer_id"] = best["issuer_id"]
        out["security_id"] = None if best["entity_level"] == "issuer" else out["security_id"]
    p = primary.filter(pl.col("issuer_id") == issuer_id)
    if p.height:
        out["primary_security_id"] = p.item(0, "primary_security_id")
        out["primary_listing_id"] = p.item(0, "primary_listing_id")
        if out["security_id"] is None:
            out["security_id"] = out["primary_security_id"]
    return out


def lookup_many(id_type: str, values: Sequence[Any] | pl.Series, as_of: date | None = None,
                min_confidence: float = 0.0) -> pl.DataFrame:
    """Vectorized lookup. Returns one row per distinct input value that resolved:
    input_value, id_value, issuer_id, security_id, listing_id, primary_security_id, primary_listing_id,
    entity_level, confidence, source_id. Aliases (merged issuers) are followed."""
    vals = list(values) if not isinstance(values, pl.Series) else values.to_list()
    pairs = [(x, normalize_id(id_type, x)) for x in dict.fromkeys(vals) if x is not None]
    schema = {"input_value": pl.Utf8, "id_value": pl.Utf8, "issuer_id": pl.Utf8, "security_id": pl.Utf8,
              "listing_id": pl.Utf8, "primary_security_id": pl.Utf8, "primary_listing_id": pl.Utf8,
              "entity_level": pl.Utf8, "confidence": pl.Float32, "source_id": pl.Utf8}
    if not pairs:
        return pl.DataFrame(schema=schema)
    inp = pl.DataFrame({"input_value": [str(a) for a, _ in pairs], "id_value": [b for _, b in pairs]},
                       schema={"input_value": pl.Utf8, "id_value": pl.Utf8})
    d = _to_date(as_of)
    idm, alias, primary = _load()
    if idm.is_empty():
        return pl.DataFrame(schema=schema)
    cand = idm.filter(
        (pl.col("id_type") == id_type) & (pl.col("valid_from") <= d)
        & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= d)) & (pl.col("confidence") >= min_confidence)
    ).with_columns(
        pl.col("entity_level").replace_strict({"listing": 0, "security": 1, "issuer": 2}, default=3).alias("__lvl")
    ).sort(["id_value", "confidence", "__lvl", "valid_from"], descending=[False, True, False, True]).unique(
        subset=["id_value"], keep="first", maintain_order=True)
    j = inp.join(cand.select("id_value", "issuer_id", "security_id", "listing_id", "entity_level", "confidence", "source_id"),
                 on="id_value", how="inner")
    if alias:
        j = j.with_columns(pl.col("issuer_id").map_elements(lambda x: _follow_alias(x, alias), return_dtype=pl.Utf8))
    j = j.join(primary, on="issuer_id", how="left").with_columns(
        pl.coalesce("security_id", "primary_security_id").alias("security_id"))
    return j.select(list(schema)).cast(schema)  # type: ignore[arg-type]


def resolve_frame(df: pl.DataFrame, value_col: str, id_type: str | None = None, *, id_type_col: str | None = None,
                  as_of: date | None = None, min_confidence: float = 0.9,
                  out_cols: Sequence[str] = ("issuer_id",)) -> pl.DataFrame:
    """Fill internal ids on a frame keyed by an external id.

    Either `id_type` (constant) or `id_type_col` (per-row; e.g. derived from source_id via SOURCE_ENTITY_ID_TYPE).
    Existing non-null values in out_cols are kept. Mappings below `min_confidence` (fuzzy) are ignored
    (ARCHITECTURE §6.5: fuzzy matches are not used for fundamentals until confirmed).
    """
    if id_type is None and id_type_col is None:
        raise ValueError("pass id_type or id_type_col")
    out = df.with_row_index("__r")
    types = [id_type] if id_type else [t for t in out.get_column(id_type_col).unique().to_list() if t]  # type: ignore[arg-type]
    pieces = []
    for t in types:
        sub = out if id_type else out.filter(pl.col(id_type_col) == t)  # type: ignore[arg-type]
        res = lookup_many(t, sub.get_column(value_col).cast(pl.Utf8), as_of=as_of, min_confidence=min_confidence)
        pieces.append(sub.select("__r", pl.col(value_col).cast(pl.Utf8).alias("input_value")).join(
            res.select(["input_value", *out_cols]), on="input_value", how="inner").drop("input_value"))
    if not pieces:
        return df
    res_all = pl.concat(pieces).unique(subset=["__r"], keep="first")
    res_all = res_all.rename({c: f"__new_{c}" for c in out_cols})
    out = out.join(res_all, on="__r", how="left")
    exprs = []
    for c in out_cols:
        if c in df.columns:
            exprs.append(pl.coalesce(pl.col(c), pl.col(f"__new_{c}")).cast(df.schema[c]).alias(c))
        else:
            exprs.append(pl.col(f"__new_{c}").alias(c))
    out = out.with_columns(exprs).drop([f"__new_{c}" for c in out_cols]).sort("__r").drop("__r")
    return out


# ================================================================================================ BUILDER
# Everything below builds the reference tables (issuers, securities_master, listings, identifier_map) from the
# sources fetched by app.normalize.identity_universe. Entry point: build_reference(). Job: run(ctx) = identity_resolve.

ISSUER_KEY_PREF = ("lei", "cik", "edinet", "dart", "cvm", "cn", "hk", "ukch", "isin")
_UNKNOWN_FROM = date(1900, 1, 1)
UTC_TZ = timezone.utc
US_MICS = ("XNAS", "XNYS", "XASE", "BATS", "ARCX", "XTXS")

COUNTRY_NAME_ISO2 = {
    "UNITED STATES": "US", "USA": "US", "BRAZIL": "BR", "UNITED KINGDOM": "GB", "NETHERLANDS": "NL", "CAYMAN ISLANDS": "KY",
    "BERMUDA": "BM", "IRELAND": "IE", "CANADA": "CA", "ISRAEL": "IL", "CHINA": "CN", "HONG KONG": "HK", "JAPAN": "JP",
    "SWITZERLAND": "CH", "LUXEMBOURG": "LU", "JERSEY": "JE", "GUERNSEY": "GG", "ISLE OF MAN": "IM", "SINGAPORE": "SG",
    "TAIWAN": "TW", "TAIWAN, PROVINCE OF CHINA": "TW", "KOREA, REPUBLIC OF": "KR", "SOUTH KOREA": "KR", "KOREA": "KR",
    "MEXICO": "MX", "ARGENTINA": "AR", "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE", "GERMANY": "DE", "FRANCE": "FR",
    "SPAIN": "ES", "ITALY": "IT", "DENMARK": "DK", "SWEDEN": "SE", "NORWAY": "NO", "FINLAND": "FI", "BELGIUM": "BE",
    "AUSTRALIA": "AU", "INDIA": "IN", "SOUTH AFRICA": "ZA", "GREECE": "GR", "CYPRUS": "CY", "PUERTO RICO": "PR",
    "PANAMA": "PA", "BAHAMAS": "BS", "MARSHALL ISLANDS": "MH", "MONACO": "MC", "BRITISH VIRGIN ISLANDS": "VG",
    "VIRGIN ISLANDS, BRITISH": "VG", "URUGUAY": "UY", "PHILIPPINES": "PH", "INDONESIA": "ID", "THAILAND": "TH",
    "MALAYSIA": "MY", "TURKEY": "TR", "TURKIYE": "TR", "POLAND": "PL", "AUSTRIA": "AT", "PORTUGAL": "PT",
    "NEW ZEALAND": "NZ", "UNITED ARAB EMIRATES": "AE", "KAZAKHSTAN": "KZ", "RUSSIAN FEDERATION": "RU", "RUSSIA": "RU",
    "CURACAO": "CW", "GIBRALTAR": "GI", "MAURITIUS": "MU", "VIETNAM": "VN", "VIET NAM": "VN", "SAUDI ARABIA": "SA",
    "QATAR": "QA", "EGYPT": "EG", "NIGERIA": "NG", "KENYA": "KE", "HUNGARY": "HU", "CZECH REPUBLIC": "CZ", "ICELAND": "IS",
    "ESTONIA": "EE", "LATVIA": "LV", "LITHUANIA": "LT", "MALTA": "MT", "UKRAINE": "UA", "ROMANIA": "RO", "BULGARIA": "BG",
}
DEFAULT_GAAP_BY_COUNTRY = {
    "US": "us_gaap", "KR": "kifrs", "CN": "cas", "JP": None, "IN": "other", "TW": "ifrs", "CA": "ifrs", "BR": "ifrs",
    "MX": "ifrs", "CL": "ifrs", "CO": "ifrs", "PE": "ifrs", "AU": "ifrs", "SG": "ifrs", "HK": "ifrs",
}
EU_UK = {"GB", "JE", "GG", "IM", "IE", "FR", "DE", "NL", "BE", "LU", "IT", "ES", "PT", "CH", "SE", "DK", "FI", "NO", "AT",
         "PL", "GR", "CZ", "HU", "IS"}
DEFAULT_CCY_BY_COUNTRY = {
    "US": "USD", "GB": "GBP", "JE": "GBP", "CH": "CHF", "SE": "SEK", "DK": "DKK", "NO": "NOK", "JP": "JPY", "KR": "KRW",
    "CN": "CNY", "HK": "HKD", "BR": "BRL", "MX": "MXN", "CL": "CLP", "CO": "COP", "PE": "USD", "CA": "CAD", "AU": "AUD",
    "IN": "INR", "TW": "TWD", "SG": "SGD", "PL": "PLN",
}
EUR_COUNTRIES = {"FR", "DE", "NL", "BE", "LU", "IT", "ES", "PT", "IE", "FI", "AT", "GR"}


def country_iso2(name_or_code: str | None) -> str | None:
    if not name_or_code:
        return None
    v = str(name_or_code).strip().upper()
    if len(v) == 2 and v.isalpha():
        return v
    if v.startswith("US-"):
        return "US"
    return COUNTRY_NAME_ISO2.get(v)


def _registry_paths(settings=None) -> dict[str, Path]:
    from app.normalize.identity_universe import registry_dir

    d = registry_dir(settings)
    return {"keys": d / "issuer_registry.parquet", "ids": d / "issuer_ids.parquet", "sec": d / "security_registry.parquet"}


class Registry:
    """Persistent, append-only id registry (ids are assigned once and never recomputed).

    issuer keys  : 'cik:0000320193' / 'lei:...' / 'seed:7203@XTKS' / 'grp:petrobras' ... -> issuer_id
    issuer ids   : issuer_id, seed, created_at, merged_into
    security keys: 'tm:AAPL@XNAS' / 'isin:...' / 'scfigi:...' -> (security_id, issuer_id)
    """

    def __init__(self, settings=None):
        self.settings = settings
        p = _registry_paths(settings)
        self.paths = p
        self.keys: dict[str, str] = {}
        self.ids: dict[str, dict[str, Any]] = {}
        self.sec_keys: dict[str, tuple[str, str]] = {}
        if p["keys"].exists():
            k = pl.read_parquet(p["keys"])
            self.keys = dict(zip(k.get_column("key").to_list(), k.get_column("issuer_id").to_list(), strict=True))
        if p["ids"].exists():
            for r in pl.read_parquet(p["ids"]).iter_rows(named=True):
                self.ids[r["issuer_id"]] = r
        if p["sec"].exists():
            for r in pl.read_parquet(p["sec"]).iter_rows(named=True):
                self.sec_keys[r["key"]] = (r["security_id"], r["issuer_id"])
        self._taken_sids = {v[0] for v in self.sec_keys.values()}
        self.new_issuers: list[str] = []
        self.merges: list[tuple[str, str]] = []  # (merged, survivor)

    def survivor(self, issuer_id: str) -> str:
        seen = set()
        while issuer_id in self.ids and self.ids[issuer_id].get("merged_into") and issuer_id not in seen:
            seen.add(issuer_id)
            issuer_id = self.ids[issuer_id]["merged_into"]
        return issuer_id

    def resolve(self, keys: Sequence[str], seed: str, today: date) -> str:
        """Issuer id for a cluster of keys: existing (duplicates merged, oldest survives) or newly assigned."""
        found = {self.survivor(self.keys[k]) for k in keys if k in self.keys}
        if len(found) > 1:
            ordered = sorted(found, key=lambda i: (self.ids.get(i, {}).get("created_at") or today, i))
            surv = ordered[0]
            for m in ordered[1:]:
                self.ids.setdefault(m, {"issuer_id": m, "seed": None, "created_at": today})["merged_into"] = surv
                self.merges.append((m, surv))
                log.info("identity: merged issuer %s into %s", m, surv)
            iid = surv
        elif found:
            iid = found.pop()
        else:
            iid = make_issuer_id(seed)
            n = 2
            while iid in self.ids:
                iid = make_issuer_id(f"{seed}#{n}")
                n += 1
            self.ids[iid] = {"issuer_id": iid, "seed": seed, "created_at": today, "merged_into": None}
            self.new_issuers.append(iid)
        for k in keys:
            if k not in self.keys or self.survivor(self.keys[k]) != iid:
                self.keys[k] = iid
        return iid

    def security(self, keys: Sequence[str], issuer_id: str, share_class_key: str) -> str:
        sid = None
        for k in keys:
            hit = self.sec_keys.get(k)
            if hit and self.survivor(hit[1]) == issuer_id:
                sid = hit[0]
                break
        if sid is None:
            sid = make_security_id(issuer_id, share_class_key)
            n = 2
            while sid in self._taken_sids:
                sid = make_security_id(issuer_id, f"{share_class_key}#{n}")
                n += 1
            self._taken_sids.add(sid)
        for k in keys:
            self.sec_keys[k] = (sid, issuer_id)
        return sid

    def save(self, today: date) -> None:
        p = self.paths
        k = pl.DataFrame({"key": list(self.keys), "issuer_id": list(self.keys.values())},
                         schema={"key": pl.Utf8, "issuer_id": pl.Utf8})
        ids = pl.DataFrame([{"issuer_id": v["issuer_id"], "seed": v.get("seed"), "created_at": v.get("created_at") or today,
                             "merged_into": v.get("merged_into")} for v in self.ids.values()],
                           schema={"issuer_id": pl.Utf8, "seed": pl.Utf8, "created_at": pl.Date, "merged_into": pl.Utf8})
        sec = pl.DataFrame([{"key": a, "security_id": b[0], "issuer_id": b[1]} for a, b in self.sec_keys.items()],
                           schema={"key": pl.Utf8, "security_id": pl.Utf8, "issuer_id": pl.Utf8})
        for df, path in ((k, p["keys"]), (ids, p["ids"]), (sec, p["sec"])):
            tmp = path.with_suffix(".tmp")
            df.write_parquet(tmp)
            tmp.replace(path)


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def record_keys(r: dict[str, Any]) -> list[str]:
    """Issuer-level union / registry keys of a listing record."""
    ks = []
    for fld, pre in (("lei", "lei"), ("cik", "cik"), ("edinet_code", "edinet"), ("dart_corp_code", "dart"),
                     ("cvm_code", "cvm"), ("cnpj", "cnpj"), ("cn_code", "cn"), ("hk_code", "hk"),
                     ("uk_company_number", "ukch"), ("isin", "isin"), ("issuer_group", "grp")):
        v = r.get(fld)
        if v:
            ks.append(f"{pre}:{v}")
    if r.get("origin") == "seed":
        ks.append(f"seed:{r['local_symbol']}@{r['mic']}")
    elif r.get("mic") and r.get("local_symbol") and not r.get("cik"):
        ks.append(f"tm:{r['local_symbol']}@{r['mic']}")
    return ks


def fuzzy_match(names: pl.DataFrame, candidates: pl.DataFrame, threshold: float = 92.0) -> pl.DataFrame:
    """Fuzzy name match (rapidfuzz token_set_ratio on normalize_name), restricted to the same country.

    names: (key, name, country); candidates: (issuer_id, name, country). Returns key, issuer_id, score, confidence
    (0.85 per ARCHITECTURE §6.5: not used for fundamentals until confirmed in seeds/manual_overrides.csv).
    Ambiguous matches (two distinct issuers within 1 point of the best score) are dropped."""
    from rapidfuzz import fuzz, process

    out = []
    cand_by_cc: dict[str, tuple[list[str], list[str]]] = {}
    for r in candidates.iter_rows(named=True):
        cc = (r.get("country") or "").upper()
        lst = cand_by_cc.setdefault(cc, ([], []))
        lst[0].append(normalize_name(r["name"]))
        lst[1].append(r["issuer_id"])
    for r in names.iter_rows(named=True):
        cc = (r.get("country") or "").upper()
        if cc not in cand_by_cc:
            continue
        choices, ids = cand_by_cc[cc]
        q = normalize_name(r["name"])
        if not q:
            continue
        top = process.extract(q, choices, scorer=fuzz.token_set_ratio, limit=5, score_cutoff=threshold)
        if not top:
            continue
        best = top[0]
        distinct = {ids[t[2]] for t in top if t[1] >= best[1] - 1}
        if len(distinct) == 1:
            out.append({"key": r["key"], "issuer_id": ids[best[2]], "score": float(best[1]), "confidence": 0.85})
    return pl.DataFrame(out, schema={"key": pl.Utf8, "issuer_id": pl.Utf8, "score": pl.Float64, "confidence": pl.Float64})


# ------------------------------------------------------------------------------------------------ record assembly

_POLY_TYPES = {"CS": "common", "ADRC": "adr", "ADRP": "adr", "GDR": "gdr", "NYRS": "common"}


def _us_records(sec: pl.DataFrame, poly: pl.DataFrame, grouped: pl.DataFrame) -> list[dict[str, Any]]:
    """One record per US-listed common / ADR instrument (Massive types), joined to SEC CIKs and last close/volume.
    Without Massive data, SEC tickers on NYSE/Nasdaq with plain common-looking symbols are used."""
    sec_by_tk: dict[str, dict[str, Any]] = {}
    if sec.height:
        for r in sec.iter_rows(named=True):
            sec_by_tk.setdefault(r["ticker"].replace("-", "."), r)
    px = {r["ticker"]: r for r in grouped.iter_rows(named=True)} if grouped.height else {}
    recs: list[dict[str, Any]] = []

    def mk(tk: str, mic: str, name: str | None, typ: str, ptype: str | None, cik: str | None, extra: dict[str, Any]) -> dict[str, Any]:
        p = px.get(tk) or {}
        s = sec_by_tk.get(tk) or {}
        return {"origin": "us", "name": s.get("name") or name, "mic": mic, "local_symbol": tk,
                "yf_ticker": tk.replace(".", "-"), "polygon_ticker": tk, "sec_type": typ, "poly_type": ptype, "cik": cik,
                "close": p.get("close"), "volume": p.get("volume"),
                "dollar_volume": float(p.get("close") or 0) * float(p.get("volume") or 0), "sec_rank": s.get("sec_rank"),
                "quote_ccy": "USD", "region": "US", "country": None, "exchange_country": "US", **extra}

    if poly.height:
        # coverage v2 W2 (2026-10-31): Massive leaves primary_exchange empty for the stocks the SEC lists on the new
        # Texas Stock Exchange (ET, DDS, SUN, TCBI ...): they were dropped from the universe; the SEC exchange MIC
        # (company_tickers_exchange) is used instead
        poly = poly.with_columns(pl.col("primary_exchange").fill_null(
            pl.col("ticker").str.replace_all("-", ".").replace_strict(
                {t: (s.get("mic") or "") for t, s in sec_by_tk.items()}, default="")).replace("", None))
        for r in poly.filter(pl.col("type").is_in(list(_POLY_TYPES)) & pl.col("primary_exchange").is_in(list(US_MICS))).iter_rows(named=True):
            tk = r["ticker"]
            s = sec_by_tk.get(tk) or {}
            cik = s.get("cik") or (str(r["cik"]).zfill(10) if r.get("cik") else None)
            recs.append(mk(tk, r["primary_exchange"], r.get("name"), _POLY_TYPES[r["type"]], r["type"], cik,
                           {"composite_figi": r.get("composite_figi"), "share_class_figi": r.get("share_class_figi")}))
    else:
        for tk, s in sec_by_tk.items():
            if s.get("mic") not in ("XNAS", "XNYS", "BATS", "XTXS") or not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", tk):
                continue
            if len(tk) == 5 and tk[-1] in "WURZ":
                continue
            recs.append(mk(tk, s["mic"], s["name"], "common", None, s["cik"], {}))
    return recs


def _seed_records(seeds: pl.DataFrame) -> list[dict[str, Any]]:
    from app.normalize.identity_figi import mic_info

    recs = []
    for i, r in enumerate(seeds.iter_rows(named=True)):
        mic = r["mic"].upper()
        m = mic_info(mic)
        sym = r["ticker"].strip().upper()
        rec: dict[str, Any] = {
            "origin": "seed", "seed_order": i, "name": r["name"], "country": (r.get("country") or m["country"]).upper(),
            "region": r.get("region") or m["region"], "mic": mic, "local_symbol": sym,
            "yf_ticker": r.get("yf_ticker"), "isin": r.get("isin"), "adr_ticker": r.get("adr_ticker"),
            "issuer_group": r.get("issuer_group"), "share_class": r.get("share_class"), "quote_ccy": m["ccy"] or None,
            "sec_type": "preferred" if (r.get("share_class") or "").lower() in ("pref", "pn") else "common",
            "exchange_country": m["country"], "notes": r.get("notes"),
            "lei": (r.get("lei") or None), "lei_src": "seed_gleif_isin" if r.get("lei") else None,
            "fye_mmdd": r.get("fye_mmdd") or None,
        }
        if mic == "XTKS":
            rec["jp_sec_code"] = sym
        elif mic in ("XKRX", "XKOS"):
            rec["kr_stock_code"] = sym.zfill(6)
        elif mic in ("XSHG", "XSHE"):
            rec["cn_code"] = ("SSE:" if mic == "XSHG" else "SZSE:") + sym.zfill(6)
        elif mic == "XHKG":
            rec["hk_code"] = sym.zfill(5)
        if mic in US_MICS:
            rec["local_symbol"] = sym.replace("-", ".")
            rec["polygon_ticker"] = rec["local_symbol"]
        recs.append(rec)
    return recs


def eu_broad_records(sd: list[dict[str, Any]], eu_file: pl.DataFrame | None,
                     eu_src: pl.DataFrame | None = None) -> list[dict[str, Any]]:
    """Seed records for the generated eu_broad seeds (app.ingestion.eu_expansion). Rows matching a curated seed (same
    ISIN, LEI, yf ticker or ticker@MIC) are not duplicated: the curated record is flagged `eu_broad` instead, so
    mvp_global stays unchanged and no issuer is created twice."""
    eu = eu_src if eu_src is not None else eu_file
    if eu is None or eu.is_empty():
        return []
    idx: dict[str, dict[str, Any]] = {}
    for r in sd:
        for k in (r.get("isin"), r.get("lei"), r.get("yf_ticker"), f"{r['local_symbol']}@{r['mic']}"):
            if k:
                idx.setdefault(k, r)
    new = _seed_records(eu.select([c for c in eu.columns if c != "lei_source"]))
    lei_src = eu.get_column("lei_source").to_list() if "lei_source" in eu.columns else [None] * eu.height
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r, ls in zip(new, lei_src, strict=True):
        keys = [k for k in (r.get("isin"), r.get("lei"), r.get("yf_ticker"), f"{r['local_symbol']}@{r['mic']}") if k]
        hit = next((idx[k] for k in keys if k in idx), None)
        if hit is not None:
            hit["eu_broad"] = True
            continue
        if any(k in seen for k in keys):
            continue
        seen.update(keys)
        r["seed_order"] = len(sd) + len(out)
        r["eu_broad"] = r["eu_broad_only"] = True
        r["lei_src"] = ("seed_" + ls) if (r.get("lei") and ls) else r.get("lei_src")
        out.append(r)
    return out


def coverage_records(sd: list[dict[str, Any]], cov: pl.DataFrame | None) -> list[dict[str, Any]]:
    """Seed records for the generated price-coverage seeds (seeds/price_coverage.csv, app.ingestion.price_coverage_ext).
    Same de-duplication as eu_broad_records (ISIN / yf ticker / ticker@MIC against curated + eu_broad seeds): a match
    only flags the existing record `coverage`; new rows are `coverage_only` (members of `full`, never mvp_*/eu_broad).
    Coverage records never trigger yfinance .info or GLEIF lookups in build_reference (Yahoo is throttled)."""
    if cov is None or cov.is_empty():
        return []
    idx: dict[str, dict[str, Any]] = {}
    for r in sd:
        for k in (r.get("isin"), r.get("yf_ticker"), f"{r['local_symbol']}@{r['mic']}"):
            if k:
                idx.setdefault(k, r)
    extra_cols = [c for c in ("native_industry", "coverage_source") if c in cov.columns]
    new = _seed_records(cov.drop(extra_cols))
    extras = cov.select(extra_cols).to_dicts() if extra_cols else [{}] * cov.height
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r, ex in zip(new, extras, strict=True):
        keys = [k for k in (r.get("isin"), r.get("yf_ticker"), f"{r['local_symbol']}@{r['mic']}") if k]
        hit = next((idx[k] for k in keys if k in idx), None)
        if hit is not None:
            hit["coverage"] = True
            continue
        if any(k in seen for k in keys):
            continue
        seen.update(keys)
        r["seed_order"] = len(sd) + len(out)
        r["coverage"] = r["coverage_only"] = True
        r["coverage_source"] = ex.get("coverage_source")
        ind = ex.get("native_industry")
        if ind:
            if r["mic"] in ("XSHG", "XSHE"):
                r["csrc_code"] = ind
            elif r["mic"] in ("XTAI", "ROCO"):
                r["tw_industry_code"] = ind
            elif r["mic"] == "XTKS":
                r["tse33_code"] = ind
            elif r["mic"] in ("XKRX", "XKOS"):
                r["kr_industry_name"] = ind
        out.append(r)
    return out


_BENCH_REGION_COUNTRY = {"US": "US", "CA": "CA", "UK": "GB", "JP": "JP", "KR": "KR", "HK": "HK", "CN": "CN"}
_BENCH_TYPE = {"etf": "etf", "index": "index", "index_ext": "index", "future": "future", "fx": "fx"}


def benchmark_reference_rows(now: datetime, run_id: str | None = None) -> tuple[list[dict[str, Any]], ...]:
    """Non-equity instruments of app.normalize.prices.BENCHMARKS (indices, ETFs, continuous futures) as reference rows:
    security_id = the synthetic price id (IDX:^GSPC / ETF:SPY / IDX:CL=F), security_type index | etf | future, one
    synthetic issuer per instrument (issuer_id = security_id; issuers.issuer_id is non-nullable), one primary listing
    (mic = trading venue / calendar MIC; XCME for CME-group futures; ARCX for US ETFs). Rebuilt on every ref_universe
    run, never members of an equity universe (build_universe only ranks common stock / seeds)."""
    from app.normalize.identity_figi import mic_info
    from app.normalize.prices import BENCHMARKS

    iss, secs, lsts, idm = [], [], [], []
    from app.ingestion.price_history_lists import CCY_COUNTRY

    for b in BENCHMARKS:
        typ = _BENCH_TYPE.get(b.kind, "other")
        # FX pairs (price-history): OTC market -> MIC XOFF, exchange_country = quote-currency country, London tz;
        # prices_daily puts them in region FX (app.normalize.prices.load_listings)
        mic = ("XOFF" if typ == "fx" else "XCME" if typ == "future"
               else ("ARCX" if typ == "etf" and b.region == "US" else (b.calendar or "XXXX")))
        m = mic_info(b.calendar or mic) if typ != "fx" else {"country": CCY_COUNTRY.get(b.currency, "US"),
                                                              "tz": "Europe/London"}
        country = m["country"] or {"XIDX": "ID", "XKLS": "MY", "XBKK": "TH"}.get(b.calendar or "")             or _BENCH_REGION_COUNTRY.get(b.region) or "US"
        region = b.region if b.region in ("US", "CA", "LATAM", "UK", "EU", "JP", "KR", "CN", "HK", "APAC") else "OTHER"
        sid = b.security_id
        lid = f"{sid}@{mic}"
        iss.append({"issuer_id": sid, "name": b.name, "short_name": b.name[:40], "country": country, "region": region,
                    "is_active": True, "updated_at": now, "run_id": run_id,
                    "description": f"benchmark {typ} (synthetic issuer; not an operating company)"})
        secs.append({"security_id": sid, "issuer_id": sid, "security_type": typ, "primary_listing_id": lid,
                     "display_ticker": b.yf_ticker, "name": b.name, "trading_currency": b.currency,
                     "is_primary_security": True, "is_active": True, "updated_at": now, "run_id": run_id})
        lsts.append({"listing_id": lid, "security_id": sid, "mic": mic, "exchange_country": country,
                     "local_symbol": b.yf_ticker, "yf_ticker": b.yf_ticker,
                     "polygon_ticker": b.yf_ticker if typ == "etf" and b.region == "US" else None,
                     "quote_currency": b.currency, "price_divisor": 1.0, "is_primary": True, "lot_size": None,
                     "timezone": m["tz"] if m["tz"] != "UTC" else {
                         "XIDX": "Asia/Jakarta", "XKLS": "Asia/Kuala_Lumpur", "XBKK": "Asia/Bangkok",
                         "XCME": "America/Chicago"}.get(b.calendar or mic, "America/New_York"), "valid_from": _UNKNOWN_FROM,
                     "valid_to": None, "updated_at": now})
        for t in (b.yf_ticker, *b.alternates):
            idm.append(_idm("yf_ticker", t, "listing", sid, sid, lid, "ref_universe", 1.0, now))
    return iss, secs, lsts, idm


def _index(df: pl.DataFrame | None, key: str) -> dict[str, dict[str, Any]]:
    if df is None or df.is_empty() or key not in df.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in df.iter_rows(named=True):
        k = r.get(key)
        if k is not None and k not in out:
            out[k] = r
    return out


def _enrich_seeds(recs: list[dict[str, Any]], src: dict[str, pl.DataFrame], warnings: list[str]) -> None:
    """Fill national ids / ISIN / LEI / local names / native industry codes on seed records from source lists."""
    import calendar

    from app.normalize.identity_universe import is_valid_isin, kr_common_isin

    jpx = _index(src.get("jpx"), "jp_sec_code")
    edinet: dict[str, dict[str, Any]] = {}
    e = src.get("edinet")
    if e is not None and e.height:
        e = e.filter(pl.col("jp_sec_code").is_not_null())
        edinet = _index(e.filter(pl.col("listed") == "上場"), "jp_sec_code") or _index(e, "jp_sec_code")
    dart = _index(src.get("dart"), "kr_stock_code")
    kind = _index(src.get("kind"), "kr_stock_code")
    sse = _index(src.get("sse"), "cn_code")
    szse = _index(src.get("szse"), "cn_code")
    hkex = _index(src.get("hkex"), "hk_code")
    cvm_tk = _index(src.get("cvm_tickers"), "ticker")
    cvm_cad: dict[str, dict[str, Any]] = {}
    cad = src.get("cvm_cad")
    if cad is not None and cad.height:
        cvm_cad = _index(cad.with_columns((pl.col("situation") == "ATIVO").alias("_act")).sort("_act", descending=True), "cnpj")
    b3 = _index(src.get("b3"), "ticker")
    for r in recs:
        if r["origin"] != "seed":
            continue
        mic = r["mic"]
        if mic == "XTKS":
            j = jpx.get(r["jp_sec_code"])
            if j:
                r["name_local"] = j.get("name_ja")
                r["tse33_code"] = j.get("tse33_code")
            r["lot_size"] = 100
            ed = edinet.get(r["jp_sec_code"])
            if ed:
                r["edinet_code"] = ed["edinet_code"]
                r["name_en_reg"] = ed.get("name_en")
                r["name_local"] = r.get("name_local") or ed.get("name_ja")
                m = re.match(r"(\d+)月(\d+)日", ed.get("fye") or "")
                if m:
                    r["fye_mmdd"] = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        elif mic in ("XKRX", "XKOS"):
            sc = r["kr_stock_code"]
            d = dart.get(sc)
            if d:
                r["dart_corp_code"] = d["dart_corp_code"]
                r["name_local"] = d.get("corp_name")
            k = kind.get(sc)
            if k:
                r["name_local"] = r.get("name_local") or k.get("name_ko")
                r["kr_industry_name"] = k.get("industry_ko")
                fm = re.sub(r"\D", "", str(k.get("fiscal_month") or ""))
                if fm and 1 <= int(fm) <= 12:
                    mm = int(fm)
                    r["fye_mmdd"] = f"{mm:02d}-{calendar.monthrange(2023, mm)[1]:02d}"
            if not r.get("isin"):
                r["isin"] = kr_common_isin(sc)
            r["lot_size"] = 1
        elif mic in ("XSHG", "XSHE"):
            c = (sse if mic == "XSHG" else szse).get(r["cn_code"])
            if c:
                r["name_local"] = c.get("name_cn")
                r["csrc_code"] = c.get("csrc_code")
            r["lot_size"] = 100
        elif mic == "XHKG":
            h = hkex.get(r["hk_code"])
            if h:
                r["lot_size"] = h.get("board_lot")
                if not r.get("isin") and is_valid_isin(h.get("isin")):
                    r["isin"] = h["isin"]
        elif mic == "BVMF":
            t = cvm_tk.get(r["local_symbol"])
            if t:
                r["cnpj"] = t["cnpj"]
                c = cvm_cad.get(t["cnpj"])
                if c:
                    r["cvm_code"] = c["cvm_code"]
                    r["cvm_sector"] = c.get("sector")
                    r["name_local"] = c.get("name")
            b = b3.get(r["local_symbol"])
            if b and not r.get("isin") and is_valid_isin(b.get("isin")):
                r["isin"] = b["isin"]
            r["lot_size"] = 100
    _firds_match(recs, src.get("firds"))


_FIRDS_NOISE = re.compile(r"\b(SHRS?|SHS|ORD|ORDINARY|BEARER|REG|REGISTERED|NPV|NAMENS|AKTIEN|INHABER|VORZUG|STAMM|VTG|"
                          r"FPD|NON|VOTING|PREF|DR|REST|EO|SF|DK|SK|NK|ON|PN|CL|CLASS|SER|ADR|GDR|BR|ACTIONS?|AZ|PORT|NOM)\b")


def _firds_match(recs: list[dict[str, Any]], firds: pl.DataFrame | None) -> None:
    """Fill ISIN + issuer LEI on EU/UK/CH seed records by fuzzy name match against ESMA FIRDS shares (CFI ES*)."""
    if firds is None or firds.is_empty():
        return
    from rapidfuzz import fuzz, process

    f = firds.filter(pl.col("cfi").str.starts_with("ES") & pl.col("issuer_lei").is_not_null())
    rows = f.to_dicts()
    names = [re.sub(r"\s+", " ", _FIRDS_NOISE.sub(" ", normalize_name(r["full_name"] or r["short_name"] or ""))).strip()
             for r in rows]
    by_prefix: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        by_prefix.setdefault(r["isin"][:2], []).append(i)
    n_ok = 0
    for r in recs:
        if r["origin"] != "seed" or (r.get("isin") and r.get("lei")):
            continue
        if r["country"] not in EU_UK and r["region"] not in ("EU", "UK"):
            continue
        prefixes = {r["country"], "NL", "LU", "IE", "JE", "GB", "CH", "BE", "DE", "FR", "GG"}
        cand = {i: names[i] for p in prefixes for i in by_prefix.get(p, [])}
        if not cand:
            continue
        q = normalize_name(r["name"])
        hits = process.extract(q, cand, scorer=fuzz.token_set_ratio, limit=25, score_cutoff=88)
        if not hits:
            continue
        pref = (r.get("share_class") or "").lower() in ("pref", "genussschein")
        top_score = max(h[1] for h in hits)

        def rank(h: tuple) -> tuple:
            row = rows[h[2]]
            voting = (row["cfi"] or "")[2:3] == "V"
            return (h[1] >= top_score - 2, row["isin"][:2] == r["country"], (not voting) if pref else voting,
                    (row.get("venue_mic") or "") == r["mic"], h[1])

        best = rows[sorted(hits, key=rank, reverse=True)[0][2]]
        top_leis = {rows[h[2]]["issuer_lei"] for h in hits if h[1] >= top_score - 1}
        if len(top_leis) > 1:
            continue  # ambiguous issuer
        if not r.get("isin"):
            r["isin"] = best["isin"]
        r["lei"] = r.get("lei") or best["issuer_lei"]
        r["firds_score"] = top_score
        n_ok += 1
    log.info("FIRDS name match: %d EU/UK seeds matched", n_ok)


# ------------------------------------------------------------------------------------------------ helpers


def _idm(id_type: str, value: str, level: str, iid: str, sid: str | None, lid: str | None, source: str,
         conf: float, now: datetime) -> dict[str, Any]:
    return {"id_type": id_type, "id_value": normalize_id(id_type, value), "entity_level": level, "issuer_id": iid,
            "security_id": sid, "listing_id": lid, "valid_from": _UNKNOWN_FROM, "valid_to": None, "source_id": source,
            "confidence": conf, "updated_at": now}


_IDM_SCHEMA = {"id_type": pl.Utf8, "id_value": pl.Utf8, "entity_level": pl.Utf8, "issuer_id": pl.Utf8,
               "security_id": pl.Utf8, "listing_id": pl.Utf8, "valid_from": pl.Date, "valid_to": pl.Date,
               "source_id": pl.Utf8, "confidence": pl.Float32, "updated_at": pl.Datetime("us", "UTC")}


def _region_of_country(cc: str, default: str) -> str:
    from app.normalize.identity_figi import MICS

    for m in MICS.values():
        if m["country"] == cc:
            return m["region"]
    return default


def _pretty_name(n: str | None) -> str | None:
    """'BANK OF AMERICA CORP /DE/' -> 'Bank of America Corp'. Mixed-case names are kept."""
    if not n:
        return None
    n = re.sub(r"\s*/[A-Z]{2,3}/?\s*$", "", n.strip())
    if n.isupper():
        small = {"OF", "AND", "THE", "DE", "LA", "DEL", "DO", "DA", "FOR"}
        keep_upper = {"LLC", "PLC", "AG", "SA", "NV", "SE", "LP", "ASA", "AB", "REIT", "ETF", "USA", "US", "II", "III",
                      "IBM", "AT&T", "NRG", "AES", "CSX", "CVS", "HP", "PPG", "PPL", "RTX", "UPS", "WEC", "CF", "KLA"}
        words = []
        for i, w in enumerate(n.split()):
            core = w.strip(".,")
            if core in keep_upper:
                words.append(w)
            elif core in small and i:
                words.append(w.lower())
            else:
                words.append(w.capitalize())
        n = " ".join(words)
    return n


def _short_name(name: str) -> str | None:
    n = normalize_name(name)
    return n.title() if n else None


def _mcap_usd(yfp: dict[str, Any], fx: dict[str, float]) -> float | None:
    mc = yfp.get("marketCap")
    if not mc:
        return None
    ccy = yfp.get("currency") or "USD"
    ccy = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(ccy, ccy).upper()
    rate = fx.get(ccy)
    return float(mc) * rate if rate else None


# ------------------------------------------------------------------------------------------------ build


def build_reference(as_of: date | None = None, run_id: str | None = None, settings=None,
                    write_universe: bool | None = None, us_size: int = 500, max_submissions: int | None = None,
                    fetch_yf: bool = True, cancel=None, sources: dict[str, pl.DataFrame] | None = None,
                    write: bool = True, keep_universes: Sequence[str] = ()) -> dict[str, Any]:
    """Build issuers / securities_master / listings / identifier_map (+ universe_membership) end to end.

    `sources` (tests / offline): pre-fetched frames keyed sec_tickers, polygon_tickers, grouped, sec_shares, submissions,
    yf_info, jpx, edinet, dart, kind, sse, szse, hkex, cvm_cad, cvm_tickers, b3, firds, gleif, fx. When `sources` is
    given, missing keys are treated as empty (no network). Otherwise everything is fetched live.
    Returns {"rows": {table: n}, "issuers": n, "warnings": [...], "stats": {...}, "frames": {...}}.
    """
    from app.core.config import get_settings as _gs
    from app.normalize import identity_universe as U
    from app.normalize import taxonomy
    from app.normalize.identity_figi import mic_info

    s = settings or _gs()
    today = as_of or date.today()
    warnings: list[str] = []
    skipped: list[str] = []
    offline = sources is not None
    src: dict[str, pl.DataFrame] = dict(sources or {})
    tick = cancel or (lambda: None)

    def get(name: str, fn, *a, **kw) -> pl.DataFrame:
        if name not in src:
            src[name] = pl.DataFrame() if offline else (fn(*a, **kw))
            if src[name] is None:
                src[name] = pl.DataFrame()
            if not offline and src[name].is_empty():
                # F17: a keyed source whose API key is not configured is skipped by design (info, not a warning):
                # it made ref_universe 'partial' on every run although nothing was wrong
                key = KEYED_SOURCES.get(name)
                if key and not s.secret(key):
                    skipped.append(f"source {name} skipped (missing {key.upper()})")
                else:
                    warnings.append(f"source {name} empty")
        return src[name]

    seeds = U.load_global_seeds(s)
    overrides = U.load_overrides(s)
    sec = get("sec_tickers", U.fetch_sec_tickers, s)
    poly = get("polygon_tickers", U.fetch_polygon_tickers, s)
    grouped = get("grouped", U.fetch_polygon_grouped, None, s)
    shares = get("sec_shares", U.fetch_sec_shares, s)
    tick()
    for name, fn in (("jpx", U.fetch_jpx_list), ("edinet", U.fetch_edinet_codelist), ("dart", U.fetch_dart_corpcodes),
                     ("kind", U.fetch_kind_list), ("sse", U.fetch_sse_list), ("szse", U.fetch_szse_list),
                     ("hkex", U.fetch_hkex_list), ("cvm_cad", U.fetch_cvm_cadastro), ("cvm_tickers", U.fetch_cvm_tickers),
                     ("b3", U.fetch_b3_isins), ("firds", U.fetch_firds_equities)):
        get(name, fn, s)
        tick()

    us = _us_records(sec, poly, grouped)
    sd = _seed_records(seeds)
    _enrich_seeds(sd, src, warnings)
    need_isins = [r["isin"] for r in sd if r.get("isin") and not r.get("lei") and r["mic"] not in US_MICS]
    if "gleif_isin" in src:
        g_map = dict(zip(src["gleif_isin"].get_column("isin").to_list(), src["gleif_isin"].get_column("lei").to_list(), strict=True)) \
            if src["gleif_isin"].height else {}
    else:
        g_map = {} if offline else U.gleif_lei_by_isin(need_isins + [r["isin"] for r in sd if r.get("isin") and r.get("lei_src") is None
                                                                    and r.get("lei")], settings=s)
    for r in sd:
        lei_g = g_map.get(r.get("isin") or "")
        if lei_g and (not r.get("lei") or r.get("lei_src") is None):
            # exact ISIN match beats the FIRDS fuzzy-name LEI
            r["lei"], r["lei_src"] = lei_g, "gleif_isin"
    # strict GLEIF name fallback for non-US seeds still without LEI (one query per issuer group / seed)
    if not offline and "gleif_name" not in src:
        q, seen_keys = [], set()
        for r in sd:
            if r.get("lei") or r["mic"] in US_MICS:
                continue
            k = r.get("issuer_group") or f"{r['local_symbol']}@{r['mic']}"
            if k in seen_keys or any(x.get("lei") for x in sd if x.get("issuer_group") and x.get("issuer_group") == r.get("issuer_group")):
                continue
            seen_keys.add(k)
            base = re.sub(r"\s*\((A|H|PN|ON|Pref)\)\s*$", "", r["name"])
            q.append((k, r["country"], [r.get("name_local"), r.get("name_en_reg"), base]))
        n_map = U.gleif_lei_by_name(q, settings=s)
        for r in sd:
            k = r.get("issuer_group") or f"{r['local_symbol']}@{r['mic']}"
            if not r.get("lei") and k in n_map:
                r["lei"], r["lei_src"] = n_map[k], "gleif_name"

    # generated eu_broad seeds (matched against the enriched curated seeds: ISIN / LEI / yf ticker / ticker@MIC)
    sd += eu_broad_records(sd, None if offline else U.load_eu_broad_seeds(s), src.get("eu_broad"))
    # generated price-coverage seeds (full market lists of CN/TW/BR/...; `full` only), enriched from the national lists
    cov_src = src.get("coverage")
    cov_recs = coverage_records(sd, cov_src if cov_src is not None else (None if offline else U.load_coverage_seeds(s)))
    if cov_recs:
        native_in = {id(r): (r.get("csrc_code"), r.get("tse33_code"), r.get("kr_industry_name")) for r in cov_recs}
        _enrich_seeds(cov_recs, {k: v for k, v in src.items() if k != "firds"}, warnings)
        for r in cov_recs:  # keep the seed's industry when the national list has none
            c, t, k = native_in[id(r)]
            r["csrc_code"] = r.get("csrc_code") or c
            r["tse33_code"] = r.get("tse33_code") or t
            r["kr_industry_name"] = r.get("kr_industry_name") or k
        sd += cov_recs

    # seed rows that ARE a US listing (e.g. BAP@XNYS) absorb the US record
    us_by_tk = {r["local_symbol"]: r for r in us}
    drop = set()
    for r in sd:
        if r["mic"] in US_MICS:
            u = us_by_tk.get(r["local_symbol"])
            if u:
                for k, v in u.items():
                    if k not in ("origin", "name", "country", "region", "mic") and v is not None and r.get(k) is None:
                        r[k] = v
                drop.add(id(u))
    us = [u for u in us if id(u) not in drop]

    # issuer-level SEC metadata
    shares_by_cik = {r["cik"]: r for r in shares.iter_rows(named=True)} if shares.height else {}
    best_close: dict[str, float] = {}
    for r in us + sd:
        if r.get("cik") and r.get("sec_type") == "common" and r.get("close"):
            best_close[r["cik"]] = max(best_close.get(r["cik"], 0.0), float(r["close"]))
    for r in us + sd:
        sh = shares_by_cik.get(r.get("cik") or "")
        if sh:
            r["shares"] = sh["shares"]
            loc = sh.get("loc") or ""
            r["frames_country"] = "US" if loc.startswith("US-") else None
            if r["cik"] in best_close:
                r["mcap_sec"] = sh["shares"] * best_close[r["cik"]]
    mcap_by_cik: dict[str, float] = {}
    for r in us:
        if r.get("cik") and r.get("mcap_sec") and r["sec_type"] == "common":
            mcap_by_cik[r["cik"]] = max(mcap_by_cik.get(r["cik"], 0.0), r["mcap_sec"])
    cand = sorted(mcap_by_cik, key=lambda c: -mcap_by_cik[c])
    # multi-class issuers (V, BRK) report dei shares per class (dimensional -> absent from frames): add CIKs by the
    # SEC company_tickers order (market-cap ordered) so they still get submissions + yfinance market cap
    rank_by_cik: dict[str, int] = {}
    for r in us:
        if r.get("cik") and r["sec_type"] == "common" and r.get("sec_rank") is not None:
            rank_by_cik[r["cik"]] = min(rank_by_cik.get(r["cik"], 10**9), int(r["sec_rank"]))
    _ncut = max(us_size + 300, 800)
    top_rank = [c for c in sorted(rank_by_cik, key=rank_by_cik.get)[:_ncut] if c not in mcap_by_cik]
    cand = cand[:_ncut] + top_rank + cand[_ncut:]
    adr_tickers = {(x.get("adr_ticker") or "").replace("-", ".") for x in sd if x.get("adr_ticker")}
    n_sub = int(max_submissions) if max_submissions is not None else max(1500, us_size * 3)
    want_ciks = cand[:n_sub] + [r["cik"] for r in us if r["local_symbol"] in adr_tickers and r.get("cik")] + \
        [r["cik"] for r in sd if r.get("cik")]
    if "submissions" in src:
        subs = src["submissions"]
    else:
        subs = pl.DataFrame() if offline else U.fetch_sec_submissions(want_ciks, settings=s)
    sub_by_cik = {r["cik"]: r for r in subs.iter_rows(named=True)} if subs.height else {}
    tick()

    # ---- clustering (union-find over issuer keys)
    uf = _UF()
    all_recs = us + sd
    for i, r in enumerate(all_recs):
        r["rid"] = f"#r{i}"
        uf.find(r["rid"])
        for k in record_keys(r):
            uf.union(r["rid"], k)
        if r["origin"] == "us":
            uf.union(r["rid"], f"ustk:{r['local_symbol']}")
    blocked = {(o["key"], o["value"]) for o in overrides.iter_rows(named=True) if o["kind"] == "block"}
    for r in sd:
        if r.get("adr_ticker"):
            k = f"ustk:{r['adr_ticker'].replace('-', '.')}"
            if (f"seed:{r['local_symbol']}@{r['mic']}", k) in blocked or k not in uf.p:
                continue
            uf.union(r["rid"], k)
    link_keys: set[str] = set()
    for o in overrides.iter_rows(named=True):
        if o["kind"] == "link" and o.get("key") and o.get("value"):
            uf.union(o["key"], o["value"])
            link_keys |= {o["key"], o["value"]}
    succ_reason: dict[str, str] = {}
    for o in U.load_successions(s).iter_rows(named=True):
        if o.get("predecessor") and o.get("successor") and o["successor"] in uf.p:
            uf.union(o["predecessor"], o["successor"])
            succ_reason[o["predecessor"]] = o.get("reason") or "succession"
    clusters: dict[str, list[dict[str, Any]]] = {}
    for r in all_recs:
        clusters.setdefault(uf.find(r["rid"]), []).append(r)
    # extra (record-less) keys attached to a cluster, e.g. predecessor CIKs from issuer_successions.csv
    extra_keys: dict[str, list[str]] = {}
    for node in list(uf.p):
        if node in succ_reason or node in link_keys:
            extra_keys.setdefault(uf.find(node), []).append(node)
    roots = list(clusters)
    cl_list = list(clusters.values())

    # ---- registry: issuer ids (assigned once, never recomputed)
    reg = Registry(s)
    issuer_of: dict[int, str] = {}
    for ci, recs in enumerate(cl_list):
        keys = sorted({k for r in recs for k in record_keys(r)} | set(extra_keys.get(roots[ci], [])))
        seed_key = next((k for pref in ISSUER_KEY_PREF for k in keys if k.startswith(pref + ":")), None)
        if seed_key is None:
            r0 = recs[0]
            seed_key = f"name:{(r0.get('country') or r0.get('exchange_country') or 'XX')}:{normalize_name(r0.get('name'))}"
            if not keys:
                keys = [seed_key]
        issuer_of[ci] = reg.resolve(keys, seed_key, today)

    # ---- yfinance info: seeds + top US candidates (one ticker per CIK)
    yf_targets = [r["yf_ticker"] for r in sd if r.get("yf_ticker") and not r.get("coverage_only")]
    by_cik_best: dict[str, dict[str, Any]] = {}
    for r in us:
        if r.get("cik") and r["sec_type"] == "common":
            b = by_cik_best.get(r["cik"])
            if b is None or (r.get("dollar_volume") or 0) > (b.get("dollar_volume") or 0):
                by_cik_best[r["cik"]] = r
    us_cand_tk = [by_cik_best[c]["yf_ticker"] for c in cand[: max(us_size + 300, 800) + len(top_rank)] if c in by_cik_best]
    if "yf_info" in src:
        yfi = src["yf_info"]
    elif fetch_yf and not offline:
        yfi = U.fetch_yf_info(yf_targets + us_cand_tk, settings=s)
    else:
        yfi = pl.DataFrame() if offline else U.fetch_yf_info(yf_targets + us_cand_tk, settings=s, budget=0)
    yf_by = {r["yf_ticker"]: r for r in yfi.iter_rows(named=True)} if yfi.height else {}
    # coverage v2 W2: regulatory-register seeds are coverage records (no live .info calls here), but the profiles the
    # register campaign cached (register_fetch step `profile`, registry/yf_info.parquet) give them sector / currency /
    # market cap - read from the cache only (budget 0)
    reg_tk = [r["yf_ticker"] for r in sd if r.get("coverage_only") and r.get("yf_ticker")
              and str(r.get("coverage_source") or "").startswith("regulatory_register")]
    if reg_tk and "yf_info" not in src and not offline:
        try:
            cached = U.fetch_yf_info(reg_tk, settings=s, budget=0, max_age_days=120)
            for r in cached.filter(pl.col("ok").fill_null(False)).iter_rows(named=True) if cached.height else []:
                yf_by.setdefault(r["yf_ticker"], r)
        except Exception as e:  # noqa: BLE001 - cache only, never fatal
            log.warning("register yf_info cache unreadable: %s", e)
    tick()
    fx = src.get("fx")
    if fx is None or fx.is_empty():
        ccys = {str(v.get("currency") or "") for v in yf_by.values()}
        ccys = {{"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(c, c).upper() for c in ccys if c}
        fx = pl.DataFrame({"currency": ["USD"], "usd_per_unit": [1.0]}) if offline else U.fetch_fx_spot(ccys, settings=s)
        if fx.is_empty():
            fx = pl.DataFrame({"currency": ["USD"], "usd_per_unit": [1.0]})
    fx_map = dict(zip(fx.get_column("currency").to_list(), fx.get_column("usd_per_unit").to_list(), strict=True))

    # ---- GLEIF for all LEIs
    leis = {r.get("lei") for r in all_recs if r.get("lei")} | {v.get("lei") for v in sub_by_cik.values() if v.get("lei")}
    gleif = src.get("gleif")
    if gleif is None:
        gleif = pl.DataFrame() if offline else U.fetch_gleif_records(leis, settings=s)
    gleif_by = {r["lei"]: r for r in gleif.iter_rows(named=True)} if gleif.height else {}

    # ---- assemble table rows
    now = datetime.now(UTC_TZ)
    issuers_rows: list[dict[str, Any]] = []
    sec_rows: list[dict[str, Any]] = []
    lst_rows: list[dict[str, Any]] = []
    idm_rows: list[dict[str, Any]] = []
    issuer_extra: dict[str, dict[str, Any]] = {}
    for ci, recs in enumerate(cl_list):
        iid = issuer_of[ci]
        seeds_c = sorted([r for r in recs if r["origin"] == "seed"], key=lambda r: r["seed_order"])
        us_c = [r for r in recs if r["origin"] == "us"]
        all_ciks = sorted({r["cik"] for r in recs if r.get("cik")})
        # current CIK: the one of the most liquid US record (successor after a re-domiciliation), else the first
        us_live = sorted([r for r in recs if r.get("cik") and r["origin"] == "us"], key=lambda r: -(r.get("dollar_volume") or 0))
        cik = (us_live[0]["cik"] if us_live else None) or (all_ciks[0] if all_ciks else None)
        sub = sub_by_cik.get(cik) if cik else None
        secs: list[dict[str, Any]] = []
        if seeds_c:
            home_region = _region_of_country(seeds_c[0]["country"], seeds_c[0]["region"])
            ordered = sorted(seeds_c, key=lambda r: (mic_info(r["mic"])["region"] != home_region, r["seed_order"]))
            for r in ordered:
                secs.append({"type": r["sec_type"], "listings": [r], "rec": r, "class": r.get("share_class")})
            for u in us_c:
                target = next((x for x in secs if (x["rec"].get("adr_ticker") or "").replace("-", ".") == u["local_symbol"]), None)
                if u["sec_type"] in ("adr", "gdr"):
                    secs.append({"type": u["sec_type"], "listings": [u], "rec": u, "class": None,
                                 "underlying": target or secs[0]})
                else:
                    (target or secs[0])["listings"].append(u)
        else:
            commons = sorted([u for u in us_c if u["sec_type"] == "common"], key=lambda u: -(u.get("dollar_volume") or 0))
            others = sorted([u for u in us_c if u["sec_type"] != "common"], key=lambda u: -(u.get("dollar_volume") or 0))
            for u in commons + others:
                secs.append({"type": u["sec_type"], "listings": [u], "rec": u, "class": None})
        if not secs:
            continue
        prim_lst = secs[0]["listings"][0]
        yfp = yf_by.get(prim_lst.get("yf_ticker") or "") or {}
        lei = next((r["lei"] for r in recs if r.get("lei") and r.get("lei_src")), None) \
            or next((r["lei"] for r in recs if r.get("lei")), None) or (sub or {}).get("lei")
        g = gleif_by.get(lei) if lei else None
        country = (seeds_c[0]["country"] if seeds_c else None) or country_iso2((sub or {}).get("biz_country")) \
            or next((r.get("frames_country") for r in recs if r.get("frames_country")), None) \
            or country_iso2(yfp.get("country")) or (g or {}).get("country") or prim_lst.get("exchange_country") or "US"
        region = (seeds_c[0].get("region") if seeds_c else None) or mic_info(prim_lst["mic"])["region"]
        if seeds_c:
            name = seeds_c[0]["name"]
            if seeds_c[0].get("issuer_group"):
                name = re.sub(r"\s*\((A|H|PN|ON|Pref)\)\s*$", "", name)
        else:
            name = _pretty_name(yfp.get("longName")) or _pretty_name(prim_lst.get("name")) or prim_lst["local_symbol"]
        aliases: list[str] = []
        for a in ([(sub or {}).get("sub_name")] + list((sub or {}).get("former_names") or []) +
                  [yfp.get("shortName"), yfp.get("longName"), (g or {}).get("legal_name")] +
                  list((g or {}).get("other_names") or []) + [r.get("name_en_reg") for r in seeds_c] +
                  [r.get("name") for r in us_c]):
            if a and a not in aliases and a != name:
                aliases.append(a)
        form = (sub or {}).get("latest_annual_form") or ""
        filer = ("domestic" if form.startswith("10-K") else "foreign_private_20f" if form.startswith("20-F")
                 else "foreign_40f" if form.startswith("40-F") else ("non_sec" if not cik else None))
        if filer == "domestic":
            gaap = "us_gaap"
        elif filer in ("foreign_private_20f", "foreign_40f") and country != "JP":
            gaap = "ifrs"
        else:
            gaap = "ifrs" if country in EU_UK else DEFAULT_GAAP_BY_COUNTRY.get(country)
        if seeds_c and seeds_c[0]["mic"] in ("XSHG", "XSHE"):
            gaap = "cas"
        rep_ccy = yfp.get("financialCurrency") or (("EUR" if country in EUR_COUNTRIES else DEFAULT_CCY_BY_COUNTRY.get(country))
                                                   if seeds_c else "USD")
        fye = next((r.get("fye_mmdd") for r in seeds_c if r.get("fye_mmdd")), None) or (sub or {}).get("fye_mmdd")

        def first(field: str) -> Any:
            return next((r[field] for r in recs if r.get(field)), None)

        ukch = (g or {}).get("registered_as") if country == "GB" else None
        native = next(((r.get("tse33_code"), "tse33") for r in seeds_c if r.get("tse33_code")), None) \
            or next(((r.get("csrc_code"), "csrc") for r in seeds_c if r.get("csrc_code")), None) \
            or next(((r.get("kr_industry_name"), "ksic_name") for r in seeds_c if r.get("kr_industry_name")), None) \
            or next(((r.get("cvm_sector"), "cvm") for r in seeds_c if r.get("cvm_sector")), None) \
            or next(((r.get("tw_industry_code"), "twse") for r in seeds_c if r.get("tw_industry_code")), None)
        issuers_rows.append({
            "issuer_id": iid, "name": name, "name_local": first("name_local"), "short_name": _short_name(name),
            "aliases": aliases[:12] or None, "country": country, "region": region, "lei": lei, "cik": cik,
            "edinet_code": first("edinet_code"), "dart_corp_code": first("dart_corp_code"), "cvm_code": first("cvm_code"),
            "cn_code": first("cn_code"), "uk_company_number": normalize_id("uk_company_number", ukch) if ukch else None,
            "sic_code": (sub or {}).get("sic"), "native_industry_code": native[0] if native else None,
            "yf_sector": yfp.get("sector"), "yf_industry": yfp.get("industry"), "reporting_gaap": gaap,
            "reporting_currency": rep_ccy, "fiscal_year_end_mmdd": fye, "filer_category": filer,
            "website": yfp.get("website") or (sub or {}).get("website"),
            "description": yfp.get("longBusinessSummary") or (sub or {}).get("description") or None,
            "employees": yfp.get("fullTimeEmployees"), "is_active": True, "inactive_date": None,
            "merged_into_issuer_id": None, "updated_at": now, "run_id": run_id,
        })
        mcap_usd = _mcap_usd(yfp, fx_map) or (mcap_by_cik.get(cik) if cik else None)
        issuer_extra[iid] = {
            "native_scheme": native[1] if native else None, "mcap_usd": mcap_usd, "filer": filer,
            "seeded": any(not (r.get("eu_broad_only") or r.get("coverage_only")) for r in seeds_c),
            "coverage": any(r.get("coverage") for r in seeds_c),
            "eu_broad": any(r.get("eu_broad") for r in seeds_c),
            "primary_close": prim_lst.get("close") or yfp.get("regularMarketPrice") or yfp.get("previousClose"),
            "override_keys": sorted({f"ticker:{l['local_symbol']}@{l['mic']}" for x in secs for l in x["listings"]}
                                    | ({f"cik:{cik}"} if cik else set())
                                    | {f"isin:{x['rec']['isin']}" for x in secs if x["rec"].get("isin")}),
        }
        # securities & listings
        for k_i, x in enumerate(secs):
            r0 = x["rec"]
            keys = [f"tm:{r0['local_symbol']}@{r0['mic']}"]
            if r0.get("isin"):
                keys.append(f"isin:{r0['isin']}")
            if r0.get("share_class_figi"):
                keys.append(f"scfigi:{r0['share_class_figi']}")
            sck = "common" if k_i == 0 else (f"adr:{r0['local_symbol']}" if x["type"] in ("adr", "gdr") else
                                             f"cls:{x.get('class') or r0['local_symbol']}")
            x["security_id"] = reg.security(keys, iid, sck)
        for k_i, x in enumerate(secs):
            r0 = x["rec"]
            sid = x["security_id"]
            lids: list[str] = []
            first_ccy = None
            for j, lrec in enumerate(x["listings"]):
                m = mic_info(lrec["mic"])
                lid = make_listing_id(sid, lrec["mic"])
                lids.append(lid)
                yfl = yf_by.get(lrec.get("yf_ticker") or "") or {}
                qccy = yfl.get("currency") or lrec.get("quote_ccy") or m["ccy"] or rep_ccy or "USD"
                qccy = "GBp" if qccy in ("GBX", "GBp") else qccy
                first_ccy = first_ccy or qccy
                is_us = lrec["mic"] in US_MICS
                lst_rows.append({
                    "listing_id": lid, "security_id": sid, "mic": lrec["mic"], "exchange_country": m["country"] or "US",
                    "local_symbol": lrec["local_symbol"], "yf_ticker": lrec.get("yf_ticker"),
                    "polygon_ticker": lrec.get("polygon_ticker") if is_us else None, "quote_currency": qccy,
                    "price_divisor": 100.0 if qccy in ("GBp", "ZAc", "ILA") else 1.0, "is_primary": j == 0,
                    "lot_size": lrec.get("lot_size"), "timezone": m["tz"], "valid_from": _UNKNOWN_FROM, "valid_to": None,
                    "updated_at": now,
                })
                for t, v in (("ticker_mic", f"{lrec['local_symbol']}@{lrec['mic']}"), ("yf_ticker", lrec.get("yf_ticker")),
                             ("polygon_ticker", lrec.get("polygon_ticker") if is_us else None)):
                    if v:
                        idm_rows.append(_idm(t, v, "listing", iid, sid, lid, "ref_universe", 1.0, now))
            tccy = {"GBp": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(first_ccy or "USD", first_ccy or "USD")
            und = x.get("underlying")
            suffix = (f" ({x['class']})" if x.get("class") and k_i else "") + (" ADR" if x["type"] in ("adr", "gdr") else "")
            sec_rows.append({
                "security_id": sid, "issuer_id": iid, "security_type": x["type"], "share_class": x.get("class"),
                "isin": r0.get("isin"), "figi": r0.get("share_class_figi"), "cusip": None, "primary_listing_id": lids[0],
                "display_ticker": r0.get("yf_ticker") or r0["local_symbol"], "name": name + suffix,
                "trading_currency": tccy, "underlying_security_id": und["security_id"] if und else None, "adr_ratio": None,
                "shares_class_weight": None, "is_primary_security": k_i == 0, "is_active": True,
                "first_trade_date": None, "last_trade_date": None, "delisting_reason": None, "updated_at": now,
                "run_id": run_id,
            })
            for t, v in (("isin", r0.get("isin")), ("share_class_figi", r0.get("share_class_figi")),
                         ("composite_figi", r0.get("composite_figi")), ("kr_stock_code", r0.get("kr_stock_code")),
                         ("jp_sec_code", r0.get("jp_sec_code")), ("hk_code", r0.get("hk_code")),
                         ("cn_code", r0.get("cn_code"))):
                if v:
                    idm_rows.append(_idm(t, v, "security", iid, sid, None, "ref_universe", 1.0, now))
        lei_src = next((r["lei_src"].replace("seed_", "") for r in recs if r.get("lei") == lei and r.get("lei_src")), None) \
            or ("esma_firds" if any(r.get("firds_score") for r in recs) and not (sub or {}).get("lei") else "gleif")
        for t, v, srcid in (("cik", cik, "sec_edgar"), ("lei", lei, lei_src),
                            ("edinet_code", first("edinet_code"), "edinet"), ("dart_corp_code", first("dart_corp_code"), "opendart"),
                            ("cvm_code", first("cvm_code"), "cvm"), ("cnpj", first("cnpj"), "cvm"),
                            ("uk_company_number", ukch, "gleif"), ("name_country", f"{name}@{country}", "ref_universe")):
            if v:
                conf = 0.95 if t == "name_country" else (0.9 if srcid in ("esma_firds", "gleif_name") else 1.0)
                idm_rows.append(_idm(t, v, "issuer", iid, None, None, srcid, conf, now))
        for other in all_ciks:
            if other != cik:
                idm_rows.append(_idm("cik", other, "issuer", iid, None, None, "sec_edgar", 1.0, now))
        for other_lei in sorted({r["lei"] for r in recs if r.get("lei")} - {lei}):
            idm_rows.append(_idm("lei", other_lei, "issuer", iid, None, None, "esma_firds", 0.9, now))
        for k in extra_keys.get(roots[ci], []):
            t, v = k.split(":", 1)
            idt = {"cik": "cik", "lei": "lei", "isin": "isin", "edinet": "edinet_code", "cvm": "cvm_code"}.get(t)
            if idt:
                idm_rows.append(_idm(idt, v, "issuer", iid, None, None, "succession_seed", 1.0, now))
        sn = _short_name(name)
        if sn:
            idm_rows.append(_idm("gdelt_org_name", sn, "issuer", iid, None, None, "ref_universe", 0.8, now))
    tick()
    # non-equity benchmark instruments (indices / ETFs / futures) with their synthetic price ids
    taken = {r["security_id"] for r in sec_rows}
    b_iss, b_sec, b_lst, b_idm = benchmark_reference_rows(now, run_id)
    keep = {r["security_id"] for r in b_sec if r["security_id"] not in taken}
    issuers_rows += [r for r in b_iss if r["issuer_id"] in keep]
    sec_rows += [r for r in b_sec if r["security_id"] in keep]
    lst_rows += [r for r in b_lst if r["security_id"] in keep]
    idm_rows += [r for r in b_idm if r["security_id"] in keep]

    issuers_df = pl.DataFrame(issuers_rows, schema_overrides={
        "aliases": pl.List(pl.Utf8), "employees": pl.Int64, "inactive_date": pl.Date, "merged_into_issuer_id": pl.Utf8,
        "name_local": pl.Utf8, "lei": pl.Utf8, "cik": pl.Utf8, "edinet_code": pl.Utf8, "dart_corp_code": pl.Utf8,
        "cvm_code": pl.Utf8, "cn_code": pl.Utf8, "uk_company_number": pl.Utf8, "sic_code": pl.Utf8,
        "native_industry_code": pl.Utf8, "yf_sector": pl.Utf8, "yf_industry": pl.Utf8, "reporting_gaap": pl.Utf8,
        "reporting_currency": pl.Utf8, "fiscal_year_end_mmdd": pl.Utf8, "filer_category": pl.Utf8, "website": pl.Utf8,
        "description": pl.Utf8, "run_id": pl.Utf8, "short_name": pl.Utf8}, infer_schema_length=None)
    # 2026-10-31: SEC submissions (SIC) and yfinance info are only fetched for the top ~1,500 US candidates and the
    # seeds, so every other issuer came out of this build without a SIC code and lost the sector that
    # identity_resolve had back-filled (~4,300 US issuers per nightly run). Merge, never blank out: inputs missing
    # from this build are taken from the current snapshot before classifying, and a classification that comes out
    # `unknown` keeps the previous one.
    from app.core.storage import read_table as _rt

    prev_issuers = _rt("issuers", settings=s)
    issuers_df, n_carried = carry_classification_inputs(issuers_df, prev_issuers)
    issuers_df, cov = taxonomy.classify_issuers(issuers_df, issuer_extra, settings=s)
    issuers_df, n_kept = keep_previous_classification(issuers_df, prev_issuers)
    if n_carried or n_kept:
        log.info("ref_universe: classification inputs carried over for %d issuers, previous sector kept for %d",
                 n_carried, n_kept)
    # taxonomy coverage stats are about operating companies: leave the synthetic benchmark issuers out
    cov = taxonomy.coverage(issuers_df, issuers_df.filter(~pl.col("issuer_id").str.contains("^(IDX|ETF):"))
                            .get_column("issuer_id").to_list())
    for merged, surv in reg.merges:
        idm_rows.append(_idm("issuer_alias", merged, "issuer", surv, None, None, "ref_universe", 1.0, now))
    sec_df = pl.DataFrame(sec_rows, schema_overrides={
        "cusip": pl.Utf8, "adr_ratio": pl.Float64, "shares_class_weight": pl.Float64, "first_trade_date": pl.Date,
        "last_trade_date": pl.Date, "delisting_reason": pl.Utf8, "underlying_security_id": pl.Utf8,
        "share_class": pl.Utf8, "isin": pl.Utf8, "figi": pl.Utf8, "run_id": pl.Utf8}, infer_schema_length=None)
    lst_df = pl.DataFrame(lst_rows, schema_overrides={"valid_to": pl.Date, "lot_size": pl.Int32, "yf_ticker": pl.Utf8,
                                                      "polygon_ticker": pl.Utf8}, infer_schema_length=None)
    idm_new = pl.DataFrame(idm_rows, schema=_IDM_SCHEMA)
    issuers_df, sec_df, lst_df, idm_df = _merge_previous(issuers_df, sec_df, lst_df, idm_new, reg, today,
                                                         us_ok=bool(us) and poly.height > 0, settings=s)
    sec_df, _ = apply_adr_ratio_seed(sec_df, s)
    stats = _stats(issuers_df, sec_df, lst_df, cov)
    loss = sector_loss(prev_issuers, issuers_df)
    loss.update(carried_inputs=n_carried, kept_previous=n_kept)
    stats["sector_loss"] = loss
    if not loss["passed"]:
        msg = (f"sector loss: {loss['lost']} of {loss['prev_classified']} classified issuers "
               f"({loss['rate']:.1%}) would lose their sector in this run (> {SECTOR_LOSS_MAX:.0%})")
        log.error("ref_universe %s; examples %s", msg, loss.get("examples"))
        warnings.append(msg)
    rows: dict[str, int] = {}
    if write:
        from app.core.storage import write_table

        rows["issuers"] = write_table(issuers_df, "issuers", mode="overwrite", settings=s)
        rows["securities_master"] = write_table(sec_df, "securities_master", mode="overwrite", settings=s)
        rows["listings"] = write_table(lst_df, "listings", mode="overwrite", settings=s)
        rows["identifier_map"] = write_table(idm_df, "identifier_map", mode="overwrite", settings=s)
        reg.save(today)
        _save_extra(issuer_extra, s)
        clear_cache()
        record_sector_loss(loss, run_id, s)
    from app.core.storage import read_table

    need_univ = write_universe
    if need_univ is None:
        need_univ = read_table("universe_membership", settings=s).is_empty()
    um = None
    if need_univ:
        um = build_universe(issuers_df, sec_df, lst_df, issuer_extra, today, us_size=us_size, settings=s,
                            keep_universes=keep_universes)
        stats["universe"] = {r["universe_id"]: r["len"] for r in
                             um.filter(pl.col("valid_to").is_null()).group_by("universe_id").len().iter_rows(named=True)}
        if write:
            from app.core.storage import write_table

            rows["universe_membership"] = write_table(um, "universe_membership", mode="overwrite", settings=s)
    return {"rows": rows, "issuers": issuers_df.height, "warnings": warnings, "skipped_sources": skipped, "stats": stats,
            "frames": {"issuers": issuers_df, "securities_master": sec_df, "listings": lst_df, "identifier_map": idm_df,
                       "universe_membership": um}}


# ------------------------------------------------------------------------------------------------ classification merge
# 2026-10-31: ref_universe rebuilt issuers from sources that only cover part of the universe (SEC submissions for the
# top ~1,500 US candidates + seeds, yfinance info for ~800 + seeds) and overwrote the table, so the SIC-derived
# sectors identity_resolve back-fills were blanked every night (02:42) until identity_resolve re-ran (03:15; budget
# 3,000 CIKs, the rest only on the weekly run). Downstream jobs in between saw ~4,300 US issuers without a sector.

#: inputs of the taxonomy classifier that a build may not have fetched for every issuer (carried from the snapshot)
CLASSIFICATION_INPUTS = ("sic_code", "yf_sector", "yf_industry")
CLASSIFICATION_OUTPUTS = ("sector_id", "subsector_id", "classification_source")
#: DQ: alert when more than this share of the classified issuers would lose their sector in one run
SECTOR_LOSS_MAX = 0.01
SECTOR_LOSS_STATE = "ref_universe_sectors.json"


def carry_classification_inputs(new: pl.DataFrame, prev: pl.DataFrame | None) -> tuple[pl.DataFrame, int]:
    """Fill the classification inputs this build did not fetch (null) from the current snapshot. A value this build
    did fetch always wins (a fresh SIC / yfinance industry is the better source). Returns (frame, issuers filled)."""
    if prev is None or prev.is_empty() or new.is_empty():
        return new, 0
    cols = [c for c in CLASSIFICATION_INPUTS if c in new.columns and c in prev.columns]
    if not cols:
        return new, 0
    p = (prev.select("issuer_id", *[pl.col(c).alias(f"__p_{c}") for c in cols])
         .unique(subset=["issuer_id"], keep="last"))
    out = new.join(p, on="issuer_id", how="left")
    filled = pl.any_horizontal([pl.col(c).is_null() & pl.col(f"__p_{c}").is_not_null() for c in cols])
    n = int(out.select(filled.sum()).item() or 0)
    out = out.with_columns([pl.coalesce(c, f"__p_{c}").alias(c) for c in cols]).drop([f"__p_{c}" for c in cols])
    return out.select(new.columns), n


def keep_previous_classification(new: pl.DataFrame, prev: pl.DataFrame | None) -> tuple[pl.DataFrame, int]:
    """Merge, never blank out: an issuer this build classifies as `unknown` keeps its previous sector / subsector /
    source. A deliberate exclusion (SIC 6770 blank check, yfinance 'Shell Companies': sector null with a real source)
    and any real new classification win. Returns (frame, issuers kept)."""
    if prev is None or prev.is_empty() or new.is_empty() or not set(CLASSIFICATION_OUTPUTS) <= set(prev.columns):
        return new, 0
    p = (prev.filter(pl.col("sector_id").is_not_null())
         .select("issuer_id", *[pl.col(c).alias(f"__p_{c}") for c in CLASSIFICATION_OUTPUTS])
         .unique(subset=["issuer_id"], keep="last"))
    if p.is_empty():
        return new, 0
    out = new.join(p, on="issuer_id", how="left")
    keep = (pl.col("sector_id").is_null() & pl.col("__p_sector_id").is_not_null()
            & (pl.col("classification_source").is_null() | (pl.col("classification_source") == "unknown")))
    out = out.with_columns(keep.alias("__keep"))
    n = int(out.get_column("__keep").sum() or 0)
    out = out.with_columns([pl.when(pl.col("__keep")).then(pl.col(f"__p_{c}")).otherwise(pl.col(c)).alias(c)
                            for c in CLASSIFICATION_OUTPUTS])
    return out.select(new.columns), n


def sector_loss(prev: pl.DataFrame | None, new: pl.DataFrame, max_rate: float = SECTOR_LOSS_MAX) -> dict[str, Any]:
    """DQ check: issuers that were active and classified before this run and come out of it without a sector.
    {"passed", "lost", "prev_classified", "rate", "threshold", "examples", "by_country"}."""
    base = {"passed": True, "lost": 0, "prev_classified": 0, "rate": 0.0, "threshold": max_rate, "examples": [],
            "by_country": {}}
    if prev is None or prev.is_empty() or not {"sector_id", "is_active"} <= set(prev.columns):
        return base
    was = prev.filter(pl.col("is_active").fill_null(True) & pl.col("sector_id").is_not_null()).select(
        "issuer_id", pl.col("country").alias("__c") if "country" in prev.columns else pl.lit(None).alias("__c"))
    was = was.unique(subset=["issuer_id"])
    base["prev_classified"] = was.height
    if was.is_empty():
        return base
    now_ = new.select("issuer_id", "sector_id").unique(subset=["issuer_id"], keep="first")
    lost = was.join(now_, on="issuer_id", how="inner").filter(pl.col("sector_id").is_null())
    rate = lost.height / was.height
    base.update(passed=rate <= max_rate, lost=lost.height, rate=round(rate, 6),
                examples=lost.get_column("issuer_id").head(10).to_list(),
                by_country={r["__c"] or "?": r["len"] for r in lost.group_by("__c").len()
                            .sort("len", descending=True).head(8).iter_rows(named=True)})
    return base


def record_sector_loss(loss: dict[str, Any], run_id: str | None, settings=None) -> None:
    """Persist the last ref_universe sector-loss check (read by the dq hook `issuer_sector_loss` and the ops alert)."""
    import json as _json

    from app.core.config import get_settings as _gs

    s = settings or _gs()
    p = s.state_dir / SECTOR_LOSS_STATE
    rec = {**loss, "run_id": run_id, "checked_at": datetime.now(UTC_TZ).isoformat(timespec="seconds")}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_json.dumps(rec, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:  # pragma: no cover
        log.warning("sector-loss state not written: %s", e)


def read_sector_loss(settings=None) -> dict[str, Any] | None:
    import json as _json

    from app.core.config import get_settings as _gs

    p = (settings or _gs()).state_dir / SECTOR_LOSS_STATE
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _merge_previous(issuers: pl.DataFrame, secs: pl.DataFrame, lsts: pl.DataFrame, idm: pl.DataFrame, reg: Registry,
                    today: date, us_ok: bool, settings=None) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Slowly-changing merge with the current snapshot: keep vanished issuers/securities (inactive, survivorship-free),
    close listing rows whose symbol changed, preserve identifier_map validity history."""
    from app.core.schema import conform
    from app.core.storage import read_table

    issuers = conform(issuers.unique(subset=["issuer_id"], keep="first", maintain_order=True), "issuers")
    secs = conform(secs.unique(subset=["security_id"], keep="first", maintain_order=True), "securities_master")
    lsts = conform(lsts.unique(subset=["listing_id", "valid_from"], keep="first", maintain_order=True), "listings")
    prev_i = read_table("issuers", settings=settings)
    prev_s = read_table("securities_master", settings=settings)
    prev_l = read_table("listings", settings=settings)
    prev_m = read_table("identifier_map", settings=settings)
    yday = today - timedelta(days=1)
    if prev_i.height:
        gone = prev_i.filter(~pl.col("issuer_id").is_in(issuers.get_column("issuer_id").implode()))
        if gone.height:
            merged = {k: reg.survivor(k) for k in gone.get_column("issuer_id").to_list() if reg.survivor(k) != k}
            gone = gone.with_columns(
                pl.col("issuer_id").replace_strict(merged, default=pl.col("merged_into_issuer_id")).alias("merged_into_issuer_id"),
                pl.lit(False).alias("is_active"),
                pl.coalesce(pl.col("inactive_date"), pl.lit(today)).alias("inactive_date"),
            )
            issuers = pl.concat([issuers, conform(gone, "issuers")], how="vertical_relaxed")
    if prev_m.height:  # FIGIs asserted by openfigi_refresh (identifier_map) fill securities_master.figi
        f = (prev_m.filter((pl.col("id_type") == "share_class_figi") & pl.col("valid_to").is_null() & pl.col("security_id").is_not_null())
             .unique(subset=["security_id"], keep="last").select("security_id", pl.col("id_value").alias("__f")))
        if f.height:
            secs = secs.join(f, on="security_id", how="left").with_columns(pl.coalesce("figi", "__f").alias("figi")).drop("__f")
    if prev_s.height:
        gone = prev_s.filter(~pl.col("security_id").is_in(secs.get_column("security_id").implode()))
        if gone.height:
            if us_ok:
                gone = gone.with_columns(pl.lit(False).alias("is_active"),
                                         pl.coalesce(pl.col("last_trade_date"), pl.lit(yday)).alias("last_trade_date"))
            secs = pl.concat([secs, conform(gone, "securities_master")], how="vertical_relaxed")
    if prev_l.height:
        cur_new = {r["listing_id"]: r for r in lsts.iter_rows(named=True)}
        hist = []
        for r in prev_l.iter_rows(named=True):
            n = cur_new.get(r["listing_id"])
            if r["valid_to"] is not None:
                hist.append(r)
            elif n is None:
                hist.append(dict(r, valid_to=yday) if (us_ok and r["exchange_country"] == "US") else r)
            elif n["local_symbol"] != r["local_symbol"]:
                hist.append(dict(r, valid_to=yday))
                cur_new[r["listing_id"]] = dict(n, valid_from=today)
            else:
                cur_new[r["listing_id"]] = dict(n, valid_from=r["valid_from"])
        lsts = pl.DataFrame(list(cur_new.values()) + hist, schema=lsts.schema).unique(
            subset=["listing_id", "valid_from"], keep="first", maintain_order=True)
    idm = idm.filter(pl.col("id_value").is_not_null()).unique(subset=["id_type", "id_value"], keep="first", maintain_order=True)
    if prev_m.height:
        tgt = ("issuer_id", "security_id", "listing_id")
        cur_prev = {(r["id_type"], r["id_value"]): r for r in prev_m.filter(pl.col("valid_to").is_null()).iter_rows(named=True)}
        out = prev_m.filter(pl.col("valid_to").is_not_null()).to_dicts()
        seen = set()
        for r in idm.iter_rows(named=True):
            k = (r["id_type"], r["id_value"])
            seen.add(k)
            p = cur_prev.get(k)
            if p is None:
                out.append(dict(r, valid_from=today))
            elif all(p[c] == r[c] for c in tgt) or reg.survivor(p["issuer_id"]) == r["issuer_id"] and p["security_id"] == r["security_id"]:
                out.append(dict(r, valid_from=p["valid_from"]))
            else:
                if p["valid_from"] < today:
                    out.append(dict(p, valid_to=yday))
                out.append(dict(r, valid_from=today))
        new_lid_types = {(r["id_type"], r["listing_id"]) for r in idm.iter_rows(named=True) if r["listing_id"]}
        for k, p in cur_prev.items():
            if k in seen:
                continue
            if p["listing_id"] and p["id_type"] in ("ticker_mic", "yf_ticker", "polygon_ticker") \
                    and (p["id_type"], p["listing_id"]) in new_lid_types:
                out.append(dict(p, valid_to=yday))  # the listing now trades under another symbol
            else:
                out.append(p)  # carried over (e.g. FIGI/fuzzy rows written by other jobs)
        idm = pl.DataFrame(out, schema=_IDM_SCHEMA)
    idm = idm.unique(subset=["id_type", "id_value", "valid_from"], keep="last", maintain_order=True)
    return issuers, secs, lsts, conform(idm, "identifier_map")


def _save_extra(extra: dict[str, dict[str, Any]], settings) -> None:
    from app.normalize.identity_universe import registry_dir

    rows = [{"issuer_id": k, "mcap_usd": v.get("mcap_usd"), "filer": v.get("filer"), "seeded": v.get("seeded"),
             "native_scheme": v.get("native_scheme"), "primary_close": v.get("primary_close"),
             "eu_broad": bool(v.get("eu_broad")), "coverage": bool(v.get("coverage"))} for k, v in extra.items()]
    df = pl.DataFrame(rows, schema={"issuer_id": pl.Utf8, "mcap_usd": pl.Float64, "filer": pl.Utf8, "seeded": pl.Boolean,
                                    "native_scheme": pl.Utf8, "primary_close": pl.Float64, "eu_broad": pl.Boolean,
                                    "coverage": pl.Boolean})
    df.write_parquet(registry_dir(settings) / "issuer_extra.parquet")


def load_extra(settings=None) -> dict[str, dict[str, Any]]:
    from app.normalize.identity_universe import registry_dir

    p = registry_dir(settings) / "issuer_extra.parquet"
    if not p.exists():
        return {}
    return {r["issuer_id"]: r for r in pl.read_parquet(p).iter_rows(named=True)}


def _stats(issuers: pl.DataFrame, secs: pl.DataFrame, lsts: pl.DataFrame, cov: dict[str, Any]) -> dict[str, Any]:
    act = issuers.filter(pl.col("is_active"))
    cur = lsts.filter(pl.col("valid_to").is_null())
    return {
        "issuers_by_region": {r["region"]: r["len"] for r in act.group_by("region").len().iter_rows(named=True)},
        "listings_by_region": {r["region"]: r["len"] for r in cur.join(secs.select("security_id", "issuer_id"), on="security_id")
                               .join(issuers.select("issuer_id", "region"), on="issuer_id").group_by("region").len().iter_rows(named=True)},
        "securities": secs.height, "listings": cur.height, "taxonomy": cov,
    }


def register_securities(issuers: pl.DataFrame, prim: pl.DataFrame, settings=None) -> dict[str, str]:
    """Coverage v2 W2: primary securities of the active regulatory-register issuers (SEC listed periodic filers, EEA
    regulated markets, UK Official List / AIM) -> {security_id: register} for universe `full` (its contract: every
    security with prices + fundamentals). Matched by CIK / LEI on the new issuers frame; blank-check / fund SICs
    (EXCLUDED_SIC) stay out like in the S2 rule. BIG360_REGISTER_UNIVERSE=0 switches the rule off."""
    import os

    from app.core.storage import read_table
    from app.normalize.identity_universe import EXCLUDED_SIC

    if os.environ.get("BIG360_REGISTER_UNIVERSE", "1") == "0":
        return {}
    try:
        reg = read_table("regulatory_register", settings=settings).filter(pl.col("status") == "active")
    except Exception:  # noqa: BLE001 - register not built yet
        return {}
    if reg.is_empty():
        return {}
    act = issuers.filter(pl.col("is_active"))
    by_cik = dict(zip(act["cik"].to_list(), act["issuer_id"].to_list(), strict=True))
    by_lei = dict(zip(act["lei"].to_list(), act["issuer_id"].to_list(), strict=True))
    want: dict[str, str] = {}
    for cik, lei, regname in reg.select("cik", "lei", "register").iter_rows():
        iid = by_cik.get(cik) if cik else None
        iid = iid or (by_lei.get(lei) if lei else None)
        if iid:
            want.setdefault(iid, regname)
    p = prim.filter(pl.col("issuer_id").is_in(list(want))
                    & ~pl.col("sic_code").fill_null("").is_in(list(EXCLUDED_SIC))
                    & pl.col("security_type").is_in(["common", "preferred", "adr", "gdr", "reit_unit"]))
    return {sid: want[iid] for sid, iid in p.select("security_id", "issuer_id").iter_rows()}


def build_universe(issuers: pl.DataFrame, secs: pl.DataFrame, lsts: pl.DataFrame, extra: dict[str, dict[str, Any]],
                   today: date, us_size: int = 500, settings=None, keep_universes: Sequence[str] = ()) -> pl.DataFrame:
    """universe_membership rows (mvp_us, mvp_global, full) merged with the previous membership history
    (members that drop out get valid_to = today-1; survivorship-free). `keep_universes` (e.g. mvp_us, mvp_global):
    their current membership is carried over unchanged (price-coverage rebalances only grow `full`)."""
    from app.core.storage import read_table
    from app.normalize.identity_universe import EXCLUDED_SIC, UNIVERSE_RULES, load_overrides, load_us_pins

    pins = load_us_pins(settings)
    excl = {o["key"] for o in load_overrides(settings).iter_rows(named=True) if o["kind"] == "exclude_universe"}
    ext = pl.DataFrame([{"issuer_id": k, "mcap_usd": v.get("mcap_usd"), "seeded": bool(v.get("seeded")),
                         "close": v.get("primary_close"), "eu_broad": bool(v.get("eu_broad")),
                         "coverage": bool(v.get("coverage"))} for k, v in extra.items()],
                       schema={"issuer_id": pl.Utf8, "mcap_usd": pl.Float64, "seeded": pl.Boolean, "close": pl.Float64,
                               "eu_broad": pl.Boolean, "coverage": pl.Boolean})
    prim = (secs.filter(pl.col("is_primary_security") & pl.col("is_active"))
            .join(lsts.filter(pl.col("valid_to").is_null()).select("listing_id", "mic", "local_symbol"),
                  left_on="primary_listing_id", right_on="listing_id", how="left")
            .join(issuers.filter(pl.col("is_active")).select("issuer_id", "region", "sic_code", "filer_category",
                                                             "sector_id", "cik"), on="issuer_id", how="inner")
            .join(ext, on="issuer_id", how="left")
            .with_columns(pl.col("seeded").fill_null(False), pl.col("eu_broad").fill_null(False),
                          pl.col("coverage").fill_null(False)))
    pin_inc = {r["ticker"].upper().replace("-", ".") for r in pins.iter_rows(named=True) if (r.get("action") or "").lower() == "include"}
    pin_exc = {r["ticker"].upper().replace("-", ".") for r in pins.iter_rows(named=True) if (r.get("action") or "").lower() == "exclude"}
    excl_ciks = [k.split(":", 1)[1] for k in excl if k.startswith("cik:")]
    us_ok = prim.filter(
        pl.col("mic").is_in(["XNAS", "XNYS", "XASE", "BATS", "XTXS"]) & (pl.col("security_type") == "common")
        & ~pl.col("seeded") & ~pl.col("eu_broad") & ~pl.col("coverage") & pl.col("mcap_usd").is_not_null()
        & (pl.col("filer_category").is_null() | (pl.col("filer_category") == "domestic"))
        & ~pl.col("sic_code").fill_null("").is_in(list(EXCLUDED_SIC))
        & ~pl.col("local_symbol").is_in(list(pin_exc)) & ~pl.col("cik").fill_null("").is_in(excl_ciks)
    )
    ranked = us_ok.sort("mcap_usd", descending=True).with_row_index("rk", offset=1)
    rows: list[tuple[str, str, int | None, str]] = []
    for r in ranked.filter((pl.col("rk") <= us_size) | pl.col("local_symbol").is_in(list(pin_inc))).iter_rows(named=True):
        rows.append(("mvp_us", r["security_id"], int(r["rk"]), f"top{us_size} US by mcap" if r["rk"] <= us_size else "pin"))
    glob = prim.filter(pl.col("seeded")).with_columns(
        pl.col("mcap_usd").rank("ordinal", descending=True).over("region").alias("rk"))
    for r in glob.iter_rows(named=True):
        rows.append(("mvp_global", r["security_id"], int(r["rk"]) if r["rk"] is not None else None, "seeds/mvp_global.csv"))
    eub = prim.filter(pl.col("eu_broad")).with_columns(pl.col("mcap_usd").rank("ordinal", descending=True).alias("rk"))
    for r in eub.iter_rows(named=True):
        rows.append(("eu_broad", r["security_id"], int(r["rk"]) if r["rk"] is not None else None,
                     "seeds/eu_broad.csv: EEA/UK/CH/NO mcap>=EUR300M & liquid"))
    rule = UNIVERSE_RULES["full"]["us"]
    full_us = ranked.filter((pl.col("mcap_usd") >= rule["min_mcap_usd"]) & (pl.col("close").fill_null(0) >= rule["min_price"]))
    rk_map = {r["security_id"]: r["rk"] for r in ranked.iter_rows(named=True)}
    cov_sids = set(prim.filter(pl.col("coverage")).get_column("security_id").to_list())
    other_sids = {x[1] for x in rows}
    reg_sids = register_securities(issuers, prim, settings)
    full_us_sids = set(full_us.get_column("security_id").to_list())
    for sid in sorted(full_us_sids | other_sids | cov_sids | set(reg_sids)):
        why = "S2: US mcap>=300M & px>=5, plus all seeds"
        if sid in cov_sids and sid not in rk_map and sid not in other_sids:
            why = "price coverage: seeds/price_coverage.csv (full market list)"
        if sid in reg_sids and sid not in other_sids and sid not in full_us_sids:
            why = f"regulatory register ({reg_sids[sid]})"
        rows.append(("full", sid, int(rk_map[sid]) if sid in rk_map else None, why))
    now = datetime.now(UTC_TZ)
    schema = {"universe_id": pl.Utf8, "security_id": pl.Utf8, "valid_from": pl.Date, "valid_to": pl.Date,
              "rank_at_entry": pl.Int32, "reason": pl.Utf8, "updated_at": pl.Datetime("us", "UTC")}
    new = pl.DataFrame([{"universe_id": u, "security_id": sid, "valid_from": today, "valid_to": None, "rank_at_entry": rk,
                         "reason": why, "updated_at": now} for u, sid, rk, why in rows], schema=schema)
    new = new.unique(subset=["universe_id", "security_id"], keep="first", maintain_order=True)
    prev = read_table("universe_membership", settings=settings)
    if keep_universes and prev.height:
        kept = prev.filter(pl.col("valid_to").is_null() & pl.col("universe_id").is_in(list(keep_universes)))
        new = pl.concat([new.filter(~pl.col("universe_id").is_in(list(keep_universes))),
                         kept.select(list(schema)).cast(schema)], how="vertical")
    if prev.is_empty():
        return new
    out = prev.filter(pl.col("valid_to").is_not_null()).to_dicts()
    cur_prev = {(r["universe_id"], r["security_id"]): r for r in prev.filter(pl.col("valid_to").is_null()).iter_rows(named=True)}
    seen = set()
    for r in new.iter_rows(named=True):
        k = (r["universe_id"], r["security_id"])
        seen.add(k)
        out.append(dict(cur_prev[k], updated_at=now) if k in cur_prev else r)
    for k, p in cur_prev.items():
        if k not in seen:
            out.append(dict(p, valid_to=max(p["valid_from"], today - timedelta(days=1)), updated_at=now))
    return pl.DataFrame(out, schema=schema).unique(subset=["universe_id", "security_id", "valid_from"], keep="last")


def merge_identifier_rows(rows: Sequence[dict[str, Any]], run_id: str | None = None, settings=None) -> int:
    """Add/refresh identifier_map rows (FIGI refresh, confirmed or fuzzy matches). A current mapping for the same
    (id_type, id_value) pointing to the same entity is kept (confidence = max); a different target closes the old row."""
    from app.core.schema import conform
    from app.core.storage import read_table, write_table

    if not rows:
        return 0
    now = datetime.now(UTC_TZ)
    today = date.today()
    new = pl.DataFrame([_idm(r["id_type"], r["id_value"], r["entity_level"], r["issuer_id"], r.get("security_id"),
                             r.get("listing_id"), r.get("source_id", "manual"), float(r.get("confidence", 1.0)), now)
                        for r in rows], schema=_IDM_SCHEMA).filter(pl.col("id_value").is_not_null())
    prev = read_table("identifier_map", settings=settings)
    out = prev.to_dicts()
    pos = {(r["id_type"], r["id_value"], r["valid_from"]): i for i, r in enumerate(out)}
    cur = {(r["id_type"], r["id_value"]): r for r in out if r["valid_to"] is None}
    n = 0
    for r in new.iter_rows(named=True):
        k = (r["id_type"], r["id_value"])
        p = cur.get(k)
        if p and (p["issuer_id"], p["security_id"], p["listing_id"]) == (r["issuer_id"], r["security_id"], r["listing_id"]):
            out[pos[(p["id_type"], p["id_value"], p["valid_from"])]] = dict(
                p, confidence=max(p["confidence"], r["confidence"]), updated_at=now)
            continue
        if p and p["confidence"] > r["confidence"]:
            continue  # never let a weaker assertion replace an authoritative one
        if p:
            out[pos[(p["id_type"], p["id_value"], p["valid_from"])]] = dict(p, valid_to=today - timedelta(days=1))
        newrow = dict(r, valid_from=today if p else _UNKNOWN_FROM)
        out.append(newrow)
        cur[k] = newrow
        n += 1
    df = pl.DataFrame(out, schema=_IDM_SCHEMA).unique(subset=["id_type", "id_value", "valid_from"], keep="last")
    write_table(conform(df, "identifier_map"), "identifier_map", mode="overwrite", settings=settings)
    clear_cache()
    return n


# ================================================================================================ IDENTITY_RESOLVE JOB
# Back-fills internal ids on rows written by adapters with source-native keys, detects CIK successions,
# harvests CUSIPs (SEC fails-to-deliver: cusip+symbol) and flags point-in-time anomalies.

def _compact(name: str | None) -> str:
    return normalize_name(name or "").replace(" ", "")


def detect_successions(unresolved_ciks: Sequence[str], settings=None, budget: int = 300) -> list[dict[str, Any]]:
    """Link CIKs that are unknown to identifier_map to an existing issuer when the SEC shows a succession:

    * shared ticker: the old CIK's submissions still list a ticker that today maps to issuer I, and the compacted
      names match (rapidfuzz ratio >= 80) — guards against recycled tickers;
    * successor registration: issuer I's current CIK filed an 8-K12B / 8-K12G3 (Rule 12g-3 successor) and the
      compacted names match (ratio >= 90) within the same country (only these candidates' submissions are fetched).
    Returns [{old_cik, issuer_id, reason, score, old_name}] (written with confidence 0.95)."""
    from rapidfuzz import fuzz, process

    from app.core.storage import read_table
    from app.normalize.identity_universe import fetch_sec_submissions

    if not unresolved_ciks:
        return []
    olds = fetch_sec_submissions(list(unresolved_ciks)[:budget], settings=settings)
    if olds.is_empty():
        return []
    iss = read_table("issuers", settings=settings).filter(pl.col("is_active") & pl.col("cik").is_not_null())
    if iss.is_empty():
        return []
    idm = read_table("identifier_map", settings=settings).filter(
        (pl.col("id_type") == "polygon_ticker") & pl.col("valid_to").is_null())
    tk2iss = dict(zip(idm.get_column("id_value").to_list(), idm.get_column("issuer_id").to_list(), strict=True))
    rows = iss.to_dicts()
    name_of = {r["issuer_id"]: r["name"] for r in rows}
    by_cc: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    for r in rows:
        lst = by_cc.setdefault(r.get("country") or "US", ([], []))
        lst[0].append(_compact(r["name"]))
        lst[1].append(r)
    out, pending = [], []
    for o in olds.iter_rows(named=True):
        oname = _compact(o.get("sub_name"))
        if not oname:
            continue
        hit = None
        for tk in o.get("tickers") or []:
            iid = tk2iss.get(tk.replace("-", ".").upper())
            if iid and fuzz.ratio(oname, _compact(name_of.get(iid))) >= 80:
                hit = (iid, "shared_ticker", fuzz.ratio(oname, _compact(name_of.get(iid))))
                break
        if hit:
            out.append({"old_cik": o["cik"], "issuer_id": hit[0], "reason": hit[1], "score": float(hit[2]), "old_name": o.get("sub_name")})
            continue
        occ = country_iso2(o.get("biz_country")) or "US"
        names, recs = by_cc.get(occ, ([], []))
        for _, score, idx in process.extract(oname, names, scorer=fuzz.ratio, limit=3, score_cutoff=90):
            if recs[idx]["cik"] != o["cik"]:
                pending.append((o, recs[idx], score))
    if pending:
        subs = fetch_sec_submissions([c["cik"] for _, c, _ in pending], settings=settings)
        succ = set(subs.filter(pl.col("succession_form").fill_null(False)).get_column("cik").to_list())
        seen = {x["old_cik"] for x in out}
        for o, cand, score in sorted(pending, key=lambda x: -x[2]):
            if cand["cik"] in succ and o["cik"] not in seen:
                out.append({"old_cik": o["cik"], "issuer_id": cand["issuer_id"], "reason": "successor_8k12", "score": float(score),
                            "old_name": o.get("sub_name")})
                seen.add(o["cik"])
    return out


def harvest_ftd_cusips(settings=None) -> list[dict[str, Any]]:
    """CUSIP <-> US ticker pairs from SEC fails-to-deliver (latest settlement date per CUSIP) -> identifier rows."""
    from app.core.storage import scan_table

    try:
        ftd = (scan_table("fails_to_deliver", settings).select("cusip", "symbol", "settlement_date")
               .filter(pl.col("cusip").is_not_null() & pl.col("symbol").is_not_null()).collect())
    except Exception as e:  # noqa: BLE001
        log.warning("fails_to_deliver unavailable: %s", e)
        return []
    if ftd.is_empty():
        return []
    latest = ftd.sort("settlement_date").unique(subset=["cusip"], keep="last")
    res = lookup_many("polygon_ticker", latest.get_column("symbol").str.replace_all("-", ".").to_list())
    m = {r["input_value"]: r for r in res.iter_rows(named=True)}
    rows = []
    for r in latest.iter_rows(named=True):
        hit = m.get(r["symbol"].replace("-", "."))
        if hit and hit.get("security_id"):
            rows.append({"id_type": "cusip", "id_value": r["cusip"], "entity_level": "security", "issuer_id": hit["issuer_id"],
                         "security_id": hit["security_id"], "source_id": "sec_ftd", "confidence": 0.95})
    return rows


def isin_from_cusip(cusip: str | None, country: str | None) -> str | None:
    """ISO 6166 ISIN for a US/CA security from its 9-char CUSIP: country prefix + CUSIP + check digit.

    Only numeric-first CUSIPs (US/CA issuers and US-issued ADRs, e.g. TM 892331307 -> US8923313071) are derived;
    CINS codes (first char a letter, e.g. Bermuda/Cayman 'G...') carry the issuer's domicile in the ISIN prefix
    and are skipped. Prefix CA only for Canadian issuers, else US."""
    from app.normalize.identity_universe import isin_check_digit  # noqa: PLC0415

    c = (cusip or "").strip().upper()
    if not re.fullmatch(r"\d[0-9A-Z]{7}\d", c):
        return None
    if _cusip_check_digit(c[:8]) != c[8]:
        return None
    body = ("CA" if (country or "").upper() == "CA" else "US") + c
    return body + isin_check_digit(body)


def _cusip_check_digit(body8: str) -> str:
    total = 0
    for i, ch in enumerate(body8):
        v = int(ch) if ch.isdigit() else (ord(ch) - 55 if ch.isalpha() else {"*": 36, "@": 37, "#": 38}[ch])
        if i % 2:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def derive_isins_from_cusips(settings=None) -> list[dict[str, Any]]:
    """identifier_map rows (isin, source 'derived_cusip', confidence 0.95) for securities that have a CUSIP but no ISIN
    from any registry (identifier_map or securities_master)."""
    from app.core.storage import read_table

    try:
        idm = read_table("identifier_map", settings=settings).filter(pl.col("valid_to").is_null())
        sm = read_table("securities_master", columns=["security_id", "issuer_id", "isin"], settings=settings)
        iss = read_table("issuers", columns=["issuer_id", "country"], settings=settings)
    except Exception as e:  # noqa: BLE001
        log.warning("derive_isins_from_cusips: reference tables unavailable: %s", e)
        return []
    has_isin = set(idm.filter((pl.col("id_type") == "isin") & pl.col("security_id").is_not_null())
                   .get_column("security_id").to_list()) | set(sm.filter(pl.col("isin").is_not_null()).get_column("security_id").to_list())
    taken = set(idm.filter(pl.col("id_type") == "isin").get_column("id_value").to_list())
    cc = dict(zip(iss.get_column("issuer_id").to_list(), iss.get_column("country").to_list(), strict=True))
    cus = (idm.filter((pl.col("id_type") == "cusip") & pl.col("security_id").is_not_null())
           .sort("confidence", "updated_at", descending=True).unique(subset=["security_id"], keep="first"))
    rows = []
    for r in cus.iter_rows(named=True):
        if r["security_id"] in has_isin:
            continue
        isin = isin_from_cusip(r["id_value"], cc.get(r["issuer_id"]))
        if isin and isin not in taken:
            taken.add(isin)
            rows.append({"id_type": "isin", "id_value": isin, "entity_level": "security", "issuer_id": r["issuer_id"],
                         "security_id": r["security_id"], "source_id": "derived_cusip", "confidence": 0.95})
    return rows


def apply_adr_ratio_seed(sec: pl.DataFrame, settings=None) -> tuple[pl.DataFrame, int]:
    """Set securities_master.adr_ratio (underlying ordinary shares per ADS) for ADR/GDR rows from seeds/adr_ratios.csv.
    Without it valuation/forecast/cost_of_capital divide by 1 and ADR market caps are off by the ratio (TM x10)."""
    from app.normalize.identity_universe import adr_ticker_key, load_adr_ratios  # noqa: PLC0415

    ratios = load_adr_ratios(settings)
    if not ratios or sec.is_empty() or "adr_ratio" not in sec.columns:
        return sec, 0
    seed = pl.col("display_ticker").map_elements(lambda t: ratios.get(adr_ticker_key(t)), return_dtype=pl.Float64)
    is_dr = pl.col("security_type").is_in(["adr", "gdr"])
    new = sec.with_columns(pl.when(is_dr & seed.is_not_null()).then(seed).otherwise(pl.col("adr_ratio"))
                           .cast(pl.Float64).alias("__adr"))
    changed = int(new.select(((pl.col("__adr") != pl.col("adr_ratio")) | (pl.col("__adr").is_not_null()
                                                                          & pl.col("adr_ratio").is_null())).sum()).item() or 0)
    return new.with_columns(pl.col("__adr").alias("adr_ratio")).drop("__adr"), changed


def backfill_sic_taxonomy(settings=None, budget: int | None = 3000, scope: str = "all") -> tuple[int, int]:
    """Issuers without a sector that have a CIK: fetch SEC submissions for their CIKs (shared 8 req/s sec_edgar
    bucket, configured User-Agent, 30-day cache), set issuers.sic_code and re-run the taxonomy classifier (precedence
    unchanged: override > yf_industry > sic > ...). Returns (sic_set, classified).
    scope "insider" = only issuers with insider activity (Insider Radar 'Unclassified', the original scope); "all"
    (default since the completeness audit 2026-10-27: 4,245 of 5,725 US issuers had no sector) = every CIK issuer,
    insider-active ones first, `budget` CIKs per run."""
    from app.core.storage import scan_table
    from app.normalize import taxonomy
    from app.normalize.identity_universe import fetch_sec_submissions

    try:
        iss = (scan_table("issuers", settings).filter(pl.col("sector_id").is_null() & pl.col("cik").is_not_null()
                                                      & pl.col("sic_code").is_null())
               .select("issuer_id", "cik").unique().collect())
    except Exception as e:  # noqa: BLE001
        log.warning("sic backfill: issuers unavailable: %s", e)
        return 0, 0
    active: set[str] = set()
    for t in ("insider_signals", "insider_transactions"):
        try:
            active |= set(scan_table(t, settings).select("issuer_id").drop_nulls().unique().collect()["issuer_id"].to_list())
        except Exception:  # noqa: BLE001
            continue
    if scope == "insider":
        todo = iss.filter(pl.col("issuer_id").is_in(list(active)))
    else:
        todo = iss.with_columns(pl.col("issuer_id").is_in(list(active)).alias("__a")).sort("__a", descending=True).drop("__a")
    if budget is not None:
        todo = todo.head(int(budget))
    if todo.is_empty():
        return 0, 0
    subs = fetch_sec_submissions(todo["cik"].to_list(), settings=settings, budget=budget)
    if subs.is_empty():
        return 0, 0
    sic = {c: v for c, v in zip(subs["cik"].to_list(), subs["sic"].to_list(), strict=True) if v}
    sic_of = {iid: sic[c] for iid, c in zip(todo["issuer_id"].to_list(), todo["cik"].to_list(), strict=True)
              if c in sic}
    if not sic_of:
        return 0, 0
    counts = {"classified": 0}

    def tf(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        hit = pl.col("issuer_id").is_in(list(sic_of)) & pl.col("sic_code").is_null() & pl.col("sector_id").is_null()
        df = df.with_columns(hit.alias("__hit"))
        if not df.get_column("__hit").any():
            return df.drop("__hit"), 0
        df = df.with_columns(pl.when(pl.col("__hit")).then(pl.col("issuer_id").replace_strict(sic_of, default=None))
                             .otherwise(pl.col("sic_code")).alias("sic_code"))
        sub = df.filter(pl.col("__hit"))
        cls, _ = taxonomy.classify_issuers(sub.drop("sector_id", "subsector_id", "classification_source", "__hit"),
                                           settings=settings)
        cls = cls.select("issuer_id", pl.col("sector_id").alias("__s"), pl.col("subsector_id").alias("__ss"),
                         pl.col("classification_source").alias("__src")).unique(subset=["issuer_id"])
        counts["classified"] += int(cls["__s"].is_not_null().sum())
        out = df.join(cls, on="issuer_id", how="left").with_columns(
            pl.coalesce("sector_id", "__s").alias("sector_id"), pl.coalesce("subsector_id", "__ss").alias("subsector_id"),
            pl.when(pl.col("sector_id").is_null() & pl.col("__s").is_not_null()).then(pl.col("__src"))
            .otherwise(pl.col("classification_source")).alias("classification_source"),
            pl.when(pl.col("__hit")).then(pl.lit(datetime.now(UTC_TZ))).otherwise(pl.col("updated_at")).alias("updated_at"),
        ).drop("__s", "__ss", "__src", "__hit")
        return out, int(sub.height)

    n = _rewrite_partitions("issuers", tf, settings)
    return n, counts["classified"]


def _rewrite_partitions(table: str, transform, settings=None) -> int:
    """Read-modify-write every partition of `table` under its writer lock. transform(df) -> (df, n_changed).
    Only partitions with changes are rewritten (atomic swap). Returns total changed rows."""
    from app.core import storage as st
    from app.core.schema import conform, get_contract

    c = get_contract(table)
    root = st.table_dir(c, settings)
    if not root.exists():
        return 0
    pcols = list(c.partition_by)
    dirs = sorted({f.parent for f in st._parquet_files(root)})
    total = 0
    with st.table_lock(table, settings):
        for d in dirs:
            df = st._read_partition(d, c)
            if df is None or df.is_empty():
                continue
            if pcols:
                kv = dict(part.split("=", 1) for part in d.relative_to(root).parts if "=" in part)
                df = df.with_columns([(pl.lit(None) if kv.get(p) in (None, "__null__") else pl.lit(kv[p])).cast(c.column(p).polars).alias(p)
                                      for p in pcols])
            new, n = transform(df)
            if not n:
                continue
            new = conform(new, c, strict=False)
            if pcols:
                new = new.drop(pcols)
            tmp = d.parent / f".tmp-{d.name}-resolve"
            if tmp.exists():
                st._rmtree(tmp)
            st._write_partition_files(tmp, new)
            st._swap_in(tmp, d)
            total += n
    return total


def _fill_issuer(df: pl.DataFrame, also_security: bool = False) -> tuple[pl.DataFrame, int]:
    if "source_entity_id" not in df.columns or "issuer_id" not in df.columns:
        return df, 0
    before = df.get_column("issuer_id").null_count()
    if before == 0 and not (also_security and df.get_column("security_id").null_count()):
        return df, 0
    df = df.with_columns(entity_id_type_expr().alias("__idt"))
    out_cols = ("issuer_id", "security_id") if also_security else ("issuer_id",)
    res = resolve_frame(df, "source_entity_id", id_type_col="__idt", min_confidence=0.9, out_cols=out_cols).drop("__idt")
    n = before - res.get_column("issuer_id").null_count()
    if also_security:
        n += df.get_column("security_id").null_count() - res.get_column("security_id").null_count()
    return res, n


def backfill_tables(settings=None) -> dict[str, int]:
    """Fill issuer_id / security_id on normalized tables written with source keys only."""
    out: dict[str, int] = {}
    out["fundamentals_raw"] = _rewrite_partitions("fundamentals_raw", _fill_issuer, settings)
    out["filings_index"] = _rewrite_partitions("filings_index", _fill_issuer, settings)

    def ins(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        return _fill_issuer(df, also_security=True)

    out["insider_transactions"] = _rewrite_partitions("insider_transactions", ins, settings)

    from app.normalize import us_symbols

    sym_map: dict[str, str] = {}
    sym_hist: list[pl.DataFrame] = []

    issuer_of: dict[str, str] = {}

    def sec_issuer_map() -> dict[str, str]:
        if not issuer_of:
            from app.core.storage import scan_table  # noqa: PLC0415

            sm = scan_table("securities_master", settings).select("security_id", "issuer_id").collect()
            issuer_of.update({a: b for a, b in zip(sm["security_id"], sm["issuer_id"], strict=True) if b})
        return issuer_of

    life_cache: list[pl.DataFrame] = []

    def price_life() -> pl.DataFrame:
        """security_id, first / last daily bar (ticker-reuse guard of si())."""
        if not life_cache:
            from app.core.storage import scan_table  # noqa: PLC0415

            try:
                # ram_fix 2026-10-31: streaming engine - the in-memory group_by over all ~99 M prices_daily rows
                # needed > 7 GB (identity_resolve peaked at 7.5 GB in the worker); streaming: 1.8 GB, 4 s
                life_cache.append(scan_table("prices_daily", settings).group_by("security_id").agg(
                    pl.col("date").min().alias("__first"), pl.col("date").max().alias("__last"))
                    .collect(engine="streaming"))
            except Exception as e:  # noqa: BLE001 - prices optional (tests)
                log.debug("price life unavailable: %s", e)
                life_cache.append(pl.DataFrame())
        return life_cache[0]

    def symbol_tables() -> tuple[dict[str, str], pl.DataFrame]:
        if not sym_hist:
            sym_map.update(us_symbols.listed_symbol_map(settings))
            sym_hist.append(us_symbols.history_map(settings))
        return sym_map, sym_hist[0]

    def si(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        """FINRA rows -> the US LISTING's security (completeness audit 2026-10-27): polygon_ticker (dot form), then the
        compact class-share / ETF / OpenFIGI symbol map, then the dated FTD ticker history. Already-resolved rows are
        re-mapped too when they point at another security of the issuer (ADR short interest was stored on the home
        listing via primary_security_id). OTC rows are never mapped to exchange listings."""
        if "security_id_resolved" not in df.columns or df.is_empty():
            return df, 0
        raw = pl.coalesce(pl.col("symbol"), pl.col("security_id").str.replace("^finra:", ""))
        work = df.with_row_index("__r").with_columns(raw.str.to_uppercase().alias("__sym"))
        otc = (pl.col("market_class") == "OTC").fill_null(False) if "market_class" in df.columns else pl.lit(False)
        work = work.with_columns(otc.alias("__otc"))
        dotted = work.get_column("__sym").str.replace_all("-", ".")
        res = lookup_many("polygon_ticker", dotted.drop_nulls().unique().to_list())
        pm = {r["input_value"]: r["security_id"] for r in res.iter_rows(named=True) if r["security_id"]}
        smap, hist = symbol_tables()
        mcol = "market_class" if "market_class" in df.columns else None
        dated = us_symbols.resolve_symbols(work, "__sym", "settlement_date", {}, hist, mcol)
        static = us_symbols.resolve_symbols(work, "__sym", "settlement_date", smap, None, mcol)
        poly = pl.Series([None if o else pm.get(d) for d, o in zip(dotted, work["__otc"], strict=True)], dtype=pl.Utf8)
        # unresolved rows: the dated FTD history first (right across ticker reuse), then today's symbol maps
        kind = pl.when(dated.is_not_null()).then(1).when(poly.is_not_null()).then(0).when(static.is_not_null()).then(2) \
            .otherwise(9)
        iss = sec_issuer_map()
        work = work.with_columns(pl.coalesce(dated, poly, static).alias("__new"), kind.alias("__kind"),
                                 pl.coalesce(poly, static).alias("__cur"))
        work = work.with_columns(pl.col("security_id").replace_strict(iss, default=None).alias("__iss_old"),
                                 pl.col("__cur").replace_strict(iss, default=None).alias("__iss_new"))
        was = pl.col("security_id_resolved").fill_null(False)
        # resolved rows: only re-pointed to another LISTING of the same issuer (ADR / class share vs home listing)
        relist = was & pl.col("__cur").is_not_null() & (pl.col("__cur") != pl.col("security_id")) \
            & (pl.col("__iss_old") == pl.col("__iss_new")).fill_null(False)
        work = work.with_columns(pl.when(was).then(pl.when(relist).then(pl.col("__cur")).otherwise(None))
                                 .otherwise(pl.col("__new")).alias("__new"))
        change = pl.col("__new").is_not_null() & (~was | (pl.col("__new") != pl.col("security_id")))
        df2 = work.with_columns(
            pl.when(change).then(pl.col("__new")).otherwise(pl.col("security_id")).alias("security_id"),
            (was | pl.col("__new").is_not_null()).alias("security_id_resolved"),
            pl.when(pl.col("__new").is_not_null()).then(pl.col("__kind")).when(was).then(0).otherwise(9).alias("__kind"))
        n = int(work.select(change.sum()).item())
        # ticker reuse: a row whose settlement lies outside the security's trading life (first / last bar +- 45 days)
        # belongs to an earlier holder of the symbol -> back to the provisional key (completeness audit 2026-10-27:
        # 2,371 rows / 85 symbols, mostly from the 2023-2024 backfill)
        life = price_life()
        if not life.is_empty():
            df2 = df2.join(life, on="security_id", how="left")
            out_life = (pl.col("security_id_resolved") & pl.col("__first").is_not_null()
                        & ((pl.col("settlement_date") < pl.col("__first") - timedelta(days=45))
                           | (pl.col("settlement_date") > pl.col("__last") + timedelta(days=45))))
            n += int(df2.select(out_life.sum()).item())
            df2 = df2.with_columns(
                pl.when(out_life).then(pl.lit("finra:") + pl.col("__sym")).otherwise(pl.col("security_id")).alias("security_id"),
                pl.when(out_life).then(False).otherwise(pl.col("security_id_resolved")).alias("security_id_resolved"),
                pl.when(out_life).then(9).otherwise(pl.col("__kind")).alias("__kind")).drop("__first", "__last")
        # several rows for one (security, date): keep the best-resolved one (exact symbol before compact / history),
        # then the latest ingested (a provisional row next to a resolved one -> the resolved one)
        df2 = (df2.sort(["__kind", "ingested_at"], descending=[True, False])
               .unique(subset=["security_id", "settlement_date"], keep="last", maintain_order=True)
               .sort("__r").drop("__r", "__sym", "__otc", "__new", "__kind", "__cur", "__iss_old", "__iss_new"))
        return df2, n


    def by_cusip(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        if "cusip" not in df.columns or df.get_column("security_id").null_count() == 0:
            return df, 0
        res = lookup_many("cusip", df.filter(pl.col("security_id").is_null()).get_column("cusip").unique().to_list())
        m = {r["input_value"]: r["security_id"] for r in res.iter_rows(named=True) if r["security_id"]}
        if "symbol" in df.columns:  # FTD rows carry the ticker as well
            sres = lookup_many("polygon_ticker", df.filter(pl.col("security_id").is_null()).get_column("symbol")
                               .drop_nulls().str.replace_all("-", ".").unique().to_list())
            sm = {r["input_value"]: r["security_id"] for r in sres.iter_rows(named=True) if r["security_id"]}
            smap, _ = symbol_tables()  # compact class-share / ETF symbols (BFB, IWM; 2026-10-27)
            fill = pl.coalesce(pl.col("cusip").replace_strict(m, default=None),
                               pl.col("symbol").str.replace_all("-", ".").replace_strict(sm, default=None),
                               pl.col("symbol").str.to_uppercase().replace_strict(smap, default=None))
        else:
            fill = pl.col("cusip").replace_strict(m, default=None)
        before = df.get_column("security_id").null_count()
        df2 = df.with_columns(pl.coalesce(pl.col("security_id"), fill).alias("security_id"))
        return df2, before - df2.get_column("security_id").null_count()

    out["institutional_holdings"] = _rewrite_partitions("institutional_holdings", by_cusip, settings)
    out["fails_to_deliver"] = _rewrite_partitions("fails_to_deliver", by_cusip, settings)
    # after fails_to_deliver: its CUSIP-resolved (symbol, date) pairs are the ticker history used by si()
    sym_hist.clear()
    out["short_interest"] = _rewrite_partitions("short_interest", si, settings)

    def news(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        if "tickers" not in df.columns:
            return df, 0
        need = df.filter((pl.col("issuer_ids").list.len().fill_null(0) == 0) & (pl.col("tickers").list.len().fill_null(0) > 0))
        if need.is_empty():
            return df, 0
        tks = need.get_column("tickers").explode().drop_nulls().str.replace_all("-", ".").unique().to_list()
        res = lookup_many("polygon_ticker", tks)
        m = {r["input_value"]: r["issuer_id"] for r in res.iter_rows(named=True)}
        filled = pl.col("tickers").list.eval(pl.element().str.replace_all("-", ".").replace_strict(m, default=None)).list.drop_nulls().list.unique()
        empty = pl.col("issuer_ids").list.len().fill_null(0) == 0
        df2 = df.with_columns(pl.when(empty).then(filled).otherwise(pl.col("issuer_ids")).alias("issuer_ids"))
        n = int((df2.get_column("issuer_ids").list.len() > 0).sum() - (df.get_column("issuer_ids").list.len().fill_null(0) > 0).sum())
        return df2, n

    out["news_items"] = _rewrite_partitions("news_items", news, settings)
    clear_cache()
    return out


def pit_anomalies(settings=None) -> dict[str, int]:
    """Count point-in-time anomalies in adapter tables (reported; dropping is done by the consumers:
    fundamentals_normalize drops raw facts with knowledge_date < period_end)."""
    from app.core.storage import scan_table

    out: dict[str, int] = {}
    checks = (
        ("fundamentals_raw", pl.col("knowledge_date") < pl.col("period_end")),
        ("insider_transactions", pl.col("transaction_date") > pl.col("knowledge_date")),
        ("filings_index", pl.col("period_end") > pl.col("knowledge_date")),
    )
    for t, cond in checks:
        try:
            out[t] = int(scan_table(t, settings).filter(cond).select(pl.len()).collect().item())
        except Exception:  # noqa: BLE001
            out[t] = -1
    return out


def unresolved_ciks(settings=None) -> list[str]:
    from app.core.storage import scan_table

    ids: set[str] = set()
    for t in ("fundamentals_raw", "filings_index", "insider_transactions"):
        try:
            df = (scan_table(t, settings).filter(pl.col("issuer_id").is_null() & (pl.col("source_id").is_in(["sec_edgar", "sec"])))
                  .select("source_entity_id").unique().collect())
            ids |= {normalize_id("cik", x) for x in df.get_column("source_entity_id").to_list() if x}
        except Exception:  # noqa: BLE001
            continue
    known = lookup_many("cik", sorted(i for i in ids if i))
    return sorted(set(i for i in ids if i) - set(known.get_column("id_value").to_list()))


def run(ctx: Any) -> Any:
    """identity_resolve: successions -> CUSIP harvest -> back-fill ids -> PIT anomaly report."""
    from app.normalize.identity_universe import registry_dir
    from app.scheduler.base import JobResult

    s = ctx.settings
    rows_written: dict[str, int] = {}
    msgs: list[str] = []
    # 1) CIK successions (seeded ones are applied by ref_universe; here: auto-detection for unknown CIKs)
    unk = unresolved_ciks(s)
    links = detect_successions(unk, settings=s, budget=int(ctx.params.get("succession_budget", 300))) if unk else []
    if links:
        n = merge_identifier_rows([{"id_type": "cik", "id_value": x["old_cik"], "entity_level": "issuer", "issuer_id": x["issuer_id"],
                                    "source_id": f"sec_succession:{x['reason']}", "confidence": 0.95} for x in links], settings=s)
        reg = Registry(s)
        for x in links:
            reg.keys[f"cik:{x['old_cik']}"] = x["issuer_id"]
        reg.save(date.today())
        p = registry_dir(s) / "successions_detected.csv"
        df = pl.DataFrame(links).with_columns(pl.lit(date.today().isoformat()).alias("detected_on"))
        if p.exists():
            df = pl.concat([pl.read_csv(p, infer_schema_length=0), df.cast(pl.Utf8)], how="diagonal_relaxed").unique(subset=["old_cik"], keep="last")
        df.cast(pl.Utf8).write_csv(p)
        rows_written["identifier_map"] = n
        msgs.append(f"successions linked={len(links)} of unknown_ciks={len(unk)}")
    # 2) CUSIPs from fails-to-deliver
    cus = harvest_ftd_cusips(s)
    if cus:
        rows_written["identifier_map"] = rows_written.get("identifier_map", 0) + merge_identifier_rows(cus, settings=s)
        msgs.append(f"cusips={len(cus)}")
    # 2b) US/CA ISINs derived from CUSIPs where no registry ISIN exists
    der = derive_isins_from_cusips(s)
    if der:
        rows_written["identifier_map"] = rows_written.get("identifier_map", 0) + merge_identifier_rows(der, settings=s)
        msgs.append(f"isins_derived={len(der)}")
    # 2c) ADR ratios from seeds/adr_ratios.csv (also applied by ref_universe; patched here so a seed edit takes
    #     effect without a full reference rebuild)
    n_adr = _rewrite_partitions("securities_master", lambda df: apply_adr_ratio_seed(df, s), s)
    if n_adr:
        rows_written["securities_master"] = n_adr
        msgs.append(f"adr_ratios_set={n_adr}")
    # 2d) SIC -> taxonomy for issuers with insider activity and no sector (SEC submissions, 8 req/s)
    if ctx.params.get("sic_backfill", True):
        n_sic, n_cls = backfill_sic_taxonomy(s, budget=int(ctx.params.get("sic_budget", 3000)),
                                             scope=str(ctx.params.get("sic_scope", "all")))
        if n_sic:
            rows_written["issuers"] = n_sic
            msgs.append(f"sic_backfilled={n_sic} classified={n_cls}")
    # 3) back-fills
    bf = backfill_tables(s)
    for k, v in bf.items():
        if v:
            rows_written[k] = v
    # 4) PIT anomalies (report only)
    an = pit_anomalies(s)
    msgs.append("pit_anomalies " + ", ".join(f"{k}={v}" for k, v in an.items()))
    ctx.log.info("identity_resolve: %s; backfilled=%s", "; ".join(msgs), bf)
    return JobResult(status="success", rows_written=rows_written, entities_ok=sum(bf.values()), message="; ".join(msgs)[:2000])
