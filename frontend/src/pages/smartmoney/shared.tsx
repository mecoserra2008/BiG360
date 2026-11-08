import { Clock, Info, ShieldCheck } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router'
import { eventMeta, lagText } from '@/api/smartmoney/adapters'
import type { SmLag, SmSource } from '@/api/smartmoney/types'
import { Badge } from '@/components/ui/badge'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtPct, isNum } from '@/lib/format'
import { cn } from '@/lib/utils'

const LINKS = [
  { to: '/smart-money', label: 'Overview', end: true },
  { to: '/smart-money/eu-shorts', label: 'EU / UK short register' },
  { to: '/smart-money/activism', label: 'Activism (13D)' },
  { to: '/smart-money/sectors', label: 'Sectors' },
]

export function SmartMoneyNav() {
  return (
    <nav aria-label="Smart money sections" className="mb-3 flex flex-wrap items-center gap-x-1 border-b border-border">
      {LINKS.map((l) => (
        <NavLink
          key={l.to}
          to={l.to}
          end={l.end}
          className={({ isActive }) =>
            cn('-mb-px h-8 border-b-2 px-3 text-sm leading-8 font-medium whitespace-nowrap transition-colors', isActive ? 'border-accent text-fg' : 'border-transparent text-muted hover:text-fg')
          }
        >
          {l.label}
        </NavLink>
      ))}
    </nav>
  )
}

/** The 13F lag, always visible next to 13F numbers. */
export function LagNote({ lag, className }: { lag: SmLag | null | undefined; className?: string }) {
  return (
    <Tip content="13F filings report quarter-end long positions and are due 45 days after the quarter. We use only filings made by the cutoff (point-in-time).">
      <span tabIndex={0} className={cn('inline-flex cursor-help items-center gap-1 text-2xs text-muted', className)}>
        <Clock className="size-3" aria-hidden />
        <span>{lagText(lag)}</span>
      </span>
    </Tip>
  )
}

export function ModelBasedTag({ tip, children }: { tip: string; children?: ReactNode }) {
  return (
    <Tip content={tip}>
      <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 text-2xs text-muted">
        <Badge tone="warn">Model-based</Badge>
        {children}
        <Info className="size-3" aria-hidden />
      </span>
    </Tip>
  )
}

export function OfficialTag({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-2xs text-muted">
      <Badge tone="up">
        <ShieldCheck aria-hidden />
        Official
      </Badge>
      {label}
    </span>
  )
}

export function SourcesFooter({ sources }: { sources: SmSource[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-2 text-2xs text-muted">
      {sources.map((s) => (
        <Tip key={s.id} content={s.note || s.label}>
          <span tabIndex={0} className="inline-flex cursor-help items-center gap-1">
            <ShieldCheck className="size-3 text-up" aria-hidden />
            {s.label}
          </span>
        </Tip>
      ))}
    </div>
  )
}

export function CompanyLink({ ticker, name, id, tab = 'ownership', className }: { ticker: string; name?: string | null; id: string; tab?: string; className?: string }) {
  return (
    <Link to={`/company/${encodeURIComponent(ticker || id)}?tab=${tab}`} className={cn('min-w-0 hover:underline', className)} title={name ?? ticker}>
      <span className="num font-semibold text-fg">{ticker}</span>
      {name && <span className="ml-1.5 truncate text-muted">{name}</span>}
    </Link>
  )
}

export function EventBadge({ type }: { type: string }) {
  const m = eventMeta(type)
  return <Badge tone={m.tone}>{m.label}</Badge>
}

/** Signed value with the sign in the text (never colour alone); invert = higher is bearish (short interest). */
export function Signed({ v, digits = 1, invert = false, pp = false }: { v: number | null | undefined; digits?: number; invert?: boolean; pp?: boolean }) {
  if (!isNum(v)) return <span className="text-faint">{DASH}</span>
  const txt = pp ? `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)} pp` : fmtPct(v, digits, true)
  const good = v === 0 ? null : (v > 0) !== invert
  return <span className={cn('num', good === null ? 'text-muted' : good ? 'text-up' : 'text-down')}>{txt}</span>
}
