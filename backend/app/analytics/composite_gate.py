"""Out-of-sample validation gate of the stored composite scores (coverage v3 A8, owner analytics-scoring).
Job id `composite_validation` (manual).

Why: the composites are a rules-based, peer-relative ranking. The only out-of-sample evidence on record shows no
predictive skill (Model Studio run SR20261012T0319222S5ZX, factor scores -> P(3M outperformance), mvp_us, purged
5-fold: AUC 0.490). So every composite is labelled **descriptive** in the API and the UI, and "signal",
"predictive" or buy / sell wording is allowed only for a profile whose recorded out-of-sample gate PASSED.

Gate (contracts/scoring.yaml composite.validation.gate; same pattern as the technicals gates, a JSON verdict file):

* panel: ONE point-in-time scoring date D per calendar month and profile of `composite_scores` (the month's last
  as_of_date: history mode writes month-ends, the nightly run adds daily rows that are thinned away; strict PIT
  inputs: knowledge_date <= D) joined to the first `ml_labels` rebalance date L >= D (L - D <= 7 days) of the same
  security, each (profile, security, L) used once; the label is `fwd_excess_sector` from close(L+1) to close(L+1+h),
  so nothing on or before D is in the label. The composite weights are fixed rules, never fitted on returns, so every
  date is out-of-sample with respect to the weights (the lake has no history before the weights to hold out further);
* per date: Spearman rank IC(score, fwd_excess_sector) and AUC(score -> fwd_excess_sector > 0) (Mann-Whitney);
* significance: Newey-West (HAC) t of the per-date mean (rank IC vs 0, AUC vs 0.5), maxlags = the horizon overlap in
  scoring dates + 1 (monthly dates: horizon months + 1; derived from the median date spacing, never fewer);
  one-sided normal p of the t;
* PASS = distinct months >= min_dates, rank IC mean > 0 with t >= min_rank_ic_t AND AUC mean > 0.5 with
  t >= min_auc_t.
* survivorship (review A7_A8): the stored history scores TODAY's MVP universe backwards (universe_membership history
  only starts when the universe was first built, 2026-10-09/10), so earlier months hold survivors only. The panel is
  masked point-in-time by universe_membership (universes of valuation.UNIVERSES['mvp']; Model Studio's convention:
  the initial snapshot, valid_from within 30 days of a universe's first row, counts as members since inception,
  later additions keep their valid_from, exits via valid_to stay point-in-time), and only the SURVIVORSHIP-FREE
  months (scoring date >= the membership history start, the latest first valid_from of those universes) can decide a
  pass: with >= min_dates such months their metrics decide (full-history metrics kept as `full_history`); with fewer,
  the full-history metrics are recorded with survivorship_biased = true and a would-be pass becomes 'not_validated'
  (a fail stays 'fail'). Membership unavailable -> the same (never 'pass'). verdict() also refuses a recorded 'pass'
  without survivorship_biased == false (older documents).

Output: data/reports/composite/gate.json {gate_version, canonical, evaluated_at, criteria, horizon, profiles: {pid:
{verdict, rank_ic_mean, rank_ic_t, auc_mean, auc_t, auc_p, n_dates, n_months, n_obs, start, end, survivorship
{universes, membership_history_start, survivorship_biased, n_months_survivorship_free, decided_on,
rows_dropped_by_membership}, universe {table, scopes, regions, n_securities, security_ids}, horizons: {...}}}}.
No lake table. Only a CANONICAL run (every stored profile, the
contract horizon first, no start / end / max_securities) writes the live file; any other run without out_dir writes
data/reports/composite/runs/gate_<run_id>.json, and the readers never trust a gate document that is not canonical.
A passed profile is "validated" only for composites inside the population it was tested on (its score scope and the
panel's security ids, recorded in the gate); every other composite stays descriptive.
Params: profiles (default: every stored profile), horizons (default [3M, 1M]; the first decides), start, end,
max_securities (sample cap, e.g. 25 for a dry run), out_dir (write the gate file there instead), dry_run (no file).
Memory: lazy scans of two small tables (~0.7 M composite rows, ~1 M label rows) + issuers (id, region) +
universe_membership (~30 k rows): < 1 GB.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

GATE_VERSION = 1
DEFAULT_CRITERIA: dict[str, Any] = {
    "horizon": "3M", "min_dates": 24, "min_names_per_date": 30, "min_rank_ic_t": 2.0, "min_auc_t": 2.0,
    "label_join_tolerance_days": 7,
}
HORIZON_MONTHS = {"1M": 1, "3M": 3, "6M": 6}
LABEL = "descriptive"
NOTE_DESCRIPTIVE = ("Descriptive: a rules-based, peer-relative ranking of the factor percentiles, not a forecast. "
                    "No recorded out-of-sample test shows predictive power (Model Studio, factor scores -> 3M "
                    "outperformance: AUC 0.49).")
NOTE_VALIDATED = ("Validated profile: its recorded out-of-sample gate passed (rank IC and AUC significant, "
                  "Newey-West t >= 2). Still a ranking, not an expected return.")
#: universes of the MVP-scope composites (composite_scores; = app.analytics.valuation.UNIVERSES['mvp'])
MEMBERSHIP_UNIVERSES = ("mvp_us", "mvp_global")
#: initial-snapshot grace (Model Studio _core.resolve_entities): rows of a universe's first build passes
MEMBERSHIP_GRACE_DAYS = 30
_INCEPTION = date(1900, 1, 1)


# ------------------------------------------------------------------------------------------------ contract / file


_SPEC: dict[str, tuple[float, dict[str, Any]]] = {}


def _composite_spec(settings: Settings | None = None) -> dict[str, Any]:
    """scoring.yaml `composite` (re-read when the file changes; no import of the scoring engine)."""
    import yaml  # noqa: PLC0415

    p = (settings or get_settings()).contracts_dir / "scoring.yaml"
    mt = p.stat().st_mtime
    hit = _SPEC.get(str(p))
    if hit is None or hit[0] != mt:
        hit = (mt, (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("composite") or {})
        _SPEC[str(p)] = hit
    return hit[1]


def criteria(settings: Settings | None = None) -> dict[str, Any]:
    """Gate thresholds: scoring.yaml composite.validation.gate over DEFAULT_CRITERIA."""
    out = dict(DEFAULT_CRITERIA)
    try:
        g = (_composite_spec(settings).get("validation") or {}).get("gate") or {}
        out.update({k: v for k, v in g.items() if k in DEFAULT_CRITERIA})
    except Exception as e:  # noqa: BLE001 - contract unreadable: defaults
        log.info("composite gate criteria from defaults: %s", e)
    return out


def evidence(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Out-of-sample evidence recorded in the contract (scoring.yaml composite.validation.evidence)."""
    try:
        ev = (_composite_spec(settings).get("validation") or {}).get("evidence") or []
        return [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in dict(e).items()} for e in ev]
    except Exception:  # noqa: BLE001
        return []


def gate_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / "reports" / "composite" / "gate.json"


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def load_gate(settings: Settings | None = None) -> dict[str, Any]:
    """The recorded gate ({} when never run / unreadable). Re-read only when the file's mtime changes."""
    p = gate_file(settings)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    with _CACHE_LOCK:
        hit = _CACHE.get(str(p))
        if hit and hit[0] == mt:
            return hit[1]
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc = doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        doc = {}
    with _CACHE_LOCK:
        _CACHE[str(p)] = (mt, doc)
    return doc


def trusted(gate: dict[str, Any]) -> bool:
    """A gate document the labels may rely on: this gate version, written by a canonical run (every stored profile,
    the contract horizon, the full history, no sample cap). A sample / partial run never unlocks signal wording."""
    return (gate.get("gate_version") == GATE_VERSION and gate.get("canonical") is True
            and not gate.get("max_securities") and not gate.get("start") and not gate.get("end"))


def verdict(gate: dict[str, Any], profile_id: str) -> str:
    """'pass' | 'fail' | 'not_validated' (no trusted gate recorded for the profile). A recorded 'pass' counts only
    when its deciding panel was survivorship-free (survivorship.survivorship_biased is false): an older document
    without that record, or a biased one, is 'not_validated' (review A7_A8)."""
    if not trusted(gate):
        return "not_validated"
    g = (gate.get("profiles") or {}).get(profile_id) or {}
    v = g.get("verdict")
    if v == "pass" and (g.get("survivorship") or {}).get("survivorship_biased") is not False:
        return "not_validated"
    return v if v in ("pass", "fail") else "not_validated"


def is_validated(profile_id: str, settings: Settings | None = None, gate: dict[str, Any] | None = None) -> bool:
    """The profile passed its trusted gate (on the universe it was tested on: see label_for for given composites)."""
    return verdict(load_gate(settings) if gate is None else gate, profile_id) == "pass"


def gate_universe(gate: dict[str, Any], profile_id: str) -> dict[str, Any]:
    """{table, scopes, regions, n_securities, security_ids} the profile's gate was computed on ({} = unknown)."""
    u = ((gate.get("profiles") or {}).get(profile_id) or {}).get("universe")
    return u if isinstance(u, dict) else {}


def _tested_ids(gate: dict[str, Any], profile_id: str) -> frozenset[str]:
    """The security ids of the profile's gate panel (memoised on the cached gate document)."""
    memo = gate.setdefault("_tested_ids", {})
    hit = memo.get(profile_id)
    if hit is None:
        hit = memo[profile_id] = frozenset(str(x) for x in (gate_universe(gate, profile_id).get("security_ids") or []))
    return hit


def label_for(profile_id: str, scope: str | None, security_ids: Any,
              settings: Settings | None = None, gate: dict[str, Any] | None = None) -> str:
    """'validated' only when the profile passed its trusted gate AND the composites in question lie inside the
    population it tested: their score scope (mvp | extended) is one the gate tested and every security was in its
    panel (a US-only pass never validates a Japanese home listing or an extended-scope score, whatever the issuer's
    region). No scope, no securities, or a gate without a recorded panel -> 'descriptive' (fail-closed)."""
    ids = [str(x) for x in (security_ids or []) if x]
    g = load_gate(settings) if gate is None else gate
    if verdict(g, profile_id) != "pass" or not scope or not ids:
        return LABEL
    if scope not in set(gate_universe(g, profile_id).get("scopes") or []):
        return LABEL
    tested = _tested_ids(g, profile_id)
    return "validated" if all(i in tested for i in ids) else LABEL


def composite_label(profile_id: str, settings: Settings | None = None, gate: dict[str, Any] | None = None, *,
                    scope: str | None = None, security_id: str | None = None) -> str:
    """Label of ONE composite (its score_scope and security): 'validated' only behind a passed out-of-sample gate
    whose panel contains it; otherwise 'descriptive'."""
    return label_for(profile_id, scope, [security_id] if security_id else None, settings, gate)


def public_universe(u: dict[str, Any] | None) -> dict[str, Any] | None:
    """The gate universe without the security id list (API metadata)."""
    return {k: v for k, v in u.items() if k != "security_ids"} if u else None


_SURV_PUBLIC = ("universes", "membership_history_start", "survivorship_biased", "n_months_survivorship_free",
                "decided_on")


def public_survivorship(sv: dict[str, Any] | None) -> dict[str, Any] | None:
    """The survivorship record of a profile's gate (API metadata)."""
    return {k: sv.get(k) for k in _SURV_PUBLIC} if isinstance(sv, dict) and sv else None


def validation_meta(profile_ids: list[str] | None = None, default_profile: str = "balanced",
                    settings: Settings | None = None) -> dict[str, Any]:
    """Public summary for GET /meta/factors (`composite_validation`): label per profile, the gate criteria, the
    recorded gate metrics and the contract's evidence. `signal_wording` is true only for a passed profile."""
    s = settings or get_settings()
    gate = load_gate(s)
    ok = trusted(gate)
    gp = (gate.get("profiles") or {}) if ok else {}
    pids = list(profile_ids or []) or sorted(gp)
    profiles: dict[str, Any] = {}
    for pid in pids:
        g = gp.get(pid) or {}
        v = verdict(gate, pid)
        profiles[pid] = {
            "label": "validated" if v == "pass" else LABEL, "verdict": v, "signal_wording": v == "pass",
            **{k: g.get(k) for k in ("horizon", "rank_ic_mean", "rank_ic_t", "auc_mean", "auc_t", "auc_p",
                                    "n_dates", "n_months", "n_obs", "start", "end")},
            # the population the gate tested: "validated" applies only to composites inside it
            "universe": public_universe(gate_universe(gate, pid)) if ok else None,
            # whether the recorded metrics come from a survivorship-free panel (review A7_A8)
            "survivorship": public_survivorship(g.get("survivorship")) if ok else None}
    dv = profiles.get(default_profile, {}).get("verdict", "not_validated")
    note = NOTE_VALIDATED if dv == "pass" else NOTE_DESCRIPTIVE
    sv = profiles.get(default_profile, {}).get("survivorship") or {}
    if sv.get("survivorship_biased"):
        hs = sv.get("membership_history_start")
        note += (f" The recorded gate panel is survivorship-biased (universe membership history "
                 f"{'starts ' + hs if hs else 'unavailable'}; earlier months score today's universe): a pass needs "
                 f">= {criteria(s)['min_dates']} survivorship-free scoring months.")
    return {
        "label": "validated" if dv == "pass" else LABEL,
        "default_profile": default_profile,
        "signal_wording": dv == "pass",
        "note": note,
        "custom_weights_label": LABEL,   # Screener re-weighting: never validated
        "evaluated_at": gate.get("evaluated_at") if ok else None,
        "gate_recorded": bool(gp),
        "universe": profiles.get(default_profile, {}).get("universe"),
        "criteria": {**criteria(s), "rule": "rank IC mean > 0 with Newey-West t >= min_rank_ic_t AND AUC mean > 0.5 "
                                            "with t >= min_auc_t, over >= min_dates point-in-time scoring dates"},
        "profiles": profiles,
        "evidence": evidence(s),
    }


# ------------------------------------------------------------------------------------------------ statistics


def _hac(x: np.ndarray, maxlags: int) -> tuple[float, float]:
    """Newey-West mean and t of a per-date series (Bartlett kernel), without statsmodels."""
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return float("nan"), float("nan")
    m = float(x.mean())
    if n < 3:
        return m, float("nan")
    e = x - m
    L = max(0, min(int(maxlags), n - 1))
    s = float(e @ e) / n
    for k in range(1, L + 1):
        s += 2.0 * (1.0 - k / (L + 1)) * float(e[k:] @ e[:-k]) / n
    se = math.sqrt(s / n) if s > 0 else 0.0
    return m, (m / se if se > 0 else float("nan"))


def _p_one_sided(t: float) -> float | None:
    return None if t is None or not math.isfinite(t) else 0.5 * math.erfc(t / math.sqrt(2.0))


def per_date_metrics(panel: pl.DataFrame, min_names: int) -> pl.DataFrame:
    """panel [date, score, fwd_excess_sector] -> [date, n, rank_ic, auc] (dates with >= min_names rows)."""
    df = panel.drop_nulls(["score", "fwd_excess_sector"]).filter(
        pl.col("score").is_finite() & pl.col("fwd_excess_sector").is_finite())
    if df.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "n": pl.UInt32, "rank_ic": pl.Float64, "auc": pl.Float64})
    df = df.with_columns(
        pl.col("score").rank("average").over("date").alias("rs"),
        pl.col("fwd_excess_sector").rank("average").over("date").alias("ry"),
        (pl.col("fwd_excess_sector") > 0).alias("pos"))
    g = df.group_by("date").agg(
        pl.len().alias("n"),
        pl.corr("rs", "ry").alias("rank_ic"),
        pl.col("pos").sum().alias("n_pos"),
        pl.col("rs").filter(pl.col("pos")).sum().alias("rank_sum_pos"))
    n_neg = pl.col("n") - pl.col("n_pos")
    auc = ((pl.col("rank_sum_pos") - pl.col("n_pos") * (pl.col("n_pos") + 1) / 2) / (pl.col("n_pos") * n_neg))
    return (g.filter(pl.col("n") >= min_names)
            .with_columns(pl.when((pl.col("n_pos") > 0) & (n_neg > 0)).then(auc).otherwise(None).alias("auc"))
            .select("date", "n", pl.col("rank_ic").cast(pl.Float64), pl.col("auc").cast(pl.Float64))
            .sort("date"))


def hac_lags(dates: pl.Series | None, horizon: str) -> int:
    """Newey-West maxlags: how many scoring dates one forward window of `horizon` overlaps, + 1. Monthly dates ->
    horizon months + 1 (3M: 4); denser dates (a panel that was not thinned) -> proportionally more, never fewer."""
    h = HORIZON_MONTHS.get(horizon, 3)
    base = h + 1
    if dates is None or len(dates) < 3:
        return base
    gaps = dates.unique().sort().diff().drop_nulls().dt.total_days()
    med = float(gaps.median()) if len(gaps) else 0.0
    if not med or med <= 0:
        return base
    return max(base, int(round(h * 30.44 / med)) + 1)


def evaluate_panel(panel: pl.DataFrame, horizon: str, crit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gate metrics + verdict of one profile x horizon. panel: [date, security_id, score, fwd_excess_sector].
    min_dates counts distinct calendar months (several scoring dates in one month are not independent)."""
    c = {**DEFAULT_CRITERIA, **(crit or {})}
    pdm = per_date_metrics(panel, int(c["min_names_per_date"]))
    lags = hac_lags(pdm["date"] if pdm.height else None, horizon)
    ric_m, ric_t = _hac(pdm["rank_ic"].to_numpy().astype(float), lags) if pdm.height else (float("nan"), float("nan"))
    auc = pdm["auc"].drop_nulls().to_numpy().astype(float) if pdm.height else np.array([])
    auc_m, auc_t = _hac(auc - 0.5, lags)
    auc_m = auc_m + 0.5 if math.isfinite(auc_m) else float("nan")
    n_dates = pdm.height
    n_months = int(pdm["date"].dt.truncate("1mo").n_unique()) if pdm.height else 0

    def f(x: float) -> float | None:
        return round(float(x), 6) if x is not None and math.isfinite(x) else None

    reasons = []
    if n_months < int(c["min_dates"]):
        reasons.append(f"{n_months} scoring months < min_dates {c['min_dates']}")
    if not (math.isfinite(ric_m) and ric_m > 0 and math.isfinite(ric_t) and ric_t >= float(c["min_rank_ic_t"])):
        reasons.append(f"rank IC {f(ric_m)} (t {f(ric_t)}) not > 0 with t >= {c['min_rank_ic_t']}")
    if not (math.isfinite(auc_m) and auc_m > 0.5 and math.isfinite(auc_t) and auc_t >= float(c["min_auc_t"])):
        reasons.append(f"AUC {f(auc_m)} (t {f(auc_t)}) not > 0.5 with t >= {c['min_auc_t']}")
    return {"horizon": horizon, "verdict": "pass" if not reasons else "fail", "reasons": reasons,
            "rank_ic_mean": f(ric_m), "rank_ic_t": f(ric_t), "auc_mean": f(auc_m), "auc_t": f(auc_t),
            "auc_p": f(_p_one_sided(auc_t)) if math.isfinite(auc_t) else None,
            "rank_ic_p": f(_p_one_sided(ric_t)) if math.isfinite(ric_t) else None,
            "n_dates": n_dates, "n_months": n_months, "n_obs": int(pdm["n"].sum()) if pdm.height else 0,
            "start": pdm["date"].min().isoformat() if pdm.height else None,
            "end": pdm["date"].max().isoformat() if pdm.height else None,
            "hac_maxlags": lags}


_METRIC_KEYS = ("verdict", "reasons", "rank_ic_mean", "rank_ic_t", "auc_mean", "auc_t", "auc_p", "rank_ic_p",
                "n_dates", "n_months", "n_obs", "start", "end", "hac_maxlags")


def evaluate_pit(panel: pl.DataFrame, horizon: str, crit: dict[str, Any] | None = None,
                 membership_start: date | None = None, universes: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """evaluate_panel + the survivorship rule (module docstring): only scoring months on or after the universe
    membership history start can decide a pass. panel: already masked by point-in-time membership.
    membership_start None = membership unknown: the whole panel may hold survivors only (never 'pass')."""
    c = {**DEFAULT_CRITERIA, **(crit or {})}
    full = evaluate_panel(panel, horizon, c)
    free = (evaluate_panel(panel.filter(pl.col("date") >= membership_start), horizon, c)
            if membership_start is not None else None)
    n_free = int(free["n_months"]) if free else 0
    surv: dict[str, Any] = {"universes": list(universes),
                            "membership_history_start": membership_start.isoformat() if membership_start else None,
                            "n_months_survivorship_free": n_free}
    if free is not None and n_free >= int(c["min_dates"]):
        return {**free, "survivorship": {**surv, "survivorship_biased": False, "decided_on": "survivorship_free"},
                "full_history": {k: full[k] for k in _METRIC_KEYS}}
    why = (f"survivorship: {n_free} scoring months on or after the universe membership history start "
           f"{membership_start.isoformat()} < min_dates {c['min_dates']} (earlier months score today's universe "
           "backwards: survivorship-biased)" if membership_start is not None else
           "survivorship: universe membership history unavailable (the panel may hold survivors only)")
    biased = membership_start is None or bool(full["start"] and full["start"] < membership_start.isoformat())
    out = {**full, "reasons": [*full["reasons"], why],
           # a would-be pass on a survivorship-biased panel is no evidence; a fail stays a fail (fail-closed)
           "verdict": "not_validated" if full["verdict"] == "pass" else full["verdict"],
           "survivorship": {**surv, "survivorship_biased": biased, "decided_on": "full_history"}}
    return out


def membership_intervals(um: pl.LazyFrame | pl.DataFrame,
                         universes: tuple[str, ...] | list[str] = MEMBERSHIP_UNIVERSES,
                         grace_days: int = MEMBERSHIP_GRACE_DAYS) -> tuple[pl.DataFrame | None, date | None]:
    """universe_membership rows of `universes` -> (intervals [security_id, valid_from, valid_to], history start).
    Model Studio's convention (_core.resolve_entities): a universe's initial snapshot (valid_from within
    `grace_days` of its first row; or null) counts as members since inception; later additions keep their
    valid_from; exits via valid_to stay point-in-time. History start = the LATEST first valid_from over the
    universes (before it at least one universe has no recorded history: survivorship-biased). (None, None) when no
    row of the universes exists."""
    lf = um.lazy() if isinstance(um, pl.DataFrame) else um
    m = (lf.filter(pl.col("universe_id").is_in(list(universes)))
         .select("universe_id", "security_id", pl.col("valid_from").cast(pl.Date), pl.col("valid_to").cast(pl.Date))
         .collect())
    if m.is_empty():
        return None, None
    m = m.with_columns(pl.col("valid_from").min().over("universe_id").alias("_first"))
    firsts = [d for d in m.group_by("universe_id").agg(pl.col("_first").first())["_first"].to_list() if d]
    if not firsts:
        return None, None
    inception = pl.col("valid_from").is_null() | (pl.col("valid_from") <= pl.col("_first") + timedelta(days=grace_days))
    m = m.with_columns(pl.when(inception).then(pl.lit(_INCEPTION)).otherwise(pl.col("valid_from")).alias("valid_from"))
    return m.select("security_id", "valid_from", "valid_to"), max(firsts)


def apply_membership(panel: pl.DataFrame, members: pl.DataFrame | None) -> pl.DataFrame:
    """Rows whose security was a universe member on the scoring date (valid_from <= date <= valid_to, open-ended
    when valid_to is null; any interval of any universe). members None -> unchanged (membership unknown)."""
    if members is None or panel.is_empty():
        return panel
    keys = panel.select("security_id", "date").unique()
    ok = (keys.join(members, on="security_id", how="inner")
          .filter((pl.col("valid_from") <= pl.col("date"))
                  & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= pl.col("date"))))
          .select("security_id", "date").unique())
    return panel.join(ok, on=["security_id", "date"], how="semi", maintain_order="left")


# ------------------------------------------------------------------------------------------------ panel


def build_panel(scores: pl.LazyFrame, labels: pl.LazyFrame, profile_ids: list[str], horizon: str,
                start: date | None = None, end: date | None = None, max_securities: int | None = None,
                tolerance_days: int = 7) -> pl.DataFrame:
    """Point-in-time panel [profile_id, date, security_id, score, fwd_excess_sector (, issuer_id)]: ONE scoring date
    D per calendar month and profile (the month's last as_of_date - the nightly run's daily rows next to the
    month-end history would otherwise add near-duplicate, heavily overlapping dates and overstate n_dates and the
    Newey-West t), each joined to the first label date L >= D within `tolerance_days` (the label's return starts at
    close(L+1) > D), each (profile, security, L) used once (the latest D)."""
    sc = scores.filter(pl.col("profile_id").is_in(profile_ids), pl.col("score").is_not_null())
    if start:
        sc = sc.filter(pl.col("as_of_date") >= start)
    if end:
        sc = sc.filter(pl.col("as_of_date") <= end)
    has_iss = "issuer_id" in sc.collect_schema().names()
    sc = sc.select("profile_id", pl.col("as_of_date").alias("date"), "security_id", pl.col("score").cast(pl.Float64),
                   *(["issuer_id"] if has_iss else []))
    if max_securities:
        keep = (sc.select("security_id").unique().sort("security_id").head(int(max_securities)))
        sc = sc.join(keep, on="security_id", how="semi")
    last = (sc.group_by("profile_id", pl.col("date").dt.truncate("1mo").alias("_m"))
            .agg(pl.col("date").max()).select("profile_id", "date"))
    sc_df = sc.join(last, on=["profile_id", "date"], how="semi").collect()
    if sc_df.is_empty():
        return pl.DataFrame(schema={"profile_id": pl.String, "date": pl.Date, "security_id": pl.String,
                                    "score": pl.Float64, "fwd_excess_sector": pl.Float64})
    sids = sc_df["security_id"].unique().to_list()
    lb = (labels.filter(pl.col("horizon") == horizon, pl.col("security_id").is_in(sids))
          .select("security_id", pl.col("date").alias("label_date"), pl.col("fwd_excess_sector").cast(pl.Float64))
          .drop_nulls("fwd_excess_sector")
          .filter(pl.col("label_date") >= sc_df["date"].min()).collect().sort("label_date"))
    left = sc_df.sort("date")
    # both sides are sorted on the date globally, hence within every security_id group
    j = left.join_asof(lb, left_on="date", right_on="label_date", by="security_id", strategy="forward",
                       tolerance=f"{int(tolerance_days)}d", check_sortedness=False)
    j = (j.drop_nulls("fwd_excess_sector").sort("date")
         .unique(["profile_id", "security_id", "label_date"], keep="last", maintain_order=True))
    return j.select("profile_id", "date", "security_id", "score", "fwd_excess_sector",
                    *(["issuer_id"] if has_iss else [])).sort("profile_id", "date", "security_id")


def panel_universe(panel: pl.DataFrame, issuer_regions: pl.DataFrame | None,
                   table: str = "composite_scores") -> dict[str, Any]:
    """The population a profile's gate was computed on: {table, scopes, regions, n_securities, security_ids}.
    composite_scores holds the MVP-scope composites (the full-universe extension is composite_scores_ext, scope
    'extended'). security_ids (the panel's securities) decide where a pass applies; regions (issuers.region of the
    panel's issuers, ADR issuers included) are informational."""
    regions: list[str] = []
    if issuer_regions is not None and not panel.is_empty() and "issuer_id" in panel.columns:
        rg = panel.select("issuer_id").unique().join(issuer_regions, on="issuer_id", how="left")["region"]
        regions = sorted({str(x) for x in rg.drop_nulls().to_list()})
    ids = sorted(panel["security_id"].unique().to_list()) if not panel.is_empty() else []
    return {"table": table, "scopes": ["extended" if table.endswith("_ext") else "mvp"], "regions": regions,
            "n_securities": len(ids), "security_ids": ids}


def evaluate(scores: pl.LazyFrame, labels: pl.LazyFrame, profile_ids: list[str], horizons: list[str],
             crit: dict[str, Any], start: date | None = None, end: date | None = None,
             max_securities: int | None = None, issuer_regions: pl.DataFrame | None = None,
             canonical: bool = False, members: pl.DataFrame | None = None, membership_start: date | None = None,
             membership_universes: tuple[str, ...] | list[str] = MEMBERSHIP_UNIVERSES) -> dict[str, Any]:
    """The gate document (see the module docstring); the first horizon decides each profile's verdict. `canonical`:
    the caller certifies a full run (every stored profile, the contract horizon first); start / end / a sample cap
    always make the document non-canonical (never trusted by the readers). `members` / `membership_start`
    (membership_intervals): the point-in-time universe mask and the survivorship-free start; None = unknown (the
    panel is unmasked and no profile can pass)."""
    out: dict[str, Any] = {}
    if members is None:
        membership_start = None
    for h in horizons:
        panel = build_panel(scores, labels, profile_ids, h, start, end, max_securities,
                            int(crit.get("label_join_tolerance_days", 7)))
        for pid in profile_ids:
            raw = panel.filter(pl.col("profile_id") == pid)
            pp = apply_membership(raw, members)
            o = out.setdefault(pid, {"horizons": {}})
            r = evaluate_pit(pp, h, crit, membership_start, membership_universes if members is not None else ())
            r["survivorship"]["rows_dropped_by_membership"] = raw.height - pp.height
            o["horizons"][h] = r
            if h == horizons[0]:
                o["universe"] = panel_universe(pp, issuer_regions)
    for pid, o in out.items():
        main = o["horizons"][horizons[0]]
        o.update({k: main[k] for k in ("horizon", "verdict", "reasons", "rank_ic_mean", "rank_ic_t", "auc_mean",
                                       "auc_t", "auc_p", "n_dates", "n_months", "n_obs", "start", "end",
                                       "survivorship")})
    canonical = bool(canonical and not max_securities and not start and not end)
    return {"gate_version": GATE_VERSION, "canonical": canonical, "evaluated_at": datetime.now(UTC).isoformat(),
            "criteria": crit, "horizon": horizons[0], "max_securities": max_securities,
            "start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
            "method": ("point-in-time composite_scores (one scoring date per month: the month's last) x "
                       "ml_labels.fwd_excess_sector (first label date >= scoring date, each label date once), masked "
                       "by point-in-time universe_membership; per-date Spearman rank IC and Mann-Whitney AUC "
                       "(fwd_excess_sector > 0); Newey-West t of the per-date means; only scoring months on or after "
                       "the membership history start (survivorship-free) can decide a pass; fixed rule weights, "
                       "never fitted on returns"),
            "membership_history_start": membership_start.isoformat() if membership_start else None,
            "profiles": out}


def write_gate(doc: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------------------------------------ job


def _stored_profiles(settings: Settings) -> list[str]:
    try:
        return list(_composite_spec(settings).get("profiles") or {}) or ["balanced"]
    except Exception:  # noqa: BLE001
        return ["balanced"]


def _d(v: Any) -> date | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `composite_validation` (manual): records the out-of-sample gate of every stored composite profile."""
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    s = ctx.settings
    stored = _stored_profiles(s)
    profiles = list(p.get("profiles") or stored)
    crit = criteria(s)
    horizons = list(p.get("horizons") or [crit["horizon"], "1M"])
    horizons = list(dict.fromkeys(h for h in horizons if h in HORIZON_MONTHS))
    if not horizons:
        return JobResult(status="skipped", message=f"no known horizon in {p.get('horizons')}", retryable=False)
    start, end = _d(p.get("start")), _d(p.get("end"))
    cap = int(p["max_securities"]) if p.get("max_securities") else None
    canonical = set(stored) <= set(profiles) and horizons[0] == crit["horizon"] and not (start or end or cap)
    try:
        scores, labels = scan_table("composite_scores", s), scan_table("ml_labels", s)
        scores.collect_schema()
        labels.collect_schema()
    except Exception as e:  # noqa: BLE001
        return JobResult(status="skipped", message=f"inputs unavailable: {e}", retryable=False)
    try:
        iss = (scan_table("issuers", s).select("issuer_id", pl.col("region").cast(pl.String))
               .unique("issuer_id").collect())
    except Exception as e:  # noqa: BLE001 - informational only (the tested security ids decide where a pass applies)
        log.warning("composite_validation: issuers unavailable (%s) - universe regions not recorded", e)
        iss = None
    try:
        members, mstart = membership_intervals(scan_table("universe_membership", s), MEMBERSHIP_UNIVERSES)
    except Exception as e:  # noqa: BLE001 - fail-closed: no survivorship-free months, no profile can pass
        log.warning("composite_validation: universe_membership unavailable (%s) - no profile can pass", e)
        members, mstart = None, None
    doc = evaluate(scores, labels, profiles, horizons, crit, start, end, cap, issuer_regions=iss, canonical=canonical,
                   members=members, membership_start=mstart, membership_universes=MEMBERSHIP_UNIVERSES)
    doc["run_id"] = ctx.run_id
    verdicts = {pid: o["verdict"] for pid, o in doc["profiles"].items()}
    msg = ", ".join(f"{pid} {v} (IC {doc['profiles'][pid]['rank_ic_mean']}, t {doc['profiles'][pid]['rank_ic_t']}; "
                    f"AUC {doc['profiles'][pid]['auc_mean']}, t {doc['profiles'][pid]['auc_t']}; survivorship-free "
                    f"months {doc['profiles'][pid]['survivorship']['n_months_survivorship_free']})"
                    for pid, v in verdicts.items())
    if p.get("dry_run"):
        log.info("composite_validation dry run: %s", msg)
        return JobResult(status="success", message=f"dry run (no file): {msg}", entities_ok=len(verdicts))
    live = gate_file(s)
    path = Path(p["out_dir"]) / "gate.json" if p.get("out_dir") else live
    if not doc["canonical"] and path.resolve() == live.resolve():
        # a sample / partial run never replaces the live gate (it decides the UI's signal wording), not even through
        # an out_dir that points at the live directory
        path = live.parent / "runs" / f"gate_{ctx.run_id}.json"
        msg = f"not canonical (profiles / horizon / start / end / max_securities): live gate unchanged; {msg}"
    write_gate(doc, path)
    log.info("composite_validation -> %s: %s", path, msg)
    return JobResult(status="success", message=f"{path}: {msg}", entities_ok=len(verdicts))
