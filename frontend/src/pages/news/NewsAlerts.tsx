import * as Dialog from '@radix-ui/react-dialog'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { BellPlus, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import type { components } from '@/api/generated/schema'
import { http } from '@/api/http'
import { defaultWatchlistId } from '@/api/remote'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input, Switch } from '@/components/ui/inputs'
import { toast } from '@/components/ui/toast'
import { playChime, setSoundEnabled, soundEnabled } from '@/lib/sound'

type Alert = components['schemas']['Alert']
const NEWS_RULES = new Set(['watchlist_news', 'story_velocity'])

function describe(a: Alert): string {
  const p = (a.params ?? {}) as Record<string, unknown>
  if (a.rule_type === 'watchlist_news') return `Watchlist news · tiers ${Array.isArray(p.tiers) ? (p.tiers as number[]).join(', ') : '1, 2, 3'}${p.min_abs_sentiment ? ` · |sentiment| ≥ ${String(p.min_abs_sentiment)}` : ''}`
  return `Story burst · ≥ ${String(p.min_articles ?? 5)} articles / 60 min · ≥ ${String(p.min_ratio ?? 3)}× baseline · ≥ ${String(p.min_sources ?? 2)} sources`
}

/** News alerts (watchlist_news / story_velocity) + the optional alert sound. Toasts also reach the tray. */
export function NewsAlerts() {
  const [open, setOpen] = useState(false)
  const [sound, setSound] = useState(soundEnabled())
  const [minArticles, setMinArticles] = useState('5')
  const [minRatio, setMinRatio] = useState('3')
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['news', 'alerts'],
    queryFn: async () => (await http.get<Alert[]>('/alerts')).filter((a) => NEWS_RULES.has(a.rule_type)),
    enabled: open,
    retry: false,
  })
  const create = async (body: Record<string, unknown>, label: string) => {
    try {
      await http.post('/alerts', body)
      toast({ tone: 'success', title: 'Alert created', message: label })
      void qc.invalidateQueries({ queryKey: ['news', 'alerts'] })
    } catch (e) {
      toast({ tone: 'error', title: 'Alert not created', message: e instanceof Error ? e.message : String(e) })
    }
  }
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <Button size="xs" variant="outline" aria-label="News alerts">
          <BellPlus /> Alerts
        </Button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="fixed top-1/2 left-1/2 z-50 w-[min(520px,95vw)] -translate-x-1/2 -translate-y-1/2 rounded-md border border-border-strong bg-surface p-4 focus-visible:outline-none" aria-describedby={undefined}>
          <div className="mb-3 flex items-center gap-2">
            <Dialog.Title className="flex-1 text-md font-semibold text-fg">News alerts</Dialog.Title>
            <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={() => setOpen(false)}>
              <X />
            </Button>
          </div>
          <div className="space-y-3 text-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="font-medium text-fg">Alert sound</div>
                <div className="text-xs text-muted">Short chime in this browser when a news alert arrives (toasts appear in the app and the tray).</div>
              </div>
              <Switch
                label="Alert sound"
                checked={sound}
                onCheckedChange={(v) => {
                  setSound(v)
                  setSoundEnabled(v)
                  if (v) playChime()
                }}
              />
            </div>
            <div className="rounded-md border border-border p-2">
              <div className="mb-1 font-medium text-fg">Watchlist news</div>
              <p className="mb-2 text-xs text-muted">A live item linked to a watchlist name (official, major press and publisher sources); one alert per story.</p>
              <Button
                size="xs"
                onClick={async () => {
                  const wid = await defaultWatchlistId()
                  if (!wid) {
                    toast({ tone: 'error', title: 'No server watchlist', message: 'The API watchlist is unavailable.' })
                    return
                  }
                  await create({ watchlist_id: wid, rule_type: 'watchlist_news', params: { tiers: [1, 2, 3] }, cooldown_days: 0 }, 'News on your watchlist')
                }}
              >
                Alert on watchlist news
              </Button>
            </div>
            <div className="rounded-md border border-border p-2">
              <div className="mb-1 font-medium text-fg">Story velocity</div>
              <p className="mb-2 text-xs text-muted">A burst of articles on one story within 60 minutes versus its own baseline (any story, any source).</p>
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-1 text-xs text-muted">
                  ≥ articles
                  <Input aria-label="Minimum articles in 60 minutes" className="w-14" inputMode="numeric" value={minArticles} onChange={(e) => setMinArticles(e.target.value)} />
                </label>
                <label className="flex items-center gap-1 text-xs text-muted">
                  ≥ × baseline
                  <Input aria-label="Minimum ratio to baseline" className="w-14" inputMode="decimal" value={minRatio} onChange={(e) => setMinRatio(e.target.value)} />
                </label>
                <Button
                  size="xs"
                  onClick={() =>
                    create(
                      { security_id: null, watchlist_id: null, rule_type: 'story_velocity', params: { min_articles: Math.max(2, Number(minArticles) || 5), min_ratio: Math.max(1, Number(minRatio) || 3), min_sources: 2 }, cooldown_days: 0 },
                      'Story bursts',
                    )
                  }
                >
                  Alert on story bursts
                </Button>
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Active news alerts</div>
              {list.isLoading && <p className="text-xs text-muted">Loading…</p>}
              {list.data?.length === 0 && <p className="text-xs text-muted">No news alerts yet.</p>}
              <ul className="space-y-1">
                {list.data?.map((a) => (
                  <li key={a.alert_id} className="flex items-center gap-2 text-xs">
                    <Badge tone={a.enabled ? 'up' : 'neutral'}>{a.enabled ? 'on' : 'off'}</Badge>
                    <span className="min-w-0 flex-1 truncate">{describe(a)}</span>
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label="Delete alert"
                      onClick={async () => {
                        await http.delete(`/alerts/${encodeURIComponent(a.alert_id)}`).catch(() => undefined)
                        void qc.invalidateQueries({ queryKey: ['news', 'alerts'] })
                      }}
                    >
                      <Trash2 />
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
