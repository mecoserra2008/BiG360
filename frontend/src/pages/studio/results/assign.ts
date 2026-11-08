/** Routes each result output (plots, tables) to the tab that renders it; leftovers go to "Other output". */
import type { StudioPlot, StudioResultView, StudioTable } from '@/api/studio/types'

/** Method-specific result tab (causal effects, IRFs, regimes, volatility …). */
export interface SpecialTab {
  label: string
  plots: [string, StudioPlot][]
  tables: StudioTable[]
}

const PROB_PLOTS = /^(pit|calibration|fan|conformal_band|slice_.*|forest|trace|pinball)$/

const SPECIAL: Record<string, { label: string; plots: RegExp; tables: string[] }> = {
  did: { label: 'Treatment effect', plots: /^(event_path)$/, tables: ['att', 'means_2x2', 'event_path', 'dynamic_att', 'group_time_att'] },
  event_car: { label: 'Event study', plots: /^(event_path|aar)$/, tables: ['events'] },
  synthetic_control: { label: 'Synthetic control', plots: /^(synthetic|gaps)$/, tables: ['weights'] },
  var: { label: 'IRF & FEVD', plots: /^(irf|fevd)$/, tables: ['lag_selection', 'granger'] },
  vecm: { label: 'Cointegration', plots: /^(ect)$/, tables: ['johansen', 'beta', 'alpha'] },
  cointegration: { label: 'Spread', plots: /^(spread|zscore|legs)$/, tables: ['johansen'] },
  garch: { label: 'Volatility', plots: /^(cond_vol|var_backtest)$/, tables: ['var_backtest'] },
  markov_switching: { label: 'Regimes', plots: /^(regime_prob)$/, tables: ['regimes', 'transition'] },
  state_space: { label: 'Components', plots: /^component_/, tables: [] },
  tv_beta: { label: 'Beta paths', plots: /^beta_/, tables: ['betas'] },
  fama_macbeth: { label: 'Premia', plots: /^lambdas/, tables: [] },
  regularized: { label: 'Regularization path', plots: /^(coef_path|cv_curve)$/, tables: ['cv_folds'] },
  tree_ensemble: { label: 'Explainability', plots: /^(shap_beeswarm|importance|pdp_.*)$/, tables: ['importance', 'shap', 'cv_folds'] },
  factor_model: { label: 'Factors', plots: /^(factor_cum|beta_heatmap|alphas)$/, tables: ['factors', 'time_series'] },
  leaderboard: { label: 'Leaderboard', plots: /^(leaderboard|cum_loss_diff)$/, tables: ['leaderboard', 'cv_folds'] },
  dml: { label: 'Treatment effect', plots: /^(overlap|residuals)$/, tables: ['nuisance', 'repetitions', 'learner_sensitivity'] },
  propensity: { label: 'Balance & overlap', plots: /^(overlap|love)$/, tables: ['balance'] },
  rdd: { label: 'Discontinuity', plots: /^(rd_plot|bandwidth_sensitivity)$/, tables: ['bandwidth', 'bandwidth_sensitivity', 'covariate_continuity', 'placebo'] },
  coef_stability: { label: 'Stability', plots: /^(beta_path|r2_path)$/, tables: ['stability', 'oster'] },
  lightgbm: { label: 'Explainability', plots: /^(shap_beeswarm|importance|learning_curve)$/, tables: ['importance', 'shap', 'best_iterations', 'cv_folds'] },
  optuna_tune: { label: 'Tuning', plots: /^(optimization_history|param_importance)$/, tables: ['holdout', 'best_params', 'trials'] },
  stacking: { label: 'Stack', plots: /^(leaderboard|cum_loss_diff)$/, tables: ['leaderboard', 'meta_weights', 'cv_folds'] },
  ltr: { label: 'Ranking', plots: /^(ic_by_date|cum_spread)$/, tables: ['ranking', 'cv_folds'] },
  mlp: { label: 'Training', plots: /^(training_curve|importance)$/, tables: ['importance', 'best_epochs', 'cv_folds'] },
  rnn: { label: 'Training', plots: /^(training_curve|oos_fit)$/, tables: ['cv_folds'] },
  autoencoder: { label: 'Factors', plots: /^(factors|loadings_heatmap|training_curve)$/, tables: ['reconstruction', 'feature_r2'] },
  // v1.50 probabilistic family: PIT, calibration, fans / conformal bands, partial-pooling forest, Gibbs traces
  bayes_ridge: { label: 'Uncertainty', plots: PROB_PLOTS, tables: ['prob_scores', 'conformal', 'relevance'] },
  gp_regression: { label: 'Uncertainty', plots: PROB_PLOTS, tables: ['prob_scores', 'conformal', 'kernels'] },
  quantile_gbm: { label: 'Uncertainty', plots: PROB_PLOTS, tables: ['prob_scores', 'pinball', 'conformal'] },
  ngboost: { label: 'Uncertainty', plots: PROB_PLOTS, tables: ['prob_scores', 'conformal', 'best_iterations', 'importance'] },
  hier_pool: { label: 'Partial pooling', plots: PROB_PLOTS, tables: ['pooling', 'convergence', 'prob_scores'] },
}
const NEW_KINDS = new Set(['ts_band', 'event_path', 'irf', 'fevd', 'regime_prob', 'coef_path', 'shap_beeswarm'])

export interface Assigned {
  special: SpecialTab | null
  coefficients: boolean
  diagnostics: boolean
  forecast: boolean
  clusters: boolean
  pca: boolean
  quantile: boolean
  failCount: number
  coefPlots: [string, StudioPlot][]
  diagPlots: [string, StudioPlot][]
  clusterPlots: Record<string, StudioPlot>
  pcaPlots: Record<string, StudioPlot>
  coefTables: StudioTable[]
  diagTables: StudioTable[]
  clusterTables: StudioTable[]
  pcaTables: StudioTable[]
  summaryTables: StudioTable[]
  otherPlots: [string, StudioPlot][]
  otherTables: StudioTable[]
}

const DIAG_ORDER = ['fit', 'residuals_vs_fitted', 'qq', 'residual_acf', 'residual_hist', 'residuals_time', 'acf_y', 'roc', 'calibration']

export function assignOutputs(r: StudioResultView): Assigned {
  const plots = Object.entries(r.plots)
  const used = new Set<string>()
  const take = (pred: (k: string, p: StudioPlot) => boolean): [string, StudioPlot][] => {
    const out = plots.filter(([k, p]) => !used.has(k) && pred(k, p))
    out.forEach(([k]) => used.add(k))
    return out
  }
  const sp = SPECIAL[r.method]
  const specialPlots = take((k, p) => (sp ? sp.plots.test(k) : false) || NEW_KINDS.has(p.kind))
  const quantile = r.coefficients.some((c) => c.quantile !== null && c.quantile !== undefined)
  const isPca = r.method === 'pca'
  const isCluster = !!r.clusters

  const pcaPlots = isPca ? Object.fromEntries(take((k) => ['scree', 'loadings_heatmap', 'biplot', 'pca_scatter', 'scores_time'].includes(k))) : {}
  const clusterPlots = isCluster
    ? Object.fromEntries(take((k) => ['pca_scatter', 'elbow', 'silhouette', 'bic', 'profile_heatmap', 'correlation_heatmap', 'dendrogram'].includes(k)))
    : {}
  // forest plot is drawn from coefficients; the server's coef_ci duplicate is dropped unless there are no coefficients
  if (r.coefficients.length) take((k) => k === 'coef_ci')
  const coefPlots = take((k) => k.startsWith('lag_profile_') || k.startsWith('process_') || k === 'coef_ci')
  const diagPlots = take((k) => DIAG_ORDER.includes(k) || k.startsWith('acf_') || k.startsWith('series_')).sort(
    ([a], [b]) => (DIAG_ORDER.indexOf(a) + 1 || 99) - (DIAG_ORDER.indexOf(b) + 1 || 99),
  )
  const otherPlots = plots.filter(([k]) => !used.has(k))

  const tUsed = new Set<string>()
  const tTake = (ids: string[]): StudioTable[] => {
    const out = r.tables.filter((t) => !tUsed.has(t.id) && ids.includes(t.id))
    out.forEach((t) => tUsed.add(t.id))
    return out
  }
  const specialTables = sp ? tTake(sp.tables) : []
  const pcaTables = isPca ? tTake(['explained_variance', 'loadings']) : []
  const clusterTables = isCluster ? tTake(['k_selection', 'centroids', 'profiles']) : []
  const coefTables = tTake(['lag_blocks', 'pseudo_r2'])
  const diagTables = tTake(['stationarity', 'first_stage'])
  if (r.evaluation) tTake(['oos_eval'])
  const summaryTables = tTake(['order_search'])
  const otherTables = r.tables.filter((t) => !tUsed.has(t.id))

  return {
    special: sp || specialPlots.length ? { label: sp?.label ?? 'Paths', plots: specialPlots, tables: specialTables } : null,
    coefficients: r.coefficients.length > 0 || coefPlots.length > 0,
    diagnostics: r.tests.length > 0 || diagPlots.length > 0 || diagTables.length > 0,
    forecast: !!r.forecast || !!r.evaluation,
    clusters: isCluster,
    pca: isPca,
    quantile,
    failCount: r.tests.filter((t) => t.verdict === 'fail').length,
    coefPlots,
    diagPlots,
    clusterPlots,
    pcaPlots,
    coefTables,
    diagTables,
    clusterTables,
    pcaTables,
    summaryTables,
    otherPlots,
    otherTables,
  }
}
