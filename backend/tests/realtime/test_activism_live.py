"""Workstream B: live Schedule 13D / 13G -> activism signal (consumer `activism`) -> live activist_13d alert; the EDGAR
Atom fixture maps 13D filers; macro watcher metrics for the Live feeds panel."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.realtime.activism_live import ActivismConsumer, event_type, schedule_of
from app.realtime.alerts_live import triggers_for
from app.realtime.bus import Message
from app.realtime.streams.edgar import envelope, parse_atom


def msg(topic: str, payload: dict, type_: str = "filing", mid: int = 1) -> Message:
    return Message(id=mid, topic=topic, key=payload.get("filing_id", "k"), type=type_,
                   ts_source="2026-10-23T22:20:00Z", ts_ingest=datetime(2026, 10, 23, 22, 21, tzinfo=UTC).timestamp(),
                   payload=payload)


def test_schedule_and_event_type():
    assert schedule_of("SC 13D/A") == "13D" and schedule_of("SCHEDULE 13G") == "13G" and schedule_of("8-K") is None
    assert event_type("SCHEDULE 13D", set()) == "new_13d"
    assert event_type("SCHEDULE 13D", {"13G"}) == "switch_13g_to_13d"
    assert event_type("SCHEDULE 13D", {"13D"}) == "amend_13d"
    assert event_type("SC 13D/A", set()) == "amend_13d"
    assert event_type("SCHEDULE 13G", set()) == "new_13g"


def test_consumer_publishes_activism_signal_and_live_alert():
    c = ActivismConsumer(history=lambda subject, filer: {"13G"})
    p = {"filing_id": "sec_edgar:0001213900-26-112224", "source_id": "sec_edgar", "source_entity_id": "0000320193",
         "accession": "0001213900-26-112224", "form_type": "SCHEDULE 13D", "issuer_id": "I_AAPL", "security_id": "S_AAPL",
         "ticker": "AAPL", "company": "Apple Inc.", "reporting_owner": "Activist Capital LP", "reporting_cik": "0001111111",
         "filed_date": "2026-10-23", "knowledge_ts": "2026-10-23T22:20:00Z", "url": "https://www.sec.gov/x"}
    out = c.handle_one(None, msg("filings", p))   # type: ignore[arg-type]
    assert len(out) == 1
    e = out[0]
    assert e.topic == "signals" and e.type == "activism" and e.key == "activism:0001213900-26-112224"
    assert e.payload["event_type"] == "switch_13g_to_13d" and e.payload["filer_name"] == "Activist Capital LP"
    assert c.handle_one(None, msg("filings", {**p, "form_type": "8-K"})) == []   # type: ignore[arg-type]
    # live activist_13d rule (same trigger as the nightly evaluator)
    sig = msg("signals", e.payload, type_="activism", mid=2)
    rule = {"rule_type": "activist_13d", "params": {}}
    t = triggers_for(rule, ["S_AAPL"], sig, {})
    assert len(t) == 1 and t[0]["as_of_date"] == date(2026, 10, 23) and "13G -> 13D switch" in t[0]["message"]
    assert triggers_for({"rule_type": "activist_13d", "params": {"include_switch": False}}, ["S_AAPL"], sig, {}) == []
    assert triggers_for(rule, ["S_OTHER"], sig, {}) == []


def test_atom_fixture_carries_13d_filer():
    atom = (Path(__file__).parent / "fixtures" / "edgar_current.atom").read_text(encoding="utf-8")
    rows = parse_atom(atom)
    sch = [r for r in rows if schedule_of(r["form_type"])]
    for r in sch:
        env = envelope(r, {"issuer_id": "I1", "security_id": "S1", "ticker": "X"})
        assert env.payload["form_type"] == r["form_type"]
        assert env.payload["reporting_owner"] == ((r["owner"] or {}).get("name"))


def test_macro_capture_metrics():
    from app.realtime.streams.macro_capture import MacroCaptureStream, Pending

    s = MacroCaptureStream()
    now = datetime.now(UTC)
    s.pending = {"a|b": Pending("RE1", "fred:CPIAUCSL", "US", date(2026, 9, 1), "mom_pct", "M", now + timedelta(hours=2),
                                release_name="CPI")}
    m = s.metrics()
    assert m["scheduled"] == 1 and m["next"][0]["release_name"] == "CPI" and m["active"] == 0
