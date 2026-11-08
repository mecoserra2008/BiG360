import { CircleCheck, Database, Moon, RefreshCw, Search, Sun, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { API_BASE_URL, MOCK_FALLBACK_ENABLED, USE_MOCK, subscribeFallbacks } from '@/api/client'
import type { RegionFilter } from '@/api/types'
import { useFreshness } from '@/api/hooks'
import { sortedRegions } from '@/api/ops/adapters'
import { useSyncStatus } from '@/api/ops/hooks'
import { reasonCounts, REASON_LABEL } from '@/api/ops/sync'
import { fmtDate, fmtDateTime } from '@/lib/format'
import { REGIONS } from '@/lib/regions'
import { cn } from '@/lib/utils'
import { useUi } from '@/store'
import { Kbd } from '../ui/data'
import { Select } from '../ui/inputs'
import { Tip } from '../ui/tooltip'

/** Endpoints currently answered by the mock because the backend returned 501 / was unreachable. */
function useFallbacks(): string[] {
  const [list, setList] = useState<string[]>([])
  useEffect(() => (USE_MOCK ? undefined : subscribeFallbacks((s) => setList([...s]))), [])
  return list
}

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/** Startup sync chip (v1.41 GET /system/sync): "Syncing data... 12/48" while it runs, then "Data up to date as of ..."
 * or "3 skipped" linking to Data health > Data sync. Hidden on backends without the endpoint and before any sync. */
function SyncChip() {
  const q = useSyncStatus(!USE_MOCK)
  const st = q.data
  const c = st?.chip
  if (!st || !c?.text) return null
  const tone = c.tone === 'down' ? 'border-down/40 bg-down-soft text-down' : c.tone === 'warn' ? 'border-warn/40 bg-warn-soft text-warn' : c.tone === 'ok' ? 'border-up/40 bg-up-soft text-up' : 'border-info/40 bg-info-soft text-info'
  const Icon = c.running ? RefreshCw : c.tone === 'ok' ? CircleCheck : TriangleAlert
  const pr = (st.progress ?? {}) as { due?: number; fetched?: number; skipped?: number; datasets_checked?: number }
  return (
    <Tip
      content={
        <div className="max-w-80 space-y-1">
          <div className="label-caps">Data sync{st.trigger ? ` · ${st.trigger}` : ''}</div>
          <div>
            {pr.datasets_checked ?? 0} datasets checked · {pr.due ?? 0} due · {pr.fetched ?? 0} fetched · {pr.skipped ?? 0} skipped
          </div>
          {reasonCounts(st).filter(([r]) => r !== 'fresh').map(([r, n]) => (
            <div key={r} className="text-muted">{REASON_LABEL[r]}: {n}</div>
          ))}
          {st.finished_at && !c.running ? <div className="text-muted">Finished {fmtDateTime(st.finished_at)}</div> : null}
          <div className="text-muted">Details: System › Data health › Data sync</div>
        </div>
      }
    >
      <Link
        to={c.link ?? '/system/data-health#sync'}
        className={cn('inline-flex h-5 items-center gap-1 rounded-sm border px-1.5 text-2xs font-bold tracking-wider uppercase', tone)}
        aria-label={`Data sync: ${c.text}`}
        aria-live="polite"
      >
        <Icon className={cn('size-3', c.running && 'animate-spin')} aria-hidden /> {c.text}
      </Link>
    </Tip>
  )
}

export function TopBar() {
  const region = useUi((s) => s.region)
  const setRegion = useUi((s) => s.setRegion)
  const theme = useUi((s) => s.theme)
  const toggleTheme = useUi((s) => s.toggleTheme)
  const openPalette = useUi((s) => s.setPaletteOpen)
  const fresh = useFreshness()
  const fallbacks = useFallbacks()

  const status = fresh.data?.status
  const dot = status === 'fresh' ? 'bg-up' : status === 'stale' ? 'bg-warn' : status === 'error' ? 'bg-down' : 'bg-faint'

  return (
    <header className="flex h-11 shrink-0 items-center gap-3 border-b border-border bg-surface px-3">
      <button
        type="button"
        onClick={() => openPalette(true)}
        className="flex h-7 w-[300px] max-w-[34vw] min-w-[160px] items-center gap-2 rounded-md border border-border bg-surface-2 px-2 text-sm text-faint hover:border-border-strong focus-visible:outline-2 focus-visible:outline-focus"
        aria-label="Search securities (ticker, company, ISIN)"
        aria-keyshortcuts={isMac ? 'Meta+K' : 'Control+K'}
      >
        <Search className="size-3.5 shrink-0" aria-hidden />
        <span className="truncate">Ticker, company or ISIN…</span>
        <span className="ml-auto flex gap-0.5">
          <Kbd>{isMac ? '⌘' : 'Ctrl'}</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>

      <div role="group" aria-label="Region filter" className="hidden min-w-0 items-center gap-0.5 min-[1180px]:flex">
        {REGIONS.map((r) => (
          <button
            key={r.code}
            type="button"
            title={r.title}
            aria-pressed={region === r.code}
            onClick={() => setRegion(r.code)}
            className={cn(
              'h-6 shrink-0 rounded-sm border px-2 text-xs font-semibold tracking-wide transition-colors focus-visible:outline-2 focus-visible:outline-focus',
              region === r.code ? 'border-accent bg-accent-soft text-accent-text' : 'border-transparent text-muted hover:bg-surface-2 hover:text-fg',
            )}
          >
            {r.label}
          </button>
        ))}
      </div>

      {/* below 1180px the chips collapse into a dropdown */}
      <Select
        label="Region filter"
        value={region}
        onChange={(v) => setRegion(v as RegionFilter)}
        options={REGIONS.map((r) => ({ value: r.code, label: r.code === 'ALL' ? 'All regions' : r.title }))}
        className="w-36 min-[1180px]:hidden"
      />

      <div className="ml-auto flex shrink-0 items-center gap-3">
        {!USE_MOCK && fallbacks.length > 0 && (
          <Tip content={<div className="space-y-0.5"><div>Backend returned 501 / was unreachable; served from mock (VITE_MOCK_FALLBACK):</div><div className="num text-muted">{fallbacks.join(', ')}</div></div>}>
            <span tabIndex={0} className="inline-flex h-5 items-center gap-1 rounded-sm border border-warn/40 bg-warn-soft px-1.5 text-2xs font-bold tracking-wider text-warn uppercase">
              <Database className="size-3" aria-hidden /> Mock fallback · {fallbacks.length}
            </span>
          </Tip>
        )}
        {!USE_MOCK && fallbacks.length === 0 && (
          <span className="hidden h-5 items-center rounded-sm border border-up/40 bg-up-soft px-1.5 text-2xs font-bold tracking-wider text-up uppercase xl:inline-flex" title={`Live API ${API_BASE_URL}${MOCK_FALLBACK_ENABLED ? ' · mock fallback armed' : ''}`}>
            Live API
          </span>
        )}
        {USE_MOCK && (
          <Tip content="Mock mode: deterministic synthetic data (VITE_USE_MOCK=true). Figures are not real.">
            <span tabIndex={0} className="inline-flex h-5 items-center gap-1 rounded-sm border border-warn/40 bg-warn-soft px-1.5 text-2xs font-bold tracking-wider text-warn uppercase">
              <Database className="size-3" aria-hidden /> Mock data
            </span>
          </Tip>
        )}
        <SyncChip />
        {fresh.data?.chip && (
          <Tip
            content={
              <div className="max-w-80 space-y-1">
                {(fresh.data.alerts ?? []).filter((a) => a.severity !== 'info').map((a) => (
                  <div key={a.id}>
                    <span className={a.severity === 'error' ? 'text-down' : 'text-warn'}>{a.title}</span>
                    {a.detail ? <span className="text-muted"> — {a.detail}</span> : null}
                  </div>
                ))}
                <div className="text-muted">Details: System › Data health</div>
              </div>
            }
          >
            <Link
              to={fresh.data.chip === 'Data stale' ? '/system/data-health' : '/system'}
              className={cn(
                'inline-flex h-5 items-center gap-1 rounded-sm border px-1.5 text-2xs font-bold tracking-wider uppercase',
                fresh.data.chipTone === 'down' ? 'border-down/40 bg-down-soft text-down' : 'border-warn/40 bg-warn-soft text-warn',
              )}
              aria-label={`Data operations warning: ${fresh.data.chip}`}
            >
              <TriangleAlert className="size-3" aria-hidden /> {fresh.data.chip}
            </Link>
          </Tip>
        )}
        <Tip
          content={
            fresh.data ? (
              <div className="space-y-0.5">
                <div className="label-caps">Data as of (per region)</div>
                {sortedRegions(fresh.data.regions ?? {}).length > 0 ? (
                  <div className="grid grid-cols-[auto_auto] gap-x-3">
                    {sortedRegions(fresh.data.regions ?? {}).map(([r, d]) => (
                      <span key={r} className="contents">
                        <span className="text-muted">{r}</span>
                        <span className="num">{fmtDate(d)}</span>
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="num">{fmtDate(fresh.data.asOf)}</div>
                )}
              </div>
            ) : (
              'Data as-of date'
            )
          }
        >
          <span tabIndex={0} className="hidden items-baseline gap-1.5 text-xs lg:flex" aria-label={`Data as of ${fresh.data ? fmtDate(fresh.data.asOf) : 'unknown'}`}>
            <span className="label-caps">Data as of</span>
            <span className="num text-fg">{fresh.data ? fmtDate(fresh.data.asOf) : '—'}</span>
          </span>
        </Tip>
        <Tip
          content={
            fresh.data ? (
              <div className="space-y-0.5">
                <div>Snapshot rebuilt {fmtDateTime(fresh.data.lastUpdate)}</div>
                {fresh.data.schedulerOn !== undefined && fresh.data.schedulerOn !== null && (
                  <div className={fresh.data.schedulerOn ? 'text-up' : 'text-down'}>
                    Scheduler {fresh.data.schedulerOn ? 'on' : 'off'}{fresh.data.profile ? ` · profile ${fresh.data.profile}` : ''}
                  </div>
                )}
                {fresh.data.staleSources.length > 0 ? <div className="text-warn">Stale / failing: {fresh.data.staleSources.slice(0, 12).join(', ')}{fresh.data.staleSources.length > 12 ? ` +${fresh.data.staleSources.length - 12}` : ''}</div> : <div className="text-up">All sources fresh</div>}
                {!USE_MOCK && <div className="text-muted">API {API_BASE_URL}</div>}
              </div>
            ) : fresh.isError ? (
              'Freshness endpoint unreachable'
            ) : (
              'Checking data freshness…'
            )
          }
        >
          <span tabIndex={0} className="inline-flex items-center gap-1.5 text-xs text-muted" aria-label={`Data freshness: ${status ?? 'unknown'}`}>
            <span className={cn('size-2 rounded-full', fresh.isError ? 'bg-down' : dot)} aria-hidden />
            <span className="hidden capitalize xl:inline">{fresh.isError ? 'offline' : (status ?? '…')}</span>
          </span>
        </Tip>
        <button
          type="button"
          onClick={toggleTheme}
          aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus"
        >
          {theme === 'dark' ? <Sun className="size-4" aria-hidden /> : <Moon className="size-4" aria-hidden />}
        </button>
      </div>
    </header>
  )
}
