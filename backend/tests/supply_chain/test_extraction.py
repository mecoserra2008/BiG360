"""Roadmap #9 link extraction rules (app.linkage.textrules) and ESEF note-table parsing (app.linkage.esef_geo) on
fixture text - no lake access."""

from __future__ import annotations

import pytest

from app.linkage import esef_geo as E
from app.linkage.textrules import RULES, candidate_units, extract_links, split_sentences


def _by_name(units):
    return {(m.link_type, m.name): m for m in extract_links(units)}


def test_customer_percentage_patterns():
    got = _by_name([
        "Sales to Walmart Inc. accounted for approximately 21% of our net sales in fiscal 2024.",
        "Apple and Samsung accounted for 25% and 12% of our total revenue, respectively.",
        "Approximately 23% of our net revenue was derived from Amazon.com, Inc.",
        "For 2025, Samsung Electronics Co., Ltd. accounted for approximately 11% of total net sales.",
        "Revenue from customers: McKesson (18%), Cardinal Health (15%) of net sales.",
        "Sales to MMC totaled 17%, 15% and 14% of FCX's consolidated revenues in 2025, 2024 and 2023.",
    ])
    assert got[("customer", "Walmart Inc")].share == pytest.approx(0.21)
    assert got[("customer", "Walmart Inc")].rule == "c_sales_to_pct"
    assert got[("customer", "Apple")].share == pytest.approx(0.25)
    assert got[("customer", "Samsung")].share == pytest.approx(0.12)
    assert got[("customer", "Amazon.com, Inc")].share == pytest.approx(0.23)
    assert got[("customer", "Samsung Electronics Co., Ltd")].share == pytest.approx(0.11)
    assert got[("customer", "McKesson")].rule == "c_paren_pct"
    assert got[("customer", "MMC")].share == pytest.approx(0.17)   # latest year = first percentage
    assert ("customer", "Apple and Samsung") not in got             # combined share is not attributed


def test_negatives_and_guards():
    got = _by_name([
        "No customer accounted for more than 10% of our revenue.",
        "Customer A accounted for 12% of net sales.",
        "Revenues generated in the Republic of Ireland represented approximately 2% of our consolidated revenues.",
        "We compete with large vendors such as Oracle Corporation and SAP SE.",
        "In 2021, Russia and Ukraine accounted for approximately 1% of our net sales.",
        "In fiscal year 2024, Rigid Packaging accounted for approximately 24% of consolidated net sales.",
        "Our strongest regions are the Americas, which accounted for 52% of revenues, and APAC represented 11% of revenues.",
        "In 2025, our Demand Response Business represented approximately 4% of total revenues.",
    ])
    # only a strict (exact-issuer-match-required) product name survives extraction; it is dropped at build time
    assert set(got) <= {("customer", "Rigid Packaging")} and all(m.strict for m in got.values())
    # no customer cue in the sentence: kept only if the name resolves exactly to an issuer (strict)
    (m,) = extract_links(["Commerce Media Solutions represented approximately 39% of consolidated revenue."])
    assert m.strict
    (m,) = extract_links(["Sales to MMC totaled 17% of consolidated revenues."])
    assert not m.strict


def test_supplier_and_list_patterns_with_strict_flags():
    got = _by_name([
        "Taiwan Semiconductor Manufacturing Company Limited is our sole foundry for advanced nodes.",
        "We rely on Foxconn to manufacture substantially all of our products.",
        "Purchases from McKesson Corporation accounted for approximately 40% of our total purchases.",
        "We purchase substantially all of our coffee from Starbucks Corporation.",
        "Our suppliers include BASF SE, Dow Inc. and LyondellBasell Industries N.V.",
        "Our largest customers include Apple, Dell Technologies and HP Inc.",
    ])
    tsmc = got[("supplier", "Taiwan Semiconductor Manufacturing Company Limited")]
    assert tsmc.rule == "s_sole" and tsmc.strict
    assert got[("supplier", "Foxconn")].rule == "s_rely" and not got[("supplier", "Foxconn")].strict
    assert got[("supplier", "McKesson Corporation")].share == pytest.approx(0.40)
    assert got[("supplier", "McKesson Corporation")].metric == "purchases"
    assert got[("supplier", "Starbucks Corporation")].rule == "s_purchase_all"
    assert got[("supplier", "LyondellBasell Industries N.V")].strict           # list rules need an exact issuer match
    assert got[("customer", "HP Inc")].rule == "c_largest" and got[("customer", "HP Inc")].strict
    for m in got.values():
        assert m.rule in RULES and 0 < m.rule_conf <= 1 and len(m.snippet) <= 200


def test_customer_table_rows_and_units():
    txt = ("Item 1. Business\nMajor customers\nCustomer \t 2024 \t 2023\n% of revenue\nWalmart Inc. \t 21 % \t 20 %\n"
           "Target Corporation \t 12 % \t 11 %\nCustomer A \t 10 % \t 9 %\n\n" + "Filler sentence without keywords. " * 3)
    units = candidate_units(txt)
    assert units[:2] == ["[T] Walmart Inc.\t21 %\t20 %", "[T] Target Corporation\t12 %\t11 %"]
    got = _by_name(units)
    assert got[("customer", "Walmart Inc")].share == pytest.approx(0.21) and got[("customer", "Walmart Inc")].rule == "c_table"
    assert ("customer", "Customer A") not in got


def test_sentence_split_keeps_legal_suffixes():
    s = split_sentences("Sales to Walmart Inc. accounted for 21% of net sales. The U.S. Government is a customer.")
    assert s[0].endswith("net sales.") and len(s) == 2


def test_snippet_is_bounded():
    long = "Sales to Walmart Inc. accounted for 21% of our net sales " + "and other things " * 40 + "."
    (m,) = extract_links([long])
    assert len(m.snippet) <= 200 and "Walmart" in m.snippet


# ------------------------------------------------------------------------------------------------ ESEF note tables
@pytest.mark.parametrize("raw, val", [("2 435", 2435.0), ("1,234.5", 1234.5), ("1.234,5", 1234.5), ("(12)", -12.0),
                                      ("1.234", 1234.0), ("289,3", 289.3), ("–", None), ("abc", None)])
def test_parse_number(raw, val):
    assert E.parse_number(raw) == val


NAMES = {"spanien": "ES", "frankrike": "FR", "norge": "NO", "sverige": "SE", "tyskland": "DE", "norway": "NO",
         "sweden": "SE", "finland": "FI", "denmark": "DK"}


def test_parse_table_swedish_revenue_by_country(monkeypatch):
    monkeypatch.setattr("app.linkage.members.classify", lambda raw, cc, s=None: ("other", None, None))
    rows = [["Externa intäkter"], ["Koncernen", "2025", "2024"], ["Spanien", "2 435", "131"], ["Frankrike", "1 766", "221"],
            ["Norge", "545", "583"], ["Övriga", "2 400", "1 559"], ["", "7 146", "2 494"]]
    res = E.parse_table(rows, "", 2025, "SE", NAMES)
    assert res is not None and res["reconciled"] and res["total"] == pytest.approx(7146)
    vals = {r[0]: (r[1], r[2], r[3]) for r in res["rows"]}
    assert vals["Spanien"] == (2435.0, "country", "ES") and vals["Övriga"][1] == "rest_of_world"


def test_parse_table_rejects_assets_and_splits_sections(monkeypatch):
    monkeypatch.setattr("app.linkage.members.classify", lambda raw, cc, s=None: ("other", None, None))
    assets = [["Non-current operating assets", "2023", "2022"], ["Norway", "4,069", "3,626"], ["Sweden", "3,116", "2,922"]]
    assert E.parse_table(assets, "", 2023, "NO", NAMES) is None
    both = [["Operating revenues", "2023", "2022"], ["Norway", "9,613", "9,401"], ["Sweden", "4,942", "4,836"],
            ["Other Europe", "40", "49"], ["Total", "14,595", "14,286"],
            ["Non-current operating assets", "2023", "2022"], ["Norway", "4,069", "3,626"], ["Sweden", "3,116", "2,922"]]
    secs = E.split_sections(both)
    assert len(secs) == 2
    res = E.parse_table(secs[0], "", 2023, "NO", NAMES)
    assert res is not None and res["reconciled"]
    kinds = {r[0]: (r[2], r[4]) for r in res["rows"]}
    assert kinds["Other Europe"] == ("region", "EUROPE")
    assert E.parse_table(secs[1], "", 2023, "NO", NAMES) is None
