"""Guide 02b: the Chicago Fed National Activity Index tile read -88.9% y/y (a growth rate of a zero-centred index)."""

from datetime import date, timedelta
from types import SimpleNamespace

from app.api.data_integrity import display_fields


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.as_of = date(2026, 10, 1)

    def all(self, _sql, _params):
        return self.rows


def _rows(sid, vals):
    d0 = date(2016, 1, 1)
    return [{"series_id": sid, "date": d0 + timedelta(days=31 * i), "value": v} for i, v in enumerate(vals)]


def test_signed_index_stays_a_level():
    vals = [0.2, -0.4, 0.1, -1.2, 0.5] * 26
    meta = {"series_id": "fred:CFNAI", "category": "growth", "frequency": "M", "units": "Index", "scale": "index"}
    out = display_fields(_Db(_rows("fred:CFNAI", vals)), [meta])["fred:CFNAI"]
    assert out["display_transform"] == "level" and out["display_unit"] == "level"


def test_positive_level_index_keeps_yoy():
    vals = [100 + i for i in range(130)]
    meta = {"series_id": "fred:INDPRO", "category": "growth", "frequency": "M", "units": "Index 2017=100", "scale": "index"}
    out = display_fields(_Db(_rows("fred:INDPRO", vals)), [meta])["fred:INDPRO"]
    assert out["display_transform"] == "yoy"


def test_fx_futures_link_the_spot_pair_pages():
    """Guide 02b: the EURUSD instrument page had no CFTC positioning link."""
    from app.ingestion.cftc_mapping import CURATED

    assert "IDX:EURUSD=X" in CURATED["099741"].links
    assert "IDX:GBPUSD=X" in CURATED["096742"].links and "IDX:USDJPY=X" in CURATED["097741"].links
