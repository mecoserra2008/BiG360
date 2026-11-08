"""Live news engine (roadmap v3 N2-N4; owner: news-live). See docs/ROADMAP_V3_PLAN.md section N.

* feeds.py       curated RSS / Atom list (config/news_feeds.yaml), conditional GET poller (ETag / If-Modified-Since,
                 per-host politeness, back-off), item parser (headline + <= 300-char snippet, never article pages)
* verify.py      `python -m app.news_live.verify`: checks every configured feed (HTTP, items, robots.txt, terms)
* hot.py         hot store paths + atomic parquet IO (GDELT slot frames, hourly news items / story assignments)
* linker.py      headline -> issuer linking with the events_v2 alias table and context rules
* desk.py        NewsDesk consumer: dedupe (URL hash + MinHash), labels, shared FinBERT, live story attach ->
                 bus topics news.item / news.story, latency metrics
* compaction.py  hourly fold of the hot store into the lake (job news_hourly) + daily finalise (job news_finalise)
* intraday.py    the hourly intraday serving DuckDB (job news_intraday; read by app.api.intraday)
* alerts.py      watchlist_news / story_velocity rule evaluation (live alert consumer)
"""
