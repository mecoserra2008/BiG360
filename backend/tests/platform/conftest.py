"""Shared helpers for platform tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.core import ratelimit
from app.scheduler.runner import JobRegistry

FAKE = "tests.platform.fake_jobs"


def make_registry(jobs: list[dict[str, Any]], dags: dict[str, Any] | None = None, **defaults: Any) -> JobRegistry:
    doc = {
        "version": 1,
        "timezone": "America/New_York",
        "dags": dags or {"t": {"cron": None}, "nightly": {"cron": "30 1 * * 2-6"}},
        "defaults": {"retries": 0, "retry_backoff_s": 0, "timeout_min": 1, "on_upstream_failure": "skip", **defaults},
        "jobs": [{"dag": "t", "owner": "test", "writes": [], **j} for j in jobs],
    }
    return JobRegistry(doc)


@pytest.fixture(autouse=True)
def _fresh_limiters():
    ratelimit.reset_limiters()
    yield
    ratelimit.reset_limiters()
