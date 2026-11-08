"""Causal designs: difference-in-differences (2x2, TWFE, event study, staggered Callaway-Sant'Anna), synthetic
control, and event studies with CAR / BHAR (owner: model-studio)."""

from __future__ import annotations

from datetime import date as Date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .. import _algos as A
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, coef_rows, series, ts_band
from .panel import lm_coefs


class TreatEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str = Field(description="entity_id or entity_label (ticker / sector)")
    date: Date


class Treatment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["entities", "attribute", "column", "events"] = Field(
        default="entities", description="entities: listed entities from `date`; attribute: entities whose sector_id/"
        "region/country is in `values` from `date`; column: 0/1 dataset column, adoption = first date it is 1; "
        "events: explicit (entity, date) list (staggered adoption)")
    entities: list[str] = Field(default_factory=list)
    attribute: Literal["sector_id", "region", "country"] | None = None
    values: list[str] = Field(default_factory=list)
    column: str | None = None
    date: Date | None = None
    events: list[TreatEvent] = Field(default_factory=list)


def _match(df: pd.DataFrame, names: list[str]) -> set[str]:
    return A.match_entities(df, names)


def resolve_treatment(df: pd.DataFrame, t: Treatment) -> dict[str, Any]:
    """entity_id -> adoption date (absent = never treated)."""
    try:
        return A.resolve_treatment_dates(df, t.mode, t.entities, t.attribute, t.values, t.column, t.date,
                                         [(e.entity, e.date) for e in t.events])
    except ValueError as e:
        raise GuardrailError("treatment", str(e)) from e


class DiDParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    design: Literal["2x2", "twfe", "event_study", "staggered"] = "event_study"
    treatment: Treatment
    leads: int = Field(default=6, ge=1, le=60, description="event study: pre periods (binned beyond)")
    lags: int = Field(default=12, ge=0, le=120, description="event study: post periods (binned beyond)")
    control: Literal["never", "notyet"] = Field(default="never", description="staggered: never-treated or not-yet-treated controls")
    bootstrap: int = Field(default=199, ge=49, le=2000, description="staggered: entity bootstrap replications")


@register
class DiD(Method):
    id = "did"
    family = "causal"
    title = "Difference-in-differences"
    description = ("Classic 2x2, two-way fixed effects, event study with leads/lags (reference period -1, joint "
                   "pre-trend test) and staggered adoption with Callaway-Sant'Anna group-time ATTs aggregated "
                   "overall and by event time. Treatment from listed entities, an attribute (sector...), a 0/1 "
                   "dataset column or explicit (entity, date) events.")
    shapes = ("panel",)
    min_x = 0
    Params = DiDParams
    outputs = ("ATT", "event-time path (event_path plot)", "pre-trend test", "group-time ATT table", "2x2 means")
    diagnostics = ("pre-trend joint Wald test", "treated / control counts")

    def extra_columns(self, params: DiDParams) -> list[str]:
        return [params.treatment.column] if params.treatment.column else []

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.5 + n_rows * 4e-6 * (params.bootstrap / 50 if params.design == "staggered" else 1)

    def fit(self, data: FitData, p: DiDParams) -> Result:
        res = Result(self.id, self.family, f"DiD ({p.design})")
        extra = [p.treatment.column] if p.treatment.column else []
        df, dropped = data.complete(extra)
        for c in ("sector_id", "region", "country"):
            if c in data.frame.columns and c not in df.columns:
                m = data.frame.select("entity_id", c).unique("entity_id").to_pandas()
                df = df.merge(m, on="entity_id", how="left")
        res.summary["rows_dropped_missing"] = dropped
        tdates = resolve_treatment(df, p.treatment)
        tp = A.treat_period(df, tdates)
        n_tr, n_ct = int(tp.notna().sum()), int(tp.isna().sum())
        res.summary.update({"dependent": data.y, "design": p.design, "n_treated": n_tr, "n_control": n_ct,
                            "n_obs": len(df)})
        if n_tr == 0 or n_ct == 0:
            raise GuardrailError("treatment", f"need treated and control entities (treated {n_tr}, control {n_ct})")
        y = data.y
        if p.design == "2x2":
            dates = {pd.Timestamp(v) for v in tdates.values()}
            if len(dates) > 1:
                raise GuardrailError("treatment", "2x2 needs a single treatment date (use staggered)")
            out = A.did_2x2(df, y, pd.Series({e: 1.0 for e in tdates}), next(iter(dates)))
            r = out["res"]
            ci = np.asarray(r.conf_int())
            res.coefficients = coef_rows(list(r.params.index), r.params, r.bse, r.tvalues, r.pvalues, ci)
            m = out["means"]
            res.add_table("means_2x2", "Mean outcome (rows treated 0/1, columns post 0/1)", ["treated", "pre", "post"],
                          clean([[int(i), m.loc[i].get(0.0), m.loc[i].get(1.0)] for i in m.index]))
            att, se = float(r.params["did"]), float(r.bse["did"])
        elif p.design == "twfe":
            pos, _ = A.period_index(df["date"])
            D = (df["date"].map(pos) >= df["entity_id"].map(tp)).astype(float)
            r = A.twfe(df, y, D, list(data.x))
            res.coefficients = lm_coefs(r)
            att, se = float(r.params["D"]), float(r.std_errors["D"])
            if len({pd.Timestamp(v) for v in tdates.values()}) > 1:
                res.warn("twfe_staggered", "staggered adoption: TWFE weights can be negative / biased with "
                         "heterogeneous effects; compare with design=staggered (Callaway-Sant'Anna)")
        elif p.design == "event_study":
            out = A.event_study(df, y, tp, p.leads, p.lags, list(data.x))
            r = out["res"]
            res.coefficients = lm_coefs(r)
            path = out["path"]
            self._path(res, [k for k, *_ in path], [b for _, b, *_ in path], [lo for *_, lo, _ in path],
                       [hi for *_, hi in path], "Event-study coefficients (reference period -1)")
            if out["pre_test"]:
                stat, k, pv = out["pre_test"]
                res.add_test("Pre-trend (all leads = 0)", stat, pv, k, "parallel pre-trends", group="diagnostics")
                if pv < 0.05:
                    res.warn("pretrend", f"joint pre-trend test rejects (p={pv:.3f}): parallel trends doubtful")
            att, se = out["att"], out["att_se"]
            res.info("att_definition", "ATT = average of the post-period event-time coefficients (delta-method SE)")
            res.add_table("event_path", "Event-time coefficients", ["rel_period", "estimate", "std_error", "ci_low", "ci_high"],
                          clean([list(x) for x in path]))
        else:
            try:
                cs = A.callaway_santanna(df, y, tp, p.control, p.bootstrap, data.seed)
            except ValueError as e:
                raise GuardrailError("treatment", str(e)) from e
            att, se = cs["overall"]
            dyn = cs["dynamic"]
            z = 1.959964
            self._path(res, dyn["event_time"].tolist(), dyn["att"].tolist(), (dyn["att"] - z * dyn["se"]).tolist(),
                       (dyn["att"] + z * dyn["se"]).tolist(), "Dynamic ATT by event time (Callaway-Sant'Anna)")
            gt = cs["group_time"]
            res.add_table("group_time_att", "Group-time ATT(g, t)", list(gt.columns), clean(gt.values.tolist()[:2000]))
            res.add_table("dynamic_att", "ATT by event time", list(dyn.columns), clean(dyn.values.tolist()))
            pre = dyn[dyn["event_time"] < 0]
            if len(pre):
                zz = pre["att"] / pre["se"].replace(0, np.nan)
                stat = float(np.nansum(zz ** 2))
                from scipy import stats as _st
                pv = float(_st.chi2.sf(stat, len(pre)))
                res.add_test("Pre-period ATT = 0 (sum of squared z)", stat, pv, len(pre), "no pre-trends",
                             group="diagnostics", note="ignores covariance between event times")
            res.summary.update({"n_groups": cs["n_groups"], "control": p.control, "bootstrap": cs["bootstrap"]})
        if att is not None:
            row = {"term": "ATT", "estimate": att, "std_error": se}
            if se:
                row.update(statistic=att / se, p_value=float(2 * (1 - __import__("scipy").stats.norm.cdf(abs(att / se)))),
                           ci_low=att - 1.959964 * se, ci_high=att + 1.959964 * se)
            res.add_table("att", "Average treatment effect on the treated", list(row), [clean(list(row.values()))])
        res.metrics.update(clean({"att": att, "att_se": se, "n_treated": n_tr, "n_obs": len(df)}))
        res.fit_stats.update(clean({"att": att, "att_se": se}))
        return res

    @staticmethod
    def _path(res: Result, x: list, est: list, lo: list, hi: list, title: str) -> None:
        res.plots["event_path"] = clean({"kind": "event_path", "title": title, "x_label": "periods relative to treatment",
                                         "y_label": "effect", "x": x, "y": est, "lower": lo, "upper": hi,
                                         "reference": -1, "event_x": 0})


class SynthParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    treated: str = Field(description="treated entity (entity_id or label)")
    treatment_date: Date
    donors: list[str] = Field(default_factory=list, description="donor pool (default: all other entities)")
    placebo: bool = Field(default=False, description="in-space placebo inference (heavy extra: one synthetic control "
                          "per donor; opt-in since S1.4)")
    max_donors: int = Field(default=60, ge=2, le=300)


@register
class SyntheticControl(Method):
    id = "synthetic_control"
    family = "causal"
    title = "Synthetic control"
    description = ("Abadie-style synthetic control on the outcome path: non-negative donor weights summing to 1 fit "
                   "the pre-period; effect = treated - synthetic; in-space placebo inference on the post/pre RMSPE ratio.")
    shapes = ("panel",)
    min_x = 0
    Params = SynthParams
    outputs = ("donor weights", "treated vs synthetic (ts_band)", "gap", "placebo gaps", "RMSPE ratio p-value")
    diagnostics = ("pre-period RMSPE", "placebo rank p-value")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.5 + (params.max_donors ** 2) * 0.004 if params.placebo else 0.5

    def fit(self, data: FitData, p: SynthParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, _ = data.complete()
        W = df.pivot_table(index="date", columns="entity_id", values=data.y, aggfunc="first").sort_index()
        tr = _match(df, [p.treated])
        if len(tr) != 1:
            raise GuardrailError("treatment", f"treated entity {p.treated!r} not found (or ambiguous)")
        tid = next(iter(tr))
        donors = sorted(_match(df, p.donors)) if p.donors else [c for c in W.columns if c != tid]
        W = W[[tid] + [d for d in donors if d != tid]].dropna(axis=1, how="any")
        if tid not in W.columns:
            raise GuardrailError("treatment", "treated unit has missing outcome periods (complete panel needed)")
        if W.shape[1] - 1 > p.max_donors:
            W = W.iloc[:, : p.max_donors + 1]
            res.info("donors_truncated", f"donor pool truncated to {p.max_donors}")
        if W.shape[1] < 3:
            raise GuardrailError("too_few_obs", "need at least 2 donors with a complete outcome path")
        dates = list(W.index)
        t0 = next((i for i, d in enumerate(dates) if pd.Timestamp(d) >= pd.Timestamp(p.treatment_date)), None)
        if t0 is None or t0 < 4:
            raise GuardrailError("treatment", "need >= 4 pre-treatment periods and a treatment date inside the window")
        data.check()
        sc = A.synthetic_control(W, tid, t0, p.placebo)
        w = sc["weights"].sort_values(ascending=False)
        labels = df.drop_duplicates("entity_id").set_index("entity_id")["entity_label"]
        res.add_table("weights", "Donor weights", ["donor", "label", "weight"],
                      clean([[d, labels.get(d), v] for d, v in w.items() if v > 1e-4]))
        y1 = W[tid].to_numpy(float)
        ts_band(res, "synthetic", f"{labels.get(tid, tid)} vs synthetic control", dates,
                [("treated", y1), ("synthetic", sc["synthetic"])], vlines=[{"x": dates[t0], "label": "treatment"}])
        lines = [("gap", sc["gap"])] + [(f"placebo {labels.get(k, k)}", v["gap"]) for k, v in sc["placebos"].items()
                                        if v["pre_rmspe"] <= 5 * sc["pre_rmspe"]][:60]
        ts_band(res, "gaps", "Gap (treated - synthetic) with placebo gaps", dates, lines,
                vlines=[{"x": dates[t0], "label": "treatment"}], hlines=[{"y": 0, "label": ""}])
        post_gap = float(np.mean(sc["gap"][t0:]))
        res.fit_stats.update(clean({"pre_rmspe": sc["pre_rmspe"], "post_rmspe": sc["post_rmspe"],
                                    "rmspe_ratio": sc["ratio"], "avg_post_gap": post_gap,
                                    "placebo_p_value": sc.get("p_value")}))
        if sc.get("p_value") is not None:
            res.add_test("Placebo RMSPE-ratio rank", sc["ratio"], sc["p_value"], len(sc["placebos"]),
                         "no effect (treated ratio typical among placebos)", group="inference", passed=None)
        sd = float(np.std(y1[:t0]))
        if sd and sc["pre_rmspe"] > 0.5 * sd:
            res.warn("poor_prefit", "pre-period fit is poor (RMSPE > half the outcome's pre-period SD)")
        res.summary.update({"dependent": data.y, "treated": labels.get(tid, tid), "n_donors": W.shape[1] - 1,
                            "pre_periods": t0, "post_periods": len(dates) - t0, "n_obs": int(W.size)})
        res.metrics.update(clean({"avg_post_gap": post_gap, "pre_rmspe": sc["pre_rmspe"], "placebo_p": sc.get("p_value")}))
        return res


class CarParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[TreatEvent] = Field(default_factory=list, description="explicit events (entity, date)")
    event_column: str | None = Field(default=None, description="or: 0/1 dataset column; every 0->1 switch is an event")
    model: Literal["market", "mean"] = Field(default="market", description="market model or constant-mean benchmark")
    market: str | None = Field(default=None, description="market return column (default: equal-weight mean of all entities)")
    est_window: list[int] = Field(default=[-250, -30], min_length=2, max_length=2)
    event_window: list[int] = Field(default=[-5, 5], min_length=2, max_length=2)
    max_events: int = Field(default=2000, ge=2, le=20000)


@register
class EventCar(Method):
    id = "event_car"
    family = "causal"
    title = "Event study (CAR / BHAR)"
    description = ("Abnormal returns around events against a market model (estimated on the estimation window) or a "
                   "constant-mean benchmark; CAAR path with confidence band, BHAR, cross-sectional t and Patell Z.")
    shapes = ("panel",)
    min_x = 0
    Params = CarParams
    outputs = ("CAAR path (event_path)", "AAR", "per-event CAR / BHAR", "t and Patell tests")
    diagnostics = ("cross-sectional t", "Patell Z")

    def extra_columns(self, params: CarParams) -> list[str]:
        return [c for c in (params.event_column, params.market) if c]

    def fit(self, data: FitData, p: CarParams) -> Result:
        res = Result(self.id, self.family, self.title)
        cols = [data.y] + [c for c in (p.event_column, p.market) if c]
        f = data.frame.select(["date", "entity_id", "entity_label"] + cols).to_pandas()
        R = f.pivot_table(index="date", columns="entity_id", values=data.y, aggfunc="first").sort_index()
        if p.market:
            mkt = f.groupby("date")[p.market].first().reindex(R.index)
        else:
            mkt = R.mean(axis=1)
            res.info("ew_market", "market return = equal-weight mean of the dataset's entities")
        events: list[tuple[str, Any]] = []
        if p.event_column:
            g = f.sort_values(["entity_id", "date"])
            on = g[p.event_column].fillna(0) > 0.5
            start = on & ~on.groupby(g["entity_id"]).shift(1, fill_value=False)
            events = list(zip(g.loc[start, "entity_id"], g.loc[start, "date"], strict=True))
        for ev in p.events:
            for e in _match(f, [ev.entity]):
                events.append((e, ev.date))
        if not events:
            raise GuardrailError("no_events", "define events (params.events or params.event_column)")
        if len(events) > p.max_events:
            res.info("events_truncated", f"{len(events)} events; using the first {p.max_events}")
            events = events[: p.max_events]
        try:
            out = A.car_study(R, mkt, events, tuple(p.est_window), tuple(p.event_window), p.model)
        except ValueError as e:
            raise GuardrailError("too_few_obs", str(e)) from e
        z = 1.959964
        lo = out["caar"] - z * out["caar_se"]
        hi = out["caar"] + z * out["caar_se"]
        res.plots["event_path"] = clean({"kind": "event_path", "title": "Cumulative average abnormal return (CAAR)",
                                         "x_label": "periods relative to event", "y_label": "CAAR", "x": out["rel"],
                                         "y": out["caar"], "lower": lo, "upper": hi, "reference": None, "event_x": 0})
        res.line_plot("aar", "Average abnormal return (AAR)", [series("AAR", out["rel"], out["aar"])], "relative period",
                      "AAR", kind="bar")
        ev = out["events"]
        lab = f.drop_duplicates("entity_id").set_index("entity_id")["entity_label"]
        names = (data.frame.select("entity_id", "entity_name").unique("entity_id").to_pandas().set_index("entity_id")["entity_name"]
                 if "entity_name" in data.frame.columns else None)
        ev.insert(1, "ticker", ev["entity_id"].map(lab))
        ev.insert(2, "name", ev["entity_id"].map(names) if names is not None else None)
        res.add_table("events", "Per-event results", list(ev.columns), clean(ev.values.tolist()[:2000]))
        res.add_test("Cross-sectional t (CAR = 0)", out["t_cs"], out["p_cs"], out["n_events"] - 1, "mean CAR = 0",
                     group="inference", passed=None)
        res.add_test("Patell Z", out["z_patell"], out["p_patell"], None, "mean standardized CAR = 0",
                     group="inference", passed=None)
        caar = float(out["caar"][-1])
        res.coefficients = [clean({"term": "CAAR", "estimate": caar, "std_error": float(out["caar_se"][-1]),
                                   "statistic": out["t_cs"], "p_value": out["p_cs"], "ci_low": float(lo[-1]),
                                   "ci_high": float(hi[-1])})]
        res.summary.update({"dependent": data.y, "n_events": out["n_events"], "model": p.model,
                            "est_window": p.est_window, "event_window": p.event_window, "n_obs": out["n_events"]})
        res.fit_stats.update(clean({"caar": caar, "mean_bhar": out["mean_bhar"], "t_cs": out["t_cs"],
                                    "z_patell": out["z_patell"]}))
        res.metrics.update(clean({"caar": caar, "mean_bhar": out["mean_bhar"], "n_events": out["n_events"]}))
        if out["n_events"] < 20:
            res.warn("few_events", f"only {out['n_events']} events: tests have little power")
        return res
