# BiG Equity 360 — Master Plan (approved 2026-10-10)

Local, single-user, 360º fundamental + quant equity analysis platform. Orange BiG branding (Logo-BIG.png).

## Decisions
- Deploy: LOCAL (Windows 11, 20 threads, 32 GB RAM, no GPU). Docker optional, native run required.
- Scope: GLOBAL listings — priority US, EU, UK, Japan, South Korea, China/HK, LATAM; anything else parseable is a bonus.
  US = full depth (SEC EDGAR XBRL, Form 4). Other regions via national filing APIs + yfinance fallback.
- Prices: daily. Polygon (Massive) + yfinance complement each other; respect Polygon free-tier limits.
- Keys in .env: FRED_APIKEY, Polygon_APIKEY (never print or commit). LLM features: OFF.
- Stack: Python 3.13 (.venv) · FastAPI · DuckDB over Parquet · Polars · APScheduler · XGBoost · PyTorch (CPU) · FinBERT/multilingual sentiment
  React + TypeScript + Vite · Tailwind · TanStack Query/Table · Lightweight Charts · ECharts.
- Point-in-time correctness is mandatory (knowledge_date = filing/publication date).

## Modules
Taxonomy · Fundamentals + sector KPI registry · Peer/self-history/market-cap scoring · Moat · 3-statement forecast + reverse DCF
Insider anomaly (XGBoost + IsolationForest) · Sentiment (FinBERT + GDELT tone + Lazy Prices) · GDELT events · Macro regime (FRED + global)
Technicals (backtested level-type ranking → confluence zones) · ML (XGBoost baseline + multi-factor Transformer, walk-forward) · Extras (Piotroski, Altman, Beneish, 13F, short interest)

## Phases
0. System design + global data-source research + frontend shell  (parallel)
1. Ingestion: EDGAR/Form4 · global filings · prices/FRED/GDELT/macro
2. Analytics: fundamentals/scoring/moat/forecast · technicals · ML/NLP
3. Wire frontend ↔ API, bug-hunt, end-to-end run. MVP = ~500 US large caps + top global names first.
