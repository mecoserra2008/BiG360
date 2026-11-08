import { Info } from 'lucide-react'
import { Missing } from '@/components/ui/missing'
import type { AvailabilityReason } from '@/lib/availability'
import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router'
import { dateSourceMeta } from '@/api/earnings/adapters'
import { EXPECTATION_LABEL } from '@/api/earnings/types'
import { Badge } from '@/components/ui/badge'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'

const LINKS = [
  { to: '/earnings', label: 'Calendar', end: true },
  { to: '/earnings/season', label: 'Earnings season' },
]

export function EarningsNav() {
  return (
    <nav aria-label="Earnings sections" className="mb-3 flex flex-wrap items-center gap-x-1 border-b border-border">
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

/** What a date is based on (official / provider estimate / model-based projection) - always visible. */
export function SourceBadge({ source, window, className }: { source: string | null | undefined; window?: { lo?: string | null; hi?: string | null }; className?: string }) {
  const m = dateSourceMeta(source)
  const tip = window?.lo && window.hi ? `${m.long}. Plausible window ${window.lo} → ${window.hi}.` : m.long
  return (
    <Tip content={tip}>
      <span tabIndex={0} className={cn('inline-flex cursor-help', className)}>
        <Badge tone={m.tone}>{m.short}</Badge>
      </span>
    </Tip>
  )
}

export function ModelBasedTag({ children }: { children?: ReactNode }) {
  return (
    <Tip content="Expectations come from time-series models on the company's own reported history (Foster seasonal SARIMA, seasonal naive, analytics forecast path). They are not analyst consensus.">
      <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 text-2xs text-muted">
        <Badge tone="warn">Model-based</Badge>
        {children ?? <span>{EXPECTATION_LABEL}</span>}
        <Info className="size-3" aria-hidden />
      </span>
    </Tip>
  )
}

export function CompanyLink({ ticker, name, id, className }: { ticker: string; name?: string | null; id: string; className?: string }) {
  return (
    <Link to={`/company/${encodeURIComponent(ticker || id)}?tab=earnings`} className={cn('min-w-0 hover:underline', className)} title={name ?? ticker}>
      <span className="num font-semibold text-fg">{ticker}</span>
      {name && <span className="ml-1.5 truncate text-muted">{name}</span>}
    </Link>
  )
}

/** Surprise / reaction value coloured by sign, with the sign also in the text (not colour alone). */
/** `field` / `reason` (completeness C): an empty value explains itself instead of a bare dash. */
export function Signed({ v, kind = 'pct', digits = 1, field, reason }: { v: number | null | undefined; kind?: 'pct' | 'z'; digits?: number; field?: string; reason?: AvailabilityReason | string | null }) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return field || reason ? <Missing field={field} reason={reason} /> : <span className="text-faint">{DASH}</span>
  const txt = kind === 'z' ? `${v > 0 ? '+' : ''}${v.toFixed(2)}σ` : fmtPct(v, digits, true)
  return <span className={cn('num', v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-muted')}>{txt}</span>
}
