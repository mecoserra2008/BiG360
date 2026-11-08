"""Live smoke tests for the SEC / FINRA endpoints used by ingest-us (run with: pytest -m network tests/ingestion/us)."""

from __future__ import annotations

import pytest

from app.ingestion.finra_si import FTD_LANDING
from app.ingestion.sec_13f import LANDING as F13_LANDING
from app.ingestion.sec_13f import dataset_links
from app.ingestion.sec_form4 import TSV_LANDING, tsv_dataset_links
from app.ingestion.sec_index import SecIndexAdapter

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def ad():
    return SecIndexAdapter()


def test_daily_index_listing(ad):
    from datetime import date

    listing = ad._daily_listing(date.today().year, (date.today().month - 1) // 3 + 1)
    assert isinstance(listing, dict)


def test_dataset_landing_pages_scrape(ad):
    assert len(tsv_dataset_links(ad.http.get(TSV_LANDING, cache_ttl=3600).text)) > 40
    assert len(dataset_links(ad.http.get(F13_LANDING, cache_ttl=3600).text)) > 20
    assert "cnsfails" in ad.http.get(FTD_LANDING, cache_ttl=3600).text


def test_company_tickers(ad):
    tm = ad.ticker_map()
    assert tm.height > 5000 and "AAPL" in set(tm["ticker"])
