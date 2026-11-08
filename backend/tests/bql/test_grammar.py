"""BQL-lite parser: golden ASTs, canonical text round trip, syntax errors."""

from __future__ import annotations

from datetime import date

import pytest

from app.bql.grammar import (
    BqlSyntaxError,
    Dur,
    Num,
    ast_hash,
    parse,
    parse_expr,
    query_text,
    to_dict,
    to_text,
    tokenize,
)

GOLDEN = [
    ("get(px_last) for(['AAPL US'])",
     {"get": [{"expr": {"name": "px_last"}}], "for": {"list": [{"str": "AAPL US"}]}, "with": {}, "where": None,
      "group": [], "agg": [], "sort": [], "limit": None}),
    ("get(px_last, chg_pct(px, 1M) as m1) for(members('S&P 500') & sector('Tech')) "
     "with(dates=range(-2Y, 0D), freq=W) where(roic > 15% and not tech.ivol_pct >= 30) "
     "group(sector).agg(median) sort(m1 desc) limit(50)",
     {"get": [{"expr": {"name": "px_last"}},
              {"expr": {"call": "chg_pct", "args": [{"name": "px"}, {"dur": "1M"}]}, "as": "m1"}],
      "for": {"op": "and", "l": {"call": "members", "args": [{"str": "S&P 500"}]},
              "r": {"call": "sector", "args": [{"str": "Tech"}]}},
      "with": {"dates": {"call": "range", "args": [{"dur": "-2Y"}, {"dur": "0D"}]}, "freq": {"name": "W"}},
      "where": {"op": "and", "l": {"op": ">", "l": {"name": "roic"}, "r": {"num": 0.15, "pct": True}},
                "r": {"op": "not", "x": {"op": ">=", "l": {"name": "tech.ivol_pct"}, "r": {"num": 30.0}}}},
      "group": [{"name": "sector"}], "agg": ["median"], "sort": [{"expr": {"name": "m1"}, "desc": True}], "limit": 50}),
    ("get(cs_zscore(roic, by=sector)) for(region('EU') | region('UK') - country('CH')) with(as_of=2026-01-31)",
     {"get": [{"expr": {"call": "cs_zscore", "args": [{"name": "roic"}], "kwargs": {"by": {"name": "sector"}}}}],
      "for": {"op": "or", "l": {"call": "region", "args": [{"str": "EU"}]},
              "r": {"op": "-", "l": {"call": "region", "args": [{"str": "UK"}]},
                    "r": {"call": "country", "args": [{"str": "CH"}]}}},
      "with": {"as_of": {"date": "2026-01-31"}}, "where": None, "group": [], "agg": [], "sort": [], "limit": None}),
    ("GET(a + b * c ^ 2 - -d) FOR(['X']) WHERE(sector in ['TECH', 'HLTH'] or x = null)",
     {"get": [{"expr": {"op": "-", "l": {"op": "+", "l": {"name": "a"},
                                          "r": {"op": "*", "l": {"name": "b"},
                                                "r": {"op": "^", "l": {"name": "c"}, "r": {"num": 2.0}}}},
                        "r": {"op": "-", "x": {"name": "d"}}}}],
      "for": {"list": [{"str": "X"}]}, "with": {},
      "where": {"op": "or", "l": {"op": "in", "l": {"name": "sector"}, "r": {"list": [{"str": "TECH"}, {"str": "HLTH"}]}},
                "r": {"op": "==", "l": {"name": "x"}, "r": {"null": True}}},
      "group": [], "agg": [], "sort": [], "limit": None}),
]


@pytest.mark.parametrize("text,want", GOLDEN)
def test_golden_ast(text, want):
    assert to_dict(parse(text)) == want


@pytest.mark.parametrize("text,_", GOLDEN)
def test_canonical_text_round_trips(text, _):
    q = parse(text)
    canon = query_text(q)
    assert to_dict(parse(canon)) == to_dict(q)
    assert ast_hash(parse(canon)) == ast_hash(q)


def test_literals():
    e = parse_expr("-2Y")
    assert isinstance(e, Dur) and e.n == -2 and e.unit == "Y"
    assert isinstance(parse_expr("15%"), Num) and parse_expr("15%").value == pytest.approx(0.15)
    assert to_dict(parse_expr("2026-01-01")) == {"date": "2026-01-01"}
    assert to_dict(parse_expr("'it''s'")) == {"str": "it's"}
    assert to_dict(parse_expr('"x"')) == {"str": "x"}
    assert to_text(parse_expr("chg_pct(px,  1M)")) == "chg_pct(px,1M)"
    assert to_text(parse_expr("(a + b) * c")) == "(a+b)*c"
    assert to_text(parse_expr("a - (b - c)")) == "a-(b-c)"
    assert [t.kind for t in tokenize("1D 2w 3M 1q 5y 1e3 2026-02-28")][:-1] == ["dur"] * 5 + ["num", "date"]
    assert date(2026, 2, 28) == parse_expr("2026-02-28").value  # type: ignore[attr-defined]


def test_hash_ignores_whitespace_and_case_of_clauses():
    a = parse("get(px_last)   for( ['A'] )  limit(5)")
    b = parse("GET(px_last) FOR(['A']) LIMIT(5)")
    assert ast_hash(a) == ast_hash(b)
    assert ast_hash(a) != ast_hash(parse("get(px_last) for(['B']) limit(5)"))


@pytest.mark.parametrize("bad,msg", [
    ("", "empty query"),
    ("for(['A'])", "needs get"),
    ("get(px) get(px) for(['A'])", "duplicate clause"),
    ("get(px for(['A'])", "expected"),
    ("get(px) for(['A']) limit(0)", "positive integer"),
    ("get(px) for(['A']) limit(2.5)", "positive integer"),
    ("get(px) for(['A']) foo(1)", "expected a clause"),
    ("get(f(a=1, 2)) for(['A'])", "positional argument after a keyword"),
    ("get(px) for(['A']) with(1)", "key=value"),
    ("get(px as 3) for(['A'])", "column name"),
    ("get(px) for(['A']) group(sector).sum(median)", "agg"),
    ("get(tech.x(1)) for(['A'])", "not a function"),
    ("get(2026-13-01) for(['A'])", "invalid date"),
])
def test_syntax_errors(bad, msg):
    with pytest.raises(BqlSyntaxError) as ei:
        parse(bad)
    assert msg in str(ei.value)


def test_nesting_and_size_limits():
    with pytest.raises(BqlSyntaxError, match="nested too deeply"):
        parse("get(" + "(" * 60 + "px" + ")" * 60 + ") for(['A'])")
    with pytest.raises(BqlSyntaxError, match="too long"):
        parse("get(px) for(['" + "A" * 9000 + "'])")
    with pytest.raises(BqlSyntaxError, match="string literal too long"):
        parse("get(px) for(['" + "A" * 600 + "'])")
