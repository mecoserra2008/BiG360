"""E1 dictionaries (`event_labels`) and the extended organisation alias table.

Dictionaries
* static: country (GLOBAL = 0 + ISO2), sector (bit), theme (bit), gkg_theme (GKG theme -> theme / sector masks),
  model;
* stable, append-only: issuer (int32 id -> issuer_id, assigned in issuer_id order on first sync), domain, lang,
  source. New codes are assigned under a file lock after re-reading the table, so the nightly job and a detached
  backfill can both add domains safely.

Alias table (`build_alias_table`): every active issuer in `full` (issuers.name / short_name / aliases, identifier_map
gdelt_org_name, SSE / SZSE English names for A-shares (cn_code), yfinance profile long/short names, curated
aliases for the largest KR / CN / JP names whose master names are local-script only) normalised with `norm_org_expr`
(the same function is applied to GKG V1Organizations), with a base confidence:
  legal name multi-word 0.95 / alias multi-word 0.9 / curated or identifier_map 0.92;
  single-word names 0.8 (>= 7 chars) / 0.6, aliases 0.75 / 0.55 (below 0.5 after the context rules = dropped);
  ambiguous brand words (app.nlp.gdelt_link.AMBIGUOUS) keep their curated low base and need title context.
Dropped: aliases shorter than 4 chars (except a whitelist), generic business words, country / demonym / big-city names,
and aliases shared by several unrelated issuers (A+H dual listings, i.e. CN + HK issuers with the same name, are
kept and linked to both).
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, table_lock, write_table
from app.events_v2 import taxonomy as tx
from app.nlp.common import has_data

log = get_logger(__name__)

DICT_TYPES = ("country", "sector", "theme", "issuer", "domain", "lang", "source", "gkg_theme", "model")
SOURCES = ["gdelt_gkg", "gdelt_events", "polygon_news", "gdelt_doc", "rss_fed", "rss_ecb", "rss_sec", "yfinance_news",
           "sec_8k", "rss_other"]  # bit order of news_stories.source_mask (append only, < 16)
MODELS = ["ProsusAI/finbert", "cardiffnlp/twitter-xlm-roberta-base-sentiment", "provider"]

_SUFFIX_WORDS = (
    "inc incorporated corp corporation co company cos ltd limited plc llc llp lp sa ag nv se asa ab spa sab cv de "
    "holdings holding group the oyj abp publ tbk bhd berhad pcl kk gmbh bv kgaa sca sapa ptc pte sarl srl as aps "
    "ord adr ads reit trust class a b com"
).split()
_SUFFIX_MULTI = ["s a b de c v", "s a de c v", "s p a", "s a", "n v", "a g", "a s", "s e", "k k", "p l c", "l p", "s r l",
                 "b v", "co ltd", "pte ltd"]
_SUFFIX_RE = r"(?:\s(?:" + "|".join(sorted(set(_SUFFIX_WORDS) | set(_SUFFIX_MULTI), key=len, reverse=True)) + r"))+$"
_MIN_LEN_WHITELIST = {"bp", "sap", "ibm", "amd", "edp", "gsk", "bhp", "aia", "ing", "hsbc", "ubs", "bmw", "lg", "sk",
                      "kia", "tsmc", "byd", "jd", "nio", "eni", "abb", "rwe", "eon", "dhl", "ups", "hp", "3m", "kt",
                      "att", "csx", "axa", "bnp", "ana", "jal", "tdk", "nec", "kddi", "jfe", "mufg", "smbc", "cnooc",
                      "icbc", "ccb", "boc", "bbva", "ntt", "uob", "dbs", "ocbc", "cba", "anz", "nab", "rbc", "td",
                      "bmo", "cibc", "omv", "kpn", "sse", "bae", "iag", "arm", "aig", "gm", "ge", "hca", "cvs"}

# generic words / places that are never a safe single-word company alias
_COMMON = set("""
american national general united international global first capital energy resources group holdings industries
pacific atlantic western eastern southern northern central royal new first union security financial bank trust
technology technologies systems solutions services service partners investment investments properties property realty
development developments media health healthcare medical pharma pharmaceuticals bio biotech green power electric
mining gold silver oil gas petroleum steel metals chemical chemicals foods food water air sea star sun moon one two
three five ten alpha beta delta omega summit pinnacle frontier harmony focus edge nexus matrix vision future prime
premier smart digital data cloud network networks software electronics energy motors motor auto automotive
insurance life assurance commerce trade trading logistics transport shipping airlines airways railway rail
construction engineering infrastructure cement paper textile textiles apparel fashion retail stores store market
markets exchange stock stocks fund funds asset assets management advisors consulting education entertainment games
sports hotel hotels resorts travel tourism agriculture farm farms dairy beverage beverages brewery wine coffee tea
house home homes land lands city state government ministry police army navy court parliament council university
college school hospital church news times post daily journal herald tribune today express world people life
light fire ice stone rock river lake mountain ocean island bridge tower park garden
""".split())


def _place_names() -> set[str]:
    out: set[str] = set()
    try:
        import pycountry  # noqa: PLC0415

        for c in pycountry.countries:
            for n in (c.name, getattr(c, "common_name", None), getattr(c, "official_name", None)):
                if n:
                    out.add(_py_norm(n))
    except Exception:  # pragma: no cover
        pass
    out |= {_py_norm(x) for x in (
        "america us usa uk britain england europe asia africa china korea japan india russia germany france italy spain "
        "canada mexico brazil taiwan hong kong singapore beijing shanghai shenzhen tokyo osaka seoul london paris berlin "
        "new york washington texas california florida chicago boston sydney toronto mumbai delhi dubai moscow istanbul "
        "american chinese korean japanese indian russian german french italian spanish canadian mexican brazilian "
        "european asian african british english taiwanese".split())}
    return out


# curated English aliases for large KR / CN / JP issuers whose master names are local-script only
# (matched to issuers by yf_ticker / ticker through identifier_map)
CURATED: dict[str, list[str]] = {
    "005930.KS": ["samsung electronics", "samsung"], "000660.KS": ["sk hynix", "hynix"], "005380.KS": ["hyundai motor"],
    "000270.KS": ["kia", "kia motors"], "373220.KS": ["lg energy solution"], "051910.KS": ["lg chem"],
    "006400.KS": ["samsung sdi"], "207940.KS": ["samsung biologics"], "005490.KS": ["posco", "posco holdings"],
    "035420.KS": ["naver"], "035720.KS": ["kakao"], "068270.KS": ["celltrion"], "105560.KS": ["kb financial"],
    "055550.KS": ["shinhan financial", "shinhan"], "012330.KS": ["hyundai mobis"], "066570.KS": ["lg electronics"],
    "028260.KS": ["samsung c t"], "015760.KS": ["korea electric power", "kepco"], "034730.KS": ["sk inc"],
    "017670.KS": ["sk telecom"], "030200.KS": ["kt corp"], "003550.KS": ["lg corp"], "096770.KS": ["sk innovation"],
    "009540.KS": ["hd korea shipbuilding", "korea shipbuilding"], "329180.KS": ["hd hyundai heavy industries", "hyundai heavy industries"],
    "010130.KS": ["korea zinc"], "032830.KS": ["samsung life"], "086790.KS": ["hana financial"], "316140.KS": ["woori financial"],
    "012450.KS": ["hanwha aerospace"], "000810.KS": ["samsung fire marine"], "018260.KS": ["samsung sds"],
    "011200.KS": ["hmm"], "003670.KS": ["posco future m"], "247540.KQ": ["ecopro bm"], "086520.KQ": ["ecopro"],
    "600519.SS": ["kweichow moutai", "moutai"], "300750.SZ": ["catl", "contemporary amperex", "contemporary amperex technology"],
    "002594.SZ": ["byd"], "601318.SS": ["ping an insurance", "ping an"], "600036.SS": ["china merchants bank"],
    "000858.SZ": ["wuliangye"], "000333.SZ": ["midea", "midea group"], "000651.SZ": ["gree electric", "gree"],
    "601012.SS": ["longi", "longi green energy"], "601899.SS": ["zijin mining", "zijin"], "601857.SS": ["petrochina"],
    "600028.SS": ["sinopec"], "601398.SS": ["icbc", "industrial and commercial bank of china"],
    "601288.SS": ["agricultural bank of china"], "601988.SS": ["bank of china"], "601939.SS": ["china construction bank"],
    "600900.SS": ["china yangtze power"], "601088.SS": ["china shenhua"], "600030.SS": ["citic securities"],
    "002415.SZ": ["hikvision"], "300059.SZ": ["east money"], "002475.SZ": ["luxshare"], "601628.SS": ["china life insurance"],
    "600276.SS": ["hengrui", "jiangsu hengrui"], "603259.SS": ["wuxi apptec"], "300760.SZ": ["mindray"],
    "601166.SS": ["industrial bank"], "000001.SZ": ["ping an bank"], "002230.SZ": ["iflytek"], "688981.SS": ["smic"],
    "601138.SS": ["foxconn industrial internet"], "600104.SS": ["saic motor", "saic"], "601633.SS": ["great wall motor"],
    "000002.SZ": ["china vanke", "vanke"], "600309.SS": ["wanhua chemical"], "002714.SZ": ["muyuan"],
    "601919.SS": ["cosco shipping"], "600050.SS": ["china unicom"], "601728.SS": ["china telecom"],
    "600941.SS": ["china mobile"], "601390.SS": ["china railway group"], "601668.SS": ["china state construction"],
    "7203.T": ["toyota", "toyota motor"], "6758.T": ["sony"], "9984.T": ["softbank", "softbank group"],
    "8306.T": ["mitsubishi ufj", "mufg"], "6861.T": ["keyence"], "9432.T": ["ntt", "nippon telegraph"],
    "7974.T": ["nintendo"], "6501.T": ["hitachi"], "8035.T": ["tokyo electron"], "9983.T": ["fast retailing", "uniqlo"],
    "7267.T": ["honda", "honda motor"], "7201.T": ["nissan", "nissan motor"], "6902.T": ["denso"],
    "4502.T": ["takeda", "takeda pharmaceutical"], "8316.T": ["sumitomo mitsui financial", "smfg"],
    "8058.T": ["mitsubishi corp"], "8031.T": ["mitsui co"], "6098.T": ["recruit holdings"], "6367.T": ["daikin"],
    "4063.T": ["shin etsu", "shin etsu chemical"], "6594.T": ["nidec"], "6752.T": ["panasonic"], "6702.T": ["fujitsu"],
    "7751.T": ["canon"], "6503.T": ["mitsubishi electric"], "5401.T": ["nippon steel"], "9201.T": ["japan airlines"],
    "9202.T": ["ana holdings", "all nippon airways"], "7011.T": ["mitsubishi heavy industries"], "8411.T": ["mizuho"],
}


EXTRA_US: dict[str, list[str]] = {
    "AMZN": ["amazon", "amazon com", "amazon web services"], "WMT": ["walmart"], "DIS": ["disney", "walt disney"],
    "NFLX": ["netflix"], "BA": ["boeing"], "JPM": ["jpmorgan", "jp morgan", "jpmorgan chase"], "GS": ["goldman sachs"],
    "MS": ["morgan stanley"], "BAC": ["bank of america"], "C": ["citigroup", "citi"], "WFC": ["wells fargo"],
    "PFE": ["pfizer"], "MRK": ["merck"], "LLY": ["eli lilly"], "UNH": ["unitedhealth"], "CVX": ["chevron"],
    "INTC": ["intel"], "CSCO": ["cisco"], "ORCL": ["oracle"], "CRM": ["salesforce"], "ADBE": ["adobe"],
    "QCOM": ["qualcomm"], "AVGO": ["broadcom"], "MU": ["micron"], "UBER": ["uber"], "ABNB": ["airbnb"],
    "PYPL": ["paypal"], "V": ["visa"], "MA": ["mastercard"], "NKE": ["nike"], "SBUX": ["starbucks"],
    "F": ["ford", "ford motor"], "GM": ["general motors"], "CAT": ["caterpillar"], "LMT": ["lockheed martin"],
    "RTX": ["raytheon", "rtx"], "T": ["at t"], "VZ": ["verizon"], "TMUS": ["t mobile"], "COST": ["costco"],
    "TGT": ["target"], "PEP": ["pepsico"], "MDLZ": ["mondelez"], "BLK": ["blackrock"], "SCHW": ["charles schwab"],
}


def _py_norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^the ", "", s)
    s = re.sub(r"^pt ", "", s)
    s = re.sub(_SUFFIX_RE, "", s)
    return s.strip()


def norm_org_expr(col: pl.Expr) -> pl.Expr:
    """Organisation name normaliser shared by the alias table and GKG V1Organizations (accent fold, lower, punctuation
    -> space, leading 'the' / 'pt', trailing legal-form words)."""
    e = col.str.normalize("NFKD").str.replace_all(r"\p{Mn}", "").str.to_lowercase()
    e = e.str.replace_all(r"[^a-z0-9 ]", " ").str.replace_all(r"\s+", " ").str.strip_chars()
    e = e.str.replace(r"^the ", "").str.replace(r"^pt ", "").str.replace(_SUFFIX_RE, "")
    return e.str.strip_chars()


def norm_org(s: str) -> str:
    return _py_norm(s)


# ================================================================================================ dictionary table
def _now() -> datetime:
    return datetime.now(UTC)


def static_rows() -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(tx.COUNTRIES):
        rows.append({"dict_type": "country", "id": i, "code": c, "name": tx.country_name(c), "grp": None,
                     "theme_mask": None, "sector_mask": None, "extra": None})
    for i, (sid, name) in enumerate(tx.SECTORS):
        rows.append({"dict_type": "sector", "id": i, "code": sid, "name": name, "grp": None, "theme_mask": None,
                     "sector_mask": 1 << i, "extra": None})
    for t in tx.THEMES:
        rows.append({"dict_type": "theme", "id": t.bit, "code": t.id, "name": t.name, "grp": t.group,
                     "theme_mask": 1 << t.bit, "sector_mask": tx.sector_mask(t.sectors),
                     "extra": orjson.dumps({"gkg": list(t.gkg), "gkg_prefix": list(t.gkg_prefix), "cameo": list(t.cameo),
                                            "title": t.title, "global_theme": t.global_theme}).decode()})
    gk: dict[str, list[int]] = {}
    for t in tx.THEMES:
        for g in t.gkg:
            gk.setdefault(g, [0, 0])[0] |= 1 << t.bit
        for g in t.gkg_prefix:
            gk.setdefault(g + "*", [0, 0])[0] |= 1 << t.bit
    for g, secs in tx.GKG_SECTOR.items():
        gk.setdefault(g, [0, 0])[1] |= tx.sector_mask(secs)
    for i, (g, (tm, sm)) in enumerate(sorted(gk.items())):
        rows.append({"dict_type": "gkg_theme", "id": i, "code": g, "name": None, "grp": None, "theme_mask": tm,
                     "sector_mask": sm, "extra": None})
    for i, m in enumerate(MODELS):
        rows.append({"dict_type": "model", "id": i, "code": m, "name": m, "grp": None, "theme_mask": None,
                     "sector_mask": None, "extra": None})
    return _frame(rows)


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    schema = {"dict_type": pl.Utf8, "id": pl.Int32, "code": pl.Utf8, "name": pl.Utf8, "grp": pl.Utf8,
              "theme_mask": pl.Int64, "sector_mask": pl.Int16, "extra": pl.Utf8}
    df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    return df.with_columns(pl.lit(_now()).alias("updated_at"))


def load_dict(dict_type: str, settings: Settings | None = None) -> pl.DataFrame:
    if not has_data("event_labels", settings):
        return _frame([]).head(0)
    return scan_table("event_labels", settings).filter(pl.col("dict_type") == dict_type).collect()


def write_dict(df: pl.DataFrame, settings: Settings | None = None) -> int:
    return write_table(df, "event_labels", mode="overwrite_partition", settings=settings)


def ensure_static(settings: Settings | None = None) -> None:
    """(Re)write the static dictionaries (country / sector / theme / gkg_theme / model) - idempotent."""
    s = settings or get_settings()
    with table_lock("event_labels_assign", s):
        write_dict(static_rows(), s)
        cur = load_dict("source", s)
        if not cur.height:  # fresh: ids = SOURCES index (= bit in news_stories.source_mask)
            write_dict(_frame([{"dict_type": "source", "id": i, "code": c, "name": c, "grp": None, "theme_mask": None,
                                "sector_mask": None, "extra": None} for i, c in enumerate(SOURCES)]), s)


def assign_ids(dict_type: str, codes: list[str], settings: Settings | None = None,
               names: dict[str, str] | None = None) -> dict[str, int]:
    """Stable append-only ids for dynamic dictionaries (domain / lang / source). Thread/process safe (file lock)."""
    s = settings or get_settings()
    codes = sorted({c for c in codes if c})
    cur = load_dict(dict_type, s)
    m = dict(zip(cur.get_column("code").to_list(), cur.get_column("id").to_list(), strict=True)) if cur.height else {}
    missing = [c for c in codes if c not in m]
    if not missing:
        return m
    with table_lock("event_labels_assign", s):
        cur = load_dict(dict_type, s)
        m = dict(zip(cur.get_column("code").to_list(), cur.get_column("id").to_list(), strict=True)) if cur.height else {}
        missing = [c for c in codes if c not in m]
        if missing:
            start = (max(m.values()) + 1) if m else (1 if dict_type in ("domain", "lang") else 0)
            if dict_type == "lang" and start > 127 or dict_type == "source" and start + len(missing) > 15:
                log.warning("event_labels %s dictionary overflow; extra codes mapped to null", dict_type)
                missing = missing[: max(0, (127 if dict_type == 'lang' else 15) - start)]
            new = [{"dict_type": dict_type, "id": start + k, "code": c, "name": (names or {}).get(c, c), "grp": None,
                    "theme_mask": None, "sector_mask": None, "extra": None} for k, c in enumerate(missing)]
            write_dict(pl.concat([cur, _frame(new)], how="vertical_relaxed") if cur.height else _frame(new), s)
            m.update({r["code"]: r["id"] for r in new})
    return m


# ================================================================================================ issuers
def _issuer_meta(s: Settings) -> pl.DataFrame:
    from app.nlp.common import security_map  # noqa: PLC0415

    iss = scan_table("issuers", s).select("issuer_id", "name", "short_name", "aliases", "country", "region", "sector_id",
                                          "is_active", "updated_at").collect()
    iss = iss.sort("updated_at", descending=True, nulls_last=True).unique("issuer_id", keep="first")
    sm = security_map(s).select("issuer_id", "security_id", "ticker")
    return iss.join(sm, on="issuer_id", how="left")


def sync_issuers(settings: Settings | None = None) -> pl.DataFrame:
    """Append ids for issuers not yet in the issuer dictionary; refresh names / sector masks. Returns the dictionary."""
    s = settings or get_settings()
    if not has_data("issuers", s):
        return load_dict("issuer", s)
    meta = _issuer_meta(s)
    with table_lock("event_labels_assign", s):
        cur = load_dict("issuer", s)
        ids = dict(zip(cur.get_column("code").to_list(), cur.get_column("id").to_list(), strict=True)) if cur.height else {}
        nxt = (max(ids.values()) + 1) if ids else 1
        rows = []
        for r in meta.sort("issuer_id").iter_rows(named=True):
            iid = r["issuer_id"]
            if iid not in ids:
                ids[iid] = nxt
                nxt += 1
            rows.append({"dict_type": "issuer", "id": ids[iid], "code": iid, "name": r["name"], "grp": r["country"],
                         "theme_mask": None, "sector_mask": tx.sector_mask([r["sector_id"]] if r["sector_id"] else []),
                         "extra": orjson.dumps({"ticker": r["ticker"], "security_id": r["security_id"],
                                                "region": r["region"], "short_name": r["short_name"]}).decode()})
        known = {r["code"] for r in rows}
        for r in cur.iter_rows(named=True) if cur.height else []:
            if r["code"] not in known:  # issuer vanished from reference data: keep its id (never renumber)
                rows.append({k: r[k] for k in ("dict_type", "id", "code", "name", "grp", "theme_mask", "sector_mask",
                                               "extra")})
        df = _frame(rows)
        write_dict(df, s)
    return df


# ================================================================================================ alias table
def _cn_english_names(s: Settings) -> list[tuple[str, str]]:
    """(cn_code 'SSE:600000' | 'SZSE:000001', english name) from the SSE / SZSE stock lists (latest snapshot)."""
    out: list[tuple[str, str]] = []
    sse = sorted((s.raw_dir / "sse" / "ref_stock_list").glob("dt=*/sse_list.json.gz"))
    if sse:
        try:
            for r in json.loads(gzip.open(sse[-1]).read()):
                code = r.get("A_STOCK_CODE") or r.get("COMPANY_CODE")
                for k in ("FULL_NAME_IN_ENGLISH", "COMPANY_ABBR_EN"):
                    if code and r.get(k) and r[k] != "-":
                        out.append((f"SSE:{code}", r[k]))
        except Exception as e:  # noqa: BLE001
            log.warning("sse list unreadable: %s", e)
    szse = sorted((s.raw_dir / "szse" / "ref_stock_list").glob("dt=*/szse_list.xlsx"))
    if szse:
        try:
            import warnings  # noqa: PLC0415

            import openpyxl  # noqa: PLC0415

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ws = openpyxl.load_workbook(szse[-1]).active
            rows = list(ws.iter_rows(values_only=True))
            hdr = [str(h or "").strip() for h in rows[0]]
            ci, ni = hdr.index("A股代码"), hdr.index("英文名称")
            for r in rows[1:]:
                if r[ci] and r[ni]:
                    out.append((f"SZSE:{str(r[ci]).zfill(6)}", str(r[ni])))
        except Exception as e:  # noqa: BLE001
            log.warning("szse list unreadable: %s", e)
    return out


def _profile_names(s: Settings) -> list[tuple[str, str]]:
    p = s.raw_dir / "yfinance_fund" / "latest" / "profiles.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [(t, n) for t, v in d.items() if isinstance(v, dict) for n in (v.get("longName"), v.get("shortName")) if n]


def alias_rows(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Raw alias candidates [org, issuer_id, kind] (kind: legal | short | alias | gdelt | curated | listname)."""
    s = settings or get_settings()
    rows: list[dict[str, Any]] = []
    if not has_data("issuers", s):
        return rows
    meta = _issuer_meta(s)
    for r in meta.iter_rows(named=True):
        if r["is_active"] is False:
            continue
        iid = r["issuer_id"]
        if r["name"]:
            rows.append({"org": r["name"], "issuer_id": iid, "kind": "legal"})
        if r["short_name"]:
            rows.append({"org": r["short_name"], "issuer_id": iid, "kind": "short"})
        for a in r["aliases"] or []:
            if a:
                rows.append({"org": a, "issuer_id": iid, "kind": "alias"})
    if has_data("identifier_map", s):
        im = scan_table("identifier_map", s).filter(pl.col("id_type").is_in(["gdelt_org_name", "cn_code", "yf_ticker"])) \
            .select("id_type", "id_value", "issuer_id").collect().filter(pl.col("issuer_id").is_not_null())
        rows += [{"org": v, "issuer_id": i, "kind": "gdelt"} for t, v, i in im.iter_rows() if t == "gdelt_org_name"]
        cn = {v: i for t, v, i in im.iter_rows() if t == "cn_code"}
        yf = {v: i for t, v, i in im.iter_rows() if t == "yf_ticker"}
        rows += [{"org": n, "issuer_id": cn[c], "kind": "listname"} for c, n in _cn_english_names(s) if c in cn]
        rows += [{"org": n, "issuer_id": yf[t], "kind": "listname"} for t, n in _profile_names(s) if t in yf]
        from app.ingestion.gdelt_gkg import SEED_ALIASES  # noqa: PLC0415 - curated aliases of the seed universe

        for t, names in [*CURATED.items(), *SEED_ALIASES.items(), *EXTRA_US.items()]:
            iid = yf.get(t)
            if iid:
                rows += [{"org": n, "issuer_id": iid, "kind": "curated"} for n in names]
    return rows


_BASE = {"legal": (0.95, 0.8, 0.6), "short": (0.9, 0.75, 0.55), "alias": (0.9, 0.75, 0.55),
         "listname": (0.9, 0.75, 0.55), "gdelt": (0.92, 0.85, 0.7), "curated": (0.95, 0.9, 0.85)}


def build_alias_table(settings: Settings | None = None, rows: list[dict[str, Any]] | None = None,
                      issuer_dict: pl.DataFrame | None = None) -> pl.DataFrame:
    """[org_norm, issuer_ids list[int32], conf float64, ambiguous bool] - one row per normalised alias."""
    from app.nlp.gdelt_link import AMBIGUOUS  # noqa: PLC0415

    s = settings or get_settings()
    rows = rows if rows is not None else alias_rows(s)
    schema = {"org": pl.Utf8, "issuer_id": pl.Utf8, "kind": pl.Utf8}
    df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    idict = issuer_dict if issuer_dict is not None else load_dict("issuer", s)
    if df.is_empty() or idict.is_empty():
        return pl.DataFrame(schema={"org_norm": pl.Utf8, "issuer_ids": pl.List(pl.Int32), "conf": pl.Float64,
                                    "ambiguous": pl.Boolean})
    meta = idict.select(pl.col("code").alias("issuer_id"), pl.col("id").alias("iid"), pl.col("grp").alias("country"),
                        pl.col("extra").str.json_path_match("$.region").alias("region"))
    df = df.with_columns(norm_org_expr(pl.col("org")).alias("org_norm")).join(meta, on="issuer_id", how="inner")
    places = _place_names()
    df = df.filter((pl.col("org_norm").str.len_chars() >= 4) | pl.col("org_norm").is_in(list(_MIN_LEN_WHITELIST)))
    df = df.filter(~pl.col("org_norm").is_in(list(places)))
    single = ~pl.col("org_norm").str.contains(" ")
    df = df.filter(~(single & pl.col("org_norm").is_in(list(_COMMON))))
    # base confidence by kind / word count / length (best kind wins per alias x issuer)

    df = df.with_columns(
        pl.when(~single).then(pl.col("kind").replace_strict({k: v[0] for k, v in _BASE.items()}, default=0.9))
        .when(pl.col("org_norm").str.len_chars() >= 7).then(pl.col("kind").replace_strict({k: v[1] for k, v in _BASE.items()}, default=0.75))
        .otherwise(pl.col("kind").replace_strict({k: v[2] for k, v in _BASE.items()}, default=0.55)).alias("conf"),
        pl.col("kind").replace_strict(_KIND_PRIO, default=9).alias("prio"))
    df = df.group_by("org_norm", "iid").agg(pl.col("conf").max(), pl.col("prio").min(), pl.col("region").first(),
                                            pl.col("kind").unique().alias("kinds"))
    # an alias shared by several issuers goes to the issuer(s) of its strongest source (curated > gdelt > legal >
    # short/alias > exchange list names); a tie keeps A+H pairs (CN + HK regions), otherwise the alias is dropped
    df = df.filter(pl.col("prio") == pl.col("prio").min().over("org_norm"))
    g = df.group_by("org_norm").agg(pl.col("iid").sort().alias("issuer_ids"), pl.col("conf").min(),
                                    pl.col("region").unique().alias("regions"), pl.len().alias("n"),
                                    pl.col("kinds").flatten().unique().alias("kinds"))
    ah = (pl.col("n") <= 3) & pl.col("regions").list.eval(pl.element().is_in(["CN", "HK"])).list.all()
    g = g.filter((pl.col("n") == 1) | ah)
    # single English dictionary words (bert vocab) need headline context unless curated / identifier_map names
    common = _common_words()
    trusted = pl.col("kinds").list.eval(pl.element().is_in(["gdelt", "curated"])).list.any()
    # ... and so do multi-word aliases made only of dictionary words ("industrial bank", "united airlines")
    all_common = pl.col("org_norm").str.split(" ").list.eval(pl.element().is_in(list(common | _COMMON))).list.all()
    word = all_common & ~trusted
    amb = dict(AMBIGUOUS)
    g = g.with_columns(
        (pl.col("org_norm").is_in(list(amb)) | word).alias("ambiguous"),
        pl.when(pl.col("org_norm").is_in(list(amb)))
        .then(pl.min_horizontal(pl.col("conf"), pl.col("org_norm").replace_strict(amb, default=1.0, return_dtype=pl.Float64)))
        .when(word).then(pl.min_horizontal(pl.col("conf"), pl.lit(0.45)))
        .otherwise(pl.col("conf")).alias("conf"))
    return g.select("org_norm", pl.col("issuer_ids").cast(pl.List(pl.Int32)), "conf", "ambiguous").sort("org_norm")


_KIND_PRIO = {"curated": 0, "gdelt": 1, "legal": 2, "short": 3, "alias": 3, "listname": 4}


@lru_cache(maxsize=1)
def _common_words() -> frozenset[str]:
    p = Path(__file__).with_name("common_words.txt")
    if not p.exists():
        return frozenset()
    return frozenset(w.strip() for w in p.read_text(encoding="utf-8").splitlines() if w and not w.startswith("#"))


def save_alias_table(df: pl.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path, compression="zstd")
    return path
