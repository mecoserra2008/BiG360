"""Synthetic W3 rates & debt lake tables for the API tests (rates-w3). Uses the real code paths: Svensson
reconstruction (app.rates.sources.svensson_rows), the curve frame builder (app.rates.curves_job.frames), the auction
parsers, the company_debt / walls builders (app.rates.company_debt) and the FIRDS collapse."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table


def _bdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_rates(s: Settings, as_of: date, secs: list[dict[str, Any]], seed: int = 7) -> dict[str, int]:
    from app.rates import company_debt as CD
    from app.rates import registry as R
    from app.rates.auctions import parse_fiscaldata_auction, to_frame
    from app.rates.bonds import SCHEMA as BOND_SCHEMA
    from app.rates.common import curve_rows
    from app.rates.curves_job import frames
    from app.rates.sources import Loaded, svensson_rows

    rng = np.random.default_rng(seed)
    today = date.today()
    days = _bdays(today - timedelta(days=3 * 365), today - timedelta(days=1))
    counts: dict[str, int] = {}
    res = Loaded()
    # Svensson curves (EA_AAA, DE_BUND) from drifting parameters
    for cid, src, b0 in (("EA_AAA", "ecb", 2.6), ("DE_BUND", "bundesbank", 2.7)):
        prm = {}
        lvl = b0
        for d in days[::5]:
            lvl += rng.normal(0, 0.02)
            prm[d] = {"BETA0": lvl, "BETA1": -0.8, "BETA2": 1.5, "BETA3": -1.0, "TAU1": 1.2, "TAU2": 9.0}
        res.extend(svensson_rows(cid, src, prm, days[0]))
    # par / spot curves on their own grids
    for cid, typ, base, slope in (("US_TSY", "par", 3.9, 0.06), ("US_TSY_REAL", "par", 1.8, 0.03),
                                  ("GB_GILT", "spot", 4.0, 0.05), ("GB_GILT_REAL", "spot", 0.9, 0.04),
                                  ("JP_JGB", "par", 1.2, 0.08), ("EA_EURIBOR", "yield", 2.1, 0.02)):
        c = R.BY_ID[cid]
        lvl = 0.0
        pts = []
        for d in days[::5]:
            lvl += rng.normal(0, 0.03)
            pts += [(d, m, base + lvl + slope * np.log1p(m / 12)) for m in c.tenors]
        res.rows += curve_rows(cid, c.kind, c.source_id, typ, pts)
    for cc in ("IT", "FR"):
        res.rows += curve_rows(f"{cc}_LT10", "nominal", "ecb_irs", "yield",
                               [(date(d.year, d.month, 1), 120, 3.4 + 0.1 * k) for k, d in enumerate(days[::21])])
    df, pf = frames(res, today)
    counts["yield_curves"] = write_table(df, "yield_curves", mode="overwrite", settings=s)
    counts["yield_curve_params"] = write_table(pf, "yield_curve_params", mode="overwrite", settings=s)
    # money markets + OAS in macro_observations (appended to the fixture's macro rows)
    now = datetime.now(UTC)
    mo = []
    for sid, lvl in (("nyfed_rates:SOFR", 4.3), ("nyfed_rates:EFFR", 4.33), ("ecb:EST.B.EU000A2X2A25.WT", 1.9),
                     ("boe:IUDSOIA", 3.95), ("fred:BAMLC0A0CM", 0.85), ("fred:BAMLC0A4CBBB", 1.1), ("fred:DBAA", 5.9),
                     ("fred:BAMLH0A3HYC", 8.5), ("eurostat:gov_10q_ggdebt.IT.PC_GDP", 135.0),
                     ("ecb:TGB.M.DE.N.A094T.U2.EUR.E", 1.05e6)):
        monthly = sid.startswith(("eurostat", "ecb:TGB"))
        ds = sorted({date(d.year, d.month, 1) for d in days}) if monthly else days
        path = lvl + np.cumsum(rng.normal(0, abs(lvl) * 0.003, len(ds)))
        mo += [{"series_id": sid, "source_id": sid.split(":")[0], "date": d, "value": float(v),
                "knowledge_date": d + timedelta(days=1), "knowledge_date_estimated": False, "ingested_at": now}
               for d, v in zip(ds, path, strict=True)]
    counts["macro_observations+w3"] = write_table(pl.DataFrame(mo), "macro_observations", mode="upsert", settings=s)
    # auctions (Fiscal Data parser) incl. one announced auction
    auc = []
    for k, d in enumerate(days[-400::10]):
        auc.append(parse_fiscaldata_auction({"cusip": f"91282C{k:03d}", "security_type": "Note", "security_term": "10-Year",
                                             "auction_date": d.isoformat(), "issue_date": (d + timedelta(days=7)).isoformat(),
                                             "maturity_date": (d + timedelta(days=3650)).isoformat(), "int_rate": "4.25",
                                             "high_yield": f"{4.1 + 0.01 * k:.3f}", "offering_amt": "42000000000",
                                             "total_tendered": "100000000000", "total_accepted": "42000000000",
                                             "bid_to_cover_ratio": "2.45", "comp_accepted": "41000000000",
                                             "indirect_bidder_accepted": "28000000000", "direct_bidder_accepted": "7000000000",
                                             "primary_dealer_accepted": "6000000000", "announcemt_date": (d - timedelta(days=7)).isoformat(),
                                             "auction_format": "Single-Price", "reopening": "No"}))
    auc.append(parse_fiscaldata_auction({"security_type": "Bill", "security_term": "13-Week",
                                         "auction_date": (today + timedelta(days=3)).isoformat(),
                                         "announcemt_date": today.isoformat(), "offering_amt": "80000000000"}))
    counts["sovereign_auctions"] = write_table(to_frame([a for a in auc if a]), "sovereign_auctions", mode="overwrite", settings=s)
    # bond reference: a few FIRDS bonds for the first issuers + one OpenFIGI bond
    br = []
    for k, x in enumerate(secs[:6]):
        for j in range(3):
            br.append({"source": "firds", "bond_id": f"XS{k:02d}{j:08d}", "isin": f"XS{k:02d}{j:08d}", "figi": None,
                       "issuer_lei": f"LEI{k:017d}", "issuer_id": x["issuer_id"], "issuer_name": x["name"],
                       "full_name": f"{x['name']} {3 + j}% {2028 + j}", "short_name": None, "cfi": "DBFTFB",
                       "security_type": "bond", "currency": "EUR", "coupon_type": "fixed", "coupon_bp_x100": 30000 + 2500 * j,
                       "floating_reference": None, "floating_spread_bp": None, "maturity_date": date(2028 + j, 6, 15),
                       "first_trade_date": today - timedelta(days=200 * (j + 1)), "issue_amount": 5e8,
                       "issue_amount_currency": "EUR", "nominal_per_unit": 1000.0, "seniority": "senior", "venue_mic": "XLUX",
                       "n_venues": 2, "country": "XS", "as_of": today, "knowledge_date": today})
    br.append({**br[0], "source": "openfigi", "bond_id": "BBG000TEST01", "isin": None, "figi": "BBG000TEST01",
               "venue_mic": "TRACE", "currency": "USD"})
    counts["bond_reference"] = write_table(pl.DataFrame(br, schema=BOND_SCHEMA).with_columns(pl.lit(now).alias("ingested_at")),
                                           "bond_reference", mode="overwrite", settings=s)
    # company debt from a synthetic canonical panel + tagged maturity profiles (real builders)
    pan, prof = [], []
    for k, x in enumerate(secs[:30]):
        for y in (2023, 2024, 2025):
            pe = date(y, 12, 31)
            td = 1e9 * (1 + k % 5) * (1 + 0.05 * (y - 2023))
            pan.append({"issuer_id": x["issuer_id"], "period_end": pe, "total_debt": td, "short_term_debt": td * 0.15,
                        "long_term_debt": td * 0.85, "cash_and_equivalents": td * 0.3, "net_debt": None,
                        "ebit": td * 0.25, "ebitda": td * 0.35, "interest_expense": td * 0.045, "currency": "USD",
                        "fx_to_usd": 1.0, "knowledge_date": date(y + 1, 2, 20), "knowledge_date_estimated": False,
                        "period_kind": "FY"})
            if k % 3 == 0:
                prof.append({"issuer_id": x["issuer_id"], "mat_period_end": pe, "mat_y1": td * 0.1, "mat_y2": td * 0.12,
                             "mat_y3": td * 0.15, "mat_y4": td * 0.13, "mat_y5": td * 0.1, "mat_after5": td * 0.4,
                             "mat_y2_5": None, "fixed_debt": td * 0.7, "floating_debt": td * 0.3,
                             "weighted_avg_rate": 0.042, "mat_source": "us_gaap", "mat_kd": date(y + 1, 2, 20)})
    cd = CD.build_company_debt(pl.DataFrame(pan), pl.DataFrame(prof),
                               pl.DataFrame(br).select("issuer_id", "maturity_date"),
                               {"USD": 0.040, "EUR": 0.025, "IG": 0.009, "HY": 0.031})
    counts["company_debt"] = write_table(cd, "company_debt", mode="overwrite", settings=s)
    meta = pl.DataFrame([{"issuer_id": x["issuer_id"], "region": x["region"], "sector_id": x["sector_id"]} for x in secs])
    walls = CD.build_walls(cd, meta, today)
    counts["debt_maturity_walls"] = write_table(walls, "debt_maturity_walls", mode="overwrite", settings=s)
    return counts
