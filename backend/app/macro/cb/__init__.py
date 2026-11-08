"""Macro v3 / Wave 3: central-bank monitor ("what's priced in").

Modules (owner macro-w3):
  banks       registry of the 17 central banks (official series, meeting calendars, inflation targets)
  common      HTTP helper (official sources only), lake readers, small table helpers
  rates       W3.1 policy rates, corridors, decisions (derived from rate changes + official meeting dates),
              meetings, rate-cycle phase (table cb_rate_cycle, consumed by macro-w4)
  curves      W3.2 pure curve math (par bootstrap, Svensson, forwards, implied meeting path)
  curve_sources  W3.2 official curve loaders (UST, NY Fed, ACM, BoE OIS, ECB YC, MoF, BoC, RBA, SNB)
  implied     W3.2 job: implied policy paths per bank (approximation, labelled)
  projections W3.3 Fed SEP medians (FRED/ALFRED), ECB staff projections (ECB MPD)
  stance      W3.4 real policy rate, Taylor rule (model-based), official r* (NY Fed LW/HLW)
  balance_sheets W3.5 Fed H.4.1 (FRED), ECB ILM, BoE weekly (IADB), BoJ accounts (stat-search)
  tone        W3.6 communication tone (model-based: local RoBERTa hawk/dove classifier + lexicon fallback)
Job entry points: run(ctx) in rates (cb_rates), implied (cb_curves_implied), projections (cb_projections),
stance (cb_stance), balance_sheets (cb_balance_sheets), tone (cb_tone).
"""
