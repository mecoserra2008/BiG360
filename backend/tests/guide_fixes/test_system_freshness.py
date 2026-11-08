"""Guide 15: the System page judged freshness on fixed per-DAG tolerances (4 days for nightly tables) while Data
Health used the schedule profile's SLA (9 days under the weekly profile)."""

from app.api.routers import system


def test_profile_sla_used_for_tolerance(tmp_data, monkeypatch):
    monkeypatch.delenv("BIG360_SCHEDULE_PROFILE", raising=False)
    assert system._profile_sla_days() == 9          # default weekly profile
    monkeypatch.setenv("BIG360_SCHEDULE_PROFILE", "daily")
    assert system._profile_sla_days() == 3
