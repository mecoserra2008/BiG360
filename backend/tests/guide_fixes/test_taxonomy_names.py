"""Guide 03: subsector names with commas were cut at the first comma (unquoted YAML flow mappings)."""

import yaml

from app.core.config import get_settings


def test_subsector_names_parse_whole():
    p = get_settings().project_root / "contracts" / "sector_taxonomy.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    for x in d["subsectors"]:
        assert set(x) <= {"id", "sector", "name", "groups", "kpi_set", "kpi_sets"}, x
    names = {x["id"]: x["name"] for x in d["subsectors"]}
    assert names["apparel_luxury"] == "Apparel, Footwear & Luxury"
    assert names["hotels_travel_leisure"] == "Hotels, Travel, Cruise & Casinos"
    assert names["reit_specialty"] == "Specialty REITs (data centers, towers, healthcare, hotel)"
