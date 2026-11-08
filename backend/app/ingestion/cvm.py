"""Brazil CVM open data -> filings_index + fundamentals_raw (DFP annual / ITR quarterly, 2010+) and
insider_transactions (VLMO monthly consolidated insider positions/trades, by role).

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §8.1 (verified live 2026-10-10, no key).
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{DFP,ITR}/DADOS/{dfp,itr}_cia_aberta_<YYYY>.zip
      index  <x>_cia_aberta_<Y>.csv : CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;CATEG_DOC;ID_DOC;DT_RECEB;LINK_DOC
      stmts  <x>_cia_aberta_{BPA,BPP,DRE,DFC_MD,DFC_MI}_{con,ind}_<Y>.csv :
             ...;MOEDA;ESCALA_MOEDA(MIL|UNIDADE);ORDEM_EXERC(ÚLTIMO|PENÚLTIMO);DT_INI_EXERC;DT_FIM_EXERC;CD_CONTA;DS_CONTA;VL_CONTA
  .../DOC/FCA/DADOS/fca_cia_aberta_<Y>.zip  -> valor_mobiliario (CNPJ <-> B3 ticker), geral (Codigo_CVM, FY end month)
  .../CAD/DADOS/cad_cia_aberta.csv          -> every registrant: CD_CVM, CNPJ, DENOM_SOCIAL / DENOM_COMERC, SIT
  .../DOC/VLMO/DADOS/vlmo_cia_aberta_<Y>.zip -> index (Codigo_CVM, Data_Entrega) + _con (Tipo_Cargo, Tipo_Movimentacao,
      Data_Movimentacao, Quantidade, Preco_Unitario) — aggregated BY ROLE, not by named person.
  Files are ';' separated, Latin-1. Yearly zips are refreshed daily (cache 6 h; weekly job).
PIT: knowledge_date = DT_RECEB of the document VERSION the values come from (DFP/ITR) / Data_Entrega (VLMO). The
statement files carry ONLY the latest version of each document (verified 2026-11-07 on itr_cia_aberta_2026.zip: a
(CD_CVM, DT_REFER) with versions 1 and 2 in the index has statement rows for VERSAO 2 only), so restated values are
dated at the restatement's receipt and never earlier; superseded versions stay in filings_index (items
'superseded_by=<filing_id>', amends_filing_id -> version 1) and facts captured from them by earlier runs are kept.
Index rows with a receipt date before the reference date or in the future are dropped (counted in stats).
Concepts: CD_CONTA codes ('3.01', '1.01.01') with DS_CONTA as label, taxonomy 'cvm_account'; values scaled by
ESCALA_MOEDA (MIL x1000; EPS lines 3.99.* are per share and never scaled). Only ORDEM_EXERC='ÚLTIMO' rows are kept
(the prior-year column is re-reported by the prior filing). Consolidated rows win over individual (parent-only) rows of
the same filing / account / period (the PK has no consolidated column). Accounting standard BR-IFRS (CPC); banks use a
different chart of accounts (codes are still kept raw). source_entity_id = CVM code without leading zeros
(normalize_id rule).

COVERAGE_V3 B3 (2026-11): scope and issuer mapping
  scope='universe' (default): every cvm_code of the reference (identifier_map id_type cvm_code, open validity,
      confidence >= 0.9) + the CVM codes resolved by the B3 crosswalk below + the mvp_global BR seed. Falls back to
      the seed when no reference exists. scope='seed': the BR seed only (pre-B3 behaviour); scope='all': every
      registrant in the zips (issuer_id stays null for unlisted registrants). Explicit entities override.
  crosswalk (raw/cvm/crosswalk.parquet): every BVMF listing of the reference (identifier_map ticker_mic SYM@BVMF ->
      issuer). B3 tickers share a 4-character company root across share classes (ON 3, PN 4, PNA 5, PNB 6, PNC 7, PND 8,
      UNIT 11; 'F' fractional and 'B' OTC suffixes dropped), so preferred / unit lines resolve to the registrant of
      their ON line: issuer's own cvm_code (identifier_map, 1.0) > exact ticker in FCA valor_mobiliario (0.95) > ticker
      root in FCA (0.92) > unique name match against active CVM registrants (every token of the B3 short name is a
      prefix of a distinct token of the registered / trade name: 0.90; consonant abbreviation such as 'EQTL PARA':
      0.85, reported only). Only matches >= 0.9 attach (issuer_id is filled at ingest; identity_resolve keeps it). A
      code already owned by another issuer is never re-assigned: the PN-only issuer is reported as a split
      (meta crosswalk.splits, with a suggested manual_overrides 'link' row) and the facts stay on the code's owner.
  Statements therefore attach at ISSUER level: an issuer whose primary line is the PN gets the registrant's facts.
Incremental: per (dataset, year) zip the state keeps the codes already normalized (with the issuer they were
written under); new codes (or codes whose issuer changed) get their full history from the zip, covered codes only rows
received on / after `since` (the job watermark). Unchanged zips with every code covered are skipped. One zip at a time
is parsed (Polars, selected columns) and written, so a backfill stays below ~1.5 GB; the run stops partial (resumable)
when free RAM < min_free_gb or after max_minutes.
Params: years ('2025,2026'; default last + current), backfill_years (N -> the last N years), datasets ('dfp,itr,vlmo'),
scope ('universe' | 'seed' | 'all'), offline (landed zips only, no downloads; raw_from = an alternative raw/cvm
root to read them from), renormalize, max_minutes, min_free_gb (1.5), dry_run (plan only).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import re
import time
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, insider_row, load_state, parse_num, run_adapter_job,
                                          safe_call, save_state, seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/"
STATEMENTS = ("BPA", "BPP", "DRE", "DFC_MD", "DFC_MI")
SCOPES = ("universe", "seed", "all")
IDX_COLS = ("CNPJ_CIA", "DT_REFER", "VERSAO", "DENOM_CIA", "CD_CVM", "ID_DOC", "DT_RECEB", "LINK_DOC")
ST_COLS = ("CD_CVM", "DT_REFER", "VERSAO", "MOEDA", "ESCALA_MOEDA", "ORDEM_EXERC", "DT_INI_EXERC", "DT_FIM_EXERC",
           "CD_CONTA", "DS_CONTA", "VL_CONTA")
#: B3 share-class suffix of a ticker (company root + suffix)
CLASS_OF_SUFFIX = {"3": "ON", "4": "PN", "5": "PNA", "6": "PNB", "7": "PNC", "8": "PND", "11": "UNIT"}
#: crosswalk confidence per method; >= ATTACH_CONFIDENCE attaches the issuer at ingest
XW_CONFIDENCE = {"identifier_map": 1.0, "fca_ticker": 0.95, "fca_root": 0.92, "name_prefix": 0.90, "name_abbrev": 0.85}
ATTACH_CONFIDENCE = 0.9
#: incremental runs re-read documents received up to this many days before the watermark (zip refresh lag)
SINCE_OVERLAP_DAYS = 31
XW_SCHEMA = {"issuer_id": pl.Utf8, "cvm_code": pl.Utf8, "cnpj": pl.Utf8, "tickers": pl.Utf8, "share_classes": pl.Utf8,
             "method": pl.Utf8, "confidence": pl.Float64, "attached": pl.Boolean, "note": pl.Utf8,
             "built_at": pl.Datetime("us", "UTC")}


def _csv(z: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    if name not in z.namelist():
        return []
    return list(csv.DictReader(io.StringIO(z.read(name).decode("latin-1")), delimiter=";"))


def cvm_code(v: Any) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    return str(int(d)) if d else None


def cnpj_digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or "")).zfill(14)


# --------------------------------------------------------------------------------------------- B3 tickers / names

_TICKER = re.compile(r"([A-Z][A-Z0-9]{3})(\d{1,2})[FB]?")


def norm_b3_ticker(sym: Any) -> str | None:
    """'petr4' / 'PETR4F' / 'MRSA5B' / 'PETR4.SA' -> 'PETR4', 'MRSA5'; None for non-ticker text ('4030', 'ADR')."""
    s = re.sub(r"\s+", "", str(sym or "")).upper().split(".")[0]
    m = _TICKER.fullmatch(s)
    return f"{m.group(1)}{m.group(2)}" if m else None


def ticker_root(sym: Any) -> str | None:
    t = norm_b3_ticker(sym)
    return t[:4] if t else None


def share_class(sym: Any) -> str | None:
    t = norm_b3_ticker(sym)
    return CLASS_OF_SUFFIX.get(t[4:]) if t else None


_NAME_STOP = {"S", "A", "SA", "CIA", "COMPANHIA", "CO", "LTDA", "DE", "DA", "DO", "DAS", "DOS", "E", "EM", "RJ",
              "RECUPERACAO", "JUDICIAL", "ON", "PN", "NM", "N1", "N2", "MA", "MB", "INC", "LTD", "THE"}


def name_tokens(s: Any) -> list[str]:
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper()
    return [x for x in re.split(r"[^A-Z0-9]+", t) if x and x not in _NAME_STOP]


def _is_subseq(t: str, word: str) -> bool:
    it = iter(word)
    return all(ch in it for ch in t)


def name_match(short: list[str], cand: list[str]) -> str | None:
    """'prefix' when every B3 short-name token is a prefix of a distinct registered-name token or of two adjacent
    tokens joined ('BTGP' ~ 'BTG PACTUAL'), in any order, or the joined short name is a prefix of the joined registered
    name ('CSNMINERACAO'), or a one-word short name equals the registered name's first word ('HAGA');
    'abbrev' when tokens are only same-initial subsequences ('EQTL' ~ 'EQUATORIAL'); else None."""
    if not short or not cand:
        return None
    if len(short) == 1 and len(short[0]) >= 4 and cand[0] == short[0]:
        return "prefix"
    if sum(len(t) for t in short) < 5 or max(len(t) for t in short) < 4:
        return None
    js, jc = "".join(short), "".join(cand)
    pairs = cand + [a + b for a, b in zip(cand, cand[1:])]

    def assign(test) -> bool:
        used: set[int] = set()
        for t in sorted(short, key=len, reverse=True):
            hit = next((i for i, c in enumerate(pairs) if i not in used and test(t, c)), None)
            if hit is None:
                return False
            used.add(hit)
        return True

    if assign(lambda t, c: c.startswith(t)) or (len(js) >= 8 and jc.startswith(js)):
        return "prefix"
    if assign(lambda t, c: c.startswith(t) or (len(t) >= 3 and c[:1] == t[:1] and _is_subseq(t, c))):
        return "abbrev"
    return None


# --------------------------------------------------------------------------------------------- zip parsing (Polars)


def _open_zip(content: bytes | str | Path) -> zipfile.ZipFile:
    if isinstance(content, (bytes, bytearray)):
        return zipfile.ZipFile(io.BytesIO(content))
    return zipfile.ZipFile(Path(content))


def _read_member(z: zipfile.ZipFile, name: str, columns: Sequence[str] | None = None) -> pl.DataFrame | None:
    """One ';' Latin-1 CSV member -> all-Utf8 frame of `columns` (missing ones null); None if the member is absent."""
    if name not in z.namelist():
        return None
    text = z.read(name).decode("latin-1")
    if text.startswith("﻿"):
        text = text[1:]
    elif text.startswith("ï»¿"):  # UTF-8 BOM read as Latin-1
        text = text[3:]
    header = [h.strip().strip('"') for h in text[:text.find("\n") if "\n" in text else len(text)].strip().split(";")]
    want = [c for c in (columns or header) if c in header]
    if not text.strip() or not want:
        out = pl.DataFrame(schema={c: pl.Utf8 for c in (columns or header)})
        return out
    data = text.encode("utf-8")
    del text
    try:
        df = pl.read_csv(data, separator=";", infer_schema_length=0, quote_char='"', columns=want,
                         truncate_ragged_lines=True)
    except Exception:  # noqa: BLE001 - malformed quoting: the csv module is lenient
        rows = [{k: v for k, v in r.items() if k in want} for r in csv.DictReader(io.StringIO(data.decode("utf-8")),
                                                                                    delimiter=";")]
        df = pl.DataFrame(rows, schema={c: pl.Utf8 for c in want}) if rows else pl.DataFrame(
            schema={c: pl.Utf8 for c in want})
    del data
    missing = [c for c in (columns or ()) if c not in df.columns]
    if missing:
        df = df.with_columns([pl.lit(None, dtype=pl.Utf8).alias(c) for c in missing])
    return df


def _code_expr(col: str = "CD_CVM") -> pl.Expr:
    d = pl.col(col).cast(pl.Utf8).str.replace_all(r"\D", "")
    return pl.when(d.str.len_chars() > 0).then(d.cast(pl.Int64, strict=False).cast(pl.Utf8)).otherwise(None)


def _date_expr(col: str) -> pl.Expr:
    s = pl.col(col).cast(pl.Utf8).str.strip_chars()
    return pl.coalesce(s.str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False),
                       s.str.slice(0, 10).str.to_date("%d/%m/%Y", strict=False))


def _issuer_expr(issuer_of: dict[str, str] | None, col: str = "code") -> pl.Expr:
    if not issuer_of:
        return pl.lit(None, dtype=pl.Utf8)
    return pl.col(col).replace_strict(issuer_of, default=None, return_dtype=pl.Utf8)


def _empty(table: str) -> pl.DataFrame:
    from app.core.schema import get_contract

    return pl.DataFrame(schema=get_contract(table).polars_schema())


def parse_statement_frames(content: bytes | str | Path, kind: str, year: int, universe: set[str] | None,
                           source_id: str = "cvm", issuer_of: dict[str, str] | None = None, today: date | None = None,
                           include: pl.Expr | None = None) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:
    """DFP/ITR yearly zip -> (filings_index frame, fundamentals_raw frame, stats) for CVM codes in `universe`
    (None = all). `issuer_of` (cvm code -> issuer_id) fills issuer_id; `include` (an expression over the index columns
    code / kd) keeps only some documents (incremental runs)."""
    kind = kind.upper()
    pre = f"{kind.lower()}_cia_aberta"
    stats: dict[str, Any] = {"index_rows": 0, "docs": 0, "codes": 0, "facts": 0, "codes_with_facts": 0,
                             "dropped_no_kd": 0, "dropped_kd_future": 0, "dropped_kd_before_period": 0}
    now = datetime.now(UTC)
    lim = (today or now.date()) + timedelta(days=1)
    with _open_zip(content) as z:
        idx = _read_member(z, f"{pre}_{year}.csv", IDX_COLS)
        if idx is None or idx.is_empty():
            return _empty("filings_index"), _empty("fundamentals_raw"), stats
        if idx.get_column("DT_RECEB").null_count() == idx.height:
            raise ValueError(f"CVM {kind} index schema changed: no DT_RECEB")
        idx = idx.with_columns(_code_expr().alias("code"), pl.col("DT_REFER").str.strip_chars().alias("ref"),
                               pl.col("VERSAO").str.strip_chars().alias("ver_s"),
                               pl.col("ID_DOC").str.strip_chars().alias("doc"),
                               _date_expr("DT_RECEB").alias("kd"), _date_expr("DT_REFER").alias("pe"))
        idx = idx.with_columns(pl.col("ver_s").cast(pl.Int32, strict=False).fill_null(1).alias("ver"))
        idx = idx.filter(pl.col("code").is_not_null() & pl.col("doc").is_not_null() & (pl.col("doc") != ""))
        if universe is not None:
            idx = idx.filter(pl.col("code").is_in(sorted(universe)))
        stats["index_rows"] = idx.height
        no_kd = pl.col("kd").is_null()
        fut = pl.col("kd") > lim
        before = pl.col("pe").is_not_null() & (pl.col("kd") < pl.col("pe"))
        cnt = idx.select(no_kd.sum().alias("a"), fut.sum().alias("b"), before.sum().alias("c")).row(0)
        stats.update(dropped_no_kd=int(cnt[0] or 0), dropped_kd_future=int(cnt[1] or 0),
                     dropped_kd_before_period=int(cnt[2] or 0))
        idx = idx.filter(~(no_kd | fut | before).fill_null(True))
        idx = idx.unique(subset=["code", "ref", "ver_s"], keep="last", maintain_order=True)
        vers = idx.group_by("code", "ref").agg(
            pl.col("doc").sort_by("ver").first().alias("_v0doc"), pl.col("ver").min().alias("_v0"),
            pl.col("doc").sort_by("ver").last().alias("_vlast"), pl.col("ver").max().alias("_vmax"))
        idx = idx.join(vers, on=["code", "ref"], how="left")
        if include is not None:
            idx = idx.filter(include)
        stats["docs"] = idx.height
        stats["codes"] = idx.get_column("code").n_unique() if idx.height else 0
        if idx.is_empty():
            return _empty("filings_index"), _empty("fundamentals_raw"), stats
        fid = pl.lit(f"{source_id}:") + pl.col("doc")
        fp = (pl.lit("FY") if kind == "DFP" else
              pl.col("pe").dt.month().replace_strict({3: "Q1", 6: "Q2", 9: "Q3"}, default=None, return_dtype=pl.Utf8))
        sup = pl.col("ver") < pl.col("_vmax")
        fil = idx.select(
            fid.alias("filing_id"), pl.lit(source_id).alias("source_id"), pl.col("kd").dt.year().alias("year"),
            pl.col("code").alias("source_entity_id"), _issuer_expr(issuer_of).alias("issuer_id"),
            pl.lit(kind).alias("form_type"), pl.lit("annual" if kind == "DFP" else "quarterly").alias("form_class"),
            pl.col("pe").alias("period_end"), pl.col("pe").dt.year().alias("fiscal_year"), fp.alias("fiscal_period"),
            pl.col("kd").alias("knowledge_date"), pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("knowledge_ts"),
            (pl.col("ver") > 1).alias("is_amendment"),
            pl.when((pl.col("ver") > 1) & (pl.col("_v0doc") != pl.col("doc")))
            .then(pl.lit(f"{source_id}:") + pl.col("_v0doc")).otherwise(None).alias("amends_filing_id"),
            pl.lit("pt").alias("language"), pl.col("LINK_DOC").alias("url"), pl.lit(None, dtype=pl.Utf8).alias("doc_path"),
            pl.lit(True).alias("has_xbrl"),
            pl.concat_list([pl.lit("accounting_standard=BR-IFRS"), pl.lit("versao=") + pl.col("ver").cast(pl.Utf8),
                            pl.when(sup).then(pl.lit(f"superseded_by={source_id}:") + pl.col("_vlast"))
                            .otherwise(pl.lit(None, dtype=pl.Utf8))]).list.drop_nulls().alias("items"),
            pl.lit(now).alias("ingested_at"))
        fil = fil.unique(subset=["filing_id"], keep="first", maintain_order=True)
        docs = idx.select("code", "ref", "ver_s", "doc", "kd")
        codes = sorted(set(docs.get_column("code").to_list()))
        parts: list[pl.DataFrame] = []
        for scope in ("con", "ind"):  # consolidated first: it wins the PK dedupe below
            for st in STATEMENTS:
                df = _read_member(z, f"{pre}_{st}_{scope}_{year}.csv", ST_COLS)
                if df is None or df.is_empty():
                    continue
                df = (df.with_columns(_code_expr().alias("code"))
                      .filter(pl.col("code").is_in(codes)
                              & pl.col("ORDEM_EXERC").str.strip_chars().str.to_uppercase().is_in(["ÚLTIMO", "ULTIMO"]))
                      .with_columns(pl.col("DT_REFER").str.strip_chars().alias("ref"),
                                    pl.col("VERSAO").str.strip_chars().alias("ver_s"))
                      .join(docs, on=["code", "ref", "ver_s"], how="inner"))
                if df.height:
                    parts.append(_facts(df, st, scope, kind, source_id, issuer_of, now))
                del df
    if not parts:
        fil, _ = _fiscal_labels(fil, None, kind)
        return _conform(fil, "filings_index"), _empty("fundamentals_raw"), stats
    from app.core.schema import get_contract

    pk = list(get_contract("fundamentals_raw").primary_key)
    facts = pl.concat(parts, how="vertical_relaxed").unique(subset=pk, keep="first", maintain_order=True)
    del parts
    fil, facts = _fiscal_labels(fil, facts, kind)
    stats["facts"] = facts.height
    stats["codes_with_facts"] = facts.get_column("source_entity_id").n_unique()
    stats["non_december_fy_codes"] = sorted(set(fil.filter(pl.col("_fye") != 12).get_column("source_entity_id").to_list())) \
        if "_fye" in fil.columns else []
    return _conform(fil.drop("_fye", strict=False), "filings_index"), _conform(facts, "fundamentals_raw"), stats


def _fiscal_labels(fil: pl.DataFrame, facts: pl.DataFrame | None, kind: str) -> tuple[pl.DataFrame, pl.DataFrame | None]:
    """Fiscal year / period labels from the fiscal-year start (the DT_INI_EXERC of the YTD / FY rows: the YTD start
    in an ITR, the FY start in a DFP), so March / June year-ends (Raízen, São Martinho) get Q1 = Apr-Jun and
    fiscal_year = the calendar year the fiscal year ENDS in (ARCHITECTURE §8). Precedence: the document's own YTD / FY
    rows > the company's most frequent month from the other documents of the zip > the document's earliest start
    (a Q1 ITR has 3-month rows only) > December. Calendar-year filers are unchanged."""
    fye_doc = fye_code = None
    if facts is not None and facts.height:
        dur = facts.filter(pl.col("period_start").is_not_null()).with_columns(
            ((pl.col("period_end") - pl.col("period_start")).dt.total_days() > 100).alias("_long"))
        fye_doc = dur.group_by("filing_id", "source_entity_id").agg(
            (pl.col("period_start").filter(pl.col("_long")).min() - timedelta(days=1)).dt.month().alias("_fye_y"),
            (pl.col("period_start").min() - timedelta(days=1)).dt.month().alias("_fye_a"))
        fye_code = (fye_doc.filter(pl.col("_fye_y").is_not_null()).group_by("source_entity_id")
                    .agg(pl.col("_fye_y").mode().sort().last().alias("_fye_c")))
        fye_doc = fye_doc.drop("source_entity_id")

    def attach(df: pl.DataFrame) -> pl.DataFrame:
        if fye_doc is None:
            return df.with_columns(pl.lit(12, dtype=pl.Int8).alias("_fye"))
        return (df.join(fye_doc, on="filing_id", how="left").join(fye_code, on="source_entity_id", how="left")
                .with_columns(pl.coalesce("_fye_y", "_fye_c", "_fye_a", pl.lit(12)).cast(pl.Int8).alias("_fye"))
                .drop("_fye_y", "_fye_a", "_fye_c"))

    def fy_of(col: str) -> pl.Expr:
        return pl.when(pl.col(col).dt.month() <= pl.col("_fye")).then(pl.col(col).dt.year()).otherwise(
            pl.col(col).dt.year() + 1)

    def qn(col: str) -> pl.Expr:  # fiscal quarter (1..4) whose last month is col's month
        return ((pl.col(col).dt.month().cast(pl.Int16) - pl.col("_fye").cast(pl.Int16) - 1) % 12) // 3 + 1

    fil = attach(fil)
    fil = fil.with_columns(fy_of("period_end").alias("fiscal_year"),
                           (pl.lit("FY") if kind == "DFP" else pl.when(qn("period_end") <= 3).then(
                               pl.lit("Q") + qn("period_end").cast(pl.Utf8)).otherwise(None)).alias("fiscal_period"))
    if facts is None:
        return fil, None
    facts = attach(facts)
    days = (pl.col("period_end") - pl.col("period_start")).dt.total_days()
    n = qn("period_end")
    fp = (pl.when(pl.col("period_start").is_null()).then(pl.lit(None, dtype=pl.Utf8))
          .when(days > 340).then(pl.lit("FY"))
          .when(days < 100).then(pl.lit("Q") + n.cast(pl.Utf8))
          .when(n == 2).then(pl.lit("H1")).when(n == 3).then(pl.lit("9M")).otherwise(pl.lit("YTD")))
    facts = facts.with_columns(fy_of("period_end").alias("fiscal_year"), fp.alias("fiscal_period")).drop("_fye")
    return fil, facts


def _conform(df: pl.DataFrame, table: str) -> pl.DataFrame:
    from app.core.schema import conform

    return conform(df, table)


def _facts(df: pl.DataFrame, st: str, scope: str, kind: str, source_id: str, issuer_of: dict[str, str] | None,
           now: datetime) -> pl.DataFrame:
    acct = pl.col("CD_CONTA").str.strip_chars()
    per_share = acct.str.starts_with("3.99")
    val = pl.col("VL_CONTA").str.strip_chars().str.replace_all(",", "").cast(pl.Float64, strict=False)
    esc = pl.col("ESCALA_MOEDA").fill_null("").str.strip_chars().str.to_uppercase()
    moeda = pl.col("MOEDA").fill_null("").str.strip_chars().str.to_uppercase()
    ccy = pl.when(moeda.is_in(["REAL", ""])).then(pl.lit("BRL")).otherwise(moeda.str.slice(0, 3))
    start = pl.lit(None, dtype=pl.Date) if st in ("BPA", "BPP") else _date_expr("DT_INI_EXERC")
    df = df.with_columns(start.alias("_s"), _date_expr("DT_FIM_EXERC").alias("_e"), per_share.alias("_ps"),
                         pl.when((esc == "MIL") & ~per_share).then(val * 1000.0).otherwise(val).alias("_v"),
                         pl.when(per_share).then(ccy + "/shares").otherwise(ccy).alias("_u"), acct.alias("_c"))
    df = df.filter(pl.col("_e").is_not_null() & pl.col("_v").is_not_null() & (pl.col("_c") != ""))
    fp = pl.lit(None, dtype=pl.Utf8)  # fiscal labels: _fiscal_labels (needs the document's fiscal-year start)
    return df.select(
        pl.lit(source_id).alias("source_id"), pl.col("_e").dt.year().alias("fiscal_year"),
        pl.col("code").alias("source_entity_id"), _issuer_expr(issuer_of).alias("issuer_id"),
        (pl.lit(f"{source_id}:") + pl.col("doc")).alias("filing_id"), pl.lit(kind).alias("form_type"),
        pl.lit("cvm_account").alias("taxonomy"), pl.col("_c").alias("concept"), pl.col("DS_CONTA").alias("label"),
        pl.when(pl.col("_s").is_null()).then(pl.lit("instant")).otherwise(pl.lit("duration")).alias("period_type"),
        pl.col("_s").alias("period_start"), pl.col("_e").alias("period_end"), fp.alias("fiscal_period"),
        pl.col("_v").alias("value"), pl.col("_u").alias("unit"), pl.lit(None, dtype=pl.Int16).alias("decimals"),
        pl.lit("").alias("dimensions_hash"), pl.lit(None, dtype=pl.Utf8).alias("dimensions_json"),
        pl.lit(scope == "con").alias("consolidated"), pl.col("kd").alias("knowledge_date"),
        pl.lit(False).alias("knowledge_date_estimated"), pl.lit(now).alias("ingested_at"))


def parse_statement_zip(content: bytes, kind: str, year: int, universe: set[str] | None,
                        source_id: str = "cvm") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """DFP/ITR yearly zip -> (filings rows, fact rows) for CVM codes in `universe` (None = all). Row-dict wrapper of
    parse_statement_frames (kept for callers / tests of the pre-B3 API)."""
    fil, facts, _ = parse_statement_frames(content, kind, year, universe, source_id)
    return fil.to_dicts(), facts.to_dicts()


VLMO_TYPES = [(r"^compra", "buy"), (r"^venda", "sell"), (r"subscri", "buy"), (r"doa[cç][aã]o \(doador\)", "gift"),
              (r"doa[cç][aã]o|heran", "gift"), (r"exerc[ií]cio de op|op[cç][aã]o de compra", "option_exercise"),
              (r"plano de remunera|recebimento de op", "grant"), (r"desdobramento|grupamento|bonifica", "other")]


def parse_vlmo_zip(content: bytes, year: int, universe: set[str] | None,
                   source_id: str = "cvm") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    z = zipfile.ZipFile(io.BytesIO(content)) if isinstance(content, (bytes, bytearray)) else zipfile.ZipFile(content)
    index = _csv(z, f"vlmo_cia_aberta_{year}.csv")
    entrega: dict[tuple[str, str, str], dict[str, str]] = {}
    for r in index:
        entrega[(cnpj_digits(r["CNPJ_Companhia"]), r["Data_Referencia"], r["Versao"])] = r
    filings: dict[str, dict[str, Any]] = {}
    ins = []
    line: dict[str, int] = {}
    for r in _csv(z, f"vlmo_cia_aberta_con_{year}.csv"):
        if r.get("Tipo_Empresa") != "Companhia":
            continue
        mov = (r.get("Tipo_Movimentacao") or "").strip()
        if mov.lower().startswith(("saldo", "posse", "desligamento")) or "empréstimo" in mov.lower():
            continue
        idx = entrega.get((cnpj_digits(r["CNPJ_Companhia"]), r["Data_Referencia"], r["Versao"]))
        if idx is None:
            continue
        code = cvm_code(idx.get("Codigo_CVM"))
        if code is None or (universe is not None and code not in universe):
            continue
        qty = parse_num(r.get("Quantidade"))
        if not qty:
            continue
        kd = to_date(idx.get("Data_Entrega"))
        ref = to_date(r["Data_Referencia"])
        if kd is None and ref is not None:
            kd = (ref.replace(day=28) + timedelta(days=14))  # due by the 10th of the following month
        tdate = to_date(r.get("Data_Movimentacao")) or ref
        if kd is None or tdate is None:
            continue
        native = f"VLMO:{code}:{r['Data_Referencia']}:v{r['Versao']}"
        fid = f"{source_id}:{native}"
        if fid not in filings:
            filings[fid] = filing_row(source_id=source_id, native_key=native, source_entity_id=code, form_type="VLMO",
                                      form_class="insider", knowledge_date=kd, period_end=ref, language="pt",
                                      url=idx.get("Link_Download"), is_amendment=int(r["Versao"] or 1) > 1)
        txn = next((t for pat, t in VLMO_TYPES if re.search(pat, mov.lower())), "other")
        role = (r.get("Tipo_Cargo") or "").strip() or "Não informado"
        rl = role.lower()
        price = parse_num(r.get("Preco_Unitario"))
        line[fid] = line.get(fid, -1) + 1
        ins.append(insider_row(
            source_id=source_id, filing_id=fid, line=line[fid], region="LATAM", source_entity_id=code,
            insider_name=f"{role} (aggregate)", insider_key=f"{code}:{role}", officer_title=role,
            is_director=rl.startswith("conselho de administra"), is_officer=rl.startswith("diretor"),
            is_ten_pct_owner=rl.startswith("controlador"),
            role_norm="director" if rl.startswith("conselho de administra") else "officer_other" if rl.startswith(
                "diretor") else "owner" if rl.startswith("controlador") else "other",
            transaction_date=tdate, knowledge_date=kd, txn_code=mov, txn_type=txn,
            is_open_market=txn in ("buy", "sell") and "vista" in mov.lower(),
            is_derivative=(r.get("Tipo_Ativo") or "") not in ("Ações", "Units"), shares=qty,
            price=price if price else None, currency="BRL", form_type="VLMO",
            security_title=" ".join(x for x in (r.get("Tipo_Ativo"), r.get("Caracteristica_Valor_Mobiliario")) if x) or None))
    z.close()
    return list(filings.values()), ins


# --------------------------------------------------------------------------------------------- reference / crosswalk


def fca_frames(contents: Iterable[tuple[int, bytes | str | Path]]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """FCA yearly zips -> (valor_mobiliario: cnpj, ticker_raw, ticker, root, name, fca_year, ended;
    geral: cnpj, cvm_code, name, fca_year)."""
    vms, gs = [], []
    for year, content in contents:
        with _open_zip(content) as z:
            names = z.namelist()
            vm_n = next((n for n in names if "valor_mobiliario" in n), None)
            g_n = next((n for n in names if "geral" in n), None)
            vm = _read_member(z, vm_n, ("CNPJ_Companhia", "Nome_Empresarial", "Codigo_Negociacao",
                                        "Data_Fim_Negociacao")) if vm_n else None
            g = _read_member(z, g_n, ("CNPJ_Companhia", "Codigo_CVM", "Nome_Empresarial")) if g_n else None
        if vm is not None and vm.height:
            vms.append(vm.select(pl.col("CNPJ_Companhia").str.replace_all(r"\D", "").str.zfill(14).alias("cnpj"),
                                 pl.col("Codigo_Negociacao").alias("ticker_raw"),
                                 pl.col("Nome_Empresarial").alias("name"), pl.lit(year).alias("fca_year"),
                                 pl.col("Data_Fim_Negociacao").fill_null("").str.strip_chars().ne("").alias("ended")))
        if g is not None and g.height:
            gs.append(g.select(pl.col("CNPJ_Companhia").str.replace_all(r"\D", "").str.zfill(14).alias("cnpj"),
                               _code_expr("Codigo_CVM").alias("cvm_code"), pl.col("Nome_Empresarial").alias("name"),
                               pl.lit(year).alias("fca_year")))
    vm = pl.concat(vms, how="vertical_relaxed") if vms else pl.DataFrame(
        schema={"cnpj": pl.Utf8, "ticker_raw": pl.Utf8, "name": pl.Utf8, "fca_year": pl.Int32, "ended": pl.Boolean})
    vm = vm.with_columns(pl.col("ticker_raw").map_elements(norm_b3_ticker, return_dtype=pl.Utf8).alias("ticker"))
    vm = vm.with_columns(pl.col("ticker").str.slice(0, 4).alias("root"))
    g = pl.concat(gs, how="vertical_relaxed") if gs else pl.DataFrame(
        schema={"cnpj": pl.Utf8, "cvm_code": pl.Utf8, "name": pl.Utf8, "fca_year": pl.Int32})
    return vm, g


def cadastro_frame(content: bytes | str | Path) -> pl.DataFrame:
    """cad_cia_aberta.csv (plain or .gz) -> cvm_code, cnpj, name, name_trade, active."""
    raw = Path(content).read_bytes() if not isinstance(content, (bytes, bytearray)) else bytes(content)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("cad.csv", raw)
    with zipfile.ZipFile(io.BytesIO(b.getvalue())) as z:
        df = _read_member(z, "cad.csv", ("CNPJ_CIA", "DENOM_SOCIAL", "DENOM_COMERC", "SIT", "CD_CVM"))
    if df is None or df.is_empty():
        return pl.DataFrame(schema={"cvm_code": pl.Utf8, "cnpj": pl.Utf8, "name": pl.Utf8, "name_trade": pl.Utf8,
                                    "active": pl.Boolean})
    return df.select(_code_expr().alias("cvm_code"), pl.col("CNPJ_CIA").str.replace_all(r"\D", "").str.zfill(14).alias("cnpj"),
                     pl.col("DENOM_SOCIAL").alias("name"), pl.col("DENOM_COMERC").alias("name_trade"),
                     (pl.col("SIT").fill_null("").str.strip_chars().str.to_uppercase() == "ATIVO").alias("active"))


def build_crosswalk(listings: Sequence[dict[str, Any]], issuer_by_code: dict[str, str], fca_vm: pl.DataFrame | None,
                    fca_geral: pl.DataFrame | None, cad: pl.DataFrame | None, issuer_names: dict[str, str] | None = None,
                    filer_codes: set[str] | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    """B3 listings (ticker, issuer_id) -> one crosswalk row per (issuer, cvm_code) decision (XW_SCHEMA) + stats.

    The issuer's own identifier_map cvm_code wins; otherwise its tickers (normalized, every share class) are looked up
    in FCA valor_mobiliario by exact ticker, then by the 4-character company root (PN / PNA / PNB / UNIT lines resolve
    to the ON line's registrant), then by name. A code owned by another issuer is reported as a split, never moved."""
    now = datetime.now(UTC)
    issuer_names = issuer_names or {}
    code_of_cnpj: dict[str, str] = {}
    if cad is not None and cad.height:
        for r in cad.sort("active", descending=True).iter_rows(named=True):
            if r["cvm_code"] and r["cnpj"] not in code_of_cnpj:
                code_of_cnpj[r["cnpj"]] = r["cvm_code"]
    if fca_geral is not None and fca_geral.height:  # newest FCA year wins over the cadastro
        for r in fca_geral.sort("fca_year").iter_rows(named=True):
            if r["cvm_code"]:
                code_of_cnpj[r["cnpj"]] = r["cvm_code"]
    by_ticker: dict[str, set[str]] = {}
    by_root: dict[str, set[str]] = {}
    if fca_vm is not None and fca_vm.height:
        for r in fca_vm.filter(pl.col("ticker").is_not_null()).iter_rows(named=True):
            c = code_of_cnpj.get(r["cnpj"])
            if c:
                by_ticker.setdefault(r["ticker"], set()).add(c)
                by_root.setdefault(r["root"], set()).add(c)
    # active registrants' names for the last-resort name match
    names_of: dict[str, list[list[str]]] = {}
    active: set[str] = set()
    if cad is not None and cad.height:
        for r in cad.iter_rows(named=True):
            if not r["cvm_code"]:
                continue
            if r["active"]:
                active.add(r["cvm_code"])
            for n in (r["name"], r["name_trade"]):
                if n:
                    names_of.setdefault(r["cvm_code"], []).append(name_tokens(n))
    if fca_geral is not None and fca_geral.height:
        for r in fca_geral.iter_rows(named=True):
            if r["cvm_code"] and r["name"]:
                names_of.setdefault(r["cvm_code"], []).append(name_tokens(r["name"]))
                active.add(r["cvm_code"])  # filed an FCA in the last years
    owner = dict(issuer_by_code)
    codes_of_issuer: dict[str, list[str]] = {}
    for c, i in owner.items():
        codes_of_issuer.setdefault(i, []).append(c)
    tickers_of: dict[str, list[str]] = {}
    for x in listings:
        if x.get("issuer_id") and x.get("ticker"):
            tickers_of.setdefault(x["issuer_id"], []).append(x["ticker"])
    rows: list[dict[str, Any]] = []
    claims: dict[str, list[int]] = {}
    stats: dict[str, Any] = {"issuers": len(tickers_of), "by_method": {}, "splits": [], "ambiguous": [], "unresolved": [],
                             "review": []}

    def add(iid: str, code: str | None, method: str, note: str | None = None, attached: bool | None = None) -> None:
        tks = sorted(set(tickers_of.get(iid, [])))
        conf = XW_CONFIDENCE.get(method, 0.0)
        att = (conf >= ATTACH_CONFIDENCE) if attached is None else attached
        rows.append({"issuer_id": iid, "cvm_code": code, "cnpj": None, "tickers": ",".join(tks),
                     "share_classes": ",".join(sorted({share_class(t) or "?" for t in tks})), "method": method,
                     "confidence": conf if code else 0.0, "attached": bool(att and code), "note": note, "built_at": now})
        if code and method != "identifier_map":
            claims.setdefault(code, []).append(len(rows) - 1)

    for iid in sorted(set(tickers_of) | set(codes_of_issuer)):
        if iid in codes_of_issuer:
            for c in sorted(codes_of_issuer[iid]):
                add(iid, c, "identifier_map")
            continue
        tks = [t for t in (norm_b3_ticker(t) for t in tickers_of.get(iid, [])) if t]
        tks = sorted(set(tks), key=lambda t: (share_class(t) != "ON", t))  # ON line first (override rows)
        cand: set[str] = set().union(*[by_ticker.get(t, set()) for t in tks]) if tks else set()
        method = "fca_ticker"
        if len(cand) != 1:
            roots = {t[:4] for t in tks}
            cand = set().union(*[by_root.get(r, set()) for r in roots]) if roots else set()
            method = "fca_root"
        if len(cand) != 1:
            short = name_tokens(issuer_names.get(iid))
            hits: dict[str, str] = {}
            for c, variants in names_of.items():
                kinds = {name_match(short, v) for v in variants} - {None}
                if kinds:
                    hits[c] = "prefix" if "prefix" in kinds else "abbrev"
            for narrow in (filer_codes, active):  # several hits: keep the ones that file / are active
                if len(hits) > 1 and narrow:
                    sub = {c: k for c, k in hits.items() if c in narrow}
                    hits = sub or hits
            if len(hits) == 1:
                c, k = next(iter(hits.items()))
                cand, method = {c}, "name_prefix" if k == "prefix" else "name_abbrev"
            elif len(hits) > 1:
                stats["ambiguous"].append({"issuer_id": iid, "tickers": tks, "candidates": sorted(hits)[:10]})
                add(iid, None, "ambiguous", note="candidates=" + ",".join(sorted(hits)[:10]))
                continue
            else:
                cand = set()
        if len(cand) != 1:
            stats["unresolved"].append({"issuer_id": iid, "tickers": tks, "name": issuer_names.get(iid)})
            add(iid, None, "unresolved", note="mapping_gap: no CVM registrant for the ticker root / name")
            continue
        code = next(iter(cand))
        if code in owner and owner[code] != iid:
            # PN / unit-only issuer split from the issuer that owns the registrant's code: facts stay on the owner
            stats["splits"].append({"issuer_id": iid, "tickers": tks, "cvm_code": code, "owner_issuer_id": owner[code],
                                    "suggested_override": f"link,cvm:{code},ticker:{(tks or ['?'])[0]}@BVMF,"
                                                          f"COVERAGE_V3 B3 PN/unit line of the CVM registrant"})
            add(iid, code, method, note=f"split: code owned by {owner[code]}", attached=False)
            continue
        add(iid, code, method)
        if XW_CONFIDENCE[method] < ATTACH_CONFIDENCE:  # abbreviation only: reported, attaches once confirmed
            stats["review"].append({"issuer_id": iid, "tickers": tks, "cvm_code": code, "name": issuer_names.get(iid),
                                    "suggested_override": f"link,ticker:{(tks or ['?'])[0]}@BVMF,cvm:{code},"
                                                          f"COVERAGE_V3 B3 confirmed CVM registrant"})
    # a code claimed by two unresolved issuers is ambiguous: attach neither
    for code, ix in claims.items():
        att = [i for i in ix if rows[i]["attached"]]
        if len(att) > 1:
            for i in att:
                rows[i]["attached"] = False
                rows[i]["note"] = "ambiguous: code claimed by " + ",".join(sorted(rows[j]["issuer_id"] for j in att))
            stats["ambiguous"].append({"cvm_code": code, "issuers": sorted(rows[j]["issuer_id"] for j in att)})
    cnpj_of = {c: n for n, c in code_of_cnpj.items()}
    for r in rows:
        r["cnpj"] = cnpj_of.get(r["cvm_code"]) if r["cvm_code"] else None
        stats["by_method"][r["method"]] = stats["by_method"].get(r["method"], 0) + 1
    stats["attached_codes"] = len({r["cvm_code"] for r in rows if r["attached"]})
    df = pl.DataFrame(rows, schema=XW_SCHEMA) if rows else pl.DataFrame(schema=XW_SCHEMA)
    return df, stats


def _file_sig(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"{p.stat().st_size}:{h.hexdigest()[:16]}"


@register_adapter
class CvmAdapter(SourceAdapter):
    source_id = "cvm"
    regions = ("LATAM",)
    produces = ("filings_index", "fundamentals_raw", "insider_transactions")
    base_url = BASE
    rate_limit = RateLimit(2.0, 2)

    def __init__(self, *args: Any, years: str | Sequence[int] | None = None, datasets: str = "dfp,itr,vlmo",
                 scope: str | None = None, all_listed: Any = None, offline: Any = False, raw_from: str | None = None,
                 backfill_years: int | str | None = None, renormalize: Any = False, max_minutes: float | None = None,
                 min_free_gb: float = 1.5, dry_run: Any = False, **kw: Any):
        super().__init__(*args, **kw)
        y = datetime.now(UTC).year
        if years:
            self.years = [int(v) for v in (str(years).split(",") if isinstance(years, str) else years)]
        elif backfill_years:
            self.years = list(range(y - int(backfill_years) + 1, y + 1))
        else:
            self.years = [y - 1, y]
        self.datasets = [d.strip().lower() for d in str(datasets).split(",") if d.strip()]
        tri = (lambda v: str(v).lower() in ("1", "true", "yes"))
        if scope is None and all_listed is not None:
            scope = "universe" if tri(all_listed) else "seed"
        self.scope = (scope or "universe").lower()
        if self.scope not in SCOPES:
            raise ValueError(f"cvm scope {scope!r} not in {SCOPES}")
        self.offline = tri(offline)
        self.raw_from = Path(raw_from) if raw_from else None
        self.renormalize = tri(renormalize)
        self.dry_run = tri(dry_run)
        self.max_minutes = float(max_minutes) if max_minutes else None
        self.min_free_gb = float(min_free_gb)
        self._ref: dict[str, Any] | None = None
        self._xw: tuple[pl.DataFrame, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ raw files
    def _zip(self, path: str) -> bytes:
        return self.http.get(BASE + path, cache_ttl=6 * 3600).content

    def raw_root(self) -> Path:
        return self.raw_from or (self.settings.raw_dir / self.source_id)

    def landed(self, dataset: str, name: str) -> Path | None:
        """Newest landed copy of raw/cvm/<dataset>/dt=*/<name> (a valid zip / non-empty file)."""
        d = self.raw_root() / dataset
        if not d.exists():
            return None
        for sub in sorted((p for p in d.glob("dt=*") if p.is_dir()), key=lambda p: p.name, reverse=True):
            p = sub / name
            if p.exists() and p.stat().st_size > 0 and (not name.endswith(".zip") or zipfile.is_zipfile(p)):
                return p
        return None

    def _fca_contents(self) -> list[tuple[int, bytes | Path]]:
        y = datetime.now(UTC).year
        out: list[tuple[int, bytes | Path]] = []
        for yy in range(y, y - 3, -1):
            p = self.landed("ref_fca", f"fca_cia_aberta_{yy}.zip") or self.landed("fca", f"fca_cia_aberta_{yy}.zip")
            if p is not None and (self.offline or (datetime.now(UTC).date() - date.fromisoformat(
                    p.parent.name[3:])).days <= 7):
                out.append((yy, p))
                continue
            if self.offline:
                continue
            try:
                content = self._zip(f"DOC/FCA/DADOS/fca_cia_aberta_{yy}.zip")
            except Exception:  # noqa: BLE001 - year not published yet
                if p is not None:
                    out.append((yy, p))
                continue
            out.append((yy, self.save_raw("fca", f"fca_cia_aberta_{yy}.zip", content, compress=False)))
        return out

    def _cadastro(self) -> pl.DataFrame | None:
        p = self.landed("ref_cadastro", "cad_cia_aberta.csv.gz") or self.landed("cadastro", "cad_cia_aberta.csv.gz")
        if p is None and not self.offline:
            try:
                r = self.http.get(BASE + "CAD/DADOS/cad_cia_aberta.csv", cache_ttl=7 * 86400)
                p = self.save_raw("cadastro", "cad_cia_aberta.csv", r.content)
            except Exception as e:  # noqa: BLE001
                self.log.info("cvm cadastro unavailable (%s)", type(e).__name__)
        return cadastro_frame(p) if p is not None else None

    # ------------------------------------------------------------------ reference / crosswalk
    def reference(self) -> dict[str, Any]:
        """identifier_map: cvm_code -> issuer_id, BVMF listings (ticker_mic) -> issuer; BR issuer names."""
        if self._ref is not None:
            return self._ref
        ref: dict[str, Any] = {"issuer_by_code": {}, "listings": [], "names": {}, "available": False}
        try:
            from app.core.storage import scan_table

            today = datetime.now(UTC).date()
            im = (scan_table("identifier_map", self.settings)
                  .filter(pl.col("id_type").is_in(["cvm_code", "ticker_mic", "issuer_alias"])
                          & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= today))
                          & (pl.col("confidence").fill_null(1.0) >= 0.9))
                  .filter((pl.col("id_type") != "ticker_mic") | pl.col("id_value").str.ends_with("@BVMF"))
                  .select("id_type", "id_value", "issuer_id", "security_id", "confidence").collect())
            alias = {r["id_value"]: r["issuer_id"] for r in im.filter(pl.col("id_type") == "issuer_alias").iter_rows(named=True)
                     if r["issuer_id"]}
            for r in im.filter(pl.col("id_type") == "cvm_code").sort("confidence", descending=True).iter_rows(named=True):
                c = cvm_code(r["id_value"])
                if c and r["issuer_id"] and c not in ref["issuer_by_code"]:
                    ref["issuer_by_code"][c] = alias.get(r["issuer_id"], r["issuer_id"])
            for r in im.filter(pl.col("id_type") == "ticker_mic").iter_rows(named=True):
                if r["issuer_id"]:
                    ref["listings"].append({"ticker": r["id_value"].split("@")[0],
                                            "issuer_id": alias.get(r["issuer_id"], r["issuer_id"]),
                                            "security_id": r["security_id"]})
            ref["available"] = im.height > 0
            try:
                iss = (scan_table("issuers", self.settings).filter(pl.col("country") == "BR")
                       .select("issuer_id", "name").collect())
                ref["names"] = {a: b for a, b in iss.iter_rows() if a and b}
            except Exception:  # noqa: BLE001 - names only feed the last-resort name match
                pass
        except Exception as e:  # noqa: BLE001 - no reference yet (fresh install / tests): seed scope
            self.log.info("cvm: identifier_map unavailable (%s)", type(e).__name__)
        self._ref = ref
        return ref

    def crosswalk(self, filer_codes: set[str] | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
        if self._xw is not None:
            return self._xw
        ref = self.reference()
        if not ref["listings"] and not ref["issuer_by_code"]:
            self._xw = (pl.DataFrame(schema=XW_SCHEMA), {"issuers": 0})
            return self._xw
        vm, g = fca_frames(self._fca_contents())
        df, stats = build_crosswalk(ref["listings"], ref["issuer_by_code"], vm, g, self._cadastro(), ref["names"],
                                    filer_codes)
        p = self.settings.raw_dir / self.source_id  # never into raw_from (a read-only source of landed zips)
        p.mkdir(parents=True, exist_ok=True)
        tmp = p / "crosswalk.parquet.tmp"
        df.write_parquet(tmp)
        tmp.replace(p / "crosswalk.parquet")
        self._xw = (df, stats)
        return self._xw

    def issuer_of(self) -> dict[str, str]:
        """cvm code -> issuer_id used at ingest: identifier_map, then crosswalk matches >= ATTACH_CONFIDENCE."""
        out = dict(self.reference()["issuer_by_code"])
        if self.scope != "seed":
            xw, _ = self.crosswalk()
            for r in xw.filter(pl.col("attached")).iter_rows(named=True):
                out.setdefault(r["cvm_code"], r["issuer_id"])
        return out

    def ticker_map(self) -> list[dict[str, str]]:
        """FCA valor_mobiliario rows (cvm, cnpj, ticker, name, class, segment, sector, fy_end_month, end)."""
        out = []
        for _yy, content in self._fca_contents():
            with _open_zip(content) as z:
                names = z.namelist()
                geral = _csv(z, next(n for n in names if "geral" in n))
                vm = _csv(z, next(n for n in names if "valor_mobiliario" in n))
            cvm_by_cnpj = {cnpj_digits(r["CNPJ_Companhia"]): (cvm_code(r["Codigo_CVM"]), r) for r in geral}
            for r in vm:
                code, g = cvm_by_cnpj.get(cnpj_digits(r["CNPJ_Companhia"]), (None, {}))
                tk = norm_b3_ticker(r.get("Codigo_Negociacao"))
                if code and tk:
                    out.append({"cvm": code, "cnpj": cnpj_digits(r["CNPJ_Companhia"]), "ticker": tk,
                                "name": r["Nome_Empresarial"], "class": r.get("Valor_Mobiliario"),
                                "segment": r.get("Segmento"), "sector": g.get("Setor_Atividade"),
                                "fy_end_month": g.get("Mes_Encerramento_Exercicio_Social"),
                                "end": r.get("Data_Fim_Negociacao")})
            if out:
                break  # newest FCA year with rows
        return out

    def discover(self) -> pl.DataFrame:
        rows = []
        seen = set()
        for r in self.ticker_map():
            if r["end"] or r["cvm"] in seen:
                continue
            seen.add(r["cvm"])
            rows.append({"source_entity_id": r["cvm"], "cvm_code": r["cvm"], "cnpj": r["cnpj"], "name": r["name"],
                         "country": "BR", "ticker": r["ticker"], "mic": "BVMF", "yf_ticker": f"{r['ticker']}.SA",
                         "industry_code": r["sector"], "industry_scheme": "cvm", "is_active": True})
        return self.discover_frame(rows, self.source_id)

    # ------------------------------------------------------------------ scope
    def _codes_for(self, wanted: Sequence[str]) -> set[str]:
        """CVM codes / B3 tickers ('PETR4', 'GGBR4.SA') -> CVM codes (identifier_map listing first, then FCA root)."""
        out: set[str] = set()
        ref = self.reference()
        codes_of: dict[str, set[str]] = {}
        for c, i in (self.issuer_of() if self.scope != "seed" else ref["issuer_by_code"]).items():
            codes_of.setdefault(i, set()).add(c)
        iss_by_tk = {norm_b3_ticker(x["ticker"]): x["issuer_id"] for x in ref["listings"]}
        todo = []
        for w in wanted:
            w = str(w).strip()
            if re.fullmatch(r"\d+", w):
                out.add(cvm_code(w))
                continue
            tk = norm_b3_ticker(w)
            cs = codes_of.get(iss_by_tk.get(tk)) if tk else None
            if cs:
                out |= cs
            elif tk:
                todo.append(tk)
        if todo:
            tmap = self.ticker_map()
            by_tk = {r["ticker"]: r["cvm"] for r in tmap}
            by_root: dict[str, set[str]] = {}
            for r in tmap:
                by_root.setdefault(r["ticker"][:4], set()).add(r["cvm"])
            for tk in todo:
                if tk in by_tk:
                    out.add(by_tk[tk])
                elif len(by_root.get(tk[:4], ())) == 1:
                    out |= by_root[tk[:4]]
        return out

    def _universe(self, entities: Sequence[str] | None) -> set[str] | None:
        if entities:
            return self._codes_for(list(entities))
        if self.scope == "all":
            return None
        seed = seed_universe(self.settings, countries=["BR"]).get_column("ticker").to_list()
        if self.scope == "universe":
            codes = set(self.issuer_of())
            if codes:
                return codes | (self._codes_for(seed) if seed else set())
        if not seed:
            return None  # pre-B3 rule: no seed and no reference -> every registrant
        return self._codes_for(seed)

    # ------------------------------------------------------------------ fetch / normalize
    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        uni = safe_call(lambda: self._universe(entities), res, "universe", self.log)
        if uni is None and "universe" in res.entities_failed:
            res.meta["message"] = "universe failed: nothing fetched (never widened to every registrant)"
            res.meta["files"] = []
            return res
        res.meta["universe"] = sorted(uni) if uni is not None else None
        res.meta["scope"] = "entities" if entities else self.scope
        if self._xw is not None:
            st = self._xw[1]
            res.meta["crosswalk"] = {k: st.get(k) for k in ("issuers", "by_method", "attached_codes", "splits",
                                                            "ambiguous", "unresolved", "review")}
        landed = []
        for ds in self.datasets:
            for y in self.years:
                name = f"{ds}_cia_aberta_{y}.zip"
                if self.offline:
                    p = self.landed(ds, name)
                    if p is not None:
                        landed.append({"ds": ds, "year": y, "_raw_path": str(p)})
                    else:
                        res.meta.setdefault("not_landed", []).append(name)
                    continue
                path = f"DOC/{ds.upper()}/DADOS/{name}"

                def one(ds=ds, y=y, path=path, name=name) -> None:
                    content = self._zip(path)
                    p = self.save_raw(ds, name, content, compress=False)
                    landed.append({"ds": ds, "year": y, "_raw_path": str(p)})
                safe_call(one, res, f"{ds}:{y}", self.log)
        if self.offline and landed:
            res.entities_ok.extend(f"{f['ds']}:{f['year']}" for f in landed)
        res.meta["files"] = landed
        res.meta["since"] = str(since) if since else None
        res.watermark = datetime.now(UTC).date().isoformat()
        return res

    def _parse_file(self, f: dict[str, Any], uni: set[str] | None, issuer_of: dict[str, str],
                    include: pl.Expr | None) -> tuple[dict[str, pl.DataFrame], dict[str, Any]]:
        path = Path(f["_raw_path"])
        if f["ds"] in ("dfp", "itr"):
            fil, facts, st = parse_statement_frames(path, f["ds"].upper(), f["year"], uni, self.source_id, issuer_of,
                                                    include=include)
            return {"filings_index": fil, "fundamentals_raw": facts}, st
        if f["ds"] == "vlmo":
            a, b = parse_vlmo_zip(path, f["year"], uni, self.source_id)
            fil, ins = frame(a, "filings_index"), frame(b, "insider_transactions")
            if issuer_of:
                fil = fil.with_columns(pl.coalesce(pl.col("issuer_id"), _issuer_expr(issuer_of, "source_entity_id")))
                ins = ins.with_columns(pl.coalesce(pl.col("issuer_id"), _issuer_expr(issuer_of, "source_entity_id")))
            if include is not None:
                fil = fil.with_columns(pl.col("source_entity_id").alias("code"), pl.col("knowledge_date").alias("kd")) \
                    .filter(include).drop("code", "kd")
                ins = ins.with_columns(pl.col("source_entity_id").alias("code"), pl.col("knowledge_date").alias("kd")) \
                    .filter(include).drop("code", "kd")
            return {"filings_index": fil, "insider_transactions": ins}, {"filings": fil.height, "insider": ins.height}
        return {}, {}

    @staticmethod
    def _include(covered: dict[str, str | None] | str, issuer_of: dict[str, str], since: date | None) -> pl.Expr | None:
        """Documents to (re)write: every document of codes not yet covered (or now under another issuer); for covered
        codes only those received on / after `since`. None = everything."""
        if since is None:
            return None
        if covered == "*":
            return pl.col("kd") >= since
        fresh = [c for c, i in covered.items() if issuer_of.get(c) == i]
        if not fresh:
            return None
        return ~pl.col("code").is_in(fresh) | (pl.col("kd") >= since)

    @staticmethod
    def _todo(uni: set[str] | None, covered: dict[str, str | None] | str, issuer_of: dict[str, str]) -> list[str] | str:
        """Codes of `uni` without a full write from this zip yet ('*' = all registrants and not every one covered)."""
        if uni is None:
            return "" if covered == "*" else "*"
        cov = {} if covered == "*" else covered
        return [c for c in sorted(uni) if c not in cov or cov[c] != issuer_of.get(c)]

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        """All landed files at once (no state; run() writes file by file instead)."""
        uni = set(fetched.meta["universe"]) if fetched.meta.get("universe") is not None else None
        since = to_date(fetched.meta.get("since"))
        issuer_of = self.issuer_of() if self.reference()["available"] else {}
        out: dict[str, list[pl.DataFrame]] = {t: [] for t in self.produces}
        inc = None if since is None else pl.col("kd") >= since
        for f in fetched.meta.get("files", []):
            tables, _ = self._parse_file(f, uni, issuer_of, inc)
            for t, df in tables.items():
                out[t].append(df)
        return {t: (pl.concat(v, how="vertical_relaxed") if v else _empty(t)) for t, v in out.items()}

    def _ram_ok(self) -> tuple[bool, str | None]:
        try:
            from app.ops import winsys

            m = winsys.memory_status() or {}
            if m.get("avail_gb") is not None and m.get("total_gb") and m["avail_gb"] < self.min_free_gb:
                return False, f"free RAM {m['avail_gb']:.1f} GB < min_free_gb {self.min_free_gb}"
        except Exception:  # noqa: BLE001 - unknown memory never blocks
            pass
        return True, None

    def run(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None,
            write: bool = True) -> tuple[FetchResult, dict[str, int]]:
        """fetch, then normalize + write ONE zip at a time (bounded memory, resumable per zip via data/state/cvm.json)."""
        from app.core.schema import conform
        from app.core.storage import write_table

        t0 = time.time()
        self.check_keys()
        fetched = self.fetch(since=since, entities=entities)
        fetched.n_requests, fetched.n_cache_hits = self.http.n_requests, self.http.n_cache_hits
        rows: dict[str, int] = {t: 0 for t in self.produces}
        uni = set(fetched.meta["universe"]) if fetched.meta.get("universe") is not None else None
        since_d = None if self.renormalize else to_date(fetched.meta.get("since"))
        if since_d is not None:  # the yearly zips can trail the receipt date by a few days: re-read a month
            since_d -= timedelta(days=SINCE_OVERLAP_DAYS)
        issuer_of = self.issuer_of() if self.reference()["available"] else {}
        state = load_state(self.source_id, self.settings)
        norm = state.setdefault("normalized", {})
        files = fetched.meta.get("files", [])
        plan, stats = [], {}
        for f in files:
            key = f"{f['ds']}:{f['year']}"
            prev = norm.get(key) or {}
            sig = _file_sig(Path(f["_raw_path"]))
            covered: dict[str, str | None] | str = {} if self.renormalize else (prev.get("codes") or {})
            todo = self._todo(uni, covered, issuer_of)
            if prev.get("sig") == sig and not todo and not self.renormalize:
                stats[key] = {"skipped": "unchanged zip, every code covered"}
                continue
            plan.append((f, key, sig, covered, todo))
        fetched.meta["plan"] = [{"file": k, "new_codes": (len(t) if isinstance(t, list) else t)} for _, k, _, _, t in plan]
        if self.dry_run:
            fetched.meta.update(dry_run=True, stats=stats)
            return fetched, rows
        for f, key, sig, covered, _todo in plan:
            ok, why = self._ram_ok()
            if not ok or (self.max_minutes and time.time() - t0 > self.max_minutes * 60):
                why = why or f"time budget {self.max_minutes} min reached"
                fetched.entities_failed["budget"] = f"stopped before {key}: {why} (resumable)"
                break
            try:
                tables, st = self._parse_file(f, uni, issuer_of, self._include(covered, issuer_of, since_d))
                for t, df in tables.items():
                    df = conform(df, t)
                    rows[t] += write_table(df, t, settings=self.settings) if write else df.height
                del tables
            except Exception as e:  # noqa: BLE001 - one bad zip must not stop the others
                from app.core.logsetup import redact

                fetched.entities_failed[f"normalize:{key}"] = redact(f"{type(e).__name__}: {e}")[:300]
                self.log.warning("cvm normalize %s failed: %s", key, fetched.entities_failed[f"normalize:{key}"])
                continue
            stats[key] = st
            if write:
                done = "*" if uni is None else {**({} if covered == "*" else covered),
                                                **{c: issuer_of.get(c) for c in uni}}
                norm[key] = {"sig": sig, "codes": done, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                             "path": f["_raw_path"], "stats": st}
                state["scope_mode"] = fetched.meta.get("scope")
                save_state(self.source_id, state, self.settings)
        fetched.meta["stats"] = stats
        fetched.finished_at = datetime.now(UTC)
        if not fetched.entities_failed:
            xw = fetched.meta.get("crosswalk") or {}
            fetched.meta.setdefault("message", (
                f"scope {fetched.meta.get('scope')}: {len(uni) if uni is not None else 'all'} codes, {len(plan)} of "
                f"{len(files)} zips normalized; crosswalk {xw.get('attached_codes', 0)} codes attached, "
                f"{len(xw.get('unresolved') or [])} unresolved, {len(xw.get('review') or [])} to review, "
                f"{len(xw.get('splits') or [])} splits"))
        self.log.info("%s run: status=%s rows=%s files=%d requests=%d cache_hits=%d (%.0fs)", self.source_id,
                      fetched.status, rows, len(plan), fetched.n_requests, fetched.n_cache_hits, time.time() - t0)
        return fetched, rows


def run(ctx: Any) -> Any:
    return run_adapter_job(CvmAdapter, ctx)
