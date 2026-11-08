import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'
import type { AlertRule, FactorFamily, FactorWeights, RegionFilter } from '@/api/types'
import { DEFAULT_WEIGHTS, FACTOR_FAMILIES, LEGACY_DEFAULT_WEIGHTS } from '@/lib/factors'
import { registerWeightsProvider } from '@/api/client'
import * as remote from '@/api/remote'
import { toast } from '@/components/ui/toast'
import { setFormatLocale } from '@/lib/format'
import { safeStorage, uid } from '@/lib/utils'

/** localStorage wrapper that never throws (private windows, blocked storage). */
const storage = createJSONStorage(
  (): StateStorage => ({
    getItem: (k) => safeStorage.get(k),
    setItem: (k, v) => safeStorage.set(k, v),
    removeItem: (k) => safeStorage.remove(k),
  }),
)

/* ── UI preferences ── */

export type Theme = 'dark' | 'light'

interface UiState {
  theme: Theme
  region: RegionFilter
  navCollapsed: boolean
  locale: string
  paletteOpen: boolean
  setTheme: (t: Theme) => void
  toggleTheme: () => void
  setRegion: (r: RegionFilter) => void
  toggleNav: () => void
  setLocale: (l: string) => void
  setPaletteOpen: (o: boolean) => void
}

/** Apply the theme attribute synchronously so canvas charts read the new CSS tokens on the next render. */
function applyThemeAttr(theme: Theme): void {
  if (typeof document !== 'undefined') document.documentElement.setAttribute('data-theme', theme)
}

export const useUi = create<UiState>()(
  persist(
    (set, get) => ({
      theme: 'dark',
      region: 'ALL',
      navCollapsed: false,
      locale: 'en-US',
      paletteOpen: false,
      setTheme: (theme) => {
        applyThemeAttr(theme)
        set({ theme })
      },
      toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),
      setRegion: (region) => set({ region }),
      toggleNav: () => set({ navCollapsed: !get().navCollapsed }),
      setLocale: (locale) => {
        setFormatLocale(locale)
        set({ locale })
      },
      setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
    }),
    {
      name: 'big360.ui',
      storage,
      partialize: (s) => ({ theme: s.theme, region: s.region, navCollapsed: s.navCollapsed, locale: s.locale }),
      onRehydrateStorage: () => (s) => {
        if (s?.locale) setFormatLocale(s.locale)
        if (s?.theme) applyThemeAttr(s.theme)
      },
    },
  ),
)

/* ── factor weights (screener re-ranking) ── */

interface WeightsState {
  weights: FactorWeights
  setWeight: (f: FactorFamily, w: number) => void
  reset: () => void
}

export const useWeights = create<WeightsState>()(
  persist(
    (set) => ({
      weights: { ...DEFAULT_WEIGHTS },
      setWeight: (f, w) => set((s) => ({ weights: { ...s.weights, [f]: w } })),
      reset: () => set({ weights: { ...DEFAULT_WEIGHTS } }),
    }),
    {
      name: 'big360.weights',
      storage,
      version: 1,
      // v1: the defaults became the balanced profile; untouched old defaults move with them, custom weights stay
      migrate: (persisted, version) => {
        const st = (persisted ?? {}) as { weights?: FactorWeights }
        const w = st.weights
        if (version < 1 && w && FACTOR_FAMILIES.every((f) => w[f] === LEGACY_DEFAULT_WEIGHTS[f])) return { ...st, weights: { ...DEFAULT_WEIGHTS } } as WeightsState
        return st as WeightsState
      },
    },
  ),
)

/* ── watchlist & alert rules ── */

interface WatchlistState {
  ids: string[]
  rules: AlertRule[]
  /** security id -> ticker / name (watchlist detail rows, rule targets; display only, not persisted) */
  names: Record<string, { ticker: string; name: string }>
  toggle: (id: string) => void
  add: (id: string) => void
  remove: (id: string) => void
  has: (id: string) => boolean
  addRule: (r: Omit<AlertRule, 'id' | 'createdAt'>) => void
  updateRule: (id: string, patch: Partial<AlertRule>) => void
  removeRule: (id: string) => void
}

export const useWatchlist = create<WatchlistState>()(
  persist(
    (set, get) => ({
      ids: ['MSFT', 'ASML.AS', '7203.T', 'EDP.LS', '005930.KS'],
      rules: [],
      names: {},
      toggle: (id) => (get().ids.includes(id) ? get().remove(id) : get().add(id)),
      add: (id) => {
        set((s) => ({ ids: s.ids.includes(id) ? s.ids : [...s.ids, id] }))
        void remote.addWatchItem(id)
      },
      remove: (id) => {
        set((s) => ({ ids: s.ids.filter((x) => x !== id) }))
        void remote.removeWatchItem(id)
      },
      has: (id) => get().ids.includes(id),
      addRule: (r) => {
        const local: AlertRule = { ...r, id: uid('rule'), createdAt: new Date().toISOString() }
        set((s) => ({ rules: [...s.rules, local] }))
        // mirror to the backend; swap in the server id when it accepts the rule. A rejected rule is removed again
        // and the server's reason shown (it used to stay as a local ghost and vanish on reload)
        void remote.createAlert(r).then(
          (srv) => {
            if (srv) set((s) => ({ rules: s.rules.map((x) => (x.id === local.id ? srv : x)) }))
          },
          (e: unknown) => {
            set((s) => ({ rules: s.rules.filter((x) => x.id !== local.id) }))
            toast({ tone: 'error', title: `Alert rule not saved (${r.ticker})`, message: e instanceof Error ? e.message : String(e) })
          },
        )
      },
      updateRule: (id, patch) => {
        set((s) => ({ rules: s.rules.map((r) => (r.id === id ? { ...r, ...patch } : r)) }))
        void remote.updateAlert(id, patch).catch((e: unknown) => toast({ tone: 'error', title: 'Alert rule not updated', message: e instanceof Error ? e.message : String(e) }))
      },
      removeRule: (id) => {
        set((s) => ({ rules: s.rules.filter((r) => r.id !== id) }))
        void remote.deleteAlert(id).catch((e: unknown) => toast({ tone: 'error', title: 'Alert rule not deleted', message: e instanceof Error ? e.message : String(e) }))
      },
    }),
    { name: 'big360.watchlist', storage, partialize: (s) => ({ ids: s.ids, rules: s.rules }) },
  ),
)

/* ── backend wiring ── */

// the screener request body carries the user's current factor weights
registerWeightsProvider(() => useWeights.getState().weights)

/** Pull watchlist + alert rules from the API once (no-op in mock mode or when the endpoints return 501). */
async function syncFromServer(): Promise<void> {
  const ids = await remote.syncWatchlist(useWatchlist.getState().ids)
  if (ids) useWatchlist.setState({ ids, names: { ...useWatchlist.getState().names, ...remote.watchlistNames } })
  const rules = await remote.loadAlerts()
  if (rules) {
    const names = { ...useWatchlist.getState().names }
    for (const r of rules) if (r.companyId && r.name && r.ticker && !/^S[0-9A-Z]{6,}$/.test(r.ticker)) names[r.companyId] = { ticker: r.ticker, name: r.name }
    useWatchlist.setState({ rules, names })
  }
}
if (typeof window !== 'undefined') void syncFromServer()
