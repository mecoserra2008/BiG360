"""13D / 13G feed (roadmap #7): role assignment subject vs filer, header parsing, rows conform to the contract."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.core.schema import conform
from app.ingestion import sec_13dg as D
from app.ingestion.sec_index import parse_master_idx

IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Company Name
CIK|Company Name|Form Type|Date Filed|File Name
--------------------------------------------------------------------------------
320193|Apple Inc.|SC 13G/A|2026-10-14|edgar/data/320193/0000102909-26-000111.txt
102909|VANGUARD GROUP INC|SC 13G/A|2026-10-14|edgar/data/102909/0000102909-26-000111.txt
1000001|Target Corp Holdings|SCHEDULE 13D|2026-10-14|edgar/data/1000001/0001234567-26-000001.txt
1234567|Elliott Investment Management L.P.|SCHEDULE 13D|2026-10-14|edgar/data/1234567/0001234567-26-000001.txt
2000002|Acme Widgets Inc|SC 13D/A|2026-10-13|edgar/data/2000002/0009999999-26-000002.txt
9999999|Starboard Value LP|SC 13D/A|2026-10-13|edgar/data/9999999/0009999999-26-000002.txt
320193|Apple Inc.|10-Q|2026-10-14|edgar/data/320193/0000320193-26-000100.txt
"""

HDR = """<html><body><pre>
SUBJECT COMPANY:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			Acme Widgets Inc
		CENTRAL INDEX KEY:			0002000002
FILED BY:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			Starboard Value LP
		CENTRAL INDEX KEY:			0009999999
</pre></body></html>"""


def test_header_roles():
    assert D.parse_header_roles(HDR) == ("0002000002", ["0009999999"])


def test_roles_and_rows(tmp_data):
    ents = parse_master_idx(IDX, date(2026, 10, 15))
    assert len(D.filter_13dg(ents)) == 6
    known = {"0000320193"}
    hdr = {"0009999999-26-000002": ("0002000002", ["0009999999"])}
    df = D.build_rows(ents, known, hdr, datetime(2026, 10, 15, tzinfo=UTC))
    df = conform(df, "activist_filings")
    r = {x["accession"]: x for x in df.to_dicts()}
    a = r["0000102909-26-000111"]
    assert a["subject_cik"] == "0000320193" and a["filer_name"] == "VANGUARD GROUP INC"
    assert a["schedule"] == "13G" and a["is_amendment"] and a["role_source"] == "ticker_map"
    b = r["0001234567-26-000001"]   # neither CIK known: manager-name heuristic picks the non-manager as subject
    assert b["subject_name"] == "Target Corp Holdings" and b["role_source"] == "name_heuristic" and not b["is_amendment"]
    c = r["0009999999-26-000002"]
    assert c["subject_cik"] == "0002000002" and c["role_source"] == "header" and c["schedule"] == "13D"
    assert c["knowledge_date"] == date(2026, 10, 13)   # dissemination lag <= 5 days -> Date Filed


def test_delayed_dissemination_and_needs_header():
    ents = parse_master_idx(IDX.replace("2026-10-13", "2026-09-01"), date(2026, 10, 15))
    df = D.build_rows(ents, set(), {}, datetime(2026, 10, 15, tzinfo=UTC))
    row = df.filter(df["accession"] == "0009999999-26-000002").row(0, named=True)
    assert row["knowledge_date"] == date(2026, 10, 15) and row["filed_date"] == date(2026, 9, 1)
    es = [e for e in ents if e["accession"] == "0009999999-26-000002"]
    assert D.needs_header(es, set()) and not D.needs_header(es, {"0002000002"})
