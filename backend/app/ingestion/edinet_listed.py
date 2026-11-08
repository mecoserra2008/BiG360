"""EDINET statements for every listed JP issuer (docs/COVERAGE_V3_PLAN.md item B1). Owner: ingest-global.

Why: app.ingestion.edinet downloads XBRL->CSV only for the mvp_global seed (40 JP issuers), although the EDINET daily
document lists of 2016-10 -> today are already landed (data/raw/edinet/lists, written by edinet_filings /
edinet_backfill) and the EDINET code list maps every listed filer. This job turns the landed lists into a download
queue for every listed JP issuer of our reference data and loads the annual securities reports (yuho, docTypeCode
120 / amendment 130), half-year reports (160 / 170) and the legacy quarterly reports (140 / 150; abolished for periods
from 2024-04) into filings_index + fundamentals_raw. fundamentals_normalize then maps them to fundamentals_canonical
with the existing 'jp' chains of contracts/canonical_line_items.yaml (+ canonical_line_items_ext_global.yaml 'jp').

Mapping (EDINET code <-> securities code <-> our securities_master):
  1. the document's edinetCode -> identifier_map edinet_code (current, confidence >= 0.9) -> issuer_id;
  2. else its secCode (5 digits: '72030' -> '7203', '285A0' -> '285A'; if the list row has none, the code list's
     証券コード of that EDINET code) -> identifier_map jp_sec_code (the JPX listing) -> issuer_id.
  Documents of filers outside the reference data (unlisted bond issuers, delisted companies, funds) are counted as
  `mapping_gap` / `out_of_scope` and not downloaded. issuer_id is filled on the written rows (resolve_frame keeps it).

PIT: knowledge_date = submitDateTime as a JST calendar date (JST is UTC+9, so it is never before the UTC date the
document became public); filings_index.knowledge_ts = the exact submit time in UTC. Facts dated after the submit date
are dropped by app.ingestion.edinet.parse_edinet_csv; amendments (130/150/170) carry their own submit date, so a
correction never back-dates the original values.

Queue: documents of the landed lists (the latest landing of each day; csvFlag=1; not withdrawn) mapped to an issuer,
minus documents already loaded (filings_index edinet rows with a doc_path), minus documents that failed `max_attempts`
times; newest submission first (order=oldest reverses). Zips already on disk (data/raw/edinet/xbrl_csv) are reused.

Resumable, chunked, RAM-bounded (params chunk_docs / commit_docs):
  * mini-batches of chunk_docs documents are downloaded (EDINET bucket 1 req/s, the settings User-Agent), parsed and
    spilled to data/tmp/edinet_listed/spill (only one mini-batch is in memory);
  * every commit_docs documents (and at the end / on the time budget) the spill is committed under the
    fundamentals_raw table lock: state 'pending' = {seq, docs} -> one file per fiscal-year partition, written in
    data/tmp/edinet_listed/stage (same volume, outside every table tree) and renamed into
    normalized/fundamentals_raw/source_id=edinet/fiscal_year=Y/part-edl-<seq>.parquet (append; nothing existing is
    read, rewritten or deleted) -> filings_index upsert -> ledger lines '<seq>\\t<docID>' -> state seq+1, pending
    cleared. The seq of a commit is max(state seq, 1 + the ledger's highest seq, 1 + the highest part-edl-<seq> on
    disk), so a lost / reset state file can never reuse (and overwrite) a committed seq; an unreadable state file
    fails the run instead of restarting at 1;
  * a run that died inside a commit leaves 'pending': the next run (recover) deletes that seq's files, rewrites its
    ledger lines as 'redo\\t<docID>' and re-processes the documents whatever the window (their zips are on disk).
    part-edl files above the ledger's highest seq (a dead commit whose state was lost) are treated the same way
    (their docIDs are read from the files first). Uncommitted spill is discarded at start;
  * at the end (skipped when the run was cancelled or its time budget is spent), a partition with >= compact_files
    part-edl files is merged by a streaming copy (sink_parquet) staged outside the table tree. A state marker
    'compacting' = {partition, inputs, merged, trash} is saved before the merged file is published and cleared after
    the inputs were moved to data/tmp/edinet_listed/trash; recover() resolves a marker left by a crash (merged
    published, no input moved -> merged removed; moves in progress -> finished; the merged file's row count was
    verified before publishing). An input that cannot be moved (Windows: open by a reader) rolls the merge back.
  A job-level lock (data/.locks/edinet_listed.lock) keeps the nightly and the backfill run from overlapping.
  fundamentals_normalize picks the new facts up by ingested_at (stamped per partition file, seconds before it becomes
  visible); its watermark overlap (app.normalize.fundamentals.WM_OVERLAP, 15 min) covers files published while a
  normalize run was listing the table. The nightly DAG runs edinet_listed before identity_resolve (and so before
  fundamentals_normalize) and earnings_events.
  A targeted re-normalisation is params issuer_ids (or full=true).

Facts kept (param dims): 'segments' (default) = every non-dimensional fact (consolidated and parent-only) plus the
dimensional facts on segment members (C1 segments); 'all' = everything (as the seed adapter); 'none' = non-dimensional
only. The raw zips stay on disk, so a later consumer can re-parse other dimensions.

Params: mode backfill|recent (recent = since_days window, default 14, + fetch missing day lists), since / until
(submit dates), backfill_years (10), doc_classes (annual,semiannual,quarterly), issuers (tickers / EDINET codes),
max_docs, max_minutes, chunk_docs (50), commit_docs (1000), dims, order, max_attempts (5), compact_files (8),
retry_failed (parse = re-queue documents whose CSV failed to parse, e.g. after a parser fix; download = documents
that failed max_attempts downloads; all = both), dry_run (plan only: counts, estimate; no download, no write).

Job ids: edinet_listed (nightly, recent), edinet_listed_backfill (manual, backfill). State: data/state/edinet_listed.json.
"""

from __future__ import annotations

import csv
import errno
import gzip
import io
import json
import re
import shutil
import time
import uuid
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger, redact
from app.ingestion._global_common import safe_call, save_state, seed_universe, state_path, to_date
from app.ingestion.base import FetchResult
from app.ingestion.edinet import EdinetAdapter

log = get_logger("app.ingestion.edinet_listed")

SOURCE_ID = "edinet"
STATE_ID = "edinet_listed"
LOCK_NAME = "edinet_listed"
FILE_PREFIX = "part-edl-"
DOC_CLASSES: dict[str, set[str]] = {"annual": {"120", "130"}, "semiannual": {"160", "170"},
                                    "quarterly": {"140", "150"}}
CLASS_OF = {c: k for k, v in DOC_CLASSES.items() for c in v}
#: facts per document with dims=segments, measured on the B1 validation sample (25 small / mid caps, 2026-11:
#: annual 543, half-year 314; quarterly = seed quarterly 356 x 0.71) and the factor of the other dims policies
AVG_FACTS = {"annual": 545, "semiannual": 315, "quarterly": 255}
DIMS_FACTOR = {"segments": 1.0, "all": 1.41, "none": 0.89}
SEC_PER_DOC = 1.25  # 1 req/s bucket + parse + spill, measured on the validation sample (117 docs in 2.4 min)
LIST_RE = re.compile(r"^documents_(\d{4}-\d{2}-\d{2})\.json(\.gz)?$")
SEQ_RE = re.compile(r"^part-edl-(\d{6})\.parquet$")
RETRY_FAILED = {"": set(), "none": set(), "parse": {"pipeline_bug"}, "download": {"source_unavailable"},
                "all": {"pipeline_bug", "source_unavailable"}, "true": {"pipeline_bug", "source_unavailable"}}
KEEP = ("docID", "docTypeCode", "edinetCode", "secCode", "filerName", "periodStart", "periodEnd", "submitDateTime",
        "parentDocID", "xbrlFlag", "csvFlag", "withdrawalStatus", "docDescription")
_SEGMENT = re.compile(r"(?i)segment")


# ------------------------------------------------------------------------------------------------ small helpers


def sec4(v: Any) -> str | None:
    """EDINET / JPX securities code -> 4-char listing code ('72030' -> '7203', '285A0' -> '285A', '7203' -> '7203')."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    if len(s) == 5 and s.endswith("0"):
        return s[:4]
    return s


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def doc_types_for(classes: Iterable[str] | None) -> set[str]:
    cls = list(classes or DOC_CLASSES)
    bad = [c for c in cls if c not in DOC_CLASSES]
    if bad:
        raise ValueError(f"unknown doc_classes {bad}; use {sorted(DOC_CLASSES)}")
    return set().union(*(DOC_CLASSES[c] for c in cls))


# ------------------------------------------------------------------------------------------------ lists on disk


def list_files(raw_dir: Path, since: date | None = None, until: date | None = None) -> dict[date, Path]:
    """Landed EDINET day lists -> {list date: path of its latest landing} (a day re-fetched later wins: withdrawals
    and late corrections are reflected there)."""
    best: dict[date, tuple[str, Path]] = {}
    root = raw_dir / SOURCE_ID / "lists"
    if not root.exists():
        return {}
    for p in root.glob("dt=*/documents_*.json*"):
        m = LIST_RE.match(p.name)
        if not m:
            continue
        d = date.fromisoformat(m.group(1))
        if (since and d < since) or (until and d > until):
            continue
        landed = p.parent.name[3:]
        prev = best.get(d)
        if prev is None or landed > prev[0]:
            best[d] = (landed, p)
    return {d: p for d, (_, p) in sorted(best.items())}


def _read_list(p: Path) -> list[dict[str, Any]]:
    data = p.read_bytes()
    if p.name.endswith(".gz"):
        data = gzip.decompress(data)
    out = json.loads(data)
    return out if isinstance(out, list) else (out.get("results") or [])


def scan_lists(files: dict[date, Path], doc_types: set[str], stats: Counter | None = None) -> pl.DataFrame:
    """Candidate documents of the landed lists: docTypeCode in doc_types, csvFlag=1, not withdrawn. Column-wise
    accumulation (10 years of lists = ~3 M entries, ~150 k candidates)."""
    st = stats if stats is not None else Counter()
    cols: dict[str, list[Any]] = {k: [] for k in (*KEEP, "list_date")}
    for d, p in files.items():
        try:
            lst = _read_list(p)
        except Exception as e:  # noqa: BLE001
            st["list_unreadable"] += 1
            log.warning("edinet_listed: unreadable list %s: %s", p.name, e)
            continue
        st["list_days"] += 1
        for x in lst:
            code = x.get("docTypeCode")
            if code not in doc_types:
                continue
            st["fund_docs"] += 1
            if x.get("withdrawalStatus") not in (None, "0"):
                st["withdrawn"] += 1
                continue
            if x.get("csvFlag") != "1":
                st["no_csv"] += 1
                continue
            if not x.get("docID") or not x.get("submitDateTime"):
                st["incomplete"] += 1
                continue
            for k in KEEP:
                cols[k].append(x.get(k))
            cols["list_date"].append(d)
    schema = {k: pl.Utf8 for k in KEEP} | {"list_date": pl.Date}
    df = pl.DataFrame(cols, schema=schema)
    return df.unique(subset=["docID"], keep="last", maintain_order=True)


# ------------------------------------------------------------------------------------------------ universe


@dataclass
class Universe:
    """EDINET code / securities code -> issuer_id lookups from the reference data."""

    by_edinet: dict[str, str] = field(default_factory=dict)
    by_sec: dict[str, str] = field(default_factory=dict)
    sec_of_edinet: dict[str, str] = field(default_factory=dict)   # code list (listed filers)
    seed_issuers: set[str] = field(default_factory=set)
    ticker_of_issuer: dict[str, str] = field(default_factory=dict)

    @property
    def issuers(self) -> set[str]:
        return set(self.by_edinet.values()) | set(self.by_sec.values())

    def restrict(self, keys: Sequence[str]) -> "Universe":
        """Only the issuers named by tickers ('7203', '285A', '7203.T') or EDINET codes ('E02144')."""
        want: set[str] = set()
        for k in keys:
            k = k.strip().upper()
            if k.startswith("E") and k[1:].isdigit():
                if k in self.by_edinet:
                    want.add(self.by_edinet[k])
                elif sec4(self.sec_of_edinet.get(k)) in self.by_sec:
                    want.add(self.by_sec[sec4(self.sec_of_edinet[k])])
            else:
                s = sec4(k.split(".")[0])
                if s in self.by_sec:
                    want.add(self.by_sec[s])
        return Universe({e: i for e, i in self.by_edinet.items() if i in want},
                        {s: i for s, i in self.by_sec.items() if i in want}, dict(self.sec_of_edinet),
                        self.seed_issuers & want, {i: t for i, t in self.ticker_of_issuer.items() if i in want})


def read_codelist(raw_dir: Path) -> dict[str, str]:
    """Latest landed Edinetcode.zip -> {EDINET code: 4-char securities code} for filers with a securities code."""
    zips: list[Path] = []
    for sub in ("ref_codelist", "codelist"):
        zips += list((raw_dir / SOURCE_ID / sub).glob("dt=*/Edinetcode.zip"))
    if not zips:
        return {}
    p = max(zips, key=lambda x: x.parent.name)
    try:
        zf = zipfile.ZipFile(io.BytesIO(p.read_bytes()))
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        lines = zf.read(name).decode("cp932", errors="replace").splitlines()[1:]
    except Exception as e:  # noqa: BLE001
        log.warning("edinet_listed: code list %s unreadable: %s", p, e)
        return {}
    out: dict[str, str] = {}
    for r in csv.reader(lines[1:]):
        if len(r) > 11 and r[0].strip() and r[11].strip():
            out[r[0].strip().upper()] = sec4(r[11]) or ""
    return out


def build_universe(identifier_map: pl.DataFrame, codelist: dict[str, str] | None = None,
                   seed_tickers: Iterable[str] = (), min_confidence: float = 0.9) -> Universe:
    """identifier_map rows (id_type, id_value, issuer_id, valid_to, confidence) -> Universe. An id mapped to more than
    one current issuer is ambiguous and dropped (never guessed)."""
    im = identifier_map
    if "valid_to" in im.columns:
        im = im.filter(pl.col("valid_to").is_null())
    if "confidence" in im.columns:
        im = im.filter(pl.col("confidence") >= min_confidence)
    im = im.filter(pl.col("id_type").is_in(["edinet_code", "jp_sec_code"]) & pl.col("issuer_id").is_not_null())
    im = im.select("id_type", pl.col("id_value").cast(pl.Utf8).str.strip_chars().str.to_uppercase(), "issuer_id").unique()
    amb = im.group_by("id_type", "id_value").agg(pl.col("issuer_id").n_unique().alias("n")).filter(pl.col("n") > 1)
    if amb.height:
        log.warning("edinet_listed: %d ambiguous EDINET / securities codes dropped", amb.height)
        im = im.join(amb.select("id_type", "id_value"), on=["id_type", "id_value"], how="anti")
    by_edinet = dict(im.filter(pl.col("id_type") == "edinet_code").select("id_value", "issuer_id").iter_rows())
    by_sec = {sec4(k): v for k, v in im.filter(pl.col("id_type") == "jp_sec_code").select("id_value", "issuer_id").iter_rows()}
    seed = {by_sec[s] for s in (sec4(t.split(".")[0]) for t in seed_tickers) if s in by_sec}
    tick = {v: k for k, v in by_sec.items()}
    return Universe(by_edinet, by_sec, dict(codelist or {}), seed, tick)


def load_universe(settings: Settings, raw_dir: Path | None = None) -> Universe:
    from app.core.storage import scan_table

    im = (scan_table("identifier_map", settings)
          .filter(pl.col("id_type").is_in(["edinet_code", "jp_sec_code"]))
          .select("id_type", "id_value", "issuer_id", "valid_to", "confidence").collect())
    seed = seed_universe(settings, countries=["JP"])
    ticks = seed.get_column("ticker").to_list() if seed.height and "ticker" in seed.columns else []
    return build_universe(im, read_codelist(raw_dir or settings.raw_dir), ticks)


def _lookup(e: pl.Expr, mapping: dict[str, str]) -> pl.Expr:
    if not mapping:
        return pl.lit(None, pl.Utf8)
    return e.replace_strict(mapping, default=None, return_dtype=pl.Utf8)


def sec4_expr(col: str) -> pl.Expr:
    s = pl.col(col).cast(pl.Utf8).str.strip_chars().str.to_uppercase()
    return (pl.when((s.str.len_chars() == 5) & s.str.ends_with("0")).then(s.str.slice(0, 4))
            .when(s.str.len_chars() > 0).then(s).otherwise(None))


def map_docs(docs: pl.DataFrame, uni: Universe) -> pl.DataFrame:
    """Add issuer_id / map_via ('edinet_code' | 'jp_sec_code' | null) / sec4 / conflict columns."""
    if docs.is_empty():
        return docs.with_columns(pl.lit(None, pl.Utf8).alias("issuer_id"), pl.lit(None, pl.Utf8).alias("map_via"),
                                 pl.lit(None, pl.Utf8).alias("sec4"), pl.lit(False).alias("conflict"))
    ed = pl.col("edinetCode").fill_null("").str.to_uppercase()
    df = docs.with_columns(pl.coalesce(sec4_expr("secCode"), _lookup(ed, uni.sec_of_edinet)).alias("sec4"))
    df = df.with_columns(_lookup(ed, uni.by_edinet).alias("__ie"), _lookup(pl.col("sec4"), uni.by_sec).alias("__is"))
    return df.with_columns(
        pl.coalesce("__ie", "__is").alias("issuer_id"),
        pl.when(pl.col("__ie").is_not_null()).then(pl.lit("edinet_code"))
        .when(pl.col("__is").is_not_null()).then(pl.lit("jp_sec_code")).otherwise(None).alias("map_via"),
        (pl.col("__ie").is_not_null() & pl.col("__is").is_not_null() & (pl.col("__ie") != pl.col("__is")))
        .fill_null(False).alias("conflict"),
    ).drop("__ie", "__is")


def loaded_doc_ids(settings: Settings) -> set[str]:
    """docIDs whose CSV is already loaded (filings_index edinet rows with a doc_path)."""
    from app.core.storage import scan_table

    try:
        fi = (scan_table("filings_index", settings)
              .filter((pl.col("source_id") == SOURCE_ID) & pl.col("doc_path").is_not_null())
              .select("filing_id").collect())
    except Exception as e:  # noqa: BLE001
        log.warning("edinet_listed: filings_index unreadable (%s); assuming nothing loaded", e)
        return set()
    return {f.split(":", 1)[1] for f in fi.get_column("filing_id").to_list() if ":" in f}


def ledger_path(settings: Settings) -> Path:
    return settings.state_dir / f"{STATE_ID}_docs.tsv"


def read_ledger(settings: Settings, next_seq: int | None = None) -> tuple[dict[str, int], set[str], int]:
    """The append-only commit ledger: '<seq>\\t<docID>' per committed document, 'redo\\t<docID>' for a document whose
    commit died and was rolled back (durable even if the state file is lost). -> ({docID: seq} committed in a seq
    < next_seq (all when None), docIDs to redo (no commit since their redo line), highest seq of any line (0 if
    none))."""
    p = ledger_path(settings)
    loaded: dict[str, int] = {}
    redo: set[str] = set()
    top = 0
    if not p.exists():
        return loaded, redo, top
    for line in p.read_text(encoding="utf-8").splitlines():
        tag, _, doc = line.partition("\t")
        if not doc:
            continue
        if tag == "redo":
            redo.add(doc)
            loaded.pop(doc, None)
        elif tag.isdigit():
            top = max(top, int(tag))
            if next_seq is None or int(tag) < next_seq:
                loaded[doc] = int(tag)
                redo.discard(doc)
    return loaded, redo, top


def ledger_docs(settings: Settings, next_seq: int) -> set[str]:
    """docIDs whose facts this job appended in a committed seq (< next_seq). Guards against a duplicate append when
    another writer overwrites a filings_index row of a loaded document without its doc_path."""
    return set(read_ledger(settings, next_seq)[0])


def read_state(settings: Settings) -> dict[str, Any]:
    """data/state/edinet_listed.json; {} when absent. Unlike _global_common.load_state an unreadable file raises: the
    state holds the pending-commit record, which must never silently vanish."""
    p = state_path(STATE_ID, settings)
    if not p.exists():
        return {}
    try:
        out = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"edinet_listed: state file {p} is unreadable ({e}); repair it before the next run (if it "
                           "is removed, the next commit seq is re-derived from the ledger and the files on disk, "
                           "but a pending commit is only found through its files)") from e
    if not isinstance(out, dict):
        raise RuntimeError(f"edinet_listed: state file {p} does not hold an object")
    return out


def landed_zips(raw_dir: Path) -> dict[str, Path]:
    root = raw_dir / SOURCE_ID / "xbrl_csv"
    if not root.exists():
        return {}
    return {p.name: p for p in root.glob("dt=*/*") if p.is_file() and not p.name.endswith(".tmp")}


def filter_dims(facts: pl.DataFrame, policy: str) -> pl.DataFrame:
    if facts.is_empty() or policy == "all":
        return facts
    nodim = pl.col("dimensions_hash").is_null() | (pl.col("dimensions_hash") == "")
    if policy == "none":
        return facts.filter(nodim)
    if policy == "segments":
        return facts.filter(nodim | pl.col("dimensions_json").fill_null("").str.contains(_SEGMENT.pattern))
    raise ValueError(f"dims must be all | segments | none, not {policy!r}")


# ------------------------------------------------------------------------------------------------ the job


class ListedRun:
    """One run of the all-listed EDINET loader. `settings` is the write target (lake, raw, state, tmp); `read_settings`
    (default = settings) supplies the reference data, the landed lists / zips index and the loaded-docs set, so a
    validation can read the live lake read-only and write to a scratch data dir."""

    def __init__(self, settings: Settings | None = None, *, read_settings: Settings | None = None,
                 params: dict[str, Any] | None = None, adapter: EdinetAdapter | None = None,
                 universe: Universe | None = None, cancel: Any = None):
        p = dict(params or {})
        self.s = settings or get_settings()
        self.rs = read_settings or self.s
        self.mode = str(p.get("mode") or "backfill").lower()
        if self.mode not in ("backfill", "recent"):
            raise ValueError(f"mode must be backfill | recent, not {self.mode!r}")
        today = datetime.now(UTC).date()
        self.today = today
        if self.mode == "recent":
            self.since = to_date(p.get("since")) or today - timedelta(days=int(p.get("since_days", 14)))
        else:
            self.since = to_date(p.get("since")) or (today - timedelta(days=round(365.25 * int(p.get("backfill_years", 10)))))
        self.until = to_date(p.get("until")) or today
        self.doc_types = doc_types_for(_list(p.get("doc_classes")) or None)
        self.issuer_keys = _list(p.get("issuers") or p.get("tickers") or p.get("entities"))
        self.max_docs = int(p.get("max_docs", 0) or 0)  # 0 = no cap
        self.max_minutes = float(p.get("max_minutes", 0) or 0)
        self.chunk_docs = max(1, int(p.get("chunk_docs", 50)))
        self.commit_docs = max(self.chunk_docs, int(p.get("commit_docs", 1000)))
        self.dims = str(p.get("dims") or "segments").lower()
        filter_dims(pl.DataFrame(), self.dims)  # validates
        if self.dims not in DIMS_FACTOR:
            raise ValueError(f"dims must be all | segments | none, not {self.dims!r}")
        self.order = str(p.get("order") or "newest").lower()
        self.max_attempts = max(1, int(p.get("max_attempts", 5)))
        rf = p.get("retry_failed")
        rf = "true" if rf is True else ("" if rf in (None, False) else str(rf).strip().lower())
        if rf not in RETRY_FAILED:
            raise ValueError(f"retry_failed must be one of {sorted(k for k in RETRY_FAILED if k)}, not {rf!r}")
        self.retry_reasons = RETRY_FAILED[rf]
        self.compact_files = int(p.get("compact_files", 8))
        self.dry_run = _bool(p.get("dry_run"))
        self.refresh_lists = str(p.get("refresh_lists") or ("missing" if self.mode == "recent" else "none")).lower()
        self._adapter = adapter
        self._universe = universe
        self.cancel = cancel
        self.stats: Counter = Counter()
        self.t0 = time.time()

    # ---------------------------------------------------------------- pieces
    @property
    def adapter(self) -> EdinetAdapter:
        if self._adapter is None:
            self._adapter = EdinetAdapter(settings=self.s)
        return self._adapter

    def universe(self) -> Universe:
        if self._universe is None:
            self._universe = load_universe(self.rs)
        u = self._universe
        return u.restrict(self.issuer_keys) if self.issuer_keys else u

    def _fetch_missing_lists(self, have: dict[date, Path]) -> int:
        """recent mode: day lists of the window that are not on disk (e.g. edinet_filings failed) -> fetched + landed."""
        n = 0
        d = max(self.since, self.today - timedelta(days=31))
        while d <= min(self.until, self.today):
            if d not in have and d.weekday() < 5:
                lst = safe_call(lambda d=d: self.adapter.day_list(d), FetchResult(SOURCE_ID, datetime.now(UTC)),
                                f"list:{d}", log)
                if lst:
                    self.adapter.save_raw("lists", f"documents_{d.isoformat()}", lst, day=self.today)
                    n += 1
            d += timedelta(days=1)
        return n

    def plan(self) -> tuple[pl.DataFrame, dict[str, Any]]:
        """-> (queue, summary). The queue holds the docs to load, newest first."""
        files = list_files(self.rs.raw_dir, self.since, self.until)
        if self.refresh_lists == "missing" and not self.dry_run:
            if self._fetch_missing_lists(files):
                files = list_files(self.rs.raw_dir, self.since, self.until)
        st: Counter = Counter()
        docs = scan_lists(files, self.doc_types, st)
        uni = self.universe()
        docs = map_docs(docs, uni)
        state = read_state(self.s)
        pend = state.get("pending") or {}
        # lines of a pending (dead, not yet recovered: dry run) commit do not count as loaded
        nxt = int(pend["seq"]) if pend.get("seq") else self._next_seq(state)
        led_loaded, led_redo, _ = read_ledger(self.s, nxt)
        loaded = loaded_doc_ids(self.rs) | set(led_loaded)
        if self.rs is not self.s:
            loaded |= loaded_doc_ids(self.s)
        pending = set(pend.get("docs") or []) | set(state.get("redo") or []) | led_redo
        loaded -= pending  # a commit that died half-way is redone (recover() moved its docs to 'redo')
        failed = state.get("failed") or {}
        dead = {k for k, v in failed.items()
                if int(v.get("n", 0)) >= self.max_attempts and v.get("reason") not in self.retry_reasons}
        mapped = docs.filter(pl.col("issuer_id").is_not_null())
        unm = docs.filter(pl.col("issuer_id").is_null())
        done_m = pl.col("docID").is_in(list(loaded))
        dead_m = pl.col("docID").is_in(list(dead))
        queue = mapped.filter(~done_m & ~dead_m)
        queue = queue.with_columns(pl.col("docTypeCode").replace_strict(CLASS_OF, default="other").alias("doc_class"))
        queue = queue.sort(["submitDateTime", "docID"], descending=self.order != "oldest")
        if self.max_docs:
            queue = queue.head(self.max_docs)
        cls = queue.group_by("doc_class").len()
        est_rows = int(sum(AVG_FACTS.get(c, 400) * n for c, n in cls.iter_rows()) * DIMS_FACTOR[self.dims])
        n_dl = queue.filter(~pl.col("docID").is_in(list(self._zips()))).height if queue.height else 0
        yrs = (queue.with_columns(pl.col("submitDateTime").str.slice(0, 4).alias("year")).group_by("year", "doc_class").len()
               .sort("year", "doc_class"))
        summary = {
            "window": [self.since.isoformat(), self.until.isoformat()], "mode": self.mode,
            "restricted": bool(self.issuer_keys),  # issuers param: 'unmapped' counts the other issuers' docs too
            "list_days": len(files), "list_first": min(files).isoformat() if files else None,
            "list_last": max(files).isoformat() if files else None,
            "candidates": docs.height, "mapped": mapped.height, "already_loaded": mapped.filter(done_m).height,
            "failed_permanent": mapped.filter(dead_m & ~done_m).height, "todo": queue.height, "todo_downloads": n_dl,
            "pending_redo": len(pending),
            "unmapped": {"mapping_gap": unm.filter(pl.col("sec4").is_not_null()).height,   # listed code, not in reference
                         "out_of_scope": unm.filter(pl.col("sec4").is_null()).height},     # unlisted filer / fund
            "conflicts": int(docs.get_column("conflict").sum() or 0) if docs.height else 0,
            "issuers_universe": len(uni.issuers), "issuers_with_docs": mapped.get_column("issuer_id").n_unique(),
            "issuers_todo": queue.get_column("issuer_id").n_unique(),
            "issuers_todo_non_seed": queue.filter(~pl.col("issuer_id").is_in(list(uni.seed_issuers))).get_column("issuer_id").n_unique(),
            "todo_by_class": dict(cls.iter_rows()),
            "todo_by_year": {f"{y}:{c}": n for y, c, n in yrs.iter_rows()},
            "est_minutes": round(n_dl * SEC_PER_DOC / 60 + (queue.height - n_dl) * 0.15 / 60, 1),
            "est_fact_rows": est_rows, "dims": self.dims, "lists": dict(st),
        }
        return queue, summary

    def _zips(self) -> dict[str, Path]:
        """Landed zips by docID: the write target's raw dir, plus (validation mode) the read side's (read-only)."""
        z = landed_zips(self.s.raw_dir)
        return z if self.rs is self.s else {**landed_zips(self.rs.raw_dir), **z}

    def _budget_left(self) -> bool:
        if self.cancel is not None and getattr(self.cancel, "is_set", lambda: False)():
            return False
        return not self.max_minutes or (time.time() - self.t0) / 60 < self.max_minutes

    # ---------------------------------------------------------------- download + parse
    def _download(self, docs: list[dict[str, Any]], zips: dict[str, Path], fetched: FetchResult) -> list[dict[str, Any]]:
        out = []
        key = None
        for x in docs:
            p = zips.get(x["docID"])
            if p is None:
                if key is None:
                    key = self.adapter.key("edinet_apikey")

                def dl(x=x, key=key) -> Path:
                    r = self.adapter.http.get(f"documents/{x['docID']}", params={"type": 5, "Subscription-Key": key},
                                              cache_ttl=0)
                    if not r.content.startswith(b"PK"):
                        raise ValueError(f"not a zip: {r.content[:160]!r}")
                    return self.adapter.save_raw("xbrl_csv", x["docID"], r.content, compress=False)

                p = safe_call(dl, fetched, x["docID"], log)
                self.stats["downloaded" if p is not None else "download_failed"] += 1
            else:
                self.stats["reused_zip"] += 1
                fetched.entities_ok.append(x["docID"])
            out.append({k: x.get(k) for k in KEEP} | ({"_raw_path": str(p)} if p is not None else {}))
        return out

    def _parse(self, landed: list[dict[str, Any]], fetched: FetchResult, issuer_of: dict[str, str]
               ) -> tuple[pl.DataFrame, pl.DataFrame]:
        fetched.meta["docs"] = landed
        tables = self.adapter.normalize(fetched)
        fil, facts = tables["filings_index"], tables["fundamentals_raw"]
        facts = filter_dims(facts, self.dims)
        # a document whose CSV failed to parse is all-or-nothing: no (partial) facts and no doc_path, so it never
        # counts as loaded and params retry_failed=parse re-queues it after a parser fix without duplicates
        bad = [f"{SOURCE_ID}:{k.split(':', 1)[1]}" for k in fetched.entities_failed if k.startswith("parse:")]
        if bad:
            if facts.height:
                facts = facts.filter(~pl.col("filing_id").is_in(bad))
            if fil.height:
                fil = fil.with_columns(pl.when(pl.col("filing_id").is_in(bad)).then(None)
                                       .otherwise(pl.col("doc_path")).alias("doc_path"))
        m = pl.DataFrame({"filing_id": [f"{SOURCE_ID}:{k}" for k in issuer_of], "__iid": list(issuer_of.values())},
                         schema={"filing_id": pl.Utf8, "__iid": pl.Utf8})
        if facts.height:
            facts = facts.join(m, on="filing_id", how="left").with_columns(
                pl.coalesce("issuer_id", "__iid").alias("issuer_id")).drop("__iid")
        if fil.height:
            fil = fil.join(m, on="filing_id", how="left").with_columns(
                pl.coalesce("issuer_id", "__iid").alias("issuer_id")).drop("__iid")
        return fil, facts

    # ---------------------------------------------------------------- commit protocol
    def _raw_root(self) -> Path:
        from app.core.storage import table_dir

        return table_dir("fundamentals_raw", self.s) / f"source_id={SOURCE_ID}"

    def _work_dir(self, name: str) -> Path:
        """data/tmp/edinet_listed/<spill | stage | trash>: the lake's volume (data_dir) but outside every table tree, so
        no reader (scan_table, DuckDB '**/*.parquet' globs, partition upserts) ever sees a partial or retired file."""
        return self.s.data_dir / "tmp" / STATE_ID / name

    def _seq_files(self) -> dict[int, list[Path]]:
        """Committed, not yet compacted part-edl-<seq>.parquet files by seq."""
        out: dict[int, list[Path]] = {}
        for f in self._raw_root().glob(f"fiscal_year=*/{FILE_PREFIX}*.parquet"):
            m = SEQ_RE.match(f.name)
            if m:
                out.setdefault(int(m.group(1)), []).append(f)
        return out

    def _next_seq(self, st: dict[str, Any]) -> int:
        """Never a seq already used, even when the state file was lost or reset: max(state seq, 1 + the ledger's
        highest seq, 1 + the highest part-edl-<seq> on disk)."""
        return max(int(st.get("seq", 1) or 1), 1 + read_ledger(self.s)[2], 1 + max(self._seq_files(), default=0))

    @staticmethod
    def _purge(files: Iterable[Path]) -> int:
        n = 0
        for f in files:
            if f.exists():
                _retry(f.unlink)
                n += 1
        return n

    def _ledger_redo(self, drop_seqs: set[int], redo_docs: Iterable[str]) -> None:
        """Rewrite the ledger without the lines of drop_seqs, plus 'redo' lines for redo_docs (atomic replace)."""
        lp = ledger_path(self.s)
        lines = lp.read_text(encoding="utf-8").splitlines() if lp.exists() else []
        drop = {str(q) for q in drop_seqs}
        keep = [ln for ln in lines if ln.partition("\t")[0] not in drop]
        keep += [f"redo\t{d}" for d in sorted(set(redo_docs))]
        lp.parent.mkdir(parents=True, exist_ok=True)
        tmp = lp.with_suffix(".tmp")
        tmp.write_text("".join(f"{ln}\n" for ln in keep), encoding="utf-8")
        _retry(tmp.replace, lp)

    def recover(self) -> None:
        """Before anything else of a run, under the fundamentals_raw lock:
        1. a compaction that died half-way (state 'compacting') is resolved (_resolve_compaction);
        2. a commit that died half-way (state 'pending'): its ledger lines become 'redo' lines (durable before any
           file is deleted), its part-edl-<seq> files are deleted and its docs kept in state 'redo' - plan() re-queues
           them whatever the window until a commit loads them again; the seq number is reused;
        3. part-edl files above the ledger's highest seq (a dead commit whose state record was lost) likewise: their
           docIDs are read from the files and written as 'redo' lines before the files are deleted;
        4. the stage / trash dirs (and in-partition temp files of the pre-2026-11-07 layout) are emptied."""
        from app.core.storage import table_lock

        with table_lock("fundamentals_raw", self.s):
            st = read_state(self.s)
            if st.get("compacting"):
                self._resolve_compaction(st["compacting"])
                st = read_state(self.s)
                st.pop("compacting", None)
                save_state(STATE_ID, st, self.s)
            pend = st.get("pending") or {}
            if pend:
                seq = int(pend["seq"])
                docs = set(pend.get("docs") or [])
                self._ledger_redo({seq}, docs)
                n = self._purge(self._seq_files().get(seq, []))
                st = read_state(self.s)
                st["redo"] = sorted(set(st.get("redo") or []) | docs)
                st["seq"] = seq
                st.pop("pending", None)
                save_state(STATE_ID, st, self.s)
                log.warning("edinet_listed: recovered pending commit seq=%s (%d docs to redo, %d files deleted)", seq,
                            len(docs), n)
            if ledger_path(self.s).exists():
                top = read_ledger(self.s)[2]
                orphans = {q: fs for q, fs in self._seq_files().items() if q > top}
                if orphans:
                    files = [f for fs in orphans.values() for f in fs]
                    ids = (pl.scan_parquet([str(f) for f in files], hive_partitioning=False)
                           .select(pl.col("filing_id").unique()).collect().get_column("filing_id").drop_nulls().to_list())
                    docs = {i.split(":", 1)[1] for i in ids if ":" in i}
                    self._ledger_redo(set(), docs)
                    n = self._purge(files)
                    st = read_state(self.s)
                    st["redo"] = sorted(set(st.get("redo") or []) | docs)
                    save_state(STATE_ID, st, self.s)
                    log.warning("edinet_listed: part-edl seqs %s have no ledger lines (dead commit, state record lost): "
                                "%d files deleted, %d docs to redo", sorted(orphans), n, len(docs))
            self._clean_work()

    def _clean_work(self) -> None:
        """Only under the fundamentals_raw lock with no 'compacting' marker: nothing in stage / trash is needed."""
        for name in ("stage", "trash"):
            shutil.rmtree(self._work_dir(name), ignore_errors=True)
        root = self._raw_root()
        for p in list(root.glob(f"fiscal_year=*/.tmp-{FILE_PREFIX}*")) + list(root.glob(f"fiscal_year=*/.old-{FILE_PREFIX}*")):
            try:
                p.unlink()
            except OSError as e:
                log.warning("edinet_listed: cannot remove leftover %s: %s", p, e)

    def _commit(self, spill: Path, docs: list[str]) -> tuple[int, int]:
        from app.core.schema import conform, get_contract
        from app.core.storage import partition_dir, physical_frame, table_dir, table_lock, write_table

        facts_files = sorted(spill.glob("facts-*.parquet"))
        fil_files = sorted(spill.glob("filings-*.parquet"))
        if not facts_files and not fil_files:
            return 0, 0
        c = get_contract("fundamentals_raw")
        pcols = list(c.partition_by)
        root = table_dir(c, self.s)
        stage = self._work_dir("stage")
        stage.mkdir(parents=True, exist_ok=True)
        n_f = n_i = 0
        with table_lock("fundamentals_raw", self.s):
            # ingested_at is stamped per file right before it is written (seconds before it becomes visible):
            # fundamentals_normalize selects raw rows by ingested_at > its last start minus WM_OVERLAP (15 min), so a
            # parse-time stamp (up to commit_docs downloads earlier) or a commit-start stamp could be skipped for good
            t_commit = time.time()
            st = read_state(self.s)
            if st.get("pending"):
                raise RuntimeError(f"edinet_listed: commit seq={st['pending'].get('seq')} is pending; run recover() first")
            seq = self._next_seq(st)
            name = f"{FILE_PREFIX}{seq:06d}.parquet"
            st["seq"] = seq
            st["pending"] = {"seq": seq, "docs": docs, "at": _now()}
            save_state(STATE_ID, st, self.s)
            if facts_files:
                lf = pl.scan_parquet([str(f) for f in facts_files], hive_partitioning=False)
                years = sorted(lf.select(pl.col("fiscal_year").unique()).collect().get_column("fiscal_year").to_list())
                for y in years:
                    part = lf.filter(pl.col("fiscal_year") == y).collect()
                    part = conform(part.with_columns(pl.lit(datetime.now(UTC)).alias("ingested_at")), c)
                    if part.is_empty():
                        continue
                    pdir = partition_dir(root, pcols, [SOURCE_ID, y])
                    pdir.mkdir(parents=True, exist_ok=True)
                    tmp = stage / f"{pdir.name}-{name}"
                    physical_frame(part.drop(pcols), c).write_parquet(
                        tmp, compression="zstd", compression_level=c.compression_level, statistics=True)
                    _move(tmp, pdir / name)  # a new name: _next_seq is above every seq on disk, nothing is replaced
                    n_f += part.height
                    del part
            if fil_files:
                fil = pl.concat([pl.read_parquet(f) for f in fil_files], how="diagonal_relaxed").with_columns(
                    pl.lit(datetime.now(UTC)).alias("ingested_at"))
                n_i = write_table(conform(fil, "filings_index"), "filings_index", settings=self.s)
            if docs:
                with ledger_path(self.s).open("a", encoding="utf-8") as fh:
                    fh.writelines(f"{seq}\t{d}\n" for d in docs)
            st = read_state(self.s)
            st["seq"] = seq + 1
            st.pop("pending", None)
            if st.get("redo"):
                st["redo"] = [d for d in st["redo"] if d not in set(docs)]
            st["committed_docs"] = int(st.get("committed_docs", 0)) + len(docs)
            st["committed_rows"] = int(st.get("committed_rows", 0)) + n_f
            st["last_commit"] = {"seq": seq, "docs": len(docs), "rows": n_f, "at": _now(),
                                 "sec": round(time.time() - t_commit, 1)}
            save_state(STATE_ID, st, self.s)
        for f in facts_files + fil_files:
            f.unlink(missing_ok=True)
        return n_f, n_i

    def compact(self, min_files: int | None = None) -> int:
        """Merge the part-edl files of a fiscal-year partition into one (streaming copy, low RAM). Crash-safe: the merged
        file is staged outside the table tree and its row count verified; a 'compacting' marker {partition, inputs,
        merged, trash} is saved; then the merged file is published and the inputs are moved to the trash dir (a reader
        sees duplicates for a moment, never missing rows); the marker is cleared. An input that cannot be moved
        (Windows: open by a reader) rolls the merge back; recover() resolves a marker left by a crash. Stops before
        the next partition when the run is cancelled or out of time (left to the next run)."""
        from app.core.storage import table_lock

        k = self.compact_files if min_files is None else min_files
        if k <= 1:
            return 0
        merged = 0
        for pdir in sorted(self._raw_root().glob("fiscal_year=*")):
            if len(list(pdir.glob(f"{FILE_PREFIX}*.parquet"))) < k:
                continue
            if not self._budget_left():
                self.stats["compaction_deferred"] += 1
                break
            with table_lock("fundamentals_raw", self.s):
                files = sorted(pdir.glob(f"{FILE_PREFIX}*.parquet"))
                if len(files) < k:
                    continue
                tag = uuid.uuid4().hex[:8]
                stage = self._work_dir("stage")
                stage.mkdir(parents=True, exist_ok=True)
                tmp = stage / f"{pdir.name}-m{tag}.parquet"
                n_in = sum(pl.scan_parquet(str(f), hive_partitioning=False).select(pl.len()).collect().item() for f in files)
                pl.scan_parquet([str(f) for f in files], hive_partitioning=False).sink_parquet(
                    tmp, compression="zstd", statistics=True)
                if pl.scan_parquet(str(tmp), hive_partitioning=False).select(pl.len()).collect().item() != n_in:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"compaction row count mismatch in {pdir}")
                final = pdir / f"{FILE_PREFIX}m{tag}.parquet"
                marker = {"pdir": str(pdir), "inputs": [f.name for f in files], "merged": final.name,
                          "trash": str(self._work_dir("trash") / f"{pdir.name}-m{tag}"), "at": _now()}
                st = read_state(self.s)
                st["compacting"] = marker
                save_state(STATE_ID, st, self.s)
                _move(tmp, final)
                done = self._finish_compaction(marker)
                st = read_state(self.s)
                st.pop("compacting", None)
                save_state(STATE_ID, st, self.s)
                shutil.rmtree(marker["trash"], ignore_errors=True)
                if done:
                    merged += len(files)
                else:
                    log.warning("edinet_listed: compaction of %s rolled back (file in use)", pdir.name)
        return merged

    def _finish_compaction(self, m: dict[str, Any]) -> bool:
        """Move the inputs of a published merge out of the partition (to its trash dir). On a PermissionError (an input
        open by a reader) roll back instead: the moved inputs are restored first, then the merged copy is moved out, so
        the partition never misses rows. -> True when merged, False when rolled back. Raises (marker kept for
        recover()) only if the rollback itself fails."""
        pdir, trash = Path(m["pdir"]), Path(m["trash"])
        trash.mkdir(parents=True, exist_ok=True)
        try:
            for name in m["inputs"]:
                if (pdir / name).exists():
                    _move(pdir / name, trash / name)
            return True
        except PermissionError:
            for name in m["inputs"]:
                if (trash / name).exists() and not (pdir / name).exists():
                    _move(trash / name, pdir / name)
            final = pdir / m["merged"]
            if final.exists():
                _move(final, trash / final.name)
            return False

    def _resolve_compaction(self, m: dict[str, Any]) -> None:
        """A 'compacting' marker left by a crash (called by recover() under the lock)."""
        pdir = Path(m["pdir"])
        final = pdir / m["merged"]
        missing = [n for n in m["inputs"] if not (pdir / n).exists()]
        if not final.exists():
            # died before publishing, or after a completed rollback: the inputs are the data
            if missing:
                log.warning("edinet_listed: compaction marker of %s: merged file and %d inputs are gone (partition "
                            "rewritten by another writer?)", pdir.name, len(missing))
            return
        if not missing:
            Path(m["trash"]).mkdir(parents=True, exist_ok=True)
            _move(final, Path(m["trash"]) / final.name)  # published, no input moved yet: drop the merged copy
            log.warning("edinet_listed: compaction of %s died after publishing; merged copy removed", pdir.name)
            return
        done = self._finish_compaction(m)  # moves in progress: the merged file holds every row (count verified)
        log.warning("edinet_listed: compaction of %s died while moving inputs; %s", pdir.name,
                    "moves finished" if done else "rolled back")

    # ---------------------------------------------------------------- main
    def run(self) -> dict[str, Any]:
        from filelock import FileLock, Timeout

        self.s.locks_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.s.locks_dir / f"{LOCK_NAME}.lock"))
        try:
            lock.acquire(timeout=1)
        except Timeout:
            return {"status": "skipped", "message": "another edinet_listed run holds the lock"}
        try:
            return self._run()
        finally:
            lock.release()

    def _run(self) -> dict[str, Any]:
        if not self.dry_run:
            self.recover()
            if self.retry_reasons:  # params retry_failed: those failures start over (counts reset)
                st = read_state(self.s)
                f = st.get("failed") or {}
                st["failed"] = {k: v for k, v in f.items() if v.get("reason") not in self.retry_reasons}
                self.stats["retry_failed"] = len(f) - len(st["failed"])
                save_state(STATE_ID, st, self.s)
        queue, summary = self.plan()
        out: dict[str, Any] = {"summary": summary, "rows": {"fundamentals_raw": 0, "filings_index": 0},
                               "docs_ok": 0, "docs_failed": 0}
        if self.dry_run:
            st = read_state(self.s)
            st["last_plan"] = {**summary, "at": _now()}
            save_state(STATE_ID, st, self.s)
            out["status"] = "success"
            out["message"] = _plan_msg(summary, dry=True)
            return out
        if queue.is_empty():
            out["compacted_files"] = self.compact()  # e.g. one deferred by a cancelled / timed-out run
            out["status"] = "success"
            out["message"] = _plan_msg(summary) + "; nothing to load"
            return out
        if summary["todo_downloads"]:
            self.adapter.check_keys()
        spill = self._work_dir("spill")
        shutil.rmtree(spill, ignore_errors=True)  # uncommitted work of a dead run: redone (zips are on disk)
        for old in spill.parent.glob("*.parquet"):  # spill files of the pre-2026-11-07 layout (data/tmp/edinet_listed/)
            old.unlink(missing_ok=True)
        spill.mkdir(parents=True, exist_ok=True)
        zips = self._zips()
        rows = queue.select(*KEEP, "issuer_id").to_dicts()
        pend_docs: list[str] = []
        n_batch = 0
        n_spilled = 0
        failed_all: dict[str, str] = {}
        stopped = None
        circuit = None

        def commit() -> None:
            nonlocal pend_docs, n_spilled
            nf, ni = self._commit(spill, pend_docs)
            out["rows"]["fundamentals_raw"] += nf
            out["rows"]["filings_index"] += ni
            out["docs_ok"] += len(pend_docs)
            pend_docs, n_spilled = [], 0

        for i in range(0, len(rows), self.chunk_docs):
            if not self._budget_left():
                stopped = "time budget" if self.max_minutes else "cancelled"
                break
            batch = rows[i:i + self.chunk_docs]
            fetched = FetchResult(SOURCE_ID, started_at=datetime.now(UTC))
            landed = self._download(batch, zips, fetched)
            issuer_of = {x["docID"]: x["issuer_id"] for x in batch}
            fil, facts = self._parse(landed, fetched, issuer_of)
            n_batch += 1
            if facts.height:
                facts.write_parquet(spill / f"facts-{n_batch:05d}.parquet")
            if fil.height:
                fil.write_parquet(spill / f"filings-{n_batch:05d}.parquet")
            del facts, fil
            unparsable = {k.split(":", 1)[1] for k in fetched.entities_failed if k.startswith("parse:")}
            ok_docs = [x["docID"] for x in landed if x.get("_raw_path") and x["docID"] not in unparsable]
            pend_docs += ok_docs
            n_spilled += len(batch)
            failed_all.update(fetched.entities_failed)
            self.stats["parse_failed"] += sum(1 for k in fetched.entities_failed if k.startswith("parse:"))
            self._record_failures(fetched.entities_failed, ok_docs)
            if fetched.meta.get("_circuit_open"):
                circuit = fetched.meta["_circuit_open"]
            if n_spilled >= self.commit_docs or circuit:
                commit()
            if circuit:
                stopped = f"circuit open ({circuit})"
                break
        if n_spilled:
            commit()
        shutil.rmtree(spill, ignore_errors=True)
        out["compacted_files"] = self.compact()  # nothing when cancelled / out of time: left to the next run
        out["docs_failed"] = len([k for k in failed_all if not k.startswith("parse:")])
        out["parse_failed"] = self.stats["parse_failed"]
        out["stats"] = dict(self.stats)
        remaining = max(0, summary["todo"] - out["docs_ok"] - out["docs_failed"] - out["parse_failed"])
        st = read_state(self.s)
        st["last_run"] = {"at": _now(), "mode": self.mode, "docs_ok": out["docs_ok"], "docs_failed": out["docs_failed"],
                          "rows": out["rows"], "remaining": remaining, "stopped": stopped,
                          "minutes": round((time.time() - self.t0) / 60, 1), "stats": dict(self.stats)}
        save_state(STATE_ID, st, self.s)
        out["remaining"] = remaining
        if circuit:
            out["status"] = "partial"
        elif (out["docs_failed"] or out["parse_failed"]) and not out["docs_ok"]:
            out["status"] = "failed"
        else:
            n_bad = out["docs_failed"] + out["parse_failed"]
            out["status"] = "partial" if n_bad > max(5, 0.05 * max(1, out["docs_ok"])) else "success"
        out["message"] = (f"{out['docs_ok']} docs loaded ({out['rows']['fundamentals_raw']} facts), "
                          f"{out['docs_failed']} failed, {out['parse_failed']} unparsable, {remaining} remaining"
                          + (f"; stopped: {stopped} (resume: run again)" if stopped else "")
                          + f" ({(time.time() - self.t0) / 60:.1f} min)")
        if failed_all:
            out["message"] += f"; e.g. {redact(str(list(failed_all.items())[:2]))[:300]}"
        return out

    def _record_failures(self, failed: dict[str, str], ok: list[str]) -> None:
        st = read_state(self.s)
        f = st.setdefault("failed", {})
        for d in ok:
            if d in f and not any(k == f"parse:{d}" for k in failed):
                f.pop(d, None)
        for k, msg in failed.items():
            if str(msg).startswith("circuit open"):
                continue  # never attempted (source down): not an attempt against the document
            doc = k.split(":", 1)[1] if k.startswith("parse:") else k
            reason = "pipeline_bug" if k.startswith("parse:") else "source_unavailable"
            r = f.setdefault(doc, {"n": 0})
            r["n"] = int(r.get("n", 0)) + 1
            r.update({"reason": reason, "err": redact(str(msg))[:200], "at": _now()})
            if reason == "pipeline_bug":
                r["n"] = self.max_attempts  # unparsable CSV: not retried until the parser changes (retry_failed=parse)
        save_state(STATE_ID, st, self.s)


def _retry(fn, *args, attempts: int = 8, delay: float = 0.25):
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (2 ** i))


def _move(src: Path, dst: Path) -> None:
    """Rename src onto dst (atomic on one volume; retried while a reader holds a file). Should data/tmp ever sit on
    another volume than the lake, fall back to a copy beside dst + rename (dst never appears partially written)."""
    try:
        _retry(src.replace, dst)
    except PermissionError:
        raise
    except OSError as e:
        if e.errno != errno.EXDEV and getattr(e, "winerror", None) != 17:  # 17 = ERROR_NOT_SAME_DEVICE
            raise
        tmp = dst.parent / f".tmp-{dst.name}-{uuid.uuid4().hex[:6]}"
        shutil.copyfile(src, tmp)
        _retry(tmp.replace, dst)
        _retry(src.unlink)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _plan_msg(s: dict[str, Any], dry: bool = False) -> str:
    return (f"{'plan' if dry else 'queue'} {s['window'][0]}..{s['window'][1]}: {s['candidates']} candidate docs, "
            f"{s['mapped']} mapped ({s['already_loaded']} loaded, {s['failed_permanent']} failed), todo {s['todo']} "
            f"({s['todo_downloads']} downloads, ~{s['est_minutes']} min, ~{s['est_fact_rows']:,} facts) for "
            f"{s['issuers_todo']} issuers ({s['issuers_todo_non_seed']} non-seed); unmapped "
            f"{s['unmapped']['mapping_gap']} mapping_gap / {s['unmapped']['out_of_scope']} out_of_scope")


def run(ctx: Any) -> Any:
    """Job entry: edinet_listed (nightly, params.mode=recent) / edinet_listed_backfill (manual, mode=backfill)."""
    from app.ingestion.base import MissingApiKey
    from app.scheduler.base import JobResult

    params = dict(getattr(ctx, "params", {}) or {})  # ctx.watermark is not used: the queue is the state
    r = ListedRun(ctx.settings, params=params, cancel=getattr(ctx, "cancel_event", None))
    try:
        out = r.run()
    except MissingApiKey as e:
        return JobResult(status="skipped", message=f"missing key: {e}")
    if out.get("status") == "skipped":
        return JobResult(status="skipped", message=out.get("message"))
    a = r._adapter
    return JobResult(status=out["status"], rows_written={k: v for k, v in out.get("rows", {}).items() if v},
                     entities_ok=out.get("docs_ok", 0), entities_failed=out.get("docs_failed", 0),
                     http_requests=a.http.n_requests if a else 0, cache_hits=a.http.n_cache_hits if a else 0,
                     message=out.get("message"))
