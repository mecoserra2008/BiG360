"""Shared helpers for the events-v2 tests: a tiny issuer universe, its alias table / worker context, synthetic GKG
lines and the recorded GDELT row fixtures (fixtures/*.sample.tsv.gz: real 15-minute-file rows, unused heavy GKG
columns blanked)."""

from __future__ import annotations

import gzip
from pathlib import Path

import polars as pl

from app.events_v2 import taxonomy as tx
from app.events_v2.context import issuer_meta
from app.events_v2.dicts import _frame, build_alias_table
from app.events_v2.parse import WorkerContext

FIX = Path(__file__).parent / "fixtures"

ISSUERS = [  # (issuer_id, name, short_name, country, sector)
    ("INVDA", "NVIDIA Corporation", "Nvidia", "US", "TECH"),
    ("ICVX", "Chevron Corporation", "Chevron", "US", "ENRG"),
    ("ICITI", "Citigroup Inc.", "Citigroup", "US", "FINS"),
    ("IWFC", "Wells Fargo & Company", "Wells Fargo", "US", "FINS"),
    ("IDAL", "Delta Air Lines, Inc.", "Delta Air Lines", "US", "INDU"),
    ("ICARR", "Carrefour SA", "Carrefour", "FR", "CSTA"),
    ("IMETA", "Meta Platforms, Inc.", "Meta Platforms", "US", "COMM"),
    ("IMU", "Micron Technology, Inc.", "Micron Technology", "US", "TECH"),
    ("IAMD", "Advanced Micro Devices, Inc.", "AMD", "US", "TECH"),
    ("IVZ", "Verizon Communications Inc.", "Verizon", "US", "COMM"),
    ("INDAQ", "Nasdaq, Inc.", "Nasdaq", "US", "FINS"),
    ("ISBUX", "Starbucks Corporation", "Starbucks", "US", "CDIS"),
    ("ICMG", "Chipotle Mexican Grill, Inc.", "Chipotle", "US", "CDIS"),
    ("IBYDA", "BYD Company Limited", "BYD", "CN", "CDIS"),
    ("IBYDH", "BYD Company Limited", "BYD", "HK", "CDIS"),
]
EXTRA_ALIASES = [("facebook", "IMETA", "alias")]


def issuer_dict() -> pl.DataFrame:
    rows = []
    for k, (iid, name, short, cty, sec) in enumerate(ISSUERS, start=1):
        region = {"US": "US", "FR": "EU", "CN": "CN", "HK": "HK"}[cty]
        rows.append({"dict_type": "issuer", "id": k, "code": iid, "name": name, "grp": cty, "theme_mask": None,
                     "sector_mask": tx.sector_mask([sec]),
                     "extra": f'{{"ticker": "{short.upper()[:4]}", "security_id": "S{iid}", "region": "{region}"}}'})
    return _frame(rows)


def alias_rows() -> list[dict]:
    rows = []
    for iid, name, short, *_ in ISSUERS:
        rows.append({"org": name, "issuer_id": iid, "kind": "legal"})
        rows.append({"org": short, "issuer_id": iid, "kind": "short"})
    rows += [{"org": o, "issuer_id": i, "kind": k} for o, i, k in EXTRA_ALIASES]
    return rows


def worker_ctx() -> WorkerContext:
    idict = issuer_dict()
    aliases = build_alias_table(rows=alias_rows(), issuer_dict=idict)
    return WorkerContext.build(aliases, issuer_meta(idict))


def iid_of(issuer_id: str) -> int:
    return [k for k, x in enumerate(ISSUERS, start=1) if x[0] == issuer_id][0]


def gkg_line(date14: str, url: str, title: str, orgs: str, v1themes: str = "", enhanced: str = "",
             locations: str = "", tone: str = "-1.5,2.0,3.5,5.5,20,1,500", domain: str = "example.com",
             translation: str = "") -> bytes:
    cols = [""] * 27
    cols[0], cols[1], cols[2], cols[3], cols[4] = f"{date14}-1", date14, "1", domain, url
    cols[7], cols[8], cols[9], cols[13], cols[15] = v1themes, enhanced, locations, orgs, tone
    cols[25] = translation
    cols[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>"
    return "\t".join(cols).encode("utf-8")


def fixture_bytes(kind: str) -> bytes:
    return gzip.open(FIX / f"{kind}.sample.tsv.gz").read()
