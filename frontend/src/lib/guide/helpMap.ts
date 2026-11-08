/**
 * Route -> guide section map for the "?" help buttons in page headers. Rules are tried in order; the first whose
 * path pattern (and optional ?tab= value) matches wins. Targets are `<chapter file without .md>#<heading anchor>`.
 */

export interface HelpRule {
  /** pathname pattern, anchored at both ends */
  path: RegExp
  /** query parameter `tab` value(s) this rule applies to (absent = any) */
  tab?: string[]
  /** chapter file (no .md) and anchor */
  target: string
}

export const HELP_RULES: HelpRule[] = [
  { path: /^\/help(\/.*)?$/, target: 'README' },
  // macro
  { path: /^\/macro\/?$/, target: '02a-macro#macro-cockpit' },
  { path: /^\/macro\/calendar\/?$/, target: '02a-macro#release-calendar' },
  { path: /^\/macro\/nowcast\/?$/, target: '02a-macro#nowcast' },
  { path: /^\/macro\/theme(\/.*)?$/, target: '02a-macro#themes' },
  { path: /^\/macro\/country(\/.*)?$/, target: '02a-macro#countries' },
  { path: /^\/macro\/series(\/.*)?$/, target: '02a-macro#series-explorer' },
  { path: /^\/macro\/cb\/?$/, target: '02a-macro#central-banks-overview' },
  { path: /^\/macro\/cb\/.+$/, target: '02a-macro#central-bank-page' },
  { path: /^\/macro\/curves\/?$/, target: '02b-markets-cross-asset#curve-monitor' },
  { path: /^\/macro\/credit\/?$/, target: '02b-markets-cross-asset#credit' },
  // markets / cross-asset
  { path: /^\/macro\/markets\/?$/, tab: ['instruments'], target: '02b-markets-cross-asset#instruments-browser' },
  { path: /^\/macro\/markets\/?$/, tab: ['exposures'], target: '02b-markets-cross-asset#exposures' },
  { path: /^\/macro\/markets\/?$/, tab: ['regimes'], target: '02b-markets-cross-asset#regime-performance' },
  { path: /^\/macro\/markets\/?$/, tab: ['scenarios'], target: '02b-markets-cross-asset#scenario-builder' },
  { path: /^\/macro\/markets\/?$/, tab: ['episodes'], target: '02b-markets-cross-asset#episode-replay' },
  { path: /^\/macro\/markets\/?$/, target: '02b-markets-cross-asset#markets-overview' },
  { path: /^\/instruments(\/.*)?$/, target: '02b-markets-cross-asset#instrument-page' },
  { path: /^\/macro\/cross-asset\/?$/, target: '02b-markets-cross-asset#cross-asset-dashboard' },
  { path: /^\/macro\/positioning\/.+$/, target: '02b-markets-cross-asset#cftc-market-page' },
  { path: /^\/macro\/positioning\/?$/, target: '02b-markets-cross-asset#positioning-cftc' },
  // research
  { path: /^\/sectors\/?$/, target: '03-sector-explorer#sector-explorer' },
  { path: /^\/screener\/?$/, target: '04-screener#screener' },
  // company 360 tabs
  { path: /^\/company\/[^/]+\/?$/, tab: ['financials'], target: '05a-company-fundamentals#financials' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['forecast'], target: '05a-company-fundamentals#forecast' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['peers'], target: '05a-company-fundamentals#peers' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['moat'], target: '05a-company-fundamentals#moat' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['debt'], target: '05a-company-fundamentals#debt' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['insiders'], target: '05a-company-fundamentals#insiders' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['ownership'], target: '05a-company-fundamentals#ownership' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['sentiment'], target: '05a-company-fundamentals#news--sentiment' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['technicals'], target: '05b-company-technicals#technicals' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['macro'], target: '05c-company-macro-events-model#macro' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['earnings'], target: '05c-company-macro-events-model#earnings' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['events'], target: '05c-company-macro-events-model#event-risk' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['network'], target: '05c-company-macro-events-model#network--geo' },
  { path: /^\/company\/[^/]+\/?$/, tab: ['model'], target: '05c-company-macro-events-model#model' },
  { path: /^\/company(\/.*)?$/, target: '05a-company-fundamentals#overview' },
  // calendars, flows, events
  { path: /^\/earnings\/season\/?$/, target: '06-earnings-events#earnings-season' },
  { path: /^\/earnings\/?$/, target: '06-earnings-events#earnings--events-calendar' },
  { path: /^\/smart-money\/eu-shorts\/?$/, target: '07-smart-money-insiders#eu--uk-short-register' },
  { path: /^\/smart-money\/activism\/?$/, target: '07-smart-money-insiders#activism-13d' },
  { path: /^\/smart-money\/sectors\/?$/, target: '07-smart-money-insiders#smart-money-sectors' },
  { path: /^\/smart-money\/?$/, target: '07-smart-money-insiders#smart-money-overview' },
  { path: /^\/insiders\/?$/, target: '07-smart-money-insiders#insider-radar' },
  { path: /^\/events\/?$/, tab: ['risk'], target: '08-global-events-news#escalation--gpr' },
  { path: /^\/events\/?$/, tab: ['contagion'], target: '08-global-events-news#contagion--spillovers' },
  { path: /^\/events\/?$/, tab: ['studies'], target: '08-global-events-news#event-studies' },
  { path: /^\/events\/?$/, target: '08-global-events-news#global-events' },
  { path: /^\/news(\/.*)?$/, target: '08-global-events-news#news-terminal' },
  // tools
  { path: /^\/studio\/?$/, target: '09-model-studio#model-studio' },
  { path: /^\/models\/?$/, target: '10-model-lab#model-lab' },
  { path: /^\/backtest\/?$/, target: '11-backtest-lab#backtest-lab' },
  { path: /^\/bql\/?$/, target: '14-bql-mnemonics#bql-console' },
  { path: /^\/theses\/new\/?$/, target: '12-thesis-tracker#thesis-editor' },
  { path: /^\/theses\/[^/]+\/edit\/?$/, target: '12-thesis-tracker#thesis-editor' },
  { path: /^\/theses\/[^/]+\/?$/, target: '12-thesis-tracker#thesis-page' },
  { path: /^\/theses\/?$/, target: '12-thesis-tracker#thesis-board' },
  { path: /^\/watchlist\/?$/, target: '13-watchlists-alerts#watchlist--alerts' },
  // system
  { path: /^\/system\/data-health\/?$/, target: '15-system#data-health' },
  { path: /^\/system\/conformal\/?$/, target: '15-system#conformal-validation' },
  { path: /^\/system\/?$/, target: '15-system#system-overview' },
]

/** `/help/<chapter>#<anchor>` for a pathname + search string, or the guide index when nothing matches. */
export function helpHref(pathname: string, search = ''): string {
  const tab = new URLSearchParams(search).get('tab')
  for (const r of HELP_RULES) {
    if (!r.path.test(pathname)) continue
    if (r.tab && (!tab || !r.tab.includes(tab))) continue
    return targetHref(r.target)
  }
  return '/help'
}

export function targetHref(target: string): string {
  const [chapter, anchor] = target.split('#')
  if (!chapter || chapter === 'README') return anchor ? `/help#${anchor}` : '/help'
  return `/help/${chapter}${anchor ? `#${anchor}` : ''}`
}

/** In-app route of a guide link target (chapter null = the current chapter). */
export function guidePath(chapter: string | null, anchor: string | null, current: string): string {
  const c = chapter ?? current
  const base = !c || c.toLowerCase() === 'readme' ? '/help' : `/help/${c}`
  return anchor ? `${base}#${anchor}` : base
}
