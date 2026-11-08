"""Reconciled limits, aliases, cross-process shared buckets and daily quotas."""

from __future__ import annotations

import time

from app.core import ratelimit as rl


def test_reconciled_defaults():
    assert rl.DEFAULT_LIMITS["polygon"].rate_per_sec * 60 == 4  # research: 4/min shared by massive + polygon hosts
    assert rl.DEFAULT_LIMITS["oecd"].rate_per_sec * 3600 < 60    # 60 downloads per hour
    assert rl.DEFAULT_LIMITS["gleif"].rate_per_sec * 60 == 50
    assert rl.DEFAULT_LIMITS["esef_xbrl_org"].rate_per_sec <= 1
    assert rl.DEFAULT_LIMITS["sec_edgar"].rate_per_sec <= 10
    assert rl.DEFAULT_LIMITS["gdelt_doc"].rate_per_sec <= 0.2


def test_aliases_share_one_bucket(tmp_data):
    assert rl.get_limiter("massive") is rl.get_limiter("polygon")
    assert rl.get_limiter("dart") is rl.get_limiter("opendart")


def test_shared_bucket_state_is_cross_instance(tmp_data):
    lim = rl.RateLimit(5.0, 2)
    a = rl.SharedTokenBucket(lim, "testsrc", tmp_data.state_dir / "ratelimit")
    b = rl.SharedTokenBucket(lim, "testsrc", tmp_data.state_dir / "ratelimit")  # e.g. another process
    t0 = time.monotonic()
    a.acquire()
    b.acquire()        # burst of 2 consumed across both instances
    a.acquire()        # must wait ~0.2 s for a refill
    assert time.monotonic() - t0 >= 0.15
    assert not a.degraded and not b.degraded
    b.penalize(1.0)
    assert a.acquire(timeout=0.3) is False  # penalty seen by the other instance


def test_shared_sources_use_file_bucket_and_can_be_disabled(tmp_data, monkeypatch):
    assert isinstance(rl.get_limiter("polygon"), rl.SharedTokenBucket)
    assert not isinstance(rl.get_limiter("ecb"), rl.SharedTokenBucket)
    rl.reset_limiters()
    monkeypatch.setenv("BIG360_SHARED_RATELIMIT", "0")
    from app.core import config

    config.reset_settings_cache()
    assert not isinstance(rl.get_limiter("polygon"), rl.SharedTokenBucket)


def test_daily_quota(tmp_data):
    q = rl.DailyQuota("dq_test", cap=3, tz="Asia/Seoul")
    assert q.try_consume(2) and q.remaining() == 1
    assert not q.try_consume(2) and q.used() == 2
    q.consume(1)
    assert q.remaining() == 0
    try:
        q.consume()
        raise AssertionError("expected QuotaExceeded")
    except rl.QuotaExceeded:
        pass
    assert rl.get_daily_quota("opendart").cap == 19_500
    assert rl.get_daily_quota("ecb") is None
