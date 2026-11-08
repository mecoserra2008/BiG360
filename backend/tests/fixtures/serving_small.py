"""Synthetic but realistic Parquet lake for API tests and the perf script (owner: api).

    from tests.fixtures.serving_small import build_lake
    info = build_lake(settings, n_securities=300, years=10)      # writes every table through app.core.storage.write_table
    rep = build_serving_db(settings)                               # then build + publish a snapshot

All rows go through the real table contracts (write_table -> conform/validate), so the fixture breaks loudly when a
contract changes. Values are random but internally consistent (prices -> market cap -> multiples, fundamentals ->
ratios, factor scores -> composites, zones around the last close, insider clusters, GDELT events linked to issuers).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import yaml

from app.core.config import Settings, get_settings
from app.core.storage import write_table

REGION_MIX = [("US", 0.58), ("EU", 0.14), ("UK", 0.06), ("JP", 0.10), ("KR", 0.04), ("CN", 0.03), ("HK", 0.02), ("LATAM", 0.03)]
REGION_INFO = {  # country, currency, mic, usd per unit, benchmark
    "US": ("US", "USD", "XNAS", 1.0), "EU": ("DE", "EUR", "XETR", 1.08), "UK": ("GB", "GBP", "XLON", 1.27),
    "JP": ("JP", "JPY", "XTKS", 0.0067), "KR": ("KR", "KRW", "XKRX", 0.00073), "CN": ("CN", "CNY", "XSHG", 0.138),
    "HK": ("HK", "HKD", "XHKG", 0.128), "LATAM": ("BR", "BRL", "BVMF", 0.18),
}
BENCH = [("IDX:^GSPC", "US", "USD"), ("IDX:^STOXX50E", "EU", "EUR"), ("IDX:^FTSE", "UK", "GBP"), ("IDX:^N225", "JP", "JPY"),
         ("IDX:^KS11", "KR", "KRW"), ("IDX:^HSI", "HK", "HKD"), ("IDX:000300.SS", "CN", "CNY"), ("IDX:^BVSP", "LATAM", "BRL")]
FACTORS = ("quality", "profitability", "growth", "value", "balance_sheet", "momentum", "insider", "sentiment", "macro", "moat")
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _id(prefix: str, seed: str) -> str:
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    out = []
    for _ in range(12):
        out.append(_B32[h % 32])
        h //= 32
    return prefix + "".join(out)


def _bdays(start: date, end: date) -> list[date]:
    d = np.arange(np.datetime64(start), np.datetime64(end) + np.timedelta64(1, "D"), dtype="datetime64[D]")
    d = d[np.is_busday(d)]
    return [x.item() for x in d]


def _w(df: pl.DataFrame | list[dict[str, Any]], table: str, s: Settings) -> int:
    if isinstance(df, list):
        df = pl.DataFrame(df, infer_schema_length=None)
    return write_table(df, table, mode="overwrite", settings=s)


def _yaml(s: Settings, name: str) -> dict[str, Any]:
    return yaml.safe_load((s.contracts_dir / name).read_text(encoding="utf-8"))


def build_lake(settings: Settings | None = None, n_securities: int = 300, years: int = 10, seed: int = 7,
               as_of: date | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    rng = np.random.default_rng(seed)
    as_of = as_of or date(2026, 10, 9)
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    tax = _yaml(s, "sector_taxonomy.yaml")
    kp = _yaml(s, "sector_kpis.yaml")
    sc = _yaml(s, "scoring.yaml")
    subs = tax["subsectors"]
    kset_of = {sub: ks for ks in kp["kpi_sets"] for sub in ks["subsectors"]}
    counts: dict[str, int] = {}

    # ------------------------------------------------------------------ reference
    regions = rng.choice([r for r, _ in REGION_MIX], size=n_securities, p=[w for _, w in REGION_MIX])
    sec_rows, iss_rows, lst_rows, idm_rows, uni_rows = [], [], [], [], []
    secs: list[dict[str, Any]] = []
    used: set[str] = set()
    for i in range(n_securities):
        reg = str(regions[i])
        country, ccy, mic, _ = REGION_INFO[reg]
        sub = subs[int(rng.integers(len(subs)))]
        letters = "".join(chr(65 + int(x)) for x in rng.integers(0, 26, size=int(rng.integers(2, 5))))
        ticker = {"US": letters, "EU": f"{letters}.DE", "UK": f"{letters}.L", "JP": f"{1000 + i}.T", "KR": f"{100000 + i:06d}.KS",
                  "CN": f"{600000 + i}.SS", "HK": f"{i:04d}.HK", "LATAM": f"{letters}3.SA"}[reg]
        name = f"{letters.title()} {['Holdings', 'Group', 'Corp', 'Industries', 'Systems', 'Technologies'][i % 6]}"
        if i == 0:
            ticker, name, reg, country, ccy, mic = "AAPL", "Apple Inc.", "US", "US", "USD", "XNAS"
            sub = next(x for x in subs if x["id"] in ("hardware_networking", "consumer_electronics", "hardware")) \
                if any(x["id"] in ("hardware_networking",) for x in subs) else sub
        while ticker in used:
            ticker = ticker.split(".")[0] + "X" + ("." + ticker.split(".")[1] if "." in ticker else "")
        used.add(ticker)
        iid = _id("I", f"issuer{i}")
        sid = _id("S", f"sec{i}")
        lid = _id("L", f"lst{i}")
        isin = f"{country}{i:09d}{i % 7}"
        iss_rows.append({"issuer_id": iid, "name": name, "short_name": name.split()[0], "aliases": [name.split()[0].upper()],
                         "country": country, "region": reg, "sector_id": sub["sector"], "subsector_id": sub["id"],
                         "classification_source": "manual", "reporting_gaap": "us_gaap" if reg == "US" else "ifrs",
                         "reporting_currency": ccy, "fiscal_year_end_mmdd": "12-31",
                         "filer_category": "domestic" if reg == "US" else "non_sec",
                         "cik": f"{320193 + i:010d}" if reg == "US" else None, "website": f"https://{letters.lower()}.example.com",
                         "description": f"{name} operates in {sub['name']}.", "employees": int(rng.integers(500, 200000)),
                         "is_active": True, "updated_at": now})
        sec_rows.append({"security_id": sid, "issuer_id": iid, "security_type": "common", "isin": isin,
                         "primary_listing_id": lid, "display_ticker": ticker, "name": name, "trading_currency": ccy,
                         "is_primary_security": True, "is_active": True, "updated_at": now})
        lst_rows.append({"listing_id": lid, "security_id": sid, "mic": mic, "exchange_country": country,
                         "local_symbol": ticker.split(".")[0], "yf_ticker": ticker, "quote_currency": ccy, "price_divisor": 1.0,
                         "is_primary": True, "timezone": "UTC", "valid_from": date(2000, 1, 1), "updated_at": now})
        idm_rows += [{"id_type": "isin", "id_value": isin, "entity_level": "security", "issuer_id": iid, "security_id": sid,
                      "valid_from": date(2000, 1, 1), "source_id": "fixture", "confidence": 1.0, "updated_at": now},
                     {"id_type": "yf_ticker", "id_value": ticker, "entity_level": "listing", "issuer_id": iid, "security_id": sid,
                      "listing_id": lid, "valid_from": date(2000, 1, 1), "source_id": "fixture", "confidence": 1.0, "updated_at": now}]
        if reg == "US":
            idm_rows.append({"id_type": "cik", "id_value": f"{320193 + i:010d}", "entity_level": "issuer", "issuer_id": iid,
                             "valid_from": date(2000, 1, 1), "source_id": "fixture", "confidence": 1.0, "updated_at": now})
        uni_rows.append({"universe_id": "mvp_us" if reg == "US" else "mvp_global", "security_id": sid,
                         "valid_from": date(2020, 1, 1), "updated_at": now})
        uni_rows.append({"universe_id": "full", "security_id": sid, "valid_from": date(2020, 1, 1), "updated_at": now})
        secs.append({"i": i, "issuer_id": iid, "security_id": sid, "listing_id": lid, "ticker": ticker, "region": reg,
                     "country": country, "ccy": ccy, "fx": REGION_INFO[reg][3], "sector_id": sub["sector"],
                     "subsector_id": sub["id"], "kset": kset_of[sub["id"]], "name": name})
    counts["issuers"] = _w(iss_rows, "issuers", s)
    counts["securities_master"] = _w(sec_rows, "securities_master", s)
    counts["listings"] = _w(lst_rows, "listings", s)
    counts["identifier_map"] = _w(idm_rows, "identifier_map", s)
    counts["universe_membership"] = _w(uni_rows, "universe_membership", s)

    # ------------------------------------------------------------------ fx
    days = _bdays(as_of - timedelta(days=int(365.25 * years)), as_of)
    nd = len(days)
    fx_frames = []
    for _, (_, ccy, _, usd) in REGION_INFO.items():
        if ccy == "USD":
            continue
        path = usd * np.exp(np.cumsum(rng.normal(0, 0.004, nd)) - np.cumsum(rng.normal(0, 0.004, nd)).mean())
        fx_frames.append(pl.DataFrame({"date": days, "currency": ccy, "usd_per_unit": path, "units_per_usd": 1 / path,
                                       "source_id": "ecb", "ingested_at": now}))
    fx = pl.concat(fx_frames).with_columns(pl.col("date").dt.year().alias("year"))
    counts["fx_rates"] = _w(fx, "fx_rates", s)
    last_fx = {ccy: (REGION_INFO[r][3] if ccy == "USD" else float(fx.filter(pl.col("currency") == ccy)["usd_per_unit"][-1]))
               for r, (_, ccy, _, _) in REGION_INFO.items()}
    last_fx["USD"] = 1.0

    # ------------------------------------------------------------------ prices (+ benchmarks)
    dates = np.array(days, dtype="datetime64[D]")
    yrs_to_end = (dates[-1] - dates).astype(int) / 365.25
    frames = []
    shares_of: dict[str, float] = {}
    last_close: dict[str, float] = {}
    for x in secs + [{"security_id": b, "listing_id": b, "region": r, "ccy": c, "fx": last_fx[c], "bench": True} for b, r, c in BENCH]:
        vol = rng.uniform(0.15, 0.45) / np.sqrt(252) if not x.get("bench") else 0.012
        rets = rng.normal(0.0003, vol, nd)
        start_px = rng.uniform(20, 300) / (x["fx"] if x["ccy"] in ("JPY", "KRW") else 1) ** 0.5
        close = start_px * np.exp(np.cumsum(rets))
        adj = np.exp(-0.015 * yrs_to_end) if not x.get("bench") else np.ones(nd)
        o = close * (1 + rng.normal(0, 0.004, nd))
        h = np.maximum(o, close) * (1 + np.abs(rng.normal(0, 0.006, nd)))
        lo = np.minimum(o, close) * (1 - np.abs(rng.normal(0, 0.006, nd)))
        vol_sh = rng.lognormal(13, 0.6, nd)
        fxs = x["fx"]
        frames.append(pl.DataFrame({"security_id": x["security_id"], "listing_id": x["listing_id"], "date": days,
                                    "region": x["region"], "open": o, "high": h, "low": lo, "close": close, "volume": vol_sh,
                                    "adj_factor": adj, "split_factor": 1.0, "adj_close": close * adj, "currency": x["ccy"],
                                    "close_usd": close * fxs, "dollar_volume_usd": close * fxs * vol_sh,
                                    "source_id": "yfinance", "ingested_at": now}))
        last_close[x["security_id"]] = float(close[-1])
        if not x.get("bench"):
            shares_of[x["security_id"]] = float(rng.lognormal(20, 1.0))
    px = pl.concat(frames).with_columns(pl.col("date").dt.year().alias("year"))
    counts["prices_daily"] = _w(px, "prices_daily", s)
    ca = [{"security_id": x["security_id"], "region": x["region"], "ex_date": as_of - timedelta(days=90 * k + 10),
           "action_type": "cash_dividend", "amount": round(float(rng.uniform(0.1, 1.5)), 2), "currency": x["ccy"],
           "source_id": "yfinance", "ingested_at": now} for x in secs[:150] for k in range(4)]
    ca.append({"security_id": secs[0]["security_id"], "region": "US", "ex_date": as_of - timedelta(days=700),
               "action_type": "split", "ratio": 4.0, "source_id": "yfinance", "ingested_at": now})
    counts["corporate_actions"] = _w(ca, "corporate_actions", s)
    short = [{"security_id": x["security_id"], "settlement_date": as_of - timedelta(days=12), "year": as_of.year,
              "short_interest_shares": shares_of[x["security_id"]] * rng.uniform(0.005, 0.08),
              "days_to_cover": float(rng.uniform(0.5, 8)), "short_pct_float": float(rng.uniform(0.005, 0.12)),
              "knowledge_date": as_of - timedelta(days=4), "source_id": "finra", "ingested_at": now}
             for x in secs if x["region"] == "US"]
    counts["short_interest"] = _w(short, "short_interest", s)

    # ------------------------------------------------------------------ fundamentals
    fy_years = list(range(as_of.year - years, as_of.year))
    q_ends = []
    y, q = as_of.year, (as_of.month - 1) // 3  # last completed quarter
    for _ in range(8):
        if q == 0:
            y, q = y - 1, 4
        q_ends.append((y, q, date(y, q * 3, 30 if q in (2, 3) else 31)))
        q -= 1
    q_ends.reverse()
    fund_rows: list[dict[str, Any]] = []
    base_rev: dict[str, float] = {}
    for x in secs:
        fxr = x["fx"]
        rev0 = float(rng.lognormal(22.5, 1.3)) / fxr  # local currency
        g = rng.normal(0.07, 0.06)
        gm = float(np.clip(rng.normal(0.42, 0.15), 0.08, 0.85))
        om = float(np.clip(gm - rng.uniform(0.05, 0.25), -0.05, 0.6))
        base_rev[x["issuer_id"]] = rev0
        shares = shares_of[x["security_id"]]

        def items(rev: float, frac: float) -> dict[str, float]:
            gp = rev * gm
            ebit = rev * om
            ni = ebit * 0.78 - rev * 0.01
            ta = rev / frac * rng.uniform(0.9, 1.6)
            debt = ta * rng.uniform(0.05, 0.4)
            cash = ta * rng.uniform(0.03, 0.2)
            eq = ta * rng.uniform(0.3, 0.6)
            cfo = ni * rng.uniform(1.0, 1.4) + rev * 0.04
            capex = rev * rng.uniform(0.02, 0.08)
            return {"revenue": rev, "cost_of_revenue": rev - gp, "gross_profit": gp, "sga": rev * 0.15,
                    "rnd": rev * 0.05, "ebit": ebit, "operating_income": ebit, "ebitda": ebit + rev * 0.04,
                    "depreciation_amortization": rev * 0.04, "interest_expense": debt * 0.04,
                    "pretax_income": ni / 0.78, "income_tax": ni / 0.78 * 0.22, "net_income": ni, "net_income_parent": ni,
                    "eps_diluted": ni / shares, "shares_diluted_weighted": shares, "cfo": cfo,
                    "capex": capex, "fcf": cfo - capex, "dividends_paid": max(ni, 0) * 0.3,
                    "_bs": {"total_assets": ta, "total_liabilities": ta - eq, "total_equity": eq, "total_equity_parent": eq,
                            "cash_and_equivalents": cash, "total_debt": debt, "net_debt": debt - cash,
                            "total_current_assets": ta * 0.35, "total_current_liabilities": ta * 0.2,
                            "retained_earnings": eq * 0.6, "shares_outstanding": shares, "invested_capital": eq + debt - cash,
                            "accounts_receivable": rev * 0.12, "inventory": rev * 0.08}}

        def emit(period_kind: str, fy: int, fp: str, pend: date, vals: dict[str, float], lag: int) -> None:
            kd = pend + timedelta(days=lag)
            if kd > as_of:
                return
            for k, v in vals.items():
                if k == "_bs":
                    for bk, bv in v.items():
                        fund_rows.append((x["issuer_id"], bk, fy, fp, "INSTANT", pend, bv, x["ccy"], bv * fxr, kd))
                    continue
                fund_rows.append((x["issuer_id"], k, fy, fp, period_kind, pend, v, x["ccy"], v * fxr, kd))

        for k, fy in enumerate(fy_years):
            rev = rev0 * (1 + g) ** k * float(rng.normal(1, 0.03))
            emit("FY", fy, "FY", date(fy, 12, 31), items(rev, 1.0), 60)
        last_fy_rev = rev0 * (1 + g) ** (len(fy_years) - 1)
        for k, (qy, qn, qend) in enumerate(q_ends):
            qrev = last_fy_rev / 4 * (1 + g) ** ((k + 1) / 4)
            emit("Q", qy, f"Q{qn}", qend, items(qrev, 0.25), 40)
            emit("TTM", qy, "TTM", qend, {k2: v for k2, v in items(qrev * 4, 1.0).items() if k2 != "_bs"}, 40)
    fund = pl.DataFrame(fund_rows, schema=["issuer_id", "item_id", "fiscal_year", "fiscal_period", "period_kind", "period_end",
                                           "value", "currency", "value_usd", "knowledge_date"], orient="row")
    fund = fund.with_columns(pl.lit(None, dtype=pl.Date).alias("period_start"), pl.lit("us_gaap").alias("gaap"),
                             pl.lit("reported").alias("derivation"), pl.lit("fixture").alias("source_id"),
                             pl.lit(False).alias("knowledge_date_estimated"), pl.lit(False).alias("is_restatement"),
                             pl.lit(0.95).alias("dq_score"), pl.lit(now).alias("ingested_at"),
                             pl.when(pl.col("period_kind") == "INSTANT").then(pl.lit("none")).otherwise(pl.lit("period_average")).alias("fx_method"))
    counts["fundamentals_canonical"] = _w(fund, "fundamentals_canonical", s)

    # ------------------------------------------------------------------ filings
    filings, ftc = [], []
    for x in secs:
        for fy in fy_years[-5:]:
            fid = f"fx:{x['i']}:10K:{fy}"
            filings.append({"filing_id": fid, "source_id": "sec_edgar" if x["region"] == "US" else "esef_xbrl_org", "year": fy + 1,
                            "source_entity_id": str(x["i"]), "issuer_id": x["issuer_id"], "form_type": "10-K" if x["region"] == "US" else "AFR",
                            "form_class": "annual", "period_end": date(fy, 12, 31), "fiscal_year": fy, "fiscal_period": "FY",
                            "knowledge_date": date(fy + 1, 2, 28), "is_amendment": False, "has_xbrl": True,
                            "url": f"https://example.com/{fid}", "items": [], "ingested_at": now})
            if fy >= fy_years[-3]:
                for sect in ("risk_factors", "mdna"):
                    ftc.append({"filing_id": fid, "prev_filing_id": f"fx:{x['i']}:10K:{fy - 1}", "issuer_id": x["issuer_id"],
                                "year": fy + 1, "section": sect, "cosine_tfidf": float(rng.uniform(0.6, 0.99)),
                                "jaccard": float(rng.uniform(0.5, 0.95)), "words_added_pct": float(rng.uniform(0, 0.2)),
                                "neg_word_delta": float(rng.normal(0, 0.01)), "language": "en",
                                "knowledge_date": date(fy + 1, 2, 28), "computed_at": now})
        for k, (qy, qn, qend) in enumerate(q_ends[-4:]):
            kd = qend + timedelta(days=35)
            if kd <= as_of:
                filings.append({"filing_id": f"fx:{x['i']}:8K:{qy}{qn}", "source_id": "sec_edgar", "year": kd.year,
                                "source_entity_id": str(x["i"]), "issuer_id": x["issuer_id"], "form_type": "8-K",
                                "form_class": "current_report", "knowledge_date": kd, "is_amendment": False, "has_xbrl": False,
                                "items": ["2.02", "9.01"], "ingested_at": now})
                filings.append({"filing_id": f"fx:{x['i']}:10Q:{qy}{qn}", "source_id": "sec_edgar", "year": kd.year,
                                "source_entity_id": str(x["i"]), "issuer_id": x["issuer_id"], "form_type": "10-Q",
                                "form_class": "quarterly", "period_end": qend, "fiscal_year": qy, "fiscal_period": f"Q{qn}",
                                "knowledge_date": kd + timedelta(days=5), "is_amendment": False, "has_xbrl": True, "ingested_at": now})
    counts["filings_index"] = _w(filings, "filings_index", s)
    counts["filing_text_changes"] = _w(ftc, "filing_text_changes", s)

    # ------------------------------------------------------------------ ratios / sector kpis
    unit_ok = {"ratio", "pct", "x", "days", "currency_usd", "score", "count"}
    gen = [(m["id"], m.get("unit", "ratio")) for m in kp["generic_metrics"]] + \
          [(m["id"], m.get("unit", "score")) for m in kp["accounting_scores"]]

    def mval(mid: str, unit: str) -> float:
        if mid == "piotroski_f":
            return float(rng.integers(2, 10))
        if mid in ("altman_z2", "altman_z"):
            return float(rng.normal(3.0, 1.5))
        if mid == "beneish_m":
            return float(rng.normal(-2.4, 0.6))
        return float({"pct": rng.normal(0.12, 0.10), "x": abs(rng.normal(2.5, 1.5)), "days": abs(rng.normal(55, 25)),
                      "currency_usd": rng.lognormal(19, 1), "score": rng.uniform(0, 100), "count": rng.integers(1, 50),
                      "ratio": rng.normal(1.0, 0.4)}.get(unit, rng.normal(1, 0.5)))

    rat, kpi = [], []
    periods = [("FY", fy, "FY", date(fy, 12, 31), date(fy + 1, 2, 28)) for fy in fy_years[-6:]] + \
              [("TTM", qy, "TTM", qe, qe + timedelta(days=40)) for qy, qn, qe in q_ends[-4:] if qe + timedelta(days=40) <= as_of]
    for x in secs:
        for pk, fy, fp, pe, kd in periods:
            for mid, unit in gen:
                rat.append((x["issuer_id"], mid, pk, fy, fp, pe, mval(mid, unit), unit if unit in unit_ok else "ratio", kd))
            for kk in x["kset"].get("kpis") or []:
                u = kk.get("unit", "ratio")
                kpi.append((x["issuer_id"], x["sector_id"], x["subsector_id"], x["kset"]["id"], kk["id"], pk, fy, fp, pe,
                            mval(kk["id"], u), u if u in unit_ok else "ratio", kd))
    ratios = pl.DataFrame(rat, schema=["issuer_id", "metric_id", "period_kind", "fiscal_year", "fiscal_period", "period_end",
                                       "value", "unit", "knowledge_date"], orient="row") \
        .with_columns(pl.lit(True).alias("inputs_complete"), pl.lit(now).alias("computed_at"))
    counts["ratios"] = _w(ratios, "ratios", s)
    skp = pl.DataFrame(kpi, schema=["issuer_id", "sector_id", "subsector_id", "kpi_set_id", "kpi_id", "period_kind", "fiscal_year",
                                    "fiscal_period", "period_end", "value", "unit", "knowledge_date"], orient="row") \
        .with_columns(pl.lit(True).alias("inputs_complete"), pl.lit(now).alias("computed_at"))
    counts["sector_kpis"] = _w(skp, "sector_kpis", s)

    # ------------------------------------------------------------------ valuation (last 30 sessions)
    val = []
    for x in secs:
        mc = last_close[x["security_id"]] * shares_of[x["security_id"]] * last_fx[x["ccy"]]
        for d in days[-30:]:
            val.append({"security_id": x["security_id"], "issuer_id": x["issuer_id"], "date": d, "year": d.year,
                        "shares_out": shares_of[x["security_id"]], "market_cap_usd": mc, "enterprise_value_usd": mc * 1.1,
                        "size_bucket": "large" if mc >= 1e10 else "mid" if mc >= 2e9 else "small",
                        "pe_ttm": abs(rng.normal(22, 10)), "earnings_yield": float(rng.normal(0.05, 0.02)),
                        "pb": abs(rng.normal(3.5, 2)), "ps_ttm": abs(rng.normal(3, 2)), "ev_sales": abs(rng.normal(3.2, 2)),
                        "ev_ebitda": abs(rng.normal(13, 5)), "ev_ebit": abs(rng.normal(17, 6)), "fcf_yield": float(rng.normal(0.045, 0.02)),
                        "dividend_yield": abs(rng.normal(0.02, 0.01)), "shareholder_yield": float(rng.normal(0.03, 0.02)),
                        "computed_at": now})
    counts["valuation_daily"] = _w(val, "valuation_daily", s)

    # ------------------------------------------------------------------ scores
    score_dates = sorted({date(as_of.year - k // 12 - (1 if (as_of.month - k % 12) <= 0 else 0),
                               (as_of.month - k % 12 - 1) % 12 + 1, 28) for k in range(1, 36)} | {as_of})
    fs_rows, cs_rows = [], []
    prof = {p: {f: float(w) for f, w in v.items() if not f.startswith("_")} for p, v in sc["composite"]["profiles"].items()}
    prof["ml_blend"] = prof["balanced"]
    latest_f: dict[str, dict[str, float]] = {}
    for x in secs:
        lvl = {f: rng.uniform(20, 80) for f in FACTORS}
        for d in score_dates:
            for f in FACTORS:
                lvl[f] = float(np.clip(lvl[f] + rng.normal(0, 4), 1, 99))
            latest_f[x["security_id"]] = dict(lvl)
            for f in FACTORS:
                fs_rows.append((d, d.year, x["security_id"], x["issuer_id"], f, lvl[f], lvl[f], 0.9, None, None, None))
            comp = sum(prof["balanced"][f] * lvl[f] for f in prof["balanced"]) / sum(prof["balanced"].values())
            cs_rows.append((d, d.year, x["security_id"], x["issuer_id"], "balanced", comp, 0.9, 0.92))
            if d == as_of:
                for p, w in prof.items():
                    if p != "balanced":
                        cs_rows.append((d, d.year, x["security_id"], x["issuer_id"], p,
                                        sum(w[f] * lvl[f] for f in w) / sum(w.values()), 0.9, 0.92))
    fs = pl.DataFrame(fs_rows, schema=["as_of_date", "year", "security_id", "issuer_id", "factor_id", "score", "score_raw",
                                       "coverage", "rank_in_sector", "sector_count", "delta_1m"], orient="row")
    fs = fs.with_columns(pl.col("score").rank("ordinal", descending=True).over(["as_of_date", "factor_id"]).cast(pl.Int32).alias("rank_in_sector"),
                         pl.len().over(["as_of_date", "factor_id"]).cast(pl.Int32).alias("sector_count"),
                         (pl.col("score") - pl.col("score").shift(1).over(["security_id", "factor_id"], order_by="as_of_date")).alias("delta_1m"),
                         pl.lit(now).alias("computed_at"))
    counts["factor_scores"] = _w(fs, "factor_scores", s)
    cs = pl.DataFrame(cs_rows, schema=["as_of_date", "year", "security_id", "issuer_id", "profile_id", "score", "coverage",
                                       "dq_score"], orient="row")
    cs = cs.with_columns(pl.col("score").rank("ordinal", descending=True).over(["as_of_date", "profile_id"]).cast(pl.Int32).alias("rank_global"),
                         pl.lit(None, dtype=pl.Int32).alias("rank_sector"), pl.lit(now).alias("computed_at"))
    cs = cs.with_columns(pl.when(pl.col("rank_global") <= pl.len().over(["as_of_date", "profile_id"]) * 0.2).then(pl.lit("A"))
                         .when(pl.col("rank_global") <= pl.len().over(["as_of_date", "profile_id"]) * 0.4).then(pl.lit("B"))
                         .when(pl.col("rank_global") <= pl.len().over(["as_of_date", "profile_id"]) * 0.6).then(pl.lit("C"))
                         .when(pl.col("rank_global") <= pl.len().over(["as_of_date", "profile_id"]) * 0.8).then(pl.lit("D"))
                         .otherwise(pl.lit("E")).alias("grade"))
    counts["composite_scores"] = _w(cs, "composite_scores", s)
    fof: dict[str, str] = {}
    for f in sc["factors"]:
        for m in (f.get("members") or {}):
            fof.setdefault(m, f["id"])
    ps = []
    for x in secs:
        for mid in list(dict.fromkeys(list(x["kset"].get("peer_metrics") or []) + ["roic", "gross_margin", "fcf_yield"])):
            ps.append({"as_of_date": as_of, "as_of_month": as_of.strftime("%Y-%m"), "security_id": x["security_id"],
                       "issuer_id": x["issuer_id"], "metric_id": mid, "factor_id": fof.get(mid, "quality"),
                       "raw_value": float(rng.normal(0.2, 0.1)),
                       "peer_group_key": f"subsector_global:{x['subsector_id']}", "peer_group_level": "subsector_global",
                       "peer_count": int(rng.integers(8, 40)), "peer_percentile": float(rng.uniform(0, 100)),
                       "peer_median": float(rng.normal(0.18, 0.05)), "self_z": float(rng.normal(0, 1)),
                       "self_hist_n": 12, "self_score": float(rng.uniform(0, 100)), "mcap_adj_score": None,
                       "metric_score": float(rng.uniform(0, 100)), "imputed": False, "computed_at": now})
    counts["peer_scores"] = _w(ps, "peer_scores", s)

    # ------------------------------------------------------------------ moat / forecasts
    moat_rows, fc_rows, fv_rows = [], [], []
    comps = list(sc["moat"]["components"])
    for x in secs:
        for k, d in enumerate([as_of - timedelta(days=730), as_of - timedelta(days=365), as_of]):
            cs_ = {c: round(float(rng.uniform(10, 95)), 2) for c in comps}
            score = sum(cs_[c] * sc["moat"]["components"][c]["weight"] for c in comps)
            cls = "wide" if score >= 70 else "narrow" if score >= 50 else "none"
            moat_rows.append({"as_of_date": d, "issuer_id": x["issuer_id"], "moat_class": cls if x["i"] % 17 else "insufficient_data",
                              "confidence": float(rng.uniform(0.3, 0.9)), "moat_score": score,
                              "roic_wacc_spread_avg": float(rng.normal(0.05, 0.06)), "roic_wacc_persistence": float(rng.uniform(0.2, 1)),
                              "years_evaluated": 10, "gm_level_pct": cs_["gm_level"], "gm_stability_pct": cs_["gm_stability"],
                              "margin_premium_pct": cs_["margin_premium"], "subsector_revenue_share": float(rng.uniform(0, 0.3)),
                              "intangibles_intensity_pct": cs_["intangibles_intensity"], "component_scores": json.dumps(cs_),
                              "sources_of_moat": ["scale", "intangibles"] if score > 55 else [], "wacc": 0.085,
                              "component_detail": json.dumps({**{c: {"score": cs_.get(c, 50.0), "value": 0.1, "threshold": 55.0,
                                                                     "passed": cs_.get(c, 50.0) >= 55, "weight": 0.1,
                                                                     "history": [{"fiscal_year": 2020 + j, "value": 0.1 * j} for j in range(5)]}
                                                                 for c in comps + ["revenue_stability"]},
                                                              "_roic_vs_wacc": [{"fiscal_year": 2020 + j, "roic": 0.12, "hurdle": 0.08 + 0.002 * j}
                                                                                for j in range(5)]}),
                              "moat_sources": json.dumps([{"type": "scale", "strength": 70.0}]), "model_variant": "standard",
                              "computed_at": now})
        rev = base_rev[x["issuer_id"]] * 1.5
        eps0 = rev * 0.1 / shares_of[x["security_id"]]
        for scen, gmul in (("bear", 0.5), ("base", 1.0), ("bull", 1.5)):
            for k, fy in enumerate(range(as_of.year - 3, as_of.year + 5)):
                actual = fy < as_of.year
                gr = 0.06 * (gmul if not actual else 1)
                r = rev * (1 + gr) ** (k - 2)
                for item, v in (("revenue", r), ("ebit", r * 0.2), ("net_income", r * 0.15), ("eps_diluted", eps0 * (1 + gr) ** (k - 2)),
                                ("fcff", r * 0.12), ("drv_revenue_growth", gr)):
                    fc_rows.append({"issuer_id": x["issuer_id"], "as_of_date": as_of, "scenario": scen, "fiscal_year": fy,
                                    "item_id": item, "value": float(v), "currency": x["ccy"] if not item.startswith("drv_") else None,
                                    "is_actual": actual, "computed_at": now})
        price = last_close[x["security_id"]]
        for scen, p, mult in (("bear", 0.25, 0.75), ("base", 0.5, 1.05), ("bull", 0.25, 1.4)):
            fvps = price * mult * float(rng.uniform(0.85, 1.15))
            eq = fvps * shares_of[x["security_id"]]
            fv_rows.append({"issuer_id": x["issuer_id"], "security_id": x["security_id"], "as_of_date": as_of, "scenario": scen,
                            "probability": p, "wacc": 0.085, "terminal_growth": 0.025, "enterprise_value": eq * 1.1,
                            "equity_value": eq, "fair_value_per_share": fvps, "price": price, "upside_pct": fvps / price - 1,
                            "currency": x["ccy"], "assumptions": "{}", "computed_at": now, "model": "fcff_dcf",
                            "cost_of_equity": 0.0945, "fair_value_dcf": fvps * 0.9, "fair_value_multiples": fvps * 1.1,
                            "fair_value_low": fvps * 0.8, "fair_value_high": fvps * 1.2, "tv_share": 0.6, "valuation_confidence": 70.0,
                            "confidence_reasons": ["stable_margins"], "flags": [], "target_price_12m": fvps * 1.02,
                            "sensitivity_grid": json.dumps({"wacc": [0.075, 0.085, 0.095], "terminal_growth": [0.02, 0.025, 0.03],
                                                            "fair_value": [[fvps * 1.2] * 3, [fvps] * 3, [fvps * 0.8] * 3],
                                                            "base_wacc": 0.085, "base_terminal_growth": 0.025})
                            if scen == "base" else None})
        fv_rows.append({"issuer_id": x["issuer_id"], "security_id": x["security_id"], "as_of_date": as_of,
                        "scenario": "market_implied", "wacc": 0.085, "terminal_growth": 0.025, "price": price,
                        "implied_revenue_cagr_5y": float(rng.normal(0.07, 0.04)), "implied_fcf_margin": float(rng.uniform(0.05, 0.3)),
                        "implied_vs_hist_growth_pct": float(rng.uniform(0, 100)), "currency": x["ccy"], "assumptions": "{}",
                        "computed_at": now})
    counts["moat"] = _w(moat_rows, "moat", s)
    counts["forecasts"] = _w(fc_rows, "forecasts", s)
    counts["forecast_valuation"] = _w(fv_rows, "forecast_valuation", s)

    # ------------------------------------------------------------------ insiders
    txns, sigs = [], []
    roles = ["ceo", "cfo", "director", "officer_other", "chair", "president", "owner"]
    for x in secs:
        if x["region"] not in ("US", "KR"):
            continue
        n = int(rng.integers(0, 14))
        cluster = x["i"] % 9 == 0
        for k in range(n + (3 if cluster else 0)):
            td = as_of - timedelta(days=int(rng.integers(1, 700))) if not (cluster and k >= n) else as_of - timedelta(days=5 + k)
            buy = cluster and k >= n or rng.random() < 0.35
            sh = float(rng.integers(500, 50000))
            pxv = last_close[x["security_id"]]
            txns.append({"txn_id": f"T{x['i']}-{k}", "region": x["region"], "year": td.year, "filing_id": f"0000{x['i']:06d}-26-{k:06d}",
                         "source_id": "sec_edgar" if x["region"] == "US" else "opendart", "source_entity_id": f"{x['i']}",
                         "issuer_id": x["issuer_id"], "security_id": x["security_id"], "insider_id": f"P{x['i']}-{k % 6}",
                         "insider_name": f"Insider {x['i']}-{k % 6}", "is_director": k % 3 == 0, "is_officer": k % 3 != 0,
                         "is_ten_pct_owner": False, "role_norm": roles[k % len(roles)], "transaction_date": td,
                         "knowledge_date": min(td + timedelta(days=2), as_of), "txn_code": "P" if buy else "S",
                         "txn_type": "buy" if buy else "sell", "is_open_market": True, "is_derivative": False,
                         "is_10b5_1": (not buy) and k % 2 == 0, "shares": sh, "price": pxv, "currency": x["ccy"],
                         "value_local": sh * pxv, "value_usd": sh * pxv * last_fx[x["ccy"]], "shares_owned_after": sh * 10,
                         "holding_change_pct": 0.1 if buy else -0.1, "ownership_nature": "direct", "is_amendment": False,
                         "ingested_at": now})
        for k, d in enumerate([as_of - timedelta(days=30 * j) for j in range(6)][::-1]):
            sigs.append({"as_of_date": d, "year": d.year, "security_id": x["security_id"], "issuer_id": x["issuer_id"],
                         "net_buy_value_usd_30d": float(rng.normal(0, 1e6)), "net_buy_value_usd_90d": float(rng.normal(0, 3e6)),
                         "net_buy_mcap_bps_90d": float(rng.normal(0, 2)), "n_buyers_90d": int(rng.integers(0, 5)),
                         "n_sellers_90d": int(rng.integers(0, 5)), "cluster_buy": cluster and d == as_of, "c_suite_buy": bool(rng.random() < 0.1),
                         "anomaly_score": float(rng.uniform(0, 1)), "xgb_prob_up": float(rng.uniform(0.3, 0.7)),
                         "signal_score": float(np.clip(rng.normal(50, 15), 0, 100)),
                         "top_transactions": json.dumps([f"T{x['i']}-0", f"T{x['i']}-1"]), "computed_at": now})
    counts["insider_transactions"] = _w(txns, "insider_transactions", s)
    counts["insider_signals"] = _w(sigs, "insider_signals", s)

    # ------------------------------------------------------------------ news / sentiment / gdelt
    news, sd, gcd = [], [], []
    for x in secs:
        for k in range(5):
            ts = now - timedelta(days=int(rng.integers(1, 120)), hours=int(rng.integers(0, 23)))
            sc_ = float(rng.uniform(-1, 1))
            news.append({"item_id": hashlib.sha1(f"n{x['i']}{k}".encode()).hexdigest()[:16], "year_month": ts.strftime("%Y-%m"),
                         "published_at": ts, "knowledge_date": ts.date(), "source_id": "gdelt_doc", "publisher": "example.com",
                         "url": f"https://example.com/news/{x['i']}/{k}", "title": f"{x['name']} headline {k}", "language": "en",
                         "issuer_ids": [x["issuer_id"]], "link_confidence": [float(rng.uniform(0.6, 1))],
                         "sentiment_label": "positive" if sc_ > 0.3 else "negative" if sc_ < -0.3 else "neutral",
                         "sentiment_score": sc_, "sentiment_model": "ProsusAI/finbert", "ingested_at": now})
        for d in days[-180:]:
            sd.append((d, d.year, x["security_id"], x["issuer_id"], int(rng.integers(0, 6)), float(rng.normal(0, 1)),
                       float(rng.normal(0, 0.3)), float(rng.normal(0, 0.2)), float(rng.normal(0, 0.1)), float(rng.normal(-1, 1)),
                       float(rng.uniform(0, 100))))
        for d in days[-120:]:
            gcd.append((d, d.year, x["issuer_id"], int(rng.integers(0, 40)), int(rng.integers(0, 20)), float(rng.normal(-1, 2)),
                        float(rng.normal(0, 1)), ["ECON_EARNINGS", "TAX_FNCACT"]))
    counts["news_items"] = _w(news, "news_items", s)
    sdf = pl.DataFrame(sd, schema=["date", "year", "security_id", "issuer_id", "news_count", "news_count_z", "sentiment_mean",
                                   "sentiment_ewm_7d", "sentiment_ewm_30d", "gdelt_tone_ewm_7d", "sentiment_score"], orient="row")
    counts["sentiment_daily"] = _w(sdf.with_columns(pl.lit(now).alias("computed_at")), "sentiment_daily", s)
    gdf = pl.DataFrame(gcd, schema=["date", "year", "issuer_id", "mentions", "unique_sources", "avg_tone", "attention_z", "top_themes"],
                       orient="row")
    counts["gdelt_company_daily"] = _w(gdf.with_columns(pl.lit(now).alias("computed_at")), "gdelt_company_daily", s)
    countries = ["US", "CN", "RU", "UA", "IL", "DE", "GB", "FR", "JP", "KR", "BR", "IN", "IR", "MX", "TW"]
    cats = ["conflict", "protest", "sanctions_trade", "diplomacy", "economy", "disaster", "legal_regulatory", "health", "other"]
    ev = []
    for k in range(3000):
        d = as_of - timedelta(days=int(rng.integers(0, 100)))
        c = countries[int(rng.integers(len(countries)))]
        linked = [secs[int(rng.integers(len(secs)))]["issuer_id"]] if rng.random() < 0.25 else []
        ev.append({"global_event_id": 1_200_000_000 + k, "year_month": d.strftime("%Y-%m"), "event_date": d,
                   "date_added": datetime(d.year, d.month, d.day, 12, tzinfo=UTC), "knowledge_date": d,
                   "event_code": f"{int(rng.integers(1, 20)):02d}{int(rng.integers(0, 9))}", "event_root_code": f"{int(rng.integers(1, 20)):02d}",
                   "quad_class": int(rng.integers(1, 5)), "goldstein": float(rng.uniform(-10, 10)), "num_mentions": int(rng.integers(3, 200)),
                   "num_sources": int(rng.integers(1, 50)), "num_articles": int(rng.integers(3, 200)), "avg_tone": float(rng.normal(-2, 3)),
                   "actor1_name": "GOVERNMENT", "actor2_name": None, "action_country": c, "action_lat": float(rng.uniform(-40, 60)),
                   "action_lon": float(rng.uniform(-120, 140)),
                   # every 10th article is coded as two GDELT events (same SOURCEURL) -> the feed must dedupe by URL
                   "source_url": f"https://news.example.com/world/2026/10/officials-announce-new-measures-{k - (k % 10 == 1)}.html",
                   "sector_ids": [secs[int(rng.integers(len(secs)))]["sector_id"]], "issuer_ids": linked,
                   "event_category": cats[int(rng.integers(len(cats)))], "ingested_at": now})
    counts["gdelt_events"] = _w(ev, "gdelt_events", s)

    # ------------------------------------------------------------------ technicals
    lv, zn, lts = [], [], []
    ltypes = [("swing_high", "NA"), ("swing_low", "NA"), ("prev_high", "W"), ("prev_low", "W"), ("prev_close", "M"),
              ("floor_p", "W"), ("fib_618", "NA"), ("avwap_ytd", "NA"), ("high_52w", "NA"), ("low_52w", "NA"), ("round_number", "NA")]
    for x in secs:
        c = last_close[x["security_id"]]
        atr = c * 0.02
        for k, (lt, tf) in enumerate(ltypes):
            p = c * (1 + rng.normal(0, 0.08))
            lv.append({"as_of_date": as_of, "security_id": x["security_id"], "level_type": lt, "timeframe": tf,
                       "level_key": f"{lt}@{tf}", "price": p, "anchor_date": as_of - timedelta(days=int(rng.integers(5, 300))),
                       "touches": int(rng.integers(0, 6)), "distance_atr": (p - c) / atr, "distance_pct": p / c - 1,
                       "side": "support" if p < c else "resistance", "type_rank_score": float(rng.uniform(0, 1)), "computed_at": now})
            lts.append({"as_of_date": as_of, "security_id": x["security_id"], "level_key": f"{lt}@{tf}", "n_events": int(rng.integers(5, 80)),
                        "hit_rate": float(rng.uniform(0.3, 0.7)), "hit_rate_shrunk": float(rng.uniform(0.35, 0.65)),
                        "mean_reaction_atr": float(rng.uniform(0.5, 2)), "break_rate": float(rng.uniform(0.1, 0.5)),
                        "expectancy_r": float(rng.normal(0.1, 0.2)), "rank": k + 1, "score": float(rng.uniform(0, 1)),
                        "window_start": as_of - timedelta(days=3650), "computed_at": now})
        for k in range(6):
            side = "support" if k % 2 == 0 else "resistance"
            ctr = c * (1 - 0.03 * (k // 2 + 1)) if side == "support" else c * (1 + 0.03 * (k // 2 + 1))
            zn.append({"as_of_date": as_of, "security_id": x["security_id"], "zone_id": k + 1, "side": side,
                       "zone_low": ctr - atr * 0.3, "zone_high": ctr + atr * 0.3, "center": ctr, "density": float(rng.uniform(0.1, 1)),
                       "strength": float(rng.uniform(0.2, 1)), "rank": k + 1, "distance_atr": (ctr - c) / atr,
                       "member_level_keys": ["swing_low@NA", "fib_618@NA"], "member_count": 2,
                       "entry": ctr, "invalidation": ctr - atr if side == "support" else ctr + atr,
                       "target": c * 1.06 if side == "support" else c * 0.94, "reward_risk": float(rng.uniform(1, 4)),
                       "atr14": atr, "last_close": c, "computed_at": now})
    counts["tech_levels"] = _w(lv, "tech_levels", s)
    counts["confluence_zones"] = _w(zn, "confluence_zones", s)
    counts["level_type_stats"] = _w(lts, "level_type_stats", s)

    # ------------------------------------------------------------------ ML
    runs = [("M_XGB_3M", "xgboost_rank", "3M", False), ("M_FTT_3M", "ft_transformer", "3M", False), ("M_ENS_3M", "ensemble", "3M", True),
            ("M_ENS_1M", "ensemble", "1M", True), ("M_ENS_6M", "ensemble", "6M", True), ("M_INS_XGB", "insider_xgb", None, True),
            ("M_HMM_US", "macro_hmm", None, True)]
    mr, mp, me = [], [], []
    oos_dates = [as_of - timedelta(days=7 * k) for k in range(52)][::-1]
    for rid, mt, hz, prod in runs:
        mr.append({"model_run_id": rid, "model_type": mt, "horizon": hz, "universe_id": "full", "feature_set_version": "fs_v1",
                   "train_start": date(2012, 1, 31), "train_end": date(2025, 9, 30), "oos_start": date(2017, 1, 31), "oos_end": as_of,
                   "cv_scheme": json.dumps({"type": "expanding", "n_folds": 3, "test_months": 12, "purge_days": 63, "embargo_days": 10,
                                            "folds": [{"fold": f"F{j}", "train_start": "2012-01-31", "train_end": f"{2021 + j}-12-31",
                                                       "test_start": f"{2022 + j}-01-31", "test_end": f"{2022 + j}-12-31",
                                                       "sharpe": round(float(rng.normal(1, 0.4)), 3)} for j in range(3)]}),
                   "params": json.dumps({"n_features": 120, "max_depth": 6}), "ic_mean": 0.04, "rank_ic_mean": 0.05,
                   "rank_ic_ir": 0.5, "rank_ic_tstat": float(rng.uniform(2, 4)), "decile_spread_ann": 0.08, "long_short_sharpe": 1.1,
                   "turnover_monthly": 0.3, "hit_rate": 0.62, "status": "completed", "is_production": prod,
                   "feature_importance": json.dumps([{"feature": f"f_mom_{k}", "importance": 1 / (k + 1)} for k in range(10)]),
                   "created_at": now - timedelta(days=3), "finished_at": now - timedelta(days=3)})
        if hz is None:
            continue
        for d in (oos_dates if rid == "M_ENS_3M" else [as_of]):
            sc_ = rng.normal(0, 1, len(secs))
            rp = (sc_.argsort().argsort() + 0.5) / len(secs)
            for x, v, r in zip(secs, sc_, rp, strict=True):
                mp.append((rid, d, x["security_id"], hz, float(v), float(r), int(min(10, r * 10 + 1)), d != as_of, d == as_of,
                           json.dumps([{"feature": "f_mom_12_1", "value": 0.3, "contribution": 0.12},
                                       {"feature": "f_roic", "value": 0.15, "contribution": -0.05}]) if d == as_of else None))
        for k in range(36):
            d = date(as_of.year - (k // 12) - 1, 12 - k % 12, 28)
            me += [(rid, "ic", d, "", float(rng.normal(0.04, 0.05))), (rid, "rank_ic", d, "", float(rng.normal(0.05, 0.05))),
                   (rid, "cum_long_short", d, "", float(0.01 * (36 - k)))]
            for dec in range(1, 11):
                me.append((rid, "decile_return", d, str(dec), float(rng.normal((dec - 5.5) * 0.004, 0.02))))
        for j in range(3):
            me.append((rid, "fold_rank_ic", date(2022 + j, 12, 31), f"F{j}", float(rng.normal(0.05, 0.02))))
        for sid_ in ("TECH", "FINS", "HLTH"):
            me.append((rid, "rank_ic", date(1900, 1, 1), sid_, float(rng.normal(0.05, 0.02))))
    counts["model_runs"] = _w(mr, "model_runs", s)
    mpf = pl.DataFrame(mp, schema=["model_run_id", "date", "security_id", "horizon", "score", "rank_pct", "decile", "is_oos", "is_live",
                                   "shap_top"], orient="row").with_columns(pl.lit(now).alias("computed_at"))
    counts["ml_predictions"] = _w(mpf, "ml_predictions", s)
    mef = pl.DataFrame(me, schema=["model_run_id", "metric", "date", "bucket", "value"], orient="row") \
        .unique(["model_run_id", "metric", "date", "bucket"]).with_columns(pl.lit(now).alias("computed_at"))
    counts["model_eval_series"] = _w(mef, "model_eval_series", s)

    # ------------------------------------------------------------------ macro
    ser = [("fred:DGS3MO", "fred", "rates", "D", "Percent", "US", True, 4.2), ("fred:DGS2", "fred", "rates", "D", "Percent", "US", False, 3.9),
           ("fred:DGS5", "fred", "rates", "D", "Percent", "US", False, 3.8), ("fred:DGS10", "fred", "rates", "D", "Percent", "US", True, 4.1),
           ("fred:DGS30", "fred", "rates", "D", "Percent", "US", False, 4.4), ("fred:EFFR", "fred", "rates", "D", "Percent", "US", False, 4.3),
           ("fred:CPIAUCSL", "fred", "inflation", "M", "Index 1982-1984=100", "US", True, 310.0),
           ("fred:UNRATE", "fred", "labor", "M", "Percent", "US", True, 4.1), ("fred:VIXCLS", "fred", "sentiment", "D", "Index", "US", True, 17.0),
           ("fred:BAMLH0A0HYM2", "fred", "credit", "D", "Percent", "US", True, 3.2),
           ("fred:DCOILWTICO", "fred", "commodities", "D", "Dollars per Barrel", "US", True, 75.0),
           ("ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "ecb", "rates", "D", "percent", "EA", False, 2.6),
           ("ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "ecb", "rates", "D", "percent", "EA", True, 2.5),
           ("ecb:HICP.M.U2.N.000000.4D0.ANR", "ecb", "inflation", "M", "percent y/y", "EA", True, 2.2)]
    ms, mo = [], []
    for sid_, src, cat, fq, units, ctry, core, lvl in ser:
        ms.append({"series_id": sid_, "source_id": src, "native_code": sid_.split(":", 1)[1], "title": sid_, "country": ctry,
                   "category": cat, "frequency": fq, "units": units, "default_transform": "level", "release_lag_days": 1,
                   "is_core": core, "last_observation_date": as_of, "updated_at": now})
        obs_days = days if fq == "D" else sorted({date(d.year, d.month, 1) for d in days})
        path = lvl + np.cumsum(rng.normal(0, lvl * 0.004, len(obs_days)))
        for d, v in zip(obs_days, path, strict=True):
            mo.append((sid_, src, d, float(v), d + timedelta(days=1 if fq == "D" else 15)))
    counts["macro_series"] = _w(ms, "macro_series", s)
    mof = pl.DataFrame(mo, schema=["series_id", "source_id", "date", "value", "knowledge_date"], orient="row") \
        .filter(pl.col("knowledge_date") <= as_of).with_columns(pl.lit(False).alias("knowledge_date_estimated"), pl.lit(now).alias("ingested_at"))
    counts["macro_observations"] = _w(mof, "macro_observations", s)
    labels = ["expansion", "slowdown", "contraction", "recovery"]
    mrg = []
    for scope, mid in (("US", "hmm4_us_v1"), ("GLOBAL", "hmm4_global_v1")):
        for k in range(240):
            d = date(as_of.year - (k // 12) - (1 if as_of.month - k % 12 <= 0 else 0), (as_of.month - k % 12 - 1) % 12 + 1, 1)
            p = rng.dirichlet([2, 1, 1, 1])
            mrg.append({"model_id": mid, "date": d, "region_scope": scope, "regime_id": int(p.argmax()), "regime_label": labels[int(p.argmax())],
                        "probabilities": [float(v) for v in p], "state_labels": labels,
                        "inputs_snapshot": json.dumps({"growth_mom": float(rng.normal()), "infl_mom": float(rng.normal())}),
                        "computed_at": now})
    counts["macro_regimes"] = _w(pl.DataFrame(mrg).unique(["model_id", "date"]), "macro_regimes", s)
    mb = []
    for x in secs:
        for f in ("market", "rates_10y", "curve_2s10s", "credit_spread", "usd_index", "oil") + tuple(f"regime_{lab}" for lab in labels):
            mb.append({"as_of_date": as_of, "security_id": x["security_id"], "factor_id": f, "beta": float(rng.normal(0.5 if f == "market" else 0, 0.3)),
                       "t_stat": float(rng.normal(1, 1)), "r2": 0.3, "n_obs": 36, "window_months": 36,
                       "peer_percentile": float(rng.uniform(0, 100)), "computed_at": now})
    counts["macro_betas"] = _w(mb, "macro_betas", s)
    from tests.macro_v2.synthetic import add_macro_v2  # macro v2 W1 (macro-w1): cells, history, cycle, revisions
    counts.update(add_macro_v2(s, as_of))
    from tests.macro_v2.synthetic_w2 import add_macro_v2_w2  # macro v2 W2 (macro-w2): calendar, expectations, surprises, nowcasts, recession
    counts.update(add_macro_v2_w2(s, as_of))
    from tests.macro_v4.synthetic_w4 import add_macro_w4  # macro v2 W4 (macro-w4): factors, exposures, regimes, episodes
    counts.update(add_macro_w4(s, as_of))
    from tests.macro_v3.synthetic_w3 import add_macro_w3  # macro v3 W3 (macro-w3): central-bank monitor tables
    counts.update(add_macro_w3(s, as_of))

    # ------------------------------------------------------------------ phase 3 tables (setups, txn scores, cost of capital)
    setups, tscores, coc = [], [], []
    for x in secs:
        c = last_close[x["security_id"]]
        atr = c * 0.02
        setups.append({"as_of_date": as_of, "security_id": x["security_id"], "last_close": c, "atr14": atr, "rsi14": 55.0,
                       "sma50": c * 0.98, "sma200": c * 0.92, "trend": "up", "structure_state": "bullish",
                       "last_break_kind": "bos", "last_break_dir": "up", "last_break_date": as_of - timedelta(days=5),
                       "last_break_price": c * 0.99, "bias": "long", "support_zone_id": 1, "resistance_zone_id": 2,
                       "entry_low": c * 0.97, "entry_high": c, "entry": c * 0.985, "invalidation": c * 0.95,
                       "target1": c * 1.05, "target2": c * 1.09, "reward_risk": 2.0, "technical_score": 72.0,
                       "score_trend": 90.0, "score_structure": 80.0, "score_location": 60.0, "score_rr": 50.0,
                       "score_momentum": 70.0, "score_components": json.dumps({"weights": {"trend": 0.25, "structure": 0.2,
                                                                                            "location": 0.35, "rr": 0.1, "momentum": 0.1}}),
                       "note": "fixture setup", "computed_at": now})
        coc.append({"as_of_date": as_of, "issuer_id": x["issuer_id"], "security_id": x["security_id"], "currency": x["ccy"],
                    "rf": 0.041, "rf_source": "fred:DGS10", "erp": 0.05, "crp": 0.0, "beta_raw": 1.1, "beta": 1.07,
                    "beta_source": "regression", "n_weeks": 156, "cost_of_equity": 0.0945, "kd_pre": 0.05, "kd_post": 0.04,
                    "tax_rate": 0.21, "tax_source": "statutory", "market_cap": 1e10, "total_debt": 2e9, "weight_equity": 0.83,
                    "wacc": 0.085, "terminal_growth": 0.025, "is_financial": False, "flags": [], "computed_at": now})
    for t_ in txns:
        tscores.append({"txn_id": t_["txn_id"], "year": t_["year"], "issuer_id": t_["issuer_id"], "security_id": t_["security_id"],
                        "source_entity_id": t_["source_entity_id"], "knowledge_date": t_["knowledge_date"],
                        "transaction_date": t_["transaction_date"], "direction": 1 if t_["txn_type"] == "buy" else -1,
                        "value_usd": t_["value_usd"], "role_weight": 1.0, "n_insiders_same_dir_30d": 1, "first_buy_2y": False,
                        "routine": False, "size_vs_hist": 1.2, "pre_drawdown_252d": -0.1, "pre_ret_126d": 0.02,
                        "anomaly_score": float(rng.uniform(0, 1)), "computed_at": now})
    counts["tech_setups"] = _w(setups, "tech_setups", s)
    counts["cost_of_capital"] = _w(coc, "cost_of_capital", s)
    counts["insider_txn_scores"] = _w(tscores, "insider_txn_scores", s)

    # ------------------------------------------------------------------ ops
    jr = []
    for k, (job, st) in enumerate([("prices_finalize", "success"), ("ratios_kpis", "success"), ("scoring", "partial"),
                                   ("sec_form4", "success"), ("gdelt_events", "failed"), ("moat", "success")]):
        jr.append({"run_id": f"R{k}", "year_month": now.strftime("%Y-%m"), "job_id": job, "dag_run_id": "D20261010-nightly",
                   "trigger": "schedule", "status": st, "attempt": 1, "started_at": now - timedelta(hours=3, minutes=k),
                   "finished_at": now - timedelta(hours=2), "duration_s": 60.0, "rows_written": 1000, "entities_ok": 10,
                   "entities_failed": 1 if st == "partial" else 0, "error_message": "boom" if st == "failed" else None})
    counts["job_runs"] = _w(jr, "job_runs", s)
    dq = [{"run_id": "DQ1", "year_month": now.strftime("%Y-%m"), "table_name": t, "partition_key": "", "check_id": c,
           "severity": sev, "passed": ok, "metric_value": 0.1, "threshold": 0.0, "checked_at": now}
          for t, c, sev, ok in (("prices_daily", "pk_unique", "error", True), ("prices_daily", "freshness", "warn", True),
                                ("insider_transactions", "null_rate:price", "warn", False), ("ratios", "row_count", "warn", True))]
    counts["dq_results"] = _w(dq, "dq_results", s)
    counts.update(build_cftc(s, as_of, seed))
    counts.update(build_cross_asset(s, as_of, seed))
    counts.update(build_earnings(s, as_of, secs[:40]))
    from tests.fixtures.smart_money_fixture import build_smart_money  # roadmap #7 smart money (13F, SI, 13D, EU shorts)
    counts.update(build_smart_money(s, as_of, secs, seed))
    from tests.fixtures.supply_chain_fixture import build_supply_chain  # roadmap #9 supply chain & revenue geography
    counts.update(build_supply_chain(s, as_of, secs, seed))
    from tests.fixtures.conformal_fixture import build_conformal  # conformal ellipsoids P3 (envelopes, touch, peers)
    counts.update(build_conformal(s, as_of, secs, last_close, seed))
    from tests.fixtures.techprob_fixture import build_techprob  # Technicals v2 T2-T4 probability models
    counts.update(build_techprob(s, as_of, secs, last_close, seed))
    from tests.fixtures.techmeta_fixture import build_techmeta  # Technicals v3 #2 meta-labelled setups
    counts.update(build_techmeta(s, as_of, secs, seed))
    from tests.fixtures.techv3_fixture import build_techv3  # Technicals v3 #1 factors / #5 breakouts (real jobs)
    counts.update(build_techv3(s, as_of))
    from tests.fixtures.rates_fixture import build_rates  # W3 rates & debt (curves, auctions, bonds, company debt)
    counts.update(build_rates(s, as_of, secs, seed))
    return {"as_of": as_of, "counts": counts, "securities": secs}


CFTC_MARKETS = {  # code -> (contract_market_name, exchange, subgroup, report families)
    "13874A": ("E-MINI S&P 500", "CHICAGO MERCANTILE EXCHANGE", "STOCK INDICES", ("legacy", "tff")),
    "240743": ("NIKKEI STOCK AVERAGE YEN DENOM", "CHICAGO MERCANTILE EXCHANGE", "STOCK INDICES", ("legacy", "tff")),
    "067651": ("WTI-PHYSICAL", "NEW YORK MERCANTILE EXCHANGE", "PETROLEUM AND PRODUCTS", ("legacy", "disagg")),
}


def build_cftc(s: Settings, as_of: date, seed: int = 7) -> dict[str, int]:
    """Synthetic CFTC COT positions (220 weekly reports) for three markets + markets / signals / event stats."""
    from app.analytics import cftc_signals as cs
    from app.core.storage import read_table, scan_table
    from app.ingestion import cftc

    rng = np.random.default_rng(seed)
    tue = as_of - timedelta(days=(as_of.weekday() - 1) % 7)
    dates = [tue - timedelta(weeks=k) for k in range(219, -1, -1)]
    pos, metas = [], []
    now = datetime.now(UTC)
    for code, (name, exch, sub, fams) in CFTC_MARKETS.items():
        oi = 500_000
        for fam in fams:
            for rt in (f"{fam}_fut", f"{fam}_comb"):
                walks = {c: np.cumsum(rng.normal(0, 4000, len(dates))) for c in cftc.CATEGORIES[fam]}
                for i, d in enumerate(dates):
                    rel = cftc.release_schedule(d)
                    for c, w in walks.items():
                        lo = int(60_000 + max(-50_000, w[i]))
                        sh = int(60_000 - min(50_000, w[i]))
                        pos.append({"report_type": rt, "market_code": code, "report_date": d, "category": c,
                                    "long_pos": lo, "short_pos": sh, "spread_pos": 1000, "chg_long": 0, "chg_short": 0,
                                    "pct_oi_long": lo / oi, "pct_oi_short": sh / oi, "traders_long": 40, "traders_short": 35,
                                    "open_interest": oi + i * 100, "chg_open_interest": 100, "traders_total": 300,
                                    "conc_net_4_long": 0.12, "conc_net_4_short": 0.15, "conc_net_8_long": 0.2,
                                    "conc_net_8_short": 0.25, "conc_gross_4_long": 0.14, "conc_gross_4_short": 0.17,
                                    "knowledge_ts": rel, "knowledge_date": rel.astimezone(cftc.ET).date(),
                                    "knowledge_date_estimated": True, "source_id": "cftc", "ingested_at": now})
                metas.append(pl.DataFrame({"market_code": [code], "report_date": [dates[-1]], "market_name": [name],
                                           "market_and_exchange": [f"{name} - {exch}"], "commodity_name": [name],
                                           "commodity_code": [code[:3]], "commodity_group": ["X"],
                                           "commodity_subgroup": [sub], "contract_units": ["(CONTRACTS)"]}))
    out = {"cftc_positions": write_table(pl.DataFrame(pos, infer_schema_length=None), "cftc_positions", mode="overwrite", settings=s)}
    out["cftc_markets"] = cftc.build_markets(metas, s, futures={})
    markets = read_table("cftc_markets", settings=s)
    sids = markets.get_column("price_security_id").drop_nulls().unique().to_list()
    prices = cs.load_prices(sids, s)
    sig = cs.compute_signals(cs.net_frame(scan_table("cftc_positions", s)), prices, markets, "Rtest")
    out["cftc_signals"] = write_table(sig, "cftc_signals", mode="overwrite", settings=s)
    ev = cs.event_stats(sig, prices, markets)
    if ev.height:
        out["cftc_event_stats"] = write_table(ev, "cftc_event_stats", mode="overwrite", settings=s)
    return out


def build_earnings(s: Settings, as_of: date, secs: list[dict[str, Any]]) -> dict[str, int]:
    """Earnings & corporate-events tables (roadmap #6) for a few securities: 8 released quarters with reactions and
    model-based expectations, one upcoming calendar row each (rotating date sources), corporate events, season."""
    from app.earnings import common as EC

    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    ev, rx, ex, cal, ce = [], [], [], [], []
    srcs = ("model_projection", "provider_estimate", "company_announced")
    for k, x in enumerate(secs):
        pe = date(2024, 9, 30)
        for q in range(8):
            rel = pe + timedelta(days=25 + k % 5)
            ts = datetime(rel.year, rel.month, rel.day, 20, 5, tzinfo=UTC)
            fp = ("Q3", "Q4", "Q1", "Q2")[q % 4]
            ev.append({"issuer_id": x["issuer_id"], "security_id": x["security_id"], "region": x["region"], "country": x["country"],
                       "period_end": pe, "fiscal_year": pe.year, "fiscal_period": fp, "period_inferred": False,
                       "release_date": rel, "release_ts": ts, "session": "post_market", "reaction_date": EC.reaction_day(rel, "post_market"),
                       "date_source": "official_release", "source_id": "sec_edgar", "source_label": "SEC 8-K Item 2.02 (acceptance time)",
                       "form_type": "8-K", "filing_id": f"sec_edgar:{k}-{q}", "url": "https://www.sec.gov/x", "knowledge_date": rel,
                       "computed_at": now})
            car = 0.02 * ((-1) ** (k + q))
            rx.append({"issuer_id": x["issuer_id"], "security_id": x["security_id"], "region": x["region"], "period_end": pe,
                       "release_date": rel, "reaction_date": EC.reaction_day(rel, "post_market"), "benchmark_id": "IDX:^GSPC",
                       "alpha": 0.0, "beta": 1.0, "sigma_daily": 0.015, "n_est": 200, "ret_m1_p1": car, "car_m1_p1": car,
                       "car_0_p5": car * 1.2, "car_p1_p20": car / 2, "bhar_m1_p1": car, "bhar_0_p5": car, "bhar_p1_p20": car / 2,
                       "ar_day0": car, "baseline_move_m1_p1": 0.026, "move_ratio": abs(car) / 0.026, "gap_pct": car / 2,
                       "gap_filled": q % 2 == 0, "drift_continuation": True, "complete": True, "knowledge_date": rel + timedelta(days=30),
                       "computed_at": now})
            for item, base in (("eps_diluted", 1.5 + k / 10), ("revenue", 1e9 * (1 + k / 10))):
                act = base * (1.05 if (k + q) % 3 else 0.97)
                for model, prim in (("sarima", True), ("seasonal_naive", False)):
                    ex.append({"issuer_id": x["issuer_id"], "item_id": item, "period_end": pe, "fiscal_period": fp,
                               "fiscal_year": pe.year, "model": model, "model_spec": "test", "is_primary": prim,
                               "expectation": base, "lo80": base * 0.9, "hi80": base * 1.1, "sigma": base * 0.08, "n_train": 20,
                               "currency": x["ccy"], "eps_basis": "diluted" if item == "eps_diluted" else None, "derived_q4": False,
                               "origin_date": rel - timedelta(days=1), "release_date": rel, "actual": act,
                               "actual_knowledge_date": rel + timedelta(days=5), "surprise": act - base,
                               "surprise_pct": (act - base) / base, "surprise_z": (act - base) / (base * 0.08), "status": "released",
                               "label": EC.LABEL_EXPECTATION, "knowledge_date": rel + timedelta(days=5), "computed_at": now})
            pe = EC.add_months_end(pe, 3)
        src = srcs[k % 3]
        exp_d = as_of + timedelta(days=2 + k % 10)
        cal.append({"issuer_id": x["issuer_id"], "security_id": x["security_id"], "region": x["region"], "country": x["country"],
                    "sector_id": x["sector_id"], "period_end": pe, "fiscal_period": "Q3", "expected_date": exp_d,
                    "session": "post_market", "window_lo": exp_d - timedelta(days=3) if src == "model_projection" else None,
                    "window_hi": exp_d + timedelta(days=3) if src == "model_projection" else None, "date_source": src,
                    "source_id": {"model_projection": "cadence_model", "provider_estimate": "yahoo", "company_announced": "jpx_schedule"}[src],
                    "source_label": EC.DATE_SOURCE_LABEL[src], "usual_session": "post_market", "n_history": 8, "first_seen_date": as_of,
                    "computed_at": now})
        ex.append({"issuer_id": x["issuer_id"], "item_id": "eps_diluted", "period_end": pe, "fiscal_period": "Q3", "fiscal_year": pe.year,
                   "model": "sarima", "is_primary": True, "expectation": 1.6, "lo80": 1.4, "hi80": 1.8, "sigma": 0.15,
                   "origin_date": as_of, "release_date": exp_d, "status": "pending", "label": EC.LABEL_EXPECTATION,
                   "knowledge_date": as_of, "computed_at": now})
        for d, typ, title in ((as_of - timedelta(days=40), "dividend_ex", "Dividend ex-date 0.25 USD"),
                              (as_of + timedelta(days=5), "dividend_ex", "Dividend ex-date 0.25 USD"),
                              (as_of - timedelta(days=10), "exec_change", "Executive / director change (8-K 5.02)")):
            ce.append({"event_id": f"{typ}:{x['security_id']}:{d}", "year": d.year, "issuer_id": x["issuer_id"],
                       "security_id": x["security_id"], "event_date": d, "event_type": typ, "title": title,
                       "amount": 0.25 if "dividend" in typ else None, "currency": "USD", "source_id": "polygon",
                       "source_label": "Corporate actions (polygon)", "knowledge_date": d - timedelta(days=20), "computed_at": now})
    out = {"earnings_events": EC.write(pl.DataFrame(ev, infer_schema_length=None), "earnings_events", s, mode="overwrite"),
           "earnings_reactions": EC.write(pl.DataFrame(rx, infer_schema_length=None), "earnings_reactions", s, mode="overwrite"),
           "earnings_expectations": EC.write(pl.DataFrame(ex, infer_schema_length=None), "earnings_expectations", s, mode="overwrite"),
           "earnings_calendar": EC.write(pl.DataFrame(cal, infer_schema_length=None), "earnings_calendar", s, mode="overwrite"),
           "corporate_events": EC.write(pl.DataFrame(ce, infer_schema_length=None), "corporate_events", s, mode="overwrite")}
    from app.earnings.signals import build_season, primary_surprises

    evd = pl.DataFrame(ev, infer_schema_length=None).select("issuer_id", "security_id", "region", "country", "period_end", "release_date")
    season = build_season(evd, pl.DataFrame(cal, infer_schema_length=None).select("issuer_id", "region", "period_end", "expected_date"),
                          primary_surprises(s), pl.DataFrame(rx, infer_schema_length=None), {x["issuer_id"]: x["sector_id"] for x in secs}, as_of)
    out["earnings_season"] = EC.write(season.with_columns(pl.lit(now).alias("computed_at")), "earnings_season", s, mode="overwrite")
    return out



XA_ASSETS = ("CL", "GC", "TTF", "SPX", "EURUSD", "USDJPY", "UST10Y", "US_HY", "BTC")


def build_cross_asset(s: Settings, as_of: date, seed: int = 7) -> dict[str, int]:
    """Synthetic cross-asset lake (roadmap #5): signals for 9 registry assets over 600 weekdays (positioning through the
    real CFTC join on the fixture's cftc_signals: CL <- 067651, SPX <- 13874A), Yahoo-style curves for CL / GC (GC with
    a missing month), contract coverage, regime + risk composite and correlation matrices of the 12-asset basket."""
    from app.analytics import cross_asset as ca
    from app.ingestion.futures_curve import ROOT_BY_ID, expiry, listed_contracts

    rng = np.random.default_rng(seed + 55)
    now = datetime.now(UTC)
    days = _bdays(as_of - timedelta(days=880), as_of)[-600:]
    assets = {a.asset_id: a for a in ca.build_assets()}
    carry_m = {"commodity": "futures_roll", "crypto": "futures_roll", "fx": "fx_rate_diff", "equity_index": "equity_div_funding",
               "rates": "bond_carry_rolldown", "credit": "credit_oas"}
    value_m = {"commodity": "reversal_5y", "fx": "fx_real_5y", "equity_index": "reversal_5y", "rates": "real_yield", "credit": "oas_level"}
    rows = []
    for aid in XA_ASSETS:
        a = assets[aid]
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
        src = "official_fx" if a.asset_class == "fx" else "official_ust_synthetic" if a.asset_class == "rates" else "yahoo"
        for i, d in enumerate(days):
            curve = a.futures_root in ("CL", "GC")
            slope = float(rng.normal(0.02, 0.05)) if curve else None
            rows.append({
                "asset_id": aid, "asset_class": a.asset_class, "sector": a.sector, "date": d, "futures_root": a.futures_root,
                "price": float(px[i]), "price_source": src, "mom_1m": float(rng.normal(0, 0.05)), "mom_3m": float(rng.normal(0, 0.08)),
                "mom_12m": float(rng.normal(0.03, 0.15)), "mom_12_1": float(rng.normal(0.03, 0.14)),
                "trend_signal": float(np.clip(rng.normal(0, 0.4), -1, 1)), "rv_1m": float(abs(rng.normal(0.2, 0.05))),
                "rv_3m": float(abs(rng.normal(0.2, 0.04))), "iv": 0.22 if aid in ("SPX", "CL") else None,
                "iv_source": "^VIX" if aid == "SPX" else "^OVX" if aid == "CL" else None,
                "iv_units": "fraction" if aid in ("SPX", "CL") else None,
                "iv_rv_ratio": 1.1 if aid in ("SPX", "CL") else None, "carry": float(rng.normal(0, 0.05)),
                "carry_method": carry_m[a.asset_class], "carry_curve": -slope if curve else None,
                "carry_12m": -slope if curve else None, "carry_fundamental": None if curve else float(rng.normal(0, 0.02)),
                "carry_front_contract": "Z26" if curve else None, "carry_dte_front": 40 if curve else None,
                "carry_dte_next": 70 if curve else None, "curve_slope": slope,
                "curve_shape": None if slope is None else "contango" if slope > 0.01 else "backwardation" if slope < -0.01 else "flat",
                "curve_n": 6 if curve else None, "value": float(rng.normal(0, 0.2)) if a.asset_class in value_m else None,
                "value_method": value_m.get(a.asset_class),
                **{z: float(rng.normal(0, 1)) for z in ("z_mom_12m", "z_carry", "z_value", "z_rv", "z_iv_rv")},
                "z_curve_slope": float(rng.normal(0, 1)) if curve else None,
                "knowledge_date": d + timedelta(days=1) if a.asset_class == "rates" else d, "computed_at": now, "run_id": "Rtest"})
    sig = pl.DataFrame(rows, infer_schema_length=None)
    xa = [assets[x] for x in XA_ASSETS]
    sig = ca.join_positioning(sig, ca.load_cot(xa, s), xa)
    out = {"cross_asset_signals": write_table(sig, "cross_asset_signals", mode="overwrite", settings=s)}

    # futures curves (CL 8 contracts, GC 6 with one listed month missing) over the last 400 weekdays
    curve_rows, cov = [], []
    for root, missing in (("CL", None), ("GC", 2)):
        r = ROOT_BY_ID[root]
        start = days[-400]
        live = [c for c in listed_contracts(r, start, 80) if c["expiry"] <= as_of + timedelta(days=420)]
        fwd = [c["symbol"] for c in live if c["expiry"] > as_of]
        gap = fwd[missing] if missing is not None else None
        for k, c in enumerate(live):
            served = c["symbol"] != gap
            first = None
            last = None
            nb = 0
            for d in days[-400:]:
                if not served or d > c["expiry"] or (c["expiry"] - d).days > 400:
                    continue
                dte = (c["expiry"] - d).days
                close = 70.0 * np.exp(0.03 * dte / 365) if root == "CL" else 2400.0 * np.exp(0.045 * dte / 365)
                curve_rows.append({"root": root, "contract_month": c["contract_month"], "date": d, "contract": c["contract"],
                                   "symbol": c["symbol"], "expiry": c["expiry"], "days_to_expiry": dte, "open": close,
                                   "high": close, "low": close, "close": float(close), "volume": 1000 + k, "source": "yahoo",
                                   "knowledge_date": d, "ingested_at": now})
                first = first or d
                last = d
                nb += 1
            status = "expired" if c["expiry"] < as_of else "served" if served else "missing"
            cov.append({"root": root, "contract_month": c["contract_month"], "contract": c["contract"], "symbol": c["symbol"],
                        "root_name": r.name, "asset_class": r.asset_class, "exchange": r.exchange, "expiry": c["expiry"],
                        "expiry_rule": "test rule", "status": status, "first_date": first, "last_date": last,
                        "n_bars": nb or None, "last_close": None, "last_checked": as_of, "updated_at": now})
        assert expiry(r, live[0]["contract_month"].year, live[0]["contract_month"].month) == live[0]["expiry"]
    out["futures_curve"] = write_table(pl.DataFrame(curve_rows, infer_schema_length=None), "futures_curve", mode="overwrite", settings=s)
    out["futures_contracts"] = write_table(pl.DataFrame(cov, infer_schema_length=None), "futures_contracts", mode="overwrite", settings=s)

    # regime + risk composite; correlation snapshots of the fixed basket
    basket = list(ca.CORR_BASKET)
    reg = []
    for i, d in enumerate(days):
        comps = {c: float(np.clip(rng.normal(0, 1), -3, 3)) for c in ca.RISK_COMPONENTS}
        if i < 300:
            comps["comp_hy_oas"] = None
        vals = [v for v in comps.values() if v is not None]
        rc = float(np.mean(vals))
        reg.append({"date": d, "eig1_share_60": float(rng.uniform(0.2, 0.5)), "avg_corr_60": float(rng.uniform(0, 0.3)),
                    "eig1_share_120": float(rng.uniform(0.2, 0.5)) if i >= 60 else None, "avg_corr_120": 0.15,
                    "eig1_z_120": float(rng.normal(0, 1)) if i >= 252 else None, "n_assets": len(basket),
                    "basket": ",".join(basket), "risk_composite": rc,
                    "risk_regime": "risk_on" if rc > 0.5 else "risk_off" if rc < -0.5 else "neutral",
                    "risk_n_components": len(vals), **comps, "knowledge_date": d, "computed_at": now, "run_id": "Rtest"})
    out["cross_asset_regime"] = write_table(pl.DataFrame(reg, infer_schema_length=None), "cross_asset_regime", mode="overwrite", settings=s)
    R = rng.normal(0, 0.01, (700, len(basket)))
    R[:, 1:4] += R[:, [0]]
    snaps = ca.corr_snapshots([as_of - timedelta(days=700 - k) for k in range(700)], R, basket, now=now)
    out["cross_asset_corr"] = write_table(snaps, "cross_asset_corr", mode="overwrite", settings=s)
    return out
