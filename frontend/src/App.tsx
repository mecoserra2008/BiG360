import { lazy, Suspense } from 'react'
import { Navigate, RouterProvider, createBrowserRouter } from 'react-router'
import { AppShell } from './components/layout/AppShell'
import { NotFound } from './pages/NotFound'

const MacroMarkets = lazy(() => import('./pages/macro/Markets'))
const MacroCentralBanks = lazy(() => import('./pages/macro/CentralBanks'))
const MacroBank = lazy(() => import('./pages/macro/Bank'))
const CurveMonitor = lazy(() => import('./pages/rates/CurveMonitor'))
const CreditPage = lazy(() => import('./pages/rates/Credit'))
const InstrumentPage = lazy(() => import('./pages/instruments/InstrumentPage'))
const Positioning = lazy(() => import('./pages/positioning/Positioning'))
const CrossAsset = lazy(() => import('./pages/crossasset/CrossAsset'))
const PositioningMarket = lazy(() => import('./pages/positioning/PositioningMarket'))
const MacroCockpit = lazy(() => import('./pages/macro/Cockpit'))
const MacroCountry = lazy(() => import('./pages/macro/Country'))
const MacroTheme = lazy(() => import('./pages/macro/Theme'))
const MacroCalendar = lazy(() => import('./pages/macro/Calendar'))
const MacroNowcast = lazy(() => import('./pages/macro/Nowcast'))
const MacroSeries = lazy(() => import('./pages/macro/Series'))
const SectorExplorer = lazy(() => import('./pages/sector/SectorExplorer'))
const Screener = lazy(() => import('./pages/screener/Screener'))
const Company360 = lazy(() => import('./pages/company/Company360'))
const InsiderRadar = lazy(() => import('./pages/insiders/InsiderRadar'))
const GlobalEvents = lazy(() => import('./pages/events/GlobalEvents'))
const NewsTerminal = lazy(() => import('./pages/news/NewsTerminal'))
const ModelLab = lazy(() => import('./pages/models/ModelLab'))
const Studio = lazy(() => import('./pages/studio/Studio'))
const BacktestLab = lazy(() => import('./pages/backtest/BacktestLab'))
const BqlConsole = lazy(() => import('./pages/bql/BqlConsole'))
const Watchlist = lazy(() => import('./pages/watchlist/Watchlist'))
const SystemPage = lazy(() => import('./pages/system/SystemPage'))
const DataHealthPage = lazy(() => import('./pages/system/DataHealthPage'))
const ConformalValidation = lazy(() => import('./pages/system/ConformalValidation'))
const EarningsCalendar = lazy(() => import('./pages/earnings/Calendar'))
const EarningsSeason = lazy(() => import('./pages/earnings/Season'))
const SmartMoney = lazy(() => import('./pages/smartmoney/SmartMoney'))
const SmartMoneyEuShorts = lazy(() => import('./pages/smartmoney/EuShorts'))
const SmartMoneyActivism = lazy(() => import('./pages/smartmoney/Activism'))
const SmartMoneySectors = lazy(() => import('./pages/smartmoney/Sectors'))
const ThesisBoard = lazy(() => import('./pages/theses/Board'))
const ThesisPage = lazy(() => import('./pages/theses/ThesisPage'))
const ThesisNew = lazy(() => import('./pages/theses/Editor').then((m) => ({ default: m.ThesisNew })))
const HelpPage = lazy(() => import('./pages/help/HelpPage'))
const HelpPrint = lazy(() => import('./pages/help/HelpPrint'))
const ThesisEdit = lazy(() => import('./pages/theses/Editor').then((m) => ({ default: m.ThesisEdit })))

const router = createBrowserRouter([
  // the whole user guide on one page, outside the shell (print / PDF export)
  {
    path: '/help/print',
    element: (
      <Suspense fallback={null}>
        <HelpPrint />
      </Suspense>
    ),
    errorElement: <NotFound crashed />,
  },
  {
    path: '/',
    element: <AppShell />,
    errorElement: <NotFound crashed />,
    children: [
      { index: true, element: <Navigate to="/macro" replace /> },
      { path: 'market', element: <Navigate to="/macro/markets" replace /> },
      { path: 'macro', element: <MacroCockpit /> },
      { path: 'macro/markets', element: <MacroMarkets /> },
      { path: 'macro/markets/instruments', element: <Navigate to="/macro/markets?tab=instruments" replace /> },
      { path: 'instruments/:id', element: <InstrumentPage /> },
      { path: 'macro/cross-asset', element: <CrossAsset /> },
      { path: 'macro/positioning', element: <Positioning /> },
      { path: 'macro/positioning/:code', element: <PositioningMarket /> },
      { path: 'instruments', element: <Navigate to="/macro/markets?tab=instruments" replace /> },
      { path: 'macro/cb', element: <MacroCentralBanks /> },
      { path: 'macro/cb/:bank', element: <MacroBank /> },
      { path: 'macro/curves', element: <CurveMonitor /> },
      { path: 'macro/credit', element: <CreditPage /> },
      { path: 'macro/country/:cc', element: <MacroCountry /> },
      { path: 'macro/country', element: <Navigate to="/macro/country/US" replace /> },
      { path: 'macro/theme/:theme', element: <MacroTheme /> },
      { path: 'macro/theme', element: <Navigate to="/macro/theme/growth" replace /> },
      { path: 'macro/calendar', element: <MacroCalendar /> },
      { path: 'macro/nowcast', element: <MacroNowcast /> },
      { path: 'macro/series', element: <MacroSeries /> },
      { path: 'macro/series/:id', element: <MacroSeries /> },
      { path: 'sectors', element: <SectorExplorer /> },
      { path: 'screener', element: <Screener /> },
      { path: 'company/:id', element: <Company360 /> },
      { path: 'company', element: <Navigate to="/company/MSFT" replace /> },
      { path: 'earnings', element: <EarningsCalendar /> },
      { path: 'earnings/season', element: <EarningsSeason /> },
      { path: 'smart-money', element: <SmartMoney /> },
      { path: 'smart-money/eu-shorts', element: <SmartMoneyEuShorts /> },
      { path: 'smart-money/activism', element: <SmartMoneyActivism /> },
      { path: 'smart-money/sectors', element: <SmartMoneySectors /> },
      { path: 'insiders', element: <InsiderRadar /> },
      { path: 'events', element: <GlobalEvents /> },
      { path: 'news', element: <NewsTerminal /> },
      { path: 'news/top', element: <NewsTerminal /> },
      { path: 'studio', element: <Studio /> },
      { path: 'backtest', element: <BacktestLab /> },
      { path: 'bql', element: <BqlConsole /> },
      { path: 'theses', element: <ThesisBoard /> },
      { path: 'theses/new', element: <ThesisNew /> },
      { path: 'theses/:id', element: <ThesisPage /> },
      { path: 'theses/:id/edit', element: <ThesisEdit /> },
      { path: 'models', element: <ModelLab /> },
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'system', element: <SystemPage /> },
      { path: 'system/data-health', element: <DataHealthPage /> },
      { path: 'system/conformal', element: <ConformalValidation /> },
      { path: 'conformal', element: <Navigate to="/system/conformal" replace /> },
      { path: 'help', element: <HelpPage /> },
      { path: 'help/:chapter', element: <HelpPage /> },
      { path: '*', element: <NotFound /> },
    ],
  },
])

export function App() {
  return <RouterProvider router={router} />
}
