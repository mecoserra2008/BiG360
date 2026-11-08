"""Coverage v3 A6: ADR ratios from SEC cover pages (parser, FilingSummary cover lookup, extraction with a fake HTTP
client, generated-seed merge where the curated adr_ratios.csv wins). No network."""

from __future__ import annotations

import pytest

from app.core.http import HttpError
from app.ingestion import adr_ratios_sec as A
from app.normalize.identity_universe import load_adr_ratios, seeds_dir

CASES = [
    ("Title of 12(b) Security American Depositary Shares, each representing 100 Series B shares", 100),
    ("American depositary shares (each representing two ordinary shares)", 2),
    ("ADSs, each representing one-fifth of one ordinary share", 0.2),
    ("American Depositary Shares, every five of which represent one ordinary share", 0.2),
    ("Each ADS represents 2,000 ordinary shares of the Company", 2000),
    ("American Depositary Shares, each representing ten (10) Class A ordinary shares, par value", 10),
    ("American Depositary Shares, each representing 0.5 of one ordinary share", 0.5),
    ("Ordinary Shares* *Not for trading, but only in connection with the listing of the American Depositary Shares, "
     "each representing 20 Class A Ordinary Shares", 20),
    ("American depositary shares, each representing twenty-five ordinary shares", 25),
    ("Global Depositary Shares, each representing four common shares", 4),
    ("American Depositary Shares (ADSs), each ADS representing 1/10 of one share", 0.1),
    ("American Depositary Shares (one American Depositary Share representing two thousand four hundred (2,400) "
     "Class A ordinary shares", 2400),
    ("American Depositary Shares, each representing four 25p ordinary shares", 4),
    ("American Depositary Receipts, each representing one B share", 1),
]


@pytest.mark.parametrize(("text", "ratio"), CASES)
def test_parse_ratio(text, ratio):
    got = A.parse_ratio(text)
    assert got is not None and got[0] == pytest.approx(ratio)


@pytest.mark.parametrize("text", ["Ordinary shares, nominal value 0.10 each, representing 5% of the capital",
                                  "Each share represents one vote", ""])
def test_parse_ratio_none(text):
    assert A.parse_ratio(text) is None


def test_cover_report_name():
    xml = ("<FilingSummary><MyReports><Report><ShortName>Cover Page</ShortName><HtmlFileName>R2.htm</HtmlFileName>"
           "</Report><Report><ShortName>Balance Sheet</ShortName><HtmlFileName>R3.htm</HtmlFileName></Report>"
           "</MyReports></FilingSummary>")
    assert A.cover_report_name(xml) == "R2.htm"
    assert A.cover_report_name(b"not xml") is None


class _Resp:
    def __init__(self, content: bytes):
        self.content = content


class FakeHttp:
    def __init__(self, pages: dict[str, bytes]):
        self.pages, self.calls = pages, []

    def get(self, url, cache_ttl=None):  # noqa: ANN001
        self.calls.append(url)
        if url not in self.pages:
            raise HttpError(404, url)
        return _Resp(self.pages[url])


def test_extract_with_fallback_cover_and_reasons():
    import polars as pl

    b1 = A._base("0001001807", "0001104659-26-062051")
    b2 = A._base("0000999999", "0000999999-26-000001")
    http = FakeHttp({
        f"{b1}/R1.htm": b"<html><body><table><tr><td>Document and Entity Information</td></tr><tr><td>Title of 12(b) "
                        b"Security</td><td>American Depositary Share representing 100 Series B shares</td></tr>"
                        b"</table></body></html>",
        f"{b2}/R1.htm": b"<html><body>Audit Information</body></html>",
        f"{b2}/FilingSummary.xml": b"<FilingSummary><MyReports><Report><ShortName>Cover</ShortName>"
                                   b"<HtmlFileName>R2.htm</HtmlFileName></Report></MyReports></FilingSummary>",
        f"{b2}/R2.htm": b"<p>American Depositary Shares, each representing 3 ordinary shares</p>",
    })
    c = pl.DataFrame({"security_id": ["S1", "S2", "S3", "S4"], "issuer_id": ["I1", "I2", "I3", "I4"],
                      "display_ticker": ["TLK", "XYZ", "NOCIK", "NO20F"],
                      "cik": ["0001001807", "0000999999", None, "0000000001"],
                      "accession": ["0001104659-26-062051", "0000999999-26-000001", None, None],
                      "filed": [None] * 4, "underlying": [None, "XYZ.L", None, None]},
                     schema_overrides={"filed": pl.Date})
    found, misses = A.extract(c, http)
    assert found.select("adr_ticker", "adr_ratio", "verified").rows() == [("TLK", "100", "sec_cover"),
                                                                         ("XYZ", "3", "sec_cover")]
    assert {m["ticker"]: m["reason"] for m in misses} == {"NOCIK": "no_cik", "NO20F": "no_20f_on_disk"}
    assert len(http.calls) == 4          # TLK: R1 only; XYZ: R1 + FilingSummary + R2


def test_generated_seed_merges_under_curated(tmp_data):
    import polars as pl

    s = tmp_data
    d = seeds_dir(s)
    d.mkdir(parents=True, exist_ok=True)
    (d / "adr_ratios.csv").write_text("adr_ticker,adr_ratio,underlying,verified,note\nTM,10,7203.T,price,curated\n",
                                      encoding="utf-8")
    found = pl.DataFrame({"adr_ticker": ["TM", "TLK"], "adr_ratio": ["5", "100"], "underlying": ["", ""],
                          "verified": ["sec_cover"] * 2, "note": ["a", "b"]})
    A.write_output(found, [{"ticker": "X", "reason": "no_ratio_phrase"}], d / A.OUT_FILE, s.logs_dir / A.REPORT_FILE, 3)
    r = load_adr_ratios(s)
    assert r["TM"] == 10.0 and r["TLK"] == 100.0      # curated wins, generated fills the rest
    # a second run replaces a ticker's row instead of duplicating it
    A.write_output(found.filter(pl.col("adr_ticker") == "TLK").with_columns(pl.lit("50").alias("adr_ratio")), [],
                   d / A.OUT_FILE, s.logs_dir / A.REPORT_FILE, 1)
    gen = pl.read_csv(d / A.OUT_FILE, comment_prefix="#")
    assert gen.height == 2 and load_adr_ratios(s)["TLK"] == 50.0
