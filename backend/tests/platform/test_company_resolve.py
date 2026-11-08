"""Coverage v3 A7: Company 360 Debt tab route ids. resolve_sec() maps a ticker / ISIN / unambiguous alias to the internal
security id; an alias root shared by several issuers is a 404 naming the candidates, never another company's row."""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest
from fastapi import HTTPException

from app.api.deps import Db
from app.api.routers._common import SEC_REF_COLS
from app.api.routers.companies import resolve_sec

ROWS = [
    # security_id, issuer_id, ticker, isin, is_active, is_primary_security, market_cap_usd
    ("S2317TW", "IHONHAI", "2317.TW", "TW0002317005", True, True, 80e9),
    ("S2317T", "ISYSTENA", "2317.T", "JP3351050009", True, True, 1.2e9),
    ("S2317HK", "IVEDAN", "2317.HK", None, True, True, None),
    ("S7203T", "ITOYOTA", "7203.T", "JP3633400001", True, True, 250e9),
    ("S7203ADR", "ITOYOTA", "TM", "US8923313071", True, False, 250e9),
    ("SBRKB", "IBERK", "BRK-B", "US0846707026", True, True, 900e9),
    ("SBRKA", "IBERK", "BRK-A", "US0846701086", True, False, 900e9),
    ("SAAPL", "IAPPLE", "AAPL", "US0378331005", True, True, 3e12),
]


@pytest.fixture()
def db():
    con = duckdb.connect()
    extra = [c for c in SEC_REF_COLS if c not in ("security_id", "issuer_id", "ticker", "isin", "market_cap_usd")]
    cols = ", ".join([f"{c} VARCHAR" for c in extra])
    con.execute(f"""CREATE TABLE sec (security_id VARCHAR, issuer_id VARCHAR, ticker VARCHAR, isin VARCHAR,
                    is_active BOOLEAN, is_primary_security BOOLEAN, market_cap_usd DOUBLE, {cols})""")
    con.executemany("INSERT INTO sec (security_id, issuer_id, ticker, isin, is_active, is_primary_security, "
                    "market_cap_usd) VALUES (?, ?, ?, ?, ?, ?, ?)", ROWS)
    d = Db(SimpleNamespace(con=con))
    yield d
    d.close()
    con.close()


@pytest.mark.parametrize("ident, sid", [
    ("SAAPL", "SAAPL"),          # internal id
    ("aapl", "SAAPL"),           # exact ticker, any case
    (" AAPL ", "SAAPL"),
    ("2317.TW", "S2317TW"),      # full ticker of a shared root still resolves
    ("2317.t", "S2317T"),
    ("JP3633400001", "S7203T"),  # ISIN
    ("7203", "S7203T"),          # alias: one issuer -> its primary listing
    ("BRK.B", "SBRKB"),          # '-' read as '.'
])
def test_resolves(db, ident, sid):
    assert resolve_sec(db, ident)["security_id"] == sid


def test_ambiguous_alias_root_is_404_with_candidates(db):
    """2317 = Hon Hai (TW), Systena (T), Vedan (HK): no silent pick of one issuer's debt."""
    with pytest.raises(HTTPException) as e:
        resolve_sec(db, "2317")
    assert e.value.status_code == 404
    assert "ambiguous" in e.value.detail and all(t in e.value.detail for t in ("2317.HK", "2317.T", "2317.TW"))


def test_unknown_is_404(db):
    for ident in ("NOPE.XX", "", "X" * 80):
        with pytest.raises(HTTPException) as e:
            resolve_sec(db, ident)
        assert e.value.status_code == 404 and "ambiguous" not in e.value.detail


def test_exact_ticker_collision_matches_search(db):
    """Review A7_A8: an exact ticker listed by two issuers (SBS: a Bulgarian primary listing and the Sabesp ADR) must
    resolve to the same security as /search, which the frontend's resolveId() uses for every Company 360 tab."""
    from app.api.routers.meta import search_securities

    db.all("ALTER TABLE sec ADD COLUMN search_text VARCHAR")
    db.all("INSERT INTO sec (security_id, issuer_id, ticker, isin, is_active, is_primary_security, market_cap_usd) "
           "VALUES ('SSBSBG', 'ISBSBG', 'SBS', 'BG1100003166', TRUE, TRUE, NULL), "
           "('SSBSADR', 'ISABESP', 'SBS', 'US20441A1025', TRUE, FALSE, 12e9)")
    db.all("UPDATE sec SET search_text = lower(ticker || ' ' || coalesce(name, '') || ' ' || coalesce(isin, ''))")
    hit = next(r for r in search_securities(q="SBS", limit=5, db=db) if r["ticker"] == "SBS")
    assert resolve_sec(db, "SBS")["security_id"] == hit["security_id"] == "SSBSBG"   # active, primary listing first
