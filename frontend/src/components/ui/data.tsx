import {
  AE, AR, AU, BE, BR, CA, CH, CL, CN, CO, DE, DK, EG, ES, FI, FR, GB, HK, ID, IE, IL, IN, IR, IT, JP, KE, KR, MX, MY, NG, NL, NO, NZ, PE, PH, PK, PL, PT, RU, SA, SE, TH, TR, TW, UA, US, VN, ZA,
} from 'country-flag-icons/react/3x2'
import { Popover as RPopover } from 'radix-ui'
import { ArrowDownRight, ArrowUpRight, Minus, ShieldCheck, ShieldHalf, ShieldOff } from 'lucide-react'
import type { ComponentProps, ComponentType, ReactNode } from 'react'
import type { DataQuality, MoatRating } from '@/api/types'
import { DASH, fmtPct, isNum } from '@/lib/format'
import { countryName } from '@/lib/regions'
import { cn } from '@/lib/utils'
import { Tip } from './tooltip'

/* ── directional change: arrow + sign + colour (never colour alone) ── */

export function Delta({
  value,
  format = (v) => fmtPct(v, 2, true),
  invert = false,
  className,
  showIcon = true,
}: {
  value: number | null | undefined
  format?: (v: number) => string
  invert?: boolean
  className?: string
  showIcon?: boolean
}) {
  if (!isNum(value)) return <span className={cn('num text-muted', className)}>{DASH}</span>
  const good = value === 0 ? null : value > 0 !== invert
  const Icon = value > 0 ? ArrowUpRight : value < 0 ? ArrowDownRight : Minus
  return (
    <span className={cn('num inline-flex items-center gap-0.5 whitespace-nowrap', good === null ? 'text-muted' : good ? 'text-up' : 'text-down', className)}>
      {showIcon && <Icon className="size-3 shrink-0" aria-hidden />}
      {format(value)}
    </span>
  )
}

/* ── percentile bar 0–100 ── */

export function PercentileBar({ value, className, showValue = true, width = 'w-14' }: { value: number | null | undefined; className?: string; showValue?: boolean; width?: string }) {
  if (!isNum(value)) return <span className="num text-muted">{DASH}</span>
  const v = Math.max(0, Math.min(100, value))
  const tone = v >= 67 ? 'bg-up' : v <= 33 ? 'bg-down' : 'bg-muted'
  return (
    <span className={cn('inline-flex items-center gap-1.5', className)} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v)} aria-label="Percentile">
      {showValue && <span className="num w-6 text-right text-fg">{Math.round(v)}</span>}
      <span className={cn('relative h-1.5 overflow-hidden rounded-full bg-surface-3', width)}>
        <span className={cn('absolute inset-y-0 left-0 rounded-full opacity-85', tone)} style={{ width: `${v}%` }} />
      </span>
    </span>
  )
}

/* ── score pill: tercile tint + number ── */

export function ScorePill({ value, className, size = 'sm' }: { value: number | null | undefined; className?: string; size?: 'sm' | 'lg' }) {
  if (!isNum(value)) return <span className="num text-muted">{DASH}</span>
  const tone = value >= 67 ? 'bg-up-soft text-up' : value <= 33 ? 'bg-down-soft text-down' : 'bg-surface-3 text-fg'
  return (
    <span className={cn('num inline-flex items-center justify-center rounded-sm font-semibold', size === 'lg' ? 'h-7 min-w-10 px-2 text-md' : 'h-5 min-w-8 px-1 text-sm', tone, className)}>
      {Math.round(value)}
    </span>
  )
}

/* ── flags (bundled SVG; Windows has no emoji flags) ── */

type FlagComp = ComponentType<{ title?: string; className?: string }>
const FLAGS: Record<string, FlagComp> = {
  AE, AR, AU, BE, BR, CA, CH, CL, CN, CO, DE, DK, EG, ES, FI, FR, GB, HK, ID, IE, IL, IN, IR, IT, JP, KE, KR, MX, MY, NG, NL, NO, NZ, PE, PH, PK, PL, PT, RU, SA, SE, TH, TR, TW, UA, US, VN, ZA,
}

export function Flag({ country, className }: { country: string; className?: string }) {
  const F = FLAGS[country]
  const name = countryName(country)
  if (!F)
    return (
      <span className={cn('inline-flex h-3 w-[18px] items-center justify-center rounded-[2px] bg-surface-3 text-[8px] font-bold text-muted', className)} title={name}>
        {country}
      </span>
    )
  return (
    <span className={cn('inline-flex h-3 w-[18px] shrink-0 overflow-hidden rounded-[2px] ring-1 ring-border', className)} title={name} role="img" aria-label={name}>
      <F className="h-full w-full" />
    </span>
  )
}

/* ── moat / data-quality badges ── */

const MOAT_META: Record<MoatRating, { label: string; cls: string; Icon: typeof ShieldCheck }> = {
  wide: { label: 'Wide moat', cls: 'bg-accent-soft text-accent-text', Icon: ShieldCheck },
  narrow: { label: 'Narrow moat', cls: 'bg-info-soft text-info', Icon: ShieldHalf },
  none: { label: 'No moat', cls: 'bg-surface-3 text-muted', Icon: ShieldOff },
  insufficient: { label: 'Moat: insufficient data', cls: 'border border-dashed border-border text-faint', Icon: ShieldOff },
}

export function MoatBadge({ rating, compact = false }: { rating: MoatRating; compact?: boolean }) {
  const m = MOAT_META[rating]
  return (
    <span className={cn('inline-flex h-5 items-center gap-1 rounded-sm px-1.5 text-2xs font-semibold tracking-wide uppercase', m.cls)} title={m.label}>
      <m.Icon className="size-3" aria-hidden />
      {compact ? rating : m.label}
    </span>
  )
}

const DQ_META: Record<DataQuality, { label: string; dots: number; cls: string; tip: string }> = {
  high: { label: 'DQ high', dots: 3, cls: 'text-up', tip: 'Data quality: high — full statements, point-in-time, reconciled' },
  medium: { label: 'DQ med', dots: 2, cls: 'text-warn', tip: 'Data quality: medium — some fields inferred or delayed' },
  low: { label: 'DQ low', dots: 1, cls: 'text-down', tip: 'Data quality: low — sparse or unreconciled fundamentals' },
}

export function DataQualityBadge({ quality, notes }: { quality: DataQuality; notes?: string[] }) {
  const m = DQ_META[quality]
  return (
    <Tip
      content={
        <div className="space-y-1">
          <div>{m.tip}</div>
          {notes?.map((n) => (
            <div key={n} className="text-muted">
              • {n}
            </div>
          ))}
        </div>
      }
    >
      <span tabIndex={0} className="inline-flex h-5 items-center gap-1 rounded-sm border border-border px-1.5 text-2xs font-semibold tracking-wide text-muted uppercase">
        <span className="inline-flex gap-0.5" aria-hidden>
          {[0, 1, 2].map((i) => (
            <span key={i} className={cn('size-1.5 rounded-full', i < m.dots ? `bg-current ${m.cls}` : 'bg-surface-3')} />
          ))}
        </span>
        {m.label}
      </span>
    </Tip>
  )
}

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return <kbd className={cn('num inline-flex h-4 items-center rounded-sm border border-border bg-surface-2 px-1 text-2xs text-muted', className)}>{children}</kbd>
}

/* ── popover ── */

export function Popover({
  trigger,
  children,
  align = 'end',
  className,
  ...props
}: { trigger: ReactNode; children: ReactNode; align?: 'start' | 'center' | 'end'; className?: string } & ComponentProps<typeof RPopover.Root>) {
  return (
    <RPopover.Root {...props}>
      <RPopover.Trigger asChild>{trigger}</RPopover.Trigger>
      <RPopover.Portal>
        <RPopover.Content
          align={align}
          sideOffset={6}
          collisionPadding={8}
          className={cn('z-50 max-h-[var(--radix-popover-content-available-height)] overflow-auto rounded-md border border-border-strong bg-surface p-3 text-sm', className)}
        >
          {children}
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  )
}
