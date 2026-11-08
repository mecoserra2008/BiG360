"""Synthetic smart-money inputs (roadmap #7) for the serving fixture: 13F holdings of 6 quarters (10 active
hedge-fund-like books + 20 stable index-like books, options), FINRA short-interest history, fails-to-deliver,
13D / 13G filings (incl. a 13G -> 13D switch), EU / UK short disclosures (individual + aggregate); then the real
analytics (app.analytics.smart_money_13f / smart_money) compute inst_filers, inst_ownership, si_signals,
activist_events and smart_money_signals on them. Called from tests.fixtures.serving_small.build_lake."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table

QUARTERS = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31),
            date(2026, 6, 30)]


def cusip_of(i: int) -> str:
    return f"{100000 + i:06d}10{i % 10}"


def build_smart_money(s: Settings, as_of: date, secs: list[dict[str, Any]], seed: int = 7) -> dict[str, int]:
    rng = np.random.default_rng(seed + 77)
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    us = [x for x in secs if x["region"] == "US"][:60]
    eu = [x for x in secs if x["region"] in ("EU", "UK")][:14]
    out: dict[str, int] = {}

    # ---- 13F holdings
    rows = []
    for f in range(30):
        hf = f < 10
        cik = f"{9000000 + f:010d}"
        name = f"Alpha Fund {f} LP" if hf else f"Index Advisors {f} LLC"
        # min(): small fixture lakes (events tests: 30 securities) have fewer than 50 US names
        book = list(rng.choice(len(us), size=min(20 if hf else 50, len(us)), replace=False))
        base = {k: float(rng.lognormal(12 if hf else 13, 0.6)) for k in range(len(us))}
        for qi, pe in enumerate(QUARTERS):
            if hf and qi:  # replace ~half of the book each quarter (high turnover)
                keep = book[: len(book) // 2]
                rest = [k for k in range(len(us)) if k not in keep]
                book = keep + list(rng.choice(rest, size=min(len(book) - len(keep), len(rest)), replace=False))
            kd = pe + timedelta(days=int(rng.integers(20, 44)))
            for k in book:
                x = us[int(k)]
                sh = base[int(k)] * (1 + 0.05 * qi) * (float(rng.uniform(0.7, 1.4)) if hf else 1.0)
                rows.append({"filer_cik": cik, "filer_name": name, "period_end": pe, "period_end_year": pe.year,
                             "cusip": cusip_of(int(k)), "security_id": x["security_id"], "put_call": "none",
                             "shares": round(sh), "value_usd": round(sh * 50.0, 2),
                             "filing_id": f"sec_edgar:{cik}-{pe:%y}-{qi:06d}", "knowledge_date": kd, "ingested_at": now,
                             "issuer_name": x["name"].upper(), "title_of_class": "COM", "amount_type": "SH"})
            if hf:
                for k in book[:2]:
                    rows.append({"filer_cik": cik, "filer_name": name, "period_end": pe, "period_end_year": pe.year,
                                 "cusip": cusip_of(int(k)), "security_id": us[int(k)]["security_id"], "put_call": "put",
                                 "shares": 1000.0, "value_usd": 5.0e5, "filing_id": f"sec_edgar:{cik}-{pe:%y}-{qi:06d}",
                                 "knowledge_date": kd, "ingested_at": now, "issuer_name": "X", "title_of_class": "PUT",
                                 "amount_type": "SH"})
    out["institutional_holdings"] = write_table(pl.DataFrame(rows, infer_schema_length=None), "institutional_holdings",
                                                mode="overwrite", settings=s)

    # ---- FINRA short interest history (6 earlier settlements; the fixture already has the latest one) + FTD
    si, ftd = [], []
    for j in range(1, 7):
        sd = as_of - timedelta(days=12 + 15 * j)
        for i, x in enumerate(us):
            si.append({"security_id": x["security_id"], "settlement_date": sd, "year": sd.year,
                       "short_interest_shares": float(1e6 * (1 + i % 7) * (1 - 0.05 * j)), "avg_daily_volume": 2e5,
                       "days_to_cover": float(1 + i % 9), "knowledge_date": sd + timedelta(days=10), "source_id": "finra",
                       "ingested_at": now, "symbol": x["ticker"], "security_id_resolved": True,
                       "knowledge_date_estimated": False})
    out["short_interest"] = write_table(pl.DataFrame(si, infer_schema_length=None), "short_interest", settings=s)
    for half in range(4):
        end = as_of - timedelta(days=20 + 15 * half)
        for i, x in enumerate(us[:15]):
            for dd in range(3):
                d = end - timedelta(days=dd)
                ftd.append({"cusip": cusip_of(i), "settlement_date": d, "year": d.year, "symbol": x["ticker"],
                            "description": x["name"], "quantity_fails": float(5e6 if (i == 0 and half == 0) else 1e3 * (1 + dd)),
                            "price": 50.0, "security_id": x["security_id"], "file_name": f"cnsfails_fx{half}",
                            "knowledge_date": end + timedelta(days=5), "knowledge_date_estimated": False,
                            "source_id": "sec_ftd", "ingested_at": now})
    out["fails_to_deliver"] = write_table(pl.DataFrame(ftd, infer_schema_length=None), "fails_to_deliver",
                                          mode="overwrite", settings=s)

    # ---- 13D / 13G
    af = []

    def filing(acc: str, form: str, d: date, x: dict[str, Any], filer: str, fcik: str) -> dict[str, Any]:
        return {"filing_id": f"sec_edgar:{acc}", "year": d.year, "accession": acc, "form_type": form,
                "schedule": "13D" if "13D" in form else "13G", "is_amendment": form.endswith("/A"), "filed_date": d,
                "knowledge_date": d, "subject_cik": f"{320193 + x['i']:010d}", "subject_name": x["name"], "filer_cik": fcik,
                "filer_name": filer, "n_filers": 1, "role_source": "ticker_map", "issuer_id": x["issuer_id"],
                "security_id": x["security_id"], "url": f"https://www.sec.gov/Archives/edgar/data/1/{acc}-index.html",
                "source_id": "sec_edgar", "ingested_at": now}

    for k, x in enumerate(us[:8]):
        af.append(filing(f"0001111111-25-{k:06d}", "SC 13G", as_of - timedelta(days=300), x, "Vanguard Group Inc", "0000102909"))
        af.append(filing(f"0001111111-26-{k:06d}", "SCHEDULE 13G/A", as_of - timedelta(days=40), x, "Vanguard Group Inc", "0000102909"))
    af.append(filing("0002222222-25-000001", "SC 13G", as_of - timedelta(days=200), us[2], "Starboard Value LP", "0001517137"))
    af.append(filing("0002222222-26-000002", "SCHEDULE 13D", as_of - timedelta(days=20), us[2], "Starboard Value LP", "0001517137"))
    af.append(filing("0003333333-26-000003", "SCHEDULE 13D", as_of, us[0], "Elliott Investment Management L.P.", "0001791786"))
    af.append(filing("0003333333-26-000004", "SCHEDULE 13D/A", as_of - timedelta(days=3), us[3], "Trian Fund Management, L.P.", "0001345471"))
    out["activist_filings"] = write_table(pl.DataFrame(af, infer_schema_length=None), "activist_filings",
                                          mode="overwrite", settings=s)

    # ---- EU / UK short positions
    es = []
    for k, x in enumerate(eu):
        reg = "fca" if x["region"] == "UK" else "afm"
        isin = f"{x['country']}{x['i']:09d}{x['i'] % 7}"
        for h, holder in enumerate(("Marshall Wace LLP", "Citadel Advisors LLC", "Qube Research & Technologies Ltd")):
            for j in range(3):
                pdte = as_of - timedelta(days=60 - 25 * j + h)
                pct = round(0.45 + 0.15 * (k % 4) + 0.1 * j + 0.05 * h, 2)
                es.append({"regulator": reg, "country": x["country"], "position_type": "individual", "holder_name": holder,
                           "holder_lei": None, "issuer_name": x["name"], "isin": isin, "issuer_lei": None,
                           "issuer_id": x["issuer_id"], "security_id": x["security_id"], "position_pct": pct,
                           "below_threshold": pct < 0.5, "shares": None, "position_date": pdte, "publication_date": None,
                           "knowledge_date": min(pdte + timedelta(days=2), as_of), "knowledge_date_estimated": True,
                           "in_current_list": j == 2, "source_url": "https://example.org/register", "source_id": "eu_short",
                           "ingested_at": now})
        if reg == "fca" and k % 2 == 0:
            es.append({"regulator": "fca", "country": x["country"], "position_type": "aggregate", "holder_name": "(aggregate)",
                       "issuer_name": x["name"], "isin": isin, "issuer_id": x["issuer_id"], "security_id": x["security_id"],
                       "position_pct": 2.1, "below_threshold": False, "position_date": as_of - timedelta(days=5),
                       "knowledge_date": as_of - timedelta(days=3), "knowledge_date_estimated": True, "in_current_list": True,
                       "source_url": "https://example.org/agg", "source_id": "eu_short", "ingested_at": now})
    out["eu_short_positions"] = write_table(pl.DataFrame(es, infer_schema_length=None), "eu_short_positions",
                                            mode="overwrite", settings=s)

    # ---- analytics (the real code paths)
    from app.analytics import smart_money as SM
    from app.analytics import smart_money_13f as S13

    con = S13.connect(s, memory="1GB")
    try:
        filers, inst = S13.compute(con, as_of, min_filers=10)
    finally:
        con.close()
    out["inst_filers"] = write_table(filers.with_columns(pl.lit(now).alias("computed_at")), "inst_filers", settings=s)
    out["inst_ownership"] = write_table(inst.with_columns(pl.lit(now).alias("computed_at")), "inst_ownership", settings=s)
    con = SM.connect(s, memory="1GB")
    try:
        sig_si = SM.si_signals(con, as_of)
        out["si_signals"] = write_table(sig_si.with_columns(pl.lit(now).alias("computed_at")), "si_signals",
                                        mode="overwrite", settings=s)
        ev = SM.activist_events(con, as_of)
        out["activist_events"] = write_table(ev.with_columns(pl.lit(now).alias("computed_at")), "activist_events",
                                             mode="overwrite", settings=s)
        sig = SM.composite(con, sig_si, SM.signal_dates(as_of, as_of - timedelta(days=60)))
    finally:
        con.close()
    sig = sig.select([c for c in SM.SIGNAL_COLS if c in sig.columns]).with_columns(pl.lit(now).alias("computed_at"))
    out["smart_money_signals"] = write_table(sig, "smart_money_signals", settings=s)
    return out
