"""Bloomberg-style security references -> internal security ids.

    'AAPL US' | 'AAPL US Equity' | 'SAP GY' | 'SAP.DE' | '7203 JT' | 'SPX Index' | 'EURUSD Curncy' | 'CL1 Comdty'
    | 'IDX:^GSPC' | 'S2MZ7RR28QA9E' | an ISIN | a plain ticker ('AAPL', best listing: exact ticker, primary, largest)

Market codes map to MICs (Bloomberg composite / exchange codes); indices, currencies and commodities map to the
synthetic instrument ids of price_history (IDX:<Yahoo symbol>). All lookups are parameterised."""

from __future__ import annotations

import re
from typing import Any

YELLOW_KEYS = ("equity", "index", "curncy", "comdty", "corp", "govt", "mtge", "muni", "pfd")

#: Bloomberg market code -> MICs
BBG_MIC: dict[str, tuple[str, ...]] = {
    "US": ("XNYS", "XNAS", "ARCX", "XASE", "BATS"), "UN": ("XNYS",), "UW": ("XNAS",), "UQ": ("XNAS",),
    "UR": ("XNAS",), "UA": ("XASE",), "UP": ("ARCX",), "UF": ("BATS",),
    "CN": ("XTSE", "XTSX"), "CT": ("XTSE",), "CV": ("XTSX",),
    "LN": ("XLON",), "GY": ("XETR",), "GR": ("XETR", "XFRA"), "GF": ("XFRA",), "FP": ("XPAR",), "NA": ("XAMS",),
    "BB": ("XBRU",), "PL": ("XLIS",), "ID": ("XDUB",), "IM": ("XMIL", "MTAA"), "SM": ("XMAD",),
    "SW": ("XSWX", "XVTX"), "SE": ("XSWX", "XVTX"), "VX": ("XVTX", "XSWX"), "SS": ("XSTO",), "DC": ("XCSE",),
    "FH": ("XHEL",), "NO": ("XOSL",), "AV": ("XWBO",), "PW": ("XWAR",), "GA": ("XATH",), "CP": ("XPRA",),
    "HB": ("XBUD",), "RO": ("XBSE",), "ET": ("XTAL",), "LH": ("XLIT",), "IR": ("XICE",),
    "JP": ("XTKS",), "JT": ("XTKS",), "KS": ("XKRX",), "KQ": ("XKOS",), "HK": ("XHKG",),
    "CH": ("XSHG", "XSHE"), "CG": ("XSHG",), "C1": ("XSHG",), "CS": ("XSHE",), "C2": ("XSHE",),
    "TT": ("XTAI", "ROCO"), "TP": ("ROCO",), "AU": ("XASX",), "AT": ("XASX",), "BZ": ("BVMF",), "BS": ("BVMF",),
    "MM": ("XMEX",), "CI": ("XSGO",), "CB": ("XBOG",), "PE": ("XLIM",), "AR": ("XBUE",),
    "IN": ("XNSE", "XBOM"), "IS": ("XNSE",), "IB": ("XBOM",), "SP": ("XSES",), "SJ": ("XJSE",), "TI": ("XIST",),
    "IT": ("XTAE",), "AB": ("XSAU",),
}

#: Bloomberg index tickers -> instrument ids
BBG_INDEX: dict[str, str] = {
    "SPX": "IDX:^GSPC", "INDU": "IDX:^DJI", "CCMP": "IDX:^IXIC", "NDX": "IDX:^NDX", "RTY": "IDX:^RUT",
    "MID": "IDX:^MID", "SML": "IDX:^SML", "VIX": "IDX:^VIX", "SX5E": "IDX:^STOXX50E", "SXXP": "IDX:^STOXX",
    "DAX": "IDX:^GDAXI", "CAC": "IDX:^FCHI", "UKX": "IDX:^FTSE", "NKY": "IDX:^N225", "HSI": "IDX:^HSI",
    "HSCEI": "IDX:^HSCE", "KOSPI": "IDX:^KS11", "KOSDAQ": "IDX:^KQ11", "IBEX": "IDX:^IBEX", "FTSEMIB": "IDX:FTSEMIB.MI",
    "SMI": "IDX:^SSMI", "AEX": "IDX:^AEX", "BEL20": "IDX:^BFX", "ATX": "IDX:^ATX", "PSI20": "IDX:PSI20.LS",
    "OMX": "IDX:^OMX", "SHCOMP": "IDX:000001.SS", "SHSZ300": "IDX:000300.SS", "CSI300": "IDX:000300.SS",
    "SZCOMP": "IDX:399001.SZ", "IBOV": "IDX:^BVSP", "MEXBOL": "IDX:^MXX", "SPTSX": "IDX:^GSPTSE",
    "AS51": "IDX:^AXJO", "TWSE": "IDX:^TWII", "NIFTY": "IDX:^NSEI", "SENSEX": "IDX:^BSESN", "STI": "IDX:^STI",
    "IPSA": "IDX:^IPSA", "MERVAL": "IDX:^MERV", "JCI": "IDX:^JKSE", "FBMKLCI": "IDX:^KLSE", "SET": "IDX:^SET.BK",
    "DXY": "IDX:DX-Y.NYB", "MOVE": "IDX:^MOVE", "SKEW": "IDX:^SKEW", "VVIX": "IDX:^VVIX", "SPGSCI": "IDX:^SPGSCI",
    "USGG10YR": "IDX:^TNX", "USGG5YR": "IDX:^FVX", "USGG30YR": "IDX:^TYX", "USGG3M": "IDX:^IRX",
}

#: Bloomberg generic commodity futures -> continuous front-month ids
BBG_COMDTY: dict[str, str] = {
    "CL": "CL=F", "CO": "BZ=F", "NG": "NG=F", "HO": "HO=F", "XB": "RB=F", "GC": "GC=F", "SI": "SI=F", "HG": "HG=F",
    "PL": "PL=F", "PA": "PA=F", "LA": "ALI=F", "C": "ZC=F", "W": "ZW=F", "S": "ZS=F", "SM": "ZM=F", "BO": "ZL=F",
    "KW": "KE=F", "O": "ZO=F", "RR": "ZR=F", "CC": "CC=F", "KC": "KC=F", "SB": "SB=F", "CT": "CT=F", "JO": "OJ=F",
    "LC": "LE=F", "FC": "GF=F", "LH": "HE=F", "DA": "DC=F", "LB": "LBR=F", "ES": "ES=F", "NQ": "NQ=F", "DM": "YM=F",
    "RTA": "RTY=F", "TY": "ZN=F", "FV": "ZF=F", "TU": "ZT=F", "US": "ZB=F", "EC": "6E=F", "BP": "6B=F", "JY": "6J=F",
    "BTC": "BTC=F", "TTF": "TTF=F",
}


def parse_ref(text: str) -> dict[str, Any]:
    """Split a reference into {base, market, yellow} (upper-cased; market / yellow may be None)."""
    parts = [p for p in re.split(r"\s+", text.strip()) if p]
    yellow = None
    if parts and parts[-1].lower() in YELLOW_KEYS:
        yellow = parts.pop().lower()
    market = None
    if len(parts) >= 2 and parts[-1].upper() in BBG_MIC:
        market = parts.pop().upper()
    base = " ".join(parts).upper()
    return {"base": base, "market": market, "yellow": yellow}


def instrument_candidates(ref: dict[str, Any]) -> list[str]:
    """Instrument ids to try for index / currency / commodity references (in order)."""
    b, y = ref["base"], ref["yellow"]
    out: list[str] = []
    if y == "index" or (y is None and b in BBG_INDEX):
        if b in BBG_INDEX:
            out.append(BBG_INDEX[b])
        out += [f"IDX:^{b}", f"IDX:{b}"]
    if y == "curncy" or (y is None and re.fullmatch(r"[A-Z]{6}", b or "")):
        c = b.replace("/", "")
        if len(c) == 3:
            out += [f"IDX:{c}USD=X", f"IDX:USD{c}=X"]
        out.append(f"IDX:{c}=X")
    if y == "comdty":
        m = re.fullmatch(r"([A-Z]+?)\s*\d*", b)
        root = m.group(1) if m else b
        if root in BBG_COMDTY:
            out.append("IDX:" + BBG_COMDTY[root])
        out.append(f"IDX:{root}=F")
    return out


def resolve_many(cur: Any, refs: list[str]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """(ids in input order, unresolved refs, per-ref detail) using a DuckDB cursor on the serving snapshot."""
    ids: list[str] = []
    bad: list[str] = []
    detail: list[dict[str, Any]] = []
    for r in refs:
        hit = resolve_one(cur, r)
        detail.append({"query": r, **(hit or {"security_id": None})})
        if hit and hit.get("security_id"):
            if hit["security_id"] not in ids:
                ids.append(hit["security_id"])
        else:
            bad.append(r)
    return ids, bad, detail


def resolve_one(cur: Any, text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if not t or len(t) > 64:
        return None

    def one(sql: str, params: list[Any]) -> dict[str, Any] | None:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row, strict=True))

    sel = ("SELECT security_id, ticker, name, mic, region, country, currency, security_type, "
           "coalesce(security_type, 'common') IN ('fx', 'future', 'index', 'etf') AS is_instrument FROM sec ")
    # internal ids / ISIN
    if re.fullmatch(r"(S[0-9A-Z]{8,16}|(IDX|ETF):\S+)", t):
        r = one(sel + "WHERE security_id = ?", [t])
        if r:
            return {**r, "match": "id"}
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", t.upper()):
        r = one(sel + "WHERE upper(isin) = ? ORDER BY coalesce(is_primary_security, FALSE) DESC, "
                      "market_cap_usd DESC NULLS LAST LIMIT 1", [t.upper()])
        if r:
            return {**r, "match": "isin"}
    ref = parse_ref(t)
    if not ref["base"]:
        return None
    for iid in instrument_candidates(ref):
        r = one(sel + "WHERE security_id = ?", [iid])
        if r:
            return {**r, "match": "instrument"}
    if ref["yellow"] in ("index", "curncy", "comdty"):
        return None
    base = ref["base"]
    mics = list(BBG_MIC.get(ref["market"] or "", ()))
    cond = "(upper(ticker) = ? OR upper(split_part(ticker, '.', 1)) = ? OR replace(upper(ticker), '-', '.') = ?)"
    params: list[Any] = [base, base, base]
    if mics:
        cond += f" AND mic IN ({', '.join('?' for _ in mics)})"
        params += mics
    r = one(sel + f"WHERE {cond} ORDER BY (upper(ticker) = ?) DESC, coalesce(is_primary_security, FALSE) DESC, "
                  "coalesce(is_active, TRUE) DESC, (coalesce(security_type, 'common') IN ('common', 'adr', 'preferred')) DESC, "
                  "market_cap_usd DESC NULLS LAST LIMIT 1", params + [base])
    if r:
        return {**r, "match": "ticker" if not mics else "ticker+market"}
    if not mics:  # instruments by their ticker (EURUSD=X, ^GSPC, CL=F)
        r = one(sel + "WHERE upper(ticker) = ? LIMIT 1", [base])
        if r:
            return {**r, "match": "ticker"}
    return None
