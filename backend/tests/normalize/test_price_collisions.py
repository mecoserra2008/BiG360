"""A6 ticker-collision fix (coverage v3): a listing on a venue no price source serves must never take a US stock's bars
through its bare ticker; the repair backs up + removes the contaminated rows, marks the listing mapping_gap and makes
the next incremental prices_finalize re-build the US twin from its full history. Fixture data only (tmp lake)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.core.storage import read_table, scan_table, write_table
from app.normalize import price_collisions as PC
from app.normalize import prices as P
from app.normalize import prices_incremental as INC

NOW = datetime.now(UTC)
TODAY = NOW.date()
T0 = NOW - timedelta(days=30)


def _listing(lid, sid, mic, cc, sym, yf, poly, ccy, tz="UTC"):
    return {"listing_id": lid, "security_id": sid, "mic": mic, "exchange_country": cc, "local_symbol": sym,
            "yf_ticker": yf, "polygon_ticker": poly, "quote_currency": ccy, "price_divisor": 1.0, "is_primary": True,
            "lot_size": None, "timezone": tz, "valid_from": date(2000, 1, 1), "valid_to": None, "updated_at": NOW}


def _security(sid, iss, tk, name, ccy, lid):
    return {"security_id": sid, "issuer_id": iss, "security_type": "common", "share_class": None, "isin": None,
            "figi": None, "cusip": None, "primary_listing_id": lid, "display_ticker": tk, "name": name,
            "trading_currency": ccy, "underlying_security_id": None, "adr_ratio": None, "shares_class_weight": None,
            "is_primary_security": True, "is_active": True, "first_trade_date": None, "last_trade_date": None,
            "delisting_reason": None, "updated_at": NOW, "run_id": None}


LISTINGS = [
    _listing("LUS", "SUS", "XNAS", "US", "HON", "HON", "HON", "USD", "America/New_York"),
    _listing("LBRA", "SBRA", "XBRA", "SK", "HON", None, None, "EUR", "Europe/Bratislava"),   # no venue symbol
    _listing("LSAP", "SSAP", "XETR", "DE", "SAP", "SAP.DE", None, "EUR", "Europe/Berlin"),
]
SECURITIES = [_security("SUS", "IUS", "HON", "Honeywell", "USD", "LUS"),
              _security("SBRA", "IBRA", "HON", "Some Bratislava Co", "EUR", "LBRA"),
              _security("SSAP", "ISAP", "SAP.DE", "SAP", "EUR", "LSAP")]


def _bars(source, sym, days, base, seed, ccy):
    rows, px = [], base
    for i, d in enumerate(days):
        px *= 1.0 + 0.01 * math.sin(0.7 * i + seed)
        rows.append({"source_id": source, "year": d.year, "symbol": sym, "date": d, "open": px, "high": px * 1.01,
                     "low": px * 0.99, "close": px, "adj_close": None, "volume": 1e6 + i, "vwap": None, "trades": None,
                     "currency": ccy, "price_basis": "raw", "is_benchmark": False, "ingested_at": T0})
    return pl.DataFrame(rows)


@pytest.fixture()
def ref(tmp_data):
    s = tmp_data
    write_table(pl.DataFrame(LISTINGS), "listings", settings=s)
    write_table(pl.DataFrame(SECURITIES), "securities_master", settings=s)
    return s


# ------------------------------------------------------------------------------------------------ resolution fix


def test_listing_key_only_from_own_venue():
    df = pl.DataFrame(LISTINGS)
    keys = df.select(P.listing_key_expr().alias("k")).get_column("k").to_list()
    assert keys == ["HON", None, "SAP.DE"]
    us_bare = pl.DataFrame([_listing("L1", "S1", "XNYS", "US", "BRK.B", None, "BRK.B", "USD")])
    assert us_bare.select(P.listing_key_expr()).item() == "BRK.B"     # a bare US symbol is the source symbol


def test_universe_keeps_us_twin_and_collider_resolves_nothing(ref):
    s = ref
    uni = P.load_universe(s, include_benchmarks=False)
    assert set(uni.get_column("security_id").to_list()) == {"SUS", "SBRA", "SSAP"}
    assert uni.filter(pl.col("security_id") == "SBRA").get_column("key").to_list() == [None]
    st = pl.DataFrame({"source_id": ["polygon", "yfinance", "polygon"], "symbol": ["HON", "HON", "VIS"]})
    r = P.resolve_symbols(st, uni)
    assert r.get_column("security_id").to_list() == ["SUS", "SUS"]   # VIS (outside the universe) resolves nowhere


def test_polygon_fallback_reaches_us_rows_only():
    uni = pl.DataFrame([
        {"key": "VIS", "yf_ticker": "VIS", "polygon_ticker": None, "security_id": "SHR", "listing_id": "LHR",
         "region": "EU", "country": "HR", "mic": "XZAG", "quote_currency": "EUR", "currency": "EUR",
         "price_divisor": 1.0, "calendar": None, "is_benchmark": False, "provisional": False, "name": "x",
         "px_from": None, "px_to": None},
        {"key": "BRK-B", "yf_ticker": "BRK-B", "polygon_ticker": None, "security_id": "SBRK", "listing_id": "LBRK",
         "region": "US", "country": "US", "mic": "XNYS", "quote_currency": "USD", "currency": "USD",
         "price_divisor": 1.0, "calendar": "XNYS", "is_benchmark": False, "provisional": False, "name": "y",
         "px_from": None, "px_to": None}], schema=P._RES_SCHEMA)
    st = pl.DataFrame({"source_id": ["polygon", "polygon", "yfinance"], "symbol": ["VIS", "BRK.B", "VIS"]})
    r = P.resolve_symbols(st, uni).sort("source_id")
    assert r.select("source_id", "symbol", "security_id").rows() == [("polygon", "BRK.B", "SBRK"),
                                                                     ("yfinance", "VIS", "SHR")]


def test_null_keys_do_not_collapse_and_duplicates_are_deterministic(ref):
    s = ref
    extra = [_listing("LMT", "SMT", "XMAL", "MT", "CVS", None, None, "EUR"),
             _listing("LCY", "SCY", "XCYS", "CY", "ROL", None, None, "EUR")]
    write_table(pl.DataFrame(LISTINGS + extra), "listings", settings=s, mode="overwrite")
    write_table(pl.DataFrame(SECURITIES + [_security("SMT", "IMT", "CVS", "m", "EUR", "LMT"),
                                           _security("SCY", "ICY", "ROL", "c", "EUR", "LCY")]),
                "securities_master", settings=s, mode="overwrite")
    a = P.load_universe(s, include_benchmarks=False)
    b = P.load_universe(s, include_benchmarks=False)
    assert a.equals(b)
    assert a.filter(pl.col("key").is_null()).height == 3              # SBRA, SMT, SCY all kept


# ------------------------------------------------------------------------------------------------ evidence (pure)


def test_identity_evidence_matches_split_adjusted_yahoo_and_rejects_genuine_series():
    d = [date(2026, 1, 5) + timedelta(days=i) for i in range(10)]
    stored = pl.DataFrame({"security_id": ["A"] * 10 + ["B"] * 10, "source_id": ["yfinance"] * 20, "date": d + d,
                           # A: unadjusted Yahoo bars = staged / split factor 0.5 for the first half
                           "close": [20.0] * 5 + [10.0] * 5 + [55.0 + i for i in range(10)],
                           "split_factor": [0.5] * 5 + [1.0] * 5 + [1.0] * 10})
    staged = pl.DataFrame({"source_id": ["yfinance"] * 20, "symbol": ["X"] * 10 + ["Y"] * 10, "date": d + d,
                           "close": [10.0] * 10 + [40.0 + i for i in range(10)]})
    pairs = pl.DataFrame({"source_id": ["yfinance", "yfinance"], "symbol": ["X", "Y"], "security_id": ["A", "B"]})
    ev = PC.identity_evidence(stored, staged, pairs)
    a, b = ev.filter(pl.col("security_id") == "A").row(0, named=True), ev.filter(pl.col("security_id") == "B").row(0, named=True)
    assert a["contaminated"] and a["n_match"] == 10 and a["symbol"] == "X"
    assert not b["contaminated"] and b["n_match"] == 0


def test_removed_identity_evidence_uses_fx_and_rejects_genuine_series():
    d = [date(2026, 10, 26) + timedelta(days=i) for i in range(10)]          # incl. a weekend: FX as of the last fixing
    sc = [80.0 + i for i in range(10)]
    fx_days = [x for x in d if x.weekday() < 5]
    fx = pl.DataFrame({"currency": ["EUR"] * len(fx_days), "date": fx_days,
                       "usd_per_unit": [1.12 + 0.001 * i for i in range(len(fx_days))]})
    rate = {r[0]: r[1] for r in fx.select("date", "usd_per_unit").iter_rows()}
    last = None
    g_close = []
    for x, c in zip(d, sc, strict=True):
        last = rate.get(x, last)
        g_close.append(c * last)                         # the gap line's EUR close = the US close: close_usd = close x FX
    val = pl.DataFrame({"security_id": ["G"] * 10 + ["H"] * 10, "date": d + d,
                        "close_usd": g_close + [20.0 + 0.5 * i for i in range(10)], "currency": ["EUR"] * 20})
    staged = pl.DataFrame({"source_id": ["polygon"] * 10 + ["yfinance"] * 10, "symbol": ["CVS"] * 10 + ["ROL"] * 10,
                           "date": d + d, "close": sc + [60.0 - i for i in range(10)]})
    pairs = pl.DataFrame({"source_id": ["polygon", "yfinance"], "symbol": ["CVS", "ROL"], "security_id": ["G", "H"]})
    ev = PC.removed_identity_evidence(val, staged, pairs, fx)
    g, h = (ev.filter(pl.col("security_id") == x).row(0, named=True) for x in ("G", "H"))
    assert g["contaminated"] and g["n_match"] == 10 and g["symbol"] == "CVS" and g["source_id"] == "polygon"
    assert not h["contaminated"] and h["n_overlap"] == 10 and h["n_match"] == 0
    # without an FX rate there is no evidence (never a guess)
    assert PC.removed_identity_evidence(val, staged, pairs, fx.head(0)).get_column("n_overlap").sum() == 0


def test_identity_pairs_dq_rule():
    d = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
    px = [10.0 + 0.37 * i for i in range(20)]
    w = pl.DataFrame({"security_id": ["A"] * 20 + ["B"] * 20 + ["C"] * 20 + ["D"] * 20,
                      "date": d * 4,
                      "close": px + px + [5.0] * 20 + [5.0] * 20})       # A == B (collision); C == D constant (stale)
    pr = PC.identity_pairs(w)
    assert pr.select("a", "b").rows() == [("A", "B")]
    assert PC.identity_pairs(w.filter(pl.col("security_id") != "B")).is_empty()


# ------------------------------------------------------------------------------------------------ repair end to end


def test_plan_apply_and_forced_rebuild_end_to_end(ref):
    s = ref
    days = sorted(P.trading_sessions("XNYS", TODAY - timedelta(days=120), TODAY - timedelta(days=2)))
    xdays = sorted(P.trading_sessions("XETR", TODAY - timedelta(days=120), TODAY - timedelta(days=2)))
    write_table(pl.concat([_bars("polygon", "HON", days, 200.0, 1, "USD"), _bars("yfinance", "SAP.DE", xdays, 180.0, 2, "EUR")]),
                "prices_staging", settings=s)
    P.finalize(s, mode="full")
    good = read_table("prices_daily", settings=s)
    n_us, n_sap = good.filter(pl.col("security_id") == "SUS").height, good.filter(pl.col("security_id") == "SSAP").height
    assert n_us > 50 and n_sap > 50
    # the pre-fix state: the Bratislava line carries Honeywell's bars and the US security lost its own
    bad = good.filter(pl.col("security_id") == "SUS").with_columns(
        pl.lit("SBRA").alias("security_id"), pl.lit("LBRA").alias("listing_id"), pl.lit("EU").alias("region"),
        pl.lit("EUR").alias("currency"))
    write_table(bad, "prices_daily", settings=s)
    PC.delete_rows(good.filter(pl.col("security_id") == "SUS"), s)
    assert read_table("prices_daily", settings=s).filter(pl.col("security_id") == "SUS").height == 0
    st = INC.load_state(s)
    for r in st["regions"].values():
        r["watermark"] = (NOW + timedelta(minutes=30)).isoformat()      # nothing new to ingest: only the request acts
    INC.save_state(s, st)

    p = PC.plan(s)
    summ = p.summary()
    assert summ["contaminated_securities"] == 1 and summ["rows_to_remove"] == n_us and summ["leftover_rows"] == 0
    assert p.twins.rows() == [("SBRA", "SUS")]
    res = PC.apply_plan(p, s, wait_min=0)
    assert res["status"] == "applied" and res["rows_removed"] == n_us, res
    back = pl.read_parquet(res["backup"])
    assert back.height == n_us and set(back.get_column("security_id").unique().to_list()) == {"SBRA"}
    now_px = read_table("prices_daily", settings=s)
    assert now_px.filter(pl.col("security_id") == "SBRA").height == 0
    assert now_px.filter(pl.col("security_id") == "SSAP").height == n_sap            # same partitions, untouched
    gaps = PC.read_mapping_gaps(s)
    assert gaps.select("listing_id", "reason", "refetch").rows() == [("LBRA", "mapping_gap", "1")]
    assert "HON" in gaps.item(0, "detail")
    assert INC.load_state(s)[INC.FORCE_FULL_KEY] == ["SUS"]

    rep = P.finalize(s)                                                    # incremental: forced full re-build
    assert rep["mode"] == "incremental" and rep.get("forced_full") == 1
    after = read_table("prices_daily", settings=s)
    assert after.filter(pl.col("security_id") == "SUS").height == n_us
    assert after.filter(pl.col("security_id") == "SBRA").height == 0
    assert INC.FORCE_FULL_KEY not in INC.load_state(s)
    # idempotent: a second plan finds nothing
    assert PC.plan(s).rows.is_empty()


def test_removed_series_are_marked_and_derived_rows_purged(ref):
    from app.core.dq import DqContext

    s = ref
    days = sorted(P.trading_sessions("XNYS", TODAY - timedelta(days=30), TODAY - timedelta(days=2)))
    write_table(_bars("polygon", "HON", days, 200.0, 1, "USD"), "prices_staging", settings=s)
    P.finalize(s, mode="full")
    us = read_table("prices_daily", settings=s).filter(pl.col("security_id") == "SUS").sort("date")
    fxd = [TODAY - timedelta(days=40) + timedelta(days=i) for i in range(41)]
    write_table(pl.DataFrame({"date": fxd, "year": [x.year for x in fxd], "currency": "EUR", "usd_per_unit": 1.12,
                              "units_per_usd": 1 / 1.12, "source_id": "ecb", "ingested_at": NOW}), "fx_rates", settings=s)
    # the Bratislava line lost its (Honeywell) bars before the repair; its valuation rows still carry them as EUR
    bra = us.select(pl.lit("SBRA").alias("security_id"), pl.lit("IBRA").alias("issuer_id"), "date",
                    pl.col("date").dt.year().cast(pl.Int32).alias("year"), (pl.col("close") * 1.12).alias("close_usd"))
    sap = us.select(pl.lit("SSAP").alias("security_id"), pl.lit("ISAP").alias("issuer_id"), "date",
                    pl.col("date").dt.year().cast(pl.Int32).alias("year"), (pl.col("close") * 0.37).alias("close_usd"))
    write_table(pl.concat([bra, sap]).with_columns(pl.lit(None, pl.Float64).alias("shares_out"),
                                                   pl.lit(None, pl.Float64).alias("market_cap_usd"),
                                                   pl.lit(NOW).alias("computed_at")), "valuation_daily", settings=s)
    n_val = bra.height

    p = PC.plan(s)
    assert p.rows.is_empty() and p.summary()["removed_earlier_securities"] == 1
    assert p.removed_contaminated().get_column("security_id").to_list() == ["SBRA"]
    ctx = DqContext("RTEST", s, tables=["prices_daily", "valuation_daily"])
    dq = {o.check_id: o for o in PC.venue_gap_outcomes(ctx)}
    assert dq["bars_without_venue_symbol"].passed
    assert not dq["derived_rows_without_venue_symbol"].passed and dq["derived_rows_without_venue_symbol"].metric_value == 1

    res = PC.apply_plan(p, s, wait_min=0)
    assert res["status"] == "marked" and res["listings_marked"] == 1, res
    gaps = PC.read_mapping_gaps(s)
    assert gaps.select("listing_id", "reason").rows() == [("LBRA", "mapping_gap")]
    assert "no row backup" in gaps.item(0, "detail") and "HON" in gaps.item(0, "detail")
    assert INC.load_state(s)[INC.FORCE_FULL_KEY] == ["SUS"]
    assert PC.apply_plan(PC.plan(s), s, wait_min=0)["listings_marked"] == 0          # an existing mark is kept

    dry = PC.purge_derived(None, s)
    assert dry["status"] == "dry_run" and dry["tables"]["valuation_daily"]["rows"] == n_val
    assert scan_table("valuation_daily", s).filter(pl.col("security_id") == "SBRA").collect().height == n_val
    done = PC.purge_derived(None, s, apply=True, wait_min=0)
    assert done["status"] == "applied" and done["removed"] == {"valuation_daily": n_val}, done
    back = pl.read_parquet(f"{done['backup_dir']}/valuation_daily.parquet")
    assert back.height == n_val and set(back.get_column("security_id").to_list()) == {"SBRA"}
    left = read_table("valuation_daily", settings=s)
    assert left.filter(pl.col("security_id") == "SBRA").is_empty()
    assert left.filter(pl.col("security_id") == "SSAP").height == sap.height          # same partitions, untouched
    ctx2 = DqContext("RTEST2", s, tables=["prices_daily", "valuation_daily"])
    assert {o.check_id: o for o in PC.venue_gap_outcomes(ctx2)}["derived_rows_without_venue_symbol"].passed
    # restorable from the backup
    write_table(back, "valuation_daily", settings=s)
    assert read_table("valuation_daily", settings=s).filter(pl.col("security_id") == "SBRA").height == n_val


def test_apply_refuses_while_a_price_job_runs(ref, monkeypatch):
    s = ref
    monkeypatch.setattr(PC, "running_price_jobs", lambda settings=None: ["prices_finalize"])
    p = PC.CollisionPlan(pl.DataFrame(), pl.DataFrame({"security_id": [], "source_id": [], "contaminated": []}),
                         pl.DataFrame({"security_id": ["X"]}), pl.DataFrame(), pl.DataFrame(), pl.DataFrame())
    assert PC.apply_plan(p, s, wait_min=0)["status"] == "busy"
    assert not (s.data_dir / "backups").exists() or not list((s.data_dir / "backups").glob("prices_collision_*"))


# ------------------------------------------------------------------------------------------------ DQ hooks (tmp lake)


def test_dq_hooks_flag_collisions_and_implausible_mcaps(ref):
    from app.core.dq import default_checks, run_checks

    s = ref
    days = sorted(P.trading_sessions("XNYS", TODAY - timedelta(days=40), TODAY - timedelta(days=1)))
    write_table(_bars("polygon", "HON", days, 200.0, 1, "USD"), "prices_staging", settings=s)
    P.finalize(s, mode="full")
    us = read_table("prices_daily", settings=s).filter(pl.col("security_id") == "SUS")
    write_table(us.with_columns(pl.lit("SBRA").alias("security_id"), pl.lit("LBRA").alias("listing_id"),
                                pl.lit("EU").alias("region")), "prices_daily", settings=s)
    d = days[-1]
    write_table(pl.DataFrame({"security_id": ["SUS", "SSAP"], "issuer_id": ["IUS", "ISAP"], "date": [d, d],
                              "year": [d.year, d.year], "shares_out": [6.5e8, 3.0e12],
                              "market_cap_usd": [6.5e8 * 200, 2.0e14 / 1.0e2], "close_usd": [200.0, 180.0],
                              "computed_at": [NOW, NOW]}), "valuation_daily", settings=s)
    checks = [c for c in default_checks(s, ["prices_daily", "valuation_daily"])
              if c.check_id in ("price_identity", "mcap_plausibility")]
    assert {c.check_id for c in checks} == {"price_identity", "mcap_plausibility"}
    out = {o.check_id: o for o in run_checks(checks, "RTEST", s, write=False)}
    assert not out["price_identity_collision"].passed and out["price_identity_collision"].metric_value == 2.0
    assert not out["bars_without_venue_symbol"].passed and out["bars_without_venue_symbol"].metric_value == 1.0
    assert out["bars_without_venue_symbol"].details["examples"] == ["HON@XBRA"]
    ps = out["mcap_price_shares_bounds"]
    assert not ps.passed and ps.details["inconsistent"] == 1 and ps.details["too_many_shares"] == 1   # SSAP only
    ex = out["mcap_vs_exchange_total"]                       # USD 2 tn = 77 % of the German market
    assert not ex.passed and [n["ticker"] for n in ex.details["names_over_max_share"]] == ["SAP.DE"]


# ------------------------------------------------------------------------------------------------ A6 review round 2


def _mark_gap(s, listing_id="LBRA", sid="SBRA", mic="XBRA", sym="HON"):
    p = PC.mapping_gaps_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(PC._GAP_HEADER + ",".join(PC._GAP_COLS) + "\n"
                 + f"{sid},{listing_id},{mic},{sym},mapping_gap,1,10,x,2026-11-06T00:00:00+00:00\n", encoding="utf-8")


def _staged_lake(s):
    days = sorted(P.trading_sessions("XNYS", TODAY - timedelta(days=90), TODAY - timedelta(days=2)))
    xdays = sorted(P.trading_sessions("XETR", TODAY - timedelta(days=90), TODAY - timedelta(days=2)))
    write_table(pl.concat([_bars("polygon", "HON", days, 200.0, 1, "USD"), _bars("yfinance", "SAP.DE", xdays, 180.0, 2, "EUR")]),
                "prices_staging", settings=s)
    P.finalize(s, mode="full")
    st = INC.load_state(s)
    for r in st["regions"].values():
        r["watermark"] = (NOW + timedelta(minutes=30)).isoformat()      # nothing new to ingest: only requests act
    INC.save_state(s, st)
    return read_table("prices_daily", settings=s)


def test_forced_selection_caps_in_request_order():
    uni = pl.DataFrame({"security_id": ["A", "B", "C", "D"], "region": ["US", "EU", "US", "US"]})
    assert INC.forced_selection(["C", "X", "B", "A", "D"], uni, 2) == {"US": {"C"}, "EU": {"B"}}
    assert INC.forced_selection(["C", "C", "A"], uni, 5) == {"US": {"C", "A"}}
    assert INC.forced_selection([], uni) == {}
    many = [f"S{i}" for i in range(40)]
    sel = INC.forced_selection(many, pl.DataFrame({"security_id": many, "region": ["US"] * 40}))
    assert sel["US"] == set(many[:INC.FORCE_FULL_MAX_PER_RUN])


def test_forced_rebuilds_are_capped_and_cleared_per_region(ref, monkeypatch):
    s = ref
    good = _staged_lake(s)
    n_us, n_sap = (good.filter(pl.col("security_id") == x).height for x in ("SUS", "SSAP"))
    PC.delete_rows(good.filter(pl.col("security_id").is_in(["SUS", "SSAP"])), s)     # both lost their bars
    assert PC.request_full_rebuild(["SSAP", "SUS"], s) == 2
    assert INC.load_state(s)[INC.FORCE_FULL_KEY] == ["SSAP", "SUS"]                    # request order kept
    rep = INC.finalize_incremental(s, force_full_max=1)
    assert rep["forced_full"] == 1 and rep["forced_full_queued"] == 2
    assert INC.load_state(s)[INC.FORCE_FULL_KEY] == ["SUS"]                            # the oldest one went first
    assert read_table("prices_daily", settings=s).filter(pl.col("security_id") == "SSAP").height == n_sap
    # a failing region no longer re-queues the regions that were written
    PC.request_full_rebuild(["SSAP"], s)
    orig = INC._finish_phase

    def boom(s_, w, *a, **k):
        if w.region == "EU":
            raise RuntimeError("EU down")
        return orig(s_, w, *a, **k)

    monkeypatch.setattr(INC, "_finish_phase", boom)
    rep = INC.finalize_incremental(s, force_full_max=5)
    assert rep["errors"] == ["EU"] and rep["forced_full"] == 2
    assert INC.load_state(s)[INC.FORCE_FULL_KEY] == ["SSAP"]                           # US cleared, EU kept
    assert read_table("prices_daily", settings=s).filter(pl.col("security_id") == "SUS").height == n_us


def test_restore_twins_rebuilds_the_twin_and_dq_postcondition(ref):
    from app.core.dq import DqContext

    s = ref
    good = _staged_lake(s)
    n_us = good.filter(pl.col("security_id") == "SUS").height
    wm = {r: v["watermark"] for r, v in INC.load_state(s)["regions"].items()}
    _mark_gap(s)
    PC.delete_rows(good.filter(pl.col("security_id") == "SUS"), s)                    # the twin lost its bars
    PC.request_full_rebuild(["SUS"], s)
    assert PC.marked_twins(s).rows() == [("SBRA", "SUS", "HON")]
    ctx = DqContext("RT1", s, tables=["prices_daily"])
    o = {x.check_id: x for x in PC.twin_outcomes(ctx)}["twins_without_bars"]
    assert not o.passed and o.metric_value == 1.0 and o.details["examples"] == ["HON"]
    assert not PC.verify(s)["ok"]

    dry = PC.restore_twins(s)
    assert dry["status"] == "dry_run" and dry["ids"] == ["SUS"] and dry["without_current_bars"] == 1
    assert read_table("prices_daily", settings=s).filter(pl.col("security_id") == "SUS").is_empty()
    res = PC.restore_twins(s, apply=True, wait_min=0)
    assert res["status"] == "applied", res
    assert res["rebuild"]["rows_written"] == n_us and res["twins_without_current_bars_after"] == []
    assert pl.read_parquet(res["backup"]).is_empty() and "prices_collision_twins_" in res["backup"]
    after = read_table("prices_daily", settings=s)
    assert after.filter(pl.col("security_id") == "SUS").height == n_us
    assert after.filter(pl.col("security_id") == "SSAP").height == good.filter(pl.col("security_id") == "SSAP").height
    assert INC.FORCE_FULL_KEY not in INC.load_state(s)
    assert {r: v["watermark"] for r, v in INC.load_state(s)["regions"].items()} == wm   # no watermark moved
    ctx2 = DqContext("RT2", s, tables=["prices_daily"])
    assert {x.check_id: x for x in PC.twin_outcomes(ctx2)}["twins_without_bars"].passed
    v = PC.verify(s)
    assert v["ok"] and v["twins_without_current_bars"] == 0 and v["forced_full_queued"] == 0
    assert PC.restore_twins(s)["status"] == "nothing_to_do"


def test_restore_twins_refuses_while_a_price_job_runs(ref, monkeypatch):
    s = ref
    _staged_lake(s)
    _mark_gap(s)
    PC.request_full_rebuild(["SUS"], s)
    monkeypatch.setattr(PC, "running_price_jobs", lambda settings=None: ["prices_finalize"])
    assert PC.restore_twins(s, apply=True, wait_min=0)["status"] == "busy"
    assert INC.load_state(s)[INC.FORCE_FULL_KEY] == ["SUS"]


def test_staged_rows_equals_the_streaming_scan(ref):
    s = ref
    _staged_lake(s)
    u = P.load_universe(s, include_delisted=True)
    k = ["source_id", "symbol", "date"]
    a = INC.staged_rows(s, u)
    b = P._staged_for(u, s)
    assert a.height == b.height > 0 and a.select(b.columns).sort(k).equals(b.sort(k))
