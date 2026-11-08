import { useQueries, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { isTerminal } from '@/api/studio/adapters'
import { studioApi } from '@/api/studio/client'
import { sk } from '@/api/studio/hooks'
import { toast } from '@/components/ui/toast'
import { fmtSeconds } from './fmt'
import { useStudio } from './store'

/**
 * App-wide watcher for queued Model Studio runs: polls each until it finishes and raises a toast,
 * so a long run can be left running while the user works elsewhere.
 */
export function RunWatcher() {
  const watching = useStudio((s) => s.watching)
  const unwatch = useStudio((s) => s.unwatch)
  const setRun = useStudio((s) => s.setRun)
  const setTab = useStudio((s) => s.setTab)
  const qc = useQueryClient()
  const results = useQueries({
    queries: watching.map((id) => ({
      queryKey: sk.run(id),
      queryFn: () => studioApi.run(id),
      staleTime: Infinity,
      retry: 1,
      refetchInterval: (q: { state: { data?: { status: { status: string } } } }) => {
        const s = q.state.data?.status.status
        return s && isTerminal(s as never) ? false : 2000
      },
    })),
  })
  useEffect(() => {
    results.forEach((r, i) => {
      const id = watching[i]
      if (!id) return
      if (r.isError) {
        unwatch(id)
        return
      }
      const run = r.data
      if (!run || !isTerminal(run.status.status)) return
      unwatch(id)
      void qc.invalidateQueries({ queryKey: sk.runs() })
      void qc.invalidateQueries({ queryKey: sk.studies() })
      const name = run.status.name ?? run.status.method
      if (run.status.status === 'completed')
        toast({
          tone: 'success',
          title: `Run finished: ${name}`,
          message: `${run.result?.title ?? run.status.method} · ${fmtSeconds(run.status.durationS)}${run.result?.warnings.some((w) => w.severity !== 'info') ? ' · check the warnings' : ''}`,
        })
      else if (run.status.status === 'failed') toast({ tone: 'error', title: `Run failed: ${name}`, message: run.status.error ?? undefined })
      else toast({ tone: 'info', title: `Run cancelled: ${name}` })
      if (useStudio.getState().runId === null) {
        setRun(id)
        setTab('builder')
      }
    })
  }, [results, watching, unwatch, qc, setRun, setTab])
  return null
}
