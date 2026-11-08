"""Synthetic macro v2 WAVE 2 tables for the API fixture lake (tests/fixtures/serving_small.py calls add_macro_v2_w2
after macro-w1's add_macro_v2). Runs the real W2 code paths offline on the fixture's macro observations: release
events (app.macro.calendar.build_events), model-based expectations (app.macro.expectations), surprises + index
(app.macro.surprise); nowcast / recession rows are synthetic but go through the same row builders."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table
from app.macro import calendar as cal
from app.macro import expectations as ex
from app.macro import recession as rc
from app.macro import surprise as su
from app.macro.nowcast import SCHEMA as NC_SCHEMA

TRACKED = [cal.Tracked("fred:PAYEMS", "US", "employment", "diff", "M", title="Nonfarm payrolls", vintages=True,
                       publisher="BLS"),
           cal.Tracked("fred:CPILFESL", "US", "core_cpi_yoy", "mom_pct", "M", title="Core CPI", publisher="BLS")]
NY = ZoneInfo("America/New_York")


def _entries(obs: dict[str, Any], today: date) -> list[cal.Entry]:
    out: list[cal.Entry] = []
    for t in TRACKED:
        o = obs.get(t.series_id)
        if o is None:
            continue
        fr = cal.first_releases(o)
        for kd in fr["knowledge_date"].tolist()[-14:]:
            out.append(cal.Entry("bls_cal", f"{t.series_id}|{kd}", f"{t.title} release", "BLS", "US", "data",
                                 datetime(kd.year, kd.month, kd.day, 8, 30, tzinfo=NY), "America/New_York", True,
                                 series_ids=[t.series_id], importance=1))
        for k, d in enumerate((today + timedelta(days=6), today + timedelta(days=37))):
            out.append(cal.Entry("bls_cal", f"{t.series_id}|future{k}", f"{t.title} release", "BLS", "US", "data",
                                 datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY), "America/New_York", True,
                                 series_ids=[t.series_id], importance=1))
    d = today + timedelta(days=9)
    out.append(cal.Entry("fomc", "fomc|next", "FOMC Meeting", "Federal Reserve", "US", "policy_meeting",
                         datetime(d.year, d.month, d.day, 14, 0, tzinfo=NY), "America/New_York", False, importance=1))
    return out


def add_macro_v2_w2(s: Settings, as_of: date) -> dict[str, Any]:
    now = datetime.now(UTC)
    today = date.today()
    obs = cal.load_obs([t.series_id for t in TRACKED], s)
    entries = _entries(obs, today)
    ev_rows: list[dict[str, Any]] = []
    for t in TRACKED:
        r, _ = cal.build_events(t, obs.get(t.series_id), entries, today - timedelta(days=1200), today, now)
        ev_rows += r
    rel = cal.entries_frame(entries, now)
    ev = cal.events_frame(ev_rows)
    counts = {"macro_releases": write_table(rel, "macro_releases", mode="overwrite", settings=s),
              "macro_release_events": write_table(ev, "macro_release_events", mode="overwrite", settings=s)}
    # model-based expectations: backfill for released events, live snapshot for pending ones
    xr: list[dict[str, Any]] = []
    for t in TRACKED:
        e = ev.filter(pl.col("series_id") == t.series_id)
        past = e.filter(pl.col("status") == "released").sort("release_date").tail(10).to_dicts()
        fut = e.filter(pl.col("status") == "pending").to_dicts()
        xr += ex.expectations_for_series(t, obs.get(t.series_id), past, True, now)
        xr += ex.expectations_for_series(t, obs.get(t.series_id), fut, False, now)
    xdf = ex.rows_frame(xr)
    counts["macro_expectations"] = write_table(xdf, "macro_expectations", mode="overwrite", settings=s)
    sur = su.surprise_rows(ev.filter(pl.col("status") == "released"), xdf.filter(pl.col("is_primary")), rel,
                           {t.series_id: t.direction for t in TRACKED}, now)
    counts["macro_surprises"] = write_table(sur, "macro_surprises", mode="overwrite", settings=s)
    idx = su.build_index(sur, today - timedelta(days=400), today, now)
    counts["macro_surprise_index"] = write_table(idx, "macro_surprise_index", mode="overwrite", settings=s)
    # nowcasts: ours (with news) + the two official benchmarks
    rng = np.random.default_rng(3)
    nc = []
    q = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    pq = (date(q.year - (q.month == 1), (q.month - 4) % 12 + 1, 1))
    for tq in (pq, q):
        lab = f"{tq.year}Q{(tq.month - 1) // 3 + 1}"
        prev = None
        for k in range(8):
            d = today - timedelta(days=7 * (7 - k))
            v = float(2.0 + rng.normal(0, 0.3))
            news = [{"series_id": "fred:PAYEMS", "title": "Payrolls", "contribution": round((v - prev) * 0.7, 3)},
                    {"series_id": "fred:INDPRO", "title": "IP", "contribution": round((v - prev) * 0.3, 3)}] if prev else None
            nc.append({"model_id": "dfm_mq_us_v1", "country": "US", "method": "dfm_mq", "target_period": tq,
                       "target_label": lab, "as_of": d, "nowcast": v, "lo": v - 2.0, "hi": v + 2.0, "units": "q/q % SAAR",
                       "prev_nowcast": prev, "news": json.dumps(news) if news else None, "source": "BiG Equity 360 model",
                       "model_based": True, "label": "Model-based nowcast (ours, DynamicFactorMQ)", "computed_at": now,
                       "run_id": None})
            prev = v
            for mid, lbl in (("gdpnow_atlanta_fed", "Atlanta Fed GDPNow (official, reference)"),
                             ("nyfed_staff_nowcast", "New York Fed Staff Nowcast (official, reference)")):
                nc.append({"model_id": mid, "country": "US", "method": "official_benchmark", "target_period": tq,
                           "target_label": lab, "as_of": d, "nowcast": v + 0.3, "lo": None, "hi": None,
                           "units": "q/q % SAAR", "prev_nowcast": None, "news": None, "source": "official",
                           "model_based": False, "label": lbl, "computed_at": now, "run_id": None})
    counts["macro_nowcasts"] = write_table(pl.DataFrame(nc, schema=NC_SCHEMA, orient="row"), "macro_nowcasts",
                                           mode="overwrite", settings=s)
    rr = []
    for k in range(24):
        m = (pd.Period(today, "M") - k).start_time.date()
        rr.append(rc._row("nyfed_yield_curve_official", "US", m, 0.2 + 0.01 * k, 0.5, False, 0.3, 12, True, "NY Fed",
                          {"target_month": "x"}, now))
        rr.append(rc._row("yield_curve_probit", "US", m, 0.15, 0.5, False, 0.3, 12, False, "ours", {"a": -1, "b": -0.4}, now))
        rr.append(rc._row("yield_curve_probit", "DE", m, 0.25 if k else 0.55, 0.1, k == 0, 0.3, 12, False, "ours", None, now))
        rr.append(rc._row("sahm_realtime", "US", m, None, 0.1, False, 0.5, None, True, "FRED", None, now))
        rr.append(rc._row("hmm_regime", "US", m, 0.05, None, False, 0.5, None, False, "macro_regimes", None, now))
        rr.append(rc._row("oecd_cli_turning_point", "US", m, None, 100.2, False, 100.0, None, True, "OECD",
                          {"phase": "expansion", "last_turning_point": None}, now))
    counts["macro_recession"] = write_table(pl.DataFrame(rr, schema=rc.SCHEMA, orient="row"), "macro_recession",
                                            mode="overwrite", settings=s)
    return counts
