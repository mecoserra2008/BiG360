"""Coverage v3 A8: composite scores are labelled descriptive; "signal" wording only behind a recorded out-of-sample gate
(app.analytics.composite_gate, job composite_validation)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.analytics import composite_gate as G


def _month_ends(n: int, year: int = 2018) -> list[date]:
    out = []
    for k in range(n):
        y, m = year + k // 12, k % 12 + 1
        out.append(date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return out


def _panel(n_dates: int, n_names: int, beta: float, seed: int = 7) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in _month_ends(n_dates):
        score = rng.uniform(0, 100, n_names)
        y = beta * (score - 50) / 50 + rng.normal(0, 1, n_names)
        rows += [{"date": d, "security_id": f"S{i:04d}", "score": float(score[i]), "fwd_excess_sector": float(y[i])}
                 for i in range(n_names)]
    return pl.DataFrame(rows)


def test_strong_signal_passes_and_noise_fails():
    good = G.evaluate_panel(_panel(36, 80, beta=1.0), "3M")
    assert good["verdict"] == "pass" and good["rank_ic_mean"] > 0.2 and good["rank_ic_t"] >= 2
    assert good["auc_mean"] > 0.6 and good["auc_t"] >= 2 and good["auc_p"] < 0.025 and good["n_dates"] == 36
    noise = G.evaluate_panel(_panel(36, 80, beta=0.0), "3M")
    assert noise["verdict"] == "fail" and abs(noise["rank_ic_mean"]) < 0.05 and noise["reasons"]


def test_too_few_dates_or_names_fails():
    r = G.evaluate_panel(_panel(12, 80, beta=1.0), "3M")
    assert r["verdict"] == "fail" and any("min_dates" in x for x in r["reasons"])
    r = G.evaluate_panel(_panel(36, 20, beta=1.0), "3M")   # < 30 names per date: no usable date at all
    assert r["verdict"] == "fail" and r["n_dates"] == 0


def test_auc_matches_brute_force():
    p = _panel(1, 60, beta=0.8, seed=3)
    m = G.per_date_metrics(p, 10)
    s, y = p["score"].to_numpy(), p["fwd_excess_sector"].to_numpy() > 0
    pos, neg = s[y], s[~y]
    brute = float(np.mean([(a > b) + 0.5 * (a == b) for a in pos for b in neg]))
    assert m["auc"][0] == pytest.approx(brute, abs=1e-12)
    ric = float(np.corrcoef(p["score"].rank().to_numpy(), p["fwd_excess_sector"].rank().to_numpy())[0, 1])
    assert m["rank_ic"][0] == pytest.approx(ric, abs=1e-12)


def test_panel_join_is_point_in_time():
    """A label is taken from the first label date L >= scoring date D (within 7 days); never from before D."""
    scores = pl.DataFrame({"as_of_date": [date(2024, 1, 31), date(2024, 2, 29)], "security_id": ["S1", "S1"],
                           "profile_id": ["balanced", "balanced"], "score": [60.0, 40.0]}).lazy()
    labels = pl.DataFrame({
        "date": [date(2024, 1, 26), date(2024, 2, 2), date(2024, 2, 23), date(2024, 3, 15)],
        "security_id": ["S1"] * 4, "horizon": ["3M"] * 4, "fwd_excess_sector": [9.0, 0.1, 8.0, 0.2]}).lazy()
    p = G.build_panel(scores, labels, ["balanced"], "3M")
    got = dict(zip(p["date"].to_list(), p["fwd_excess_sector"].to_list(), strict=True))
    assert got == {date(2024, 1, 31): 0.1}   # 02-02 for 01-31; nothing within 7 days after 02-29 -> dropped



TESTED = ["SAAPL", "SMSFT", "STMADR"]   # gate panel (US MVP, incl. a US-listed ADR of a JP issuer)


def _gate(verdicts: dict[str, str], ids=TESTED, scopes=("mvp",), **over) -> dict:
    """A trusted (canonical) gate document; `over` overrides top-level fields."""
    u = {"table": "composite_scores", "scopes": list(scopes), "regions": ["JP", "US"], "n_securities": len(ids),
         "security_ids": list(ids)}
    doc = {"gate_version": G.GATE_VERSION, "canonical": True, "max_securities": None, "start": None, "end": None,
           "evaluated_at": "2026-11-06T00:00:00+00:00",
           "profiles": {pid: {"verdict": v, "rank_ic_mean": 0.03, "rank_ic_t": 2.4, "universe": u,
                              "survivorship": {"survivorship_biased": False, "membership_history_start": "2017-01-02",
                                               "n_months_survivorship_free": 40, "decided_on": "survivorship_free"}}
                        for pid, v in verdicts.items()}}
    doc.update(over)
    return doc


def _write(doc, settings) -> None:
    G.write_gate(doc, G.gate_file(settings))
    G._CACHE.clear()


def test_validation_meta_and_labels(tmp_data):
    meta = G.validation_meta(["balanced", "growth"])
    assert meta["label"] == "descriptive" and meta["signal_wording"] is False and not meta["gate_recorded"]
    assert meta["profiles"]["balanced"]["verdict"] == "not_validated" and "AUC 0.49" in meta["note"]
    assert any(e["id"] == "SR20261012T0319222S5ZX" and e["value"] == pytest.approx(0.49) for e in meta["evidence"])
    assert meta["criteria"]["min_rank_ic_t"] == 2.0 and meta["criteria"]["horizon"] == "3M"
    assert G.composite_label("balanced", scope="mvp", security_id="SAAPL") == "descriptive"
    _write(_gate({"balanced": "pass", "growth": "fail"}), tmp_data)
    meta = G.validation_meta(["balanced", "growth", "momentum"])
    assert meta["label"] == "validated" and meta["signal_wording"] is True
    assert meta["universe"] == {"table": "composite_scores", "scopes": ["mvp"], "regions": ["JP", "US"],
                                "n_securities": 3}                    # public metadata: no security id list
    assert meta["profiles"]["growth"]["label"] == "descriptive" and meta["profiles"]["growth"]["verdict"] == "fail"
    assert meta["profiles"]["momentum"]["verdict"] == "not_validated" and meta["custom_weights_label"] == "descriptive"
    assert G.composite_label("balanced", scope="mvp", security_id="SAAPL") == "validated"
    assert G.composite_label("growth", scope="mvp", security_id="SAAPL") == "descriptive"
    G.gate_file(tmp_data).write_text("{not json", encoding="utf-8")   # unreadable gate -> descriptive again
    G._CACHE.clear()
    assert G.composite_label("balanced", scope="mvp", security_id="SAAPL") == "descriptive"


def test_pass_applies_only_inside_the_tested_population(tmp_data):
    """A US / MVP-scope pass never validates an extended-scope composite or a security outside the gate panel - e.g.
    the Japanese home listing of an issuer whose US ADR was tested (review A7_A8)."""
    _write(_gate({"balanced": "pass"}), tmp_data)
    lab = G.composite_label
    assert lab("balanced", scope="mvp", security_id="SAAPL") == "validated"
    assert lab("balanced", scope="mvp", security_id="STMADR") == "validated"     # tested ADR
    assert lab("balanced", scope="mvp", security_id="S7203T") == "descriptive"   # its JP home listing: untested
    assert lab("balanced", scope="extended", security_id="SAAPL") == "descriptive"  # composite_scores_ext row
    assert lab("balanced", scope=None, security_id="SAAPL") == "descriptive"
    assert lab("balanced") == "descriptive"                                       # nothing known: fail-closed
    assert G.label_for("balanced", "mvp", ["SAAPL", "SMSFT"]) == "validated"
    assert G.label_for("balanced", "mvp", ["SAAPL", "S7203T"]) == "descriptive"   # a mixed page
    assert G.label_for("balanced", "mvp", []) == "descriptive"
    _write(_gate({"balanced": "pass"}, ids=()), tmp_data)                         # panel ids not recorded
    assert lab("balanced", scope="mvp", security_id="SAAPL") == "descriptive"
    doc = _gate({"balanced": "pass"})
    del doc["profiles"]["balanced"]["universe"]                                   # gate without a universe
    _write(doc, tmp_data)
    assert lab("balanced", scope="mvp", security_id="SAAPL") == "descriptive"


@pytest.mark.parametrize("over", [{"canonical": False}, {"max_securities": 25}, {"start": "2024-01-01"},
                                  {"gate_version": 0}, {"canonical": None}])
def test_untrusted_gate_never_validates(tmp_data, over):
    """A sample / partial / older gate document is ignored by every reader (review A7_A8)."""
    _write(_gate({"balanced": "pass"}, **over), tmp_data)
    meta = G.validation_meta(["balanced"])
    assert meta["label"] == "descriptive" and meta["signal_wording"] is False and not meta["gate_recorded"]
    assert meta["profiles"]["balanced"]["verdict"] == "not_validated" and meta["evaluated_at"] is None
    assert G.composite_label("balanced", scope="mvp", security_id="SAAPL") == "descriptive"



def _with_daily(p: pl.DataFrame, months: int, seed: int = 11) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Monthly panel p -> (scores, labels) with extra DAILY scoring dates + labels on days 1..27 of the first
    `months` months (the nightly composite rows next to the month-end history)."""
    rng = np.random.default_rng(seed)
    names = p["security_id"].unique().sort().to_list()
    extra = []
    for d in sorted(p["date"].unique().to_list())[:months]:
        for day in range(1, 28):
            dd = d.replace(day=day)
            sc = rng.uniform(0, 100, len(names))
            extra += [{"date": dd, "security_id": s, "score": float(sc[i]),
                       "fwd_excess_sector": float((sc[i] - 50) / 50 + rng.normal(0, 1))} for i, s in enumerate(names)]
    full = pl.concat([p, pl.DataFrame(extra, schema=p.schema)])
    scores = full.select(pl.col("date").alias("as_of_date"), "security_id", pl.lit("balanced").alias("profile_id"),
                         "score")
    labels = full.select("date", "security_id", pl.lit("3M").alias("horizon"), "fwd_excess_sector")
    return scores, labels


def test_daily_scoring_dates_are_thinned_to_one_per_month():
    """Nightly composite rows next to the month-end history must not inflate n_dates or the Newey-West t."""
    base = _panel(30, 40, beta=0.3, seed=5)
    s0 = base.select(pl.col("date").alias("as_of_date"), "security_id", pl.lit("balanced").alias("profile_id"),
                     "score").lazy()
    l0 = base.select("date", "security_id", pl.lit("3M").alias("horizon"), "fwd_excess_sector").lazy()
    r0 = G.evaluate_panel(G.build_panel(s0, l0, ["balanced"], "3M"), "3M")
    s1, l1 = _with_daily(base, months=10)
    p1 = G.build_panel(s1.lazy(), l1.lazy(), ["balanced"], "3M")
    assert p1["date"].n_unique() == 30 and set(p1["date"].to_list()) == set(base["date"].to_list())
    r1 = G.evaluate_panel(p1, "3M")
    assert r1["n_dates"] == r1["n_months"] == r0["n_dates"] == 30
    assert r1["rank_ic_t"] == pytest.approx(r0["rank_ic_t"]) and r1["auc_t"] == pytest.approx(r0["auc_t"])
    # an un-thinned daily panel handed to evaluate_panel directly: min_dates counts months, HAC lags widen
    raw = pl.concat([base, pl.DataFrame([{"date": d.replace(day=k), "security_id": f"S{i:04d}", "score": 50.0 + i,
                                          "fwd_excess_sector": 0.0} for d in base["date"].unique().to_list()[:2]
                                         for k in range(1, 28) for i in range(40)], schema=base.schema)])
    r2 = G.evaluate_panel(raw.filter(pl.col("date") < base["date"].unique().sort()[5]), "3M")
    assert r2["n_dates"] > r2["n_months"] == 5 and any("5 scoring months" in x for x in r2["reasons"])


def test_hac_lags_follow_date_spacing():
    monthly = pl.Series(_month_ends(24))
    assert G.hac_lags(monthly, "3M") == 4 and G.hac_lags(monthly, "1M") == 2
    daily = pl.Series([date(2026, 1, 1) + timedelta(days=k) for k in range(60)])
    assert G.hac_lags(daily, "3M") >= 90 and G.hac_lags(None, "3M") == 4


def test_label_date_used_once():
    """Two scoring dates (last of January, only row of February) that share one label date: the later one is kept."""
    scores = pl.DataFrame({"as_of_date": [date(2024, 1, 29), date(2024, 2, 1)], "security_id": ["S1", "S1"],
                           "profile_id": ["balanced", "balanced"], "score": [60.0, 40.0]}).lazy()
    labels = pl.DataFrame({"date": [date(2024, 2, 2)], "security_id": ["S1"], "horizon": ["3M"],
                           "fwd_excess_sector": [0.5]}).lazy()
    p = G.build_panel(scores, labels, ["balanced"], "3M")
    assert p.select("date", "score").rows() == [(date(2024, 2, 1), 40.0)]


def test_job_writes_gate_and_dry_run(tmp_data, monkeypatch, tmp_path):
    from app.core import storage
    from app.scheduler.base import JobContext

    p = _panel(36, 80, beta=1.0)
    scores = p.select(pl.col("date").alias("as_of_date"), "security_id",
                      ("I" + pl.col("security_id")).alias("issuer_id"), pl.lit("balanced").alias("profile_id"), "score")
    labels = p.select("date", "security_id", pl.lit("3M").alias("horizon"), "fwd_excess_sector")
    labels = pl.concat([labels, labels.with_columns(pl.lit("1M").alias("horizon"))])
    issuers = scores.select("issuer_id", pl.lit("US").alias("region")).unique()
    # membership history long before the panel (2018-01 ->): every scoring month is survivorship-free
    um = _membership([(f"S{i:04d}", date(2017, 12, 1), None) for i in range(80)])
    tabs = {"composite_scores": scores, "ml_labels": labels, "issuers": issuers, "universe_membership": um}
    monkeypatch.setattr(storage, "scan_table", lambda name, settings=None: tabs[name].lazy())

    def ctx(**params):
        return JobContext(job_id="composite_validation", run_id="R1", as_of=date(2026, 11, 6), params=params,
                          settings=tmp_data)

    live = G.gate_file(tmp_data)
    r = G.run(ctx(profiles=["balanced"], dry_run=True))
    assert r.status == "success" and "dry run" in r.message and not live.exists()
    r = G.run(ctx(profiles=["balanced"], out_dir=str(tmp_path / "scratch")))
    doc = json.loads((tmp_path / "scratch" / "gate.json").read_text(encoding="utf-8"))
    assert r.status == "success" and not live.exists()
    b = doc["profiles"]["balanced"]
    assert b["verdict"] == "pass" and b["horizon"] == "3M" and set(b["horizons"]) == {"3M", "1M"} and b["n_dates"] == 36
    assert b["survivorship"]["survivorship_biased"] is False and b["survivorship"]["decided_on"] == "survivorship_free"
    assert b["survivorship"]["n_months_survivorship_free"] == 36 and doc["membership_history_start"] == "2017-12-01"
    u = b["universe"]
    assert {k: u[k] for k in ("table", "scopes", "regions", "n_securities")} == {
        "table": "composite_scores", "scopes": ["mvp"], "regions": ["US"], "n_securities": 80}
    assert u["security_ids"] == [f"S{i:04d}" for i in range(80)]
    # not canonical (a subset of the stored profiles / a sample cap / a window): never the live gate
    r = G.run(ctx(profiles=["balanced"]))
    assert not live.exists() and (live.parent / "runs" / "gate_R1.json").exists() and "live gate unchanged" in r.message
    monkeypatch.setattr(G, "_stored_profiles", lambda s: ["balanced"])
    for params in ({"max_securities": 25}, {"start": "2019-01-01"}, {"horizons": ["1M", "3M"]},
                   {"max_securities": 25, "out_dir": str(live.parent)}):   # out_dir = the live directory: redirected
        G.run(ctx(**params))
        assert not live.exists()
    # canonical: the live gate, trusted, validated only inside its universe
    r = G.run(ctx())
    G._CACHE.clear()
    doc = json.loads(live.read_text(encoding="utf-8"))
    assert doc["canonical"] is True and G.trusted(doc) and doc["profiles"]["balanced"]["verdict"] == "pass"
    assert G.composite_label("balanced", scope="mvp", security_id="S0007") == "validated"
    assert G.composite_label("balanced", scope="mvp", security_id="S9999") == "descriptive"     # not in the panel
    assert G.composite_label("balanced", scope="extended", security_id="S0007") == "descriptive"
    assert "S0007" not in json.dumps(G.validation_meta(["balanced"]))                         # ids stay in the file


# ------------------------------------------------------------------------------------------------ survivorship
# (review A7_A8: composite_scores / ml_labels hold today's MVP universe in every year; universe_membership history
# only starts 2026-10-09/10, so a pass on the full history would rest on a survivor-only panel)


def _membership(rows: list[tuple], universe: str = "mvp_us") -> pl.DataFrame:
    return pl.DataFrame({"universe_id": [universe] * len(rows), "security_id": [r[0] for r in rows],
                         "valid_from": [r[1] for r in rows], "valid_to": [r[2] for r in rows]},
                        schema={"universe_id": pl.String, "security_id": pl.String, "valid_from": pl.Date,
                                "valid_to": pl.Date})


@pytest.mark.parametrize(("history_start", "verdict", "biased", "n_free"), [
    (date(2026, 10, 9), "not_validated", True, 0),     # the live lake: every month before the membership history
    (date(2019, 7, 1), "not_validated", True, 18),     # 18 survivorship-free months < min_dates 24
    (date(2018, 1, 1), "pass", False, 36),             # the whole panel is survivorship-free
])
def test_survivor_only_months_cannot_decide_a_pass(history_start, verdict, biased, n_free):
    """A strong signal on a panel whose months precede the membership history is no evidence: never 'pass'."""
    r = G.evaluate_pit(_panel(36, 80, beta=1.0), "3M", None, history_start, ["mvp_us"])
    sv = r["survivorship"]
    assert r["verdict"] == verdict and sv["survivorship_biased"] is biased and sv["n_months_survivorship_free"] == n_free
    assert sv["membership_history_start"] == history_start.isoformat() and sv["universes"] == ["mvp_us"]
    if verdict == "pass":
        assert sv["decided_on"] == "survivorship_free" and r["n_months"] == 36 and r["full_history"]["n_months"] == 36
    else:
        assert sv["decided_on"] == "full_history" and r["rank_ic_t"] >= 2           # the metrics alone would pass
        assert any(x.startswith("survivorship:") for x in r["reasons"])


def test_survivorship_free_months_decide_on_their_own_metrics():
    """Only the months on or after the membership start count: 30 noise months there fail, whatever the earlier
    (survivor-only) months show; membership unknown never passes; a fail stays a fail."""
    strong, noise = _panel(36, 80, beta=1.5, seed=1), _panel(66, 80, beta=0.0, seed=2)
    cut = sorted(noise["date"].unique().to_list())[36]
    mixed = pl.concat([strong, noise.filter(pl.col("date") >= cut)])
    r = G.evaluate_pit(mixed, "3M", None, cut)
    assert r["survivorship"]["n_months_survivorship_free"] == 30 and r["verdict"] == "fail"
    assert r["survivorship"]["decided_on"] == "survivorship_free" and r["full_history"]["rank_ic_mean"] > 0.1
    r = G.evaluate_pit(_panel(36, 80, beta=1.0), "3M", None, None)
    assert r["verdict"] == "not_validated" and r["survivorship"]["survivorship_biased"] is True
    assert any("membership history unavailable" in x for x in r["reasons"])
    r = G.evaluate_pit(_panel(36, 80, beta=0.0), "3M", None, date(2026, 10, 9))
    assert r["verdict"] == "fail"


def test_membership_mask_is_point_in_time():
    """Initial snapshot (within 30 days of the first row) = members since inception; a later addition counts from
    its valid_from; an exit stops at valid_to; a security never in the universe is dropped."""
    um = pl.concat([_membership([("SA", date(2026, 10, 10), None), ("SB", date(2026, 10, 23), date(2026, 10, 30))]),
                    _membership([("SC", date(2026, 10, 9), None), ("SD", date(2027, 3, 1), None)], "mvp_global"),
                    _membership([("SX", date(2020, 1, 1), None)], "full")])
    members, start = G.membership_intervals(um)
    assert start == date(2026, 10, 10)                       # the latest first valid_from of mvp_us / mvp_global
    vf = dict(zip(members["security_id"].to_list(), members["valid_from"].to_list(), strict=True))
    assert vf["SA"] == vf["SB"] == vf["SC"] == date(1900, 1, 1) and vf["SD"] == date(2027, 3, 1) and "SX" not in vf
    days = [date(2020, 6, 30), date(2026, 10, 31), date(2027, 3, 31)]
    panel = pl.DataFrame([{"date": d, "security_id": s, "score": 1.0, "fwd_excess_sector": 0.0}
                          for d in days for s in ("SA", "SB", "SC", "SD", "SX")])
    kept = set(G.apply_membership(panel, members).select("date", "security_id").iter_rows())
    assert kept == {(days[0], "SA"), (days[0], "SB"), (days[0], "SC"),
                    (days[1], "SA"), (days[1], "SC"),
                    (days[2], "SA"), (days[2], "SC"), (days[2], "SD")}
    assert G.membership_intervals(_membership([("SX", date(2020, 1, 1), None)], "full")) == (None, None)
    assert G.apply_membership(panel, None).height == panel.height


def test_recorded_pass_needs_a_survivorship_free_record(tmp_data):
    """verdict() refuses a recorded 'pass' that is biased or has no survivorship record (older documents)."""
    doc = _gate({"balanced": "pass"})
    assert G.verdict(doc, "balanced") == "pass"
    doc["profiles"]["balanced"]["survivorship"]["survivorship_biased"] = True
    assert G.verdict(doc, "balanced") == "not_validated"
    del doc["profiles"]["balanced"]["survivorship"]
    assert G.verdict(doc, "balanced") == "not_validated"
    _write(doc, tmp_data)
    assert G.composite_label("balanced", scope="mvp", security_id="SAAPL") == "descriptive"
    assert G.validation_meta(["balanced"])["signal_wording"] is False


def test_job_on_a_survivor_only_lake_records_no_pass(tmp_data, monkeypatch):
    """The live situation: the stored history predates the membership history -> the canonical gate is recorded,
    but no profile is validated and the API note says the panel is survivorship-biased; no membership -> same."""
    from app.core import storage
    from app.scheduler.base import JobContext

    p = _panel(36, 80, beta=1.0)
    scores = p.select(pl.col("date").alias("as_of_date"), "security_id", pl.lit("balanced").alias("profile_id"),
                      "score")
    labels = p.select("date", "security_id", pl.lit("3M").alias("horizon"), "fwd_excess_sector")
    labels = pl.concat([labels, labels.with_columns(pl.lit("1M").alias("horizon"))])
    um = _membership([(f"S{i:04d}", date(2026, 10, 10), None) for i in range(80)])
    tabs = {"composite_scores": scores, "ml_labels": labels,
            "issuers": pl.DataFrame(schema={"issuer_id": pl.String, "region": pl.String}), "universe_membership": um}
    monkeypatch.setattr(storage, "scan_table", lambda name, settings=None: tabs[name].lazy())
    monkeypatch.setattr(G, "_stored_profiles", lambda s: ["balanced"])
    ctx = JobContext(job_id="composite_validation", run_id="R2", as_of=date(2026, 11, 6), params={},
                     settings=tmp_data)
    r = G.run(ctx)
    G._CACHE.clear()
    doc = json.loads(G.gate_file(tmp_data).read_text(encoding="utf-8"))
    b = doc["profiles"]["balanced"]
    assert r.status == "success" and doc["canonical"] is True and b["verdict"] == "not_validated"
    assert b["survivorship"]["survivorship_biased"] is True and b["survivorship"]["n_months_survivorship_free"] == 0
    assert b["survivorship"]["rows_dropped_by_membership"] == 0     # initial snapshot: members since inception
    meta = G.validation_meta(["balanced"])
    assert meta["label"] == "descriptive" and meta["signal_wording"] is False
    assert meta["profiles"]["balanced"]["survivorship"]["survivorship_biased"] is True
    assert "survivorship-biased" in meta["note"] and "starts 2026-10-10" in meta["note"]
    assert G.composite_label("balanced", scope="mvp", security_id="S0007") == "descriptive"
    del tabs["universe_membership"]                               # membership unavailable: fail-closed
    G.run(ctx)
    G._CACHE.clear()
    b = json.loads(G.gate_file(tmp_data).read_text(encoding="utf-8"))["profiles"]["balanced"]
    assert b["verdict"] == "not_validated" and b["survivorship"]["membership_history_start"] is None
