"""Export a backtest run as a standalone Python script (owner: backtest; pattern of app.modeling.export).

The script embeds verbatim: the PIT helper (app.normalize.pit.asof_join), the Model Studio dataset engine
(app/modeling/_core.py), the formula engine (app/normalize/formula.py), the backtest engine core
(app/backtest/_engine.py) and the panel builder (app/backtest/_panel.py), plus the run's normalized spec and panel
plan. It rebuilds the point-in-time panel read-only from the Parquet lake, re-simulates the strategy and prints the
headline metrics and a final `RESULT_JSON {...}` line so the reproduction can be checked mechanically.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.schema import get_contract
from app.modeling import _core
from app.modeling.export import _strip_future
from app.normalize import formula, pit

from . import _engine, _panel

HEADER = '''#!/usr/bin/env python
"""BiG Equity 360 - Backtesting lab reproducible script.

Run:      {run_id}   ({name})
Created:  {created}  (snapshot {snapshot}, spec hash {spec_hash}, plan key {plan_key}, seed {seed})

Rebuilds the point-in-time panel read-only from the Parquet lake under --data-dir (default: the data dir of the
run) with the embedded Studio dataset engine + backtest panel builder, re-simulates the strategy with the embedded
engine core and prints the headline metrics. Requires: duckdb polars numpy
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SPEC = json.loads(r"""{spec_json}""")
PLAN = json.loads(r"""{plan_json}""")
LAKE_TABLES = json.loads(r"""{tables_json}""")
DEFAULT_DATA_DIR = r"{data_dir}"
OOS_START = "{oos_start}"
'''

LOADER = '''

# ============================================================================== lake access


def lake_connection(data_dir: str):
    import duckdb

    con = duckdb.connect(":memory:")
    root = Path(data_dir)
    for table, t in LAKE_TABLES.items():
        d = root / t["path"]
        cols = t.get("columns")
        files = list(d.rglob("*.parquet")) if d.exists() else []
        if not files:  # typed empty view (as app.core.storage.view_sql)
            con.execute(f'CREATE OR REPLACE VIEW "{table}" AS SELECT ' +
                        ", ".join(f'CAST(NULL AS {ty}) AS "{c}"' for c, ty in cols) + " WHERE FALSE")
            continue
        glob = (d.as_posix() + ("/**/*.parquet" if t["hive_types"] else "/*.parquet")).replace("'", "''")
        opts = ["union_by_name = true"]
        if t["hive_types"]:
            types = ", ".join(f"'{k}': {v}" for k, v in t["hive_types"].items())
            opts += ["hive_partitioning = true", "hive_types = {" + types + "}"]
        src = f'read_parquet(\\'{glob}\\', {", ".join(opts)})'
        sel = t.get("select")
        body = sel.replace("__SRC__", src) if sel else f"SELECT * FROM {src}"
        con.execute(f'CREATE OR REPLACE VIEW "{table}" AS {body}')
    return con


def main(data_dir: str) -> dict:
    import math
    from datetime import date

    con = lake_connection(data_dir)
    panel, px, events, meta = build_panel(PLAN, con, None, data_dir)
    for w in meta["warnings"]:
        print("panel warning:", w)
    states = meta.get("_states")
    regime_ok = None
    rf = (SPEC.get("construction") or {}).get("regime_filter")
    if rf and states is not None and not states.is_empty():
        regime_ok, _ = regime_overlay(states, sorted(panel["date"].unique().to_list()), rf)
    if SPEC.get("mode") == "event":
        attrs = panel.select("entity_id", "date", "adv_usd", "vol")
        sim = simulate_events(events, px, SPEC, attrs)
    else:
        sim = simulate_rank(panel, px, SPEC, regime_ok=regime_ok)
    m = split_metrics(sim, px, date.fromisoformat(OOS_START))
    full = m["full"]
    print(f"panel: {meta['n_rows']} rows, {meta['n_entities']} entities, {meta['start']} .. {meta['end']}")
    print(f"simulated {len(sim.ret)} days {sim.dates[0]} .. {sim.dates[-1]}")
    for k in ("cagr", "vol", "sharpe", "sortino", "max_drawdown", "beta", "information_ratio"):
        if full.get(k) is not None:
            print(f"  {k:18s} {full[k]: .4f}")
    out = {"total_return": full.get("total_return"), "cagr": full.get("cagr"), "sharpe": full.get("sharpe"),
           "max_drawdown": full.get("max_drawdown"), "n_days": full.get("n_days"),
           "oos_sharpe": (m.get("out_of_sample") or {}).get("sharpe")}
    print("RESULT_JSON " + json.dumps(out))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    a = ap.parse_args()
    main(a.data_dir)
'''


def _src(mod: Any) -> str:
    return _strip_future(Path(mod.__file__).read_text(encoding="utf-8").replace("\r\n", "\n"))


def _tables(plan: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for t in _panel.plan_tables(plan):
        c = get_contract(t)
        hive = {p: c.column(p).duckdb for p in c.partition_by} if c.partition_by else None
        out[t] = {"path": c.path, "hive_types": hive, "columns": [[col.name, col.duckdb] for col in c.columns]}
        if c.narrowed_columns or c.derived_columns:
            from app.core.storage import logical_select_sql, physical_columns  # noqa: PLC0415

            out[t]["select"] = logical_select_sql(c, physical_columns(c), "__SRC__")
    return out


def embedded_sources() -> str:
    sep = "\n\n# " + "=" * 78
    pit_src = ("\n\n# ============================================================================== PIT helper "
               "(app.normalize.pit, verbatim)\nfrom collections.abc import Sequence\nimport polars as pl\n\n\n"
               + inspect.getsource(pit._as_lazy) + "\n\n" + inspect.getsource(pit.asof_join))
    return (pit_src + sep + " dataset engine (app/modeling/_core.py, verbatim)\n" + _src(_core)
            + sep + " formula engine (app/normalize/formula.py, verbatim)\n" + _src(formula)
            + sep + " backtest engine core (app/backtest/_engine.py, verbatim)\n" + _src(_engine)
            + sep + " backtest panel builder (app/backtest/_panel.py, verbatim)\n" + _src(_panel))


def export_script(env: dict[str, Any], data_dir: Path) -> str:
    """Python source for a finished run envelope (result.json)."""
    rep = env.get("reproducibility") or {}
    spec = env["run_spec"]
    plan = env["plan"]
    head = HEADER.format(run_id=env["run_id"], name=spec.get("name"), created=datetime.now(UTC).isoformat(timespec="seconds"),
                         snapshot=rep.get("snapshot_id"), spec_hash=rep.get("spec_hash"), plan_key=rep.get("plan_key"),
                         seed=spec.get("seed"), spec_json=json.dumps(spec, indent=1).replace('"""', '\\"\\"\\"'),
                         plan_json=json.dumps(plan, indent=1, default=str).replace('"""', '\\"\\"\\"'),
                         tables_json=json.dumps(_tables(plan), indent=1), data_dir=str(data_dir),
                         oos_start=((env.get("result") or {}).get("summary") or {}).get("oos_start") or "")
    return head + embedded_sources() + LOADER
