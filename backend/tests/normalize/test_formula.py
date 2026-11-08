"""app.normalize.formula: AST whitelist, null semantics, panel functions, contract formulas parse."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
import yaml

from app.core.config import get_settings
from app.normalize.formula import FormulaError, compile_expr, dependencies, evaluate, uses_panel_functions, validate_expr


def test_scalar_row_basics():
    assert evaluate("div(gross_profit, revenue)", {"gross_profit": 40, "revenue": 100}) == pytest.approx(0.4)
    assert evaluate("revenue - nz(foo)", {"revenue": 100}) == 100
    assert evaluate("revenue - foo", {"revenue": 100}) is None          # missing input -> null
    assert evaluate("a / b", {"a": 1, "b": 0}) is None                 # division by zero -> null
    assert evaluate("safe_div(a, b)", {"a": 1, "b": -2}) is None       # safe_div: b <= 0 -> null
    assert evaluate("div(a, b)", {"a": 1, "b": -2}) == -0.5
    assert evaluate("coalesce(x, y, 3)", {"x": None, "y": None}) == 3
    assert evaluate("clip(x, 0.0, 0.4, 0.25)", {"x": None}) == 0.25
    assert evaluate("clip(x, 0.0, 0.4, 0.25)", {"x": 0.9}) == 0.4
    assert evaluate("max(a, b)", {"a": 1, "b": None}) is None
    assert evaluate("abs(a) + -b", {"a": -3, "b": 1}) == 2
    assert evaluate("a > 0", {"a": 2}) == 1.0
    assert evaluate("(a > 0) and (b < 0)", {"a": 2, "b": 1}) == 0.0
    assert evaluate("1 if a > 0 else 2", {"a": -1}) == 2


@pytest.mark.parametrize("bad", ["__import__('os')", "a.b", "a[0]", "lambda x: x", "open('f')", "f'x'", "'s'", "a = 1"])
def test_whitelist_rejects(bad):
    with pytest.raises(FormulaError):
        validate_expr(bad)


def test_dependencies_and_panel_detection():
    assert dependencies("clip(safe_div(income_tax, pretax_income), 0.0, 0.40, 0.25)") == {"income_tax", "pretax_income"}
    assert uses_panel_functions("growth_yoy(revenue)")
    assert not uses_panel_functions("div(a, b)")
    with pytest.raises(FormulaError):
        compile_expr("lag(revenue, 1)")


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "issuer_id": ["A"] * 6 + ["B"] * 2,
        "period_kind": ["FY"] * 8,
        "period_end": [date(2019 + i, 12, 31) for i in range(6)] + [date(2023, 12, 31), date(2024, 12, 31)],
        "revenue": [100.0, 110, 121, 133.1, 146.41, 161.051, 50, 40],
        "total_assets": [1000.0, 1100, 1200, 1300, 1400, 1500, 10, 20],
    })


def test_panel_functions_vectorized():
    df = _panel()
    g = evaluate("growth_yoy(revenue)", df).to_list()
    assert g[0] is None and g[1] == pytest.approx(0.1) and g[6] is None and g[7] == pytest.approx(-0.2)
    cg = evaluate("cagr(revenue, 3)", df).to_list()
    assert cg[3] == pytest.approx(0.1) and cg[2] is None
    av = evaluate("avg(total_assets)", df).to_list()
    assert av[0] == 1000.0 and av[1] == 1050.0              # falls back to x when prior missing
    sd = evaluate("stdev_fy(revenue, 3)", df).to_list()
    assert sd[0] is None and sd[2] is not None              # min n-1 = 2 observations
    lag2 = evaluate("lag(revenue, 2)", df).to_list()
    assert lag2[2] == 100.0 and lag2[7] is None             # panels never cross issuers


def test_panel_lag_tolerates_52_53_week_dates():
    df = pl.DataFrame({"issuer_id": ["A", "A"], "period_kind": ["FY", "FY"],
                       "period_end": [date(2024, 9, 28), date(2025, 9, 27)], "x": [1.0, 2.0]})
    assert evaluate("lag(x, 1)", df).to_list() == [None, 1.0]


def test_row_mode_with_history():
    hist = [{"issuer_id": "A", "period_kind": "FY", "period_end": date(2023, 12, 31), "revenue": 100.0}]
    row = {"issuer_id": "A", "period_kind": "FY", "period_end": date(2024, 12, 31), "revenue": 120.0}
    assert evaluate("growth_yoy(revenue)", row, history=hist) == pytest.approx(0.2)


def test_lazyframe_and_order_preserved():
    df = _panel().reverse()
    out = evaluate("revenue * 2", df.lazy())
    assert out.to_list() == [x * 2 for x in df.get_column("revenue").to_list()]


def test_all_contract_formulas_parse():
    root = get_settings().contracts_dir
    canon = yaml.safe_load((root / "canonical_line_items.yaml").read_text(encoding="utf-8"))
    for it in canon["items"]:
        if it.get("derive"):
            validate_expr(it["derive"])
    kpis = yaml.safe_load((root / "sector_kpis.yaml").read_text(encoding="utf-8"))
    n = 0
    for m in kpis["generic_metrics"]:
        validate_expr(m["formula"])
        n += 1
    for ks in kpis["kpi_sets"]:
        for k in ks.get("kpis") or []:
            if isinstance(k, dict) and k.get("formula"):
                validate_expr(k["formula"])
                n += 1
    assert n > 40
