"""Real-time layer R1 (owner: realtime). See docs/EVENTS_MODELS_REALTIME_PLAN.md section R1.

* bus.py        message bus interface + SQLite WAL implementation (data/realtime/bus.sqlite) + Redis stub
* streams/      stream workers (EDGAR, FCA NSM, TDnet, HKEXnews, DART, official RSS, macro capture, GDELT, Polygon)
* enrich.py     online enrichment consumers (linking, themes, stories, sentiment, insider, change points, alerts)
* compact.py    hourly hot-store -> Parquet lake compaction (live_* tables, knowledge_ts = first seen)
* supervisor.py asyncio supervisor (restart policy, backpressure, heartbeats, status file, graceful shutdown)
"""
