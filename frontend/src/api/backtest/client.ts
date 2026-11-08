import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type {
  BtCatalog,
  BtCompare,
  BtFromScreen,
  BtFromScreenRequest,
  BtHoldings,
  BtRun,
  BtRunMode,
  BtRunStatus,
  BtSpec,
  BtStrategy,
  BtStrategyDetail,
  BtTrades,
  BtValidation,
} from './types'

/** The backtesting lab has no mock implementation: it needs the backend engine and the Parquet lake. */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/backtest`, 'operation Backtest lab needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent

export const backtestApi = {
  async catalog(): Promise<BtCatalog> {
    live()
    return http.get('/backtest/catalog')
  },
  async validate(spec: BtSpec): Promise<BtValidation> {
    live()
    return http.post('/backtest/validate', { spec })
  },
  async fromScreen(q: BtFromScreenRequest): Promise<BtFromScreen> {
    live()
    return http.post('/backtest/from-screen', q)
  },
  /** mode auto: inline when cheap (200, finished), else queued on the worker (202) - poll `run()`. */
  async submit(spec: BtSpec, opts: { mode?: BtRunMode; strategyId?: string | null } = {}): Promise<BtRun> {
    live()
    return http.post('/backtest/runs', { spec, mode: opts.mode ?? 'auto', strategy_id: opts.strategyId ?? null })
  },
  async run(runId: string): Promise<BtRun> {
    live()
    return http.get(`/backtest/runs/${enc(runId)}`)
  },
  async runs(strategyId?: string | null, limit = 100): Promise<BtRunStatus[]> {
    live()
    return http.get('/backtest/runs', { strategy_id: strategyId ?? undefined, limit })
  },
  async cancel(runId: string): Promise<BtRunStatus> {
    live()
    return http.delete(`/backtest/runs/${enc(runId)}`)
  },
  async holdings(runId: string, date?: string | null, limit = 500): Promise<BtHoldings> {
    live()
    return http.get(`/backtest/runs/${enc(runId)}/holdings`, { date: date ?? undefined, limit })
  },
  async trades(runId: string, opts: { entityId?: string | null; limit?: number; offset?: number } = {}): Promise<BtTrades> {
    live()
    return http.get(`/backtest/runs/${enc(runId)}/trades`, { entity_id: opts.entityId ?? undefined, limit: opts.limit ?? 500, offset: opts.offset ?? 0 })
  },
  async exportPython(runId: string): Promise<string> {
    live()
    return http.get<string>(`/backtest/runs/${enc(runId)}/export.py`)
  },
  async compare(runIds: string[]): Promise<BtCompare> {
    live()
    return http.post('/backtest/compare', { run_ids: runIds })
  },
  async strategies(): Promise<BtStrategy[]> {
    live()
    return http.get('/backtest/strategies')
  },
  async strategy(id: string): Promise<BtStrategyDetail> {
    live()
    return http.get(`/backtest/strategies/${enc(id)}`)
  },
  async createStrategy(body: { name: string; description?: string | null; spec: BtSpec; tags?: string[]; origin?: Record<string, unknown> | null }): Promise<BtStrategy> {
    live()
    return http.post('/backtest/strategies', { name: body.name, description: body.description ?? null, spec: body.spec, tags: body.tags ?? [], origin: body.origin ?? null })
  },
  async updateStrategy(id: string, patch: { name?: string; description?: string | null; spec?: BtSpec; tags?: string[]; schedule?: 'none' | 'weekly'; drift_threshold?: number }): Promise<BtStrategy> {
    live()
    return http.patch(`/backtest/strategies/${enc(id)}`, patch)
  },
  async deleteStrategy(id: string): Promise<void> {
    live()
    await http.del(`/backtest/strategies/${enc(id)}`)
  },
  async runStrategy(id: string, mode: BtRunMode = 'auto'): Promise<BtRun> {
    live()
    return http.post(`/backtest/strategies/${enc(id)}/run`, { mode })
  },
}
