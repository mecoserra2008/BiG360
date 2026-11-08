"""Delayed quotes service (docs/STARTUP_SYNC_PLAN.md section 2, workstream B; owner: realtime).

A light 5th supervisor child (`python -m app.cli realtime quotes`), separate from the realtime / live-news child
because that one already runs at ~1.3-1.5 GB with FinBERT and is restarted by its RAM cap (1536 MB) every 1-2 h.

* sessions.py  regular trading hours per exchange (MIC) + exchange holidays from the cached exchange calendars;
               FX 24x5 and CME Globex hours
* universe.py  what to poll: watchlists, open thesis subjects, main indices, FX majors, futures front months, the
               cross-asset cockpit assets and securities viewed recently in the UI (interest file)
* sources.py   Yahoo batched quotes (v7 quote endpoint via the yfinance session) + Polygon snapshot (only when the
               account plan includes it); pure parsers are fixture-tested
* store.py     data/realtime/quotes.sqlite (WAL): last quote per security + intraday ticks (2 days); labelled delayed
* service.py   the polling loop: per-session cadence (1-5 min while open, idle with a reason when closed), budgets,
               back-off, bus publish (quotes.tick / quotes.session), status file data/state/quotes_status.json

Delayed quotes are never written into the official daily bars (prices_daily): the official close still comes from the
daily finalise.
"""
