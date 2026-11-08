"""R1.1 bus semantics: idempotent publish, consumer groups + offsets, at-least-once redelivery, replay, DLQ,
retention / compaction, read-only access, Redis stub interface."""

from __future__ import annotations

import inspect
import time

import pytest

from app.realtime.bus import Bus, Envelope, RedisBus, SqliteBus


@pytest.fixture()
def bus(tmp_path):
    b = SqliteBus(tmp_path / "bus.sqlite")
    yield b
    b.close()


def test_publish_idempotent_and_global_ids(bus):
    a = bus.publish("filings", "acc-1", {"x": 1})
    b = bus.publish("news", "n-1", {"x": 2})
    dup = bus.publish("filings", "acc-1", {"x": 999})
    assert a is not None and b is not None and b > a
    assert dup is None                                   # same (topic, key) -> no-op
    assert bus.publish("news", "acc-1", {"x": 3}) is not None   # keys are per topic
    forced = bus.publish("filings", "acc-1", {"x": 4}, force=True)
    assert forced is not None and forced > b
    ids = bus.publish_many([Envelope("filings", "acc-2", {}), Envelope("filings", "acc-2", {}),
                            Envelope("filings", "acc-3", {})])
    assert ids[0] is not None and ids[1] is None and ids[2] is not None
    assert bus.seen("filings", ["acc-1", "acc-9"]) == {"acc-1"}


def test_consumer_groups_offsets_and_redelivery(bus):
    for i in range(5):
        bus.publish("filings", f"k{i}", {"i": i})
    bus.publish("news", "n0", {"i": 100})
    got = bus.poll("g1", ["filings", "news"], limit=3)
    assert [m.payload["i"] for m in got] == [0, 1, 2]
    # nothing committed -> at-least-once: the same messages come back
    again = bus.poll("g1", ["filings", "news"], limit=3)
    assert [m.id for m in again] == [m.id for m in got]
    bus.commit_many("g1", got)
    nxt = bus.poll("g1", ["filings", "news"], limit=10)
    assert [m.payload["i"] for m in nxt] == [3, 4, 100]
    # an independent group starts from the beginning
    assert len(bus.poll("g2", ["filings"], limit=10)) == 5
    # commits are monotonic
    bus.commit("g1", "filings", got[0].id)
    assert bus.offsets("g1")["g1"]["filings"] == got[-1].id
    st = bus.stats()
    assert st["groups"]["g1"]["lag"]["filings"] == 2 and st["topics"]["news"]["messages"] == 1


def test_replay_and_seek(bus):
    ids = [bus.publish("events", f"e{i}", {"i": i}) for i in range(4)]
    assert [m.id for m in bus.read(["events"], after_id=ids[1])] == ids[2:]
    assert [m.id for m in bus.read(None, after_id=0, limit=2)] == ids[:2]
    bus.commit("g", "events", ids[-1])
    assert bus.poll("g", ["events"]) == []
    bus.seek("g", "events", ids[0])            # rewind
    assert [m.id for m in bus.poll("g", ["events"])] == ids[1:]


def test_nack_dead_letter_and_requeue(bus):
    bad = bus.publish("filings", "bad", {"v": "x"})
    good = bus.publish("filings", "good", {"v": "y"})
    m = bus.poll("g", ["filings"])[0]
    assert m.id == bad
    assert bus.nack("g", m, "boom", max_attempts=3) is False
    assert bus.nack("g", m, "boom", max_attempts=3) is False
    assert bus.attempts("g", bad) == 2
    assert bus.poll("g", ["filings"])[0].id == bad          # still redelivered
    assert bus.nack("g", m, "boom 3", max_attempts=3) is True
    rest = bus.poll("g", ["filings"])
    assert [x.id for x in rest] == [good]                     # offset moved past the poison message
    dl = bus.dead_letters("g")
    assert len(dl) == 1 and dl[0]["msg_id"] == bad and dl[0]["error"] == "boom 3" and dl[0]["attempts"] == 3
    assert bus.requeue_dead([dl[0]["id"]]) == 1
    again = bus.poll("g", ["filings"])
    assert again[-1].payload == {"v": "x"} and again[-1].id > good
    assert bus.stats()["dead_letters"] == 0


def test_retention_compaction_and_protected_group(bus):
    for i in range(6):
        bus.publish("signals", f"s{i}", {"i": i}, ckey="AAPL" if i % 2 == 0 else "MSFT")
    for i in range(5):
        bus.publish("raw.news", f"r{i}", {"i": i})
    # age everything
    bus._write(lambda con: con.execute("UPDATE messages SET ts_ingest = ts_ingest - 10 * 86400"))
    bus.commit("lake", "raw.news", bus.latest("raw.news", 10)[2].id)   # lake consumed the first 3 only
    rep = bus.retention({"signals": (1.0, 0, True), "raw.news": (1.0, 0, False)}, protect_groups=("lake",))
    assert rep["raw.news"] == 3 and "signals" not in rep            # the lake never consumed signals: kept
    left = [m.payload["i"] for m in bus.read(["raw.news"])]
    assert left == [3, 4]
    bus.commit("lake", "signals", bus.head_id())
    assert bus.retention({"signals": (1.0, 0, True)})["signals"] == 6
    sig = bus.read(["signals"])
    assert sig == []   # aged out entirely (keep_min 0)
    # compaction alone keeps the newest per ckey
    for i in range(4):
        bus.publish("signals", f"t{i}", {"i": i}, ckey="X" if i < 3 else "Y")
    bus.retention({"signals": (1e6, 0, True)}, protect_groups=())
    assert sorted(m.payload["i"] for m in bus.read(["signals"])) == [2, 3]


def test_keep_min_and_latest_filters(bus):
    for i in range(5):
        bus.publish("news", f"n{i}", {"i": i, "issuer_ids": ["I1"] if i % 2 else ["I2"], "source_id": "rss_fed"},
                    type="press" if i < 2 else "article")
    bus._write(lambda con: con.execute("UPDATE messages SET ts_ingest = ts_ingest - 10 * 86400"))
    bus.retention({"news": (1.0, 3, False)}, protect_groups=())
    assert [m.payload["i"] for m in bus.latest("news", 10)] == [4, 3, 2]
    assert [m.payload["i"] for m in bus.latest("news", 10, where={"issuer_ids": "I1"})] == [3]
    assert [m.payload["i"] for m in bus.latest("news", 10, where={"source_id": ["rss_fed"]})] == [4, 3, 2]
    assert [m.payload["i"] for m in bus.latest("news", 10, types=["press"])] == []


def test_read_only_view(bus, tmp_path):
    bus.publish("macro", "m1", {"a": 1})
    ro = SqliteBus(tmp_path / "bus.sqlite", read_only=True)
    try:
        assert [m.key for m in ro.read(["macro"])] == ["m1"]
        with pytest.raises(PermissionError):
            ro.publish("macro", "m2", {})
        t0 = time.time()
        bus.publish("macro", "m3", {"a": 3})
        assert ro.head_id() == bus.head_id() and time.time() - t0 < 2
    finally:
        ro.close()


def test_redis_stub_has_same_interface():
    abstract = {n for n, v in inspect.getmembers(Bus) if getattr(v, "__isabstractmethod__", False)}
    r = RedisBus()
    for n in abstract:
        assert callable(getattr(r, n))
    with pytest.raises(NotImplementedError):
        r.publish("filings", "k", {})
