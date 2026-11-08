import { BookOpen, Building2, CalendarDays, Code2, FlaskConical, Gauge, Globe2, History, Landmark, Layers, ListFilter, Newspaper, NotebookPen, Radar, Server, Sigma, Star, type LucideIcon } from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  description: string
  /** matches when the pathname starts with this prefix */
  match: string
  /** sub-pages shown under the item while its section is active */
  children?: { to: string; label: string; match: string; exact?: boolean }[]
}

export const NAV: NavItem[] = [
  {
    to: '/macro',
    match: '/macro',
    label: 'Macro',
    icon: Gauge,
    description: 'Macro cockpit, countries, themes, release calendar, nowcasts, central banks, yield curves, credit, markets, cross-asset signals and CFTC positioning',
    children: [
      { to: '/macro', match: '/macro', label: 'Cockpit', exact: true },
      { to: '/macro/calendar', match: '/macro/calendar', label: 'Calendar' },
      { to: '/macro/nowcast', match: '/macro/nowcast', label: 'Nowcast' },
      { to: '/macro/theme/growth', match: '/macro/theme', label: 'Themes' },
      { to: '/macro/country/US', match: '/macro/country', label: 'Countries' },
      { to: '/macro/series', match: '/macro/series', label: 'Series explorer' },
      { to: '/macro/cb', match: '/macro/cb', label: 'Central banks' },
      { to: '/macro/curves', match: '/macro/curves', label: 'Curve monitor' },
      { to: '/macro/credit', match: '/macro/credit', label: 'Credit' },
      { to: '/macro/markets', match: '/macro/markets', label: 'Markets' },
      { to: '/macro/markets?tab=instruments', match: '/instruments', label: 'Instruments' },
      { to: '/macro/cross-asset', match: '/macro/cross-asset', label: 'Cross-asset' },
      { to: '/macro/positioning', match: '/macro/positioning', label: 'Positioning (CFTC)' },
    ],
  },
  { to: '/sectors', match: '/sectors', label: 'Sector Explorer', icon: Layers, description: 'Sector → subsector KPI distributions' },
  { to: '/screener', match: '/screener', label: 'Screener / Ranking', icon: ListFilter, description: 'Composite & factor ranking, live re-weighting' },
  { to: '/company/MSFT', match: '/company', label: 'Company 360', icon: Building2, description: 'Flagship single-name analysis' },
  {
    to: '/earnings',
    match: '/earnings',
    label: 'Earnings & Events',
    icon: CalendarDays,
    description: 'Earnings & corporate-events calendar, reactions and the earnings season (model-based expectations, not consensus)',
    children: [
      { to: '/earnings', match: '/earnings', label: 'Calendar', exact: true },
      { to: '/earnings/season', match: '/earnings/season', label: 'Earnings season' },
    ],
  },
  {
    to: '/smart-money',
    match: '/smart-money',
    label: 'Smart Money',
    icon: Landmark,
    description: 'Who is buying, selling, shorting and crowding: 13F ownership and flows, short interest, 13D activism, EU / UK short-seller register',
    children: [
      { to: '/smart-money', match: '/smart-money', label: 'Overview', exact: true },
      { to: '/smart-money/eu-shorts', match: '/smart-money/eu-shorts', label: 'EU / UK short register' },
      { to: '/smart-money/activism', match: '/smart-money/activism', label: 'Activism (13D)' },
      { to: '/smart-money/sectors', match: '/smart-money/sectors', label: 'Sectors' },
    ],
  },
  { to: '/insiders', match: '/insiders', label: 'Insider Radar', icon: Radar, description: 'Anomalous insider activity across markets' },
  { to: '/events', match: '/events', label: 'Global Events', icon: Globe2, description: 'GDELT tone & intensity by country' },
  {
    to: '/news',
    match: '/news',
    label: 'News Terminal',
    icon: Newspaper,
    description: 'Live headlines (official, regulators, exchanges, publishers, Polygon, GDELT 15-min) grouped into stories, keyboard-first',
    children: [
      { to: '/news', match: '/news', label: 'Live', exact: true },
      { to: '/news/top', match: '/news/top', label: 'Top stories' },
    ],
  },
  { to: '/studio', match: '/studio', label: 'Model Studio', icon: Sigma, description: 'Econometrics & ML workbench: regressions, ARIMA, clustering, PCA' },
  { to: '/backtest', match: '/backtest', label: 'Backtest Lab', icon: History, description: 'Point-in-time strategy backtests with costs, attribution and overfitting controls (deflated Sharpe, PBO)' },
  { to: '/bql', match: '/bql', label: 'BQL console', icon: Code2, description: 'BQL-lite queries: get / for / with over the serving snapshot, point-in-time, exports, saved screens' },
  { to: '/theses', match: '/theses', label: 'Thesis Tracker', icon: NotebookPen, description: 'Decision log: theses with linked alerts, automatic status, performance vs benchmark, reviews and hit rates' },
  { to: '/models', match: '/models', label: 'Model Lab', icon: FlaskConical, description: 'Nightly ML monitor: runs, IC, decile spreads, folds' },
  { to: '/watchlist', match: '/watchlist', label: 'Watchlist & Alerts', icon: Star, description: 'Tracked names and alert rules' },
  {
    to: '/system',
    match: '/system',
    label: 'System',
    icon: Server,
    description: 'Jobs, scheduler and schedule profile, data freshness and the Data Health supervisor (gaps, backfills, sources)',
    children: [
      { to: '/system', match: '/system', label: 'Overview', exact: true },
      { to: '/system/data-health', match: '/system/data-health', label: 'Data health' },
      { to: '/system/conformal', match: '/system/conformal', label: 'Conformal validation' },
    ],
  },
  { to: '/help', match: '/help', label: 'Guide', icon: BookOpen, description: 'User guide: every page, its methodology, validation status and correct usage (HELP)' },
]
