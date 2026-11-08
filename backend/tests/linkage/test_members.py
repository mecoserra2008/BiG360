"""Geographic member -> ISO2 / region normalization (A5)."""

from __future__ import annotations

import pytest

from app.linkage.members import classify, norm, region_countries

CASES = [
    ("US", "US", ("country", "US", None)),
    ("UK", "US", ("country", "GB", None)),
    ("country:JP", "US", ("country", "JP", None)),
    ("Germany", "US", ("country", "DE", None)),
    ("UnitedKingdom", "US", ("country", "GB", None)),
    ("SouthKorea", "US", ("country", "KR", None)),
    ("Guangdong", "CN", ("country", "CN", None)),
    ("Ontario", "CA", ("country", "CA", None)),
    ("NortheastRegion", "US", ("country", "US", None)),
    ("Europe", "US", ("region", None, "EUROPE")),
    ("EMEA", "US", ("region", None, "EMEA")),
    ("AsiaPacific", "US", ("region", None, "ASIA_PACIFIC")),
    ("PRCHK", "US", ("region", None, "CHINA_HK")),
    ("LatinAmerica", "US", ("region", None, "LATAM")),
    ("EuropeMiddleEastAfrica", "US", ("region", None, "EMEA")),
    ("CanadaAndLatinAmerica", "US", ("region", None, "CA+LATAM")),
    ("AmericasOtherThanUS", "US", ("region", None, "AMERICAS")),
    ("OtherEuropeanCountries", "US", ("region", None, "EUROPE")),
    ("NonUs", "US", ("rest_of_world", None, None)),
    ("RestOfWorld", "US", ("rest_of_world", None, None)),
    ("TotalInternational", "US", ("rest_of_world", None, None)),
    ("ifrs-full:ForeignCountriesMember", "CL", ("rest_of_world", None, None)),
    ("ifrs-full:CountryOfDomicileMember", "CL", ("domestic", "CL", None)),
    ("Domestic", "JP", ("domestic", "JP", None)),
    ("tx:IFRS8InfoAreaGeog_Chile_7", "CL", ("country", "CL", None)),
    ("tx:IFRS8InfoAreaGeog_Europa_10", "CL", ("region", None, "EUROPE")),
    ("tx:IFRS8InfoAreaGeog_Norteamrica_11", "CL", ("region", None, "NORTH_AMERICA")),
    ("IntersegmentElimination", "US", ("other", None, None)),
    ("Corporate", "US", ("other", None, None)),
    ("SegmentGeographicalGroupsOfCountriesGroupOne", "US", ("other", None, None)),
]


@pytest.mark.parametrize("member,cc,expected", CASES)
def test_classify(member, cc, expected):
    assert classify(member, cc) == expected


def test_norm_strips_prefix_suffix_digits():
    assert norm("abc:EuropeMember") == "europe"
    assert norm("tx:IFRS8InfoAreaGeog_Chile_7") == "chile"


def test_region_countries_static_and_composite():
    assert region_countries("NORDICS") == ["DK", "FI", "IS", "NO", "SE"]
    assert set(region_countries("CA+US_CA")) == {"CA", "US"}
    assert "DE" in region_countries("EU") and "GB" not in region_countries("EU")
    assert region_countries("JP") == ["JP"]
    assert region_countries("NOPE") == []
