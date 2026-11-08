"""Global macro (non-FRED) umbrella. Job `macro_global` (kept for compatibility; DAG dependency of the regime model).

Since the macro-coverage split the work lives in three catalog-driven jobs (contracts/macro_catalog.yaml):
  macro_eu        app.ingestion.macro_eu        ECB, Eurostat, Bundesbank, SNB, national EU offices
  macro_intl      app.ingestion.macro_intl      OECD, BIS, IMF, World Bank (+ Pink Sheet commodities)
  macro_national  app.ingestion.macro_national  US Treasury, BoE/ONS, BoJ/MoF, ECOS, AKShare China, HKMA, RBA/ABS,
                                                StatCan/BoC, BCB/IBGE, Banxico/INEGI, BCCh, BCRP, BCRA/datos.gob.ar, ...
In the nightly DAG macro_global depends on those three: a group whose run report (data/logs/reports/<job>_<date>.json)
is younger than 20 h is not re-run, so the umbrella only summarises. Run alone (`jobs run macro_global`) it runs the
groups itself, as before. params: sources (restrict), run_all=true (ignore fresh reports), start.

The pure parsers historically imported from here are re-exported from app.ingestion.macro_common.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.ingestion.macro_common import (  # noqa: F401 - re-exports (compat)
    GSeries,
    obs_frame,
    parse_bcb,
    parse_boe_csv,
    parse_boj,
    parse_jsonstat,
    parse_period,
    parse_sdmx_csv,
    run_group,
)

GROUPS: dict[str, str] = {"macro_eu": "app.ingestion.macro_eu", "macro_intl": "app.ingestion.macro_intl",
                          "macro_national": "app.ingestion.macro_national"}


def group_sources() -> dict[str, list[str]]:
    import importlib  # noqa: PLC0415

    return {job: list(importlib.import_module(mod).SOURCES) for job, mod in GROUPS.items()}


def fresh_report(job: str, s: Settings, max_age_h: float = 20.0) -> bool:
    import time  # noqa: PLC0415

    d = s.logs_dir / "reports"
    files = sorted(d.glob(f"{job}_*.json")) if d.exists() else []
    return bool(files) and (time.time() - files[-1].stat().st_mtime) < max_age_h * 3600


def run_macro_global(settings: Settings | None = None, sources: list[str] | None = None, write: bool = True,
                     start: date | None = None, skip_fresh: bool = False) -> dict[str, Any]:
    s = settings or get_settings()
    out: dict[str, Any] = {"ok": {}, "failed": {}, "skipped": {}, "requests": 0, "rows_series": 0,
                           "rows_observations": 0, "groups": {}}
    for job, srcs in group_sources().items():
        if skip_fresh and not sources and fresh_report(job, s):
            out["groups"][job] = "fresh upstream run (skipped)"
            continue
        if sources:
            srcs = [x for x in srcs if x in sources]
            if not srcs:
                continue
        rep = run_group(job, srcs, s, start, write=write)
        out["groups"][job] = {k: rep.get(k) for k in ("requests", "rows_series", "rows_observations", "seconds")}
        for k in ("ok", "failed", "skipped"):
            out[k].update(rep.get(k, {}))
        for k in ("requests", "rows_series", "rows_observations"):
            out[k] += rep.get(k, 0) or 0
    return out


def run(ctx):  # noqa: ANN001 - JobContext
    from app.ingestion.macro_common import job_result  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    start = date.fromisoformat(p["start"]) if p.get("start") else None
    rep = run_macro_global(ctx.settings, p.get("sources"), start=start, skip_fresh=not p.get("run_all"))
    if not rep["ok"] and not rep["failed"]:
        return JobResult(status="success", rows_written={}, watermark=datetime.now(UTC).date().isoformat(),
                         message=f"umbrella: {rep['groups']}")
    return job_result(rep)
