"""Guide 08 / 14: the News Terminal impact estimate showed the event-study CAR 100x too large. The library stores
CAR in percent; the payload now says so (unit = percent) and passes the value through unscaled."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.routers import news as N


class _Db:
    def __init__(self, rows):
        self.snap = SimpleNamespace(is_empty=False)
        self._rows = rows

    def all(self, _sql, _params):
        return self._rows


def _cell(**o):
    base = {"shock_kind": "attention", "window": "0,5", "level": "country_index", "theme": "conflict_military",
            "iso2": "ALL", "sector": "ALL", "n_events": 25, "mean_car": -1.37, "ci_low": -2.1, "ci_high": -0.64,
            "p_value": 0.001, "share_negative": 0.64, "benchmark": "ETF:SPY"}
    return base | o


def test_impact_is_percent_and_unscaled():
    out = N._impact(_Db([_cell()]), ["conflict_military"], ["US"], [])
    assert out is not None and out["unit"] == "percent"
    assert out["mean_car"] == -1.37 and out["ci_low"] == -2.1 and out["ci_high"] == -0.64   # not x100, not /100
    assert out["share_negative"] == 0.64                                                       # fraction stays fraction


def test_impact_none_without_library():
    assert N._impact(_Db([]), ["conflict_military"], ["US"], []) is None
