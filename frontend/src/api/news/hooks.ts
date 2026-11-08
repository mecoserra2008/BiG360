import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { newsApi } from './client'
import { useNewsLive } from './stream'
import { type NewsPage, type TermFilter, type TermRow, filterQuery } from './types'

const FIRST_PAGE = 150
const PAGE = 1500
const MAX_ROWS = 6000

/** First page fetched as soon as the route chunk loads (before the first render): first paint < 1 s. */
const prefetched = new Map<string, { at: number; p: Promise<NewsPage> }>()
export function prefetchFirstPage(f: TermFilter): void {
  const q = filterQuery(f, FIRST_PAGE)
  const k = JSON.stringify(q)
  if (!prefetched.has(k)) prefetched.set(k, { at: Date.now(), p: newsApi.terminal(q) })
}
function firstPage(f: TermFilter): Promise<NewsPage> {
  const q = filterQuery(f, FIRST_PAGE)
  const k = JSON.stringify(q)
  const hit = prefetched.get(k)
  prefetched.delete(k)
  return hit && Date.now() - hit.at < 15_000 ? hit.p : newsApi.terminal(q)
}

/**
 * Terminal seed: a small first page (fast first paint), one older page right after, then older pages on demand
 * (`more()`, called when the list scrolls near its end) up to MAX_ROWS - the virtualised list renders only what is
 * visible. Re-seeds when the server-side filter changes.
 */
export function useTerminalSeed(f: TermFilter) {
  const key = JSON.stringify(filterQuery(f, 0))
  const [state, setState] = useState<{ loading: boolean; error: unknown; loaded: number; done: boolean }>({ loading: true, error: null, loaded: 0, done: false })
  const gen = useRef(0)
  const cursor = useRef<{ before: string | null; loaded: number; busy: boolean; f: TermFilter }>({ before: null, loaded: 0, busy: false, f })
  const more = useCallback(async () => {
    const c = cursor.current
    if (c.busy || !c.before || c.loaded >= MAX_ROWS) return
    const g = gen.current
    c.busy = true
    try {
      const page = await newsApi.terminal(filterQuery(c.f, PAGE, c.before))
      if (g !== gen.current) return
      useNewsLive.getState().appendOlder(page.items.map((x) => ({ ...x, arrivedAt: null }) as TermRow))
      c.loaded += page.items.length
      c.before = page.next_before ?? null
      setState({ loading: false, error: null, loaded: c.loaded, done: !c.before || c.loaded >= MAX_ROWS })
    } catch {
      /* next scroll retries */
    } finally {
      c.busy = false
    }
  }, [])
  useEffect(() => {
    const g = ++gen.current
    let cancelled = false
    setState({ loading: true, error: null, loaded: 0, done: false })
    useNewsLive.setState({ rows: useNewsLive.getState().rows.filter((r) => r.arrivedAt !== null) })
    ;(async () => {
      try {
        const first = await firstPage(f)
        if (cancelled || g !== gen.current) return
        useNewsLive.getState().seed(first.items.map((x) => ({ ...x, arrivedAt: null }) as TermRow), first.head_id ?? null)
        cursor.current = { before: first.next_before ?? null, loaded: first.items.length, busy: false, f }
        setState({ loading: false, error: null, loaded: first.items.length, done: !first.next_before })
        await new Promise((r) => setTimeout(r, 400))
        if (!cancelled && g === gen.current) await more()
      } catch (e) {
        if (!cancelled) setState({ loading: false, error: e, loaded: 0, done: true })
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return { ...state, more }
}

export const useNewsStory = (id: string | null) =>
  useQuery({
    queryKey: ['news', 'story', id],
    queryFn: () => newsApi.story(id as string),
    enabled: !!id,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    retry: false,
  })

export const useNewsStories = (q: { hours?: number; sort?: 'salience' | 'velocity' | 'recent' | 'size' }) =>
  useQuery({
    queryKey: ['news', 'stories', q],
    queryFn: () => newsApi.stories({ ...q, limit: 150 }),
    staleTime: 30_000,
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  })

export const useNewsHourly = (labelType: string, labelId: string, hours = 72) =>
  useQuery({
    queryKey: ['news', 'hourly', labelType, labelId, hours],
    queryFn: () => newsApi.hourly(labelType, labelId, hours),
    staleTime: 5 * 60_000,
  })

export const useNewsHealth = (refetchMs = 30_000, delayMs = 0) =>
  useQuery({
    queryKey: ['news', 'health'],
    queryFn: async () => {
      if (delayMs) await new Promise((r) => setTimeout(r, delayMs))   // after the terminal's first page
      return newsApi.health()
    },
    refetchInterval: refetchMs,
    staleTime: 10_000,
    retry: false,
  })

export const useNewsFeeds = () =>
  useQuery({
    queryKey: ['news', 'feeds'],
    queryFn: () => newsApi.feeds(),
    staleTime: 5 * 60_000,
  })
