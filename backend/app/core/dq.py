"""Data-quality checks -> ops/dq_results (job `dq_checks`, owner: platform). ARCHITECTURE §13.

    from app.core.dq import run_checks, default_checks, dq_summary
    outcomes = run_checks(default_checks(), run_id="R...")      # evaluates + writes dq_results
    dq_summary()                                              # System page summary of the latest dq run

Rule framework: a Check evaluates against DuckDB views over the Parquet lake (app.core.storage.register_views) and
returns DqOutcome rows. Built-in rule types:
    SchemaConformance   column set / DuckDB types vs contract                         error
    PkUnique            duplicate primary keys across all partitions                  error
    RowCount            row count (info) + delta vs previous dq run (|Δ| > 50%)       warn
    NullRate            null share of a column vs absolute max or 2x previous run      warn
    Freshness           age of max(write-timestamp or date column) vs expected cadence warn / error at 3x
    ScheduledFreshness  datasets with an official publication calendar (13F, FINRA short interest; coverage.yaml
                        check quarterly_13f / finra_settlements): days past the publication deadline of the newest
                        published period not yet loaded (startup-sync rules) vs the SLA      warn / error at 3x
    ValueRange          share of rows violating a SQL predicate                       warn
    SqlMetric           any scalar SQL metric compared to a threshold (PIT, FX coverage ...)
    DateSanity          (A5) per table, one scan: dates in the future beyond a per-table tolerance, implausibly old
                        dates, knowledge dates before the event they describe (DATE_RULES / KNOWLEDGE_ORDER + generic
                        knowledge / write timestamps)                warn; error only when certainly wrong AND systemic
    hooks               register_hook(name, fn) for custom checks; the default `accounting_identity` hook checks
                        total_assets ~= total_liabilities + total_equity (+-2%) on fundamentals_wide.
Tables without data yield one `has_data` info row (never an error) so a fresh install does not block the serving
snapshot. Any failed `error` check makes the dq_checks job return failed (retryable=False): the runner then skips
build_serving_db and the previous snapshot stays live.

Schedule: the nightly DAG (gate before build_serving_db) and, since A5 2026-11, the startup / interval sync after a
publish at most every POST_PUBLISH_MIN_INTERVAL_H hours (post_publish_due; monitoring only, the snapshot is live).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import polars as pl

from .config import Settings, get_settings
from .logsetup import get_logger
from .schema import TableContract, get_contract, load_contracts
from .storage import _parquet_files, duckdb_connect, read_table, register_views, table_dir, write_table

log = get_logger(__name__)

SEVERITIES = ("info", "warn", "error")
WRITE_TS_COLUMNS = ("ingested_at", "computed_at", "updated_at", "checked_at", "started_at", "created_at")
#: expected max age (days) of a table's newest write by the cadence of the DAG that writes it
CADENCE_MAX_AGE_DAYS = {"nightly": 4.0, "weekly": 10.0, "monthly": 40.0}
SKIP_TABLES = {"dq_results"}  # never check our own output
PIT_MAX_VIOLATION_RATE = 0.001


@dataclass
class DqOutcome:
    table_name: str
    check_id: str
    severity: str
    passed: bool
    metric_value: float | None = None
    threshold: float | None = None
    details: dict[str, Any] | None = None
    partition_key: str = ""


class DqContext:
    """Shared state for one dq run: DuckDB connection with lake views and the previous run's metrics."""

    def __init__(self, run_id: str, settings: Settings | None = None, con: duckdb.DuckDBPyConnection | None = None,
                 now: datetime | None = None, tables: Iterable[str] | None = None):
        self.settings = settings or get_settings()
        self.run_id = run_id
        self.now = now or datetime.now(UTC)
        self.con = con or duckdb_connect(None, settings=self.settings)
        if con is None:
            register_views(self.con, tables, settings=self.settings)
        self._has: dict[str, bool] = {}
        self._prev: dict[tuple[str, str], float | None] | None = None

    def has_data(self, table: str) -> bool:
        if table not in self._has:
            c = get_contract(table)
            self._has[table] = c.is_parquet and bool(_parquet_files(table_dir(c, self.settings)))
        return self._has[table]

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        row = self.con.execute(sql, params or []).fetchone()
        return None if row is None else row[0]

    def columns(self, table: str) -> dict[str, str]:
        return {r[0]: r[1] for r in self.con.execute(f'DESCRIBE SELECT * FROM "{table}"').fetchall()}

    def prev_metric(self, table: str, check_id: str) -> float | None:
        """metric_value of the same table/check in the latest earlier dq run (baseline for deltas)."""
        if self._prev is None:
            self._prev = {}
            try:
                df = read_table("dq_results", settings=self.settings)
            except Exception:
                df = pl.DataFrame()
            if df.height:
                df = df.filter(pl.col("run_id") != self.run_id)
                if df.height:
                    last = df.sort("checked_at", descending=True)["run_id"][0]
                    for r in df.filter(pl.col("run_id") == last).iter_rows(named=True):
                        self._prev[(r["table_name"], r["check_id"])] = r["metric_value"]
        return self._prev.get((table, check_id))


class Check(ABC):
    check_id: str = "check"
    severity: str = "warn"

    def __init__(self, table: str, severity: str | None = None, check_id: str | None = None):
        self.table = table
        if severity:
            self.severity = severity
        if check_id:
            self.check_id = check_id
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid severity {self.severity!r}")

    def run(self, ctx: DqContext) -> list[DqOutcome]:
        try:
            return self.evaluate(ctx)
        except Exception as e:  # a broken check must not kill the dq run
            log.warning("dq check %s/%s errored: %s", self.table, self.check_id, e)
            return [DqOutcome(self.table, self.check_id, "warn", False, details={"check_error": str(e)[:500]})]

    @abstractmethod
    def evaluate(self, ctx: DqContext) -> list[DqOutcome]: ...

    def _out(self, passed: bool, metric: float | None = None, threshold: float | None = None,
             details: dict[str, Any] | None = None, severity: str | None = None, check_id: str | None = None) -> DqOutcome:
        return DqOutcome(self.table, check_id or self.check_id, severity or self.severity, bool(passed),
                         None if metric is None else float(metric), None if threshold is None else float(threshold), details)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SchemaConformance(Check):
    check_id, severity = "schema_conformance", "error"

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        c = get_contract(self.table)
        have = ctx.columns(self.table)
        problems: dict[str, Any] = {}
        missing_req = [col.name for col in c.columns if col.name not in have and not col.nullable]
        missing_opt = [col.name for col in c.columns if col.name not in have and col.nullable]
        bad_type = {col.name: f"{have[col.name]} != {col.duckdb}" for col in c.columns
                    if col.name in have and have[col.name].upper() != col.duckdb.upper()}
        unknown = [n for n in have if n not in c.column_names and not (c.dynamic and c.dynamic.matches(n))]
        if missing_req:
            problems["missing_required"] = missing_req
        if bad_type:
            problems["type_mismatch"] = bad_type
        if unknown:
            problems["unknown_columns"] = unknown[:20]
        if missing_opt:
            problems["missing_nullable"] = missing_opt[:20]
        hard = bool(missing_req or bad_type)
        sev = "error" if hard else ("warn" if unknown else "info")
        return [self._out(not hard and not unknown, len(missing_req) + len(bad_type) + len(unknown), 0,
                          problems or None, severity=sev)]


class PkUnique(Check):
    check_id, severity = "pk_unique", "error"

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        c = get_contract(self.table)
        if not c.primary_key:
            return []
        keys = ", ".join(_q(k) for k in c.primary_key)
        sql = (f'SELECT {keys}, COUNT(*) AS n FROM {_q(self.table)} GROUP BY {keys} HAVING COUNT(*) > 1 '
               f'ORDER BY n DESC LIMIT 5')
        sample = ctx.con.execute(sql).fetchall()
        n_dup = ctx.scalar(f"SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM {_q(self.table)} "
                           f"GROUP BY {keys} HAVING COUNT(*) > 1)")
        det = {"sample": [[str(v) for v in r] for r in sample]} if sample else None
        return [self._out(n_dup == 0, n_dup, 0, det)]


class RowCount(Check):
    """Row count (info) plus delta vs the previous dq run (warn when |Δ| > max_rel_change)."""

    check_id, severity = "row_count", "info"

    def __init__(self, table: str, max_rel_change: float = 0.5, **kw: Any):
        super().__init__(table, **kw)
        self.max_rel_change = max_rel_change

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        n = float(ctx.scalar(f"SELECT COUNT(*) FROM {_q(self.table)}") or 0)
        out = [self._out(True, n)]
        prev = ctx.prev_metric(self.table, "row_count")
        if prev is not None and prev > 0:
            delta = (n - prev) / prev
            out.append(self._out(abs(delta) <= self.max_rel_change, delta, self.max_rel_change,
                                 {"previous": prev, "current": n}, severity="warn", check_id="row_count_delta"))
        return out


class NullRate(Check):
    severity = "warn"

    def __init__(self, table: str, column: str, max_rate: float | None = None, baseline_mult: float = 2.0,
                 check_id: str | None = None, **kw: Any):
        super().__init__(table, check_id=check_id or f"null_rate:{column}", **kw)
        self.column, self.max_rate, self.baseline_mult = column, max_rate, baseline_mult

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        if self.column not in ctx.columns(self.table):
            return []
        rate = ctx.scalar(f"SELECT AVG(CASE WHEN {_q(self.column)} IS NULL THEN 1.0 ELSE 0.0 END) FROM {_q(self.table)}")
        rate = float(rate or 0.0)
        if self.max_rate is not None:
            return [self._out(rate <= self.max_rate, rate, self.max_rate)]
        prev = ctx.prev_metric(self.table, self.check_id)
        if prev is None:
            return [self._out(True, rate, None, {"baseline": "none yet"})]
        thr = max(prev * self.baseline_mult, prev + 0.01)
        return [self._out(rate <= thr, rate, thr, {"baseline": prev})]


class Freshness(Check):
    check_id, severity = "freshness", "warn"

    def __init__(self, table: str, column: str, max_age_days: float, error_mult: float = 3.0, **kw: Any):
        super().__init__(table, **kw)
        self.column, self.max_age_days, self.error_mult = column, max_age_days, error_mult

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        latest = ctx.scalar(f"SELECT MAX({_q(self.column)}) FROM {_q(self.table)}")
        if latest is None:
            return [self._out(False, None, self.max_age_days, {"reason": f"no non-null {self.column}"}, severity="info")]
        if isinstance(latest, datetime):
            lt = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
            age = (ctx.now - lt).total_seconds() / 86400.0
        else:
            age = float((ctx.now.date() - latest).days) if isinstance(latest, date) else 0.0
        sev = "error" if age > self.max_age_days * self.error_mult else self.severity
        return [self._out(age <= self.max_age_days, age, self.max_age_days, {"latest": str(latest), "column": self.column},
                          severity=sev)]


class ScheduledFreshness(Check):
    """Freshness of a dataset with an official publication calendar (W1 2026-10-31): the startup sync's expected-period
    rules (app.ops.sync.expect) instead of the write time. A table is fresh while its newest period is the newest one
    PUBLISHED (13F: quarter ends whose 45-day filing deadline has passed; FINRA short interest: settlement dates whose
    publication, 8 sessions later at 18:00 ET, has passed); otherwise metric = days since that publication deadline,
    warn beyond the coverage.yaml SLA and error beyond error_mult x SLA. The write-time rule failed both tables every
    night (identity_resolve, a nightly job, also writes them, so the generic rule expected a 4-day cadence for
    quarterly / bi-monthly data)."""

    check_id, severity = "freshness", "warn"
    KINDS = ("quarterly_13f", "finra_settlements")

    def __init__(self, table: str, kind: str, column: str, sla_days: float, error_mult: float = 3.0, **kw: Any):
        super().__init__(table, **kw)
        self.kind, self.column, self.sla_days, self.error_mult = kind, column, float(sla_days), error_mult

    def overdue(self, latest: date | None, now: datetime) -> tuple[float, dict[str, Any]]:
        from datetime import time as dtime  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415

        from app.ops.sync import expect as X  # noqa: PLC0415 - lazy: keeps core free of ops at import time

        if self.kind == "quarterly_13f":
            exp = X.expected_13f(now)
            det: dict[str, Any] = {"expected_period": str(exp), "next_deadline": X.next_13f(now).isoformat()}
            if latest is not None and latest >= exp:
                return 0.0, det
            # the oldest published quarter after `latest` is the one overdue the longest
            q = X.cal.quarter_end((latest or exp - timedelta(days=400)) + timedelta(days=1))
            q = min(q, exp)
            deadline = datetime.combine(q + timedelta(days=45), dtime(22, 0), UTC)
            return max(0.0, (now - deadline).total_seconds() / 86400.0), det | {"missing_period": str(q)}
        exp = X.finra_expected(now)
        nxt = X.next_finra(now)
        det = {"expected_period": str(exp), "next_publication": nxt.isoformat() if nxt else None}
        if exp is None or (latest is not None and latest >= exp - timedelta(days=3)):
            return 0.0, det
        start = (latest + timedelta(days=4)) if latest else now.date() - timedelta(days=75)
        pub = [sd for sd in X.finra_published(start, now)]
        if not pub:
            return 0.0, det
        return max(0.0, (now - X._finra_pub(pub[0])).total_seconds() / 86400.0), det | {"missing_period": str(pub[0])}

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        latest = ctx.scalar(f"SELECT MAX(CAST({_q(self.column)} AS DATE)) FROM {_q(self.table)}")
        days, det = self.overdue(latest, ctx.now)
        det = det | {"latest": str(latest), "column": self.column, "rule": f"publication calendar ({self.kind})"}
        sev = "error" if days > self.sla_days * self.error_mult else self.severity
        return [self._out(days <= self.sla_days, days, self.sla_days, det, severity=sev)]


def _scheduled_tables(settings: Settings) -> dict[str, tuple[str, str, float]]:
    """table -> (check kind, period column, SLA days) for coverage.yaml datasets with a publication calendar."""
    try:
        from app.ops.coverage import load_coverage  # noqa: PLC0415

        cov = load_coverage(settings)
    except Exception:  # noqa: BLE001 - no registry: the generic rule applies
        return {}
    out: dict[str, tuple[str, str, float]] = {}
    for d in (cov.get("datasets") or {}).values():
        if d.get("check") in ScheduledFreshness.KINDS and d.get("table"):
            col = d.get("date_column") or ("period_end" if d["check"] == "quarterly_13f" else "settlement_date")
            out[d["table"]] = (d["check"], col, float(d.get("sla", 14)))
    return out


class ValueRange(Check):
    """Share of rows where `violation_sql` (a SQL boolean over the table, or a full query via `from_sql`) is true."""

    severity = "warn"

    def __init__(self, table: str, check_id: str, violation_sql: str, max_violation_rate: float = 0.0,
                 from_sql: str | None = None, **kw: Any):
        super().__init__(table, check_id=check_id, **kw)
        self.violation_sql, self.max_rate, self.from_sql = violation_sql, max_violation_rate, from_sql

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        src = f"({self.from_sql})" if self.from_sql else _q(self.table)
        row = ctx.con.execute(f"SELECT COUNT(*) FILTER (WHERE {self.violation_sql}), COUNT(*) FROM {src} AS t").fetchone()
        bad, total = (row or (0, 0))
        rate = (bad / total) if total else 0.0
        return [self._out(rate <= self.max_rate, rate, self.max_rate, {"violations": int(bad), "rows": int(total)})]


class SqlMetric(Check):
    """Generic scalar metric: passes when `metric <op> threshold`."""

    severity = "warn"
    _OPS: dict[str, Callable[[float, float], bool]] = {
        "<=": lambda a, b: a <= b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
        "==": lambda a, b: a == b,
    }

    def __init__(self, table: str, check_id: str, sql: str, threshold: float, op: str = "<=",
                 requires: Iterable[str] = (), **kw: Any):
        super().__init__(table, check_id=check_id, **kw)
        self.sql, self.threshold, self.op, self.requires = sql, threshold, op, tuple(requires)

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        if any(not ctx.has_data(t) for t in self.requires):
            return []
        v = float(ctx.scalar(self.sql) or 0.0)
        return [self._out(self._OPS[self.op](v, self.threshold), v, self.threshold)]


# ------------------------------------------------------------------------------------------------ date sanity (A5)

#: a date before this is impossible for any filing, transaction or fiscal period we ingest: filer typos such as
#: 0016-03-16 / 0024-02-01 (for 2016 / 2024) survive EDGAR's ownership XML and the SEC data sets verbatim
DATE_IMPOSSIBLE_BEFORE = date(1900, 1, 1)
#: a certainly-wrong date is a (blocking) error only when it is systemic - a parse bug or a source format change:
#: violation rate > DATE_ERROR_RATE AND at least DATE_ERROR_MIN_ROWS rows. A handful of filer typos (2026-11: 36 Form 4
#: dates up to 2047 among 2.8 M rows) is a warning with counts and examples, so it never blocks the snapshot.
DATE_ERROR_RATE = PIT_MAX_VIOLATION_RATE
DATE_ERROR_MIN_ROWS = 10
DATE_EXAMPLES = 5


@dataclass(frozen=True)
class DateRule:
    """Plausibility of one date / timestamp column (DateSanity).

    future_days        tolerance beyond the run date (None: no future check); certain=True when a later value is
                       certainly wrong (the event already happened, or a knowledge / write date) -> error tier
    old_before         warn below this date (implausibly old)
    impossible_before  certainly wrong below this date -> error tier
    where / suffix     optional SQL filter of the rows the rule covers; the suffix keeps check ids unique"""

    column: str
    future_days: int | None = None
    certain: bool = False
    old_before: date | None = None
    impossible_before: date | None = None
    where: str | None = None
    suffix: str = ""


@dataclass(frozen=True)
class KnowledgeOrder:
    """knowledge_col must not precede event_col by more than tolerance_days: a filing / report cannot be public before
    the transaction, settlement or report date it describes. certain=False for period_end pairs, where the generic
    pit_violation rule (error above PIT_MAX_VIOLATION_RATE) stays the gate and this check adds the warning tier with
    examples."""

    event_col: str
    knowledge_col: str = "knowledge_date"
    tolerance_days: int = 0
    certain: bool = True
    where: str | None = None
    suffix: str = ""


class DateSanity(Check):
    """Date plausibility of one table in a single scan (A5 2026-11). One outcome per rule:
        date_future:<col>       col > run date + DateRule.future_days
        date_too_old:<col>      col < old_before (warn) / impossible_before (error tier)
        knowledge_before:<col>  knowledge_col < col - KnowledgeOrder.tolerance_days
    metric = violating share of the covered rows, threshold 0. Severity `error` only for a certainly-wrong violation
    that is systemic (DATE_ERROR_RATE / DATE_ERROR_MIN_ROWS), otherwise `warn`; details carry the counts, the
    min / max offending value and up to DATE_EXAMPLES example rows (primary key + the dates involved)."""

    check_id, severity = "date_sanity", "warn"

    def __init__(self, table: str, rules: Iterable[DateRule] = (), order: Iterable[KnowledgeOrder] = (), **kw: Any):
        super().__init__(table, **kw)
        self.rules, self.order = tuple(rules), tuple(order)

    def _specs(self, cols: dict[str, str], ref: date) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for r in self.rules:
            if r.column not in cols:
                continue
            d, w = f"CAST({_q(r.column)} AS DATE)", f"({r.where})" if r.where else "TRUE"
            if r.future_days is not None:
                lim = ref + timedelta(days=int(r.future_days))
                v = f"{d} > DATE '{lim.isoformat()}'"
                specs.append({"check_id": f"date_future:{r.column}{r.suffix}", "scope": w, "bad": v,
                              "certain": v if r.certain else None, "col": r.column, "dates": [r.column],
                              "desc": True, "det": {"tolerance_days": r.future_days, "limit": lim.isoformat()}})
            floors = [x for x in (r.old_before, r.impossible_before) if x is not None]
            if floors:
                v = f"{d} < DATE '{max(floors).isoformat()}'"
                imp = (f"{d} < DATE '{r.impossible_before.isoformat()}'" if r.impossible_before else None)
                specs.append({"check_id": f"date_too_old:{r.column}{r.suffix}", "scope": w, "bad": v, "certain": imp,
                              "col": r.column, "dates": [r.column], "desc": False,
                              "det": {"old_before": str(r.old_before) if r.old_before else None,
                                      "impossible_before": str(r.impossible_before) if r.impossible_before else None}})
        for o in self.order:
            if o.event_col not in cols or o.knowledge_col not in cols:
                continue
            v = (f"CAST({_q(o.knowledge_col)} AS DATE) < CAST({_q(o.event_col)} AS DATE) - {int(o.tolerance_days)}")
            specs.append({"check_id": f"knowledge_before:{o.event_col}{o.suffix}",
                          "scope": f"({o.where})" if o.where else "TRUE", "bad": v, "certain": v if o.certain else None,
                          "col": o.event_col, "dates": [o.event_col, o.knowledge_col], "desc": True,
                          "det": {"knowledge_col": o.knowledge_col, "tolerance_days": o.tolerance_days}})
        return specs

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        cols = ctx.columns(self.table)
        specs = self._specs(cols, ctx.now.date())
        if not specs:
            return []
        t = _q(self.table)
        aggs = []
        for s in specs:
            c = _q(s["col"])
            aggs += [f"COUNT(*) FILTER (WHERE {s['scope']})", f"COUNT(*) FILTER (WHERE {s['scope']} AND {s['bad']})",
                     f"COUNT(*) FILTER (WHERE {s['scope']} AND {s['certain'] or 'FALSE'})",
                     f"MIN(CASE WHEN {s['scope']} AND {s['bad']} THEN {c} END)",
                     f"MAX(CASE WHEN {s['scope']} AND {s['bad']} THEN {c} END)"]
        row = ctx.con.execute(f"SELECT {', '.join(aggs)} FROM {t}").fetchone() or ()
        try:
            pk = [k for k in get_contract(self.table).primary_key if k in cols][:4]
        except KeyError:
            pk = []
        out: list[DqOutcome] = []
        for i, s in enumerate(specs):
            n, bad, cert, lo, hi = row[i * 5: i * 5 + 5]
            n, bad, cert = int(n or 0), int(bad or 0), int(cert or 0)
            rate = bad / n if n else 0.0
            systemic = s["certain"] is not None and cert >= DATE_ERROR_MIN_ROWS and cert / max(n, 1) > DATE_ERROR_RATE
            det: dict[str, Any] = {"violations": bad, "rows": n, **{k: v for k, v in s["det"].items() if v is not None}}
            if s["certain"] is not None:
                det |= {"certainly_wrong": cert, "error_rate": DATE_ERROR_RATE, "error_min_rows": DATE_ERROR_MIN_ROWS}
            if bad:
                det |= {"min": str(lo), "max": str(hi), "examples": self._examples(ctx, s, pk)}
            sev = "error" if systemic else "warn"
            out.append(self._out(bad == 0, rate, 0.0, det, severity=sev, check_id=s["check_id"]))
        return out

    def _examples(self, ctx: DqContext, s: dict[str, Any], pk: list[str]) -> list[list[str]]:
        show = list(dict.fromkeys(pk + s["dates"]))
        sel = ", ".join(_q(c) for c in show)
        order = f"{_q(s['col'])} {'DESC' if s['desc'] else 'ASC'}"
        try:
            rows = ctx.con.execute(f"SELECT {sel} FROM {_q(self.table)} WHERE {s['scope']} AND {s['bad']} "
                                   f"ORDER BY {order} LIMIT {DATE_EXAMPLES}").fetchall()
        except Exception as e:  # noqa: BLE001 - examples are best effort
            return [[f"examples unavailable: {str(e)[:120]}"]]
        return [show] + [[str(v) for v in r] for r in rows]


_PERIODIC_FORMS = "form_class IN ('annual', 'semiannual', 'quarterly')"
#: a transaction date (Form 3/4/5, PDMR): already happened when filed; 1990 is before any insider source we load
_TXN_DATE = DateRule("transaction_date", 1, True, old_before=date(1990, 1, 1), impossible_before=DATE_IMPOSSIBLE_BEFORE)
#: a fiscal period end: forecasts (TDnet tanshin ForecastMember rows, next fiscal year) lie up to ~1 year ahead
_PERIOD_END = DateRule("period_end", 731, False, old_before=date(1950, 1, 1), impossible_before=DATE_IMPOSSIBLE_BEFORE)


def _observed(col: str, **kw: Any) -> DateRule:
    """An observation / settlement / report / filing date: in the past by construction (1-day timezone tolerance)."""
    return DateRule(col, 1, True, **kw)


#: per-table date rules (A5). Tolerances from the 2026-11-06 lake profile: scheduled-event tables legitimately hold
#: future dates (earnings expectations to ~2.2 y, central-bank meetings and futures expiries to ~2 y, Polygon's
#: scheduled preferred dividends to ~4 y), so their future checks only catch garbage (e.g. year 4172) and only warn.
DATE_RULES: dict[str, tuple[DateRule, ...]] = {
    "insider_transactions": (_TXN_DATE,),
    "insider_txn_scores": (_TXN_DATE,),
    "filings_index": (
        # a periodic report covers a period that has ended; 8-K / 6-K / proxy "periods" may lie a little ahead
        DateRule("period_end", 1, True, old_before=date(1990, 1, 1), impossible_before=DATE_IMPOSSIBLE_BEFORE,
                 where=_PERIODIC_FORMS, suffix=":periodic"),
        DateRule("period_end", 400, False, where=f"NOT ({_PERIODIC_FORMS}) OR form_class IS NULL", suffix=":other_forms"),
        DateRule("knowledge_date", 1, True, old_before=date(1990, 1, 1), impossible_before=DATE_IMPOSSIBLE_BEFORE),
    ),
    "fundamentals_raw": (_PERIOD_END, DateRule("period_start", None, old_before=date(1950, 1, 1),
                                               impossible_before=DATE_IMPOSSIBLE_BEFORE)),
    "fundamentals_canonical": (_PERIOD_END,),
    "fundamentals_wide": (_PERIOD_END,),
    "ratios": (_PERIOD_END,),
    "sector_kpis": (_PERIOD_END,),
    "earnings_events": (_PERIOD_END,),
    "earnings_reactions": (_PERIOD_END,),
    "institutional_holdings": (_observed("period_end", old_before=date(1990, 1, 1)),),
    "short_interest": (_observed("settlement_date"),),
    "fails_to_deliver": (_observed("settlement_date"),),
    "eu_short_positions": (_observed("position_date"), _observed("publication_date")),
    "activist_filings": (_observed("filed_date"),),
    "cftc_positions": (_observed("report_date"),),
    "prices_daily": (_observed("date", old_before=DATE_IMPOSSIBLE_BEFORE),),
    "fx_rates": (_observed("date"),),
    "yield_curves": (_observed("date"),),
    "futures_curve": (_observed("date"), DateRule("expiry", 1830)),
    # feeds with unreliable publisher dates: warn only
    "news_items": (DateRule("published_at", 2),),
    "gdelt_events": (DateRule("event_date", 2),),
    # scheduled events (warn only, wide tolerance); Polygon scheduled corporate actions carry knowledge_date = the
    # event date (conservative PIT), so corporate_events.knowledge_date is not a write-time knowledge date
    "corporate_actions": (DateRule("ex_date", 1830, impossible_before=DATE_IMPOSSIBLE_BEFORE),),
    "corporate_events": (DateRule("event_date", 1830), DateRule("knowledge_date", 1830)),
    "earnings_announcements": (DateRule("event_date", 400),),
    "earnings_calendar": (DateRule("expected_date", 1100),),
    "macro_releases": (DateRule("release_date", 400),),
    "macro_release_events": (DateRule("release_date", 400),),
    "cb_meetings": (DateRule("meeting_date", 1100),),
    # FIRDS / register admission dates: 1899-12-31 is a spreadsheet-epoch artefact, 9999-12-31 the "unknown" sentinel
    "regulatory_register": (DateRule("first_admission", 1, old_before=DATE_IMPOSSIBLE_BEFORE,
                                     where="first_admission < DATE '9999-01-01'"),),
}

#: knowledge dates that cannot precede the date they describe (A5)
KNOWLEDGE_ORDER: dict[str, tuple[KnowledgeOrder, ...]] = {
    "insider_transactions": (KnowledgeOrder("transaction_date", tolerance_days=1),),
    "short_interest": (KnowledgeOrder("settlement_date"),),
    "fails_to_deliver": (KnowledgeOrder("settlement_date"),),
    "eu_short_positions": (KnowledgeOrder("position_date"),
                           KnowledgeOrder("position_date", "publication_date", suffix=":publication_date")),
    "cftc_positions": (KnowledgeOrder("report_date"),),
    "activist_filings": (KnowledgeOrder("filed_date"),),
    "yield_curves": (KnowledgeOrder("date"),),
    "futures_curve": (KnowledgeOrder("date"),),
    "gdelt_events": (KnowledgeOrder("event_date", certain=False),),
    # period_end pairs: pit_violation is the blocking gate, these add the warning tier with examples
    "filings_index": (KnowledgeOrder("period_end", certain=False, where=_PERIODIC_FORMS),),
    "institutional_holdings": (KnowledgeOrder("period_end", certain=False),),
    "inst_ownership": (KnowledgeOrder("period_end", certain=False),),
    "earnings_events": (KnowledgeOrder("period_end", certain=False),),
    "earnings_reactions": (KnowledgeOrder("period_end", certain=False),),
}


def date_rules_for(c: TableContract) -> tuple[list[DateRule], list[KnowledgeOrder]]:
    """Table rules (DATE_RULES) plus the generic ones: a knowledge date / timestamp or write timestamp later than the
    run date (+1 day) is certainly wrong for every table that does not define its own rule for that column."""
    rules = list(DATE_RULES.get(c.table, ()))
    have = {r.column for r in rules}
    for col in ("knowledge_date", "knowledge_ts", _write_ts_column(c)):
        if col and col in c.column_names and col not in have:
            rules.append(_observed(col))
            have.add(col)
    return rules, list(KNOWLEDGE_ORDER.get(c.table, ()))


# ------------------------------------------------------------------------------------------------ hooks

HookFn = Callable[[DqContext], list[DqOutcome]]
_HOOKS: dict[str, HookFn] = {}


def register_hook(name: str, fn: HookFn) -> None:
    """Register (or replace) a custom dq hook, e.g. a better accounting identity check from ref-identity."""
    _HOOKS[name] = fn


class HookCheck(Check):
    severity = "warn"

    def __init__(self, hook: str, table: str = "", **kw: Any):
        super().__init__(table or hook, check_id=hook, **kw)
        self.hook = hook

    def evaluate(self, ctx: DqContext) -> list[DqOutcome]:
        fn = _HOOKS.get(self.hook)
        return fn(ctx) if fn else []


def accounting_identity_hook(ctx: DqContext, tolerance: float = 0.02, max_violation_rate: float = 0.05) -> list[DqOutcome]:
    """Balance-sheet identity on fundamentals_wide: |assets - (liabilities + equity)| <= 2% of assets."""
    t = "fundamentals_wide"
    if not ctx.has_data(t):
        return []
    cols = ctx.columns(t)
    if not {"total_assets", "total_liabilities", "total_equity"} <= set(cols):
        return [DqOutcome(t, "accounting_identity", "info", True, details={"reason": "identity columns not present"})]
    row = ctx.con.execute(
        f"""SELECT COUNT(*) FILTER (WHERE abs(total_assets - (total_liabilities + total_equity)) > {tolerance} * abs(total_assets)),
                   COUNT(*)
            FROM {t} WHERE total_assets IS NOT NULL AND total_liabilities IS NOT NULL AND total_equity IS NOT NULL
                       AND total_assets <> 0""").fetchone()
    bad, n = row or (0, 0)
    rate = bad / n if n else 0.0
    return [DqOutcome(t, "accounting_identity", "warn", rate <= max_violation_rate, rate, max_violation_rate,
                      {"violations": int(bad), "rows": int(n), "tolerance": tolerance})]


register_hook("accounting_identity", accounting_identity_hook)


def macro_freshness_hook(ctx: DqContext) -> list[DqOutcome]:
    """Macro freshness vs expected release cadence (core / rank-1 series; app.ingestion.macro_audit)."""
    from app.ingestion.macro_audit import freshness_outcomes  # noqa: PLC0415 - lazy: keeps core free of ingestion

    return freshness_outcomes(ctx)


register_hook("macro_freshness", macro_freshness_hook)


def price_coverage_hook(ctx: DqContext) -> list[DqOutcome]:
    """Per-market coverage / per-region freshness / delisted-window rules (app.normalize.prices_dq)."""
    from app.normalize.prices_dq import coverage_outcomes  # noqa: PLC0415 - lazy

    return coverage_outcomes(ctx)


register_hook("price_coverage", price_coverage_hook)


#: a ref_universe check older than this is not reported (the nightly rebuild refreshes it)
SECTOR_LOSS_MAX_AGE_DAYS = 8.0


def issuer_sector_loss_hook(ctx: DqContext) -> list[DqOutcome]:
    """2026-10-31: share of classified issuers that lost their sector in the latest ref_universe run (recorded by
    app.normalize.identity.build_reference; error above 1 %)."""
    from app.normalize.identity import SECTOR_LOSS_MAX, read_sector_loss  # noqa: PLC0415 - lazy

    rec = read_sector_loss(ctx.settings)
    if not rec:
        return []
    try:
        at = datetime.fromisoformat(str(rec.get("checked_at")))
    except ValueError:
        at = None
    if at is not None and (ctx.now - at).total_seconds() > SECTOR_LOSS_MAX_AGE_DAYS * 86400:
        return []
    details = {k: rec.get(k) for k in ("lost", "prev_classified", "examples", "by_country", "run_id", "checked_at",
                                       "kept_previous", "carried_inputs")}
    return [DqOutcome("issuers", "sector_loss", "error", bool(rec.get("passed", True)),
                      float(rec.get("rate") or 0.0), float(rec.get("threshold") or SECTOR_LOSS_MAX), details)]


register_hook("issuer_sector_loss", issuer_sector_loss_hook)


def price_identity_hook(ctx: DqContext) -> list[DqOutcome]:
    """coverage v3 A6: identical close series across securities (ticker collision) + bars on listings without a venue
    symbol (app.normalize.price_collisions)."""
    from app.normalize.price_collisions import identity_collision_outcomes  # noqa: PLC0415 - lazy

    return identity_collision_outcomes(ctx)


register_hook("price_identity", price_identity_hook)


def mcap_plausibility_hook(ctx: DqContext) -> list[DqOutcome]:
    """coverage v3 A6: market caps vs exchange totals and vs price x shares bounds (app.analytics.mcap_plausibility)."""
    from app.analytics.mcap_plausibility import plausibility_outcomes  # noqa: PLC0415 - lazy

    return plausibility_outcomes(ctx)


register_hook("mcap_plausibility", mcap_plausibility_hook)


# ------------------------------------------------------------------------------------------------ default rule set


def _writer_cadence(settings: Settings) -> dict[str, str]:
    """table -> fastest DAG cadence among the jobs writing it (from contracts/jobs.yaml)."""
    import yaml

    doc = yaml.safe_load((settings.contracts_dir / "jobs.yaml").read_text(encoding="utf-8"))
    rank = {"nightly": 0, "weekly": 1, "monthly": 2, "manual": 3}
    out: dict[str, str] = {}
    for j in doc.get("jobs", []):
        for t in j.get("writes", []) or []:
            if t not in out or rank.get(j["dag"], 9) < rank.get(out[t], 9):
                out[t] = j["dag"]
    return out


def _write_ts_column(c: TableContract) -> str | None:
    for n in WRITE_TS_COLUMNS:
        if n in c.column_names:
            return n
    return None


def default_checks(settings: Settings | None = None, tables: Iterable[str] | None = None) -> list[Check]:
    """Generic per-table checks for every Parquet contract table + table-specific rules."""
    s = settings or get_settings()
    contracts = load_contracts()
    cadence = _writer_cadence(s)
    scheduled = _scheduled_tables(s)
    names = [n for n in (tables or contracts) if n in contracts and contracts[n].is_parquet and n not in SKIP_TABLES]
    checks: list[Check] = []
    for n in names:
        c = contracts[n]
        checks += [SchemaConformance(n), PkUnique(n), RowCount(n)]
        ts = _write_ts_column(c)
        dag = cadence.get(n)
        if n in scheduled and scheduled[n][1] in c.column_names:
            kind, col, sla = scheduled[n]
            checks.append(ScheduledFreshness(n, kind, col, sla))
        elif ts and dag in CADENCE_MAX_AGE_DAYS:
            checks.append(Freshness(n, ts, CADENCE_MAX_AGE_DAYS[dag]))
        if "issuer_id" in c.column_names and c.column("issuer_id").nullable:
            checks.append(NullRate(n, "issuer_id", check_id="unresolved_identity_share"))
        if "knowledge_date" in c.column_names and "period_end" in c.column_names:
            # 8-K / proxy "period" dates and some XBRL instants legitimately lie after the filing date: only periodic
            # reports count for filings_index, and a 0.1% tolerance absorbs odd future-dated facts elsewhere.
            pred = "knowledge_date < period_end"
            if "form_class" in c.column_names:
                pred += " AND form_class IN ('annual', 'semiannual', 'quarterly')"
            checks.append(ValueRange(n, "pit_violation", pred, PIT_MAX_VIOLATION_RATE, severity="error"))
        drules, dorder = date_rules_for(c)
        if drules or dorder:
            checks.append(DateSanity(n, drules, dorder))
    sel = set(names)
    if "prices_daily" in sel:
        checks += [
            ValueRange("prices_daily", "value_range:daily_return_gt_80pct", "abs(ret) > 0.8", 0.0005, from_sql=(
                "SELECT adj_close / NULLIF(LAG(adj_close) OVER (PARTITION BY security_id ORDER BY date), 0) - 1 AS ret "
                "FROM prices_daily WHERE date >= current_date - INTERVAL 45 DAY")),
            ValueRange("prices_daily", "value_range:ohlc_inconsistent",
                       "high < low OR close > high * 1.0001 OR close < low * 0.9999", 0.001),
            ValueRange("prices_daily", "value_range:nonpositive_close", "close <= 0", 0.0),
            NullRate("prices_daily", "close_usd", max_rate=0.02),
            HookCheck("price_coverage", "prices_daily"),
            HookCheck("price_identity", "prices_daily"),
        ]
        if "fx_rates" in sel:
            # error: a currency we normally have official FX for (any fixing in the last year) went stale;
            # warning: currencies with no official keyless FX source at all (e.g. SAR/AED/KZT/NGN from Yahoo
            # index/frontier instruments, RUB since 2022) -> close_usd stays null for them, not a pipeline failure.
            _uncovered = (
                "FROM prices_daily p "
                "WHERE p.date >= current_date - INTERVAL 10 DAY AND p.currency <> 'USD' AND NOT EXISTS ("
                "SELECT 1 FROM fx_rates f WHERE f.currency = upper(p.currency) "
                # TWD's only keyless official source (Fed H.10 via FRED, DEXTAUS) is published weekly -> up to ~7d lag
                "AND f.date BETWEEN p.date - (CASE WHEN upper(p.currency) IN ('TWD') THEN 10 ELSE 5 END) * INTERVAL 1 DAY "
                "AND p.date)")
            _supported = ("upper(p.currency) IN (SELECT DISTINCT currency FROM fx_rates "
                          "WHERE date >= current_date - INTERVAL 365 DAY)")
            checks.append(SqlMetric(
                "fx_rates", "fx_coverage",
                f"SELECT COUNT(DISTINCT p.currency) {_uncovered} AND {_supported}",
                0, "<=", requires=("prices_daily", "fx_rates"), severity="error"))
            checks.append(SqlMetric(
                "fx_rates", "fx_unsupported_currencies",
                f"SELECT COUNT(DISTINCT p.currency) {_uncovered} AND NOT {_supported}",
                0, "<=", requires=("prices_daily", "fx_rates"), severity="warn"))
    for t, col in (("composite_scores", "score"), ("factor_scores", "score"), ("insider_signals", "signal_score"),
                   ("sentiment_daily", "sentiment_score")):
        if t in sel:
            checks.append(ValueRange(t, f"value_range:{col}_0_100", f"{col} < 0 OR {col} > 100", 0.0))
    if "ml_predictions" in sel:
        checks.append(ValueRange("ml_predictions", "value_range:decile", "decile < 1 OR decile > 10", 0.0, severity="error"))
    if "fx_rates" in sel:
        checks.append(ValueRange("fx_rates", "value_range:usd_per_unit", "usd_per_unit <= 0", 0.0, severity="error"))
    if "fundamentals_wide" in sel:
        checks.append(HookCheck("accounting_identity", "fundamentals_wide"))
    if "macro_series" in sel:
        checks.append(HookCheck("macro_freshness", "macro_series"))
    if "issuers" in sel:
        checks.append(HookCheck("issuer_sector_loss", "issuers", severity="error"))
    if "valuation_daily" in sel:
        checks.append(HookCheck("mcap_plausibility", "valuation_daily"))
    return checks


# ------------------------------------------------------------------------------------------------ run / persist


def run_checks(checks: Iterable[Check], run_id: str, settings: Settings | None = None, write: bool = True,
               ctx: DqContext | None = None) -> list[DqOutcome]:
    s = settings or get_settings()
    checks = list(checks)
    ctx = ctx or DqContext(run_id, s, tables={c.table for c in checks if c.table in load_contracts()} or None)
    outcomes: list[DqOutcome] = []
    seen_empty: set[str] = set()
    for chk in checks:
        if chk.table in load_contracts() and not ctx.has_data(chk.table):
            if chk.table not in seen_empty:
                seen_empty.add(chk.table)
                outcomes.append(DqOutcome(chk.table, "has_data", "info", False, 0.0, None, {"reason": "no data yet"}))
            continue
        outcomes.extend(chk.run(ctx))
    # de-duplicate on the dq_results PK (last wins)
    uniq: dict[tuple[str, str, str], DqOutcome] = {}
    for o in outcomes:
        uniq[(o.table_name, o.check_id, o.partition_key)] = o
    outcomes = list(uniq.values())
    if write and outcomes:
        write_results(outcomes, run_id, s, ctx.now)
    return outcomes


def write_results(outcomes: list[DqOutcome], run_id: str, settings: Settings | None = None,
                  checked_at: datetime | None = None) -> int:
    ts = checked_at or datetime.now(UTC)
    df = pl.DataFrame(
        {
            "run_id": [run_id] * len(outcomes),
            "year_month": [ts.strftime("%Y-%m")] * len(outcomes),
            "table_name": [o.table_name for o in outcomes],
            "partition_key": [o.partition_key for o in outcomes],
            "check_id": [o.check_id for o in outcomes],
            "severity": [o.severity for o in outcomes],
            "passed": [o.passed for o in outcomes],
            "metric_value": [o.metric_value for o in outcomes],
            "threshold": [o.threshold for o in outcomes],
            "details": [json.dumps(o.details, default=str) if o.details else None for o in outcomes],
            "checked_at": [ts] * len(outcomes),
        },
        schema=get_contract("dq_results").polars_schema(),
    )
    return write_table(df, "dq_results", settings=settings)


def blocking_failures(outcomes: Iterable[DqOutcome]) -> list[DqOutcome]:
    return [o for o in outcomes if o.severity == "error" and not o.passed]


def dq_summary(run_id: str | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """Summary of a dq run (default: latest) for the System page / API."""
    s = settings or get_settings()
    try:
        df = read_table("dq_results", settings=s)
    except Exception:
        df = pl.DataFrame()
    if df.is_empty():
        return {"run_id": None, "checked_at": None, "counts": {}, "blocking": False, "failed": [], "tables": {}}
    if run_id is None:
        run_id = df.sort("checked_at", descending=True)["run_id"][0]
    df = df.filter(pl.col("run_id") == run_id)
    counts: dict[str, dict[str, int]] = {}
    for sev in SEVERITIES:
        sub = df.filter(pl.col("severity") == sev)
        counts[sev] = {"passed": int(sub["passed"].sum()), "failed": int((~sub["passed"]).sum())}
    failed = df.filter(~pl.col("passed") & (pl.col("check_id") != "has_data")).sort(["severity", "table_name"])
    order = {"info": 0, "warn": 1, "error": 2}
    tables: dict[str, str] = {}
    for r in df.iter_rows(named=True):
        st = "no_data" if r["check_id"] == "has_data" else ("ok" if r["passed"] else r["severity"])
        cur = tables.get(r["table_name"])
        if cur is None or (st not in ("ok", "no_data") and order.get(st, -1) > order.get(cur, -1)):
            tables[r["table_name"]] = st
    return {
        "run_id": run_id,
        "checked_at": df["checked_at"].max().isoformat() if df.height else None,
        "counts": counts,
        "blocking": counts["error"]["failed"] > 0,
        "failed": [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k != "year_month"}
                   for r in failed.iter_rows(named=True)],
        "tables": dict(sorted(tables.items())),
    }


# ------------------------------------------------------------------------------------------------ schedule (A5)

#: the startup / interval sync runs dq_checks after a publish at most this often (the nightly cadence the job has in
#: jobs.yaml). Root cause of the 10-31 -> 11-06 gap: under the weekly profile the nightly DAG - the only caller - runs
#: on Saturdays, while the 30-min syncs (which exclude dq_checks from their plan) published every day.
POST_PUBLISH_MIN_INTERVAL_H = 20.0
#: ... and not within this many hours of an attempt that ended without results (timeout, OOM, process death)
POST_PUBLISH_RETRY_H = 6.0
#: ... or of a run that never started (status skipped: deferred for the heavy slot / RAM floor / start budget, or
#: already running elsewhere): the next publishing sync after this many hours tries again
POST_PUBLISH_DEFER_RETRY_H = 1.0
DQ_JOB = "dq_checks"


def _completed(df: pl.DataFrame) -> pl.Expr:
    """dq_checks runs that evaluated the rules (a run that found errors fails with JobReportedFailure)."""
    ok = pl.col("status").is_in(["success", "partial"])
    if "error_type" in df.columns:
        ok = ok | ((pl.col("status") == "failed") & (pl.col("error_type") == "JobReportedFailure"))
    return ok


def post_publish_due(job_runs: pl.DataFrame | None, now: datetime) -> tuple[bool, str]:
    """(due, reason) for a dq_checks run after a sync publish, from the job_runs history (any trigger)."""
    if job_runs is None or job_runs.is_empty() or "job_id" not in job_runs.columns:
        return True, "no dq_checks run recorded"
    runs = job_runs.filter(pl.col("job_id") == DQ_JOB)
    if runs.is_empty():
        return True, "no dq_checks run recorded"

    def _ts(v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        return v if v.tzinfo else v.replace(tzinfo=UTC)

    end = pl.coalesce(pl.col("finished_at"), pl.col("started_at")) if "finished_at" in runs.columns else pl.col("started_at")
    done = runs.filter(_completed(runs))
    last_done = _ts(done.select(end.max()).item()) if done.height else None
    if last_done is not None and now - last_done < timedelta(hours=POST_PUBLISH_MIN_INTERVAL_H):
        return False, (f"dq_checks ran {last_done:%Y-%m-%d %H:%M} UTC ({(now - last_done).total_seconds() / 3600:.1f} h ago); "
                       f"at most every {POST_PUBLISH_MIN_INTERVAL_H:g} h after a publish")
    last = runs.sort("started_at").tail(1)
    row = last.row(0, named=True)
    started = _ts(row.get("started_at"))
    wait_h = POST_PUBLISH_DEFER_RETRY_H if row.get("status") == "skipped" else POST_PUBLISH_RETRY_H
    if last.filter(_completed(last)).is_empty() and started is not None \
            and now - started < timedelta(hours=wait_h):
        return False, (f"last dq_checks attempt {started:%Y-%m-%d %H:%M} UTC: {row.get('status')} "
                       f"({row.get('error_type') or 'no results'}); next try {wait_h:g} h after it")
    since = f"last complete run {last_done:%Y-%m-%d %H:%M} UTC" if last_done else "no complete run recorded"
    return True, f"{since}; due after a publish (every {POST_PUBLISH_MIN_INTERVAL_H:g} h)"


# ------------------------------------------------------------------------------------------------ job entry point


def run(ctx: Any) -> Any:
    """jobs.yaml `dq_checks`: run the default rule set and record dq_results under the job's run_id."""
    from app.scheduler.base import JobResult

    tables = ctx.params.get("tables")
    outcomes = run_checks(default_checks(ctx.settings, tables), ctx.run_id, ctx.settings)
    errors = blocking_failures(outcomes)
    warns = [o for o in outcomes if o.severity == "warn" and not o.passed]
    msg = f"{len(outcomes)} checks, {len(errors)} errors, {len(warns)} warnings"
    if errors:
        msg += " | errors: " + "; ".join(f"{o.table_name}/{o.check_id}={o.metric_value}" for o in errors[:10])
        ctx.log.error(msg)
        return JobResult(status="failed", rows_written={"dq_results": len(outcomes)}, message=msg, retryable=False)
    ctx.log.info(msg)
    return JobResult(status="success", rows_written={"dq_results": len(outcomes)}, message=msg)


__all__ = [
    "Check", "DateRule", "DateSanity", "DqContext", "DqOutcome", "Freshness", "HookCheck", "KnowledgeOrder", "NullRate",
    "PkUnique", "RowCount", "ScheduledFreshness", "SchemaConformance", "SqlMetric", "ValueRange",
    "accounting_identity_hook", "blocking_failures", "date_rules_for", "default_checks", "dq_summary",
    "post_publish_due", "register_hook", "run", "run_checks", "write_results",
]
