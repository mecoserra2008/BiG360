import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { HelpLink } from '../layout/HelpLink'

interface PanelProps extends Omit<ComponentProps<'section'>, 'title'> {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  /** remove body padding (tables, edge-to-edge charts) */
  flush?: boolean
  bodyClassName?: string
}

/** The basic surface: subtle border, 4px radius, compact header. No shadows. */
export function Panel({ title, subtitle, actions, flush, className, bodyClassName, children, ...props }: PanelProps) {
  return (
    <section className={cn('flex min-w-0 flex-col rounded-md border border-border bg-surface', className)} {...props}>
      {(title || actions) && (
        <header className="flex min-h-9 items-center gap-2 border-b border-border px-3 py-1.5">
          <div className="min-w-0 flex-1">
            {title && <h2 className="truncate text-sm font-semibold text-fg">{title}</h2>}
            {subtitle && <p className="truncate text-xs text-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cn('min-h-0 flex-1', !flush && 'p-3', bodyClassName)}>{children}</div>
    </section>
  )
}

export function PageHeader({ title, subtitle, actions, children }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-end gap-x-4 gap-y-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <h1 className="text-xl font-semibold tracking-tight text-fg">{title}</h1>
          <HelpLink />
        </div>
        {subtitle && <p className="text-sm text-muted">{subtitle}</p>}
      </div>
      {children}
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Stat({ label, value, sub, className }: { label: ReactNode; value: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="label-caps truncate">{label}</div>
      <div className="num truncate text-md font-medium text-fg">{value}</div>
      {sub && <div className="truncate text-xs text-muted">{sub}</div>}
    </div>
  )
}
