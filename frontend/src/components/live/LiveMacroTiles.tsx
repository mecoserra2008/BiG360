import { useQuery } from '@tanstack/react-query'
import { Landmark } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router'
import { USE_MOCK } from '@/api/http'
import { liveApi } from '@/api/live/client'
import { useLive } from '@/api/live/store'
import { type FeedItem, type LiveMacro, feedFromMacro, mergeFeed } from '@/api/live/types'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { Panel } from '@/components/ui/panel'
import { fmtRelative } from '@/lib/format'
import { cn } from '@/lib/utils'

/** Days since an ISO timestamp. */
const ageDays = (iso: string): number => (Date.now() - Date.parse(iso)) / 86_400_000
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const num = (v: unknown) => (fin(v) ? (Math.abs(v) >= 1000 ? v.toFixed(0) : Number(v.toPrecision(4)).toString()) : '—')

/** Cockpit tiles: the latest macro releases with the model-based surprise; live items replace the snapshot as they stream in. */
export function LiveMacroTiles() {
  const q = useQuery({
    queryKey: ['live', 'macro', 8],
    queryFn: () => liveApi.macro(8),
    refetchInterval: 120_000,
    staleTime: 60_000,
    enabled: !USE_MOCK,
  })
  const streamed = useLive((s) => s.feed)
  const byUid = useMemo(() => new Map((q.data?.items ?? []).map((m) => [feedFromMacro(m).uid, m])), [q.data])
  const tiles = useMemo(() => {
    const base = (q.data?.items ?? []).map((m) => feedFromMacro(m))
    return mergeFeed(
      base,
      streamed.filter((x) => x.topic === 'macro'),
      50,
    ).slice(0, 4)
  }, [q.data, streamed])
  if (USE_MOCK) return null
  // audit F41: say how old the newest captured release is (nothing new ingested ≠ nothing released)
  const newest = tiles.map((t) => t.ts).filter((x): x is string => !!x).sort().reverse()[0] ?? null
  const staleDays = newest ? ageDays(newest) : 0
  return (
    <Panel
      title={
        <span className="inline-flex items-center gap-2">
          <Landmark className="size-4 text-accent" aria-hidden /> Latest releases
        </span>
      }
      subtitle={`Most recent official macro releases · surprise vs the model-based expectation (not consensus) · ${newest ? `newest captured ${fmtRelative(newest)}` : 'updates live'}`}
      actions={
        <>
          {staleDays > 3 && <Badge tone="warn">no new release ingested for {Math.floor(staleDays)} days</Badge>}
          <Badge tone="info">model-based surprise</Badge>
        </>
      }
    >
      {tiles.length === 0 ? (
        <p className="py-4 text-center text-xs text-muted">{q.isLoading ? 'Loading…' : 'No releases captured yet.'}</p>
      ) : (
        <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
          {tiles.map((t) => (
            <Tile key={t.uid} t={t} m={byUid.get(t.uid) ?? null} />
          ))}
        </div>
      )}
    </Panel>
  )
}

function Tile({ t, m }: { t: FeedItem; m: LiveMacro | null }) {
  const z = m ? (fin(m.signed_z) ? m.signed_z : fin(m.surprise_z) ? m.surprise_z : null) : null
  const cc = m?.country ?? t.title.split(' ')[0]
  return (
    <Link to={t.link ?? '/macro'} className="block rounded-md border border-border px-3 py-2 hover:border-border-strong hover:bg-surface-2">
      <div className="flex items-center gap-1.5 text-2xs text-muted">
        {cc && cc.length === 2 && <Flag country={cc} />}
        <span className="truncate">{m?.publisher ?? 'official release'}</span>
        {t.origin === 'stream' || m?.origin === 'live' ? <Badge tone="up">live</Badge> : null}
        <span className="num ml-auto shrink-0">{t.ts ? fmtRelative(t.ts) : ''}</span>
      </div>
      <div className="mt-0.5 line-clamp-2 min-h-8 text-xs font-medium text-fg" title={t.title}>
        {m?.release_name ?? t.title}
      </div>
      {m ? (
        <div className="mt-1 flex items-end justify-between gap-2">
          <div className="num text-xs text-muted">
            <span className="text-md font-semibold text-fg">{num(m.actual)}</span> vs model {num(m.expectation)}
          </div>
          <span
            className={cn('num rounded-sm px-1.5 py-0.5 text-xs font-semibold', z === null ? 'bg-surface-3 text-muted' : Math.abs(z) < 1 ? 'bg-surface-3 text-fg' : z > 0 ? 'bg-up-soft text-up' : 'bg-down-soft text-down')}
            title="Surprise z vs the model-based expectation"
          >
            z {z === null ? '—' : `${z > 0 ? '+' : z < 0 ? '−' : ''}${Math.abs(z).toFixed(1)}`}
          </span>
        </div>
      ) : (
        <div className="mt-1 text-xs text-muted">{t.detail}</div>
      )}
    </Link>
  )
}
