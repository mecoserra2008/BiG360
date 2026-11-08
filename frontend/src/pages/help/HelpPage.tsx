import { BookOpen, Printer, Search, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router'
import { buildToc } from '@/lib/guide/toc'
import { searchSections } from '@/lib/guide/search'
import { ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Kbd } from '@/components/ui/data'
import { cn } from '@/lib/utils'
import { loadGuide, type Guide } from './content'
import { guidePath } from '@/lib/guide/helpMap'
import { Blocks } from './MarkdownView'

function useGuide(): { guide: Guide | null; error: unknown } {
  const [guide, setGuide] = useState<Guide | null>(null)
  const [error, setError] = useState<unknown>(null)
  useEffect(() => {
    let alive = true
    loadGuide().then(
      (g) => alive && setGuide(g),
      (e) => alive && setError(e),
    )
    return () => {
      alive = false
    }
  }, [])
  return { guide, error }
}

/** Scroll to the hash anchor after the chapter has rendered (and whenever the hash changes). */
function useAnchorScroll(ready: boolean, chapter: string) {
  const { hash } = useLocation()
  useEffect(() => {
    if (!ready) return
    const id = decodeURIComponent(hash.replace(/^#/, ''))
    const main = document.getElementById('main')
    if (!id) {
      main?.scrollTo({ top: 0 })
      return
    }
    // images above the anchor load after the first scroll and push it down: re-scroll a few times until the layout
    // settles, unless the reader scrolls first
    let stop = false
    const halt = () => {
      stop = true
    }
    const go = () => !stop && document.getElementById(id)?.scrollIntoView({ block: 'start' })
    const timers = [30, 250, 700, 1500, 2500].map((ms) => window.setTimeout(go, ms))
    window.addEventListener('wheel', halt, { passive: true, once: true })
    window.addEventListener('keydown', halt, { once: true })
    window.addEventListener('pointerdown', halt, { once: true })
    return () => {
      timers.forEach((t) => window.clearTimeout(t))
      window.removeEventListener('wheel', halt)
      window.removeEventListener('keydown', halt)
      window.removeEventListener('pointerdown', halt)
    }
  }, [hash, ready, chapter])
}

export default function HelpPage() {
  const { chapter: param } = useParams()
  const slug = param ?? 'README'
  const { guide, error } = useGuide()
  const [q, setQ] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()
  const chapter = guide?.bySlug.get(slug) ?? null
  useAnchorScroll(!!chapter && !q.trim(), slug)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (e.key === 'f' && (e.ctrlKey || e.metaKey) && e.shiftKey) {
        e.preventDefault()
        inputRef.current?.focus()
      } else if (e.key === 'Escape' && t === inputRef.current) setQ('')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const hits = useMemo(() => (guide && q.trim() ? searchSections(guide.sections, q, 60) : []), [guide, q])
  const toc = useMemo(() => (chapter ? buildToc(chapter.blocks) : []), [chapter])
  const idx = guide ? guide.chapters.findIndex((c) => c.slug === slug) : -1
  const prev = guide && idx > 0 ? guide.chapters[idx - 1] : null
  const next = guide && idx >= 0 && idx < guide.chapters.length - 1 ? guide.chapters[idx + 1] : null

  if (error) return <ErrorState error={error} className="rounded-md border border-border bg-surface" />

  return (
    <div className="flex items-start gap-4">
      <aside aria-label="Guide contents" className="sticky top-0 hidden max-h-[calc(100vh-7rem)] w-64 shrink-0 overflow-y-auto rounded-md border border-border bg-surface p-2 lg:block print:hidden">
        <div className="mb-2 flex items-center gap-2 px-1.5 pt-1 text-sm font-semibold text-fg">
          <BookOpen className="size-4 text-accent" aria-hidden /> User guide
        </div>
        {!guide ? (
          <SkeletonRows rows={14} className="px-1.5" />
        ) : (
          <ul className="space-y-px text-xs">
            {guide.chapters.map((c) => {
              const on = c.slug === slug
              return (
                <li key={c.slug}>
                  <Link
                    to={guidePath(c.slug, null, c.slug)}
                    onClick={() => setQ('')}
                    aria-current={on ? 'page' : undefined}
                    className={cn('block rounded-sm px-1.5 py-1 leading-snug', on ? 'bg-surface-3 font-semibold text-fg' : 'text-muted hover:bg-surface-2 hover:text-fg')}
                  >
                    {c.slug === 'README' ? 'Contents' : c.title}
                  </Link>
                  {on && toc.length > 0 && (
                    <ul className="mt-px mb-1 ml-2 space-y-px border-l border-border pl-1.5">
                      {toc.map((t) => (
                        <li key={t.id}>
                          <Link to={guidePath(c.slug, t.id, c.slug)} className="block truncate rounded-sm px-1.5 py-0.5 text-muted hover:bg-surface-2 hover:text-fg" title={t.text}>
                            {t.text}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </aside>

      <div className="min-w-0 flex-1">
        <div className="mb-3 flex flex-wrap items-center gap-2 print:hidden">
          <label className="flex h-8 min-w-[260px] flex-1 items-center gap-2 rounded-md border border-border bg-surface px-2 text-sm focus-within:border-border-strong">
            <Search className="size-3.5 shrink-0 text-faint" aria-hidden />
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search the guide (e.g. first-passage, ECO, valuation confidence)"
              aria-label="Search the guide"
              className="h-full min-w-0 flex-1 bg-transparent text-fg outline-none placeholder:text-faint"
            />
            {q ? (
              <button type="button" aria-label="Clear search" onClick={() => setQ('')} className="text-faint hover:text-fg">
                <X className="size-3.5" />
              </button>
            ) : (
              <span className="hidden gap-0.5 sm:flex">
                <Kbd>Ctrl</Kbd>
                <Kbd>Shift</Kbd>
                <Kbd>F</Kbd>
              </span>
            )}
          </label>
          <Link to="/help/print" target="_blank" rel="noreferrer" className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-xs text-muted hover:text-fg" title="Open the whole guide on one page for printing / saving as PDF">
            <Printer className="size-3.5" aria-hidden /> Print / PDF
          </Link>
        </div>

        {q.trim() ? (
          <section aria-label="Search results" className="rounded-md border border-border bg-surface">
            <header className="border-b border-border px-3 py-1.5 text-xs text-muted">
              {!guide ? 'Loading the guide…' : `${hits.length}${hits.length === 60 ? '+' : ''} result${hits.length === 1 ? '' : 's'} for “${q.trim()}”`}
            </header>
            <ul className="divide-y divide-border">
              {hits.map((h, i) => (
                <li key={i}>
                  <button
                    type="button"
                    onClick={() => {
                      setQ('')
                      navigate(guidePath(h.section.chapter, h.section.anchor, h.section.chapter))
                    }}
                    className="block w-full px-3 py-2 text-left hover:bg-surface-2"
                  >
                    <div className="text-2xs tracking-wide text-faint uppercase">{h.section.trail.join(' › ')}</div>
                    <div className="text-sm font-medium text-fg">{h.section.heading || h.section.chapterTitle}</div>
                    <div className="mt-0.5 text-xs text-muted">
                      {h.snippet.map((r, k) => (r.hit ? <mark key={k} className="rounded-sm bg-accent-soft px-px text-accent-text">{r.text}</mark> : <span key={k}>{r.text}</span>))}
                    </div>
                  </button>
                </li>
              ))}
              {guide && !hits.length && <li className="px-3 py-6 text-center text-sm text-muted">No section contains every word of the query.</li>}
            </ul>
          </section>
        ) : !guide ? (
          <SkeletonRows rows={20} />
        ) : !chapter ? (
          <div className="rounded-md border border-border bg-surface px-4 py-10 text-center text-sm text-muted">
            No guide chapter “{slug}”. <Link to="/help" className="text-accent-text hover:underline">Open the contents</Link>.
          </div>
        ) : (
          <article className="max-w-[1080px] rounded-md border border-border bg-surface px-6 py-5 text-sm text-fg/90">
            <Blocks blocks={chapter.blocks} chapter={chapter.slug} />
            <nav aria-label="Chapter navigation" className="mt-10 flex justify-between gap-4 border-t border-border pt-3 text-xs print:hidden">
              {prev ? (
                <Link to={guidePath(prev.slug, null, prev.slug)} className="text-muted hover:text-fg">
                  ← {prev.slug === 'README' ? 'Contents' : prev.title}
                </Link>
              ) : (
                <span />
              )}
              {next && (
                <Link to={guidePath(next.slug, null, next.slug)} className="text-right text-muted hover:text-fg">
                  {next.title} →
                </Link>
              )}
            </nav>
          </article>
        )}
      </div>
    </div>
  )
}
