import { useRef, useState } from 'react'
import { studioApi } from '@/api/studio/client'
import type { CStudySpec, LintView } from '@/api/studio/types'

type Mode = 'auto' | 'async'

export interface LintGateState {
  lint: LintView
  spec: CStudySpec
  mode: Mode
  /** error-severity issues: the run waits for an explicit "Run anyway" */
  blocked: boolean
}

const LINT_TIMEOUT_MS = 8000

/** null lint (request failed / old backend) never blocks; error-severity issues do; warnings / notes are shown. */
export function gateDecision(lint: LintView | null): 'submit' | 'notes' | 'block' {
  if (!lint) return 'submit'
  if (!lint.ok || lint.issues.some((i) => i.severity === 'error')) return 'block'
  return lint.issues.length ? 'notes' : 'submit'
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('lint timeout')), ms)
    p.then(
      (v) => {
        clearTimeout(t)
        resolve(v)
      },
      (e: unknown) => {
        clearTimeout(t)
        reject(e instanceof Error ? e : new Error(String(e)))
      },
    )
  })
}

/**
 * v1.50 pre-run lint in front of the builder's Run: errors block until "Run anyway", warnings / notes are
 * shown without blocking. A failing lint request (old backend, timeout) never blocks the run.
 */
export function useLintGate(submit: (spec: CStudySpec, mode: Mode) => void) {
  const [state, setState] = useState<LintGateState | null>(null)
  const [checking, setChecking] = useState(false)
  const seq = useRef(0)

  const run = async (spec: CStudySpec, mode: Mode): Promise<void> => {
    const my = ++seq.current
    // leaderboard specs go to POST /studio/leaderboard; the candidates are linted by the engine itself
    if (spec.method === 'leaderboard') {
      setState(null)
      submit(spec, mode)
      return
    }
    setChecking(true)
    let lint: LintView | null = null
    try {
      lint = await withTimeout(studioApi.lint(spec), LINT_TIMEOUT_MS)
    } catch {
      lint = null
    } finally {
      if (my === seq.current) setChecking(false)
    }
    if (my !== seq.current) return
    const d = gateDecision(lint)
    if (lint && d === 'block') {
      setState({ lint, spec, mode, blocked: true })
      return
    }
    setState(lint && d === 'notes' ? { lint, spec, mode, blocked: false } : null)
    submit(spec, mode)
  }

  const runAnyway = () => {
    if (!state) return
    setState({ ...state, blocked: false })
    submit(state.spec, state.mode)
  }

  return { run: (spec: CStudySpec, mode: Mode) => void run(spec, mode), checking, state, runAnyway, dismiss: () => setState(null) }
}
