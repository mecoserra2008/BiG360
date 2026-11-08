"""BQL-lite: a small declarative query language over the serving snapshot (docs/ROADMAP_V3_PLAN.md section B1).

    get(px_last, chg_pct(px, 1M), pe_fwd, roic, tech.hi52_pct)
      for(members('S&P 500') & sector('Tech'))
      with(dates=range(-2Y, 0D), freq=W, currency=USD, fill=prev)
      where(roic > 15% and tech.ivol_pct < 30)
      sort(chg_pct desc) limit(50)

Modules
* `grammar`  - hand-written lexer + recursive-descent parser -> AST (no lark dependency), canonical text, AST hash.
* `fields`   - the field dictionary (mnemonic -> serving table / column, unit, PIT rule, history source), generated
               from contracts/schemas + the serving builder and a curated alias layer; published as
               contracts/bql_fields.yaml (`python -m app.bql.fields --write`).
* `compiler` - AST -> parameterised DuckDB SQL over the serving snapshot (identifiers only from the dictionary, every
               user literal is a `?` parameter).
* `engine`   - execution with limits (rows, universe x dates, timeout), result cache keyed by AST hash + snapshot id,
               exports (CSV / XLSX / JSON) and the panel helper used by Model Studio and the Backtest Lab.
* `store`    - saved queries (app_state table bql_saved).
"""
