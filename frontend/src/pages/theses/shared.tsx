import { NotebookPen } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { autoMeta, DIRECTION_META, newThesisHref, REASON_TONE, type NewThesisLink } from '@/api/theses/adapters'
import type { Direction, ThesisReason } from '@/api/theses/types'
import { Badge } from '@/components/ui/badge'
import { buttonVariants } from '@/components/ui/button'
import { parseInline, parseMarkdown, type Inline } from '@/lib/markdown'
import { cn } from '@/lib/utils'

export function AutoStatusBadge({ status, className }: { status: string | null | undefined; className?: string }) {
  const m = autoMeta(status)
  return (
    <Badge tone={m.tone} className={className} title="Automatic status (model-based rules on the latest data)">
      {m.label}
    </Badge>
  )
}

export function DirectionBadge({ direction }: { direction: Direction }) {
  const m = DIRECTION_META[direction]
  return <Badge tone={m.tone}>{m.label}</Badge>
}

export function ConvictionDots({ value, className }: { value: number; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-0.5', className)} role="img" aria-label={`Conviction ${value} of 5`} title={`Conviction ${value}/5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} className={cn('size-1.5 rounded-full', i <= value ? 'bg-accent' : 'bg-surface-3')} />
      ))}
    </span>
  )
}

export function ReasonList({ reasons, max }: { reasons: ThesisReason[]; max?: number }) {
  const rs = max ? reasons.slice(0, max) : reasons
  if (!rs.length) return <p className="text-xs text-muted">No reasons recorded yet.</p>
  return (
    <ul className="space-y-1 text-xs">
      {rs.map((r) => (
        <li key={r.code + r.message} className="flex items-start gap-2">
          <Badge tone={REASON_TONE[r.level]} className="mt-px shrink-0">
            {r.level === 'invalid' ? 'invalid' : r.level}
          </Badge>
          <span className="text-fg">{r.message}</span>
        </li>
      ))}
    </ul>
  )
}

/** "Create thesis" entry point used across pages (Company 360, Screener, Studio, Cross-asset, Backtest Lab). */
export function CreateThesisLink({ link, label = 'Create thesis', size = 'sm', variant = 'outline', className, title }: { link: NewThesisLink; label?: ReactNode; size?: 'xs' | 'sm'; variant?: 'outline' | 'secondary' | 'ghost'; className?: string; title?: string }) {
  return (
    <Link to={newThesisHref(link)} className={cn(buttonVariants({ variant, size }), className)} title={title ?? 'Open the thesis tracker with this subject pre-filled'}>
      <NotebookPen /> {label}
    </Link>
  )
}

function InlineView({ inl }: { inl: Inline[] }) {
  return (
    <>
      {inl.map((x, i) =>
        x.t === 'b' ? (
          <strong key={i} className="font-semibold text-fg">
            {x.v}
          </strong>
        ) : x.t === 'i' ? (
          <em key={i}>{x.v}</em>
        ) : x.t === 'code' ? (
          <code key={i} className="num rounded-sm bg-surface-2 px-1 text-xs">
            {x.v}
          </code>
        ) : x.t === 'a' ? (
          <a key={i} href={x.href} target="_blank" rel="noreferrer noopener" className="text-accent-text underline-offset-2 hover:underline">
            {x.v}
          </a>
        ) : (
          <span key={i}>{x.v}</span>
        ),
      )}
    </>
  )
}

/** Safe Markdown subset renderer (lib/markdown). */
export function Markdown({ text, className }: { text: string | null | undefined; className?: string }) {
  const blocks = parseMarkdown(text ?? '')
  if (!blocks.length) return <p className={cn('text-xs text-muted', className)}>Nothing written yet.</p>
  return (
    <div className={cn('space-y-2 text-sm leading-relaxed text-fg', className)}>
      {blocks.map((b, i) => {
        switch (b.t) {
          case 'h':
            return b.level === 1 ? (
              <h3 key={i} className="text-md font-semibold">
                <InlineView inl={b.inl} />
              </h3>
            ) : (
              <h4 key={i} className={cn('font-semibold', b.level === 2 ? 'text-sm' : 'text-xs uppercase tracking-wide text-muted')}>
                <InlineView inl={b.inl} />
              </h4>
            )
          case 'ul':
          case 'ol': {
            const L = b.t === 'ul' ? 'ul' : 'ol'
            return (
              <L key={i} className={cn('space-y-0.5 pl-5', b.t === 'ul' ? 'list-disc' : 'list-decimal')}>
                {b.items.map((it, j) => (
                  <li key={j}>
                    <InlineView inl={it} />
                  </li>
                ))}
              </L>
            )
          }
          case 'quote':
            return (
              <blockquote key={i} className="border-l-2 border-accent pl-3 text-muted">
                <InlineView inl={b.inl} />
              </blockquote>
            )
          case 'pre':
            return (
              <pre key={i} className="num overflow-x-auto rounded-md bg-surface-2 p-2 text-xs">
                {b.v}
              </pre>
            )
          case 'hr':
            return <hr key={i} className="border-border" />
          default:
            return (
              <p key={i}>
                <InlineView inl={b.inl} />
              </p>
            )
        }
      })}
    </div>
  )
}

export function InlineMarkdown({ text }: { text: string }) {
  return <InlineView inl={parseInline(text)} />
}

export const textareaCls =
  'w-full rounded-md border border-border bg-surface-2 px-2 py-1.5 text-sm text-fg placeholder:text-faint hover:border-border-strong focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-focus'
