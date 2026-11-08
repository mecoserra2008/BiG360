"""Full-universe extension: the serving builder registers lake views for contracts that a long-lived process (the
worker caches its contract set at start) does not know yet, instead of failing the build (2026-10-31 nightly)."""

from __future__ import annotations

import duckdb

from app.api import serving as SV


def test_ensure_lake_views_registers_missing_contract_views():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA lake")
    con.execute("CREATE SCHEMA rawlake")
    SV.ensure_lake_views(con, SV.EXT_LAKE_TABLES)
    views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE schema_name = 'lake'").fetchall()}
    assert set(SV.EXT_LAKE_TABLES) <= views
    cols = {r[0] for r in con.execute("DESCRIBE lake.composite_scores_ext").fetchall()}
    assert {"scope", "reason", "missing_inputs", "rank_global"} <= cols
    SV.ensure_lake_views(con, SV.EXT_LAKE_TABLES)   # idempotent
