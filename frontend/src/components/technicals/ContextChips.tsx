import { contextChips, type ChipTone } from '@/api/techcontext/adapters'
import type { TechContext } from '@/api/techcontext/types'
import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

const TONE: Record<ChipTone, string> = {
  up: 'border-up/40 text-up',
  down: 'border-down/40 text-down',
  warn: 'border-warn/50 text-warn',
  info: 'border-info/50 text-info',
  neutral: 'border-border text-fg',
}

/** T5 context chips: range-width squeeze / expansion, realised / implied vol, ATR, ADX, trend alignment, earnings. */
export function ContextChips({ ctx, loading, className }: { ctx: TechContext | undefined; loading?: boolean; className?: string }) {
  const chips = contextChips(ctx)
  if (loading) return <div className={cn('h-7 animate-pulse rounded-md bg-surface-2', className)} aria-hidden />
  if (!chips.length) return null
  return (
    <ul className={cn('flex flex-wrap items-center gap-1.5', className)} aria-label="Volatility, regime and earnings context" data-testid="context-chips">
      {chips.map((c) => (
        <li key={c.key}>
          <Tip content={<span className="block max-w-[300px] whitespace-normal">{c.tooltip}</span>}>
            <button
              type="button"
              className={cn('inline-flex h-7 cursor-help items-center gap-1.5 rounded-md border bg-surface px-2 text-xs focus-visible:outline-2 focus-visible:outline-focus', TONE[c.tone])}
              aria-label={`${c.label}: ${c.value}`}
            >
              <span className="label-caps text-muted">{c.label}</span>
              <span className="num font-medium whitespace-nowrap">{c.value}</span>
            </button>
          </Tip>
        </li>
      ))}
    </ul>
  )
}
