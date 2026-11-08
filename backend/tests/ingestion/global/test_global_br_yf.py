"""Brazil CVM (DFP/ITR/VLMO zips built from real row layouts) and yfinance fallback fundamentals (no network)."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from app.core.schema import conform
from app.ingestion import cvm, yf_fundamentals
from app.ingestion._global_common import frame

H_IDX = "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;CATEG_DOC;ID_DOC;DT_RECEB;LINK_DOC"
H_ST = ("CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;GRUPO_DFP;MOEDA;ESCALA_MOEDA;ORDEM_EXERC;DT_INI_EXERC;DT_FIM_EXERC;"
        "CD_CONTA;DS_CONTA;VL_CONTA;ST_CONTA_FIXA")


def _zip(files: dict[str, str]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, t in files.items():
            z.writestr(n, t.encode("latin-1"))
    return b.getvalue()


def test_cvm_itr_zip():
    pet = "33.000.167/0001-01;2026-06-30;{v};PETROLEO BRASILEIRO S.A. PETROBRAS;009512"
    files = {
        "itr_cia_aberta_2026.csv": "\n".join([H_IDX,
                                              pet.format(v=1) + ";ITR;160001;2026-08-07;http://x/1",
                                              pet.format(v=2) + ";ITR;160002;2026-08-20;http://x/2",
                                              "00.000.000/0001-91;2026-06-30;1;BCO BRASIL;001023;ITR;160003;2026-08-12;http://x/3"]),
        "itr_cia_aberta_DRE_con_2026.csv": "\n".join([H_ST,
            pet.format(v=1) + ";DF Consolidado - DRE;REAL;MIL;ÚLTIMO;2026-04-01;2026-06-30;3.01;Receita de Venda;119130000;S",
            pet.format(v=1) + ";DF Consolidado - DRE;REAL;MIL;ÚLTIMO;2026-01-01;2026-06-30;3.01;Receita de Venda;242270000;S",
            pet.format(v=1) + ";DF Consolidado - DRE;REAL;MIL;PENÚLTIMO;2025-01-01;2025-06-30;3.01;Receita de Venda;1;S",
            pet.format(v=1) + ";DF Consolidado - DRE;REAL;MIL;ÚLTIMO;2026-01-01;2026-06-30;3.99.01.01;LPA ON;2.31;S",
            pet.format(v=2) + ";DF Consolidado - DRE;REAL;MIL;ÚLTIMO;2026-04-01;2026-06-30;3.01;Receita de Venda;119200000;S"]),
        "itr_cia_aberta_BPA_ind_2026.csv": "\n".join([H_ST,
            pet.format(v=1) + ";DF Individual - BPA;REAL;MIL;ÚLTIMO;;2026-06-30;1;Ativo Total;900000000;S"]),
    }
    fil, facts = cvm.parse_statement_zip(_zip(files), "ITR", 2026, {"9512"})
    f = conform(frame(fil, "filings_index"), "filings_index")
    assert f.height == 2 and set(f["source_entity_id"]) == {"9512"} and f.filter(f["is_amendment"]).height == 1
    df = conform(frame(facts, "fundamentals_raw"), "fundamentals_raw")
    q2 = df.filter((df["concept"] == "3.01") & (df["period_start"] == date(2026, 4, 1)))
    assert sorted(q2["value"].to_list()) == [119_130_000_000.0, 119_200_000_000.0]
    assert set(q2["knowledge_date"]) == {date(2026, 8, 7), date(2026, 8, 20)} and set(q2["fiscal_period"]) == {"Q2"}
    assert df.filter(df["concept"] == "3.99.01.01")["value"][0] == pytest.approx(2.31)  # per-share never scaled
    assert df.filter(df["concept"] == "3.99.01.01")["unit"][0] == "BRL/shares"
    bpa = df.filter(df["concept"] == "1")
    assert bpa["period_type"][0] == "instant" and not bpa["consolidated"][0]
    assert df.filter(df["value"] == 1000.0).height == 0  # PENÚLTIMO dropped


def test_cvm_vlmo_zip():
    idx = ("CNPJ_Companhia;Nome_Companhia;Data_Referencia;Versao;Codigo_CVM;Categoria;Tipo;Data_Entrega;Tipo_Apresentacao;"
           "Motivo_Reapresentacao;Protocolo_Entrega;Link_Download\n"
           "33.000.167/0001-01;PETROBRAS;2026-08-01;1;9512;VM;Posição Consolidada;2026-09-09;AP;;p;http://x")
    con = ("CNPJ_Companhia;Nome_Companhia;Data_Referencia;Versao;Tipo_Empresa;Empresa;Tipo_Cargo;Tipo_Movimentacao;"
           "Descricao_Movimentacao;Tipo_Operacao;Tipo_Ativo;Caracteristica_Valor_Mobiliario;Intermediario;Data_Movimentacao;"
           "Quantidade;Preco_Unitario;Volume\n"
           "33.000.167/0001-01;PETROBRAS;2026-08-01;1;Companhia;PETROBRAS;Diretor ou Vinculado;Saldo Inicial;;Crédito;Ações;PN;;;5000;;\n"
           "33.000.167/0001-01;PETROBRAS;2026-08-01;1;Companhia;PETROBRAS;Diretor ou Vinculado;Compra à vista;;Crédito;Ações;PN;;2026-08-14;1000;36.5000000000;36500\n"
           "33.000.167/0001-01;PETROBRAS;2026-08-01;1;Companhia;PETROBRAS;Conselho de Administração ou Vinculado;Venda à vista;;Débito;Ações;ON;;2026-08-20;200;40.1;8020\n"
           "33.000.167/0001-01;PETROBRAS;2026-08-01;1;Controlada;SUB;Diretor ou Vinculado;Compra à vista;;Crédito;Ações;ON;;2026-08-20;9;1;9")
    fil, ins = cvm.parse_vlmo_zip(_zip({"vlmo_cia_aberta_2026.csv": idx, "vlmo_cia_aberta_con_2026.csv": con}), 2026, None)
    df = conform(frame(ins, "insider_transactions"), "insider_transactions")
    assert df.height == 2 and set(df["region"]) == {"LATAM"} and set(df["knowledge_date"]) == {date(2026, 9, 9)}
    buy = df.filter(df["txn_type"] == "buy")
    assert buy["is_open_market"][0] and buy["is_officer"][0] and buy["price"][0] == 36.5 and buy["shares"][0] == 1000
    assert buy["insider_id"][0] == "cvm:9512:Diretor ou Vinculado"
    sell = df.filter(df["txn_type"] == "sell")
    assert sell["is_director"][0] and sell["acquired_disposed"][0] == "D"
    assert len(fil) == 1 and fil[0]["form_class"] == "insider"


def test_yfinance_payload_to_facts(tmp_data):
    payload = {"ticker": "0700.HK", "profile": {"financialCurrency": "CNY", "sharesOutstanding": 9.1e9},
               "statements": {"income_stmt": [{"label": "Total Revenue", "end": "2025-12-31", "value": 7.5e11},
                                              {"label": "Diluted EPS", "end": "2025-12-31", "value": 21.0}],
                              "quarterly_income_stmt": [{"label": "Total Revenue", "end": "2026-06-30", "value": 2.0e11}],
                              "balance_sheet": [{"label": "Ordinary Shares Number", "end": "2025-12-31", "value": 9.1e9}]}}
    rows = yf_fundamentals.yf_records_to_facts(payload, first_seen=date(2026, 10, 10), region="HK")
    df = conform(frame(rows, "fundamentals_raw"), "fundamentals_raw")
    assert df["knowledge_date_estimated"].all() and set(df["filing_id"]) == {"yfinance_fund:na"}
    fy = df.filter((df["concept"] == "Total Revenue") & (df["period_end"] == date(2025, 12, 31)))
    assert fy["knowledge_date"][0] == date(2026, 4, 30) and fy["period_start"][0] == date(2025, 1, 1) and fy["unit"][0] == "CNY"
    q = df.filter(df["period_end"] == date(2026, 6, 30))
    assert q["period_start"][0] == date(2026, 4, 1) and q["knowledge_date"][0] == date(2026, 8, 29)
    assert df.filter(df["concept"] == "Diluted EPS")["unit"][0] == "CNY/shares"
    assert df.filter(df["concept"] == "Ordinary Shares Number")["unit"][0] == "shares"
    assert df.filter(df["concept"] == "info:sharesOutstanding")["knowledge_date"][0] == date(2026, 10, 10)
