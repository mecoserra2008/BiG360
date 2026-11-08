"""BQL fields of W3 (rates-w3): per-security rates context (serving table rates_sec: the domicile country's benchmark
government curve, spreads vs Bund / UST, US IG / HY OAS). The issuer debt fields (namespace debt.*) are generated from
the company_debt contract by app.bql.fields NAMESPACES["debt"]."""

from __future__ import annotations

from typing import Any

PIT = "snapshot value (latest official curve at build time; not point-in-time)"
SRC = "yield_curves"
#: column -> (mnemonic, unit, description)
RATE_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("gov_2y", "rates.gov_2y", "pct", "2y government yield of the security's domicile country (benchmark curve)"),
    ("gov_5y", "rates.gov_5y", "pct", "5y government yield of the domicile country"),
    ("gov_10y", "rates.gov_10y", "pct", "10y government yield of the domicile country (daily curve, else monthly OECD / ECB)"),
    ("gov_30y", "rates.gov_30y", "pct", "30y government yield of the domicile country"),
    ("curve_2s10s_bp", "rates.curve_2s10s", "bps", "Domicile-country curve slope 10y - 2y (bp)"),
    ("spread_10y_vs_bund_bp", "rates.spread_10y_vs_bund", "bps", "Domicile 10y yield minus the German 10y Bund (bp)"),
    ("spread_10y_vs_ust_bp", "rates.spread_10y_vs_ust", "bps", "Domicile 10y yield minus the 10y US Treasury (bp)"),
    ("us_ig_oas", "credit.us_ig_oas", "pct", "ICE BofA US Corporate (IG) OAS - FRED, personal use"),
    ("us_hy_oas", "credit.us_hy_oas", "pct", "ICE BofA US High Yield OAS - FRED, personal use"),
    ("rates_curve_id", "rates.curve_id", "text", "Benchmark curve used for the domicile country (app.rates.registry)"),
    ("rates_date", "rates.curve_date", "date", "Date of the curve values"),
)


def rate_fields(schema: dict[str, dict[str, str]]) -> list[Any]:
    from app.bql.fields import FieldDef  # noqa: PLC0415

    cols = schema.get("rates_sec", {})
    out = []
    for col, mn, unit, desc in RATE_FIELDS:
        if col not in cols:
            continue
        dtype = "text" if unit == "text" else ("date" if unit == "date" else "number")
        out.append(FieldDef(mn, desc, unit, dtype, "rates", "rates_sec", col, "security_id", "snapshot", PIT,
                            "macro_observations" if mn.startswith("credit.") else SRC))
    return out
