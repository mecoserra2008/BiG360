"""Startup sync A3: section-incremental publish == full build (docs/STARTUP_SYNC_PLAN.md).

A synthetic lake (tests/fixtures/serving_small.py) is built once and published with the regular full build. Then
some lake tables change; the dirty-section rule picks the sections that read them (plus their dependents), the
incremental publish patches a copy of the published snapshot, and a fresh full build of the same lake must have
the same rows in every table (EXCEPT ALL both ways; exact for every non-float column, floats to 10 significant
digits because parallel DuckDB sums are not bit-deterministic even between two full builds; only _meta run_id /
built_at differ)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import duckdb
import polars as pl
import pytest

from app.core import config as config_mod
from app.core import schema as schema_mod


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    d = tmp_path_factory.mktemp("sync_publish") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    old_slot = os.environ.get("BIG360_HEAVY_SLOT")
    os.environ["BIG360_DATA_DIR"] = str(d)
    os.environ["BIG360_HEAVY_SLOT"] = "0"
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from tests.fixtures.serving_small import build_lake

    build_lake(s, n_securities=40, years=3)
    yield s
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    if old_slot is None:
        os.environ.pop("BIG360_HEAVY_SLOT", None)
    else:
        os.environ["BIG360_HEAVY_SLOT"] = old_slot
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def _cols(con: duckdb.DuckDBPyConnection, db: str, t: str, round_floats: bool) -> str:
    out = []
    for name, typ, *_ in con.execute(f'DESCRIBE {db}."{t}"').fetchall():
        q = f'"{name}"'
        if round_floats and typ in ("DOUBLE", "FLOAT", "REAL"):
            # 10 significant digits: parallel DuckDB aggregates (sum / regr_slope / avg) are not bit-deterministic in
            # their summation order, also between two full builds of the same lake
            q = (f"CASE WHEN {q} IS NULL OR {q} = 0 OR isnan({q}) OR isinf({q}) THEN {q} "
                 f"ELSE round({q}, 10 - CAST(ceil(log10(abs({q}))) AS INTEGER)) END AS {q}")
        out.append(q)
    return ", ".join(out)


def _diff(a: str, b: str, round_floats: bool = False) -> list[str]:
    """Tables whose rows differ between two snapshot files (EXCEPT ALL both ways); _meta run_id / built_at ignored."""
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{a}' AS a (READ_ONLY)")
    con.execute(f"ATTACH '{b}' AS b (READ_ONLY)")
    ta = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'a'").fetchall()}
    tb = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'b'").fetchall()}
    bad = sorted(f"missing:{t}" for t in ta ^ tb)
    for t in sorted(ta & tb):
        ca = [r[0] for r in con.execute(f"DESCRIBE a.\"{t}\"").fetchall()]
        cb = [r[0] for r in con.execute(f"DESCRIBE b.\"{t}\"").fetchall()]
        if ca != cb:
            bad.append(f"columns:{t}")
            continue
        where = " WHERE key NOT IN ('run_id', 'built_at')" if t == "_meta" else ""
        sa = f'SELECT {_cols(con, "a", t, round_floats)} FROM a."{t}"{where}'
        sb = f'SELECT {_cols(con, "b", t, round_floats)} FROM b."{t}"{where}'
        n1 = con.execute(f"SELECT count(*) FROM ({sa} EXCEPT ALL {sb})").fetchone()[0]
        n2 = con.execute(f"SELECT count(*) FROM ({sb} EXCEPT ALL {sa})").fetchone()[0]
        if n1 or n2:
            bad.append(f"rows:{t} (+{n1}/-{n2})")
    con.close()
    return bad


def test_section_map_capture(lake):
    from app.ops.sync.publish import section_map

    m = section_map(lake, refresh=True)["sections"]
    assert "prices_daily" in m["prices"]["lake"] and "prices" in m["prices"]["writes"]
    assert "sec" in m["events_v2"]["reads"]           # events_v2 joins the sec table of the sec section
    assert m["prices"]["as_of"] is True                # the price window depends on the as_of date
    assert all(v["sql_hash"] for v in m.values())


def test_incremental_publish_equals_full_build(lake):
    from app.api.serving import build_serving_db
    from app.core.storage import current_serving_db, write_table
    from app.ops.sync import publish as PB

    rep = build_serving_db(lake)
    snap = PB.current_snapshot(lake)
    assert snap["run_id"] == rep.run_id and snap["record"] and snap["record"]["sql_hash"]
    assert PB.dirty_sections(lake, snap["as_of"]) == {}
    assert PB.plan_publish(lake, snap["as_of"])["mode"] == "none"

    time.sleep(1.2)   # file mtimes after the build start
    from app.core.storage import read_table

    obs = read_table("macro_observations", settings=lake)
    extra = obs.head(5).with_columns(pl.col("date") + timedelta(days=1), pl.lit(datetime.now(UTC)).alias("ingested_at"))
    write_table(extra, "macro_observations", settings=lake)
    dirty = PB.dirty_sections(lake, snap["as_of"])
    assert dirty, "a macro_observations change must dirty its sections"
    assert "prices" not in dirty and "fundamentals" not in dirty
    assert all("macro_observations" in why or why.startswith(("reads", "co-writes")) for why in dirty.values())

    inc = PB.incremental_publish(list(dirty), snap["as_of"], settings=lake)
    assert current_serving_db(lake).name == f"serving_{inc['run_id']}.duckdb"
    full = build_serving_db(lake, as_of=snap["as_of"], publish=False)
    # every table equal to the full build: exactly for non-float columns, to 10 significant digits for floats
    assert _diff(inc["path"], str(full.path), round_floats=True) == []
    # the incremental snapshot is recorded: nothing is dirty against it any more
    assert PB.dirty_sections(lake, snap["as_of"]) == {}


def test_as_of_change_dirties_as_of_sections(lake):
    from app.ops.sync import publish as PB

    snap = PB.current_snapshot(lake)
    m = PB.section_map(lake)["sections"]
    dirty = PB.dirty_sections(lake, snap["as_of"] + timedelta(days=1))
    assert {n for n, v in m.items() if v["as_of"]} <= set(dirty)
    plan = PB.plan_publish(lake, snap["as_of"] + timedelta(days=1))
    assert plan["mode"] in ("incremental", "full") and plan["est_s"] > 0


def test_code_change_dirties_its_section(lake):
    from app.ops.sync import publish as PB

    snap = PB.current_snapshot(lake)
    rec = dict(snap["record"])
    rec["sql_hash"] = dict(rec["sql_hash"], cftc="0000")
    snap = dict(snap, record=rec)
    dirty = PB.dirty_sections(lake, snap["as_of"], snap=snap)
    assert dirty.get("cftc") == "builder code of this section changed"
