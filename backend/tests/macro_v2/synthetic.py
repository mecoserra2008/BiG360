"""Synthetic macro v2 inputs for the API fixture lake (tests/fixtures/serving_small.py calls add_macro_v2).

Adds growth/inflation/labor series with long monthly/quarterly history (incl. ALFRED-like vintages for PAYEMS) to the
fixture's macro lake, then runs the real macro_cells and macro_cycle jobs so every /macro/v2 endpoint has data."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table

EXTRA = [  # series_id, category, freq, units, country, level, monthly drift, vintages
    ("fred:INDPRO", "growth", "M", "Index 2017=100", "US", 100.0, 0.002, False),
    ("fred:RRSFS", "growth", "M", "Millions of Dollars", "US", 2.0e5, 0.002, False),
    ("fred:GDPC1", "growth", "Q", "Billions of Chained 2017 Dollars", "US", 2.0e4, 0.005, False),
    ("fred:CPILFESL", "inflation", "M", "Index 1982-1984=100", "US", 300.0, 0.0025, False),
    ("fred:PPIFIS", "inflation", "M", "Index Nov 2009=100", "US", 140.0, 0.002, False),
    ("fred:PAYEMS", "labor", "M", "Thousands of Persons", "US", 1.5e5, 0.001, True),
    ("fred:ICSA", "labor", "W", "Number", "US", 2.2e5, 0.0, False),
    ("oecd:CLI.USA.LI", "sentiment", "M", "index, long-term average = 100", "US", 100.0, 0.0, False),
    ("fred:WALCL", "liquidity", "W", "Millions of U.S. Dollars", "US", 7.0e6, 0.0, False),
    ("fred:WTREGEN", "liquidity", "W", "Millions of U.S. Dollars", "US", 7.0e5, 0.0, False),
    ("fred:RRPONTSYD", "liquidity", "D", "Billions of U.S. Dollars", "US", 300.0, 0.0, False),
]


def _months(end: date, n: int, step: int = 1) -> list[date]:
    out, y, m = [], end.year, end.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= step
        while m <= 0:
            m += 12
            y -= 1
    return sorted(out)


def add_macro_v2(s: Settings, as_of: date, seed: int = 7) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    now = datetime.now(UTC)
    ms, rows = [], []
    for sid, cat, fq, units, cc, lvl, drift, vint in EXTRA:
        src = sid.split(":", 1)[0]
        ms.append({"series_id": sid, "source_id": src, "native_code": sid.split(":", 1)[1], "title": sid, "country": cc,
                   "category": cat, "frequency": fq, "units": units, "default_transform": "level", "release_lag_days": 20,
                   "is_core": False, "last_observation_date": None, "updated_at": now})
        if fq == "M":
            dates = _months(date(as_of.year, as_of.month, 1) - timedelta(days=40), 15 * 12)
        elif fq == "Q":
            q0 = date(as_of.year, ((as_of.month - 1) // 3) * 3 + 1, 1)
            dates = _months(q0 - timedelta(days=100), 60, 3)
        elif fq == "W":
            dates = [as_of - timedelta(days=7 * k + 3) for k in range(520)][::-1]
        else:
            dates = [as_of - timedelta(days=k + 1) for k in range(1500)][::-1]
        cyc = np.sin(np.arange(len(dates)) / 9.0) * 0.004
        path = lvl * np.exp(np.cumsum(drift + cyc + rng.normal(0, 0.002, len(dates))))
        for d, v in zip(dates, path, strict=True):
            k = d + timedelta(days=35 if fq in ("M", "Q") else 2)
            if k > as_of:
                continue
            rows.append((sid, src, d, float(v), k, not vint))
            if vint:  # one revision a month later (ALFRED-like observed vintages)
                k2 = k + timedelta(days=30)
                if k2 <= as_of:
                    rows.append((sid, src, d, float(v) * (1 + rng.normal(0, 0.001)), k2, False))
    n_ms = write_table(pl.DataFrame(ms), "macro_series", mode="upsert", settings=s)
    obs = pl.DataFrame(rows, schema=["series_id", "source_id", "date", "value", "knowledge_date",
                                     "knowledge_date_estimated"], orient="row").with_columns(pl.lit(now).alias("ingested_at"))
    n_obs = write_table(obs, "macro_observations", mode="upsert", settings=s)
    from app.macro.cycle import run_cycle
    from app.macro.indicators import run_cells

    rc = run_cells(s, as_of)
    cy = run_cycle(s, as_of)
    return {"macro_v2_series": n_ms, "macro_v2_obs": n_obs, "macro_cells": rc.get("rows_macro_cells", 0),
            "macro_cycle": cy.get("rows_written", 0)}
