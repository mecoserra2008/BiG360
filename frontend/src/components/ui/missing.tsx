import { createContext, useContext, type ReactNode } from 'react'
import { useCompanyAvailability, useFieldAvailability } from '@/api/availability/hooks'
import { CODE_LABEL, documentedNotApplicable, reasonText, reasonTone, resolveReason, type AvailabilityReason } from '@/lib/availability'
import { DASH } from '@/lib/format'
import { cn } from '@/lib/utils'
import { EmptyState, NotApplicableState } from './feedback'
import { Tip } from './tooltip'

/**
 * Empty values that explain themselves (completeness audit, docs/DATA_COMPLETENESS.md): every "—" carries a tooltip
 * with the reason - "Not published yet: next release 2026-11-05", "No free source for this market", "Needs
 * OPENDART_APIKEY", "Pending data refresh" ...
 *
 *   <Missing field="roic" />                       reason from the security scope / region / field catalog
 *   <Missing reason={notPublishedUntil(date)} />   explicit reason
 *   <Avail value={x} field="pe_ttm">{fmtMultiple(x)}</Avail>   value when present, else <Missing>
 *
 * <AvailabilityScope securityId region> (Company 360) loads the security's own reasons once for every tab below.
 */

interface Scope {
  region?: string | null
  reasons?: Record<string, AvailabilityReason> | null
}
const ScopeCtx = createContext<Scope>({})

export function AvailabilityScope({ securityId, region, children }: { securityId?: string | null; region?: string | null; children: ReactNode }) {
  const q = useCompanyAvailability(securityId)
  return <ScopeCtx.Provider value={{ region: region ?? q.data?.region ?? null, reasons: q.data?.reasons ?? null }}>{children}</ScopeCtx.Provider>
}

/** Region-only scope (tables of one region, e.g. a screener row). */
export function RegionScope({ region, children }: { region?: string | null; children: ReactNode }) {
  const parent = useContext(ScopeCtx)
  return <ScopeCtx.Provider value={{ ...parent, region }}>{children}</ScopeCtx.Provider>
}

export function useMissingReason(field?: string | null, region?: string | null, reason?: AvailabilityReason | string | null): AvailabilityReason {
  const scope = useContext(ScopeCtx)
  const cat = useFieldAvailability()
  return resolveReason({ reason, field, region: region ?? scope.region, entity: scope.reasons, catalog: cat.data ?? null })
}

const TONE: Record<ReturnType<typeof reasonTone>, string> = {
  neutral: 'decoration-border-strong',
  info: 'decoration-info',
  warn: 'decoration-warn',
  down: 'decoration-down',
}

export function MissingContent({ reason }: { reason: AvailabilityReason }) {
  return (
    <span className="block space-y-0.5" data-testid="missing-reason">
      <span className="block font-semibold">{reason.label || CODE_LABEL[reason.code]}</span>
      <span className="block text-muted">{reasonText(reason)}</span>
      {reason.detail && reason.detail !== '-' && reason.detail !== 'by design' && <span className="block text-2xs text-faint">Fix / owner: {reason.detail}</span>}
    </span>
  )
}

export function Missing({
  field,
  region,
  reason,
  text = DASH,
  className,
}: {
  field?: string | null
  region?: string | null
  reason?: AvailabilityReason | string | null
  text?: string
  className?: string
}) {
  const r = useMissingReason(field, region, reason)
  return (
    <Tip content={<MissingContent reason={r} />}>
      <span
        tabIndex={0}
        role="note"
        aria-label={`${text === DASH ? 'Not available' : text}. ${reasonText(r)}`}
        data-reason={r.code}
        className={cn('num cursor-help text-faint underline decoration-dotted underline-offset-2', TONE[reasonTone(r.code)], className)}
      >
        {text}
      </span>
    </Tip>
  )
}

/**
 * An empty panel that explains itself: a neutral "not applicable" note when the catalog / the security documents
 * that the data does not apply here (FINRA short interest or SEC 13F on a non-US listing), otherwise the usual empty
 * state with the reason (data expected but missing). Alert hygiene 2026-10-30: the UI smoke suite flagged the
 * Ownership tab of 7203.T because "not applicable" was rendered as missing data.
 */
export function NoDataState({ reason, title, className }: { reason: AvailabilityReason; title: string; className?: string }) {
  if (documentedNotApplicable(reason)) return <NotApplicableState title="Not applicable to this listing" message={reasonText(reason)} className={className} />
  return <EmptyState title={title} message={reasonText(reason)} className={className} />
}

const present = (v: unknown): boolean => v !== null && v !== undefined && !(typeof v === 'number' && !Number.isFinite(v)) && v !== ''

/** Render children when `value` is present, else the explained "—". */
export function Avail({
  value,
  field,
  region,
  reason,
  children,
  className,
}: {
  value: unknown
  field?: string | null
  region?: string | null
  reason?: AvailabilityReason | string | null
  children?: ReactNode
  className?: string
}) {
  if (present(value)) return <>{children}</>
  return <Missing field={field} region={region} reason={reason} className={className} />
}
