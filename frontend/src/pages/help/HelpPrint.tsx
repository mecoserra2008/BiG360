import { useEffect, useState } from 'react'
import { loadGuide, type Guide } from './content'
import { Blocks } from './MarkdownView'

declare global {
  interface Window {
    /** set when every chapter and image has rendered (used by the PDF export) */
    __BIG360_GUIDE_READY__?: boolean
  }
}

/**
 * The whole guide on one page, outside the app shell, in the light theme: for the browser's Print / Save as PDF and
 * the headless PDF export. Each chapter starts on a new page. `?chapter=a,b` prints only those chapters (file names
 * without .md), which keeps each print job small.
 */
export default function HelpPrint() {
  const [guide, setGuide] = useState<Guide | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', 'light')
    document.title = 'BiG Equity 360 - User guide'
    loadGuide().then(setGuide, (e: unknown) => setError(String(e)))
  }, [])

  useEffect(() => {
    if (!guide) return
    let alive = true
    const imgs = [...document.querySelectorAll('img')]
    imgs.forEach((i) => (i.loading = 'eager'))
    Promise.all(
      imgs.map((i) =>
        i.complete
          ? Promise.resolve()
          : new Promise<void>((r) => {
              i.addEventListener('load', () => r(), { once: true })
              i.addEventListener('error', () => r(), { once: true })
            }),
      ),
    )
      .then(() => alive && (window.__BIG360_GUIDE_READY__ = true))
    return () => {
      alive = false
    }
  }, [guide])

  if (error) return <p className="p-6 text-sm text-down">Could not load the guide: {error}</p>
  if (!guide) return <p className="p-6 text-sm text-muted">Loading the guide…</p>
  const only = new URLSearchParams(window.location.search).get('chapter')?.split(',').filter(Boolean)
  const chapters = only?.length ? guide.chapters.filter((c) => only.includes(c.slug)) : guide.chapters

  return (
    <div data-guide-print className="mx-auto max-w-[1000px] bg-bg px-8 py-6 text-sm text-fg">
      {chapters.map((c, i) => (
        <section key={c.slug} className={i > 0 ? 'break-before-page pt-2' : ''} aria-label={c.title}>
          <Blocks blocks={c.blocks} chapter={c.slug} />
        </section>
      ))}
    </div>
  )
}
