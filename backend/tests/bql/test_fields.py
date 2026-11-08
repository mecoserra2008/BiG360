"""BQL field dictionary: the published contracts/bql_fields.yaml is up to date, every mnemonic / alias resolves to an
existing serving column or history source, and every field compiles and runs (cross-section and history)."""

from __future__ import annotations

import re

import pytest

from app.bql import engine
from app.bql import fields as F
from app.bql.grammar import parse_expr, walk, Name


@pytest.fixture(scope="module")
def schema():
    return F.serving_schema()


def test_published_yaml_is_up_to_date():
    assert F.check_published() == [], "run: python -m app.bql.fields --write"


def test_counts_and_curated_core_names():
    d = F.dictionary()
    assert len(d) >= 700
    for mn in ("px_last", "px", "chg_pct_1m", "pe_fwd", "pe_ttm", "ev_ebit", "roic", "mkt_cap_usd", "div_yld",
               "tech.hi52_pct", "tech.ivol_pct", "cf.touch_20d", "sm.score", "si.pct_float", "eps_surprise",
               "sector", "region", "country", "f.quality", "c.balanced", "fa.revenue", "kpi.nim", "vol_1y"):
        assert F.resolve(mn) is not None, mn
    assert F.resolve("cur_mkt_cap").mnemonic == "mkt_cap_usd"
    assert F.resolve("PX_LAST").mnemonic == "px_last"
    assert F.resolve("short_pct_float").mnemonic == "si.pct_float"


def test_every_alias_resolves_and_is_unique():
    d = F.dictionary()
    seen: dict[str, str] = {}
    for f in d.values():
        assert f.unit in F.UNITS, (f.mnemonic, f.unit)
        assert f.dtype in ("number", "text", "bool", "date"), f.mnemonic
        assert f.description and f.pit, f.mnemonic
        for a in (f.mnemonic,) + f.aliases:
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", a), a
            assert a.lower() not in seen, f"{a} used by {seen.get(a.lower())} and {f.mnemonic}"
            seen[a.lower()] = f.mnemonic
            assert F.resolve(a) is f, a


def test_sources_exist_in_the_serving_schema(schema):
    from app.core.schema import load_contracts

    contracts = load_contracts()
    for f in F.dictionary().values():
        if f.table is not None:
            table = F._virtual_base(f.table)
            assert table in schema, (f.mnemonic, f.table)
            cols = schema[table]
            if f.column and "{t}" in f.column:
                for c in re.findall(r"\{t\}\.([a-z_0-9]+)", f.column):
                    assert c in cols, (f.mnemonic, c)
            else:
                assert f.column in cols, (f.mnemonic, f.column)
        h = f.hist
        if h is None:
            assert f.table is not None, f.mnemonic
            continue
        if h.kind == "px":
            for c in re.findall(r"[a-z_]+", h.value):
                if c not in ("nullif",):
                    assert c in schema["prices"], (f.mnemonic, c)
        elif h.kind == "expr":
            for n in walk(parse_expr(h.expr)):
                if isinstance(n, Name):
                    assert F.resolve(n.name) is not None, (f.mnemonic, n.name)
        else:
            if h.table.startswith("lake:"):
                c = contracts[h.table[5:]]
                cols = {x.name for x in c.columns}
            else:
                assert h.table in schema, (f.mnemonic, h.table)
                cols = set(schema[h.table])
            assert h.key in cols and h.date in cols, (f.mnemonic, h.table)
            if re.fullmatch(r"[a-z_0-9]+", h.value):
                assert h.value in cols, (f.mnemonic, h.value)
            if h.kind == "period":
                assert h.period in cols, f.mnemonic


def _batches(items, n=40):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def test_every_field_compiles_and_runs(bql_env):
    d = list(F.dictionary().values())
    for batch in _batches([f.mnemonic for f in d]):
        r = engine.run(f"get({', '.join(batch)}) for(['AAA US', 'IDX:^GSPC'])", bql_env["db"], use_cache=False)
        # ticker / name / id duplicate the key columns and are not repeated as value columns (2026-10-27)
        from app.bql.compiler import KEY_DUP_FIELDS

        assert [c["name"] for c in r["columns"]][3:] == [m for m in batch if m not in KEY_DUP_FIELDS]
    hist = [f.mnemonic for f in d if f.hist is not None and f.dtype in ("number", "bool")]
    for batch in _batches(hist, 30):
        r = engine.run(f"get({', '.join(batch)}) for(['AAA US']) with(dates=range(-1M, 0D), freq=W)", bql_env["db"],
                       use_cache=False)
        assert r["mode"] == "timeseries"
    pit = [f.mnemonic for f in d if f.hist is not None or f.static]
    for batch in _batches(pit, 40):
        engine.run(f"get({', '.join(batch)}) for(['AAA US']) with(as_of=2025-12-31)", bql_env["db"], use_cache=False)


def test_search():
    total, items = F.search("hi52")
    assert total >= 3 and items[0].mnemonic in ("hi52", "tech.hi52", "tech.hi52_pct", "px_to_hi52")
    total, items = F.search("market cap")
    assert any(f.mnemonic == "mkt_cap_usd" for f in items)
    total, items = F.search(None, group="identity", limit=5)
    assert total >= 10 and len(items) == 5 and all(f.group == "identity" for f in items)
