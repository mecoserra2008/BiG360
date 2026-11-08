/** Contract-typed Model Studio fixtures (shapes copied from the live v1.5 backend). */
import type { components } from '../../generated/schema'

type S = components['schemas']

export const dlPreset: S['StudioPreset'] = {
  id: 'sector_returns_lagged_rates',
  title: 'Financials vs lagged rate changes',
  description: 'Distributed-lag OLS of financials returns on lagged rate changes',
  method: 'distributed_lag',
  tags: ['regression', 'rates'],
  study: {
    name: 'Financials vs lagged rate changes',
    method: 'distributed_lag',
    y: 'fins_ret',
    x: ['d_10y', 'd_slope'],
    params: { lags: 3, cov_type: 'HAC' },
    seed: 42,
    timeout_s: 600,
    dataset: {
      kind: 'timeseries',
      frequency: 'M',
      start: '2005-01-01',
      lookback: 0,
      entity_level: 'security',
      macro_alignment: 'auto',
      pit_universe: true,
      max_entities: 5000,
      max_rows: 2000000,
      missing: { ffill_limit: 0, fill: 'none', drop: 'any' },
      variables: [
        { name: 'fins_ret', source: 'prices', field: 'ret', entity_agg: 'mean', entities: { universe: 'mvp_us', sectors: ['FINS'], primary_only: true } },
        { name: 'd_10y', source: 'macro', field: 'fred:DGS10', transforms: [{ op: 'diff' }] },
        { name: 'd_slope', source: 'macro', field: 'fred:T10Y2Y', transforms: [{ op: 'diff' }], max_stale_days: 10 },
      ],
    },
  },
}

export const kmeansSpec: S['StudioStudySpec'] = {
  name: 'MVP US peer clusters',
  method: 'kmeans',
  x: ['roic', 'op_margin'],
  params: { k_min: 3 },
  seed: 42,
  timeout_s: 600,
  dataset: {
    kind: 'cross_section',
    frequency: 'M',
    lookback: 0,
    entity_level: 'security',
    macro_alignment: 'auto',
    pit_universe: true,
    max_entities: 5000,
    max_rows: 2000000,
    entities: { universe: 'mvp_us', primary_only: true },
    variables: [
      { name: 'roic', source: 'ratios', field: 'roic', transforms: [{ op: 'winsorize', lower: 0.02, upper: 0.98 }] },
      { name: 'op_margin', source: 'ratios', field: 'operating_margin' },
    ],
  },
}

export const dlMethod: S['StudioMethod'] = {
  id: 'distributed_lag',
  family: 'regression',
  title: 'Distributed lag',
  description: 'OLS with lags 0..L of each regressor',
  shapes: ['timeseries', 'panel'],
  needs_y: true,
  min_x: 1,
  max_x: null,
  uses_weights: false,
  outputs: ['coefficients'],
  diagnostics: ['breusch_pagan'],
  params_schema: {
    type: 'object',
    properties: {
      add_constant: { default: true, title: 'Add Constant', type: 'boolean' },
      cov_type: { default: 'HAC', enum: ['nonrobust', 'HC1', 'HAC', 'cluster'], title: 'Cov Type', type: 'string' },
      hac_maxlags: { anyOf: [{ maximum: 60, minimum: 0, type: 'integer' }, { type: 'null' }], default: null, title: 'Hac Maxlags' },
      alpha: { default: 0.05, exclusiveMaximum: 0.5, exclusiveMinimum: 0, title: 'Alpha', type: 'number' },
      lags: { default: 3, maximum: 36, minimum: 0, title: 'Lags', type: 'integer' },
      lag_overrides: { additionalProperties: { type: 'integer' }, title: 'Lag Overrides', type: 'object' },
      order: { default: [1, 0, 0], items: { type: 'integer' }, maxItems: 3, minItems: 3, title: 'Order', type: 'array' },
      quantiles: { default: [0.1, 0.5, 0.9], items: { type: 'number' }, maxItems: 19, minItems: 1, title: 'Quantiles', type: 'array' },
      trend: { anyOf: [{ enum: ['n', 'c', 't', 'ct'], type: 'string' }, { type: 'null' }], default: null, title: 'Trend' },
      exog_values: { additionalProperties: { items: { type: 'number' }, type: 'array' }, title: 'Exog Values', type: 'object' },
    },
  },
}

export const kmeansMethod: S['StudioMethod'] = {
  ...dlMethod,
  id: 'kmeans',
  family: 'clustering',
  title: 'k-means',
  shapes: ['timeseries', 'panel', 'cross_section'],
  needs_y: false,
  min_x: 1,
  params_schema: { type: 'object', properties: { k_min: { default: 2, minimum: 2, maximum: 50, title: 'K Min', type: 'integer' } } },
}

export const runPayload: S['StudioRun'] = {
  run: {
    run_id: 'SR1',
    method: 'distributed_lag',
    status: 'completed',
    mode: 'sync',
    created_at: '2026-10-11T21:08:22Z',
    duration_s: 0.085,
    metrics: { r2: 0.05, aic: -735.3, bad: Number.NaN, label: 'x' },
    n_obs: 258,
  },
  result: {
    run_id: 'SR1',
    method: 'distributed_lag',
    status: 'completed',
    study: dlPreset.study,
    dataset: {
      key: 'cc61',
      snapshot_id: 'R1',
      kind: 'timeseries',
      frequency: 'M',
      start: '2005-01-31',
      end: '2026-09-30',
      n_rows: 258,
      coverage: [{ column: 'fins_ret', n_non_null: 258, pct_non_null: 1, uses_future: false }],
      warnings: [],
    },
    result: {
      method: 'distributed_lag',
      family: 'regression',
      title: 'Distributed lag',
      summary: { dependent: 'fins_ret' },
      fit_stats: { r2: 0.05 },
      coefficients: [{ term: 'const', estimate: 0.012, std_error: 0.003, statistic: 3.5, p_value: 0.0005, ci_low: 0.005, ci_high: 0.019 }],
      tests: [
        { name: 'Breusch-Pagan', group: 'diagnostics', statistic: 15.3, p_value: 0.053, df: 8, conclusion: 'do not reject H0 at 5%', passed: true },
        { name: 'White', group: 'diagnostics', statistic: 165.4, p_value: 5.8e-16, conclusion: 'reject H0 at 5%', passed: false },
        { name: 'Durbin-Watson', group: 'diagnostics', statistic: 1.98, conclusion: '~2 = none; < 1.5 positive / > 2.5 negative autocorrelation (time-ordered)', passed: true },
        { name: 'Pearson dispersion (chi2/df)', group: 'diagnostics', statistic: 1.91, df: 112, conclusion: 'overdispersed', passed: false },
        { name: 'd_10y lags (joint F)', group: 'lag_blocks', statistic: 1.32, p_value: 0.26, df: [4, 249], conclusion: 'do not reject H0 at 5%', passed: null },
        { name: 'ADF ip (diff 0)', group: 'stationarity', statistic: -13.6, p_value: 1e-25, conclusion: 'reject H0 at 5%' },
      ],
      tables: [],
      plots: {},
      warnings: [{ code: 'multiple_testing', severity: 'info', message: '8 coefficients tested at once' }],
      metrics: {},
    },
    reproducibility: { spec_hash: 'abc', dataset_key: 'cc61', snapshot_id: 'R1', seed: 42, versions: { numpy: '2' } },
  },
}

export const comparePayload: S['StudioCompare'] = {
  run_ids: ['SR1', 'SR2'],
  runs: [runPayload.run, { ...runPayload.run, run_id: 'SR2' }],
  metrics: [{ metric: 'r2', values: { SR1: 0.05, SR2: null } }],
  coefficients: [{ term: 'd_10y', values: { SR1: { estimate: 0.06, std_error: 0.04, p_value: 0.17 }, SR2: null } }],
}

/** IV / DiD params schemas (v1.6 shapes: $defs + $ref, string arrays, date format, integer enums). */
export const ivMethod: S['StudioMethod'] = {
  ...dlMethod,
  id: 'iv2sls',
  title: 'IV',
  shapes: ['timeseries', 'panel', 'cross_section'],
  min_x: 0,
  params_schema: {
    type: 'object',
    required: ['endog', 'instruments'],
    properties: {
      endog: { items: { type: 'string' }, minItems: 1, title: 'Endog', type: 'array' },
      instruments: { items: { type: 'string' }, minItems: 1, title: 'Instruments', type: 'array' },
      estimator: { default: '2sls', enum: ['2sls', 'liml', 'gmm'], title: 'Estimator', type: 'string' },
    },
  },
}

export const didSchema = {
  type: 'object',
  required: ['treatment'],
  $defs: {
    TreatEvent: { properties: { entity: { type: 'string' }, date: { format: 'date', type: 'string' } }, required: ['entity', 'date'], type: 'object' },
    Treatment: {
      properties: {
        mode: { default: 'entities', enum: ['entities', 'attribute', 'column', 'events'], type: 'string' },
        events: { items: { $ref: '#/$defs/TreatEvent' }, type: 'array' },
        date: { anyOf: [{ format: 'date', type: 'string' }, { type: 'null' }], default: null },
      },
      title: 'Treatment',
      type: 'object',
    },
  },
  properties: {
    treatment: { $ref: '#/$defs/Treatment' },
    events: { items: { $ref: '#/$defs/TreatEvent' }, title: 'Events', type: 'array' },
    treatment_date: { format: 'date', title: 'Treatment Date', type: 'string' },
    donors: { items: { type: 'string' }, title: 'Donors', type: 'array' },
    det_order: { default: 0, enum: [-1, 0, 1], title: 'Det Order', type: 'integer' },
  },
}
