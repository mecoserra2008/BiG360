"""Sector / subsector classification per contracts/sector_taxonomy.yaml. Owner: ref-identity.

Public API
    load_taxonomy() -> dict                      parsed + indexed contract (cached)
    classify(issuer: dict, extra: dict | None) -> (sector_id, subsector_id, classification_source)
    classify_issuers(issuers_df, extra_by_issuer, settings=None) -> (issuers_df with sector columns, coverage dict)
    subsector_groups(subsector_id) -> list[str]   business-model group tags (banks, insurers, reits, ...)
    sector_of(subsector_id) -> sector_id

Precedence (contract): override > yf_industry > sic > tse33 > ksic > csrc > cvm > twse > unknown.
  * override: contract `overrides` (keys cik:/ticker:SYM@MIC/isin:) and seeds/manual_overrides.csv (kind=subsector).
  * yf_industry: yfinance `info['industry']` normalized (em/en dash -> ' - ', collapsed spaces).
  * sic: SEC SIC ranges, first match top-down. SIC 6770 (blank checks) -> excluded (sector null).
  * tse33 / ksic / csrc / cvm: national codes. KSIC also accepts the KRX KIND Korean industry *name* (keyword map
    below) when the numeric DART code is unavailable. CSRC accepts 'C39', 'C', or SZSE text 'J 金融业'.
  * size rule: banks_regional -> banks_global when total_assets_usd > 500e9 or the issuer is a G-SIB.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from app.core.config import get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

# FSB G-SIB list (2025 vintage) — normalized name fragments
GSIB_NAMES = (
    "JPMORGAN", "CITIGROUP", "HSBC", "BANK OF AMERICA", "BANK OF CHINA", "BARCLAYS", "BNP PARIBAS", "DEUTSCHE BANK",
    "GOLDMAN SACHS", "INDUSTRIAL AND COMMERCIAL BANK OF CHINA", "MITSUBISHI UFJ", "UBS", "AGRICULTURAL BANK OF CHINA",
    "CHINA CONSTRUCTION BANK", "WELLS FARGO", "MIZUHO", "MORGAN STANLEY", "BANCO SANTANDER", "SOCIETE GENERALE",
    "STANDARD CHARTERED", "STATE STREET", "SUMITOMO MITSUI FINANCIAL", "TORONTO DOMINION", "BANK OF NEW YORK MELLON",
    "CREDIT AGRICOLE", "BPCE", "ING GROEP", "ROYAL BANK OF CANADA", "BANK OF COMMUNICATIONS",
)

# KRX KIND industry names (KSIC class names in Korean) -> subsector; first regex match wins
KSIC_NAME_RULES: tuple[tuple[str, str], ...] = (
    (r"반도체", "semiconductors"),
    (r"전자부품|인쇄회로|표시장치", "electronic_components"),
    (r"통신 및 방송 장비|컴퓨터 및 주변", "hardware_networking"),
    (r"영상 및 음향기기|가정용 기기", "household_durables_leisure"),
    (r"전지", "electrical_equipment"),
    (r"자동차 신품 부품|자동차 부품|차체", "auto_parts"),
    (r"자동차", "autos"),
    (r"선박|조선", "industrial_machinery"),
    (r"항공기|우주선", "aerospace_defense"),
    (r"의약품|의약물질|생물학적", "pharma"),
    (r"의료용 기기|의료기기", "medtech"),
    (r"연구개발", "biotech"),
    (r"화학|합성고무|플라스틱", "chemicals_diversified"),
    (r"철강|1차 금속", "steel_aluminum"),
    (r"비철금속", "metals_mining"),
    (r"석유 정제|석유정제", "refining_marketing"),
    (r"전기업|발전업|전력", "electric_utilities"),
    (r"가스", "gas_utilities"),
    (r"은행", "banks_regional"),
    (r"보험", "insurance_life_health"),
    (r"증권|금융 지원|투자", "capital_markets_exchanges"),
    (r"지주회사|회사 본부|기타 금융", "diversified_holdings"),
    (r"포털|인터넷 정보|자료처리|호스팅", "internet_platforms"),
    (r"게임", "video_games"),
    (r"소프트웨어", "software_application"),
    (r"컴퓨터 프로그래밍|시스템 통합", "it_services"),
    (r"전기통신|통신업", "telecom_operators"),
    (r"방송|영화|오디오", "media_entertainment"),
    (r"건설|토목", "construction_building"),
    (r"도매|상품 중개", "trading_distributors"),
    (r"소매|백화점", "specialty_retail"),
    (r"식료품|식품|음료", "packaged_food"),
    (r"담배", "tobacco"),
    (r"화장품|세제", "household_personal_products"),
    (r"의복|섬유|신발|가방", "apparel_luxury"),
    (r"해상 운송|철도|항만", "rail_marine_infra"),
    (r"항공 운송", "airlines"),
    (r"운송|물류|창고", "logistics_freight"),
    (r"기계|장비", "industrial_machinery"),
    (r"전기장비|전동기|발전기|전기 변환", "electrical_equipment"),
    # completeness audit 2026-10-27: KRX KIND names that matched no rule (452 KR issuers without a sector). Appended
    # last, so no issuer classified by an earlier rule changes.
    (r"의료용품|의약 관련", "medtech"),
    (r"정밀기기|측정|광학", "electronic_components"),
    (r"절연선|케이블|전구|조명장치", "electrical_equipment"),
    (r"부동산", "real_estate_development"),
    (r"금속 가공|금속제품|금속 주조|증기발생기", "industrial_machinery"),
    (r"무기 및 총포탄", "aerospace_defense"),
    (r"광고|시장조사|여론조사", "advertising_publishing"),
    (r"출판|인쇄", "advertising_publishing"),
    (r"정보 서비스|기록매체|마그네틱", "internet_platforms"),
    (r"시멘트|석회|플라스터|비금속 광물|요업|유리", "construction_materials"),
    (r"엔지니어링|건축기술|과학기술 서비스|과학 및 기술 서비스|전문 서비스|전문디자인|사업지원|경비|시설 유지|"
     r"사업시설", "business_services"),
    (r"공사업|공사|시설물 축조", "construction_building"),
    (r"펄프|종이|판지|골판지|목재|나무제품", "packaging_paper"),
    (r"가구|악기|운동 및 경기용구|귀금속|장신용품|그외 기타 제품", "household_durables_leisure"),
    (r"비료|농약|살균|살충", "chemicals_specialty"),
    (r"고무제품", "chemicals_specialty"),
    (r"도축|육류|곡물|전분|수산물|유지|낙농|과실|채소|떡|빵|과자", "packaged_food"),
    (r"작물 재배|어로|어업", "agri_products"),
    (r"여행|숙박", "hotels_travel_leisure"),
    (r"유원지|오락|스포츠|창작 및 예술", "consumer_services"),
    (r"교습|학원|교육", "consumer_services"),
    (r"직물|방적|가공사", "apparel_luxury"),
    (r"폐기물|원료 재생|해체", "business_services"),
    (r"증기|냉·온수|공기조절", "multi_utilities"),
    (r"수리업|임대업|개인 서비스", "consumer_services"),
)


def _contract_path() -> Path:
    return get_settings().contracts_dir / "sector_taxonomy.yaml"


def norm_industry(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).replace("—", " - ").replace("–", " - ")
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def load_taxonomy() -> dict[str, Any]:
    doc = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))
    subs = {s["id"]: s for s in doc["subsectors"]}
    yf = {norm_industry(k).lower(): v for k, v in (doc.get("yf_industry") or {}).items()}
    sic = [(int(a), int(b), c) for a, b, c in doc.get("sic_ranges") or []]
    tse = {str(k).zfill(4): v.get("default") for k, v in (doc.get("tse33") or {}).items()}
    ksic = doc.get("ksic") or {}
    csrc = doc.get("csrc") or {}
    cvm = [(re.compile(p, re.I), sub) for p, sub in ((doc.get("cvm") or {}).get("keywords") or [])]
    twse = {str(k).zfill(2): v for k, v in ((doc.get("twse") or {}).get("codes") or {}).items()}
    overrides = {str(k): v for k, v in (doc.get("overrides") or {}).items()}
    return {"doc": doc, "subsectors": subs, "yf": yf, "sic": sic, "tse33": tse, "ksic": ksic, "csrc": csrc, "cvm": cvm, "twse": twse,
            "overrides": overrides, "size_rules": doc.get("size_rules") or {}}


def sector_of(subsector_id: str | None) -> str | None:
    if not subsector_id:
        return None
    s = load_taxonomy()["subsectors"].get(subsector_id)
    return s["sector"] if s else None


def subsector_groups(subsector_id: str | None) -> list[str]:
    s = load_taxonomy()["subsectors"].get(subsector_id or "")
    return list(s.get("groups") or []) if s else []


def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def map_yf(industry: str | None) -> str | None:
    if not industry:
        return None
    return load_taxonomy()["yf"].get(norm_industry(industry).lower())


def map_sic(sic: str | int | None) -> tuple[bool, str | None]:
    """(matched, subsector). matched with subsector None means 'excluded' (e.g. 6770 blank checks)."""
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return False, None
    for lo, hi, sub in load_taxonomy()["sic"]:
        if lo <= code <= hi:
            return True, sub
    return False, None


def map_tse33(code: str | None) -> str | None:
    if not code or not str(code).strip().isdigit():
        return None
    return load_taxonomy()["tse33"].get(str(code).strip().zfill(4))


def map_ksic(code_or_name: str | None) -> str | None:
    """Numeric KSIC (longest prefix in divisions / 4-digit overrides, else section default) or a Korean KSIC name."""
    if not code_or_name:
        return None
    v = str(code_or_name).strip()
    k = load_taxonomy()["ksic"]
    if v.isdigit():
        o4 = (k.get("overrides_4digit") or {})
        if v[:4] in o4:
            return o4[v[:4]]
        div = {str(a): b for a, b in (k.get("divisions") or {}).items()}
        for n in (3, 2):
            if v[:n] in div:
                return div[v[:n]]
        d2 = int(v[:2]) if len(v) >= 2 else None
        for letter, (lo, hi) in (k.get("section_of_division") or {}).items():
            if d2 is not None and lo <= d2 <= hi:
                return (k.get("sections") or {}).get(letter, {}).get("default")
        return None
    for pat, sub in KSIC_NAME_RULES:
        if re.search(pat, v):
            return sub
    return None


def map_csrc(code: str | None) -> str | None:
    if not code:
        return None
    v = str(code).strip().upper()
    m = re.match(r"^([A-S])\s*(\d{2})?", v)
    if not m:
        return None
    c = load_taxonomy()["csrc"]
    codes = {str(a).upper(): b for a, b in (c.get("codes") or {}).items()}
    letters = {str(a).upper(): b for a, b in (c.get("letters") or {}).items()}
    if m.group(2):
        key = m.group(1) + m.group(2)
        if key in codes:
            return codes[key]
        if key in letters:
            return letters[key]
    return letters.get(m.group(1))


def map_twse(code: str | None) -> str | None:
    """TWSE / TPEx 2-digit industry code (產業別) -> subsector."""
    if not code or not str(code).strip().isdigit():
        return None
    return load_taxonomy()["twse"].get(str(code).strip().zfill(2))


def map_cvm(sector_text: str | None) -> str | None:
    if not sector_text:
        return None
    t = sector_text.lower()
    t2 = _strip_accents(t)
    for rx, sub in load_taxonomy()["cvm"]:
        if rx.search(t) or rx.search(t2):
            return sub
    return None


def _is_gsib(name: str | None) -> bool:
    from app.normalize.identity import normalize_name

    n = normalize_name(name or "", strip_suffixes=False)
    return any(g in n for g in GSIB_NAMES)


def _manual_subsectors(settings=None) -> dict[str, str]:
    from app.normalize.identity_universe import load_overrides

    try:
        ov = load_overrides(settings)
    except Exception:  # noqa: BLE001
        return {}
    return {r["key"]: r["value"] for r in ov.iter_rows(named=True) if r["kind"] == "subsector" and r.get("value")}


def classify(issuer: dict[str, Any], extra: dict[str, Any] | None = None, manual: dict[str, str] | None = None,
             total_assets_usd: float | None = None) -> tuple[str | None, str | None, str]:
    """Classify one issuer row (dict with issuers columns). extra: {'override_keys': [...], 'native_scheme': ...}.

    Returns (sector_id, subsector_id, classification_source)."""
    tax = load_taxonomy()
    extra = extra or {}
    keys = list(extra.get("override_keys") or [])
    if issuer.get("cik"):
        keys.append(f"cik:{issuer['cik']}")
    sub, src = None, "unknown"
    for k in keys:
        k2 = k.replace("-", ".") if k.startswith("ticker:") else k
        if manual and (k in manual or k2 in manual):
            sub, src = manual.get(k) or manual.get(k2), "manual"
            break
        hk = re.sub(r"^ticker:0+(\d{4})@XHKG$", r"ticker:\1@XHKG", k2)
        if k2 in tax["overrides"] or hk in tax["overrides"]:
            sub, src = tax["overrides"].get(k2) or tax["overrides"][hk], "override"
            break
    excluded = False
    if sub is None:
        y = map_yf(issuer.get("yf_industry"))
        if y:
            sub, src = y, "yf_industry"
        elif norm_industry(issuer.get("yf_industry")).lower() == "shell companies":
            excluded, src = True, "yf_industry"
    if sub is None and not excluded and issuer.get("sic_code"):
        matched, s = map_sic(issuer.get("sic_code"))
        if matched:
            sub, src = s, "sic"
            excluded = s is None
    if sub is None and not excluded:
        scheme = extra.get("native_scheme")
        code = issuer.get("native_industry_code")
        if code:
            if scheme == "tse33":
                sub, src = map_tse33(code), "tse33"
            elif scheme in ("ksic", "ksic_name"):
                sub, src = map_ksic(code), "ksic"
            elif scheme == "csrc":
                sub, src = map_csrc(code), "csrc"
            elif scheme == "cvm":
                sub, src = map_cvm(code), "cvm"
            elif scheme == "twse":
                sub, src = map_twse(code), "twse"
            if sub is None:
                src = "unknown"
    if sub == "banks_regional":
        rule = tax["size_rules"].get("banks_global") or {}
        big = total_assets_usd is not None and total_assets_usd > float(rule.get("min", 500e9))
        if big or (rule.get("also_if_gsib", True) and _is_gsib(issuer.get("name"))):
            sub = "banks_global"
    if sub is not None and sub not in tax["subsectors"]:
        log.warning("taxonomy: unknown subsector %r for %s", sub, issuer.get("issuer_id"))
        sub, src = None, "unknown"
    return sector_of(sub), sub, (src if sub or excluded else "unknown")


def _bank_assets(settings=None) -> dict[str, float]:
    """Latest FY total assets in USD per issuer from fundamentals_wide (if built)."""
    try:
        from app.core.storage import scan_table

        lf = scan_table("fundamentals_wide", settings)
        if "total_assets" not in lf.collect_schema().names():
            return {}
        df = (lf.filter((pl.col("currency_basis") == "USD") & (pl.col("period_kind") == "FY"))
              .select("issuer_id", "period_end", "knowledge_date", "total_assets").collect())
        df = df.sort(["period_end", "knowledge_date"]).group_by("issuer_id").agg(pl.col("total_assets").drop_nulls().last())
        return {r["issuer_id"]: r["total_assets"] for r in df.iter_rows(named=True) if r["total_assets"]}
    except Exception:  # noqa: BLE001
        return {}


def classify_issuers(issuers: pl.DataFrame, extra: dict[str, dict[str, Any]] | None = None,
                     settings=None) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Add sector_id / subsector_id / classification_source to an issuers frame; return (frame, coverage report)."""
    extra = extra or {}
    manual = _manual_subsectors(settings)
    assets = _bank_assets(settings)
    sectors, subs, srcs = [], [], []
    for r in issuers.iter_rows(named=True):
        sec, sub, src = classify(r, extra.get(r["issuer_id"]), manual, assets.get(r["issuer_id"]))
        sectors.append(sec)
        subs.append(sub)
        srcs.append(src)
    out = issuers.with_columns(pl.Series("sector_id", sectors, dtype=pl.Utf8), pl.Series("subsector_id", subs, dtype=pl.Utf8),
                               pl.Series("classification_source", srcs, dtype=pl.Utf8))
    return out, coverage(out)


def coverage(issuers: pl.DataFrame, issuer_ids: list[str] | None = None) -> dict[str, Any]:
    df = issuers if issuer_ids is None else issuers.filter(pl.col("issuer_id").is_in(issuer_ids))
    if "is_active" in df.columns:
        df = df.filter(pl.col("is_active"))
    n = df.height
    if not n:
        return {"n": 0, "classified_pct": 0.0}
    cls = df.filter(pl.col("subsector_id").is_not_null()).height
    by_src = {r["classification_source"]: r["len"] for r in df.group_by("classification_source").len().iter_rows(named=True)}
    by_region = {}
    for r in df.group_by("region").agg(pl.len().alias("n"), pl.col("subsector_id").is_not_null().sum().alias("c")).iter_rows(named=True):
        by_region[r["region"]] = round(100.0 * r["c"] / r["n"], 1)
    return {"n": n, "classified": cls, "classified_pct": round(100.0 * cls / n, 1), "by_source": by_src,
            "by_region_pct": by_region}
