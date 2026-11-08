import { Link2 } from 'lucide-react'
import { Fragment, type ReactNode } from 'react'
import { Link } from 'react-router'
import { guidePath } from '@/lib/guide/helpMap'
import { resolveLink, type Align, type Block, type Inline, type ListBlock } from '@/lib/guide/markdown'
import { cn } from '@/lib/utils'
import { imageUrl } from './content'

function Inl({ nodes, chapter }: { nodes: Inline[]; chapter: string }): ReactNode {
  return nodes.map((n, i) => {
    switch (n.t) {
      case 'text':
        return <Fragment key={i}>{n.v}</Fragment>
      case 'code':
        return (
          <code key={i} className="num rounded-sm bg-surface-2 px-1 py-px text-[0.9em] text-fg">
            {n.v}
          </code>
        )
      case 'b':
        return (
          <strong key={i} className="font-semibold text-fg">
            <Inl nodes={n.c} chapter={chapter} />
          </strong>
        )
      case 'i':
        return (
          <em key={i}>
            <Inl nodes={n.c} chapter={chapter} />
          </em>
        )
      case 'img': {
        const url = imageUrl(n.src)
        return url ? <img key={i} src={url} alt={n.alt} className="inline max-h-6 align-middle" /> : <Fragment key={i}>{n.alt}</Fragment>
      }
      case 'a': {
        const tgt = resolveLink(n.href)
        const kids = <Inl nodes={n.c} chapter={chapter} />
        if (tgt.kind === 'external')
          return (
            <a key={i} href={tgt.href} target="_blank" rel="noreferrer noopener" className="text-accent-text underline-offset-2 hover:underline">
              {kids}
            </a>
          )
        if (tgt.kind === 'guide')
          return (
            <Link key={i} to={guidePath(tgt.chapter, tgt.anchor, chapter)} className="text-accent-text underline-offset-2 hover:underline">
              {kids}
            </Link>
          )
        return (
          <span key={i} className="text-fg" title={n.href}>
            {kids}
          </span>
        )
      }
    }
  })
}

const alignCls = (a: Align) => (a === 'right' ? 'text-right' : a === 'center' ? 'text-center' : 'text-left')

function List({ b, chapter }: { b: ListBlock; chapter: string }) {
  const Tag = b.ordered ? 'ol' : 'ul'
  return (
    <Tag start={b.ordered && b.start !== 1 ? b.start : undefined} className={cn('my-2 space-y-1 pl-5', b.ordered ? 'list-decimal' : 'list-disc', 'marker:text-faint')}>
      {b.items.map((it, i) => (
        <li key={i} className="pl-0.5">
          <Inl nodes={it.inl} chapter={chapter} />
          {it.sub && <List b={it.sub} chapter={chapter} />}
        </li>
      ))}
    </Tag>
  )
}

function quoteTone(b: Block[]): string {
  const first = b[0]
  const lead = first && first.t === 'p' && first.inl[0]?.t === 'b' ? first.inl[0].c.map((x) => (x.t === 'text' ? x.v : '')).join('') : ''
  if (/caution|warning|experimental|pending/i.test(lead)) return 'border-warn bg-warn-soft'
  if (/model-based|approximation/i.test(lead)) return 'border-info bg-info-soft'
  return 'border-border-strong bg-surface-2'
}

export function Blocks({ blocks, chapter }: { blocks: Block[]; chapter: string }) {
  return blocks.map((b, i) => <BlockView key={i} b={b} chapter={chapter} />)
}

function BlockView({ b, chapter }: { b: Block; chapter: string }) {
  switch (b.t) {
    case 'h': {
      const cls = {
        1: 'mt-1 mb-4 text-2xl font-semibold tracking-tight',
        2: 'mt-10 mb-3 border-b border-border pb-1.5 text-xl font-semibold tracking-tight',
        3: 'mt-6 mb-2 text-md font-semibold',
        4: 'mt-4 mb-1.5 text-sm font-semibold uppercase tracking-wide text-muted',
      }[b.level]
      const H = (['h1', 'h2', 'h3', 'h4'] as const)[b.level - 1]
      return (
        <H id={b.id} className={cn('group scroll-mt-4 text-fg', cls)}>
          <Inl nodes={b.inl} chapter={chapter} />
          {b.level > 1 && (
            <Link to={guidePath(chapter, b.id, chapter)} aria-label={`Link to ${b.text}`} className="ml-2 inline-flex align-middle text-faint opacity-0 group-hover:opacity-100 focus-visible:opacity-100 print:hidden">
              <Link2 className="size-3.5" aria-hidden />
            </Link>
          )}
        </H>
      )
    }
    case 'p':
      return (
        <p className="my-2.5 leading-relaxed">
          <Inl nodes={b.inl} chapter={chapter} />
        </p>
      )
    case 'list':
      return <List b={b} chapter={chapter} />
    case 'table':
      return (
        <div className="my-3 overflow-x-auto rounded-md border border-border">
          <table className="w-full border-collapse text-sm">
            <thead className="bg-surface-2">
              <tr>
                {b.head.map((c, k) => (
                  <th key={k} className={cn('border-b border-border px-2.5 py-1.5 font-semibold text-fg', alignCls(b.align[k]))}>
                    <Inl nodes={c} chapter={chapter} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {b.rows.map((r, ri) => (
                <tr key={ri} className="border-b border-border last:border-b-0 even:bg-surface-2/40">
                  {r.map((c, k) => (
                    <td key={k} className={cn('px-2.5 py-1.5 align-top', alignCls(b.align[k]))}>
                      <Inl nodes={c} chapter={chapter} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'quote':
      return (
        <blockquote className={cn('my-3 rounded-r-md border-l-2 px-3 py-0.5 text-sm', quoteTone(b.blocks))}>
          <Blocks blocks={b.blocks} chapter={chapter} />
        </blockquote>
      )
    case 'pre':
      return (
        <pre className="num my-3 overflow-x-auto rounded-md border border-border bg-surface-2 px-3 py-2 text-xs leading-relaxed text-fg">
          <code>{b.v}</code>
        </pre>
      )
    case 'img': {
      const url = imageUrl(b.src)
      if (!url)
        return (
          <div role="img" aria-label={b.alt} className="my-3 rounded-md border border-dashed border-border-strong px-4 py-8 text-center text-xs text-muted">
            Screenshot not available in this build: {b.alt}
          </div>
        )
      return (
        <figure className="my-3">
          <a href={url} target="_blank" rel="noreferrer" title="Open the full-size image">
            <img src={url} alt={b.alt} decoding="async" className="max-w-full rounded-md border border-border" />
          </a>
        </figure>
      )
    }
    case 'hr':
      return <hr className="my-6 border-border" />
  }
}
