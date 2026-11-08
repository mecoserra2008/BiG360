import type { UseQueryResult } from '@tanstack/react-query'
import { AlertTriangle, Hourglass, Inbox, MinusCircle, RotateCw } from 'lucide-react'
import type { CSSProperties, ReactNode } from 'react'
import { isPendingError } from '@/api/errors'
import { cn } from '@/lib/utils'
import { Button } from './button'

export function Skeleton({ className, style }: { className?: string; style?: CSSProperties }) {
  return <div aria-hidden className={cn('skeleton rounded-md', className)} style={style} />
}

export function SkeletonRows({ rows = 8, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('space-y-1.5', className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-5" style={{ width: `${92 - ((i * 37) % 30)}%` }} />
      ))}
    </div>
  )
}

export function EmptyState({ title = 'No data', message, icon, action, className }: { title?: string; message?: ReactNode; icon?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div data-testid="empty-state" className={cn('flex flex-col items-center justify-center gap-2 px-4 py-10 text-center', className)}>
      <div className="text-faint [&_svg]:size-6">{icon ?? <Inbox />}</div>
      <div className="text-sm font-medium text-fg">{title}</div>
      {message && <div className="max-w-sm text-xs text-muted">{message}</div>}
      {action}
    </div>
  )
}

/**
 * The data does not apply to this entity by design (e.g. FINRA short interest or SEC 13F for a non-US listing,
 * availability code not_applicable): an informational note, NOT an empty panel. EmptyState means "data expected but
 * missing" (the UI smoke suite fails on it); this one is neutral and carries data-testid="na-state".
 */
export function NotApplicableState({ title = 'Not applicable', message, className }: { title?: string; message?: ReactNode; className?: string }) {
  return (
    <div role="note" data-testid="na-state" className={cn('flex flex-col items-center justify-center gap-1.5 px-4 py-8 text-center', className)}>
      <MinusCircle className="size-5 text-faint" aria-hidden />
      <div className="text-sm font-medium text-muted">{title}</div>
      {message && <div className="max-w-sm text-xs text-faint">{message}</div>}
    </div>
  )
}

/** Shown when the backend answers 501: the contract defines the endpoint but the pipeline has not produced it yet. */
export function PendingState({ detail, className, compact = false }: { detail?: string; className?: string; compact?: boolean }) {
  return (
    <div role="status" data-testid="pending-state" className={cn('flex flex-col items-center justify-center gap-1.5 px-4 text-center', compact ? 'py-6' : 'py-10', className)}>
      <Hourglass className="size-5 text-faint" aria-hidden />
      <div className="text-sm font-medium text-fg">Not yet available — pipeline pending</div>
      <div className="max-w-sm text-xs text-muted">{detail ?? 'This view is defined in the API contract but the backend has not published it yet.'}</div>
    </div>
  )
}

export function ErrorState({ error, onRetry, className }: { error: unknown; onRetry?: () => void; className?: string }) {
  if (isPendingError(error))
    return <PendingState className={className} detail={error instanceof Error ? `Backend: ${error.message.replace(' is defined in contracts/api.yaml but', '')}.` : undefined} />
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div role="alert" data-testid="error-state" className={cn('flex flex-col items-center justify-center gap-2 px-4 py-10 text-center', className)}>
      <AlertTriangle className="size-6 text-down" aria-hidden />
      <div className="text-sm font-medium text-fg">Could not load data</div>
      <div className="max-w-md text-xs break-words text-muted">{msg}</div>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RotateCw /> Retry
        </Button>
      )}
    </div>
  )
}

/** Renders loading / error / empty / data states for a TanStack query uniformly. */
export function QueryView<T>({
  query,
  skeleton,
  isEmpty,
  empty,
  children,
}: {
  query: UseQueryResult<T>
  skeleton?: ReactNode
  isEmpty?: (d: T) => boolean
  empty?: ReactNode
  children: (data: T) => ReactNode
}) {
  if (query.isPending) return <>{skeleton ?? <SkeletonRows className="p-3" />}</>
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />
  const data = query.data as T
  // v1.2 contract: payloads carry data_status (ok | partial | pending)
  const status = (data as { dataStatus?: string } | null)?.dataStatus
  if (status === 'pending') return <PendingState detail="The backend has this view but the pipeline has not produced it yet (e.g. models still training)." />
  if (isEmpty?.(data)) return <>{empty ?? <EmptyState />}</>
  if (status === 'partial')
    return (
      <>
        <PartialNote />
        {children(data)}
      </>
    )
  return <>{children(data)}</>
}

/** Thin banner for payloads the API marks data_status = "partial". */
export function PartialNote({ className }: { className?: string }) {
  return (
    <div role="note" className={cn('mb-2 flex items-center gap-2 rounded-md border border-warn/40 bg-warn-soft px-3 py-1.5 text-xs text-warn', className)}>
      <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
      Partial data — some inputs for this view are missing or stale; figures shown are what the pipeline has published.
    </div>
  )
}
