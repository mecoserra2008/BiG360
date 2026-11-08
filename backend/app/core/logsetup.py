"""Logging setup with secret redaction, optional JSON lines and per-job log files.

Every handler gets a SecretRedactingFilter that masks (a) the literal values of all configured API keys and
(b) common key-bearing query parameters (api_key=, apikey=, crtfc_key=, token=, Subscription-Key=).

    setup_logging()                                   # stderr only (text)
    setup_logging(log_file="worker.log")              # + data/logs/worker.log (rotating 20 MB x 5, JSON lines by default)
    with job_log_handler("prices_finalize", run_id): # + data/logs/jobs/<job_id>/<run_id>.log for that job's records
        ...

File logs are JSON lines when Settings.log_json is true (env BIG360_LOG_JSON, default on); stderr stays human-readable
unless BIG360_LOG_STDERR_JSON=1 (containers / log shippers).
JSON line keys: ts, level, logger, msg, process, thread, [service], [request_id], [job_id, run_id, dag_run_id], [exc]
plus the keys of a `fields` dict passed as `extra={"fields": {...}}` (e.g. the API access log: method, route, status,
duration_ms). `request_id` comes from the context variable set by the API request-id middleware (app.api.observability)
and is also accepted as an explicit extra. `service` is the process role set by setup_logging(service=...).
"""

from __future__ import annotations

import json
import logging
import contextvars
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import get_settings

#: request id of the API request being served (set by app.api.observability.RequestContextMiddleware)
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("big360_request_id", default=None)
_service: str | None = None

_PARAM_RE = re.compile(r"(?i)\b(api[_-]?key|apikey|crtfc_key|token|subscription-key|access_key|key)=([^&\s\"']+)")


def redact(text: str, secrets: list[str] | None = None) -> str:
    for s in secrets if secrets is not None else get_settings().secret_values():
        if s and s in text:
            text = text.replace(s, "***")
    return _PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)


class SecretRedactingFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets = get_settings().secret_values()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed args
            return True
        clean = redact(msg, self._secrets)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra attributes job_id / run_id are included when present."""

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "process": record.process,
            "thread": record.threadName,
        }
        if _service:
            out["service"] = _service
        rid = getattr(record, "request_id", None) or request_id_var.get()
        if rid:
            out["request_id"] = rid
        for k in ("job_id", "run_id", "dag_run_id"):
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                out.setdefault(k, v)
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False, default=str)


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured = False
_file_handlers: dict[str, logging.Handler] = {}
_lock = threading.Lock()


def _file_formatter(json_format: bool | None) -> logging.Formatter:
    use_json = get_settings().log_json if json_format is None else json_format
    return JsonFormatter() if use_json else logging.Formatter(_FORMAT)


def _resolve_log_path(log_file: str | Path) -> Path:
    path = Path(log_file)
    if not path.is_absolute():
        settings = get_settings()
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        path = settings.logs_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(level: str | None = None, log_file: str | Path | None = None, json_format: bool | None = None,
                  service: str | None = None) -> None:
    """Idempotent root logger configuration. log_file is relative to data/logs/ unless absolute.

    Calling it again with the same log_file does not add a second handler. `service` (api, worker, filler, supervisor
    ...) is written into every JSON line of this process.
    """
    global _configured, _service
    settings = get_settings()
    if service:
        _service = service
    root = logging.getLogger()
    root.setLevel((level or settings.log_level).upper())
    with _lock:
        if not _configured:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(JsonFormatter() if settings.log_stderr_json else logging.Formatter(_FORMAT))
            sh.addFilter(SecretRedactingFilter())
            root.addHandler(sh)
            for noisy in ("httpx", "httpcore", "urllib3", "yfinance", "peewee", "apscheduler.executors.default"):
                logging.getLogger(noisy).setLevel(logging.WARNING)
            _configured = True
        if log_file:
            path = _resolve_log_path(log_file)
            key = str(path.resolve())
            if key in _file_handlers:
                return
            fh = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5, encoding="utf-8", delay=True)
            fh.setFormatter(_file_formatter(json_format))
            fh.addFilter(SecretRedactingFilter())
            root.addHandler(fh)
            _file_handlers[key] = fh


class _JobRecordFilter(logging.Filter):
    """Pass records emitted by the job's logger (app.job.<job_id>) or by threads registered to the run."""

    def __init__(self, job_id: str, run_id: str):
        super().__init__()
        self.job_id, self.run_id = job_id, run_id
        self.prefix = f"app.job.{job_id}"
        self.threads: set[int] = {threading.get_ident()}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == self.prefix or record.name.startswith(self.prefix + "."):
            ok = True
        else:
            ok = record.thread in self.threads
        if ok:
            record.job_id = self.job_id
            record.run_id = self.run_id
        return ok


@contextmanager
def job_log_handler(job_id: str, run_id: str, json_format: bool | None = None) -> Iterator[Path]:
    """Attach data/logs/jobs/<job_id>/<run_id>.log to the root logger for the duration of a job run.

    Records from the job logger `app.job.<job_id>` and from the current thread are captured. Yields the absolute
    log path; callers store it relative to data/logs in job_runs.log_path.
    """
    path = _resolve_log_path(Path("jobs") / job_id / f"{run_id}.log")
    job_logger = logging.getLogger("app.job")
    if job_logger.getEffectiveLevel() > logging.INFO:  # logging not configured (tests, library use)
        job_logger.setLevel(logging.INFO)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(_file_formatter(json_format))
    fh.addFilter(SecretRedactingFilter())
    flt = _JobRecordFilter(job_id, run_id)
    fh.addFilter(flt)
    fh._job_filter = flt  # type: ignore[attr-defined]  # lets the runner register worker threads
    root = logging.getLogger()
    root.addHandler(fh)
    try:
        yield path
    finally:
        root.removeHandler(fh)
        fh.close()


def register_job_thread(job_id: str, run_id: str, ident: int | None = None) -> None:
    """Route records of another thread (e.g. a job's inner execution thread) into the job's log file."""
    ident = ident or threading.get_ident()
    for h in logging.getLogger().handlers:
        flt = getattr(h, "_job_filter", None)
        if isinstance(flt, _JobRecordFilter) and flt.job_id == job_id and flt.run_id == run_id:
            flt.threads.add(ident)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
