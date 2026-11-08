"""Test fixtures. `tmp_data` points the app's data dir at a temp folder (never touches the real data/)."""

from __future__ import annotations

import pytest

from app.core import config as config_mod
from app.core import schema as schema_mod


@pytest.fixture(autouse=True)
def _no_live_process_side_effects(monkeypatch):
    """Incident 2026-10-24 safety: tests never run the supervisor's reaper / stray-API killer against the real
    machine, and the runner's machine-pressure guard does not delay jobs (tests that need them enable them)."""
    monkeypatch.setenv("BIG360_SUPERVISOR_REAPER", "0")
    monkeypatch.setenv("BIG360_PRESSURE_GUARD", "0")
    # refresh-pipeline fixes 2026-10-26: the exclusive heavy slot (app.ops.heavy_slot) is off unless a test enables it
    monkeypatch.setenv("BIG360_HEAVY_SLOT", "0")
    # ram_fix 2026-10-31: RAM-budget admission (app.ops.ram_budget) reads the real machine's free RAM: off unless a
    # test enables it (budget tests pass their own free-RAM numbers)
    monkeypatch.setenv("BIG360_RAM_BUDGET", "0")
    # coverage v3 A7: /system/status computes inline (no background cache) so tests see their own state changes;
    # tests/platform/test_status_cache.py enables the cache
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "0")
    yield


@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("BIG360_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    s = config_mod.get_settings()
    s.ensure_dirs()
    yield s
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
