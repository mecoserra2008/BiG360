"""Guide 13: the watchlist editor's 1-day change % and Composite score rules were rejected (HTTP 4xx) and vanished.
The client now sends the contract params; the server also accepts the payload older clients sent."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routers.alerts import validate_params


def test_price_change_contract_payload():
    assert validate_params("price_change_pct_1d", {"pct": 0.05, "direction": "up"}) == {"pct": 0.05, "direction": "up"}


def test_price_change_legacy_payload_is_normalised():
    p = validate_params("price_change_pct_1d", {"pct_change_1d": -0.05, "direction": "below", "ui_metric": "change1dPct"})
    assert p["pct"] == 0.05 and p["direction"] == "down" and "pct_change_1d" not in p
    assert validate_params("price_change_pct_1d", {"pct": 0.02, "direction": "above"})["direction"] == "up"


def test_score_change_level_payloads():
    p = validate_params("score_change", {"profile_id": "balanced", "mode": "level", "level": 70, "direction": "up"})
    assert p["mode"] == "level" and p["level"] == 70
    legacy = validate_params("score_change", {"profile_id": "balanced", "level": 70, "direction": "above"})
    assert legacy["mode"] == "level" and legacy["direction"] == "up"


@pytest.mark.parametrize(("rt", "params"), [
    ("price_change_pct_1d", {}),
    ("price_change_pct_1d", {"pct": 0.05, "direction": "sideways"}),
    ("score_change", {"mode": "delta"}),
    ("score_change", {"mode": "level"}),
])
def test_still_rejects_invalid(rt, params):
    with pytest.raises(HTTPException):
        validate_params(rt, params)
