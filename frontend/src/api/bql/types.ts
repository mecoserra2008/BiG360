/** BQL-lite (contracts/api.yaml v1.37.0, tag bql). */

export type BqlUnit = string | null

export interface BqlColumn {
  name: string
  unit: BqlUnit
  dtype: 'number' | 'text' | 'bool' | 'date' | 'list'
  role: 'key' | 'value' | 'group' | 'date'
  field: string | null
  description: string | null
  currency: string | null
  model_based: boolean
}

export interface BqlDates {
  start: string
  end: string
  freq: 'D' | 'W' | 'M' | 'Q' | null
  n: number
}

export type BqlCell = string | number | boolean | null | BqlCell[]

export interface BqlResult {
  query: string
  mode: 'cross_section' | 'timeseries'
  grouped: boolean
  as_of: string
  snapshot_as_of: string
  serving_run_id: string
  dates: BqlDates | null
  currency: string | null
  fill: 'prev' | 'none'
  universe_size: number
  columns: BqlColumn[]
  rows: BqlCell[][]
  n_rows: number
  truncated: boolean
  units: Record<string, BqlUnit>
  warnings: string[]
  fields?: Record<string, unknown>[]
  cached: boolean
  elapsed_ms?: number
}

export interface BqlExplain {
  query: string
  ast: Record<string, unknown>
  mode: 'cross_section' | 'timeseries'
  grouped: boolean
  as_of: string
  universe_sql: string
  universe_size: number
  sql: string
  params: { name: string; type: string }[]
  columns: BqlColumn[]
  estimates: { cells: number; daily_rows: number; needs_daily_panel: boolean; daily_from?: string | null }
  warnings: string[]
}

export interface BqlField {
  mnemonic: string
  description: string
  unit: string
  dtype: 'number' | 'text' | 'bool' | 'date'
  group: string
  table: string | null
  column: string | null
  key: string
  frequency: string
  pit: string
  source: string
  aliases: string[]
  static: boolean
  model_based: boolean
  curated: boolean
  has_history: boolean
  history: { kind: string; table?: string | null; frequency: string; expr?: string } | null
}

export interface BqlFieldPage {
  total: number
  limit: number
  offset: number
  groups: string[]
  counts: { fields: number; aliases: number; with_history: number }
  functions: { signature: string; description: string }[]
  items: BqlField[]
}

export interface BqlSaved {
  query_id: string
  name: string
  description: string | null
  query: string
  kind: 'query' | 'screen'
  tags: string[]
  created_at: string
  updated_at: string
  last_run_at?: string | null
}

export interface BqlSavedInput {
  name: string
  query: string
  kind?: 'query' | 'screen'
  description?: string | null
  tags?: string[]
}

export interface BqlResolve {
  query: string
  base: string
  market: string | null
  yellow_key: string | null
  match: string | null
  asset_class: 'Equity' | 'Index' | 'Curncy' | 'Comdty' | null
  security: { security_id: string; ticker: string; name: string; country: string; mic?: string | null } | null
  market_cap_usd: number | null
}
