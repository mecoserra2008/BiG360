import type {
  CAlert,
  CAlertCreate,
  CAlertRuleType,
  CAlertEvent,
  CClusterBuy,
  CCountryEventAgg,
  CEventTimelinePoint,
  CFactorMeta,
  CHealth,
  CGdeltEventPage,
  CInsiderFeedPage,
  CInsiderSignalRow,
  CJobRunPage,
  CJobStatus,
  CModelRun,
  CModelRunDetail,
  CSystemStatus,
  CWatchlistDetail,
} from '../contract'
import type {
  AlertEventView,
  AlertMetric,
  BackendHealth,
  FactorDefinition,
  FactorModelMeta,
  AlertOperator,
  AlertRule,
  FreshnessSummary,
  GlobalEventsPayload,
  InsiderRadarItem,
  JobRun,
  ModelLabPayload,
  ModelRun,
  RegionFilter,
  RunStatus,
  SourceFreshness,
  SystemStatus,
} from '../types'
import { headerFreshness, isOptionalTable, statusCacheView } from '../ops/adapters'
import { compositeValidationFromContract } from '@/lib/compositeLabel'
import { COUNTRIES, countryName } from '@/lib/regions'
import { type AdapterCtx, factorFromContract, factorMap, num, regionFromContract } from './common'
import { adaptGdeltEvent, adaptInsiderTxn, modelKindOf } from './company'

const fmtUsd = (v: number) => (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}k`)

/* ───────────── insider radar = feed ⊕ top-signals ⊕ clusters ───────────── */

export function adaptInsiderRadar(feed: CInsiderFeedPage | null, signals: CInsiderSignalRow[], clusters: CClusterBuy[], ctx: AdapterCtx): InsiderRadarItem[] {
  const sigBy = new Map(signals.map((s) => [s.security.security_id, s.signal]))
  const clusterBy = new Map(clusters.map((c) => [c.security.security_id, c]))
  const out: InsiderRadarItem[] = []
  const seenCluster = new Set<string>()
  const norm = (s: number | null | undefined) => {
    const x = num(s)
    return Number.isFinite(x) && x > 1 ? x / 100 : x
  }

  for (const row of feed?.items ?? []) {
    const sec = row.security
    const cl = clusterBy.get(sec.security_id)
    const sig = sigBy.get(sec.security_id)
    const t = adaptInsiderTxn(row.transaction, { id: sec.security_id, ticker: sec.ticker }, 'Insider feed', cl && !row.transaction.cluster_id ? { from: cl.first_date, to: cl.last_date } : null)
    if (t.isClusterBuy) seenCluster.add(t.clusterId ?? sec.security_id)
    const reason = t.isClusterBuy
      ? cl
        ? `Cluster buy: ${cl.n_buyers} insiders ${cl.first_date} → ${cl.last_date} (${fmtUsd(cl.total_value_usd)})`
        : `Cluster buy (${t.clusterId})`
      : sig?.c_suite_buy && t.code === 'P'
        ? 'C-suite open-market purchase'
        // the insider XGBoost P(outperform) failed its out-of-sample gate (guide 07): never a reason to flag
        : `${row.transaction.txn_type.replace(/_/g, ' ')} · ${Number.isFinite(t.valueUsd) ? fmtUsd(Math.abs(t.valueUsd)) : 'value n/a'}${typeof sig?.signal_score === 'number' ? ` · rules-based insider score ${Math.round(sig.signal_score)}` : ''}`
    out.push({
      ...t,
      // per-transaction IsolationForest score (v1.2) → else the security-level signal
      anomalyScore: Number.isFinite(t.anomalyScore) ? t.anomalyScore : norm(row.signal_score ?? sig?.anomaly_score ?? sig?.signal_score),
      companyName: sec.name,
      country: sec.country,
      region: regionFromContract(sec.region),
      sector: ctx.sectorName(sec.sector_id),
      marketCapUsd: num(sec.market_cap_usd),
      reason,
    })
  }

  // clusters with no transaction in the feed window → one summary row each
  for (const c of clusters) {
    if (seenCluster.has(c.security.security_id) || (c.cluster_id && seenCluster.has(c.cluster_id))) continue
    const sec = c.security
    const sig = sigBy.get(sec.security_id)
    out.push({
      id: `cluster-${c.cluster_id ?? sec.security_id}-${c.first_date}-${c.last_date}`,
      companyId: sec.security_id,
      ticker: sec.ticker,
      filedAt: c.last_date,
      tradeDate: c.last_date,
      insiderName: c.insiders.join(', '),
      role: `${c.n_buyers} insiders`,
      code: 'P',
      shares: NaN,
      price: NaN,
      valueUsd: c.total_value_usd,
      sharesOwnedAfter: NaN,
      ownershipChangePct: NaN,
      anomalyScore: norm(sig?.anomaly_score ?? sig?.signal_score),
      isClusterBuy: true,
      clusterId: c.cluster_id ?? null,
      source: 'Cluster',
      companyName: sec.name,
      country: sec.country,
      region: regionFromContract(sec.region),
      sector: ctx.sectorName(sec.sector_id),
      marketCapUsd: num(sec.market_cap_usd),
      reason: `Cluster buy: ${c.n_buyers} insiders ${c.first_date} → ${c.last_date} (${fmtUsd(c.total_value_usd)})`,
    })
  }
  return out.sort((a, b) => (Number.isFinite(b.anomalyScore) ? b.anomalyScore : -1) - (Number.isFinite(a.anomalyScore) ? a.anomalyScore : -1) || b.tradeDate.localeCompare(a.tradeDate))
}

/* ───────────── global events = map ⊕ timeline ⊕ feed ───────────── */

let displayNames: Intl.DisplayNames | null = null
function nameOf(cc: string): string {
  if (COUNTRIES[cc]) return countryName(cc)
  try {
    displayNames ??= new Intl.DisplayNames(['en'], { type: 'region' })
    return displayNames.of(cc) ?? cc
  } catch {
    return cc
  }
}

/** GDELT emits one event per (actor, action) coding of the same article → keep one row per headline/day/country. */
function dedupeEvents(items: CGdeltEventPage['items']): CGdeltEventPage['items'] {
  const best = new Map<string, CGdeltEventPage['items'][number]>()
  for (const e of items) {
    const key = `${(e.headline ?? e.source_url ?? e.global_event_id).toString().toLowerCase()}|${e.event_date}|${e.action_country ?? ''}`
    const cur = best.get(key)
    if (!cur || e.num_articles > cur.num_articles) best.set(key, e)
  }
  return [...best.values()]
}

/** Contract says fraction; the live backend currently sends percent (e.g. 162.2) → normalise. */
export function adaptGlobalEvents(
  map: CCountryEventAgg[],
  timeline: CEventTimelinePoint[],
  feed: CGdeltEventPage | null,
  ctx: AdapterCtx,
  region: RegionFilter,
  asOf: string,
): GlobalEventsPayload {
  const maxEvents = Math.max(1, ...map.map((c) => c.events))
  const inRegion = (cc: string) => region === 'ALL' || (region === 'CNHK' ? cc === 'CN' || cc === 'HK' : COUNTRIES[cc]?.region === region)
  return {
    dataStatus: feed?.data_status,
    asOf,
    countries: map.map((c) => ({ country: c.country, countryName: nameOf(c.country), tone: num(c.avg_tone), intensity: c.events / maxEvents, eventCount: c.events, change7d: num(c.change_7d) })),
    timeline: [...timeline].sort((a, b) => a.date.localeCompare(b.date)).map((p) => ({ date: p.date, tone: num(p.avg_tone), eventCount: p.events })),
    events: dedupeEvents(feed?.items ?? [])
      .filter((e) => !e.action_country || inRegion(e.action_country))
      .map((e) => ({
        ...adaptGdeltEvent(e),
        affectedSectors: (e.sector_ids ?? []).map((s) => ctx.sectorName(s)),
        // v1.1+: securities[] gives linkable security ids; older payloads only had issuer ids
        affectedCompanies: e.securities?.length
          ? e.securities.map((x) => ({ id: x.security_id, ticker: x.ticker, name: x.ticker, exposure: NaN }))
          : [],
      }))
      .sort((a, b) => b.date.localeCompare(a.date)),
  }
}

/* ───────────── model lab = runs ⊕ run detail (production runs) ───────────── */

const RUN_STATUS: Record<CModelRun['status'], RunStatus> = { running: 'running', completed: 'success', failed: 'failed', archived: 'skipped' }

export function adaptModelRun(r: CModelRun): ModelRun {
  const dur = r.finished_at ? (Date.parse(r.finished_at) - Date.parse(r.created_at)) / 1000 : NaN
  const nf = r.params?.n_features
  return {
    id: r.model_run_id,
    model: modelKindOf(r.model_type),
    version: `${r.model_type}${r.horizon ? ` · ${r.horizon}` : ''}${r.feature_set_version ? ` · ${r.feature_set_version}` : ''}`,
    status: RUN_STATUS[r.status],
    startedAt: r.created_at,
    durationSec: dur,
    trainStart: r.train_start ?? '',
    trainEnd: r.train_end ?? '',
    testStart: r.oos_start ?? '',
    testEnd: r.oos_end ?? '',
    ic: num(r.ic_mean),
    rankIc: num(r.rank_ic_mean),
    icir: num(r.rank_ic_ir),
    hitRate: num(r.hit_rate),
    decileSpreadAnn: num(r.decile_spread_ann),
    nFeatures: typeof r.n_features === 'number' ? r.n_features : typeof nf === 'number' ? nf : NaN,
    promoted: r.is_production,
    modelType: r.model_type,
    horizon: r.horizon ?? null,
    leakageGate: r.leakage_gate ?? (r.model_type === 'xgboost_rank' ? 'passed' : RANKING_MODELS.has(r.model_type) ? 'failed' : null),
    gateNote: r.gate_note ?? null,
  }
}

const runRef = (d: CModelRunDetail | undefined): ModelLabPayload['xgboostRun'] => {
  if (!d) return null
  const r = adaptModelRun(d.run)
  return { id: r.id, modelType: r.modelType ?? '', horizon: r.horizon ?? null, leakageGate: r.leakageGate ?? null, gateNote: r.gateNote ?? null }
}

/** Ranking models only (insider/macro models live elsewhere in the UI). */
export const RANKING_MODELS = new Set(['xgboost_rank', 'ft_transformer', 'cs_attention', 'ensemble'])

export function adaptModelLab(runs: CModelRun[], details: CModelRunDetail[]): ModelLabPayload {
  const ranking = runs.filter((r) => RANKING_MODELS.has(r.model_type))
  const xgb = details.find((d) => modelKindOf(d.run.model_type) === 'xgboost')
  const tr = details.find((d) => modelKindOf(d.run.model_type) === 'transformer')
  const byDate = (pts: { date: string; value: number | null }[] | undefined) => new Map((pts ?? []).map((p) => [p.date, num(p.value)]))
  const xi = byDate(xgb?.ic_series)
  const xr = byDate(xgb?.rank_ic_series)
  const ti = byDate(tr?.ic_series)
  const trr = byDate(tr?.rank_ic_series)
  const icDates = [...new Set([...xr.keys(), ...trr.keys(), ...xi.keys(), ...ti.keys()])].sort()
  const bench = byDate(xgb?.benchmark_series ?? tr?.benchmark_series)
  const xc = byDate(xgb?.cum_long_short)
  const tc = byDate(tr?.cum_long_short)
  const cumDates = [...new Set([...xc.keys(), ...tc.keys()])].sort()
  const dec = new Set([...(xgb?.decile_returns ?? []), ...(tr?.decile_returns ?? [])].map((d) => d.decile))
  const decOf = (d: CModelRunDetail | undefined, k: number) => num(d?.decile_returns.find((x) => x.decile === k)?.mean_excess_return)
  const fiSrc = xgb ?? tr
  return {
    dataStatus: xgb?.data_status ?? tr?.data_status,
    runs: ranking.map(adaptModelRun),
    icSeries: icDates.map((date) => ({ date, xgboostIc: xi.get(date) ?? NaN, xgboostRankIc: xr.get(date) ?? NaN, transformerIc: ti.get(date) ?? NaN, transformerRankIc: trr.get(date) ?? NaN })),
    deciles: [...dec].sort((a, b) => a - b).map((k) => ({ decile: k, xgboost: decOf(xgb, k), transformer: decOf(tr, k) })),
    cumulativeSpread: cumDates.map((date) => ({ date, xgboost: xc.get(date) ?? NaN, transformer: tc.get(date) ?? NaN, benchmark: bench.get(date) ?? NaN })),
    featureImportance: (fiSrc?.feature_importance ?? [])
      .map((f) => ({ feature: f.feature, family: factorFromContract(f.family) ?? 'quality', familyLabel: f.family ?? undefined, importance: f.importance }))
      .sort((a, b) => b.importance - a.importance)
      .slice(0, 20),
    folds: (xgb?.fold_metrics ?? tr?.fold_metrics ?? []).map((f, i) => ({
      fold: i + 1,
      trainStart: f.train_start ?? '',
      trainEnd: f.train_end ?? '',
      testStart: f.test_start ?? '',
      testEnd: f.test_end ?? f.end_date ?? '',
      rankIc: num(f.rank_ic),
      sharpe: num(f.sharpe),
    })),
    xgboostRun: runRef(xgb),
    transformerRun: runRef(tr),
  }
}

/* ───────────── system & freshness (both from /system/status) ───────────── */

const JOB_STATUS: Record<CJobStatus, RunStatus> = { running: 'running', success: 'success', partial: 'partial', failed: 'failed', skipped: 'skipped' }
const CADENCE = new Set(['daily', 'weekly', 'monthly', 'manual'])
function regionOfFreshness(r: string | null | undefined): SourceFreshness['region'] {
  if (!r) return 'GLOBAL'
  const u = regionFromContract(r)
  return u === 'OTHER' ? 'GLOBAL' : u
}

export function adaptSystemStatus(st: CSystemStatus, runs: CJobRunPage | null, now = new Date()): SystemStatus {
  const cache = statusCacheView(st)
  // coverage v3 A7: a failed data_freshness section serves its last good rows (<= ~10 min): never shown as "fresh"
  const reusedAt = cache?.failedSections.includes('data_freshness') ? (cache.sectionAsOf.data_freshness ?? null) : undefined
  const sources: SourceFreshness[] = st.data_freshness.map((f) => ({
    source: f.table,
    description: `${f.last_date ? `Data through ${f.last_date}` : 'No data yet'}${reusedAt !== undefined ? ` (status from ${reusedAt ?? 'an earlier check'}: refresh failed)` : ''}`,
    region: regionOfFreshness(f.region),
    cadence: (CADENCE.has(f.expected_cadence ?? '') ? f.expected_cadence : 'daily') as SourceFreshness['cadence'],
    dqStatus: f.dq_status ?? null,
    jobId: f.job_id ?? null,
    lastSuccess: f.last_ingested_at ?? '',
    lagHours: typeof f.lag_days === 'number' ? f.lag_days * 24 : NaN,
    status: isOptionalTable(f.table, f.optional)
      ? (f.status === 'fresh' && reusedAt === undefined ? 'fresh' : 'stale') // optional / realtime (live_*): never an error
      : f.status === 'missing' || f.dq_status === 'error' || f.last_status === 'failed' ? 'error' : f.status === 'fresh' && reusedAt !== undefined ? 'stale' : f.status,
    coverage: num(f.row_count),
  }))
  const jobs: JobRun[] = [
    ...st.last_dag_runs.map((d) => ({
      id: d.dag_run_id,
      job: `dag:${d.dag}`,
      source: 'scheduler',
      status: JOB_STATUS[d.status],
      startedAt: d.started_at ?? '',
      durationSec: d.finished_at && d.started_at ? (Date.parse(d.finished_at) - Date.parse(d.started_at)) / 1000 : NaN,
      rowsWritten: NaN,
      message: `${d.jobs_total ?? 0} jobs · ${d.jobs_failed ?? 0} failed${d.status === 'partial' ? ' (partial)' : ''}`,
    })),
    ...(runs?.items ?? []).map((r) => ({
      id: r.run_id,
      job: r.job_id,
      source: r.trigger,
      status: JOB_STATUS[r.status],
      startedAt: r.started_at,
      durationSec: num(r.duration_s),
      rowsWritten: num(r.rows_written),
      message: r.error_message ?? (r.status === 'partial' ? `partial — ${r.entities_failed ?? '?'} entities failed` : 'OK'),
    })),
  ]
  return {
    asOf: st.serving_built_at ?? now.toISOString(),
    jobs,
    sources,
    scheduler: st.scheduler ?? null,
    dataAsOf: (st.data_as_of ?? {}) as Record<string, string>,
    dataAsOfOverall: st.data_as_of_overall ?? null,
    servingSections: st.serving_sections ?? [],
    opsAlerts: st.ops_alerts ?? [],
    statusCache: cache,
  }
}

/** Header badge (v1.28): data as-of (not the rebuild time), optional/realtime tables ignored, scheduler / SLA chip. */
export function adaptFreshness(st: CSystemStatus, now = new Date()): FreshnessSummary {
  const h = headerFreshness(st)
  const lastUpdate = st.serving_built_at ?? now.toISOString()
  return {
    asOf: h.asOf ?? lastUpdate.slice(0, 10),
    lastUpdate,
    status: h.status,
    staleSources: [
      ...((st.data_freshness ?? []).length === 0 ? ['freshness not reported by backend'] : h.staleSources),
      // coverage v3 A7: sections of the status payload that failed to refresh (their last good value, if any)
      ...(h.statusCache?.failedSections.length ? [`status check failed: ${h.statusCache.failedSections.join(', ')}`] : []),
    ],
    regions: h.regions,
    schedulerOn: h.schedulerOn,
    profile: h.profile,
    chip: h.chip,
    chipTone: h.chipTone,
    alerts: h.alerts,
  }
}

/* ───────────── watchlists & alerts ───────────── */

export const watchlistIds = (d: CWatchlistDetail): string[] => d.rows.map((r) => r.security.security_id)

const RULE: Record<AlertMetric, CAlertRuleType> = {
  price: 'price_cross',
  change1dPct: 'price_change_pct_1d',
  compositeScore: 'score_change',
  insiderAnomaly: 'insider_buy',
  sentiment: 'sentiment_shift',
  zoneTouch: 'zone_touch',
  studioDrift: 'studio_drift',
  backtestDrift: 'backtest_drift',
  earningsUpcoming: 'earnings_upcoming',
  earningsSurprise: 'earnings_surprise',
  activist13d: 'activist_13d',
  siSpike: 'short_interest_spike',
  euShort: 'eu_short_position',
  cfOutsideRange: 'price_outside_calibrated_range',
  cfPeerAnomaly: 'conformal_peer_anomaly',
  macroRelease: 'macro_release',
  macroSurprise: 'macro_surprise',
  regimeChange: 'regime_change',
}

const MACRO_METRIC: Partial<Record<CAlertRuleType, AlertMetric>> = { macro_release: 'macroRelease', macro_surprise: 'macroSurprise', regime_change: 'regimeChange' }
export const isMacroMetric = (m: AlertMetric): boolean => m === 'macroRelease' || m === 'macroSurprise' || m === 'regimeChange'

/** UI AlertRule → contract AlertCreate (contracts/schemas/alerts.yaml params + ui_* keys for round-trip). */
export function alertToContract(r: Omit<AlertRule, 'id' | 'createdAt'>): CAlertCreate {
  if (isMacroMetric(r.metric)) return { security_id: null, rule_type: RULE[r.metric], params: { ...(r.macroParams ?? {}), ui_metric: r.metric, ui_ticker: r.ticker }, cooldown_days: 1 }
  const ui = { ui_metric: r.metric, ui_operator: r.operator, ui_threshold: r.threshold, ui_ticker: r.ticker }
  const dir = r.operator === '<' ? 'below' : 'above'
  // price_change_pct_1d / score_change take direction up | down | any (the server rejected above / below: HTTP 400)
  const move = r.operator === '<' ? 'down' : r.operator === '>' ? 'up' : 'any'
  const params: Record<string, unknown> =
    r.metric === 'price'
      ? { level: r.threshold, direction: dir }
      : r.metric === 'change1dPct'
        ? { pct: Math.abs(r.threshold) / 100, direction: move }
        : r.metric === 'compositeScore'
          ? { profile_id: 'balanced', mode: 'level', level: r.threshold, direction: move }
          : r.metric === 'insiderAnomaly'
            ? { min_value_usd: 0, cluster_only: false, min_anomaly: r.threshold }
            : r.metric === 'sentiment'
              ? { threshold_z: r.threshold }
              : r.metric === 'earningsUpcoming'
                ? { days_ahead: Math.max(0, Math.round(r.threshold)) }
                : r.metric === 'earningsSurprise'
                  ? { threshold_z: Math.abs(r.threshold) }
                  : r.metric === 'activist13d'
                    ? { include_amendments: false, include_switch: true }
                    : r.metric === 'siSpike'
                      ? { min_chg_pct: Math.max(0.01, Math.abs(r.threshold) / 100) }
                      : r.metric === 'euShort'
                        ? { min_pct: Math.max(0.1, Math.abs(r.threshold)), include_increases: true }
                        : r.metric === 'cfOutsideRange'
                          ? { level: [80, 90, 95].includes(Math.round(r.threshold)) ? Math.round(r.threshold) : 95, horizon: 1, side: r.operator === '<' ? 'down' : r.operator === '>' ? 'up' : 'any' }
                          : r.metric === 'cfPeerAnomaly'
                            ? { percentile: Math.min(0.999, Math.max(0.5, Math.abs(r.threshold) > 1 ? Math.abs(r.threshold) / 100 : Math.abs(r.threshold))), scope: 'any' }
                            : { zone_rank_max: Math.round(r.threshold) }
  return { security_id: r.companyId, rule_type: RULE[r.metric], params: { ...params, ...ui }, cooldown_days: 5 }
}

/** Threshold in UI units from the server params of any rule type (NaN only when the rule has no threshold at all). */
export function ruleThreshold(ruleType: string, p: Record<string, unknown>): number {
  const n = (k: string): number | null => (typeof p[k] === 'number' && Number.isFinite(p[k] as number) ? (p[k] as number) : null)
  switch (ruleType) {
    case 'price_change_pct_1d':
      return n('pct') !== null ? (n('pct') as number) * 100 : NaN
    case 'score_change':
      return p.mode === 'level' ? (n('level') ?? NaN) : (n('delta') ?? 10)
    case 'short_interest_spike':
      return (n('min_chg_pct') ?? 0.25) * 100
    case 'eu_short_position':
      return n('min_pct') ?? 0.5
    case 'insider_buy':
      return n('min_value_usd') ?? 0
    case 'conformal_peer_anomaly':
      return (n('percentile') ?? 0.95) * 100
    case 'price_outside_calibrated_range':
      return n('level') ?? 95
    case 'earnings_upcoming':
      return n('days_ahead') ?? 3
    case 'earnings_surprise':
    case 'sentiment_shift':
      return n('threshold_z') ?? 2
    case 'zone_touch':
      return n('zone_rank_max') ?? 3
    default:
      return n('level') ?? n('threshold_z') ?? n('days_ahead') ?? n('zone_rank_max') ?? NaN
  }
}

export function alertFromContract(a: CAlert): AlertRule {
  const p = a.params as Record<string, unknown>
  const macro = MACRO_METRIC[a.rule_type]
  if (macro) {
    const params = Object.fromEntries(Object.entries(p).filter(([k]) => !k.startsWith('ui_')))
    const cs = Array.isArray(p.countries) ? (p.countries as string[]) : []
    return {
      id: a.alert_id,
      companyId: '',
      ticker: typeof p.ui_ticker === 'string' ? p.ui_ticker : cs.length ? cs.join(', ') : 'All countries',
      metric: macro,
      operator: '>',
      threshold: typeof p.threshold_z === 'number' ? p.threshold_z : typeof p.level === 'number' ? p.level : typeof p.max_importance === 'number' ? p.max_importance : 0,
      enabled: a.enabled,
      createdAt: a.created_at,
      macroParams: params,
    }
  }
  if (a.rule_type === 'backtest_drift') {
    const name = typeof p.strategy_name === 'string' ? p.strategy_name : 'Backtest strategy'
    return {
      id: a.alert_id,
      companyId: a.security_id ?? '',
      ticker: name,
      metric: 'backtestDrift',
      operator: '>',
      threshold: typeof p.drift_threshold === 'number' ? p.drift_threshold * 100 : 30,
      enabled: a.enabled,
      createdAt: a.created_at,
      studyId: typeof p.strategy_id === 'string' ? p.strategy_id : undefined,
      studyName: name,
    }
  }
  if (a.rule_type === 'studio_drift') {
    const name = typeof p.study_name === 'string' ? p.study_name : 'Model Studio study'
    return {
      id: a.alert_id,
      companyId: a.security_id ?? '',
      ticker: name,
      metric: 'studioDrift',
      operator: '>',
      threshold: typeof p.drift_threshold === 'number' ? p.drift_threshold * 100 : typeof p.threshold === 'number' ? p.threshold * 100 : 20,
      enabled: a.enabled,
      createdAt: a.created_at,
      studyId: typeof p.study_id === 'string' ? p.study_id : undefined,
      studyName: name,
    }
  }
  const metric = (p.ui_metric as AlertMetric) ?? (a.rule_type === 'price_cross' ? 'price' : a.rule_type === 'price_change_pct_1d' ? 'change1dPct' : a.rule_type === 'score_change' ? 'compositeScore' : a.rule_type === 'insider_buy' ? 'insiderAnomaly' : a.rule_type === 'sentiment_shift' ? 'sentiment' : a.rule_type === 'earnings_upcoming' ? 'earningsUpcoming' : a.rule_type === 'earnings_surprise' ? 'earningsSurprise' : a.rule_type === 'activist_13d' ? 'activist13d' : a.rule_type === 'short_interest_spike' ? 'siSpike' : a.rule_type === 'eu_short_position' ? 'euShort' : a.rule_type === 'price_outside_calibrated_range' ? 'cfOutsideRange' : a.rule_type === 'conformal_peer_anomaly' ? 'cfPeerAnomaly' : 'zoneTouch')
  const dirOf = (d: unknown): AlertOperator | null => (d === 'below' || d === 'down' ? '<' : d === 'above' || d === 'up' ? '>' : d === 'any' ? 'crosses' : null)
  const operator = (p.ui_operator as AlertOperator) ?? dirOf(p.direction ?? p.side) ?? '>'
  const threshold = typeof p.ui_threshold === 'number' ? p.ui_threshold : ruleThreshold(a.rule_type, p)
  return {
    id: a.alert_id,
    companyId: a.security_id ?? '',
    // rules created through the API have no ui_ticker: use the server-resolved ticker, never the raw security id
    ticker: (typeof p.ui_ticker === 'string' && p.ui_ticker) || a.ticker || (a.watchlist_id ? (a.watchlist_name ?? 'Watchlist') : null) || a.security_id || '—',
    metric,
    operator,
    threshold,
    enabled: a.enabled,
    createdAt: a.created_at,
    ruleType: a.rule_type,
    params: p,
    name: a.security_name ?? null,
    watchlistId: a.watchlist_id ?? null,
    watchlistName: a.watchlist_name ?? null,
  }
}

/* ───────────── backend health & factor model (/health, /system/status, /meta/factors) ───────────── */

export function adaptHealth(h: CHealth, st: CSystemStatus | null): BackendHealth {
  return {
    status: h.status,
    version: h.version,
    time: h.time,
    servingRunId: h.serving_run_id ?? null,
    servingBuiltAt: h.serving_built_at ?? null,
    keysPresent: st?.keys_present ?? {},
    universeCounts: st?.universe_counts ?? {},
    diskUsageGb: st?.disk_usage_gb ?? null,
  }
}

export function adaptFactorMeta(m: CFactorMeta): FactorModelMeta {
  return {
    compositeValidation: compositeValidationFromContract(m.composite_validation, m.default_profile),
    defaultProfile: m.default_profile,
    profiles: Object.fromEntries(Object.entries(m.profiles).map(([k, w]) => [k, factorMap(w, 0)])),
    factors: m.factors
      .map((f) => {
        const family = factorFromContract(f.id)
        return family ? { family, name: f.name, description: f.description ?? '', memberCount: Object.keys(f.members ?? {}).length } : null
      })
      .filter((x): x is FactorDefinition => x !== null),
  }
}

/** GET /alerts/events row → view (v1.11 `target`: a security id or "macro:<CC>"). */
export function alertEventFromContract(e: CAlertEvent): AlertEventView {
  return { id: e.event_id, alertId: e.alert_id, target: e.target ?? e.security_id, firedAt: e.fired_at, asOfDate: e.as_of_date, message: e.message, read: e.read }
}
