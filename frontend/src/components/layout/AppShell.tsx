import { Suspense, useState } from 'react'
import { Outlet, useLocation } from 'react-router'
import { useUi } from '@/store'
import { RunWatcher } from '@/pages/studio/RunWatcher'
import { LiveBridge, LiveTicker } from '../live/LiveTicker'
import { SkeletonRows } from '../ui/feedback'
import { Toaster } from '../ui/toast'
import { CommandPalette } from './CommandPalette'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'

export function AppShell() {
  const { pathname } = useLocation()
  const [lastCompany, setLastCompany] = useState<string | null>(null)
  const locale = useUi((s) => s.locale)

  // remember the last Company 360 visited (derived during render — no effect needed)
  const m = pathname.match(/^\/company\/([^/]+)/)
  const current = m ? decodeURIComponent(m[1]) : null
  if (current && current !== lastCompany) setLastCompany(current)

  return (
    <div className="flex h-full min-w-[1024px] bg-bg text-fg">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-accent focus:px-3 focus:py-1 focus:text-accent-fg">
        Skip to content
      </a>
      <Sidebar lastCompanyId={lastCompany} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <LiveTicker />
        <main id="main" className="min-h-0 flex-1 overflow-y-auto">
          {/* keyed on locale so every formatted figure re-renders when the number locale changes */}
          <div key={locale} className="mx-auto max-w-[1680px] p-3 lg:p-4">
            <Suspense fallback={<SkeletonRows rows={12} />}>
              <Outlet />
            </Suspense>
          </div>
        </main>
      </div>
      <CommandPalette />
      <Toaster />
      <RunWatcher />
      <LiveBridge />
    </div>
  )
}
