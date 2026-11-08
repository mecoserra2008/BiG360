import type { components } from '../generated/schema'

type S = components['schemas']

/** Contract types (contracts/api.yaml v1.24.0, tag backtest). The spec is edited as plain contract JSON. */
export type BtSpec = S['BacktestSpec']
export type BtUniverse = S['BacktestUniverse']
export type BtSignal = S['BacktestSignal']
export type BtSignalVar = S['BacktestSignalVar']
export type BtConstruction = S['BacktestConstruction']
export type BtEvent = S['BacktestEvent']
export type BtCosts = S['BacktestCosts']
export type BtPeriod = S['BacktestPeriod']
export type BtRobustness = S['BacktestRobustness']
export type BtScreenRule = S['BacktestScreenRule']
export type BtRegimeFilter = S['BacktestRegimeFilter']
export type BtCatalog = S['BacktestCatalog']
export type BtCatalogSource = S['BacktestCatalogSource']
export type BtValidation = S['BacktestValidation']
export type BtFromScreen = S['BacktestFromScreen']
export type BtFromScreenRequest = S['BacktestFromScreenRequest']
export type BtRunStatus = S['BacktestRunStatus']
export type BtRun = S['BacktestRun']
export type BtResult = S['BacktestResult']
export type BtMetrics = S['BacktestMetrics']
export type BtSeries = S['BacktestSeries']
export type BtHolding = S['BacktestHolding']
export type BtHoldings = S['BacktestHoldings']
export type BtTrades = S['BacktestTrades']
export type BtCompare = S['BacktestCompare']
export type BtStrategy = S['BacktestStrategy']
export type BtStrategyDetail = S['BacktestStrategyDetail']

/** Editable draft: the spec with every block present (defaults filled), assignable to BtSpec. */
export type BtDraft = Omit<BtSpec, 'universe' | 'construction' | 'costs' | 'period' | 'robustness'> & {
  universe: BtUniverse
  construction: BtConstruction
  costs: BtCosts
  period: BtPeriod
  robustness: BtRobustness
}

export type BtRunMode = 'auto' | 'sync' | 'async'
export type BtStatus = BtRunStatus['status']

/** Loosely typed blocks of the result (additionalProperties objects in the contract). */
export interface BtDeciles {
  n_quantiles: number
  n_periods: number
  dates?: string[]
  mean_return?: number[]
  ann_return?: number[]
  hit_rate?: number[]
  cumulative?: number[][]
  spread_mean?: number
  spread_ann?: number
  spread_t?: number | null
  spread_cumulative?: number[]
  ic?: (number | null)[]
  ic_mean?: number | null
  ic_ir?: number | null
  ic_t?: number | null
  ic_hit_rate?: number | null
  monotonicity?: number | null
}

export interface BtFactorRow {
  factor: string
  label: string
  beta: number
  t_stat: number | null
  factor_mean_ann: number
  contribution_ann: number
}

export interface BtAttributionModel {
  n: number
  alpha_ann?: number
  alpha_t?: number | null
  r2?: number | null
  total_ann?: number
  factors: BtFactorRow[]
}

export interface BtAttribution {
  periods?: number
  style?: BtAttributionModel
  factors?: BtAttributionModel
  exposures?: Record<string, number | null>
  note?: string
}

export interface BtPermutation {
  n: number
  sharpes: number[]
  mean: number
  std: number | null
  actual_gross_sharpe: number | null
  p_value: number
  note?: string
}

export interface BtDsr {
  sr0: number
  dsr: number
  psr: number
  n_trials: number
  sr_var: number
  sharpe_ann: number
  sr0_ann: number
  window: string
  n_obs: number
  skew: number | null
  kurtosis: number | null
  sr_var_source: string
  n_trials_this_run: number
  n_trials_prior: number
}

export interface BtSweepRow {
  params: Record<string, unknown>
  is_sharpe?: number | null
  oos_sharpe?: number | null
  is_cagr?: number | null
  error?: string
}

export interface BtSweep {
  n: number
  grid: BtSweepRow[]
  best_in_sample?: Record<string, unknown> | null
  walk_forward?: { folds: { fold: number; params: Record<string, unknown> | null; train_sharpe: number | null; test_sharpe: number | null; test_start_date: string; test_end_date: string }[]; metrics: BtMetrics; dates: string[]; nav: number[] } | null
  pbo?: { pbo: number | null; n_combinations?: number; n_strategies?: number; logits?: number[]; prob_oos_loss?: number; degradation_slope?: number | null; note?: string }
  holdout?: { params: Record<string, unknown>; is_sharpe: number | null; metrics: BtMetrics } | null
  note?: string
}

export interface BtRobustnessView {
  n_trials?: number
  dsr?: BtDsr
  permutation?: BtPermutation
  stale?: { sharpe: number | null; actual_gross_sharpe: number | null; delta: number | null; suspicious: boolean; lag: string; note: string }
  sweep?: BtSweep
}

export interface BtEventBlock {
  n_events: number
  n_trades: number
  hit_rate: number | null
  avg_return: number | null
  median_return: number | null
  avg_win: number | null
  avg_loss: number | null
  profit_factor: number | null
  avg_holding_days: number | null
  exit_reasons: Record<string, number>
  path: { days: number[]; n: number; mean: number[]; lo: number[]; hi: number[]; excess_mean: number[] } | null
}

export interface BtRebalance {
  date: string
  exec_date: string
  turnover: number
  cost: number
  n_long: number
  n_short: number
  n_eligible: number
  note: string | null
}

/** Where a draft came from (Screener / Studio / cross-asset / CFTC entry points). */
export interface BtOrigin {
  kind: 'screener' | 'studio' | 'cross_asset' | 'cftc' | 'preset' | 'manual'
  ref?: string
  label?: string
  notes?: string[]
}
