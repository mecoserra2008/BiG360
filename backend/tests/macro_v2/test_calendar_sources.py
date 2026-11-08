"""Offline tests for app.macro.calendar_sources parsers (trimmed real responses recorded 2026-10-12)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.macro import calendar_sources as cs

FX = Path(__file__).parent / "fixtures" / "calendar"


def raw(name: str) -> bytes:
    return (FX / name).read_bytes()


def _check(entries, source_id, country, kind, tz, publisher):
    assert len(entries) > 0
    keys = set()
    for e in entries:
        assert isinstance(e, cs.CalEntry)
        assert e.source_id == source_id
        assert e.country == country
        assert e.kind == kind
        assert e.tz == tz
        assert e.publisher == publisher
        assert isinstance(e.scheduled_local, datetime)
        assert e.scheduled_local.tzinfo is not None and e.scheduled_local.utcoffset() is not None
        assert str(e.scheduled_local.tzinfo) == tz
        assert e.name
        keys.add(e.native_key)
    assert len(keys) == len(entries), "native_key must be unique within a source"


def _find(entries, d: date, name_part: str = ""):
    hits = [e for e in entries if e.scheduled_local.date() == d and name_part.lower() in e.name.lower()]
    assert hits, f"no entry on {d} containing {name_part!r}"
    return hits[0]


# ----------------------------------------------------------------------------------------- US

def test_bls():
    es = cs.parse_bls(raw("bls.ics"))
    _check(es, "bls_cal", "US", "data", "America/New_York", "BLS")
    e = _find(es, date(2025, 1, 10), "Employment Situation")
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (8, 30) and e.time_known
    assert e.scheduled_local.utcoffset().total_seconds() == -5 * 3600


def test_bea():
    es = cs.parse_bea(raw("bea.ics"))
    _check(es, "bea_cal", "US", "data", "America/New_York", "BEA")
    e = _find(es, date(2025, 1, 30), "Gross Domestic Product")
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (8, 30) and e.time_known  # 13:30Z -> 08:30 EST
    assert e.reference_period and "2024" in e.reference_period
    assert _find(es, date(2025, 1, 31), "Personal Income").reference_period == "December 2024"


def test_fomc():
    es = cs.parse_fomc(raw("fomc.html"))
    _check(es, "fomc", "US", "policy_meeting", "America/New_York", "Federal Reserve")
    years = {e.scheduled_local.year for e in es}
    assert {2021, 2022, 2023, 2024, 2025, 2026, 2027} <= years
    assert sum(1 for e in es if e.scheduled_local.year == 2024) == 8
    e = _find(es, date(2024, 12, 18))  # Dec 17-18, 2024 meeting -> decision on the 18th
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (14, 0) and not e.time_known
    _find(es, date(2023, 11, 1))  # 'Oct/Nov 31-1' cross-month meeting
    _find(es, date(2026, 10, 28))
    assert not any(e.scheduled_local.date() == date(2024, 11, 6) for e in es)  # first day is not the decision day


# ----------------------------------------------------------------------------------------- Euro area

def test_eurostat():
    es = cs.parse_eurostat(raw("eurostat.json"))
    _check(es, "eurostat_cal", "EA", "data", "Europe/Luxembourg", "Eurostat")
    e = _find(es, date(2025, 1, 7), "inflation")
    assert e.scheduled_local.hour == 11 and not e.time_known
    assert e.reference_period == "December 2024"
    all_ = cs.parse_eurostat(raw("eurostat.json"), euro_indicators_only=False)
    assert len(all_) > len(es)  # non euro-indicator items are filtered by default


def test_ecb_gc_future_and_past():
    es = cs.parse_ecb_gc(raw("ecb_gc.html"))
    _check(es, "ecb_gc", "EA", "policy_meeting", "Europe/Berlin", "ECB")
    e = _find(es, date(2026, 10, 29))  # Day 2 of the 28-29 Oct 2026 meeting
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (14, 15)
    assert not any(e.scheduled_local.date() == date(2026, 10, 28) for e in es)  # Day 1 dropped
    assert not any(e.scheduled_local.date() == date(2026, 11, 25) for e in es)  # non-monetary meeting dropped
    past = cs.parse_ecb_mopo(raw("ecb_mopo_2025.html"))
    _check(past, "ecb_gc", "EA", "policy_meeting", "Europe/Berlin", "ECB")
    assert len(past) == 8
    _find(past, date(2025, 12, 18))
    _find(past, date(2025, 6, 5))


def test_ecb_statcal():
    es = cs.parse_ecb_statcal(raw("ecb_statcal.html"))
    _check(es, "ecb_statcal", "EA", "data", "Europe/Berlin", "ECB")
    e = _find(es, date(2026, 9, 18), "balance of payments")
    assert e.time_known and (e.scheduled_local.hour, e.scheduled_local.minute) == (10, 0)
    assert e.reference_period == "Jul-2026"
    assert "Dataset" not in e.name


# ----------------------------------------------------------------------------------------- UK

def test_ons():
    es = cs.parse_ons(raw("ons.json"))
    _check(es, "ons_cal", "GB", "data", "Europe/London", "ONS")
    e = _find(es, date(2026, 10, 13), "industry concentration")
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (9, 30)  # 08:30Z -> 09:30 BST
    assert any(e.scheduled_local.year == 2023 for e in es)
    assert all(e.url and e.url.startswith("https://www.ons.gov.uk/releases/") for e in es)


def test_boe():
    es = cs.parse_boe(raw("boe.html"))
    _check(es, "boe_mpc", "GB", "policy_meeting", "Europe/London", "Bank of England")
    assert len(es) == 16
    e = _find(es, date(2026, 11, 5))
    assert e.scheduled_local.hour == 12 and not e.time_known
    _find(es, date(2027, 2, 4))


# ----------------------------------------------------------------------------------------- Japan

def test_boj():
    es = cs.parse_boj(raw("boj.html"))
    _check(es, "boj_mpm", "JP", "policy_meeting", "Asia/Tokyo", "Bank of Japan")
    _find(es, date(2026, 10, 30))  # Oct 29-30, 2026
    _find(es, date(2027, 1, 22))
    past = cs.parse_boj(raw("boj_past.html"), cs.BOJ_PAST_URL)
    _check(past, "boj_mpm", "JP", "policy_meeting", "Asia/Tokyo", "Bank of Japan")
    _find(past, date(2025, 5, 1))  # 'Apr. 30 (Wed.), May 1 (Thurs.)' cross-month meeting
    _find(past, date(2024, 3, 19))  # March 2024 meeting (end of negative rates)


# ----------------------------------------------------------------------------------------- Canada

def test_statcan():
    es = cs.parse_statcan(raw("statcan.json"))
    _check(es, "statcan_cal", "CA", "data", "America/Toronto", "Statistics Canada")
    assert all((e.scheduled_local.hour, e.scheduled_local.minute) == (8, 30) and not e.time_known for e in es)
    assert any(e.scheduled_local.year == 2012 for e in es)
    oct26 = [e for e in es if e.scheduled_local.date().strftime("%Y-%m") == "2026-10"]
    assert oct26 and all(e.reference_period for e in oct26)


def test_boc():
    es = cs.parse_boc(raw("boc.html"))
    _check(es, "boc_rate", "CA", "policy_meeting", "America/Toronto", "Bank of Canada")
    e = _find(es, date(2026, 10, 28))
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (9, 45)
    assert e.reference_period == "with Monetary Policy Report"
    _find(es, date(2027, 1, 27))


# ----------------------------------------------------------------------------------------- Australia

def test_abs():
    es = cs.parse_abs(raw("abs_202611.html"))
    _check(es, "abs_cal", "AU", "data", "Australia/Sydney", "ABS")
    e = _find(es, date(2026, 11, 2), "Building Approvals")
    assert e.time_known and (e.scheduled_local.hour, e.scheduled_local.minute) == (11, 30)
    assert e.reference_period == "September 2026"


def test_rba():
    es = cs.parse_rba(raw("rba.html"))
    _check(es, "rba_board", "AU", "policy_meeting", "Australia/Sydney", "RBA")
    e = _find(es, date(2026, 11, 3))  # 2-3 November 2026 -> decision on the 3rd
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (14, 30)
    assert not any(e.scheduled_local.date() == date(2026, 3, 5) for e in es)  # Payments System Board ignored
    past = cs.parse_rba_media(raw("rba_media_2025.html"))
    _check(past, "rba_board", "AU", "policy_meeting", "Australia/Sydney", "RBA")
    assert len(past) == 8
    _find(past, date(2025, 2, 18))


# ----------------------------------------------------------------------------------------- Brazil

def test_ibge():
    es = cs.parse_ibge(raw("ibge.json"))
    _check(es, "ibge_cal", "BR", "data", "America/Sao_Paulo", "IBGE")
    ipca = [e for e in es if e.name == "Índice Nacional de Preços ao Consumidor Amplo"]
    assert ipca and ipca[0].scheduled_local.date() == date(2025, 10, 9)
    assert (ipca[0].scheduled_local.hour, ipca[0].scheduled_local.minute) == (9, 0) and ipca[0].time_known


def test_bcb_copom():
    past = cs.parse_bcb_comunicados(raw("bcb_comunicados.json"))
    _check(past, "bcb_copom", "BR", "policy_meeting", "America/Sao_Paulo", "BCB")
    e = _find(past, date(2026, 9, 16), "281")
    assert (e.scheduled_local.hour, e.scheduled_local.minute) == (18, 30) and not e.time_known
    fut = cs.parse_bcb_calendar(raw("bcb_calendar.json"))
    _check(fut, "bcb_copom", "BR", "policy_meeting", "America/Sao_Paulo", "BCB")
    _find(fut, date(2026, 11, 4))  # meeting 3-4 Nov 2026 -> decision on day 2
    assert not any(e.scheduled_local.date() == date(2026, 11, 3) for e in fut)


# ----------------------------------------------------------------------------------------- EU national offices

def test_insee():
    es = cs.parse_insee(raw("insee.json"))
    _check(es, "insee_cal", "FR", "data", "Europe/Paris", "INSEE")
    e = _find(es, date(2026, 10, 15), "Consumer price index")
    assert e.time_known and (e.scheduled_local.hour, e.scheduled_local.minute) == (8, 45)
    assert e.reference_period == "September 2026"


def test_destatis():
    es = cs.parse_destatis(raw("destatis.html"))
    _check(es, "destatis_cal", "DE", "data", "Europe/Berlin", "Destatis")
    e = _find(es, date(2026, 10, 13), "Consumer price index")
    assert e.scheduled_local.hour == 8 and not e.time_known
    assert e.reference_period == "September 2026"
    assert any(e.name.endswith("(deadline)") for e in es)


def test_istat():
    es = cs.parse_istat(raw("istat.ics"))
    _check(es, "istat_cal", "IT", "data", "Europe/Rome", "ISTAT")
    assert all(e.time_known for e in es)
    assert all("|" not in e.name for e in es)
    assert any(e.reference_period for e in es)


def test_ine():
    es = cs.parse_ine(raw("ine.ics"))
    _check(es, "ine_cal", "ES", "data", "Europe/Madrid", "INE")
    e = _find(es, date(2025, 1, 24), "Hotel")
    assert e.time_known and e.scheduled_local.hour == 9
    assert e.reference_period == "December 2024"


# ----------------------------------------------------------------------------------------- registry / fetch_all

def test_registry_and_fetch_all_never_raises(monkeypatch):
    assert set(cs.SOURCES) >= {"bls_cal", "bea_cal", "fomc", "eurostat_cal", "ecb_gc", "ons_cal", "boe_mpc",
                               "boj_mpm", "statcan_cal", "boc_rate", "abs_cal", "rba_board", "ibge_cal",
                               "bcb_copom", "insee_cal", "destatis_cal", "istat_cal", "ine_cal", "ecb_statcal"}

    def ok(start, end, settings):
        return cs._in_window(cs.parse_fomc(raw("fomc.html")), start, end)

    def boom(start, end, settings):
        raise RuntimeError("blocked")

    def skip(start, end, settings):
        raise cs.SkipSource("out of range")

    monkeypatch.setattr(cs, "SOURCES", {"fomc": ok, "bad": boom, "skip": skip})
    entries, rep = cs.fetch_all(date(2024, 1, 1), date(2024, 12, 31), settings=object(),
                                sources=["fomc", "bad", "skip", "nope"])
    assert len(entries) == 8 and all(e.scheduled_local.year == 2024 for e in entries)
    assert rep["fomc"]["status"] == "ok" and rep["fomc"]["n"] == 8
    assert rep["bad"]["status"] == "failed" and "blocked" in rep["bad"]["error"]
    assert rep["skip"]["status"] == "skipped"
    assert rep["nope"]["status"] == "skipped"
    for r in rep.values():
        assert set(r) == {"status", "n", "error", "seconds"}


@pytest.mark.parametrize("value,expected", [
    ("Personal Income and Outlays, December 2024", "December 2024"),
    ("Gross Domestic Product, 4th Quarter and Year 2024 (Advance Estimate)", "4th Quarter and Year 2024"),
    ("Employment Situation", None),
])
def test_ref_from_title(value, expected):
    assert cs._ref_from_title(value) == expected
