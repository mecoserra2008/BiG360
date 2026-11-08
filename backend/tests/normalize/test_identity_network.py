"""LIVE integration: build identity for ~30 real issuers across regions into a temp data dir.
Run with:  ..\\.venv\\Scripts\\python.exe -m pytest -m network tests/normalize/test_identity_network.py -q
(SEC, Massive (if key), JPX, EDINET code list, KRX KIND, SSE/SZSE, HKEX, CVM, B3, ESMA FIRDS, GLEIF.)"""

from __future__ import annotations

import polars as pl
import pytest

from app.core.storage import read_table
from app.normalize import identity as I

pytestmark = pytest.mark.network

SEEDS = """ticker,mic,yf_ticker,isin,name,country,region,adr_ticker,issuer_group,share_class,notes
MC,XPAR,MC.PA,,LVMH Moet Hennessy Louis Vuitton,FR,EU,,,,
ASML,XAMS,ASML.AS,,ASML Holding,NL,EU,ASML,,,
SAP,XETR,SAP.DE,,SAP,DE,EU,SAP,,,
NESN,XSWX,NESN.SW,,Nestle,CH,EU,,,,
NOVO B,XCSE,NOVO-B.CO,,Novo Nordisk,DK,EU,NVO,,B,
EDP,XLIS,EDP.LS,,EDP - Energias de Portugal,PT,EU,,,,
SHEL,XLON,SHEL.L,,Shell,GB,UK,SHEL,,,
AZN,XLON,AZN.L,,AstraZeneca,GB,UK,AZN,,,
HSBA,XLON,HSBA.L,,HSBC Holdings,GB,UK,HSBC,,,
7203,XTKS,7203.T,,Toyota Motor,JP,JP,TM,,,
6758,XTKS,6758.T,,Sony Group,JP,JP,SONY,,,
8306,XTKS,8306.T,,Mitsubishi UFJ Financial Group,JP,JP,MUFG,,,
005930,XKRX,005930.KS,,Samsung Electronics,KR,KR,,,,
000660,XKRX,000660.KS,,SK hynix,KR,KR,,,,
0700,XHKG,0700.HK,,Tencent Holdings,CN,HK,,,,
1299,XHKG,1299.HK,,AIA Group,HK,HK,,,,
600519,XSHG,600519.SS,,Kweichow Moutai,CN,CN,,,A,
300750,XSHE,300750.SZ,,Contemporary Amperex Technology,CN,CN,,,A,
PETR4,BVMF,PETR4.SA,,Petrobras (PN),BR,LATAM,PBR-A,petrobras,PN,
PETR3,BVMF,PETR3.SA,,Petrobras (ON),BR,LATAM,PBR,petrobras,ON,
VALE3,BVMF,VALE3.SA,,Vale,BR,LATAM,VALE,,,
WALMEX,XMEX,WALMEX.MX,,Wal-Mart de Mexico,MX,LATAM,,,,
"""


def test_live_identity_30_issuers(tmp_data):
    d = tmp_data.data_dir / "reference" / "seeds"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mvp_global.csv").write_text(SEEDS, encoding="utf-8")
    res = I.build_reference(settings=tmp_data, us_size=10, max_submissions=40, fetch_yf=False, write_universe=True)
    iss = read_table("issuers")
    assert iss.height > 5000                                    # every US-listed common stock / ADR + the seeds
    # US (SEC + Massive)
    for cik in ("320193", "789019", "19617", "1045810", "1018724", "1652044", "1067983", "200406"):  # AAPL MSFT JPM NVDA AMZN GOOGL BRK JNJ
        assert I.lookup("cik", cik) is not None, cik
    assert I.lookup("ticker_mic", "AAPL@XNAS")["listing_id"]
    # seeds + national registries
    toy = I.lookup("yf_ticker", "7203.T")
    assert toy and I.lookup("edinet_code", "E02144")["issuer_id"] == toy["issuer_id"]
    assert I.lookup("cik", "1094517")["issuer_id"] == toy["issuer_id"]                 # TM ADR linked to the home issuer
    assert I.lookup("isin", "KR7005930003")["issuer_id"] == I.lookup("yf_ticker", "005930.KS")["issuer_id"]
    assert I.lookup("isin", "KYG875721634")["issuer_id"] == I.lookup("hk_code", "700")["issuer_id"]
    pb = I.lookup("cvm_code", "9512")
    assert pb and pb["issuer_id"] == I.lookup("ticker_mic", "PBR@XNYS")["issuer_id"] == I.lookup("yf_ticker", "PETR3.SA")["issuer_id"]
    assert I.lookup("lei", "724500Y6DUVHQD6OXN27")["issuer_id"] == I.lookup("yf_ticker", "ASML.AS")["issuer_id"]  # FIRDS
    assert I.lookup("cn_code", "600519.SS") is not None
    seeds = iss.filter(pl.col("region") != "US")
    assert seeds.filter(pl.col("region").is_in(["EU", "UK", "JP", "KR", "CN", "HK", "LATAM"])).height >= 20
    um = read_table("universe_membership")
    assert um.filter(pl.col("universe_id") == "mvp_global").height >= 20
    assert um.filter(pl.col("universe_id") == "mvp_us").height == 10
    assert res["stats"]["taxonomy"]["n"] > 0
