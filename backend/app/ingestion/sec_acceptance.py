"""SEC EDGAR acceptance times: correction of the submissions API's `acceptanceDateTime` (known bug, PHASE3 / guide
05c "SEC times are currently 4 hours late (5 in winter)").

What EDGAR actually serves (measured 2026-10-27 against the SGML header ACCEPTANCE-DATETIME (US/Eastern) of the
same accessions, and against the real-time Atom feed whose <updated> carries an explicit -04:00/-05:00 offset):
* most submissions files serve their settled entries LATE by exactly the Eastern UTC offset: Apple's 16:30 ET 8-K of
  30 Jul 2026 reads 2026-07-31T00:30:28Z (true 20:30:28Z), its 18:30 ET Form 4 of Nov 2024 reads 04:30Z (+5 h, EST),
  in the recent block and in the older `files[]` pages alike (2015-2026 checked);
* but it is per FILE, not per filing: some issuers' files are right for every entry (Dianthus' 2024 8-K reads the
  true 21:03:32Z), and entries added since a late file was last regenerated are mostly right; rarely the ET
  wall-clock time is labelled "Z" (4 h EARLY).
So `correct_acceptance` picks among three candidates
  as_is (A)  the value as UTC;   shifted (B)  value - ET offset;   et_wall (C)  the wall clock read as US/Eastern
using what EDGAR guarantees:
  1. EDGAR accepts filings 06:00-22:00 ET on business days;
  2. the filing date is the acceptance day, or the next business day when accepted after 17:30 ET (22:00 ET for
     Forms 3/4/5 and Schedules 13D/G); candidates that contradict the filing date are dropped;
  3. among the survivors: A for an entry fetched < 2 days after its filing date (fresh), else the FILE's vote
     (`vote`: entries plausible only shifted vs only as is, over the whole block / all stored rows of the entity;
     B when there is no evidence), then the other reading, then C.
On 1,105 accessions labelled with the true time this is right for 1,081 (97.8%); face value is right for ~40%. A true
time from another source (`truth`: the Atom feed via the bus / live_filings, the SGML header via
insider_transactions) always wins. Method codes: truth | as_is | shifted | et_wall | raw (nothing plausible: the
preferred reading).

Backfill (`python -m app.ingestion.sec_acceptance [--apply] [--force]`): filings_index.knowledge_ts and
filing_news.ts (source sec_8k) were stored at face value; they are recomputed from the stored value with the vote of
the entity's stored rows (one entity = one submissions file: source_entity_id / issuer_id), the form, the filing date
and the row's ingested_at, truth first; the report gives the rule's accuracy on the rows whose true time is known.
Forms 3/4/5 rows of filings_index already carry the SGML header time (sec_form4) and change only where a true time
is known. filing_news `date` follows the corrected ts; the row's story stays on the day events_v2 merged it.
Recorded in data/state/sec_acceptance_fix.json (a table is never corrected twice for the same FIX_VERSION).
earnings_events / earnings_calendar / corporate_events are rebuilt in full by the nightly earnings job from the landed
submissions files, so they pick up the fixed parser on their next run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = time(6, 0), time(22, 0)
STD_CUTOFF = time(17, 31)                    # everything else: after 17:30 ET -> next business day
SETTLED_DAYS = 2
STATE_FILE = "sec_acceptance_fix.json"
FIX_VERSION = 2


def base_form(form: str | None) -> str:
    return (form or "").strip().upper().removesuffix("/A").strip()


def late_cutoff_form(form: str | None) -> bool:
    b = base_form(form)
    return b in ("3", "4", "5") or b.startswith(("SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"))


def _next_bday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _d(x: Any) -> date | None:
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def plausible(ts: datetime, form: str | None, filing_date: date | None) -> bool:
    e = ts.astimezone(ET)
    if e.weekday() >= 5 or not (OPEN <= e.time() <= CLOSE):
        return False
    if filing_date is None:
        return True
    cutoff = CLOSE if late_cutoff_form(form) else STD_CUTOFF
    expected = e.date() if e.time() <= cutoff else _next_bday(e.date())
    return expected == filing_date


def to_utc(raw: str | datetime | None) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def candidates(raw: datetime) -> dict[str, datetime]:
    """as_is / shifted (late by the ET offset) / et_wall (ET wall clock labelled Z)."""
    out = {"as_is": raw}
    for h in (4, 5):                      # the true time's ET offset decides the shift (EDT 4 h, EST 5 h)
        c = raw - timedelta(hours=h)
        off = -c.astimezone(ET).utcoffset().total_seconds() / 3600   # type: ignore[union-attr]
        if round(off) == h:
            out["shifted"] = c
            break
    out.setdefault("shifted", raw - timedelta(hours=4))
    out["et_wall"] = raw.replace(tzinfo=None).replace(tzinfo=ET).astimezone(UTC)
    return out


def evidence(raw: str | datetime | None, form: str | None, filing_date: date | None) -> int:
    """+1 = only the shifted reading is plausible, -1 = only the value as is, 0 = both / neither."""
    r = to_utc(raw)
    if r is None:
        return 0
    c = candidates(r)
    a = plausible(c["as_is"], form, filing_date)
    b = plausible(c["shifted"], form, filing_date)
    return 1 if (b and not a) else -1 if (a and not b) else 0


def vote_counts(entries: Iterable[tuple[Any, Any, Any]]) -> tuple[int, int]:
    """(entries plausible only shifted, entries plausible only as is); entries = (raw, form, filing date)."""
    n_b = n_a = 0
    for raw, form, fd in entries:
        e = evidence(raw, form, _d(fd))
        n_b += e == 1
        n_a += e == -1
    return n_b, n_a


def _decide(n_b: int, n_a: int) -> bool | None:
    return None if n_a == n_b else n_b > n_a


def vote(entries: Iterable[tuple[Any, Any, Any]]) -> bool | None:
    """Is this submissions file (or an entity's stored rows) late by the ET offset? True / False by majority of the
    decisive entries, None without evidence."""
    return _decide(*vote_counts(entries))


def block_vote(block: dict[str, Any]) -> bool | None:
    """vote() over a submissions JSON block (recent block / older page: columnar lists)."""
    return vote(zip(block.get("acceptanceDateTime") or [], block.get("form") or [], block.get("filingDate") or [],
                    strict=False))


def correct_acceptance(raw: str | datetime | None, form: str | None = None, filing_date: date | None = None,
                       fetched_on: date | None = None, truth: datetime | None = None,
                       file_shifted: bool | None = None) -> tuple[datetime | None, str]:
    """-> (corrected UTC acceptance time, method). See the module docstring."""
    if truth is not None:
        return (truth if truth.tzinfo else truth.replace(tzinfo=UTC)).astimezone(UTC), "truth"
    r = to_utc(raw)
    if r is None:
        return None, "none"
    cands = candidates(r)
    ok = {k: v for k, v in cands.items() if plausible(v, form, filing_date)}
    fresh = fetched_on is not None and filing_date is not None and (fetched_on - filing_date).days < SETTLED_DAYS
    pref = "as_is" if (fresh or file_shifted is False) else "shifted"
    for k in (pref, "shifted" if pref == "as_is" else "as_is", "et_wall"):
        if k in ok:
            return ok[k], k
    return cands[pref], "raw"


def parse_acceptance_utc(s: str | None, form: str | None = None, filing_date: Any = None, fetched_on: Any = None,
                         truth: datetime | None = None, file_shifted: bool | None = None) -> datetime | None:
    """submissions acceptanceDateTime -> corrected aware UTC datetime (see correct_acceptance)."""
    return correct_acceptance(s, form, _d(filing_date), _d(fetched_on), truth, file_shifted)[0]


# ------------------------------------------------------------------------------------------------ truth sources
def load_truth(settings: Settings | None = None, accessions: Iterable[str] | None = None,
               include_insider: bool = True) -> dict[str, datetime]:
    """accession -> true acceptance (UTC) from the EDGAR Atom feed (bus `filings` + lake live_filings) and the SGML
    headers parsed into insider_transactions (Forms 3/4/5)."""
    import polars as pl  # noqa: PLC0415

    from app.core.storage import scan_table  # noqa: PLC0415

    s = settings or get_settings()
    want = set(accessions) if accessions is not None else None
    out: dict[str, datetime] = {}

    def put(acc: str | None, ts: Any) -> None:
        if not acc or ts is None or (want is not None and acc not in want):
            return
        t = to_utc(ts)
        if t is not None:
            out.setdefault(acc, t)

    try:   # bus (Atom, last 14 days)
        from app.realtime.bus import SqliteBus, default_bus_path  # noqa: PLC0415

        p = default_bus_path(s)
        if p.exists():
            bus = SqliteBus(p, read_only=True)
            try:
                for acc, ts in bus._con().execute(
                        "SELECT json_extract(payload, '$.accession'), ts_source FROM messages WHERE topic = 'filings' "
                        "AND key LIKE 'sec_edgar:%'").fetchall():
                    put(acc, ts)
            finally:
                bus.close()
    except Exception as e:  # noqa: BLE001
        log.debug("truth from bus unavailable: %s", e)
    for table, col in (("live_filings", "source_ts"), ("insider_transactions", "knowledge_ts")):
        if table == "insider_transactions" and not include_insider:
            continue
        try:
            lf = scan_table(table, s).filter(pl.col("source_id") == "sec_edgar").select(
                pl.col("filing_id").str.replace("^sec_edgar:", "").alias("acc"), pl.col(col).alias("ts"))
            if want is not None:
                lf = lf.filter(pl.col("acc").is_in(list(want)))
            for acc, ts in lf.drop_nulls().unique("acc").collect().iter_rows():
                put(acc, ts)
        except Exception as e:  # noqa: BLE001 - table missing on a fresh install
            log.debug("truth from %s unavailable: %s", table, e)
    return out


# ------------------------------------------------------------------------------------------------ backfill
def _state_path(settings: Settings):
    return settings.state_dir / STATE_FILE


def read_state(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    try:
        return json.loads(_state_path(s).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def raw_reference(settings: Settings, want: dict[str, set[str]]) -> tuple[dict[str, tuple[Any, date | None]],
                                                                          dict[str, tuple[int, int]]]:
    """From the latest landed submissions file of each CIK: accession -> (raw value, landing day) for the wanted
    accessions, and CIK -> the file's vote counts (n_b, n_a). Missing files are skipped."""
    from app.earnings.sec_history import _load, cik10, landing_date, latest_raw_index  # noqa: PLC0415

    idx = latest_raw_index(settings)
    refs: dict[str, tuple[Any, date | None]] = {}
    counts: dict[str, tuple[int, int]] = {}
    for cik, accs in want.items():
        try:
            c10 = cik10(cik)
        except (TypeError, ValueError):
            continue
        p = idx.get(c10)
        if p is None:
            continue
        doc = _load(p) or {}
        blk = (doc.get("filings") or {}).get("recent") or {}
        counts[cik] = vote_counts(zip(blk.get("acceptanceDateTime") or [], blk.get("form") or [],
                                      blk.get("filingDate") or [], strict=False))
        land = landing_date(p)
        for a, r in zip(blk.get("accessionNumber") or [], blk.get("acceptanceDateTime") or [], strict=False):
            if a in accs:
                refs[a] = (to_utc(r), land)
    return refs, counts


def resolve(stored: Any, form: Any, fd: Any, ing: Any, truth: datetime | None, ent_late: bool | None,
            ref: tuple[Any, date | None] | None = None) -> tuple[datetime | None, str]:
    """One stored face-value time -> (corrected, method), using the latest landed file of the CIK when it lists it."""
    if truth is not None:
        return correct_acceptance(stored, truth=truth)
    st = to_utc(stored)
    if st is not None and ref is not None and ref[0] is not None:
        r, land = ref
        if r == st:   # stored = the file's current value: the file's vote applies, fetched on its landing day
            return correct_acceptance(st, form, _d(fd), land, None, ent_late)
        if st == candidates(r)["shifted"]:   # stored before the file went late: it was right
            return st, "as_is_ref"
        if r == candidates(st)["shifted"]:   # stored late, the file serves the right value now
            return r, "ref"
    return correct_acceptance(st, form, _d(fd), _d(ing), None, ent_late)


def correct_rows(accs: list[str], ts: list[Any], forms: list[Any], fds: list[Any], ings: list[Any],
                 entities: list[Any], truth: dict[str, datetime], eligible: list[bool] | None = None,
                 refs: dict[str, tuple[Any, date | None]] | None = None,
                 file_counts: dict[Any, tuple[int, int]] | None = None,
                 entity_file: dict[Any, Any] | None = None) -> tuple[list[Any], list[bool], dict[str, Any]]:
    """Stored face-value acceptance times -> (corrected values, changed flags, report incl. accuracy on truth rows).
    Entity vote = the entity's stored rows' evidence + its submissions file's (entity_file: entity -> CIK)."""
    groups: dict[Any, list[tuple[Any, Any, Any]]] = {}
    for t, f, fd, ent in zip(ts, forms, fds, entities, strict=True):
        if t is not None and ent is not None:
            groups.setdefault(ent, []).append((t, f, fd))
    votes: dict[Any, bool | None] = {}
    for e, v in groups.items():
        n_b, n_a = vote_counts(v)
        fb, fa = (file_counts or {}).get((entity_file or {}).get(e, e), (0, 0))
        votes[e] = _decide(n_b + fb, n_a + fa)
    refs = refs or {}
    new: list[Any] = []
    changed: list[bool] = []
    counts: dict[str, int] = {}
    hit = miss = face_hit = 0
    for i, (acc, t, f, fd, ing, ent) in enumerate(zip(accs, ts, forms, fds, ings, entities, strict=True)):
        if t is None or (eligible is not None and not eligible[i]):
            new.append(t)
            changed.append(False)
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        tr = truth.get(acc)
        if tr is not None and ent is not None:   # accuracy of the rule where the true time is known
            guess, _m = resolve(t, f, fd, ing, None, votes.get(ent), refs.get(acc))
            hit += guess == tr
            miss += guess != tr
            face_hit += to_utc(t) == tr
        v, m = resolve(t, f, fd, ing, tr, votes.get(ent), refs.get(acc))
        counts[m] = counts.get(m, 0) + 1
        new.append(v)
        changed.append(v != to_utc(t))
    n = hit + miss
    rep = {"methods": counts, "entities": len(votes), "entities_late": sum(1 for v in votes.values() if v),
           "entities_right": sum(1 for v in votes.values() if v is False), "with_file_reference": len(refs),
           "check_vs_truth": {"rows": n, "rule_right": hit, "face_value_right": face_hit,
                              "rule_accuracy": round(hit / n, 4) if n else None,
                              "face_value_accuracy": round(face_hit / n, 4) if n else None}}
    return new, changed, rep


def issuer_ciks(settings: Settings) -> dict[str, str]:
    """issuer_id -> CIK (current serving snapshot; {} when unavailable)."""
    try:
        import duckdb  # noqa: PLC0415

        from app.core.storage import current_serving_db  # noqa: PLC0415

        p = current_serving_db(settings)
        if p is None:
            return {}
        con = duckdb.connect(str(p), read_only=True)
        try:
            return dict(con.execute("SELECT issuer_id, cik FROM issuers WHERE cik IS NOT NULL").fetchall())
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        log.debug("issuer CIKs unavailable: %s", e)
        return {}


def backfill(settings: Settings | None = None, apply: bool = False, force: bool = False,
             tables: Iterable[str] = ("filings_index", "filing_news")) -> dict[str, Any]:
    """Correct stored SEC acceptance times once (dry run unless apply). Returns per-table counts.

    A table already corrected for this FIX_VERSION (state file) is skipped; `force` then only touches rows ingested
    after the recorded cutoff (rows written by the old parser between the backfill and the restart)."""
    import polars as pl  # noqa: PLC0415

    from app.core.storage import read_table, write_table  # noqa: PLC0415

    s = settings or get_settings()
    st = read_state(s)
    now = datetime.now(UTC)
    rep: dict[str, Any] = {"apply": apply, "version": FIX_VERSION, "tables": {}}
    done = st.get("tables") or {}
    truth = load_truth(s)
    rep["truth_accessions"] = len(truth)
    for table in tables:
        prev = done.get(table) or {}
        lo = None
        if prev.get("version") == FIX_VERSION:
            if not force:
                rep["tables"][table] = {"status": "already applied", **prev}
                continue
            lo = to_utc(prev.get("cutoff_ingested_at"))
        extra: dict[str, Any] = {}
        try:
            if table == "filings_index":
                df = read_table(table, filters=(pl.col("source_id") == "sec_edgar") & pl.col("knowledge_ts").is_not_null(),
                                settings=s)
                ts_col, ing = "knowledge_ts", df.get_column("ingested_at").to_list()
                accs = df.get_column("filing_id").str.replace("^sec_edgar:", "").to_list()
                forms = df.get_column("form_type").to_list()
                # Forms 3/4/5 carry the SGML header time written by sec_form4 (measured right for 99.9%): changed only
                # where a true time is known, and kept out of the per-entity vote
                s16 = [base_form(f) in ("3", "4", "5") for f in forms]
                ents = [None if x else e for x, e in zip(s16, df.get_column("source_entity_id").to_list(), strict=True)]
                elig = [(lo is None or (x is not None and x > lo)) and (not y or a in truth)
                        for x, y, a in zip(ing, s16, accs, strict=True)]
                want: dict[str, set[str]] = {}
                for e, a, ok in zip(ents, accs, elig, strict=True):
                    if e and ok:
                        want.setdefault(e, set()).add(a)
                refs, fcounts = raw_reference(s, want)
                new, changed, info = correct_rows(accs, df.get_column(ts_col).to_list(), forms,
                                                  df.get_column("knowledge_date").to_list(), ing, ents, truth, elig,
                                                  refs, fcounts)
                extra["section16_kept"] = sum(1 for y, e in zip(s16, elig, strict=True) if y and not e)
                fixed = df.with_columns(pl.Series(ts_col, new, dtype=df.schema[ts_col]), pl.Series("__changed", changed))
            elif table == "filing_news":
                df = read_table(table, filters=pl.col("source_id") == "sec_8k", settings=s)
                if df.is_empty():
                    rep["tables"][table] = {"status": "empty"}
                    continue
                ids = df.get_column("filing_id").to_list()
                fi = read_table("filings_index", columns=["filing_id", "knowledge_date"],
                                filters=pl.col("filing_id").is_in(ids), settings=s)
                fdm = dict(fi.iter_rows())
                ing = df.get_column("ingested_at").to_list()
                accs = [i.removeprefix("sec_edgar:") for i in ids]
                iss = df.get_column("issuer_id").to_list()
                elig = [lo is None or (x is not None and x > lo) for x in ing]
                icik = issuer_ciks(s)
                want = {}
                for e, a, ok in zip(iss, accs, elig, strict=True):
                    if ok and icik.get(e):
                        want.setdefault(icik[e], set()).add(a)
                refs, fcounts = raw_reference(s, want)
                new, changed, info = correct_rows(accs, df.get_column("ts").to_list(), ["8-K"] * df.height,
                                                  [fdm.get(i) for i in ids], ing, iss, truth, elig, refs, fcounts, icik)
                fixed = df.with_columns(pl.Series("__new", new, dtype=df.schema["ts"]), pl.Series("__changed", changed))
                # never move a row across a year partition. The UTC `date` follows ts; the row's story stays where the
                # events_v2 merge put it (no mass re-merge on a live system; the next full events build re-days it)
                yr = (pl.col("__new").dt.year() != pl.col("year").cast(pl.Int32)) & pl.col("__changed")
                extra["skipped_year_boundary"] = int(fixed.select(yr.sum()).item())
                fixed = fixed.with_columns(pl.when(yr).then(False).otherwise(pl.col("__changed")).alias("__changed"))
                moved = pl.col("__changed") & (pl.col("__new").dt.date() != pl.col("date"))
                extra["date_moved"] = int(fixed.select(moved.sum()).item())
                fixed = fixed.with_columns(
                    pl.when(pl.col("__changed")).then(pl.col("__new").dt.date()).otherwise(pl.col("date")).alias("date"),
                    pl.when(pl.col("__changed")).then(pl.col("__new")).otherwise(pl.col("ts")).alias("ts"),
                ).drop("__new")
            else:
                rep["tables"][table] = {"status": "unknown table"}
                continue
        except Exception as e:  # noqa: BLE001 - table missing: nothing to correct
            rep["tables"][table] = {"status": f"unavailable: {type(e).__name__}: {e}"[:200]}
            continue
        n_changed = int(fixed.get_column("__changed").sum())
        entry = {"rows": df.height, "rows_corrected": n_changed, **info, **extra}
        if apply and n_changed:
            part_df = fixed.filter(pl.col("__changed")).drop("__changed")
            n = 0
            for (_y,), part in part_df.partition_by("year", as_dict=True).items():
                n += write_table(part, table, mode="upsert", settings=s)
            entry["rows_written"] = n
        if apply:
            done[table] = {"version": FIX_VERSION, "applied_at": now.isoformat(timespec="seconds"),
                           "cutoff_ingested_at": now.isoformat(timespec="seconds"), **entry}
        rep["tables"][table] = {"status": "applied" if apply else "dry run", **entry}
    if apply:
        p = _state_path(s)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": FIX_VERSION, "tables": done, "updated_at": now.isoformat(timespec="seconds")},
                                indent=1, default=str), encoding="utf-8")
    return rep


def main(argv: list[str] | None = None) -> int:
    from app.core.logsetup import setup_logging  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="app.ingestion.sec_acceptance",
                                 description="Correct stored SEC acceptance times (EDGAR acceptanceDateTime bug)")
    ap.add_argument("--apply", action="store_true", help="write the corrections (default: dry run)")
    ap.add_argument("--force", action="store_true", help="also rows ingested after the recorded cutoff")
    ap.add_argument("--tables", default="filings_index,filing_news")
    a = ap.parse_args(argv)
    setup_logging(log_file="cli.log", service="cli")
    rep = backfill(apply=a.apply, force=a.force, tables=[t for t in a.tables.split(",") if t])
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
