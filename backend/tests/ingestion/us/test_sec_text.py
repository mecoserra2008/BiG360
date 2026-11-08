"""sec_text: HTML -> text and 10-K / 10-Q section extraction (synthetic filings shaped like real EDGAR HTML)."""

from __future__ import annotations

from app.ingestion.sec_text import exhibit_docs, extract_section, extract_sections, html_to_text

FILLER = " ".join(["lorem ipsum dolor sit amet consectetur"] * 150)  # ~5.8k chars


def _10k_html() -> str:
    return f"""<html><head><title>10-K</title><style>p {{color:red}}</style></head><body>
<div style="display:none"><ix:header><ix:hidden>dei:EntityCentralIndexKey 0000320193</ix:hidden></ix:header></div>
<table><tr><td>Item 1.</td><td>Business</td><td>3</td></tr>
<tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr>
<tr><td>Item 7.</td><td>Management&#8217;s Discussion and Analysis</td><td>30</td></tr></table>
<p style="font-weight:bold">PART I</p>
<p><span>Item 1.&#160;&#160;&#160;&#160;Business</span></p><p>BUSINESS-TEXT {FILLER}</p>
<p>See also Item 1A. Risk Factors for a discussion of risks, which is a cross-reference inside a paragraph.</p>
<div>Item 1A. Risk Factors</div><p>RISK-TEXT {FILLER} {FILLER}</p>
<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>
<div>Item 2. Properties</div><p>Cupertino.</p>
<div>Item 3. Legal Proceedings</div><p>LEGAL-TEXT {FILLER[:600]}</p>
<div>Item 4. Mine Safety Disclosures</div><p>Not applicable.</p>
<p>PART II</p>
<div>Item 7. Management&#8217;s Discussion and Analysis of Financial Condition</div><p>MDNA-TEXT {FILLER}</p>
<div>Item 7A. Quantitative and Qualitative Disclosures</div><p>Rates.</p>
<div>Item 8. Financial Statements and Supplementary Data</div><table><tr><td>Revenue</td><td>416,161</td></tr></table>
<script>var x = 1;</script></body></html>"""


def test_html_to_text_strips_hidden_and_scripts():
    t = html_to_text(_10k_html())
    assert "EntityCentralIndexKey" not in t and "var x" not in t and "color:red" not in t
    assert "\nItem 1A. Risk Factors\n" in t                    # block elements become lines
    assert "Revenue \t416,161" in t or "Revenue 416,161" in t
    assert " " not in t


def test_10k_sections_skip_toc_and_cross_references():
    t = html_to_text(_10k_html())
    s = extract_sections(t, "10-K")
    assert set(s) >= {"business", "risk_factors", "mdna", "legal"}
    assert s["business"].startswith("Item 1.") and "BUSINESS-TEXT" in s["business"] and "RISK-TEXT" not in s["business"]
    assert s["risk_factors"].startswith("Item 1A. Risk Factors") and "RISK-TEXT" in s["risk_factors"]
    assert "Unresolved" not in s["risk_factors"]
    assert "MDNA-TEXT" in s["mdna"] and "Rates." not in s["mdna"]
    assert "LEGAL-TEXT" in s["legal"]


def test_10q_sections():
    html = f"""<html><body><p>PART I - FINANCIAL INFORMATION</p><div>Item 1. Financial Statements</div><p>x</p>
    <div>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</div><p>Q-MDNA {FILLER}</p>
    <div>Item 3. Quantitative and Qualitative Disclosures About Market Risk</div><p>q</p>
    <div>Item 4. Controls and Procedures</div><p>c</p><p>PART II - OTHER INFORMATION</p>
    <div>Item 1A. Risk Factors</div><p>Q-RISK {FILLER}</p><div>Item 2. Unregistered Sales of Equity Securities</div>
    <p>none</p></body></html>"""
    s = extract_sections(html_to_text(html), "10-Q")
    assert "Q-MDNA" in s["mdna"] and "Q-RISK" not in s["mdna"]
    assert "Q-RISK" in s["risk_factors"] and "Unregistered" not in s["risk_factors"]


def test_section_guards():
    t = "Item 1A. Risk Factors\nshort\nItem 1B. x"
    assert extract_section(t, r"(?im)^item 1a\. risk factors", [r"(?im)^item 1b"], 3000) is None   # too short
    big = "Item 1A. Risk Factors\n" + FILLER * 3
    assert extract_section(big, r"(?im)^item 1a\. risk factors", [r"(?im)^item 1b"], 500) is None   # >60% of doc


def test_exhibit_docs_from_filing_index():
    html = """<table class="tableFile" summary="Document Format Files">
    <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
    <tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/320193/000032019326000001/aapl-8k.htm">aapl-8k.htm</a></td><td>8-K</td><td>30000</td></tr>
    <tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/320193/000032019326000001/ex99-1.htm">ex99-1.htm</a></td><td>EX-99.1</td><td>9000</td></tr>
    <tr><td>3</td><td></td><td><a href="/Archives/edgar/data/320193/000032019326000001/ex10.htm">ex10.htm</a></td><td>EX-10.1</td><td>1</td></tr>
    </table>"""
    assert exhibit_docs(html) == [("EX-99.1", "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/ex99-1.htm")]
    prim = exhibit_docs(html, ("8-K",))
    assert prim[0][1] == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-8k.htm"
