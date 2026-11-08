"""Query-time helpers for the Technicals v3 API (GET /technicals/{id}/factors and /breakout).

Factors are deterministic transforms of past prices; their historical edge (rank IC, gate verdict) comes from the
walk-forward validation rows of tech_factor_eval (serving table tv3_eval). A factor or composite that FAILED the gate
is returned with experimental = true (the UI hides it unless the viewer enables experimental outputs).
"""

from __future__ import annotations

import json
import math
from typing import Any

from app.techv3 import factors as F

VALIDATION_DOC = "docs/TECHNICALS_V3_VALIDATION.md"
GATE_REGIONS = ("US", "EU", "UK", "JP")
HORIZONS = (5, 20, 60)
DIRECTION_NOTE = {
    "hi52": "near the 52-week high = historically higher returns",
    "mom": "past 12-1 month winners = historically higher returns",
    "rmom": "high residual (stock-specific) momentum = historically higher returns",
    "strev": "last month's losers = historically higher next-month returns (reversal)",
    "maxret": "lottery-like (large recent daily jump) = historically lower returns",
    "ivol": "high idiosyncratic volatility = historically lower returns",
    "indmom": "industry with strong 6-month returns = historically higher returns",
}
LABELS = {
    "factors": "Evidence-based technical factors: percentile within region x sector (liquid names, same session). "
               "Each badge shows the factor's own out-of-sample rank IC on this app's data (2005-2026, weekly origins, "
               "sector-neutral 20-session forward returns from the next close). Historical evidence, not a forecast.",
    "percentile": "Percentile 0-100 of the raw value among liquid peers (higher raw value = higher percentile); "
                  "the edge direction is shown per factor.",
    "composite": "Composite = equal-weight mean of the sign-aligned percentiles of the factors that passed the gate "
                 "(fixed ex ante); shown only because it passed the walk-forward gate.",
    "composite_failed": "No composite is shown: the factor composite did not pass the walk-forward gate.",
    "experimental": "Experimental = failed the walk-forward gate (docs/TECHNICALS_V3_VALIDATION.md); hidden by default.",
    "breakout": "Breakout = a fresh close beyond the prior 20 / 55-session high (low). Follow-through = the price reaches "
                "+1 sigma x sqrt(20) beyond the breakout close before closing back inside the level (20 sessions). "
                "P(follow-through) is calibrated on history (model-based), not a forecast.",
    "confirmation": "Volume confirmation (pre-registered): volume >= 1.5 x its 50-session average and the close in the "
                    "upper quarter of the day's range (lower quarter for a down breakout).",
    "not_validated_region": "This region was not part of the validation (US / EU / UK / JP): values shown, edge not validated.",
}


def f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def r(x: Any, nd: int = 4) -> float | None:
    v = f(x)
    return None if v is None else round(v, nd)


def eval_index(rows: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """(signal, horizon, region, segment_type, segment) -> validation row."""
    return {(x["signal"], int(x["horizon"]), x["region"], x["segment_type"], x["segment"]): x for x in rows
            if x.get("eval_kind", "validation") == "validation"}


def verdict_of(idx: dict[tuple, dict[str, Any]], signal: str) -> str:
    row = idx.get((signal, 20, "ALL", "all", "all"))
    v = (row or {}).get("verdict")
    return v if v in ("pass", "fail") else "not_validated"


def detail(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row or not row.get("detail"):
        return {}
    d = row["detail"]
    if isinstance(d, str):
        try:
            return json.loads(d)
        except ValueError:
            return {}
    return d if isinstance(d, dict) else {}


def ic_cells(idx: dict[tuple, dict[str, Any]], signal: str, region: str | None) -> list[dict[str, Any]]:
    out = []
    for h in HORIZONS:
        a = idx.get((signal, h, "ALL", "all", "all")) or {}
        g = idx.get((signal, h, region, "all", "all")) if region else None
        out.append({"horizon": h, "mean_ic": r(a.get("mean_ic"), 4), "aligned_ic": r(a.get("aligned_ic"), 4),
                    "t_stat": r(a.get("t_stat"), 2), "p_bh": r(a.get("p_bh"), 4),
                    "region_ic": r((g or {}).get("mean_ic"), 4), "region_t": r((g or {}).get("t_stat"), 2)})
    return out


def badge(verdict: str, aligned_ic: float | None, t: float | None, tradable: bool | None = None) -> dict[str, str]:
    if verdict == "pass":
        ic = f" {aligned_ic:+.3f}" if aligned_ic is not None else ""
        if tradable is False:
            return {"label": f"rank IC{ic}", "tone": "info"}
        return {"label": f"edge · IC{ic}", "tone": "up"}
    if verdict == "fail":
        return {"label": "no reliable edge", "tone": "neutral"}
    return {"label": "not validated", "tone": "neutral"}


def factor_items(row: dict[str, Any], idx: dict[tuple, dict[str, Any]], region: str | None) -> list[dict[str, Any]]:
    out = []
    for k in F.FACTORS:
        v = verdict_of(idx, k)
        cells = ic_cells(idx, k, region if region in GATE_REGIONS else None)
        c20 = next(c for c in cells if c["horizon"] == 20)
        eco = (detail(idx.get((k, 20, "ALL", "all", "all"))) or {}).get("economics") or {}
        tradable = eco.get("tradable_after_costs")
        pct = r(row.get(f"pct_{k}"), 2)
        out.append({"id": k, "label": F.LABEL[k], "reference": F.REFERENCE[k], "sign": F.SIGN[k],
                    "direction_note": DIRECTION_NOTE[k], "value": r(row.get(k), 6), "percentile": pct,
                    "aligned_percentile": (None if pct is None else round(pct if F.SIGN[k] > 0 else 100.0 - pct, 2)),
                    "ic": cells, "verdict": v, "experimental": v != "pass",
                    "badge": badge(v, c20["aligned_ic"], c20["t_stat"], tradable),
                    "tradable": tradable, "note": economics_note(eco) if v == "pass" else None,
                    "cross_section": "region" if k == "indmom" else (row.get("pct_scope") or "sector")})
    return out


def economics_note(eco: dict[str, Any]) -> str | None:
    if not eco:
        return None
    sr, ss = eco.get("us_ls_sharpe_net"), eco.get("simple_spread_20d")
    parts = []
    if sr is not None:
        parts.append(f"US quintile long-short Sharpe after costs {sr:+.2f}")
    if ss is not None:
        parts.append(f"mean (simple-return) top-bottom decile spread {ss * 100:+.2f}% per 20 sessions")
    head = ("Tradable after costs" if eco.get("tradable_after_costs") else
            "Rank (median) edge only: not profitable as a stand-alone quintile strategy after costs")
    return head + (" (" + "; ".join(parts) + ")" if parts else "")


def composite_item(row: dict[str, Any] | None, idx: dict[tuple, dict[str, Any]]) -> dict[str, Any]:
    v = verdict_of(idx, "composite")
    top = idx.get(("composite", 20, "ALL", "all", "all")) or {}
    d = detail(top)
    members = d.get("members") or []
    out = {"verdict": v, "experimental": v != "pass", "members": members, "variant": d.get("variant"),
           "value": r((row or {}).get("composite"), 2) if v == "pass" else None,
           "percentile": r((row or {}).get("composite_pct"), 2) if v == "pass" else None,
           "ic_20": r(top.get("aligned_ic"), 4), "t_20": r(top.get("t_stat"), 2),
           "v2_ic_20": r(d.get("v2_ic_20"), 4), "beats_v2": d.get("beats_v2"),
           "note": LABELS["composite"] if v == "pass" else LABELS["composite_failed"]}
    return out
