"""COVERAGE_V3 B3: CVM DFP/ITR for every cvm_code of the universe, PN / unit lines mapped to the registrant (issuer
level), PIT knowledge dates from DT_RECEB of the version the values come from, fiscal labels for non-December year
ends, incremental / resumable per-zip state. Zips are built in the tests from the real CVM row layouts (no network).
"""

from __future__ import annotations

import gzip
import io
import zipfile
from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.core.storage import read_table, write_table
from app.ingestion import cvm
from app.ingestion._global_common import load_state

H_IDX = "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;CATEG_DOC;ID_DOC;DT_RECEB;LINK_DOC"
H_ST = ("CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;GRUPO_DFP;MOEDA;ESCALA_MOEDA;ORDEM_EXERC;DT_INI_EXERC;DT_FIM_EXERC;"
        "CD_CONTA;DS_CONTA;VL_CONTA;ST_CONTA_FIXA")
H_VM = ("CNPJ_Companhia;Data_Referencia;Versao;ID_Documento;Nome_Empresarial;Valor_Mobiliario;Sigla_Classe_Acao_Preferencial;"
        "Classe_Acao_Preferencial;Codigo_Negociacao;Composicao_BDR_Unit;Mercado;Sigla_Entidade_Administradora;"
        "Entidade_Administradora;Data_Inicio_Negociacao;Data_Fim_Negociacao;Segmento;Data_Inicio_Listagem;Data_Fim_Listagem")
H_GERAL = "CNPJ_Companhia;Data_Referencia;Versao;ID_Documento;Nome_Empresarial;Codigo_CVM;Setor_Atividade"
H_CAD = "CNPJ_CIA;DENOM_SOCIAL;DENOM_COMERC;DT_REG;SIT;CD_CVM;SETOR_ATIV"

# registrants: GERDAU (3980, ON+PN, in identifier_map), TREVISA (8192, PN-only line, CVM code missing in the reference:
# FCA ticker is garbage '8192' -> resolved by name), MRS (17949, OTC 'MRSA5B' lines -> exact FCA ticker),
# RAIZEN (25917, March year-end), OUTSIDER (99999, not listed in the reference -> out of scope)
GER, TRE, MRS, RAI, OUT = ("33.611.500/0001-19", "3980", "GERDAU S.A."), ("92.660.570/0001-26", "8192", "TREVISA INVESTIMENTOS SA"), \
    ("01.417.222/0001-77", "17949", "MRS LOGISTICA S.A."), ("33.453.598/0001-23", "25917", "RAIZEN S.A."), \
    ("11.111.111/0001-11", "99999", "OUTSIDER SA")


def _zip(files: dict[str, str]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, t in files.items():
            z.writestr(n, t.encode("latin-1"))
    return b.getvalue()


def _co(c: tuple[str, str, str], ref: str, ver: int = 1) -> str:
    return f"{c[0]};{ref};{ver};{c[2]};{c[1].zfill(6)}"


def _st(c, ref, ver, start, end, acct, val, order="ÚLTIMO", scale="MIL", grp="DF Consolidado") -> str:
    return f"{_co(c, ref, ver)};{grp};REAL;{scale};{order};{start};{end};{acct};Conta {acct};{val};S"


def itr_2026() -> bytes:
    idx = [H_IDX,
           _co(GER, "2026-06-30", 1) + ";ITR;160001;2026-08-05;http://x/1",
           _co(GER, "2026-06-30", 2) + ";ITR;160050;2026-08-19;http://x/2",          # restatement: values of v2 only
           _co(TRE, "2026-06-30", 1) + ";ITR;160002;2026-08-12;http://x/3",
           _co(MRS, "2026-06-30", 1) + ";ITR;160003;2026-08-10;http://x/4",
           _co(RAI, "2026-06-30", 1) + ";ITR;160004;2026-08-13;http://x/5",
           _co(OUT, "2026-06-30", 1) + ";ITR;160005;2026-08-14;http://x/6",
           _co(TRE, "2026-03-31", 1) + ";ITR;160006;2099-01-01;http://x/7",          # future receipt: dropped
           _co(MRS, "2026-03-31", 1) + ";ITR;160007;2026-03-01;http://x/8"]          # before the period end: dropped
    dre = [H_ST,
           _st(GER, "2026-06-30", 2, "2026-04-01", "2026-06-30", "3.01", "18000000"),
           _st(GER, "2026-06-30", 2, "2026-01-01", "2026-06-30", "3.01", "35000000"),
           _st(GER, "2026-06-30", 2, "2025-04-01", "2025-06-30", "3.01", "1", order="PENÚLTIMO"),
           _st(GER, "2026-06-30", 2, "2026-01-01", "2026-06-30", "3.99.01.01", "0.85"),
           _st(TRE, "2026-06-30", 1, "2026-04-01", "2026-06-30", "3.01", "40000"),
           _st(TRE, "2026-06-30", 1, "2026-01-01", "2026-06-30", "3.01", "80000"),
           _st(MRS, "2026-06-30", 1, "2026-01-01", "2026-06-30", "3.01", "4000000"),
           _st(MRS, "2026-06-30", 1, "2026-04-01", "2026-06-30", "3.01", "2000000"),
           _st(RAI, "2026-06-30", 1, "2026-04-01", "2026-06-30", "3.01", "51460000"),
           _st(OUT, "2026-06-30", 1, "2026-04-01", "2026-06-30", "3.01", "7"),
           _st(TRE, "2026-03-31", 1, "2026-01-01", "2026-03-31", "3.01", "9"),
           _st(MRS, "2026-03-31", 1, "2026-01-01", "2026-03-31", "3.01", "9")]
    dre_ind = [H_ST,
               _st(GER, "2026-06-30", 2, "2026-04-01", "2026-06-30", "3.01", "5000000", grp="DF Individual"),
               _st(GER, "2026-06-30", 2, "2026-04-01", "2026-06-30", "3.02", "-3000000", grp="DF Individual")]
    bpa = [H_ST, _st(GER, "2026-06-30", 2, "", "2026-06-30", "1", "90000000"),
           _st(RAI, "2026-06-30", 1, "", "2026-06-30", "1", "120000000")]
    return _zip({"itr_cia_aberta_2026.csv": "\n".join(idx), "itr_cia_aberta_DRE_con_2026.csv": "\n".join(dre),
                 "itr_cia_aberta_DRE_ind_2026.csv": "\n".join(dre_ind), "itr_cia_aberta_BPA_con_2026.csv": "\n".join(bpa)})


def dfp_2025() -> bytes:
    idx = [H_IDX, _co(GER, "2025-12-31", 1) + ";DFP;150001;2026-02-25;http://x/9",
           _co(RAI, "2026-03-31", 1) + ";DFP;150002;2026-06-29;http://x/10"]
    dre = [H_ST, _st(GER, "2025-12-31", 1, "2025-01-01", "2025-12-31", "3.01", "69860000"),
           _st(RAI, "2026-03-31", 1, "2025-04-01", "2026-03-31", "3.01", "225850000")]
    return _zip({"dfp_cia_aberta_2025.csv": "\n".join(idx), "dfp_cia_aberta_DRE_con_2025.csv": "\n".join(dre)})


def fca_2026() -> bytes:
    def vm(c, cls, tk):
        return f"{c[0]};2026-01-01;1;1;{c[2]};{cls};;;{tk};;Bolsa;;;;;Básico;;"
    vms = [H_VM, vm(GER, "Ações Ordinárias", "GGBR3"), vm(GER, "Ações Preferenciais", "GGBR4"),
           vm(TRE, "Ações Preferenciais", "8192"), vm(MRS, "Ações Preferenciais", "MRSA5"),
           vm(RAI, "Ações Preferenciais", "RAIZ4"), vm(OUT, "Ações Ordinárias", "OUTS3")]
    geral = [H_GERAL] + [f"{c[0]};2026-01-01;1;1;{c[2]};{c[1].zfill(6)};Indústria" for c in (GER, TRE, MRS, RAI, OUT)]
    return _zip({"fca_cia_aberta_valor_mobiliario_2026.csv": "\n".join(vms), "fca_cia_aberta_geral_2026.csv": "\n".join(geral)})


def cad_csv() -> bytes:
    rows = [H_CAD] + [f"{c[0]};{c[2]};{c[2]};2000-01-01;ATIVO;{c[1]};Indústria" for c in (GER, TRE, MRS, RAI, OUT)]
    return gzip.compress("\n".join(rows).encode("latin-1"))


# ------------------------------------------------------------------------------------------------ pure helpers


def test_ticker_normalization_and_share_classes():
    assert cvm.norm_b3_ticker("petr4") == "PETR4" and cvm.norm_b3_ticker("PETR4F") == "PETR4"
    assert cvm.norm_b3_ticker("MRSA5B") == "MRSA5" and cvm.norm_b3_ticker("GGBR4.SA") == "GGBR4"
    assert cvm.norm_b3_ticker("BPAC11") == "BPAC11" and cvm.norm_b3_ticker("B3SA3") == "B3SA3"
    for garbage in ("4030", "713854", "ADR", "1545-8", "000000", "", None):
        assert cvm.norm_b3_ticker(garbage) is None
    assert [cvm.share_class(t) for t in ("GGBR3", "GGBR4", "MRSA5B", "MRSA6", "BPAC11")] == ["ON", "PN", "PNA", "PNB", "UNIT"]
    assert cvm.ticker_root("ITUB4") == "ITUB"


@pytest.mark.parametrize("short,reg,kind", [
    ("SID NACIONAL", "CIA SIDERURGICA NACIONAL", "prefix"),
    ("NORDON MET", "NORDON INDUSTRIAS METALURGICAS S.A.", "prefix"),
    ("LOJAS MARISA", "MARISA LOJAS SA", "prefix"),
    ("CSNMINERACAO", "CSN MINERAÇÃO S.A.", "prefix"),
    ("BTGP BANCO", "BANCO BTG PACTUAL S/A", "prefix"),          # BTG + P(ACTUAL): adjacent tokens joined
    ("HAGA S/A", "HAGA S.A. INDUSTRIA E COMERCIO", "prefix"),   # one word == first registered word
    ("EQTL PARA", "EQUATORIAL PARÁ DISTRIBUIDORA DE ENERGIA S.A.", "abbrev"),
    ("SID NACIONAL", "SIDERURGICA RIOGRANDENSE", None),
    ("ABC", "ABC BRASIL", None),                               # too short to be evidence
    ("HAGA S/A", "INDUSTRIAS HAGA", None),
])
def test_name_match(short, reg, kind):
    assert cvm.name_match(cvm.name_tokens(short), cvm.name_tokens(reg)) == kind


def test_build_crosswalk_pn_lines_splits_and_ambiguity():
    listings = [{"ticker": "GGBR3", "issuer_id": "I_GER"}, {"ticker": "GGBR4", "issuer_id": "I_GER"},
                {"ticker": "LUXM4", "issuer_id": "I_TRE"},                     # PN-only issuer, no cvm_code
                {"ticker": "MRSA3B", "issuer_id": "I_MRS"}, {"ticker": "MRSA5B", "issuer_id": "I_MRS"},
                {"ticker": "RAIZ4", "issuer_id": "I_RAI"},                     # FCA root RAIZ -> 25917
                {"ticker": "GOAU4", "issuer_id": "I_SPLIT"},                   # PN line split from its ON issuer
                {"ticker": "ZZZZ3", "issuer_id": "I_NONE"},
                {"ticker": "EQPA3", "issuer_id": "I_EQPA"}]
    vm = pl.DataFrame({"cnpj": [cvm.cnpj_digits(c[0]) for c in (GER, TRE, MRS, RAI, GER)],
                       "ticker_raw": ["GGBR4", "8192", "MRSA5", "RAIZ3", "GOAU3"], "name": ["x"] * 5,
                       "fca_year": [2026] * 5, "ended": [False] * 5})
    vm = vm.with_columns(pl.col("ticker_raw").map_elements(cvm.norm_b3_ticker, return_dtype=pl.Utf8).alias("ticker"))
    vm = vm.with_columns(pl.col("ticker").str.slice(0, 4).alias("root"))
    geral = pl.DataFrame({"cnpj": [cvm.cnpj_digits(c[0]) for c in (GER, TRE, MRS, RAI)],
                          "cvm_code": [c[1] for c in (GER, TRE, MRS, RAI)], "name": [c[2] for c in (GER, TRE, MRS, RAI)],
                          "fca_year": [2026] * 4})
    cad = pl.DataFrame({"cvm_code": ["8192", "18309"], "cnpj": [cvm.cnpj_digits(TRE[0]), "0" * 14],
                        "name": ["TREVISA INVESTIMENTOS SA", "EQUATORIAL PARÁ DISTRIBUIDORA DE ENERGIA S.A."],
                        "name_trade": [None, None], "active": [True, True]})
    names = {"I_TRE": "TREVISA", "I_EQPA": "EQTL PARA", "I_NONE": "NOBODY HOLDING"}
    xw, st = cvm.build_crosswalk(listings, {"3980": "I_GER"}, vm, geral, cad, names)
    got = {r["issuer_id"]: r for r in xw.iter_rows(named=True)}
    assert got["I_GER"]["method"] == "identifier_map" and got["I_GER"]["share_classes"] == "ON,PN"
    assert got["I_TRE"]["cvm_code"] == "8192" and got["I_TRE"]["method"] == "name_prefix" and got["I_TRE"]["attached"]
    assert got["I_MRS"]["cvm_code"] == "17949" and got["I_MRS"]["method"] == "fca_ticker" and got["I_MRS"]["attached"]
    assert got["I_RAI"]["cvm_code"] == "25917" and got["I_RAI"]["method"] == "fca_root" and got["I_RAI"]["attached"]
    # GOAU's root resolves to GERDAU's code, owned by I_GER: never moved, reported as a split with an override line
    assert got["I_SPLIT"]["cvm_code"] == "3980" and not got["I_SPLIT"]["attached"]
    assert st["splits"][0]["owner_issuer_id"] == "I_GER" and st["splits"][0]["suggested_override"].startswith("link,cvm:3980,")
    assert got["I_EQPA"]["method"] == "name_abbrev" and not got["I_EQPA"]["attached"] and st["review"][0]["cvm_code"] == "18309"
    assert got["I_NONE"]["method"] == "unresolved" and got["I_NONE"]["cvm_code"] is None
    assert st["attached_codes"] == 4  # 3980 (map) + 8192 + 17949 + 25917
    # two unresolved issuers claiming the same registrant: neither attaches
    xw2, st2 = cvm.build_crosswalk([{"ticker": "MRSA3", "issuer_id": "A"}, {"ticker": "MRSA5", "issuer_id": "B"}], {},
                                   vm, geral, cad, {})
    assert not xw2["attached"].any() and st2["ambiguous"][0]["cvm_code"] == "17949"


# ------------------------------------------------------------------------------------------------ parsing


def test_parse_frames_pit_versions_consolidation_and_fiscal_labels():
    fil, facts, st = cvm.parse_statement_frames(itr_2026(), "ITR", 2026, {"3980", "8192", "17949", "25917"},
                                                issuer_of={"3980": "I_GER", "25917": "I_RAI"}, today=date(2026, 11, 7))
    assert st["dropped_kd_future"] == 1 and st["dropped_kd_before_period"] == 1
    assert "99999" not in set(fil["source_entity_id"]) and "cvm:160006" not in set(fil["filing_id"])
    f = {r["filing_id"]: r for r in fil.iter_rows(named=True)}
    # both versions are indexed; v1 points at its successor, v2 is an amendment of v1
    assert "superseded_by=cvm:160050" in f["cvm:160001"]["items"] and not f["cvm:160001"]["is_amendment"]
    assert f["cvm:160050"]["is_amendment"] and f["cvm:160050"]["amends_filing_id"] == "cvm:160001"
    assert f["cvm:160050"]["issuer_id"] == "I_GER" and f["cvm:160002"]["issuer_id"] is None
    ger = facts.filter(pl.col("source_entity_id") == "3980")
    # the statement files hold the latest version only: its values are dated at ITS receipt, never at v1's
    assert set(ger["filing_id"]) == {"cvm:160050"} and set(ger["knowledge_date"]) == {date(2026, 8, 19)}
    q2 = ger.filter((pl.col("concept") == "3.01") & (pl.col("period_start") == date(2026, 4, 1)))
    assert q2.height == 1 and q2["consolidated"][0] and q2["value"][0] == 18_000_000_000.0  # con beats ind
    assert ger.filter(pl.col("concept") == "3.02")["consolidated"].to_list() == [False]       # ind-only account kept
    assert ger.filter(pl.col("value") == 1000.0).height == 0                                     # PENÚLTIMO dropped
    eps = ger.filter(pl.col("concept") == "3.99.01.01")
    assert eps["value"][0] == pytest.approx(0.85) and eps["unit"][0] == "BRL/shares"
    assert set(ger.filter(pl.col("concept") == "3.01")["fiscal_period"]) == {"Q2", "H1"}
    assert f["cvm:160050"]["fiscal_period"] == "Q2" and f["cvm:160050"]["fiscal_year"] == 2026
    # Raízen (March year-end): Apr-Jun is fiscal Q1 of the fiscal year ending 2027-03-31
    rai = facts.filter(pl.col("source_entity_id") == "25917")
    assert set(rai.filter(pl.col("concept") == "3.01")["fiscal_period"]) == {"Q1"}
    assert set(rai["fiscal_year"]) == {2027} and f["cvm:160004"]["fiscal_period"] == "Q1"
    assert set(rai["issuer_id"]) == {"I_RAI"} and rai.filter(pl.col("concept") == "1")["period_type"][0] == "instant"
    assert (facts["knowledge_date"] >= facts["period_end"]).all() and not facts["knowledge_date_estimated"].any()
    # DFP: the March FY of Raízen gets fiscal_year 2026, Gerdau's calendar FY 2025
    fil2, facts2, _ = cvm.parse_statement_frames(dfp_2025(), "DFP", 2025, None)
    assert dict(zip(fil2["source_entity_id"], fil2["fiscal_year"], strict=True)) == {"3980": 2025, "25917": 2026}
    assert set(facts2["fiscal_period"]) == {"FY"}


def test_include_expression_limits_to_new_codes_or_recent_receipts():
    inc = cvm.CvmAdapter._include({"3980": "I_GER", "17949": None}, {"3980": "I_GER", "17949": "I_MRS"}, date(2026, 8, 15))
    fil, _, _ = cvm.parse_statement_frames(itr_2026(), "ITR", 2026, {"3980", "8192", "17949"}, include=inc,
                                           today=date(2026, 11, 7))
    # 3980 covered under the same issuer: only receipts >= since (v2); 17949 changed issuer: full; 8192 new: full
    assert set(fil["filing_id"]) == {"cvm:160050", "cvm:160002", "cvm:160003"}
    assert cvm.CvmAdapter._include({}, {}, date(2026, 1, 1)) is None and cvm.CvmAdapter._include({"1": None}, {}, None) is None
    assert cvm.CvmAdapter._todo({"1", "2"}, {"1": None}, {}) == ["2"] and cvm.CvmAdapter._todo(None, "*", {}) == ""


# ------------------------------------------------------------------------------------------------ adapter run


def _land(tmp_data) -> None:
    raw = tmp_data.raw_dir / "cvm"
    for ds, name, content in (("itr", "itr_cia_aberta_2026.zip", itr_2026()), ("dfp", "dfp_cia_aberta_2025.zip", dfp_2025()),
                              ("ref_fca", "fca_cia_aberta_2026.zip", fca_2026()),
                              ("ref_cadastro", "cad_cia_aberta.csv.gz", cad_csv())):
        d = raw / ds / "dt=2026-11-01"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(content)


def _reference(tmp_data, cvm_codes: dict[str, str]) -> None:
    now = datetime.now(UTC)
    base = {"listing_id": None, "valid_from": date(1900, 1, 1), "valid_to": None, "confidence": 1.0, "updated_at": now}
    rows = [{"id_type": "cvm_code", "id_value": c, "entity_level": "issuer", "issuer_id": i, "security_id": None,
             "source_id": "cvm", **base} for c, i in cvm_codes.items()]
    for tk, iid in (("GGBR3", "I_GER"), ("GGBR4", "I_GER"), ("LUXM4", "I_TRE"), ("MRSA5B", "I_MRS"), ("RAIZ4", "I_RAI")):
        rows.append({"id_type": "ticker_mic", "id_value": f"{tk}@BVMF", "entity_level": "listing", "issuer_id": iid,
                     "security_id": "S" + tk, "source_id": "ref_universe", **base})
    write_table(pl.DataFrame(rows), "identifier_map", mode="overwrite", settings=tmp_data)
    iss = pl.DataFrame({"issuer_id": ["I_GER", "I_TRE", "I_MRS", "I_RAI"], "name": ["GERDAU", "TREVISA", "MRS LOGIST", "RAIZEN"],
                        "country": ["BR"] * 4, "region": ["LATAM"] * 4, "is_active": [True] * 4, "updated_at": [now] * 4})
    write_table(iss, "issuers", mode="overwrite", settings=tmp_data)


def _adapter(tmp_data, **kw) -> cvm.CvmAdapter:
    return cvm.CvmAdapter(settings=tmp_data, offline=True, years="2025,2026", datasets="dfp,itr", **kw)


def test_run_universe_scope_attaches_issuers_and_is_incremental(tmp_data):
    _land(tmp_data)
    _reference(tmp_data, {"3980": "I_GER", "25917": "I_RAI"})
    a = _adapter(tmp_data)
    fetched, rows = a.run()
    assert fetched.status == "success", fetched.entities_failed
    # universe = identifier_map codes + crosswalk (TREVISA by name, MRS by its OTC 'B' ticker); OUTSIDER not listed
    assert fetched.meta["universe"] == ["17949", "25917", "3980", "8192"]
    assert fetched.meta["crosswalk"]["attached_codes"] == 4
    raw = read_table("fundamentals_raw", settings=tmp_data)
    fil = read_table("filings_index", settings=tmp_data)
    assert rows["fundamentals_raw"] == raw.height and "99999" not in set(raw["source_entity_id"])
    assert raw["issuer_id"].null_count() == 0 and fil["issuer_id"].null_count() == 0
    assert dict(raw.select("source_entity_id", "issuer_id").unique().iter_rows()) == {
        "3980": "I_GER", "25917": "I_RAI", "8192": "I_TRE", "17949": "I_MRS"}
    assert (tmp_data.raw_dir / "cvm" / "crosswalk.parquet").exists()
    st = load_state("cvm", tmp_data)
    assert set(st["normalized"]["itr:2026"]["codes"]) == {"3980", "25917", "8192", "17949"}
    assert st["normalized"]["itr:2026"]["codes"]["8192"] == "I_TRE"
    # unchanged zips, every code covered: both skipped, nothing rewritten
    f2, rows2 = _adapter(tmp_data).run(since="2026-11-01")
    assert rows2["fundamentals_raw"] == 0 and f2.meta["stats"]["itr:2026"]["skipped"]
    # a new reference code (OUTSIDER now listed) -> only that code gets its full history; covered codes since-filtered
    _reference(tmp_data, {"3980": "I_GER", "25917": "I_RAI", "99999": "I_OUT"})
    f3, rows3 = _adapter(tmp_data).run(since="2026-11-01")
    new = read_table("fundamentals_raw", settings=tmp_data)
    assert "99999" in set(new["source_entity_id"]) and f3.meta["stats"]["itr:2026"]["codes"] == 1
    assert new.filter(pl.col("source_entity_id") == "99999")["issuer_id"].to_list() == ["I_OUT"]
    assert new.height == raw.height + 1
    # dry run: plan only
    f4, rows4 = _adapter(tmp_data, dry_run=True, renormalize=True).run()
    assert f4.meta["dry_run"] and sum(rows4.values()) == 0 and len(f4.meta["plan"]) == 2


def test_seed_scope_and_explicit_entities(tmp_data):
    _land(tmp_data)
    _reference(tmp_data, {"3980": "I_GER", "25917": "I_RAI"})
    # explicit tickers: the PN-only TREVISA line resolves through the crosswalk, GGBR4 through the reference
    a = _adapter(tmp_data)
    assert a._universe(["LUXM4", "GGBR4.SA", "17949"]) == {"8192", "3980", "17949"}
    # seed scope keeps the pre-B3 behaviour: no seed file -> every registrant of the zips, no crosswalk
    b = _adapter(tmp_data, scope="seed")
    assert b._universe(None) is None and b.issuer_of() == {"3980": "I_GER", "25917": "I_RAI"}
    assert _adapter(tmp_data, all_listed=False).scope == "seed" and _adapter(tmp_data, all_listed=True).scope == "universe"
    with pytest.raises(ValueError):
        _adapter(tmp_data, scope="everything")


def test_universe_failure_never_widens_to_every_registrant(tmp_data, monkeypatch):
    _land(tmp_data)
    a = _adapter(tmp_data)
    monkeypatch.setattr(a, "_universe", lambda entities: (_ for _ in ()).throw(RuntimeError("reference broken")))
    fetched, rows = a.run()
    assert fetched.status == "failed" and fetched.meta["files"] == [] and sum(rows.values()) == 0


def test_job_and_coverage_contract_entries(tmp_data):
    import yaml

    from app.core.config import get_settings

    cdir = get_settings().contracts_dir
    jobs = {j["id"]: j for j in yaml.safe_load((cdir / "jobs.yaml").read_text(encoding="utf-8"))["jobs"]}
    b = jobs["cvm_backfill"]
    assert b["module"] == jobs["cvm_filings"]["module"] == "app.ingestion.cvm" and b["dag"] == "manual"
    assert b["params"]["scope"] == "universe" and b["params"]["offline"] and b["timeout_min"] > b["params"]["max_minutes"]
    assert b["executor"] == "process" and b["sources"] == ["cvm"]
    # run_adapter_job drops ALL params when the constructor raises TypeError: the job's params must construct it
    a = cvm.CvmAdapter(settings=tmp_data, **b["params"])
    assert a.offline and a.scope == "universe" and len(a.years) == 8 and a.datasets == ["dfp", "itr", "vlmo"]
    assert cvm.CvmAdapter(settings=tmp_data).scope == "universe"  # weekly cvm_filings (no params): universe too
    cov = yaml.safe_load((cdir / "coverage.yaml").read_text(encoding="utf-8"))
    ds = {d["id"]: d for d in cov["datasets"]}["filings.br_cvm"]
    assert ds["parts"] == {"source_id": ["cvm"]} and ds["live_job"] == "cvm_filings"
    assert any(g["id"] == "seed:b3:cvm_mapping" and g["dataset"] == "filings.br_cvm" for g in cov["known_gaps"])


def test_normalize_compat_path_and_vlmo_issuer_fill(tmp_data):
    """normalize() (all landed files at once, no state) still works for callers of the SourceAdapter interface; VLMO
    insider rows get the issuer of their CVM code."""
    _land(tmp_data)
    _reference(tmp_data, {"3980": "I_GER", "25917": "I_RAI"})
    vidx = ("CNPJ_Companhia;Nome_Companhia;Data_Referencia;Versao;Codigo_CVM;Categoria;Tipo;Data_Entrega;Tipo_Apresentacao;"
            f"Motivo_Reapresentacao;Protocolo_Entrega;Link_Download\n{GER[0]};GERDAU;2026-08-01;1;3980;VM;Posição;2026-09-09;AP;;p;x")
    vcon = ("CNPJ_Companhia;Nome_Companhia;Data_Referencia;Versao;Tipo_Empresa;Empresa;Tipo_Cargo;Tipo_Movimentacao;"
            "Descricao_Movimentacao;Tipo_Operacao;Tipo_Ativo;Caracteristica_Valor_Mobiliario;Intermediario;Data_Movimentacao;"
            f"Quantidade;Preco_Unitario;Volume\n{GER[0]};GERDAU;2026-08-01;1;Companhia;GERDAU;Diretor ou Vinculado;"
            "Compra à vista;;Crédito;Ações;PN;;2026-08-14;1000;20.5;20500")
    d = tmp_data.raw_dir / "cvm" / "vlmo" / "dt=2026-11-01"
    d.mkdir(parents=True, exist_ok=True)
    (d / "vlmo_cia_aberta_2026.zip").write_bytes(_zip({"vlmo_cia_aberta_2026.csv": vidx, "vlmo_cia_aberta_con_2026.csv": vcon}))
    a = cvm.CvmAdapter(settings=tmp_data, offline=True, years="2026", datasets="itr,vlmo")
    f = a.fetch(entities=["GGBR4"])
    t = a.normalize(f)
    assert set(t["fundamentals_raw"]["source_entity_id"]) == {"3980"} and set(t["fundamentals_raw"]["issuer_id"]) == {"I_GER"}
    ins = t["insider_transactions"]
    assert ins.height == 1 and ins["issuer_id"][0] == "I_GER" and ins["knowledge_date"][0] == date(2026, 9, 9)
