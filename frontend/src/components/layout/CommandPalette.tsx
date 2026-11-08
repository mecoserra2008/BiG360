import { Command } from 'cmdk'
import { BookOpen, Building2, CircleHelp, Clock, Code2, CornerDownLeft, Moon, Search, Star, Sun, Terminal } from 'lucide-react'
import { useContext, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { UNSAFE_DataRouterContext, matchRoutes, useLocation, useNavigate } from 'react-router'
import { useSearch } from '@/api/hooks'
import { useBqlResolve } from '@/api/bql/hooks'
import { securityRoute, isInstrumentId } from '@/lib/routes'
import { globalMnemonics, helpText, loadHistory, mnemonic, mnemonicsFor, MNEMONICS, parseCommand, pushHistory, routeFor, type AssetClass, type CommandTarget } from '@/lib/mnemonics'
import { useUi, useWatchlist } from '@/store'
import { Flag, Kbd } from '../ui/data'
import { NAV } from './nav'

const itemCls =
  'flex h-8 cursor-pointer items-center gap-2.5 rounded-sm px-3 text-sm text-fg aria-disabled:opacity-50 aria-selected:bg-surface-2 [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted'

/** Does the SPA have a route for `path` (the News Terminal is installed separately)? */
function useHasRoute(): (path: string) => boolean {
  const ctx = useContext(UNSAFE_DataRouterContext)
  return (path: string) => {
    const routes = ctx?.router?.routes
    if (!routes) return false
    const m = matchRoutes(routes, path)
    return !!m && m[m.length - 1].route.path !== '*'
  }
}

export function CommandPalette() {
  const open = useUi((s) => s.paletteOpen)
  const setOpen = useUi((s) => s.setPaletteOpen)
  const theme = useUi((s) => s.theme)
  const toggleTheme = useUi((s) => s.toggleTheme)
  const watch = useWatchlist((s) => s.ids)
  const watchNames = useWatchlist((s) => s.names)
  const [q, setQ] = useState('')
  const [history, setHistory] = useState<string[]>(() => loadHistory())
  const [selected, setSelected] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const dq = useDeferredValue(q)
  const navigate = useNavigate()
  const location = useLocation()
  const hasRoute = useHasRoute()

  const cmd = useMemo(() => parseCommand(dq), [dq])
  // "AAPL US " (trailing space): suggest the mnemonics of that security
  const suggestRef = /\s$/.test(dq) && (cmd.kind === 'search' || cmd.kind === 'security') && dq.trim().split(/\s+/).length <= 3 ? dq.trim() : null
  const secRef = cmd.security ?? suggestRef ?? ''
  const resolved = useBqlResolve(secRef)
  const searchTerm = cmd.kind === 'search' || cmd.kind === 'security' ? dq : ''
  const results = useSearch(suggestRef ? '' : searchTerm)

  // the security of the current page (a bare `DES` / `GP` applies to it)
  const pageSec = useMemo(() => {
    const m = /^\/(company|instruments)\/([^/?#]+)/.exec(location.pathname)
    return m ? decodeURIComponent(m[2]) : null
  }, [location.pathname])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen(!useUi.getState().paletteOpen)
      } else if (e.key === '/' && !open && !location.pathname.startsWith('/news')) {
        // the News Terminal binds "/" to its own filter box (guide 08); Ctrl+K still opens the palette there
        const t = e.target as HTMLElement
        if (t.tagName !== 'INPUT' && t.tagName !== 'TEXTAREA' && t.tagName !== 'SELECT' && !t.isContentEditable) {
          e.preventDefault()
          setOpen(true)
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setOpen, location.pathname])

  const onOpenChange = (o: boolean) => {
    if (!o) setQ('')
    setOpen(o)
  }

  const go = (path: string, record?: string) => {
    if (record) setHistory(pushHistory(record))
    onOpenChange(false)
    navigate(path)
  }

  const sec = resolved.data?.security ?? null
  const cls: AssetClass = (resolved.data?.asset_class as AssetClass | null) ?? cmd.yellow ?? 'Equity'
  const instrument = sec ? isInstrumentId(sec.security_id) || cls !== 'Equity' : false

  /** The command item(s) for the parsed input. */
  const commandItems = useMemo(() => {
    const out: { key: string; label: string; detail: string; target: CommandTarget | null; record: string; note?: string }[] = []
    if (cmd.kind === 'bql') {
      const t = routeFor(cmd, { hasRoute })
      if (t) out.push({ key: 'bql', label: cmd.bql ? `Run BQL: ${cmd.bql.slice(0, 70)}` : 'Open the BQL console', detail: t.label, target: t, record: cmd.raw.trim() })
      return out
    }
    if (cmd.kind !== 'mnemonic' && cmd.kind !== 'security') return out
    const m = cmd.code ? mnemonic(cmd.code) : mnemonic('DES')
    if (!m) return out
    if (cmd.security) {
      if (!sec) return out
      const t = routeFor(cmd, { securityId: sec.security_id, ticker: sec.ticker, instrument, hasRoute })
      out.push({ key: 'cmd', label: `${sec.ticker} ${cls} · ${m.code} — ${m.title}`, detail: t ? t.path : m.unavailable ? `unavailable: ${m.unavailable}` : 'not applicable', target: t, record: cmd.raw.trim(), note: t?.note })
      return out
    }
    const scopedId = m.scope !== 'global' ? pageSec : null
    const t = routeFor(cmd, { securityId: scopedId ?? undefined, ticker: scopedId ?? undefined, instrument: scopedId ? isInstrumentId(scopedId) : false, hasRoute })
    out.push({
      key: 'cmd',
      label: `${m.code}${cmd.arg ? ` ${cmd.arg}` : ''} — ${m.title}${scopedId ? ` (${scopedId})` : ''}`,
      detail: t ? t.path : m.scope === 'security' ? `type a ticker first: AAPL ${m.code}` : m.unavailable ?? '',
      target: t,
      record: cmd.raw.trim(),
      note: t?.note,
    })
    return out
  }, [cmd, sec, cls, instrument, hasRoute, pageSec])

  // keep the first enabled item selected as the groups change (items render after the deferred parse / fetches)
  const resultsKey = `${dq}|${commandItems.map((c) => c.target?.path ?? '').join(',')}|${results.data?.length ?? 0}|${sec?.security_id ?? ''}`
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      const el = listRef.current?.querySelector('[cmdk-item]:not([aria-disabled="true"])')
      setSelected(el?.getAttribute('data-value') ?? '')
    })
    return () => cancelAnimationFrame(id)
  }, [resultsKey, open])

  const ql = q.trim().toLowerCase()
  const navMatches = cmd.kind === 'search' || cmd.kind === 'empty' ? NAV.filter((n) => !ql || n.label.toLowerCase().includes(ql) || n.description.toLowerCase().includes(ql)) : []
  const showHelpList = cmd.kind === 'mnemonic' && cmd.code === 'HELP'
  const suggest = suggestRef && sec ? mnemonicsFor(cls) : []
  const globalMatches = cmd.kind === 'search' && ql.length >= 1 && ql.length <= 5 ? globalMnemonics().filter((m) => m.code.toLowerCase().startsWith(ql)) : []

  return (
    <Command.Dialog
      open={open}
      onOpenChange={onOpenChange}
      label="Command palette"
      shouldFilter={false}
      value={selected}
      onValueChange={setSelected}
      loop
      overlayClassName="fixed inset-0 z-40 bg-black/50"
      contentClassName="fixed top-[12vh] left-1/2 z-50 w-[min(680px,calc(100vw-32px))] -translate-x-1/2 overflow-hidden rounded-md border border-border-strong bg-surface"
    >
      <div className="flex items-center gap-2 border-b border-border px-3">
        <Search className="size-4 text-muted" aria-hidden />
        <Command.Input
          value={q}
          onValueChange={setQ}
          placeholder="Ticker, company, ISIN — or a command: AAPL US Equity DES · SPX Index GP · ECO · WIRP US · HELP"
          className="h-11 flex-1 bg-transparent text-md text-fg outline-none placeholder:text-faint"
        />
        <Kbd>Esc</Kbd>
      </div>
      <Command.List ref={listRef} className="max-h-[min(60vh,460px)] overflow-y-auto p-1.5">
        <Command.Empty className="px-3 py-6 text-center text-sm text-muted">
          {results.isFetching || resolved.isFetching ? 'Searching…' : secRef && resolved.data && !sec ? `Unknown security “${secRef}”.` : 'No matches.'}
        </Command.Empty>

        {commandItems.length > 0 && (
          <Command.Group heading="Command">
            {commandItems.map((c) => (
              <Command.Item key={c.key} value={`cmd:${c.key}`} disabled={!c.target} onSelect={() => c.target && go(c.target.path, c.record)} className={itemCls}>
                <Terminal />
                <span className="num min-w-0 flex-1 truncate font-medium">{c.label}</span>
                <span className="max-w-[45%] truncate text-xs text-faint">{c.note ?? c.detail}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {cmd.kind === 'help' && (
          <Command.Group heading="Help">
            <Command.Item value="help:text" onSelect={() => setQ(cmd.helpFor && mnemonic(cmd.helpFor)?.scope === 'global' ? cmd.helpFor : `${cmd.helpFor ?? ''} `)} className="flex cursor-pointer items-start gap-2.5 rounded-sm px-3 py-1.5 text-sm text-fg aria-selected:bg-surface-2 [&_svg]:mt-0.5 [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted">
              <CircleHelp />
              <span className="whitespace-normal">{helpText(cmd.helpFor ?? '')}</span>
            </Command.Item>
            <Command.Item value="help:guide" onSelect={() => go('/help/14-bql-mnemonics#mnemonic-reference', cmd.raw.trim())} className={itemCls}>
              <BookOpen />
              <span>Open the mnemonic reference in the user guide</span>
            </Command.Item>
          </Command.Group>
        )}

        {showHelpList && (
          <Command.Group heading="Mnemonics (HELP <code> for details)">
            {MNEMONICS.map((m) => (
              <Command.Item key={m.code} value={`help:${m.code}`} onSelect={() => setQ(`HELP ${m.code}`)} className={itemCls}>
                <span className="num w-14 shrink-0 font-semibold">{m.code}</span>
                <span className="min-w-0 flex-1 truncate">{m.title}</span>
                <span className="shrink-0 text-xs text-faint">{m.scope === 'global' ? 'global' : m.scope === 'security' ? '<ticker>' : 'both'}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {suggest.length > 0 && sec && (
          <Command.Group heading={`${sec.ticker} ${cls} — functions`}>
            {suggest.map((m) => {
              const c = parseCommand(`${dq.trim()} ${m.code}`)
              const t = routeFor(c, { securityId: sec.security_id, ticker: sec.ticker, instrument, hasRoute })
              return (
                <Command.Item key={m.code} value={`fn:${m.code}`} disabled={!t} onSelect={() => t && go(t.path, `${dq.trim()} ${m.code}`)} className={itemCls}>
                  <span className="num w-14 shrink-0 font-semibold">{m.code}</span>
                  <span className="min-w-0 flex-1 truncate">{m.title}</span>
                  <span className="max-w-[40%] truncate text-xs text-faint">{t?.note ?? m.unavailable ?? ''}</span>
                </Command.Item>
              )
            })}
          </Command.Group>
        )}

        {globalMatches.length > 0 && (
          <Command.Group heading="Functions">
            {globalMatches.map((m) => {
              const t = routeFor({ kind: 'mnemonic', raw: m.code, code: m.code }, { hasRoute })
              return (
                <Command.Item key={m.code} value={`gfn:${m.code}`} disabled={!t} onSelect={() => t && go(t.path, m.code)} className={itemCls}>
                  <Code2 />
                  <span className="num w-14 shrink-0 font-semibold">{m.code}</span>
                  <span className="min-w-0 flex-1 truncate">{m.title}</span>
                  <span className="max-w-[40%] truncate text-xs text-faint">{t?.note ?? m.description}</span>
                </Command.Item>
              )
            })}
          </Command.Group>
        )}

        {!suggestRef && searchTerm.trim() && (results.data?.length ?? 0) > 0 && (
          <Command.Group heading="Securities">
            {results.data?.map((r) => (
              <Command.Item key={r.id} value={`sec:${r.id}`} onSelect={() => go(securityRoute(r.id))} className={itemCls}>
                <Flag country={r.country} />
                <span className="num w-24 shrink-0 truncate font-semibold">{r.ticker}</span>
                <span className="min-w-0 flex-1 truncate">{r.name}</span>
                <span className="num hidden shrink-0 text-xs text-faint sm:inline">{r.isin}</span>
                <span className="w-24 shrink-0 truncate text-right text-xs text-muted">{r.exchange}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {!ql && history.length > 0 && (
          <Command.Group heading="Recent commands">
            {history.slice(0, 6).map((h) => (
              <Command.Item key={h} value={`hist:${h}`} onSelect={() => setQ(h)} className={itemCls}>
                <Clock /> <span className="num truncate">{h}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {!ql && watch.length > 0 && (
          <Command.Group heading="Watchlist">
            {watch.slice(0, 8).map((id) => (
              <Command.Item key={id} value={`wl:${id} ${watchNames[id]?.ticker ?? ''} ${watchNames[id]?.name ?? ''}`} onSelect={() => go(securityRoute(id))} className={itemCls}>
                <Star /> <span className="num font-semibold">{watchNames[id]?.ticker ?? id}</span>
                {watchNames[id]?.name && <span className="ml-2 truncate text-xs text-faint">{watchNames[id].name}</span>}
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {navMatches.length > 0 && (
          <Command.Group heading="Go to">
            {navMatches.map((n) => (
              <Command.Item key={n.to} value={`nav:${n.to}`} onSelect={() => go(n.to)} className={itemCls}>
                <n.icon />
                <span>{n.label}</span>
                <span className="ml-auto truncate text-xs text-faint">{n.description}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {(!ql || 'theme'.includes(ql) || 'dark light'.includes(ql)) && (
          <Command.Group heading="Actions">
            <Command.Item value="act:theme" onSelect={() => { toggleTheme(); onOpenChange(false) }} className={itemCls}>
              {theme === 'dark' ? <Sun /> : <Moon />} Switch to {theme === 'dark' ? 'light' : 'dark'} theme
            </Command.Item>
            <Command.Item value="act:msft" onSelect={() => go('/company/MSFT')} className={itemCls}>
              <Building2 /> Open example: Microsoft 360
            </Command.Item>
            <Command.Item value="act:help" onSelect={() => setQ('HELP')} className={itemCls}>
              <CircleHelp /> Command bar help (mnemonics)
            </Command.Item>
            <Command.Item value="act:guide" onSelect={() => go('/help')} className={itemCls}>
              <BookOpen /> Open the user guide
            </Command.Item>
          </Command.Group>
        )}
      </Command.List>
      <div className="flex items-center gap-3 border-t border-border px-3 py-1.5 text-2xs text-faint">
        <span className="inline-flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> navigate</span>
        <span className="inline-flex items-center gap-1"><Kbd><CornerDownLeft className="size-2.5" /></Kbd> open</span>
        <span className="inline-flex items-center gap-1"><Kbd>/</Kbd> or <Kbd>Ctrl K</Kbd> toggle</span>
        <span className="ml-auto">AAPL US Equity GP · ECO · HELP DES · BQL get(…)</span>
      </div>
    </Command.Dialog>
  )
}
