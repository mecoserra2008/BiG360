import { useDeferredValue, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useBqlFields } from '@/api/bql/hooks'
import { FUNCTIONS, tokenize, wordAt, type TokKind } from '@/lib/bql'
import { cn } from '@/lib/utils'

const TONE: Record<TokKind, string> = {
  clause: 'text-accent-text font-semibold',
  keyword: 'text-accent-text',
  func: 'text-info',
  field: 'text-fg',
  string: 'text-up',
  number: 'text-warn',
  date: 'text-warn',
  duration: 'text-warn',
  op: 'text-muted',
  punct: 'text-muted',
  ws: '',
  error: 'text-down underline decoration-wavy',
}

interface Suggestion {
  text: string
  kind: 'field' | 'func'
  detail: string
}

/**
 * Textarea with a syntax-highlighted overlay (no CodeMirror dependency) and field / function autocomplete from
 * GET /bql/fields. Ctrl+Enter runs; Tab / Enter accepts a suggestion; Esc closes the list.
 */
export function BqlEditor({ value, onChange, onRun, height = 168 }: { value: string; onChange: (v: string) => void; onRun: () => void; height?: number }) {
  const ta = useRef<HTMLTextAreaElement>(null)
  const pre = useRef<HTMLPreElement>(null)
  const [cursor, setCursor] = useState(0)
  const [open, setOpen] = useState(false)
  const [sel, setSel] = useState(0)
  const { word, start } = wordAt(value, cursor)
  const dword = useDeferredValue(word)
  const fields = useBqlFields(dword.length >= 2 ? dword : '', '', 8)
  const sugg = useMemo<Suggestion[]>(() => {
    if (word.length < 2) return []
    const lw = word.toLowerCase()
    const fn = FUNCTIONS.filter((f) => f.startsWith(lw) && f !== lw).slice(0, 4).map((f) => ({ text: f, kind: 'func' as const, detail: 'function' }))
    const fl = dword.length >= 2 ? (fields.data?.items ?? []).filter((f) => f.mnemonic.toLowerCase() !== lw).map((f) => ({ text: f.mnemonic, kind: 'field' as const, detail: `${f.unit} · ${f.description}` })) : []
    return [...fl, ...fn].slice(0, 9)
  }, [word, dword, fields.data])
  const showList = open && sugg.length > 0

  useLayoutEffect(() => {
    if (pre.current && ta.current) {
      pre.current.scrollTop = ta.current.scrollTop
      pre.current.scrollLeft = ta.current.scrollLeft
    }
  })

  const accept = (s: Suggestion) => {
    const after = value.slice(cursor)
    const insert = s.kind === 'func' && !after.startsWith('(') ? `${s.text}(` : s.text
    const next = value.slice(0, start) + insert + after
    onChange(next)
    setOpen(false)
    const pos = start + insert.length
    requestAnimationFrame(() => {
      ta.current?.focus()
      ta.current?.setSelectionRange(pos, pos)
      setCursor(pos)
    })
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      setOpen(false)
      onRun()
      return
    }
    if (showList) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSel((i) => (i + 1) % sugg.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSel((i) => (i - 1 + sugg.length) % sugg.length)
        return
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault()
        accept(sugg[Math.min(sel, sugg.length - 1)])
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setOpen(false)
        return
      }
    }
  }

  const toks = tokenize(value)
  const common = 'm-0 w-full whitespace-pre-wrap break-words px-3 py-2 font-mono text-[13px] leading-5'
  return (
    <div className="relative">
      <div className="relative overflow-hidden rounded-md border border-border bg-surface-2 focus-within:border-border-strong" style={{ height }}>
        <pre ref={pre} aria-hidden className={cn(common, 'pointer-events-none absolute inset-0 overflow-hidden text-fg')}>
          {toks.map((t, i) => (
            <span key={i} className={TONE[t.kind]}>
              {t.text}
            </span>
          ))}
          {'\n'}
        </pre>
        <textarea
          ref={ta}
          aria-label="BQL query"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          value={value}
          onChange={(e) => {
            onChange(e.target.value)
            setCursor(e.target.selectionStart)
            setOpen(true)
            setSel(0)
          }}
          onKeyDown={onKey}
          onKeyUp={(e) => setCursor(e.currentTarget.selectionStart)}
          onClick={(e) => setCursor(e.currentTarget.selectionStart)}
          onScroll={(e) => {
            if (pre.current) {
              pre.current.scrollTop = e.currentTarget.scrollTop
              pre.current.scrollLeft = e.currentTarget.scrollLeft
            }
          }}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          className={cn(common, 'absolute inset-0 h-full resize-none overflow-auto bg-transparent text-transparent caret-[var(--fg)] outline-none selection:bg-accent-soft')}
        />
      </div>
      {showList && (
        <ul role="listbox" aria-label="Suggestions" className="absolute top-full left-0 z-20 mt-1 max-h-64 w-[min(560px,100%)] overflow-y-auto rounded-md border border-border-strong bg-surface py-1 text-sm shadow-lg">
          {sugg.map((s, i) => (
            <li
              key={`${s.kind}:${s.text}`}
              role="option"
              aria-selected={i === sel}
              onMouseDown={(e) => {
                e.preventDefault()
                accept(s)
              }}
              className={cn('flex cursor-pointer items-baseline gap-2 px-2 py-1', i === sel && 'bg-surface-3')}
            >
              <span className={cn('num shrink-0 font-medium', s.kind === 'func' ? 'text-info' : 'text-fg')}>{s.text}</span>
              <span className="truncate text-2xs text-muted">{s.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
