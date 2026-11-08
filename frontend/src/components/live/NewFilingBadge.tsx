import { useQuery } from '@tanstack/react-query'
import { FileText } from 'lucide-react'
import { useMemo } from 'react'
import { resolveId } from '@/api/client'
import { USE_MOCK } from '@/api/http'
import { liveApi } from '@/api/live/client'
import { useLive } from '@/api/live/store'
import { feedFromFiling, mergeFeed } from '@/api/live/types'
import { Badge } from '@/components/ui/badge'
import { Tip } from '@/components/ui/tooltip'
import { fmtDateTime } from '@/lib/format'

/** "New filing" badge on Company 360: filings first seen by the real-time layer (REST hot store + the live stream). */
export function NewFilingBadge({ id, ticker }: { id: string; ticker: string }) {
  const q = useQuery({
    queryKey: ['live', 'filings', id],
    queryFn: async () => liveApi.filings({ securityId: await resolveId(id), limit: 10 }),
    enabled: !USE_MOCK,
    refetchInterval: 120_000,
    staleTime: 60_000,
    retry: false,
  })
  const feed = useLive((s) => s.feed)
  const items = useMemo(() => {
    const sid = q.data?.items[0]?.security_id ?? (id.startsWith('S') ? id : null)
    const rest = (q.data?.items ?? []).filter((f) => f.origin === 'live').map((f) => feedFromFiling(f))
    const streamed = feed.filter((x) => x.topic === 'filings' && ((sid && x.securityId === sid) || x.ticker === ticker))
    return mergeFeed(rest, streamed, 20)
  }, [q.data, feed, id, ticker])
  if (USE_MOCK || items.length === 0) return null
  return (
    <Tip
      content={
        <ul className="max-w-sm space-y-1">
          {items.slice(0, 5).map((x) => (
            <li key={x.uid}>
              <span className="font-medium">{x.title}</span>
              {x.detail ? ` · ${x.detail}` : ''}
              <span className="block text-2xs text-muted">{x.ts ? fmtDateTime(x.ts) : ''}</span>
            </li>
          ))}
        </ul>
      }
    >
      {items[0].href ? (
        <a href={items[0].href} target="_blank" rel="noreferrer" aria-label={`${items.length} new filing${items.length > 1 ? 's' : ''}, open the latest`}>
          <Badge tone="accent">
            <FileText /> {items.length === 1 ? 'New filing' : `${items.length} new filings`}
          </Badge>
        </a>
      ) : (
        <Badge tone="accent" tabIndex={0}>
          <FileText /> {items.length === 1 ? 'New filing' : `${items.length} new filings`}
        </Badge>
      )}
    </Tip>
  )
}
