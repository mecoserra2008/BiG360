# BiG Equity 360 — frontend

Vite + React 19 + TypeScript (strict) + Tailwind v4, TanStack Query/Table/Virtual, React Router 7, Zustand,
TradingView Lightweight Charts v5, Apache ECharts 6 (tree-shaken core), Radix primitives, cmdk, lucide.

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # tsc -b + vite build → dist/
npm run lint     # oxlint
npm test         # vitest — adapter unit tests (src/api/adapters/__tests__)
```

## Data source switches (`.env`, see `.env.example`)

| var | default | meaning |
|---|---|---|
| `VITE_USE_MOCK` | `false` in `.env` (code default `true`) | `true` → deterministic in-browser mock (`src/api/mock`); `false` → FastAPI backend (contract v1.2) |
| `VITE_API_URL` | `http://localhost:8000` | backend base URL (`/health`, `/openapi.json`, `/api/v1/...`) |
| `VITE_MOCK_FALLBACK` | `false` | live mode only: endpoints answering 501 / unreachable are served by the mock. `true` = all, or a comma list of `Api` methods (`screener,modelLab`). Top bar shows "Mock fallback · n". |

Without fallback, unimplemented endpoints render **"Not yet available — pipeline pending"** in their panel.
The client reads the backend's `/openapi.json` once (501 stubs are hidden from it) and answers those
endpoints locally, so the browser console stays free of failed-request noise.

## API layer

```
pages → src/api/hooks.ts → api (src/api/client.ts) ─┬─ mockApi (src/api/mock)
                                                     └─ httpApi → src/api/http.ts → contract JSON
                                                                 → src/api/adapters/* (pure) → UI types (src/api/types.ts)
```

- Contract types are generated from `../contracts/api.yaml`:
  `npx -y openapi-typescript@7.13.0 ../contracts/api.yaml -o src/api/generated/schema.d.ts`
  (run via npx because openapi-typescript's peer range is TypeScript ^5; this app uses TS 6).
- `src/api/remote.ts` mirrors the watchlist / alert rules to `/watchlists` and `/alerts`, falling back to
  localStorage when the API is in mock mode, returns 501 or is unreachable.

## Layout

- `src/components/ui` — design-system primitives (Button, Badge, Panel, Tabs, inputs, Delta, PercentileBar, Flag, Pending/Error states …)
- `src/components/charts` — themed ECharts wrapper, lightweight-charts `PriceChart` (zones as a series primitive), sparkline, gauge, world map
- `src/components/tables` — virtualized/sortable/pinnable `DataTable` and shared column sets
- `src/components/layout` — app shell, sidebar, top bar (region chips → dropdown below 1180px), command palette (Ctrl/⌘+K or `/`)
- `src/pages/*` — routed pages; Company 360 tabs are lazy-loaded
- `src/index.css` — design tokens (dark default, `[data-theme=light]`); charts read the same CSS variables
