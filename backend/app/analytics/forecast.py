"""Driver-based 3-statement forecast + DCF / excess-return valuation + reverse DCF (nightly job `forecast`).

Writes `forecasts` (long format), `forecast_valuation` (bear/base/bull + market_implied) and `cost_of_capital`.
Everything is point-in-time at ctx.as_of: fundamentals_wide rows with knowledge_date <= D, close <= D, rates <= D.

Non-financials (FCFF DCF), 5 annual years after the last reported FY:
* revenue growth: g1 = 0.5 * Theil-Sen trend of log revenue (<= 7y) + 0.5 * last-3y CAGR (clip -25%..+50%),
  fading linearly to g5 = 0.5 * subsector median 5y CAGR + 0.5 * terminal growth.
* margins are EBITA (EBIT + amortization of acquired intangibles; D&A driver = depreciation only). Regimes:
  normal: last FY -> 0.9 * (0.6 own 5y median + 0.4 last) + 0.1 subsector median; cyclical (cyclical subsectors or
  sigma > 0.5 |margin|): start = 0.5 last + 0.5 own 10y median, target = 10y median; loss-makers: target = 0.8 x
  median margin of *profitable* subsector peers. Speed cap 2pp/yr (4pp for loss-makers), continued in the fade
  stage. Gross margin is a display driver; opex% = GM - margin.
* D&A, capex, SBC, operating NWC as % revenue (own 3y medians, subsector fallback); tax = normalized rate
  (cost_of_capital); interest on average debt at kd, interest income on average cash at max(rf - 1%, 0)
  (small fixed-point iteration, so interest really is on *average* balances).
* dividends / buybacks = own 3y median payout of net income; debt held flat; cash is the plug with a revolver
  when cash would fall below 2% of revenue. Balance sheet: cash + NWC + fixed assets + other_net = debt +
  revolver + equity; identity asserted every year (other_net = opening plug, held constant).
* capex reaches depreciation + PP&E/revenue * g/(1+g) within 3 years.
* FCFF = NOPAT + D&A - capex - dNWC (SBC treated as a real cost). Discounting at WACC on fractional years from
  as_of to each FY end. Fade stage (5y): growth -> g, margin -> long-run target, reinvestment = dRevenue /
  sales-to-capital. Terminal: NOPAT * (1 - g / RONIC) / (WACC - g), RONIC = max(WACC, (ROIC + WACC) / 2) on
  operating capital (NWC + PP&E); tv_share = PV(TV)/EV, flagged > 85%.
* equity = EV + cash - debt - minorities - preferred; per share / latest shares; converted to the trading currency
  (FX at as_of; / adr_ratio for ADRs).
* scenarios: bear/bull shift the growth path by -/+ 1.28 * 0.6 * sigma(yoy growth) and the margin target by
  -/+ 1.28 * sigma(EBIT margin) (sigmas from own 10y history, floored/capped). Probabilities .25/.5/.25.
* reverse DCF: implied constant 5y revenue CAGR at base margins (Brent root), implied steady FCFF margin at base
  growth (closed form), percentile of the implied CAGR within own yoy growth history.
* sensitivity_grid: WACC -2..+2% (1% steps) x terminal g -1..+1% (0.5% steps).
Financials (banks, insurers, consumer finance, holdings, levered FINS): excess-return model
  value = BV0 + sum (ROE_t - ke) * BV_{t-1} / (1+ke)^t + RI6 / (ke - g); ROE fades to 0.6 own 5y median + 0.4
  subsector median; payout from history; reverse DCF = implied steady ROE (assumptions.implied_roe).
Reported fair value (both models) = blend in log space of the model value (weight 0.2-0.5 by model stability) and
  the peer-multiple value (EV/EBITA, P/E on normalized earnings, EV/Sales for loss-makers; banks P/E + P/B; only
  50% of the gap to peers assumed to close), then shrunk toward the price by credibility k = 0.4 + 0.6 * conf.
  valuation_confidence = 100 x data_quality x model_stability x DCF/multiples agreement (capped at 35 when
  |upside| > 100% or upside < -90%), with confidence_reasons. fair_value_dcf / fair_value_multiples /
  fair_value_low / fair_value_high keep the raw legs. ERP calibration (implied ERP) uses the model leg only.
Skips (flags in the job message): stale (last FY > 2y old), < 2 usable FY, no price/shares.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import orjson
import polars as pl
from scipy.optimize import brentq

from app.analytics import cost_of_capital as cc
from app.analytics.moat import robust_ebit, robust_invested_capital, target_issuers
from app.core.logsetup import get_logger
from app.core.storage import write_table

log = get_logger(__name__)

H = 5
FADE_YEARS = 5
MARGIN_SPEED = 0.02   # max EBITA-margin change per year (mean-reversion speed cap)
LOSS_SPEED = 0.04     # loss-makers converge faster toward the profitable-peer margin
ROE_SPEED = 0.02      # banks/insurers: max ROE change per year
CAPEX_FADE_YEARS = 3  # capex reaches the growth-consistent level within 3 years
TV_SHARE_FLAG = 0.85  # terminal value share of EV above which the DCF is flagged
CYCLICAL_SUBSECTORS = {"semiconductors", "semi_equipment", "metals_mining", "steel_aluminum", "precious_metals",
                       "chemicals_diversified", "integrated_oil_gas", "exploration_production", "refining_marketing",
                       "oilfield_services", "coal_uranium", "autos", "auto_parts", "airlines", "packaging_paper",
                       "construction_materials", "homebuilders", "agri_products", "trading_distributors"}
W_DCF_MIN, W_DCF_MAX = 0.2, 0.5  # DCF weight in the blend by model stability (rest = peer multiples)
MULT_CONVERGENCE = 0.5            # share of the gap to the peer-multiple value assumed to close
CRED_MIN = 0.4                    # credibility shrink of the log-upside: k = CRED_MIN + (1 - CRED_MIN) * conf
MIN_SPREAD = 0.03  # valuation guard: WACC (or ke) - terminal growth >= 3%
MIN_FY = 2  # usable fiscal years required (JP/EDINET history is often only 2-5y)
PROB = {"bear": 0.25, "base": 0.5, "bull": 0.25}
ITEMS_ACTUAL = ["revenue", "gross_profit", "ebit", "ebitda", "net_income", "net_income_parent", "eps_diluted", "cfo",
                "capex", "fcf", "depreciation_amortization", "sbc", "interest_expense", "income_tax", "dividends_paid",
                "buybacks", "total_debt", "cash_and_st_investments", "net_debt", "total_equity",
                "total_equity_parent", "operating_working_capital", "shares_diluted_weighted"]
Z = 1.28


# ------------------------------------------------------------------------------------------------ math helpers


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _med(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None and math.isfinite(x)]
    return float(np.median(v)) if v else None


def theil_sen_growth(years: list[int], revenue: list[float]) -> float | None:
    """exp(Theil-Sen slope of log revenue) - 1 over the given points (revenue > 0)."""
    pts = [(y, math.log(r)) for y, r in zip(years, revenue, strict=False) if r and r > 0]
    if len(pts) < 3:
        return None
    sl = [(b[1] - a[1]) / (b[0] - a[0]) for i, a in enumerate(pts) for b in pts[i + 1:] if b[0] != a[0]]
    return math.exp(float(np.median(sl))) - 1 if sl else None


def discount_times(last_fy_end: date, as_of: date, n: int = H) -> np.ndarray:
    """Fractional years from as_of to each forecast FY end (>= 0)."""
    ends = [date(last_fy_end.year + t, last_fy_end.month, min(last_fy_end.day, 28)) for t in range(1, n + 1)]
    return np.array([max((e - as_of).days / 365.25, 0.0) for e in ends])


def dcf_value(fcff: np.ndarray, tau: np.ndarray, wacc: float, g: float, terminal_cf: float) -> float | None:
    """EV = sum FCFF_t / (1+w)^tau_t + terminal_cf / (w - g) / (1+w)^tau_T."""
    if wacc <= g + 0.005:
        return None
    df = (1 + wacc) ** -tau
    return float((fcff * df).sum() + terminal_cf / (wacc - g) * df[-1])


def terminal_fcff(nopat_T: float, g: float, ronic: float) -> float:
    """Value-driver terminal cash flow: NOPAT_{T+1} * (1 - g / RONIC)."""
    reinv = g / ronic if ronic > 0 else 0.0
    return nopat_T * (1 + g) * (1 - min(max(reinv, 0.0), 1.0))


# ------------------------------------------------------------------------------------------------ 3-statement


@dataclass
class Drivers:
    growth: np.ndarray          # revenue growth per year (H)
    ebit_margin: np.ndarray     # H
    gross_margin: np.ndarray    # H (display)
    da_pct: float
    capex_pct: float | np.ndarray  # scalar or per-year path
    nwc_pct: float
    sbc_pct: float
    tax_rate: float
    kd: float
    cash_yield: float
    payout: float               # dividends / NI
    buyback: float              # buybacks / NI
    min_cash_pct: float = 0.02


@dataclass
class Opening:
    revenue: float
    cash: float
    debt: float
    equity: float
    nwc: float
    fixed: float                # net PPE + intangibles (excl. goodwill)
    shares: float
    price: float | None = None  # current price per share (reporting ccy) -> buyback share retirement
    price_growth: float = 0.08


def project(dr: Drivers, op: Opening, iters: int = 4) -> dict[str, np.ndarray]:
    """Linked IS/BS/CF projection. Returns arrays of length H. Raises AssertionError if the BS does not balance."""
    other_net = op.equity + op.debt - op.cash - op.nwc - op.fixed  # opening plug (goodwill, investments, other liabs)
    out: dict[str, list[float]] = {k: [] for k in (
        "revenue", "gross_profit", "ebit", "ebitda", "depreciation_amortization", "interest_expense",
        "interest_income", "pretax_income", "income_tax", "net_income", "sbc", "dividends_paid", "buybacks",
        "capex", "change_nwc", "cfo", "fcf", "fcff", "nopat", "cash_and_st_investments", "revolver", "total_debt",
        "total_equity", "operating_working_capital", "fixed_assets", "net_debt", "invested_capital",
        "operating_capital", "shares_diluted_weighted", "eps_diluted", "bs_imbalance")}
    rev0, cash0, rvl0, eq0, nwc0, fix0, sh = op.revenue, op.cash, 0.0, op.equity, op.nwc, op.fixed, op.shares
    cxp = np.broadcast_to(np.asarray(dr.capex_pct, float), (len(dr.growth),))
    for t in range(len(dr.growth)):
        rev = rev0 * (1 + dr.growth[t])
        ebit = rev * dr.ebit_margin[t]
        da = rev * dr.da_pct
        capex = rev * cxp[t]
        sbc = rev * dr.sbc_pct
        nwc = rev * dr.nwc_pct
        dnwc = nwc - nwc0
        fix = fix0 + capex - da
        cash, rvl = cash0, rvl0
        for _ in range(iters):  # interest on average balances (fixed point)
            int_exp = dr.kd * (op.debt + (rvl0 + rvl) / 2)
            int_inc = dr.cash_yield * (cash0 + cash) / 2
            pti = ebit - int_exp + int_inc
            tax = max(pti, 0.0) * dr.tax_rate
            ni = pti - tax
            div = max(ni, 0.0) * dr.payout
            bb = max(ni, 0.0) * dr.buyback
            cfo = ni + da + sbc - dnwc
            pre = cash0 + cfo - capex - div - bb - rvl0  # repay revolver first
            floor = dr.min_cash_pct * rev
            if pre >= floor:
                cash, rvl = pre, 0.0
            else:
                cash, rvl = floor, floor - pre
        eq = eq0 + ni + sbc - div - bb
        assets = cash + nwc + fix + other_net
        liab_eq = op.debt + rvl + eq
        imb = assets - liab_eq
        scale = max(abs(assets), abs(rev), 1.0)
        assert abs(imb) <= 1e-6 * scale, f"balance sheet does not balance: {imb}"
        nopat = ebit * (1 - dr.tax_rate) if ebit > 0 else ebit
        if bb > 0 and op.price:  # buybacks retire shares at a price drifting with the cost of equity
            sh = max(sh - bb / (op.price * (1 + op.price_growth) ** (t + 1)), 0.5 * op.shares)
        vals = {"revenue": rev, "gross_profit": rev * dr.gross_margin[t], "ebit": ebit, "ebitda": ebit + da,
                "depreciation_amortization": da, "interest_expense": int_exp, "interest_income": int_inc,
                "pretax_income": pti, "income_tax": tax, "net_income": ni, "sbc": sbc, "dividends_paid": div,
                "buybacks": bb, "capex": capex, "change_nwc": dnwc, "cfo": cfo, "fcf": cfo - capex,
                "fcff": nopat + da - capex - dnwc, "nopat": nopat, "cash_and_st_investments": cash, "revolver": rvl,
                "total_debt": op.debt + rvl, "total_equity": eq, "operating_working_capital": nwc,
                "fixed_assets": fix, "net_debt": op.debt + rvl - cash, "invested_capital": op.debt + rvl + eq - cash,
                "operating_capital": nwc + fix,
                "shares_diluted_weighted": sh, "eps_diluted": ni / sh if sh else float("nan"), "bs_imbalance": imb}
        for k, v in vals.items():
            out[k].append(v)
        rev0, cash0, rvl0, eq0, nwc0, fix0 = rev, cash, rvl, eq, nwc, fix
    return {k: np.array(v, float) for k, v in out.items()}


def value_fcff_detail(proj: dict[str, np.ndarray], tau: np.ndarray, wacc: float, g: float,
                      g_start: float | None = None, fade_years: int = FADE_YEARS, m_lr: float | None = None,
                      tax: float = 0.25, speed: float = MARGIN_SPEED) -> dict[str, float] | None:
    """Explicit FCFF (H years) + fade stage + value-driver terminal value.

    Fade stage (`fade_years`): growth moves linearly from g_start to g, the operating margin keeps converging toward
    the long-run margin m_lr at <= `speed` per year, reinvestment = delta revenue / sales-to-capital (revenue /
    operating capital of the last explicit year). Terminal: NOPAT_{T+1} * (1 - g / RONIC) / (WACC - g) with
    RONIC = max(WACC, (ROIC_T + WACC) / 2), ROIC on operating capital (NWC + PP&E; excludes goodwill/acquired
    intangibles). Returns ev, pv_explicit, pv_fade, pv_tv, tv_share, ronic, roic_T."""
    if wacc <= g + 0.005:
        return None
    rev = float(proj["revenue"][-1])
    m = float(proj["ebit"][-1] / rev) if rev > 0 else 0.0
    m_lr = m if m_lr is None else m_lr
    opc = float(proj["operating_capital"][-1])
    s2c = min(max(rev / opc, 0.3), 6.0) if opc > 0 else 3.0
    nopat_t = float(proj["nopat"][-1])
    roic_t = min(nopat_t / opc, 1.0) if opc > 0 else wacc + 0.10
    df = (1 + wacc) ** -tau
    pv_exp = float((proj["fcff"] * df).sum())
    gs = g if g_start is None else g_start
    pv_fade, nopat = 0.0, nopat_t
    for k in range(1, fade_years + 1):
        gk = gs + (g - gs) * k / fade_years
        m = m + min(max(m_lr - m, -speed), speed)
        new_rev = rev * (1 + gk)
        ebit = new_rev * m
        nopat = ebit * (1 - tax) if ebit > 0 else ebit
        fcff = nopat - max(new_rev - rev, 0.0) / s2c
        opc += max(new_rev - rev, 0.0) / s2c
        rev = new_rev
        pv_fade += fcff * df[-1] * (1 + wacc) ** -k
    roic_end = min(nopat / opc, 1.0) if opc > 0 else roic_t
    ronic = max(wacc, (roic_end + wacc) / 2)
    tcf = terminal_fcff(nopat, g, ronic) if nopat > 0 else nopat * (1 + g)
    pv_tv = tcf / (wacc - g) * df[-1] * (1 + wacc) ** -fade_years
    ev = pv_exp + pv_fade + pv_tv
    return {"ev": ev, "pv_explicit": pv_exp, "pv_fade": pv_fade, "pv_tv": pv_tv, "ronic": ronic,
            "roic_t": roic_t, "tv_share": pv_tv / ev if ev > 0 else float("nan")}


def value_fcff(proj: dict[str, np.ndarray], tau: np.ndarray, wacc: float, g: float, roic_hint: float | None = None,
               g_start: float | None = None, fade_years: int = FADE_YEARS, m_lr: float | None = None,
               tax: float = 0.25) -> tuple[float | None, float]:
    """(EV, RONIC) - see value_fcff_detail (roic_hint kept for API compatibility, unused)."""
    d = value_fcff_detail(proj, tau, wacc, g, g_start=g_start, fade_years=fade_years, m_lr=m_lr, tax=tax)
    return (None, wacc) if d is None else (d["ev"], d["ronic"])


def excess_return_value(bv0: float, roe: np.ndarray, payout: float, ke: float, g: float, tau: np.ndarray
                        ) -> tuple[float | None, dict[str, np.ndarray]]:
    """Residual-income equity value and the book-value path."""
    if ke <= g + 0.005:
        return None, {}
    bv, out = bv0, {k: [] for k in ("net_income", "dividends_paid", "total_equity_parent", "residual_income")}
    for r in roe:
        ni = r * bv
        dist = max(ni, 0.0) * payout
        out["residual_income"].append((r - ke) * bv)
        bv = bv + ni - dist
        out["net_income"].append(ni)
        out["dividends_paid"].append(dist)
        out["total_equity_parent"].append(bv)
    arr = {k: np.array(v, float) for k, v in out.items()}
    df = (1 + ke) ** -tau
    g_t = min(g, max(roe[-1] * (1 - payout), 0.0))
    ri6 = (roe[-1] - ke) * bv
    val = bv0 + float((arr["residual_income"] * df).sum()) + ri6 / (ke - g_t) * df[-1]
    return val, arr


def implied_root(fn, lo: float, hi: float) -> float | None:
    """Brent root of fn on [lo, hi]; None when not bracketed."""
    try:
        a, b = fn(lo), fn(hi)
        if a is None or b is None or not (math.isfinite(a) and math.isfinite(b)) or a * b > 0:
            return None
        return float(brentq(fn, lo, hi, xtol=1e-6, maxiter=200))
    except (ValueError, ZeroDivisionError, AssertionError):
        return None


# ------------------------------------------------------------------------------------------------ panel & peers


def prepare_panel(fy: pl.DataFrame) -> pl.DataFrame:
    need = ["revenue", "gross_profit", "ebit", "operating_income", "pretax_income", "interest_expense",
            "depreciation_amortization", "capex", "operating_working_capital", "sbc", "net_income",
            "net_income_parent", "dividends_paid", "buybacks", "income_tax", "cash_and_st_investments", "total_debt",
            "total_equity", "total_equity_parent", "total_assets", "total_liabilities", "ppe_net", "intangible_assets",
            "invested_capital", "operating_lease_liabilities", "eps_diluted", "shares_diluted_weighted", "cfo",
            "fcf", "ebitda", "net_debt", "minority_interest", "preferred_equity", "amortization_intangibles",
            "eps_basic", "shares_outstanding"]
    fy = fy.with_columns([pl.lit(None, pl.Float64).alias(c) for c in need if c not in fy.columns])
    return fy.with_columns(robust_ebit().alias("ebit_r"), robust_invested_capital().alias("ic_r")).sort(
        ["issuer_id", "fiscal_year"])


def peer_stats(fy: pl.DataFrame, issuers: pl.DataFrame) -> dict[str, dict[str, float]]:
    """Medians by subsector / sector / all of 5y revenue CAGR, EBIT margin, gross margin, ROE, capex/da/nwc %."""
    p = fy.filter(pl.col("revenue") > 0).with_columns(
        (pl.col("ebit_r") / pl.col("revenue")).alias("om"), (pl.col("gross_profit") / pl.col("revenue")).alias("gm"),
        (pl.col("capex").abs() / pl.col("revenue")).alias("capex_pct"),
        (pl.col("depreciation_amortization") / pl.col("revenue")).alias("da_pct"),
        (pl.col("operating_working_capital") / pl.col("revenue")).alias("nwc_pct"),
        (pl.col("net_income_parent") / pl.col("total_equity_parent")).alias("roe"))
    per = p.group_by("issuer_id").agg(
        pl.col("om").tail(5).median(), pl.col("gm").tail(5).median(), pl.col("capex_pct").tail(3).median(),
        pl.col("da_pct").tail(3).median(), pl.col("nwc_pct").tail(3).median(),
        pl.col("roe").tail(5).median().clip(-0.5, 0.6),
        ((pl.col("revenue").last() / pl.col("revenue").tail(6).first()) ** (1 / (pl.col("revenue").tail(6).len() - 1))
         - 1).alias("cagr5"))
    per = per.join(issuers.select("issuer_id", "subsector_id", "sector_id"), on="issuer_id", how="left").with_columns(
        pl.col("cagr5").clip(-0.3, 0.6))
    cols = ["om", "gm", "capex_pct", "da_pct", "nwc_pct", "roe", "cagr5"]
    res: dict[str, dict[str, float]] = {}
    for key in ("subsector_id", "sector_id"):
        g = per.filter(pl.col(key).is_not_null()).group_by(key).agg(
            pl.len().alias("n"), *[pl.col(c).median() for c in cols],
            pl.col("om").filter(pl.col("om") > 0).median().alias("om_pos"))
        for r in g.iter_rows(named=True):
            if r["n"] >= 4:
                res[f"{key}:{r[key]}"] = {c: r[c] for c in [*cols, "om_pos"]}
    res["all"] = {c: per.get_column(c).median() for c in cols}
    res["all"]["om_pos"] = per.filter(pl.col("om") > 0).get_column("om").median()
    return res


def _peer(stats: dict[str, dict[str, float]], sub: str | None, sec: str | None, k: str) -> float | None:
    for key in (f"subsector_id:{sub}", f"sector_id:{sec}", "all"):
        v = stats.get(key, {}).get(k)
        if v is not None and math.isfinite(v):
            return float(v)
    return None


# ------------------------------------------------------------------------------------------------ per issuer


def _hist(h: pl.DataFrame, col: str) -> list[float | None]:
    return [_f(x) for x in h.get_column(col).to_list()]


def _rates(ctx: dict[str, Any], erp: float, g: float) -> tuple[float, float]:
    """(ke, wacc) at a given ERP with the valuation guard: discount rate >= terminal growth + MIN_SPREAD."""
    ke, w = cc.rates(ctx["rf"], ctx["beta"], ctx["crp"], ctx["kd_post"], ctx["weight_equity"],
                     bool(ctx["is_financial"]), erp)
    return max(ke, g + MIN_SPREAD), max(w, g + MIN_SPREAD)


def _sens_grid(fn, w0: float, g0: float) -> dict[str, Any]:
    """WACC -2..+2% (1% steps) x terminal growth -1..+1% (0.5% steps); values floored at 0."""
    dw, dg = (-0.02, -0.01, 0.0, 0.01, 0.02), (-0.01, -0.005, 0.0, 0.005, 0.01)
    grid = [[None if (w0 + a <= g0 + b + 0.005 or w0 + a <= 0.02) else _r(max(fn(w0 + a, g0 + b), 0.0))
             for b in dg] for a in dw]
    return {"wacc": [round(w0 + a, 4) for a in dw], "terminal_growth": [round(g0 + b, 4) for b in dg],
            "fair_value": grid, "base_wacc": w0, "base_terminal_growth": g0}


def _r(x: float | None, n: int = 6) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), n)


def margin_path(start: float, target: float, speed: float, n: int = H) -> np.ndarray:
    """Margin moving from `start` toward `target` by at most `speed` per year."""
    out, m = [], start
    for _ in range(n):
        m = m + min(max(target - m, -speed), speed)
        out.append(m)
    return np.array(out, float)


def market_basics(h: pl.DataFrame, ctx: dict[str, Any]) -> dict[str, Any]:
    """Shares (with fallbacks), price/FX conversion, market cap and EV claims in reporting currency, data flags.
    Returns {'skip': reason} when the issuer cannot be valued."""
    flags: list[str] = []
    price = ctx.get("price")
    shares = ctx.get("shares")
    if not shares or shares <= 0:
        for col in ("shares_outstanding", "shares_diluted_weighted"):
            v = [x for x in _hist(h, col)[-2:] if x and x > 0] if col in h.columns else []
            if v:
                shares = v[-1]
                flags.append("shares_from_fy")
                break
    if not shares or shares <= 0:  # last resort: net income / EPS of the last FY
        for eps_col in ("eps_diluted", "eps_basic"):
            if eps_col in h.columns:
                e, n = _f(h.get_column(eps_col)[-1]), _f(h.get_column("net_income_parent")[-1])
                if e and n and e > 0 and n > 0:
                    shares = n / e
                    flags.append("shares_from_eps")
                    break
    if price is None or price <= 0:
        return {"skip": "no_price"}
    if not shares or shares <= 0:
        return {"skip": "no_shares"}
    fx = ctx.get("fx_rep_to_px")
    if not fx:
        return {"skip": "fx_missing"}
    adr = ctx.get("adr_ratio") or 1.0
    if ctx.get("security_type") == "adr" and not ctx.get("adr_ratio"):
        flags.append("adr_ratio_missing")
    to_px = fx * adr
    price_rep = price / to_px
    eps = _f(h.get_column("eps_diluted")[-1]) if "eps_diluted" in h.columns else None
    ni = _f(h.get_column("net_income_parent")[-1])
    if eps and eps > 0 and (price_rep / eps < 2.5 or price_rep / eps > 1500):
        # P/E alone cannot tell a share-basis error (split / ADR / units) from a near-zero profit year (BP FY2025:
        # NI $55M, EPS $0.0034 -> P/E ~2,300). Cross-check P/B on the same share count before suppressing.
        book = ctx.get("book_latest")
        if book is None and "total_equity_parent" in h.columns:
            book = _f(h.get_column("total_equity_parent")[-1])
        pb = price_rep / (book / shares) if book and book > 0 and shares else None
        if pb is not None and 0.1 <= pb <= 50:
            flags.append("eps_outlier")
        else:
            flags.append("share_basis_mismatch")
    if eps and ni and eps > 0 and ni > 0 and "shares_from_eps" not in flags:
        ratio = (ni / shares) / eps
        if not 0.6 <= ratio <= 1.67:  # shares count inconsistent with reported EPS (units / class / ADR basis)
            flags.append("shares_eps_inconsistent")
    if ctx.get("reporting_currency") and ctx["reporting_currency"] != ctx["currency"]:
        flags.append("currency_mismatch")
    cash0 = ctx.get("cash_latest") if ctx.get("cash_latest") is not None else (_hist(h, "cash_and_st_investments")[-1]
                                                                             or 0.0)
    debt0 = ctx.get("debt_latest") if ctx.get("debt_latest") is not None else (_hist(h, "total_debt")[-1] or 0.0)
    claims = (debt0 or 0.0) + (ctx.get("minority") or 0.0) + (ctx.get("preferred") or 0.0) - (cash0 or 0.0)
    mcap = price_rep * shares
    return {"shares": shares, "flags": flags, "to_px": to_px, "fx": fx, "adr": adr, "price_rep": price_rep,
            "mcap": mcap, "claims": claims, "cash0": cash0 or 0.0, "debt0": debt0 or 0.0}


def issuer_multiples(h: pl.DataFrame, mb: dict[str, Any]) -> dict[str, float | None]:
    """Own market multiples on the last FY (for peer medians): EV/EBITA, P/E, EV/Sales, P/B."""
    rev = _f(h.get_column("revenue")[-1])
    ebita = _f(h.get_column("ebit_r")[-1])
    if ebita is not None:
        ebita += _f(h.get_column("amortization_intangibles")[-1]) or 0.0
    ni = _f(h.get_column("net_income_parent")[-1])
    bv = _f(h.get_column("total_equity_parent")[-1])
    ev = mb["mcap"] + mb["claims"]

    def ok(x: float | None, hi: float) -> float | None:
        return x if x is not None and math.isfinite(x) and 0 < x <= hi else None
    return {"ev_ebita": ok(ev / ebita, 100) if ebita and ebita > 0 else None,
            "pe": ok(mb["mcap"] / ni, 150) if ni and ni > 0 else None,
            "ev_sales": ok(ev / rev, 50) if rev and rev > 0 else None,
            "pb": ok(mb["mcap"] / bv, 20) if bv and bv > 0 else None}


def multiples_equity(peer: dict[str, float] | None, *, ebita_norm: float | None, ni_norm: float | None,
                     rev: float | None, bv: float | None, claims: float, is_fin: bool,
                     loss_maker: bool) -> tuple[float | None, dict[str, float]]:
    """Peer-multiple equity value (geometric mean of the available methods)."""
    if not peer:
        return None, {}
    vals: dict[str, float] = {}
    if is_fin:
        if peer.get("pe") and ni_norm and ni_norm > 0:
            vals["pe"] = peer["pe"] * ni_norm
        if peer.get("pb") and bv and bv > 0:
            vals["pb"] = peer["pb"] * bv
    else:
        if peer.get("ev_ebita") and ebita_norm and ebita_norm > 0:
            vals["ev_ebita"] = peer["ev_ebita"] * ebita_norm - claims
        if peer.get("pe") and ni_norm and ni_norm > 0:
            vals["pe"] = peer["pe"] * ni_norm
        if loss_maker and not vals and peer.get("ev_sales") and rev and rev > 0:
            vals["ev_sales"] = peer["ev_sales"] * rev - claims
    pos = [v for v in vals.values() if v > 0]
    if not pos:
        return (0.0 if vals else None), vals
    return float(np.exp(np.mean(np.log(pos)))), vals


def data_quality(h: pl.DataFrame, ctx: dict[str, Any], flags: list[str], as_of: date,
                 reasons: list[str]) -> float:
    q = _f(h.get_column("dq_score")[-1]) if "dq_score" in h.columns else None
    q = 0.8 if q is None else min(max(q, 0.3), 1.0)
    if {"currency_mismatch", "share_basis_mismatch"} & set(flags):
        reasons.append("data:currency_or_share_basis")
        return 0.0
    for f, k, why in (("shares_from_eps", 0.8, "data:shares_backsolved"), ("shares_from_fy", 0.9, "data:shares_from_fy"),
                      ("shares_eps_inconsistent", 0.6, "data:shares_eps_inconsistent"),
                      ("adr_ratio_missing", 0.5, "data:adr_ratio_missing")):
        if f in flags:
            q *= k
            reasons.append(why)
    n = h.height
    if n < 5:
        q *= math.sqrt(n / 5)
        reasons.append("data:short_history")
    if "knowledge_date_estimated" in h.columns and bool(h.get_column("knowledge_date_estimated")[-1]):
        q *= 0.9
    if (as_of - h.get_column("period_end")[-1]).days > 456:
        q *= 0.9
        reasons.append("data:old_fiscal_year")
    pd_ = ctx.get("price_date")
    if pd_ is not None and (as_of - pd_).days > 5:
        q *= 0.9
        reasons.append("data:stale_price")
    return q


def blend_and_score(val_rows: list[dict[str, Any]], *, fv_mult: float | None, s: float, q: float,
                    reasons: list[str], price: float, to_px: float, shares: float, claims: float | None,
                    ke: float) -> dict[str, Any]:
    """DCF x peer-multiples blend, fair-value range and valuation_confidence (0..100) on the scenario rows."""
    rows = {r["scenario"]: r for r in val_rows}
    base = rows.get("base", {})
    dcf_b = base.get("fair_value_per_share")
    dcf_b = None if dcf_b is None or not math.isfinite(dcf_b) else dcf_b
    if fv_mult is None:
        a, w = 0.75, 1.0
        reasons.append("model:no_peer_multiples")
    elif dcf_b is None or dcf_b <= 0:
        a, w = 0.3, 0.0
        reasons.append("model:dcf_nonpositive")
    else:
        r = abs(math.log(dcf_b / fv_mult)) if fv_mult > 0 else math.log(4)
        a = 1 - 0.7 * min(r / math.log(4), 1.0)
        w = W_DCF_MIN + (W_DCF_MAX - W_DCF_MIN) * s
        if r > math.log(2):
            reasons.append("model:dcf_multiples_disagree")
    # log-space blend; peer multiples only close MULT_CONVERGENCE of the gap (relative-valuation gaps revert partially)
    l_d = math.log(dcf_b / price) if dcf_b and dcf_b > 0 else None
    l_m = MULT_CONVERGENCE * math.log(fv_mult / price) if fv_mult and fv_mult > 0 else None
    if l_d is None and l_m is None:
        l_b = math.log(0.05)  # nothing positive: equity worth (almost) nothing on both methods
    elif l_m is None:
        l_b = l_d
    elif l_d is None or w <= 0.0:
        l_b = l_m
    else:
        l_b = w * l_d + (1 - w) * l_m
    conf = 100.0 * q * s * a
    # credibility shrink toward the market price (Bayesian prior = price): k = 0.4 + 0.6 * confidence
    k_cred = CRED_MIN + (1 - CRED_MIN) * conf / 100.0
    blend_b = price * math.exp(l_b * k_cred)
    up_b = blend_b / price - 1
    if up_b > 1.0:
        conf = min(conf, 35.0)
        reasons.append("extreme_upside")
    elif up_b < -0.9:
        conf = min(conf, 35.0)
        reasons.append("extreme_downside")
    lo_hi = [x for x in (dcf_b, fv_mult) if x is not None and x > 0]
    for scen, r in rows.items():
        if scen == "market_implied":
            pass
        else:
            dcf = r.get("fair_value_per_share")
            dcf = None if dcf is None or not math.isfinite(dcf) else dcf
            if scen == "base":
                fv = blend_b
            elif dcf_b and dcf_b > 0 and dcf is not None:
                fv = blend_b * min(max(dcf / dcf_b, 0.25), 4.0)
            else:
                fv = blend_b * (0.75 if scen == "bear" else 1.25)
            r["fair_value_dcf"] = None if dcf is None else max(dcf, 0.0)
            r["fair_value_per_share"] = fv
            r["upside_pct"] = fv / price - 1
            r["target_price_12m"] = fv * (1 + ke) - (r.pop("_dps", 0.0) or 0.0)
            r["equity_value"] = fv / to_px * shares
            r["enterprise_value"] = None if claims is None else r["equity_value"] + claims
            if r.get("sensitivity_grid") and dcf_b and dcf_b > 0:
                grid = orjson.loads(r["sensitivity_grid"])
                k = blend_b / dcf_b
                grid["fair_value"] = [[None if x is None else _r(x * k) for x in row] for row in grid["fair_value"]]
                grid["scaled_to_blend"] = _r(k)
                r["sensitivity_grid"] = orjson.dumps(grid).decode()
            elif r.get("sensitivity_grid"):
                r["sensitivity_grid"] = None
        r["fair_value_multiples"] = fv_mult
        r["fair_value_low"] = min(lo_hi) if lo_hi else None
        r["fair_value_high"] = max(lo_hi) if lo_hi else None
        r["valuation_confidence"] = round(conf, 1)
        r["confidence_reasons"] = sorted(set(reasons)) or None
    return {"w_dcf": w, "agreement": a, "stability": s, "data_quality": q, "confidence": conf,
            "credibility_k": k_cred, "multiples_convergence": MULT_CONVERGENCE, "fv_dcf": dcf_b,
            "fv_multiples": fv_mult, "fv_blend": blend_b}


def forecast_issuer(h: pl.DataFrame, ctx: dict[str, Any], stats: dict[str, dict[str, float]], as_of: date
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(forecast rows, valuation rows, flags) for one issuer. h = FY panel (prepared), ascending."""
    light = bool(ctx.get("light"))
    years = [int(y) for y in h.get_column("fiscal_year").to_list()]
    rev = _hist(h, "revenue")
    last_end: date = h.get_column("period_end")[-1]
    ccy = ctx["currency"]
    price, px_ccy = ctx.get("price"), ctx.get("price_currency")
    mb = market_basics(h, ctx)
    if "skip" in mb:
        return [], [], [mb["skip"]]
    flags: list[str] = list(mb["flags"])
    shares, to_px, price_rep, mcap_rep = mb["shares"], mb["to_px"], mb["price_rep"], mb["mcap"]
    fx, adr = mb["fx"], mb["adr"]
    tau = discount_times(last_end, as_of)
    rf, g = ctx["rf"], ctx["terminal_growth"]
    erp = ctx.get("erp_used") or ctx["erp"]
    ke, wacc = _rates(ctx, erp, g)
    if wacc <= g + MIN_SPREAD + 1e-9:
        flags.append("discount_rate_floor")
    sub, sec = ctx.get("subsector_id"), ctx.get("sector_id")
    tax = ctx["tax_rate"]
    now = datetime.now(UTC)
    f_rows: list[dict[str, Any]] = []
    fy_fc = [years[-1] + t for t in range(1, H + 1)]
    reasons: list[str] = []
    peer_mult = ctx.get("peer_multiples")

    def add(scen: str, item: str, vals, yrs, actual: bool) -> None:
        for y, v in zip(yrs, vals, strict=False):
            v = _f(v)
            f_rows.append({"scenario": scen, "fiscal_year": y, "item_id": item, "value": v,
                           "currency": None if item.startswith("drv_") or item in ("shares_diluted_weighted",)
                           else ccy, "is_actual": actual})

    if not light:  # ---- actual anchors (last 5 FY) for every scenario
        ha = h.tail(5)
        ya = [int(y) for y in ha.get_column("fiscal_year").to_list()]
        for scen in PROB:
            for item in ITEMS_ACTUAL:
                src = "ebit_r" if item == "ebit" else item
                if src in ha.columns:
                    add(scen, item, ha.get_column(src).to_list(), ya, True)
            fcff_a = [(_f(e) or 0) * (1 - tax) + (_f(d) or 0) - abs(_f(c) or 0)
                      if _f(e) is not None else None for e, d, c in zip(ha["ebit_r"], ha["depreciation_amortization"],
                                                                       ha["capex"], strict=False)]
            add(scen, "fcff", fcff_a, ya, True)

    val_rows: list[dict[str, Any]] = []
    assumptions: dict[str, Any] = {"as_of": as_of.isoformat(), "last_fy": years[-1], "last_fy_end": last_end.isoformat(),
                                   "currency": ccy, "price_currency": px_ccy, "fx_reporting_to_price": _r(fx),
                                   "adr_ratio": adr if adr != 1.0 else None, "shares": shares, "rf": _r(rf),
                                   "beta": _r(ctx.get("beta")), "cost_of_equity": _r(ke), "wacc": _r(wacc),
                                   "terminal_growth": _r(g), "tax_rate": _r(tax), "erp": _r(erp),
                                   "erp_contract": _r(ctx["erp"]), "crp": _r(ctx["crp"]), "peer_multiples": peer_mult}
    q = 1.0 if light else data_quality(h, ctx, flags, as_of, reasons)

    if ctx["is_financial"]:
        ni = _hist(h, "net_income_parent")
        ni = [a if a is not None else b for a, b in zip(ni, _hist(h, "net_income"), strict=False)]
        bv = _hist(h, "total_equity_parent")
        bv = [a if a is not None else b for a, b in zip(bv, _hist(h, "total_equity"), strict=False)]
        roe_h = [n / ((b + bp) / 2 if bp and bp > 0 else b) if (n is not None and b and b > 0) else None
                 for n, b, bp in zip(ni, bv, [None, *bv[:-1]], strict=False)]
        roe_v = [x for x in roe_h if x is not None]
        bv0 = ctx.get("book_latest") or bv[-1]
        if len(roe_v) < MIN_FY or not bv0 or bv0 <= 0:
            return [], [], ["insufficient_history"]
        roe_last = 0.5 * roe_v[-1] + 0.5 * float(np.mean(roe_v[-3:]))
        own = float(np.median(roe_v[-5:]))
        peer = _peer(stats, sub, sec, "roe")
        target = 0.6 * own + 0.4 * peer if peer is not None else own
        target, roe_last = min(max(target, -0.2), 0.4), min(max(roe_last, -0.3), 0.5)
        sig = min(max(float(np.std(roe_v[-10:])) if len(roe_v) >= 3 else 0.02, 0.01), 0.05)
        dist = [((_f(d) or 0) + (_f(b) or 0)) / n for d, b, n in zip(h["dividends_paid"], h["buybacks"], ni, strict=False)
                if n and n > 0][-3:]
        payout = min(max(float(np.median([abs(x) for x in dist])) if dist else 0.4, 0.0), 1.0)
        assumptions.update({"model": "excess_return", "roe_start": _r(roe_last), "roe_target": _r(target),
                            "roe_own_5y": _r(own), "roe_peer": _r(peer), "roe_sigma": _r(sig), "payout": _r(payout),
                            "book_value_0": bv0})
        shift = {"bear": -Z * sig, "base": 0.0, "bull": Z * sig}

        def er_val(path: np.ndarray, k: float = ke, gg: float = g) -> float | None:
            v, _ = excess_return_value(bv0, path, payout, k, gg, tau)
            return v

        for scen, sh in shift.items():
            if light and scen != "base":
                continue
            path = margin_path(roe_last, target + sh, ROE_SPEED)
            v, arr = excess_return_value(bv0, path, payout, ke, g, tau)
            if light:  # calibration: upside as a cheap function of the ERP (projection fixed)
                def up_fn(e: float, path=path) -> float | None:
                    k_e, _ = _rates(ctx, e, g)
                    val, _ = excess_return_value(bv0, path, payout, k_e, g, tau)
                    return None if val is None else max(val / shares * to_px, 0.0) / price - 1
                return [], [{"scenario": "base", "up_fn": up_fn}], flags
            if not arr:
                continue
            add(scen, "net_income", arr["net_income"], fy_fc, False)
            add(scen, "net_income_parent", arr["net_income"], fy_fc, False)
            add(scen, "dividends_paid", arr["dividends_paid"], fy_fc, False)
            add(scen, "total_equity_parent", arr["total_equity_parent"], fy_fc, False)
            add(scen, "residual_income", arr["residual_income"], fy_fc, False)
            add(scen, "eps_diluted", arr["net_income"] / shares, fy_fc, False)
            add(scen, "drv_roe", path, fy_fc, False)
            add(scen, "drv_payout", [payout] * H, fy_fc, False)
            fv = v / shares * to_px if v is not None else None
            grid = _sens_grid(lambda w, gg, p=path: (er_val(p, w, gg) or float("nan")) / shares * to_px, ke, g) \
                if scen == "base" else None
            val_rows.append({"scenario": scen, "probability": PROB[scen], "wacc": ke, "terminal_growth": g,
                             "fair_value_per_share": fv, "_dps": arr["dividends_paid"][0] / shares * to_px,
                             "sensitivity_grid": orjson.dumps(grid).decode() if grid else None})
        implied_roe = implied_root(lambda r: (er_val(np.full(H, r)) or float("nan")) - mcap_rep, -0.3, 0.8)
        assumptions["implied_roe"] = _r(implied_roe)
        # stability + peer multiples (P/E on normalized earnings = own 5y ROE x book, P/B)
        s = 1.0 - 0.4 * min(sig / max(abs(own), 0.02), 1.0)
        if roe_last <= 0:
            s *= 0.6
            reasons.append("model:loss_making_financial")
        if "discount_rate_floor" in flags:
            s *= 0.85
            reasons.append("model:discount_rate_floor")
        eq_m, parts = multiples_equity(peer_mult, ebita_norm=None, ni_norm=max(own, 0.0) * bv0, rev=None, bv=bv0,
                                       claims=0.0, is_fin=True, loss_maker=roe_last <= 0)
        fv_mult = None if eq_m is None else eq_m / shares * to_px
        val_rows.append({"scenario": "market_implied", "probability": None, "wacc": ke, "terminal_growth": g,
                         "enterprise_value": None, "equity_value": mcap_rep, "fair_value_per_share": None,
                         "upside_pct": None, "implied_revenue_cagr_5y": None, "implied_fcf_margin": None,
                         "implied_vs_hist_growth_pct": None})
        info = blend_and_score(val_rows, fv_mult=fv_mult, s=s, q=q, reasons=reasons, price=price, to_px=to_px,
                               shares=shares, claims=None, ke=ke)
        assumptions.update({"valuation_blend": {k: _r(v) for k, v in info.items()},
                            "multiples_parts_per_share": {k: _r(v / shares * to_px) for k, v in parts.items()}})
        for r in val_rows:
            r["model"] = "excess_return"
            r["cost_of_equity"] = ke
        return _finish(f_rows, val_rows, assumptions, flags, ctx, price, now) + (flags,)

    # ---- non-financial: FCFF
    ebit = _hist(h, "ebit_r")
    amort = _hist(h, "amortization_intangibles")
    ebita = [e + (a or 0.0) if e is not None else None for e, a in zip(ebit, amort, strict=False)]
    ok = [i for i, (r, e) in enumerate(zip(rev, ebita, strict=False)) if r and r > 0 and e is not None]
    if len(ok) < MIN_FY or ok[-1] != len(rev) - 1:
        return [], [], ["insufficient_history"]
    r_last = rev[-1]
    om = [e / r if (r and r > 0 and e is not None) else None for r, e in zip(rev, ebita, strict=False)]
    om_v = [x for x in om if x is not None]
    gm_h = [gp / r if (r and r > 0 and gp is not None) else None for r, gp in zip(rev, _hist(h, "gross_profit"),
                                                                               strict=False)]
    yo = [(rev[i] / rev[i - 1] - 1) if (rev[i] and rev[i - 1] and rev[i - 1] > 0 and years[i] == years[i - 1] + 1)
          else None for i in range(1, len(rev))]
    yo_v = [min(max(x, -0.5), 1.0) for x in yo if x is not None]
    ts = theil_sen_growth(years[-7:], rev[-7:])
    k3 = min(3, len(rev) - 1)
    cagr3 = (rev[-1] / rev[-1 - k3]) ** (1 / k3) - 1 if rev[-1 - k3] and rev[-1 - k3] > 0 and rev[-1] > 0 else None
    g_parts = [x for x in (ts, cagr3) if x is not None]
    g1 = min(max(float(np.mean(g_parts)) if g_parts else 0.03, -0.25), 0.50)
    g_peer = _peer(stats, sub, sec, "cagr5")
    g_peer = min(max(g_peer if g_peer is not None else 0.05, -0.05), 0.15)
    g5 = 0.5 * g_peer + 0.5 * g
    growth = g1 + (g5 - g1) * np.arange(H) / (H - 1)
    m_last = om[-1]
    m_own = _med(om[-5:])
    m_med10 = _med(om[-10:])
    m_peer = _peer(stats, sub, sec, "om")
    m_peer = m_own if m_peer is None else m_peer
    m_peer_pos = _peer(stats, sub, sec, "om_pos")
    sig_m = min(max(float(np.std(om_v[-10:])) if len(om_v) >= 3 else 0.03, 0.01), 0.10)
    sig_g = min(max(float(np.std(yo_v[-10:])) if len(yo_v) >= 3 else 0.10, 0.03), 0.25)
    loss = m_last <= 0 or m_own <= 0
    deep = m_last < -0.5
    cyclical = (not loss) and (sub in CYCLICAL_SUBSECTORS or (len(om_v) >= 5 and sig_m > 0.5 * abs(m_own)))
    if loss:
        regime, speed = "loss_maker", LOSS_SPEED
        m_start = m_last
        m_lr = max(0.8 * (m_peer_pos if m_peer_pos is not None else 0.10), 0.03)
    elif cyclical:
        regime, speed = "cyclical", MARGIN_SPEED
        m_norm = m_med10
        m_start = 0.5 * m_last + 0.5 * m_norm
        m_lr = 0.9 * m_norm + 0.1 * m_peer
    else:
        regime, speed = "normal", MARGIN_SPEED
        m_norm = 0.6 * m_own + 0.4 * m_last
        m_start = m_last
        m_lr = 0.9 * m_norm + 0.1 * m_peer
    gm_last = gm_h[-1]
    gm_own = _med(gm_h[-5:])
    gm_target = gm_own if gm_own is not None else None

    def pct(col: str, default_key: str, absval: bool = False) -> tuple[float, str]:
        v = [(abs(x) if absval else x) / r for x, r in zip(_hist(h, col)[-3:], rev[-3:], strict=False)
             if x is not None and r and r > 0]
        if v:
            return float(np.median(v)), "own_3y"
        p = _peer(stats, sub, sec, default_key)
        return (p if p is not None else 0.0), "peer"

    da_pct, da_src = pct("depreciation_amortization", "da_pct")
    amort_pct, _ = pct("amortization_intangibles", "_none")
    dep_pct = max(da_pct - amort_pct, 0.0)  # depreciation only: margins are EBITA (acquired-intangible amortization
    capex_pct, cx_src = pct("capex", "capex_pct", absval=True)  # is non-cash and needs no replacement capex)
    nwc_pct, nwc_src = pct("operating_working_capital", "nwc_pct")
    nwc_last = _hist(h, "operating_working_capital")[-1]
    if nwc_last is not None and r_last > 0:  # start from the last reported intensity (no artificial year-1 jump)
        nwc_pct, nwc_src = nwc_last / r_last, "last_fy"
    sbc_pct, _ = pct("sbc", "_none")
    nwc_pct = min(max(nwc_pct, -0.5), 0.8)
    ni_h = _hist(h, "net_income_parent")
    pay = [abs(_f(d) or 0) / n for d, n in zip(h["dividends_paid"], ni_h, strict=False) if n and n > 0][-3:]
    bbk = [abs(_f(b) or 0) / n for b, n in zip(h["buybacks"], ni_h, strict=False) if n and n > 0][-3:]
    payout = min(float(np.median(pay)) if pay else 0.3, 1.0)
    buyback = min(float(np.median(bbk)) if bbk else 0.0, max(1.2 - payout, 0.0))
    cash0, debt0, claims = mb["cash0"], mb["debt0"], mb["claims"]
    eq0 = _hist(h, "total_equity")[-1]
    if eq0 is None:
        eq0 = (_hist(h, "total_assets")[-1] or 0.0) - (_hist(h, "total_liabilities")[-1] or 0.0)
    nwc0 = _hist(h, "operating_working_capital")[-1]
    nwc0 = nwc0 if nwc0 is not None else nwc_pct * r_last
    fixed0 = _hist(h, "ppe_net")[-1] or 0.0  # intangibles/goodwill stay in the constant 'other_net' block
    kd = ctx["kd_pre"]
    opening = Opening(revenue=r_last, cash=cash0, debt=max(debt0, 0.0), equity=eq0, nwc=nwc0,
                      fixed=fixed0, shares=shares, price=price_rep, price_growth=ke)
    # capex fades within CAPEX_FADE_YEARS from the own 3y level to a growth-consistent level that keeps the
    # PP&E turnover constant: capex% = depreciation% + (PP&E / revenue) * g / (1 + g)
    fa_turn = min(max(fixed0 / r_last, 0.0), 5.0) if r_last > 0 else 0.0
    capex_ss = dep_pct + fa_turn * g5 / (1 + g5)
    capex_path = capex_pct + (capex_ss - capex_pct) * np.minimum(np.arange(1, H + 1) / CAPEX_FADE_YEARS, 1.0)
    assumptions.update({
        "model": "fcff_dcf", "margin_regime": regime, "margin_basis": "ebita", "g1": _r(g1),
        "g_trend_theil_sen": _r(ts), "g_cagr3": _r(cagr3), "g_peer": _r(g_peer), "g5": _r(g5),
        "ebit_margin_last": _r(m_last), "ebit_margin_own_5y": _r(m_own), "ebit_margin_median_10y": _r(m_med10),
        "ebit_margin_peer": _r(m_peer), "ebit_margin_peer_profitable": _r(m_peer_pos),
        "ebit_margin_start": _r(m_start), "ebit_margin_target": _r(m_lr), "margin_speed_cap": speed,
        "sigma_growth": _r(sig_g), "sigma_margin": _r(sig_m), "da_pct": _r(da_pct), "amortization_pct": _r(amort_pct),
        "depreciation_pct": _r(dep_pct), "da_source": da_src, "capex_pct": _r(capex_pct), "capex_source": cx_src,
        "capex_pct_steady": _r(capex_ss), "nwc_pct": _r(nwc_pct), "nwc_source": nwc_src, "sbc_pct": _r(sbc_pct),
        "payout": _r(payout), "buyback": _r(buyback), "kd_pre": _r(kd), "cash_0": cash0, "debt_0": debt0,
        "minority_interest": ctx.get("minority"), "preferred_equity": ctx.get("preferred")})
    ev_market = mcap_rep + claims

    def drivers(gpath: np.ndarray, mpath: np.ndarray, gmpath: np.ndarray) -> Drivers:
        return Drivers(growth=gpath, ebit_margin=mpath, gross_margin=gmpath, da_pct=dep_pct, capex_pct=capex_path,
                       nwc_pct=nwc_pct, sbc_pct=sbc_pct, tax_rate=tax, kd=kd,
                       cash_yield=max(rf - 0.01, 0.0), payout=payout, buyback=buyback)

    gs = {"bear": -Z * 0.6 * sig_g, "base": 0.0, "bull": Z * 0.6 * sig_g}
    ms = {"bear": -Z * sig_m, "base": 0.0, "bull": Z * sig_m}
    t_idx = np.arange(1, H + 1) / H
    max_imb = 0.0
    base_m = None
    base_det: dict[str, float] = {}
    base_proj = None
    for scen in PROB:
        if light and scen != "base":
            continue
        gpath = growth + gs[scen]
        mpath = margin_path(m_start, m_lr + ms[scen], speed)
        gmpath = (gm_last + (gm_target - gm_last) * t_idx + ms[scen]) if (gm_last is not None and gm_target is not None) \
            else np.full(H, np.nan)
        try:
            proj = project(drivers(gpath, mpath, gmpath), opening)
        except AssertionError as e:
            return [], [], [f"bs_identity_failed:{e}"]
        m_lr_s, g_start = m_lr + ms[scen], float(gpath[-1])
        if light:  # calibration: upside as a cheap function of the ERP (projection fixed)
            def up_fn(e: float, proj=proj, g_start=g_start, m_lr_s=m_lr_s) -> float | None:
                _, w_e = _rates(ctx, e, g)
                ev_e, _ = value_fcff(proj, tau, w_e, g, g_start=g_start, m_lr=m_lr_s, tax=tax)
                return None if ev_e is None else max((ev_e - claims) / shares * to_px, 0.0) / price - 1
            return [], [{"scenario": "base", "up_fn": up_fn}], flags
        max_imb = max(max_imb, float(np.abs(proj["bs_imbalance"]).max()))
        det = value_fcff_detail(proj, tau, wacc, g, g_start=g_start, m_lr=m_lr_s, tax=tax) or {}
        ev = det.get("ev")
        fv = (ev - claims) / shares * to_px if ev is not None else None
        for item in ("revenue", "gross_profit", "ebit", "ebitda", "depreciation_amortization", "interest_expense",
                     "pretax_income", "income_tax", "net_income", "sbc", "dividends_paid", "buybacks", "capex", "cfo",
                     "fcf", "fcff", "cash_and_st_investments", "total_debt", "total_equity", "net_debt",
                     "operating_working_capital", "shares_diluted_weighted", "eps_diluted"):
            add(scen, item, proj[item], fy_fc, False)
        add(scen, "net_income_parent", proj["net_income"], fy_fc, False)
        add(scen, "drv_revenue_growth", gpath, fy_fc, False)
        add(scen, "drv_ebit_margin", mpath, fy_fc, False)
        add(scen, "drv_gross_margin", gmpath, fy_fc, False)
        add(scen, "drv_opex_pct", gmpath - mpath, fy_fc, False)
        add(scen, "drv_capex_pct", capex_path, fy_fc, False)
        add(scen, "drv_da_pct", [dep_pct] * H, fy_fc, False)
        add(scen, "drv_nwc_pct", [nwc_pct] * H, fy_fc, False)
        add(scen, "drv_tax_rate", [tax] * H, fy_fc, False)
        add(scen, "drv_payout", [payout] * H, fy_fc, False)
        grid = None
        if scen == "base":
            base_m, base_det, base_proj = mpath, det, proj

            def fv_at(w: float, gg: float, proj=proj, g_start=g_start) -> float:
                e, _ = value_fcff(proj, tau, w, gg, g_start=g_start, m_lr=m_lr, tax=tax)
                return (e - claims) / shares * to_px if e is not None else float("nan")
            grid = _sens_grid(fv_at, wacc, g)
        val_rows.append({"scenario": scen, "probability": PROB[scen], "wacc": wacc, "terminal_growth": g,
                         "fair_value_per_share": fv, "_dps": proj["dividends_paid"][0] / shares * to_px,
                         "tv_share": det.get("tv_share"),
                         "sensitivity_grid": orjson.dumps(grid).decode() if grid else None})
    assumptions["bs_max_imbalance"] = max_imb
    assumptions["dcf"] = {k: _r(v) for k, v in base_det.items()}

    # ---- reverse DCF
    gm_base = np.full(H, np.nan)

    def ev_at_growth(gc: float) -> float:
        p = project(drivers(np.full(H, gc), base_m, gm_base), opening)
        e, _ = value_fcff(p, tau, wacc, g, g_start=gc, m_lr=m_lr, tax=tax)
        return (e if e is not None else float("nan")) - ev_market

    implied_g = implied_root(ev_at_growth, -0.5, 1.5) if ev_market > 0 else None
    base_rev = r_last * np.cumprod(1 + growth)
    df = (1 + wacc) ** -tau
    k_ = float((base_rev * df).sum()) + (base_rev[-1] * (1 + g) / (wacc - g) * df[-1] if wacc > g + 0.005 else 0.0)
    implied_m = ev_market / k_ if k_ > 0 and ev_market > 0 else None
    pct_hist = (100.0 * sum(1 for x in yo_v[-10:] if x <= implied_g) / len(yo_v[-10:])
                if implied_g is not None and len(yo_v) >= 3 else None)
    if implied_g is None:
        flags.append("reverse_dcf_unbracketed")

    # ---- model stability, peer multiples, blend, confidence
    s = 1.0
    tvs = base_det.get("tv_share")
    if tvs is not None and math.isfinite(tvs):
        if tvs > TV_SHARE_FLAG:
            flags.append("terminal_value_dominant")
            reasons.append("model:terminal_value_dominant")
        s *= 1 - 0.5 * min(max(tvs - 0.75, 0.0) / 0.2, 1.0)
    neg = int((base_proj["fcff"] < 0).sum()) if base_proj is not None else 0
    s *= max(1 - 0.12 * neg, 0.4)
    if neg >= 2:
        reasons.append("model:negative_fcff")
    rel = sig_m / max(abs(m_own), 0.02)
    s *= 1 - 0.4 * min(rel, 1.0)
    if rel > 0.5:
        reasons.append("model:volatile_margins")
    if loss:
        s *= 0.6
        reasons.append("model:loss_maker_normalized_margin")
    if deep:
        s *= 0.5
        reasons.append("model:deep_losses")
    if cyclical:
        s *= 0.85
        reasons.append("model:cyclical_normalized_margin")
    if "discount_rate_floor" in flags:
        s *= 0.85
        reasons.append("model:discount_rate_floor")
    if abs(g1) > 0.30:
        s *= 0.85
        reasons.append("model:high_growth_extrapolation")
    ebita_norm = m_start * r_last if not loss else None
    ni_last = ni_h[-1]
    ni_norm = ni_last if (ni_last and ni_last > 0 and not cyclical) else (
        ebita_norm * (1 - tax) if ebita_norm and ebita_norm > 0 else None)
    eq_m, parts = multiples_equity(peer_mult, ebita_norm=ebita_norm, ni_norm=ni_norm, rev=r_last, bv=None,
                                   claims=claims, is_fin=False, loss_maker=loss)
    fv_mult = None if eq_m is None else eq_m / shares * to_px
    val_rows.append({"scenario": "market_implied", "probability": None, "wacc": wacc, "terminal_growth": g,
                     "enterprise_value": ev_market, "equity_value": mcap_rep, "fair_value_per_share": None,
                     "upside_pct": None, "implied_revenue_cagr_5y": implied_g, "implied_fcf_margin": implied_m,
                     "implied_vs_hist_growth_pct": pct_hist})
    info = blend_and_score(val_rows, fv_mult=fv_mult, s=min(max(s, 0.0), 1.0), q=q, reasons=reasons, price=price,
                           to_px=to_px, shares=shares, claims=claims, ke=ke)
    assumptions.update({"valuation_blend": {k: _r(v) for k, v in info.items()},
                        "multiples_parts_per_share": {k: _r(v / shares * to_px) for k, v in parts.items()}})
    for r in val_rows:
        r["model"] = "fcff_dcf"
        r["cost_of_equity"] = ke
    return _finish(f_rows, val_rows, assumptions, flags, ctx, price, now) + (flags,)


def _finish(f_rows, val_rows, assumptions, flags, ctx, price, now) -> tuple[list[dict], list[dict]]:
    a = orjson.dumps(assumptions, option=orjson.OPT_SERIALIZE_NUMPY).decode()
    for r in f_rows:
        r.update({"issuer_id": ctx["issuer_id"], "as_of_date": ctx["as_of"], "computed_at": now, "run_id": ctx["run_id"]})
    for r in val_rows:
        r.pop("_dps", None)
        for k in ("enterprise_value", "equity_value", "fair_value_per_share", "upside_pct", "target_price_12m",
                  "implied_revenue_cagr_5y", "implied_fcf_margin", "implied_vs_hist_growth_pct", "fair_value_dcf",
                  "fair_value_multiples", "fair_value_low", "fair_value_high", "tv_share", "valuation_confidence"):
            r[k] = _f(r.get(k))
        if r.get("equity_value") is not None and r["equity_value"] < 0 and r["scenario"] != "market_implied":
            r["equity_value"] = 0.0
            r["fair_value_per_share"] = 0.0 if r.get("fair_value_per_share") is not None else None
            r["upside_pct"] = -1.0 if r.get("fair_value_per_share") is not None else None
            r["target_price_12m"] = 0.0 if r.get("target_price_12m") is not None else None
            if "negative_equity_value" not in flags:
                flags.append("negative_equity_value")
        if "share_basis_mismatch" in flags or "currency_mismatch" in flags:
            for k in ("fair_value_per_share", "upside_pct", "target_price_12m", "sensitivity_grid", "fair_value_dcf",
                      "fair_value_multiples", "fair_value_low", "fair_value_high"):
                r[k] = None
            r["valuation_confidence"] = 0.0
        r.update({"issuer_id": ctx["issuer_id"], "security_id": ctx["security_id"], "as_of_date": ctx["as_of"],
                  "price": price, "currency": ctx["currency"], "assumptions": a, "flags": flags or None,
                  "computed_at": now, "run_id": ctx["run_id"]})
    return f_rows, val_rows


# ------------------------------------------------------------------------------------------------ driver


ERP_BUCKETS = {"US": "NA", "CA": "NA", "EU": "EUROPE", "UK": "EUROPE", "JP": "JP", "CN": "CHINA", "HK": "CHINA"}
ERP_BOUNDS = (0.01, 0.10)
ERP_MIN_NAMES = 12


def erp_bucket(region: str | None) -> str:
    return ERP_BUCKETS.get(region or "", "EM_OTHER")


def erp_keys(region: str | None, is_fin: bool) -> list[str]:
    """Calibration keys from most to least specific: region bucket x model family, region bucket, pooled."""
    rb = erp_bucket(region)
    return [f"{rb}|{'FIN' if is_fin else 'NONFIN'}", rb, "ALL"]


def erp_for(res: dict[str, float], region: str | None, is_fin: bool) -> float | None:
    return next((res[k] for k in erp_keys(region, is_fin) if k in res), None)


def calibrate_erp(ctxs: list[tuple[dict[str, Any], pl.DataFrame | None, list[str]]],
                  stats: dict[str, dict[str, float]], as_of: date) -> dict[str, float]:
    """Market-implied ERP per region bucket: the ERP at which the median base-case upside is 0 (Damodaran-style
    implied premium, solved by bisection on [1%, 10%]). Keys: region bucket (NA, EUROPE, JP, CHINA, EM_OTHER) x
    model family (FIN = excess-return, NONFIN = FCFF), then region bucket, then pooled 'ALL'; a key needs
    >= ERP_MIN_NAMES valued names. The fixed contract ERP still drives cost_of_capital / moat."""
    live = [(c, h) for c, h, pre in ctxs if not pre and h is not None]

    fns: dict[int, Any] = {}
    for i, (c, h) in enumerate(live):
        try:
            _, vr, fl = forecast_issuer(h, dict(c, light=True, erp_used=0.05), stats, as_of)
        except Exception:  # noqa: BLE001
            continue
        if vr and "up_fn" in vr[0] and not ({"share_basis_mismatch", "currency_mismatch"} & set(fl)):
            fns[i] = vr[0]["up_fn"]
    idx = {id(c): i for i, (c, _) in enumerate(live)}

    def ups(erp: float, subset) -> list[float]:
        out = []
        for c, _h in subset:
            fn = fns.get(idx[id(c)])
            v = fn(erp) if fn else None
            if v is not None and math.isfinite(v):
                out.append(v)
        return out

    def solve(subset) -> float | None:
        if len(subset) < 5:
            return None
        lo, hi = ERP_BOUNDS
        u_lo, u_hi = ups(lo, subset), ups(hi, subset)
        if len(u_lo) < 5 or len(u_hi) < 5:
            return None
        if np.median(u_lo) <= 0:
            return lo
        if np.median(u_hi) >= 0:
            return hi
        for _ in range(14):
            mid = (lo + hi) / 2
            if np.median(ups(mid, subset)) > 0:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    res: dict[str, float] = {}
    pooled = solve(live)
    if pooled is not None:
        res["ALL"] = pooled
    buckets: dict[str, list] = {}
    for c, h in live:
        for k in erp_keys(c.get("region"), bool(c.get("is_financial")))[:-1]:
            buckets.setdefault(k, []).append((c, h))
    for b, subset in buckets.items():
        v = solve(subset) if len(subset) >= ERP_MIN_NAMES else None
        if v is not None:
            res[b] = v
    log.info("implied ERP by bucket: %s", res)
    return res


def peer_multiples(ctxs: list[tuple[dict[str, Any], pl.DataFrame | None, list[str]]],
                   min_peers: int = 5) -> dict[str, dict[str, float]]:
    """issuer_id -> peer median multiples {ev_ebita, pe, ev_sales, pb, level}: same model family, subsector, then
    sector, then family-wide (>= min_peers valid values per multiple; data-flagged issuers excluded)."""
    rows = []
    for c, h, pre in ctxs:
        if pre or h is None:
            continue
        mb = market_basics(h, c)
        if "skip" in mb or {"currency_mismatch", "share_basis_mismatch", "shares_eps_inconsistent"} & set(mb["flags"]):
            continue
        rows.append({"issuer_id": c["issuer_id"], "fam": bool(c.get("is_financial")), "sub": c.get("subsector_id"),
                     "sec": c.get("sector_id"), **issuer_multiples(h, mb)})
    if not rows:
        return {}
    df = pl.DataFrame(rows, infer_schema_length=None)
    keys = ["ev_ebita", "pe", "ev_sales", "pb"]
    levels = [("subsector", ["fam", "sub"]), ("sector", ["fam", "sec"]), ("family", ["fam"])]
    meds: dict[str, dict[tuple, dict[str, Any]]] = {}
    for name, by in levels:
        g = df.group_by(by).agg(*[pl.col(k).median().alias(k) for k in keys],
                                *[pl.col(k).count().alias(f"n_{k}") for k in keys])
        meds[name] = {tuple(r[b] for b in by): r for r in g.iter_rows(named=True)}
    out: dict[str, dict[str, float]] = {}
    for c, _h, _pre in ctxs:
        vals = {"fam": bool(c.get("is_financial")), "sub": c.get("subsector_id"), "sec": c.get("sector_id")}
        res: dict[str, Any] = {}
        for k in keys:
            for name, by in levels:
                m = meds[name].get(tuple(vals[b] for b in by))
                if m and (m[f"n_{k}"] or 0) >= min_peers and m[k] is not None:
                    res[k] = round(float(m[k]), 4)
                    res[f"{k}_level"] = name
                    break
        out[c["issuer_id"]] = res
    return out


def compute(inp: cc.Inputs, as_of: date, coc: pl.DataFrame, keep: set[str] | None = None,
            run_id: str | None = None, erp_mode: str = "implied"
            ) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:
    # share counts / EPS on the share basis of the as_of close (post-split prices vs pre-split filings)
    fy = prepare_panel(cc.split_adjust(cc.fy_asof(inp.fy_all, as_of, max_years=10), inp.prices, inp.issuers, as_of))
    stats = peer_stats(fy, inp.issuers)
    lat = cc.split_adjust(cc.latest_asof(inp.latest_all, as_of), inp.prices, inp.issuers, as_of)
    px = cc.close_asof(inp.prices, as_of)
    meta = (coc.join(inp.issuers.select("issuer_id", "sector_id", "subsector_id", "adr_ratio", "display_ticker",
                                        *[c for c in ("security_type",) if c in inp.issuers.columns]),
                     on="issuer_id", how="left")
            .join(px, on="security_id", how="left")
            .join(inp.issuers.select("issuer_id", "reporting_currency", "region"), on="issuer_id", how="left")
            .join(lat.select("issuer_id", pl.col("shares_outstanding").alias("sh_out"),
                             pl.col("shares_diluted_weighted").alias("sh_dil"),
                             pl.col("cash_and_st_investments").alias("cash_latest"),
                             pl.col("total_debt").alias("debt_latest"),
                             pl.col("minority_interest").alias("minority"), pl.col("preferred_equity").alias("preferred"),
                             pl.col("total_equity_parent").alias("book_latest")), on="issuer_id", how="left"))
    if keep is not None:
        meta = meta.filter(pl.col("issuer_id").is_in(list(keep)))
    groups = {k[0]: g for k, g in fy.partition_by("issuer_id", as_dict=True).items()}
    f_all, v_all = [], []
    stat: dict[str, Any] = {"ok": 0, "skipped": {}}
    ctxs: list[tuple[dict[str, Any], pl.DataFrame | None, list[str]]] = []
    for m in meta.iter_rows(named=True):
        h = groups.get(m["issuer_id"])
        pre: list[str] = []
        if h is None or h.is_empty():
            pre = ["no_fundamentals"]
        elif h.get_column("period_end")[-1] < as_of - timedelta(days=730):
            pre = ["stale_fundamentals"]
        ctx = dict(m)
        ctx.update({"as_of": as_of, "run_id": run_id, "price": m["close"],
                    "shares": m["sh_out"] if (m["sh_out"] or 0) > 0 else m["sh_dil"],
                    "fx_rep_to_px": cc.fx_cross(inp.fx, m["currency"], m["price_currency"], as_of)
                    if m["price_currency"] else None})
        ctxs.append((ctx, h, pre))
    peer_mults = peer_multiples(ctxs)
    for ctx, _h, _pre in ctxs:
        ctx["peer_multiples"] = peer_mults.get(ctx["issuer_id"])
    erp_by_bucket = calibrate_erp(ctxs, stats, as_of) if erp_mode == "implied" else {}
    stat["erp"] = {k: round(v, 4) for k, v in erp_by_bucket.items()}
    for ctx, h, pre in ctxs:
        flags: list[str] = pre
        if not pre:
            ctx["erp_used"] = erp_for(erp_by_bucket, ctx.get("region"), bool(ctx.get("is_financial")))
            try:
                fr, vr, flags = forecast_issuer(h, ctx, stats, as_of)
            except Exception as e:  # noqa: BLE001 - one bad issuer must not kill the run
                log.warning("forecast %s failed: %s", ctx.get("display_ticker"), e)
                fr, vr, flags = [], [], [f"error:{type(e).__name__}"]
            if vr:
                f_all.extend(fr)
                v_all.extend(vr)
                stat["ok"] += 1
                continue
        key = flags[0].split(":")[0] if flags else "unknown"
        stat["skipped"][key] = stat["skipped"].get(key, 0) + 1
    fdf = pl.DataFrame(f_all, infer_schema_length=None) if f_all else pl.DataFrame()
    vdf = pl.DataFrame(v_all, infer_schema_length=None) if v_all else pl.DataFrame()
    if fdf.height:
        fdf = fdf.with_columns(pl.col("fiscal_year").cast(pl.Int16)).unique(
            ["issuer_id", "as_of_date", "scenario", "fiscal_year", "item_id"], keep="last")
    return fdf, vdf, stat


def write_forecasts(fdf: pl.DataFrame, as_of: date, settings, keep_days: int = 10) -> int:
    """Write the long forecast rows with retention: the last as_of of every month plus the last `keep_days` days
    are kept (a nightly full-universe run is ~0.5M rows; an unbounded upsert history would grow ~150M rows/yr)."""
    from app.core.storage import scan_table  # noqa: PLC0415
    try:
        old = scan_table("forecasts", settings).filter(pl.col("as_of_date") != as_of).collect()
    except Exception:  # noqa: BLE001 - first run
        old = pl.DataFrame()
    if old.height:
        # the month key needs its own name: an unaliased strftime keeps the name "as_of_date" and collides with the
        # aggregated column (DuplicateError once the table holds more than one as_of)
        ends = old.group_by(pl.col("as_of_date").dt.strftime("%Y-%m").alias("_month")).agg(pl.col("as_of_date").max())
        keep = set(ends.get_column("as_of_date").to_list()) | {
            d for d in old.get_column("as_of_date").unique().to_list() if d >= as_of - timedelta(days=keep_days)}
        old = old.filter(pl.col("as_of_date").is_in(sorted(keep)))
        # issuers re-run today replace their rows; others keep their latest history
        fdf = pl.concat([old, fdf], how="diagonal_relaxed")
    write_table(fdf, "forecasts", mode="overwrite", settings=settings)
    return fdf.filter(pl.col("as_of_date") == as_of).height


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    as_of = ctx.as_of
    keep = target_issuers(p, as_of, s)
    inp = cc.load_inputs([as_of], s)
    coc = cc.compute(inp, as_of, run_id=ctx.run_id)
    if coc.is_empty():
        return JobResult(status="success", message="no fundamentals")
    ctx.check_cancelled()
    fdf, vdf, stat = compute(inp, as_of, coc, keep, ctx.run_id, erp_mode=p.get("erp_mode", "implied"))
    coc_w = coc if keep is None else coc.filter(pl.col("issuer_id").is_in(list(keep)))
    rows = {"cost_of_capital": write_table(coc_w, "cost_of_capital", settings=s)}
    if fdf.height:
        rows["forecasts"] = write_forecasts(fdf, as_of, s)
        rows["forecast_valuation"] = write_table(vdf, "forecast_valuation", settings=s)
    msg = f"issuers_ok={stat['ok']} skipped={stat['skipped']} implied_erp={stat.get('erp')}"
    if vdf.height:
        up = vdf.filter(pl.col("scenario") == "base").get_column("upside_pct").drop_nulls()
        if up.len():
            msg += f" base_upside_median={up.median():.3f} p10={up.quantile(0.1):.3f} p90={up.quantile(0.9):.3f}"
    return JobResult(status="success", rows_written=rows, entities_ok=stat["ok"],
                     entities_failed=sum(stat["skipped"].values()), message=msg)
